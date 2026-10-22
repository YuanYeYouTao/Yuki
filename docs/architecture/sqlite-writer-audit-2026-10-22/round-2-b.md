# SQLite 全覆盖审查：第 2 轮 B 域独立复核

基线：`cdb49eadc0c7cd16060bf0329234ae69611e62bd`，工作树 `codex/sqlite-writer-architecture-plan-20261022`。范围是 `src/yuki/memory` 的全部 87 个 Python 文件、真实写入委托与相关测试。先独立读实现和测试，最后才对照第 1 轮 `review-b.md`；没有把前轮结论或 AST 候选当作本轮全文阅读。已重读 AGENTS、`development-contract.md`、`memory-change.md`、`memory-v2.md`、`self-reflection.md`。本轮只写本文；未修改产品代码，未运行测试、模型或生产操作。

本文用 F 表示分块全文阅读，T 表示已读实际数据库函数及关键调用链、其他部分结构分类，S 表示 AST/import/函数调用结构分类排除。末尾逐文件列明，T/S 不计为全文。源文件都纳入复核；纯类型、格式、向量计算和模型适配不因此扩成数据库改造任务。源码中的注释或测试名同样不代替真实事务边界。

## 1. 本轮确认及对任务书的具体增补

### B2-01：selector 已经固定的事实 ID 与集合唯一性须分开

`mutation/service.py:_prepare`（1786–1995）先调用 `_locate_fact`（2322–2391）。未指定 fact ID 的 selector 按目标、状态、规范化 key/category/content 查候选，必须唯一；之后通过 `request.model_copy` 固定真实 `fact_id` 并移除 selector。原 request fingerprint 在此变换之前形成，仍区分原请求。`_commit_prepared`（1227–1486）随后核原事实签名、目标、源与权限，不再次定位整个 selector 集合。

因此：后来出现另一个同 key 事实，不能成为拒绝这个已定位操作的新门槛；同 key 独立事实本来允许共存。新 writer 对原 fact/source/owner 的真实依赖需重新核验，但不能把“防 phantom”解释成提交时重新唯一搜索。第 1 轮列出选择语义待明确；本轮从转成固定 ID 的真实实现确认了现行语义。当前任务书 §4.1.1 后的相关集合说明已经吸收这个澄清。V46 的“准备后新增匹配事实”应明确预期：已定位原 ID 的操作继续按原 ID，新增匹配项本身不拒绝；未完成定位的 selector 仍按现行唯一规则处理。

已有 `test_memory_mutation.py:290–325` 只覆盖 selector 不跨目标；还需上述两种时点的回归，不能拿这个测试当新增匹配事实的覆盖。

### B2-02：当前 writer session 必须传到底，收起 factory 有实际价值

`_validate_fact_requirements`（2491–2549）内部调用 `MemoryFactService.same_targets/person_owns_fact` 未传 session。这两个服务会在没有 session 时另开 `repository.transaction()`；准备阶段可独立读，writer 内的当前权限/所有权核验必须复用当前 writer。`_same_target_owners`（2949–2969）已经接受 session，以 `current_person_ids` 比较同人合并后的根，不必把原保存 owner 改写成今日根。

同样需沿 `create_fact → resolve_fact_canonical_owners`、证据复制、状态迁移、Work receipt 绑定、ReflectionResult、Dream operation 源/结果等链传递已有 session。保留公共裸 factory 会继续允许服务自行打开不受 admission 的写事务，也让“当前核验”误用另一个 reader 难以发现。赞同任务书的内部 `_sessions`、明确 `read_session()` / `immediate_session()`，但这不是只替换公开属性：旧 `MemoryFactRepository.transaction()` 是可读可写、自动提交的混合入口，应随调用者迁完删除/收窄，不能留下第三个公开混合入口。

`read_session` 对自行创建 reader 设置 `autoflush=False`、不自动 commit、退出结束读快照；传入 session 时原样复用，不改变外层 writer 的 autoflush。不要给 reader 建 SQL 拦截子类。读 helper 可以接受 writer；其内部 SELECT 不等于新开 read_session。当前多查询真实快照路径的显式 `BEGIN` 不能因 Python 3.12 legacy transaction 行为而被机械删除。

### B2-03：Rebuild proposal 采用与 receipt sweep 是两个独立证明

`rebuild/service.py:706–936` 在 callback 之前对 event hash、live job、target/claim 做准备；`commit`（825–862）内重新读 proposal/run/批准状态和创建者权限，然后 `apply_resolution` 与 `finish_proposal` 同提交。这个 callback 没有显式将当前 proposal claim body/target、run selection、当前源指纹与准备时的值一一比较。下游 `apply_claim → add_evidence` 证明源仍存在且可读、owner 对齐，不重新证明旧 quote 仍是当前完整模型来源。

这是源码推导的待验证竞态窗口，不是本轮已经跑出的线上事故。任务书 W54/V50 应独立列出：**全部 resolution 准备完成后、writer 接纳前**编辑/隐藏/删除原 event，修改原 proposal body/target/批准状态，改变实际选择或出现 live receipt。writer 要验证实际依赖，按原结果/原回执处理冲突，不重新买模型。验证目标是旧来源不能写出新事实，不能仅断言后续 item 没有完成。

`rebuild/receipt_finalization.py` 是另一条路径：页外来源指纹和原历史身份准备，writer 内重新读 run selection、item/event/conversation token、proposal 是否仍未决、原 live job，再把该页 job receipt 与 item 状态一起提交。它不会回滚或修复先前 proposal 已写出的事实。其轻量计数确实是 finalization 的业务条件；不需要为了“完整证明”在 sweep 内读取/解析所有 claim JSON。`test_rebuild_receipt_finalization.py:169–213` 明确限制每页 SQL，禁止 CLAIM_JSON 和首写后 SELECT；313–373 的来源竞态只覆盖 sweep，不能自动算作 proposal 采用已覆盖。

### B2-04：隐私选择 phantom 要核相关谓词，不是旧 ID 或全库摘要

`rebuild/privacy.py:prepare_forget` 选择由 canonical 人物族、原 alias/账号归属时段、实际擦除 event、run 当前/原 selection 等共同决定。`apply_forget` 只比较 `prepared.changes` 中已有 run 的 token；新增或另一旧 run 新变成匹配项不会因此出现。当前 owned `rebuild/repository.py:1001–1021` 会在旧 snapshot callback 重新 prepare，所以不能把这个旧 ID 检查直接搬进两事务方案并称完整复核。

新 writer 需要查询同一个**相关匹配谓词的结果成员/缺席**，必要时退出重新准备；不是增加全库 revision、全表 count/max/hash。已有 `test_rebuild_privacy_preparation` 在准备后改变相关 selection，保持总行数/max ID/max updated_at 不变，正是必须保留的场景。`test_person_identity.py:95–132` 还要求准备后人物合并时重新取真实 family，删除整族并保无关人物。不能只修 Rebuild JSON 的 CAS 而忽略外层人物族变化。

保存历史选择的 `event_ids=()` 必须保持“空交集”，不能因为集合为空省略过滤条件而扩大成全库。原 selection body 该擦则擦，hash/预算/原请求回执等事实按原合同保留。

### B2-05：Self 水位准备要包含本次待提交的完成效果

`self_reflection/repository.py:_advance_state`（189–275）被 `complete`（836–875）、`_recover_processing_run`（943–1017）调用；调用者先把当前 run 在 ORM 改成 completed，再算仍 live gap、连续水位与全部剩余 evidence_content。`autoflush=False` 保证这些准备发生在旧首次 DML 前；它不保证整段代码已经适合放进新 IMMEDIATE writer。

移到 reader 准备时，数据库中当前 run 仍是 processing；若按旧状态直接算，会制造本次原本将消除的假空洞。应按本次 run 完成后的领域效果算结果，并在 writer 核真实 state 水位、相关 run/gap 集合、源范围及 Conversation generation，不只 CAS `state.updated_at`。同批 recover 多个 ordinary/initiative run，也必须把本批完成效果带入计算，不能只按单个旧 run 各算一份互相覆盖的 pending 值。

`test_self_initiative_memory.py:642–744` 使用 mixed recovery、105 条尾部、空白正文和当前/引用语音，要求看见本次 staged completion、只解码真实当前语音、保持真实剩余计数。429–505 覆盖 claim/complete/interrupted/stale 的真实 WAL 插入与原水位。任务书 §6 现已明确单 run 的 projected completion；建议 V47 同时点名 mixed 多 run、跨 owner、末尾空白/语音和失败空洞，避免仅测 state.updated_at。

### B2-06：保留真实原子单元，不用“批处理”概括

| 路径 | 当前真实原子单元 | 不应变成的边界 |
|---|---|---|
| Live Memory | `mutate_validated_claim` 的单 claim fact/evidence/relation + mutation receipt + 原 job claim fence；`worker.py:293–344` 多 claim 逐项调用，随后 `jobs.complete` | 不是整批模型输出或同 event 全 claims+job done 一个事务；后项失败不得抹掉前项原 receipt |
| 通用 mutation | version/source transition/new fact/copy/state event/receipt；若传 self_reflection_result，同次插入 ReflectionResult | 不可先 commit fact 再写 mutation receipt |
| Self Reflection | 单 proposal/episode mutation+ReflectionResult；初始 paid checkpoint、进度 expected JSON CAS、run 完成/水位各有原阶段 | 不将全 cycle 合一大事务；不因每日预算已用完拒绝原 paid 结果纯 DB 收尾 |
| Dream | `_commit_cluster_decision:367–519` 的整个 cluster 全 actions、operation source/result、mutation receipt、checkpoint、preview consume | 不是每 action 各自提交；第二 action 失败不能留下第一 action |
| Dream 回退 | `rollback_dream_operation:665–881` 单 operation 的恢复/删除新增项/回退 receipt；`rollback_run` 逐 operation | 不把整 run 所有 cluster 回退包装为一个数据库事务；已完成回退可恢复 |
| Rebuild | stage_claims 的同 item 全 proposals+item staged；采用单 proposal 的事实+proposal receipt；sweep 单页 job+item finalization | 采用与 sweep 不合并成整 run；sweep 页中途失败不得只留下前半页 |
| Embedding | `_complete_page` 内各原 claim CAS、vector upsert、job 完成在同页事务 | 文本/hash/HTTP/向量编码不进 writer；不同页仍可独立完成 |
| Maintenance / hygiene | 原候选页各状态事件与事实变更同提交；跨页可独立 | 不拆坏单 fact 的状态/事件原子性，不把全库维护合并成大事务 |
| Dream plan | 原 source/owner 核验后 run+全部 clusters 原子创建；原 savepoint 局部失败边界 | SQL 分 256 行 INSERT 不等于 256 行可分别提交 |

当前任务书 §6 已吸收主要矩阵；实施时把它作为方法边界核对，不增加通用批处理框架。

### B2-07：full evidence、absence 与真正新增 ID 不可混作可删缓存

`repository.py:353–380/1005–1262` 准备全部 readable evidence，复制同时证明源和 replacement target 的资格；去重和 `INSERT RETURNING` 得到真实新增 evidence ID。`mutation/service.py:638–662` 将实际新增 evidence/relation ID 返回，`DreamRepository.commit_operation:1067–1108` 保存供回退使用。删 first-DML listener/旧 BUSY 循环时，不能连这些领域结果一起删。可以将 session.info 汇总改为直接返回领域结果，但不能用“本次请求的 ID”替代实际新插入 ID。

Dream 回退还核原 source/result signature、真正属于该 operation 的新增 evidence/relation、没有后续 committed operation 依赖。只 CAS facts.updated_at 不覆盖新增 relation/后续 operation 的缺席依赖。`MemoryFactService._clear_resolved_related_conflicts:1022–1085` 也依赖两跳相关 contradiction 和当前 active 对端；只核最初旧 fact IDs 不够。完整的是实际局部依赖，不是把整张图或全库变成每次 mutation 的门槛。

### B2-08：读名称不代表只读，prepared core 也须检查实际调用

`DreamRepository.ready_preview:1174–1204` 会把不匹配 paid preview 标 stale；是读选择+条件失效的复合操作。`preview_cluster` 本身也在模型后保存 paid preview。查询必须仍可并发，失效/保存才短写，不能按方法名整体迁 read_session 或整体塞入 writer。

`MemoryRebuildService.plan:319–348` 接受 supplied session，但仍无条件调用 ledger.maximum_event_id/count_rebuild_candidates 和 JSON/hash；相比 `plan_rebuild_core:87–112` 已要求 supplied session 必须有 prepared。已检索当前 src 消费者：`services/commands/memory.py:130` 不传 session，因此本轮未证实现存在线上“writer 里扫历史”。建议 W54 明确统一走已有 prepared core 或删除无实际消费者的 session 形态；不要留下一个未来 writer 调用会另开 reader/扫全量的同名路径。无需新 plan 框架。

`SelfReflection.control.claim/finish` 和 Dream finish/defer 仍在旧 begin 内解析/拼 report/statistics JSON。小 indexed RMW 可以直接短 writer；大报告 snapshot/编码、正文渲染不应因替换入口提前持锁。`_scan_event_batch` 初次部署只设 max event baseline，不重放全部历史；claim_due 还要保原失败窗口、真实 context 和单大事件完整性。

### B2-09：Embedding 是现有可复用范例，不能把派生清退都称短写

`embedding/jobs.py:_prepare_page/_enqueue_prepared` 在 reader 取输入、外部算 hash，writer 核 fact 字段/资格和原 job token；`claim` 在 writer 重新取 now。`_complete_page:365–479` 准备 digest，writer 只让原 profile/fact/hash/processing/updated_at/attempts claim 完成；内容变更仅归还原 claim，不覆盖新 claim。`embedding/worker.py:90–158` HTTP、build、codec 在 repository writer 之外。

仍需迁 `embedding/repository.ensure_profile/purge_old_profiles`、`jobs.delete_for_old_profiles` 的裸 begin。profile purge 可能 FK cascade 大批 vectors/jobs，不能从“一条 DELETE”推出持锁短；它本来是显式维护。`runtime.purge_old` 当前先删旧 jobs，再删 profiles 是两个阶段，不可未经语义决定就声称全清退整体原子。

FTS 全量 rebuild 有 `fts.py:358` 与 `quality/hygiene.py:261` 两入口，是实际 SQLite rebuild 原子维护。统一 admission 可覆盖它，不能把它拆为普通数据页来承诺短锁。`quality/hygiene._invalidate_page:175–242` 还在旧 snapshot 里渲染/规范化完整证据；新 writer 前准备后，须核相关 evidence/event/tool 的实际资格和新增有效来源，不能只有 fact.updated_at。

### B2-10：取消和未知提交保护原结果，测试注入点要迁

`_commit_prepared`、`mutate_validated_claim` 发生异常后用原 idempotency/claim fingerprint 查 receipt；确认已提交可返回原结果，取消仍传播。Dream 失败收尾按真实 committed operation 恢复 operation_count，不重新运行 paid 模型。`MemoryJobRepository.complete` 认原 DONE receipt；`fail` 和 mutation 内的 fence 保原 claim ownership。

旧测试中 `prepare_evidence_write` 内同步打开另一个 writer 来制造 517；新架构若把这个 hook 留在 admission 内，会测试自身等待自身。应将竞争安排在新“准备完、接纳前”的真实边界，另用独立连接持 writer 验证 admission/取消。删除“恰好 4 次重试/5 个 session”的旧实现断言，保原 ID、模型调用次数、paid budget、已提交结果、取消和不确定 ack 的业务断言。没有必要为保这些测试留下第二套重试。

## 2. 范围、锁与性能复核

B 域未发现自建 SQLite 文件/engine/connect；它们经装配的 `Database` 操作主库及其 FTS、embedding BLOB、run/receipt 表。`embedding/query_cache.py` 是进程内 TTL/vector request 合并，不是数据库缓存文件；其 asyncio.Lock 只保护容器，网络 factory 在锁外。`embedding._start_lock` 只串初始化；Dream/Self/Maintenance process_lock 是各 worker 生命周期/执行串行，不是统一 DB writer。这些锁不应被替换成一个跨模型周期的全局 DB 锁。

边顺序应是现有业务 process_lock → 模型/外部准备 → reader 完毕 → 短 writer；embedding 后置调度、report Social 外发、paid Provider 请求都在 writer 之外。B 内写 helper 不新建 owner，不拿着 writer 再等待另一个 writer。原历史 binding/alias/owner proof 不能按今日账号状态重建；同人 merge 是等价 owner，账号 transfer 不是把旧来源转送给新主人。Person/Space 身份材料及外层 privacy 的跨域收口由 A/C 轮次继续复核。

保留 query/FTS/向量/ACL/health/audit 的并发 reader。长扫描 reader 应及时退出结束 WAL snapshot；不必把所有单 SELECT 改成显式 BEGIN。读取完整已选证据与 related predicate 的 SQL 要分批/批量，避免 source 复核退化成逐 fact/person/evidence 小查询。现有 `test_dream_plan_transactions`、receipt_finalization read budget、embedding page guards 是基线；不新增固定百分比性能门槛。记录同负载下到达→响应、admission 等待、真实持锁、SQL/正文读取量即可。现有延迟用户可接受，不要用整个 callback 套 IMMEDIATE 换取表面统一。

## 3. 本轮测试阅读与具体缺口

以下是本轮实际阅读，**均未执行**。F/T 的含义与源码一致；前轮已全文测试并不自动计入本轮。

| 测试 | 本轮 | 已核的真实场景 / 待补 |
|---|---|---|
| `unit/test_self_initiative_memory.py` | F，1494 行 | 真实 WAL scan/claim/complete/recover，原 paid receipt 窗口，晚到 receipt、large source、mixed recovery staged completion、105 条尾部、raw owner 流与原回执；不制造人类 event、不重选旧窗口预算 |
| `unit/test_memory_person_identity.py` | F，768 行 | 人物整族隐私；保别人内容与原 Social receipt；merged read/SELF private；paid Dream proof；历史 mention 经 transfer 后仍归原人；实际 erased/support source 才删 initiative receipt；原 Person 自省水位不合并 |
| `unit/test_self_initiative_sources.py` | F，165 行 | 多个新不可读 evidence 不挤掉老可读来源；精确 140 facts 不受候选页上限；当前 lifecycle/visibility/source 真实核验 |
| `unit/test_person_identity.py` | T，85–132 | 并发 merge 无环；准备后 merge 重新取整族。其余 config/route/rollback 归 A/C 接力，不计全文 |
| `unit/test_memory_mutation.py` | T，290–325、579–669、915–1083、1156–1314 | selector 目标隔离；fact/evidence authority；reassign 原子新版本；并发原请求一次；finalize 失败回滚；embedding 失败保事实；merge/metadata/contest/invalidate/restore。未新增“定位后另一同 key 不拒绝”的竞态用例 |
| `unit/test_mutation_concurrent_resolution.py` | F | tool/worker 可并行 prepare，各自独立事实/receipt；不能新增全局 mutation 锁 |
| `unit/test_mutation_snapshot_upgrade.py` | F | tool/worker 原 mutation UUID 与 resolve 只一次；实际 5/517；unknown commit/rollback ack；取消；transfer 保原 owner、hide 拒绝旧来源。固定 retry 次数应随旧机制删除 |
| `unit/test_memory_paid_tail.py` | F | Self 已花原每日额度仍按 checkpoint 完成；同人 merge 保原 paid 结果；Dream paid preview 与新模型预算区别；job complete 真实 WAL 与 unknown ack、取消、原 attempts |
| `unit/test_memory_maintenance_evidence_snapshot.py` | F | 原候选/cutoff，延期到期跳过；两跳冲突；整页完整准备；真实 WAL；取消/unknown commit/rollback 不盲重放 |
| `unit/test_dream_busy_recovery.py` | F | 原 cluster/operation/mutation ID；模型一次；unknown commit 用实际 operation；6/262 不当 BUSY 重备；取消与 ORM flush |
| `unit/test_dream_input_snapshot.py` | F | 隐藏/等数量替换真实 evidence 使原 input proof 失效；saved proof 真正 merge 且完整复制 |
| `unit/test_dream_owner_projection.py` | F | PersonGroup/Self-private/Self-group 输入本地 alias；冷恢复在预算上限下用原 paid schema/proof，transfer 不重买模型 |
| `unit/test_dream_plan_transactions.py` | F | 1/5/10/300 cluster 批量写；source version/owner/delete 后无半 run；prepare 不占 writer；savepoint failure；260 recovery 按 committed operation 恢复 |
| `unit/test_dream_recovery_snapshot.py` | F | 空恢复不占 writer；实际 writer 后读取新 committed operation；统计/cluster/preview JSON 首写前编码 |
| `unit/test_rebuild_receipt_finalization.py` | F | 1/10/100/1000 每页 SQL；129 边界与别 run；新增 live job 的 pending/processing/done/failed/own_done；source/review/selection/generation/revision；20/20/5 采用与独立 sweep ticks；failed selection 不扩权 |
| `unit/test_rebuild_trusted_receipts.py` | F | 当前 person/binding disable、rename、merge、transfer 不重写真实历史 mention；forget 使真实来源失效；下一页独立推进 |
| `unit/test_rebuild_privacy_preparation.py` | F | alias 批量准备、相关 selection phantom 在 count/max 不变时仍被删除；caller writer 缺准备失败；父事务回滚；原 selection body 清除与预算保留 |
| `integration/test_self_reflection_checkpoint_privacy_race.py` | F | 首次 checkpoint 拒绝迟到 erased event/tool；expected JSON CAS 保 privacy；真实 517；未引用私人来源的空输出可完成 |
| `integration/test_self_reflection_checkpoint_recovery.py` | F | 首条已提交后第二条中断保原索引和结果；模型一次；旧 NOOP/isolated candidate paid 输出不重解释 |

仍需实施新增或第 3 轮追踪的用例：

1. 通用 selector 在定位前/后新增匹配项的不同预期；原固定 ID 不被另一同 key 事实改变；本次 request fingerprint 不因解析结果被重写。
2. Rebuild proposal 准备完成后，writer 前 source edit/hide/delete、claim body/target/review/selection/live-job 的各自变化；断言事实和原 proposal receipt 一起拒绝/一起写，不能只检查 sweep。
3. Dream cluster 第二 action 写失败全 cluster 回滚；sweep 页第二 item/job 写失败全页回滚；Self 第二 proposal 失败却保第一条原 receipt；Live 同 event 第二 claim 失败保第一条。按真实原子矩阵验证，不能统一写成“全部失败都回滚”。
4. Self 新 prepare/commit 分离后 mixed 多 run 的 projected completion、失败空洞、raw owner、generation/新增相关 gap、晚 receipt 竞争；只有 state.updated_at 不足。
5. maintenance/hygiene 准备后新增有效 evidence/relation，旧主行时间及 evidence 数量保持不变的替换；只核实际相关集合，不加全库 revision。
6. `read_session` 不 commit/flush、离开释放快照，supplied writer helper 无隐式第二连接；旧 ready_preview 失效与纯查询分流；空页、无变化只读快路不排 admission。

`test_memory_dream.py`、`test_memory_rebuild.py`、`test_memory_v2.py`、`test_memory_partition.py`、`test_memory_retrieval.py` 及其余 extraction/lifecycle/authority/worker shutdown 等大文件本轮没有全文阅读；只因已读测试引用其 helper 不应记全文。它们仍由全局测试 coverage 逐项接力。前轮已经全文的 embedding claim/cost、read policy、internal selection 等测试按前轮记录，不在本文重复宣称独立验证完成。

## 4. 与第 1 轮交叉核对

独立读取后才读 `review-b.md`。对其 22 项结论逐项核对：本轮未推翻实际写入口、完整证据、paid/receipt、FTS 维护窗口及纯读分流的结论。B2-01 把前轮 selector 待定语义收敛成基线的固定 ID 行为；B2-05 补“本次完成后的投影”与 mixed recover；B2-06 将 worker 真实单 claim 原子性写清；B2-08 补 class plan 与 prepared core 的 supplied session 差别。Rebuild proposal 与 sweep、privacy phantom 都由本轮自己读取真实实现确认，未依赖前轮概述。

本文是第 2 轮 B 域复核，不代表全仓三轮完成；没有实现通过/性能改善/上线结论。落盘期间 root 已将工作树快进到 `c2ce7da66ab54efe192993b9615df48266a62137`；B 域源码未变，本报告仍按上述 `cdb49ead` 基线记账，新 main 差异由第 3 轮单独审查。

## 5. 全文件复核分类

下表路径均相对 `src/yuki/memory/`。F 是本轮源码全文；T 行注明已读函数与剩余排除；S 只做结构分类，不能写作全文。所有文件的 AST/import/call 候选均复查，未发现 B 自建 engine 或独立数据库文件。

本轮分类：F 33, T 11, S 43，合计 87 文件。

| 文件 | 行数 | 本轮 | 阅读范围/排除依据 |
|---|---:|---|---|
| `__init__.py` | 65 | S | 类型导出/lazy import；无运行时 DML |
| `audit.py` | 199 | T | health 全读；其余 repository 只读委托和格式结构复查 |
| `authorized_scope.py` | 259 | S | 授权 SELECT/EXISTS 表达式和目标投影；无 session owner/DML |
| `claim_processor.py` | 186 | F | 全文：实际 DB/委托或来源语义已核 |
| `context.py` | 100 | S | 检索组合；只调用 query/retriever，无 DB 写委托 |
| `dream/__init__.py` | 1 | S | 类型导出/lazy import；无运行时 DML |
| `dream/db_models.py` | 333 | S | ORM/schema 声明、索引外键；无运行时事务 owner |
| `dream/models.py` | 285 | S | 纯类型/校验/格式/计算/Provider 或只读接口组合；AST 无独立 DB 写 owner |
| `dream/planning.py` | 89 | S | 准备 DTO/owner 分组和 revision 计算；无 DB 调用 |
| `dream/repository.py` | 1426 | T | 全部写入口、准备/owner/plan/recovery/preview/checkpoint/rollback；候选/health 分页查询结构排除 |
| `dream/service.py` | 1034 | F | 全文：实际 DB/委托或来源语义已核 |
| `dream/worker.py` | 242 | T | 165–242 实际 drain/failure 及未知提交恢复；start/管理包装和定时结构复查 |
| `eligibility.py` | 142 | S | 来源/claim 纯资格 SQL 表达式与 pure policy；无 execute/DML |
| `embedding/__init__.py` | 41 | S | 类型导出/lazy import；无运行时 DML |
| `embedding/codec.py` | 52 | S | 纯类型/校验/格式/计算/Provider 或只读接口组合；AST 无独立 DB 写 owner |
| `embedding/fake.py` | 81 | S | 纯类型/校验/格式/计算/Provider 或只读接口组合；AST 无独立 DB 写 owner |
| `embedding/health.py` | 66 | F | 全文：实际 DB/委托或来源语义已核 |
| `embedding/jobs.py` | 616 | F | 全文：实际 DB/委托或来源语义已核 |
| `embedding/metrics.py` | 68 | S | 纯类型/校验/格式/计算/Provider 或只读接口组合；AST 无独立 DB 写 owner |
| `embedding/models.py` | 143 | S | 纯类型/校验/格式/计算/Provider 或只读接口组合；AST 无独立 DB 写 owner |
| `embedding/provider.py` | 35 | S | 纯类型/校验/格式/计算/Provider 或只读接口组合；AST 无独立 DB 写 owner |
| `embedding/query_cache.py` | 102 | F | 全文：实际 DB/委托或来源语义已核 |
| `embedding/qwen.py` | 260 | S | 纯类型/校验/格式/计算/Provider 或只读接口组合；AST 无独立 DB 写 owner |
| `embedding/repository.py` | 395 | F | 全文：实际 DB/委托或来源语义已核 |
| `embedding/runtime.py` | 155 | F | 全文：实际 DB/委托或来源语义已核 |
| `embedding/semantic.py` | 135 | F | 全文：实际 DB/委托或来源语义已核 |
| `embedding/text.py` | 90 | S | 纯类型/校验/格式/计算/Provider 或只读接口组合；AST 无独立 DB 写 owner |
| `embedding/worker.py` | 158 | F | 全文：实际 DB/委托或来源语义已核 |
| `enums.py` | 259 | S | 纯类型/校验/格式/计算/Provider 或只读接口组合；AST 无独立 DB 写 owner |
| `errors.py` | 11 | S | 纯类型/校验/格式/计算/Provider 或只读接口组合；AST 无独立 DB 写 owner |
| `event_extractor.py` | 257 | S | SubjectContextBuilder 只读 + structured model；无 writer，模型调用必须保持事务外 |
| `extraction.py` | 136 | S | 纯类型/校验/格式/计算/Provider 或只读接口组合；AST 无独立 DB 写 owner |
| `fts.py` | 421 | F | 全文：实际 DB/委托或来源语义已核 |
| `job_claims.py` | 38 | F | 全文：实际 DB/委托或来源语义已核 |
| `maintenance.py` | 165 | F | 全文：实际 DB/委托或来源语义已核 |
| `metrics.py` | 74 | S | 纯类型/校验/格式/计算/Provider 或只读接口组合；AST 无独立 DB 写 owner |
| `models.py` | 622 | S | 纯类型/校验/格式/计算/Provider 或只读接口组合；AST 无独立 DB 写 owner |
| `mutation/__init__.py` | 29 | S | 类型导出/lazy import；无运行时 DML |
| `mutation/models.py` | 268 | F | 全文：实际 DB/委托或来源语义已核 |
| `mutation/repository.py` | 203 | F | 全文：实际 DB/委托或来源语义已核 |
| `mutation/service.py` | 2969 | T | 全部实际写和准备/定位/源/owner/权限函数；其余 pure quote/provenance/result 投影结构复查 |
| `partition.py` | 288 | F | 全文：实际 DB/委托或来源语义已核 |
| `projections.py` | 212 | F | 全文：实际 DB/委托或来源语义已核 |
| `quality/__init__.py` | 1 | S | 类型导出/lazy import；无运行时 DML |
| `quality/audit.py` | 436 | T | run 及 _query 全读；_checks 全部 AST/字符串结构只读查询排除，未逐字重读完整 SQL 文本 |
| `quality/event_evidence.py` | 108 | F | 全文：实际 DB/委托或来源语义已核 |
| `quality/hygiene.py` | 271 | F | 全文：实际 DB/委托或来源语义已核 |
| `quality/models.py` | 41 | S | 纯类型/校验/格式/计算/Provider 或只读接口组合；AST 无独立 DB 写 owner |
| `query.py` | 94 | S | 纯类型/校验/格式/计算/Provider 或只读接口组合；AST 无独立 DB 写 owner |
| `ranking.py` | 250 | S | 纯类型/校验/格式/计算/Provider 或只读接口组合；AST 无独立 DB 写 owner |
| `read_scope.py` | 379 | F | 全文：实际 DB/委托或来源语义已核 |
| `rebuild/__init__.py` | 8 | S | 类型导出/lazy import；无运行时 DML |
| `rebuild/models.py` | 189 | S | 纯类型/校验/格式/计算/Provider 或只读接口组合；AST 无独立 DB 写 owner |
| `rebuild/privacy.py` | 254 | F | 全文：实际 DB/委托或来源语义已核 |
| `rebuild/receipt_finalization.py` | 352 | F | 全文：实际 DB/委托或来源语义已核 |
| `rebuild/repository.py` | 1131 | T | create/transition/pause/ensure/stage/review/finish/retry/purge/privacy 全部写；只读统计/过滤/health 结构排除 |
| `rebuild/service.py` | 951 | F | 全文：实际 DB/委托或来源语义已核 |
| `rebuild/trusted_sources.py` | 189 | F | 全文：实际 DB/委托或来源语义已核 |
| `rebuild/worker.py` | 61 | F | 全文：实际 DB/委托或来源语义已核 |
| `repository.py` | 2154 | T | transaction/完整 evidence/copy/全部 fact/job 写/claim/fence/定位；只读 list/health/target SQL 结构排除 |
| `retrieval.py` | 578 | T | 144–322 authorized 实际查询及网络后重读；旧 target 检索/排名委托结构排除 |
| `runtime/__init__.py` | 13 | S | 类型导出/lazy import；无运行时 DML |
| `runtime/capability_view.py` | 61 | S | 纯类型/校验/格式/计算/Provider 或只读接口组合；AST 无独立 DB 写 owner |
| `runtime/contract.py` | 53 | S | 纯类型/校验/格式/计算/Provider 或只读接口组合；AST 无独立 DB 写 owner |
| `runtime/partition_lookup.py` | 34 | F | 全文：实际 DB/委托或来源语义已核 |
| `runtime/query_plane.py` | 191 | S | 纯类型/校验/格式/计算/Provider 或只读接口组合；AST 无独立 DB 写 owner |
| `runtime/resolver.py` | 77 | S | 纯类型/校验/格式/计算/Provider 或只读接口组合；AST 无独立 DB 写 owner |
| `runtime/turn_session.py` | 96 | S | 纯类型/校验/格式/计算/Provider 或只读接口组合；AST 无独立 DB 写 owner |
| `self_origin.py` | 287 | F | 全文：实际 DB/委托或来源语义已核 |
| `self_reflection/__init__.py` | 24 | S | 类型导出/lazy import；无运行时 DML |
| `self_reflection/control.py` | 455 | F | 全文：实际 DB/委托或来源语义已核 |
| `self_reflection/db_models.py` | 32 | S | ORM/schema 声明、索引外键；无运行时事务 owner |
| `self_reflection/initiative.py` | 281 | F | 全文：实际 DB/委托或来源语义已核 |
| `self_reflection/models.py` | 183 | S | 纯类型/校验/格式/计算/Provider 或只读接口组合；AST 无独立 DB 写 owner |
| `self_reflection/reporting.py` | 96 | T | 62–96 原 receipt 查证/reader/独立 Social 外发；报告格式结构排除 |
| `self_reflection/repository.py` | 1235 | F | 全文：实际 DB/委托或来源语义已核 |
| `self_reflection/service.py` | 635 | F | 全文：实际 DB/委托或来源语义已核 |
| `self_reflection/worker.py` | 272 | T | 91–222 cycle/完成恢复/report/health；start/定时/错误文本结构排除 |
| `service.py` | 1404 | F | 全文：实际 DB/委托或来源语义已核 |
| `subjects.py` | 481 | T | 193–481 全部 DB 目标/历史归属/名称读取；1–192 纯上下文对象与选择结构排除 |
| `targets.py` | 97 | F | 全文：实际 DB/委托或来源语义已核 |
| `temporal.py` | 64 | S | 纯类型/校验/格式/计算/Provider 或只读接口组合；AST 无独立 DB 写 owner |
| `temporal_filter.py` | 36 | S | 纯类型/校验/格式/计算/Provider 或只读接口组合；AST 无独立 DB 写 owner |
| `testing.py` | 18 | S | 纯类型/校验/格式/计算/Provider 或只读接口组合；AST 无独立 DB 写 owner |
| `tool_intent.py` | 74 | S | 纯类型/校验/格式/计算/Provider 或只读接口组合；AST 无独立 DB 写 owner |
| `validation.py` | 234 | S | 纯类型/校验/格式/计算/Provider 或只读接口组合；AST 无独立 DB 写 owner |
| `worker.py` | 358 | F | 全文：实际 DB/委托或来源语义已核 |
