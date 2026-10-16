# 配置与 Secret

配置与 Secret 是两条独立通道：普通配置可读取、校验、按作用域覆盖；Secret 只能按名字获取，不进入配置列表、日志、`repr`、Prompt 或工具结果。

## 注册配置 Schema

```python
from yuki_plugin_sdk.models import StrictModel


class WeatherConfig(StrictModel):
    units: str = "metric"
    alerts_enabled: bool = True


registrar.register_config_schema(WeatherConfig)
```

需要 `plugin.config.read` 或 `plugin.config.write`；实际读取和写入分别要求对应权限。一个插件只能注册一个自己的配置 Schema，不能反射 Yuki `Settings`。

## 配置作用域

```python
global_value = await ctx.config.get("units", scope_type="global")
user_value = await ctx.config.get("units", scope_type="user", scope_id=user_id)
group_value = await ctx.config.get("units", scope_type="group", scope_id=group_id)

await ctx.config.set("units", "imperial", scope_type="group", scope_id=group_id)
```

- `global` 的 `scope_id` 必须为空。
- `user`/`group` 必须给出真实可访问的内部 Person/Space ID。
- Facade 会绑定当前真实上下文，插件不能借参数跨作用域。
- 值必须能表示为 `JsonValue` 并通过注册 Schema。

建议在插件内明确实现回退顺序，例如 `group → user → global → Schema default`，不要依赖未声明的 Host 隐式规则。

## Secret

```python
if ctx.secrets.configured("weather_api_key"):
    token = ctx.secrets.get("weather_api_key")
```

不要：

- 将 Secret 放进 `plugin.toml`、普通配置或 KV；
- 在异常、审计、HTTP URL 查询串、Prompt、LLM 或 `PluginResult` 中返回 Secret；
- 提供“列出所有 Secret”的插件接口；
- 缓存 Secret 到模块级长期对象。

部署者通过 Host 支持的 Secret 配置渠道提供值。插件发布包只能记录所需 Secret 名称和用途，不能包含真实值。

