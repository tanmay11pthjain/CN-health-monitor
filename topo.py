#!/usr/bin/env python3
# Builds the Mininet network, starts the monitor and the agents,
# and adds our own commands to the mininet> prompt.
#
#            [ SDN controller ]
#                    |
#               [ switch s1 ]
#     ______________|_______________________
#    |      |      |      |       |         |
#   h1     h2     h3     h4      h5        h6
#  agent  agent  agent  agent  MONITOR   noisy host
#
# Run (the controller must already be running):
#     sudo python3 topo.py           normal use, opens the mininet> prompt
#     sudo python3 topo.py --test    runs the automatic checks and exits

import argparse
import json
import os
import subprocess
import sys
import time

from mininet.net import Mininet
from mininet.node import RemoteController, OVSSwitch
from mininet.cli import CLI
from mininet.log import setLogLevel, info

HERE = os.path.dirname(os.path.abspath(__file__))

LOGS = os.path.join(HERE, "logs")
STATE_DIR = "/tmp/hmon"
AGENTS = ["h1", "h2", "h3", "h4"]
MONITOR = "h5"
NOISY = "h6"

LINK_MBPS = 10          # speed of every cable
ALERT_QUEUE_MBPS = 8    # speed guaranteed to the alert fast lane


def build(userspace=False):
    switch_opts = {"protocols": "OpenFlow13"}
    if userspace:
        switch_opts["datapath"] = "user"

    net = Mininet(switch=OVSSwitch, controller=None,
                  autoSetMacs=True, waitConnected=False)
    net.addController("c0", controller=RemoteController, ip="127.0.0.1", port=6653)
    s1 = net.addSwitch("s1", **switch_opts)
    for i in range(1, 7):
        h = net.addHost("h%d" % i, ip="10.0.0.%d/24" % i)
        # the speed of each cable is set by the switch queues below
        net.addLink(h, s1)
    return net


def setup_queues(net):
    """Give every switch port two queues:
       queue 0 = normal traffic, queue 1 = alert fast lane (guaranteed speed)."""
    s1 = net.get("s1")
    max_bps = LINK_MBPS * 1000000
    fast_bps = ALERT_QUEUE_MBPS * 1000000
    for intf in s1.intfList():
        if intf.name == "lo":
            continue
        subprocess.run(
            ["ovs-vsctl", "--", "set", "port", intf.name, "qos=@q", "--",
             "--id=@q", "create", "qos", "type=linux-htb",
             "other-config:max-rate=%d" % max_bps, "queues:0=@q0", "queues:1=@q1", "--",
             "--id=@q0", "create", "queue", "other-config:max-rate=%d" % max_bps,
             "other-config:priority=2", "--",
             "--id=@q1", "create", "queue", "other-config:min-rate=%d" % fast_bps,
             "other-config:max-rate=%d" % max_bps, "other-config:priority=1"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def remove_queues():
    subprocess.run("ovs-vsctl --all destroy qos -- --all destroy queue",
                   shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def start_monitor(net):
    h = net.get(MONITOR)
    h.cmd("python3 -u %s/monitor.py > %s/monitor_output.txt 2>&1 &" % (HERE, LOGS))


def start_agent(net, name):
    h = net.get(name)
    h.cmd("python3 -u %s/agent.py --name %s >> %s/agent_%s.txt 2>&1 &" % (HERE, name, LOGS, name))


def stop_agent(net, name):
    net.get(name).cmd("pkill -f 'agent.py --name %s$'" % name)


def set_state(name, state):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(os.path.join(STATE_DIR, name + ".state"), "w") as f:
        f.write(state)


def read_status():
    try:
        with open(os.path.join(LOGS, "status.json")) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"nodes": {}, "events": []}


class HealthCLI(CLI):
    """The normal mininet> prompt plus our own commands."""

    prompt = "mininet> "

    def do_spike(self, line):
        """spike h2 [cpu|mem|procs] : make a server very busy (default cpu)"""
        parts = line.split()
        if not parts or parts[0] not in AGENTS:
            print("usage: spike h2 [cpu|mem|procs]")
            return
        what = parts[1] if len(parts) > 1 else "cpu"
        if what not in ("cpu", "mem", "procs"):
            print("choose cpu, mem or procs")
            return
        set_state(parts[0], what)
        print("%s %s is going up. Watch the monitor screen." % (parts[0], what))

    def do_calm(self, line):
        """calm h2 : put a server back to normal"""
        name = line.strip()
        if name not in AGENTS:
            print("usage: calm h2")
            return
        set_state(name, "normal")
        print("%s is going back to normal." % name)

    def do_fail(self, line):
        """fail h4 : switch off the agent on a server (server failure)"""
        name = line.strip()
        if name not in AGENTS:
            print("usage: fail h4")
            return
        stop_agent(self.mn, name)
        print("%s agent stopped at %s. The monitor should say DOWN in about 3 seconds."
              % (name, time.strftime("%H:%M:%S")))

    def do_recover(self, line):
        """recover h4 : switch the agent on again"""
        name = line.strip()
        if name not in AGENTS:
            print("usage: recover h4")
            return
        stop_agent(self.mn, name)
        start_agent(self.mn, name)
        print("%s agent started again." % name)

    def do_screen(self, line):
        """screen : show the monitor table once"""
        try:
            with open(os.path.join(LOGS, "screen.txt")) as f:
                print(f.read())
        except OSError:
            print("no screen yet, wait a second")


# ----------------------------------------------------------------------
# automatic test (sudo python3 topo.py --test)
# ----------------------------------------------------------------------

def wait_for(check, seconds):
    end = time.time() + seconds
    while time.time() < end:
        if check():
            return True
        time.sleep(0.5)
    return False


def run_tests(net):
    results = []

    def result(name, ok, detail=""):
        results.append(ok)
        print("  [%s] %s %s" % ("PASS" if ok else "FAIL", name, detail), flush=True)

    print("\n*** Running automatic tests\n")

    loss = net.pingAll(timeout=1)
    result("every host can reach every other host (pingall)", loss == 0, "(%d%% lost)" % loss)

    ok = wait_for(lambda: all(read_status()["nodes"].get(a, {}).get("status") == "UP"
                              for a in AGENTS), 15)
    result("all 4 agents are UP on the monitor", ok)

    ok = wait_for(lambda: any("controller: hello" in e for e in read_status()["events"]), 10)
    result("monitor and controller talk on port 9999", ok)

    set_state("h2", "cpu")
    ok = wait_for(lambda: any("ALERT CRIT h2 cpu" in e for e in read_status()["events"]), 15)
    result("CPU spike on h2 gives a CRITICAL alert", ok)
    set_state("h2", "normal")

    stop_agent(net, "h4")
    ok = wait_for(lambda: read_status()["nodes"].get("h4", {}).get("status") == "DOWN", 10)
    result("stopped agent h4 is marked DOWN", ok)

    ok = wait_for(lambda: any("h4 is down, removed" in e for e in read_status()["events"]), 8)
    result("controller confirms h4 is down", ok)

    start_agent(net, "h4")
    ok = wait_for(lambda: read_status()["nodes"].get("h4", {}).get("status") == "UP", 10)
    result("h4 comes back UP after recover", ok)

    flows = net.get("s1").dpctl("dump-flows", "-O", "OpenFlow13")
    if read_status().get("controller_mode") == "simple":
        result("simple mode: no fast lane rule in the switch", "set_queue:1" not in flows)
    else:
        result("smart mode: alert fast lane rule is in the switch", "set_queue:1" in flows)

    status = read_status()
    lost = sum(n.get("lost", 0) for n in status["nodes"].values())
    got = sum(n.get("received", 0) for n in status["nodes"].values())
    result("reports received with little loss", got > 0 and lost <= max(2, got // 50),
           "(received %d, lost %d)" % (got, lost))

    passed = sum(results)
    print("\n*** %d of %d tests passed\n" % (passed, len(results)))
    return passed == len(results)


# ----------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--test", action="store_true", help="run automatic tests and exit")
    p.add_argument("--userspace", action="store_true",
                   help="use the OVS userspace switch (only if the kernel module is missing)")
    args = p.parse_args()

    if os.geteuid() != 0:
        print("Please run with sudo:  sudo python3 topo.py")
        sys.exit(1)

    setLogLevel("info")
    os.makedirs(LOGS, exist_ok=True)
    os.makedirs(STATE_DIR, exist_ok=True)
    for name in AGENTS:
        set_state(name, "normal")
    for f in ["screen.txt", "status.json"]:
        try:
            os.remove(os.path.join(LOGS, f))
        except OSError:
            pass

    net = build(args.userspace)
    ok = True
    try:
        net.start()
        info("*** Waiting for the switch to connect to the controller\n")
        if not net.waitConnected(timeout=15):
            print("\n!!! The switch could not reach the controller.")
            print("!!! Start the controller first (Window 1), then run this again.\n")
            raise SystemExit(1)
        setup_queues(net)
        if args.userspace:
            # the userspace switch can't fix UDP checksums for the hosts
            for h in net.hosts:
                h.cmd("ethtool -K %s-eth0 tx off" % h.name)

        start_monitor(net)
        time.sleep(1)
        info("*** Monitor server started on h5 (10.0.0.5)\n")
        for name in AGENTS:
            start_agent(net, name)
        info("*** Agents started on %s\n" % " ".join(AGENTS))
        info("*** Watch the monitor in another window:  watch -n 1 cat logs/screen.txt\n")

        if args.test:
            time.sleep(3)
            ok = run_tests(net)
        else:
            info("*** Our commands: spike, calm, fail, recover, screen  (type 'help')\n")
            HealthCLI(net)
    finally:
        for name in AGENTS + [MONITOR]:
            net.get(name).cmd("pkill -f '%s/(agent|monitor).py'" % HERE)
        net.stop()
        remove_queues()
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
