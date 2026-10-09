# 语义参与与持续接话重构任务书

状态：2026-10-04，双仓库合并与 Bot 上线完成，真实 QQ 能力验收由用户进行。Host 本地实现已完成，库 PR #13、#14、#15 已合并，Host 已固定库修订 `68bf033c37a524a2d377d2f67eaa25ce779c9e90`；Host PR #230 完整 CI、合并与上线均已完成。本文保留设计及验收边界，当前证据见[交付记录](../operations/semantic-continuation-2026-10-03.md)。

## 1. 目标与边界

当一个人想和 Yuki 聊天时，Yuki 能接上，并自然地聊下去；明确告别时先回应，再收起这段参与。Jev 负责发现入口、处理歧义和矫正关系，不负责每轮决定是否允许 Yuki 接话。主 Agent 自己决定说什么、是否沉默、是否继续参与。

持续接话以当前讨论和交流对象为范围，不设全群统一的 `engaged` 布尔值。另一人插话、另一个话题出现、短暂沉默，不自动清除原互动；同一个人发言也不保证全部都在对 Yuki 说。感谢、简短确认和明确告别应结合上下文理解，不用关键词表决定进退。

原始观测记录清理不应误断已核验的持续交流。复用有限来源指纹与原真实表达回执，并仅在合法裁剪时记录单元退休证明；不延长所有原文的有效期。重新解释为 unknown、来源修订或撤销时撤掉派生关系，晚到意愿不能重建它；真实回执继续按原身份记账。旧 reader 缺少证明时保持未知。验收覆盖连续输入超过 600 秒、840 秒空档、裁剪后重启、quiet 和来源修改。

本任务必须同时修改：

- [Yuki-Semantic-Participation](https://github.com/YuanYeYouTao/Yuki-Semantic-Participation)：参与状态、纯查询、观察排队、反馈协议、快照兼容与独立测试。
- Yuki Host：真实消息入场、普通轮反馈、发送与讨论关联、调度、固定依赖、联合测试和文档。

复用唯一 `YukiRuntime`、主 TurnService、Runner、固定工具合同及既有 Work 调度。不开第二个聊天 runtime、长驻闲聊 Work 或新的 SessionManager。不做 Work 跨 session 迁移，不重构无来源自主机会公式，也不增加每轮质量判断模型。

共同约束以 [development-contract.md](development-contract.md) 为准，模块边界参见 [语义接入](semantic-participation.md)、[自主模型](autonomous-participation-model.md)、[主 Agent](main-agent-runtime.md) 和 [canonical runtime](canonical-runtime.md)。库侧先读其 `AGENTS.md`、`docs/protocol.md`、`docs/implementation.md` 与 `docs/autonomous-evolution.md`。

时间尺度、观察节奏、召回排序、参与强度属于可调策略。不能把本轮示例的秒数、消息条数或概率写成永久业务门槛。真实请求容量、队列资源限制和来源有效性仍须核验；资源限制要有配置、依据和明确缺样说明。

## 2. 设计核查基线与问题（历史）

Host 核查基线为本地提交 `95aba212cd4aa6ab0ea111126f155dfec4bded03`；这是未推送的本地清理提交，不代表生产版本。其 `pyproject.toml`、`uv.lock` 与已安装包固定库修订 `b9c7cc71f8dbaf9bc4b45419e0270a08bf48fe9a`。库本地检出 `ccf03db6a77c8e0adddfea5d264b470b34e7bf86` 比该固定修订旧七个提交，不能直接作为实现起点。实施前重新确认双方远端基线，从包含 Host 固定修订及后续有效改动的版本隔离开发。

以下缺口与文件位置记录设计核查时的 `b9c7cc71`，不是当前实现状态。当前版本与验证以交付记录及现行模块文档为准。

| 已核查事实 | 代码位置 | 本任务处理 |
| --- | --- | --- |
| 普通群消息先记入语义上下文；未命中直接触发时在 Work 输入匹配前返回 `group_observed` | Host `services/processor.py`：823、856、997 | 在该早退前补普通续接判定，保留媒体准备、协调器和真实用户入口 |
| `_hydrate` 对真人先 `observation.observe`，之后才标记直接消息 consumed；库 `observe` 同时入账、排队和预测 | Host `services/semantic_participation.py`：639–646；库 `session.py` 的 `observe` | 拆开“已看到事件”和“需要 Jev 评分” |
| 队列只合并同一事件的修订；不同焦点仍可逐个评分，并非一次请求判断整个批次 | 库 `scheduling.py`：36 起 | 不把去抖或 context 误称为多焦点评分；减少无须评分的焦点 |
| `predict_continuation` 只覆盖引用原真人候选、同作者/单元、已有支持的窄路径，而且仍排 Jev；生成的仍是自主候选 | 库 `controller.py`：668 起 | 用普通续接查询替换生产调用；不是在旧预测之上再加一套控制器 |
| 状态已有按 `(thread, target)` 的 O/H/Y/E/C、停止边界、事件、反馈、出站锚点和持久快照 | 库 `controller.py` 的 State、belief；Host `services/participation_snapshot.py` | 复用，不能把当前架构描述成“完全没保存接话状态” |
| 普通 Main 的自报被 `initiative_run_id` 等条件直接排除；无当前 Work 时只写 session progress 不能保证保存 | Host `services/main_agent_backend.py`：787 起；`runtime/work_session.py` 保存边界 | 普通轮直接反馈给参与服务；不为保存意愿创建 Work |
| `join/stay/quiet` 目前是 scope 最近一份 SELF 意愿：`stay=0`，会覆盖之前的 join；不表示维持某话题 | 库 `controller.py`：1342、1376 起 | 明确区分局部持续参与与旧自主倾向，不能悄悄改全局公式 |
| 普通发送记为 `host:<run>`，没有 semantic proposal；群投递的 `actual_targets` 是 group，个人 target 也不会据此形成自身单元效果 | Host `services/participation_feedback.py`：44 起；库 `controller.py`：735 起 | 在真实回执上关联讨论，不伪造 proposal 或个人投递 |
| 无引用焦点的单元候选依赖近期 SELF 锚点；第三个较旧话题可被挤出，再经最近 context 裁剪失去关联 | Host `services/semantic_participation.py` 的 `_event`；库 `scheduling.py` 的 `take` | 按活跃讨论、真实引用与证据相关性选锚，不能机械扩大最近 N 条 |
| 真实 `extend_yuki`/楼层交给 Yuki 的及时路径已有密度豁免；错归为 open_group 才会被自主 Work 压力严重抑制 | 库 `controller.py` 的 `_addressed`、来源机会率 | 已建立讨论的普通续接不再支付“新自主 Work”启动成本 |

既有离线复现包含：多条 Work 脉冲显著压低开放群聊来源率，但不压低真实 addressed 路径；旧 SELF 锚点被两段新话题及插话挤出后，无引用续接失败，真实引用仍能保住锚点。这些证明具体路径，不证明线上每一次不回复都由它们造成。

## 3. 主干设计

```text
真实群事件 → canonical 账本/媒体索引 → 原参与状态记录事件
                                      ↓
                             只读参与视图/续接候选
                              ↙                 ↘
             已有唯一、仍有效的互动                 未建立或有实质歧义
                       ↓                           ↓
            原普通用户入场/Work 输入匹配        原稀疏 Jev 队列与协议
                       ↓                           ↓
                同一主 Agent              语义结果修正状态/发现机会
                       ↓                      ↙           ↘
                真发送或安静结束        真人邀请/续聊      自发参与
                       ↓                  普通入场        SELF 接纳
             局部意愿 + 真实发送回执 ←──── 同一主执行链

无来源自主唤醒 → 原 selector/SELF 接纳 → 原 Work 调度 → 同一主执行链
```

### 3.1 记录事件、查询参与、请求观察分别执行

库提供同步纯查询，例如 `participation_view(event, now)`。名字是接口建议，实现时可合并到合适的现有类型。它返回来源引用、候选讨论/对象、真实锚点、局部意愿、有效边界、证据种类与歧义；不调用 Provider，不排队，不采样、不 consume，也不创建 proposal。

保留 `observe_committed_event`，将 ObservationSession 的自动排队改成明确的 `request_observation(ref, kind)`：仅排入已记录且版本一致的来源。库不导入 Host，也不决定 Main 的入场权限。Host 可以据返回视图选择普通入场、只保留上下文或请求纠正。

判断成本来自已有关系和事实，不再加一个逐轮分类模型。真实引用可以准确定位；已有唯一互动的同对象无引用输入可以成为续接候选。单凭同作者或距离很近不等于新的确定语义证据。主 Agent 看当前原文和群史，仍可选择不回复；候选误接后通过局部状态和必要的 Jev 矫正退出，不给猜测制造 observed 概率。

查询明确分开“已建立的旧单元”与“当前未评事件匹配出的候选”。现有 `resolved_unit` 对没有 observation、但 Host fallback 标为非歧义的新单元也可能返回结果，不能直接当成当前语义已确认。匹配旧单元不改当前事件的未评状态，不写 observation/resolution，不补 observed 支持。

### 3.2 普通续接复用真实用户入口

在 Processor 已入账、已取得内部 ID、但尚未 `group_observed` 返回处判定续接。命中的原消息进入 `USER_MESSAGE`，可记录 `group_continuation` 原因；不改作者、不伪造 @ 或 SELF initiative。协调器须把原 observation token 正式提升为前台激活，并保留当前保护/版本检查；不能只绕过 early return，就让仍是观察属性的 token 执行发信。复用后续限流、语音/视觉准备、Memory 入队规则、`stage_work_input` 与 `handle_turn`。

原 Work 等待输入的匹配优先使用已有规则：确实是给原任务的新输入时进入原 Work，保留其 ID、journal、预算和回执；普通接话不复活已结束任务，也不能自动完成或取消任务。无任务聊天不因接话而被强制 accept。

最终入口统一：Jev 确认的真实真人邀请/续聊也走 `USER_MESSAGE`；open_group 中 Yuki 自己决定加入、recall/contact/intrinsic 等才是 SELF 自发参与。库区分语义机会类别，Host 唯一接纳，不让同一个焦点同时启动普通轮和 SELF 轮。这样同一真人请求不会因为有无 @ 而换成两种执行主体或工具权限。

异步 Jev 结果通过显式的“已入账事件提升”接口接到相同普通入场函数，按可信内部 ID 读取原事件，复用原媒体/来源准备；不重跑整个 Processor 接入去重和副作用，也不按平台消息号或最新正文拼假入站消息。复核原事件的当前版本/可读性、真实人物、原 Presence 与当前场景；并发接纳按下述真实入场事实去重，不能把可丢执行 trace 当接纳台账。已 consumed 的语义结果只纠正状态，不启动第二次回答。

过渡提交可以短期保留初次 SELF 路径，但不作为最终合同或联合完成状态。已经接纳的历史 SELF Work 不改来源，不搬预算，仍按原合同恢复；删除旧路径只针对新入场。

#### 真人入场去重的最小事实

现有 `processed_events` 在观察消息时就已登记，只证明接入去重；ChatEvent 存在也不证明 Main 接纳。原 Work/input 只覆盖确实交给任务的分支，普通无 Work 的安静结束可能没有它们。`autonomy_source_claims.run_id` 是非空 initiative 外键，且依赖自主 binding；不能直接塞普通来源或制造假 SELF run。

因此允许在 Bot DB 增加一份小型真人入场记录，名称随领域约定：按 conversation/generation+原内部事件唯一，冻结来源 revision、真实 actor/Presence、所选单元依据和原激活身份；只记录 ordinary 或原 Work/input 的关联，不复制聊天正文、预算或模型日志。direct、快速续接、异步 Jev 提升共用这个入口。source 修订使旧判断失效，不能为了再答一次换去重键。

准备、限流及当前容量检查在事务外；仅在实际可接纳时短事务复核来源并插入。busy 或预测不写 consumed。给原 Work 的记录与其 input 同事务发布；ordinary 在首次 Main 请求前提交，库的 consumed 是这个已提交事实的派生结果，跨库失败时重核原入场记录。此处是防重复激活的真实事实，不是另一套参与状态机。

入场记录归属原事件与代次；只保存最小身份/关联，沿既有隐私清理及事件生命周期处理，不积累另一份无限聊天档案。协调器 version 不是事件身份：直接 A 受保护期间 B/C 观察可能共享 version，后续提升仍逐一绑定原事件。Rollup deferred 只刷新合法 coordinator version，保留原 actor/event/generation/unit，不重跑接入或换成最新 trigger。

普通无 Work 仍没有持久 Main journal。上述记录只证明接纳，不证明完成；接纳后进程退出，不能由 Jev 自动补跑整个 Main 或补发。已发生/未知效果仍查原回执，用户的新请求使用新的真实事件。此次保留原普通聊天的恢复能力边界，不增加 ordinary outbox/长驻 Work，也不声称保证这个崩溃窗口自动回复；若以后要求此保证，需另设计最小持久交接。

### 3.3 开关与已有执行

复用现有 semantic 开关控制语义入口和持续接话，自主总开关只控制新的自发行动。群启用、来源可读性和现有执行边界仍独立生效。当前观察装配被 `master && external` 一起控制的代码必须随之拆开，不能仅改文档。

| 自主总开关 | Semantic | 显式 @ 等直接入口 | no@ 已有互动 / Jev 真人邀请 | 新自发行动 |
| --- | --- | --- | --- | --- |
| off | off | 原普通入口 | 不启用语义候选 | 无 |
| off | on | 原普通入口 | 原普通入口 | 无 |
| on | off | 原普通入口 | 不启用语义候选 | 原 legacy 策略 |
| on | on | 原普通入口 | 原普通入口 | semantic 策略 |

SELF selector 的 off 不再被解释成“真人不能和 Yuki 聊天”；不通过 fallback 反启自发行为。Jev 故障只推迟新的不确定解释，不改 proposer 归属；仍有效的局部互动可按已知事实续接。切换开关不取消已接纳 Work、预算或未知效果。

### 3.4 局部参与状态与 Main 反馈

进入参与：真实邀请、Host 正式接纳的用户输入或已参与话题中的真实发送，可以建立或加强相应单元；发送尝试、工具产物和模型说“我发了”不能替代真实发送。

维持参与：相关真人输入、确认的实际回答和 `stay` 保持该单元的参与倾向，随时间和后续互动自然演化。`stay` 不是每轮必须输出的保活字段；没报状态不清空参与。不能把每次群消息当成续命，也不把一次沉默视为全群结束。

退出参与：Main 在告别轮先自然回应，可附现有 `<yuki-state>{"engage":"quiet"}</yuki-state>`；Host 将它绑定本轮单元，收起该单元的继续参与意愿。感谢、嗯、好的等不自动 quiet。`quiet` 表达 Yuki 自己的选择，不制造“用户要求全群停止”的边界；`join` 也不能自行解除真实停止边界。新真实邀请按原来源和范围重开。

Jev 的 closing 可能先于或晚于该轮 Main 返回。已建立互动的本次真实告别仍可完成一次前台处理，由 Main 自然告别；用户明确要求不回复时也可沉默。边界阻止后续新参与，不能在入口吞掉这个尚未处理的原结束焦点，也不能把这次收尾当成重新邀请。

普通轮与主 SELF 都可以报告自身意愿；worker 不能报告主 Yuki 状态。保持尾段可选，不新增每轮必填字段，不额外请求模型补报；无效尾段只剥离、忽略并作有限诊断。

普通 Main 优先沿现有空最终正文的沉默通路结束。若内部使用保留结果 `NO_REPLY`，在同一输出边界归一为沉默，不作为发送文字，也不进入“非空正文未发送”的补答反馈；不能为了这个可选表达另调用模型。移除尾段后再判断正文，只有状态尾段的普通轮也可合法安静结束。

Host 绑定 scope/generation、原入站事件或真实 run、主请求序号/response identity 与已解析的单元，并冻结 Main 本次实际看到的单元映射；响应不能按稍后变化的“当前讨论”重新归属。模型不填写任意 Person/thread ID。单元不唯一时不把一个 quiet 广播给所有话题；没有可靠局部绑定时不更新局部意愿。主 SELF 原来的 scope 倾向与普通局部意愿分开使用，升级不得把普通 quiet 静默写成全局自主关闭。

用原激活/请求顺序及现有有效性核验处理迟到自报，不能按回传时的最新时间让已被抢占旧 Main 的 quiet 覆盖更新意愿。已经确认的旧发送仍按原 effect/来源对账，不因意愿过期被抹掉。

自身意愿是可重建/可丢的派生状态，用现有 State 快照保存即可。保存失败不会将已确认发送改为失败，不重跑 Main、不重发、不恢复预算。若进程在保存前退出，可能丢失最后一份意愿，恢复后重新观察；不新建 canonical 意愿审计表。

### 3.5 真实发送与讨论单元分离表达

继续按原 SocialOperation/逻辑表达 ID 去重，分条和文件附言按已有真实父操作归并。平台实际目标仍是 Space/group；语义关联另指本轮讨论及对象，不能为让 `_belief_actions` 命中而篡改 `actual_targets`。

库须接纳没有 semantic proposal 的真实普通回复关联，优先在既有 effect/出站锚点结构上表达。发送证明“Yuki 在这个单元说过话”，不证明某个人已收到、赞同或回应；真人反馈仍按其原始事件与可靠关系形成。群消息不提高所有成员的互惠 E。未知效果只保留未知；迟到的真实回执可对账，不重新执行或复活终态。

普通无发送轮也允许收起自身意愿，因此反馈接口不能要求先存在成功发送才接受可信主轮自报。它接受 Host 已验证的原主轮绑定，不能通过构造一个虚假 initiative/Feedback 来绕过库现在的 `run_ref` 检查。

## 4. Jev 的职责与输入输出

### 4.1 何时调用

Jev 用于未建立关系的语义入口、多个讨论/对象的实质歧义、需要重新判断的关系变化、原来源的停止/重开解释，以及原有记忆种子等观察。已建立、来源仍有效且关系唯一的普通续接不因每条新消息自动排 Jev。

直接 @ 等现有确定性入口不等待 Jev 才回应。输入已处理只防第二次入场，不等于语义已经评完。必须保住现有顺序：合法纠正先更新 reception/停止边界，再检查 consumed 是否禁止新候选。旧的未决停止或邀请焦点不能被最新消息覆盖，更不能因它当过 context 而被标成已评分。

省掉常规评分后，纠正的触发者必须明确：已有 pending/in-flight 的解释依赖照常处理；无法从局部状态确定对象/范围的原事件进入 Jev。普通 Main 的有效局部 quiet 导致该单元从参与转为收起时，Host 先更新自己的意愿，再对本轮绑定的真实真人焦点选择性请求一次 Jev 纠正；不伪造用户 stop，结果仍可能只是 unknown 或 acknowledge。普通短答/常规 stay 不排这次纠正；没有真人来源的 SELF quiet 不能制造焦点；重复回传同一响应或已有同版本解释不重复请求。

该设计复用 `SelfDelta` 和原队列，不另加逐轮停止分类器。缺 hint 或无效 hint 时只知道本轮没有可靠的新自身状态，不能宣称“用户已要求停止且 boundary 已落实”；模型正常阅读原文仍应遵从用户。验收必须分别展示成功闭环与无报告的证据缺口，不把关键状态未更新的表现写成已完成。必要的再次明确邀请也通过其真实来源和边界解释重开，不能靠普通 join 解禁。

纠正需求跟随原焦点版本，不能反复对同一不变材料评分。新鲜度、退避、公平性与容量丢弃仍分别诊断。

保留原单 scope 单 in-flight、跨 scope 公平性、健康退避和请求序号。当前 `active=bool(candidates)` 不能继续冒充“正在聊天”；观察节奏可读取实际参与视图，作为可替换策略。先删自动逐事件排队，不另建全群轮询判断器或固定时间的校正模型。

### 4.2 协议样例

当前 Jev 原生请求是 `model/state/questions`，返回 `answers/usage`。`state` 只给必要原文、作者角色、局部中性引用、候选单元、相对时间和省略说明；canonical ID、generation、revision 和请求序号由 Host/原 Snapshot 保存核验。

例：Yuki 先说“前缀缓存需要尽量保持请求开头一致”，一个尚未绑定该讨论的人问“那如果换一台机器呢？”；Host 提供新单元和真实 SELF 锚点两个选项。这时有归属歧义，可以请求 Jev：

```json
{
  "model": "jev-1.13.0",
  "state": {
    "focus": {"id":"e2","author":"person-A","kind":"human","thread":"t2","target":"person-A","text":"那如果换一台机器呢？","seconds_from_focus":0},
    "context": [{"id":"e1","author":"SELF","kind":"self","thread":"t1","target":"group","text":"前缀缓存需要尽量保持请求开头一致。","seconds_from_focus":-10}],
    "focus_age_seconds": 2,
    "omitted_context": 0,
    "unit_options": [
      {"key":"new","thread":"t2","target":"person-A","label":"本条消息的新讨论"},
      {"key":"u1","thread":"t1","target":"person-A","label":"前缀缓存需要尽量保持请求开头一致。","self_anchor":"e1"}
    ]
  },
  "questions": "这里省略；正式请求由库 rubric 生成 choice 问题"
}
```

上面是阅读用缩写，不能直接发送。完整可解析请求与模拟响应见 [协议样例](examples/jev-continuation-protocol-example.json)，包含每一维完整分布。模拟结果中 `interaction_mark=extend_yuki`、`floor_state=yuki`、`unit_selection=u1`；其概率是合成测试值，不是真实 Jev 判断或准确率。库验证类型、全部选项分布、唯一赢家和原候选映射后应用，unknown 保持 unknown。

一旦 person-A 与该单元建立互动，下一句“那旧缓存怎么办？”可以由参与视图进入普通 Main，**这一轮没有 Jev 输入或输出**。Main 的可选局部 `stay`、真实发送与原事件更新参与，不能给这轮补一份假的 Jev 概率。

当前请求 context schema 最多六条，默认请求字节预算为 16,000；这些是核查到的现行限制，不是本设计永久标准。锚点选择与 schema/请求容量须一起检查，按需要调整可配置预算，保留原焦点和关键引用；不能只在 Host 多塞锚点，再让库机械裁掉。非关键上下文可整条退出，缺失明确标记，不用截断原焦点伪装完整。

## 5. 关键过程与上下文编排

| 过程 | Host/库动作 | Main 与 Jev |
| --- | --- | --- |
| 人第一次 @Yuki | 入账；确定性普通入场；建立真实单元绑定 | 同一 Main 回答，不以 Jev 成功为前提 |
| 未 @ 的人明确想加入，但尚无关系 | 原事件排稀疏观察；Jev 解释后 Host 唯一接纳 | Jev 是入口；Main 使用原来源，不重复接纳 |
| 同一讨论连续数轮无 @ | 记录原事件；查询已有参与；普通入场；反馈真实回答 | 常规续接无额外 Jev，不另建 SELF Work |
| 多人插话/旧话题锚点不在最近两条 | 保住按活动单元召回的真实锚点，保留插话时间线；实质歧义才观察 | 不因最近消息覆盖关系，也不把所有群友当当前对象 |
| “谢谢”后继续问 | 原讨论自然保持，Main 决定短答或沉默 | 不靠词表退出 |
| 明确告别 | Main 自然告别；可选 quiet 收起本单元 | 不关闭所有讨论，也不取消其他 Work |
| 模糊的退出/要求停止后又有新消息 | 原焦点纠正保留；版本/范围验证；新消息不抹旧焦点 | consumed 不禁止停止解释，新邀请不凭自身意愿解禁 |
| Work 暂停、群友闲聊、再给原任务输入 | 普通互动与 Work 生命周期分开；原输入规则接回原 ID | 继续使用当前合法历史和原任务线索，不恢复旧整份群史 |
| 重启或热更新参数 | 原 scope 快照/CAS 恢复；来源重新核验；不补采停机机会 | 真实副作用恢复沿原回执，参与意愿按现实重新演化 |

提示词结构仍是固定 system/工具合同 → 稳定公共聊天投影及其已冻结资料 → 新的当前消息/必要运行资料 → 本轮工具配对。局部参与说明仅在有用时放在新请求后部，不逐轮输出空字段、计分公式、全群话题目录或权限复述；模型不需要看到数值状态才能自然继续。

参与查询、队列排序和 receipt 关联都在 Host/库内完成。不改变旧消息角色、文本或历史中的时间，不把内部状态写成真人发言，不因不同话题互换 system 前缀。公共群聊仍按实际已观察顺序保存；两话题工具结果不互相反复注入。Rollup、Provider 合同变化和 generation 仍使用现有明确边界。

本次删除固定 prompt 句子是一次真实前缀变更；升级按既有合同建立合法新输入，不改旧请求或旧 opaque。缓存验收区分这次边界与之后的多轮稳定追加，不能声称删句也会保住完全相同的旧前缀。

## 6. 两仓库修改与删除清单

| 仓库 | 必须修改 | 直接复用 | 删除或替换 |
| --- | --- | --- | --- |
| 独立库 | Controller 的纯参与视图、局部自身意愿/真实回复关联、显式观察请求、相关 Snapshot/schema 兼容 | O/H/Y/E/C、边界、来源版本、原 queue/health/checkpoint、SnapshotStore CAS、原 Jev rubric/分布验证 | `observe` 的自动排队与预测耦合；新普通续接覆盖后退役旧 predicted 入场路径，不能保两套相同业务判断 |
| Host | Processor 早退前续接、异步真人提升与小型入场去重事实、普通主轮反馈、真实发送到单元关联、配置投影和诊断；`pyproject.toml`/`uv.lock` 固定新库提交 | canonical 账本、协调器、媒体准备、原输入匹配、唯一 Runtime、主发送和 receipt、snapshot 专属线程 | 仅 SELF 能报告意愿的限制；为普通续接绕行自主 Work 的代码；过时文档与重复状态 |

如果新参与视图能直接由原事实得到，不新增状态。确须保存局部意愿或关联时，只扩展原 State 的有界派生字段：唯一修改者是 Host 提供的可信主轮反馈，作用域为 conversation/generation+原讨论/对象，源撤回、修订或 generation 变化时失效。生命周期和清理不能依赖不相关的最新一条聊天。

首选复用现有 `State.host_checkpoint` 的版本化 namespace 保存 Host 提供的局部意愿与表达关联，库提供 typed DTO 验证和纯查询；保留原 `outbound_threads` 等调用者，旧读者可忽略该 namespace，不需要新 SQLite 表。事件、实际 effect ID 仍在其原结构中，不复制正文或整套接纳日志。不要为迁就老的错误语义保留两套参与算法。

若实现证明 State/RecordedEffect 的新 typed 字段更简洁，可采用，但必须设计新读旧与回退读取。当前库 Record 使用 `extra="forbid"`；“新增字段默认空”只保证新读旧，不保证旧镜像能读新快照。必须验证真实旧 reader 保存/恢复新 namespace，或交付有明确退出条件的兼容读取/版本转换；不能写完新快照就声称旧镜像可无损回退。

迁移只解释可确认的派生状态，不把旧 scope-wide `stay=0` 生造为某人的 active。保留真实已接纳/待确认 proposal、队列健康、序号及效果身份。退役 predicted 候选只影响尚未接纳机会；不确定接纳先查原 Host 结果，不能换 ID 重提。真人入场记录按新增冻结 Alembic 迁移交付；除已证明缺少的该事实外，不默认给意愿或打分新建 canonical 表，不改已发布迁移。

所有外部观察、身份准备、JSON 编码与证据整理在写事务外完成。写时只重核必要来源/代次和提交短原子变更；库快照仍由同一专属线程持有连接生命周期。不在入场读查询中额外取得 writer，也不为诊断持锁或重复保存未变内容。核对 SnapshotStore 在 `BEGIN IMMEDIATE` 后进行全快照容量 SUM 的既有路径：如需调整，应保证容量复核正确，不在新增高频普通反馈后扩大锁内扫描或另建配额账本。

Bot DB 与 participation DB 没有共同原子事务。已发送以原 Social 回执为事实源；只有原本冻结的 unit/source 绑定仍能核对时，参与快照才可按原 effect ID 重放派生关联。effect ID 单独不能重建讨论归属：普通发送后、参与快照保存前崩溃而映射丢失时，保持未知/重新观察，不能按后来“当前讨论”猜回。保存失败不把发送改为失败；普通意愿丢失不补跑 Main。不能以跨库“全有或全无”的虚构保证掩盖这个边界。

## 7. 文档与 prompt 清理要求

实施代码与对应合同同一轮更新，删除相互冲突的现行表述，不靠在旧规则后追加一句“但新规则除外”。不能提前把尚未实现的设计写成现行能力。历史实测 JSON、发布回执和日期交付记录保留其真实基线；其中有旧方法时标成历史对照，不能修改证据数字或冒充新验收。

| 文件 | 清理内容 |
| --- | --- |
| Host `docs/architecture/semantic-participation.md` | 分开普通续接与 SELF 机会；修改“只有 Jev 选择锚点才续聊”“自报只用于主 SELF”等现行限制；说明来源、纠正、快照与联合版本 |
| Host `docs/architecture/autonomous-participation-model.md` | 自主来源率/Work 密度仅决定新自主行动，不作为已参与普通聊天的接话门槛；保留无来源模型的真实公式 |
| Host `docs/architecture/main-agent-runtime.md` | 真实用户 continuation 来源、普通轮局部反馈、原 Work 输入匹配及缓存后部投影；不制造第二执行链 |
| Host `docs/architecture/main-agent-runtime.md` | 普通群输入可成为前台持续候选，仍保留“被动观察不直接唤醒等待 Work”的原规则 |
| Host `docs/architecture/development-contract.md`、`canonical-runtime.md` | 只在此次改变事实边界时改对应句，不把经验参数固化成共同约束 |
| Host 架构入口、README、配置示例、管理参数 schema | 指向唯一现行合同，展示真实配置和禁用行为；区分源码默认、挂载值与生效状态 |
| Host 旧 V6/自主反馈任务书与报告 | 与新入口冲突的当前指导删去或改成清楚的历史说明，移除过时实施入口；不删除仍被使用的回执/恢复事实 |
| 库 `docs/protocol.md` | 显式事件记录/观察请求/参与查询；普通反馈单元绑定；unknown、predicted、closing、quiet 的区别 |
| 库 `docs/implementation.md`、README | 删除“尚未合并”等已经被提交记录推翻的绝对现状；能力按实际固定提交与 Host 联测记载，部署另证 |
| 库 `docs/autonomous-evolution.md`、`docs/group-chat-experiment.md` | 区分自主机会公式与普通持续接话；合成机会数不能代替 Main/JeV 实际调用数或聊天效果 |
| 双方测试说明与实施交付记录 | 标明 synthetic/mock、真实 Provider、CI、固定依赖、上线和 QQ 人工验收各自证据 |

Prompt 只保留一份短的行为说明，可采用：“正在交流时自然接话；告别后收起这段参与。可选状态尾段只描述自己的意愿。”格式说明复用现有尾段，不重复写授权要求，也不增加每轮强制输出。

本次任务书制定时已从 Host `prompting/contracts.py` 删除“表情和语音通过媒体工具发送，不用 [表情：…]、[语音：…] 等占位文字冒充发送。”整句。保留真实平台回执要求；真实 QQ face 收件投影及既有媒体发送协议不变，不声称新增独立 face 发送参数。`config/system_prompt.example.md` 没有该句，无需为凑修改重复改动。

## 8. 联合验收

测试通过真实库调用、原 Processor/协调器与发送回执形成闭环；不要只 mock 新查询的返回值再证明分支可到达。相近性质合并参数化，删除被新真实调用覆盖的重复旧测试；保留旧恢复/回执回归。固定种子的群聊回放可用于比较调用分布，不为得到满意数量写死机会配额。

| 编号 | 场景与必须核验的结果 |
| --- | --- |
| C01 | @、真实引用、已有无引用讨论分别入场；内部事件与真实 actor 不变；Work 接纳开关关闭时普通聊天仍能运行 |
| C02 | 建立讨论后多个相关无 @ 输入进入原 Main；记录实际 Jev HTTP 次数，常规续接没有额外观察；Main 可以沉默，不强制发送 |
| C03 | Jev 首次入口与快速续接并发，同一焦点只产生一个入场结果；直接 A 受保护而 B/C 共用 coordinator.version 时各自原事件不混；busy 不提前 consumed，版本变更不能沿用旧判断；被抢占旧 Main 的迟到 quiet 不覆盖更新意愿 |
| C04 | 自主开启话题后真人接话；不再被新 SELF Work 密度当作自主插话压低；原 SELF Work 的迟到回执不重复发言 |
| C05 | 多人物、多话题与插话，第三个仍活跃的旧 SELF 锚点经真实 `_event → queue.take → observer` 路径可读取；真实引用和无引用分别测试；无法唯一定位时不误绑 |
| C06 | 谢谢/短确认/明显告别：结合合成语义和 Main 模拟输出，确认只收起本单元；Jev closing 先到/后到都不吞掉未处理的原收尾轮，用户要求不回复可以沉默；quiet 不生成用户全群停止，join 不解除真实边界 |
| C07 | 无 pending 的唯一互动收到 stop → 原 Main quiet → 原焦点选择性纠正 → 后续消息/SELF：证明新停止闭环；再测缺 hint 保持未知、较旧 stop 焦点、最新继续消息及 in-flight 迟到结果。已 consumed 仍能更新合法边界；撤回/修订/新 generation 使旧解释失效，context 不能批量标已评分 |
| C08 | 普通无 Work Main 的 join/stay/quiet 生效，覆盖零发送/空正文/内部 NO_REPLY 且无 Social/effect 的 quiet：保存意愿、真实焦点纠正一次，不造 Work/initiative/Feedback；无 hint 不清空，invalid/worker hint 不补问；重放同响应/回执不双计 |
| C09 | 真实 Space 发送与个人语义目标分开；失败/未知发送不产生确认表达，多成员不被一次群发批量提高 E；原 effect 身份可恢复对账 |
| C10 | 重启、热参数更新、scope 淘汰/重新载入、旧快照升级及旧 reader 读取/保存新快照的实际回退；保持真实预算/效果；ordinary 入场后崩溃不由 Jev 重跑，deferred 保原来源/单元；旧 predicted 退役不丢待确认 proposal |
| C11 | waiting_user/暂停 Work 与普通群聊交错；匹配才追加原任务，不重置预算、不重发、不取消其他 session 工作；开关切换不取消已接纳 Work |
| C12 | 双仓库 schema/接口、Host 固定 SHA、lock、fresh install 与已安装包版本一致；测试使用固定产物而非另一个本地 src，升级与恢复路径同时覆盖 |
| C13 | 新局部状态只出现在新尾部；比较实际 messages/input、tools/native_tools 顺序、固定设置与冻结前缀。两话题交错不重写前部；deferred 仅刷新协调器版本，不换当前原消息身份；删固定 prompt 的一次合法边界单独记录 |
| C14 | 不主动写的参与查询、真实 SQLite 保存竞争/取消、错误阶段分类；Main/JeV 调用不在写事务中，诊断失败不改变真实发送结果 |
| C15 | 3.3 开关矩阵、Jev 无 key/故障/合法 unknown/本地容量拒绝分别核验；已建立且仍合法的普通互动不因观察故障被当成新自主候选，真正不确定的首次入口保持未知 |

手工真实模型实验按用户已有授权可使用 Gemini/DeepSeek，**不加入常驻 CI**。若需要比较缓存，使用同一 session 的多轮追加、相同模型/固定工具合同及相同材料；分别报告冷首轮、热续接、交错话题、重启与合法整理边界。

按 Provider 返回的 usage 汇总命中输入 token、未命中输入 token、输出 token 和缺样覆盖；命中率用已知输入的加权总量，不平均各轮百分比。记录真实 provider/Chat 或 Responses 路由、调用数和并发条件；未提供 cache 指标就是未知。哈希辅助定位，不代替报文比较。不能靠无意义热身保温，不能用某个固定命中率或伪造 token 门槛宣称社交验收通过。

成本同时统计 Jev HTTP、Main、辅助模型、输入/输出与延迟，避免只报 Jev 减少而遗漏 Main 增加。真实 QQ 的持续接话、告别、误接与主动打扰由用户验收；离线 fixture、CI 和有限 Provider 请求不替代它。

## 9. 实施顺序与交付

1. 重新确认双方基线与真实依赖；划分文件所有权，以本任务书和现行共同约束删除冲突设计。先固定事件记录/参与查询/显式观察及反馈的跨仓库协议。
2. 库侧实现最小参与视图、普通回复关联和局部意愿，升级现有快照；定向测试后形成可固定的提交。
3. Host 固定该库提交并更新 lock，接普通 ingress 与反馈、媒体/Work 匹配；保住原 pending 纠正路径，删除被替代的预测入场和重复状态。
4. 同轮更新双方合同/README/配置/历史导航；检查 prompt 文案、所有真实调用者及不存在的接口引用。新任务书不能代替现行模块文档。
5. 运行按风险选择的独立与 Host 联测、既有 CI 规定检查及固定安装产物核验；逐条回对 C01–C15，明确未做的真实模型/QQ 项目。
6. 实施完成后分别记录本地提交、推送、双方 PR/CI、库合并 SHA、Host pin/lock、Host 合并和实际部署版本。上线按届时授权的老流程执行，不能只合一边就称联合交付；保留原 DB、Work、预算和回执，不默认重建数据或清理未知效果。

双方实现、固定安装产物、CI、迁移演练与实际部署已分别核验；真实 QQ 效果尚由用户验收。实际版本、逐项证据和覆盖限制见交付记录。
