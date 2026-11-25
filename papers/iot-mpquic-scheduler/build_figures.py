#!/usr/bin/env python3
"""Generate native 7.16-in IEEE print-ready vector PDF (+PNG) figures and Graphical Abstract for IoT-MPQUIC-Scheduler."""
from collections import defaultdict
import csv
import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch
import numpy as np

ROOT = Path(__file__).resolve().parents[2]
RES10 = ROOT / "results/stage10"
RES8 = ROOT / "results/stage8"
FIG_DIR = Path(__file__).resolve().parent / "figures"
FIG_DIR.mkdir(parents=True, exist_ok=True)
OUT_PNG = RES10 / "figures"
OUT_PNG.mkdir(parents=True, exist_ok=True)

IEEE_W = 7.16

plt.rcParams.update({
    "font.family": "serif",
    "font.serif": ["DejaVu Serif", "Times New Roman", "Times"],
    "mathtext.fontset": "stix",
    "font.size": 8.0,
    "axes.titlesize": 8.1,
    "axes.labelsize": 8.0,
    "xtick.labelsize": 7.2,
    "ytick.labelsize": 7.2,
    "legend.fontsize": 6.8,
    "axes.spines.top": False,
    "axes.spines.right": False,
    "axes.linewidth": 0.65,
    "grid.alpha": 0.22,
    "grid.linewidth": 0.45,
    "pdf.fonttype": 42,
    "ps.fonttype": 42,
})

ARMS_SHOW = [
    ("fifo_minrtt",  "FIFO+MinRTT",   "#666666", "*"),
    ("app_minrtt",   "App+MinRTT",    "#8855aa", "X"),
    ("fixed_minrtt", "Fixed-MinRTT",  "#cc7722", "v"),
    ("ecf",          "ECF",           "#cc3311", "^"),
    ("blest",        "BLEST",         "#bb8833", "h"),
    ("prob_strict",  "Prob (Stage8)", "#228833", "s"),
    ("fap_noprobe",  "FAP-NoProbe",   "#6699cc", "P"),
    ("fap_strict",   "FAP-MPQUIC",    "#004488", "o"),
    ("fap_cross",    "FAP-Cross",     "#aa3377", "D"),
]


def _load_csv(p: Path) -> list[dict]:
    with p.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _save_both(fig, stem: str) -> None:
    fig.savefig(FIG_DIR / f"{stem}.pdf")
    fig.savefig(OUT_PNG / f"{stem}.png", dpi=200)
    plt.close(fig)


def _seed_ci(runs: list[dict], sc: str, arm: str, col: str, scale: float = 1.0):
    rng = np.random.default_rng(20261126)
    vals = np.array([float(r[col]) * scale for r in runs if r["scenario"] == sc and r["arm"] == arm and r[col] not in ("", None)], dtype=float)
    m = float(vals.mean())
    idx = rng.integers(0, len(vals), size=(3000, len(vals)))
    lo, hi = np.percentile(vals[idx].mean(axis=1), [2.5, 97.5])
    return m, max(0.0, m - float(lo)), max(0.0, float(hi) - m)


def build_fig02_event_and_aoi(runs: list[dict]) -> None:
    """Fig 2: 2x2 paired Event Deadline Success & Mean State AoI across S1-S4 (16 held-out seeds)."""
    fig, axes = plt.subplots(2, 2, figsize=(IEEE_W, 4.15))
    scenarios = [
        ("S1_outage_reversal", "(a) S1: Outage + Reversal (High Load)"),
        ("S4_mid_blackout", "(b) S4: Mid-Run Blackout (Mixed Load)"),
        ("S2_asymmetric_reversal", "(c) S2: Asymmetric Reversal (No Outage)"),
        ("S3_double_reversal", "(d) S3: Double Reversal + Jitter"),
    ]
    arms_bar = ["fifo_minrtt", "app_minrtt", "fixed_minrtt", "ecf", "blest", "prob_strict", "fap_noprobe", "fap_strict", "fap_cross"]
    arm_meta = {a[0]: a for a in ARMS_SHOW}
    x = np.arange(len(arms_bar))

    # Top row: Event Deadline Success (%) on S1 & S4 (primary deadline discrimination)
    for ax, (sc, title) in zip(axes[0], scenarios[:2]):
        ms, los, his, cols = [], [], [], []
        for a in arms_bar:
            m, lo, hi = _seed_ci(runs, sc, a, "event_deadline_success", 100.0)
            ms.append(m); los.append(lo); his.append(hi); cols.append(arm_meta[a][2])
        bars = ax.bar(x, ms, yerr=[los, his], capsize=2.0, width=0.62, color=cols, edgecolor="#222222", lw=0.5,
                      error_kw=dict(elinewidth=0.7, ecolor="#222222"))
        bars[7].set_linewidth(1.2)
        for xi, m, hi in zip(x, ms, his):
            ax.text(xi, m + hi + (1.2 if sc == "S1_outage_reversal" else 2.2), f"{m:.1f}", ha="center", va="bottom",
                    fontsize=5.8, fontweight="bold" if xi == 7 else "normal", color="#004488" if xi == 7 else "#222222")
        ax.set(title=title, ylabel="Event Deadline Success (%) ↑", xticks=x)
        ax.set_xticklabels([arm_meta[a][1] for a in arms_bar], rotation=26, ha="right", fontsize=6.4)
        ax.set_ylim(0, 34 if sc == "S1_outage_reversal" else 112)
        ax.grid(axis="y")

    # Bottom row: State Mean AoI (ms) on S2 & S3 (freshness & reversal tracking)
    for ax, (sc, title) in zip(axes[1], scenarios[2:]):
        ms, los, his, cols = [], [], [], []
        for a in arms_bar:
            m, lo, hi = _seed_ci(runs, sc, a, "mean_aoi_ms", 1.0)
            ms.append(m); los.append(lo); his.append(hi); cols.append(arm_meta[a][2])
        bars = ax.bar(x, ms, yerr=[los, his], capsize=2.0, width=0.62, color=cols, edgecolor="#222222", lw=0.5,
                      error_kw=dict(elinewidth=0.7, ecolor="#222222"))
        bars[7].set_linewidth(1.2)
        for xi, m, hi in zip(x, ms, his):
            ax.text(xi, m + hi + 28, f"{m:.0f}", ha="center", va="bottom",
                    fontsize=5.7, fontweight="bold" if xi == 7 else "normal", color="#004488" if xi == 7 else "#222222")
        ax.set(title=title, ylabel="Observed Mean AoI (ms) ↓", xticks=x)
        ax.set_xticklabels([arm_meta[a][1] for a in arms_bar], rotation=26, ha="right", fontsize=6.4)
        ax.set_ylim(0, max(ms) * 1.18)
        ax.grid(axis="y")

    fig.subplots_adjust(left=0.085, right=0.99, bottom=0.14, top=0.93, wspace=0.22, hspace=0.46)
    _save_both(fig, "fig02_stage10_main")


def build_fig03_path_and_pareto(runs: list[dict]) -> None:
    """Fig 3: (a) S4 Mid-Run Blackout Pareto (AoI vs Event Success); (b) Path-B Adaptation Across S1-S4."""
    fig, axes = plt.subplots(1, 2, figsize=(IEEE_W, 2.55), gridspec_kw={"width_ratios": [1.05, 1.15]})
    arm_meta = {a[0]: a for a in ARMS_SHOW}

    # Panel (a): S4 Pareto with non-overlapping clean annotations
    ann_cfg = {
        "fifo_minrtt":  ((-52, 8), False),
        "app_minrtt":   ((8, -3), False),
        "fixed_minrtt": ((10, -2), False),
        "ecf":          ((-26, 7), False),
        "blest":        ((-40, -10), False),
        "prob_strict":  ((32, 10), True),
        "fap_noprobe":  ((44, 0), True),
        "fap_cross":    ((44, -11), True),
        "fap_strict":   ((28, -22), True),
    }
    for arm, lbl, col, mk in ARMS_SHOW:
        aoi_m, _, _ = _seed_ci(runs, "S4_mid_blackout", arm, "mean_aoi_ms", 1.0)
        ev_m, _, _ = _seed_ci(runs, "S4_mid_blackout", arm, "event_deadline_success", 100.0)
        axes[0].scatter(aoi_m, ev_m, color=col, marker=mk, s=58 if arm == "fap_strict" else 34,
                        edgecolors="#111111" if arm == "fap_strict" else "none", linewidths=0.8, zorder=7 if arm == "fap_strict" else 4)
        (dx, dy), use_arrow = ann_cfg[arm]
        kw = dict(arrowprops=dict(arrowstyle="-", color="#777777", lw=0.5, shrinkA=0, shrinkB=3)) if use_arrow else {}
        axes[0].annotate(lbl, (aoi_m, ev_m), xytext=(dx, dy), textcoords="offset points",
                         fontsize=6.0, fontweight="bold" if arm == "fap_strict" else "normal",
                         color="#004488" if arm == "fap_strict" else "#222222", **kw)
    axes[0].set(title="(a) S4 Mid-Run Blackout Pareto (Upper-Left Better)",
                xlabel="Observed Mean State AoI (ms) ↓", ylabel="Event Deadline Success (%) ↑",
                xlim=(820, 1870), ylim=(27, 99))
    axes[0].grid(True)

    # Panel (b): Path-B UDP Utilization (%) across S1-S4 for key schedulers
    scs = ["S1_outage_reversal", "S2_asymmetric_reversal", "S3_double_reversal", "S4_mid_blackout"]
    sc_lbls = ["S1\nOutage+Rev", "S2\nAsym Rev", "S3\nDouble Rev", "S4\nMid Blackout"]
    sel = ["fixed_minrtt", "ecf", "blest", "prob_strict", "fap_strict"]
    xb = np.arange(len(scs))
    w = 0.155
    for idx_a, arm in enumerate(sel):
        _, lbl, col, _ = arm_meta[arm]
        ys = [_seed_ci(runs, sc, arm, "path_b_share", 100.0)[0] for sc in scs]
        axes[1].bar(xb + (idx_a - 2) * w, ys, width=w, color=col, edgecolor="#222222", lw=0.45, label=lbl)
    axes[1].set(title="(b) Backup Path-B Traffic Shift Across Reversals", xticks=xb, xticklabels=sc_lbls,
                ylabel="Path-B UDP Share (%)", ylim=(0, 112))
    axes[1].grid(axis="y")
    axes[1].legend(fontsize=6.1, loc="upper center", ncol=3, frameon=True, framealpha=0.92, borderpad=0.22, columnspacing=0.7)

    fig.subplots_adjust(left=0.08, right=0.99, bottom=0.20, top=0.88, wspace=0.26)
    _save_both(fig, "fig03_pareto_and_path_shift")


def build_fig04_paired_effects(effects: list[dict]) -> None:
    """Fig 4: Paired bootstrap 95% CIs (FAP-MPQUIC minus comparators) on Event Success & AoI."""
    fig, axes = plt.subplots(1, 2, figsize=(IEEE_W, 2.65))
    comps = [("fifo_minrtt", "FIFO+MinRTT"), ("app_minrtt", "App+MinRTT"),
             ("fixed_minrtt", "Fixed-MinRTT"), ("ecf", "ECF"), ("blest", "BLEST"),
             ("prob_strict", "Prob (Stg8)"), ("fap_noprobe", "FAP-NoProbe")]

    # Panel (a): Event Deadline Success Delta (pp) on S1 and S4
    y = np.arange(len(comps))
    for off, sc, col, lbl, mk in [(-0.16, "S1_outage_reversal", "#004488", "S1: Outage+Rev", "o"),
                                   (0.16, "S4_mid_blackout", "#cc3311", "S4: Mid-Blackout", "s")]:
        ms, los, his = [], [], []
        for c, _ in comps:
            r = next(x for x in effects if x["scenario"] == sc and x["comparator"] == c and x["metric"] == "event_deadline_success")
            m = float(r["mean_diff"]); lo = m - float(r["ci_lo"]); hi = float(r["ci_hi"]) - m
            ms.append(m); los.append(max(0, lo)); his.append(max(0, hi))
        axes[0].errorbar(ms, y + off, xerr=[los, his], fmt=mk, color=col, capsize=2.2, ms=4.2, elinewidth=1.0, label=lbl)
    axes[0].axvline(0.0, color="#222222", ls="--", lw=0.7)
    axes[0].set(title="(a) Event Success Gain: FAP − Comparator", xlabel=r"$\Delta$ Event Deadline Success (pp, >0 better)",
                yticks=y, yticklabels=[c[1] for c in comps])
    axes[0].invert_yaxis(); axes[0].grid(axis="x")
    axes[0].legend(fontsize=6.3, loc="lower right", frameon=True)

    # Panel (b): Mean AoI Delta (ms) across S1, S3, S4 (<0 better)
    for off, sc, col, lbl, mk in [(-0.20, "S1_outage_reversal", "#004488", "S1: Outage+Rev", "o"),
                                   (0.0, "S3_double_reversal", "#228833", "S3: Double Rev", "^"),
                                   (0.20, "S4_mid_blackout", "#cc3311", "S4: Mid-Blackout", "s")]:
        ms, los, his = [], [], []
        for c, _ in comps:
            r = next(x for x in effects if x["scenario"] == sc and x["comparator"] == c and x["metric"] == "mean_aoi_ms")
            m = float(r["mean_diff"]); lo = m - float(r["ci_lo"]); hi = float(r["ci_hi"]) - m
            ms.append(m); los.append(max(0, lo)); his.append(max(0, hi))
        axes[1].errorbar(ms, y + off, xerr=[los, his], fmt=mk, color=col, capsize=2.0, ms=4.0, elinewidth=0.95, label=lbl)
    axes[1].axvline(0.0, color="#222222", ls="--", lw=0.7)
    axes[1].set(title="(b) State AoI Reduction: FAP − Comparator", xlabel=r"$\Delta$ Observed Mean AoI (ms, <0 better)",
                yticks=y, yticklabels=[""] * len(comps))
    axes[1].invert_yaxis(); axes[1].grid(axis="x")
    axes[1].legend(fontsize=6.1, loc="lower left", frameon=True)

    fig.subplots_adjust(left=0.14, right=0.99, bottom=0.18, top=0.88, wspace=0.12)
    _save_both(fig, "fig04_paired_effects")


def build_fig05_historical_repair_and_cal() -> None:
    """Fig 5: Stage 8 Directed Outage Repair Timeline & Offline Probability Calibration (Archive Continuity)."""
    fig, axes = plt.subplots(1, 2, figsize=(IEEE_W, 2.35))

    # Panel (a): Stage 8 directed outage repair probe completion (20 seeds, archived statistics)
    cats = ["Strict Affinity\n(Wait Recovery)", "Cross-Path Repair\n(Alt. Path Failover)"]
    vals = [1544.0, 253.0]
    cols = ["#cc3311", "#004488"]
    bars = axes[0].bar([0, 1], vals, width=0.46, color=cols, edgecolor="#222222", lw=0.6)
    for b, v in zip(bars, vals):
        axes[0].text(b.get_x() + b.get_width()/2, v + 45, f"{v:.0f} ms", ha="center", va="bottom", fontsize=7.2, fontweight="bold")
    axes[0].annotate("−1,291 ms (−83.6%)\n95% CI [−1314, −1267] ms", xy=(1, 410), xytext=(0.62, 1080),
                     ha="center", fontsize=6.3, color="#004488", fontweight="bold",
                     arrowprops=dict(arrowstyle="->", color="#004488", lw=0.9),
                     bbox=dict(fc="#f4f8fc", ec="#004488", pad=2.0))
    axes[0].set(title="(a) Directed Outage Failover (Stage 8, 20 Seeds)", xticks=[0, 1], xticklabels=cats,
                ylabel="Mean Probe Completion (ms) ↓", ylim=(0, 1820))
    axes[0].grid(axis="y")

    # Panel (b): Stage 8 Independent Calibration Brier Score (1,031 test records)
    models = ["RTT-only", "Raw Prob.", "Earliest\n(ECF)", "Slack-only", "Calibrated\nProb."]
    brier_all = [0.11885, 0.04131, 0.04519, 0.04158, 0.03547]
    brier_short = [0.49904, 0.26002, 0.25765, 0.24074, 0.21786]
    xb = np.arange(len(models)); w = 0.36
    axes[1].bar(xb - w/2, brier_all, width=w, color="#004488", edgecolor="#222222", lw=0.45, label="All Effective Budgets")
    axes[1].bar(xb + w/2, brier_short, width=w, color="#228833", edgecolor="#222222", lw=0.45, hatch="///", alpha=0.88, label="Short Budget (0–100 ms)")
    axes[1].set(title="(b) Path Brier Error (1,031 Held-Out Recs)", xticks=xb, xticklabels=models,
                ylabel="Brier Score ↓", ylim=(0, 0.56))
    axes[1].grid(axis="y")
    axes[1].legend(fontsize=6.3, loc="upper right", frameon=True)

    fig.subplots_adjust(left=0.09, right=0.99, bottom=0.20, top=0.87, wspace=0.25)
    _save_both(fig, "fig05_repair_and_calibration")


def build_graphical_abstract(runs: list[dict]) -> None:
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 8.6})
    fig = plt.figure(figsize=(14.5, 5.4), facecolor="white")
    fig.text(0.5, 0.945, "IoT-MPQUIC-Scheduler (FAP-MPQUIC): Freshness- & Stall-Aware Dual-Path QUIC Scheduling",
             ha="center", va="center", fontsize=11.8, fontweight="bold", color="#112233")
    fig.text(0.5, 0.892, "Graphical Abstract  |  Real C XQUIC Protocol Stack  ·  Linux netns + tc/netem  ·  1,596+ Audited Connections  ·  MinRTT / ECF / BLEST / Prob",
             ha="center", va="center", fontsize=8.7, color="#445566")

    # Panel 1: Architecture Card
    ax1 = fig.add_axes([0.02, 0.11, 0.27, 0.72])
    ax1.set_xlim(0, 10); ax1.set_ylim(0, 10); ax1.axis("off")
    ax1.add_patch(FancyBboxPatch((0.35, 0.35), 9.3, 9.3, boxstyle="round,pad=0.22", fc="#f4f7fb", ec="#004488", lw=1.4))
    ax1.text(5.0, 9.05, "1. FAP-MPQUIC Architecture (C)", ha="center", va="center", fontsize=9.8, fontweight="bold", color="#004488")
    cards = [
        (7.35, "#e8eef5", "#335577", "App Queue: Deadline + Overwrite AoI\nReplaces stale state · Bounded bursts"),
        (5.20, "#dbeafe", "#004488", "CWND-Aware Delivery Probability\nPenalizes in-flight > CWND + PTO + loss"),
        (3.05, "#e6f4ea", "#1b6e35", "Health-Gated Freshness Probing\nRefreshes stale RTT (≥150ms) iff idle/clean"),
        (1.25, "#fff4e5", "#b25e09", "Real XQUIC + BoringSSL + netns/netem\nDual-PCAP + Independent Oracle Audited"),
    ]
    for y, fc, ec, txt in cards:
        ax1.add_patch(FancyBboxPatch((0.85, y - 0.62), 8.3, 1.48, boxstyle="round,pad=0.14", fc=fc, ec=ec, lw=1.0))
        ax1.text(5.0, y + 0.12, txt, ha="center", va="center", fontsize=7.8, color="#111111")

    # Panel 2: S1 Outage+Reversal
    ax2 = fig.add_axes([0.355, 0.17, 0.26, 0.61])
    pols = ["fifo_minrtt", "fixed_minrtt", "ecf", "blest", "fap_strict"]
    lbls = ["FIFO+\nMinRTT", "Fixed\nMinRTT", "ECF", "BLEST", "FAP\n(Ours)"]
    cols = ["#777777", "#cc7722", "#cc3311", "#bb8833", "#004488"]
    ev1 = [_seed_ci(runs, "S1_outage_reversal", p, "event_deadline_success", 100.0)[0] for p in pols]
    aoi1 = [_seed_ci(runs, "S1_outage_reversal", p, "mean_aoi_ms", 1.0)[0] / 1000.0 for p in pols]
    x = np.arange(len(pols))
    b2 = ax2.bar(x, ev1, width=0.52, color=cols, alpha=0.88)
    ax2.set_ylim(0, 34); ax2.set_xticks(x); ax2.set_xticklabels(lbls, fontsize=7.8)
    ax2.set_ylabel("Event Deadline Success (%) [bars]", color="#004488", fontweight="bold", fontsize=8.2)
    ax2.set_title("2. S1: High-Load Outage + Reversal", fontsize=9.8, fontweight="bold", color="#004488", pad=8)
    ax2.spines["top"].set_visible(False); ax2.grid(axis="y", alpha=0.22)
    for b, v in zip(b2, ev1):
        ax2.text(b.get_x() + b.get_width()/2, v + 0.8, f"{v:.1f}%", ha="center", va="bottom", fontsize=7.6, fontweight="bold")
    ax2b = ax2.twinx()
    ax2b.plot(x, aoi1, "o--", color="#cc3311", lw=2.0, ms=5.5)
    ax2b.set_ylim(1.2, 3.1); ax2b.set_ylabel("Mean State AoI (s) [line]", color="#cc3311", fontweight="bold", fontsize=8.2)
    ax2b.spines["top"].set_visible(False)
    for xi, av in zip(x, aoi1):
        ax2b.text(xi, av + 0.09, f"{av:.2f}s", ha="center", va="bottom", fontsize=7.4, color="#cc3311", fontweight="bold",
                  bbox=dict(boxstyle="round,pad=0.14", fc="white", ec="none", alpha=0.86))
    ax2.text(0.5, -0.23, "+20 pp event success & -300 to -750 ms AoI vs MinRTT/ECF/BLEST",
             transform=ax2.transAxes, ha="center", fontsize=7.5, color="#333333", style="italic")

    # Panel 3: S4 Mid-Run Blackout
    ax3 = fig.add_axes([0.72, 0.17, 0.26, 0.61])
    pols3 = ["fifo_minrtt", "ecf", "blest", "prob_strict", "fap_strict"]
    lbls3 = ["FIFO+\nMinRTT", "ECF", "BLEST", "Prob\n(Stg8)", "FAP\n(Ours)"]
    cols3 = ["#777777", "#cc3311", "#bb8833", "#228833", "#004488"]
    ev4 = [_seed_ci(runs, "S4_mid_blackout", p, "event_deadline_success", 100.0)[0] for p in pols3]
    aoi4 = [_seed_ci(runs, "S4_mid_blackout", p, "mean_aoi_ms", 1.0)[0] / 1000.0 for p in pols3]
    b3 = ax3.bar(x, ev4, width=0.52, color=cols3, alpha=0.88)
    ax3.set_ylim(0, 112); ax3.set_xticks(x); ax3.set_xticklabels(lbls3, fontsize=7.8)
    ax3.set_ylabel("Event Deadline Success (%) [bars]", color="#004488", fontweight="bold", fontsize=8.2)
    ax3.set_title("3. S4: Mid-Run Path-A Blackout", fontsize=9.8, fontweight="bold", color="#004488", pad=8)
    ax3.spines["top"].set_visible(False); ax3.grid(axis="y", alpha=0.22)
    for b, v in zip(b3, ev4):
        ax3.text(b.get_x() + b.get_width()/2, v + 2.0, f"{v:.1f}%", ha="center", va="bottom", fontsize=7.6, fontweight="bold")
    ax3b = ax3.twinx()
    ax3b.plot(x, aoi4, "o--", color="#cc3311", lw=2.0, ms=5.5)
    ax3b.set_ylim(0.5, 2.2); ax3b.set_ylabel("Mean State AoI (s) [line]", color="#cc3311", fontweight="bold", fontsize=8.2)
    ax3b.spines["top"].set_visible(False)
    for xi, av in zip(x, aoi4):
        ax3b.text(xi, av + 0.08, f"{av:.2f}s", ha="center", va="bottom", fontsize=7.4, color="#cc3311", fontweight="bold",
                  bbox=dict(boxstyle="round,pad=0.14", fc="white", ec="none", alpha=0.86))
    ax3.text(0.5, -0.23, "+30–52 pp event success & -168 to -872 ms AoI vs MinRTT/ECF/BLEST",
             transform=ax3.transAxes, ha="center", fontsize=7.5, color="#333333", style="italic")

    out_p = Path(__file__).resolve().parent
    fig.savefig(out_p / "graphical_abstract.png", dpi=220)
    fig.savefig(out_p / "graphical_abstract.pdf")
    plt.close(fig)


if __name__ == "__main__":
    runs = _load_csv(RES10 / "runs.csv")
    effects = _load_csv(RES10 / "paired_effects.csv")
    build_fig02_event_and_aoi(runs)
    build_fig03_path_and_pareto(runs)
    build_fig04_paired_effects(effects)
    build_fig05_historical_repair_and_cal()
    build_graphical_abstract(runs)
    print("Built all 4 IEEE vector figures + PNGs + Graphical Abstract cleanly.")
