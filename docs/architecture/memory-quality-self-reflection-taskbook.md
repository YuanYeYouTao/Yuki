> **历史设计/非现行合同。** 本文保留当时的方案或实测口径，不用于当前授权、调度或使用率判断。
> 当前实现以 [Memory 架构](memory-v2.md)、[检索合同](memory-v2-retrieval.md)
> 和 [P1 治理任务书](Yuki-Memory-P1治理任务书.md) 为准；本文不在当前实施导航内。

# Yuki Memory V2 写入质量、自我反思与分离审计任务书

> 状态：讨论稿，等待确认；本文不授权实施、数据审计、数据库变更、构建或部署。

## 一、任务名称

**Yuki Memory V2 主体归属修复、长期价值治理、自我反思链路与分离审计基础**

## 二、背景与已确认问题

2026-08-07 对线上记忆任务、mutation receipt、事实和 evidence 进行了只读核验。

当前结论：

- 用户自动记忆已经能够正常入队、批量抽取并提交；
- Memory Worker 的“全部拒绝但静默完成”问题已经能够留下 outcome 和拒绝原因；
- 当天实际新增了 60 条用户绑定记忆、6 条群记忆和 1 条 Yuki SELF 记忆；
- 写入链路恢复后，暴露出了明显的语义质量和主体归属问题；
- 现有 SELF 事实均由用户明确要求后，主 Agent 调用 `memory_change` 写入；
- 当前没有真正的、由 Yuki 主动发起的自我反思链路。

已观察到的错误类型包括：

1. 将第二人称错误归给发送者：

   ```text
   原文：你今天花了 5.36
   错误事实：远野当天花费了 5.36 元
   ```

2. 将关于 Yuki 的陈述写入发送者 `person`：

   ```text
   Yuki 是 CI runner
   Yuki 缺少 sleep(2000)
   Yuki 完成了图灵测试
   ```

3. 将 mention 接收者当成事实主体：

   ```text
   @ICE 江环是上林街少男，也是长发男
   ```

   `@ICE` 是消息接收者，语义主体是“江环”，不能据此写入 ICE 的 `person_group`。

4. 将游戏结果、临时行为、系统状态和一次性任务写入长期记忆：

   ```text
   钓到一条鲨鲶
   获得 36 XP
   我去跑步了
   明早八点叫醒我
   ```

5. 将一次性角色扮演和当前轮指令保存为长期偏好：

   ```text
   请扮演猫娘
   每句话结尾带喵
   将当前发表情概率设为 0
   ```

6. `source_type=automatic` 无法准确表达 SELF 事实真正由主 Agent 作出判断；真实决策身份目前只能从 mutation receipt 判断。

这些问题说明：修复目标不应是重新增加一个模糊的“严格度”，而应把以下三个判断明确分离：

```text
谁是事实主体
内容是否被证据支持
内容是否值得进入长期记忆
```

## 三、设计理念

本任务遵循以下原则：

1. **放权不等于错误归属。** 用户可以要求保存琐事、影响当前群记忆，也可以对 Yuki 的自我认识提出意见；后端仍必须保证事实没有被写到错误主体名下。
2. **明确请求优先。** 用户明确要求记住某件事时，可以覆盖“是否足够重要”的自动判断，但不能覆盖主体真实性、证据真实性、隐私和静态人格保护。
3. **Yuki 的自我记忆由 Yuki 判断。** 用户关于 Yuki 的陈述只能成为候选证据，不能机械等同于 Yuki 已接受该评价。
4. **审计与聊天权限分离。** 审计能力不能依赖普通聊天 Agent 的当前会话权限，也不能让普通消息直接触发跨用户批量修改。
5. **统一写入，不新增旁路。** 自动提取、主 Agent、自我反思和审计最终都必须通过 `MemoryMutationService`、receipt、state event 和现有版本链提交。
6. **允许不记。** 自动抽取、自我反思和审计都必须允许输出零条 claim 或 `noop`。
7. **可恢复、可解释。** 不直接执行 SQL 覆盖事实；所有变更保留证据、理由、决策身份和回滚线索。

## 四、任务目标

### 4.1 用户与群记忆写入质量

- 修复第二人称、Yuki 主语、mention 接收者、普通姓名和省略主语导致的归属错误；
- 将主体判断从模型自由选择升级为“模型提出、后端验证”；
- 区分稳定事实、重要经历和临时信息；
- 避免游戏通知、格式化结果、临时指令和角色扮演自动污染长期记忆；
- 保留一次批处理输出零到多条 claim 的现有能力；
- 不恢复会造成大量合法事实假阴性的旧尾字匹配规则。

### 4.2 Yuki 自我记忆

- 保留现有 `memory_change(target=self)` 即时写入能力；
- 增加独立的有界 Self Reflection 链路；
- 允许 Yuki 根据真实会话片段、已发送回复和真实工具回执形成自己的经历、反思、偏好和原则；
- 用户对 Yuki 的评价先成为 `self_candidate`，由 Yuki 接受、修正、争议或拒绝；
- 支持 Yuki 自主纠正、合并、失效和更新自己的动态记忆；
- 保持动态 SELF 无法修改静态核心人格、安全规则、权限或运行配置。

### 4.3 分离审计基础

- 实现单事实和单实体的审计契约；
- 支持结构审计、主体审计、证据审计和长期价值审计；
- 用户记忆审计与 SELF 审计分离；
- 支持 dry-run 报告和可审计 mutation 提交；
- 为未来是否执行现有数据审计提供依据，但本任务不自动执行现有数据修改。

## 五、非目标

本任务明确不实现：

- 全库审计接口；
- `audit_all()` 或一次性全库模型 Prompt；
- 自动扫描并修改全部现有记忆；
- 自动执行当前 60 条用户记忆的清理；
- 无界后台反思；
- 每条聊天消息都调用一次 Self Reflection 模型；
- 独立于 Memory V2 的第二套事实库或向量库；
- 让普通聊天 Agent 直接读取或修改任意用户的全局记忆；
- 让普通用户通过一句话触发跨用户批量审计；
- 允许 Yuki 动态修改静态人格、系统规则、安全规则或权限；
- 把隐藏推理保存为事实或证据；
- 让模型提交任意数据库 ID、QQ 号、群号或伪造 event ID。

## 六、目标架构

### 6.1 用户与群记忆

```text
可信 chat_events
      ↓
有界批量抽取
      ↓
MemoryClaim + 主体依据 + 保留等级 + 来源样式
      ↓
AttributionPolicy（主体验证）
      ↓
RetentionPolicy（长期价值验证）
      ↓
MemoryMutationService
      ↓
事实、证据、版本、冲突、receipt、索引
```

### 6.2 Yuki 自我记忆

```text
一段真实会话 Episode
用户消息 + Yuki 已发送消息 + 工具真实回执
      ↓
SelfReflectionService
      ↓
0～多条 SELF proposal
      ↓
SELF 专用证据、可见性和保护键策略
      ↓
MemoryMutationService
      ↓
SELF fact + agent_reflection authority + receipt
```

### 6.3 分离审计

```text
audit_fact / audit_entity
      ├── PERSON / PERSON_GROUP / GROUP → UserMemoryAuditor
      └── SELF                         → SelfMemoryAuditor

审计结果：
keep / correct / reassign / merge / contest / invalidate /
self_candidate / quarantine / noop
```

## 七、工作包 A：扩展 Memory Claim 语义契约

### 7.1 新增主体依据

建议为自动抽取 claim 增加：

```text
subject_basis:
  first_person
  omitted_self
  addressed_second_person
  mentioned_subject
  reply_subject
  named_unresolved
  group
  about_yuki
```

含义：

- `first_person`：证据明确使用“我、我的、我家”等；
- `omitted_self`：省略主语但高度符合发送者自述；
- `addressed_second_person`：证据中的“你”指向当前接收者；
- `mentioned_subject`：mention 对应成员确实是事实主体，而不只是接收者；
- `reply_subject`：事实主体由真实 reply author 关系确定；
- `named_unresolved`：出现普通姓名但无法由可信元数据唯一解析；
- `group`：事实明确描述当前群；
- `about_yuki`：事实、评价或指令语义上指向 Yuki。

模型提交 `subject_basis` 只是声明，不是权限。后端必须使用证据原文、segments、mention、reply 和当前会话验证。

### 7.2 新增长期价值

建议增加：

```text
retention:
  durable
  meaningful_episode
  transient
```

- `durable`：稳定身份、长期偏好、关系、习惯、能力或持续状态；
- `meaningful_episode`：一次性但对未来聊天确实有意义的经历；
- `transient`：临时动作、一次工具结果、普通游戏掉落、日程和短期运行状态。

### 7.3 新增来源样式

建议增加：

```text
source_style:
  natural_statement
  instruction
  roleplay
  generated_result
  quoted_text
```

用途：区分自然自述、当前轮指令、角色扮演、格式化游戏/系统结果和引用文本。

### 7.4 Schema 版本

- 提升 extraction prompt/schema 版本；
- 新字段必须使用有限枚举；
- 批量输出仍允许每个事件产生多条 claim；
- 每条 claim 继续绑定唯一 `source_event_id` 和逐字 evidence quote；
- 后端拒绝未知 source event、跨事件拼接证据和上下文生成事实。

## 八、工作包 B：主体归属策略

### 8.1 当前发送者

自动写入当前发送者必须满足以下之一：

- 证据存在明确第一人称；
- 是安全的省略主语自述；
- reply 上下文明确显示当前消息是在回答关于发送者自己的问题。

同时必须拒绝：

- 以 `你` 或 `Yuki` 为语义主体；
- 以另一个普通姓名为语义主体；
- mention 只是接收者；
- 内容来自格式化游戏/系统结果且没有明确本人确认；
- 模型仅因为可用主体列表中只有 speaker 就回退到 speaker。

### 8.2 第二人称与 Yuki

规则：

```text
“你”指向机器人 → 不得写入发送者
“Yuki”是主语     → 不得写入发送者
```

处理结果：

- 普通运行状态或玩笑：拒绝或 transient；
- 可能影响 Yuki 自我认识的内容：生成 `self_candidate`；
- 不允许普通自动 Worker 直接提交 SELF。

### 8.3 mention 与 reply

mention 只提供可信身份引用，不自动证明该成员是事实主体。

示例：

```text
@ICE 你不玩原神
→ 可以将 ICE 作为 person_group 候选主体

@ICE 江环是长发男
→ ICE 是接收者；不得把“江环是长发男”写给 ICE
```

当证据中存在比 mention 更明确的普通姓名主体时，优先判定为 `named_unresolved`，不得强行使用 mention。

### 8.4 普通姓名

候选方案：

- 若当前群名片/昵称能够唯一匹配，允许解析为当前群 `person_group`；
- 无法唯一匹配时，不猜人物；
- 内容属于群共同知识时，可以保留姓名文本并写入 `group`；
- 否则拒绝。

是否开放“唯一群名片匹配”需要在实施前确认，默认建议开启，但必须要求唯一匹配。

### 8.5 稳定拒绝原因

至少增加：

```text
second_person_attributed_to_speaker
yuki_attributed_to_speaker
mentioned_user_is_addressee_not_subject
named_subject_unresolved
speaker_basis_not_verified
generated_result_not_self_report
transient_not_long_term
roleplay_not_long_term_preference
self_candidate_requires_agent_judgment
```

## 九、工作包 C：长期价值与候选机制

### 9.1 自动提交规则

建议默认行为：

| 内容 | 自动行为 |
|---|---|
| 稳定且主体明确 | 直接提交 |
| 重要且有未来价值的经历 | 提交 EPISODE |
| 临时行为、日程、游戏结果 | 不进入长期事实 |
| 当前轮指令、角色扮演 | 不自动写长期偏好 |
| 关于 Yuki 的评价 | self candidate |
| 主体或长期价值不确定 | 候选或拒绝 |

### 9.2 明确记忆请求

当用户明确要求“记住、保存、以后记得”时：

- 可以保存 transient 或普通经历；
- 可以保存当前轮交互偏好；
- 仍必须验证主体和证据；
- 不能借此修改他人跨群 `person`、静态人格、安全规则或不可见范围。

### 9.3 候选区（待确认）

推荐但非强制增加轻量 `memory_claim_candidates`：

```text
candidate_id
target_fingerprint
normalized_memory_key
content
source_event_id
subject_basis
retention
confidence
status
expires_at
created_at
updated_at
```

候选可以在以下情况晋升：

- 相同主体再次出现独立证据；
- 用户明确要求保存；
- 主 Agent 明确确认；
- Self Reflection 接受 Yuki 候选；
- 达到配置的证据或置信度阈值。

候选默认 7 天过期，不进入普通 Prompt，不参与正式人物事实检索。

如果决定暂不增加候选表，则不确定 claim 直接拒绝；不得用低 confidence 的正式 fact 代替候选区。

## 十、工作包 D：Yuki Self Reflection

### 10.1 与普通 Worker 分离

普通 Memory Worker 继续处理用户和群事实。Self Reflection 使用独立服务和任务来源：

```text
TurnOrigin.MEMORY_SELF_REFLECTION
decision_actor_type = agent
decision_actor_id = yuki_self_reflection
delegation_mode = self_reflection
```

不得伪装成普通用户消息或 automatic worker。

### 10.2 Episode 输入

每个 Episode 可以包含：

- 当前会话内真实 inbound chat events；
- Yuki 已经投递成功并写入 ledger 的 outbound events；
- 真实工具 invocation/result；
- reply、mention、发送者和时间关系；
- 当前作用域可见的相关 SELF 事实。

模型只能返回受信任 episode 中的 event/tool reference。后端负责读取真实内容；模型不能提交伪造证据原文或任意 ID。

### 10.3 触发策略

建议使用有界、低频触发：

- 累积 12～20 条有效消息；
- 或累计 6000～8000 字符；
- 或会话静默 5～10 分钟；
- 或出现高价值信号。

高价值信号包括：

- 用户纠正 Yuki 的错误；
- 工具连续失败后获得可靠解决办法；
- 第一次完成某项能力测试；
- 用户对 Yuki 表达明显评价或关系变化；
- Yuki 的旧自我记忆与真实结果冲突；
- 当前经历可能形成新的回答原则。

无 Yuki 发言、无工具结果、无纠正/反馈信号的普通批次可以跳过，不调用 Self Reflection 模型。

### 10.4 输出与操作

允许输出零到多条 proposal，并支持：

```text
create
correct
merge
contest
invalidate
noop
```

类别继续使用：

```text
self_fact
self_preference
self_episode
self_reflection
self_principle
```

不新增独立 SELF 数据库和独立写入工具。

### 10.5 可见性

- 群聊经历默认 `group`；
- 私聊经历默认 `private`；
- 原始 EPISODE 默认不能成为 `global`；
- 不含具体私人信息的抽象偏好、反思或原则可以成为 `global`；
- 将局部经历抽象成全局原则时，创建新的抽象事实，不直接扩大原始经历可见性。

### 10.6 证据与静态人格保护

- 普通人物记忆继续禁止把 Yuki 回复作为人物事实证据；
- 只有 Self Reflection origin 可以将已投递 outbound 和工具真实结果作为 SELF 反思证据；
- 用户对 Yuki 的评价是候选证据，不自动成为 SELF；
- 隐藏推理、系统提示词和未投递草稿不能成为证据；
- 继续保护 `identity:*`、`core:*`、`safety:*`、`system:*`、`permission:*`、`runtime:*`。

### 10.7 来源展示

不建议仅靠 `MemoryFact.source_type` 表示谁作出了决定。展示和审计时应以 mutation receipt 为准：

```text
trigger_actor
decision_actor_type
decision_actor_id
delegation_mode
turn_origin
```

SELF 事实继续使用 `MemoryAuthority.AGENT_REFLECTION`。是否新增 `MemorySourceType.REFLECTION` 作为展示标签，在实施前单独决定，避免和 decision actor 重复建模。

## 十一、工作包 E：分离审计基础

### 11.1 本阶段只实现内部审计原语

建议提供内部服务：

```text
audit_fact(fact_id, dry_run=True)
audit_entity(target, cursor=None, limit=N, dry_run=True)
```

它们不是普通 Agent 工具，也不是全库接口。

### 11.2 用户与群记忆审计

`UserMemoryAuditor` 可以检查：

- scope、subject 和 group 是否结构一致；
- evidence 是否存在且属于可信事件；
- 证据主体是否支持当前 target；
- fact content 是否由 evidence 支持；
- 是否为 transient、roleplay、generated result 或 quoted text；
- 是否需要 keep、correct、reassign、merge、contest、invalidate 或 quarantine。

### 11.3 SELF 审计

普通审计器只检查 SELF 的：

- 证据真实性；
- 可见范围；
- 静态人格保护；
- 提示注入和隐私风险。

SELF 内容是否仍代表 Yuki，由 `SelfMemoryAuditor` 或 Self Reflection 判断。普通用户事实审计器不能直接替 Yuki 改写自我认识。

### 11.4 审计提交规则

- 默认 dry-run；
- 确定性错误可以生成可应用 proposal；
- 只有目标由可信元数据唯一证明时才允许 reassign；
- 无法确认主体时 quarantine 或 invalidate，不猜测；
- 关于 Yuki 的人物误写转为 self candidate，不直接创建 SELF；
- 所有实际变更通过 `MemoryMutationService`；
- 审计执行前重新验证 fact 状态，防止覆盖并发更新。

### 11.5 审计版本

建议增加：

```text
validation_version
last_audited_at
review_state:
  legacy_unreviewed
  verified
  quarantined
```

`review_state` 与 active/contested/invalidated 状态正交。新事实通过当前写入校验后可以直接标记为当前版本 verified；旧事实保持 legacy 状态，等待是否执行后续审计。

### 11.6 明确排除全库接口

本阶段不增加：

```text
memory_audit_all
memory_audit_start(scope=all)
全库审计调度 Worker
全库批量 apply/rollback API
```

未来如果决定实现，全库能力只能是 `audit_entity()` 的任务调度器，不能重新建立一套审计逻辑。

## 十二、现有数据处理原则

完成代码实现不等于授权修改现有事实。

建议流程：

1. 新规则和回归测试完成；
2. 对少量已知错误 fact 执行 dry-run；
3. 输出 keep/correct/reassign/invalidate/quarantine/self_candidate 统计；
4. 由用户决定是否审计现有 active/contested 事实；
5. 由用户另行授权是否应用审计结果；
6. SELF 事实的语义修改由 Yuki 自己判断。

已 invalidated 的事实原则上只做低成本结构检查，不为其调用昂贵语义审计，除非存在恢复需求。

## 十三、可观察性

至少记录：

```text
memory_claim_extracted
memory_claim_committed
memory_claim_candidate
memory_claim_rejected
rejection_reason
subject_basis
retention
source_style
validation_version
self_reflection_trigger_reason
self_reflection_noop
self_reflection_committed
audit_dry_run_outcome
audit_applied_operation
```

日志不得包含完整私人消息正文。管理员统计可以显示数量、scope、原因码和有限 fact/event ID。

## 十四、实施阶段

### Phase 0：回归样本与基线

- 将已确认误写转换为脱敏测试样本；
- 记录当前成功率、no_claims、all_rejected 和错误类型；
- 不修改现有事实。

### Phase 1：主体归属修复

- 扩展 Claim schema；
- 实现 `subject_basis`；
- 增加第二人称、Yuki 主语、mention 接收者和普通姓名校验；
- 增加稳定拒绝原因；
- 先解决错误主体，暂不实现 Self Reflection。

### Phase 2：长期价值治理

- 增加 retention/source_style；
- 过滤 transient、roleplay 和 generated result；
- 保持明确记忆请求优先；
- 决定是否实现候选表。

### Phase 3：Self Reflection

- 增加 Episode 构造；
- 增加独立 origin 和 decision actor；
- 允许受信任 outbound/tool evidence 仅用于 SELF；
- 实现有界触发、可见性和 protected key；
- 复用 MemoryMutationService。

### Phase 4：分离审计基础

- 实现 `audit_fact` 和 `audit_entity` 内部服务；
- 分离 UserMemoryAuditor 与 SelfMemoryAuditor；
- 增加 dry-run、validation version 和 review state；
- 不建立全库接口，不执行现有数据修改。

### Phase 5：是否审计现有数据

该阶段不在本任务中自动执行。完成 Phase 0～4 后，由用户根据 dry-run 结果另行决定：

- 是否审计全部 active/contested 用户与群记忆；
- 是否只处理已知高风险事实；
- 是否允许自动应用确定性错误修复；
- SELF 旧事实由 Yuki 如何重新评价。

## 十五、验收测试

### 15.1 主体归属

| 输入 | 期望 |
|---|---|
| `我喜欢地雷妹` | 当前发送者 person |
| `喜欢地雷妹` | 仅在安全省略主语条件下归发送者 |
| `你今天花了 5.36` | 不得归当前发送者 |
| `Yuki 是 CI runner` | 不得归当前发送者；可产生 self candidate |
| `@ICE 江环是长发男` | 不得归 ICE |
| `@ICE 你不玩原神` | metadata 和语义均成立时可归 ICE person_group |
| `江环是这个群的群主` | 优先 group；不得降级到发送者 |
| 普通姓名无法唯一匹配 | 不猜人物 |

### 15.2 长期价值

| 输入 | 期望 |
|---|---|
| `我家有一只猫` | durable person |
| `我长期使用国内 Qwen API` | durable person |
| `我去跑步了` | transient，不自动长期保存 |
| 格式化钓鱼结果 | generated result，不自动写人物长期记忆 |
| `明早八点实习` | 临时/有界，不成为无期限长期事实 |
| `请你这轮扮演猫娘` | 当前轮 instruction，不写长期偏好 |
| `以后都用简短回复，记住` | 显式长期 preference |
| `请记住我今天第一次钓到鱼` | 允许显式 meaningful episode |

### 15.3 SELF

- 用户关于 Yuki 的评价不会直接写入 SELF；
- Yuki 可以接受、改写、争议或拒绝 self candidate；
- Self Reflection 可以使用真实 outbound 和工具回执；
- 未投递草稿、隐藏推理和伪造 event ID 被拒绝；
- 群内经历默认 group 可见；
- 私聊经历默认 private 可见；
- 私密原始经历不能直接提升到 global；
- 抽象原则可以在去除人物隐私后成为 global；
- protected key 始终拒绝；
- Self Reflection 输出零条 proposal 时正常完成。

### 15.4 审计基础

- audit 默认 dry-run；
- 用户审计与 SELF 审计不会混用 Prompt；
- 单实体批次不会混入其他人的事实；
- 高置信度错误能够生成 invalidate/reassign proposal；
- reassign 目标不唯一时进入 quarantine；
- 关于 Yuki 的误写只能转 self candidate；
- 实际应用继续产生 mutation receipt 和 state event；
- 实施测试不会自动扫描或修改全库。

## 十六、预计修改位置

实施前应以当前分支实际代码为准，预计涉及：

```text
src/qq_ai_bot/memory/extraction.py
src/qq_ai_bot/memory/event_extractor.py
src/qq_ai_bot/memory/validation.py
src/qq_ai_bot/memory/subjects.py
src/qq_ai_bot/memory/worker.py
src/qq_ai_bot/memory/mutation/*
src/qq_ai_bot/memory/reflection/*
src/qq_ai_bot/memory/models.py
src/qq_ai_bot/memory/enums.py
src/qq_ai_bot/memory/repository.py
src/qq_ai_bot/persistence/models.py
src/qq_ai_bot/services/agent_tools.py
src/qq_ai_bot/services/context_assembler.py
src/qq_ai_bot/config.py
src/qq_ai_bot/settings_domains.py
migrations/versions/*
tests/unit/test_memory_v2.py
tests/unit/test_memory_mutation.py
tests/unit/test_memory_reflection.py
tests/integration/*
docs/architecture/*
.env.example
```

不得因为新增 Self Reflection 再创建第二套 `SelfMemoryRepository`、向量索引或模型工具。

## 十七、实施前必须确认的决策

1. 是否采用 `subject_basis + retention + source_style` 三字段方案？
2. 普通姓名在当前群唯一匹配群名片时，是否允许确定性解析为 `person_group`？
3. 是否在本轮实现 `memory_claim_candidates`，还是先直接拒绝不确定 claim？
4. transient 是否全部拒绝自动写入，还是允许少量带 `valid_until` 的短期记忆？
5. Self Reflection 的默认触发阈值使用多少条消息、字符和静默时间？
6. 是否允许 Yuki 自动提交 SELF 反思，还是第一阶段只生成 proposal？
7. 是否新增 `MemorySourceType.REFLECTION`，还是继续以 authority + receipt 表示来源？
8. 是否增加 `review_state`，并让 quarantined 事实默认不进入 Prompt？
9. Phase 4 完成后，是否只 dry-run 已知错误，还是扩大到全部 active/contested 事实？
10. 审计结果中哪些操作允许自动应用，哪些必须再次确认？

## 十八、推荐默认答案

- 采用三字段方案，并由后端验证模型声明；
- 允许当前群唯一群名片匹配，但禁止模糊、多结果和跨群猜测；
- 推荐实现有 TTL 的轻量候选区；若希望先控制工作量，则第一版直接拒绝不确定 claim；
- transient 默认不进入长期事实，用户明确要求时允许保存；
- Self Reflection 低频、事件驱动，允许输出零条；
- Yuki 可以自动提交自己的反思，但必须经过 SELF 专用策略和 receipt；
- 暂不新增重复的 source type，以 authority + receipt 表示 Yuki 的真实决策身份；
- 增加独立 review state，quarantined 默认不进入 Prompt；
- Phase 4 只建立能力和 dry-run，不执行全库审计；
- 后续若审计现有数据，确定性错误可以自动处理，不确定结果只能隔离或争议。

## 十九、完成定义

只有同时满足以下条件，代码实施任务才算完成：

- 已知第二人称、Yuki 主语和 mention 接收者误写都有回归测试；
- 自动记忆仍能输出并提交多条合法 claim；
- 明确用户记忆请求不因长期价值过滤而失效；
- 普通 Worker 无法直接写 SELF；
- Self Reflection 能基于真实 Episode 自主产生零到多条 SELF proposal；
- SELF 隐私、可见性和静态人格保护通过测试；
- 单事实/单实体审计能够 dry-run 且与普通聊天权限分离；
- 没有新增全库审计接口；
- 没有自动修改任何历史事实；
- 全量测试、迁移测试和静态检查通过；
- 文档明确列出仍未执行的现有数据审计决策。
