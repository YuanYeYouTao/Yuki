# 兼容性

Yuki 产品版本、Plugin API、Feature 和各 Schema 版本相互独立。

## Plugin API

- 当前 `3.5`；Host 只加载精确声明 `3.5` 的插件。
- 1.x、2.x、3.0、3.1 和其它 3.x 次版本都会在导入插件代码前被拒绝，没有半加载兼容层。
- 3.5 删除未实现的事件与运行时配置权限，现行通知只声明真实发布点；Manifest 批准事实仍沿现行 Host 核验。
- 插件不得依赖 `_` 开头属性、Host 类或数据库表结构。

从 3.1 升级先完成 [API 3.2 语音退役](api-3.2-migration.md)，再完成 [API 3.4 迁移](api-3.4-migration.md)。更早版本先核对 [API 3.1 历史迁移](api-3.1-migration.md)、[API 3.0 历史迁移](api-3.0-migration.md)及 [API 2.0 历史迁移](api-2.0-migration.md)，再完成 [API 3.5 迁移](api-3.5-migration.md)，按当前精确合同适配。

Manifest 同时使用：

```toml
plugin_api = "3.5"
yuki_requires = ">=3.9.0,<4.0"
```

## Feature 探测

```python
if ctx.features.has("admission.signal.v1"):
    ...
ctx.features.require("plugin.agent_session.v1")
```

3.5 默认 Feature 包括：

- `message.normalized.v1`
- `message.current.mentions.v1`
- `prompt.fragment.v1`
- `admission.signal.v1`
- `plugin.agent_session.v1`
- `emoji.facade.v1`
- `emoji.selection_signals.v1`
- `notification.facade.v1`
- `media.artifact.v1`
- `http.credential.v1`

不要因为 Yuki 版本“看起来足够新”就假设部署者启用了某 Feature。

## Schema 版本

工具和 Event 各自声明 Schema 版本。定时管理使用共同 TaskSpec；旧定义、版本与已发生回执仍按原件保存，不被新任务定义改写。

当前合同见 [API 3.5 迁移](api-3.5-migration.md)；旧关系服务退役见 [API 3.4 迁移](api-3.4-migration.md)。
