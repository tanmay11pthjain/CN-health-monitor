#!/usr/bin/env python3
# Central monitoring server. Runs on h5.
#
#  - port 5000: normal REPORTs from the agents
#  - port 5001: ALERTs (we answer every alert with an ACK)
#  - port 9999: talks to the SDN controller (we tell it when a node goes
#               DOWN or comes back UP, it replies and tells us about
#               network events like a cable being unplugged)
#
# Each port has its own thread. A fourth thread checks every half second
# which nodes went quiet, which host sends far too many packets (flooding),
# and writes the live table to logs/screen.txt.
#
# Flooding: if one IP sends more than "flood_pps" packets per second, we ask
# the controller to BLOCK it. The controller drops its packets for
# "block_seconds" and then lets it in again.
#
# LAN mode (real laptops):  python monitor.py --lan
#   - no SDN controller; a flooding IP is blocked by the monitor itself
#   - port 5002: answers DISCOVER broadcasts so agents can find us
#   - live web dashboard on http://<this laptop's IP>:8080
#
# Usage:  python3 monitor.py            (Mininet, with the SDN controller)
#         python monitor.py --lan       (real laptops)

import argparse
import csv
import errno
import http.server
import json
from collections import deque
import os
import signal
import socket
import sys
import threading
import time

from common import (HERE, load_config, real_config, make_msg, read_msg, check_level,
                    worst_level, valid_report, valid_alert, is_number, udp_socket, local_ips,
                    ConfigError, METRICS, REPORT, ALERT, ACK, HELLO, NODE_DOWN, NODE_UP, BLOCK,
                    CTRL_REPLY, NET_EVENT, PING, DISCOVER, I_AM_MONITOR)

# extra numbers / text a real laptop sends (shown on the dashboard)
INFO_NUMBERS = ("up_kbps", "down_kbps", "disk", "swap", "temp_c", "battery", "uptime_h",
                "cores", "mem_gb", "rtt_ms")
HISTORY = 120        # points kept per node for the dashboard graphs (2 minutes)

MONITOR_NAME = "monitor"
MAX_NODES = 500      # ignore new names after this (protects memory from junk)
MAX_GAP = 1000       # a bigger jump in seq is not "lost", the counter was reset


class Node:
    def __init__(self, name, ip):
        self.name = name
        self.ip = ip
        self.status = "UP"
        self.boot = None
        self.last_seq = 0
        self.last_seen = time.time()
        self.received = 0
        self.lost = 0
        self.stats = {}
        self.level = "OK"
        self.alerts = 0
        self.info = {}
        self.history = deque(maxlen=HISTORY)


class Monitor:
    def __init__(self, cfg, logdir, bind_ip="0.0.0.0", report_port=None,
                 alert_port=None, control_port=None, use_controller=True,
                 lan=False, web_port=None, discovery_port=None):
        self.lan = lan
        if lan:
            cfg = real_config(cfg)
            use_controller = False
        self.cfg = cfg
        self.logdir = logdir
        self.use_controller = use_controller
        self.quiet = False
        self.ctrl_addr = (cfg["controller_ip"], cfg["control_port"])

        self.nodes = {}
        self.lock = threading.Lock()
        self.running = True
        self.events = []             # last few lines shown on the screen
        self.seen_alerts = set()     # (node, boot, seq) we already handled
        self.bad_packets = 0
        self.total_reports = 0
        self.total_alerts = 0
        self.ctrl_seq = 0
        self.ctrl_pending = {}       # seq -> (msg bytes, first send time, tries)
        self.ctrl_ok = 0
        self.ctrl_mode = "unknown"   # the controller tells us (simple or smart)
        self.started = time.time()
        self.file_lock = threading.Lock()

        # flood detection: packets per source IP in the current 1 second window
        self.pkt_count = {}
        self.window_start = time.time()
        self.rates = {}              # ip -> packets/s in the last window
        self.block_asked = {}        # ip -> when we asked the controller to block it
        self.blocked = {}            # ip -> name, confirmed by the controller
        self.ignored_nodes = 0

        os.makedirs(logdir, exist_ok=True)

        self.app_blocked = {}        # LAN mode: ip -> time the monitor stops ignoring it
        self.recent_alerts = deque(maxlen=50)
        self.report_sock = self.alert_sock = self.ctrl_sock = self.disc_sock = None
        self.web = None
        try:
            self.report_sock = self.open_socket(bind_ip, report_port or cfg["report_port"])
            self.alert_sock = self.open_socket(bind_ip, alert_port or cfg["alert_port"])
            if use_controller:
                self.ctrl_sock = self.open_socket(bind_ip, control_port or cfg["control_port"])
            if lan:
                self.disc_sock = self.open_socket(
                    bind_ip, discovery_port if discovery_port is not None
                    else cfg["real"]["discovery_port"])
        except OSError:
            for sock in (self.report_sock, self.alert_sock, self.ctrl_sock):
                if sock:
                    sock.close()
            raise

        # log files. We add to old files (so a monitor restart keeps the history);
        # topo.py and the experiments start with an empty logs folder.
        self.report_file, self.report_csv = self.open_csv(
            "reports.csv", ["time", "node", "seq", "cpu", "mem", "procs",
                            "net_kbps", "latency_ms", "lost_so_far"])
        self.alert_file, self.alert_csv = self.open_csv(
            "alerts.csv", ["time", "node", "kind", "metric", "value", "level",
                           "latency_ms", "tries"])
        self.event_file = open(os.path.join(logdir, "events.txt"), "a")

    def open_csv(self, name, header):
        path = os.path.join(self.logdir, name)
        new = not os.path.exists(path) or os.path.getsize(path) == 0
        f = open(path, "a", newline="")
        w = csv.writer(f)
        if new:
            w.writerow(header)
            f.flush()
        return f, w

    def open_socket(self, ip, port):
        # no SO_REUSEADDR: for UDP it would let two monitors share a port by mistake
        s = udp_socket()
        # a bigger receive buffer so bursts from many agents don't get dropped
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
        except OSError:
            pass
        try:
            s.bind((ip, port))
        except OSError as e:
            s.close()
            if e.errno == errno.EADDRINUSE:
                raise OSError(e.errno, "UDP port %d is already in use. Is another monitor "
                              "running? Close it first (Mininet: sudo mn -c)" % port)
            raise
        s.settimeout(0.5)   # so threads can notice when we stop
        return s

    def count_packet(self, ip):
        with self.lock:
            self.pkt_count[ip] = self.pkt_count.get(ip, 0) + 1

    def event(self, text):
        line = "%s  %s" % (time.strftime("%H:%M:%S"), text)
        if not self.quiet:
            try:
                print(line, flush=True)
            except UnicodeEncodeError:
                print(line.encode("ascii", "replace").decode(), flush=True)
        with self.lock:
            self.events.append(line)
            self.events = self.events[-12:]
        self.event_file.write(line + "\n")
        self.event_file.flush()

    def write_alert(self, row):
        with self.file_lock:
            self.alert_csv.writerow(row)
            self.alert_file.flush()

    # ---------------- port 5000: reports ----------------

    def report_loop(self):
        while self.running:
            try:
                data, addr = self.report_sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                if self.running:
                    time.sleep(0.1)
                continue
            self.count_packet(addr[0])
            if self.is_app_blocked(addr[0]):
                continue
            msg = read_msg(data)
            if msg is None or msg["type"] != REPORT or not valid_report(msg):
                self.bad_packets += 1
                continue
            try:
                self.handle_report(msg, addr)
            except (KeyError, TypeError, ValueError):
                self.bad_packets += 1

    def handle_report(self, msg, addr):
        now = time.time()
        latency_ms = (now - msg["time"]) * 1000
        came_back = False

        with self.lock:
            node = self.nodes.get(msg["node"])
            if node is None:
                if len(self.nodes) >= MAX_NODES:
                    self.ignored_nodes += 1
                    return
                node = Node(msg["node"], addr[0])
                self.nodes[node.name] = node
                new_node = True
            else:
                new_node = False

            if node.boot != msg.get("boot"):
                # first message, or the agent restarted: start counting again
                node.boot = msg.get("boot")
                node.last_seq = msg["seq"] - 1

            if msg["seq"] <= node.last_seq:
                return   # old or repeated packet, ignore it

            gap = msg["seq"] - node.last_seq - 1
            if gap <= MAX_GAP:
                node.lost += gap            # gaps = lost packets
            node.last_seq = msg["seq"]
            node.last_seen = now
            node.ip = addr[0]
            node.received += 1
            node.stats = {k: msg[k] for k in METRICS}
            info = {k: msg[k] for k in INFO_NUMBERS
                    if is_number(msg.get(k)) and msg[k] >= 0}
            if isinstance(msg.get("plugged"), bool):
                info["plugged"] = msg["plugged"]
            if isinstance(msg.get("os"), str):
                info["os"] = msg["os"][:30]
            top = msg.get("top")
            if isinstance(top, list):
                info["top"] = [[str(t[0])[:24], t[1]] for t in top[:3]
                               if isinstance(t, list) and len(t) == 2 and is_number(t[1])]
            node.info = info
            node.history.append([round(now, 1), msg["cpu"], msg["mem"], msg["net_kbps"]])

            # the monitor checks the limits itself too, it does not only trust the agent
            values = dict(info, **node.stats)
            levels = [check_level(values[m], lim) for m, lim in self.cfg["limits"].items()
                      if is_number(values.get(m))]
            node.level = worst_level(levels)

            if node.status != "UP":
                node.status = "UP"
                came_back = True
            self.total_reports += 1
            lost = node.lost

        with self.file_lock:
            self.report_csv.writerow(["%.3f" % now, msg["node"], msg["seq"], msg["cpu"],
                                      msg["mem"], msg["procs"], msg["net_kbps"],
                                      "%.3f" % latency_ms, lost])
        if new_node:
            self.event("NEW   %s joined from %s" % (msg["node"], addr[0]))
        if came_back:
            self.event("UP    %s is back" % msg["node"])
            self.write_alert(["%.3f" % now, msg["node"], "UP", "", "", "OK", "", ""])
            self.tell_controller(NODE_UP, msg["node"], addr[0])

    # ---------------- port 5001: alerts ----------------

    def alert_loop(self):
        while self.running:
            try:
                data, addr = self.alert_sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                if self.running:
                    time.sleep(0.1)
                continue
            self.count_packet(addr[0])
            if self.is_app_blocked(addr[0]):
                continue
            msg = read_msg(data)
            if msg is not None and msg["type"] == PING:
                # an agent measuring its round trip time: just answer
                try:
                    self.alert_sock.sendto(make_msg(ACK, MONITOR_NAME, msg["seq"]), addr)
                except OSError:
                    pass
                continue
            if msg is None or msg["type"] != ALERT or not valid_alert(msg):
                self.bad_packets += 1
                continue

            # always ACK, even repeats: maybe our last ACK got lost
            try:
                self.alert_sock.sendto(make_msg(ACK, MONITOR_NAME, msg["seq"]), addr)
            except OSError:
                pass

            key = (msg["node"], str(msg.get("boot")), msg["seq"])
            if key in self.seen_alerts:
                continue
            if len(self.seen_alerts) > 100000:
                self.seen_alerts.clear()   # don't grow forever
            self.seen_alerts.add(key)
            try:
                self.handle_alert(msg)
            except (KeyError, TypeError, ValueError):
                self.bad_packets += 1

    def handle_alert(self, msg):
        now = time.time()
        # delay from the moment the agent first noticed the problem
        latency_ms = (now - msg.get("first_sent", msg["time"])) * 1000
        with self.lock:
            node = self.nodes.get(msg["node"])
            if node:
                node.alerts += 1
            self.total_alerts += 1

        if msg["level"] == "OK":
            text = "CLEAR %s %s back to normal (%s)" % (msg["node"], msg["metric"], msg["value"])
        elif self.lan:
            # different laptops have different clocks, so a one-way delay would be
            # wrong. The real network delay is the RTT column on the dashboard.
            text = "ALERT %s %s %s = %s (limit %s)" % (
                msg["level"], msg["node"], msg["metric"], msg["value"], msg["limit"])
        else:
            text = "ALERT %s %s %s = %s (limit %s)  latency %.1f ms" % (
                msg["level"], msg["node"], msg["metric"], msg["value"], msg["limit"], latency_ms)
            if msg.get("tries", 1) > 1:
                text += "  [arrived on try %d]" % msg["tries"]
        self.event(text)
        with self.lock:
            self.recent_alerts.append({"time": now, "node": msg["node"], "metric": msg["metric"],
                                       "value": msg["value"], "level": msg["level"],
                                       "tries": msg.get("tries", 1)})
        self.write_alert(["%.3f" % now, msg["node"], "ALERT", msg["metric"],
                                 msg["value"], msg["level"], "%.3f" % latency_ms,
                                 msg.get("tries", 1)])

    # ---------------- port 9999: SDN controller ----------------

    def tell_controller(self, msg_type, node, ip, **fields):
        if not self.use_controller:
            return
        with self.lock:
            self.ctrl_seq += 1
            seq = self.ctrl_seq
        data = make_msg(msg_type, MONITOR_NAME, seq, target=node, target_ip=ip, **fields)
        with self.lock:
            self.ctrl_pending[seq] = (data, time.time(), 1, msg_type, node)
        self.send_ctrl(data)

    def send_ctrl(self, data):
        try:
            self.ctrl_sock.sendto(data, self.ctrl_addr)
        except OSError as e:
            self.event("CTRL  could not reach controller: %s" % e)

    def ctrl_loop(self):
        # say hello so the controller knows where we are
        self.tell_controller(HELLO, MONITOR_NAME, "")
        while self.running:
            try:
                data, addr = self.ctrl_sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                if self.running:
                    time.sleep(0.1)
                continue
            msg = read_msg(data)
            if msg is None or addr[0] != self.ctrl_addr[0]:
                self.bad_packets += 1
                continue
            try:
                self.handle_ctrl_msg(msg)
            except (KeyError, TypeError, ValueError):
                self.bad_packets += 1

    def handle_ctrl_msg(self, msg):
        text = str(msg.get("text", ""))[:200]
        action = msg.get("action")
        target_ip = msg.get("target_ip")
        now = time.time()
        if msg["type"] == CTRL_REPLY:
            with self.lock:
                pending = self.ctrl_pending.pop(msg.get("reply_to"), None)
                if pending is None:
                    return      # a repeated reply, we already handled it
                self.ctrl_ok += 1
                if msg.get("mode") in ("simple", "smart"):
                    self.ctrl_mode = msg["mode"]
            rtt = (now - pending[1]) * 1000
            self.event("SDN   controller: %s  (%.1f ms)" % (text or "ok", rtt))
        elif msg["type"] == NET_EVENT:
            self.event("NET   %s" % text)
        else:
            self.bad_packets += 1
            return

        # remember what the controller did about blocking
        if action == "blocked" and isinstance(target_ip, str):
            with self.lock:
                self.blocked[target_ip] = self.name_of(target_ip)
        elif action in ("unblocked", "block_refused") and isinstance(target_ip, str):
            with self.lock:
                self.blocked.pop(target_ip, None)
                if action == "unblocked":
                    self.block_asked.pop(target_ip, None)
        if action or msg["type"] == NET_EVENT:
            self.write_alert(["%.3f" % now, msg.get("target", ""), action or "NET_EVENT",
                              "", "", text, "", ""])

    def name_of(self, ip):
        # call with self.lock held
        for n in self.nodes.values():
            if n.ip == ip:
                return n.name
        return ip

    def resend_ctrl(self):
        """Controller messages are also UDP, so resend if no reply came."""
        now = time.time()
        with self.lock:
            items = list(self.ctrl_pending.items())
        for seq, (data, first, tries, msg_type, node) in items:
            if now - first < tries * 1.0:
                continue
            if tries >= 3:
                with self.lock:
                    self.ctrl_pending.pop(seq, None)
                self.event("CTRL  no reply from controller for %s %s" % (msg_type, node))
                if msg_type == HELLO:
                    self.ctrl_mode = "not reachable"
                continue
            with self.lock:
                self.ctrl_pending[seq] = (data, first, tries + 1, msg_type, node)
            self.send_ctrl(data)

    # ---------------- checker: DOWN detection + screen ----------------

    def check_loop(self):
        while self.running:
            now = time.time()
            gone = []
            with self.lock:
                for node in self.nodes.values():
                    quiet = now - node.last_seen
                    if node.status == "UP" and quiet > self.cfg["down_after"]:
                        node.status = "DOWN"
                        gone.append((node.name, node.ip, quiet))
            for name, ip, quiet in gone:
                self.event("DOWN  %s - no message for %.1f s" % (name, quiet))
                with self.lock:
                    self.recent_alerts.append({"time": now, "node": name, "metric": "silence",
                                               "value": round(quiet, 1), "level": "DOWN",
                                               "tries": 1})
                self.write_alert(["%.3f" % now, name, "DOWN", "silence",
                                  "%.3f" % quiet, "CRIT", "", ""])
                self.tell_controller(NODE_DOWN, name, ip)
            self.check_flood(now)
            if self.use_controller:
                self.resend_ctrl()
            self.write_screen()
            with self.file_lock:
                self.report_file.flush()
            time.sleep(0.5)

    def check_flood(self, now):
        """Once a second: work out packets/s per IP and report flooding hosts."""
        elapsed = now - self.window_start
        if elapsed < 1.0:
            return
        with self.lock:
            counts, self.pkt_count = self.pkt_count, {}
            self.window_start = now
            self.rates = {ip: c / elapsed for ip, c in counts.items()}
            flooders = []
            for ip, rate in self.rates.items():
                asked = self.block_asked.get(ip)
                # ask once; ask again only if it is still flooding long after
                if rate > self.cfg["flood_pps"] and (
                        asked is None or now - asked > self.cfg["block_seconds"] + 5):
                    self.block_asked[ip] = now
                    flooders.append((ip, self.name_of(ip), rate))
        for ip, name, rate in flooders:
            self.write_alert(["%.3f" % now, name, "FLOOD", "pps", "%.0f" % rate,
                              "CRIT", "", ""])
            if self.use_controller:
                self.event("FLOOD %s (%s) sends %.0f packets/s (limit %s), asking to block it"
                           % (name, ip, rate, self.cfg["flood_pps"]))
                self.tell_controller(BLOCK, name, ip, seconds=self.cfg["block_seconds"])
            else:
                # no SDN controller on a real LAN: the monitor ignores that IP itself
                self.event("FLOOD %s (%s) sends %.0f packets/s (limit %s), ignoring it for %s s"
                           % (name, ip, rate, self.cfg["flood_pps"], self.cfg["block_seconds"]))
                with self.lock:
                    self.app_blocked[ip] = now + self.cfg["block_seconds"]
                    self.blocked[ip] = name

    def is_app_blocked(self, ip):
        until = self.app_blocked.get(ip)
        if until is None:
            return False
        if time.time() < until:
            return True
        with self.lock:
            self.app_blocked.pop(ip, None)
            self.blocked.pop(ip, None)
            self.block_asked.pop(ip, None)
        self.event("UNBLOCK %s - the monitor listens to it again" % ip)
        return False

    # ---------------- port 5002: discovery (LAN mode) ----------------

    def discovery_loop(self):
        while self.running:
            try:
                data, addr = self.disc_sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                if self.running:
                    time.sleep(0.1)
                continue
            msg = read_msg(data)
            if msg is None or msg["type"] != DISCOVER:
                self.bad_packets += 1
                continue
            try:
                self.disc_sock.sendto(make_msg(
                    I_AM_MONITOR, MONITOR_NAME, msg["seq"],
                    report_port=self.report_sock.getsockname()[1],
                    alert_port=self.alert_sock.getsockname()[1]), addr)
            except OSError:
                pass

    # ---------------- web dashboard (LAN mode) ----------------

    def api_status(self):
        now = time.time()
        with self.lock:
            nodes = []
            for n in sorted(self.nodes.values(), key=lambda n: n.name):
                total = n.received + n.lost
                nodes.append({"name": n.name, "ip": n.ip, "status": n.status,
                              "level": n.level, "stats": n.stats, "info": n.info,
                              "last_seen": round(now - n.last_seen, 1),
                              "received": n.received, "lost": n.lost,
                              "loss_pct": round(100.0 * n.lost / total, 2) if total else 0,
                              "alerts": n.alerts, "history": list(n.history)})
            return {"time": now, "uptime": round(now - self.started), "nodes": nodes,
                    "total_reports": self.total_reports, "total_alerts": self.total_alerts,
                    "bad_packets": self.bad_packets, "blocked": dict(self.blocked),
                    "limits": self.cfg["limits"], "down_after": self.cfg["down_after"],
                    "events": list(self.events), "alerts": list(self.recent_alerts)[::-1]}

    def start_web(self, port, bind_ip="0.0.0.0"):
        mon = self
        page_path = os.path.join(HERE, "dashboard.html")

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                try:
                    if self.path.split("?")[0] == "/api/status":
                        body, ctype = json.dumps(mon.api_status()).encode(), "application/json"
                    elif self.path in ("/", "/index.html"):
                        with open(page_path, "rb") as f:
                            body, ctype = f.read(), "text/html; charset=utf-8"
                    else:
                        self.send_error(404)
                        return
                    self.send_response(200)
                    self.send_header("Content-Type", ctype)
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except (OSError, ValueError):
                    pass      # browser closed the tab, etc.

            def log_message(self, *args):
                pass          # keep the console clean

        http.server.ThreadingHTTPServer.daemon_threads = True
        self.web = http.server.ThreadingHTTPServer((bind_ip, port), Handler)
        threading.Thread(target=self.web.serve_forever, daemon=True).start()
        return self.web.server_address[1]

    def write_screen(self):
        now = time.time()
        with self.lock:
            nodes = sorted(self.nodes.values(), key=lambda n: n.name)
            lines = []
            lines.append("HEALTH MONITOR   %s   controller mode: %s   running %d s" % (
                time.strftime("%H:%M:%S"), self.ctrl_mode, now - self.started))
            lines.append("")
            lines.append("%-16s %-7s %-6s %6s %6s %6s %10s %10s %6s %7s" % (
                "NODE", "STATUS", "LEVEL", "CPU%", "MEM%", "PROCS", "NET(kbps)",
                "LAST SEEN", "LOST", "ALERTS"))
            lines.append("-" * 92)
            status = {}
            for n in nodes:
                s = n.stats
                lines.append("%-16s %-7s %-6s %6s %6s %6s %10s %9.1fs %6d %7d" % (
                    n.name, n.status, n.level if n.status == "UP" else "-",
                    s.get("cpu", "-"), s.get("mem", "-"), s.get("procs", "-"),
                    s.get("net_kbps", "-"), now - n.last_seen, n.lost, n.alerts))
                status[n.name] = {"status": n.status, "level": n.level,
                                  "received": n.received, "lost": n.lost,
                                  "alerts": n.alerts, "stats": s}
            lines.append("")
            lines.append("reports: %d   alerts: %d   bad packets: %d   controller replies: %d" % (
                self.total_reports, self.total_alerts, self.bad_packets, self.ctrl_ok))
            if self.blocked:
                lines.append("blocked by the controller: %s" % ", ".join(
                    "%s (%s)" % (n, ip) for ip, n in sorted(self.blocked.items())))
            lines.append("")
            lines.append("Recent events:")
            lines.extend("  " + e for e in self.events)
            events = list(self.events)
            blocked = dict(self.blocked)
            rates = {ip: round(r, 1) for ip, r in self.rates.items()}

        write_file(os.path.join(self.logdir, "screen.txt"), "\n".join(lines) + "\n")
        write_file(os.path.join(self.logdir, "status.json"), json.dumps(
            {"time": now, "controller_mode": self.ctrl_mode, "nodes": status, "bad_packets": self.bad_packets,
             "controller_replies": self.ctrl_ok, "blocked": blocked, "rates": rates,
             "total_reports": self.total_reports, "total_alerts": self.total_alerts,
             "events": events}, indent=1))

    # ---------------- start / stop ----------------

    def run(self):
        loops = [self.report_loop, self.alert_loop, self.check_loop]
        if self.use_controller:
            loops.append(self.ctrl_loop)
        if self.disc_sock:
            loops.append(self.discovery_loop)
        threads = [threading.Thread(target=f, daemon=True) for f in loops]
        for t in threads:
            t.start()
        self.event("monitor started (reports on %d, alerts on %d)" % (
            self.report_sock.getsockname()[1], self.alert_sock.getsockname()[1]))
        try:
            while self.running:
                time.sleep(0.2)
        finally:
            self.running = False
            for t in threads:
                t.join(timeout=2)
            if self.web:
                self.web.shutdown()
                self.web.server_close()
            for sock in (self.report_sock, self.alert_sock, self.ctrl_sock, self.disc_sock):
                if sock:
                    sock.close()
            self.report_file.close()
            self.alert_file.close()
            self.event_file.close()


def write_file(path, text):
    # write to a temp file first, then rename, so readers never see half a file
    tmp = path + ".tmp"
    try:
        with open(tmp, "w") as f:
            f.write(text)
        os.replace(tmp, path)
    except OSError:
        pass      # Windows: the file is open in another program. Try again next time.


def main():
    p = argparse.ArgumentParser(description="UDP health monitoring server")
    p.add_argument("--config", default=None)
    p.add_argument("--logdir", default=os.path.join(HERE, "logs"))
    p.add_argument("--bind", default="0.0.0.0")
    p.add_argument("--report-port", type=int, default=None)
    p.add_argument("--alert-port", type=int, default=None)
    p.add_argument("--no-controller", action="store_true",
                   help="run without the SDN controller (for testing on one PC)")
    p.add_argument("--lan", action="store_true",
                   help="real laptops on a LAN / Wi-Fi: no controller, discovery + web dashboard")
    p.add_argument("--web-port", type=int, default=None, help="dashboard port (default 8080)")
    p.add_argument("--table", action="store_true",
                   help="also show a live table in this terminal (redrawn every second)")
    args = p.parse_args()

    try:
        cfg = load_config(args.config)
        mon = Monitor(cfg, args.logdir, args.bind, args.report_port, args.alert_port,
                      use_controller=not (args.no_controller or args.lan), lan=args.lan)
    except ConfigError as e:
        print("ERROR: %s" % e, file=sys.stderr)
        sys.exit(2)
    except OSError as e:
        print("ERROR: could not start the monitor: %s" % e, file=sys.stderr)
        sys.exit(1)

    def stop(*_):
        mon.running = False
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    if args.lan:
        web_port = args.web_port or cfg["real"]["web_port"]
        try:
            mon.start_web(web_port, args.bind)
        except OSError as e:
            print("WARNING: dashboard not started, TCP port %d: %s (use --web-port 8081)"
                  % (web_port, e))
        ips = local_ips() or ["<this laptop's IP>"]
        print("=" * 64)
        print(" HEALTH MONITOR (LAN mode)")
        print(" This laptop's IP address(es): %s" % ", ".join(ips))
        print(" On each client laptop run:   python agent.py --monitor %s" % ips[0])
        if mon.web:
            print(" Live dashboard:              http://%s:%d" % (ips[0], web_port))
            print("                              (or http://localhost:%d here)" % web_port)
        print(" Press Ctrl+C to stop.")
        print("=" * 64, flush=True)
    if args.table:
        threading.Thread(target=show_table, args=(mon,), daemon=True).start()
    mon.run()


def show_table(mon):
    """Redraw logs/screen.txt in the terminal every second (works in Windows Terminal too)."""
    if os.name == "nt":
        os.system("")          # turns on ANSI colours/cursor codes in the Windows console
    mon.quiet = True
    path = os.path.join(mon.logdir, "screen.txt")
    while mon.running:
        time.sleep(1)
        try:
            with open(path) as f:
                text = f.read()
            print("\033[2J\033[H" + text, end="", flush=True)
        except (OSError, UnicodeEncodeError):
            pass


if __name__ == "__main__":
    main()
