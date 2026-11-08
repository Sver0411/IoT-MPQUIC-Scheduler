"""Freeze a development-only pilot design without starting any runs."""

import json
from itertools import product
from math import ceil
from random import Random
from statistics import stdev

import numpy as np

from stage93a import ROOT, check_partitions, file_hash
from stage93b import window_rows
from stage93b_model import cluster_means
from stage92_design import case_config, fingerprint, load_design, write_workload

ENVIRONMENTS = (
    {"environment": "b2-train-a-reversal", "family": "b2-a-to-b-delay-crossing", "split": "train", "mechanism": "reversal",
     "initial": {"a": [4, 25, 0], "b": [17, 25, 0]}, "after": {"a": [21, 25, 0], "b": [6, 25, 0]}, "side": "both"},
    {"environment": "b2-train-a-outage", "family": "b2-client-a-brief-blackhole", "split": "train", "mechanism": "outage",
     "initial": {"a": [5, 25, 0], "b": [14, 25, 0]}, "after": {"a": [5, 25, 100]}, "side": "client", "restore_us": 85000},
    {"environment": "b2-validation-b-reversal", "family": "b2-b-to-a-jitter-crossing", "split": "validation", "mechanism": "reversal",
     "initial": {"a": [18, 27, 0, 1], "b": [5, 21, 0, 1]}, "after": {"a": [7, 27, 0, 1], "b": [24, 21, 0, 1]}, "side": "both"},
    {"environment": "b2-validation-b-outage", "family": "b2-server-b-blackhole-recovery", "split": "validation", "mechanism": "outage",
     "initial": {"a": [11, 26, 0], "b": [4, 19, 0]}, "after": {"b": [4, 19, 100]}, "side": "server", "restore_us": 115000},
)


def validate_schedule(cases, historical_runs, seal):
    if not cases or any(c["split"] not in ("train", "validation") for c in cases):
        raise ValueError("pilot schedule must be development-only")
    check_partitions(cases, seal)
    history = [row["case"] for row in historical_runs]
    reserved = {key: {r[key] for r in history if key in r}
                for key in ("name", "seed", "network_seed", "assignment_seed", "network_sha256", "workload_sha256")}
    seen = {key: {} for key in (*reserved, "connection_id", "trajectory_sha256")}
    for row in cases:
        for key, identities in seen.items():
            value = row[key]
            reused = value in identities and (key != "network_sha256" or identities[value] != row["split"])
            if value in reserved.get(key, ()) or reused:
                raise ValueError(f"pilot reused {key}")
            identities[value] = row["split"]
    if {row["split"] for row in cases} != {"train", "validation"}:
        raise ValueError("pilot requires both development partitions")


def seed_plan(data, predictions, prior, config):
    planning = {}
    for env in prior["validation_environments"]:
        rows = window_rows(data, env, (0, 25_000))
        y = data.y[rows]
        diff = (predictions["M3"][rows] - y)**2 - (predictions["M1"][rows] - y)**2
        _, means = cluster_means(diff, data.group[rows])
        sd = stdev(means.tolist())
        planning[env] = {"seeds": len(means), "seed_delta_sd": sd,
                         "required_usable_seeds": ceil((1.96 * sd / config["target_brier_halfwidth"])**2)}
    cells = len(config["loads"]) * len(config["switch_times_us"])
    repeats = ceil(max(config["min_scheduled_seeds_per_environment"],
                       max(v["required_usable_seeds"] for v in planning.values()) / config["usable_seed_fraction"]) / cells)
    return planning, repeats


def build_plan(data, predictions, prior, config, historical_runs, seal, temporary):
    planning, repeats = seed_plan(data, predictions, prior, config)
    base = load_design()
    prior_coverage = json.loads((ROOT / "configs/stage92r2.json").read_text())
    cases = []
    payload = temporary / "pilot-workload.bin"
    for env, load, target, repeat in product(ENVIRONMENTS, config["loads"], config["switch_times_us"], range(repeats)):
        seed = config["seed_base"] + len(cases)
        # Sub-ms offsets are fixed by the case index; actual phase classification requires data-plane evidence.
        at = target + (repeat * 173) % 1000
        phases = [{"at_us": at, "side": env["side"], "links": env["after"]}]
        if "restore_us" in env:
            phases.append({"at_us": at + env["restore_us"], "side": env["side"],
                           "links": {link: env["initial"][link] for link in env["after"]}})
        profile = {"initial": env["initial"], "phases": phases}
        case = case_config(base, stage92r=True, stage92r2=True, name=f"b2-{seed}", connection_id=f"b2-conn-{seed}",
                           environment=env["environment"], family=env["family"], split=env["split"],
                           load=load, timing=str(target), budget="short", trace="full", features=True,
                           seed=seed, assignment_seed=seed + 100_000, network_seed=seed + 200_000,
                           path_scheduler="random-path", network_sha256=fingerprint(profile), **profile)
        offsets = (-45000, -25000, -12000, -5000, 0, 4000, 9000, 16000, 24000, 35000)
        case["release_times_us"] = sorted({160000, 230000, 310000, 450000, 1880000, 2020000, 2200000,
                                           *(phase["at_us"] + t for phase in phases for t in offsets)})
        case["deadline_pattern_us"] = [12000, 18000, 24000, 32000]
        write_workload(payload, case)
        case["workload_sha256"] = file_hash(payload)
        payload.unlink()
        cases.append(case)
    # Network identity hashes represent profiles; repeated profiles within a split are intentional.
    # A separate per-run trajectory fingerprint also binds the network RNG seed.
    for case in cases:
        case["trajectory_sha256"] = fingerprint([case["network_sha256"], case["network_seed"]])
    validate_schedule(cases, historical_runs, seal)
    Random(config["order_seed"]).shuffle(cases)
    return {"status": "frozen design; collection and development validation pending", "schedule_sha256": fingerprint(cases),
            "cases": cases, "planning": planning, "scheduled_per_environment": repeats * len(config["loads"]) * len(config["switch_times_us"]),
            "runs_executed": 0, "sealed_runs_executed": 0, "settings": config,
            "time_quality": {k: base["quality"][k] for k in ("max_edge_error_us", "max_direction_skew_us")},
            "inherited_environment_coverage": {k: prior_coverage[k] for k in
                                               ("min_short_rows", "min_rows_per_path", "min_negative_labels", "min_seeds")},
            "planning_endpoint": "0-25 ms same-record M3 minus M1 seed-equal Brier; planning approximation only",
            "acceptance": "For each applicable verified phase require both class counts and seed counts. "
                          "Apply the UNKNOWN limit to observations targeted near switches/recovery, including failed evidence.",
            "phase_evidence": {
                "clock": "same connection and Linux monotonic epoch; separately validate probe/pcap clock mapping",
                "reversal": "reuse dual-direction probe delay and ranking bounds; fixed rate per link; verify continuous new state",
                "outage": "independent probe sends, matching kernel drops and receives; verify outage and restoration in the affected direction",
                "phase_rule": "last old to stable new is transition; verified stable outage ends at recovery lower bound; recovery through stable confirmation",
                "missing": "UNKNOWN for every record in a run with absent or unreliable required switch/recovery evidence",
                "scope": "phase labels are offline-only; verify on the same XQUIC runs, not separate calibration repetitions",
                "instrumentation": "integrate per-run probes in both directions and independently check probe/trace perturbation before collection"
            },
            "preflight": "Freeze real XQUIC build and verifier fingerprints; verify simultaneous probes under both load levels "
                         "and recheck existing 5 ms error/skew and ±5% Goodput limits before acquisition.",
            "post_collection": "Validate actual trajectory SHA-256, workload SHA-256 and connection identity by partition; retain failed/UNKNOWN runs.",
            "stop_rule": "Attempt each frozen seed once in manifest order; retain failures and stop after the fixed schedule. Any shortfall remains a failure of coverage."}
