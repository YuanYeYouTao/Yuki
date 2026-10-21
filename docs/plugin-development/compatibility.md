# 兼容性

Yuki 产品版本、Plugin API、Feature 和各 Schema 版本相互独立。

## Plugin API

- 当前 `7.1`；同一 major 内，Host 接受不高于自身 minor 的插件合同：7.0、7.1 可加载；7.2 需要更新 Host。不同 major 仍需完成对应破坏性迁移。
- 1.x、2.x、3.0、3.1 和其它 3.x 次版本都会在导入插件代码前被拒绝，没有半加载兼容层。
- 旧表情 facade、选择信号、专属权限与生命周期事件已退役；现行通知只声明真实发布点。Manifest 批准事实仍沿现行 Host 核验。
- 插件不得依赖 `_` 开头属性、Host 类或数据库表结构。

从 3.1 升级先完成 [API 3.2 语音退役](api-3.2-migration.md)，再完成 [API 3.4 迁移](api-3.4-migration.md)。更早版本先核对 [API 3.1 历史迁移](api-3.1-migration.md)、[API 3.0 历史迁移](api-3.0-migration.md)及 [API 2.0 历史迁移](api-2.0-migration.md)，再完成 [API 7.0 迁移](api-7.0-migration.md)，按当前合同适配。

Manifest 同时使用：

```toml
plugin_api = "7.1"
yuki_requires = ">=4.0.0,<5.0"
```

## Feature 探测

```python
if ctx.features.has("admission.signal.v1"):
    ...
ctx.features.require("plugin.agent_session.v2")
```

7.1 默认 Feature 包括：

- `message.normalized.v1`
- `message.history.v2`
- `message.current.mentions.v2`
- `prompt.fragment.v1`
- `admission.signal.v1`
- `plugin.agent_session.v2`
- `notification.facade.v1`
- `media.artifact.v1`
- `http.credential.v1`

不要因为 Yuki 版本“看起来足够新”就假设部署者启用了某 Feature。

## Schema 版本

工具和 Event 各自声明 Schema 版本。定时管理使用共同 TaskSpec；旧定义、版本与已发生回执仍按原件保存，不被新任务定义改写。

当前新增字段和检索参数见 [API 7.1 迁移](api-7.1-migration.md)；旧关系服务退役见 [API 3.4 迁移](api-3.4-migration.md)。
