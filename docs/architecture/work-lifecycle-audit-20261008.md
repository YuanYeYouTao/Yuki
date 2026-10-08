# Yuki Work 全链路审查与 Pi 对照（2026-10-08）

## 接手原则

注重可扩展性和可维护性，不把代码和流程写死，为未来变化留出必要空间。局部成果和未完成状态可以按真实需求保留，要有清楚的继续或退出方式。

见[共同开发约束的开篇原则](development-contract.md#0-设计原则)。

## 结论

Work 的问题集中在**失败后怎样退出、谁负责恢复、模型结束怎样进入业务收尾**。不是所有挂起都应自动判完成，也不能用延长重试、扩大容量或删除历史记录修复。已确认的执行事实、未知效果、原任务身份、预算、来源与晚到输入保护应继续保留。

**Yuki 能主动完成 Work，不能概括为“Agent 无法主动结束”。** 普通根 Work 的显式 complete、合法无工具 final、SELF 的静默结束均有持久 completed 出口；但主动 fail 没有及时退出循环，段末甚至会把失败提议覆盖成 queued。

当前已确认 W01–W09：挂起任务的退出/恢复 owner 不闭合；完成候选与证据核验不对称；fail、等待与完成后的停止边界不可靠。追加审查还证明内部草稿被强行纳入交付、隐式完成拒绝原因被吞。旧的强制开始通报与负数 QQ 消息 ID 问题属于真实历史故障，当前代码已经修改，但旧任务没有自动得到新的生命周期处置。

最近两轮终止专项的问题索引如下；详细路径、源码位置与验证记录均在对应条目。

| 编号 | 问题 | 当前状态 |
| --- | --- | --- |
| W05 | fail 后继续执行，段末覆盖成 queued | 已复现，未修复 |
| W06 | 失租后原执行重入遗忘等待，时间未到即发送并完成 | 已复现，未修复 |
| W07 | complete 后的内部结果阶段仍可新增业务发送 | 已复现，未修复 |
| W08 | 隐式完成强制要求交付内部草稿 | 已复现，未修复 |
| W09 | 隐式完成被拒，具体原因被吞成通用暂停 | 已复现，未修复 |
| D02 | 暂停与等待信号叠加后的恢复政策不明确 | 状态已验证，政策待明确 |
| D03 | 旧模块文档仍要求已删除的正文纠正轮 | 文档冲突已确认，待更新 |

本报告是审查与修复建议，没有修改产品代码、恢复或重发生产任务，也没有部署。按用户要求，已在本报告、共同开发约束和 AGENTS 的开头补入简短设计原则；文档要求不等于源码已经修复。此前用户授权取消的 14 个旧自主任务已取消，保留原任务、失败和管理回执；这次清理只解除那一次容量阻塞。

## 基线、方法与证据边界

- 当前源码：`f4f483d70c3fdb0302b2bece9f6220971ef3f955`。本地 `e62fd1258e929730657b240821727abc7197447f`、远端 main `dd20c89a525c3e69570d38ddcf1ba9f13fda7a3a` 只有文档差异，已核对相关源码树相同。
- 生产镜像：`sha256:82ed61b72b0231fd43f933f79467deef5778ae7efa08324789d48e15897478bb`，默认 direct。只读采样于北京时间 **2026-10-08 14:06:48**；Bot healthy、OneBot 已连接、无重启或 OOM。
- 生产 SQLite 使用 `mode=ro`、`query_only` 和同一读取快照，读取任务与回执元数据，不启动第二个 Bot、模型或控制器。内部 Work ID 是调查和管理对象，平台消息 ID 只用于传输证据。
- 三个新建子智能体均为 **gpt-6.1-sol / high**，分别检查挂起恢复、完成交付及 Pi。先前低推理设置的审查已中断，没有继续使用旧 Astra 子智能体。新审查继承事实线索，重新核对源码证据。
- 主动结束复核另新建两个 **gpt-6.1-sol / high** 子智能体，分别追踪入口和执行隔离 SQLite 探针；root 复核源码、测试日志与持久状态。没有调用旧智能体。
- 开放状态与终止细节追加审查再新建三个 **gpt-6.1-sol / high** 子智能体，分别检查控制批次/恢复、开放继续入口、官方 Pi 代码细节；root 独立检查隐式完成和产物集合。均只使用隔离样本，没有新增线上探测。
- 本报告依据明确列出的源码、现行合同、生产记录和定向验证，不声称覆盖未核查的系统分支。
- Pi 指官方 `earendil-works/pi` 的 Durable 实现，固定源码版本在对照节列明；Yuki 的 `codex/pi-codemode-experiment` 是另一个 Yuki 实验分支，不能当成官方 Pi。

## 生命周期中需要分开的事实

| 层次 | 真实含义 | 不应替代的结论 |
| --- | --- | --- |
| Work `queued/running` | 排队或取得某次执行机会 | 不证明模型已调用或业务已发生 |
| journal `dispatched/response/paired` | 原协议请求及工具往返的检查点 | 不等于 Work 完成状态 |
| 工具/Social 回执 | 原调用的确定成功、失败、待处理或未知 | 单条发送不证明整个目标完成 |
| `waiting_external/waiting_user` | 存在合法等待条件或缺少用户输入 | 释放执行租约不等于退出持久任务接纳计数 |
| `suspended` | 当前处理不能继续，保留原状态 | 不是终态；不会单凭时间或重新联网自动消失 |
| `completed/failed/cancelled` | 目标完成、终结失败或明确取消 | 不能抹去先前已发生或尚未知的效果 |

## 已确认问题

### 主动结束能力：完成与失败必须分别核对

以下 completed/failed/queued 均指 owner 退出后的真实 SQLite 状态，不以工具返回的 ending_proposed 代替终态。模型响应和 QQ 网关均为本地受控测试替身，没有真实 QQ 发送。

| 入口 | 核查结果 | 本轮探针模型请求 / 合成发送 |
| --- | --- | --- |
| 非 caller 根 Work，合法 final 发送后显式 complete | completed；complete 成功后不再请求模型 | 2 / 1 |
| 非 caller 根 Work，合法 final 发送后正常无工具 final | completed；final 调用共同完成核验 | 2 / 1 |
| caller，发送后显式 complete，再返回空或非空内部结果 | 两例均 completed；complete 后仍请求一次内部结果 | 各 3 / 1 |
| SELF，合法无工具 NO_REPLY | 可静默 completed；读取既有真实入口测试的数据库断言，本轮未重跑 | 不计入新增探针 |
| caller / 非 caller，fail 后下一轮正常 final | 两例均 failed；fail 本身未立即停止循环 | 各 2 / 0 |
| caller / 非 caller，fail 恰好位于本段最后一个请求 | 两例均 queued，失败提议被覆盖（W05） | 各 1 / 0 |
| caller，fail 后继续 send_message，再正常 final | failed，但 fail 后仍新增一次成功发送（W05） | 3 / 1 |

caller 显式 complete 后取得内部模型结果，是 `main-agent-runtime.md:167–175` 的明文合同：工具提议不能冒充调用结果，原完成条件与回执仍须复核。对目标已外发的简单任务，这会多一次模型请求，可讨论按明确返回合同优化；它与 W03 的隐式空 final 闭环分别处理。不能直接拿任意成功 send 免除业务完成核验。

child 也存在真实 completed 出口：`services/subagent_execution.py:247–253` 先 settle，再写子任务结果；`subagent_repository.py:531–567` 从 Work 行读取真实状态。显式 complete 后可能仍需内部结果轮，不能仅凭子任务正文判断其已完成。本轮读源码和既有测试，未新增 child 执行样本。

### W01：未执行的短暂 SELF 机会，重试耗尽后永久占容量（P1）

`activation_outcome.py:200–206` 将原 Presence 无可用连接识别为 `gateway_disconnected / retryable / not_sent`。`work_supervisor.py:154–173` 对相同错误累计次数，前三次以 2/10/30 秒退避重排，第 4 次进入 `suspended`。正常 SELF 使用 `return_to_caller`，不会建立暂停通知；调度器通常不会再选择它，SELF 恢复入口也跳过挂起状态。

`work_repository.py:45,343–358` 的非终态计数仍包含它们，限制为全局 128、单会话 16；`reclaim_terminal():1938` 只回收终态。网关重新连接不会释放这些接纳名额。

**生产实证：** 14 个被清理的任务，模型请求、工具调用和发送计数全为 0，没有 effects 或 delivery intents；均在第 4 次断连处理后暂停。它们加两个旧任务占满会话 16 个名额，持续触发 `WorkCapacityError`。取消后新的任务得以执行，后续已完成。这里的“占容量”指持久 Work 接纳计数，不代表 14 个模型或线程同时运行。

**最小修复方向：** 区分短暂自主机会和长期承诺。对真实未派发、无已承诺/未决效果、无任务输入或子任务的 SELF 机会，重试耗尽后按原 ID 终结为失败或明确过期，而不是永久挂起；必须保留失败事实。已有执行/承诺的长期目标仍走原挂起恢复，不能批量按年龄删除或改成完成。只提高 16/128 会推迟下一次积累。

### W02：插件外层 Job 结束，内层 Work 却留在不可恢复的挂起状态（P1）

插件后台源使用 `owner=plugin_background / origin=plugin_background / delivery_contract=return_to_caller`。`plugin_host/background_turns.py:369–373` 在 Work 挂起时调用 `fail_turn`；`notification_repository.py:821–839` 最后把外层 Job 标成 `failed`。重复尝试读取同一原 Work 的挂起结果，不能靠再次投递同一 Job 解除它。

普通 Work 调度器只处理其支持的 owner/origin；`services/work_resume.py:84–90` 未覆盖该插件后台来源。`work_management.py:177–186` 也不接受它的管理 resume。外层队列不再工作，内层仍是非终态并占容量；错误处理有 owner，但持久任务的最终退出与合法恢复责任没有闭合。

**生产实证：** Work `ae6fcdea-9706-4ae0-879f-40118f3cd64c` 仍为 `suspended / LLMEmptyResponseError`；原 source event `90780` 对应插件 Job `573` 已 `failed`，attempts/max_attempts=3/3，错误 `runtime_work_blocked`。Job 的汇总模型/工具字段为 0，Work 则记录模型请求 3 次、工具 1 次、发送 1 次。失败路径未完成汇总，所以 Job 的 0 不能被解释为没有模型调用或费用。

**最小修复方向：** 由原插件执行 owner 在同一原执行身份下负责重试、暂停、完成和最终退出；管理恢复应委托该 owner 或明确返回可用的处置入口。外层队列终结时不能留下没有任何消费者的非终态 Work，也不能通过创建新事件/新 Work 重发已成功消息。

### W03：已确认发送的调用方 Work，空内部 final 到不了隐式完成核验（P1）

`services/turn_execution.py:823–858` 的空响应处理，仅在没有 Work 或已经有 `ending=completed` 时允许确认发送后返回空内部结果。`1519–1535` 的正常 final 路径，先检查空正文，之后才调用共同 `complete_internal`。`services/turn_execution.py:415–463` 的调用方完成复核又要求已经提出 completed。

因此顺序形成闭环：需要完成提议才能容忍空 final，但无显式 complete 的空 final 在产生隐式完成提议前就报错。显式 complete 后的空结果有保护和恢复路径；send 后的空 final 尚未经过同样的完成核验。

**生产实证与复现边界：** 上述插件目标为“自然说一句真实反应”。它已通过 `send_message` 成功发送，真实 Social 事件为 `90792`，没有待处理或未知发送；后续空模型响应使 Work 挂起。生产记录证明发送后暂停，不能独立证明模型已提出目标完成，也不能把异常空响应与正常可用空 final 混为同一机制。正常空 final 到不了隐式核验由源码和隔离探针另行确认。该来源是 `return_to_caller`，不要求 `work_report.kind=final`，所以不能把缺少 final 标签当成此任务原因。

**最小修复方向：** 在可信模型结束/调用方结果结算边界，先形成并复核原目标的完成候选，再决定内部结果是否允许为空；复用现有未决效果、未完成子任务、并发新输入和真实交付核验。传输空响应、Provider 故障、进度发送或任意 send 成功均不能直接授权完成。已有目标完成候选与确认交付成立时，才允许省略内部结束正文，不能再发结束礼仪或重复回答。

### W04：状态修改证据依赖可选沟通标签，可接纳只有状态说明的完成提议（P2，隔离反例）

`work_control.py:1068–1074` 要求成功的 side_effecting 效果，同时用 `not effect.work_report` 排除沟通报告。未标记的 `send_message` 本身也是 side_effecting，因此相同通信效果仅改变可选标签就改变完成结果；没有核对具体目标是否获得所要求的修改证据。

本地隔离反例沿真实 WorkControl/WorkSession 和 Social 测试适配，将原持久 goal 明确更新为 `write a state file`、output_kind=state_change，只执行一次 `send_message("synthetic status")`，没有文件修改。无 work_report 时 complete 提议被接纳；加 work_report.kind=final 后同一提议被拒绝。此处验证的是原共同完成入口的提议接纳边界，不是线上模型已经误把某个真实任务完成，也不是任意发送都应被拒绝。

`test_work_communication.py:536–572` 已测试带 start/final 标签的沟通不充当修改证据，缺少未标记状态说明的对照。root 把 probe 的实际持久 goal 收紧后，两个对照案例再次通过，日志 `.cache/work-audit-state-change-verified.log`。

**最小修复方向：** 由可信工具执行与业务结果给出“沟通报告/目标交付/业务修改”的证据角色，不能仅由模型可省略的标签裁决。保留“发送本身就是目标副作用”的合法任务，避免统一排除全部 Social 或用目标关键词猜策略。W03 的收尾顺序改善须与这个核验一起审查，不能让空 final 扩大松散效果证据的影响。

### W05：主动 fail 不退出循环，段末可丢失失败结束意图（P1，真实执行器隔离复现）

`work_control.py:1007–1014` 校验 reason 和未完成子任务后，只保存 reason checkpoint、提出 ending=failed。`services/turn_execution.py:1743–1774` 的工具后退出分支处理部分 completed 和 waiting 状态，没有 failed，最后在 `1798` 返回 Continue；`550–640` 的下一轮 begin 也不以 failed 为停止条件。

**已实测三种后果：** caller 与非 caller 均在 fail 后继续请求一次模型，取得正常 final 才落库 failed；caller 的下一轮仍能执行 send_message 并保存成功回执；fail 恰好位于段末时，两类任务均落库 queued。样本没有新输入、未决效果或活子任务，不是合法等待条件阻止终结。

段末原因是 `services/turn_execution.py:1818–1820` 无条件设置 yield_segment=True、ending=queued，随后 `work_supervisor.py:292–293` 优先处理分段退出，走不到 `301–303` 的 failed 结算。原 fail 工具回执和 reason 留在 journal，不等于保留可恢复的终态提议。`work_session.py:1691–1705,1752–1761` 的业务执行围栏仍核验身份、权限、新输入和未知效果，但没有以 ending=failed 禁止后续执行；此时数据库仍 running、租约有效。

所以 fail 并非永远无效，而是**不能可靠地作为立即结束边界**。其后模型请求发生异常还可能替代原失败提议，是源码可达风险，未计入上述实测案例；不笼统声称所有异常都导致 suspended。

**最小修复方向：** 原 fail 工具结果配对、journal 保存后，结束本次 activation，交由原 supervisor/writer 结算；段末耗尽必须尊重已接纳的结束提议。禁止依赖另购一轮模型 final 才完成主动失败。仍须处理晚到输入、活子任务、未知效果和迟到回执，不让 fail 抹去已发生或未知的事实。caller complete 的内部结果合同与 fail 的停止问题分别修复。

### W06：结算前失去 owner，paired 检查点中的等待意图未被恢复（P1，恢复边界复现）

`task_control.wait` 已登记一小时后的真实 time_due 绑定，且 journal 的 metadata.ending 已保存 waiting_external。若在 paired 保存之后、外层 owner settle 之前失去执行权，Work 行仍 running。`work_session.py:316–336` 恢复 progress/handoff 等资料却不恢复通用 ending；`343–355` 只在 delivery/delivered 分支恢复 ending。新的主执行器于是可把等待当成普通可继续工作。

隔离样本用真实 lease release/acquire 模拟该窗口，没有直接改 Work 状态，也没有真正杀进程。fresh WorkSession 恢复时 ending=None、原 wait 仍 active；真实主循环随后在时间未到、没有新输入时成功发送，并以正常 final 落库 completed。终态 writer（`work_repository.py:568–572`）还取消了尚未命中的原 wait。

随后补验真实 DurableInvocations 原执行重入：仅在首次 waiting_external 结算处受控释放原租约并抛 WorkConflict，真实 owner cleanup 留下持久 running；再用同 service/runtime/execution 调用原 service.run，仍是原 Work ID，却在 timer 未到时发送，最终 completed 并取消 wait。`durable_invocations.py:79–94` 按 Work 行状态阻挡已结算 waiting，却允许 running 进入共享执行恢复；没有在此重核仍 active 的原 wait。该新增案例单独 1 passed，没有重跑前七例。

**边界限制：** 证明的是共享主执行恢复和同步原 owner 的同 execution 重入，失租在私有结算边界受控注入；不是实际杀进程或完整生产权限/入站验收。正常 WorkResumer 对 running orphan 在 `services/work_resume.py:77–83` 先登记中断失败，并非直接调用模型；不能描述成“所有调度恢复都会自动提前发送”。

**最小修复方向：** 新 owner 在任何新模型或业务派发前，对账原持久等待、绑定状态、输入、来源和 generation，兑现等待或处理真实唤醒；不能只恢复展示 transcript 而遗忘控制意图。已经命中的信号、真实改向输入或取消也不能被盲目恢复的旧 waiting 标记覆盖。应在原 journal/等待 owner 上修复，不新建第二套恢复循环。

### W07：caller 已接纳 complete，取得内部结果时仍可新增业务效果（P2）

caller 的后续内部结果请求是合法继续入口，但当前未限定继续工作的性质。隔离原目标为 `send once and finish`：send 成功 → complete 成功 → 没有新输入仍执行第二个不同调用 ID 的 send → 内部 final；同段四次请求与跨段 2+2 两条路径均落库 completed、两次确认发送。请求边界的 ending 为 `[None, None, completed, completed]`。第二次是新效果，不是原发送回放。

`turn_execution.py:1680–1694,1743–1748,512–540` 保留并重核候选，却未建立“取得结果”的执行范围；`agent_runner.py:1133–1140` 只拦截没有 Work 的相关调用。实际 `main_agent_backend.py:411–459,534–552` 仍核验来源、租约、权限和新输入，但不因 completed 候选限制新业务副作用。探针使用既有 caller 后端和真实 Social 服务，修正私有适配器为每次传输使用原 call ID；不是完整生产 MainAgentBackend 装配测试。

**最小修复方向：** 保留必要内部结果和获准证据读取；没有新输入撤销候选时，应停止新增业务修改/发送。发现原回执未知、目标修订或晚到输入仍走共同复核。不要用一刀切禁止所有读取来“闭合”这个阶段，也不要让原候选自动给后续修改背书。

### W08：隐式 final 把内部草稿也强制纳入产物交付（P2，明确原目标对照）

`work_control.py:366–372` 的 complete_internal 固定传入 all_artifacts=True；`1027–1050` 因而把全部 successful known artifacts 作为选择集合，并在 deliver_artifacts=true 时要求全部已交付。代码把“已知产物存在”提前写成“原目标承诺向外发送”。

Root 隔离样本的真实持久 goal 明确为“交付最终包，未完成草稿留内部”。两个真实不可变本地快照通过受控 producer receipt 登记，Social 服务和 FakeTransport 只成功交付最终文件。正常无工具 final 后 DB suspended；显式 complete(artifact_ids=[final]) 后 DB completed。两例均两次模型请求，最终文件确认送达，内部草稿未发送；没有真实 QQ。

这是合法保留不完整成果被多余交付门槛阻断，不是缺少最终文件，也不证明草稿应被补发。**最小修复方向：** 完成候选应复用原目标实际选择/承诺的产物集合，明确中间成果与交付成果；无法确定集合时保留具体缺口和原 ID 的选择入口，不能默认选全部、默认选最新或按文件名/正文猜。显式选择不能越过真实 artifact、文件附言和未知效果核验。

### W09：隐式完成被拒后丢失具体原因，仅留下通用暂停（P2）

`complete_internal:366–380` 捕获 ValueError/WorkConflict 后 pass。若没有 child 或未决 effect，ending 仍 None；`turn_execution.py:1531–1540` 结束 activation，`work_supervisor.py:313–314` 转为 suspended/paused，`work_repository.py:547–566` 的普通结算没有收到原拒绝原因。

Root 三例真实主执行器/SQLite 探针分别缺 answer 的 final 交付、artifact 产物、state_change 修改证据：均 DB suspended、reason=paused、recovery.failure_json={}，内部结果正常返回。正确拒绝 completed 的保护存在，但“具体还缺什么、谁能继续”被压成泛化状态。现有旧 recovery 行还可能保留旧 failure_json；本轮只实测无旧行的三例，不增加未验证的旧原因误归因结论。

**最小修复方向：** 共同完成核验保留稳定拒绝 code、必要原效果/产物/子任务引用和后续 owner 动作，显式与隐式入口复用。缺证据、等待已有执行、unknown、来源失效和缺用户输入分别处理；不因拒绝就购买礼仪纠正轮，不用另一模型或关键词分类器猜完成。

## 不提前写死或闭合：本轮具体判断

本报告开头、[共同开发约束](development-contract.md#0-设计原则)和 AGENTS 已同步补入简短接手原则。原则是保留合法未完成、原因与继续入口，并让已承诺结束的执行真正停止；不要求删除全部枚举或核验。

| 规则 | 本轮判断 |
| --- | --- |
| 身份、授权、generation、原执行 ID、unknown、累计预算 | 必须保留；未知结果不能为了闭合而假定失败或成功 |
| direct 生命周期控制必须单独成批 | 三例 complete/fail/wait 混 send 均整批拒绝、零发送；正确保护原顺序/owner，未证过度限制 |
| Code 控制后停止 VM 与 handoff 恢复 | `codemode/driver.py:719–769,352–373` 和 `agent_runner.py:700–715` 有持久停止边界；本轮只读源码，不报新增 Code 测试通过 |
| goal 修正不静默更换 output_kind | 有真实新用户输入的独立任务/handoff 路线；不凭固定 enum 判错误。混合成果沿原目标继续怎样扩展合同仍是产品政策 |
| 已知 artifact 全部必须外发 | W08 已证过度收尾；内部草稿可以合法留下 |
| 三次同参数/结果就“没有进展” | `turn_execution.py:1695–1726,1786–1794` 在无持久有限模型预算时触发暂停；同结果不证明语义无进展。这是启发式拥有暂停权的设计风险，尚未构造真实合法进展被误暂停反例，不新增 P 级故障 |
| 缺功能/定义或 Provider 不完整 | 当前主要保留检查点并暂停/有界继续，未找到默认 completed 的新反例；必须说明恢复触发，不能靠暗换 Provider 解决 |
| child failed/cancelled 作为 owned_run 信号 | `work_wait.py:655–699` 将 member_failed 交回父任务，不直接判父目标失败；这是应保留的继续空间 |

### D02：信号已保存但暂停未解除，需要明确叠加政策

实测原 wait 登记后在 owner 恢复边界注入真实 WorkCapacityError(work_protocol_storage_capacity)：Work suspended；信号到达后绑定 delivered、原输入 ready/pending 且未消费，Work 仍 suspended、调度器选择 0 项。信号后的管理 resume 可以 queued 原 ID；另一个探针确认同来源的中立 task_control.resume 在绑定仍 active 时也有合法入口。没有丢失信号，不是永不可恢复。

`work_wait.py:385` 只唤醒 waiting_external/waiting_user，`work_scheduler.py:209–239` 不因 ready 输入选暂停项。容量暂停可能正确地需要先解除阻塞，信号并不证明容量恢复。应明确“原 owner 重核后自动继续”还是“保留原暂停并明确提供已保存信号/恢复入口”；不能无条件让信号越过 capacity、unknown 或 generation。此处是已确认状态不对称及未定义政策，不列为已证明应自动恢复的故障。

### D03：旧文档还在要求已删除的未发送纠正轮

`provider-output-boundary.md:17–24,44–49` 仍描述一次未送达反馈和最终正文纠正；现行 `main-agent-runtime.md` 与当前正常 final 源码明确内部结束、不追加结束礼仪轮。不能让接手者按这份旧说明重新写回强制门槛。应更新冲突文档，同时保留推理/正文隔离、显式发送与真实回执合同。本轮登记该漂移，尚未改该模块文档或源码。

## 历史问题、排除项与尚未确认的线索

### H01：旧强制开始通报把真实用户任务阻断；当前源码已经移除

Work `1f294ea0-67b8-48e1-b38b-9c575069605d` 的目标是创建、实现和打包“死神 vs 火影”原型，output_kind=artifact。原失败记录是 `WorkNoProgress`，diagnostics.reason=`work_start_not_delivered`。开始报告发送失败后，后续成功的说明被标为 reply，旧门槛仍反复拒绝开工。原回复解析还受负数平台 ID 的旧限制影响。

当前有符号 ASCII 十进制解析器已支持负数 ID，当前执行链没有旧 `work_start_not_delivered` 开工门槛。这个任务仍挂起，只证明旧检查点没有被自动处置，不能据此认定旧开工限制仍在运行。它的目标是可玩的 artifact，发过“准备开工”不等于已完成原型。需要按原任务核对已有工程/执行回执后，显式恢复或取消；不能由迁移替用户作出决定。

### 排除：把 SELF 通知重选饥饿当成本次 14 个任务的根因

调度器会处理带 planned/blocked notice 的挂起项，但正常 SELF `return_to_caller` 不创建该 notice，生产 14 项也确实没有投递记录。这条额外饥饿推断不适用于这些真实样本。

### 排除：直接沿用旧的 lease 失效后恢复永远不落账结论

历史调查曾发现失效 lease 无法提交恢复状态、running 被反复选择。当前源码已有 fresh-lease orphan/deferred 处理，并核对 generation、fence、cancel_epoch 与 revision；新 owner 可以登记原失败而不重做模型或外部效果。报告不能把旧历史线索直接记作现版本已证实缺陷；仍需特定数据库竞争复现才能增加结论。

### D01：后台恢复逐项串行，慢任务能拖延其他会话的恢复（P2，影响需继续量测）

`work_scheduler.py:239–248` 在一次最多 8 个候选中逐项 `await resumer.resume`。该 await 可包括模型和任务执行，不仅是短数据库接纳。14:06 快照的 serial_resumer 为 11 次 / 总计 397.26 秒 / 单次最大 83.56 秒，说明真实处理能够长时间占该恢复循环。它不等价普通聊天的全局锁死：普通聊天有独立入口，子任务也有自己的调度容量。

建议在原每会话租约、预算和效果 owner 不变的条件下评估有界的不同会话并发，或让调度器仅派出并持有执行句柄。不能无上限并发，也不能用这些累计数字直接计算回复 p95 或声称所有回复慢均来自调度器。先用两个不同会话、一快一慢的恢复回归证明排队边界。

### 保留的未证实线索

- child 显式 complete 后同样可能继续取得内部结果，但本轮没有证明 child 会因此新增有害业务效果；W07 的实测范围仅为 caller，不将它扩写成全部子任务已证故障。
- 完成准备核验子树，writer 的完成检查直接读取本 Work 效果；终态 child 的迟到回执能否在两者之间形成新的未决事实，尚未证明存在合法竞态。不能仅凭查询范围不同宣称完成 CAS 已失效。
- paired 阶段其他通用 ending 的恢复也值得核对；等待丢失由 W06 证明，fail 分段覆盖属于 W05，其他分支没有新增独立实测，不重复计为新问题。

## 修复时必须保留的安全边界

- 同 source_key 去重、原 Work/execution/operation ID 和累计预算。
- 任务完成时在原 writer 围栏内复核晚到输入、prepared/unknown 效果与未完成子任务。
- 分别确认最终文本、artifact 文件及附言、业务变更；沟通发送不能代替 state_change。
- SELF 可以合法 NO_REPLY 静默完成，但源身份或未知效果不能因此跳过核验。
- 管理取消保留已发生效果和迟到回执；禁止为了“清干净”删除源事实或重新创建工作。
- generation/授权失效停止旧链，不能用当前账号、最近用户或最新消息重建旧授权。

## 审查覆盖情况

| 环节 | 本轮判断 |
| --- | --- |
| 接纳与重复来源 | 原 source_key 去重、容量核验存在；停摆任务仍计接纳容量是 W01/W02 |
| 激活、租约与取消 | 原 lease/fence/generation 保护存在；当前 fresh-owner 恢复已区别于历史旧缺陷 |
| 模型与工具往返 | journal 配对与 paid dispatch 不等于目标完成；恢复不得重复原效果 |
| 正常完成 | 共同 complete 仍核验输出合同及未决事实；这些保护应保留 |
| 主动失败 | fail 有控制入口，但没有即时退出与执行停止边界；段末可覆盖为 queued（W05） |
| 结束提议恢复 | paired 等待已保存，但 fresh session 丢通用 ending；主执行恢复路径可提前继续（W06），调度 orphan 另走异常结算 |
| 完成后的结果阶段 | caller 内部返回必须保留，但仍能新增业务效果（W07） |
| 未完成产物与拒绝事实 | 草稿被纳入全部交付集合（W08）；隐式完成拒绝原因被吞（W09） |
| 空 final / 空 Provider 响应 | 显式 complete 后有收尾保护；没有提议的隐式空 final 存在 W03 不对称路径 |
| 挂起与重试 | 有界重试存在；重试耗尽后不同 origin 的退出/恢复不闭合 |
| 等待与唤醒 | 信号持久、按原 ID 去重；等待释放执行权但保留长期任务，不应混作失效机会 |
| 子任务 | 原子完成重核未终态子任务；不能用父任务失败强行吞掉活 child |
| 插件 / 自动化 / SELF owner | 不能只修普通用户入口；插件 Job/Work 状态不一致已有线上实证 |
| 管理取消与回收 | 本次 14 次管理取消成功；终态退出接纳计数，事实和旧回执继续保留 |
| 诊断与实际状态 | 健康只证明循环运行；active_work_count、外层 Job 统计和状态缺样不能证明目标完成 |

## Pi 对照、验证与实施顺序

### 官方 Pi 的对照基线

采用官方 [earendil-works/pi 的固定提交 `9fba660cf1caca0ade5bea72269352416e595a19`](https://github.com/earendil-works/pi/tree/9fba660cf1caca0ade5bea72269352416e595a19)，提交日期 2026-10-02，复用此前 10-03 对照基线；**不声称为 10-08 最新版**。实际阅读 `packages/durable` 的实现、规格和测试源码，未安装或运行 Pi demo/测试，没有引入 Pi 依赖或复制实现代码。

| 问题 | Pi Durable 的真实边界 | Yuki 可以借鉴的部分 |
| --- | --- | --- |
| 任务与执行名额 | pending/running/waiting/completing/terminal；显式 waiting 停车释放 invocation，但仍是 live 任务 | 分清执行实例、保留承诺、接纳容量；暂停释放租约不代表生命周期已经退出 |
| 暂时失败 | 模型错误按策略有限重试，耗尽后 generation failed；定义缺失的 blocked 则保留 live 等合法代码 | 明确失败、失效机会和可恢复承诺的不同出口，不能统一永久 suspended |
| 等待与退避 | join waiting 释放 invocation；retry/poll 的 runtime.sleep 保持 running invocation | 不把所有 sleep/wait 误称为不占资源；对应 driver 应明确唯一所有者 |
| 模型结束 | stop/length 或无 call 的 toolUse 进入 answer，提交回答；onYield 可续行，否则 submission done | 使用确定结束边界进行共同业务结算，不在已经完成后再购买礼仪轮 |
| 工具结束 | 工具效果与 generation/submission 结果分开；正常错误结果仍供原模型继续 | 工具 succeeded 不等于 goal 完成；未知 QQ 发送继续查原持久回执，不能借 safe-replay 无条件重发 |
| 子树与后台 | outcome 已定但普通子树未排空则 completing；background 有明确 idle/abort 边界 | 父完成、子结束、后台独立生命周期分开；保持原 owner，不用当前真人授权恢复 SELF |

上述事实对应官方固定源码：[TaskState](https://github.com/earendil-works/pi/blob/9fba660cf1caca0ade5bea72269352416e595a19/packages/durable/src/types.ts#L486)、[scheduler 的 waiting/预约](https://github.com/earendil-works/pi/blob/9fba660cf1caca0ade5bea72269352416e595a19/packages/durable/src/harness/scheduler.ts#L699)、[模型 retry/poll](https://github.com/earendil-works/pi/blob/9fba660cf1caca0ade5bea72269352416e595a19/packages/durable/src/harness/generation.ts#L210)、[blocked 保持 live](https://github.com/earendil-works/pi/blob/9fba660cf1caca0ade5bea72269352416e595a19/packages/durable/docs/spec.md#L1969)、[completing 子树结算](https://github.com/earendil-works/pi/blob/9fba660cf1caca0ade5bea72269352416e595a19/packages/durable/src/harness/scheduler.ts#L965)。

Pi 的 [final 分类及 answer/onYield](https://github.com/earendil-works/pi/blob/9fba660cf1caca0ade5bea72269352416e595a19/packages/durable/src/harness/generation.ts#L447) 解决会话轮次结束，未核验 QQ 最终发言、文件交付或业务修改目标。因此不能直接复制 answer done → Yuki completed。Pi 的 [工具恢复](https://github.com/earendil-works/pi/blob/9fba660cf1caca0ade5bea72269352416e595a19/packages/durable/src/harness/tool.ts#L84) 也有 safe/interrupted 边界；Yuki 仍须按原效果 ID 核对 QQ 发送，不重新派发未知效果。

此 Pi 基线没有 Yuki 的全局 128 / 会话 16 非终态接纳政策，所以不能用“Pi waiting 会释放 invocation”证明 Yuki 所有挂起都应从容量移除。可直接借鉴的是原身份的 intent/effect/outcome、不同等待的停车语义、模型结束与业务证据/子树排空的分层；不建议引入第二套任务引擎、状态账或恢复循环。

### 本轮深入到代码细节的借鉴点

| Pi 细节 | Yuki 可参考的实现结构与边界 |
| --- | --- |
| outcome 与 state 分开；completing 无 checkpoint、禁止继续父执行；waiting 有 checkpoint、以后继续 | 区分“已停止执行但正在收拢”与“暂时停车等待新事实”。借鉴结束意图持久化及执行 gate；Yuki 成功仍是需复核目标/交付的候选，不能照搬 held completed 后永不改变成功 |
| commitState 看同次候选 overlay，finalize 反复结算已排空子树直到稳定 | 原 owner 统一收尾，child 结束可释放上级；不通过额外模型 final 维持结束意图。Yuki writer 只作有界当前事实复核，不搬入 Pi 的整树物化或外部等待 |
| step 先检查已提交 terminal/completing/waiting，再处理 phase 后续 throw | 已提交的合法控制决定先于派生收尾故障；W05/W06 应保住原意图。内存提议不等于数据库已确认结果，仍须原 revision/fence 围栏 |
| 工具参数拒绝/blocked 是明确配对错误结果；tool task failed 不直接判父目标 failed | 保留稳定拒绝 code 与原结果，给父目标合法继续空间（W09）；协议任务完成不证明业务成功，QQ unknown 仍不得新增副作用 |
| answer/onYield 可在 final 边界保留原 input 并 handOver 后续 generation | 为真实继续原因留下入口；不能重新引入礼仪轮。hook 普通异常仅 report 后放行，所以必需授权/交付核验不能放在这种最佳努力 hook；无限 continue 也没有活性保证 |
| 缺定义 blocked 由 record+registry 派生，registry 变化触发重新核验；取消另有出口 | 开放状态要有明确恢复事实与消费者。Pi 也可能永久 blocked，不能复制成 Yuki 所有 suspended 的默认政策 |
| safe subagent replay 找原 taskId 名下已有 child，再用稳定 requestId submit | 继续原身份和回执，避免重建任务。不能用 safe 声明替代 QQ 原 effect ID 的确定性查证 |
| abort mark 持久保存，撤权、停止执行与子树收拢分别进行 | Yuki 的 cancelled 首先表明原授权已撤销，不能据此宣称外部进程或 QQ 请求已经全部停止。应按原 run/effect ID 在 writer 外核对收拢，保留迟到事实；未证明现有撤权设计有缺陷 |

固定源码引用：[state/outcome 类型](https://github.com/earendil-works/pi/blob/9fba660cf1caca0ade5bea72269352416e595a19/packages/durable/src/types.ts#L451)、[commitState](https://github.com/earendil-works/pi/blob/9fba660cf1caca0ade5bea72269352416e595a19/packages/durable/src/harness/scheduler.ts#L976)、[finalize](https://github.com/earendil-works/pi/blob/9fba660cf1caca0ade5bea72269352416e595a19/packages/durable/src/harness/scheduler.ts#L491)、[step 结算优先级](https://github.com/earendil-works/pi/blob/9fba660cf1caca0ade5bea72269352416e595a19/packages/durable/src/harness/scheduler.ts#L931)、[工具配对与诊断](https://github.com/earendil-works/pi/blob/9fba660cf1caca0ade5bea72269352416e595a19/packages/durable/src/harness/tool.ts#L359)、[onYield](https://github.com/earendil-works/pi/blob/9fba660cf1caca0ade5bea72269352416e595a19/packages/durable/src/harness/generation.ts#L505)、[hook 异常边界](https://github.com/earendil-works/pi/blob/9fba660cf1caca0ade5bea72269352416e595a19/packages/durable/src/harness/scheduler.ts#L1059)、[blocked 派生](https://github.com/earendil-works/pi/blob/9fba660cf1caca0ade5bea72269352416e595a19/packages/durable/src/harness/scheduler.ts#L754)、[原身份 replay 样例](https://github.com/earendil-works/pi/blob/9fba660cf1caca0ade5bea72269352416e595a19/packages/durable/test/harness-ownership.test.ts#L845)。只读实现及官方测试断言，未运行 Pi。

此版本 [LICENSE](https://github.com/earendil-works/pi/blob/9fba660cf1caca0ade5bea72269352416e595a19/LICENSE) 为 MIT；以后直接移植实质代码应随副本保留原 copyright 与许可通知。本轮没有复制 Pi 实现或引入依赖。

### 建议实施顺序与验收

| 顺序 | 目标 | 必须覆盖的验收 |
| --- | --- | --- |
| 1 | 保住 fail/wait 控制意图与原停止边界；解决 W05/W06 | caller/非 caller/child、段末 fail、paired 后失租、原 execution 重入、时间未到与真实信号已到；新输入、活 child、unknown、迟到回执仍保留原围栏 |
| 2 | 关闭零效果、失效的短暂 SELF 机会；解决 W01 的新增积累 | 四次断连、单会话 16 名额、重新连接后新接纳；已有发送/未知效果/长期承诺不能误终结 |
| 3 | 由插件原 owner 闭合 Job/Work 生命周期；解决 W02 | 原事件重复处理、外层三次失败、管理恢复委托、原批准/generation/预算；确认发送不重发，未知发送不猜失败 |
| 4 | 修正 caller 完成候选与内部结果阶段；解决 W03/W07 | 可用非空/空 final、Provider 空异常、新输入；允许必要结果读取，禁止候选未撤销时新增业务效果 |
| 5 | 共同核验保留证据角色、产物选择和拒绝原因；解决 W04/W08/W09 | 未标记通信/合法发送型目标/真实修改；内部草稿+最终产物；具体拒绝 code、原继续入口、unknown 与交付 CAS |
| 6 | 明确 D02 的暂停/信号政策，更新 D03 冲突文档，再按证据评估 D01 并发 | signal-held 与继续 owner；旧礼仪规则不写回代码；一快一慢两个会话、同 scope 单一租约、关停回收，不绕过 shared budget |
| 7 | 对剩余旧任务逐个决定恢复或放弃 | 旧 artifact 目标看真实工程和交付；旧已发送插件目标看原效果和候选，不批量按年龄删除或标完成 |

每个 Work 必须有明确的恢复消费者或明确终态出口。临时自主机会与长期承诺采用不同退出政策；终态仍保留真实已发生/未知效果的原 ID。完成结算应集中复用当前共同核验，以删除重复门槛和闭环为优先，而不是添加语义分类器、定时催促或礼仪重试。

### 验证与交付状态

- 新 high 完成专项执行 21 项既有定向测试，全部通过：caller completion、晚到输入 CAS、artifact 交付、终态 child 迟到回执、unknown mutation 围栏。原始结果保留于子任务工具输出，未落盘 stdout 日志；21 个已有 ORM 建表循环警告如实保留，不记为产品错误。可复核命令如下。
- 空 final 专项的前三个合成案例分别验证非空正常完成、控制尾段剥离后空 final 暂停、连续 Provider 空响应暂停；均只有一次合成发送。后两个 state_change 标签对照也分别通过。期间新增 audit fixture 首次误用 WorkSession 导入而失败，修正的是私有 probe，未改产品。
- root 发现 state_change probe 最初只改变模型输入而未明确改变持久 goal，进一步通过原 task_control.update 将 goal 固定为写状态文件，再运行两例：2 passed、2 个同类 ORM 警告。不能把这个补验写成未修改版本的整文件一次 5 passed。
- 挂起专项 4 个离线 probe 验证原插件事件返回 suspended 且不执行、管理 resume 拒绝、根调度排除该来源、外层 fail_turn 三次 pending/pending/failed；其中 fail_turn 使用真实方法与受控 job 行，不冒称完整插件端到端测试。首次 probe 的测试身份配置错误与最终成功日志均保留。
- Pi 只阅读固定官方源码与测试，没有执行 Pi 测试，不报告其在本环境通过。
- 主动结束追加 9 个独立案例，经真实 MainAgentTurnService、Work owner 结算和 SQLite 验证，分两次成功覆盖：首次 5 passed / 4 failed，4 项仅因私有断言误把 FakeBot 初始化身份读取视为发送而失败；修正为无 send_group_msg 后仅重跑这 4 项，4 passed / 5 deselected。保留原失败及成功日志，不冒称整文件一次 9 passed。首次混合私有目录与现有 SELF 测试的 collection 双注册错误也保留，0 项执行。SELF 与旧 21 项未在追加复核中重跑。
- 开放/终止细节又新增 **15 个独立案例**，分别通过而非同一次整文件通过：控制专项 8 项（3 个 direct 混批拒绝、2 个 caller 新效果、1 个 wait restore、1 个主循环失租、1 个真实 DurableInvocations 原 execution 重入）；信号/暂停政策 2 项；root 拒绝原因 3 项与 draft/final 选择 2 项。控制专项首次 3 passed / 3 failed，仅因私有 Social call_id 重用及未 hydrate manifest；修正后仅重跑三项，3 passed / 3 deselected。新增主循环单例首次因错把 bare result.work_state 当持久状态而失败，修正后 1 passed / 6 deselected；真实 caller 重入另 1 passed / 7 deselected。所有失败尝试 append 保留。root 两次为 3 passed、2 passed / 3 deselected；信号两例整文件 2 passed。既有 ORM 循环警告保留；不把 probe 通过写成产品已修复。
- 已检查报告源码引用、相对路径及事实口径；本轮没有重复全量 CI，没有修复、提交、推送、合并或部署产品。源码政策缺口仍在；当前生产状态不能由已完成报告推导为这些问题已修。

```powershell
.venv/Scripts/python.exe -m pytest tests/unit/test_caller_work_completion.py tests/unit/test_runtime_work.py::test_completion_cas_retains_input_arriving_after_empty_mailbox_read tests/unit/test_runtime_work.py::test_artifact_completion_requires_verified_delivery tests/unit/test_work_effect_lifecycle_repository.py::test_terminal_child_late_execution_receipt_settles_without_reviving_child tests/unit/test_work_execution_dependencies.py::test_unknown_mutation_blocks_following_mutations_and_completion -q -p no:cacheprovider
```

私有证据文件：`.cache/work-chain-production-audit.json`、`work-audit-suspend-high.md`、`work-audit-suspend-high-probe.py/.log`、`work-audit-finish-high.md`、`test_work_finish_probe.py`、`work-audit-state-change-verified.log`、`work-audit-pi-high.md`。它们保留调查/复现细节，未作为生产代码或凭据提交。

主动结束追加证据：`.cache/work-active-finish-paths-high.md`、`work-active-finish-high.md`、`test_work_active_finish_probe.py`、`work-active-finish-probe-collection-attempt.log`、`work-active-finish-probe-assertion-attempt.log`、`work-active-finish-probe.log`（首次 5 passed / 4 failed）、`work-active-fail-probe.log`（修正断言后的 4 passed）。精确命令分别为 `.venv/Scripts/python.exe -m pytest .cache/test_work_active_finish_probe.py -q -s` 与 `.venv/Scripts/python.exe -m pytest .cache/test_work_active_finish_probe.py -k explicit_fail_db_and_next_request -q -s`。

开放/终止细节追加证据：`.cache/work-open-termination-batches-high.md`、`test_work_open_termination_batches_high.py`、`work-open-termination-batches-high.log`；`work-open-continuations-high.md`、`test_work_open_continuations_high.py`、`work-open-continuations-high.log`；`test_work_open_final_root.py`、`work-open-final-root.log`、`work-open-artifact-root.log`；`work-open-pi-details-high.md`。本地 Pi 45 个固定引用逐项核对文件/行号存在，不等于运行官方测试。

root 的精确新增命令为 `.venv/Scripts/python.exe -m pytest .cache/test_work_open_final_root.py -q -s`（当时三例）和追加后 `.venv/Scripts/python.exe -m pytest .cache/test_work_open_final_root.py -k internal_draft -q -s`；控制专项首次整文件后分别只重跑 caller/wait_restore、主循环失租、durable 原 execution 重入；信号命令为 `.venv/Scripts/python.exe -m pytest .cache/test_work_open_continuations_high.py -q -s -p no:cacheprovider`。具体筛选命令与每次失败尝试保留在相应私有报告/日志。

**推荐先修退出与恢复所有权，再统一完成候选和证据核验。** 清理存量、正确终结任务、成功交付、模型停止和诊断统计必须分别确认。
