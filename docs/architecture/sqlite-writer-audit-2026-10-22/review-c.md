# 第 1 轮 C 域：Runtime、模型、Social、插件与 Sandbox 写入边界

基线：`cdb49eadc0c7cd16060bf0329234ae69611e62bd`。审查工作区为
`codex/sqlite-writer-architecture-plan-20261022`。本报告只记录源码、调用链与测试阅读，
没有改运行代码、运行模型、制造锁竞争或访问生产。已有 README 与任务书改动不是本审查新增代码。

## 审查口径与覆盖边界

已读根 `AGENTS.md`、`development-contract.md`，并沿架构入口核对
`persistent-subagents.md`、`execution-trace.md`、Main/插件/持久环境相关现行合同。
不能从日期任务书反推当前实现。

范围包含 `runtime` 45、`model_runtime` 14、`execution_trace` 5、`social` 12、
`plugin_host` 34、`sandbox` 19、`services` 82，共 211 个 Python 文件、66,076 行。
全文件均进入 AST/导入/调用索引；下文逐文件区分已核事务方法、间接调用、纯结构排除。
**AST 命中不表示真的写数据库，也不表示全文人工阅读完成。** 例如 tools 的 `execute`、
文件对象的 `flush`、Model dispatch 的 `commit` 不能计作 SQLite 写点。
本轮以实际数据库事务和外部效果边界为重点，逐方法核对；大文件未核的业务分支明确交后轮复核，
不把 211 文件索引等同于 211 文件全部逐行验收。

## C01：Work 的短写与恢复是两件事

`runtime/work_repository.py:135-243`：acquire 先只读拒绝 busy/旧 generation，真正
IMMEDIATE 内使用 SQLite 当前时钟和条件 UPSERT。renew/release 仍是 `sessions()+begin()`；
它们先 `valid()` 的 SELECT 只是避免无变化写，真正 UPDATE 仍须核完整 fence、owner、
cancel_epoch、generation 和未过期条件。迁移只能收口实际 writer，不能删除 SQL 条件或把排队前时间当续租时刻。

方法级入口按实际效果迁移：

- `renew:209`、`release:232`、`accept:281`、`transition:539`、`accept_control:843`、
  `checkpoint:885`、`patch_communication:1023`、`stage:1328`、`consume:1349`、
  自管 `cancel:1363`、`prepare_effect:1920` 的裸事务应进入同一 owner。
- `accept_in_session:308`、`commit_state:636`、`cancel_in_session:1378`、
  `purge_scope:1428` 是组合提交接口，继续只用调用者 session，不能在内部排第二次 writer。
- `derive:458`、`patch_context_note:951`、`enqueue:1054`、`prepare_input:1168`、
  completion route/confirm、`discard_input:1889`、Code checkpoint/dispatch、
  `record_effect:2648` 已有 immediate 边界，接入公共准入后继续保留领域 CAS。
- `_assert_lease:267` 用写同值 fence 建立当前写资格，不应保留为第二个数据库调度器。
  是否能在已持统一 IMMEDIATE 的路径改为短条件读/将条件并入实际 DML，要按真实调用者和返回行为核对；
  不能为了少一条 SQL 丢掉同事务内的租约核验。

`runtime/lease_heartbeat.py:19-71` 的 BUSY 循环受上次确认 expiry 约束；
`supervise_lease:75-119` 在续租失效时取消原激活并等待 pulse 退出。
这不是可直接删除的任意仓库重试：统一内部 writer 后要保留真实到期监督和取消传播。
外部进程 BUSY、提交不明、物理清理未确认不能获得重放资格。

`runtime/work_session.py:955-1055` 保存同一已付响应，只有识别 sqlite_busy 且没有
rollback/invalidation 失败注记才重新发布；源 revision 变化另走原 guard。
任务书应单列检查此处阶段分类：不能因底层改动而把 commit 不明的异常送回这条循环，
也不能删除循环时使已持有模型输出重新请求。`save:921-953` 的 stage/rollback/finalize
是内存与实际提交边界配合，不是数据库锁的替代层。

## C02：Protocol、Journal 与 GC 的锁序必须按实际交错验证

`runtime/work_journal.py:313-450` 已将请求编码、对象文件写入、输入媒体引用提取放在 writer 前。
`ProtocolStore.publication:401-437` 先取得 protocol 文件锁，在线程中核验文件，再用只读连接
读取已拥有 refs，yield 已准备元数据；随后 journal 才取得 IMMEDIATE，核 lease/source/privacy，
同事务发布 projection callback、对象 refs、media refs 和 journal。

需要保留 `protocol lock -> 读准备结束 -> writer -> refs/journal commit -> 释放 protocol lock`。
读准备不能外传仍活跃的 SQLite 快照。writer 中不得反向请求 publication 或读文件。
`work_journal.record_effect:713-752` 也先 externalize/写对象/publication 再调用
`WorkRepository.record_effect`；该方法 `:2738-2779` 只发布准备好的 refs，不能再加文件锁。

`protocol_store.cleanup:570-677` 已有另一套正确的分阶段流程：

1. `_gc_lock` 下目录候选在 worker thread 发现；必要时短 writer 登记孤儿。
2. `:611-629` 短 writer CAS 标记 deleting，提交并释放。
3. `:633-656` protocol 锁内只读重新核 refs/版本，然后在受收拢的线程里 unlink；此阶段不请求 writer。
4. 退出文件锁后 `:669-676` 再短 writer 按原版本/deleting/仍无 refs 确认删除。

不能将整个 cleanup 包进统一 writer，也不能把第 2 步 writer 持有延长到文件删除。
`_finish_thread:62-80` 包含重复取消下等待真实线程退出，保护文件 fence；这不是可丢诊断，
不能用普通取消 await 返回替换。普通 DB 连接收拢仍由公共 DB 层负责，二者对象不同。

`work_journal.invalidate:452-493`、`recovered_inputs:495-520` 仍有裸 begin，应迁移；
恢复原 journal、取消旧 staged input、原引用和消费状态继续同事务。

## C03：归档、等待和批量输入不是只改 BEGIN

`runtime/subagent_repository.py:159-356` maintain 先只读找漏通知/过期树与资源，
有 ACP peers 时在 writer 外 `close_peers_for_requests:293`，随后 writer 重新查完整归档资格
和 `ResourceCleanupPreparation.prepare:311`，再归档 children、Work、journal/effects/inputs/refs。
`sandbox/resource_cleanup.py:64-144` 查真实 request、run、peer/session、Work source 和 Conversation，
`verify()` 比较完整 request 集合及不可改写来源。不能只比较旧 IDs 的 updated_at，
新增 peer/未终态后代属于真实“缺席依赖”。

writer 中这次完整资源重读及 JSON 解码是规模检查对象：保留其正确性，但按索引/实际候选优化，
不能整体搬历史扫描，也不能把外部 close peers 作为可重放 callback。预先限制 16 个归档候选
不意味着每棵树/所关联资源一定很小，测试要包含大树和长 source/progress JSON。

`runtime/work_wait.py:960-1016` deliver_due 已是只读分页发现变化，最多选当前页候选，
writer 内重核 `_observe_page` 后才更新/投递。真实 now 在取得 writer 后采样；显式测试时间另保。
`match_event:489`、`match_input:628`、`match_user_reply:693` 有传入 session 的组合路径，
插件 publication/permission completion 已依赖它们同事务提交；不可一律独立开 writer。

`WorkRepository.prepare_input:1168-1269` 先准备媒体/JSON，writer 内把 ready、媒体 refs、
Work wakeup 一起发布，并调用 `before_publish`。必须追每个 before_publish 实参，
保证只有已准备 SQL；不能将公开的 async callback 视作天然短事务。

## C04：Social 的平台效果与本地结算必须分开记事实

正常路径 `social/service.py:_effect:1296-1475`：准备/目标查询在外，
`:1358-1449` 单 writer 中核 Person/SELF/插件/Work/route/reply 及 reserve，
`receipts.claim` 条件更新 prepared->executing，提交后才 dispatch。
`_dispatch_claimed:1506-1514` 是唯一外部 send/recall 调用，位于 SQLite 事务外。

正常确认后 `:1519` 分词在线程准备，`:1521-1581` 同 writer 内：
来源仍保留时追加实际出站账本；finish Social receipt；写原 Work delivery。
`social/repository.finish:352-405` 要求调用者传入 session。这里不能拆三个提交。

异常路径有必须单独覆盖的裸写：`service.py:1595-1607` 与
`plugin_host/facades.py:2693-2704`。平台已确认而正文已不可保留/本地正常结算失败时，
会另存 succeeded receipt（可能没有 ledger event）；未确认则 uncertain/failed。
因此任务书不能承诺“任意失败后都必有完整出站事件”，也不能把 absence 当作没发送而重发。
迁移这些收尾事务后，仍按原 operation ID 查询真实提交，提交不明保留未知；
不要对 `_dispatch_claimed` 或 SDK `_send_onebot` 整段自动重试。

`social/repository.prepare:100-193` 和 `recover_interrupted:426` 仍用裸 begin。
前者已有显式 BEGIN 的只读幂等快路，写时再次核 target/payload 并绑定 Work domain receipt；
保原 source_turn/call IDs。`prepare_sequence:195`、claim/stop/finish 的原 CAS 和冻结物理 parts 必须保留。
`social/emoji_intents.py` 是真实持久 deferred effect，不能当诊断删除；领取与关闭短写进公共 owner，
模型/查素材/传输继续在 writer 外。

插件 `_send_social:2426-2545` 与 `_send_onebot:2548-2714` 的 `state.lock`
用于一次 callback 内的 call ordinal/idempotency，不能因为有 DB writer 就删掉。
其读文件/快照/网关调用都在 SQLite writer 外；QQ 特权扩展仍只按原 QQ 入口授权。

## C05：诊断已经有队列，不能再造第二条业务队列

`application/modules/model_runtime.py:65-86` 在生产 build 中创建一个 DiagnosticWriter，
同时注入 ModelInvocationRepository 和 TraceRecorder，并注册 lifecycle；两者使用同一个 Database。
它不是独立数据库。`model_runtime/executor.py:471-600` 先 provider 返回，再记录真实 usage/attempt；
不在 writer 内等模型。缓存字段缺失继续 NULL，不因调度重构补零。

`persistence/diagnostic_writer.py:DiagnosticWrite.commit` 当前是 sessions+begin；
只把这一次 prepared SQL commit 迁到统一 writer。消费者在空 Context 运行，
冻结独立 payload、资源准入、压缩/编码先于事务；失败只缺样、不重试未知提交。
close 的有界 drain 之后会取消 consumer 并等待 task 真退出；公共 DB 清理不能让该等待提前解锁。

还要覆盖无 writer fallback：`model_runtime/repository.py:117-121` 和
`execution_trace/recorder.py:250-253`，不能因“生产通常注入”就留下新构造路径绕行。
前者先完成 correlation 再单短写；后者最终仍用 DiagnosticWrite。
`execution_trace/recorder.py:389-406` expiry cleanup 当前裸 begin、每 500 条提交，继续分页释放 writer。

保留 trace INSERT 中 source/privacy/delivery 原子 guards；不要恢复消费前逐条 preflight 查询，
不要用消费时当前 lease 否定发生时的真实诊断。不要为观察 writer 再同步写审计表。

## C06：插件仓库的迁移要覆盖配置、状态、会话和 notification 收尾

`plugin_host/configuration_service.save:158-210` 已先执行插件 Pydantic validators/序列化，
callback 只核 revision/schema identity/owner 并替换对应字段。
删除 apply_snapshot_write 后直接用短 writer；不额外造一套 prepare DTO 或把 validators 移回锁内。

`plugin_host/repository.py` 的 Installation upsert/status/failure、Config CAS/delete、
State delete、Audit record 仍有裸 begin；approve/set_enabled 走 optional_session。
`State.compare_and_set:606-703` 已在外序列化 JSON、内核 owner/version/expiry CAS。
这些简单主键读改写不需要双读所有数据。installation 状态和批准是执行事实，不能当临时状态删除。

`session_repository.py` create/append/reset 已 immediate；close/delete/delete_ephemeral 仍裸 begin；
expire_due 先读候选后 writer 条件清理。append 的父会话、owner、sender person、sequence/turn_count
及 message 必须同事务；已完成模型响应不因消费时自然 TTL 过期而失去保存资格。

`notification_repository._publish_once:448-682` 已先读 grant/identity/媒体 manifest/去重并准备分词，
writer 复核真实 installation/grant/source/媒体，追加 external event、匹配原 wait、
登记 outbox 和 background job，整体原子；不能拆分为“异步各自写”。
`grant_target:253`、`revoke_target:309`、`ensure_resolved_transport_alias:1360` 的裸 begin 收口；
claim/finish/fail/defer/retry 原状态 CAS、attempt、lease 与累计预算继续保留。

`notification_delivery.prepare:100-144` 只有只读 session 和实际传输计划转换；传输在原消费路径执行。
`background_turns.py:232-234` 读取 presence 后关闭 session，再进入 background turn/Work 激活/模型。
`main_turn.py:269-283` 同样先关闭身份读取 session，随后做外部模型请求；不要用 writer 包整次 SDK 调用。

`media_artifacts.create:50-117` 文件原子写在 writer 外，记录失败时清理原文件；
cleanup `:150-197` 先候选/短 writer 重核无 pending outbox，退出事务 unlink，再提交删除记录。
迁移保 pending/processing outbox 对原件的引用，不能因过期清理破坏已接纳效果。
该 cleanup 当前一次载入全部候选，实际规模下应按真实标识分页而不让早期失败文件饿死后续对象。

## C07：Sandbox 的两份数据库不是一个 owner

Bot 中 `SandboxTaskRepository`、Continuation、control completion 使用主 Database：
prepare intent 要先提交，再 socket submit。`task_repository.py:156/245/260/293` 的
checkpoint/bind/reject/receive 裸 begin、`continuations.rotate:41-51` 应收口。
receive terminal 与 continuation 的原子性、request/run/source/payload hash/旧完成回执不能删。
`completion_receiver.py:83-97` 在 tasks.receive 返回后才向 Manager ACK；
permission events 同样先 `receive_permission` 同事务保存 input/wait，再 ACK cursor。

Manager 是另一进程、另一文件：`sandbox/persistent.py:67` 直接
`sqlite3.connect(root/'jobs.sqlite3')`，completions 与 ACPResources 使用该连接。
该文件不是 Bot 的 DB owner，也不与 Bot 共享跨库原子事务。13 个 `with self.db` 事务块
经 AST 定位均没有 await；短同步 SQL 提交后再 Docker/execd/socket 等待。
不能把 Manager 的任务 queue 当数据库写队列，也不能把主 Bot FIFO 宣称为跨进程保护。
同步 sqlite3 的事件循环阻塞、Manager 自己的来源/回执/close 另按本文件规模验证，
不能为了“统一”给它新增网络依赖或移动执行事实。

`persistent.close:1254-1275` 会收拢 pumps 与所有真实 file worker，随后 db.close。
`completions.py` 的 terminal outbox 与 job 结果在同一 `with self.db`；它是可靠交接事实，
绝不能按 trace queue 的可丢标准处理。

## C08：Services 中最容易漏掉的升级快照与组合回调

`participation_feedback._commit_pending:516-549` 是显式 BEGIN 后 `_read_facts` 聚合，
再 `record_feedback(session=同一session)` 和手动 commit；这是读快照升级的具体候选。
需要准备真实 feedback page 后短 writer 重核 sequence、run 状态、实际效果与已消费 refs 全集合；
不能直接把整个聚合函数提前拿 writer。`:556-607` 的 controller session lock/_save
在 DB 事务外，DB writer 不是 controller 生命周期锁的替代。

`client_link.py:76-95` 使用 apply_snapshot_write；先确认原 connection proof，写时再核当前
presence/连接并调用 identity claim/issue(session=writer)。它涉及身份合并，按实际调用者一次提交，
不能由当前账号反写旧事件归属。

`services/main_agent_turns.py:176-282/369-510` 和 `context_compaction.py:124-172`
先准备 projection/publication，再由原 writer 发布；Work 路径将 publication 并入 dispatched journal，
普通聊天才自行短 writer。保 stage/rollback/finalize，不能同时让 caller 和 journal 分别排队提交。
`turn_execution.py:1180-1298` 的模型 compact 在 writer 外，paid response/checkpoint 先保存，
public compact publication 后提交；不能把整个 compact_current 视作纯 DB callback。

`services/admin/memory_admin.py` 的 add/update/delete fallback 仍通过
`Memory.apply_evidence_write`，且 mutation+admin audit 同 session；应随 Memory 域迁移，
不能只改直接 ORM 仓库。已调模型的 mutation 与确定性旧 fallback 的回执语义分别保留。
`services/admin/group_recovery.py:81-107` 网络候选准备在 writer 外，`:107-255`
再核 request/actor/binding/presence/connection/state 后写 binding/route/audit；不扩大为一整个路由恢复长事务。

## 删除与保留清单

可删：迁完后各裸 begin/手动 commit 的事务自管壳；仅服务旧升级快照的 BUSY 重备层；
无真实调用者的兼容 session 包装；旧实现专用的 SQL 次数/BEGIN 文本断言。
简单键更新不再加 generic plan、队列表、writer daemon、第二 commit journal 或每模块 shield。

保留：原 effect keys/domain receipt、paid model response、Work journal/协议配对、root budget、
lease expiry、generation/privacy/source、Social executing/uncertain、outbox ACK、callback call ordinal、
plugin approvals、文件 refs 与真实 GC 状态。删除必须证明职责被公共入口替代，而不是名称相似。

## 性能核对

当前用户接受已有响应延迟；本改造不以“提速”名义再加准入开关或业务门槛。
报告将 admission wait 与 transaction hold 分开；正常连接复用，不在每次短写重连。
增加大 Work/大量 refs、多个插件 outbox、trace 持续写、前台收发与心跳同负载组合，
包括无变化 acquire/renew/release 仍只读返回；不得让统一入口把这些快路也串行堵住。
writer 内重点成本是 ResourceCleanupPreparation/WorkWait 条件集合、JSON/refs 批量、
Social 各次授权查读；既不能删除必要核验，也不能无差别重复整份来源重读。

## 对应测试阅读与实施时要补的真实场景

本轮未运行测试；以下只说明源测试覆盖意图，不能写成验证通过。

- `test_work_scope_contention.py`：busy/旧 acquire、失效 renew/release 不写；取得 writer 后的数据库时钟；
  两候选只一个 owner；源 generation/owner 变化重核；独立 scope 不被大量候选遮挡。
  目前监听 BEGIN IMMEDIATE 的 barrier 需换成实际 admission/SQL 阶段，保留行为，不冻结旧实现。
- `test_work_journal_busy_recovery.py`：同 paid response 新事务发布、不限固定次数、原预算、cancel/lease 退出，
  部分 synthetic OperationalError 注入不能证明真实 driver 在 commit 取消后已经停止；新增真实 WAL/线程 barrier。
- `test_social_concurrency.py`、`test_social_delivery_integrity.py`、`test_social_receipt_original_anchor.py`：
  原 claim 唯一、route/identity 变化、未知与 confirmed settlement；补 writer 等待后取消/平台确认但本地 ACK 丢失。
- `test_diagnostic_writer.py`：prepared-before-write、单条失败不重试、已 commit 丢 ACK 时不声称成功、
  关闭与 payload 释放；`test_diagnostic_source_privacy.py` 保原 source/privacy 原子条件。
- `test_plugin_media_cleanup_recovery.py`：unlink 临时失败、取消后原 row 保留、超过 128 的后续原件不饥饿、
  pending outbox 保过期媒体。继续核 writer 交错，不要求把文件线程变成 DB writer。
- 后轮继续逐方法读 `test_invocation_crash_windows.py`、`test_invocation_process_crash.py`、
  `test_work_settlement_writer.py`、`test_work_delivery_ownership.py`、`test_work_native_media_receipts.py`、
  `test_plugin_notification_idempotency.py`、`test_plugin_background_worker_recovery.py`、
  `test_sandbox_submission_unknown_lifecycle*`、ACP close/cancel 与 Protocol GC/并发对应测试；
  仅文件存在不能认定其已覆盖新的统一 writer 行为。

## 第 2/3 轮待交叉核对

1. 大型 repository 的非热点写法和所有 session 外传调用者；下附清单标记范围，不冒称逐行全文完成。
2. publication/before_publish 全部实参是否只做准备好的 SQL；和 Memory/Canonical 域的 session 组合。
3. worker close -> diagnostic drain -> DB close 的生命周期实际顺序；DB owner 停止接纳时不得阻断必要已知效果收尾。
4. 各类未知提交的实际探测能证明什么；不能将“没找到 receipt”推断成允许重购/重发。
5. Manager jobs.sqlite3 及 ACP sharing connection 的同步 SQL 是否有跨 await 隐式事务；
   目前已核显式 with 块无 await，还要追未包 with 的 DML 与 execscript 生命周期。
6. 所有纯结构/渲染排除项由下一轮独立确认；直接 DB 信号来自工具 execute 的误报应明确消除。

## 第 1 轮全局汇总：对 D 域的跨界复核

本节仍是第 1 轮，不预记第 2 轮。阅读 `review-d.md` 后，补读
`participation_snapshot.py` 全文、`participation_diagnostics.py` 全文，
`semantic_participation.py:194-235/423-519`、
`participation_feedback.py:315-669`、`container.py:844-936/1011-1019`，
以及 LifecycleRegistry 的注册/启停错误收拢全文。

1. **D05 方向正确，但“单线程队列”不能独立证明取消后的业务 revision 正确。**
   `AsyncSnapshotStore._run:43-48` shield 的是 executor future；调用者仍可先收到取消，
   同步保存可能继续。真正使已提交 revision 不被丢弃的是
   `SemanticParticipationService._save:488-519`：保存 payload 不变、使用原 revision，
   重复取消时等 save task 真实结束，先更新 `item.revision/item.saved` 才传播取消。
   迁移时必须一起保留两层的实际职责，不能仅见 SnapshotStore 有 shield 就删除 service 层收拢。

2. **D05 的跨库非原子性准确；需要补“哪一边先提交”和恢复身份。**
   `participation_feedback.py:516-549` 先主库保存 durable sequence/effect refs；
   `:590-605` 退出主库事务后，在 controller `_session_lock` 下按真实反馈 replay 并 `_save`
   到 participation.sqlite3。`:610-645` 复用原 run/proposal/sequence，冷快照缺 proposal 时
   只还原实际已计费/已发生 effect，不伪造新 admission。
   主库 receipt 已提交而参与快照未保存是可恢复边界，不能加跨库事务或重做原模型/发送。
   统一主库 writer 也不能把 `_session_lock -> SnapshotStore.save` 搬进主库事务。

3. **D05 的“取消不取消正在执行函数”应区分连接退出与成功认领。**
   SnapshotStore.close 排在之前已经提交到 executor 的工作后，但 close 之前停止业务提交的事实
   来自 `semantic_participation.close:217-235`：先收拢 loop/promotions，再取得已有 locks，
   保存 session、关闭 observer/store。不要把 executor close 描述成它自己阻止任何新 producer；
   同理主库 owner 的停止接纳时点应依靠现有 lifecycle 顺序，而不是第二个关机状态机。

4. **D02 的逆序关闭已核成立，但读健康不等于“本次全部收尾成功”。**
   `container.close:1014-1019` 先发布 APPLICATION_STOPPING，随后 registry 逆序关资源；
   `LifecycleRegistry._close_entries` 即使某 hook 抛异常也继续关其它资源并聚合错误。
   对 writer 清理未确认的情况，后续 Database.dispose 不能把尚在执行的真实 driver 操作当作消失；
   公共层必须持有具体清理归属。D02 所列“正常关闭应”是设计目标，不能据注册顺序单独写成
   所有失败/重复取消/启动回滚都已获得真实驱动退出证明。

5. **D08 没有把统计窗口当业务限制，现行实现也应照保。**
   `participation_diagnostics.py:128-232` 只读有界窗口，记录 truncated、feedback_complete、
   NULL 未知 token/latency/lifetime；不因诊断缺样阻断参与或回复。它不是主库写热点，
   也不需要为了“统一”进入 writer。`participation_feedback._read_facts:349-513`
   则明确是原 run 的完整证据集合、不是诊断窗口；两者不能共用一个有界抽样查询代替正确性。

本次未发现 D01/D04/D06/D07 与上述现行边界直接矛盾。建议在总任务书将以上五点写成具体
方法/恢复场景，避免仅列“跨库反馈保留”和“关闭安全”而漏掉实际先后关系。没有运行相关测试。

逐文件静态覆盖清单与方法入口见下文；它是继续审查的导航，不是单凭清单证明全部实现已审完。

## 逐文件导航（第 1 轮）

D = 已人工核读下列精确范围；I = 文件级 AST/导入/调用入口已分类，未声称全文深读；P = DB 迁移排除的结构/纯处理部分，后轮仍核排除是否成立。D 行的未覆盖段不自动算通过。

| 文件（相对 src/yuki） | 行数 | 覆盖 | 本轮范围/排除理由；实际事务候选入口 |
| --- | ---: | --- | --- |
| `runtime/__init__.py` | 26 | P | 结构/参数/数据模型/纯格式处理；不自管 SQLite 写事务（依赖调用者） |
| `runtime/activation_bindings.py` | 62 | P | 结构/参数/数据模型/纯格式处理；不自管 SQLite 写事务（依赖调用者） |
| `runtime/activation_outcome.py` | 171 | P | 结构/参数/数据模型/纯格式处理；不自管 SQLite 写事务（依赖调用者） |
| `runtime/activation_tasks.py` | 67 | I | 间接服务/调用链分类；不以 execute/flush 名称推断 DB 写入，后轮核具体方法 |
| `runtime/context_preparation.py` | 116 | I | 间接服务/调用链分类；不以 execute/flush 名称推断 DB 写入，后轮核具体方法 |
| `runtime/contracts.py` | 27 | P | 结构/参数/数据模型/纯格式处理；不自管 SQLite 写事务（依赖调用者） |
| `runtime/delivery.py` | 14 | I | 间接服务/调用链分类；不以 execute/flush 名称推断 DB 写入，后轮核具体方法 |
| `runtime/delivery_intents.py` | 124 | I | 间接服务/调用链分类；不以 execute/flush 名称推断 DB 写入，后轮核具体方法；`reserve:21`, `record:104` |
| `runtime/effect_outcomes.py` | 421 | I | 间接服务/调用链分类；不以 execute/flush 名称推断 DB 写入，后轮核具体方法 |
| `runtime/effect_queries.py` | 75 | I | 间接服务/调用链分类；不以 execute/flush 名称推断 DB 写入，后轮核具体方法 |
| `runtime/effect_schema.py` | 45 | P | 结构/参数/数据模型/纯格式处理；不自管 SQLite 写事务（依赖调用者） |
| `runtime/errors.py` | 15 | P | 结构/参数/数据模型/纯格式处理；不自管 SQLite 写事务（依赖调用者） |
| `runtime/execution_receipts.py` | 27 | I | 间接服务/调用链分类；不以 execute/flush 名称推断 DB 写入，后轮核具体方法 |
| `runtime/lease_heartbeat.py` | 119 | D | 全文；expiry/renew/取消监督 |
| `runtime/observability.py` | 77 | I | 间接服务/调用链分类；不以 execute/flush 名称推断 DB 写入，后轮核具体方法 |
| `runtime/origin.py` | 28 | P | 结构/参数/数据模型/纯格式处理；不自管 SQLite 写事务（依赖调用者） |
| `runtime/principal.py` | 25 | P | 结构/参数/数据模型/纯格式处理；不自管 SQLite 写事务（依赖调用者） |
| `runtime/protocol_schema.py` | 49 | P | 结构/参数/数据模型/纯格式处理；不自管 SQLite 写事务（依赖调用者） |
| `runtime/protocol_store.py` | 677 | D | 62-80、401-467、570-677；publication/GC/线程收拢；`cleanup:570` |
| `runtime/subagent_repository.py` | 725 | D | 159-356；其它事务方法入口索引，后轮深读；`start:36`, `acquire:91`, `maintain:159`, `message:467`, `resume:579`, `cancel:634`, `finish:645` |
| `runtime/subagent_scheduler.py` | 137 | I | 间接服务/调用链分类；不以 execute/flush 名称推断 DB 写入，后轮核具体方法 |
| `runtime/subagent_schema.py` | 42 | P | 结构/参数/数据模型/纯格式处理；不自管 SQLite 写事务（依赖调用者） |
| `runtime/subagent_tools.py` | 155 | I | 间接服务/调用链分类；不以 execute/flush 名称推断 DB 写入，后轮核具体方法 |
| `runtime/trigger.py` | 104 | I | 间接服务/调用链分类；不以 execute/flush 名称推断 DB 写入，后轮核具体方法 |
| `runtime/work_activation.py` | 206 | I | 间接服务/调用链分类；不以 execute/flush 名称推断 DB 写入，后轮核具体方法 |
| `runtime/work_budget.py` | 64 | I | 间接服务/调用链分类；不以 execute/flush 名称推断 DB 写入，后轮核具体方法 |
| `runtime/work_budget_schema.py` | 21 | P | 结构/参数/数据模型/纯格式处理；不自管 SQLite 写事务（依赖调用者） |
| `runtime/work_context_note.py` | 190 | I | 间接服务/调用链分类；不以 execute/flush 名称推断 DB 写入，后轮核具体方法 |
| `runtime/work_control.py` | 1132 | I | 间接服务/调用链分类；不以 execute/flush 名称推断 DB 写入，后轮核具体方法；`_control:679` |
| `runtime/work_journal.py` | 757 | D | 313-520、713-757；save/invalidate/input/effect publication；`save:313`, `invalidate:452`, `recovered_inputs:495` |
| `runtime/work_management.py` | 356 | I | 间接服务/调用链分类；不以 execute/flush 名称推断 DB 写入，后轮核具体方法 |
| `runtime/work_media.py` | 43 | I | 间接服务/调用链分类；不以 execute/flush 名称推断 DB 写入，后轮核具体方法 |
| `runtime/work_observation_schema.py` | 10 | P | 结构/参数/数据模型/纯格式处理；不自管 SQLite 写事务（依赖调用者） |
| `runtime/work_queries.py` | 234 | I | 间接服务/调用链分类；不以 execute/flush 名称推断 DB 写入，后轮核具体方法 |
| `runtime/work_query_schema.py` | 89 | P | 结构/参数/数据模型/纯格式处理；不自管 SQLite 写事务（依赖调用者） |
| `runtime/work_recovery_schema.py` | 44 | P | 结构/参数/数据模型/纯格式处理；不自管 SQLite 写事务（依赖调用者） |
| `runtime/work_repository.py` | 2781 | D | 135-243、1168-1269、1889-1918、2080-2285、2630-2781；其余方法静态边界索引，后轮深读；`acquire:135`, `renew:209`, `release:232`, `accept:281`, `derive:458`, `transition:539`, `accept_control:843`, `checkpoint:885`, `patch_context_note:951`, `patch_communication:1023`, `prepare_input:1168`, `stage:1328`, `consume:1349`, `cancel:1363`, `confirm_child_completion:1507`, `route_child_completion:1615`, `reclaim_unreferenced_media:1863`, `discard_input:1889`, `prepare_effect:1920`, `publish_code_boundary:1972`, `admit_dispatch:2218`, `resolve_run_effects:2534`, `record_effect:2648` |
| `runtime/work_scheduler.py` | 358 | I | 间接服务/调用链分类；不以 execute/flush 名称推断 DB 写入，后轮核具体方法 |
| `runtime/work_schema_v1.py` | 120 | P | 结构/参数/数据模型/纯格式处理；不自管 SQLite 写事务（依赖调用者） |
| `runtime/work_session.py` | 1055 | D | 921-1055；其它 restore/compact 调用边界索引 |
| `runtime/work_source_guard.py` | 391 | I | 间接服务/调用链分类；不以 execute/flush 名称推断 DB 写入，后轮核具体方法 |
| `runtime/work_supervisor.py` | 267 | I | 间接服务/调用链分类；不以 execute/flush 名称推断 DB 写入，后轮核具体方法；`recover_failure:53` |
| `runtime/work_tree.py` | 69 | I | 间接服务/调用链分类；不以 execute/flush 名称推断 DB 写入，后轮核具体方法 |
| `runtime/work_wait.py` | 1016 | D | 837-1016；其它传入 session 调用边界索引，后轮深读；`cancel:152`, `register:170`, `match_event:489`, `match_input:628`, `match_user_reply:693`, `deliver_due:960` |
| `runtime/work_wait_schema.py` | 48 | P | 结构/参数/数据模型/纯格式处理；不自管 SQLite 写事务（依赖调用者） |
| `model_runtime/__init__.py` | 63 | P | 结构/参数/数据模型/纯格式处理；不自管 SQLite 写事务（依赖调用者） |
| `model_runtime/capacity.py` | 264 | P | 结构/参数/数据模型/纯格式处理；不自管 SQLite 写事务（依赖调用者） |
| `model_runtime/db_models.py` | 61 | P | 结构/参数/数据模型/纯格式处理；不自管 SQLite 写事务（依赖调用者） |
| `model_runtime/dispatch_guard.py` | 26 | I | 间接服务/调用链分类；不以 execute/flush 名称推断 DB 写入，后轮核具体方法 |
| `model_runtime/executor.py` | 965 | D | 363-600、928-965；provider 调度方法按调用索引 |
| `model_runtime/models.py` | 214 | P | 结构/参数/数据模型/纯格式处理；不自管 SQLite 写事务（依赖调用者） |
| `model_runtime/pool.py` | 172 | I | 间接服务/调用链分类；不以 execute/flush 名称推断 DB 写入，后轮核具体方法 |
| `model_runtime/profiles.py` | 240 | P | 结构/参数/数据模型/纯格式处理；不自管 SQLite 写事务（依赖调用者） |
| `model_runtime/repository.py` | 274 | D | 全文；生产 DI 与无 writer fallback；`record:39` |
| `model_runtime/request_accounting.py` | 92 | I | 间接服务/调用链分类；不以 execute/flush 名称推断 DB 写入，后轮核具体方法 |
| `model_runtime/routes.py` | 32 | P | 结构/参数/数据模型/纯格式处理；不自管 SQLite 写事务（依赖调用者） |
| `model_runtime/search_defaults.py` | 110 | P | 结构/参数/数据模型/纯格式处理；不自管 SQLite 写事务（依赖调用者） |
| `model_runtime/secrets.py` | 80 | P | 结构/参数/数据模型/纯格式处理；不自管 SQLite 写事务（依赖调用者） |
| `model_runtime/structured.py` | 393 | I | 间接服务/调用链分类；不以 execute/flush 名称推断 DB 写入，后轮核具体方法 |
| `execution_trace/__init__.py` | 1 | P | 结构/参数/数据模型/纯格式处理；不自管 SQLite 写事务（依赖调用者） |
| `execution_trace/db_models.py` | 63 | P | 结构/参数/数据模型/纯格式处理；不自管 SQLite 写事务（依赖调用者） |
| `execution_trace/payload.py` | 184 | P | 结构/参数/数据模型/纯格式处理；不自管 SQLite 写事务（依赖调用者） |
| `execution_trace/phases.py` | 150 | P | 结构/参数/数据模型/纯格式处理；不自管 SQLite 写事务（依赖调用者） |
| `execution_trace/recorder.py` | 521 | D | 170-260、389-521；其余 guard/encoding 按合同与调用索引；`cleanup_expired:385` |
| `social/__init__.py` | 1 | P | 结构/参数/数据模型/纯格式处理；不自管 SQLite 写事务（依赖调用者） |
| `social/agent_adapter.py` | 216 | I | 间接服务/调用链分类；不以 execute/flush 名称推断 DB 写入，后轮核具体方法 |
| `social/confirmed_content.py` | 35 | P | 结构/参数/数据模型/纯格式处理；不自管 SQLite 写事务（依赖调用者） |
| `social/content.py` | 331 | I | 间接服务/调用链分类；不以 execute/flush 名称推断 DB 写入，后轮核具体方法 |
| `social/db_models.py` | 89 | P | 结构/参数/数据模型/纯格式处理；不自管 SQLite 写事务（依赖调用者） |
| `social/emoji_intents.py` | 376 | I | 间接服务/调用链分类；不以 execute/flush 名称推断 DB 写入，后轮核具体方法；`select:346`, `close:359` |
| `social/history.py` | 386 | I | 间接服务/调用链分类；不以 execute/flush 名称推断 DB 写入，后轮核具体方法 |
| `social/models.py` | 81 | P | 结构/参数/数据模型/纯格式处理；不自管 SQLite 写事务（依赖调用者） |
| `social/repository.py` | 709 | D | 1-240、300-510；其余 projection/read helper 后轮；`prepare:100`, `prepare_sequence:195`, `claim:302`, `stop_sequence:335`, `recover_interrupted:426` |
| `social/service.py` | 1811 | D | 1280-1620；前置 send/route/read 方法调用索引；`_validate_person_context:164`, `_validate_self_context:173`, `check_target:479`, `_effect:1296`, `_dispatch_claimed:1477` |
| `social/source_keys.py` | 22 | P | 结构/参数/数据模型/纯格式处理；不自管 SQLite 写事务（依赖调用者） |
| `social/tools.py` | 167 | P | 结构/参数/数据模型/纯格式处理；不自管 SQLite 写事务（依赖调用者） |
| `plugin_host/__init__.py` | 5 | P | 结构/参数/数据模型/纯格式处理；不自管 SQLite 写事务（依赖调用者） |
| `plugin_host/admission_adapter.py` | 172 | I | 间接服务/调用链分类；不以 execute/flush 名称推断 DB 写入，后轮核具体方法 |
| `plugin_host/approval.py` | 66 | I | 间接服务/调用链分类；不以 execute/flush 名称推断 DB 写入，后轮核具体方法 |
| `plugin_host/audit.py` | 42 | I | 间接服务/调用链分类；不以 execute/flush 名称推断 DB 写入，后轮核具体方法 |
| `plugin_host/background_turns.py` | 456 | D | 181-330；其余 worker lifecycle/callback 索引 |
| `plugin_host/canonical_projection.py` | 57 | I | 间接服务/调用链分类；不以 execute/flush 名称推断 DB 写入，后轮核具体方法 |
| `plugin_host/capability_adapter.py` | 375 | I | 间接服务/调用链分类；不以 execute/flush 名称推断 DB 写入，后轮核具体方法 |
| `plugin_host/command_adapter.py` | 206 | I | 间接服务/调用链分类；不以 execute/flush 名称推断 DB 写入，后轮核具体方法 |
| `plugin_host/config.py` | 128 | P | 结构/参数/数据模型/纯格式处理；不自管 SQLite 写事务（依赖调用者） |
| `plugin_host/configuration_service.py` | 210 | D | 158-210；validator/版本回调；`save:158` |
| `plugin_host/db_models.py` | 635 | P | 结构/参数/数据模型/纯格式处理；不自管 SQLite 写事务（依赖调用者） |
| `plugin_host/direct_command_router.py` | 91 | I | 间接服务/调用链分类；不以 execute/flush 名称推断 DB 写入，后轮核具体方法 |
| `plugin_host/direct_delivery.py` | 163 | I | 间接服务/调用链分类；不以 execute/flush 名称推断 DB 写入，后轮核具体方法 |
| `plugin_host/discovery.py` | 87 | I | 间接服务/调用链分类；不以 execute/flush 名称推断 DB 写入，后轮核具体方法 |
| `plugin_host/event_bus.py` | 297 | I | 间接服务/调用链分类；不以 execute/flush 名称推断 DB 写入，后轮核具体方法 |
| `plugin_host/extension_registry.py` | 283 | I | 间接服务/调用链分类；不以 execute/flush 名称推断 DB 写入，后轮核具体方法 |
| `plugin_host/facades.py` | 3260 | D | 2426-2714；其余 SDK methods 按调用索引；`_send_onebot:2548`, `_is_real_superuser:783`, `_require_user_scope:800` |
| `plugin_host/http_client.py` | 314 | I | 间接服务/调用链分类；不以 execute/flush 名称推断 DB 写入，后轮核具体方法 |
| `plugin_host/loader.py` | 86 | I | 间接服务/调用链分类；不以 execute/flush 名称推断 DB 写入，后轮核具体方法 |
| `plugin_host/main_turn.py` | 714 | D | 198-345；其余原 Work resume 调用索引 |
| `plugin_host/manager.py` | 876 | I | 间接服务/调用链分类；不以 execute/flush 名称推断 DB 写入，后轮核具体方法 |
| `plugin_host/manifest.py` | 239 | P | 结构/参数/数据模型/纯格式处理；不自管 SQLite 写事务（依赖调用者） |
| `plugin_host/media_artifacts.py` | 263 | D | 50-197；文件/DB 分阶段；`create:50`, `cleanup:150` |
| `plugin_host/models.py` | 42 | P | 结构/参数/数据模型/纯格式处理；不自管 SQLite 写事务（依赖调用者） |
| `plugin_host/notification_delivery.py` | 633 | D | 100-144；其余 delivery 消费调用链索引 |
| `plugin_host/notification_manifest.py` | 109 | P | 结构/参数/数据模型/纯格式处理；不自管 SQLite 写事务（依赖调用者） |
| `plugin_host/notification_repository.py` | 2231 | D | 448-682；其余 lease/job/outbox methods 静态索引，后轮深读；`admit_background_turn_work:1552`, `fail_unclaimed_outbox:192`, `prepare_outbox:216`, `grant_target:253`, `revoke_target:309`, `_publish_once:448`, `claim_outbox:684`, `finish_outbox:756`, `retry_outbox:786`, `claim_turn:834`, `reconcile_terminal_turns:923`, `validate_turn_attempt:947`, `finish_turn:978`, `fail_turn:1011`, `abandon_turn:1033`, `park_turn:1052`, `defer_turn:1082`, `require_outbox_ready:1145`, `require_agent_reply_ready:1158`, `load_background_context:1257`, `ensure_resolved_transport_alias:1360` |
| `plugin_host/ownership.py` | 517 | I | 间接服务/调用链分类；不以 execute/flush 名称推断 DB 写入，后轮核具体方法 |
| `plugin_host/prompt_adapter.py` | 52 | I | 间接服务/调用链分类；不以 execute/flush 名称推断 DB 写入，后轮核具体方法 |
| `plugin_host/repository.py` | 942 | D | 606-703；其它 Config/Installation/Audit 入口索引，后轮深读；`upsert_discovered:119`, `get:184`, `approve:200`, `set_enabled:224`, `set_status:245`, `record_failure:267`, `compare_and_set:328`, `delete:441`, `compare_and_set:606`, `delete:705`, `cleanup_expired:729`, `record:759` |
| `plugin_host/secrets.py` | 48 | P | 结构/参数/数据模型/纯格式处理；不自管 SQLite 写事务（依赖调用者） |
| `plugin_host/session_facade.py` | 93 | I | 间接服务/调用链分类；不以 execute/flush 名称推断 DB 写入，后轮核具体方法 |
| `plugin_host/session_repository.py` | 493 | D | 192-253；其它会话状态入口索引，后轮深读；`create:87`, `append_message:192`, `close:286`, `reset:308`, `delete:352`, `expire_due:362`, `delete_ephemeral:390` |
| `plugin_host/storage.py` | 154 | I | 间接服务/调用链分类；不以 execute/flush 名称推断 DB 写入，后轮核具体方法 |
| `sandbox/__init__.py` | 1 | P | 结构/参数/数据模型/纯格式处理；不自管 SQLite 写事务（依赖调用者） |
| `sandbox/acp_config.py` | 73 | P | 结构/参数/数据模型/纯格式处理；不自管 SQLite 写事务（依赖调用者） |
| `sandbox/acp_manager.py` | 477 | I | 间接服务/调用链分类；不以 execute/flush 名称推断 DB 写入，后轮核具体方法 |
| `sandbox/acp_peer.py` | 636 | I | 间接服务/调用链分类；不以 execute/flush 名称推断 DB 写入，后轮核具体方法 |
| `sandbox/client.py` | 217 | I | 间接服务/调用链分类；不以 execute/flush 名称推断 DB 写入，后轮核具体方法 |
| `sandbox/completion_receiver.py` | 140 | D | 全文；receive 后 ACK/cursor |
| `sandbox/completions.py` | 54 | I | 间接服务/调用链分类；不以 execute/flush 名称推断 DB 写入，后轮核具体方法；`acknowledge:49` |
| `sandbox/continuation_worker.py` | 62 | I | 间接服务/调用链分类；不以 execute/flush 名称推断 DB 写入，后轮核具体方法 |
| `sandbox/continuations.py` | 51 | D | 全文；ready/observed/rotate；`rotate:41` |
| `sandbox/control_completions.py` | 94 | D | 全文；operator completion 事实；`record_control_completion:18` |
| `sandbox/db_models.py` | 58 | P | 结构/参数/数据模型/纯格式处理；不自管 SQLite 写事务（依赖调用者） |
| `sandbox/environment_supervisor.py` | 198 | I | 间接服务/调用链分类；不以 execute/flush 名称推断 DB 写入，后轮核具体方法 |
| `sandbox/environment_tools.py` | 138 | I | 间接服务/调用链分类；不以 execute/flush 名称推断 DB 写入，后轮核具体方法 |
| `sandbox/execd.py` | 48 | I | 间接服务/调用链分类；不以 execute/flush 名称推断 DB 写入，后轮核具体方法 |
| `sandbox/external_agents.py` | 252 | I | 间接服务/调用链分类；不以 execute/flush 名称推断 DB 写入，后轮核具体方法 |
| `sandbox/manager.py` | 90 | I | 间接服务/调用链分类；不以 execute/flush 名称推断 DB 写入，后轮核具体方法 |
| `sandbox/persistent.py` | 1275 | D | 1-220、245-335、430-510、830-935、1120-1275；所有显式 with self.db 块 AST 无 await，剩余 DML 后轮深读；`_finish_receipt:131`, `set_setting:201`, `finish:297`, `launch:570`, `submit:623`, `_file_request:880`, `service:996`, `restore_services:1056` |
| `sandbox/resource_cleanup.py` | 144 | D | 全文；source/集合复核 |
| `sandbox/task_repository.py` | 324 | D | 1-164、180-324；原 intent 与 completion/continuation；`prepare:53`, `checkpoint:154`, `receive_permission:185`, `bind_run:242`, `reject:258`, `receive:275` |
| `services/__init__.py` | 29 | P | 结构/参数/数据模型/纯格式处理；不自管 SQLite 写事务（依赖调用者） |
| `services/admin/__init__.py` | 13 | P | 结构/参数/数据模型/纯格式处理；不自管 SQLite 写事务（依赖调用者） |
| `services/admin/config_admin.py` | 108 | I | 间接服务/调用链分类；不以 execute/flush 名称推断 DB 写入，后轮核具体方法 |
| `services/admin/control_auth.py` | 73 | I | 间接服务/调用链分类；不以 execute/flush 名称推断 DB 写入，后轮核具体方法 |
| `services/admin/group_admin.py` | 84 | I | 间接服务/调用链分类；不以 execute/flush 名称推断 DB 写入，后轮核具体方法 |
| `services/admin/group_recovery.py` | 379 | D | 81-180；其余 route/audit 同事务写入口索引；`enable:45` |
| `services/admin/memory_admin.py` | 1208 | D | 232-308；update/delete 同类回调入口索引；`add_memory:232`, `update_memory:310`, `delete_memory:407`, `_require_authorized_person_target:1106`, `_is_superuser:1190` |
| `services/admin/preference_admin.py` | 167 | I | 间接服务/调用链分类；不以 execute/flush 名称推断 DB 写入，后轮核具体方法 |
| `services/agent_runner.py` | 1106 | I | 间接服务/调用链分类；不以 execute/flush 名称推断 DB 写入，后轮核具体方法 |
| `services/agent_tools.py` | 2227 | I | 间接服务/调用链分类；不以 execute/flush 名称推断 DB 写入，后轮核具体方法 |
| `services/automation_commands.py` | 121 | I | 间接服务/调用链分类；不以 execute/flush 名称推断 DB 写入，后轮核具体方法 |
| `services/autonomous_groups.py` | 326 | I | 间接服务/调用链分类；不以 execute/flush 名称推断 DB 写入，后轮核具体方法 |
| `services/chat.py` | 1685 | I | 间接服务/调用链分类；不以 execute/flush 名称推断 DB 写入，后轮核具体方法 |
| `services/chat_preparation_timings.py` | 110 | P | 结构/参数/数据模型/纯格式处理；不自管 SQLite 写事务（依赖调用者） |
| `services/client_link.py` | 104 | D | 全文；connection proof + identity callback；`execute:50` |
| `services/command_service.py` | 231 | I | 间接服务/调用链分类；不以 execute/flush 名称推断 DB 写入，后轮核具体方法 |
| `services/commands/__init__.py` | 1 | P | 结构/参数/数据模型/纯格式处理；不自管 SQLite 写事务（依赖调用者） |
| `services/commands/admin.py` | 154 | I | 间接服务/调用链分类；不以 execute/flush 名称推断 DB 写入，后轮核具体方法 |
| `services/commands/conversation.py` | 117 | I | 间接服务/调用链分类；不以 execute/flush 名称推断 DB 写入，后轮核具体方法 |
| `services/commands/group.py` | 14 | I | 间接服务/调用链分类；不以 execute/flush 名称推断 DB 写入，后轮核具体方法 |
| `services/commands/help.py` | 153 | I | 间接服务/调用链分类；不以 execute/flush 名称推断 DB 写入，后轮核具体方法 |
| `services/commands/identity.py` | 216 | I | 间接服务/调用链分类；不以 execute/flush 名称推断 DB 写入，后轮核具体方法 |
| `services/commands/memory.py` | 564 | I | 间接服务/调用链分类；不以 execute/flush 名称推断 DB 写入，后轮核具体方法 |
| `services/commands/models.py` | 46 | P | 结构/参数/数据模型/纯格式处理；不自管 SQLite 写事务（依赖调用者） |
| `services/commands/plugins.py` | 81 | I | 间接服务/调用链分类；不以 execute/flush 名称推断 DB 写入，后轮核具体方法 |
| `services/commands/work.py` | 49 | I | 间接服务/调用链分类；不以 execute/flush 名称推断 DB 写入，后轮核具体方法 |
| `services/concurrency.py` | 95 | I | 间接服务/调用链分类；不以 execute/flush 名称推断 DB 写入，后轮核具体方法 |
| `services/config_commands.py` | 286 | I | 间接服务/调用链分类；不以 execute/flush 名称推断 DB 写入，后轮核具体方法 |
| `services/context_assembler.py` | 501 | I | 间接服务/调用链分类；不以 execute/flush 名称推断 DB 写入，后轮核具体方法 |
| `services/context_boundary.py` | 55 | P | 结构/参数/数据模型/纯格式处理；不自管 SQLite 写事务（依赖调用者） |
| `services/context_compaction.py` | 652 | D | 85-205；read/write/publication；`write:124` |
| `services/deduplication.py` | 14 | P | 结构/参数/数据模型/纯格式处理；不自管 SQLite 写事务（依赖调用者） |
| `services/document_reader.py` | 214 | P | 结构/参数/数据模型/纯格式处理；不自管 SQLite 写事务（依赖调用者） |
| `services/durable_invocations.py` | 299 | I | 间接服务/调用链分类；不以 execute/flush 名称推断 DB 写入，后轮核具体方法 |
| `services/effect_gate.py` | 65 | I | 间接服务/调用链分类；不以 execute/flush 名称推断 DB 写入，后轮核具体方法 |
| `services/evidence_observation.py` | 92 | I | 间接服务/调用链分类；不以 execute/flush 名称推断 DB 写入，后轮核具体方法 |
| `services/evidence_state.py` | 39 | I | 间接服务/调用链分类；不以 execute/flush 名称推断 DB 写入，后轮核具体方法 |
| `services/execution_sources.py` | 599 | I | 间接服务/调用链分类；不以 execute/flush 名称推断 DB 写入，后轮核具体方法 |
| `services/group_recovery.py` | 131 | I | 间接服务/调用链分类；不以 execute/flush 名称推断 DB 写入，后轮核具体方法 |
| `services/history_projection.py` | 221 | I | 间接服务/调用链分类；不以 execute/flush 名称推断 DB 写入，后轮核具体方法 |
| `services/history_query.py` | 73 | I | 间接服务/调用链分类；不以 execute/flush 名称推断 DB 写入，后轮核具体方法 |
| `services/http_logging.py` | 40 | P | 结构/参数/数据模型/纯格式处理；不自管 SQLite 写事务（依赖调用者） |
| `services/image_preprocessor.py` | 212 | I | 间接服务/调用链分类；不以 execute/flush 名称推断 DB 写入，后轮核具体方法 |
| `services/invocation_context.py` | 54 | P | 结构/参数/数据模型/纯格式处理；不自管 SQLite 写事务（依赖调用者） |
| `services/invocation_service.py` | 328 | I | 间接服务/调用链分类；不以 execute/flush 名称推断 DB 写入，后轮核具体方法 |
| `services/main_agent_backend.py` | 744 | I | 间接服务/调用链分类；不以 execute/flush 名称推断 DB 写入，后轮核具体方法 |
| `services/main_agent_contract.py` | 153 | P | 结构/参数/数据模型/纯格式处理；不自管 SQLite 写事务（依赖调用者） |
| `services/main_agent_turns.py` | 664 | D | 176-282、369-510；其余 compose/run 方法边界索引；`compose:102`, `commit_projection:176`, `observation_boundary:284`, `commit_boundary:485` |
| `services/media_process.py` | 25 | I | 间接服务/调用链分类；不以 execute/flush 名称推断 DB 写入，后轮核具体方法 |
| `services/media_resolver.py` | 625 | I | 间接服务/调用链分类；不以 execute/flush 名称推断 DB 写入，后轮核具体方法 |
| `services/message_splitter.py` | 358 | P | 结构/参数/数据模型/纯格式处理；不自管 SQLite 写事务（依赖调用者） |
| `services/model_context_projection.py` | 10 | P | 结构/参数/数据模型/纯格式处理；不自管 SQLite 写事务（依赖调用者） |
| `services/native_media.py` | 126 | I | 间接服务/调用链分类；不以 execute/flush 名称推断 DB 写入，后轮核具体方法 |
| `services/native_tool_binder.py` | 87 | I | 间接服务/调用链分类；不以 execute/flush 名称推断 DB 写入，后轮核具体方法 |
| `services/participation_diagnostics.py` | 232 | D | 全文；第 1 轮 D08 跨界复核，纯读窗口与未知值；不以 execute/flush 名称推断 DB 写入，后轮核具体方法 |
| `services/participation_feedback.py` | 669 | D | 315-669；facts聚合、主库反馈提交、controller replay、跨库保存；其它入口索引；其余 facts/refs 查询调用链索引 |
| `services/participation_identity.py` | 28 | I | 仍有会话/授权 helper 或执行边界，不能按纯格式文件排除；`actor_identity:17` |
| `services/participation_ordinary.py` | 463 | I | 间接服务/调用链分类；不以 execute/flush 名称推断 DB 写入，后轮核具体方法 |
| `services/participation_parameters.py` | 13 | P | 结构/参数/数据模型/纯格式处理；不自管 SQLite 写事务（依赖调用者） |
| `services/participation_snapshot.py` | 96 | D | 全文；第 1 轮 D05 跨界复核，单专用线程/shield/close；不自管 SQLite 写事务（依赖调用者） |
| `services/participation_trace.py` | 91 | P | 结构/参数/数据模型/纯格式处理；不自管 SQLite 写事务（依赖调用者） |
| `services/plugin_events.py` | 64 | I | 间接服务/调用链分类；不以 execute/flush 名称推断 DB 写入，后轮核具体方法 |
| `services/plugin_sessions.py` | 356 | I | 间接服务/调用链分类；不以 execute/flush 名称推断 DB 写入，后轮核具体方法 |
| `services/policies.py` | 171 | I | 间接服务/调用链分类；不以 execute/flush 名称推断 DB 写入，后轮核具体方法 |
| `services/processing_status.py` | 148 | P | 结构/参数/数据模型/纯格式处理；不自管 SQLite 写事务（依赖调用者） |
| `services/processor.py` | 2111 | I | 间接服务/调用链分类；不以 execute/flush 名称推断 DB 写入，后轮核具体方法 |
| `services/profile_commands.py` | 137 | I | 间接服务/调用链分类；不以 execute/flush 名称推断 DB 写入，后轮核具体方法 |
| `services/prompt_composer.py` | 159 | P | 结构/参数/数据模型/纯格式处理；不自管 SQLite 写事务（依赖调用者） |
| `services/prompt_registry.py` | 102 | P | 结构/参数/数据模型/纯格式处理；不自管 SQLite 写事务（依赖调用者） |
| `services/qq_face_resolver.py` | 55 | P | 结构/参数/数据模型/纯格式处理；不自管 SQLite 写事务（依赖调用者） |
| `services/rate_limit.py` | 79 | P | 结构/参数/数据模型/纯格式处理；不自管 SQLite 写事务（依赖调用者） |
| `services/renderer.py` | 82 | P | 结构/参数/数据模型/纯格式处理；不自管 SQLite 写事务（依赖调用者） |
| `services/semantic_participation.py` | 1509 | D | 194-235、423-519；启停、controller锁、实际revision更新；其余调用入口索引；不以 execute/flush 名称推断 DB 写入，后轮核具体方法 |
| `services/subagent_execution.py` | 385 | I | 间接服务/调用链分类；不以 execute/flush 名称推断 DB 写入，后轮核具体方法 |
| `services/turn_coordinator.py` | 377 | I | 间接服务/调用链分类；不以 execute/flush 名称推断 DB 写入，后轮核具体方法 |
| `services/turn_execution.py` | 1660 | D | 1180-1323；其余 request/effect/save 边界索引；`compact_current:1017` |
| `services/turn_transcript.py` | 243 | I | 间接服务/调用链分类；不以 execute/flush 名称推断 DB 写入，后轮核具体方法 |
| `services/video_frames.py` | 142 | P | 结构/参数/数据模型/纯格式处理；不自管 SQLite 写事务（依赖调用者） |
| `services/vision_rate_limit.py` | 62 | P | 结构/参数/数据模型/纯格式处理；不自管 SQLite 写事务（依赖调用者） |
| `services/vision_service.py` | 609 | I | 间接服务/调用链分类；不以 execute/flush 名称推断 DB 写入，后轮核具体方法 |
| `services/work_resume.py` | 439 | I | 间接服务/调用链分类；不以 execute/flush 名称推断 DB 写入，后轮核具体方法 |
| `services/yuki_runtime.py` | 87 | I | 间接服务/调用链分类；不以 execute/flush 名称推断 DB 写入，后轮核具体方法 |
