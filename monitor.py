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
# which nodes went quiet and writes the live table to logs/screen.txt.
#
# Usage:  python3 monitor.py

import argparse
import csv
import json
import os
import signal
import socket
import threading
import time

from common import (HERE, load_config, make_msg, read_msg, check_level, worst_level,
                    REPORT, ALERT, ACK, NODE_DOWN, NODE_UP, CTRL_REPLY, NET_EVENT)

MONITOR_NAME = "monitor"


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


class Monitor:
    def __init__(self, cfg, logdir, bind_ip="0.0.0.0", report_port=None,
                 alert_port=None, control_port=None, use_controller=True):
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

        os.makedirs(logdir, exist_ok=True)

        self.report_sock = self.open_socket(bind_ip, report_port or cfg["report_port"])
        self.alert_sock = self.open_socket(bind_ip, alert_port or cfg["alert_port"])
        self.ctrl_sock = None
        if use_controller:
            self.ctrl_sock = self.open_socket(bind_ip, control_port or cfg["control_port"])

        # log files
        self.report_file = open(os.path.join(logdir, "reports.csv"), "w", newline="")
        self.report_csv = csv.writer(self.report_file)
        self.report_csv.writerow(["time", "node", "seq", "cpu", "mem", "procs",
                                  "net_kbps", "latency_ms", "lost_so_far"])
        self.alert_file = open(os.path.join(logdir, "alerts.csv"), "w", newline="")
        self.alert_csv = csv.writer(self.alert_file)
        self.write_alert(["time", "node", "kind", "metric", "value", "level",
                                 "latency_ms", "tries"])
        self.event_file = open(os.path.join(logdir, "events.txt"), "w")

    def open_socket(self, ip, port):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # a bigger receive buffer so bursts from many agents don't get dropped
        s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
        s.bind((ip, port))
        s.settimeout(0.5)   # so threads can notice when we stop
        return s

    def event(self, text):
        line = "%s  %s" % (time.strftime("%H:%M:%S"), text)
        if not self.quiet:
            print(line, flush=True)
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
            msg = read_msg(data)
            if msg is None or msg["type"] != REPORT:
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

            node.lost += msg["seq"] - node.last_seq - 1   # gaps = lost packets
            node.last_seq = msg["seq"]
            node.last_seen = now
            node.ip = addr[0]
            node.received += 1
            node.stats = {k: msg[k] for k in ("cpu", "mem", "procs", "net_kbps")}

            # the monitor checks the limits itself too, it does not only trust the agent
            levels = [check_level(node.stats[m], lim) for m, lim in self.cfg["limits"].items()]
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
            msg = read_msg(data)
            if msg is None or msg["type"] != ALERT:
                self.bad_packets += 1
                continue

            # always ACK, even repeats: maybe our last ACK got lost
            try:
                self.alert_sock.sendto(make_msg(ACK, MONITOR_NAME, msg["seq"]), addr)
            except OSError:
                pass

            key = (msg["node"], msg.get("boot"), msg["seq"])
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
        else:
            text = "ALERT %s %s %s = %s (limit %s)  latency %.1f ms" % (
                msg["level"], msg["node"], msg["metric"], msg["value"], msg["limit"], latency_ms)
            if msg.get("tries", 1) > 1:
                text += "  [arrived on try %d]" % msg["tries"]
        self.event(text)
        self.write_alert(["%.3f" % now, msg["node"], "ALERT", msg["metric"],
                                 msg["value"], msg["level"], "%.3f" % latency_ms,
                                 msg.get("tries", 1)])

    # ---------------- port 9999: SDN controller ----------------

    def tell_controller(self, msg_type, node, ip):
        if not self.use_controller:
            return
        with self.lock:
            self.ctrl_seq += 1
            seq = self.ctrl_seq
        data = make_msg(msg_type, MONITOR_NAME, seq, target=node, target_ip=ip)
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
        self.tell_controller("HELLO", MONITOR_NAME, "")
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
            if msg is None:
                self.bad_packets += 1
                continue
            if msg["type"] == CTRL_REPLY:
                with self.lock:
                    pending = self.ctrl_pending.pop(msg.get("reply_to"), None)
                    self.ctrl_ok += 1
                    self.ctrl_mode = msg.get("mode", self.ctrl_mode)
                if pending:
                    rtt = (time.time() - pending[1]) * 1000
                    self.event("SDN   controller: %s  (%.1f ms)" % (msg.get("text", "ok"), rtt))
            elif msg["type"] == NET_EVENT:
                self.event("NET   %s" % msg.get("text", ""))
                self.write_alert(["%.3f" % time.time(), msg.get("target", ""),
                                         "NET_EVENT", "", "", msg.get("text", ""), "", ""])

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
                self.write_alert(["%.3f" % now, name, "DOWN", "silence",
                                         "%.3f" % quiet, "CRIT", "", ""])
                self.tell_controller(NODE_DOWN, name, ip)
            if self.use_controller:
                self.resend_ctrl()
            self.write_screen()
            with self.file_lock:
                self.report_file.flush()
            time.sleep(0.5)

    def write_screen(self):
        now = time.time()
        with self.lock:
            nodes = sorted(self.nodes.values(), key=lambda n: n.name)
            lines = []
            lines.append("HEALTH MONITOR   %s   controller mode: %s   running %d s" % (
                time.strftime("%H:%M:%S"), self.ctrl_mode, now - self.started))
            lines.append("")
            lines.append("%-6s %-7s %-6s %6s %6s %6s %10s %10s %6s %7s" % (
                "NODE", "STATUS", "LEVEL", "CPU%", "MEM%", "PROCS", "NET(kbps)",
                "LAST SEEN", "LOST", "ALERTS"))
            lines.append("-" * 82)
            status = {}
            for n in nodes:
                s = n.stats
                lines.append("%-6s %-7s %-6s %6s %6s %6s %10s %9.1fs %6d %7d" % (
                    n.name, n.status, n.level if n.status == "UP" else "-",
                    s.get("cpu", "-"), s.get("mem", "-"), s.get("procs", "-"),
                    s.get("net_kbps", "-"), now - n.last_seen, n.lost, n.alerts))
                status[n.name] = {"status": n.status, "level": n.level,
                                  "received": n.received, "lost": n.lost,
                                  "alerts": n.alerts, "stats": s}
            lines.append("")
            lines.append("reports: %d   alerts: %d   bad packets: %d   controller replies: %d" % (
                self.total_reports, self.total_alerts, self.bad_packets, self.ctrl_ok))
            lines.append("")
            lines.append("Recent events:")
            lines.extend("  " + e for e in self.events)
            events = list(self.events)

        write_file(os.path.join(self.logdir, "screen.txt"), "\n".join(lines) + "\n")
        write_file(os.path.join(self.logdir, "status.json"), json.dumps(
            {"time": now, "controller_mode": self.ctrl_mode, "nodes": status, "bad_packets": self.bad_packets,
             "controller_replies": self.ctrl_ok, "events": events}, indent=1))

    # ---------------- start / stop ----------------

    def run(self):
        loops = [self.report_loop, self.alert_loop, self.check_loop]
        if self.use_controller:
            loops.append(self.ctrl_loop)
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
            for sock in (self.report_sock, self.alert_sock, self.ctrl_sock):
                if sock:
                    sock.close()
            self.report_file.close()
            self.alert_file.close()
            self.event_file.close()


def write_file(path, text):
    # write to a temp file first, then rename, so readers never see half a file
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(text)
    os.replace(tmp, path)


def main():
    p = argparse.ArgumentParser(description="UDP health monitoring server")
    p.add_argument("--config", default=None)
    p.add_argument("--logdir", default=os.path.join(HERE, "logs"))
    p.add_argument("--bind", default="0.0.0.0")
    p.add_argument("--report-port", type=int, default=None)
    p.add_argument("--alert-port", type=int, default=None)
    p.add_argument("--no-controller", action="store_true",
                   help="run without the SDN controller (for testing on one PC)")
    args = p.parse_args()

    cfg = load_config(args.config)
    mon = Monitor(cfg, args.logdir, args.bind, args.report_port, args.alert_port,
                  use_controller=not args.no_controller)

    def stop(*_):
        mon.running = False
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    mon.run()


if __name__ == "__main__":
    main()
