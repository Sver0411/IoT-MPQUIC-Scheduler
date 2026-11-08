"""Replay Stage 9.3-C clock, packet, and measurement qualification evidence."""

import argparse
import json
import math
import random
import struct
import sys
import tempfile
from collections import Counter, defaultdict
from pathlib import Path
from statistics import fmean, stdev

from scipy.stats import t

from stage93a import ROOT, file_hash, replay_records, write_json
from stage93b2_diagnostics import phase_at
from stage7_statistics import summarize
from verify_stage91 import by_event, log_rows
from verify_stage92 import verify

sys.path.insert(0, str(ROOT / "network/scenarios"))
from run_stage92 import checksums
from stage92_design import fingerprint
from stage93c_evidence import join_probes, kernel_drops, max_gap_ns, read_capture
from stage93c_probe import LINKS, SIDES, identity, token_for

FROZEN_SHA256 = "85010bd35858a8fa2e22814abcef82d5652e532cdbf4800172129587fa02f4fe"
FROZEN_FILE = ROOT / "results/stage93b2/development_pilot_plan.json"
PRIOR_OVERHEAD = ROOT / "results/stage92r2/analysis/overhead.json"


def limits():
    quality = json.loads((ROOT / "configs/stage92.json").read_text())["quality"]
    settings = {"probe_period_us": 650, "max_probe_spacing_us": 1000, "max_mapping_error_us": 100,
                "pre_switch_window_us": 25000, "target_window_us": 50000, "confirm_count": 5,
                "max_unknown_transition_fraction": .10, "max_pairs_per_cell": 16,
                "planning_goodput_halfwidth_pp": 3.0, "planning_event_halfwidth_pp": .75,
                "pair_confidence": .90, "order_seed": 93303}
    return quality, settings


def validate_frozen(path=FROZEN_FILE):
    digest = file_hash(path)
    if digest != FROZEN_SHA256:
        raise ValueError("frozen Stage 9.3-B2 file SHA-256 changed")
    plan = json.loads(path.read_text())
    cases = plan["cases"]
    if (len(cases) != 784 or fingerprint(cases) != plan["schedule_sha256"] or
            plan["runs_executed"] or plan["sealed_runs_executed"] or
            plan["settings"]["max_probe_spacing_us"] != 1000 or
            plan["settings"]["max_unknown_transition_fraction"] != .10):
        raise ValueError("frozen plan identity or original limits differ")
    for key in ("name", "connection_id", "seed", "assignment_seed", "network_seed", "trajectory_sha256", "workload_sha256"):
        if len({row[key] for row in cases}) != len(cases):
            raise ValueError(f"duplicate frozen identity: {key}")
    groups = Counter((r["environment"], r["split"], r["load"], r["timing"]) for r in cases)
    if (len(groups) != 16 or set(groups.values()) != {49} or
            {r["split"] for r in cases} != {"train", "validation"} or
            {r["load"] for r in cases} != {"low", "high"} or {r["timing"] for r in cases} != {"750000", "1300000"}):
        raise ValueError("frozen plan partition, load or timing coverage differs")
    for case in cases:
        if (fingerprint({k: case[k] for k in ("initial", "phases")}) != case["network_sha256"] or
                fingerprint([case["network_sha256"], case["network_seed"]]) != case["trajectory_sha256"] or
                not 0 <= min(case["release_times_us"]) <= max(case["release_times_us"]) < case["horizon_us"]):
            raise ValueError("frozen network/workload configuration differs")
    for field in ("environment", "family", "network_sha256"):
        train = {r[field] for r in cases if r["split"] == "train"}
        if any(r[field] in train for r in cases if r["split"] == "validation"):
            raise ValueError(f"frozen partition overlap: {field}")
    return {"file_sha256": digest, "schedule_sha256": plan["schedule_sha256"], "cases": cases,
            "case_cells": {" / ".join(key): count for key, count in sorted(groups.items())}}


def halfwidth(sd, n, confidence):
    return float(t.ppf((1 + confidence) / 2, n - 1)) * sd / math.sqrt(n)


def pair_plan(quality, settings):
    prior = json.loads(PRIOR_OVERHEAD.read_text())["effects"]
    cap = settings["max_pairs_per_cell"]
    metrics = {}
    for name, target, margin in (("goodput_delta_percent", settings["planning_goodput_halfwidth_pp"],
                                  quality["max_goodput_delta_percent"]),
                                 ("event_on_time_pp", settings["planning_event_halfwidth_pp"],
                                  quality["max_event_delta_pp"])):
        full, quiet = (prior[arm][name]["differences"] for arm in ("full", "quiet"))
        if len(full) != len(quiet) or any(x is None for x in (*full, *quiet)):
            raise ValueError("incomplete historical planning pairs")
        proxies = {"full_minus_off": stdev(full), "quiet_minus_off": stdev(quiet),
                   "full_minus_quiet": stdev(a - b for a, b in zip(full, quiet))}
        sd = max(proxies.values())
        required = next(n for n in range(2, 10000) if halfwidth(sd, n, settings["pair_confidence"]) <= target)
        metrics[name] = {"historical_pairs": len(full), "historical_proxy_sd": proxies, "planning_sd": sd,
                         "target_halfwidth": target, "halfwidth_at_cap": halfwidth(sd, cap, settings["pair_confidence"]),
                         "equivalence_margin": margin, "required_blocks": required}
    required = max(v["required_blocks"] for v in metrics.values())
    return {"source_sha256": file_hash(PRIOR_OVERHEAD), "planning_metrics": metrics, "cap": cap,
            "formal_blocks_per_cell": required if required <= cap else 0,
            "status": "planned" if required <= cap else "NO-GO: historical variance exceeds capped precision",
            "interval": "two-sided 90% paired Student t for Goodput/Event Delivery; existing 95% seed bootstrap for send overhead",
            "arm_order_seed": settings["order_seed"], "send_overhead_limit_us": quality["max_send_overhead_us"],
            "contrasts": ["E1-E0", "E2-E1 (outage only)"]}


def make_schedule(frozen, planning, settings):
    profiles = {}
    for case in sorted(frozen["cases"], key=lambda r: r["seed"]):
        if case["timing"] == "750000":
            profiles.setdefault((case["environment"], case["load"]), case)
    schedule = []

    def add(profile, arm, seed, block):
        serial = len(schedule)
        row = {k: v for k, v in profile.items() if k not in ("workload_sha256", "trajectory_sha256")}
        scenario = "reversal" if len(row["phases"]) == 1 else "outage"
        schedule.append({**row, "name": f"c93-{serial:03d}", "connection_id": f"c93-engineering-{serial:03d}",
                         "split": "engineering", "environment": f"engineering/{profile['environment']}",
                         "family": f"engineering/{profile['family']}", "shape": profile["environment"],
                         "scenario": scenario, "serial": serial, "seed": seed, "assignment_seed": seed + 10_000,
                         "network_seed": seed + 20_000, "block": block, "arm": arm})

    for i, (_, case) in enumerate(sorted(profiles.items())):
        add(case, "E1" if len(case["phases"]) == 1 else "E2", 3_930_000 + i, None)
    rng = random.Random(settings["order_seed"])
    block = 0
    for (environment, load), case in sorted(profiles.items()):
        if not environment.startswith("b2-train-"):
            continue  # One reversal and one outage for the formal comparison.
        for _ in range(planning["formal_blocks_per_cell"]):
            arms = ["E0", "E1"] if len(case["phases"]) == 1 else ["E0", "E1", "E2"]
            rng.shuffle(arms)
            for arm in arms:
                add(case, arm, 3_931_000 + block, block)
            block += 1
    for key in ("name", "connection_id", "seed", "network_seed", "assignment_seed"):
        if {r[key] for r in schedule} & {r[key] for r in frozen["cases"]}:
            raise ValueError(f"engineering schedule overlaps frozen {key}")
    if len({r["connection_id"] for r in schedule}) != len(schedule):
        raise ValueError("engineering connection identity reused")
    return schedule


def edge_control(row, direction, origin):
    control = row["directions"][direction]
    planned, start, submitted, ack = (control[k] for k in ("planned_us", "start_us", "submitted_us", "done_us"))
    if not origin + row["offset_us"] == planned <= start <= submitted <= ack:
        raise ValueError("invalid netlink chronology")
    return {"planned_us": planned, "command_start_us": start, "netlink_submitted_us": submitted, "qdisc_ack_us": ack}


def edge_result(control, old, new, confirmation, uncertainty_ns=0):
    lower = (old["tx_before_ns"] - uncertainty_ns) // 1000
    upper = (new["tx_after_ns"] + uncertainty_ns + 999) // 1000
    return {**control, "last_old_probe_us": old["tx_before_ns"] // 1000,
            "first_new_probe_us": new["tx_before_ns"] // 1000,
            "last_old_sequence": old["sequence"], "first_new_sequence": new["sequence"],
            "transition_lower_us": lower, "transition_upper_us": upper,
            "stable_confirmation_us": (confirmation + uncertainty_ns + 999) // 1000,
            "max_error_us": max(abs(lower - control["planned_us"]), abs(upper - control["planned_us"]))}


def state_change(rows, states, planned, count):
    """Require a run of old observations and sustained new state in send order."""
    for i in range(count, len(rows) - count):
        if states[i:i + count] != ["new"] * count:
            continue
        if abs(rows[i]["tx_before_ns"] // 1000 - planned) > 50_000:
            continue
        previous = [j for j in range(i) if states[j] == "old"]
        if not previous or previous[-1] < count - 1:
            continue
        old = previous[-1]
        if (states[old - count + 1:old + 1] != ["old"] * count or
                "old" in [state for row, state in zip(rows[i:], states[i:])
                          if row["tx_before_ns"] // 1000 <= planned + 50_000]):
            continue
        return rows[old], rows[i], max(row["rx_ns"] for row in rows[i:i + count])
    raise ValueError("no sustained, independently observed old-to-new probe boundary")


def reversal_edges(case, timeline, directions, origin, settings, uncertainty):
    update = timeline[1]
    output, ranking = {}, {}
    count = settings["confirm_count"]
    for direction, rows in directions.items():
        link = direction[-1]
        initial, changed = case["initial"][link], update["links"][link]
        control = edge_control(update, direction, origin)
        states = []
        for row in rows:
            latency = (row["rx_ns"] - row["tx_before_ns"]) / 1000 if row["rx_ns"] is not None else None
            matches = [label for label, profile in (("old", initial), ("new", changed))
                       if latency is not None and abs(latency - profile[0] * 1000) <= 1600 + 1000 * sum(profile[3:])]
            states.append(matches[0] if len(matches) == 1 else "unknown")
        output[direction] = edge_result(control, *state_change(rows, states, control["planned_us"], count), uncertainty)
    for side in SIDES:
        pairs, states = [], []
        old_sign = case["initial"]["a"][0] < case["initial"]["b"][0]
        new_sign = update["links"]["a"][0] < update["links"]["b"][0]
        if old_sign == new_sign:
            raise ValueError("network profiles do not reverse relative delay")
        left, right = directions[f"{side}-a"], directions[f"{side}-b"]
        for a, b in zip(left, right, strict=True):
            if a["sequence"] != b["sequence"]:
                raise ValueError("path ranking sequence mismatch")
            row = dict(sequence=a["sequence"], tx_before_ns=min(a["tx_before_ns"], b["tx_before_ns"]),
                       tx_after_ns=max(a["tx_after_ns"], b["tx_after_ns"]), rx_ns=None)
            state = "unknown"
            if a["rx_ns"] is not None and b["rx_ns"] is not None:
                margin = ((a["rx_ns"] - a["tx_before_ns"]) - (b["rx_ns"] - b["tx_before_ns"]))
                if abs(margin) >= 4_000_000:
                    state = "old" if (margin < 0) == old_sign else "new"
                row["rx_ns"] = max(a["rx_ns"], b["rx_ns"])
            pairs.append(row)
            states.append(state)
        control = {"planned_us": origin + update["offset_us"]}
        ranking[side] = edge_result(control, *state_change(pairs, states, control["planned_us"], count), uncertainty)
    return {"reversal": output, "relative_ranking": ranking}


def outage_edges(timeline, directions, drops, origin, settings, uncertainty):
    begin, end = timeline[1:]
    direction, = begin["directions"]
    if set(end["directions"]) != {direction}:
        raise ValueError("recovery does not match outage direction")
    rows, lost = directions[direction], drops.get(direction, {})
    count = settings["confirm_count"]
    before = edge_control(begin, direction, origin)
    after = edge_control(end, direction, origin)
    if not lost:
        raise ValueError("affected direction has no QDISC_DROP witnesses")
    first, last = min(lost), max(lost)
    if (first < count or last + count >= len(rows) or set(lost) != set(range(first, last + 1)) or
            len(lost) < count or any(row["rx_ns"] is None for row in (*rows[first-count:first], *rows[last+1:last+1+count]))):
        raise ValueError("outage or continuous recovery has unmatched probe sequences")
    if any(key != direction for key in drops):
        raise ValueError("unexpected QDISC_DROP on control direction")
    for key, group in directions.items():
        if key == direction:
            continue
        during = [row for row in group if rows[first]["tx_before_ns"] <= row["tx_before_ns"] <= rows[last]["tx_before_ns"]]
        if len(during) < count or any(row["rx_ns"] is None for row in during):
            raise ValueError("unaffected path did not witness continuity")
    first_drop = {**rows[first], "tx_after_ns": lost[first]["drop_ns"] + 999}
    outage = edge_result(before, rows[first-1], first_drop, lost[first + count - 1]["drop_ns"] + 999, uncertainty)
    restored = edge_result(after, rows[last], rows[last+1],
                           max(row["rx_ns"] for row in rows[last+1:last+1+count]), uncertainty)
    return {"outage": {direction: {**outage, "first_kernel_drop": lost[first], "kernel_matched_count": len(lost)}},
            "recovery": {direction: {**restored, "last_kernel_drop": lost[last]}}}


def direction_metrics(directions, raw, origin, case, settings):
    first, last = (origin + case["phases"][0]["at_us"]) * 1000, (origin + case["phases"][-1]["at_us"]) * 1000
    pre, post = settings["pre_switch_window_us"] * 1000, 50_000_000
    output = {}
    for key, rows in directions.items():
        gap = max_gap_ns(rows)
        received = [r for r in rows if r["rx_ns"] is not None]
        output[key] = {"transmitted": len(rows), "received": len(received), "missing": len(rows) - len(received),
                       "max_spacing_us": gap / 1000 if gap is not None else None,
                       "spacing_pass": gap is not None and gap <= settings["max_probe_spacing_us"] * 1000,
                       "first_tx_us": rows[0]["tx_before_ns"] / 1000 - origin,
                       "last_tx_us": rows[-1]["tx_before_ns"] / 1000 - origin,
                       "probe_started_us": raw[key.split("-")[0]]["started_ns"] / 1000 - origin,
                       "pre_and_post_covered": (sum(r["tx_before_ns"] <= first-pre for r in received) >= settings["confirm_count"]
                                                and sum(r["tx_before_ns"] >= last+post for r in received) >= settings["confirm_count"])}
    return output


def phase_records(records, case, origin, bounds, settings):
    result = []
    for row in records:
        if row["choice_lower_us"] is None or not 0 < row["effective_slack_us"] <= 100000 or row["label"] is None:
            continue
        lower, upper = (row[k] for k in ("choice_lower_us", "choice_upper_us"))
        targeted = any(abs(lower - p["at_us"]) <= settings["target_window_us"] for p in case["phases"])
        phase = phase_at(origin + lower, origin + upper, bounds, settings["pre_switch_window_us"])
        result.append({"record_id": row["record_id"], "path": next(c["link"] for c in row["candidates"] if c["selected"]),
                       "selection_bracket_us": [lower, upper], "effective_slack_us": row["effective_slack_us"],
                       "targeted": targeted, "phase": phase})
    targeted = [r for r in result if r["targeted"]]
    return {"records": result, "targeted_n": len(targeted), "targeted_unknown": sum(r["phase"] == "UNKNOWN" for r in targeted)}


def bound_quality(edges, quality):
    spans = {key: (max(row["transition_upper_us"] for row in group.values()) -
                   min(row["transition_lower_us"] for row in group.values())) for key, group in edges.items()}
    return {"spans_us": spans,
            "time_pass": all(row["max_error_us"] <= quality["max_edge_error_us"] for group in edges.values() for row in group.values()),
            "skew_pass": all(value <= quality["max_direction_skew_us"] for value in spans.values())}


def run_analysis(folder, case, quality, settings):
    clock, probes, witness, metrics, records = {}, {}, {}, None, []
    origin = 0
    errors = []
    try:
        launched = json.loads((folder / "run-clock.json").read_text())
        starts = by_event(log_rows(folder / "client.log"))["iot_start"]
        start, = starts
        origin = int(start["origin"])
        if (launched["identity"] != identity(case) or launched["origin_us"] != origin or
                not launched["launched_ns"]["client"] <= origin*1000 <= int(start["t_us"])*1000
                <= launched["client_origin_seen_ns"] <= launched["finished_ns"]):
            raise ValueError("XQUIC origin does not match the same monotonic connection")
        clock.update(origin_us=origin, connection_id=case["connection_id"], xquic_origin_pass=True,
                     netlink_timeline=[])
        timeline = [json.loads(line) for line in (folder / "network.jsonl").read_text().splitlines()]
        clock["netlink_timeline"] = [{d: edge_control(row, d, origin) for d in row["directions"]} for row in timeline[1:]]
        with tempfile.TemporaryDirectory(prefix="stage93c-quic-only-") as temporary:
            overlay = Path(temporary)
            for file in folder.iterdir():
                if file.is_file() and file.suffix != ".pcap":
                    (overlay / file.name).symlink_to(file.resolve())
            stamps = {}
            for side in SIDES:
                for link in LINKS:
                    prefix = "client-" if side == "client" else ""
                    stamps[side, link] = read_capture(folder / f"wire-{side}-{link}.pcap",
                                                      overlay / f"{prefix}link-{link}.pcap", link, case)
            verified = verify(overlay)
            metrics = verified["metrics"]
            records, _ = replay_records("stage93c", case, overlay)
        if case["arm"] == "E0":
            if any(stamps.values()):
                raise ValueError("baseline arm contains probe traffic")
            return clock, probes, {"status": "BASELINE", "errors": errors}, metrics
        raw = {side: json.loads((folder / f"probe-{side}.json").read_text()) for side in SIDES}
        directions, mapped = join_probes(case, origin, raw, stamps, settings["max_mapping_error_us"] * 1000)
        clock.update(mapped)
        probes.update(direction_metrics(directions, raw, origin, case, settings))
        if not mapped["clock_pass"]:
            errors.append("clock offset envelope exceeds 100 us")
        if not mapped["pcap_pass"]:
            errors.append("PCAP/kernel timestamp agreement exceeds 100 us or lacks pairs")
        if not all(row["spacing_pass"] for row in probes.values()):
            errors.append("observed send spacing exceeds 1000 us")
        if not all(row["pre_and_post_covered"] for row in probes.values()):
            errors.append("required pre/post data-plane window not covered")
        if case["scenario"] == "reversal":
            edges = reversal_edges(case, timeline, directions, origin, settings, mapped["mapping_uncertainty_ns"])
            entries = list(edges["reversal"].values())
            bounds = (min(r["transition_lower_us"] for r in entries), max(r["stable_confirmation_us"] for r in entries), None, None)
        else:
            if case["arm"] != "E2":
                raise ValueError("outage without tracefs cannot supply a phase witness")
            drop, markers = kernel_drops((folder / "kernel-events.log").read_text(),
                                         json.loads((folder / "tracefs.json").read_text()), raw, directions, token_for(case))
            clock["tracefs_marker_mono_ns"] = markers
            edges = outage_edges(timeline, directions, drop, origin, settings, mapped["mapping_uncertainty_ns"])
            edge, = edges["outage"].values()
            recovery, = edges["recovery"].values()
            bounds = (edge["transition_lower_us"], edge["stable_confirmation_us"],
                      recovery["transition_lower_us"], recovery["stable_confirmation_us"])
        witness.update(edges=edges, **bound_quality(edges, quality))
        if not witness["time_pass"] or not witness["skew_pass"]:
            errors.append("measured transition or direction bounds exceed 5000 us")
        witness["provisional_bounds_us"] = bounds
    except (OSError, ValueError, KeyError, IndexError, struct.error) as error:
        errors.append(str(error))
    qualified = not errors and "provisional_bounds_us" in witness
    witness.update(phase_records(records, case, origin, witness.get("provisional_bounds_us") if qualified else None, settings),
                   status="VERIFIED" if qualified else "UNKNOWN", errors=errors)
    return clock, probes, witness, metrics


def paired_interval(differences, limit, confidence):
    n = len(differences)
    mean = fmean(differences) if n else None
    half = halfwidth(stdev(differences), n, confidence) if n >= 2 else None
    ci = [mean-half, mean+half] if half is not None else None
    return {"n": n, "differences": differences, "mean": mean, "ci": ci,
            "equivalent": ci is not None and -limit <= ci[0] <= ci[1] <= limit}


def overhead(schedule, metrics, plan, quality, settings):
    groups = defaultdict(dict)
    for case in schedule:
        if case["block"] is not None:
            block = groups[case["shape"], case["load"], case["block"]]
            if case["arm"] in block:
                raise ValueError("duplicate formal arm")
            block[case["arm"]] = case
    data, skipped = defaultdict(list), []
    fields = ("seed", "network_seed", "assignment_seed", "load", "initial", "phases", "workload_sha256")
    for (shape, load, block), arms in groups.items():
        for treatment, control in (("E1", "E0"), ("E2", "E1")):
            if treatment not in arms or control not in arms:
                continue
            a, b = arms[treatment], arms[control]
            ma, mb = metrics.get(a["name"]), metrics.get(b["name"])
            if ma is None or mb is None or not mb["goodput_bps"] or any(
                    row["client_choice_to_send_mean_us"] is None for row in (ma, mb)):
                skipped.append({"block": block, "contrast": f"{treatment}-{control}"})
                continue
            if a["connection_id"] == b["connection_id"] or any(a[k] != b[k] for k in fields):
                raise ValueError("formal measurement pair has unequal inputs")
            data[shape, load, treatment, control].append({
                "block": block, "treatment": a["name"], "control": b["name"],
                "goodput_delta_percent": 100 * (ma["goodput_bps"] / mb["goodput_bps"] - 1),
                "event_delta_pp": ma["event_on_time_pp"] - mb["event_on_time_pp"],
                "send_delta_us": ma["client_choice_to_send_mean_us"] - mb["client_choice_to_send_mean_us"]})
    result = {}
    for (shape, load, arm, control), rows in sorted(data.items()):
        send = summarize([r["send_delta_us"] for r in rows])
        send["equivalent"] = (send["ci_low"] is not None and
                              -quality["max_send_overhead_us"] <= send["ci_low"] <= send["ci_high"] <= quality["max_send_overhead_us"])
        result[f"{shape}/{load}/{arm}-{control}"] = {
            "pairs": rows, "send_mean_us_existing_95pct_bootstrap": send,
            "goodput_90pct": paired_interval([r["goodput_delta_percent"] for r in rows],
                                            quality["max_goodput_delta_percent"], settings["pair_confidence"]),
            "event_90pct": paired_interval([r["event_delta_pp"] for r in rows],
                                          quality["max_event_delta_pp"], settings["pair_confidence"])}
    passed = len(result) == 6 and not skipped and all(
        len(v["pairs"]) == plan["formal_blocks_per_cell"] and all(q["equivalent"] for k, q in v.items() if k != "pairs")
        for v in result.values())
    return {"pair_plan": plan, "formal_comparisons": result, "incomplete_comparisons": skipped, "pass": passed,
            "direction_runs_excluded": sum(r["block"] is None for r in schedule), "raw_xquic_metrics": metrics}


def replay(raw, output):
    frozen = validate_frozen()
    if checksums(raw) != json.loads((raw / "sha256.json").read_text()):
        raise ValueError("raw run checksum differs")
    manifest = json.loads((raw / "manifest.json").read_text())
    quality, settings = limits()
    plan = pair_plan(quality, settings)
    schedule = make_schedule(frozen, plan, settings)
    if (not manifest["source_build_and_frozen_plan_unchanged"] or manifest["frozen_plan_sha256"] != frozen["file_sha256"]
            or manifest["schedule"] != schedule or fingerprint(schedule) != manifest["schedule_sha256"]
            or manifest["quality"] != quality or manifest["settings"] != settings or manifest["pair_plan"] != plan
            or any(manifest[key] != 0 for key in ("sealed_executions", "sealed_payload_reads", "frozen_pilot_executions"))):
        raise ValueError("raw manifest does not match frozen engineering design")
    execution = json.loads((raw / "execution.json").read_text())
    if [r["name"] for r in execution] != [r["name"] for r in schedule]:
        raise ValueError("execution order or completeness differs from manifest")
    clocks, probes, witnesses, metrics, cases, inventory = {}, {}, {}, {}, [], []
    archived = json.loads((ROOT / "results/stage93a/data_quality_summary.json").read_text())["runs"]
    protected = [r["case"] for r in archived] + frozen["cases"]
    for key in ("name", "seed", "network_seed", "assignment_seed"):
        if {row[key] for row in schedule} & {row[key] for row in protected}:
            raise ValueError(f"engineering schedule overlaps a development case {key}")
    protected_workloads = {r.get("workload_sha256") for r in protected}
    for row, outcome in zip(schedule, execution):
        name = row["name"]
        folder = raw / "runs" / name
        workload = file_hash(folder / "workload.bin")
        case = json.loads((folder / "case.json").read_text())
        if case != {**row, "workload_sha256": workload} or workload in protected_workloads:
            raise ValueError("engineering workload or identity differs from schedule")
        cases.append(case)
        if outcome["exit_code"]:
            clocks[name], probes[name], witnesses[name], metrics[name] = {}, {}, {
                "status": "UNKNOWN", "errors": [outcome["error"]], "targeted_n": 0, "targeted_unknown": 0}, None
        else:
            clocks[name], probes[name], witnesses[name], metrics[name] = run_analysis(folder, case, quality, settings)
        inventory.append({**outcome, "connection_id": row["connection_id"], "shape": row["shape"], "load": row["load"],
                          "arm": row["arm"], "workload_sha256": workload, "xquic_verified": metrics[name] is not None,
                          "phase_status": witnesses[name]["status"]})
    perturbation = overhead(cases, metrics, plan, quality, settings)
    direction = [witnesses[r["name"]] for r in cases if r["block"] is None]
    unknown_n = sum(r["targeted_unknown"] for r in direction)
    total_n = sum(r["targeted_n"] for r in direction)
    unknown_rate = unknown_n / total_n if total_n else None
    gate = {"all_direction_witnesses_verified": all(r["status"] == "VERIFIED" for r in direction),
            "unknown_fraction": unknown_rate, "unknown_targeted": unknown_n, "targeted": total_n,
            "unknown_pass": unknown_rate is not None and unknown_rate <= settings["max_unknown_transition_fraction"],
            "instrumentation_equivalence": perturbation["pass"]}
    status = "GO" if all(gate[key] for key in ("all_direction_witnesses_verified", "unknown_pass",
                                             "instrumentation_equivalence")) else "NO-GO"
    payloads = {"manifest": {**manifest, "frozen_plan_review": {k: v for k, v in frozen.items() if k != "cases"},
                             "verdict": status, "gate": gate, "raw_manifest_sha256": file_hash(raw / "sha256.json")},
                "clock_alignment": clocks, "probe_metrics": probes, "phase_witness": witnesses,
                "instrumentation_overhead": perturbation, "run_inventory": inventory}
    output.mkdir(parents=True, exist_ok=True)
    for key, value in payloads.items():
        write_json(output / f"{key}.json", value)
    write_json(output / "sha256.json", {f"{key}.json": file_hash(output / f"{key}.json") for key in payloads})
    print(status, flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("raw", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    output = args.output.resolve()
    archive = ROOT / "results"
    if output == archive or any(p.parent == archive and p.name not in ("stage93c", "runs") for p in (output, *output.parents)):
        parser.error("output must be outside historical results")
    replay(args.raw.resolve(), output)
