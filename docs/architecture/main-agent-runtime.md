# 主 Agent 执行与恢复合同

本文定义当前执行合同；实际部署版本以运行镜像和提交记录为准。

## 执行与来源

普通聊天、主动群聊、自动化、插件通知自主轮和 SDK 的 Yuki 生成使用
`MainAgentTurnService`、`AgentRunner` 和 `MainAgentBackend`。自动化不再拥有第二套
模型工具后端、名称映射或参数转换。独立 agent_sessions、子 Agent 合同和辅助模型不属于主合同。
应用装配采用同一个 `YukiRuntime`，持有共享的主 TurnService、Runner、固定工具合同及
Work/子任务调度器生命周期。Chat、SELF、自动化和插件主调用使用这个实例；自动化显式
接收共享 TurnService 和合同，不再构造只用于取得 Chat 主服务的 Runner。
Runtime 不持有单一当前 Conversation；每次调用仍显式传递其原来源、权限、场景和执行 ID。
插件注册完成后冻结主工具清单，再启动 Runtime 的恢复调度器和插件后台主调用。
legacy/semantic 自主机会均以正式 SELF 来源进入这条执行链；
真实 QQ 小范围社交效果验收仍需单独记录。

模型侧每项业务只有一个公开名称和参数合同。冻结清单只来自主工具注册表，不追加 DSL、MCP
或插件的自动化别名；目录查询不会加载工具、改变声明或提升权限。合同版本 6 为信号等待参数建立
明确的新链边界，版本 8 为 Work 的只读目录，版本 9 为子任务真实分页建立新链边界。
续跑仍保留执行记录、预算和已提交回执，不改写旧 Provider 请求或重跑已有操作。

运行状态用 `state_scope=current_activation` 区分本轮激活与持久 Work 生命周期。
`no_active_work` 只表示本轮没有激活的 Work，不表示过去未接纳工作或工具失败。
没有当前 Work 时，`recent_work` 保留最近一项当前来源可见 Work 的真实 ID、状态和目标短线索，
用于识别已有工作，避免重复创建；三项执行计数和创建者等详情使用既有查询工具按需读取。
有当前 Work 时保留其完整目标与已设置的汇报模式，不混入另一项工作的状态。
未设置的 work ID、goal、reporting 和空 `available_work` 不占常驻提示。
`available_work` 至多 16 项，只提供内部 ID、状态、目标摘录、创建者显示名和 `has_wait`。
两种目录投影的目标摘录最多 160 字符，available 目录显示名最多 64 字符；`goal_complete=false` 明确表示
摘录不完整，不能代替原目标。读取使用有界 SQL 最小投影，不逐项预载完整等待 JSON。
续接目标或等待内容不清楚时先用 `get` 读取完整 goal 与 wait，活动 Work 原目标不截断。
目录中的 revision、owner/Conversation/generation、来源与审计时间等详情不常驻 Prompt，
需要核实再调用既有 `task_control(action=get/list)`；get 同时提供存在的完整等待登记。
后端身份、权限和恢复事实仍保持完整。
自动注入按原 actor、Conversation、generation 及执行来源核验可见范围，
不因为查询工具能读全局目录就自动注入其他会话的工作。
不注入完整 journal、工具正文或其他主体的任务。既有请求链只追加当前视图，不重写历史状态。

人物与场景块保留当前说话人、群及可信引用；关系仅在 `context.relationship` 提供当前阶段、
风格和未验证陈述规则，不在人物块重复注入好感/信任等数值详情，详情使用 `get_relationship`。
给模型的资料按当前有效内容投影：空短记录只保留 CAS 必需的 slot/revision，非空记录保留内容和到期信息；
精确重复的称呼与人群身份、空 group card、空投递媒体列表不重复呈现。
这只精简新请求的文字视图，不改变原记录、未知业务 JSON 或旧冻结快照。
短期记录继续保留 CAS revision，权限、时间、当前 Work、最近投递、当前媒体和固定工具合同
不因显示精简而改变。文件、终端、完整自动化目录和长期记忆均使用已有工具按需查询。

主 Agent 的 `task_control(action=get, work_id=...)` 按原内部 Work ID 只读查询全局安全目录，
与 Automation 目录读取一致，不按创建者、当前会话或 generation 过滤；
`task_control(action=list)` 有界分页列出同一目录，`status` 默认 `active`，
查已完成、失败或取消的工作用 `terminal`，同时看活动与终态用 `all`。
`limit` 默认 8、允许 1–50，`cursor` 用于续页，不把这个简表宣称为完整任务存档。
全局投影只提供安全任务元数据和创建者标识，不提供 journal、工具正文或跨会话聊天内容。
子 Agent 使用固定工作工具子集，其 `get` / `list` 只查看自身 Work，不继承主 Agent 的全局读取。
两个查询不要求先接纳 Work，不获取执行租约，不追加输入、恢复工作或发放预算。
进度、用途、结束时间和已有结果的追问先查询原 Work，不因此另行 `accept`。
终态查询不会复活旧任务；真实独立新请求仍可接纳新 Work。
持久状态是查询时刻的生命周期证据，具体执行与交付仍由原回执证明；
查询不扩大修改权限、不解除未知效果围栏，也不把口头承诺当作已执行事实。

当前群的 active 自动化目录不再预取进每轮主 Agent 上下文。明确涉及自动化的当前请求由
`automation_list` / `automation_get` 按需核实；读取目录不授予修改权限。普通聊天、SELF
自主轮、插件唤醒和定时 Agent 不因群里存在任务而自动收到任务清单。

Person 自动化以创建者的当前身份和权限执行，与其在目标会话发起普通聊天一致；不再交集创建时的
工具白名单，不要求 TaskSpec 选择 capabilities。每次模型请求和工具执行重查任务有效性、
所有者、租约、权限及会话 generation，权限变化终止旧授权下的执行。插件本身的安装批准边界不变。

`ToolActor` 传递真实主体和执行来源。聊天保留内部事件 ID，定时任务保留 run/step/execution ID；
不能生成假的 InboundMessage、QQ 消息或提及来通过校验。定时来源仍为 scheduled_automation。
历史、记忆的主动补查使用同一读取授权；context 配置只控制首次预取。记忆写入仍需真实证据，
定时任务通过 evidence_event_id 引用创建者在当前会话的原始事件，不把任务指令伪装成人类发言。

SELF 使用数据库内唯一的 `PrincipalRef(self, self)`，不携带真人 user/person。自主入口的
`ToolActor` / `TurnAuthority` 使用可信 `initiative_run_id`；SELF 定时入口使用真实
`automation_run_id` 与 `scheduled_automation`，沿普通自动化的 claim、run/step 游标和
主 Agent 工具合同执行。`SelfInitiativeTrigger` 固定原 Conversation、
generation、Space、Presence 与群传输目标。目标成员和资料来源都不授予其私人权限。
`ConversationTurnSnapshot` 的事件 ID 与 initiative run 严格二选一；无事件工作不得借
最近真人消息补锚点。SELF 主入口首次只预取当前群与该群可见的 SELF Memory。
每次模型请求、工具执行和发送准备仍复核原 run 与场景权限。SELF 自动化还固定创建时
Conversation/generation、Space、Presence 和群绑定；场景失效即阻止执行。当前 SELF 社交工具只允许
当前群发送、通讯录、历史和成员读取；不支持自动结构化 @、私人目标、撤回或戳人。
完整声明不随主体改动，执行处拒绝不获准的能力；详见 [语义参与接入](semantic-participation.md)。

模型侧只用 `send_message` 发送可见内容：省略 target 时发到当前群或私聊，显式 target 可指定
其他人或群，后端选择私聊或群聊路由。语音、表情、附件、引用、提及均由同一工具的参数表达；
真实网关发送和持久回执组成普通工具结果，`uncertain` 不自动重试。一次循环可多次发送，也可
完全不发送；模型最终正文只供内部收尾，不会自动发给用户。`report_progress` 和本轮最终回复
效果队列不再是模型可调用合同。工作完成可以安静结束，不需要人为制造发送回执。
直接用户消息若生成了非空最终正文、却从未尝试 `send_message` 且没有已接纳工作，
Runner 在原循环中给一次未送达反馈；再次遗漏则记为失败，不把它标成已回复。
这类失败单独记为 `unsent_final_response`（Agent 输出阶段），不归因于 Provider 请求失败。
空最终正文可表示明确沉默，不强制每轮发信；已有发送尝试、已接纳工作与其他入口不被
这条聊天纠正逻辑盲目重发或代发。
主 SELF 另将未发送的非空最终正文作为内部反馈交回同一循环，由 Agent 决定发送或
`NO_REPLY`；调度器不自动转发。`NO_REPLY` 是合法完成，`return_to_caller` 不制造真人
收尾通知。作为 `send_message` 正文的完整 `NO_REPLY` 会在发送和回执前被拒绝；SELF 的子
Agent 返回内部任务结果，不继承主 SELF 的发送要求。
单次 `send_message` 的纯文本复用原回复分条规则（换行、结构与字数上限），
逐条持久化发送回执；模型主动多次调用仍是独立的发送。部分发送未知时停止后续
分条，重入只读取已有回执，不盲目重发。媒体和语音仍按单次发送处理。
`send_message` 是显式会话交付，不要求先 `task_control.accept`；它仍被视为真实副作用，
目标权限、路由、mutation 围栏和投递回执均在执行处核验。其他业务副作用仍按工作接纳
合同执行。社交工具不再对发消息或戳一戳施加额外的每目标/全局分钟频率上限。
已接纳 Work 也不额外施加发送次数或 16 条/60 秒窗口；投递意图只负责身份、内容冲突和
回执防重放，不是频率配额。已有明确未派发的 blocked 意图可按原身份恢复；accepted、
dispatching 和 unknown 不因此获得重发资格。平台实际拒绝仍按真实结果处理。
原 Presence 的实时连接在发送前断开时，原 Work 可按持久检查点短暂重试；
明确暂停、路由歧义和已进入 dispatching/unknown 的发送不因此获得重发资格。
分条只在实际发送该条前准备子回执；确认失败与结果未知分别报告，不把发送前拒绝误报为未知。
模型提供给 `send_message` 的文本在分条、回执准备、媒体生成、网关发送和账本写入之前统一净化；
内部历史事件前缀不会进入新的可见消息。既有 QQ 消息、原始账本、Rollup 和记忆不追溯改写。
可选 `<yuki-state>` 控制尾段同样在发送、语音与最终正文净化时移除，格式错误不额外重试模型。
主 SELF 自报按实际 run、请求序号和响应 ID 保存；它不是别人行为的证据，也不允许工作者代报。
普通主轮的稀疏自报使用原真人入场冻结的讨论/对象及激活、请求顺序；不为保存意愿创建
Work 或假 initiative。`join/stay/quiet` 不必逐轮填写，没报不清空；quiet 只收起本单元自身
意愿，不解释成用户全群停止。旧响应不得按较晚到达时间覆盖新意愿。派生保存失败不改变
已确认发送，不重跑模型、不重发或退还预算。
移除控制尾段后，普通轮的空正文及内部 `NO_REPLY` 都按原沉默通路结束，不补答、不外发。
真实发送效果保持原 Space/person 投递目标，讨论关联只使用原持久入场/source；来源失效或
绑定缺失就保持未知，不能从后来当前话题补造。查询和反馈沿原主执行链，不增加逐轮审核模型。

新建 `generated`、`agentic` 和 `auto` 任务统一编译为一个 `yuki.agent` 步骤，
由同一主 Agent 显式调用 `send_message`。`generated` 只是保留的任务策略输入，不再走
生成正文后追加 DSL 发送的路径；固定工具 schema、提示词前缀和 Provider 设置不因此改变。
静态字面量提醒由 DSL 调用 `social.send_message`，与主 Agent 使用相同的 Social
路由、净化和持久发送回执，不进入模型循环。SELF 与用户创建的提醒走同一合同；
SELF 只可投递到原群。`delivery=none` 只执行内部工作，不会产生可见提醒。
模型自动化要求开启 `runtime_work_enabled`；关闭时在执行前返回
`automation_runtime_required`，不会先发送再因缺少持久工作而报未知。明确交付任务缺少
canonical Conversation 时同样在模型调用前阻断。

内部 DSL 的 `delivery_target` 记录已经解析的交付要求，不改变运行上下文的归属。
`self_private` 核验创建者 Person，`current_group` 核验当前 Space，`none` 允许安静完成。
存在交付要求时，只有原持久工作 completed 且该目标的完整工具回执与真实 Social 发送一致，
才确认交付；分条和文件附言不能仅凭其中一项成功通过。确定失败可由后续完整发送解决，
未知结果不能被另一次成功覆盖。内部最终正文始终不会被外层补发。
核验在同一只读快照中读取工作与回执，不持 SQLite 写锁；原工作已归档、完整工具记录
不可用时返回未知，不凭幸存的单条成功回执推断全部交付完成。
这些事实由 `runtime/effect_queries.py` 的 `RuntimeEffectQueries` 读取并返回类型化
交付结论；Automation 消费结论，不自行解释内部 Work journal 或 Social ORM 状态。
查询不补写工具结果、解除未知效果围栏或改变 Social 执行语义。
这证明传输事实，不证明内容在语义上已完成目标；不按措辞猜测进度或最终回答。

同步 `return_to_caller` 调用在显式 complete 成功后仍取得调用方所需的真实内部模型结果，
不将工具提议或内部正文补发到 QQ。单段额度先耗尽时，原 complete 参数保留在原 journal
的 progress 中，工作排队等待结果；下一段收到新输入即撤销提议，否则重新核验当前来源、
权限和原回执后恢复完成条件；取得内部模型结果后再次复核，正文非空也不能越过新未决
回执。模型期间出现新输入时，空响应异常同样交回 paired/queued，不向调用方报告 completed
或保存成功结果。这个提议不授予执行或交付权限，也不增加或重置预算。
完成条件已复核且原 Work 有 confirmed 消息发送事实时，内部最终结果可以为空；仅存
sent_messages 计数或缺失、失败、未知回执不能取得此资格。无已确认发送的同步调用仍须
返回实际内部结果，不能凭 complete 提议把空模型响应当作成功。

历史脚本保留原 run/step、script_hash、工作 ID、预算与游标。旧模型正文尾发不再执行：
可以定位原工作及目标且已有完整成功证据时，记录跳过该尾步；未知则停在 uncertain，
无确认或结构变形则 blocked，要求更新任务。尚未派发的旧 `yuki.generate + 发送` 脚本
直接要求更新，不改写资料包或重建执行链；已开始的主工作仍按原身份恢复。
新建和更新也拒绝模型输出通过 DSL 文本、语音、插件发送等旁路外发。

Social 回执来源键保留 128 字符以内的旧键；超长完整内部身份统一映射为版本化 SHA256。
写入、查找和交付核验使用同一映射，不截断原执行 ID，也不改变任务恢复和预算身份。

调度器的结构化步骤、执行游标、原有业务集成和发送回执保留；它们不再构成另一套模型工具声明。
旧存储的 Agent `allowed_capabilities` 元数据由迁移删除；主 Agent 的固定工具合同不读取
TaskSpec 内的工具白名单。

自动化意图和自然语言时间由主 Agent 根据真实请求与上下文解释；歧义由 Agent 澄清。
后端不使用关键词或正则判断是否应创建任务、不重新解析原话中的时间，也不按成功措辞拦截正文。
后端继续校验结构化 schedule、时区、当前主体权限、目标来源及幂等；Agent 以持久化回执和真实 ID 报告结果。
`automation_list` 提供不按当前发言人过滤的全局任务简表，默认只列 active，并返回创建者的稳定
Person ID、外部账号 ID 和当前可用显示名；`automation_get` 使用相同的创建者投影。读取不授予修改权限，
更新、暂停、恢复、取消、立即运行和执行历史按真实 actor 授权：普通用户只能管理自己的任务，
超级管理员可以管理全部任务。管理员更新不改变 canonical 创建者，后续执行仍按原创建者的当前权限
重新核验，不能借一次管理操作永久提升任务权限。当前群自动化目录不再常驻注入；需要时按需调用原工具核实。

## 所有者与恢复

| 来源 | 恢复所有者 | 交付所有者 |
| --- | --- | --- |
| 用户消息 | WorkScheduler 选择，WorkResumer 恢复原 work/generation | Agent 显式 `send_message` |
| SELF 自主群聊（legacy/semantic） | Host 接纳记录 → WorkScheduler 选择、WorkResumer 恢复原 initiative/work/generation | Agent 显式 `send_message`；允许 `NO_REPLY` |
| 自动化生成、Agent 步骤 | AutomationWorker，原 run/step 游标 | Agent 显式 `send_message`；旧模型尾发仅核对回执后退休 |
| 插件通知自主轮 | PluginBackgroundTurnWorker，原 job/event | Agent 显式 `send_message`；插件自己的通知 outbox 独立 |
| 有真实事件的 SDK 主调用 | Host 持有正在运行的协程，后续由 WorkScheduler 选择并交回插件 Host 恢复 | 原调用方查询结果 |
| 子 Agent | SubagentScheduler 选择，SubagentExecution 恢复原父子关系 | 父 Agent 验收后交付 |

调度器负责有界选择、时钟维护和循环生命周期；来源重建、权限复核、工具后端与发送
编排归应用服务。`WorkResumer` 通过显式依赖取得原来源和 Host 回调，不接收整个容器。
WorkScheduler 的监督任务拥有候选恢复循环和独立时间维护循环；后者每 2 秒调用
`deliver_due()`，是单进程唯一的 Work 时间驱动。候选恢复等待多段模型请求时，时间维护
仍能按原 Work 登记到期信号并入队。扫描异常记录 `wait_error_category` 后重试；任一循环
意外终止会记录错误并收拢另一循环，health 的 `running`、`wait_running` 显示停止状态。
关停取消监督任务并等待两个所属循环结束；二次关闭取消不打断正在进行的收拢，再释放
它们使用的依赖。
Host 同步主调用的持久接纳由 `DurableInvocations` 管理，沿原 invocation boundary 查询和
接纳 Work 后进入已准备的执行路径，不递归调用主入口，也不重置原身份、历史和预算。
根 Work、自动化和子任务共用 `bind_work_activation` 的 ContextVar、续租、计量、恢复和
释放边界；来源核验和所属执行的结算仍使用各自原有合同。
`ActiveWorkBindings` 仅登记当前活跃的进程内控制器，供跨任务输入定位原 Work；
退出时按同一控制器身份移除。它不是授权来源，持久租约、generation 与原来源仍是执行围栏。

SDK 回调等待约 5 秒可返回 `work_id/state/pending`；等待不是模型正文。
`agent.result(work_id)` 不依赖已经退出的 ContextVar，但核对插件所有权、批准版本、权限和 generation。
等待原因随结果返回；`agent.resume(work_id, text, request_id=...)` 将补充资料追加到原工作。
相同 request_id 重放幂等，不同内容冲突；不新建 work，不重置历史或预算。
接纳前超时取消准备，接纳后回调退出不取消 Host 工作。完成句柄至少保留 7 天，超过保留期查询明确返回已归档。
插件停止时取消进程内任务，保留已接纳工作和回执；失效授权不得继续执行或自行发送。
没有真实事件的 SDK 调用仍须用目标明确的通知 API，禁止补造用户。
主 Agent 正在执行插件工具时，插件递归调用主生成会被拒绝，不能抢占父锁或绕开总预算。
嵌套的独立 agent_sessions 保留自己的提示词和历史，每次模型尝试仍计入发起工作的预算。

恢复从原 journal 核对执行身份、预算、调用和回执；未决协议保持原签名和配对。
业务续跑应接当前获准聊天和必要任务材料，已用原文可退出私有执行尾部，不要求永久
恢复旧群史。改变已提交序列时使用显式新请求编排，不能混接新聊天与旧 opaque。
上下文分工规格见 [Work 上下文与普通聊天续接任务书](work-context-and-chat-continuation-taskbook.md)；
本地验证、合并及上线状态见 [实施交付记录](../operations/work-context-delivery-2026-10-03.md)。
结果由调用方取得不代表 QQ 已发送；只有实际网关回执可以确认发送。

Journal 保留 dispatched、response、paired 的独立持久边界。不可变媒体只批量插入缺少的 blob，
按引用差额增删当前 Work 的 refs；其 pending/staged 输入尚未并入 transcript 时仍保留媒体引用。
其他 Work 的引用不被当前保存回收。恢复仍按原 call、response、预算和执行回执，不因减少媒体
写放大而跳过请求意图、实际响应或成对工具结果的记录。

Work 发现首个 pending 输入尚未准备完时，立即按原 `WorkInputsPreparing` 退出本轮，
由既有结算提交 `waiting_external` 并释放 activation，不轮询等待附件处理。
准备提交按原输入 ID、Work 和 canonical generation 核验所有权，原输入的图像使用既有
媒体 blob/引用持久化；即使旧 activation 已退出或进程重启，也能恢复。准备完成与结算
在同一 SQLite writer 边界串行核验首个输入：先准备或先结算均续原 Work 入队；后面的
ready 输入不能越过未准备的首项。已准备或消费的输入重试只确认原记录，不覆盖内容或预算。

普通聊天的 context prepare 与前台 rollup 等待在 effect gate 外执行。准备完成后，
gate 内只复核 turn snapshot、read version 或原 Work source guard 并提交有界 projection；
模型请求继续使用同一来源核验。reset/privacy 在准备等待期间可取得 gate，失效的旧链
不能继续 dispatch 或发送。
`ContextAssembler` 只组装上下文并报告所需 rollup；`runtime/context_preparation.py` 的
`prepare_context` 负责依原 Work 停放、结算、恢复或采用现行 extractive fallback。
没有已接纳 Work 的准备继续使用前台路径，不为准备另造 Work 或获取执行租约。

普通聊天先只读检查同一内部 source key、actor 与交接边界是否有可选 Work；没有候选时
上下文准备不领取再释放空租约。只读结果仅用于安排准备，正式激活仍重新读取候选、
取得租约并执行原来源与状态核验，准备期间新增、取消或改向的 Work 不复用旧预检授权。
已占用或过时代际的作用域领取、失效租约的续期和释放可只读拒绝；真实变更仍使用原
writer 与执行时的 owner/fence/generation/期限条件，不以锁外读取代替 CAS。

尚无模型 journal 的原 Work 遇到 required rollup 时，在原 checkpoint 中记录准备水位与
原期限，交给既有 canonical rollup job，随即按原 `waiting_external` 结算并释放 activation。
WorkScheduler 每轮先只读发现至多 32 个已完成、失败或到期的准备，再在短写事务复核并
将原 Work 入队；空页无写事务。完成先于结算、进程重启和取消都按原 Work/generation
恢复，不补造输入或重置预算。真实原 Work 的压缩前置需求保留 REQUIRED 模型优先级，
已有 processing claim 和失败 backoff 不被接管；原期限或失败仍使用既有 extractive fallback。
未决私有协议恢复在群史准备前核原 journal、固定合同和持久来源依赖；合法时复用原
协议，不无谓准备最终不会派发的新群史。完整配对的业务恢复使用当前获准聊天与轻量
任务材料；整理只在完整往返安全点建立新输入，来源冲突保留明确链边界。

来源核验须覆盖实际提交的事件、观察、摘要版本和读取范围。只读快照准备结束后，在同一
新的显式读快照核验原执行租约、SQL 执行时的期限、generation、owner、来源 revision 和
隐私代次；纯检查不通过无变化 UPDATE 占用 writer。读结果不是写入授权，真实 journal、
投影发布和效果仍保留各自短写事务中的执行时围栏与来源 CAS；失败不推进指纹或选取记录。
未决协议恢复核原检查点，已配对业务续跑核当前合法视图；后台仅发布新摘要不意味着已选
旧快照失效，真实编辑、删除或权限变化仍拒绝。跨激活恢复按原实际选取的依赖核验，
不以未观察事件或后台派生摘要的标量变化单独拒绝。缺持久 guard 的旧 journal 保留严格
兼容边界；不能删除来源检查来获得缓存稳定。

群消息增量只在原本即将请求模型、工具往返完整配对的安全点读取，不因一般群聊唤醒
Work。steer 与普通观察按原事件顺序呈现一次，输入消费仍沿原 Work 输入 ID；候选失败
不推进已观察水位。临时工具尾部整理保留这一公共增量的原呈现，不先摘要掉再
作为未读事件重复追加。已付整理页尚未发布最终候选时，重开复用原 saved anchor、
页游标和引用；只有完整配对的业务恢复重新选择当前聊天。
真实 Work 的 projection CAS、来源依赖和 dispatched journal 同事务
发布，文件读取、散列及 JSON 编码在 writer 之前完成。

选取准备在显式读快照中确定本次缺失的 `(view_key, source_key)`，writer 只插新增项。
同 epoch 的既有冻结片段保持不变，显式容量或合同 epoch 边界仍保留原 first-selection
来源和覆盖口径；owner、scope、generation、事件身份冲突不能用忽略重复插入隐藏。
只有首次选中的摘要转移 parent artifact refs，写时批量核验真实 parent handles 已由
对应摘要保留，空集不删除；后续 journal 失败与 projection、selection、refs 一起回滚。
历史观察先读作用域 metadata 和完整来源闭包，再读取实际有效的正文；合法未选新
work-note 仍进入下一请求，未选 snapshot/摘要候选不能充当已观察覆盖。
metadata 分页限制单次读取，当前 JSON 来源图的总访问量仍可能随历史增长。

普通 journal 保存因来源标量版本变化而失败时，最多在 writer 之外用原入口授权与来源
guard 重新核验一次，再重备同一数据库保存；无原 guard 或真实来源变化保持拒绝。
重备不请求模型、执行工具、重发消息或重置预算，压缩的冻结来源 CAS 不使用此路径。

0082 为既有 revision 补齐 canonical owner、事件元数据更新、rollup 移出原会话的
触发器闭包，没有新增持久字段。升级和降级只创建/删除这四个触发器，不清理聊天、
Work 或发送回执。
旧镜像的启动检查要求旧迁移 head，因此回退到 0081 镜像前需用新镜像执行
`alembic downgrade 0081`，再替换镜像；不能直接恢复旧数据库覆盖上线后产生的事实。

Work 冲突在恢复记录和运维日志中保留受控的具体原因码，不把任意异常文本发到聊天。
`work_journal_source_changed` 若尚无效果或投递回执，可用原 Work ID 有界重试，并在新来源
上建立显式链边界；已有任何效果记录时暂停并提示核对状态，不自动重放已创建的自动任务、
已执行工具或未知投递。租约失效与其他冲突仍遵守原所有权围栏，不借来源变化扩大重试范围。

暂停通知交付是原 Work 的维护，不把它改为 running，也不消费 pending 输入或进入业务
结算，不计任务活动时间。同一暂停期间复用原通知身份与交付回执；真实恢复执行后再次
失败才登记新暂停通知。通知准备或收尾失败不覆盖原业务失败；已确认接受的通知按原
effect 修复交付记录，unknown 不获重发资格。

原任务要求沿 goal、trigger/input 和显式登记资料保留，不从第一条 user 猜测，不让新唤醒
替换原目标。保留要求不等于每次注入最初 compiled envelope；旧动态资料明确标为旧观察，
必要细节通过引用回读，当前授权在执行处核验。当前容量整理仍保存编译后的任务 anchor，
本次修复需把必要要求和原聊天分开。旧记录缺少 note 不成为统一停止门槛；无法从真实
来源安全取得要求时保留具体记录及执行事实，不能猜进度、清预算或新建替代任务。

协议检查点保留 opaque Responses item 的字段顺序；媒体外置及恢复不改变实际序列化
请求。主执行的新链继续使用完整固定工具声明与设置，禁止依赖 DeepSeek 的 tool_choice
控制执行。no-progress 的最后一次恢复响应不会执行本地函数工具。容量摘要使用同一连接的
独立无工具请求，不携带 native tools 或原 opaque continuation，不覆盖主链的 dispatched/
paired 检查点。真实主请求超预算且整理不能使其装窗时，暂停原 Work 并保留事实。
主请求仍合法时，摘要来源容量或未能缩小的软整理失败保持原链继续；非法摘要、来源、
隐私、权限与租约拒绝仍按原正确性边界处理。
成功候选保留原 brief、原 inputs 的完整追加要求与来源、所有未决效果及最近成功回执。
近期公开 call/result 对按完整请求的剩余容量保留；重复镜像只呈现一次，原始协议与完整
回执通过不可变对象引用保留，私有签名不会当普通文本交给摘要器。
摘要来源按规范化后的完整请求容量分页，单个较大公开记录以原来源编号的连续片段呈现，
不得截掉尚未处理的来源。已付摘要页的来源快照和游标随原 paired journal 持久化；真实
来源与隐私代次不变时，恢复复用已验证的页，不因分段重做付费工作。窗口调整也不重写
已付最终页的引用范围。只有全部来源与输入处理完毕，才验证并发布压缩候选。
目标水位是整理策略，候选只需实际缩小且装入完整可用窗口，不要求达到固定缩减百分比。
新链记录派生容量水位，后续按新增内容占用剩余空间触发整理，避免刚压缩后立即反复压缩。
摘要 schema 仅核验类型、非空、来源引用及仍有效指令完整保留，不以固定条数、短字符数或
另一层小字节常量拒绝合法结果；真实模型输出与协议存储政策独立核验。

数据库 journal 保存小清单，协议对象与媒体在数据库同目录的 `work-protocol` 中按内容 hash
保存；新增引用与 journal 在原租约的短事务一起提交。活动 Work 保留引用，归档与隐私清理
释放拥有者；GC 先取得无拥有者删除围栏，再在 writer 外删文件。对象资源配额和请求 token
容量分开；存储压力不能触发模型摘要。备份必须包含 DB 和协议/工具证据目录并核验引用。

Work 输入准备恢复使用 pending 且 ready 为 false 的部分索引发现有界候选；空轮询
不读取已消费或取消输入的历史页，也不申请 writer。准备 owner 不同或超过原 120 秒
时限的判定不变，unknown owner 的 SQL NULL 语义不变；取得 writer 后按原 ID、
state、ready、owner 和时限复核，保留原输入、Work 与累计预算。

Protocol GC 分 deleting 恢复与普通过期两个索引分支，先取有界 metadata 页再批读真实 refs；
owned 页也推进原进程内游标，固定 cutoff 和同排序高水位，次轮回访新插入及状态变化。
短 writer 重新核对原 metadata、期限和无拥有者条件并提交 deleting 屏障；文件锁只保护
实际文件查证/删除，释放后批量确认仍 deleting 且无拥有者的 metadata。GC 不持 writer
等待文件锁；publication 保留文件核验至原 journal/ref 提交的保护和 deleting 全集合检查。
文件线程在调用方取消后须真正收尾才释放原保护。普通不可变检查点条目可在原 chain
复用有界 digest/size 缓存，opaque 与会改变媒体外置的条目仍按原协议准备；缓存不代替
发布前真实文件身份/完整性核验。文件缺失重新准备，变化重新查证，损坏拒绝发布。

热配置 `context.window_tokens` / `context.work_window_tokens` 初始为 96000 / 128000；
普通历史整理使用独立 `context.compaction_window_tokens`，初始 90000；初始历史预算、
重启预取和 Rollup scope policy 取它与聊天容量上界的较小值。群史默认 0.90 / 0.60 对应
81000 / 54000 的整理水位，不能因上调主请求容量而推迟历史回收。
聊天/群史触发与目标初始 0.90 / 0.60，Work 使用独立热配置
`context.work_compaction_trigger_ratio` / `context.work_compaction_target_ratio`，初始 0.90 / 0.50。
两种摘要输出预算初始 32768。这些是可调政策，
不是模型硬上限或必须填满的长度。连接 Profile 的 `max_input_tokens`、
`context_window_tokens` 与输出限额独立核验，联合窗口才扣输出预留。
Work 整理同样取完整输入预算与 `context.compaction_window_tokens` 的较小值，再使用自身
触发/目标比例；默认基准 90000 对应 81000 / 45000。已整理或软尝试后的派生容量水位仍
用于等待真实增长；硬容量和整理策略分别核验，不要求为缓存填满窗口，也不因尚未达到
软目标拒绝仍能装入真实输入预算的请求。
普通初始历史的软整理不能制造业务等待：raw 连续完整且完整来源仍装入实际剩余容量时，
整理无候选或略超软目标可继续原准备；只有真实不足或 raw 未读完才建立必需覆盖等待。
固定必需资料超过软目标时使用实际容量回退，保留来源和原资料，不吞来源或隐私错误。
普通轮无需接纳 Work 即可在完整工具配对后的安全点整理临时尾部，建立合法新请求；
公共聊天与新观察保留原呈现，原件、发送回执和预算不变。辅助整理失败但原请求仍 fit 时
继续；真正超限且候选仍不能 fit 时准确停止，不自动 accept、清预算、重发或宣告完成。
新请求只提交实际采用的来源，不能复用另一份 composition 的提交动作；私有工具回合与签名仍属原执行。

SELF 接纳记录构成持久待派发事实，以 `initiative:<run_id>` 唯一关联原 Work；同一标记
也写入 journal，避免恢复时把新 brief 或记忆重新追加成原触发。沙箱完成先唤醒原 Work/
子任务，保留执行 ID 和总预算。controller owner/epoch 切换只控制新接纳，不使已接受工作
失效；generation reset 或原授权失效仍阻止继续执行。发送沿原 Presence，不借当前主动路由。
Host 对原 run 独立对账真实效果；终态迟到回执保留，但不复活 Work、重复记账或重发。
`suspended`/`waiting_user` 映射为 interrupted，保留检查点，不由参与控制器自行启动新任务。
WorkScheduler 始终启动；普通聊天及无现有 WorkControl 的 Host 调用接纳、模型自动化执行
仍各自核验 `RUNTIME_WORK_ENABLED`，
关闭开关不停止 WorkScheduler 所管理的已有 Work；由 AutomationWorker 恢复的模型自动化
仍受其执行开关约束。SELF 接纳也继续使用这套持久恢复机制，不能产生无人调度的执行记录。
SubagentScheduler 同样启动以恢复原子任务；新子任务接纳关闭不等于停止已有子任务的恢复。

## 预算、等待与异常

自动化创建在最终短 writer 中复核永久创建者、当前权限、SELF 场景及 active 数量上限；
同 creation key 的原结果先于容量拒绝。普通 create/update/pause/resume/cancel/run_now
与强制管理审计共用事务，审计失败一同回滚，before 使用写时实际版本；resume/run_now
保留原准入 policy。读取目录不授予修改权限，修改按当前 canonical owner 或 superuser 核验。

每段默认 24 次模型请求、32 次业务工具调用，单段用尽仅让出并排队续跑。新主任务、子任务
及自动化 run 没有默认累计次数上限；root/run 持久计数不清零，显式有限限额仍同事务预留。
旧已登记的有限预算保留原值，不因升级、换步骤、重启或取结果获得新额度。纯 DSL 保留原声明限制。
旧含 Yuki 生成步骤的脚本采用 runtime 预算，不因旧外层 1 次模型 / 2 次工具限制拒绝已完成工作。
自动化的旧累计激活时间限制不再作为主 Agent 总寿命；Provider 请求超时和总预算仍有效。

真实 Provider 请求只在 `TaskModelExecutor` 统一接纳。后台与维护请求合计不超过
`max(1, N-1)`，总请求不超过 N；N 大于 1 时保留一个前台名额，N 为 1 时串行并优先
接纳已排队的前台。普通持久后台请求使用不可抢占的 `BACKGROUND`；关系评价和
attribution 等最佳努力任务仍可被前台抢占，`REQUIRED` 压缩按前台必需工作接纳。
`ConcurrencyManager` 只保留会话互斥和排队/执行中的取消登记。Runner 在实际请求
接纳后复核来源、登记一次逻辑请求预算和 dispatched 检查点；HTTP 重试不重复预留
这笔预算，额外真实请求沿原 transport accounting 计费。

普通私聊的新用户输入可抢占尚未接纳 Work、尚未开始本地效果且尚未派发原生工具的旧轮。
入口在 Coordinator guard 内捕获原 token 登记的 task，推进输入版本后只取消该原 task，
并在 guard 外等待它真正退出；不得延迟按 conversation key 重新找当前请求来取消。
准备和生成嵌套登记共享原 task 的保护，发送/修改开始与原生工具请求派发前标记的保护
一直保留到该 task 最后退出，不因连续新输入更新 version 而清除。新入口自身取消时仍
收拢原任务，不能二次取消正在完成的 transport、线程或回执清理；会话互斥和 Provider
名额沿各自真实 finally 释放。已接受 Work 继续接入原 Work 输入；群聊和显式停止仍沿原策略。
已开始的分段发送保存完整计划和真实配对回执，不以首条成功提前终止 Runner。原 HTTP
取消可能已经发生上游计费或原生效果，未知不当零、不盲重发、不退还原预算；新的内部
事件须通过正常来源与权限接纳形成新的请求。此策略允许新私聊输入替代旧轮尚未发送的
回答，不能据此承诺新轮必定完整回答旧问题。原生保护由 MainAgentBackend 的原 token
实施；无普通私聊入口的 SDK backend 不获得该抢占状态。

自动化 claim 使用每次唯一所有者并续期，提交时再次核验；忙任务延迟接纳，不创建新 run。
Work、工作者与自动化的续租任务由原激活 task 监督。续租返回失效、非瞬态数据库错误
或计量失败会停止该激活，并交回原 Work/run 的恢复；不新建执行身份、不清空预算或 journal。
续租只对具有明确 SQLite `BUSY` 扩展错误码的错误，在最后确认的租约到期前有界重试；
`LOCKED` 不按外部 writer 繁忙重试。renew 与 meter 分别记录失败阶段，meter 写入失败
不在心跳中重放。等待 writer 后的自动化续租也在 SQL 执行时检查原 claim 尚未到期。
新 run 与保存原 script_hash 的初始 ready 游标在同一事务登记；登记后重启恢复同一个 run。
恢复当前时段的历史 running run 若缺少游标，明确记为 uncertain/missing_initial_run_cursor，
保留已有计数、预算与结果，不补造空游标或重新执行业务步骤。旧版本已经跳过时段的历史
孤立记录不倒追执行。前置权限、路由或配置拒绝同样保留累计计数；旧工作者不能重新载入
新领取者身份来通过执行检查。
停机等待有界，取消后按持久检查点恢复。插件自主轮不会因两次会话变动直接放弃。

超时的写入和发送可能已经提交。插件副作用不因 transient_once 自动重试，未知结果携带
`uncertain` 回执，后续副作用受现有 WorkSession 围栏阻止。读取可按声明重试。
自动化收到插件任务句柄后查询该 work，不重新执行产生句柄的 handler。
未取得可核验结果的外层 dispatch 恢复为 uncertain，不猜测成功或重跑。
DSL 外层 deadline 在权限复核后区分实际进入 handler 的 SEND/MUTATE 与纯 READ、
尚未通过验证的步骤：前者没有终态效果证据时保留 uncertain，后两者保持 failed。
原 cursor、run/step 请求键和 Social receipt 不重建；单效果的已确认回执可以说明
accepted/failed，部分复合交付不充当整个步骤已完成的证据。效果已确认但步骤记账失败
仍报告运行失败，并保留已确认投递计数；恢复只读原 dispatch，不再次调用 handler。

普通回答无需先调用 task_control.answer。未采用 interactive 的工作，模型最终文字可结束内部循环，但不触发发送；
工作完成仍由同一回执校验检查未结束执行和 artifact。发送是否成功只看显式发送回执，
不自动把工具 JSON 或固定成功句发到聊天中。
分条 `send_message` 在首条网关调用前持久记录计划段数，并按原调用的确定性子调用 ID
核对每一段 Social 回执。自动化交付检查在最终工具汇总缺失时，仅凭完整计划和全部
成功的同目标子回执确认实际投递；旧父回执没有计划段数时仍为未知。这个核对不补写
原 Work 工具结果，也不解除通用 WorkSession 对未知效果的围栏；要自动续原 Work，
必须先建立首写时持久的 Work effect 与 Social 父回执所有权关联。
明确管理命令和模型完全不可用时的运行状态反馈与普通 Agent 正文区分。

较长的用户交互任务可在 `task_control.accept` 声明 `reporting=interactive`；
省略沿用原节奏，`quiet` 用于用户要求安静执行。主 Agent 合同固定包含可选字段，
不按每次任务修改 schema。子任务、SELF 和禁止外发/内部返回来源不采用 interactive。
`update` 仅改变 reporting 时不修改目标或解除等待；quiet 可提升为 interactive，
已采用 interactive 的原 Work 不接受模型自行降为 quiet。该元数据保存于原 checkpoint 的
有界 `communication` 子路径，原 wait/need_input/fail 检查点不会擦除它。

`send_message.work_report` 只声明 `start/progress/reply/final` 用途和至多八个原内部事件
引用；它不进入平台消息字段，不授予权限。引用只可指向原 trigger 或同 Work 获准的
staged/consumed 输入；目标须为当前 canonical 目标。派发前在原 prepared effect 保存关联，
结果仍取原 Social/Work effect 回执；prepared、失败和 unknown 不当作用户已收到。
查询原效果事实不依赖最近 64 条展示窗口，不新建投递状态副本。

interactive 的首次可控业务工具在真实 start 回执前不执行、不计工具消耗、不登记业务效果。
原调用顺序保持：send 是串行屏障；先业务后 send 的前项返回未执行，随后发送照常处理。
任务登记、原 Work 查询等安全准备可先进行，调查读取和子任务委派也受顺序检查。
同一开始缺口只有一次纠正；失败/未知保留原围栏，不能靠换目标或重复发送解决。
Provider 原生工具发生于服务端，后端只提供派发前机会，不能宣称逐次硬拦截。

阶段报告由模型按实质发现、阻塞和目标调整判断，不建立阶段事项台账或定时群发。
已有 steer 接入、输入消费及完成 CAS 不变；新真人输入按原输入水位提供有限非阻断答复
机会，quiet 与旧 Work 也适用。消费/提醒并不证明答复，相关发送仍看真实回执。
interactive 无工具内部正文不自动完成，须继续执行或显式 complete/wait/need_input/fail；
缺少意图只有一次反馈，之后按原 no-progress 有界收束。明确 answer 交付只接受 final 用途
的当前目标成功回执，开始、进度和插话回复不代替最终答复。带 work_report 的纯沟通
发送不充当 state_change 的业务修改证据。文件、业务变更、SELF
静默及内部返回继续各自交付合同，不额外群发。标签和传输成功都不能证明语义目标完成。

`task_control.wait` 保留单一所属 `run_id` 路径，也可登记一次性 `conditions`：
`time_due`、当前 canonical Conversation 的新消息、获准插件发布的事件或所属 run。
集合支持 `any` / `all` 和可选 `deadline_at`；没有隐式 90 秒有效期。绑定持久化在
`runtime_work_waits`，带 Work ID、generation、主体、事件水位和唯一调用键。消息入口、
插件发布事务与 WorkScheduler 的时钟分别交付信号；命中时同事务登记原 Work 输入并入队，
不创建第二项 Agent 工作。局部满足的 `all` 条件留在绑定中；超时向原 Work 交付明确结果，
不视作同意。插件必须显式设置 SDK 的 `resume_waiting_work`，否则维持原通知和新轮行为；
命中原 Work 的事件不会同时启动独立的插件 Agent 轮。
AutomationWorker 只管理自己的计划、claim 和原 run/step 游标；等待状态通过 Runtime 查询。
释放自动化 claim 前后均核对原 Work 等待是否仍活跃，信号先到或后到都唤醒原计划，
不因停放覆盖已经到达的唤醒，也不创建新 run。
`task_control.wait_status` 查看原 Work 的绑定及未满足条件，`cancel_wait` 撤销它；
`waiting_user` 只由原提问内部发送事件所收到的同一 Person 回复自动恢复，普通群消息仍按新输入处理。

时钟轮询先分页只读观察等待条件、原 Work 终态及 Conversation/Work generation，
没有变化时不取得 SQLite 写锁。有变化的最多 128 个候选在短写事务中重新核验，
再更新部分命中、失效状态或向原 Work 投递；所属子 Agent 的状态取自原 Work。
输入准备修复、终态回收及 Emoji 分析领取也先只读发现候选，空轮询不产生写事务。
Emoji 只接管候选中的到期分析租约，不全局清扫过期行。Work 激活的有效期在执行
数据库语句时核验；等待写锁期间到期的旧租约不能续期或提交状态。

## 持久化与验证

0060 为新的自动化创建调用键增加部分唯一索引，为跨步骤预算增加 run 级计数。
0071 为 SELF 自动化增添可判别创建者和唯一调用键，并增添 SELF 时区、Work 等待绑定与
插件恢复选择字段；原 Person 任务和运行回执不重建身份。
同一 Host 调用重放和并发提交只登记一次，参数冲突明确拒绝；同事件的两个合法调用不合并。
automation_list 的 match_task 按结构化目标、时间、读取范围及交付查询等价待执行项，忽略显示名称；
只返回候选，不自动合并或阻止用户要求的另一个实例，不猜测近义自然语言。
迁移继承既有 run 及未完成步骤的消耗；缺恢复所有者的旧 SDK/自动化 work 明确暂停并保留原因。
迁移不重跑旧日记、不清除原 work、artifact、预算或交付回执。

V6 迁移链为 `0065`（PR #111 关系历史索引）→ `0066`（autonomy 接纳和反馈）→
`0067`（SELF 工具证据及自省独立回执水位）→ `0068`（内部引用事件）→
`0069`（Social 发送回执的内部事件关联）→ `0070`（无来源机会与讨论线程）。
SELF 工具证据以 event/run 二选一归属，
不通过假聊天事件进入 Memory；自省迟到回执按原 run 的独立水位续读。

新发送成功的 Social 回执与其内部出站事件在同一短事务中关联。恢复投影只按回执的
`event_id` 查询并核验会话、来源与因果关系，不按平台消息编号或时间窗口反查。
旧回执没有内部关联，或原事件因隐私清理而不存在时，保留成功状态并明确正文不可用；
不能据此补发或从当前调用参数伪造已发送正文。

验收比较两个 Provider 协议的实际 messages/input、工具和原生工具；统计命中率需要 Provider
真实指标。离线请求回放可证明协议前缀，不等于真实缓存命中率；指标不可用标记未知。

本地已对照 SELF 的 Responses / Chat Completions 真实序列化输入，以及 Responses 原生工具
声明；第 24 次请求后第 25 次继续原 Work，恢复前缀不改写。这不替代 T20 真实 QQ、生产
缓存和长期参与效果验收。Jev 合成 smoke 只提供协议及样本分歧证据，不是独立人工准确率。


## 统计故障与未接纳轮次

模型调用统计是观测数据，不是执行预算或副作用回执。统计写入抛出数据库异常时，
保留 Provider 已返回的结果；模型请求失败时，统计异常不能覆盖原始模型异常。
成功请求之后的可丢统计身份校验或编程错误同样只记固定类别和缺样，保留成功响应、
不重跑模型；实际 dispatch 身份/权限、预算、journal 与效果回执失败仍按原合同阻断。
任务取消及 SystemExit/KeyboardInterrupt 照常传播，不承诺
取消或进程终止时仍能交还、持久化已经收到的 Provider 响应。

生产统计由调用任务冻结原可信身份、时间和 trace 根已捕获的 privacy generation，
有界快照入原共享队列；来源 SQL、编码与 writer 都由同一个空 Context 消费者处理。
没有原 trace/privacy 来源的直接 queued record 丢样，不在模型成功后重新捕获删除代次。
同步 writer=None 的独立调用保留原即时 API。模型返回和并发名额释放不等待可丢准备或写锁。数据库聚合只代表已经落盘的样本，队列中仍可能有待写记录，
不能宣称完整命中率。入队拒绝、异步写入失败及关闭丢弃通过内容无关日志和诊断 health
报告 coverage_incomplete；报错可能发生在真实提交后，不把失败次数当作确定缺失数，也不重试
结果未知的 INSERT。容量、隐私删除围栏及生命周期见 [执行过程查看](execution-trace.md)。
预算、授权、请求检查点和工具回执仍按原持久化合同失败关闭。

ModelInvocation latency_seconds 的新成功/失败样本统一从完成路由/归一化后、进入
Provider 调用等待前计到调用及结果准备完成，含 Provider slot 等待、guard、重试和响应处理；
成功样本不再直接使用协议自身的 complete latency。ChatResponse.latency_seconds 保留各协议
原兼容定义。完整 logical call 与 cancellation 通过 version=1 的 model_phases 观测，旧统计
没有分段/版本时不可推断新阶段；取消仍不新增虚假的成功统计。互斥/嵌套计量见
[执行过程查看](execution-trace.md)。

WorkScheduler 仍按原顺序执行 repair_inputs/wake_rollups/reclaim/protocol_cleanup/selection，
之后逐个 serial_resumer；health.phase_timings 的固定分桶和慢阶段数值日志分别计量这些
区间，不改变调度并发、maintenance 次序或已承诺 Work 生命周期。等待驱动仍独立。

尚未接纳工作的普通聊天，对原本发送运维反馈的 Provider、校验及其他内部异常分支
复用 RuntimeFailure 分类，区分数据库繁忙、内部错误、认证/请求配置问题和 Provider
超时。配置缺失、空响应保留各自的显式反馈。既有 OSError、RuntimeError、TypeError
分支仍只记录 send_or_storage_failure，不发送额外消息；这一分支包含发送或存储结果
不确定的情形，不能据此声称所有异常已经统一反馈。没有持久恢复所有者时不自动重放
请求；有工作时继续由既有 supervisor 按原恢复合同处理。

## 执行诊断

[执行过程查看](execution-trace.md) 在共享执行器和协议 HTTP 边界记录逐次请求、
Provider 实际返回的可读思考、重试与工具批次结果。它独立于覆盖/回收的恢复 journal，
没有重放、预算重置或发送权限。Chat 在组装历史和 Memory 前绑定诊断删除代次，
隐私删除后旧调用不能再写回旧上下文。
诊断数据库/编码失败只报告 coverage_incomplete；取消及进程终止照常传播，
不承诺取消时仍能保存或交还已收到的响应。过程记录复用上述有界诊断队列，
异步提交失败由诊断日志和 health 报告；轮次结束标记只反映当时已知的记录缺口。
诊断可以丢弃，不建立额外持久恢复状态，也不替代任何真实效果回执。
