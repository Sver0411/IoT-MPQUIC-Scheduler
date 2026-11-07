"""Publication figures from Stage 6 tables and packet traces."""

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
from matplotlib.patches import Patch
from matplotlib.ticker import MultipleLocator

BLUE = "#235789"
ORANGE = "#A44822"
GRAY = "#68778B"
INK = "#172638"
GRID = "#DBE2E9"

SCENARIOS = (
    ("stable", "Stable"), ("bandwidth", "Bandwidth\ndrop"), ("latency", "Latency\nspike"),
    ("loss", "Random\nloss"), ("outage", "Path\noutage"), ("reversal", "Path\nreversal"),
)
BASELINES = (
    ("fifo", "FIFO"), ("strict-priority", "Strict priority"), ("edf", "EDF"),
    ("aoi", "AoI-aware"), ("latest-rr", "Latest-value + RR"), ("proposed-cross", "Proposed"),
)
ABLATIONS = (
    ("no-replace", "No pending replacement"), ("no-event", "No event boost"),
    ("no-aoi", "No AoI score"), ("fixed-path", "Fixed initial path"),
    ("no-recovery", "No recovery class"), ("no-cross", "No cross-path repair"),
)
SEEDS = ((17, BLUE, "o", -0.12), (29, ORANGE, "D", 0.12))


def setup():
    plt.rcParams.update({
        "font.family": "DejaVu Sans", "font.size": 10, "text.color": INK,
        "axes.labelcolor": INK, "axes.titlecolor": INK, "axes.titleweight": "bold",
        "axes.spines.top": False, "axes.spines.right": False, "axes.edgecolor": "#A8B4C2",
        "xtick.color": INK, "ytick.color": INK, "grid.color": GRID, "grid.linewidth": 0.75,
        "legend.frameon": False, "svg.hashsalt": "iot-mpquic-stage6", "svg.fonttype": "path",
    })


def save(fig, out, name):
    fig.savefig(out / f"{name}.png", dpi=220, metadata={"Software": f"Matplotlib {matplotlib.__version__}"})
    fig.savefig(out / f"{name}.svg", metadata={"Date": None})
    plt.close(fig)
    return name


def scenario_figure(runs, out):
    selected = {(row["scenario"], row["policy"]): row for row in runs if row["dataset"] == "seed17"
                and row["workload"] == "mixed" and row["ablation"] == "full"}
    fig, (deadline, age) = plt.subplots(2, 1, figsize=(10.4, 6.2), sharex=True)
    fig.subplots_adjust(left=.11, right=.98, top=.83, bottom=.13, hspace=.33)
    fig.suptitle("Deadline delivery and freshness across six link scenarios", x=.11, y=.965, ha="left", fontsize=17)
    fig.text(.11, .91, "Mixed workload  |  Seed 17  |  One independent connection per policy and scenario",
             color=GRAY, fontsize=10)
    for policy, name, color, shift in (("fifo", "FIFO", GRAY, -.19), ("proposed-cross", "Proposed", BLUE, .19)):
        x = [i + shift for i in range(len(SCENARIOS))]
        rows = [selected[scenario, policy] for scenario, _ in SCENARIOS]
        counts = [row["events_on_time"] for row in rows]
        bars = deadline.bar(x, counts, .34, color=color, label=name, zorder=3)
        deadline.bar_label(bars, padding=3, fontsize=9)
        age.bar(x, [row["mean_aoi_ms"] / 1000 for row in rows], .34, color=color, zorder=3)
    deadline.set_ylim(0, 10.6)
    deadline.yaxis.set_major_locator(MultipleLocator(3))
    deadline.set_ylabel("On-time events / 9")
    age.set_ylabel("Mean AoI (s)")
    age.set_ylim(0, 1.85)
    age.yaxis.set_major_locator(MultipleLocator(.5))
    age.set_xticks(range(len(SCENARIOS)), [label for _, label in SCENARIOS])
    for ax in (deadline, age):
        ax.grid(axis="y")
        ax.set_axisbelow(True)
    deadline.legend(loc="upper right", bbox_to_anchor=(1, 1.20), ncols=2)
    return save(fig, out, "01-dynamic-scenarios")


def seeded_axes(title, subtitle, labels, xlabel, figsize=(10.4, 4.9)):
    fig, axes = plt.subplots(1, 2, figsize=figsize, sharey=True)
    fig.subplots_adjust(left=.255, right=.965, top=.76, bottom=.17, wspace=.24)
    fig.suptitle(title, x=.055, y=.96, ha="left", fontsize=16)
    fig.text(.055, .89, subtitle, color=GRAY, fontsize=10)
    legend = [Line2D([], [], linestyle="none", marker=marker, color=color, label=f"Seed {seed}", markersize=7)
              for seed, color, marker, _ in SEEDS]
    fig.legend(handles=legend, loc="upper right", bbox_to_anchor=(.97, .855), ncols=2)
    y = list(range(len(labels)))
    for ax, caption in zip(axes, xlabel):
        ax.set_xlabel(caption)
        ax.set_yticks(y, labels)
        ax.set_ylim(len(labels) - .48, -.52)
        ax.tick_params(axis="y", length=0)
        ax.grid(axis="x")
        ax.set_axisbelow(True)
    return fig, axes, y


def baseline_figure(runs, out):
    rows = {(row["seed"], row["policy"]): row for row in runs if row["workload"] == "high-mixed"
            and row["ablation"] == "full"}
    fields = ("events_on_time", "mean_aoi_ms")
    fig, axes, y = seeded_axes("Baselines under outage and path reversal",
                               "High mixed workload  |  Each marker represents one independent run",
                               [label for _, label in BASELINES],
                               ("On-time events / 20", "Mean AoI (s)"))
    for ax, field, scale in zip(axes, fields, (1, .001)):
        for seed, color, marker, shift in SEEDS:
            ax.scatter([rows[seed, policy][field] * scale for policy, _ in BASELINES],
                       [row + shift for row in y], color=color, marker=marker, s=45, zorder=3)
    axes[0].set_xlim(-.5, 20.5)
    axes[0].xaxis.set_major_locator(MultipleLocator(5))
    axes[1].set_xlim(0, 2.25)
    axes[1].xaxis.set_major_locator(MultipleLocator(.5))
    return save(fig, out, "02-high-load-baselines")


def ablation_figure(runs, deltas, out):
    cases = {(row["dataset"], row["case"]): row for row in runs}
    paired = {}
    for row in deltas:
        run = cases[row["dataset"], row["case"]]
        if row["group"] == "ablation" and run["workload"] == "high-mixed":
            paired[row["seed"], run["ablation"]] = row
    fields = ("events_deadline_success_ratio", "mean_aoi_ms")
    fig, axes, y = seeded_axes("Ablation effects relative to the full scheduler",
                               "High mixed workload  |  Ablated minus full, paired within each seed",
                               [label for _, label in ABLATIONS],
                               ("Change in event success (pp)", "Change in mean AoI (s)"))
    for ax, field, scale in zip(axes, fields, (100, .001)):
        ax.axvline(0, color=GRAY, linestyle=(0, (4, 3)), linewidth=1)
        for seed, color, marker, shift in SEEDS:
            ax.scatter([paired[seed, variant][field] * scale for variant, _ in ABLATIONS],
                       [row + shift for row in y], color=color, marker=marker, s=45, zorder=3)
    axes[0].set_xlim(-29, 9)
    axes[0].xaxis.set_major_locator(MultipleLocator(10))
    axes[1].set_xlim(-.46, .39)
    axes[1].xaxis.set_major_locator(MultipleLocator(.2))
    return save(fig, out, "03-paired-ablations")


def repair_figure(timeline, out):
    fig, axes = plt.subplots(2, 1, figsize=(10.4, 4.6), sharex=True)
    fig.subplots_adjust(left=.12, right=.97, top=.73, bottom=.17, hspace=.63)
    fig.suptitle("Probe Stream during a client-to-server path A outage",
                 x=.12, y=.965, ha="left", fontsize=16)
    fig.text(.12, .895, "Seed 17  |  Same probe and link schedule  |  Times relative to replay origin",
             color=GRAY, fontsize=10)
    for ax, (mode, label) in zip(axes, (("full", "Cross-path repair enabled"), ("no-cross", "Cross-path repair disabled"))):
        rows = [row for row in timeline if row["ablation"] == mode]
        time = {row["event"]: row["replay_us"] / 1_000_000 for row in rows if row["event"] != "stream_packet_sent"}
        ax.axvspan(time["cut_applied"], time["restore_started"], color=ORANGE, alpha=.13)
        ax.axvline(time["generated"], color=GRAY, linewidth=1.15, linestyle=":")
        ax.axvline(time["deadline"], color=ORANGE, linewidth=1.15, linestyle="--")
        for path, y, color, marker in (("0", 0, GRAY, "o"), ("1", 1, BLUE, "D")):
            packets = [row["replay_us"] / 1_000_000 for row in rows if row.get("path_id") == path]
            ax.scatter(packets, [y] * len(packets), color=color, marker=marker, s=23, zorder=4)
        fin = time["server_fin"]
        ax.scatter([fin], [2], color=INK, marker="*", s=100, zorder=5)
        annotation = f"{(fin - time['generated']) * 1000:,.0f} ms E2E"
        left = fin > 1.5
        ax.annotate(annotation, (fin, 2), xytext=(-10 if left else 10, -3),
                    textcoords="offset points", ha="right" if left else "left", va="center", fontsize=9)
        ax.set_title(label, loc="left", fontsize=10, pad=6)
        ax.set_yticks([0, 1, 2], ["A sent", "B sent", "Server FIN"])
        ax.set_ylim(-.38, 2.43)
        ax.grid(axis="x")
        ax.set_axisbelow(True)
        ax.tick_params(axis="y", length=0)
    legend = [Patch(facecolor=ORANGE, alpha=.13, label="Path A cut"),
              Line2D([], [], color=GRAY, linestyle=":", label="Generated"),
              Line2D([], [], color=ORANGE, linestyle="--", label="25 ms deadline")]
    fig.legend(handles=legend, loc="upper right", bbox_to_anchor=(.97, .855), ncols=3)
    axes[1].set_xlim(0, 1.93)
    axes[1].xaxis.set_major_locator(MultipleLocator(.25))
    axes[1].set_xlabel("Time from replay origin (s)")
    return save(fig, out, "04-cross-path-repair")


def render(runs, deltas, timeline, out):
    setup()
    out.mkdir(parents=True, exist_ok=True)
    return [scenario_figure(runs, out), baseline_figure(runs, out),
            ablation_figure(runs, deltas, out), repair_figure(timeline, out)]
