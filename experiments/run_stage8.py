"""Run committed Stage 8 designs in isolated network namespaces."""

import argparse
import hashlib
import json
import os
import random
import struct
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from run_stage7 import DEFAULT_TUNING, ROOT, build_workload, stamp, workload, write_json

PAIRED = (
    ("A", "fifo", "minrtt"), ("B", "proposed-app", "minrtt"),
    ("C", "fifo", "proposed-path"), ("D", "proposed-app", "proposed-path"),
    ("Fixed", "proposed-app", "fixed-minrtt"), ("Earliest", "proposed-app", "earliest-completion"),
    ("Cross", "proposed-app", "proposed-path"),
)


def make_blocks(config, legacy, suite):
    scenarios = {
        "high-load": (legacy["initial"], legacy["scenarios"]["outage-reversal"], "high-mixed"),
        "novel-reversal": (config["novel"]["initial"], config["novel"]["phases"], "short-burst"),
        "repair": (legacy["initial"], legacy["scenarios"]["repair-probe"], "repair-probe"),
    }

    def block(seed, scenario, load, cells):
        initial, phases, profile = scenarios[scenario]
        if scenario != "repair" and load == "light":
            profile = "short-light"
        elif suite == "explore" or load == "explore-heavy":
            profile = "short-burst"
        group = []
        for cell, policy, path in cells:
            group.append({
                "name": f"{scenario}--{load}--{seed}--{cell}", "scenario": scenario, "seed": seed,
                "cell": cell, "load": load, "workload": profile,
                "assignment_seed": random.Random(f"assignment/{scenario}/{load}/{seed}").getrandbits(32),
                "network_seed": random.Random(f"network/{scenario}/{seed}").getrandbits(31),
                "policy": policy, "path_scheduler": path, "ablation": "full",
                "repair": "cross" if cell == "Cross" else "native" if path == "minrtt" else "strict",
                "initial": initial, "phases": phases, "horizon_us": config["horizon_us"],
                "recovery_k": config["recovery_k"], "tuning": DEFAULT_TUNING,
            })
        random.Random(f"order/{scenario}/{load}/{seed}").shuffle(group)
        return group

    paired, explored = config["paired_seeds"], config["exploration_seeds"]
    seeds = [config["pilot_seed"]] if suite == "pilot" else range(
        (explored if suite == "explore" else paired)[0], (explored if suite == "explore" else paired)[1] + 1)
    blocks = []
    for seed in seeds:
        if suite in ("pilot", "paired"):
            blocks.extend(block(seed, scene, "heavy", PAIRED) for scene in ("high-load", "novel-reversal"))
            blocks.append(block(seed, "repair", "directed", [PAIRED[3], PAIRED[-1]]))
        if suite in ("pilot", "explore"):
            cells = [("Random", "proposed-app", "random-path")]
            blocks.extend(block(seed, scene, load, cells) for scene in ("high-load", "novel-reversal")
                          for load in ("light", "explore-heavy"))
    random.Random(f"stage8/{suite}").shuffle(blocks)
    for order, case in enumerate((case for block in blocks for case in block), 1):
        case["experiment_order"] = order
    return blocks


def short_workload(file, profile, seed):
    rng = random.Random(seed)
    heavy = profile == "short-burst"
    rows = []
    # Event bursts put real positive slack on both sides of the 100 ms boundary.
    for burst, at in enumerate((190_000, 390_000, 580_000, 800_000, 1160_000, 1450_000)):
        for j in range(9 if heavy else 4):
            release = at + rng.randrange(14_000)
            size = (384, 1400, 4200)[(burst + j) % 3]
            slack = (25_000, 55_000, 90_000, 130_000, 210_000)[(j + burst) % 5]
            severity = rng.randint(1, 5)
            rows.append((release, 2, 1 + severity, severity, 1 + j % 4, 10 * burst + j + 1,
                         release, release + slack, 0, rng.randbytes(size)))
    for sensor in range(1, 5 if heavy else 3):
        for seq in range(1, 11):
            release = 180_000 + (seq - 1) * (95_000 + 10_000 * sensor) + rng.randrange(8000)
            age = (90_000, 180_000, 330_000)[(sensor + seq) % 3]
            size = (64, 192, 1800)[sensor % 3]
            payload = struct.pack("!hh", rng.randint(1700, 3600), rng.randint(2000, 9000))
            rows.append((release, 1, 1, 0, sensor, seq, release, release + age + 60_000, age,
                         payload + rng.randbytes(size - len(payload))))
    with file.open("xb") as output:
        for id_, row in enumerate(sorted(rows, key=lambda row: row[0]), 1):
            release, kind, priority, severity, source, serial, generated, deadline, age, data = row
            header = workload.RECORD.pack(b"IOT1", kind, priority, severity, 0, len(data), id_,
                                          source, serial, generated, deadline, age)
            output.write(workload.PREFIX.pack(release, len(header) + len(data)) + header + data)


def get_provenance(build_dir):
    def git(directory, *args):
        return subprocess.check_output(["git", "-c", f"safe.directory={directory}", "-C", directory, *args],
                                       text=True).strip()

    def sha(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    tracked_changes = git(ROOT, "status", "--porcelain", "--untracked-files=no", "--",
                          "src", "CMakeLists.txt", "scripts", "configs", "evaluation", "experiments", "network")
    if tracked_changes:
        raise ValueError("commit experiment code and configuration before measurement")
    return {
        "commit": git(ROOT, "rev-parse", "HEAD"),
        "build": {
            "exe_sha256": sha(build_dir / "app/iot-mpquic"),
            "xquic_commit": git(ROOT / "third_party/xquic", "rev-parse", "HEAD"),
            "boringssl_commit": git(build_dir / "boringssl", "rev-parse", "HEAD"),
            "stream_pto_patch_sha256": sha(ROOT / "patches/xquic/stream-pto-scheduler.patch"),
        },
        "compiler": subprocess.check_output(["cc", "--version"], text=True).splitlines()[0],
        "cmake": subprocess.check_output(["cmake", "--version"], text=True).splitlines()[0],
        "config_sha256": sha(ROOT / "configs/stage8.json"),
        "reference_network_sha256": sha(ROOT / "configs/stage5.json"),
    }


def run(args):
    if os.geteuid() != 0 or args.jobs < 1:
        raise ValueError("network namespaces require root and jobs must be positive")
    config = json.loads((ROOT / "configs/stage8.json").read_text())
    legacy = json.loads((ROOT / "configs/stage5.json").read_text())
    out = args.directory.resolve()
    build_dir = Path(os.environ.get("IOT_BUILD_DIR", ROOT / "build")).resolve()
    provenance = get_provenance(build_dir)
    blocks = make_blocks(config, legacy, args.suite)
    cases = [case for block in blocks for case in block]
    for case in cases:
        case["provenance"] = provenance
    manifest = {"suite": args.suite, "jobs": args.jobs, "provenance": provenance,
                "config": config, "cases": cases}
    manifest_path = out / "manifest.json"
    if args.resume:
        if json.loads(manifest_path.read_text()) != manifest:
            raise ValueError("resume requires identical source commit, binary and design")
    else:
        out.mkdir(parents=True)
        write_json(manifest_path, manifest)
    for child in ("runs", "inputs", "execution"):
        (out / child).mkdir(exist_ok=True)

    def input_path(case):
        return out / "inputs" / f"{case['workload']}--{case['seed']}.bin"

    for case in cases:
        file = input_path(case)
        if not file.exists():
            generator = short_workload if case["workload"].startswith("short-") else build_workload
            generator(file, case["workload"], case["seed"])
        casefile = out / "inputs" / f"{case['name']}.json"
        write_json(casefile, case)

    def run_block(block):
        for case in block:
            name = case["name"]
            folder = out / "runs" / name
            execution = out / "execution" / f"{name}.json"
            log_path = execution.with_suffix(".log")
            if args.resume and execution.is_file() and (folder / "summary.json").is_file():
                continue
            if folder.exists():
                failed = f".failed-{name}-{stamp().replace(':', '')}"
                folder.rename(folder.with_name(failed))
                if log_path.is_file():
                    log_path.rename(log_path.with_name(failed + ".log"))
            env = {**os.environ, "IOT_SEED": str(case["seed"]), "IOT_PATH_POLICY": case["path_scheduler"],
                   "IOT_HORIZON_US": str(case["horizon_us"]), "IOT_BUILD_DIR": str(build_dir),
                   "IOT_STAGE8_SEED": str(case["assignment_seed"]), "PYTHONDONTWRITEBYTECODE": "1"}
            env.pop("IOT_TUNING", None)
            env.pop("IOT_STAGE8_REPAIR", None)
            if case["repair"] == "cross":
                env["IOT_STAGE8_REPAIR"] = "cross"
            command = ["bash", ROOT / "scripts/run-lab.sh", "8", folder, case["policy"], input_path(case),
                       str(case["recovery_k"]), out / "inputs" / f"{name}.json", case["ablation"]]
            print(f"[{case['experiment_order']}/{len(cases)}] {name}", flush=True)
            start = stamp()
            with log_path.open("w") as log:
                result = subprocess.run(command, env=env, stdout=log, stderr=subprocess.STDOUT)
            if result.returncode:
                raise RuntimeError(f"{name} failed ({result.returncode}): {log_path}")
            write_json(execution, {"start_utc": start, "end_utc": stamp()})

    with ThreadPoolExecutor(args.jobs) as pool:
        list(pool.map(run_block, blocks))
    print(f"Verified {len(cases)} runs at {out}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--suite", required=True, choices=("pilot", "paired", "explore"))
    parser.add_argument("--jobs", type=int, default=1)
    parser.add_argument("--resume", action="store_true")
    run(parser.parse_args())
