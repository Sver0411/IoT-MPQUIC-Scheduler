"""Render Stage 7 metrics and packet evidence as PNG and SVG figures."""

import statistics

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from stage6_figures import BLUE, GRAY, ORANGE, save, setup

CELLS = ("A", "B", "C", "D")
COLORS = {"A": GRAY, "B": BLUE, "C": ORANGE, "D": "#326853"}
LABELS = {"A": "FIFO + MinRTT", "B": "Proposed-App + MinRTT",
          "C": "FIFO + Proposed-Path", "D": "Proposed-App + Proposed-Path"}
METRICS = (("event_deadline_success", "On-time events (%)", 100),
           ("mean_aoi_ms", "Mean observed AoI (ms)", 1))
BASELINES = ("fifo", "strict-priority", "edf", "aoi", "latest-rr", "full")


def canvas(title, subtitle, rows=1, cols=1, size=(9.5, 4.6), left=.12):
    fig, axes = plt.subplots(rows, cols, figsize=size, squeeze=False)
    fig.subplots_adjust(left=left, right=.97, top=.80, bottom=.17, wspace=.38, hspace=.5)
    fig.suptitle(title, x=left, y=.96, ha="left", fontsize=15)
    fig.text(left, .875, subtitle, color=GRAY, fontsize=9)
    for ax in axes.flat:
        ax.grid(axis="y", alpha=.8)
        ax.set_axisbelow(True)
    return fig, axes


def interval(ax, item, position, scale, color, horizontal=False, label=None):
    point = item["mean"] * scale
    errors = None if item["ci_low"] is None else [[max(0, point - item["ci_low"] * scale)],
                                                   [max(0, item["ci_high"] * scale - point)]]
    if horizontal:
        ax.errorbar(point, position, xerr=errors, fmt="o", capsize=3, color=color, label=label)
    else:
        ax.errorbar(position, point, yerr=errors, fmt="o", capsize=3, color=color, label=label)


def across_scenes(index, scenes, out):
    title = "Deadline delivery and freshness across network and load conditions"
    subtitle = "Cell means and 95% whole-seed bootstrap intervals"
    fig, axes = canvas(title, subtitle, cols=2, size=(12.5, 5.5))
    for ax, (metric, label, scale) in zip(axes[0], METRICS):
        for j, cell in enumerate(CELLS):
            for i, scene in enumerate(scenes):
                key = ("cell", "factorial", scene, cell, metric)
                if key in index:
                    interval(ax, index[key], i + (j - 1.5) * .19, scale, COLORS[cell],
                             label=LABELS[cell] if i == 0 else None)
        ax.set_xticks(range(len(scenes)), [s.replace("-", "\n") for s in scenes], fontsize=8)
        ax.set_ylabel(label)
    handles, labels = axes[0, 1].get_legend_handles_labels()
    fig.legend(handles, labels, loc="lower center", bbox_to_anchor=(.5, .01), ncol=4, fontsize=8)
    return save(fig, out, "01-conditions")


def forest(index, suite, scenes, labels, out, name, title, select, subtitle):
    fig, axes = canvas(title, subtitle, cols=2, size=(11.5, max(4.5, len(labels) * .44 + 2)), left=.22)
    for ax, (metric, unit, scale) in zip(axes[0], METRICS):
        ax.axvline(0, color=GRAY, lw=1)
        for i, (scene, label) in enumerate(zip(scenes, labels)):
            for j, contrast in enumerate(select(scene, label)):
                item = index.get(("contrast", suite, scene, contrast, metric))
                if item:
                    count = len(select(scene, label))
                    interval(ax, item, i + (j - (count - 1) / 2) * .15, scale,
                             (BLUE, ORANGE, "#326853", GRAY, "#613A6B", "#807533")[j],
                             horizontal=True, label=contrast if i == 0 and count > 1 else None)
        ax.set_yticks(range(len(labels)), labels)
        ax.invert_yaxis()
        ax.set_xlabel("Event deadline difference (pp)" if scale == 100 else f"{unit} difference")
    if len(select(scenes[0], labels[0])) > 1:
        handles, names = axes[0, 1].get_legend_handles_labels()
        fig.legend(handles, names, loc="lower center", bbox_to_anchor=(.60, .018), ncol=4, fontsize=8)
    return save(fig, out, name)


def tradeoff(rows, out, fairness=False):
    if fairness:
        x, y = "useful_state_delivery", "jain_freshness"
        xlabel, ylabel = "Useful state delivery ratio", "Jain index of in-budget receiver time"
        title = "State delivery and freshness fairness"
    else:
        x, y = "mean_aoi_ms", "event_deadline_success"
        xlabel, ylabel = "Mean observed AoI (ms)", "Event deadline success ratio"
        title = "Event deadline delivery and state age"
    fig, axes = canvas(title, "Each marker represents one factorial connection", size=(8.1, 4.8))
    ax = axes[0, 0]
    for cell in CELLS:
        group = [r for r in rows if r["cell"] == cell and r[x] is not None and r[y] is not None]
        ax.scatter([r[x] for r in group], [r[y] for r in group], s=20, alpha=.56, color=COLORS[cell],
                   label=LABELS[cell])
    ax.set(xlabel=xlabel, ylabel=ylabel)
    ax.legend(fontsize=8)
    return save(fig, out, "09-fairness" if fairness else "03-tradeoff")


def baselines(index, out):
    fig, axes = canvas("Application baselines under high mixed load",
                       "Independent seed means and 95% bootstrap intervals", cols=2, size=(10, 5.4))
    for ax, (metric, label, scale) in zip(axes[0], METRICS):
        for i, cell in enumerate(BASELINES):
            item = index.get(("cell", "ablation", "mechanisms", cell, metric))
            if item:
                interval(ax, item, i, scale, BLUE if cell == "full" else GRAY)
            else:
                ax.text(i, .05, "N/A", transform=ax.get_xaxis_transform(), ha="center", color=GRAY)
        ax.set_xticks(range(len(BASELINES)), [s.replace("-", "\n") if s != "full" else "Joint" for s in BASELINES],
                      fontsize=8)
        ax.set_ylabel(label)
    return save(fig, out, "04-baselines")


def paths(rows, out):
    scenes = sorted({r["scenario"] for r in rows})
    fig, axes = canvas("Path traffic under heterogeneous conditions", "Mean client-to-server UDP payload per connection",
                       cols=len(scenes), size=(max(8, len(scenes) * 4), 4.8))
    for scene, ax in zip(scenes, axes[0]):
        totals = []
        for link in ("a", "b"):
            totals.append([statistics.fmean(r[f"path_{link}_udp_rx_bytes"] / 1024 for r in rows
                                            if r["cell"] == cell and r["scenario"] == scene) for cell in CELLS])
        ax.bar(CELLS, totals[0], color=BLUE, label="Path A")
        ax.bar(CELLS, totals[1], bottom=totals[0], color=ORANGE, label="Path B")
        ax.set(title=scene.replace("-", " ").capitalize(), xlabel="Factorial cell", ylabel="UDP payload (KiB)")
    axes[0, -1].legend()
    return save(fig, out, "05-path-traffic")


def reliability(bins, out):
    fig, axes = canvas("Selected-path completion model calibration",
                       "Factorial submissions, 10 equal-width bins. Marker area reflects sample count.",
                       cols=2, size=(10.3, 4.8))
    for ax, target, title in zip(axes[0], ("deadline_met", "budget_met"), ("Deadline", "Effective budget")):
        ax.plot([0, 1], [0, 1], ls="--", color=GRAY, lw=1)
        for kind, color in (("event", BLUE), ("state", ORANGE)):
            group = [r for r in bins if r["suite"] == "factorial" and r["dimension"] == "kind" and
                     r["value"] == kind and r["target"] == target]
            ax.plot([r["predicted"] for r in group], [r["observed"] for r in group], color=color, alpha=.6)
            ax.scatter([r["predicted"] for r in group], [r["observed"] for r in group], color=color,
                       s=[12 + 3 * r["n"] ** .5 for r in group], label=kind.capitalize())
        ax.set(xlim=(-.03, 1.03), ylim=(-.03, 1.03), xlabel="Predicted probability",
               ylabel="Observed success ratio", title=title)
        ax.legend()
    return save(fig, out, "06-reliability")


def repair(timeline, out):
    formal = [row for row in timeline if row["suite"] == "repair"]
    seed = min(row["seed"] for row in formal)
    rows = [row for row in formal if row["seed"] == seed]
    fig, axes = canvas("Actual STREAM transmissions during a path outage",
                       f"Directed probe, repair seed {seed}", rows=2, size=(9.5, 6.1))
    for ax, cell in zip(axes[:, 0], ("Strict", "Cross")):
        group = [r for r in rows if r["cell"] == cell]
        times = {r["event"]: r["relative_us"] / 1000 for r in group if r["event"] != "stream_sent"}
        ax.axvspan(times["cut"], times["restore"], color=GRAY, alpha=.14, label="Path A outage")
        for path, color in (("0", BLUE), ("1", ORANGE)):
            sent = [r["relative_us"] / 1000 for r in group if r["event"] == "stream_sent" and r["path"] == path]
            ax.scatter(sent, [int(path)] * len(sent), color=color)
        for event, color, style in (("deadline", ORANGE, ":"), ("received", "#326853", "-.")):
            if event in times:
                ax.axvline(times[event], color=color, ls=style, label=event.capitalize())
        ax.set(yticks=(0, 1), yticklabels=("Path A", "Path B"), title=cell, ylim=(-.45, 1.45))
        ax.legend(loc="upper right", fontsize=8)
    axes[-1, 0].set_xlabel("Time from replay start (ms)")
    return save(fig, out, "07-repair-timeline")


def render(runs, stats, bins, timeline, out):
    setup()
    plt.rcParams["svg.hashsalt"] = "iot-mpquic-stage7"
    out.mkdir(parents=True, exist_ok=True)
    index = {(r["kind"], r["suite"], r["scenario"], r["label"], r["metric"]): r for r in stats}
    factorial = [r for r in runs if r["suite"] == "factorial"]
    names = []
    if factorial:
        scenes = list(dict.fromkeys(r["scenario"] for r in factorial))
        names.append(across_scenes(index, scenes, out))
        contrasts = ("B-A", "C-A", "D-B-C+A", "D-A")
        names.append(forest(index, "factorial", scenes, scenes, out, "02-factorial",
                            "Application, path and interaction contrasts", lambda *_: contrasts,
                            "Paired seed deltas and 95% bootstrap intervals"))
        names.append(tradeoff(factorial, out))
        names.append(paths([r for r in runs if (r["suite"], r["scenario"]) in
                            (("factorial", "dynamic-heterogeneous"), ("factorial", "high-load"),
                             ("loss", "partial-loss"))], out))
        names.append(reliability(bins, out))
        names.append(tradeoff(factorial, out, fairness=True))
    if any(r["suite"] == "repair" for r in runs):
        names.append(repair(timeline, out))
    if any(r["suite"] == "ablation" for r in runs):
        names.append(baselines(index, out))
        labels = ["no-event", "no-aoi", "no-replace", "no-recovery", "fixed-path", "no-probability", "no-cross"]
        names.append(forest(index, "ablation", ["mechanisms"] * len(labels), labels, out, "08-ablations",
                            "Individual mechanism ablations", lambda _, label: [label + "-full"],
                            "Ablation minus full joint policy on matched high-load replays"))
    labels = sorted({r["cell"] for r in runs if r["suite"] == "sensitivity" and r["cell"] != "full"})
    if labels:
        names.append(forest(index, "sensitivity", ["sensitivity"] * len(labels), labels, out, "10-sensitivity",
                            "One-parameter sensitivity under high load", lambda _, label: [label + "-full"],
                            "Variant minus default joint policy, one parameter varied per contrast"))
    return sorted(names)
