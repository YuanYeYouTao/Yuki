# SQLite writer 实现后交叉复审 R1：Memory

本轮 reviewer 为原 A 域实现者，独立复查原 B 的 Memory 及 root 实现的 SELF。
冻结实现基线 `109cbd40274ca8db1a0b381ac2db1e35c3cf1df3`；审查对象为本工作树，
不把实施前的三轮报告算作本次复审。当前只是本地修改和本地验证，未提交、推送或上线。

先读 AGENTS、development-contract、memory-v2 现行合同和 B 实施记录，再读实际实现及测试。
范围覆盖 U24–U30、W42–W56；U23 Sandbox 本体由 C reviewer 负责，这里只核其 Memory 来源接口。
87 个 Memory 源码路径的状态逐项列于文末。S 项明确排除无 I/O 的结构、导出或纯计算，
不把 AST 解析成功称作全文语义通过；真正的事务实现和调用链按具名方法审查。

## 本轮发现与修改

1. **独立证据准备串用同一 reader 的缓存。** `MemoryFactRepository.prepare_write` 把
   reader 已读过的全部 evidence rows 放进每次证明。先准备 A 再准备 B，B 的 fact_ids
   只有 B，rows 却含 A+B；`use_prepared_write` 因此误判 `memory_evidence_changed`。
   新回归先在原实现复现失败。本次只取本次两跳关系集合的证据；copies / dream bundles
   也改为每次准备自己的容器，仍允许 Dream 在同一准备内填入各 action 的原材料。
   没有清空 reader 的公共证据缓存，也没有增加永久缓存或 revision。

2. **按 key 删除偏好会在 writer 补读新事实的历史。** 原逻辑 reader 预选最多两条、
   writer 再完整 list_facts；交接期间新同 key 事实进入结果，就走 missing-cache 历史准备，
   最后才报歧义。该操作只需当前唯一 ID 和原状态事件。本次按原 owner/kind/key/status/
   quarantine/expiry 谓词查最多两个 ID，直接短 writer 复核和 transition；删掉证据准备。
   准备后 phantom 回归确认仍报原歧义且不改任何事实，没有在 writer 读取 evidence 正文。

3. **普通自省的 paid checkpoint 未保护临时工具回执。** `cleanup_receipts` 原来只保
   evidence 和未完成 initiative window；普通 ReflectionCheckpoint 的 tool_map 引用
   没参与清理资格。结果已付费但尚未完成的普通自省可能在 TTL 后丢失来源。本次清理
   SELECT 和 DELETE 共用原 checkpoint 中实际 receipt ID 的 NOT EXISTS 条件。真实
   save_checkpoint 回归证明：只保被引用的回执，其他到期回执照删；解除引用后原件可删。
   不延长 TTL，不加保留门槛或影子引用表。

4. **Rebuild 暂存已提交后，失败收尾会撤回 durable 状态。** `_extract_one` 在
   stage_claims 成功提交但退出报 RuntimeError 时调用 fail_item，原方法可把 staged 改回
   pending，下一次再次调用模型。新增回归在原代码明确复现 pending。现在失败收尾只
   改实际 extracting 行，stage 同样只处理原 extracting 状态。proposals、item 和该次
   usage 一起提交；删掉唯一旧 record_model_usage 独立 writer，减少 paid 输出落盘前的
   一次可失败写。恢复仍读原 item/proposals，不重新调用模型，不重置 attempts。

5. **Rebuild receipt sweep 缺实际正文来源复核。** reader 计算 source fingerprint，
   writer 的 `_read_batch` 只比较会话/作者等 source_token，缺 content、ASR、segments、
   时间及 ingress 字段。交接后正文变了也可能登记 done、推进 item。本次在 writer 按
   原实际字段做批量 SQL 等值查询，只返回匹配 ID；正文解码和 hash 留在 reader。
   content / 合法 ASR 两个竞争窗口均验证先不落回执，下一轮按变化来源将 item skipped。

6. **过期维护准备了根本不消费的完整证据。** expiry 与关联冲突清除使用当前小列和
   关系 SQL，不消费 evidence 正文。删掉该整批 PreparedMemoryEvidence 和两跳历史准备，
   保整批 expiry/state events/conflict clearing 原子。另修发现后变成 superseded 的事实
   仍被过期器改为 invalidated 的竞态：writer 复用原 active/contested+到期谓词。
   原延期、两跳冲突、等待、取消、close、未知 commit/rollback 回归均保留。

7. **Dream 正常运行的 paid 输出没有先暂存。** 原 ready preview 只覆盖手动预览；
   runtime `_decide` 后准备或数据库失败，重试又请求模型。本次直接使用既有 preview 表、
   codec、input fingerprint 和 ready/applied 语义，正常输出先保存，再按原输入提交。
   新保存结果按原 public ID 精确取回，避免与同时新建的另一份预览混用身份。
   原 disabled-owner/restart 用例现保 run/cluster/attempts，只请求一次模型，累计预算不增。

8. **Dream 空 actions 的已应用结果没有被恢复认出。** 只数 committed operations
   无法证明空结果已经提交。worker 故障收尾和 restart 恢复现在同时核原 applied preview，
   operation_count 保持 0。新增两个提交已成功、确认丢失的回归分别覆盖 worker 和 restart，
   两者都按原结果完成，模型只调用一次；不新增完成状态或重试运行时。

9. **公开 RebuildService.plan 仍保留旧重准备路径。** supplied writer 时会另开授权
   reader，并调用 ledger maximum/count 扫历史。删重复实现，委托已有 plan_rebuild_core /
   PreparedRebuildPlan。plan/start/cancel 的授权读取也复用 supplied session。新增回归在
   有真实准备的 writer 中禁止再开 reader，仍能按当前权限创建原计划。

`memory-v2.md` 同步了以上真实边界；历史实施报告保留当时事实，没有改成事后验收记录。

## 逐类审查结论

| 范围 | 具名入口与审查重点 | 结果 |
| --- | --- | --- |
| 基础事实 / job（W42） | prepare/use_prepared_write、get/list/target 查询、create/transition/review/metadata、add/copy evidence、relation、mark_injected；enqueue_resolved、claim/claim_ready_batch、complete/fail | 当前 session 复用；新 fact 注册空证据缓存；job 采用原 id/event/status/updated_at claim；本轮删 preference/expiry 不必要准备 |
| Mutation（W43–46） | mutate/mutate_resolved/mutate_validated_claim、_prepare/_prepare_self_origin、_commit_prepared、_mutation_source_snapshot/_mutation_sources_current、_apply/_version、所有 receipt reserve/finalize | 历史来源准备在 reader；writer 读原 owner、权限、源字段、claim，fact 与原 mutation receipt 同提交；异常后先查原 receipt，取消传播，不重做外部效果 |
| 证据关系 | 两次 relation selection 的完整成员复核、readable evidence ID/原字段、目标 owners、copy eligibility；新增 IDs 实际记录 | 没有用 count/max/time 代替原关系与证据；没有全库门槛。更正/merge/version 的复制匹配同一准备缓存 |
| SELF（W47–49） | _scan_event_batch、claim_due、claim_initiative、_prepare_advance/_cursor_current/_events_current、_finish_runs、save_checkpoint、cleanup_receipts；control 全部方法；service.reflect/_evidence 与 worker 全调度 | 源正文、渲染、hash、剩余尾部计数在 reader；writer 核完整原窗口、gaps、run 与实际结果；多 run 同 state 先共同准备再推进，不越过失败窗口；initiative cursor 按原 receipt ID，晚回执仍有后续窗口；paid checkpoint 不重购模型 |
| Quiet | worker._process_cycle → wait_for_quiet，service 每次提交前等待，control 真实物理请求 reserve/finish | 等待不包在数据库事务中；manual/scheduled 原 cycle 保留，抢占退出不继续 claim 下一个 owner，关闭可取消等待；前台 tracker 的源实现由 C reviewer 独立审，相关消费者测试本轮也运行 |
| Dream（W50–51） | load_candidates / prepare_clusters / create_run；claim/reserve/finish/finalize/recovery；process_cluster/_commit_cluster_decision；prepare_dream_rollback/rollback_dream_operation；preview 全链 | 聚类在 thread；模型在 DB 外；一个 cluster 的全部 actions 原子；rollback 核原 source/result、added evidence/relation 外属与后续依赖缺席；保预算/原 operation/result。新的 paid 暂存与空结果恢复见上 |
| Embedding（W52–53） | _prepare_page/_enqueue_prepared、claim/_claim_guard、_complete_page、recover/skip/fail/retry/delete；repository 全部 DB 方法；worker/runtime/semantic | hash、HTTP、vector encode 在 writer 外；当前原 claim/fact 字段 CAS；128 条写页，不加逐 evidence 历史验证；查询/health 只用 reader，无变化不排 writer；query cache 仅向量，不缓存事实授权 |
| Rebuild（W54–55） | 全部 repository 事务方法；_extract_one/process_commit_once；complete_receipts/_read_batch；prepare_forget/apply_forget；trusted_sources | 每个来源全部 proposals+item+usage 原子暂存；每 proposal 采用与其原 receipt 同提交；最终状态按当前 pending/review/item 缺席；selection/原 JSON/隐私匹配 phantom 复核保留；修暂存/receipt 源遗漏 |
| Maintenance/quality/FTS（W56） | expiry 整批；quality.run/_event_rows/scan/_invalidate_page/_purge_page/rebuild_fts；FTS search/health/rebuild | 普通维护按候选页释放 writer；显式 FTS rebuild 保完整原维护命令；Hygiene 原 evidence 成员和 event 原字段在 SQL 核，tool provenance 当前核，状态和 state event 一次提交；不引入年龄/置信度淘汰 |
| 检索与原来源消费者 | partition/projections/read_scope/authorized_scope/subjects/targets/self_origin、retrieval/runtime/query/context | read_session 无 autoflush/自动 commit；权限在实际 SQL 过滤，不将网络 embedding 包在事务中；历史 raw owner 与当前归并展示分别保原语义；未发现隐式写入 |

检查过无 Memory 产品残留 public sessions、apply_snapshot_write、旧 mixed transaction、
第二套 BUSY 循环、first-write listener 或无效抢锁 UPDATE。必要的状态/身份 SELECT 保留。
新增 Dream operation/result 的实际 SQLite ID、时间及原证据新增 ID 参与最终回执，相关小型
编码/签名仍在原子 writer；没有为追求零 CPU/零 SELECT 造通用模板或索引框架。

## 验证

- Memory/Self/Dream/Embedding/quiet 的 45 个 unit 文件：**377 passed**，101.74 秒。
  这次完整范围包含当时全部产品修复和新增测试；之后增加 supplied plan 回归并收窄
  新 Dream preview 的精确 public ID 选择，再分别定向验证。
- 最后 Dream、paid tail、busy recovery：**52 passed**；Rebuild writer boundaries：
  **4 passed**（含 stage 提交后失去确认、正文/ASR 竞争、supplied session）。
- 证据/独立事实：10 passed；SELF priority：8 passed；维护/cost/writer：17 passed；
  Rebuild 原组与新增：22 passed。均为本轮实际执行，重叠用例不累加成独立测试总数。
- Ruff：Memory 全域及本轮涉及的 8 个 test 文件通过。mypy：87 个 Memory 源码通过；
  最后的 preview public ID 小改后再核 87 源码，同样通过。
- 没有真实模型请求、生产操作或平台消息。测试中的模型均为 Fake/注入实现。

## 后续交叉审查与性能边界

本轮不替代 root 的统一端到端性能报告。删除 preference/expiry 的历史准备、合并 Rebuild
暂存与 usage 减少读量和 writer 次数；新增 Dream paid 暂存是一次保存真实结果的短事务，
是保留原付费结果所需的写入。receipt sweep 新增批量原字段 SQL 复核，无正文回传/解码。
SELF checkpoint 引用清理使用原 JSON 的实际引用；当前未增加反向索引，后续性能复核应关注
大量仍保留 checkpoint 时的 GC 代价，不能把该引用检查删成不保护 paid 原件。

R1 没有留下未处理的已确认 Memory 数据库实现缺口。R2/R3 应继续独立挑战：大证据族的
writer SQL 比较成本；SELF 大量同 owner run / 乱序晚 receipt；Dream ready/applied 的并发
消费与 unknown outcome；Rebuild 稀疏 staged/source 变化与最终 receipt sweep；当前业务
允许的完整隐私清退原子单位。源码外的共用 owner/driver 阶段、前台 activity tracker 和
Sandbox ACK 由各自 reviewer 审，不在这里冒充已独立全审。

## 87 路径逐项状态

D = 真实 DB 方法及其读写/调用语义已审；I = 间接调用或外部效果编排已审；
S = 明确排除的导出、类型/ORM 声明、纯算法/序列化结构。S 检查 imports/calls/定义及相关
消费点，不宣称逐行审过全部无关算法。纯算法中与 writer 位置相关的调用也在 D/I 链核过。

| 路径（相对 src/yuki/memory） | 等级 | 本轮落点 |
| --- | --- | --- |
| __init__.py | S | 公共导出 |
| audit.py | D | explain/chain/health 纯读；供给 session 复用 |
| authorized_scope.py | D | 同人族/共同群/SELF SQL 授权与结果目标 |
| claim_processor.py | I | resolve 锁外，apply_resolution 复用 writer |
| context.py | I | query/retrieval 编排，无隐式发布 |
| dream/__init__.py | S | 导出 |
| dream/db_models.py | S | 原 run/cluster/operation/source/result/preview/checkpoint/compaction 声明 |
| dream/models.py | S | Action/Output/预算/状态 DTO 与纯形状校验 |
| dream/planning.py | I | 原 owner/source 材料构造及 create_run 消费 |
| dream/repository.py | D | 全部具名 DB 方法；新增 applied 空结果恢复 |
| dream/service.py | D | 全部计划/preview/cluster/rollback；正常 paid 暂存 |
| dream/worker.py | D | 调度/失败收尾/原回执恢复 |
| eligibility.py | D | Python/SQL 来源资格，原 live receipt 缺席 |
| embedding/__init__.py | S | 导出 |
| embedding/codec.py | S | 纯编码/向量数学；调用点锁外 |
| embedding/fake.py | S | 无 DB 的测试 provider |
| embedding/health.py | I | local health 读与显式 remote doctor 分离 |
| embedding/jobs.py | D | 全部准备/claim/CAS/upsert/recovery/retry/page 方法 |
| embedding/metrics.py | S | 内存指标 |
| embedding/models.py | S | Profile/vector/usage DTO |
| embedding/provider.py | S | Protocol/异常，无事务 |
| embedding/query_cache.py | I | 内存 single-flight，仅 HTTP 在锁外，无 DB session |
| embedding/qwen.py | I | HTTP/并发/编码，无持有 DB 的消费者 |
| embedding/repository.py | D | 全部 profile/vector/health/purge DB 方法 |
| embedding/runtime.py | I | owner/worker/provider 生命周期与 prepare 调度 |
| embedding/semantic.py | I | SQL 获准后锁外向量计算 |
| embedding/text.py | S | 文本/hash 纯函数，worker 调用点锁外 |
| embedding/worker.py | I | claim→读→HTTP/encode→短 complete |
| enums.py | S | 枚举 |
| errors.py | S | 领域异常 |
| event_extractor.py | I | 主体准备/模型/claims 编排，模型不持 DB |
| extraction.py | S | 提取 DTO 与原源指纹函数 |
| fts.py | D | search/search_authorized/health 读；显式 rebuild writer |
| job_claims.py | D | 原 id/event/status/time claim SELECT fence |
| maintenance.py | D | 删除重准备；当前到期状态与批次原子 |
| metrics.py | S | 内存诊断结构 |
| models.py | S | Fact/evidence/target/query DTO、纯校验 |
| mutation/__init__.py | S | 导出 |
| mutation/models.py | S | request/context/result/receipt DTO |
| mutation/repository.py | D | find/reserve/reserve_dream/finalize 同 session |
| mutation/service.py | D | mutation 全准备/应用/来源/授权/rollback 链 |
| partition.py | D | Person/Space 原 owner 读与 canonical shape |
| projections.py | D | 当前展示绑定只读；原 owner 不回写 |
| quality/__init__.py | S | 导出 |
| quality/audit.py | D | run 实际读；_checks 仅构造 SQL，无隐藏写 |
| quality/event_evidence.py | D | 纯资格投影与流式 reader，finally close |
| quality/hygiene.py | D | scan/apply/page/source proof/purge/显式 FTS |
| quality/models.py | S | 治理报告 DTO |
| query.py | I | target resolver 与纯 query 构造 |
| ranking.py | S | 纯检索排序，无 DB |
| read_scope.py | D | 全部历史关系/name/owner 查询；people 一致读 |
| rebuild/__init__.py | S | 导出 |
| rebuild/models.py | S | Selection/Run/Review/health DTO |
| rebuild/privacy.py | D | 完整实际相关集合与 selection 原 JSON/phantom |
| rebuild/receipt_finalization.py | D | 原页/source/claim 证明与短写 |
| rebuild/repository.py | D | 所有公开 DB 方法；staging/usage 原子与终态保留 |
| rebuild/service.py | D | core 与 service 全模型/采用/授权/收尾边界 |
| rebuild/trusted_sources.py | D | 原 ingress 绑定时段/提及/回复批量复核 |
| rebuild/worker.py | I | restart pause；只处理已有执行 run |
| repository.py | D | 所有 Fact/Job 读写方法、证据/关系/copy 证明 |
| retrieval.py | D | 全部 retrieval 编排；query HTTP 不持 reader |
| runtime/__init__.py | S | 导出 |
| runtime/capability_view.py | S | 固定能力视图纯构造 |
| runtime/contract.py | S | per-turn 只读写权能结构 |
| runtime/partition_lookup.py | D | 同一 reader 解析原 SELF |
| runtime/query_plane.py | I | 固定 read consumer；无 query 发布写 |
| runtime/resolver.py | S | trusted origin 纯判定 |
| runtime/turn_session.py | I | SELF 原来源解析与固定 capability 投影 |
| self_origin.py | D | 原 initiative/automation execution 与历史 evidence SQL |
| self_reflection/__init__.py | S | 导出/模型预构建 |
| self_reflection/control.py | D | 全部 cycle/request/report/预算短写与读快路 |
| self_reflection/db_models.py | S | cursor/window 原表与唯一约束 |
| self_reflection/initiative.py | D | backlog/claim/source完整窗口/cursor/晚 receipt |
| self_reflection/models.py | S | 输入/输出/window/tool DTO |
| self_reflection/reporting.py | D | 实际 report source reader；Social 原 effect receipt |
| self_reflection/repository.py | D | 全部 scan/claim/checkpoint/finish/recover/GC |
| self_reflection/service.py | I | 模型/quiet/paid输出/逐结果原 mutation 回执 |
| self_reflection/worker.py | I | manual/schedule/quiet/抢占/关闭与恢复 |
| service.py | D | 全部 fact helper；当前 writer 身份；偏好短写 |
| subjects.py | D | ingress 历史主体、名字与 reply 纯读 |
| targets.py | D | 同 snapshot 解析 current/mentioned/reply |
| temporal.py | S | 原事件时钟纯解析 |
| temporal_filter.py | S | 纯 SQL 条件构造，无执行 |
| testing.py | S | 纯测试 DTO builder |
| tool_intent.py | S | 模型只读意图解析 |
| validation.py | S | 原 claim 纯校验，调用点锁外 |
| worker.py | I | 批抽取在外；逐原 job/claim/mutation/complete |
