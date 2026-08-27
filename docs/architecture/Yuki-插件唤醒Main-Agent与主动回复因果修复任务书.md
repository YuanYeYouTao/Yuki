# Yuki 插件唤醒 Main Agent 与主动回复因果修复任务书

## 1. 状态与目标

本文档定义 Yuki 3.8.1 之后的插件主动事件修复工作。本轮不实施“统一所有事件类型的新架构”，继续保留现有 canonical Conversation、`external_event`、插件可靠队列与主聊天事件账本。

本轮只修复以下两个错误：

1. 插件唤醒当前走特殊、缩水、tool-free/read-only 的 Agent 路径，Prompt、Context 与 Tools 均不同于正常 Main Agent。
2. Yuki 由插件事件触发的主动回复没有保存稳定的触发因果，后续容易被理解成对上一位群友的回复。

目标流程：

```text
external_event 独立落账
        ↓
可靠 WakeupRequest
        ↓
加载主 Conversation 的稳定完整快照
        ↓
Normal Main Agent
        ↓
仅在当前轮尾部追加临时 external_event 提醒
        ↓
Yuki 回复或保持沉默
        ↓
可靠 Outbox 发送
        ↓
回复以 caused_by_event_id 指向 external_event
```

## 2. 首要硬性要求：稳定的主 Conversation 上下文

稳定上下文是本任务的硬性要求，不是优化项，也不能以实现复杂为由降级。

### 2.1 “稳定上下文”的定义

插件唤醒必须加载与同一 Conversation 普通聊天轮次相同的：

- canonical Conversation ID 与 generation；
- primary alias 与当前 transport alias；
- 有效 Rollup、coverage 和 revision；
- 普通 Main Agent 使用的 canonical raw history window；
- 相同的 Context 字符和事件预算；
- 相同的 SELF、Person、Person-Group、Group Memory 检索规则中适用于当前 target 的部分；
- 相同的插件 Prompt fragments；
- 相同的运行时配置、模型 profile、reasoning 配置和 Web 路由；
- 相同的 Main Agent 工具内核与 capability discovery；
- 相同的 PromptCompiler、AgentRunner、输出清洗和效果围栏。

这里的“完整”是指普通 Main Agent 在相同 Conversation snapshot 下经过正常 Rollup 与预算裁剪后能够看到的完整上下文，不是绕开正常上下文预算加载无限历史。

### 2.2 明确禁止

禁止通过以下方式实现插件回复：

- 单独构建只包含 external event 和最近几条消息的短上下文；
- 创建独立的 background/external Prompt history；
- 使用单独的 Context 上限、history limit 或简化 Memory 检索；
- 只加载 SELF/GROUP 的缩水上下文并宣称等同 Main Agent；
- 创建插件专属 system/developer Prompt；
- 创建插件专属 tool-free/read-only Agent；
- 使用不同的工具 schema，再通过运行时错误伪装成普通工具集；
- 使用 `max_model_requests=min(2, ...)` 等插件专属模型循环限制；
- 把插件 grant creator、SUPERUSER 或任意配置账号伪造成当前说话人；
- 创建虚假 `InboundMessage`、Person、chat message 或 profile 以复用旧接口；
- 将 external payload 全量复制进 Prompt；
- 把 WakeupRequest 本身写入聊天历史。

若实现无法证明插件轮次与普通 Main Agent 使用同一稳定 Conversation snapshot，则验收失败。

## 3. 保留的现有边界

- `external_event` 继续独立落账，保持 `event_kind=external_event`、`direction=external`、`origin=plugin_background`。
- external event 不伪装成真人消息，不创建 Person。
- external event 不直接进入 Person Memory、Relationship 或 Automation。
- 普通 Agent 继续通过 recent-event digest 和 Rollup 感知外部事件。
- GitHub 等插件继续自行负责聚合；Host 不实现 GitHub 专属聚合规则。
- 插件队列、request identity、幂等、lease、重试、路由和崩溃恢复全部保留。
- `PLUGIN_BACKGROUND` 继续作为审计、并发、权限和因果来源，但不得决定 Agent、Prompt、模型配置或静态工具形状。

## 4. 触发提醒合同

### 4.1 WakeupRequest

WakeupRequest 是持久调度数据，不是会话消息。继续使用现有可靠 background job 能力，至少保存：

- `source_event_id`；
- `canonical_conversation_id`；
- Conversation generation；
- canonical target；
- 有界 `agent_intent`；
- status、lease、attempt、next-attempt；
- request identity、审计和幂等信息。

WakeupRequest 不得：

- 新增 `chat_events` 行；
- 进入 Prompt；
- 进入 Rollup；
- 进入 Memory；
- 携带 event 正文副本；
- 改变 Conversation generation；
- 改变路由 generation。

### 4.2 当前轮临时提醒

Worker 完成 canonical、generation、路由和 freshness 验证后，在内存中从 source event 构造一次当前轮提醒：

```text
[external_event_trigger]
event_id=123
source=github
event_type=pull_request
occurred_at=...
content_trust=external_untrusted
summary=...
agent_intent=如有必要，简短点评
```

要求：

- 只能位于模型输入最后一条 current-turn 中；
- 不能插入 system/developer instructions；
- 当前 source event 只能出现一次，必须从 recent digest 排除；
- `agent_intent` 最长 1000 字符，仅为不可信建议；
- 不包含 job ID、attempt、lease、route、Presence、token 或完整 payload；
- 字段顺序和序列化格式必须确定性固定；
- 使用事件落账时的 summary 与 occurred-at，不在 retry 时重新生成内容。

## 5. Yuki 主动回复合同

Yuki 实际发送成功的插件回复是她真实说过的话，应继续作为普通对话中的 assistant/Yuki 消息存在：

- `event_kind=message`；
- `direction=outbound`；
- `author_kind=yuki`；
- `origin=plugin_background`；
- `caused_by_event_id` 指向来源 external event；
- 使用真实 Provider/Presence 和平台发送回执；
- 进入普通 Main Agent raw history；
- 不要求伪造对应的 user/chat message。

没有前置 user message 本身不是错误。真正需要修复的是主动回复缺少因果，导致模型可能把它理解成对上一位群友的回复。

## 6. 数据模型与迁移

新增 Alembic `0050`：

```text
chat_events.caused_by_event_id INTEGER NULL
    REFERENCES chat_events(id) ON DELETE RESTRICT
```

新增索引：

```text
ix_chat_events_caused_by_event_id
```

### 6.1 应用层写入约束

新产生的 plugin-origin outbound 必须满足：

- cause 已存在；
- cause 与 outbound 属于同一 canonical Conversation；
- cause ID 早于 outbound ID；
- cause 为 keeper；
- cause 满足 `event_kind=external_event`、`direction=external`、`origin=plugin_background`；
- 重试不能生成第二条相同平台回执的 outbound event。

普通真人消息、普通 Yuki 回复和现有其他事件保持 nullable，不强迫建立因果。

### 6.2 历史数据

- 使用现有 outbox 的 `source_event_id + platform_message_id` 对能够唯一证明的旧插件发送结果进行安全回填。
- 无法唯一证明的旧行保持 NULL，不猜测、不按时间邻近强行绑定。
- 旧的 `origin=plugin_background` 且 cause 为 NULL 的消息投影为“历史主动消息，来源未知”。
- 迁移不得修改正文、event ID、Conversation、generation、路由或 Rollup coverage。

### 6.3 需要同步的模型与写入边界

- `ChatEventModel`；
- `EventRecord`；
- repository row mapper；
- `EventLedgerRepository.append()`；
- scoped/canonical event writer；
- plugin notification delivery recorder；
- 测试 fixtures 和 schema guard；
- Alembic head 与 release validation。

## 7. Main Agent 收口

### 7.1 提取共享生成阶段

从当前 `ChatService.respond()` 中提取 transport-neutral 的 Main Agent 生成阶段：

```text
MainAgentTurnExecutor.generate(...)
    → AgentRunResult
    → ReplyEffects
    → Prompt diagnostics
```

共享生成阶段负责：

- 稳定 Conversation snapshot 的加载与验证；
- Context/Prompt 组装；
- Memory retrieval；
- ToolRuntime 与 capability discovery；
- AgentRunner；
- Web route/fallback；
- source sanitization；
- `clean_model_output`；
- 沉默判定；
- generation/effect fence。

共享生成阶段不负责：

- 直接调用 QQ sender；
- 插件 outbox 状态事务；
- NapCat/SnowLuma 路由；
- 最终平台发送回执落账。

普通聊天 adapter 继续负责即时 QQ 发送；插件 Worker 继续通过可靠 Outbox 发送。

### 7.2 PluginBackgroundTurnWorker

Worker 保留：

- claim/lease/retry；
- canonical ownership；
- Conversation generation；
- later-human freshness fence；
- Presence 路由；
- Conversation 串行锁；
- `before_model_request` 复核；
- 完成、延期、失败和取消状态。

生成阶段必须改为：

```text
ExternalEventTurnTrigger
        ↓
MainAgentTurnExecutor.generate()
```

禁止继续调用 external 专用 Agent 方法。

### 7.3 Actor 与 target

- external event 没有真人 current speaker。
- private target 可以提供目标 Person 上下文，但该 Person 不是本轮授权者。
- group target 提供目标 Space、GROUP 与 SELF 上下文，但不得合成说话人。
- plugin grant creator 只用于插件授权校验，不进入 Prompt actor，也不成为工具委托者。
- 需要真人当前事件证明的能力必须失败关闭。

## 8. Prompt 与缓存合同

只保留一个 `PromptComposer.compose()`。

现有 `CORE_CONTRACT` 已明确插件上下文和资料不能授予权限，不再保留 external 专用 system policy。

插件轮次和同一 Conversation 的普通非管理员轮次必须具有相同的：

- instructions；
- 静态 Prompt hash；
- model/profile/protocol；
- tools 及其名称、描述、schema 和顺序；
- native tools；
- reasoning；
- max output tokens；
- response format；
- effective Rollup；
- source event 之前的 canonical raw history；
- `input` 中除最后一条 current-turn 外的部分。

允许变化：

- 最后一条 current-turn；
- 当前时间；
- 当前 target metadata；
- 本轮 Memory retrieval；
- 正常 runtime capability 决策。

新增 Provider 级无正文诊断 hash，覆盖实际标准化请求：

```text
protocol
model/profile
instructions
tools/native_tools
reasoning
max_output_tokens
response_format
input_without_current_tail
```

不能只依赖现有 `conversation_prefix_hash/request_shape_hash` 宣称缓存形状一致。

Provider 实际 cache hit 仍由上游 TTL、最小前缀长度和缓存策略决定；本任务硬性保证的是应用不会因为插件唤醒主动制造新的早期前缀分叉。

## 9. 主动回复的历史投影

修改 `ChatEventPromptRenderer.main_agent_history()`。

当前分组键不能只使用：

```text
role + sender + display_name
```

应至少加入：

```text
origin_class + caused_by_event_id
```

行为：

- 普通相邻 Yuki 消息仍可正常合并；
- plugin-background 主动消息不能与上一条普通 Yuki 回复合并；
- 不同 external event 触发的主动回复不能互相合并；
- 同一 external event 的多个可靠发送 part 可以作为同一主动 episode 投影。

模型历史显示类似：

```text
[Yuki主动消息｜由外部事件 #123 触发｜source=github｜type=pull_request]
PR #58 已经合并了喵。
```

该标记：

- 只进入模型历史和 Rollup source projection；
- 不修改平台实际发送正文；
- 不暴露 payload、Secret 或内部任务状态；
- 通过批量加载 cause events 生成，禁止 N+1 查询；
- cause 已被 Rollup 覆盖时仍能通过持久 ID 和批量查询恢复来源类型。

Rollup source projection 同样保留主动消息的因果标签。事件被覆盖后，Yuki 仍应知道这是一次外部事件触发的主动表达。

## 10. Rollup 与任务围栏

当前代码在 source event 已被 Rollup 覆盖后取消任务，属于事后失败，不能满足稳定上下文要求。

增加 coverage hold：

```text
max_rollup_coverage
<
同 Conversation 最早 pending/processing WakeupRequest.source_event_id
```

规则：

- external event 与 WakeupRequest 在同一事务创建；
- pending/processing 状态阻止 coverage 越过 source event；
- retry 等待期间继续持有；
- completed、silent、cancelled、abandoned、failed-terminal 自动解除；
- 后来出现真人消息导致任务取消后立即解除；
- 不修改 Conversation generation；
- 不修改普通聊天 protected-tail 尺子；
- Rollup 层通过 persistence query port 获取 hold，禁止依赖插件业务服务。

## 11. 工具与权限

- 删除 background turn 的全局 `tools_closed/read_only` 特例。
- 插件唤醒使用正常 Main Agent 工具内核和 `request_tools`。
- capability 是否可加载由真实 target、authority 和 runtime scene 决定。
- 没有真人事件证明时，管理员、关系、人物 mutation 和 `current_speaker` 能力不得授权。
- 允许正常授权的 Web、Memory read、history read 等能力工作。
- 禁止通过 `align_conversation_prefix_tools` 向模型展示看似可用、实际统一拒绝的工具。
- `decline_reply` 或等价正常沉默机制继续有效；沉默成功完成 job，但不生成 agent-reply outbox。

## 12. 必须删除的错误代码

以下代码必须彻底删除，不能保留 deprecated wrapper：

- `PromptComposer.compose_external()`；
- `EXTERNAL_EVENT_HOST_POLICY`；
- `ContextAssembler.assemble_external()`；
- `ContextAssembler._external_history_identity()`；
- `ContextAssembler._require_uncovered_external_trigger()`；
- `ChatService.generate_external_reply()`；
- `ToolRuntime.align_conversation_prefix_tools`；
- `_ChatAgentBackend._prefix_policy_origin()`；
- 所有围绕 `align_conversation_prefix_tools` 的条件分支；
- “展示正常工具、执行时统一 tools_closed”的 background 特例；
- Worker 对 `generate_external_reply()` 的调用；
- external Agent 专属 `max_model_requests=min(2, ...)`；
- external Agent 专属 tool-free/read-only Prompt 说明；
- 冻结上述错误行为的测试和当前架构文档。

保留：

- `tools_closed` 本身，如果其他真实封闭场景仍使用；
- external event digest；
- external event Rollup source projection；
- plugin queue/outbox/idempotency；
- generation、route 与 freshness fence；
- `ExternalEventTurnTrigger`；
- `PLUGIN_BACKGROUND` origin。

删除验收：

```text
rg "compose_external|assemble_external|generate_external_reply|align_conversation_prefix_tools|EXTERNAL_EVENT_HOST_POLICY" src
```

生产源码必须零匹配。

## 13. 测试重建

### 13.1 稳定上下文硬门

- 相同 Conversation snapshot 下，普通轮次与插件轮次加载相同 Rollup、coverage、revision 和 raw history。
- 插件轮次不得使用更小的 history/context/memory/model-request 上限。
- 普通 history 中任意早期 marker 在插件轮次仍可见，除非同样被正常 Rollup 覆盖。
- 禁止只使用 source event 与少量最近事件构造测试夹具来规避完整上下文断言。
- 普通与插件轮次的 Provider payload 除最后 current-turn 外保持一致。
- retry 在 generation、Rollup 和 history 未变化时生成相同稳定前缀。

### 13.2 Prompt 与缓存

- instructions 完全相同；
- 工具名称、描述、schema 和顺序相同；
- reasoning、model、profile 与 max output 一致；
- external event 只在最后一条 input 出现一次；
- current source 不出现在 recent digest；
- WakeupRequest ID、attempt、lease、route、Presence 不进入 Prompt；
- `normal A → plugin wake → normal B` 保留共同主前缀；
- 插件轮次不产生额外 system message。

### 13.3 因果与历史

- 插件回复落账包含正确 `caused_by_event_id`；
- cause 与 reply 属于同一 Conversation；
- 插件主动回复不与上一条普通 Yuki 消息合并；
- 两个不同 external event 的回复不合并；
- 下一轮 Agent 能区分“主动消息”和“回复上一位群友”；
- 用户引用插件主动消息时能够解析来源；
- legacy NULL cause 不伪造关联；
- external event 本身仍不进入 ordinary raw history。

### 13.4 安全

- external event 不创建 Person；
- grant creator 不成为 current speaker；
- 不产生 superuser authority；
- `current_speaker` 人物记忆 mutation 在无真人事件时失败关闭；
- 外部正文不能获得 admin/config/relationship 权限；
- Web、Memory read 等正常可授权能力不因 plugin origin 被整体关闭；
- 模型选择沉默时不创建 agent-reply outbox。

### 13.5 可靠性

- event/job 原子创建；
- crash after generation 不产生重复回复；
- outbox retry 复用同一生成结果；
- later human message 取消过期 wakeup；
- `/ai new` generation 改变取消旧任务；
- route paused 不发送；
- Presence 切换不改变 Conversation 或 cause；
- pending coverage hold 阻止 Rollup 抢先覆盖；
- terminal 状态解除 hold；
- 同一个 source event 最多一个 Agent wakeup 和一个 agent-reply part。

### 13.6 数据库

- fresh `0048 → 0049 → 0050`；
- populated `0049 → 0050`；
- 可证明旧数据的安全回填；
- FK、index、ORM 和 schema guard 一致；
- migration rollback 或快照恢复演练；
- `PRAGMA foreign_key_check` 全绿。

## 14. Commit 顺序

### C1 — `feat(ledger): record proactive reply causality`

- 新增 0050；
- 增加 `caused_by_event_id`；
- 扩展 EventRecord、mapper、writer；
- Plugin outbox 落账时写入 cause；
- 安全回填可证明的旧数据；
- 不改变 Prompt 或 Agent 行为。

验收：migration、FK、幂等和跨 Conversation 拒绝测试。

### C2 — `refactor(agent): route plugin wakeups through the main agent`

- 提取共享 Main Agent 生成阶段；
- Worker 使用 `ExternalEventTurnTrigger`；
- 复用正常 composer、runner、tools、web 和清洗；
- 删除 external 专用 Agent 路径和伪工具对齐；
- 保持插件可靠队列与 Outbox。

验收：正常聊天黄金测试不变；完整上下文硬门通过；插件轮次与普通轮次 Provider shape 对拍。

### C3 — `fix(context): preserve proactive message causality`

- 主动回复加入稳定的模型历史因果标签；
- 分组键加入 origin/cause；
- recent digest 排除当前 source；
- Rollup source 保留主动因果；
- 增加 pending wakeup coverage hold。

验收：无人物串位、无事件重复、无伪前置输入、Rollup 不抢跑。

### C4 — `test(docs): freeze stable plugin wakeup behavior`

- 删除旧 external special-agent 测试；
- 新建参数化行为矩阵；
- 增加 Provider 级缓存形状诊断；
- 更新当前架构文档；
- 在 3.8.1 历史文档注明旧路径已被后续修复，不篡改历史发行事实；
- 保留本任务书作为实施合同。

验收：完整质量门和源码零匹配门。

## 15. 最终质量门

- `ruff format --check`；
- `ruff check`；
- `mypy src`；
- 完整 pytest；
- migration fresh/populated；
- SQLite quick/FK/FTS/trigger；
- memory quality validation；
- release smoke；
- `git diff --check`；
- 无 `.env`、token、QQ 号、Cookie 或 external payload 泄露；
- 无新增 HTTP 管理 API；
- 无 GitHub 插件专属 Main Agent 分支；
- 无 external 专用 Main Agent；
- 无独立短上下文；
- 无虚假 Person/current speaker；
- 无 `align_conversation_prefix_tools`。

## 16. 非目标

- 不统一所有 future event；
- 不重命名或重建 `chat_events`；
- 不把 external event 放入普通 raw history；
- 不修改 GitHub 聚合策略；
- 不重做 Rollup 总体架构；
- 不承诺 Provider 一定返回某个缓存命中率；
- 不修改 NapCat/SnowLuma；
- 不实现 WebUI；
- 本任务书不授权推送、部署或发布。

## 17. 最终架构判定

本轮修复必须遵守以下结论：

> 插件触发提醒是一次不进入普通历史的临时动态输入；Yuki 的回复是真实主动发言。回复无需伪造前置用户消息，但必须永久保存它为何产生。插件唤醒时必须加载普通 Main Agent 在同一 canonical Conversation 中使用的完整稳定上下文，禁止以独立短上下文、特殊 Agent 或伪工具对齐替代。旧的 external 专用生成路径属于错误实现，必须删除。
