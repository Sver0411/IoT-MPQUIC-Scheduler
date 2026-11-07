"""Verify Stage 7 transport, fixed-window accounting, path binding and captures."""

import argparse
import hashlib
import json
import math
from collections import Counter, defaultdict
from pathlib import Path

from stage6 import provenance
from verify_stage2 import (ACCEPTED, DUPLICATE, EVENT, STALE, STATE, UPDATED, keyed, read_events,
                           read_workload, received_frames, require, verify_links)
from verify_stage4 import transmitted_streams, verify_affinity
from verify_stage5 import verify_network


def verify_run(root):
    case = json.loads((root / "case.json").read_text())
    client, server = (read_events(root / f"{role}.log") for role in ("client", "server"))
    for trace in (client, server):
        require(len(trace["handshake"]) == 1 and
                [row["count"] for row in trace["paths_validated"]] == ["2"], "invalid transport lifecycle")
    start, = client["iot_start"]
    config, = client["iot_experiment"]
    end, = client["iot_window_end"]
    origin, horizon = int(start["origin"]), case["horizon_us"]
    require(horizon <= int(end["elapsed"]) <= horizon + 100_000, "measurement cutoff missed")
    teardowns = {name: endpoint_teardown(root, role, trace, origin + horizon)
                 for name, role, trace in (("client_teardown", "client", client),
                                           ("receiver_teardown", "server", server))}
    require(start["policy"] == case["policy"] and config["path"] == case["path_scheduler"] and
            config["repair"] == case["repair"] and int(config["horizon_us"]) == horizon and
            config["cc"] == "cubic" and config["pacing"] == "1", "unexpected experiment configuration")
    tuning, = client["iot_tuning"]
    require({key: float(value) for key, value in tuning.items() if key not in ("t_us", "event")} ==
            case["tuning"], "parameter settings do not match manifest")
    if case["policy"].startswith("proposed-"):
        recovery, = client["iot_recovery_config"]
        ablation, = client["iot_ablation"]
        require(int(recovery["k"]) == case["recovery_k"] and ablation["name"] == case["ablation"] and
                int(recovery["event_burst"]) == case["tuning"]["event_burst"],
                "unexpected application configuration")
    records = read_workload(root / "workload.bin")
    inputs = keyed(client["iot_input"], "id")
    require(len(records) == int(start["records"]) and inputs.keys() ==
            {id_ for id_, row in records.items() if row["release"] < horizon}, "incorrect replay inputs")
    for id_, row in inputs.items():
        require(all(int(row[k]) == records[id_][k] for k in ("kind", "source", "serial", "release")),
                "replay input metadata changed")
    submits = keyed(client.get("iot_submit", []), "id")
    arrivals = keyed(server.get("iot_rx", []), "id")
    acks = keyed(client.get("iot_ack", []), "id")
    replaced = keyed(client.get("iot_replace", []), "dropped")
    obsolete = keyed(client.get("iot_obsolete", []), "id")
    dropped = replaced.keys() | obsolete.keys()
    require(acks.keys() <= arrivals.keys() <= submits.keys() <= inputs.keys() and
            dropped <= inputs.keys() and not (dropped & submits.keys()) and
            not (replaced.keys() & obsolete.keys()), "inconsistent record lifecycle")
    if case["policy"] == "fifo":
        require(list(map(int, submits)) == sorted(map(int, submits)) and not dropped,
                "FIFO queue order or retention changed")
    if case["ablation"] == "no-replace":
        require(not replaced, "replacement occurred in ablation")
    streams = keyed(list(submits.values()), "stream_id")
    require(all(int(sid) % 4 == 2 and 0 < int(row["active_tx"]) <= int(start["tx_window"])
                for sid, row in streams.items()), "invalid client stream or application window")
    verify_arrivals(records, submits, arrivals, acks, origin)

    frames = [received_frames(root / f"{role}.xquic.log") for role in ("client", "server")]
    links = verify_links(root, client, server, *frames, False)
    network = verify_network(root, case, origin)
    for index, (row, phase) in enumerate(zip(network[1:], case["phases"])):
        partial = {link for link, (_, _, loss) in phase["links"].items() if 0 < loss < 100}
        if partial:
            require(row["random_loss_seed"] == case["network_seed"] + 4 * index,
                    "partial loss lacks the preregistered phase seed")
            for direction, qdisc in row["qdiscs"].items():
                side, link = direction.split("-")
                if link in partial:
                    seed = row["random_loss_seed"] + (side == "server") + (2 if link == "b" else 0)
                    require(f"seed {seed}" in qdisc, "kernel qdisc did not confirm its seed")
    cross = []
    if config["path"] == "proposed-path":
        plans, sent, cross = verify_affinity(root, client, config["repair"] == "cross")
        require(plans.keys() == streams.keys() and
                all(plan["id"] == streams[sid]["id"] for sid, plan in plans.items()),
                "bound streams lack record plans")
        verify_estimates(case, records, plans, client)
    else:
        require(not client.get("iot_packet_path") and not client.get("iot_plan"),
                "native transport unexpectedly uses a bound path")
        sent = list(transmitted_streams(root / "client.xquic.log"))
    require(all(sid in streams for sid, _, _, _ in sent), "wire transmission without submitted record")
    sent_before_cutoff = [row for row in sent if origin <= row[3] < origin + horizon]
    return {
        "status": "passed", "case": case["name"], "records": len(records), "submitted": len(submits),
        "received": len(arrivals), "replaced": len(replaced), "obsolete": len(obsolete),
        "acked": len(acks), "captured_paths": links,
        "workload_sha256": hashlib.sha256((root / "workload.bin").read_bytes()).hexdigest(),
        "flow": {field: int(start[field]) for field in ("tx_window", "initial_uni_credit", "send_packets")},
        "build": provenance(root),
        "cross_path_packets": len(cross), "stream_packets_in_window": len(sent_before_cutoff),
        "transition_lag_us": max((row["end_us"] - origin - row["offset_us"] for row in network[1:]), default=0),
        **{name: result for name, result in teardowns.items() if result},
    }


def endpoint_teardown(root, role, trace, cutoff):
    final, = trace["exit"]
    closed, = trace["conn_closed"]
    stderr = (root / f"{role}.stderr").read_text()
    require(closed["error"] == "0" and int(closed["t_us"]) >= cutoff and
            not trace.get("invalid_stream"), f"{role} failed during measurement")
    if final["ok"] == "1":
        require(not stderr, f"unexpected {role} diagnostic")
        return None
    unfinished = [row for row in trace.get("stream_closed", []) if row["fin"] == "0"]
    timed_out = trace.get("timeout", [])
    evidence = unfinished + timed_out
    require(all(int(row["t_us"]) >= cutoff for row in evidence),
            f"{role} had an incomplete stream or timeout during measurement")
    if role == "client" and stderr.strip() == "UDP recv: Connection refused":
        # A receive error exits the main loop, so the recorded client cutoff proves it occurred afterward.
        return "peer-closed-during-drain"
    require(not stderr and evidence, f"{role} exit failure has no post-window teardown evidence")
    return "close-timeout" if timed_out else "unfinished-streams-at-close"


def verify_arrivals(records, submits, arrivals, acks, origin):
    sensors, events = {}, set()
    for id_, rx in arrivals.items():
        record = records[id_]
        submitted = submits[id_]
        require(all(int(rx[k]) == value + (origin if k in ("generated", "deadline") else 0)
                    for k, value in record.items() if k != "release"), "record metadata changed on wire")
        t = int(rx["received"])
        require(origin + record["release"] <= int(submitted["submitted"]) <= t and
                submitted["stream_id"] == rx["stream_id"] and int(rx["on_time"]) ==
                (t <= origin + record["deadline"]), "invalid submit/receive time or stream")
        source = record["source"]
        latest = (0, 0)
        if record["kind"] == STATE:
            update = int(rx["generated"]), record["serial"]
            previous = sensors.get(source)
            status = UPDATED if previous is None or update > previous else STALE
            latest = sensors[source] = update if status == UPDATED else previous
        else:
            event = source, record["serial"]
            status = DUPLICATE if event in events else ACCEPTED
            events.add(event)
        require(status == int(rx["status"]) and latest ==
                (int(rx["latest_generated"]), int(rx["latest_serial"])), "incorrect receiver state or event identity")
        if id_ in acks:
            ack = acks[id_]
            require(ack["stream_id"] == rx["stream_id"] and ack["submitted"] == submitted["submitted"] and
                    ack["server_received"] == rx["received"] and t <= int(ack["client_received"]) and
                    all(ack[k] == rx[k] for k in ("status", "on_time", "latest_generated", "latest_serial")),
                    "application ACK differs from receiver evidence")


def verify_estimates(case, records, plans, client):
    samples = defaultdict(list)
    for row in client["iot_path_sample"]:
        samples[row["id"]].append(row)
    for plan in plans.values():
        record = records[plan["id"]]
        decision = int(plan["decision_us"])
        slack = record["deadline"] - decision
        if record["kind"] == STATE:
            slack = min(slack, record["maximum_age"] - (decision - record["generated"]))
        predictions = []
        size = record["payload_len"] + 52
        for row in samples[plan["id"]]:
            queued, inflight, scheduled = (int(row[k]) for k in ("queued_bytes", "in_flight_bytes", "scheduled_bytes"))
            require(queued == inflight + scheduled, "queue decomposition differs")
            rtt, spread = int(row["rtt_us"]), max(int(row["rttvar_us"]), 1000)
            duration = rtt * (2.5 if int(row["pto"]) else 0.5) + (
                queued + size) * 1e6 / max(int(row["rate_Bps"]), 15000)
            model_slack = slack - case["tuning"]["slack_margin_us"]
            arrival = min(1, max(0, (model_slack - duration + spread) / (2 * spread))) if model_slack > 0 else 0
            require(abs(float(row["loss"]) - min(.95, float(row["observed_loss"]) *
                                                 case["tuning"]["loss_scale"])) < 1e-10,
                    "loss scaling differs from plan")
            probability = arrival * (1 - float(row["loss"])) ** math.ceil(size / 1200)
            predictions.append((probability, -duration, row["path_id"]))
        best = predictions[0] if case["ablation"] == "fixed-path" else max(predictions, key=lambda p: p[:2])
        require(best[2] == plan["home"] and abs(best[0] - float(plan["probability"])) < 1e-8 and
                abs(-best[1] - float(plan["estimate_us"])) < 1e-5 and slack == float(plan["slack_us"]),
                "binding or logged estimate differs from available paths")


def archive_files(root):
    return sorted((p for p in root.rglob("*") if p.is_file() and p != root / "archive-sha256.txt"),
                  key=lambda p: p.relative_to(root).as_posix())


def checksums(root):
    for file in archive_files(root):
        with file.open("rb") as data:
            digest = hashlib.file_digest(data, "sha256").hexdigest()
        yield f"{digest}  {file.relative_to(root).as_posix()}\n"


def verify_archive(root, seal):
    path = root / "archive-sha256.txt"
    observed = "".join(checksums(root))
    require(observed and (root / "audit.json").is_file(), "archive has no analysis audit")
    if seal:
        with path.open("x", encoding="utf-8", newline="\n") as manifest:
            manifest.write(observed)
    else:
        require(path.read_text(encoding="utf-8") == observed, "archived evidence checksum differs")
    return {"status": "passed", "files": observed.count("\n")}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--archive", action="store_true")
    modes.add_argument("--seal", action="store_true")
    args = parser.parse_args()
    result = (verify_archive(args.directory, args.seal) if args.archive or args.seal else
              verify_run(args.directory))
    print(json.dumps(result, indent=2))
