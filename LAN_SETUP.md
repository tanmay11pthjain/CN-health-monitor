# Real deployment on 3 laptops (real hardware data)

```
  CLIENT laptop 1                CLIENT laptop 2
  agent.py (psutil)              agent.py (psutil)
       |  UDP 5000 reports (1/s)       |
       |  UDP 5001 alerts <-> ACK      |
       |  UDP 5001 ping <-> ACK (RTT)  |
       +-------- same Wi-Fi / LAN -----+
                       |
              MONITOR laptop
              monitor.py --lan
              UDP 5000 / 5001 / 5002 (discovery)
              web dashboard  http://<monitor IP>:8080
```

The two clients send the **real** CPU, memory, process count, network upload/download, disk, swap, temperature (Linux/macOS), battery, uptime and top processes of their own laptop every second. All three laptops must be on the **same Wi-Fi or router**. A phone hotspot works well. College Wi-Fi often blocks laptops from talking to each other ("client isolation").

## Monitor laptop (do this first)

1. **Install Python 3.10 or newer** from https://www.python.org/downloads/. On the first installer screen, tick **"Add python.exe to PATH"**.
2. **Copy the project folder** (`cn-health-monitor`) to the laptop, e.g. `C:\Users\<you>\Desktop\CN\cn-health-monitor`.
3. Open **Command Prompt** in that folder (in File Explorer, type `cmd` in the address bar and press Enter), then run:
   ```
   python -m pip install psutil
   ```
4. **Open the firewall.** Right-click `firewall_windows.bat` and choose **Run as administrator**. It opens UDP 5000-5002 and TCP 8080.
   (Linux: `sudo ufw allow 5000:5002/udp && sudo ufw allow 8080/tcp`. macOS: click "Allow" when asked.)
5. **Start the monitor:**
   ```
   python monitor.py --lan
   ```
   If Windows shows a "Windows Defender Firewall" popup, tick **both Private and Public** and click **Allow access**.
   The monitor prints its IP address, for example:
   ```
    This laptop's IP address(es): 192.168.1.20
    On each client laptop run:   python agent.py --monitor 192.168.1.20
    Live dashboard:              http://192.168.1.20:8080
   ```
   Write this IP down. (You can also find it with `ipconfig`: look for "IPv4 Address" under your Wi-Fi adapter.)
6. Open **http://localhost:8080** in a browser on the monitor laptop. It says "Waiting for agents...".

Optional: add `--table` to also see a live table in the terminal: `python monitor.py --lan --table`.

## Each client laptop (do this on both)

1. Install Python (same as monitor step 1).
2. Copy the same project folder.
3. In Command Prompt in that folder: `python -m pip install psutil`
4. Check you are on the **same Wi-Fi** as the monitor.
5. **Start the agent**, using the monitor's IP:
   ```
   python agent.py --monitor 192.168.1.20
   ```
   The agent uses the laptop's own name (hostname). To pick a name: `python agent.py --monitor 192.168.1.20 --name client1`
6. **Check that it works.** Within 2 seconds the agent prints:
   ```
   monitor 192.168.1.20 answered, round trip 4.2 ms. Sending reports...
   ```
   The laptop also appears as a card on the dashboard. If you see `WARNING: no answer from the monitor` instead, see Troubleshooting below.

No firewall change is needed on the clients. The agent only sends, and Windows lets the replies (ACKs) come back in.

Bonus: `python agent.py` with no IP finds the monitor by itself with a UDP broadcast on port 5002. Some Wi-Fi networks block broadcasts, so for the demo it is safer to give `--monitor <IP>`.

## Demo script (real events)

| Show | Do this | What you see |
|---|---|---|
| Live real data | open some YouTube / Chrome tabs on a client | CPU, memory and download change on its card |
| CPU alert | on a client: `python stress.py cpu 30` | CRIT cpu alert within 1-2 s, ACKed; CLEAR after 30 s |
| Memory alert | on a client: `python stress.py mem 85 30` | WARN mem alert (capped at 92 %, memory is freed afterwards) |
| Failure detection | close the agent window, or turn off the client's Wi-Fi | card turns red **DOWN** after 3 s |
| Recovery | start the agent again / Wi-Fi back on | `UP ... is back` |
| Packet loss | walk far from the router | "Lost" counter and RTT go up |
| Flood protection | on a client: `python agent.py --monitor <IP> --name bad --flood 300 --seconds 10` | monitor logs FLOOD and ignores that IP for 30 s |

Logs are in `logs/` on the monitor: `reports.csv`, `alerts.csv`, `events.txt`.

## Troubleshooting

- **"no answer from the monitor"**:
  1. Check the IP. The monitor's IP can change after a Wi-Fi reconnect.
  2. The monitor laptop's firewall. Run `firewall_windows.bat` as administrator. If you ever clicked **Cancel** on the firewall popup, Windows created a BLOCK rule for Python, and a block beats an allow. Fix: Windows Security → Firewall & network protection → *Allow an app through firewall* → Change settings → tick Private and Public for every **Python** entry.
  3. Same network? Phone hotspot or home router work. College/guest Wi-Fi may isolate clients.
  4. `ping` is not a good test, because Windows blocks ping by default even when everything works. The agent's own check is the real test.
- **"UDP port 5000 is already in use"**: a monitor is already running. Close the other window.
- **`python` not found**: reinstall Python with "Add to PATH" ticked, or use `py` instead of `python`.
- **Temperature shows n/a on Windows**: Windows does not give CPU temperature to normal programs. Linux and macOS laptops show it.
- **Dashboard from another laptop**: open `http://<monitor IP>:8080`. It needs TCP 8080 open (the .bat does that).

## Settings (`config.json` → `"real"`)

- `monitor_ip`: `"auto"` (broadcast discovery) or a fixed IP like `"192.168.1.20"`. Then clients can run just `python agent.py`.
- `web_port`: dashboard port (8080). `discovery_port`: 5002.
- `limits`: the alert limits for real laptops. CPU and memory are the same as the Mininet ones (75/90, 80/90). Processes (450/600) and network (20/50 Mbps) are higher, because a normal Windows laptop already runs about 250-350 processes. `disk` and `temp_c` are new.
- `report_every`, `down_after`, `ack_wait`, `max_tries` at the top are shared with Mininet mode.

The Mininet + SDN mode still works exactly as before (`guide.md`). It now starts the agents with `--sim`.
