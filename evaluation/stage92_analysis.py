"""Recompute engineering results from immutable Stage 9.2 acquisition folders."""

import argparse
import json
import math
from collections import Counter, defaultdict
from pathlib import Path

from stage7_statistics import summarize
from stage8_statistics import fit_logistic, model_metrics, sigmoid
from stage92_timing import analyze_trial, distribution, summarize_timing
from verify_stage2 import EVENT, read_workload
from verify_stage91 import by_event, log_rows
from verify_stage92 import observation_rows, verify


def paired_overhead(runs, quality, arms=("off", "logs", "digest", "capture", "full"), arm_key="trace"):
    blocks = defaultdict(dict)
    for case, metrics in runs:
        block = blocks[case["block"]]
        if case[arm_key] in block:
            raise ValueError("duplicate overhead arm")
        block[case[arm_key]] = case, metrics
    deltas, excluded = defaultdict(list), []
    comparisons = [(arm, "off") for arm in arms if arm != "off"]
    for block, runs_by_arm in sorted(blocks.items()):
        if runs_by_arm.keys() != set(arms):
            excluded.append(block)
            continue
        reference = runs_by_arm["off"][0]
        for case, _ in runs_by_arm.values():
            if any(case[k] != reference[k] for k in ("seed", "assignment_seed", "network_seed", "initial",
                    "phases", "workload_sha256", "load", "budget", "path_scheduler", "horizon_us")):
                raise ValueError("overhead block changed its inputs")
        for treatment, control in comparisons:
            _, a = runs_by_arm[treatment]
            _, b = runs_by_arm[control]
            for key in a:
                if a[key] is not None and b[key] is not None:
                    deltas[treatment, key].append(a[key] - b[key])
            deltas[treatment, "goodput_delta_percent"].append(100 * (a["goodput_bps"] / b["goodput_bps"] - 1)
                                                             if b["goodput_bps"] else None)
    output = {}
    for (arm, key), values in sorted(deltas.items()):
        valid = [value for value in values if value is not None]
        if valid:
            output.setdefault(arm, {})[key] = {**summarize(valid), "differences": values}
    budgets = {"client_choice_to_send_mean_us": quality["max_send_overhead_us"],
               "event_on_time_pp": quality["max_event_delta_pp"],
               "goodput_delta_percent": quality["max_goodput_delta_percent"]}
    budget_pass = {}
    for metric, limit in budgets.items():
        row = output.get("full", {}).get(metric, {})
        low, high = row.get("ci_low"), row.get("ci_high")
        budget_pass[metric] = (low is not None and high is not None and low >= -limit and high <= limit)
    return {"effects_full_and_components_vs_off": output, "incomplete_blocks": excluded,
            "full_trace_equivalence_budget_pass": budget_pass,
            "components_are_marginal": True}


def selected(row):
    matches = [item for item in row["candidates"] if item["selected"]]
    if len(matches) != bool(row["candidates"]):
        raise ValueError("invalid observed path count")
    return matches[0] if matches else None


def short(row):
    return row["effective_slack_us"] is not None and 0 < row["effective_slack_us"] <= 100_000


def development_baselines(rows):
    training = [(r, selected(r)) for r in rows if r["split"] == "train" and len(r["candidates"]) == 2 and short(r)]
    labels = [p["effective_label"] for _, p in training]
    if len(set(labels)) < 2:
        return {"status": "insufficient training labels"}
    def features(row, path):
        return (row["effective_slack_us"] / 1000, (row["effective_slack_us"] - path["estimate_us"]) / 1000)

    xs = [features(row, path) for row, path in training]
    fits = {name: fit_logistic(list(values), labels)
            for name, values in zip(("slack_only", "completion_margin"), zip(*xs))}
    for row in rows:
        path = selected(row)
        if path is not None:
            path["development_scores"] = {"current_probability": path["current_probability"]}
            for (name, (intercept, slope)), x in zip(fits.items(), features(row, path)):
                path["development_scores"][name] = sigmoid(intercept + slope * x)
    return {"status": "fitted on train short dual-candidate observations only", "coefficients": fits}


def coverage(rows):
    groups = defaultdict(list)
    for row in rows:
        for cell in ("all", f"{row['load']}/{row['budget']}/{row['timing']}"):
            groups[row["environment"], cell].append(row)
    output = []
    for (environment, cell), group in sorted(groups.items()):
        candidates = [r for r in group if r["candidates"]]
        samples = [r for r in candidates if short(r) and len(r["candidates"]) == 2]
        seeds = defaultdict(list)
        for row in samples:
            seeds[row["seed"]].append(row)
        counts = Counter(selected(row)["link"] for row in samples)
        labels = Counter(selected(row)["effective_label"] for row in samples)
        yield_per_seed = [len(seeds[seed]) for seed in sorted({r["seed"] for r in group})]
        seed_deltas = []
        for pair in seeds.values():
            differences = []
            for r in pair:
                path = selected(r)
                scores = path.get("development_scores", {})
                if scores:
                    y = path["effective_label"]
                    differences.append((scores["current_probability"]-y)**2 - (scores["slack_only"]-y)**2)
            if differences:
                seed_deltas.append(sum(differences) / len(differences))
        contrast = summarize(seed_deltas) if seed_deltas else {}
        # The planning precision is heuristic and cannot freeze a formal n from this small pilot.
        sd = contrast.get("sd")
        implied = max(30, math.ceil((1.96 * sd / .01)**2)) if sd is not None else None
        window_events = [r for r in group if r["kind"] == EVENT]
        model_scores = {}
        if samples and selected(samples[0]).get("development_scores"):
            for name in selected(samples[0])["development_scores"]:
                pairs = [(p["development_scores"][name], p["effective_label"]) for p in map(selected, samples)]
                model_scores[name], _ = model_metrics(pairs)
        output.append({"environment": environment, "cell": cell, "split": group[0]["split"],
                       "records": len(group), "connection_seeds": len(yield_per_seed),
                       "record_status": dict(Counter(r["status"] for r in group)),
                       "blocked_records": sum(r["blocked_seen"] for r in group),
                       "no_candidate_records": sum(r["no_candidate_seen"] for r in group),
                       "timeout_records": sum(r["timeout"] for r in group),
                       "effective_slack_us": distribution([r["effective_slack_us"] for r in candidates]),
                       "budget_counts": dict(Counter("expired" if r["effective_slack_us"] <= 0 else
                                                     "short" if short(r) else "long" for r in candidates)),
                       "short_dual_n": len(samples), "short_path": dict(counts),
                       "short_positive": labels[1], "short_negative": labels[0],
                       "short_seeds": len(seeds), "short_near_switch": sum(r["near_planned_edge"] for r in samples),
                       "event_generated": len(window_events), "event_on_time": sum(r["event_on_time"] for r in window_events),
                       "short_yield_per_seed": distribution(yield_per_seed),
                       "descriptive_short_scores": model_scores,
                       "descriptive_seed_brier_delta": contrast,
                       "illustrative_seeds_for_brier_halfwidth_0_01": implied})
    return output


def analyze(root, out, checksums, validate_isolation, verify_seal):
    out.mkdir(parents=True)
    seal = json.loads((root / "seal").read_text())
    verify_seal(seal)
    summaries = {}
    all_cases, workload_splits, runs, rows, edges = [], {}, [], [], []
    for suite in ("timing", "overhead", "pilot"):
        folder = root / suite
        if checksums(folder) != json.loads((folder / "sha256.json").read_text()):
            raise ValueError(f"archive digest mismatch: {suite}")
        manifest = json.loads((folder / "manifest.json").read_text())
        quality = manifest["config"]["quality"]
        identity = ("base_commit", "binary_sha256", "xquic_commit", "boringssl_commit", "tc_sha256")
        if any(manifest["build"][k] != seal["build"][k] for k in identity) or not manifest["source_and_build_unchanged"]:
            raise ValueError("acquisition or build fingerprint mismatch")
        schedule = {r["name"]: r for r in manifest["schedule"]}
        results = json.loads((folder / "execution.json").read_text())
        if schedule and set(schedule) != {r["case"] for r in results}:
            raise ValueError("execution log differs from schedule")
        verified = 0
        if suite == "pilot":
            all_cases.extend(schedule.values())
        for result in results:
            directory = folder / "runs" / result["case"]
            if result["exit_code"] != 0:
                continue
            if suite == "timing":
                edges.extend({**edge, "case": result["case"]}
                             for edge in analyze_trial(directory, result["scenario"], quality))
            else:
                case = json.loads((directory / "case.json").read_text())
                planned = schedule[case["name"]]
                if {k: v for k, v in case.items() if k != "workload_sha256"} != planned:
                    raise ValueError("case differs from manifest")
                if case["workload_sha256"] != checksums_for_workload(directory) or manifest["build"]["binary_sha256"] not in (
                        directory / "environment.txt").read_text():
                    raise ValueError("workload or binary differs from manifest")
                actual = verify(directory)
                if actual != json.loads((directory / "summary.json").read_text()):
                    raise ValueError("recomputed result differs from original")
                if suite == "overhead":
                    runs.append((case, actual["metrics"]))
                else:
                    if workload_splits.setdefault(case["workload_sha256"], case["split"]) != case["split"]:
                        raise ValueError("workload overlaps partitions")
                    client, server = [by_event(log_rows(directory / f"{role}.log")) for role in ("client", "server")]
                    origin = int(client["iot_start"][0]["origin"])
                    arrivals = {row["id"]: row for row in server["iot_rx"]}
                    for row in observation_rows(case, read_workload(directory / "workload.bin"), client, arrivals, origin):
                        row["near_planned_edge"] = short(row) and any(
                            abs(row["decision_us"] - phase["at_us"]) <= 100_000 for phase in case["phases"])
                        rows.append(row)
            verified += 1
        summaries[suite] = {"planned": len(results), "verified": verified,
                            "failed": [r["case"] for r in results if r["exit_code"] != 0],
                            "hashed_files": len(json.loads((folder / "sha256.json").read_text()))}
    validate_isolation([*all_cases, *seal["cases"]])
    fits = development_baselines(rows)
    timing = summarize_timing(edges)
    overhead = paired_overhead(runs, quality)
    pilot = coverage(rows)
    reasons = []
    if any(result["failed"] for result in summaries.values()):
        reasons.append("acquisition-or-integrity-failure")
    if not timing or any(item["qc_pass"] != item["expected"] for item in timing.values()):
        reasons.append("switch-timing-outside-budget")
    if overhead["incomplete_blocks"] or not all(overhead["full_trace_equivalence_budget_pass"].values()):
        reasons.append("trace-equivalence-not-established")
    for item in pilot:
        if item["cell"] == "all" and (item["short_dual_n"] < 200 or min(item["short_path"].get(link, 0)
                for link in ("A", "B")) < 80 or item["short_seeds"] < 20 or
                not min(item["short_positive"], item["short_negative"])):
            reasons.append(f"insufficient-short-budget-coverage:{item['environment']}")
    if not pilot:
        reasons.append("no-pilot-coverage")
    with (out / "pilot-records.jsonl").open("w") as file:
        for row in rows:
            file.write(json.dumps(row, separators=(",", ":")) + "\n")
    objects = {"timing-edges": edges, "timing-summary": timing, "overhead": overhead, "pilot-coverage": pilot,
               "audit": {"acquisition": summaries, "design_isolation_verified": True,
                         "verified_development_workload_hashes": len(workload_splits),
                         "sealed_cases": len(seal["cases"]), "sealed_runs": 0, "baseline_fits": fits,
                         "decision": "NO-GO" if reasons else "GO", "decision_reasons": reasons}}
    for name, value in objects.items():
        (out / f"{name}.json").write_text(json.dumps(value, indent=2) + "\n")


def checksums_for_workload(directory):
    from stage92_design import file_hash
    return file_hash(directory / "workload.bin")


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "experiments"))
    from run_stage92 import checksums
    from stage92_design import validate_isolation, verify_seal

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("raw", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    analyze(args.raw, args.output, checksums, validate_isolation, verify_seal)
