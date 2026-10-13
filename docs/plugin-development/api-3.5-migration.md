# Plugin API 3.5 迁移

3.5 删除从未由 Host 发布的通知事件，以及未实现的 `runtime.config.read/write` 权限。保留真实发布点与现行 Facade，不为旧声明增加空实现或别名。

1. 删除以下事件订阅与事件名引用：

- `application.starting`
- `message.recorded`
- `message.observed`
- `context.assembled`
- `prompt.collecting`
- `prompt.composed`
- `agent.starting`
- `agent.tool_called`
- `agent.tool_completed`
- `agent.finished`
- `agent.interrupted`
- `reply.planned`
- `reply.generated`
- `reply.sending`
- `reply.cancelled`
- `reply.failed`
- `memory.created`
- `memory.updated`
- `memory.deleted`
- `vision.completed`
- `vision.failed`
- `web.search_completed`
- `web.read_completed`
- `automation.created`
- `automation.started`
- `automation.completed`
- `automation.failed`
- `emoji.candidate_collected`
- `emoji.analysis_completed`
- `emoji.selection_started`
- `emoji.selected`
- `emoji.send_attempted`
- `emoji.send_failed`
- `emoji.fallback_text_sent`

2. 删除 manifest 中的 `runtime.config.read`、`runtime.config.write`；插件自己的配置继续使用 `plugin.config.read/write`，Yuki 运维配置通过现有管理入口处理。
3. 将 `plugin_api` 更新为 `3.5`，重新发现、审阅并批准实际 manifest。Host 精确匹配 API 和 manifest hash；旧批准不会自动授权新合同。

加载历史批准记录时只忽略这两个已退役权限，其他未知权限仍按原失败路径处理。旧批准、审计和执行回执保留，既有权限不扩大。当前可订阅事件见 [Event Catalog](api-reference/events.md)。更早插件先完成 [API 3.4 迁移](api-3.4-migration.md)。
