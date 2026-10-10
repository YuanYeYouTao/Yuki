# Yuki Work 内核删减与 Pi durable 对照重构任务书

日期：2026-10-10。实施记录更新至2026-10-11。状态：用户已授权实施，三名 gpt-6.1-sol/max 在隔离工作树并行修改，已经完成的项逐行标记。核心修改已合并并上线；逐入口终审继续补查原完成事实的外层消费，真实 QQ 验收进行中，未触发或失败的场景如实保留，详见末尾交付记录。

## 0. 最高开发约束与本轮范围

**禁止防御性编程。能通过删除或放宽条件解决的问题，不新增替代机制。** 注重可扩展性和可维护性，不把当前实现和经验门槛写死，为未来变化留下必要空间。

- 先删多余门槛，再整理已有内核。禁止重建完成评分器、语义分类器、汇报协调器或无限兜底循环。
- 不把删除改写成“先满足新的步骤才能继续”：无需先分类产出、先补齐笔记、先单独取消所有孩子或先证明全部历史效果成功。复用原执行事实即可，不以新开关、允许名单或重试框架替代旧门槛。
- 事实如实保留：失败仍失败、unknown 仍 unknown，部分成果可以保留，不能要求全部记录成功才准结束。
- 任务树可以先完善，子 Agent 扩展随后接入；不硬编码层数、子数、必须先有完整计划或强制派生。Yuki 已有可用子 Agent，本轮不是从零造它。
- **重点是内核。WebUI 将在下个大版本删除，本轮不建设新页面、树面板、图订阅或前端重构。** 内核字段变更只对尚在使用的接口/客户端作必要适配，不提前执行整个 WebUI 删除。
- 不因学习 Pi 新建另一套 Task 引擎、调度器、数据库、Registry 或身份系统。新增字段/代码须说明真实消费者、删掉什么旧链、为什么现有结构不足。
- **沿用现有上下文冻结机制。** 公共投影/Observation、Work 私有 journal、业务续跑与未决协议恢复已经分工；任务树扩展使用这些入口，不另造 context coordinator、全量 Work 快照层或共享私有协议池。
- 删除贯穿运行时、提示、工具合同、状态消费者、存储、测试与 CI；不得把 Python 中删掉的限制移回文档或测试。
- 每项真正完成才在索引标记，并写代码、验证及交付证据。阅读源码不等于测试通过，研究完成不等于修复完成。

先读[全景盘点](Yuki-Work现状全量盘点-2026-10-10.md)，再读[开发约束](development-contract.md)、[主 Agent](main-agent-runtime.md)、[持久工作者](persistent-subagents.md)、[输出边界](provider-output-boundary.md)。本任务书提出目标变更，实施时修改冲突现行合同，不并列保留两套相反规则。

## 1. 基线与方法

| 对象 | 固定证据 |
| --- | --- |
| Yuki main | `4c52889855d13c66dae4e3ba68e36eff1be00e3e`，前轮核对远端，本轮复核本地固定审查基线 |
| Yuki 审查目录 | `C:/Users/bymay/.codex/worktrees/gemini-search-bridge/Yuki-QQbot` |
| 文档目录 | `D:/Code/My_Github_Repo/Yuki-QQbot/docs/architecture`；此工作区 HEAD `25cd6083` 较旧，保留原有未提交修改，不用其源码冒充当前 main |
| Pi 官方本地副本 | `D:/Code/My_Github_Repo/pi`；官方 origin，完整工作文件的浅克隆 |
| Pi main | `c5f5b3282d5e4203c085e59837ba17aeaf2829b5`，前轮与远端一致，本轮按该固定提交终审 |
| Pi 核心比对 | durable tree `6f57be387b68c3068b97e8facec24c8149a940f6`；durable/chord/ai 与先前 `42a3497d` 一致，补充发现不是上游新增 |
| 核验范围 | 实现、类型、原测试和示例；三名 gpt-6.1-sol/high 分别复核生命周期、Pi/树/存储、工具/提示/消费者。前轮 Yuki 定向测试 38 例通过，本轮源码终审，Pi 测试未运行；详见全景盘点 §§16–18 |
| 生产证据 | 前轮 21:00:21 快照：Work/subagents 开启，Code 关闭，保留 3 个 child；详见全景盘点 §2。本轮不重复在线验收 |

后文 Yuki runtime/services 等索引相对 `src/qq_ai_bot`；Pi 索引相对 `packages/durable`。研究章节行号固定于以上提交，实施索引已更新为当前函数名。Pi README 明示 Experimental；不把“架构看起来稳”当作本项目验收。

## 2. 双侧对应后的设计结论

两边基本骨架相似，优先修改 Yuki 已有实现，而不是再包装一层。

| Pi 机制 | Yuki 已有落点 | 本轮采用方式 |
| --- | --- | --- |
| TaskRuntime / scheduler 的每阶段执行能力 | YukiRuntime、Runner、TurnExecution、WorkSession 与原 Repository | 对应执行宿主和持久边界；不把 TaskRuntime 误当业务 Work，也不新增同名外壳 |
| runtime.context / committed cutoff / 不可变 Entry | MainAgentTurnService、FrozenFragments、Observation/Selection、WorkJournal 与 ProtocolStore | 已有冻结主链复用；扩展只发布实际可见的新事实，私有协议按原 Work 恢复 |
| Submission / inbox / boundary | runtime_work_inputs、source_key、steer、handoff | 复用持久输入，明确本轮观察与下一轮输入；不新增 inbox，不拿 requestId 代替内部事件 |
| generation / tool phase | TurnExecution、WorkSession、invocation、journal | 复用准备→执行→结果→配对；Pi generation Task 不等于业务 Work 或 Conversation.generation |
| ToolTask intent/result | 原 effect_key 与 Social/Sandbox 回执 | 工具错误交回模型；删除历史 unknown 对整个目标的完成否决，同一效果不重放 |
| decided outcome / completing | accepted_control + 原结算 writer | 把候选改成清楚的已接纳决定，执行收拢不再反复审批目标；不复制第二套 TaskState |
| ownership / join / abort | Work 关系、child lease、原 run/effect、等待 | 业务父子与执行归属分开；取消先持久撤权，再收拢原执行，不等待所有后代业务成功 |
| Session commit/publication | SQL writer、journal 保存、通知 revision | 已提交结果是事实，观察与辅助报告不能改写它；不引进全局 Session 队列 |
| usage | Work/automation 原预算 | 保留累计计量，默认无限已实现，不重新加入收尾预留或次数门槛 |
| TaskGraph | 原查询和管理接口 | 可派生查看归属，不持久化第二套图、不做新 WebUI |

必须纠正旧稿的四个等号：Task≠业务 Work，Submission.done≠目标全部成功，held outcome≠当前 accepted_control，Pi background≠Yuki 暂停或后台 worker。Pi 泛型 Task 可以承载业务流程，但默认树混合模型、工具、压缩、自定义执行和 owned Conversation；不能把全部节点塞进 runtime_work。

Pi 关键参考：`src/types.ts:269、294、460、497`；`src/harness/inbox.ts:46、67`；`generation.ts:517、611、668`；`tool.ts:150、543`；`scheduler.ts:244、594、714、1126、1183`；`src/session/session.ts:508–592`。完整双侧索引及反例见全景盘点 §§12–14。

### 2.1 已有冻结主链不重构

Pi `scheduler.ts:1193、1326` 提供每阶段 runtime 和上下文读取；`generation.ts:166–208` 先提交模型、选项与 cutoff，再从该边界组装请求。它不是给每个业务目标复制整份群史。Yuki 当前也不是把几个 Work 的历史不断拼接：公共投影按可信会话、actor、read scope 和投影范围冻结；每个 Work 的工具配对、Provider opaque、原调用与压缩恢复资料留在私有 journal。

Yuki 同一公共投影代次已经保留首次实际选取的片段，只追加新事件/Observation；已配对业务续跑接当前获准聊天与任务材料；未决协议先恢复原检查点，不能用当前聊天改写旧请求。对应 `services/main_agent_turns.py:198、438、466、552、599`、`conversation/frozen_fragments.py:87、133`、`runtime/work_session.py:117、400、1469`、`services/turn_execution.py:352`。普通聊天的提交现已按 `COMPOSED_INITIAL` 判断（`services/chat.py:1422–1428`），旧的“只要存在 control 就跳过公共投影”问题不能再次列为当前缺陷。

任务树只需让新节点的笔记、实际输入和结果沿原 Observation/Selection 与 journal 发布/恢复。不得以 parent/root 相同为由互通所有 actor/read scope，也不把其他 Work 私有工具尾部注入公共前缀。Pi 的 `beforeRequest` hook 会在 request 执行时重新运行，不能把其 cutoff 称作整份 HTTP payload 的字节冻结；Yuki 已有协议级原请求测试，继续保留。

前轮 38 个既有案例提供冻结、交错和恢复证据，不证明本任务书中的收尾改造已经实现。新增删除点集中在外围辅助政策及原函数内减重复，见 D22–D31；具体证据见全景盘点 §§16–18。业务树是独立扩展，不作为这些局部删减的前置框架。

## 3. 目标内核行为

### 3.1 完成决定和真实结果分开

显式 complete 与合法无工具 final 进入同一结束路径。completed 仍表示 Agent 对业务 Work 提交完成，不是任意 activation 退出；fail/cancel 与完成不同。当前 answer 已允许空 final、无发送，本轮不重复实现。

删除 output_kind/artifact_ids/state_fact 的通用完成审批和全局 unresolved 否决；连同 accept、独立 Work 接纳、Repository 和 child start 对产出类型的强制分类一起删除。无独立消费者的 output_kind/deliver_artifacts 字段删除，有真实描述用途的资料不再拥有接纳或结束否决权。goal、产物、交付及状态变化供模型和调用方理解，真实工具仍保存原结果；不再用规则模拟目标验收。文件成功、caption unknown 可以同时存在，Agent 可以据此结束；查询不得把 unknown 改成成功。

复用 accepted_control 保存 complete/fail/cancel 决定，唯一 writer 发布实际状态和 sync_result。决定接纳后只进行原效果配对、输入归属和所属执行收拢，不为“证明完成”再买模型。段结束、辅助异常、旧通知和后台状态不能自动撤销决定；真实新授权输入或显式修改按输入边界处理。

当前 TurnExecution 在恢复、配对后和额度结束时已有 accepted decision 早返回入口（375–378、1440–1444、1504–1505）。直接复用；修改 writer 对任何非 running 状态都消费 accepted_control 的逻辑（work_repository.py:587–592）：等待所属执行收拢期间保留决定，真正发布终态后才消费。不得先把决定删掉，再启动模型重新生成同一决定。

Pi 的 completing 是 outcome 已定、不再执行父 phase；Yuki 当前候选会被重新判定。这是要修改的差异，不是已有等价机制。是否需要新字段由原数据能否表达决定；不预建 completing 枚举、结束决定表或 finish coordinator。

### 3.2 send_message 是特殊的显式工具

模型正文用于内部结果；QQ 发送必须显式调用 send_message。发送成功不代表 Work 完成，发送失败也不替 Agent 判定目标失败。普通聊天无 Work 仍可发送，不强制 accept，不因发送补建 Work。

“特殊”指原社交授权、路由、内部事件及实际投递回执，不是额外状态机。开始通报、阶段说明、steer 答复由原模型循环决定；删除送达后才允许业务执行的锁、漏发纠正轮和固定暂停模板播报链。完全无法运行模型时的原管理反馈逐入口核对，不按“固定文字”批量误删。

删除 work_report 及其专用阶段/答复分类链，不再持久化 stage_feedback_batch、input_feedback_through_id 或维护“这一批算不算汇报”的哈希。真人输入仍经原 take_inputs 给模型，发送结果仍经原 Social/effect 回执返回，进度与追问的沟通要求放在简洁提示中。已提交原调用参数里的退役可选元数据由既有恢复读取边界忽略，不改写原参数、重发调用或恢复新工具声明。

普通单次写操作也不由 Work 开关强制任务化。删除 Runner 的“除 send_message 外所有副作用必须先 accept”笼统拒绝，沿现有领域执行授权、原调用 ID 和回执工作。确实使用 WorkSession 的 Code VM、持久派生等继续沿其原执行入口；不补建隐藏 Work，也不另做工具分类允许名单。

同批 send→complete 按原调用顺序执行、逐条配对；结束接纳后不执行后面的新业务调用，为未执行调用保留明确结果。前面已经发送不能回滚，后面控制失败不能造成重发。删除 memory_change 阻止整批 send 的特殊轮次政策。

Pi 默认收齐工具轮，所有 slot 都要求 terminate 才结束。上述顺序和显式发送是 Yuki 的适配，不声称原样复制 Pi。

### 3.3 任务树先完善，执行归属继续复用

业务 Work 父子边表达实际子目标，直接父与预算根独立；执行归属仍用原 Work→effect/run、调用 parent_effect 和来源 owner。原 worker 关系迁入共同 Work 关系，worker 表保留执行者资料。不得把 Pi owned transcript 映射成新的 Person/Space/canonical Conversation。

树的内核先提供实际创建/关联、直接子与祖先/子树查询、指定节点取消、直接父通知、预算根计量和原身份恢复/归档。沿既有 accept/start/Repository 与模型控制入口作最小扩展，使主 Agent 能使用业务分解；不只添字段和展示接口，也不要求先开递归 worker。计划说明继续用 goal/context_note，未接受执行的待办不制造 queued Work。

原 `_queue_work` 是新用户请求交接，不是通用子目标创建器：它要求新 trigger，并使用 event source key；SELF 根又有 initiative run 唯一键。原目标拆解沿现有 child start 的 parent/source/持久调用 key 扩展，继承原获准目标，不要求用户再发一条消息，不拿最近观察事件充当新授权。原独立请求交接继续保留自己的输入来源，不与派生目标混成一个 accept 分支。

无须新 DAG、多父节点、节点类型注册表或通用 scheduler；不得把现有 worker 的工具限制写成未来树只能一层。反过来，仅删 recursive_subagent_forbidden 也不等于多层完成。

结束收拢的范围是选中 Work 真正拥有且尚未停止的执行，不是共享预算的兄弟、历史 unknown 或全部未完成目标。complete 接纳后，真实在途执行沿原 run/回执收拢，父模型不再重复验证。fail/cancel 先持久撤权，停止新的效果；执行器已有取消能力的按原 run 请求停止，否则保留实际在途/未知事实与迟到回执。不等孩子业务自然成功，也不把“所有远端都确认停止”变成 Work 终止条件；Work 已取消不冒称外部效果已回滚。

拟议允许无实际活动 owned run 的 waiting_user/suspended 子目标保留未完成事实而不否决父业务完成；这不是 Pi 默认行为。Pi 会等待所有普通 live 后代，background 才是显式边界。实施不得用状态名猜外部执行已停，不给 child 伪造成功、自动 detach 或自行重新排队。

现有管理 cancel 已支持指定 Work 加一层 children，扩成真实子树并供模型入口复用；不能扩大为整个群的 hard cancel。明确继续 child 时保留原来源、关系与预算，只恢复实际需要执行的节点；不为预算或归属查询递归重开已完成祖先。删除原 reopen_parent/父必须运行的耦合，父业务确需继续时按明确输入处理。父终态清理不能顺带删掉仍需保留的暂停后代，复用现有归档按真实引用处理。

### 3.4 输入边界和恢复

模型已选取的真实输入属于当前执行，工具后边界接入 steer；结束边界原子结算已处理输入，未选取的新要求保留明确后续归属。辅助 child/run 通知只补事实或唤醒收拢，不能隐式撤销已接纳结束或复活暂停/终态根。

此区分贯穿工具派发前的 pending 检查、take_inputs 的决定退役、结算 writer，以及管理取消 child 写父通知的独立分支；不能只改通知生产者。已有 input.kind/source 描述来源，不新增语义分类器判断用户“是不是新要求”。

复用 input ID、ready/staged/consumed 和 source/handoff，不增加意图分类器，也不能仅删除 pending 查询而丢消息。Pi followUp 是下一 run 输入；Pi handoff 是上下文 reset，均不等于 Yuki 登记新业务 Work/授权。不得凭群聊任意一句话制造新目标或以假内部事件唤醒。

普通工具错误返回原循环。历史 unknown 不再全局阻止新调用、明确恢复或完成；原不确定 effect 本身仍只查证或按已有执行器能力续接，不取得重新派发资格。真正活动的 Sandbox run 与中断未知分开，不用新错误分类框架替代现有回执。

恢复按原 Work、source owner、call/run、journal 和预算进行。Pi phase 恢复可能再次调用外部服务，依赖外部幂等；不是通用 exactly-once。Yuki 已付费 response/paired 保存失败仅重备原结果，不能照搬重发模型请求。

等待统一沿已有 WorkWaitRepository：保留 run_id 简写时，将其归一到已有 owned_run conditions，删除“执行必须仍 pending 才能等待”的额外资格。已结束或 uncertain 的真实执行可以满足完成信号，结果仍如实返回。无需第三条等待路径。

明确 resume/改向若替代原一次性等待，同一原状态变更中撤销被替代绑定；无需用户先另发 cancel_wait。查询、辅助通知不取消等待；继续等原信号则走原 wait 唤醒。复用已有 wait 状态/cancel/writer，不把旧 timer 留在新执行后再次唤醒，也不隐式取消对应外部 run。

删除固定两次 BUSY 封口须区分三种事实：当前 activation 仍持有原响应时，确认原纯数据库写已回滚后重备同一发布；已经 durable 的 phase 按原 journal 恢复；进程退出前尚未 durable 的响应可能无法找回，只能如实退出、不自动重购。沿原取消、租约与真实存储错误退出，不能仅删 range(2) 后写无条件循环；不新增 spool、保存队列或第二套持久化来兑现不存在的保证。

### 3.5 状态与消费者

先删重复裁决和无消费字段，再合并可合并的状态；不按 Pi 枚举机械改名。queued/running 承担调度，wait 有真实唤醒来源，suspended 保存实际无法继续的原因，终态与领域效果分别可见。

无已接纳决定的普通无法恢复异常拟以 failed 记录本次失败，释放占位；资料尚存时允许明确原 ID 继续。这是 Yuki 目标，不是 Pi terminal Task 的能力。同步 WorkControl.resume、enqueue、管理、子任务、祖先重开、查询与归档，删除 failed→PAUSED 的语义混淆；已归档不得补造 checkpoint。

普通终态超过最近 128 项可能回收，七天 caller 保护不是所有 Work 的承诺；不为 failed 加无限保留。cancelled 不由定时器重开，辅助通知不自动重开 failed。SELF/automation/plugin/worker 继续由原 owner 消费状态，不能因统一树而吞并 run/step/Job 语义。

## 4. 原事故必须复现的行为

2026-10-10 19:52 数字生命研究所人物提示词任务：Work `a5f5aabd-6a7b-46f0-8dbf-6bad743766db`，文件成功交付，内部事件 `94477`、artifact `3bcb12f2-ef34-4c28-96ec-4636322c4ff7`；附言 Social 操作 `a1a4b167-70bb-4bb5-a97b-fc9969d8d49e` uncertain。随后模型准备 ValueError 的物理请求数为 0，具体来源尚未确证。

用户继续后两条解释已发，文件未重传，旧附言 unknown 仍挡 Work 完成。前轮只读状态 suspended/work_has_unresolved_execution，累计预算 null。改造应保留“文件成功、附言未知”的事实，允许 Agent 完成且原调用不重放。删除报错播报本身不能冒充问题解决。

## 5. 任务索引与漏删清单

编号沿用前稿；终审新增 R05、D28–D31，修订已有任务的遗漏入口与过强承诺。每项完成后在该行填写实际代码与验证证据；隔离回归和真实生产验收分别记录，前轮研究测试不代替改造后验收。

| 编号 | 状态 | 任务与删除/替换范围 | 主要代码索引（当前实现，以函数名定位） | 完成证据 |
| --- | --- | --- | --- | --- |
| R01 | [x] | 当前完成门槛、事故回执与 Pi 原始源码研究 | 全景盘点 §§11–15；本任务书 §§2–4 | 只读研究，未声称运行 Pi 测试 |
| R02 | [x] | 当前 Work 全量盘点及纠正单层边界假设 | [现状盘点](Yuki-Work现状全量盘点-2026-10-10.md) §§1–10 | 源码与生产只读核验；未执行实现/测试 |
| R03 | [x] | 同级 Pi 官方源码克隆、双侧内核映射与错误推论校正 | 全景盘点 §§11–15；Pi c5f5b328 | 源码/测试阅读；未运行 Pi 测试 |
| R04 | [x] | Pi runtime/context 与 Yuki 多 Work 冻结主链核验；补查外围硬门槛 | 全景盘点 §§16–17；本文 §2.1 | 既有测试 28 + 10 = 38 通过，无 skip；使用隔离数据库、模型传输及替代 VM，非线上验收 |
| R05 | [x] | 最后广泛逻辑审查：接纳/输入/等待/结束/继续、Pi 实现及 Yuki 原能力对照，纠正不可兑现或增加步骤的方案 | 全景盘点 §18；本文 §§3、5.3 | 固定双侧源码、消费者、测试/CI 阅读；三名 6.1-sol/high 复核，本轮未重复测试 |
| T01 | [x] | 完善所有 Work 共用的父子关系，直接父与预算根分离；迁移当前 worker 关系，删除重复关系来源 | runtime/work_schema_v1.py:22；work_tree.py::direct_children/descendants/ancestors/budget_root_id；work_budget.py::charge；migrations/versions/0105_work_tree_and_retired_completion_policy.py::upgrade | Work.parent_work_id唯一关系；祖先预算与0105升级、树/迁移/原SELF-person恢复29例通过（本地；线上另见V02） |
| T02 | [x] | 接入实际子目标创建、模型查询与指定节点管理；沿原 child start 的来源与调用键扩展，不把新用户请求交接用作目标拆解；不绑死 worker 类型，不建 WebUI | WorkRepository::accept_in_session/derive；WorkControl::_control；WorkQueries::_query/get/list；ControlWorkQueryAdapter::read_work | 原derive/get/list/指定fail/resume/cancel实际接通；真实孙节点操作、原调用重入与自身子树查询通过；没有新增WebUI（本地；线上另见V02） |
| T03 | [x] | 将通知、预算、执行归属、等待、完成证据、恢复与归档从单层查询迁到真实关系；复用原调度器和 writer | WorkRepository::_owned_execution_clause/commit_state/route_child_completion；SubagentRepository::finish/maintain；WorkWaitRepository::_observe_due/_deliver；SubagentScheduler::loop；ToolArtifactRepository::_protected | 真实树撤权/预算/直接父通知/GC/产物保留29例；最后孙Work/Sandbox等待及Automation claim不变11例；worker维护/cancel持续异常仍调度2例。分批有重叠，不累计（本地；线上另见V02） |
| T04 | [x] | 同步三层关系/子树取消/直接父通知/预算根/迁移恢复验证与现行合同；清理一层假设断言 | test_subagent_result_checkpoint.py::test_three_level_creation_queries_budget_and_direct_parent_recovery/test_paused_descendant_retains_terminal_ancestors_and_resumes_without_instruction/test_completed_nested_artifact_remains_readable_until_retained_tree_settles；test_scheduled_subagent_recovery.py::test_scheduled_child_runs_without_a_fabricated_message_or_initiative/test_self_child_runs_and_continues_after_parent_and_original_initiative_settle；test_migration_0105.py | 树/迁移/调度恢复29例，加2个既有SELF接纳/恢复例共31例通过；维护失败2参数已包含在29中（本地；线上另见V02） |
| D01 | [x] | 删除全局 unresolved 阻断 complete；删除 finished child 历史效果参与完成否决 | WorkControl::_control；WorkRepository::commit_state | Control/writer删全局unknown完成门槛；发送未知后完成、原回执保留已验证（本地；线上另见V02） |
| D02 | [x] | 删除 artifact_ids/交付子集/state_fact 完成审批，以及 accept/排队/child start 的 output_kind 强制分类、child acceptance 必填；清理无独立消费者的分类字段和装配 | WorkRepository::accept_in_session；SubagentRepository::start；subagent_tools.py；effect_outcomes.py::execution_evidence | accept/child/schema/消费者退役产出分类；0105迁移及最小goal接纳通过（本地；线上另见V02） |
| D03 | [x] | 删除 background_state 覆盖与 writer 重复审批；跨 activation 收拢时保留 accepted_control，终态发布后消费；复用已有免模型早返回 | runtime/work_supervisor.py::recover_failure/settle；WorkRepository::set_accepted_control/commit_state；TurnExecution::_accepted_result | accepted_control沿原writer收拢并保留决定；已接纳完成免模型及辅助失败测试通过（本地；线上另见V02） |
| D04 | [x] | direct/Code 取消整批生命周期独占，按原顺序配对，到结束停止后续业务调用 | AgentRunner::_execute_tool_batch_impl；codemode/driver.py::CodeModeDriver._dispatch_all/_dispatch_control/_settle | 原direct/Code逐条配对与结束屏障；真实pinned Code send→complete及停止后续调用通过（本地；线上另见V02） |
| D05 | [x] | 删除仅因声明原生工具而拒绝合法空 final 的分支 | TurnExecution::guard_native_response/settle_final | turn_execution删原生声明空final拒绝；native boundary案例通过（本地；线上另见V02） |
| D06 | [x] | 缺软压缩锚点不暂停仍能装入的请求；删除辅助整理错误升级链 | WorkSession::require_compaction_anchor/compact；TurnExecution::prepare_request | WorkSession软锚点缺失仍fit继续；容量及压缩回归通过（本地；线上另见V02） |
| D07 | [x] | 删除先通报/先写 note/副作用先 accept 的强制顺序、interactive 正文不能结束和“已结束工作一律不能恢复”的旧提示 | prompting/contracts.py::_CORE_START/_CORE_END；work_control.py::work_control_tools；subagent_tools.py::worker_prompt | contracts/worker提示删先通报、先accept与产出审批；direct生命周期验证通过（本地；线上另见V02） |
| D08 | [x] | 删除 runtime 固定暂停 notice 生成/调度/发送/激活链；核对其他固定错误播报及 issue #285 | WorkResumer::resume；WorkScheduler::dispatch_once；runtime/work_supervisor.py::recover_failure；services/processor.py::MessageProcessor._prepare_foreground（旧failure_status_text整helper删除）；migrations/versions/0105_work_tree_and_retired_completion_policy.py::upgrade | Work notice生成/调度/发送整链删除；普通processor六个异常固定发送出口及failure_status_text整helper删除，原日志/失败/真实send保留；相关既有15例通过（本地；线上另见V02） |
| D09 | [x] | send_message 分开传输与目标完成，文件/caption 原回执独立保真 | SocialService::_effect/_file_result；effect_outcomes.py::execution_evidence；InvocationService::invoke | Social原文件成功/附言未知分别保留；文件不重传、随后说明成功且完成案例通过（本地；线上另见V02） |
| D10 | [x] | 删除辅助通知无条件拒绝新工具、退役结束决定和唤醒暂停根的行为；真人 steer 沿原安全边界接入 | WorkControl::has_pending_business_inputs/take_inputs；MainAgentBackend::execute_call；WorkRepository::_business_input_clause/commit_state；SubagentRepository::finish；work_management.py::manage_work | pending/take_inputs/writer/producer/settle_final按原输入来源区分；晚到业务与辅助通知对照通过（本地；线上另见V02） |
| D11 | [x] | 主动 fail/cancel 复用指定树管理撤权及原执行器取消能力，补模型入口；删除先清空子任务/远端全部确认停止才结束的要求 | work_management.py::stop_owned_execution/manage_work；SubagentExecution::cancel_commands | 原stop_owned_execution撤权子树，cancel_commands沿原Sandbox run停止；迟到真实成功不改写（本地；线上另见V02） |
| D12 | [x] | 统一管理/模型恢复，删除整树 unknown/旧 notice/active wait 的恢复否决；显式继续替代旧等待时在原 writer 撤销绑定，无需先单独 cancel_wait | work_management.py::resume_blocker/manage_work；SubagentRepository::resume；WorkRepository::enqueue | 管理与模型resume删旧unknown否决；wait原绑定同次撤销、原ID恢复验证通过（本地；线上另见V02） |
| D13 | [x] | 沿 TurnTranscript→response→原 effect/result→paired→journal publication 核查并删除重复分支；保留已付费响应与原协议恢复，Pi 仅作局部算法参考 | TurnTranscript::request/append_result；WorkSession::restore/save；WorkJournal::load/save/effect_result；AgentRunner::_run_with_receipts | 原journal/ProtocolStore/call/effect/预算身份保留；协议恢复与原调用不重放通过（本地；线上另见V02） |
| D14 | [x] | 核对自动化、SELF、插件与子 Agent 的已有结果消费；当前开关不否决原来源，待处理只读不要求新上下文；删除无消费者的额外接纳上限 | DurableInvocations::read_result/run；MainAgentTurnService::read_result/run；plugin_host/main_turn.py::run_plugin_main_turn/_execute_plugin_main_turn；automation/handlers.py::AutomationCapabilityHandlers.agent；automation/executor.py::AutomationExecutor.execute；WorkResumer及原SELF/plugin_background/worker恢复入口 | 将原Durable只读分支机械搬到一个reader供三个入口复用，无新cache/状态；SDK、Person/SELF自动化真实waiting→关闭Work+capacity0→同ID只读，原Work/Wait不变且0新请求；真实queued续跑仍走完整准备。删除Executor外层当前开关封口和SDK固定8任务上限，原调用复用保留；最终SDK等26例、native/Person/SELF/scheduled69例分别通过、无skip，不累加重叠（本地；线上另见V02） |
| D15 | [x] | 精简无消费状态/字段；删 retained_tool_rounds 死清理；核对后删 journal ending 新写入及无人消费的旧提取复制；保留真实查询与恢复事实 | WorkSession::rebase_business/_unobserved_tool_round；WorkJournal::_load；WorkRepository的旧has_unresolved_effects与work_reporting模块删除 | 删work_reporting、communication专链、ending副本及无调用者has_unresolved_effects API；原回执/协议仍保留，Repo/runtime128例通过（本地；线上另见V02） |
| D16 | [x] | 改写或删除冻结旧门槛的测试，合并重复样本，清理 CI 已无入口的检查 | 本文§7；既有tests；.github/workflows/quality.yml、release.yml；scripts/verify_monty_packaging.py | 旧分类/拒绝/布局镜像与空方法删改；Code悬空进程入口改既有case内真实os._exit、错误fake复用原typed回执；删除packaging中固定0099编号断言，保留实际init-db、完整性/FK及head报告。Quality/Release逐job核查，未新增workflow。最终冻结SHA的CI见V01 |
| D17 | [x] | 修订现行开发合同、主 Agent、工作者、输出边界和用户说明；旧审查按日期保留 | 本文§8；现行开发合同、主Agent/worker/输出/工具/插件文档与README/3.9.0 | 开发约束/主Agent/worker/输出/工具/插件及README/3.9.0同步；当前函数名经AST复核，旧报告按日期保留；release_validate v3.9.0通过 |
| D18 | [x] | 检查当前迁移链和真实消费者，完成必要的数据结构升级与旧记录恢复；不凭状态批量改成功 | migrations/versions/0105_work_tree_and_retired_completion_policy.py::upgrade；schema_guard.py::require_canonical_schema；0056/0057原schema冻结声明 | 0105空库完整升级与seed0104升级通过；0056/0057仅冻结原schema声明，数据库历史不变（本地；线上另见V02） |
| D19 | [x] | 删除“同批有 memory_change 就不能 send_message”及 Code 强制 STOP_MEMORY 的特殊轮次政策 | AgentRunner::_execute_tool_batch_impl；codemode/driver.py::CodeModeDriver._dispatch_all；codemode/contract.py | 删memory_change/发送同批policy；真实pinned Code同脚本记忆与发送通过（本地；线上另见V02） |
| D20 | [x] | 修复暂停 child 的归档保留；child 继续不为读取来源/预算重开已结束祖先；资料尚存的 failed 可原 ID 明确继续，消除 failed/paused 混淆 | SubagentRepository::acquire/resume/maintain；WorkRepository::reclaim_terminal；conversation/self_initiative.py::validate_self_initiative；self_origin.py::resolve_self_origin | failed显式原ID继续，子继续不复活祖先，暂停后代不归档；真实SELF与scheduled恢复通过（本地；线上另见V02） |
| D21 | [x] | 删除固定两次 BUSY 封口；同 activation 持有原结果时重备原发布，已 durable phase 原身份恢复；未持久结果在进程退出后不承诺找回 | WorkSession::_save；WorkJournal::_load；WorkResumer::_recover_preparation_failure；SubagentExecution::run | 同activation可信BUSY重备原发布、沿租约退出；未持久响应丢失不自动重购案例通过（本地；线上另见V02） |
| D22 | [x] | 删除 work_report 及专用答复关联/已答未答分类；删除 communication_reports 与 input_feedback_through_id 专用消费，保留原输入观察和真实发送回执 | SocialService::_message_arguments；migrations/versions/0105_work_tree_and_retired_completion_policy.py::upgrade；旧work_report声明、验证、专用查询整链删除 | work_report退出声明与专用读写，原Social读取忽略已提交退役可选参数；验证不重发（本地；线上另见V02） |
| D23 | [x] | 删除阶段汇报结果分类、NO_REPLY 特判、kind 白名单、批次哈希与 stage_feedback_batch 状态；清理不可达 _given 兼容分支 | TurnExecution::settle_final/take_boundary_inputs；旧work_reporting与_given专链删除 | 删除阶段分类/哈希/答复水位与_given；原输入消费和恢复回归通过（本地；线上另见V02） |
| D24 | [x] | 删除可选笔记派生发布成为模型准备前置条件的耦合；原保存笔记沿现有发布入口重试，不另建队列或快照系统 | MainAgentTurnService::compose；work_context_note.py::publish_pending_note；WorkControl::update_context_note | 可选note发布ValueError/ProjectionConflict不阻断业务准备；原发布入口冲突案例通过（本地；线上另见V02） |
| D25 | [x] | 删除 complete 等生命周期动作仅因附带 context_note/reporting 而整次拒绝的政策；同步 schema、提示及旧断言 | WorkControl::_control/complete_final | complete可附可选note/reporting，省略或null结果沿统一结束；结算测试通过（本地；线上另见V02） |
| D26 | [x] | 删除外部 run_id 在 schema/等待/恢复中重复且不一致的 36/128/64 字符门槛；复用原字符串身份和所属执行查询 | work_control.py::work_control_tools；work_wait.py::normalize_conditions；WorkSession::restore | run_id重复长度门槛移除，原字符串身份保留；等待与协议回归通过（本地；线上另见V02） |
| D27 | [x] | 在原投影函数中删除无条件 fresh 候选构造和未换候选时重复 fit/hard-fit 计算；必要时再构建，复用已算结果 | history_projection.py::prepare_history | history_projection延迟fresh构造并复用fit结果；历史/压缩35例通过（本地；线上另见V02） |
| D28 | [x] | wait(run_id) 复用已有 owned_run conditions/WorkWaitRepository，删除独立 pending 资格和两套等待存储 | WorkControl::_control(wait)；WorkWaitRepository::register/_observe_due | run_id归一owned_run conditions；已完成与未知真实信号等待通过（本地；线上另见V02） |
| D29 | [x] | 删除“有副作用就必须先 accept”的 Runner 拒绝和配套提示，普通操作复用原领域调用/授权/回执，不自动补建 Work | AgentRunner::_execute_tool_batch_impl；InvocationService::invoke | 删普通副作用先accept；Work开关两种路径普通写操作通过，不补隐藏Work（本地；线上另见V02） |
| D30 | [x] | 删除终态/协议垃圾回收作为每轮业务调度前置条件的耦合；原循环内分离维护失败与新 Work 选择 | WorkScheduler::dispatch_once；SubagentScheduler::loop；WorkRepository::reclaim_terminal；ProtocolStore::cleanup | 根scheduler及worker scheduler均删除维护/取消查询错误阻断业务选择的耦合；真实连续错误仍派发正常原Work/worker，原31例通过（本地；线上另见V02） |
| D31 | [x] | 生命周期与通知正文解耦：删 worker need_input 的 reason 隐性必填/None.strip 异常，以及 child 明确 resume 对额外 instruction 的强制要求 | SubagentRepository::resume；subagent_tools.py::execute_subagent；WorkControl::_control | worker need_input可省略reason，child原目标resume不必补instruction；树恢复通过（本地；线上另见V02） |
| D32 | [x] | 逐条复核发现的漏删：删除一次 admin 失败关闭整个 Backend 后续能力及参数哈希单次写锁；原调用恢复复用真实ID和回执 | MainAgentBackend::execute_call/_is_mutating_call；旧_tools_closed/_mutation_identity/_completed_admin_mutations/_ADMIN_RETRYABLE_ERRORS删除；原领域operation回执 | 整链删除（Backend净删69行）；真实Admin失败→读取/修正→17→18→17新调用成功；原operation重入保持原change_id且不重做。原文件19例与最新pinned Code30例通过（本地；线上另见V02） |
| D33 | [x] | 删除Control历史读取对合法wait的重复8192字节/8条条件拒绝；不让展示reader阻断已登记事实读取 | persistence/control_work_query.py::_conditions；test_work_settlement_writer.py::test_existing_timer_can_resolve_all_conditions_without_delay_policy | 两条旧大小门槛及过期注释删除；复用原9/300等实际time_due登记场景读取Control wait history，不新增状态、参数或校验器；定向结果见§10.2 |
| D34 | [x] | 删除Manager将PTY未启动/旧APT无heartbeat按10/15秒猜成失败的分支；恢复原session，真实Supervisor回执裁决终态 | sandbox/persistent.py:480 reconcile/462 attach/503 no-session return/507 original attach；environment_supervisor.py:55 started O_EXCL；test_work_execution_receipt_regressions.py:182 | 源码净删12行；原case四参数PTY未started/已started、APT无/陈旧heartbeat分别RED→GREEN，最终Linux18例无skip。真实Supervisor证明原命令至多一次；APT真实status exit7由原finish/outbox落同ID，不依据年龄猜失败。Manager上线及QQstop独立见V02/V03 |
| D35 | [x] | 删除Social与OneBot适配器重叠的30秒超时；取消/未知仍按原回执处理 | social/service.py::SocialService._call/_dispatch_claimed；实际OneBot适配器API timeout | 只删Social外层计时，保留原适配器30秒与真实回执。7个已有发送/重放/并发案例通过；原历史样本TimeoutError结果及CancelledError传播通过，不宣称已修好SL Highway网络故障 |
| D36 | [x] | 删除持久Work被临时聊天coordinator_version重复否决的门槛及无调用者依赖；普通前台取消和真实generation/来源/租约仍沿原合同 | WorkResumer::_scene::validate/_resume_automation::validate；ChatService::validate_turn_snapshot/_run_agent::before_model_request/run_effect；ConversationEffectGate原锁；test_history_dispatch_ownership.py::test_real_work_restore_keeps_private_tail_out_of_ordinary_projection；test_work_owner_recovery.py::test_derived_automation_work_runs_on_original_scheduler_after_owner_settles | 真实snapshot/bind窗口普通群观察：原恢复1例、Person/SELF自动化3参数分别RED→GREEN，同Work/预算/来源且真实后续effect成功；普通无Work旧version拒绝、真实generation仍拒绝。删除Resumer两入口重复条件及无caller依赖；58个原边界案例通过，后续3例是其中子集，不累加 |
| V01 | [x] | 隔离回归：完成、发送未知、重启、取消、子任务、晚到输入与各 Provider 协议 | 本文 §7 | 本地末轮pinned Code89例与native/Person58例全部通过、无skip；Linux全量两条旧断言已修正且原文件9例通过。远端CI状态另记，不冒充全绿；用户明确要求不等待CI便合并部署，详见§10.2 |
| V02 | [ ] | 数字生命研究所真实 QQ 全链路：用户任务、自主触发、等待续跑与最终释放 | 本文 §7、§10.3 | 真人追加B、90秒等待、三个子目标、受控Host SELF安静结束、完成后独立C根与新文件成功、跨轮模型取消旧hds已有真实记录。旧B上传失败保留unknown；活动进程停止仍待最终Manager更新后补测 |
| V03 | [ ] | 逐行回看任务索引，终局反向审计及交付记录 | 本文 §9、§10 | 已逐行核对并补正外层/pending/Manager/coordinator遗漏，末轮交叉审查45条R/T/D函数索引无失配、未见新分类/上限/状态/付费重跑；PR286/287已合并，核心7f2f5664已上线0105。最后源码/原Manager模块交付与真实停止验收独立记录 |

### 5.1 实施前基线中容易漏掉的实际分支

以下保留4c528898审查时的实际遗漏线索；现行处理与验证见上表，不把这些旧门槛视作当前合同。


1. 前置效果查询包括 root 和全部直属孩子，writer 却只查当前 Work 的效果，两层范围不同。禁止只删 _prepare_completion 然后遗漏 writer。
2. fail 仍会被活子任务拒绝；接纳后也可能被 background_state 覆盖。fail 不是需要另通过完成审批的动作。
3. 明确只读的新 outcome 通常不算效果债，但畸形或历史回执会通过兼容路径重新变成完成围栏。删除完成围栏时同时核对历史投射，不新建版本猜测规则。
4. 旧 final_delivery 可被 state_fact 当成业务变更；这是类型推断失真。删除泛化 state_fact 完成审批即可，不再补工具名黑名单。
5. settle_final 的新输入提示仍说“上一段回复尚未发送”，内部 final 原本不承诺发送。删除此假定，不加正文分类。
6. 管理 resume 比模型 resume 更严格，旧暂停通知自身 unknown 也会挡恢复。恢复入口不能要求先证明所有历史发送确定。
7. 源码已允许 answer 安静结束，也已不强制交付全部草稿；不能把这两项旧问题重复写成待删代码。
8. 自动化现行实现主要消费 Work 状态和 sync_result；文档仍写 completed 加全部目标交付核验，不能据此凭空新增“完整交付检查器”。
9. memory_change 的批次特殊处理会连“先发说明再写记忆”也拒绝；按原顺序执行、把真实写入结果交给模型即可，不另设观察/发送专用轮。
10. 现有 notice 有专门激活和 suspended 候选选择，不能只删模板函数。旧 planned/blocked 通知退役而不补发；accepted/dispatching/unknown 保留原事实和防重放作用。管理命令及模型完全不可用时的现有状态反馈逐入口审查，不按“固定文字”批量误删。
11. work_control 对 complete 附带 context_note/reporting 的额外拒绝、result 可空但显式 null 被拒等参数分支一起核对。按统一工具 schema 处理真实参数，不再加一套仅用于拒绝收尾的校验器。
12. 本次未找到“重复读无进展到固定次数就失败”的残留；24/32 是分段额度，min(attempts,3) 是退避延迟封顶，分页数量不是任务接纳上限，不把这些误写为已证实的卡死门。
13. `work_report.kind` 已允许任意字符串，阶段汇报却只认 progress/reply/final；删除后者及冗余派生状态，不反向收紧 schema。真实新输入 ID、原 send 回执仍有消费者，不能把它们连同汇报提醒一起误删。
14. 可选笔记发布冲突和真实请求来源失效不同：前者不应把正常 Work 卡在组装前，后者按实际授权和内容处理。不能通过捕获全部异常、伪造发布成功或跳过原来源检查解决 D24。

### 5.2 全局未知效果与单次调用恢复

基线审查覆盖了 complete、services/invocation_service.py:161–168、管理 resume、Code driver 及当时所有 has_unresolved_effects 调用；实施已删除该死API与全局否决，单次原调用恢复沿真实ID/回执。

删除“无关历史效果未知，因此禁止新调用/恢复/完成”的全局政策。原 call/effect/run 已经 dispatch 但结果未知时，同一调用只能查证、保留中断结果或按执行器真实恢复能力续接，不能重新取得执行资格。使用现有身份和执行器能力表达，禁止新增语义相似度去判断是不是重复操作。

尚在真实运行的命令与已经中断但结果未知不同；前者可沿原 run 等待或取消，后者不能被当作永远运行的任务。不得为让状态好看而伪造终端完成回执。

### 5.3 终审后的方案边界：删门槛，不改造为新审批

| 检查点 | 现有能力和最小变化 | 不能重新写回的限制 |
| --- | --- | --- |
| 新建与拆解 | 原目标、来源、调用键和 child start 足以承载真实子目标；独立新请求仍走原输入交接 | 强制产出分类、新用户消息才能拆解、完整计划才能开工、为每个普通写操作补建 Work |
| 输入接入 | 原 input.kind/source、take_inputs、dispatch 边界消费；辅助事实与真人 steer 分开 | 任何 pending 通知都拒绝业务工具、任何通知都撤销结束决定 |
| 等待与继续 | 统一已有 owned_run/time/event 等条件，明确继续时同次撤销被替代的旧绑定 | 已结束执行不能 wait、resume 前必须额外 cancel_wait、继续 child 必须复活全部祖先 |
| 已接纳结束 | 复用 accepted_control、TurnExecution 已有早返回、原 writer 和 owned execution 收拢 | background 覆盖决定、等待期间提前消费决定、再买模型验收已决定目标 |
| fail/cancel | 原撤权先落盘；原执行器能取消才请求取消，迟到/unknown 回执继续保真 | 先证明所有外部都停止、所有子目标都结束或全部历史 unknown 清零 |
| 响应保存 | 当前内存结果原发布重备；已提交 phase 原 journal 恢复；未持久且丢失则如实退出 | 固定次数封口、无条件循环、承诺无记录也能恢复、为此新增 spool |
| 辅助链 | 删除汇报分类/标记；可选 note 发布、垃圾回收不成为新模型请求/业务调度的前置条件 | 辅助失败升级为全 Work 停止、重建协调器或维护重试服务 |

这些是现有能力上的修改范围，不新增一个运行时规则引擎。代码存在某个 if、Pi 也有某项校验、旧测试要求某个错误码，都不能独立证明它应保留；反过来，分页/分段数量不等于语义拒绝，未证实的线上因果不写成已复现事故。

## 6. 实施顺序与范围

1. 依据全景盘点 §§11–14 确定概念和真实消费者；不要先实现一个 Pi 同名类再寻找用途。
2. 删除接纳/完成审批、汇报锁和重复决定（D01–D10、D19、D22–D31）；同步原断言，统一 explicit/final/fail/cancel 结束路径。保留 §2.1 已验证的冻结与私有恢复主链；D27 只在原函数中减重复，等待与恢复相邻入口一起改。
3. 完善业务树地基 T01–T04，与 D11–D13、D20、D21 一起对齐执行收拢、直接父通知、预算根、恢复和归档。树先于递归 worker 能力开放；局部门槛删减不等待整个树实现。D21 按真实持久状态验证，不能只删除重试计数便标完成。
4. 清理消费者、无用存储、文档、测试/CI（D14–D18）。WebUI 仅必要接口字段适配，不新增功能，也不在本轮提前砍整个客户端。
5. 隔离验证、真实群验收、回看未执行项和终局审查（V01–V03）。主路径通过不能替代所有来源验证。

可翻译的是边界与局部算法：Pi inbox 的选取/结算，工具 intent/result，不丢已定 outcome 的收拢，持久取消后的底向上清理，提交后通知。翻译时删掉被替换旧分支，不加包装层。

不移植：Registry 热替换/动态缩工具、Chord 文档库、混合 Task/Conversation 新身份、图订阅、新线程调度体系、failFast 默认策略、background 产品开关、固定重试/压缩阈值、任何“无进展次数达到就失败”的门槛。Pi storage 故障结束 Session 不适合照搬为 Yuki 任意保存错误终止全部运行。

## 7. 测试与真实验收


### 7.1 改现有测试，不为每条 if 再写一份测试

| 当前文件/入口 | 处理方向 |
| --- | --- |
| tests/unit/test_work_execution_receipt_regressions.py:101、123、204 | 分开真实活跃执行和已中断 unknown；删除 unknown 必须阻断 completion 的断言，保留原调用不重复执行 |
| tests/unit/test_work_settlement_writer.py:73、91、127、262 | 晚到输入必须有后续归属；删除无条件撤销决定与 state_change 成功效果审批预期 |
| tests/unit/test_work_delivery_ownership.py:283 | 增加文件成功、caption uncertain、后续已发送解释后能结束；断言文件只上传一次，未知附言保持未知 |
| tests/unit/test_provider_native_result_boundaries.py:362 | 合法 completed 空响应走正常收尾；实际原生执行未结束和 paid opaque 恢复单独按协议验证 |
| tests/unit/test_work_protocol_continuity.py:998 | 仍能装入请求时缺软 anchor 不挂起；不拿缺失原请求内容的损坏情况替代软整理 |
| tests/unit/test_subagent_result_checkpoint.py:234 | 删除 state_fact 完成审批断言；验证真实结果、子任务状态和父通知消费 |
| 现有 direct/Code 工具批次测试 | 同批 send→complete、业务失败→complete、complete→后续调用不执行、每个 call 都配对 |
| 现有 automation/SELF/plugin 测试 | 内部结果与 QQ 发送分别断言；不用正文、完成状态或健康检查冒充交付 |
| test_history_dispatch_ownership.py、test_history_time_projection.py、test_codemode_interleaved_recovery.py | 沿用公共前缀冻结、普通聊天/多 Work 交错、原私有协议恢复样本，不重写另一套冻结测试框架 |
| test_work_settlement_writer.py:354、现有控制/发送案例 | 改掉可选汇报元数据否决合法发送、complete 附带可选字段即拒绝的预期；实际 Social 授权与回执分别验证 |
| 现有 context_note 发布/协议准备案例 | 补一个笔记发布竞争不阻断正常请求且不伪造可见笔记的样本；不制造重试服务 |
| test_work_settlement_writer.py:546 与既有 wait/resume 案例 | wait 简写与 conditions 使用同一事实；执行先结束再登记仍可返回；显式继续后旧 timer 到达不重复唤醒 |
| 现有普通工具/worker 生命周期案例 | Work 开关不改变普通单次写操作的可执行性；worker need_input 无 reason 仍能等待，不虚构正文 |
| test_work_owner_recovery.py 与原 scheduler 案例 | 回收持续失败仍能选择正常 queued Work；维护的真实错误保留，不启动第二个调度器 |

保留少量能暴露真实断点的链路场景，删除只证明旧拒绝码、字数/次数、分类或实现布局的测试。不要删掉唯一证明无重复发送、原调用恢复和输入不丢的案例。

隔离验收至少覆盖：

- 普通聊天、SELF、自动化、插件 caller、子 Agent 的 explicit complete、空/非空 final、fail、cancel。
- 未接纳 Work 的普通聊天发送、单次记忆/状态写入后继续或内部 final；Work 功能开关不制造强制 accept、补建任务或补发。实际依赖 WorkSession 的能力仍按其原入口验收。
- 一个工具确定失败、一个工具 unknown、可选子任务取消/暂停、产物草稿未发，Agent 仍可据真实结果结束。
- send_message 前准备失败、网关调用后失联、文件与附言部分成功；重复恢复不新增发送。
- complete 接纳前后、外部效果返回后/回执写入前、response 保存后/paired 前、取消提交前后的进程中断。
- 活子任务结束、子任务需用户输入、父 fail/cancel、迟到子回执、明确继续原 child，不重置身份与预算。
- 父任务结束后的维护不删除仍暂停的 child；child 明确继续不为预算查询复活已完成父；failed 明确继续沿原 ID；任一晚到通知不能自行重开 failed/cancelled。
- 结算与真人新输入、未就绪附件准备、child 通知竞争；输入只处理一次，旧完成不被辅助通知撤销。
- 原生工具声明但未调用的空 final，以及真实 pause_turn/未知传输，不能混为同类。
- 软压缩失败但仍 fit、真正超硬容量、有限预算耗尽、段结束续跑和已接纳结束免额外请求。
- response/paired 保存连续遇到可确认回滚的 BUSY 时只重备仍持有的原结果；取消、租约失效或不可恢复存储故障时退出。另测“已 durable 后重启”和“未 durable 前进程退出”，后者不能假装恢复或自动重购。
- child/run 辅助通知到达不拒绝无关新工具，管理取消 child 不复活 waiting_user/suspended 父；原真人 steer 仍实际被模型观察。
- wait(run_id) 与 conditions 对已结束执行一致；显式继续替代等待后旧信号迟到不重复生效；worker 无说明 need_input 与根的生命周期语义一致。
- 维护持续异常时正常根任务仍可调度；实际损坏的某 Work 仍报告自身事实，不把清理错误泛化为全局不可工作。

Pi 可按行为翻译的测试线索（同级目录 c5f5b328 的 packages/durable/test，未在本轮执行）：harness-tasks-recovery.test.ts:112、388（意图/结果后恢复）；harness-tools-recovery.test.ts:109、136（unsafe 与 replay）；harness-submissions.test.ts:82、178（原请求身份）；harness-cancellation-barrier.test.ts:39、190（取消与已决定结果）；harness-inbox.test.ts:203、404、431（输入边界）；harness-storage-failure.test.ts:291、328（提交不确定和监听失败）；harness-compaction.test.ts:560、1691（摘要边界）。翻译行为，不复制其类层次与模型重购策略。

CI 当前 Quality 包含 ruff、mypy、前端 test/build、pytest；Release 包含标签、镜像与部署包验证。删除相关失效断言与重复任务，不借 Work 重构删掉无关构建验证；不增加源码字符串扫描、文件数量门槛或多套重复生命周期测试作“防回归”。

Code 测试的 requires_worker 在缺少 pinned worker 时会 skip（tests/support/codemode_cases.py:33）；当前 Quality 未专门构建 worker。D04/D19 改到 Code 时，沿现有构建方式准备对应 worker，记录实际执行数、skip 及原因，运行对应集成场景；Quality 全绿不能替代这部分验收。无需为此永久新增一套 CI gate，也不把 Code 装进默认 direct 生产构建。

### 7.2 数字生命研究所真实 QQ 验收

目标群：数字生命研究所，平台群号 1049765710。用户核验账号 2186567848。实施时重新解析可信 Conversation/Space 与内部事件，不把平台号当内部事件 ID。

用户已提出该群全链路验收；到实施阶段沿同一原 Bot 进行可辨识、低干扰的测试。不得为测试停止 SnowLuma、故意断开生产网关或运行第二个 Bot。断网与崩溃样本在隔离网关/数据库完成，线上验证恢复后的实际行为。

| 场景 | 线上必须取得的证据 |
| --- | --- |
| 用户长任务 | 接纳、模型按需通报、实际工具执行、发送产物、主动结束；Work 终态与真实文件事件分别记录 |
| 自动轮自主创建 Work | 从真实 SELF/自动化入口触发，记录原来源、创建、执行及结束；允许安静完成，不强求为了验收发言 |
| 中途追问/改向 | 原输入入账并被模型观察，原循环用 send_message 回应后继续；无强制开始/阶段通报锁 |
| 等待与自动续跑 | 真实时间或执行信号释放名额后唤醒同一 Work，最终能结束，无额外自造事件 |
| 子任务 | 子结果回到父任务，父任务交付；失败或暂停的非必需子任务不把已结束决定变永久悬挂 |
| 用户要求终止 | 指定 Work 撤权、可取消的所属执行收到停止请求；其他会话继续，未确认停止及迟到回执如实保留 |
| 完成后的新要求 | 旧结果与新输入归属清楚，不把旧任务无声反复重开 |

每例只记录必要的内部 Work/event/run ID、创建/等待/恢复/结束时间、模型和工具请求数量、发送回执、最终状态及未决效果；不保存完整群聊正文或凭据。
没有实际触发的场景标“未验收”，不得根据 healthy、源码或合成响应勾选 V02。自然模型表现与执行机制分别记录。

## 8. 数据、现行文档与接口

从真实消费方反查 output_kind、deliver_artifacts、state_fact、communication/work_report、notice deliveries、accepted_control 与恢复状态：展示字段不能继续拥有完成审批权，无消费者则连同 SQL、DTO、提示与断言删除。树关系删除重复事实源；不长期并存三份可变 parent/root。

实施前读取最新 Alembic head 和实际生产版本，新增必要冻结迁移，不改变历史迁移已发布的数据库行为、不预编编号。0056/0057 原先导入可变运行时 TABLES，实施时只冻结其原 schema 声明，避免当前字段改动反过来改变空库升级；现有数据库在0105升级。保留原 Work、输入、call/run、效果与预算身份；旧 unknown 不批量改成功，旧任务不按年龄自动判断完成。

现行文档随实现同步：development-contract.md、main-agent-runtime.md、persistent-subagents.md、provider-output-boundary.md、tool-kernel.md、tool-results.md、chat-media-workspace.md，以及自动化/插件与用户说明。删除“必须先逐个取消孩子”“固定收尾预留”“全部交付审批”等与新行为冲突或已过期的文字；保留显式发送、来源授权与真实回执。日期盘点保留基线，不作为永远适用的合同。

Control/API 中有实际模型、SDK或管理消费者的能力继续随内核正确工作；不因为 WebUI 退役计划就一并误删。前端只做当下必要适配，不新增树导航、布局、组件或订阅。下个大版本删除 WebUI 属于后续范围。

直接翻译 Pi 实质代码时记录 `c5f5b328`、原文件及 MIT 许可（Mario Zechner 2025）；仅研究不添加运行依赖。独立 Pi 仓库保留为后续源码参考，不复制整个包进入 Yuki。

## 9. 终局审查与交付

逐入口反查：真人/SELF/automation/plugin/worker → 输入 → 模型 → 工具/发送 → journal → writer → 收拢/恢复 → 原调用方消费。

- unknown、工具失败、空 final、缺通报、草稿未发、暂停 child 是否仍被某层当成全局完成否决？
- 已接纳决定是否又被 background_state、段限制、cleanup、晚到通知覆盖，或多买一次模型/多发一次消息？
- 任务树是否仍混淆直接父、预算根、授权 owner、历史 parent 和外部执行？是否只添字段，实际创建/查询/取消未接通？
- 原输入是否不丢不重，原效果是否不重放，真正活动的执行是否仍能按原 ID 收拢？
- 单层假设、旧审批是否移到了 Code、SQL、测试、提示、管理入口？是否为 Pi 概念新增无消费者框架？
- 多 Work 的公共冻结前缀是否仍按原实际选取边界追加、私有协议是否仍按原 Work 隔离？有没有因为学习 Pi 新造上下文层？可选笔记/汇报是否又成了执行前置门槛？
- 接纳是否还强制产出分类/普通写操作先 accept？wait 是否仍有 pending 资格双轨？继续 child 是否无故重开祖先？是否声称能恢复仅在内存且已丢失的响应？
- T01–T04、D01–D36、V01–V03 是否逐行完成？没有实际跑的场景保持未验收；WebUI 不是新建设验收项。

实施结束分别记录净删增行数、定向回归与 skip、CI、本地构建、PR、合并 SHA、部署镜像、迁移版本和真实群结果。净删行数不是唯一正确性证明。

用户已授权执行本任务书，并按既定流程办理 PR、合并、本地构建及 Bot-only 生产部署；不动 SnowLuma。每一步以实际记录为准，不把实现或本地验证当作上线完成。

## 10. 实施、逐条复核与交付记录

本地实现、定向验证及逐项复核已有记录。PR286/287已经合并，核心7f2f5664已上线；合并后的逐入口审查又发现AutomationExecutor外层仍以当前开关拒绝消费旧cursor，继续补正并准备最终镜像。用户已明确要求不等待远端全量CI，定向验证后直接合并部署；不把未结束的CI或未触发的QQ场景写成通过。

### 10.1 逐条复核额外发现并补正的遗漏

| 实际遗漏 | 处理与验证 |
| --- | --- |
| 普通子目标 terminal 原调用重入被当作新接纳拒绝 | WorkRepository.accept_in_session 读取原调用结果，不重开终态子目标；独立根新接纳仍沿原身份 |
| 主 Backend 两个派发入口仍把辅助通知当作新业务输入 | execute_call/invoke_binding 改为原 has_pending_business_inputs；实际读取、工作区删除与晚到子通知竞争案例通过 |
| 完成信号提前绕过尚未满足的 all 等待 | 原普通子结果和 Sandbox 完成通知保留原 active wait，组合 run+timer 的实际案例通过 |
| wait 孙节点仍被直属关系拒绝，Sandbox 查询只认当前节点 | 沿原descendants统一真实拥有子树；孙Work与后代Sandbox等待的原8个参数通过 |
| 旧wait信号直接清空Automation owner claim | 重复owner写入整段删除；原3个真实Automation场景确认整个Automation行不变，各执行者沿原claim继续 |
| 多层树的隐私删除触发 parent RESTRICT | 原隐私事务先解除被清退会话内部的父边，再删除原树；真实清退案例通过 |
| 终态父因为后代引用永不释放大资料，或暂停后代被父归档牵连 | 原 GC 保留必要关系墓碑、回收终态详情；真实130条终态与仍暂停后代案例通过 |
| 直属父结束后，仍活跃祖先下的深层工具产物过期 | 原 _protected 复用 ancestors(include_self=True)，保留不扩读取授权；实际读取、清理和无关树对照通过 |
| 自动化/后台来源仍只接 child，原 root 明确继续没有消费者 | 原 WorkScheduler/Resumer 交回原入口，使用原 Work/run/Job/source；原 run/Job 不重开；真实入口案例通过 |
| worker 能 derive 孙节点，却不能查询或指定管理它 | WorkQueries复用原descendants自身子树；实际get/list/孙节点fail→resume→cancel通过，原祖先/兄弟未获授权，归档节点仍可读安全事实 |
| 一条权限提升测试被 Settings 缓存遮蔽，未形成真实权限变化 | 改为真实活动账号绑定并直接核验当前权限；原委托上限仍保留。实际降权暴露的原执行权限遗漏已沿 Handler 原回调补正，管理员配置未被写入、原 ID/预算不变 |
| admin 单次错误关闭整个 Backend 后续能力，以及同参数被当成已执行调用 | D32整链删除；原调用防重只沿真实ID和回执，合法17→18→17实际通过，最新pinned Code30例无skip |
| 根调度放宽后，worker仍被维护和取消查询失败阻断 | 原SubagentScheduler.loop分开处理这两项已有辅助工作；实际连续两轮错误时SELF/person worker仍执行完成，原31例通过 |
| 同步completed缺正文仍重入接纳，后续循环又请求模型；SDK已完成读取先要求新上下文 | Durable删除正文资格门槛；真实空/缺/null同ID读取。SDK复用原来源验证与Durable读取，实际3例及外围47例通过；自动化同类读取接回唯一原返回出口，最终native44+Person自动化14共58例通过；无新cache，分批重叠不累加 |
| Work notice删掉后，普通异常仍固定发送QQ失败文案 | issue285普通processor六异常固定发送与死文案整helper删除；SELF/自动化无其他此类广播消费者，原15例及Runner58消费者通过，不建立新分类/替代播报 |
| 文档仍承诺暂停 notice、通用产物验收或旧固定暂停原因 | 删除冲突说明，按实际结束决定、独立效果查询和原来源消费者更新；本轮current函数索引纠正了类名缩写与旧consumer措辞 |
| 构建packaging检查仍固定要求迁移0099 | 删除这条已过期编号断言，保留原init-db实际执行、数据库完整性/FK检查及实际head报告；0105空库/旧库升级由原迁移场景验证。重新提交并以新head复核CI |
| 已登记多条件wait正常运行，Control读历史却因大于8条或8192字节报STATE_MISMATCH | D33删除reader两条重复门槛及过期注释，原JSON读取与安全字段继续复用；原多条件timer场景同时验证真实Control读回 |
| D14模式切换仍有外层与pending漏点 | 原Durable只读分支搬到共享read_result，SDK/Auto不为旧等待构造新上下文；真实新进展走完整backend。Executor删除关闭Work时全拒agent脚本的6行；SDK删除固定8任务准入上限。原owner、原Work/Wait和累计预算不重置；26例及69例验证分别通过 |
| 真实sleep300未开始就被判execution_interrupted | D34删除按PTY running=false/10秒和APT无heartbeat/15秒猜失败；复用原session和started标记，不新建PTY或重复命令。真实监督进程终态与容器代际继续提供事实 |
| Social外层30秒计时与网关适配器重复 | D35纯删除外层asyncio.timeout；B文件仍是SL Highway连接失败和uncertain，不转成功或自动重发。C新文件真实发送成功独立验收 |
| 合法原Work被背景恢复早期snapshot的临时协调版本拒绝 | D36沿现有持久Work authority处理，删除Resumer重复版本校验；Chat同一validator放开已持久Work的coordinator条件，普通前台和真实generation检查仍在，同一效果锁仍线性化重置 |

### 10.2 验证与行数

本地完整Linux运行1381 passed、55 pinned-worker缺失skip、2 failed（两条旧LOCKED挂起断言）；按真实失败状态修正原断言后该文件9例通过。最终pinned Code89例全部通过、0 failed、0 skip，覆盖全量缺worker的55例及其余Code消费者；原有30例是其中子集。最终native44+Person自动化14共58例通过、0 skip；SDK3例与插件外围47例分别通过，不把重叠结果累加成全量总数。PR的Quality按原工作流继续执行；用户明确要求不等待CI便合并部署，不把前次有失败或当前未结束的运行记作全绿。

末轮ruff、Linux平台mypy（644个源码文件）、release_validate v3.9.0、diff检查通过。前端代码未改；原18例及构建已验证，全量Quality仍按原工作流复核。未创建版本标签或GitHub Release。实际运行记录保留在本地.cache日志，不提交临时测试镜像、凭据或完整生产数据。

D33复用原timer登记/Control读取场景10个参数全部通过、0skip（7.85秒）；该源码Linux mypy、两文件Ruff/format及diff检查通过，未新增case或fixture。

D14模式切换额外验证：原SDK3例加原关闭模式消费者4例共7例通过、0skip；最终native/Person58例复跑全部通过、0skip（45.61秒），不与前次58累加。SDK改前真实失败在assemble_plugin，Auto改前真实失败在assemble_automation；改后读取相同原结果，不写原Work、不请求模型。对应3个源码Linux mypy、Ruff/format和diff通过。

最后pending/Executor补正：SDK/owner/mode-off入口26个唯一案例通过，native44/Person14/SELF4/scheduled7合计69个唯一案例通过、0skip（78.87秒），包括此前58/3/4的相关子集，不重复累加。Sandbox原receipt文件17+原FD案例1共18通过、0skip（14.99秒），四个新增参数实际RED→GREEN，真实监督进程防重不靠源码扫描；Linux Mypy该模块、Ruff/format/diff通过。Social原发送/重放/并发7案例通过、原历史TimeoutError及取消传播样本另执行通过。

末轮Linux全src Mypy644和全库Ruff lint通过；全库format查出本轮遗留6处纯排版差异并用现有formatter修正（条件/函数参数换行与空行），不增加逻辑或重跑无关全量测试。D36冻结后全库Ruff与format（875文件）、对应3源码LinuxMypy、release_validate及diff全部通过。

2026-10-11 最终源码差异，相对4c528898、启用Git重命名识别：生产src新增1773行、删除2428行，净删655行；迁移新增217行、删除4行；二者合计新增1990行、删除2432行，净删442行。构建脚本另删1行。测试新增3762行、删除648行；实际迁移、进程中断和原消费者链路样本与生产代码分别统计，不把测试增加隐藏在净删数中。PR286初次交付生产净删635行、PR287净删636行均为中间基线，不能替代此次全部补正。文档和README另计。

### 10.3 生产与真实 QQ

PR286：https://github.com/YuanYeYouTao/Yuki/pull/286 ，UTC2026-10-10 17:24:12合并，main `7f2f5664c6aa9737746515b29035e07262f70a04`。以此SHA本地构建direct镜像并离线验证无Monty、Code默认关闭；空库0105预检integrity/FK通过，没有启动Bot或调用模型。

正式镜像 `ghcr.io/yuanyeyoutao/yuki-qqbot:ops-7f2f5664`，image ID `sha256:31a40809e19a3a998f7d18b8c9ccb1fb55ba361c535feda3a2e8e89295ba3c31`。归档267800576 bytes，上传SHA256 `2f11077fd53dbe75e68a4f171a0acca22f4867e66fbfa0fabd017aa7300304e7`。沿真实Compose全部叠加文件，只替换Bot；部署时曾保存0104一致性SQLite及配置/工作区备份，生产已经升级0105。UTC2026-10-10 18:34:46按用户最新明确要求，全部四处备份及废弃临时文件已删除，现无部署备份可恢复；不把旧备份位置写成仍存在。

部署时全库integrity/FK扫描耗时过长，造成额外停机；确认备份完成、迁移已到0105后，停止本次部署进程多余的全库复查，改核受影响Work/worker表外键，0错误，启动Bot。没有停止迁移或恢复旧数据库。Bot于UTC17:37:06.559137启动，随后health HTTP200、database ok、OneBot连接正常、direct固定74工具；SnowLuma容器ID `cbdabcdf7c2fff6763d3688b10f606b26c6be06ffce98cfba8bccd2032850d89` 和StartedAt `2026-10-09T05:10:47.99332112Z` 完全未变。

PR287：https://github.com/YuanYeYouTao/Yuki/pull/287 ，UTC17:53:22合并，main `201acdeb8392f02e1a80b157ab99fff27617e9df`，补正SDK/自动化已完成读取的模式切换。该SHA的direct镜像构建、离线封装和上传曾完成，但未部署；上传的中间归档已随临时文件清理删除。最终合并外层/pending与真实终端遗漏后构建新SHA一次更新，不把中间构建记为线上版本。

以下线上事实均来自当前唯一Bot的7f2f5664，群数字生命研究所，可信Conversation `5b234414-7537-4f1f-8f27-d03c2c0949c7`、generation20；用户为2186567848。除受控Host接纳机会外只读核验，未伪造真人事件或启动第二个Bot。

| 实际场景 | 原身份与结果 | 验收范围 |
| --- | --- | --- |
| 真人长任务及改向 | Event94923→Work `e508f663-c4ca-45c1-a95d-c60e6c13d02a`；追加B的Event94925→同Work input51，最终consumed | 同Work实际接入新要求；父最终completed，无新建伪造事件 |
| 真实子目标与预算 | 三个child `42af61c5-55e7-4315-b2ab-bde407cdd7dd`、`b9d46937-404f-4fbe-ab5a-2c5711775668`、`4d63f9ec-4839-44b3-88bc-09763f5e6a3f` 全completed；原父e508；全树模型17+3+4+4=28、工具6+2+3+3=14，等于原shared budget | 三个实际直属子目标，不冒称线上三层树；返回inputs50/52/54已consumed |
| 90秒等待同ID续跑 | 原wait `fc456d99-8fcb-4f61-8777-d8916678e75a`，UTC17:48:04.819814登记，17:49:34.819814到期，17:49:36.453648交付input53 | 原父恢复并结束，timer input consumed；无额外测试事件 |
| B产物与文件交付 | 原artifact `9a2a0f3b-f491-4a0d-8501-8bb229b7bc5c`，13 bytes、revision1、SHA256 `70ad8ef0eabfb62adda977e5c3c47cbf89ad30ef14d0b287367ae741033baa92`，实际manifest一致 | B产物确实存在；QQ文件交付未通过，不能混作成功 |
| 文件传输失败后仍结束 | Social `d40145fe-ca93-4050-9f17-5532c1c46761`，UTC17:51:46.565439派发→17:52:16.607670 TimeoutError/uncertain，无内部Event。SL在17:52:18明确报upload_group_file Highway TCP connect timeout、三次上传失败；用户最新确认未收到。随后说明text→Event94927 succeeded | 保留unknown、不重发；父及三child仍completed，journal paired/pending0/引用缺失0，scope无租约 |
| 自主轮受控Host接纳 | 原proposal `work-kernel-self-20261011-7f2f5664-01`→run `25345010-1b22-488b-a043-070c98b2cf00`，UTC17:59:03.338814接纳→SELF Work `4e4cb139-30b3-4432-849e-a5167358c3a3`，17:59:04.877102创建 | 当前唯一Bot原outbox执行；principal self、actor空、owner semantic/epoch1、intrinsic、sources/support0；非自然概率唤醒 |
| SELF安静完成与释放 | 原SELF Work UTC18:00:13.850796 completed，journal paired/pending0/1200引用无缺失；累计2模型/0工具/0发送；原run UTC18:00:15.759361 no_reply/feedback seq4，执行租约0 | 安静结束合法，无QQ发送；同原来源、ID和预算，未买额外收尾模型 |
| 跨轮主动取消旧挂起目标 | 用户2186567848的真人Event95050→turn `569c7b72894f47afb6bbf3fcd04d5743`，原call_50436 task_control.cancel指定旧hds Work `a5f5aabd-6a7b-46f0-8dbf-6bad743766db`；实际trace170674 executed=true/ok=true/state=cancelled/rev10，目标UTC18:45:10.180146更新 | 真实模型控制能够结束另一个旧Work，CLI只读未重复取消；旧文件成功/附言unknown保持事实。该目标当时无真正活动终端，不代替进程停止验收 |
| 完成后独立新请求及C文件 | Event94938→新Work `fa2f7bf5-8c5c-4dd3-9c29-0ad38f76ce03`，UTC18:07:11.848创建→18:18:08.624 completed；e508未复活。新Social `375b33fd-01bc-44c5-8885-52f9e8dee43a` succeeded→Event94980 file，18:16:27.124入账 | 新请求使用新Work；C文件真实网关成功，不能倒写旧B unknown |
| sleep300及主动停止 | child `69376f07-2869-42e6-b98e-4c76cfea61c3` 的原Sandbox run `f69f763f-a386-45a3-bd08-2716f05d1f7e` 在WS握手超时后尚无Supervisor started/status，Manager用10秒推断failed/execution_interrupted；child随后completed，main没有登记300秒wait | 停止仍未验收，不能对已结束树发停止当作有效测试；D34修正后再等真实进程启动核对用户停止 |

故障资源旁证：UTC18:04 Bot约305MiB、SL约315MiB，Host MemAvailable约653.5MiB，双方restarts0/OOMKilled=false/cgroup OOM计数0；17:50–17:54内核无OOM记录、无网关WS断连重连，之后普通文本发送成功。当前RSS不等于故障瞬时完整采样或内存泄露验收；这里只能支持现有证据指向QQ文件上传连接失败。

C主链14个真实HTTP200的transport合计173.716秒、最长45.835秒，另两次work_turn_changed与两次取消均physical_attempts=0；journal的dispatched时间不能冒充211秒HTTP挂起。普通实际tool_start为写文件2/发布3/发送2，子任务及wait/get控制另外记录；多轮模型/准备和Host压力分别分析，不归为单一SL内存泄漏。

清理回执 `/opt/yuki-qqbot/ops/cleanup-all-backups-temp-20261011.json`：264个目标，实际释放7,936,364,544 bytes，可用19,299,426,304 bytes；四处备份不存在，活跃Compose文件/挂载仍在，Bot/SL ID和StartedAt未变。UTC18:17换页si最高1656KiB/s、I/O wait 8–14%；清理后18:45可用内存572MiB、swap987MiB，4个新秒si16/292/116/8、so0、wa2/5/1/3%，压力减轻但未证明内存泄漏或其已修复。

UTC18:46:18一次cgroup分解：SL resident charge455.22MiB中anon109.00/file305.01/kernel40.42、swap598.10；Bot359.14中anon295.88/file54.21/kernel8.57、swap134.09。文件缓存和换页会改变容器headline，RSS升降均不足以证明堆泄漏/释放。Host available654.82MiB、swap1028.04MiB、memory PSI10 some0.39/full0.32、IO2.16/1.73，无OOM/restart；Bot/SL无独立memory/CPU hard limit，共享Host1612MiB。当前证据支持确有资源竞争、后来压力减轻，不独断为SL泄漏。
