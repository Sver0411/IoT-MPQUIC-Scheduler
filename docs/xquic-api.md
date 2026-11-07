# 固定版本 XQUIC 源码核查

实验用 Alibaba XQUIC `dffd32c42a511edae544b87169976516b7c4cbfa`，在 QUIC v1 上协商该提交的实验性 `XQC_MULTIPATH_10`（`0x0a`）。实现按路径管理应用数据包编号空间。互操作范围为同一固定实现的两端，未测试 IETF Multipath QUIC draft-21 互操作性。 [固定提交](https://github.com/alibaba/xquic/tree/dffd32c42a511edae544b87169976516b7c4cbfa)

| 检查项 | 固定版本的实现与实验用法 |
| --- | --- |
| Multipath transport parameter | `xqc_write_transport_params` 编码 `XQC_TRANSPORT_PARAM_INIT_MAX_PATH_ID_V10`（`0x0f739bbc1b666d09`），解码恢复 `XQC_MULTIPATH_10`。双方设 `enable_multipath=1` 和 `init_max_path_id=1`。协商取两端路径 ID 上限的最小值。连接统计里的 `enable_multipath=1` 对应源码枚举 `XQC_CONN_MP_ENABLED`。 |
| 创建路径 | `ready_to_create_path_notify` 在 CID 条件满足时发出。客户端在回调外用 `xqc_conn_create_path` 获取新 ID，先建立应用映射，再让 engine 发送。该版本的新路径会继承连接初始地址。客户端 B 以绑定并连接的 UDP Socket 作为实际地址来源。 |
| 服务端映射与验证 | `xqc_engine_packet_process` 传入收到包的本地和远端地址。新路径触发 `xqc_conn_server_init_path_addr`，随后调用 `path_created_notify`。端点通过 `xqc_path_get_local_addr` 和 `xqc_path_get_peer_addr` 检查是否匹配 B。路径建立通知发生在验证期间。`PATH_CHALLENGE` 和匹配的 `PATH_RESPONSE` 完成后，`xqc_conn_available_paths` 才将它计为可用。 |
| 发送与接收 | `write_socket_ex` 给出 XQUIC 选定的 `path_id`。应用只从已映射的 Socket 发送。接收 API 无应用可读的 `path_id` 输出，入口接口由 Socket 确定，协议路径由 XQUIC 按 CID 处理。应用日志和 XQUIC 内部逐路径日志分别保留。 |
| `scheduler_callback` | `xqc_scheduler_get_path` 接收连接、待发包、拥塞检查和 reinjection 参数。Stage 1 直接使用 `xqc_minrtt_scheduler_cb`；固定路径控制帧可绕过普通数据包的路径决策。当前实现没有给每条 Stream 设置亲和性。 |
| PATH_ACK / MP_ACK | 在此版本中，生成器发出 `XQC_TRANS_FRAME_TYPE_MP_ACK0`（`0x15228c00`），接收分支识别 `MP_ACK0` 和 `MP_ACK1`（`0x15228c01`），内部日志名为 `ACK_MP`。帧中的路径 ID 指定被确认的路径，可与实际载送 ACK 的路径不同。本实验使用默认的原路径 ACK 偏好。 |
| 重传 | XQUIC 按路径检测丢失，把需要重传的包交给丢失队列，`xqc_conn_schedule_packets_to_paths` 调度 `XQC_SEND_TYPE_RETRANS`。本实验关闭额外的主动 reinjection（`mp_enable_reinjection=0`），普通丢失重传仍按上游逻辑运行。严格同路和跨路径恢复两个实验模式留到 Stage 4。 |
| 单向 Stream 与流控 | 通过 `xqc_stream_create_with_direction` 创建；`xqc_stream_send` 允许部分写和 `-XQC_EAGAIN`；`xqc_stream_recv` 交付有序字节与 FIN。发送端通过 FIN 正常结束，`xqc_stream_close` 是重置或停止接口。credit 由 XQUIC 的 `MAX_STREAMS` 处理，应用并发槽位在 `stream_close_notify` 释放。 |
| 计数与时钟 | `xqc_conn_get_stats` 的 `paths_info[]` 包含路径上的包计数和数据包字节计数。`xqc_conn_path_metrics_print` 不填充此数组的 `path_srtt`，因此报告单独使用 ICMP RTT。engine 定时器和数据记录用同一 Linux 单调时钟。 |
| Token 与 ticket | `xqc_process_new_token_frame` 和 `xqc_conn_tls_session_cb` 直接调用保存回调。客户端提供回调并丢弃返回的数据，实验每次新建连接且不使用 0-RTT。 |

主要核查文件为公开头文件 `include/xquic/xquic.h`，以及 `src/transport/` 下的 `xqc_engine.c`、`xqc_conn.c`、`xqc_multipath.c`、`xqc_transport_params.c`、`xqc_frame.c`、`xqc_frame_parser.c`、`xqc_send_ctl.c`、`xqc_stream.c` 和 `scheduler/xqc_scheduler_minrtt.c`。 [公开 API](https://github.com/alibaba/xquic/blob/dffd32c42a511edae544b87169976516b7c4cbfa/include/xquic/xquic.h) · [传输层源码](https://github.com/alibaba/xquic/tree/dffd32c42a511edae544b87169976516b7c4cbfa/src/transport)

## Stage 4 扩展

`xqc_send_queue_get_packet_out_for_stream` 在固定版本中只合并同一 Stream 的 STREAM 帧，适配器据此用包的 `po_stream_id` 查询发送端绑定。普通丢失重传走现有的路径调度回调。`xqc_send_queue_copy_to_probe` 给 PTO 副本设置 `XQC_PATH_SPECIFIED_BY_PTO`，因此新增补丁仅在研究回调启用时清除此绑定，让该类 STREAM 副本也参与路径选择。重传副本用 `po_origin` 识别。路径状态、SRTT、RTTVAR、拥塞带宽估计、在途字节和近期丢失计数由适配器读取固定版内部结构。 [PTO 调度补丁](../patches/xquic/stream-pto-scheduler.patch)
