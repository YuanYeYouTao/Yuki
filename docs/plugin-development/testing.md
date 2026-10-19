# 测试插件

插件测试不得连接真实 QQ、DeepSeek、Qwen、Tavily 或外部 HTTP。`yuki_plugin_sdk.testing` 提供网络为空的 Facade Fake。

## 生命周期契约

```python
from pathlib import Path
from yuki_plugin_sdk.testing import run_plugin_contract_tests


async def test_contract() -> None:
    report = await run_plugin_contract_tests(Path(__file__).parents[1])
    assert report.passed, report.error_category
```

契约检查 Manifest、权限、入口、`register`、`start` 和 `stop`，不会连接外部系统。

## FakePluginContext

```python
from datetime import UTC, datetime
from yuki_plugin_sdk.models import CurrentMessage
from yuki_plugin_sdk.testing import FakePluginContext

ctx = FakePluginContext("com.example.plugin")
person_id = "11111111-1111-4111-8111-111111111111"
space_id = "22222222-2222-4222-8222-222222222222"
ctx.messages.current = CurrentMessage(
    message_id="m-1",
    platform="telegram",
    sender_user_id="42",
    group_id="-10042",
    person_id=person_id,
    space_id=space_id,
    scope_type="group",
    text="hello",
    received_at=datetime.now(UTC),
)
await ctx.config.set("mode", "short", scope_type="user", scope_id=person_id)
await ctx.config.set("mode", "short", scope_type="group", scope_id=space_id)
await ctx.storage.set("state", "count", 1)
```

`person_id` / `space_id` 是业务与配置作用域的内部 ID。`sender_user_id` / `group_id`
在 `CurrentMessage` 中仅记录原传输出处，不能用作 API 6.0 的人物或群选择参数。

可用 Fake 覆盖 Message、People、Group、Memory、Agent、AgentSession、Web、HTTP、Vision、Media、Notification、Automation、OneBot、Config、Secrets、Storage、Scheduler 和 EventBus。

## 推荐测试矩阵

- Manifest 合法、API/Yuki 不兼容、权限变化重新批准。
- 注册项名称冲突和 Schema 严格性。
- 普通用户与超级管理员路径分别验证。
- 群/用户/插件作用域不能越界。
- 图片、网页和自动化来源不会扩大写权限。
- Hook 超时/异常不会破坏主流程。
- Secret 不出现在异常、日志和结果。
- 独立 AI 会话不混入主聊天，`reset/close` 正确。
- 后台任务可取消，`stop()` 幂等。

运行 Echo 示例：

```bash
uv run pytest -q examples/plugins/com.example.echo/tests
uv run ruff check examples/plugins/com.example.echo
```

