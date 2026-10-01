# Yuki 共享持久 Runtime 主干重构任务书

> **建议：保留现有执行内核，重构外围编排。**
>
> 以现有 durable work 为持续工作的权威状态源，收拢 activation 的进入、恢复、退出责任；让调度器只负责调度，让来源恢复和实际投递回到明确的业务边界。删除重复驱动、绕路依赖、散落的执行装配和重复清理代码。
>
> 本任务不是再建立一个全局 `AgentState`，也不是把所有 worker 合成一个永久循环。

| 项目 | 内容 |
| --- | --- |
| 仓库 | `YuanYeYouTao/Yuki` |
| 核查分支 | `main` |
| 核查提交 | `b1382cf80429d82e5071275d5bc97f594209ed0b` |
| 提交说明 | 合并 PR #213：Reduce SQLite lock contention and response-path waits |
| 文档日期 | 2026-10-01 |
| 本次交付 | 源码静态核查结论、目标架构、分阶段任务及验收合同 |
| 实施状态 | 已授权自主实施、提 PR、合并及 Bot-only 上线；实施分支 `codex/persistent-runtime-core`，当前任务完成情况以交付记录为准 |
| 验证状态 | 已完成本地关键路径静态核查；实施期间按切口定向验证，终局执行完整门禁，未运行项不能写为通过 |

## 1. 核查结论

### 1.1 需要重构，但先纠正问题定义

当前 Yuki 不是多个互不相干的 Agent 引擎拼接起来的系统。它已经拥有共享的主 Agent 执行入口、冻结的主工具清单、持久 work 状态、activation 退出结果和来源恢复机制。[S05][S06][S07][S08][S15]

尤其是 automation：`AutomationCapabilityHandlers._main_turn_service()` 会返回 `contract.chat._main_turns`。所以不能把它描述成一套另行实现的主 Agent loop。Subagent 也直接复用 `self.app.chat._agent_runner`，只使用不同的 worker 上下文和固定工具子集。[S14][S10]

**真正需要治理的是：共同执行内核已经存在，但其外围的生命周期编排、依赖获取、来源重建和回执翻译仍分散在多个入口。**

这比“新增统一状态视图”更值得优先动代码。只在外面再套一个状态视图，不会减少现有私有字段访问、重复租约清理或调度器里的平台代码。

本文不依赖此前关于 Dots 的实现推断。取舍依据是当前 Yuki 源码、现行开发合同，以及保留成熟能力、减少实现分叉的目标。

### 1.2 已核实的具体问题

下表中的“事实”是源码可直接确认的结构；“处理”是本任务的设计建议。结构耦合不等于已经观察到线上故障。

| 编号 | 已核实事实 | 建议处理 |
| --- | --- | --- |
| F01 | `MainAgentTurnService` 同时承担 prompt/history projection、已有 invocation 查找、恢复条件判断、activation 开启、同步结果缓存和 runner 调用；`run()` 通过再次调用自身进入已绑定 work 的分支。[S05] | 保留主入口，移出其持久执行编排，改为清楚的“准备后执行”；消除递归式二次入口。 |
| F02 | `WorkScheduler` 除了扫描 work，还进行来源解释、恢复调用、sender 构造，并访问 `ChatService` 私有成员；`_resume()` 内的 `deliver_message/send` 包含实际平台投递代码。[S09] | scheduler 只选择候选并请求恢复；来源解析、执行和投递不再由 scheduler 亲自实现。 |
| F03 | `SubagentScheduler(app: Any)` 可以访问整个应用；执行中直接使用 `chat._open_memory_session`、`chat._open_self_memory_session`、`chat._agent_runner` 等，并自行装配 WorkControl、ContextVar、续租和清理。[S10] | 显式注入依赖；将 worker 执行体移出调度器，并复用 activation 的公共生命周期骨架。 |
| F04 | 本地固定提交中只有 `AutomationWorker._loop()` 调用 `WorkWaitRepository.deliver_due()`，通用 Work 等待依附自动化驱动。[S09][S13] | 将通用 Work 时间条件驱动迁回持久 runtime 的维护 tick；接管完成再删自动化中的调用，保留并发幂等与 step 防丢唤醒。 |
| F05 | automation 创建自己的 `AgentRunner(..., task=AUTOMATION_AGENT)`，但主调用服务通过该 runner 的 `main_contract` 再绕回 `contract.chat._main_turns`。[S14] | 直接注入实际使用的共享服务和合同；核对全部读写点后删除仅用于这种依赖绕行的对象与装配。不要再建第二个共享服务。 |
| F06 | `ChatService._active_work` 被用于跨 asyncio task 的活动 work 判断及输入投递，`stage_work_input()` 还检查原 actor；它并非无意义缓存。[S12] | 收拢为 activation 所有的短生命周期绑定，保留必要的跨 task 查询，禁止外部代码直接写字典。不能只换成 ContextVar 后直接删除。 |
| F07 | 已有 `sandbox/source_recovery.py` 被子任务复用；它明确区分消息来源与 SELF，并明确禁止将 scheduled automation 经消息恢复入口重建成真人消息。[S11] | 提升可共享部分的归属；来源专有授权继续分别校验，不能为了统一而抹平来源差别。 |
| F08 | `ActivationOutcome` 已经区分 segment、等待、重试、取消、预算等退出；automation 仍在入口处解释 `work_state` 和转换能力结果。[S07][S14] | 复用已有类型作为 activation 退出合同，减少散落判断；不再创建平行的全局状态枚举。 |

### 1.3 状态多，不等于状态重复

必须保留以下区别：

| 对象 | 它回答的问题 | 本次处理 |
| --- | --- | --- |
| 永久 Yuki 主体与共享主合同 | 这是哪个持续存在的主体，它有哪些稳定能力？ | 保留现行约定，不新建主体表。 |
| 一次 conversation turn | 当前会话的这次处理是否被后续输入替代？ | 保留 turn 协调语义。 |
| 持久 work | 这件工作做到哪里、在等什么、还能否恢复？ | 作为执行状态核心。 |
| 一次 activation | 本次醒来为何结束，是否等待、让出或失败？ | 用既有 `ActivationOutcome` 表达。 |
| automation 定义 | 这个定时计划是否启用，下次什么时候到期？ | 保留计划生命周期。 |
| automation run / step cursor | 本次计划触发执行到了哪个步骤？ | 保留运行及游标合同。 |
| autonomy initiative | 这次主动参与提案是否接纳，反馈是什么？ | 保留提案和反馈含义，不冒充 work 完成状态。 |
| child work | 父任务委托的局部执行进展和预算如何？ | 复用 work 内核，保留父子关系及独立 lease。 |
| memory / capability 的 turn-scoped session | 本轮记忆和工具上下文处于什么阶段？ | 保留领域边界，不揉进 work 状态表。 |

这些对象的状态可以同时成立。例如，“定时计划 active、本次 run 尚未结束、关联 work 正等待外部结果、当前没有模型调用”，不构成状态矛盾。[S04][S07][S08][S13]

因此本次不要求一个 `Yuki.lifecycle = waiting` 覆盖整个系统。多会话和子任务并行时，单个总状态反而会隐去事实。

## 2. 目标架构

### 2.1 最终只保留一套执行生命周期骨架

```text
真实消息 / SELF initiative / automation / plugin / completion
                         |
                各来源现有接纳与授权
                         |
            来源事实 + 原工作引用 + 本次唤醒原因
                         |
       准备本轮上下文，按现行边界完成版本检查
                         |
              共享 activation 生命周期
       选择原 work、取得正确 lease、绑定 control
                         |
          主 Agent 入口 / 专用 child 执行入口
                         |
                    AgentRunner
                         |
             WorkSession / 工具执行 / 回执
                         |
             ActivationOutcome + 实际交付结果
                         |
          work 结算 + 原调用方自己的游标/反馈
```

“共享生命周期”不是“每个入口都执行完全相同的业务步骤”。区别在于：

- root 与 child 使用各自正确的租约取得方式，但共用进入后、退出前的生命周期骨架。
- 普通聊天可以没有已接纳的 work；不能为了代码整齐强制每句话先创建任务。
- main agent 继续使用共同主合同；child 保留 worker prompt 和固定工具子集。
- 无需 LLM 的 automation DSL 步骤仍直接执行，不强制先绕一轮主 Agent。
- 不同来源仍负责证明自身的接纳、授权和外部回执，只是不再各自重新实现通用执行框架。

### 2.2 明确四类责任，避免再造总控大类

**来源责任：谁让这件事开始，恢复时依据什么事实。**

复用已有 `TurnTrigger`、原始 event/run 标识与来源校验。共享的是 canonical 事实解析、身份绑定核对和一致的返回形状；automation delegation、plugin approval、SELF initiative 仍有各自的证明过程。

**执行责任：谁拥有这次 activation。**

以 `runtime/work_activation.py` 为归属中心，复用 `WorkControl`、`WorkSession`、`WorkRepository`、现有续租及恢复逻辑。它不直接读取 QQ 平台连接，不负责生成 prompt，也不拥有全应用服务定位器。[S06]

**调度责任：哪件已经可运行的事应被取出。**

`WorkScheduler`、`AutomationWorker`、`SubagentScheduler` 可以继续存在。它们分别调度 queued work、到期计划、child work；保留真实不同的 claim 与容量规则。但它们不再自行装配一整轮 Agent。

**领域结果责任：这次执行结果怎样反馈给原调用方。**

work 生命周期由执行内核结算；automation 的计划推进和 step cursor 由 automation 结算；initiative 的参与反馈归 autonomy；child 结果回父任务。不得为了“单一结算”把这些不同的持久事务合成一个跨全系统的大事务。

### 2.3 文件归属建议

以下是目标责任，不要求机械拆成“一类一个文件”。已有合适位置优先复用。

| 位置 | 最终责任 | 应移出的内容 |
| --- | --- | --- |
| `runtime/work_activation.py` | root / child 的公共绑定、续租监督、退出与释放骨架 | 平台 sender、prompt 拼装、全应用依赖 |
| `runtime/work_control.py`、`work_session.py`、相关 repository | 现有任务控制、journal、恢复、输入与副作用事实 | 不做整体改写，只收拢重复调用责任 |
| `services/main_agent_turns.py` | 主 Agent 的组合与执行入口，调用公共执行生命周期 | 独立重做来源恢复、递归式开 activation 分支 |
| `services/work_resume.py`，建议新增或复用同责模块 | 将一个持久 work 恢复为一次已验证的主调用 | 不接管轮询、DSL 计划或子任务队列 |
| `services/execution_sources.py`，建议提升现有恢复代码至此类共享位置 | canonical 来源恢复与分来源验证编排 | sandbox 自有 completion 存取和平台发送 |
| `services/subagent_execution.py`，建议新增或复用同责模块 | worker 专有上下文和结果返回，复用 Runner / activation | 扫描队列、持有整个 `app` |
| `runtime/work_scheduler.py` | 候选扫描、必要恢复维护、请求 `resume(work_id)` | `_resume()` 内的 `deliver_message/send`、平台消息重建、写 `chat._active_work` |
| `runtime/subagent_scheduler.py` | child 选择、容量与调度 | memory session 装配和整个执行体 |
| `automation/worker.py`、`executor.py` | 计划 claim、DSL step cursor、计划推进 | 通用 work 时间驱动；主调用的另套生命周期 |
| `automation/handlers.py` | 能力到共享主入口的薄桥接 | 为取主服务而创建或遍历多余对象 |
| `application/modules/*`、现有装配根 | 显式构建与注入依赖 | 业务执行时从 `app` 或 `contract.chat` 取私有服务 |

新模块只有在实际吸收旧实现、删除旧分支后才算完成。把 `WorkScheduler` 原样挪成一个同样巨大的 `WorkResumer`，不满足目标。

### 2.4 不建立第二套事实源

保留原数据库中的 `work_id`、`source_key`、work revision、输入、effect receipts、journal、automation run / cursor、父子关系。[S08][S17]

本次默认不增加 `AgentState` 表、通用 `RuntimeRun` 表、全局 GoalGraph、影子工作队列，也不把现有数据同步写入另一套状态记录。

内存中的只读快照和派生结果不是问题；问题是多个模块都可以独立决定同一件 work 已完成、应恢复或属于谁。**要减少的是写入与决策所有者，不是禁止所有读取投影。**

### 2.5 活动绑定必须正确处理跨 task 输入

建议将 `_active_work` 的写入和清理收回 activation 层，由一个小型、带明确生命周期的绑定对象管理。

该对象仅支持已存在的用途：登记当前会话激活、按会话查询供新输入关联、只注销自己登记的激活。不得演变成通用服务注册中心、全局 Agent 状态库或独立授权系统。

保留两个不同工具：

1. `ContextVar` 为当前执行调用链提供便捷绑定。
2. 有界的会话索引供另一 asyncio task 的输入处理查询当前激活。

数据库 lease / generation 才是执行权威。会话索引只提供本进程的活动线索；原 actor 校验、显式 wait 匹配和 durable input 记录不能被这个索引替代。[S12]

退出时应验证绑定身份后再删除，避免旧 activation 的 `finally` 删除刚建立的新绑定。用 `asynccontextmanager` 或 `AsyncExitStack` 表达生命周期，不新增长期同步逻辑。

### 2.6 唤醒原因与原执行来源分开

```text
wake: sandbox completion / timer / work resume
source: 最初的真人事件、SELF initiative、自动化委托或插件授权
work: 继续的原 work_id
```

唤醒属于系统事件，不意味着原工作取得了系统权限。来源恢复也不意味着可以拿新消息的 actor 替换旧工作的 actor。

继续使用现有来源数据；需要更明确的类型时，为已存在的字段增加内存中的 typed view 或 tagged union，不重新生成另一套持久来源协议。`source_key` 和 `invocation_boundary()` 的现有计算结果必须保持兼容。[S05][S06][S11]

### 2.7 所有会话共享一个常驻 YukiRuntime

用户已确认同一部署内所有会话共用一个常驻 runtime。一个 ApplicationContainer 构造唯一的 `YukiRuntime`，管理共享主 Agent 服务、执行激活绑定和持久工作调度的生命周期。各入口使用该实例或其显式注入的窄能力；不得为会话、自动化、插件或 child 创建另一套主 runtime。

主体沿用现有 canonical Yuki。Conversation 历史、主体权限、每件 Work 的目标/预算/journal 和每次 activation 的临时上下文保持隔离；共享运行时不合并聊天历史、不使所有会话全局串行、不把当前 actor/gateway/memory session 存入共享单例。

`YukiRuntime` 是小型应用组合服务，实际生命周期规则归 `work_activation`，来源和恢复归业务模块，领域事实由原领域服务拥有。它提供唯一主执行服务及活动查询，并管理 work/child 调度资源的启停和健康；不复制现有 WorkRepository，不成为全应用 service locator。

启动时根据原持久事实恢复合法工作，内存通知仅加速，原有有界扫描能发现就绪项。退出停止新 activation、取消/收拢执行，保留原 ID、未知效果、回执和预算；重启不依赖旧进程的活动索引。仍遵守各来源现行开关、授权与原调用方结算合同。

### 2.8 追加的职责收拢和删除要求

- ContextAssembler 不拥有 Work 的结算。上下文准备返回资料或明确的准备等待结果，调用者在公共 activation 边界登记等待并退出；保留 required rollup 原期限和旧 journal 的冻结前缀。
- 效果核对归窄应用接口，Automation 不直接解释 runtime effect/journal 和 Social 内部存储。先复用已存在的交付核对算法和原 operation/call 引用，不为统一增加新表；未知旧效果不得凭相似参数补归属。
- production 构造依赖显式且完整。删除本轮被替代的 runner/私有服务绕路和旧内部导出，不以新开关维持双轨。Memory 自身 UnitOfWork 的整体重写独立于本轮，不自动扩大实现范围。
- 不为架构名义调整全局 child 数量、模型预算、插件 API、semantic participation 固定版本或提示词内容。调度并发优化先保留现行容量合同，本轮至少不增加跨会话串行门。

## 3. 必须保留的成熟能力

以下是重构边界，不是另一次功能建设清单。

### 3.1 work 持久执行

保留 checkpoint / journal、跨激活预算、输入 readiness、父子工作归属、效果记录和 unknown / uncertain 处理。`WorkRepository` 的 fenced 写入与 `WorkSession` 的恢复职责保持单一。不要以缩减代码为由让失败后“从头再跑”代替恢复。[S04][S06][S08]

历史 schema 文件不是重构时随手修改的数据模型。`work_schema_v1.py` 明确是不可变迁移合同；改变表结构必须新增 migration，而不是改旧文件制造新旧数据库差异。[S08]

### 3.2 各类并发边界

不得直接把以下三者合并成一把锁：

- turn coordinator：本进程会话处理的占用、替代和协作中断。
- durable work lease：跨激活或进程的工作所有权与失效隔离。
- effect gate / 发送前校验：阻止过期上下文在最后副作用边界继续生效。

可以删除证明完全重复的检查，但必须说明被替代的检查现在在哪个同等可信边界执行。存在等待、外部 I/O 或 revision 变化时，不能凭“前面检查过”删掉后面的检查。

### 3.3 上下文准备与前台响应

普通聊天尚未接纳 work 时，不得在历史整理、memory 或外部准备期间长期占着空的 work lease。当前 `_respond()` 有释放空 lease、准备后再进入的安排，现有测试要求 reset 或另一个合法 owner 可以在准备期间胜出。[S12][S16]

新入口不能用一次覆盖所有阶段的 `async with activation(...)` 抹掉这个边界。

### 3.4 权限与身份

保持 event-id 主体、canonical conversation / generation、person / space / presence 的真实来源。SELF 不借用用户身份；automation 使用自己的委托与脚本来源；插件使用批准及来源事实。来源快照提供上下文，不能替代当前 dispatch 时的权限复核。[S02][S11]

主 Agent 的工具声明继续共用冻结合同。不要因 wake 来源不同，悄悄创造另一套主 Agent 人格、能力目录或系统提示。child 的受限 worker 合同保留，不强行变成主 Agent 的完整工具面。[S10][S15]

### 3.5 automation 运行连续性

保留计划与 run 的区别、首次 run / 初始 cursor 的原子接纳、脚本 hash 固定、misfire 区分、run 使用量及原 run 恢复。

旧 run 已接纳后重启，不应被当作一次新的过期触发。缺失 cursor 或副作用结果不明时，不能清空使用量再重试。现有测试已经对这些情况写出明确断言。[S13][S17]

### 3.6 模型、记忆与部署合同

保留现有 ModelRuntime 路由、协议续接、预算、诊断写入和工具回执。memory / capability session 的领域状态机不在本次合并范围内。工作区文件清理、记忆维护等后台 worker 不强制走主 Agent。

保留 #213 后的短事务及响应路径目标；不引入所有数据库写入都经过一个全局队列的方案，也不为了架构统一增加每个事件的 LLM 调用。[S02]

## 4. 删除清单与禁止范围

### 4.1 必须有实际删除结果的目标

| 删除或替换对象 | 完成条件 |
| --- | --- |
| automation 对通用 work 时间等待的驱动 | 单一 work 时间驱动已经接管，开关和重启测试证明不会遗漏原本可唤醒的 work。 |
| scheduler 中的主 Agent 装配与平台 sender 实现 | 通用恢复和现有投递边界已接管；原 scheduler 分支被删除，而非保留后再外套 facade。 |
| scheduler 对 `chat._active_work` 的直接写入 | 统一绑定入口接管，跨 task 输入和退出注销测试通过。 |
| `SubagentScheduler(app: Any)` 的服务定位方式 | 注入实际需要的依赖；child 执行不再从 scheduler 直接读取 chat 私有方法。 |
| 多处重复的 control 绑定、续租、failure recovery 和 release 骨架 | root / child / 同步 invocation 的差异仍可表达，但公共退出过程只维护一份。 |
| `MainAgentTurnService.run()` 自我递归式 activation 初始化 | 准备后执行入口明确，既有同步结果缓存与回放仍有效。 |
| automation 经 `_agent_runner.main_contract.chat._main_turns` 取服务的绕路 | 装配根注入共享服务；全部引用检查后移除仅用于绕路的 runner / 字段 / setter。 |
| 被新路径完全替代的旧辅助类、转发函数与兼容分支 | 当前仓库调用、公开 API、持久数据读取及插件使用方已分别核对。 |

### 4.2 不应删除的内容

不能直接删除 automation 的 DSL 执行器、计划/run 状态机、child 的独立 lease、实际投递回执、unknown 状态、generation / fence / cancel_epoch、内存领域的 session，或已承诺的工具别名。

例如 `yuki.generate` 和 `yuki.agent` 当前映射到同一 handler，本来就没有两套执行实现。保留一个别名映射可以满足既有脚本兼容，不必为了少一行映射破坏持久任务。[S14]

兼容性的两类情况必须分开：

- **无外部消费者的私有 Python 路径：**所有内部引用迁完后删除，不建立无限期兼容层。
- **持久 journal/source/cursor 与公开插件或工具接口：**保留兼容读取或显式版本迁移，不能用“新架构更干净”跳过旧数据。

### 4.3 本轮不做

不建设 universal scheduler、工作流平台、统一事件总线、全局可写 AgentState、全域 service locator、面向任意未来插件的抽象工厂，或所有后台 worker 的继承树。

不统一所有业务状态枚举，不大规模改数据库，不更换模型 SDK，不重写 memory，不以新增开关长期维持新旧两条执行路径。

不为满足净删行指标删除错误处理、证明性测试或关键合同注释。

## 5. 实施任务

默认按六个可独立验收的阶段完成。每个阶段迁移一个清楚的责任，完成即删除对应旧路径。阶段内并行实现互不重叠的文件，集成只由主 Agent 完成。下面的拟议模块名可按仓库真实结构微调，但不能把已确定的责任重新拆成更多平行 runtime。

### 阶段一：建立基线，先做低风险减法

#### T01 固定实施基线和路径清单

**输入：**本次核查 SHA、现行 `AGENTS.md`、开发合同。[S01][S02]

**工作：**

1. 更新实施分支；若 `main` 已前进，记录新 SHA 及其相对本核查提交的差异，优先处理已经改变的目标代码。
2. 通过本地检索列出真实生产入口：消息、SELF、automation agent step、plugin main-agent 调用、work resume、sandbox completion、child。
3. 对每条入口记录：来源校验者、上下文准备者、activation owner、lease 类型、runner、回执接受者。
4. 补充代码中实际存在的停用、取消、reset、恢复路径；不要凭空生成额外生命周期。

**交付：**一份可跟随每阶段更新的执行路径表，以及基线测试命令和原始结果。

**验收：**路径来自调用点，不来自文件名猜测；未完整核查的入口显式标出。不能把测试里的构造算成生产中的第二个执行引擎。

#### T02 固定行为回归场景

**工作：**在现有测试体系内建立后文第 6 节的跨入口验收场景。优先复用 fake model、fake clock、测试数据库和现有 harness。

**保留的现有起点：**

- `tests/unit/test_chat_context_preparation_gate.py`：完整文件已核查。[S16]
- `tests/unit/test_automation_run_admission.py`：接纳、原 run 重启及 orphan 处理部分已核查。[S17]
- 其他已确认存在的测试路径可作为补充入口：`tests/unit/test_automation_runtime.py`、`tests/unit/test_automation_timeout_certainty.py`、`tests/unit/test_capability_runtime_security.py`。这些不能代替缺失的跨入口断言。

**验收：**断言至少覆盖模型调用、工具副作用、实际发送、work/run 标识和持久状态；不能只断言接口返回了 `ok`。

#### T03 直接注入 automation 已在使用的主服务

**目标位置：**`automation/handlers.py`、`application/modules/automation.py`、现有装配根。

**工作：**

- 将当前实际使用的 `MainAgentTurnService`、主合同及必要执行依赖显式注入，移除 `_main_turn_service()` 中通过 `contract.chat` 找私有服务的绕路。
- 全局核对 `_agent_runner` 的读写、合同赋值和测试使用。删除确认只承担这种绕路的本地 runner 实例及关联 wiring。
- 保持实际模型路由、预算、工具清单和 main-agent shared service 行为；不要因为旧对象构造时写着 `AUTOMATION_AGENT` 就凭名称修改模型任务路由。
- 保持 `yuki.generate` 的单一别名映射。

**验收：**有合同与无合同的行为仍分别是正常共享调用和明确拒绝；不得增加缺服务时自行创建另一套 runner 的 fallback。

### 阶段二：来源恢复归位

#### T04 提升共享来源恢复，保留分来源证明

**目标位置：**`sandbox/source_recovery.py`、`runtime/work_scheduler.py`、`runtime/subagent_scheduler.py` 及第 T01 核实的 completion 调用点。

**工作：**

- 将消息与 SELF 已可共享的 canonical 恢复代码移至执行来源的共同业务位置。
- sandbox 只保留读取自己的 task / completion 记录并转入共享恢复的薄部分。
- 消息、SELF、automation、plugin 采用明确分支或已存在的 tagged union。共享检查组件，不把四种来源强行转成同一种消息。
- 将现有 `source_json` 解码放在受控边界内；下游优先接收已核验的来源视图，减少到处重新解释散装字典。
- 保持原序列化、source key 及 authority 字段兼容；本阶段默认不增加持久数据格式。

**必须删除：**已由共享恢复覆盖的重复 canonical / actor / source 解码片段及非必要转发函数。

**验收：**真人消息仍依赖原 event；SELF 无伪造 actor；automation 不经过 message-only 恢复；权限或 generation 变化不会被旧快照绕过。[S11]

#### T05 将投递移回已有业务与适配器边界

**目标位置：**`work_scheduler.py` 中的 `_resume()` 内的 `deliver_message/send`、`plugin_host/main_turn.py` 的原 invocation 恢复桥接 及现有 SocialService / PresenceRouter / OneBot 投递适配。

**工作：**

- 复用当前真正发送及回执记录的路径，删除 scheduler 里的平台 sender 实现。
- work resume 只选择既有交付语义：交付到原目标、同步返回原调用方、返回父 work；不新增平行 delivery framework。
- 保持原 source actor、presence、目标 conversation 和 effect key，不根据当前在线账号或最新消息推断原发送身份。
- 确认 draft、已接纳发送、tool-only 结果、同步返回的不同含义仍被保留。

**验收：**scheduler 不再 import 平台消息实现，也不直接发送；发送成功后清理失败不触发第二次发送，未知结果不被擅自当作未发送。

### 阶段三：收拢 activation 生命周期

#### T06 在现有 work_activation 内收拢公共骨架

**目标位置：**`work_activation.py`、`subagent_scheduler.py`、`main_agent_turns.py`、`chat.py` 中重复的生命周期调用。

**推荐实现：**

- 保留 `activate_work()` 作为 root scope 取得入口。
- 提取有限的公共“已取得 lease 后的 activation 绑定”上下文，或等价的显式函数。
- child 通过自己的 lease 取得函数进入同一骨架。不能让 child 再争用 root scope lease，也不能使 child 获得 root 的全部权限。
- 已由外层拥有的 activation 只能被内部调用借用，不能再次取得、再次释放或再次自主结算。
- 公共骨架统一负责 ContextVar 设置/恢复、续租监督的接入、活动耗时记录、失败恢复和最终释放。业务 completion 事实由真实执行/交付路径提供，不能通过一个统一 `delivered=True` 伪造。

不要设计 `activate(mode=..., is_child=..., owns=..., recovered=..., synchronous=...)` 的多布尔组合。使用真实存在的两三种明确进入方式即可。

**验收：**所有者清楚；无重复 release / budget 结算；租约失效后不能继续副作用；child 失败和 root 失败保留各自正确的后续动作。[S06][S10]

#### T07 收回跨 task 活动 work 绑定

**工作：**按 2.5 节处理 `_active_work`，让注册和注销只能跟随 activation 生命周期发生。

保留 `work_is_active`、输入关联等必要公开调用能力，但调用者不得拿到可随意改写的内部字典。`stage_work_input()` 的 actor 检查和显式 signal wait 匹配顺序保持原语义。

**验收：**新输入在另一个 task 到达时仍能找到正确 work；旧执行退出不删除新绑定；异常、取消、无 work 的普通聊天不会留下脏条目；进程重启后不依赖此索引恢复工作。

#### T08 简化 MainAgentTurnService 的持久执行分支

**工作：**

- 从 `run()` 移出 durable invocation 查找、退避读取、原同步结果回放和 activation 取得等通用持久编排，使其调用既有 work 层能力。
- 明确“输入已准备后的实际 runner 调用”，移除 `self.run(... work_control=bounded ...)` 形式的递归进入。
- 保留 `compose()` 的当前任务捕获、历史 projection、原生 continuation 及提交一致性行为；本阶段不改 prompt 内容或模型协议。
- 保留主调用通过 `runtime.work_control` 显式借用激活的方式；若临时保留 ContextVar 兼容读，明确唯一写入者及删除条件。
- 普通聊天没有 work、原 work 恢复、同步 invocation 已完成后回放，三者分别测试。

**验收：**`MainAgentTurnService` 不再通过 `_projections.database` 间接获得并亲自编排整个持久 work 生命周期；同步结果仍返回原调用者，已完成 invocation 不重跑模型或副作用。[S05]

**特别禁止：**为了获得统一入口，把上下文准备搬进空 work lease。必须保留第 3.3 节对应测试。

### 阶段四：削薄调度器，归位通用等待驱动

#### T09 WorkScheduler 只负责 work 调度

**工作：**

- 保留候选扫描、已有必要的恢复维护、容量控制及向共享恢复入口提交 work 引用。
- 将来源恢复后的具体调用交给 `services/work_resume.py` 或既有同责业务服务。
- 删掉 scheduler 对 `chat._models`、`chat._active_work` 等私有状态的直接访问。
- 输入准备函数迁回已存在的输入准备职责，或独立为很小的业务 helper。保留 durable input claim / readiness / generation / owner 校验及重新接纳流程，不另建一套队列。
- 保持 automation-owned work 仍由其原 step cursor 继续。不能让 WorkScheduler 和 AutomationWorker 同时作为同一 invocation 的完成者。

**验收：**仅阅读 scheduler 的依赖和主方法即可看清候选如何被取出；其中不再出现 AgentRuntime / ToolRuntime 拼装、平台 sender 或 prompt 构造。

#### T10 work 的时间等待仅保留一个生产驱动者

**默认归属：**WorkScheduler 拥有的独立时间维护循环。它与候选恢复循环由同一监督任务管理，不能把时间轮询放在等待长 root 恢复完成的循环内。若 T01 证明它在必要运行模式下并不启动，先修正其生命周期注册，使其能维护已经接纳的 work；不要先删除另一处扫描。

**工作：**

- 删除 `AutomationWorker._loop()` 的 work `deliver_due()` 调用及只为该调用存在的依赖。
- 时间维护循环每 2 秒调用 `deliver_due()`，候选恢复循环不重复调用；原 root 正等待多段模型请求时，其他 Work 的到期信号仍能登记原输入并入队。
- 扫描异常记录独立 health 类别并重试。监督任务发现任一循环意外终止时记录错误、收拢另一循环；关停取消并等待全部所属任务结束，不隐藏已经停止的时间维护循环。
- 不删除 automation 对自身 pending work 等待状态的查询，也不删除 `wake_claim` 及释放 claim 前后防丢唤醒的核对。
- automation 的 schedule 到期继续由 automation worker 扫描；它与 work 的 wait 到期不是同一个调度对象。
- event / plugin / owned-run 条件继续从真实事件到达路径匹配，不能全部改为周期扫描。

**验收：**单进程生产装配中 `deliver_due()` 驱动一处；多进程或重复信号下仍依赖原数据库原子性确保正确；关闭新的自主任务接纳不应意外停止本来必须维持的 work 等待服务。固定时钟和阻塞恢复器的真实 SQLite 回归须证明长 root 恢复不阻塞另一 Work 的时间信号，并验证循环意外退出的 health 与关停收拢。具体开关行为以 T01 核实的现行合同为准。

#### T11 拆分 child 调度和执行

**工作：**

- `SubagentScheduler` 改为显式依赖，而非 `app: Any`。
- 将 worker 专有的 brief、memory session、固定工具面与父任务结果返回放在 child 执行业务模块。
- 复用 T06 的生命周期骨架和已有 AgentRunner，不创建 `ChildAgentRuntimeEngine`。
- 保留 child 自己的 lease、祖先预算约束、父状态检查、取消命令和执行来源复核。
- 将 worker 所需的 memory / backend 构造能力从 ChatService 私有方法提升到当前业务所属的明确接口；只暴露实际需要的能力，不把整个 ChatService 改成 public service locator。

**验收：**scheduler 不直接读取 chat 私有方法；parent 取消后 child 不继续副作用；parent 等待时 child 能独立推进；关闭新的 child 接纳不能遗弃按现行合同仍需处理的已接纳 child。[S10]

### 阶段五：统一边界数据，不制造新状态机

#### T12 收拢入口上下文装配

**工作：**

- 对 T01 的全部主入口比较 `AgentRuntime`、`ToolRuntime`、`ToolActor`、来源字典中的重复字段来源。
- 对相同的 canonical identity、generation、执行引用和授权事实，建立一条宿主侧构造链；领域视图由同一份已核验输入派生。
- 不要求这些 dataclass 物理合成一个大对象。模型调用配置和工具执行上下文可以不同，只要不各自推断同一份 actor / source。
- 不把 per-turn memory、gateway、回执或 work_control 留在共享单例字段中。
- provider 路由、fixed tools、主系统前缀、当前任务消息的位置按原有合同保持。

**验收：**同一激活的模型调用、工具执行与回执使用一致来源；两个并行会话以及真人/SELF 交错执行不会串上下文。重构前后主工具声明和稳定前缀不因入口变化而分叉。[S05][S12][S14][S15]

#### T13 用既有 ActivationOutcome 收敛退出语义

**工作：**

- 公共 activation 返回现有 `ActivationOutcome`；真实交付结果、同步返回值仍各有适当载体。
- 把散落的“等待是否等于 running”“让出是否等于 failed”等映射收敛到来源桥接边界。
- `AgentRunResult.work_state` 等字段若只是派生投影，可以保留到调用迁完；不得成为另一处可写 work 状态。
- automation run / step、initiative feedback、child finish 分别消费明确结果，不依赖错误消息字符串推断成功。
- 保留 `WorkActivationHandled` 与 `WorkRecoveryDeferred` 区别，除非新控制流已完整证明同等语义；不能用一个 `except Exception: failed` 代替。

**验收：**segment yield、等信号、等用户、可重试错误、真正失败、unknown side effect、取消可以被区分；同一退出不会在多个入口重复计费、重复结算或重复报错。[S07]

#### T14 迁移其余主调用入口，拒绝旁路

**范围：**T01 实际找到的 plugin main-agent 调用、plugin background、sandbox completion、SELF resume。

**工作：**

- 把它们接入已经落地的来源恢复及 activation 边界，不再保留一条旧的平行执行路径。
- 同步 plugin/automation 调用采用明确的借用或自有 activation 关系；不能从环境 ContextVar 意外继承另一个调用方的 work。
- 不改变插件 SDK 的公开请求/结果合同；必要的兼容适配留在边界，内部立即统一。
- 插件审批、delegation、用户中断和 source generation 必须在恢复与 dispatch 边界仍能生效。

**验收：**逐入口提供同一行为矩阵结果。本次静态核查没有逐行审完全部插件和 completion 实现，实施者必须先完成 T01 的真实调用清单，不能凭本文推断这些入口已经全部一致。

### 阶段六：删干净，验证，再合并

#### T14a 完成共享 runtime 装配与剩余边界

- 创建并注入唯一 YukiRuntime，收回 work/child 调度启停与健康，主聊天、SELF、自动化和插件复用它拥有的共同主服务。
- 公共 active bindings 由 activation 登记/注销，旧 Chat 字典及 scheduler 直接写入清零；跨 task 查询保留受控接口。
- ContextAssembler 的结算移动到调用边界，原工作等待准备仍续原 ID、期限与预算。
- 将现有交付核对算法放入运行时效果查询边界，Automation 只消费类型化结果，测试保留原交付断言。

验收：生产装配中只有一个常驻 runtime；两个会话互不串临时上下文；重启原 work 能恢复；调度器不持有完整应用或平台投递实现；新增模块必须删除实际旧职责。

#### T15 删除旧路径并同步当前合同

**工作：**

- 删除已经没有生产和公开消费者的旧 sender、runner 包装、重复来源 parser、转发 helper、过渡 setter / facade。
- 删除纯粹服务于旧架构的重复装配测试，保留并迁移其中仍有效的行为断言。
- 更新当前架构文档的执行路径、所有者表、恢复说明和开发约束；不建立长期并存的新旧架构两本手册。
- 如移动内部模块，迁移所有内部 imports 后删除兼容 re-export；外部插件及持久数据合同另行处理，不能混为一谈。

**验收：**不是“旧路径还在，但暂时不用”；生产装配无法选择另一套旧执行内核，也没有偷偷降级到旧 runner 的 fallback。

#### T16 完成行为、恢复、性能和发布验收

**工作：**执行第 6、7 节要求，并提交删除前后的结构证据。

**交付：**分支、完整 commit SHA、删除/保留清单、原始测试结果、升级与恢复证据、性能对照、尚未验证的真实外部能力。

**验收：**仅以已观察到的结果标为通过；测试未运行或外部能力未验证要分别列明，不能用“代码看起来一样”替代。

## 6. 验收矩阵

以下测试是行为合同，不要求每行一个新文件。优先扩展现有场景，避免复制整套 harness。

| 编号 | 场景 | 必须观察的结果 |
| --- | --- | --- |
| A01 | 普通聊天，没有接受持续工作 | 不强制创建 work；模型调用和发送行为不被额外 planner 改变。 |
| A02 | 准备上下文时 reset 或另一 owner 取得 scope | 准备阶段不长期占空 lease / effect gate；旧输入不调用模型、不发送、不覆盖新 owner。[S16] |
| A03 | 消息触发 work，等待外部执行，随后恢复 | 恢复原 work_id、原 actor、原预算和 journal，不新增一件等价工作。 |
| A04 | 新消息在另一 task 中到达，恰逢旧 activation 退出 | 关联正确的 work；旧 finally 不删除新绑定；不丢 durable input。 |
| A05 | 同一 work 同时收到重复 completion / timer / wake | 既有 claim / lease / 去重生效；不重复产生副作用，等待信号消费可重复安全。 |
| A06 | root scope 被占用，child 尚可执行 | child 使用自己的合法 lease，不能因为统一而全局串行，也不能反向取得 root 权限。 |
| A07 | parent 取消、generation 改变或来源权限撤销 | 已过期 child 和旧 activation 不再触发工具副作用。 |
| A08 | automation 已建 run，尚未执行首步时重启 | 续原 run / cursor，即使超过 misfire grace 也不制造第二个 run。[S17] |
| A09 | automation 首次 cursor 写入失败或并发接纳 | run 与初始 cursor 原子；并发只有一个有效接纳。[S17] |
| A10 | automation step 已 dispatch，效果结果不明 | 保持 uncertain / 待核对，不清计数、不盲目重跑。[S17] |
| A11 | work 等待已登记，automation 正在释放 claim 时信号到达 | 不丢唤醒；依然由原 automation step 继续，不由两个 owner 同时结算。 |
| A12 | SELF、真人、定时委托、插件四种来源交错恢复 | 保持各自授权证明；不产生伪造真人事件，不借用当前聊天人的身份。 |
| A13 | 同步 invocation 完成，原调用方重试读取结果 | 返回原已保存结果；不再次调用模型，不再次发送。 |
| A14 | 模型 segment 到限 / 等信号 / 真预算用尽 | 退出语义正确；等待或让出不被记录成运行失败，累计预算不重置。 |
| A15 | 发送被平台接纳后，journal 或 cleanup 遇到数据库错误 | 原发送不重试成第二条消息；按已知事实保留并恢复状态。 |
| A16 | lease 续租失败、租约过期、旧 owner 延迟返回 | 先失效隔离再阻止旧执行；不能在 catch/finally 中恢复过期执行权。 |
| A17 | 隐私删除或 generation 改变发生在 context build 与 dispatch 之间 | 不使用旧隐私资料继续模型调用/工具输出；保持现有投影与诊断隔离。 |
| A18 | 运行开关变化与重启 | 分别验证“禁止新接纳”“暂停”“取消”的实际合同；不因新驱动归属漏掉已登记等待。 |
| A19 | 两个并行 conversation + 一个 child | memory、actor、ToolRuntime、gateway、work_control 不串；现有并发容量仍成立。 |
| A20 | main agent 经不同来源进入 | 工具声明、主合同 revision、稳定前缀符合现有共同主 Agent 合同；child 保持专用合同。 |

必须在副作用边界前后做故障注入，至少包含：准备后、模型请求前、工具 dispatch 前、外部效果接纳后、回执持久化前后、activation 退出时。断言应分别记录：模型请求数、工具调用数、发送数、work/run 标识、计数、cursor、effect certainty。

**不要将所有外部效果一律宣传成 exactly-once。** 对支持幂等键的效果验证幂等；对结果未知的非幂等效果验证“不盲重试、保留未知事实和恢复路径”。

### 6.1 已核实的开发环境和基础命令

项目要求 Python `>=3.12,<3.13`，开发依赖位于 `dev` extra。[S18]

以下全量命令只在终局执行一次；阶段内仅运行与改动相关的定向测试和静态检查，通过且未变化的检查不重复。沿用实施工作树现有 dev 环境；仅缺依赖时同步冻结锁文件。

```bash
uv sync --frozen --extra dev

uv run pytest -q \
  tests/unit/test_chat_context_preparation_gate.py \
  tests/unit/test_automation_run_admission.py

uv run pytest -q
uv run ruff check .
uv run mypy src
```

原方案的静态审查没有运行这些命令；本轮实施结果以交付记录为准。若实施分支的 AGENTS、CI 或依赖锁规定额外步骤，以该分支的最新合同补充，不悄悄缩减。

### 6.2 性能验证

在相同 commit 基线、数据库 fixture、配置、并发数和 fake/external 延迟条件下对比：

- 普通消息到首个模型请求、到首个发送的 p50 / p95。
- 相同负载下的数据库写事务数、锁等待及超时数。
- work 时间维护的扫描次数及空闲 wake 次数。
- 长等待期间新增模型调用数和 work/队列增长情况。
- 前台有请求时，automation / child 是否仍按原容量规则让出资源。

“只登记外部等待而无新事件”的固定测试场景，预期不应新增无目的模型调用；其他独立的合法后台模型任务需从计数中分开。

不凭两次手工计时宣布性能提升。若结果波动，保留原始样本并重跑；性能结论以数据为准。新增上下文视图不能偷偷变成每轮全库聚合查询。

## 7. 数据兼容与发布

### 7.1 默认不改表

这次主要是实现归属重构，不是数据迁移项目。默认沿用现有表与 key；不新增影子状态，不重新生成所有 work ID，不清空旧 journal / cursor。

必须保护：`work_id`、`source_key`、`effect_key`、`invocation_boundary()` 输出、script hash、generation、cancel_epoch、lease fence、原始 event/run 引用和累计使用量。[S05][S08][S17]

### 7.2 只有真实需要时才做 migration

若实施中发现某个权威关系确实缺乏可表达字段，单独写明：现有字段为什么不够、哪条行为需要它、旧记录如何读取、新旧代码是否能共同读写。增加 migration 和升级 fixture 后再实施。

不得回改旧 migration 或把补数据逻辑藏进 runtime 启动。不能为了移模块改变历史 journal 的 contract/hash 然后直接丢弃原执行记录。

### 7.3 升级恢复场景

发布前至少构造并保存以下旧版本数据库场景，切换到新代码验证：

1. 原 work 正等外部结果，completion 在升级后才到达。
2. work 带未消费输入、已完成工具回执和未交付的结果。
3. automation 已接纳 run，尚未进入首步，或已进入某一步。
4. root 正等待 child，child 有自己的执行记录。
5. 已接纳的外部效果尚未完全完成本地结算。

维护窗口中停止新接纳、退出原进程，再由新进程依既有 lease / recovery 接管。不要同时启用“旧 scheduler”和“新 scheduler”来试运行同一批真实任务。可对只读投影做对照，不能双执行真实副作用。

### 7.4 回退条件

无 schema / 序列化变化不等于自动证明可以安全回退。仍须用新代码写过的 fixture 验证旧代码是否能读取并继续；无法证明时只能回到已验证的兼容提交或采取明确的数据恢复方案，不能承诺任意二进制互换。

回退不得清掉已完成的效果记录，也不得让外部已经发生的动作因恢复旧备份而被自动重做。

### 7.5 本次发布授权与操作边界

用户已授权自行完成任务书、开发、新分支、PR、合并和按既有规矩上线，无需再次确认。先完成所有实现和终局门禁，再提 PR、等待最新 head CI、合并，使用合并提交构建 linux/amd64 的 ops-SHA 镜像。

部署依现行运维手册，并从当前 Bot labels 读取完整 Compose overlays 和最终生效的 bot image 定义。不能照抄手册中的旧 explicit-send 覆盖文件位置。仅替换 Bot，保持 SnowLuma、Manager、工作区和插件数据。停 Bot 后用 SQLite backup API 备份主库及 participation 库，验证备份；不把线上全库扫描作为无负载成本的常规步骤。

核验镜像 revision、迁移、health、OneBot、runtime/child/自动化/插件恢复、错误和重启次数。不得发送未经单独授权的真实 QQ 测试消息。合成与只读恢复验证不冒充真人社交验收；不创建正式 Release 或 tag。

## 8. 如何判断真的变干净了

验收不是“多了三个新类”，也不是“文件名都改成 runtime”。

| 检查项 | 目标 |
| --- | --- |
| work 时间到期的生产驱动归属 | 明确的一处；不是一个全局万能 scheduler。 |
| scheduler 平台投递代码 | 清零。 |
| scheduler 写 ChatService 私有活动状态 | 清零。 |
| SubagentScheduler 全应用服务定位器 | 移除 `app: Any` 式访问。 |
| 公共 activation 清理骨架 | 一份，root / child 保留明确的进入差异。 |
| 自调用式 main activation 初始化 | 移除，准备和执行阶段可直接追踪。 |
| automation 获取共享主服务 | 显式依赖，不穿过 runner → contract → chat 私有成员。 |
| 持久 work 权威状态 | 保持原存储，不增加双写。 |
| 过渡双路径与旧 fallback | 迁完删除，不能长期保留。 |
| main vs child 的合同差异 | 明确且有测试，不能统一掉必要限制。 |

每阶段提交结构账本：删除的实现责任、移走的跨域依赖、新增的抽象、尚未消除的分支，以及为什么保留。

行数可以辅助评价，但不设“全仓必须删 30%”这样的机械指标。观察目标区域中重复编排、运行时对象和控制流分支的净变化。测试因补足行为合同而增多是合理的；只删注释、压缩语句或把代码搬到另一个巨型文件不算收益。

新增一个 abstraction 至少要满足一条：实际替代两个已有的同责实现，或封住本次已核实的跨层边界。不能以“以后可能扩展”为唯一理由。

### 8.1 最终交付清单

- [ ] 六阶段任务的完成/未完成说明及实际 commit。
- [ ] 每条生产入口的最终来源、activation owner、runner、回执接受者表。
- [ ] 删除清单和所有仍保留的兼容点说明。
- [ ] 第 6 节行为矩阵逐项对应的测试与原始结果。
- [ ] 旧数据库升级及原 work/run 恢复证据。
- [ ] 性能对照，或明确标记未测，不声称优化已证实。
- [ ] 当前架构文档和代码一致，无旧执行路径继续被生产装配引用。

## 9. 原方案静态核查范围与证据

本次通过 GitHub 连接器固定到上述 commit，核查了执行入口、root activation、child 调度与执行、自动化桥接及 worker、来源恢复、共享工具合同、持久 work schema，并阅读了两个关键测试文件的全部或指定片段。

这是针对重构决策的关键路径静态审查，不是对全仓每个函数、全部插件或所有异常交错的穷尽验证。未取得调用次数、LOC 净变化或性能实测，文中没有将这些数字当作已知结果。

原方案作者当时的本地 git 获取因无法解析 `github.com` 未成功，以下为该静态审查的历史证据。本轮实施已取得本地完整源码，使用分支 `codex/persistent-runtime-core` 完成调用检索、开发和验证；不得把这一历史环境限制解释成本轮没有 checkout 或没有执行测试。

证据中的代码链接全部固定到审查 SHA。使用方式：查看列出的符号及所在文件；不将会移动的 `main` 链接当成固定证据。

### 9.1 源码索引

- **S01** [仓库开发入口与验证要求][S01]：`AGENTS.md`。
- **S02** [现行开发合同][S02]：`docs/architecture/development-contract.md`。
- **S03** [架构文档入口][S03]：`docs/architecture/README.md`。
- **S04** [主 Agent 与持续工作合同][S04]：`docs/architecture/main-agent-runtime.md`。
- **S05** [MainAgentTurnService.compose / run][S05]：`src/qq_ai_bot/services/main_agent_turns.py`。
- **S06** [activate_work 与 current_work_control][S06]：`src/qq_ai_bot/runtime/work_activation.py`。
- **S07** [ActivationOutcome / ExitReason / RuntimeFailure][S07]：`src/qq_ai_bot/runtime/activation_outcome.py`。
- **S08** [既有持久 work、输入、效果和 journal 表合同][S08]：`src/qq_ai_bot/runtime/work_schema_v1.py`。
- **S09** [WorkScheduler 调度、恢复、投递与输入准备][S09]：`src/qq_ai_bot/runtime/work_scheduler.py`。
- **S10** [SubagentScheduler 与 WorkerBackend][S10]：`src/qq_ai_bot/runtime/subagent_scheduler.py`。
- **S11** [消息与 SELF 来源恢复][S11]：`src/qq_ai_bot/sandbox/source_recovery.py`。
- **S12** [ChatService._respond、输入归属与局部执行绑定][S12]：`src/qq_ai_bot/services/chat.py`。
- **S13** [AutomationWorker 的计划租约、等待与恢复][S13]：`src/qq_ai_bot/automation/worker.py`。
- **S14** [AutomationCapabilityHandlers.agent / _main_turn_service][S14]：`src/qq_ai_bot/automation/handlers.py`。
- **S15** [共享 MainAgentContract 与冻结工具清单][S15]：`src/qq_ai_bot/services/main_agent_contract.py`。
- **S16** [上下文准备不得阻塞 reset / 新 owner 的测试][S16]：`tests/unit/test_chat_context_preparation_gate.py`。
- **S17** [自动化 run / 初始游标 / 重启恢复的测试][S17]：`tests/unit/test_automation_run_admission.py`。
- **S18** [Python 版本、开发依赖与检查配置][S18]：`pyproject.toml`。

---

**实施原则：把已经存在的共同执行内核真正用到底，删除外围重复实现；保留那些表达真实业务差异和恢复事实的边界。**

<!-- 固定提交的源码证据 -->
[S01]: https://github.com/YuanYeYouTao/Yuki/blob/b1382cf80429d82e5071275d5bc97f594209ed0b/AGENTS.md
[S02]: https://github.com/YuanYeYouTao/Yuki/blob/b1382cf80429d82e5071275d5bc97f594209ed0b/docs/architecture/development-contract.md
[S03]: https://github.com/YuanYeYouTao/Yuki/blob/b1382cf80429d82e5071275d5bc97f594209ed0b/docs/architecture/README.md
[S04]: https://github.com/YuanYeYouTao/Yuki/blob/b1382cf80429d82e5071275d5bc97f594209ed0b/docs/architecture/main-agent-runtime.md
[S05]: https://github.com/YuanYeYouTao/Yuki/blob/b1382cf80429d82e5071275d5bc97f594209ed0b/src/qq_ai_bot/services/main_agent_turns.py
[S06]: https://github.com/YuanYeYouTao/Yuki/blob/b1382cf80429d82e5071275d5bc97f594209ed0b/src/qq_ai_bot/runtime/work_activation.py
[S07]: https://github.com/YuanYeYouTao/Yuki/blob/b1382cf80429d82e5071275d5bc97f594209ed0b/src/qq_ai_bot/runtime/activation_outcome.py
[S08]: https://github.com/YuanYeYouTao/Yuki/blob/b1382cf80429d82e5071275d5bc97f594209ed0b/src/qq_ai_bot/runtime/work_schema_v1.py
[S09]: https://github.com/YuanYeYouTao/Yuki/blob/b1382cf80429d82e5071275d5bc97f594209ed0b/src/qq_ai_bot/runtime/work_scheduler.py
[S10]: https://github.com/YuanYeYouTao/Yuki/blob/b1382cf80429d82e5071275d5bc97f594209ed0b/src/qq_ai_bot/runtime/subagent_scheduler.py
[S11]: https://github.com/YuanYeYouTao/Yuki/blob/b1382cf80429d82e5071275d5bc97f594209ed0b/src/qq_ai_bot/sandbox/source_recovery.py
[S12]: https://github.com/YuanYeYouTao/Yuki/blob/b1382cf80429d82e5071275d5bc97f594209ed0b/src/qq_ai_bot/services/chat.py
[S13]: https://github.com/YuanYeYouTao/Yuki/blob/b1382cf80429d82e5071275d5bc97f594209ed0b/src/qq_ai_bot/automation/worker.py
[S14]: https://github.com/YuanYeYouTao/Yuki/blob/b1382cf80429d82e5071275d5bc97f594209ed0b/src/qq_ai_bot/automation/handlers.py
[S15]: https://github.com/YuanYeYouTao/Yuki/blob/b1382cf80429d82e5071275d5bc97f594209ed0b/src/qq_ai_bot/services/main_agent_contract.py
[S16]: https://github.com/YuanYeYouTao/Yuki/blob/b1382cf80429d82e5071275d5bc97f594209ed0b/tests/unit/test_chat_context_preparation_gate.py
[S17]: https://github.com/YuanYeYouTao/Yuki/blob/b1382cf80429d82e5071275d5bc97f594209ed0b/tests/unit/test_automation_run_admission.py
[S18]: https://github.com/YuanYeYouTao/Yuki/blob/b1382cf80429d82e5071275d5bc97f594209ed0b/pyproject.toml

## 10. 本轮实施账本

实际入口、职责删除/保留、行为矩阵和验证边界记录在
[共享 Runtime 实现记录](../operations/persistent-runtime-20261001.md)。

T01–T15 的实现按该账本归属：显式主服务依赖、共享 canonical 来源恢复、调度/执行拆分、
共同激活退出、受控活动索引、非递归 invocation、唯一时间驱动、同 Runner 的 child、
上下文准备结算和类型化效果查询均已迁移。主体身份仍沿入口已经核验的内部事件、
ToolActor 和来源事实派生，不为统一 DTO 再建一套权限对象；每次激活的模型视图与工具视图
可以分开，不能自行猜测 actor。状态投影只在来源桥接解释，原 ActivationOutcome 和持久状态
继续区分等待、让出、暂停、取消与未知效果。

T10 的最终时间驱动位于 WorkScheduler 的独立维护循环，候选恢复循环不再轮询等待。
监督任务负责两循环的意外退出与关停收拢，二次关闭取消不打断正在进行的收拢。
终局核查先复现长 root 恢复阻塞 timer，修正后 `test_runtime_scheduler_boundary.py` 的
8 项定向回归通过（2.50 秒），覆盖阻塞期间到期、重复交付、接纳开关、扫描错误隔离、
循环退出可见、二次取消期间收拢及既有媒体交付合同；源码检索
确认生产 `deliver_due()` 调用唯一。该结果是本地定向证据，不代表最新 PR CI 或上线完成。

T16 区分代码验证、隔离旧新数据库试验、离线性能样本、PR CI 与生产部署。
未测的锁竞争/生产延迟和真实 QQ 验收明确保留边界；不因共享 Runtime 已装配就宣称全部性能
或外部社交效果已经证实。合并和上线必须由最新 PR 与实际镜像/数据库/健康证据确认。
