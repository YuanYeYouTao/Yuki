# Event Catalog v1

来源：`yuki_plugin_sdk.events.EventName`。所有事件使用不可变 `EventEnvelope`，默认 `schema_version=1`。

核心元数据通知异步入队并按发布顺序投递，采用进程内有界队列，允许在队满、取消订阅和关闭时丢弃。
SDK `ctx.events.publish()` 仍等待 Hook；事件名中的 `before`、`collecting` 或 `admitted`
不表示该通知具有审批或改变决策的权限。准入、Prompt、Emoji 选择和权限接口保持独立。
详见 [执行语义](../events.md#执行语义)。

## 应用

- `application.started`
- `application.stopping`

## 消息

- `message.normalized`
- `message.triggered`

## 回合

- `turn.admitted`
- `turn.rejected`
- `turn.autonomous_declined`
- `turn.closed`

## 回复

- `reply.sent`

## 表情

- `emoji.adopted`
- `emoji.rejected`
- `emoji.banned`
- `emoji.sent`
- `emoji.collected`
- `emoji.analyzed`
- `emoji.unadopted`
- `emoji.before_select`
- `emoji.after_select`
- `emoji.queued`
- `emoji.prepare_ready`
- `emoji.prepare_no_candidate`
- `emoji.prepare_failed`
- `emoji.send_accepted`
- `emoji.usage_recorded`
- `emoji.usage_record_failed`
- `emoji.missing`
- `emoji.restored`

```python
class EventEnvelope(StrictModel):
    event_id: UUID
    name: EventName
    schema_version: int = 1
    occurred_at: datetime
    payload: Mapping[str, JsonValue]


class HookExecution(StrictModel):
    plugin_id: str
    hook_id: str
    success: bool
    duration_seconds: float
    error_category: str | None
```

具体 `payload` 字段由事件 Schema 版本定义；插件必须忽略未知可选字段，并在执行前验证需要的值。

