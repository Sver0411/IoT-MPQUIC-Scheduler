"""Verify Stage 9.1 attempt, binding, wire and delivery evidence."""

import argparse
import hashlib
import json
import re
import struct
from collections import Counter, defaultdict, deque
from pathlib import Path

from oracle import app_scheduler_oracle as app, path_scheduler_oracle as path
from oracle.stage91_lifecycle import Attempts, Paths, sequence
from verify_stage2 import RECORD, keyed, read_workload, require
from verify_stage5 import verify_network
from verify_stage7 import endpoint_teardown, verify_arrivals


def fields(line):
    tokens = [part.split("=", 1) for part in line.split()]
    result = dict(tokens)
    require(len(tokens) == len(result), "duplicate log field")
    return result


def log_rows(file):
    return [fields(line) for line in file.read_text(encoding="utf-8").splitlines()]


def by_event(rows):
    grouped = defaultdict(list)
    for row in rows:
        grouped[row["event"]].append(row)
    return grouped


def integer(row, key):
    value = int(row[key])
    require(value >= 0, f"negative {key}")
    return value


def packet_key(row):
    return row["path"], row["pns"], row["pn"]


def frame_key(row):
    return row["stream"], row["offset"], row["length"], row["fin"]


def datagram_key(row, role):
    client_port = int(row["local_port" if role == "client" else "peer_port"])
    server_port = int(row["peer_port" if role == "client" else "local_port"])
    require(server_port == 4433 and client_port in (4434, 4435) and
            (client_port == 4434 or row["link"] == "B"), "unexpected socket port")
    return row["link"], client_port, int(row["bytes"]), row["digest"]


def check_datagram(row, table):
    key = row["datagram"]
    require(int(key) == len(table) + 1 and int(row["at_us"]) <= int(row["t_us"]) and
            integer(row, "bytes") > 0 and row["link"] in ("A", "B") and
            re.fullmatch(r"[0-9a-f]{64}", row["digest"]), "invalid datagram observation")
    table[key] = row


def verify_xquic_log(rows, file):
    raw = file.read_text(encoding="utf-8")
    markers = [fields(line.split("|iot91|", 1)[1].split("|", 1)[0])
               for line in raw.splitlines() if "|iot91|" in line]
    observed = [{k: v for k, v in row.items() if k not in
                 ("event", "t_us", "audit_seq", "tx_datagram", "rx_datagram")}
                for row in rows if row["event"] == "iot91_xquic"]
    require(markers == observed, "XQUIC trace and endpoint telemetry differ")
    # The stock packet summary detects STREAM packets missing from the new hook.
    send_pattern = re.compile(r"\|xqc_send_packet_with_pn\|<==\|.*?\|path:(\d+)\|pkt_num:(\d+)"
                              r"\|.*?\|frame:([^|]+)\|")
    raw_packets = Counter((path_id, pn) for path_id, pn, frames in send_pattern.findall(raw)
                          if "STREAM" in frames.split())
    observed_packets = Counter((p, n) for p, _, n in
                               {packet_key(row) for row in observed if row["kind"] == "wire_tx"})
    require(raw_packets == observed_packets, "STREAM packets missing from telemetry")


def verify_endpoint(rows, role, cross, profiling=False):
    sequence(rows, role)
    grouped = by_event(rows)
    paths, attempts = Paths(), Attempts()
    tx, rx, choices, packets = {}, {}, {}, {}
    data_rx, fin_reads, read_streams = defaultdict(list), {}, set()
    delivered, timed_out, closed_streams = set(), {}, set()
    transmitted_choices = set()
    opened_tx, links = set(), {}
    decision_states = {}
    wire_rx = []
    cutoff_seen = False
    totals_seen = False
    final_events = ("process_cpu", "exit", "iot92_rss") if profiling else ("process_cpu", "exit")
    for row in rows:
        event = row["event"]
        require(not totals_seen or event in final_events, "activity after trace totals")
        if role == "client":
            attempts.observe(row)
        if event == "iot_app_decision":
            decision_states[row["id"]] = paths.eligible()
        if event == "path_map":
            require(row["path_id"] not in links and row["link"] not in links.values(), "duplicate path mapping")
            links[row["path_id"]] = row["link"]
        if event == "iot91_bind":
            require(row["home"] in paths.eligible(), "bound to unavailable path")
        elif event in ("iot91_path_command", "iot91_path_result"):
            paths.command(row)
        elif event == "iot91_choice":
            require(role == "client" and row["stream"] in attempts.submitted and
                    row["stream"] not in closed_streams and row["id"] == attempts.bound[row["stream"]]["id"] and
                    int(row["choice"]) == len(choices) + 1, "missing, duplicate or unbound choice")
            choices[row["choice"]] = row, paths.choose(row, attempts.bound[row["stream"]]["home"], cross)
        elif event == "iot91_udp_tx":
            check_datagram(row, tx)
        elif event == "iot91_udp_rx":
            check_datagram(row, rx)
        elif event == "iot91_xquic":
            kind = row["kind"]
            if kind not in ("wire_tx", "wire_rx"):
                paths.observe(row)
                continue
            stream = row["stream"]
            require(row["fin"] in ("0", "1") and int(row["pns"]) == 2 and
                    integer(row, "pn") >= 0 and integer(row, "offset") + integer(row, "length") <= RECORD.size + 16384,
                    "invalid authenticated STREAM evidence")
            key = packet_key(row)
            if kind == "wire_tx":
                sock = tx[row["tx_datagram"]]
                require(sock["path"] == row["path"] and links[row["path"]] == sock["link"] and
                        next(reversed(tx)) == row["tx_datagram"],
                        "wire send disagrees with socket path or order")
                first = key not in packets
                entry = packets.setdefault(key, [])
                require(first or (entry[0]["tx_datagram"], entry[0]["choice"]) ==
                        (row["tx_datagram"], row["choice"]), "duplicate packet number on path")
                require(stream in opened_tx and stream not in closed_streams and
                        (int(stream) % 4 == 3 if role == "server" else stream in attempts.submitted),
                        "send before submission or after close")
                if role == "client":
                    choice, epoch = choices[row["choice"]]
                    require(all(row[k] == choice[k] for k in ("stream", "path", "repair")) and
                            int(choice["t_us"]) <= int(sock["at_us"]) and
                            int(choice["audit_seq"]) < int(sock["audit_seq"]) and
                            paths.available(row["path"]) and paths.epoch[row["path"]] == epoch,
                            "actual send disagrees with choice, order or path lifecycle")
                    require(not first or row["choice"] not in transmitted_choices, "choice reused by another packet")
                    transmitted_choices.add(row["choice"])
                if row["repair"] == "1":
                    original = row["orig_path"], row["orig_pns"], row["orig_pn"]
                    start, length = int(row["offset"]), int(row["length"])
                    require(original != key and any(
                        f["stream"] == stream and int(f["offset"]) <= start and
                        start + length <= int(f["offset"]) + int(f["length"]) and
                        (row["fin"] == "0" or f["fin"] == "1" and start + length == int(f["offset"]) + int(f["length"]))
                        for f in packets.get(original, ())
                    ), "repair lacks original STREAM send")
                else:
                    require(row["repair"] == "0", "invalid repair flag")
                entry.append(row)
            else:
                sock = rx[row["rx_datagram"]]
                require(sock["path"] == row["path"] and links[row["path"]] == sock["link"] and
                        next(reversed(rx)) == row["rx_datagram"],
                        "authenticated frame disagrees with receive datagram")
                wire_rx.append(row)
                data_rx[stream].append(row)
        elif event == "stream_open":
            stream = row["stream_id"]
            require(row["direction"] in ("rx", "tx") and stream not in read_streams and stream not in opened_tx,
                    "duplicate or invalid stream open")
            (read_streams if row["direction"] == "rx" else opened_tx).add(stream)
        elif event == "iot91_fin_read":
            stream = row["stream"]
            require(stream in read_streams and stream not in fin_reads and stream not in closed_streams,
                    "duplicate or unopened receive FIN")
            size = integer(row, "bytes")
            ranges = [(int(f["offset"]), int(f["length"]), f["fin"])
                      for f in data_rx[stream]]
            end = 0
            for start, length, _ in sorted(ranges):
                if start > end:
                    break
                end = max(end, start + length)
            require(end == size and any(start + length == size and fin == "1" for start, length, fin in ranges),
                    "stream completion precedes complete authenticated bytes and FIN")
            fin_reads[stream] = row
        elif event in ("iot_rx", "iot_ack"):
            stream = row["stream_id"] if event == "iot_rx" else row["ack_stream_id"]
            time = int(row["received" if event == "iot_rx" else "client_received"])
            require(stream in fin_reads and stream not in delivered and
                    int(fin_reads[stream]["t_us"]) <= time <= int(row["t_us"]),
                    "delivery or ACK precedes application FIN")
            delivered.add(stream)
        elif event == "stream_closed":
            stream = row["stream_id"]
            require(stream not in closed_streams, "duplicate stream close")
            if row["direction"] == "rx":
                require(stream in read_streams and (row["fin"] == "1") == (stream in delivered),
                        "receive close disagrees with delivery")
            closed_streams.add(stream)
        elif event == "iot91_timeout":
            stream = row["stream"]
            require(role == "client" and not cutoff_seen and stream in attempts.submitted and
                    attempts.submitted[stream]["id"] == row["id"] and stream not in timed_out,
                    "invalid stream timeout")
            timed_out[stream] = row
        elif event == "iot_window_end":
            require(role == "client" and not cutoff_seen, "duplicate client cutoff")
            cutoff_seen = True
        elif event == "iot91_totals":
            require(not totals_seen and not paths.commands and (role != "client" or cutoff_seen),
                    "unfinished lifecycle or cutoff missing at trace end")
            totals_seen = True
            attempts.totals(row)
            require(int(row["choices"]) == len(choices) and int(row["tx"]) == len(tx) and
                    int(row["rx"]) == len(rx), "trace totals disagree")
    require(totals_seen and delivered == fin_reads.keys(), "application delivery or ACK missing after FIN")
    return {"role": role, "events": grouped, "tx": tx, "rx": rx, "packets": packets, "wire_rx": wire_rx, "paths": paths,
            "decisions": decision_states, "choices": len(choices), "timed_out": timed_out,
            "fin_reads": fin_reads, "bound": attempts.bound, "attempts": attempts.count, "blocked": attempts.blocked}


def verify_direction(sender, receiver):
    sent = defaultdict(deque)
    for row in sender["tx"].values():
        sent[datagram_key(row, sender["role"])].append(int(row["at_us"]))
    for row in receiver["rx"].values():
        key = datagram_key(row, receiver["role"])
        require(sent[key] and sent[key].popleft() <= int(row["at_us"]), "datagram has no earlier peer send")
    seen = Counter()
    for frame in receiver["wire_rx"]:
        key = packet_key(frame)
        matching = [tx for tx in sender["packets"].get(key, ()) if frame_key(tx) == frame_key(frame)]
        require(matching, "received QUIC frame has no matching sent packet and range")
        key_rx = frame["rx_datagram"], key, frame_key(frame)
        seen[key_rx] += 1
        require(seen[key_rx] <= len(matching) and
                datagram_key(sender["tx"][matching[0]["tx_datagram"]], sender["role"]) ==
                datagram_key(receiver["rx"][frame["rx_datagram"]], receiver["role"]),
                "received frame belongs to another ciphertext datagram")


def capture(file, client_ip, server_ip, ports):
    """Read full-snaplen classic Ethernet/IPv4 PCAP from the isolated lab."""
    output = {"client": Counter(), "server": Counter()}
    with file.open("rb") as fd:
        header = fd.read(24)
        endian = {b"\xd4\xc3\xb2\xa1": "<", b"\xa1\xb2\xc3\xd4": ">",
                  b"\x4d\x3c\xb2\xa1": "<", b"\xa1\xb2\x3c\x4d": ">"}[header[:4]]
        require(len(header) == 24 and struct.unpack_from(endian + "I", header, 20)[0] == 1,
                f"{file.name}: expected Ethernet PCAP")
        while frame := fd.read(16):
            _, _, size, length = struct.unpack(endian + "IIII", frame)
            raw = fd.read(size)
            require(len(raw) == size == length and raw[12:14] == b"\x08\x00" and
                    raw[14] >> 4 == 4 and raw[23] == 17 and
                    not (int.from_bytes(raw[20:22], "big") & 0x3fff), f"{file.name}: invalid UDP capture")
            udp = 14 + (raw[14] & 15) * 4
            source, dest, udp_size = struct.unpack_from("!HHH", raw, udp)
            ips = (raw[26:30], raw[30:34])
            if source in ports and (dest, ips) == (4433, (client_ip, server_ip)):
                role, client_port = "client", source
            else:
                require(dest in ports and (source, ips) == (4433, (server_ip, client_ip)),
                        f"{file.name}: invalid socket tuple")
                role, client_port = "server", dest
            payload = raw[udp + 8:udp + udp_size]
            require(len(payload) == udp_size - 8, f"{file.name}: incomplete UDP payload")
            output[role][client_port, len(payload), hashlib.sha256(payload).hexdigest()] += 1
    return output


def verify_captures(directory, client, server, migration):
    endpoints = {"client": client, "server": server}
    for link, subnet in (("A", 1), ("B", 2)):
        ports = (4434, 4435) if migration and link == "B" else (4434,)
        captures = {}
        for side, prefix in (("client", "client-"), ("server", "")):
            captured = capture(directory / f"{prefix}link-{link.lower()}.pcap",
                               bytes((10, 47, subnet, 1)), bytes((10, 47, subnet, 2)), ports)
            captures[side] = captured
            log = (directory / f"{prefix}tcpdump-{link.lower()}.log").read_text()
            require(re.search(r"^0 packets dropped by kernel$", log, re.M), "capture drops reported")
            for source, target in (("client", "server"), ("server", "client")):
                sent = Counter(datagram_key(row, source)[1:] for row in endpoints[source]["tx"].values()
                               if row["link"] == link)
                require(captured[source] <= sent, "capture includes a datagram absent from socket sends")
                if side == target:
                    observed = Counter(datagram_key(row, target)[1:] for row in endpoints[target]["rx"].values()
                                       if row["link"] == link)
                    require(observed <= captured[source], "socket receipt is absent from destination capture")
        require(captures["client"]["server"] <= captures["server"]["server"] and
                captures["server"]["client"] <= captures["client"]["client"],
                "destination capture contains a datagram missing from source capture")


def verify_migration(client, server, rebind_at, origin):
    rebinds = client["events"]["iot91_rebind"]
    migrations = {role["role"]: [row for row in role["events"]["iot91_xquic"] if row["kind"].startswith("migration_")]
                  for role in (client, server)}
    if rebind_at is None:
        require(not rebinds and not any(migrations.values()) and all(
            datagram_key(row, trace["role"])[1] == 4434
            for trace in (client, server) for direction in ("tx", "rx") for row in trace[direction].values()),
            "unexpected UDP rebinding")
        return 0
    change, = rebinds
    changed = int(change["t_us"])
    require(change["link"] == "B" and (change["old_port"], change["new_port"]) == ("4434", "4435") and
            origin + rebind_at <= changed < origin + rebind_at + 100000, "client socket did not rebind as scheduled")
    require(not migrations["client"] and [r["kind"] for r in migrations["server"]] ==
            ["migration_start", "migration_commit", "migration_end"], "incomplete server rebinding")
    start, commit, _ = migrations["server"]
    for row in migrations["server"]:
        received = server["rx"][row["rx_datagram"]]
        require(row["path"] == change["path"] and received["link"] == "B" and received["peer_port"] == "4435" and
                changed <= int(received["at_us"]) <= int(row["t_us"]), "rebinding lacks candidate datagram evidence")
    require(start["rx_datagram"] != commit["rx_datagram"], "rebinding committed on its first datagram")
    commit_seq = int(commit["audit_seq"])
    after = 0
    for frames in server["packets"].values():
        frame = frames[0]
        if frame["path"] != change["path"]:
            continue
        sent = server["tx"][frame["tx_datagram"]]
        committed = int(sent["audit_seq"]) > commit_seq
        require(sent["peer_port"] == ("4435" if committed else "4434"),
                "server STREAM send used uncommitted or stale peer port")
        after += committed
    client_ports = Counter()
    for frames in client["packets"].values():
        sent = client["tx"][frames[0]["tx_datagram"]]
        if sent["link"] == "B":
            require(sent["local_port"] == ("4435" if int(sent["at_us"]) >= changed else "4434"),
                    "client STREAM send used the wrong port for the migration phase")
            client_ports[sent["local_port"]] += 1
    require(client_ports["4434"] and client_ports["4435"] and after,
            f"fixture lacked data on both sides of the migration, client={dict(client_ports)}, server after={after}")
    require(any(client["rx"][r["rx_datagram"]]["peer_port"] == "4433" and
                client["rx"][r["rx_datagram"]]["local_port"] == "4435" for r in client["wire_rx"]),
            "server STREAM did not reach the rebound client socket")
    return server["paths"].transitions["migrated"]


def verify_binding(case, records, client):
    grouped = client["events"]
    plans = keyed(grouped["iot_plan"], "stream_id")
    forecasts = {(row["id"], row["path_id"]): row for row in grouped["iot_path_forecast"]}
    require(len(forecasts) == len(grouped["iot_path_forecast"]), "duplicate forecast")
    samples = defaultdict(list)
    for row in grouped["iot_path_sample"]:
        samples[row["id"]].append({**row, **forecasts.pop((row["id"], row["path_id"]))})
    require(not forecasts and plans.keys() == client["bound"].keys() and
            samples.keys() == {row["id"] for row in plans.values()}, "binding forecasts incomplete")
    decisions = {row["id"]: row for row in grouped["iot_app_decision"]}
    for stream, plan in plans.items():
        key = plan["id"]
        bound = client["bound"][stream]
        require(bound["home"] == plan["home"] and bound["id"] == key and
                int(decisions[key]["audit_seq"]) < int(plan["audit_seq"]) < int(bound["audit_seq"]) and
                {s["path_id"] for s in samples[key]} == client["decisions"][key],
                "initial path decision differs from authoritative path state")
        path.validate(case, records[key], plan, samples[key], {int(p) for p in client["decisions"][key]},
                      decisions[key])


def verify_run(directory):
    case = json.loads((directory / "case.json").read_text())
    records = read_workload(directory / "workload.bin")
    results = {}
    for role in ("client", "server"):
        rows = log_rows(directory / f"{role}.log")
        results[role] = verify_endpoint(rows, role, case["repair"] == "cross", profiling=case.get("stage92", False))
        verify_xquic_log(rows, directory / f"{role}.xquic.log")
    client, server = results["client"], results["server"]
    left, right = client["events"], server["events"]
    start, = left["iot_start"]
    cfg, = left["iot_experiment"]
    assignment, = left["iot_oracle"]
    recovery, = left["iot_recovery_config"]
    ablation, = left["iot_ablation"]
    tuning, = left["iot_tuning"]
    origin, horizon = int(start["origin"]), case["horizon_us"]
    cutoff = origin + horizon
    end, = left["iot_window_end"]
    require(start["policy"] == case["policy"] and cfg["path"] == case["path_scheduler"] and
            cfg["repair"] == case["repair"] and int(cfg["horizon_us"]) == horizon and
            int(assignment["assignment_seed"]) == case["assignment_seed"] and
            int(recovery["k"]) == case["recovery_k"] and ablation["name"] == case["ablation"] and
            all(float(tuning[key]) == value for key, value in case["tuning"].items()) and
            horizon <= int(end["elapsed"]) <= horizon + 100000, "case or cutoff differs")
    verify_network(directory, case, origin)
    for role, trace in (("client", left), ("server", right)):
        require(len(trace["handshake"]) == 1 and [r["count"] for r in trace["paths_validated"]] == ["2"],
                f"{role}: missing validated connection")
        endpoint_teardown(directory, role, trace, cutoff)
    inputs = keyed(left["iot_input"], "id")
    require(inputs.keys() == {k for k, r in records.items() if r["release"] < horizon}, "incomplete workload admission")
    app_result = app.verify(records, log_rows(directory / "client.log"), origin, case)
    require(app_result["decisions"] == client["attempts"], "Oracle and attempt totals disagree")
    verify_binding(case, records, client)
    submits, arrivals, acks = (keyed(rows, "id") for rows in (left["iot_submit"], right["iot_rx"], left["iot_ack"]))
    require(acks.keys() <= arrivals.keys() <= submits.keys(), "unsolicited delivery or ACK")
    verify_arrivals(records, submits, arrivals, acks, origin)
    data_written = keyed(left["stream_fin_tx"], "stream_id")
    ack_written = keyed(right["stream_fin_tx"], "stream_id")
    for role, trace, writes in (("client", client, data_written), ("server", server, ack_written)):
        for frames in trace["packets"].values():
            for frame in frames:
                if frame["fin"] == "1":
                    written = writes.get(frame["stream"])
                    require(written and int(written["bytes"]) == int(frame["offset"]) + int(frame["length"]) and
                            (written["id"] in arrivals if role == "server" else
                             submits[written["id"]]["stream_id"] == frame["stream"]), "wire FIN disagrees with app write")
    for key, row in arrivals.items():
        require(int(server["fin_reads"][row["stream_id"]]["bytes"]) == records[key]["payload_len"] + RECORD.size,
                "data FIN has wrong byte count")
    for key, row in acks.items():
        ack_stream = row["ack_stream_id"]
        require(ack_written[ack_stream]["id"] == key and int(client["fin_reads"][ack_stream]["bytes"]) == 44,
                "ACK FIN has wrong record or byte count")
    unacked = {r["stream_id"] for k, r in submits.items()
               if k not in acks or int(acks[k]["client_received"]) >= int(end["t_us"])}
    require(unacked == client["timed_out"].keys() and len(unacked) == int(end["outstanding"]) and
            all(cutoff <= int(r["t_us"]) <= int(end["t_us"]) for r in client["timed_out"].values()),
            "deadline timeout does not match missing ACK at cutoff")
    verify_direction(client, server)
    verify_direction(server, client)
    rebind_at = case.get("migration_at_us")
    verify_captures(directory, client, server, rebind_at is not None)
    migrated = verify_migration(client, server, rebind_at, origin)
    require(client["paths"].observed_commands == case.get("lifecycle_commands", []), "path commands differ from case")
    if case.get("lifecycle_commands"):
        for name in ("frozen", "recovered", "removed"):
            require(server["paths"].transitions[name] >= case["required"][name], f"peer did not observe {name}")
    crossed = sum(f[0]["repair"] == "1" and f[0]["path"] != client["bound"][f[0]["stream"]]["home"]
                  for f in client["packets"].values())
    ack_wire = {row["stream"] for frames in server["packets"].values() for row in frames if row["fin"] == "1"}
    ack_received = {row["stream"] for row in client["wire_rx"]}
    lost_ack = {row["id"] for stream, row in ack_written.items()
                if stream in ack_wire and stream not in ack_received and row["id"] in arrivals and
                submits[row["id"]]["stream_id"] in unacked}
    checks = {"blocked": client["blocked"], "cross_packets": crossed, "timeouts": len(unacked), "migrated": migrated,
              "lost_ack": len(lost_ack),
              **{name: client["paths"].transitions[name] for name in ("frozen", "recovered", "removed")}}
    for name, minimum in case["required"].items():
        require(checks[name] >= minimum, f"fixture failed to exercise {name}")
    return {"status": "passed", "attempts": client["attempts"], **checks, "choices": client["choices"],
            "submissions": len(submits), "delivered": len(arrivals), "acked": len(acks),
            "client_packets": len(client["packets"]), "server_packets": len(server["packets"]),
            "client_transitions": dict(client["paths"].transitions),
            "server_transitions": dict(server["paths"].transitions), "captures": 4}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    print(json.dumps(verify_run(parser.parse_args().run), indent=2))
