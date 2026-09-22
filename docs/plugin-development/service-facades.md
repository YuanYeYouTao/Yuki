# 服务 Facade

`PluginContext` 是 Host 在 `start()` 阶段绑定的能力集合。它不暴露 Settings、Container、Repository、数据库 Session、NoneBot Bot 或原始 `MessageEvent`。

| 属性 | 主要用途 |
|---|---|
| `current` | 当前脱敏 `CurrentMessage` 投影 |
| `messages` | 当前/回复/近期/搜索，以及受限发送 |
| `people`, `groups` | 人物、别名、群和成员投影 |
| `memory`, `relationship` | 结构记忆与关系服务 |
| `llm`, `agent` | 一次生成或受控 Agent 运行 |
| `agent_sessions` | 插件拥有的独立连续 AI 会话 |
| `web`, `http` | Yuki 联网与白名单 HTTP |
| `vision`, `media` | 当前真实媒体的受控分析 |
| `automation` | 当前所有者的持久化任务 |
| `mcp` | MCP Server 状态、目录检索与工具调用 |
| `config`, `secrets`, `storage` | 插件配置、Secret 和私有 KV |
| `scheduler` | Host 托管的短生命周期后台任务 |
| `onebot` | 按读/发送/修改分类的 OneBot 接口 |
| `events` | 发布类型化通知事件 |

完整签名见 [Facade API Reference](api-reference/facades.md)。每次调用仍会检查当前批准权限；持有一个 Python 属性不等于拥有调用权限。

## 显式消息投递与回执

`messages.send_*`、`onebot.send_*` 与高权限 `call_mutating_action` 的
`send_private_msg` / `send_group_msg` 使用同一 Host 投递记录。纯文本及消息段中的 `text`
先移除控制字符和内部历史前缀，净化后的文字同时用于投递和新账本；已有历史不回写，
显式 `at`、`reply` 和媒体消息段不改成文本。净化后没有可发送内容时拒绝发送。
`send_msg` 只接受明确且唯一的目标，归一到上述发送路径；合并转发的
`send_private_forward_msg`、`send_group_forward_msg`、`send_forward_msg` 尚无同等回执合同，
当前在发送前明确拒绝，不通过原始动作绕过。其他管理动作保留原有权限与行为。

发送前先持久化执行意图。只有严格有效的网关消息 ID 才确认成功，成功回执和账本在同一
事务提交；超时、无效回执、取消和提交失败记为 `unknown` / `uncertain=true`，不自动重试，
并停止该回调后续发送。已提交成功的回执不会因后续唤醒通知失败而降级。

每个显式发送调用使用独立序号，相同正文主动调用两次仍发送两次，不按正文去重。
工具调用使用原执行/调用 ID，自动化插件步骤使用原 run/step ID；同一可信身份按原顺序
重放只读取回执，参数或来源冲突明确拒绝。身份不因批准版本变化而更新，权限仍每次核验。
其他没有持久 callback ID 的入口仅有本次 Host 回调的唯一身份，重新调用属于新调用，
**不承诺任意插件回调的跨重启 exactly-once**；插件不得据 `unknown` 自行重跑整个回调。
没有真实来源、canonical 会话或持久账本时在网络发送前拒绝，不补造入站消息。
这不是自动最终回复，也不增加插件发送权限或新的 SDK 参数。

Memory V2 的写入仍统一经过 Host `MemoryFactService`。插件 update 创建修正版本，delete 只做显式
失效；插件不能直接访问 Repository、指定事实状态/authority、物理删除审计记录或绕过当前真实
调用作用域。冲突审计与管理员 merge/resolve 不属于 Plugin API 3.0。

## 独立 AI 会话：跑团示例

插件可为骰子跑团建立与 Yuki 主聊天完全分离的连续会话：

```python
from yuki_plugin_sdk.sessions import (
    CreateAgentSessionRequest,
    RunAgentSessionRequest,
    SessionContextProfile,
    SessionPersistence,
)

campaign = await ctx.agent_sessions.create(
    CreateAgentSessionRequest(
        name="周末克苏鲁跑团",
        instructions=(
            "你是本次跑团的守秘人。连续维护角色、场景、线索和骰点后果；"
            "不离开跑团任务，不声称拥有 Yuki 管理权限。"
        ),
        persistence=SessionPersistence.DURABLE,
        context_profile=SessionContextProfile.CURRENT_GROUP,
        allowed_capabilities=(),
    )
)

turn = await ctx.agent_sessions.run(
    RunAgentSessionRequest(
        session_id=campaign.session_id,
        user_input="调查书房里的旧书桌。",
        max_model_requests=4,
    )
)
await ctx.messages.send_text(turn.text)
```

需要 Manifest 权限 `agent.session`。`CURRENT_USER` 还需 `person.current.read`；`CURRENT_GROUP` 还需 `group.current.read` 且当前必须是真实群聊。

### 会话保证

- `DURABLE` 可跨 Host 重启；`EPHEMERAL` 仅当前 Host 生命周期有效。
- 默认 `context_profile=none`，不注入 QQ、群、主聊天、人物记忆或关系。
- 只持久化用户/助手可见正文；不返回也不存储隐藏推理。
- `allowed_capabilities` 仍取批准交集；当前 v1 初始运行层可以把工具集收窄为 `none`。
- `reset()` 只清空该会话历史；`close()` 关闭后不能继续运行。
- 会话 UUID 是不透明标识，不能用来跨插件或跨真实场景访问。

## Yuki 主 Agent 调用

`ctx.llm.generate()`、`generate_with_context()` 和 `ctx.agent.run()` 均进入 Yuki 主 Agent，使用相同固定提示词、工具声明和 short_state 编译。旧的独立 system 提示词与随机会话键已移除，允许兼容性变化。调用必须绑定真实入站事件、canonical Conversation 和 Presence；缺少来源或来源已被会话重置淘汰时明确报错，不自动选择人物或群。后台任务应通过有明确目标的通知唤醒入口发起；独立计算使用 `agent_sessions`。

`generate()` 返回文字，不自动投递；全局 short_state 工具仍可使用。`generate_with_context()` 需要对应权限，仅沿用所选人物/当前群的有限资料范围，聊天历史仅在批准 message.history.read 时按绑定会话载入 Rollup 和原文尾部。`agent.run()` 只执行插件获批且本轮允许的 capability 交集，不能传入超级管理员标志。固定工具声明不代表获准执行；递归调用这组生成接口会被拒绝。来源 generation 在每次模型请求前重新检查。

主调用复用 `MainAgentBackend`，Host 根据批准权限设置执行范围：

| Manifest 批准权限 | 可请求 capability |
|---|---|
| `agent.run` | 持久工作区、终端、安装、文件发布和环境服务 |
| `message.history.read` | 当前授权会话的近期记录、搜索、原文定位与网关补查 |
| `memory.person.read` | `get_person_memories` |
| `memory.group.read` | `get_group_memories` |
| `web.search` | `web_search` |
| `web.read` | `read_webpage` |

实际能力取批准权限、真实来源和显式能力参数的交集；省略 `allowed_capabilities` 使用 Host 批准的默认集合。普通用户仍只能读获准人物和会话；目录、查询和媒体输入不改变固定声明。宿主管理、QQ 发送、记忆写入和自动化仍需各自的委托，获得工作环境权限不自动获得这些能力。

已知原生工具差异：主 Agent 的 function 工具清单固定，但支持原生搜索的非 DeepSeek
Provider 当前会按真实批准能力决定是否提交 native 声明。原生工具由 Provider 执行，不能
声称由本地执行围栏逐次拦截；不能为统一声明外观给插件补授搜索权限。因此目前不声称
所有 Provider 的所有入口都具有相同 native 字节合同。这仍是共同架构约束中“原生工具
部署级固定”尚待单独治理的差异，不是已经通过的验收。当前 DeepSeek 路由剔除 native
search，不受此项差异影响；不依赖 `tool_choice` 实现权限控制。

## MCP Facade

`ctx.mcp.status/list_servers/search_tools` 需要 `mcp.read`；
`ctx.mcp.call(server_id, tool_name, arguments)` 需要 `mcp.call`。Facade 复用宿主唯一
`MCPManager`，不会创建插件私有连接池，也不会向插件暴露 Session、Header 或环境 Secret。

这两项是 Plugin Host 的能力批准，不是针对每个 MCP Tool 的审批；Server 是否可用仍只取决于
Yuki 配置和启停状态。

## 当前会话音乐卡片

`ctx.onebot.send_music_card(provider=..., resource_id=...)` 使用 `onebot.send` 权限，将
OneBot `music` 消息段发送到触发插件的当前真实私聊或群聊。插件不能为这个方法传入 QQ 号或
群号，因此它不能跨会话改变目标；Host 会再次验证当前事件、provider、资源 ID、图片轮次隔离和
发送权限，成功后再写事件账本与脱敏审计。

当前 provider 支持 `qq`、`netease`（发送时规范化为 `163`）、`kugou`、`kuwo` 和 `migu`。
如果资源来自 MCP、网页或其他外部数据，插件应先做结构校验和重名消歧，不得把自定义 URL 当成
资源 ID。任意 OneBot action 仍必须走权限更高的 `call_mutating_action`，不能借音乐卡片 Facade
绕过。

独立长期故事、跑团或游戏状态使用 `agent_sessions`；不要把大量连续历史塞进一次 `llm.generate()`。

## 持续调用的状态

Runtime 升级后，`agent.run` 返回 `state`、`work_id`、`pending`。
`llm.generate` / `generate_with_context` 成功仍返回字符串，未完成返回包含上述字段的 `PluginResult`。
Host 等待约 5 秒后可返回持久任务句柄，生成由 Host 继续持有。
`await ctx.agent.result(work_id)` 可在原回调退出后查询本插件的任务；检查批准版本、权限和 generation。
同一合法 invocation 用同样参数接回原工作；不要将等待结果当正文发送。分段、恢复和重复查询不重置预算。
新的后台目标仍通过通知 API 接纳，不能伪造调用者。已经接纳的 SDK 工作由 Host 调度器恢复；
普通 `agent_sessions` 保持独立。详见 [主入口执行合同](../architecture/main-agent-runtime.md)。
