# Reads REAL health numbers from the computer this runs on, using psutil.
# Works on Windows, Linux and macOS.
#
#   cpu       % of all CPU cores in use
#   mem       % of RAM in use
#   procs     number of running processes
#   net_kbps  network traffic in + out, kilobits per second (all real interfaces)
#   up_kbps / down_kbps   the same, split into sent and received
#   disk      % used of the system drive (C:\ on Windows, / on Linux/macOS)
#   swap      % of swap / page file in use
#   temp_c    hottest CPU temperature in degrees C   (only where the OS gives it)
#   battery   battery charge %, and "plugged" true/false   (laptops only)
#   uptime_h  hours since the computer started
#   top       the 3 processes using the most CPU right now
#
# Anything the computer can't tell us is sent as null (None), never a fake number.
#
# Try it:   python metrics.py

import os
import platform
import sys
import time

try:
    import psutil
except ImportError:          # agent.py prints a clear message about this
    psutil = None


def system_drive():
    if os.name == "nt":
        return os.environ.get("SystemDrive", "C:") + "\\"
    return "/"


def is_loopback(nic):
    n = nic.lower()
    return n == "lo" or n.startswith("lo0") or "loopback" in n


class RealMachine:
    def __init__(self):
        if psutil is None:
            raise RuntimeError("psutil is not installed. Run:  pip install psutil")
        self.cores = psutil.cpu_count() or 1
        self.mem_total_gb = round(psutil.virtual_memory().total / 2**30, 1)
        self.os = ("%s %s" % (platform.system(), platform.release()))[:30]
        self.drive = system_drive()
        self.last_net = None      # (time, bytes sent, bytes received)
        self.last_top = 0
        self.top = []
        # the first reading is meaningless (0 or 100), so take a short real one now
        psutil.cpu_percent(interval=0.5)
        self.read_net()

    def read_net(self):
        """kbps sent and received since the last call (loopback not counted)."""
        sent = recv = 0
        try:
            for nic, c in psutil.net_io_counters(pernic=True).items():
                if not is_loopback(nic):
                    sent += c.bytes_sent
                    recv += c.bytes_recv
        except (OSError, RuntimeError):
            return 0.0, 0.0
        now = time.time()
        up = down = 0.0
        if self.last_net is not None:
            t, s, r = self.last_net
            if now > t:
                # max(0, ...) because a counter can restart when Wi-Fi reconnects
                up = max(sent - s, 0) * 8 / 1000 / (now - t)
                down = max(recv - r, 0) * 8 / 1000 / (now - t)
        self.last_net = (now, sent, recv)
        return round(up, 1), round(down, 1)

    def temperature(self):
        if not hasattr(psutil, "sensors_temperatures"):
            return None          # Windows: psutil can't read sensors
        try:
            temps = psutil.sensors_temperatures()
        except (OSError, RuntimeError, AttributeError):
            return None
        best = None
        for entries in (temps or {}).values():
            for e in entries:
                if e.current and 0 < e.current < 150:
                    best = e.current if best is None else max(best, e.current)
        return None if best is None else round(best, 1)

    def battery(self):
        if not hasattr(psutil, "sensors_battery"):
            return None, None
        try:
            b = psutil.sensors_battery()
        except (OSError, RuntimeError, AttributeError):
            return None, None
        if b is None or b.percent is None:
            return None, None
        return round(b.percent, 1), bool(b.power_plugged) if b.power_plugged is not None else None

    def top_processes(self):
        """3 processes using the most CPU. Refreshed every 3 s (it costs a little time)."""
        now = time.time()
        if now - self.last_top < 3:
            return self.top
        self.last_top = now
        found = []
        # psutil remembers the process objects between calls, so cpu_percent
        # gives a real number from the second call on
        for p in psutil.process_iter(["name"]):
            try:
                if p.pid == 0:                 # "System Idle Process" on Windows
                    continue
                cpu = p.cpu_percent(interval=None) / self.cores
                if cpu > 0:
                    found.append((cpu, (p.info.get("name") or "?")[:24]))
            except (psutil.Error, OSError):
                continue
        found.sort(reverse=True)
        self.top = [[name, round(cpu, 1)] for cpu, name in found[:3]]
        return self.top

    def read(self):
        """All numbers for one REPORT."""
        up, down = self.read_net()
        try:
            disk = round(psutil.disk_usage(self.drive).percent, 1)
        except OSError:
            disk = None
        try:
            swap = round(psutil.swap_memory().percent, 1)
        except (OSError, RuntimeError):
            swap = None
        battery, plugged = self.battery()
        try:
            procs = len(psutil.pids())
        except OSError:
            procs = 0
        return {
            "cpu": round(psutil.cpu_percent(interval=None), 1),
            "mem": round(psutil.virtual_memory().percent, 1),
            "procs": procs,
            "net_kbps": round(up + down, 1),
            "up_kbps": up,
            "down_kbps": down,
            "disk": disk,
            "swap": swap,
            "temp_c": self.temperature(),
            "battery": battery,
            "plugged": plugged,
            "uptime_h": round((time.time() - psutil.boot_time()) / 3600, 1),
            "cores": self.cores,
            "mem_gb": self.mem_total_gb,
            "os": self.os,
            "top": self.top_processes(),
        }


if __name__ == "__main__":
    if psutil is None:
        print("psutil is not installed. Run:  %s -m pip install psutil" % sys.executable)
        sys.exit(1)
    m = RealMachine()
    print("Reading this computer every second. Press Ctrl+C to stop.")
    try:
        while True:
            time.sleep(1)
            print(m.read(), flush=True)
    except KeyboardInterrupt:
        pass
