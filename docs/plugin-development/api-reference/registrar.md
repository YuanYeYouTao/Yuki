# PluginRegistrar Reference

`PluginRegistrar` 只在 `register()` 有效，不提供运行时 Facade。

```python
register_tool(ToolRegistration) -> None
register_command(CommandRegistration) -> None
register_event_hook(EventHookRegistration) -> None
register_prompt_fragment(PromptFragment) -> None
register_admission_signal(AdmissionSignalRegistration) -> None
register_config_schema(type[BaseModel]) -> None
register_background_service(BackgroundServiceRegistration) -> None
```

## Metadata

```python
class ToolMetadata(StrictModel):
    name: str  # [a-z][a-z0-9_]{0,63}
    description: str  # 1..1000
    permission: PermissionLevel = user
    risk: RiskClass = read
    schema_version: int = 1
    timeout_seconds: float | None = None  # explicit values are >0
    retry_policy: RetryPolicy = none
    namespace: str = ""  # empty → Host fills plugin.{plugin_id}
    aliases: tuple[str, ...] = ()  # <=8, lowercase, unique
    use_when: tuple[str, ...] = ()  # <=8, each 1..200
    tags: tuple[str, ...] = ()  # <=8, lowercase, unique


class CommandMetadata(StrictModel):
    name: str  # [a-z][a-z0-9_-]{0,63}
    description: str  # 1..1000
    permission: PermissionLevel = user
    short_alias: str | None
    timeout_seconds: float = 10


class EventHookMetadata(StrictModel):
    id: str  # [a-z][a-z0-9_.-]{0,127}
    event: EventName
    priority: int = 0  # -10000..10000
    timeout_seconds: float | None


class BackgroundServiceMetadata(StrictModel):
    name: str  # [a-z][a-z0-9_]{0,63}
    description: str = ""
    shutdown_timeout_seconds: float = 10
    max_concurrency: int = 1  # 1..64
    restart_policy: RestartPolicy = never
```

Tool 未声明期限时不制造外层超时；作者明确的期限、真实外部请求超时与取消
仍沿原执行处生效。输入在派发前只解析一次，typed handler收到声明的模型实例；返回的对应
output实例不再dump后重复解析。TRUSTED/MODERATOR仍是未指派的保留角色，不降成USER执行。

## Registration containers

- `ToolRegistration(metadata, input_model, output_model, handler)`
- `CommandRegistration(metadata, argument_model, handler)`
- `EventHookRegistration(metadata, handler)`
- `AdmissionSignalRegistration(name, provider)`
- `BackgroundServiceRegistration(metadata, runner)`

所有输入/输出/参数/配置 Schema 必须是 Pydantic 模型并设置 `extra='forbid'`。推荐继承 `StrictModel`。

