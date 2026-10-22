# 第 2 轮 A 域独立复核

基线 `cdb49eadc0c7cd16060bf0329234ae69611e62bd`。先读第 1 轮 A/D 报告，再独立读取源码、
重建调用点与方法边界；不把第 1 轮结论重标为本轮证据。源码未改，未调模型、未访问生产。
只做了一项锁定依赖的内存 SQLite 事件探针，结果见 R2A03；没有运行项目测试套件。
用户确认当前延迟可接受，以下不把架构任务改造成新的延迟事故。

## R2A01：新任务单缺了 Automation 的完整写入单元

本轮读取 `automation/repository.py:606-910/970-1100`、
`automation/worker.py:113-334`、`automation/service.py:186-368` 和 `automation/context.py` 全文。
U01–U32/W01–W66 当前没有与原 Automation 定义/version/claim/run/occurrence 相对应的独立任务。
U14–U18 处理的是 Work；把 automation 最终绑定 Work 不能代替此前真实写入链。

应在现有任务表补明确归属，不增加新的 runtime 调度器：

| 实际方法 | 事务/业务边界 | 实施重点 |
| --- | --- | --- |
| create:61、set_status:410、cancel:441、resume:465、schedule_now:497、publish_task:524 | 定义、版本、创建幂等键与真实 actor/目标/审计 | service 已在外 validate/prepare task；writer 核真实来源和当前版本，不复制新的 Plan |
| claim_due:606 | 候选读 + 每 128 个候选一次 writer UPDATE/返回原版本 | 初次候选是 `.all()`，不是读阶段已经完整 keyset 分页；已有短 writer 批次保留，若做规模优化须准确区分两层 |
| release_claim:710 | 仍是 sessions+begin，按 claimed_by UPDATE | 进公共 writer；不是可丢 cleanup |
| renew_claim:738 | IMMEDIATE 中核 claimed_by 和 SQL 当前未过期 | `worker.py:117-122` 在等待 writer 前算 until。writer 后核未过期不等于这个 until 一定延长了租约；使用现有 clock/lease duration 在实际准入后计算，不新增时间阈值 |
| create_run:751 | version、scheduled_for、max_runs、唯一 occurrence 同事务 | 原 occurrence ID/计划时间保留；不能重建一个新 run 掩盖未知提交 |
| settle_interrupted_occurrences:852、settle_work_occurrences:894、finish_run:970 | 从原 Work/原版本结算 occurrence、累计计数和下次计划 | 不是重跑模型；finish_run 的计划计算保持原接受版本，必要纯编码移前 |

`AutomationWorker._process` 读 scene/identity 后退出 session，再进入 turn、Work lease、模型。
`bind_automation_conversation` 没有隐式 ensure/create，只读原 Conversation/alias；本轮排除了一个
可能的隐藏写者疑点。不要把其名字里的 bind 当成需要额外 writer。

## R2A02：supplied-session 分词是接口风险，当前调用者已准备

独立 AST 枚举全仓源码中带 `session=` 的 `append/append_inbound/append_external`：

| 当前调用点 | prepared_terms |
| --- | --- |
| `client_api/works.py:474` | 已显式提供 |
| `persistence/scoped_event_uow.py:117` 内部转发 | 已显式提供 |
| `plugin_host/facades.py:2657` | 已显式提供 |
| `plugin_host/notification_repository.py:562` | 已显式提供 |
| `social/service.py:1553` | 已显式提供 |

两个 append 入口在 supplied session + prepared_terms=None 时依然可能调用 to_thread，
但目前没有找到生产调用者实际漏传的证据。U03/U04/W13/W14 应写成“保留并核对现有准备边界”，
不能将它宣传为已确认热点、重复迁移这五个正确调用，也不需要另加内容/大小准入规则。
AST 直接调用枚举不证明任意外部代码动态调用安全；测试 helper 与未来接口合同仍需核对。

`identity/person_state.py:prepare_message_account_observations`、
`prepare_canonical_person_observation` 的 apply closure 捕获了原 session 和 live ORM。
它们不能直接在新 read_session 准备后把原 closure 带到新 writer 使用。
要沿原 native account/stage/time、完整账号/名片/mention 选择条件准备不可变资料，
writer 核当前依赖再对其自己的 ORM 写入；实际原回执和历史 Assignment 不能被今日账号归属覆盖。
这是 U03/U04 中“把准备前移”真正需要改的具体 API，而不是再造通用 Command/Plan。

## R2A03：savepoint、HintSession 与真实外层提交不能混用

全文重新读取 `admin/operations/base.py`、`persistence/database.py`、
`persistence/unit_of_work.py`、`persistence/commit_hints.py`。

`database_command:154-230` 取得外层 writer，授权/原 receipt 检查后进入 `begin_nested()`：
mutate 若返回可缓存失败，只回滚 savepoint，再在外层写失败 audit/receipt。
成功则变更、审计、control receipt 同一次外层 commit。savepoint 不是第二个 writer，不能删掉。
也不能让它获取第二次 FIFO admission。

**新实测事实：HintSession.after_commit 不是外层物理提交确认。** 使用本机
SQLAlchemy **2.0.51**、aiosqlite **0.22.1**，独立内存 DB、专用 ProbeSession：

```text
BEGIN IMMEDIATE -> begin_nested -> SELECT 1 -> release savepoint
after_release_before_outer_commit: [('after_commit', True, True)]
outer rollback 后仍为: [('after_commit', True, True)]
```

事件字段依次是 after_commit、in_nested_transaction、in_transaction。
因此不能以 HintSession event/CommitHints.publish/SSE hint 作为 writer 清理、完成回执或外层
提交成功的证明。hint 本来就是可丢唤醒，不为此新增可靠通知总线或第二 journal。
保持或调整提示触发均以原 GET/receipt 事实为准；不要把它作为公共 owner 的释放条件。

现有 `test_client_views.py:test_post_commit_hook_never_publishes_rollback...` 只覆盖直接
rollback 与空 session.begin；未覆盖 savepoint release 后 outer rollback。
应补真实管理 savepoint 行为和提示消费者事实来源，不冻结提示次数。

## R2A04：HTTP 结果分类必须覆盖最外层清理与客户端状态

全文读取 `client_api/http_errors.py`、`admin_api/client.py`、`client_sdk/outbox.py`，
读取 `client_sdk/client.py:72-118/283-406`、`ClientRequests.get` 和 ClientMessages.submit。

`database_error` 目前依赖原生 SQLite code、statement 非 COMMIT、rollback_failed 注记，
将确认可重试的 busy 作为 unavailable，其他 SQLAlchemy 错误 unknown。
`PersonalClient.submit` 遇非 unknown ClientProblem 会把某些请求恢复 draft；unknown 保留
原 submitted UUID。新公共 writer 如果丢失 commit/rollback/close 阶段事实或包装成可重试 busy，
会改变客户端是否可能再次 POST。W06/W24/V22 必须同时验证服务端映射和客户端 outbox 状态。

`CommandExecutor.external_command:232-345` 先 reserve/提交，再执行 effect，再短 writer 结算。
异常时先查 succeeded 原 receipt，再 shield mark_unknown；原 effect 不在 writer，不能整段重试。
shield 只保护内部任务不被第一次取消取消，不是调用方已等待物理清理完成的独立证明；
公共数据库层仍须持有该任务的真实事务退出归属。

`test_client_api.py:235-268` 已有实际 commit 后抛 COMMIT OperationalError 的 ACK 丢失测试，
但不是驱动在线程中仍执行时的取消测试。保已有 one-time link code/Device 唯一性并补新阶段。
`test_admin_external_effects.py` 全文核了失败文件写、enable 已发生后 ACK 丢失、配置回滚未确认；
原 UUID 不能因统一 writer 被替换。

## R2A05：read_session 改造须保留 SQL 与对象所有权边界

§4.1.1 的内部 factory + read_session/IMMEDIATE 方向可行；不需要只读 Session 子类或 SQL 拦截器。
重新读取 Config/Identity/Receipt/Projection 相关接口后确认：

- `RuntimeConfigService._mutation_session:534-544` **当前没有配置 mutex**，明确复用 caller writer。
  U06“现有配置锁”应区分 `ConfigFileService._lock`（外部文件操作）与此无锁事务入口，
  避免实施时凭空新增锁。read_session 不改变传入 writer 的 autoflush。
- `identity/event_author.py`、`ownership_interval.py`、`mentions.py`、`receipt_compat.py`、
  `conversation/event_entry.py` 的对应函数是传入 session 的只读 helper；它们可能在 writer 中服务
  身份/历史作者/引用校验，不能自己另开只读快照，也不能关闭 caller autoflush。
- `conversation/correlation.stamp_conversation_correlation` 对传入 row 赋属性；
  “stamp”本身不代表自管 writer，必须看 row 是否已 attached。模型诊断 fallback 是先对新 row
  stamp，退出读取后才 attach；不能全局把 correlation helper 判成 SQL 写。
- `PromptProjectionRepository.prepare_commit:244-418` 当前确实在明确 BEGIN 的只读快照准备
  immutable chunks、source/privacy，再返回带 caller session 的 publication。新入口保多查询快照，
  不再为每个纯 SELECT 多发 BEGIN，也不复制整份旧 frozen history。
- `schema_guard.py` 全文核查：独立 NullPool 引擎只读 sqlite_master/PRAGMA table_info/触发器，
  不安装主 Database 的 WAL connect hook，无业务 DML，不是遗漏 runtime writer。
- `sqlite_diagnostics.py` 的 SQL regex 是计时分类，不能被“禁止 SQL 拦截器”误删；它不负责
  允许/拒绝 SQL。原 held_lower_bound 仍是观测下界，不可改名为精确 writer wait。

`test_runtime_config_read_snapshot.py` 已读真实 owner+override 一致视图场景和只读 SQL 意图；
其中 monkeypatch 公开 `database.sessions` 的实现断言应改测试 helper，保真实一致性行为。

## R2A06：第 1 轮 N/S 中要补入调用归属的文件

N 只表示“无直接 DB API”，不是“完全不参与持久化”。本轮重新枚举所有 N 文件的 await/导入：

- `capabilities/results.py:ToolResultBudgeter.render` 可调用 write_media_artifact 和 write_artifact；
  应从纯渲染排除项转为**已核间接持久化消费者**，真实 writer 在 ToolArtifactRepository。
  原工具效果已成功不能因展示归档失败被改写或重新执行；不需要结果层再开 writer。
- `client_sdk/client.py` 调用本机 Outbox；`acp_adapter/agent.py:82-185` 创建 sessions.sqlite3
  和每 Conversation 的本地 outbox。前者不是无状态纯 HTTP wrapper，后者不连接服务器主库。
- `automation/tools.py` 间接写 create/update/pause/resume/cancel/run_now 和创建失败审计，
  消费者归 R2A01，不能因无 ORM import 而从自动化迁移矩阵消失。
- `gateway/providers/telegram_ingress.py:338-505`、`feishu_ingress.py:300-406`
  启动 create_presence、冷 Space create_binding、Telegram 群地址迁移、title observation、edit
  都有真实写。U07/W17 应明确包括这些入口，网络 get_me/is_member 先在 session 外完成。
  它们不是通用 normalize_message 的纯 Provider 渲染部分。
- App ingress 的 receive 读取来源后启动原 Processor；其 on_finished 先创建 settlement task，
  取消时收拢该 task。不要在其 writer 内 join 原 Main；其任务可能需要 writer 结算。

本轮未找到仅依赖 SQL 字符串拼接、绕过主 Database 的新增在线主库连接。
该结论限于逐文件 AST/导入与已读调用，不把动态 Python 扩展解释成已穷尽证明。

## R2A07：独立 SQLite 与 D 域再次 challenge

重新读取 `client_sdk/outbox.py`、`web/bridge_state.py` 全文，以及 WorkspaceStore 的
连接/transaction/publish 入口和 ToolArtifactRepository 的 storage-lock/writer/unlink 路径。

1. Outbox 每次方法独立 sqlite3.connect/commit/close，没有跨 HTTP 等待持同一事务；是原稿与 UUID
   事实，不能搬到 Bot writer 或按 cache 删除。同步调用是否要移线程属于独立性能工作，当前无证据
   需要为此次统一主库额外改造它。
2. BridgeState 只有首次读可能建 cache schema；正常命中读不进入 IMMEDIATE，写值才清过期/upsert。
   不应把 A10 表的“_access 短 IMMEDIATE”概括成每次查询都写；cache decode 在连接关闭后。
3. WorkspaceStore 的 manifest fence 包含 rename/短 stat，是独立文件/跨进程事实；D04 的
   ToolArtifact storage-lock 是另一个资源。不能统一命名后误认为可用一把 Bot lock 覆盖两者。
4. ToolArtifactRepository 当前已经明确 metadata commit 不明时不直接 unlink 原件；D04 对该点
   是保留现有行为，不是尚未实现的新修复。同步 refs helper 不获取 storage lock，避免 writer 反序。
5. D02 生命周期顺序不能代替 SQLAlchemy/aiosqlite 真正退出证明；R2A03 又补了 savepoint event 反例。
   writer 停止应复用当前 resource lifecycle，不造永久 failed-owner 表或人工解锁机制。

## 对主任务书的建议变更

必须补：Automation 原始存储/worker 单元；savepoint/commit hint 与外层真实 commit 的区分；
HTTP/outbox 端到端错误分类；gateway 冷启动/迁移/edit 的方法映射。
收紧表述：当前 supplied-session 分词已准备、claim_due 初次 `.all()` 不是全部读分页、
RuntimeConfig mutation 无配置 mutex、BridgeState 普通读不一定写、Artifact unknown 原件保护已经存在。
继续删减：不新增全局 SQL 准入拦截、generic Plan、第二 journal 或独立 cache 的统一网络队列。

## R2A08：HintSession 可以整链删除，显式 live hint 保留

全仓重新枚举 commit/engine.begin/CommitHints 调用后，主库 runtime 在全量迁移完成后应都通过
同一个 IMMEDIATE；create_schema/history index 重建是离线生命周期，独立 SQLite 不属于主库 SSE。
因此 U34/W69 可直接采用“根 commit 确认后公共入口 publish”，删除 HintSession、after_commit
监听、session.info['commit_hints'] 与 factory sync_session_class 注入，无需补新通知机制。

不过 `execution_trace/recorder.py:475/510` 两个显式 publish 必须保留：它们在 live span
开始/结束时通知 App activity 变化，原本就没有数据库提交，不是漏迁的裸 writer。

正常已确认根 commit 后，释放 writer 再同步 publish，中间不要增加可独立取消的 await/task。
若重复取消发生而公共清理最终确认 commit 已成功，可以发 hint 后继续传播原取消；
未知 commit 不因 hint 变成确认成功。`ClientViews.stream:288-331` 的 15 秒 timeout 只发
keepalive，不重新查询；漏掉最后一个成功变化的 hint 会一直旧到下个 hint/重连，
所以不能把同步 publish 搬成无人收拢的后台 task。仍然不承诺可靠/持久投递。

## R2A09：旧争锁观测不应整包留在新 owner 上

全文重读 `persistence/sqlite_diagnostics.py:1-349`，核了 health 消费者与对应旧测试。
统一后拥有者已明确，不再需要根据多个连接的首次写猜可能 holder。

| 具名旧实现 | 处置 |
| --- | --- |
| `writers` physical-ID dict、sequence、coroutine/operation holder records | 删除；公共 owner 直接知道当前写入阶段与原调用归属 |
| `finish`、`observation_end_unknown`、holder 收集与 `sqlite_write_contended holders=` JSON | 删除；不再每次竞争扫描所有观测连接 |
| `first_write`、`held_lower_bound`、`held_release_unknown` 与 SQL regex 的首次 writer 推断 | 删除或退役对应当前输出字段；不能改名伪装成精确 admission wait |
| `transaction_call/physical_exit` 为日志 patch dialect.do_commit/do_rollback/do_close/do_terminate | 删除诊断 monkeypatch；实际 driver 收拢由公共 DB 正确性实现负责 |
| engine.raw_connection monkeypatch | 若公共入口已有 inclusive checkout/BEGIN 阶段计时则删除；不额外 acquire 一个连接只为测量 |
| RELEASE 后检查 driver.in_transaction，pool checkin 推断 holder 结束 | 删除所有权猜测；savepoint 不改变根 owner，实际退出按公共层事实 |
| `TimingSummary` | 保留；DiagnosticWriter、WorkScheduler 真正在用，不是旧锁包装 |
| before/after_cursor_execute 的纯 SQL read/other 总耗时与原生 error code | 需要时保为薄观察，不参与准入/重试；不能从它证明物理取消已完成 |

新 owner 的必要计时只在内存汇总 admission、BEGIN/获取连接的 inclusive 时间、SQL/flush、
commit/rollback、异常 cleanup；说明嵌套/重叠，不虚构 pool wait、driver queue、SQLite wait 分量。
已经收到 CancelledError、pool invalidate 或 driver._connection 属性变化都不独立授予释放资格；
删除旧观测不能删除公共层真实等待驱动退出、保原异常和停止失效 owner 的正确性。

`test_sqlite_physical_exit.py`、`sqlite_diagnostics_helpers.holders` 当前很多断言冻结旧观测结构，
需要改测下一 writer 不抢在前 driver 真实退出前进入；保必要反例，删除只检查 holders JSON 的实现测试。
`health.py` 同步读取新汇总；日期历史报告仍按原指标定义保留，不能重写旧 lower bound 成实际等待。

## R2A10：主库旧 Web 索引、ASR 与媒体分析明确归属

`persistence/web_repository.py` 当前只有 `cleanup_expired`，**没有 save_run**；源码全仓也无
save_run 调用。它保留已退役 WebSearchRun/Source 的历史行清理，不能因本次重构恢复退役 index。
`container.py:972` 的维护调用属于主库：先只读取最多 128 IDs，无候选返回，writer 中再次按
created_at 条件 DELETE，source 子行由外键 cascade。需要明确纳入某 U/W，不是 BridgeState cache，
也不能用 Memory W56 泛指所有缓存。保持短写/原 FK；无需新队列或删除历史正文以外的事实。

`MediaAnalysisRepository` 全文核读：find_cached 只读；save/associate_event 仍裸 begin；
cleanup 先候选后短 IMMEDIATE，原源事件关联不因 cache hit 转移。它们已经可映射 U13/W23，
但任务描述应保留 cleanup 方法，避免只列 save/associate_event。其 JSON 编码在 writer 外。

ASR 的对应真实链是 `processor._prepare_audio_source` → ASRService.prepare（下载/转换/付费
transcribe）→ EventLedger.set_audio_transcript → ScopedEventLedgerUnitOfWork.set_audio_transcript/
`_set_derived_text:418-481`。ASRService 本身没有 SQLite；
`_prepare_derived_text` 只读快照核源，分词在外，然后短 writer 比较精确 EventRecord 与
Conversation token/generation、原 transcript 是否已保存，再更新 transcript+FTS。
重备时仍持同一 ASR 输出，不重新购买识别。应明确挂 U04/W14（派生正文/FTS）与
U13/W23（媒体观察），而不是当 Memory mutation 或新 model_runtime 账本。

ASR/derived text 对应 `test_derived_text_transactions.py`；MediaAnalysis 与 retired Web cleanup
现有测试按清理/源关联行为复用，补无候选不进 writer、并发过期/source变化的实际场景。

## 本轮验证与剩余工作

执行：锁定依赖内存 SQLite 的 savepoint event 探针（R2A03）；文档 diff/清单校验。
未执行：项目测试、真实并发压力/driver取消、模型/平台/生产操作。

第 3 轮仍须补大型文件尚未全文的 DB helper/组合 closure、Person forget/merge 全选择依赖、
route 接纳后复核与所有 supplied-session 消费者。下表保留第 1 轮等级并写本轮实际覆盖，
没有把上轮 F 直接认作本轮 F；结构扫描也不冒充第二次全文审阅。

## 第 2 轮逐文件覆盖与相对变化

F=本轮全文；T=本轮具名方法/精确范围；S=本轮重新AST/导入/方法调用分类，未冒称全文；N=本轮重核排除的无直接DB部分（可能消费另列服务）。上轮F不会自动成为本轮F。

| 文件（相对 src/yuki） | 行数 | R1 | R2 | 本轮证据/边界 |
| --- | ---: | --- | --- | --- |
| `acp_adapter/__init__.py` | 6 | N | N | 本轮重新核导入/await/SQL字面量，无自管DB；纯结构或外部协议/已列领域委托 |
| `acp_adapter/agent.py` | 394 | S | T | 82-230；本机Outbox创建/提交身份，余纯ACP framing按结构核 |
| `acp_adapter/cli.py` | 51 | N | N | 本轮重新核导入/await/SQL字面量，无自管DB；纯结构或外部协议/已列领域委托 |
| `adapters/__init__.py` | 1 | N | N | 本轮重新核导入/await/SQL字面量，无自管DB；纯结构或外部协议/已列领域委托 |
| `adapters/onebot/__init__.py` | 1 | N | N | 本轮重新核导入/await/SQL字面量，无自管DB；纯结构或外部协议/已列领域委托 |
| `adapters/onebot/activity.py` | 21 | N | N | 本轮重新核导入/await/SQL字面量，无自管DB；纯结构或外部协议/已列领域委托 |
| `adapters/onebot/card_parser.py` | 122 | N | N | 本轮重新核导入/await/SQL字面量，无自管DB；纯结构或外部协议/已列领域委托 |
| `adapters/onebot/media.py` | 77 | N | N | 本轮重新核导入/await/SQL字面量，无自管DB；纯结构或外部协议/已列领域委托 |
| `adapters/onebot/message_id.py` | 9 | N | N | 本轮重新核导入/await/SQL字面量，无自管DB；纯结构或外部协议/已列领域委托 |
| `adapters/onebot/normalizer.py` | 472 | N | N | 本轮重新核导入/await/SQL字面量，无自管DB；纯结构或外部协议/已列领域委托 |
| `adapters/onebot/provider_adapter.py` | 152 | N | N | 本轮重新核导入/await/SQL字面量，无自管DB；纯结构或外部协议/已列领域委托 |
| `adapters/onebot/sender.py` | 168 | N | N | 本轮重新核导入/await/SQL字面量，无自管DB；纯结构或外部协议/已列领域委托 |
| `adapters/onebot/social.py` | 132 | N | N | 本轮重新核导入/await/SQL字面量，无自管DB；纯结构或外部协议/已列领域委托 |
| `admin/__init__.py` | 23 | N | N | 本轮重新核导入/await/SQL字面量，无自管DB；纯结构或外部协议/已列领域委托 |
| `admin/audit.py` | 242 | F | S | 本轮重新核全部方法与DB/session/DDL静态边界；未全文SQL/业务分支留R3 |
| `admin/authority/__init__.py` | 1 | N | N | 本轮重新核导入/await/SQL字面量，无自管DB；纯结构或外部协议/已列领域委托 |
| `admin/authority/capabilities.py` | 110 | N | N | 本轮重新核导入/await/SQL字面量，无自管DB；纯结构或外部协议/已列领域委托 |
| `admin/authority/command_types.py` | 341 | N | N | 本轮重新核导入/await/SQL字面量，无自管DB；纯结构或外部协议/已列领域委托 |
| `admin/authority/commands.py` | 85 | N | N | 本轮重新核导入/await/SQL字面量，无自管DB；纯结构或外部协议/已列领域委托 |
| `admin/authority/decision.py` | 114 | N | N | 本轮重新核导入/await/SQL字面量，无自管DB；纯结构或外部协议/已列领域委托 |
| `admin/authority/json_types.py` | 50 | N | N | 本轮重新核导入/await/SQL字面量，无自管DB；纯结构或外部协议/已列领域委托 |
| `admin/authority/operations.py` | 118 | N | N | 本轮重新核导入/await/SQL字面量，无自管DB；纯结构或外部协议/已列领域委托 |
| `admin/authority/operators.py` | 114 | F | S | 本轮重新核全部方法与DB/session/DDL静态边界；未全文SQL/业务分支留R3 |
| `admin/authority/principal.py` | 119 | N | N | 本轮重新核导入/await/SQL字面量，无自管DB；纯结构或外部协议/已列领域委托 |
| `admin/authority/problems.py` | 62 | N | N | 本轮重新核导入/await/SQL字面量，无自管DB；纯结构或外部协议/已列领域委托 |
| `admin/authority/targets.py` | 61 | N | N | 本轮重新核导入/await/SQL字面量，无自管DB；纯结构或外部协议/已列领域委托 |
| `admin/authority/tokens.py` | 38 | N | N | 本轮重新核导入/await/SQL字面量，无自管DB；纯结构或外部协议/已列领域委托 |
| `admin/config_files.py` | 482 | T | T | 233-402；文件锁、replace/restore、取消收拢，无DB事务 |
| `admin/config_owners.py` | 16 | S | F | 本轮独立全文；正文对应R2A条目；保原caller session/事实 |
| `admin/config_registry.py` | 160 | N | N | 本轮重新核导入/await/SQL字面量，无自管DB；纯结构或外部协议/已列领域委托 |
| `admin/config_service.py` | 1827 | T | T | 515-825；caller mutation、纯读initialize、set事务组合；余结构核 |
| `admin/config_spec_helpers.py` | 77 | S | S | 本轮重新核全部方法与DB/session/DDL静态边界；未全文SQL/业务分支留R3 |
| `admin/config_specs_asr.py` | 77 | N | N | 本轮重新核导入/await/SQL字面量，无自管DB；纯结构或外部协议/已列领域委托 |
| `admin/config_specs_future.py` | 45 | N | N | 本轮重新核导入/await/SQL字面量，无自管DB；纯结构或外部协议/已列领域委托 |
| `admin/config_specs_hot.py` | 525 | N | N | 本轮重新核导入/await/SQL字面量，无自管DB；纯结构或外部协议/已列领域委托 |
| `admin/config_specs_planner_plugins.py` | 172 | N | N | 本轮重新核导入/await/SQL字面量，无自管DB；纯结构或外部协议/已列领域委托 |
| `admin/config_specs_protected.py` | 122 | S | S | 本轮重新核全部方法与DB/session/DDL静态边界；未全文SQL/业务分支留R3 |
| `admin/config_specs_restart.py` | 512 | N | N | 本轮重新核导入/await/SQL字面量，无自管DB；纯结构或外部协议/已列领域委托 |
| `admin/config_specs_social.py` | 7 | N | N | 本轮重新核导入/await/SQL字面量，无自管DB；纯结构或外部协议/已列领域委托 |
| `admin/config_specs_tooling.py` | 64 | N | N | 本轮重新核导入/await/SQL字面量，无自管DB；纯结构或外部协议/已列领域委托 |
| `admin/control_resolution.py` | 270 | S | F | 本轮独立全文；正文对应R2A条目；保原caller session/事实 |
| `admin/models.py` | 296 | N | N | 本轮重新核导入/await/SQL字面量，无自管DB；纯结构或外部协议/已列领域委托 |
| `admin/operations/__init__.py` | 39 | S | S | 本轮重新核全部方法与DB/session/DDL静态边界；未全文SQL/业务分支留R3 |
| `admin/operations/base.py` | 345 | F | F | 本轮独立全文；正文对应R2A条目；保原caller session/事实 |
| `admin/operations/config.py` | 184 | S | F | 本轮独立全文；正文对应R2A条目；保原caller session/事实 |
| `admin/operations/groups.py` | 35 | S | F | 本轮独立全文；正文对应R2A条目；保原caller session/事实 |
| `admin/operations/identity.py` | 535 | T | T | 1-230；transfer/forget/merge+原receipt，余方法结构核 |
| `admin/operations/models.py` | 56 | S | F | 本轮独立全文；正文对应R2A条目；保原caller session/事实 |
| `admin/operations/plugins.py` | 154 | S | F | 本轮独立全文；正文对应R2A条目；保原caller session/事实 |
| `admin/operations/work.py` | 40 | S | F | 本轮独立全文；正文对应R2A条目；保原caller session/事实 |
| `admin/permission_catalog.py` | 781 | N | N | 本轮重新核导入/await/SQL字面量，无自管DB；纯结构或外部协议/已列领域委托 |
| `admin/queries.py` | 442 | S | T | 全部方法await/SQL目标重核；实际读/doctor/Work管理委托，非整文件全文 |
| `admin_api/__init__.py` | 1 | N | N | 本轮重新核导入/await/SQL字面量，无自管DB；纯结构或外部协议/已列领域委托 |
| `admin_api/cli.py` | 342 | N | N | 本轮重新核导入/await/SQL字面量，无自管DB；纯结构或外部协议/已列领域委托 |
| `admin_api/client.py` | 145 | N | F | 本轮独立全文；正文对应R2A条目；保原caller session/事实 |
| `admin_api/http.py` | 640 | S | T | 1-150及全部route调用目标；状态/错误映射消费，余请求schema结构核 |
| `automation/__init__.py` | 6 | N | N | 本轮重新核导入/await/SQL字面量，无自管DB；纯结构或外部协议/已列领域委托 |
| `automation/authority.py` | 61 | N | N | 本轮重新核导入/await/SQL字面量，无自管DB；纯结构或外部协议/已列领域委托 |
| `automation/context.py` | 72 | F | F | 本轮独立全文；正文对应R2A条目；保原caller session/事实 |
| `automation/control_context.py` | 152 | F | S | 本轮重新核全部方法与DB/session/DDL静态边界；未全文SQL/业务分支留R3 |
| `automation/creation_key.py` | 14 | N | N | 本轮重新核导入/await/SQL字面量，无自管DB；纯结构或外部协议/已列领域委托 |
| `automation/gateway.py` | 12 | N | N | 本轮重新核导入/await/SQL字面量，无自管DB；纯结构或外部协议/已列领域委托 |
| `automation/models.py` | 152 | N | N | 本轮重新核导入/await/SQL字面量，无自管DB；纯结构或外部协议/已列领域委托 |
| `automation/repository.py` | 1253 | F | T | 606-910、970-1100；其余create/publish方法AST事务边界核 |
| `automation/service.py` | 768 | S | T | 186-368；全部事务/授权调用结构核 |
| `automation/task_spec.py` | 62 | N | N | 本轮重新核导入/await/SQL字面量，无自管DB；纯结构或外部协议/已列领域委托 |
| `automation/tools.py` | 481 | N | T | 全部await指向service；重新纳入间接写者，非全文工具schema |
| `automation/worker.py` | 334 | S | T | 113-334；Work/claim/真实模型/结算边界 |
| `capabilities/__init__.py` | 65 | N | N | 本轮重新核导入/await/SQL字面量，无自管DB；纯结构或外部协议/已列领域委托 |
| `capabilities/binding.py` | 43 | N | N | 本轮重新核导入/await/SQL字面量，无自管DB；纯结构或外部协议/已列领域委托 |
| `capabilities/catalog.py` | 124 | N | N | 本轮重新核导入/await/SQL字面量，无自管DB；纯结构或外部协议/已列领域委托 |
| `capabilities/coordinator.py` | 235 | F | S | 本轮重新核全部方法与DB/session/DDL静态边界；未全文SQL/业务分支留R3 |
| `capabilities/exposure.py` | 5 | N | N | 本轮重新核导入/await/SQL字面量，无自管DB；纯结构或外部协议/已列领域委托 |
| `capabilities/invocation.py` | 173 | N | N | 本轮重新核导入/await/SQL字面量，无自管DB；纯结构或外部协议/已列领域委托 |
| `capabilities/media.py` | 30 | N | N | 本轮重新核导入/await/SQL字面量，无自管DB；纯结构或外部协议/已列领域委托 |
| `capabilities/models.py` | 143 | N | N | 本轮重新核导入/await/SQL字面量，无自管DB；纯结构或外部协议/已列领域委托 |
| `capabilities/policy.py` | 62 | N | N | 本轮重新核导入/await/SQL字面量，无自管DB；纯结构或外部协议/已列领域委托 |
| `capabilities/provider.py` | 327 | N | N | 本轮重新核导入/await/SQL字面量，无自管DB；纯结构或外部协议/已列领域委托 |
| `capabilities/results.py` | 694 | N | T | ToolResultBudgeter.render持久化分支；余纯projection结构核 |
| `capabilities/runtime.py` | 119 | N | N | 本轮重新核导入/await/SQL字面量，无自管DB；纯结构或外部协议/已列领域委托 |
| `capabilities/validation.py` | 151 | N | N | 本轮重新核导入/await/SQL字面量，无自管DB；纯结构或外部协议/已列领域委托 |
| `character/__init__.py` | 1 | N | N | 本轮重新核导入/await/SQL字面量，无自管DB；纯结构或外部协议/已列领域委托 |
| `character/db_models.py` | 38 | S | S | 本轮重新核全部方法与DB/session/DDL静态边界；未全文SQL/业务分支留R3 |
| `character/repository.py` | 265 | F | S | 本轮重新核全部方法与DB/session/DDL静态边界；未全文SQL/业务分支留R3 |
| `character/soul.py` | 354 | F | S | 本轮重新核全部方法与DB/session/DDL静态边界；未全文SQL/业务分支留R3 |
| `client_api/__init__.py` | 1 | N | N | 本轮重新核导入/await/SQL字面量，无自管DB；纯结构或外部协议/已列领域委托 |
| `client_api/db_models.py` | 40 | S | S | 本轮重新核全部方法与DB/session/DDL静态边界；未全文SQL/业务分支留R3 |
| `client_api/errors.py` | 10 | N | N | 本轮重新核导入/await/SQL字面量，无自管DB；纯结构或外部协议/已列领域委托 |
| `client_api/files.py` | 143 | S | T | 全部方法await/来源+artifact reads目标重核；非全文 |
| `client_api/http.py` | 324 | S | T | 全部route调用目标和异常入口核；非全文请求模型 |
| `client_api/http_errors.py` | 67 | F | F | 本轮独立全文；正文对应R2A条目；保原caller session/事实 |
| `client_api/identity.py` | 735 | F | S | 本轮重新核全部方法与DB/session/DDL静态边界；未全文SQL/业务分支留R3 |
| `client_api/messages.py` | 145 | S | F | 本轮独立全文；正文对应R2A条目；保原caller session/事实 |
| `client_api/models.py` | 111 | N | N | 本轮重新核导入/await/SQL字面量，无自管DB；纯结构或外部协议/已列领域委托 |
| `client_api/openapi.py` | 30 | N | N | 本轮重新核导入/await/SQL字面量，无自管DB；纯结构或外部协议/已列领域委托 |
| `client_api/prompts.py` | 295 | F | S | 本轮重新核全部方法与DB/session/DDL静态边界；未全文SQL/业务分支留R3 |
| `client_api/requests.py` | 265 | S | F | 本轮独立全文；正文对应R2A条目；保原caller session/事实 |
| `client_api/sessions.py` | 115 | F | S | 本轮重新核全部方法与DB/session/DDL静态边界；未全文SQL/业务分支留R3 |
| `client_api/version.py` | 3 | N | N | 本轮重新核导入/await/SQL字面量，无自管DB；纯结构或外部协议/已列领域委托 |
| `client_api/views.py` | 331 | S | T | 288-331 SSE全文；其余sync/activity SQL和helper调用结构核 |
| `client_api/works.py` | 575 | T | S | 本轮重新核全部方法与DB/session/DDL静态边界；未全文SQL/业务分支留R3 |
| `client_sdk/__init__.py` | 6 | N | N | 本轮重新核导入/await/SQL字面量，无自管DB；纯结构或外部协议/已列领域委托 |
| `client_sdk/cli.py` | 196 | S | S | 本轮重新核全部方法与DB/session/DDL静态边界；未全文SQL/业务分支留R3 |
| `client_sdk/client.py` | 435 | N | T | 72-118、283-406；Outbox+HTTP错误恢复 |
| `client_sdk/outbox.py` | 122 | F | F | 本轮独立全文；正文对应R2A条目；保原caller session/事实 |
| `conversation/__init__.py` | 1 | N | N | 本轮重新核导入/await/SQL字面量，无自管DB；纯结构或外部协议/已列领域委托 |
| `conversation/autonomy_binding.py` | 162 | N | N | 本轮重新核导入/await/SQL字面量，无自管DB；纯结构或外部协议/已列领域委托 |
| `conversation/autonomy_db_models.py` | 110 | S | S | 本轮重新核全部方法与DB/session/DDL静态边界；未全文SQL/业务分支留R3 |
| `conversation/autonomy_repository.py` | 584 | F | S | 本轮重新核全部方法与DB/session/DDL静态边界；未全文SQL/业务分支留R3 |
| `conversation/canonical_db_models.py` | 722 | S | S | 本轮重新核全部方法与DB/session/DDL静态边界；未全文SQL/业务分支留R3 |
| `conversation/canonical_schema.py` | 176 | N | N | 本轮重新核导入/await/SQL字面量，无自管DB；纯结构或外部协议/已列领域委托 |
| `conversation/correlation.py` | 115 | S | F | 本轮独立全文；正文对应R2A条目；保原caller session/事实 |
| `conversation/event_entry.py` | 29 | S | F | 本轮独立全文；正文对应R2A条目；保原caller session/事实 |
| `conversation/features.py` | 131 | S | F | 本轮独立全文；正文对应R2A条目；保原caller session/事实 |
| `conversation/frozen_fragments.py` | 359 | N | N | 本轮重新核导入/await/SQL字面量，无自管DB；纯结构或外部协议/已列领域委托 |
| `conversation/hydrate.py` | 434 | F | S | 本轮重新核全部方法与DB/session/DDL静态边界；未全文SQL/业务分支留R3 |
| `conversation/initiative_sources.py` | 58 | S | F | 本轮独立全文；正文对应R2A条目；保原caller session/事实 |
| `conversation/media_index.py` | 135 | F | S | 本轮重新核全部方法与DB/session/DDL静态边界；未全文SQL/业务分支留R3 |
| `conversation/media_service.py` | 1186 | T | T | 988-1176；expire/GC exact候选、media锁、writer/文件隔离；余结构核 |
| `conversation/observation_models.py` | 75 | S | S | 本轮重新核全部方法与DB/session/DDL静态边界；未全文SQL/业务分支留R3 |
| `conversation/observation_schema.py` | 131 | S | S | 本轮重新核全部方法与DB/session/DDL静态边界；未全文SQL/业务分支留R3 |
| `conversation/observations.py` | 134 | F | S | 本轮重新核全部方法与DB/session/DDL静态边界；未全文SQL/业务分支留R3 |
| `conversation/ordinary_admission.py` | 490 | F | S | 本轮重新核全部方法与DB/session/DDL静态边界；未全文SQL/业务分支留R3 |
| `conversation/ordinary_admission_db_models.py` | 36 | S | S | 本轮重新核全部方法与DB/session/DDL静态边界；未全文SQL/业务分支留R3 |
| `conversation/participation.py` | 318 | N | N | 本轮重新核导入/await/SQL字面量，无自管DB；纯结构或外部协议/已列领域委托 |
| `conversation/projection_chunks_schema.py` | 80 | S | S | 本轮重新核全部方法与DB/session/DDL静态边界；未全文SQL/业务分支留R3 |
| `conversation/projection_compact_schema.py` | 209 | N | N | 本轮重新核导入/await/SQL字面量，无自管DB；纯结构或外部协议/已列领域委托 |
| `conversation/projection_current_schema.py` | 268 | N | N | 本轮重新核导入/await/SQL字面量，无自管DB；纯结构或外部协议/已列领域委托 |
| `conversation/projection_history_schema.py` | 220 | N | N | 本轮重新核导入/await/SQL字面量，无自管DB；纯结构或外部协议/已列领域委托 |
| `conversation/projection_material_schema.py` | 268 | N | N | 本轮重新核导入/await/SQL字面量，无自管DB；纯结构或外部协议/已列领域委托 |
| `conversation/projection_models.py` | 82 | S | S | 本轮重新核全部方法与DB/session/DDL静态边界；未全文SQL/业务分支留R3 |
| `conversation/projection_revision_schema.py` | 85 | N | N | 本轮重新核导入/await/SQL字面量，无自管DB；纯结构或外部协议/已列领域委托 |
| `conversation/projection_schema.py` | 58 | S | S | 本轮重新核全部方法与DB/session/DDL静态边界；未全文SQL/业务分支留R3 |
| `conversation/projections.py` | 650 | T | T | 244-418；prepare/publish依赖；余方法事务结构核 |
| `conversation/prompt_history.py` | 317 | S | S | 本轮重新核全部方法与DB/session/DDL静态边界；未全文SQL/业务分支留R3 |
| `conversation/scope.py` | 118 | S | S | 本轮重新核全部方法与DB/session/DDL静态边界；未全文SQL/业务分支留R3 |
| `conversation/scope_models.py` | 50 | S | S | 本轮重新核全部方法与DB/session/DDL静态边界；未全文SQL/业务分支留R3 |
| `conversation/scope_repository.py` | 300 | F | S | 本轮重新核全部方法与DB/session/DDL静态边界；未全文SQL/业务分支留R3 |
| `conversation/self_initiative.py` | 51 | F | S | 本轮重新核全部方法与DB/session/DDL静态边界；未全文SQL/业务分支留R3 |
| `conversation/uploads.py` | 149 | F | T | 105-末；media锁/rename/receipt writer/收尾 |
| `deployment_setup/__init__.py` | 5 | N | N | 本轮重新核导入/await/SQL字面量，无自管DB；纯结构或外部协议/已列领域委托 |
| `deployment_setup/command.py` | 934 | S | S | 本轮重新核全部方法与DB/session/DDL静态边界；未全文SQL/业务分支留R3 |
| `deployment_setup/environment_check.py` | 40 | N | N | 本轮重新核导入/await/SQL字面量，无自管DB；纯结构或外部协议/已列领域委托 |
| `deployment_setup/service.py` | 970 | T | S | 本轮重新核全部方法与DB/session/DDL静态边界；未全文SQL/业务分支留R3 |
| `deployment_setup/terminal.py` | 168 | N | N | 本轮重新核导入/await/SQL字面量，无自管DB；纯结构或外部协议/已列领域委托 |
| `emoji/__init__.py` | 1 | N | N | 本轮重新核导入/await/SQL字面量，无自管DB；纯结构或外部协议/已列领域委托 |
| `emoji/delivery.py` | 355 | F | S | 本轮重新核全部方法与DB/session/DDL静态边界；未全文SQL/业务分支留R3 |
| `emoji/library.py` | 342 | F | S | 本轮重新核全部方法与DB/session/DDL静态边界；未全文SQL/业务分支留R3 |
| `emoji/media.py` | 151 | N | N | 本轮重新核导入/await/SQL字面量，无自管DB；纯结构或外部协议/已列领域委托 |
| `emoji/models.py` | 27 | N | N | 本轮重新核导入/await/SQL字面量，无自管DB；纯结构或外部协议/已列领域委托 |
| `emoji/sticker_index.py` | 543 | F | S | 本轮重新核全部方法与DB/session/DDL静态边界；未全文SQL/业务分支留R3 |
| `gateway/__init__.py` | 23 | N | N | 本轮重新核导入/await/SQL字面量，无自管DB；纯结构或外部协议/已列领域委托 |
| `gateway/activity.py` | 11 | N | N | 本轮重新核导入/await/SQL字面量，无自管DB；纯结构或外部协议/已列领域委托 |
| `gateway/compatibility.py` | 66 | N | N | 本轮重新核导入/await/SQL字面量，无自管DB；纯结构或外部协议/已列领域委托 |
| `gateway/local_clients.py` | 7 | N | N | 本轮重新核导入/await/SQL字面量，无自管DB；纯结构或外部协议/已列领域委托 |
| `gateway/media.py` | 30 | N | N | 本轮重新核导入/await/SQL字面量，无自管DB；纯结构或外部协议/已列领域委托 |
| `gateway/models.py` | 44 | N | N | 本轮重新核导入/await/SQL字面量，无自管DB；纯结构或外部协议/已列领域委托 |
| `gateway/provider.py` | 145 | N | N | 本轮重新核导入/await/SQL字面量，无自管DB；纯结构或外部协议/已列领域委托 |
| `gateway/providers/__init__.py` | 36 | N | N | 本轮重新核导入/await/SQL字面量，无自管DB；纯结构或外部协议/已列领域委托 |
| `gateway/providers/app/__init__.py` | 45 | N | N | 本轮重新核导入/await/SQL字面量，无自管DB；纯结构或外部协议/已列领域委托 |
| `gateway/providers/app/ingress.py` | 262 | S | T | 119-262；read来源、Processor、on_finished取消收拢 |
| `gateway/providers/app/social.py` | 27 | N | N | 本轮重新核导入/await/SQL字面量，无自管DB；纯结构或外部协议/已列领域委托 |
| `gateway/providers/feishu.py` | 279 | N | N | 本轮重新核导入/await/SQL字面量，无自管DB；纯结构或外部协议/已列领域委托 |
| `gateway/providers/feishu_ingress.py` | 406 | S | T | 300-406；start/receive全部真实DB与network边界 |
| `gateway/providers/feishu_protocol.py` | 55 | N | N | 本轮重新核导入/await/SQL字面量，无自管DB；纯结构或外部协议/已列领域委托 |
| `gateway/providers/feishu_social.py` | 198 | N | N | 本轮重新核导入/await/SQL字面量，无自管DB；纯结构或外部协议/已列领域委托 |
| `gateway/providers/feishu_ws.py` | 231 | N | N | 本轮重新核导入/await/SQL字面量，无自管DB；纯结构或外部协议/已列领域委托 |
| `gateway/providers/snowluma.py` | 63 | N | N | 本轮重新核导入/await/SQL字面量，无自管DB；纯结构或外部协议/已列领域委托 |
| `gateway/providers/telegram.py` | 183 | N | N | 本轮重新核导入/await/SQL字面量，无自管DB；纯结构或外部协议/已列领域委托 |
| `gateway/providers/telegram_activity.py` | 28 | N | N | 本轮重新核导入/await/SQL字面量，无自管DB；纯结构或外部协议/已列领域委托 |
| `gateway/providers/telegram_ingress.py` | 505 | S | T | 338-505；start/receive全部真实DB与network边界 |
| `gateway/providers/telegram_social.py` | 126 | N | N | 本轮重新核导入/await/SQL字面量，无自管DB；纯结构或外部协议/已列领域委托 |
| `gateway/registry.py` | 449 | N | N | 本轮重新核导入/await/SQL字面量，无自管DB；纯结构或外部协议/已列领域委托 |
| `gateway/social.py` | 126 | N | N | 本轮重新核导入/await/SQL字面量，无自管DB；纯结构或外部协议/已列领域委托 |
| `identity/__init__.py` | 1 | N | N | 本轮重新核导入/await/SQL字面量，无自管DB；纯结构或外部协议/已列领域委托 |
| `identity/authority.py` | 26 | S | S | 本轮重新核全部方法与DB/session/DDL静态边界；未全文SQL/业务分支留R3 |
| `identity/bootstrap.py` | 28 | F | S | 本轮重新核全部方法与DB/session/DDL静态边界；未全文SQL/业务分支留R3 |
| `identity/canonical_repository.py` | 395 | F | S | 本轮重新核全部方法与DB/session/DDL静态边界；未全文SQL/业务分支留R3 |
| `identity/canonical_uow.py` | 663 | F | T | supplied session分词入口和全部调用方本轮枚举；其余方法结构核 |
| `identity/db_models.py` | 347 | S | S | 本轮重新核全部方法与DB/session/DDL静态边界；未全文SQL/业务分支留R3 |
| `identity/errors.py` | 11 | N | N | 本轮重新核导入/await/SQL字面量，无自管DB；纯结构或外部协议/已列领域委托 |
| `identity/event_author.py` | 117 | S | F | 本轮独立全文；正文对应R2A条目；保原caller session/事实 |
| `identity/evidence.py` | 31 | S | F | 本轮独立全文；正文对应R2A条目；保原caller session/事实 |
| `identity/ingress.py` | 693 | F | S | 本轮重新核全部方法与DB/session/DDL静态边界；未全文SQL/业务分支留R3 |
| `identity/memory_guard.py` | 45 | F | S | 本轮重新核全部方法与DB/session/DDL静态边界；未全文SQL/业务分支留R3 |
| `identity/mentions.py` | 131 | S | F | 本轮独立全文；正文对应R2A条目；保原caller session/事实 |
| `identity/ownership.py` | 56 | N | N | 本轮重新核导入/await/SQL字面量，无自管DB；纯结构或外部协议/已列领域委托 |
| `identity/ownership_interval.py` | 139 | S | F | 本轮独立全文；正文对应R2A条目；保原caller session/事实 |
| `identity/person_identity.py` | 355 | F | S | 本轮重新核全部方法与DB/session/DDL静态边界；未全文SQL/业务分支留R3 |
| `identity/person_state.py` | 732 | T | T | prepare_message_account_observations及prepare_canonical_person_observation闭包；后续查询结构核 |
| `identity/readers.py` | 43 | F | S | 本轮重新核全部方法与DB/session/DDL静态边界；未全文SQL/业务分支留R3 |
| `identity/receipt_compat.py` | 204 | S | F | 本轮独立全文；正文对应R2A条目；保原caller session/事实 |
| `identity/routing.py` | 1536 | T | T | 各session/IMMEDIATE与对应await结构核；未全文所有route predicate |
| `identity/space_metadata.py` | 57 | F | S | 本轮重新核全部方法与DB/session/DDL静态边界；未全文SQL/业务分支留R3 |
| `identity/sql_constraints.py` | 23 | S | F | 本轮独立全文；正文对应R2A条目；保原caller session/事实 |
| `identity/write_settings.py` | 51 | F | S | 本轮重新核全部方法与DB/session/DDL静态边界；未全文SQL/业务分支留R3 |
| `persistence/__init__.py` | 1 | N | N | 本轮重新核导入/await/SQL字面量，无自管DB；纯结构或外部协议/已列领域委托 |
| `persistence/artifact_refs.py` | 29 | F | S | 本轮重新核全部方法与DB/session/DDL静态边界；未全文SQL/业务分支留R3 |
| `persistence/command_receipts.py` | 827 | F | S | 本轮重新核全部方法与DB/session/DDL静态边界；未全文SQL/业务分支留R3 |
| `persistence/commit_hints.py` | 39 | F | F | 本轮独立全文；正文对应R2A条目；保原caller session/事实 |
| `persistence/database.py` | 245 | F | F | 本轮独立全文；正文对应R2A条目；保原caller session/事实 |
| `persistence/diagnostic_writer.py` | 227 | F | S | 本轮重新核全部方法与DB/session/DDL静态边界；未全文SQL/业务分支留R3 |
| `persistence/event_repository.py` | 986 | S | T | 全方法只读/写委托、976 transcript转发；大型query正文未全文 |
| `persistence/event_view.py` | 66 | S | S | 本轮重新核全部方法与DB/session/DDL静态边界；未全文SQL/业务分支留R3 |
| `persistence/history_search.py` | 174 | N | N | 本轮重新核导入/await/SQL字面量，无自管DB；纯结构或外部协议/已列领域委托 |
| `persistence/history_search_index.py` | 97 | F | S | 本轮重新核全部方法与DB/session/DDL静态边界；未全文SQL/业务分支留R3 |
| `persistence/instance_lock.py` | 94 | F | S | 本轮重新核全部方法与DB/session/DDL静态边界；未全文SQL/业务分支留R3 |
| `persistence/job_claims.py` | 62 | F | S | 本轮重新核全部方法与DB/session/DDL静态边界；未全文SQL/业务分支留R3 |
| `persistence/media_repository.py` | 243 | F | F | 本轮独立全文；正文对应R2A条目；保原caller session/事实 |
| `persistence/metadata.py` | 37 | F | S | 本轮重新核全部方法与DB/session/DDL静态边界；未全文SQL/业务分支留R3 |
| `persistence/models.py` | 1844 | S | S | 本轮重新核全部方法与DB/session/DDL静态边界；未全文SQL/业务分支留R3 |
| `persistence/people_repository.py` | 1736 | T | T | 全部owned begin/optional session和forget准备入口结构核，未全文大集合SQL |
| `persistence/repositories.py` | 33 | S | S | 本轮重新核全部方法与DB/session/DDL静态边界；未全文SQL/业务分支留R3 |
| `persistence/repository_helpers.py` | 142 | F | S | 本轮重新核全部方法与DB/session/DDL静态边界；未全文SQL/业务分支留R3 |
| `persistence/repository_records.py` | 183 | N | N | 本轮重新核导入/await/SQL字面量，无自管DB；纯结构或外部协议/已列领域委托 |
| `persistence/schema_guard.py` | 337 | S | F | 本轮独立全文；正文对应R2A条目；保原caller session/事实 |
| `persistence/scoped_event_uow.py` | 1074 | F | T | 361-480派生正文/FTS；supplied-session调用与准备参数重核 |
| `persistence/sqlite_diagnostics.py` | 349 | S | F | 本轮独立全文；正文对应R2A条目；保原caller session/事实 |
| `persistence/unit_of_work.py` | 59 | F | F | 本轮独立全文；正文对应R2A条目；保原caller session/事实 |
| `persistence/web_repository.py` | 50 | F | F | 本轮独立全文；正文对应R2A条目；保原caller session/事实 |
| `persistence/worker_recovery.py` | 24 | F | S | 本轮重新核全部方法与DB/session/DDL静态边界；未全文SQL/业务分支留R3 |
| `web/__init__.py` | 19 | N | N | 本轮重新核导入/await/SQL字面量，无自管DB；纯结构或外部协议/已列领域委托 |
| `web/base.py` | 95 | N | N | 本轮重新核导入/await/SQL字面量，无自管DB；纯结构或外部协议/已列领域委托 |
| `web/bridge_state.py` | 63 | F | F | 本轮独立全文；正文对应R2A条目；保原caller session/事实 |
| `web/http_transport.py` | 304 | N | N | 本轮重新核导入/await/SQL字面量，无自管DB；纯结构或外部协议/已列领域委托 |
| `web/models.py` | 64 | N | N | 本轮重新核导入/await/SQL字面量，无自管DB；纯结构或外部协议/已列领域委托 |
| `web/native_bridge.py` | 337 | S | T | 77-98及252-311调用/usage边界；provider请求不持SQLite，余结构核 |
| `web/native_sources.py` | 89 | N | N | 本轮重新核导入/await/SQL字面量，无自管DB；纯结构或外部协议/已列领域委托 |
| `web/page_extractor.py` | 117 | N | N | 本轮重新核导入/await/SQL字面量，无自管DB；纯结构或外部协议/已列领域委托 |
| `web/route_context.py` | 22 | N | N | 本轮重新核导入/await/SQL字面量，无自管DB；纯结构或外部协议/已列领域委托 |
| `web/search_config.py` | 54 | N | N | 本轮重新核导入/await/SQL字面量，无自管DB；纯结构或外部协议/已列领域委托 |
| `web/tavily.py` | 270 | N | N | 本轮重新核导入/await/SQL字面量，无自管DB；纯结构或外部协议/已列领域委托 |
| `workspace/__init__.py` | 1 | N | N | 本轮重新核导入/await/SQL字面量，无自管DB；纯结构或外部协议/已列领域委托 |
| `workspace/files.py` | 170 | N | N | 本轮重新核导入/await/SQL字面量，无自管DB；纯结构或外部协议/已列领域委托 |
| `workspace/inspect.py` | 139 | N | N | 本轮重新核导入/await/SQL字面量，无自管DB；纯结构或外部协议/已列领域委托 |
| `workspace/service.py` | 244 | F | S | 本轮重新核全部方法与DB/session/DDL静态边界；未全文SQL/业务分支留R3 |
| `workspace/short_state.py` | 109 | F | S | 本轮重新核全部方法与DB/session/DDL静态边界；未全文SQL/业务分支留R3 |
| `workspace/store.py` | 284 | F | S | 本轮重新核全部方法与DB/session/DDL静态边界；未全文SQL/业务分支留R3 |
| `workspace/tools.py` | 65 | N | N | 本轮重新核导入/await/SQL字面量，无自管DB；纯结构或外部协议/已列领域委托 |

本轮A域清单共239文件；覆盖等级：F=32, N=115, S=64, T=28。
