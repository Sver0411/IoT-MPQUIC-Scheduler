# Stage 1 技术报告

**运行日期** 2026-11-07

**验收结果** 两次独立运行均通过。每次新建两个 Linux Network Namespace 和一条双路径 QUIC 连接。

## 实验条件

- Ubuntu 24.04 WSL2，Linux `6.18.54.1-microsoft-standard-WSL2`，GCC 13.3，CMake 3.28。
- XQUIC `dffd32c42a511edae544b87169976516b7c4cbfa` 与 BoringSSL `b69a6175320af9b6fca7295fdbb4fb130f4804a2` 均已编译并静态链接。XQUIC 使用实验性 `XQC_MULTIPATH_10`，路径调度为上游 MinRTT，拥塞控制为 CUBIC，开启 pacing。
- A 与 B 使用独立的 /30 子网、veth 对和 UDP Socket。路径 0 映射 A（`10.47.1.1:4434 → 10.47.1.2:4433`），路径 1 映射 B（`10.47.2.1:4434 → 10.47.2.2:4433`）。初始配置为 A 4 ms / 30 Mbit/s、B 24 ms / 12 Mbit/s。首批应用 ACK 后，分别改为 A 45 ms / 6 Mbit/s 和 B 5 ms / 30 Mbit/s。时延为每方向配置。
- 每批 64 个单向数据 Stream，每条应用记录 16,384 字节。每次运行交付 128 条记录，共 2 MiB，包括记录 ID；服务端逐条验证长度、内容、唯一 ID 和 FIN。

## 实测结果

| 复跑 | 阶段 | A 平均 ICMP RTT | B 平均 ICMP RTT | B 占服务端 QUIC 数据包字节 | 服务端批完成 | 客户端收到批 ACK |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| 1 | 初始配置 | 9.801 ms | 57.979 ms | 1.34% | 1,760.744 ms | 1,764.942 ms |
| 1 | 性能反转 | 90.518 ms | 10.199 ms | 74.83% | 5,229.421 ms | 5,234.600 ms |
| 2 | 初始配置 | 9.781 ms | 57.864 ms | 1.34% | 1,757.023 ms | 1,761.209 ms |
| 2 | 性能反转 | 90.372 ms | 10.170 ms | 74.83% | 5,195.749 ms | 5,200.991 ms |

两次运行均在两端观测到 `mp=1`、`mp_state=1`、`available=2`，并在路径 1 的收包日志中观测到双向 `PATH_CHALLENGE` 和 `PATH_RESPONSE`。两条路径上均观测到 `STREAM` 与 `ACK_MP`。每次运行的客户端日志各含 14 个收到的 `MAX_STREAMS` 帧。

| 复跑 | 路径 | 抓包 C→S | 抓包 S→C | 服务端收到的 STREAM 包 | QUIC 数据包字节，初始 → 反转 |
| --- | --- | ---: | ---: | ---: | ---: |
| 1 | A / 0 | 1,147 | 590 | 1,110 | 1,066,040 → 272,608 |
| 1 | B / 1 | 691 | 351 | 684 | 14,440 → 810,528 |
| 2 | A / 0 | 1,147 | 588 | 1,110 | 1,066,040 → 272,608 |
| 2 | B / 1 | 691 | 351 | 684 | 14,440 → 810,528 |

每条链路每个方向的抓包包数都与发送及接收 Socket 的计数相等，tcpdump 均报告零内核抓包丢包。抓包用于核对实际接口、UDP 四元组与包数。加密 QUIC 帧类型与协议路径来自两端 XQUIC 日志。表中 QUIC 数据包字节使用协议计数，可能含包头和重复传输。

RTT 与带宽在同一轮配置变更中一起调整。批完成时间还受到 Stream 并发上限、关闭通知和拥塞窗口演化影响。这组运行验收双路径和动态配置响应。算法优劣、丢包恢复与亲和性的对照属于后续实验。

## 原始证据和复查

| 复跑 | 自动验收 | 链路 A | 链路 B | 端点日志 |
| --- | --- | --- | --- | --- |
| 1 | [summary.json](../results/stage1-wsl/replicate-1/summary.json) | [link-a.pcap](../results/stage1-wsl/replicate-1/link-a.pcap) | [link-b.pcap](../results/stage1-wsl/replicate-1/link-b.pcap) | [客户端](../results/stage1-wsl/replicate-1/client.log) · [服务端](../results/stage1-wsl/replicate-1/server.log) |
| 2 | [summary.json](../results/stage1-wsl/replicate-2/summary.json) | [link-a.pcap](../results/stage1-wsl/replicate-2/link-a.pcap) | [link-b.pcap](../results/stage1-wsl/replicate-2/link-b.pcap) | [客户端](../results/stage1-wsl/replicate-2/client.log) · [服务端](../results/stage1-wsl/replicate-2/server.log) |

两组目录都保存了 XQUIC 内部日志、配置快照、ICMP 探测输出和可执行文件哈希。已有结果可离线重新验收，无需 root。

```bash
python3 evaluation/verify_stage1.py results/stage1-wsl/replicate-1
python3 evaluation/verify_stage1.py results/stage1-wsl/replicate-2
```

## 实现文件与上游边界

| 文件 | 职责 |
| --- | --- |
| [src/transport/endpoint.c](../src/transport/endpoint.c) | XQUIC 两端、Socket 映射、记录与批 ACK Stream、定时器和遥测 |
| [CMakeLists.txt](../CMakeLists.txt) · [scripts/build.sh](../scripts/build.sh) | 固定源码构建与静态链接 |
| [network/topology/dual-veth.sh](../network/topology/dual-veth.sh) · [network/scenarios/performance-reversal.sh](../network/scenarios/performance-reversal.sh) | 双链路拓扑与双向 netem 配置 |
| [scripts/run-stage1.sh](../scripts/run-stage1.sh) · [evaluation/verify_stage1.py](../evaluation/verify_stage1.py) | 实验编排、抓包、清理与自动验收 |

官方 XQUIC 子模块保持原始提交。CMake 生成的配置头文件写入构建副本。具体回调、transport parameter、ACK 帧及重传路径见 [源码核查](xquic-api.md)。

## Stage 2 开发计划

1. **负载和协议。** 定义有版本、类型和长度的记录格式。周期状态记录含 `sensor_id`、`sequence_id`、生成时间、最大信息年龄、绝对截止时间、优先级和 payload。事件记录含独立 `event_id`、类型、严重程度、生成时间、截止时间、优先级和 payload。Python 生成器按 seed 输出可重放的周期负载和集中事件。
2. **Gateway 队列。** 分离每传感器待发状态与独立事件。较新状态只能替换还未提交的同传感器记录；事件各自保留。定义提交边界及替换、过期、背压的结构化日志。
3. **接收与反馈。** FIN 后解析记录并保留服务端完整接收时间。状态仅接受更新的生成时间，事件按 `event_id` 去重。服务端经独立单向 ACK Stream 返回记录 ID、接收时间、处理结果和传感器最新状态。按服务端时间判断截止，另外记录客户端收到 ACK 的时间。
4. **Stream 与 credit。** 把本端并发槽位、对端累计 `MAX_STREAMS` credit、部分写及记录状态分开追踪。在小 credit 和发送背压下运行超过初始 credit 的负载，检查恢复、重复写、FIN 与并发释放。
5. **验收。** 以同一 seed 重放，验证未提交状态可替换、已提交 Stream 内容稳定、独立事件完整送达、重复事件只处理一次，并用双 veth 抓包保留 Stage 1 的传输回归。

后续依次完成应用层基线、AoI 与事件联合调度及 K 次机会恢复、严格亲和与跨路径重传两种模式，以及带宽、延迟、丢包、中断和混合负载场景下的对照与消融。指标统计和科研结论以对应真实运行数据为依据。
