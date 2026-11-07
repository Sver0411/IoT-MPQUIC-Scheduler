"""Independently replay Stage 8 choices and validate the raw transport evidence."""

import argparse
import json
from collections import defaultdict
from pathlib import Path

from oracle import app_scheduler_oracle as app, path_scheduler_oracle as path
from verify_stage2 import event_rows, keyed, read_workload, require
from verify_stage7 import verify_archive, verify_run as transport


def verify_paths(case, records, plans, client, origin):
    available = {int(row["path_id"]): int(row["t_us"]) for row in client["path_map"]}
    removed = {int(row["path_id"]): int(row["t_us"]) for row in client.get("path_removed", [])}
    forecasts = {(row["id"], row["path_id"]): row for row in client["iot_path_forecast"]}
    samples = defaultdict(list)
    for sample in client["iot_path_sample"]:
        key = sample["id"], sample["path_id"]
        samples[sample["id"]].append({**sample, **forecasts.pop(key)})
    require(not forecasts and samples.keys() == {row["id"] for row in plans.values()},
            "extra or missing candidate forecasts")
    decisions = {row["id"]: row for row in client["iot_app_decision"]}
    for plan in plans.values():
        key = plan["id"]
        t = origin + int(plan["decision_us"])
        eligible = {p for p, when in available.items() if when <= t and t < removed.get(p, float("inf"))}
        require(set(int(row["path_id"]) for row in samples[key]) == eligible,
                f"{key}: logged candidates differ from available paths")
        path.validate(case, records[key], plan, samples[key], eligible, decisions[key])


def verify_run(directory):
    case = json.loads((directory / "case.json").read_text())
    rows = list(event_rows(directory / "client.log"))
    start, = [row for row in rows if row["event"] == "iot_start"]
    config, = [row for row in rows if row["event"] == "iot_oracle"]
    require(int(config["assignment_seed"]) == case["assignment_seed"], "assignment seed differs")
    if case["path_scheduler"] == "minrtt":
        require(not any(row["event"] == "iot_path_forecast" for row in rows), "native has bound forecasts")
    records = read_workload(directory / "workload.bin")
    app_result = app.verify(records, rows, int(start["origin"]), case)
    result = transport(directory, path_oracle=verify_paths)
    require(result["build"] == case["provenance"]["build"], "build differs from manifest")
    return {**result, "application_oracle": app_result,
            "path_oracle_decisions": result["submitted"] if case["path_scheduler"] != "minrtt" else 0}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--archive", action="store_true")
    modes.add_argument("--seal", action="store_true")
    args = parser.parse_args()
    result = (verify_archive(args.directory, args.seal) if args.archive or args.seal else verify_run(args.directory))
    print(json.dumps(result, indent=2))
