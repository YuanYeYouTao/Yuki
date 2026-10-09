# 记忆与查证可靠性历史检查点

基线 `codex/memory-relevance-and-reflection` / `3e67492`，版本 3.8.1 / schema 0051。
原施工要求已由现行合同替代；当时未通过的真实验收和授权停止点保留为历史，
后续追加发布授权见 [历史验收记录](memory-evidence-acceptance-progress.md)。

## 实施记录

初始工作树干净。线上 schema 0051，镜像标签 memory-recovery-92483ab1，镜像 ID：
`sha256:29b3bfbc7ca3460b337d8f373e573b5eeaa23ffce7fcb606ea4771b957c938a3`。
环境 WEB_MODE=native_with_tavily_fallback，不等于所有会话最终有效权限。
当前容器仅启动约三小时，9 月 7–8 日容器日志无记录，不能解释成零调用。尚未完成 C1 验收。

后续实现和真实复测详见 [验收进度](memory-evidence-acceptance-progress.md)。
C1–C4 代码提交保留，但不等于真实效果通过；自省语义与相关性校准等硬门槛仍未通过，禁止部署。
