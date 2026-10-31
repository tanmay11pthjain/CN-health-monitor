# Small helpers used by agent.py, monitor.py and the tests.

import json
import os
import time

HERE = os.path.dirname(os.path.abspath(__file__))

# biggest message we accept (keeps us far below the 1500 byte network limit)
MAX_SIZE = 1200

# message types
REPORT = "REPORT"
ALERT = "ALERT"
ACK = "ACK"
NODE_DOWN = "NODE_DOWN"
NODE_UP = "NODE_UP"
CTRL_REPLY = "CTRL_REPLY"
NET_EVENT = "NET_EVENT"


def load_config(path=None):
    if path is None:
        path = os.path.join(HERE, "config.json")
    with open(path) as f:
        return json.load(f)


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
    if len(data) > MAX_SIZE:
        return None
    try:
        msg = json.loads(data.decode())
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(msg, dict):
        return None
    for key in ("type", "node", "seq", "time"):
        if key not in msg:
            return None
    if not isinstance(msg["seq"], int) or not isinstance(msg["time"], (int, float)):
        return None
    return msg


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
