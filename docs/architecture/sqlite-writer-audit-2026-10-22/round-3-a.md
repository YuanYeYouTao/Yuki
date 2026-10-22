# 第 3 轮 A 域终审

日期：2026-10-23。源码基线 `c2ce7da66ab54efe192993b9615df48266a62137`，与前两轮
`cdb49eadc0c7cd16060bf0329234ae69611e62bd` 分开记录。仓库 origin 为
`https://github.com/YuanYeYouTao/Yuki-QQbot.git`。开始时工作树只有本次计划/审查文档的未提交变化。
本轮只写本文；未修改产品代码，未运行项目测试、模型、生产或真实平台消息。

已读 AGENTS、development-contract、A 域前两轮报告及当前统一写入任务书。按源码独立核方法正文，
再与前两轮范围交叉检查；239 个文件的结构清单不是 239 个文件逐字全文，也不是三遍机器扫描。
当前主任务书为 35 单元、73 任务、50 场景；下述新增内容已发给主审合入，不另造框架。

## R3A01 人物遗忘：完整准备集合比 Person/fact 的 revision 大得多

本轮全文读 `persistence/people_repository.py`，重点 `prepare_person_forget:820–1223`、
`apply_person_forget:1225–1425` 和全部四组删除助手。旧入口 `delete_person_id:784–803`
在同一显式 WAL 快照准备并升级写；外部 peer 关闭已在此前独立完成。换成新 reader + writer 后，
旧快照提供的集合与缺席保护必须明确接回，不能只保 `person_family_ids` 比较和旧行 CAS。

| 准备依赖 | 原查询/后续用途 | 新 writer 必须保的语义 |
| --- | --- | --- |
| 人物族、全部历史 Assignment、private Conversation | 833–864；含 bounded 平台/时间窗 | 新合并成员、Assignment 阶段或新 private Conversation 不漏清，不能只核原 person_id |
| mentions、原正文、Presence 平台 | 925–970；按原平台及历史归属生成 redaction/FTS | 新增命中事件和原事件正文/segments/ASR 变化均影响准备结果，不能把旧 redaction 直接覆盖新正文 |
| Memory tool/artifact/Work 的 copied source_event_ids | 875–908 | 新增匹配复制来源、来源事件归属变化不漏；artifact 标 deleting 与原 Work 清退依旧在同一提交 |
| initiative 的 sources/support_refs、关联 tool/fact/candidate | 1014–1097 | 不只核已选 initiative_ids；当前匹配成员新增和 evidence 关系变化都属于实际选择依赖 |
| SELF checkpoint 四类 map 与旧 rendered envelope | 1098–1188 | 保 event/tool/fact/candidate 原拷贝、提及人及旧格式真实事件信封；不能只查 run 的 person_id |
| audit 的 actor/target/conversation/JSON 解码匹配 | 978–1009 | 新增匹配 audit、原 before/after 快照变化都需处理；解析、脱敏、大 JSON 编码移出 writer |
| resource cleanup 的原请求/peer | 1011–1013、1208–1222 | 已关 peers 是事实；新请求/peer/作用域不能凭旧关闭结果直接当已清完 |

apply 中不少 DELETE 仍使用当前子查询，例如 claim candidate、原 event 的 owned 谓词；另一些只消费
prepared IDs/bytes，例如 redactions、audit、checkpoint、source artifacts。因此不能用“apply 里会重查”
概括全部。原 `closed_resources` 只验证四组集合与 cleanup，不是全部隐私准备的独立证明。

原子单元仍是一次授权清退：Work/continuation、Memory、插件、自动化、配置、事件/FTS、原引用和人物族
一起提交。不能按维护页数拆开。全局 trace 删除及 privacy_generation 增量是原清退行为，
不为统一 writer 新增另一份全库版本。

## R3A02 合并与三处 session closure

全文读 `identity/person_identity.py`。`merge_persons:150–355` 的实际准备包括两当前 root 的资料、
source 族、source/target RuntimeConfig、PluginConfig、source 旧名 alias，以及目标已有配置集合。
目标 scalar key、plugin 对象和 alias 的“不存在”决定移动/丢弃/补 alias，旧行 version CAS 不覆盖后来新增目标。
审计 JSON 的丢弃资料解析/脱敏、移动谱系编码保在准备阶段；原 Person、Assignment、事件与 receipt 不改写。

`person_state.py` 全文已核：`prepare_message_account_observations:52–280` 的 apply 捕获
session、新 Account、历史 assignment、profile/observation ORM；`prepare_canonical_person_observation:378–433`
捕获 session、current person、space/binding、membership。新增 writer 不能执行原 reader closure。
复用原 native/time/stage、完整观测 key/absence 与实际字段，应用到 writer 自己的对象即可，不需要通用 Plan。

第三处是 `routing.prepare_person_merge_routes:844–910`：它还捕获两 route 的 ORM、目标 Person、
Registry/Binding/Presence 候选和 target route absence；apply 决定继承、保留或删除 route。
必须与合并配置迁移及原 control receipt 在同一 writer 复核/应用。只改 person_state 的两个 closure 仍漏这一处。

普通 route writer 与 ingest writer 的现状并不完全相同：

- `_insert_person_route:1165–1187`、`_write_person_transition:1189–1233` 及 Space 对应方法，
  写内主要核 route absence/generation/observed stamp；不能把这个 CAS 宣称为重新核过全部候选。
- `_provision_ingest_route:1337–1453` 已在 writer 核 binding revision、原连接、Presence 及 ingest 资格。
  当前 schema route trigger 核所有权/平台/类型，不替代 enabled/status/Registry 资格。
- `_space_candidates:912–973`、`_space_pin_healthy:975–1001`、`_ingest_pin_healthy:1003–1025`
  先退出 reader 再 membership probe；新设计保这一点，不把网络探针移进 writer。

## R3A03 入账与 projection：保正确现有准备，删除旧混合边界

独立读 canonical `record_account_fact/append_inbound` 及全部辅助方法、scoped 全部数据库方法，
并全文读 projection repository。canonical `append:258–446` 的 Account/mention/Person 观察准备依然
属于旧快照 callback；FTS 分词已在外，不把五个已正确传 prepared_terms 的调用写成新缺陷。
scoped `_append_canonical:727–1074` 的已有 Conversation/alias/receipt、causal/reply 校验和外部 payload 编码
要按真实轻重分开；原 event/receipt/媒体/FTS/水位继续同提交。savepoint 不再次进入 admission。

`ProjectionPublication.publish:364–537` 接受 caller session，没有捕获 reader session。
准备编码、source/privacy 一致读已在外；写内核 owner/source、privacy、head epoch/revision、checkpoint 覆盖，
再发布 chunk/selection。它还有当前全局派生容量 SUM/victim 选择，不能宣称写内只有单行 UPDATE，
也不能把这段相同 SQL 再复制成新框架。无变化返回保留。`invalidate_view` 真实 commit 后才清内存缓存。
`read/read_checkpoint/selected` 的多查询显式 BEGIN 保留；不把所有单 SELECT 都机械加 BEGIN。

## R3A04 配置与管理：新增一项旧审计谱系的重准备

`admin/operations/base.py` 全文复核：database_command 的 mutation savepoint、失败回执及外层 root commit
分别认原事实；external_command 的 reserve → 外部 action → settle 不合并成一个长 writer。
`config_files.save:233–402` 持文件配置锁完成 prepare/persist/activate，并收拢实际文件任务；它本身不连库。
`CharacterSoul` 全文复核，SOUL lock → reserve writer → 文件/热装配 → finish writer 的顺序保留，
原 running/unknown 与实际文件 hash、active revision 分开，不能重写已发生文件效果。

本轮补全 `RuntimeConfigRepository` 全部读写方法、Service 的全部真实 DB 方法及 rollback。
`_matches_original_state:1731–1776` 有前两轮未展开的历史分支：旧 audit 没有 row_id，且配置经历人物合并时，
会扫描同人族成功 merge 的 after_json，逐项解析 configuration_moves 追谱系。
管理 ConfigOperations._change 在外层 writer/savepoint 调 rollback，因此这段不因“第一条 DML 前”就算 writer 外。
应前移原 audit/谱系读取和 JSON 准备，写内核原 audit、当前 root、配置 row/version、实际相关 move 集合/absence；
有 row_id 的当前快路直接保留。主审已将其补入 W16。不要新增配置 mutex、全局世代或新 journal。

## R3A05 Automation、个人端与独立小库

本轮全文读 Automation repository/service/worker/context/control_context；所有方法的范围见文件表。
create、set_status/cancel/resume/schedule_now、publish_task、claim/release/renew、create_run、
两种 occurrence settlement、finish_run 均有明确 owner。create 的 JSON 虽在 repository 自己的 transaction 前，
Service.create_task 已先持外层 writer 再调用，因此预编码要沿真实外层 owner 前移，不能只看 repository 缩进。
finish_run 读取原 immutable version、解析 schedule/summary 并计算下一次计划；不能把它全称“单行短写”。
claim_due 首次候选 `.all()`，写阶段每 128 个；取得 writer 后 sampled claimed_at 保留。
renew 的 until 在 worker 等 writer 前计算问题已由 W68 覆盖。结算已发生 occurrence 不等于重新授予执行资格。

App identity 的全部方法、Messages/Sessions/Prompts/Files、Work submit/cancel 实际写链已核。
Work input 的 canonical event/mailbox/resume/personal receipt 原子；waiting 重取原 lease 的外循环不是 BUSY 循环。
cancel 先在 writer 外 join 原请求，短事务撤权，再退出 writer join 模型和原效果 activation，避免相互等待。
个人鉴权、来源/下载授权和 GET 保纯读。HTTP database_error 与 SDK submitted/draft 决策必须共同验证 unknown 阶段。

独立库维持各自事实：Outbox 全方法每连接短 commit、不跨 HTTP；ACP 创建本地 sessions.sqlite3/每 Conversation
Outbox，不接服务器主库 admission。WorkspaceStore 与 ShortState 全文读，manifest fence 的短 rename/stat
保护文件引用发布；大读/hash/fsync 已在 fence 外。GC 核引用后 unlink，不明 commit 不直接删已发布 blob。
BridgeState 全文读，普通命中不写、首次访问可建 schema，存入值才 IMMEDIATE。StickerIndex 的真实 DML/cache
修复/rebuild/search 方法核过，保 `.search.lock` flock，查询可修派生缓存；原图片/JSON 不当 cache 清理。

media `_cache_original`、`_sources`、`_expire_page`、`_gc_cache_page` 等全部直接 DB 方法逐个核，
下载/hash/发布在 writer 外，media lock → writer；GC 精确 event/name/receipt 引用读取与文件删除不进主 writer。
MediaAnalysis save/associate/cleanup、job_claims、退役 web 表 cleanup、FTS replace/rebuild 全文复核。
ASR 原输出复用，derived text 的精确 EventRecord/Conversation token/原 transcript 判定保留，不重新买识别。

## R3A06 根入口、诊断、Hint 与 Schema

全文读 Database、optional_session、DiagnosticWriter、SQLiteDiagnostics、CommitHints。
当前 immediate_session 无统一 admission，异常 rollback/invalidate 不独立证明物理退出；任务书 W04–W06
集中实现真实 driver 收拢是必要边界。不能把 shield、pool 事件或 `_connection` 属性当退出确认。
本轮没有另运行 probe，沿已记录的锁定依赖探针作为待实施验证依据。

删除 HintSession/after_commit、session.info 注入，以及旧多连接 holders/first_write 推断、仅为日志的
dialect/raw_connection monkeypatch；保 TimingSummary 和必要薄 SQL 计时。根 commit 确认、owner 释放后
同步 publish，无独立取消 await。live span 两处显式 hint 是内存活动通知，仍保留。hint 不成为 durable receipt。

现行安装链已具名核：metadata 聚合 → Database.create_schema/_create_fts_schema；canonical 五个 after_create
installer、_install_parent_route_guards_if_ready、_install_assignment_roles → canonical_schema 全部 SQL；
PromptProjectionModel after_create → projection_chunks_schema → projection_compact_schema 的当前完整 SQL；
event metadata 字段来自 projection_revision_schema；ChatEventArtifactRef installer → artifact_refs 两触发器。
另复核 Protocol install_quota 的计数字段触发器与 tool_artifact_refs FK；它们同事务生效，不另排队。
旧 projection_current/history/material/0054/0089 文件按冻结历史定义分类，不误当当前独立 writer。

这些是静态安装链/隐式写语义审查，**不代表 ORM create_schema 与 Alembic head 0147 已完成实际等价性验证**。
W59 仍须实施时建库、比结构及行为；纯 ORM 字段/声明按 S 分类，不虚称全文 SQL 验证。触发器和 FK cascade 的实际
执行成本计入 writer。无需为只变事务组织新建 migration。

## R3A07 cdb49ead → c2ce7da 的 36 文件增量

独立检查全部 changed paths 与 src 完整 diff，并读新链的完整方法；14 个测试读修改 hunks/新增文件，
6 份文档读新增/修改范围，配置/CI/依赖读 diff，不把未运行测试写成通过。

| 分类 | 文件 | 数据库结论 |
| --- | --- | --- |
| D 域 src 2 | container.py；prompting/contracts.py | 前者只绑定 participation_runtime_context 回调，后者提示文字；没有新 DB owner/事务 |
| C 域 src 9 | runtime/work_control.py；services/agent_runner.py、chat.py、main_agent_backend.py、participation_identity.py、participation_ordinary.py、processor.py、semantic_participation.py、turn_execution.py | 新只读反馈/现有 snapshot 保存链，详见下文；没有新增裸主库 writer |
| 集成测试 4 | participation_foreground_cost、parallel_review、person_identity、projection_cost | 原 source/admission、同人解析、observer 队列和当前名称；是行为证据源码，未执行 |
| 单元测试 10 | acp_adapter、open_group_semantic_qualification、ordinary_input_handoff、participation_context_scope、participation_foreground_preparation、participation_ordinary_feedback、participation_pipeline_retirement、participation_profile_upgrade、participation_quiet_coverage、participation_runtime_tail | 原 quiet 覆盖、same activation/actor、readonly feedback、paid pause/recovery/原配对、环境隔离；未执行 |
| 文档 6 | architecture/README、JEV稀疏复核任务书、autonomous-participation-model、main-agent-runtime、semantic-participation；operations/sparse-jev-implementation | 参与合同/实施证据，既往通过数不当本轮 writer 验收 |
| 配置 2 | config/SOUL.md；autonomous-model.example.json | 文案与退役 continuation_review_span，无主库 Schema 变化 |
| CI 1 | .github/workflows/quality.yml | APT 源修正，无数据库生命周期改变 |
| 依赖 2 | pyproject.toml；uv.lock | 固定 addfcddecb09f93ad8846db508879c451bc4bc47；两个文件一致 |

`runtime_context:370–476` 沿原 effective Conversation/event、已有 admission 和已加载有限 controller 投影，
先 readonly admission、再一次人物批读；不拿 observer lock、不 hydrate、不保存、不扫永久历史。
不能因新反馈发生于请求准备就搬进 writer，也不能新增统一“读取排队”使此路径等待所有写入。
`actor_identity` 新收 events/boundaries 人物；`continuation_current` 接收的 session 一路传到该批读，
不能机械替为另一个 reader。`_sources_current` 新分支也复用 supplied session。

quiet 补入实际已观察的 same activation/actor/unit continuation refs，经 current_admissions 的真实
event/owner/generation/source 校验后更新原 hint basis，再由 `_save:492–523` 保存独立 snapshot。
这是已付响应后的派生保存，不是新工具或模型执行，不因保存失败重调响应。
新正常请求尾部材料保已付 pause/exact recovery、原 prefix/签名/工具配对，反馈不写公共 projection。

本地参与库可读新 pin；`git diff 438f204f..addfcdde -- src/yuki_participation/store.py` 为空，
并读了新 pin store.py 全文及宿主 AsyncSnapshotStore。save 编码在 BEGIN 前，原 CAS/总量/单专用线程模型未变；
宿主 _save 的实际 save 任务完成后先认真实 revision 再传播取消，不能把 AsyncSnapshotStore._run 单次 shield
解读为任意调用者已 join。未核实归档安装/CI/线上运行，不以本地 git 对象代替实际部署。

## 测试、性能与累计覆盖结论

本轮新增测试阅读集中于上表 14 个 delta 文件，复用既有 privacy/preparation、person_merge_routes/regrant、
runtime_config_read_snapshot、derived_text_transactions、admin_external_effects、client_api/views/continuation、
Automation lifecycle 与真实 writer cancellation 的测试入口。未运行测试。需新增的 barrier 场景仍是 V03/V30：
准备后新增族/Assignment/匹配 source、redaction/audit 正文改变、目标配置/alias absence 变化及旧谱系新增，
并验证 commit unknown 不重跑外部效果；不以固定 SQL 数或私有函数布局冻结实现。

保持热 receipt/路由/GET 并行；简单状态变更不强拆两段，不复制已 frozen 历史、不每行新开事务/重建连接。
重准备只用于真实大 JSON/历史/证据/文件；writer 重核实际相关集合，不能改成全库 revision 或全量扫描。
当前延迟可接受，性能对照用同负载的总等待/吞吐/事务持有/SQL次数，不能用少告警冒充更快。

主审 D 在本轮交叉补齐 AdminQueries 全文、EventLedgerRepository 全文、ClientViews 全文、ClientWorks 1–331
全部 GET 与 setup 的 apply_pending_plugins/_bootstrap_pending_plugins/load_plugin_setup_states。相关路径在下表
标 X，不冒称由本 reviewer 全文。EventLedger 的只读 interrupt/收拢不是旧 writer 诊断包装，应保留。
结合 R1/R2 的 F/具名方法正文及本轮补齐，未再发现未归属的 A 域真实写入口或未明确交接的数据库方法。
剩余 ORM 全字段逐字核对、冻结历史 SQL 和实际 Schema 等价性按 S/历史链与 W59 标明，不混称运行时写遗漏。

## 239 文件最终清单

F=本轮全文；M=本轮具名完整 DB 方法/精确范围，余结构；I=间接消费者/调用目标复核；
S=结构/Schema 分类及已列安装链，非全文所有字段；N=无自管 DB 的结构排除；X=本轮主审 D 交叉补读。
累计列标前两轮 F 或明确具名范围，不把前轮 F 自动升级为本轮 F。

本轮等级：F=61, I=49, M=16, N=103, S=6, X=4。

| 文件（相对 src/yuki） | 行数 | R3 | 累计正文依据 | 本轮实际范围/排除依据 |
| --- | ---: | --- | --- | --- |
| `acp_adapter/__init__.py` | 6 | N | 本轮补齐/分类 | 导入/await/SQL结构重核；纯协议/配置/计算或已列领域委托，无自管DB连接/提交 |
| `acp_adapter/agent.py` | 394 | M | 本轮补齐/分类 | 82–195 __init__/_session/_submit/new_session（本地Outbox）；_events/_event_update界面读取到230；余framing结构 |
| `acp_adapter/cli.py` | 51 | N | 本轮补齐/分类 | 导入/await/SQL结构重核；纯协议/配置/计算或已列领域委托，无自管DB连接/提交 |
| `adapters/__init__.py` | 1 | N | 本轮补齐/分类 | 导入/await/SQL结构重核；纯协议/配置/计算或已列领域委托，无自管DB连接/提交 |
| `adapters/onebot/__init__.py` | 1 | N | 本轮补齐/分类 | 导入/await/SQL结构重核；纯协议/配置/计算或已列领域委托，无自管DB连接/提交 |
| `adapters/onebot/activity.py` | 21 | N | 本轮补齐/分类 | 导入/await/SQL结构重核；纯协议/配置/计算或已列领域委托，无自管DB连接/提交 |
| `adapters/onebot/card_parser.py` | 122 | N | 本轮补齐/分类 | 导入/await/SQL结构重核；纯协议/配置/计算或已列领域委托，无自管DB连接/提交 |
| `adapters/onebot/media.py` | 77 | N | 本轮补齐/分类 | 导入/await/SQL结构重核；纯协议/配置/计算或已列领域委托，无自管DB连接/提交 |
| `adapters/onebot/message_id.py` | 9 | N | 本轮补齐/分类 | 导入/await/SQL结构重核；纯协议/配置/计算或已列领域委托，无自管DB连接/提交 |
| `adapters/onebot/normalizer.py` | 472 | N | 本轮补齐/分类 | 导入/await/SQL结构重核；纯协议/配置/计算或已列领域委托，无自管DB连接/提交 |
| `adapters/onebot/provider_adapter.py` | 152 | N | 本轮补齐/分类 | 导入/await/SQL结构重核；纯协议/配置/计算或已列领域委托，无自管DB连接/提交 |
| `adapters/onebot/sender.py` | 168 | N | 本轮补齐/分类 | 导入/await/SQL结构重核；纯协议/配置/计算或已列领域委托，无自管DB连接/提交 |
| `adapters/onebot/social.py` | 132 | N | 本轮补齐/分类 | 导入/await/SQL结构重核；纯协议/配置/计算或已列领域委托，无自管DB连接/提交 |
| `admin/__init__.py` | 23 | N | 本轮补齐/分类 | 导入/await/SQL结构重核；纯协议/配置/计算或已列领域委托，无自管DB连接/提交 |
| `admin/audit.py` | 242 | I | R1全文 | 方法/导入/调用owner结构重核；累计R1全文；本轮不冒称全文 |
| `admin/authority/__init__.py` | 1 | N | 本轮补齐/分类 | 导入/await/SQL结构重核；纯协议/配置/计算或已列领域委托，无自管DB连接/提交 |
| `admin/authority/capabilities.py` | 110 | N | 本轮补齐/分类 | 导入/await/SQL结构重核；纯协议/配置/计算或已列领域委托，无自管DB连接/提交 |
| `admin/authority/command_types.py` | 341 | N | 本轮补齐/分类 | 导入/await/SQL结构重核；纯协议/配置/计算或已列领域委托，无自管DB连接/提交 |
| `admin/authority/commands.py` | 85 | N | 本轮补齐/分类 | 导入/await/SQL结构重核；纯协议/配置/计算或已列领域委托，无自管DB连接/提交 |
| `admin/authority/decision.py` | 114 | N | 本轮补齐/分类 | 导入/await/SQL结构重核；纯协议/配置/计算或已列领域委托，无自管DB连接/提交 |
| `admin/authority/json_types.py` | 50 | N | 本轮补齐/分类 | 导入/await/SQL结构重核；纯协议/配置/计算或已列领域委托，无自管DB连接/提交 |
| `admin/authority/operations.py` | 118 | N | 本轮补齐/分类 | 导入/await/SQL结构重核；纯协议/配置/计算或已列领域委托，无自管DB连接/提交 |
| `admin/authority/operators.py` | 114 | I | R1全文 | 方法/导入/调用owner结构重核；累计R1全文；本轮不冒称全文 |
| `admin/authority/principal.py` | 119 | N | 本轮补齐/分类 | 导入/await/SQL结构重核；纯协议/配置/计算或已列领域委托，无自管DB连接/提交 |
| `admin/authority/problems.py` | 62 | N | 本轮补齐/分类 | 导入/await/SQL结构重核；纯协议/配置/计算或已列领域委托，无自管DB连接/提交 |
| `admin/authority/targets.py` | 61 | N | 本轮补齐/分类 | 导入/await/SQL结构重核；纯协议/配置/计算或已列领域委托，无自管DB连接/提交 |
| `admin/authority/tokens.py` | 38 | N | 本轮补齐/分类 | 导入/await/SQL结构重核；纯协议/配置/计算或已列领域委托，无自管DB连接/提交 |
| `admin/config_files.py` | 482 | M | 本轮补齐/分类 | save 233–402完整：文件prepare/persist/activate/取消收拢；无自管DB；replace助手R2 |
| `admin/config_owners.py` | 16 | F | R2全文 | 全文；实际DB/间接调用与结构/Schema按正文分类 |
| `admin/config_registry.py` | 160 | N | 本轮补齐/分类 | 导入/await/SQL结构重核；纯协议/配置/计算或已列领域委托，无自管DB连接/提交 |
| `admin/config_service.py` | 1827 | M | 本轮补齐/分类 | Repository全部120–432；Service _mutation_session、initialize/startup、get/inspect/set/delete/history/rollback/snapshot、owner/bind/actor/matches_original_state完整；纯_resolve/格式帮助结构 |
| `admin/config_spec_helpers.py` | 77 | F | 本轮补齐/分类 | 全文；实际DB/间接调用与结构/Schema按正文分类 |
| `admin/config_specs_asr.py` | 77 | N | 本轮补齐/分类 | 导入/await/SQL结构重核；纯协议/配置/计算或已列领域委托，无自管DB连接/提交 |
| `admin/config_specs_future.py` | 45 | N | 本轮补齐/分类 | 导入/await/SQL结构重核；纯协议/配置/计算或已列领域委托，无自管DB连接/提交 |
| `admin/config_specs_hot.py` | 525 | N | 本轮补齐/分类 | 导入/await/SQL结构重核；纯协议/配置/计算或已列领域委托，无自管DB连接/提交 |
| `admin/config_specs_planner_plugins.py` | 172 | N | 本轮补齐/分类 | 导入/await/SQL结构重核；纯协议/配置/计算或已列领域委托，无自管DB连接/提交 |
| `admin/config_specs_protected.py` | 122 | F | 本轮补齐/分类 | 全文；实际DB/间接调用与结构/Schema按正文分类 |
| `admin/config_specs_restart.py` | 512 | N | 本轮补齐/分类 | 导入/await/SQL结构重核；纯协议/配置/计算或已列领域委托，无自管DB连接/提交 |
| `admin/config_specs_social.py` | 7 | N | 本轮补齐/分类 | 导入/await/SQL结构重核；纯协议/配置/计算或已列领域委托，无自管DB连接/提交 |
| `admin/config_specs_tooling.py` | 64 | N | 本轮补齐/分类 | 导入/await/SQL结构重核；纯协议/配置/计算或已列领域委托，无自管DB连接/提交 |
| `admin/control_resolution.py` | 270 | I | R2全文 | 方法/导入/调用owner结构重核；累计R2全文；本轮不冒称全文 |
| `admin/models.py` | 296 | N | 本轮补齐/分类 | 导入/await/SQL结构重核；纯协议/配置/计算或已列领域委托，无自管DB连接/提交 |
| `admin/operations/__init__.py` | 39 | F | 本轮补齐/分类 | 全文；实际DB/间接调用与结构/Schema按正文分类 |
| `admin/operations/base.py` | 345 | F | R1全文 | 全文；实际DB/间接调用与结构/Schema按正文分类 |
| `admin/operations/config.py` | 184 | F | R2全文 | 全文；实际DB/间接调用与结构/Schema按正文分类 |
| `admin/operations/groups.py` | 35 | I | R2全文 | 方法/导入/调用owner结构重核；累计R2全文；本轮不冒称全文 |
| `admin/operations/identity.py` | 535 | F | 本轮补齐/分类 | 全文；实际DB/间接调用与结构/Schema按正文分类 |
| `admin/operations/models.py` | 56 | I | R2全文 | 方法/导入/调用owner结构重核；累计R2全文；本轮不冒称全文 |
| `admin/operations/plugins.py` | 154 | I | R2全文 | 方法/导入/调用owner结构重核；累计R2全文；本轮不冒称全文 |
| `admin/operations/work.py` | 40 | I | R2全文 | 方法/导入/调用owner结构重核；累计R2全文；本轮不冒称全文 |
| `admin/permission_catalog.py` | 781 | N | 本轮补齐/分类 | 导入/await/SQL结构重核；纯协议/配置/计算或已列领域委托，无自管DB连接/提交 |
| `admin/queries.py` | 442 | X | 本轮补齐/分类 | 本轮D交叉全文442；本reviewer重核方法/调用结构 |
| `admin_api/__init__.py` | 1 | N | 本轮补齐/分类 | 导入/await/SQL结构重核；纯协议/配置/计算或已列领域委托，无自管DB连接/提交 |
| `admin_api/cli.py` | 342 | I | R1N/R2N | HTTP控制客户端；mutation委托API，不直连Bot库 |
| `admin_api/client.py` | 145 | I | R2全文 | HTTP错误/原UUID mutation消费；无SQLite连接 |
| `admin_api/http.py` | 640 | I | R1S/R2T | 全部route调用目标/错误映射重核；写委托AdminOperations/Character/身份领域，非自管事务 |
| `automation/__init__.py` | 6 | N | 本轮补齐/分类 | 导入/await/SQL结构重核；纯协议/配置/计算或已列领域委托，无自管DB连接/提交 |
| `automation/authority.py` | 61 | N | 本轮补齐/分类 | 导入/await/SQL结构重核；纯协议/配置/计算或已列领域委托，无自管DB连接/提交 |
| `automation/context.py` | 72 | F | R1全文 | 全文；实际DB/间接调用与结构/Schema按正文分类 |
| `automation/control_context.py` | 152 | F | R1全文 | 全文；实际DB/间接调用与结构/Schema按正文分类 |
| `automation/creation_key.py` | 14 | N | 本轮补齐/分类 | 导入/await/SQL结构重核；纯协议/配置/计算或已列领域委托，无自管DB连接/提交 |
| `automation/gateway.py` | 12 | N | 本轮补齐/分类 | 导入/await/SQL结构重核；纯协议/配置/计算或已列领域委托，无自管DB连接/提交 |
| `automation/models.py` | 152 | N | 本轮补齐/分类 | 导入/await/SQL结构重核；纯协议/配置/计算或已列领域委托，无自管DB连接/提交 |
| `automation/repository.py` | 1253 | F | R1全文 | 全文；实际DB/间接调用与结构/Schema按正文分类 |
| `automation/service.py` | 768 | F | 本轮补齐/分类 | 全文；实际DB/间接调用与结构/Schema按正文分类 |
| `automation/task_spec.py` | 62 | N | 本轮补齐/分类 | 导入/await/SQL结构重核；纯协议/配置/计算或已列领域委托，无自管DB连接/提交 |
| `automation/tools.py` | 481 | I | R1N/R2T | 全部await委托已全文的AutomationService；失败审计亦归原服务 |
| `automation/worker.py` | 334 | F | 本轮补齐/分类 | 全文；实际DB/间接调用与结构/Schema按正文分类 |
| `capabilities/__init__.py` | 65 | N | 本轮补齐/分类 | 导入/await/SQL结构重核；纯协议/配置/计算或已列领域委托，无自管DB连接/提交 |
| `capabilities/binding.py` | 43 | I | R1N/R2N | 动态handler执行消费者，实际持久效果在已列domain owner |
| `capabilities/catalog.py` | 124 | N | 本轮补齐/分类 | 导入/await/SQL结构重核；纯协议/配置/计算或已列领域委托，无自管DB连接/提交 |
| `capabilities/coordinator.py` | 235 | I | R1全文 | 方法/导入/调用owner结构重核；累计R1全文；本轮不冒称全文 |
| `capabilities/exposure.py` | 5 | N | 本轮补齐/分类 | 导入/await/SQL结构重核；纯协议/配置/计算或已列领域委托，无自管DB连接/提交 |
| `capabilities/invocation.py` | 173 | N | 本轮补齐/分类 | 导入/await/SQL结构重核；纯协议/配置/计算或已列领域委托，无自管DB连接/提交 |
| `capabilities/media.py` | 30 | N | 本轮补齐/分类 | 导入/await/SQL结构重核；纯协议/配置/计算或已列领域委托，无自管DB连接/提交 |
| `capabilities/models.py` | 143 | N | 本轮补齐/分类 | 导入/await/SQL结构重核；纯协议/配置/计算或已列领域委托，无自管DB连接/提交 |
| `capabilities/policy.py` | 62 | N | 本轮补齐/分类 | 导入/await/SQL结构重核；纯协议/配置/计算或已列领域委托，无自管DB连接/提交 |
| `capabilities/provider.py` | 327 | I | R1N/R2N | 执行provider/workspace委托，非第二事务owner |
| `capabilities/results.py` | 694 | I | R1N/R2T | render137起持久化分支读取；write_media_artifact/write_artifact委托真实Artifact owner；非纯渲染排除 |
| `capabilities/runtime.py` | 119 | N | 本轮补齐/分类 | 导入/await/SQL结构重核；纯协议/配置/计算或已列领域委托，无自管DB连接/提交 |
| `capabilities/validation.py` | 151 | N | 本轮补齐/分类 | 导入/await/SQL结构重核；纯协议/配置/计算或已列领域委托，无自管DB连接/提交 |
| `character/__init__.py` | 1 | N | 本轮补齐/分类 | 导入/await/SQL结构重核；纯协议/配置/计算或已列领域委托，无自管DB连接/提交 |
| `character/db_models.py` | 38 | F | 本轮补齐/分类 | 全文；实际DB/间接调用与结构/Schema按正文分类 |
| `character/repository.py` | 265 | F | R1全文 | 全文；实际DB/间接调用与结构/Schema按正文分类 |
| `character/soul.py` | 354 | F | R1全文 | 全文；实际DB/间接调用与结构/Schema按正文分类 |
| `client_api/__init__.py` | 1 | N | 本轮补齐/分类 | 导入/await/SQL结构重核；纯协议/配置/计算或已列领域委托，无自管DB连接/提交 |
| `client_api/db_models.py` | 40 | F | 本轮补齐/分类 | 全文；实际DB/间接调用与结构/Schema按正文分类 |
| `client_api/errors.py` | 10 | N | 本轮补齐/分类 | 导入/await/SQL结构重核；纯协议/配置/计算或已列领域委托，无自管DB连接/提交 |
| `client_api/files.py` | 143 | F | 本轮补齐/分类 | 全文；实际DB/间接调用与结构/Schema按正文分类 |
| `client_api/http.py` | 324 | I | R1S/R2T | 全部route委托个人API服务；错误边界复核，无新writer |
| `client_api/http_errors.py` | 67 | F | R1全文 | 全文；实际DB/间接调用与结构/Schema按正文分类 |
| `client_api/identity.py` | 735 | M | R1全文 | 全部类方法103–735完整，含_code/pair/transfer/claim/cleanup与replay；61–102身份值类型；顶层hash helper结构 |
| `client_api/messages.py` | 145 | F | R2全文 | 全文；实际DB/间接调用与结构/Schema按正文分类 |
| `client_api/models.py` | 111 | N | 本轮补齐/分类 | 导入/await/SQL结构重核；纯协议/配置/计算或已列领域委托，无自管DB连接/提交 |
| `client_api/openapi.py` | 30 | N | 本轮补齐/分类 | 导入/await/SQL结构重核；纯协议/配置/计算或已列领域委托，无自管DB连接/提交 |
| `client_api/prompts.py` | 295 | F | R1全文 | 全文；实际DB/间接调用与结构/Schema按正文分类 |
| `client_api/requests.py` | 265 | I | R2全文 | 方法/导入/调用owner结构重核；累计R2全文；本轮不冒称全文 |
| `client_api/sessions.py` | 115 | F | R1全文 | 全文；实际DB/间接调用与结构/Schema按正文分类 |
| `client_api/version.py` | 3 | N | 本轮补齐/分类 | 导入/await/SQL结构重核；纯协议/配置/计算或已列领域委托，无自管DB连接/提交 |
| `client_api/views.py` | 331 | X | 本轮补齐/分类 | 本轮D交叉全文331；本reviewer核SSE/hint与只读目标 |
| `client_api/works.py` | 575 | M | 本轮补齐/分类 | 333–575全部写/receipt方法完整；1–331 GET由本轮D交叉全文 |
| `client_sdk/__init__.py` | 6 | N | 本轮补齐/分类 | 导入/await/SQL结构重核；纯协议/配置/计算或已列领域委托，无自管DB连接/提交 |
| `client_sdk/cli.py` | 196 | I | R1S/R2S | 本地PersonalClient/Outbox构造与命令委托；非Bot主库 |
| `client_sdk/client.py` | 435 | M | 本轮补齐/分类 | submit 283–347、refresh349–406完整；_bind及prepare/resolve累计R2具名范围；其余Outbox/HTTP调用结构 |
| `client_sdk/outbox.py` | 122 | F | R1全文 | 全文；实际DB/间接调用与结构/Schema按正文分类 |
| `conversation/__init__.py` | 1 | N | 本轮补齐/分类 | 导入/await/SQL结构重核；纯协议/配置/计算或已列领域委托，无自管DB连接/提交 |
| `conversation/autonomy_binding.py` | 162 | N | 本轮补齐/分类 | 导入/await/SQL结构重核；纯协议/配置/计算或已列领域委托，无自管DB连接/提交 |
| `conversation/autonomy_db_models.py` | 110 | F | 本轮补齐/分类 | 全文；实际DB/间接调用与结构/Schema按正文分类 |
| `conversation/autonomy_repository.py` | 584 | I | R1全文 | 方法/导入/调用owner结构重核；累计R1全文；本轮不冒称全文 |
| `conversation/canonical_db_models.py` | 722 | M | 本轮补齐/分类 | 全部installer完整：92–127、262–274、329–341、391–404、447–460、508–521、710–722；SQL常量canonical_schema全文；ORM字段S |
| `conversation/canonical_schema.py` | 176 | F | 本轮补齐/分类 | 全文；实际DB/间接调用与结构/Schema按正文分类 |
| `conversation/correlation.py` | 115 | I | R2全文 | 方法/导入/调用owner结构重核；累计R2全文；本轮不冒称全文 |
| `conversation/event_entry.py` | 29 | I | R2全文 | 方法/导入/调用owner结构重核；累计R2全文；本轮不冒称全文 |
| `conversation/features.py` | 131 | I | R2全文 | 方法/导入/调用owner结构重核；累计R2全文；本轮不冒称全文 |
| `conversation/frozen_fragments.py` | 359 | N | 本轮补齐/分类 | 导入/await/SQL结构重核；纯协议/配置/计算或已列领域委托，无自管DB连接/提交 |
| `conversation/hydrate.py` | 434 | I | R1全文 | 方法/导入/调用owner结构重核；累计R1全文；本轮不冒称全文 |
| `conversation/initiative_sources.py` | 58 | I | R2全文 | 方法/导入/调用owner结构重核；累计R2全文；本轮不冒称全文 |
| `conversation/media_index.py` | 135 | I | R1全文 | 方法/导入/调用owner结构重核；累计R1全文；本轮不冒称全文 |
| `conversation/media_service.py` | 1186 | M | 本轮补齐/分类 | 全部直接DB方法：resolve_media_file146–162、_cache_event407–450、_sources452–522、_cache_original661–752、authorized_path754–781、_expire_page988–1029、_gc_cache_page1031–1170；余文件/网络助手结构 |
| `conversation/observation_models.py` | 75 | F | 本轮补齐/分类 | 全文；实际DB/间接调用与结构/Schema按正文分类 |
| `conversation/observation_schema.py` | 131 | F | 本轮补齐/分类 | 全文；实际DB/间接调用与结构/Schema按正文分类 |
| `conversation/observations.py` | 134 | I | R1全文 | 方法/导入/调用owner结构重核；累计R1全文；本轮不冒称全文 |
| `conversation/ordinary_admission.py` | 490 | M | R1全文 | get/admitted_event_ids/current/current_admissions/current_sources/_current_rows 113–245完整；用于新main读链；其余方法累计R1全文 |
| `conversation/ordinary_admission_db_models.py` | 36 | F | 本轮补齐/分类 | 全文；实际DB/间接调用与结构/Schema按正文分类 |
| `conversation/participation.py` | 318 | N | 本轮补齐/分类 | 导入/await/SQL结构重核；纯协议/配置/计算或已列领域委托，无自管DB连接/提交 |
| `conversation/projection_chunks_schema.py` | 80 | F | 本轮补齐/分类 | 全文；实际DB/间接调用与结构/Schema按正文分类 |
| `conversation/projection_compact_schema.py` | 209 | F | 本轮补齐/分类 | 全文；实际DB/间接调用与结构/Schema按正文分类 |
| `conversation/projection_current_schema.py` | 268 | S | 本轮补齐/分类 | Schema/ORM/SQL结构；安装链见R3A06；非运行时自管writer，未称字段全文/实际等价性通过 |
| `conversation/projection_history_schema.py` | 220 | S | 本轮补齐/分类 | Schema/ORM/SQL结构；安装链见R3A06；非运行时自管writer，未称字段全文/实际等价性通过 |
| `conversation/projection_material_schema.py` | 268 | S | 本轮补齐/分类 | Schema/ORM/SQL结构；安装链见R3A06；非运行时自管writer，未称字段全文/实际等价性通过 |
| `conversation/projection_models.py` | 82 | F | 本轮补齐/分类 | 全文；实际DB/间接调用与结构/Schema按正文分类 |
| `conversation/projection_revision_schema.py` | 85 | F | 本轮补齐/分类 | 全文；实际DB/间接调用与结构/Schema按正文分类 |
| `conversation/projection_schema.py` | 58 | F | 本轮补齐/分类 | 全文；实际DB/间接调用与结构/Schema按正文分类 |
| `conversation/projections.py` | 650 | F | 本轮补齐/分类 | 全文；实际DB/间接调用与结构/Schema按正文分类 |
| `conversation/prompt_history.py` | 317 | F | 本轮补齐/分类 | 全文；实际DB/间接调用与结构/Schema按正文分类 |
| `conversation/scope.py` | 118 | F | 本轮补齐/分类 | 全文；实际DB/间接调用与结构/Schema按正文分类 |
| `conversation/scope_models.py` | 50 | F | 本轮补齐/分类 | 全文；实际DB/间接调用与结构/Schema按正文分类 |
| `conversation/scope_repository.py` | 300 | I | R1全文 | 方法/导入/调用owner结构重核；累计R1全文；本轮不冒称全文 |
| `conversation/self_initiative.py` | 51 | I | R1全文 | 方法/导入/调用owner结构重核；累计R1全文；本轮不冒称全文 |
| `conversation/uploads.py` | 149 | F | R1全文 | 本轮全文；media lock→rename→writer原receipt，finally仅清原临时文件 |
| `deployment_setup/__init__.py` | 5 | N | 本轮补齐/分类 | 导入/await/SQL结构重核；纯协议/配置/计算或已列领域委托，无自管DB连接/提交 |
| `deployment_setup/command.py` | 934 | I | R1S/R2S | apply_pending_plugins/load_plugin_setup_states调用；不直接SQLite；对应服务本轮D补齐 |
| `deployment_setup/environment_check.py` | 40 | N | 本轮补齐/分类 | 导入/await/SQL结构重核；纯协议/配置/计算或已列领域委托，无自管DB连接/提交 |
| `deployment_setup/service.py` | 970 | X | 本轮补齐/分类 | 本轮D交叉具名全读apply_pending_plugins/_bootstrap_pending_plugins/load_plugin_setup_states；本reviewer核592起锁/在线委托与文件链 |
| `deployment_setup/terminal.py` | 168 | N | 本轮补齐/分类 | 导入/await/SQL结构重核；纯协议/配置/计算或已列领域委托，无自管DB连接/提交 |
| `emoji/__init__.py` | 1 | N | 本轮补齐/分类 | 导入/await/SQL结构重核；纯协议/配置/计算或已列领域委托，无自管DB连接/提交 |
| `emoji/delivery.py` | 355 | I | R1全文 | 方法/导入/调用owner结构重核；累计R1全文；本轮不冒称全文 |
| `emoji/library.py` | 342 | M | R1全文 | publish197–234、describe236–342完整；末尾_index在模型后、独立manifest发布；其余累计R1全文 |
| `emoji/media.py` | 151 | N | 本轮补齐/分类 | 导入/await/SQL结构重核；纯协议/配置/计算或已列领域委托，无自管DB连接/提交 |
| `emoji/models.py` | 27 | N | 本轮补齐/分类 | 导入/await/SQL结构重核；纯协议/配置/计算或已列领域委托，无自管DB连接/提交 |
| `emoji/sticker_index.py` | 543 | M | R1全文 | cache_writer/valid_schema/cache/ready/_cache_batch/rebuild/discard_cache/search/_search_ready完整；126–268、354–500；非DB图片/JSON助手累计R1全文 |
| `gateway/__init__.py` | 23 | N | 本轮补齐/分类 | 导入/await/SQL结构重核；纯协议/配置/计算或已列领域委托，无自管DB连接/提交 |
| `gateway/activity.py` | 11 | N | 本轮补齐/分类 | 导入/await/SQL结构重核；纯协议/配置/计算或已列领域委托，无自管DB连接/提交 |
| `gateway/compatibility.py` | 66 | N | 本轮补齐/分类 | 导入/await/SQL结构重核；纯协议/配置/计算或已列领域委托，无自管DB连接/提交 |
| `gateway/local_clients.py` | 7 | N | 本轮补齐/分类 | 导入/await/SQL结构重核；纯协议/配置/计算或已列领域委托，无自管DB连接/提交 |
| `gateway/media.py` | 30 | N | 本轮补齐/分类 | 导入/await/SQL结构重核；纯协议/配置/计算或已列领域委托，无自管DB连接/提交 |
| `gateway/models.py` | 44 | N | 本轮补齐/分类 | 导入/await/SQL结构重核；纯协议/配置/计算或已列领域委托，无自管DB连接/提交 |
| `gateway/provider.py` | 145 | N | 本轮补齐/分类 | 导入/await/SQL结构重核；纯协议/配置/计算或已列领域委托，无自管DB连接/提交 |
| `gateway/providers/__init__.py` | 36 | N | 本轮补齐/分类 | 导入/await/SQL结构重核；纯协议/配置/计算或已列领域委托，无自管DB连接/提交 |
| `gateway/providers/app/__init__.py` | 45 | N | 本轮补齐/分类 | 导入/await/SQL结构重核；纯协议/配置/计算或已列领域委托，无自管DB连接/提交 |
| `gateway/providers/app/ingress.py` | 262 | F | 本轮补齐/分类 | 本轮全文；AppSender Social委托、start/receive读、Processor回调与finish任务收拢 |
| `gateway/providers/app/social.py` | 27 | N | 本轮补齐/分类 | 导入/await/SQL结构重核；纯协议/配置/计算或已列领域委托，无自管DB连接/提交 |
| `gateway/providers/feishu.py` | 279 | N | 本轮补齐/分类 | 导入/await/SQL结构重核；纯协议/配置/计算或已列领域委托，无自管DB连接/提交 |
| `gateway/providers/feishu_ingress.py` | 406 | I | R1S/R2T | 方法/导入/调用owner结构重核；累计前轮具名方法范围：300-406；start/receive全部真实DB与network边界；本轮不冒称全文 |
| `gateway/providers/feishu_protocol.py` | 55 | N | 本轮补齐/分类 | 导入/await/SQL结构重核；纯协议/配置/计算或已列领域委托，无自管DB连接/提交 |
| `gateway/providers/feishu_social.py` | 198 | N | 本轮补齐/分类 | 导入/await/SQL结构重核；纯协议/配置/计算或已列领域委托，无自管DB连接/提交 |
| `gateway/providers/feishu_ws.py` | 231 | N | 本轮补齐/分类 | 导入/await/SQL结构重核；纯协议/配置/计算或已列领域委托，无自管DB连接/提交 |
| `gateway/providers/snowluma.py` | 63 | N | 本轮补齐/分类 | 导入/await/SQL结构重核；纯协议/配置/计算或已列领域委托，无自管DB连接/提交 |
| `gateway/providers/telegram.py` | 183 | N | 本轮补齐/分类 | 导入/await/SQL结构重核；纯协议/配置/计算或已列领域委托，无自管DB连接/提交 |
| `gateway/providers/telegram_activity.py` | 28 | N | 本轮补齐/分类 | 导入/await/SQL结构重核；纯协议/配置/计算或已列领域委托，无自管DB连接/提交 |
| `gateway/providers/telegram_ingress.py` | 505 | I | R1S/R2T | 方法/导入/调用owner结构重核；累计前轮具名方法范围：338-505；start/receive全部真实DB与network边界；本轮不冒称全文 |
| `gateway/providers/telegram_social.py` | 126 | N | 本轮补齐/分类 | 导入/await/SQL结构重核；纯协议/配置/计算或已列领域委托，无自管DB连接/提交 |
| `gateway/registry.py` | 449 | N | 本轮补齐/分类 | 导入/await/SQL结构重核；纯协议/配置/计算或已列领域委托，无自管DB连接/提交 |
| `gateway/social.py` | 126 | N | 本轮补齐/分类 | 导入/await/SQL结构重核；纯协议/配置/计算或已列领域委托，无自管DB连接/提交 |
| `identity/__init__.py` | 1 | N | 本轮补齐/分类 | 导入/await/SQL结构重核；纯协议/配置/计算或已列领域委托，无自管DB连接/提交 |
| `identity/authority.py` | 26 | F | 本轮补齐/分类 | 全文；实际DB/间接调用与结构/Schema按正文分类 |
| `identity/bootstrap.py` | 28 | I | R1全文 | 方法/导入/调用owner结构重核；累计R1全文；本轮不冒称全文 |
| `identity/canonical_repository.py` | 395 | F | R1全文 | 全文；实际DB/间接调用与结构/Schema按正文分类 |
| `identity/canonical_uow.py` | 663 | M | R1全文 | 91–663：record_account_fact/append_inbound/append_new_generation及全部DB辅助方法完整；头部声明结构 |
| `identity/db_models.py` | 347 | S | 本轮补齐/分类 | Schema/ORM/SQL结构；安装链见R3A06；非运行时自管writer，未称字段全文/实际等价性通过 |
| `identity/errors.py` | 11 | N | 本轮补齐/分类 | 导入/await/SQL结构重核；纯协议/配置/计算或已列领域委托，无自管DB连接/提交 |
| `identity/event_author.py` | 117 | I | R2全文 | 方法/导入/调用owner结构重核；累计R2全文；本轮不冒称全文 |
| `identity/evidence.py` | 31 | I | R2全文 | 方法/导入/调用owner结构重核；累计R2全文；本轮不冒称全文 |
| `identity/ingress.py` | 693 | M | R1全文 | 139–693 pre_admit/_admit/resolve_canonical_reply/同平台账号/_author_for/_ensure_person_id/_hydrate_for_message完整；100–138辅助构造；头部声明结构 |
| `identity/memory_guard.py` | 45 | I | R1全文 | 方法/导入/调用owner结构重核；累计R1全文；本轮不冒称全文 |
| `identity/mentions.py` | 131 | I | R2全文 | 方法/导入/调用owner结构重核；累计R2全文；本轮不冒称全文 |
| `identity/ownership.py` | 56 | N | 本轮补齐/分类 | 导入/await/SQL结构重核；纯协议/配置/计算或已列领域委托，无自管DB连接/提交 |
| `identity/ownership_interval.py` | 139 | I | R2全文 | 方法/导入/调用owner结构重核；累计R2全文；本轮不冒称全文 |
| `identity/person_identity.py` | 355 | F | R1全文 | 全文；实际DB/间接调用与结构/Schema按正文分类 |
| `identity/person_state.py` | 732 | F | 本轮补齐/分类 | 全文；实际DB/间接调用与结构/Schema按正文分类 |
| `identity/readers.py` | 43 | I | R1全文 | 方法/导入/调用owner结构重核；累计R1全文；本轮不冒称全文 |
| `identity/receipt_compat.py` | 204 | I | R2全文 | 方法/导入/调用owner结构重核；累计R2全文；本轮不冒称全文 |
| `identity/routing.py` | 1536 | M | 本轮补齐/分类 | 128–1536全部查询/准备/transition/provision/monitor与观察辅助方法完整；1–127声明/构造结构 |
| `identity/space_metadata.py` | 57 | I | R1全文 | 方法/导入/调用owner结构重核；累计R1全文；本轮不冒称全文 |
| `identity/sql_constraints.py` | 23 | S | R2全文 | Schema/ORM/SQL结构；安装链见R3A06；非运行时自管writer，未称字段全文/实际等价性通过 |
| `identity/write_settings.py` | 51 | I | R1全文 | 方法/导入/调用owner结构重核；累计R1全文；本轮不冒称全文 |
| `persistence/__init__.py` | 1 | N | 本轮补齐/分类 | 导入/await/SQL结构重核；纯协议/配置/计算或已列领域委托，无自管DB连接/提交 |
| `persistence/artifact_refs.py` | 29 | F | R1全文 | 全文；实际DB/间接调用与结构/Schema按正文分类 |
| `persistence/command_receipts.py` | 827 | I | R1全文 | 方法/导入/调用owner结构重核；累计R1全文；本轮不冒称全文 |
| `persistence/commit_hints.py` | 39 | F | R1全文 | 全文；实际DB/间接调用与结构/Schema按正文分类 |
| `persistence/database.py` | 245 | F | R1全文 | 全文；实际DB/间接调用与结构/Schema按正文分类 |
| `persistence/diagnostic_writer.py` | 227 | F | R1全文 | 全文；实际DB/间接调用与结构/Schema按正文分类 |
| `persistence/event_repository.py` | 986 | X | 本轮补齐/分类 | 本轮D交叉全文986；全部只读/interrupt/derived委托；本reviewer结构 |
| `persistence/event_view.py` | 66 | F | 本轮补齐/分类 | 全文；实际DB/间接调用与结构/Schema按正文分类 |
| `persistence/history_search.py` | 174 | N | 本轮补齐/分类 | 导入/await/SQL结构重核；纯协议/配置/计算或已列领域委托，无自管DB连接/提交 |
| `persistence/history_search_index.py` | 97 | F | R1全文 | 全文；实际DB/间接调用与结构/Schema按正文分类 |
| `persistence/instance_lock.py` | 94 | I | R1全文 | 方法/导入/调用owner结构重核；累计R1全文；本轮不冒称全文 |
| `persistence/job_claims.py` | 62 | F | R1全文 | 全文；实际DB/间接调用与结构/Schema按正文分类 |
| `persistence/media_repository.py` | 243 | F | R1全文 | 全文；实际DB/间接调用与结构/Schema按正文分类 |
| `persistence/metadata.py` | 37 | F | R1全文 | 全文；实际DB/间接调用与结构/Schema按正文分类 |
| `persistence/models.py` | 1844 | M | 本轮补齐/分类 | _install_artifact_references321–323完整，artifact_refs常量全文；ChatEvent/refs270–323字段核；其余ORM字段S |
| `persistence/people_repository.py` | 1736 | F | 本轮补齐/分类 | 全文；实际DB/间接调用与结构/Schema按正文分类 |
| `persistence/repositories.py` | 33 | F | 本轮补齐/分类 | 全文；实际DB/间接调用与结构/Schema按正文分类 |
| `persistence/repository_helpers.py` | 142 | I | R1全文 | 方法/导入/调用owner结构重核；累计R1全文；本轮不冒称全文 |
| `persistence/repository_records.py` | 183 | N | 本轮补齐/分类 | 导入/await/SQL结构重核；纯协议/配置/计算或已列领域委托，无自管DB连接/提交 |
| `persistence/schema_guard.py` | 337 | S | R2全文 | Schema/ORM/SQL结构；安装链见R3A06；非运行时自管writer，未称字段全文/实际等价性通过 |
| `persistence/scoped_event_uow.py` | 1074 | M | R1全文 | 99–1074全部append/external/edit/derived/receipt/causal/reuse/_append_canonical完整；80–98 token/构造；头部类型结构 |
| `persistence/sqlite_diagnostics.py` | 349 | F | R2全文 | 全文；实际DB/间接调用与结构/Schema按正文分类 |
| `persistence/unit_of_work.py` | 59 | F | R1全文 | 全文；实际DB/间接调用与结构/Schema按正文分类 |
| `persistence/web_repository.py` | 50 | F | R1全文 | 全文；实际DB/间接调用与结构/Schema按正文分类 |
| `persistence/worker_recovery.py` | 24 | I | R1全文 | 方法/导入/调用owner结构重核；累计R1全文；本轮不冒称全文 |
| `web/__init__.py` | 19 | N | 本轮补齐/分类 | 导入/await/SQL结构重核；纯协议/配置/计算或已列领域委托，无自管DB连接/提交 |
| `web/base.py` | 95 | N | 本轮补齐/分类 | 导入/await/SQL结构重核；纯协议/配置/计算或已列领域委托，无自管DB连接/提交 |
| `web/bridge_state.py` | 63 | F | R1全文 | 全文；实际DB/间接调用与结构/Schema按正文分类 |
| `web/http_transport.py` | 304 | N | 本轮补齐/分类 | 导入/await/SQL结构重核；纯协议/配置/计算或已列领域委托，无自管DB连接/提交 |
| `web/models.py` | 64 | N | 本轮补齐/分类 | 导入/await/SQL结构重核；纯协议/配置/计算或已列领域委托，无自管DB连接/提交 |
| `web/native_bridge.py` | 337 | I | R1S/R2T | BridgeState cache与模型/HTTP委托；累计R2 77–98/252–311具名范围 |
| `web/native_sources.py` | 89 | N | 本轮补齐/分类 | 导入/await/SQL结构重核；纯协议/配置/计算或已列领域委托，无自管DB连接/提交 |
| `web/page_extractor.py` | 117 | I | R1N/R2N | BridgeState.access经to_thread；网络/正文提取在cache事务外，非纯无存储文件 |
| `web/route_context.py` | 22 | N | 本轮补齐/分类 | 导入/await/SQL结构重核；纯协议/配置/计算或已列领域委托，无自管DB连接/提交 |
| `web/search_config.py` | 54 | N | 本轮补齐/分类 | 导入/await/SQL结构重核；纯协议/配置/计算或已列领域委托，无自管DB连接/提交 |
| `web/tavily.py` | 270 | N | 本轮补齐/分类 | 导入/await/SQL结构重核；纯协议/配置/计算或已列领域委托，无自管DB连接/提交 |
| `workspace/__init__.py` | 1 | N | 本轮补齐/分类 | 导入/await/SQL结构重核；纯协议/配置/计算或已列领域委托，无自管DB连接/提交 |
| `workspace/files.py` | 170 | F | 本轮补齐/分类 | 全文；实际DB/间接调用与结构/Schema按正文分类 |
| `workspace/inspect.py` | 139 | F | 本轮补齐/分类 | 全文；实际DB/间接调用与结构/Schema按正文分类 |
| `workspace/service.py` | 244 | I | R1全文 | 执行器委托FileWorkspace/WorkspaceStore/Inspector；累计R1全文，本轮锁/调用结构 |
| `workspace/short_state.py` | 109 | F | R1全文 | 全文；实际DB/间接调用与结构/Schema按正文分类 |
| `workspace/store.py` | 284 | F | R1全文 | 全文；实际DB/间接调用与结构/Schema按正文分类 |
| `workspace/tools.py` | 65 | N | 本轮补齐/分类 | 导入/await/SQL结构重核；纯协议/配置/计算或已列领域委托，无自管DB连接/提交 |

