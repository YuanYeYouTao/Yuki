# Plugin API 7.1 历史查询扩展

> 本页保留 7.1 历史变更；当前公开合同见 [API 9.0 迁移](api-9.0-migration.md)。

7.1 是 7.0 的增量合同，原方法仍可调用。Host 加载同一 major 内不高于自身 minor 的插件；7.0 插件可直接在 7.1 运行，无需批改插件声明或改原批准事实。要求 7.2 或不同 major 的插件仍需满足实际合同后再加载。

`messages.get_recent(limit)` 与 `messages.search_history(query, limit)` 统一使用当前 Conversation 的内部 ID 页、原作者及正文/current ASR 匹配；返回仍按账本正序排列。不再有历史查询的 100 条帽、400 字符查询帽或空关键词拒绝。空查询可浏览，`limit` 是正整数，不做静默裁剪。

search_history 新增可选关键字参数 `cursor`、`match=all|any|phrase` 和 `exclude`。`cursor` 是内部事件 ID，继续读取更早的事件；不是平台 message_id。all/any 按字面词组合，phrase 保留词间空格；完整繁简转换只放宽原字面匹配。排除项同样逐独立原字段核验。

CurrentMessage 新增真实入账时才有的 `event_id`、`event_kind`、`author_presence_id`、`ingress_presence_id`、`occurred_at`、`observed_at`、当时昵称/群名片、`reply_to_event_id`、`asr` 与稳定 `attachments.attachment_index`。`message_id` 仍是原平台传输号，`presence_id` 仍认接入账号；不以今日账号归属改写原作者。

`content` 保原正文事实，历史结果的 `text` 包含正文及此作者 current ASR 的带来源转写，`asr` 另给原片段编号。引用语音不作为当前作者的搜索证据。文件原件过期不删除已观察消息，不为搜索补下载、转录或识图。

新增 `message.history.v2` Feature；原有 Feature 保持。FakeMessageFacade 按原 `content` 与独立 current ASR 核验，不检索展示标签或跨字段拼接；同步支持空查询、繁简、字面模式、排除项、内部 ID 游标与无经验帽的 limit，SDK 不导入 Host 来实现查询。

```python
ctx.features.require("message.history.v2")
messages = await ctx.messages.search_history(
    "电动车 京", limit=120, match="any", exclude=("已取消",)
)
if messages:
    older = await ctx.messages.search_history("", cursor=messages[0].event_id)
```

查询只读并沿原调用的真实委托与 Conversation 范围，读取完整已入账历史，不继承 `/ai new` 的上下文 floor；App/ACP 平台身份不另授或限制历史读取资格。SDK 当前 Conversation 方法不因此扩为无委托的全库读取；Main 工具的跨入口查询与来源目录见[聊天记录读取](../architecture/chat-history.md)。索引中的会话前缀不授予读取资格。旧回执和已付请求继续保原 ID、参数和真实结果，不重新执行退役工具。
