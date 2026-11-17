# Small helpers used by agent.py, monitor.py, controller.py and the tests.

import ipaddress
import json
import math
import os
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

METRICS = ("cpu", "mem", "procs", "net_kbps")
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
    cfg = dict(DEFAULTS)
    cfg.update(raw)
    check_config(cfg)
    return cfg


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

    limits = cfg.get("limits")
    if not isinstance(limits, dict):
        fail("limits is missing")
    for metric in METRICS:
        lim = limits.get(metric)
        if not isinstance(lim, dict) or not is_number(lim.get("warn")) \
                or not is_number(lim.get("crit")):
            fail('limits.%s must look like {"warn": 75, "crit": 90}' % metric)
        if lim["warn"] >= lim["crit"]:
            fail("limits.%s: warn (%s) must be smaller than crit (%s)"
                 % (metric, lim["warn"], lim["crit"]))
    for metric in limits:
        if metric not in METRICS:
            fail("limits.%s is unknown (use %s)" % (metric, ", ".join(METRICS)))


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
    return (msg.get("metric") in METRICS and msg.get("level") in LEVELS
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
