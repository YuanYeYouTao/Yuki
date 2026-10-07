# 事件与通知 Hook

事件是不可变 `EventEnvelope`：`event_id`、`name`、`schema_version`、`occurred_at`、`payload`。插件 Hook 是通知型观察者，不能修改原事件或主聊天流水线。

```python
from yuki_plugin_sdk.events import EventEnvelope, EventName
from yuki_plugin_sdk.registrar import (
    EventHookMetadata,
    EventHookRegistration,
)


async def on_reply_sent(event: EventEnvelope) -> None:
    sent = event.payload.get("sent", False)
    logger.debug("reply.sent: %s", sent)


registrar.register_event_hook(
    EventHookRegistration(
        metadata=EventHookMetadata(
            id="observe_reply_sent",
            event=EventName.REPLY_SENT,
            priority=0,
        ),
        handler=on_reply_sent,
    )
)
```

需要 `event.subscribe`。

3.6.0 删除 `planner.*` 事件。当前准入、自主拒绝和回合结束使用 `turn.admitted`、`turn.rejected`、`turn.autonomous_declined`、`turn.closed`。旧本地能力搜索事件随目录查询入口删除；`agent.*` 与 `reply.*` 继续复用。映射表见 [API 2.0 迁移](api-2.0-migration.md)。这些 payload 只有 origin、scope、hash、分数、原因码、工具 id 和延迟，不含聊天正文。

## 执行语义

- 同一事件的 Hook 按优先级从高到低，再按插件 ID/Hook ID 稳定排序并并行调用。
- 每个 Hook 使用自身 `timeout_seconds` 或 Host 默认值。
- 核心聊天、回复和 Emoji 的元数据通知使用进程内有界 FIFO，由应用生命周期托管的
  单个消费者投递。请求链仅入队，不等待 Hook；同一回合的通知按发布顺序投递，后一事件
  在前一事件所有 Hook 完成或超时后处理。默认最多 256 条、序列化数据共 1 MiB，包含正在投递的事件。
- 入队时冻结 payload 和订阅快照；新订阅不会收到之前排队的事件。取消订阅或停止插件会取消
  在途 Hook，失效订阅不会继续消费旧通知，重新启动的同名 Hook 也不接收旧快照。
- 超时、异常和慢 Hook 记录脱敏日志；队满、字节超限、关闭和失效订阅导致的丢弃计数可由
  `plugin_events` 生命周期 health 查看。关闭时最多等待消费者取消 2 秒，并丢弃剩余通知。
- SDK `ctx.events.publish()` 仍等待该事件的 Hook 完成或超时。准入信号、Emoji 选择信号、
  Prompt 贡献及权限核验使用各自的原有接口，不从通知队列获取决策结果。
- 通知不属于领域事务，不保证投递；必须容忍丢失、重复和进程重启。已提交的发送、事实和任务回执
  不依赖通知成功，也不会因通知丢弃而重新执行。
- 不要在 Hook 中执行长耗时工作；将短任务交给 `ctx.scheduler`，持久任务交给 Automation。
- `payload` 是按事件定义的 JSON 值投影，不是原始 OneBot/NoneBot 对象。

完整事件名见 [Event Catalog](api-reference/events.md)。

