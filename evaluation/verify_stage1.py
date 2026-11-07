"""Validate Stage 1 logs and independent server-side captures; emit JSON."""

import json
import re
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path


def require(condition, message):
    if not condition:
        raise ValueError(message)


def event_rows(path):
    with path.open(encoding="utf-8") as file:
        for line in file:
            yield dict(field.split("=", 1) for field in line.split())


def read_events(path):
    events = {}
    for row in event_rows(path):
        events.setdefault(row["event"], []).append(row)
    return events


def keyed(rows, key):
    values = {row[key]: row for row in rows}
    require(len(values) == len(rows), f"duplicate {key} in event series")
    return values


def capture_counts(path, client, server):
    output = subprocess.run(
        ["tcpdump", "-tt", "-nn", "-r", str(path)],
        check=True, capture_output=True, text=True,
    ).stdout
    pattern = re.compile(r"^\S+ IP ([0-9.]+) > ([0-9.]+): UDP, length (\d+)$")
    counts = {direction: {"packets": 0, "udp_payload_bytes": 0}
              for direction in ("client_to_server", "server_to_client")}
    tuples = {(client, server): "client_to_server", (server, client): "server_to_client"}
    for line in output.splitlines():
        match = pattern.fullmatch(line)
        require(match, f"{path.name}: unexpected capture line {line!r}")
        src, dst, size = match.groups()
        require((src, dst) in tuples, f"{path.name}: unexpected UDP tuple {src} -> {dst}")
        direction = counts[tuples[src, dst]]
        direction["packets"] += 1
        direction["udp_payload_bytes"] += int(size)
    return counts


def tcpdump_address(address):
    ip, port = address.split(":")
    return f"{ip}.{port}"


def rtt_ms(path):
    match = re.search(r"= [0-9.]+/([0-9.]+)/[0-9.]+/[0-9.]+ ms", path.read_text())
    require(match, f"missing ping RTT statistics in {path.name}")
    return float(match[1])


def quic_path_samples(events, id_, field):
    samples = {int(row["phase"]): int(row[field])
               for row in events["quic_path"] if row["path_id"] == id_}
    return [samples[1] - samples.get(0, 0), samples[2] - samples[1]]


def received_frames(path):
    frames = defaultdict(Counter)
    pattern = re.compile(r"\|xqc_conn_on_pkt_processed\|.*?\|path:(\d+)\|.*?\|frame:([^|]+)\|")
    with path.open(encoding="utf-8") as trace:
        for line in trace:
            match = pattern.search(line)
            if match:
                frames[match[1]].update(match[2].split())
    return frames


def validate(root):
    client = read_events(root / "client.log")
    server = read_events(root / "server.log")
    for name, events in (("client", client), ("server", server)):
        require([row["ok"] for row in events["exit"]] == ["1"], f"{name} did not exit cleanly")
        require(len(events["handshake"]) == 1, f"{name}: expected one TLS handshake")
        for phase in ("1", "2"):
            stats = [row for row in events["quic_stats"] if row["phase"] == phase]
            require(len(stats) == 1 and stats[0]["mp"] == "1" and stats[0]["mp_state"] == "1"
                    and stats[0]["available"] == "2", f"{name}: phase {phase} lacks two validated paths")

    starts = keyed(client["phase_start"], "phase")
    acks = keyed(client["phase_acked"], "phase")
    completed = keyed(server["phase_complete"], "phase")
    require(starts.keys() == acks.keys() == completed.keys() == {"1", "2"},
            "expected exactly two complete phases")
    ack_tx = keyed(server["stream_fin_tx"], "id")
    ack_rx = keyed(client["phase_ack_rx"], "phase")
    require(ack_tx.keys() == ack_rx.keys() == starts.keys() and
            all(row["bytes"] == "4" and row["stream_id"] == ack_rx[phase]["stream_id"]
                for phase, row in ack_tx.items()), "phase ACK stream ID or size mismatch")
    received = keyed(server["record_rx"], "id")
    sent = keyed(client["stream_fin_tx"], "id")
    require(received.keys() == sent.keys(), "sent FIN IDs differ from validated received record IDs")
    require(len({row["stream_id"] for row in received.values()}) == len(received),
            "records did not use distinct streams")
    require(all(row["stream_id"] == sent[id_]["stream_id"] and row["bytes"] == sent[id_]["bytes"]
                for id_, row in received.items()), "record stream ID or size changed between endpoints")

    phases = []
    next_id = 0
    for phase in ("1", "2"):
        count = int(starts[phase]["records"])
        size = int(starts[phase]["record_bytes"])
        records = [row for row in received.values() if row["phase"] == phase]
        require({int(row["id"]) for row in records} == set(range(next_id, next_id + count))
                and all(int(row["bytes"]) == size for row in records)
                and int(acks[phase]["records"]) == int(completed[phase]["records"]) == count,
                f"phase {phase}: missing or invalid complete records")
        next_id += count
        start = int(starts[phase]["t_us"])
        end = int(completed[phase]["t_us"])
        ack = int(acks[phase]["t_us"])
        require(start <= end <= ack, f"phase {phase}: inconsistent monotonic timestamps")
        phases.append({"phase": int(phase), "records": count, "record_bytes": size,
                       "server_complete_ms": round((end - start) / 1000, 3),
                       "client_ack_ms": round((ack - start) / 1000, 3)})

    client_frames = received_frames(root / "client.xquic.log")
    server_frames = received_frames(root / "server.xquic.log")
    credit = sum(frames["MAX_STREAMS"] for frames in client_frames.values())
    require(credit > 0, "client trace contains no MAX_STREAMS update")
    links = {}
    for link in ("A", "B"):
        c = keyed(client["path_map"], "link")[link]
        s = keyed(server["path_map"], "link")[link]
        path_id = c["path_id"]
        require(path_id == s["path_id"] and c["local"] == s["peer"] and c["peer"] == s["local"],
                f"link {link}: endpoint path mapping disagrees")
        counts = capture_counts(root / f"link-{link.lower()}.pcap",
                                tcpdump_address(c["local"]), tcpdump_address(s["local"]))
        dump_log = (root / f"tcpdump-{link.lower()}.log").read_text()
        require(re.search(r"^0 packets dropped by kernel$", dump_log, re.M),
                f"link {link}: tcpdump reported capture drops or failed to stop cleanly")
        for source, destination, direction in (
            (client, server, "client_to_server"), (server, client, "server_to_client"),
        ):
            tx = keyed(source["udp_summary"], "link")[link]
            rx = keyed(destination["udp_summary"], "link")[link]
            observed = counts[direction]
            require(0 < int(rx["rx"]) <= observed["packets"] <= int(tx["tx"]) and
                    0 < int(rx["rx_bytes"]) <= observed["udp_payload_bytes"] <= int(tx["tx_bytes"]),
                    f"link {link}: capture and socket counters disagree for {direction}")
            observed["socket_sent"] = int(tx["tx"])
            observed["socket_received"] = int(rx["rx"])

        sent_data = quic_path_samples(client, path_id, "data_tx_bytes")
        received_data = quic_path_samples(server, path_id, "data_rx_bytes")
        require(min(sent_data + received_data) >= 0 and sum(sent_data) > 0 and sum(received_data) > 0,
                f"link {link}: no evidence of data-bearing QUIC packets")
        require(server_frames[path_id]["STREAM"] > 0 and client_frames[path_id]["ACK_MP"] > 0,
                f"link {link}: no trace of STREAM or ACK_MP")
        if link == "B":
            require(all(frames[path_id]["PATH_CHALLENGE"] > 0 and frames[path_id]["PATH_RESPONSE"] > 0
                        for frames in (client_frames, server_frames)),
                    "link B: path validation exchange missing in endpoint traces")
        links[link] = {
            "path_id": int(path_id), "client": c["local"], "server": s["local"],
            "capture": counts, "client_quic_data_tx_bytes_by_phase": sent_data,
            "server_quic_data_rx_bytes_by_phase": received_data,
            "server_stream_packets": server_frames[path_id]["STREAM"],
            "client_ack_mp_packets": client_frames[path_id]["ACK_MP"],
            "icmp_mean_rtt_ms_by_phase": [rtt_ms(root / f"ping-{link}-{phase}.txt") for phase in (1, 2)],
        }

    require(links["A"]["path_id"] != links["B"]["path_id"], "links share a QUIC path ID")
    a, b = (links[link]["icmp_mean_rtt_ms_by_phase"] for link in ("A", "B"))
    require(a[0] < b[0] and a[1] > b[1], "ping did not confirm the configured RTT reversal")
    for i, phase in enumerate(phases):
        a_bytes, b_bytes = (links[link]["server_quic_data_rx_bytes_by_phase"][i] for link in ("A", "B"))
        require(a_bytes + b_bytes > 0, f"phase {i + 1}: no receiver QUIC data bytes")
        phase["link_b_share_of_server_quic_data_rx_bytes"] = round(b_bytes / (a_bytes + b_bytes), 4)
    return {"status": "passed", "client_max_streams_frames": credit, "phases": phases, "links": links}


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("Usage: python3 evaluation/verify_stage1.py RESULTS_DIRECTORY")
    try:
        print(json.dumps(validate(Path(sys.argv[1])), indent=2))
    except (OSError, KeyError, ValueError, subprocess.CalledProcessError) as exc:
        sys.exit(f"Stage 1 verification failed: {exc}")
