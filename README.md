# IoT-MPQUIC-Scheduler

**English** | [简体中文](README.zh-CN.md)

**Deadline- and freshness-aware multipath QUIC scheduling for IoT traffic.**

IoT-MPQUIC-Scheduler sends periodic sensor updates and independent events over one QUIC connection with two real UDP paths. It measures whether joint record scheduling and path selection can improve event deadline delivery while keeping sensor data fresh as link conditions change.

[Results](#results) · [Quick start](#quick-start) · [Reproduce](#reproduce-the-experiments) · [Technical report](docs/stage6-research-report.md)

## How it works

- **Application scheduling.** Pending sensor updates can be replaced by newer values. Events retain independent IDs. The joint policy uses receiver ACK feedback, age of information (AoI), deadline budget, event severity and path estimates. A configurable recovery threshold and a service quota reserve submission opportunities for state updates.
- **Path scheduling.** Each record uses one unidirectional QUIC stream. The client binds its initial transmission to one path. Strict affinity keeps repair on that path, while cross-path mode can send repair packets on the other path. The receiver judges deadlines at complete record reception and returns a separate application ACK.
- **Evaluation.** Five application baselines cover FIFO, strict priority, earliest deadline first, AoI-aware scheduling and latest-value replacement with round robin. The experiment runner replays the same seeded workload across policies and records both endpoints and both interfaces.

## Results

| Comparison | FIFO or control | Joint scheduler |
| --- | ---: | ---: |
| On-time events across six link scenarios, mixed workload, seed 17 | 23/54 | **49/54** |
| Mean AoI across the same six scenarios, equal weight per scenario | 1.329 s | **0.907 s** |
| On-time events under high mixed load, seeds 17 and 29 | 1/20 in each run | **10/20 in each run** |
| Probe completion during a path outage, cross-path repair off vs. on | 1,529.6 ms | **338.0 ms** |

![Event deadline delivery and mean AoI across six link scenarios](results/stage6/figures/01-dynamic-scenarios.png)

In the event-only burst, the joint policy improved the on-time count from 11/18 to 14/18 while event P95 increased from 1.36 s to 2.34 s. Under high mixed load, it also had a lower Jain index for useful state delivery than FIFO. The outage probe completed before path A recovered with cross-path repair enabled. Both modes exceeded its 25 ms deadline.

The archive contains **42 runs, 84 interface captures and 1,830 input records**. [Technical report and all figures](docs/stage6-research-report.md) · [Run metrics](results/stage6/runs.csv) · [Per-record data](results/stage6/records.csv) · [Paired deltas](results/stage6/paired-deltas.csv)

Stage 7 separates application and path scheduling, repeats the factorial across independent seeds, and evaluates cross-path repair and completion calibration. [Stage 7 report](docs/stage7-research-report.md) · [Stage 7 audit](results/stage7/audit.json)

## Quick start

Use a Linux shell with network namespace, veth and `sch_netem` support. The archived runs used Ubuntu 24.04 on WSL2. From the repository checkout:

```bash
sudo apt-get update
sudo apt-get install -y build-essential git cmake ninja-build iproute2 iputils-ping tcpdump openssl python3
git submodule update --init
bash scripts/build.sh
sudo bash scripts/run-stage1.sh
```

The smoke test opens both paths, reverses the link conditions during the connection and checks the captures against the transport logs. The runner prints the artifact directory. [Environment setup and WSL build paths](docs/experiment-environment.md)

## Reproduce the experiments

```bash
sudo python3 experiments/run_stage5.py results/runs/stage5-seed17 --seed 17
sudo python3 experiments/run_stage5.py results/runs/stage5-seed29 --seed 29 --suite high
sudo python3 experiments/run_stage5.py results/runs/stage5-repair17 --seed 17 --suite repair
```

Each run saves the workload, `tc netem` settings and change times, endpoint logs and two captures. To check the archived main matrix offline with `tcpdump` installed:

```bash
python3 evaluation/verify_stage5.py results/stage5-wsl/seed17 --matrix
```

[Experiment configuration](configs/stage5.json) · [Metric definitions and ablations](docs/stage5-report.md) · [Rebuild the figures and data tables](docs/stage6-research-report.md#6-数据索引与复现)

## Repository layout

| Directory | Contents |
| --- | --- |
| `src/` | Gateway queue, scheduling policies, XQUIC endpoints and workload generator |
| `network/` | Dual-veth topology and timed link changes |
| `experiments/`, `evaluation/` | Matrix runner, offline verification and figure generation |
| `patches/xquic/` | Opt-in STREAM PTO scheduling patch, applied to the build copy |
| `results/`, `docs/` | Raw evidence, derived tables, figures and technical documentation |

## Scope

The two paths are emulated IP links on one host. The six-scenario comparison uses one workload seed, and the high-load comparison uses two. Random netem packet drops varied between runs in the archived WSL environment. The project runs the pinned XQUIC implementation of `XQC_MULTIPATH_10`, with both endpoints using the same build. [XQUIC version and API details](docs/xquic-api.md) · [Scheduling rules](docs/algorithm-spec.md)

## License

Project code and documentation use the [MIT License](LICENSE). XQUIC and BoringSSL retain their upstream licenses.
