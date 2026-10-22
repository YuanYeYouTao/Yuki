# SQLite writer 实现后复审 R2：Memory、SELF 与性能

本轮起点为 `f8fbe14dc2b78a4160d004e714d7f272641a6b40`，由 root 复查 Memory 87 个源码路径及其调用边界。
公共 owner/Automation/离线资源另由 [C 复查](sqlite-writer-review-r2-c-20261023.md)。当前只是本地实现与验证。

## 发现与修改

1. SELF 的事件复核按 100 行构造 VALUES，并逐页返回完整原字段。1024 条尾部恢复的诊断测量从旧 main
   约 35ms 退到 R1 约 193ms。现在在 reader 使用实际 SQLite 类型处理器冻结原值，writer 用固定形状的
   json_each、原选择 CTE 和逐原 ID 的索引查询，一次证明完整集合及全部原字段。保留 ORDER/LIMIT，
   没有用 hash、总数或时间戳代替源证明。总数与每一个原成员、字段的精确匹配共同判定。
2. `_prepare_advance` 原来载入整行并对每条事件重复遍历全部完成区间。现在只载入计数实际消费的
   id/content/ASR/time，完成区间排序后与有序事件一起推进，复杂度从 O(E×R) 变为 O(R log R+E+R)。
   完整事件窗口的证明仍覆盖原 direction/author 与其余实际来源字段。
3. `_finish_runs` 原 run、结果计数、initiative window 的三次查询合成一次；reader 和 writer 使用同一
   原关联，保完整 run 字段、实际结果数量和 window 原四列。`_cursor_current` 同时取当前 state 与 gaps，
   并复用该 writer 已加载的 state，删掉重复主键查询。原 processing 集合、失败空洞、共同完成后的 cursor、
   pending 数量/字符/最早时间仍按当前实际来源计算，不越过失败窗口。
4. [证据计数专项](sqlite-writer-memory-count-review-r2-20261023.md)由 C 追真实消费者后修复：缺项只需
   counts 时按原 readability 做分组 SQL，不在 writer 装完整 Evidence DTO；`list_evidence` 的准备分支
   直接使用真实已备 rows 与本事务新项，删除隐式补历史。普通 reader 的有界 SQL 保留。
5. `resolve_self_origin` 的 initiative 分支只取实际消费的五个字段，不再载入未消费的大 sources/support JSON。
   原 Conversation、Presence、Space、Binding、generation 与原执行版本证明不变。
6. Dream claim 的 fact_ids JSON 和 finalize 的 statistics DTO 转换移到 owned writer 退出后；pending/failed
   两次计数合为一次聚合。cluster 原多 action 原子性、ready/applied paid preview、空结果恢复与回滚依赖保留。
7. Embedding worker 把数据库 complete 放在“Provider 返回值 ValueError”捕获范围内。真实 vector commit
   成功后丢 ACK 的 ValueError 被误报为 invalid provider response。新测试先复现 DID NOT RAISE，随后把
   complete 移到该 try/except 外；不加错误类型分支。原异常保 commit/unknown，已存 vector/job 不被改为
   provider 失败，再次 poll 不重买已完成向量。
8. 性能脚本原 Windows asyncio 定时器存在早唤醒，不能据此解释事件循环长尾。统一改为 20Hz 线程投递的
   callback dispatch 采样；最终数据还须在同一固定脚本、固定源码和无并发重测试时交叉运行。早期 dirty
   树的约 58–61ms SELF、聊天 SQL 模板变化只作定位证据，不作为最终性能结果。

## 全域核查

- 基础 fact/relation/evidence 的准备、两跳关系完整成员复核、copy 当前资格、原新增 evidence ID 与事实/receipt
  原子提交；owned/supplied session 沿完整调用传递。counts 专项回归覆盖缺项、重复来源与当前 writer 新增。
- Mutation 的准备/授权/原 event/tool receipt/Conversation/SELF 来源证明、原操作回执恢复；Dream 的每 action
  当前 fact/owner 证明与整 cluster 原子结果；Rebuild stage、adoption、receipt sweep、隐私 selection 与最终 absence。
- SELF scan、claim、checkpoint、原 run 恢复、initiative receipt cursor、paid tool 引用保护；control 的真实 cycle、
  daily 请求计数和 manual event；worker 在 quiet 后才 claim，抢占后停止继续认领下一个 owner，等待不包事务。
- Embedding 原 fact/hash/profile/claim CAS、分页 upsert、外部 Provider/编码在锁外；maintenance 到期整批与状态事件；
  hygiene 原证据与来源成员核验、分页清退和显式 FTS rebuild。没有把所有维护拆成可部分完成的隐私操作。
- audit/read_scope/partition/projections/authorized_scope/targets/runtime/query/retrieval 是 reader 或无 DB 编排，
  网络向量查询、历史正文、渲染、hash 不进入 writer。类型、导出、纯算法仅核其 I/O/消费边界，不声称逐行算法审计。
- 显式 FTS rebuild 和完整隐私清退仍保原原子单位；独立 Protocol/Manifest 文件的已有原子发布不改成新的 orphan 协议。
  没有为了零 SQL/零 CPU 添加缓存世代、通用证据框架或辅助完成门槛。

## 本轮验证

- Memory/Dream/Embedding/SELF/quiet 相关 unit 选择：399 passed、1 POSIX skip、3109 deselected，127.62s。
  该批完成后新增 Embedding ACK 回归并收窄 SELF counter 字段，再做以下定向复验，数字不重叠相加。
- 最后 SELF tail/checkpoint recovery/Embedding claim 组：24 passed。Embedding 三文件独立组通过；
  SELF/priority/checkpoint/Dream 组合 53 passed，较早的组合数字不作为独立总数。
- 新 tail 用实际 SQLite variable limit=256，64 条大正文，以及准备后正文变化，验证 paid 原 run 只恢复一次、
  cursor 不越过未处理尾部、pending 数量/字符完整。原 blank/voice/gap/privacy 回归保持。
- Memory 87 源码 mypy --platform linux 通过；最终全仓质量与 Linux/CI 验证另记。
- 使用 Fake/注入 Provider 和隔离数据库，没有真实模型请求、平台发送或生产变更。

## 87 路径范围

D=数据库具名方法与实际读写链；I=间接编排/外部效果消费链；S=类型、导出或纯计算边界。
以下路径目录与 R1 共用同一全范围清单，列出的消费点本轮继续核查；不把 AST 成功等同语义通过。

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
