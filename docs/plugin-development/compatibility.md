# 兼容性

Yuki 产品版本、Plugin API、Feature 和各 Schema 版本相互独立。

## Plugin API

- 当前 `3.3`；Host 只加载精确声明 `3.3` 的插件。
- 1.x、2.x、3.0、3.1 和其它 3.x 次版本都会在导入插件代码前被拒绝，没有半加载兼容层。
- SDK 合同变化时直接提升 Plugin API 版本，并要求插件同步升级。
- 插件不得依赖 `_` 开头属性、Host 类或数据库表结构。

从 3.1 升级先完成 [API 3.2 语音退役](api-3.2-migration.md)，再完成 [API 3.3 迁移](api-3.3-migration.md)。更早版本先核对 [API 3.1 历史迁移](api-3.1-migration.md)、[API 3.0 历史迁移](api-3.0-migration.md)及 [API 2.0 历史迁移](api-2.0-migration.md)，最终按 3.3 的精确合同适配。

Manifest 同时使用：

```toml
plugin_api = "3.3"
yuki_requires = ">=3.9.0,<4.0"
```

## Feature 探测

```python
if ctx.features.has("admission.signal.v1"):
    ...
ctx.features.require("plugin.agent_session.v1")
```

3.3 默认 Feature 包括：

- `message.normalized.v1`
- `message.current.mentions.v1`
- `prompt.fragment.v1`
- `admission.signal.v1`
- `automation.action.v1`
- `plugin.agent_session.v1`
- `emoji.facade.v1`
- `emoji.selection_signals.v1`
- `notification.facade.v1`
- `media.artifact.v1`
- `http.credential.v1`

不要因为 Yuki 版本“看起来足够新”就假设部署者启用了某 Feature。

## Schema 版本

工具、自动化 Action 和 Event 各自声明 Schema 版本。改变必填字段、类型或语义属于不兼容变更，应新建组件名或提升 Schema 并迁移；旧 Automation 不会自动套用新 Schema。
