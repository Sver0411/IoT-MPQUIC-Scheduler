"""Seed-level contrasts, paired bootstrap, and selected-path calibration."""

import itertools
import math
import random
import statistics
from collections import defaultdict

METRICS = ("event_deadline_success", "event_latency_p95_ms", "mean_aoi_ms",
           "maximum_age_violation_ratio", "state_deadline_success", "useful_state_delivery",
           "jain_delivery", "jain_freshness", "useful_goodput_bps", "stream_repeat_offsets",
           "stream_repeat_byte_ratio", "cross_path_byte_ratio",
           "cross_path_packets", "cross_during_outage", "complete_before_restore",
           "event_recovery_ms", "state_recovery_ms", "probe_latency_ms", "client_cpu_percent")

FACTOR_CONTRASTS = {
    "B-A": {"B": 1, "A": -1}, "D-C": {"D": 1, "C": -1},
    "C-A": {"C": 1, "A": -1}, "D-B": {"D": 1, "B": -1},
    "D-B-C+A": {"D": 1, "B": -1, "C": -1, "A": 1},
    "D-A": {"D": 1, "A": -1},
}


def quantile(values, probability):
    ordered = sorted(values)
    point = (len(ordered) - 1) * probability
    index = int(point)
    return ordered[index] + (ordered[math.ceil(point)] - ordered[index]) * (point - index)


def paired_deltas(rows):
    blocks = defaultdict(dict)
    for row in rows:
        cells = blocks[row["suite"], row["scenario"], row["seed"]]
        if row["cell"] in cells:
            raise ValueError("duplicate cell in paired block")
        cells[row["cell"]] = row
    for (suite, scenario, seed), cells in sorted(blocks.items()):
        if set(cells) == {"A", "B", "C", "D"}:
            contrasts = FACTOR_CONTRASTS
        elif set(cells) == {"Cross", "Strict"}:
            contrasts = {"Cross-Strict": {"Cross": 1, "Strict": -1}}
        elif "full" in cells:
            contrasts = {f"{cell}-full": {cell: 1, "full": -1} for cell in cells if cell != "full"}
        else:
            raise ValueError(f"incomplete paired block {suite}/{scenario}/{seed}")
        for name, contrast in contrasts.items():
            for metric in METRICS:
                values = [(weight, cells[cell].get(metric)) for cell, weight in contrast.items()]
                if all(value is not None for _, value in values):
                    yield {"suite": suite, "scenario": scenario, "seed": seed, "contrast": name,
                           "metric": metric, "delta": sum(weight * value for weight, value in values)}


def summarize(values, inferential=False):
    n = len(values)
    mean = statistics.fmean(values)
    sd = statistics.stdev(values) if n > 1 else None
    result = dict(n=n, mean=mean, median=statistics.median(values), sd=sd, ci_low=None, ci_high=None,
                  paired_dz=None, sign_flip_p=None)
    if n > 1:
        rng = random.Random(7007)
        means = [statistics.fmean(rng.choices(values, k=n)) for _ in range(4000)]
        result.update(ci_low=quantile(means, .025), ci_high=quantile(means, .975),
                      paired_dz=mean / sd if inferential and sd else None)
        if inferential:
            observed = abs(sum(values))
            if n <= 16:
                signs = itertools.product((-1, 1), repeat=n)
                count, adjustment = 2**n, 0
            else:
                count, adjustment = 20_000, 1
                signs = (rng.choices((-1, 1), k=n) for _ in range(count))
            extreme = sum(abs(sum(s * d for s, d in zip(sign, values))) >= observed - 1e-12
                          for sign in signs)
            result["sign_flip_p"] = (extreme + adjustment) / (count + adjustment)
    return result


def summarize_rows(rows, deltas):
    grouped = defaultdict(list)
    for row in rows:
        for metric in METRICS:
            if row.get(metric) is not None:
                grouped["cell", row["suite"], row["scenario"], row["cell"], metric].append(row[metric])
    for row in deltas:
        grouped["contrast", row["suite"], row["scenario"], row["contrast"], row["metric"]].append(row["delta"])
    output = [{"kind": kind, "suite": suite, "scenario": scene, "label": label, "metric": metric,
               **summarize(values, kind == "contrast"), "holm_primary_p": None}
              for (kind, suite, scene, label, metric), values in sorted(grouped.items())]
    family = sorted((r for r in output if r["suite"] == "factorial" and r["kind"] == "contrast" and
                     r["label"] in ("B-A", "C-A", "D-B-C+A") and r["sign_flip_p"] is not None and
                     r["metric"] in ("event_deadline_success", "mean_aoi_ms")), key=lambda r: r["sign_flip_p"])
    running = 0
    for i, row in enumerate(family):
        running = max(running, row["sign_flip_p"] * (len(family) - i))
        row["holm_primary_p"] = min(running, 1)
    return output


def calibration(rows):
    by_group = defaultdict(list)
    for row in rows:
        for dimension, value in (("overall", "all"), ("scenario", row["scenario"]),
                                 ("kind", row["kind"]), ("payload", row["payload_bin"]),
                                 ("slack", row["slack_bin"]), ("kind/scenario", f"{row['kind']}/{row['scenario']}")):
            by_group[row["suite"], dimension, value].append(row)
    scores, bins = [], []
    for (suite, dimension, value), selected in sorted(by_group.items()):
        errors = [(r["estimated_completion_delay_us"] - r["actual_completion_delay_us"]) / 1000
                  for r in selected if r["actual_completion_delay_us"] is not None]
        timing = {"completed_n": len(errors), "time_bias_ms": statistics.fmean(errors) if errors else None,
                  "time_mae_ms": statistics.fmean(map(abs, errors)) if errors else None}
        for target in ("deadline_met", "budget_met"):
            pairs = [(row["predicted_probability"], row[target]) for row in selected]
            summary, reliability = score(pairs)
            metadata = dict(suite=suite, dimension=dimension, value=value, target=target)
            scores.append({**metadata, **summary, **timing})
            bins.extend({**metadata, **row} for row in reliability)
    return scores, bins


def score(pairs):
    slots = defaultdict(list)
    for p, y in pairs:
        slots[min(9, int(p * 10))].append((p, y))
    total = len(pairs)
    bins = [{"bin_lower": i / 10, "bin_upper": (i + 1) / 10, "n": len(group),
             "predicted": statistics.fmean(p for p, _ in group),
             "observed": statistics.fmean(y for _, y in group)} for i, group in sorted(slots.items())]
    negative_seen = 0
    concordant = 0
    positives = sum(y for _, y in pairs)
    for _, ties in itertools.groupby(sorted(pairs), key=lambda row: row[0]):
        outcomes = [y for _, y in ties]
        successes = sum(outcomes)
        failures = len(outcomes) - successes
        concordant += successes * (negative_seen + failures / 2)
        negative_seen += failures
    auc = concordant / (positives * negative_seen) if positives and negative_seen else None
    return {"n": total, "brier": statistics.fmean((p - y) ** 2 for p, y in pairs),
            "ece": sum(row["n"] * abs(row["predicted"] - row["observed"]) for row in bins) / total,
            "observed": statistics.fmean(y for _, y in pairs), "roc_auc": auc}, bins


def heldout_calibration(rows):
    rows = [row for row in rows if row["suite"] == "factorial"]
    seeds = sorted({row["seed"] for row in rows})
    if len(seeds) < 10:
        return []
    # Alternate seed assignments so both partitions include the high-load extension.
    train_seeds, test_seeds = set(seeds[::2]), set(seeds[1::2])
    results = []
    for target in ("deadline_met", "budget_met"):
        train_rows = [row for row in rows if row["seed"] in train_seeds]
        test_rows = [row for row in rows if row["seed"] in test_seeds]
        train = [(row["predicted_probability"], row[target]) for row in train_rows]
        test = [(row["predicted_probability"], row[target]) for row in test_rows]
        # Pool adjacent violations of monotonicity on the 10 predeclared probability bins.
        slots = defaultdict(list)
        for p, y in train:
            slots[min(9, int(p * 10))].append(y)
        blocks = []
        for slot, group in sorted(slots.items()):
            blocks.append((slot, len(group), sum(group)))
            while len(blocks) > 1 and blocks[-2][2] / blocks[-2][1] > blocks[-1][2] / blocks[-1][1]:
                right, left = blocks.pop(), blocks.pop()
                blocks.append((right[0], left[1] + right[1], left[2] + right[2]))
        def predict(p):
            slot = min(9, int(p * 10))
            _, n, successes = next((block for block in blocks if slot <= block[0]), blocks[-1])
            return successes / n
        prior = statistics.fmean(y for _, y in train)
        slack_groups = defaultdict(list)
        for row in train_rows:
            slack_groups[row["slack_us"] > 0].append(row[target])
        slack_rates = {positive: statistics.fmean(outcomes) for positive, outcomes in slack_groups.items()}
        slack_only = [(slack_rates[row["slack_us"] > 0], row[target]) for row in test_rows]
        for model, pairs in (("raw", test), ("training-base-rate", [(prior, y) for _, y in test]),
                             ("slack-sign", slack_only),
                             ("monotone-10-bin", [(predict(p), y) for p, y in test])):
            summary, _ = score(pairs)
            results.append({"target": target, "model": model, "train_seeds": sorted(train_seeds),
                            "test_seeds": sorted(test_seeds), **summary})
    return results
