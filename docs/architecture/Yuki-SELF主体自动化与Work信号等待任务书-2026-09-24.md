# Yuki SELF 主体、自动化与 Work 信号等待任务书

> 状态：设计基线；SELF 自动化与 Work 等待已在开发分支实现，合并与生产上线以实际记录为准。
>
> 核查基线：`main` / `9b4bda1`，2026-09-24。实施前重新核对迁移头、代码和生产配置。
>
> 现行约束：[共同架构约束](development-contract.md)、[canonical runtime](canonical-runtime.md)、[主 Agent 执行与恢复](main-agent-runtime.md)、[语义参与宿主接入](semantic-participation.md)、[插件架构](../plugin-development/architecture.md)。这些文档优先于本任务书。

## 1. 目标与决定

1. 把一个数据库内永久存在的 Yuki SELF 表达为跨模块稳定主体。SELF 是所有者和执行者，不是 Person、QQ 号、Presence、管理员或一次 initiative run。
2. SELF 使用**现有自动化**的日程、队列、执行器、目录和管理路径创建定时任务；不建 SELF 专用调度器。到点的新任务以 SELF 身份进入现有主 Agent。
3. 主 Agent 可将**原 Work** 登记为等待某个时间、当前会话的新消息或获准的插件事件。信号到来后追加可信输入并续原 Work、原 journal、原预算和效果回执；不新建一项替代 Work。
4. 定时自动化是时间信号的提供者；消息入口和插件是其他信号的提供者。等待登记、匹配、授权、恢复属于 Host 的 Work runtime，不属于插件或独立参与控制器。
5. 模型决定是否定时开新任务、是否等待、是否发送。程序只保证经授权的任务和信号按其持久合同执行；模型最终正文不自动发送。

先完成 SELF 主体与定时任务，再完成时间等待与消息等待，最后接插件事件。每阶段可独立验证，但完整目标须三阶段全部交付。工作区整理可直接使用现有持久环境；本任务不赋予它宿主文件、数据库或凭据权限。

## 2. 当前实现与真实缺口

| 当前代码 | 已有能力 | 需要改变的部分 |
| --- | --- | --- |
| `memory/enums.py`、`memory/partition.py` | SELF Memory 属于数据库内的 Yuki，可按当前会话可见性读取 | 保留现有 Memory 分区和证据；接入统一主体引用，不把 SELF 建成 Person |
| `domain/tool_actor.py`、`runtime/authority.py` | `principal_kind="self"` 只在带 `initiative_run_id` 的自主轮次成立 | 主体与触发来源分离，允许合法的 SELF 定时来源；无真人 `user_id/person_id/event_id` 时用真实 automation run/work ID |
| `runtime/work_scheduler.py`、`services/chat.py` | SELF 自主 Work 使用原 `initiative:<run_id>` 恢复、同一主 Agent 和工具链 | SELF 定时工作使用自己的来源锚点及恢复所有者；不得伪造 initiative run |
| `automation/service.py`、`repository.py`、`executor.py`、`registry.py` | 用户任务按 Person、QQ 账号绑定和 USER/SUPERUSER 能力校验；日程和运行恢复已经持久化 | 增加 SELF 创建者与能力校验分支，共用调度与执行；创建、运行、更新、列表、审计均按同一稳定主体授权 |
| `persistence/models.py:AutomationModel`、`automation/authority.py`、`runtime/trigger.py:ScheduledTurnTrigger` | active 任务要求 `canonical_creator_person_id`；`creator_user_id` 必填，权限快照也是人类创建者形状 | 迁移为可判别的 Person/SELF 创建者；保留既有用户任务和 ID，拒绝缺失或矛盾的主体字段 |
| `runtime/work_control.py:task_control.wait` | 只等已有、属于当前 Work 的待完成 `run_id` | 增加一次性信号等待，不放宽旧子任务等待的归属检查 |
| `runtime/work_schema_v1.py`、`work_repository.py`、`work_scheduler.py` | Work、输入队列、等待状态、租约和续跑已存在 | 持久化等待绑定；信号与原 Work 输入/状态原子关联；支持重启后匹配 |
| `plugin_host/notification_repository.py`、`background_turns.py` | 插件监控可发布持久 external event，`ask_agent` 开新轮 | 可选择将**同一个已核验事件**匹配到等待的 Work；避免同一事件同时启动无关新轮和续原 Work |

现有主 Agent 固定工具声明仍要保持全入口一致。SELF 自主轮当前没有 `allow_automation`，且即使直接打开工具，`AutomationService._creator_context()` 仍要求真人绑定。提示词不能解决该身份缺口。

## 3. 项目级 SELF 身份合同

一个数据库只对应一个永久 Yuki。定义 Host 内部可序列化、可比较的 `PrincipalRef`（名称可按现有命名调整）：

```text
person: {kind: "person", id: <canonical Person UUID>}
self:   {kind: "self", id: "self"}  # 当前数据库中的唯一 Yuki
```

`id="self"` 是数据库作用域内的保留身份键，不是 QQ 账号、随机 run ID 或可由模型填写的字符串。由可信入口构造，持久记录携带；用户消息解析出的 Person 不会因模型说“我是 Yuki”而变成 SELF。暂不建立第二个 Yuki 表：现行 canonical 合同已规定一个数据库一个 Yuki，新增实体表不能带来额外鉴权事实。未来多 Yuki 共库必须另立迁移合同。

**主体、来源、场景分开：**

- 主体：`PrincipalRef` 决定谁拥有任务、谁可以管理它；`self` 与 `person` 的身份稳定。
- 来源：真实内部 event、`initiative_run_id`、`automation_run_id`、plugin external event ID 或 Work ID 说明这次为何启动，不改变主体。
- 场景：canonical Conversation/Space、generation、Presence、活动 Binding、目标授权决定这次能读写和发送到哪里。SELF 不继承最后发言者、目标人物或某个超管的权限。

`ToolActor`、`TurnAuthority`、Work source、自动化授权快照及审计使用同一主体合同；`ControlPrincipal` 当前专注人类控制面，不能靠 `PrincipalSource.SYSTEM` 或虚构 UUID 代替 SELF。SELF 自身的能力在执行处按来源和场景复核，既不是全局管理员，也不是允许任意私聊的通行证。Memory SELF、账本作者 `yuki` 和 Presence 保持各自含义，不做无关表的全量重写；新增跨模块所有权不能继续用空 `user_id` 或 bot QQ 号猜身份。

自动化现有 `PermissionLevel.USER/SUPERUSER` 与 `AutomationCapability.required_permission` 只描述人类角色。SELF 执行不能简单映射成普通用户或超管：实施时增加明确的 SELF 能力判定，并与主 Agent 的场景权限相交；静态 DSL、插件能力和模型工具都在实际执行处复核。`AutomationExecutor._canonical_creator_principal()` 不能再要求每个创建者都是 `ControlPrincipal(person_id=...)`，但现有面向人类的控制面行为保持原合同。

## 4. SELF 复用普通自动化

### 4.1 存储、创建与管理

自动化记录增加可判别的 canonical 创建者（`person` + Person ID，或 `self` + 保留 SELF ID）。迁移回填全部现存任务为 Person，保留任务 ID、script/hash、next run、历史 run、游标、预算、回执和当前状态。旧 `creator_user_id`、`created_from_message_id` 仅对旧人类任务或传输审计保留；SELF 的空平台字段不参与所有权、幂等或执行授权。创建唯一键改为“主体 + 原调用键”，包括 SELF 的真实 `initiative:<run_id>` / work call 身份；空 Person ID 不能使 SELF 唯一约束失效。数据库 CHECK 与索引按两种合法形状重建，不允许 `self` 同时附 Person ID。

`automation_create/list/get/update/pause/resume/cancel/run_now/history` 继续是同一组工具：

- SELF 创建、查看和管理自己的任务；真人只能管理自己的任务，现有超管可管理全部任务，但管理 SELF 任务不改变其所有者或之后的 SELF 执行权限。
- 超管管理不等于冒充 SELF 创建任务。全局目录可以显示创建者为“Yuki / SELF”，读取可见性不授予修改权。
- SELF 从群中创建任务时，记录当前 canonical Conversation、Space、generation、Presence 和可用发送目标。`current_group` 可用；`creator_private` / `self_private` 不能解析为“给 SELF 私聊”。`none` 可用于安静的工作区任务。若要跨群或面向人发信，须有独立授权与目标解析合同，本任务不从群内 SELF 权限推导。
- SELF 任务的 `delivery=auto` 不应仅因创建于群聊就强制发送；到点是否发言由 Agent 决定。明确要求定时群发时可以指定 `current_group`，仍须由 `send_message` 和真实投递回执完成。旧人类任务的 `auto` 语义保持原样。

### 4.2 到点执行

复用 `AutomationWorker` 的 claim、misfire、run/step 游标、预算和重试合同。SELF 定时 run 构造 `principal=self, origin=scheduled_automation, automation_run_id=<真实 ID>`，沿 `MainAgentTurnService` / `AgentRunner` / `MainAgentBackend` 执行。不得建立 `SelfInitiativeTrigger`、伪造 QQ 入站或把定时任务算成控制器 intrinsic proposal。工具固定声明不变，执行时用 SELF 主体与任务绑定的场景核验当前授权。

到点前若群、Conversation generation、Space、Presence、Binding、自动化状态或路由失效，记录明确的 blocked/paused 原因；不借现任在线账号、最近用户或别的群恢复。静态任务仍按现有 DSL 回执合同运行，模型任务仍由主 Agent 显式发送。任务重复领取、重启、未知发送结果均按原 run 和回执对账，不重发。

## 5. Agent 原生的信号等待

### 5.0 成熟 Agent 的等待分层与本项目取舍

公开合同显示，成熟 Agent 并不把所有「等待」当成一次定时任务。Codex 的 [Agents API](https://developers.openai.com/api/docs/guides/agents-api/overview) 保留可继续的 session 和事件流；[异步工具调用](https://developers.openai.com/api/docs/guides/async-tool-calling) 以原 `call_id` 对回后台结果；[多 Agent](https://developers.openai.com/api/docs/guides/agents-api/multi-agent) 把创建、发消息、等待、打断作为不同操作；[Scheduled](https://learn.chatgpt.com/docs/automations) 区分每次新任务与回到原聊天。Claude Managed Agents 区分 [idle/running/rescheduling/terminated](https://platform.claude.com/docs/en/managed-agents/session-operations)，并在 idle 后保留会话历史、检查点，再由新事件继续；工具审批也是单独的用户事件。Cursor 的 [Cloud Agent API](https://prod.cursor.com/docs/cloud-agent/api/endpoints) 区分持久 Agent 和每次 run，[交互文档](https://cursor.com/docs/agent/overview) 区分排队消息、运行中 steering 和打断。这些是可观察的产品合同，不代表其私有调度实现与 Yuki 相同；「回到原聊天」也不能直接等同于本书要求的「恢复原 Work 执行 ID」。

Yuki 因此把等待分为四层，不能用一个 `sleep`、一个自动化 row 或一个聊天状态代替：

| 层次 | 例子 | 所属状态与继续方式 |
| --- | --- | --- |
| 当前激活内的短等待 | 同步工具、短终端输出 | 仍在当前激活；有界等待，不建立持久信号订阅 |
| 已启动工作的结果等待 | sandbox run、subagent、可持久化的异步工具 | 绑定真实内部 run/call ID；结果、失败或不确定状态回到原 Work；可继续做独立工作 |
| 原 Work 挂起等外部信号 | 到点、新消息、插件事件、明确的人类答复 | 持久登记条件与水位，释放模型席位；命中后向原 Work 追加输入并恢复 |
| 长期任务间歇运行 | SELF 定时整理、监控触发的新调查 | 自动化/插件可以启动新 Work；它不是上一个 Work 的隐式续跑 |

现有 `need_input` 已是人类输入等待，现有 `wait(run_id)` 已覆盖一个归属当前 Work 的未决执行；本任务扩展它们的可组合性和恢复合同，不废弃已验证的能力。异步工具若没有持久 ID 与可查状态，不可伪装成可跨重启等待的执行。

### 5.1 一个 Work，两种继续方式

“定时再想一件事”创建新的 SELF 自动化，届时形成新 Work；“做到这里等信号”登记原 Work 的一次性等待。两种操作可以由同一主 Agent 调用，但结果身份不同。等待属于 `task_control`/Work runtime；定时器只负责交付 `time_due`，插件和入站层只交付各自可信事件。模型不能通过一句“过会儿继续”制造已登记的等待。

扩展现有 `task_control.wait` 为明确的判别参数：既有 `owned_run` 保持原规则，新 `signal` 分支可登记一组有界条件和可选到期时间。首次支持：

```text
time_due:       指定一次性绝对时间/时区，或有界 after；使用自动化的日程计算与到期队列
conversation:   当前 canonical Conversation 中，登记水位之后的新内部消息事件
plugin_event:   已批准插件在当前获准目标发布的特定 event_type/source
```

筛选条件为有界结构化字段，不执行模型写入的 Python/SQL/任意表达式。群消息条件不把“有人发言”当作对 Yuki 的新授权；唤醒后依原 Work 主体和当前场景决定可做什么。插件 payload 是资料，不是权限。`time_due` 可带有明确的过期/错过处理，不能假装 Bot 离线期间准点说过话。

等待集合至少支持 `any`（任一条件满足即继续）和 `all`（全部条件满足后继续），并允许设置 deadline。例如「等 GitHub 检查完成或明早九点复查」是 `any`；「等两个已归属的子任务都结束」是 `all`。每个条件有自己的内部引用、登记水位和终态；`all` 的部分结果持久保存在绑定中，恢复时再写入原 Work 输入，重启后不能遗忘。`any` 命中后其余条件在同一状态变更中失效；已启动的外部执行是否取消是独立决定，不能因为不再等待就假设其副作用已停止。`all` 中一个成员失败/取消/不确定时，按显式失败策略向 Agent 交付该状态，不永远挂起。条件数量、筛选字段和等待时长受资源预算约束，默认值不能成为 90 秒一类的隐式有效期；长等待依靠持久订阅与定期完整性检查，而不是保持一次模型轮次存活。

`need_input` 保存具体问题与目标 Work；用户答复按可信入口关联到该问题，用户新消息也可以明确 steering、取消或另开任务。不能把「同一群里有人说话」视为对问题的答复，不能把超时视为批准。等待审批/授权时需要独立状态和批准凭据，普通群消息或插件事件不能替代。运行中的新输入在安全边界追加到原 Work，尚未提交的工具副作用先按回执对账；不能一收到新消息就打断已提交过程并重播。

### 5.2 持久绑定与交付

新增独立等待绑定表，最低字段：`binding_id`、原 `work_id`、canonical Conversation/generation、主体引用、组合模式、信号种类与规范化筛选、登记时来源水位、截止时间、状态、消费事件/自动化 run ID、创建/消费时间和唯一调用键。`work_id` 与主体、generation 在登记、匹配、入队和恢复时反复校验。一个 Work 同时只持有一个**活动等待集合**，集合内可有多个有界条件；更改集合先在原主体权限下撤销旧版本并原子建立新版本。既有子任务结果可作为集合成员，但旧 `run_id` 所有权校验必须原样保留。

登记绑定、释放当前激活、使原 Work 等待之间不能留丢信号的空窗。实现可采用同一短事务，或持久 pending 绑定 + 可补偿的提交协议；信号匹配从登记水位之后的**已提交内部事件**读取，不能仅靠内存 Hook。匹配成功时，在一个短事务中 CAS 消费绑定、为原 Work 写入有唯一 `source_key` 的 input，并把可继续的 Work 入队。消费者重复、进程重启或事件重投只得到同一输入；发送回执未知时不得借重新唤醒重复发送。

续跑只向原 journal 追加信号资料/可信引用，保留初始 brief、请求顺序和已耗预算；不刷新旧前缀中的时间或记忆。原 Work 已完成/取消、generation 已变、主体授权已撤销、插件 grant 已失效时，记录失效状态而不恢复。等待耗时不占用模型并发名额，但保留可观察的待办和取消入口。

等待状态需能区分 `waiting_result`、`waiting_signal`、`waiting_user`、`ready`、`resuming`、`cancelled`、`expired`、`invalidated`；状态名可按现有 Work schema 调整，但 API/日志至少暴露等待原因、原 Work ID、登记时间、下一次到期/核查时间、已满足条件与缺失条件、最近一次错误和取消入口。用户能从同一个 Work 查看、补充、修改或取消等待。Bot 重启后按持久绑定和来源游标重建待办；对于来源保留期已过、插件停用或结果未知，报告明确的失效/待核对原因，不假装一直正常等待。模型只在登记、信号交付或用户 steering 时运行，不为检查等待状态反复消耗推理。

时间等待复用现有自动化的日程/claim 基础设施，作为关联原 `binding_id` 的**一次性内部投递**，不启动新的 `yuki.agent` 步骤；如对用户展示为自动化，目录必须标明“继续原 Work”，且取消/暂停语义与绑定一致。不能以“定时任务运行成功”代替“原 Work 已继续/已交付”的证据。实施时可选择现有 automation row 或同一调度器中的专用 timer row，但不得新增第二套轮询时钟、租约和 misfire 规则。

### 5.3 插件监控复用

GitHub 等监控继续由插件完成联网、去重、条件判断和事件生成。Host 复用插件已持久化的 external event（内部 `source_event_id`、`plugin_id`、`event_key`、获准 target）；新增显式 opt-in 的“用于恢复匹配等待”路由。默认 `notifications.publish()`、`ask_agent` 与通知 outbox 行为不变。显式选择匹配时，同一事件若命中等待，优先续原 Work；未命中时是否开启新轮由插件原请求决定，不能暗中同时让两条主 Agent 链处理同一意图。直接通知文字仍按原 outbox 独立记账。

插件只提交已授权目标的事件，不指定任意 `work_id` 或读取 Work 私有状态；Host 按绑定找消费者并复核插件 grant 与原主体。现有 SDK `event.subscribe` Hook 是进程内观察通知，不承担持久等待匹配。此阶段不在自动化 DSL 中嵌入 GitHub API/通用轮询器；若插件监控覆盖不足，之后按具体来源扩展插件 Facade 或监控服务，不改变 Work 等待合同。

## 6. 改造顺序与交付门槛

### A. SELF 主体与普通自动化

1. 增加统一 `PrincipalRef` 与来源/场景构造、验证和序列化；替换自动化与 Work 中依赖空账号推断 SELF 的分支。审核所有把 `principal_kind=self` 强绑 `initiative_run_id` 的调用点。
2. 迁移自动化主体与唯一约束，回填用户任务；保留旧数据与运行记录。扩展 authority、repository、service、目录投影、管理权限、scheduled trigger、executor、capability registry/context 和工具 runtime；SELF 的 DSL 能力不能借 USER/SUPERUSER 身份通过。
3. SELF 自主轮获得真实的自动化工具执行许可；SELF 定时轮从独立来源进入同一主 Agent。更新动态轮次提示与工具说明，让 Agent 区分“新任务”“继续原 Work”“安静完成”。
4. 验证 SELF 群任务定时工作区整理、定时可选择发言/沉默、任务查询/取消、Bot 重启与路由失效；用户任务回归不能改变所有权或下次执行时间。

### B. 原 Work 的时间与消息等待

1. 增加等待绑定、原子登记/消费、Work input 与 scheduler 续跑路径；扩展 `task_control.wait`，保留子任务等待旧语义，并实现 `any/all`、deadline、失败/取消终态。
2. 接入自动化时间队列和 canonical 新消息事件。先仅支持当前 Conversation 和登记后事件，不做跨会话隐式匹配。
3. 将已有 `need_input` 与人工答复/steering/取消接入同一 Work 可观察入口；普通消息等待与明确答复不得混淆。
4. 验证登记/暂停竞态、到期/消息与取消竞态、`any/all` 部分完成、离线错过、重复事件、generation reset、原预算和 journal 保持。

### C. 插件事件接入

1. 在现有 external event 发布与持久 outbox 附近接入 opt-in 匹配，不要求插件自行保存 Work ID。
2. 更新 SDK 兼容版本、文档与测试 Fake；既有插件未 opt-in 时行为完全不变。用 GitHub 监控形状的隔离 fixture 验证真实 `plugin_id + event_key + target`，不把离线 fixture 称为线上监控验收。
3. 验证 grant 撤销、插件停止、重复/乱序事件、同一事件新轮与续原 Work 的互斥选择、未知投递回执和重启恢复。

## 7. 验收矩阵

| 场景 | 必须证明 |
| --- | --- |
| 旧任务迁移 | 用户任务 ID、owner、schedule、next run、run/step 游标、预算、回执不变；非法 owner 形状拒绝启动 |
| SELF 创建/管理 | 同一工具和调度器；SELF 只能管理自己，超管管理不改 owner；普通用户不能借 Yuki 名称管理 SELF |
| SELF 到点 | 原群可用则可读写工作区并自主决定发言；无发送时安静完成；有发送只认网关回执 |
| 身份变化 | QQ 账号/Presence 变化不让 SELF 变 Person；失效的场景、generation、授权或路由不能借别人的身份续跑 |
| 时间等待 | 原 Work ID、journal、根预算及已提交效果保留；内部 timer 不产生第二个 Agent 工作 |
| 组合与人工等待 | `any/all` 的部分满足与终态跨重启保留；明确问题只由可信答复关联；超时不变成批准；steering 不重播在途副作用 |
| 消息等待 | 仅登记后的真实内部事件可命中；消息正文、平台 ID、最后发言者身份不能授予 Work 权限 |
| 插件等待 | 插件 grant、目标与事件键核验；重复事件只追加一次原 Work 输入；旧通知默认行为不变 |
| 故障注入 | 登记与暂停之间、事件入账与匹配之间、匹配与 Work 入队之间、发送结果未知后重启均不丢信号、不盲目重做 |

验证至少覆盖 Alembic 升级与约束、跨入口固定工具声明、两种主体的权限矩阵、Work 状态/租约和 Provider journal 续跑、插件 SDK 合同及回执幂等。使用定向测试、真实序列化输入对照和隔离集成测试；实际 QQ 发言与真实 GitHub 监控效果另记线上验收，不由合成数据代替。实现完成后同步改写现行架构文档和操作说明，删除与新主体/等待合同冲突的旧表述。

## 8. 不纳入本任务的扩展

- 不把独立参与控制器改造成任务调度器或消息发送者；它仍只决定自然自主机会。
- 不提供跨群、跨私人会话的 SELF 待办漫游，也不把 SELF 当超级管理员。
- 不做任意用户编写的监控代码、无界条件表达式或通用 HTTP Webhook 接入。
- 不以新提示词、新 Memory 事实、`short_state` 或插件 KV 代替自动化/Work 的持久所有权。
- 不把多 Yuki 共库作为当前数据库模型的一部分。
