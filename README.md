# Remote System Health Monitoring (UDP + SDN)

Computer Networks mini project: SDN-Socket Programming.

Servers send their health (CPU, memory, processes, network) every second over **UDP** to one central monitor. The monitor shows a live table, raises alerts when a value crosses a limit, and marks a server DOWN when it stops talking. The network is built in **Mininet** and controlled by an **SDN controller** (os-ken, OpenFlow 1.3). The controller gives alert packets a fast lane and talks to the monitor over UDP.

- How to run it step by step: [guide.md](guide.md)
- Full plan and rubric checklist: [PLAN.md](PLAN.md)

Status: **Phase 1 (D1) done.** Phase 2 (smart controller reactions, hard scenarios, experiments) is next.

---

## Objectives

1. Agents on every server collect statistics and send them over UDP.
2. A central monitor receives them, checks alert limits, and finds failed servers.
3. Critical alerts get priority in the network.
4. The SDN controller and the monitor program work together.
5. Measure alert latency and how the system scales (Phase 2).

## Architecture

```
            [ SDN controller ]   controller.py (os-ken, OpenFlow 1.3)
                    |
               [ switch s1 ]     Open vSwitch, 2 queues on every port
     _______________|________________________
    |      |      |      |       |          |
   h1     h2     h3     h4      h5         h6
  agent  agent  agent  agent  MONITOR    noisy host (Phase 2)
 10.0.0.1 ...         10.0.0.4 10.0.0.5   10.0.0.6
```

| Part | File | Job |
|---|---|---|
| Agent (client) | `agent.py` | makes CPU / memory / process numbers for its server, reads real network traffic, sends a REPORT every second, sends an ALERT when a limit is crossed and resends it until it gets an ACK |
| Monitor (server) | `monitor.py` | listens on UDP 5000 (reports) and 5001 (alerts) with one thread each, keeps the node table, checks limits, marks DOWN after 3 s of silence, writes `logs/` |
| SDN controller | `controller.py` | learning switch, alert fast lane, answers the monitor on port 9999, tells the monitor when a cable goes down |
| Network | `topo.py` | builds the Mininet network, sets up switch queues, starts everything, adds our commands |
| Shared | `common.py`, `config.json` | message format and limits |

## Communication flow

```
agent  --REPORT (UDP 5000, every 1 s)------------------------->  monitor
agent  --ALERT  (UDP 5001, marked urgent: DSCP 46)------------>  monitor
agent  <--ACK---------------------------------------------------  monitor
         (no ACK in 0.5 s -> send again, wait 1 s, then 2 s ...)

monitor --NODE_DOWN / NODE_UP / HELLO (UDP to 10.0.0.254:9999)-->  switch --packet-in-->  controller
monitor <--CTRL_REPLY / NET_EVENT---------------------------------  switch <--packet-out-- controller
```

`10.0.0.254` is a fake address. A rule in the switch sends every packet for `10.0.0.254:9999` up to the controller, and the controller also answers ARP for that address. So the monitor talks to the controller with a normal UDP socket.

## Message format

Every message is one UDP datagram with a small JSON object (always under 1200 bytes, so it never gets split).

```json
{"type": "REPORT", "node": "h1", "seq": 57, "time": 1730275200.123, "boot": 1730275100000,
 "cpu": 34.5, "mem": 51.2, "procs": 180, "net_kbps": 120.0}

{"type": "ALERT", "node": "h2", "seq": 3, "time": 1730275201.456, "first_sent": 1730275201.456,
 "tries": 1, "metric": "cpu", "value": 95.0, "level": "CRIT", "limit": 90}

{"type": "ACK", "node": "monitor", "seq": 3, "time": 1730275201.457}
```

- `seq` goes up by one for every report. A gap means a report was lost. An old or repeated number is ignored.
- `boot` changes when an agent restarts, so the monitor starts counting again.
- `time` / `first_sent` give the latency. All Mininet hosts share one clock, so latency = arrival time - send time.
- Broken packets (not JSON, missing fields, too big) are counted and skipped. They never crash the monitor.

## Alert limits (`config.json`)

| Metric | WARN | CRIT |
|---|---|---|
| CPU % | 75 | 90 |
| Memory % | 80 | 90 |
| Processes | 300 | 400 |
| Network kbps | 5000 | 8000 |
| Silence | | 3 s, then DOWN |

The agent sends an alert every time a metric changes level (OK -> WARN -> CRIT and back). The monitor checks every report against the same limits itself, so it doesn't depend on the agent alone.

## SDN part

Flow tables in switch s1:

| Table | Priority | Match | Action |
|---|---|---|---|
| 0 | 100 | UDP to 10.0.0.254:9999 | send to controller |
| 0 | 100 | ARP for 10.0.0.254 | send to controller |
| 0 | 50 | UDP port 5001 (to or from), **smart mode only** | put in queue 1, go to table 1 |
| 0 | 0 | anything | go to table 1 |
| 1 | 10 | in_port + source MAC + destination MAC (learned) | output to the right port |
| 1 | 0 | anything | send to controller |

Every switch port has two queues (Linux HTB): queue 0 for normal traffic and queue 1 for alerts, with 8 of the 10 Mbit/s guaranteed.

Controller modes:
- **simple**: plain learning switch (our baseline for comparison)
- **smart**: learning switch + alert fast lane. Phase 2 adds traffic watching, slowing down a noisy host, and blocking a flooding host.

The monitor and the controller work together: when the monitor marks a server DOWN, it tells the controller, and the controller deletes that server's flow rules and replies. When a switch port goes down, the controller tells the monitor.

## How to run (short version)

```
bash setup.sh                                  # once
./venv/bin/osken-manager controller.py         # window 1
sudo python3 topo.py                           # window 2
watch -n 1 cat logs/screen.txt                 # window 3
```

Commands at the `mininet>` prompt: `spike h2 [cpu|mem|procs]`, `calm h2`, `fail h4`, `recover h4`, `screen`, plus all normal Mininet commands.

Simple (baseline) mode: `HMON_MODE=simple ./venv/bin/osken-manager controller.py`

## Tests

| Test | Command | What it checks |
|---|---|---|
| Unit + local tests | `python3 test_basic.py` | message format, broken packets, limits, resend until ACK, giving up, lost / repeated packets, real monitor + agents on 127.0.0.1, DOWN and back UP (11 tests) |
| Full system | `sudo python3 topo.py --test` | pingall, all agents UP, monitor and controller talking, CPU alert, DOWN detection, controller confirms, recovery, fast lane rule, report loss (9 checks) |

Results from our run (Ubuntu 24.04, Mininet 2.3, OVS 3.3): 11/11 unit tests OK, 9/9 system checks passed in smart mode and in simple mode. Alert latency in the normal case was about 0.5 to 1.5 ms. Detection of a stopped agent took 3.0 s (the limit we set).

## Logs

| File | Content |
|---|---|
| `logs/screen.txt` | the live table |
| `logs/status.json` | the same table as data (used by the tests) |
| `logs/reports.csv` | every report with latency and lost count |
| `logs/alerts.csv` | alerts, DOWN, UP and network events with latency and tries |
| `logs/events.txt` | event list |

## Software

Ubuntu 22.04 / 24.04, Mininet 2.3, Open vSwitch, Python 3.10+, os-ken 3.1.1 (Ryu's maintained fork, since Ryu no longer installs on new Python).
