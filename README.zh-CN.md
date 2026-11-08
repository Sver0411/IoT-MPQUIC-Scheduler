# IoT-MPQUIC-Scheduler

[English](README.md) | **简体中文**

**面向 IoT 流量的截止时间与信息新鲜度感知多路径 QUIC 调度。**

IoT-MPQUIC-Scheduler 在同一条 QUIC 连接上通过两条独立的 UDP 路径传输周期传感器状态和独立事件，用来评估链路条件变化时，联合记录调度与路径选择能否同时改善事件按时交付和状态新鲜度。

[实验结果](#实验结果) · [快速开始](#快速开始) · [复现实验](#复现实验) · [最新报告](docs/stage93a-data-quality-feature-analysis.md)

## 工作方式

```mermaid
flowchart LR
    Q["事件与状态队列"] --> A["FIFO 或 Proposed-App"]
    A --> P["原生 MinRTT 或绑定式选路"]
    P --> S["QUIC Stream · 双 UDP 路径"]
    S --> R["接收端 FIN · 截止与 AoI"]
    R -->|"应用 ACK"| A
    A --> O["独立应用 Oracle"]
    P --> O2["独立路径 Oracle"]
```

Proposed-App 使用接收端 ACK、截止预算、事件严重度和状态新鲜度，并在队列内替换旧状态。自定义路径策略为每条 Stream 绑定初始路径。严格模式沿原路径修复，跨路径模式可以在备用路径发送修复包。两套 Oracle 分别重放实际队列选择与初始路径选择。

## 实验结果

### Stage 9 · 实验基础设施与数据审查

Stage 9.3-A 回放核对四个开发环境中的 **288 次 XQUIC 连接运行**。共有 **6,072 条**提交记录获得有效标签，其中 **1,688 条**剩余预算大于 0 且不超过 100 ms。503 个短预算失败中，496 个集中在 25 ms 以内。现有数据支持以 Slack 和 SRTT 为起点开展离线模型开发，链路切换附近和较长预算区间的证据仍有限。48 条封存配置保持关闭，执行次数为零。 [研究计划](docs/stage9-research-plan.md) · [数据质量与特征报告](docs/stage93a-data-quality-feature-analysis.md) · [机器可读结果](results/stage93a/)

| 阶段 | 结果 |
| --- | --- |
| 9.1 | 验证从调度决策到 XQUIC 实际发包、应用交付的追踪链。 [验收报告](docs/stage91-integrity-acceptance.md) |
| 9.2 与 9.2R | 切换时序、追踪开销与短预算覆盖尚未同时满足工程门槛，判定为 NO-GO。 [可行性报告](docs/stage92-feasibility-report.md) · [可靠性报告](docs/stage92r-reliability-report.md) |
| 9.2R2 | 通过 5 ms 切换精度、±5% Goodput 等效性及环境级样本覆盖门槛。 [定向验收报告](docs/stage92r2-targeted-closure-report.md) |

### Stage 8 · 独立验证与泛化

Stage 8 完成 **400 次正式连接实测**。配对实验使用 20 个新种子，随机路径探索使用另外 20 个种子。

- 高负载事件按时率分别为 FIFO + Native **5.00%**、Proposed-App + Native **2.75%**、Fixed MinRTT 与 Earliest Completion 各 **3.00%**、Proposed Probability **24.75%**。新反转场景未复现该优势。
- 高负载的应用层与路径层交互为 **+5.00 个百分点**。Holm 校正后 `p = 0.0512`，略高于预设的 0.05 阈值。
- 跨路径修复将中断探针的平均完成时间从 **1,544 ms 降到 253 ms**，两组均超过 **25 ms** 截止。
- 原始概率模型未稳定优于 Slack-only，离线校准模型的改善也缺少充分的统计证据。

![两种网络条件下的路径策略与观测 AoI](results/stage8/figures/01-path-comparison.png)

[研究报告](docs/stage8-research-report.md) · [最终审查](docs/stage8-final-review.md) · [逐次指标](results/stage8/runs.csv) · [校准图](results/stage8/figures/03-reliability.png) · [实验审计](results/stage8/audit.json)

### 历史实验

- **Stage 7** 分开评估应用和路径调度，完成 600 次正式实测，重点高负载有 20 个种子。 [研究报告](docs/stage7-research-report.md) · [归档审计](results/stage7/audit.json)
- **Stage 6 及之前** 保存 42 次动态网络和机制验证运行，包含双接口抓包和更早阶段的报告。 [Stage 6 报告](docs/stage6-research-report.md) · [Stage 5 报告](docs/stage5-report.md) · [文档目录](docs/)

## 快速开始

在支持网络命名空间、veth 和 `sch_netem` 的 Linux 环境中运行。归档使用 Ubuntu 24.04 WSL2。

```bash
sudo apt-get install -y build-essential git cmake ninja-build iproute2 iputils-ping tcpdump openssl python3
git submodule update --init
bash scripts/build.sh
bash scripts/test.sh
sudo python3 experiments/run_stage8.py results/runs/stage8-pilot --suite pilot --jobs 5
```

Pilot 会运行 20 次独立连接，并核对队列决策、路径决策、端点日志和双接口抓包。 [环境配置](docs/experiment-environment.md)

## 复现实验

### Stage 9 归档回放

使用 Python 3.12 或更高版本，从开发集原始归档重算 Stage 9.3-A 的四份结果。更早的 Stage 9 套件复算步骤见各阶段报告。

```bash
bash scripts/test.sh --evidence results/stage91/final
python3 -B tests/stage92r_test.py
python3 -B tests/stage92r2_test.py
python3 -B tests/stage93a_test.py
python3 -B evaluation/stage93a.py results/runs/stage93a-recomputed
diff -rq results/stage93a results/runs/stage93a-recomputed
```

### Stage 8 实验

在干净的代码 checkout 上构建后，运行完整配对和探索矩阵。安装 `evaluation/stage6-requirements.txt` 中的依赖可重新绘图。

```bash
sudo python3 experiments/run_stage8.py results/runs/stage8-paired --suite paired --jobs 8
sudo python3 experiments/run_stage8.py results/runs/stage8-explore --suite explore --jobs 8
python3 evaluation/verify_stage8.py results/stage8 --archive
python3 evaluation/stage8.py results/stage8/raw --out results/runs/stage8-recomputed \
  --figures --compare results/stage8
```

[实验配置](configs/stage8.json) · [实验计划](docs/stage8-experiment-plan.md) · [复现说明](docs/stage8-research-report.md#17-reproducibility-instructions--复现)

## 项目结构

| 目录 | 内容 |
| --- | --- |
| `src/` | Gateway 队列、调度策略、XQUIC 端点和负载生成器 |
| `network/` | 双 veth 拓扑和定时链路变更 |
| `experiments/`、`evaluation/` | 实验矩阵、离线验收和绘图 |
| `patches/xquic/` | 对构建副本应用的 STREAM PTO 调度、完整性追踪和测量补丁 |
| `results/`、`docs/` | 原始证据、整理后的数据、图表及技术文档 |

## 实验范围

两条路径为同一宿主机上的仿真链路，业务负载为合成重放。事件延迟分位数以窗口内完整交付为条件。路径概率校准来自随机分配的已观测路径，保留在线原公式。两端运行固定的 XQUIC `XQC_MULTIPATH_10` 实现。 [XQUIC 版本](docs/xquic-api.md) · [调度规则](docs/stage7-algorithm-spec.md)

## 许可证

项目代码和文档使用 [MIT 许可证](LICENSE)。XQUIC 和 BoringSSL 各自保留上游许可证。
