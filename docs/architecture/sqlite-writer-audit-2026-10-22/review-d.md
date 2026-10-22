# D 域：装配、资源生命周期、Schema 与离线工具

第1、2轮基线：`cdb49eadc0c7cd16060bf0329234ae69611e62bd`；第3轮与最终清单基线：
`c2ce7da66ab54efe192993b9615df48266a62137`。审查只修改文档与本目录的静态取证脚本。
当前线上延迟按用户反馈可接受；本轮研究架构收敛与维护成本，不把它改写成新的延迟事故。

## 第 1 轮：完整资源面与实际调用

### 范围与阅读口径

- `surface.json` 包含全部 647 个源码文件，先分类，数据库实现及间接调用再进入语义审查。
- D 域为 110 个源码文件，其中 24 个有直接数据库静态信号。纯 Provider/提示渲染/类型定义只作结构排除，不声称逐字审查。
- 本轮全文核查：`application/lifecycle.py`、`main.py`、`health.py`、`cli.py`、`operations/reset_conversations.py`、`plugins/ai_chat/metadata.py`、`tool_results/artifacts.py`、`tool_results/recorder.py`；应用模块的 persistence/model_runtime/conversation/admin/admin_api/plugins/media/automation/web；`container.py` 的装配、create/start/close/维护与注册顺序。
- `codemode/driver.py` 按所有导入、数据库异常处理、effect dispatch/commit 调用审查；工具执行本体仍由原 Work/effect 负责，没有另开 SQLite 引擎。其纯脚本解析与渲染部分不当成数据库实现。
- `config.py`、`settings_domains.py` 按数据库 URL、各独立 SQLite 路径、存储容量与 lifecycle 配置核查；其余配置不纳入 writer 改造。
- 所有 97 个 revision 和 `migrations/env.py` 进入 `schema.json`，逐项核对 revision 链、DDL/DML、运行时代码导入、触发器和执行环境。冻结历史转换不改成运行时 writer，也不借本轮重写历史业务转换算法。当前 head 是 **0147**，不是因 0145 有特殊转换就把 0145 写成 head。
- `schema.json` 的 217 个定义是 ORM/Core/SQL schema 定义出现次数，含冻结旧版本与重复定义，不是当前 217 张业务表。现行 Schema、触发器和运行时表的调用由 A/B/C 域对应模块复核。
- 操作脚本逐个核对连接方式和目标：`evaluate_search_memory_gold.py`、`prepare_person_authority.py`、`release_smoke.py`、`retire_media_snapshots.py`、`verify_monty_packaging.py`、`verify_work_backup.py`、`start.sh`、`tools/cache_probe_materials.py`、`tools/rehearse_current_material_migration.py`。实验报告 JSON 是历史数据，不能列作运行时 writer。

### D01：资源 owner 与事务 API 需要一起收敛

`ApplicationContainer.create()` 建立一个 Database；`PersistenceModule.build()` 注入同一实例；模型诊断、Memory、插件、媒体、Work、工具 Artifact 都复用它。构造器独立调用、测试 fixture、CLI 另一个进程不在同一内存队列。

最终把公开 `sessions` factory 收为持久化层内部 `_sessions`，正常代码使用 `read_session()` 与 `immediate_session()`。前者只管理自己创建的读 session，`autoflush=False`、不自动提交；后者保留 writer 的正常 flush 语义。传入的同事务 session 不另改配置、不另开连接。只读入口不是 SQL 拦截器，不加防御性 Session 子类。

Python 3.12 SQLite legacy 模式下，SQLAlchemy autobegin 不等于数据库多语句一致读。已有显式 `BEGIN` 的只读快照按真实需要保留；不让每个单 SELECT 都多一次 BEGIN 往返。

### D02：启动与关闭是事务所有权的一部分

`main.bootstrap()` 先取得 SQLiteApplicationLock，再进行 schema guard 与 ApplicationContainer 创建；schema guard 使用临时 NullPool 引擎只查 Schema，发生在 Bot writer 启动前。

LifecycleRegistry 按注册顺序启动、逆序关闭。Database 先注册，DiagnosticWriter 后注册，业务 worker 再注册；因此正常关闭应先停止业务产生者，再完成剩余必要状态写、关闭模型运行、排空诊断，最后 dispose Database。插件停止回调、应用停止事件仍可能产生写入，不能提前关闭公共 writer。

排队前取消可退出；已开始的驱动操作必须真实结束。关闭时的数据库准入停止接纳时点，需要与最后一个真实写入产生者退出对齐。不能一进 `ApplicationContainer.close()` 就拒绝所有恢复/插件收尾写入。

`healthz`/`Database.ping()` 的 SELECT 1 只证明读取可用。若 writer 物理退出无法确认，不能仍靠 SELECT 1 报数据库写入健康；复用现有生命周期错误事实，健康中区分读可用与写入已停止。无需新的持久故障表。

### D03：工具回执是主库 writer，不是可丢诊断

`ToolReceiptRepository.record_receipt()` 在只读阶段解析来源、owner、privacy 和 SELF 执行，再在 `recorder.py:339` 的裸 begin 执行条件 INSERT。迁移成短 writer，保留 INSERT SELECT 的当前来源谓词和 `source_call_key` 唯一性；该方法可从原工具结果补记，但不能借重试重调工具。

`audit_source`、source events、SELF 无事件身份、privacy generation 都是实际证据。不能把此回执转入 DiagnosticWriter 的可丢队列；来源校验也不能因统一 writer 消失。

### D04：Artifact 文件锁与 writer 顺序

`ToolArtifactRepository.write_artifact()`：编码/hash 在外 → storage lock → 文件 publish/fsync → 短 writer 保存 metadata。`cleanup()` 同锁下先查候选 → writer 标记 deleting → writer 外 unlink → writer 删除 metadata。`add_refs/release_refs` 复用调用者事务，不再次拿 storage lock。

最终保持 **artifact storage lock → writer**，禁止 writer → storage lock。metadata 提交不明时不能删除可能已经被引用的文件；沿原 UUID 文件和孤儿维护处理。保留已付 Work、observation、summary 和 work_note 的引用关系。

Artifact 的保护谓词有 Work ancestors/ref 查询，性能测试必须包含非空任务树和足量引用，不能只测空表。纯读取后的文件内容/来源复核不需要 writer。

### D05：独立 SQLite 已有合适的 owner，不能合并

锁定参与库为 `438f204f9ebe2b8c1b698b68c0f1b3ac8b4facef`；已核本地安装 `direct_url.json` 与 `yuki_participation/store.py`。其 SnapshotStore 是独立 participation.sqlite3，CAS revision，WAL，`BEGIN IMMEDIATE`。

宿主 `services/participation_snapshot.py` 已有 **单专用线程** AsyncSnapshotStore：创建、load/save、close 同一线程；取消不取消正在执行的函数，close 排在既有工作后。这是应保留的已有设计，不能把同步库改成随意 `to_thread` 池，也不能让其排队阻塞主业务库 writer。跨库反馈仍按原主库 receipt/seq 与 snapshot revision 推进，不伪造跨库原子提交。

其他独立资源由 A/C 域列出：sticker search/index、workspace/short state、web bridge、client Outbox/ACP、sandbox jobs/manager。每个独立数据库都要记录是否缓存、是否重建、运行在哪个进程，以及真实事实保存在哪里。

### D06：CLI / 维护 / 升级与在线管理分流

`cli._plugin_command()` 的在线状态修改已经用现有 Admin API；保留这个单进程业务 owner，不让 CLI 直接连接主库并发修改。

`_runtime_diagnostics → _runtime_snapshot → RuntimeConfigService.initialize` 经 A 域复核 **只读**：initialize 加载配置并更新进程内 restart cache，不是首次创建业务行。这个初始疑点已排除，不能写成实际裸 writer。

init-db、hygiene apply/rebuild-fts、reset_all 和数据迁移是离线维护。明确它们原有的停写前提与是否取得现有 application lock，补齐入口和操作文档；不声称进程内队列覆盖它们，不新增运行时审批、持久 maintenance 状态机或无关门槛。

`scripts/start.sh` 先 init-db 再 exec Bot。其当前先迁移、Bot 后取得应用锁的顺序需要在交付设计中列明，避免误开第二容器先改库；沿现有部署停旧 Bot/维护流程处理。若未来把现有互斥覆盖到 init-db，应统一已有锁的作用域，避免 setup 先持锁再嵌套 init-db 自锁。

### D07：Schema 的隐式写放大不能漏

FTS、projection invalidation、quota/usage、artifact refs、外键 cascade 都在原事务内产生效果。调用一次 UPDATE 不等于只修改一行。必须保触发器语义，同时将其 SQL/页写入算在 writer 占用中；不能为了短事务关闭 foreign_keys、删除现行触发器或破坏 erasure/reset 的原子性。

head 0147 与 create_schema()/schema_guard 的等价性、文件型 WAL 测试和冻结 schema 的导入应一起核对。最终若只有事务组织变化，就不新增 migration；只有真实表/索引变化才新增 revision，不能重写已发布迁移。

### D08：测试与诊断的覆盖口径

397/398 个测试候选含 fixtures、迁移测试、数据库性能和恢复测试，不表示这些测试已经运行，也不表示每份都逐字读取。按真实行为映射已有测试，再只补缺失的全局竞争/取消/复核场景。

测试 fixture 可由内部测试 helper 构造原始 session/多独立连接来复现旧路径和外部 writer；不能以“所有源码不用 raw factory”为由删掉真实竞争测试。运行时路径不再依赖旧入口以后，才删冻结旧 wrapper 的测试。

新增诊断只用内存聚合和现有健康出口；不得每次测 writer 等待又同步写一条诊断。诊断统计既不能污染性能样本，也不能把抽样 slow/failure 记录当全部业务的延迟分布。

### 第 1 轮待写入主任务书

1. 补全部数据库资源和进程拓扑，以及现有 AsyncSnapshotStore。
2. 收敛公开 factory、读入口、一致快照和 writer 内传 session；调用者按真实写行为分类。
3. 启动/关闭/失败清理流程、读写健康区分。
4. 补工具回执、Artifact/storage/media/protocol/config 锁序与独立 DB。
5. 原子单位包含 SQL 触发器/外键效果、跨库反馈和平台已发后的本地补记。
6. 细化 schema/离线脚本/打包测试范围，保当前 0147 与只读命令实际行为。

以上为第1轮形成时的待办，后两轮的核查与最终裁决见下文。

## 第 2 轮：反例、语义与全局消费者

### D09：第一轮的推断按调用事实修正

- 配置initialize的候选已核为只读，保留正常临时读取；不制造停机或writer门禁。
- Canonical/scoped supplied-session入口允许缺prepared_terms，但现有5个明确生产调用已预备分词。这是接口/未来调用风险，不是已证实的现行漏传热点；不能重复前移已经在外的工作。
- Memory selector定位完成后已改成fact_id。完整集合重核只针对提交实际依赖的来源/证据/owner/absence，不让另一条后来出现的同key事实阻塞原ID操作。
- RuntimeConfig的_mutation_session本身没有新增config mutex；锁表改为核现有实际边，不凭名称发明一把config锁。
- Work lease失效拒绝新的受保护执行，不等于真实外部效果没有发生；公共层不新增“过期就拒存全部receipt”规则。

### D10：取消与通知的层次不同

全文补读 `test_sqlite_physical_exit.py`、`test_sqlite_failure_classification.py`、`commit_hints.py`。
pool invalidate/close事件可以先于真实驱动结束；forced termination队列future完成前，另一物理连接仍可能BUSY。
现有诊断记录“观测结束但释放未知”是正确事实，不能把它当作新writer放行依据。

`AsyncSnapshotStore._run` 的shield只使worker继续；`SemanticParticipationService._save`在另层等待
真实save完成、认领实际revision后再传播取消。这两层不同，不能按“都处理取消”删掉其中一层。
关闭顺序同理：LifecycleRegistry会在一个close失败后继续关闭其他资源并聚合错误，不能从逆序注册
推断失败分支下驱动已经停止。writer owner的退出确认必须由公共DB清理本身成立。

`HintSession.after_commit` 是当前SDK/SSE订阅的内存唤醒，不能把hint当可靠账本。
第2轮独立探针补证：SAVEPOINT release也会触发该事件，此时root事务尚未提交，随后仍可rollback。
因此不能借此释放writer、认领原receipt或证明完整提交。最终全量收口后，hint直接在公共writer
确认根commit成功后发布，删除专用HintSession/after_commit监听，不再复制另一层事件过滤器。
读取session不主动commit，避免空读产生新的唤醒与反馈噪声。实际取证见round-2-a。

### D11：Schema/离线工具再分类

静态范围最初扩展到mako、shell与JSON时的中间清单为1163项：647源码、99迁移材料（97revision+env+template）、
398测试候选、14操作/配置文件、5历史实验数据。后者只有数据，没有活跃DB调用，明确排除运行时迁移。
补读start.sh，确认容器先init-db再exec Bot；schema guard正常不修改业务数据。

CI Quality使用Python3.12、锁定依赖、unit/integration与native分组；现有本地Windows取消/文件锁结果不能
替代Linux driver/OS锁验收。未改变Schema时不增加revision，head0147继续成立；若改索引需同批维护
create_schema、schema_guard、实际Alembichead和对应fixture模板，不可只在测试建库时加索引。

### D12：任务书本身的覆盖遗漏

初次扩写U01–U32仍漏掉独立Automation工作单；虽然原稿提自动任务，但不能用Work迁移替代它。
交叉审查后新增U33、W67/W68，明确定义版本、claim lease、run/occurrence settlement及worker入参until的时钟来源。
对错误恢复、savepoint、原paid结果与receipt的保留均进入具名验证，而不是一句“注意兼容”。

第2轮结论已与A/B/C域交叉核对并写入主任务书；最终清单与第3轮结果如下。

## 第3轮：全局交错、遗漏补读、删减与新main差异

### D13：最终清单不再依赖“搜到了才审”

在同一份git tracked清单上保留全部src、tests及其他Python入口，再按实际导入、调用、SQL
模板和Schema交叉分类。最终1298项：647源码、496测试/支持材料、99迁移、24操作/配置、
18历史实验代码、9扩展/示例、5历史数据。无Python解析错误。A239/B87/C211/D110源码
文件数合计647；D的27个tag是候选，包括SQL字符串或类型误命中，不是27个writer。

所有97个revision及env再次核revision/down_revision链、upgrade/downgrade的函数调用、
运行时代码导入和SQL；head仍0147，另一个mako模板也在清单。98个Python迁移文件共15050行。
冻结schema和历史转换按离线语义检查，不借重构改写历史转换业务。

217处Schema定义包括冻结旧版本、Core/ORM及触发器出现次数，不当作当前表数。
静态扫描曾漏artifact_refs拼接模板，现已补SQL模板信号；三轮语义复核仍负责判断实际用途，
不能把后补tag当成完整性证明。

### D14：D域数据库实现和消费者的第三次核查

- 重新核tool receipt原source/SELF/privacy条件、裸begin写入和Artifact各阶段；均进入主库
  owner。Artifact完整正文已在前轮分块读过，本轮按publish/refs/cleanup/protection实际方法
  重读，不把纯文本分页再称新的全文验收。原paid文件和已引用文件按真实结果保留。
- application modules全部装配入口已读；对persistence/model_runtime/plugins/conversation/
  admin/admin_api/media/automation/web的同Database注入、启动/关闭与独立资源再核。
  container保留大构造器按DB装配/生命周期/维护方法阅读的等级，没有声称1038行每轮全文。
- Codemode的四处直接reader（_admitted、_pairing内原receipt、_parent_row、_live_source）和
  T1/T2/T3/T4的Work/journal委托均归原owner；VM/模型调用在writer外。子工具unknown不能
  因新公共错误映射变成未执行，原operation_id/配对和已付原输出沿既有receipt恢复。
- ASRService.prepare/_prepare是下载、转码、Provider阶段，没有独立DB；processor随后用
  原event/generation/expected_source更新derived文本/FTS。asr/schema是0055冻结触发器，
  不把它当现行runtime writer，也不恢复被替换的历史FTS结构。
- SDK context/sessions是Host facade协议，真实KV/config/notification/session写入在C域；
  ToolResult.mutation_committed=None有实际不确定含义。testing fake_services的字典CAS和
  fake transcript不是新的SQLite，不能为全量统一而增加数据库依赖。
- domain.conversations/identity/messages的字符串SQL命中源于命名/样例/类型，未发现引擎；
  event_prompt只渲染已取到的EventRecord/引用，不查询或写DB。Provider、vision网络适配、
  prompting、纯time格式与调度计算按结构排除，保存全文件清单以便回查。

### D15：跨域累计缺口已补到真实方法

A域审查人主动指出前两轮仅结构/局部阅读的几个大查询；本轮主审接手：

| 文件 / 阅读范围 | 实际结论 |
| --- | --- |
| admin/queries.py，442行全文 | person/binding/plugin/config/operation/traces/persons/bindings/presences/works/work查询；鉴权与领域read委托，无独立业务写。payload解码在reader，不迁入writer |
| persistence/event_repository.py，986行全文 | rebuild选择/统计/分页、原ID查证、scope context/delta/before/after/missing history、query_history/history_around、历史主体与derived写委托；真实一致读BEGIN保留 |
| client_api/views.py，331行全文 | home/events/sync多表一致读，activity只读，Character.current为同session读取；events复用history reader；SSE只等原hint，15s仅keepalive不重查 |
| client_api/works.py，1–331全文 | input_result原event/mailbox/device/source校验；public_many批量查询root、budget、children和Social真实输出，get同快照纯读；后半submit/cancel由A三轮审查 |
| deployment_setup/service.py，apply_pending_plugins/_bootstrap_pending_plugins/load_plugin_setup_states具名方法全文 | 现有SQLiteApplicationLock决定offline临时Database或online Admin API；bootstrap逐repo写后close，随后删pending文件，不是Bot内第二owner；读取现有安装状态不写库 |

`_history_read`的shield→interrupt→等待真实读取结束不是BUSY重试。query_history会在reader
内分词/匹配并保同快照邻居；现有supplied-session生产调用为ClientViews、Social history、
plugin history及内部around，未发现必须把其重准备放进writer的理由。保取消语义，不在删除
diagnostic monkeypatch时误删。read_session收口应改接口归属，不能变成reader也排FIFO。

### D16：所有非src Python入口、外部文件和独立进程

完整清单中的部署3、scripts12、tools3、历史docs实验18、插件7、examples2个Python文件
均核导入/函数与连接调用；实际DB/间接写方法补正文：

- 示例echo通过SDK config/KV；subscription_monitor把HTTP读取放在DB之外，以KV CAS保存
  原pending request/cursor，Host publication按原UUID幂等后再推进状态。保先保存请求再发布
  的恢复语义，不把整次poll套writer或另加插件DB队列。
- setup、reset、hygiene、FTS rebuild、prepare_person_authority、retire_media_snapshots与
  migration rehearsal为独立离线写；在线plugin命令委托Admin API。前者必须在现有停写/
  OS锁范围内，进程内FIFO不覆盖它们。release/packaging/backup脚本的临时数据库与复制
  目标按实际生命周期处理，不把试验副本算Bot writer。
- cache probe/replay及issue267/268/269模型试验是显式实验入口，有些会调用付费HTTP；
  本轮只读代码，未执行。旧qq_ai_bot导入不作为现行runtime模块迁移。
- configure-manager只改环境文件，subscription test_feeds只测feed解析。搜索到SQL词
  不足以认定写库；历史JSON报告为数据。独立Manager jobs、SDK outbox、ACP、workspace、
  bridge、sticker和参与snapshot的资源归属不因全仓清单而合并进主库。

### D17：删除方案与异常传播终审

最终采用同协程FIFO→新短IMMEDIATE→复核/变更→确认真实退出→释放owner。长准备和独立
读取不排队；不新增actor、Plan框架、持久队列、提交journal、严格优先级或全库版本门槛。

- 公共writer已知owner后，旧holders/first_write/physical ID/driver观察patch可删；
  TimingSummary和仍有消费者的简单计时保留，真实driver收尾由持久化正确性边界负责。
- HintSession/after_commit可删；根commit实际确认、owner释放后同步发布原hint。取消已到
  但commit确认成功仍应先publish再传播取消；不建容易被取消丢掉的post-commit任务。
  TraceRecorder两处live span显式publish保留，与DB提交hint不同。
- Memory claim和Dream.create_run的抢writer空UPDATE在统一入口后删除，保真实claim/
  source/双owner条件。Work.record_effect的receipt bytes CAS不属于BUSY循环。
- HTTP→SDK、agent_tools→typed result→MainAgentBackend→Work/effect全链都须保错误阶段。
  unknown不能经_result默认uncertain=False变成mutation_committed=False。已有执行身份
  和回执决定恢复，不根据缺回执重派工具或消息。
- plugin媒体文件create提交不明后直接unlink的窗口、notification_delivery出站事件与
  finish_outbox两次提交、旧配置rollback扫描merge谱系、跨入口WorkSourceGuard到writer
  的源复核，均已具名入任务书。它们是源码推导的改造/验证项，未冒称故障注入已通过。

性能沿当前可接受延迟做同到达负载对照，含非空历史、任务树、artifact refs、Memory关系、
媒体清理及必要心跳/结算。不能通过降低到达速率、排除取消样本或新增聊天接纳阈值换结果。
全量替换与删除一起交付，实施时再验证；本轮不执行产品测试或生产变更。

### D18：main更新的独立差异复核

审查期间origin/main从cdb49ead推进到c2ce7da6，文档worktree已ff同步。36文件差异中11个
src文件（C9、D2），A/B源码未变；另有14测试文件、6架构/执行文档、SOUL与模型示例、
quality工作流、pyproject/uv.lock。主审阅读全部src完整diff和依赖/CI差异；A/B/C审查人也
分别独立核对36文件对数据库边界的影响，原两轮报告保留旧基线。

D域container新增read callback装配；prompting/contracts新增普通参与上下文渲染，无新
DB。C域runtime_context沿ordinary.get+actor_identity只读，不hydrate或取observer lock；
continuation_current复用传入session。quiet可见覆盖增加current_admissions读取，随后仍
走原admitted_scope/_save→专用snapshot线程，不是新的主库writer。

新pin为addfcddecb09f93ad8846db508879c451bc4bc47，已用本地git对象独立核store.py与
438f204f完全无diff，且依赖库只有store.py持SQLite。当前共享旧venv仍装旧pin，本轮AST
脚本不import项目，不能借该venv声称已运行新main测试。新增每模型请求读取单列入新基线
性能对照，不归因于尚未实施的writer重构。

## D域逐文件覆盖

下表是三轮累计范围与第3轮实际动作。F=至少一次分块全文，T=实际DB/生命周期/组合方法
正文，S=导入/AST/调用结构排除，I=SDK接口与间接委托，N=纯类型/格式/计算。F不表示每轮
全文；T/S/I/N均不冒称全文。A/B/C中由D补读的5项见D15，不重复计入D110。

| 文件 | 累计等级 | 第3轮 | 实际范围 / 排除理由 |
| --- | --- | --- | --- |
| `src/yuki/__init__.py` | S | S | 导入/AST/调用分类；无新数据库执行入口 |
| `src/yuki/agent_core/__init__.py` | S | S | 主循环/模型边界协议；持久化由C域backend负责 |
| `src/yuki/agent_core/loop.py` | S | S | 主循环/模型边界协议；持久化由C域backend负责 |
| `src/yuki/agent_core/model_boundary.py` | S | S | 主循环/模型边界协议；持久化由C域backend负责 |
| `src/yuki/agent_core/types.py` | S | S | 主循环/模型边界协议；持久化由C域backend负责 |
| `src/yuki/application/__init__.py` | S | S | 导入/AST/调用分类；无新数据库执行入口 |
| `src/yuki/application/lifecycle.py` | F | T | 逆序close遇错仍继续并聚合，不能推定driver退出 |
| `src/yuki/application/modules/__init__.py` | F | T | 装配构造/依赖与资源生命周期；共用主库、独立库不合并 |
| `src/yuki/application/modules/admin.py` | F | T | 装配构造/依赖与资源生命周期；共用主库、独立库不合并 |
| `src/yuki/application/modules/admin_api.py` | F | T | 装配构造/依赖与资源生命周期；共用主库、独立库不合并 |
| `src/yuki/application/modules/asr.py` | F | T | 装配构造/依赖与资源生命周期；共用主库、独立库不合并 |
| `src/yuki/application/modules/automation.py` | F | T | 装配构造/依赖与资源生命周期；共用主库、独立库不合并 |
| `src/yuki/application/modules/conversation.py` | F | T | 装配构造/依赖与资源生命周期；共用主库、独立库不合并 |
| `src/yuki/application/modules/media.py` | F | T | 装配构造/依赖与资源生命周期；共用主库、独立库不合并 |
| `src/yuki/application/modules/model_runtime.py` | F | T | 装配构造/依赖与资源生命周期；共用主库、独立库不合并 |
| `src/yuki/application/modules/persistence.py` | F | T | 装配构造/依赖与资源生命周期；共用主库、独立库不合并 |
| `src/yuki/application/modules/plugins.py` | F | T | 装配构造/依赖与资源生命周期；共用主库、独立库不合并 |
| `src/yuki/application/modules/web.py` | F | T | 装配构造/依赖与资源生命周期；共用主库、独立库不合并 |
| `src/yuki/asr/__init__.py` | S | S | 音频/Provider/类型；无独立DB执行，schema/service另列 |
| `src/yuki/asr/audio.py` | S | S | 音频/Provider/类型；无独立DB执行，schema/service另列 |
| `src/yuki/asr/provider.py` | S | S | 音频/Provider/类型；无独立DB执行，schema/service另列 |
| `src/yuki/asr/schema.py` | F | F | 冻结0055触发器；区别于现行FTS |
| `src/yuki/asr/service.py` | F | F | HTTP/转码在writer外，后续derived文本委托见U35 |
| `src/yuki/cli.py` | F | T | init-db/character/Memory维护独立生命周期；在线plugin委托API |
| `src/yuki/codemode/__init__.py` | S | S | 引擎/IPC/类型/工具投影；主库委托由driver与Work负责 |
| `src/yuki/codemode/api_projection.py` | S | S | 引擎/IPC/类型/工具投影；主库委托由driver与Work负责 |
| `src/yuki/codemode/capacity.py` | S | S | 引擎/IPC/类型/工具投影；主库委托由driver与Work负责 |
| `src/yuki/codemode/contract.py` | S | S | 引擎/IPC/类型/工具投影；主库委托由driver与Work负责 |
| `src/yuki/codemode/driver.py` | T | T | 4处reader与T1–T4原Work/effect/journal委托；纯VM渲染结构 |
| `src/yuki/codemode/driver_types.py` | S | S | 引擎/IPC/类型/工具投影；主库委托由driver与Work负责 |
| `src/yuki/codemode/engine_monty.py` | S | S | 引擎/IPC/类型/工具投影；主库委托由driver与Work负责 |
| `src/yuki/codemode/limits.py` | S | S | 引擎/IPC/类型/工具投影；主库委托由driver与Work负责 |
| `src/yuki/codemode/snapshot_binding.py` | F | F | T0仅ProtocolStore私有文件准备/恢复，T1另由Work同提交引用 |
| `src/yuki/codemode/tool_visibility.py` | S | S | 引擎/IPC/类型/工具投影；主库委托由driver与Work负责 |
| `src/yuki/config.py` | T | T | 主库URL/独立路径与生命周期设置；其余配置结构 |
| `src/yuki/container.py` | T | T | Database共享注入、生命周期、启动失败/close/cleanup与新main装配diff |
| `src/yuki/domain/__init__.py` | N | N | 领域值对象/编解码/类型；SQL命中不表示DB调用 |
| `src/yuki/domain/audio.py` | N | N | 领域值对象/编解码/类型；SQL命中不表示DB调用 |
| `src/yuki/domain/control.py` | N | N | 领域值对象/编解码/类型；SQL命中不表示DB调用 |
| `src/yuki/domain/conversations.py` | N | N | 领域值对象/编解码/类型；SQL命中不表示DB调用 |
| `src/yuki/domain/identity.py` | N | N | 领域值对象/编解码/类型；SQL命中不表示DB调用 |
| `src/yuki/domain/memory_config.py` | N | N | 领域值对象/编解码/类型；SQL命中不表示DB调用 |
| `src/yuki/domain/messages.py` | N | N | 领域值对象/编解码/类型；SQL命中不表示DB调用 |
| `src/yuki/domain/profiles.py` | N | N | 领域值对象/编解码/类型；SQL命中不表示DB调用 |
| `src/yuki/domain/tool_actor.py` | N | N | 领域值对象/编解码/类型；SQL命中不表示DB调用 |
| `src/yuki/event_prompt.py` | S | S | EventRecord/引用渲染；无查询或writer |
| `src/yuki/health.py` | F | T | SELECT 1及读/写健康边界，非写入证明 |
| `src/yuki/http_environment.py` | S | S | 导入/AST/调用分类；无新数据库执行入口 |
| `src/yuki/llm/__init__.py` | S | S | Provider/HTTP/协议数据，调用持久化由C域ModelRuntime负责 |
| `src/yuki/llm/anthropic_messages.py` | S | S | Provider/HTTP/协议数据，调用持久化由C域ModelRuntime负责 |
| `src/yuki/llm/base.py` | S | S | Provider/HTTP/协议数据，调用持久化由C域ModelRuntime负责 |
| `src/yuki/llm/deepseek_responses.py` | S | S | Provider/HTTP/协议数据，调用持久化由C域ModelRuntime负责 |
| `src/yuki/llm/fake.py` | S | S | Provider/HTTP/协议数据，调用持久化由C域ModelRuntime负责 |
| `src/yuki/llm/gemini.py` | S | S | Provider/HTTP/协议数据，调用持久化由C域ModelRuntime负责 |
| `src/yuki/llm/http_errors.py` | S | S | Provider/HTTP/协议数据，调用持久化由C域ModelRuntime负责 |
| `src/yuki/llm/json_http.py` | S | S | Provider/HTTP/协议数据，调用持久化由C域ModelRuntime负责 |
| `src/yuki/llm/openai_compatible.py` | S | S | Provider/HTTP/协议数据，调用持久化由C域ModelRuntime负责 |
| `src/yuki/llm/openai_responses.py` | S | S | Provider/HTTP/协议数据，调用持久化由C域ModelRuntime负责 |
| `src/yuki/llm/protocol_state.py` | S | S | Provider/HTTP/协议数据，调用持久化由C域ModelRuntime负责 |
| `src/yuki/llm/vendor_policy.py` | S | S | Provider/HTTP/协议数据，调用持久化由C域ModelRuntime负责 |
| `src/yuki/logging.py` | S | S | 导入/AST/调用分类；无新数据库执行入口 |
| `src/yuki/main.py` | F | T | 应用OS锁→guard→container及失败/关闭 |
| `src/yuki/operations/__init__.py` | S | S | 导入/AST/调用分类；无新数据库执行入口 |
| `src/yuki/operations/reset_conversations.py` | F | T | 显式离线写，原身份/FTS/refs/cascade原子范围 |
| `src/yuki/plugins/__init__.py` | S | S | 导入/AST/调用分类；无新数据库执行入口 |
| `src/yuki/plugins/ai_chat/__init__.py` | S | S | 导入/AST/调用分类；无新数据库执行入口 |
| `src/yuki/plugins/ai_chat/matcher.py` | S | S | 导入/AST/调用分类；无新数据库执行入口 |
| `src/yuki/plugins/ai_chat/metadata.py` | F | T | 元资料回调真实session写入，进入主库owner |
| `src/yuki/prompting/__init__.py` | N | S | 导出/渲染接口，无owner |
| `src/yuki/prompting/compiler.py` | N | N | 纯计算/渲染或协议，不另开DB |
| `src/yuki/prompting/contracts.py` | N | T | 新main完整diff：runtime_context渲染，无新DB |
| `src/yuki/prompting/contributors.py` | N | N | 纯计算/渲染或协议，不另开DB |
| `src/yuki/prompting/metrics.py` | N | N | 纯计算/渲染或协议，不另开DB |
| `src/yuki/prompting/models.py` | N | N | 纯计算/渲染或协议，不另开DB |
| `src/yuki/prompting/serializer.py` | N | N | 纯计算/渲染或协议，不另开DB |
| `src/yuki/settings_domains.py` | T | T | 独立库/worker/存储设置；不按所有配置新增调度 |
| `src/yuki/time/__init__.py` | N | N | 纯计算/渲染或协议，不另开DB |
| `src/yuki/time/formatting.py` | N | N | 纯计算/渲染或协议，不另开DB |
| `src/yuki/time/models.py` | N | N | 纯计算/渲染或协议，不另开DB |
| `src/yuki/time/schedules.py` | N | N | 纯计算/渲染或协议，不另开DB |
| `src/yuki/time/service.py` | N | N | 纯计算/渲染或协议，不另开DB |
| `src/yuki/tool_results/__init__.py` | S | S | 导入/AST/调用分类；无新数据库执行入口 |
| `src/yuki/tool_results/access.py` | F | T | 原来源/权限/文件/回执与同writer组合，纯展示不迁DB |
| `src/yuki/tool_results/artifacts.py` | F | T | 原来源/权限/文件/回执与同writer组合，纯展示不迁DB |
| `src/yuki/tool_results/recorder.py` | F | T | 原来源/权限/文件/回执与同writer组合，纯展示不迁DB |
| `src/yuki/tool_results/redaction.py` | S | S | 导入/AST/调用分类；无新数据库执行入口 |
| `src/yuki/tool_results/schema.py` | F | T | 原来源/权限/文件/回执与同writer组合，纯展示不迁DB |
| `src/yuki/vision/__init__.py` | S | S | 视觉Provider/结果类型；分析存储在C域service |
| `src/yuki/vision/base.py` | S | S | 视觉Provider/结果类型；分析存储在C域service |
| `src/yuki/vision/fake.py` | S | S | 视觉Provider/结果类型；分析存储在C域service |
| `src/yuki/vision/models.py` | S | S | 视觉Provider/结果类型；分析存储在C域service |
| `src/yuki/vision/qwen.py` | S | S | 视觉Provider/结果类型；分析存储在C域service |
| `src/yuki_plugin_sdk/__init__.py` | I | I | SDK声明/注册/类型；Host具体DB在C域，不公开session |
| `src/yuki_plugin_sdk/api.py` | I | I | SDK声明/注册/类型；Host具体DB在C域，不公开session |
| `src/yuki_plugin_sdk/context.py` | I | I | SDK声明/注册/类型；Host具体DB在C域，不公开session |
| `src/yuki_plugin_sdk/errors.py` | I | I | SDK声明/注册/类型；Host具体DB在C域，不公开session |
| `src/yuki_plugin_sdk/events.py` | I | I | SDK声明/注册/类型；Host具体DB在C域，不公开session |
| `src/yuki_plugin_sdk/features.py` | I | I | SDK声明/注册/类型；Host具体DB在C域，不公开session |
| `src/yuki_plugin_sdk/models.py` | I | I | SDK声明/注册/类型；Host具体DB在C域，不公开session |
| `src/yuki_plugin_sdk/namespace.py` | I | I | SDK声明/注册/类型；Host具体DB在C域，不公开session |
| `src/yuki_plugin_sdk/observation.py` | I | I | SDK声明/注册/类型；Host具体DB在C域，不公开session |
| `src/yuki_plugin_sdk/permissions.py` | I | I | SDK声明/注册/类型；Host具体DB在C域，不公开session |
| `src/yuki_plugin_sdk/plugin.py` | I | I | SDK声明/注册/类型；Host具体DB在C域，不公开session |
| `src/yuki_plugin_sdk/registrar.py` | I | I | SDK声明/注册/类型；Host具体DB在C域，不公开session |
| `src/yuki_plugin_sdk/results.py` | F | F | mutation_committed=None保留unknown语义 |
| `src/yuki_plugin_sdk/sessions.py` | F | F | 会话协议；实际持久化在Host，不是独立DB |
| `src/yuki_plugin_sdk/testing/__init__.py` | S | S | 内存fake/测试合同；字典CAS与fake transcript非SQLite |
| `src/yuki_plugin_sdk/testing/contract.py` | S | S | 内存fake/测试合同；字典CAS与fake transcript非SQLite |
| `src/yuki_plugin_sdk/testing/fake_context.py` | S | S | 内存fake/测试合同；字典CAS与fake transcript非SQLite |
| `src/yuki_plugin_sdk/testing/fake_services.py` | S | S | 内存fake/测试合同；字典CAS与fake transcript非SQLite |
