"""Paired development diagnostics with whole-connection-seed resampling."""

from functools import lru_cache

import numpy as np

from stage7_statistics import score
from stage8_statistics import EPS
from stage93b_model import cluster_means, fit_logistic, support


def supported(counts, config):
    return (min(counts["positive"], counts["negative"]) >= config["support"]["per_class_rows"] and
            counts["seed_clusters"] >= config["support"]["seed_clusters"])


def log_losses(p, y):
    return -np.log(np.clip(np.where(y == 1, p, 1 - p), EPS, 1 - EPS))


def average_precision(p, y):
    order = np.argsort(-p, kind="stable")
    p, y = p[order], y[order]
    ends = np.r_[np.flatnonzero(p[1:] != p[:-1]), len(p) - 1]
    positives = y.cumsum()[ends]
    return float((np.diff(positives, prepend=0) * positives / (ends + 1)).sum() / positives[-1])


@lru_cache(maxsize=128)
def bootstrap_indices(n, draws, seed):
    return np.random.default_rng(seed).integers(n, size=(draws, n))


def mean_interval(seed_means, config):
    indices = bootstrap_indices(len(seed_means), config["validation_bootstrap_draws"],
                                config["validation_bootstrap_seed"])
    return np.quantile(seed_means[indices].mean(axis=1), [.025, .975]).tolist()


def loss_summary(values, groups, interval, config):
    _, means = cluster_means(values, groups)
    return {"record_equal": float(values.mean()) if len(values) else None,
            "seed_equal": float(means.mean()) if len(means) else None,
            "seed_bootstrap_ci95": mean_interval(means, config) if interval else None}


def calibration_fit(p, y, enough):
    out = {"intercept": None, "slope": None, "status": "insufficient class or seed support"}
    if not enough:
        return out
    clipped = np.clip(p, EPS, 1 - EPS)
    logits = np.log(clipped / (1 - clipped))
    if np.ptp(logits) == 0:
        return {**out, "status": "constant forecast"}
    a, b = logits[y == 0], logits[y == 1]
    if a.max() <= b.min() or b.max() <= a.min():
        return {**out, "status": "complete or quasi separation"}
    try:
        intercept, slope = fit_logistic(logits[:, None], y, 0)["raw"]
    except RuntimeError as error:
        return {**out, "status": str(error)}
    return {"intercept": intercept, "slope": slope, "status": "ok"}


def metric_summary(data, p, rows, population, config):
    counts = support(data, rows)
    enough = supported(counts, config)
    y = data.y[rows]
    metrics, bins = score(list(zip(p, y))) if len(rows) else ({"roc_auc": None, "ece": None}, [])
    summary = {**counts, "coverage": len(rows) / population if population else None,
               "positive_fraction": counts["positive"] / len(rows) if len(rows) else None,
               "negative_fraction": counts["negative"] / len(rows) if len(rows) else None,
               "supported": enough, "brier": loss_summary((p - y)**2, data.group[rows], enough, config),
               "log_loss": loss_summary(log_losses(p, y), data.group[rows], enough, config),
               "roc_auc": float(metrics["roc_auc"]) if enough else None,
               "pr_auc_average_precision": average_precision(p, y) if enough else None}
    by_bin = {round(row["bin_lower"] * 10): row for row in bins}
    slots = []
    for i in range(10):
        row = by_bin.get(i)
        slots.append(row or {"bin_lower": i / 10, "bin_upper": (i + 1) / 10, "n": 0,
                             "predicted": None, "observed": None})
    cal = {**counts, **calibration_fit(p, y, enough), "ece_record_equal": metrics["ece"],
           "reliability": slots}
    return summary, cal


def paired(data, p, rows, candidate, reference, config):
    # Both errors are computed on the very same observed-path row.
    left, right = p[candidate][rows], p[reference][rows]
    rows = rows[np.isfinite(left) & np.isfinite(right)]
    counts = support(data, rows)
    y = data.y[rows]
    diff = (p[candidate][rows] - y)**2 - (p[reference][rows] - y)**2
    groups, means = cluster_means(diff, data.group[rows])
    enough = supported(counts, config)
    result = {**counts, "supported": enough,
              "brier_difference": loss_summary(diff, data.group[rows], enough, config),
              "seed_directions": {"better": int(np.sum(means < -1e-12)), "worse": int(np.sum(means > 1e-12)),
                                  "tied": int(np.sum(np.abs(means) <= 1e-12))},
              "leave_one_seed_out": None}
    if len(means) > 1:
        drop = (means.sum() - means) / (len(means) - 1)
        minimum, maximum = drop.argmin(), drop.argmax()
        result["leave_one_seed_out"] = {"min": float(drop[minimum]), "max": float(drop[maximum]),
                                        "min_omitted_seed": int(data.seed_by_group[groups[minimum]]),
                                        "max_omitted_seed": int(data.seed_by_group[groups[maximum]])}
    return result


def common_rows(p, models, rows):
    return rows[np.logical_and.reduce([np.isfinite(p[model][rows]) for model in models])]
