# 架构

Plugin API 3.3 把“可声明的扩展”和“可使用的运行时服务”分开：

```text
plugin.toml
  → Discovery（只读 TOML，不导入代码）
  → Compatibility + Manifest hash
  → Administrator approval
  → Loader（本地可信 Python）
  → register(PluginRegistrar)
  → start(bound PluginContext)
  → events / tools / commands / automations / background services
  → stop()
```

## SDK 与 Host

- `yuki_plugin_sdk`：稳定、依赖轻的公开 Protocol、Pydantic 模型、枚举和测试 Fake。
- `qq_ai_bot.plugin_host`：Host 私有实现，负责发现、批准、名称冲突、事件超时、数据隔离和权限校验。
- 插件：只能依赖 SDK。不要导入 `qq_ai_bot`、访问 `_` 开头属性或保存 Host 内部对象。

`register()` 得到的是声明型 `PluginRegistrar`，没有运行时 Facade；`start()` 才得到已绑定 `plugin_id`、真实当前用户/群和已批准权限的 `PluginContext`。

## Conversation Runtime 主聊天

主聊天由 Conversation Runtime 做确定性准入，再调用单一 Main Agent。私聊、@ 与回复机器人直接进入 Agent；群自主插话先评分，再决定是否回复。Runtime 不能授予权限；主 Agent 使用启动时冻结的完整工具声明，真实权限在执行处检查。参见 [共同架构约束](../architecture/development-contract.md)。

插件可以贡献有界 `AdmissionSignal`，但只影响自主群评分，且总和会被 Host 裁剪。确定性 `/ai` 命令仍绕过该评分。

## 独立插件 AI 会话

`ctx.agent_sessions` 用于跑团、游戏主持、插件向导等需要独立连续历史的功能：

- 会话键为 `plugin-session:<plugin_id>:<uuid>`；
- 历史只在 `plugin_agent_messages` 中，默认不读取 Yuki 主聊天、人物记忆或群记忆；
- 不写入 `chat_events`；
- 不向插件返回隐藏推理；
- 能力始终取“插件声明 ∩ 管理员批准 ∩ 本轮请求”；
- 插件会话永远不能伪造 `SUPERUSERS`。

这不是第二套 Yuki 人格或管理员路由，而是插件拥有的隔离任务会话。完整用法见 [服务 Facade](service-facades.md)。

## 数据所有权

| 数据 | 所有者 | 插件访问方式 |
|---|---|---|
| 主聊天账本、人物、关系、记忆 | Yuki Core | 对应只读/写 Facade + 权限 |
| 插件配置 | Host，按插件/作用域隔离 | `ctx.config` |
| 插件 Secret | Host/部署者 | `ctx.secrets`，只按名称读取 |
| 插件 KV | 插件命名空间 | `ctx.storage` |
| 准入与会话观测 | Yuki Core | 无原始数据库访问 |
| 独立插件 AI 历史 | Host，按插件和会话隔离 | `ctx.agent_sessions` |

所有 Facade 都是能力边界，不是 Repository 的别名；插件永远不能获得 SQLAlchemy Session。

独立会话创建、追加和重置在首次 DML 前解析 canonical owner、消息发送者与返回 DTO 的
Binding 投影。投影只读取确定排序后的首个可展示绑定，不用它选择业务所有者。
追加的序号分配与消息写入仍在同一短事务内；身份拒绝不会消耗序号。
插件 KV 的 CAS 同样先核验所有权并准备展示字段，再按原版本条件写入。

## 管理界面的可选只读观察

需要提供插件领域状态时可实现 `yuki_plugin_sdk.observation.ObservablePlugin`：
`async observe(context: PluginObservationContext, request: PluginObservationRequest) -> JsonObject`。
这不是新的必需生命周期；只实现 register/start/stop 的旧插件仍能运行。
请求有 repository 等插件自定义 cursor（最多 512 字符）与 limit（1–20），由插件核验
分页含义，不接收任意命名空间或工具名。只有已经运行且获准的实例可被观察。

context 仅有 `get_config(key)` 和 `get_state(namespace, key)`，绑定自身插件、全局 config
及原批准权限；没有写操作、HTTP、Secret、通知或 Agent 服务。callback 必须无副作用，
从原插件状态投影审核过的字段，不返回 Secret、私有 payload 或未来执行的冻结原文。
Host 在 Manager lock/写事务外调用，工厂与 callback 最多 5 秒、JSON 结果最多 64 KiB，
生命周期发生变化则拒绝旧投影。仍是可信 Python 插件合同，不能替代 OS 隔离。
GitHub Monitor 的实现参考 `github_monitor/observation.py`；Host 不解释插件私有 KV。

