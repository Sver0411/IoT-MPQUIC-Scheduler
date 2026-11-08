"""Descriptive, reproducible statistics. No model fitting or row-level inference."""

from collections import Counter, defaultdict
from itertools import combinations, groupby
from math import floor
from statistics import correlation, fmean, stdev

from stage93a_features import COMBINATIONS, DICTIONARY

BINS = ("nonpositive", "0-25ms", "25-50ms", "50-100ms", ">100ms")
STAT_FEATURES = tuple(k for k, v in DICTIONARY.items() if v["available_at_decision"])
SIGNALS = ("slack_us", "rtt_us", "rttvar_us", "scheduled_bytes", "in_flight_bytes", "cwnd_bytes",
           "rate_Bps", "loss", "pto", "payload_bytes", "rtt_age_us", "recent_loss_increment",
           "srtt_rate_us_per_s", "scheduled_growth_Bps", "srtt_order_flip")


def budget_bin(slack):
    if slack <= 0:
        return BINS[0]
    for upper, name in zip((25_000, 50_000, 100_000), BINS[1:4]):
        if slack <= upper:
            return name
    return BINS[4]


def quantile(ordered, q):
    index = q * (len(ordered) - 1)
    low = floor(index)
    return ordered[low] + (index - low) * (ordered[min(low + 1, len(ordered) - 1)] - ordered[low])


def distribution(values):
    values = list(values)
    ordered = sorted(v for v in values if v is not None)
    n = len(ordered)
    out = {"total": len(values), "n": n, "missing": len(values) - n,
           "missing_rate": (len(values) - n) / len(values) if values else None,
           "mean": None, "sd": None, "median": None, "p5": None, "p25": None, "p75": None, "p95": None,
           "min": None, "max": None, "nonzero": sum(v != 0 for v in ordered),
           "outlier_fraction": None, "tukey_degenerate": None}
    if n:
        out.update(mean=fmean(ordered), sd=stdev(ordered) if n > 1 else None, min=ordered[0], max=ordered[-1])
        for key, q in (("p5", .05), ("p25", .25), ("median", .5), ("p75", .75), ("p95", .95)):
            out[key] = quantile(ordered, q)
        iqr = out["p75"] - out["p25"]
        out["tukey_degenerate"] = iqr == 0
        if iqr:
            lo, hi = out["p25"] - 1.5 * iqr, out["p75"] + 1.5 * iqr
            out["outlier_fraction"] = sum(x < lo or x > hi for x in ordered) / n
    return out


def split_by(rows, field):
    groups = defaultdict(list)
    for row in rows:
        groups[row[field]].append(row)
    return groups


def ks(left, right):
    a = [x for x in left if x is not None]
    b = [x for x in right if x is not None]
    if not a or not b:
        return None
    counts_a, counts_b = Counter(a), Counter(b)
    ca = cb = gap = 0
    for x in sorted(counts_a.keys() | counts_b.keys()):
        ca += counts_a[x]
        cb += counts_b[x]
        gap = max(gap, abs(ca / len(a) - cb / len(b)))
    return gap


def ranks(values):
    result = [0] * len(values)
    start = 0
    order = sorted(range(len(values)), key=values.__getitem__)
    for _, tied in groupby(order, key=values.__getitem__):
        group = list(tied)
        for index in group:
            result[index] = start + (len(group) + 1) / 2
        start += len(group)
    return result


def auc_parts(points):
    n_pos = sum(label for _, label in points)
    n_neg = len(points) - n_pos
    scores = ranks([score for score, _ in points])
    return sum(rank for rank, (_, label) in zip(scores, points) if label) - n_pos * (n_pos + 1) / 2, n_pos * n_neg


def association(rows, name):
    available = [r for r in rows if r["features"].get(name) is not None]
    points = [(r["features"][name], r["label"]) for r in available]
    pos = sum(r["label"] for r in available)
    neg = len(available) - pos
    seeds = len({r["seed"] for r in available})
    numerator, pairs = auc_parts(points)
    strata = defaultdict(list)
    for row, point in zip(available, points):
        base = row["features"]
        # A descriptive, matched rank comparison beyond coarse slack, RTT and path bins.
        strata[(row["link"], base["slack_us"] // 10_000, base["rtt_us"] // 10_000)].append(point)
    mixed = [group for group in strata.values() if {label for _, label in group} == {0, 1}]
    matched = [auc_parts(group) for group in mixed]
    matched_pairs = sum(p for _, p in matched)
    return {"n": len(available), "positive": pos, "negative": neg, "seed_clusters": seeds,
            "sparse": min(pos, neg) < 20 or seeds < 12, "rank_auc": numerator / pairs if pairs else None,
            "matched_rank_auc": sum(x for x, _ in matched) / matched_pairs if matched_pairs else None,
            "matched_pairs": matched_pairs, "mixed_cells": len(mixed), "matched_rows": sum(map(len, mixed))}


def correlations(rows):
    output = []
    for a, b in combinations(SIGNALS, 2):
        points = [(r["features"][a], r["features"][b]) for r in rows
                  if r["features"][a] is not None and r["features"][b] is not None]
        pearson = spearman = None
        if len(points) > 1:
            x, y = zip(*points)
            if len(set(x)) > 1 and len(set(y)) > 1:
                pearson = correlation(x, y)
                spearman = correlation(ranks(x), ranks(y))
        output.append(dict(a=a, b=b, n=len(points), pearson=pearson, spearman=spearman,
                           strong_redundancy=any(abs(r) >= .90 for r in (pearson, spearman) if r is not None)))
    return output


def coverage(rows):
    selected = [r for r in rows if "link" in r]
    valid = [r for r in selected if r["label"] is not None]
    short = [r for r in valid if r["budget_bin"] in BINS[1:4]]
    return {"raw_records": len(rows), "selected": len(selected), "valid": len(valid), "short_valid": len(short),
            "short_selected": sum(r["budget_bin"] in BINS[1:4] for r in selected),
            "generated_short_records": sum(0 < r["generated_budget_us"] <= 100_000 for r in rows),
            "short_dual_valid": sum(len(r["candidates"]) == 2 for r in short),
            "on_time": sum(r["label"] == 1 for r in valid),
            "late": sum(r["label_state"] == "late" for r in rows),
            "unfinished_at_window_with_deadline_elapsed": sum(r["label_state"] == "unfinished" for r in rows),
            "received_after_window": sum(r["status"] == "after-window" for r in rows),
            "no_observed_delivery_at_any_time": sum(r["received_us"] is None for r in selected),
            "no_pre_window_delivery": sum(r["received_us"] is None or r["received_us"] >= r["horizon_us"]
                                          for r in selected),
            "unknown_after_submission": sum(r["label_state"] == "unknown" for r in rows),
            "unsubmitted": sum(r["label_state"] == "unsubmitted" for r in rows),
            "unsubmitted_reasons": dict(Counter(r["status"] for r in rows if r["label_state"] == "unsubmitted")),
            "path_valid": {link: sum(r["link"] == link for r in valid) for link in ("A", "B")},
            "unobserved_candidate_paths": sum(not c["selected"] for r in rows for c in r["candidates"]),
            "connection_runs": len({r["connection_id"] for r in rows}),
            "seed_clusters": len({(r["environment"], r["seed"]) for r in rows}),
            "legacy_zero_now_unknown": sum(r["legacy_label"] == 0 and r["label"] is None for r in rows),
            "legacy_zero_now_positive": sum(r["legacy_label"] == 0 and r["label"] == 1 for r in rows),
            "stale_state_deliveries": sum(r["receiver_stale"] for r in rows)}


def support(rows):
    return {"n": len(rows), "positive": sum(r["label"] == 1 for r in rows),
            "negative": sum(r["label"] == 0 for r in rows), "seed_clusters": len({r["seed"] for r in rows}),
            "connections": len({r["connection_id"] for r in rows}),
            "path": {link: sum(r["link"] == link for r in rows) for link in ("A", "B")}}


def feature_distributions(rows):
    return {k: distribution(r["features"].get(k) for r in rows) for k in STAT_FEATURES}


def phase_coverage(rows):
    return {phase: support(group) for phase, group in split_by(rows, "phase").items()}


def selection_audit(rows):
    dual = [r for r in rows if len(r["candidates"]) == 2]
    chosen = [c for r in dual for c in r["candidates"] if c["selected"]]
    unchosen = [c for r in dual for c in r["candidates"] if not c["selected"]]
    path_counts = {}
    for name in BINS:
        group = [r for r in dual if r["budget_bin"] == name]
        path_counts[name] = {path: sum(r["link"] == path for r in group) for path in ("A", "B")}
    features = ("rtt_us", "rttvar_us", "in_flight_bytes", "loss", "pto")
    return {"two_candidates": len(dual), "single_candidate": sum(len(r["candidates"]) == 1 for r in rows),
            "dual_assigned_path": dict(Counter(c["link"] for c in chosen)), "dual_assigned_path_by_budget": path_counts,
            "empirical_A_share": sum(c["link"] == "A" for c in chosen) / len(chosen) if chosen else None,
            "nominal_p_values": sorted({c["assignment_probability"] for r in rows for c in r["candidates"]}),
            "chosen_vs_unchosen_ks_within_path": {
                path: {k: ks([c[k] for c in chosen if c["link"] == path], [c[k] for c in unchosen if c["link"] == path])
                       for k in features} for path in ("A", "B")},
            "record_admission_by_kind_and_load": {
                f"{load}/{kind}": {"generated": len(group), "selected": sum("link" in r for r in group)}
                for load, workload in split_by(rows, "load").items() for kind, group in split_by(workload, "kind").items()},
            "submitted_vs_unsubmitted_ks": {
                k: ks([r[k] for r in rows if "link" in r], [r[k] for r in rows if "link" not in r])
                for k in ("priority", "kind", "payload_bytes", "release_us")}}


def drift(left, right):
    result = {}
    for name in STAT_FEATURES:
        a = [r["features"].get(name) for r in left]
        b = [r["features"].get(name) for r in right]
        delta = ks(a, b)
        result[name] = dict(ks=delta, missing_rate_delta=(b.count(None) / len(b) - a.count(None) / len(a)) if a and b else None,
                            band=(None if delta is None else "small" if delta <= .10 else "mild" if delta <= .25 else "large"))
    return result


def analyze(records):
    for row in records:
        chosen = next((c for c in row["candidates"] if c["selected"]), None)
        if chosen is not None:
            row.update(link=chosen["link"], features=chosen["features"])
    envs = split_by(records, "environment")
    by_env = {}
    features = {}
    valid_groups = {}
    for env, rows in envs.items():
        valid = [r for r in rows if r["label"] is not None]
        valid_groups[env] = valid
        by_budget = {name: [r for r in valid if r["budget_bin"] == name] for name in BINS}
        short = [r for name in BINS[1:4] for r in by_budget[name]]
        by_env[env] = {"split": rows[0]["split"], "family": rows[0]["family"], "coverage": coverage(rows),
                       "by_batch": {k: coverage(v) for k, v in split_by(rows, "batch").items()},
                       "budget_bins": {k: {**support(v), "by_phase": phase_coverage(v)} for k, v in by_budget.items()},
                       "short_by_phase": phase_coverage(short), "selection": selection_audit(rows)}
        strata = {"all": valid, "short": short, **{f"budget/{k}": v for k, v in by_budget.items()}}
        for dimension in ("link", "phase", "load", "label", "batch", "budget", "kind"):
            strata.update({f"{dimension}/{k}": v for k, v in split_by(valid, dimension).items()})
        features[env] = {
            "split": rows[0]["split"], "family": rows[0]["family"],
            "distributions": {k: feature_distributions(v) for k, v in strata.items()},
            "associations": {k: {f: association(v, f) for f in SIGNALS} for k, v in
                             {"all": valid, "short": short, **{f"budget/{k}": v for k, v in by_budget.items()}}.items()},
            "correlations": {"all": correlations(valid), "short": correlations(short)},
            "combination_coverage": {k: {name: support([r for r in v if all(r["features"][f] is not None for f in combo)])
                                         for name, combo in COMBINATIONS.items()}
                                     for k, v in {"all": valid, "short": short, **by_budget}.items()}}
    selected = [r for r in records if "link" in r]
    samples = [c for r in selected for c in r["candidates"]]
    for c in samples:
        if c["rate_Bps"] != (c["cwnd_bytes"] * 1_000_000 // c["rtt_us"] if c["rtt_us"] else 0):
            raise ValueError("rate proxy disagrees with CUBIC window and SRTT")
    development = {env: {"split": rows[0]["split"], "label_support": support(valid_groups[env])}
                   for env, rows in envs.items()}
    drift_tables = {}
    for a in sorted(envs):
        if development[a]["split"] != "train":
            continue
        for b in sorted(envs):
            if development[b]["split"] == "validation":
                drift_tables[f"{a} -> {b}"] = {
                    "all": drift(valid_groups[a], valid_groups[b]),
                    "short": drift([r for r in valid_groups[a] if r["budget_bin"] in BINS[1:4]],
                                   [r for r in valid_groups[b] if r["budget_bin"] in BINS[1:4]])}
    return {
        "feature_dictionary": {"features": {
            k: {**entry, "observed_eligible_snapshot_missing": distribution(c["features"].get(k) for c in samples)["missing"],
                "observed_eligible_snapshots": len(samples)}
            for k, entry in DICTIONARY.items()}, "combinations": COMBINATIONS,
            "exclusions": ["actual send/link and repair", "receiver FIN", "delivery label", "application ACK",
                           "transport ACK for this record", "future configured or measured state", "identity and assignment probability"]},
        "data_quality_summary": {"total": coverage(records), "by_batch": {k: coverage(v) for k, v in split_by(records, "batch").items()},
                                 "by_split": {k: coverage(v) for k, v in split_by(records, "split").items()},
                                 "timeline": {
                                     "tick_to_selection_lower_us": distribution(r["choice_lower_us"] - r["decision_us"] for r in selected),
                                     "tick_to_selection_upper_us": distribution(r["choice_upper_us"] - r["decision_us"] for r in selected),
                                     "selection_bracket_width_us": distribution(r["choice_upper_us"] - r["choice_lower_us"] for r in selected),
                                     "budget_bin_sensitive_to_tick_reference": sum(r["bin_sensitive"] for r in selected),
                                     "budget_bin_uncertain_within_selection_bracket": sum(r["selection_bin_uncertain"] for r in selected),
                                     "wire_sends": sum(r["first_send_us"] is not None for r in selected),
                                     "first_wire_on_different_link": sum(r["first_send_link"] is not None and r["first_send_link"] != r["link"] for r in selected),
                                     "any_wire_on_different_link": sum(any(link != r["link"] for link in r["wire_links"]) for r in selected),
                                     "receiver_fins": sum(r["fin_us"] is not None for r in selected),
                                     "client_application_acks": sum(r["app_ack_us"] is not None for r in selected)},
                                 "rate_proxy_identity_verified_on_candidate_snapshots": len(samples)},
        "environment_coverage": {"environments": by_env, "budget_intervals": "<=0, (0,25], (25,50], (50,100], >100 ms",
                                 "independent_unit": "environment and connection seed; records and reused seeds are dependent"},
        "feature_distribution": {"environments": features, "development_environment_support": development,
                                 "train_to_validation_drift": drift_tables,
                                 "methods": {"distributions": "sample SD, linear quantiles, Tukey 1.5 IQR; null fence for zero IQR",
                                             "correlations": "pairwise complete Pearson and Spearman; high overlap at absolute r >= .90",
                                             "exact_collinearity": ["rate_Bps determined by cwnd_bytes and rtt_us (integer floor)",
                                                                    "legacy_queue_bytes = scheduled_bytes + in_flight_bytes",
                                                                    "send_budget_bytes determined by cwnd_bytes and in_flight_bytes",
                                                                    "rtt_to_slack determined by rtt_us and slack_us"],
                                             "association": "Descriptive tie-adjusted rank AUC (no fitted model); matched by path, 10ms slack and 10ms SRTT strata",
                                             "sparse_association": "<20 positive or negative observations, or <12 seeds",
                                             "drift": "Empirical two-sample KS and missingness delta (validation minus train); small <=.10, mild <=.25, large >.25. No iid inference."}},
    }
