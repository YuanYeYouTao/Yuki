# 后台服务

短生命周期轮询或缓存维护可以注册后台服务；用户定时任务必须使用 Automation。

```python
from yuki_plugin_sdk.models import RestartPolicy
from yuki_plugin_sdk.registrar import (
    BackgroundServiceMetadata,
    BackgroundServiceRegistration,
)


async def worker() -> None:
    while True:
        await run_one_bounded_iteration()
        await asyncio.sleep(30)


registrar.register_background_service(
    BackgroundServiceRegistration(
        metadata=BackgroundServiceMetadata(
            name="cache_refresh",
            description="刷新插件内存缓存",
            shutdown_timeout_seconds=5,
            max_concurrency=1,
            restart_policy=RestartPolicy.ON_FAILURE,
        ),
        runner=worker,
    )
)
```

需要 `background.worker`，并受 Manifest `limits.background_tasks` 和 Host `PLUGIN_BACKGROUND_TASK_LIMIT` 双重限制。

## 规则

- 不要创建未跟踪的永久 `asyncio.Task`。
- 临时派生任务使用 `ctx.scheduler.create_task(name, runner)`，并在停止时取消。
- 循环必须可取消，有限处理单批，避免同步阻塞。
- `NEVER` 失败后结束该 runner；`ON_FAILURE` 失败后重启同一 runner。其他后台服务、工具和插件批准不受牵连，失败类别记录在 Host 日志中。
- 不在内存后台任务中实现持久提醒、定时发送或跨重启业务流程。
- `stop()` 必须让任务在声明的关闭超时内结束。


通知 `NotificationTarget.target_id` 为内部 Person/Space，`conversation_id` 为原授权入口；同 Space 的不同 topic、同 Person 的不同平台/私聊入口分别保存授权与去重身份。授权不从裸平台号推断。已有事件、Job、outbox 和 receipt 沿原 ID 读取；迁移前 target_id 的原传输出处只参与精确旧请求回执比对，不据此重新建立所有者或重发。

`notifications.publish` 对已接受的原请求只读取原回执，不因临时媒体过期或已被清理而重新发布。原 outbox 保留实际媒体 handle，现代媒体按原 SHA 比对；缺少 SHA 的旧格式按完整原请求与原 handle 证明比对，不伪造摘要。临时媒体文件和 artifact 行照常回收，新发布和真实媒体读取仍核有效期。原证明早已缺失时不能猜测或重发。

首次发布在接纳事务内、首个写入前复核媒体的归属、有效期与原 SHA，再一起保存事件和 outbox。
清理只查已过期的插件临时媒体，短事务复核待发送引用后在事务外删文件；文件删除失败或清理被取消时，
原路径记录保留给下轮清理。收到的附件原件与持久表情库不属于此清理范围。

Main 后台 Job 的普通轮询分别按索引发现尚未绑定 Work 的任务、可继续的原 Work 和过期 processing 租约，
暂停的 Work 不领取、不增加尝试次数。原 Work 已结束或历史关联缺失时，启动先对账，此后每 60 秒按原结果
收尾 Job，保留原 ID、用量和错误类别；不把终态对账扫描放进每秒的领取热路。
