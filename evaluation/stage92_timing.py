"""Extract directional command and observed UDP transition timing."""

import json
from collections import defaultdict
from pathlib import Path
from statistics import median

from stage7_statistics import quantile


def distribution(values):
    return ({"n": len(values), "min": min(values), "p50": median(values), "p95": quantile(values, .95),
             "max": max(values)} if values else {"n": 0})


def probe_series(sent_rows, received_rows, link):
    sent = {seq: t for kind, name, seq, t, _ in sent_rows if kind == "tx" and name == link}
    received = {seq: (t, arrival) for kind, name, seq, t, arrival in received_rows if kind == "rx" and name == link}
    if len(sent) < 100 or any(sent.get(seq) != pair[0] or pair[1] < pair[0] for seq, pair in received.items()):
        raise ValueError("invalid probe capture")
    groups = []
    for seq, t in sorted(sent.items()):
        if seq % 4 or not all(seq + j in sent for j in range(4)):
            continue
        arrivals = [received[seq+j][1] for j in range(4) if seq+j in received]
        spread = (arrivals[-1] - arrivals[0]) / 3 if len(arrivals) == 4 else None
        latency = received[seq][1] - t if seq in received else None
        groups.append((t, latency, spread, 4 - len(arrivals)))
    return groups


def infer_edge(groups, scenario, stage, old_delay, new_delay):
    """Use observed packet groups, never the command completion timestamp, to locate change."""
    if scenario in ("rtt", "reversal"):
        threshold = (old_delay + new_delay) * 500
        sign = 1 if new_delay > old_delay else -1
        after = [latency is not None and sign * (latency - threshold) > 0 for _, latency, _, _ in groups]
        length = 3
    elif scenario == "bandwidth":
        after = [spread is not None and spread > 1000 for _, _, spread, _ in groups]
        length = 3
    elif scenario == "outage":
        after = [loss == (4 if stage == 1 else 0) for _, _, _, loss in groups]
        length = 3
        if stage == 2:
            # Recovery needs a preceding observed outage rather than any fully delivered initial group.
            missing = next((i for i in range(len(groups)-2) if all(r[3] == 4 for r in groups[i:i+3])), None)
            if missing is None:
                return None
            after[:missing+3] = [False] * (missing+3)
    else:
        length = 6
        for i in range(1, len(groups)-length+1):
            if sum(r[3] for r in groups[i-1:i]) == 0 and sum(r[3] for r in groups[i:i+length]) >= 8:
                return groups[i-1][0], groups[i][0], groups[i+length-1][0]
        return None
    for i in range(1, len(groups)-length+1):
        if not after[i-1] and all(after[i:i+length]):
            return groups[i-1][0], groups[i][0], groups[i+length-1][0]
    return None


def analyze_trial(folder, scenario, quality):
    phases = [json.loads(line) for line in (folder / "network.jsonl").read_text().splitlines()]
    origin = phases[0]["origin_us"]
    raw = {side: json.loads((folder / f"{side}.json").read_text()) for side in ("client", "server")}
    old = phases[0]["links"].copy()
    output = []
    for stage, phase in enumerate(phases[1:], 1):
        scheduled = origin + phase["offset_us"]
        edges = []
        for direction, timing in phase["directions"].items():
            side, link = direction.split("-")
            peer = "client" if side == "server" else "server"
            groups = probe_series(raw[side], raw[peer], link)
            window = [row for row in groups if abs(row[0] - scheduled) <= 170_000]
            edge = infer_edge(window, scenario, stage, old[link][0], phase["links"][link][0])
            measured = edge[1] - scheduled if edge else None
            bounded = max(abs(edge[0] - scheduled), abs(measured)) if edge else None
            edges.append(dict(scenario=scenario, stage=stage, direction=direction, planned_us=scheduled,
                              command_start_us=timing["start_us"], command_done_us=timing["done_us"],
                              command_lag_us=timing["done_us"] - scheduled,
                              last_old_probe_us=edge[0] if edge else None, first_new_probe_us=edge[1] if edge else None,
                              corroborated_through_us=edge[2] if edge else None, measured_error_us=measured,
                              bracket_error_us=bounded, probe_resolution_us=edge[1]-edge[0] if edge else None,
                              pass_edge=bounded is not None and bounded <= quality["max_edge_error_us"],
                              before=probe_metrics(window, scheduled, before=True),
                              after=probe_metrics(window, scheduled, before=False)))
        observed = [row["first_new_probe_us"] for row in edges]
        skew = max(observed) - min(observed) if all(t is not None for t in observed) else None
        for row in edges:
            row["observed_skew_us"] = skew
            row["pass_skew"] = skew is not None and skew <= quality["max_direction_skew_us"]
        output.extend(edges)
        old.update(phase["links"])
    return output


def probe_metrics(groups, scheduled, before):
    signed = -1 if before else 1
    rows = [r for r in groups if 10_000 <= signed * (r[0] - scheduled) <= 55_000]
    delay = [r[1] for r in rows if r[1] is not None]
    spread = [r[2] for r in rows if r[2] is not None]
    return {"groups": len(rows), "median_delay_us": median(delay) if delay else None,
            "median_spacing_us": median(spread) if spread else None,
            "lost": sum(r[3] for r in rows), "sent": 4 * len(rows)}


def summarize_timing(edges):
    buckets = defaultdict(list)
    for row in edges:
        buckets[row["scenario"]].append(row)
    return {name: {"expected": len(rows), "observed": sum(r["measured_error_us"] is not None for r in rows),
                   "qc_pass": sum(r["pass_edge"] and r["pass_skew"] for r in rows),
                   **{key: distribution([r[key] for r in rows if r[key] is not None])
                      for key in ("command_lag_us", "measured_error_us", "bracket_error_us", "observed_skew_us")}}
            for name, rows in sorted(buckets.items())}
