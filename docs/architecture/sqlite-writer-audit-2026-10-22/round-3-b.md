# SQLite 全覆盖审查：第 3 轮 B 域终审与新 main 差异

基线：`c2ce7da66ab54efe192993b9615df48266a62137`。本轮独立复核 `src/yuki/memory` 全部 87 个 Python 文件的数据库入口、委托、来源与排除分类；先读源码，再核 `review-b.md`、`round-2-b.md` 和主任务书。Memory 源码在 `cdb49ead..c2ce7da6` 间没有变化，前两轮结论仍保留其旧基线，不能因此把前轮阅读算成本轮全文。另独立检查这次 main 的全部 36 个变更文件，重点核新增 Main 资料只读链、quiet 反馈保存链和参与库 pin。

本轮只写本文，没有改产品、参与库工作区或生产；没有执行测试、调用真实模型或发送平台消息。阅读前已核 AGENTS、`development-contract.md`；对照了 Memory change/rebuild/self-reflection/embedding/quality 现行文档。现有延迟用户接受，本轮不以旧线上争锁报告建立新的严重度或完成门槛。

范围分级：F 是本轮全文阅读；T 是本轮读取表中具名实际函数/范围，再核其余 AST/import/call/SQL 类别；S 是结构排除，**不是全文**。长文件中的纯 DTO、格式、定时包装或只读 SQL 没有因为“全范围”三个字就被记录为全文。末尾逐文件表与测试表明确边界。

## 1. 最终结论与剩余细化

35 个 U 单元、73 项 W 任务与 50 个 V 场景已经覆盖本轮确认的主库 Memory 写入家族。本轮没有发现需要再增一个持久化框架或新领域单元的遗漏；有两处明确可以删除的旧抢锁写法，以及几个应在既有任务/测试里点名的交错。

### R3B-01：统一入口之后，删掉仅为取得 writer 的空 UPDATE

`dream/repository.py:432-440` 在 `optional_session(write=True)` 后执行 `UPDATE memory_dream_runs ... WHERE false()`；注释明确只为 legacy SELECT-only caller 抢 SQLite writer。公共 `BEGIN IMMEDIATE` 已提供这个物理边界后，这段空 UPDATE 应删除。保留 441-474 的真实 source/双 owner 重核、477 起的局部 SAVEPOINT，以及 run+全部 clusters 一次提交；256 行 INSERT 页是 SQL 分页，不是业务可独立 commit 的页。归 W46/W50，而非新增第二种锁。

`job_claims.py:26-38` 的 `updated_at=updated_at` UPDATE 同样用来抢短 writer 并验证原 job claim。实际唯一调用在 `mutation/service.py:1724`，处于该 claim 的来源重核之后、mutation receipt reserve 之前；原条件是 job ID/event ID/processing/updated_at（18-23）。新 IMMEDIATE owner 内可以用同 writer 的当前条件读保留 claim 事实，删除仅为锁取得而做的 no-op 写；或者将条件并入已有真实写入。不能删除原 claim 资格，更不能只在旧 reader 中检查。源码与迁移中未见 memory_jobs 专用 trigger，实施时仍须核最终 schema/调用者。

这是计划中的删除候选，尚未修改或验证性能。受影响测试 `test_mutation_snapshot_upgrade.py:143-196` 的旧 fence 注入点应迁到准备结束、admission 之前，避免让 hook 在已持全局 writer 时同步等待另一个 writer。

### R3B-02：selector 的固定 ID、相关集合 phantom、完整来源是三件事

独立读取 `mutation/service.py:1786-1995/2322-2391` 后确认：selector 在准备阶段按目标、状态、规范化 key/category/content 唯一定位；随后 1891/1900 固定 fact/merge fact ID 并移除 selector。`_commit_prepared:1264-1463` 核原事实签名、目标与来源，没有重跑 selector。任务书现在保这一语义正确。V46 应明确：定位后新增另一同 key 事实本身不拒绝原 ID；定位前歧义仍按原规则处理。

与之不同，Rebuild privacy `privacy.py:38-171` 依赖“当前所有相关 selection”的成员/缺席。`apply_forget:174-254` 只核准备过的旧 run IDs 的 token。两阶段方案必须复核原相关谓词，发现新匹配项后重新准备；不能只核旧 IDs、全库 count/max，或让 `event_ids=()` 丢掉空交集语义。现有 owned `forget_people` 在旧 snapshot callback 内会重新 prepare（repository.py:1010-1016），不能把其安全性推给拆开的新方案。

同样，Memory evidence copy（repository.py:1005-1176）在 source 与 replacement owner 两侧核可读资格；真实 `INSERT RETURNING` IDs 用于 Dream 回退。事实行签名或 evidence 数量相同，不证明来源或关系没变。W43-W45/W55 已有归属；不需要引入通用全库 revision。

### R3B-03：Self 的 projected completion 要包含同批多 run

`self_reflection/repository.py:836-875` 先在 ORM 把 run 标 completed，再 `_advance_state:189-275` 从相关 gap 和剩余 evidence_content 计算水位/计数。`autoflush=False` 不妨碍 identity map 返回本 session 已 staged 的 completed 对象；它也没有使正文扫描自动适合 IMMEDIATE writer。

`recover_stale_runs:897-925` 在一个 session 逐 run 调用 `_recover_processing_run:943-1018`，可能包含多个 ordinary run 和 initiative window。新 reader 准备必须包含本次准备提交的整个逻辑完成集合；不能每个旧 processing run 各算独立结果，最后互相覆盖水位。还要保失败 gap、raw Person/Space owner、generation、晚 receipt 和空白/语音尾部的实际计数。

主任务书 §6.4 已明确单 run 完成后逻辑结果；建议在 W48/V47 直接点名 mixed 多 run，避免实现者只完成单 run 版本。`test_self_initiative_memory.py:642-744` 已有该类领域基线；实施后要把竞争注入安排在新的准备/接纳边界。不得用影子水位、全库 revision 或另一份计数账本绕过真实核验。

### R3B-04：原子单位已经正确，故障测试应断言不同结果

| 业务 | 源码确认的一次事务 | 实施时必须验证的故障 |
| --- | --- | --- |
| Live Memory | worker.py:293-344 每个 claim 调一次 mutate_validated_claim；该 claim 的 fact/evidence/relation+mutation receipt+原 job fence；之后 jobs.complete | 第二 claim 失败，第一 claim 原 receipt 保留；不能要求整 event 全回滚 |
| Self | service.py:225-269 每条 proposal/episode 独立 mutate_resolved；mutation/service.py:1443-1462 同次写 ReflectionResult+receipt | 第二条失败保第一条索引/结果；重启不用原付费输出再买模型 |
| Dream | service.py:350-362 的单 apply_evidence_write 包住 _commit_cluster_decision:367-519 全 actions、operation/source/result/checkpoint/preview consume | 第二 action 写失败，整个 cluster 无半个 operation/receipt/preview 消费；与逐 operation rollback_run 区分 |
| Rebuild | stage_claims:329-400 的同 item 全 proposals+item；采用 service.py:825-865 的单 proposal fact+proposal receipt；receipt_finalization.py:253-352 的整页 | 采用和 receipt sweep 各自验证；页第二项失败不得只留下前半页 |
| Embedding | jobs.py:430-486 当前页的原 claim 完成与 vectors upsert 同提交 | late claim/profile/fact 变更不覆盖新结果；跨页独立 |
| Maintenance/hygiene | maintenance.py:110-151 原候选页；hygiene.py:175-236 事实状态与 state event 同次提交 | 源变化不失效有新有效证据的事实；状态和审计无半提交 |

`test_memory_dream.py:1095-1199` 的真实多 action 用例证明成功路径、实际消费与恢复；`1203-1234` 是 worker schedule 的取消/数据库异常，并非“第二 action 写失败”的覆盖。不能因测试名含 atomic 就将新交错视为已验收。现有 Self checkpoint recovery 有部分成功基线；两种相反的原子预期应分别保留。

### R3B-05：Rebuild 的采用、sweep 与完成判定各自有边界

`rebuild/service.py:724-823` 在 callback 外核源 hash、live receipt、eligibility、claim target；825-862 的 callback 当前重新读批准状态、run 状态及当前管理员权限，并同次写 fact/proposal receipt。它没有在 callback 内逐项重核准备时 source/proposal body/target/run selection 的全部依赖。这是待防的源码竞态，不是本轮复现的线上事故。W54/V50 要把“准备完成后、取得 writer 前”的源 edit/hide/delete、claim/target/review/selection/live receipt 变化点名；不能只靠随后 sweep 阻止 item 完成来掩盖事实已经采用。

`receipt_finalization.py:161-352` 是另一条完整准备+短 writer 页流程：actual source、原历史身份、selection token、item/event/conversation、unfinished proposal、原 live receipt 均参与重核。它不逆向修复先前采用的事实。`process_commit_once:915-934` 的剩余 commit/review/item receipt/item 状态计数是当前业务完成判断；保留真实轻量条件，不额外规定所有 proposal 必须批准，或重扫全部 JSON/hash 才允许完成。

`MemoryRebuildService.plan` 的 supplied session 形态仍应沿已有 prepared core 收敛（见 R2B-08），没有必要新增 Plan 平台。

### R3B-06：派生数据清理与显式全量维护不要混成短写承诺

`fts.py:358-369` 与 `quality/hygiene.py:261-271` 都是实际 FTS `rebuild`；后者已在公共 immediate_session。迁入口后仍属于显式全索引维护，不能把 SQLite FTS 命令拆普通页并宣称同等原子性或低持锁时间。普通 fact DML 对 FTS trigger 的效果继续属于原事务，查询/health 不排 writer。

Embedding `jobs.py:127-211/285-320/365-486` 已有正文/hash 在外、writer 当前条件/原 claim CAS 的可复用实例；`repository.ensure_profile/purge_old_profiles` 和 `jobs.delete_for_old_profiles` 裸 begin 归 W52/W53。`runtime.purge_old:150-155` 当前先删 jobs 再删 profiles，是两阶段；单条 DELETE 的 FK cascade 不保证持锁短。不要新增每行独立读验证，或用 health/doctor 的远端请求包进 writer。

一个小而具体的查询改进也应随 W42/W43 处理：`service.delete_preference:1317-1332` 当前 `list_facts(limit=None)` 读取该人物所有 preference，再 Python 按 key 筛选和检查歧义。新 IMMEDIATE writer 不应机械保留这段全量正文读取；按原 target/kind/key 在 SQL 取所需候选与歧义即可。它不是新增 prepared DTO 或通用 selector 框架的理由。

## 2. 具名方法与任务归属复核

本轮未发现以下实际写家族缺少 U/W：

| 单元 / 任务 | 本轮核到的方法家族 |
| --- | --- |
| U24 / W42 | repository 的 mark_injected/create/transition/record_created/update/restore confirmation/review/add_relation/add/copy evidence；job enqueue/claim/claim_ready_batch/complete/fail；service 的 remember/apply_claim/explicit/correct/version/confirm/bundle/invalidate/contest/restore/merge/resolve_conflicts/delete_preference/admin confirm/quarantine |
| U25 / W43-W46 | mutation 的 prepare/commit/validated claim、source snapshot、fact/owner/selector/permission helper；receipt reserve/reserve_dream/finalize；Dream mutations 与 rollback 的领域结果 |
| U26 / W47-W49 | scan/baseline/claim_due/claim_initiative/cursor；request budget reserve/finish；cycle enqueue/claim/finish/delivered/resume；checkpoint/complete/fail/recover/advance/receipt cleanup；service paid output/逐项 mutation/report |
| U27 / W50-W51 | Dream plan/create/start/claim/reserve/finish/finalize/cancel/retry/defer/recovery；operation/source/result/checkpoint；preview save/ready(含 stale 写)/consume；rollback operation/run |
| U28 / W52-W53 | ensure profile、jobs enqueue/reconcile/recover/claim/complete/skip/fail/retry/delete old、vector upsert/profile cascade；worker/model与查询缓存、read health/remote doctor 分离 |
| U29 / W54-W55 | Rebuild create/transition/restart pause/ensure/stage/defer/fail/checkpoint/usage/review/proposal result/finish/retry/purge/forget；receipt sweep 与 run 完成；trusted source 当前历史归属 |
| U30-U31 / W56-W59 | maintenance、hygiene invalidate/purge/rebuild、schema/FTS effects；ACL/query/quality/audit/partition projection/read_session 的只读分流；原子状态事件/FK cascade 保留 |

锁顺序仍是现有业务 worker/process lock → 模型/历史/身份准备 → reader 退出 → 公共短 writer；embedding schedule、Social report 与远端 model 在 writer 外。`QueryEmbeddingCache._lock` 是进程内缓存容器锁，factory 在锁外；embedding `_start_lock` 是生命周期锁，不是 DB admission，也不应扩大到整轮后台周期。Memory 内没有自建 SQLite 文件或 engine/connect，均消费容器装配的主 Database。独立 SnapshotStore 另见 delta。

## 3. 本轮实际补读的测试与实施缺口

均为源码阅读，未执行；T 不代表整个文件全文。前两轮已有的测试覆盖继续保留，不在本轮虚构一次新运行。

| 测试 | 本轮范围 | 结论 |
| --- | --- | --- |
| test_memory_v2.py | F | fact+evidence FK 失败整体回滚；queue 真实 inbound/作者资格；worker CancelledError 传播 |
| test_memory_dream.py | T：590-790、1095-1234 | merge 回退保护真实新增 IDs/无关资源，真实多 action 成功/冷恢复，worker 取消；不覆盖第二 action 写失败 |
| test_memory_rebuild.py | T：379-490、520-677 | live receipt/source 改变发生在 process_commit_once 之前；fact/proposal receipt 原子、unknown ack 查原 proposal、持续 DB 错误保 paid extraction；不覆盖所有准备后竞态 |
| test_memory_partition.py | T：342-451 | 批量 direct membership、forget 生效、规范化完整同名候选；避免 writer 内逐个别名/模糊查询 |
| test_memory_retrieval.py | T：381-400 | FTS 缺失稳定错误、旧版本物理索引但不可召回；不能为删除异常而降级全表扫 |
| test_mutation_snapshot_upgrade.py | T：fence hook 143-196 的调用/结构 | 旧锁注入需迁准备/接纳边界，保原模型/ID/receipt，而非保固定重试次数 |
| test_self_initiative_memory.py | T：混合恢复 642-744 的场景与前轮引用交叉核 | mixed completion、105 条尾部、空白/语音应进入 V47；本轮不记录整 1494 行全文 |

具体待实施验证：固定 ID selector 前/后新增匹配项；同 writer helper 不隐式再开连接；Self mixed recovery 的投影完成集合；Rebuild proposal 与 sweep 各自的源/批准/选择/live receipt 交错；Dream 第二 action 与 sweep 页第二项失败；Live/Self 第二项失败保前项；FTS/cascade 显式维护持锁与普通查询并发；取消时实际 driver 操作退出与原结果确认。

现有 517 注入不能机械留在新 writer 内等待另一个同 Database writer；必须改到新准备/接纳边界或独立外部连接。否则测试自等不等于产品死锁。上述新增场景归已有 V03/V05/V10/V17/V46-V50，不要求新增同名测试文件或 50 个测试壳。

## 4. 新 main 的 36 文件独立 delta 复核

`git diff --name-only cdb49ead..c2ce7da6` 为 36 文件；Memory 目录 diff 为空。下表 Δ 是本轮直接读取变更 hunk，F 为新增测试全文，S 为文档/配置用途结构核验（不声称文档全文）。源码改变共 11 文件均直接读 delta，并追实际调用者；没有仅因 grep 未命中便宣称无 DB 影响。

新增运行资料链：container.py:502 绑定 → ChatService 的 reader callback → MainAgentBackend.participation_context:584-586 → TurnExecution._participation_context:819-830/prepare_request。`participation_ordinary.runtime_context:370-476` 只查原 admission 和当前人物 alias，投影已加载有限 controller 状态；不 hydrate/save，不取得 observer lock，不启动模型。只读仍需 read_session 迁移，不能整个新 helper 纳入 writer。

quiet 新读写链：`observe_main_response:479-541` 经原 admitted_scope → ordinary.current_admissions:527 → 同 activation/actor/thread/target 过滤 → 原 controller hint → `_save`。主库读 helper 返回后 session 已退出；独立快照保存仍为 `SemanticParticipationService._save:492-523` → AsyncSnapshotStore 单线程。它不属于新增主库事务，不把主 writer 持到独立 snapshot。原取消时先认实际 revision 再传播的合同未被改动。

`semantic_participation._sources_current:861-914` 新增 invalid source 身份批读时把 supplied session 继续传给 `actor_identity`；后者 `optional_session(write=False)` 复用传入 session。`actor_identity:23-39` 扩大有限事件/边界 actor 集，没有新 DB owner。将其局部投影错误替换成历史全扫描或新持久 owner map 都超出本次需要。

依赖由 `438f204f9ebe2b8c1b698b68c0f1b3ac8b4facef` pin 到 `addfcddecb09f93ad8846db508879c451bc4bc47`；pyproject 与 lock 一致，lock 只更新该 archive URL/hash，SQLAlchemy/aiosqlite 未改变。只读查本机参与库对象并独立运行 `git diff --exit-code old..new -- src/yuki_participation/store.py`，结果为空/0；变更集中在 controller/observer/participation/session/scheduling 等与测试/文档，未新增 store 数据库格式或写 owner。未 sync 依赖，未改参与库工作区。

| 文件 | 复核 | 对本任务的影响 |
| --- | --- | --- |
| .github/workflows/quality.yml | Δ | APT URI 修正；无 DB 边界变化 |
| config/SOUL.md | Δ | 发言风格；无 DB 入口 |
| config/autonomous-model.example.json | Δ | 删除 continuation_review_span；不新增 writer 配置 |
| docs/architecture/README.md | S/Δ | 导航/历史口径；无源码 owner |
| docs/architecture/Yuki-JEV稀疏复核与闲聊收束任务书-2026-10-22.md | S | 历史任务书；不作 SQLite 新架构已实施证据 |
| docs/architecture/autonomous-participation-model.md | Δ | 稀疏观察/原快路；无 DB owner |
| docs/architecture/main-agent-runtime.md | Δ | 正常请求尾部局部参与资料，不改 paid replay |
| docs/architecture/semantic-participation.md | Δ | 核资料读取、原范围/已选择输入与保存边界；非另一 DB 框架 |
| docs/operations/sparse-jev-implementation-20261022.md | S/Δ | 原实施/合成和 CI 记录不代替本轮测试运行 |
| pyproject.toml | Δ | 实际 pin 更新 |
| src/yuki/container.py | Δ | 注册 runtime_context 只读回调 |
| src/yuki/prompting/contracts.py | Δ | 提示改文，无持久化 |
| src/yuki/runtime/work_control.py | Δ | 输入提示改文；原 Work DB 边界未变 |
| src/yuki/services/agent_runner.py | Δ | 添加 read-only protocol 方法 |
| src/yuki/services/chat.py | Δ | callback 声明 |
| src/yuki/services/main_agent_backend.py | Δ | 原 runtime 绑定与 visible_event_ids 传递 |
| src/yuki/services/participation_identity.py | Δ/F | 全文 39 行，supplied session 与批量身份读 |
| src/yuki/services/participation_ordinary.py | Δ/T | runtime_context 与 quiet read/save 链；admitted_scope/event_scope 追读 |
| src/yuki/services/processor.py | Δ | 普通入口当前资格/配置读；现有 writer callback supplied session 保持 |
| src/yuki/services/semantic_participation.py | Δ/T | 新 wrapper、source 当前身份；_save/actual cancel 追读 |
| src/yuki/services/turn_execution.py | Δ | 正常请求边界 append；exact recovery/pause 不改原付费请求 |
| tests/integration/test_participation_foreground_cost.py | Δ | saved quiet 恢复、无新增补评 |
| tests/integration/test_participation_parallel_review.py | Δ | 已选择输入原资格；后到修正不重解释原 admission |
| tests/integration/test_participation_person_identity.py | Δ | 原人 merge、withdrawal 与单次批量 SQL；原 event author 不重写 |
| tests/integration/test_participation_projection_cost.py | Δ | 名字/问题投影；不代表 DB 测试已运行 |
| tests/unit/test_acp_adapter.py | Δ | 隔离环境变量，非 DB 改造 |
| tests/unit/test_open_group_semantic_qualification.py | F | 当前真实 admission/worker，语义开关与明确 @ |
| tests/unit/test_ordinary_input_handoff.py | Δ | 去旧 check_budget 参数；原 handoff 事实保留 |
| tests/unit/test_participation_context_scope.py | F | 持 observer lock 时仍可读、无 DML/IMMEDIATE、缺 scope 不 hydrate、merge 保原源 |
| tests/unit/test_participation_foreground_preparation.py | Δ | 取消保真实 snapshot revision、原输入恢复 |
| tests/unit/test_participation_ordinary_feedback.py | Δ | 不因状态变化购买观察；原 visible IDs/冷恢复 |
| tests/unit/test_participation_pipeline_retirement.py | Δ | 已选原 activation、去配额；不是新 DB 写队列 |
| tests/unit/test_participation_profile_upgrade.py | Δ | 去废配置保真实覆盖；文件热更不等 DB writer |
| tests/unit/test_participation_quiet_coverage.py | F | 仅覆盖已派发且同原 activation/actor；晚响应不扩大 paid 可见范围 |
| tests/unit/test_participation_runtime_tail.py | F | 正常请求尾部、固定前缀、签名配对、exact replay；不额外启动模型 |
| uv.lock | Δ | archive URL/hash 与 pyproject 对齐；DB 驱动未变 |

新增测试源码提供边界实例，**本轮没有运行这些测试**。新 main 不推翻本任务书 U19/W35/W41 的主库 feedback → 独立快照边界；W58/read_session 迁移应包括新 runtime_context 的 ordinary/actor 读调用。没有理由给它新建独立数据库、队列或通知账本。

## 5. 全文件分类

下表相对 `src/yuki/memory/`。所有 87 文件都重新解析 AST、imports、实际委托与动态 SQL/事务候选；这不是“所有 26,948 行本轮全文读完”的声明。纯模型、schema、计算与 provider 的结构排除不算全文，T 的其余只读 SQL/格式也不算全文。表中保留前两轮表进行对照，不把分级变化解释为源码变化。


本轮分类：F 21, T 11, S 55，合计 87。R1 声明全文件全文；下列前级引用 R2 自身表，不反向替它扩大阅读口径。

| 文件 | 行数 | R2 → R3 | 本轮范围/排除依据 |
| --- | ---: | --- | --- |
| `__init__.py` | 65 | S → S | 导出/lazy import；无运行时 DML；结构排除，未全文 |
| `audit.py` | 199 | T → S | repository SELECT-only 诊断委托与 scalar health；未逐字全文，不作为写者排 admission |
| `authorized_scope.py` | 259 | S → S | SQL authorization/EXISTS 表达式；不是纯类型，current writer 复核依赖；无 owner/DML |
| `claim_processor.py` | 186 | F → F | 本轮分段全文；实际 DB/委托、来源与取消/收尾边界已核 |
| `context.py` | 100 | S → S | 只读 retriever 组合；query embedding 可能 HTTP，保持 writer 外 |
| `dream/__init__.py` | 1 | S → S | 导出/lazy import；无运行时 DML；结构排除，未全文 |
| `dream/db_models.py` | 333 | S → S | ORM/schema/index/FK 声明；无独立运行时 writer；结构排除，未全文 |
| `dream/models.py` | 285 | S → S | 类型/校验/格式/策略或纯计算；AST/import/调用复核无 DB 写 owner；未全文 |
| `dream/planning.py` | 89 | S → S | prepared DTO/owner 分组/revision 纯计算；无 DB owner；结构排除，未全文 |
| `dream/repository.py` | 1426 | T → T | create_run/全部状态写/恢复/operation/source/result/checkpoint/preview/rollback 方法；候选、健康与分页 SELECT 结构复查，非全文 |
| `dream/service.py` | 1034 | F → T | 150-220、285-530、580-690、693-839：cluster 原子/paid preview/rollback/input proof；其余模型/聚类纯计算结构复查 |
| `dream/worker.py` | 242 | T → S | actual reset/claim/finish/recovery 调用图，未全文；写者归 DreamRepository，模型在 service 外部阶段 |
| `eligibility.py` | 142 | S → S | 类型/校验/格式/策略或纯计算；AST/import/调用复核无 DB 写 owner；未全文 |
| `embedding/__init__.py` | 41 | S → S | 导出/lazy import；无运行时 DML；结构排除，未全文 |
| `embedding/codec.py` | 52 | S → S | 类型/校验/格式/策略或纯计算；AST/import/调用复核无 DB 写 owner；未全文 |
| `embedding/fake.py` | 81 | S → S | 类型/校验/格式/策略或纯计算；AST/import/调用复核无 DB 写 owner；未全文 |
| `embedding/health.py` | 66 | F → F | 本轮分段全文；实际 DB/委托、来源与取消/收尾边界已核 |
| `embedding/jobs.py` | 616 | F → F | 本轮分段全文；实际 DB/委托、来源与取消/收尾边界已核 |
| `embedding/metrics.py` | 68 | S → S | 类型/校验/格式/策略或纯计算；AST/import/调用复核无 DB 写 owner；未全文 |
| `embedding/models.py` | 143 | S → S | 类型/校验/格式/策略或纯计算；AST/import/调用复核无 DB 写 owner；未全文 |
| `embedding/provider.py` | 35 | S → S | 类型/校验/格式/策略或纯计算；AST/import/调用复核无 DB 写 owner；未全文 |
| `embedding/query_cache.py` | 102 | F → F | 本轮分段全文；实际 DB/委托、来源与取消/收尾边界已核 |
| `embedding/qwen.py` | 260 | S → S | HTTP Provider/响应解析；无 Database/SQLite owner；结构排除，未全文 |
| `embedding/repository.py` | 395 | F → T | 119-145 ensure_profile、249-268 purge；vectors/health 的 SELECT 与派生 hash 调用结构复查 |
| `embedding/runtime.py` | 155 | F → F | 本轮分段全文；实际 DB/委托、来源与取消/收尾边界已核 |
| `embedding/semantic.py` | 135 | F → S | 只读 load_authorized/target_vectors 后向量计算；不是主库写者 |
| `embedding/text.py` | 90 | S → S | 类型/校验/格式/策略或纯计算；AST/import/调用复核无 DB 写 owner；未全文 |
| `embedding/worker.py` | 158 | F → F | 本轮分段全文；实际 DB/委托、来源与取消/收尾边界已核 |
| `enums.py` | 259 | S → S | 类型/校验/格式/策略或纯计算；AST/import/调用复核无 DB 写 owner；未全文 |
| `errors.py` | 11 | S → S | 类型/校验/格式/策略或纯计算；AST/import/调用复核无 DB 写 owner；未全文 |
| `event_extractor.py` | 257 | S → S | SubjectContextBuilder+structured model 委托；无事务 owner，网络不进 writer |
| `extraction.py` | 136 | S → S | 类型/校验/格式/策略或纯计算；AST/import/调用复核无 DB 写 owner；未全文 |
| `fts.py` | 421 | F → T | 1-115、325-421：query 构造/health/rebuild/scope；中间 search 的 SQL 类别与 owner/过滤调用结构复查 |
| `job_claims.py` | 38 | F → F | 本轮分段全文；实际 DB/委托、来源与取消/收尾边界已核 |
| `maintenance.py` | 165 | F → F | 本轮分段全文；实际 DB/委托、来源与取消/收尾边界已核 |
| `metrics.py` | 74 | S → S | 类型/校验/格式/策略或纯计算；AST/import/调用复核无 DB 写 owner；未全文 |
| `models.py` | 622 | S → S | 类型/校验/格式/策略或纯计算；AST/import/调用复核无 DB 写 owner；未全文 |
| `mutation/__init__.py` | 29 | S → S | 导出/lazy import；无运行时 DML；结构排除，未全文 |
| `mutation/models.py` | 268 | F → S | 类型/校验/格式/策略或纯计算；AST/import/调用复核无 DB 写 owner；未全文 |
| `mutation/repository.py` | 203 | F → F | 本轮分段全文；实际 DB/委托、来源与取消/收尾边界已核 |
| `mutation/service.py` | 2969 | T → T | 665-881 rollback；1227-1488/1615-1998/2322-2400/2491-2555/2949-2969：prepare/commit/claim/selector/owner/source；其余 Dream mutate、authorize 和 provenance 为 AST/委托复查，非全文 |
| `partition.py` | 288 | F → S | 实际 canonical owner/alias SELECT helper；不是纯类型，supplied session 透传；无独立 DML |
| `projections.py` | 212 | F → S | owner/binding/alias 的批量 SELECT 投影；不是纯类型，无独立写 owner |
| `quality/__init__.py` | 1 | S → S | 导出/lazy import；无运行时 DML；结构排除，未全文 |
| `quality/audit.py` | 436 | T → S | 动态 SQL 均 SELECT/PRAGMA 元数据读取与 event_evidence 审核；不是全文，不以 run 名称推定写者 |
| `quality/event_evidence.py` | 108 | F → F | 本轮分段全文；实际 DB/委托、来源与取消/收尾边界已核 |
| `quality/hygiene.py` | 271 | F → F | 本轮分段全文；实际 DB/委托、来源与取消/收尾边界已核 |
| `quality/models.py` | 41 | S → S | 类型/校验/格式/策略或纯计算；AST/import/调用复核无 DB 写 owner；未全文 |
| `query.py` | 94 | S → S | 类型/校验/格式/策略或纯计算；AST/import/调用复核无 DB 写 owner；未全文 |
| `ranking.py` | 250 | S → S | 类型/校验/格式/策略或纯计算；AST/import/调用复核无 DB 写 owner；未全文 |
| `read_scope.py` | 379 | F → T | 117-237 people 的 owned snapshot/批量 roots+membership SELECT；其余 requester/space/格式 helper 结构复查 |
| `rebuild/__init__.py` | 8 | S → S | 导出/lazy import；无运行时 DML；结构排除，未全文 |
| `rebuild/models.py` | 189 | S → S | 类型/校验/格式/策略或纯计算；AST/import/调用复核无 DB 写 owner；未全文 |
| `rebuild/privacy.py` | 254 | F → F | 本轮分段全文；实际 DB/委托、来源与取消/收尾边界已核 |
| `rebuild/receipt_finalization.py` | 352 | F → F | 本轮分段全文；实际 DB/委托、来源与取消/收尾边界已核 |
| `rebuild/repository.py` | 1131 | T → T | 58-220/269-457/579-606/633-668/773-829/898-1021：全部写与重启/隐私；统计、过滤和 health SELECT 结构复查 |
| `rebuild/service.py` | 951 | F → T | 706-951 process_commit_once/adapt/require 逐段读；其余 plan/manage/extract 的调用/事务与现行文档复核，非全文 |
| `rebuild/trusted_sources.py` | 189 | F → F | 本轮分段全文；实际 DB/委托、来源与取消/收尾边界已核 |
| `rebuild/worker.py` | 61 | F → F | 本轮分段全文；实际 DB/委托、来源与取消/收尾边界已核 |
| `repository.py` | 2154 | T → S | 类型/校验/格式/策略或纯计算；AST/import/调用复核无 DB 写 owner；未全文 |
| `retrieval.py` | 578 | T → S | FTS/vector/ACL 与 network 后重读委托结构；不是全文；mark_injected 的真实写由 service/repository 单列 |
| `runtime/__init__.py` | 13 | S → S | 导出/lazy import；无运行时 DML；结构排除，未全文 |
| `runtime/capability_view.py` | 61 | S → S | 类型/校验/格式/策略或纯计算；AST/import/调用复核无 DB 写 owner；未全文 |
| `runtime/contract.py` | 53 | S → S | 类型/校验/格式/策略或纯计算；AST/import/调用复核无 DB 写 owner；未全文 |
| `runtime/partition_lookup.py` | 34 | F → F | 本轮分段全文；实际 DB/委托、来源与取消/收尾边界已核 |
| `runtime/query_plane.py` | 191 | S → S | 只读 retriever/partition lookup 组合；无 DB writer，独立 reader 迁移沿下游 |
| `runtime/resolver.py` | 77 | S → S | 类型/校验/格式/策略或纯计算；AST/import/调用复核无 DB 写 owner；未全文 |
| `runtime/turn_session.py` | 96 | S → S | 类型/校验/格式/策略或纯计算；AST/import/调用复核无 DB 写 owner；未全文 |
| `self_origin.py` | 287 | F → S | 原 SELF/Work/receipt 与来源 SQL；不是纯类型，复用 caller session，无独立写 owner |
| `self_reflection/__init__.py` | 24 | S → S | 导出/lazy import；无运行时 DML；结构排除，未全文 |
| `self_reflection/control.py` | 455 | F → T | 36-85/258-365/432-455：request reserve/finish、cycle 状态/回执；snapshot/report SELECT/格式结构复查 |
| `self_reflection/db_models.py` | 32 | S → S | ORM/schema/index/FK 声明；无独立运行时 writer；结构排除，未全文 |
| `self_reflection/initiative.py` | 281 | F → F | 本轮分段全文；实际 DB/委托、来源与取消/收尾边界已核 |
| `self_reflection/models.py` | 183 | S → S | 类型/校验/格式/策略或纯计算；AST/import/调用复核无 DB 写 owner；未全文 |
| `self_reflection/reporting.py` | 96 | T → F | 本轮分段全文；实际 DB/委托、来源与取消/收尾边界已核 |
| `self_reflection/repository.py` | 1235 | F → F | 本轮分段全文；实际 DB/委托、来源与取消/收尾边界已核 |
| `self_reflection/service.py` | 635 | F → T | 131-276/380-635：paid checkpoint、逐 proposal/episode、source receipt/result 原子；_input/提示和构造结构复查 |
| `self_reflection/worker.py` | 272 | T → S | scan/claim/reflect/complete/recover/report 的真实调用图，未全文；静默调度归独立分支核合流 |
| `service.py` | 1404 | F → F | 本轮分段全文；实际 DB/委托、来源与取消/收尾边界已核 |
| `subjects.py` | 481 | T → S | SubjectContextBuilder 与目标/历史身份 SELECT；不是纯类型，无 DML/新 owner |
| `targets.py` | 97 | F → F | 本轮分段全文；实际 DB/委托、来源与取消/收尾边界已核 |
| `temporal.py` | 64 | S → S | 类型/校验/格式/策略或纯计算；AST/import/调用复核无 DB 写 owner；未全文 |
| `temporal_filter.py` | 36 | S → S | 类型/校验/格式/策略或纯计算；AST/import/调用复核无 DB 写 owner；未全文 |
| `testing.py` | 18 | S → S | 类型/校验/格式/策略或纯计算；AST/import/调用复核无 DB 写 owner；未全文 |
| `tool_intent.py` | 74 | S → S | 类型/校验/格式/策略或纯计算；AST/import/调用复核无 DB 写 owner；未全文 |
| `validation.py` | 234 | S → S | 类型/校验/格式/策略或纯计算；AST/import/调用复核无 DB 写 owner；未全文 |
| `worker.py` | 358 | F → T | 250-358：逐 claim/complete/fail；enqueue/claim/start/close 经实际 repository 委托图核验，其余模型构造非全文 |

## 6. 跨三轮证据与未完成边界

依 R1 全文清单、R2 实际函数表与本轮独立写入口/委托复核，没有发现三轮合计始终未作语义审查的实际主库写方法；不因此宣称每一行由三人各全文读过。R3 的 S 中包含实际只读 SQL/authorization/owner projection 文件，已单独标出，不能混称纯计算。它们的源码全文证据在 R1，R2 部分复核，R3 做当前调用/session/SQL 类别复核；如果要求三轮每一段 SQL 均独立逐字全文，当前 F/T/S 记录并不证明这一更强口径。

本轮仍是架构任务书终审。新的 admission、read_session、短事务证明与取消清理尚未实现；V 场景尚未因本轮阅读而通过。现有源码竞态、未来迁移风险、既有测试覆盖和新实现验收是不同证据。没有发布、合并、部署或真实性能改善结论。
