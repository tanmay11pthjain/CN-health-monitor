# How to Run the Project (step by step)

This guide is for someone who has never done networking before. Do the steps in order and type the commands exactly as shown.

---

## The idea in one minute

Think of a school with 4 classrooms. Every second, each classroom sends a small note to the principal's office: "Room 2 here, all fine." If something goes wrong in a room, it sends an urgent note. If a room stops sending notes, the principal knows something is wrong.

In our project:

- the classrooms are 4 fake servers called **h1, h2, h3, h4**
- the notes are **UDP messages**
- the principal's office is the **monitor**, on **h5**
- the corridor that carries the notes is a **switch** called **s1**
- the person who controls the corridor is the **SDN controller**. It lets urgent notes go first.
- **h6** is a noisy room that blocks the corridor with lots of useless paper (a traffic jam)

---

## Step 1: Get an Ubuntu computer

You need Ubuntu 22.04. If you don't have it, make a virtual machine (a computer inside your computer):

1. Download and install **VirtualBox**: https://www.virtualbox.org/wiki/Downloads
2. Download **Ubuntu 22.04 Desktop** (the .iso file): https://releases.ubuntu.com/22.04/
3. In VirtualBox click **New**. Give it a name, choose the Ubuntu .iso file, and tick **Skip Unattended Installation**.
4. Give it **4096 MB** memory, **2** CPUs and a **25 GB** disk. Click **Finish**, then **Start**.
5. Install Ubuntu with the normal options. Remember your password.

## Step 2: Open the Terminal

Press **Ctrl + Alt + T**. A window opens where you type commands.

- Type a command and press **Enter**.
- To paste in the Terminal use **Ctrl + Shift + V**.
- When it asks for your password, type it and press Enter. Nothing shows on the screen while you type. That is normal.

## Step 3: Get the project

```
sudo apt update
sudo apt install -y git
cd ~
git clone <link-to-our-github-repo> CN
cd CN
```

Type `ls` and press Enter. You should see files like `agent.py`, `monitor.py`, `controller.py` and `topo.py`.

(No GitHub link yet? Copy the `CN` folder into your Home folder with the Files app, then type `cd ~/CN`.)

## Step 4: Install everything (only the first time)

```
cd ~/CN
bash setup.sh
```

It asks for your password once. This takes 5 to 15 minutes. At the end it prints `SETUP DONE`.

---

## Step 5: Start the project

You need **3 Terminal windows**. Open each one with **Ctrl + Alt + T**.

**Window 1: start the controller**

```
cd ~/CN
sudo mn -c
./venv/bin/osken-manager controller.py
```

You should see `controller started, mode = smart`. Leave this window running. Don't close it.

When the network starts (next step) this window shows `switch s1 connected`.

**Window 2: start the network**

```
cd ~/CN
sudo python3 topo.py
```

After a few seconds you see `mininet>`. This means the network is ready and the monitor and the 4 agents have started by themselves.

**Window 3: watch the monitor**

```
cd ~/CN
watch -n 1 cat logs/screen.txt
```

You should see a table like this, changing every second:

```
HEALTH MONITOR   09:18:07   controller mode: smart   running 6 s

NODE   STATUS  LEVEL    CPU%   MEM%  PROCS  NET(kbps)  LAST SEEN   LOST  ALERTS
----------------------------------------------------------------------------------
h1     UP      OK       26.9   51.1    178        1.5       0.5s      0       0
h2     UP      OK       30.4   50.7    180        1.5       0.5s      0       0
h3     UP      OK       29.6   49.3    178        1.5       0.5s      0       0
h4     UP      OK       27.5   50.6    183        1.5       0.5s      0       0

Recent events:
  09:18:01  monitor started (reports on 5000, alerts on 5001)
  09:18:01  SDN   controller: hello monitor, mode=smart  (3.6 ms)
  09:18:02  NEW   h1 joined from 10.0.0.1
```

If you see this, the project is working.

---

## Step 6: Try things

Type these in **Window 2** after `mininet>`, then look at **Window 3**.

| Type this | What happens |
|---|---|
| `pingall` | checks that every host can reach every other host. You want `0% dropped` |
| `spike h2` | h2's CPU goes up. First a WARN alert, then a CRIT alert, each with its latency (how fast it arrived) |
| `spike h3 mem` | same, but for memory. You can also use `procs` |
| `calm h2` | h2 goes back to normal and you see a CLEAR message |
| `fail h4` | turns h4's agent off. After about 3 seconds h4 shows **DOWN**, and the controller replies that it removed h4's rules |
| `recover h4` | h4 comes back **UP** |
| `screen` | prints the monitor table once inside this window |
| `dpctl dump-flows -O OpenFlow13` | shows the rules inside the switch. Look for `tp_dst=5001` with `set_queue:1`. That is the fast lane for alerts |
| `help` | lists all commands |

More commands (`congest`, `flood`, `loss`, `cutlink`) come in Phase 2.

### Try the simple mode (the normal switch, without the fast lane)

Stop everything (Step 7). In Window 1 start the controller like this instead:

```
HMON_MODE=simple ./venv/bin/osken-manager controller.py
```

Then do Window 2 and 3 again. The monitor table now says `controller mode: simple`, and `dpctl dump-flows` has no `set_queue:1` rule.

---

## Step 7: Stop everything

1. In Window 2 type `exit`
2. In Window 1 and Window 3 press **Ctrl + C**
3. Clean up:

```
sudo mn -c
```

Always do this before starting again.

---

## Step 8: Run the tests

**Test 1: quick tests (no network needed)**

```
cd ~/CN
python3 test_basic.py
```

At the end you should see `Ran 11 tests` and `OK`.

**Test 2: the full system inside Mininet**

Start the controller in Window 1 (Step 5). Then in Window 2:

```
cd ~/CN
sudo mn -c
sudo python3 topo.py --test
```

It builds the network, does everything by itself (ping, spike, fail, recover) and prints:

```
  [PASS] every host can reach every other host (pingall) (0% lost)
  [PASS] all 4 agents are UP on the monitor
  [PASS] monitor and controller talk on port 9999
  [PASS] CPU spike on h2 gives a CRITICAL alert
  [PASS] stopped agent h4 is marked DOWN
  [PASS] controller confirms h4 is down
  [PASS] h4 comes back UP after recover
  [PASS] smart mode: alert fast lane rule is in the switch
  [PASS] reports received with little loss (received 45, lost 0)

*** 9 of 9 tests passed
```

## Step 9: Look at the saved data

Everything the monitor sees is saved in the `logs` folder:

| File | What is inside |
|---|---|
| `logs/reports.csv` | every report: time, node, CPU, memory, processes, network, latency |
| `logs/alerts.csv` | every alert, DOWN and UP, with latency and how many tries it needed |
| `logs/events.txt` | the same events you see at the bottom of the table |
| `logs/agent_h1.txt` | what agent h1 printed |

You can open the `.csv` files with LibreOffice Calc (double-click them in the Files app).

Graphs and experiments come in Phase 2.

---

## If something goes wrong

| Problem | Fix |
|---|---|
| `Permission denied` | put `sudo` in front of the command |
| `File exists` or `Error creating interface` | type `exit` in Mininet, run `sudo mn -c`, try again |
| `pingall` shows `100% dropped` | the controller is not running. Start Window 1 first, then Window 2 |
| `The switch could not reach the controller` | same: start the controller in Window 1 first |
| `failed to create datapath` or the switch never connects | your system has no Open vSwitch kernel module. Use `sudo python3 topo.py --userspace` |
| table in Window 3 is empty | wait 5 seconds. If still empty, stop everything (Step 7) and start again |
| `Address already in use` | run `sudo mn -c` and start again |
| still not working | restart the virtual machine and start from Step 5 |

---

## Words you will hear

- **UDP**: a quick way to send messages. It doesn't check if they arrived.
- **IP address**: the address of a computer, like `10.0.0.1`
- **Port**: a door number on a computer. Normal reports use door 5000, alerts use door 5001.
- **Switch**: a box that passes messages to the right computer
- **SDN controller**: a program that tells the switch what to do
- **Mininet**: a program that makes a fake network inside one computer
- **Latency**: how long a message takes to arrive
- **ACK**: a "got it" reply
