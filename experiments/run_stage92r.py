"""Acquire a fixed Stage 9.2R engineering schedule. No sealed cases can run."""

import argparse
import concurrent.futures
import itertools
import json
import os
import random
import subprocess
import sys
from pathlib import Path

from run_stage7 import ROOT, stamp, write_json
from run_stage92 import assert_development, checksums, provenance, run_case
from stage92_design import case_config, file_hash, fingerprint, load_design, network, validate_isolation, verify_seal
from stage92_probe import SCENARIOS, trial

sys.path.insert(0, str(ROOT / "evaluation"))
from oracle.path_scheduler_oracle import assignment

SEALED = ROOT / "results/stage92/sealed-manifest.json"
QUEUE_RELEASES = [180_000, 350_000, 515_000, 715_000, 738_000, 770_000, 810_000, 856_000,
                  1_040_000, 1_250_000, 1_480_000, 1_690_000, 1_920_000, 2_140_000]


def design():
    return json.loads((ROOT / "configs/stage92r.json").read_text())


def engineering_guard(case, sealed):
    assert_development(case)
    if any(case.get(field) == other[field] for other in sealed["cases"] for field in
           ("name", "environment", "family", "seed", "assignment_seed", "network_seed", "network_sha256")):
        raise ValueError("sealed test identity is reserved")


def schedule(suite, config, base):
    rng = random.Random(config["design_seed"])
    cases = []
    if suite == "timing":
        cases = [{"name": f"{scenario}-{i}", "scenario": scenario, "replicate": i, "split": "engineering",
                  "environment": f"calibration-{scenario}"}
                 for i in range(config["calibration_repeats"]) for scenario in SCENARIOS]
    if suite == "overhead":
        for block in range(config["overhead_blocks"]):
            arms = ["off", "full", "quiet"]
            rng.shuffle(arms)
            for arm in arms:
                cases.append(case_config(base, name=f"trace-{block:02d}-{arm}", block=block, split="engineering",
                                         environment="trace-cost", seed=962_000 + block, assignment_seed=972_000 + block,
                                         network_seed=982_000 + block, initial={"a": [5, 24, 0], "b": [11, 18, 0]},
                                         phases=[], path_scheduler="proposed-path", load="high" if block % 2 else "low",
                                         budget="short", trace="off" if arm == "off" else "full", trace_arm=arm,
                                         features=arm != "off"))
    if suite in ("queue", "queue-active"):
        for i, (duration, packets, replicate) in enumerate(itertools.product(config["queue_durations_us"],
                config["queue_packets_per_tick"], range(config["queue_repeats"]))):
            profile = {"initial": {"a": [4, 5, 0], "b": [14, 20, 0]}, "phases": [],
                       "queue_burst": {"side": "client", "link": "a", "at_us": 705_000,
                                       "duration_us": duration, "packets": packets, "interval_us": 5000, "bytes": 1200}}
            cases.append(case_config(base, name=f"queue-{duration}-{packets}-{replicate}", split="engineering",
                                     environment="queue-perturbation", seed=963_000 + i, assignment_seed=973_000 + i,
                                     network_seed=983_000 + i, load="low", budget="short", timing="burst",
                                     path_scheduler="random-path", trace="full", features=True,
                                     network_sha256=fingerprint(profile), **profile))
            if suite == "queue-active":
                # Target A then B across the two fixtures with the existing random-path function.
                # The fourth release follows only three warm-up records, leaving initial stream credit.
                seed = 986_000 + i * 100
                while assignment(seed, 4, 35_000, 240, 2) != replicate:
                    seed += 1
                cases[-1].update(name=f"queue-active-{duration}-{packets}-{replicate}",
                                 seed=966_000 + i, assignment_seed=seed, network_seed=996_000 + i,
                                 release_times_us=QUEUE_RELEASES, expected_burst_link="AB"[replicate])
    if suite == "pilot":
        environments = [e for e in base["environments"] if e["split"] in ("train", "validation")]
        cells = list(itertools.product(("low", "high"), ("early", "late"), range(config["pilot_repeats_per_cell"])))
        for e, environment in enumerate(environments):
            for i, (load, timing, replicate) in enumerate(cells):
                seed = 964_000 + e * 100 + i
                profile = network(environment, timing)
                cases.append(case_config(base, name=f"{environment['id']}--{load}--{timing}--{replicate}",
                                         environment=environment["id"], family=environment["family"],
                                         split=environment["split"], timing=timing, load=load, budget="short", seed=seed,
                                         deadline_pattern_us=config["deadline_pattern_us"],
                                         assignment_seed=seed + 10_000, network_seed=seed + 20_000,
                                         network_sha256=fingerprint(profile), path_scheduler="random-path",
                                         trace="full", features=True, **profile))
        rng.shuffle(cases)
    return [{**case, "stage92r": True} for case in cases]


def acquire(suite, out, tc, smoke=False):
    if os.geteuid() != 0:
        raise ValueError("network namespaces require root")
    config, base = design(), load_design()
    sealed = json.loads(SEALED.read_text())
    verify_seal(sealed)
    cases = schedule(suite, config, base)
    if suite == "pilot":
        validate_isolation([*cases, *sealed["cases"]])
    for case in cases:
        engineering_guard(case, sealed)
    if smoke:
        cases = cases[:3 if suite == "overhead" else 1]
    build = Path(os.environ.get("IOT_BUILD_DIR", ROOT / "build")).resolve()
    build_record = provenance(build, tc)
    out.mkdir(parents=True)
    for folder in ("inputs", "execution"):
        (out / folder).mkdir()
    manifest = {"suite": suite, "config": config, "quality": base["quality"], "build": build_record,
                "schedule": cases, "sealed_sha256": file_hash(SEALED), "smoke": smoke}
    write_json(out / "manifest.json", manifest)

    def run(case):
        started = stamp()
        try:
            if suite != "timing":
                return run_case(out, case, build, tc)
            print(case["name"], flush=True)
            trial(out / "runs" / case["name"], case["scenario"], case["replicate"], str(tc), refined=True)
            code, error = 0, None
        except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as failure:
            code, error = 1, str(failure)
        return {"case": case["name"], "exit_code": code, "error": error,
                "start_utc": started, "end_utc": stamp()}

    outcomes = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=2 if suite == "pilot" and not smoke else 1) as workers:
        for outcome in workers.map(run, cases):
            outcomes.append(outcome)
            write_json(out / "execution.json", outcomes)
    unchanged = build_record == provenance(build, tc) and manifest["sealed_sha256"] == file_hash(SEALED)
    write_json(out / "manifest.json", {**manifest, "source_and_build_unchanged": unchanged})
    write_json(out / "sha256.json", checksums(out))
    return unchanged and all(item["exit_code"] == 0 for item in outcomes)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("suite", choices=("timing", "overhead", "queue", "queue-active", "pilot"))
    parser.add_argument("directory", type=Path)
    parser.add_argument("--tc", type=Path, default=Path("/usr/sbin/tc"))
    parser.add_argument("--smoke", action="store_true")
    args = parser.parse_args()
    raise SystemExit(0 if acquire(args.suite, args.directory.resolve(), args.tc.resolve(), args.smoke) else 1)
