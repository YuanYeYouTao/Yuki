# PluginContext Facade Reference

下列签名来自 `yuki_plugin_sdk.context`；所有方法都可能因权限、作用域、当前轮安全策略或功能关闭而拒绝。

API 7.0 的普通 `user_id` / `group_id` / `target_id` 是内部 Person / Space ID；通知目标还可指定原 `conversation_id`。独立 QQ 扩展中的外部参数保留其明确传输语义，不能回流成业务所有者。

## Message

```python
get_current() -> CurrentMessage | None
get_reply() -> CurrentMessage | None
get_recent(limit=20) -> tuple[CurrentMessage, ...]
search_history(query, limit=20) -> tuple[CurrentMessage, ...]
send_text(text) -> PluginResult
send_private(user_id, text) -> PluginResult
send_group(group_id, text) -> PluginResult
send_image(*, target_type, target_id, media_reference) -> PluginResult
```

## People / Group

```python
people.get_current() -> Mapping | None
people.get(user_id) -> Mapping | None
people.list_aliases(user_id) -> tuple[str, ...]
people.add_alias(user_id, alias) -> PluginResult

groups.get_current() -> Mapping | None
groups.get(group_id) -> Mapping | None
groups.list_members(group_id, limit=100) -> tuple[Mapping, ...]
groups.get_settings(group_id) -> Mapping
groups.set_setting(group_id, key, value) -> PluginResult
```

## Memory

```python
memory.list_person(user_id, limit=20) -> tuple[Mapping, ...]
memory.list_group(group_id, limit=20) -> tuple[Mapping, ...]
memory.search(query, *, scope_type, subject_id, limit=20) -> tuple[Mapping, ...]
memory.add(*, scope_type, subject_id, content, source_type, confidence,
           source_event_ids=()) -> PluginResult
memory.update(memory_id, *, content, confidence=None) -> PluginResult
memory.delete(memory_id) -> PluginResult

```

`memory.search()` 在 Plugin API 9.0 内部复用主程序的 `MemoryRetriever`：插件作用域校验先确定
真实人物或群，随后 FTS 与可选 Embedding 都只在该 SQL 硬过滤范围内召回，并由确定性 RRF
融合。插件不能选择 Provider/profile、获取原始向量、提交 FTS 语法、通过关键词改变
`subject_id`，也不会在无匹配时回退加载全部事实。返回记录只包含有界的
`retrieval_reason`；`list_person()` / `list_group()` 仍用于确定性列表。Embedding 关闭或故障时
自动保持词法行为；当前 Plugin API 版本由 SDK 的 `PLUGIN_API_VERSION` 定义，为 `9.0`。

返回记录的 `user_id` / `subject_user_id` 表示内部 Person，`group_id` 表示内部 Space；
不适用的主体字段为 `None`。这些值可以直接交回同一授权范围的 `list_person()`、
`list_group()`、`search(subject_id=...)` 或 People/Group Facade，不返回 QQ 兼容投影。
`fact_id` 和 `memory_id` 标识原事实，与人物、群和平台账号 ID 分开。

`add` 返回 `data.memory`，其中 `memory_id=person:<fact_id>`；`update` 返回新版本的
`data.memory_id`，`delete` 返回被失效原件的 `data.memory_id`。`confidence` 进入本次实际
fact 和 evidence；`update(confidence=None)` 沿原 fact 的值，不强制改成 1。现有来源证据按
真实范围核验，`source_event_ids` 只能引用当前真实事件，不能用其他事件补造出处。

同一可信 callback 按原调用顺序重入时，`update` 返回原 mutation 的结果 ID；该版本被后续
修正或失效，不会把原成功改成 `memory.not_found`。原效果与当前对象展示分开：`add` 可返回
原 fact 的当前状态，不承诺冻结整份 JSON。权限与隐私规则继续生效，已清退的正文不被恢复；
没有持久 callback 身份的普通新回调不因此获得跨重启重放语义。

`add` 的 `source_type=automatic` 表示自动记录；`plugin` 表示插件生产者，不等于用户显式
指令，两者均沿真实来源形成 automatic 事实。插件身份记录于原 mutation receipt，
不另造持久 `plugin` 来源枚举。公开 `update` 仍是显式 correction 操作；后台、第三方等来源的
具体 authority 继续由真实 owner、来源和既有 provenance 规则决定，参数不能授予权限。

SDK Fake 现在使用相同的 `person:<int>` / 结果 shape；修正创建新 ID 并保留旧记录，
删除只失效，不再给旧 UUID 或原地修改行为保留别名。插件测试应从 `add.data.memory.memory_id`
读取原 ID，并从 `update.data.memory_id` 接续新版本；Fake 不模拟 Host 的授权/恢复引擎。

从 Yuki `3.0.0b2` 开始，`memory.update()` 会创建 explicit correction 新版本并让旧 fact 进入
superseded，不原地改写旧正文；`memory.delete()` 会以
`plugin_explicit_invalidation` 将事实标记为 invalidated，不物理删除事实、证据或状态历史。
插件不能设置 authority、status、conflict_state、supersedes_id，也不能跨人物/群合并或解决
冲突。这些写入语义保留在当前 Plugin API 9.0 中，不代表 Host 兼容旧 1.x 插件。

Yuki 3.0.0 对 MemoryFacade 做了正式 contract freeze：稳定方法为 `list_person`、`list_group`、
`search`、`add`、`update`、`delete`。插件不能访问原始向量、历史 rebuild、质量 fixture、全局
生产 audit、其他人物证据或 Provider API Key；上述能力不会因插件声明额外 permission 而开放。

## Agent / AgentSession

```python
agent.result(work_id) -> PluginResult
agent.resume(work_id, text, *, request_id) -> PluginResult
agent.run(instruction, *, context_profile="none",
          max_tool_calls=None, max_model_requests=None) -> PluginResult

agent_sessions.create(CreateAgentSessionRequest) -> AgentSession
agent_sessions.run(RunAgentSessionRequest) -> AgentSessionRunResult
agent_sessions.reset(session_id: UUID) -> AgentSession
agent_sessions.close(session_id: UUID) -> AgentSession
```

`agent.run` 复用真实 Host actor/source；无入站消息的 SELF 或定时来源同样进入 Main。
同来源及参数读取原 Work 与结果，恢复保原来源、执行 ID、回执和预算；不同来源不会共用结果。
当前真实 Person 的权限在执行处核验，SELF 保自身主体。主调用不再接收工具名子集；
现有插件领域批准在执行处核验，旧 Work 不因退役名称失配而重建或重买。已有 Work 上下文本身不禁用独立调用，
同会话已占用的执行租约沿原执行层处理。

## Web / HTTP / Vision / Media

```python
web.search(query) -> PluginResult
web.read(url, question="") -> PluginResult
http.request(method, url, *, headers=None, body=None, auth_secret=None) -> PluginResult
vision.analyze_current_media(question="") -> PluginResult
media.get_current() -> tuple[Mapping, ...]
media.create_artifact(*, data, content_type, filename, ttl_seconds=86400) -> MediaArtifactHandle
```

`media.get_current()` 包含当前／回复附件的通用类型、真实文件名、大小、MIME、尺寸、`is_sticker`、`sticker_format` 和 `preview_available`，同时保留原QQ `sub_type` 等事实；不返回平台 file_id、预览定位符、原始字节或Host路径。详见[媒体与视觉](../media-and-vision.md)。

`web.search` 只返回供应商已有来源、内容和总结，不自动抓页或调用 Extract。
`web.read` 明确读取选定的公开 URL，可使用历史链接；两者分别需要 `web.search`、
`web.read` 批准，搜索权限不包含读页权限。

`auth_secret` 只能引用 Manifest 已声明的 Secret。Host 仅在同源请求中注入 Bearer credential，跨源重定向会移除；响应只返回 ETag、Last-Modified、Retry-After、Link 和有限的 Rate Limit / request-id Header。

## Background Notification

```python
notifications.publish(PublishNotificationRequest) -> NotificationPublishReceipt
notifications.grant_target(NotificationTarget, *, bot_user_id) -> BackgroundTargetGrantView
notifications.revoke_target(NotificationTarget) -> bool
notifications.list_grants() -> tuple[BackgroundTargetGrantView, ...]
notifications.status() -> Mapping[str, int]
```

后台发布不需要伪造当前用户调用，但只能投向 Host 已授权目标。外部事件以 canonical
`external_event` 进入 EventLedger，不作为普通 user/assistant/system history；文字、媒体和可选
Agent turn 由持久 Outbox 独立发送。当前通知只在其后台 turn 的 current input 出现一次，
常规会话不自动附加旧外部事件摘要；需要历史事实时由 Agent 显式检索。Grant 的增删要求原 actor Person 的当前有效超管资格。

`plugin_id + event_key + target` 只负责定位，Host 还会校验完整请求身份：`external_source`、
`event_type`、时间、payload、summary、text、`ask_agent`、intent 与按顺序排列的媒体 SHA。
原位置的原 handle 使用原已接纳媒体证明；换用另一个 handle 时须证明内容 SHA 相同。媒体的当前
有效期只约束首次接纳，过期或清理不否定原 receipt。请求内容冲突返回 `receipt_conflict`；
已存在路径只读复用，不能给旧通知补 text、media 或 Agent job。插件必须在第一次 attempt 前
确定请求；结果未知时沿原请求身份恢复。

## Automation

```python
automation.list_current_owner() -> tuple[Mapping, ...]
automation.create_task(task: Mapping[str, JsonValue], *, request_id: str, max_runs: int | None = None) -> PluginResult
automation.update_task(task_id, task: Mapping[str, JsonValue], *, max_runs=...) -> PluginResult
automation.pause(task_id) -> PluginResult
automation.resume(task_id) -> PluginResult
automation.cancel(task_id) -> PluginResult
```

task 只要求完整 goal 和 trigger，可选公开 name；不再传 version、constraints、delivery 或 task 级 timezone。
max_runs 位于调用参数，create 缺省/null 不限；update 缺省保留、null 解除、正整数修改。
name 在 create 缺省/null 为中性名称，update 缺省保留/null 清除。返回 data.automation，task_id 是整数型字符串。
完整时间、幂等和迁移说明见[定时安排](../automation.md)。
Facade 复用 Host 原 actor 与会话，SELF 不需要制造真人入站消息。

## Config / Secret / Storage

```python
config.get(key, *, scope_type="global", scope_id="") -> JsonValue
config.set(key, value, *, scope_type="global", scope_id="") -> None
secrets.configured(name) -> bool
secrets.get(name) -> str
storage.get(namespace, key) -> JsonValue
storage.set(namespace, key, value) -> None
storage.delete(namespace, key) -> bool
storage.list(namespace) -> Mapping[str, JsonValue]
storage.compare_and_set(namespace, key, expected, value) -> bool
```

## Scheduler / OneBot / Events

```python
scheduler.create_task(name, runner) -> str
scheduler.cancel(task_id) -> bool
scheduler.sleep_until_stopped() -> None
onebot.send_music_card(*, provider, resource_id) -> PluginResult
onebot.send_private(user_id, text) -> PluginResult
onebot.send_group(group_id, text) -> PluginResult
onebot.call_read_action(action, params) -> PluginResult
onebot.call_mutating_action(action, params) -> PluginResult
events.publish(EventEnvelope) -> None
```

`PluginContext` 还提供 `plugin_id`、隔离 `logger`、脱敏 `current` 和 `FeatureRegistry features`。

`ctx.agent.run`和`ctx.agent.result(work_id)`沿原SDK投影返回完整text、state、work_id、pending、
reason及原统计，同步与异步不因等待时间不同丢字段。普通PluginResult.data中的同名字段
仍只是业务数据；自动化的控制和计量来自Host私有类型化执行事实，读取结果不转计旧累计。
需要补充资料时调用
`ctx.agent.resume(work_id, text, request_id="稳定的调用标识")`，在原历史后追加并交给 Host 续跑。
同一 request_id 重放复用原输入，不同内容明确冲突；已归档、取消或完成的工作不会另建新任务。

定時任務 create_task 的 request_id 標識本次插件操作，同一操作重試使用原 ID；不同任務使用不同 ID。Host 與真實插件、來源及原調用共同綁定，改參數重用同 ID 返回 creation key conflict。
