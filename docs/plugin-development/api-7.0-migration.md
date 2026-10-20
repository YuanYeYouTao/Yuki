# Plugin API 7.0 迁移

Host 只加载精确声明 `plugin_api = "7.0"` 的插件。独立会话的公开 feature 为 `plugin.agent_session.v2`；其他能力和真实批准权限不变。

独立 `agent_sessions` 只运行连续模型会话，保留原会话 UUID、可见历史、逐消息 metadata、实际模型请求预算和计数。删除没有执行消费者的接口：

- `CreateAgentSessionRequest.metadata`、`allowed_capabilities`。
- `RunAgentSessionRequest.allowed_capabilities`、`max_tool_calls`。
- `AgentSessionRunResult.tool_calls_used`，它在无工具会话中始终为零。

旧字段不被忽略，也不转换成新授权；调用方直接移除这些参数。`max_model_requests` 继续限制真实请求，返回值仍有 `text`、`session` 和 `model_requests`。主 Agent 的 `ctx.agent.run()` 工具与权限合同不因此变化。

同一人的原 Session 历史按原归属读取。Space 暂停后仍能读取已有资料、保存并返回已接纳的模型结果；开始新的 run 仍核真实当前资格和插件会话权限，不能跨插件或场景读取。

会话自然到期或到期维护不撤销已接纳的模型响应，原结果继续保存并返回；新 run 仍按当前状态与实际期限接纳。SDK 如实返回 Host 已有的 `expired`、`blocked` 状态，不把它们改为 `active`。真正删除的会话不重建。

一次普通后台 runner 失败只结束该 runner；声明 `on_failure` 的 runner 只重启自己。其他后台任务、普通工具和插件批准保持。初始化失败或显式停止插件仍按原生命周期处理。

部署先升级插件调用和声明，再沿现有管理员发现、批准与启用流程更新；声明变更不增加批准权限。数据库迁移删除没有业务消费者的 `plugin_agent_sessions.allowed_capabilities_json`，不清 Session 历史、人物来源或执行回执。API 6.0 的变更记录保留在[历史迁移说明](api-6.0-migration.md)。
