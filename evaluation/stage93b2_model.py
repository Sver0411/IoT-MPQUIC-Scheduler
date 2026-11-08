"""Prespecified training-only calibration and dynamic feature diagnostics."""

from collections import Counter
from dataclasses import dataclass

import numpy as np
from scipy.optimize import minimize
from scipy.special import expit

from stage8_statistics import EPS
from stage93b_model import FEATURES, MONOTONE, OPTIMIZER_OPTIONS, cluster_means, fit_logistic, predict_logistic, support

M3 = FEATURES["M3"]
FULL = FEATURES["M7"]
DYNAMIC = ("rtt_age_us", "delivery_sample_age_us", "srtt_rate_us_per_s", "rttvar_change_us",
           "srtt_order_flip", "loss", "recent_loss_increment")


@dataclass(frozen=True)
class Spec:
    features: tuple
    reference: str = "M3"
    missing: str = "complete"


BLOCKS = {
    "E1_freshness": Spec(M3 + DYNAMIC[:2]),
    "E2_trend": Spec(M3 + DYNAMIC[2:4]),
    "E3_order": Spec(M3 + ("srtt_order_flip",)),
    "E4_reliability": Spec(M3 + ("loss", "recent_loss_increment")),
    "E5_task": Spec(M3 + ("payload_bytes", "priority", "kind")),
    "E6_slack_srtt": Spec(("slack_srtt_low", "slack_srtt_high", "rtt_us")),
    "E6_age_additive": Spec(M3 + ("rtt_age_us",)),
    "E6_slack_age": Spec(("slack_age_low", "slack_age_high", "rtt_us", "rtt_age_us"), "E6_age_additive"),
    "E6_rate_additive": Spec(M3 + ("srtt_rate_us_per_s",)),
    "E6_srtt_rate": Spec(M3 + ("srtt_rate_us_per_s", "srtt_rate_product"), "E6_rate_additive"),
    "F_full_complete": Spec(FULL),
    "F_full_median": Spec(FULL, "F_full_complete", "median"),
    "F_full_indicators": Spec(FULL, "F_full_complete", "median+indicators"),
}


def oof(data, matrix, available, folds, fit_predict):
    if (folds[data.train] < 0).any() or (folds[data.validation] != -1).any():
        raise ValueError("OOF requires disjoint training folds and untouched validation")
    train = data.train[available[data.train]]
    p = np.full(len(data.y), np.nan)
    for fold in np.unique(folds[data.train]):
        held = train[folds[train] == fold]
        if len(held):
            fit = train[folds[train] != fold]
            p[held] = fit_predict(matrix[fit], data.y[fit], matrix[held])
    if not np.isfinite(p[train]).all() or np.isfinite(p[data.validation]).any():
        raise ValueError("OOF prediction coverage or partition error")
    return p


def historical_oof(data, models, folds, names):
    p = {}
    for name in names:
        if name == "M1":
            p[name] = np.full(len(data.y), np.nan)
            p[name][data.train] = data.x[name][data.train, 0]
            continue
        ridge = models[name]["fit"]["ridge"]
        bounds = [i for i, f in enumerate(FEATURES[name]) if f in MONOTONE]
        p[name] = oof(data, data.x[name], data.available[name], folds,
                      lambda x, y, held: predict_logistic(fit_logistic(x, y, ridge, bounds), held))
    return p


def seed_weights(data, rows):
    groups = Counter(data.group[rows])
    envs = Counter(data.env_by_group[group] for group in groups)
    return np.array([1 / (len(envs) * envs[data.env[i]] * groups[data.group[i]]) for i in rows])


def logit_probability(p):
    p = np.clip(p, EPS, 1 - EPS)
    return np.log(p / (1 - p))


def calibrate(data, p):
    if np.isfinite(p[data.validation]).any():
        raise ValueError("calibration input must contain training OOF probabilities only")
    rows = data.train[np.isfinite(p[data.train])]
    z, y, weight = logit_probability(p[rows]), data.y[rows], seed_weights(data, rows)
    if set(y) != {0, 1}:
        raise ValueError("calibration needs both classes in training")

    def objective(theta):
        logits = theta[0] + theta[1] * z
        residual = weight * (expit(logits) - y)
        loss = weight @ (np.logaddexp(0, logits) - y * logits)
        return loss, np.array([residual.sum(), residual @ z])

    result = {}
    for name, slope in (("intercept_only", (1, 1)), ("platt", (0, None))):
        fitted = minimize(objective, [0, 1], jac=True, bounds=[(None, None), slope],
                          method="L-BFGS-B", options=OPTIMIZER_OPTIONS)
        if not fitted.success or not np.isfinite(fitted.x).all():
            raise RuntimeError(f"OOF calibration failed: {fitted.message}")
        result[name] = {"intercept": float(fitted.x[0]), "slope": float(fitted.x[1]), "training": support(data, rows)}
    return result


def calibrated(p, fit):
    return expit(fit["intercept"] + fit["slope"] * logit_probability(p))


def feature_columns(rows, config):
    """Build only from the audited decision-time snapshots, using fixed interaction anchors."""
    columns = {feature: np.array([row["features"][feature] for row in rows], dtype=float)
               for feature in set(M3 + FULL + DYNAMIC + ("payload_bytes", "priority", "kind"))}
    slack = columns["slack_us"]
    for name, feature in (("srtt", "rtt_us"), ("age", "rtt_age_us")):
        anchor = config["interaction_anchors_us"]["srtt" if name == "srtt" else "rtt_age"]
        weight = np.clip(columns[feature] / anchor, 0, 1)
        columns[f"slack_{name}_low"] = slack * (1 - weight)
        columns[f"slack_{name}_high"] = slack * weight
    columns["srtt_rate_product"] = columns["rtt_us"] * columns["srtt_rate_us_per_s"]
    return columns


def impute(x, medians, indicators):
    missing = ~np.isfinite(x)
    filled = np.where(missing, medians, x)
    return np.column_stack((filled, missing.astype(float))) if indicators else filled


def block_fit(x, y, spec, ridge):
    median = None
    if spec.missing != "complete":
        # A training fold with no observation for a column uses zero and retains the missing mask.
        median = np.array([np.median(v[np.isfinite(v)]) if np.isfinite(v).any() else 0 for v in x.T])
        x = impute(x, median, spec.missing == "median+indicators")
    constrained = [i for i, name in enumerate(spec.features) if name.startswith("slack_")]
    return {"fit": fit_logistic(x, y, ridge, constrained), "medians": None if median is None else median.tolist()}


def block_predict(fit, x, spec):
    if fit["medians"] is not None:
        x = impute(x, fit["medians"], spec.missing == "median+indicators")
    return predict_logistic(fit["fit"], x)


def cv_score(data, p, rows, environments):
    return {env: float(cluster_means((p[sub] - data.y[sub])**2, data.group[sub])[1].mean()) if len(sub) else None
            for env in environments for sub in [rows[data.env[rows] == env]]}


def fit_blocks(data, rows, folds, base_oof, base_ridge, config, prior):
    columns = feature_columns(rows, config)
    development_oof, final, results, matched, controls = {"M3": base_oof}, {}, {}, {}, {}
    for name, spec in BLOCKS.items():
        print(f"Diagnosing {name}", flush=True)
        x = np.column_stack([columns[key] for key in spec.features])
        available = np.isfinite(x).all(axis=1) if spec.missing == "complete" else np.ones(len(x), dtype=bool)
        used = data.train[available[data.train]]
        tuning = []
        for ridge in prior["ridge_candidates"]:
            p = oof(data, x, available, folds,
                    lambda a, y, b: block_predict(block_fit(a, y, spec, ridge), b, spec))
            scores = cv_score(data, p, used, prior["train_environments"])
            tuning.append((float(np.mean(list(scores.values()))), ridge, scores, p))
        score, ridge, by_env, p = min(tuning, key=lambda item: (round(item[0], 12), -item[1]))
        reference = development_oof[spec.reference]
        paired_rows = used[np.isfinite(reference[used])]
        left = cv_score(data, p, paired_rows, prior["train_environments"])
        right = cv_score(data, reference, paired_rows, prior["train_environments"])
        deltas = {env: left[env] - right[env] for env in left}
        coverage = len(used) / len(data.train)
        passing = (coverage >= config["feature_min_cv_coverage"] and all(v < 0 for v in deltas.values())
                   and np.mean(list(deltas.values())) <= -config["feature_min_cv_brier_reduction"])
        fitted = block_fit(x[used], data.y[used], spec, ridge)
        final[name] = np.full(len(x), np.nan)
        final[name][available] = block_predict(fitted, x[available], spec)
        development_oof[name] = p
        results[name] = {"features": spec.features, "missing": spec.missing, "reference": spec.reference,
                         "training": support(data, used), "training_coverage": coverage, "selected_ridge": ridge,
                         "cv_brier": score, "cv_brier_by_environment": by_env,
                         "cv_paired_delta_by_environment": deltas,
                         "cv_paired_support": {env: support(data, paired_rows[data.env[paired_rows] == env])
                                               for env in prior["train_environments"]},
                         "cv_screen_pass": bool(passing), "fit": fitted,
                         "cv_grid": [{"ridge": strength, "brier": loss, "by_environment": scores}
                                     for loss, strength, scores, _ in tuning]}
        if spec.reference == "M3" and len(used) != len(data.train):
            # Diagnostic control for complete-case selection; keep the prespecified screen unchanged.
            key = available[data.train].tobytes()
            if key not in controls:
                matrix = data.x["M3"]
                mask = available.copy()
                predict = lambda a, y, b: predict_logistic(fit_logistic(a, y, base_ridge, (0,)), b)
                cv_reference = oof(data, matrix, mask, folds, predict)
                fitted_reference = predict(matrix[used], data.y[used], matrix)
                controls[key] = cv_reference, fitted_reference
            cv_reference, matched[name] = controls[key]
            control_scores = cv_score(data, cv_reference, paired_rows, prior["train_environments"])
            results[name]["cv_delta_vs_m3_with_matched_training"] = {env: left[env] - control_scores[env] for env in left}
    return results, final, matched
