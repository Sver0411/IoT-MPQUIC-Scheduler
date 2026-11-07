"""Specification-based replay of the Stage 8 FIFO and Proposed-App decisions."""

import math
from fractions import Fraction

STATE = 1
NORMAL, RECOVERY, EVENT = 1, 2, 3
WAIT_US = 100_000


def version(record):
    return record["generated"], record["serial"]


def budget(record, now):
    due = record["deadline"]
    if record["kind"] == STATE:
        due = min(due, record["generated"] + record["maximum_age"])
    return due - now


def ratio(value, scale=1):
    return Fraction(value, value + scale)


def candidate(record, now, known, tuning):
    """Evaluate the independent mathematical specification in exact rational arithmetic."""
    slack = budget(record, now)
    priority = ratio(record["priority"])
    waiting = Fraction(str(tuning["waiting_weight"])) * ratio(now - record["release"], WAIT_US)
    if record["kind"] != STATE:
        severity = Fraction(str(tuning["severity_weight"])) * ratio(record["severity"])
        return EVENT, priority + waiting + severity + Fraction(WAIT_US, WAIT_US + max(0, slack))
    age = record["maximum_age"]
    gain = ratio(record["generated"] - known[0], age) if known else 1
    if slack > 0:
        return NORMAL, priority + waiting + gain + Fraction(WAIT_US, WAIT_US + slack)
    excess = ratio(max(0, now - known[0] - age), age) if known else 1
    return RECOVERY, priority + waiting + gain + excess


def select(pending, records, feedback, now, case, normal, events):
    if case["policy"] == "fifo":
        return (min(pending, key=int), 0, 0) if pending else None
    ranked = {}
    tuning = case["tuning"]
    for key in sorted(pending, key=int):
        record = records[key]
        known = feedback.get(record["source"]) if record["kind"] == STATE else None
        if known is not None and version(record) <= known:
            continue
        category, score = candidate(record, now, known, tuning)
        ranked.setdefault(category, []).append((key, category, score))

    def best(categories):
        rows = [row for category in categories for row in ranked.get(category, ())]
        return max(rows, key=lambda row: (row[2], -int(row[0])), default=None)

    if RECOVERY in ranked and normal >= case["recovery_k"]:
        return best([RECOVERY])
    if events >= tuning["event_burst"] and (NORMAL in ranked or RECOVERY in ranked):
        return best([NORMAL]) or best([RECOVERY])
    return best([NORMAL, EVENT]) or best([RECOVERY])


def verify(records, rows, origin, case):
    pending, feedback = set(), {}
    normal = events = admitted = decisions = 0
    batches = iter(records)
    next_input = next(batches, None)
    expected_changes = []
    attempted = None
    snapshot = None

    def check(condition, row, reason):
        if not condition:
            raise ValueError(f"application oracle at {row['event']} {row.get('id', '')}: {reason}")

    for row in rows:
        event = row["event"]
        if event == "iot_admission":
            check(not expected_changes and attempted is None, row, "missing queue or stream-create outcome")
            tick = int(row["elapsed"])
            check(0 <= tick <= int(row["t_us"]) - origin, row, "invalid admission clock")
            while next_input is not None and records[next_input]["release"] <= tick:
                key = next_input
                record = records[key]
                expected_changes.append(("iot_input", key, None))
                if case["policy"] == "proposed-app" and record["kind"] == STATE:
                    old = next((k for k in pending if records[k]["kind"] == STATE and
                                records[k]["source"] == record["source"]), None)
                    if old is not None:
                        drop = old if version(records[old]) < version(record) else key
                        expected_changes.append(("iot_replace", key, drop))
                        pending.discard(drop)
                        if drop == key:
                            next_input = next(batches, None)
                            continue
                pending.add(key)
                next_input = next(batches, None)
            if case["policy"] == "proposed-app":
                for key in sorted(pending, key=int):
                    record = records[key]
                    known = feedback.get(record["source"]) if record["kind"] == STATE else None
                    if known is not None and version(record) <= known:
                        expected_changes.append(("iot_obsolete", key, None))
                        pending.remove(key)
        elif event in ("iot_input", "iot_replace", "iot_obsolete"):
            key = row["input"] if event == "iot_replace" else row["id"]
            check(bool(expected_changes) and expected_changes.pop(0) ==
                  (event, key, row.get("dropped")), row, "unexpected admission, replacement or obsolete record")
            admitted += event == "iot_input"
        elif event == "iot_feedback":
            check(snapshot is None, row, "missing ACK for previous feedback")
            snapshot = row
        elif event == "iot_ack":
            record = records[row["id"]]
            if case["policy"] == "proposed-app" and record["kind"] == STATE:
                source = record["source"]
                before = feedback.get(source)
                offered = int(row["latest_generated"]) - origin, int(row["latest_serial"])
                after = max(before, offered) if before is not None else offered
                previous_age = int(row["client_received"]) - origin - before[0] if before is not None else -1
                new_age = int(row["client_received"]) - origin - after[0]
                check(snapshot is not None and snapshot["id"] == row["id"], row, "missing feedback log")
                check((snapshot["feedback_before"], snapshot["feedback_after"]) == (
                    "-1/0" if before is None else f"{before[0]}/{before[1]}", f"{after[0]}/{after[1]}") and
                    (int(snapshot["aoi_before"]), int(snapshot["aoi_after"])) == (previous_age, new_age),
                    row, "ACK snapshot or AoI transition differs")
                feedback[source] = after
                snapshot = None
        elif event == "iot_app_decision":
            check(not expected_changes and attempted is None, row, "incomplete admission or previous decision")
            now = int(row["decision_us"])
            check(0 <= now <= int(row["t_us"]) - origin, row, "decision clock outside replay")
            choice = select(pending, records, feedback, now, case, normal, events)
            check(choice is not None, row, "decision has no eligible candidate")
            key, category, score = choice
            check((row["id"], int(row["kind"]), int(row["class"])) ==
                  (key, records[key]["kind"], category), row, f"expected record {key} class {category}")
            check(math.isclose(float(row["score"]), float(score), abs_tol=1e-10, rel_tol=0),
                  row, f"expected score {float(score):.16g}")
            check((int(row["normal"]), int(row["events"])) == (normal, events),
                  row, f"expected quota {normal}/{events}")
            attempted = key, category, now
            decisions += 1
        elif event == "iot_stream_create" and row["blocked"] == "1":
            check(attempted is not None and attempted[0] == row["id"], row, "blocked record differs from decision")
            attempted = None
        elif event == "iot_submit":
            check(attempted is not None and attempted[0] == row["id"], row, "missing matching decision")
            key, category, now = attempted
            check(origin + now <= int(row["submitted"]), row, "submission precedes decision")
            pending.remove(key)
            if category == EVENT:
                events = min(events + 1, case["tuning"]["event_burst"])
            elif category:
                events = 0
                normal = 0 if category == RECOVERY else min(normal + 1, case["recovery_k"])
            attempted = None
    if expected_changes or attempted is not None or snapshot is not None or not decisions:
        raise ValueError("application oracle: incomplete decision evidence")
    return {"admitted": admitted, "decisions": decisions}
