#!/usr/bin/env python3
# Health agent. Runs on every client laptop (real mode) or on every Mininet
# host h1 .. h4 (--sim mode).
#
# Real mode (default): reads the REAL CPU, memory, processes, network, disk,
# temperature and battery of this computer with psutil (see metrics.py).
# Sim mode (--sim, used by topo.py in Mininet): all Mininet hosts share one
# PC, so their CPU/memory would be identical. There each agent simulates its
# own server (FakeServer below).
#
# Every second it sends a REPORT to the monitor on UDP port 5000.
# When a value crosses a limit it also sends an ALERT to UDP port 5001.
# Alerts must be answered with an ACK. If no ACK comes back, the alert
# is sent again (wait 0.5s, then 1s, then 2s ...).
#
# The agent does NOT mark its own packets as urgent. In smart mode the SDN
# controller does that in the switch, so a host can't cheat.
#
# In real mode the agent also sends a small PING to the alert port every
# 5 seconds. The monitor answers with an ACK, so we measure the real round
# trip time (RTT) over the LAN and put it in the next REPORT.
#
# Usage (real laptop):  python agent.py --monitor 192.168.1.20
#                       python agent.py                 (find the monitor by broadcast)
# Usage (Mininet):      python3 agent.py --sim --name h1
#                       python3 agent.py --sim --name h3 --flood 300 --seconds 20

import argparse
import json
import os
import queue
import random
import signal
import socket
import sys
import tempfile
import threading
import time

from common import (load_config, real_config, make_msg, read_msg, check_level, is_number,
                    clean_name, udp_socket, ConfigError, METRICS,
                    REPORT, ALERT, ACK, FLOOD, PING, DISCOVER, I_AM_MONITOR)

# topo.py writes "spike" commands here (Mininet). On Windows use the temp folder.
STATE_DIR = "/tmp/hmon" if os.name == "posix" else os.path.join(tempfile.gettempdir(), "hmon")
MODES = ("normal", "cpu", "mem", "procs")
MAX_WAITING_ALERTS = 50   # if the monitor is gone, don't keep alerts forever
PING_EVERY = 5.0          # real mode: measure the RTT to the monitor this often

# extra fields a real REPORT carries besides cpu / mem / procs / net_kbps
EXTRA_FIELDS = ("up_kbps", "down_kbps", "disk", "swap", "temp_c", "battery", "plugged",
                "uptime_h", "cores", "mem_gb", "os", "top")


class FakeServer:
    """Makes believable CPU / memory / process numbers.

    All Mininet hosts run on the same real machine, so real CPU and memory
    would be the same for every host. Instead each agent simulates its own
    server. The network number is real (read from /proc/net/dev)."""

    def __init__(self, name):
        self.name = name
        rnd = random.Random(name)          # same name -> same starting point
        self.cpu = rnd.uniform(20, 40)
        self.mem = rnd.uniform(40, 55)
        self.procs = rnd.randint(150, 200)
        self.last_bytes = None
        self.last_time = None

    def mode(self):
        # "normal", "cpu", "mem" or "procs". Set by the spike / calm commands.
        try:
            with open(os.path.join(STATE_DIR, self.name + ".state")) as f:
                mode = f.read(32).strip()
        except (OSError, UnicodeDecodeError):
            return "normal"
        return mode if mode in MODES else "normal"

    def read(self):
        mode = self.mode()

        # move a little towards a target, plus some noise
        cpu_target = 95 if mode == "cpu" else 30
        mem_target = 94 if mode == "mem" else 50
        procs_target = 450 if mode == "procs" else 180
        speed = 0.6 if mode != "normal" else 0.3

        self.cpu += (cpu_target - self.cpu) * speed + random.uniform(-3, 3)
        self.mem += (mem_target - self.mem) * speed + random.uniform(-1, 1)
        self.procs += int((procs_target - self.procs) * speed) + random.randint(-3, 3)

        self.cpu = min(max(self.cpu, 1), 100)
        self.mem = min(max(self.mem, 5), 100)
        self.procs = max(self.procs, 50)

        return {
            "cpu": round(self.cpu, 1),
            "mem": round(self.mem, 1),
            "procs": self.procs,
            "net_kbps": self.net_kbps(),
        }

    def net_kbps(self):
        """Real traffic (in + out) of this host, in kilobits per second."""
        total = 0
        try:
            with open("/proc/net/dev") as f:
                for line in f.readlines()[2:]:
                    iface, data = line.split(":", 1)
                    if iface.strip() == "lo":
                        continue
                    cols = data.split()
                    total += int(cols[0]) + int(cols[8])   # rx bytes + tx bytes
        except (OSError, ValueError, IndexError):
            return 0
        now = time.time()
        kbps = 0
        if self.last_bytes is not None and now > self.last_time:
            kbps = (total - self.last_bytes) * 8 / 1000 / (now - self.last_time)
        self.last_bytes, self.last_time = total, now
        return round(max(kbps, 0), 1)


class Agent:
    def __init__(self, name, cfg, monitor_ip, report_port, alert_port, real=False):
        self.name = name
        self.cfg = cfg
        self.real = real
        self.report_addr = (monitor_ip, report_port)
        self.alert_addr = (monitor_ip, alert_port)
        if real:
            from metrics import RealMachine
            self.server = RealMachine()
        else:
            self.server = FakeServer(name)
        # reports and alerts have their own counters, so the monitor can
        # find lost reports by looking for gaps
        self.report_seq = 0
        self.alert_seq = 0
        self.boot = int(time.time() * 1000)   # tells the monitor if we restarted
        self.levels = {}                        # metric -> last level we alerted
        self.alert_queue = queue.Queue(maxsize=MAX_WAITING_ALERTS)
        self.running = True

        # socket for normal reports, and a separate one for alerts + ACKs
        self.report_sock = udp_socket()
        self.alert_sock = udp_socket()
        self.sock_lock = threading.Lock()   # alerts and pings share alert_sock

        self.rtt_ms = None        # last measured round trip time to the monitor
        self.last_ping = 0
        self.pings_sent = 0
        self.pings_answered = 0
        self.last_error_log = 0

        self.sent_reports = 0
        self.made_alerts = 0      # alerts we wanted to send
        self.sent_alerts = 0      # alerts the monitor ACKed
        self.failed_alerts = 0    # alerts that got no ACK after all tries
        self.dropped_alerts = 0   # alerts thrown away because the queue was full
        self.tries_total = 0

    # ---------------- reports ----------------

    def send_report(self):
        stats = self.server.read()
        self.report_seq += 1
        fields = {k: stats[k] for k in METRICS}
        fields.update({k: stats[k] for k in EXTRA_FIELDS if k in stats})
        if self.real:
            fields["rtt_ms"] = self.rtt_ms
        try:
            data = make_msg(REPORT, self.name, self.report_seq, boot=self.boot, **fields)
        except ValueError:
            # too big for one packet (very long process names?): leave the extras out
            data = make_msg(REPORT, self.name, self.report_seq, boot=self.boot,
                            **{k: stats[k] for k in METRICS})
        try:
            self.report_sock.sendto(data, self.report_addr)
            self.sent_reports += 1
        except OSError as e:
            # Wi-Fi dropped, cable cut ... just try again next second
            self.log_error("could not send report: %s" % e)
        self.check_limits(stats)

    def log_error(self, text):
        # when the network is gone this would repeat every second, so at most every 10 s
        now = time.time()
        if now - self.last_error_log >= 10:
            self.last_error_log = now
            log(self.name, text)

    def check_limits(self, stats):
        """Send an alert when a metric changes level (OK -> WARN -> CRIT, or back to OK)."""
        for metric, limit in self.cfg["limits"].items():
            if not is_number(stats.get(metric)):
                continue      # this computer can't measure it (e.g. no temperature sensor)
            level = check_level(stats[metric], limit)
            old = self.levels.get(metric, "OK")
            if level != old:
                self.levels[metric] = level
                self.made_alerts += 1
                alert = {
                    "metric": metric,
                    "value": stats[metric],
                    "level": level,
                    "limit": limit["crit"] if level == "CRIT" else limit["warn"],
                }
                try:
                    self.alert_queue.put_nowait(alert)
                except queue.Full:
                    # monitor unreachable for a long time: drop the oldest alert
                    try:
                        self.alert_queue.get_nowait()
                        self.dropped_alerts += 1
                    except queue.Empty:
                        pass
                    try:
                        self.alert_queue.put_nowait(alert)
                    except queue.Full:
                        self.dropped_alerts += 1

    # ---------------- alerts ----------------

    def alert_worker(self):
        """Sends alerts one by one and waits for an ACK for each."""
        while self.running:
            try:
                alert = self.alert_queue.get(timeout=0.5)
            except queue.Empty:
                if self.real and time.time() - self.last_ping >= PING_EVERY:
                    self.ping()
                continue
            self.send_alert(alert)

    def ping(self):
        """Send one PING to the alert port and time the ACK (real round trip time)."""
        self.last_ping = time.time()
        self.alert_seq += 1
        seq = self.alert_seq
        self.pings_sent += 1
        start = time.time()
        try:
            with self.sock_lock:
                self.alert_sock.sendto(make_msg(PING, self.name, seq, boot=self.boot),
                                       self.alert_addr)
        except OSError as e:
            self.log_error("could not send ping: %s" % e)
            self.rtt_ms = None
            return None
        if self.wait_for_ack(seq, 1.0):
            self.pings_answered += 1
            self.rtt_ms = round((time.time() - start) * 1000, 2)
        else:
            self.rtt_ms = None
        return self.rtt_ms

    def send_alert(self, alert):
        self.alert_seq += 1
        seq = self.alert_seq
        wait = self.cfg["ack_wait"]
        first_sent = time.time()

        for attempt in range(1, self.cfg["max_tries"] + 1):
            if not self.running and attempt > 1:
                break
            # keep the first send time so the monitor measures the real delay
            data = make_msg(ALERT, self.name, seq, boot=self.boot, tries=attempt,
                            first_sent=first_sent, **alert)
            self.tries_total += 1
            sent_at = time.time()
            try:
                with self.sock_lock:
                    self.alert_sock.sendto(data, self.alert_addr)
            except OSError as e:
                self.log_error("could not send alert: %s" % e)

            if self.wait_for_ack(seq, wait):
                self.sent_alerts += 1
                log(self.name, "ALERT %s %s=%s acked in %.1f ms (try %d)"
                    % (alert["level"], alert["metric"], alert["value"],
                       (time.time() - sent_at) * 1000, attempt))
                return True
            wait *= 2   # wait longer each time

        self.failed_alerts += 1
        log(self.name, "ALERT %s %s gave up after %d tries"
            % (alert["level"], alert["metric"], self.cfg["max_tries"]))
        return False

    def wait_for_ack(self, seq, wait):
        end = time.time() + wait
        while True:
            left = end - time.time()
            if left <= 0:
                return False
            self.alert_sock.settimeout(left)
            try:
                data, _ = self.alert_sock.recvfrom(2048)
            except socket.timeout:
                return False
            except (ConnectionRefusedError, ConnectionResetError):
                # "port unreachable" from the monitor host: keep waiting
                continue
            except OSError:
                return False
            msg = read_msg(data)
            # ignore late ACKs for older alerts
            if msg and msg["type"] == ACK and msg["seq"] == seq:
                return True

    # ---------------- main loop ----------------

    def run(self, count=0):
        log(self.name, "started (%s data), sending to %s:%d (alerts to port %d)"
            % ("REAL" if self.real else "simulated", self.report_addr[0],
               self.report_addr[1], self.alert_addr[1]))
        if self.real:
            self.first_check()
        t = threading.Thread(target=self.alert_worker, daemon=True)
        t.start()

        every = self.cfg["report_every"]
        next_time = time.time()
        sent = 0
        while self.running:
            self.send_report()
            sent += 1
            if sent % 5 == 0:
                self.save_stats()
            if count and sent >= count:
                break
            next_time += every
            pause = next_time - time.time()
            if pause > 0:
                time.sleep(pause)
            else:
                next_time = time.time()   # we fell behind, don't try to catch up

        # give pending alerts a moment to finish
        end = time.time() + 3
        while not self.alert_queue.empty() and time.time() < end:
            time.sleep(0.1)
        self.running = False
        t.join(timeout=10)
        self.save_stats()
        self.report_sock.close()
        self.alert_sock.close()
        log(self.name, "stopped. reports=%d alerts=%d failed_alerts=%d"
            % (self.sent_reports, self.sent_alerts, self.failed_alerts))

    def first_check(self):
        """Real mode: ping the monitor once at the start and say clearly if it answers."""
        for _ in range(3):
            if self.ping() is not None:
                log(self.name, "monitor %s answered, round trip %.1f ms. Sending reports..."
                    % (self.alert_addr[0], self.rtt_ms))
                return True
        log(self.name, "WARNING: no answer from the monitor at %s:%d. Reports are sent "
            "anyway. Check: is monitor.py --lan running? right IP? firewall on the "
            "monitor laptop open (guide.md)? same Wi-Fi?" % self.alert_addr)
        return False

    def save_stats(self):
        """Write our counters to /tmp/hmon/<name>.stats (the experiments read it)."""
        stats = {"node": self.name, "time": time.time(), "boot": self.boot,
                 "reports": self.sent_reports, "alerts_made": self.made_alerts,
                 "alerts_acked": self.sent_alerts, "alerts_failed": self.failed_alerts,
                 "alerts_dropped": self.dropped_alerts, "tries": self.tries_total}
        try:
            path = os.path.join(STATE_DIR, self.name + ".stats")
            with open(path + ".tmp", "w") as f:
                json.dump(stats, f)
            os.replace(path + ".tmp", path)
        except OSError:
            pass


def flood(name, monitor_ip, port, pps, seconds):
    """Bad host test: send far too many packets to the monitor."""
    sock = udp_socket()
    pad = "x" * 900
    gap = 1.0 / pps
    end = time.time() + seconds
    next_time = time.time()
    sent = 0
    log(name, "FLOODING %s:%d with %d packets/s for %d s" % (monitor_ip, port, pps, seconds))
    while time.time() < end:
        try:
            sock.sendto(make_msg(FLOOD, name, sent, pad=pad), (monitor_ip, port))
            sent += 1
        except OSError:
            pass          # blocked, or the network is down
        next_time += gap
        pause = next_time - time.time()
        if pause > 0:
            time.sleep(pause)
        elif pause < -1:
            next_time = time.time()
    sock.close()
    log(name, "flood finished, %d packets sent" % sent)


def broadcast_addresses():
    """255.255.255.255 plus the broadcast address of every network card."""
    addrs = ["255.255.255.255"]
    try:
        import psutil
        for nics in psutil.net_if_addrs().values():
            for a in nics:
                if a.family == socket.AF_INET and a.broadcast and \
                        not a.address.startswith("127.") and a.broadcast not in addrs:
                    addrs.append(a.broadcast)
    except Exception:       # psutil missing or no permission: the first one is enough
        pass
    return addrs


def discover_monitor(name, cfg, timeout=2.0, targets=None):
    """Find the monitor on the LAN: broadcast DISCOVER on UDP port 5002, the
    monitor answers I_AM_MONITOR. Returns (ip, report_port, alert_port) or None."""
    port = cfg["real"]["discovery_port"]
    sock = udp_socket()
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        data = make_msg(DISCOVER, name, 1)
        for addr in targets or broadcast_addresses():
            try:
                sock.sendto(data, (addr, port))
            except OSError:
                pass
        end = time.time() + timeout
        while time.time() < end:
            sock.settimeout(max(end - time.time(), 0.01))
            try:
                reply, src = sock.recvfrom(2048)
            except socket.timeout:
                break
            except OSError:
                continue
            msg = read_msg(reply)
            if msg and msg["type"] == I_AM_MONITOR:
                rp, ap = msg.get("report_port"), msg.get("alert_port")
                if isinstance(rp, int) and isinstance(ap, int):
                    return src[0], rp, ap
    finally:
        sock.close()
    return None


QUIET = False


def log(name, text):
    if QUIET:
        return
    line = "[%s] [agent %s] %s" % (time.strftime("%H:%M:%S"), name, text)
    try:
        print(line, flush=True)
    except UnicodeEncodeError:      # old Windows console
        print(line.encode("ascii", "replace").decode(), flush=True)


def main():
    p = argparse.ArgumentParser(
        description="UDP health agent. Real laptop: python agent.py --monitor <monitor IP>")
    p.add_argument("--name", default=None,
                   help="name of this computer (default: its hostname; h1.. in Mininet)")
    p.add_argument("--monitor", default=None,
                   help='monitor IP, or "auto" to find it by broadcast '
                        '(default: from config.json)')
    p.add_argument("--sim", action="store_true",
                   help="Mininet mode: simulated CPU/memory instead of this computer's real ones")
    p.add_argument("--config", default=None)
    p.add_argument("--report-port", type=int, default=None)
    p.add_argument("--alert-port", type=int, default=None)
    p.add_argument("--count", type=int, default=0, help="stop after N reports (0 = forever)")
    p.add_argument("--flood", type=int, default=0, metavar="PPS",
                   help="bad host test: flood the monitor with PPS packets per second")
    p.add_argument("--seconds", type=int, default=20, help="how long to flood")
    args = p.parse_args()

    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print("ERROR: %s" % e, file=sys.stderr)
        sys.exit(2)
    if args.count < 0 or args.flood < 0 or args.seconds <= 0:
        print("ERROR: --count, --flood and --seconds can't be negative", file=sys.stderr)
        sys.exit(2)

    real = not args.sim
    name = clean_name(args.name or (socket.gethostname() if real else ""))
    if args.sim and not args.name:
        print("ERROR: --sim needs --name (h1, h2 ...)", file=sys.stderr)
        sys.exit(2)
    if real:
        try:
            import psutil  # noqa: F401
        except ImportError:
            print("ERROR: psutil is not installed. Run:\n    %s -m pip install psutil"
                  % sys.executable, file=sys.stderr)
            sys.exit(2)
        cfg = real_config(cfg)

    report_port = args.report_port or cfg["report_port"]
    alert_port = args.alert_port or cfg["alert_port"]
    monitor_ip = args.monitor or (cfg["real"]["monitor_ip"] if real else cfg["monitor_ip"])

    stopping = []
    if monitor_ip == "auto":
        log(name, "looking for the monitor on the LAN (broadcast to UDP port %d) ..."
            % cfg["real"]["discovery_port"])
        found = None
        try:
            while found is None:
                found = discover_monitor(name, cfg)
                if found is None:
                    log(name, "no monitor answered yet. Is monitor.py --lan running? "
                        "(Or start me with --monitor <IP>.) Trying again ...")
                    time.sleep(1)
        except KeyboardInterrupt:
            return
        monitor_ip = found[0]
        if not args.report_port and not args.alert_port:
            report_port, alert_port = found[1], found[2]
        log(name, "found the monitor at %s" % monitor_ip)
    else:
        try:
            socket.inet_aton(monitor_ip)
        except OSError:
            print("ERROR: --monitor must be an IP address like 192.168.1.20, not %r"
                  % monitor_ip, file=sys.stderr)
            sys.exit(2)

    if args.flood:
        flood(name, monitor_ip, report_port, args.flood, args.seconds)
        return

    try:
        agent = Agent(name, cfg, monitor_ip, report_port, alert_port, real=real)
    except RuntimeError as e:
        print("ERROR: %s" % e, file=sys.stderr)
        sys.exit(2)

    def stop(*_):
        agent.running = False
        stopping.append(1)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    try:
        os.makedirs(STATE_DIR, exist_ok=True)
    except OSError:
        pass
    agent.run(args.count)


if __name__ == "__main__":
    main()
