# Plugin API 9.0 消息身份合同迁移

9.0 删除 `CurrentMessage.message_id`，改为 `native_message_id: str | None`。它只表示真实的平台消息引用；App/ACP 和没有原生消息号的事件返回 `None`，不再塞入请求键或内部事件号。字段不再有 128 字符上限。

插件的任务归属、回复与历史关联继续使用内部 `event_id`、Person 和 Conversation。入站去重用的 `ingress_key` 与原信封 `origin_key` 不对插件公开，不能拿原生消息号恢复已有事件或业务所有权。`MESSAGE_NORMALIZED` 仍在首次入账之前触发，此时 `event_id` 可以为空；`MESSAGE_TRIGGERED` 保留原触发时点，通知资料中的原生字段也统一叫 `native_message_id`；`REPLY_SENT` 用 `trigger_event_id` 表达原触发事件，原生回执字段为可空 `native_message_id`。

适配步骤：

1. 将读取和构造 `CurrentMessage.message_id` 的代码改用可空 `native_message_id`；需要内部来源时读 `event_id`。
2. 将 `plugin.toml` 的 `plugin_api` 改为 `"9.0"`，按真实 manifest 重新发现并批准。
3. 验证原生消息号为空、超过 128 字符以及入账前 hook 的场景。Host 不给旧字段保留 alias，也不加载旧 8.x 合同。

本次没有新增默认工具、服务或权限。客户端 HTTP API revision 仍为 7，SDK 的 major 升级只表示插件公开投影发生破坏性变化。自动化 TaskSpec 变化沿用 [8.0 迁移](api-8.0-migration.md)，旧版本资料和任务定义的 hash 不重写。
