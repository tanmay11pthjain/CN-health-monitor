#!/usr/bin/env python3
# Runs the Phase 2 experiments in simple mode (baseline) and smart mode,
# and saves the numbers in results/results.json. Then run plot.py for graphs.
#
#   sudo python3 experiments.py                 all 5 tests, both modes (about 15 minutes)
#   sudo python3 experiments.py --quick         shorter runs (about 8 minutes)
#   sudo python3 experiments.py --tests jam,loss --modes smart
#
# This script starts and stops the SDN controller by itself, so close the
# controller in Window 1 first.
#
# The 5 tests (PLAN.md section 9):
#   jam      no traffic jam vs heavy jam          -> how long alerts take to arrive
#   failure  stop an agent / cut a cable           -> how long until the monitor says DOWN
#   loss     0, 5, 10, 20 % of packets lost         -> how many alerts still arrive
#   scale    4, 10, 20, 50 agents                   -> lost reports, latency, monitor CPU
#   flood    one host floods the monitor            -> time until it gets blocked

import argparse
import csv
import json
import os
import shutil
import signal
import socket
import statistics
import subprocess
import sys
import time
import traceback

import topo
from topo import (HERE, agent_names, build, start_all, stop_all, set_state,
                  stop_agent, start_agent, congest, flood, set_loss, set_link,
                  clean_state, read_status, wait_for)

RESULTS = os.path.join(HERE, "results")
RUNS = os.path.join(RESULTS, "runs")
TESTS = ["jam", "failure", "loss", "scale", "flood"]
MODES = ["simple", "smart"]
CLK = os.sysconf("SC_CLK_TCK")


def say(text):
    print("[%s] %s" % (time.strftime("%H:%M:%S"), text), flush=True)


# ----------------------------------------------------------------------
# the controller
# ----------------------------------------------------------------------

def find_osken():
    for path in (os.path.join(HERE, "venv", "bin", "osken-manager"),
                 shutil.which("osken-manager") or ""):
        if path and os.access(path, os.X_OK):
            return path
    return None


def port_open(port, host="127.0.0.1"):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.5)
    try:
        return s.connect_ex((host, port)) == 0
    finally:
        s.close()


class Controller:
    def __init__(self, mode):
        self.mode = mode
        self.proc = None

    def __enter__(self):
        exe = find_osken()
        if exe is None:
            raise SystemExit("ERROR: osken-manager not found. Run  bash setup.sh  first.")
        env = dict(os.environ, HMON_MODE=self.mode)
        self.log = open(os.path.join(RESULTS, "controller_%s.log" % self.mode), "w")
        self.proc = subprocess.Popen([exe, os.path.join(HERE, "controller.py")], cwd=HERE,
                                     env=env, stdout=self.log, stderr=subprocess.STDOUT)
        if not wait_for(lambda: port_open(6653) or self.proc.poll() is not None, 20) \
                or self.proc.poll() is not None:
            self.__exit__()
            raise SystemExit("ERROR: the controller did not start. See results/controller_%s.log"
                             % self.mode)
        say("controller started in %s mode" % self.mode)
        return self

    def __exit__(self, *exc):
        if self.proc and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGINT)
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        self.log.close()
        wait_for(lambda: not port_open(6653), 10)


# ----------------------------------------------------------------------
# one network for one run
# ----------------------------------------------------------------------

class Run:
    """Builds a fresh network, starts monitor + agents, and cleans up after."""

    def __init__(self, name, n_agents, userspace):
        self.name = name
        self.agents = agent_names(n_agents)
        self.userspace = userspace
        self.logdir = os.path.join(RUNS, name)

    def __enter__(self):
        if os.path.isdir(self.logdir):
            shutil.rmtree(self.logdir)
        os.makedirs(self.logdir)
        clean_state()
        self.net = build(self.agents, self.userspace)
        self.net.start()
        if not self.net.waitConnected(timeout=20):
            self.__exit__()
            raise RuntimeError("switch did not connect to the controller")
        self.mon_pid = start_all(self.net, self.agents, self.userspace, self.logdir)
        ok = wait_for(lambda: sum(1 for a in self.agents if self.status(a) == "UP")
                      == len(self.agents), 20 + len(self.agents) / 5)
        if not ok:
            say("  note: not every agent showed up (%d of %d)" % (
                sum(1 for a in self.agents if self.status(a) == "UP"), len(self.agents)))
        time.sleep(1)
        return self

    def __exit__(self, *exc):
        try:
            stop_all(self.net)
        finally:
            self.net.stop()

    # helpers
    def read_status(self):
        return read_status(self.logdir)

    def status(self, name):
        return self.read_status()["nodes"].get(name, {}).get("status")

    def rows(self, name):
        try:
            with open(os.path.join(self.logdir, name)) as f:
                return list(csv.DictReader(f))
        except OSError:
            return []

    def alerts(self, since=0, kind="ALERT"):
        out = []
        for r in self.rows("alerts.csv"):
            try:
                if r["kind"] == kind and float(r["time"]) >= since:
                    out.append(r)
            except (KeyError, ValueError):
                pass
        return out

    def report_latency(self, since, until=None):
        lat = []
        for r in self.rows("reports.csv"):
            try:
                t = float(r["time"])
                if t >= since and (until is None or t <= until):
                    lat.append(float(r["latency_ms"]))
            except (KeyError, ValueError):
                pass
        return lat

    def agent_stats(self):
        """Stop the agents (they save their counters) and read the counters."""
        for a in self.agents:
            stop_agent(self.net, a)
        wait_for(lambda: all(not self.agent_running(a) for a in self.agents), 15)
        total = {}
        for a in self.agents:
            try:
                with open(os.path.join(topo.STATE_DIR, a + ".stats")) as f:
                    st = json.load(f)
            except (OSError, ValueError):
                continue
            for k, v in st.items():
                if isinstance(v, (int, float)) and k not in ("time", "boot"):
                    total[k] = total.get(k, 0) + v
        return total

    def agent_running(self, name):
        out = self.net.get(name).cmd("pgrep -f 'agent.py --sim --name %s$'" % name)
        return out.strip() != ""

    def cpu_seconds(self):
        try:
            with open("/proc/%d/stat" % self.mon_pid) as f:
                parts = f.read().rsplit(")", 1)[1].split()
            return (int(parts[11]) + int(parts[12])) / CLK      # utime + stime
        except (OSError, ValueError, IndexError, TypeError):
            return None

    def spike_cycles(self, names, cycles, up=3.0, down=3.0):
        for _ in range(cycles):
            for n in names:
                set_state(n, "cpu")
            time.sleep(up)
            for n in names:
                set_state(n, "normal")
            time.sleep(down)


def summary(values):
    values = [v for v in values if v is not None]
    if not values:
        return {"n": 0, "mean": None, "median": None, "p95": None, "max": None}
    values = sorted(values)
    p95 = values[min(len(values) - 1, int(round(0.95 * (len(values) - 1))))]
    return {"n": len(values), "mean": round(statistics.mean(values), 3),
            "median": round(statistics.median(values), 3), "p95": round(p95, 3),
            "max": round(values[-1], 3)}


# ----------------------------------------------------------------------
# the 5 tests
# ----------------------------------------------------------------------

def test_jam(mode, quick, userspace):
    rows = []
    cycles = 3 if quick else 5
    for jam in (False, True):
        with Run("%s_jam_%s" % (mode, "on" if jam else "off"), 4, userspace) as r:
            if jam:
                congest(r.net, True)
                time.sleep(1)
            t0 = time.time()
            r.spike_cycles(r.agents, cycles)
            time.sleep(4)          # let the last alerts (and resends) arrive
            congest(r.net, False)
            got = r.alerts(t0)
            st = r.agent_stats()
            lat = [float(a["latency_ms"]) for a in got]
            rows.append({"jam": jam, "alerts_made": st.get("alerts_made", 0),
                         "alerts_arrived": len(got),
                         "alerts_failed": st.get("alerts_failed", 0),
                         "alert_latency_ms": summary(lat),
                         "report_latency_ms": summary(r.report_latency(t0)),
                         "slowed_events": len(r.alerts(t0, "slowed"))})
            say("  jam=%s  alerts %d/%d  latency mean %s ms  p95 %s ms" % (
                jam, len(got), st.get("alerts_made", 0), rows[-1]["alert_latency_ms"]["mean"],
                rows[-1]["alert_latency_ms"]["p95"]))
    return rows


def test_failure(mode, quick, userspace):
    rows = []
    repeats = 2 if quick else 4
    with Run("%s_failure" % mode, 4, userspace) as r:
        for kind, name in (("stop agent", "h4"), ("cut cable", "h3")):
            times = []
            for _ in range(repeats):
                t0 = time.time()
                if kind == "stop agent":
                    stop_agent(r.net, name)
                else:
                    set_link(r.net, name, False)
                ok = wait_for(lambda: any(a["node"] == name for a in r.alerts(t0, "DOWN")), 10)
                if ok:
                    down = min(float(a["time"]) for a in r.alerts(t0, "DOWN")
                               if a["node"] == name)
                    times.append(down - t0)
                if kind == "stop agent":
                    start_agent(r.net, name, r.logdir)
                else:
                    set_link(r.net, name, True)
                wait_for(lambda: r.status(name) == "UP", 15)
                time.sleep(1.5)
            rows.append({"kind": kind, "detect_s": summary(times), "missed": repeats - len(times)})
            say("  %s: detected DOWN after %s s (mean of %d)" % (
                kind, rows[-1]["detect_s"]["mean"], len(times)))
    return rows


def test_loss(mode, quick, userspace):
    rows = []
    cycles = 4 if quick else 8
    for pct in (0, 5, 10, 20):
        with Run("%s_loss_%d" % (mode, pct), 4, userspace) as r:
            how = set_loss(r.net, pct, r.agents)
            t0 = time.time()
            r.spike_cycles(r.agents, cycles)
            time.sleep(8)          # resends can take 0.5 + 1 + 2 + 4 s
            got = r.alerts(t0)
            set_loss(r.net, 0, r.agents)
            st = r.agent_stats()
            made = st.get("alerts_made", 0)
            first = sum(1 for a in got if a.get("tries") == "1")
            status = r.read_status()["nodes"]
            rec = sum(n.get("received", 0) for n in status.values())
            lost = sum(n.get("lost", 0) for n in status.values())
            rows.append({"loss_pct": pct, "method": how, "alerts_made": made,
                         "alerts_arrived": len(got),
                         "delivered_pct": round(100.0 * len(got) / made, 1) if made else None,
                         "first_try_pct": round(100.0 * first / made, 1) if made else None,
                         "alert_latency_ms": summary([float(a["latency_ms"]) for a in got]),
                         "report_loss_pct": round(100.0 * lost / (rec + lost), 1)
                         if rec + lost else None})
            say("  loss %d%%: alerts delivered %s%% (without resend it would be %s%%), "
                "reports lost %s%%" % (pct, rows[-1]["delivered_pct"], rows[-1]["first_try_pct"],
                                       rows[-1]["report_loss_pct"]))
    return rows


def test_scale(mode, quick, userspace):
    rows = []
    seconds = 12 if quick else 25
    for n in (4, 10, 20, 50):
        with Run("%s_scale_%d" % (mode, n), n, userspace) as r:
            before = r.read_status()["nodes"]
            c0, t0 = r.cpu_seconds(), time.time()
            set_state("h1", "cpu")
            time.sleep(seconds / 2.0)
            set_state("h1", "normal")
            time.sleep(seconds / 2.0)
            c1, t1 = r.cpu_seconds(), time.time()
            after = r.read_status()["nodes"]
            rec = sum(after.get(a, {}).get("received", 0) - before.get(a, {}).get("received", 0)
                      for a in r.agents)
            lost = sum(after.get(a, {}).get("lost", 0) - before.get(a, {}).get("lost", 0)
                       for a in r.agents)
            up = sum(1 for a in r.agents if after.get(a, {}).get("status") == "UP")
            cpu = round(100.0 * (c1 - c0) / (t1 - t0), 2) if c0 is not None and c1 is not None \
                else None
            rows.append({"agents": n, "agents_up": up, "reports": rec, "lost": lost,
                         "report_loss_pct": round(100.0 * lost / (rec + lost), 2)
                         if rec + lost else None,
                         "report_latency_ms": summary(r.report_latency(t0, t1)),
                         "alert_latency_ms": summary([float(a["latency_ms"])
                                                      for a in r.alerts(t0)]),
                         "monitor_cpu_pct": cpu})
            say("  %d agents: %d UP, reports lost %s%%, latency mean %s ms, monitor CPU %s%%" % (
                n, up, rows[-1]["report_loss_pct"], rows[-1]["report_latency_ms"]["mean"], cpu))
    return rows


def test_flood(mode, quick, userspace):
    seconds = 15
    with Run("%s_flood" % mode, 4, userspace) as r:
        t0 = time.time()
        flood(r.net, "h3", seconds=seconds, logdir=r.logdir)
        wait_for(lambda: r.alerts(t0, "blocked") or r.alerts(t0, "block_refused"), 8)
        time.sleep(seconds + 1)
        noticed = r.alerts(t0, "FLOOD")
        blocked = r.alerts(t0, "blocked")
        bad = r.read_status().get("bad_packets", 0)
        others = [a for a in r.agents if a != "h3"]
        lat = []
        for row in r.rows("reports.csv"):
            try:
                if float(row["time"]) >= t0 and row["node"] in others:
                    lat.append(float(row["latency_ms"]))
            except (KeyError, ValueError):
                pass
        row = {"time_to_notice_s": round(float(noticed[0]["time"]) - t0, 2) if noticed else None,
               "time_to_block_s": round(float(blocked[0]["time"]) - t0, 2) if blocked else None,
               "flood_packets_at_monitor": bad,
               "flood_packets_sent": int(300 * seconds),
               "other_agents_latency_ms": summary(lat)}
    say("  flood: noticed after %s s, blocked after %s s, %d of ~%d junk packets reached "
        "the monitor" % (row["time_to_notice_s"], row["time_to_block_s"],
                         row["flood_packets_at_monitor"], row["flood_packets_sent"]))
    return [row]


RUNNERS = {"jam": test_jam, "failure": test_failure, "loss": test_loss,
           "scale": test_scale, "flood": test_flood}


# ----------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(description="Phase 2 experiments: simple vs smart mode")
    p.add_argument("--tests", default=",".join(TESTS),
                   help="comma separated, from: %s" % ",".join(TESTS))
    p.add_argument("--modes", default=",".join(MODES), help="simple,smart")
    p.add_argument("--quick", action="store_true", help="shorter runs")
    p.add_argument("--userspace", action="store_true",
                   help="use the OVS userspace switch (automatic if the kernel module is missing)")
    args = p.parse_args()

    tests = [t.strip() for t in args.tests.split(",") if t.strip()]
    modes = [m.strip() for m in args.modes.split(",") if m.strip()]
    bad = [t for t in tests if t not in TESTS] + [m for m in modes if m not in MODES]
    if bad or not tests or not modes:
        print("ERROR: unknown test or mode: %s" % ", ".join(bad or ["(none given)"]))
        print("tests: %s   modes: %s" % (", ".join(TESTS), ", ".join(MODES)))
        sys.exit(2)
    if os.geteuid() != 0:
        print("Please run with sudo:  sudo python3 experiments.py")
        sys.exit(1)
    try:
        from common import load_config, ConfigError
        load_config()
    except ConfigError as e:
        print("ERROR: %s" % e)
        sys.exit(2)
    try:
        from mininet.log import setLogLevel
        from mininet.clean import cleanup
    except ImportError:
        print("ERROR: Mininet is not installed. Run:  bash setup.sh")
        sys.exit(1)
    topo.check_ovs()
    if port_open(6653):
        print("ERROR: something is already using port 6653 (probably the controller in "
              "Window 1).\nStop it with Ctrl+C, then run this again. This script starts "
              "the controller by itself.")
        sys.exit(1)
    userspace = args.userspace or not topo.kernel_ovs_ok()
    if userspace:
        say("using the userspace switch (no Open vSwitch kernel module)")

    setLogLevel("warning")
    os.makedirs(RUNS, exist_ok=True)
    out_path = os.path.join(RESULTS, "results.json")
    try:
        with open(out_path) as f:
            results = json.load(f)
        if not isinstance(results, dict):
            results = {}
    except (OSError, ValueError):
        results = {}
    results.setdefault("info", {})
    results["info"].update({"date": time.strftime("%Y-%m-%d %H:%M"), "quick": args.quick,
                            "userspace": userspace})

    started = time.time()
    failed = []
    for mode in modes:
        with Controller(mode):
            for test in tests:
                rows = None
                for attempt in (1, 2):          # if something odd happens, clean up and retry once
                    say("=== %s mode: %s test%s ===" % (mode, test,
                                                       " (second try)" if attempt == 2 else ""))
                    try:
                        rows = RUNNERS[test](mode, args.quick, userspace)
                        break
                    except Exception as e:      # one broken test must not stop the others
                        say("  !!! %s test had a problem: %s" % (test, e))
                        traceback.print_exc()
                        cleanup()
                if rows is None:
                    failed.append("%s/%s" % (mode, test))
                    continue
                results.setdefault(mode, {})[test] = rows
                with open(out_path + ".tmp", "w") as f:
                    json.dump(results, f, indent=1)
                os.replace(out_path + ".tmp", out_path)
    topo.remove_old_qos()
    say("done in %.1f minutes. Results: results/results.json" % ((time.time() - started) / 60))
    say("next:  python3 plot.py")
    if failed:
        say("these runs had a problem: %s" % ", ".join(failed))
        sys.exit(1)


if __name__ == "__main__":
    main()
