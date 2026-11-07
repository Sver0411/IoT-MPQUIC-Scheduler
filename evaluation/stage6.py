"""Re-audit Stage 5, export analysis-ready tables, and render Stage 6 figures."""

import argparse
import csv
import hashlib
import json
import re
from pathlib import Path

from verify_stage2 import STATE, keyed, read_events, read_workload, require
from verify_stage4 import transmitted_streams
from verify_stage5 import flat, matrix

ROOT = Path(__file__).resolve().parents[1]
DATASETS = ("seed17", "seed29", "repair17")
RX_STATUS = {"1": "updated", "2": "stale", "3": "accepted", "4": "duplicate"}


def csv_value(value):
    return "" if value is None else str(value)


def write_csv(path, rows):
    fields = dict.fromkeys(key for row in rows for key in row)
    with path.open("w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def read_csv(path):
    with path.open(newline="", encoding="utf-8") as source:
        return list(csv.DictReader(source))


def record_table(run, dataset, case):
    workload = read_workload(run / "workload.bin")
    client = read_events(run / "client.log")
    server = read_events(run / "server.log")
    start, = client["iot_start"]
    origin = int(start["origin"])
    inputs = keyed(client["iot_input"], "id")
    submitted = keyed(client.get("iot_submit", []), "id")
    received = keyed(server.get("iot_rx", []), "id")
    acked = keyed(client.get("iot_ack", []), "id")
    replaced = keyed(client.get("iot_replace", []), "dropped")
    obsolete = keyed(client.get("iot_obsolete", []), "id")
    plans = keyed(client.get("iot_plan", []), "id")
    require(submitted.keys() == received.keys() == acked.keys(), f"{run}: incomplete record trace")
    outcomes = (received.keys(), replaced.keys(), obsolete.keys())
    require(set.union(set(), *outcomes) == workload.keys() and sum(map(len, outcomes)) == len(workload),
            f"{run}: record outcomes are incomplete or overlap")

    def relative(row, field):
        return int(row[field]) - origin if row else None

    for id_, source in workload.items():
        submission, rx, ack = submitted.get(id_), received.get(id_), acked.get(id_)
        disposition = "received" if rx else ("replaced" if id_ in replaced else "obsolete")
        rx_us = relative(rx, "received")
        yield {
            "dataset": dataset, "case": case["name"], "record_id": int(id_),
            "kind": "state" if source["kind"] == STATE else "event",
            "sensor_id_or_event_type": source["source"], "sequence_or_event_id": source["serial"],
            "priority": source["priority"], "severity": source["severity"],
            "payload_bytes": source["payload_len"], "payload_hash_fnv1a64": source["hash"],
            "scheduled_release_us": source["release"], "generated_us": source["generated"],
            "deadline_us": source["deadline"], "maximum_age_us": source["maximum_age"],
            "admitted_us": relative(inputs[id_], "t_us"),
            "submitted_us": relative(submission, "submitted"), "server_complete_us": rx_us,
            "client_ack_us": relative(ack, "client_received"),
            "end_to_end_us": rx_us - source["generated"] if rx else None,
            "stream_id": rx["stream_id"] if rx else None, "home_path_id": plans.get(id_, {}).get("home"),
            "disposition": disposition, "receiver_status": RX_STATUS[rx["status"]] if rx else None,
            "deadline_met": int(rx["on_time"]) if rx else 0,
            "superseding_input_id": replaced[id_]["input"] if id_ in replaced else None,
        }


def repair_timeline(run, case):
    client = read_events(run / "client.log")
    server = read_events(run / "server.log")
    start, = client["iot_start"]
    origin = int(start["origin"])
    _, cut, restore = [json.loads(line) for line in (run / "network.jsonl").read_text().splitlines()]
    probe = keyed(server["iot_rx"], "id")["1"]
    plan = keyed(client["iot_plan"], "id")["1"]
    require(plan["stream_id"] == probe["stream_id"], "probe Stream changed between endpoints")
    points = [("cut_applied", cut["end_us"]), ("restore_started", restore["begin_us"]),
              ("generated", probe["generated"]), ("deadline", probe["deadline"]),
              ("server_fin", probe["received"])]
    for event, time in points:
        yield {"ablation": case["ablation"], "event": event, "replay_us": int(time) - origin}
    for stream, offset, path, sent in transmitted_streams(run / "client.xquic.log"):
        if stream == probe["stream_id"]:
            yield {"ablation": case["ablation"], "event": "stream_packet_sent",
                   "replay_us": sent - origin, "path_id": path, "stream_id": stream, "offset": offset}


def provenance(run):
    env = (run / "environment.txt").read_text()
    patterns = {
        "exe_sha256": r"^([a-f0-9]{64}) .*/iot-mpquic$",
        "xquic_commit": r"^xquic=([a-f0-9]{40})$",
        "boringssl_commit": r"^boringssl=([a-f0-9]{40})$",
        "stream_pto_patch_sha256": r"^stream_pto_patch=([a-f0-9]{64})$",
    }
    result = {}
    for key, pattern in patterns.items():
        match = re.search(pattern, env, re.M)
        require(match is not None, f"{run}: missing {key}")
        result[key] = match[1]
    return result


def collect(source):
    runs, records, deltas, timeline = [], [], [], []
    audited = {}
    builds = set()
    for dataset in DATASETS:
        directory = source / dataset
        report = matrix(directory)
        require(report == json.loads((directory / "comparison.json").read_text()), f"{dataset}: matrix mismatch")
        manifest = json.loads((directory / "matrix.json").read_text())
        expected_metrics = []
        for number, case in enumerate(manifest["cases"], 1):
            run = directory / case["name"]
            summary = json.loads((run / "summary.json").read_text())
            metrics = {**{key: case[key] for key in ("seed", "scenario", "workload", "policy", "ablation")},
                       **flat(summary)}
            expected_metrics.append({key: csv_value(value) for key, value in metrics.items()})
            build = provenance(run)
            builds.add(tuple(build.values()))
            raw = run.relative_to(source).as_posix()
            runs.append({"dataset": dataset, "suite": manifest["suite"], "run_order": number,
                         **metrics, **build, "raw_run": raw,
                         "capture_a": f"{raw}/link-a.pcap", "capture_b": f"{raw}/link-b.pcap"})
            records.extend(record_table(run, dataset, case))
            if dataset == "repair17":
                timeline.extend(repair_timeline(run, case))
        require(expected_metrics == read_csv(directory / "metrics.csv"), f"{dataset}: stored CSV differs")
        expected_deltas = [{key: csv_value(value) for key, value in row.items()} for row in report["comparisons"]]
        require(expected_deltas == read_csv(directory / "paired-deltas.csv"), f"{dataset}: stored deltas differ")
        deltas.extend({"dataset": dataset, "seed": manifest["seed"], **row} for row in report["comparisons"])
        audited[dataset] = report["verified_runs"]
        print(f"{dataset}: audited {report['verified_runs']} runs", flush=True)
    require(len(builds) == 1, "different binary or dependency provenance across suites")
    return runs, records, deltas, timeline, audited


def write_manifest(source, output):
    files = sorted((file for dataset in DATASETS for file in (source / dataset).rglob("*") if file.is_file()),
                   key=lambda file: file.relative_to(source).as_posix())
    with output.open("w", encoding="utf-8", newline="\n") as manifest:
        for file in files:
            with file.open("rb") as contents:
                digest = hashlib.file_digest(contents, "sha256").hexdigest()
            manifest.write(f"{digest}  {file.relative_to(source).as_posix()}\n")
    return len(files)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT / "results/stage5-wsl")
    parser.add_argument("--out", type=Path, default=ROOT / "results/stage6")
    args = parser.parse_args()
    source, out = args.source.resolve(), args.out.resolve()
    if out == source or source in out.parents:
        parser.error("output must be separate from the Stage 5 archive")
    try:
        from stage6_figures import render

        runs, records, deltas, timeline, audited = collect(source)
        out.mkdir(parents=True, exist_ok=True)
        for name, rows in (("runs.csv", runs), ("records.csv", records), ("paired-deltas.csv", deltas),
                           ("repair-timeline.csv", timeline)):
            write_csv(out / name, rows)
        file_count = write_manifest(source, out / "source-sha256.txt")
        figures = render(runs, deltas, timeline, out / "figures")
        summary = {"datasets": audited, "runs": len(runs), "captures": 2 * len(runs),
                   "records": len(records), "paired_comparisons": len(deltas),
                   "source_files_hashed": file_count, "figures": figures}
        (out / "audit.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(summary, indent=2))
    except (ImportError, OSError, ValueError, KeyError) as exc:
        parser.exit(1, f"Stage 6 failed: {exc}\n")


if __name__ == "__main__":
    main()
