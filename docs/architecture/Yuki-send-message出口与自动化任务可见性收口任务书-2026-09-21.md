# Yuki `send_message` 出口与自动化任务可见性收口任务书

> 状态：已按本任务书实施；提交与生产部署状态以交付记录为准
>
> 基线：`codex/explicit-send-message` / `16e839c`（`refactor: make outbound delivery explicit`）
>
> 核查时间：2026-09-21，Asia/Shanghai
>
> 范围：只处理显式消息出口与自动化任务认知/可见性问题

## 1. 结论

当前需要完成两项 P0 收口：

1. `send_message` 必须在任何持久回执、分条计划、媒体准备和网关发送之前，经过一次统一的模型输出规范化。现在旧最终回复路径会调用 `sanitize_model_output()`，新 `send_message` 路径却直接使用工具参数，因此已经出现 `#62051>`、`#62052>` 这类内部历史事件前缀被原样发到 QQ 的生产事件。
2. 自动化任务的“读取可见性”必须与“修改权限”分离。Yuki 应能查看全局任务简表，并在每个群聊轮次中收到该群当前 active 任务的极简快照；创建、更新、暂停、恢复、取消和立即运行仍只允许任务所有者。当前 `automation_list` 只查询当前发言人名下任务，已经导致 Yuki 把“我无权管理”误判成“任务不存在”，进而在无关消息轮次重复创建日记任务。

这两项修改都不需要数据库迁移。需要新增查询、投影和提示词装配，但不应修改 `automations` 表结构，也不应引入语义去重或唯一性约束。

提示词方面有两个不可妥协的条件：任务快照必须是 `TURN` 级动态贡献，只能附加到当前 user input；同时必须尽可能短，只展示一次范围说明和每项的 `ID + 任务内容 + 下次时间`。不得进入稳定 system 前缀，不得改变固定工具声明，不得因为任务列表变化击穿 Provider 前缀缓存。

## 2. 已确认事实

### 2.1 `send_message` 出口前缀泄漏

生产账本中已观察到以下真实出站正文：

| 事件 ID | origin | 正文 |
| --- | --- | --- |
| 62051 | `social_tool` | `#62051>这么一算，裁判比我还先欠到负分` |
| 62052 | `social_tool` | `#62052>那刻度校完，我可就不只是开张，是直接反超了喵` |

现有代码并非不知道这种格式：

- `src/qq_ai_bot/services/renderer.py` 的 `_MAIN_AGENT_EVENT_PREFIX` 已识别 `#事件号>` 和带引用描述的历史前缀。
- `sanitize_model_output()` 已负责控制字符归一化、内部历史标记清理和长度约束。
- `tests/unit/test_renderer.py` 已覆盖 `#37483`、`#37483>`、`#37483|回复:...>`，并验证模型主动给出的来源和链接不会被删改。

缺口发生在新链路：

```text
模型工具参数
  -> SocialMessage.model_validate(raw args)
  -> OutboundMessageSplitter.render(raw text)
  -> receipt.prepare(payload=raw args)
  -> 媒体/语音准备(raw text)
  -> OneBot params(raw text)
  -> EventLedger(raw content)
```

`ChatService` 对最终正文的旧净化还存在，但最终正文已经不自动发送；真正可见的 `send_message` 工具参数没有经过该边界。

### 2.2 自动化任务重复创建不是数据库重试

核查快照中，与同一群、同一天日记文件相关的任务至少有：

| ID | 创建主体 | 状态 | 摘要 |
| --- | --- | --- | --- |
| 85 | 远野 | active | 每小时写日记到 22 点，已经执行过 |
| 86 | Winter | cancelled | 后续被替换的任务 |
| 87 | Winter | active | 每小时补记到 22 点并发群，已经执行过 |
| 88 | Diana | active | 每小时日记 |
| 89 | Diana | active | 22:00 日记发送 |
| 90 | 查无此人 | active | 今晚十点发日记 |

这些任务的 `creation_source_key` 不同，因此不是同一工具调用重试，也不是数据库幂等失效。它们来自不同发言主体、不同轮次的独立创建。

其中，Yuki 曾明确声称“85、87 没真正挂上，只有 90”，但 85、87 实际均处于 active 且已有运行记录；同时，ID 61 的早八点叫醒任务也真实存在并持续运行。这证明问题不在调度器是否持久化，而在模型所看到的任务目录被当前发言人的所有权过滤了。

### 2.3 当前代码把读权限和管理权限绑死

当前模型工具合同和执行路径如下：

- `automation_list` 描述为“只列出当前执行主体”的任务。
- `AutomationToolService.execute()` 调用 `AutomationService.list_current(actor.user_id)`。
- Service 将 QQ 用户解析成 `canonical_creator_person_id`。
- Repository 使用 `AutomationModel.canonical_creator_person_id == creator_person_id` 过滤。
- `automation_get` 也直接调用 `require_owned()`。
- `find_equivalent_task()` 只在当前创建者名下搜索。
- `automation_list_history` 同样只看当前创建者。

因此，群成员 A 创建的群任务，对群成员 B 的主 Agent 轮次完全不可见。B 看到的空列表只是“B 名下为空”，却很容易被模型解释成“这个群没有任务”。

### 2.4 数据库结构已足够

`automations` 已包含：

- `canonical_creator_person_id`
- `canonical_target_person_id`
- `canonical_target_space_id`
- `status`、`next_run_at`、`last_run_at`
- `schedule_json`、`script_json`
- `creation_source_key`

现有索引已经覆盖状态/下次运行时间、创建者、目标 person、目标 space。实现全局目录和当前群快照只需要查询与投影，不需要新增列或迁移。

## 3. 根因

### 3.1 消息问题

根因不是正则缺失，而是出口边界分裂：

- 旧的自动最终回复路径拥有净化器。
- 新的显式 `send_message` 成为唯一用户可见出口后，没有接入同一净化器。
- 分条 manifest、幂等 payload、语音 `response_text`、表情准备、附件 caption、OneBot segments 和账本 content 各自继续消费原始参数。

这会形成更深的回执一致性风险：如果只在网关前临时清理，回执 hash/重放依据仍是原文，而实际发出的内容是净化后文本；若重入时走另一条分支，可能产生 payload 不一致、重复投递或账本与 QQ 正文不一致。

### 3.2 任务问题

根因是四层信息缺失叠加：

1. `automation_list` 的查询范围是当前 actor，不是 Yuki 的任务目录。
2. 群聊轮次没有权威的“当前群任务快照”。
3. 工具错误只表达“不是当前用户的任务”，没有稳定地区分 `not_found` 与 `exists_but_not_owned`。
4. 尽管静态合同已经写明“讨论、列举、当前查询和历史旧承诺不构成新建请求”，模型仍可能把历史中的任务讨论当成当前创建意图，尤其在它又看不到现存任务时。

因此，单纯增加提示词一句“不要重复创建”不够；单纯做语义去重也会把权限、时间或交付范围不同的合法任务错误合并。

## 4. 设计原则

### 4.1 单一出站规范化边界

所有模型产生的可见文本在进入副作用系统前只规范化一次，之后所有消费者共享同一份 canonical payload。

该规则只作用于今后执行的出站发送。已经写入事件账本、历史消息、Rollup、记忆或已经发到 QQ 的旧内容保持原样，不做回填、迁移、批量清洗或历史重写；旧记录是事实与审计证据。模型若从旧历史中再次复制出 `#62052>` 一类前缀，只在它准备通过 `send_message` 重新发送时清理本次新出口。

```text
raw tool args
  -> schema validation
  -> normalize_model_owned_outbound(args)
  -> canonical args
       |- sequence manifest / receipt payload
       |- splitter
       |- text/image/file/voice preparation
       |- OneBot params
       |- work delivery intent
       `- event ledger
```

禁止在网关 adapter、账本 writer 或某个媒体分支里各自补正则。这些位置太晚，而且会让回执、重放和实际消息不一致。

### 4.2 读取可见性与修改权限分离

“Yuki 看得到任务”不等于“当前发言人能修改任务”。

- 读取：主 Agent 可查询全局任务简表；当前群轮次自动预取该群 active 任务。
- 修改：继续由后端按 `canonical_creator_person_id`、真实 actor、任务版本和目标范围核验。
- 展示：提示词快照不重复人物、状态、权限和范围字段；模型不得从“能看到”推导“能修改”。

### 4.3 一个查询语义、两种消费方式

自动化目录必须有一个共享的 read model/projector：

- `automation_list` 按需查询全局目录。
- 提示词装配查询当前会话相关的有界子集。

两者必须复用相同 Repository/Service 查询和“哪些任务算存在”的定义。工具结果可以按需返回结构化详情；提示词必须使用单独的极简投影，不能把完整 DTO 原样塞进每轮上下文。

### 4.4 前缀缓存优先

实际任务列表是动态状态，只允许进入当前输入：

- `PromptStability.TURN`
- `PromptChannel.RUNTIME`
- `PromptTrust.TRUSTED`
- 由 `PromptCompiler._with_dynamic_prefix()` 附加到最后一条 user message

禁止放入：

- `CORE_CONTRACT` 中的动态正文
- persona/system prompt
- 固定工具 schema/description 的运行时变体
- history、rollup、memory 或 `short_state`
- Responses continuation 之前的既有消息

静态合同可以增加稳定的解释规则，但该文本只在版本发布时变化一次，不得随任务状态变化。

## 5. 工作包 A：统一 `send_message` 出口

### A1. 建立 canonical 参数

在 `SocialService` 的 `send_message` 入口增加单一规范化函数，例如：

```python
normalize_model_owned_message(
    message: SocialMessage,
    *,
    max_characters: int,
) -> SocialMessage
```

要求：

- 复用 `sanitize_model_output()` 的既有语义，不复制正则。
- 仅规范化模型拥有的文本字段。
- `mentions` 保持结构化对象，不转成纯文本，不参与前缀正则。
- 规范化完成后重新生成 canonical args，后续不再持有 raw args。
- 普通 Markdown 标题、行内 `#62052`、URL、来源和引用文字不得被误删。

### A2. 规范化必须早于分条与回执

调整 `_send_message_sequence()`：

- 先校验和规范化完整 `SocialMessage`。
- 再运行 `OutboundMessageSplitter.render(canonical_text)`。
- sequence manifest 的 `payload.original` 必须是 canonical args。
- 每个 part receipt 的 payload、work intent 和执行参数都必须来自 canonical args。
- 第一条保留 reply/mentions，后续分条继续按现有逻辑移除；主动多次调用 `send_message` 仍互相独立。

### A3. 覆盖所有媒体分支

同一 canonical text 必须用于：

- 纯文本 OneBot segment
- 图片附带文本
- 文件 caption 及其独立回执
- 语音 `response_text` 和最终 `spoken_text` fallback
- 表情准备的 `response_text`
- `content` 与 `ledger_segments`
- delivery intent 中的 arguments

不能只修纯文本分支。

### A4. 空内容规则

- 纯文本经清理后为空：在任何 receipt prepare、claim 或网关调用前返回稳定错误，例如 `empty_message_after_sanitization`。
- 只有媒体、附件、表情或合法结构化 mention 的消息：按现有 schema 与网关规则判定，不因文本为空误杀。
- 不允许先创建 PREPARED receipt 再发现正文为空。

### A5. 回执与重放一致性

canonical args 必须成为以下内容的唯一来源：

- receipt payload/hash
- split manifest
- child call payload
- work reservation arguments
- ledger content/segments
- 网关实际参数

同一个 `turn_id + call_id` 重放时，必须读取相同 canonical 内容，不能再次发送，也不能因为 raw/canonical 差异报冲突。

### A6. 引用失败语义

实施前 `ConfirmedQuoteRejection` 类型仍存在，但新社交路径没有使用它；所有网关异常统一进入 uncertain/failed 流程。

实施已收口这一遗留：

- 只有网关明确证明“带引用的消息未被接受”时，才允许去掉 reply segment 后重试一次。
- 超时、断连、取消、无回执或任何可能已投递的情况保持 `uncertain`，禁止重试。
- 当前网关层无法可靠产出“确认未接纳”的分类，因此已删除死类型；不得靠错误字符串猜测。

这是兼容性修复，不得恢复旧回复队列或自动最终回复。

## 6. 工作包 B：自动化全局安全目录

### B1. 新建只读查询模型

在 automation 层新增面向模型的紧凑任务目录。默认 active 查询只返回：

```text
automation_id
task
next_run_at_local
```

其中 `task` 直接使用任务的简短展示名称（与 `/ai automation list` 的 `row.name` 同源），不再让模型或另一套摘要器重写；建议硬限制在 96 字以内。任务内容通常已经包含相关人物、目标和动作，不再额外重复 `creator_display_name`、`target_display_name` 等人物字段。时区、默认状态和查询范围放在结果顶层写一次，不在每条任务重复。

建议默认返回形状：

```json
{
  "timezone": "Asia/Shanghai",
  "default_status": "active",
  "tasks": [
    {
      "automation_id": 85,
      "task": "每小时补充远野的日记，今晚十点发送到当前群",
      "next_run_at_local": "2026-09-21 21:55"
    }
  ],
  "next_cursor": null
}
```

只有调用方显式查询 paused、terminal 或单项详情时，才返回该查询确实需要的状态、最后运行时间等额外字段。不要为“以后可能有用”把完整 schedule、运行计数、人物、工作区资源和权限标记塞进默认列表。

不得暴露：

- `authority_snapshot_json`
- 完整 capability 授权快照
- 私有记忆配置
- 内部路由、Presence、Binding 或账号凭据
- 完整隐藏脚本和与展示无关的内部参数

目标是让 Yuki 低成本判断“任务是否存在、内容是什么、下次何时运行”。谁能管理由执行工具时的后端权限判断，不需要在每轮列表中重复说明。

### B2. 改造现有 `automation_list`

不增加过渡性 `automation_list_all`；直接把现有模型工具变成 Yuki 的统一任务目录。

建议参数：

```text
status: current | terminal | all
target_scope: all | current_conversation | current_group | current_private
limit: 1..100
cursor: opaque string
match_task: optional TaskSpec
max_runs: optional integer
```

默认值：

- `status=active`
- `target_scope=all`
- 有界分页，不一次把全库塞进模型上下文

默认展示参考 `/ai automation list` 的单行格式，但进一步去掉每条重复的 `[active]`。按需调用
`automation_list` 时，每项另返回创建者的稳定 Person ID、外部账号 ID 和可用显示名；这些身份字段
只属于工具查询结果，不进入每轮自动注入的极简快照：

```text
当前任务（Asia/Shanghai，默认 active）：
[ID 85] 每小时补充远野的日记，今晚十点发送到当前群；下次：2026-09-21 21:55
```

`match_task` 必须在选定的可见目录范围内寻找结构化等价候选，而不是继续隐式限定当前 owner。它只返回候选，不自动合并、不阻止创建。

### B3. `automation_get` 的干净语义

建议让 `automation_get(id)` 对任何已存在任务返回安全摘要；写操作仍单独校验 owner。

若暂时不开放跨 owner 的单项读取，也必须返回结构化区分：

```json
{
  "error": "not_task_owner",
  "task_exists": true,
  "automation_id": 85
}
```

禁止把存在但无权管理伪装成 `not_found`。

### B4. 变更工具描述与权限目录

同步修改：

- `src/qq_ai_bot/automation/tools.py`
- `src/qq_ai_bot/admin/permission_catalog.py`
- `src/qq_ai_bot/capabilities/search_aliases.toml`（如索引描述需要）

读能力描述应是“查看 Yuki 自动化任务的安全摘要”；写能力继续明确“只能管理当前发送者自己的任务”。

`/ai automation ...` 管理命令和插件 SDK 的 `list_current_owner()` 可继续保持 owner-scoped，因为它们是明确的账户管理接口，不应为了模型目录改名义。必须在文档和测试中区分它们，不得误称为全局目录。

### B5. 保持写入权限不变

以下入口继续调用 owner 校验：

- `automation_update`
- `automation_pause`
- `automation_resume`
- `automation_cancel`
- `automation_run_now`
- 修改性 slash/admin 命令

前端工具 schema 是否公开不构成授权；执行处必须继续用真实 actor 核验。

### B6. 不做语义去重

本任务不增加：

- 基于自然语言标题或 goal 的唯一索引
- “同群同文件”强制单例
- 自动取消相似任务
- 模糊向量去重
- 创建前由后端猜测用户意图

保留现有 `creation_source_key` 对同一调用重放的幂等保护。是否复用已有任务由主 Agent 根据权威目录和当前明确请求决定。

## 7. 工作包 C：当前会话任务快照进入统一提示词

### C1. 装配位置

在 `ContextAssembler` 取得当前会话相关任务，并在 `AssembledContext` 增加有界字段，例如：

```python
visible_automations: tuple[VisibleAutomation, ...] = ()
automation_snapshot_scope: str = ""
```

`PromptComposer` 生成独立贡献。为减少动态 token，优先使用紧凑 `content`，不要序列化完整任务 DTO：

```python
PromptContribution(
    id="runtime.current_conversation_automations",
    channel=PromptChannel.RUNTIME,
    trust=PromptTrust.TRUSTED,
    priority=93,
    stability=PromptStability.TURN,
    content=compact_automation_snapshot,
    required=True,
)
```

当前 `PromptCompiler` 已把 TURN 贡献序列化后附加到最后一条 user message；实现应沿用该机制，不修改 system message 的组成。

### C2. 群聊快照语义

群聊的预取范围固定为：

```text
active tasks targeting this group
```

只预取 active 任务。paused、terminal 和全局详情由 `automation_list` 按需查询，不占用每轮上下文。即使为空也必须用一行极简文本区分：

- 查询成功且该群当前没有任务
- 查询失败/不可用
- 未加载

建议非空格式：

```text
当前群任务（默认 active）：
[ID 85] 每小时补充远野的日记，今晚十点发送到当前群；下次：2026-09-21 21:55
```

建议空格式：

```text
当前群没有 active 自动化任务。
```

建议查询失败格式：

```text
当前群任务状态不可用；需要时调用 automation_list 核实。
```

快照中不逐条写 `active`，不重复人物、目标类型、时区、owner、`manageable`、schedule JSON、运行次数或解释性元数据。任务内容本身已经包含人物时，不再额外突出人物。

快照必须有单独的低成本上限：按下次运行时间排序，最多放 8 项、正文最多 1,200 字符，以先到者为准。超出时只追加一行 `另有 N 项，调用 automation_list 查看。`；不能为了塞下完整列表扩大本轮上下文预算，也不能让模型先总结一遍任务。

私聊只预取当前私聊目标/当前 actor 相关的有界任务。全局完整目录由 `automation_list` 按需查询，不能每轮全部注入。

### C3. 覆盖所有 Main Agent 入口

至少覆盖：

- 普通私聊/群聊
- 主动群聊
- 插件外部事件唤醒
- automation `current_group` Agent 执行
- 其他共用 `ContextAssembler -> PromptComposer -> Main Agent` 的入口

无真实当前群的入口不得伪造群快照。自动化执行应使用任务自身已授权的 canonical target space，而不是创建假的 QQ inbound actor。

### C4. 稳定合同只增加解释规则

`CORE_CONTRACT` 可增加固定文本：

- `runtime.current_conversation_automations` 是当前群 active 任务的权威简表。
- 简表中出现任务只表示任务存在，不表示当前主体可以修改；写权限由工具后端核验。
- 当前消息必须明确请求创建/修改；历史承诺、他人讨论、状态确认和无关消息不得触发 mutation。
- 创建成功仍只以 `confirmation=persisted` 和真实 `automation_id` 为准。

静态合同不嵌入任何任务数据、ID、状态或数量。

### C5. 前缀缓存验收

必须以完整 Provider 请求为准，而不是只看 `stable_prefix_hash`：

1. 同一部署下，任务列表从空变为非空时，system/instructions 字节完全相同。
2. 完整 `tools`、`native_tools`、工具顺序和 schema 完全相同。
3. 仅最后一条 current user input 中的 runtime payload 发生变化。
4. Responses continuation 的既有 input 顺序不变，动态快照只能追加在本轮输入尾部。
5. `static_prompt_revision` 不变；`request_shape_hash` 不因任务数据变化而变。
6. `conversation_prefix_hash` 在相同历史前缀下保持一致。
7. 对普通聊天、主动群聊、插件唤醒和自动化 Agent 入口分别比较最终送到 Provider 的 payload，并报告第一处差异。

允许部署版本因为一次静态合同更新产生新的稳定前缀；部署后每轮任务状态变化不得继续改变它。

## 8. 工作包 D：文档与旧链路清理

### D1. 更新现行文档

实施后同步更新：

- `docs/architecture/main-agent-runtime.md`
- `docs/operations/social-workspace-sandbox.md`
- `docs/architecture/README.md` 中对应入口（如新增独立现行文档）

其中 `social-workspace-sandbox.md` 在实施前有以下过时内容，现已改正文：

- 仍使用 `send_private_message` / `send_group_message` 名称。
- 仍声称 send/poke 存在每目标/全局分钟限流。
- 仍区分“普通 final reply 不计入主动操作限流”。

这些陈述与当前 `send_message`、无自动最终回复、已删除社交限流的运行合同冲突，必须直接改正文，不能再叠一份补充说明。

### D2. 删除确认死亡的代码

引用扫描和回归证明无调用后已删除：

- 无法产生或消费的 `ConfirmedQuoteRejection`
- 只服务于已删除旧发送工具的适配分支
- 过时测试 fixture 和文档示例

第二轮清理进一步完成：Self Reflection 管理报告迁移到 `send_message`；
`SocialService`、工作交付观察器和新回执写入仓库不再接受
`send_private_message` / `send_group_message`。旧回执 action 只作为不可变历史锚点读取，
不构成可执行兼容入口。Agentic 自动化脚本中的无效 `allowed_capabilities` 元数据由
迁移 0064 物理删除并重算脚本哈希；显式调度 DSL 的 `onebot.send_*` 不属于旧 Agent 工具，继续保留。

不建立兼容别名，不保留“以后可能有用”的双链路。

## 9. 明确不做

本任务禁止顺手恢复或新增：

- `report_progress`
- 自动最终回复
- reply effect queue
- `finish_turn` 的发送语义
- send/poke 频率限制
- 新消息到达时取消已经准备执行的旧发送
- 来源白名单、引用出口审查或链接改写
- 插件 SDK 过渡兼容层
- 自动化语义去重或数据库唯一约束
- 对 `[提及成员1]` 这类普通文本占位符做无证据清理
- 对既有历史消息、账本事件、Rollup 或记忆做追溯清洗

结构化 `mentions` 必须继续生成真实 OneBot `at` segment。当前没有证据证明 `[提及成员1]` 是生产缺陷；它可能只是模型讨论历史文本，出现真实错误案例后再单独处理。

## 10. 文件级改动清单

| 区域 | 主要文件 | 预期改动 |
| --- | --- | --- |
| 出站净化 | `services/renderer.py` | 复用/收窄为可显式调用的模型文本规范化合同 |
| 社交发送 | `social/service.py` | canonical args、分条前净化、统一回执/媒体/账本输入 |
| 社交模型 | `social/models.py` | 如需要，增加规范化后的安全构造；不放业务正则 |
| OneBot | `adapters/onebot/sender.py`、gateway provider | 明确引用拒绝分类或删除死类型 |
| 自动化查询 | `automation/repository.py` | 全局/目标会话有界查询与分页 |
| 自动化服务 | `automation/service.py` | 安全目录 DTO、可见性与 owner mutation 分离 |
| 自动化工具 | `automation/tools.py` | 重定义 list/get/match 返回；写工具保持 owner 校验 |
| 权限目录 | `admin/permission_catalog.py` | 区分全局安全读取与本人管理 |
| 上下文 | `services/context_assembler.py` | 装配当前会话任务快照 |
| 提示词 | `services/prompt_composer.py`、`prompting/contracts.py` | TURN 动态贡献与固定解释规则 |
| 依赖注入 | `application/modules/automation.py`、`container.py`、相关 handlers | 向统一装配器提供只读目录能力 |
| SDK/命令 | `plugin_host/facades.py`、`services/automation_commands.py` | 明确保留 owner-scoped，不与模型全局目录混淆 |
| 文档 | 两份现行运行文档 | 删除旧工具名、限流与自动 final 叙述 |

## 11. 验证矩阵

### 11.1 `send_message`

- 精确输入 `#62052>正文`，网关、receipt payload、ledger content 均为 `正文`。
- 带 `#事件号|回复:...>` 的多行文本逐行正确清理。
- 分条在净化后执行；manifest 的 original 与 chunks 都是 canonical 内容。
- 图片文字、文件 caption、语音 spoken text fallback、表情上下文全部使用 canonical text。
- 清理后为空时，无 receipt、无 claim、无网关调用、无账本事件。
- structured mentions 原样保留，后续分条不重复 mention。
- Markdown `# 标题`、句中 `#62052`、模型主动来源/链接保持不变。
- 同 call 重放不重复发送，raw/canonical 不造成 receipt 冲突。
- uncertain 不自动重试；仅确认未接纳的引用拒绝允许一次无引用重试。

### 11.2 自动化目录

- A 创建目标为当前群的任务；B 的 `automation_list` 能看到相同的
  `ID + 任务内容 + 下次时间 + 创建者`。
- B 尝试 update/pause/cancel/run_now 时后端拒绝，任务不变。
- `automation_get` 能区分不存在与存在但不可管理。
- `match_task` 能找到其他 owner 创建的同群等价候选，但不自动合并。
- 全局目录分页稳定，无重复/漏项；active/paused/terminal 状态过滤正确。
- 默认目录不逐条重复 active、人物、owner、权限、完整 schedule 或运行计数。
- 目录不包含 authority snapshot、完整脚本和路由秘密。
- slash command 与 SDK `list_current_owner()` 仍只返回本人任务。

### 11.3 提示词与前缀缓存

- 群聊即使无任务也只带一行明确的空快照。
- 数据库查询失败时标记 unavailable，不伪装为空。
- 当前群只预取该群 active 任务；paused/terminal 按需调用工具，不泄漏其他私聊任务。
- 每轮自动注入的快照严格限制为 `ID + 任务内容 + 下次时间`，active 状态和人物字段不重复；
  按需调用 `automation_list/get` 的结果仍包含创建者。
- 快照最多 8 项、1,200 字符，溢出只给数量和 `automation_list` 提示。
- 不同任务快照下 system/instructions、tools、native_tools 完全一致。
- 对最终 DeepSeek Responses/OpenAI-compatible Provider payload 做序列化比较，不只比较内部 metrics。
- 普通聊天、主动群聊、插件唤醒、自动化 Agent 各有覆盖。
- 动态快照超预算时必须明确失败或采用规定的有界截断；不得静默删除 required 贡献，也不得移动到 system 前缀。

### 11.4 回归

- 自动化调度、claim、misfire、run cursor、owner mutation 测试通过。
- 社交路由、mentions、附件、语音、表情、recall 测试通过。
- `git diff --check`、Ruff、相关 mypy 通过。
- 只运行与风险匹配的定向测试；部署前再执行既定主 Agent/automation/social 回归集合。

## 12. 实施顺序

1. 先建立出站 canonical payload 与回归测试，修复已经发生的用户可见泄漏。
2. 建立 automation 安全 DTO、Repository 查询和 Service read API。
3. 改造 `automation_list/get/match`，保持所有 mutation owner-only。
4. 将当前会话快照接入 `ContextAssembler` 和 `PromptComposer`。
5. 做完整 Provider payload 前缀一致性测试；若前缀被动态状态改变，不得继续上线步骤。
6. 更新现行文档并删除确认死亡的旧链路。
7. 在部署前刷新生产任务清单，制定一次性重复任务清理表。

建议分为至少三个独立提交：

1. `fix: canonicalize explicit outbound messages`
2. `feat: expose safe automation directory and runtime snapshot`
3. `docs: align social and automation runtime contracts`

## 13. 生产清理与上线

代码上线不会自动消除已经重复创建的任务。上线前必须重新读取生产状态，并由用户确认保留哪一项。

按当前快照，合理候选是保留最早且已真实运行的 ID 85，取消 87、88、89、90；但这是时点建议，不是本任务书授权的操作。不得静默删除数据库行，优先使用正常 cancel 流程保留审计和历史。

上线步骤：

1. 备份配置与 SQLite DB/WAL/SHM，并验证备份可读。
2. 只替换 Bot，不重启 SnowLuma/QQ 网关。
3. 等待健康检查、OneBot 重连、automation worker 和 runtime work ready。
4. 不发送真实测试消息、不戳真实用户。
5. 用只读账本/回执和受控 fake gateway 验证新链路。
6. 对两个不同群成员查询同一群任务，确认“可见但不可管理”。
7. 比较实际 Provider 请求前缀；确认任务状态变化没有改变稳定 instructions/tools。

回滚代码时保留新产生的消息、任务和回执；不得用旧数据库覆盖生产库。

## 14. 完成定义

只有同时满足以下条件才可称为完成：

- `send_message` 的所有文字出口共享同一 canonical payload。
- 生产复现格式 `#62052>` 不再进入网关或账本。
- Yuki 能通过一个统一工具查看全局自动化任务简表。
- 当前群每轮都有有界、权威、极简的 active 任务快照。
- 非 owner 能看到任务存在，但不能修改。
- 不依赖语义去重也不会再把 owner-scoped 空列表说成全局不存在。
- 动态任务状态不改变 system/instructions、固定 tools 或 Provider 可复用前缀。
- 旧工具名、旧限流、自动 final 等过时文档已清理。
- 定向回归通过，并完成备份、Bot-only 部署、健康与重连核验。
