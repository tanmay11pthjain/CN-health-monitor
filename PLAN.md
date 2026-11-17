# Project Plan: Remote System Health Monitoring

Topic: Remote System Health Monitoring
Protocol: UDP
Tools: Python, Mininet, os-ken (SDN controller), Ubuntu VM

---

## 1. What are we making?

We have a few servers. We want to know if they are healthy.

Every server runs a small program called an **agent**. Every 1 second the agent sends a short message to one central computer called the **monitor**. The message says:

- how busy the CPU is
- how much memory is used
- how many programs (processes) are running
- how much network traffic there is

The monitor reads these messages and shows them in a table. If a number goes above a limit (for example CPU above 90%), the monitor shows an **alert**. If a server stops sending messages, the monitor marks it as **DOWN**.

The messages travel over **UDP**. UDP is fast and simple. It just sends the message and does not wait for a reply.

The network is made in **Mininet** (a fake network inside one computer). The switch in the network is controlled by an **SDN controller**. The controller makes sure alert messages always get through first, even when the network is busy.

---

## 2. The network

We keep it small: one switch and six hosts.

```
            [ SDN Controller ]
                    |
                [ switch s1 ]
    ________________|________________________
   |      |      |      |         |          |
  h1     h2     h3     h4        h5         h6
 agent  agent  agent  agent    MONITOR    noisy host
                                         (makes traffic jam)
```

- **h1, h2, h3, h4** are the servers. Each runs `agent.py`.
- **h5** is the monitor. It runs `monitor.py`.
- **h6** is a "noisy" host. It sends a lot of useless data to create a traffic jam, so we can test what happens when the network is busy.
- **s1** is the switch. The controller tells it what to do.

---

## 3. The messages

Every message is a small piece of text in JSON format. Easy to read and print.

Normal report (sent every 1 second to port **5000**):

```json
{"type": "REPORT", "node": "h1", "seq": 57, "time": 1730275200.123,
 "cpu": 34.5, "mem": 51.2, "procs": 180, "net_kbps": 120}
```

Alert (sent to port **5001** when something is wrong):

```json
{"type": "ALERT", "node": "h2", "seq": 58, "time": 1730275201.456,
 "metric": "cpu", "value": 95.0, "limit": 90}
```

Reply from the monitor for an alert:

```json
{"type": "ACK", "node": "h2", "seq": 58}
```

Why these fields:
- **seq** (sequence number) counts up by 1 every message. If the monitor sees 57 and then 59, it knows message 58 was lost.
- **time** is when the agent sent it. The monitor subtracts it from the time the message arrived, which gives the **latency** (how long it took). All Mininet hosts use the same clock, so this number is correct.
- Alerts need an **ACK**. UDP can lose messages, so if the agent gets no ACK within 0.5 seconds, it sends the alert again (up to 3 times). Normal reports don't need this, because a new report comes next second anyway.

Two ports (5000 for normal, 5001 for alerts) make it easy for the switch to tell important messages from normal ones.

---

## 4. Alert limits

These are kept in `config.json` so they are easy to change.

| What | Warning | Critical |
|---|---|---|
| CPU | 75% | 90% |
| Memory | 80% | 90% |
| Processes | 300 | 400 |
| Network | 5000 kbps | 8000 kbps |
| No message received | 3 seconds = DOWN | |

---

## 5. Where do the numbers come from?

In Mininet, all hosts run on the same real computer, so they would all show the same CPU and memory. That would be boring. So each agent **simulates** a server: it makes believable numbers that go up and down a bit. We can make a server "busy" with a command (`spike h2`), and its CPU jumps to around 95%.

Network traffic numbers are real. Each Mininet host has its own network card, and the agent reads it from `/proc/net/dev`.

---

## 6. What the SDN controller does

The controller runs `controller.py` (written with os-ken). It has two modes, so we can compare them.

**Simple mode (our baseline):**
- Works like a normal switch. It learns which host is on which port and forwards packets.
- All packets are treated the same.

**Smart mode (our project):**
1. **Priority for alerts.** The controller marks packets going to (or coming from) port 5001 as urgent (DSCP 46) inside the switch. Every switch port has two lanes, and the fast lane only takes urgent packets, so alerts never wait behind normal traffic. All other packets get their mark cleared, so no host can cheat.
2. **Watching traffic.** Every 2 seconds the controller asks the switch how much data each port is sending. If one host sends too much (the noisy host), the controller slows that host down with an OpenFlow meter (a speed limit in the switch). When it calms down, the limit is removed.
3. **Blocking a bad host.** If the monitor sees a host sending far too many messages (flooding), it asks the controller to block it. The controller adds a "drop" rule for that host. The rule removes itself after 30 seconds and the controller tells the monitor.
4. **Talking to the monitor.** The monitor sends its requests to the controller as normal UDP messages to port 9999. The switch passes these to the controller. The controller replies and also tells the monitor about network events ("h6 slowed down", "h3 blocked"). This is how our socket program and the SDN network work together.

---

## 7. Files

```
CN/
  README.md          what the project is (for GitHub)
  PLAN.md            this plan
  guide.md           step by step: how to run it
  setup.sh           installs everything
  config.json        ports, alert limits
  requirements.txt   what the controller needs (os-ken)
  common.py          small helpers shared by agent and monitor (make / read messages)
  agent.py           runs on each server, sends reports and alerts
  monitor.py         central monitor, shows table and alerts
  controller.py      SDN controller (simple mode and smart mode)
  topo.py            creates the Mininet network + our extra commands
  noise.py           the noisy host's traffic jam (plain Python, no iperf needed)
  test_basic.py      quick tests
  experiments.py     runs the tests for the graphs
  plot.py            draws the graphs
  logs/              monitor saves its data here
  results/           graphs go here
```

Only a handful of code and config files. The agent and the monitor use only plain Python (no extra installs needed).

---

## 8. Phase 1 (first submission, D1, 15 marks)

Goal: the basic system works from start to end.

1. Write `config.json` with ports and limits.
2. Write `agent.py`: sends a report every second, sends an alert with resend when a limit is crossed.
3. Write `monitor.py`: listens on ports 5000 and 5001, shows a live table, prints alerts with latency, marks a server DOWN after 3 seconds of silence, sends ACKs, saves a log file.
4. Write `topo.py`: makes the network in section 2, starts the monitor and the agents automatically, and adds our commands (`spike`, `calm`, `fail`, `recover`).
5. Write `controller.py` simple mode, plus the alert fast lane.
6. Connect the monitor to the controller on port 9999 (monitor tells the controller when a server goes DOWN, and the controller replies).
7. Test: `pingall` works, all 4 servers show in the table, an alert shows up, a stopped server shows DOWN.
8. Write `setup.sh`, `guide.md`, and the README.

**Phase 1 demo:** start everything, show the table, make h2 busy and show the alert, turn off h4 and show DOWN, show the switch rules.

How this matches the marks:

| Rubric | What we show |
|---|---|
| Problem understanding and architecture (2) | Sections 1 to 6 and the network picture |
| UDP socket code (4) | agent.py and monitor.py: two ports, threads, ACK and resend, lost-message count |
| Mininet + SDN (4) | topo.py and controller.py, switch rules |
| Socket and SDN together (3) | monitor talks to the controller on port 9999, alert fast lane |
| Testing and demo (2) | test_basic.py and the demo above |

---

## 9. Phase 2 (final submission, D2, 25 marks)

Status: **built and tested.** Results are in the README and in `results/`.

Goal: make the network smart, test hard situations, and measure everything.

1. Add smart mode to the controller: traffic watching, slowing down the noisy host, blocking a flooding host.
2. Add more commands to `topo.py`:
   - `congest on/off` makes a traffic jam from h6
   - `flood h3` makes h3 send too many messages
   - `loss 10` makes 10% of packets get lost
   - `cutlink h4` / `fixlink h4` unplugs a server's cable
3. Run the experiments in simple mode and smart mode:

| Test | What we change | What we measure |
|---|---|---|
| Traffic jam | no jam vs heavy jam | how long alerts take to arrive |
| Server failure | stop an agent | how long until the monitor says DOWN |
| Packet loss | 0%, 5%, 10%, 20% lost | how many alerts still arrive (thanks to resend) |
| More servers | 4, 10, 20, 50 agents | lost messages, latency, monitor CPU use |
| Flooding host | one host floods | time until it gets blocked |

4. Draw graphs with `plot.py`.
5. Finish the README with results, and prepare for the viva.

What we expect: in simple mode, alerts get stuck behind the traffic jam and arrive late or get lost. In smart mode they still arrive quickly.

| Rubric | What we show |
|---|---|
| Full functionality (4) | everything in the brief works |
| Dynamic SDN (6) | controller reacts by itself: priority, slowing down, blocking |
| Hard situation (4) | traffic jam, packet loss, server failure, flooding |
| Performance and comparison (5) | graphs: simple mode vs smart mode |
| Documentation, demo, viva (6) | README, guide.md, demo, section 11 below |

---

## 10. Rubric checklist (every point the teacher checks)

We tick these before each submission.

**D1 (15 marks)**

| Rubric item | Marks | What the rubric asks | Where we cover it |
|---|---|---|---|
| Problem understanding and architecture | 2 | objectives, architecture, components, communication flow, expected network behaviour | Sections 1, 2, 3, 6 and the network picture in the README |
| TCP/UDP socket implementation | 4 | low-level sockets, client/server, datagram handling, concurrency, error handling, message exchange | agent.py (client) and monitor.py (server) use plain `socket`. Two ports, one thread per port, ACK and resend, bad messages are skipped and counted, timeouts handled |
| Mininet + SDN implementation | 4 | correct topology, working controller, flow rules, host/switch connectivity | topo.py (6 hosts, 1 switch), controller.py installs flow rules, `pingall` works, `dpctl dump-flows` shows the rules |
| Socket and SDN integration | 3 | the app and the network affect each other, end to end | the monitor tells the controller when a server is DOWN and the controller replies (port 9999); alerts go in the fast lane |
| Initial testing and demo | 2 | basic end to end works, test cases shown | test_basic.py and the Phase 1 demo |

**D2 (25 marks)**

| Rubric item | Marks | What the rubric asks | Where we cover it |
|---|---|---|---|
| Complete application functionality | 4 | all project requirements work, many clients | all 8 points of the brief (agents, statistics, monitor, limits, priority, failures, latency, scalability) with 4 to 50 agents |
| Dynamic SDN functionality | 6 | controller reacts by itself: routing, filtering, prioritization, monitoring, blocking, recovery | smart mode: fast lane for alerts (prioritization), traffic watching every 2 s (monitoring), slowing the noisy host (filtering), blocking a flooding host (blocking), removing limits and unblocking when things are normal again (recovery) |
| Challenging scenario / robustness | 4 | congestion, packet loss, link failure, overload, service failure | traffic jam, 5 to 20% packet loss, cut cable, server crash, flooding host, monitor restart (agents keep going) |
| Performance evaluation and comparison | 5 | metrics, many conditions, clear results, compared to a baseline | 5 tests in section 9, simple mode (baseline) vs smart mode, graphs and a results table |
| Documentation, final demo and viva | 6 | full docs, reproducible demo, each member explains | README, PLAN.md, guide.md, code comments, GitHub repo, section 11 answers |

**From the "Mandatory Requirements" list:** UDP sockets (yes), client and server parts (agent and monitor), many clients (4 to 50 agents), runs in Mininet (yes), app and SDN work together (port 9999 and fast lane), Mininet + controller + sockets together (yes), we give topology, code, config, tests and steps to repeat (topo.py, all code, config.json, test_basic.py, guide.md), and everything goes on GitHub.

---

## 11. How to explain it in the viva (short answers)

**What does your project do?**
Servers send their health (CPU, memory, processes, network) every second over UDP to one monitor. The monitor shows alerts when something is wrong. An SDN controller makes sure alerts get through first.

**Why UDP and not TCP?**
Health reports are small and come every second. If one is lost, the next one comes soon, so we don't need TCP's connection and waiting. UDP is faster and lighter. For alerts, which matter more, we added our own ACK and resend.

**How do you know a server is down?**
If the monitor gets no message from it for 3 seconds, it marks it DOWN.

**How do you measure latency?**
The agent puts the send time in the message. The monitor subtracts it from the arrival time. All Mininet hosts share one clock, so it's accurate.

**What does SDN add?**
In a normal network, the switch treats all packets the same. With SDN, our controller program decides: alerts go in a fast lane, a host that sends too much gets slowed down, and a flooding host gets blocked. It does this by itself while the system runs.

**How do the monitor and the controller talk?**
The monitor sends a UDP message to port 9999. The switch sends it to the controller, and the controller replies the same way.

**What did you compare?**
Simple mode (normal switch) vs smart mode (our controller). Under a traffic jam, smart mode keeps alerts fast.

---

## 12. Software

- Ubuntu 22.04 in VirtualBox (4 GB RAM, 2 CPUs)
- Mininet and Open vSwitch (installed with apt)
- os-ken 3.1.1 for the controller. We use os-ken instead of Ryu because Ryu doesn't install on new Python versions anymore. os-ken is the same code, still maintained.
- Python 3, matplotlib for graphs, iperf for the traffic jam
