# 实验环境与运行方法

## 安装和构建

需要 Linux、可创建 network namespace 的 root、veth、`sch_netem` 和 IPv4。已验证的环境为 Ubuntu 24.04 WSL2。Windows 仅作为宿主机，执行以下命令时进入 Linux shell。

```bash
sudo apt-get update
sudo apt-get install -y build-essential git cmake ninja-build iproute2 iputils-ping tcpdump openssl python3
git submodule update --init
bash scripts/build.sh
sudo bash scripts/run-stage1.sh
```

构建使用 CMake 3.22 及以上版本。构建脚本把 BoringSSL 源码和编译产物写入 `build/`，支持用 `IOT_BUILD_DIR` 指定其他位置，用 `JOBS` 指定并行数。首次构建需要访问 GitHub 获取固定的 BoringSSL 提交。XQUIC 从固定提交导出到构建目录，所需的配置头文件在导出副本内生成，再将静态库链接进实验程序。

Stage 4 的 STREAM PTO 补丁也在导出副本内应用，副本目录包含补丁哈希。原有 Stage 1 至 Stage 3 调度器保持上游控制路径。

在 WSL2 上建议把构建和实时日志放在 Linux 原生文件系统，减少跨挂载点 I/O 对短时实验的扰动。构建和运行使用同一个构建路径，结果目录应为尚不存在的路径。

```bash
export IOT_BUILD_DIR="$HOME/.cache/iot-mpquic-stage1"
bash scripts/build.sh
sudo env IOT_BUILD_DIR="$IOT_BUILD_DIR" bash scripts/run-stage1.sh "$HOME/iot-stage1-run"
python3 evaluation/verify_stage1.py "$HOME/iot-stage1-run"
```

若创建命名空间、veth 或 netem 失败，检查 Linux 权限和内核配置。内核将 netem 编译为模块时，可在运行前用 `sudo modprobe sch_netem` 加载。实验脚本分配临时命名空间，在退出时清理；证书和私钥由脚本为每次运行临时生成并清理。仅隔离环境使用此 TLS 配置，客户端没有启用服务端身份验证。

## 拓扑和场景

| 链路 | 接口名 | 客户端 UDP | 服务端 UDP | 阶段 1 每方向 | 阶段 2 每方向 |
| --- | --- | --- | --- | --- | --- |
| A | `iot-a` | `10.47.1.1:4434` | `10.47.1.2:4433` | 4 ms、30 Mbit/s | 45 ms、6 Mbit/s |
| B | `iot-b` | `10.47.2.1:4434` | `10.47.2.2:4433` | 24 ms、12 Mbit/s | 5 ms、30 Mbit/s |

每条链路各有一对 veth，在两端分别应用出口 `tc netem`，表中时延为单向配置值，队列上限为 1,000 包。配置无随机丢包。首批记录在初始配置下发送。收到该批的应用 ACK 后，脚本在同一连接上反转两条链路的配置，并在第二批开始前对两个接口各运行 5 次 ICMP 探测。该拓扑是异构 IP 链路实验抽象。

## 复查结果

- `summary.json` 是自动验收输出。检查两端各一次握手、两个可用路径、记录完整性、路径 ID 与地址映射、XQUIC 逐路径数据和抓包数量。
- `client.log` 与 `server.log` 记录实际 UDP 收发、逐路径 XQUIC 计数、Stream 生命周期和批次 ACK。`t_us` 来自同一 Linux 内核的 `CLOCK_MONOTONIC`。
- `*.xquic.log` 记录协议事件。可按 `PATH_CHALLENGE`、`PATH_RESPONSE`、`ACK_MP`、`STREAM`、`MAX_STREAMS` 检索。
- `link-a.pcap` 与 `link-b.pcap` 来自服务端两个独立 veth，保留 96 字节的包前缀及原始帧长度。使用 `tcpdump -tt -nn -r link-a.pcap` 查看 IP、UDP 端口、长度和时间。
- `environment.txt` 保存系统版本、源码提交、可执行文件 SHA-256 和网络地址。 `netem-phase-*.txt` 与 `ping-*.txt` 保存每阶段的配置及实测 ICMP RTT。

抓包计数与应用层完成数分别统计。每接口抓包的发送方向和接收方向由精确 UDP 四元组识别。XQUIC 数据字节统计的是携带 STREAM 或 DATAGRAM 帧的 QUIC 包长度。本实验只使用 Stream。应用交付字节根据通过 FIN 和内容校验的记录统计。

## Stage 2 重放

Stage 2 使用上述初始链路配置。脚本生成并保存完整的 `workload.bin`，默认 seed 为 17。也可用 `sudo env IOT_SEED=17 bash scripts/run-stage2.sh` 显式指定 seed。若构建目录在 Linux 原生文件系统上，用 `sudo env IOT_BUILD_DIR="$IOT_BUILD_DIR" bash scripts/run-stage2.sh "$HOME/iot-stage2-run"`。

用 `python3 evaluation/verify_stage2.py RESULTS_DIRECTORY` 离线重放验收逻辑。验收以保存的原始输入、客户端与服务端日志、XQUIC trace 和两份抓包为依据。使用相同 seed 可重建相同输入文件。发送时序受实际传输和 credit 影响，待发状态替换数量可能随运行变化。
