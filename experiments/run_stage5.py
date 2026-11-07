"""Run the fixed Stage 5 matrix in isolated Linux network namespaces."""

import argparse
import json
import os
import random
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROPOSED = "proposed-cross"


def manifest(config, seed, suite):
    cases = []

    def add(scenario, workload, policy, ablation="full"):
        suffix = f"--{ablation}" if ablation != "full" else ""
        cases.append({
            "name": f"{scenario}--{workload}--{policy}{suffix}", "scenario": scenario,
            "workload": workload, "policy": policy, "ablation": ablation, "seed": seed,
            "initial": config["initial"], "phases": config["scenarios"][scenario],
            "horizon_us": config["horizon_us"], "recovery_k": config["recovery_k"],
        })

    if suite == "repair":
        add("repair-probe", "repair-probe", PROPOSED)
        add("repair-probe", "repair-probe", PROPOSED, "no-cross")
        return {"seed": seed, "suite": suite, "horizon_us": config["horizon_us"],
                "recovery_k": config["recovery_k"], "cases": cases}

    if suite == "full":
        for scenario in config["scenarios"]:
            if scenario not in ("outage-reversal", "repair-probe"):
                for policy in ("fifo", PROPOSED):
                    add(scenario, "mixed", policy)
        for workload in ("state-only", "event-burst"):
            for policy in ("fifo", PROPOSED):
                add("stable", workload, policy)

    for policy in [*config["baselines"], PROPOSED]:
        add("outage-reversal", "high-mixed", policy)
    for ablation in config["ablations"]:
        add("outage-reversal", "high-mixed", PROPOSED, ablation)
    random.Random(seed).shuffle(cases)
    return {"seed": seed, "suite": suite, "horizon_us": config["horizon_us"],
            "recovery_k": config["recovery_k"], "cases": cases}


def write_json(path, data):
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def successful(directory):
    try:
        return json.loads((directory / "summary.json").read_text())["status"] == "passed"
    except (OSError, ValueError, KeyError):
        return False


def run(args):
    if os.geteuid() != 0:
        raise ValueError("run with sudo for netns, tc and tcpdump")
    if not 0 <= args.seed < 2**32 - 1:
        raise ValueError("seed must be an unsigned 32-bit integer below 4294967295")
    config = json.loads((ROOT / "configs/stage5.json").read_text())
    plan = manifest(config, args.seed, args.suite)
    out = args.directory.resolve()
    if args.resume:
        if json.loads((out / "matrix.json").read_text()) != plan:
            raise ValueError("existing manifest differs from the requested seed or suite")
    else:
        out.mkdir(parents=True)
        write_json(out / "matrix.json", plan)

    cases = out / "cases"
    cases.mkdir(exist_ok=True)
    env = {**os.environ, "IOT_SEED": str(args.seed), "PYTHONDONTWRITEBYTECODE": "1"}
    for profile in sorted({case["workload"] for case in plan["cases"]}):
        workload = out / f"{profile}.bin"
        if not args.resume or not workload.is_file():
            subprocess.run([sys.executable, ROOT / "src/workload/generate.py", workload,
                            "--profile", profile, "--seed", str(args.seed)], check=True)

    with (out / "runner.log").open("a") as log:
        for index, case in enumerate(plan["cases"], 1):
            name = case["name"]
            directory = out / name
            if args.resume and successful(directory):
                continue
            if directory.exists():
                directory.rename(out / f".failed-{name}-{time.time_ns()}")
            casefile = cases / f"{name}.json"
            write_json(casefile, case)
            print(f"[{index}/{len(plan['cases'])}] {name}", flush=True)
            command = ["bash", ROOT / "scripts/run-lab.sh", "5", directory, case["policy"],
                       out / f"{case['workload']}.bin", str(case["recovery_k"]), casefile, case["ablation"]]
            log.write(f"\n=== {name} ===\n")
            log.flush()
            subprocess.run(command, env=env, stdout=log, stderr=subprocess.STDOUT, check=True)
    with (out / "comparison.json").open("w") as summary:
        subprocess.run([sys.executable, ROOT / "evaluation/verify_stage5.py", out, "--matrix", "--write-csv"],
                       env=env, stdout=summary, check=True)
    print(f"Verified {len(plan['cases'])} real runs. Metrics: {out / 'metrics.csv'}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--seed", type=int, default=17)
    parser.add_argument("--suite", choices=("full", "high", "repair"), default="full")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    try:
        run(args)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f"Stage 5 failed: {exc}\nInspect {args.directory / 'runner.log'}\n")
