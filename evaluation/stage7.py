"""Recompute the Stage 7 archive from immutable per-run evidence."""

import argparse
import hashlib
import json
import statistics
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from stage6 import write_csv
from stage7_statistics import calibration, heldout_calibration, paired_deltas, summarize_rows
from verify_stage2 import ACCEPTED, EVENT, STATE, UPDATED, keyed, read_events, read_workload, require
from verify_stage4 import transmitted_streams
from verify_stage5 import cpu_metrics, fraction, path_usage, percentile_ms
from verify_stage7 import verify_run

US = 1_000_000
ROOT = Path(__file__).resolve().parents[1]


def json_file(path):
    return json.loads(path.read_text())


def jain(values):
    values = [value for value in values if value is not None]
    return fraction(sum(values) ** 2, len(values) * sum(v * v for v in values))


def aoi(updates, horizon):
    # Age grows at unit speed between accepted receiver updates.
    segments, violations = [], 0
    if not updates:
        return segments, horizon
    violations += updates[0][0]
    for i, (arrival, generated, maximum_age) in enumerate(updates):
        end = updates[i + 1][0] if i + 1 < len(updates) else horizon
        low, high = arrival - generated, end - generated
        if high > low:
            segments.append((low, high))
            violations += max(0, high - max(low, maximum_age))
    return segments, violations


def age_fields(segments):
    span = sum(high - low for low, high in segments)
    if not span:
        return dict(mean_aoi_ms=None, peak_aoi_ms=None, p95_aoi_ms=None, covered_us=0)
    peak = max(high for _, high in segments)
    low, high = 0, peak
    while high > low:
        mid = (low + high) // 2
        if sum(max(0, min(mid, end) - start) for start, end in segments) >= .95 * span:
            high = mid
        else:
            low = mid + 1
    area_twice = sum(end**2 - start**2 for start, end in segments)
    return dict(mean_aoi_ms=area_twice / (2 * span * 1000), peak_aoi_ms=peak / 1000,
                p95_aoi_ms=low / 1000, covered_us=span)


def event_key(row):
    return row["source"], row["serial"]


def extract(run, meta):
    source = read_workload(run / "workload.bin")
    client, server = (read_events(run / f"{role}.log") for role in ("client", "server"))
    start, = client["iot_start"]
    origin, horizon = int(start["origin"]), meta["horizon_us"]
    window_end = origin + horizon
    inputs = keyed(client["iot_input"], "id")
    submits = keyed(client.get("iot_submit", []), "id")
    arrivals = keyed(server.get("iot_rx", []), "id")
    plans = keyed(client.get("iot_plan", []), "id")
    samples = {(r["id"], r["path_id"]): r for r in client.get("iot_path_sample", [])}
    replaced = keyed(client.get("iot_replace", []), "dropped")
    obsolete = keyed(client.get("iot_obsolete", []), "id")
    acks = keyed(client.get("iot_ack", []), "id")
    records, predictions = [], []
    by_sensor, updates = defaultdict(list), defaultdict(list)
    for id_, item in source.items():
        rx, submit, plan = arrivals.get(id_), submits.get(id_), plans.get(id_)
        completion = int(rx["received"]) - origin if rx else None
        received = completion is not None and completion < horizon
        budget = min(item["deadline"], item["generated"] + item["maximum_age"]) if item["kind"] == STATE else item["deadline"]
        status = int(rx["status"]) if received else None
        row = {**meta, **item, "record_id": int(id_), "kind": "state" if item["kind"] == STATE else "event",
               "admitted": int(id_ in inputs), "stream_id": submit["stream_id"] if submit else None,
               "submitted_us": int(submit["submitted"]) - origin if submit else None,
               "actual_completion_us": completion, "complete_in_window": int(received),
               "conditional_latency_us": completion - item["generated"] if received else None,
               "receiver_status": {1: "updated", 2: "stale", 3: "accepted", 4: "duplicate"}.get(status),
               "acked": int(id_ in acks), "deadline_met": int(received and completion <= item["deadline"]),
               "budget_met": int(received and completion <= budget),
               "disposition": ("received" if received else "replaced" if id_ in replaced else
                               "obsolete" if id_ in obsolete else "unfinished" if submit else
                               "pending" if id_ in inputs else "outside-window")}
        records.append(row)
        if item["kind"] == STATE and id_ in inputs:
            by_sensor[item["source"]].append(row)
        if plan:
            path = samples[id_, plan["home"]]
            slack = float(plan["slack_us"])
            size = item["payload_len"]
            decision = int(plan["decision_us"])
            predictions.append({**row, "selected_path": int(plan["home"]),
                                "decision_us": decision, "slack_us": slack,
                                "estimated_completion_delay_us": float(plan["estimate_us"]),
                                "actual_completion_delay_us": completion - decision if received else None,
                                "predicted_probability": float(plan["probability"]),
                                "srtt_us": int(path["rtt_us"]), "rttvar_us": int(path["rttvar_us"]),
                                "queued_bytes": int(path["scheduled_bytes"]),
                                "in_flight_bytes": int(path["in_flight_bytes"]),
                                "bandwidth_Bps": int(path["rate_Bps"]), "recent_loss": float(path["observed_loss"]),
                                "modeled_loss": float(path["loss"]),
                                "pto": int(path["pto"]),
                                "payload_bin": "<1200" if size < 1200 else "1200–2999" if size < 3000 else ">=3000",
                                "slack_bin": "<=0" if slack <= 0 else "<100ms" if slack < 100_000 else
                                "100–399ms" if slack < 400_000 else ">=400ms"})
    for rx in arrivals.values():
        end = int(rx["received"]) - origin
        if end < horizon and int(rx["status"]) == UPDATED:
            updates[int(rx["source"])].append((end, int(rx["generated"]) - origin, int(rx["maximum_age"])))

    sensors, age_segments = [], []
    for sensor, group in sorted(by_sensor.items()):
        segments, violations = aoi(updates[sensor], horizon)
        age_segments.extend(segments)
        sensors.append({**meta, "sensor": sensor, "generated": len(group),
                        "useful": sum(r["receiver_status"] == "updated" for r in group),
                        "on_time": sum(r["deadline_met"] for r in group),
                        "received": sum(r["complete_in_window"] for r in group),
                        "max_age_violation_us": violations, **age_fields(segments)})

    unique_events = {event_key(r) for r in records if r["kind"] == "event" and r["admitted"]}
    events = [r for r in records if r["receiver_status"] == "accepted"]
    states = [r for r in records if r["kind"] == "state" and r["admitted"]]
    on_time_events = sum(r["deadline_met"] for r in events)
    event_latencies = [r["conditional_latency_us"] for r in events]
    useful = [r for r in records if r["receiver_status"] in ("accepted", "updated")]
    dispositions = Counter(r["disposition"] for r in records)
    sent = [entry for entry in transmitted_streams(run / "client.xquic.log", include_bytes=True)
            if origin <= entry[3] < window_end]
    offsets = set()
    repeat_count, repeat_bytes = 0, 0
    for sid, offset, _, _, size in sent:
        key = sid, offset
        if key in offsets:
            repeat_count += 1
            repeat_bytes += size
        offsets.add(key)
    stream_udp_bytes = sum(entry[4] for entry in sent)
    homes = {r["stream_id"]: r["home"] for r in plans.values()}
    cross = [entry for entry in sent if entry[0] in homes and entry[2] != homes[entry[0]]]
    network = [json.loads(line) for line in (run / "network.jsonl").read_text().splitlines()]
    paths = path_usage(client, server, network, origin, horizon)
    stats, = [r for r in client["quic_stats"] if r["phase"] == "1"]
    age = age_fields(age_segments)
    result = {
        **meta, "events_generated": len(unique_events), "events_received": len(events), "events_on_time": on_time_events,
        "event_deadline_success": fraction(on_time_events, len(unique_events)),
        "event_deadline_miss": fraction(len(unique_events) - on_time_events, len(unique_events)),
        "event_delivery": fraction(len(events), len(unique_events)),
        **{f"event_latency_p{p}_ms": percentile_ms(event_latencies, p) for p in (50, 95, 99)},
        "states_generated": len(states), "states_received": sum(r["complete_in_window"] for r in states),
        "state_deadline_success": fraction(sum(r["deadline_met"] for r in states), len(states)),
        "useful_state_delivery": fraction(sum(r["receiver_status"] == "updated" for r in states), len(states)),
        "stale_states": sum(r["receiver_status"] == "stale" for r in states),
        "duplicate_events": sum(r["receiver_status"] == "duplicate" for r in records),
        "mean_useful_state_age_ms": (statistics.fmean(r["conditional_latency_us"] for r in states
                                      if r["receiver_status"] == "updated") / 1000
                                     if any(s["useful"] for s in sensors) else None),
        "replaced": dispositions["replaced"], "obsolete": dispositions["obsolete"],
        "unfinished": dispositions["unfinished"], "pending": dispositions["pending"],
        **age, "aoi_coverage": fraction(age["covered_us"], len(sensors) * horizon),
        "maximum_age_violation_ratio": fraction(sum(s["max_age_violation_us"] for s in sensors), len(sensors) * horizon),
        "jain_delivery": jain([s["useful"] / s["generated"] for s in sensors]),
        "jain_freshness": jain([1 - s["max_age_violation_us"] / horizon for s in sensors]),
        "useful_goodput_bps": 8 * US * sum(r["payload_len"] for r in useful) / horizon,
        "quic_detected_loss_ratio": fraction(int(stats["lost"]), int(stats["sent"])),
        "stream_repeat_offsets": repeat_count, "stream_repeat_udp_payload_bytes": repeat_bytes,
        "stream_repeat_byte_ratio": fraction(repeat_bytes, stream_udp_bytes),
        "cross_path_packets": len(cross), "cross_path_packet_ratio": fraction(len(cross), len(sent)),
        "cross_path_udp_payload_bytes": sum(entry[4] for entry in cross),
        "cross_path_byte_ratio": fraction(sum(entry[4] for entry in cross), stream_udp_bytes),
        **{f"path_{link.lower()}_udp_rx_bytes": info["received_udp_payload_bytes_in_window"] for link, info in paths.items()},
        **{f"path_{link.lower()}_utilization": info["modeled_usable_capacity_fraction"] for link, info in paths.items()},
        "total_udp_rx_payload_bytes": sum(int(r["bytes"]) for trace in (client, server) for r in trace["udp_rx"]
                                          if origin <= int(r["recv_us"]) < window_end),
        "total_udp_tx_payload_bytes": sum(int(r["bytes"]) for trace in (client, server) for r in trace["udp_tx"]
                                          if origin <= int(r["t_us"]) < window_end),
        "client_cpu_percent": cpu_metrics(client)["one_core_percent"],
        "server_cpu_percent": cpu_metrics(server)["one_core_percent"],
    }
    timeline = []
    if meta["scenario"] == "repair":
        cut, restore = network[1]["end_us"] - origin, network[2]["begin_us"] - origin
        probe = records[0]
        require(homes.get(probe["stream_id"]) == "0" and
                any(sid == probe["stream_id"] and path == "0" and cut <= time - origin < restore
                    for sid, _, path, time, _ in sent), "repair probe did not transmit on impaired path")
        result.update(cross_during_outage=sum(cut <= entry[3] - origin < restore for entry in cross),
                      complete_before_restore=sum(r["complete_in_window"] and
                                                  cut <= r["actual_completion_us"] < restore for r in records),
                      useful_before_restore=sum(cut <= r["actual_completion_us"] < restore for r in useful),
                      probe_latency_ms=(probe["conditional_latency_us"] / 1000
                                        if probe["conditional_latency_us"] is not None else None),
                      probe_before_restore=int(probe["complete_in_window"] and probe["actual_completion_us"] < restore))
        for kind in ("event", "state"):
            candidates = [r["actual_completion_us"] - cut for r in useful
                          if r["kind"] == kind and r["actual_completion_us"] >= cut]
            result[f"{kind}_recovery_ms"] = min(candidates) / 1000 if candidates else None
        points = [("cut", cut), ("restore", restore), ("generated", probe["generated"]),
                  ("deadline", probe["deadline"]), ("received", probe["actual_completion_us"])]
        timeline.extend({**meta, "event": label, "relative_us": time} for label, time in points if time is not None)
        timeline.extend({**meta, "event": "stream_sent", "relative_us": time - origin, "path": path, "offset": offset}
                        for sid, offset, path, time, _ in sent if sid == probe["stream_id"])
    return result, records, sensors, predictions, timeline


def collect(directories):
    runs, records, sensors, predictions, timeline = [], [], [], [], []
    manifests = []
    builds = set()
    for root in directories:
        manifest = json_file(root / "experiment-manifest.json")
        suite = "factorial" if manifest["suite"] == "high-extension" else manifest["suite"]
        paired = {}
        workload_hashes = defaultdict(dict)
        manifests.append(manifest)
        if suite == "sensitivity":
            controls = {case["seed"]: case for case in manifest["cases"] if case["cell"] == "full"}
            for case in manifest["cases"]:
                reference = controls[case["seed"]]
                changed = sum(case["tuning"][key] != value for key, value in reference["tuning"].items())
                changed += case["recovery_k"] != reference["recovery_k"]
                require(changed == (case["cell"] != "full") and case["policy"] == reference["policy"] and
                        case["path_scheduler"] == reference["path_scheduler"] and
                        case["ablation"] == reference["ablation"], "sensitivity comparison changed multiple controls")
        for case in manifest["cases"]:
            name = case["name"]
            raw = root / "raw" / name
            evidence = verify_run(raw)
            require(evidence == json_file(raw / "summary.json"), f"{name}: saved verification differs")
            require(json_file(raw / "case.json") == case, f"{name}: run manifest differs")
            require(evidence["build"]["exe_sha256"] == manifest["executable_sha256"], f"{name}: wrong executable")
            builds.add(tuple(evidence["build"].items()))
            input_file = root / "inputs" / f"{case['workload']}--{case['seed']}.bin"
            digest = hashlib.sha256(input_file.read_bytes()).hexdigest()
            require(digest == evidence["workload_sha256"], f"{name}: raw replay differs from seed input")
            workload_hashes[case["workload"]][case["seed"]] = digest
            execution = json_file(root / "execution" / f"{name}.json")
            require(datetime.fromisoformat(execution["start_utc"]) < datetime.fromisoformat(execution["end_utc"]),
                    f"{name}: invalid wall time order")
            invariant = (case["network_seed"], case["initial"], case["phases"], case["horizon_us"],
                         evidence["workload_sha256"], evidence["flow"], evidence["build"],
                         (case["recovery_k"], case["tuning"]) if suite != "sensitivity" else None)
            key = case["scenario"], case["seed"]
            require(invariant == paired.setdefault(key, invariant), f"{name}: paired controls differ")
            meta = {"suite": suite, **{key: case[key] for key in
                    ("scenario", "seed", "network_seed", "cell", "policy", "path_scheduler", "workload",
                     "horizon_us", "repair", "ablation", "experiment_order", "recovery_k",
                     "application_scheduler")}, **case["tuning"]}
            result, rec, sen, pred, time = extract(raw, meta)
            runs.append({**result, **execution, **evidence["build"], **evidence["flow"],
                         "raw_run": f"raw/{root.name}/raw/{name}", "workload_sha256": evidence["workload_sha256"],
                         "receiver_teardown": evidence.get("receiver_teardown", "quic-close"),
                         "client_teardown": evidence.get("client_teardown", "quic-close"),
                         "transition_lag_us": evidence["transition_lag_us"]})
            records.extend(rec)
            sensors.extend(sen)
            predictions.extend(pred)
            timeline.extend(time)
        for profile, hashes in workload_hashes.items():
            require(len(set(hashes.values())) == len(hashes), f"{suite}/{profile}: duplicate seed payloads")
    require(len(builds) == 1, "mixed endpoint, XQUIC, TLS, or patch fingerprints")
    return manifests, runs, records, sensors, predictions, timeline, dict(next(iter(builds)))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("suites", nargs="+", type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--figures", action="store_true")
    args = parser.parse_args()
    out = args.out.resolve()
    suites = [suite.resolve() for suite in args.suites]
    legacy = [path for path in (ROOT / "results").iterdir()
              if path.name in {"stage6", *(f"stage{number}-wsl" for number in range(1, 6))}]
    if any(out == root or root in out.parents for root in [*legacy, *suites]):
        parser.error("write the analysis outside historical and raw evidence directories")
    if (out / "archive-sha256.txt").exists():
        parser.error("write recomputed tables to a separate directory from the sealed archive")
    manifests, runs, records, sensors, predictions, timeline, build = collect(suites)
    deltas = list(paired_deltas(runs))
    stats = list(summarize_rows(runs, deltas))
    scores, bins = calibration(predictions)
    for subdir in ("metrics", "statistics"):
        (out / subdir).mkdir(parents=True, exist_ok=True)
    tables = {"runs.csv": runs, "paired-deltas.csv": deltas, "calibration.csv": predictions,
              "metrics/records.csv": records, "metrics/sensors.csv": sensors, "metrics/repair-timeline.csv": timeline,
              "statistics/summary.csv": stats, "statistics/calibration-scores.csv": scores,
              "statistics/calibration-bins.csv": bins}
    for file, rows in tables.items():
        if rows:
            write_csv(out / file, rows)
    (out / "statistics/calibration-heldout.json").write_text(json.dumps(heldout_calibration(predictions), indent=2) + "\n")
    (out / "experiment-manifest.json").write_text(json.dumps(manifests, indent=2) + "\n")
    figures = []
    if args.figures:
        from stage7_figures import render
        figures = render(runs, stats, bins, timeline, out / "figures")
    seeds_by_condition = defaultdict(set)
    for row in runs:
        seeds_by_condition[row["suite"], row["scenario"]].add(row["seed"])
    coverage = defaultdict(dict)
    for (suite, scene), seeds in sorted(seeds_by_condition.items()):
        coverage[suite][scene] = len(seeds)
    audit = {"status": "passed", "suites": dict(Counter(r["suite"] for r in runs)), "runs": len(runs),
             "captures": len(runs) * 2, "records": len(records), "predictions": len(predictions),
             "paired_deltas": len(deltas), "seeds_per_condition": dict(coverage), "build": build, "figures": figures}
    (out / "audit.json").write_text(json.dumps(audit, indent=2) + "\n")
    print(json.dumps(audit, indent=2))


if __name__ == "__main__":
    main()
