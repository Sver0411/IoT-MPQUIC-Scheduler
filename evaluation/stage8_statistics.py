"""Paired seed-level contrasts and independent holdout probability evaluation."""

import math
import statistics
from collections import defaultdict

from stage7_statistics import FACTOR_CONTRASTS, score, summarize

CONTRASTS = {
    **FACTOR_CONTRASTS, "D-Fixed": {"D": 1, "Fixed": -1},
    "D-Earliest": {"D": 1, "Earliest": -1}, "Cross-D": {"Cross": 1, "D": -1},
}
METRICS = (
    "event_deadline_success", "mean_aoi_ms", "maximum_age_violation_ratio", "state_deadline_success",
    "useful_state_delivery", "mean_useful_state_age_ms", "event_latency_p50_ms", "event_latency_p95_ms",
    "event_latency_p99_ms", "record_latency_p50_ms", "record_latency_p95_ms", "record_latency_p99_ms",
    "useful_goodput_bps", "quic_detected_loss_ratio", "stream_repeat_udp_payload_bytes",
    "path_a_udp_rx_bytes", "path_b_udp_rx_bytes", "path_a_utilization", "path_b_utilization",
    "cross_path_packets", "cross_during_outage", "complete_before_restore", "probe_latency_ms",
    "event_recovery_ms", "state_recovery_ms",
)
EPS = 1e-6


def holm(rows, field="sign_flip_p"):
    order = sorted((row for row in rows if row[field] is not None), key=lambda row: row[field])
    adjusted = 0
    for i, row in enumerate(order):
        adjusted = max(adjusted, min(1, (len(order) - i) * row[field]))
        row["holm_p"] = adjusted


def paired(runs, config):
    blocks, groups = defaultdict(dict), defaultdict(list)
    for row in runs:
        if row["suite"] != "paired":
            continue
        cells = blocks[row["scenario"], row["seed"]]
        if row["cell"] in cells:
            raise ValueError("duplicate paired cell")
        cells[row["cell"]] = row
        for metric in METRICS:
            if row.get(metric) is not None:
                groups["cell", row["scenario"], row["cell"], metric].append(row[metric])
    deltas = []
    for (scenario, seed), cells in sorted(blocks.items()):
        contrasts = {"Cross-D": CONTRASTS["Cross-D"]} if scenario == "repair" else CONTRASTS
        for label, weights in contrasts.items():
            for metric in METRICS:
                values = [cells[cell].get(metric) for cell in weights]
                if any(value is None for value in values):
                    continue
                delta = sum(weight * value for weight, value in zip(weights.values(), values))
                deltas.append(dict(scenario=scenario, seed=seed, contrast=label, metric=metric, delta=delta))
                groups["contrast", scenario, label, metric].append(delta)
    summary = [
        dict(kind=kind, scenario=scenario, label=label, metric=metric,
             **summarize(values, kind == "contrast" and metric in config["primary_metrics"]), holm_p=None)
        for (kind, scenario, label, metric), values in sorted(groups.items())
    ]
    holm([row for row in summary if row["kind"] == "contrast" and row["scenario"] != "repair"
          and row["label"] in config["primary_contrasts"] and row["metric"] in config["primary_metrics"]])
    return deltas, summary


def sigmoid(z):
    return 1 / (1 + math.exp(-max(-40, min(40, z))))


def clip(p):
    return min(1 - EPS, max(EPS, p))


def logit(p):
    p = clip(p)
    return math.log(p / (1 - p))


def fit_logistic(xs, ys):
    """One-variable logistic regression with a small slope penalty for separation."""
    center = statistics.fmean(xs)
    scale = statistics.pstdev(xs) or 1
    features = [(x - center) / scale for x in xs]
    intercept = logit(statistics.fmean(ys))
    slope = 0.0
    ridge = .1
    for _ in range(80):
        p = [sigmoid(intercept + slope * x) for x in features]
        residual = [y - q for y, q in zip(ys, p)]
        weights = [q * (1 - q) for q in p]
        a = sum(weights)
        b = sum(w * x for w, x in zip(weights, features))
        c = ridge + sum(w * x * x for w, x in zip(weights, features))
        g = sum(residual)
        h = sum(r * x for r, x in zip(residual, features)) - ridge * slope
        det = a * c - b * b
        di, ds = (c * g - b * h) / det, (a * h - b * g) / det
        step = max(1, abs(di), abs(ds))
        intercept += di / step
        slope += ds / step
        if max(abs(di), abs(ds)) < 1e-8:
            break
    return intercept - slope * center / scale, slope / scale


def model_metrics(pairs):
    result, bins = score(pairs)
    labels = [y for _, y in pairs]
    result["log_loss"] = statistics.fmean(-math.log(clip(p if y else 1 - p)) for p, y in pairs)
    result["calibration_intercept"] = result["calibration_slope"] = None
    logits = [logit(p) for p, _ in pairs]
    if 0 < sum(labels) < len(labels) and len(set(logits)) > 1:
        intercept, slope = fit_logistic(logits, labels)
        result.update(calibration_intercept=intercept, calibration_slope=slope)
    return result, bins


def calibration(predictions):
    rows = [r for r in predictions if r["suite"] == "explore" and r["selected"] and r["candidates"] == 2]
    train = [r for r in rows if r["seed"] % 2]
    test = [r for r in rows if not r["seed"] % 2]
    train_seeds, test_seeds = {r["seed"] for r in train}, {r["seed"] for r in test}
    if len(train_seeds) != 10 or len(test_seeds) != 10 or train_seeds & test_seeds:
        raise ValueError("calibration needs 10 disjoint seeds in each partition")
    models = {
        "slack-only": lambda r: r["slack_us"],
        "rtt-only": lambda r: r["rtt_us"],
        "earliest-completion": lambda r: r["slack_us"] - r["estimate_us"],
        "calibrated-probability": lambda r: logit(r["probability"]),
    }
    scored, scores, reliability, comparisons = [], [], [], []
    fits = {"train_seeds": sorted(train_seeds), "test_seeds": sorted(test_seeds), "targets": {}}
    for target in ("deadline_met", "budget_met"):
        params = {model: fit_logistic([feature(r) for r in train], [r[target] for r in train])
                  for model, feature in models.items()}
        fits["targets"][target] = {key: dict(intercept=a, slope=b) for key, (a, b) in params.items()}
        groups = defaultdict(list)
        differences = defaultdict(list)
        for model in (*models, "current-probability"):
            pairs = [(r["probability"] if model == "current-probability" else
                      sigmoid(params[model][0] + params[model][1] * models[model](r)), r[target]) for r in train]
            result, _ = model_metrics(pairs)
            scores.append(dict(partition="train", target=target, model=model, dimension="overall", value="all",
                               **result))
        for row in test:
            y = row[target]
            probabilities = {model: sigmoid(params[model][0] + params[model][1] * feature(row))
                             for model, feature in models.items()}
            probabilities["current-probability"] = row["probability"]
            for model, p in probabilities.items():
                scored.append({**{k: row[k] for k in ("run", "seed", "record_id", "path_id", "scenario",
                                                        "load", "slack_us", "payload_len", "kind")},
                               "target": target, "model": model, "forecast": p, "outcome": y})
                for field, value in (
                    ("overall", "all"), ("path", row["path_id"]), ("scenario", row["scenario"]),
                    ("load", row["load"]), ("slack", row["slack_bin"]), ("payload", row["payload_bin"]),
                    ("kind", row["kind"]),
                ):
                    groups[model, field, value].append((p, y))
                if model != "slack-only":
                    change = (p - y)**2 - (probabilities["slack-only"] - y)**2
                    differences[model, "all", row["seed"]].append(change)
                    if row["slack_bin"] == "0–100ms":
                        differences[model, "0–100ms", row["seed"]].append(change)
        for (model, dimension, value), pairs in sorted(groups.items()):
            result, bins = model_metrics(pairs)
            meta = dict(partition="test", target=target, model=model, dimension=dimension, value=value)
            scores.append({**meta, **result})
            reliability.extend({**meta, **item} for item in bins)
        clustered = defaultdict(list)
        for (model, band, _), values in sorted(differences.items()):
            clustered[model, band].append(statistics.fmean(values))
        for (model, band), values in sorted(clustered.items()):
            comparisons.append(dict(target=target, model=model, band=band,
                                    **summarize(values, inferential=True), holm_p=None))
    holm(comparisons)
    return scored, scores, reliability, comparisons, fits
