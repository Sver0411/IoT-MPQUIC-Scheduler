"""Read targeted qdisc, kernel receive and probe-rate evidence."""

import json
from collections import defaultdict
from statistics import median

from stage92r_analysis import edge_quality, summary
from stage92_probe import INITIAL, SCENARIOS
from stage92r2_kernel import identify_first_drop, trace_drops
from stage92_timing import probe_series
from verify_stage5 import verify_network


def stable_edge(groups, planned, old, new):
    """Locate a sustained transition, preserving ambiguous groups in its interval."""
    classified = [(t, value) for t, value in groups if abs(t-planned) <= 35_000]
    for i in range(1, len(classified)-4):
        t = classified[i][0]
        if not all(new(v) for _, v in classified[i:i+5]):
            continue
        if not all(new(v) for _, v in classified[i:] if v is not None):
            continue
        earlier = [u for u, v in classified[:i] if old(v)]
        if earlier:
            return earlier[-1], t, classified[i+4][0]
    return None


def rate_windows(tx, rx, origin):
    by_sequence = {seq: (sent, at) for kind, link, seq, sent, at in rx if kind == "burst_rx" and link == "a"}
    result = []
    for start, expected in ((155_000, 25), (615_000, 4)):
        offered = {seq: sent for kind, link, seq, sent, _ in tx if kind == "burst_tx" and link == "a"
                   and origin + start <= sent < origin + start + 36_000}
        samples = sorted((at, at-sent) for seq, sent in offered.items() if seq in by_sequence
                         for echoed, at in (by_sequence[seq],) if echoed == sent and at >= sent)
        duration = 20_000
        if len(samples) < 10 or len(samples) != len(offered):
            result.append({"expected_mbit": expected, "sent": len(offered), "received": len(samples), "pass": False})
            continue
        head = samples[0][0] + 3000
        count = sum(head <= at < head + duration for at, _ in samples)
        # 800-byte payload and 42 bytes of IPv4, UDP and Ethernet headers.
        rate = count * 842 * 8 / duration
        quarter = len(samples)//4
        growth = median(delay for _, delay in samples[-quarter:]) - median(delay for _, delay in samples[:quarter])
        result.append({"expected_mbit": expected, "sent": len(offered), "received": len(samples),
                       "receive_window_us": [head, head + duration], "measured_mbit": rate,
                       "median_added_queue_delay_us": growth,
                       "pass": samples[-1][0] >= head+duration and .75*expected <= rate <= 1.25*expected and growth > 3000})
    return result


def loss_evidence(tx, rx, link, planned, ack):
    sent = {seq: t for kind, name, seq, t, _ in tx if kind == "tx" and name == link}
    received = {seq for kind, name, seq, _, _ in rx if kind == "rx" and name == link}
    # Independent packet observations. The ACK is used only to select the post-update cohort.
    before = [(seq, t) for seq, t in sent.items() if planned-70_000 <= t < planned-5000]
    after = [(seq, t) for seq, t in sent.items() if ack+5000 <= t < ack+70_000]
    first = min((t for seq, t in sent.items() if t >= planned-5000 and seq not in received), default=None)
    def window(rows):
        return {"n": len(rows), "lost": sum(seq not in received for seq, _ in rows)}
    pre, post = window(before), window(after)
    return {"before": pre, "after": post, "first_missing_probe_send_us": first,
            "first_missing_minus_planned_us": first-planned if first is not None else None,
            "last_confirmation_send_us": max((t for _, t in after), default=None),
            "rate_verified": pre["n"] >= 100 and post["n"] >= 100 and pre["lost"] == 0
            and .35 <= post["lost"]/post["n"] <= .65}


def paired_probes(tx, rx, planned):
    sent = {(link, seq): t for kind, link, seq, t, _ in tx if kind == "tx" and seq % 4 == 0
            and abs(t-planned) <= 36_000}
    received = {(link, seq): at-t for kind, link, seq, t, at in rx if kind == "rx" and (link, seq) in sent}
    result = []
    for (link, seq), a in sorted(sent.items(), key=lambda row: row[1]):
        if link != "a":
            continue
        b = sent.get(("b", seq))
        delta = None
        if b is not None and abs(b-a) <= 500 and ("a", seq) in received and ("b", seq) in received:
            delta = received["a", seq]-received["b", seq]
        result.append((max(a, b) if b is not None else a, delta))
    return result


def probe_confirmed_at(probes, side, link, lower, upper):
    peer = "server" if side == "client" else "client"
    return max((at for kind, name, _, t, at in probes[peer]
                if kind == "rx" and name == link and lower <= t <= upper+500), default=None)


def analyze_trial(folder, case, quality):
    scenario = case["scenario"]
    with (folder / "network.jsonl").open() as file:
        initial, phase = map(json.loads, file)
    origin = initial["origin_us"]
    verify_network(folder, {"initial": INITIAL, "stage92": True,
                            "phases": [{"at_us": at, "links": links} for at, links in SCENARIOS[scenario]]},
                   origin)
    planned = origin + phase["offset_us"]
    probes = {side: json.loads((folder / f"{side}.json").read_text()) for side in ("client", "server")}
    rows, rankings = [], []
    kernel = folder / "kernel-events.log"
    pids = json.loads((folder / "kernel-pids.json").read_text()) if kernel.exists() else {}
    for direction, timing in phase["directions"].items():
        side, link = direction.split("-")
        peer = "server" if side == "client" else "client"
        if not planned == timing["planned_us"] <= timing["start_us"] <= timing["submitted_us"] <= timing["done_us"]:
            raise ValueError("invalid configuration chronology")
        group = probe_series(probes[side], probes[peer], link)
        edge, evidence = None, {}
        if scenario == "bandwidth":
            edge = stable_edge([(t, spread) for t, _, spread, _ in group], planned,
                               lambda v: v is not None and 0 <= v <= 160, lambda v: v is not None and 315 <= v <= 800)
            evidence["rate_windows"] = rate_windows(probes[side], probes[peer], origin)
        elif scenario == "reversal":
            if "qdisc tbf 1: root" not in timing["qdisc"] or "qdisc netem 10: parent 1:1" not in timing["qdisc"]:
                raise ValueError("reversal must retain the fixed outer rate limiter")
            old, new = INITIAL[link][0]*1000, phase["links"][link][0]*1000
            edge = stable_edge([(t, delay) for t, delay, _, _ in group], planned,
                               lambda v: v is not None and abs(v-old) <= 3500,
                               lambda v: v is not None and abs(v-new) <= 3500)
        else:
            evidence["loss"] = loss_evidence(probes[side], probes[peer], link, planned, timing["done_us"])
            if pids:
                drops = trace_drops(kernel, pids[side])
                witness = identify_first_drop(drops, probes[side], probes[peer], timing["start_us"],
                                              evidence["loss"]["first_missing_probe_send_us"])
                evidence["loss"]["kernel_drops"] = len(drops)
                evidence["loss"]["kernel_witness"] = witness
                if witness and evidence["loss"]["rate_verified"]:
                    # The rule cannot change before its request starts. A matching QDISC_DROP
                    # independently bounds the latest data-plane effect. ACK time is reported separately.
                    edge = (timing["start_us"], witness["kernel_drop_us"], None)
        if scenario == "loss":
            confirmation = evidence["loss"]["last_confirmation_send_us"]
            stable = confirmation + 55_000 if confirmation is not None else None
        else:
            stable = probe_confirmed_at(probes, side, link, edge[0], edge[2]) if edge else None
        rows.append({"case": case["name"], "scenario": scenario, "direction": direction, "planned_us": planned,
                     "command_start_us": timing["start_us"], "netlink_submitted_us": timing["submitted_us"],
                     "qdisc_ack_us": timing["done_us"], "snapshot_us": timing["snapshot_us"],
                     "last_old_probe_us": edge[0] if edge and scenario != "loss" else None,
                     "first_new_probe_us": edge[1] if edge and scenario != "loss" else None,
                     "edge_lower_us": edge[0] if edge else None, "edge_upper_us": edge[1] if edge else None,
                     "edge_error_us": edge[1]-planned if edge else None,
                     "bound_error_us": max(abs(edge[0]-planned), abs(edge[1]-planned)) if edge else None,
                     "stable_after_us": stable-planned if stable else None, **evidence})
    if scenario == "reversal":
        for side, peer in (("client", "server"), ("server", "client")):
            pairs = paired_probes(probes[side], probes[peer], planned)
            edge = stable_edge(pairs, planned, lambda v: v is not None and v < -7000,
                               lambda v: v is not None and v > 7000)
            rankings.append({"side": side, "planned_us": planned,
                             "last_old_probe_us": edge[0] if edge else None, "first_new_probe_us": edge[1] if edge else None})
        edge_quality(rankings, quality["max_edge_error_us"], quality["max_direction_skew_us"])
    bounds = [dict(planned_us=row["planned_us"], last_old_probe_us=row["edge_lower_us"],
                   first_new_probe_us=row["edge_upper_us"]) for row in rows]
    edge_quality(bounds, quality["max_edge_error_us"], quality["max_direction_skew_us"])
    for row, bound in zip(rows, bounds):
        row["pass"] = bound["pass"]
        row["phase_skew_bound_us"] = bound["phase_skew_bound_us"]
        if scenario == "bandwidth":
            row["pass"] &= all(w["pass"] for w in row["rate_windows"])
        if scenario == "reversal":
            row["pass"] &= all(r["pass"] for r in rankings)
            row["relative_ranking"] = rankings
        if scenario == "loss":
            row["pass"] &= row["loss"].get("kernel_witness") is not None
        if row["direction"].startswith("client"):
            counterpart = next(r for r in rows if r["direction"] == row["direction"].replace("client", "server"))
            pair = row["edge_error_us"], counterpart["edge_error_us"]
            row["client_minus_server_us"] = pair[0]-pair[1] if None not in pair else None
    return rows


def timing_summary(edges):
    by_scenario = defaultdict(list)
    for edge in edges:
        by_scenario[edge["scenario"]].append(edge)
    return {name: {"directions": len(rows), "passed": sum(r["pass"] for r in rows),
                   "configuration_ack_lag_us": summary([r["qdisc_ack_us"]-r["planned_us"] for r in rows]),
                   "loss_regimes_verified": sum(r.get("loss", {}).get("rate_verified", False) for r in rows),
                   "first_missing_minus_planned_us": summary([r["loss"]["first_missing_minus_planned_us"] for r in rows
                       if r.get("loss", {}).get("first_missing_minus_planned_us") is not None]),
                   "loss_window_complete_us": summary([r["loss"]["last_confirmation_send_us"]-r["planned_us"]
                       for r in rows if r.get("loss", {}).get("last_confirmation_send_us") is not None]),
                   **{key: summary([r[key] for r in rows if r.get(key) is not None])
                      for key in ("edge_error_us", "bound_error_us", "phase_skew_bound_us", "client_minus_server_us",
                                  "stable_after_us")}}
            for name, rows in sorted(by_scenario.items())}
