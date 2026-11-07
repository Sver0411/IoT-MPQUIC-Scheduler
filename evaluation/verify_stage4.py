"""Recompute joint decisions and verify actual STREAM transmissions by affinity mode."""

import hashlib
import json
import math
import re
import struct
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path

from verify_stage2 import STATE, event_rows, keyed, read_events, require, validate

MODES = ("proposed-strict", "proposed-cross")
NORMAL, RECOVERY, EVENT = 1, 2, 3


def sat(value):
    return value / (1 + value)


def estimate(record, path, slack):
    size = 52 + record["payload_len"]
    duration = int(path["rtt_us"]) * (2.5 if int(path["pto"]) else .5)
    duration += (int(path["queued_bytes"]) + size) * 1_000_000 / max(int(path["rate_Bps"]), 15000)
    spread = max(int(path["rttvar_us"]), 1000)
    arrival = min(1, max(0, (slack - duration + spread) / (2 * spread))) if slack > 0 else 0
    return arrival * (1 - float(path["loss"])) ** math.ceil(size / 1200), duration


def obsolete(record, feedback):
    latest = feedback.get(record["source"])
    return record["kind"] == STATE and latest is not None and (record["generated"], record["serial"]) <= latest


def ranked(record, now, paths, feedback, ablation):
    slack = record["deadline"] - now
    gain = excess = 0
    category = EVENT
    if record["kind"] == STATE:
        scale = record["maximum_age"]
        latest = feedback.get(record["source"])
        if latest is None:
            gain = excess = 1
        else:
            gain = sat((record["generated"] - latest[0]) / scale)
            excess = sat(max(0, (now - latest[0]) / scale - 1))
        slack = min(slack, scale - (now - record["generated"]))
        category = NORMAL if slack > 0 or ablation == "no-recovery" else RECOVERY
    if ablation == "no-aoi":
        gain = excess = 0
    priority = 0 if category == EVENT and ablation == "no-event" else record["priority"]
    severity = 0 if ablation == "no-event" else record["severity"]
    options = [(path, *estimate(record, path, slack)) for path in paths]
    path, probability, duration = (options[0] if ablation == "fixed-path" else
                                   max(options, key=lambda option: (option[1], -option[2])))
    wait = sat((now - record["release"]) / 100_000)
    if category == RECOVERY:
        score = sat(priority) + gain + excess + wait
    else:
        score = sat(priority) + 2 * sat(severity) + gain + wait
        score += probability * (1 + 100_000 / (100_000 + max(0, slack)))
    return category, path["path_id"], probability, duration, score


def verify_joint_queue(root, client, workload, origin, policy):
    del policy
    ablation = client.get("iot_ablation", [{"name": "full"}])[0]["name"]
    config, = client["iot_recovery_config"]
    k, burst = int(config["k"]), int(config["event_burst"])
    require(1 <= k <= 64 and burst == 3, "invalid recovery configuration")
    pending, feedback, dropped, expected_replace = {}, {}, {}, {}
    samples = defaultdict(list)
    plans = {}
    submitted = set()
    normal = events = 0
    for row in event_rows(root / "client.log"):
        name = row["event"]
        if name == "iot_input":
            id_ = row["id"]
            record = workload[id_]
            require(int(row["t_us"]) >= origin + record["release"], "early input release")
            if record["kind"] == STATE and ablation != "no-replace":
                for old_id, old in pending.items():
                    if old["kind"] == STATE and old["source"] == record["source"]:
                        newer = (record["generated"], record["serial"]) > (old["generated"], old["serial"])
                        expected_replace[old_id if newer else id_] = id_
                        break
            pending[id_] = record
        elif name in ("iot_replace", "iot_obsolete"):
            id_ = row["dropped"] if name == "iot_replace" else row["id"]
            record = pending.pop(id_)
            require(record["kind"] == STATE and int(row["source"]) == record["source"], "event was dropped")
            if name == "iot_replace":
                require(expected_replace.pop(id_) == row["input"], "wrong latest-value replacement")
            else:
                require(obsolete(record, feedback), "discarded useful state")
            dropped[id_] = name
        elif name == "iot_ack" and workload[row["id"]]["kind"] == STATE:
            sensor = workload[row["id"]]["source"]
            value = int(row["latest_generated"]) - origin, int(row["latest_serial"])
            feedback[sensor] = max(feedback.get(sensor, (0, 0)), value)
        elif name == "iot_path_sample":
            samples[row["id"]].append(row)
        elif name == "iot_plan":
            id_ = row["id"]
            now = int(row["decision_us"])
            paths = samples[id_]
            require(paths and len({path["path_id"] for path in paths}) == len(paths), "missing path samples")
            candidates = {candidate: ranked(record, now, paths, feedback, ablation)
                          for candidate, record in pending.items()
                          if not obsolete(record, feedback)}
            best = {}
            for candidate, plan in candidates.items():
                category = plan[0]
                if category not in best or plan[4] > candidates[best[category]][4]:
                    best[category] = candidate
            if RECOVERY in best and normal >= k:
                selected = best[RECOVERY]
            elif events >= burst and (NORMAL in best or RECOVERY in best):
                selected = best.get(NORMAL, best.get(RECOVERY))
            else:
                options = [best[category] for category in (NORMAL, EVENT) if category in best]
                selected = max(options, key=lambda item: (candidates[item][4], -int(item))) if options else best[RECOVERY]
            require(id_ == selected and id_ not in plans and int(row["normal"]) == normal and
                    int(row["events"]) == events, f"incorrect joint choice for {id_}, expected {selected}")
            category, home, probability, duration, score = candidates[id_]
            require(category == int(row["class"]) and home == row["home"] and
                    all(math.isclose(actual, float(row[key]), abs_tol=1e-6)
                        for actual, key in ((probability, "probability"), (duration, "estimate_us"), (score, "score"))),
                    f"incorrect path estimate or score for {id_}")
            plans[id_] = row
        elif name == "iot_submit":
            id_ = row["id"]
            plan = plans[id_]
            require(id_ not in submitted and plan["stream_id"] == row["stream_id"], "plan/stream mismatch")
            pending.pop(id_)
            submitted.add(id_)
            category = int(plan["class"])
            if category == EVENT:
                events = min(burst, events + 1)
            else:
                events = 0
                normal = min(k, normal + 1) if category == NORMAL else 0
    require(not pending and not expected_replace and plans.keys() == submitted, "incomplete queue trace")
    require(list(keyed(client["iot_input"], "id")) == list(workload), "input does not match workload")
    return dropped


def transmitted_streams(path, include_bytes=False):
    pattern = re.compile(r"\|xqc_send_packet_with_pn\|<==\|.*?\|path:(\d+)\|.*?\|sent:(\d+)\|.*?\|frame:([^|]+)"
                         r"\|.*?\|now:(\d+)\|stream_id:(\d+)\|stream_offset:(\d+)\|")
    with path.open() as file:
        for line in file:
            match = pattern.search(line)
            if match and "STREAM" in match[3].split():
                row = match[5], match[6], match[1], int(match[4])
                yield (*row, int(match[2])) if include_bytes else row


def verify_affinity(root, client, allow_cross):
    plans = keyed(client["iot_plan"], "stream_id")
    packet_decisions = Counter()
    repairs = set()
    for row in client["iot_packet_path"]:
        stream = row["stream_id"]
        require(plans[stream]["home"] == row["home"] and plans[stream]["id"] == row["id"],
                "packet home disagrees with plan")
        key = stream, row["offset"], row["chosen"]
        packet_decisions[key] += 1
        if row["home"] != row["chosen"]:
            require(allow_cross and row["repair"] == "1", "non-repair or strict packet crossed paths")
            repairs.add(key)

    sent = list(transmitted_streams(root / "client.xquic.log"))
    sent_counts = Counter((stream, offset, path) for stream, offset, path, _ in sent)
    require(sent_counts and sent_counts <= packet_decisions, "STREAM transmission bypassed affinity")
    crossed = [packet for packet in sent if packet[:3] in repairs]
    return plans, sent, crossed


def run_metrics(root):
    client = read_events(root / "client.log")
    server = read_events(root / "server.log")
    mode = client["iot_start"][0]["policy"]
    require(mode in MODES, "unknown affinity mode")
    transport = validate(root, mode, verify_joint_queue)
    config, = client["iot_recovery_config"]
    plans, sent, crossed = verify_affinity(root, client, mode == MODES[1])
    transition = (root / "transition.txt").read_text()
    outage = int(re.search(r"^outage_us (\d+)$", transition, re.M)[1])
    restore = int(re.search(r"^restore_begin_us (\d+)$", transition, re.M)[1])
    require("loss 100%" in transition and outage < restore, "missing physical path outage")
    crossed_in_outage = sum(outage <= packet[3] < restore for packet in crossed)
    require(mode == MODES[0] or crossed_in_outage > 0, "no actual cross-path repair during outage")
    first, = [row for row in server["iot_rx"] if row["id"] == "1"]
    first_plan, = [row for row in plans.values() if row["id"] == "1"]
    require(first_plan["home"] == "0" and
            any(stream == first_plan["stream_id"] and path == "0" and outage <= timestamp < restore
                for stream, _, path, timestamp in sent), "test record did not start on the impaired path")
    homes = Counter(int(row["home"]) for row in plans.values())
    require(len(homes) == 2, "joint scheduler did not bind new streams to both paths")
    require(any(row["class"] == str(RECOVERY) for row in plans.values()), "expired state was not recovered")
    return {
        "status": "passed", "policy": mode, "recovery_k": int(config["k"]),
        "workload_sha256": hashlib.sha256((root / "workload.bin").read_bytes()).hexdigest(),
        "transport": transport, "new_streams_by_home": dict(sorted(homes.items())),
        "expired_state_submissions": sum(row["class"] == str(RECOVERY) for row in plans.values()),
        "k_triggered_recoveries": sum(row["class"] == str(RECOVERY) and row["normal"] == config["k"]
                                      for row in plans.values()),
        "obsolete_states": len(client.get("iot_obsolete", [])),
        "cross_path_stream_packets": len(crossed),
        "cross_path_packets_during_outage": crossed_in_outage,
        "probe_event_rx_before_restore": int(first["received"]) < restore,
        "probe_event_on_time": first["on_time"] == "1",
        "probe_event_latency_ms": round((int(first["received"]) - int(first["generated"])) / 1000, 3),
    }


def compare(root):
    expected = hashlib.sha256((root / "workload.bin").read_bytes()).hexdigest()
    # Normalize integer path keys through JSON for comparison with stored evidence.
    runs = [json.loads(json.dumps(run_metrics(root / mode))) for mode in MODES]
    conditions = set()
    for mode, run in zip(MODES, runs):
        path = root / mode
        require(run["policy"] == mode and run["workload_sha256"] == expected, "mode or input differs")
        require(run == json.loads((path / "summary.json").read_text()), "stored summary differs")
        env = (path / "environment.txt").read_text()
        exe = re.search(r"^([a-f0-9]{64}) .*/iot-mpquic$", env, re.M)
        patch = re.search(r"^stream_pto_patch=([a-f0-9]{64})$", env, re.M)
        require(exe and patch, "missing executable or patch hash")
        start, = read_events(path / "client.log")["iot_start"]
        qdiscs = tuple(re.findall(r"^qdisc netem \S+ root (?:refcnt \d+ )?(.+)$",
                                  (path / f"netem-phase-{phase}.txt").read_text(), re.M)
                       for phase in (1, 2))
        require(all(len(qdisc) == 4 for qdisc in qdiscs), "missing qdisc profile")
        conditions.add((exe[1], patch[1], run["recovery_k"], start["tx_window"],
                        start["initial_uni_credit"], start["send_packets"], tuple(map(tuple, qdiscs))))
    require(len(conditions) == 1, "mode comparisons used different build, recovery or link settings")
    return {"status": "passed", "runs": runs}


if __name__ == "__main__":
    try:
        if len(sys.argv) == 2:
            result = run_metrics(Path(sys.argv[1]))
        elif len(sys.argv) == 3 and sys.argv[1] == "--compare":
            result = compare(Path(sys.argv[2]))
        else:
            sys.exit("Usage: verify_stage4.py [--compare] RESULTS_DIRECTORY")
        print(json.dumps(result, indent=2))
    except (OSError, KeyError, ValueError, TypeError, struct.error, subprocess.CalledProcessError) as exc:
        sys.exit(f"Stage 4 verification failed: {exc}")
