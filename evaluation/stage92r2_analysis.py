"""Replay Stage 9.2R2 and apply the frozen timing, trace and coverage gates."""

import argparse
import json
import math
from collections import defaultdict
from pathlib import Path
from statistics import correlation

from stage92_analysis import paired_overhead, selected, short
from stage92_timing import analyze_trial as stage92_timing
from stage92r_analysis import (SEALED, by_event, checksums, engineering_guard, file_hash, load_design, log_rows,
                               observation_rows, precision, read_workload, summary, validate_isolation, validate_labels,
                               verify, verify_seal, analyze_timing as stage92r_timing)
from stage92r2_timing import analyze_trial, timing_summary
from run_stage92r2 import design, schedule


def verify_suite(folder):
    hashes = json.loads((folder / "sha256.json").read_text())
    if hashes != checksums(folder):
        raise ValueError(f"raw checksum mismatch: {folder}")
    manifest = json.loads((folder / "manifest.json").read_text())
    if not manifest["source_and_build_unchanged"]:
        raise ValueError("source or build changed during acquisition")
    outcomes = json.loads((folder / "execution.json").read_text())
    if len({r["case"] for r in outcomes}) != len(outcomes):
        raise ValueError("duplicate execution case")
    return manifest, outcomes, len(hashes)


def file_manifest(case, folder, manifest):
    saved = json.loads((folder / "case.json").read_text())
    digest = file_hash(folder / "workload.bin")
    environment = (folder / "environment.txt").read_text()
    if saved != {**case, "workload_sha256": digest} or manifest["build"]["binary_sha256"] not in environment:
        raise ValueError(f"run or binary differs from manifest: {folder}")
    result = verify(folder)
    if result != json.loads((folder / "summary.json").read_text()):
        raise ValueError(f"run summary differs from replay: {folder}")
    return saved, result["metrics"], environment


def configured_state(decision, phases, origin, case):
    if decision is None:
        return "unsubmitted"
    active = "initial"
    for i, phase in enumerate(phases):
        start = min(r["start_us"] for r in phase["directions"].values()) - origin
        end = max(r["done_us"] for r in phase["directions"].values()) - origin
        if start - 5000 <= decision <= end + 5000:
            return "cutover-uncertain"
        if decision < start - 5000:
            return active
        profile = case["phases"][i]["links"]
        active = ("outage" if any(p[2] == 100 for p in profile.values()) else
                  "restored" if i else "updated")
    return active


def records_for_case(case, folder):
    trace = {side: by_event(log_rows(folder / f"{side}.log")) for side in ("client", "server")}
    origin = int(trace["client"]["iot_start"][0]["origin"])
    lines = (folder / "network.jsonl").read_text().splitlines()
    phases = [json.loads(line) for line in lines[1:]]
    arrivals = {r["id"]: r for r in trace["server"]["iot_rx"]}
    rows = list(observation_rows(case, read_workload(folder / "workload.bin"), trace["client"], arrivals, origin))
    validate_labels(rows)
    for row in rows:
        picked = selected(row)
        decision, received = row["decision_us"], row["received_us"]
        if picked is None:
            state = "unsubmitted"
        else:
            due = decision + row["effective_slack_us"]
            if received is None and due >= case["horizon_us"]:
                state = "unknown"
                picked["effective_label"] = None
            elif received is None:
                state = "unfinished"
            else:
                state = "late" if picked["effective_label"] == 0 else "on-time"
        row["label_state"] = state
        row["configured_network_state"] = configured_state(decision, phases, origin, case)
        row["near_planned_edge"] = decision is not None and any(abs(decision-phase["at_us"]) <= 100_000
                                                                  for phase in case["phases"])
    return rows


def coverage_table(rows, config):
    envs = defaultdict(list)
    for row in rows:
        envs[row["environment"]].append(row)
    result = []
    for environment, group in sorted(envs.items()):
        valid = [r for r in group if len(r["candidates"]) == 2 and short(r)
                 and selected(r)["effective_label"] in (0, 1)]
        counts = {"environment": environment, "split": group[0]["split"], "records": len(group),
                  "short_dual_n": len(valid), "short_path": {link: sum(selected(r)["link"] == link for r in valid)
                                                             for link in ("A", "B")},
                  "short_positive": sum(selected(r)["effective_label"] == 1 for r in valid),
                  "short_negative": sum(selected(r)["effective_label"] == 0 for r in valid),
                  "short_seeds": len({r["seed"] for r in valid}),
                  "near_planned_edge": sum(r["near_planned_edge"] for r in valid),
                  "unobserved_paths": sum(not c["selected"] for r in group for c in r["candidates"]),
                  "label_status": {state: sum(r["label_state"] == state for r in group) for state in
                                   ("unsubmitted", "unfinished", "late", "unknown", "on-time")},
                  "after_window": sum(r["status"] == "after-window" for r in group),
                  "short_slack_us": summary([r["effective_slack_us"] for r in valid])}
        regimes = defaultdict(list)
        for row in valid:
            regimes[row["configured_network_state"]].append(selected(row))
        counts["short_by_configured_state"] = {k: {"n": len(v), "positive": sum(r["effective_label"] for r in v),
                                                      "A": sum(r["link"] == "A" for r in v),
                                                      "B": sum(r["link"] == "B" for r in v)} for k, v in sorted(regimes.items())}
        counts["planning"] = precision(valid, counts, config)
        counts["pass"] = (counts["short_dual_n"] >= config["min_short_rows"] and
                          min(counts["short_path"].values()) >= config["min_rows_per_path"] and
                          min(counts["short_positive"], counts["short_negative"]) >= config["min_negative_labels"] and
                          counts["short_seeds"] >= config["min_seeds"])
        result.append(counts)
    return result


def busy_percent(outcome):
    if "host_before" not in outcome:
        return None
    before = outcome["host_before"]["cpu_jiffies"]
    after = outcome["host_after"]["cpu_jiffies"]
    delta = [b-a for a, b in zip(before[:8], after[:8])]
    return 100 * (1 - sum(delta[3:5])/sum(delta)) if sum(delta) else None


def audit_pairs(items):
    blocks = defaultdict(dict)
    for order, (case, metrics, outcome, affinity) in enumerate(items):
        blocks[case["block"]][case["trace_arm"]] = (order, metrics, outcome, affinity)
    evidence = {}
    for arm in ("full", "quiet"):
        rows = []
        for block, group in sorted(blocks.items()):
            if not {"off", arm} <= group.keys():
                continue
            off_order, base, off, off_affinity = group["off"]
            arm_order, traced, exp, affinity = group[arm]
            if affinity != off_affinity:
                raise ValueError("CPU affinity differs within overhead block")
            a, b = busy_percent(exp), busy_percent(off)
            rows.append({"block": block, "run_order": "before" if arm_order < off_order else "after",
                         "goodput_delta_percent": 100*(traced["goodput_bps"]/base["goodput_bps"]-1),
                         "base_goodput_bps": base["goodput_bps"], "traced_goodput_bps": traced["goodput_bps"],
                         "client_cpu_delta_us": traced["client_cpu_us"]-base["client_cpu_us"],
                         "server_cpu_delta_us": traced["server_cpu_us"]-base["server_cpu_us"],
                         "client_log_delta_us": traced["client_log_total_us"]-base["client_log_total_us"],
                         "client_sha256_us": traced["client_digest_total_us"], "capture_cpu_us": traced["capture_cpu_us"],
                         "host_busy_delta_pp": a-b if None not in (a, b) else None})
        order_stats = {where: summary([r["goodput_delta_percent"] for r in rows if r["run_order"] == where])
                       for where in ("before", "after")}
        variables = ("client_cpu_delta_us", "server_cpu_delta_us", "client_log_delta_us",
                     "client_sha256_us", "capture_cpu_us", "host_busy_delta_pp")
        correlations = {}
        for k in variables:
            points = [(r[k], r["goodput_delta_percent"]) for r in rows if r[k] is not None]
            correlations[k] = (correlation(*zip(*points)) if len(points) > 1 and
                               all(len(set(col)) > 1 for col in zip(*points)) else None)
        evidence[arm] = {"pairs": rows, "order": order_stats, "descriptive_correlations": correlations}
    return evidence


def historical(root, sealed, quality):
    data = {"timing": {}, "rows": [], "cases": [], "hashes": {}}
    for name, read_edge in (("stage92", stage92_timing), ("stage92r", stage92r_timing)):
        folder = root / name / "timing"
        manifest, outcomes, count = verify_suite(folder)
        data["hashes"][f"{name}/timing"] = count
        cases = {r["name"]: r for r in manifest["schedule"]}
        edges = []
        for row in outcomes:
            scenario = row["scenario"] if name == "stage92" else cases[row["case"]]["scenario"]
            if row["exit_code"] or scenario not in ("bandwidth", "reversal", "loss"):
                continue
            path = folder / "runs" / row["case"]
            current = (read_edge(path, scenario, quality) if name == "stage92" else
                       read_edge(path, cases[row["case"]], quality))
            edges.extend({**edge, "case": row["case"]} for edge in current)
        data["timing"][name] = edges
    folder = root / "stage92r" / "overhead"
    manifest, outcomes, count = verify_suite(folder)
    data["hashes"]["stage92r/overhead"] = count
    cases = {r["name"]: r for r in manifest["schedule"]}
    runs = []
    for row in outcomes:
        if row["exit_code"]:
            continue
        case, metrics, _ = file_manifest(cases[row["case"]], folder / "runs" / row["case"], manifest)
        runs.append((case, metrics, row, None))
    data["overhead"] = audit_pairs(runs)
    old_stats = paired_overhead([(case, m) for case, m, _, _ in runs], quality, ("off", "full", "quiet"), "trace_arm")
    sd = {arm: metrics["goodput_delta_percent"]["sd"]
          for arm, metrics in old_stats["effects_full_and_components_vs_off"].items()}
    data["sample_plan"] = {"prior_sd_pp": sd, "target_half_width_pp": 3, "assumed_abs_mean_pp": 2,
                           "normal_approx_blocks": math.ceil((1.96 * max(sd.values()) / 3)**2)}
    folder = root / "stage92r" / "pilot"
    manifest, outcomes, count = verify_suite(folder)
    data["hashes"]["stage92r/pilot"] = count
    cases = {r["name"]: r for r in manifest["schedule"]}
    for row in outcomes:
        if row["exit_code"]:
            raise ValueError("historical pilot failure requires explicit exclusion")
        case = cases[row["case"]]
        engineering_guard(case, sealed)
        path = folder / "runs" / row["case"]
        saved, _, _ = file_manifest(case, path, manifest)
        data["cases"].append(saved)
        data["rows"].extend(records_for_case(case, path))
    return data


def analyze(root, previous, out):
    sealed = json.loads(SEALED.read_text())
    verify_seal(sealed)
    config, base = design(), load_design()
    quality = base["quality"]
    earlier = historical(previous, sealed, quality)
    audit, edges, initial_edges, runs, new_rows, new_cases = {}, [], [], [], [], []
    build = None
    for suite in ("timing", "overhead", "pilot", "bandwidth", "loss"):
        folder = root / suite
        manifest, outcomes, count = verify_suite(folder)
        planned = schedule(suite, config, base)
        if manifest["smoke"] or manifest["schedule"] != planned or manifest["config"] != config or (
                manifest["sealed_sha256"] != file_hash(SEALED)) or manifest["quality"] != quality:
            raise ValueError("manifest differs from frozen design")
        if [r["case"] for r in outcomes] != [r["name"] for r in planned]:
            raise ValueError("fixed schedule incomplete")
        if build is None:
            build = manifest["build"]
        elif manifest["build"] != build:
            identity = ("base_commit", "binary_sha256", "xquic_commit", "boringssl_commit", "tc_sha256")
            if suite not in ("bandwidth", "loss") or any(manifest["build"][k] != build[k] for k in identity):
                raise ValueError("suites used different acquisition sources or binaries")
        failures = []
        for case, row in zip(planned, outcomes):
            engineering_guard(case, sealed)
            if row["exit_code"]:
                failures.append(row)
                continue
            path = folder / "runs" / row["case"]
            if suite in ("timing", "bandwidth", "loss"):
                measured = analyze_trial(path, case, quality)
                if suite == "timing" and case["scenario"] != "reversal":
                    initial_edges.extend(measured)
                else:
                    edges.extend(measured)
            else:
                saved, metrics, environment = file_manifest(case, path, manifest)
                if suite == "overhead":
                    affinity = [line for line in environment.splitlines() if line.startswith("cpu_affinity ")]
                    if len(affinity) != 1:
                        raise ValueError("missing overhead CPU isolation record")
                    runs.append((saved, metrics, row, affinity[0]))
                else:
                    new_cases.append(saved)
                    new_rows.extend(records_for_case(case, path))
        audit[suite] = {"planned": len(planned), "verified": len(planned)-len(failures), "failures": failures,
                        "files": count, "build": manifest["build"]}
    all_cases = earlier["cases"] + new_cases
    validate_isolation([*all_cases, *sealed["cases"]])
    for key in ("seed", "assignment_seed", "network_seed", "workload_sha256"):
        if len({c[key] for c in all_cases}) != len(all_cases):
            raise ValueError(f"development run identity overlaps: {key}")
    cover = {k: coverage_table(rows, config) for k, rows in
             (("previous", earlier["rows"]), ("new", new_rows), ("combined", earlier["rows"] + new_rows))}
    overhead = paired_overhead([(case, metrics) for case, metrics, _, _ in runs],
                               quality, ("off", "full", "quiet"), "trace_arm")
    effects = overhead["effects_full_and_components_vs_off"]
    for arm in ("full", "quiet"):
        effects.setdefault(arm, {})
    budget = {"goodput_delta_percent": quality["max_goodput_delta_percent"],
              "event_on_time_pp": quality["max_event_delta_pp"],
              "client_choice_to_send_mean_us": quality["max_send_overhead_us"]}
    def equivalent(row, limit):
        return row.get("n") == config["overhead_blocks"] and -limit <= row["ci_low"] <= row["ci_high"] <= limit
    trace_gates = {arm: {key: equivalent(effects[arm].get(key, {}), limit) for key, limit in budget.items()}
                   for arm in ("full", "quiet")}
    gates = {"timing": len(edges) == config["calibration_repeats"]*8 and all(edge["pass"] for edge in edges),
             "trace": all(all(group.values()) for group in trace_gates.values()),
             "coverage": len(cover["combined"]) == 4 and all(e["pass"] for e in cover["combined"]),
             "integrity": all(not s["failures"] for s in audit.values()),
             "sealed": len(sealed["cases"]) == 48}
    objects = {"audit": {"decision": "GO" if all(gates.values()) else "NO-GO", "gates": gates, "suites": audit,
                         "sealed_configurations": len(sealed["cases"]), "sealed_runs": 0, "build": build,
                         "previous_files": earlier["hashes"]},
               "timing-edges": edges, "timing-summary": timing_summary(edges), "coverage": cover,
               "overhead": {"effects": effects, "budgets_met": trace_gates, "incomplete_blocks": overhead["incomplete_blocks"],
                            "pairs": audit_pairs(runs), "sample_plan": earlier["sample_plan"]},
               "prior-diagnostics": {"timing": earlier["timing"], "overhead": earlier["overhead"],
                                     "initial_r2_replaced_timing": initial_edges}}
    out.mkdir(parents=True)
    for name, value in objects.items():
        (out / f"{name}.json").write_text(json.dumps(value, indent=2) + "\n")
    with (out / "new-observations.jsonl").open("w") as stream:
        for row in new_rows:
            stream.write(json.dumps(row, separators=(",", ":")) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("raw", type=Path)
    parser.add_argument("previous", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    analyze(args.raw, args.previous, args.output)
