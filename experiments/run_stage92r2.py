"""Run the frozen Stage 9.2R2 engineering design in isolated namespaces."""

import argparse
import concurrent.futures
import json
import os
import subprocess
import time
from pathlib import Path

from run_stage7 import ROOT, stamp, write_json
from run_stage92 import checksums, provenance, run_case
from run_stage92r import SEALED, engineering_guard, schedule as prior_schedule
from stage92_design import file_hash, load_design, validate_isolation, verify_seal
from stage92_probe import trial

SCENARIOS = ("bandwidth", "reversal", "loss")


def design():
    return json.loads((ROOT / "configs/stage92r2.json").read_text())


def schedule(suite, config, base):
    if suite in ("timing", "bandwidth", "loss"):
        return [{"name": f"{scenario}-{i:02d}", "scenario": scenario, "replicate": i, "split": "engineering"}
                for i in range(config["calibration_repeats"]) for scenario in SCENARIOS
                if suite in ("timing", scenario)]
    cases = prior_schedule(suite, config, base)
    for case in cases:
        case.update(stage92r2=True, name=f"r2-{case['name']}", seed=case["seed"] + 100_000,
                    assignment_seed=case["assignment_seed"] + 100_000, network_seed=case["network_seed"] + 100_000)
        if suite == "pilot":
            edge = case["phases"][0]["at_us"]
            phase_offsets = (-110_000, -22_000, 12_000, 43_000, 92_000, 151_000)
            case["release_times_us"] = sorted({165_000, 300_000, 445_000, 1_680_000, 1_875_000,
                                               2_060_000, 2_210_000, *(edge + t for t in phase_offsets)})
    return cases


def host_snapshot():
    return {"monotonic_ns": time.monotonic_ns(),
            "load": Path("/proc/loadavg").read_text().split()[:3],
            "cpu_jiffies": [int(value) for value in Path("/proc/stat").read_text().splitlines()[0].split()[1:]]}


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
        cases = [case for case in cases if case["replicate"] == 0] if suite in ("timing", "bandwidth", "loss") else cases[
            :3 if suite == "overhead" else 2]
    build = Path(os.environ.get("IOT_BUILD_DIR", ROOT / "build")).resolve()
    build_record = provenance(build, tc)
    out.mkdir(parents=True)
    for folder in ("inputs", "execution"):
        (out / folder).mkdir()
    manifest = {"suite": suite, "config": config, "quality": base["quality"], "build": build_record,
                "schedule": cases, "sealed_sha256": file_hash(SEALED), "smoke": smoke,
                "stop_rule": "Run every scheduled case once; retain failures; analyze only after all are recorded."}
    write_json(out / "manifest.json", manifest)

    def run(case):
        started = stamp()
        before = host_snapshot()
        try:
            if suite in ("timing", "bandwidth", "loss"):
                print(case["name"], flush=True)
                trial(out / "runs" / case["name"], case["scenario"], case["replicate"], str(tc),
                      refined=True, targeted=True, kernel_loss=suite == "loss")
                result = {"case": case["name"], "exit_code": 0, "start_utc": started, "end_utc": stamp()}
            else:
                result = run_case(out, case, build, tc)
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
            result = {"case": case["name"], "exit_code": 1, "error": str(error),
                      "start_utc": started, "end_utc": stamp()}
        return {**result, "host_before": before, "host_after": host_snapshot()}

    results = []
    workers = 3 if suite == "pilot" and not smoke else 1
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        for outcome in pool.map(run, cases):
            results.append(outcome)
            write_json(out / "execution.json", results)
    unchanged = build_record == provenance(build, tc) and manifest["sealed_sha256"] == file_hash(SEALED)
    write_json(out / "manifest.json", {**manifest, "source_and_build_unchanged": unchanged})
    write_json(out / "sha256.json", checksums(out))
    return unchanged and all(row["exit_code"] == 0 for row in results)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("suite", choices=("timing", "overhead", "pilot", "bandwidth", "loss"))
    parser.add_argument("directory", type=Path)
    parser.add_argument("--tc", type=Path, default=Path("/usr/sbin/tc"))
    parser.add_argument("--smoke", action="store_true")
    args = parser.parse_args()
    raise SystemExit(0 if acquire(args.suite, args.directory.resolve(), args.tc.resolve(), args.smoke) else 1)
