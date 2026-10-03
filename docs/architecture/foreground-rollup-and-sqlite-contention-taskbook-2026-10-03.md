# 前台回复、后台历史整理与 SQLite 写锁修复任务书

日期：2026-10-03。源码核查基线：`3d9e2733991fe00f7774ed83c2c4acf9eaafdb01`（PR #228 合并后）。

状态：设计与源码核查；本文不表示运行代码已修改、测试通过或上线。本轮只编制任务书，未修改业务源码、生产配置或服务。事故与并发复现的证据日期均单独标注。

## 1. 目标与最小方案

完整请求能够装入实际可用容量时，前台先执行原回复，聊天事件的 Rollup 在后台进行。后台完成后发布可用摘要，在下一次明确的上下文整理边界采用；不替换正在执行的请求或工具续接前缀。私有观察线索保持原来源与整理机制，不因此交给公共 Rollup。

同时修复已复现的 Rollup 提交竞态，消除已确认的周期空写和重复读取占用 writer，把准备工作移到首次写入前，并补齐能够解释等待的诊断。优先级为：Rollup 正确性 → 前台不等待软整理 → 空维护与重复读取 → 锁内准备 → 计时与观测。首两项是同一修复的正确性和体验两面。

最小充分改动使用现有唯一 `YukiRuntime`、Rollup signal/job/worker、覆盖点、冻结投影、来源版本、原 Work journal 和回执。不新增压缩运行时、第二套任务调度器、永久“摘要已通知”状态或强制进度发送器。分批是资源调度粒度，不是“必须压三批”的业务条件。

实施前阅读 [共同开发约束](development-contract.md)、[Conversation Rollup](conversation-rollup.md)、[主执行合同](main-agent-runtime.md)、[Memory](memory-v2.md)、[Self Reflection](self-reflection.md)、[执行诊断](execution-trace.md)、[插件架构](../plugin-development/architecture.md)及具体模块文档。此前 [Work 上下文任务书](work-context-and-chat-continuation-taskbook.md)的历史/线索/原件分工继续使用；其中与本次软整理等待规则冲突的说明在实施时直接改写。

## 2. 已查明的事实及证据边界

### 2.1 普通回复确实被三批压缩挡住

2026-10-03 台北 08:39 的内部事件 `81659`，主执行轮 `4e9caaa18dcf4b10b224a3926276d032` 没有接纳 Work。主 Agent 开始前连续完成三批前台历史压缩，模型耗时合计约 318 秒；主请求约 15 秒后才首次发送回复。

三批各覆盖 116 / 112 / 114 个旧事件，总计 342 个，来源没有跨批重复。每批把前一份摘要与下一段完整事件合成新摘要，全部成功，并非模型格式错误导致的重试。摘要最终覆盖至 `81115`，本次提问不在三批来源中。它们不包含完整工具研究原文或 Work 私有协议尾部。

没有保存当时最初的完整请求 fit 判断，所以不能断言这次三批全部可以省略。能确定的是：前台等待发生了，而且现行代码仍有软目标同步等待路径。后续同一激活的自然续接请求报告 cache hit 44295 / input 47502，约 93.25%；这不是全日缓存改善证明。三个摘要请求的缓存字段缺失，应记为未知。

### 2.2 源码缺口

以下行号为上述基线，实施后更新定位。

| 编号 | 已确认缺口 | 生产代码位置 | 修复方向 |
| --- | --- | --- | --- |
| F01 | 超软预算而仍在实际容量内时，仍调用并等待 `ensure_required_coverage` | `services/context_assembler.py:1803–1920`，尤其 1846、1877 | fit 请求立即返回准备结果；软整理交现有后台链 |
| F02 | 初始原文只按较小整理预算读取，`raw_complete=False` 被当作必须压缩 | `conversation/rollup/repository.py:332–382,629–646,1128`；`services/context_assembler.py:1689` | 首次预取小页；实际请求需要时在一致快照按容量继续读取，不能假装完整或仅因预取不足等待模型 |
| F03 | 冻结投影/观察摘要仍按软准备预算回收或同步摘要，hard fit 是事后回退 | `services/chat.py:472–497`；`services/main_agent_turns.py:209–288`；`services/history_projection.py:220–390` | 全装配链统一区分安排整理与必需容量；原合法输入能 fit 时不先请求辅助摘要 |
| F04 | Rollup 提交读来源后、首次 DML 前没有一致读快照或完整最终 CAS | `conversation/rollup/repository.py:805–835,1364–1535` | 显式 SQLite 读快照准备并升级，517 时有限重备纯数据库计划 |
| F05 | Memory activation backfill 空插入仍抢 writer，发现候选在写 SQL 内扫描/排序 | `memory/maintenance.py:116`；`memory/repository.py:323` | 只读有界发现；空页返回；主键候选短写复核 |
| F06 | Memory 治理恢复空队列仍执行两条 UPDATE，积压恢复未分页 | `memory/reflection/worker.py:95`；`memory/reflection/repository.py:184–199` | 有界只读候选、写时重核原领取身份和期限 |
| F07 | Social 已有相同回执仍 INSERT ON CONFLICT；插件正常只读上下文仍 BEGIN IMMEDIATE | `social/repository.py:79`、`social/service.py:1301`；`plugin_host/notification_repository.py:1038–1049` | 精确已有对象只读返回；只有真实取消/新建/领取时写 |
| F08 | 重复 observation、prepared snapshot、Work 输入、canonical 入账及已被领取的 Rollup 会占 writer 后才判空 | `conversation/observations.py:104–149`；`conversation/projections.py:322–341`；`runtime/work_repository.py:862–939,964–990`；`identity/canonical_uow.py:112–126,303–347`；`conversation/rollup/repository.py:699,989–998` | 锁外准确命中返回；写路径保持条件写/唯一约束与当前复核 |
| F09 | Identity 编码、Dream DTO/聚合、观察父引用准备部分在 writer 内 | `identity/canonical_uow.py:191`；`memory/dream/repository.py:894,1078`；`conversation/observations.py:311–324,401–446` | 纯准备前移，一致依赖与原子发布保留 |
| F10 | 只测写 SQL 整段耗时；没有独立 commit/rollback 和完整事务持有观测 | `persistence/sqlite_diagnostics.py:23–134` | 明确计时边界、不可分离项和可见持有者范围 |

F04 已用隔离 WAL SQLite、真实 Repository/UoW 在“来源读完、首次写入前”插入竞争复现：并发新增消息后实际未覆盖 4 条却保存为 3；原事件视觉来源 revision 改变后，旧摘要仍成功提交。原消息没有丢失。既有测试只覆盖更早的并发窗口，不足以排除此竞态。

F05 隔离执行实际编译 SQL/现有索引，发现 `SCAN memory_facts` 和临时排序；影响零行的 INSERT 仍阻止另一连接取得 writer。此事实不等于所有事实表扫描都必须新增索引：查询和候选推进方式需要一起改变。

2026-10-03 台北 10:57–12:57 的有界线上只读样本：一次 `execution_trace_entries` 写 SQL 耗时 2.166 秒，没有锁超时告警。主机同时存在 I/O 和内存压力。该样本不能确定阻塞连接，不能将慢写归因为上述任一路径。

## 3. 前台与后台的设计

### 3.1 两种预算只表达两种用途

- 软触发/目标用于安排后台整理。保持现有配置入口，默认整理基准 90000 token，0.90 / 0.60 对应约 81000 / 54000；这些数字是可调整政策。
- 实际输入容量取当前 Profile、输入限制、联合窗口扣除输出预留以及显式活动窗口的交集。524288 是本次部署的配置余量，不代表任意模型保证能接纳该值。
- fit 检查包含固定 instructions、全部 tools/native tools、所选摘要、完整历史、当前资料、媒体及协议负担；复用 `model_runtime/capacity.py` 与 Runner 估算/预检。缓存命中的 token 也占上下文。
- 准备余量只能安排整理和预留资源，不能冒充真实容量拒绝。超大当前必需输入仍按真实容量核验，不能为不可达软目标反复整理。

### 3.2 主处理路径

1. 按可信内部事件、Conversation/generation 和当前读取范围取得一致历史视图。
2. 优先复用仍合法的冻结前缀，加当前新消息与必要资料；确认摘要覆盖与原文连续，当前触发只出现一次。
3. 初次读取没读完时，按内部 ID 分页补齐到同一视图上界，边读取边计算完整分组请求成本。页面大小限制单次读取，不限制历史的业务寿命。
4. 完整请求 fit：继续原主请求；软水位超标只安排/唤醒现有后台 Rollup，不 await 摘要完成，不先生成“请稍等”的额外模型轮。
5. 完整请求真实超限：优先采用已经完成且通过复核的摘要，重新装配并检查。仍装不下时，才进入现有必要覆盖等待；没有合法候选时按原明确失败/应急合同处理。
6. 如果还没读全就已证明整份请求无法 fit，停止扩大读取，走必要整理；不能把后缀标记为完整。分页的单条代价估计不能替代最终分组/完整请求检查，不能因保守逐条累加而误判所有原文必定超限。

只扩大本次需要的读取，不在启动时把全库加载进内存，也不依靠一次大 LIMIT 或全库 COUNT。摘要、来源版本、覆盖点及 raw 上界必须来自一致快照；后台提交、编辑、撤回或 reset 期间的拼接继续受来源复核约束。

当前 `load_prompt_snapshot` 返回时事务已经关闭；软预取后需要增读时，首选重新开启一个真实读快照并在其中分页完整读取，不能拼接两个已经关闭快照的页面。前台临时读取预算作为显式调用参数，不修改后台软政策或共享当前 scope。assembler 的局部预算决定读取规划，完整 Main 装配与 Runner 的最终预检才决定 fit；包含 4096 在内的准备余量不得被当作新的硬上限。

### 3.3 后台与前台资源

复用 `signal_canonical_rollup_if_needed` → `drain_rollup_signals` → `ConversationRollupWorker`。已有 signal/job 去重、租约、heartbeat、批次与失败退避继续使用；不建另一张队列表。

压缩模型/结构输出校验均在写事务外，结果短事务提交。后台可以连续处理多批，并在批间释放资源；不规定恰好三批，也不把目标未达视为主任务失败。后台失败不会推翻能 fit 的前台输入。

后台调用使用现有维护优先级与模型容量调度，验证前台预留没有被占满。已有上游请求是否可抢占按现有能力处理；“不等摘要”不等于承诺消除所有 Provider 排队、网络或数据库等待。不为本次另加抢占控制状态。

此处后台 Rollup 整理聊天事件，不因此获得私有 observation 的覆盖能力。观察线索优先复用 `ContextObservationRepository.prepared_summary`；没有就绪摘要而原请求仍 fit 时保留线索继续，不同步新购摘要，也不为本次增加独立观察摘要 worker。真正超限时沿原纯整理与 scope-summary 发布边界处理；公共事件覆盖不能替代私有观察引用的覆盖证明。

### 3.4 摘要完成与采用是两个动作

后台提交新的语义摘要只改变可用派生投影；不改已派发请求、冻结片段或原 journal。模型结果返回也不自动触发新的主请求、群消息或 Work 恢复。

采用时机：下一次正常主激活准备输入，已经达到整理条件且存在可用完整摘要时，在派发前一次性采用；如果后台尚未完成而旧输入能 fit，仍先回复。当前激活的普通工具往返继续追加原前缀；真实超限才在原完整配对安全点整理。业务恢复可在合法新装配边界采用当前公共视图；未知效果、未决 opaque、child 或 Provider pause 等精确协议恢复保留原合同，不能强行改接摘要。

采用后的公共视图：`固定指令与工具 + 已选摘要 + 覆盖点之后的完整聊天/已选线索 + 当前资料与消息`。若摘要覆盖到 E120，而模型整理期间新增 E121…E150，则这些事件按内部账本顺序保留在尾部；无需再次把工具研究全文塞进历史。

采用需要完整的一致性选择与原派发 CAS；若来源真实失效，沿原失效路径处理。普通追加不等同来源修改。不要先把摘要覆盖点推进到新值、随后仍拿旧 raw 尾部装配。

前缀验收规则：当前激活期间后台提交不会改变其固定前缀；下一次采用摘要产生一次有原因的输入变化，之后继续稳定追加。不承诺摘要切换零缓存失效，也不把缓存追求变成无限保留旧原文。

### 3.5 关键场景

| 场景 | 前台行为 | 后台/后续行为 |
| --- | --- | --- |
| 低于软水位的普通聊天 | 旧稳定前缀追加新消息 | 不因轮数触发压缩 |
| 超软水位但完整请求 fit | 原请求立即继续 | 独立整理；完成不改本轮 |
| 小预取没读完，但实际容量够 | 一致分页补齐后回复 | 可以整理，前台不等模型 |
| 后台压缩期间群友继续聊天、没有触发 Yuki | 只入账，不强制唤醒 | 已有合法整理可继续；后续正常激活补齐增量 |
| 已有新摘要，下一普通轮达到整理条件 | 采用一次，经复核的摘要＋全部后续消息 | 不再沿用过大的旧视图；不重复采用相同候选 |
| W1/W2 交替、不同会话同时工作 | 同读取范围复用公共聊天；各自私有尾部隔离 | 不因任务选择回到 H0；调度沿现有 runtime |
| 原请求真实超限 | 采用就绪结果，必要时等待最小覆盖 | 共用原等待期限；同次提交的 DB 重备复用原付费候选 |
| 后台失败或有来源 hold，原请求 fit | 正常继续 | 保留失败/hold 事实，按既有策略追赶 |
| 撤回、来源编辑、reset、范围收窄 | 拒绝过期视图或结果，重新核验 | 不能当成软失败吞掉 |
| 重启 | 按需有界读取，复用合法投影 | 未接受承诺不恢复“思考”；原 Work/效果按原回执恢复 |

## 4. Rollup 提交竞态的最小修复

首选局部显式 deferred `BEGIN`：在同一 SQLite 读快照中读取来源、当前覆盖/计数、引用合法性、hold 与 lease 依赖，完成编码与计划后首次 DML 升级。已有 `load_prompt_snapshot` 和 Memory evidence 的显式快照模式可借鉴；Rollup 不依赖 Memory 领域 Repository。

成功提交必须原子发布摘要、覆盖点、准确的未覆盖计数及 job 状态；提交时来源、generation、覆盖前提、原租约身份和真实有效期均须有效。禁止把来源扫描、fingerprint 或 JSON 校验搬进 `BEGIN IMMEDIATE` 来堵竞态。

升级发生 517 时，完整回滚并有限重建纯数据库提交计划；原候选输出、模型调用计量和 execution 身份保持。并发追加且候选来源未变，可复用该候选并基于新快照正确计算计数；候选来源编辑/删除、隐私变化、reset、hold 冲突或租约失效则拒绝旧判断。后续确需新候选由正常调度决定，不能把模型重跑塞进数据库重试。

同语义/应急覆盖发布路径一起检查，防止只修普通摘要。CAS 或 517 重备不能再次减计数、覆盖已发布新摘要，或将确认丢失当作未提交。复用现有原内部身份查询结果。

当前 Rollup 模型结果仅在 worker 本次调用的局部变量中，尚无跨崩溃候选存储。本项保证同次提交的有限数据库重备不再调用模型，不承诺进程崩溃后所有未发布摘要都免重新付费；不为该保证扩展长期候选表。原 Work 已有 paid 游标与恢复合同仍保留。

领取/续租相关时间判断遵守 §7.2：领取时刻在取得 writer 后确定，有效期在 SQL 执行时核验，不能使用锁排队前的旧 now 来认证租约。只改涉及的 Rollup 路径，不扩展成全库租约重构。

## 5. 少占 writer，而非取消必要原子边界

### 5.1 周期维护

Memory activation backfill：先按主键取有界源窗口，仅在这个窗口内按必要列判缺失；空候选页直接返回。内存游标按最后扫描的主键推进，即使修补零行也推进；每轮固定来源 high-water，扫到后回绕，后来新增和状态变化在下一轮发现。避免每五分钟重复从表头扫描及排序全事实。原 active-first 属于启发式顺序；首选改为页内优先并明确行为变化，若业务确需全局优先再证明可索引候选分类的必要性。证明轮转/游标最终回访及新增、状态变化不会永久饿死；游标是可重建调度位置，不增加永久业务表。

写时仅重核该页 fact 的当前状态、初始化来源及 activation 是否仍缺失，用唯一约束处理竞争；不得锁外授权后直接写。每页提交释放 writer。真实缺失记录的恢复口径保持，不用 LIMIT 伪装全量修复完成；一次周期工作有界，后续周期继续推进。

源窗口大小和每周期扫描量沿可配置维护政策设置，不增加写死的业务门槛。扫描窗口有界后不再保证“每周期必定修复原 batch 数量的缺失项”；完成量可能为零，推进与发现延迟单独报告，不能以低修复数假定维护失败。

Memory 治理恢复：读取到期 run 的有界候选页，按实际 `status/claimed_at/id` 查询验证索引；写时重核原 status、claim 身份、租约/期限与 attempts/max-attempts 政策。现有 `commit_job_claims` 仅有 status/updated-at 条件，不能直接套用而漏掉恢复依赖。空队列不发 UPDATE；有积压分页推进，不以分页重置预算或增加重试次数。

同仓库 enqueue 的 fingerprint 已全部存在时也只读返回，缺失候选保留原唯一约束。治理 `discover` 的事实自连接与排序目前在只读边界，但最终 LIMIT 不证明扫描有界；本轮记录为读取成本风险，不将它算成锁内扫描，也不顺便重构重复对发现算法。若后续指标指向该路径，再单独设计可延续来源窗口，不能截断重复对后宣称完整发现。

### 5.2 精确重复与只读返回

Social 已有回执：按原 source-turn/call 身份定位，并核对 payload hash、分条计划和原目标；完全一致时只读返回。内容冲突保持原拒绝，发送前的当前 route/身份围栏仍在真实执行位置。新对象继续使用唯一约束及短写，不用正文相同跨执行去重。

插件 `load_background_context`：正常读取不领取 writer。确需取消失效 job 时，在短写事务重核 job、订阅及来源版本后条件取消；读出的 context 仍在原执行/效果边界复核，不能靠一次读取授权后续发送。

重复 observation、projection、Work input 和有效 processing Rollup：只读检查确实无变更的结果；新关联/选取提交仍需写。返回依赖当前来源的路径使用原版本/归属 guard；没有锁外精确命中时进入原条件写，不靠先查后写取消唯一约束。避免为每个函数加同一套多层包装。

Canonical 接入重复回执也用显式只读快照核对原 receipt、keeper、Conversation、primary alias 与兼容性，保留 ingest fence。平台凭据只在原接入去重边界使用，不能改成平台 ID 裸返回或用它重建内部事件。未命中时原 writer 重查并原子入账。正式 projection selection 与 journal 发布不能因 payload 相同就跳过；Work 显式 resume、input 从 ready 到 staged/consumed 是真实变更，不算空写。

### 5.3 锁内准备前移

- Identity segments 编码、content fingerprint 和不依赖新增数据库 ID 的 DTO 在首次 DML/IMMEDIATE 前完成；event、canonical 关联、计数及原入账原子边界保持。
- Dream run/cluster、preview 的 JSON 前移。恢复 cluster 页在显式读快照准备完整 operation 聚合和 run 状态，再短写复核/发布；继续使用原 operation ID 和完整计数，不重放 mutation。
- Observation 先在一致快照准备父引用闭包和编码结果，再用现有 source/privacy revision 及引用依赖复核，复用 `conversation/projections.py:343–361` 已有锁外展开方式。现有查询读取的是引用元数据，不应把它误写成“全历史正文扫描”；必要的主键、归属和版本复核保留。

不扩大到全库格式或性能重构；不移动强制审计、预算、journal 或效果回执到可丢诊断队列。已经符合边界的文件 hash/fsync、模型/网络调用、关系 evidence 预备与提交后文件 GC 直接保留。

## 6. 诊断：分别说明等待、执行和持有

扩展现有 `install_sqlite_diagnostics`，沿物理连接记录短生命周期状态。commit/rollback 必须在真实 DBAPI 完成后清除持有者；失败仍保持原释放/失效清理语义。日志只记录阶段、操作类别、有限来源关联及耗时，不记录参数、正文、凭据；不以高基数 ID 作为 metrics label。

必须记录与标明边界：连接/调度等待（可观测时）、已有 `BEGIN IMMEDIATE` 的获取尝试、写 SQL 的整段耗时、真实 commit/rollback、事务持有观测区间及失败类别。首次 deferred DML 的忙等待与 SQL 执行混在同一个 DBAPI 调用中，现有钩子无法精确分开；应报告 `first_write_elapsed`、计时精度/未知字段，不能编造精确锁排队或用相减冒充事实。

对于现有 explicit IMMEDIATE，可独立报告 acquire elapsed 与之后的 SQL，但 acquire elapsed 仍包含驱动/线程调度。首次 deferred DML 完成之后记录的 held 时间是保守可观测下界，不包含该 SQL 内部已经取得锁后的执行部分。应据实际可提供的采样记录范围或明确不完整，不为测量给全部事务提前加 IMMEDIATE。

持有者列表只覆盖已安装钩子的 engine/进程，不能排除其他连接、I/O、checkpoint 或线程调度。诊断不递归写同一个 SQLite，采样/聚合有界；可丢诊断过载仍报告缺样，不阻塞业务。

`DiagnosticWriter` 可另外记录入队至消费开始、消费提交的耗时，沿用固定分桶与 dropped/failures；这属于诊断队列，不叫 SQLite writer 排队。物理连接上 savepoint/嵌套事务继承外层持有者，回滚 savepoint 不清外层；覆盖外层 RELEASE 的真实结束。同步/异步执行线程的 tracker 访问采用短同步保护，取消、失败和清理不改变原数据库错误或效果结果。

主输入准备另保存内容无关的判断摘要：原准备成本、实际容量、raw 是否完整、是否软触发、等待/采用原因、所选摘要 revision/coverage、实际耗时。复用现有诊断链，不加入模型提示词，也不充当恢复事实。

## 7. 复用、局部重构与删除

| 分类 | 具体处理 |
| --- | --- |
| 直接复用 | 唯一 YukiRuntime、Rollup signal/job/worker/heartbeat、原 protected suffix/hold、完整事件 renderer、共享容量估算、冻结投影/派发 CAS、Work paid 进度/回执、现有后台模型容量调度 |
| 局部重构 | 预取与实际装配的读取预算；assembler/投影的 fit 分支；Rollup 局部纯数据库重备；上述维护候选与短写；现有诊断钩子 |
| 删除替代 | 超软水位仍等待必需覆盖的分支；仅因小预取不完整就要求模型压缩的规则；能 fit 却先同步摘要再回退的重复流程；空维护与精确重复的无变化 DML |
| 保留 | 来源、租约、generation、privacy、owner 复核；计量、发送及 mutation 原子性；未知效果和 opaque 精确恢复；真实资源预算与必要等待 |

实现时同步改写 `conversation-rollup.md` 关于预取不完整、软 fit 和冻结投影回收的冲突描述；删除该文已经过时的“普通工具续接尚无整理能力”说明。主合同/任务书不得同时保留两套相反流程。说明写在开发合同和实现中，不扩充人格/权限提示词。

## 8. 验收矩阵

常驻测试使用隔离 SQLite、真实 Repository/Runner 和受控 fake Provider/transport；真实付费测试只手动运行。

| 验收 | 必须证明的行为 | 优先扩展的现有测试 |
| --- | --- | --- |
| V01 超软但 fit | 阻塞后台摘要的 fake Provider 时，主请求仍已派发并可回复；断言未发起前台辅助摘要、不等摘要、不制造 Work | `test_rollup_soft_window.py`, `test_history_soft_coverage.py`, `test_chat_context_preparation_gate.py` |
| V02 小预取与重启 | 超预取而低于真实容量时补齐全部事件，触发一次；多页无丢失/重复；超容量走必要等待 | `test_rollup_complete_sources.py`, `test_projection_capacity_io.py` |
| V03 实际容量 | schema/native tools、输出预留、媒体和 opaque 成本加入后真正超限仍挡住；配置上调不伪造 Profile 容量 | 容量及 Runner 既有测试 |
| V04 后台交错 | 压缩中追加消息；提交前后都有新消息；当前激活 wire 前缀不变；下次采用无覆盖洞/重叠 | `test_rollup_chat_wakeup_wire.py`, `test_history_time_projection.py`, `test_work_effective_rollup_source.py` |
| V05 工作与多会话 | W1/W2 公共视图稳定、私有尾部隔离；两 Conversation 不串来源；ordinary/SELF/plugin 原入口均不等软摘要 | `test_main_agent_entrypoints.py`, Work/activation 既有测试 |
| V06 拒绝与精确恢复 | hold、来源编辑/撤回、reset、读取范围变化拒绝过期候选；unknown/opaque 保持原检查点、不重发 | `test_work_context_rollup_wait.py`, observation/source guard 既有测试 |
| V07 提交竞态 | 来源读完后首次 DML 前真实第二连接追加/编辑；准确计数，编辑旧候选不发布；lease/hold/reset 竞争不越界 | Rollup Repository 既有测试，加真实 WAL barrier 回归 |
| V08 纯 DB 重备 | 同次提交调用内 517 完整回滚；追加可复用付费候选；模型/工具只执行一次；提交确认丢失查原结果；重试有限，不声称跨崩溃候选免付费 | Rollup 与 snapshot 既有测试 |
| V09 空维护/重复 | 持有别的 writer 时，空队列、精确已有回执、插件只读、重复 observation/projection/input 能读取返回；SQL 轨迹无 DML/IMMEDIATE | `test_memory_maintenance_boundaries.py`, `test_storage_writer_boundaries.py`, Social/plugin 测试 |
| V10 候选竞争与公平 | 发现后删除/状态变更/新 claimant 不被旧计划覆盖；多个页正确推进，无表头反复扫描和尾部饿死 | Memory/reflection/索引 query-plan 测试 |
| V11 首写边界 | 编码、引用准备、Dream 聚合在首次写入前；取消/来源变化完整回滚；隐私与强制审计原子性不拆 | `test_dream_plan_transactions.py`, `test_memory_maintenance_evidence_snapshot.py`, `test_context_observation_sources.py` |
| V12 诊断真实性 | real SQLite 两连接竞争、慢 commit/rollback、提交失败、savepoint、取消/失效和 pool exit；诊断队列等待不算 SQL/持锁；未知字段明确 | `test_sqlite_diagnostics.py` |
| V13 缓存与文本 | 后台提交不改变本轮规范化 messages/input/tools/固定请求配置；摘要采用只有明确一次边界；无重复权限文案 | wire/投影既有测试 |

并发回归使用事件/barrier 定位竞争窗口，不依赖随机 sleep 或永久硬性能阈值。开发阶段只运行直接相关验证；最终按改动范围运行格式、类型、相关集成及 PR CI 所需全量，避免机械重复未变化检查。

### 手动 Gemini / DeepSeek 验证

使用同一个真实 Provider/Profile/协议与同一累积 session，多轮聊天堆过软水位；控制 messages/input、工具顺序、reasoning、output 和 schema 配置，记录冷首轮、稳定追加、后台多批交错、采用一次摘要及采用后重新追加。

Gemini 多做相同场景以观察后台整理与前台首请求是否解耦；DeepSeek 分别用真实 DeepSeek provider 的 Chat 与 Responses（只有实际支持时），协议组内保持同一 session，不能跨组比较冷暖请求。复用此前手动脚本思想，不将真实调用纳入 CI/常驻测试，不向真实 QQ 发探测消息，不用循环保温。

每轮报告实际 input/cache hit/cache miss/output、协议/provider、摘要覆盖变化、前台准备/首模型/首送达耗时。缓存率按有实测字段的 `sum(hit) / sum(input)` 计算，冷首轮和后续热轮分别列；缺字段为未知并报告覆盖率，不能填零或借代理汇总推断。比较相同 fixture/轮数/累计来源的修复前后运行，不把偶然高命中作为固定门槛。

## 9. 实施顺序与完成条件

1. 先用最小真实 WAL 回归锁定 F04，修正确快照和有限 DB 重备，补齐应急/租约同类边界。
2. 联动修 F01–F03，复用后台处理，明确摘要采用规则；用阻塞摘要与完整多页来源验收“先回复”。
3. 修 F05–F08，先周期空写，再精确重复；验证候选页/query plan 和当前状态复核。
4. 修 F09 与 F10；不做全库大改，不加无依据超时或另一套锁调度器。
5. 对照全部验收项、现行文档与真实 wire，再运行手动 Provider 比较，逐项记录通过/失败/未知。

开发可按 Rollup/装配、Memory/Social/Dream、诊断拆分独立文件所有权，另做接口与最终审查；不要并行改同一 Repository 或任务书。可合并实施，不强制三阶段上线或新增永久迁移；只有查询计划和缺失索引证明必要时才增加对应迁移。

本轮任务书交付到设计和核查为止。后续实施沿已有提交、PR、合并、Bot-only 上线流程；部署时重新查实际 Compose、schema、镜像、备份与健康，不沿用历史配置当成当前事实。代码回退保留上线后的事件、预算和回执，不默认恢复旧数据库；不触碰其他服务。

完成报告分开列本地实现、验证、提交、推送、CI、合并、部署及自然聊天效果。线上等待原因、缓存计量缺失、负载差异或真实能力尚未验收时如实保留，不能因为辅助测试通过就宣布“所有延迟已解决”。用户继续承担真实群聊能力验收。
