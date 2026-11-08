"""Record-aligned error, calibration, and feature-coverage diagnostics."""

from collections import Counter, defaultdict

import numpy as np

from stage93a_statistics import distribution
from stage93b_metrics import common_rows, log_losses, metric_summary, paired
from stage93b_model import support
from stage93b2_model import BLOCKS, DYNAMIC, FULL, calibrated

MODELS = ("M1", "M3", "M4", "M5", "M6")
PHASES = ("STABLE", "PRE_SWITCH", "TRANSITION", "POST_STABLE", "OUTAGE", "RECOVERY", "UNKNOWN")


def phase_at(lower, upper, witness, pre_us):
    """Classify the entire selection bracket against independently verified phase bounds."""
    if witness is None:
        return "UNKNOWN"
    edge, stable, recovery, restored = witness
    if not lower <= upper or not edge < stable or ((recovery is None) != (restored is None)):
        raise ValueError("invalid verified phase bounds")
    if recovery is not None and not stable <= recovery < restored:
        raise ValueError("invalid outage/recovery bounds")

    def phase(t):
        if t < edge - pre_us:
            return "STABLE"
        if t < edge:
            return "PRE_SWITCH"
        if t < stable:
            return "TRANSITION"
        if recovery is not None:
            if t < recovery:
                return "OUTAGE"
            if t < restored:
                return "RECOVERY"
        return "POST_STABLE"

    first, last = phase(lower), phase(upper)
    return first if first == last else "UNKNOWN"


def historical_phases(temporary, audit_runs, rows, config):
    evidence = defaultdict(Counter)
    for run in audit_runs:
        if run["exit_code"]:
            continue
        folder = temporary / run["batch"] / "pilot/runs" / run["case"]["name"]
        files = {file.name for file in folder.iterdir() if file.is_file()}
        if files & {"client.json", "server.json", "kernel-events.log", "phase_witness.json"}:
            raise ValueError("historical run has new phase evidence requiring independent review")
        evidence[run["batch"]].update(runs=1, with_control_timeline=int("network.jsonl" in files),
                                     with_xquic_pcaps=int(sum(f.endswith(".pcap") for f in files) == 4),
                                     with_same_connection_verified_probe_witness=0)
    phases = [phase_at(row["choice_lower_us"], row["choice_upper_us"], None,
                       config["pilot"]["pre_switch_window_us"]) for row in rows]
    return phases, {"run_inventory": evidence,
                    "evidence_scope": "Stage 9.2R/R2 data-plane calibration used separate probe trials; no run/time join to XQUIC pilots",
                    "decision": "Stage 9.3-A configured phase is not a data-plane phase; every historical phase is UNKNOWN"}


def task_type(row, config):
    if row["kind"] == 1:
        return "state"
    return "urgent_event" if row["priority"] >= config["urgent_priority_minimum"] else "other_event"


def band(value, edges):
    if value is None:
        return "missing"
    return next((f"{lo}-{hi}" for lo, hi in zip(edges, edges[1:]) if lo < value <= hi),
                f">{edges[-1]}" if value > edges[-1] else str(value))


def sign(value):
    if value is None:
        return "missing"
    return "negative" if value < 0 else "positive" if value > 0 else "zero"


def error_summary(y, p, threshold):
    predicted = p >= threshold
    diff = p - y
    n = len(y)
    return {"n": n, "positive": int(y.sum()), "negative": n - int(y.sum()),
            "false_on_time": int(np.sum(predicted & (y == 0))), "false_late": int(np.sum(~predicted & (y == 1))),
            "mean_probability": float(p.mean()) if n else None,
            "mean_probability_minus_outcome": float(diff.mean()) if n else None,
            "brier_record_equal": float((diff**2).mean()) if n else None,
            "log_loss_record_equal": float(log_losses(p, y).mean()) if n else None}


def sliced(rows, row_ids, key):
    grouped = defaultdict(list)
    for i in row_ids:
        grouped[str(key(rows[i]))].append(i)
    return {name: np.array(ids) for name, ids in sorted(grouped.items())}


def example(row, p, i):
    return {"connection_id": row["connection_id"], "record_id": row["record_id"], "label": row["label"],
            "label_state": row["label_state"], "path": row["link"], "load": row["load"],
            "kind": row["kind"], "priority": row["priority"],
            **{k: row["features"][k] for k in ("slack_us", "rtt_us", "rtt_age_us", "srtt_rate_us_per_s")},
            "p": {model: float(p[model][i]) for model in MODELS}}


def error_attribution(data, rows, phases, train_oof, p, config, prior):
    threshold = config["diagnostic_threshold"]
    dimensions = {
        "seed": lambda r: r["seed"], "batch": lambda r: r["batch"], "path": lambda r: r["link"],
        "load": lambda r: r["load"], "task": lambda r: task_type(r, config), "priority": lambda r: r["priority"],
        "slack_us": lambda r: band(r["effective_slack_us"], config["short_budget_edges_us"]),
        "srtt_us": lambda r: band(r["features"]["rtt_us"], [0, 15000, 30000, 60000, 100000]),
        "rtt_age_us": lambda r: band(r["features"]["rtt_age_us"], [0, 5000, 20000, 50000]),
        "delivery_age_us": lambda r: band(r["features"]["delivery_sample_age_us"], [0, 5000, 20000, 50000]),
        "srtt_rate_sign": lambda r: sign(r["features"]["srtt_rate_us_per_s"]),
        "rttvar_change_sign": lambda r: sign(r["features"]["rttvar_change_us"]),
        **{k: lambda r, f=k: r["features"][f] for k in ("srtt_order_flip", "recent_loss_increment", "pto")}
    }
    result = {}
    for env in (*prior["train_environments"], *prior["validation_environments"]):
        training = env in prior["train_environments"]
        scores = train_oof if training else p
        all_rows = np.flatnonzero(data.env == env)
        short = all_rows[data.slack[all_rows] <= 25000]
        bins = {name: sliced(rows, short, key) for name, key in dimensions.items()}
        bins["verified_phase"] = sliced(phases, short, lambda phase: phase)
        by_model = {}
        for model in MODELS:
            estimate = scores[model]
            summary = {**error_summary(data.y[short], estimate[short], threshold), **support(data, short),
                       "residual_distribution": distribution((estimate[short] - data.y[short]).tolist())}
            y = data.y[short]
            by_model[model] = {"short": summary, "context_0-100ms": error_summary(data.y[all_rows], estimate[all_rows], threshold),
                               "short_strata": {name: {key: error_summary(data.y[sub], estimate[sub], threshold)
                                                       for key, sub in groups.items()} for name, groups in bins.items()},
                               "worst_false_on_time": [example(rows[i], scores, i) for i in sorted(
                                   short[(estimate[short] >= threshold) & (y == 0)], key=lambda i: -estimate[i])[:5]],
                               "worst_false_late": [example(rows[i], scores, i) for i in sorted(
                                   short[(estimate[short] < threshold) & (y == 1)], key=lambda i: estimate[i])[:5]]}
        result[env] = {"evaluation": "training OOF at historically selected ridge" if training else "historical diagnostic only",
                       "models": by_model,
                       "paired_short_m3_minus_m1": paired(data, scores, short, "M3", "M1", prior),
                       "paired_short_m3_minus_m1_by_rtt_age": {
                           key: paired(data, scores, sub, "M3", "M1", prior) for key, sub in bins["rtt_age_us"].items()}}
    return {"threshold": threshold, "urgent_priority_minimum": config["urgent_priority_minimum"], "environments": result}


def missing_causes(records):
    """Explain NaNs using prior snapshots only, following Stage 9.3-A's feature rules."""
    connections = defaultdict(list)
    for row in records:
        if row["candidates"]:
            connections[row["connection_id"]].append(row)
    result = {}
    for group in connections.values():
        previous, had_order = {}, False
        for row in sorted(group, key=lambda r: r["decision_attempt"]):
            rtts = {path["rtt_us"] for path in row["candidates"]}
            chosen = next(path for path in row["candidates"] if path["selected"])
            old = previous.get(chosen["path_id"])
            why = {}
            for name in FULL:
                if chosen["features"][name] is not None:
                    continue
                reason = "unobserved_scalar"
                if name == "rtt_age_us":
                    reason = "no_recorded_rtt_update"
                elif name == "delivery_sample_age_us":
                    reason = "no_recorded_delivery_sample"
                elif name in ("srtt_rate_us_per_s", "rttvar_change_us"):
                    reason = ("no_recorded_rtt_update" if chosen["rtt_age_us"] is None
                              else "fewer_than_two_distinct_rtt_updates")
                elif name in ("recent_loss_increment", "scheduled_growth_Bps"):
                    reason = ("no_preceding_path_snapshot" if old is None else "no_distinct_sample_interval"
                              if old["sampled_us"] == chosen["sampled_us"] else "counter_reset_or_unavailable_delta")
                elif name == "srtt_order_flip":
                    reason = ("second_candidate_absent" if len(row["candidates"]) < 2 else "tied_current_srtt"
                              if len(rtts) < 2 else "no_preceding_comparable_order")
                    if len(rtts) >= 2 and had_order:
                        raise ValueError("order flip missing despite current and previous order")
                why[name] = reason
            result[row["connection_id"], row["record_id"]] = why
            previous.update({path["path_id"]: path for path in row["candidates"]})
            had_order |= len(rtts) >= 2
    return result


def availability(data, rows, reasons, config, prior):
    def summarize(indices):
        masks = [reasons[rows[i]["connection_id"], rows[i]["record_id"]] for i in indices]
        complete = indices[np.array([not m for m in masks], dtype=bool)]
        absent = defaultdict(Counter)
        for mask in masks:
            for feature, cause in mask.items():
                absent[feature][cause] += 1
        return {"total": support(data, indices), "complete": support(data, complete),
                "missing_by_feature_and_cause": absent,
                "missing_patterns": dict(Counter(",".join(sorted(mask)) or "complete" for mask in masks))}

    out = {}
    for env in (*prior["train_environments"], *prior["validation_environments"]):
        result = {}
        for window, (lo, hi) in prior["budget_windows_us"].items():
            indices = np.flatnonzero((data.env == env) & (data.slack > lo) & (data.slack <= hi))
            result[window] = {**summarize(indices), "by_batch": {
                k: summarize(v) for k, v in sliced(rows, indices, lambda r: r["batch"]).items()},
                "by_path": {k: summarize(v) for k, v in sliced(rows, indices, lambda r: r["link"]).items()},
                "by_task": {k: summarize(v) for k, v in sliced(rows, indices, lambda r: task_type(r, config)).items()}}
        out[env] = result
    return {"environments": out, "features": FULL,
            "causality": "Only missingness visible on the same pre-decision feature snapshot is used in F."}


def phase_coverage(data, rows, phases, metadata, prior):
    out = {}
    phases = np.array(phases)
    features = ("rtt_us", "rttvar_us", *DYNAMIC, "pto")
    for env in (*prior["train_environments"], *prior["validation_environments"]):
        all_rows = np.flatnonzero(data.env == env)
        short = all_rows[data.slack[all_rows] <= 25000]
        out[env] = {"0-100ms": {phase: support(data, all_rows[phases[all_rows] == phase]) for phase in PHASES},
                    "0-25ms": {phase: support(data, short[phases[short] == phase]) for phase in PHASES},
                    "short_features_by_verified_phase": {
                        phase: {name: distribution(rows[i]["features"][name] for i in short if phases[i] == phase)
                                for name in features} for phase in PHASES}}
    return {**metadata, "environments": out}


def calibration_results(data, rows, historical, train_oof, fits, config, prior):
    results = {}
    for env in (*prior["train_environments"], *prior["validation_environments"]):
        training = env in prior["train_environments"]
        source = train_oof if training else historical
        p = {"M1": source["M1"], "M3": source["M3"],
             **{key: calibrated(source["M3"], fit) for key, fit in fits.items()}}
        by_window = {}
        for window, (lo, hi) in prior["budget_windows_us"].items():
            indices = np.flatnonzero((data.env == env) & (data.slack > lo) & (data.slack <= hi))
            by_window[window] = calibration_slice(data, p, indices, prior)
            if window == "0-25ms":
                urgent = np.array([i for i in indices if task_type(rows[i], config) == "urgent_event"], dtype=int)
                by_window["urgent_events_0-25ms"] = calibration_slice(data, p, urgent, prior)
        results[env] = {"scope": "apparent calibration fit on training OOF" if training else "historical diagnosis only",
                        "windows": by_window}
    return {"training_oof_calibrators": fits, "weighting": "environment-equal then seed-equal then record-equal within seed",
            "environments": results}


def calibration_slice(data, p, rows, prior):
    models = {}
    for name, forecast in p.items():
        metric, calibration = metric_summary(data, forecast[rows], rows, len(rows), prior)
        models[name] = {"metrics": metric, "calibration": calibration}
    return {"models": models, "paired_vs_m3": {name: paired(data, p, rows, name, "M3", prior)
                                              for name in p if name != "M3"},
            "paired_vs_m1": {name: paired(data, p, rows, name, "M1", prior)
                             for name in p if name != "M1"}}


def block_validation(data, p, matched, prior):
    result = {}
    full_models = ("M3", "F_full_complete", "F_full_median", "F_full_indicators")
    for env in prior["validation_environments"]:
        result[env] = {}
        for window in ("0-100ms", "0-25ms"):
            lo, hi = prior["budget_windows_us"][window]
            rows = np.flatnonzero((data.env == env) & (data.slack > lo) & (data.slack <= hi))
            shared = common_rows(p, full_models, rows)
            comparisons = {}
            for model, spec in BLOCKS.items():
                available = rows[np.isfinite(p[model][rows])]
                metric, _ = metric_summary(data, p[model][available], available, len(rows), prior)
                comparisons[model] = {"own": metric, "pairwise_common": paired(data, p, rows, model, spec.reference, prior),
                                      "vs_m3_same_rows": paired(data, p, rows, model, "M3", prior)}
                if model in matched:
                    comparisons[model]["vs_m3_with_matched_training"] = paired(
                        data, {model: p[model], "M3": matched[model]}, rows, model, "M3", prior)
            result[env][window] = {"comparisons": comparisons, "full_models_common": {
                model: {"metrics": metric_summary(data, p[model][shared], shared, len(rows), prior)[0],
                        "vs_m3": paired(data, p, shared, model, "M3", prior)} for model in full_models}}
    return result
