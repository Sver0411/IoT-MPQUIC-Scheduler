"""Independent ranking and probability checks on logged eligible-path measurements."""

import math


def forecast(sample, wire_bytes, slack):
    rtt = int(sample["rtt_us"])
    deviation = max(1000, int(sample["rttvar_us"]))
    queued = int(sample["scheduled_bytes"]) + int(sample["in_flight_bytes"])
    rate = max(15000, int(sample["rate_Bps"]))
    latency = rtt / 2 + 2 * rtt * (int(sample["pto"]) != 0) + (queued + wire_bytes) / rate * 1e6
    lower, upper = latency - deviation, latency + deviation
    timely = min(1.0, max(0.0, (slack - lower) / (upper - lower))) if slack > 0 else 0
    loss = min(.95, (int(sample["recent_lost"]) + 1) / (int(sample["recent_sent"]) + 20))
    return latency, timely * (1 - loss) ** ((wire_bytes + 1199) // 1200), loss


def assignment(seed, key, slack, payload, count):
    tier = 0 if slack <= 0 else 1 if slack <= 100_000 else 2 if slack <= 400_000 else 3
    size = 0 if payload < 1200 else 1 if payload < 3000 else 2
    mask = 2**32 - 1
    value = (seed ^ key * 0x9e3779b9 ^ (tier * 3 + size + 1) * 0x85ebca6b) & mask
    value = ((value ^ (value >> 16)) * 0x7feb352d) & mask
    value = ((value ^ (value >> 15)) * 0x846ca68b) & mask
    return (value ^ (value >> 16)) % count


def validate(case, record, plan, samples, eligible_ids, decision):
    from .app_scheduler_oracle import budget

    key = plan["id"]

    def check(condition, reason):
        if not condition:
            raise ValueError(f"path oracle record {key}: {reason}")

    now = int(plan["decision_us"])
    check(decision["decision_us"] == plan["decision_us"] and decision["id"] == key,
          "application and path decisions differ")
    slack = budget(record, now)
    payload = record["payload_len"]
    paths = [int(sample["path_id"]) for sample in samples]
    check(bool(paths) and len(paths) == len(set(paths)) and set(paths) <= eligible_ids and
          len(paths) == int(plan["candidates"]), "invalid candidate path set")
    predicted = []
    for sample in samples:
        check(all(int(sample[field]) >= 0 for field in ("rtt_us", "rttvar_us", "rate_Bps",
                  "scheduled_bytes", "in_flight_bytes", "recent_lost", "recent_sent", "pto")),
              "negative path counter")
        check(int(sample["queued_bytes"]) == int(sample["scheduled_bytes"]) + int(sample["in_flight_bytes"]),
              "queue sum differs from underlying counters")
        latency, probability, loss = forecast(sample, payload + 52, slack)
        check(math.isclose(loss, float(sample["observed_loss"]), abs_tol=1e-12) and
              math.isclose(loss, float(sample["loss"]), abs_tol=1e-12), "recent loss estimate differs")
        check(math.isclose(latency, float(sample["estimate_us"]), abs_tol=1e-5, rel_tol=0) and
              math.isclose(probability, float(sample["probability"]), abs_tol=1e-8, rel_tol=0),
              "candidate estimate differs")
        predicted.append((latency, probability))
    policy = case["path_scheduler"]
    if policy == "random-path":
        selected = assignment(case["assignment_seed"], int(key), slack, payload, len(paths))
    else:
        rank = {
            "fixed-minrtt": lambda i: int(samples[i]["rtt_us"]),
            "earliest-completion": lambda i: predicted[i][0],
            "proposed-path": lambda i: (-predicted[i][1], predicted[i][0]),
        }[policy]
        selected = min(range(len(paths)), key=rank)
    latency, probability = predicted[selected]
    check(int(plan["home"]) == paths[selected] and int(plan["slack_us"]) == slack and
          math.isclose(latency, float(plan["estimate_us"]), abs_tol=1e-5, rel_tol=0) and
          math.isclose(probability, float(plan["probability"]), abs_tol=1e-8, rel_tol=0),
          f"expected selected path {paths[selected]}, duration {latency}, probability {probability}")
    return predicted
