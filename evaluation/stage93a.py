"""Audit only the three archived development pilots and regenerate Stage 9.3-A."""

import argparse
import json
import tarfile
import tempfile
from collections import Counter
from pathlib import Path

from stage92r2_analysis import (by_event, configured_state, engineering_guard, file_hash, file_manifest,
                               log_rows, observation_rows, read_workload, validate_isolation, validate_labels,
                               verify_seal, verify_suite)
from stage93a_features import add_features
from stage93a_statistics import analyze, budget_bin
from verify_stage2 import EVENT, STALE, keyed, require

ROOT = Path(__file__).resolve().parents[1]
BATCHES = {"stage92": 32, "stage92r": 128, "stage92r2": 128}


def verified_archive(results, batch, name):
    batch_root = results / batch
    archive = batch_root / name
    digest = file_hash(archive)
    require(digest == json.loads((batch_root / "sha256.json").read_text())[name],
            f"{batch}/{name} outer checksum mismatch")
    return archive, digest


def extract_pilot(results, batch, target, seal):
    archive, digest = verified_archive(results, batch, "raw/pilot.tar.gz")
    with tarfile.open(archive) as source:
        members = source.getmembers()
        require(all(Path(m.name).parts[:1] == ("pilot",) and ".." not in Path(m.name).parts
                    and (m.isdir() or m.isfile()) for m in members)
                and len({m.name for m in members}) == len(members), "unexpected pilot archive members")
        with source.extractfile("pilot/manifest.json") as stream:
            cases = json.load(stream)["schedule"]
        check_partitions(cases, seal)
        names = {case["name"] for case in cases}
        for member in members:
            parts = Path(member.name).parts
            if len(parts) <= 2:
                require(len(parts) == 1 or parts[1] in
                        {"manifest.json", "execution.json", "sha256.json", "inputs", "execution", "runs"},
                        "unexpected pilot metadata")
            else:
                require((parts[1] == "runs" and parts[2] in names) or
                        (parts[1] in {"inputs", "execution"} and len(parts) == 3 and Path(parts[2]).stem in names),
                        "unlisted case in pilot archive")
        source.extractall(target, members=members, filter="data")
    folder = target / "pilot"
    return folder, digest


def check_partitions(cases, seal):
    verify_seal(seal)
    require(len(seal["cases"]) == 48, "unexpected sealed configuration count")
    environments = {}
    for case in cases:
        require(case["split"] in ("train", "validation"), "non-development case in pilot schedule")
        engineering_guard(case, seal)
        identity = case["split"], case["family"]
        require(environments.setdefault(case["environment"], identity) == identity, "environment identity changed")
    validate_isolation([*cases, *seal["cases"]])


def initial_pilot_history(results, seal):
    name = "raw/initial-overhead-and-pilot.tar.gz"
    archive, digest = verified_archive(results, "stage92", name)
    with tarfile.open(archive) as source:
        with source.extractfile("pilot/manifest.json") as stream:
            manifest = json.load(stream)
        check_partitions(manifest["schedule"], seal)
        with source.extractfile("pilot/execution.json") as stream:
            outcomes = json.load(stream)
    require({r["case"] for r in outcomes} == {c["name"] for c in manifest["schedule"]} and
            len(outcomes) == len(manifest["schedule"]) == len({r["case"] for r in outcomes}),
            "initial pilot execution does not match schedule")
    return {"archive": f"stage92/{name}", "archive_sha256": digest,
            "source_and_build_unchanged": manifest["source_and_build_unchanged"],
            "exit_counts": dict(Counter(str(r["exit_code"]) for r in outcomes)),
            "failure_cases": [r["case"] for r in outcomes if r["exit_code"]],
            "included_in_analysis": False, "reason": "historical failed acquisition superseded by full pilot replay"}


def observed_label(submitted, received, due, horizon):
    if not submitted:
        require(received is None, "delivery without submission")
        return None, "unsubmitted"
    if received is not None:
        return (1, "on-time") if received <= due else (0, "late")
    if due < horizon:
        return 0, "unfinished"
    return None, "unknown"


def causal_order(tick, samples, app, create, submitted, logged, bind, submit_log, sent, fin, rx, ack):
    require(samples and len(samples) == len(logged) and
            tick <= min(samples) <= max(samples) <= app <= create <= submitted <= min(logged)
            <= max(logged) <= bind <= submit_log, "non-causal decision, sample or submission")
    if sent is not None:
        require(submit_log <= sent, "actual send predates submission")
    if rx is not None:
        require(sent is not None and fin is not None and sent <= fin <= rx, "non-causal delivery")
    if ack is not None:
        require(rx is not None and rx <= ack, "non-causal application ACK")


def replay_records(batch, case, folder):
    client, server = (by_event(log_rows(folder / f"{side}.log")) for side in ("client", "server"))
    start, = client["iot_start"]
    origin = int(start["origin"])
    for side in (client, server):
        require(len(side["conn_open"]) == 1, "ambiguous connection identity")
    phases = [json.loads(line) for line in (folder / "network.jsonl").read_text().splitlines()[1:]]
    workload = read_workload(folder / "workload.bin")
    arrives = keyed(server["iot_rx"], "id")
    rows = list(observation_rows(case, workload, client, arrives, origin))
    validate_labels(rows)
    admits = keyed(client["iot_input"], "id")
    require({str(r["record_id"]) for r in rows} == admits.keys(), "admission/workload coverage differs")
    admission_times = sorted(int(r["t_us"]) - origin for r in admits.values())
    app = {r["id"]: r for r in client["iot_app_decision"]}  # Last attempt includes the successful create.
    created = keyed([r for r in client["iot91_create"] if r["outcome"] == "created"], "id")
    binds = keyed(client["iot91_bind"], "id")
    submits = keyed(client["iot_submit"], "id")
    acks = keyed(client["iot_ack"], "id")
    fins = keyed(server["iot91_fin_read"], "stream")
    udp = keyed(client["iot91_udp_tx"], "datagram")
    first_send, wire_links = {}, {}
    for frame in client["iot91_xquic"]:
        if frame["kind"] == "wire_tx":
            socket = udp[frame["tx_datagram"]]
            first_send.setdefault(frame["stream"], (int(socket["at_us"]) - origin, socket["link"]))
            wire_links.setdefault(frame["stream"], set()).add(socket["link"])
    feature_logs = {(r["id"], int(r["path_id"])): int(r["t_us"]) - origin for r in client["iot92_feature"]}
    for row in rows:
        key = str(row["record_id"])
        raw = workload[key]
        due = raw["deadline"] if raw["kind"] == EVENT else min(raw["deadline"], raw["generated"] + raw["maximum_age"])
        label, status = observed_label(key in submits, row["received_us"], due, case["horizon_us"])
        picked = next((c for c in row["candidates"] if c["selected"]), None)
        require(sum(c["selected"] for c in row["candidates"]) == (key in submits) and
                len({c["link"] for c in row["candidates"]}) == len(row["candidates"]) and
                all(c["link"] in ("A", "B") and c["assignment_probability"] == 1 / len(row["candidates"])
                    for c in row["candidates"]), "invalid chosen path identity or propensity")
        receiver = arrives.get(key)
        row.update(batch=batch, family=case["family"], connection_id=f"{batch}/{case['name']}",
                   priority=raw["priority"], generated_us=raw["generated"], deadline_us=raw["deadline"],
                   effective_deadline_us=due, horizon_us=case["horizon_us"], label=label, label_state=status,
                   receiver_stale=bool(receiver and int(receiver["status"]) == STALE),
                   phase=configured_state(row["decision_us"], phases, origin, case),
                   stream_id=None, choice_lower_us=None, choice_upper_us=None, first_send_us=None,
                   first_send_link=None, fin_us=None, app_ack_us=None, legacy_label=None, decision_attempt=None)
        if picked is None:
            continue
        stream = submits[key]["stream_id"]
        require(stream == binds[key]["stream"] == created[key]["stream"] and
                int(binds[key]["home"]) == picked["path_id"], "record, stream and binding disagree")
        send, link = first_send.get(stream, (None, None))
        fin = int(fins[stream]["t_us"]) - origin if stream in fins else None
        ack = int(acks[key]["client_received"]) - origin if key in acks else None
        sampled = [c["sampled_us"] for c in row["candidates"]]
        logged = [feature_logs[key, c["path_id"]] for c in row["candidates"]]
        submitted = int(submits[key]["submitted"]) - origin
        causal_order(row["decision_us"], sampled, int(app[key]["t_us"]) - origin,
                     int(created[key]["t_us"]) - origin, submitted, logged, int(binds[key]["t_us"]) - origin,
                     int(submits[key]["t_us"]) - origin, send, fin, row["received_us"], ack)
        row.update(stream_id=int(stream), choice_lower_us=submitted, choice_upper_us=min(logged), first_send_us=send,
                   first_send_link=link, wire_links=sorted(wire_links.get(stream, ())), fin_us=fin, app_ack_us=ack,
                   legacy_label=picked["effective_label"], decision_attempt=int(created[key]["attempt"]),
                   budget_bin=budget_bin(row["effective_slack_us"]),
                   bin_sensitive=len({budget_bin(due - t) for t in (row["decision_us"], submitted, min(logged))}) > 1,
                   selection_bin_uncertain=budget_bin(due - submitted) != budget_bin(due - min(logged)))
        if due < case["horizon_us"] or (row["received_us"] is not None and row["received_us"] < case["horizon_us"]):
            require(label == row["legacy_label"], "comparable label differs from historical replay")
        picked["effective_label"] = label
        picked["absolute_label"] = observed_label(True, row["received_us"], raw["deadline"], case["horizon_us"])[0]
    add_features(rows, admission_times)
    return rows, len(workload) - len(rows)


def audit(results, temporary):
    seal = json.loads((results / "stage92/sealed-manifest.json").read_text())
    failed_acquisition = initial_pilot_history(results, seal)
    suites = {}
    for batch, expected in BATCHES.items():
        folder, digest = extract_pilot(results, batch, temporary / batch, seal)
        manifest, outcomes, files = verify_suite(folder)
        schedule = {c["name"]: c for c in manifest["schedule"]}
        require(len(schedule) == len(manifest["schedule"]) == len(outcomes) == expected and
                schedule.keys() == {r["case"] for r in outcomes}, "pilot schedule/execution mismatch")
        suites[batch] = folder, digest, manifest, outcomes, files, schedule
    check_partitions([c for *_, schedule in suites.values() for c in schedule.values()], seal)
    records, evidence, run_evidence = [], {}, []
    workload_split, identifiers, trajectory_split = {}, set(), {}
    for batch, (folder, digest, manifest, outcomes, files, schedule) in suites.items():
        print(f"Replaying {batch}: {len(outcomes)} development runs", flush=True)
        evidence[batch] = dict(archive_sha256=digest, inner_files_verified=files, build=manifest["build"],
                               exit_counts=dict(Counter(str(row["exit_code"]) for row in outcomes)),
                               scheduled=len(schedule), runs_verified=0)
        for execution in outcomes:
            case = schedule[execution["case"]]
            run = folder / "runs" / case["name"]
            if execution["exit_code"]:
                run_evidence.append({"batch": batch, "case": case, "exit_code": execution["exit_code"]})
                continue
            require(case["trace"] == "full" and case["path_scheduler"] == "random-path" and case["features"],
                    "not a fully traced random-path pilot")
            saved, _, _ = file_manifest(case, run, manifest)
            require(workload_split.setdefault(saved["workload_sha256"], case["split"]) == case["split"],
                    "cross-partition workload reuse")
            trajectory = file_hash(run / "network.jsonl")
            require(trajectory_split.setdefault(trajectory, case["split"]) == case["split"],
                    "cross-partition network timeline reuse")
            rows, out_of_window = replay_records(batch, case, run)
            for row in rows:
                identity = row["connection_id"], row["record_id"]
                require(identity not in identifiers, "duplicate scoped record ID")
                identifiers.add(identity)
            records.extend(rows)
            run_evidence.append({"batch": batch, "case": saved, "exit_code": 0, "network_log_sha256": trajectory,
                                 "records_before_horizon": len(rows), "workload_rows_at_or_after_horizon": out_of_window})
            evidence[batch]["runs_verified"] += 1
    isolation = {"seal_sha256": seal["seal_sha256"], "sealed_configurations": len(seal["cases"]),
                 "sealed_configurations_executed_by_this_analysis": 0, "sealed_payloads_read": 0,
                 "sealed_access": "closed configuration manifest only",
                 "cross_partition_checks": ["environment", "family", "network profile and normalized shape",
                                            "network/connection/assignment seeds", "workload SHA-256",
                                            "actual network timeline SHA-256"]}
    return records, {"archives": evidence, "runs": run_evidence, "isolation": isolation,
                     "stage92_initial_acquisition": failed_acquisition,
                     "label_contract": {
                         "population": "successfully submitted record and its selected path only",
                         "effective_deadline": "event deadline; state min(deadline, generated + maximum_age)",
                         "positive": "verified application receipt at or before effective deadline",
                         "negative": "verified application receipt after deadline, or no receipt and deadline < horizon",
                         "unknown": "unsubmitted, unselected, or no receipt and deadline >= horizon",
                         "after_window": "verified drain receipt labeled by its actual time and reported separately",
                     }}


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8", newline="\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--records", type=Path, help="optional normalized audit JSONL, outside the historical archive")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="stage93a-") as temporary:
        records, audit_info = audit(ROOT / "results", Path(temporary))
    output = analyze(records)
    output["data_quality_summary"].update(audit_info)
    args.output.mkdir(parents=True, exist_ok=True)
    for name, content in output.items():
        write_json(args.output / f"{name}.json", content)
    if args.records:
        with args.records.open("w", encoding="utf-8", newline="\n") as stream:
            for row in records:
                stream.write(json.dumps(row, sort_keys=True, allow_nan=False) + "\n")


if __name__ == "__main__":
    main()
