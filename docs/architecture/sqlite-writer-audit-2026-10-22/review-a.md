# SQLite 全局审查 A 域，第 1 轮

日期：2026-10-22。基线：`cdb49eadc0c7cd16060bf0329234ae69611e62bd`。
工作区：`codex/sqlite-writer-architecture-plan-20261022`。只读源码，未修改运行代码、执行模型、访问生产或发送消息。

本轮覆盖 A 域 239 个源码文件的完整结构清单，重点逐函数审查数据库入口和间接调用。
清单区分全文、指定范围和结构分类；结构候选不等于完成语义全文审查。
已读根 AGENTS、development-contract、架构索引、Canonical 和个人 API 合同及本轮统一写入任务书。
现有延迟由用户确认可以接受；本轮首先完整替换写入架构，保留性能观测与真实回退定位，不以新增速度目标扩大范围。

## 1. 本轮结论与任务书修订

### A01 收起公开 session factory 可以明确边界，但不能机械替换

`persistence/database.py:56-62` 的公开 `sessions` 目前同时承担纯读、快照、短写、长准备后写。
只给 `immediate_session()` 加准入，保留调用者随手 `sessions()+begin()`，不能形成清楚的最终接口。
建议最终使用内部 `_sessions`，对外只给 `read_session()` 和 `immediate_session()`；子仓库接收外层同一个 writer session。

`read_session()` 自己拥有的 session 可使用 `autoflush=False`、不自动 commit，退出关闭读快照。
这只是明确使用合同，不需要只读 Session 子类、SQL 字符串拦截或第二套执行框架。
不能把整个 factory 全局设为 `autoflush=False`，也不能在接收 caller session 的只读帮助函数中改其 flush 策略。
`ensure_account → replace_assignment`、Conversation/alias 创建和多个领域组合写仍依赖同事务待写对象与唯一索引顺序。

SQLite 的 SELECT 在当前 Python legacy 事务模式中不等于真正建立数据库读快照。
现有 `EventLedgerRepository.read_scope_context/read_scope_delta/query_history`、`OrdinaryAdmissionRepository._current_rows`、
`PromptProjectionRepository.read/read_checkpoint` 的显式 `BEGIN` 不能随 factory 迁移丢掉。
若 `read_session()` 统一发 `BEGIN`，须记录原来单 SELECT 多出的往返与读快照寿命；不要未核成本就宣称免费。

### A02 名为 pre_admit 的入口确实写库，是本轮热点遗漏

`identity/ingress.py:139-194` 两次自行 `sessions()+session.begin()`；`_admit()` 会调用
`_ensure_person_id()`，再到 `create_person_binding/replace_assignment`，并经 `_hydrate_for_message()`
调用 `ensure_canonical_conversation/ensure_legacy_alias` 创建身份、会话、别名。
因此不能把 pre_admit 全部换成纯读，也不能把整个入站准备函数提前拿 writer。

保留已有回执、已有身份与 hot ingest pin 的只读快路；实际首次身份/会话/alias 建立要有明确短 writer。
冷路由的网络探针继续在所有 DB session 外；writer 重新核原连接、Presence、归属及原本“不存在”的选择条件。
这项上游写入口应与 `canonical_uow.append_inbound` 分开列任务，否则 14 个 snapshot callback 全迁走仍会漏写者。

### A03 两个事件 append 的 supplied-session 路径不能隐藏准备工作

`identity/canonical_uow.py:205-450` 与 `persistence/scoped_event_uow.py:136-205` 都允许 caller 传 session；
`prepared_terms is None` 时仍会 `asyncio.to_thread(prepare_terms, ...)`。
如果 caller 已取得 writer，此 await 就发生在 writer 内；不能因源码把它放在 append 的前半段就当作 writer 外。
应逐个核调用者，外层明确准备分词/JSON/媒体快照，传入 writer 后只做短复核与同事务写入。
`ClientWorks.submit:430-517` 已明确传 prepared_terms，是可保留的正确例子。

Canonical 原子单元仍包含：去重 receipt、chat event、账号/昵称观察、人物别名/群成员、FTS、媒体索引、
Conversation 水位和必要 generation/reset。不能为拆短事务先单独提交 receipt，留下“占号但无事件”。

### A04 现有独立 begin 写入口须逐函数迁移

已确认的例子：

| 文件/函数 | 原入口 | 最终去向 |
| --- | --- | --- |
| `persistence/unit_of_work.py:39 optional_session(write=True)` | owned `sessions()+begin()` | owned 路径进统一 writer；caller session 原样复用 |
| `persistence/job_claims.py:25 commit_job_claims` | `sessions()+begin()` | 统一 writer；保 status/updated_at/live_event 条件写，不重做提取 |
| `persistence/media_repository.py:58 save`、`:123 associate_event` | `sessions()+begin()` | 统一短写；序列化已在外，不新增 Plan |
| `persistence/diagnostic_writer.py:44 DiagnosticWrite.commit` | `sessions()+begin()` | 仅 SQL commit 进公共 writer，原有有界消费和 prepare 在外 |
| `admin/audit.py:154 transaction` | `sessions()+begin()` | 统一入口；业务变更与 audit/receipt 原子组合 |
| `identity/bootstrap.py:12 bootstrap_settings_identity` | `sessions()+begin()` | 初始化的真实写也统一，不把 allow-list 扫描整体变长事务 |
| `conversation/projections.py:588 invalidate_view` | `sessions()+begin()` | 统一短写，提交后才失效进程缓存 |
| `automation/repository.py:710 release_claim` | `sessions()+begin()` | 统一短写，保原 worker ID 条件 |
| `persistence/people_repository.py set_enabled`、`GroupSettingsRepository.set_enabled/set_autonomous_enabled` | owned `sessions()+begin()` | writer owner 外提，caller session 原样组合 |

`begin_nested()` 是现有原子操作内的 savepoint，不能误判为第二个 writer 并删除。
例如 Canonical receipt 争用、Conversation/Space 创建与管理命令可缓存失败，均利用 savepoint 保留真实唯一性/失败回执语义。

### A05 锁顺序不只有 protocol lock

| 现有外层资源 | 当前顺序 | 本轮要求 |
| --- | --- | --- |
| ConversationMediaService._lock | media lock → 短 read/文件发布 → writer；`_expire_page` 同为 media lock → writer | 不引入 writer → media lock 的路径；下载、hash、复制仍在 writer 外 |
| ConversationUploads.upload | 流式上传在外；media lock → 原子 rename → writer 登记 receipt | 不把文件内容写入公共 writer；未知 commit 后不以无本地回执为由删可能已引用文件 |
| CharacterSoul.lock | SOUL lock → reserve writer → 文件发布/热装配 → finish writer | 不能把 reserve、文件 I/O 和 finish 合并成一个 DB writer；保 running/unknown 原件 |
| RuntimeConfigService._mutation_session | caller writer 原样复用，或自行 immediate | 源码已明确禁加与 SQLite writer 反序的进程锁；收口不再新增 config mutex |
| WorkspaceStore 独立库 | manifest BEGIN IMMEDIATE → rename/短 stat → 插入引用 | 独立域，见 A10；不与 Bot writer 建双向等待 |
| StickerIndex | `.search.lock` flock → 准备 JSON 资料 → cache connection/DML | 跨进程环境自己的锁，不能用 Bot asyncio.Lock 替换 |

媒体 GC 在 media lock 下读取准确候选、核引用再删除；其查库不是业务 writer，不能因为看见锁就放进公共队列。

### A06 管理命令的两种事务形态必须保留

`admin/operations/base.py:154 database_command`：授权、领域变更、审计、原请求 receipt 同事务；
领域变更使用 savepoint，某些可缓存失败回滚变更后仍写真实失败 receipt。

`:232 external_command`：先 reserve running，再退出 DB 执行 effect，再短事务结算；失败后查已提交成功，
否则沿原 receipt 标 unknown。不能为了“只有一个事务入口”把外部 effect 移进 writer，也不能重试整个 effect。
`persistence/command_receipts.py` 的状态、audit 关联、原 request/principal/hash 是业务事实，不按防御性包装清空。

人物遗忘、人物合并、改名/账号阶段变更是不同原子操作。`merge_persons` 的准备涉及当前 root、族成员、
RuntimeConfig/PluginConfig、目标已存在对象、别名和路由；拆两阶段时必须核完整集合和 absence，
不能只检查 source/target Person.revision 或旧 row ID。配置搬迁审计和原历史 Person ID 仍保留。

### A07 App/ACP 的复合提交不能拆散

`client_api/works.py:377-524 submit`：App work_input event、原 Work mailbox、resume 接纳与 personal receipt
在同一 writer；外层 `while True` 用于 waiting 状态变动后重新取得原 Work lease，不是可一并删除的 BUSY 重试。
内层 canonical append、WorkRepository.enqueue、record_personal_command 必须复用 caller session。

`client_api/prompts.py:219 cancel` 先 join 原 request，再短 writer 撤权原树，退出后取消模型任务并等原效果/activation 收拢。
不能在 writer 内 join 这些任务；它们可能自己等待 writer 结算。
`ClientSessions.create` 的 ACP thread/Conversation 与原请求 receipt 同事务。
所有请求仍沿原 App/ACP scope、UUID、payload_hash，不借迁移改为当前 root 或平台消息号。

### A08 只读 GET 和 SSE 不应重新变成写者

`ClientIdentityService.authenticate/require_device/me` 读取真实撤销、Person 和 Binding，没有 last_used_at 写入。
`RuntimeConfigService.initialize:546` 只读取 repository.list_all 并建立进程内 `_active_restart`，无首次建表或业务写。
`deployment_setup.load_plugin_setup_states` 也只读取当前安装记录；离线 bootstrap 才写，见 A11。
不能把名字含 initialize、JSON 编码或实例化 Database 当作写入证据。

`commit_hints.py` 仅进程内丢失可恢复的 after_commit 唤醒，不是 durable commit journal。
SSE 订阅后 sync，事实继续 GET；收口要保原 HintSession/hints 生命周期，不把 hint 当提交成功证据。
普通读取应保持并发和独立快照；接收 writer session 的读取可能 autoflush，须按 caller 归属核对。

### A09 Automation 不能只列一个“迁移 worker”任务

逐方法包括 create、set_status、cancel、resume、schedule_now、publish_task、claim_due、release_claim、renew_claim、
create_run、finish_run 与 interrupted/work occurrence settlement。创建/版本/审计、run/owner 计数和下一次计划分别保原子单元。

`claim_due:606` 已在 writer 前分页选 candidate，每页 writer 内取真实 claimed_at，提交后才解析 ORM 的原 JSON；保留。
`renew_claim:738` 用 SQL 当前 julianday 核未过期，不能因公共排队复活过期资格；还要核入参 until 由谁何时计算，
避免排队前算出的延长时间在提交时已经无效。无需要求扩成严格优先级队列。

`publish_task` 当前在 writer 内做 model_dump_json/hash 与版本资料，适合前移纯编码；`finish_run` 的计划解析/下一次时间计算
需要和真实原版本及状态一致，不能跨 snapshot 直接使用旧 live ORM。现有外部 Main/模型仍在事务外。

### A10 A 域至少四类独立 SQLite，不能笼统称“全部走 Bot Database”

| 资源 | 事实/缓存、进程 | 当前入口 | 处置 |
| --- | --- | --- | --- |
| client_sdk Outbox（含 ACP sessions.sqlite3、每 session outbox） | 本机客户端原 UUID/草稿/提交事实；客户端/ACP进程 | 同步 sqlite3.connect；bind/prepare/update 自己 commit | 独立 owner/小事务；不能迁进服务器 Bot writer，不删原恢复内容 |
| `<workspace>/manifest.sqlite3` | 不可变 artifact 原件引用、short_state；Bot/Manager/终端环境 | WorkspaceStore._transaction，写 BEGIN IMMEDIATE，读 BEGIN | 独立文件和跨进程范围；保文件发布/GC与原引用，不能宣称 Bot asyncio 准入覆盖 |
| `<workspace>/stickers/<platform>/search.sqlite3` | 可重建派生缓存；原图/JSON 是事实源；共享环境脚本 | `.search.lock` OS flock、SQLite WAL、短批 DML | 保既有跨进程锁与来源文件；不同 platform 不连 Bot 队列 |
| Web bridge configured cache path | 可丢检索/页面 cache；Bot 进程同步 sqlite3，可能同文件多个 BridgeState 实例 | `_access` 短 IMMEDIATE 清过期/upsert | 单列资源与调用线程，不为可丢缓存增加原请求 journal |

`WorkspaceStore._transaction(write=False)` 在老/空 manifest 会创建/升级 schema；首次“读”不保证零写。
`ShortState.snapshot` 已把到期表现做成只读投影，真实 update 才清旧内容，不要恢复每次读写过期清理。
`BridgeState.access(key)` 首次读取也可建 cache 表。
`StickerIndex.search` 可修坏 cache、发现失效条目后回填，EmojiLibrary.search 在 needs_build 时调用 rebuild；
因此工具名为查询不等于不会改派生库。原图与 JSON 不能按 cache 删除。

Workspace snapshot 大量读/hash/fsync 已在 BEGIN IMMEDIATE 外，但 publish 的 rename/短 lstat 在 manifest fence 内：
它保护 GC 与引用发布。不能照 Bot 的“零文件 I/O”口号机械移出，必须先证明新的文件/引用原子性。
同样，统一主库目标不能偷偷变成跨这些独立库的通用事务框架。

### A11 离线 setup 是单独生命周期，已有在线委托

`deployment_setup/service.py:592 apply_pending_plugins` 先拿 SQLiteApplicationLock；在线 owner 已存在时走 admin API，
否则在原 lock 生命周期内自建 Database，执行 `_bootstrap_pending_plugins` 后关闭。
`load_plugin_setup_states` 自建临时 Database 只读；不要为只读向导增加停机门禁。
这些与主 Bot 的进程内 writer 不在同一 owner；需要在资源表标注，而不是在每个 URL 建全局锁注册表。

### A12 删除应围绕旧调度机制，不破坏业务 CAS/唯一性

可删除/重写：`Database.apply_snapshot_write`、owned begin 分叉、旧持 session closure 的准备/应用耦合、
因为反复抢 SQLite 才存在的重备循环，以及 no consumer 的指标/标记。保留真正读取快照和 caller session 组合能力。

保留：AccountAssignment 历史阶段、原 Person/事件 ID、receipt 唯一索引、route generation、权限、
Memory/隐私/来源版本、外部 effect 未知状态、Work lease 与原 mailbox、协议原检查点。
`recover_database_loop` 是 worker 生命周期容错，可能包括纯准备/结算恢复，不能仅凭名字一刀切删除。

### A13 统一异常边界要覆盖个人 HTTP 的恢复判断

`client_api/http_errors.py:48 database_error` 当前通过原生 BUSY/LOCKED、statement 阶段和
`rollback_failed:` 注记区分 `unavailable` 与 `unknown`。新的公共清理/准入不能吞掉原异常或阶段，
否则客户端可能把未知提交当成确认未提交，沿错误判断重新派发。
这处应列为公共事务修改的直接消费者，并用真实 BEGIN 失败、已写 commit ACK 丢失和 rollback 失败场景验证。
不要求新增通用提交 journal；继续依原请求 UUID 与现有 receipt 查证。

## 2. 测试与补充场景

本轮未运行测试。下面是已查看或定点追踪的测试材料，不写成验证通过。

全文阅读：`test_identity_preparation_boundary.py`（网络探针前物理 checkin）、
`test_workspace_publication_cost.py`（规模复杂度）、`test_short_state_execution_fence.py`（过期来源不得写独立库）、
`test_mutation_snapshot_upgrade.py`（真实 WAL 517、unknown ack 不重放；其中原 retry 结构断言应随新入口重写）。

定点追踪：`test_duplicate_receipt_reads.py`、`test_diagnostic_writer.py`、`test_group_route_recovery.py`、
`test_projection_capacity_cost.py`、`test_privacy_preparation.py`、`test_runtime_config_read_snapshot.py`、
`test_workspace_publish_versions.py`、`test_conversation_media_cleanup.py`；完整复核列入后续轮次。

建议本轮补到验收矩阵的 A 域场景：

1. hot pre_admit/已有 receipt 在另一个 writer 持有时仍纯读；首次人物/Conversation/alias 创建并发不重复、不抢错历史归属。
2. supplied-session canonical/scoped append 不调用 to_thread、文件或外部网络；分词已准备，但来源变化仍正确拒绝/重备。
3. media lock→writer 与 uploads/GC 并发，writer 队列取消时文件事实和已引用原件不被误删；无反向等待。
4. SOUL reserve→文件→finish 的取消/unknown 不重写原文件、不造第二原请求；读取仍能分辨已写与已启用。
5. 管理 database_command 内 mutate 失败：savepoint 回滚真实变更、原失败 audit/receipt 正确；external_command 不重放 effect。
6. App Work input 的 event/mailbox/receipt 原子性、waiting 状态转换时原 lease 和原请求不变。
7. queue 等待期间 Automation lease 过期，不被 renew/create_run 重新授予；必要 settlement 仍可推进。
8. read_session 迁移后只读 GET/SSE/配置 initialize 无 DML；保显式一致读和 caller writer 的 autoflush 语义。
9. workspace/bridge 首读 schema 建立与两个不同进程并发、StickerIndex OS 锁范围，分别核查；不把它们计入主库 FIFO 成功率。
10. 独立 outbox 重启、HTTP ACK 丢失与原 UUID/原 scope 保留；原稿不是可删诊断。

## 3. 性能关注

重点不是另加经验速度门槛，而是避免引入本可删除的开销：

- 已有回执/hot identity、只读 GET 和 snapshot 不应先申请 writer。
- 简单状态更新不额外造 Plan、复制 DTO、每次关闭物理连接。
- 原来在外完成的分词、JSON、文件预处理不能因 supplied session 迁移落回 writer 内。
- 自动化分页领取、媒体候选分页与派生投影的已有复用继续保留。
- 同一到达负载比较总等待，分别报告 writer 准入、事务占用、cleanup、SQL 次数、池等待与吞吐；不把少告警当变快。
- 公开 factory 收口后若 read_session 自动 BEGIN，要把新增读事务往返/长读/WAL保留纳入对照。

## 4. 逐文件覆盖清单

下表从固定基线 `surface.json` 的 A 域生成。`F` 为本轮全文阅读，`T` 为指定数据库函数/调用链阅读，
`S` 为完整 AST/导入/函数结构分类，`N` 为无直接 DB 行为的结构排除（可能调用已另列领域服务）。
S/T 未被写成全文读完；后两轮须独立核排除项与本轮尚未全文的候选，不能用机器命中数冒充三遍语义审查。

本轮覆盖等级：N=119, S=57, F=53, T=10。

| 文件 | 行数 | 覆盖 | 归属/排除依据 |
| --- | ---: | --- | --- |
| `src/yuki/acp_adapter/__init__.py` | 6 | N | AST/导入未见直接数据库入口；格式/协议/配置声明或间接委托，接收领域另核 |
| `src/yuki/acp_adapter/agent.py` | 394 | S | 完整结构候选；数据库读/间接调用及遗漏写入留第二轮独立细核 |
| `src/yuki/acp_adapter/cli.py` | 51 | N | AST/导入未见直接数据库入口；格式/协议/配置声明或间接委托，接收领域另核 |
| `src/yuki/adapters/__init__.py` | 1 | N | AST/导入未见直接数据库入口；格式/协议/配置声明或间接委托，接收领域另核 |
| `src/yuki/adapters/onebot/__init__.py` | 1 | N | AST/导入未见直接数据库入口；格式/协议/配置声明或间接委托，接收领域另核 |
| `src/yuki/adapters/onebot/activity.py` | 21 | N | AST/导入未见直接数据库入口；格式/协议/配置声明或间接委托，接收领域另核 |
| `src/yuki/adapters/onebot/card_parser.py` | 122 | N | AST/导入未见直接数据库入口；格式/协议/配置声明或间接委托，接收领域另核 |
| `src/yuki/adapters/onebot/media.py` | 77 | N | AST/导入未见直接数据库入口；格式/协议/配置声明或间接委托，接收领域另核 |
| `src/yuki/adapters/onebot/message_id.py` | 9 | N | AST/导入未见直接数据库入口；格式/协议/配置声明或间接委托，接收领域另核 |
| `src/yuki/adapters/onebot/normalizer.py` | 472 | N | AST/导入未见直接数据库入口；格式/协议/配置声明或间接委托，接收领域另核 |
| `src/yuki/adapters/onebot/provider_adapter.py` | 152 | N | AST/导入未见直接数据库入口；格式/协议/配置声明或间接委托，接收领域另核 |
| `src/yuki/adapters/onebot/sender.py` | 168 | N | AST/导入未见直接数据库入口；格式/协议/配置声明或间接委托，接收领域另核 |
| `src/yuki/adapters/onebot/social.py` | 132 | N | AST/导入未见直接数据库入口；格式/协议/配置声明或间接委托，接收领域另核 |
| `src/yuki/admin/__init__.py` | 23 | N | AST/导入未见直接数据库入口；格式/协议/配置声明或间接委托，接收领域另核 |
| `src/yuki/admin/audit.py` | 242 | F | 见本轮相应领域函数及原子/锁边界；包含完整实现 |
| `src/yuki/admin/authority/__init__.py` | 1 | N | AST/导入未见直接数据库入口；格式/协议/配置声明或间接委托，接收领域另核 |
| `src/yuki/admin/authority/capabilities.py` | 110 | N | AST/导入未见直接数据库入口；格式/协议/配置声明或间接委托，接收领域另核 |
| `src/yuki/admin/authority/command_types.py` | 341 | N | AST/导入未见直接数据库入口；格式/协议/配置声明或间接委托，接收领域另核 |
| `src/yuki/admin/authority/commands.py` | 85 | N | AST/导入未见直接数据库入口；格式/协议/配置声明或间接委托，接收领域另核 |
| `src/yuki/admin/authority/decision.py` | 114 | N | AST/导入未见直接数据库入口；格式/协议/配置声明或间接委托，接收领域另核 |
| `src/yuki/admin/authority/json_types.py` | 50 | N | AST/导入未见直接数据库入口；格式/协议/配置声明或间接委托，接收领域另核 |
| `src/yuki/admin/authority/operations.py` | 118 | N | AST/导入未见直接数据库入口；格式/协议/配置声明或间接委托，接收领域另核 |
| `src/yuki/admin/authority/operators.py` | 114 | F | 全文读取；读事务/认证/异常/DDL或记录投影语义已分类 |
| `src/yuki/admin/authority/principal.py` | 119 | N | AST/导入未见直接数据库入口；格式/协议/配置声明或间接委托，接收领域另核 |
| `src/yuki/admin/authority/problems.py` | 62 | N | AST/导入未见直接数据库入口；格式/协议/配置声明或间接委托，接收领域另核 |
| `src/yuki/admin/authority/targets.py` | 61 | N | AST/导入未见直接数据库入口；格式/协议/配置声明或间接委托，接收领域另核 |
| `src/yuki/admin/authority/tokens.py` | 38 | N | AST/导入未见直接数据库入口；格式/协议/配置声明或间接委托，接收领域另核 |
| `src/yuki/admin/config_files.py` | 482 | T | 本轮读取写入口/组合调用/锁顺序，未声明整文件全文 |
| `src/yuki/admin/config_owners.py` | 16 | S | 完整结构候选；数据库读/间接调用及遗漏写入留第二轮独立细核 |
| `src/yuki/admin/config_registry.py` | 160 | N | AST/导入未见直接数据库入口；格式/协议/配置声明或间接委托，接收领域另核 |
| `src/yuki/admin/config_service.py` | 1827 | T | 本轮读取写入口/组合调用/锁顺序，未声明整文件全文 |
| `src/yuki/admin/config_spec_helpers.py` | 77 | S | 结构/模型/DDL/配置声明候选；按迁移、触发器与事实约束复核，不当运行时新writer |
| `src/yuki/admin/config_specs_asr.py` | 77 | N | AST/导入未见直接数据库入口；格式/协议/配置声明或间接委托，接收领域另核 |
| `src/yuki/admin/config_specs_future.py` | 45 | N | AST/导入未见直接数据库入口；格式/协议/配置声明或间接委托，接收领域另核 |
| `src/yuki/admin/config_specs_hot.py` | 525 | N | AST/导入未见直接数据库入口；格式/协议/配置声明或间接委托，接收领域另核 |
| `src/yuki/admin/config_specs_planner_plugins.py` | 172 | N | AST/导入未见直接数据库入口；格式/协议/配置声明或间接委托，接收领域另核 |
| `src/yuki/admin/config_specs_protected.py` | 122 | S | 结构/模型/DDL/配置声明候选；按迁移、触发器与事实约束复核，不当运行时新writer |
| `src/yuki/admin/config_specs_restart.py` | 512 | N | AST/导入未见直接数据库入口；格式/协议/配置声明或间接委托，接收领域另核 |
| `src/yuki/admin/config_specs_social.py` | 7 | N | AST/导入未见直接数据库入口；格式/协议/配置声明或间接委托，接收领域另核 |
| `src/yuki/admin/config_specs_tooling.py` | 64 | N | AST/导入未见直接数据库入口；格式/协议/配置声明或间接委托，接收领域另核 |
| `src/yuki/admin/control_resolution.py` | 270 | S | 完整结构候选；数据库读/间接调用及遗漏写入留第二轮独立细核 |
| `src/yuki/admin/models.py` | 296 | N | AST/导入未见直接数据库入口；格式/协议/配置声明或间接委托，接收领域另核 |
| `src/yuki/admin/operations/__init__.py` | 39 | S | 完整结构候选；数据库读/间接调用及遗漏写入留第二轮独立细核 |
| `src/yuki/admin/operations/base.py` | 345 | F | 见本轮相应领域函数及原子/锁边界；包含完整实现 |
| `src/yuki/admin/operations/config.py` | 184 | S | 完整结构候选；数据库读/间接调用及遗漏写入留第二轮独立细核 |
| `src/yuki/admin/operations/groups.py` | 35 | S | 完整结构候选；数据库读/间接调用及遗漏写入留第二轮独立细核 |
| `src/yuki/admin/operations/identity.py` | 535 | T | 本轮读取写入口/组合调用/锁顺序，未声明整文件全文 |
| `src/yuki/admin/operations/models.py` | 56 | S | 结构/模型/DDL/配置声明候选；按迁移、触发器与事实约束复核，不当运行时新writer |
| `src/yuki/admin/operations/plugins.py` | 154 | S | 完整结构候选；数据库读/间接调用及遗漏写入留第二轮独立细核 |
| `src/yuki/admin/operations/work.py` | 40 | S | 完整结构候选；数据库读/间接调用及遗漏写入留第二轮独立细核 |
| `src/yuki/admin/permission_catalog.py` | 781 | N | AST/导入未见直接数据库入口；格式/协议/配置声明或间接委托，接收领域另核 |
| `src/yuki/admin/queries.py` | 442 | S | 完整结构候选；数据库读/间接调用及遗漏写入留第二轮独立细核 |
| `src/yuki/admin_api/__init__.py` | 1 | N | AST/导入未见直接数据库入口；格式/协议/配置声明或间接委托，接收领域另核 |
| `src/yuki/admin_api/cli.py` | 342 | N | AST/导入未见直接数据库入口；格式/协议/配置声明或间接委托，接收领域另核 |
| `src/yuki/admin_api/client.py` | 145 | N | AST/导入未见直接数据库入口；格式/协议/配置声明或间接委托，接收领域另核 |
| `src/yuki/admin_api/http.py` | 640 | S | 完整结构候选；数据库读/间接调用及遗漏写入留第二轮独立细核 |
| `src/yuki/automation/__init__.py` | 6 | N | AST/导入未见直接数据库入口；格式/协议/配置声明或间接委托，接收领域另核 |
| `src/yuki/automation/authority.py` | 61 | N | AST/导入未见直接数据库入口；格式/协议/配置声明或间接委托，接收领域另核 |
| `src/yuki/automation/context.py` | 72 | F | 见本轮相应领域函数及原子/锁边界；包含完整实现 |
| `src/yuki/automation/control_context.py` | 152 | F | 见本轮相应领域函数及原子/锁边界；包含完整实现 |
| `src/yuki/automation/creation_key.py` | 14 | N | AST/导入未见直接数据库入口；格式/协议/配置声明或间接委托，接收领域另核 |
| `src/yuki/automation/gateway.py` | 12 | N | AST/导入未见直接数据库入口；格式/协议/配置声明或间接委托，接收领域另核 |
| `src/yuki/automation/models.py` | 152 | N | AST/导入未见直接数据库入口；格式/协议/配置声明或间接委托，接收领域另核 |
| `src/yuki/automation/repository.py` | 1253 | F | 见本轮相应领域函数及原子/锁边界；包含完整实现 |
| `src/yuki/automation/service.py` | 768 | S | 完整结构候选；数据库读/间接调用及遗漏写入留第二轮独立细核 |
| `src/yuki/automation/task_spec.py` | 62 | N | AST/导入未见直接数据库入口；格式/协议/配置声明或间接委托，接收领域另核 |
| `src/yuki/automation/tools.py` | 481 | N | AST/导入未见直接数据库入口；格式/协议/配置声明或间接委托，接收领域另核 |
| `src/yuki/automation/worker.py` | 334 | S | 完整结构候选；数据库读/间接调用及遗漏写入留第二轮独立细核 |
| `src/yuki/capabilities/__init__.py` | 65 | N | AST/导入未见直接数据库入口；格式/协议/配置声明或间接委托，接收领域另核 |
| `src/yuki/capabilities/binding.py` | 43 | N | AST/导入未见直接数据库入口；格式/协议/配置声明或间接委托，接收领域另核 |
| `src/yuki/capabilities/catalog.py` | 124 | N | AST/导入未见直接数据库入口；格式/协议/配置声明或间接委托，接收领域另核 |
| `src/yuki/capabilities/coordinator.py` | 235 | F | 见本轮相应领域函数及原子/锁边界；包含完整实现 |
| `src/yuki/capabilities/exposure.py` | 5 | N | AST/导入未见直接数据库入口；格式/协议/配置声明或间接委托，接收领域另核 |
| `src/yuki/capabilities/invocation.py` | 173 | N | AST/导入未见直接数据库入口；格式/协议/配置声明或间接委托，接收领域另核 |
| `src/yuki/capabilities/media.py` | 30 | N | AST/导入未见直接数据库入口；格式/协议/配置声明或间接委托，接收领域另核 |
| `src/yuki/capabilities/models.py` | 143 | N | AST/导入未见直接数据库入口；格式/协议/配置声明或间接委托，接收领域另核 |
| `src/yuki/capabilities/policy.py` | 62 | N | AST/导入未见直接数据库入口；格式/协议/配置声明或间接委托，接收领域另核 |
| `src/yuki/capabilities/provider.py` | 327 | N | AST/导入未见直接数据库入口；格式/协议/配置声明或间接委托，接收领域另核 |
| `src/yuki/capabilities/results.py` | 694 | N | AST/导入未见直接数据库入口；格式/协议/配置声明或间接委托，接收领域另核 |
| `src/yuki/capabilities/runtime.py` | 119 | N | AST/导入未见直接数据库入口；格式/协议/配置声明或间接委托，接收领域另核 |
| `src/yuki/capabilities/validation.py` | 151 | N | AST/导入未见直接数据库入口；格式/协议/配置声明或间接委托，接收领域另核 |
| `src/yuki/character/__init__.py` | 1 | N | AST/导入未见直接数据库入口；格式/协议/配置声明或间接委托，接收领域另核 |
| `src/yuki/character/db_models.py` | 38 | S | 结构/模型/DDL/配置声明候选；按迁移、触发器与事实约束复核，不当运行时新writer |
| `src/yuki/character/repository.py` | 265 | F | 见本轮相应领域函数及原子/锁边界；包含完整实现 |
| `src/yuki/character/soul.py` | 354 | F | 见本轮相应领域函数及原子/锁边界；包含完整实现 |
| `src/yuki/client_api/__init__.py` | 1 | N | AST/导入未见直接数据库入口；格式/协议/配置声明或间接委托，接收领域另核 |
| `src/yuki/client_api/db_models.py` | 40 | S | 结构/模型/DDL/配置声明候选；按迁移、触发器与事实约束复核，不当运行时新writer |
| `src/yuki/client_api/errors.py` | 10 | N | AST/导入未见直接数据库入口；格式/协议/配置声明或间接委托，接收领域另核 |
| `src/yuki/client_api/files.py` | 143 | S | 完整结构候选；数据库读/间接调用及遗漏写入留第二轮独立细核 |
| `src/yuki/client_api/http.py` | 324 | S | 完整结构候选；数据库读/间接调用及遗漏写入留第二轮独立细核 |
| `src/yuki/client_api/http_errors.py` | 67 | F | 全文读取；读事务/认证/异常/DDL或记录投影语义已分类 |
| `src/yuki/client_api/identity.py` | 735 | F | 见本轮相应领域函数及原子/锁边界；包含完整实现 |
| `src/yuki/client_api/messages.py` | 145 | S | 完整结构候选；数据库读/间接调用及遗漏写入留第二轮独立细核 |
| `src/yuki/client_api/models.py` | 111 | N | AST/导入未见直接数据库入口；格式/协议/配置声明或间接委托，接收领域另核 |
| `src/yuki/client_api/openapi.py` | 30 | N | AST/导入未见直接数据库入口；格式/协议/配置声明或间接委托，接收领域另核 |
| `src/yuki/client_api/prompts.py` | 295 | F | 见本轮相应领域函数及原子/锁边界；包含完整实现 |
| `src/yuki/client_api/requests.py` | 265 | S | 完整结构候选；数据库读/间接调用及遗漏写入留第二轮独立细核 |
| `src/yuki/client_api/sessions.py` | 115 | F | 见本轮相应领域函数及原子/锁边界；包含完整实现 |
| `src/yuki/client_api/version.py` | 3 | N | AST/导入未见直接数据库入口；格式/协议/配置声明或间接委托，接收领域另核 |
| `src/yuki/client_api/views.py` | 331 | S | 完整结构候选；数据库读/间接调用及遗漏写入留第二轮独立细核 |
| `src/yuki/client_api/works.py` | 575 | T | 本轮读取写入口/组合调用/锁顺序，未声明整文件全文 |
| `src/yuki/client_sdk/__init__.py` | 6 | N | AST/导入未见直接数据库入口；格式/协议/配置声明或间接委托，接收领域另核 |
| `src/yuki/client_sdk/cli.py` | 196 | S | 完整结构候选；数据库读/间接调用及遗漏写入留第二轮独立细核 |
| `src/yuki/client_sdk/client.py` | 435 | N | AST/导入未见直接数据库入口；格式/协议/配置声明或间接委托，接收领域另核 |
| `src/yuki/client_sdk/outbox.py` | 122 | F | 见本轮相应领域函数及原子/锁边界；包含完整实现 |
| `src/yuki/conversation/__init__.py` | 1 | N | AST/导入未见直接数据库入口；格式/协议/配置声明或间接委托，接收领域另核 |
| `src/yuki/conversation/autonomy_binding.py` | 162 | N | AST/导入未见直接数据库入口；格式/协议/配置声明或间接委托，接收领域另核 |
| `src/yuki/conversation/autonomy_db_models.py` | 110 | S | 结构/模型/DDL/配置声明候选；按迁移、触发器与事实约束复核，不当运行时新writer |
| `src/yuki/conversation/autonomy_repository.py` | 584 | F | 见本轮相应领域函数及原子/锁边界；包含完整实现 |
| `src/yuki/conversation/canonical_db_models.py` | 722 | S | 结构/模型/DDL/配置声明候选；按迁移、触发器与事实约束复核，不当运行时新writer |
| `src/yuki/conversation/canonical_schema.py` | 176 | N | AST/导入未见直接数据库入口；格式/协议/配置声明或间接委托，接收领域另核 |
| `src/yuki/conversation/correlation.py` | 115 | S | 完整结构候选；数据库读/间接调用及遗漏写入留第二轮独立细核 |
| `src/yuki/conversation/event_entry.py` | 29 | S | 完整结构候选；数据库读/间接调用及遗漏写入留第二轮独立细核 |
| `src/yuki/conversation/features.py` | 131 | S | 完整结构候选；数据库读/间接调用及遗漏写入留第二轮独立细核 |
| `src/yuki/conversation/frozen_fragments.py` | 359 | N | AST/导入未见直接数据库入口；格式/协议/配置声明或间接委托，接收领域另核 |
| `src/yuki/conversation/hydrate.py` | 434 | F | 见本轮相应领域函数及原子/锁边界；包含完整实现 |
| `src/yuki/conversation/initiative_sources.py` | 58 | S | 完整结构候选；数据库读/间接调用及遗漏写入留第二轮独立细核 |
| `src/yuki/conversation/media_index.py` | 135 | F | 见本轮相应领域函数及原子/锁边界；包含完整实现 |
| `src/yuki/conversation/media_service.py` | 1186 | T | 本轮读取写入口/组合调用/锁顺序，未声明整文件全文 |
| `src/yuki/conversation/observation_models.py` | 75 | S | 结构/模型/DDL/配置声明候选；按迁移、触发器与事实约束复核，不当运行时新writer |
| `src/yuki/conversation/observation_schema.py` | 131 | S | 结构/模型/DDL/配置声明候选；按迁移、触发器与事实约束复核，不当运行时新writer |
| `src/yuki/conversation/observations.py` | 134 | F | 全文读取；读事务/认证/异常/DDL或记录投影语义已分类 |
| `src/yuki/conversation/ordinary_admission.py` | 490 | F | 见本轮相应领域函数及原子/锁边界；包含完整实现 |
| `src/yuki/conversation/ordinary_admission_db_models.py` | 36 | S | 结构/模型/DDL/配置声明候选；按迁移、触发器与事实约束复核，不当运行时新writer |
| `src/yuki/conversation/participation.py` | 318 | N | AST/导入未见直接数据库入口；格式/协议/配置声明或间接委托，接收领域另核 |
| `src/yuki/conversation/projection_chunks_schema.py` | 80 | S | 结构/模型/DDL/配置声明候选；按迁移、触发器与事实约束复核，不当运行时新writer |
| `src/yuki/conversation/projection_compact_schema.py` | 209 | N | AST/导入未见直接数据库入口；格式/协议/配置声明或间接委托，接收领域另核 |
| `src/yuki/conversation/projection_current_schema.py` | 268 | N | AST/导入未见直接数据库入口；格式/协议/配置声明或间接委托，接收领域另核 |
| `src/yuki/conversation/projection_history_schema.py` | 220 | N | AST/导入未见直接数据库入口；格式/协议/配置声明或间接委托，接收领域另核 |
| `src/yuki/conversation/projection_material_schema.py` | 268 | N | AST/导入未见直接数据库入口；格式/协议/配置声明或间接委托，接收领域另核 |
| `src/yuki/conversation/projection_models.py` | 82 | S | 结构/模型/DDL/配置声明候选；按迁移、触发器与事实约束复核，不当运行时新writer |
| `src/yuki/conversation/projection_revision_schema.py` | 85 | N | AST/导入未见直接数据库入口；格式/协议/配置声明或间接委托，接收领域另核 |
| `src/yuki/conversation/projection_schema.py` | 58 | S | 结构/模型/DDL/配置声明候选；按迁移、触发器与事实约束复核，不当运行时新writer |
| `src/yuki/conversation/projections.py` | 650 | T | 本轮读取写入口/组合调用/锁顺序，未声明整文件全文 |
| `src/yuki/conversation/prompt_history.py` | 317 | S | 完整结构候选；数据库读/间接调用及遗漏写入留第二轮独立细核 |
| `src/yuki/conversation/scope.py` | 118 | S | 完整结构候选；数据库读/间接调用及遗漏写入留第二轮独立细核 |
| `src/yuki/conversation/scope_models.py` | 50 | S | 结构/模型/DDL/配置声明候选；按迁移、触发器与事实约束复核，不当运行时新writer |
| `src/yuki/conversation/scope_repository.py` | 300 | F | 全文读取；读事务/认证/异常/DDL或记录投影语义已分类 |
| `src/yuki/conversation/self_initiative.py` | 51 | F | 见本轮相应领域函数及原子/锁边界；包含完整实现 |
| `src/yuki/conversation/uploads.py` | 149 | F | 见本轮相应领域函数及原子/锁边界；包含完整实现 |
| `src/yuki/deployment_setup/__init__.py` | 5 | N | AST/导入未见直接数据库入口；格式/协议/配置声明或间接委托，接收领域另核 |
| `src/yuki/deployment_setup/command.py` | 934 | S | 完整结构候选；数据库读/间接调用及遗漏写入留第二轮独立细核 |
| `src/yuki/deployment_setup/environment_check.py` | 40 | N | AST/导入未见直接数据库入口；格式/协议/配置声明或间接委托，接收领域另核 |
| `src/yuki/deployment_setup/service.py` | 970 | T | 本轮读取写入口/组合调用/锁顺序，未声明整文件全文 |
| `src/yuki/deployment_setup/terminal.py` | 168 | N | AST/导入未见直接数据库入口；格式/协议/配置声明或间接委托，接收领域另核 |
| `src/yuki/emoji/__init__.py` | 1 | N | AST/导入未见直接数据库入口；格式/协议/配置声明或间接委托，接收领域另核 |
| `src/yuki/emoji/delivery.py` | 355 | F | 见本轮相应领域函数及原子/锁边界；包含完整实现 |
| `src/yuki/emoji/library.py` | 342 | F | 见本轮相应领域函数及原子/锁边界；包含完整实现 |
| `src/yuki/emoji/media.py` | 151 | N | AST/导入未见直接数据库入口；格式/协议/配置声明或间接委托，接收领域另核 |
| `src/yuki/emoji/models.py` | 27 | N | AST/导入未见直接数据库入口；格式/协议/配置声明或间接委托，接收领域另核 |
| `src/yuki/emoji/sticker_index.py` | 543 | F | 见本轮相应领域函数及原子/锁边界；包含完整实现 |
| `src/yuki/gateway/__init__.py` | 23 | N | AST/导入未见直接数据库入口；格式/协议/配置声明或间接委托，接收领域另核 |
| `src/yuki/gateway/activity.py` | 11 | N | AST/导入未见直接数据库入口；格式/协议/配置声明或间接委托，接收领域另核 |
| `src/yuki/gateway/compatibility.py` | 66 | N | AST/导入未见直接数据库入口；格式/协议/配置声明或间接委托，接收领域另核 |
| `src/yuki/gateway/local_clients.py` | 7 | N | AST/导入未见直接数据库入口；格式/协议/配置声明或间接委托，接收领域另核 |
| `src/yuki/gateway/media.py` | 30 | N | AST/导入未见直接数据库入口；格式/协议/配置声明或间接委托，接收领域另核 |
| `src/yuki/gateway/models.py` | 44 | N | AST/导入未见直接数据库入口；格式/协议/配置声明或间接委托，接收领域另核 |
| `src/yuki/gateway/provider.py` | 145 | N | AST/导入未见直接数据库入口；格式/协议/配置声明或间接委托，接收领域另核 |
| `src/yuki/gateway/providers/__init__.py` | 36 | N | AST/导入未见直接数据库入口；格式/协议/配置声明或间接委托，接收领域另核 |
| `src/yuki/gateway/providers/app/__init__.py` | 45 | N | AST/导入未见直接数据库入口；格式/协议/配置声明或间接委托，接收领域另核 |
| `src/yuki/gateway/providers/app/ingress.py` | 262 | S | 完整结构候选；数据库读/间接调用及遗漏写入留第二轮独立细核 |
| `src/yuki/gateway/providers/app/social.py` | 27 | N | AST/导入未见直接数据库入口；格式/协议/配置声明或间接委托，接收领域另核 |
| `src/yuki/gateway/providers/feishu.py` | 279 | N | AST/导入未见直接数据库入口；格式/协议/配置声明或间接委托，接收领域另核 |
| `src/yuki/gateway/providers/feishu_ingress.py` | 406 | S | 完整结构候选；数据库读/间接调用及遗漏写入留第二轮独立细核 |
| `src/yuki/gateway/providers/feishu_protocol.py` | 55 | N | AST/导入未见直接数据库入口；格式/协议/配置声明或间接委托，接收领域另核 |
| `src/yuki/gateway/providers/feishu_social.py` | 198 | N | AST/导入未见直接数据库入口；格式/协议/配置声明或间接委托，接收领域另核 |
| `src/yuki/gateway/providers/feishu_ws.py` | 231 | N | AST/导入未见直接数据库入口；格式/协议/配置声明或间接委托，接收领域另核 |
| `src/yuki/gateway/providers/snowluma.py` | 63 | N | AST/导入未见直接数据库入口；格式/协议/配置声明或间接委托，接收领域另核 |
| `src/yuki/gateway/providers/telegram.py` | 183 | N | AST/导入未见直接数据库入口；格式/协议/配置声明或间接委托，接收领域另核 |
| `src/yuki/gateway/providers/telegram_activity.py` | 28 | N | AST/导入未见直接数据库入口；格式/协议/配置声明或间接委托，接收领域另核 |
| `src/yuki/gateway/providers/telegram_ingress.py` | 505 | S | 完整结构候选；数据库读/间接调用及遗漏写入留第二轮独立细核 |
| `src/yuki/gateway/providers/telegram_social.py` | 126 | N | AST/导入未见直接数据库入口；格式/协议/配置声明或间接委托，接收领域另核 |
| `src/yuki/gateway/registry.py` | 449 | N | AST/导入未见直接数据库入口；格式/协议/配置声明或间接委托，接收领域另核 |
| `src/yuki/gateway/social.py` | 126 | N | AST/导入未见直接数据库入口；格式/协议/配置声明或间接委托，接收领域另核 |
| `src/yuki/identity/__init__.py` | 1 | N | AST/导入未见直接数据库入口；格式/协议/配置声明或间接委托，接收领域另核 |
| `src/yuki/identity/authority.py` | 26 | S | 完整结构候选；数据库读/间接调用及遗漏写入留第二轮独立细核 |
| `src/yuki/identity/bootstrap.py` | 28 | F | 见本轮相应领域函数及原子/锁边界；包含完整实现 |
| `src/yuki/identity/canonical_repository.py` | 395 | F | 见本轮相应领域函数及原子/锁边界；包含完整实现 |
| `src/yuki/identity/canonical_uow.py` | 663 | F | 见本轮相应领域函数及原子/锁边界；包含完整实现 |
| `src/yuki/identity/db_models.py` | 347 | S | 结构/模型/DDL/配置声明候选；按迁移、触发器与事实约束复核，不当运行时新writer |
| `src/yuki/identity/errors.py` | 11 | N | AST/导入未见直接数据库入口；格式/协议/配置声明或间接委托，接收领域另核 |
| `src/yuki/identity/event_author.py` | 117 | S | 完整结构候选；数据库读/间接调用及遗漏写入留第二轮独立细核 |
| `src/yuki/identity/evidence.py` | 31 | S | 完整结构候选；数据库读/间接调用及遗漏写入留第二轮独立细核 |
| `src/yuki/identity/ingress.py` | 693 | F | 见本轮相应领域函数及原子/锁边界；包含完整实现 |
| `src/yuki/identity/memory_guard.py` | 45 | F | 见本轮相应领域函数及原子/锁边界；包含完整实现 |
| `src/yuki/identity/mentions.py` | 131 | S | 完整结构候选；数据库读/间接调用及遗漏写入留第二轮独立细核 |
| `src/yuki/identity/ownership.py` | 56 | N | AST/导入未见直接数据库入口；格式/协议/配置声明或间接委托，接收领域另核 |
| `src/yuki/identity/ownership_interval.py` | 139 | S | 完整结构候选；数据库读/间接调用及遗漏写入留第二轮独立细核 |
| `src/yuki/identity/person_identity.py` | 355 | F | 见本轮相应领域函数及原子/锁边界；包含完整实现 |
| `src/yuki/identity/person_state.py` | 732 | T | 本轮读取写入口/组合调用/锁顺序，未声明整文件全文 |
| `src/yuki/identity/readers.py` | 43 | F | 见本轮相应领域函数及原子/锁边界；包含完整实现 |
| `src/yuki/identity/receipt_compat.py` | 204 | S | 完整结构候选；数据库读/间接调用及遗漏写入留第二轮独立细核 |
| `src/yuki/identity/routing.py` | 1536 | T | 本轮读取写入口/组合调用/锁顺序，未声明整文件全文 |
| `src/yuki/identity/space_metadata.py` | 57 | F | 见本轮相应领域函数及原子/锁边界；包含完整实现 |
| `src/yuki/identity/sql_constraints.py` | 23 | S | 完整结构候选；数据库读/间接调用及遗漏写入留第二轮独立细核 |
| `src/yuki/identity/write_settings.py` | 51 | F | 见本轮相应领域函数及原子/锁边界；包含完整实现 |
| `src/yuki/persistence/__init__.py` | 1 | N | AST/导入未见直接数据库入口；格式/协议/配置声明或间接委托，接收领域另核 |
| `src/yuki/persistence/artifact_refs.py` | 29 | F | 全文读取；读事务/认证/异常/DDL或记录投影语义已分类 |
| `src/yuki/persistence/command_receipts.py` | 827 | F | 见本轮相应领域函数及原子/锁边界；包含完整实现 |
| `src/yuki/persistence/commit_hints.py` | 39 | F | 见本轮相应领域函数及原子/锁边界；包含完整实现 |
| `src/yuki/persistence/database.py` | 245 | F | 见本轮相应领域函数及原子/锁边界；包含完整实现 |
| `src/yuki/persistence/diagnostic_writer.py` | 227 | F | 见本轮相应领域函数及原子/锁边界；包含完整实现 |
| `src/yuki/persistence/event_repository.py` | 986 | S | 完整结构候选；数据库读/间接调用及遗漏写入留第二轮独立细核 |
| `src/yuki/persistence/event_view.py` | 66 | S | 完整结构候选；数据库读/间接调用及遗漏写入留第二轮独立细核 |
| `src/yuki/persistence/history_search.py` | 174 | N | AST/导入未见直接数据库入口；格式/协议/配置声明或间接委托，接收领域另核 |
| `src/yuki/persistence/history_search_index.py` | 97 | F | 见本轮相应领域函数及原子/锁边界；包含完整实现 |
| `src/yuki/persistence/instance_lock.py` | 94 | F | 见本轮相应领域函数及原子/锁边界；包含完整实现 |
| `src/yuki/persistence/job_claims.py` | 62 | F | 见本轮相应领域函数及原子/锁边界；包含完整实现 |
| `src/yuki/persistence/media_repository.py` | 243 | F | 见本轮相应领域函数及原子/锁边界；包含完整实现 |
| `src/yuki/persistence/metadata.py` | 37 | F | 全文读取；读事务/认证/异常/DDL或记录投影语义已分类 |
| `src/yuki/persistence/models.py` | 1844 | S | 结构/模型/DDL/配置声明候选；按迁移、触发器与事实约束复核，不当运行时新writer |
| `src/yuki/persistence/people_repository.py` | 1736 | T | 本轮读取写入口/组合调用/锁顺序，未声明整文件全文 |
| `src/yuki/persistence/repositories.py` | 33 | S | 完整结构候选；数据库读/间接调用及遗漏写入留第二轮独立细核 |
| `src/yuki/persistence/repository_helpers.py` | 142 | F | 全文读取；读事务/认证/异常/DDL或记录投影语义已分类 |
| `src/yuki/persistence/repository_records.py` | 183 | N | AST/导入未见直接数据库入口；格式/协议/配置声明或间接委托，接收领域另核 |
| `src/yuki/persistence/schema_guard.py` | 337 | S | 完整结构候选；数据库读/间接调用及遗漏写入留第二轮独立细核 |
| `src/yuki/persistence/scoped_event_uow.py` | 1074 | F | 见本轮相应领域函数及原子/锁边界；包含完整实现 |
| `src/yuki/persistence/sqlite_diagnostics.py` | 349 | S | 完整结构候选；数据库读/间接调用及遗漏写入留第二轮独立细核 |
| `src/yuki/persistence/unit_of_work.py` | 59 | F | 见本轮相应领域函数及原子/锁边界；包含完整实现 |
| `src/yuki/persistence/web_repository.py` | 50 | F | 见本轮相应领域函数及原子/锁边界；包含完整实现 |
| `src/yuki/persistence/worker_recovery.py` | 24 | F | 见本轮相应领域函数及原子/锁边界；包含完整实现 |
| `src/yuki/web/__init__.py` | 19 | N | AST/导入未见直接数据库入口；格式/协议/配置声明或间接委托，接收领域另核 |
| `src/yuki/web/base.py` | 95 | N | AST/导入未见直接数据库入口；格式/协议/配置声明或间接委托，接收领域另核 |
| `src/yuki/web/bridge_state.py` | 63 | F | 见本轮相应领域函数及原子/锁边界；包含完整实现 |
| `src/yuki/web/http_transport.py` | 304 | N | AST/导入未见直接数据库入口；格式/协议/配置声明或间接委托，接收领域另核 |
| `src/yuki/web/models.py` | 64 | N | AST/导入未见直接数据库入口；格式/协议/配置声明或间接委托，接收领域另核 |
| `src/yuki/web/native_bridge.py` | 337 | S | 完整结构候选；数据库读/间接调用及遗漏写入留第二轮独立细核 |
| `src/yuki/web/native_sources.py` | 89 | N | AST/导入未见直接数据库入口；格式/协议/配置声明或间接委托，接收领域另核 |
| `src/yuki/web/page_extractor.py` | 117 | N | AST/导入未见直接数据库入口；格式/协议/配置声明或间接委托，接收领域另核 |
| `src/yuki/web/route_context.py` | 22 | N | AST/导入未见直接数据库入口；格式/协议/配置声明或间接委托，接收领域另核 |
| `src/yuki/web/search_config.py` | 54 | N | AST/导入未见直接数据库入口；格式/协议/配置声明或间接委托，接收领域另核 |
| `src/yuki/web/tavily.py` | 270 | N | AST/导入未见直接数据库入口；格式/协议/配置声明或间接委托，接收领域另核 |
| `src/yuki/workspace/__init__.py` | 1 | N | AST/导入未见直接数据库入口；格式/协议/配置声明或间接委托，接收领域另核 |
| `src/yuki/workspace/files.py` | 170 | N | AST/导入未见直接数据库入口；格式/协议/配置声明或间接委托，接收领域另核 |
| `src/yuki/workspace/inspect.py` | 139 | N | AST/导入未见直接数据库入口；格式/协议/配置声明或间接委托，接收领域另核 |
| `src/yuki/workspace/service.py` | 244 | F | 见本轮相应领域函数及原子/锁边界；包含完整实现 |
| `src/yuki/workspace/short_state.py` | 109 | F | 见本轮相应领域函数及原子/锁边界；包含完整实现 |
| `src/yuki/workspace/store.py` | 284 | F | 见本轮相应领域函数及原子/锁边界；包含完整实现 |
| `src/yuki/workspace/tools.py` | 65 | N | AST/导入未见直接数据库入口；格式/协议/配置声明或间接委托，接收领域另核 |


## 5. 函数级事务候选索引

下表为 AST 定位补充，覆盖本轮 A 域全部直接事务/flush/DML 候选；嵌套函数可能同时列在外层。
调用点不证明该函数必写，只有上文语义确认可称真实写入。未执行的分支、ORM dirty setter 和间接仓库提交仍要沿 caller 补核。

| 文件 | 函数与范围 | 事务/写入信号 |
| --- | --- | --- |
| `src/yuki/admin/audit.py` | `add_audit_event:108-144` | 142:session.add, 143:session.flush |
| `src/yuki/admin/audit.py` | `transaction:154-158` | 157:session.begin |
| `src/yuki/admin/config_files.py` | `_replace:454-482` | 467:stream.flush |
| `src/yuki/admin/config_service.py` | `save_with_audit:317-390` | 334:optional_session, 360:active.add, 368:active.flush |
| `src/yuki/admin/config_service.py` | `delete_with_audit:392-432` | 406:optional_session, 409:active.execute |
| `src/yuki/admin/config_service.py` | `_mutation_session:534-544` | 543:self._database.immediate_session |
| `src/yuki/admin/config_service.py` | `rollback:987-1319` | 1011:self._database.apply_snapshot_write, 1001:self.rollback |
| `src/yuki/admin/config_service.py` | `apply:1000-1009` | 1001:self.rollback |
| `src/yuki/admin/operations/base.py` | `database_command:154-230` | 173:self.database.immediate_session, 196:session.begin_nested |
| `src/yuki/admin/operations/base.py` | `external_command:232-345` | 250:self.database.immediate_session, 286:self.database.immediate_session, 306:self.database.immediate_session |
| `src/yuki/admin/operations/config.py` | `_change:65-130` | 102:self.config.rollback |
| `src/yuki/admin/operations/config.py` | `mutate:81-126` | 102:self.config.rollback |
| `src/yuki/admin/operations/groups.py` | `set_enabled:19-35` | 30:session.flush |
| `src/yuki/admin/operations/groups.py` | `mutate:24-31` | 30:session.flush |
| `src/yuki/admin/operations/identity.py` | `transfer_binding:55-73` | 69:self.database.immediate_session |
| `src/yuki/admin/operations/identity.py` | `forget_person:75-121` | 121:self.database.apply_snapshot_write |
| `src/yuki/admin/operations/identity.py` | `change_person:123-177` | 177:self.database.apply_snapshot_write |
| `src/yuki/admin/operations/identity.py` | `create_person:179-201` | 195:session.add, 196:session.flush |
| `src/yuki/admin/operations/identity.py` | `mutate:186-197` | 195:session.add, 196:session.flush |
| `src/yuki/automation/repository.py` | `create:61-168` | 78:self._database.immediate_session, 80:optional_session, 152:active.add, 166:active.add, 153:active.flush, 167:active.flush |
| `src/yuki/automation/repository.py` | `set_status:410-439` | 427:optional_session, 428:active.execute |
| `src/yuki/automation/repository.py` | `cancel:441-463` | 449:optional_session, 450:active.execute |
| `src/yuki/automation/repository.py` | `resume:465-495` | 474:optional_session, 475:active.execute |
| `src/yuki/automation/repository.py` | `schedule_now:497-522` | 506:optional_session, 507:active.execute |
| `src/yuki/automation/repository.py` | `publish_task:524-604` | 536:optional_session, 602:active.add, 603:active.flush |
| `src/yuki/automation/repository.py` | `claim_due:606-708` | 648:self._database.immediate_session |
| `src/yuki/automation/repository.py` | `release_claim:710-726` | 718:session.begin, 719:session.execute |
| `src/yuki/automation/repository.py` | `renew_claim:738-749` | 739:self._database.immediate_session, 740:session.execute |
| `src/yuki/automation/repository.py` | `create_run:751-850` | 764:self._database.immediate_session, 823:session.add, 824:session.flush |
| `src/yuki/automation/repository.py` | `finish_run:970-1066` | 989:self._database.immediate_session, 991:optional_session, 1002:active.execute |
| `src/yuki/automation/service.py` | `create_task:186-277` | 248:self._repository._database.immediate_session |
| `src/yuki/automation/service.py` | `_commit_update:305-368` | 346:self._repository._database.immediate_session |
| `src/yuki/automation/service.py` | `pause:458-478` | 461:self._repository._database.immediate_session |
| `src/yuki/automation/service.py` | `resume:480-503` | 484:self._repository._database.immediate_session |
| `src/yuki/automation/service.py` | `cancel:505-524` | 508:self._repository._database.immediate_session |
| `src/yuki/automation/service.py` | `run_now:526-546` | 529:self._repository._database.immediate_session |
| `src/yuki/character/repository.py` | `initialize:92-111` | 94:self.database.immediate_session, 107:session.add, 108:session.flush |
| `src/yuki/character/repository.py` | `update:137-196` | 146:self.database.immediate_session |
| `src/yuki/character/repository.py` | `set_owner:215-265` | 220:self.database.immediate_session, 238:session.add |
| `src/yuki/character/soul.py` | `_replace:54-73` | 66:stream.flush |
| `src/yuki/character/soul.py` | `_reserve:140-213` | 150:self.identity.database.immediate_session |
| `src/yuki/character/soul.py` | `_finish:215-255` | 218:self.identity.database.immediate_session, 248:session.add, 249:session.flush |
| `src/yuki/client_api/identity.py` | `_issue_code:289-338` | 319:session.add |
| `src/yuki/client_api/identity.py` | `issue_for_device:340-351` | 341:self.database.immediate_session |
| `src/yuki/client_api/identity.py` | `issue_for_operator:353-363` | 356:self.database.immediate_session |
| `src/yuki/client_api/identity.py` | `pair:389-433` | 390:self.database.immediate_session, 419:session.add |
| `src/yuki/client_api/identity.py` | `cleanup_expired:460-467` | 461:self.database.immediate_session, 462:session.execute |
| `src/yuki/client_api/identity.py` | `revoke:561-578` | 564:self.database.immediate_session |
| `src/yuki/client_api/identity.py` | `revoke_for_operator:580-593` | 583:self.database.immediate_session |
| `src/yuki/client_api/identity.py` | `transfer_for_operator:595-640` | 610:self.database.immediate_session |
| `src/yuki/client_api/identity.py` | `issue_for_account:642-661` | 646:self.database.immediate_session |
| `src/yuki/client_api/identity.py` | `claim_account:663-735` | 685:self.database.apply_snapshot_write |
| `src/yuki/client_api/prompts.py` | `finish:115-176` | 134:self.identity.database.immediate_session |
| `src/yuki/client_api/prompts.py` | `cancel:219-295` | 241:self.identity.database.immediate_session |
| `src/yuki/client_api/prompts.py` | `stop_original:232-283` | 241:self.identity.database.immediate_session |
| `src/yuki/client_api/sessions.py` | `create:24-97` | 25:self.identity.database.immediate_session |
| `src/yuki/client_api/works.py` | `submit:377-524` | 517:self.database.apply_snapshot_write |
| `src/yuki/client_api/works.py` | `cancel:526-575` | 532:self.database.immediate_session |
| `src/yuki/client_sdk/outbox.py` | `__init__:12-29` | 16:db.execute, 24:db.execute, 28:db.commit, 23:db.execute |
| `src/yuki/client_sdk/outbox.py` | `bind:42-69` | 51:db.execute, 64:db.execute, 69:db.commit |
| `src/yuki/client_sdk/outbox.py` | `prepare:71-84` | 76:db.execute, 83:db.commit |
| `src/yuki/client_sdk/outbox.py` | `update:102-112` | 108:db.execute, 112:db.commit |
| `src/yuki/conversation/autonomy_repository.py` | `_feedback_session:45-52` | 51:database.immediate_session |
| `src/yuki/conversation/autonomy_repository.py` | `ensure_binding:118-142` | 120:self._database.immediate_session, 140:session.add, 141:session.flush |
| `src/yuki/conversation/autonomy_repository.py` | `transition:149-195` | 161:self._database.immediate_session |
| `src/yuki/conversation/autonomy_repository.py` | `accept_host_proposal:197-377` | 247:self._database.immediate_session, 375:session.add, 376:session.flush |
| `src/yuki/conversation/autonomy_repository.py` | `record_feedback:524-584` | 575:session.add |
| `src/yuki/conversation/canonical_db_models.py` | `_install_assignment_roles:710-722` | 720:connection.execute |
| `src/yuki/conversation/hydrate.py` | `ensure_canonical_conversation:166-308` | 231:session.begin_nested, 232:session.add, 253:session.add, 263:session.flush |
| `src/yuki/conversation/hydrate.py` | `ensure_legacy_alias:311-355` | 335:session.execute |
| `src/yuki/conversation/hydrate.py` | `bump_canonical_generation:358-383` | 382:session.flush |
| `src/yuki/conversation/media_index.py` | `index_event_media:97-135` | 135:session.add |
| `src/yuki/conversation/media_service.py` | `_cache_original:661-752` | 730:self.database.immediate_session |
| `src/yuki/conversation/media_service.py` | `_expire_page:988-1029` | 1009:self.database.immediate_session, 1010:writer.execute |
| `src/yuki/conversation/observations.py` | `_valid_observation_root:18-52` | 49:active.add |
| `src/yuki/conversation/ordinary_admission.py` | `publisher:297-342` | 334:self.commit |
| `src/yuki/conversation/ordinary_admission.py` | `commit_to_activation:344-373` | 366:self.commit |
| `src/yuki/conversation/ordinary_admission.py` | `commit:375-452` | 430:session.execute, 387:self.database.immediate_session, 388:self.commit |
| `src/yuki/conversation/ordinary_admission.py` | `publish:300-340` | 334:self.commit |
| `src/yuki/conversation/projection_models.py` | `install_projection_triggers:75-82` | 80:connection.exec_driver_sql |
| `src/yuki/conversation/projections.py` | `prepare_commit:244-539` | 511:session.add, 512:session.flush, 514:session.add, 536:session.flush, 480:session.execute, 527:session.execute |
| `src/yuki/conversation/projections.py` | `invalidate_view:588-607` | 591:session.begin, 592:session.execute |
| `src/yuki/conversation/projections.py` | `publish:364-537` | 511:session.add, 512:session.flush, 514:session.add, 536:session.flush, 480:session.execute, 527:session.execute |
| `src/yuki/conversation/uploads.py` | `upload:56-149` | 123:self.identity.database.immediate_session |
| `src/yuki/deployment_setup/service.py` | `initialize_soul:432-448` | 441:stream.flush |
| `src/yuki/deployment_setup/service.py` | `_atomic_write:840-856` | 847:handle.flush |
| `src/yuki/emoji/sticker_index.py` | `write_json:80-98` | 87:output.flush |
| `src/yuki/emoji/sticker_index.py` | `cache:178-205` | 199:db.executescript |
| `src/yuki/emoji/sticker_index.py` | `_cache_batch:229-267` | 257:db.execute, 259:db.execute, 261:db.execute, 262:db.executemany |
| `src/yuki/gateway/providers/feishu_ingress.py` | `start:300-310` | 302:self.database.immediate_session |
| `src/yuki/gateway/providers/feishu_ingress.py` | `receive:333-406` | 367:self.database.immediate_session |
| `src/yuki/gateway/providers/telegram_ingress.py` | `start:338-351` | 342:self.database.immediate_session |
| `src/yuki/gateway/providers/telegram_ingress.py` | `receive:396-505` | 405:self.database.immediate_session, 466:self.database.immediate_session |
| `src/yuki/identity/bootstrap.py` | `bootstrap_settings_identity:12-28` | 16:session.begin |
| `src/yuki/identity/canonical_repository.py` | `ensure_account:117-136` | 135:session.add |
| `src/yuki/identity/canonical_repository.py` | `replace_assignment:139-184` | 182:session.add, 183:session.flush, 165:session.flush |
| `src/yuki/identity/canonical_repository.py` | `create_person_binding:230-257` | 254:session.add |
| `src/yuki/identity/canonical_repository.py` | `create_space_binding:287-334` | 299:session.begin_nested, 323:session.add_all, 324:session.flush |
| `src/yuki/identity/canonical_repository.py` | `forget_person_by_id:342-376` | 355:session.execute, 359:session.execute, 362:session.execute, 367:session.execute, 370:session.execute, 376:session.execute |
| `src/yuki/identity/canonical_uow.py` | `record_account_fact:91-203` | 185:session.add_all, 203:self._database.apply_snapshot_write, 200:session.flush |
| `src/yuki/identity/canonical_uow.py` | `append_inbound:205-450` | 410:session.add, 450:self._database.apply_snapshot_write, 411:session.flush, 373:session.begin_nested, 374:session.add, 386:session.flush |
| `src/yuki/identity/canonical_uow.py` | `record:111-201` | 185:session.add_all, 200:session.flush |
| `src/yuki/identity/canonical_uow.py` | `append:258-446` | 410:session.add, 411:session.flush, 373:session.begin_nested, 374:session.add, 386:session.flush |
| `src/yuki/identity/ingress.py` | `pre_admit:139-194` | 143:session.begin, 183:session.begin |
| `src/yuki/identity/person_identity.py` | `rename_person:111-147` | 134:session.add |
| `src/yuki/identity/person_identity.py` | `merge_persons:150-355` | 341:session.add, 306:session.execute, 320:session.execute, 331:session.add, 287:session.execute, 298:session.execute |
| `src/yuki/identity/person_state.py` | `prepare_message_account_observations:52-280` | 220:session.add_all, 225:session.add, 254:session.add |
| `src/yuki/identity/person_state.py` | `observe_canonical_person:287-375` | 341:session.add, 363:session.add |
| `src/yuki/identity/person_state.py` | `prepare_canonical_person_observation:378-433` | 422:session.add |
| `src/yuki/identity/person_state.py` | `apply:219-278` | 220:session.add_all, 225:session.add, 254:session.add |
| `src/yuki/identity/person_state.py` | `apply:412-431` | 422:session.add |
| `src/yuki/identity/routing.py` | `prepare_person_merge_routes:844-910` | 886:session.add |
| `src/yuki/identity/routing.py` | `_insert_person_route:1165-1187` | 1172:self._database.immediate_session, 1175:session.add |
| `src/yuki/identity/routing.py` | `_write_person_transition:1189-1233` | 1198:self._database.immediate_session |
| `src/yuki/identity/routing.py` | `_insert_space_route:1268-1290` | 1275:self._database.immediate_session, 1278:session.add |
| `src/yuki/identity/routing.py` | `_write_space_transition:1292-1335` | 1301:self._database.immediate_session |
| `src/yuki/identity/routing.py` | `_provision_ingest_route:1337-1453` | 1380:self._database.immediate_session, 1421:session.add |
| `src/yuki/identity/routing.py` | `apply:881-908` | 886:session.add |
| `src/yuki/identity/space_metadata.py` | `observe_space_name:15-57` | 57:database.apply_snapshot_write |
| `src/yuki/persistence/command_receipts.py` | `record_personal_command:228-291` | 271:session.add, 289:session.add, 272:session.flush, 290:session.flush |
| `src/yuki/persistence/command_receipts.py` | `recover_interrupted_controls:293-313` | 295:self._database.immediate_session, 296:session.execute |
| `src/yuki/persistence/command_receipts.py` | `reserve_external:315-363` | 347:session.add, 349:session.add, 348:session.flush |
| `src/yuki/persistence/command_receipts.py` | `mark_unknown:365-406` | 375:self._database.immediate_session, 403:session.add, 404:session.flush |
| `src/yuki/persistence/command_receipts.py` | `record_success:658-760` | 711:session.add, 759:session.add, 712:session.flush |
| `src/yuki/persistence/command_receipts.py` | `record_failure:762-827` | 802:session.add, 826:session.add, 803:session.flush |
| `src/yuki/persistence/database.py` | `create_schema:87-128` | 104:self.engine.begin, 121:connection.exec_driver_sql |
| `src/yuki/persistence/database.py` | `immediate_session:189-210` | 199:session.commit, 195:session.execute, 197:session.begin, 202:session.rollback |
| `src/yuki/persistence/database.py` | `apply_snapshot_write:212-240` | 221:session.begin, 226:session.flush |
| `src/yuki/persistence/diagnostic_writer.py` | `commit:44-47` | 45:session.begin |
| `src/yuki/persistence/diagnostic_writer.py` | `_prepare_and_commit:136-169` | 144:prepared.commit |
| `src/yuki/persistence/history_search_index.py` | `replace_event:20-46` | 31:session.execute, 44:session.execute |
| `src/yuki/persistence/history_search_index.py` | `rebuild_history_search:49-97` | 55:connection.exec_driver_sql, 57:connection.exec_driver_sql, 60:connection.commit, 74:connection.commit, 62:connection.rollback, 88:connection.exec_driver_sql, 66:connection.exec_driver_sql, 90:connection.exec_driver_sql, 93:connection.commit, 95:connection.rollback |
| `src/yuki/persistence/instance_lock.py` | `_lock_handle:55-75` | 62:handle.flush |
| `src/yuki/persistence/instance_lock.py` | `acquire:25-43` | 37:handle.flush |
| `src/yuki/persistence/job_claims.py` | `commit_job_claims:25-62` | 32:session.begin |
| `src/yuki/persistence/media_repository.py` | `save:58-121` | 118:session.begin |
| `src/yuki/persistence/media_repository.py` | `associate_event:123-146` | 134:session.begin, 135:session.execute |
| `src/yuki/persistence/media_repository.py` | `cleanup_expired:148-171` | 165:self._database.immediate_session, 166:session.execute |
| `src/yuki/persistence/models.py` | `_install_artifact_references:321-323` | 323:connection.exec_driver_sql |
| `src/yuki/persistence/people_repository.py` | `observe:493-540` | 540:self._database.apply_snapshot_write |
| `src/yuki/persistence/people_repository.py` | `set_enabled:747-762` | 755:owned_session.begin |
| `src/yuki/persistence/people_repository.py` | `delete_person_id:784-803` | 803:self._database.apply_snapshot_write |
| `src/yuki/persistence/people_repository.py` | `apply_person_forget:1225-1425` | 1246:session.execute, 1255:session.execute, 1264:session.execute, 1289:session.execute, 1294:session.execute, 1299:session.execute, 1304:session.execute, 1309:session.execute, 1312:session.execute, 1317:session.execute, 1325:session.execute, 1351:session.execute, 1370:session.execute, 1394:session.execute, 1403:session.execute, 1407:session.execute, 1412:session.execute, 1417:session.execute, 1418:session.execute, 1421:session.execute, 1365:session.execute, 1359:session.execute |
| `src/yuki/persistence/people_repository.py` | `_forget_person_plugin:1428-1477` | 1447:session.execute, 1456:session.execute, 1461:session.execute, 1467:session.execute, 1473:session.execute, 1439:session.execute |
| `src/yuki/persistence/people_repository.py` | `_forget_person_automations:1480-1492` | 1485:session.execute |
| `src/yuki/persistence/people_repository.py` | `_forget_person_memory:1495-1546` | 1512:session.execute, 1513:session.execute, 1526:session.execute, 1529:session.execute, 1537:session.execute, 1542:session.execute |
| `src/yuki/persistence/people_repository.py` | `_forget_person_conversation:1585-1651` | 1623:session.execute, 1631:session.execute, 1633:session.execute, 1635:session.execute, 1640:session.execute, 1647:session.execute |
| `src/yuki/persistence/people_repository.py` | `set_enabled:1671-1682` | 1679:owned_session.begin |
| `src/yuki/persistence/people_repository.py` | `set_autonomous_enabled:1684-1703` | 1694:owned_session.begin |
| `src/yuki/persistence/scoped_event_uow.py` | `append:136-205` | 204:self._database.immediate_session |
| `src/yuki/persistence/scoped_event_uow.py` | `update_platform_message:214-359` | 303:self._database.immediate_session, 347:session.flush, 327:session.add, 344:session.delete |
| `src/yuki/persistence/scoped_event_uow.py` | `_set_derived_text:418-481` | 442:self._database.immediate_session, 471:session.flush |
| `src/yuki/persistence/scoped_event_uow.py` | `_append_canonical:727-1074` | 1019:session.add, 923:session.begin_nested, 924:session.add, 1015:session.begin_nested, 1016:session.add, 1020:session.flush, 936:session.flush, 1017:session.flush |
| `src/yuki/persistence/unit_of_work.py` | `optional_session:39-59` | 56:owned.begin |
| `src/yuki/persistence/web_repository.py` | `cleanup_expired:21-50` | 44:self._database.immediate_session, 45:session.execute |
| `src/yuki/web/bridge_state.py` | `_access:28-63` | 35:db.execute, 41:db.execute, 42:db.execute, 44:db.execute, 48:db.execute |
| `src/yuki/workspace/files.py` | `_write:119-170` | 155:stream.flush |
| `src/yuki/workspace/short_state.py` | `_prepare:40-45` | 41:db.execute, 45:db.execute |
| `src/yuki/workspace/short_state.py` | `update:68-87` | 81:db.execute |
| `src/yuki/workspace/store.py` | `_transaction:38-78` | 72:db.execute, 51:db.execute, 58:db.execute, 62:db.commit, 66:db.execute, 71:db.commit, 75:db.commit, 70:db.execute, 77:db.rollback |
| `src/yuki/workspace/store.py` | `cleanup:139-164` | 157:db.execute, 158:db.executemany, 159:db.execute |
| `src/yuki/workspace/store.py` | `snapshot:202-284` | 231:output.flush, 262:db.execute, 277:db.execute |

## 6. 尚需后续独立轮补齐

本轮不是整个任务的三轮完成。第二/三轮应优先把 S/T 中的实际 DB 实现补为函数全文，尤其 PeopleRepository 隐私清退准备/应用、完整 route candidate/dispatch 读链、projection.prepare_commit 准备半段、AutomationService/Worker 组合边界、ClientViews/Files/Requests 的一致读与权限、schema/trigger 安装与诊断 hook。每个被排除的传输/能力文件还要核是否把同一 session 或 callback 间接传入 writer。

没有更改任务书主文件；A01-A13 是供主审合并的具体修订，不把本轮静态观察当作重构实施或性能验收。
