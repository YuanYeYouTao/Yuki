# 服务 Facade

`PluginContext` 是 Host 在 `start()` 阶段绑定的能力集合。它不暴露 Settings、Container、Repository、数据库 Session、NoneBot Bot 或原始 `MessageEvent`。

| 属性 | 主要用途 |
|---|---|
| `current` | 当前脱敏 `CurrentMessage` 投影 |
| `messages` | 当前/回复/近期/搜索，以及受限发送 |
| `people`, `groups` | 人物、别名、群和成员投影 |
| `memory` | 结构记忆服务 |
| `agent` | 主 Agent 运行、原 Work 结果读取与续跑；`run` 显式选择 `context_profile` |
| `agent_sessions` | 插件拥有的独立连续 AI 会话 |
| `web`, `http` | Yuki 联网与白名单 HTTP |
| `vision`, `media` | 当前真实媒体的受控分析 |
| `automation` | 当前所有者的持久化任务 |
| `config`, `secrets`, `storage` | 插件配置、Secret 和私有 KV |
| `scheduler` | Host 托管的短生命周期后台任务 |
| `onebot` | 按读/发送/修改分类的 OneBot 接口 |
| `events` | 发布类型化通知事件 |

API 7.0 的普通 user_id/group_id/target_id 参数分别表示内部 Person/Space，不接受平台裸号；`CurrentMessage` 的 sender_user_id/group_id/message_id 仍只是原传输出处。需要选择 topic 或多 Bot 私聊入口时使用原 Conversation ID。

完整签名见 [Facade API Reference](api-reference/facades.md)。每次调用仍会检查当前批准权限；持有一个 Python 属性不等于拥有调用权限。

## 显式消息投递与回执

`messages.send_*` 经共同 Social 意图、授权与回执发送；同场景保留原 Conversation、Presence 和 topic。独立批准的 QQ 扩展 `onebot.send_*` 与 `call_mutating_action` 的
`send_private_msg` / `send_group_msg` 保留显式 QQ 传输边界与原 Host 投递记录。纯文本及消息段中的 `text`
先移除控制字符和内部历史前缀，净化后的文字同时用于投递和新账本；已有历史不回写，
显式 `at`、`reply` 和媒体消息段不改成文本。净化后没有可发送内容时拒绝发送。
`send_msg` 只接受明确且唯一的目标，归一到上述发送路径；合并转发的
`send_private_forward_msg`、`send_group_forward_msg`、`send_forward_msg` 尚无同等回执合同，
当前在发送前明确拒绝，不通过原始动作绕过。其他管理动作保留原有权限与行为。

发送前先持久化执行意图。只有严格有效的网关消息 ID 才确认成功，通常成功回执和账本在同一
事务提交；没有真实平台确认的超时、无效回执或取消保留 `unknown` / `uncertain=true`，不自动重试，
仅该次调用保留未知事实，后续独立显式发送继续执行。已提交成功的回执不会因后续唤醒通知失败而降级。
Social与SDK OneBot两条路径收到真实确认后，即使并发遗忘已不允许追加正文，也沿原operation结算
无正文成功事实，`event_id`可为空；不复活已删除的资料，也不重新发送。

每个显式发送调用使用独立序号，相同正文主动调用两次仍发送两次，不按正文去重。
工具调用使用原执行/调用 ID；归档的旧自动化插件步骤保留原 run/step ID，新的定时执行已退出步骤解释器。同一可信身份按原顺序
重放只读取回执，参数或来源冲突明确拒绝。身份不因批准版本变化而更新，权限仍每次核验。
其他没有持久 callback ID 的入口仅有本次 Host 回调的唯一身份，重新调用属于新调用，
**不承诺任意插件回调的跨重启 exactly-once**；插件不得据 `unknown` 自行重跑整个回调。
没有真实来源、canonical 会话或持久账本时在网络发送前拒绝，不补造入站消息。
这不是自动最终回复，也不增加插件发送权限或新的 SDK 参数。

SDK图片重放先核原callback/call、参数和不可变snapshot来源，再读取原Social结果，不重新读取已到期或
已清理的插件媒体。新媒体发送仍核当前媒体资格。分段发送直接使用Social完整聚合结果及各段真实状态，
不以父记录的`prepared`否定已发送部分。OneBot原调用按原参数和已提交传输事实对账，不先要求今日
`/ai new`水位、目标Binding或Presence仍可用；实际新派发仍在原claim边界复核当前来源和资格。

人物作用域按当前归并关系核权，原插件session、grant、publication和回执保留原ID及来源。
USER配置只读取当前根人物的有效行，原来源冲突行不作为unset后的fallback；plugin-global KV中的
不透明JSON不因人物合并而改写。原人物或根人物禁用、安装批准和grant撤销仍生效。

Memory V2 的写入仍统一经过 Host `MemoryFactService`。插件 update 创建修正版本，delete 只做显式
失效；插件不能直接访问 Repository、指定事实状态/authority、物理删除审计记录或绕过当前真实
调用作用域。冲突审计与管理员 merge/resolve 不属于 Plugin API 7.0。
Memory 记录中的 `user_id` / `subject_user_id` 和 `group_id` 同样是内部 Person / Space，
可在原授权范围继续作为查询参数；它们不是平台账号或群号。

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
- 在原配置的历史消息查询范围内保留完整正文，模型返回的可见正文完整保存并返回。
  Host 与 Repository 不再额外压到 500/10000 条；SDK 输入合同只在 SDK 验证，Host 仍拒绝空白输入。
  不按固定字符数截取消息后缀或输出；完整请求仍由原模型容量检查负责，初始历史超容量时明确失败。
- 独立会话只调用模型，不加载工具；`max_model_requests` 沿实际请求预算生效。
- `reset()` 只清空该会话历史；`close()` 关闭后不能继续运行。
- 会话 UUID 是不透明标识，不能用来跨插件或跨真实场景访问。

## Yuki 主 Agent 调用

`ctx.agent.run()` 是唯一的主 Agent 调用入口，使用固定提示词、工具声明和 short_state 编译。`context_profile` 显式选择 `none`、`current_user` 或 `current_group`；默认不加入人物或群资料。选择上下文还需要对应资料读取权限，聊天历史另外需要 `message.history.read`。

调用绑定原 ToolActor、canonical Conversation 和 Presence。消息来源保原事件编号；SELF、定时等无入站消息的来源保原执行编号或自主 run，不制造真人事件。执行能力沿插件实际领域批准；每次物理模型请求检查当前真实身份、原来源与权限，用户权限变更不被旧 USER 快照封顶。

主调用复用 `MainAgentBackend`，Host 在执行处复用批准权限：

| Manifest 批准权限 | 执行范围 |
|---|---|
| `agent.run` | 持久工作区、终端、安装、文件发布和环境服务 |
| `message.history.read` | 当前授权会话的近期记录、搜索、原文定位与网关补查 |
| `memory.person.read` | 记忆搜索与详情（人物可见范围） |
| `memory.group.read` | 记忆搜索与详情（群可见范围） |
| `web.search` | `web_search` |
| `web.read` | `read_webpage` |

`agent.run` 不再接受可选工具名名单，也不写入 `source.allowed_tools`。历史 source、boundary、hash 和回执保原件；恢复按原 Conversation、插件与执行 ID 找回原 Work，不因工具改名重建、重发或重置预算。不会维护旧名到 action/view 的授权表。普通用户仍只能读获准人物和会话；目录、查询和媒体输入不改变固定声明。

联网继续分别核 `web.search` 和 `web.read`；聊天记录与联系人沿 `message.history.read`，并保持原 Conversation 的读取范围；App 插件不能借 `read_personal_entry` 扩读其他私聊，普通 App Main 的本人跨入口显式读取不受影响。群成员沿 `group.members.read`。自动化使用现有 `automation.read/manage_self`；可信当前时间属于通用只读能力，不要求自动化批准。普通发送沿现有 `message.private.send/group.send`，附件沿 `message.media.send`；撤回自己消息复用当前会话的原发送批准并由原平台回执执行，再由原 Social 主体、目标、来源与回执执行；真实撤权后不派发新消息。每次记忆搜索与事实/证据读取按当前 Host 的 `memory.person.read/group.read` 批准投影调用范围；证据仍仅本人且需人物读取范围，入场旧快照不保留已撤销批准。并行读取各用本次执行副本，不改原来源或上下文证明。记忆持久修改沿现有 `memory.write`，`invalidate` 沿现有 SDK delete 的 `memory.delete`；后续仍核原 SELF/人物主体、事件和证据。宿主管理与其他真实业务授权仍在原领域执行处处理，工作环境批准不替代这些事实。独立 PluginSession 保持无工具的连续模型会话。

主 Agent 的 function 工具清单固定；原生工具按真实批准权限、显式配置和协议能力决定
是否提交。原生工具由 Provider 执行，不能声称本地执行围栏逐次拦截，也不能为统一声明
外观补授搜索权限。不同授权或协议不承诺相同 native 字节合同；DeepSeek 主连接不声明
原生搜索，权限控制不依赖 `tool_choice`。

## 当前会话音乐卡片

`ctx.onebot.send_music_card(provider=..., resource_id=...)` 使用 `onebot.send` 权限，将
OneBot `music` 消息段发送到触发插件的当前真实私聊或群聊。插件不能为这个方法传入 QQ 号或
群号，因此它不能跨会话改变目标；Host 会再次验证真实执行来源、provider、资源 ID 和
发送权限，成功后再写事件账本与脱敏审计。
已入账来源沿原事件作者核当前人物资格，不以发送者账号今日的绑定归属重建作者；
发送目标仍核实际 Binding、Presence 和批准权限，原投递回执按原调用键读取。

SELF 与定时 Main 使用原 actor 和场景，无真人事件时不伪造 Inbound；来源、原调用 ID 与
发送回执继续由 Host 持有，媒体和音乐卡不另有来源菜单。

当前 provider 支持 `qq`、`netease`（发送时规范化为 `163`）、`kugou`、`kuwo` 和 `migu`。
如果资源来自网页或其他外部数据，插件应先做结构校验和重名消歧，不得把自定义 URL 当成
资源 ID。任意 OneBot action 仍必须走权限更高的 `call_mutating_action`，不能借音乐卡片 Facade
绕过。

独立长期故事、跑团或游戏状态使用 `agent_sessions`；不要把大量连续历史塞进一次 `agent.run()`。

## 持续调用的状态

Runtime 升级后，`agent.run` 返回 `state`、`work_id`、`pending`。
每次调用均返回 `PluginResult`；调用方检查状态与结果，不把等待句柄当作生成正文。
Host 等待约 5 秒后可返回持久任务句柄，生成由 Host 继续持有。
`await ctx.agent.result(work_id)` 可在原回调退出后查询本插件的任务；检查批准版本、权限和 generation。
同一合法来源用同样参数读取原工作和已存结果，不再次执行；不同实际来源使用不同执行键。分段、恢复和重复查询不重置预算。
独立调用隔离原 invocation/Work 的协议上下文；同会话已占用的租约仍由现有执行层报告冲突。新的后台目标通过通知 API 接纳。已经接纳的 SDK 工作由 Host 调度器恢复；
普通 `agent_sessions` 保持独立。详见 [主入口执行合同](../architecture/main-agent-runtime.md)。

已接纳 Work 的子目标沿原任务树派生，继承原调用者、批准和来源。SDK 与后台通知子目标由原 Host 入口继续；父 Work 或原 Job 已结束不妨碍尚保留的子目标执行，也不重开它们。结果与查询不提升原工具授权或重置预算。
