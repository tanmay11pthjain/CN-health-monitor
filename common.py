# Small helpers used by agent.py, monitor.py, controller.py and the tests.
# Works on Windows, Linux and macOS (only the standard library is used here).

import copy
import ipaddress
import json
import math
import os
import re
import socket
import time

HERE = os.path.dirname(os.path.abspath(__file__))

# biggest message we accept (keeps us far below the 1500 byte network limit)
MAX_SIZE = 1200
MAX_NAME = 32           # longest node name we accept

# message types
REPORT = "REPORT"
ALERT = "ALERT"
ACK = "ACK"
HELLO = "HELLO"
NODE_DOWN = "NODE_DOWN"
NODE_UP = "NODE_UP"
BLOCK = "BLOCK"
CTRL_REPLY = "CTRL_REPLY"
NET_EVENT = "NET_EVENT"
FLOOD = "FLOOD"
PING = "PING"              # agent -> monitor on the alert port, answered with an ACK
DISCOVER = "DISCOVER"      # agent broadcast: "where is the monitor?"
I_AM_MONITOR = "I_AM_MONITOR"   # monitor's answer to DISCOVER

# every REPORT must have these four numbers
METRICS = ("cpu", "mem", "procs", "net_kbps")
# limits (and alerts) may also use these. A real laptop sends them when it can
# read them (no temperature sensor on most Windows laptops, for example).
EXTRA_METRICS = ("disk", "temp_c")
ALERT_METRICS = METRICS + EXTRA_METRICS
LEVELS = ("OK", "WARN", "CRIT")

# Values used when config.json does not have them (so an old config still works).
DEFAULTS = {
    "monitor_ip": "10.0.0.5",
    "report_port": 5000,
    "alert_port": 5001,
    "controller_ip": "10.0.0.254",
    "control_port": 9999,
    "report_every": 1.0,
    "down_after": 3.0,
    "ack_wait": 0.5,
    "max_tries": 4,
    "mode": "smart",
    # Phase 2
    "flood_pps": 50,          # more packets per second than this from one host = flooding
    "block_seconds": 30,      # how long a flooding host stays blocked
    "watch_every": 2.0,       # controller asks the switch for traffic numbers this often
    "slow_above_mbps": 4.0,   # a host sending more than this is slowed down ...
    "slow_rate_mbps": 1.0,    # ... to this speed
    "calm_below_mbps": 0.5,   # limit is removed when the host stays under this
    # Real laptops on a LAN / Wi-Fi (agent.py without --sim, monitor.py --lan)
    "real": {
        "monitor_ip": "auto",     # "auto" = find the monitor with a broadcast
        "discovery_port": 5002,
        "web_port": 8080,
        "limits": {
            "cpu":      {"warn": 75,    "crit": 90},
            "mem":      {"warn": 80,    "crit": 90},
            "procs":    {"warn": 450,   "crit": 600},
            "net_kbps": {"warn": 20000, "crit": 50000},
            "disk":     {"warn": 90,    "crit": 97},
            "temp_c":   {"warn": 80,    "crit": 90},
        },
    },
}


class ConfigError(Exception):
    """config.json is missing, broken or has a wrong value."""


def load_config(path=None):
    """Read config.json, fill in missing values and check every value.
    Raises ConfigError with a clear message if something is wrong."""
    if path is None:
        path = os.path.join(HERE, "config.json")
    try:
        with open(path) as f:
            raw = json.load(f)
    except OSError as e:
        raise ConfigError("cannot open %s: %s" % (path, e))
    except ValueError as e:
        raise ConfigError("%s is not valid JSON: %s" % (path, e))
    if not isinstance(raw, dict):
        raise ConfigError("%s must contain one JSON object { ... }" % path)
    cfg = copy.deepcopy(DEFAULTS)
    real = raw.get("real")
    cfg.update(raw)
    if isinstance(real, dict):
        # fill in missing keys of the "real" part too
        cfg["real"] = dict(DEFAULTS["real"], **real)
    check_config(cfg)
    return cfg


def real_config(cfg):
    """The config used for real laptops: same as cfg, but with the "real" limits."""
    out = dict(cfg)
    out["limits"] = cfg["real"]["limits"]
    return out


def check_config(cfg):
    def fail(text):
        raise ConfigError("config.json: " + text)

    for key in ("monitor_ip", "controller_ip"):
        try:
            ipaddress.IPv4Address(cfg[key])
        except (ValueError, TypeError):
            fail("%s must be an IPv4 address like 10.0.0.5, not %r" % (key, cfg[key]))

    ports = []
    for key in ("report_port", "alert_port", "control_port"):
        v = cfg[key]
        if not is_int(v) or not 1 <= v <= 65535:
            fail("%s must be a whole number from 1 to 65535, not %r" % (key, v))
        ports.append(v)
    if len(set(ports)) != 3:
        fail("report_port, alert_port and control_port must all be different")

    for key in ("report_every", "down_after", "ack_wait", "watch_every",
                "slow_above_mbps", "slow_rate_mbps", "calm_below_mbps", "block_seconds",
                "flood_pps"):
        v = cfg[key]
        if not is_number(v) or v <= 0:
            fail("%s must be a number bigger than 0, not %r" % (key, v))
    if cfg["down_after"] <= cfg["report_every"]:
        fail("down_after (%s) must be bigger than report_every (%s)"
             % (cfg["down_after"], cfg["report_every"]))
    if cfg["calm_below_mbps"] >= cfg["slow_above_mbps"]:
        fail("calm_below_mbps must be smaller than slow_above_mbps")
    if not is_int(cfg["max_tries"]) or not 1 <= cfg["max_tries"] <= 10:
        fail("max_tries must be a whole number from 1 to 10, not %r" % (cfg["max_tries"],))
    if cfg["mode"] not in ("simple", "smart"):
        fail('mode must be "simple" or "smart", not %r' % (cfg["mode"],))

    check_limits(cfg.get("limits"), "limits", fail)

    real = cfg.get("real")
    if not isinstance(real, dict):
        fail('"real" must look like {"monitor_ip": "auto", "web_port": 8080, ...}')
    ip = real.get("monitor_ip")
    if ip != "auto":
        try:
            ipaddress.IPv4Address(ip)
        except (ValueError, TypeError):
            fail('real.monitor_ip must be "auto" or an IPv4 address like 192.168.1.20, not %r'
                 % (ip,))
    for key in ("discovery_port", "web_port"):
        v = real.get(key)
        if not is_int(v) or not 1 <= v <= 65535:
            fail("real.%s must be a whole number from 1 to 65535, not %r" % (key, v))
    if real["discovery_port"] in (cfg["report_port"], cfg["alert_port"]):
        fail("real.discovery_port must be different from report_port and alert_port")
    check_limits(real.get("limits"), "real.limits", fail)


def check_limits(limits, where, fail):
    if not isinstance(limits, dict):
        fail("%s is missing" % where)
    for metric in limits:
        if metric not in ALERT_METRICS:
            fail("%s.%s is unknown (use %s)" % (where, metric, ", ".join(ALERT_METRICS)))
    for metric in METRICS:
        if metric not in limits:
            fail('%s.%s is missing' % (where, metric))
    for metric, lim in limits.items():
        if not isinstance(lim, dict) or not is_number(lim.get("warn")) \
                or not is_number(lim.get("crit")):
            fail('%s.%s must look like {"warn": 75, "crit": 90}' % (where, metric))
        if lim["warn"] >= lim["crit"]:
            fail("%s.%s: warn (%s) must be smaller than crit (%s)"
                 % (where, metric, lim["warn"], lim["crit"]))


def is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def is_number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def make_msg(msg_type, node, seq, **fields):
    """Build one message and turn it into bytes ready for sendto()."""
    msg = {"type": msg_type, "node": node, "seq": seq, "time": time.time()}
    msg.update(fields)
    data = json.dumps(msg).encode()
    if len(data) > MAX_SIZE:
        raise ValueError("message too big: %d bytes" % len(data))
    return data


def read_msg(data):
    """Turn received bytes back into a dict.
    Returns None if the packet is broken, so the caller can just skip it."""
    if not data or len(data) > MAX_SIZE:
        return None
    try:
        msg = json.loads(data.decode())
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    if not isinstance(msg, dict):
        return None
    for key in ("type", "node", "seq", "time"):
        if key not in msg:
            return None
    if not isinstance(msg["type"], str) or not isinstance(msg["node"], str):
        return None
    if not msg["node"] or len(msg["node"]) > MAX_NAME:
        return None
    if not is_int(msg["seq"]) or msg["seq"] < 0 or not is_number(msg["time"]):
        return None
    return msg


def valid_report(msg):
    """True if a REPORT has all four numbers and they make sense."""
    for m in METRICS:
        v = msg.get(m)
        if not is_number(v) or v < 0:
            return False
    return True


def valid_alert(msg):
    """True if an ALERT has everything the monitor needs."""
    return (msg.get("metric") in ALERT_METRICS and msg.get("level") in LEVELS
            and is_number(msg.get("value")) and is_number(msg.get("limit"))
            and is_number(msg.get("first_sent", msg["time"]))
            and is_int(msg.get("tries", 1)))


def check_level(value, limit):
    """Compare a value with its limits. Returns "OK", "WARN" or "CRIT"."""
    if value >= limit["crit"]:
        return "CRIT"
    if value >= limit["warn"]:
        return "WARN"
    return "OK"


def worst_level(levels):
    if "CRIT" in levels:
        return "CRIT"
    if "WARN" in levels:
        return "WARN"
    return "OK"


def clean_name(text):
    """Turn a computer name into a safe node name: letters, digits, - _ . only."""
    name = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(text)).strip("-.")
    return name[:MAX_NAME] or "node"


def udp_socket():
    """A UDP socket that behaves the same on Windows, Linux and macOS."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    # Windows only: when we send to a port nobody listens on, Windows makes the
    # NEXT recvfrom() fail with "connection reset" (error 10054). For UDP that
    # makes no sense, so switch it off.
    if hasattr(socket, "SIO_UDP_CONNRESET"):
        try:
            s.ioctl(socket.SIO_UDP_CONNRESET, False)
        except (OSError, ValueError):
            pass
    return s


def local_ips():
    """IPv4 addresses of this computer that other laptops can reach (best guess)."""
    ips = []
    # the address used to reach the internet / the router (no packet is sent)
    for target in ("8.8.8.8", "192.168.0.1", "10.255.255.255"):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect((target, 80))
            ip = s.getsockname()[0]
            if ip not in ips and not ip.startswith(("127.", "0.")):
                ips.append(ip)
        except OSError:
            pass
        finally:
            s.close()
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            if ip not in ips and not ip.startswith(("127.", "169.254.")):
                ips.append(ip)
    except OSError:
        pass
    return ips
