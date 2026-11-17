# Remote System Health Monitoring (UDP + SDN)

Computer Networks mini project: SDN-Socket Programming.

Servers send their health (CPU, memory, processes, network) every second over **UDP** to one central monitor. The monitor shows a live table, raises alerts when a value crosses a limit, and marks a server DOWN when it stops talking. The network is built in **Mininet** and controlled by an **SDN controller** (os-ken, OpenFlow 1.3). The controller gives alert packets a fast lane and talks to the monitor over UDP.

- How to run it step by step: [guide.md](guide.md)
- Full plan and rubric checklist: [PLAN.md](PLAN.md)

Status: **Phase 1 (D1) and Phase 2 (D2) done.** Smart controller (priority, traffic watching, slowing down, blocking, recovery), hard situations (traffic jam, packet loss, cut cable, server crash, flooding, monitor restart), and experiments comparing simple mode with smart mode. Results are below.

---

## Objectives

1. Agents on every server collect statistics and send them over UDP.
2. A central monitor receives them, checks alert limits, and finds failed servers.
3. Critical alerts get priority in the network.
4. The SDN controller and the monitor program work together.
5. The controller reacts by itself: slows down a host that jams the network, blocks a host that floods the monitor, and removes both again when things are normal.
6. Measure alert latency, failure detection, loss and scaling, in simple mode (baseline) and smart mode.

## Architecture

```
            [ SDN controller ]   controller.py (os-ken, OpenFlow 1.3)
                    |
               [ switch s1 ]     Open vSwitch, 2 queues on every port
     _______________|________________________
    |      |      |      |       |          |
   h1     h2     h3     h4      h5         h6
  agent  agent  agent  agent  MONITOR    noisy host
 10.0.0.1 ...         10.0.0.4 10.0.0.5   10.0.0.6
```

| Part | File | Job |
|---|---|---|
| Agent (client) | `agent.py` | makes CPU / memory / process numbers for its server, reads real network traffic, sends a REPORT every second, sends an ALERT when a limit is crossed and resends it until it gets an ACK |
| Monitor (server) | `monitor.py` | listens on UDP 5000 (reports) and 5001 (alerts) with one thread each, keeps the node table, checks limits, marks DOWN after 3 s of silence, writes `logs/` |
| SDN controller | `controller.py` | learning switch; smart mode: marks alerts urgent, watches traffic every 2 s, slows down a noisy host (meter), blocks a flooding host (drop rule), removes both later; answers the monitor on port 9999 and tells it about network events |
| Network | `topo.py` | builds the Mininet network, sets up the two lanes on every switch port, starts everything, adds our commands and the automatic test |
| Noisy host | `noise.py` | sends 20 Mbit/s of junk from h6 (traffic jam) |
| Experiments | `experiments.py`, `plot.py` | runs the 5 tests in both modes, draws the graphs |
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
- Broken packets (not JSON, missing fields, wrong types, NaN, too big, unknown metric) are counted and skipped. They never crash the monitor, and a broken alert gets no ACK.
- A jump of more than 1000 in `seq` is treated as a reset, not as 1000 lost packets.
- The monitor accepts at most 500 node names, so junk names can't fill its memory.
- `config.json` is checked when every program starts. A wrong value stops it with a clear message (for example `limits.cpu: warn (95) must be smaller than crit (90)`). Missing Phase 2 values get defaults.

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

| Table | Priority | Match | Action | Mode |
|---|---|---|---|---|
| 0 | 100 | UDP to 10.0.0.254:9999 | send to controller | both |
| 0 | 100 | ARP for 10.0.0.254 | send to controller | both |
| 0 | 90 | IP from a flooding host | drop (removes itself after 30 s) | smart, added when the monitor asks |
| 0 | 50 | UDP port 5001 (to or from) | mark DSCP 46 (urgent), go to table 1 | smart |
| 0 | 41 / 40 | everything from the noisy host's port | meter (1 Mbit/s limit), go to table 1 | smart, added when a host sends > 4 Mbit/s |
| 0 | 1 | any other IP packet | clear the mark (DSCP 0), go to table 1 | smart |
| 0 | 0 | anything | go to table 1 | both |
| 1 | 10 | in_port + source MAC + destination MAC (learned) | output to the right port | both |
| 1 | 0 | anything | send to controller | both |

**Two lanes on every switch port** (Linux HTB set up by `topo.py`, 10 Mbit/s per cable): the fast lane gets 8 Mbit/s guaranteed and is served first, and only takes packets marked DSCP 46. Everything else uses the normal lane. Only the smart controller marks packets, and it clears the mark on everything else, so a host can't put itself in the fast lane. In simple mode nothing is marked, so alerts wait in the same queue as the junk.

**What smart mode does by itself:**

| Rubric word | What happens | How |
|---|---|---|
| prioritization | alerts and their ACKs use the fast lane. The controller's own messages to the monitor too | set_field DSCP 46 |
| monitoring | every 2 s the controller asks the switch for the bytes received on each port and works out each host's speed | OFPPortStatsRequest |
| filtering | a host above 4 Mbit/s for two checks in a row is limited to 1 Mbit/s | OpenFlow meter |
| blocking | the monitor sees a host sending more than 50 packets/s and asks for a block | drop rule with hard_timeout |
| recovery | the limit goes when the host stays under 0.5 Mbit/s for 3 checks. The block ends after 30 s (the switch tells us: FlowRemoved), and the monitor is told. A DOWN host's rules are removed and learned again when it comes back | |

The controller only takes orders from the monitor's IP, and refuses to block the monitor or the controller address.

**Simple mode** (the baseline) is a plain learning switch. It still talks to the monitor, but it doesn't mark, slow down or block anything.

## How to run (short version)

```
bash setup.sh                                  # once
./venv/bin/osken-manager controller.py         # window 1
sudo python3 topo.py                           # window 2
watch -n 1 cat logs/screen.txt                 # window 3
```

Commands at the `mininet>` prompt: `spike h2 [cpu|mem|procs]`, `calm h2`, `fail h4`, `recover h4`, `congest on|off`, `flood h3`, `loss 10`, `cutlink h4`, `fixlink h4`, `restartmon`, `screen`, plus all normal Mininet commands.

Experiments (stop the controller first, the script starts it in both modes by itself):

```
sudo python3 experiments.py        # about 20 minutes, or --quick for about 10
python3 plot.py                    # graphs + results/summary.md
```

Simple (baseline) mode: `HMON_MODE=simple ./venv/bin/osken-manager controller.py`

## Tests

| Test | Command | What it checks |
|---|---|---|
| Unit + local tests | `python3 test_basic.py` | 33 tests. Phase 1: message format, broken packets, limits, resend until ACK, giving up, lost / repeated packets, real monitor + agents on 127.0.0.1, DOWN and back UP. Phase 2: bad config files (13 kinds of mistakes), strange packets (NaN, bool, huge seq jump, deep JSON), broken alerts get no ACK, repeated alerts counted once, monitor restart, port already in use, flood detected and BLOCK sent to a fake controller, unblock, normal agents never counted as flooding, alert queue limit, flood and noise senders, graph code with missing data |
| Full system | `sudo python3 topo.py --test` | 23 checks in smart mode (17 in simple mode): pingall, all agents UP, monitor and controller talking, CPU alert, DOWN, controller confirms, recovery, fast lane rule, lanes on the switch port, alert during a traffic jam, h6 slowed down + meter rule + limit removed, flood noticed + h3 blocked + drop rule + h3 DOWN + h3 let in again, cut cable + fix, alerts at 20% packet loss, monitor restart |

Our run (Ubuntu 24.04, Mininet 2.3, OVS 3.3, os-ken 3.1.1): 33/33 unit tests OK, 23/23 system checks in smart mode and 17/17 in simple mode.

## Results (simple mode = baseline, smart mode = our controller)

Full tables: [results/summary.md](results/summary.md). Graphs: `results/1_jam_alert_latency.png` ... `results/5_flood.png`. Run them yourself with `sudo python3 experiments.py` and `python3 plot.py`.

| Test | Simple mode | Smart mode |
|---|---|---|
| Traffic jam (h6 sends 20 Mbit/s into a 10 Mbit/s cable): alert latency | mean **327.6 ms**, 95th pct 610.6 ms, max 1611.5 ms | mean **0.3 ms**, 95th pct 0.6 ms, max 0.8 ms. h6 was slowed down by itself |
| No jam: alert latency | 0.5 ms | 0.4 ms |
| Server failure: time until DOWN | stopped agent 2.05 s, cut cable 3.08 s | stopped agent 2.18 s, cut cable 3.08 s |
| Packet loss 5 / 10 / 20 %: alerts delivered | 100 / 100 / 100 % (first try only: 89 / 88 / 69 %) | 100 / 100 / 100 % (first try only: 93 / 86 / 77 %) |
| 4 / 10 / 20 / 50 agents | all UP, 0 % lost, latency about 0.5 ms, monitor CPU 0.3 to 0.8 % | same |
| Flooding host (300 packets/s for 15 s) | never blocked, 4500 of 4500 junk packets reached the monitor | blocked after **1.04 s**, only 300 reached the monitor, let in again after 30 s |

What it means:
- **The fast lane works.** In a traffic jam simple mode alerts wait behind the junk (about 1/3 s on average, up to 1.6 s). In smart mode alerts take under 1 ms, because they skip the queue and the noisy host is slowed down.
- **Failure detection** doesn't depend on the mode. It is about 2 to 3 s: the limit is 3 s of silence since the *last report*, and the last report came up to 1 s before the crash.
- **ACK + resend** keeps alert delivery at 100 % even at 20 % loss. Without resend only 69 to 77 % would arrive. Normal reports are not resent, so their loss matches the network loss.
- **Scaling:** 50 agents means 50 small packets per second, which is easy for the monitor (under 1 % CPU, nothing lost).
- **Blocking** cuts a flood off about 1 s after it starts. In simple mode nothing stops it.

These numbers come from Mininet with the OVS userspace switch (our test machine had no OVS kernel module). With the normal kernel switch on the Ubuntu VM the numbers will be a little different, but the comparison stays the same.

## Logs

| File | Content |
|---|---|
| `logs/screen.txt` | the live table |
| `logs/status.json` | the same table as data (used by the tests) |
| `logs/reports.csv` | every report with latency and lost count |
| `logs/alerts.csv` | alerts, DOWN, UP and network events with latency and tries |
| `logs/events.txt` | event list |
| `results/` | experiment numbers (`results.json`, `summary.md`) and graphs (`*.png`) |

## Software

Ubuntu 22.04 / 24.04, Mininet 2.3, Open vSwitch, Python 3.10+, os-ken 3.1.1 (Ryu's maintained fork, since Ryu no longer installs on new Python).
