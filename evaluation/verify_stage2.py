"""Validate seeded IoT replay against both endpoints, QUIC traces, and veth captures."""

import json
import re
import struct
import subprocess
import sys
from collections import Counter
from fractions import Fraction
from pathlib import Path

from verify_stage1 import capture_counts, event_rows, keyed, read_events, received_frames, require, tcpdump_address

PREFIX = struct.Struct("!QI")
RECORD = struct.Struct("!4sBBBBIIIQQQQ")
STATE, EVENT = 1, 2
UPDATED, STALE, ACCEPTED, DUPLICATE = 1, 2, 3, 4
REPLACEMENT_POLICIES = {"fifo-replace", "latest-rr"}


def payload_hash(payload):
    value = 14695981039346656037
    for byte in payload:
        value = ((value ^ byte) * 1099511628211) & ((1 << 64) - 1)
    return value


def read_workload(path):
    records = {}
    with path.open("rb") as file:
        while prefix := file.read(PREFIX.size):
            release, length = PREFIX.unpack(prefix)
            require(RECORD.size <= length <= RECORD.size + 16384, "invalid replay record length")
            wire = file.read(length)
            fields = RECORD.unpack_from(wire)
            magic, kind, priority, severity, reserved, size, id_, source, serial, gen, deadline, age = fields
            require(magic == b"IOT1" and not reserved and len(wire) == length == RECORD.size + size
                    and id_ == len(records) + 1 and gen <= release, "invalid replay record")
            records[str(id_)] = dict(release=release, kind=kind, priority=priority, severity=severity,
                                     source=source, serial=serial, generated=gen, deadline=deadline,
                                     maximum_age=age, payload_len=size, hash=payload_hash(wire[RECORD.size:]))
    return records


def expected_pick(policy, pending, feedback, last_sensor, prefer_event):
    if policy == "strict-priority":
        return max(pending, key=lambda id_: pending[id_]["priority"])
    if policy == "edf":
        return min(pending, key=lambda id_: pending[id_]["deadline"])
    if policy == "aoi":
        def gain(id_):
            record = pending[id_]
            if record["kind"] == EVENT:
                return 0
            latest = feedback.get(record["source"])
            if latest is None:
                return 1
            return Fraction(max(0, record["generated"] - latest), record["maximum_age"])
        return max(pending, key=gain)
    if policy == "latest-rr":
        def order(id_):
            record = pending[id_]
            if record["kind"] == EVENT:
                return (not prefer_event, 0, 0)
            sensor = record["source"]
            return (prefer_event, sensor <= last_sensor, sensor)
        return min(pending, key=order)
    return next(iter(pending))


def verify_queue(root, client, workload, origin, policy):
    pending = {}
    by_sensor = {}
    replaced = {}
    confirmed = set()
    submitted_sensors = set()
    later_state = False
    feedback = {}
    last_sensor = 0
    prefer_event = False
    replace = policy in REPLACEMENT_POLICIES
    for row in client["iot_input"]:
        id_ = row["id"]
        require(id_ in workload and all(int(row[key]) == workload[id_][key]
                for key in ("kind", "source", "serial", "release"))
                and int(row["t_us"]) >= origin + workload[id_]["release"], "incorrect input release")
    require(list(keyed(client["iot_input"], "id")) == list(workload), "input IDs differ from replay")
    for row in event_rows(root / "client.log"):
        if row["event"] == "iot_input":
            id_ = row["id"]
            record = workload[id_]
            pending[id_] = record
            if record["kind"] == EVENT:
                continue
            sensor = record["source"]
            later_state |= sensor in submitted_sensors
            if not replace:
                continue
            prior = by_sensor.get(sensor)
            if prior:
                old = pending[prior]
                newer = (record["generated"], record["serial"]) > (old["generated"], old["serial"])
                drop = prior if newer else id_
                del pending[drop]
                replaced[drop] = id_
                id_ = id_ if newer else prior
            by_sensor[sensor] = id_
        elif row["event"] == "iot_replace":
            drop = row["dropped"]
            require(replaced.get(drop) == row["input"] and drop not in confirmed and
                    int(row["source"]) == workload[drop]["source"], "incorrect pending replacement")
            confirmed.add(drop)
        elif row["event"] == "iot_submit":
            id_ = row["id"]
            require(pending and id_ == expected_pick(policy, pending, feedback, last_sensor, prefer_event),
                    f"{policy}: incorrect decision at record {id_}")
            record = pending.pop(id_)
            prefer_event = record["kind"] == STATE
            if prefer_event:
                last_sensor = record["source"]
                if replace:
                    del by_sensor[record["source"]]
                submitted_sensors.add(record["source"])
            require(int(row["expired"]) == (int(row["submitted"]) > origin + record["deadline"]),
                    "incorrect expired flag at submission")
        elif row["event"] == "iot_ack" and workload[row["id"]]["kind"] == STATE:
            sensor = workload[row["id"]]["source"]
            snapshot = int(row["latest_generated"]) - origin
            feedback[sensor] = max(snapshot, feedback.get(sensor, 0))
    require(not pending and confirmed == replaced.keys(), "unverified pending records or replacements")
    if policy == "fifo-replace":
        require(replaced and later_state, "pending replacement or later input for a submitted sensor unverified")
    return replaced


def verify_streams(client, server, submits, received, acks, workload):
    for source, destination, is_ack in ((client, server, False), (server, client, True)):
        finished = keyed(source["stream_fin_tx"], "id")
        closed_tx = keyed([r for r in source["stream_closed"] if r["direction"] == "tx"], "stream_id")
        closed_rx = keyed([r for r in destination["stream_closed"] if r["direction"] == "rx"], "stream_id")
        require(finished.keys() == submits.keys(), "FIN IDs differ from submitted IDs")
        require(len(closed_tx) == len(closed_rx) == len(finished), "incomplete Stream lifecycle")
        offsets = Counter()
        for row in source.get("iot_write_backpressure", []):
            id_ = row["id"]
            result = int(row["result"])
            stream_id = finished[id_]["stream_id"]
            require(row["stream_id"] == stream_id and int(row["offset"]) == offsets[id_] and
                    int(row["offset"]) + int(row["offered"]) == int(finished[id_]["bytes"])
                    and result < int(row["offered"]), "partial write did not resume at the expected offset")
            offsets[id_] += max(result, 0)
        for id_, row in finished.items():
            stream_id = acks[id_]["ack_stream_id"] if is_ack else received[id_]["stream_id"]
            size = 44 if is_ack else RECORD.size + workload[id_]["payload_len"]
            require(row["stream_id"] == stream_id and int(stream_id) % 4 == (3 if is_ack else 2) and
                    int(row["bytes"]) == size, "incorrect stream direction, size, or ID")
            for closed in (closed_tx, closed_rx):
                require(closed[stream_id]["fin"] == "1" and closed[stream_id]["id"] == id_,
                        "stream closed without FIN or with a different record")


def verify_links(root, client, server, client_frames, server_frames, require_both_streams):
    results = {}
    client_links, server_links = (keyed(events["path_map"], "link") for events in (client, server))
    require(client_links.keys() == server_links.keys() == {"A", "B"}, "missing physical link")
    for link, c in client_links.items():
        s = server_links[link]
        path_id = c["path_id"]
        require(s["path_id"] == path_id and s["peer"] == c["local"] and s["local"] == c["peer"],
                f"path mapping disagrees for {link}")
        counts = capture_counts(root / f"link-{link.lower()}.pcap",
                                tcpdump_address(c["local"]), tcpdump_address(c["peer"]))
        dump_log = (root / f"tcpdump-{link.lower()}.log").read_text()
        require(re.search(r"^0 packets dropped by kernel$", dump_log, re.M), f"capture loss on {link}")
        for direction, source, dest in (("client_to_server", client, server),
                                        ("server_to_client", server, client)):
            tx = keyed(source["udp_summary"], "link")[link]
            rx = keyed(dest["udp_summary"], "link")[link]
            captured = counts[direction]
            require(0 < int(rx["rx"]) <= captured["packets"] <= int(tx["tx"]) and
                    0 < int(rx["rx_bytes"]) <= captured["udp_payload_bytes"] <= int(tx["tx_bytes"]),
                    f"capture and Socket counters disagree for {link}/{direction}")
        if link == "B":
            require(all(frames[path_id]["PATH_CHALLENGE"] and frames[path_id]["PATH_RESPONSE"]
                        for frames in (client_frames, server_frames)), "missing path B validation frames")
        if require_both_streams:
            require(server_frames[path_id]["STREAM"] > 0, f"no data-bearing STREAM packets on {link}")
        results[link] = dict(path_id=int(path_id), capture=counts,
                             server_stream_packets=server_frames[path_id]["STREAM"])
    require(len({link["path_id"] for link in results.values()}) == 2, "duplicate logical path ID")
    return results


def validate(root, policy="fifo-replace", queue_check=verify_queue):
    client, server = (read_events(root / f"{role}.log") for role in ("client", "server"))
    for role, events in (("client", client), ("server", server)):
        require([row["ok"] for row in events["exit"]] == ["1"] and len(events["handshake"]) == 1 and
                [row["count"] for row in events["paths_validated"]] == ["2"], f"{role} transport failed")
    start, = client["iot_start"]
    require(start.get("policy", "fifo-replace") == policy, "incorrect scheduler for this run")
    origin = int(start["origin"])
    workload = read_workload(root / "workload.bin")
    require(len(workload) == int(start["records"]), "incorrect workload size")
    replaced = queue_check(root, client, workload, origin, policy)

    submits = keyed(client["iot_submit"], "id")
    received = keyed(server["iot_rx"], "id")
    acks = keyed(client["iot_ack"], "id")
    require(submits.keys() == received.keys() == acks.keys() == workload.keys() - replaced.keys(),
            "missing, unsolicited, or superseded record was sent or acknowledged")
    require(len(client["iot_complete"]) == 1, "client did not finish replay")
    verify_streams(client, server, submits, received, acks, workload)

    sensors, events = {}, set()
    statuses = Counter()
    misses = 0
    for id_, row in received.items():
        expected = workload[id_].copy()
        expected.pop("release")
        for key in ("generated", "deadline"):
            expected[key] += origin
        require(all(int(row[key]) == value for key, value in expected.items()),
                f"wire metadata or payload changed for record {id_}")
        ack = acks[id_]
        require(row["stream_id"] == ack["stream_id"] == submits[id_]["stream_id"] and
                int(submits[id_]["submitted"]) == int(ack["submitted"]) and
                int(ack["submitted"]) <= int(row["received"]) == int(ack["server_received"]) <=
                int(ack["client_received"]), f"invalid stream ID or timestamps for {id_}")
        on_time = int(row["received"]) <= expected["deadline"]
        misses += not on_time
        require(int(row["on_time"]) == int(ack["on_time"]) == on_time and
                int(row["age_exceeded"]) == (expected["kind"] == STATE and
                    int(row["received"]) - expected["generated"] > expected["maximum_age"]),
                f"incorrect deadline or age for {id_}")
        latest = (0, 0)
        if expected["kind"] == STATE:
            sensor = expected["source"]
            incoming = expected["generated"], expected["serial"]
            previous = sensors.get(sensor, (0, 0))
            status = UPDATED if incoming > previous else STALE
            latest = sensors[sensor] = max(incoming, previous)
        else:
            status = DUPLICATE if expected["serial"] in events else ACCEPTED
            events.add(expected["serial"])
        require(int(row["status"]) == int(ack["status"]) == status and all(
            (int(record["latest_generated"]), int(record["latest_serial"])) == latest for record in (row, ack)),
            f"incorrect deduplication or state snapshot for {id_}")
        statuses[status] += 1

    client_frames, server_frames = (received_frames(root / f"{role}.xquic.log")
                                    for role in ("client", "server"))
    credit = sum(frames["MAX_STREAMS"] for frames in client_frames.values())
    streams_blocked = sum(frames["STREAMS_BLOCKED"] for frames in server_frames.values())
    blocked = [row for row in client.get("iot_stream_create", []) if row["blocked"] == "1"]
    credit_resume = [row for row in client.get("iot_stream_create", []) if row["blocked"] == "0"]
    window = int(start["tx_window"])
    backpressure = client.get("iot_write_backpressure", [])
    peak = max(int(row["active_tx"]) for row in submits.values())
    require(peak <= window, "application window exceeded")
    strict = policy == "fifo-replace"
    if strict:
        require(0 < misses < len(submits) and all(statuses[status] > 0 for status in
                (UPDATED, STALE, ACCEPTED, DUPLICATE)), "missing required deadline or receiver outcome")
        require(credit > 0 and streams_blocked > 0 and len(submits) > int(start["initial_uni_credit"])
                and blocked and credit_resume and any(int(row["active_tx"]) < window for row in blocked),
                "credit exhaustion/recovery unverified")
        require(any(row["blocked"] == "1" for row in client["iot_window"]) and
                any(row["blocked"] == "0" for row in client["iot_window"]), "application window did not stall/release")
        require(any(int(row["result"]) > 0 for row in backpressure)
                and any(int(row["result"]) < 0 for row in backpressure), "missing partial write or EAGAIN")
    return {
        "status": "passed", "generated": len(workload), "pending_replaced": len(client.get("iot_replace", [])),
        "submitted": len(submits), "acknowledged": len(acks), "status_updates": statuses[UPDATED],
        "stale_statuses": statuses[STALE], "unique_events": statuses[ACCEPTED],
        "duplicate_events": statuses[DUPLICATE], "deadline_misses": misses,
        "initial_uni_credit": int(start["initial_uni_credit"]), "client_max_streams_frames": credit,
        "server_streams_blocked_frames": streams_blocked,
        "client_credit_stalls": len(blocked), "client_peak_active_streams": peak,
        "partial_writes": sum(int(row["result"]) > 0 for row in backpressure),
        "write_eagain": sum(int(row["result"]) < 0 for row in backpressure),
        "links": verify_links(root, client, server, client_frames, server_frames, strict),
    }


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("Usage: python3 evaluation/verify_stage2.py RESULTS_DIRECTORY")
    try:
        print(json.dumps(validate(Path(sys.argv[1])), indent=2))
    except (OSError, KeyError, ValueError, struct.error, subprocess.CalledProcessError) as exc:
        sys.exit(f"Stage 2 verification failed: {exc}")
