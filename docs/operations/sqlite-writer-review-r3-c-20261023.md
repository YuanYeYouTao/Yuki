# SQLite 实现后交叉复审 R3：Memory 与 SELF

本轮独立复审基线为 `1fb34b85031dc3a7df1f8f5c7e7f0cdf60fc6966`，范围是
`src/yuki/memory` 全部 87 个 Python 路径及实际消费者。阅读当前 AGENTS、共同开发合同、
Memory change/v2、SELF、Dream、Rebuild、Embedding 合同后，先核源码，再对照 R2 根报告。
没有把旧报告、AST 命中或测试通过代称本轮源码覆盖。三处修复及回归已进入根冻结
`c0af6e8240548acbbd816edc07a3b964d1a16657`；以下行号按该轮源码定位。

本轮没有真实模型请求、平台发送、生产操作、独立数据库格式变更或 benchmark 结果修改。
后续受托补审 plugin_host / Sandbox 的记录另列，不能代称 Memory 87 路径覆盖了 C 全域。

## 本轮发现与修复

### 1. 排队前的 claim 时间使新任务刚领到就过期

`MemoryJobRepository.claim`、`claim_ready_batch` 原先在 reader 准备时取时间，通用
`persistence/job_claims.py` 等待 FIFO 后直接写入该旧时间。等待超过五分钟时，返回的
processing claim 已满足 stale 条件，另一 worker 可以马上领走同一原 job。后续 fence
虽能阻止旧结果覆盖，却不能避免重复模型费用。

已删除只有 Memory 两个生产调用者的泛型 persistence helper，将其并入现有
`memory/job_claims.py:33`，去掉 model 参数。原 ID/status/updated_at CAS、live event 的
canonical owner/floor EXISTS 保留；在真实 writer 准入后用 `next_updated_at` 产生本次
processing token，通过 SQL RETURNING 返回实际持久时间，两调用者只更新原 DTO 的 token。
跳过和删除不虚构 processing 时间；原 job/event ID、预算、attempts、原请求到期时间不变。
也删除准备值中随后会被覆盖的旧 `updated_at`。

真实队列回归持有公共 writer，让另一个 claim 完成 reader 准备后排队，再推进时钟六分钟。
旧基线两个入口都返回六分钟前的 token；新代码返回准入后的实际 token，第二次 claim 为空，
同一原 token 可以完成原 job，attempts 仍为零。不是靠修改旧 claimant 的数据库行制造成功。

### 2. 同一 operation ID 的并发不同内容可冒用对方成功回执

`_original_operation` 的首次读会比较原请求 fingerprint，但并发时两个请求均可能先读到空。
原 `_commit_prepared` 的预读、writer 内重复检查和丢 ACK 后查证直接 `from_receipt`，且
repository.find 的 `claim_fingerprint` 参数实际被丢弃，因此另一 payload 可收到第一个请求
的成功事实 ID。

新增具名短 helper `_original_result`，四个原结果消费点共用原 fingerprint 比较，冲突仍返回
既有 `memory_operation_payload_conflict`，相同 payload 返回同一原 receipt。删除 find 不消费
的参数，并迁完八个实际调用点。自动 worker 的 key 本来包含完整 claim fingerprint，原义未改；
没有新 receipt、资格状态、请求 ID、补偿写或重试循环。

真实并发两个不同 CREATE、writer 出口丢失 ACK，以及第二请求已准备但第一请求先提交三种
交错均已验证。只产生一条事实和一条 receipt；冲突方没有借到另一内容的成功；相同 payload
重放返回原 mutation/fact ID。已有顺序冲突、相同 payload 并发和原 automatic claim 重放仍通过。

### 3. FTS 重建把写入异常包装成只读错误，丢失真实阶段

`SQLiteMemoryFTSIndex.rebuild:358` 在 owned writer 外捕获 DatabaseError 后重新构造
MemoryRetrievalError，原异常上的 execute/commit 与 confirmed/unknown 信息不在新异常上。
已只删除这个写入口的 catch；search、search_authorized 和 health 的只读领域错误保持。

实际消费者是 `MemoryAdminService.rebuild_index` 和聊天管理命令，不是新增的 ControlGateway
操作。沿真实 Admin→FTS→Database 链在 SQL 执行处以及 SQLAlchemy after_commit 处注入原
OperationalError，旧代码两个参数都转成领域异常；新代码保留同一异常对象。执行失败为
execute/confirmed rollback，丢提交 ACK 为 commit/unknown，索引仍可按实际状态读取。
独立 reader 缺 FTS 表仍返回既有 `memory_index_unavailable`。没有为所有 audit 泛化 best effort。

## 全域语义复核

- U24/U25、W42–W46、V46：Fact 的两跳 relation 完整成员、当前 evidence 可读性及原 raw
  source 值在 writer 重核；copy 保实际返回的新增 evidence/relation ID。身份与 authority helper
  复用当前 writer。selector 在 reader 唯一解析成固定 fact ID 后不会因后来出现另一个同名事实
  重选或扩大范围。mutation 原事实、状态、证据、原执行 receipt、SELF result 同提交。
- counts 专项重新核到全部实际消费：missing counts 走分组 SQL 可读性计数，并显式补零，
  不渲染证据正文；同 writer 新增只累计真正插入的来源。list_evidence 在 prepared 模式直接索引
  完整准备集合，没有默认空或锁内 prepare fallback。实际 writer bundle 消费来自 Dream，
  输入和每个 recompose output 的原完整来源在 reader 已准备；普通审计/展示保 reader order/limit。
- U26、W47–W49、V47：SELF fixed-shape JSON 证明保原完整 query 的 join、order、limit，
  同时比较实际选中成员数和每个原 PK 的所有字段，不能由另一条相似事件顶替。cursor 仅消费
  id/content/audio_transcript/occurred_at 四字段，实际 evidence_content 也只读 content/当前 ASR；
  claim、context、checkpoint 保完整 source 字段，没有把 full proof 一起收窄。
- `_prepare_advance` 读原 gaps、按最早 unfinished 限定 cursor，用排序 completed ranges 与事件
  双指针算完整剩余计数；writer 复核原 state/gap/source 后应用。`_finish_runs` 的 run/window/result
  查询保原 processing membership、真实结果计数和原窗口，多个 run 的 cursor 不会越过未处理洞。
  completed ranges 不是新的持久计数表，也没有用输出报告当完成依据。
- SELF 原 initiative receipt 窗口完整读取，保数字 ID 排序、原 first/last 和正文/metadata 全字段。
  late receipt 不吸收到已经付费的窗口。checkpoint 固定原 event/tool map；成功结果有逐项原 mutation
  回执，恢复不重新购买模型、不重置 daily budget。空输出可以合法完成。receipt GC 重核原 expiry、
  evidence、未完成 window 及任何 checkpoint 的引用，过期不撤销已提交证据。
- source 真实调用链核至 initiative、mutation 准备/原来源快照/当前来源、runtime partition lookup
  和 ToolReceipt recorder。历史读取可以 require_live=False；新 delayed receipt 的当前 generation、
  Presence/Space 和 privacy 资格仍由原 writer SQL 校验。SELF 没有借 human trigger 生成所有权。
- quiet 等待发生在 claim、付费请求和逐项恢复写之前的协程层，未持 DB writer。仅固定时段或
  明确手动 cycle 驱动，原 failed run 不自动另起一轮；前台抢占后保原 cycle/run/checkpoint 并停止
  继续认领下一 owner。真正恢复的 paid checkpoint 不再消耗一次 daily request。
- U27、W50/W51、V48：Dream plan 以原 owner/source 准备，writer 核原成员与同人族；原 cluster
  action 整体提交。付费 preview 先 ready 暂存，输入原来源/完整 evidence fingerprint 变化会失效，
  应用与 applied 标志同一事务；空 actions 也留下 applied，restart 不凭 operation_count=0 再买模型。
  rollback 检查原 operation/source/result、后续依赖不存在、原新增 evidence/relation 所有权，再同
  writer 恢复原状态与 evidence cache，未把 rollback 当数据库通用重试。
- U28、W52/W53、V49：embedding hash、HTTP、向量编码均在 writer 外。claim 后的 complete
  同时核原 ID/hash/profile/attempt/time、当前 fact 小字段及当前有效期；变动来源重新 pending，
  新 owner 不被旧完成/失败覆盖。vector+done 同提交。jobs.complete 在 Provider ValueError catch
  之外，丢数据库 ACK 不被当作向量错误再次记 fail。health/vector 查询是 reader，流式 health 不占 writer。
- U29、W54/W55、V50：Rebuild source/adoption 当前重核 run selection、原 proposal/item/event、
  当前授权、原 ingress 历史主体与 live job 缺席。事实与 proposal completion 同一事务；item/job
  receipt sweep 是另一个原子单元，不能宣称全部三者同提交。sweep 完整重核当前 proposal 状态
  聚合和来源、原页、trusted refs；finalize 在 writer 查询真正 pending/failed/review/item absence，
  不靠准备时的空结果或新增完成门槛。隐私 selection 检查当前完整相关 run 集合与原 JSON，包含
  新匹配 phantom；删除最后一个交集成员不会把 selection 扩大。
- U30、W56：到期维护在原批次短 writer 重新判断真实到期状态并写 state events；hygiene 在 reader
  准备实际证据/raw source，writer 重核完整成员，分页清理只分割原可独立维护项。整次人物隐私操作
  仍由外层 owner，FTS 显式全索引重建保原原子步骤。没有为少等锁增加缓存世代、持久 validated 位或新 gate。

仍有必要的当前行 DTO/新结果 ID、小型签名、原 receipt JSON 与 SQL 条件计算在事务中；它们不是
历史查询或外部 I/O，未为字面上的“零 CPU”另造框架。Dream 新 operation/result 的实际 ID、结果
时间与状态签名必须属于原事务，不能提前伪造。只读诊断可能观察 Rebuild 已采用而 item receipt
尚未扫完的中间状态，诊断本身不推进或阻止完成。

## 本轮验证与边界

- 真实旧基线 red：两 claim 入口及两种并发 payload/ACK 交错，4 failed；恢复新源码后同组所在
  三文件 19 passed。新增的预准备交错后来单独纳入补充组。
- Memory/Dream/Rebuild/SELF/Embedding/claim/mutation unit 选择，385 passed / 139.27s。
  这一批在 FTS 三项和预准备交错加入前完成，不能说它包含所有后来新增测试。
- FTS 旧基线 2 failed / 1 passed，失败来自原包装丢 phase；新源码 3 passed。
  首次编写该测试曾漏 AdminActor 两个必要字段，修好夹具后才重新取得上述有效 red 证据。
- 最后 FTS + operation identity + app canonical Memory integration + SELF initiative sources：
  23 passed / 8.86s。与前面有重叠，数字不能相加成独立总数。
- Ruff check 本轮 8 个修改文件通过；format 当时剩 test_job_claim_transactions 的函数签名排版，
  根随后已冻结 `c0af6e82`，该纯排版已在补审解冻后完成。暂停重测试遵守根的固定性能测量窗口；没有在 dirty
  树上据本轮 unit 时间宣称最终提速。全仓静态检查、Linux CI、PR/merge 和最终性能由根报告汇总。
- 测试证据在 `D:/Temp/yuki-sqlite-validation/r3-c-{red,fts-red,memory-tests}.txt`，不是产品依赖。
  无真实数据库方法仍只以 AST 或纯结构排除；纯类型/网络/数学代码的具体排除见下表。

## 87 路径逐项覆盖

D=全部具名 DB 方法、来源与当前实际读写调用链；I=实际间接编排/外部调用消费边界；
S=导出、类型/Schema 声明或纯计算，核定义、imports/calls 及调用位置后排除 DB 行为。
D/I 不表示测试逐行穷举，S 不冒充全文算法审计。两份 db_models 已额外核真实列、FK、唯一约束与删除规则。

| 路径（相对 src/yuki/memory） | 等级 | 本轮落点或排除依据 |
| --- | --- | --- |
| __init__.py | S | 仅导出，无执行 |
| audit.py | D | get/evidence/relation/history/chain/explain/health 均纯读，supplied session 复用 |
| authorized_scope.py | D | 实际 SQL 权限条件与原 canonical owner 映射，无 DML |
| claim_processor.py | I | resolve 纯准备；apply_resolution 供给原 writer |
| context.py | I | resolve/search 调用，无 query 后隐式写 |
| dream/__init__.py | S | 仅包入口 |
| dream/db_models.py | S | 原 run/cluster/operation/source/result/preview/checkpoint/compaction FK/unique |
| dream/models.py | S | 输出/状态/预算 DTO 与纯形状 validator |
| dream/planning.py | I | 原 canonical owner、完整固定 fact IDs 与 revision 准备 |
| dream/repository.py | D | 全部 plan/run/claim/budget/finish/preview/checkpoint/rollback/health 方法 |
| dream/service.py | D | 全部 plan/preview/paid/prepare/commit/rollback；legacy paid codec 只读 |
| dream/worker.py | I | 原 run 调度/锁外模型/失败回执恢复/空 action 完成 |
| eligibility.py | D | 当前 event 的 Python/SQL 可读性、原 live receipt 缺席 |
| embedding/__init__.py | S | 仅导出 |
| embedding/codec.py | S | 纯 math/struct；actual worker 编码在外 |
| embedding/fake.py | S | 内存测试 provider，无 DB |
| embedding/health.py | I | local repository reader；显式 remote doctor 不持事务 |
| embedding/jobs.py | D | 全部 prepare/enqueue/reconcile/recover/claim/complete/skip/fail/retry/purge |
| embedding/metrics.py | S | 仅内存快照和计数 |
| embedding/models.py | S | profile/vector/usage DTO 及纯 hash/形状 validator |
| embedding/provider.py | S | Protocol/ProviderError，无持久调用 |
| embedding/query_cache.py | I | 内存 single-flight callback；actual factory 仅 Provider，无 DB owner |
| embedding/qwen.py | I | HTTP/并发/解析；actual caller 不持 DB，未执行网络 |
| embedding/repository.py | D | 授权/target vector 读、ensure_profile、purge、stream health |
| embedding/runtime.py | I | provider/worker 生命周期、调度/health/显式 retry/rebuild |
| embedding/semantic.py | I | 读授权向量后纯解码和相似度计算 |
| embedding/text.py | S | 原文本 build/hash，实际调用在 writer 外 |
| embedding/worker.py | I | claim→reader→HTTP/encode→短 complete，ACK catch 边界 |
| enums.py | S | 枚举，无调用 |
| errors.py | S | 异常结构，无 DB |
| event_extractor.py | I | 主体 reader 和模型调用分开；原 event IDs 逐项归属 |
| extraction.py | S | DTO 与 source_event_fingerprint 纯函数 |
| fts.py | D | 所有 search/health/rebuild 与 scope SQL，写异常包装已删 |
| job_claims.py | D | 原 fence + 本轮合回实际 Memory claim helper，准入后 token |
| maintenance.py | D | 原到期页当前 SQL 与 state event 同提交 |
| metrics.py | S | 内存/日志诊断，无 DB |
| models.py | S | Fact/evidence/target/query DTO、纯生命周期和 owner 形状 |
| mutation/__init__.py | S | 仅导出 |
| mutation/models.py | S | 原 request/context/receipt/result 身份与返回定义 |
| mutation/repository.py | D | find/reserve/reserve_dream/finalize；删除无效 find 参数 |
| mutation/service.py | D | 全部具名 DB/准备/应用/源/授权/receipt/Dream rollback 链 |
| partition.py | D | canonical owner、原 ingress、active selector 解析；无隐式写 |
| projections.py | D | 当前展示 Binding 点读，不回写事实 owner |
| quality/__init__.py | S | 仅包入口 |
| quality/audit.py | D | run/_checks 实际 SELECT；诊断不做完成 gate |
| quality/event_evidence.py | D | 同源资格投影、reader stream/finally close |
| quality/hygiene.py | D | scan/apply/invalid/page/purge/显式 FTS；raw source 重核 |
| quality/models.py | S | 报告/计划 DTO，无执行 |
| query.py | I | target resolver 与纯 query 构造 |
| ranking.py | S | 纯排名算法，无 session/外部回调 |
| read_scope.py | D | authorized/people/person/group/name 全部只读；一致 snapshot |
| rebuild/__init__.py | S | 仅导出 |
| rebuild/models.py | S | Selection/Run/Review/health 与纯 ID/时间 validator |
| rebuild/privacy.py | D | prepare/完整相关集合/原 JSON/phantom/交集不扩大 |
| rebuild/receipt_finalization.py | D | 完整原页 aggregate/source/ref/claim 复核和同笔 item/job receipt |
| rebuild/repository.py | D | 全部 run/item/stage/review/proposal/finalize/privacy/health 方法 |
| rebuild/service.py | D | core/service 全入口、模型/采用当前 proof、原 proposal ACK |
| rebuild/trusted_sources.py | D | 原 ingress 绑定时段、固定 mention/reply 来源复核 |
| rebuild/worker.py | I | restart pause；只推进原 executing 状态，不自动 resume |
| repository.py | D | 全 Fact/Job 方法、完整 prepared evidence/relation、missing counts |
| retrieval.py | I | 实际 reader/FTS/Provider/semantic 编排；无外部等待持 writer |
| runtime/__init__.py | S | 仅包入口 |
| runtime/capability_view.py | S | 固定声明纯投影 |
| runtime/contract.py | S | 固定 turn 权能结构与纯构造 |
| runtime/partition_lookup.py | D | reader 原 SELF 来源，无写 |
| runtime/query_plane.py | I | 真实 query consumer；read 后无隐式发布 |
| runtime/resolver.py | S | trusted origin 纯函数 |
| runtime/turn_session.py | I | actual SELF 来源→固定 turn capability |
| self_origin.py | D | initiative/automation 原 source、历史 receipt 可读性、原 SELF facts |
| self_reflection/__init__.py | S | 延迟导出，没有持久行为 |
| self_reflection/control.py | D | 原 cycle/request/report/daily budget/current source 全方法 |
| self_reflection/db_models.py | S | 原 cursor PK/window PK/initiative+first unique 与 FK |
| self_reflection/initiative.py | D | backlog/完整 claim/window/source/cursor；数字 ID/late receipt |
| self_reflection/models.py | S | 输入/输出/event/window/tool DTO，无 DB |
| self_reflection/reporting.py | D | 实际 report source reader→原 Social effect receipt |
| self_reflection/repository.py | D | 全部 source proof/cursor/scan/claim/checkpoint/finish/recovery/GC |
| self_reflection/service.py | I | quiet/原 paid checkpoint/逐 mutation receipt，不重置预算 |
| self_reflection/worker.py | I | 固定时段/manual/quiet/preempt/shutdown 原 cycle 恢复 |
| service.py | D | 全 Fact helper、current writer 授权、偏好按键短写 |
| subjects.py | D | 原 ingress 历史主体/名字/reply，所有数据库方法只读 |
| targets.py | D | 同一 snapshot 的 current/mentioned/reply 身份 |
| temporal.py | S | 原事件时间纯解析，无时钟 claim |
| temporal_filter.py | S | 仅 SQL 条件构造，无 execute |
| testing.py | S | 纯 DTO builder |
| tool_intent.py | S | 纯 query 意图解析，无执行 |
| validation.py | S | 原 claim/证据纯校验，actual callers 在 reader/锁外 |
| worker.py | I | 锁外批抽取；逐原 job/source/mutation/complete，保 ACK 与 claim fence |

附属删除：`src/yuki/persistence/job_claims.py`，经全仓 imports/calls 核对仅上述 Memory 两调用者，
没有为 Media/Automation 留隐藏兼容层。公共 Database/SQLiteConnection 的 R3 实现由 B reviewer
负责；本文没有将这次 Memory helper 删除代称公共核心复审。

## R3 后续补审：Plugin Host 与 Sandbox

本节在根冻结 `c0af6e8240548acbbd816edc07a3b964d1a16657` 后受托补审。
先读现行 `docs/plugin-development/architecture.md`、`background-services.md`、`storage.md`
及 `docs/operations/persistent-environment.md` 的 Manager/持久执行合同，再读实际源码。
R2 报告只用来定位。以下是追加覆盖，不能据此声称本 reviewer 全文审了 C 域全部 211 路径。

### 已核事务和回执

- `notification_repository.py` 全部具名 DB 方法及 helper：grant lineage 在 reader 准备，writer
  复核原 target/person/presence；publish 的原 manifest、media hash/terms 在外准备，当前 grant、
  media 到期和原 waits 在 writer 核实。新 event、outbox parts、background job、wait resume
  同笔提交；已有 event 先保原 manifest/原回执，不补发子任务。media cleanup 与发布分别重核
  活跃引用和到期，不能仅凭准备 reader 声称媒体仍可用。
- outbox/turn claim 的时钟在实际准入后，原 attempt、Work ID、预算、lease 不重置。
  已派发但没有平台 ACK 的通知到期变 uncertain；不会改回 pending 自动重发。
  `notification_delivery._record_outbound` 在同一 writer 完成 sent receipt 与原 ledger append，
  内容、附件读取、平台发送均在 writer 外；已确认平台 ID 在后续发布失败时仍保留。
- plugin session 的 message sequence 与 append 同提交，DTO/history 解码在外。真实 model
  caller 在无 DB 事务时请求模型，再保存 assistant 消息；这里原 SDK 会话本身不提供 SELF
  那种逐 request paid checkpoint，不能把未发生的能力写进报告。
- media create 只有确定未提交才删文件；unknown/已提交异常按原 handle 点读，有原登记返回
  原对象，无登记保留文件并传播原异常。现有 GC 由 DB 登记发现媒体，未知且没有登记的文件
  不会被它扫描到；这是保留的实际边界，没有新增七天 mtime 条件或二次补登记。
- Sandbox Task 的原 source/request/run/permission wait 与完成 continuation 原子单位保留。
  completion receiver 先完成 Bot DB receipt 再向 Manager ACK，unknown 不 ACK，后续以同一原
  request/run 重核。control completion 的原 audit/response 同提交，后台 continuation 只回原 Work。
- `ResourceCleanup.validate` 的实际 writer 调用只取固定 raw 字段、JSON SQL 小列和完整 request
  membership；没有 Python 解码 progress 或打开第二个 reader。已 close 的原资源范围没有扩大。
- Manager 的 `jobs.sqlite3` 由单独宿主进程负责，保持同步短 `with self.db` 原子单位。
  `PersistentManager`、ACPResources、CompletionOutbox 的所有实际 SQL/事务方法均已读。
  launch 先持久 dispatched/session 再外部执行；recover 只观察原 execd/session，unknown 不重派；
  文件任务在锁外实际执行，close 等原 file tasks 退出后关闭自己的连接。没有并入 Bot FIFO。

### 补审发现和修复

1. `sandbox/manager.main` 原 owner finally 从 serve_forever 才开始，recover 取消、socket/server
   启动失败都可绕过 `manager.close()`。`PersistentManager.__init__` 在打开 jobs.sqlite3 后才校验
   name、解析 ACP 配置，schema 初始化或后续构造抛错也漏关。解冻后真实 entry 和实际 SQLite
   连接回归取得 red，已将既有 owner try/finally 前移。name 和 ACP 配置在打开 DB 前处理，
   其余资源构造同样前移；随后 schema 初始化失败由构造者关闭未交付连接。server 创建后 chmod
   或 signal handler 失败也由相应 owner 收尾；保原 server.close/wait_closed → worker.cancel/gather
   → manager.close 顺序。recover 取消传播原异常，正常 serve 取消保持既有正常退出。
   不关闭外部注入 DB，没有新资源注册器或 started flags。
2. OneBot 特权 send 在 reader 阶段确认 Person superuser，实际 Social claim writer 只复核
   source/person/presence/binding。role 在 FIFO 排队时撤销后仍能 claim 并发送。
   真实 role 撤销/FIFO barrier 回归取得 red 后，已复用已有 `_require_privileged` 的 supplied writer
   与 `_is_real_superuser`，在实际 claim 前重核同一既有权限。原成功 receipt 仍按原结果返回；
   integration 回归确认首次成功后撤销 role，再读相同原结果不归一化历史正文、不再次发送。

两项实际修复与验证都在固定性能窗口结束后进行，没有在冻结测量中改变产品。

另按 root 的实际失败假设追读 `Execd.connect`、当前安装的 websockets `ClientConnection.close`
和全部 pump 消费：生产 close 的 send_context 将 BrokenPipe/OSError 转为 ConnectionClosed，
close 自行处理已关闭异常；唯一 pump 是 attach.drain，只接收丢弃消息、pop 内存字典和关闭连接，
不读取或写入 jobs.sqlite3。因此“connection close OSError 后 pump 仍写已关 DB”不成立，未写
fake OSError 回归或增加防御分支。close 期间外部取消可能留下尚未 join 的网络 pump，属于纯网络
owner 的原边界，本次数据库任务不扩大修复。

### 追加路径分类

等级沿用本轮：D=实际 DB 方法全文；I=实际调用/错误/paid 编排；S=结构/Schema 定义；
X=另一 reviewer 明确负责，不据此宣称全文覆盖。路径以 `src/yuki/` 为根。

| 路径 | 等级 | 本节实际阅读范围 |
| --- | --- | --- |
| plugin_host/repository.py | D | Installation/Config/State/Audit 全部方法、CAS、容量、expiry 与转换 |
| plugin_host/configuration_service.py | D | read/revision/schema validation/save 同 writer CAS |
| plugin_host/config.py | I | 实际 facade scope/validator/配置 CAS 消费 |
| plugin_host/storage.py | I | 同 owner、容量、CAS 与原 domain-conflict 重试 |
| plugin_host/audit.py | I | 原 best effort optional audit；Cancel 不吞 |
| plugin_host/session_repository.py | D | 全部 create/get/append/read/close/reset/delete/expiry/ephemeral |
| plugin_host/session_facade.py | I | SDK 实际 reader/mutation 参数传递 |
| services/plugin_sessions.py | I | 原 conversation lock、history、paid request、assistant receipt |
| plugin_host/media_artifacts.py | D | create/get/cleanup、线程文件退出与未知结果保留 |
| plugin_host/notification_repository.py | D | 全部 grant/revoke/publish/claim/fence/finish/retry/turn/context/helper |
| plugin_host/notification_delivery.py | D/I | 真实 prepare/send/settle/ledger、lease 与媒体读取编排 |
| plugin_host/background_turns.py | I | 原 Work paid/claim/fence、park/defer/fail、原 lease keepalive |
| plugin_host/ownership.py | D | 全部 owner/scope/helper 的 supplied-session SQL |
| plugin_host/direct_delivery.py | D | 原 Social identity/prepare/当前 source point proof |
| plugin_host/facades.py | D/I | 全部实际 SQL/helper 与 paid DB consumer、OneBot claim/settle；纯 SDK facade 未全文 |
| plugin_host/db_models.py | S | 实际 owner/FK/PK/unique/status、outbox parts、Work 唯一链接全部定义 |
| plugin_host/manager.py | X | 已定位 repository 委托；完整 invocation/manager 及真实 DB 故障收尾由 root/B 负责，本文不替其给出通过结论 |
| sandbox/task_repository.py | D | 全部 source/intent/checkpoint/permission/completion/continuation |
| sandbox/resource_cleanup.py | D | 完整 selected resource/closure proof 与 writer SQL validate |
| sandbox/completion_receiver.py | I | 原 receipt 在前、Manager ACK 在后、permission 原 cursor |
| sandbox/continuations.py | D/I | 原 Work 指向与当前 continuation 状态 |
| sandbox/continuation_worker.py | I | 原 Work 唤醒，不起独立 Agent 循环 |
| sandbox/control_completions.py | D | 原 principal/request/run/audit 与当前 response receipt |
| sandbox/completions.py | D | Manager 独立库 record/pending/ack 原原子单元 |
| sandbox/client.py | I | Task intent→socket→原 run bind/observe；原 close request，不重派 |
| sandbox/manager.py | I | 完整 entry/startup/server/worker/close，发现 owner 起点遗漏 |
| sandbox/persistent.py | D/I | 全部 SQL/事务与 recover/submit/launch/service/file_request/close；纯文件执行未全文 |
| sandbox/acp_manager.py | D/I | 全部独立库方法及原 request/native receipt/purge/ACK |
| sandbox/acp_config.py | I/S | load_agents 全配置验证；构造前可完成的真实输入处理 |
| sandbox/db_models.py | S | request PK/run unique/source RESTRICT、continuation FK/状态全部定义 |

### 空来源和 reader 的等价减法

SELF `save_checkpoint(expected!=None)`、Dream 无 selected fact ID、ReadScope 无 requester/current_space
原来会打开没有 SQL 的 reader，当前 core 的真实 checkout 使它们有借还成本。已把原纯条件移到
context 外。Dream `person_roots` 无 person ID 原来还调用 current_person_ids 的空 IN 查询，也改为
原空结果直接返回。有来源的 current identity/source proof 没变。`mutate_resolved` 已在外层判断
`additional_evidence`，空 tuple 不进入 reader；这条候选不是现存问题，没有增加判断。

`_events_current` 的 cursor、claim/context、checkpoint 五个真实调用点可能准备空集合；原函数仍
构造 json_each 和完整 raw field 匹配 COUNT。现在只对 count=0 直接对同一 selected CTE 做 NOT EXISTS，
原 query 的 join/where/order/limit/offset 完整保留。有来源仍用原 PK/full field 比较，不能借此省掉
新增成员证明。真实 SQL 测试覆盖空集合后插入成员（失效）、原 LIMIT 0（仍空）、原 OFFSET 后空窗
插入新成员（失效）。这里没有测量它的独立耗时，也不据此声称最终 benchmark 提速。

### 补审验证与最终状态

- 旧产品真实 red：Manager recover cancel/server start/name/ACP config/schema 五项与 OneBot
  FIFO role 撤销一项，6 failed；再单独补跑 chmod/signal 两失败窗口，四个 startup 参数均 red。
  失败分别来自实际连接仍可 SELECT、撤销 role 仍发生发送，不是测试假设或 fixture 错误。
- 修复后首组 Manager/插件 known-result/audit/role：17 passed。随后增加正常 serve 取消和来源空集
  回归，连同 paid tail、真实来源变更、Dream plan 与 Memory person identity 共 53 passed / 15.04s。
- 完整 SELF initiative memory/quiet priority、Dream plan、notification idempotency/turn queue、
  plugin session/media/background recovery、Manager ACP 与 ACP lifecycle 共 169 passed / 57.89s。
- OneBot 真实本地 fixture 首次发送后 role 撤销、同原 receipt 重放：1 passed / 0.98s，原发送仍仅一次。
  53 与 169 两组都包含 `test_dream_plan_transactions.py` 的 10 个参数化 node；先前 17 项首组
  已包含在 53 项中。最后 1 项来自独立 integration 文件，不与这两组 unit node 重复。此前 385/23
  等 Memory 组也有重叠，不相加为独立总数，更不称全仓验收；全部是假平台/FakeLLM，没有真实网络
  Agent 或平台发送。
- Ruff check 本轮 Memory、三个补审产品文件和全部本轮测试通过，13 个相关文件 format 检查通过；
  git diff --check 通过。Memory87 + facades 在 Windows 类型目标 88 source files 通过；包含 Linux
  Manager/persistent 的 Linux 目标 90 source files 通过。首次按 Windows 检查 Linux 宿主模块时发现
  原有 os.geteuid/chown 平台类型错误，未用 ignore 改产品，随后按其真实运行平台核验。
- 补审日志为 `D:/Temp/yuki-sqlite-validation/r3-c-supplement-{red,green,contracts}.txt` 和
  `r3-c-manager-startup-red.txt`。已复读六个产品文件的实际最终 delta；本节明确列为 D/I 的范围内
  无确认而未修的数据库缺口。本文交付是 Memory 87 路径分级复审，加 Plugin facade/仓库和 Sandbox
  实际 DB 方法补扫；不覆盖 C211 全部源码，不替 root/B 的 PluginManager 真实故障修复验收，不称
  全局交付完成。产品修改已停止，最终冻结、全仓检查、CI/PR 与性能由根报告。

## 最后只读总表核对

补审完成后按受托范围核对当前 `sqlite-writer-acceptance-20261023.md` 与任务书，不重读全源码，
没有运行重测试或修改根验收表。按表第一列展开编号区间，W01–W73 和 V01–V50 各恰好出现一次，
无遗漏、重复或额外编号。71 个测试文件引用都能在当前仓库唯一定位；任务书链接、性能脚本和
collect_surface/collect_schema 文件存在。文件存在不代表该场景或整个任务已经通过。

向 root 提交以下记录口径项，由根统一处理：

- 34 个测试引用省略 unit/integration 前缀，与表头“相对 tests”字面不一致；都能唯一定位，
  没有不存在的测试。可以补前缀，或明确后续唯一 basename 的简写规则。
- W32–W34 合列未明写 emoji/媒体/跨入口，root 复审实际覆盖了这些消费者；建议总表补关键词，
  区分编号完整与条目细义。W40–W41/V19 可补 Manager startup owner 的真实 SQLite 回归，
  W37–W39/V44 可补 privileged role 和 root/B 的 PluginManager 故障证据；不是新增产品缺口。
- W63 对应任务书 §9。当前性能报告有 CPU2 证据，没有受限内存测试结果；最终表述应如实保留
  已测资源范围，不将 CPU affinity 说成验证了所有资源约束。后续性能与交付结论归根。
- 任务书约 515 行的 resolve_mutation_commit 说明省略了原显式 mutation_committed 的优先级；
  当前实际函数在 uncertain 后、not-ok 前保留显式提交事实，代码行为正确，说明可补清楚。

总表明确 W65 等待最终 PR/CI、W66 本次未部署，并保留独立 DB、进程内 FIFO、unknown 与测试重叠
边界，这些口径准确。此次只读核对不能替代各 owner 尚在收束的实际验证或全局验收。
