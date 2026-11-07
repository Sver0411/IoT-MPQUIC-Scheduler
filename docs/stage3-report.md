# Stage 3 调度基线与对照实验

五种应用调度基线通过统一接口接入 Gateway。固定版本 XQUIC、QUIC 路径选择、接收处理、发送窗口和协议 credit 由各组共享。策略规则与 AoI 公式见 [调度算法规范](algorithm-spec.md)。

## 复现

```bash
bash scripts/build.sh
cmake --build "${IOT_BUILD_DIR:-build}/app" --target iot-scheduler-test
ctest --test-dir "${IOT_BUILD_DIR:-build}/app" --output-on-failure
sudo bash scripts/run-stage3.sh
python3 evaluation/verify_stage3.py --compare RESULTS_DIRECTORY
```

如果使用自定义构建目录，运行命令为 `sudo env IOT_BUILD_DIR="$IOT_BUILD_DIR" bash scripts/run-stage3.sh`。默认 seed 为 17，可用 `IOT_SEED` 指定。每组对照生成一次输入，按 [策略清单](../configs/stage3-policies.txt) 顺序启动独立的双路径连接，保存每个策略的日志和 veth 抓包。离线验收重新计算每次成功的提交决定，校验完整收发、ACK 与路径证据，并核对五组输入哈希、二进制哈希、netem 和流控条件。

## 实验条件

使用 Ubuntu 24.04 WSL2 和 Stage 1 的初始链路配置。A 为单向 4 ms / 30 Mbit/s，B 为 24 ms / 12 Mbit/s，两端各自应用相同的出口配置。应用发送并发上限 8，初始单向 Stream credit 4，发送缓冲 24 个包。负载含集中事件和周期状态，事件截止时间采用 100、240、420、750 和 1,100 ms，四个传感器的最大年龄为 80、120、200、120 ms。

## 指标口径

- 事件按 `event_id` 取首次完整接收，用服务端 FIN 判定是否按时。时延从生成时刻算到服务端 FIN，P95 使用最近秩。
- 状态按时数的分母为所有生成的状态记录，被队列替换的记录也在分母内。状态接收和最新值更新由不同日志字段记录。
- AoI 统计从各连接的重放原点起算，固定 4 秒。初始生成时间取原点作为统一的虚拟初值，之后在服务端完整接收更新版本时切换生成时间。各传感器按时间积分后取等权均值，峰值取窗口内各传感器的最大年龄。超出窗口的交付仍计入时延与截止指标。

## 实测结果

2026-11-07 完成两组实跑，均使用 seed 17 和相同的二进制输入。每组每种策略输入 66 条记录，其中有 45 条状态、20 个独立事件和 1 条重复事件。五个独立 CTest 均通过，两组实验的每一次成功提交都通过离线规则重算。十次连接均在 A 和 B 上抓到实际 UDP 流量，并在 XQUIC 日志中确认两路都承载了 STREAM。

表中斜线分隔两次运行的计数，时延与 AoI 为两次运行的实测范围。

| 策略 | ACK 条数 | 事件按时 / 20（两次） | 状态按时 / 45（两次） | 事件 P95（ms） | 平均 AoI（ms） |
| --- | ---: | ---: | ---: | ---: | ---: |
| FIFO | 66 | 2 / 2 | 4 / 4 | 2,799.1–2,801.0 | 1,988.6–1,988.8 |
| Strict Priority | 66 | 5 / 5 | 0 / 0 | 2,299.2–2,303.1 | 1,995.0–1,995.5 |
| EDF | 66 | 4 / 4 | 2 / 0 | 8,239.6–8,542.8 | 1,907.3–1,908.8 |
| AoI-aware | 66 | 0 / 0 | 8 / 8 | 5,536.5–6,083.8 | 1,495.9–1,514.0 |
| Latest-value + RR | 30 | 3 / 3 | 5 / 5 | 3,340.1–3,448.7 | 1,432.3–1,434.4 |

在这组高压力负载下，Strict Priority 的事件按时数最多，同时两次都没有状态按时。AoI-aware 两次都使 8 条状态按时，20 个独立事件则全部超时。Latest-value + RR 两次各在提交前替换 36 条状态，得到最低平均 AoI。替换和轮转在该策略中同时开启，单个机制的贡献需要独立消融。这里使用单一网络场景和 seed，保留固定的小 credit 与发送缓冲，指标用于基线验证和观察该条件下的权衡。

[复跑 1 原始结果与对照 JSON](../results/stage3-wsl/replicate-1/comparison.json) · [复跑 2 原始结果与对照 JSON](../results/stage3-wsl/replicate-2/comparison.json)。各策略目录保留输入、端点日志、两路抓包、策略提交顺序和完整指标。

共享端点的 Stage 1 和 Stage 2 原入口也通过实跑回归，分别完成 128 条固定记录交付和 28 条 IoT 记录级 ACK。
