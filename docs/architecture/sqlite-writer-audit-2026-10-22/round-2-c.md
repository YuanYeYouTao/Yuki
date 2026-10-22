# 第 2 轮 C 域：独立复核与补读

基线 `cdb49eadc0c7cd16060bf0329234ae69611e62bd`。本轮由 B 域第 1 轮 reviewer 轮转，独立核对 C 域 211 个文件的数据库实现、组合调用、外部效果与排除理由。阅读第 1 轮 C/D 报告和当前主任务书，不把其结论视作源码事实。只写本文，不修改产品代码，不运行测试、模型或生产操作。

## 增补与纠正

1. `runtime/lease_heartbeat.py:43` 仅凭 SQLite BUSY code 重试，没有检查 commit 阶段或 rollback/invalidation 注记；`work_session.py:1000` 只排除清理失败注记，未直接识别提交不明。W06 的错误阶段事实必须一路到达这两个现存循环，W27/V22/V38 不能只测试底层 callback 不重放。重购模型禁止与纯数据库已知失败恢复应分清。
2. `WorkRepository.record_effect:2648` 的 while 是原 receipt bytes 的 CAS 竞争重读，不是 SQLite 异常重试；它有意不要求当前 lease，记录迟到已发生事实。不得以统一 writer 为由删除其原 metadata 合并/accepted 不降级或给收尾加 lease 门槛。`resolve_run_effects:2534` 则仍核原 lease，分页只更新原 execution refs；二者职责不同。
3. `plugin_host/notification_delivery.py:504-528` 不是 Social 的三方原子结算：先 `_record_outbound` 自管 ScopedEvent UoW 提交，再 `finish_outbox` 单独 writer。失败后补 uncertain 和已知 message_id，不重发。W38/V44 需要明确正常本地 event+outbox 是否合一 writer，以及现有两个提交间崩溃的恢复；保留 source/privacy 不可留正文时的已发送事实。
4. Plugin `claim_outbox:684`、`claim_turn:834` 已在 writer 后重采 now，`validate_turn_attempt:947`、`require_outbox_ready:1145`、`require_agent_reply_ready:1158` 却在 writer 前取 now 后用作新 lease deadline。统一队列新增的等待会直接吃掉续租时间；按真实时钟区分固定 occurred/paid 时间、排队后接纳时刻与显式测试 now，不笼统修改所有 timestamp。`_owns_processing_attempt:1395` 仅核 status/attempt，现行语义不是“时间过了就拒绝迟到事实”；不要顺便给 finish_outbox 增加到期即拒存规则。
5. `plugin_host/media_artifacts.py:110` create 的 except Exception 无条件 unlink 原文件。若 commit 已成功但确认丢失，会有已提交 metadata 指向缺文件。W38/V34/V35 要覆盖按原 handle 查证提交/保留待清孤儿；只有确认未提交才删。不能将其原样迁入统一 writer 后宣称未知提交安全。其配额检查在页外 SELECT sum，统一提交排序不会自动让“sum+INSERT”原子化；是否保留并发配额语义应明示，不能声称队列已经解决。
6. `services/agent_tools.py:995` 通用 SQLAlchemyError 映射包含 memory_change，却统一返回 retryable=True/database_failure。W06/V22 需要追到工具结果与 typed effect outcome，不只 HTTP。`plugin_host/facades.py:2845` 则主要生成 audit category，不能把审计分类错误地当实际发送可重试判据。
7. `WorkSourceGuard.check:220/365` 对跨 Conversation client_input 核原事件 bytes 和实际 input 接纳；只读最终复核结束后，`WorkJournal.save:389` writer 核本 Conversation revision/generation，compact 另核 privacy，未直接携跨输入版本。W27/W28/V32/V38 应给跨输入源在 guard→writer 间变化的真实测试，并查清真实源写路径有无覆盖它的全局 privacy/revision。此处是明确验证缺口，未跑竞态，不宣称已经复现可见泄露；不能只测本会话主行改变，也不建议 writer 重扫整个 transcript。
8. R1 的 P 不能沿用为无数据库代码：`plugin_host/config.py` 的 set→Config.compare_and_set 是实际写；`services/commands/models.py` 是统计查询；`participation_trace.py`、`chat_preparation_timings.py` 委托诊断写；`social/confirmed_content.py` 是 writer 内来源资格查询；`activation_outcome.py` 决定数据库错误的恢复资格。`runtime/effect_schema.py`、`protocol_schema.py`、`work_budget_schema.py` 有 DDL/建表入口，归 Schema/离线，不归普通 runtime writer。
9. Work `commit_state:636` 不是一条状态 UPDATE：完整检查 mailbox/owned execution、更新 recovery、撤 wait、对子工作终态投 parent input。`work_management.stop_owned_execution:125` 递归撤整个真实子树；不能只迁 WorkRepository 的自管 begin，也不能用 bounded Work directory 代替全树事实。`SubagentRepository.finish:645` 原 notified_revision/结果/父 mailbox/wakeup 同事务；共享 writer 不替代实际完成通知。
10. `WorkRepository.prepare_input:1168` 的 before_publish 和 journal publication 已逐个追实际实参：ordinary publisher 使用 caller session；Main prepared publication 组合 projection+admission；public compact 回调由 prepare_commit 产生。没有发现这些现行实参在 writer 内调用模型、网关或文件锁。该结论只限已列现行回调，不能将公开异步 Callable 的类型本身当作短事务保证。
11. `WorkJournal.save:369` 的 input_media 提取在 writer 外；`:416` 的 previous_media 读取在 writer 内，且保留活动 Work 的老 refs，避免早准备删新输入。这一正确保留语义不应变为每次保存整批历史媒体重核。可比较直接批量 ON CONFLICT 与当前全旧 refs 差集的成本，按数据决定；不是必须新建媒体 Plan 或 ref revision。
12. Plugin KV facade 的 `_write_lock` 是每个 BoundStorageFacade 实例的业务锁、CAS 重试是版本竞争；不能见同一个 asyncio.Lock 或三次循环就统归数据库 BUSY 删除。插件 manager `_lock` 跨 register/start/stop hooks 和各次独立状态提交；必须保模型/插件外部代码在数据库事务外。统一数据库 owner 不让整个 PluginManager.start/stop 入队，更不在 writer 内等待它关 worker。
13. `services/admin/memory_admin.py:364/453` fallback 为写 audit 的 before 值先 list_person 再找单个 ID。若直接把整个旧 apply_evidence_write callback 套 IMMEDIATE，会在 writer 中查全部 target facts；可改为同 session 的定点事实/owner 查询，保原 audit 与 mutation 同提交。真实 mutation 路径的 audit 另一次提交是当前实现事实，不能笼统说所有 add/update/delete 都与 audit 原子。

## 锁序、外部效果与 D 域复核

- Protocol 文件 fence→读准备退出→writer→refs/journal commit 已复核；GC 的 writer 标记→退出 writer→protocol fence/真实线程 unlink→解锁→确认 writer 没有反向边。`_finish_thread:62` 等实际线程退出，是文件 fence 的必要语义；数据库公共取消收尾不能冒充这份文件线程收拢。
- Artifact/媒体原件的 metadata、Protocol refs、Work budget 与 effects 都属于业务事实；诊断 payload 队列只承载可丢诊断。`DiagnosticWrite` 只需收口 SQL commit，已有 TraceRecorder 编码线程和资源归属仍在 writer 外。
- Manager `jobs.sqlite3` 与 Bot 主库是不同进程/文件；全文核了 persistent、ACPResources、CompletionOutbox。所有 DML 在同步 with self.db 内结算，constructor DDL/executescript 后显式 commit；未发现跨 await 的隐式 DML 事务。文件 RPC/post_request、Docker/execd、文件 copy 的“intent→外部→result”各自仍有恢复事实，不能为统一主库队列搬成跨进程事务。同步大 SQL/JSON 可阻塞 Manager 自身事件循环，但本轮不凭此要求新 daemon。
- Bot tasks.receive 的 completion+continuation 同提交，receive_permission 的原 task/input/wait 同提交，然后 receiver 才 ACK Manager/cursor；毒前缀按原页游标复访，不以读页就 ACK。提交确认不明时不 ACK，让原事件再次被幂等 receive；不能再提交一次远程命令。
- D05 的单专用 SnapshotStore 线程范围成立；`_save:488` 仍须先认领实际 revision/item.saved 再传播取消。主库 `_commit_pending:516` 先提交原 sequence/refs，独立 snapshot 后追赶；D02 的逆序生命周期本身不证明失败分支下 driver 退出。与 R1 C 对 D 的这些纠正一致，本轮独立读对应实现后未发现相反证据。
- `_read_facts:349` 是原 run 的完整 Work/后代计费、Social/caption、tool refs、durable feedback 集合；每页只写最多 64 refs 不意味着可只读 64 refs。`_scope_social_rows:172` 的 600 秒/2048 限制是另一条实时观测路径，不能拿来代替完整反馈证据。W35 移准备时必须保原选择/消费序列与“后来新增事实留待下一页”，而非冻结任何新效果都拒绝收尾。

## 旧机制删除与性能边界

W25–W41 应覆盖本表所有自管方法、supplied-session helper 和被误排的委托者；共享 session 的方法不再排第二次 writer。普通单键状态/CAS 更新直接短写，较大 JSON、全文历史、媒体字节、分词、模型与文件准备在外；固定小 payload 编码可以在真实规模对照后决定，不为每个字段增加一层 DTO。

可以删除旧写事务壳、首写抢锁同值 UPDATE（当其唯一职责已由 IMMEDIATE 取代）、安全回滚后 BUSY 争抢的重复准备。必须保同事务实际 lease/owner/source/CAS，原 intent/dispatch/result、root budget、paid response、未知结果和 domain receipt；不是删所有 while、所有状态或所有 source guards。Work 的 `_assert_lease` 若删除同值写，应换成当前 writer 内相同真实谓词核验/并入实际 DML，不能只沿用排队前 valid()。

只读 Work 查询、current ACL、历史、向量和 health 继续并行；没有变化的 acquire/renew/release、duplicate enqueue、空维护保只读快路。后台全树归档、Plugin 媒体 cleanup 的全部候选、refs 与 JSON、trace 满负载是规模场景，候选 16/128 不足以证明每次占用小。比较 arrival→response、admission wait、writer hold、只读吞吐和 SQL/字节量；不制造新的固定 p95/SQL 毫秒门槛。

## 测试阅读与明确缺口

本轮全文阅读 `tests/unit/test_plugin_media_cleanup_recovery.py`、`tests/integration/test_invocation_crash_windows.py`、`test_invocation_process_crash.py`，未执行。前者覆盖 unlink 失败/取消、超过 128 后续原件、pending/processing outbox 保留与自然到期读；**不覆盖 create commit ACK 丢失后误删文件**。后两者用真实隔离 SQLite 和独立下游日志验证 T1/T2/T3、原 budget、取消后 late receipt、CAS 并发、进程硬退出无重发，不能把静态阅读写成新 FIFO 已通过。

R3/实施验证接力：`test_work_settlement_writer.py`、`test_work_delivery_ownership.py`、`test_work_native_media_receipts.py`、`test_plugin_notification_idempotency.py` 的全函数覆盖；补真实 driver/外部 WAL BUSY 的 commit/cleanup 阶段传递、notification event→outbox 中间崩溃、已知 message_id 的 uncertain、Plugin create 原文件、跨入口 source guard→writer、writer 等待跨 lease expiry。旧测试听 BEGIN IMMEDIATE 的 barrier 与固定 retry/session 次数需要改行为注入点，不删保护的原业务事实。

## 覆核记录

下表逐项登记 211 个文件。F=本轮全文；M=完整核读实际 DB 方法/接口与所列调用，未将其它业务分支计为全文；I=独立复核导入/实际委托关系，无自管 writer，仍由目标 owner 的方法审查承担；S=结构/纯处理排除，独立核了函数调用面，不冒称整文件逐字阅读。I/S 的结论不是“从未访问数据库”，实际读/诊断/Schema 等已单列。

收束时远端 main 已前进到 `c2ce7da66ab54efe192993b9615df48266a62137`；本文源码结论仍只认 cdb49ead。参与稀疏化涉及的源码与依赖 pin 变更由后续 delta 单独审查，不混记为旧基线全文。`participation_ordinary.py` 后半段恰在 ff 后读取，因此不计旧基线全文，表内明确保留旧范围与 delta 接力。


### 逐文件复核表（旧基线）

| 文件（src/yuki/ 下） | 旧基线行数 | 第 2 轮 | 范围与职责 |
|---|---:|---|---|
| `runtime/__init__.py` | 26 | S | 独立核导入、函数声明与调用面：类型/表结构/编码/格式/文件或配置处理，无自管 runtime SQLite writer；非全文业务验收 |
| `runtime/activation_bindings.py` | 62 | S | 独立核导入、函数声明与调用面：类型/表结构/编码/格式/文件或配置处理，无自管 runtime SQLite writer；非全文业务验收 |
| `runtime/activation_outcome.py` | 171 | F | 全文；真实事务/委托、来源与外部效果按上文及方法表复核 |
| `runtime/activation_tasks.py` | 67 | I | 接口/调用面复核：本模块现有执行/服务接口；无本轮新增迁移 owner，未全文，不据此排除相关 DB 调用 |
| `runtime/context_preparation.py` | 116 | F | 全文；真实事务/委托、来源与外部效果按上文及方法表复核 |
| `runtime/contracts.py` | 27 | S | 独立核导入、函数声明与调用面：类型/表结构/编码/格式/文件或配置处理，无自管 runtime SQLite writer；非全文业务验收 |
| `runtime/delivery.py` | 14 | I | 接口/调用面复核：本模块现有执行/服务接口；无本轮新增迁移 owner，未全文，不据此排除相关 DB 调用 |
| `runtime/delivery_intents.py` | 124 | F | 全文；真实事务/委托、来源与外部效果按上文及方法表复核 |
| `runtime/effect_outcomes.py` | 421 | I | 接口/调用面复核：capabilities.results；无本轮新增迁移 owner，未全文，不据此排除相关 DB 调用 |
| `runtime/effect_queries.py` | 75 | F | 全文；真实事务/委托、来源与外部效果按上文及方法表复核 |
| `runtime/effect_schema.py` | 45 | S | 全文；install_indexes 实际 DDL，走离线 Schema owner |
| `runtime/errors.py` | 15 | S | 独立核导入、函数声明与调用面：类型/表结构/编码/格式/文件或配置处理，无自管 runtime SQLite writer；非全文业务验收 |
| `runtime/execution_receipts.py` | 27 | I | 接口/调用面复核：sandbox.continuations；无本轮新增迁移 owner，未全文，不据此排除相关 DB 调用 |
| `runtime/lease_heartbeat.py` | 119 | F | 全文；真实事务/委托、来源与外部效果按上文及方法表复核 |
| `runtime/observability.py` | 77 | I | 接口/调用面复核：runtime.origin；无本轮新增迁移 owner，未全文，不据此排除相关 DB 调用 |
| `runtime/origin.py` | 28 | S | 独立核导入、函数声明与调用面：类型/表结构/编码/格式/文件或配置处理，无自管 runtime SQLite writer；非全文业务验收 |
| `runtime/principal.py` | 25 | S | 独立核导入、函数声明与调用面：类型/表结构/编码/格式/文件或配置处理，无自管 runtime SQLite writer；非全文业务验收 |
| `runtime/protocol_schema.py` | 49 | S | 全文；install_quota 实际 INSERT/trigger DDL，走 Schema owner |
| `runtime/protocol_store.py` | 677 | F | 全文；真实事务/委托、来源与外部效果按上文及方法表复核 |
| `runtime/subagent_repository.py` | 725 | F | 全文；真实事务/委托、来源与外部效果按上文及方法表复核 |
| `runtime/subagent_scheduler.py` | 137 | I | 接口/调用面复核：runtime.subagent_repository、runtime.subagent_schema、runtime.work_recovery_schema、runtime.work_repository；无本轮新增迁移 owner，未全文，不据此排除相关 DB 调用 |
| `runtime/subagent_schema.py` | 42 | S | 独立核导入、函数声明与调用面：类型/表结构/编码/格式/文件或配置处理，无自管 runtime SQLite writer；非全文业务验收 |
| `runtime/subagent_tools.py` | 155 | I | 接口/调用面复核：runtime.subagent_repository、runtime.work_queries、sandbox.environment_tools；无本轮新增迁移 owner，未全文，不据此排除相关 DB 调用 |
| `runtime/trigger.py` | 104 | I | 接口/调用面复核：runtime.errors、runtime.origin；无本轮新增迁移 owner，未全文，不据此排除相关 DB 调用 |
| `runtime/work_activation.py` | 206 | F | 全文；真实事务/委托、来源与外部效果按上文及方法表复核 |
| `runtime/work_budget.py` | 64 | F | 全文；真实事务/委托、来源与外部效果按上文及方法表复核 |
| `runtime/work_budget_schema.py` | 21 | S | 结构+create_current_budget_tables 调用面；真实建表，不归普通业务 writer |
| `runtime/work_context_note.py` | 190 | F | 全文；真实事务/委托、来源与外部效果按上文及方法表复核 |
| `runtime/work_control.py` | 1132 | F | 全文；真实事务/委托、来源与外部效果按上文及方法表复核 |
| `runtime/work_journal.py` | 757 | F | 全文；真实事务/委托、来源与外部效果按上文及方法表复核 |
| `runtime/work_management.py` | 356 | F | 全文；真实事务/委托、来源与外部效果按上文及方法表复核 |
| `runtime/work_media.py` | 43 | F | 全文；真实事务/委托、来源与外部效果按上文及方法表复核 |
| `runtime/work_observation_schema.py` | 10 | S | 独立核导入、函数声明与调用面：类型/表结构/编码/格式/文件或配置处理，无自管 runtime SQLite writer；非全文业务验收 |
| `runtime/work_queries.py` | 234 | I | 真实纯读 DB 查询；本轮复核 Work/owner/目录接口，未逐方法全文，R3 补；不得据此计为完整 DB 实现已审 |
| `runtime/work_query_schema.py` | 89 | S | 独立核导入、函数声明与调用面：类型/表结构/编码/格式/文件或配置处理，无自管 runtime SQLite writer；非全文业务验收 |
| `runtime/work_recovery_schema.py` | 44 | S | 独立核导入、函数声明与调用面：类型/表结构/编码/格式/文件或配置处理，无自管 runtime SQLite writer；非全文业务验收 |
| `runtime/work_repository.py` | 2781 | F | 全文；真实事务/委托、来源与外部效果按上文及方法表复核 |
| `runtime/work_scheduler.py` | 358 | M | 231-358；完整候选选择/实际维护委托；health 104-120 仅入口复核，R3 补完整查询 |
| `runtime/work_schema_v1.py` | 120 | S | 独立核导入、函数声明与调用面：类型/表结构/编码/格式/文件或配置处理，无自管 runtime SQLite writer；非全文业务验收 |
| `runtime/work_session.py` | 1055 | F | 全文；真实事务/委托、来源与外部效果按上文及方法表复核 |
| `runtime/work_source_guard.py` | 391 | F | 全文；真实事务/委托、来源与外部效果按上文及方法表复核 |
| `runtime/work_supervisor.py` | 267 | F | 全文；真实事务/委托、来源与外部效果按上文及方法表复核 |
| `runtime/work_tree.py` | 69 | F | 全文；真实事务/委托、来源与外部效果按上文及方法表复核 |
| `runtime/work_wait.py` | 1016 | F | 全文；真实事务/委托、来源与外部效果按上文及方法表复核 |
| `runtime/work_wait_schema.py` | 48 | S | 独立核导入、函数声明与调用面：类型/表结构/编码/格式/文件或配置处理，无自管 runtime SQLite writer；非全文业务验收 |
| `model_runtime/__init__.py` | 63 | S | 独立核导入、函数声明与调用面：类型/表结构/编码/格式/文件或配置处理，无自管 runtime SQLite writer；非全文业务验收 |
| `model_runtime/capacity.py` | 264 | S | 独立核导入、函数声明与调用面：类型/表结构/编码/格式/文件或配置处理，无自管 runtime SQLite writer；非全文业务验收 |
| `model_runtime/db_models.py` | 61 | S | 独立核导入、函数声明与调用面：类型/表结构/编码/格式/文件或配置处理，无自管 runtime SQLite writer；非全文业务验收 |
| `model_runtime/dispatch_guard.py` | 26 | F | 全文；真实事务/委托、来源与外部效果按上文及方法表复核 |
| `model_runtime/executor.py` | 965 | M | 363-639、929-965；完整 _execute/_record_invocation 和关闭；其余 Provider 名额调度不作 DB writer |
| `model_runtime/models.py` | 214 | S | 独立核导入、函数声明与调用面：类型/表结构/编码/格式/文件或配置处理，无自管 runtime SQLite writer；非全文业务验收 |
| `model_runtime/pool.py` | 172 | I | 接口/调用面复核：model_runtime.models；无本轮新增迁移 owner，未全文，不据此排除相关 DB 调用 |
| `model_runtime/profiles.py` | 240 | S | 独立核导入、函数声明与调用面：类型/表结构/编码/格式/文件或配置处理，无自管 runtime SQLite writer；非全文业务验收 |
| `model_runtime/repository.py` | 274 | F | 全文；真实事务/委托、来源与外部效果按上文及方法表复核 |
| `model_runtime/request_accounting.py` | 92 | F | 全文；真实事务/委托、来源与外部效果按上文及方法表复核 |
| `model_runtime/routes.py` | 32 | S | 独立核导入、函数声明与调用面：类型/表结构/编码/格式/文件或配置处理，无自管 runtime SQLite writer；非全文业务验收 |
| `model_runtime/search_defaults.py` | 110 | S | 独立核导入、函数声明与调用面：类型/表结构/编码/格式/文件或配置处理，无自管 runtime SQLite writer；非全文业务验收 |
| `model_runtime/secrets.py` | 80 | S | 独立核导入、函数声明与调用面：类型/表结构/编码/格式/文件或配置处理，无自管 runtime SQLite writer；非全文业务验收 |
| `model_runtime/structured.py` | 393 | I | 接口/调用面复核：model_runtime.executor、model_runtime.models、runtime.work_activation；无本轮新增迁移 owner，未全文，不据此排除相关 DB 调用 |
| `execution_trace/__init__.py` | 1 | S | 独立核导入、函数声明与调用面：类型/表结构/编码/格式/文件或配置处理，无自管 runtime SQLite writer；非全文业务验收 |
| `execution_trace/db_models.py` | 63 | S | 独立核导入、函数声明与调用面：类型/表结构/编码/格式/文件或配置处理，无自管 runtime SQLite writer；非全文业务验收 |
| `execution_trace/payload.py` | 184 | S | 独立核导入、函数声明与调用面：类型/表结构/编码/格式/文件或配置处理，无自管 runtime SQLite writer；非全文业务验收 |
| `execution_trace/phases.py` | 150 | S | 独立核导入、函数声明与调用面：类型/表结构/编码/格式/文件或配置处理，无自管 runtime SQLite writer；非全文业务验收 |
| `execution_trace/recorder.py` | 521 | F | 全文；真实事务/委托、来源与外部效果按上文及方法表复核 |
| `social/__init__.py` | 1 | S | 独立核导入、函数声明与调用面：类型/表结构/编码/格式/文件或配置处理，无自管 runtime SQLite writer；非全文业务验收 |
| `social/agent_adapter.py` | 216 | I | 接口/调用面复核：capabilities.invocation、conversation.canonical_db_models、identity.person_identity、identity.routing；无本轮新增迁移 owner，未全文，不据此排除相关 DB 调用 |
| `social/confirmed_content.py` | 35 | F | 全文；真实事务/委托、来源与外部效果按上文及方法表复核 |
| `social/content.py` | 331 | I | 接口/调用面复核：gateway.social；无本轮新增迁移 owner，未全文，不据此排除相关 DB 调用 |
| `social/db_models.py` | 89 | S | 独立核导入、函数声明与调用面：类型/表结构/编码/格式/文件或配置处理，无自管 runtime SQLite writer；非全文业务验收 |
| `social/emoji_intents.py` | 376 | F | 全文；真实事务/委托、来源与外部效果按上文及方法表复核 |
| `social/history.py` | 386 | I | 接口/调用面复核：capabilities.invocation、conversation.canonical_db_models、execution_trace.db_models、identity.db_models；无本轮新增迁移 owner，未全文，不据此排除相关 DB 调用 |
| `social/models.py` | 81 | S | 独立核导入、函数声明与调用面：类型/表结构/编码/格式/文件或配置处理，无自管 runtime SQLite writer；非全文业务验收 |
| `social/repository.py` | 709 | F | 全文；真实事务/委托、来源与外部效果按上文及方法表复核 |
| `social/service.py` | 1811 | M | 326-390、611-780、941-1080、1296-1631、1651-1809；真实 claim/dispatch/settle/fallback 全读，其余 target/contacts/权限读未全文，R3 补 |
| `social/source_keys.py` | 22 | S | 独立核导入、函数声明与调用面：类型/表结构/编码/格式/文件或配置处理，无自管 runtime SQLite writer；非全文业务验收 |
| `social/tools.py` | 167 | S | 独立核导入、函数声明与调用面：类型/表结构/编码/格式/文件或配置处理，无自管 runtime SQLite writer；非全文业务验收 |
| `plugin_host/__init__.py` | 5 | S | 独立核导入、函数声明与调用面：类型/表结构/编码/格式/文件或配置处理，无自管 runtime SQLite writer；非全文业务验收 |
| `plugin_host/admission_adapter.py` | 172 | I | 接口/调用面复核：admin.models、automation.models、conversation.scope、plugin_host.canonical_projection；无本轮新增迁移 owner，未全文，不据此排除相关 DB 调用 |
| `plugin_host/approval.py` | 66 | I | 接口/调用面复核：plugin_host.manifest、plugin_host.models；无本轮新增迁移 owner，未全文，不据此排除相关 DB 调用 |
| `plugin_host/audit.py` | 42 | F | 全文；真实事务/委托、来源与外部效果按上文及方法表复核 |
| `plugin_host/background_turns.py` | 456 | F | 全文；真实事务/委托、来源与外部效果按上文及方法表复核 |
| `plugin_host/canonical_projection.py` | 57 | I | 接口/调用面复核：persistence.repository_records；无本轮新增迁移 owner，未全文，不据此排除相关 DB 调用 |
| `plugin_host/capability_adapter.py` | 375 | I | 接口/调用面复核：automation.authority、capabilities.results、identity.authority、plugin_host.audit；无本轮新增迁移 owner，未全文，不据此排除相关 DB 调用 |
| `plugin_host/command_adapter.py` | 206 | I | 接口/调用面复核：admin.control_resolution、admin.models、automation.models、conversation.scope；无本轮新增迁移 owner，未全文，不据此排除相关 DB 调用 |
| `plugin_host/config.py` | 128 | F | 全文；真实事务/委托、来源与外部效果按上文及方法表复核 |
| `plugin_host/configuration_service.py` | 210 | F | 全文；真实事务/委托、来源与外部效果按上文及方法表复核 |
| `plugin_host/db_models.py` | 635 | S | 独立核导入、函数声明与调用面：类型/表结构/编码/格式/文件或配置处理，无自管 runtime SQLite writer；非全文业务验收 |
| `plugin_host/direct_command_router.py` | 91 | I | 接口/调用面复核：plugin_host.extension_registry、settings_domains；无本轮新增迁移 owner，未全文，不据此排除相关 DB 调用 |
| `plugin_host/direct_delivery.py` | 163 | F | 全文；真实事务/委托、来源与外部效果按上文及方法表复核 |
| `plugin_host/discovery.py` | 87 | I | 接口/调用面复核：plugin_host.manifest、plugin_host.models；无本轮新增迁移 owner，未全文，不据此排除相关 DB 调用 |
| `plugin_host/event_bus.py` | 297 | I | 接口/调用面复核：本模块现有执行/服务接口；无本轮新增迁移 owner，未全文，不据此排除相关 DB 调用 |
| `plugin_host/extension_registry.py` | 283 | I | 接口/调用面复核：本模块现有执行/服务接口；无本轮新增迁移 owner，未全文，不据此排除相关 DB 调用 |
| `plugin_host/facades.py` | 3260 | M | 615-969、1189-1521、1931-2036、2426-2716、2837-2858、3068-3134；实际 SQL/Memory/SDK 发送与审计委托；其它 facade 仍接口级 |
| `plugin_host/http_client.py` | 314 | I | 接口/调用面复核：services.http_logging；无本轮新增迁移 owner，未全文，不据此排除相关 DB 调用 |
| `plugin_host/loader.py` | 86 | I | 接口/调用面复核：plugin_host.manifest；无本轮新增迁移 owner，未全文，不据此排除相关 DB 调用 |
| `plugin_host/main_turn.py` | 714 | M | 346-457、586-626；Work admit/resume 及 source 读；R1 已读 198-345 尚未在本轮独立全文，列 R3 接力 |
| `plugin_host/manager.py` | 876 | F | 全文；真实事务/委托、来源与外部效果按上文及方法表复核 |
| `plugin_host/manifest.py` | 239 | S | 独立核导入、函数声明与调用面：类型/表结构/编码/格式/文件或配置处理，无自管 runtime SQLite writer；非全文业务验收 |
| `plugin_host/media_artifacts.py` | 263 | F | 全文；真实事务/委托、来源与外部效果按上文及方法表复核 |
| `plugin_host/models.py` | 42 | S | 独立核导入、函数声明与调用面：类型/表结构/编码/格式/文件或配置处理，无自管 runtime SQLite writer；非全文业务验收 |
| `plugin_host/notification_delivery.py` | 633 | F | 全文；真实事务/委托、来源与外部效果按上文及方法表复核 |
| `plugin_host/notification_manifest.py` | 109 | S | 独立核导入、函数声明与调用面：类型/表结构/编码/格式/文件或配置处理，无自管 runtime SQLite writer；非全文业务验收 |
| `plugin_host/notification_repository.py` | 2231 | F | 全文；真实事务/委托、来源与外部效果按上文及方法表复核 |
| `plugin_host/ownership.py` | 517 | F | 全文；真实事务/委托、来源与外部效果按上文及方法表复核 |
| `plugin_host/prompt_adapter.py` | 52 | I | 接口/调用面复核：plugin_host.extension_registry、services.prompt_registry；无本轮新增迁移 owner，未全文，不据此排除相关 DB 调用 |
| `plugin_host/repository.py` | 942 | F | 全文；真实事务/委托、来源与外部效果按上文及方法表复核 |
| `plugin_host/secrets.py` | 48 | S | 独立核导入、函数声明与调用面：类型/表结构/编码/格式/文件或配置处理，无自管 runtime SQLite writer；非全文业务验收 |
| `plugin_host/session_facade.py` | 93 | F | 全文；真实事务/委托、来源与外部效果按上文及方法表复核 |
| `plugin_host/session_repository.py` | 493 | F | 全文；真实事务/委托、来源与外部效果按上文及方法表复核 |
| `plugin_host/storage.py` | 154 | F | 全文；真实事务/委托、来源与外部效果按上文及方法表复核 |
| `sandbox/__init__.py` | 1 | S | 独立核导入、函数声明与调用面：类型/表结构/编码/格式/文件或配置处理，无自管 runtime SQLite writer；非全文业务验收 |
| `sandbox/acp_config.py` | 73 | S | 独立核导入、函数声明与调用面：类型/表结构/编码/格式/文件或配置处理，无自管 runtime SQLite writer；非全文业务验收 |
| `sandbox/acp_manager.py` | 477 | F | 全文；真实事务/委托、来源与外部效果按上文及方法表复核 |
| `sandbox/acp_peer.py` | 636 | I | 接口/调用面复核：本模块现有执行/服务接口；无本轮新增迁移 owner，未全文，不据此排除相关 DB 调用 |
| `sandbox/client.py` | 217 | I | 接口/调用面复核：runtime.execution_receipts、runtime.work_activation、sandbox.acp_peer、sandbox.environment_tools；无本轮新增迁移 owner，未全文，不据此排除相关 DB 调用 |
| `sandbox/completion_receiver.py` | 140 | F | 全文；真实事务/委托、来源与外部效果按上文及方法表复核 |
| `sandbox/completions.py` | 54 | F | 全文；真实事务/委托、来源与外部效果按上文及方法表复核 |
| `sandbox/continuation_worker.py` | 62 | I | 接口/调用面复核：runtime.work_repository、sandbox.continuations；无本轮新增迁移 owner，未全文，不据此排除相关 DB 调用 |
| `sandbox/continuations.py` | 51 | F | 全文；真实事务/委托、来源与外部效果按上文及方法表复核 |
| `sandbox/control_completions.py` | 94 | F | 全文；真实事务/委托、来源与外部效果按上文及方法表复核 |
| `sandbox/db_models.py` | 58 | S | 独立核导入、函数声明与调用面：类型/表结构/编码/格式/文件或配置处理，无自管 runtime SQLite writer；非全文业务验收 |
| `sandbox/environment_supervisor.py` | 198 | I | 接口/调用面复核：本模块现有执行/服务接口；无本轮新增迁移 owner，未全文，不据此排除相关 DB 调用 |
| `sandbox/environment_tools.py` | 138 | I | 接口/调用面复核：本模块现有执行/服务接口；无本轮新增迁移 owner，未全文，不据此排除相关 DB 调用 |
| `sandbox/execd.py` | 48 | I | 接口/调用面复核：本模块现有执行/服务接口；无本轮新增迁移 owner，未全文，不据此排除相关 DB 调用 |
| `sandbox/external_agents.py` | 252 | I | 接口/调用面复核：runtime.work_activation、runtime.work_queries、sandbox.client；无本轮新增迁移 owner，未全文，不据此排除相关 DB 调用 |
| `sandbox/manager.py` | 90 | I | 接口/调用面复核：sandbox.persistent、workspace.store；无本轮新增迁移 owner，未全文，不据此排除相关 DB 调用 |
| `sandbox/persistent.py` | 1275 | F | 全文；真实事务/委托、来源与外部效果按上文及方法表复核 |
| `sandbox/resource_cleanup.py` | 144 | F | 全文；真实事务/委托、来源与外部效果按上文及方法表复核 |
| `sandbox/task_repository.py` | 324 | F | 全文；真实事务/委托、来源与外部效果按上文及方法表复核 |
| `services/__init__.py` | 29 | S | 独立核导入、函数声明与调用面：类型/表结构/编码/格式/文件或配置处理，无自管 runtime SQLite writer；非全文业务验收 |
| `services/admin/__init__.py` | 13 | S | 独立核导入、函数声明与调用面：类型/表结构/编码/格式/文件或配置处理，无自管 runtime SQLite writer；非全文业务验收 |
| `services/admin/config_admin.py` | 108 | I | 接口/调用面复核：admin.config_service、admin.models；无本轮新增迁移 owner，未全文，不据此排除相关 DB 调用 |
| `services/admin/control_auth.py` | 73 | I | 接口/调用面复核：admin.authority.decision、admin.authority.principal、admin.authority.targets；无本轮新增迁移 owner，未全文，不据此排除相关 DB 调用 |
| `services/admin/group_admin.py` | 84 | I | 接口/调用面复核：admin.audit、admin.authority.principal、admin.authority.targets、admin.models；无本轮新增迁移 owner，未全文，不据此排除相关 DB 调用 |
| `services/admin/group_recovery.py` | 379 | F | 全文；真实事务/委托、来源与外部效果按上文及方法表复核 |
| `services/admin/memory_admin.py` | 1208 | M | 232-489；add/update/delete 完整事务回调与 mutation/audit 分提交；其它管理接口委托 B，R3 补授权 helper/治理入口 |
| `services/admin/preference_admin.py` | 167 | I | 接口/调用面复核：admin.audit、admin.authority.principal、admin.authority.targets、admin.models；无本轮新增迁移 owner，未全文，不据此排除相关 DB 调用 |
| `services/agent_runner.py` | 1106 | I | Runner→WorkSession/InvocationService/ModelExecutor 调用链；无自管连接；模型执行循环未全文 |
| `services/agent_tools.py` | 2227 | M | 755-1016、1376-1431、1845-1860；真实 SQL 读、Sandbox/Workspace/Social/Memory dispatch 和 SQLAlchemyError 结果映射 |
| `services/automation_commands.py` | 121 | I | 接口/调用面复核：automation.service、conversation.scope、time.formatting；无本轮新增迁移 owner，未全文，不据此排除相关 DB 调用 |
| `services/autonomous_groups.py` | 326 | I | 接口/调用面复核：admin.config_service、admin.models、automation.models、conversation.features；无本轮新增迁移 owner，未全文，不据此排除相关 DB 调用 |
| `services/chat.py` | 1685 | M | 566-659、1251-1296；全部 before_publish 实参/原 source 读；其余大业务分支不算全文 |
| `services/chat_preparation_timings.py` | 110 | F | 全文；真实事务/委托、来源与外部效果按上文及方法表复核 |
| `services/client_link.py` | 104 | F | 全文；真实事务/委托、来源与外部效果按上文及方法表复核 |
| `services/command_service.py` | 231 | I | 接口/调用面复核：admin.config_service、admin.control_resolution、admin.models、admin.operations；无本轮新增迁移 owner，未全文，不据此排除相关 DB 调用 |
| `services/commands/__init__.py` | 1 | S | 独立核导入、函数声明与调用面：类型/表结构/编码/格式/文件或配置处理，无自管 runtime SQLite writer；非全文业务验收 |
| `services/commands/admin.py` | 154 | I | 接口/调用面复核：admin.authority.command_types、admin.authority.commands、admin.authority.json_types、admin.authority.principal；无本轮新增迁移 owner，未全文，不据此排除相关 DB 调用 |
| `services/commands/conversation.py` | 117 | I | 接口/调用面复核：admin.config_service、automation.repository、automation.worker、conversation.projection_models；无本轮新增迁移 owner，未全文，不据此排除相关 DB 调用 |
| `services/commands/group.py` | 14 | I | 接口/调用面复核：本模块现有执行/服务接口；无本轮新增迁移 owner，未全文，不据此排除相关 DB 调用 |
| `services/commands/help.py` | 153 | I | 接口/调用面复核：本模块现有执行/服务接口；无本轮新增迁移 owner，未全文，不据此排除相关 DB 调用 |
| `services/commands/identity.py` | 216 | I | 接口/调用面复核：admin.authority.principal、admin.operations、admin.queries、services.commands.help；无本轮新增迁移 owner，未全文，不据此排除相关 DB 调用 |
| `services/commands/memory.py` | 564 | I | 接口/调用面复核：admin.models、memory.rebuild.models、memory.rebuild.service、memory.self_reflection.reporting；无本轮新增迁移 owner，未全文，不据此排除相关 DB 调用 |
| `services/commands/models.py` | 46 | F | 全文；真实事务/委托、来源与外部效果按上文及方法表复核 |
| `services/commands/plugins.py` | 81 | I | 接口/调用面复核：admin.authority.principal、admin.operations、admin.queries、services.commands.help；无本轮新增迁移 owner，未全文，不据此排除相关 DB 调用 |
| `services/commands/work.py` | 49 | I | 接口/调用面复核：admin.authority.principal、admin.operations、admin.queries、services.commands.help；无本轮新增迁移 owner，未全文，不据此排除相关 DB 调用 |
| `services/concurrency.py` | 95 | I | 接口/调用面复核：本模块现有执行/服务接口；无本轮新增迁移 owner，未全文，不据此排除相关 DB 调用 |
| `services/config_commands.py` | 286 | I | 接口/调用面复核：admin.authority.commands、admin.authority.principal、admin.models、admin.operations；无本轮新增迁移 owner，未全文，不据此排除相关 DB 调用 |
| `services/context_assembler.py` | 501 | I | 接口/调用面复核：admin.models、conversation.frozen_fragments、conversation.projections、conversation.prompt_history；无本轮新增迁移 owner，未全文，不据此排除相关 DB 调用 |
| `services/context_boundary.py` | 55 | F | 全文；真实事务/委托、来源与外部效果按上文及方法表复核 |
| `services/context_compaction.py` | 652 | M | 1-340；CompactReceipt read/write、current_public_compact/public_compact_publication 完整；剩余纯容量/格式算法结构排除 |
| `services/deduplication.py` | 14 | S | 独立核导入、函数声明与调用面：类型/表结构/编码/格式/文件或配置处理，无自管 runtime SQLite writer；非全文业务验收 |
| `services/document_reader.py` | 214 | S | 独立核导入、函数声明与调用面：类型/表结构/编码/格式/文件或配置处理，无自管 runtime SQLite writer；非全文业务验收 |
| `services/durable_invocations.py` | 299 | F | 全文；真实事务/委托、来源与外部效果按上文及方法表复核 |
| `services/effect_gate.py` | 65 | I | 接口/调用面复核：conversation.scope；无本轮新增迁移 owner，未全文，不据此排除相关 DB 调用 |
| `services/evidence_observation.py` | 92 | I | 接口/调用面复核：runtime.observability；无本轮新增迁移 owner，未全文，不据此排除相关 DB 调用 |
| `services/evidence_state.py` | 39 | I | 接口/调用面复核：本模块现有执行/服务接口；无本轮新增迁移 owner，未全文，不据此排除相关 DB 调用 |
| `services/execution_sources.py` | 599 | M | 226-469、509-599；recover background/automation/SELF/message 全部实际 DB 函数；原数据模型和纯转换未全文 |
| `services/group_recovery.py` | 131 | F | 全文；真实事务/委托、来源与外部效果按上文及方法表复核 |
| `services/history_projection.py` | 221 | I | 接口/调用面复核：conversation.frozen_fragments、conversation.projections、event_prompt、execution_trace.phases；无本轮新增迁移 owner，未全文，不据此排除相关 DB 调用 |
| `services/history_query.py` | 73 | I | 接口/调用面复核：本模块现有执行/服务接口；无本轮新增迁移 owner，未全文，不据此排除相关 DB 调用 |
| `services/http_logging.py` | 40 | S | 独立核导入、函数声明与调用面：类型/表结构/编码/格式/文件或配置处理，无自管 runtime SQLite writer；非全文业务验收 |
| `services/image_preprocessor.py` | 212 | I | 接口/调用面复核：vision.models；无本轮新增迁移 owner，未全文，不据此排除相关 DB 调用 |
| `services/invocation_context.py` | 54 | F | 全文；真实事务/委托、来源与外部效果按上文及方法表复核 |
| `services/invocation_service.py` | 328 | M | 76-328；完整 invoke：T1→T2→外部→T3、typed outcome/late receipt/audits；其余构造与纯预算分类接口 |
| `services/main_agent_backend.py` | 744 | I | 工具、实际 effect/结果委托；无自管连接，完整调用分支留 R3 与新 main delta |
| `services/main_agent_contract.py` | 153 | F | 全文；真实事务/委托、来源与外部效果按上文及方法表复核 |
| `services/main_agent_turns.py` | 664 | M | 176-283、369-510、569-664；全部真实 publication callbacks、独立 writer 与原结果读取 |
| `services/media_process.py` | 25 | I | 接口/调用面复核：本模块现有执行/服务接口；无本轮新增迁移 owner，未全文，不据此排除相关 DB 调用 |
| `services/media_resolver.py` | 625 | I | 接口/调用面复核：gateway.media、vision.models；无本轮新增迁移 owner，未全文，不据此排除相关 DB 调用 |
| `services/message_splitter.py` | 358 | S | 独立核导入、函数声明与调用面：类型/表结构/编码/格式/文件或配置处理，无自管 runtime SQLite writer；非全文业务验收 |
| `services/model_context_projection.py` | 10 | S | 独立核导入、函数声明与调用面：类型/表结构/编码/格式/文件或配置处理，无自管 runtime SQLite writer；非全文业务验收 |
| `services/native_media.py` | 126 | I | 接口/调用面复核：admin.models、emoji.media、services.image_preprocessor、services.media_resolver；无本轮新增迁移 owner，未全文，不据此排除相关 DB 调用 |
| `services/native_tool_binder.py` | 87 | I | 接口/调用面复核：model_runtime.models、web.models；无本轮新增迁移 owner，未全文，不据此排除相关 DB 调用 |
| `services/participation_diagnostics.py` | 232 | I | 接口/调用面复核：conversation.autonomy_db_models、conversation.canonical_db_models、persistence.database；无本轮新增迁移 owner，未全文，不据此排除相关 DB 调用 |
| `services/participation_feedback.py` | 669 | F | 全文；真实事务/委托、来源与外部效果按上文及方法表复核 |
| `services/participation_identity.py` | 28 | F | 全文；真实事务/委托、来源与外部效果按上文及方法表复核 |
| `services/participation_ordinary.py` | 463 | M | 旧基线 1-165：event_scope/hydrated_scope/admitted_scope；后半读取发生 ff 后，不计旧全文，交 delta/R3 复核 |
| `services/participation_parameters.py` | 13 | S | 独立核导入、函数声明与调用面：类型/表结构/编码/格式/文件或配置处理，无自管 runtime SQLite writer；非全文业务验收 |
| `services/participation_snapshot.py` | 96 | F | 全文；真实事务/委托、来源与外部效果按上文及方法表复核 |
| `services/participation_trace.py` | 91 | F | 全文；真实事务/委托、来源与外部效果按上文及方法表复核 |
| `services/plugin_events.py` | 64 | I | 接口/调用面复核：runtime.observability；无本轮新增迁移 owner，未全文，不据此排除相关 DB 调用 |
| `services/plugin_sessions.py` | 356 | F | 全文；真实事务/委托、来源与外部效果按上文及方法表复核 |
| `services/policies.py` | 171 | I | 接口/调用面复核：本模块现有执行/服务接口；无本轮新增迁移 owner，未全文，不据此排除相关 DB 调用 |
| `services/processing_status.py` | 148 | F | 全文；真实事务/委托、来源与外部效果按上文及方法表复核 |
| `services/processor.py` | 2111 | M | 176-244、1136-1251、1711-1727；附件/ordinary 准备与 supplied-session callback、policy 只读；其余业务入口接口级 |
| `services/profile_commands.py` | 137 | I | 接口/调用面复核：admin.authority.principal、admin.control_resolution、admin.models、memory.service；无本轮新增迁移 owner，未全文，不据此排除相关 DB 调用 |
| `services/prompt_composer.py` | 159 | F | 全文；真实事务/委托、来源与外部效果按上文及方法表复核 |
| `services/prompt_registry.py` | 102 | S | 独立核导入、函数声明与调用面：类型/表结构/编码/格式/文件或配置处理，无自管 runtime SQLite writer；非全文业务验收 |
| `services/qq_face_resolver.py` | 55 | S | 独立核导入、函数声明与调用面：类型/表结构/编码/格式/文件或配置处理，无自管 runtime SQLite writer；非全文业务验收 |
| `services/rate_limit.py` | 79 | S | 独立核导入、函数声明与调用面：类型/表结构/编码/格式/文件或配置处理，无自管 runtime SQLite writer；非全文业务验收 |
| `services/renderer.py` | 82 | S | 独立核导入、函数声明与调用面：类型/表结构/编码/格式/文件或配置处理，无自管 runtime SQLite writer；非全文业务验收 |
| `services/semantic_participation.py` | 1509 | M | 194-237、423-520、691-790、844-911、1171-1216；Snapshot 保存/关闭/恢复及来源验证；_scene/其它 dispatch/reconcile 委托未全文，R3 接力 |
| `services/subagent_execution.py` | 385 | M | 90-385；cancel_commands 与 run 全文，外部 cancel 在读会话关闭后；构造依赖接口级 |
| `services/turn_coordinator.py` | 377 | I | 接口/调用面复核：automation.models、persistence.event_repository；无本轮新增迁移 owner，未全文，不据此排除相关 DB 调用 |
| `services/turn_execution.py` | 1660 | M | 101-232、387-441、1179-1324；完整 PrimaryDispatch、paid compact 恢复与 publication；其余执行循环接口级 |
| `services/turn_transcript.py` | 243 | I | 接口/调用面复核：capabilities.media；无本轮新增迁移 owner，未全文，不据此排除相关 DB 调用 |
| `services/video_frames.py` | 142 | S | 独立核导入、函数声明与调用面：类型/表结构/编码/格式/文件或配置处理，无自管 runtime SQLite writer；非全文业务验收 |
| `services/vision_rate_limit.py` | 62 | S | 独立核导入、函数声明与调用面：类型/表结构/编码/格式/文件或配置处理，无自管 runtime SQLite writer；非全文业务验收 |
| `services/vision_service.py` | 609 | I | 接口/调用面复核：admin.models、gateway.media、persistence.repositories、services.image_preprocessor；无本轮新增迁移 owner，未全文，不据此排除相关 DB 调用 |
| `services/work_resume.py` | 439 | M | 107-184、366-439；完整恢复失败与原 work resume 的 DB 函数；其它 scene/source 构造接口级 |
| `services/yuki_runtime.py` | 87 | I | 接口/调用面复核：application.lifecycle、runtime.activation_bindings、services.agent_runner、services.main_agent_contract；无本轮新增迁移 owner，未全文，不据此排除相关 DB 调用 |

覆盖计数：F 65、M 18、I 72、S 56，合计 211。该计数用于接力，不能把 I/S 写成全文。

尚未在本轮全文的真实 DB/授权依赖已经在 M 表中点名，重点是 WorkQueries 的全部只读方法、SocialService 非派发的 target/contacts/权限查询、MainTurn 完整 validate、MemoryAdmin 其它治理/授权 helper、SemanticParticipation 其余 scene/dispatch/reconcile。已审自管 writer 的结论不替代这些查询语义；第三轮需补齐，尤其新 main 修改的 host sparse identity/session 与上下文链。
