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
# With --agents N (for the "more servers" test) the extra agents are
# h7, h8, ... with addresses 10.0.0.7, 10.0.0.8, ...
#
# Every switch port has two lanes (Linux HTB, 10 Mbit/s in total):
#   fast lane   = packets marked urgent (DSCP 46), 8 Mbit/s guaranteed, served first
#   normal lane = everything else
# Only the smart controller marks packets (alerts), so in simple mode
# everything goes through the normal lane.
#
# Run (the controller must already be running):
#     sudo python3 topo.py           normal use, opens the mininet> prompt
#     sudo python3 topo.py --test    runs the automatic checks and exits

import argparse
import csv
import glob
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
LOGS = os.path.join(HERE, "logs")
STATE_DIR = "/tmp/hmon"
MONITOR = "h5"
NOISY = "h6"

LINK_MBPS = 10          # speed of every cable
FAST_MBPS = 8           # speed guaranteed to the fast lane
JAM_MBPS = 20           # how hard the noisy host sends (twice the cable speed)
FLOOD_PPS = 300         # packets per second for "flood h3"
URGENT_TOS = "0xb8"     # DSCP 46 in the IP header's TOS byte (46 << 2)

PY = "python3 -u"


def agent_names(n=4):
    """h1..h4, then h7, h8 ... (h5 and h6 are the monitor and the noisy host)."""
    if n < 1 or n > 200:
        raise ValueError("number of agents must be from 1 to 200")
    names = ["h%d" % i for i in range(1, min(n, 4) + 1)]
    names += ["h%d" % i for i in range(7, 7 + max(n - 4, 0))]
    return names


AGENTS = agent_names(4)


# ----------------------------------------------------------------------
# the network
# ----------------------------------------------------------------------

def run(cmd):
    """Run a command in the main computer, return (ok, output)."""
    r = subprocess.run(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    return r.returncode == 0, r.stdout.decode("utf-8", "replace")


def kernel_ovs_ok():
    """True if Open vSwitch can use the fast kernel switch."""
    if os.path.exists("/sys/module/openvswitch"):
        return True
    return run("modprobe openvswitch")[0]


def check_ovs():
    ok, out = run("ovs-vsctl show")
    if not ok:
        print("\n!!! Open vSwitch is not running. Start it with:")
        print("!!!     sudo service openvswitch-switch start\n")
        raise SystemExit(1)


def build(agents, userspace=False):
    from mininet.net import Mininet
    from mininet.node import RemoteController, OVSSwitch

    switch_opts = {"protocols": "OpenFlow13"}
    if userspace:
        switch_opts["datapath"] = "user"
    net = Mininet(switch=OVSSwitch, controller=None, autoSetMacs=True, waitConnected=False)
    net.addController("c0", controller=RemoteController, ip="127.0.0.1", port=6653)
    s1 = net.addSwitch("s1", **switch_opts)
    names = sorted(set(agents) | {MONITOR, NOISY}, key=lambda n: int(n[1:]))
    for name in names:
        h = net.addHost(name, ip="10.0.0.%s/24" % name[1:])
        net.addLink(h, s1)
    return net


def setup_lanes(net):
    """Two lanes on every switch port (see the top of this file)."""
    s1 = net.get("s1")
    rest = LINK_MBPS - FAST_MBPS
    failed = []
    for intf in s1.intfList():
        if intf.name == "lo":
            continue
        dev = intf.name
        cmds = [
            "tc qdisc del dev %s root" % dev,
            "tc qdisc add dev %s root handle 1: htb default 20" % dev,
            "tc class add dev %s parent 1: classid 1:1 htb rate %dmbit ceil %dmbit"
            % (dev, LINK_MBPS, LINK_MBPS),
            "tc class add dev %s parent 1:1 classid 1:10 htb rate %dmbit ceil %dmbit prio 0"
            % (dev, FAST_MBPS, LINK_MBPS),
            "tc class add dev %s parent 1:1 classid 1:20 htb rate %dmbit ceil %dmbit prio 1"
            % (dev, rest, LINK_MBPS),
            "tc qdisc add dev %s parent 1:10 handle 10: pfifo limit 100" % dev,
            "tc qdisc add dev %s parent 1:20 handle 20: pfifo limit 1000" % dev,
            "tc filter add dev %s parent 1: protocol ip prio 1 u32 match ip tos %s 0xfc "
            "flowid 1:10" % (dev, URGENT_TOS),
        ]
        for i, c in enumerate(cmds):
            ok, out = run(c)
            if not ok and i > 0:
                failed.append("%s: %s" % (c, out.strip()))
    if failed:
        print("!!! could not set up the switch lanes:\n  " + "\n  ".join(failed))
    return not failed


def remove_old_qos():
    # Phase 1 used OVS queues. Remove any that are left over.
    run("ovs-vsctl --all destroy qos -- --all destroy queue")


def start_monitor(net, logdir=LOGS):
    h = net.get(MONITOR)
    h.cmd("%s %s/monitor.py --logdir %s >> %s/monitor_output.txt 2>&1 &"
          % (PY, HERE, logdir, logdir))
    pid = h.cmd("echo $!").strip()
    return int(pid) if pid.isdigit() else None


def stop_monitor(net):
    net.get(MONITOR).cmd("pkill -f '%s/monitor.py'" % HERE)


def start_agent(net, name, logdir=LOGS):
    h = net.get(name)
    h.cmd("%s %s/agent.py --sim --name %s >> %s/agent_%s.txt 2>&1 &" % (PY, HERE, name, logdir, name))


def stop_agent(net, name):
    net.get(name).cmd("pkill -f 'agent.py --sim --name %s$'" % name)


def congest(net, on, mbps=JAM_MBPS, seconds=3600):
    h = net.get(NOISY)
    h.cmd("pkill -f '%s/noise.py'" % HERE)
    if on:
        h.cmd("%s %s/noise.py --to %s --mbps %s --seconds %d > /tmp/hmon/noise.txt 2>&1 &"
              % (PY, HERE, net.get(MONITOR).IP(), mbps, seconds))


def flood(net, name, pps=FLOOD_PPS, seconds=20, logdir=LOGS):
    h = net.get(name)
    h.cmd("pkill -f 'agent.py --sim --name %s --flood'" % name)
    h.cmd("%s %s/agent.py --sim --name %s --flood %d --seconds %d >> %s/agent_%s.txt 2>&1 &"
          % (PY, HERE, name, pps, seconds, logdir, name))


def stop_flood(net, name):
    net.get(name).cmd("pkill -f 'agent.py --sim --name %s --flood'" % name)


def set_loss(net, percent, agents):
    """Lose `percent` % of packets on the cables of the agents and the monitor.
    Uses tc netem. If netem is missing, uses iptables (drops packets on arrival)."""
    hosts = [net.get(n) for n in list(agents) + [MONITOR]]
    for h in hosts:
        dev = h.defaultIntf().name
        h.cmd("tc qdisc del dev %s root 2>/dev/null" % dev)
        h.cmd("iptables -D INPUT -j HMON_LOSS 2>/dev/null; iptables -F HMON_LOSS 2>/dev/null;"
              " iptables -X HMON_LOSS 2>/dev/null")
    if percent <= 0:
        return "off"
    out = hosts[0].cmd("tc qdisc add dev %s root netem loss %s%% 2>&1"
                       % (hosts[0].defaultIntf().name, percent))
    use_netem = "rror" not in out and "nknown" not in out
    for h in hosts:
        dev = h.defaultIntf().name
        if use_netem:
            if h is not hosts[0]:
                h.cmd("tc qdisc add dev %s root netem loss %s%%" % (dev, percent))
        else:
            h.cmd("iptables -N HMON_LOSS; iptables -A HMON_LOSS -m statistic --mode random "
                  "--probability %.4f -j DROP; iptables -I INPUT -j HMON_LOSS"
                  % (percent / 100.0))
    return "netem" if use_netem else "iptables"


def set_link(net, name, up):
    net.configLinkStatus(name, "s1", "up" if up else "down")


def set_state(name, state):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(os.path.join(STATE_DIR, name + ".state"), "w") as f:
        f.write(state)


def clean_logs(logdir):
    os.makedirs(logdir, exist_ok=True)
    for pattern in ("screen.txt", "status.json", "reports.csv", "alerts.csv", "events.txt",
                    "monitor_output.txt", "agent_*.txt"):
        for f in glob.glob(os.path.join(logdir, pattern)):
            try:
                os.remove(f)
            except OSError:
                pass


def clean_state():
    os.makedirs(STATE_DIR, exist_ok=True)
    for f in glob.glob(os.path.join(STATE_DIR, "*")):
        try:
            os.remove(f)
        except OSError:
            pass


def read_status(logdir=LOGS):
    try:
        with open(os.path.join(logdir, "status.json")) as f:
            data = json.load(f)
        if isinstance(data, dict):
            data.setdefault("nodes", {})
            data.setdefault("events", [])
            return data
    except (OSError, ValueError):
        pass
    return {"nodes": {}, "events": []}


def read_alerts(logdir=LOGS):
    try:
        with open(os.path.join(logdir, "alerts.csv")) as f:
            return list(csv.DictReader(f))
    except OSError:
        return []


def start_all(net, agents, userspace, logdir=LOGS):
    """Everything after net.start(): lanes, checksum fix, monitor, agents."""
    setup_lanes(net)
    if userspace:
        # the userspace switch can't fix UDP checksums for the hosts
        for h in net.hosts:
            h.cmd("ethtool -K %s tx off" % h.defaultIntf().name)
    for name in agents:
        set_state(name, "normal")
    pid = start_monitor(net, logdir)
    time.sleep(1)
    for name in agents:
        start_agent(net, name, logdir)
    return pid


def stop_all(net):
    for h in net.hosts:
        h.cmd("pkill -f '%s/(agent|monitor|noise).py'" % HERE)
    time.sleep(0.3)


# ----------------------------------------------------------------------
# our commands at the mininet> prompt
# ----------------------------------------------------------------------

def make_cli():
    from mininet.cli import CLI

    class HealthCLI(CLI):
        """The normal mininet> prompt plus our own commands."""

        prompt = "mininet> "

        def __init__(self, net, agents, **kw):
            self.agents = agents
            CLI.__init__(self, net, **kw)

        def agent_arg(self, line, usage):
            name = line.strip()
            if name not in self.agents:
                print("usage: %s   (agents: %s)" % (usage, " ".join(self.agents)))
                return None
            return name

        def do_spike(self, line):
            """spike h2 [cpu|mem|procs] : make a server very busy (default cpu)"""
            parts = line.split()
            if not parts or parts[0] not in self.agents:
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
            name = self.agent_arg(line, "calm h2")
            if name:
                set_state(name, "normal")
                print("%s is going back to normal." % name)

        def do_fail(self, line):
            """fail h4 : switch off the agent on a server (server failure)"""
            name = self.agent_arg(line, "fail h4")
            if name:
                stop_agent(self.mn, name)
                print("%s agent stopped at %s. The monitor should say DOWN in about 3 seconds."
                      % (name, time.strftime("%H:%M:%S")))

        def do_recover(self, line):
            """recover h4 : switch the agent on again"""
            name = self.agent_arg(line, "recover h4")
            if name:
                stop_agent(self.mn, name)
                start_agent(self.mn, name)
                print("%s agent started again." % name)

        def do_congest(self, line):
            """congest on|off : h6 sends 20 Mbit/s of junk to the monitor (traffic jam)"""
            arg = line.strip()
            if arg not in ("on", "off"):
                print("usage: congest on   or   congest off")
                return
            congest(self.mn, arg == "on")
            if arg == "on":
                print("traffic jam ON: h6 sends %d Mbit/s to h5 (the cable is %d Mbit/s)."
                      % (JAM_MBPS, LINK_MBPS))
                print("In smart mode the controller slows h6 down in a few seconds.")
            else:
                print("traffic jam OFF.")

        def do_flood(self, line):
            """flood h3 : h3 floods the monitor with packets for 20 s"""
            name = self.agent_arg(line, "flood h3")
            if name:
                flood(self.mn, name)
                print("%s floods the monitor with %d packets/s for 20 s. In smart mode the "
                      "controller blocks it." % (name, FLOOD_PPS))

        def do_loss(self, line):
            """loss 10 : lose 10% of packets on the agents' and monitor's cables (loss 0 = off)"""
            try:
                pct = float(line.strip())
            except ValueError:
                pct = -1
            if not 0 <= pct <= 100:
                print("usage: loss 10   (a number from 0 to 100)")
                return
            how = set_loss(self.mn, pct, self.agents)
            print("packet loss is now %s%% (%s)" % (pct, how))

        def do_cutlink(self, line):
            """cutlink h4 : unplug the cable of h4"""
            name = self.agent_arg(line, "cutlink h4")
            if name:
                set_link(self.mn, name, False)
                print("cable of %s unplugged at %s." % (name, time.strftime("%H:%M:%S")))

        def do_fixlink(self, line):
            """fixlink h4 : plug the cable of h4 back in"""
            name = self.agent_arg(line, "fixlink h4")
            if name:
                set_link(self.mn, name, True)
                print("cable of %s plugged in again." % name)

        def do_restartmon(self, line):
            """restartmon : stop the monitor and start it again (agents keep going)"""
            stop_monitor(self.mn)
            time.sleep(1)
            start_monitor(self.mn)
            print("monitor restarted. The agents never stopped, the table fills up again.")

        def do_screen(self, line):
            """screen : show the monitor table once"""
            try:
                with open(os.path.join(LOGS, "screen.txt")) as f:
                    print(f.read())
            except OSError:
                print("no screen yet, wait a second")

    return HealthCLI


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


def has_event(text):
    return any(text in e for e in read_status()["events"])


def node_status(name):
    return read_status()["nodes"].get(name, {}).get("status")


def run_tests(net, agents):
    results = []

    def result(name, ok, detail=""):
        results.append(ok)
        print("  [%s] %s %s" % ("PASS" if ok else "FAIL", name, detail), flush=True)

    print("\n*** Running automatic tests\n")

    # ---------- Phase 1 ----------
    loss = net.pingAll(timeout=1)
    result("every host can reach every other host (pingall)", loss == 0, "(%d%% lost)" % loss)

    ok = wait_for(lambda: all(node_status(a) == "UP" for a in agents), 15)
    result("all %d agents are UP on the monitor" % len(agents), ok)

    ok = wait_for(lambda: has_event("controller: hello"), 10)
    result("monitor and controller talk on port 9999", ok)
    mode = read_status().get("controller_mode")
    print("      controller mode: %s" % mode)
    smart = mode == "smart"

    set_state("h2", "cpu")
    ok = wait_for(lambda: has_event("ALERT CRIT h2 cpu"), 15)
    result("CPU spike on h2 gives a CRITICAL alert", ok)
    set_state("h2", "normal")

    stop_agent(net, "h4")
    ok = wait_for(lambda: node_status("h4") == "DOWN", 10)
    result("stopped agent h4 is marked DOWN", ok)

    ok = wait_for(lambda: has_event("h4 is down, removed"), 8)
    result("controller confirms h4 is down", ok)

    start_agent(net, "h4")
    ok = wait_for(lambda: node_status("h4") == "UP", 10)
    result("h4 comes back UP after recover", ok)

    flows = net.get("s1").dpctl("dump-flows", "-O", "OpenFlow13")
    mark = "set_field:46->ip_dscp"
    if smart:
        result("smart mode: alert fast lane rule is in the switch", mark in flows)
    else:
        result("simple mode: no fast lane rule in the switch", mark not in flows)

    ok, out = run("tc class show dev s1-eth5")
    result("switch port to the monitor has a fast lane and a normal lane",
           "1:10" in out and "1:20" in out)

    # ---------- Phase 2 ----------
    print("\n  -- Phase 2 --")

    # traffic jam
    congest(net, True)
    time.sleep(1)
    set_state("h1", "cpu")
    ok = wait_for(lambda: has_event("ALERT CRIT h1 cpu"), 20)
    result("alert from h1 still arrives during the traffic jam", ok)
    set_state("h1", "normal")
    if smart:
        ok = wait_for(lambda: has_event("SLOWED DOWN"), 15)
        result("smart mode: controller slows down the noisy host h6", ok)
        flows = net.get("s1").dpctl("dump-flows", "-O", "OpenFlow13")
        result("smart mode: meter rule for h6 is in the switch", "meter:" in flows)
    congest(net, False)
    if smart:
        ok = wait_for(lambda: has_event("speed limit REMOVED"), 20)
        result("smart mode: limit on h6 is removed when it is quiet again", ok)
    wait_for(lambda: has_event("CLEAR h1 cpu"), 15)

    # flooding host
    flood(net, "h3", seconds=8)
    ok = wait_for(lambda: has_event("FLOOD h3"), 10)
    result("monitor notices that h3 is flooding", ok)
    if smart:
        ok = wait_for(lambda: has_event("BLOCKED h3"), 5)
        result("smart mode: controller blocks h3", ok)
        flows = net.get("s1").dpctl("dump-flows", "-O", "OpenFlow13")
        result("smart mode: drop rule for 10.0.0.3 is in the switch", "nw_src=10.0.0.3" in flows)
        ok = wait_for(lambda: node_status("h3") == "DOWN", 10)
        result("blocked h3 shows DOWN on the monitor", ok)
    else:
        ok = wait_for(lambda: has_event("blocking is off"), 5)
        result("simple mode: controller can't block h3", ok)
    block_started = time.time()

    # cable cut
    set_link(net, "h4", False)
    ok = wait_for(lambda: has_event("is DOWN") and node_status("h4") == "DOWN", 10)
    result("cut cable: controller reports it and h4 goes DOWN", ok)
    set_link(net, "h4", True)
    ok = wait_for(lambda: node_status("h4") == "UP", 15)
    result("fixed cable: h4 comes back UP", ok)

    # packet loss
    how = set_loss(net, 20, agents)
    before = sum(1 for r in read_alerts() if r.get("kind") == "ALERT" and r.get("node") == "h2")
    set_state("h2", "mem")
    ok = wait_for(lambda: sum(1 for r in read_alerts() if r.get("kind") == "ALERT"
                              and r.get("node") == "h2") >= before + 2, 25)
    result("20%% packet loss (%s): alerts still arrive thanks to resend" % how, ok)
    set_state("h2", "normal")
    set_loss(net, 0, agents)

    # monitor restart
    stop_monitor(net)
    time.sleep(1.5)
    start_monitor(net)
    ok = wait_for(lambda: has_event("monitor started")
                  and sum(1 for a in agents if node_status(a) == "UP") >= len(agents) - 1, 15)
    result("monitor restart: agents keep going and show up again", ok)

    if smart:
        left = 30 + 5 - (time.time() - block_started)
        print("      waiting up to %.0f s for the block on h3 to end ..." % max(left, 0))
        ok = wait_for(lambda: node_status("h3") == "UP", max(left, 10) + 10)
        result("smart mode: h3 is let in again after the block time and is UP", ok)

    status = read_status()
    lost = sum(n.get("lost", 0) for n in status["nodes"].values())
    got = sum(n.get("received", 0) for n in status["nodes"].values())
    result("monitor kept working the whole time", got > 0, "(received %d reports since "
           "restart, %d lost)" % (got, lost))

    passed = sum(results)
    print("\n*** %d of %d tests passed\n" % (passed, len(results)))
    return passed == len(results)


# ----------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--test", action="store_true", help="run automatic tests and exit")
    p.add_argument("--agents", type=int, default=4, help="number of agents (default 4)")
    p.add_argument("--userspace", action="store_true",
                   help="use the OVS userspace switch (automatic if the kernel module is missing)")
    args = p.parse_args()

    if os.geteuid() != 0:
        print("Please run with sudo:  sudo python3 topo.py")
        sys.exit(1)
    try:
        agents = agent_names(args.agents)
    except ValueError as e:
        print("ERROR: %s" % e)
        sys.exit(2)
    try:
        sys.path.insert(0, HERE)
        from common import load_config, ConfigError
        load_config()
    except ConfigError as e:
        print("ERROR: %s" % e)
        sys.exit(2)
    try:
        from mininet.log import setLogLevel, info
    except ImportError:
        print("ERROR: Mininet is not installed. Run:  bash setup.sh")
        sys.exit(1)

    check_ovs()
    userspace = args.userspace
    if not userspace and not kernel_ovs_ok():
        print("NOTE: no Open vSwitch kernel module here, using the userspace switch.")
        userspace = True

    setLogLevel("info")
    clean_logs(LOGS)
    clean_state()
    remove_old_qos()

    net = build(agents, userspace)
    ok = True
    try:
        net.start()
        info("*** Waiting for the switch to connect to the controller\n")
        if not net.waitConnected(timeout=15):
            print("\n!!! The switch could not reach the controller.")
            print("!!! Start the controller first (Window 1), then run this again.\n")
            ok = False
            return
        start_all(net, agents, userspace)
        info("*** Monitor server started on h5 (10.0.0.5)\n")
        info("*** Agents started on %s\n" % " ".join(agents))
        info("*** Watch the monitor in another window:  watch -n 1 cat logs/screen.txt\n")

        if args.test:
            time.sleep(3)
            ok = run_tests(net, agents)
        else:
            info("*** Our commands: spike, calm, fail, recover, congest, flood, loss, "
                 "cutlink, fixlink, restartmon, screen  (type 'help')\n")
            make_cli()(net, agents)
    finally:
        try:
            set_loss(net, 0, agents)
            stop_all(net)
        finally:
            net.stop()
        sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
