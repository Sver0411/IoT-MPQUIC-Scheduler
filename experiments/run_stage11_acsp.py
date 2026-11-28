#!/usr/bin/env python3
"""Stage 11: Real XQUIC Dual-Path Evaluation of ACSP-MPQUIC vs. Classical & Modern SOTA.

Evaluates:
  Suite 1 (--suite formal / smoke): 4 dynamic 4 s regimes (S1-S4) across 13 scheduling arms
    covering classical baselines (FIFO-MinRTT, Fixed-MinRTT, ECF, BLEST), modern external SOTA
    baselines (Peekaboo-UCB, DaMPS-EDF), Stage-9/10 baselines (Prob-Strict, FAP-Strict), and
    the full 4-pillar ACSP-MPQUIC ablation ladder (FGCA, +OCRQ, +JBM-SP, +RESET_STREAM Preemption,
    and +Cross-Path Repair).
  Suite 2 (--suite scaling):
    (a) Credit-Scaling Sweep (MAX_STREAMS_UNI C in {4, 8, 16}) on S1 & S3.
    (b) Long-Horizon 12 s Multi-Reversal + Blackout Wireless Trace (S5) across 8 held-out seeds.

All experiments run real Linux netns dual-veth topologies with tc/netem and real BoringSSL+XQUIC
binaries, extracting exact Stage-7/8 audited metrics (honest denominator over all released events).
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
    ("fixed_minrtt", "proposed-app", "fixed-minrtt", "strict"),
    ("ecf", "proposed-app", "earliest-completion", "strict"),
    ("blest", "proposed-app", "blest", "strict"),
    ("peekaboo_ucb", "proposed-app", "peekaboo-ucb", "strict"),
    ("damps_edf", "proposed-app", "damps-edf", "strict"),
    ("prob_strict", "proposed-app", "proposed-path", "strict"),
    ("fap_strict", "proposed-app", "fresh-aware", "strict"),
    ("acsp_fgca", "proposed-app", "acsp-fgca", "strict"),
    ("acsp_conformal", "proposed-app", "acsp-conformal", "strict"),
    ("acsp_joint", "proposed-app", "acsp-joint", "strict"),
    ("acsp_full", "proposed-app", "acsp-full", "strict"),
    ("acsp_cross", "proposed-app", "acsp-full", "cross"),
]

SCALING_ARMS = [
    ("ecf", "proposed-app", "earliest-completion", "strict"),
    ("blest", "proposed-app", "blest", "strict"),
    ("peekaboo_ucb", "proposed-app", "peekaboo-ucb", "strict"),
    ("damps_edf", "proposed-app", "damps-edf", "strict"),
    ("fap_strict", "proposed-app", "fresh-aware", "strict"),
    ("acsp_full", "proposed-app", "acsp-full", "strict"),
    ("acsp_cross", "proposed-app", "acsp-full", "cross"),
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

LONG_SCENARIO = {
    "S5_long_multi_reversal": {
        "initial": {"a": [5, 26, 0], "b": [26, 12, 0]},
        "phases": [
            {"at_us": 1500000, "links": {"a": [115, 0.9, 0, 12], "b": [6, 28, 0]}},
            {"at_us": 3800000, "links": {"a": [6, 28, 0], "b": [120, 0.8, 0, 15]}},
            {"at_us": 6200000, "side": "client", "links": {"a": [6, 28, 100]}},
            {"at_us": 7600000, "links": {"a": [50, 5, 0], "b": [5, 30, 0]}},
            {"at_us": 9600000, "links": {"a": [6, 28, 0], "b": [95, 1.2, 0, 10]}},
        ],
        "workload": "long-horizon",
        "horizon_us": 12000000,
    },
}


def build_long_horizon_workload(path: Path, seed: int) -> None:
    """12-second dynamic IoT workload: 4 periodic sensors + 16 alarm bursts (~360 records)."""
    rng = random.Random(seed)
    rows = []
    # 16 alarm bursts spanning 0.3s .. 11.4s (128 alarms total)
    for burst in range(16):
        at = 300_000 + burst * 700_000
        for j in range(8):
            release = at + rng.randrange(25_000)
            size = (256, 1024, 2400, 4200)[(burst + j) % 4]
            slack = (55_000, 90_000, 140_000, 220_000)[(j + burst) % 4]
            severity = rng.randint(1, 5)
            rows.append((
                release, 2, 1 + severity, severity, 1 + (j % 4), 100 * burst + j + 1,
                release, release + slack, 0, rng.randbytes(size),
            ))
    # 4 periodic sensors spanning 0.2s .. 11.6s (~230 state updates total)
    for sensor, period, age, payload_len in (
        (1, 145_000, 260_000, 64),
        (2, 185_000, 340_000, 192),
        (3, 240_000, 460_000, 640),
        (4, 310_000, 620_000, 1400),
    ):
        seq = 1
        t = 200_000 + sensor * 15_000
        while t < 11_600_000:
            release = t + rng.randrange(10_000)
            data = struct.pack("!hh", rng.randint(1700, 3600), rng.randint(2000, 9000))
            rows.append((
                release, 1, 1, 0, sensor, seq, release, release + age + 60_000, age,
                data + rng.randbytes(payload_len - len(data)),
            ))
            seq += 1
            t += period

    with path.open("xb") as output:
        for id_, row in enumerate(sorted(rows, key=lambda r: r[0]), 1):
            release, kind, priority, severity, source, serial, generated, deadline, age, data = row
            header = workload.RECORD.pack(
                b"IOT1", kind, priority, severity, 0, len(data), id_,
                source, serial, generated, deadline, age,
            )
            output.write(workload.PREFIX.pack(release, len(header) + len(data)) + header + data)


def run_single_connection(
    case: dict,
    inp_file: Path,
    work_dir: Path,
    build_dir: Path,
    cert: Path,
    key: Path,
    capture_pcap: bool = False,
) -> dict:
    """Run one real dual-veth XQUIC connection inside isolated Linux netns."""
    ns_tag = f"s11-{os.getpid()}-{case['worker_tag']}"
    c_ns = f"c-{ns_tag}"[:15]
    s_ns = f"s-{ns_tag}"[:15]
    work_dir.mkdir(parents=True, exist_ok=True)
    (work_dir / "case.json").write_text(json.dumps(case, indent=2) + "\n")
    shutil.copyfile(inp_file, work_dir / "workload.bin")

    exe = build_dir / "app/iot-mpquic"
    pcap_block = ""
    if capture_pcap:
        pcap_block = f"""
for link in a b; do
  ip netns exec "$server_ns" tcpdump -nn -i "iot-$link" -s 96 -U --immediate-mode -B 2048 -Z root \\
    -w "{work_dir}/link-$link.pcap" 'udp and port 4433 and port 4434' 2> "{work_dir}/tcpdump-$link.log" &
  captures+=("$!")
  wait_log "{work_dir}/tcpdump-$link.log" 'listening on' "$!"
done
"""
    timeout_s = max(45, int(case["horizon_us"] / 1_000_000) + 35)
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
    sleep 0.02
  done
}}
source "{ROOT}/network/topology/dual-veth.sh"
setup_topology
python3 "{ROOT}/network/scenarios/stage5.py" initial "{work_dir}" "$client_ns" "$server_ns"
{pcap_block}
ip netns exec "$server_ns" "{exe}" iot-server "{work_dir}/server.xquic.log" "{cert}" "{key}" \\
  > "{work_dir}/server.log" 2> "{work_dir}/server.stderr" &
server_pid=$!
wait_log "{work_dir}/server.log" 'event=ready role=server' "$server_pid"
extra_args=()
if [[ "{case['policy']}" == "proposed-app" ]]; then extra_args+=("{case['recovery_k']}"); fi
ip netns exec "$client_ns" "{exe}" iot-client "{work_dir}/client.xquic.log" "{work_dir}/workload.bin" \\
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
        "IOT_MAX_CREDITS": str(case.get("max_credits", 4)),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    env.pop("IOT_STAGE8_REPAIR", None)
    if case["repair"] == "cross":
        env["IOT_STAGE8_REPAIR"] = "cross"
    proc = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, timeout=timeout_s)
    if proc.returncode != 0:
        raise RuntimeError(f"Run failed {case['name']}: {proc.stderr[:500]}")

    metrics, *_ = extract(work_dir, case)
    client_text = (work_dir / "client.log").read_text()
    server_text = (work_dir / "server.log").read_text()
    a_b = metrics.get("path_a_udp_rx_bytes") or 0
    b_b = metrics.get("path_b_udp_rx_bytes") or 0
    tot_b = a_b + b_b
    shed_cnt = client_text.count("event=iot_shed ")
    reset_cnt = client_text.count("event=iot_reset_stream ")
    rx_reset_cnt = server_text.count("event=iot_rx_reset ")
    conf_cnt = client_text.count("event=iot_conformal_update ")

    return {
        "scenario": case["scenario"],
        "seed": case["seed"],
        "max_credits": case.get("max_credits", 4),
        "arm": case["arm"],
        "policy": case["policy"],
        "path_scheduler": case["path_scheduler"],
        "repair": case["repair"],
        "events_generated": metrics["events_generated"],
        "events_on_time": metrics["events_on_time"],
        "event_deadline_success": metrics["event_deadline_success"],
        "mean_aoi_ms": metrics["mean_aoi_ms"],
        "p95_aoi_ms": metrics.get("p95_aoi_ms"),
        "aoi_coverage": metrics["aoi_coverage"],
        "maximum_age_violation_ratio": metrics["maximum_age_violation_ratio"],
        "useful_goodput_bps": metrics["useful_goodput_bps"],
        "event_p50_ms": metrics.get("event_latency_p50_ms"),
        "event_p95_ms": metrics.get("event_latency_p95_ms"),
        "path_a_share": (a_b / tot_b) if tot_b > 0 else 0.0,
        "path_b_share": (b_b / tot_b) if tot_b > 0 else 0.0,
        "retrans_ratio": metrics.get("retransmission_ratio"),
        "shed_events": shed_cnt,
        "reset_streams": reset_cnt,
        "server_rx_resets": rx_reset_cnt,
        "conformal_updates": conf_cnt,
        "client_ok": int("event=exit ok=1" in client_text),
        "server_ok": int("event=exit ok=1" in server_text),
    }


def paired_stats(rows: list[dict], scenarios: list[str], arms: list[str], ref_arm: str = "acsp_full") -> list[dict]:
    import numpy as np

    rng = np.random.default_rng(20261128)
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
    for sc in scenarios:
        ref_map = by_sc_arm.get((sc, ref_arm), {})
        for arm in arms:
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


def summarize(rows: list[dict], group_keys: list[str]) -> list[dict]:
    groups: dict[tuple, list[dict]] = {}
    order: list[tuple] = []
    for r in rows:
        k = tuple(r[g] for g in group_keys)
        if k not in groups:
            groups[k] = []
            order.append(k)
        groups[k].append(r)

    summary = []
    for k in order:
        sub = groups[k]
        aois = [r["mean_aoi_ms"] for r in sub if r["mean_aoi_ms"] is not None]
        p95s = [r["p95_aoi_ms"] for r in sub if r["p95_aoi_ms"] is not None]
        rec = {g: val for g, val in zip(group_keys, k)}
        rec.update({
            "n_runs": len(sub),
            "event_deadline_success_pct": 100.0 * statistics.fmean(r["event_deadline_success"] for r in sub),
            "event_deadline_sd_pct": (100.0 * statistics.stdev(r["event_deadline_success"] for r in sub)) if len(sub) > 1 else 0.0,
            "mean_aoi_ms": statistics.fmean(aois) if aois else None,
            "mean_aoi_sd_ms": statistics.stdev(aois) if len(aois) > 1 else 0.0,
            "p95_aoi_ms": statistics.fmean(p95s) if p95s else None,
            "aoi_coverage_pct": 100.0 * statistics.fmean(r["aoi_coverage"] for r in sub),
            "max_age_violation_pct": 100.0 * statistics.fmean(r["maximum_age_violation_ratio"] for r in sub),
            "goodput_kbps": 1e-3 * statistics.fmean(r["useful_goodput_bps"] for r in sub),
            "path_b_share_pct": 100.0 * statistics.fmean(r["path_b_share"] for r in sub),
            "mean_shed_events": statistics.fmean(r["shed_events"] for r in sub),
            "mean_reset_streams": statistics.fmean(r["reset_streams"] for r in sub),
            "all_client_ok": int(all(r["client_ok"] == 1 for r in sub)),
            "all_ok": int(all(r["client_ok"] == 1 and r["server_ok"] == 1 for r in sub)),
        })
        summary.append(rec)
    return summary


def write_csv_rows(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


def generate_figures(out_dir: Path) -> None:
    """Generate publication-grade PNG + PDF charts from Stage 11 audited CSVs."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    fig_dir = out_dir / "figures"
    fig_dir.mkdir(parents=True, exist_ok=True)
    plt.rcParams.update({
        "font.size": 10,
        "axes.titlesize": 11,
        "axes.labelsize": 10,
        "legend.fontsize": 8.5,
        "axes.grid": True,
        "grid.alpha": 0.25,
    })

    summary_path = out_dir / "summary.csv"
    if summary_path.exists():
        with summary_path.open() as f:
            srows = list(csv.DictReader(f))
        scenarios = ["S1_outage_reversal", "S2_asymmetric_reversal", "S3_double_reversal", "S4_mid_blackout"]
        sc_labels = ["S1 Outage\n+Reversal", "S2 Asym.\nReversal", "S3 Double\nReversal", "S4 Mid-Run\nBlackout"]
        show_arms = [
            ("fifo_minrtt", "FIFO-MinRTT", "#94a3b8"),
            ("ecf", "ECF", "#64748b"),
            ("blest", "BLEST", "#0284c7"),
            ("peekaboo_ucb", "Peekaboo-UCB", "#7c3aed"),
            ("damps_edf", "DaMPS-EDF", "#d97706"),
            ("fap_strict", "FAP-MPQUIC", "#059669"),
            ("acsp_conformal", "ACSP-OCRQ (Ours)", "#f97316"),
            ("acsp_full", "ACSP-Full (Ours)", "#dc2626"),
        ]
        lookup = {(r["scenario"], r["arm"]): r for r in srows}

        # Fig 1: Main SOTA comparison (EvSucc % and Mean AoI ms)
        fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(13.2, 4.8), dpi=220)
        x = np.arange(len(scenarios))
        width = 0.102
        for idx, (arm, label, color) in enumerate(show_arms):
            offset = (idx - (len(show_arms) - 1) / 2.0) * width
            evs = [float(lookup[(sc, arm)]["event_deadline_success_pct"]) for sc in scenarios]
            aois = [float(lookup[(sc, arm)]["mean_aoi_ms"]) for sc in scenarios]
            edge = "#111827" if arm.startswith("acsp_") else "none"
            lw = 0.9 if arm.startswith("acsp_") else 0.0
            ax1.bar(x + offset, evs, width, label=label, color=color, edgecolor=edge, linewidth=lw)
            ax2.bar(x + offset, aois, width, label=label, color=color, edgecolor=edge, linewidth=lw)
        ax1.set_xticks(x)
        ax1.set_xticklabels(sc_labels)
        ax1.set_ylabel("Event Deadline Success (%)  ↑")
        ax1.set_title("(a) Alarm Timely Delivery Across Dynamic Regimes (n=16 held-out seeds)")
        ax1.set_ylim(0, 108)
        ax2.set_xticks(x)
        ax2.set_xticklabels(sc_labels)
        ax2.set_ylabel("Mean Age of Information (ms)  ↓")
        ax2.set_title("(b) Sensor Freshness (Mean AoI) Across Dynamic Regimes (n=16 held-out seeds)")
        handles, labels = ax1.get_legend_handles_labels()
        fig.legend(handles, labels, loc="upper center", ncol=4, frameon=False, bbox_to_anchor=(0.5, 1.05))
        fig.tight_layout(rect=[0, 0, 1, 0.92])
        fig.savefig(fig_dir / "fig1_sota_comparison.png", dpi=220, bbox_inches="tight")
        fig.savefig(fig_dir / "fig1_sota_comparison.pdf", bbox_inches="tight")
        plt.close(fig)

        # Fig 2: 4-Pillar Ablation Ladder (EvSucc & AoI Coverage / Mean AoI Pareto Shift)
        ablation_arms = [
            ("fap_strict", "FAP Base"),
            ("acsp_fgca", "+P1 FGCA"),
            ("acsp_conformal", "+P2 OCRQ"),
            ("acsp_joint", "+P3 JBM-SP"),
            ("acsp_full", "+P4 RESET"),
            ("acsp_cross", "+Cross"),
        ]
        fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12.2, 4.4), dpi=220)
        for sc, sc_lbl in zip(scenarios, ["S1 Outage+Rev", "S2 Asym Rev", "S3 Double Rev", "S4 Mid Blackout"]):
            evs = [float(lookup[(sc, a)]["event_deadline_success_pct"]) for a, _ in ablation_arms]
            aois = [float(lookup[(sc, a)]["mean_aoi_ms"]) for a, _ in ablation_arms]
            ax1.plot(range(len(ablation_arms)), evs, marker="o", linewidth=2.1, label=sc_lbl)
            ax2.plot(range(len(ablation_arms)), aois, marker="s", linewidth=2.1, label=sc_lbl)
        ax1.set_xticks(range(len(ablation_arms)))
        ax1.set_xticklabels([lbl for _, lbl in ablation_arms], rotation=15)
        ax1.set_ylabel("Event Deadline Success (%)  ↑")
        ax1.set_title("(a) 4-Pillar Ablation: Timely Alarm Success")
        ax1.legend(frameon=False)
        ax2.set_xticks(range(len(ablation_arms)))
        ax2.set_xticklabels([lbl for _, lbl in ablation_arms], rotation=15)
        ax2.set_ylabel("Mean Age of Information (ms)  ↓")
        ax2.set_title("(b) 4-Pillar Ablation: Sensor Mean AoI")
        ax2.legend(frameon=False)
        fig.tight_layout()
        fig.savefig(fig_dir / "fig2_ablation_ladder.png", dpi=220, bbox_inches="tight")
        fig.savefig(fig_dir / "fig2_ablation_ladder.pdf", bbox_inches="tight")
        plt.close(fig)

    # Fig 3: Credit Scaling Sweep (C=4,8,16) + Long-Horizon 12s S5 Mean AoI Collapse
    credit_path = out_dir / "credit_sweep_summary.csv"
    long_path = out_dir / "long_horizon_summary.csv"
    if credit_path.exists() and long_path.exists():
        with credit_path.open() as f:
            crows = list(csv.DictReader(f))
        with long_path.open() as f:
            lrows = list(csv.DictReader(f))
        fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12.6, 4.5), dpi=220)
        palette = {
            "ecf": ("ECF", "#64748b", "--"),
            "blest": ("BLEST", "#0284c7", "--"),
            "peekaboo_ucb": ("Peekaboo-UCB", "#7c3aed", "-."),
            "damps_edf": ("DaMPS-EDF", "#d97706", "-."),
            "fap_strict": ("FAP-MPQUIC", "#059669", "-"),
            "acsp_full": ("ACSP-Full (Ours)", "#dc2626", "-"),
            "acsp_cross": ("ACSP-Cross (Ours)", "#991b1b", ":"),
        }
        credits = [4, 8, 16]
        for arm, (lbl, col, ls) in palette.items():
            vals = []
            for c in credits:
                sub = [float(r["event_deadline_success_pct"]) for r in crows if r["arm"] == arm and int(r["max_credits"]) == c]
                vals.append(statistics.fmean(sub) if sub else 0.0)
            lw = 2.6 if arm.startswith("acsp_") else 1.7
            ax1.plot(credits, vals, marker="o", linestyle=ls, color=col, linewidth=lw, label=lbl)
        ax1.set_xticks(credits)
        ax1.set_xlabel("QUIC MAX_STREAMS_UNI Credit Budget (C)")
        ax1.set_ylabel("Event Deadline Success (%)  ↑")
        ax1.set_title("(a) Credit-Scaling Sensitivity (Mean across S1 & S3, n=8 seeds)")
        ax1.set_ylim(0, 105)
        ax1.legend(frameon=False, ncol=2, loc="upper left")

        l_arms = [a for a in ("ecf", "blest", "damps_edf", "fap_strict", "peekaboo_ucb", "acsp_cross", "acsp_full") if any(r["arm"] == a for r in lrows)]
        l_map = {r["arm"]: r for r in lrows}
        lx = np.arange(len(l_arms))
        aois = [float(l_map[a]["mean_aoi_ms"]) for a in l_arms]
        viols = [float(l_map[a]["max_age_violation_pct"]) for a in l_arms]
        cols = [palette[a][1] for a in l_arms]
        bars = ax2.bar(lx, aois, width=0.58, color=cols)
        ax2.set_xticks(lx)
        ax2.set_xticklabels([palette[a][0].replace(" (Ours)", "") for a in l_arms], rotation=18)
        ax2.set_ylabel("Mean Age of Information (ms)  ↓")
        ax2.set_title("(b) 12 s Long-Horizon Multi-Reversal + Blackout (S5, C=4)")
        ax2.set_ylim(0, max(aois) * 1.18)
        for bar, viol in zip(bars, viols):
            ax2.text(bar.get_x() + bar.get_width() / 2.0, bar.get_height() + 55,
                     f"{bar.get_height():.0f} ms\n(Viol {viol:.1f}%)", ha="center", va="bottom", fontsize=7.5)
        fig.tight_layout()
        fig.savefig(fig_dir / "fig3_credit_and_long_horizon.png", dpi=220, bbox_inches="tight")
        fig.savefig(fig_dir / "fig3_credit_and_long_horizon.pdf", bbox_inches="tight")
        plt.close(fig)


def run_batch(tasks: list[tuple[dict, Path, Path]], build_dir: Path, cert: Path, key: Path, jobs: int, label: str) -> list[dict]:
    total = len(tasks)
    print(f"{label}: running {total} real XQUIC dual-path connections ({jobs} parallel)...", flush=True)
    t0 = time.time()
    rows: list[dict] = []

    def _exec(item):
        case, wf, wdir = item
        try:
            return run_single_connection(case, wf, wdir, build_dir, cert, key, capture_pcap=False)
        finally:
            shutil.rmtree(wdir, ignore_errors=True)

    with ThreadPoolExecutor(max_workers=jobs) as pool:
        for done, r in enumerate(pool.map(_exec, tasks), 1):
            rows.append(r)
            if done % max(1, min(20, total // 10 or 1)) == 0 or done == total:
                print(
                    f"  [{done:3d}/{total}] t={time.time()-t0:5.1f}s "
                    f"{r['scenario']}/C={r['max_credits']}/{r['arm']} "
                    f"Ev={r['event_deadline_success']*100:5.1f}% AoI={r['mean_aoi_ms'] or 0:5.0f}ms "
                    f"shed={r['shed_events']} rst={r['reset_streams']} ok={r['client_ok']}/{r['server_ok']}",
                    flush=True,
                )
    return rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--suite", choices=("smoke", "formal", "scaling", "long", "figures"), default="smoke")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--build", type=Path, required=True)
    ap.add_argument("--jobs", type=int, default=6)
    args = ap.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    if args.suite == "figures":
        generate_figures(args.out)
        print("Figures generated.")
        return

    with tempfile.TemporaryDirectory(prefix="stage11-xquic-") as tmp_str:
        tmp = Path(tmp_str)
        cert, key = tmp / "cert.pem", tmp / "key.pem"
        subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256",
             "-nodes", "-days", "1", "-subj", "/CN=iot-stage11", "-keyout", str(key), "-out", str(cert)],
            check=True, stderr=subprocess.DEVNULL,
        )

        if args.suite in ("smoke", "formal"):
            seeds = [10001, 10002] if args.suite == "smoke" else list(range(10101, 10117))
            arms = ARMS
            tasks = []
            rng = random.Random(112026)
            idx = 0
            for sc, sc_cfg in SCENARIOS.items():
                for sd in seeds:
                    wf = tmp / f"wl-{sc}-{sd}.bin"
                    build_workload(wf, sc_cfg["workload"], sd)
                    arms_shuf = list(arms)
                    rng.shuffle(arms_shuf)
                    for arm_name, pol, path_pol, repair in arms_shuf:
                        idx += 1
                        case = {
                            "name": f"{sc}--{sd}--{arm_name}",
                            "scenario": sc,
                            "seed": sd,
                            "max_credits": 4,
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
                            "assignment_seed": random.Random(f"s11-assign/{sc}/{sd}").getrandbits(32),
                            "network_seed": random.Random(f"s11-net/{sc}/{sd}").getrandbits(31),
                            "worker_tag": f"{idx:04d}",
                        }
                        tasks.append((case, wf, tmp / f"run-{idx:04d}"))

            t0 = time.time()
            rows = run_batch(tasks, args.build, cert, key, args.jobs, f"Stage 11 ({args.suite})")
            arm_order = [a[0] for a in arms]
            rows.sort(key=lambda r: (r["scenario"], r["seed"], arm_order.index(r["arm"])))
            write_csv_rows(args.out / "runs.csv", rows)

            summary = summarize(rows, ["scenario", "arm", "policy", "path_scheduler", "repair"])
            write_csv_rows(args.out / "summary.csv", summary)

            effects = paired_stats(rows, list(SCENARIOS.keys()), arm_order, ref_arm="acsp_full")
            write_csv_rows(args.out / "paired_effects.csv", effects)

            manifest = {
                "suite": args.suite,
                "seeds": seeds,
                "n_runs": len(rows),
                "elapsed_s": round(time.time() - t0, 2),
                "all_client_ok": all(r["client_ok"] == 1 for r in rows),
                "all_ok": all(r["client_ok"] == 1 and r["server_ok"] == 1 for r in rows),
                "exe_sha256": hashlib.sha256((args.build / "app/iot-mpquic").read_bytes()).hexdigest(),
            }
            (args.out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")

            print("\n=== STAGE 11 SUMMARY ===")
            for sc in SCENARIOS:
                print(f"\n[{sc}]")
                for s in [x for x in summary if x["scenario"] == sc]:
                    print(
                        f"  {s['arm']:15s} EvSucc={s['event_deadline_success_pct']:5.2f}%  "
                        f"AoI={s['mean_aoi_ms'] or 0:6.1f}ms  MaxAgeV={s['max_age_violation_pct']:5.1f}%  "
                        f"Gp={s['goodput_kbps']:6.1f}kbps  Shed={s['mean_shed_events']:4.1f}  "
                        f"Rst={s['mean_reset_streams']:3.1f}  OK={s['all_ok']}"
                    )

        elif args.suite in ("scaling", "long"):
            c_seeds = list(range(10201, 10209))
            arm_order = [a[0] for a in SCALING_ARMS]
            idx = 0
            c_sum = []
            if args.suite == "scaling":
                # Part A: Credit sweep C in {4, 8, 16} across S1 & S3 (8 held-out seeds 10201-10208)
                c_scenarios = ["S1_outage_reversal", "S3_double_reversal"]
                tasks = []
                rng = random.Random(112027)
                for sc in c_scenarios:
                    sc_cfg = SCENARIOS[sc]
                    for sd in c_seeds:
                        wf = tmp / f"wl-c-{sc}-{sd}.bin"
                        build_workload(wf, sc_cfg["workload"], sd)
                        for cred in (4, 8, 16):
                            arms_shuf = list(SCALING_ARMS)
                            rng.shuffle(arms_shuf)
                            for arm_name, pol, path_pol, repair in arms_shuf:
                                idx += 1
                                case = {
                                    "name": f"C{cred}--{sc}--{sd}--{arm_name}",
                                    "scenario": sc,
                                    "seed": sd,
                                    "max_credits": cred,
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
                                    "assignment_seed": random.Random(f"s11-c/{sc}/{sd}/{cred}").getrandbits(32),
                                    "network_seed": random.Random(f"s11-cnet/{sc}/{sd}").getrandbits(31),
                                    "worker_tag": f"c{idx:04d}",
                                }
                                tasks.append((case, wf, tmp / f"crun-{idx:04d}"))

                c_rows = run_batch(tasks, args.build, cert, key, args.jobs, "Stage 11 Credit Sweep (C=4,8,16)")
                c_rows.sort(key=lambda r: (r["scenario"], r["max_credits"], r["seed"], arm_order.index(r["arm"])))
                write_csv_rows(args.out / "credit_sweep_runs.csv", c_rows)
                c_sum = summarize(c_rows, ["scenario", "max_credits", "arm"])
                write_csv_rows(args.out / "credit_sweep_summary.csv", c_sum)

            # Part B: Long-horizon 12 s S5 (8 held-out seeds 10201-10208)
            rng = random.Random(112028)
            l_tasks = []
            sc = "S5_long_multi_reversal"
            sc_cfg = LONG_SCENARIO[sc]
            for sd in c_seeds:
                wf = tmp / f"wl-long-{sd}.bin"
                build_long_horizon_workload(wf, sd)
                arms_shuf = list(SCALING_ARMS)
                rng.shuffle(arms_shuf)
                for arm_name, pol, path_pol, repair in arms_shuf:
                    idx += 1
                    case = {
                        "name": f"{sc}--{sd}--{arm_name}",
                        "scenario": sc,
                        "seed": sd,
                        "max_credits": 4,
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
                        "assignment_seed": random.Random(f"s11-long/{sd}").getrandbits(32),
                        "network_seed": random.Random(f"s11-lnet/{sd}").getrandbits(31),
                        "worker_tag": f"l{idx:04d}",
                    }
                    l_tasks.append((case, wf, tmp / f"lrun-{idx:04d}"))

            l_rows = run_batch(l_tasks, args.build, cert, key, args.jobs, "Stage 11 Long-Horizon 12s (S5)")
            l_rows.sort(key=lambda r: (r["seed"], arm_order.index(r["arm"])))
            write_csv_rows(args.out / "long_horizon_runs.csv", l_rows)
            l_sum = summarize(l_rows, ["scenario", "arm"])
            write_csv_rows(args.out / "long_horizon_summary.csv", l_sum)
            l_eff = paired_stats(l_rows, [sc], arm_order, ref_arm="acsp_full")
            write_csv_rows(args.out / "long_horizon_effects.csv", l_eff)

            print("\n=== CREDIT SWEEP SUMMARY ===")
            for s in c_sum:
                print(
                    f"  {s['scenario']:20s} C={s['max_credits']:2d} {s['arm']:13s} "
                    f"EvSucc={s['event_deadline_success_pct']:5.2f}% AoI={s['mean_aoi_ms'] or 0:6.1f}ms OK={s['all_ok']}"
                )
            print("\n=== LONG-HORIZON 12s (S5) SUMMARY ===")
            for s in l_sum:
                print(
                    f"  {s['arm']:13s} EvSucc={s['event_deadline_success_pct']:5.2f}% "
                    f"AoI={s['mean_aoi_ms'] or 0:6.1f}ms MaxAgeV={s['max_age_violation_pct']:5.1f}% "
                    f"Shed={s['mean_shed_events']:4.1f} Rst={s['mean_reset_streams']:3.1f} OK={s['all_ok']}"
                )

    generate_figures(args.out)


if __name__ == "__main__":
    main()
