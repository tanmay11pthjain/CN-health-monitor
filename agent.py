#!/usr/bin/env python3
# Health agent. Runs on every server (h1 .. h4).
#
# Every second it sends a REPORT to the monitor on UDP port 5000.
# When a value crosses a limit it also sends an ALERT to UDP port 5001.
# Alerts must be answered with an ACK. If no ACK comes back, the alert
# is sent again (wait 0.5s, then 1s, then 2s ...).
#
# Usage:  python3 agent.py --name h1

import argparse
import os
import queue
import random
import signal
import socket
import threading
import time

from common import (load_config, make_msg, read_msg, check_level,
                    REPORT, ALERT, ACK)

# DSCP "EF" (expedited forwarding) = 46. The TOS byte is DSCP shifted left by 2.
# This marks our alert packets as urgent in the IP header.
TOS_URGENT = 46 << 2

STATE_DIR = "/tmp/hmon"   # topo.py writes "spike" commands here


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
                return f.read().strip() or "normal"
        except OSError:
            return "normal"

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
    def __init__(self, name, cfg, monitor_ip, report_port, alert_port):
        self.name = name
        self.cfg = cfg
        self.report_addr = (monitor_ip, report_port)
        self.alert_addr = (monitor_ip, alert_port)
        self.server = FakeServer(name)
        # reports and alerts have their own counters, so the monitor can
        # find lost reports by looking for gaps
        self.report_seq = 0
        self.alert_seq = 0
        self.boot = int(time.time() * 1000)   # tells the monitor if we restarted
        self.levels = {}                        # metric -> last level we alerted
        self.alert_queue = queue.Queue()
        self.running = True

        # socket for normal reports
        self.report_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

        # separate socket for alerts, marked as urgent
        self.alert_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            self.alert_sock.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, TOS_URGENT)
        except OSError:
            pass

        self.sent_reports = 0
        self.sent_alerts = 0
        self.failed_alerts = 0

    # ---------------- reports ----------------

    def send_report(self):
        stats = self.server.read()
        self.report_seq += 1
        data = make_msg(REPORT, self.name, self.report_seq, boot=self.boot, **stats)
        try:
            self.report_sock.sendto(data, self.report_addr)
            self.sent_reports += 1
        except OSError as e:
            # network down, cable cut ... just try again next second
            log(self.name, "could not send report: %s" % e)
        self.check_limits(stats)

    def check_limits(self, stats):
        """Send an alert when a metric changes level (OK -> WARN -> CRIT, or back to OK)."""
        for metric, limit in self.cfg["limits"].items():
            level = check_level(stats[metric], limit)
            old = self.levels.get(metric, "OK")
            if level != old:
                self.levels[metric] = level
                self.alert_queue.put({
                    "metric": metric,
                    "value": stats[metric],
                    "level": level,
                    "limit": limit["crit"] if level == "CRIT" else limit["warn"],
                })

    # ---------------- alerts ----------------

    def alert_worker(self):
        """Sends alerts one by one and waits for an ACK for each."""
        while self.running:
            try:
                alert = self.alert_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            self.send_alert(alert)

    def send_alert(self, alert):
        self.alert_seq += 1
        seq = self.alert_seq
        wait = self.cfg["ack_wait"]
        first_sent = time.time()

        for attempt in range(1, self.cfg["max_tries"] + 1):
            # keep the first send time so the monitor measures the real delay
            data = make_msg(ALERT, self.name, seq, boot=self.boot, tries=attempt,
                            first_sent=first_sent, **alert)
            try:
                self.alert_sock.sendto(data, self.alert_addr)
            except OSError as e:
                log(self.name, "could not send alert: %s" % e)

            if self.wait_for_ack(seq, wait):
                self.sent_alerts += 1
                log(self.name, "ALERT %s %s=%s acked (try %d)"
                    % (alert["level"], alert["metric"], alert["value"], attempt))
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
            except OSError:
                return False
            msg = read_msg(data)
            # ignore late ACKs for older alerts
            if msg and msg["type"] == ACK and msg["seq"] == seq:
                return True

    # ---------------- main loop ----------------

    def run(self, count=0):
        t = threading.Thread(target=self.alert_worker, daemon=True)
        t.start()
        log(self.name, "started, sending to %s:%d (alerts to port %d)"
            % (self.report_addr[0], self.report_addr[1], self.alert_addr[1]))

        every = self.cfg["report_every"]
        next_time = time.time()
        sent = 0
        while self.running:
            self.send_report()
            sent += 1
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
        t.join(timeout=5)
        self.report_sock.close()
        self.alert_sock.close()
        log(self.name, "stopped. reports=%d alerts=%d failed_alerts=%d"
            % (self.sent_reports, self.sent_alerts, self.failed_alerts))


QUIET = False


def log(name, text):
    if QUIET:
        return
    print("[%s] [agent %s] %s" % (time.strftime("%H:%M:%S"), name, text), flush=True)


def main():
    p = argparse.ArgumentParser(description="UDP health agent")
    p.add_argument("--name", required=True, help="name of this server, e.g. h1")
    p.add_argument("--config", default=None)
    p.add_argument("--monitor", default=None, help="monitor IP (default from config)")
    p.add_argument("--report-port", type=int, default=None)
    p.add_argument("--alert-port", type=int, default=None)
    p.add_argument("--count", type=int, default=0, help="stop after N reports (0 = forever)")
    args = p.parse_args()

    cfg = load_config(args.config)
    agent = Agent(args.name, cfg,
                  args.monitor or cfg["monitor_ip"],
                  args.report_port or cfg["report_port"],
                  args.alert_port or cfg["alert_port"])

    def stop(*_):
        agent.running = False
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    os.makedirs(STATE_DIR, exist_ok=True)
    agent.run(args.count)


if __name__ == "__main__":
    main()
