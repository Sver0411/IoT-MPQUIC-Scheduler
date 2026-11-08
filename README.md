# IoT-MPQUIC-Scheduler

**English** | [简体中文](README.zh-CN.md)

**Deadline- and freshness-aware multipath QUIC scheduling for IoT traffic.**

IoT-MPQUIC-Scheduler sends periodic sensor updates and independent events over one QUIC connection with two real UDP paths. It measures whether joint record scheduling and path selection can improve event deadline delivery while keeping sensor data fresh as link conditions change.

[Results](#results) · [Quick start](#quick-start) · [Reproduce](#reproduce-the-experiments) · [Latest report](docs/stage93a-data-quality-feature-analysis.md)

## How it works

```mermaid
flowchart LR
    Q["Event and state queue"] --> A["FIFO or Proposed-App"]
    A --> P["Native MinRTT or bound path selection"]
    P --> S["QUIC stream · two UDP paths"]
    S --> R["Receiver FIN · deadline and AoI"]
    R -->|"Application ACK"| A
    A --> O["Independent application oracle"]
    P --> O2["Independent path oracle"]
```

Proposed-App uses receiver ACKs, deadline budget, event severity and state freshness. It replaces older pending state. Custom path policies bind each stream to an initial path. Strict mode repairs on that path, while cross-path mode can repair over the alternate. Separate oracles replay the application choices and initial path choices.

## Results

### Stage 9 · Experimental infrastructure and data audit

Stage 9.3-A audited **288 XQUIC connections** across four development environments. It verified **6,072 labeled submissions**, including **1,688** with a positive remaining budget of at most 100 ms. Of 503 short-budget failures, 496 occurred with 25 ms or less remaining. The data support offline model development with Slack and SRTT as the starting features. Evidence around link transitions and at longer budgets remains sparse. The 48 sealed configurations remain closed with zero runs. [Research plan](docs/stage9-research-plan.md) · [Data and feature audit](docs/stage93a-data-quality-feature-analysis.md) · [Machine-readable results](results/stage93a/)

| Stage | Outcome |
| --- | --- |
| 9.1 | Validated the trace chain from scheduling decisions through actual XQUIC transmission and application delivery. [Acceptance report](docs/stage91-integrity-acceptance.md) |
| 9.2 and 9.2R | Engineering acceptance remained NO-GO on switch timing, tracing overhead and short-budget coverage. [Feasibility report](docs/stage92-feasibility-report.md) · [Reliability report](docs/stage92r-reliability-report.md) |
| 9.2R2 | Passed the engineering gates for 5 ms switch timing, ±5% Goodput equivalence and per-environment sample coverage. [Closure report](docs/stage92r2-targeted-closure-report.md) |

### Stage 8 · Independent validation and generalization

Stage 8 ran **400 formal connections** with 20 new seeds for paired comparisons and 20 separate seeds for randomized path exploration.

- High-load event deadline success was **5.00%** for FIFO + Native, **2.75%** for Proposed-App + Native, **3.00%** each for Fixed MinRTT and Earliest Completion, and **24.75%** for Proposed Probability. The advantage did not recur in the unseen reversal scenario.
- The high-load application–path interaction was **+5.00 percentage points** with Holm-adjusted `p = 0.0512`, just above the prespecified 0.05 threshold.
- Cross-path repair cut mean outage-probe completion from **1,544 ms to 253 ms**. Both means exceeded the **25 ms** deadline.
- The uncalibrated probability model did not consistently beat Slack-only. The offline calibrated model had insufficient statistical evidence of improvement.

![Path policies and observed mean AoI across two network environments](results/stage8/figures/01-path-comparison.png)

[Research report](docs/stage8-research-report.md) · [Final review](docs/stage8-final-review.md) · [Run metrics](results/stage8/runs.csv) · [Calibration figure](results/stage8/figures/03-reliability.png) · [Experiment audit](results/stage8/audit.json)

### Historical experiments

- **Stage 7** separated application and path scheduling across 600 formal runs, with 20 seeds for the high-load factorial. [Research report](docs/stage7-research-report.md) · [Archive audit](results/stage7/audit.json)
- **Stage 6 and earlier** archived 42 dynamic network and mechanism runs with captures from both interfaces and earlier technical reports. [Stage 6 report](docs/stage6-research-report.md) · [Stage 5 report](docs/stage5-report.md) · [Documentation](docs/)

## Quick start

Use Linux with network namespaces, veth and `sch_netem`. The archived runs used Ubuntu 24.04 on WSL2.

```bash
sudo apt-get install -y build-essential git cmake ninja-build iproute2 iputils-ping tcpdump openssl python3
git submodule update --init
bash scripts/build.sh
bash scripts/test.sh
sudo python3 experiments/run_stage8.py results/runs/stage8-pilot --suite pilot --jobs 5
```

The pilot runs 20 independent connections and checks scheduling decisions, endpoint traces and captures from both paths. [Environment setup](docs/experiment-environment.md)

## Reproduce the experiments

### Stage 9 archive replay

Use Python 3.12 or later to regenerate the four Stage 9.3-A outputs from the archived development runs. The reports above describe replay for the earlier Stage 9 suites.

```bash
bash scripts/test.sh --evidence results/stage91/final
python3 -B tests/stage92r_test.py
python3 -B tests/stage92r2_test.py
python3 -B tests/stage93a_test.py
python3 -B evaluation/stage93a.py results/runs/stage93a-recomputed
diff -rq results/stage93a results/runs/stage93a-recomputed
```

### Stage 8 experiments

Build from a clean code checkout to run the full paired and exploration matrices. Install the dependencies in `evaluation/stage6-requirements.txt` to render figures.

```bash
sudo python3 experiments/run_stage8.py results/runs/stage8-paired --suite paired --jobs 8
sudo python3 experiments/run_stage8.py results/runs/stage8-explore --suite explore --jobs 8
python3 evaluation/verify_stage8.py results/stage8 --archive
python3 evaluation/stage8.py results/stage8/raw --out results/runs/stage8-recomputed \
  --figures --compare results/stage8
```

[Experiment configuration](configs/stage8.json) · [Experimental plan](docs/stage8-experiment-plan.md) · [Reproduction details](docs/stage8-research-report.md#17-reproducibility-instructions--复现)

## Repository layout

| Directory | Contents |
| --- | --- |
| `src/` | Gateway queue, scheduling policies, XQUIC endpoints and workload generator |
| `network/` | Dual-veth topology and timed link changes |
| `experiments/`, `evaluation/` | Matrix runner, offline verification and figure generation |
| `patches/xquic/` | STREAM PTO scheduling, integrity tracing and measurement patches applied to the build copy |
| `results/`, `docs/` | Raw evidence, derived tables, figures and technical documentation |

## Scope

The two paths are emulated links on one host and the traffic is generated for replay. Event latency percentiles are conditional on completion inside the measurement window. Probability calibration uses observed outcomes on randomized paths and remains offline. Both endpoints use the pinned XQUIC `XQC_MULTIPATH_10` implementation. [XQUIC version](docs/xquic-api.md) · [Scheduling rules](docs/stage7-algorithm-spec.md)

## License

Project code and documentation use the [MIT License](LICENSE). XQUIC and BoringSSL retain their upstream licenses.
