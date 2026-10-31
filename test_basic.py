#!/usr/bin/env python3
# Quick tests that run on one computer, no Mininet needed.
#
#   python3 test_basic.py
#
# They check the message format, the alert limits, and run a real
# monitor + agents on 127.0.0.1 to see reports, alerts, ACKs, resends
# and DOWN detection working.

import json
import os
import shutil
import socket
import tempfile
import threading
import time
import unittest

import agent
from common import load_config, make_msg, read_msg, check_level, worst_level, REPORT, ALERT, ACK
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
