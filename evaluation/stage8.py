"""Rebuild Stage 8 tables and figures from archived experiment evidence."""

import argparse
import hashlib
import json
from collections import Counter
from datetime import datetime
from pathlib import Path

from stage6 import write_csv
from stage7 import extract
from stage8_statistics import calibration, paired
from verify_stage2 import keyed, read_events, require
from verify_stage5 import percentile_ms
from verify_stage8 import verify_run

ROOT = Path(__file__).resolve().parents[1]


def json_file(path):
    return json.loads(path.read_text())


def dump(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8", newline="\n")


def path_rows(directory, case, records, meta):
    trace = read_events(directory / "client.log")
    plans = keyed(trace.get("iot_plan", []), "id")
    forecasts = {(row["id"], row["path_id"]): row for row in trace.get("iot_path_forecast", [])}
    for sample in trace.get("iot_path_sample", []):
        key = sample["id"]
        plan = plans[key]
        record = records[int(key)]
        forecast = forecasts[key, sample["path_id"]]
        selected = sample["path_id"] == plan["home"]
        slack, payload = int(plan["slack_us"]), record["payload_len"]
        yield {
            **meta, "record_id": int(key), "stream_id": int(plan["stream_id"]),
            "kind": record["kind"], "payload_len": payload,
            "payload_bin": "<1200" if payload < 1200 else "1200–2999" if payload < 3000 else ">=3000",
            "slack_us": slack, "slack_bin": ("<=0" if slack <= 0 else "0–100ms" if slack <= 100_000
                                          else "100–400ms" if slack <= 400_000 else ">400ms"),
            "decision_us": int(plan["decision_us"]), "selected": int(selected),
            "candidates": int(plan["candidates"]),
            "assignment_probability": (1 / int(plan["candidates"]) if case["path_scheduler"] == "random-path"
                                       else None),
            **{field: int(sample[field]) for field in ("path_id", "rtt_us", "rttvar_us", "rate_Bps",
                                                       "scheduled_bytes", "in_flight_bytes", "pto")},
            **{field: int(forecast[field]) for field in ("recent_lost", "recent_sent")},
            "loss": float(sample["observed_loss"]), "estimate_us": float(forecast["estimate_us"]),
            "probability": float(forecast["probability"]),
            **{field: record[field] if selected else None for field in
               ("actual_completion_us", "complete_in_window", "deadline_met", "budget_met")},
        }


def collect(raw):
    runs, records, sensors, forecasts, timeline = [], [], [], [], []
    suites = ("pilot", "paired", "explore")
    manifests = {suite: json_file(raw / suite / "manifest.json") for suite in suites}
    provenance = manifests["paired"]["provenance"]
    config = manifests["paired"]["config"]
    conditions = {}
    coverage = Counter()
    artifacts = set()
    for suite, manifest in manifests.items():
        folder = raw / suite
        cases = manifest["cases"]
        require(manifest["provenance"] == provenance and manifest["config"] == config and
                manifest["suite"] == suite, f"{suite}: manifest differs")
        names = {case["name"] for case in cases}
        observed_names = {p.name for p in (folder / "runs").iterdir() if p.is_dir() and not p.name.startswith(".failed-")}
        require(names == observed_names and len(names) == len(cases), f"{suite}: run inventory differs")
        require([c["experiment_order"] for c in cases] == list(range(1, len(cases) + 1)),
                f"{suite}: experiment order differs")
        for case in cases:
            name = case["name"]
            directory = folder / "runs" / name
            require(case == json_file(directory / "case.json") and case["provenance"] == provenance,
                    f"{name}: execution config differs")
            evidence = verify_run(directory)
            require(evidence == json_file(directory / "summary.json"), f"{name}: raw validation differs")
            execution = json_file(folder / "execution" / f"{name}.json")
            require(datetime.fromisoformat(execution["start_utc"]) < datetime.fromisoformat(execution["end_utc"]),
                    f"{name}: execution timestamps out of order")
            input_file = folder / "inputs" / f"{case['workload']}--{case['seed']}.bin"
            require(hashlib.sha256(input_file.read_bytes()).hexdigest() == evidence["workload_sha256"],
                    f"{name}: replay input differs")
            key = suite, case["scenario"], case["load"], case["seed"]
            same = (evidence["build"], evidence["flow"], evidence["workload_sha256"], case["initial"], case["phases"],
                    case["network_seed"], case["assignment_seed"], case["horizon_us"], case["tuning"], case["recovery_k"])
            require(same == conditions.setdefault(key, same), f"{name}: paired input, transport or build differs")
            coverage[suite, case["scenario"], case["load"], case["cell"]] += 1
            artifacts.add((suite, name))
            meta = {"suite": suite, "run": name, **{k: case[k] for k in
                    ("scenario", "load", "workload", "seed", "policy", "path_scheduler", "cell",
                     "horizon_us", "repair", "experiment_order", "network_seed", "assignment_seed")}}
            metrics, rec, sens, _, points = extract(directory, meta)
            latency = [r["conditional_latency_us"] for r in rec if r["complete_in_window"]]
            runs.append({**metrics, **{f"record_latency_p{p}_ms": percentile_ms(latency, p) for p in (50, 95, 99)},
                         **execution, **evidence["build"], **evidence["flow"],
                         "application_decisions": evidence["application_oracle"]["decisions"],
                         "path_decisions": evidence["path_oracle_decisions"],
                         "client_teardown": evidence.get("client_teardown", "quic-close"),
                         "receiver_teardown": evidence.get("receiver_teardown", "quic-close"),
                         "raw_run": f"raw/{suite}/runs/{name}", "source_commit": provenance["commit"],
                         "workload_sha256": evidence["workload_sha256"]})
            records.extend(rec)
            sensors.extend(sens)
            timeline.extend(points)
            forecasts.extend(path_rows(directory, case, {r["record_id"]: r for r in rec}, meta))
        print(f"{suite}: independently verified {len(cases)} raw runs", flush=True)

    expected = {
        "pilot": 20,
        "paired": (config["paired_seeds"][1] - config["paired_seeds"][0] + 1) * 16,
        "explore": (config["exploration_seeds"][1] - config["exploration_seeds"][0] + 1) * 4,
    }
    require(dict(Counter(r["suite"] for r in runs)) == expected and len(artifacts) == sum(expected.values()),
            "archive does not cover the full experiment design")
    for (suite, scene, load, cell), n in coverage.items():
        require(n == (1 if suite == "pilot" else 20), f"{suite}/{scene}/{load}/{cell}: incomplete seed coverage")
    return manifests, runs, records, sensors, forecasts, timeline


def analyze(raw, out, figures):
    manifests, runs, records, sensors, forecasts, timeline = collect(raw)
    config = manifests["paired"]["config"]
    deltas, summary = paired(runs, config)
    scored, scores, reliability, comparisons, fits = calibration(forecasts)
    exposure = Counter((r["scenario"], r["load"], r["slack_bin"], r["payload_bin"],
                        r["candidates"], r["path_id"])
                       for r in forecasts if r["suite"] == "explore" and r["selected"])
    allocation = [dict(zip(("scenario", "load", "slack_bin", "payload_bin", "candidates", "path_id"), key),
                       n=count) for key, count in sorted(exposure.items())]
    (out / "statistics").mkdir(parents=True, exist_ok=True)
    for file, rows in {
        "runs.csv": runs, "records.csv": records, "paired-deltas.csv": deltas,
        "path-predictions.csv": forecasts, "calibration.csv": scored,
        "statistics/summary.csv": summary, "statistics/sensors.csv": sensors,
        "statistics/repair-timeline.csv": timeline, "statistics/allocation.csv": allocation,
        "statistics/calibration-scores.csv": scores, "statistics/reliability.csv": reliability,
        "statistics/calibration-paired-brier.csv": comparisons,
    }.items():
        write_csv(out / file, rows)
    dump(out / "statistics/calibration-models.json", fits)
    dump(out / "manifest.json", {suite: {"raw_manifest": f"raw/{suite}/manifest.json", "runs": len(data["cases"]),
                                        "jobs": data["jobs"], "provenance": data["provenance"]}
                                 for suite, data in manifests.items()})
    names = []
    if figures:
        from stage8_figures import render
        names = render(runs, summary, reliability, timeline, out / "figures")
    audit = {
        "status": "passed", "runs": len(runs), "captures": len(runs) * 2, "records": len(records),
        "suites": dict(Counter(r["suite"] for r in runs)),
        "application_oracle_decisions": sum(r["application_decisions"] for r in runs),
        "path_oracle_decisions": sum(r["path_decisions"] for r in runs),
        "path_candidate_forecasts": len(forecasts),
        "randomized_two_path_observations": sum(r["suite"] == "explore" and r["selected"] and
                                               r["candidates"] == 2 for r in forecasts),
        "paired_deltas": len(deltas), "figures": names,
    }
    dump(out / "audit.json", audit)
    return audit


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("raw", type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--figures", action="store_true")
    parser.add_argument("--compare", type=Path)
    args = parser.parse_args()
    raw, out = args.raw.resolve(), args.out.resolve()
    historical = ROOT / "results"
    if (raw == out or raw in out.parents or (out / "archive-sha256.txt").exists() or
            any(folder.name.startswith("stage") and folder.name != "stage8" and
                (folder == out or folder in out.parents) for folder in historical.iterdir())):
        parser.error("output must be separate from raw or historical data and sealed archives")
    audit = analyze(raw, out, args.figures)
    if args.compare:
        for file in sorted(p for p in out.rglob("*") if p.is_file()):
            relative = file.relative_to(out)
            require(file.read_bytes() == (args.compare / relative).read_bytes(),
                    f"recomputed table or figure differs: {relative}")
        print("All recomputed outputs match the saved archive")
    print(json.dumps(audit, indent=2))


if __name__ == "__main__":
    main()
