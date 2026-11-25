#!/usr/bin/env python3
"""Stage 10: Real XQUIC Dual-Path Evaluation of FAP-MPQUIC vs. MinRTT, ECF, BLEST, and Probability.

Runs isolated Linux network namespaces (dual veth + tc/netem + real XQUIC client/server +
dual-interface tcpdump PCAP capture) and extracts exact Stage-7/8 audited metrics.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import csv
import hashlib
import json
import math
import os
from pathlib import Path
import random
import shutil
import statistics
import struct
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "experiments"))
sys.path.insert(0, str(ROOT / "evaluation"))

from run_stage7 import DEFAULT_TUNING, build_workload, workload  # noqa: E402
from stage7 import extract  # noqa: E402

ARMS = [
    ("fifo_minrtt", "fifo", "minrtt", "native"),
    ("app_minrtt", "proposed-app", "minrtt", "native"),
    ("fixed_minrtt", "proposed-app", "fixed-minrtt", "strict"),
    ("ecf", "proposed-app", "earliest-completion", "strict"),
    ("blest", "proposed-app", "blest", "strict"),
    ("prob_strict", "proposed-app", "proposed-path", "strict"),
    ("fap_noprobe", "proposed-app", "fresh-aware-noprobe", "strict"),
    ("fap_strict", "proposed-app", "fresh-aware", "strict"),
    ("fap_cross", "proposed-app", "fresh-aware", "cross"),
]

SCENARIOS = {
    "S1_outage_reversal": {
        "initial": {"a": [4, 30, 0], "b": [24, 12, 0]},
        "phases": [
            {"at_us": 120000, "links": {"a": [4, 30, 100]}},
            {"at_us": 850000, "links": {"a": [45, 6, 0], "b": [5, 30, 0]}},
        ],
        "workload": "high-mixed",
        "horizon_us": 4000000,
    },
    "S2_asymmetric_reversal": {
        "initial": {"a": [4, 30, 0], "b": [24, 12, 0]},
        "phases": [
            {"at_us": 500000, "links": {"a": [120, 0.8, 0, 10], "b": [6, 28, 0]}},
        ],
        "workload": "high-mixed",
        "horizon_us": 4000000,
    },
    "S3_double_reversal": {
        "initial": {"a": [5, 24, 0], "b": [24, 14, 0]},
        "phases": [
            {"at_us": 450000, "links": {"a": [110, 1.0, 0, 12], "b": [5, 30, 0]}},
            {"at_us": 1250000, "links": {"a": [6, 28, 0], "b": [110, 1.0, 0, 12]}},
        ],
        "workload": "high-mixed",
        "horizon_us": 4000000,
    },
    "S4_mid_blackout": {
        "initial": {"a": [4, 30, 0], "b": [24, 12, 0]},
        "phases": [
            {"at_us": 450000, "side": "client", "links": {"a": [4, 30, 100]}},
            {"at_us": 1150000, "links": {"a": [45, 6, 0], "b": [5, 30, 0]}},
        ],
        "workload": "mixed",
        "horizon_us": 4000000,
    },
}


def build_stage10_burst_workload(path: Path, seed: int) -> None:
    """Mixed bursty IoT alarms (45-200ms slack) + periodic sensor states across both reversal phases."""
    rng = random.Random(seed)
    rows = []
    for burst, at in enumerate((190_000, 420_000, 620_000, 820_000, 1_020_000, 1_240_000, 1_480_000)):
        for j in range(6):
            release = at + rng.randrange(18_000)
            size = (256, 1024, 2800)[(burst + j) % 3]
            slack = (45_000, 75_000, 110_000, 150_000, 200_000)[(j + burst) % 5]
            severity = rng.randint(1, 5)
            rows.append((release, 2, 1 + severity, severity, 1 + j % 4, 10 * burst + j + 1,
                         release, release + slack, 0, rng.randbytes(size)))
    for sensor in range(1, 5):
        for seq in range(1, 16):
            release = 160_000 + (seq - 1) * (90_000 + 8_000 * sensor) + rng.randrange(6_000)
            age = (100_000, 180_000, 320_000)[(sensor + seq) % 3]
            size = (64, 192, 1200)[sensor % 3]
            payload = struct.pack("!hh", rng.randint(1700, 3600), rng.randint(2000, 9000))
            rows.append((release, 1, 1, 0, sensor, seq, release, release + age + 60_000, age,
                         payload + rng.randbytes(size - len(payload))))
    with path.open("xb") as output:
        for id_, row in enumerate(sorted(rows, key=lambda r: r[0]), 1):
            release, kind, priority, severity, source, serial, generated, deadline, age, data = row
            header = workload.RECORD.pack(b"IOT1", kind, priority, severity, 0, len(data), id_,
                                          source, serial, generated, deadline, age)
            output.write(workload.PREFIX.pack(release, len(header) + len(data)) + header + data)


def run_single_connection(case: dict, inp_file: Path, work_dir: Path, build_dir: Path, cert: Path, key: Path) -> dict:
    """Run one real dual-veth XQUIC connection inside isolated Linux netns."""
    ns_tag = f"s10-{os.getpid()}-{case['worker_tag']}"
    c_ns = f"c-{ns_tag}"[:15]
    s_ns = f"s-{ns_tag}"[:15]
    work_dir.mkdir(parents=True, exist_ok=True)
    case_path = work_dir / "case.json"
    case_path.write_text(json.dumps(case, indent=2) + "\n")
    shutil.copyfile(inp_file, work_dir / "workload.bin")

    exe = build_dir / "app/iot-mpquic"
    script = f"""
set -euo pipefail
client_ns="{c_ns}"
server_ns="{s_ns}"
client_created=0; server_created=0; client_pid=""; server_pid=""; captures=()
cleanup() {{
  local st=$?
  trap - EXIT
  [[ -n "$client_pid" ]] && {{ kill "$client_pid" 2>/dev/null || true; wait "$client_pid" 2>/dev/null || true; }}
  [[ -n "$server_pid" ]] && {{ kill "$server_pid" 2>/dev/null || true; wait "$server_pid" 2>/dev/null || true; }}
  for p in "${{captures[@]}}"; do kill -INT "$p" 2>/dev/null || true; done
  for p in "${{captures[@]}}"; do wait "$p" 2>/dev/null || true; done
  ((server_created)) && ip netns del "$server_ns" 2>/dev/null || true
  ((client_created)) && ip netns del "$client_ns" 2>/dev/null || true
  exit "$st"
}}
trap cleanup EXIT
wait_log() {{
  local file=$1 pat=$2 pid=$3 until=$((SECONDS + 30))
  until [[ -f "$file" ]] && grep -q -- "$pat" "$file"; do
    if ! kill -0 "$pid" 2>/dev/null || ((SECONDS >= until)); then return 1; fi
    sleep 0.03
  done
}}
source "{ROOT}/network/topology/dual-veth.sh"
setup_topology
python3 "{ROOT}/network/scenarios/stage5.py" initial "{work_dir}" "$client_ns" "$server_ns"
for link in a b; do
  ip netns exec "$server_ns" tcpdump -nn -i "iot-$link" -s 96 -U --immediate-mode -B 2048 -Z root \
    -w "{work_dir}/link-$link.pcap" 'udp and port 4433 and port 4434' 2> "{work_dir}/tcpdump-$link.log" &
  captures+=("$!")
  wait_log "{work_dir}/tcpdump-$link.log" 'listening on' "$!"
done
ip netns exec "$server_ns" "{exe}" iot-server "{work_dir}/server.xquic.log" "{cert}" "{key}" \
  > "{work_dir}/server.log" 2> "{work_dir}/server.stderr" &
server_pid=$!
wait_log "{work_dir}/server.log" 'event=ready role=server' "$server_pid"
extra_args=()
if [[ "{case['policy']}" == "proposed-app" ]]; then extra_args+=("{case['recovery_k']}"); fi
ip netns exec "$client_ns" "{exe}" iot-client "{work_dir}/client.xquic.log" "{work_dir}/workload.bin" \
  "{case['policy']}" "${{extra_args[@]}}" > "{work_dir}/client.log" 2> "{work_dir}/client.stderr" &
client_pid=$!
wait_log "{work_dir}/client.log" 'event=iot_start ' "$client_pid"
python3 "{ROOT}/network/scenarios/stage5.py" timeline "{work_dir}" "$client_ns" "$server_ns"
wait "$client_pid" || true; client_pid=""
wait "$server_pid" || true; server_pid=""
for p in "${{captures[@]}}"; do kill -INT "$p" 2>/dev/null || true; done
for p in "${{captures[@]}}"; do wait "$p" 2>/dev/null || true; done
captures=()
"""
    env = {
        **os.environ,
        "IOT_SEED": str(case["seed"]),
        "IOT_PATH_POLICY": case["path_scheduler"],
        "IOT_HORIZON_US": str(case["horizon_us"]),
        "IOT_STAGE8_SEED": str(case["assignment_seed"]),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    env.pop("IOT_STAGE8_REPAIR", None)
    if case["repair"] == "cross":
        env["IOT_STAGE8_REPAIR"] = "cross"
    proc = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, timeout=45)
    if proc.returncode != 0:
        raise RuntimeError(f"Run failed {case['name']}: {proc.stderr[:400]}")

    metrics, *_ = extract(work_dir, case)
    a_b = metrics.get("path_a_udp_rx_bytes") or 0
    b_b = metrics.get("path_b_udp_rx_bytes") or 0
    tot_b = a_b + b_b
    row = {
        "scenario": case["scenario"],
        "seed": case["seed"],
        "arm": case["arm"],
        "policy": case["policy"],
        "path_scheduler": case["path_scheduler"],
        "repair": case["repair"],
        "event_deadline_success": metrics["event_deadline_success"],
        "mean_aoi_ms": metrics["mean_aoi_ms"],
        "p95_aoi_ms": metrics.get("p95_aoi_ms"),
        "aoi_coverage": metrics["aoi_coverage"],
        "maximum_age_violation_ratio": metrics["maximum_age_violation_ratio"],
        "useful_goodput_bps": metrics["useful_goodput_bps"],
        "event_p50_ms": metrics.get("event_p50_ms"),
        "event_p95_ms": metrics.get("event_p95_ms"),
        "path_a_share": (a_b / tot_b) if tot_b > 0 else 0.0,
        "path_b_share": (b_b / tot_b) if tot_b > 0 else 0.0,
        "retrans_ratio": metrics.get("retransmission_ratio"),
        "client_ok": int("event=exit ok=1" in (work_dir / "client.log").read_text()),
    }
    return row


def paired_stats(rows: list[dict], ref_arm: str = "fap_cross") -> list[dict]:
    import numpy as np

    rng = np.random.default_rng(20261126)
    by_sc_arm: dict[tuple[str, str], dict[int, dict]] = {}
    for r in rows:
        by_sc_arm.setdefault((r["scenario"], r["arm"]), {})[int(r["seed"])] = r

    effects = []
    metrics = [
        ("event_deadline_success", 100.0),
        ("mean_aoi_ms", 1.0),
        ("useful_goodput_bps", 1e-3),
        ("maximum_age_violation_ratio", 100.0),
    ]
    for sc in SCENARIOS:
        ref_map = by_sc_arm.get((sc, ref_arm), {})
        for arm, _, _, _ in ARMS:
            if arm == ref_arm:
                continue
            cmp_map = by_sc_arm.get((sc, arm), {})
            common = sorted(set(ref_map) & set(cmp_map))
            if not common:
                continue
            for m, scale in metrics:
                diffs = []
                for sd in common:
                    v1, v2 = ref_map[sd][m], cmp_map[sd][m]
                    if v1 is not None and v2 is not None:
                        diffs.append((float(v1) - float(v2)) * scale)
                if len(diffs) < 2:
                    continue
                arr = np.array(diffs, dtype=float)
                mean_d = float(arr.mean())
                sd_d = float(arr.std(ddof=1))
                dz = mean_d / sd_d if sd_d > 1e-12 else (math.copysign(99.0, mean_d) if abs(mean_d) > 1e-12 else 0.0)
                idx = rng.integers(0, len(arr), size=(5000, len(arr)))
                ci_lo, ci_hi = np.percentile(arr[idx].mean(axis=1), [2.5, 97.5])
                signs = rng.choice([-1.0, 1.0], size=(20000, len(arr)))
                perm = np.abs((signs * arr).mean(axis=1))
                p_raw = float((1 + np.sum(perm >= abs(mean_d) - 1e-12)) / (20000 + 1))
                effects.append({
                    "scenario": sc,
                    "reference": ref_arm,
                    "comparator": arm,
                    "metric": m,
                    "n_seeds": len(arr),
                    "mean_diff": mean_d,
                    "ci_lo": float(ci_lo),
                    "ci_hi": float(ci_hi),
                    "cohens_dz": dz,
                    "p_raw": p_raw,
                })

    # Global Holm-Bonferroni per metric across all scenario x comparator pairs
    for m, _ in metrics:
        sub = [e for e in effects if e["metric"] == m]
        sub.sort(key=lambda x: x["p_raw"])
        m_cnt = len(sub)
        prev = 0.0
        for rank, item in enumerate(sub):
            adj = min(1.0, max(prev, item["p_raw"] * (m_cnt - rank)))
            item["p_holm"] = adj
            prev = adj
    return effects


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--suite", choices=("pilot", "formal"), default="pilot")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--build", type=Path, required=True)
    ap.add_argument("--jobs", type=int, default=4)
    args = ap.parse_args()

    seeds = [10001, 10002] if args.suite == "pilot" else list(range(10101, 10117))  # 16 held-out seeds = 576 runs
    args.out.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="stage10-xquic-") as tmp_str:
        tmp = Path(tmp_str)
        cert, key = tmp / "cert.pem", tmp / "key.pem"
        subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256",
             "-nodes", "-days", "1", "-subj", "/CN=iot-stage10", "-keyout", str(key), "-out", str(cert)],
            check=True, stderr=subprocess.DEVNULL,
        )

        # Pre-generate identical per-(scenario, seed) workloads
        workloads: dict[tuple[str, int], Path] = {}
        tasks = []
        rng = random.Random(102026)
        idx = 0
        for sc, sc_cfg in SCENARIOS.items():
            for sd in seeds:
                wf = tmp / f"wl-{sc}-{sd}.bin"
                if sc_cfg["workload"] == "stage10-burst":
                    build_stage10_burst_workload(wf, sd)
                else:
                    build_workload(wf, sc_cfg["workload"], sd)
                workloads[(sc, sd)] = wf
                arms_shuf = list(ARMS)
                rng.shuffle(arms_shuf)
                for arm_name, pol, path_pol, repair in arms_shuf:
                    idx += 1
                    case = {
                        "name": f"{sc}--{sd}--{arm_name}",
                        "scenario": sc,
                        "seed": sd,
                        "arm": arm_name,
                        "policy": pol,
                        "path_scheduler": path_pol,
                        "repair": repair,
                        "ablation": "full",
                        "workload": sc_cfg["workload"],
                        "initial": sc_cfg["initial"],
                        "phases": sc_cfg["phases"],
                        "horizon_us": sc_cfg["horizon_us"],
                        "recovery_k": 2,
                        "tuning": DEFAULT_TUNING,
                        "assignment_seed": random.Random(f"s10-assign/{sc}/{sd}").getrandbits(32),
                        "network_seed": random.Random(f"s10-net/{sc}/{sd}").getrandbits(31),
                        "worker_tag": f"{idx:04d}",
                    }
                    tasks.append((case, wf, tmp / f"run-{idx:04d}"))

        total = len(tasks)
        print(f"Stage 10 ({args.suite}): running {total} real XQUIC dual-path connections ({args.jobs} parallel)...", flush=True)
        t0 = time.time()
        rows: list[dict] = []

        def _exec(item):
            case, wf, wdir = item
            try:
                res = run_single_connection(case, wf, wdir, args.build, cert, key)
                return res
            finally:
                shutil.rmtree(wdir, ignore_errors=True)

        with ThreadPoolExecutor(max_workers=args.jobs) as pool:
            for done, r in enumerate(pool.map(_exec, tasks), 1):
                rows.append(r)
                if done % 18 == 0 or done == total:
                    print(f"  [{done}/{total}] elapsed={time.time()-t0:.1f}s last={r['scenario']}/{r['arm']} "
                          f"ev={r['event_deadline_success']*100:.1f}% aoi={r['mean_aoi_ms'] or 0:.0f}ms "
                          f"pathB={r['path_b_share']*100:.1f}%", flush=True)

    rows.sort(key=lambda r: (r["scenario"], r["seed"], [a[0] for a in ARMS].index(r["arm"])))
    with (args.out / "runs.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    # Summary table
    summary = []
    for sc in SCENARIOS:
        for arm, pol, path_pol, rep in ARMS:
            sub = [r for r in rows if r["scenario"] == sc and r["arm"] == arm]
            aois = [r["mean_aoi_ms"] for r in sub if r["mean_aoi_ms"] is not None]
            summary.append({
                "scenario": sc,
                "arm": arm,
                "policy": pol,
                "path_scheduler": path_pol,
                "repair": rep,
                "n_runs": len(sub),
                "event_deadline_success_pct": 100.0 * statistics.fmean(r["event_deadline_success"] for r in sub),
                "mean_aoi_ms": statistics.fmean(aois) if aois else None,
                "aoi_coverage_pct": 100.0 * statistics.fmean(r["aoi_coverage"] for r in sub),
                "max_age_violation_pct": 100.0 * statistics.fmean(r["maximum_age_violation_ratio"] for r in sub),
                "goodput_kbps": 1e-3 * statistics.fmean(r["useful_goodput_bps"] for r in sub),
                "path_b_share_pct": 100.0 * statistics.fmean(r["path_b_share"] for r in sub),
            })
    with (args.out / "summary.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(summary[0].keys()))
        w.writeheader()
        w.writerows(summary)

    effects = paired_stats(rows, ref_arm="fap_strict")
    if effects:
        with (args.out / "paired_effects.csv").open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(effects[0].keys()))
            w.writeheader()
            w.writerows(effects)

    manifest = {
        "suite": args.suite,
        "seeds": seeds,
        "n_runs": len(rows),
        "elapsed_s": round(time.time() - t0, 2),
        "scenarios": SCENARIOS,
        "arms": ARMS,
        "all_client_ok": all(r["client_ok"] == 1 for r in rows),
        "exe_sha256": hashlib.sha256((args.build / "app/iot-mpquic").read_bytes()).hexdigest(),
    }
    (args.out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")

    print("\n=== SUMMARY ===")
    for sc in SCENARIOS:
        print(f"\n[{sc}]")
        for s in [x for x in summary if x["scenario"] == sc]:
            print(f"  {s['arm']:14s} EvSucc={s['event_deadline_success_pct']:5.2f}%  "
                  f"AoI={s['mean_aoi_ms'] or 0:6.1f}ms  Goodput={s['goodput_kbps']:6.1f}kbps  "
                  f"PathB={s['path_b_share_pct']:5.1f}%")


if __name__ == "__main__":
    main()
