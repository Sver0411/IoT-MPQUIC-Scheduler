# Stage 4 联合调度与路径亲和性

联合调度使用 ACK 快照评估状态更新价值，按事件严重程度、优先级、等待、时限和路径估计评分，并用普通状态配额与连续事件配额安排过期恢复。路径估计根据实时 XQUIC 统计更新，初发路径在单向 Stream 创建成功后绑定。两种模式共享应用层规则，差异只在客户端携带 STREAM 的修复副本能否改用备用路径。 [算法规范](algorithm-spec.md) · [XQUIC 扩展](xquic-api.md)

## 复现

```bash
bash scripts/build.sh
cmake --build "${IOT_BUILD_DIR:-build}/app" --target iot-scheduler-test
ctest --test-dir "${IOT_BUILD_DIR:-build}/app" --output-on-failure
sudo bash scripts/run-stage4.sh
sudo env IOT_RECOVERY_K=1 bash scripts/run-stage4.sh
python3 evaluation/verify_stage4.py --compare RESULTS_DIRECTORY
```

默认 seed 为 17、K 为 2。自定义构建路径时，在 sudo 后通过 `env IOT_BUILD_DIR="$IOT_BUILD_DIR"` 传入。每组只生成一次重放输入，给两个模式各新建一条双路径连接。验收器根据逐次路径样本、队列日志和 ACK 快照重算评分、路径估计、配额与选择，再将绑定及修复决策与真实 XQUIC 发包和两接口的抓包核对。

## 受控中断与反转

2026-11-07 在 Ubuntu 24.04 WSL2 实跑。路径验证完成后，脚本对客户端 A 的出口设置约 0.9 s 的 `netem loss 100%`，随后恢复并反转两条链路的性能。首个事件在重放开始后 240 ms 生成，线上 deadline 为 550 ms，即生成后 310 ms。每次重放共有 39 条输入，含 17 个独立事件和 22 条状态。两种模式都实际尝试从 A 发送该事件的首批 STREAM 数据。

| K | 模式 | 初始绑定 A / B | 收到记录 ACK | 过期状态提交 / K 触发 | 全程跨路修复包 / 中断期间 | 首个事件延迟 | A 恢复前完整接收 |
| ---: | --- | ---: | ---: | ---: | ---: | ---: | --- |
| 2 | 严格 | 4 / 18 | 22 | 2 / 1 | 0 / 0 | 1,547.547 ms | 否 |
| 2 | 跨路径 | 5 / 18 | 23 | 3 / 1 | 15 / 6 | 341.679 ms | 是 |
| 1 | 严格 | 4 / 18 | 22 | 2 / 2 | 0 / 0 | 1,552.457 ms | 否 |
| 1 | 跨路径 | 4 / 18 | 22 | 1 / 1 | 15 / 6 | 346.389 ms | 是 |

四次运行都交付了全部 17 个独立事件，并各丢弃一条已由 ACK 证明过旧的待发状态。跨路径模式在中断窗口内通过 B 发送修复副本，两个配置下的首个事件都早于 A 恢复完整到达，但均超过 310 ms 截止。计数基于实际发送的 STREAM 包；包中可含多个同一 Stream 的帧或重复字节。

严格模式在原路不可用时等待该路径恢复。跨路径模式只迁移修复副本，原始数据仍遵循初始绑定。任一模式的接收端都必须等到 Stream 字节有序齐全并收到 FIN，晚到的缺失前缀仍会造成 Stream 内队头阻塞。当前场景用于验证机制与配置可运行，完成概率仍是启发式，K 的广泛敏感性和多个网络场景的消融留到 Stage 5。

[K=2 对照结果](../results/stage4-wsl/k2/comparison.json) · [K=1 对照结果](../results/stage4-wsl/k1/comparison.json)。各模式目录保留原始输入、两端应用和 XQUIC 日志、双链路抓包、中断时间、netem 快照和自动验收结果。
