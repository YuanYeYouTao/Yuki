# SQLite 重构交付对照

本表对应[任务书](../architecture/Yuki-SQLite统一写入与短事务重构任务书-2026-10-22.md)，记录实际落点与验证材料，不参与运行时判断。
R3 已完成，固定源码 48+44 轮及真实 512 MiB 对照已归档。用户后续授权合流全部 PR、上线和本机 Codex 连接，
合流、约 2.4 GiB 历史组和最终两组热路径复测已完成；完整 CI、实际合并、部署和连接状态在
[PR #498](https://github.com/YuanYeYouTao/Yuki/pull/498)分项记录。此表是合流提交时的验证快照。

源码起点是 `c2ce7da66ab54efe192993b9615df48266a62137`。R1、R2、R3 产品快照依次见
`f8fbe14d`、`1fb34b85`、`c0af6e82`；设计阶段的三次审查不计入实现后复审。

## 实施项

表中产品路径相对 `src/yuki/`；测试首次引用带 `tests/` 下的目录，同一行后续唯一文件名省略目录。
一行合列的任务共用同一边界，编号没有省略。

| 任务 | 实际落点与保留的事实 | 验证材料 |
| --- | --- | --- |
| W01 | audit 目录的 collect_surface/collect_schema 索引所有受管路径、具名方法及迁移；语义范围按三轮逐路径报告分级 | 三轮 A/B/C/root 报告；索引不是语义验收 |
| W02 | 主库由 ApplicationContainer 注入同一 Database；snapshot、Manager jobs、manifest 独立 | 实施记录资源图；integration/test_database_lifecycle_ownership.py |
| W03–W04 | Database.read_session 与 private factory；FIFO 在 checkout/BEGIN 前，同一连接池并行读 | unit/test_database_writer.py |
| W05–W06 | 原异常 phase、commit/rollback 确认与三态；SQLiteConnection 等实际 worker 退出 | unit/test_database_writer.py、test_database_acquisition_cancellation.py |
| W07–W08 | optional_session、ScopedEventLedgerUoW、Control supplied writer 不再另开事务；文件锁与 writer 顺序明确 | unit/test_writer_c_source_atomicity.py、test_storage_writer_boundaries.py；各域 R3 |
| W09 | 应用 create/start/close 失败回滚；关闭产生者、诊断、Database 的所有权明确 | integration/test_database_lifecycle_ownership.py；R3 公共 owner 报告 |
| W10 | SQLiteDiagnostics 只保内存 SQL/owner 阶段摘要，health 分开报告读与写 | unit/test_diagnostic_writer.py、test_database_writer.py |
| W11–W13 | canonical_repository 的热 receipt/route 读与冷身份、Conversation/alias/Assignment 短写分开；内部 ID 沿原链路传递 | unit/test_duplicate_receipt_reads.py、test_identity_preparation_boundary.py、test_route_writer_preparation.py |
| W14 | ledger prepare_append 先准备正文、分词和 metadata；event/media/FTS/水位仍原子提交 | unit/test_derived_text_transactions.py、test_confirmed_outbound_identity.py |
| W15 | People/Person 合并、forget、账号阶段与原 control receipt；复核原 selector、完整引用和不存在条件 | unit/test_privacy_preparation.py、test_binding_ownership_privacy.py、test_person_merge_routes.py |
| W16 | Config 原 revision/row/合并谱系 CAS 与 audit/control 同笔；validator、文件准备在外 | unit/test_runtime_config_read_snapshot.py；R3 A |
| W17 | bootstrap、名称、Character、授权与鉴权 reader 按实际效果收口 | unit/test_identity_point_reads.py；R3 A/B 的装配与独立 owner 核查 |
| W18–W19 | ordinary 原 event/generation/identity/activation；projection/Compact 原 head/source CAS，共用公共历史与已付私有尾部 | unit/test_ordinary_admission_identity.py、test_projection_source_invalidation.py；integration/test_worker_shared_compact.py |
| W20 | Tool receipt 条件 INSERT 保 source_call_key、SELF 与 privacy；不重调已完成工具 | unit/test_tool_receipt_diagnostics.py、test_diagnostic_source_privacy.py |
| W21–W23 | Artifact、媒体、uploads、analysis 各阶段统一短写；文件/hash/模型在外；已引用原件与 refs 保留 | unit/test_storage_writer_boundaries.py、test_tool_artifact_orphans.py；Linux 媒体定向与全量 |
| W24 | HTTP/ACP/Client error 传真实提交状态；已提交 UUID receipt 不被后续 lease release 失败覆盖 | unit/test_client_works.py；R3 A |
| W25–W26 | Work lease、accept/state/checkpoint/mailbox、stage/consume/cancel 统一 owner；供给 session 原样复用 | unit/test_work_settlement_writer.py、test_work_lifecycle_tail.py、test_work_delivery_ownership.py |
| W27–W28 | WorkSession/Journal 保原 paid response、original call、budget 与 Protocol refs；仅确认回滚后的纯 DB 发布可重备 | unit/test_work_journal_busy_recovery.py、test_work_journal_source_retry.py；integration/test_codemode_lifecycle_recovery.py |
| W29 | Protocol GC 标记后释放 writer，再持文件锁读/unlink，释放后确认；不反向持文件锁等 writer | unit/test_storage_writer_boundaries.py；三轮 C 域报告 |
| W30 | Wait、输入、effect 与 delivery 按原 lease/current source 和完整匹配集合原子提交 | unit/test_work_effect_lifecycle_repository.py、test_writer_c_source_atomicity.py |
| W31 | Subagent 整树/当前 eligible 集合与外部资源准备分开；新增后代/request/peer 复核；原 close 不重放 | unit/test_subagent_lifecycle.py、test_acp_resource_cleanup.py |
| W32–W34 | Social 原 prepare/claim/send/settle；正常 ledger+Social+Work receipt 同提交；unknown/已发补记不授予重发 | unit/test_social_prepared_delivery.py、test_social_delivery_integrity.py、test_social_receipt_original_anchor.py |
| W35 | feedback 原 run/seq/selected refs/absence 在 writer 核，完整材料与独立 snapshot 在外 | unit/test_feedback_writer_preparation.py；三轮 C 域报告 |
| W36 | ModelInvocation/Trace 的队列与 direct fallback 都走公共 writer；诊断失败不推翻已付结果 | unit/test_diagnostic_writer.py、test_diagnostic_source_privacy.py |
| W37–W39 | Plugin 安装/config/KV/session/notification/grant/job/media 的当前 owner、版本、配额与原 receipt；validator/hook/模型/传输在外；OneBot claim 同 writer 核当前 privileged role；PluginManager DB 故障仍 join 原生产者，保原取消与 phase | unit/test_storage_writer_boundaries.py、test_plugin_notification_idempotency.py、test_notification_preparation_sources.py、test_plugin_known_result_audit.py、test_plugin_runner_isolation.py |
| W40–W41 | Sandbox 原 request/run/permission/completion 先持久再 ACK；独立 Manager/snapshot 保自己的 owner 与 CAS，Manager 启动失败也关闭原连接/任务 | unit/test_legacy_acp_receipts.py、test_sandbox_manager_startup_owner.py、test_external_agent_writer_contract.py、test_external_agent_lifecycle.py；integration/test_subagent_sdk_recovery.py；C 域报告 |
| W42 | Memory 基础事实、关系、目标、job 直接短写；一般主键修改不加通用 Plan | unit/test_memory_writer_boundaries.py、test_job_claim_transactions.py |
| W43–W45 | Mutation 读准备、同 writer 授权、完整 source/member/absence 证明和原新增 IDs/receipt；operation ID 不接受不同 payload 冒领回执 | unit/test_mutation_snapshot_upgrade.py、test_memory_operation_identity.py、test_memory_count_projection.py |
| W46 | 删除 Memory apply/prepare_evidence_write、首写升级机制、第二套 BUSY 循环与空 UPDATE；claim 保原 CAS，token 取准入后实际时间 | unit/test_job_claim_transactions.py、test_memory_job_claims.py；R3 Memory |
| W47–W49 | SELF 正文/阈值/尾部计数在 reader 准备；固定形状 SQL 精确核源；原 paid/run/cursor/gap；quiet 在 claim 前等待并退让 | unit/test_self_reflection_tail_recovery.py、test_self_reflection_priority.py；integration/test_self_reflection_checkpoint_recovery.py |
| W50–W51 | Dream 双 owner、原 cluster 多 action 原子单位、preview/paid/checkpoint 与 rollback dependency；删除仅抢写的空 UPDATE | unit/test_dream_plan_transactions.py、test_dream_recovery_snapshot.py、test_dream_input_snapshot.py |
| W52–W53 | Embedding Provider/hash/编码在外，claim/profile/fact CAS 与批量 upsert；数据库 ACK 丢失不改报 Provider 错误 | unit/test_embedding_claim_boundaries.py、test_embedding_partial_results.py |
| W54–W55 | Rebuild 原 selection/source/target、付费暂存、采用、receipt sweep 与最终 absence 分别核验；保原结果/已完成项 | unit/test_memory_rebuild_writer_boundaries.py、test_rebuild_receipt_finalization.py、test_rebuild_privacy_preparation.py |
| W56 | 维护只拆独立项；显式 FTS rebuild、整次隐私清退仍原子；FTS writer 不再包掉真实数据库 phase | unit/test_memory_maintenance_evidence_snapshot.py、test_memory_fts_write_outcomes.py；R3 Memory |
| W57–W58 | 迁完真实消费者后删除旧 factory、snapshot 升级、HintSession、holders/首写/物理 ID 观测、死 People API 和泛型 claim helper | 下文删除账；全仓源码反查与三轮审查 |
| W59–W60 | SQLite 阶段冻结的 97 个 revision/head 0147 不改；最终组合新增历史索引 0148、外部 Agent 0149，共 99 个 revision、单 head 0149；create_schema/guard/index/FTS/FK 与离线 close/取消所有权核查 | unit/test_schema_guard_startup.py、test_offline_database_owners.py、test_migration_0149.py；integration/test_history_scope_indexes.py；公共 owner R3 与合流记录 |
| W61 | development-contract、Memory/历史/插件/runtime 当前合同与实施记录同步，历史报告保原事实 | 三轮报告、当前架构入口 |
| W62 | 文件 WAL、多连接、真实线程 barrier、实际 COMMIT 后丢 ACK 和跨领域回归 | 下列 V 对照、Linux/CI 最终结果 |
| W63 | 同一脚本、同 fixture、同到达计划、固定 Fake Provider/平台，旧→新→新→旧；真成功 Social receipt 关联输入 | scripts/benchmark_sqlite_writer.py；最终性能报告 |
| W64 | R1/R2/R3 都按全源码清单审实际 DB 与调用者，纯类型/渲染明确排除；发现后修复再复读 | 实施记录三轮矩阵；R3 最后 PluginManager 与 eager delta 已独立复读 |
| W65 | 最终提交、推送、PR 与 CI 分别记实 | #494/#497 原提交已合入 #498 并推送；完整 CI 与合并状态见 PR #498，Desktop #3 已独立合并 |
| W66 | 生产上线及自然流量验收 | 已获上线授权；需同步必要 Manager 源码并保原环境，再做 Bot-only 容器替换；一致备份/健康/清理、自然回执与本机连接按 PR 后续交付记录 |
| W67–W68 | Automation task/version/audit 与 occurrence/finish/schedule 原子；准入后 lease 时钟；create_run 丢 ACK 按原 occurrence 结算，不重买模型 | integration/test_automation_lifecycle.py、test_automation_unknown_commit.py |
| W69 | 根提交确认且释放 owner 后同步发布 CommitHint；已确认 commit 的取消也发布，savepoint 不发布；live span 原显式通知 | unit/test_database_writer.py；公共 owner R3 |
| W70–W71 | Notification ledger+finish_outbox 合并；旧中间状态据原 receipt 恢复；plugin media 注册未知不直接 unlink | unit/test_notification_preparation_sources.py、test_plugin_notification_idempotency.py；C 域报告 |
| W72 | 删除观察用 dialect/raw driver patch；仅 SQLiteConnection 保有关闭实际完成事实 | unit/test_database_acquisition_cancellation.py；公共 owner R3 |
| W73 | web/media 过期清理保无候选 reader 和分页；ASR 预分词及原源重核，不恢复退役索引写 | integration/test_web_search_read_separation.py；unit/test_conversation_media_cleanup.py、test_derived_text_transactions.py；A 域报告 |

## 删除账

- 公共 `Database.sessions`、`apply_snapshot_write`、首写快照升级闭包与对应监听；实际读显式使用 `read_session`。
- Memory `apply_evidence_write`、`prepare_evidence_write`、额外 BUSY 重试层和仅认领写锁的空 UPDATE。
- `HintSession` 及提交监听；holder、first-write、物理连接 ID 和只用于观测的 driver/dialect 包装。
- `People.observe` 及无生产调用者的旧观察链，三处死 canonical reader；测试改用当前真实 canonical 入口。
- 五处无生产消费者的 Memory/Preference 后置 AdminAudit 写及其专属注入/计时；保 fallback/control/config/Automation 的原子审计和真正的 mutation receipt。
- 仅两处 Memory 消费者使用的 `persistence/job_claims.py` 泛型层，合回 Memory 的已有 claim 模块；删除 `find` 中无效的 fingerprint 参数，实际 receipt 校验收口到具名消费点。
- SQLite checkout `pool_pre_ping` 和重复 raw-get 异步包装；不新增替代探活。预算初始化原 SELECT 与 INSERT 合为同 writer 的 `INSERT … SELECT`。
- 无产品和测试消费者的 `GroupAdminService` 整文件、DI 字段及导出；真实群管理仍走现有 operations，GroupRecovery 保留自己实际使用的类型。
- 无 SQL 的 reader context 与空 Protocol refs 的线程准备；保留原纯条件，有数据时仍执行原查询和来源证明。

真实身份、授权、当前来源、幂等键、租约、业务 CAS、预算、已付结果与回执均属于领域事实，不列入删除目标。

## 场景对照

这些场景并非五十份彼此孤立的新测试。优先沿现有真实业务回归，只有确认缺口才新增；各报告的重叠测试数不累加。

| 场景 | 实际验证落点 |
| --- | --- |
| V01–V02 | database_writer 的同文件 WAL/FIFO、旧快照交错；mutation_snapshot_upgrade；正式 mixed/stress 负载 |
| V03 | privacy_preparation、route_writer_preparation、writer_c_source_atomicity、Memory mutation/Dream/Rebuild 各源变化及新增成员回归 |
| V04–V06 | database_writer、memory_writer_boundaries、derived_text_transactions；supplied session 的 dirty ORM 可见与整笔回滚 |
| V07–V08 | database_writer 的并行 WAL reader、相同 owner 与不同文件；database_lifecycle_ownership |
| V09–V11 | database_writer 与 database_acquisition_cancellation 的真实队列/connector/configure/SQL/flush/commit/rollback/close 取消及物理退出 |
| V12 | social_delivery_integrity、social_receipt_original_anchor、notification_preparation_sources 与真实 ACK 故障 |
| V13 | SELF priority/quiet、Work/Automation/Memory 准入后时钟回归；mixed 与双核负载 |
| V14 | storage_writer_boundaries、Protocol/Artifact GC 与引用/发布交错；C 三轮锁序核查 |
| V15 | diagnostic_writer 满队列/失败/关闭；ModelExecutor 诊断不推翻模型结果 |
| V16 | database_writer 实际外部连接 BEGIN IMMEDIATE；不宣称进程内锁覆盖跨进程 |
| V17–V18 | 原 paid checkpoint/operation/tool/Social/Work 恢复组；固定脚本所有输入与成功回执及规模负载 |
| V19 | offline_database_owners、独立 snapshot/Manager 原 owner 回归与三轮资源图 |
| V20–V21 | database_writer、duplicate_receipt_reads、runtime_config_read_snapshot、identity_point_reads、同 supplied session 组合回归 |
| V22 | 真实 commit ACK 故障组：Automation binding、ClientWorks、WorkControl、Embedding、Memory operation、FTS 与 typed outcome 映射 |
| V23–V26 | lifecycle_ownership、offline_database_owners、database_writer、schema_guard_startup；资源顺序、真实收尾和读/写健康分开 |
| V27–V28 | duplicate_receipt_reads、identity_preparation_boundary、route_writer_preparation、binding_ownership_privacy、canonical forget/merge/ingress 组 |
| V29 | schema/FTS/FK/迁移回归、derived_text_transactions、projection_source_invalidation；head/frozen revision 静态比对 |
| V30–V31 | 原 Control database savepoint/external reserve/settle、Config/Person/Character/SOUL 故障与真实原 receipt |
| V32–V33 | ordinary/projection/Compact 与 shared compact；tool receipt 的 current source/SELF/privacy 条件 INSERT |
| V34–V35 | Artifact/Protocol/plugin media 的原引用与 unknown 注册、GC/媒体取消/原件保护；Linux 原生媒体组 |
| V36–V38 | ClientWorks 原 UUID/event/input 回执与 release 故障；Work/Automation/Memory 原 lease/paid 结果与队列时钟 |
| V39 | WorkWait 原完整 matching/owned-run 集合、reply internal event、mailbox/条件/水位同笔 |
| V40–V42 | Subagent lifecycle/resource cleanup、Social 三方 settlement、feedback 与独立 snapshot CAS/取消恢复 |
| V43–V45 | DiagnosticWriter 与 writer=None、Plugin validators/notification/session/background；Sandbox 原 request/completion/permission/ACK |
| V46 | Memory mutation/relation/evidence/source/selection、operation 并发 payload、copy/forget/merge 与原新增 IDs |
| V47 | SELF tail/privacy/runtime/priority/checkpoint 组，含 variable limit=256、大正文、空白/语音/gap/多 run 与已付检查点 |
| V48–V50 | Dream preview/cluster/rollback；Embedding claim/profile/源变化与真实 complete ACK；Rebuild receipt/privacy/完成竞态 |

最终全量和 CI 的准确命令、计数、失败及修复记录见实施与性能报告；此表不把尚未执行的上线或 native CI 写成通过。
