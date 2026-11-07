"""Research figures for paired path controls and randomized-path calibration."""

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from stage6_figures import BLUE, GRAY, ORANGE, save, setup

MODELS = {
    "slack-only": ("Slack-only", GRAY),
    "rtt-only": ("RTT-only", "#805779"),
    "earliest-completion": ("Earliest completion", ORANGE),
    "current-probability": ("Current probability", BLUE),
    "calibrated-probability": ("Calibrated probability", "#326853"),
}
SCENES = {"high-load": "High mixed load", "novel-reversal": "Unseen reversal"}
CELLS = {"A": "FIFO + Native", "B": "Proposed-App + Native", "C": "FIFO + Probability",
         "Fixed": "Fixed MinRTT", "Earliest": "Earliest completion", "D": "Probability", "Cross": "Probability + Cross"}


def interval(ax, row, y, scale, color):
    m, lo, hi = (row[key] * scale for key in ("mean", "ci_low", "ci_high"))
    ax.errorbar(m, y, xerr=[[max(0, m - lo)], [max(0, hi - m)]], fmt="o", capsize=3, color=color)


def render(runs, summary, reliability, timeline, out):
    setup()
    plt.rcParams["svg.hashsalt"] = "iot-mpquic-stage8"
    out.mkdir(parents=True, exist_ok=True)
    index = {(r["kind"], r["scenario"], r["label"], r["metric"]): r for r in summary}
    names = []
    for kind, labels, title, name in (
        ("cell", CELLS, "Scheduling performance across two network environments", "01-path-comparison"),
        ("contrast", {"B-A": "App effect, native", "C-A": "Path effect, FIFO", "D-B-C+A": "Interaction",
                      "D-Fixed": "Probability − Fixed MinRTT", "D-Earliest": "Probability − Earliest",
                      "Cross-D": "Cross − Strict"},
         "Independent application, path and repair effects", "02-paired-effects"),
    ):
        fig, axes = plt.subplots(2, 2, figsize=(10.8, 7.9), sharey="row")
        fig.suptitle(title, y=.97, x=.23, ha="left", fontsize=16)
        fig.text(.23, .918, "Means and 95% seed-bootstrap intervals, 20 seeds per environment", color=GRAY)
        fig.subplots_adjust(left=.25, right=.97, bottom=.095, top=.84, hspace=.46, wspace=.23)
        for (scene, scene_label), row in zip(SCENES.items(), axes):
            for ax, (metric, unit, scale) in zip(row, (
                ("event_deadline_success", "Event deadline success (%)" if kind == "cell" else "Event success difference (pp)", 100),
                ("mean_aoi_ms", "Observed mean AoI (ms)" if kind == "cell" else "Observed mean AoI difference (ms)", 1),
            )):
                if kind == "contrast":
                    ax.axvline(0, color=GRAY, lw=1)
                for i, key in enumerate(labels):
                    interval(ax, index[kind, scene, key, metric], i, scale, BLUE if scene == "high-load" else ORANGE)
                ax.set(yticks=range(len(labels)), yticklabels=list(labels.values()), xlabel=unit,
                       ylim=(len(labels) - .5, -.5))
                ax.grid(axis="y", alpha=.6)
            row[0].set_title(scene_label, loc="left")
        names.append(save(fig, out, name))

    fig, axes = plt.subplots(1, 2, figsize=(11.0, 5.0))
    fig.suptitle("Completion prediction on randomized, held-out paths", x=.1, y=.97, ha="left", fontsize=16)
    fig.text(.1, .88, "Even exploration seeds, equal-width probability bins", color=GRAY)
    fig.subplots_adjust(left=.105, right=.96, bottom=.22, top=.76, wspace=.32)
    for ax, (target, caption) in zip(axes, (("deadline_met", "Absolute deadline"), ("budget_met", "Effective budget"))):
        ax.plot([0, 1], [0, 1], ls="--", color="#A0A6AF", lw=1)
        for model, (label, color) in MODELS.items():
            rows = [r for r in reliability if r["model"] == model and r["target"] == target and
                    r["dimension"] == "overall"]
            ax.plot([r["predicted"] for r in rows], [r["observed"] for r in rows],
                    "o-", ms=4, lw=1.15, color=color, label=label)
        ax.set(xlim=(-.03, 1.03), ylim=(-.03, 1.03), xlabel="Predicted probability",
               ylabel="Observed success ratio", title=caption)
        ax.grid(alpha=.6)
    handles, labels = axes[-1].get_legend_handles_labels()
    fig.legend(handles, labels, loc="lower center", bbox_to_anchor=(.53, .025), ncol=3, fontsize=8)
    names.append(save(fig, out, "03-reliability"))

    formal = [r for r in timeline if r["suite"] == "paired"]
    seed = min(row["seed"] for row in formal)
    fig, axes = plt.subplots(2, 1, figsize=(9, 5.8), sharex=True)
    fig.suptitle("Directed outage and actual STREAM transmission", x=.13, y=.97, ha="left", fontsize=16)
    fig.text(.13, .89, f"Probe in paired seed {seed}", color=GRAY)
    fig.subplots_adjust(left=.13, right=.96, top=.80, bottom=.13, hspace=.43)
    for cell, ax in zip(("D", "Cross"), axes):
        group = [r for r in formal if r["seed"] == seed and r["cell"] == cell]
        times = {r["event"]: r["relative_us"] / 1000 for r in group if r["event"] != "stream_sent"}
        ax.axvspan(times["cut"], times["restore"], color=GRAY, alpha=.16, label="Path A outage")
        for p, color in (("0", BLUE), ("1", ORANGE)):
            sent = [r["relative_us"] / 1000 for r in group if r["event"] == "stream_sent" and r["path"] == p]
            ax.scatter(sent, [int(p)] * len(sent), color=color, s=22)
        for event, color in (("deadline", ORANGE), ("received", "#326853")):
            if event in times:
                ax.axvline(times[event], ls=":", color=color, label=event.capitalize())
        ax.set(yticks=(0, 1), yticklabels=("Path A", "Path B"), ylim=(-.35, 1.35),
               title="Strict" if cell == "D" else "Cross repair")
        ax.legend(loc="upper right", fontsize=8)
    axes[-1].set_xlabel("Time from replay origin (ms)")
    names.append(save(fig, out, "04-repair-timeline"))
    return names
