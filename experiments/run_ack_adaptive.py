"""Small paired XQUIC experiment for the opt-in ACK-adaptive path policy."""

import argparse
import json
import os
import random
from collections import Counter, defaultdict
from pathlib import Path
from statistics import fmean, median

from run_stage92 import checksums, provenance
from run_stage93c import ROOT, limits, run_case, write_json
from stage7 import extract
from stage93c import run_analysis
from verify_stage2 import read_workload
from verify_stage91 import by_event, log_rows
from oracle.path_scheduler_oracle import forecast

ARMS = ("proposed-path", "ack-adaptive")
METRICS = ("event_deadline_success", "useful_goodput_bps", "mean_aoi_ms", "aoi_coverage",
           "maximum_age_violation_ratio", "path_a_share", "path_b_share")


def feedback_summary(folder):
    events = by_event(log_rows(folder / "client.log"))
    origin = int(events["iot_start"][0]["origin"])
    workload = read_workload(folder / "workload.bin")
    links = {r["path_id"]: r["link"] for r in events["path_map"]}
    updates = {r["id"]: r for r in events["iot_path_bias"]}
    acks = {r["id"]: r for r in events["iot_ack"]}
    samples = defaultdict(list)
    for row in events["iot_path_sample"]:
        samples[row["id"]].append(row)
    forecasts = {(r["id"], r["path_id"]): r for r in events["iot_path_forecast"]}
    boundaries = [max(r["done_us"] for r in row["directions"].values()) - origin
                  for row in map(json.loads, (folder / "network.jsonl").read_text().splitlines()[1:])]
    chosen, flips, errors, corrections, waits, bias = Counter(), Counter(), defaultdict(list), defaultdict(list), defaultdict(list), defaultdict(list)
    for plan in events["iot_plan"]:
        key, path = plan["id"], plan["home"]
        tick = int(plan["decision_us"])
        phase = sum(tick >= boundary for boundary in boundaries)
        link = links[path]
        chosen[phase, link] += 1
        correction = float(forecasts[key, path]["correction_us"])
        for candidate in samples[key]:
            candidate_path = candidate["path_id"]
            penalty = float(forecasts[key, candidate_path]["correction_us"])
            if penalty:
                corrections[phase, links[candidate_path]].append((tick, penalty))
        estimates = [forecast({**row, **forecasts[key, row["path_id"]]}, workload[key]["payload_len"] + 52,
                              float(plan["slack_us"])) for row in samples[key]]
        base_index = min(range(len(estimates)), key=lambda i: (-estimates[i][1], estimates[i][0]))
        flips[phase, link] += path != samples[key][base_index]["path_id"]
        if key in acks:
            ack = acks[key]
            actual = int(ack["server_received"]) - int(ack["submitted"])
            corrected_error = actual - float(plan["estimate_us"])
            errors[phase, link].append((corrected_error + correction, corrected_error))
            waits[phase, link].append((int(ack["client_received"]) - origin, int(ack["client_received"]) - int(ack["server_received"])))
        if key in updates:
            row = updates[key]
            bias[phase, link].append((int(row["t_us"]) - origin, float(row["bias_us"])))
    summary = {}
    for phase in range(len(boundaries) + 1):
        summary[str(phase)] = {}
        for link in ("A", "B"):
            key = phase, link
            observed, penalties, ack_waits, path_bias = errors[key], corrections[key], waits[key], bias[key]
            summary[str(phase)][link] = {
                "chosen": chosen[key], "acks": len(observed), "changed_from_same_snapshot_baseline": flips[key],
                "base_mean_residual_us": fmean(a for a, _ in observed) if observed else None,
                "base_mae_us": fmean(abs(a) for a, _ in observed) if observed else None,
                "corrected_mae_us": fmean(abs(b) for _, b in observed) if observed else None,
                "first_ack_us": min(t for t, _ in ack_waits) if ack_waits else None,
                "median_ack_return_us": median(wait for _, wait in ack_waits) if ack_waits else None,
                "first_nonzero_correction_us": penalties[0][0] if penalties else None,
                "max_correction_us": max(p for _, p in penalties) if penalties else 0,
                "last_bias_update": max(path_bias) if path_bias else None}
    return {"configuration_ack_boundaries_us": boundaries, "by_submit_phase": summary}


def evaluate(folder, case):
    _, _, witness, verified = run_analysis(folder, case, *limits())
    if witness["errors"] or verified is None:
        raise ValueError(f"{case['name']}: {witness['errors']}")
    metrics, *_ = extract(folder, case)
    a, b = metrics["path_a_udp_rx_bytes"], metrics["path_b_udp_rx_bytes"]
    metrics.update(path_a_share=a / (a + b), path_b_share=b / (a + b))
    return {"case": case, "metrics": {k: metrics[k] for k in METRICS}, "feedback": feedback_summary(folder)}


def main(output, build):
    output.mkdir(parents=True)
    templates = {r["name"]: r for r in json.loads((ROOT / "results/stage93c/manifest.json").read_text())["schedule"]}
    tc = Path("/usr/sbin/tc")
    cpus = sorted(os.sched_getaffinity(0))[:4]
    write_json(output / "build.json", provenance(build, tc))
    rng = random.Random(9342)
    serial = 0
    pairs = []
    for fixture in ("c93-002", "c93-000"):
        for seed in (3_942_000, 3_942_001, 3_942_002):
            pair = {}
            order = list(ARMS)
            rng.shuffle(order)
            for arm in order:
                serial += 1
                name = f"c93-ack-{serial:02d}"
                case = {**templates[fixture], "name": name, "serial": serial, "connection_id": f"c93-engineering-ack-{serial:02d}",
                        "arm": "E0", "seed": seed, "network_seed": seed + 20_000, "assignment_seed": seed + 10_000,
                        "path_scheduler": arm}
                print(f"{name} {case['scenario']} {seed} {arm}", flush=True)
                folder = output / name
                with (output / f"{name}.log").open("w") as log:
                    run_case(folder, case, build, tc, cpus, limits()[1], log)
                case = json.loads((folder / "case.json").read_text())
                pair[arm] = evaluate(folder, case)
                write_json(folder / "summary.json", pair[arm])
            control, adaptive = (pair[arm] for arm in ARMS)
            if control["case"]["workload_sha256"] != adaptive["case"]["workload_sha256"]:
                raise ValueError("paired workload differs")
            deltas = {key: adaptive["metrics"][key] - control["metrics"][key]
                      for key in METRICS if adaptive["metrics"][key] is not None and control["metrics"][key] is not None}
            pairs.append({"scenario": control["case"]["scenario"], "seed": seed, "order": order, "runs": pair, "delta": deltas})
            write_json(output / "summary.json", pairs)
    write_json(output / "sha256.json", checksums(output))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--build", type=Path, required=True)
    args = parser.parse_args()
    main(args.output.resolve(), args.build.resolve())
