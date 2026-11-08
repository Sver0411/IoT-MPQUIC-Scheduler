"""Fit and evaluate Stage 9.3-B models on the audited development pilots only."""

import argparse
import hashlib
import json
import platform
import tempfile
from collections import Counter, defaultdict
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import scipy

from stage93a import ROOT, analyze, audit, file_hash, write_json
from stage93b_metrics import (common_rows, log_losses, loss_summary, metric_summary, paired, supported)
from stage93b_model import (FEATURES, NAMES, OPTIMIZER_OPTIONS, PRIMARY, coefficient_bootstrap, fold_assignments,
                            predictions, support, train_models)

BATCHES = ("stage92", "stage92r", "stage92r2")
COMPACT = ("M3", "M4", "M5", "M6")
ABLATIONS = (("M3", "M0"), ("M4", "M3"), ("M5", "M3"), ("M6", "M3"), ("M7", "M3"),
             ("M3", "D0"), ("M3", "D1"), ("D0", "M0"), ("D1", "M0"))


def replay_development(results, temporary):
    rows, evidence = audit(results, temporary)
    computed = analyze(rows)
    computed["data_quality_summary"].update(evidence)
    hashes = {}
    for name, content in computed.items():
        original = results / "stage93a" / f"{name}.json"
        replay = temporary / original.name
        write_json(replay, content)
        if original.read_bytes() != replay.read_bytes():
            raise ValueError(f"Stage 9.3-A output differs from its immutable archive: {name}")
        hashes[original.name] = file_hash(replay)
    provenance = {"stage93a_byte_identical": hashes,
                  "development_pilots": {batch: {"archive_sha256": item["archive_sha256"],
                                                 "endpoint_binary_sha256": item["build"]["binary_sha256"],
                                                 "verified_runs": item["runs_verified"]}
                                         for batch, item in evidence["archives"].items()},
                  "sealed_access": {"configuration_count": evidence["isolation"]["sealed_configurations"],
                                    "executions": evidence["isolation"]["sealed_configurations_executed_by_this_analysis"],
                                    "payload_reads": evidence["isolation"]["sealed_payloads_read"]}}
    return rows, provenance


def dataset(records, config):
    split_by_env = {env: split for split in ("train", "validation") for env in config[f"{split}_environments"]}
    counts = defaultdict(Counter)
    features, meta, labels = [], [], []
    identities, split_by_seed = set(), {}
    fingerprint = hashlib.sha256()
    for row in records:
        env, split, batch, seed = (row[k] for k in ("environment", "split", "batch", "seed"))
        if env not in split_by_env or split != split_by_env[env] or batch not in BATCHES:
            raise ValueError("input is outside the designated development populations")
        if split_by_seed.setdefault(seed, split) != split:
            raise ValueError("connection seed reused across training and validation")
        identity = row["connection_id"], row["record_id"]
        if identity in identities:
            raise ValueError("duplicate observation")
        identities.add(identity)
        counts[env]["generated"] += 1
        picked = [c for c in row["candidates"] if c["selected"]]
        if any(c["effective_label"] is not None or c["absolute_label"] is not None or c["received_us"] is not None
               for c in row["candidates"] if not c["selected"]):
            raise ValueError("unobserved candidate contains an outcome")
        if not picked:
            if row["label"] is not None or row["candidates"]:
                raise ValueError("supervision without an actual path")
            counts[env]["unsubmitted"] += 1
            continue
        if len(picked) != 1 or picked[0]["effective_label"] != row["label"]:
            raise ValueError("selected path and row label differ")
        counts[env]["submitted"] += 1
        slack = row["effective_slack_us"]
        y = row["label"]
        if y is None:
            counts[env]["unknown"] += 1
            continue
        if y not in (0, 1):
            raise ValueError("invalid label")
        if slack <= 0:
            counts[env]["nonpositive_budget"] += 1
            counts[env]["nonpositive_on_time"] += y
            continue
        if slack > 100_000:
            counts[env]["above_100ms"] += 1
            continue
        path, = picked
        raw = path["features"]
        if raw["slack_us"] != slack:
            raise ValueError("forecast and target use different slack")
        values = {**raw, "earliest_completion_margin_us": slack - path["estimate_us"],
                  "slack_minus_srtt_us": slack - raw["rtt_us"]}
        model_features = {key: [values[name] for name in names] for key, names in FEATURES.items()}
        row_meta = env, split, batch, seed, row["connection_id"], row["record_id"]
        fingerprint.update((json.dumps([row_meta, y, model_features], sort_keys=True, allow_nan=False) + "\n").encode())
        counts[env]["positive_budget_labeled"] += 1
        features.append(model_features)
        meta.append(row_meta)
        labels.append(y)
    if set(counts) != split_by_env.keys():
        raise ValueError("required development environments missing")
    group_keys = sorted({(env, seed) for env, _, _, seed, *_ in meta})
    group_ids = {key: i for i, key in enumerate(group_keys)}
    x = {key: np.asarray([row[key] for row in features], dtype=float) for key in FEATURES}
    return SimpleNamespace(x=x, available={key: np.isfinite(values).all(axis=1) for key, values in x.items()},
                           y=np.asarray(labels), slack=x["M0"][:, 0], srtt=x["D0"][:, 0],
                           group=np.array([group_ids[env, seed] for env, _, _, seed, *_ in meta]),
                           env=np.array([row[0] for row in meta]), batch=np.array([row[2] for row in meta]),
                           connection=np.array([row[4] for row in meta]),
                           train=np.flatnonzero([row[1] == "train" for row in meta]),
                           validation=np.flatnonzero([row[1] == "validation" for row in meta]),
                           env_by_group=[env for env, _ in group_keys], seed_by_group=[seed for _, seed in group_keys],
                           population=counts, row_sha256=fingerprint.hexdigest())


def window_rows(data, env, limits):
    lo, hi = limits
    return data.validation[(data.env[data.validation] == env) & (data.slack[data.validation] > lo) &
                           (data.slack[data.validation] <= hi)]


def contrasts(data, p, rows, pairs, config):
    return {f"{a}-{b}": paired(data, p, rows, a, b, config) for a, b in pairs}


def joint_grid(data, p, rows, config):
    grid = []
    slack_edges, srtt_edges = config["slack_grid_edges_us"], config["srtt_grid_edges_us"]
    for lo, hi in zip(slack_edges, slack_edges[1:]):
        for low_rtt, high_rtt in zip(srtt_edges, srtt_edges[1:]):
            group = rows[(data.slack[rows] > lo) & (data.slack[rows] <= hi) & (data.srtt[rows] > low_rtt) &
                         (data.srtt[rows] <= (high_rtt if high_rtt is not None else np.inf))]
            counts = support(data, group)
            grid.append({"slack_us": [lo, hi], "srtt_us": [low_rtt, high_rtt], **counts,
                         "supported": supported(counts, config),
                         "observed": float(data.y[group].mean()) if len(group) else None,
                         "m3_probability": float(p["M3"][group].mean()) if len(group) else None})
    return grid


def validation_results(data, p, config):
    comparisons, calibration, ablation = {}, {}, {}
    baseline_pairs = [(model, base) for model in PRIMARY for base in ("M0", "M1") if model != base]
    for env in config["validation_environments"]:
        comparisons[env], calibration[env], ablation[env] = {}, {}, {}
        for budget, limits in config["budget_windows_us"].items():
            rows = window_rows(data, env, limits)
            shared = common_rows(p, PRIMARY, rows)
            comparison = {"population": support(data, rows), "common_complete": support(data, shared)}
            cal = {}
            for key, subset in (("own", rows), ("common", shared)):
                comparison[key], cal[key] = {}, {}
                for model in FEATURES:
                    used = subset[np.isfinite(p[model][subset])]
                    comparison[key][model], cal[key][model] = metric_summary(
                        data, p[model][used], used, len(rows), config)
                comparison[f"paired_{key}"] = contrasts(data, p, subset, baseline_pairs, config)
            comparisons[env][budget] = comparison
            calibration[env][budget] = cal
            omitted = rows[~np.isin(rows, shared)]
            ablation[env][budget] = {"paired_available": contrasts(data, p, rows, ABLATIONS, config),
                                     "paired_common": contrasts(data, p, shared, ABLATIONS, config),
                                     "common_support": support(data, shared),
                                     "excluded_by_common_support": support(data, omitted)}
        ablation[env]["slack_srtt_grid"] = joint_grid(data, p, window_rows(data, env, (0, 100_000)), config)
    return comparisons, calibration, ablation


def batch_scores(data, p, full, rows, config):
    summary = {}
    for model in PRIMARY:
        used = rows[np.isfinite(p[model][rows])]
        y = data.y[used]
        counts = support(data, used)
        brier = (p[model][used] - y)**2
        summary[model] = {**counts, "brier": loss_summary(brier, data.group[used], False, config),
                          "log_loss": loss_summary(log_losses(p[model][used], y), data.group[used], False, config),
                          "refit_change_on_same_rows": loss_summary(
                              brier - (full[model][used] - y)**2, data.group[used], False, config)["seed_equal"]}
    pairs = [(m, b) for m in COMPACT + ("M7",) for b in ("M0", "M1", "M3") if m != b]
    return {"population": support(data, rows), "models": summary, "paired": contrasts(data, p, rows, pairs, config)}


def batch_results(data, full, folds, config):
    result = {}
    for batch in BATCHES:
        print(f"Refitting with {batch} excluded from training", flush=True)
        fit_rows = data.train[data.batch[data.train] != batch]
        fitted, cv = train_models(data, fit_rows, folds, config)
        refit = predictions(data, fitted)
        views = {}
        for env in config["validation_environments"]:
            views[env] = {}
            for window in ("0-100ms", "0-25ms"):
                rows = window_rows(data, env, config["budget_windows_us"][window])
                in_batch = data.batch[rows] == batch
                views[env][window] = {
                    "training_excluded": batch_scores(data, refit, full, rows, config),
                    "validation_only": batch_scores(data, full, full, rows[in_batch], config),
                    "validation_excluded": batch_scores(data, full, full, rows[~in_batch], config),
                    "jointly_excluded": batch_scores(data, refit, full, rows[~in_batch], config)}
        result[batch] = {"refit": fitted, "refit_cv": cv, "validation": views}
    return result


def decision(comparisons, batches, config):
    """Development progression gate, separate from any confirmatory test."""
    candidates = {}
    for model in COMPACT:
        checks = {}
        for env in config["validation_environments"]:
            for base in ("M0", "M1"):
                pair = f"{model}-{base}"
                main = comparisons[env]["0-100ms"]["paired_own"][pair]["brier_difference"]
                short = comparisons[env]["0-25ms"]["paired_own"][pair]["brier_difference"]
                checks[f"{env}/{base}/0-100ms"] = (main["seed_bootstrap_ci95"] is not None and
                    main["seed_equal"] <= -config["go_minimum_brier_reduction"] and main["seed_bootstrap_ci95"][1] < 0)
                checks[f"{env}/{base}/0-25ms"] = (short["seed_bootstrap_ci95"] is not None and short["seed_equal"] < 0)
            for batch, result in batches.items():
                diff = result["validation"][env]["0-100ms"]["training_excluded"]["paired"][f"{model}-M0"]
                checks[f"{env}/without_training_{batch}/M0"] = diff["supported"] and diff["brier_difference"]["seed_equal"] < 0
        candidates[model] = {"passes": all(checks.values()), "checks": checks}
    candidate = next((name for name in COMPACT if candidates[name]["passes"]), None)
    return {"verdict": "GO" if candidate else "NO-GO", "candidate": candidate, "checks": candidates,
            "selection_order": list(COMPACT), "scope": "development-set progression only"}


def run(records, provenance, config):
    data = dataset(records, config)
    folds, assignments = fold_assignments(data, config)
    models, cv = train_models(data, data.train, folds, config)
    p = predictions(data, models)
    comparisons, calibration, ablation = validation_results(data, p, config)
    batches = batch_results(data, p, folds, config)
    verdict = decision(comparisons, batches, config)
    print("Bootstrapping training coefficients", flush=True)
    coefficients = {key: coefficient_bootstrap(data, key, model, config) if model["fit"] else None
                    for key, model in models.items()}
    return {
        "model_comparison": {"environments": comparisons, "population": data.population,
                             "primary_metric": "seed-equal Brier; candidate minus reference; negative is better",
                             "models": NAMES, "development_gate": verdict},
        "feature_ablation": {"environments": ablation, "negative_difference_favors": "first model",
                             "joint_grid_intervals": "left open, right closed; null upper is unbounded"},
        "calibration": {"environments": calibration,
                        "calibration": "unpenalized joint logistic recalibration of observed y on prediction logits",
                        "clipping": "1e-6 for logits and log loss only; original probabilities for Brier and ranking",
                        "reliability_bins": "10 equal-width probability bins; final bin includes 1"},
        "cross_validation": {"config": config, "provenance": provenance, "model_row_sha256": data.row_sha256,
                             "versions": {"python": platform.python_version(), "numpy": np.__version__,
                                          "scipy": scipy.__version__},
                             "tuning": "environment-equal, seed-equal out-of-fold Brier; ties to stronger ridge",
                             "fold_groups": [{"environment": data.env_by_group[group], "seed": data.seed_by_group[group],
                                              "held_out_fold": fold} for group, fold in sorted(assignments.items())],
                             "models": cv},
        "batch_sensitivity": {"endpoint_fingerprints": provenance["development_pilots"], "exclusions": batches},
        "model_coefficients": {"models": models, "bootstrap": coefficients, "optimizer": OPTIMIZER_OPTIONS,
                               "model_row_sha256": data.row_sha256,
                               "bootstrap_design": "whole training seeds within environment; refit scaler at fixed ridge",
                               "derived_features": {"earliest_completion_margin_us": "slack_us minus archived estimate_us",
                                                    "slack_minus_srtt_us": "slack_us minus rtt_us"}},
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    output = args.output.resolve()
    results = ROOT / "results"
    if output == results or any(parent.parent == results and parent.name != "stage93b" and parent.name != "runs"
                                for parent in (output, *output.parents)):
        parser.error("choose a new output directory outside historical results")
    config = json.loads((ROOT / "configs/stage93b.json").read_text())
    with tempfile.TemporaryDirectory(prefix="stage93b-") as temporary:
        records, provenance = replay_development(results, Path(temporary))
    outcomes = run(records, provenance, config)
    output.mkdir(parents=True, exist_ok=True)
    for name, content in outcomes.items():
        write_json(output / f"{name}.json", content)
    print(f"Development verdict: {outcomes['model_comparison']['development_gate']['verdict']}", flush=True)


if __name__ == "__main__":
    main()
