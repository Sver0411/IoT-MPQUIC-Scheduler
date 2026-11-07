"""Deterministic cross-environment designs and a closed test manifest."""

import hashlib
import itertools
import json
import random
import struct
from pathlib import Path

from run_stage7 import DEFAULT_TUNING, ROOT, workload

ARMS = {"off": [], "logs": ["logs"], "digest": ["digest"], "capture": ["capture"],
        "full": ["logs", "digest", "capture"]}
TIMES = {"early": 700_000, "late": 1_250_000}
POLICIES = ("fixed-minrtt", "earliest-completion", "proposed-path", "random-path")


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def file_hash(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def load_design():
    return json.loads((ROOT / "configs/stage92.json").read_text())


def network(environment, timing):
    start = TIMES[timing]
    side = environment.get("side", "both")
    phases = [{"at_us": start, "side": side, "links": environment["after"]}]
    if "restore_us" in environment:
        phases.append({"at_us": start + environment["restore_us"], "side": side,
                       "links": environment.get("restore", {k: environment["initial"][k] for k in environment["after"]})})
    return {"initial": environment["initial"], "phases": phases,
            **({"queue_burst": {**environment["queue_burst"], "at_us": start + 10_000}}
               if "queue_burst" in environment else {})}


def case_config(config, **fields):
    return {"stage92": True, "version": config["version"], "policy": "proposed-app", "repair": "strict",
            "recovery_k": 2, "tuning": DEFAULT_TUNING, "ablation": "full", "required": {},
            "horizon_us": config["horizon_us"], **fields}


def matrix(config):
    cases = []
    for e, environment in enumerate(config["environments"]):
        for replica, timing in itertools.product(range(config["pilot_repeats"]), TIMES):
            connection = 920_000 + 100 * e + 2 * replica + (timing == "late")
            profile = network(environment, timing)
            for load, budget in itertools.product(("low", "high"), ("regular", "short")):
                # Use a common superset payload trace across density and budget within an environment block.
                suffix = f"{environment['id']}--{timing}--{replica}--{load}--{budget}"
                cases.append(case_config(config, name=suffix, environment=environment["id"],
                                         family=environment["family"], split=environment["split"],
                                         timing=timing, load=load, budget=budget, seed=connection,
                                         assignment_seed=connection + 10_000, network_seed=connection + 20_000,
                                         network_sha256=fingerprint(profile), path_scheduler="random-path",
                                         trace="full", features=True, **profile))
    validate_isolation(cases)
    return cases


def validate_isolation(cases):
    seen = {}
    for case in cases:
        split = case["split"]
        if split not in ("train", "validation", "sealed"):
            raise ValueError("unknown partition")
        profile = {k: case[k] for k in ("initial", "phases", "queue_burst") if k in case}
        if fingerprint(profile) != case["network_sha256"]:
            raise ValueError("network fingerprint mismatch")
        keys = [(field, case[field]) for field in
                ("environment", "family", "network_sha256", "seed", "network_seed", "assignment_seed")]
        # Ignore absolute timing when testing whether another partition reused a mechanism/config.
        phase = profile["phases"][0]["at_us"]
        normalized = {**profile, "phases": [{**row, "at_us": row["at_us"] - phase} for row in profile["phases"]]}
        if "queue_burst" in profile:
            normalized["queue_burst"] = {**profile["queue_burst"], "at_us": profile["queue_burst"]["at_us"] - phase}
        keys.append(("network_shape", fingerprint(normalized)))
        for key in keys:
            if seen.setdefault(key, split) != split:
                raise ValueError(f"cross-partition overlap: {key[0]}")


def overhead(config):
    rng = random.Random(config["design_seed"])
    for block in range(config["overhead_blocks"]):
        seed = 929_000 + block
        arms = list(ARMS)
        rng.shuffle(arms)
        for arm in arms:
            yield case_config(config, name=f"overhead-{block:02d}-{arm}", block=block, split="engineering",
                              seed=seed, network_seed=seed + 20_000, assignment_seed=seed + 10_000,
                              load="high" if block % 2 else "low", budget="short", trace=arm, features=False,
                              initial={"a": [5, 24, 0], "b": [11, 18, 0]}, phases=[],
                              path_scheduler="proposed-path")


def write_workload(path, case):
    rng = random.Random(case["seed"])
    records = []
    for i in range(40):
        release = 165_000 + i * 45_000 + rng.randrange(3000)
        for lane in range(3):
            data = rng.randbytes((240, 900, 2500)[(i + lane) % 3])
            severity = rng.randrange(1, 6)
            if case["load"] == "low" and lane:
                continue
            kind = 1 if i % 4 == 0 and not lane else 2
            budget = (35_000, 60_000, 95_000)[i % 3] if case["budget"] == "short" else (140_000, 210_000)[i % 2]
            generated = release + lane * 1000
            age = budget * 4 // 5 if kind == 1 else 0
            records.append((generated, kind, severity, lane + 1, i + 1, budget, age, data))
    with path.open("xb") as stream:
        for key, (release, kind, severity, source, serial, budget, age, data) in enumerate(records, 1):
            header = workload.RECORD.pack(b"IOT1", kind, severity + 1 if kind == 2 else 1,
                                          severity if kind == 2 else 0, 0, len(data), key, source, serial,
                                          release, release + budget, age)
            stream.write(workload.PREFIX.pack(release, len(header) + len(data)) + header + data)


def freeze(config, cases, build):
    validate_isolation(cases)
    sealed = [case for case in cases if case["split"] == "sealed"]
    if len({case["family"] for case in sealed}) < 3:
        raise ValueError("at least three sealed environment families are required")
    body = {"state": "closed", "design_version": config["version"], "build": build,
            "quality": config["quality"], "cases": sealed, "comparison_policies": POLICIES,
            "statistics": {"unit": "independent connection seed", "prediction": "paired Brier, effective slack (0,100000]",
                           "delivery": "generated short events including unsubmitted; received before deadline and window",
                           "interval": "paired seed bootstrap, 4000 draws; Holm by prespecified hypothesis family",
                           "missing": "retain every run and row; report coverage and invalid runs by environment"},
            "access": "Configuration only. An independent custodian must freeze candidate, sample size and software, verify the manifest and authorize a separate confirmatory runner. Development runner rejects this partition."}
    return {**body, "seal_sha256": fingerprint(body)}


def verify_seal(seal):
    body = {k: v for k, v in seal.items() if k != "seal_sha256"}
    if body["state"] != "closed" or fingerprint(body) != seal.get("seal_sha256"):
        raise ValueError("closed manifest was modified")
    validate_isolation(body["cases"])
    if any(case["split"] != "sealed" for case in body["cases"]):
        raise ValueError("development case in sealed manifest")
