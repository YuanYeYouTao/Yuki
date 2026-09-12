# 注册 Agent 工具

工具是由 Yuki 主 Agent 按需调用的结构化函数。需要 `tool.register`，工具内部使用的 Facade 还需各自权限。

```python
from yuki_plugin_sdk.models import (
    PermissionLevel,
    RiskClass,
    StrictModel,
    TurnOrigin,
)
from yuki_plugin_sdk.registrar import ToolMetadata, ToolRegistration


class DiceInput(StrictModel):
    sides: int


class DiceOutput(StrictModel):
    value: int


async def roll(arguments: DiceInput) -> DiceOutput:
    if not 2 <= arguments.sides <= 1000:
        raise ValueError("sides out of range")
    return DiceOutput(value=secure_roll(arguments.sides))


registrar.register_tool(
    ToolRegistration(
        metadata=ToolMetadata(
            name="roll_dice",
            description="掷一个指定面数的骰子。",
            permission=PermissionLevel.USER,
            risk=RiskClass.GENERATE,
            schema_version=1,
            allowed_origins=frozenset({TurnOrigin.USER_MESSAGE}),
            timeout_seconds=5,
            namespace="",
            aliases=(),
            use_when=("用户要求掷骰或随机整数时",),
            tags=("game",),
        ),
        input_model=DiceInput,
        output_model=DiceOutput,
        handler=roll,
    )
)
```

## Schema

输入和输出必须是 Pydantic 模型，且 `extra='forbid'`。推荐继承 SDK `StrictModel`。Host 会在注册时生成 JSON Schema，在调用前后都校验；未知字段、错误类型和越界值不会静默通过。

工具本地名必须匹配 `[a-z][a-z0-9_]{0,63}`。`namespace` 可留空，Host 会填 `plugin.{plugin_id}`；不能使用保留前缀。`aliases` / `tags` 最多 8 个且必须小写不重复；`use_when` 最多 8 条、每条 1–200 字符。Host 生成全局唯一的模型工具名并处理过长名称，插件不要依赖该内部变换。

## 可见性

主 Agent 的工具声明在启动时冻结，目录检索不会增删声明。执行插件工具仍须同时满足：

- 插件运行且批准仍匹配当前 Manifest；
- 注册权限和工具所需服务权限仍有效；
- 当前真实用户达到 `permission`；
- 当前 `TurnOrigin` 在 `allowed_origins`；
- 只读模式下，工具风险被 Host 判定为只读；
- 适用的图片、联网限制与自动化委托允许本次操作。

插件不能借工具声明可见绕过执行授权，也不能在主循环中追加定义。`ToolResult`/输出数据作为不可信工具结果传给模型，不能成为系统指令或权限凭证。

## 超时、重试和副作用

明确设置 `RiskClass`：`read`、`generate`、`send`、`mutate`、`destructive`。修改型工具必须幂等或使用业务幂等键；默认 `RetryPolicy.NONE`。只有确定可安全重试的瞬时只读操作才使用 `transient_once`。

