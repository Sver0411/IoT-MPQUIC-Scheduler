"""Recompute Stage 9.2R timing, trace overhead and coverage from raw evidence."""

import argparse
import json
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path
from statistics import fmean, median, stdev

from stage92_analysis import coverage, paired_overhead, selected, short
from stage92_timing import distribution, probe_series
from verify_stage2 import read_workload
from verify_stage5 import verify_network
from verify_stage91 import by_event, log_rows
from verify_stage92 import observation_rows, verify

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "experiments"))
from run_stage92 import checksums
from run_stage92r import SEALED, engineering_guard, schedule
from stage92_design import file_hash, load_design, validate_isolation, verify_seal
from stage92_probe import INITIAL, SCENARIOS


def summary(values):
    return {**distribution(values), "mean": fmean(values)} if values else distribution(values)


def effect(groups, scenario, stage, old, new, planned):
    if scenario == "loss":
        # Compare successive eight-group loss windows. Isolated drops cannot establish a regime.
        before, after = 8, 8
        count = [g[3] for g in groups]
        for i in range(before, len(groups) - after + 1):
            if not sum(count[i-before:i]) and 9 <= sum(count[i:i+after]) <= 24:
                return groups[i-1][0], groups[i][0], groups[i+after-1][0]
        return None
    if scenario == "bandwidth":
        classified = [None if g[2] is None else g[2] >= 400 for g in groups]
    elif scenario == "outage":
        classified = [g[3] == (4 if stage == 1 else 0) for g in groups]
    else:
        threshold = (old[0] + new[0]) * 500
        classified = [None if g[1] is None else (g[1] - threshold) * (new[0] - old[0]) > 0 for g in groups]
    for i in range(2, len(groups)-4):
        if (classified[i-2:i] == [False, False] and classified[i:i+5] == [True]*5
                and abs(groups[i][0] - planned) <= 60_000):
            return groups[i-1][0], groups[i][0], groups[i+4][0]
    return None


def edge_quality(rows, limit, skew_limit):
    bounds = [(r["last_old_probe_us"], r["first_new_probe_us"]) for r in rows]
    skew = (max(upper for _, upper in bounds) - min(lower for lower, _ in bounds)
            if all(None not in pair for pair in bounds) else None)
    for row, (lower, upper) in zip(rows, bounds):
        row["phase_skew_bound_us"] = skew
        planned = row["planned_us"]
        row["pass"] = (lower is not None and upper is not None and skew is not None and
                       max(abs(planned-lower), abs(upper-planned)) <= limit and skew <= skew_limit)
    return rows


def analyze_timing(folder, case, quality):
    phases = [json.loads(line) for line in (folder / "network.jsonl").read_text().splitlines()]
    if not phases[1:]:
        raise ValueError("missing cutovers")
    origin, prior = phases[0]["origin_us"], phases[0]["links"].copy()
    verify_network(folder, {"initial": INITIAL, "stage92": True,
                            "phases": [{"at_us": at, "links": links} for at, links in SCENARIOS[case["scenario"]]]},
                   origin)
    probes = {side: json.loads((folder / f"{side}.json").read_text()) for side in ("client", "server")}
    rows = []
    for stage, phase in enumerate(phases[1:], 1):
        current = []
        planned = origin + phase["offset_us"]
        for direction, timing in phase["directions"].items():
            side, link = direction.split("-")
            peer = "server" if side == "client" else "client"
            if not (timing["planned_us"] == planned <= timing["start_us"] <= timing["submitted_us"]
                    <= timing["done_us"] <= timing["snapshot_us"]):
                raise ValueError("invalid netlink command chronology")
            groups = [g for g in probe_series(probes[side], probes[peer], link) if abs(g[0]-planned) <= 65_000]
            edge = effect(groups, case["scenario"], stage, prior[link], phase["links"][link], planned)
            arrivals = defaultdict(list)
            for kind, key, seq, sent, received in probes[peer]:
                if kind == "rx" and key == link:
                    arrivals[seq - seq % 4].append(received)
            first_arrival = stable_at = None
            if edge:
                seq_by_time = {sent: seq for kind, key, seq, sent, _ in probes[side]
                               if kind == "tx" and key == link and seq % 4 == 0}
                first_group = arrivals[seq_by_time[edge[1]]]
                first_arrival = min(first_group) if first_group else None
                # Allow 55 ms for absence-based evidence and all prior in-flight probes.
                if case["scenario"] == "loss" or case["scenario"] == "outage" and stage == 1:
                    stable_at = edge[2] + 55_000
                else:
                    stable_at = max((t for g in groups if g[0] <= edge[2]
                                     for t in arrivals[seq_by_time[g[0]]]), default=None)
            current.append({"case": case["name"], "scenario": case["scenario"], "stage": stage,
                            "direction": direction, "planned_us": planned, "command_start_us": timing["start_us"],
                            "netlink_submitted_us": timing["submitted_us"], "qdisc_ack_us": timing["done_us"],
                            "last_old_probe_us": edge[0] if edge else None, "first_new_probe_us": edge[1] if edge else None,
                            "first_observed_receive_us": first_arrival, "stable_observation_us": stable_at,
                            "stability_basis": ("loss-window" if case["scenario"] == "loss" else
                                "absence-window" if case["scenario"] == "outage" and stage == 1 else "receives"),
                            "edge_error_us": edge[1]-planned if edge else None,
                            "absolute_bound_us": max(abs(planned-edge[0]), abs(edge[1]-planned)) if edge else None,
                            "stability_error_us": stable_at-planned if stable_at else None})
        for row in current:
            counterpart = next((r for r in current if r["direction"].split("-")[1] == row["direction"].split("-")[1]
                                and r is not row), None)
            row["client_minus_server_us"] = (row["edge_error_us"] - counterpart["edge_error_us"]
                if row["direction"].startswith("client-") and counterpart and
                None not in (row["edge_error_us"], counterpart["edge_error_us"]) else None)
        edge_quality(current, quality["max_edge_error_us"], quality["max_direction_skew_us"])
        for row in current:
            row["loss_edge_is_statistical"] = case["scenario"] == "loss"
            if row["loss_edge_is_statistical"]:
                row["pass"] = False
        rows.extend(current)
        prior.update(phase["links"])
    return rows


def queue_effect(raw, burst, origin):
    source = burst["side"]
    peer = "server" if source == "client" else "client"
    link = burst["link"]
    sent = {seq: t for kind, name, seq, t, _ in raw[source] if kind == "burst_tx" and name == link}
    received = {seq: (t, arrival) for kind, name, seq, t, arrival in raw[peer] if kind == "burst_rx" and name == link}
    if not sent or not received or any(sent.get(seq) != t or arrival < t for seq, (t, arrival) in received.items()):
        raise ValueError("no correlated burst datagrams")
    first, last = min(sent.values()), max(sent.values())
    planned = origin + burst["at_us"]
    if not (planned <= first <= planned+10_000 and last < planned+burst["duration_us"]+1000
            and last-first >= burst["duration_us"]*.8):
        raise ValueError("burst did not cover the scheduled interval")
    delay = {}
    for side, target in ((source, peer), (peer, source)):
        for name in ("a", "b"):
            series = probe_series(raw[side], raw[target], name)
            windows = {"before": (first-110_000, first-10_000), "during": (first+15_000, last),
                       "after": (last+600_000, last+750_000)}
            delay[f"{side}-{name}"] = {k: summary([g[1] for g in series if a <= g[0] < b and g[1] is not None])
                                       for k, (a, b) in windows.items()}
    target = delay[f"{source}-{link}"]
    controls = [metrics for name, metrics in delay.items() if name != f"{source}-{link}"]
    raised = target["during"]["p50"] - target["before"]["p50"]
    max_control = max(abs(r["during"]["p50"] - r["before"]["p50"]) for r in controls)
    recovered = abs(target["after"]["p50"] - target["before"]["p50"])
    return {"passed": raised >= 5000 and max_control <= 5000 and recovered <= 5000,
            "first_us": first-origin, "last_us": last-origin, "sent": len(sent), "received": len(received),
            "queue_delta_us": raised, "control_delta_us": max_control, "recovery_delta_us": recovered, "delay_us": delay}


def queue_observations(rows, result, client, origin):
    snapshots = defaultdict(list)
    picks = Counter()
    def period(t):
        if t < result["first_us"]:
            return "before"
        if t <= result["last_us"]:
            return "burst"
        return "drain" if t < result["last_us"]+600_000 else "after"

    datagrams = {r["datagram"]: r for r in client["iot91_udp_tx"]}
    wire = defaultdict(set)
    for frame in client["iot91_xquic"]:
        if frame["kind"] == "wire_tx":
            datagram = datagrams[frame["tx_datagram"]]
            wire[period(int(datagram["at_us"])-origin), datagram["link"]].add(frame["tx_datagram"])
    for row in rows:
        if row["decision_us"] is None:
            continue
        phase = period(row["decision_us"])
        for candidate in row["candidates"]:
            key = phase, candidate["link"]
            snapshots[key].append(candidate)
            if candidate["selected"]:
                picks[key] += 1
    fields = ("rtt_us", "rttvar_us", "scheduled_bytes", "cwnd_bytes", "in_flight_bytes", "rate_Bps", "pto")
    return {f"{phase}/{link}": {"selections": picks[phase, link], "actual_stream_datagrams": len(wire[phase, link]),
                               **{k: summary([r[k] for r in records]) for k in fields}}
            for phase, link in sorted(snapshots.keys() | wire.keys()) for records in (snapshots[phase, link],)}


def validate_labels(rows):
    seen = set()
    for row in rows:
        key = row["run"], row["record_id"]
        if key in seen:
            raise ValueError("duplicate record identity")
        seen.add(key)
        path = selected(row)
        if path is not None:
            if abs(sum(c["assignment_probability"] for c in row["candidates"])-1) > 1e-9:
                raise ValueError("invalid random selection probabilities")
            if any(c["effective_label"] is not None or c["absolute_label"] is not None or c["received_us"] is not None
                   for c in row["candidates"] if not c["selected"]):
                raise ValueError("unobserved path carries a label")
            received = row["received_us"]
            label = int(received is not None and row["status"] != "after-window" and
                        received <= row["decision_us"] + row["effective_slack_us"])
            absolute = int(received is not None and row["status"] != "after-window" and
                           received <= row["decision_us"] + row["absolute_slack_us"])
            if label != path["effective_label"] or absolute != path["absolute_label"] or received != path["received_us"]:
                raise ValueError("selected path label differs from delivery evidence")


def precision(rows, counts, config):
    by_seed = defaultdict(list)
    for row in rows:
        if row["environment"] == counts["environment"] and len(row["candidates"]) == 2 and short(row):
            by_seed[row["seed"]].append(selected(row))
    loss = [fmean((r["current_probability"] - r["effective_label"])**2 for r in group) for group in by_seed.values()]
    if len(loss) < 2:
        return {}
    sd = stdev(loss)
    total = len(loss)
    demand = [(config["min_short_rows"], counts["short_dual_n"]),
              (config["min_negative_labels"], counts["short_negative"])]
    demand.extend((config["min_rows_per_path"], counts["short_path"].get(link, 0)) for link in ("A", "B"))
    return {"observed_seed_brier_sd": sd, "approx_current_halfwidth": 1.96*sd/math.sqrt(total),
            "seeds_for_0_01_halfwidth": max(config["min_seeds"], math.ceil((1.96*sd/.01)**2)),
            "seeds_for_mean_coverage": max(config["min_seeds"], *(math.ceil(need*total/got) for need, got in demand))
            if all(got for _, got in demand) else None}


def analyze(root, output):
    sealed = json.loads(SEALED.read_text())
    verify_seal(sealed)
    output.mkdir(parents=True)
    suites, edges, queues, overhead_runs, pilot_rows, pilot_cases = {}, [], [], [], [], []
    workload_splits = {}
    build = None
    identity = ("base_commit", "binary_sha256", "xquic_commit", "boringssl_commit", "tc_sha256")
    for suite in ("timing", "overhead", "queue", "pilot", "queue-active"):
        folder = root / suite
        manifest = json.loads((folder / "manifest.json").read_text())
        if manifest["smoke"] or not manifest["source_and_build_unchanged"] or manifest["sealed_sha256"] != file_hash(SEALED):
            raise ValueError("invalid acquisition manifest")
        if build is not None and (any(build[k] != manifest["build"][k] for k in identity) or
                                  suite != "queue-active" and build != manifest["build"]):
            raise ValueError("mixed acquisition builds or sources")
        if build is None:
            build = manifest["build"]
        config, quality = manifest["config"], manifest["quality"]
        planned = schedule(suite, config, load_design())
        if planned != manifest["schedule"]:
            raise ValueError("acquisition differs from preregistered schedule")
        actual = json.loads((folder / "execution.json").read_text())
        hashes = json.loads((folder / "sha256.json").read_text())
        if checksums(folder) != hashes or [r["case"] for r in actual] != [r["name"] for r in planned]:
            raise ValueError("incomplete acquisition or changed file")
        failures = []
        for case, outcome in zip(planned, actual):
            engineering_guard(case, sealed)
            if suite == "pilot":
                pilot_cases.append(case)
            directory = folder / "runs" / case["name"]
            if outcome["exit_code"]:
                failures.append(outcome)
                continue
            if suite == "timing":
                edges.extend(analyze_timing(directory, case, quality))
                continue
            saved = json.loads((directory / "case.json").read_text())
            digest = file_hash(directory / "workload.bin")
            if saved != {**case, "workload_sha256": digest} or build["binary_sha256"] not in (
                    directory / "environment.txt").read_text():
                raise ValueError("input or binary fingerprint differs from manifest")
            result = verify(directory)
            if result != json.loads((directory / "summary.json").read_text()):
                raise ValueError("summary differs from archived evidence")
            if suite == "overhead":
                overhead_runs.append((saved, result["metrics"]))
                continue
            client, server = [by_event(log_rows(directory / f"{side}.log")) for side in ("client", "server")]
            origin = int(client["iot_start"][0]["origin"])
            arrivals = {r["id"]: r for r in server["iot_rx"]}
            rows = list(observation_rows(case, read_workload(directory / "workload.bin"), client, arrivals, origin))
            validate_labels(rows)
            if suite in ("queue", "queue-active"):
                raw = {side: json.loads((directory / f"{side}.json").read_text()) for side in ("client", "server")}
                queue = queue_effect(raw, case["queue_burst"], origin)
                queue["estimated_added_backlog_bytes"] = queue["queue_delta_us"] * case["initial"]["a"][1] / 8
                observations = queue_observations(rows, queue, client, origin)
                expected = case.get("expected_burst_link")
                target_count = observations.get(f"burst/{expected}", {}).get("actual_stream_datagrams", 0)
                queues.append({"case": case["name"], **queue, "event_on_time_pp": result["metrics"]["event_on_time_pp"],
                               "suite": suite, "expected_burst_link": expected, "target_burst_datagrams": target_count,
                               "xquic_candidates": observations})
            else:
                if workload_splits.setdefault(digest, case["split"]) != case["split"]:
                    raise ValueError("workload reused across partitions")
                for row in rows:
                    row["near_planned_edge"] = row["decision_us"] is not None and any(
                        abs(row["decision_us"]-p["at_us"]) <= 100_000 for p in case["phases"])
                    pilot_rows.append(row)
        suites[suite] = {"planned": len(planned), "verified": len(planned)-len(failures),
                         "failed": failures, "files": len(hashes), "build": manifest["build"]}
    validate_isolation([*pilot_cases, *sealed["cases"]])
    cover = coverage(pilot_rows)
    for row in cover:
        if row["cell"] == "all":
            row["precision_planning"] = precision(pilot_rows, row, config)
    overhead = paired_overhead(overhead_runs, quality, ("off", "full", "quiet"), "trace_arm")
    budget = {"goodput_delta_percent": quality["max_goodput_delta_percent"],
              "event_on_time_pp": quality["max_event_delta_pp"],
              "client_choice_to_send_mean_us": quality["max_send_overhead_us"]}
    quiet = overhead["effects_full_and_components_vs_off"].get("quiet", {})
    quiet_pass = {}
    for key, limit in budget.items():
        lower, upper = (quiet.get(key, {}).get(field) for field in ("ci_low", "ci_high"))
        quiet_pass[key] = lower is not None and upper is not None and lower >= -limit and upper <= limit
    by_scenario = defaultdict(list)
    for edge in edges:
        by_scenario[edge["scenario"]].append(edge)
    timing = {name: {"observed": sum(r["edge_error_us"] is not None for r in group),
                     "passed": sum(r["pass"] for r in group), "total": len(group),
                     **{key: summary([r[key] for r in group if r[key] is not None]) for key in
                        ("edge_error_us", "absolute_bound_us", "phase_skew_bound_us", "client_minus_server_us",
                         "stability_error_us")},
                     "qdisc_ack_lag_us": summary([r["qdisc_ack_us"]-r["planned_us"] for r in group])}
              for name, group in sorted(by_scenario.items())}
    active_queue = [r for r in queues if r["suite"] == "queue-active"]
    gates = {"timing": bool(edges) and all(r["pass"] for r in edges),
             "trace": not overhead["incomplete_blocks"] and all(quiet_pass.values()),
             "queue": len(active_queue) == suites["queue-active"]["planned"] and
                      all(r["passed"] and r["target_burst_datagrams"] for r in active_queue),
             "integrity": all(not s["failed"] for s in suites.values()), "coverage": False}
    environments = [r for r in cover if r["cell"] == "all"]
    gates["coverage"] = len(environments) == 4 and all(r["short_dual_n"] >= config["min_short_rows"] and
        r["short_seeds"] >= config["min_seeds"] and min(r["short_positive"], r["short_negative"]) >= config["min_negative_labels"]
        and min(r["short_path"].get(link, 0) for link in ("A", "B")) >= config["min_rows_per_path"] for r in environments)
    objects = {"audit": {"decision": "GO" if all(gates.values()) else "NO-GO", "gates": gates, "suites": suites,
                         "sealed_configurations": len(sealed["cases"]), "sealed_runs": 0, "build": build},
               "timing-edges": edges, "timing-summary": timing, "queue": queues, "pilot-coverage": cover,
               "overhead": {**overhead, "quiet_equivalence_budget_pass": quiet_pass}}
    for name, value in objects.items():
        (output / f"{name}.json").write_text(json.dumps(value, indent=2) + "\n")
    with (output / "pilot-records.jsonl").open("w") as stream:
        for row in pilot_rows:
            stream.write(json.dumps(row, separators=(",", ":")) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("raw", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    analyze(args.raw, args.output)
