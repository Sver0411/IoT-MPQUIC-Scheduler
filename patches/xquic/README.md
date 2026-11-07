# XQUIC 修改边界

官方 XQUIC Git 子模块固定在 `dffd32c42a511edae544b87169976516b7c4cbfa`。构建脚本检查子模块版本及工作树，并将补丁应用到以提交和补丁哈希命名的构建副本。

`stream-pto-scheduler.patch` 在路径调度回调中增加一个 opt-in 标志，将原本直接绑定的 STREAM PTO 副本交给研究调度器。控制包、其他路径绑定标志及原有调度器继续使用上游逻辑。应用与 XQUIC 一起从此固定源码构建。运行 `bash scripts/build.sh` 应用补丁并构建，运行 `sudo bash scripts/run-stage4.sh` 验证两种模式。公开 API 和补丁的作用范围见 [XQUIC 源码核查](../../docs/xquic-api.md)。
