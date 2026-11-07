# Stage 7 · IoT 多路径 QUIC 调度的贡献分离与重复验证

## 1. 摘要

在固定 XQUIC、CUBIC 和发送约束下，以应用队列和路径策略构建 2×2 因子实验。六个主场景各有 10 个配对种子，高负载扩展至 20 个，另做 10 种子部分丢包、定向修复、基线消融和参数敏感性对照。高负载时，路径策略单独提高事件按时率 18.25 个百分点，应用策略单独降低观测平均 AoI 696 ms，两者联合提高按时率 20.75 个百分点并降低 AoI 808 ms。在稳定、动态异构和状态主导条件下，事件按时改善主要来自应用层。事件主导时，应用层以约 2.3 个百分点的按时率代价换取约 258 ms 的 AoI 下降。定向中断中，开启跨路径修复的 10 次探针均在恢复前完整接收。两组探针均未满足 25 ms 截止。

## 2. 引言

一条 IoT 连接同时承载可被新值替换的状态和必须独立交付的事件。前者关心接收端掌握的信息有多旧，后者关心是否完整到达以及是否满足截止。Stage 6 将记录选择、初始路径和跨路径修复放在同一联合策略中，较难解释收益来源。Stage 7 将两个选择分开，并扩大每种条件下的独立重放数量。

## 3. 研究问题

1. 应用层和路径层单独改变时，各自影响哪些指标？两层叠加时，增益是否可相加？
2. 选中路径的完成分数能否预测实际按时接收？短剩余预算和路径变化时误差有多大？
3. 跨路径修复何时能提前完整接收？事件优先级、恢复和权重各有怎样的代价？

## 4. 相关工作

Kaul、Yates 和 Gruteser 将接收端掌握的最新生成时间用于量化状态年龄。Liu 和 Layland 的 EDF 工作提供了截止时间优先调度的经典参照，本实验的 EDF 应用基线则保留 QUIC 流控、并发和网络约束。 [AoI](https://doi.org/10.1109/INFCOM.2012.6195689) · [EDF](https://doi.org/10.1145/321738.321743)

Lim 等人的 ECF 说明异构链路下完成时间估计应考虑 RTT 以外的信息。这里将带宽、排队、丢包和记录截止预算用于 IoT Stream 的路径估计。QUIC 丢包和 PTO 的语义参照 RFC 9002。多路径互通的范围由固定 XQUIC 版本决定。 [ECF](https://www.repository.cam.ac.uk/items/359be341-09f9-4f1b-869c-d1d36eca498d) · [RFC 9000](https://www.rfc-editor.org/rfc/rfc9000.html) · [RFC 9002](https://www.rfc-editor.org/rfc/rfc9002.html) · [IETF Multipath QUIC draft](https://datatracker.ietf.org/doc/draft-ietf-quic-multipath/)

## 5. 系统架构

应用队列按重放时钟接纳记录。调度选出记录并成功建流后，提交状态才更新。每条记录占用一条单向 Stream，接收端以 FIN 时刻判定完成，并用反向 Stream 返回应用 ACK。事件去重使用来源与序号的复合键。两端各在两个 veth 接口上使用独立的 UDP Socket。 [算法规范](stage7-algorithm-spec.md)

## 6. 调度方法

Proposed-App 根据事件优先级、严重度、截止预算、状态新鲜度和等待时间打分，并保留状态替换和恢复配额。评分可在没有路径样本的情况下计算。Proposed-Path 为已选记录估计两路完成情况并绑定 Stream。原联合算法继续用路径概率加权应用评分，用于基线、消融和敏感性对照。Cross 实验只改变已绑定 Stream 的修复规则。

## 7. 实验环境

Ubuntu 24.04 WSL2，Linux `6.18.54.1-microsoft-standard-WSL2`。连接使用 CUBIC 和 pacing，应用发送窗口 8，初始单向 Stream credit 4，发送队列上限 24。主场景复用 Stage 5 的链路参数、反转和中断时序。实验共享一个端点构建，运行表保存端点 SHA-256、XQUIC、BoringSSL 和补丁指纹。 [运行表](../results/stage7/runs.csv) · [构建输入](../results/stage7/validation/build-sources-sha256.txt)

系统自带的 iproute2 6.1 无法设置 netem 种子。部分丢包用独立构建的 iproute2 6.18.0，对每个方向读取内核 qdisc 的种子。编号 UDP 探针验证了三个种子各重复两次时的相同掩码和种子间的不同掩码。共同随机流以 qdisc 入队包序号为索引，各调度器的包内容和到达顺序仍可不同。 [种子探针](../results/stage7/validation/netem-seed-probe.json) · [tc 指纹](../results/stage7/validation/netem-build-sha256.txt)

## 8. 实验设计

| 组 | 应用层 | 路径层 |
| --- | --- | --- |
| A | FIFO | 原生 MinRTT |
| B | Proposed-App | 原生 MinRTT |
| C | FIFO | Proposed-Path，严格亲和 |
| D | Proposed-App | Proposed-Path，严格亲和 |

同种子各组的重放文件逐字节一致，网络配置与固定 4 s 窗口一致。种子块内随机化策略次序，独立命名空间允许多个种子块并行。高负载扩展使用额外的 10 个种子。部分丢包的 12% 配置单列。负载为程序生成，包含温湿度状态、不同周期和大小的传感器数据、突发状态、多来源事件、事件严重度与截止预算。正式结果共 600 次连接。加入主 Pilot 后，统一分析覆盖 639 次连接、1,278 份抓包、30,095 条输入记录和 7,662 条预测。

事件按时率分母为窗口内生成的唯一事件。P50、P95 和 P99 基于窗口内完整接收的事件。AoI 按接收端实际生效的生成时间分段积分，从首次有效状态开始，并另报覆盖率。未知状态及超龄时间计入全窗最大年龄违约率。Jain 指数分别基于各传感器的有效交付比例和在龄时间比例。严格优先级的高负载运行未收到有效状态，故其 AoI 无定义。有效 goodput 只计应用 payload，UDP 统计独立记录。CPU 百分比以进程总运行时间为分母，包含日志与关闭时间。

配对单位为独立种子。区间为 4,000 次整种子配对 bootstrap 的 95% 分位数区间。显著性检验使用双侧配对符号翻转，10 种子时精确枚举，20 种子时固定随机种子抽样 20,000 次。六主场景中 B−A、C−A 和交互项的事件按时率及 AoI 检验组成 36 项 Holm 校正族。其余对照和区间用于估计和探索。 [实验设计](stage7-experiment-plan.md) · [统计表](../results/stage7/statistics/summary.csv)

## 9. 基线对比

下表为高混合负载下 10 个种子的运行均值。P95 是每次运行的条件 P95 的均值。

| 策略 | 事件按时率 | 事件 P95（ms） | 观测平均 AoI（ms） | 有效状态交付 Jain |
| --- | ---: | ---: | ---: | ---: |
| FIFO | 5.0% | 1,361 | 2,604 | 0.754 |
| Strict Priority | 7.5% | 2,933 | — | — |
| EDF | 0.0% | 2,683 | 2,898 | 0.424 |
| AoI-aware | 4.0% | 844 | 2,569 | 0.800 |
| Latest-RR | 5.0% | 2,934 | 1,629 | 0.800 |
| 原联合策略 | 49.5% | 1,605 | 1,782 | 0.402 |

联合策略提高按时事件比例，但有效状态交付更集中，观测 AoI 比 Latest-RR 高约 153 ms，事件 P95 也高于 FIFO 和 AoI-aware。 [基线图](../results/stage7/figures/04-baselines.png) · [公平性图](../results/stage7/figures/09-fairness.png)

## 10. 因子分析

事件按时率的变化以百分点计，AoI 的变化以毫秒计。交互项为 D−B−C+A。

| 场景 | 种子 | B−A 事件 | C−A 事件 | 交互事件 | D−A 事件 | D−A AoI |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 稳定 | 10 | +55.56 | 0.00 | 0.00 | +55.56 | −540 |
| 动态异构 | 10 | +53.33 | 0.00 | −10.00 | +43.33 | −578 |
| 高负载 | 20 | −2.00 | +18.25 | +4.50 | +20.75 | −808 |
| 路径中断 | 10 | +36.67 | 0.00 | −2.22 | +34.44 | −660 |
| 状态主导 | 10 | +46.67 | 0.00 | −6.67 | +40.00 | −559 |
| 事件主导 | 10 | −2.29 | 0.00 | 0.00 | −2.29 | −249 |
| 部分丢包 | 10 | +55.56 | 0.00 | 0.00 | +55.56 | −549 |

高负载时，C−A 的事件按时提升区间为 [17.00, 19.50] 个百分点。B−A 的 AoI 差值为 −696 ms，区间 [−718, −682] ms。D−A 的事件提升区间为 [19.50, 22.00] 个百分点。事件交互项的未校正区间为 [2.50, 6.50] 个百分点，Holm 校正后 `p=0.0544`。AoI 的交互项为 +281 ms，联合降低了 AoI，降幅小于两项单独降幅之和。高负载路径层的事件收益和两层各自的 AoI 变化通过了该校正族的 0.05 阈值。其余场景的区间应结合校正结果和负载结构解释。

事件主导时，B−A 的观测 AoI 降低 258 ms，但最大年龄违约比例仍约 93.7%。这与状态争取有限提交机会的代价相符，也表明降低观测 AoI 后仍可能长期违反新鲜度预算。 [条件对比](../results/stage7/figures/01-conditions.png) · [因子差值](../results/stage7/figures/02-factorial.png) · [AoI 与事件取舍](../results/stage7/figures/03-tradeoff.png) · [路径流量](../results/stage7/figures/05-path-traffic.png)

## 11. 概率校准

主因子矩阵中 C、D 的 2,991 次已提交预测，以完整接收为观测基础，未完成记录按失败计。原模型对有效预算的 Brier 为 0.0078、ECE 为 0.0326、ROC-AUC 为 0.9985。以绝对 deadline 为目标时，对应数值为 0.0499、0.0749 和 0.9483。模型按状态最大年龄与截止的较紧预算预测，绝对截止允许部分超龄状态随后到达，因此两种标签的误差不同。Brier 和 ECE 越低越好，ROC-AUC 衡量排序。 [校准方法](https://proceedings.mlr.press/v70/guo17a.html)

1,877 条预测在决策时已无有效预算。有效预算剩余 0–100 ms 的 158 条样本上，Brier 为 0.0892，AUC 为 0.8545。总体的高 AUC 包含由剩余预算即可判定的记录，区分两条候选路径能力还需要对同一记录随机分配初始路径，补齐未选路径的反事实。

按奇偶种子分离训练与测试，1,486 条独立测试种子上的预测用于比较十桶单调校准、原模型、训练总体成功率和仅用 slack 正负的基线。有效预算的单调校准 Brier 为 0.0048，slack 正负基线为 0.0079。单调映射只做离线验证，线上公式和选路权重保持原样。 [可靠性图](../results/stage7/figures/06-reliability.png) · [分层误差](../results/stage7/statistics/calibration-scores.csv) · [独立测试](../results/stage7/statistics/calibration-heldout.json)

## 12. 消融与跨路径修复

消融与完整联合策略在同一高负载条件下配对。关闭事件加权使事件按时率下降 25.5 个百分点，同时观测 AoI 降低 251 ms。固定初始路径使事件按时率下降 27.0 个百分点，观测 AoI 增加 206 ms。去掉路径概率权重使事件按时率下降 11.0 个百分点。去掉恢复分类在该条件下提高按时率 3.5 个百分点并降低 AoI 203 ms。AoI 打分、替换和跨路径修复的事件按时差值在这组高负载对照中接近零，各自仍可能影响公平性或其他场景。 [消融图](../results/stage7/figures/08-ablations.png)

定向中断的 Strict/Cross 对照共有 10 对。Cross 的 10 次均有实际跨路 STREAM 包在中断期间发出，平均每次 8.0 包。Strict 为 0。恢复前完整接收数平均从 0 增至 1.7，探针完整接收均值从 1,541 ms 降至 250 ms，配对差值 95% 区间为 [−1,317, −1,262] ms。首次有效事件与首次有效状态相对于中断开始分别提前约 1,291 ms 和 1,273 ms。Cross 的跨路包约占客户端 STREAM UDP 字节的 18.6%，重复 STREAM 偏移包由 21.0 降至 13.7。25 ms 探针截止依然过短。 [修复时间线](../results/stage7/figures/07-repair-timeline.png) · [真实发包记录](../results/stage7/metrics/repair-timeline.csv)

## 13. 参数敏感性

每个种子包含完整联合策略和 12 个单参数变体。分别改变 Recovery K、连续事件上限、严重度权重、等待权重、初始路径剩余预算余量及路径丢包估计倍率。区间使用同种子变体减默认的配对差值。结果按指标分别呈现。 [敏感性图](../results/stage7/figures/10-sensitivity.png) · [完整差值](../results/stage7/paired-deltas.csv)

事件上限从 3 调到 6 时，按时率增加 9.5 个百分点，观测 AoI 增加 130 ms。调到 1 时，按时率下降 8.0 个百分点，AoI 降低 247 ms。Recovery K 从 2 调到 4 时，按时率增加 5.5 个百分点，AoI 降低 344 ms。调整严重度权重、等待权重或初始路径余量时，事件按时变化均不超过 2.5 个百分点。丢包估计倍率两个变体的事件和 AoI 区间均跨零。这些结果来自一个高负载场景，默认参数保持原值。

## 14. 讨论

应用优先级在链路有足够余量时能把更多事件提前。状态替换与恢复同时改变待发队列及占用流控的时机。拥塞下，两类记录竞争同一并发窗口。此时路径层可绕开差路径，应用层的价值更多体现在状态年龄。严格亲和也限制了原生 MinRTT 能使用的修复路径，可能解释动态异构与状态主导中 B 的事件按时率高于 D。这里的路径对照估计的是动态选路与严格绑定这项组合的净效应。

事件加权和动态初始路径是本次高负载联合策略的主要按时收益来源。去掉恢复分类的局部收益值得在长期状态断流条件下复测。定向中断中的修复优势远大于高负载消融中的平均差值，适用条件是已经有数据被绑定到故障路径、备用路径仍可发送且等待恢复的代价较高。

## 15. 局限

链路为同主机双 veth，重放为合成负载，固定窗口包含最后一批生成后的尾部时间。事件主导和高负载目前仍存在明显截止或新鲜度违约。种子控制随机输入与配置，无法消除操作系统调度和实际 qdisc 切换时刻的差异。单因素消融和参数扫描限于一种高负载场景。校准来自已选路径及这一套拓扑，跨环境能力需要另行检验。

六次正式运行在测量截止后有未完成 Stream、关闭超时或对端先关闭。测量验证保留了原始窗口和未完成记录，按连接日志独立标记关闭异常。另有一次消融的外层脚本在证据已验证后退出。上述七次运行的开始和结束 UTC 从案例文件与终端日志的文件时间恢复，并在运行表中标记时间来源。其余 UTC 时间由运行器记录。

## 16. 结论

在当前场景中，应用层提升状态新鲜度和多种中低负载场景的按时事件比例，路径层在高负载异构条件下贡献更多按时收益，跨路径修复则对已经压在故障路径上的记录最有效。下一步应在正剩余预算的边界区间内随机化初始路径，并在独立网络配置上测试，以分离路径能力、调度选择和概率校准的贡献。

## 17. 复现

先按项目环境文档安装 Linux 依赖并构建。Python 绘图环境沿用 `evaluation/stage6-requirements.txt`。以下命令使用默认构建目录，若使用缓存构建目录则将相同的 `IOT_BUILD_DIR` 传入运行器。

```bash
bash scripts/test.sh
sudo python3 experiments/run_stage7.py results/runs/stage7/pilot --suite pilot
sudo python3 experiments/run_stage7.py results/runs/stage7/factorial --suite factorial --jobs 8
sudo python3 experiments/run_stage7.py results/runs/stage7/high-extension --suite high-extension --jobs 5
sudo python3 experiments/run_stage7.py results/runs/stage7/repair --suite repair --jobs 8
sudo python3 experiments/run_stage7.py results/runs/stage7/ablation --suite ablation --jobs 6
sudo python3 experiments/run_stage7.py results/runs/stage7/sensitivity --suite sensitivity --jobs 8
```

部分丢包使用支持 `netem seed` 的 `tc`。归档使用 [iproute2 6.18.0 源码](https://cdn.kernel.org/pub/linux/utils/net/iproute2/iproute2-6.18.0.tar.xz) 构建的二进制。先运行掩码探针，再运行部分丢包矩阵。

```bash
sudo python3 network/scenarios/verify_seeded_netem.py --tc /path/to/seed-capable/tc
sudo env IOT_NETEM_TC=/path/to/seed-capable/tc \
  python3 experiments/run_stage7.py results/runs/stage7/loss --suite loss --jobs 4
python3 evaluation/verify_stage7.py results/stage7 --archive
python3 evaluation/stage7.py results/stage7/raw/{pilot,factorial,high-extension,loss,repair,ablation,sensitivity} \
  --out results/runs/stage7-recomputed --figures
```

归档另保存 4 次部分丢包 Pilot，与正式统计分开。Stage 1–5 的 11 次实网回归、9 项 CTest、5 项 Python 测试通过。Stage 6 的 42 次归档运行从原始文件复算通过，733 项源文件哈希一致。 [实验清单](../results/stage7/experiment-manifest.json) · [逐记录结果](../results/stage7/metrics/records.csv) · [归档审计](../results/stage7/audit.json) · [验收日志](../results/stage7/validation/)
