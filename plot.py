#!/usr/bin/env python3
# Draws the graphs from results/results.json (made by experiments.py)
# and writes a results table to results/summary.md.
#
#   python3 plot.py
#
# Needs matplotlib:  sudo apt install python3-matplotlib

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
RESULTS = os.path.join(HERE, "results")
MODES = ["simple", "smart"]
COLORS = {"simple": "#9a9a9a", "smart": "#1f6fd1"}
LABELS = {"simple": "simple mode (baseline)", "smart": "smart mode (our controller)"}


def load():
    path = os.path.join(RESULTS, "results.json")
    try:
        with open(path) as f:
            data = json.load(f)
    except OSError:
        sys.exit("ERROR: %s not found. Run  sudo python3 experiments.py  first." % path)
    except ValueError:
        sys.exit("ERROR: %s is broken. Run the experiments again." % path)
    if not isinstance(data, dict):
        sys.exit("ERROR: %s is broken. Run the experiments again." % path)
    return data


def rows(data, mode, test):
    r = data.get(mode, {}).get(test)
    return r if isinstance(r, list) else []


def num(v):
    return v if isinstance(v, (int, float)) else 0


def fmt(v, digits=1):
    if v is None:
        return "-"
    if isinstance(v, float):
        return "%.*f" % (digits, v)
    return str(v)


def get(d, *keys):
    for k in keys:
        if not isinstance(d, dict):
            return None
        d = d.get(k)
    return d


# ----------------------------------------------------------------------
# graphs
# ----------------------------------------------------------------------

def bars(ax, groups, values_by_mode, ylabel, title, log=False):
    """Grouped bars: one group per condition, one bar per mode."""
    width = 0.38
    xs = range(len(groups))
    for i, mode in enumerate(MODES):
        vals = values_by_mode.get(mode)
        if not vals:
            continue
        pos = [x + (i - 0.5) * width for x in xs]
        b = ax.bar(pos, [num(v) for v in vals], width, color=COLORS[mode], label=LABELS[mode])
        for rect, v in zip(b, vals):
            ax.annotate(fmt(v, 1) if v is not None else "n/a",
                        (rect.get_x() + rect.get_width() / 2, rect.get_height()),
                        ha="center", va="bottom", fontsize=8, xytext=(0, 2),
                        textcoords="offset points")
    ax.set_xticks(list(xs))
    ax.set_xticklabels(groups)
    ax.set_ylabel(ylabel)
    ax.set_title(title)
    top = max([num(v) for vals in values_by_mode.values() for v in (vals or [])] + [1])
    if log:
        ax.set_yscale("symlog", linthresh=1)
        ax.set_ylim(0, top * 20)            # room for the numbers and the legend
    else:
        ax.set_ylim(0, top * 1.35)
    ax.grid(axis="y", alpha=0.3)
    ax.legend(fontsize=8)


def plot_all(data, plt):
    made = []

    def save(fig, name):
        fig.tight_layout()
        path = os.path.join(RESULTS, name)
        fig.savefig(path, dpi=120)
        plt.close(fig)
        made.append(path)

    # 1. traffic jam -> alert latency
    if any(rows(data, m, "jam") for m in MODES):
        fig, axes = plt.subplots(1, 2, figsize=(10, 4))
        for ax, stat, title in ((axes[0], "mean", "Average alert latency"),
                                (axes[1], "p95", "Slowest alerts (95th percentile)")):
            vals = {}
            for m in MODES:
                r = {x["jam"]: x for x in rows(data, m, "jam")}
                if r:
                    vals[m] = [get(r.get(False), "alert_latency_ms", stat),
                               get(r.get(True), "alert_latency_ms", stat)]
            bars(ax, ["no traffic jam", "heavy traffic jam"], vals, "milliseconds (log scale)",
                 title, log=True)
        save(fig, "1_jam_alert_latency.png")

    # 2. server failure -> detection time
    if any(rows(data, m, "failure") for m in MODES):
        fig, ax = plt.subplots(figsize=(6, 4))
        kinds = ["stop agent", "cut cable"]
        vals = {}
        for m in MODES:
            r = {x["kind"]: x for x in rows(data, m, "failure")}
            if r:
                vals[m] = [get(r.get(k), "detect_s", "mean") for k in kinds]
        bars(ax, kinds, vals, "seconds until the monitor says DOWN", "Failure detection time")
        ax.axhline(3.0, color="#c0392b", linestyle="--", linewidth=1)
        ax.text(1.45, 3.05, "3 s limit", color="#c0392b", fontsize=8, ha="right")
        save(fig, "2_failure_detection.png")

    # 3. packet loss -> alerts delivered
    if any(rows(data, m, "loss") for m in MODES):
        fig, ax = plt.subplots(figsize=(7, 4.5))
        for m in MODES:
            r = sorted(rows(data, m, "loss"), key=lambda x: x["loss_pct"])
            if not r:
                continue
            xs = [x["loss_pct"] for x in r]
            ax.plot(xs, [num(x["delivered_pct"]) for x in r], "o-", color=COLORS[m],
                    label="%s: with resend" % m)
            ax.plot(xs, [num(x["first_try_pct"]) for x in r], "s--", color=COLORS[m],
                    alpha=0.6, label="%s: first try only (no resend)" % m)
        ax.set_xlabel("packets lost on every cable (%)")
        ax.set_ylabel("alerts that reached the monitor (%)")
        ax.set_title("Packet loss: ACK + resend keeps alerts arriving")
        ax.set_ylim(0, 105)
        ax.grid(alpha=0.3)
        ax.legend(fontsize=8)
        save(fig, "3_loss_alerts_delivered.png")

    # 4. more servers -> loss, latency, monitor CPU
    if any(rows(data, m, "scale") for m in MODES):
        fig, axes = plt.subplots(1, 3, figsize=(13, 4))
        for ax, key, ylabel, title in (
                (axes[0], ("report_loss_pct",), "% of reports lost", "Lost reports"),
                (axes[1], ("report_latency_ms", "mean"), "milliseconds", "Report latency"),
                (axes[2], ("monitor_cpu_pct",), "% of one CPU", "Monitor CPU use")):
            for m in MODES:
                r = sorted(rows(data, m, "scale"), key=lambda x: x["agents"])
                if r:
                    ax.plot([x["agents"] for x in r], [num(get(x, *key)) for x in r], "o-",
                            color=COLORS[m], label=LABELS[m])
            ax.set_xlabel("number of agents")
            ax.set_ylabel(ylabel)
            ax.set_title(title)
            ax.grid(alpha=0.3)
            ax.legend(fontsize=8)
        save(fig, "4_scale.png")

    # 5. flooding host
    if any(rows(data, m, "flood") for m in MODES):
        fig, axes = plt.subplots(1, 2, figsize=(10, 4))
        r = {m: (rows(data, m, "flood") or [{}])[0] for m in MODES}
        bars(axes[0], ["time until blocked"],
             {m: [r[m].get("time_to_block_s")] for m in MODES if rows(data, m, "flood")},
             "seconds (n/a = never blocked)", "Flooding host: time until blocked")
        bars(axes[1], ["junk packets that reached the monitor"],
             {m: [r[m].get("flood_packets_at_monitor")] for m in MODES if rows(data, m, "flood")},
             "packets", "Flooding host: junk that got through")
        save(fig, "5_flood.png")
    return made


# ----------------------------------------------------------------------
# results table
# ----------------------------------------------------------------------

def table(data):
    out = ["# Results: simple mode (baseline) vs smart mode", ""]
    info = data.get("info", {})
    out.append("Run on %s%s%s." % (info.get("date", "?"),
                                   ", quick mode" if info.get("quick") else "",
                                   ", userspace switch" if info.get("userspace") else ""))
    out.append("")

    out += ["## 1. Traffic jam: alert latency (ms)", "",
            "| Mode | Jam | Alerts arrived | Mean | 95th pct | Max | Host slowed down |",
            "|---|---|---|---|---|---|---|"]
    for m in MODES:
        for x in rows(data, m, "jam"):
            lat = x.get("alert_latency_ms", {})
            out.append("| %s | %s | %s of %s | %s | %s | %s | %s |" % (
                m, "heavy" if x.get("jam") else "none", x.get("alerts_arrived"),
                x.get("alerts_made"), fmt(lat.get("mean")), fmt(lat.get("p95")),
                fmt(lat.get("max")), "yes" if x.get("slowed_events") else "no"))

    out += ["", "## 2. Server failure: time until DOWN (s)", "",
            "| Mode | What happened | Mean | Max | Missed |", "|---|---|---|---|---|"]
    for m in MODES:
        for x in rows(data, m, "failure"):
            d = x.get("detect_s", {})
            out.append("| %s | %s | %s | %s | %s |" % (m, x.get("kind"), fmt(d.get("mean"), 2),
                                                     fmt(d.get("max"), 2), x.get("missed")))

    out += ["", "## 3. Packet loss: alerts delivered", "",
            "| Mode | Loss | Alerts arrived | Delivered | First try only | Reports lost |",
            "|---|---|---|---|---|---|"]
    for m in MODES:
        for x in rows(data, m, "loss"):
            out.append("| %s | %s%% | %s of %s | %s%% | %s%% | %s%% |" % (
                m, x.get("loss_pct"), x.get("alerts_arrived"), x.get("alerts_made"),
                fmt(x.get("delivered_pct")), fmt(x.get("first_try_pct")),
                fmt(x.get("report_loss_pct"))))

    out += ["", "## 4. More servers", "",
            "| Mode | Agents | UP | Reports | Lost | Latency mean (ms) | Monitor CPU |",
            "|---|---|---|---|---|---|---|"]
    for m in MODES:
        for x in rows(data, m, "scale"):
            out.append("| %s | %s | %s | %s | %s%% | %s | %s%% |" % (
                m, x.get("agents"), x.get("agents_up"), x.get("reports"),
                fmt(x.get("report_loss_pct"), 2), fmt(get(x, "report_latency_ms", "mean"), 2),
                fmt(x.get("monitor_cpu_pct"))))

    out += ["", "## 5. Flooding host", "",
            "| Mode | Noticed after (s) | Blocked after (s) | Junk packets at monitor |",
            "|---|---|---|---|"]
    for m in MODES:
        for x in rows(data, m, "flood"):
            out.append("| %s | %s | %s | %s of ~%s |" % (
                m, fmt(x.get("time_to_notice_s"), 2),
                fmt(x.get("time_to_block_s"), 2) if x.get("time_to_block_s") is not None
                else "never", x.get("flood_packets_at_monitor"), x.get("flood_packets_sent")))
    out.append("")
    return "\n".join(out)


def main():
    data = load()
    os.makedirs(RESULTS, exist_ok=True)
    with open(os.path.join(RESULTS, "summary.md"), "w") as f:
        f.write(table(data))
    print("wrote results/summary.md")
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib is missing, so no graphs. Install it:  sudo apt install "
              "python3-matplotlib")
        return
    for path in plot_all(data, plt):
        print("wrote %s" % os.path.relpath(path, HERE))


if __name__ == "__main__":
    main()
