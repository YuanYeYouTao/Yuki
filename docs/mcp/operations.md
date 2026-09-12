# MCP 运维

普通用户可查看：`/ai mcp list`、`show`、`status`、`tools` 和 `search`。SUPERUSERS 可执行
`refresh`、`reconnect`、`enable`、`disable`、`doctor` 和确定性 `call`。诊断调用绕过
Capability Search 的自然语言检索，但仍经过同一 Manager、结果归一化和结果预算。

`/healthz` 公开启用状态、配置/连接 Server 数、缓存工具数和活动调用数，不连接 lazy Server。
`/ai status` 还显示最近调用时间和最近错误类别。调用指标位于 `tool_invocations`，不保存参数、结果或
用户消息；`conversation_key` 只保存 SHA-256。

`/ai mcp refresh <server>` 刷新目录元数据，不重写已冻结的主 Agent 声明。
变更工具定义或启用清单后，重启 Bot 形成新合同。需要更新镜像时按
[版本化部署流程](../operations/versioned-docker-release.md) 在本地构建，沿用生产 Compose
组合更新 Bot；不在服务器临时构建，不为工具刷新重启 QQ 网关。
