# Stage 8 · IoT 多路径 QUIC 调度的独立验证与泛化

## 1. Abstract · 摘要

本阶段使用独立 Oracle 重放应用层和初始路径决策，并完成 320 次配对运行和 80 次随机路径探索。高负载下，概率选路比同为严格亲和的 Fixed MinRTT 和 Earliest Completion 各提高 21.75 个百分点的事件按时率。新反转场景中的概率选路优势未复现。高负载事件交互项为 +5.00 个百分点，Holm 校正后 `p=0.0512`。原始概率模型在独立样本上未稳定优于 slack-only，尤其在 0–100 ms 的短预算区间。

## 2. Introduction · 引言

IoT 网关同时发送有截止时间的事件和可以被新值替换的状态。Stage 7 将队列排序与初始路径分开，发现高负载时路径层贡献较多事件按时收益，但原生 MinRTT 与 Proposed-Path 之间还混有路径亲和和修复机制的差异。Stage 8 用共享亲和机制的路径基线拆解这一比较，并把原模型与简单预测器放在同一批随机化样本上比较。 [Stage 7 报告](stage7-research-report.md)

## 3. Research Questions · 研究问题

1. 应用选择、路径选择和跨路径修复各自能解释多少事件按时率和 AoI 的变化？
2. 在独立种子和新反转配置下，Stage 7 的高负载交互效应是否复现？
3. 对已实际分配的路径，完成概率的预测误差是否低于 slack-only、RTT-only 和最短完成时间基线？短预算样本是否给出相同答案？

## 4. Related Work · 相关工作

MPTCP 和 Multipath QUIC 为一条连接使用多个路径提供传输机制。RFC 9000 和 RFC 9002 界定 QUIC 流、丢包检测和 PTO。实验运行固定的 XQUIC MPQUIC 实现。 [RFC 8684](https://www.rfc-editor.org/rfc/rfc8684.html) · [RFC 9000](https://www.rfc-editor.org/rfc/rfc9000.html) · [RFC 9002](https://www.rfc-editor.org/rfc/rfc9002.html) · [Multipath QUIC](https://datatracker.ietf.org/doc/draft-ietf-quic-multipath/)

ECF 在异构 MPTCP 中用预计完成时间改进路径排序。AoI 描述接收端已知状态的年龄，EDF 则提供经典的截止时间优先参照。本文将两类应用目标与初始 Stream 绑定和修复对照结合。 [ECF](https://www.repository.cam.ac.uk/items/359be341-09f9-4f1b-869c-d1d36eca498d) · [AoI](https://doi.org/10.1109/INFCOM.2012.6195689) · [EDF](https://doi.org/10.1145/321738.321743)

## 5. System Architecture · 系统架构

每条记录用一条单向 Stream。应用选择在创建 Stream 前完成，成功建流才提交队列和配额。初始路径策略为该 Stream 选择 home，并在首个写操作前登记绑定。服务端以完整 FIN 作为交付时刻，使用独立 ACK Stream 返回接收结果。原生臂把数据包交给 XQUIC MinRTT，自定义臂共用同一个路径亲和回调。控制包和 ACK 仍走原生调度。

## 6. Mathematical Model · 数学模型

设决策时刻为 `t`，事件预算为 `s = d − t`，状态预算为 `s = min(d, g + M) − t`。其中 `d` 为截止时刻，`g` 为生成时刻，`M` 为最大状态年龄。路径的 `R` 是 SRTT，`Q` 为已调度字节与在途字节之和，`B` 为应用头加 payload，`v` 为 XQUIC 速率估计（B/s）。时间统一用微秒。

```text
T = (0.5 + 2 × I[PTO > 0]) × R + 1e6 × (Q + B) / max(v, 15000)
V = max(RTTVAR, 1000)
L = min(0.95, (recent_lost + 1) / (recent_sent + 20))
P = 0                                         if s <= 0
P = clip((s - T + V) / (2V), 0, 1) × (1-L)^ceil(B/1200)  otherwise
```

Proposed-App 沿用 Stage 7 规范中的等待、新鲜度、严重度及配额。普通候选的应用紧迫度项为 `100000 / (100000 + max(0, s))`。恢复候选使用状态年龄改善与超龄项。原联合算法仍由旧入口提供。 [Stage 7 算法规范](stage7-algorithm-spec.md)

## 7. Scheduling Algorithms · 调度算法

- **Fixed MinRTT** 在当前可用路径中按 SRTT 最小值选初始路径并绑定。这里的 Fixed 指每条 Stream 绑定固定，后续 Stream 仍可重选。
- **Earliest Completion** 选择最小 (T)，考虑 RTT、PTO、在途、排队、字节数和速率。
- **Proposed Probability** 最大化 (P)，概率相同时选更小的 (T)。同分取可用路径列表首项。
- **Cross Repair** 初始规则与 Proposed Probability 相同。修复按既有 PTO、拥塞窗口及预算条件尝试备用路径。
- **Random Path** 对可用路径做等概率分配。以场景、负载、种子派生独立 assignment seed，再按记录 ID、预算桶和 payload 桶混合出索引。

## 8. Experimental Methodology · 实验方法

Ubuntu 24.04 WSL2 上运行真实 XQUIC，使用两个 Linux netns、两对 veth 和实际 UDP 数据包。配对实验使用种子 8101–8120，高混合负载复用历史的中断反转，另有此前未使用的两次带宽与时延反转。每个种子内固定重放文件、链路时间表、CUBIC、pacing、初始单向 Stream credit 4、并发窗口 8、发送队列上限 24 和 4 秒窗口，策略顺序打乱。独立随机探索使用 8201–8220，覆盖两个场景和轻、重两种负载。Pilot 8091 单列。 [预定设计](stage8-experiment-plan.md) · [清单与指纹](../results/stage8/manifest.json)

## 9. Baselines · 对照组

| 单元 | 应用 | 路径与修复 |
| --- | --- | --- |
| A | FIFO | 原生 MinRTT |
| B | Proposed-App | 原生 MinRTT |
| C | FIFO | Probability + 严格亲和 |
| D | Proposed-App | Probability + 严格亲和 |
| Fixed | Proposed-App | Fixed MinRTT + 严格亲和 |
| Earliest | Proposed-App | Earliest Completion + 严格亲和 |
| Cross | Proposed-App | Probability + 跨路修复 |

B−A、C−A 和 D−B−C+A 衡量应用层、路径层及其交互。C−A 同时包含初始选择、绑定和原生修复机制的变化。D−Fixed 与 D−Earliest 固定亲和和修复语义。Cross−D 单独评估修复变化。

## 10. Independent Validation · 独立验证

应用 Oracle 用二进制输入、准入批次和 ACK 日志重建 pending 集合与反馈。候选评分用有理数表示，按类别、配额和评分排序。阻塞尝试保留队列和配额，成功提交后推进两类计数。每次 ACK 验证前后版本与发送端可见 AoI。路径 Oracle 从候选计数独立重算延迟、丢包与概率，验证每个初始绑定和随机分配，并将绑定与实际 XQUIC STREAM 发送和抓包互证。修复检验覆盖实际跨路发送与流亲和约束，原生回调作为上游黑盒运行。

Stage 7 的六次非标准关闭以及一次脚本退出均单独复算。所有可定时的关闭、未完成 Stream、超时事件在 4 秒截止之后，窗口内指标与保存表一致。7106/B 的拒绝接收来自无独立时间戳的 stderr，客户端先记录窗口截止，错误随后退出主循环。一次消融的两个端点正常结束，外层脚本在验证通过后出错。七次文件时间来源保留标记。进程生命周期 CPU 包含关闭等待，尤其是两次服务端超时。Stage 7 的 12,363 项归档哈希复核通过，Git 树与 Stage 8 开工前一致。 [逐条历史审计](../results/stage8/validation/stage7-special-runs.json)

## 11. Experimental Results · 实验结果

统一分析覆盖 420 次连接、840 份双接口抓包和 29,043 条输入记录。独立验证检查了 36,092 次应用决策和 7,973 次初始路径绑定。第一轮 Pilot 的七次未通过运行单独保留。它们暴露了旧日志只记录首次建流阻塞的问题，补齐后 20 次 Pilot 全部通过。 [归档审计](../results/stage8/audit.json) · [真实日志篡改测试](../results/stage8/validation/log-mutations.txt)

| 策略 | 高负载按时率 | 高负载 AoI | 新反转按时率 | 新反转 AoI |
| --- | ---: | ---: | ---: | ---: |
| A · FIFO + Native | 5.00% | 2,607 ms | 0.00% | 1,836 ms |
| B · Proposed-App + Native | 2.75% | 1,971 ms | 0.56% | 1,395 ms |
| C · FIFO + Probability | 22.00% | 2,278 ms | 0.00% | 1,835 ms |
| Fixed | 3.00% | 2,158 ms | 0.93% | 1,393 ms |
| Earliest | 3.00% | 2,157 ms | 0.74% | 1,390 ms |
| D · Probability | 24.75% | 1,826 ms | 0.65% | 1,378 ms |
| Cross | 24.50% | 1,807 ms | 0.74% | 1,387 ms |

新反转场景使用更短截止的密集突发，所有策略的事件按时率都低于 1%。应用层在该条件下仍明显缩短状态 AoI。窗口内延迟分位数、状态交付、逐路径流量、有效 goodput、丢包和重传代理均保存在运行表。 [策略对比图](../results/stage8/figures/01-path-comparison.png) · [运行表](../results/stage8/runs.csv)

## 12. Statistical Analysis · 统计分析

主指标为窗口内事件按时率及接收端观测平均 AoI。每个种子为一个配对单位。采用 4,000 次整种子 bootstrap、配对标准化效应量 `d_z` 和双侧符号翻转检验，20 种子用固定随机种子抽样 20,000 次。预定的两场景、六对照、两指标共 24 项检验使用 Holm 校正。事件和记录延迟分位数仅以窗口内完成为条件，未完成结果仍进入按时率分母。AoI 的观测覆盖率和最大年龄违约率单独记录。 [配对表](../results/stage8/paired-deltas.csv) · [估计与检验](../results/stage8/statistics/summary.csv)

高负载下，B−A 的 AoI 为 −636 ms，95% 区间 [−693, −529] ms。事件按时率下降 2.25 个百分点，Holm 校正后 `p=0.0512`。C−A 的事件按时率提高 17.00 个百分点，区间 [16.00, 18.00]，校正后 `p=0.0012`。D 相对 Fixed 和 Earliest 分别提高 21.75 个百分点，两项区间均为 [19.75, 23.75]，校正后 `p=0.0012`。D−A 的 AoI 降低 781 ms，事件按时率提高 19.75 个百分点。

高负载事件交互项为 +5.00 个百分点，区间 [2.25, 7.50]，配对效应量 `d_z=0.855`，Holm 校正后 `p=0.0512`。Stage 7 的点估计方向复现，两阶段校正后的检验均未达到 0.05。高负载 AoI 交互项为 +184 ms，校正后 `p=0.0812`。在新反转中，B−A 的 AoI 为 −441 ms，校正后 `p=0.0012`，概率选路相对两个绑定式基线的事件差值均接近零。 [配对效应图](../results/stage8/figures/02-paired-effects.png)

## 13. Ablation Study · 机制消融

高负载与新反转的 Cross−D 主指标均未达到预定显著性阈值。定向中断提供了不同证据。在 20 对探针中，严格组全部等到 A 路恢复之后完成。跨路组全部在恢复前完成，且每次中断期间均有 8 个已核对的跨路 STREAM 发包。探针均值由 1,544 ms 降到 253 ms，配对差值区间为 [−1,314, −1,267] ms。两组均超过探针的 25 ms 截止。

跨路组在恢复前每次多完成 1.75 条记录。首次有效事件和状态分别提前约 1,287 ms、1,274 ms，重复 STREAM offset 的 UDP 字节均值从 24.1 kB 降至 15.1 kB。这个收益出现在已绑定路径中断、备用路径仍可发送的条件。 [修复时间线](../results/stage8/figures/04-repair-timeline.png) · [逐包时间表](../results/stage8/statistics/repair-timeline.csv)

## 14. Calibration Analysis · 校准分析

探索数据按奇数种子训练、偶数种子测试。只将有两条可用路径且实际选中该路径的记录计为一个观察。对未选路径仅存预测和特征，结果为空。Slack-only、RTT-only、Earliest 分别以 `s`、`R`、`s − T` 为单一特征拟合逻辑回归。Platt 用原始 `P` 的 logit 拟合。训练和诊断使用 0.1 标准化斜率惩罚，log loss 与 logit 在 `[10⁻⁶, 1 − 10⁻⁶]` 截断，Brier 和 ECE 使用原预测。校准只在离线运行。 [校准图](../results/stage8/figures/03-reliability.png) · [分层统计](../results/stage8/statistics/calibration-scores.csv) · [分配记录](../results/stage8/statistics/allocation.csv)

随机探索实际观测 A 路 1,044 条、B 路 1,015 条。两边都有完整与未按时结果。独立测试包含 1,031 条，正剩余预算 0–100 ms 有 105 条。下表是同一批记录的 Brier，数值越低表示预测误差越小。

| 模型 | 绝对截止，全部 | 绝对截止，0–100 ms | 有效预算，全部 | 有效预算，0–100 ms |
| --- | ---: | ---: | ---: | ---: |
| Slack-only | 0.04242 | 0.20462 | 0.04158 | 0.24074 |
| RTT-only | 0.13089 | 0.49974 | 0.11885 | 0.49904 |
| Earliest Completion | 0.04521 | 0.21961 | 0.04519 | 0.25765 |
| Current Probability | 0.05589 | 0.26985 | 0.04131 | 0.26002 |
| Calibrated Probability | 0.04670 | 0.19536 | 0.03547 | 0.21786 |

原概率预测绝对截止的误差高于 slack-only，预测有效预算时整体接近，短预算误差也较高。离线校准降低了有效预算 Brier，整体相对 slack-only 的种子平均差为 −0.00581，95% 区间 [−0.00964, −0.00156]，校正后 `p=0.3145`。绝对截止的总体 Brier 仍由 slack-only 领先。两个目标在短预算上的校准优势区间均跨零。测试表还报告 log loss、ECE、ROC-AUC、校准截距和斜率。 [按种子比较](../results/stage8/statistics/calibration-paired-brier.csv) · [训练系数](../results/stage8/statistics/calibration-models.json)

## 15. Limitations · 局限

这是同一台机器的受控仿真链路，网关负载为合成重放，结果适用于所测拥塞控制和拓扑。多个隔离种子块可能并行运行并共享宿主机 CPU。随机分配提供在该探索策略下的实际路径样本，先前记录会影响后续队列，不能由单条反事实预测直接推断整个在线策略。短预算样本仍随负载和有效 Stream credit 变化。重复 STREAM offset 的 UDP 字节为重传开销代理，未重建全部 QUIC 帧的重叠区间。八次运行的接收端在截止后关闭了未完成 Stream，已保留窗口内未交付记录并在运行表中标记。

## 16. Conclusion · 结论

高负载的事件改善主要来自路径层，应用层主要改善状态 AoI。概率选路对同为严格亲和的简单路径算法有场景内优势，换到短截止的反转负载后该优势未延续。两阶段对高负载正交互均只给出方向性证据。保留跨路修复有明确的定向中断依据。原模型仍有校准余地，独立验证支持在后续实验中重点扩大正剩余预算的边界样本，并将训练与测试按网络环境进一步隔离。

## 17. Reproducibility Instructions · 复现

需要项目现有的 Linux 工具和固定 XQUIC 子模块。正式实验的源码提交为 `d1fb64c48690d65f8dc30c1e412a4c5094ea9dca`。在干净 checkout 上构建并运行实验，在含完整 Stage 8 分析代码的分支版本上复算归档。以下命令使用默认构建目录。

```bash
git submodule update --init
bash scripts/build.sh
bash scripts/test.sh
sudo python3 experiments/run_stage8.py results/runs/stage8/pilot --suite pilot --jobs 5
sudo python3 experiments/run_stage8.py results/runs/stage8/paired --suite paired --jobs 8
sudo python3 experiments/run_stage8.py results/runs/stage8/explore --suite explore --jobs 8
python3 evaluation/verify_stage8.py results/stage8 --archive
python3 evaluation/stage8_history.py results/stage7
python3 evaluation/stage8.py results/stage8/raw --out results/runs/stage8-recomputed \
  --figures --compare results/stage8
```

复算需要 `tcpdump`。图表使用 `evaluation/stage6-requirements.txt` 固定的 matplotlib。 [逐运行指标](../results/stage8/runs.csv) · [逐记录证据](../results/stage8/records.csv) · [路径候选](../results/stage8/path-predictions.csv) · [归档审计](../results/stage8/audit.json)

## 18. References · 参考文献

- Ford 等，Multipath TCP v1。 [RFC 8684](https://www.rfc-editor.org/rfc/rfc8684.html)
- Iyengar 与 Thomson，QUIC Transport。 [RFC 9000](https://www.rfc-editor.org/rfc/rfc9000.html)
- Iyengar 与 Swett，QUIC Loss Detection and Congestion Control。 [RFC 9002](https://www.rfc-editor.org/rfc/rfc9002.html)
- IETF QUIC 工作组，Multiple Paths for QUIC。 [Internet-Draft](https://datatracker.ietf.org/doc/draft-ietf-quic-multipath/)
- Lim 等，ECF。 [正式出版记录与作者稿](https://www.repository.cam.ac.uk/items/359be341-09f9-4f1b-869c-d1d36eca498d)
- Kaul、Yates 与 Gruteser，Real-Time Status。 [INFOCOM 2012](https://doi.org/10.1109/INFCOM.2012.6195689)
- Liu 与 Layland，硬实时多程序调度。 [JACM 1973](https://doi.org/10.1145/321738.321743)
- Guo 等，概率校准、可靠性图和 ECE。 [ICML 2017](https://proceedings.mlr.press/v70/guo17a.html)
