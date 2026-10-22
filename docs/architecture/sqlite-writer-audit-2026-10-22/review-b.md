# SQLite 全覆盖审查 B：Memory，第 1 轮

基线：`cdb49eadc0c7cd16060bf0329234ae69611e62bd`。范围是 `src/yuki/memory` 的 87 个 Python 文件（26948 行）与实际关联测试。87 个源码文件已全文分块阅读，26 个重点测试文件与 4 个 support 文件已全文阅读；其余关联测试明确列为后轮接力，未计为全文完成。不能把目录清单或 AST 标签视为已经完成语义审查。只修改本文，不修改产品代码、不运行模型、不连接生产。

已读 AGENTS、共同约束及现行 `memory-v2.md`、`memory-change.md`、`memory-v2-lifecycle.md`、`memory-v2-rebuild.md`、`memory-v2-embedding.md`、`memory-v2-retrieval.md`、`memory-v2-quality.md`、`self-reflection.md`。现行合同的“同一 WAL 读快照后升级写、BUSY 确认回滚后新 session 重备”描述的是本基线实现；任务书改为 admission 后短复核时必须同时更新，不能把旧快照保护静默删掉。

## 已确认的跨域要点

1. MemoryFactRepository.transaction(read_snapshot=True) 是旧入口：显式 `BEGIN`、session.info 缓存、连接 before_cursor_execute 监听首次 DML；`apply_evidence_write` 自己处理 operation/flush 的 BUSY 重备，物理 commit/rollback 异常不授予重放资格。它和 Database 的同类循环是迁移删除对象，但不能删除其真实业务消费者。
2. 完整 readable evidence、replacement target/source 双重资格、复制去重、累计新增 evidence 与 relation ID 是不同语义。后两项还写入 Dream operation，供按原回执回滚。重构不能只按名字把所有 session.info 一起删掉；能由 INSERT RETURNING/领域结果直接返回的辅助汇总应简化。
3. `MemoryFactService` 大部分无 session 的写经 apply_evidence_write，但 `resolve_conflicts` 直接开 read_snapshot transaction；`mark_injected` 是独立 sessions()+begin UPDATE。`MemoryJobRepository` 的 enqueue、fail、complete 与通用 commit_job_claims 也各有写入口。统一覆盖不能只迁 MutationService。
4. `_commit_prepared.apply` 的来源、owner、fact signature、权限准备发生在旧 deferred snapshot 首写之前；随后 reserve receipt flush 才取得 writer。不能将整个旧 callback 原封不动包进新 immediate writer。
5. `_validate_fact_requirements` 调用 same_targets/person_owns_fact 未传当前 session，会另开连接；旧实现内层查询不属于外层快照。统一事务时应显式复用当前事务进行实际必要复核，避免形成 writer 内跨连接调用链。
6. selector 先在 `_prepare/_locate_fact` 查出唯一 fact，再转为 fact_id；后续 commit 仅核该事实签名。任务书需明确“本次查找已固定原 fact_id”与“提交时仍须唯一匹配”的真实产品语义，不能笼统要求所有集合永不变化。若保留提交点唯一匹配，则需对完整 selector 谓词复查新增匹配项，旧 ID 列表不能覆盖 phantom。
7. Dream rollback 既核 source/result signature，也核“没有后续 committed dependency”、added evidence/relation 真正归属；这些 absence/集合依赖不能只用 fact.updated_at 替代。
8. Dream 模型、聚类已经在 writer 外；聚类在 asyncio.to_thread。`_commit_cluster_decision` 则在旧快照里重建完整模型输入指纹、完整证据和全部 action 准备，然后首写。迁移时要拆开，不可把 `_input/_fit_input/JSON/SHA/证据全量读取` 挪进统一 writer。
9. Dream plan 的 create_run 已预序列化统计/cluster IDs，并在 writer 内查小列 CAS、核当前同人归属、savepoint+run+clusters 原子创建；仍有零行 UPDATE 抢 writer，迁新统一入口后可删除这段抢锁写。savepoint 的真实局部失败语义不能一并取消。
10. Dream preview 的 ready_preview 名称像查询，实际上会把不匹配 ready rows 标 stale；JSON 解析也在 sessions()+begin 内，且前面 ORM dirty 状态可能触发 autoflush。应拆为读选择/短条件失效，保留 paid preview fingerprint、schema reader、原预算，不给新 DTO 框架。
11. Dream reset_processing_after_restart 已按 128 页发现、immediate 内按原 committed operation 复原执行元数据；这是可直接迁统一入口的短写范例。普通 claim_next_cluster、reserve_model_call、finish/finalize/cancel/retry/defer/mark_run 仍是分散 begin。
12. Dream 的 process_lock 是本 worker 执行生命周期/模型串行，维护另有自身 process_lock；它们不应升级成跨模块 DB 锁。新 writer 等待应发生在实际短数据库提交前，不把整个 Dream 模型周期排进去。
13. 自省 `repository.py:189 _advance_state` 为一个 owner 流式重扫尚余事件、排除完成窗口、计算 evidence_content 长度；由 complete/recover_stale/recover_interrupted 调用。当前 autoflush=False 只推迟 DML，session.begin 不必然建立 SELECT 的物理快照。统一 writer 后不能将这些整段历史扫描留在 writer 内；应先在一致读快照算当前候选及真实范围，writer 再核 source/generation、水位和未完成窗口集合。不能简单把旧 pending 计数当事实源，也不能跨失败空洞推进。
14. `claim_due` 与 `claim_initiative` 包含候选/上下文正文读取、渲染、字符预算、历史时段、source fingerprint、旧窗口重放判断。它们不是只有 run INSERT 的小写；准备应移出，writer 内核原 owner/cursor/状态、范围与窗口互斥。首次启动 baseline、旧 generation live-source、late receipt 独立水位不能改变。
15. 自省请求账本 `control.py:47 reserve_request` 是 INSERT SELECT count 的单语句预算接纳；它本身适合短 writer，**不能**把整个 Provider 请求放进 writer。`finish_request`、cycle 状态、report delivery update 仍须收口；Social 发送继续沿原 call_id 和独立回执，不重发 uncertain。
16. `save_checkpoint` 初次保存会核所引用 event/tool 的完整真实来源后 CAS checkpoint；完成计数更新只认原 JSON expected CAS。这两种依赖不同，不能都要求重扫所有原始输入，更不能用“额度用尽”拒绝已付输出纯数据库收尾。引用集合为空的正常输出不应携未引用私人原文。
17. Embedding jobs 页外文本 hash、writer 内 fact 字段/资格/原 claim CAS 和批量 upsert 已接近目标；claim 在 writer 内重取 now，是正面范例。profile/job purge 独立裸 begin 与 FK cascade 仍需收口和规模审查；不能由“只有一条 DELETE”推断持锁一定短。
18. Rebuild receipt finalization 已做页外 source fingerprint/subject hydration、writer 内页计数/来源 token/selection/原 live receipt 复核，且 writer 内无逐 item 正文解析。`transition` 的 NOT EXISTS 其他执行 run、`complete_receipts` 的未决 proposal 缺席以及 trusted historical binding/reply 集合都属于保留的实际判定。它们可重用共享 writer，不需要再包一套通用计划框架。
19. Rebuild **proposal commit** 与 receipt sweep 是两条路径。`service.py:706/825` 在 writer callback 外验证 event fingerprint、live receipt、subject；callback 内重核 run/proposal/current superuser，然后 apply_resolution。迁移设计应给 proposal commit 同样的实际 source/claim/原批准证明，不能误以为 receipt sweep 的检查自动覆盖了此前事实写入。全文审查未在这里运行真实竞态，需用测试确认 prepare 后 source hide/edit/新增 live receipt 的行为。
20. Rebuild 隐私 prepare_forget 的匹配 selection、人物族/原账号时段、相关事件与原 selection body 是完整选择集合；`apply_forget` 仅比较 prepared.changes 旧行 CAS。它在旧同一 WAL 快照升级路径里避免 phantom；拆事务后须重新查询同一个相关 selection 谓词的成员/缺席，不可用全库 count/max/hash，也不可只核已选 run。writer 内不重新解析所有 selection JSON。
21. `quality/hygiene.py:175 _invalidate_page` 在 deferred snapshot 里流式检查 event evidence（含正文渲染/规范化）再逐条 UPDATE+state INSERT；这是显式维护但仍需拆，只有 fact.updated_at CAS 不证明 source 未更新/新增。FTS 全量 rebuild 在 `fts.py:358` 和 hygiene:261 两入口，属于明确全量维护窗口，不能承诺普通聊天里持锁很短，也不应悄悄拆成破坏索引原子性的分页。
22. Query Plane/FTS 检索/语义向量/名称与历史关系授权/health/audit 都应继续纯读，不经 writer admission。QueryEmbeddingCache 的 asyncio.Lock 只保护进程向量缓存，factory 网络在锁外；不要把它收编成数据库锁，也不要引入完整检索结果缓存。

## 方法级迁移索引（已读部分）

| 文件/方法 | 当前入口和真实 DML | 迁移与保留 |
|---|---|---|
| repository.py:transaction / apply_evidence_write | deferred BEGIN；operation+flush；物理 commit 在上下文退出；BUSY 按原错误对象确认整体 rollback 后重备 | 删除旧升级/重试/first-DML listener；保原取消、未知提交边界；复杂证据准备另行短复核 |
| repository.py:create_fact / transition / record_created / set_review_state | session.add+flush；SELECT 可能 autoflush | 复用调用者 writer；created fact、原版本与状态事件同提交 |
| repository.py:add_evidence / copy_prepared_evidence / add_relation | SQL 资格查询、ON CONFLICT RETURNING；更新 fact.updated_at；汇总 additions | 保可读来源/真实目标与去重；批量插入返回供 Dream 回执，不复制另一套 journal |
| repository.py:update_confirmation_metadata / restore_confirmation_metadata | 当前值读取+UPDATE | 普通短 RMW，可在 writer 小查询内完成，不需要另建准备对象 |
| repository.py:mark_injected | 独立 begin UPDATE | 核真实调用者后迁或删除无人用写入，不能漏过 |
| service.py:remember / apply_claim / confirm / version / invalidate / contest / restore / merge / explicit/preferences / administrator_* | 无 session 包 apply_evidence_write；共享调用复用 session；原证据复制/两跳冲突邻居 | 重准备在 writer 外；必要 indexed state/owner/CAS 与局部关系谓词在 writer；embedding 在提交后 |
| service.py:resolve_conflicts | 独立 transaction(read_snapshot=True)，逐项 transition+flush | 统一短写；原指定 alternatives 与 preferred 的原子性保留 |
| repository.py:MemoryJobRepository.enqueue_resolved | 多个身份/原事件/分区读取后 INSERT ON CONFLICT | 查证外移，短 writer 核原 source/owner、event 去重；不借今日绑定改变旧来源 |
| repository.py:MemoryJobRepository.claim / claim_ready_batch | 读 owner readiness、event/text 后 commit_job_claims 条件领取 | 共用通用 claim writer；writer 时刻取时间；原 job claim CAS 保留 |
| repository.py:MemoryJobRepository.complete / fail | complete 用 apply_evidence_write+原 receipt 查证；fail 独立 begin UPDATE | 简单条件短写；原 claim updated_at/event/status 和 unknown acknowledgement 查证保留 |
| job_claims.py:fence_memory_job_claim | UPDATE updated_at=itself，核 claim event/status/updated_at | 与 mutation 原子接纳；统一 writer 后仍需真实 ownership CAS，可合入真实后续条件写而非无条件删除 |
| mutation/service.py:_commit_prepared / mutate_validated_claim | 外部身份/source snapshot；apply 内纯 DB 再核；reserve flush、Work receipt、mutation/result 原子写 | 原 mutation UUID 在重备外；派生 embedding 后置；未知提交读原 receipt，取消仍传播 |
| mutation/service.py:mutate_dream / rollback_dream_operation | 共用 caller session；operation、evidence/relation、原状态、mutation receipt | 大准备外移；完整 source/copy/absence 短复核；不重买模型、不重置预算 |
| mutation/repository.py:reserve / reserve_dream / finalize | 仅共享 session 的 add/flush 或 UPDATE | 保真实稳定 ID/参数冲突/处理终态；不新建另一份 commit journal |
| dream/repository.py:create_run | optional_session(write=True)、零行 UPDATE、小列 source CAS、savepoint INSERT | 统一 admission 替代零行锁；原来源集/owner核验和原子 run-cluster 保留 |
| dream/repository.py:start_run / claim_next_cluster / reserve_model_call / finish_cluster / finalize_run / cancel / retry_failed / defer_pending | 分散 optional write/session.begin；dirty ORM+后续 SELECT 会 autoflush | 多数是 indexed RMW 直接短 writer；预算按新 Provider 实际派发前原子登记 |
| dream/repository.py:reset_processing_after_restart | 页外读取 IDs；页内 immediate 按 committed operation 恢复 | 直接迁共享 writer；不回放 effects，保 attempts/model_calls |
| dream/repository.py:save_preview / ready_preview / stale_previews / mark_preview_applied / mark_run_* | proposal JSON 预编码；ready_preview 读后改 stale；其他条件 UPDATE | paid preview 与 mutation/operation 同原生命周期；读不申请 writer，失效部分短条件写 |
| dream/repository.py:create_operation / commit_operation / checkpoint_fact | caller session 内 add/flush/UPDATE；JSON/signature 部分仍在写区算 | 可预编码已固定字段；ID与新状态签名须认实际写出值；回执/rollback资料保留 |
| dream/service.py:process_cluster / _commit_cluster_decision / rollback_operation | 模型外部；纯 DB whole cluster 同事务；post-commit embedding | 准备完整模型依赖与证据集；writer 只核与提交；原 action IDs、mutation IDs、preview consume 原子 |
| maintenance.py:process_once / _invalidate_candidates | process_lock→候选只读→apply_evidence_write；预证据后逐项 invalidate | 保分页与空页不抢writer；到期与当前status短复核；不引入普通事实年龄淘汰 |
| self_reflection/repository.py:_scan_event_batch (293) / claim_due (421) / _advance_state (189) | sessions()+begin；autoflush=False；多段事件/窗口/owner读取、渲染hash，最终run/state DML | 一致读准备与完整owner/源范围/未决集合复核；不能整段套immediate；失败空洞、baseline、raw owner水位保留 |
| self_reflection/repository.py:complete (836) / recover_interrupted (877) / recover_stale_runs (897) / _recover_processing_run (943) | run/checkpoint/result map读取；advance cursor或历史重扫；ORM提交 | 恢复原run/results/checkpoint；JSON与历史扫描外移；原状态/源集/水位短复核，处理页之间释放writer |
| self_reflection/repository.py:fail (1020) / save_checkpoint (1044) / cleanup_receipts (1143) | fail条件UPDATE；checkpoint旧apply_snapshot_write；清理页外发现+DELETE内完整NOT EXISTS引用/窗口谓词 | fail直接短写；初次checkpoint完整source proof、后续expected JSON CAS；清理保持新增引用/延期优先与空页只读 |
| self_reflection/control.py:reserve_request (47) / finish_request (78) / enqueue (258) / claim (333) / finish (348) / delivered (432) / resume_batch (448) | INSERT SELECT预算；cycle INSERT ON CONFLICT；ORM状态与report JSON、receipt UPDATE | 普通短写直接统一；报告snapshot/JSON外备，原run/cycle/请求账本/社交回执保留；retry_due读不写 |
| self_reflection/initiative.py:claim_initiative (61) / advance_cursor (25) | 读run/cursor/window/receipt正文后UPDATE/INSERT；cursor UPSERT MAX | 按原initiative独立水位，完整旧窗口重试；源准备外移，新writer核cursor/互斥与state，不重编原run |
| rebuild/repository.py:create_run / transition / ensure_item / stage_claims / fail_item / defer_item / update_scan_checkpoint / record_model_usage / set_review / finish_proposal / fail_proposal / reset_failed / purge | optional write或裸begin；stagingJSON序列化和run解析穿插DML；transition用NOT EXISTS互斥 | 短RMW或条件DML进入统一writer；固定JSON外编码，staging/receipt与事实的原子性保留，terminal purge核cascade规模 |
| rebuild/repository.py:pause_after_restart (169) | 只读同一BEGIN双absence probe；非空immediate中三条状态UPDATE | 保空闲只读、原ID预算、原子pause、未知ack不自动resume |
| rebuild/service.py:process_commit_once (706) | 模型/subject/source检查外部；callback内run/proposal授权和事实+proposal receipt；embedding后置 | 单独补完整源/claim/批准/live receipt写时证明，不能由后续receipt sweep补救已写事实 |
| rebuild/receipt_finalization.py:complete_receipts (161) | 页外source/hash/hydration；immediate下_read_batch+trusted fences+job/item DML | 保未决proposal完整选择、run selection与原live receipt，writer内批量SQL，原item/job不重写 |
| rebuild/privacy.py:prepare_forget (37) / apply_forget (174) | read解析JSON/人物族/原时段；caller writer核旧行CAS+proposal DELETE+selection UPDATE | 拆快照后补相关selection成员/absence复核；原合族与跨平台scope保留，不加全库catalogue gate |
| embedding/jobs.py:_enqueue_prepared (127) / recover_interrupted (248) / claim (285) / _complete_page (365) / skip_many (491) / fail_many (520) / retry_failed (545) | immediate短写+fact/claim CAS；页外hash；批量upsert | 保正确已有边界，接共享admission，实际写时now与next_updated_at防旧claim复用 |
| embedding/repository.py:ensure_profile (119) / purge_old_profiles (249)；jobs.py:delete_for_old_profiles (594) | 裸begin INSERT ON CONFLICT/DELETE，FK cascade | 统一短writer；大规模显式purge单列维护实际占用，不重复排同一profile在飞任务 |
| quality/hygiene.py:_invalidate_page (175) / _purge_page (238) / rebuild_fts (261)；fts.py:rebuild (358) | deferred历史检查后变更；staging DELETE；FTS rebuild | 只读扫描外移并保完整来源absence；FTS全量属显式维护窗口，非日常延迟承诺 |

## 覆盖清单

以下 87 文件均已全文读取，分 DB/组合调用审查、只读授权与查询、纯数据/计算/模型适配三类记录；分类是阅读后的排除理由，不是以 AST 代替阅读。大型文件分块，首次输出截断的 eligibility/extraction/mutation provenance 已单独补读。

## 测试与后续轮次

以下 26 个测试文件均已全文阅读，**没有执行测试**。表中描述是源码所表达的验证，不能写成新架构已经通过。旧实现专用的 BUSY 注入、重试次数和 session 数量应随旧机制删除；其保护的原 ID、paid result、回执原子性与真实竞态仍需保留。

| 测试（tests/ 下） | 已读的关键合同 / 对应任务 |
|---|---|
| `unit/test_memory_writer_boundaries.py` | held writer 下空维护与未过期页仍纯 SELECT；W42/W56 |
| `unit/test_memory_maintenance_evidence_snapshot.py` | 完整批证据首写前准备；到期延长、两跳冲突变化、真实 BUSY/517、取消与未知 commit/rollback 不盲重放；W45/W46/W56 |
| `unit/test_memory_maintenance_query_cost.py` | 50→2000 行下分页维护/待处理/报告扫描 SQL VM 成本；W47/W56 |
| `unit/test_memory_paid_tail.py` | self checkpoint 与 Dream paid preview 在原预算耗尽后仍能纯数据库完成；provider 只调一次、原 job 不重编；W48/W51 |
| `unit/test_memory_job_claims.py` | 重复 enqueue 纯读、原 claim fence、模型期间被重新领取时拒绝旧结果；W42/W45 |
| `unit/test_dream_busy_recovery.py` | 原 operation/mutation/model_calls、已提交回执计数、commit/cleanup 与 operation BUSY 分类、取消；W46/W51 |
| `unit/test_dream_recovery_snapshot.py` | 空恢复只读，等到 writer 后能认新 committed operation；统计/cluster/preview 编码在首写前；W50/W51 |
| `unit/test_dream_planning_cost.py` | 不变分区不重算 pair、变化保邻居、归一化只做一次且不占宿主线程；W50 |
| `unit/test_dream_input_snapshot.py` | 模型期间来源隐藏/等数量替换与重备变化必须使旧指纹失效；复制保所有证据；W43/W45/W50 |
| `unit/test_dream_plan_transactions.py` | 300 cluster 批量准备/写入，来源与 owner 变化无半个 run，savepoint 失败原子性，260 项恢复分批；W50 |
| `unit/test_dream_owner_projection.py` | 原 owner 与跨账号迁移，旧 paid schema 冷恢复仍用原证明和原预算；W50/W51 |
| `unit/test_memory_rebuild_repository.py` | 同一 WAL 双 absence probe、准备后变更、三种 pause 更新同事务、第二更新失败全回滚、未知 ack 不自动 resume；W54/W55 |
| `integration/test_self_reflection_checkpoint_privacy_race.py` | 初始 checkpoint 不能复活已擦除 event/tool；最终 expected JSON CAS；真实 517 重核；未引用私人输入不扩权限；W48 |
| `integration/test_self_reflection_checkpoint_recovery.py` | 原结果位置/映射、旧 NOOP、legacy 输出保留，原 checkpoint 不重调模型、不重记预算；W48 |
| `unit/test_embedding_claim_boundaries.py` | 1/20/129 页外 hash、无变化纯读；late claim、部分页、时钟停滞与内容变化、失败重试/取消；W52/W53 |
| `unit/test_mutation_concurrent_resolution.py` | 工具与 worker 可并行准备，不应新增全局 mutation 锁；W45 |
| `unit/test_mutation_snapshot_upgrade.py` | 原 mutation ID 与 resolve 仅一次；未知 commit/rollback 读原 receipt；绑定变化与隐藏源、取消；W44/W45/W46 |
| `unit/test_rebuild_privacy_preparation.py` | 别名完整准备、同 count/max 也能发现新增相关 selection、共享事务全回滚、擦 selection body 留预算；W55/W56 |
| `unit/test_rebuild_receipt_finalization.py` | 1/10/100/1000 项有界页、无逐项 claim body/首写后查询；新增 live receipt、review/generation/selection/source 变化，新页和完成范围不扩；W54/W55 |
| `unit/test_rebuild_trusted_receipts.py` | 原 mention/binding 历史证明不被今日名称/启用/合并/转移解释重写；真擦除重备；W54/W55 |
| `unit/test_memory_mutation.py` | 1864 行全文：create/correct/version/merge/contest/invalidate/restore 与原证据/权限、并发同请求一次、finalize 失败整体回滚、post-commit embedding 失败不撤事实、当前 ACL 与忘记立即生效；W42–W46 |
| `unit/test_memory_current_reads.py` | embedding 等待后用当前 SQL ACL；只缓存 query vector，不缓存整个结果；真实 token accounting；W53 |
| `unit/test_memory_operation_identity.py` | 原 operation 重放不同于同内容新操作，不复活后来失效事实；当前语音原文的证据资格；W43/W45 |
| `unit/test_memory_evidence_batch.py` | 1/64/1000 完整可读证据排序/limit/count；各 owner shape、隐藏/搬移/擦除源、initiative 不借 chat source；W43/W45 |
| `unit/test_memory_hygiene_reason.py` | 缺来源 invalidation 与 state event reason 相同，不伪造管理员；W56 |
| `unit/test_embedding_manual_authority.py` | 手工 reconcile 每页用原 Person 当前权限，撤权后停止；retry 在原 writer 中验权限，不重置旧 failed job；W52/W53 |

已全文读 `tests/support/sqlite_cost.py`、`memory_identity.py`、`self_initiative_memory_quality_helpers.py`、`sqlite_diagnostics_helpers.py`。它们分别提供只读 VM 步数、真实账号归属迁移、SELF 实际回执来源、writer 归因解析；这些 helper 不能替代真实并发行为验证。

后轮必须接力的未全文项：`test_memory_dream.py`、`test_self_initiative_memory.py`、`test_memory_person_identity.py`、`test_memory_partition.py`、`test_memory_rebuild.py`、`test_memory_retrieval.py`、`test_memory_v2.py`，以及其余 extraction/independent_facts/multiplatform/rebuild_authority/rebuild_internal_selection/rebuild_lifecycle/result_grounding/evidence_retirement/worker_shutdown、self cycle_enqueue/privacy/runtime、reflection_report_delivery、embedding_partial_results、candidate_privacy 相关测试。这些已做文件/引用定位，**不计为本轮全文审查**。`test_memory_v2` 的 `_append_event`、`test_memory_partition` 的 `_owners`、`test_memory_retrieval` 的 `_query`、`test_self_initiative_memory` 的 seed/record/finish/claim 被已读测试引用；本轮只确认调用用途，不能据此声称其定义文件全文已读。

跨域集成测试（app/Telegram/plugin/CodeMode Memory 授权、privacy/person/material/原工具回执）由后轮与 A/C 报告合并核对。Shared `conftest`、model executor 和 canonical fixtures 没有在 B 重复全文登记；应由全局覆盖表说明归属。本轮仅完成 B 全源码和上述 26 个关键测试的独立语义审查，不能将尚未全文的测试计入全项目最终完成。

## 任务书需落细的实施点

现任务书已将 Memory 拆为 W42–W56，比最初单列“迁 Memory callback”更接近真实边界。以下作为方法级补充，不再新增通用框架：

- W42：将 mark_injected、MemoryJob enqueue/complete/fail、profile/job purge、preview stale、cycle/report 状态等隐藏写列入真实清单；普通 indexed 条件写直接用共享 writer，不为每条 UPDATE 造 DTO。
- W43/W45：为 source/copy/evidence、两跳关系、Dream rollback 后续 committed dependency、rebuild 相关 selection 成员/缺席分别写实际 SQL 依赖；只核旧 IDs、count/max 或 fact.updated_at 均不足。明确 selector 是否已经固定原 fact_id，避免把推测变成新业务门槛。
- W44：same_targets/person_owns_fact 的当前事务参数必须沿调用链传到底；加入禁止 writer 内隐式开新 reader 的针对性回归，不能靠再加一把锁掩盖。
- W46/W51：删除旧监听、首写标志、快照升级循环前，先移走其所有消费者；实际 evidence/relation additions 和原 operation 回滚材料保为领域回执。未知提交以原 receipt 查证，不另建提交 journal。
- W47/W48：claim_due、claim_initiative、_advance_state、recover 的流式历史/正文/hash/JSON 都要检查实际首写位置和 autoflush；不是将 sessions()+begin 全文替换为 immediate_session。quiet 调度分支另行合流，不带入本文基线假设。
- W54/W55：proposal commit 的源/claim 证明需单独验证；receipt sweep 不能修复已经由旧来源写出的事实。补“全部准备完成后、真实 writer 接纳前”的 hide/edit/delete/new-live-receipt 测试，与当前在页读取中插入变更的测试区分。
- W56：FTS rebuild、级联 purge 与 privacy 原子授权变更按真实业务划边界；能分的独立维护页释放 writer，不能分的明确测持锁和等待，不声称所有事务天然短。

V03 还需覆盖旧主行不变而新增相关证据/关系/匹配 selection 的 phantom，等数量替换，以及不同 canonical owner 的组合；V17 覆盖 saved reflection checkpoint、旧 Dream paid preview reader、原结果索引与原预算、rebuild 已完成 item/receipt。新测试应验证业务结果，不冻结旧 BUSY 重试 4 次或新 session 5 次等实现细节。旧测试在准备回调中同步写另一事务的注入点，迁移后必须放在接纳前；若机械搬入 writer，会制造测试自身死锁。

性能不设新的经验门槛。对照同一负载的到达→响应、admission 等待、真实 lock hold、只读并发和 SQL/正文读取量，重点看大量历史的 self claim/advance、Dream source recheck、级联维护。现有延迟可接受，应以保住当前路径为目标；只读检索与无变化快路继续绕开 writer，不让后台等待传染到所有读取。

### 逐文件全文清单

| 文件（src/yuki/memory/ 下） | 行数 | 第1轮阅读结论 |
|---|---:|---|
| `__init__.py` | 65 | 全文：类型/纯计算/Provider或导出；无直接DB写 |
| `audit.py` | 199 | 全文：只读/来源/授权依赖 |
| `authorized_scope.py` | 259 | 全文：只读/来源/授权依赖 |
| `claim_processor.py` | 186 | 全文：事务/真实写或委托链 |
| `context.py` | 100 | 全文：事务/真实写或委托链 |
| `dream/__init__.py` | 1 | 全文：类型/纯计算/Provider或导出；无直接DB写 |
| `dream/db_models.py` | 333 | 全文：ORM结构/索引外键；无运行时事务 |
| `dream/models.py` | 285 | 全文：类型/纯计算/Provider或导出；无直接DB写 |
| `dream/planning.py` | 89 | 全文：类型/纯计算/Provider或导出；无直接DB写 |
| `dream/repository.py` | 1426 | 全文：事务/真实写或委托链 |
| `dream/service.py` | 1034 | 全文：事务/真实写或委托链 |
| `dream/worker.py` | 242 | 全文：事务/真实写或委托链 |
| `eligibility.py` | 142 | 全文：只读/来源/授权依赖 |
| `embedding/__init__.py` | 41 | 全文：类型/纯计算/Provider或导出；无直接DB写 |
| `embedding/codec.py` | 52 | 全文：类型/纯计算/Provider或导出；无直接DB写 |
| `embedding/fake.py` | 81 | 全文：类型/纯计算/Provider或导出；无直接DB写 |
| `embedding/health.py` | 66 | 全文：只读/来源/授权依赖 |
| `embedding/jobs.py` | 616 | 全文：事务/真实写或委托链 |
| `embedding/metrics.py` | 68 | 全文：类型/纯计算/Provider或导出；无直接DB写 |
| `embedding/models.py` | 143 | 全文：类型/纯计算/Provider或导出；无直接DB写 |
| `embedding/provider.py` | 35 | 全文：类型/纯计算/Provider或导出；无直接DB写 |
| `embedding/query_cache.py` | 102 | 全文：类型/纯计算/Provider或导出；无直接DB写 |
| `embedding/qwen.py` | 260 | 全文：类型/纯计算/Provider或导出；无直接DB写 |
| `embedding/repository.py` | 395 | 全文：事务/真实写或委托链 |
| `embedding/runtime.py` | 155 | 全文：事务/真实写或委托链 |
| `embedding/semantic.py` | 135 | 全文：只读/来源/授权依赖 |
| `embedding/text.py` | 90 | 全文：类型/纯计算/Provider或导出；无直接DB写 |
| `embedding/worker.py` | 158 | 全文：事务/真实写或委托链 |
| `enums.py` | 259 | 全文：类型/纯计算/Provider或导出；无直接DB写 |
| `errors.py` | 11 | 全文：类型/纯计算/Provider或导出；无直接DB写 |
| `event_extractor.py` | 257 | 全文：只读/来源/授权依赖 |
| `extraction.py` | 136 | 全文：类型/纯计算/Provider或导出；无直接DB写 |
| `fts.py` | 421 | 全文：事务/真实写或委托链 |
| `job_claims.py` | 38 | 全文：事务/真实写或委托链 |
| `maintenance.py` | 165 | 全文：事务/真实写或委托链 |
| `metrics.py` | 74 | 全文：类型/纯计算/Provider或导出；无直接DB写 |
| `models.py` | 622 | 全文：类型/纯计算/Provider或导出；无直接DB写 |
| `mutation/__init__.py` | 29 | 全文：类型/纯计算/Provider或导出；无直接DB写 |
| `mutation/models.py` | 268 | 全文：类型/纯计算/Provider或导出；无直接DB写 |
| `mutation/repository.py` | 203 | 全文：事务/真实写或委托链 |
| `mutation/service.py` | 2969 | 全文：事务/真实写或委托链 |
| `partition.py` | 288 | 全文：只读/来源/授权依赖 |
| `projections.py` | 212 | 全文：只读/来源/授权依赖 |
| `quality/__init__.py` | 1 | 全文：类型/纯计算/Provider或导出；无直接DB写 |
| `quality/audit.py` | 436 | 全文：只读/来源/授权依赖 |
| `quality/event_evidence.py` | 108 | 全文：只读/来源/授权依赖 |
| `quality/hygiene.py` | 271 | 全文：事务/真实写或委托链 |
| `quality/models.py` | 41 | 全文：类型/纯计算/Provider或导出；无直接DB写 |
| `query.py` | 94 | 全文：类型/纯计算/Provider或导出；无直接DB写 |
| `ranking.py` | 250 | 全文：类型/纯计算/Provider或导出；无直接DB写 |
| `read_scope.py` | 379 | 全文：只读/来源/授权依赖 |
| `rebuild/__init__.py` | 8 | 全文：类型/纯计算/Provider或导出；无直接DB写 |
| `rebuild/models.py` | 189 | 全文：类型/纯计算/Provider或导出；无直接DB写 |
| `rebuild/privacy.py` | 254 | 全文：事务/真实写或委托链 |
| `rebuild/receipt_finalization.py` | 352 | 全文：事务/真实写或委托链 |
| `rebuild/repository.py` | 1131 | 全文：事务/真实写或委托链 |
| `rebuild/service.py` | 951 | 全文：事务/真实写或委托链 |
| `rebuild/trusted_sources.py` | 189 | 全文：只读/来源/授权依赖 |
| `rebuild/worker.py` | 61 | 全文：事务/真实写或委托链 |
| `repository.py` | 2154 | 全文：事务/真实写或委托链 |
| `retrieval.py` | 578 | 全文：只读/来源/授权依赖 |
| `runtime/__init__.py` | 13 | 全文：类型/纯计算/Provider或导出；无直接DB写 |
| `runtime/capability_view.py` | 61 | 全文：类型/纯计算/Provider或导出；无直接DB写 |
| `runtime/contract.py` | 53 | 全文：类型/纯计算/Provider或导出；无直接DB写 |
| `runtime/partition_lookup.py` | 34 | 全文：只读/来源/授权依赖 |
| `runtime/query_plane.py` | 191 | 全文：只读/来源/授权依赖 |
| `runtime/resolver.py` | 77 | 全文：类型/纯计算/Provider或导出；无直接DB写 |
| `runtime/turn_session.py` | 96 | 全文：只读/来源/授权依赖 |
| `self_origin.py` | 287 | 全文：只读/来源/授权依赖 |
| `self_reflection/__init__.py` | 24 | 全文：类型/纯计算/Provider或导出；无直接DB写 |
| `self_reflection/control.py` | 455 | 全文：事务/真实写或委托链 |
| `self_reflection/db_models.py` | 32 | 全文：ORM结构/索引外键；无运行时事务 |
| `self_reflection/initiative.py` | 281 | 全文：事务/真实写或委托链 |
| `self_reflection/models.py` | 183 | 全文：类型/纯计算/Provider或导出；无直接DB写 |
| `self_reflection/reporting.py` | 96 | 全文：事务/真实写或委托链 |
| `self_reflection/repository.py` | 1235 | 全文：事务/真实写或委托链 |
| `self_reflection/service.py` | 635 | 全文：事务/真实写或委托链 |
| `self_reflection/worker.py` | 272 | 全文：事务/真实写或委托链 |
| `service.py` | 1404 | 全文：事务/真实写或委托链 |
| `subjects.py` | 481 | 全文：只读/来源/授权依赖 |
| `targets.py` | 97 | 全文：只读/来源/授权依赖 |
| `temporal_filter.py` | 36 | 全文：类型/纯计算/Provider或导出；无直接DB写 |
| `temporal.py` | 64 | 全文：类型/纯计算/Provider或导出；无直接DB写 |
| `testing.py` | 18 | 全文：类型/纯计算/Provider或导出；无直接DB写 |
| `tool_intent.py` | 74 | 全文：类型/纯计算/Provider或导出；无直接DB写 |
| `validation.py` | 234 | 全文：类型/纯计算/Provider或导出；无直接DB写 |
| `worker.py` | 358 | 全文：事务/真实写或委托链 |
