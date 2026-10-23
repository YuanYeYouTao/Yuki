# 语义参与 V6：宿主接入与验证边界

2026-10-23。独立仓库：
[Yuki-Semantic-Participation](https://github.com/Institute-of-Agent-Life/Yuki-Semantic-Participation)。
本文描述 SELF 接入与普通持续接话的合同。实现、验证和生产启用状态分别由交付记录证明；
历史 T20 真实 QQ 集成验收不是本轮持续接话的验收结果。

## 分工与唯一接纳

独立库负责语义观测、参与状态和 proposal。Yuki Host 负责 canonical 来源、唯一控制器、
权限、接纳、Main Agent、steer、工具、工作恢复和发送。Jev 使用自己的观测协议，
不生成聊天回复，也不构成第二个 Agent Runner。

人物同一性查询从原checkpoint读取actor/target事实，再由Host按内部ID批量解析当前人物；`participation_actor_ids()`不评估参与资格、不重放belief，也不修改checkpoint。来源分页、水合及冷恢复不得为收集人物ID调用全局`participating_units()`；当前参与判定仍只在真实决策处计算。

普通接纳先在写事务外收集所需人物 ID，包括原 SELF 单元、语义 review 和 proposal 保留的目标。
writer 按这些固定 ID 批读当前人物归属，继续使用独立库原有的当前邀请、单元、边界和到期判定；
不在 writer 内遍历历史事件收集人物，也不把准备时的资格保存成许可。当前判定实际引用了新人物时，
退出未写入的事务重新准备同一接纳；只有确认回滚才能重备，未知提交沿原事件与接纳回执处理。

普通输入的 binding 准备复用入口已有的原事件与配置；场景在一次关联读取中解析原
Conversation、Space、Binding 与接收 Presence。事件、场景及配置的准备在 Controller 应用锁外，
取得锁后读取当前场景，不用准备期间的旧 generation 或路由覆盖新值。控制器应用、历史水合、
冷加载唯一性和共享快照保存仍沿原锁，不据此宣称跨会话全部无等待。

接纳反馈与普通自报批读真实已提交 admission 和当前来源，局部幂等恢复原输入，不重新扫描
效果、读取配置或 hydrate 整页；缺少控制器时仍沿原路径冷恢复。admission 提交成功而派生
反馈失败，也可由这份原 admission 局部恢复，不丢后来的合法 hint。quiet 在主响应观察返回前
应用，必要的 Jev 观察排队和快照保存同步完成；未保存的 hint 不能仅凭 admission 重建。
锁外准备遇关闭时沿原 store 生命周期退出，取消保存仍确认实际提交的 revision 后传播。

绑定仍读取真实近窗历史，供新焦点冻结引用、讨论与 SELF 锚点。新发送已入账而后台尚未
tick 时，Social 回执只能证明表达，不能制造尚未读取的事件 SourceRef；不能只投影当前输入
就省略这份历史。原来源最终按本次 basis 核验，不在其前重复单查同一焦点。

`SemanticParticipationService` 读取已提交的 canonical 群事件，按
Conversation/generation 保存控制器快照。新观察只以真实人类事件为焦点，context 可包含真实人类和 SELF 发言；
来源沿带版本的内部 event ID 传递。后台 Memory 扫描、seed 事件及 recall/contact 新候选已退出，
Memory 仍由主 Agent 按需查询；
目标人物只是交流对象，不是执行主体。观测正文保留原事件中按原位置还原的 `@名字`，
与 Main 共用入账时的目标及名称快照；不在 JEV 侧再补查资料或把其他成员改称 Yuki。
Host 从同一原 segments 复制窄 `EventMention` 到 `ScopedEvent.mentions`，保原顺序、重复、展示名、
位置及可证明的 Account／Assignment／Person／Presence。普通文字中的 @ 不反造原生提及。
新 Jev 请求使用 `semantic-state-v3`：人物随原作者共用当前同人投影，Account 在该请求内只用
中性 aN 关联，Assignment／Presence UUID 留在本地原 Snapshot；不改变参与 target 或授予权限。
原来源修订包含提及快照，旧未知位置保未知，旧已付 Snapshot 不补查或改写。
观测和历史读取完成后才进入短接纳事务，事务中不等待 Jev、主模型或 QQ 网关。
启动和运行期间会主动发现当前获准的群会话代次；即使 `/ai new` 后还没有新的群消息、
新代次尚无 selector 行，也会建立控制器并按现行配置选择 owner。只读取新代次事件，
不补采停机期间的机会，也不延续旧代次待接纳 proposal。

`AutonomyBinding` 持久保存 master/external 开关、唯一有效 owner、controller_epoch 和
revision。有效 owner 只有 off、legacy、semantic：总开关关闭始终 off；显式关闭
外部控制器时使用 legacy；开启时由 semantic 接纳新机会。Jev 健康只控制观测退避，
不切换 proposer 或阻断无来源苏醒；缺凭据时没有新观测，仍可使用真实活动与已有反馈。
暂时评分失败、合法 unknown 和群聊安静不是同一种状态。

已停用的Space不再读取需要在线身份的群运行配置；Host沿原binding同步off。停用期间不调用观测或主模型，已接纳的Work和迟到效果仍按原ID及回执收尾。重新启用后读取原群配置，不为停用状态另建执行或关闭链。

原 `AutonomousGroupService` 保留本地机会评分，经同一 selector 和 `accept_legacy`
登记自主工作；不再把最新真人的 profile/权限交给 `ChatService.respond()` 冒充发起者。
两种 proposer 共用同一接纳合同，相同 proposal 重放返回原 run。已有 queued/running Work
不封住真实新提议，执行容量沿原 Work 调度处理；等待和暂停继续保留原恢复身份。

尚未建立互动时，单纯名字提及仍是优先观察线索，不等同于 `@`。
Jev 判断为邀请/续聊且回应楼层留给 Yuki 时，即使无法归入已有讨论，
Controller 可采用 Host 提供并核验的该作者新讨论单元。真人邀请和已有互动的续聊进入
原 `USER_MESSAGE` 主入口；非直接输入的语义邀请、普通续接与开放群聊的新机会共用
原 worker 的去抖窗口，窗口为零时直接尝试。邀请保留自己的 reason 和原 binding，
显式 @ 等原直接入口保持即时。普通续接不创建自主 Work，也不逐条等待 Jev 评分。
尚未开始的机会合并到较新来源；已开始的工作不会仅因后到消息被淘汰，后到输入沿原入口处理。
原 Work/wait 的获准输入先于新机会分类尝试，逐条发布，不经过此去抖窗口；
部分匹配保持原 wait，不开第二个普通 Main。语义开启时，开放群和需提及群的隐式新 Main
统一核当前语义资格，开放群不绕开已经收起的讨论关系；语义关闭时沿原 require_mention 策略。
原输入 publisher、显式入口和已接纳工作不因此增加 veto。
非请求式加入和无来源参与按[连续概率模型](autonomous-participation-model.md)
决定时机；Host 继续核验来源、真实停聊边界、代际、权限和发送链。
观察统一使用 3 秒去抖、12 秒最短调用间隔、20 秒最长去抖等待，删除活跃时加密档。
待办替换保留最早待评时间；最长等待不包含在途请求、失败退避或其他单元排队，首次观察不预等完整调用间隔。
待评 conversation 在源事件后 75 秒到期，已完成观测在源事件后 90 秒内具备候选资格；支持最多到源事件后 150 秒，
且评分完成后最多再保留 75 秒。这是来源新鲜度边界，不是整个会话的统一有效期。

接纳在数据库内 CAS 核对 owner/epoch 和 proposal 身份；同一 proposal 重放返回原 run。
Host 不因某来源曾被另一提议考虑过而拒绝新提议；来源按内部 ID 和 revision 核对实际版本。
接纳后的原 run 可在模式切换后继续查询和接收反馈，
controller_epoch 不是已接纳 Work 的执行授权版本。
普通真人入场使用 `ordinary_turn_admissions` 冻结原事件、actor/Presence、激活及单元来源，
与自主 proposal 分开；自主开关关闭不拦截原确定性入口。语义开关开启时，获准群中的真实
邀请和唯一持续互动也不借 SELF owner 取得普通回复资格。接纳记录不表示 Main 已完成，
不能用它恢复整轮普通闲聊。Input 只表明真人入场，不证明 Yuki 已发言；快速续接仍需
原单元的真实表达或局部 `join/stay` 意愿，并同时保有未被后续语义否定的关系依据。
自身意愿与语义关系不能互相替代：更新的 quiet 收起该单元快路，其他人交流、转题或真实停止也能收起原借用关系；
stay 或自身发送不能推翻纠正。仅入场后 `NO_REPLY` 不自证持续互动。
运行中普通 Main 明确接纳的补充与原事件共用 activation；原排队 attempt 通过同一接纳记录
退出，不按观察水位、最大事件号或发送正文消费其他机会。仅被看到的群史仍是观察。
每个补充按自己的 binding 记录真人 input；原 Main 的发送和自报不广播给同 activation
的其他来源。原 Work/wait 和显式调用保持各自的即时接纳与交接入口；自动语义邀请
先向原 Work/wait 提供输入，未接纳时才进入同一普通 worker，不另建邀请队列。
单元保留建立时的原来源 basis，新的 input_ref 与活动时间独立更新，不把每轮输入累积为
后续入场的全部祖先依赖。每次真人 admission、效果关联和自报仍保留本次原来源；
快速续接既核验建立来源，也核验当前输入，不能靠稳定 basis 放过已修订或撤回的最新输入。

原始观测记录的裁剪不等于结束持续交流。已核验单元可保留有限来源指纹、明确的退休证明
及一条原真实发送回执，用于验证原人物与讨论；不保留整段正文，也不扩大通用来源有效期。
指纹本身不能补出参与关系。来源撤销或重新解释否定原单元时，派生关系退出；未知评分不新建或抹掉关系，也不伪造用户停止。
晚到自报不重建它，真实晚到回执仍按原身份入账。资源压力优先清理旧派生关系，新的真人
输入仍可接纳。旧读取器折叠后缺少必要证明时保持未知，不猜回持续互动。

`intrinsic` 是无 event/memory 来源的独立提议类型，不伪造 Jev 观测或真人消息；
已核验的本群互动、实际自主 Work 密度和回应共同决定机会率。极久沉寂时机会率
随真人活动与沉默情境持续趋近零，无真人活动证据时为零；不等待累积门槛，也不将静默转成真人来源。
宿主按 semantic owner、有效群授权、来源事实和真实停止边界接纳；
不叠加 proposal 经验到期、支持强度、全集覆盖、较新发言或等待他人评分门槛。
已接纳 run 仍按原执行身份恢复。
无来源机会由主 SELF 决定行动或沉默，不通过读取 Memory 人造新观察。

### 普通快路与后台复核

同作者与已有有效讨论仍可及时续聊，Main 不等待 Jev，也没有续聊跨度或次数到限后等待评分的门槛。
真实新输入沿原观察队列稀疏复核：能唯一归入同一讨论/对象的待办合并为最新焦点，
原上下文继续提供交流过程；独立邀请和归属不明来源分别保留，不按全群或同名覆盖。
已取出的在途 Snapshot 不改写，后到输入成为下一份合并待办；失败也不恢复已被替换的逐消息积压。
admission 已存在不代表输入已经获得语义确认。观察结果立即更新原单元关系、回应与停止事实，
再由原 admission/consumed 防止同一输入二次激活 Main。

只有真实新输入提供观察材料；自身发送、stay、quiet、保存或重启不单独购买判断。
较新 quiet 清理其原讨论中已经覆盖的旧续聊复核，不删除原在途 Snapshot 或后到的新邀请。
unknown、缺维度和观察失败不新建或抹掉已有关系。来源、语义纠正和自身 quiet 各自保有收起作用。
旧 checkpoint 一次性清退辅助计数与队列旧结构，保留单元、hint、真实 review、来源和顺序；
废参数在读取时清退，不因废字段丢弃整份有效控制器或配置。

迟到反馈关联原来源、单元与顺序，仍可否定借用的旧关系，但不能覆盖较新已确认邀请。
尚未选择的队列和新 activation 在原 promotion 边界重新核对当前资格；不要求正文 revision 改变才能退出过时快路。
同一 Main 已选择的补充保留选择时的参与资格，后到 K、quiet 或语义纠正不再否决其发布，
也不因此中断原 Main。原来源、身份、授权和唯一接纳继续核验，不增加重试或重新装配链。
已接纳 Main、累计预算与真实效果不因纠正倒退，不撤回、重跑或补发已确认回复。

## SELF 主体与同一执行链

人物归并经宿主准备的当前`actor_identity`关系参与独立库比较；库不读取Host数据库或平台账号，
也不保存另一份人物所有权。原ScopedEvent、unit、source revision、review来源和已付观察
不改写。关系只作用于同Conversation/generation及真实讨论；quiet/stay沿原hint/source次序判定，
迟到发送或旧stay不盖过较新quiet。语义纠正按原source和review顺序裁决，
不按根人物重建Main接纳键。准备时批读当前人物关系，
writer沿同一会话复核真实身份，不缓存永久许可或把SELF放入人物桶。

持久 `AcceptedInitiative` 是待派发事实，以 `initiative:<run_id>` 唯一关联原 Work。
接纳后进程退出时，宿主重新核对原 Work；存在则接回，不重新创建任务、重置预算或重放发送。
WorkScheduler 调用 `generate_self_initiative`，随后仍经过 `MainAgentTurnService`、
`AgentRunner`、`MainAgentBackend` 和既有工具执行器。

- `TurnOrigin.SELF_INITIATIVE` 的值为 `self_initiative`；`ToolActor` 和 `TurnAuthority`
  明确携带 SELF principal、initiative run 和 canonical 场景，不携带真人 user/person。
- `ConversationTurnSnapshot` 的内部事件 ID 与 initiative run 严格二选一。SELF 不制造
  `InboundMessage`，不使用假 QQ 消息、不借最近发言者或目标人物的权限。
- 每次请求、工具执行和发送准备前校验原 run、Conversation generation、Space、Presence
  及其原群入口。发送使用接纳时原 Presence；主动发送路由切换不替换执行主体。
- 控制器切换本身不取消已接纳工作；reset/generation 失效、原场景权限或 Presence 失效仍
  拒绝继续执行。终态迟到的真实效果可以入账，不能把终态改回运行或触发重新发送。

工作区、终端、沙箱与子 Agent 复用原执行 ID、租约、回执和根预算。子 Agent 仍由既有
SubagentScheduler 和 Runner 运行，身份保持 SELF，内部结果交给父 Work；Social 仍按其原来源和真实授权执行，
不因委派增加发送权限。它不继承主 SELF 的“发送正文或沉默”反馈，也不代报参与意愿。没有另起一套工作执行循环。

SELF 使用共同 Social 目标解析、通讯录、历史、发送、结构化 @、戳人和原发送撤回。
默认当前群仍使用原场景与 Presence；显式目标沿共同路由和真实授权核验。
Memory 按需查询当前群与该群可见的 SELF 范围，不自动预取或从最后发言者生成个人上下文。
完整工具声明仍固定提供；工具是否可执行由上述可信来源和现有权限检查决定。

SELF 当前指令只保简短意图与真实来源编号，不重复附来源正文包；详情沿已有历史和记忆
工具按需读取。Host 与固定语义依赖的 DTO 不按摘要、字符串或来源数量经验帽截断。
Jev 保留 Host 已提供的快照，实际请求容量错误沿原 Provider 失败处理。

## 交付、回执与缓存

主 SELF 只有显式 `send` 才能产生可见回复。模型最终正文是内部结果，
按共同完成合同核验，不购买未发送纠正请求。调度器不自动转发最终文字。
`NO_REPLY` 可以合法完成原 Work；`return_to_caller` 不生成面向某个真人的固定收尾通知。
实际发送未知时保留 uncertain，不能据此重发或声称已发送。

自主轮默认静默处理具体工作：按需核对已有 Work、待办、获准历史与记忆和工作区，跟进承诺、
未完成事项、重要人物已表达的关切及事件进展，整理资料或推进研究、创作。发现与后续线索按需
保存在原 Work 或工作区，可委派独立子任务；未来另开具体目标用 SELF 自动化，原 Work 等信号
用持久等待。没有新变化但仍需跟进时，保存必要进展后登记 `wait`，而非直接返回 `NO_REPLY`；
无其他结束决定的最终正文会结算当前 Work。SELF 自主工作及后台维护按原指令安静处理，不因阶段进展额外通报。
没有需要继续的工作时安静结束，不因醒来或群里安静而开话题、打招呼、追问
或找人聊天。已有任务需要反馈、约定提醒到期或重要变化确实需要对方知道或决定时，才按需
显式发送。旧消息仍是历史，不变成新的请求。此行为指引不改变控制器机会函数、Host 接纳
与发送授权；人物、主题及外部变化的持续观察能力仍取决于实际等待与事件接入合同。

社会效果依据真实 SocialOperation 回执归属 run；单次 Work 不因分条或多次发送
增加自主调用密度。确认公开发送使该 Work 产生一份等待回应的暴露；
分条及文件说明仍归并到原逻辑发送回执。
确认发送的内部事件 ID 关联原 run 或真人入场记录冻结的讨论单元；平台实际群目标仍为
Space，不能为语义关联改成某个人。普通发送通过原回执和入场来源对账，没有原绑定就
保持未知，不按后来变化的当前讨论猜回。已有唯一互动的无引用输入可以续接普通 Main，
不把快捷匹配冒充 Jev observation 或 observed 概率；原焦点仍可并行进入语义复核。
工具效果、模型请求消耗、取消、暂停与无回复分开反馈。Host 确认活跃 run 的新进展后，
只将本次新增反馈交给控制器；反馈序列和效果 ID 防止重入重复累计。
保留型 `suspended`/`waiting_user` Work 不终结 run：反馈保持 running，原 run 仍可经管理 resume
由原 SELF 来源继续；参与控制器不自行重跑。只有真实终态 Work（completed/failed/cancelled）结束 run，
已终结的 run 不被 resume 复活。

后台只对账活跃 run，不循环扫描终态历史为短期刹车补账。
每页在显式读快照批读原 Work、Social/MemoryTool 回执、child 计量和全部已提交反馈；
无变化时不申请 writer。新增反馈仍按原 64 effects/页原子提交，每 run 单轮页数有界，
仍活跃的 run 可在下一轮继续；已经终态的剩余短期投影允许丢弃，原领域回执与累计预算保留。
准备快照升级竞争结束本次对账，后续活跃任务巡回沿原 run/sequence 重读
数据库计划，不重跑原模型或发送；每巡回分页工作量不等于失败重试次数上限。
Host 提交后按原 scope/generation 应用新增反馈并保存控制器。无变化时不重放、不另存快照；
冷加载不补齐旧刹车，允许领域提交与控制器保存之间丢失短期调节信号。真实任务状态、
执行回执和累计预算仍在原账本，不据此重跑任务或补发消息。近期确认的发送沿现有 Social
近窗读取按原 run 接入，即使原 run 已终态；不用 run 的最后修改时间猜测发送是否发生。
控制器已折叠时间边界之前、且不再保留的效果直接退出，不重新插入再裁剪。
普通入场及 boundary 来源按当前核验阶段批读，跨外部等待重新核验，不缓存授权。
冷加载先为本页未知入场来源建立版本，再批量核验，保持原逐事件 projection 顺序。
尚未识别且没有保留依赖的来源暂为 unknown，不授权、不提前失效；已知来源缺证明仍失效。

工具证据沿实际内部事件、initiative 或已保存执行来源，并按 run、execution、Provider、
工具名和实际调用身份去重；无真人事件的定时 SELF 不制造事件或主动轮。SELF 的调用身份以原 journal chain/请求序号限定 Provider
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
`quiet` 收起自己的参与，不制造用户停止边界，不广播关闭全群，也不为补确认请求 Jev。
无 hint 不清空原参与，迟到旧响应不能覆盖新意愿，保存失败不重跑或重发。
Host 在内部事件保留原账本 row.id 作为 source_order，普通自报沿 source 保存原事件引用；同时间输入仍按原来源顺序裁决。
不以 event_id 字符串大小或响应到达时间推断新旧，缺顺序证明保持未知；这些字段不伪装成真人发言。
可见发送和最终正文会剥离该控制尾段；格式错误不靠额外模型请求修复。
自报不能作为其他人的语义证据，也不允许工作者冒充主 Yuki 自报。
普通轮允许空正文、仅状态尾段或内部 `NO_REPLY` 安静结束；它们不发送文字，也不触发
未发送正文的补答。普通入场记录不是 Main journal，进程退出后不自动补跑未完成普通轮。

已有真人焦点的 Jev Snapshot 附带当时合法的局部 engage 和可选 SELF 整体意愿，
分别限定讨论/对象与 scope；歧义 unit_options 各自映射，不靠同作者或 quiet 强选单元。
请求明确说明这是 Yuki 自身意愿，不是真人停止、邀请或授权；相对时间沿原报告时间，不伪造历史发言。
状态仅在原请求取出时冻结，在途报告变化不改写请求、拒绝旧结果或新开评分。
缺报告保持未知，旧 Snapshot 缺字段仍可读；mood 没有消费者，已从新报告退出，旧报告窄读取保留 engage 与原身份。
quiet/closed 单元继续保留查询事实，不单独新增观察待办。

Main 在原本需要的正常请求边界，从原入场或 Work 场景绑定读取相关 SelfReport 与
UnitSemanticReview 的最小事实，随 `<runtime_context>` 尾部追加，不注入全群最后一份判断。
没有正常请求时不额外唤醒；精确已付续接不插入，已接纳输入、任务和发送能力保持。
相关判断更新不改原请求、公共历史或缓存前缀，也不建立另一份持久刹车状态。

Jev 固定问句使用 `{{name}}`，实际新请求沿当前 CharacterIdentity 展开，不固定构造时旧名。
固定协议标签仍保留，原聊天名称与在途 Snapshot 不追溯替换。确认、致谢与纠正按当前交流
判断是否还需回应，不一律续聊或一律停止。

固定系统合同的【发言】指导说到点上自然收束，保留换行分条，任务仍必须及时报告真实进展、阻塞与结果。
这是模型表达指导，未新增发送次数配额或中途停止工作的执行条件；仓库示例不代表生产挂载提示已加载。

## 配置与验证方式

两种 proposer 均使用持久 Work 调度。WorkScheduler 始终启动，既有已接纳工作和 SELF
工作必须继续恢复；`RUNTIME_WORK_ENABLED` 只控制普通聊天新工作接纳，不是恢复停机开关。
SubagentScheduler 同样持续接管已接纳的子任务；子任务接纳开关不取消原父子执行关系。
Jev 密钥不得写入公开配置、提示词或验收记录。

开放群聊来源机会率由 `source_interval_seconds` 调节；参数含义与挂载覆盖见[自主参与模型](autonomous-participation-model.md#在线调参)。

| 配置 | 默认值 / 含义 |
| --- | --- |
| `conversation.auto_reply_enabled` / `CONVERSATION_AUTO_REPLY_ENABLED` | 默认 true；只控制新 group_open、语义邀请/续聊及 conversation SELF/legacy 机会 |
| `conversation.proactive_wakeup_enabled` / `CONVERSATION_PROACTIVE_WAKEUP_ENABLED` | 默认 true；只控制新 intrinsic SELF 机会 |
| `conversation.autonomous_enabled` / `CONVERSATION_AUTONOMOUS_ENABLED` | 保留 SELF 总开关及群 Space 的 enabled/autonomous_enabled；普通真人接话独立 |
| `conversation.semantic_participation_enabled` / `CONVERSATION_SEMANTIC_PARTICIPATION_ENABLED` | 默认 false；开启使用 semantic，显式关闭使用 legacy；观测故障不自动回退 |
| `SEMANTIC_PARTICIPATION_API_KEY` | 默认空；Jev 观测凭据；未配置时不产生新语义观测，不改变 proposer 归属 |
| `SEMANTIC_PARTICIPATION_MODEL` | 默认 `jev-1.13.0` |
| `SEMANTIC_PARTICIPATION_STATE_PATH` | 默认 `data/participation.sqlite3`，控制器快照；不代替 Host 的 run/Work/回执数据库 |

四个 conversation 配置支持现有全局/群作用域管理接口。两个产品开关都是 HOT，
unset 沿 group→global→Settings 继承。设置页区分本地 row 与继承值：新建群覆盖用版本 0，
修改/删除已有覆盖用该 row.version；没有本地 row 时不可 unset，false 保留其实际值。
canonical inspect_configs 沿传入 session 定键读取 global 与指定 owner，空 keys 不扫描全部配置，
停用 owner 的控制元数据仍可读取，存在性与真实执行授权分别核验。

两个产品开关不改 binding epoch，不取消已接纳 Work，不限制原 wait/input/repair、明确 @/引用/
前缀/命令、定时工作或既有工具。新 ordinary 与 SELF 的接纳 writer 读取当前产品键；
prior/payload 对账先于新接纳 predicate。SELF 仍受原 master/semantic/Space/Presence 约束。
Host 在原推进边界刷新 scene/runtime，按 auto_reply 允许 conversation，按 proactive_wakeup
允许 intrinsic，先排除候选种类再计算机会率。
关闭期间继续推进原 last_sample_at，不累计可补采的时间，不删除原反馈或改概率公式。
尚未接纳的 pending 沿原 discard 返回值及原 proposal/run 查询处理，已接纳身份继续对账。

参与库的确切版本以 `pyproject.toml`、`uv.lock` 与实际安装一致的修订为准。
早期接入与当时 pin 见 [#268 实施记录](../operations/issue-268-implementation-20261013.md)，不作为当前依赖声明。
当前没有名为 `shadow` 的运行配置开关；
关闭 semantic 表示恢复 legacy，**不等于影子 Jev 仍在后台评分**。影子对照应使用隔离的
事件重放和观测流程，不提交 proposal、不调用 QQ 发送，不能把它描述成现网已启用模式。

## 迁移与验收边界

迁移链从 `0064` 接到 `0066`，后者为 autonomy selector/run/
反馈，`0067` 为 SELF 工具来源与自省回执水位，`0068` 为接入时解析的内部引用事件，
`0069` 将 Social 回执关联至内部事件，`0070` 持久化自主触发类型与讨论 thread。
没有修改已发布迁移来加入新行为；
当前升级清退辅助 `autonomy_source_claims`，保原 run、来源和反馈回执。
新增表默认不会开启 semantic，也不会恢复被用户关闭的自主总开关。

旧控制器读取在严格校验前清退 seed 正文、未接纳候选及其派生游标/claim，不清空整个控制器，
不把旧 seed 搬入后续 Jev context。旧已接纳 recall/contact run 及普通 binding 保留原执行身份与来源，
Memory 依赖只按明确 fact ID 核验 active、verified、有效时间、原群/可见 SELF 范围及至少一条当前可读证据；
没有候选扫描、重新提议或新种子执行入口。原 Work 密度、真实发送、计算与回应回执仍可按原 run 对账，预算不重置。

本地定向验证覆盖接纳并发与重复、模式切换、代际失效、原 Work/沙箱/子任务恢复、当前群
发送权限、独立自省回执水位。真实 Provider 序列化对照覆盖 Responses、Chat Completions
及 Responses 原生工具声明：24 次请求后原 Work 第 25 次续跑，普通续跑前缀逐项保留。
这能证明请求合同，不是线上缓存命中率；未取得真实缓存指标时保持未知。

有限真实 Jev HTTP 验证使用合成样本，只检验协议、投影、延迟和分歧；期望由开发者编写，
不是独立人工标注准确率，不能宣称改善了群聊。详细证据见独立库的
[实现记录](https://github.com/Institute-of-Agent-Life/Yuki-Semantic-Participation/blob/main/docs/implementation.md)。
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

Jev/Host 的诊断沿[执行过程查看合同](execution-trace.md)保存新增的真实
观察和接纳尝试，默认保留 30 天。该诊断不参与语义 apply、权限、proposal 或恢复；
记录失败不会丢弃成功观察、再调用 Jev 或改变原重试/退避。完整概率与来源正文需
独立 `control.execution.content.read`，元数据分页不加载正文；旧判定不猜测回填。
当前控制器状态快照仍是有界即时状态，不充当全生命周期统计。

控制器快照仍使用独立库的 revision CAS 和容量约束。Host 用一个专属线程创建、读写及
关闭快照 SQLite 连接；事件循环只维护控制器，提交时冻结 JSON，不把可变状态交给线程。
快照等待写锁不会阻塞普通聊天及健康接口；保存仍等待真实提交结果，不把快照当成可丢弃
的诊断写入。保存期间取消时先对账实际 revision，再传播取消；缓存加载、淘汰及保存
串行核验，避免新增 await 后产生重复控制器或用旧 revision 覆盖快照。

控制器按真实会话与 generation 保留，不再以固定 32 个缓存槽拒绝其他会话的机会。旧 generation 在持有者退出后按既有流程保存并清退；并发推进仍使用现有 semaphore。

新事件的讨论候选直接取有效历史，SELF 锚点优先，各自按原时间倒序保留；候选排序不计算全量参与状态。
同一内部来源及 revision 已有投影时复用原 ScopedEvent，既有候选键和在途 Jev Snapshot 不重建。
参与查询只返回来源、表达、意愿及纠偏进度，不附带无人读取的 belief 概率投影。
自主机会评分及历史折叠仍使用真实 belief；同一次同步计算只解析一次参与检查点并按讨论/对象归集事实，
这些局部数据不跨状态修改保留，不增加快照缓存、调用阈值或状态清空流程。
