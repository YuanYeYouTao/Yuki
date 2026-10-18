# Plugin API 5.0 迁移

5.0 删除旧表情池专属 API；Host 精确加载 `plugin_api = "5.0"`。内部 Person、Space、Conversation 与现行媒体、消息、独立 Agent session 合同沿用 [4.0 身份迁移](api-4.0-migration.md)。产品版本与 Plugin API 独立。

- 删除 `ctx.emoji` 及其 list/get/search/collect_current/select/adopt/reject/ban 方法，不保留兼容别名。
- 删除 `emoji.read/collect/select/manage/hook` 权限、`emoji.facade.v1`、`emoji.selection_signals.v1` Feature，以及所有旧 `emoji.*` 生命周期事件。
- 删除 EmojiSelectionCandidate、EmojiSelectionSignalContext、EmojiSelectionSignal、EmojiSelectionSignalRegistration 和 `register_emoji_selection_signal`。选择信号不再对新图库打分或参与发送。
- 插件读取当前媒体继续用 `ctx.media`；发送插件拥有的图片继续用 `ctx.messages.send_image` 和不可变 media artifact。共享图库由 Yuki 的持久终端、媒体保存桥及统一 Social 发送处理，插件不直访 Bot 文件、数据库或图库内部服务。
- 从 manifest 移除退役权限/Feature/Hook，修改真实调用，然后将 `plugin_api` 改为 `5.0`，重新发现并审阅实际 manifest。旧批准不静默适用于改变后的 manifest；不把 4.0 插件当作已经适配。

原通知、独立 Agent session、执行预算与已发送回执保留原身份。升级不重发旧通知或付费请求。没有旧表情能力的插件仅需更新合同声明并重新发现、批准。

共享图库见[表情与配图合同](../emoji-system/architecture.md)。
