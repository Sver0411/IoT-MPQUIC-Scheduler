"""Development-set ridge logistic models and group-level training."""

from collections import defaultdict
from random import Random

import numpy as np
from scipy.optimize import minimize
from scipy.special import expit

from stage93a_features import COMBINATIONS

FEATURES = {
    "M0": ("slack_us",),
    "M1": ("current_probability",),
    "M2": ("earliest_completion_margin_us",),
    "M3": COMBINATIONS["slack_rtt"],
    "M4": COMBINATIONS["slack_rtt_rttvar"],
    "M5": COMBINATIONS["slack_rtt_in_flight"],
    "M6": COMBINATIONS["slack_rtt_cwnd"],
    "M7": COMBINATIONS["full_network_state"],
    "D0": ("rtt_us",),
    "D1": ("slack_minus_srtt_us",),
}
PRIMARY = tuple(k for k in FEATURES if k.startswith("M"))
MONOTONE = {"slack_us", "earliest_completion_margin_us", "slack_minus_srtt_us"}
NAMES = dict(M0="Slack-only", M1="Current Probability", M2="Earliest Completion",
             M3="Slack + SRTT", M4="Slack + SRTT + RTTVAR", M5="Slack + SRTT + InFlight",
             M6="Slack + SRTT + CWND", M7="Full Network State", D0="SRTT-only", D1="Slack minus SRTT")
OPTIMIZER_OPTIONS = {"maxiter": 500, "ftol": 1e-13, "gtol": 1e-8, "maxls": 40}


def fit_logistic(x, y, ridge, nonnegative=()):
    """Minimize mean logistic loss plus ridge/2 times squared standardized slopes."""
    if not len(y) or not 0 < np.sum(y) < len(y):
        raise ValueError("fit requires both outcome classes")
    center = x.mean(axis=0)
    deviation = x.std(axis=0)
    scale = np.where(deviation > 0, deviation, 1)
    z = (x - center) / scale

    def objective(theta):
        intercept, slopes = theta[0], theta[1:]
        logits = intercept + z @ slopes
        residual = expit(logits) - y
        loss = np.mean(np.logaddexp(0, logits) - y * logits) + ridge / 2 * (slopes @ slopes)
        gradient = np.r_[residual.mean(), z.T @ residual / len(y) + ridge * slopes]
        return loss, gradient

    prevalence = y.mean()
    start = np.r_[np.log(prevalence / (1 - prevalence)), np.zeros(x.shape[1])]
    bounds = [(None, None)] + [(0, None) if i in nonnegative else (None, None) for i in range(x.shape[1])]
    result = minimize(objective, start, jac=True, bounds=bounds, method="L-BFGS-B", options=OPTIMIZER_OPTIONS)
    if not result.success or not np.isfinite(result.x).all():
        raise RuntimeError(f"logistic fit failed: {result.message}")
    slopes = result.x[1:] / scale
    return {"center": center.tolist(), "scale": scale.tolist(), "standardized": result.x.tolist(),
            "raw": np.r_[result.x[0] - center @ slopes, slopes].tolist(), "ridge": ridge}


def predict_logistic(fit, x):
    intercept, *slopes = fit["standardized"]
    return expit(intercept + ((x - fit["center"]) / fit["scale"]) @ slopes)


def cluster_means(values, groups):
    labels, inverse, counts = np.unique(groups, return_inverse=True, return_counts=True)
    return labels, np.bincount(inverse, weights=values) / counts


def fold_assignments(data, config):
    """Round-robin shuffled seed groups within each training environment and batch."""
    batch_by_group = {}
    for i in data.train:
        key = data.group[i]
        if batch_by_group.setdefault(key, data.batch[i]) != data.batch[i]:
            raise ValueError("seed group spans acquisition batches")
    strata = defaultdict(list)
    for group, batch in batch_by_group.items():
        strata[data.env_by_group[group], batch].append(int(group))
    rng = Random(config["cv_seed"])
    fold_by_group = {}
    for offset, (_, groups) in enumerate(sorted(strata.items())):
        groups.sort()
        rng.shuffle(groups)
        for i, group in enumerate(groups):
            fold_by_group[group] = (offset + i) % config["cv_folds"]
    return np.array([fold_by_group.get(group, -1) for group in data.group]), fold_by_group


def support(data, rows):
    positive = int(data.y[rows].sum())
    return {"n": len(rows), "positive": positive, "negative": len(rows) - positive,
            "seed_clusters": len(set(data.group[rows])), "connections": len(set(data.connection[rows]))}


def model_fit(data, model, rows, ridge):
    return fit_logistic(data.x[model][rows], data.y[rows], ridge,
                        [i for i, name in enumerate(FEATURES[model]) if name in MONOTONE])


def train_models(data, rows, folds, config):
    if not np.isin(rows, data.train).all() or (folds[rows] < 0).any():
        raise ValueError("training requires development training rows with assigned folds")
    models, cv = {}, {}
    for name, features in FEATURES.items():
        eligible = rows[data.available[name][rows]]
        info = {"name": NAMES[name], "features": features, "training_coverage": support(data, eligible),
                "missing": "complete cases per model; no imputation", "class_weights": "none"}
        if name == "M1":
            models[name] = {**info, "training": None, "fit": None,
                            "method": "fixed logged probability; no fit or recalibration"}
            continue
        candidates = []
        for ridge in config["ridge_candidates"]:
            held_out = np.full(len(eligible), np.nan)
            fit_support = []
            for fold in range(config["cv_folds"]):
                mask = folds[eligible] == fold
                validation, train = eligible[mask], eligible[~mask]
                fit = model_fit(data, name, train, ridge)
                p = predict_logistic(fit, data.x[name][validation])
                held_out[mask] = p
                fit_support.append({"fold": fold, "fit": support(data, train), "held_out": support(data, validation),
                                    "brier": float(np.mean((p - data.y[validation])**2)) if len(p) else None})
            if not np.isfinite(held_out).all():
                raise ValueError("training CV did not score every eligible training row")
            errors = (held_out - data.y[eligible])**2
            by_env = {}
            for env in config["train_environments"]:
                mask = data.env[eligible] == env
                _, seed_means = cluster_means(errors[mask], data.group[eligible][mask])
                by_env[env] = float(seed_means.mean())
            candidates.append({"ridge": ridge, "seed_equal_brier_by_environment": by_env,
                               "environment_equal_seed_equal_brier": float(np.mean(list(by_env.values()))),
                               "folds": fit_support})
        chosen = min(candidates, key=lambda r: (round(r["environment_equal_seed_equal_brier"], 12), -r["ridge"]))
        cv[name] = {"selected_ridge": chosen["ridge"], "candidates": candidates}
        models[name] = {**info, "training": support(data, eligible),
                        "fit": model_fit(data, name, eligible, chosen["ridge"]),
                        "method": "L2 logistic regression; training-only population SD; unpenalized intercept",
                        "nonnegative_features": [k for k in features if k in MONOTONE]}
    return models, cv


def predictions(data, models):
    results = {}
    for model, details in models.items():
        available = data.available[model]
        p = np.full(len(data.y), np.nan)
        x = data.x[model][available]
        p[available] = predict_logistic(details["fit"], x) if details["fit"] is not None else x[:, 0]
        results[model] = p
    return results


def coefficient_bootstrap(data, name, model, config):
    """Refit preprocessing and coefficients at the CV-selected ridge, resampling whole training seeds."""
    rows = data.train[data.available[name][data.train]]
    env_rows = []
    for env in config["train_environments"]:
        subset = rows[data.env[rows] == env]
        env_rows.append([subset[data.group[subset] == group] for group in np.unique(data.group[subset])])
    rng = np.random.default_rng(config["coefficient_bootstrap_seed"])
    draws = config["coefficient_bootstrap_draws"]
    results, failures = [], []
    for draw in range(draws):
        sampled = np.concatenate([groups[i] for groups in env_rows
                                  for i in rng.integers(len(groups), size=len(groups))])
        try:
            results.append(model_fit(data, name, sampled, model["fit"]["ridge"]))
        except (ValueError, RuntimeError) as error:
            failures.append({"draw": draw, "reason": str(error)})
    estimates = {}
    for scale in ("raw", "standardized"):
        values = np.array([fit[scale] for fit in results])
        terms = ("intercept", *FEATURES[name])
        estimates[scale] = {
            term: {"point": model["fit"][scale][i],
                   "ci95": np.quantile(values[:, i], [.025, .975]).tolist() if len(values) > 1 else None,
                   "negative_fraction": float(np.mean(values[:, i] < -1e-10)) if len(values) else None,
                   "positive_fraction": float(np.mean(values[:, i] > 1e-10)) if len(values) else None}
            for i, term in enumerate(terms)}
    return {"draws": draws, "successful": len(results), "failures": failures, "terms": estimates}
