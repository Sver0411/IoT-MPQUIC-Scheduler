"""Decision-time feature definitions and causal derivations for Stage 9.3-A."""

from bisect import bisect_left, bisect_right
from collections import defaultdict
from statistics import linear_regression


def feature(group, source, formula, unit, missing="None in eligible snapshots", role="supplemental",
            at="eligible-path snapshot, before initial path selection", available=True, risk="None with this snapshot"):
    return dict(group=group, source=source, formula=formula, unit=unit, missing_rule=missing, role=role,
                sampling_time=at, available_at_decision=available, leakage_risk=risk)


DICTIONARY = {
    "slack_us": feature("task", "workload.bin and iot_plan", "effective deadline minus scheduler tick", "us",
                        role="core", at="start of scheduling iteration"),
    "rtt_us": feature("delay", "iot_path_sample / XQUIC ctl_srtt", "smoothed RTT, not latest RTT sample", "us",
                      role="core"),
    "rttvar_us": feature("delay", "iot_path_sample / XQUIC ctl_rttvar", "QUIC smoothed RTT deviation", "us"),
    "srtt_rate_us_per_s": feature("dynamics", "two distinct same-path RTT updates", "1e6 * delta SRTT / delta update time",
                                  "us/s", "fewer than two observed RTT updates", "environment-dependent"),
    "srtt_trend_us_per_s": feature("dynamics", "three distinct same-path RTT updates",
                                   "least-squares SRTT slope over the last three observed updates", "us/s",
                                   "fewer than three observed RTT updates", "environment-dependent"),
    "rtt_to_slack": feature("delay", "SRTT and effective slack", "SRTT / slack", "ratio",
                            "slack <= 0", "redundant"),
    "cwnd_bytes": feature("congestion", "iot92_feature / XQUIC congestion control", "current congestion window", "bytes"),
    "in_flight_bytes": feature("congestion", "iot_path_sample / XQUIC send controller", "current bytes in flight", "bytes"),
    "scheduled_bytes": feature("congestion", "iot_path_sample / XQUIC path_schedule_bytes",
                               "bytes queued for path scheduling, excluding in-flight and kernel queue", "bytes",
                               role="unsuitable"),
    "legacy_queue_bytes": feature("congestion", "iot_path_sample.queued_bytes",
                                  "scheduled_bytes + in_flight_bytes", "bytes", role="redundant"),
    "queue_delay_proxy_us": feature("congestion", "scheduled bytes and rate proxy", "1e6 * scheduled_bytes / rate_Bps", "us",
                                    "rate proxy <= 0", "unsuitable"),
    "rate_Bps": feature("congestion", "iot_path_sample / path_affinity.c",
                        "floor(cwnd * 1e6 / SRTT) when SRTT > 0, else 0; not delivery rate or link bandwidth", "bytes/s",
                        role="redundant"),
    "send_budget_bytes": feature("congestion", "cwnd and bytes in flight", "max(0, cwnd - in_flight); no pacing or flow control",
                                 "bytes", role="redundant"),
    "loss": feature("reliability", "iot_path_sample.observed_loss",
                    "min(0.95, (recent_lost+1)/(recent_sent+20)); counter proxy, not netem loss", "ratio",
                    role="environment-dependent"),
    "total_lost": feature("reliability", "iot92_feature / ctl_lost_count", "cumulative QUIC packets declared lost", "packets",
                           role="environment-dependent"),
    "retransmission_rate": feature("reliability", "iot92_feature / xqc_send_ctl_get_retransmission_rate",
                                   "(lost count + TLP count) / send count; zero without sends", "ratio", role="environment-dependent"),
    "pto": feature("reliability", "iot_path_sample / ctl_pto_count", "current PTO counter", "count", role="unsuitable"),
    "recent_loss_increment": feature("reliability", "same-path snapshots of ctl_lost_count",
                                     "current minus preceding distinct snapshot; no exact loss event time", "packets",
                                     "no preceding snapshot or counter reset"),
    "payload_bytes": feature("task", "workload.bin", "application payload excluding framing", "bytes", role="core",
                             at="record admission"),
    "priority": feature("task", "workload.bin", "encoded event priority (state records use 1)", "code", at="record admission"),
    "kind": feature("task", "workload.bin", "1 = state, 2 = event", "code", at="record admission"),
    "admissions_100ms": feature("task", "iot_input", "records admitted strictly inside (sample time - 100ms, sample time)", "records",
                               role="environment-dependent"),
    "path_frozen": feature("reliability", "Stage 9.1 eligibility checks", "0 on eligible candidates; frozen paths excluded", "flag",
                           role="unsuitable"),
    "rtt_change_us": feature("dynamics", "two distinct same-path RTT updates", "current minus previous observed SRTT", "us",
                              "fewer than two observed updates", "redundant"),
    "rttvar_change_us": feature("dynamics", "two distinct same-path RTT updates", "current minus previous observed RTTVAR", "us",
                                 "fewer than two observed updates"),
    "rate_change_Bps": feature("dynamics", "two distinct same-path snapshots", "current minus previous rate proxy", "bytes/s",
                                "no preceding snapshot", "redundant"),
    "scheduled_growth_Bps": feature("dynamics", "two distinct same-path snapshots",
                                    "1e6 * delta scheduled bytes / delta snapshot time", "bytes/s", "no preceding snapshot",
                                    "unsuitable"),
    "srtt_order_flip": feature("dynamics", "two paths and previous comparable dual-candidate snapshot",
                               "1 when current non-tied SRTT ordering differs from last comparable ordering", "flag",
                               "no preceding comparable snapshot, one candidate or current tie", "environment-dependent"),
    "rtt_age_us": feature("dynamics", "iot92_feature", "sampled_us minus rtt_updated_us", "us",
                          "no recorded RTT update", "environment-dependent"),
    "delivery_sample_age_us": feature("dynamics", "iot92_feature / ctl_delivered_time",
                                      "sampled_us minus prior path delivery sample time, not this record's receipt", "us",
                                      "no prior delivery sample", "environment-dependent"),
    "snapshot_age_at_tick_delta_us": feature("dynamics", "iot92_feature and iot_plan",
                                             "snapshot time minus scheduler tick; instrumentation diagnostic", "us",
                                             role="unsuitable", risk="May encode build and tracing load"),
    "current_probability": feature("derived", "iot_path_forecast", "legacy forecast using slack and the causal snapshot", "ratio",
                                    role="redundant"),
    "latest_rtt_us": feature("delay", "unarchived raw RTT measurements", "most recent unsmoothed RTT sample", "us",
                             "not logged", "unsuitable", at="would need previous RTT sample hook", available=False,
                             risk="Do not reconstruct from packet timing after the decision"),
    "kernel_queue_packets": feature("congestion", "unarchived per-decision qdisc samples", "actual queue packet count", "packets",
                                    "not logged", "unsuitable", at="would need causal data-plane instrumentation", available=False,
                                    risk="Do not join nearest future queue probe"),
    "true_queue_delay_us": feature("congestion", "unarchived enqueue/dequeue per-path telemetry",
                                   "pre-decision packet queue residence times", "us", "not logged", "unsuitable",
                                   at="would need prior packet telemetry", available=False,
                                   risk="Do not use the selected record's eventual delay"),
    "true_bandwidth_change_Bps": feature("dynamics", "unarchived per-decision link measurement",
                                        "difference in measured capacity", "bytes/s", "not logged", "unsuitable",
                                        at="would need past-only estimation", available=False,
                                        risk="Configured future phases or downstream observations leak information"),
    "actual_retransmissions": feature("reliability", "unarchived per-decision retransmission count",
                                      "previous path retransmissions, not current record's subsequent repairs", "packets",
                                      "not logged as a snapshot counter", "unsuitable", at="would need prior transport counters",
                                      available=False, risk="Current record's retransmissions are downstream of selection"),
    "path_recovery_state": feature("reliability", "endpoint path lifecycle",
                                   "path recovery age/state was not joined into the eligible snapshot", "code",
                                   "not present in the feature snapshot", "unsuitable", at="would need past-only lifecycle join",
                                   available=False, risk="Do not backfill from eventual recovery or repair"),
    "record_binding_state": feature("task", "iot91_bind", "initial binding created after choosing the path", "code",
                                    "no pre-selection binding value", "unsuitable", at="after initial selection", available=False,
                                    risk="Encodes assignment directly"),
    "configured_load": feature("task", "case.json", "low or high workload generator setting", "category",
                                "analysis stratum, not endpoint telemetry", "unsuitable", at="before run", available=False,
                                risk="Environment and batch shortcut; use admissions instead"),
    "configured_phase": feature("dynamics", "network.jsonl and case.json",
                                 "command-timeline stratum with a 5 ms guard, not measured path condition", "category",
                                 "analysis stratum, not endpoint telemetry", "unsuitable", at="offline network timeline",
                                 available=False, risk="Uses configuration and can depend on future command completion"),
    "receive_or_ack_time": feature("outcome", "iot_rx / iot_ack / wire telemetry",
                                   "receive and ACK timing retained as audit evidence only", "us", "unknown before selection",
                                   "unsuitable", at="after selection", available=False, risk="Direct target leakage"),
    "experiment_identity": feature("provenance", "case manifest and run",
                                   "seed, environment, connection, record identity and split", "ID",
                                   "audit and grouping only", "unsuitable", at="before run", available=False,
                                   risk="Assignment or environment shortcut, keep out of model"),
}

RAW = ("rtt_us", "rttvar_us", "cwnd_bytes", "in_flight_bytes", "scheduled_bytes", "rate_Bps",
       "loss", "total_lost", "retransmission_rate", "pto", "rtt_age_us", "delivery_sample_age_us", "current_probability")

COMBINATIONS = {
    "slack_rtt": ("slack_us", "rtt_us"),
    "slack_rtt_rttvar": ("slack_us", "rtt_us", "rttvar_us"),
    "slack_rtt_in_flight": ("slack_us", "rtt_us", "in_flight_bytes"),
    "slack_rtt_cwnd": ("slack_us", "rtt_us", "cwnd_bytes"),
    "slack_rtt_loss_pto": ("slack_us", "rtt_us", "loss", "pto"),
    "full_network_state": ("slack_us", "rtt_us", "rttvar_us", "scheduled_bytes", "in_flight_bytes", "cwnd_bytes",
                           "loss", "pto", "rtt_age_us", "delivery_sample_age_us", "srtt_rate_us_per_s",
                           "rttvar_change_us", "scheduled_growth_Bps", "recent_loss_increment", "srtt_order_flip"),
}


def add_features(rows, admission_times):
    previous = {}
    updates = defaultdict(list)
    previous_order = None
    previous_tick = None
    for row in sorted((r for r in rows if r["candidates"]), key=lambda r: r["decision_attempt"]):
        tick = row["decision_us"]
        if previous_tick is not None and tick < previous_tick:
            raise ValueError("non-monotonic decision history")
        previous_tick = tick
        candidates = row["candidates"]
        slack = row["effective_slack_us"]
        rtts = {c["link"]: c["rtt_us"] for c in candidates}
        order = (rtts["A"] > rtts["B"]) - (rtts["A"] < rtts["B"]) if len(rtts) == 2 else 0
        flip = int(order != previous_order) if order and previous_order is not None else None
        if order:
            previous_order = order
        for candidate in candidates:
            path = candidate["path_id"]
            sampled = candidate["sampled_us"]
            old = previous.get(path)
            if sampled < tick or (old and old["sampled_us"] > sampled):
                raise ValueError("non-causal snapshot history")
            rate = candidate["rate_Bps"]
            rtt = candidate["rtt_us"]
            cwnd = candidate["cwnd_bytes"]
            scheduled = candidate["scheduled_bytes"]
            flight = candidate["in_flight_bytes"]
            history = updates[path]
            age = candidate["rtt_age_us"]
            if age is not None:
                update = sampled - age
                if age < 0 or (history and update < history[-1][0]):
                    raise ValueError("non-causal RTT update history")
                if not history or update > history[-1][0]:
                    history.append((update, rtt, candidate["rttvar_us"]))
                    del history[:-3]
            # Repeated snapshots are valid, but cannot establish a time derivative.
            delta = sampled - old["sampled_us"] if old else 0
            loss_delta = candidate["total_lost"] - old["total_lost"] if delta else -1
            drtt = dvar = velocity = trend = None
            if age is not None and len(history) >= 2:
                a, b = history[-2:]
                drtt, dvar = b[1] - a[1], b[2] - a[2]
                velocity = 1e6 * drtt / (b[0] - a[0])
            if age is not None and len(history) == 3:
                trend = 1e6 * linear_regression([p[0] for p in history], [p[1] for p in history]).slope
            candidate["features"] = {
                **{key: candidate[key] for key in RAW},
                "slack_us": slack, "payload_bytes": row["payload_bytes"], "priority": row["priority"], "kind": row["kind"],
                "path_frozen": 0, "rtt_to_slack": rtt / slack if slack > 0 else None,
                "legacy_queue_bytes": scheduled + flight, "queue_delay_proxy_us": 1e6 * scheduled / rate if rate else None,
                "send_budget_bytes": max(0, cwnd - flight),
                "recent_loss_increment": loss_delta if loss_delta >= 0 else None,
                "admissions_100ms": bisect_left(admission_times, sampled) - bisect_right(admission_times, sampled - 100_000),
                "srtt_order_flip": flip, "rtt_change_us": drtt, "rttvar_change_us": dvar, "srtt_rate_us_per_s": velocity,
                "srtt_trend_us_per_s": trend, "rate_change_Bps": rate - old["rate_Bps"] if delta else None,
                "scheduled_growth_Bps": 1e6 * (scheduled - old["scheduled_bytes"]) / delta if delta else None,
                "snapshot_age_at_tick_delta_us": sampled - tick,
            }
            previous[path] = candidate
