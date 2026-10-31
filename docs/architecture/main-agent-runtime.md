# 主 Agent 执行与恢复合同

个人明确授权的本机 Codex、Claude 沿现有 NativeConnector 和固定 `external_agent` 工具执行。桌面建立连接不要求选择项目；原生会话、任务目录与真实授权仍各认原事实。`local_device` 工具和活执行协议已退出；旧 Work 保留原表、内部 ID 与效果键的只读回执查询，pending/unknown 不转成已完成，不重派，也不转换为 Native 操作。远程电脑的有头界面继续沿现役终端/Manager 能力，不复制 Main。

本文定义当前执行合同；实际部署版本以运行镜像和提交记录为准。

## 执行与来源

普通聊天、主动群聊、自动化、插件通知自主轮和 SDK 的 Yuki 生成使用
`MainAgentTurnService`、`AgentRunner` 和 `MainAgentBackend`。自动化不再拥有第二套
模型工具后端、名称映射或参数转换。独立 agent_sessions 与辅助模型不属于主合同，持久子 Agent 复用 Main 的完整工具合同。
应用装配采用同一个 `YukiRuntime`，持有共享的主 TurnService、Runner、固定工具合同及
Work/子任务调度器生命周期。Chat、SELF、定时和插件主调用使用这个实例；定时与SELF
通过共同的真实来源入口装配本轮材料，再调用共享 TurnService、Runner 与固定合同。
Runtime 不持有单一当前 Conversation；每次调用仍显式传递其原来源、权限、场景和执行 ID。
插件注册完成后冻结主工具清单，再启动 Runtime 的恢复调度器和插件后台主调用。
Chat 在构造时持有该固定合同及实际 short_state、参与反馈和响应观察依赖；Workspace、媒体、
Sandbox、Social 与 Emoji 依真实依赖顺序构造，不依赖启动后的 None 槽回填。
Participation 接收实际领域服务与 bound method，各领域事务和生命周期仍归原 owner。
legacy/semantic 自主机会均以正式 SELF 来源进入这条执行链；
真实 QQ 小范围社交效果验收仍需单独记录。

完全缺少正文、当前附件、Bot 提及及引用内容或引用 ID 的平台输入在 Processor 入口静默返回，不固定模型、不入账、不通知当前轮，也不接纳 Work 或启动 JEV。正常纯媒体、只提及 Bot 和真实引用仍按原来源处理；准备后没有可处理内容时只清理本次未就绪输入，不发送固定的“请输入内容”提示，不撤销原 Work。真实语音或视觉失败继续保留已有错误事实。

普通消息触发 Main 前不再补查昵称、群名片或群名。资料使用原事件携带的字段与既有内部目录，
缺名仍按原 Person、Conversation 和内部事件接纳、装配与回复。真实资料通知只更新资料，
不成为新的 Main 激活。该更新不增加工具、提示词或运行资料，不改固定声明及公共历史前缀。

真人输入从接纳、准备到工具和交付都处在共享模型执行器的前台活动范围；用户来源的持久
Main 恢复同样登记活动。SELF 自省沿原 best-effort 后台名额退让，等当前活动结束并经过配置的
安静间隔后再准备下一批。工具间隙仍属于原前台轮次，不会被误判为空闲；取消会释放活动范围。

`MainAgentTurnService._run_prepared` 在实际 Main runner 活跃期间，通过 `ProcessingStatusService` 后台维护原 Conversation/Presence 的原生正在输入状态。已知 Presence 的连接不支持对应私聊或群提示时，内存 Registry 直接跳过该 UI 动作，不查询路由或群成员；其余沿原路由、Registry 和内部 source event 取得适配器。同一入口重叠执行共用临时引用计数，最后退出请求停止续期，在途 UI 请求与原生停止仅由后台收尾，不等待路由、刷新或停止 API 才交回 Main 结果。服务继续持有并收拢 UI task；相邻同入口的新提示在后台等原停止请求完成或超时后再开始，模型不等该顺序。等待中的 Work 不维持提示。应用关闭先由 Runtime 收拢执行，再关闭状态服务并等待剩余 UI task。后台提示失败只写诊断，不裁决模型、工具或交付结果；该状态不入账、不建立回执或预算，不改变固定工具声明、静态提示词或缓存前缀。平台实际范围见[平台适配](platform-adapters.md#空输入与处理状态)。

所有主入口默认采用 direct 构建及静态工具政策，不加载 Code 引擎，部署内固定完整工具清单直接可达。
显式启用 Code 的部署才注入持续 Work 的 Code Mode 编排策略。
已知多步流程在脚本内读取、核验和聚合，只把必要结果交回模型；需要解释新证据时再交回
模型决策。基础工具包括联网搜索与网页读取，短聊、基础单次操作及生命周期控制可直接
调用；专用工具按需查询原 schema 后通过 Code Mode 使用。工作者使用同一分层，具体
执行、pending 等待、原 VM 续跑与授权仍遵守 [Tool Kernel 合同](tool-kernel.md)。

模型侧每项业务只有一个公开名称和参数合同。冻结清单只来自主工具注册表，不追加 DSL
或插件的自动化别名；目录查询不会加载工具、改变声明或提升权限。声明变更在合法新链应用。
续跑仍保留执行记录、预算和已提交回执，不改写旧 Provider 请求或重跑已有操作。

运行状态用 `state_scope=current_activation` 区分本轮激活与持久 Work 生命周期。
`no_active_work` 只表示本轮没有激活的 Work，不表示过去未接纳工作或工具失败。
没有当前 Work 时，`recent_work` 保留最近一项当前来源可见 Work 的真实 ID、状态和目标短线索，
用于识别已有工作，避免重复创建；三项执行计数和创建者等详情使用既有查询工具按需读取。
有当前 Work 时保留其完整目标，不混入另一项工作的状态。
未设置的 work ID、goal 和空 `available_work` 不占常驻提示。
`available_work` 至多 16 项，只提供内部 ID、状态、目标摘录、创建者显示名和 `has_wait`。
两种目录投影的目标摘录最多 160 字符，available 目录显示名最多 64 字符；`goal_complete=false` 明确表示
摘录不完整，不能代替原目标。读取使用有界 SQL 最小投影，不逐项预载完整等待 JSON。
续接目标或等待内容不清楚时先用 `get` 读取完整 goal 与 wait，活动 Work 原目标不截断。
目录中的 revision、owner/Conversation/generation、来源与审计时间等详情不常驻 Prompt，
需要核实再调用既有 `task_control(action=get/list)`；get 同时提供存在的完整等待登记。
get 的全局安全元数据不授予私有步骤读取。原 actor、Conversation、generation 和执行树范围
获准时，直接读取原 effects 的最近一页、真实结果及原 context_note；`limit/cursor` 续读更早
记录，`history_order=desc`，不聚合完整 journal，也不另存进度摘要。范围不符时明确
`progress_available=false`，不把缺失步骤当作没有行动。`observed_at` 是查询时间，步骤的
created_at/updated_at 是原回执时间，生命周期和计数本身不能证明业务已做完。
后端身份、权限和恢复事实仍保持完整。
自动注入按原 actor、Conversation、generation 及执行来源核验可见范围，
不因为查询工具能读全局目录就自动注入其他会话的工作。
不注入完整 journal、工具正文或其他主体的任务。既有请求链只追加当前视图，不重写历史状态。

当前场景只用简短的群聊/私聊说明，不预取称呼全集、记忆引用或关系评分。短期记录只展示当前有内容的 slot/text，写入只需要 slot/text，返回本次槽位结果；清空和到期仍沿原存储语义处理。插件资料保留实际来源。

`Settings` 从唯一 `SOUL_FILE` 读取用户人格原件，名称取当前 `character_profile` 的已提交资料。
`PromptComposer` 将最小角色身份、SOUL、`prompting.contracts.core_contract` 和 Memory grounding
合成稳定系统段。SOUL 是自由 Markdown，精确展开 `{{name}}`，空正文只省略人格段。
性格、关系、喜好、动作/旁白偏好和虚构语气示例归 SOUL；工具、统一发送、Work、回执与
自主触发语义归程序规则。Main、SELF、自动化与子 Agent 复用相同入口。
角色资料改名或 SOUL 编辑/reload 后，合法新激活使用新值；当前工具往返及原协议请求保持原材料。
Main 的恢复合同只纳既有 INVARIANT 技术规则、实际模型设置和固定工具合同，SOUL 与名称保存在原请求正文中。
改名不废弃原已付响应的未决工具尾部或签名，也不使未知派发可重购；完整配对后的合法业务续跑沿现有 rebase 使用新 SOUL。
工具说明使用中性角色称谓，不因改名重建 manifest；历史只认原事件名称快照，不用当前角色名补造旧名。
桌面与 CLI 共用单 SOUL 读写/重载合同，详见[SOUL 管理与升级](../operations/soul-deployment-20261022.md)。
Main 的系统约束、
插件资料、外部事件、聊天历史、附件、网页结果和 compact 检查点不再附加概括性“不可信”
提醒；原事件类型、来源、内部身份及 `<runtime_context>` 的程序资料标签仍按原载体保留。
工具执行继续由后端核验真实授权，已有付费请求、工具声明和冻结历史不追溯改写。

当前时间与历史显示统一默认 UTC+8，个人与 SELF 时区偏好、读取/设置工具和存储已退出。
明确指定的安排时区仍属于任务本身，真实时间按 UTC 保存；原付费请求中的时间原件不重写。
本轮资料每次准备当前有效值，放在当前触发消息的正文前面；首次激活新建的 Work 运行状态块同样前置，以 `<runtime_context>` / `</runtime_context>` 包裹，固定系统约束说明其中是程序提供的本轮系统运行资料、不是任何人的发言，并保留前后双换行，当前正文在最后。标签只说明资料来源，消息角色仍为 user。资料包含本轮 Work 状态、可见工作线索，以及原入场或 Work 场景绑定的局部参与反馈；为空无标签。它不进入公共 FrozenFragments、自动 dynamic_snapshot、观察或摘要，不再保存首份、变化差量、移除流水和最后展示值。一个 Conversation 的公共投影只冻结实际采用的原事件渲染，保名字、引用、AT与附件事实；当前说话人、入口和个人偏好不建立另一份公共历史。

Provider 会话元数据沿真实 canonical conversation、generation 和已有 Work ID 传递。未接纳 Work 的主请求使用 main 分支，接纳后沿原 Work 分支；公共及私有 compact 继承原请求的身份。通用结构化请求与原生搜索辅助请求在已有 Work 控制器时沿原租约传递身份；独立结构化请求可透传已知元数据，没有明确来源时保持空值。该元数据只进入 HTTP header，正文和固定工具合同保持原有装配。
普通主轮不默认注入时间、完整 participation 状态、管理权限证明、最近投递包装及空视觉观察。
定时新激活在同一运行资料块说明原创建者、原 Conversation/平台、automation/run、真实创建时间、
scheduled_for 和本次可信时间快照，明确当前目标已经到期，不把原相对时间从现在重新起算。
资料在首个合法新请求准备时冻结，工具往返和 HTTP 重试不重复追加或回写旧请求；原恢复 transcript
沿原初始化跳过合同接续。它不包含第二份完整目标，也不进入公共历史或固定系统前缀。
执行仍核验当前身份和授权；真实媒体、附件错误、聊天时间线及发送回执保留在原入口。
当前 Work 状态沿原私有有序输入尾部接入。工具往返与恢复保原签名、调用/回执配对和初始请求，不将新运行块插回已经增长的轨迹前面。
有 Work 和无 Work 的 Main 均在本来需要的下一次正常请求前，经 `AgentToolBackend.participation_context(runtime)` 读取相关参与事实：`participation` 下的原讨论和对象、自身意愿、语义判断及原来源顺序。只投影已有 SelfReport、UnitSemanticReview 和未释放的相关 Boundary，不复制概率、队列、人物列表或新建第三份刹车状态。反馈沿原有序输入尾部追加；已付请求、冻结前缀和初始运行块不改写。原生 pause 或 opaque 精确续接不能插入时，留待下个合法业务请求。反馈不进入业务 mailbox，不单独唤醒 Main 或购买确认请求，也不否决发送、已接纳输入或原任务；后端收起尚未选择的普通机会，本体结合当前交流决定是否继续闲聊。
文件、终端、完整自动化目录和长期记忆均使用已有工具按需查询。

主 Agent 的 `task_control(action=get, work_id=...)` 按原内部 Work ID 只读查询全局安全目录，
与 Automation 目录读取一致，不按创建者、当前会话或 generation 过滤；
`task_control(action=list)` 有界分页列出同一目录，`status` 默认 `active`，
查已完成、失败或取消的工作用 `terminal`，同时看活动与终态用 `all`。
`limit` 默认 8、允许 1–50，`cursor` 用于续页，不把这个简表宣称为完整任务存档。
全局投影只提供安全任务元数据和创建者标识，不提供 journal、工具正文或跨会话聊天内容。
子 Agent 使用 Main 的固定完整工具合同，其 `get` / `list` 查看自身及实际后代的安全事实；指定节点
管理也限于该子树，原祖先和兄弟不因此开放。`parent_work_id` 可分页查询直接子目标。
两个查询不要求先接纳 Work，不获取执行租约，不追加输入、恢复工作或发放预算。
进度、用途、结束时间和已有结果的追问先查询原 Work，不因此另行 `accept`。
终态查询不会复活旧任务；真实独立新请求仍可接纳新 Work。
持久状态是查询时刻的生命周期证据，具体执行与交付仍由原回执证明；
查询不扩大修改权限、不授予原未知调用的重派资格，也不把口头承诺当作已执行事实。

当前群的 active 自动化目录不再预取进每轮主 Agent 上下文。明确涉及自动化的当前请求由
`automation_query(view=list)` / `automation_query(view=get)` 按需核实；读取目录不授予修改权限。普通聊天、SELF
自主轮、插件唤醒和定时 Agent 不因群里存在任务而自动收到任务清单。

定时使用固定主 Agent 工具声明，TaskSpec 不选择工具名单。执行时依据原永久创建者或
SELF 及当前真实授权核验；创建时的 USER 身份不永久封顶。每次模型请求和效果执行复核
原来源、所有者、租约及会话 generation；当前授权被撤销时停止旧授权执行。原 Work
可明确沿原 ID 继续，插件安装批准仍按原事实核验。

人物管理和目录读取使用当前归并后的 Person，原 Work/source、事件作者、委托快照、journal、
工具调用键与根预算保持原件。账号转移、停用或没有可选的人类平台账号，不单独阻止已接纳的
内部计算；新平台效果在派发处核实际目标、Presence、Binding和当前真实授权。原人物或当前根人物
显式禁用、Work取消、generation失效及批准撤销仍生效。人物合并或改名不取消在途模型请求，
不重买已付响应，也不修改旧工具配对或冻结前缀；新请求开始前复核真实人物授权。

`ToolActor` 传递真实主体和执行来源。聊天保留内部事件 ID，定时任务保留原 run/execution ID；
不能生成假的 InboundMessage、QQ 消息或提及来通过校验。定时来源仍为 scheduled_automation。
历史、记忆的主动补查使用同一读取授权；长期记忆不自动预取或注入。记忆写入仍需真实证据，
定时任务通过 evidence_event_id 引用创建者在当前会话的原始事件，不把任务指令伪装成人类发言。

SELF 使用数据库内唯一的 `PrincipalRef(self, self)`，不携带真人 user/person。自主入口的
`ToolActor` / `TurnAuthority` 使用可信 `initiative_run_id`；SELF 定时入口使用真实
`automation_run_id` 与 `scheduled_automation`，沿同一时间扫描、唯一到期发生和共同 Main 工具合同执行。`SelfInitiativeTrigger` 固定原 Conversation、
generation、Space、Presence 与群传输目标。目标成员和资料来源都不授予其私人权限。
`ConversationTurnSnapshot` 使用原 event、initiative run 或 automation run 三者之一；无事件工作不得借
最近真人消息补锚点。SELF主入口按需读取当前群及该群可见的SELF Memory；主动SELF写入沿原initiative和原执行的真实工具回执，不借真人事件。
每次模型请求、工具执行和发送准备仍复核原 run 与场景权限。SELF 自动化保留原
Conversation、Space、Presence 和群绑定；每次新到期发生使用该会话的当前 generation，
已接纳 Work 则保留原发生代次。SELF 的社交调用沿共同目标选择、资料授权和发送回执处理，不设来源专属四动作、当前群或提及名单。
固定直调声明与完整执行 API 均不随主体改动，执行处拒绝不获准的能力；详见 [语义参与接入](semantic-participation.md)。

模型侧只用 `send` 发送可见内容：省略 target 时发到当前群或私聊，显式 target 可指定
其他人或群，后端选择私聊或群聊路由。表情、附件和引用由同一工具的参数表达；提及直接在正文原位置写 `@名字`，同名时复制 `@{account:UUID}`；语音合成与发送已退出；
真实网关发送和持久回执组成普通工具结果，`uncertain` 不自动重试。一次循环可按实际需要多次发送，也可
完全不发送；模型最终正文交回原调用方，不会自动作为平台聊天消息发送。`report_progress` 和本轮最终回复
效果队列不再是模型可调用合同。工作完成可以安静结束，不需要人为制造发送回执。
固定媒体提示将表情作为聊天语气，按当前话题和任务需要选读附件；讨论图片本身时仍可具体查看与说明。
自然配图通过 `send.emoji.goal` 表达情绪或含义，正文先发、后台选图；纯表情可省略 `text`，不要求模型等图或逐张复述标签。
有前台文字（包括只写 `@名字`）时，提及和引用随前台原位发送一次，后台只追加图片。配图意图与首个正文发送 claim 在同一短事务保存原来源，并关联原文本回执；
后台沿原分段计划等待文字派发结束，失败或未知同样保留原配图意图，不要求正文全部成功。
正文前不单独补查配图来源，正文后不另开接纳写事务；来源查询与必要复核复用原 claim。
重启或工具重入继续原意图与回执，不重发正文，也不借今日 generation 重建旧配图来源。
分段发送在关闭时中断，或启动时发现已开始的原计划仍有未派发步骤时，只在原父回执记录派发停止；
已发送、未知和未发送子回执保持原事实。没有真实撤权时，原配图继续独立恢复。
固定系统合同的【发言】指导日常说清当前值得回应的一层后收住；确认或话头转移可以自然结束。【发送】要求可见回复调用 `send` 并把原话放入 `text`，同一次调用内换行分条，最终正文不自动送达。不设条数、统一字数上限或后端发送配额。
交互任务有实质进展、阻塞或待决定问题时及时简短说明，完成后交付结果；同一任务的操作结果可合并反馈，静默执行约定仍然适用。
没有新进展不刷屏，已送达的结果和附言不重复发送。
Main、JEV 和插件读取同一事件原位提及快照（显示名、Account、原 Assignment 和位置），不从后来的账号归属或历史末条猜测对象。目录按目标入口的实际账号返回；精确 Account 引用直接批查该平台账号，普通文字不查提及目录。
固定前缀和工具描述变更只影响新请求，已付协议保留原件。
模型的无工具 final 在普通聊天、SELF 和自动化入口都是内部终止信号，不触发未发送纠正轮。
存在原 Work 时经共同 complete 入口接纳结束决定，由原 writer 收拢所属执行和晚到业务输入；没有
Work 时返回内部结果。自动化沿原 Work 终态收尾，完成不强制发送；发送是否发生由原回执证明，内部 final 不证明 QQ 已回复。参数、权限或预算拒绝不伪造发送尝试，未知发送不盲目重试。
共享执行提示明确要求：当前真人委托的写操作按真实回执简短反馈，相关结果可随任务进展或交付合并说明。
实际发给当前会话仍调用 `send`；未确认不宣称成功，已送达的
同一结果不重复反馈。明确安静执行、自主 SELF、定时任务和后台维护不因此额外发消息。
这是 Agent 的执行指导，不新增关键词判断、强制代发或恢复循环；合成回放验证提示装配、
工具回执顺序和显式发送边界，不证明真实模型在任意场景都遵守提示。
恢复读取沿原 effect 到领域回执：Memory 认绑定 mutation 的原 outcome，原生控制认原 Work、
action、actor 下的 operation；不以当前 fact 可见性、设备在线或是否有 run 重判已确认效果。
已保存的完整 T3 结果优先；缺结果时恢复出的模型结果与效果证据使用同一事实，保留
`executed`、`mutation_committed`、pending/uncertain。steer/respond 被接纳不是任务完成，
cancel/detach 按原动作语义解释。Main、Code child、worker 复用这条读取链，原预算和身份不重置。

记忆写入不创建独占轮、单次写配额或特殊发送权限。每次操作仍核对原 actor、授权、
输入来源、预算和效果回执；发送继续经 Social 权限、路由与投递合同。记忆写和发送可在同批或同一 Code 脚本按原调用顺序执行，真实结果逐条返回。原未知调用不重新派发，后续独立调用不因历史未知被整体拒绝。
`NO_REPLY` 是合法内部沉默，`return_to_caller` 不制造真人收尾通知。作为 `send`
正文的完整 `NO_REPLY` 仍在发送和回执前拒绝；子 Agent 可沿真实来源与授权发送，内部结果与实际发送回执分别记录。

工具执行、pending、retryable 和 committed 判据只来自类型化执行事实或原持久回执，
不从模型可见展示文本反推。新 call 每次核当前授权并读取当前事实，不以另一 call 的同参
业务结果代替执行。原 call 与旧 readonly alias 沿自己的持久回执恢复，保原展示与执行事实；
纯 query-vector、不可变内容及 Provider 的真实缓存各沿原合同。恢复不增加新执行或重置预算。旧格式只在一次升级中归一；原回执和已付快照保留原件，新执行不保留旧参数解析器。来源标题和摘要等展示材料不作为执行权威。
单次 `send` 的纯文本复用原回复分条规则（换行、结构与字数上限），
逐条持久化发送回执；模型主动多次调用仍是独立的发送。部分发送未知时停止后续
分条，重入只读取已有回执，不盲目重发。文字、原位提及、引用与媒体先形成共同内容，再由适配器纯 `plan_send` 布局成实际派发。
`send` 是显式会话交付，不要求先 `task_control.accept`；它仍被视为真实副作用，
目标权限、路由、mutation 围栏和投递回执均在执行处核验。普通写操作沿原领域授权和回执执行，不强制先接纳 Work。社交工具不再对发消息或戳一戳施加额外的每目标/全局分钟频率上限。
已接纳 Work 也不额外施加发送次数或 16 条/60 秒窗口；投递意图只负责身份、内容冲突和
回执防重放，不是频率配额。已有明确未派发的 blocked 意图可按原身份恢复；accepted、
dispatching 和 unknown 不因此获得重发资格。平台实际拒绝仍按真实结果处理。
原 Presence 的实时连接在发送前断开时，原 Work 可按持久检查点短暂重试；
明确暂停、路由歧义和已进入 dispatching/unknown 的发送不因此获得重发资格。
共同内容、原路由和物理 parts 在首次接纳时一次冻结，父子回执沿同一原计划保存；重试不按新昵称、今日归属、运行参数或当前适配器重新布局。每个物理派发仍单独 claim/ACK；确认失败与结果未知分别报告，不把发送前拒绝误报为未知。
模型提供给 `send` 的文本在分条、回执准备、媒体生成、网关发送和账本写入之前统一净化；
内部历史事件前缀不会进入新的可见消息。既有 QQ 消息、原始账本、公共检查点和记忆不追溯改写。
可选 `<yuki-state>` 控制尾段同样在发送与最终正文净化时移除，格式错误不额外重试模型。
主 SELF 自报按实际 run、请求序号和响应 ID 保存；它不是别人行为的证据，也不允许工作者代报。
普通主轮的稀疏自报使用原真人入场冻结的讨论/对象及激活、请求顺序；不为保存意愿创建
Work 或假 initiative。`join/stay/quiet` 不必逐轮填写，没报不清空；quiet 只收起本单元自身
意愿，不解释成用户全群停止、取消已有任务或拒绝后续新问题。旧响应不得按较晚到达时间覆盖新意愿。派生保存失败不改变
已确认发送，不重跑模型、不重发或退还预算。
已有真人焦点的 Jev 请求可附带合法局部 engage 与 SELF 整体意愿，分别限定单元与 scope，作为 Yuki 自身意愿解释，
不等同真人停止、邀请或授权，也不为补状态增加 Main 或 Jev 请求。较新 quiet 清理已经覆盖的旧续聊复核待办，保留原在途观察与后到的新邀请。
请求取出时冻结状态，在途更新不改写原请求或拒绝旧结果。
普通快接沿原 admission 归属，后台稀疏复核不作为同步接纳门槛；不借同一 activation 的其他来源自证关系，自身 stay 或晚到发送不推翻较新的语义纠正。
移除控制尾段后，普通轮的空正文及内部 `NO_REPLY` 都按原沉默通路结束，不补答、不外发。
真实发送效果保持原 Space/person 投递目标，讨论关联只使用原持久入场/source；来源失效或
绑定缺失就保持未知，不能从后来当前话题补造。查询和反馈沿原主执行链，不增加逐轮审核模型。

定时定义只要求完整 goal 和 trigger，name 是可选显示标签。具体收件人、渠道、内容和通知约定
属于同一完整 goal，不另设 constraints、delivery、固定 version 或 task 级 timezone。
after/interval 认相对秒数，once 必须包含 UTC offset，daily/weekly 的有效时区固化在 trigger 中。
固定文字提醒也作为完整要求到点激活同一个 Main；没有静态直送、Compiler、专属能力
注册表、steps、模板、save_as 或外层步骤解释器。原目标 Conversation、scope、generation
及 run ID 构成本次真实来源，不生成真人 Inbound。群和私聊都复用共同历史准备；不再有
scene/include_memories/history_limit/context_profile 读取预设或 creator_private 旁路。
长期记忆沿共同检索工具按需读取。

已有 claim_due → 唯一到期槽 create_run → 共同 Main → 原回执/finish_run 安排链保持。
到期扫描按 `(next_run_at, id)` 用 SQL keyset 读取 128 条候选页，每页在原 writer 核验、
领取并提交后即逐项交给现有并发执行器，不先累积整轮定义或等待前页执行结束。
竞争使某页没有成功领取项时仍扫描下一页；租约起点认实际取得 writer 后的时间。
到点首次激活使用原版本保存的 hash；同一次发生重扫或重启不购买第二份模型/工具执行，
不同日期保留不同 run、私有执行身份和效果。时间扫描只负责安排，WorkScheduler 负责
实际 Work 的等待和恢复，暂停或取消未来安排不停止已经接纳的 Work。

定时入口不强制 Host accept，也不为工具、沙箱或环境增加先登记 Work 的门槛。
真正需要持久工作时由 Main 沿普通 task_control 接纳；已接纳工作按原 Work ID、私有
检查点、真实预算和效果回执接续。模型最终文字只作内部结束，QQ 可见内容必须显式发送。
首次到期正文是接纳版本的完整 goal，不再给模型一份自动化配置 JSON。
已有 Work 的当前目标认 Work.goal，复用当前运行状态投影；原安排以 version/run 引用溯源，
不再插入第二份原 goal。源 Conversation 保留上下文和真实授权，实际投递统一使用 send 的真实
target/binding，省略 target 仍是当前源会话；明确指定其他人的入口时使用已查明的稳定目标。
内部任务和提醒共用同一 Main，不裁剪工具、不强制发送，也不禁止合法安排独立后续事项。

`task_control(complete)` 可在同一次调用中携带真实内部 `result`。工作者沿 Main 同一完整声明与 revision；Code 控制 API 为 `yuki.codemode.api.v2`。direct 与 Code 共用原结束入口，把 Agent 的结束决定写入 Work `checkpoint_json` 的
`accepted_control`（complete/fail/need_input/wait 各一份，wait(conditions) 与原 wait
绑定同事务）。普通 complete、wait、need_input 与交接只登记决定，同一份原响应及 Code 程序中仍获准的调用按原顺序收拢；不要求模型把控制放在最后，也不因普通结束提案裁剪工具。原批次、程序和配对保存后，本次 activation 沿共同出口结束，不再请求后续模型。结算 writer 仅复核真实新授权输入及尚在执行的所属执行，等待期间保留结束决定；真正提交 completed 时才把 result 原子转存为 `sync_result`。caller 与子任务通知只读已提交行。need_input/fail 的 reason 和 context_note 是可选资料，不作为结束审批。
合法无工具 final（包括空正文）走同一结束路径；正文供内部结果，QQ 仍须显式发送。旧 unknown、暂停子目标、产物标签与辅助通知不否决业务完成，查询仍保存实际失败和未知事实。
原 wait 绑定仍 active 时，普通结算保留其 accepted_control；无新业务或匹配信号的辅助
激活沿原等待收拢，不重购模型或改原截止时间。原 wait 在结算前兑现时，writer 认同一
wait ID 的 delivered/expired 及尚待处理的原 signal，将 Work 续回 queued，避免用旧等待
覆盖已经到达的信号。合法新输入仍可退休原决定；显式新 wait 在同一事务中取消原 active
绑定并登记新绑定，保旧取消事实，同 call key 不续期，已兑现的旧 wait 不复活。
主任务的 `need_input.reason` 是内部等待资料，不会自动变成平台提问；需要用户补充时，先用 `send` 交付问题，再登记等待。
子任务直接 `need_input(reason=问题)`，Host 会通过父子通信发送问题并按原回执决定是否等待，无需再单独提问；底层等价方式是 `subagent_control(action=message, ask=true)`。
恢复先读 accepted_control，配齐原调用后由同一 writer 收尾，不再购买模型确认完成；没有持久响应的丢进程请求不自动重购。真实输入和目标修改沿原边界接续。
已有 Work 的 direct 控制使用原 InvocationService 的 T1/T2/T3 保存调用与回执，不扣业务工具预算。原响应中明确未派发的尾部沿原调用键接续；已派发未知调用、旧未执行终结回执与已关闭程序不因升级取得重派资格。合同变更在合法新链应用，旧已付请求、公共历史和固定前缀原件保持不变。

0110 一次升级将有真实高层要求的旧定义发布为干净 Main 任务，固定提醒原文和目标完整
保留。不能恢复真实目标的多步骤、插件或模板定义停止未来安排并作为原件存档。
原 run/version/raw/hash、私有检查点、历史步骤审计和已发生回执不变；旧 Main/Work
沿原来源接续，未完成的外层 DSL 不再解释。原 hash 直接读取保存值，不由新 schema 重算。
旧 cursor 与第二预算表仅为私有历史记录，不再写入或参与新执行。

Social 回执来源键保留旧键；较长的完整内部身份沿共同 SHA256 映射。写入、查找和
回执核对使用同一映射，不截断原执行 ID，不以另一项 Schedule 的来源合并私有执行。

自动化意图和自然语言时间由主 Agent 根据真实请求与上下文解释；歧义由 Agent 澄清。
后端不使用关键词或正则判断是否应创建任务、不重新解析原话中的时间，也不按成功措辞拦截正文。
后端继续校验结构化 trigger、时区、当前主体权限、目标来源及幂等；Agent 以持久化回执和真实 ID 报告结果。
`automation_query(view=list)` 提供不按当前发言人过滤的全局安全简表，默认只列 active，按 status 查询其他
真实生命周期或 all，并返回创建者的稳定身份。未命名任务用中性 ID 标签，不从私有 goal 生成标题。
`automation_query(view=get/history)` 按原创建者或当前超管资格返回完整定义、原版本和发生结果。
读取目录不授予私有正文读取或修改权限，
更新、暂停、恢复、取消、立即运行和执行历史按真实 actor 授权：普通用户只能管理自己的任务，
超级管理员可以管理全部任务。管理员更新不改变 canonical 创建者，后续执行仍按原创建者的当前权限
重新核验，不能借一次管理操作永久提升任务权限。当前群自动化目录不再常驻注入；需要时按需调用原工具核实。

## 所有者与恢复

| 来源 | 恢复所有者 | 交付所有者 |
| --- | --- | --- |
| 用户消息 | WorkScheduler 选择，WorkResumer 恢复原 work/generation | Agent 显式 `send` |
| SELF 自主群聊（legacy/semantic） | Host 接纳记录 → WorkScheduler 选择、WorkResumer 恢复原 initiative/work/generation | Agent 显式 `send`；允许 `NO_REPLY` |
| 定时 Main 发生 | AutomationWorker 保存唯一 run；实际 Work/派生目标由 WorkScheduler 交给共同 Main 入口 | Agent 显式 `send`，原 Social 回执不重发 |
| 插件通知自主轮 | PluginBackgroundTurnWorker 保留原 job/event；尚存 Work 由 WorkScheduler 交回原 Host 入口 | Agent 显式 `send`；插件自己的通知 outbox 独立 |
| 真实 actor/source 的 SDK 主调用 | Host 持有正在运行的协程，后续由 WorkScheduler 选择并交回插件 Host 恢复；无消息来源保原 run/执行编号 | 原调用方查询结果 |
| 子 Agent | SubagentScheduler 选择，SubagentExecution 恢复原父子关系 | 父 Agent 根据真实子结果决定交付 |

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
整理来源能完整容纳时直接沿原完整来源处理；真实超容量才分页，分页让出执行机会，避免饿死心跳和消息处理。
父激活取消保留原取消原因。合法新 owner 接管失租激活时按原 journal 阶段恢复，不把 Deferred 协调包装当作业务失败；已派发却丢失响应仍不可自动重购。
`ActiveWorkBindings` 仅登记当前活跃的进程内控制器，供跨任务输入定位原 Work；
退出时按同一控制器身份移除。它不是授权来源，持久租约、generation 与原来源仍是执行围栏。
已经接纳的持久Work不再由临时聊天coordinator版本拒绝；普通群观察不使合法恢复失效。
来源、generation、原租约与取消事实继续沿原执行核验，同一ConversationEffectGate仍处理效果与重置。
尚未接纳Work的普通前台保留原聊天版本取消。

SDK 回调等待约 5 秒可返回 `work_id/state/pending`；等待不是模型正文。
`agent.result(work_id)` 不依赖已经退出的 ContextVar，但核对插件所有权、批准版本、权限和 generation。
等待原因随结果返回；`agent.resume(work_id, text, request_id=...)` 将补充资料追加到原工作。
相同 request_id 重放幂等，不同内容冲突；不新建 work，不重置历史或预算。
接纳前超时取消准备，接纳后回调退出不取消 Host 工作。`return_to_caller` 的原同步结果
保留供调用方读取，不按七天期限回收；查询不重新执行模型或工具。
插件停止时取消进程内任务，保留已接纳工作和回执；失效授权不得继续执行或自行发送。
SDK 主调用使用 Host 原 actor/source，不要求真人事件；SELF 保自身主体和原 run，定时来源保原到期执行。
相同来源及参数读取原 Work 和保存结果；不同实际来源的独立调用隔离原协议上下文。
SDK 先沿原 durable 来源取得实际 Work 租约，再准备上下文和请求；同 Conversation 已占用的执行租约仍沿原执行层报告冲突。
不同真实来源和 Conversation 的独立调用是独立根，不声称继承另一根的预算，也不重置原 Work。
嵌套的独立 agent_sessions 保留自己的提示词和完整历史，不按固定 120000 字符截取历史或
24000 字符截取结果；每次模型尝试仍计入发起工作的预算，完整请求沿原 Runner/执行器容量
核验。初始输入没有可容纳的合法切点时仍报告真实容量错误。

恢复从原 journal 核对执行身份、预算、调用和回执；未决协议保持原签名和配对。
业务续跑应接当前获准聊天和必要任务材料，已用原文可退出私有执行尾部，不要求永久
恢复旧群史。改变已提交序列时使用显式新请求编排，不能混接新聊天与旧 opaque。
真实子 Work 也沿完整配对边界建立业务新链，保原 Work ID、来源、父子关系、消耗和执行回执。
精确私有恢复只取实际尚未执行的调用、仍开放的 Code 程序或 Provider pause；
普通 unknown 仅约束原效果，不阻止无关业务使用当前上下文继续。
本轮已经接纳的精确恢复继续使用其原 read set；下一次激活重新按原回执选择恢复方式。
退役工具合同及冻结最终发送计划只在离线升级保留原件，运行时不再提供旧计划执行或导入命令。
公共历史的冻结与采用见[会话 Compact](conversation-compact.md)；
该轮本地验证、合并及上线证据见[实施交付记录](../operations/work-context-delivery-2026-10-03.md)。
结果由调用方取得不代表 QQ 已发送；只有实际网关回执可以确认发送。

Journal 保留 dispatched、response、paired 的独立持久边界。不可变媒体只批量插入缺少的 blob，
按引用差额增删当前 Work 的 refs；其 pending/staged 输入尚未并入 transcript 时仍保留媒体引用。
其他 Work 的引用不被当前保存回收。恢复仍按原 call、response、预算和执行回执，不因减少媒体
写放大而跳过请求意图、实际响应或成对工具结果的记录。

新真人 keeper 入账后先尝试原 Work/wait；正 input ID、有效部分匹配 0 和未匹配 None
保持不同含义。原输入不要求先通过新接话资格，不以去抖窗口延迟已有工作的输入。
conversation 信号用实际触发该发布的内部事件 ID；全部 matched 引用仍保留在原 wait 条件中。
已在当前聊天展示的 anchor 不吞掉信号，consumer 仍带 wait_id、reason、conditions 与获准正文。
匹配到 conversation 等待时，在 reader 之外完成原语音转写和记忆入队，再准备接纳来源；
原 writer 复核等待、身份和代次后才发布 ready/admission/wake。部分匹配也先保留转写，
后到信号沿原事件读取真实语音正文。开放群聊逐条准备原语音和记忆，只合并新 Main 机会，
不把前条语音的准备交给最终候选；未匹配且无接话资格的消息不额外调用 ASR。

Work 发现首个 pending 输入尚未准备完时，立即按原 `WorkInputsPreparing` 退出本轮，
沿原 ending/reason 传递 `work_input_preparing`，由既有结算提交 `waiting_external` 并释放 activation。
准备提交按原输入 ID、Work 和 canonical generation 核验所有权，原输入的图像使用既有
媒体 blob/引用持久化；即使旧 activation 已退出或进程重启，也能恢复。准备完成与结算
在同一 SQLite writer 边界串行核验首个输入：先准备或先结算均续原 Work 入队；后面的
ready 输入不能越过未准备的首项。已准备或消费的输入重试只确认原记录，不覆盖内容或预算。
真人 live/repair 共用原 publisher，将 ready、ordinary admission 和原 Work 入队提交在同一事务。
repair 在 reader 准备来源，只有原 prepare_owner 证明属于先前进程的未发布输入才可接手；
不因当前准备超过固定秒数抢占。缺失或真实失效来源清退原未发布输入；清退与停放反序也由
原 writer 读取实际 mailbox，不保留已经清退的 preparing 等待。原 complete/fail 决定在准备
期间保留；同一 fulfilled wait 的旧结束决定沿原 wait_id 撤下，接续原预算，不重注册或重发。

上下文准备与 compact 的模型请求在 effect gate 外执行。gate 内只复核 turn snapshot、
read version 或原 Work source guard 并提交 projection；reset/privacy 不等待模型占用 writer。
`ContextAssembler` 在同一 reader 内选择公共检查点、有效投影、来源版本与高水位 H，
热准备只加载新增原文和真实未接纳洞；显式 raw-history reader 仍返回完整获准后缀。
Main 先选择实际投影与最终 context，再 compose 一次；同一公共投影 owner 贯穿读取与发布。
恢复与无公共投影的入口仍使用各自实际 context，本轮 short_state、插件资料不进入公共段。
当前公共表示由 `prompt_projections` 头与 `prompt_projection_chunks` 的当前 epoch 有序块持有，
0133 迁移删除旧 head blob 列。原头 CAS 只追加新不可变块与首次 selection 来源，未派发候选
不提升前缀；不以历次首次 selection 原件代替真实当前表示。失效、reset、事件删除、更换 epoch
及容量回收沿原 canonical 来源清退当前块，摘要与原执行/付费/发送回执保留。
轻量原分组、显示引用与精确洞索引归原 repository，同 view/LRU/字节预算回收，不保存正文副本。
候选固定捕获头、H、引用序号和洞；其他读取或提交不能改写旧候选。旧 current 剔除造成的真实
分组传播按受影响范围处理，不截断原事件；内存淘汰或真实失效时按数据库原件冷重建。
热 ID 追加为持久 AVL 的 `O(Δ log E)`，冷索引为 `O(E log E)`；最终请求展开与编码仍须线性
输出完整内容。准备继续位于原前台流程，不在入站时异步预装配。
`prepare_context` 先核原 journal 是否需要保留未决私有协议；合法 exact 恢复使用原签名与配对，
业务恢复使用当前公共历史和原任务材料，不再创建公共摘要等待或 extractive fallback。

普通聊天先只读检查同一内部 source key、actor 与交接边界是否有可选 Work；没有候选时
上下文准备不领取再释放空租约。只读结果仅用于安排准备，正式激活仍重新读取候选、
取得租约并执行原来源与状态核验，准备期间新增、取消或改向的 Work 不复用旧预检授权。
运行中的普通 Main 可在原业务续请求接纳已分类的普通补充：复用原音频、引用与媒体准备，
仅对最终请求确实保留的输入，将其 ordinary admission 与公共 projection 同事务提交。
准备期间的重读只刷新首批选中事件，不扩收后来到达的输入或越过它推进观察水位。
容量整理后按各候选实际保留的材料选择；一个候选被替换不连带否决另一个完整候选。
补充沿原 activation，保各事件的真实作者、Presence 和 binding；原 ToolActor、Memory、
预算与效果回执不改绑。准备材料留在业务私有尾部，纯文本沿原公开片段，不重复注入。
普通参与资格在选择补充前判定；同 Main 已选择输入的发布不再复查后到的 K、quiet 或语义纠正。
这些反馈收起后续选择和新 activation，不能通过否决已选择补充连带结束原 Main；真实来源与授权仍按原边界处理。
排队入口读同一 admission，不再为已接纳事件新开 Main；仅观察、晚就绪或被 compact
替换掉的输入保留原合法机会。历史曝光和公共/私有 compact 请求不取得 ordinary 归属，
接纳也不证明已经回复；空 final、NO_REPLY、取消或失败均不授予重放资格。
Compact 已公开同一投影后，业务接纳仍在原 writer 核验来源、隐私和投影版本，只跳过重复写入。
接纳后的派生参与反馈保存失败保留错误记录，不阻断业务请求；真实取消和接纳失败仍传播。
关闭 Work 运行时仍核验原公共来源及已采用补充，不创建 Work 或空租约。新输入的
ASR 使公共投影失效时，只有原来源核验仍有效才用原冻结字节追加新材料并重建投影
epoch；模型原请求链不变。准备期间正文、附件或身份变化，不给旧载荷换新来源。
已占用或过时代际的作用域领取、失效租约的续期和释放可只读拒绝；真实变更仍使用原
writer 与执行时的 owner/fence/generation/期限条件，不以锁外读取代替 CAS。

未决私有协议恢复在公共历史准备前核原 journal、固定合同和持久来源依赖；合法时复用原
协议，不准备最终不会派发的新公共窗口。业务恢复使用当前获准聊天与任务材料，最近一次
私有 compact 检查点仍归原 Work。整理在完整往返边界建立新链，来源冲突不重购已付响应。
新 Work source guard 使用 codec 3：普通事件来源按实际 owner、输入、版本和引用核验，
不绑定无关的诊断 privacy counter；存在 observation 依赖时仍核原隐私依赖。
旧 codec 2 继续按原完整指纹和原计数围栏读取，不原地升级或用当前上下文重新认证。
旧已付来源无法证明有效时保留原 Work、journal、预算和回执，沿原恢复失败处理；
不得借此重新购买响应、重派 unknown 调用或重发已提交效果。
Work 输入准备恢复使用 pending 且 ready 为 false 的部分索引发现有界候选；空轮询
不读取已消费或取消输入的历史页，也不申请 writer。准备 owner 不同或超过原 120 秒
时限的判定不变，unknown owner 的 SQL NULL 语义不变；取得 writer 后按原 ID、
state、ready、owner 和时限复核，保留原输入、Work 与累计预算。

Protocol GC 分 deleting 恢复与普通过期两个索引分支，先取有界 metadata 页再批读真实 refs；
owned 页也推进原进程内游标，固定 cutoff 和同排序高水位，次轮回访新插入及状态变化。
短 writer 重新核对原 metadata、期限和无拥有者条件并提交 deleting 屏障；文件锁只保护
实际文件查证/删除，释放后批量确认仍 deleting 且无拥有者的 metadata。GC 不持 writer
等待文件锁；publication 保留文件核验至原 journal/ref 提交的保护。同内容对象的新引用在
原 writer 撤销旧删除标记；GC 取得文件锁后重新读取删除资格，避免删除已复用的对象。
文件线程在调用方取消后须真正收尾才释放原保护。普通不可变检查点条目复用随原记录
生命周期释放的弱引用 digest/size 元数据，不另留正文，不因前缀超过LRU容量反复编码；
opaque 与会改变媒体外置的条目仍按原协议准备；复用不代替
发布前真实文件身份/完整性核验。文件缺失重新准备，变化重新查证，损坏拒绝发布。

热配置 `context.compact_trigger_tokens` 默认 `96000`，环境变量为
`CONTEXT_COMPACT_TRIGGER_TOKENS`，普通聊天、Work 与子任务使用同一提前整理触发值。
本地估值包含完整工具、原生声明、媒体与私有协议；触发值不限制可执行输入，也不要求
整理结果降到该值以下。没有可整理前缀或未得到有效检查点时，仍可容纳的请求继续执行。
硬容量只按当前 Profile 的 `max_input_tokens`、`context_window_tokens` 与本次输出额度计算；
未声明的项保持未知，两项均未知时没有本地硬容量兜底。旧 Host 双窗口
`context.window_tokens` / `context.work_window_tokens` 及其环境变量退役，不迁成 Profile 配置。
不维护触发比例、独立整理窗口或摘要输出限额；compact 沿当前 Main 的输出、思考、超时与调度合同派发。

公共 P 与私有 E 共用 `TurnExecution.compact_current`。P 请求在公共边界结束，结果发布到
原 Conversation 的 projection；E 整理实际已配对执行前缀，保留初始材料和切点后的原文。
完整请求触发后，在原公共片段、前缀与原生协议边界内优先复用 P，不要求 initial 单独越线；
E 未覆盖的 initial 保留。未交给正常 Main 的完整工具结果先保留，再选择可覆盖前缀；
rebase／恢复后的 portable 证据沿原调用键接续，compact 自身看过不算正常 Main 已观察。
执行摘要及后续正常请求认本轮原输入与已接纳 Work，不从背景任务推导目标；公共摘要不混入私有目标。
普通轮的 compact 不自动接纳业务 Work；其付费输入/响应使用既有私有存储记录，不作为公共观察。
已有 Work 沿原 journal 保存派发、响应与配对，冷恢复重用已付响应，未知派发不盲重试。
工具响应不执行，原请求仍可容纳时继续；真正不足则返回容量失败。新原件、真实回执、
预算、来源和隐私核验保留，不以摘要格式、额外汇报或全部子任务成功作为继续门槛。
详细窗口与 `/ai compact [重点]` 授权见[会话 Compact](conversation-compact.md)。

SELF 接纳记录构成持久待派发事实，以 `initiative:<run_id>` 唯一关联原 Work；同一标记
也写入 journal，避免恢复时把新 brief 或记忆重新追加成原触发。沙箱完成先唤醒原 Work/
子任务，保留执行 ID 和总预算。controller owner/epoch 切换只控制新接纳，不使已接受工作
失效；generation reset 或原授权失效仍阻止继续执行。发送沿原 Presence，不借当前主动路由。
Host 对原 run 独立对账真实效果；终态迟到回执保留，但不复活 Work、重复记账或重发。
保留型 `suspended`/`waiting_user` 不使原 run 丧失执行资格，管理 resume 由原 SELF 来源继续。
资料尚存的 failed 可明确沿原 ID 继续；子目标不为读取来源和预算重开已结束的祖先或原 run。
只有真实终态根 Work 才终结原 run，参与控制器不自行启动新任务。首次连接准备失败继续沿
原 Work 重排，不因三次重试或未开始执行另判终结失败。
WorkScheduler 始终启动；`RUNTIME_WORK_ENABLED` 控制普通聊天及 Host 调用的新 Work 接纳。
关闭开关不停止 WorkScheduler 所管理的已有 Work，也不阻断原定时 Work 的恢复与发生结算。SELF 接纳继续使用这套持久恢复机制，不能产生无人调度的执行记录。
SubagentScheduler 同样启动以恢复原子任务；新子任务接纳关闭不等于停止已有子任务的恢复。

## 预算、等待与异常

外部 Agent 由 ExternalAgent 服务管理授权和原操作关联，经用户电脑主动建立的设备通道连接原生 Codex、Claude 或通用 ACP 驱动。原生会话归用户／设备／实例，Work 只拥有自己提交的操作或监督关系。list／get 无需接纳 Work；prompt／queue／watch 保存原 Work 关联；steer／respond／cancel／detach 按当前人物授权操作原资源，不要求另接纳 Work。

prompt／queue 沿稳定调用键登记原 SandboxTaskRun 与 ExternalAgent 操作，再在 writer 外派发。run_id 是原 Work 等待引用，execution_id 是原生执行映射的内部 ID，二者不混用；native thread／turn／permission ID 留在适配器映射中。watch 使用 ownership=observe，只接收事实，不作为所属执行阻止 Work 完成。结束监督只 detach，不关闭用户 session、daemon 或兄弟执行。真正属于 Work 的未完成操作在来源撤销／终态时精确取消；取消接纳与原生终态回执分开。

重要进度、权限和用户问题先进入原 Work 私有输入与原回执，再确认设备事件。沿原等待／恢复链接入，不逐 token 激活 Main，不复活终态或已撤权 Work。回答绑定原问题和原执行，普通已授权操作不一律升级为人工批准。跨入口查询认原 Person 授权，不拼接 Conversation 历史。外部输出是待核对材料，交付继续通过 send／Social。

每次 external_agent 调用计入原 Work 工具预算；prompt／queue 登记新输入操作，steer 和查询不冒充新模型执行。原生内部轮数、usage、cache 只记实际报告字段，缺失保持未知。原生完整上下文和工具留在原生 Agent，Yuki 只接收必要进度、问题与结果引用。

旧服务器 ACP 不再接纳新 peer 或 prompt；legacy reader 仅保原 request／run／peer／cursor／permission／completion 的查询、原活动精准取消和结算，不作为后备设备 runtime。确认丢失查原 ID，unknown 不重发；历史回执不改名、不清空，也不声称擦除供应商或用户电脑上的资料。

自动化创建在最终短 writer 中复核永久创建者、当前权限与 SELF 场景；
同 creation key 读取原创建版本，不重新校验已经过去的首次日期。普通 create/update/pause/resume/cancel/run_now
与强制管理审计共用事务，审计失败一同回滚，before 使用写时实际版本；resume/run_now
沿当前任务定义合同核验未来安排。读取目录不授予修改权限，修改按当前 canonical owner 或 superuser 核验。

所有 Main 激活使用同一部署配置的模型/工具段预算。持久工作仅记入原根 Work 账本，
同一次发生不再叠加定时 limits、timeout、第二 run 预算或二次扣费。根 Work 的既存
累计值、明确有限预算和真实 Provider 超时仍保持；恢复不清零，不为升级取得新额度。
发生记录在真实根 Work 终态后结算并安排下一次时间；没有 Work 的激活结算其实际返回计数。
历史 messages_sent 的无 Work 成功分片口径和 Work 投递预留口径不能混称送达数。查询从原 Social
操作/分片回执解释成功、失败和未知，预留及待派发认原 Work 意图，不改预算或预留数来修正展示。
确定无 send 且无意图时是已知零发送，有派发或预留但缺确认则保持未知。丢进程或未知执行保留
uncertain，不重跑模型或工具猜测结果。

主模型请求在 `TaskModelExecutor` 统一接纳；AutomationWorker 不再按模型池 N−1 限制整项任务。
后台与维护请求合计不超过
`max(1, N-1)`，总请求不超过 N；N 大于 1 时保留一个前台名额，N 为 1 时串行并优先
接纳已排队的前台。普通持久后台请求使用不可抢占的 `BACKGROUND`；SELF 自省等
最佳努力任务仍可被前台抢占，`REQUIRED` 压缩按前台必需工作接纳。
`ConcurrencyManager` 只保留会话互斥和排队/执行中的取消登记。Runner 在实际请求
接纳后复核来源、登记一次逻辑请求预算和 dispatched 检查点；HTTP 重试不重复预留
这笔预算，额外真实请求沿原 transport accounting 计费。

实际 Provider 在每次派发前执行共同 dispatch guard，覆盖名额等待与重试退避后的来源变化；
Executor 不在同一物理 attempt 前重复调用它。原 Work source guard 首次完整证明依赖，
之后只在同一实例的来源 revision、身份与隐私代次未变时复用原 fingerprint，新增事件仍准备原依赖。
来源修订变化重新核原件，不因未观察的新语音转写直接拒绝；observation DAG 与跨会话输入继续单独核验。
临时 projection 准备以同一 guard 的 fork/adopt 保持 stage/rollback，journal 冷恢复重新证明来源。
这些读取准备不取代派发、journal/publication writer 和效果边界的真实授权与 CAS。

根 Work 的精确协议恢复保留原私有前缀、签名和工具配对；原调用配对后，在下一次真实模型派发前沿同一公共观察边界追加该 Conversation 的新片段。可回收投影缺失时按原内部 Conversation 准备公共历史与摘要，不以空基线代替，也不改写已付输入。原 Work 只增加本次实际交给模型的事件依赖，共享投影中别轮新增的片段不成为它的来源门槛。插件原本未获历史读取时，恢复也不装载公共观察入口。

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
主请求的派发 admission 在真实原生工具请求启动前调用该保护；不依赖已退休的旧循环。

定时 claim 使用每次唯一所有者并续期，首次发生提交时核对原 owner、到期时间和版本。
会话忙时不分配新 run。原版本与绑定的 canonical 目标保存于既有版本表，新定义只用于
未来发生；缺失旧版本事实不按当前角色、日期或 hash 猜补。实际身份从原永久 Person
当前账号关系解析，历史创建账号仅为来源元数据。
每次新发生使用原目标会话的当前 generation；创建时的旧 generation 不封锁未来发生。
已接纳 Work 仍绑定原 generation，重置不会重开旧工作或清零其预算、效果与回执。
Main 的读取与内部执行不要求发送路由未暂停，也不按后来的主动发送路由重选原触发目标。
实际发送沿共同 Social 路由与发送权限核验，暂停发送仍在发送处生效。

Work、子任务与定时激活的租约由原 task 监督；失效后沿原 Work/run 事实收拢，不新建
执行身份或清空检查点。只有明确 SQLite BUSY 且 Database 已确认没有提交的续租错误，才沿原租约期限重试；
LOCKED 不当外部 writer 繁忙；心跳不重放模型、工具或计量。已有 Work 只由共同调度器
恢复，不再周期扫描 running_runs、步骤游标或 pending_work_id。未接纳 Work 且丢失
进程的原发生一次记录未知，不重新激活。

超时的写入和发送可能已经提交。插件副作用不因 transient_once 自动重试，未知结果携带
`uncertain` 回执，同一原调用不再取得派发资格；其他独立调用仍可执行。读取可按声明重试。
插件 Work/Social 状态由共同 Host 类型化回执解释，业务 JSON 的 state、pending、
work_id 或 usage 不授予恢复、控制或预算权。查询不重计旧累计，不重新执行原工具。
原效果未知时保留未知，同一原调用不得重新派发，独立新调用仍沿普通执行合同处理。

普通回答无需接纳 Work。模型最终文字可结束内部循环，但不触发发送；
工作完成由 Agent 提交决定，原 writer 收拢实际所属执行。发送是否成功只看显式发送回执，
不自动把工具 JSON 或固定成功句发到聊天中。
分条 `send` 在首条网关调用前持久记录计划段数，并按原调用的确定性子调用 ID
核对每一段 Social 回执。完整计划和同目标的全部子回执说明实际投递，部分成功不
冒充整次发送成功；缺失计划或结果保持未知。领域回执不补造原 Work 工具结果，也不
给未知调用重新派发资格。原 Work effect 与 Social 操作之间的真实归属继续核验。
明确管理命令和模型完全不可用时的运行状态反馈与普通 Agent 正文区分。

任务的沟通要求沿原 goal、用户追加输入与 context_note 保留，不另外保存汇报模式。
用户要求安静、SELF 自主工作和后台维护按原指令安静处理，新追问照常响应；用户委托的工作在后台运行仍按原反馈约定处理。

沟通由 Agent 在原循环决定，send 是有原领域权限和回执的显式工具。开始、实质进展、阻塞和追问需要说明时直接发送；没有必须先通报的门槛，不再分类汇报、保存答复水位或哈希批次，也不补固定暂停模板。
同批 send→complete 与 complete→send 均依原调用顺序、来源及授权执行并配对；已确认发送不回滚或重发，未确认不补造成功。当前目标的真实 failed/cancelled 撤销、来源失效、撤权或预算拒绝按原事实阻止新效果；取消另一个目标不撤销当前执行权。记忆修改与普通生命周期决定不迫使整批发送或脚本停止。
`task_control.derive` 沿已有 Work 派生普通业务子目标；get/list 可查父子与祖先，指定 work_id 的 cancel/fail/resume 使用原管理 writer。直接父与预算根不同，子任务继续保留来源和预算，不重开已完成祖先。

`task_control.wait(run_id)` 是已有 `owned_run` conditions 的简写。等待子任务时，`run_id` 使用原 `subagent_start` 回执返回的 `child_id`；已结束或未知的执行也可提供真实完成信号。也可登记一次性 `conditions`：
`time_due`、当前 canonical Conversation 的新消息、获准插件发布的事件或所属 run。主工作和子工作都可按自己的原 Work ID 登记、接收并消费信号，再沿原 lease 完成。
同时接收会话输入与子结果时，用 `any` 组合 `conversation` 和 `owned_run`，后者的 `run_id` 仍填原 `child_id`；只等子任务可直接使用上述简写。
等待是一次性的。`any` 任一命中后，处理当前输入并继续可执行工作；仍依赖未完成子任务且当前没有可执行步骤时，按原 ID 等待剩余结果，不重新派生。
`all` 等全部条件满足，局部命中留在原绑定中。集合可选 `deadline_at`，没有隐式 90 秒有效期。绑定持久化在
`runtime_work_waits`，带 Work ID、generation、主体、事件水位和唯一调用键。消息入口、
插件发布事务与 WorkScheduler 的时钟分别交付信号；命中时同事务登记原 Work 输入并入队，
不创建第二项 Agent 工作。超时向原 Work 交付明确结果，
不视作同意。插件必须显式设置 SDK 的 `resume_waiting_work`，否则维持原通知和新轮行为；
命中原 Work 的事件不会同时启动独立的插件 Agent 轮。等待命中与输入只保存事件引用和匹配元数据；构造请求时按当前 canonical Conversation、generation 和可见性读取原账本正文，历史 matched.text 不再采用。
只登记 `conversation` 不会因普通子任务完成通知而唤醒。完成结果持久化、父 Work 入队、模型实际观察和用户交付是分别发生的事实；普通完成通知不撤销已接纳结束决定，恢复仍可按原 complete 直接收尾。用户交付以显式 `send` 的真实回执为准，完成通知不提供新授权。
AutomationWorker 只扫描未来安排、接纳唯一发生并结算真实结果。实际 Work 的等待
和信号由 WorkWaitRepository 与 WorkScheduler 沿原 Work ID 处理，不唤醒第二个步骤机，
也不因停放或迟到信号创建另一份 run。
信号到达不解锁 `suspended`：容量和来源失效等独立暂停原因不被信号覆盖，信号作为待处理
输入保留在原邮箱。控制面的原 Work 详情与等待详情（`wait_status` 的 `work`）
同时给出暂停原因、已到达信号及其输入状态、可用的原 resume/cancel 动作；显式 resume 消费原信号，
不创建新事件或新状态。
明确 resume 或修改目标在同一 writer 撤销被替代的一次性等待，旧 timer 不再唤醒新执行。
`task_control.wait_status` 查看原 Work 的绑定及未满足条件，`cancel_wait` 撤销它；
`waiting_user` 只由原提问内部发送事件所收到的同一 Person 回复自动恢复，普通群消息仍按新输入处理。

时钟轮询先分页只读观察等待条件、原 Work 终态及 Conversation/Work generation，
没有变化时不取得 SQLite 写锁。有变化的最多 128 个候选在短写事务中重新核验，
再更新部分命中、失效状态或向原 Work 投递；所属子 Agent 的状态取自原 Work。
输入准备修复与未引用媒体回收也先只读发现候选，空轮询不产生写事务。
终态 Work 不按全局条数删除原 journal、效果、输入、等待或检查点；原执行 ID 保留，
自动化结算仍能关联原 Work。工作者显式归档与 canonical 隐私清退沿各自合同处理。
表情原件、标签和索引沿共享沙箱文件维护，不在主业务库领取分析租约。Work 激活的有效期在执行
数据库语句时核验；等待写锁期间到期的旧租约不能续期或提交状态。

## 持久化与验证

同一个 Database 的业务和诊断写入共用 FIFO owner，先排队，再取得连接并开始
`BEGIN IMMEDIATE`。owned 读取使用 `read_session()`，不 autoflush、不自动提交；组合操作
显式复用当前 writer session。历史扫描、分词、文件和外部工作在 writer 外完成，当前 writer
只核验选定来源、真实身份、租约和原始回执，并提交完整原子单位。

付费响应发布仍保留原 stage/finalize/journal。仅已确认未提交的 BUSY 可重备同一份纯数据库
发布；commit 未知不回到模型或工具重放。工具结果保存实际提交三态和原错误阶段，错误展示
不把已确认提交改成未提交。跨入口的 client input 在发布 writer 中重新核对原事件、邮箱和 Person。

既有迁移保存自动化创建调用键、历史发生与累计计数；0110 只发布干净未来定义，旧执行记录存档。
0071 为 SELF 自动化增添可判别创建者和唯一调用键，并增添 SELF 时区、Work 等待绑定与
插件恢复选择字段；原 Person 任务和运行回执不重建身份。
同一 Host 调用重放和并发提交只登记一次，参数冲突明确拒绝；同事件的两个合法调用不合并。
创建调用恢复只认原调用键、原参数和领域回执，已确认结果先于新 schema 校验读取。
同一事件的不同合法调用不合并，不以自然语言或结构等价查询代替原创建回执。
迁移保留既有 run、Work 的消耗与旧步骤原件；缺恢复所有者的旧 SDK/自动化 work 明确暂停并保留原因。
迁移不重跑旧日记、不清除原 work、artifact、预算或交付回执。

V6 迁移链从 `0064` 接到 `0066`（autonomy 接纳和反馈）→
`0067`（SELF 工具证据及自省独立回执水位）→ `0068`（内部引用事件）→
`0069`（Social 发送回执的内部事件关联）→ `0070`（无来源机会与讨论线程）。
SELF 工具证据沿原真实执行来源归属，不通过假聊天事件进入 Memory。
定时来源可用已保存的发生、原 Version 和 SDK 执行锚点；0111 退出早期 event/run
二选一的来源 CHECK。自省迟到回执按原 run 的独立水位续读。

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
有界快照入原共享队列；锁外编码与 SQL 准备由同一个空 Context 消费者处理，
来源/隐私条件在最终 INSERT 原子核验，不逐条建立独立来源 reader。
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

WorkScheduler 沿原循环尝试 repair_inputs/reclaim/protocol_cleanup，再选择业务；
维护错误记录原类别，不阻断正常 queued Work 的选择。SubagentScheduler 同样将维护和
取消查询错误与 worker 选择分开。之后按会话 scope 有界派发 resume（最多 8 个并发，
同 scope 不重复）；health、phase_timings 和慢阶段日志反映实际结果，等待驱动仍独立。
Work 选择沿仅含 queued/running 的索引按 Conversation 游标轮转，逐个读取该 scope 最早符合
原来源、租约与恢复时间的 Work，直到填满空闲名额或完成一圈。空页环绕；忙碌或尚未到期
的 scope 只推进遍历游标，不消耗空闲执行名额，不一次加载整个 scope 的积压。
跨 scope 使用轮转公平性，scope 内仍按 updated、内部 Work ID 排序；游标只存在调度器内存中。

尚未接纳工作的普通聊天，Provider、配置、空响应、容量、校验和内部异常保留原日志、
ProcessResult 原失败原因及 TURN_CLOSED，不因异常自动向 QQ 插入固定报错。
工具失败仍按原工具结果交回模型；模型请求失败不另外购买一次请求来解释错误。
旧 detached sandbox continuation 的 claimed 状态由 0116 一次性迁移为 blocked；
保留原 request/run ID、次数和执行回执，启动不再重复清理。
没有持久恢复所有者时不自动重放；有 Work 时由原 supervisor 保存真实状态和恢复事实。
发送或存储未知仍保持未知，不因删除通知改成成功。

## 执行诊断

[执行过程查看](execution-trace.md) 在共享执行器和协议 HTTP 边界记录逐次请求、
Provider 实际返回的可读思考、重试与单项工具结果。逻辑 turn/model 不再复制整份正文，
空 Provider 结束与重复工具批次诊断删除。它独立于覆盖/回收的恢复 journal，
没有重放、预算重置或发送权限。Chat 在组装历史和 Memory 前绑定诊断删除代次，
隐私删除后旧调用不能再写回旧上下文。
诊断数据库/编码失败只报告 coverage_incomplete；取消及进程终止照常传播，
不承诺取消时仍能保存或交还已收到的响应。过程记录复用上述有界诊断队列，
异步提交失败由诊断日志和 health 报告；轮次结束标记只反映当时已知的记录缺口。
诊断可以丢弃，不建立额外持久恢复状态，也不替代任何真实效果回执。

纯文本 LENGTH 截断保留已付费响应并报告 incomplete，不自动购买下一轮纠正请求。原本未执行的截断本地工具调用仍配对 non-execution 回执；受支持的 Provider pause 仍按原 checkpoint 续接。
