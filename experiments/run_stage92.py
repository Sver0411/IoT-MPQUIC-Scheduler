"""Acquire Stage 9.2 development evidence. The sealed partition cannot run here."""

import argparse
import json
import os
import random
import subprocess
import sys
from pathlib import Path

from run_stage7 import ROOT, stamp, write_json
from stage92_design import ARMS, file_hash, freeze, load_design, matrix, overhead, verify_seal, write_workload

sys.path.insert(0, str(ROOT / "network/scenarios"))
from stage92_probe import SCENARIOS, trial


def git(root, *args):
    return subprocess.check_output(["git", "-c", f"safe.directory={root}", "-C", root, *args], text=True).strip()


def provenance(build, tc):
    scopes = ("src/**/*.[ch]", "scripts/*.sh", "scripts/capture_usage.py", "patches/xquic/*.patch",
              "configs/stage92*.json",
              "network/**/*.py", "network/**/*.sh", "experiments/*.py", "evaluation/*.py", "evaluation/oracle/*.py")
    return {"base_commit": git(ROOT, "rev-parse", "HEAD"),
            "binary_sha256": file_hash(build / "app/iot-mpquic"),
            "xquic_commit": git(ROOT / "third_party/xquic", "rev-parse", "HEAD"),
            "boringssl_commit": git(build / "boringssl", "rev-parse", "HEAD"),
            "tc_sha256": file_hash(tc),
            "sources": {path.relative_to(ROOT).as_posix(): file_hash(path)
                        for pattern in scopes for path in sorted(ROOT.glob(pattern))}}


def assert_development(case):
    if case.get("split") not in ("train", "validation", "engineering"):
        raise ValueError("sealed test is closed")


def run_case(folder, case, build, tc):
    assert_development(case)
    name = case["name"]
    inputs = folder / "inputs"
    workload = inputs / f"{name}.bin"
    write_workload(workload, case)
    case = {**case, "workload_sha256": file_hash(workload)}
    config = inputs / f"{name}.json"
    write_json(config, case)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("IOT_STAGE", "IOT_TUNING"))}
    env.update(IOT_BUILD_DIR=str(build), IOT_NETEM_TC=str(tc), IOT_SEED=str(case["seed"]),
               IOT_PATH_POLICY=case["path_scheduler"], IOT_STAGE8_SEED=str(case["assignment_seed"]),
               IOT_HORIZON_US=str(case["horizon_us"]), IOT_STAGE92_PROFILE="1", PYTHONDONTWRITEBYTECODE="1")
    for feature, key in (("logs", "IOT_STAGE91_TRACE"), ("digest", "IOT_STAGE92_DIGEST"),
                         ("capture", "IOT_STAGE92_CAPTURE")):
        if feature in ARMS[case["trace"]]:
            env[key] = "1"
    if case["features"]:
        env["IOT_STAGE92_FEATURES"] = "1"
    if case.get("stage92r"):
        env["IOT_STAGE92R"] = "1"
        if case.get("trace_arm") == "quiet":
            env["IOT_STAGE92R_QUIET"] = "1"
    out = folder / "runs" / name
    started = stamp()
    print(name, flush=True)
    with (folder / "execution" / f"{name}.log").open("w") as log:
        result = subprocess.run(["bash", ROOT / "scripts/run-lab.sh", "92", out, case["policy"], workload,
                                 str(case["recovery_k"]), config, case["ablation"]],
                                env=env, stdout=log, stderr=subprocess.STDOUT)
    return {"case": name, "exit_code": result.returncode, "start_utc": started, "end_utc": stamp()}


def checksums(folder):
    return {path.relative_to(folder).as_posix(): file_hash(path)
            for path in sorted(folder.rglob("*")) if path.is_file() and path.name != "sha256.json"}


def acquire(args):
    config = load_design()
    cases = matrix(config)
    build = Path(os.environ.get("IOT_BUILD_DIR", ROOT / "build")).resolve()
    tc = args.tc.resolve()
    design_build = provenance(build, tc)
    if args.suite == "seal":
        seal = freeze(config, cases, design_build)
        verify_seal(seal)
        with args.directory.open("x") as file:
            json.dump(seal, file, indent=2)
            file.write("\n")
        return True
    if os.geteuid() != 0:
        raise ValueError("network namespaces require root")
    folder = args.directory.resolve()
    folder.mkdir(parents=True)
    outcomes = []
    if args.suite == "timing":
        for replicate in range(config["calibration_repeats"]):
            for scenario in SCENARIOS:
                name = f"{scenario}-{replicate}"
                print(name, flush=True)
                started = stamp()
                status = {"case": name, "scenario": scenario, "replicate": replicate}
                try:
                    trial(folder / "runs" / name, scenario, replicate, str(tc))
                    status["exit_code"] = 0
                except (OSError, ValueError, subprocess.SubprocessError, RuntimeError) as error:
                    status.update(exit_code=1, error=str(error))
                outcomes.append({**status, "start_utc": started, "end_utc": stamp()})
                write_json(folder / "execution.json", outcomes)
        schedule = []
    else:
        for subdirectory in ("inputs", "execution"):
            (folder / subdirectory).mkdir()
        if args.suite == "pilot":
            # Early and late each appear once per load/budget combination. Replicas use independent seeds.
            schedule = [case for case in cases if case["split"] in ("train", "validation") and
                        ((case["timing"] == "early") == ((case["seed"] // 2 + (case["load"] == "high") +
                                                          (case["budget"] == "short")) % 2 == 0))]
            random.Random(config["design_seed"]).shuffle(schedule)
        else:
            schedule = list(overhead(config))
        for case in schedule:
            outcomes.append(run_case(folder, case, build, tc))
            write_json(folder / "execution.json", outcomes)
    unchanged = design_build == provenance(build, tc)
    write_json(folder / "manifest.json", {"suite": args.suite, "config": config, "build": design_build,
                                          "schedule": schedule, "source_and_build_unchanged": unchanged})
    write_json(folder / "sha256.json", checksums(folder))
    return unchanged and all(outcome["exit_code"] == 0 for outcome in outcomes)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("suite", choices=("timing", "overhead", "pilot", "seal"))
    parser.add_argument("directory", type=Path)
    parser.add_argument("--tc", type=Path, default=Path("/usr/sbin/tc"))
    raise SystemExit(0 if acquire(parser.parse_args()) else 1)
