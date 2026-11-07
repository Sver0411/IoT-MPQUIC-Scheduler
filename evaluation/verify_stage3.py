"""Verify application scheduler decisions and compare identical MPQUIC replays."""

import hashlib
import json
import re
import struct
import subprocess
import sys
from pathlib import Path

from verify_stage2 import EVENT, STATE, read_events, read_workload, require, validate

POLICIES = ("fifo", "strict-priority", "edf", "aoi", "latest-rr")
HORIZON_US = 4_000_000


def age_summary(workload, received, origin, horizon_us=HORIZON_US):
    sensors = {r["source"]: [] for r in workload.values() if r["kind"] == STATE}
    if not sensors:
        return None, None
    for row in received:
        if int(row["kind"]) == STATE:
            sensors[int(row["source"])].append((int(row["received"]) - origin, int(row["generated"]) - origin))

    areas_twice = 0
    peak = 0
    for updates in sensors.values():
        start = latest = 0
        for arrival, generated in updates:
            if arrival >= horizon_us or generated <= latest:
                continue
            areas_twice += (arrival - start) * (arrival + start - 2 * latest)
            peak = max(peak, arrival - latest)
            start, latest = arrival, generated
        areas_twice += (horizon_us - start) * (horizon_us + start - 2 * latest)
        peak = max(peak, horizon_us - latest)
    return round(areas_twice / (2 * horizon_us * len(sensors) * 1000), 3), round(peak / 1000, 3)


def run_metrics(root):
    client = read_events(root / "client.log")
    received = read_events(root / "server.log")["iot_rx"]
    start, = client["iot_start"]
    policy = start["policy"]
    require(policy in POLICIES, f"unknown baseline {policy}")
    transport = validate(root, policy)
    workload = read_workload(root / "workload.bin")
    state_generated = sum(r["kind"] == STATE for r in workload.values())
    event_generated = len({r["serial"] for r in workload.values() if r["kind"] == EVENT})
    events = {}
    for row in received:
        if int(row["kind"]) == EVENT:
            events.setdefault(row["serial"], row)
    latencies = sorted(int(r["received"]) - int(r["generated"]) for r in events.values())
    require(len(events) == event_generated and state_generated > 0, "mixed workload or event delivery missing")
    origin = int(start["origin"])
    mean_age, peak_age = age_summary(workload, received, origin)
    return {
        "policy": policy,
        "workload_sha256": hashlib.sha256((root / "workload.bin").read_bytes()).hexdigest(),
        "transport": transport,
        "first_submitted_ids": [int(r["id"]) for r in client["iot_submit"][:12]],
        "events_generated": event_generated,
        "events_on_time": sum(int(r["on_time"]) for r in events.values()),
        "event_mean_ms": round(sum(latencies) / (len(latencies) * 1000), 3),
        "event_p95_ms": round(latencies[(95 * len(latencies) + 99) // 100 - 1] / 1000, 3),
        "states_generated": state_generated,
        "states_on_time": sum(int(r["kind"]) == STATE and r["on_time"] == "1" for r in received),
        "mean_aoi_ms": mean_age,
        "peak_aoi_ms": peak_age,
    }


def compare(root):
    policies = (root / "policies.txt").read_text().splitlines()
    require(len(policies) == len(POLICIES) and set(policies) == set(POLICIES), "missing or duplicate baseline")
    expected_hash = hashlib.sha256((root / "workload.bin").read_bytes()).hexdigest()
    conditions = set()
    runs = []
    for policy in policies:
        path = root / policy
        run = run_metrics(path)
        require(run == json.loads((path / "summary.json").read_text()), f"{policy}: stored summary changed")
        require(run["policy"] == policy and run["workload_sha256"] == expected_hash, "policy or workload differs")
        environment = (path / "environment.txt").read_text()
        exe = re.search(r"^([a-f0-9]{64}) .*/iot-mpquic$", environment, re.M)
        qdiscs = re.findall(r"^qdisc netem \S+ root (?:refcnt \d+ )?(.+)$",
                            (path / "netem-phase-1.txt").read_text(), re.M)
        start, = read_events(path / "client.log")["iot_start"]
        require(exe and len(qdiscs) == 4, "missing build or network profile evidence")
        conditions.add((exe[1], tuple(qdiscs), start["tx_window"],
                        start["initial_uni_credit"], start["send_packets"]))
        runs.append(run)
    require(len(conditions) == 1, "executable, flow control or netem differs between policies")
    return {"status": "passed", "workload_sha256": expected_hash, "aoi_horizon_us": HORIZON_US, "runs": runs}


if __name__ == "__main__":
    try:
        if len(sys.argv) == 2:
            result = run_metrics(Path(sys.argv[1]))
        elif len(sys.argv) == 3 and sys.argv[1] == "--compare":
            result = compare(Path(sys.argv[2]))
        else:
            sys.exit("Usage: verify_stage3.py [--compare] RESULTS_DIRECTORY")
        print(json.dumps(result, indent=2))
    except (OSError, KeyError, ValueError, struct.error, subprocess.CalledProcessError) as exc:
        sys.exit(f"Stage 3 verification failed: {exc}")
