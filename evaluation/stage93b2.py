"""Diagnose short-deadline probabilities using archived development connections."""

import argparse
import json
import tempfile
from pathlib import Path

import numpy as np

from stage93a import ROOT, file_hash, write_json
from stage93b import dataset, replay_development, run as replay_b
from stage93b_model import fold_assignments, predictions
from stage93b2_diagnostics import (MODELS, availability, block_validation, calibration_results, error_attribution,
                                   historical_phases, missing_causes, phase_coverage)
from stage93b2_model import calibrate, fit_blocks, historical_oof
from stage93b2_pilot import build_plan


def recheck_stage_b(records, provenance, prior, archived, temporary):
    print("Independently reproducing Stage 9.3-B", flush=True)
    artifacts = replay_b(records, provenance, prior)
    hashes = {}
    directory = temporary / "stage93b-recomputed"
    directory.mkdir()
    for key, body in artifacts.items():
        name = f"{key}.json"
        replay = directory / name
        write_json(replay, body)
        if replay.read_bytes() != (archived / name).read_bytes():
            raise ValueError(f"Stage 9.3-B byte mismatch: {name}")
        hashes[name] = file_hash(replay)
    return artifacts, hashes


def generate(temporary, config, prior):
    results = ROOT / "results"
    records, provenance = replay_development(results, temporary)
    historical, hashes = recheck_stage_b(records, provenance, prior, results / "stage93b", temporary)
    data = dataset(records, prior)
    rows = [r for r in records if r["label"] in (0, 1) and 0 < r["effective_slack_us"] <= 100000]
    if not np.array_equal([r["label"] for r in rows], data.y) or not np.array_equal(
            [r["connection_id"] for r in rows], data.connection):
        raise ValueError("diagnostic rows and prediction rows differ")
    folds, _ = fold_assignments(data, prior)
    models = historical["model_coefficients"]["models"]
    p = predictions(data, models)
    oof = historical_oof(data, models, folds, MODELS)
    cal = calibrate(data, oof["M3"])
    phase_archive = json.loads((results / "stage93a/data_quality_summary.json").read_text())
    phases, metadata = historical_phases(temporary, phase_archive["runs"], rows, config)
    errors = error_attribution(data, rows, phases, oof, p, config, prior)
    for env in prior["validation_environments"]:
        expected = historical["model_comparison"]["environments"][env]["0-25ms"]["paired_own"]["M3-M1"]
        if errors["environments"][env]["paired_short_m3_minus_m1"] != expected:
            raise ValueError("short-budget same-record comparison changed")
    features, extension, matched = fit_blocks(data, rows, folds, oof["M3"], models["M3"]["fit"]["ridge"], config, prior)
    if not np.array_equal(p["M7"], extension["F_full_complete"], equal_nan=True):
        raise ValueError("complete-case Full Network State reference changed")
    # Only closed manifest identities are read by the development isolation guard.
    seal = json.loads((results / "stage92/sealed-manifest.json").read_text())
    plan = build_plan(data, p, prior, config["pilot"], phase_archive["runs"], seal, temporary)
    return {
        "error_attribution": {**errors, "stage93a_and_b_replay": {**provenance, "stage93b_byte_identical": hashes},
                              "gate": {"verdict": "NO-GO", "next_step": "Collect and verify the frozen development pilot",
                                       "reasons": ["no independently verified same-run transition timing",
                                                   "new development validation pending"]}},
        "calibration_diagnostics": calibration_results(data, rows, p, oof, cal, config, prior),
        "feature_availability": availability(data, rows, missing_causes(records), config, prior),
        "transition_phase_coverage": phase_coverage(data, rows, phases, metadata, prior),
        "feature_block_comparison": {"training_oof": features,
                                     "historical_diagnostics": block_validation(data, {**p, **extension}, matched, prior),
                                     "screen": {"min_oof_coverage": config["feature_min_cv_coverage"],
                                                "min_seed_equal_brier_reduction": config["feature_min_cv_brier_reduction"],
                                                "require_same_direction_in_both_training_environments": True},
                                     "validation_scope": "historical diagnostics only; cannot advance a candidate",
                                     "interaction_anchors_us": config["interaction_anchors_us"]},
        "development_pilot_plan": plan,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    output = args.output.resolve()
    archive = ROOT / "results"
    if output == archive or any(p.parent == archive and p.name not in ("stage93b2", "runs")
                                for p in (output, *output.parents)):
        parser.error("choose a new output directory outside the historical archive")
    config = json.loads((ROOT / "configs/stage93b2.json").read_text())
    prior = json.loads((ROOT / "configs/stage93b.json").read_text())
    with tempfile.TemporaryDirectory(prefix="stage93b2-") as temp:
        artifacts = generate(Path(temp), config, prior)
    output.mkdir(parents=True, exist_ok=True)
    for name, body in artifacts.items():
        write_json(output / f"{name}.json", body)


if __name__ == "__main__":
    main()
