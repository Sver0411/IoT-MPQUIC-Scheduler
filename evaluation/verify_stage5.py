"""Audit network traces, compute per-run metrics and produce paired comparisons."""

import argparse
import csv
import hashlib
import json
import re
from collections import Counter
from pathlib import Path

from verify_stage2 import ACCEPTED, EVENT, STATE, UPDATED, keyed, read_events, read_workload, require, validate
from verify_stage3 import age_summary
from verify_stage4 import transmitted_streams, verify_affinity, verify_joint_queue


def fraction(value, total):
    return round(value / total, 8) if total else None


def percentile_ms(samples, percentile):
    if not samples:
        return None
    ordered = sorted(samples)
    return round(ordered[(percentile * len(ordered) + 99) // 100 - 1] / 1000, 3)


def deadline_metrics(generated, arrivals):
    on_time = sum(row["on_time"] == "1" for row in arrivals)
    latencies = [int(row["received"]) - int(row["generated"]) for row in arrivals]
    return {
        "generated": generated, "received": len(arrivals), "on_time": on_time,
        "deadline_success_ratio": fraction(on_time, generated),
        "deadline_miss_ratio": fraction(generated - on_time, generated),
        **{f"latency_p{p}_ms": percentile_ms(latencies, p) for p in (50, 95, 99)},
    }


def verify_network(root, case, origin):
    rows = [json.loads(line) for line in (root / "network.jsonl").read_text().splitlines()]
    profiles = [dict(at_us=None, links=case["initial"]), *case["phases"]]
    require(len(rows) == len(profiles), "network timeline is incomplete")
    require(rows[0]["end_us"] < origin, "initial network configured after replay")
    for row, expected in zip(rows, profiles):
        offset = row["offset_us"]
        scope = expected.get("side", "both")
        sides = ("client", "server") if scope == "both" else (scope,)
        require(offset == expected["at_us"] and row["links"] == expected["links"] and
                row.get("side", "both") == scope and scope in ("client", "server", "both") and
                row["end_us"] >= row["begin_us"], "network timeline differs from case manifest")
        if offset is not None:
            require(0 <= row["begin_us"] - origin - offset <= 150_000 and
                    row["end_us"] - origin - offset <= 250_000, "network transition missed timing tolerance")
        expected_qdiscs = {f"{side}-{link}" for side in sides for link in row["links"]}
        require(row["qdiscs"].keys() == expected_qdiscs, "missing directional qdisc snapshot")
        for link, (delay, rate, loss) in row["links"].items():
            for side in sides:
                qdisc = row["qdiscs"][f"{side}-{link}"]
                match = re.search(r"\bloss (?:random )?([\d.]+)%", qdisc)
                actual_loss = float(match[1]) if match else 0
                require("qdisc netem " in qdisc and f"delay {delay}ms" in qdisc and
                        f"rate {rate}Mbit" in qdisc and abs(actual_loss - loss) < .01, "tc profile mismatch")
    return rows


def path_usage(client, server, rows, origin, horizon):
    received = Counter()
    for row in server["udp_rx"]:
        if origin <= int(row["recv_us"]) < origin + horizon:
            received[row["link"]] += int(row["bytes"])
    tx = keyed(client["udp_summary"], "link")
    rx = keyed(server["udp_summary"], "link")
    output = {}
    for link in ("A", "B"):
        current = rows[0]["links"][link.lower()]
        previous = origin
        capacity_bits = 0
        for row in rows[1:]:
            if link.lower() in row["links"] and row.get("side") != "server":
                change = min(origin + horizon, row["end_us"])
                capacity_bits += (change - previous) * current[1] * (current[2] < 100)
                previous = change
                current = row["links"][link.lower()]
        capacity_bits += (origin + horizon - previous) * current[1] * (current[2] < 100)
        packets_sent, packets_rx = int(tx[link]["tx"]), int(rx[link]["rx"])
        output[link] = {
            "received_udp_payload_bytes_in_window": received[link],
            "received_udp_payload_share": fraction(received[link], sum(received.values())),
            "modeled_usable_capacity_fraction": fraction(8 * received[link], capacity_bits),
            "udp_sent": packets_sent, "udp_received": packets_rx,
            "udp_non_delivery_ratio": fraction(packets_sent - packets_rx, packets_sent),
        }
    return output


def cpu_metrics(events):
    row, = events["process_cpu"]
    user, system, wall = (int(row[key]) for key in ("user_us", "sys_us", "wall_us"))
    require(wall > 0, "invalid CPU wall duration")
    return {"user_us": user, "sys_us": system, "wall_us": wall,
            "one_core_percent": round(100 * (user + system) / wall, 4)}


def metrics(root):
    case = json.loads((root / "case.json").read_text())
    client, server = (read_events(root / f"{role}.log") for role in ("client", "server"))
    start, = client["iot_start"]
    policy = case["policy"]
    origin, horizon = int(start["origin"]), case["horizon_us"]
    proposed = policy == "proposed-cross"
    crossed = []
    if proposed:
        config, = client["iot_recovery_config"]
        ablation, = client["iot_ablation"]
        require(int(config["k"]) == case["recovery_k"] and ablation["name"] == case["ablation"],
                "unexpected proposed configuration")
        transport = validate(root, policy, verify_joint_queue)
        plans, sent, crossed = verify_affinity(root, client, case["ablation"] != "no-cross")
        if case["ablation"] == "no-replace":
            require(transport["pending_replaced"] == 0, "pending state replaced during ablation")
        if case["ablation"] == "no-recovery":
            require(all(row["class"] != "2" for row in plans.values()), "recovery class used during ablation")
    else:
        transport = validate(root, policy)
    network = verify_network(root, case, origin)
    workload = read_workload(root / "workload.bin")
    arrivals = server["iot_rx"]
    states = [row for row in arrivals if int(row["kind"]) == STATE]
    events = {}
    for row in arrivals:
        if int(row["kind"]) == EVENT:
            events.setdefault(row["serial"], row)
    event_count = len({row["serial"] for row in workload.values() if row["kind"] == EVENT})
    sensor_total = Counter(r["source"] for r in workload.values() if r["kind"] == STATE)
    sensor_useful = Counter(int(r["source"]) for r in states if int(r["status"]) == UPDATED)
    service = [sensor_useful[sensor] / count for sensor, count in sensor_total.items()]
    jain = fraction(sum(service) ** 2, len(service) * sum(x * x for x in service))
    mean_age, peak_age = age_summary(workload, arrivals, origin, horizon)
    useful = [row for row in arrivals if int(row["status"]) in (UPDATED, ACCEPTED) and
              int(row["received"]) < origin + horizon]
    final, = [row for row in client["quic_stats"] if row["phase"] == "1"]
    stats = re.findall(r"\|xqc_conn_destroy\|[^\n]*\|tlp_pkt:(\d+)\|",
                       (root / "client.xquic.log").read_text())
    require(len(stats) == 1, "missing XQUIC probe counter")
    # Upstream's rtx_pkt destruction-log field contains lost_count, not actual sent copies.
    sent_streams = Counter((stream, offset) for stream, offset, _, _ in
                           transmitted_streams(root / "client.xquic.log"))
    require(sent_streams, "no transmitted STREAM offsets in QUIC trace")
    result = {
        "status": "passed", "case": case["name"],
        "workload_sha256": hashlib.sha256((root / "workload.bin").read_bytes()).hexdigest(),
        "events": deadline_metrics(event_count, list(events.values())),
        "states": deadline_metrics(sum(sensor_total.values()), states),
        "mean_aoi_ms": mean_age, "peak_aoi_ms": peak_age, "jain_useful_state_delivery": jain,
        "useful_goodput_bps": round(8_000_000 * sum(int(r["payload_len"]) for r in useful) / horizon, 3),
        "quic_detected_loss_ratio": fraction(int(final["lost"]), int(final["sent"])),
        "quic_detected_lost": int(final["lost"]), "quic_counted_sent": int(final["sent"]),
        "quic_spurious_loss": int(final["spurious"]),
        "stream_retransmission_packets": sum(n - 1 for n in sent_streams.values()),
        "quic_tail_loss_probes": int(stats[0]), "cross_path_stream_packets": len(crossed),
        "pending_replaced": transport["pending_replaced"], "acknowledged": transport["acknowledged"],
        "paths": path_usage(client, server, network, origin, horizon),
        "cpu": {"client": cpu_metrics(client), "server": cpu_metrics(server)},
        "network_transitions": len(network) - 1,
        "max_transition_lag_us": max((r["begin_us"] - origin - r["offset_us"] for r in network[1:]), default=0),
        "netem_random_seeded": any(r.get("random_loss_seed") is not None for r in network),
    }
    if case["scenario"] == "repair-probe":
        probe, = [row for row in arrivals if row["id"] == "1"]
        home, = [row["home"] for row in plans.values() if row["id"] == "1"]
        require(home == "0", "repair probe was not bound to the impaired path")
        _, cut, restore = network
        require(any(stream == probe["stream_id"] and path == home and
                    cut["end_us"] <= timestamp < restore["begin_us"]
                    for stream, _, path, timestamp in sent), "probe did not send on A during the outage")
        during = sum(cut["end_us"] <= timestamp < restore["begin_us"] for _, _, _, timestamp in crossed)
        require((during > 0) == (case["ablation"] == "full"), "repair probe did not exercise the cross-path switch")
        result.update(probe_latency_ms=round((int(probe["received"]) - int(probe["generated"])) / 1000, 3),
                      probe_received_before_restore=int(probe["received"]) < restore["begin_us"],
                      cross_path_packets_during_cut=during)
    return result


def flat(row):
    result = {}
    for key, value in row.items():
        if isinstance(value, dict):
            result.update({f"{key}_{field}": val for field, val in flat(value).items()})
        else:
            result[key] = value
    return result


PAIR_FIELDS = ("events_deadline_success_ratio", "states_deadline_success_ratio", "events_latency_p95_ms",
               "mean_aoi_ms", "peak_aoi_ms", "useful_goodput_bps", "stream_retransmission_packets",
               "jain_useful_state_delivery", "cpu_client_one_core_percent")


def matrix(root, write_csv=False):
    manifest = json.loads((root / "matrix.json").read_text())
    rows, by_key = [], {}
    workload_hashes, build_hashes, network_profiles = {}, set(), {}
    for case in manifest["cases"]:
        path = root / case["name"]
        require(json.loads((path / "case.json").read_text()) == case, "run manifest changed")
        settings = case["initial"], case["phases"]
        require(network_profiles.setdefault(case["scenario"], settings) == settings and
                case["horizon_us"] == manifest["horizon_us"] and case["recovery_k"] == manifest["recovery_k"],
                "paired network or experiment settings differ")
        summary = metrics(path)
        require(summary == json.loads((path / "summary.json").read_text()), "stored run metrics changed")
        profile = case["workload"]
        require(workload_hashes.setdefault(profile, summary["workload_sha256"]) == summary["workload_sha256"] ==
                hashlib.sha256((root / f"{profile}.bin").read_bytes()).hexdigest(), "workload differs within pair")
        env = (path / "environment.txt").read_text()
        build = re.search(r"^([a-f0-9]{64}) .*/iot-mpquic$", env, re.M)
        patch = re.search(r"^stream_pto_patch=([a-f0-9]{64})$", env, re.M)
        require(build and patch, "missing executable or patch provenance")
        start, = read_events(path / "client.log")["iot_start"]
        build_hashes.add((build[1], patch[1], start["tx_window"], start["initial_uni_credit"], start["send_packets"]))
        row = {**{k: case[k] for k in ("seed", "scenario", "workload", "policy", "ablation")}, **flat(summary)}
        rows.append(row)
        by_key[case["scenario"], profile, case["policy"], case["ablation"]] = row
    require(len(build_hashes) == 1 and len(by_key) == len(rows), "mixed build or duplicated case")

    comparisons = []
    for key, row in by_key.items():
        scene, workload, policy, ablation = key
        reference = None
        group = ""
        if ablation != "full":
            reference, group = by_key.get((scene, workload, policy, "full")), "ablation"
        elif policy != "fifo" and manifest["suite"] != "repair":
            reference, group = by_key.get((scene, workload, "fifo", "full")), "policy"
        if group:
            require(reference is not None, f"paired reference missing for {row['case']}")
            delta = {field: round(row[field] - reference[field], 8) if row[field] is not None and
                     reference[field] is not None else None for field in PAIR_FIELDS}
            comparisons.append({"group": group, "case": row["case"], "reference": reference["case"], **delta})
        if scene != "stable" and workload == "mixed":
            reference = by_key.get(("stable", workload, policy, ablation))
            require(reference is not None, "stable reference missing")
            delta = {field: round(row[field] - reference[field], 8) if row[field] is not None and
                     reference[field] is not None else None for field in PAIR_FIELDS}
            comparisons.append({"group": "network", "case": row["case"], "reference": reference["case"], **delta})
    if write_csv:
        for name, data in (("metrics.csv", rows), ("paired-deltas.csv", comparisons)):
            with (root / name).open("w", newline="", encoding="utf-8") as output:
                writer = csv.DictWriter(output, data[0].keys())
                writer.writeheader()
                writer.writerows(data)
    return {"status": "passed", "seed": manifest["seed"], "suite": manifest["suite"],
            "verified_runs": len(rows), "comparisons": comparisons}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--matrix", action="store_true")
    parser.add_argument("--write-csv", action="store_true")
    args = parser.parse_args()
    try:
        data = matrix(args.directory, args.write_csv) if args.matrix else metrics(args.directory)
    except (OSError, KeyError, TypeError, ValueError) as exc:
        parser.exit(1, f"Stage 5 verification failed: {exc}\n")
    print(json.dumps(data, indent=2))
