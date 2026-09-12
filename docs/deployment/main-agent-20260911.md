# Main Agent 与沙箱续跑上线记录

2026-09-11 19:26（Asia/Shanghai）上线完成。

## 交付

- 代码：`04da263`（历史投影、主入口续跑与预算）、`5be2817`（旧社交调用兼容）、`a1fac0d`（保留执行回执的版本回退）。
- 镜像：`ghcr.io/yuanyeyoutao/yuki-qqbot:main-agent-a1fac0d`，本地构建并流式传输到服务器，没有远端重新构建。
- 本地与服务器镜像 ID 相同：`sha256:fe183caa7da40ffc83c0bd8e906e9f85a9d660b24c2b45ad2fc81ee57615377d`。
- 只重建 `qq-ai-bot-bot-1`，并更新、重启主机 `yuki-sandbox.service` 的 Manager。SnowLuma 容器 ID 保持不变；未清理 orphan、未改登录配置。
- 新增覆盖文件 `/opt/yuki-qqbot/docker-compose.main-agent.yml`，追加在原有 Compose 文件之后。

## 验证

- Ruff 检查和格式检查完成；Mypy 检查 559 个源文件通过。
- 最终集中运行 800 项：799 项通过，一项发现旧社交调用没有 `execution_id`。修复后只重跑该定向场景，通过；没有再跑一次全量。
- 两协议主入口 HTTP 对照共 56 次请求，包含沙箱续跑、跨服务重建、改名、读取收窄及直接媒体投递后的下一轮。详见请求对照报告。
- 生产数据库副本执行 `0052 → 0054 → 0052 → 0054`；integrity_check、foreign_key_check 通过，聊天、会话、绑定、Presence、记忆事实和自动化数量不变。旧二进制通过回退后的 schema 检查；模拟的 uncertain/accepted 执行回执完整保留。
- 现网 `/healthz`：status/database 为 ok、OneBot connected、两个插件、三个 MCP 连接、79 个 MCP 工具、自动化 Worker 正常。Manager 服务 active；Bot UID 10001 经实际 Unix socket 读取完成队列成功。
- 新启动日志未发现 ERROR/Traceback，现网 schema 为 0054、foreign_key_check 无错误。
- 已安装插件源码没有遗留 `llm.generate / generate_with_context / agent.run` 调用。安装中的 SnowLuma action registry 包含好友和群历史读取接口，参数与本次工具使用的 `user_id/group_id + count` 相符。
- 没有向 QQ 用户发测试消息，没有发起付费模型验收请求。检查期间没有新的提示词投影/沙箱续跑样本，实际缓存命中与新任务在线效果仍待自然流量；不能从健康状态推断命中率。

## 备份与回退

备份目录：`/opt/yuki-qqbot/backups/pre-main-agent-04da263`。包含停止 Bot、排空任务、停止 Manager 后的一致性 Bot DB、Manager DB、工作区/索引，以及原配置、Manager 源码、旧镜像 ID 和副本演练报告。

旧镜像另打标为 `yuki-rollback:pre-main-agent-04da263`。需要回退时：停止新 Bot 和 Manager，以新镜像对**当前业务库**执行 `alembic downgrade 0052`，还原备份的 Manager 源码，使用原 Compose 文件列表（不附加 main-agent 覆盖文件）重新启动旧 Bot。0053/0054 的执行回执表保留；不把旧整库覆盖到当前库，不丢弃上线后的事件。

部署前磁盘不足。仅对旧备份目录 `pre-3.8.0-final-20260826T005706Z/data/backups` 中的独立 `.db` 备份逐个 gzip 压缩；每个文件解压计算 SHA256，与压缩前一致后才移除未压缩副本。保留 `gzip-preservation-20260911.jsonl` 映射及摘要，释放 739,441,336 字节。业务库、当前备份和登录数据未清理。未部署的中间候选镜像已删除，现网旧镜像保留。
