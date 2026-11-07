"""Run Stage 7 seed blocks through the real dual-namespace XQUIC lab."""

import argparse
import hashlib
import importlib.util
import json
import os
import random
import struct
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("workload", ROOT / "src/workload/generate.py")
workload = importlib.util.module_from_spec(spec)
spec.loader.exec_module(workload)

FACTORIAL = {
    "A": ("fifo", "minrtt"),
    "B": ("proposed-app", "minrtt"),
    "C": ("fifo", "proposed-path"),
    "D": ("proposed-app", "proposed-path"),
}
ABLATIONS = ("no-event", "no-aoi", "no-replace", "no-recovery",
             "fixed-path", "no-probability", "no-cross")
DEFAULT_TUNING = {"event_burst": 3, "severity_weight": 2, "waiting_weight": 1,
                  "slack_margin_us": 0, "loss_scale": 1}
SENSITIVITY = {
    "recovery-k": (1, 4), "event_burst": (1, 6), "severity_weight": (1, 4),
    "waiting_weight": (0.5, 2), "slack_margin_us": (25_000, 50_000), "loss_scale": (0.5, 2),
}


def stamp():
    return datetime.now(UTC).isoformat()


def write_json(path, data):
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def build_workload(path, profile, seed):
    rng = random.Random(seed)
    if profile == "repair-probe":
        rows = workload.proposed_workload(rng, probe_budget_us=25_000)
        release, *fields = rows[0]
        shift = rng.randint(-15_000, 15_000)
        fields[5] += shift
        fields[6] += shift
        rows[0] = (release + shift, *fields)
    elif profile in {"state-heavy", "event-heavy"}:
        rows = mixed_sensors(rng, profile == "state-heavy")
    else:
        rows = workload.experiment_workload(rng, profile)

    with path.open("xb") as file:
        for id_, row in enumerate(sorted(rows, key=lambda item: item[0]), 1):
            release, kind, priority, severity, source, serial, generated, deadline, age, data = row
            header = workload.RECORD.pack(b"IOT1", kind, priority, severity, 0, len(data), id_,
                                          source, serial, generated, deadline, age)
            file.write(workload.PREFIX.pack(release, len(header) + len(data)))
            file.write(header)
            file.write(data)


def mixed_sensors(rng, state_heavy):
    rows = []
    for sensor, period, age, payload in ((1, 85_000, 200_000, 16), (2, 140_000, 320_000, 32),
                                          (3, 250_000, 550_000, 192), (4, 370_000, 760_000, 1200)):
        if not state_heavy and sensor > 2:
            break
        for serial in range(1, 16 if state_heavy else 6):
            release = 200_000 + (serial - 1) * period + rng.randrange(15_000)
            if release >= 1_750_000:
                break
            data = struct.pack("!hh", rng.randint(1700, 3600), rng.randint(2000, 9000))
            rows.append((release, 1, 1, 0, sensor, serial, release, release + age, age,
                         data + rng.randbytes(payload - len(data))))
            if state_heavy and sensor == 1 and serial % 4 == 0:
                rows.append((release + 1, 1, 1, 0, sensor, 100 + serial, release + 1,
                             release + age, age, data))
    for burst in range(3):
        for source in range(1, 3 if state_heavy else 5):
            for offset in range(1 if state_heavy else 4):
                serial = 1 + burst * 4 + offset
                release = 220_000 + 310_000 * burst + rng.randrange(12_000)
                severity = rng.randint(1, 5)
                deadline = release + (95_000, 240_000, 650_000)[(burst + offset) % 3]
                size = rng.choice((64, 640, 2400, 4800))
                rows.append((release, 2, severity + 1, severity, source, serial,
                             release, deadline, 0, rng.randbytes(size)))
    return rows


def make_plan(config, legacy, seeds, suite):
    blocks = []
    for seed in seeds:
        network_seed = random.Random(seed ^ 0x570701).randrange(1, 2**31)
        def add(label, scene, profile, policies):
            block = []
            for cell, policy, path, ablation in policies:
                block.append({
                    "name": f"{label}--{seed}--{cell}", "scenario": label,
                    "network_profile": scene, "workload": profile, "cell": cell,
                    "policy": policy, "path_scheduler": path, "ablation": ablation,
                    "application_scheduler": "proposed-app" if policy == "proposed-app" else policy,
                    "repair": ("cross" if policy == "proposed-cross" and ablation != "no-cross"
                               else "strict" if path == "proposed-path" else "native"),
                    "seed": seed, "network_seed": network_seed, "initial": legacy["initial"],
                    "phases": legacy["scenarios"][scene], "horizon_us": legacy["horizon_us"],
                    "recovery_k": legacy["recovery_k"], "tuning": dict(DEFAULT_TUNING),
                })
            random.Random(f"{seed}/{label}").shuffle(block)
            blocks.append(block)

        if suite in {"pilot", "factorial", "high-extension"}:
            for label, (scene, profile) in config["scenarios"].items():
                if suite == "high-extension" and label != "high-load":
                    continue
                add(label, scene, profile, [(cell, *policies, "full")
                                            for cell, policies in FACTORIAL.items()])
        if suite == "loss":
            add("partial-loss", "loss", "mixed", [(cell, *policies, "full")
                                                  for cell, policies in FACTORIAL.items()])
        if suite in {"pilot", "repair"}:
            add("repair", "repair-probe", "repair-probe",
                [(cell, "proposed-cross", "proposed-path", ablation)
                 for cell, ablation in (("Strict", "no-cross"), ("Cross", "full"))])
        if suite in {"pilot", "ablation"}:
            controls = [(p, p, "minrtt", "full") for p in legacy["baselines"]]
            joint = [(variant, "proposed-cross", "proposed-path", variant)
                     for variant in ("full", *ABLATIONS)]
            add("mechanisms", "outage-reversal", "high-mixed", controls + joint)
        if suite == "sensitivity":
            variants = {"full": (None, None)}
            variants.update({f"{key}-{value}": (key, value)
                             for key, values in SENSITIVITY.items() for value in values})
            add("sensitivity", "outage-reversal", "high-mixed",
                [(label, "proposed-cross", "proposed-path", "full") for label in variants])
            for case in blocks[-1]:
                key, value = variants[case["cell"]]
                if key == "recovery-k":
                    case["recovery_k"] = value
                elif key is not None:
                    case["tuning"][key] = value
    random.Random(f"stage7/{suite}").shuffle(blocks)
    for order, case in enumerate((case for block in blocks for case in block), 1):
        case["experiment_order"] = order
    return blocks


def run(args):
    if os.geteuid() != 0:
        raise ValueError("network namespaces require root")
    config = json.loads((ROOT / "configs/stage7.json").read_text())
    legacy = json.loads((ROOT / "configs/stage5.json").read_text())
    seed_list = {"pilot": "pilot_seeds", "high-extension": "high_load_extension_seeds"}
    seeds = args.seeds or config[seed_list.get(args.suite, "seeds")]
    if len(set(seeds)) != len(seeds) or any(not 0 <= seed < 2**32 for seed in seeds):
        raise ValueError("seeds must be unique uint32 values")
    out = args.directory.resolve()
    blocks = make_plan(config, legacy, seeds, args.suite)
    cases = [case for block in blocks for case in block]
    exe = Path(os.environ.get("IOT_BUILD_DIR", ROOT / "build")) / "app/iot-mpquic"
    plan = {"suite": args.suite, "seeds": seeds, "jobs": args.jobs,
            "executable_sha256": hashlib.sha256(exe.read_bytes()).hexdigest(), "cases": cases}
    if args.resume:
        if json.loads((out / "experiment-manifest.json").read_text()) != plan:
            raise ValueError("existing manifest differs from this plan or build")
    else:
        out.mkdir(parents=True)
        write_json(out / "experiment-manifest.json", plan)
    for name in ("raw", "inputs", "execution"):
        (out / name).mkdir(exist_ok=True)
    for case in cases:
        profile, seed = case["workload"], case["seed"]
        path = out / "inputs" / f"{profile}--{seed}.bin"
        if not path.exists():
            build_workload(path, profile, seed)
        write_json(out / "inputs" / f"{case['name']}.json", case)

    def run_block(block):
        for case in block:
            name = case["name"]
            directory = out / "raw" / name
            execution = out / "execution" / f"{name}.json"
            if args.resume and execution.exists() and (directory / "summary.json").is_file():
                continue
            log_path = out / "execution" / f"{name}.log"
            if directory.exists():
                failed = f".failed-{name}-{datetime.now(UTC).timestamp()}"
                directory.rename(directory.with_name(failed))
                if log_path.exists():
                    log_path.rename(log_path.with_name(failed + ".log"))
            path = out / "inputs" / f"{case['workload']}--{case['seed']}.bin"
            env = {**os.environ, "IOT_SEED": str(case["seed"]), "PYTHONDONTWRITEBYTECODE": "1",
                   "IOT_PATH_POLICY": case["path_scheduler"], "IOT_HORIZON_US": str(case["horizon_us"]),
                   "IOT_TUNING": ",".join(str(case["tuning"][key]) for key in DEFAULT_TUNING)}
            command = ["bash", ROOT / "scripts/run-lab.sh", "7", directory, case["policy"], path,
                       str(case["recovery_k"]), out / "inputs" / f"{name}.json", case["ablation"]]
            started = stamp()
            print(f"[{case['experiment_order']}/{len(cases)}] {name}", flush=True)
            with log_path.open("w") as log:
                subprocess.run(command, env=env, stdout=log, stderr=subprocess.STDOUT, check=True)
            write_json(execution, {"start_utc": started, "end_utc": stamp()})

    with ThreadPoolExecutor(args.jobs) as pool:
        list(pool.map(run_block, blocks))
    print(f"Verified {len(cases)} runs at {out}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--suite", choices=("pilot", "factorial", "high-extension", "repair",
                                           "ablation", "loss", "sensitivity"), required=True)
    parser.add_argument("--seeds", nargs="+", type=int)
    parser.add_argument("--jobs", type=int, default=1)
    parser.add_argument("--resume", action="store_true")
    run(parser.parse_args())
