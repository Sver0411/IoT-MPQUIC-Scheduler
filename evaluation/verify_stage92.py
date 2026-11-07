"""Recompute Stage 9.2 metrics and selected-path observation rows."""

import argparse
import json
from collections import Counter
from pathlib import Path

from oracle import app_scheduler_oracle as app
from stage92_timing import distribution
from verify_stage2 import ACCEPTED, EVENT, UPDATED, keyed, read_workload, require
from verify_stage5 import verify_network
from verify_stage7 import endpoint_teardown, verify_arrivals
from verify_stage91 import by_event, log_rows, verify_run as verify_integrity


def record_status(key, submits, arrivals, expired, blocked, no_candidate, replaced, obsolete):
    if key in arrivals:
        return "after-window" if expired else "received"
    if key in submits:
        return "submitted-no-fin"
    if key in replaced:
        return "replaced"
    if key in obsolete:
        return "obsolete"
    if key in blocked:
        return "stream-create-blocked"
    if key in no_candidate:
        return "no-candidate"
    return "unsubmitted-pending"


def observation_rows(case, records, client, arrivals, origin):
    plans = keyed(client["iot_plan"], "id")
    submits = keyed(client["iot_submit"], "id")
    features = {(r["id"], r["path_id"]): r for r in client["iot92_feature"]}
    samples = {(r["id"], r["path_id"]): r for r in client["iot_path_sample"]}
    forecasts = {(r["id"], r["path_id"]): r for r in client["iot_path_forecast"]}
    require(features.keys() == samples.keys() == forecasts.keys() and plans.keys() == submits.keys() and
            len(features) == len(client["iot92_feature"]), "missing or duplicate candidate feature")
    decisions = {r["id"]: r for r in client["iot_app_decision"]}
    blocked = {row["id"] for row in client["iot_stream_create"] if row["blocked"] == "1"}
    no_candidate = {row["id"] for row in client["iot92_no_candidate"]}
    replaced = {row["dropped"] for row in client["iot_replace"]}
    obsolete = {row["id"] for row in client["iot_obsolete"]}
    timeout = {row["id"] for row in client["iot91_timeout"]}
    links = {row["path_id"]: row["link"] for row in client["path_map"]}
    candidates_by_key = {}
    for (key, path_id), sample in samples.items():
        candidates_by_key.setdefault(key, []).append((path_id, sample, forecasts[key, path_id], features[key, path_id]))
    for key, record in records.items():
        if record["release"] >= case["horizon_us"]:
            continue
        plan = plans.get(key)
        arrival = arrivals.get(key)
        received = int(arrival["received"]) - origin if arrival else None
        expired = received is not None and received >= case["horizon_us"]
        status = record_status(key, submits, arrivals, expired, blocked, no_candidate, replaced, obsolete)
        effective_due = (min(record["deadline"], record["generated"] + record["maximum_age"])
                         if record["kind"] != EVENT else record["deadline"])
        selection = int(plan["decision_us"]) if plan else None
        label = int(received is not None and not expired and received <= effective_due)
        result = {"run": case["name"], "split": case["split"], "environment": case["environment"], "seed": case["seed"],
                  "load": case["load"], "budget": case["budget"], "timing": case["timing"], "record_id": int(key),
                  "kind": record["kind"], "release_us": record["release"], "payload_bytes": record["payload_len"],
                  "generated_budget_us": record["deadline"] - record["generated"], "status": status,
                  "decision_us": selection, "effective_slack_us": effective_due - selection if plan else None,
                  "absolute_slack_us": record["deadline"] - selection if plan else None,
                  "received_us": received, "event_on_time": int(received is not None and not expired and
                      received <= record["deadline"] and int(arrival["status"]) == ACCEPTED) if record["kind"] == EVENT else None,
                  "blocked_seen": key in blocked, "no_candidate_seen": key in no_candidate, "timeout": key in timeout,
                  "candidates": []}
        if plan:
            candidates = candidates_by_key[key]
            require(len(candidates) == int(plan["candidates"]), "candidate feature count differs")
            for path_id, sample, forecast, feature in candidates:
                selected = path_id == plan["home"]
                sampled = int(feature["sampled_us"])
                rtt_updated, delivered = int(feature["rtt_updated_us"]), int(feature["delivered_us"])
                require(origin + selection <= sampled <= int(decisions[key]["t_us"]) and
                        all(0 <= t <= sampled for t in (rtt_updated, delivered)), "non-causal path sample time")
                result["candidates"].append({
                    "path_id": int(path_id), "link": links[path_id], "selected": selected,
                    "assignment_probability": 1 / len(candidates),
                    "effective_label": label if selected else None,
                    "absolute_label": int(received is not None and not expired and received <= record["deadline"]) if selected else None,
                    "received_us": received if selected else None,
                    **{k: int(sample[k]) for k in ("rtt_us", "rttvar_us", "scheduled_bytes", "in_flight_bytes", "rate_Bps", "pto")},
                    **{k: int(forecast[k]) for k in ("recent_sent", "recent_lost")},
                    "loss": float(sample["observed_loss"]), "estimate_us": float(forecast["estimate_us"]),
                    "current_probability": float(forecast["probability"]),
                    "cwnd_bytes": int(feature["cwnd_bytes"]), "total_lost": int(feature["total_lost"]),
                    "retransmission_rate": float(feature["retransmission_rate"]), "sampled_us": sampled - origin,
                    "rtt_age_us": sampled - rtt_updated if rtt_updated else None,
                    "delivery_sample_age_us": sampled - delivered if delivered else None})
        yield result


def run_metrics(directory, case, records, client, server, origin):
    horizon = case["horizon_us"]
    cutoff = origin + horizon
    arrivals = [row for row in server["iot_rx"] if int(row["received"]) < cutoff]
    generated = sum(r["kind"] == EVENT and r["release"] < horizon for r in records.values())
    on_time = sum(int(row["status"]) == ACCEPTED and row["on_time"] == "1" for row in arrivals)
    valid_bytes = sum(int(row["payload_len"]) for row in arrivals if int(row["status"]) in (ACCEPTED, UPDATED))
    udp = sum(int(r["bytes"]) for r in server["udp_rx"] if origin <= int(r["recv_us"]) < cutoff)
    result = dict(event_generated=generated, event_on_time=on_time, event_on_time_pp=100 * on_time / generated,
                  goodput_bps=valid_bytes * 8e6 / horizon, udp_throughput_bps=udp * 8e6 / horizon)
    for role, trace in (("client", client), ("server", server)):
        cpu, = trace["process_cpu"]
        rss, = trace["iot92_rss"]
        result[f"{role}_cpu_us"] = int(cpu["user_us"]) + int(cpu["sys_us"])
        result[f"{role}_peak_rss_kib"] = int(rss["peak_kib"])
        files = [directory / f"{role}{suffix}" for suffix in (".log", ".xquic.log")]
        result[f"{role}_log_bytes"] = sum(file.stat().st_size for file in files)
        result[f"{role}_log_lines"] = sum(len(file.read_bytes().splitlines()) for file in files)
        metrics = keyed(trace["iot92_profile"], "metric")
        require(metrics.keys() == {"decision", "create", "submit", "choice_to_send", "log", "digest"}, "missing profile")
        for name, row in metrics.items():
            n, total, maximum = (int(row[k]) for k in ("n", "total_ns", "max_ns"))
            require(n >= 0 and 0 <= maximum <= total <= n * maximum, "invalid profile duration")
            result[f"{role}_{name}_n"] = n
            result[f"{role}_{name}_total_us"] = total / 1000
            result[f"{role}_{name}_mean_us"] = total / n / 1000 if n else None
            result[f"{role}_{name}_max_us"] = maximum / 1000 if n else None
    result["capture_bytes"] = sum(file.stat().st_size for file in directory.glob("*.pcap"))
    for role, trace in (("client", client), ("server", server)):
        require(bool(trace["iot91_open"]) == (case["trace"] in ("logs", "full")) and
                bool(result[f"{role}_digest_n"]) == (case["trace"] in ("digest", "full")), "trace switches disagree")
    require(bool(result["capture_bytes"]) == (case["trace"] in ("capture", "full")), "capture switch disagrees")
    return result


def verify(directory):
    case = json.loads((directory / "case.json").read_text())
    require(case["split"] != "sealed", "sealed test is closed")
    records = read_workload(directory / "workload.bin")
    client, server = [by_event(log_rows(directory / f"{role}.log")) for role in ("client", "server")]
    start, = client["iot_start"]
    origin = int(start["origin"])
    cutoff = origin + case["horizon_us"]
    timeline = verify_network(directory, case, origin)
    for role, trace in (("client", client), ("server", server)):
        endpoint_teardown(directory, role, trace, cutoff)
    if case["trace"] == "full":
        integrity = verify_integrity(directory)
    else:
        app.verify(records, log_rows(directory / "client.log"), origin, case)
        require(keyed(client["iot_input"], "id").keys() ==
                {key for key, row in records.items() if row["release"] < case["horizon_us"]}, "missing admission")
        verify_arrivals(records, keyed(client["iot_submit"], "id"), keyed(server["iot_rx"], "id"),
                        keyed(client["iot_ack"], "id"), origin)
        integrity = None
    result = run_metrics(directory, case, records, client, server, origin)
    lags = [edge["done_us"] - origin - row["offset_us"]
            for row in timeline[1:] for edge in row["directions"].values()]
    return {"status": "passed", "integrity": integrity, "metrics": result, "command_lag_us": distribution(lags)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    print(json.dumps(verify(parser.parse_args().run), indent=2))
