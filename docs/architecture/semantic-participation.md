# 语义参与 V6：宿主接入与验证边界

2026-10-03。独立仓库：
[Yuki-Semantic-Participation](https://github.com/YuanYeYouTao/Yuki-Semantic-Participation)。
本文描述 SELF 接入与普通持续接话的合同。实现、验证和生产启用状态分别由交付记录证明；
历史 T20 真实 QQ 集成验收不是本轮持续接话的验收结果。

## 分工与唯一接纳

独立库负责语义观测、参与状态和 proposal。Yuki Host 负责 canonical 来源、唯一控制器、
权限、接纳、Main Agent、steer、工具、工作恢复和发送。Jev 使用自己的观测协议，
不生成聊天回复，也不构成第二个 Agent Runner。

`SemanticParticipationService` 读取已提交的 canonical 群事件和获准的记忆候选，按
Conversation/generation 保存控制器快照。来源是带版本的内部 event 或 memory ID；
目标人物只是交流对象，不是执行主体。来源正文和资料包均是不可信上下文，不能授予权限。
观测和历史读取完成后才进入短接纳事务，事务中不等待 Jev、主模型或 QQ 网关。
启动和运行期间会主动发现当前获准的群会话代次；即使 `/ai new` 后还没有新的群消息、
新代次尚无 selector 行，也会建立控制器并按现行配置选择 owner。只读取新代次事件，
不补采停机期间的机会，也不延续旧代次待接纳 proposal。

`AutonomyBinding` 持久保存 master/external 开关、唯一有效 owner、controller_epoch 和
revision。有效 owner 只有 off、legacy、semantic：总开关关闭始终 off；显式关闭
外部控制器时使用 legacy；开启时由 semantic 接纳新机会。Jev 健康只控制观测退避，
不切换 proposer 或阻断无来源苏醒；缺凭据时没有新观测，仍可使用真实活动与已有反馈。
暂时评分失败、合法 unknown 和群聊安静不是同一种状态。

原 `AutonomousGroupService` 保留本地机会评分，经同一 selector 和 `accept_legacy`
登记自主工作；不再把最新真人的 profile/权限交给 `ChatService.respond()` 冒充发起者。
两种 proposer 共用来源去重与一个活跃自主 run 边界，不能同时为同一机会开工。

尚未建立互动时，单纯名字提及仍是优先观察线索，不等同于 `@`。
Jev 判断为邀请/续聊且回应楼层留给 Yuki 时，即使无法归入已有讨论，
Controller 可采用 Host 提供并核验的该作者新讨论单元。真人邀请和已有互动的续聊进入
原 `USER_MESSAGE` 主入口；普通续接不创建自主 Work，也不逐条等待 Jev 评分。
非请求式加入、回忆、联系和无来源参与按[连续概率模型](autonomous-participation-model.md)
决定时机；Host 继续核验来源、停聊边界、并发占用、代际、权限和发送链。
活跃/非活跃观察去抖、调用间隔、最长等待分别为 2/8/15 秒和 3/12/20 秒；
conversation 观测在源事件后 90 秒内完成才有资格，支持最多到源事件后 150 秒，
且评分完成后最多再保留 75 秒。这是来源新鲜度边界，不是整个会话的统一有效期。

接纳在数据库内 CAS 核对 owner/epoch 和 proposal 身份；同一 proposal 重放返回原 run。
已考虑来源按类型、内部 ID 和 revision 持久去重，跨 legacy/semantic 共用；仅被动出现在
上下文的消息不会因此标成已考虑焦点。接纳后的原 run 可在模式切换后继续查询和接收反馈，
controller_epoch 不是已接纳 Work 的执行授权版本。
普通真人入场使用 `ordinary_turn_admissions` 冻结原事件、actor/Presence、激活及单元来源，
与自主 proposal 分开；自主开关关闭不拦截原确定性入口。语义开关开启时，获准群中的真实
邀请和唯一持续互动也不借 SELF owner 取得普通回复资格。接纳记录不表示 Main 已完成，
不能用它恢复整轮普通闲聊。Input 只表明真人入场，不证明 Yuki 已发言；快速续接还需
原单元的真实 message 表达回执（存在时保留真实锚点），或明确的局部 `join/stay` 意愿。仅入场后 `NO_REPLY` 不自证持续互动。
单元保留建立时的原来源 basis，新的 input_ref 与活动时间独立更新，不把每轮输入累积为
后续入场的全部祖先依赖。每次真人 admission、效果关联和自报仍保留本次原来源；
快速续接既核验建立来源，也核验当前输入，不能靠稳定 basis 放过已修订或撤回的最新输入。

原始观测记录的裁剪不等于结束持续交流。已核验单元可保留有限来源指纹、明确的退休证明
及一条原真实发送回执，用于验证原人物与讨论；不保留整段正文，也不扩大通用来源有效期。
指纹本身不能补出参与关系。来源撤销、unknown 或不同单元的重新解释会使派生关系退出；
晚到自报不重建它，真实晚到回执仍按原身份入账。资源压力优先清理旧派生关系，新的真人
输入仍可接纳。旧读取器折叠后缺少必要证明时保持未知，不猜回持续互动。

`intrinsic` 是无 event/memory 来源的独立提议类型，不伪造 Jev 观测或真人消息；
已核验的本群互动、实际自主 Work 密度和回应共同决定机会率。极久沉寂时机会率
随真人活动与沉默情境持续趋近零，无真人活动证据时为零；不等待累积门槛，也不将静默转成真人来源。
宿主只在 semantic owner、有效群授权和无活动 Work 下接纳，并拒绝提议之后出现真人发言、
Yuki 发言或未解除的全群停止边界的待接纳提议。已接纳 run 仍按原执行身份恢复。
获准群记忆可在没有近期真人消息时形成 recall 候选；无来源机会由主 SELF 决定行动或沉默。

## SELF 主体与同一执行链

持久 `AcceptedInitiative` 是待派发事实，以 `initiative:<run_id>` 唯一关联原 Work。
接纳后进程退出时，宿主重新核对原 Work；存在则接回，不重新创建任务、重置预算或重放发送。
WorkScheduler 调用 `generate_self_initiative`，随后仍经过 `MainAgentTurnService`、
`AgentRunner`、`MainAgentBackend` 和既有工具执行器。

- `TurnOrigin.SELF_INITIATIVE` 的值为 `self_initiative`；`ToolActor` 和 `TurnAuthority`
  明确携带 SELF principal、initiative run 和 canonical 场景，不携带真人 user/person。
- `ConversationTurnSnapshot` 的内部事件 ID 与 initiative run 严格二选一。SELF 不制造
  `InboundMessage`，不使用假 QQ 消息、不借最近发言者或目标人物的权限。
- 每次请求、工具执行和发送准备前校验原 run、Conversation generation、Space、Presence
  及唯一有效 QQ Binding。发送使用接纳时原 Presence；主动发送路由切换不替换执行主体。
- 控制器切换本身不取消已接纳工作；reset/generation 失效、原场景权限或 Presence 失效仍
  拒绝继续执行。终态迟到的真实效果可以入账，不能把终态改回运行或触发重新发送。

工作区、终端、沙箱与子 Agent 复用原执行 ID、租约、回执和根预算。子 Agent 仍由既有
SubagentScheduler 和 Runner 运行，身份保持 SELF，内部结果交给父 Work；它不获得 QQ
发送权，也不继承主 SELF 的“发送正文或沉默”反馈。没有另起一套工作执行循环。

SELF 社交范围目前限定为当前群：发送、当前空间通讯录、历史和成员查询。不能借此私聊
目标人、访问目标人的私人资料、撤回或戳人；本期不支持 SELF 自动结构化 @。
Memory 按需查询当前群与该群可见的 SELF 范围，不自动预取或从最后发言者生成个人上下文。
完整工具声明仍固定提供；工具是否可执行由上述可信来源和现有权限检查决定。

## 交付、回执与缓存

主 SELF 只有显式 `send_message` 才能产生可见回复。模型最终正文是内部结果，
按共同完成合同核验，不购买未发送纠正请求。调度器不自动转发最终文字。
`NO_REPLY` 可以合法完成原 Work；`return_to_caller` 不生成面向某个真人的固定收尾通知。
实际发送未知时保留 uncertain，不能据此重发或声称已发送。

自主轮提示词先让 SELF 从当前时间、群聊历史、获准记忆和自己的兴趣寻找具体想说或想做的事；
无新真人消息、无人在线不自动等于应当沉默。可自然开启话题、接续有意义的讨论、查询或推进
自己的工作；确有后续目标时使用现有 SELF 自动化，原 Work 等信号时使用持久等待。
不得把旧消息伪装成新请求、为凑发言复读或虚构在线对象。没有自然切入点时仍可 `NO_REPLY`；
这只是主 Agent 的表达选择，不改变控制器机会函数、Host 接纳与发送授权。

社会效果依据真实 SocialOperation 回执归属 run；单次 Work 不因分条或多次发送
增加自主调用密度。确认公开发送使该 Work 产生一份等待回应的暴露；
分条及文件说明仍归并到原逻辑发送回执。
确认发送的内部事件 ID 关联原 run 或真人入场记录冻结的讨论单元；平台实际群目标仍为
Space，不能为语义关联改成某个人。普通发送通过原回执和入场来源对账，没有原绑定就
保持未知，不按后来变化的当前讨论猜回。已有唯一互动的无引用输入可以续接普通 Main，
这不制造 Jev observation 或 observed 概率；歧义和需要纠正的原焦点仍请求原语义观察。
工具效果、模型请求消耗、取消、暂停与无回复分开反馈。Host 先持久登记反馈，再回放给
控制器；已结束 run 仍对账迟到效果，反馈序列和效果 ID 防止重入重复累计。
保留型 `suspended`/`waiting_user` Work 不终结 run：反馈保持 running，原 run 仍可经管理 resume
由原 SELF 来源继续；参与控制器不自行重跑。只有真实终态 Work（completed/failed/cancelled）结束 run，
已终结的 run 不被 resume 复活。

活跃 run 保持恢复优先；终态 run 按原稳定 ID、有界页和每次巡回固定上界轮转，
不只读取最近修改的尾页。游标只存在于进程内，不承担效果事实或跨代所有权。
每页在显式读快照批读原 Work、Social/MemoryTool 回执、child 计量和全部已提交反馈；
无变化时不申请 writer。新增反馈仍按原 64 effects/页原子提交，每 run 单轮页数有界，
剩余回执下一巡回继续。准备快照升级竞争只有限重备数据库计划，不重跑原模型或发送。
Host 提交后按原 scope/generation 聚合回放并保存一次控制器；冷加载也回放已有反馈。
普通入场及 boundary 来源按当前核验阶段批读，跨外部等待重新核验，不缓存授权。
冷加载先为本页未知入场来源建立版本，再批量核验，保持原逐事件 projection 顺序。
尚未识别且没有保留依赖的来源暂为 unknown，不授权、不提前失效；已知来源缺证明仍失效。

工具证据使用内部事件或 initiative run 严格互斥的来源，并按 run、execution、Provider、
工具名和实际调用身份去重。SELF 的调用身份以原 journal chain/请求序号限定 Provider
call ID，避免不同响应重复使用 `call_0` 被误合并；Provider 请求历史仍保留原始 ID。
无聊天消息但有工具执行的自主回合仍可进入 Self Reflection；
每个 run 使用独立回执水位，迟到回执进入后续窗口，不借聊天事件水位、不伪造聊天事件。
详见 [Self Reflection](self-reflection.md)。

主入口使用同一固定提示词、工具顺序和参数 schema。run ID、资料、时间与状态在固定部分
之后；不根据 legacy/semantic 或场景动态裁剪声明。Work journal 保存实际请求和 Provider
continuation；未决协议按原检查点精确恢复，正常业务续跑使用当前获准聊天及必要任务材料。
`initiative:<run_id>` 防止将变化后的 brief、short_state 或记忆当作新的发起事实；私有尾部退出或
整理采用明确新链，公共聊天无需清空，预算与效果不清零。具体边界见[主 Agent 合同](main-agent-runtime.md)。

可选 `<yuki-state>` 尾段描述主 Yuki 自身意愿，不要求逐轮输出。主 SELF 自报按真实 run、
请求序号和 response ID 记录；普通主轮按原持久入场绑定、激活和请求顺序归属局部单元。
`quiet` 收起自己的参与，不制造用户停止边界，不广播关闭全群；原焦点可选择性请求 Jev
纠正。无 hint 不清空原参与，迟到旧响应不能覆盖新意愿，保存失败不重跑或重发。
可见发送和最终正文会剥离该控制尾段；格式错误不靠额外模型请求修复。
自报不能作为其他人的语义证据，也不允许工作者冒充主 Yuki 自报。
普通轮允许空正文、仅状态尾段或内部 `NO_REPLY` 安静结束；它们不发送文字，也不触发
未发送正文的补答。普通入场记录不是 Main journal，进程退出后不自动补跑未完成普通轮。

## 配置与验证方式

两种 proposer 均使用持久 Work 调度。WorkScheduler 始终启动，既有已接纳工作和 SELF
工作必须继续恢复；`RUNTIME_WORK_ENABLED` 只控制普通聊天新工作接纳，不是恢复停机开关。
SubagentScheduler 同样持续接管已接纳的子任务；子任务接纳开关不取消原父子执行关系。
Jev 密钥不得写入公开配置、提示词或验收记录。

开放群聊来源机会率由 `source_interval_seconds` 调节；参数含义与挂载覆盖见[自主参与模型](autonomous-participation-model.md#在线调参)。

| 配置 | 默认值 / 含义 |
| --- | --- |
| `conversation.autonomous_enabled` / `CONVERSATION_AUTONOMOUS_ENABLED` | 保留原自主总开关；还要满足群 Space 的 enabled/autonomous_enabled。关闭后不能被 fallback 反启 |
| `conversation.semantic_participation_enabled` / `CONVERSATION_SEMANTIC_PARTICIPATION_ENABLED` | 默认 false；开启使用 semantic，显式关闭使用 legacy；观测故障不自动回退 |
| `SEMANTIC_PARTICIPATION_API_KEY` | 默认空；Jev 观测凭据；未配置时不产生新语义观测，不改变 proposer 归属 |
| `SEMANTIC_PARTICIPATION_MODEL` | 默认 `jev-1.13.0` |
| `SEMANTIC_PARTICIPATION_STATE_PATH` | 默认 `data/participation.sqlite3`，控制器快照；不代替 Host 的 run/Work/回执数据库 |

前两个配置支持现有全局/群作用域管理接口。当前没有名为 `shadow` 的运行配置开关；
关闭 semantic 表示恢复 legacy，**不等于影子 Jev 仍在后台评分**。影子对照应使用隔离的
事件重放和观测流程，不提交 proposal、不调用 QQ 发送，不能把它描述成现网已启用模式。

## 迁移与验收边界

迁移链已顺延：`0065` 为 PR #111 的关系历史索引，`0066` 为 autonomy selector/run/
来源去重/反馈，`0067` 为 SELF 工具来源与自省回执水位，`0068` 为接入时解析的内部引用事件，
`0069` 将 Social 回执关联至内部事件，`0070` 持久化自主触发类型与讨论 thread。
没有修改已发布迁移来加入新行为；
新增表默认不会开启 semantic，也不会恢复被用户关闭的自主总开关。

本地定向验证覆盖接纳并发与重复、模式切换、代际失效、原 Work/沙箱/子任务恢复、当前群
发送权限、独立自省回执水位。真实 Provider 序列化对照覆盖 Responses、Chat Completions
及 Responses 原生工具声明：24 次请求后原 Work 第 25 次续跑，普通续跑前缀逐项保留。
这能证明请求合同，不是线上缓存命中率；未取得真实缓存指标时保持未知。

有限真实 Jev HTTP 验证使用合成样本，只检验协议、投影、延迟和分歧；期望由开发者编写，
不是独立人工标注准确率，不能宣称改善了群聊。详细证据见独立库的
[实现记录](https://github.com/YuanYeYouTao/Yuki-Semantic-Participation/blob/main/docs/implementation.md)。
T20 真实 QQ 小范围集成、长期群聊效果和生产负载仍未验收。代码合并与本地测试
不能替代这些证据；后续上线另按确认的发布流程处理。

Health 的 `diagnostics` 只返回有界聚合：最新 128 条当前 generation selector、最新
128 个已接纳 run 及最多 1024 页反馈、已加载控制器的保留快照。owner、回退、候选和
observed/predicted 支持分别计数；NO_REPLY 比例以选中 run 为分母。真实消息效果、
工具回执和已计费模型请求分开去重，工具回执本身不表示业务成功。反馈被截断时，效果
比例返回未知。token 只汇总保留观测里 Provider 明确提供的值，另报样本覆盖；延迟、
全生命周期失败率和独立人工准确率没有持久事实时返回未知。此健康诊断不能代替 T19
标注验收，不输出正文、人物、会话 ID 或原始异常文本，也不改变控制器状态。
观测最近失败按有限错误类别和 HTTP 状态聚合；这是快照保留的历史事实，
不代表当前仍故障或全生命周期错误率。成功后的健康和连续失败数独立清零。

WebUI 的 Jev/Host 时间线沿[执行过程查看合同](execution-trace.md)保存新增的真实
观察和接纳尝试，默认保留 30 天。该诊断不参与语义 apply、权限、proposal 或恢复；
记录失败不会丢弃成功观察、再调用 Jev 或改变原重试/退避。完整概率与来源正文需
独立 `control.execution.content.read`，元数据分页不加载正文；旧判定不猜测回填。
当前控制器状态快照仍是有界即时状态，不充当全生命周期统计。

控制器快照仍使用独立库的 revision CAS 和容量约束。Host 用一个专属线程创建、读写及
关闭快照 SQLite 连接；事件循环只维护控制器，提交时冻结 JSON，不把可变状态交给线程。
快照等待写锁不会阻塞普通聊天及健康接口；保存仍等待真实提交结果，不把快照当成可丢弃
的诊断写入。保存期间取消时先对账实际 revision，再传播取消；缓存加载、淘汰及保存
串行核验，避免新增 await 后产生重复控制器或用旧 revision 覆盖快照。
