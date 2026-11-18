#!/usr/bin/env python3
# Quick tests that run on one computer, no Mininet needed.
#
#   python3 test_basic.py
#
# Phase 1: message format, alert limits, and a real monitor + agents on
# 127.0.0.1 (reports, alerts, ACKs, resends, DOWN detection).
# Phase 2: bad config files, broken and strange packets, flood detection
# and blocking (with a fake controller), monitor restart, the flood and
# noise senders, and the experiment / graph helpers.

import json
import os
import shutil
import socket
import tempfile
import threading
import time
import unittest

import agent
import common
from common import (load_config, make_msg, read_msg, check_level, worst_level, ConfigError,
                    valid_report, valid_alert, REPORT, ALERT, ACK, BLOCK, CTRL_REPLY, NET_EVENT)
from monitor import Monitor

CFG = load_config()
agent.QUIET = True   # keep the test output clean


def free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class TestMessages(unittest.TestCase):

    def test_make_and_read(self):
        data = make_msg(REPORT, "h1", 7, cpu=10.5, mem=20.0, procs=100, net_kbps=1.0)
        msg = read_msg(data)
        self.assertEqual(msg["type"], REPORT)
        self.assertEqual(msg["node"], "h1")
        self.assertEqual(msg["seq"], 7)
        self.assertEqual(msg["cpu"], 10.5)

    def test_broken_packets_are_rejected(self):
        self.assertIsNone(read_msg(b"hello"))
        self.assertIsNone(read_msg(b"\xff\xfe\x00"))
        self.assertIsNone(read_msg(b"[1, 2, 3]"))
        self.assertIsNone(read_msg(json.dumps({"type": "REPORT"}).encode()))   # fields missing
        self.assertIsNone(read_msg(json.dumps(
            {"type": "REPORT", "node": "h1", "seq": "x", "time": 1}).encode()))  # bad seq
        self.assertIsNone(read_msg(b"x" * 5000))                                # too big

    def test_message_size_limit(self):
        with self.assertRaises(ValueError):
            make_msg(REPORT, "h1", 1, junk="x" * 2000)


class TestLimits(unittest.TestCase):

    def test_levels(self):
        lim = {"warn": 75, "crit": 90}
        self.assertEqual(check_level(10, lim), "OK")
        self.assertEqual(check_level(75, lim), "WARN")
        self.assertEqual(check_level(89.9, lim), "WARN")
        self.assertEqual(check_level(90, lim), "CRIT")

    def test_worst(self):
        self.assertEqual(worst_level(["OK", "WARN", "OK"]), "WARN")
        self.assertEqual(worst_level(["OK", "CRIT", "WARN"]), "CRIT")
        self.assertEqual(worst_level(["OK"]), "OK")

    def test_fake_server_spike(self):
        agent.STATE_DIR = tempfile.mkdtemp()
        srv = agent.FakeServer("hx")
        for _ in range(3):
            self.assertLess(srv.read()["cpu"], 75)
        with open(os.path.join(agent.STATE_DIR, "hx.state"), "w") as f:
            f.write("cpu")
        for _ in range(6):
            stats = srv.read()
        self.assertGreaterEqual(stats["cpu"], 90)
        shutil.rmtree(agent.STATE_DIR)


class TestAlertResend(unittest.TestCase):
    """A fake monitor that ignores the first try: the agent must send again."""

    def test_resend_until_ack(self):
        port = free_port()
        server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        server.bind(("127.0.0.1", port))
        server.settimeout(5)
        tries_seen = []

        def fake_monitor():
            while len(tries_seen) < 2:
                data, addr = server.recvfrom(2048)
                msg = read_msg(data)
                tries_seen.append(msg["tries"])
                if msg["tries"] >= 2:                     # ignore try 1, answer try 2
                    server.sendto(make_msg(ACK, "monitor", msg["seq"]), addr)

        t = threading.Thread(target=fake_monitor, daemon=True)
        t.start()
        a = agent.Agent("hx", CFG, "127.0.0.1", free_port(), port)
        ok = a.send_alert({"metric": "cpu", "value": 95, "level": "CRIT", "limit": 90})
        t.join(3)
        server.close()
        a.report_sock.close()
        a.alert_sock.close()
        self.assertTrue(ok)
        self.assertEqual(tries_seen, [1, 2])

    def test_gives_up_without_monitor(self):
        cfg = dict(CFG, ack_wait=0.05, max_tries=3)
        a = agent.Agent("hx", cfg, "127.0.0.1", free_port(), free_port())
        ok = a.send_alert({"metric": "cpu", "value": 95, "level": "CRIT", "limit": 90})
        a.report_sock.close()
        a.alert_sock.close()
        self.assertFalse(ok)
        self.assertEqual(a.failed_alerts, 1)


class TestEndToEnd(unittest.TestCase):
    """Real monitor and real agents talking over UDP on 127.0.0.1."""

    def setUp(self):
        self.logdir = tempfile.mkdtemp()
        agent.STATE_DIR = tempfile.mkdtemp()
        self.rport, self.aport = free_port(), free_port()
        cfg = dict(CFG, report_every=0.2, down_after=1.0)
        self.cfg = cfg
        self.mon = Monitor(cfg, self.logdir, "127.0.0.1", self.rport, self.aport,
                           use_controller=False)
        self.mon.quiet = True
        self.mon_thread = threading.Thread(target=self.mon.run, daemon=True)
        self.mon_thread.start()

    def tearDown(self):
        self.mon.running = False
        self.mon_thread.join(3)
        shutil.rmtree(self.logdir)
        shutil.rmtree(agent.STATE_DIR)

    def start_agent(self, name, count=0):
        a = agent.Agent(name, self.cfg, "127.0.0.1", self.rport, self.aport)
        t = threading.Thread(target=a.run, args=(count,), daemon=True)
        t.start()
        return a, t

    def wait(self, check, seconds=5):
        end = time.time() + seconds
        while time.time() < end:
            if check():
                return True
            time.sleep(0.1)
        return False

    def test_reports_alerts_and_down(self):
        agents = [self.start_agent("h%d" % i) for i in range(1, 4)]

        # all three show up
        self.assertTrue(self.wait(lambda: len(self.mon.nodes) == 3))

        # make h2 busy -> CRIT alert arrives and is ACKed
        with open(os.path.join(agent.STATE_DIR, "h2.state"), "w") as f:
            f.write("cpu")
        self.assertTrue(self.wait(lambda: any("ALERT CRIT h2 cpu" in e for e in self.mon.events)))
        self.assertTrue(self.wait(lambda: agents[1][0].sent_alerts >= 1))

        # stop h3 -> DOWN
        agents[2][0].running = False
        self.assertTrue(self.wait(lambda: self.mon.nodes["h3"].status == "DOWN"))

        # restart h3 -> UP again (new agent, sequence numbers start again)
        agents.append(self.start_agent("h3"))
        self.assertTrue(self.wait(lambda: self.mon.nodes["h3"].status == "UP"))

        for a, _ in agents:
            a.running = False
        # nothing was lost on localhost
        self.assertEqual(sum(n.lost for n in self.mon.nodes.values()), 0)

    def test_lost_and_repeated_packets(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        send = lambda seq: s.sendto(make_msg(REPORT, "hz", seq, boot=1, cpu=1, mem=1,
                                             procs=1, net_kbps=1), ("127.0.0.1", self.rport))
        for seq in (1, 2, 5, 5, 3, 6):     # 3 and 4 missing, 5 twice, 3 arrives late
            send(seq)
            time.sleep(0.02)
        self.assertTrue(self.wait(lambda: "hz" in self.mon.nodes and
                                  self.mon.nodes["hz"].last_seq == 6))
        node = self.mon.nodes["hz"]
        self.assertEqual(node.received, 4)   # 1, 2, 5, 6
        self.assertEqual(node.lost, 2)       # 3 and 4
        s.close()

    def test_garbage_is_counted_not_crashing(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        for junk in (b"hello", b"{}", b"\x00\x01", make_msg(ALERT, "hz", 1)):
            s.sendto(junk, ("127.0.0.1", self.rport))
        s.close()
        self.assertTrue(self.wait(lambda: self.mon.bad_packets >= 4))
        # monitor still works after the junk
        self.start_agent("h9", count=2)
        self.assertTrue(self.wait(lambda: "h9" in self.mon.nodes))


# ======================================================================
# Phase 2 tests
# ======================================================================

def write_cfg(text):
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w") as f:
        f.write(text)
    return path


def wait(check, seconds=5):
    end = time.time() + seconds
    while time.time() < end:
        if check():
            return True
        time.sleep(0.05)
    return False


class TestConfig(unittest.TestCase):

    def bad(self, change, word):
        cfg = json.loads(json.dumps(CFG))
        change(cfg)
        path = write_cfg(json.dumps(cfg))
        try:
            with self.assertRaises(ConfigError) as cm:
                load_config(path)
            self.assertIn(word, str(cm.exception))
        finally:
            os.remove(path)

    def test_real_config_is_ok(self):
        cfg = load_config()
        self.assertEqual(cfg["mode"] in ("simple", "smart"), True)

    def test_missing_and_broken_file(self):
        with self.assertRaises(ConfigError):
            load_config("/no/such/file.json")
        path = write_cfg("{ this is not json")
        try:
            with self.assertRaises(ConfigError):
                load_config(path)
        finally:
            os.remove(path)
        path = write_cfg("[1, 2]")
        try:
            with self.assertRaises(ConfigError):
                load_config(path)
        finally:
            os.remove(path)

    def test_old_config_gets_defaults(self):
        # a Phase 1 config without the new keys still works
        cfg = json.loads(json.dumps(CFG))
        for key in ("flood_pps", "block_seconds", "watch_every", "slow_above_mbps"):
            cfg.pop(key, None)
        path = write_cfg(json.dumps(cfg))
        try:
            loaded = load_config(path)
        finally:
            os.remove(path)
        self.assertEqual(loaded["flood_pps"], common.DEFAULTS["flood_pps"])

    def test_bad_values(self):
        self.bad(lambda c: c.update(report_port=70000), "report_port")
        self.bad(lambda c: c.update(alert_port="5001"), "alert_port")
        self.bad(lambda c: c.update(alert_port=c["report_port"]), "different")
        self.bad(lambda c: c.update(monitor_ip="10.0.0"), "monitor_ip")
        self.bad(lambda c: c.update(down_after=0.5), "down_after")
        self.bad(lambda c: c.update(ack_wait=-1), "ack_wait")
        self.bad(lambda c: c.update(max_tries=0), "max_tries")
        self.bad(lambda c: c.update(max_tries=True), "max_tries")
        self.bad(lambda c: c.update(mode="fast"), "mode")
        self.bad(lambda c: c["limits"].update(cpu={"warn": 95, "crit": 90}), "warn")
        self.bad(lambda c: c["limits"].pop("mem"), "limits.mem")
        self.bad(lambda c: c["limits"].update(gpu={"warn": 1, "crit": 2}), "unknown")
        self.bad(lambda c: c.update(calm_below_mbps=10), "calm_below_mbps")


class TestStrangePackets(unittest.TestCase):

    def test_read_msg_edge_cases(self):
        good = {"type": "REPORT", "node": "h1", "seq": 1, "time": 1.0}

        def enc(**change):
            m = dict(good)
            m.update(change)
            return json.dumps(m).encode()
        self.assertIsNotNone(read_msg(enc()))
        self.assertIsNone(read_msg(b""))
        self.assertIsNone(read_msg(enc(seq=True)))              # bool is not a number here
        self.assertIsNone(read_msg(enc(seq=-1)))
        self.assertIsNone(read_msg(enc(seq=1.5)))
        self.assertIsNone(read_msg(enc(time="now")))
        self.assertIsNone(read_msg(b'{"type":"REPORT","node":"h1","seq":1,"time":NaN}'))
        self.assertIsNone(read_msg(b'{"type":"REPORT","node":"h1","seq":1,"time":Infinity}'))
        self.assertIsNone(read_msg(enc(node="")))
        self.assertIsNone(read_msg(enc(node="x" * 100)))
        self.assertIsNone(read_msg(enc(node=5)))
        self.assertIsNone(read_msg(enc(type=None)))
        self.assertIsNone(read_msg(b"[" * 1000 + b"]" * 1000 + b"x"))   # deep nesting

    def test_valid_report_and_alert(self):
        r = {"cpu": 1, "mem": 2.5, "procs": 3, "net_kbps": 0}
        self.assertTrue(valid_report(r))
        self.assertFalse(valid_report(dict(r, cpu="high")))
        self.assertFalse(valid_report(dict(r, mem=-5)))
        self.assertFalse(valid_report({"cpu": 1}))
        a = {"time": 1.0, "metric": "cpu", "level": "CRIT", "value": 95, "limit": 90}
        self.assertTrue(valid_alert(a))
        self.assertFalse(valid_alert(dict(a, metric="gpu")))
        self.assertFalse(valid_alert(dict(a, level="PANIC")))
        self.assertFalse(valid_alert(dict(a, value=None)))
        self.assertFalse(valid_alert(dict(a, tries="2")))


class MonitorCase(unittest.TestCase):
    """Starts a real monitor on 127.0.0.1, with a fake SDN controller."""

    use_controller = False

    def setUp(self):
        self.logdir = tempfile.mkdtemp()
        agent.STATE_DIR = tempfile.mkdtemp()
        self.rport, self.aport, self.cport = free_port(), free_port(), free_port()
        self.ctrl_port = free_port()
        self.cfg = dict(CFG, report_every=0.2, down_after=1.0, flood_pps=50, block_seconds=30,
                        controller_ip="127.0.0.1", control_port=self.ctrl_port)
        self.ctrl = None
        if self.use_controller:
            self.ctrl = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.ctrl.bind(("127.0.0.1", self.ctrl_port))
            self.ctrl.settimeout(0.2)
            self.ctrl_got = []
            self.ctrl_running = True
            self.ctrl_thread = threading.Thread(target=self.fake_controller, daemon=True)
            self.ctrl_thread.start()
        self.start_monitor()

    def start_monitor(self):
        self.mon = Monitor(self.cfg, self.logdir, "127.0.0.1", self.rport, self.aport,
                           control_port=self.cport, use_controller=self.use_controller)
        self.mon.quiet = True
        self.mon_thread = threading.Thread(target=self.mon.run, daemon=True)
        self.mon_thread.start()

    def stop_monitor(self):
        self.mon.running = False
        self.mon_thread.join(5)

    def fake_controller(self):
        """Answers like controller.py in smart mode."""
        while self.ctrl_running:
            try:
                data, addr = self.ctrl.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                return
            msg = read_msg(data)
            if msg is None:
                continue
            self.ctrl_got.append(msg)
            extra = {}
            if msg["type"] == BLOCK:
                extra = {"action": "blocked"}
            self.ctrl.sendto(make_msg(CTRL_REPLY, "controller", 0, text="ok " + msg["type"],
                                      reply_to=msg["seq"], mode="smart",
                                      target=msg.get("target"), target_ip=msg.get("target_ip"),
                                      **extra), addr)

    def tearDown(self):
        self.stop_monitor()
        if self.ctrl:
            self.ctrl_running = False
            self.ctrl_thread.join(2)
            self.ctrl.close()
        shutil.rmtree(self.logdir)
        shutil.rmtree(agent.STATE_DIR)

    def send(self, data, port=None):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.sendto(data, ("127.0.0.1", port or self.rport))
        s.close()


class TestMonitorRobust(MonitorCase):

    def test_bad_alert_is_not_acked_good_one_once(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind(("127.0.0.1", 0))
        s.settimeout(1)
        bad = make_msg(ALERT, "hx", 1, boot=1, metric="gpu", level="CRIT", value=1, limit=1)
        s.sendto(bad, ("127.0.0.1", self.aport))
        with self.assertRaises(socket.timeout):
            s.recvfrom(2048)                       # no ACK for a broken alert
        good = make_msg(ALERT, "hx", 2, boot=1, metric="cpu", level="CRIT", value=95, limit=90)
        for _ in range(2):                          # same alert twice (a resend)
            s.sendto(good, ("127.0.0.1", self.aport))
            self.assertEqual(read_msg(s.recvfrom(2048)[0])["type"], ACK)
        s.close()
        self.assertTrue(wait(lambda: self.mon.total_alerts == 1))
        time.sleep(0.2)
        self.assertEqual(self.mon.total_alerts, 1)  # counted once
        self.assertGreaterEqual(self.mon.bad_packets, 1)

    def test_bad_numbers_in_report(self):
        self.send(make_msg(REPORT, "hy", 1, boot=1, cpu="lots", mem=1, procs=1, net_kbps=1))
        self.send(make_msg(REPORT, "hy", 2, boot=1, cpu=1, mem=1, procs=1))   # net missing
        self.assertTrue(wait(lambda: self.mon.bad_packets >= 2))
        self.assertNotIn("hy", self.mon.nodes)

    def test_huge_seq_jump_is_not_counted_as_lost(self):
        for seq in (1, 2, 10 ** 9):
            self.send(make_msg(REPORT, "hz", seq, boot=1, cpu=1, mem=1, procs=1, net_kbps=1))
            time.sleep(0.02)
        self.assertTrue(wait(lambda: "hz" in self.mon.nodes
                             and self.mon.nodes["hz"].last_seq == 10 ** 9))
        self.assertEqual(self.mon.nodes["hz"].lost, 0)

    def test_monitor_restart_agents_keep_going(self):
        a = agent.Agent("h1", self.cfg, "127.0.0.1", self.rport, self.aport)
        t = threading.Thread(target=a.run, daemon=True)
        t.start()
        self.assertTrue(wait(lambda: "h1" in self.mon.nodes))
        self.stop_monitor()
        time.sleep(0.5)                            # agent sends into the void meanwhile
        self.start_monitor()
        self.assertTrue(wait(lambda: "h1" in self.mon.nodes
                             and self.mon.nodes["h1"].status == "UP"))
        a.running = False
        t.join(5)
        # the log files were added to, not overwritten: only one header line
        with open(os.path.join(self.logdir, "reports.csv")) as f:
            self.assertEqual(sum(1 for line in f if line.startswith("time,")), 1)

    def test_port_in_use_gives_clear_error(self):
        with self.assertRaises(OSError) as cm:
            Monitor(self.cfg, self.logdir, "127.0.0.1", self.rport, free_port(),
                    use_controller=False)
        self.assertIn("already in use", str(cm.exception))


class TestFloodBlocking(MonitorCase):

    use_controller = True

    def test_flood_is_noticed_and_blocked(self):
        self.assertTrue(wait(lambda: self.mon.ctrl_mode == "smart"))   # HELLO answered
        # one well behaved node
        for seq in (1, 2):
            self.send(make_msg(REPORT, "h1", seq, boot=1, cpu=1, mem=1, procs=1, net_kbps=1))
        # now a flood: 300 junk packets as fast as we can
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        for i in range(300):
            s.sendto(make_msg("FLOOD", "h1", i, pad="x" * 100), ("127.0.0.1", self.rport))
        s.close()
        self.assertTrue(wait(lambda: any(m["type"] == BLOCK for m in self.ctrl_got), 4))
        block = [m for m in self.ctrl_got if m["type"] == BLOCK][0]
        self.assertEqual(block["target_ip"], "127.0.0.1")
        self.assertEqual(block["target"], "h1")
        self.assertEqual(block["seconds"], 30)
        self.assertTrue(wait(lambda: "127.0.0.1" in self.mon.blocked))
        self.assertTrue(wait(lambda: any("FLOOD h1" in e for e in self.mon.events)))
        # asked only once, even if it keeps flooding a bit
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        for i in range(200):
            s.sendto(b"junk", ("127.0.0.1", self.rport))
        s.close()
        time.sleep(1.5)
        self.assertEqual(sum(1 for m in self.ctrl_got if m["type"] == BLOCK), 1)

    def test_unblock_event_clears_the_block(self):
        self.assertTrue(wait(lambda: self.mon.ctrl_mode == "smart"))
        self.mon.blocked["10.0.0.3"] = "h3"
        self.mon.block_asked["10.0.0.3"] = time.time()
        self.ctrl.sendto(make_msg(NET_EVENT, "controller", 0, text="10.0.0.3 is unblocked",
                                  action="unblocked", target_ip="10.0.0.3"),
                         ("127.0.0.1", self.cport))
        self.assertTrue(wait(lambda: "10.0.0.3" not in self.mon.blocked))
        self.assertNotIn("10.0.0.3", self.mon.block_asked)

    def test_normal_agents_are_not_flooders(self):
        agents = []
        for i in range(1, 4):
            a = agent.Agent("h%d" % i, self.cfg, "127.0.0.1", self.rport, self.aport)
            t = threading.Thread(target=a.run, daemon=True)
            t.start()
            agents.append((a, t))
        time.sleep(2.5)
        for a, t in agents:
            a.running = False
        for a, t in agents:
            t.join(5)
        self.assertFalse(any(m["type"] == BLOCK for m in self.ctrl_got))


class TestAgentPhase2(unittest.TestCase):

    def test_bad_state_file_means_normal(self):
        agent.STATE_DIR = tempfile.mkdtemp()
        with open(os.path.join(agent.STATE_DIR, "hq.state"), "w") as f:
            f.write("explode\n")
        self.assertEqual(agent.FakeServer("hq").mode(), "normal")
        with open(os.path.join(agent.STATE_DIR, "hq.state"), "wb") as f:
            f.write(b"\xff\xfe")
        self.assertEqual(agent.FakeServer("hq").mode(), "normal")
        shutil.rmtree(agent.STATE_DIR)

    def test_alert_queue_does_not_grow_forever(self):
        a = agent.Agent("hq", CFG, "127.0.0.1", free_port(), free_port())
        stats = {"cpu": 10, "mem": 10, "procs": 10, "net_kbps": 0}
        for i in range(agent.MAX_WAITING_ALERTS + 30):
            stats["cpu"] = 95 if i % 2 == 0 else 10     # flapping: a new alert every time
            a.check_limits(stats)
        self.assertEqual(a.alert_queue.qsize(), agent.MAX_WAITING_ALERTS)
        self.assertEqual(a.dropped_alerts, 30)
        a.report_sock.close()
        a.alert_sock.close()

    def test_stats_file(self):
        agent.STATE_DIR = tempfile.mkdtemp()
        a = agent.Agent("hq", dict(CFG, report_every=0.05), "127.0.0.1", free_port(),
                        free_port())
        a.run(count=3)
        with open(os.path.join(agent.STATE_DIR, "hq.stats")) as f:
            st = json.load(f)
        self.assertEqual(st["reports"], 3)
        shutil.rmtree(agent.STATE_DIR)

    def test_flood_sender(self):
        port = free_port()
        server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        server.bind(("127.0.0.1", port))
        server.settimeout(0.5)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
        agent.flood("hq", "127.0.0.1", port, 200, 1)
        got = 0
        try:
            while True:
                data, _ = server.recvfrom(2048)
                got += 1
                self.assertLessEqual(len(data), common.MAX_SIZE)
        except socket.timeout:
            pass
        server.close()
        self.assertGreater(got, 150)
        self.assertLessEqual(got, 210)


class TestHelpers(unittest.TestCase):

    def test_agent_names(self):
        import topo
        self.assertEqual(topo.agent_names(4), ["h1", "h2", "h3", "h4"])
        self.assertEqual(topo.agent_names(2), ["h1", "h2"])
        names = topo.agent_names(10)
        self.assertEqual(names, ["h1", "h2", "h3", "h4", "h7", "h8", "h9", "h10", "h11", "h12"])
        self.assertNotIn("h5", topo.agent_names(50))
        self.assertNotIn("h6", topo.agent_names(50))
        self.assertEqual(len(topo.agent_names(50)), 50)
        with self.assertRaises(ValueError):
            topo.agent_names(0)

    def test_summary(self):
        import experiments
        s = experiments.summary([3, 1, 2, None])
        self.assertEqual((s["n"], s["mean"], s["median"], s["max"]), (3, 2, 2, 3))
        self.assertEqual(experiments.summary([])["mean"], None)

    def test_plot_table_with_missing_data(self):
        import plot
        sample = {"info": {"date": "today"},
                  "smart": {"jam": [{"jam": True, "alerts_made": 10, "alerts_arrived": 10,
                                     "alert_latency_ms": {"mean": 1.0, "p95": 2.0, "max": 3.0},
                                     "slowed_events": 1}],
                            "flood": [{"time_to_notice_s": 1.0, "time_to_block_s": None,
                                       "flood_packets_at_monitor": 5,
                                       "flood_packets_sent": 4500}]}}
        text = plot.table(sample)
        self.assertIn("| smart | heavy | 10 of 10 |", text)
        self.assertIn("never", text)
        self.assertIn("Results", plot.table({}))
        try:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
        except ImportError:
            self.skipTest("matplotlib not installed")
        old = plot.RESULTS
        plot.RESULTS = tempfile.mkdtemp()
        try:
            made = plot.plot_all(sample, plt)
            self.assertEqual(len(made), 2)
            self.assertEqual(plot.plot_all({}, plt), [])
        finally:
            shutil.rmtree(plot.RESULTS)
            plot.RESULTS = old

    def test_noise_sender(self):
        import subprocess
        import sys
        server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        server.bind(("127.0.0.1", 6000)) if port_free(6000) else None
        here = os.path.dirname(os.path.abspath(__file__))
        r = subprocess.run([sys.executable, os.path.join(here, "noise.py"), "--to", "127.0.0.1",
                            "--mbps", "2", "--seconds", "1"], stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, universal_newlines=True, timeout=10)
        server.close()
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("stopped, sent", r.stdout)
        r = subprocess.run([sys.executable, os.path.join(here, "noise.py"), "--to", "127.0.0.1",
                            "--mbps", "-1"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           universal_newlines=True, timeout=10)
        self.assertEqual(r.returncode, 2)


def port_free(port):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


class TestRealLaptops(unittest.TestCase):
    """Real mode: psutil numbers, discovery, ping/RTT, web dashboard."""

    def setUp(self):
        try:
            import psutil  # noqa: F401
        except ImportError:
            self.skipTest("psutil not installed")
        self.dir = tempfile.mkdtemp()
        self.rp, self.ap, self.dp, self.wp = free_port(), free_port(), free_port(), free_port()
        self.mon = Monitor(CFG, self.dir, "127.0.0.1", self.rp, self.ap, lan=True,
                           discovery_port=self.dp)
        self.mon.quiet = True
        self.mon.start_web(self.wp, "127.0.0.1")
        threading.Thread(target=self.mon.run, daemon=True).start()

    def tearDown(self):
        self.mon.running = False
        time.sleep(0.6)
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_metrics_are_real_and_fit_in_one_packet(self):
        from metrics import RealMachine
        r = RealMachine().read()
        for k in common.METRICS:
            self.assertTrue(common.is_number(r[k]))
        self.assertTrue(0 <= r["cpu"] <= 100 and 0 < r["mem"] <= 100 and r["procs"] > 0)
        data = make_msg(REPORT, "x" * 32, 1, **r)
        self.assertTrue(common.valid_report(read_msg(data)))

    def test_discovery_ping_reports_and_dashboard(self):
        import urllib.request
        cfg = common.real_config(CFG)
        cfg["real"] = dict(cfg["real"], discovery_port=self.dp)
        found = agent.discover_monitor("t", cfg, targets=["127.0.0.1"])
        self.assertEqual(found, ("127.0.0.1", self.rp, self.ap))
        a = agent.Agent("laptop-1", cfg, "127.0.0.1", self.rp, self.ap, real=True)
        self.assertIsNotNone(a.ping())
        a.run(count=3)
        with urllib.request.urlopen("http://127.0.0.1:%d/api/status" % self.wp) as r:
            st = json.loads(r.read())
        n = st["nodes"][0]
        self.assertEqual(n["name"], "laptop-1")
        self.assertEqual(n["received"], 3)
        self.assertIn("disk", n["info"])
        self.assertIn("rtt_ms", n["info"])
        with urllib.request.urlopen("http://127.0.0.1:%d/" % self.wp) as r:
            self.assertIn(b"Health Monitor", r.read())

    def test_flood_is_ignored_without_controller(self):
        agent.flood("bad", "127.0.0.1", self.rp, 300, 2)
        time.sleep(1)
        self.assertIn("127.0.0.1", self.mon.app_blocked)

    def test_names_are_cleaned(self):
        self.assertEqual(common.clean_name("My Laptop (Tanmay)!"), "My-Laptop-Tanmay")
        self.assertEqual(len(common.clean_name("x" * 100)), 32)


if __name__ == "__main__":
    unittest.main(verbosity=2)
