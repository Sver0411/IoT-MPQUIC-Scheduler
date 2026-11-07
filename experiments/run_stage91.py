"""Run isolated Stage 9.1 engineering fixtures and retain every outcome."""

import argparse
import hashlib
import json
import os
import subprocess
from pathlib import Path

from run_stage7 import ROOT, build_workload, stamp, write_json
from run_stage8 import make_blocks, short_workload


def fixtures():
    config = json.loads((ROOT / "configs/stage8.json").read_text())
    legacy = json.loads((ROOT / "configs/stage5.json").read_text())
    config["pilot_seed"] = 8091
    cases = {(c["scenario"], c["load"], c["cell"]): c
             for block in make_blocks(config, legacy, "pilot") for c in block}
    burst = cases["novel-reversal", "heavy", "D"]
    directed = cases["repair", "directed", "D"]
    return [
        {**burst, "name": "attempts", "required": {"blocked": 1}},
        {**cases["repair", "directed", "Cross"], "name": "repair", "required": {"cross_packets": 1}},
        {**burst, "name": "lifecycle", "phases": [], "required": {"frozen": 1, "recovered": 1, "removed": 1},
         "lifecycle_commands": ["freeze", "recover", "remove"]},
        {**directed, "name": "timeout", "horizon_us": 1200000, "required": {"timeouts": 1, "lost_ack": 1},
         "phases": [{"at_us": 100000, "side": "server", "links": {"a": [4, 30, 100], "b": [24, 12, 100]}}]},
        {**burst, "name": "migration", "phases": [], "migration_at_us": 1150000, "required": {"migrated": 1},
         "initial": {"a": [42, 6, 0], "b": [5, 30, 0]}},
    ]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(directory):
    if os.geteuid() != 0:
        raise ValueError("network namespaces require root")
    directory.mkdir(parents=True)
    build = Path(os.environ.get("IOT_BUILD_DIR", ROOT / "build")).resolve()
    cases = fixtures()
    sources = ["CMakeLists.txt", "configs/stage5.json", "configs/stage8.json"]
    for pattern in ("src/**/*.[ch]", "src/**/*.py", "evaluation/**/*.py", "scripts/*.sh",
                    "patches/xquic/*.patch", "network/**/*", "experiments/*.py", "tests/stage91_test.py"):
        sources.extend(str(path.relative_to(ROOT)) for path in ROOT.glob(pattern) if path.is_file())
    source_hashes = {name: digest(ROOT / name) for name in sorted(sources)}
    binary = build / "app/iot-mpquic"
    binary_hash = digest(binary)
    write_json(directory / "manifest.json", {
        "purpose": "Stage 9.1 engineering acceptance",
        "base_commit": subprocess.check_output(
            ["git", "-c", f"safe.directory={ROOT}", "-C", ROOT, "rev-parse", "HEAD"], text=True).strip(),
        "source_sha256": source_hashes,
        "binary_sha256": binary_hash,
        "cases": cases,
    })
    inputs = directory / "inputs"
    inputs.mkdir()
    outcomes = []
    for case in cases:
        name = case["name"]
        print(f"Stage 9.1 fixture {name}", flush=True)
        workload = inputs / f"{name}.bin"
        generator = short_workload if case["workload"].startswith("short-") else build_workload
        generator(workload, case["workload"], case["seed"])
        casefile = inputs / f"{name}.json"
        write_json(casefile, case)
        env = {k: v for k, v in os.environ.items() if k not in
               ("IOT_TUNING", "IOT_STAGE8_REPAIR", "IOT_STAGE91_LIFECYCLE", "IOT_STAGE91_MIGRATION", "IOT_NETEM_TC")}
        env.update(IOT_BUILD_DIR=str(build), IOT_SEED=str(case["seed"]), IOT_PATH_POLICY=case["path_scheduler"],
                   IOT_HORIZON_US=str(case["horizon_us"]), IOT_STAGE8_SEED=str(case["assignment_seed"]),
                   IOT_STAGE91_TRACE="1", PYTHONDONTWRITEBYTECODE="1")
        if case["repair"] == "cross":
            env["IOT_STAGE8_REPAIR"] = "cross"
        if case.get("lifecycle_commands"):
            env["IOT_STAGE91_LIFECYCLE"] = "1"
        if case.get("migration_at_us"):
            env["IOT_STAGE91_MIGRATION"] = "1"
        command = ["bash", ROOT / "scripts/run-lab.sh", "91", directory / name, case["policy"], workload,
                   str(case["recovery_k"]), casefile, case["ablation"]]
        start = stamp()
        with (directory / f"{name}.log").open("w") as log:
            result = subprocess.run(command, env=env, stdout=log, stderr=subprocess.STDOUT)
        outcomes.append({"case": name, "start_utc": start, "end_utc": stamp(), "exit_code": result.returncode})
        write_json(directory / "execution.json", outcomes)
    unchanged = binary_hash == digest(binary) and all(digest(ROOT / name) == sha for name, sha in source_hashes.items())
    write_json(directory / "provenance-check.json", {"source_and_binary_unchanged": unchanged})
    checksums = {str(file.relative_to(directory)): digest(file)
                 for file in sorted(directory.rglob("*")) if file.is_file()}
    write_json(directory / "sha256.json", checksums)
    return unchanged and all(result["exit_code"] == 0 for result in outcomes)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    raise SystemExit(0 if run(parser.parse_args().directory.resolve()) else 1)
