# Stage 9.1 实验基础设施验收

**通过，可进入 Stage 9.2 的工程可行性检查和训练、验证准备。** 五个工程用例在同一台 WSL2 Linux 内核的两个隔离网络命名空间中运行真实 XQUIC。Stage 9.1 新增 23 项自动测试，全部通过。既有 18 项回归测试也全部通过。此次未进行正式科研实验，调度算法和 Stage 8 归档保持原样。Stage 8 归档的 8,260 项 SHA-256 已重新核对。 [测试输出](../results/stage91/validation/tests.log) · [运行清单](../results/stage91/final/manifest.json) · [Stage 8 哈希清单](../results/stage8/archive-sha256.txt)

| 测试类别 | 总数 | 通过 | 失败 |
| --- | ---: | ---: | ---: |
| 状态机单元测试 | 5 | 5 | 0 |
| 实栈日志故障注入 | 12 | 12 | 0 |
| 归档和构建指纹核对 | 1 | 1 | 0 |
| 工程用例归档回放 | 5 | 5 | 0 |
| Stage 9.1 自动测试小计 | 23 | 23 | 0 |
| CTest、Stage 7 和 Stage 8 回归 | 18 | 18 | 0 |
| 合计 | 41 | 41 | 0 |

工程用例各执行一次，随后各从归档重算一次。上表将同一用例只计为一项。没有跳过测试，开发过程中的失败另列。

## 证据链

1. **应用决策。** 从工作负载、队列事件和已接收的 ACK 独立重算队列与选择。单独记录每次尝试及实际建流调用的结果。阻塞不得产生 Stream，成功建流须先收到 XQUIC 建流回调，随后建立唯一绑定并提交。端点序号、尝试编号、阻塞总数和提交总数用于交叉检查缺失与重复。 [应用 Oracle](../evaluation/oracle/app_scheduler_oracle.py) · [状态重放](../evaluation/oracle/stage91_lifecycle.py)
2. **选路和发包。** 初始绑定从路径快照独立重算。每次选出路径的包调度回调得到连接内唯一编号。实际 Socket 成功发送后，XQUIC 报告路径 ID、包号空间、包号和 STREAM 字节范围。验证器核对绑定、所选路径、当时的路径状态、真实发送路径和决策在发送之前的顺序。跨路修复须引用已发送的原包，满足已有修复规则，且选择之后至实际发送之间所选路径的状态不能改变。 [路径回调](../src/transport/path_affinity.c) · [XQUIC 追踪补丁](../patches/xquic/stage91-integrity-trace.patch)
3. **接收和完成。** 两端 Socket 记录 UDP 密文长度和 SHA-256，按接口、端口及摘要对照四份全长抓包。接收端的 XQUIC 认证后 STREAM 帧须和发送端的包、字节范围及数据报匹配。应用读取 FIN 前须已收到连续完整的字节范围，随后重放数据交付和应用 ACK。固定窗口结束时，对已提交但未确认的记录逐条登记，后续到达仍保留。 [独立验证器](../evaluation/verify_stage91.py)
4. **路径状态。** 传输层状态、应用层可用或冻结状态、地址重绑定分别推进。冻结恢复沿用原路径 ID，已移除的路径 ID 不允许直接恢复。重绑定沿用路径 ID，服务器完成候选地址验证后才向新端口发送 STREAM。 [状态重放](../evaluation/oracle/stage91_lifecycle.py) · [迁移原始日志](../results/stage91/final/migration/server.log)

## 五个实栈工程用例

| 用例 | 验收结果 | 证据 |
| --- | --- | --- |
| 应用尝试与建流 | 60 次尝试中 31 次阻塞，29 次成功提交并交付。截止时 13 条未确认 | [摘要](../results/stage91/final/attempts/summary.json) · [客户端日志](../results/stage91/final/attempts/client.log) |
| 跨路径修复 | 14 个跨路修复 STREAM 包通过原包与修复状态检查。19 条提交、18 条交付、1 条截止时未确认 | [摘要](../results/stage91/final/repair/summary.json) · [客户端日志](../results/stage91/final/repair/client.log) |
| 路径冻结、恢复和移除 | 两端各观察到一次冻结、一次恢复和一次移除。64 条提交、64 条交付、12 条截止时未确认 | [摘要](../results/stage91/final/lifecycle/summary.json) · [客户端日志](../results/stage91/final/lifecycle/client.log) · [服务端日志](../results/stage91/final/lifecycle/server.log) |
| ACK 丢失和窗口超时 | 只在服务器出向两条链路注入 100% 丢包。4 条数据均已交付，4 条应用 ACK 的 STREAM FIN 都有发送证据、没有客户端认证接收，4 条均记为截止时未确认 | [摘要](../results/stage91/final/timeout/summary.json) · [网络切换](../results/stage91/final/timeout/network.jsonl) |
| UDP 端口重绑定 | B 路径客户端从 4434 切换至 4435，服务器观察到候选验证、提交和结束。两端均有新旧端口上的 STREAM 发送证据，且新端口实际收到服务端 STREAM | [摘要](../results/stage91/final/migration/summary.json) · [客户端日志](../results/stage91/final/migration/client.log) · [服务端 B 路径抓包](../results/stage91/final/migration/link-b.pcap) |

五个用例共核对 351 个客户端 STREAM 包、136 个服务端 STREAM 包和 20 份全长抓包。144 条成功提交中，143 条交付，114 条获得应用 ACK，30 条在截止时未确认。跨路修复用例的服务端在截止后关闭一个未收齐 FIN 的 Stream，进程返回 1。连接关闭码为 0，未完成记录和退出码均保留，按固定窗口规则判为测量期后的未完成收尾。 [运行结果](../results/stage91/final/execution.json) · [跨路修复退出日志](../results/stage91/final/repair.log) · [服务端原始日志](../results/stage91/final/repair/server.log)

## 故障注入验收

所有篡改均在原始证据的内存副本上执行。需要隔离序号检查的用例会重新编号，并断言对应的语义拒绝原因。

| 注入 | 预期结果 | 实际与证据 |
| --- | --- | --- |
| 删除单条应用决策日志 | 序号缺口，拒绝 | 通过 · `test_delete_single_decision` |
| 成对删除应用决策和阻塞日志后重新编号 | 建流结果找不到决策，拒绝 | 通过 · `test_delete_paired_blocked_logs_and_renumber` |
| 删除整组阻塞尝试后重新编号 | 尝试序号或端点计数不符，拒绝 | 通过 · `test_delete_entire_blocked_attempt_and_renumber` |
| 修改实际发包路径 ID | XQUIC 路径与 Socket 路径不符，拒绝 | 通过 · `test_change_actual_path_id` |
| 交换尝试和决策并重新编号 | 决策没有在先尝试，拒绝 | 通过 · `test_swap_attempt_and_decision_after_renumbering` |
| 重复决策并重新编号 | 同次尝试重复决策，拒绝 | 通过 · `test_duplicate_decision_after_renumbering` |
| 删除已发生的 ACK 日志并重新编号 | 已读 FIN 没有对应 ACK，拒绝 | 通过 · `test_missing_ack_log_after_renumbering` |
| 提前标记接收 FIN | 已认证字节范围或 FIN 不全，拒绝 | 通过 · `test_early_fin_before_authenticated_fin` |
| 伪造冻结后的路径恢复 | 端点日志与原始 XQUIC 日志不同，拒绝 | 通过 · `test_forged_recovery_differs_from_raw_xquic` |
| 篡改修复原包号 | 找不到对应原始 STREAM 发送，拒绝 | 通过 · `test_cross_repair_original_packet_tampering` |
| 删除队列入队日志并重新编号 | 队列重放不一致，拒绝 | 通过 · `test_queue_operation_removed_after_renumbering` |
| 将迁移提交对应的数据报改成旧端口 | 缺少候选地址的数据报证据，拒绝 | 通过 · `test_migration_commit_on_wrong_socket` |

五项状态机单元测试分别验证正常迁移提交、候选验证中止、缺少候选验证时拒绝提交、已移除 ID 禁止直接恢复，以及跨路修复必须先满足状态条件。归档测试核对 118 个文件的 SHA-256、五个用例配置、可执行文件指纹和运行期间源码及二进制一致性。 [测试实现](../tests/stage91_test.py) · [测试输出](../results/stage91/validation/tests.log) · [归档 SHA-256](../results/stage91/final/sha256.json) · [构建一致性记录](../results/stage91/final/provenance-check.json)

开发阶段保留了四次失败。首轮有三个用例未通过启动检查，原因是追踪序号插在现有启动日志匹配所需的字段之间。第三轮真实发生了端口重绑定，但客户端在切换前未通过 B 路径发送 STREAM，因证据覆盖不足被拒绝。最终用例将换端口时点从 525 ms 移到 1,150 ms，并沿用同样的迁移覆盖门槛。 [首轮原始退出状态](../results/stage91/validation/trial-01/execution.json) · [迁移覆盖失败](../results/stage91/validation/trial-03/migration.log) · [最终迁移配置](../results/stage91/final/migration/case.json)

## 未覆盖与进入 Stage 9.2 的条件

- 实栈迁移覆盖了同 IPv4 地址、同接口的 UDP 端口重绑定。验证中止目前只有单元测试覆盖。跨 IP、跨接口、IPv6、移除后以新 ID 重建，以及真实多主机时钟对齐还没有实栈证据。
- 真实跨路修复覆盖了定向故障用例。验证器检查已有规则允许的原因和路径状态，拥塞窗口及 PTO 取值来自端点快照。各修复分支的完整实栈覆盖和独立重算仍待验证。Native MinRTT 的内部逐包选择未纳入本阶段重放。
- Stream 超时按工程用例的固定窗口定义。未确认可来自数据未完成或应用 ACK 未回传。此次还没有覆盖所有传输层超时、QUIC 控制 ACK 丢失和迟到组合。
- 同内核单调时钟用于因果排序，抓包校验 UDP 密文和接口、端口。五次配置切换中，整批 qdisc 更新比计划晚 5.457–24.078 ms。短预算研究应在正式采集前校准各方向的生效时间和时钟误差，且记录测量开销。本轮没有量化 SHA-256、抓包和同步日志对指标的影响。
- 序号、端点计数、XQUIC 原始日志、抓包和哈希用于交叉核对缺失与篡改。验收依赖至少一份独立证据保持完整，没有证明所有证据被同步改写时仍能识别伪造。

**Stage 9.2 可用范围。** 可以用这条证据链开展受控 Linux 双路径的工程可行性检查及训练、验证准备。进入封存测试前，仍须按研究计划校准测量精度、冻结环境与样本门槛，并为测试矩阵中实际使用的未覆盖状态补齐证据。 [Stage 9 研究计划](stage9-research-plan.md)

## 复核命令

```bash
# 在 Linux 上构建并重放已归档证据
bash scripts/test.sh --evidence results/stage91/final

# 在全新目录重跑工程用例，需要网络命名空间权限
sudo python3 -B experiments/run_stage91.py /var/tmp/iot-stage91-new
python3 -B tests/stage91_test.py --evidence /var/tmp/iot-stage91-new
```

若构建使用了自定义 `IOT_BUILD_DIR`，运行和回放应按上面的命令传入相同的构建目录。工程运行器拒绝覆盖已有输出目录。
