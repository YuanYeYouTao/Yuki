# SQLite 全覆盖审查：第 3 轮 C 域终审

日期：2026-10-23。源码基线 `c2ce7da66ab54efe192993b9615df48266a62137`；独立工作树 `codex/sqlite-writer-architecture-plan-20261022`。先独立核源码及实际 SQL/事务块，再对照第 2 轮报告和最终任务书。前两轮报告仍认 `cdb49ead`，未改旧报告基线。本轮只写本文，没有改产品、执行测试、调用模型、接触生产库或发送平台消息。

已重读 AGENTS、共同开发约束，按现行 Main/Work/参与/诊断/插件/持久环境合同核边界。C 域仍为 211 个路径；本轮覆盖等级逐项登记，**不是 211 份全文验收**。全文、实际 DB 方法/事务块、间接委托和纯结构分开。第 2 轮遗留的 WorkQueries、Social target/contacts、Main publication/validate、MemoryAdmin 治理授权、SemanticParticipation scene/dispatch/reconcile 已补。

## 结论与任务书修订

共享主库 writer 准入、并行读、短 IMMEDIATE 的方向成立。最终任务书的 U01–U35、W01–W73、V01–V50 已覆盖本域原子单位、独立数据库、取消/未知提交、源复核、保留与删除、性能对照；实施尚未开始。不能把队列接好就称全量完成，也不能为保持旧接口永久保留读快照升级写入口。

本轮新增或加深的两处缺口已交主任务书：

1. `services/agent_tools.py:995` 将 SQLAlchemyError 统一渲染为 retryable database failure；`_result:2190` 默认 uncertain=False，进一步产生 mutation_committed=False。`capabilities/results.py:540` 先看 uncertain，再因 not ok 直接返回 False；仅把 commit 字段改 None 仍不够。提交确认未知必须沿 typed outcome 保 **uncertain=True / mutation_committed=None**，禁止把原效果记成确定未发生，也不能让模型/SDK重派。`main_agent_backend.py:429` 与 `invocation_service.py:233–295` 已实际追到 effect receipt 写入，非只看底层错误类。对应 W06/V22。
2. `services/admin/memory_admin.py:1122/1166/1196` 接受 session，但其 `_actor_person_id:1200` 在 actor.person_id 缺失时，经 ledger.get_event 隐式新开 reader。迁移授权链须在准备阶段沿原内部事件解析 actor，或把同一个 writer session 传到底。不能用今日账号猜原 Person，也不能只修 same_targets/person_owns_fact 两个 helper。对应 W44 后说明。

其余重点与前轮结论相符：WorkSourceGuard 的跨入口原事件证明必须在 publication writer 复核真实依赖；notification 已发后的出站账本与 finish_outbox 需要合成一个短提交，保旧中间状态 reader；plugin media create 提交未知不能直接 unlink；原 effect receipt-bytes CAS、paid 恢复和迟到事实不属于可删 BUSY 循环。

## 实际方法与交错

| 源码/方法 | 本轮独立核对的事实与实施边界 |
| --- | --- |
| `runtime/work_repository.py:135–2781` | 全部正文复核。acquire/renew/release、accept/derive、state/control/checkpoint、input/stage/consume/cancel、child delivery/repair/media、T1/T2/resolve/effect 均有真实 owner。`commit_state:687` 同笔处理业务输入、accepted control、recovery、wait 与父结果；不能仅迁移 state UPDATE。 |
| `runtime/work_repository.py:209/232/2648` | 续租/释放当前 SQL fence 含真实时钟；排队前 valid 只是预筛。record_effect 可无活 lease 接收迟到事实，while 是原 receipt bytes CAS；accepted 不被 unknown 倒写。删除同值 UPDATE 仅限其抢 writer 作用，真实 owner/generation/epoch/expiry 谓词保留。 |
| `runtime/work_wait.py:170/326/367/489/628/693/960` | register、signal、deliver、event/reply匹配、due批处理都核实际 SQL。wait+mailbox+wake 同提交；供给 session 时复用。_observe_page 按 owner/run 祖先关系批查 Work 与 Sandbox，不能因 run_id 相同误认 owner。register 的 after_seconds 是原请求时间基准；不能机械改成取得 writer 后再开始计时。lease/claim 则必须用取得 writer 后当前时间。 |
| `runtime/work_queries.py:35–234` | 全文。get/available/recent/list 纯查询，Person root 用于当前展示，不改原 source；全局可读目录不授予 mutation，local 过滤也不能改写所有权。迁到 read_session 后保持并行和索引分页，不排 writer。 |
| `runtime/work_journal.py:313–520/713–757`、`work_session.py:919–1055` | 文件/JSON/外化像素先准备；protocol publication 锁后才 writer。当前 writer 原子核 lease/generation/source/隐私、refs/journal/recovery。previous media refs 的当前查询用于防 GC/并发，不随准备前移而删掉。paid publication 的外部 BUSY恢复保原输出/预算；commit未知或cleanup未确认不能进入整段重放。 |
| `runtime/work_source_guard.py:1–391`、`services/main_agent_turns.py:176–510` | guard.check 的跨client_input/多Conversation依赖在 reader 查证；callback stage/rollback/finalize 不等于数据库提交证明。publication writer 只核本会话revision不足以证明跨来源仍可读；按选定原event小列/身份/隐私复核，不在writer重扫完整transcript。普通追加消息与原选定内容被改/删除分开。 |
| `services/chat.py:1312–1344` | validate_turn_snapshot 是 coordinator版本或原Work当前控制，再核同Conversation generation 的纯读。run_effect 在内存permit中调用，不自管写事务。它不能替代具体效果 writer 当前 source/authority 检查，也不该被改成排队写。 |
| `social/service.py:151–770`、`social/history.py:39–386` | target/contacts/permission/reply实际查询补齐。精确名字唯一匹配不能以20条目录页断定；current route不伪造原event；私聊原presence/同人族检查保留。History显式BEGIN中一次读完整授权/来源/账本，保各原Conversation与privacy_generation，旧original整数不能解析删除后替代行。 |
| `social/service.py:1296–1618`、`social/repository.py:301–426` | prepare→短claim→外部transport→正常settle，ledger+receipt+Work delivery同session。fallback可能只保存已确认receipt而缺ledger，按原事实报告。claim的prepared→executing是唯一派发归属，不用回调外层锁替代。 |
| `plugin_host/notification_repository.py:448–681/684–1224/1552–1603` | 外部publication event+wait+outbox/job已有原子组合，先认已有publication receipt，再查当前media，不能重试补出缺子项。claim/renew的expiry在writer当前时钟核；finish允许同attempt的迟到已知message_id，不加统一lease活性门槛。admit Work与job.work_id同提交。 |
| `plugin_host/notification_delivery.py:450–638` | 平台确认后当前_record_outbound另开UoW，随后finish_outbox再次提交。W70应先备content/segments/分词，再同session写两者；保旧两提交间崩溃和已知message_id的uncertain，不据缺完成位重发。 |
| `plugin_host/media_artifacts.py:50–148/150–263` | create先写原文件，数据库异常后无条件unlink有提交未知误删风险。W71按原handle查事实或交孤儿维护。cleanup另有pending/processing引用保留与取消/unlink失败恢复；只扩大create异常回滚壳不能解决。 |
| `plugin_host/configuration_service.py:158–225`、`repository.py:361–439/606–750` | validators/JSON在外，当前revision/schema/owner及config/state CAS在当前writer。KV本地Lock与3次版本冲突处理是领域CAS，不是SQLite BUSY；共享writer不自动让事务外usage quota查询变成原子，不以加新持久配额表解决本轮。 |
| `runtime/subagent_repository.py:159–357/467–725`、`sandbox/resource_cleanup.py` | 归档候选16并不限制整个后代/request/peer/ref集合。准备关闭peer后不能重发close；writer核实际资源闭包和absence。message/resume/cancel/finish原mailbox/wait/tree与回执组合保持。 |
| `services/participation_feedback.py:349–594` | _read_facts读取完整原run/祖先计费、Social/caption、tool receipts、durable feedback和checkpoint；64 refs只是每页写量。新效果留下一页，不能拿实时600秒/2048条观察代替真实全效果证明。主库seq/refs先提交，再独立snapshot CAS追赶。 |
| `services/semantic_participation.py:194–237/293–580/682–1504` | scene/retire/hydrate/source/admit/dispatch/reconcile全部实际DB函数补齐。_sources_current供给session时沿同快照，先解析当前身份再更新派生边界；不新增永久授权缓存。_dispatch按原initiative source找Work/接纳，Snapshot不是另一个执行owner。 |
| `services/participation_snapshot.py:1–96`、`semantic_participation.py:488` | 同步SnapshotStore单专用线程串行；取消save时先取得实际结果/revision并更新item.saved，再传播取消。不同文件不与主writer共锁，不伪造跨库事务。 |
| `sandbox/persistent.py`、`acp_manager.py`、`completions.py` | 独立Manager jobs.sqlite3。全部同步with db DML块及裸SQL复核：构造DDL后显式commit；with块无await。intent→dispatch→外部→result、job终态+completion原子，file/service分别保原ID。同步SQL/JSON仍可能占自身事件循环，但不为此新造daemon或合入主库队列。 |
| `sandbox/task_repository.py:53–328`、`completion_receiver.py:61–145` | Host submission先存；completion+continuation/permission+input+wait先本地提交，之后才Manager ACK。提交未知不ACK，重访原事件幂等接收；毒前缀按页游标复访，不丢后页。不重新远程submit原request。 |
| `execution_trace/recorder.py:385–521`、`model_runtime/repository.py:39–274` | writer=None fallback、直接record、cleanup都要收口，诊断异步队列保留。trace_span开始/结束两处live hint不是DB commit，删除HintSession时保显式publish。缺cache/usage保NULL，统计sum/coalesce不证明每样本已知。 |

## 同 session、隐式写与 Schema 排除

读取本身不一定纯读：GroupAdmin._set_enabled在AdminAudit.transaction内修改Group并同session记audit；PreferenceAdmin的事实变更与后置audit目前是分开的真实阶段，不得顺手在writer里调用整个公开服务。Plugin配置、Grant、Session、Sandbox receive、Social finish都有dirty ORM与commit flush；只搜execute(UPDATE)会漏。新read_session关闭autoflush只用于自己拥有的新reader，供给的writer不切模式。

Schema分类实际核了表字段、FK、unique/check/index，以及有执行效果的安装函数：effect_schema.install_indexes、protocol_schema.install_quota含DDL/INSERT/trigger；work_budget_schema.create_current_budget_tables不是纯类型。它们归Schema/setup owner，不按运行时writer，也不从审查排除。Work父子/inputs/effects/journal的RESTRICT与protocol refs CASCADE、Social原event SET NULL、plugin session message CASCADE等清退顺序保留。冻结迁移不在C域重写；本域未发现另一份新增SQLite文件。

协议发布 protocol lock→reader退出→writer；GC短writer标记→writer外文件锁/unlink→短writer确认。Plugin lifecycle hook、媒体/Provider/Manager调用在短SQL阶段之外；不引入可重入writer，也不在writer里等另一个要写库的关闭任务。媒体unknown与底层driver取消是两件事，文件fence的_finish_thread不能被当作aiosqlite物理退出证明。

## 性能与可删除内容

当前用户认可延迟；这次目标是统一职责并删旧机制。普通GET/target/health/history/向量查询继续并行；空poll/无候选不占writer。正常Main新参与资料增加原admission主键读与当前人物批读，既不hydrate也不save；必须计入每请求真实SQL和到达→响应，不能因为没有writer就当免费。Snapshot JSON/CAS、Work树/媒体ref、notification全部候选与trace满负载都是规模场景，不能只测空库。

删旧apply_snapshot_write/证据BUSY循环、仅抢writer空UPDATE、旧holder/first-write观测、仅观测dialect/raw_connection patch和HintSession监听；保原CAS/owner/source、paid原件、intent/dispatch/result、budget、late/unknown事实。W05仍需锁定真实driver做取消实验，W06须追到消费错误的工具/SDK/Work；这不是本轮静态审查已经完成的验证。

## 测试与覆盖边界

本轮全文阅读且未执行：`tests/unit/test_work_settlement_writer.py`（1284行）、`test_work_delivery_ownership.py`（704行）、`test_work_native_media_receipts.py`（319行）、`test_plugin_notification_idempotency.py`（1196行）。

它们实际保护：accepted completion与sync_result、业务/辅助输入区别、原Work跨账号/同人合并、长run/多条件wait、页尾待办推进、ordinary ready/admission/wake失败全回滚、原文件/附言unknown不重传、paid媒体恢复不重调、隐私删除后的旧像素不复用、publication重复快路与原manifest、并发winner先于当前media、到期send隔离与同attempt晚回执、仅确认presend失败可重试、旧raw target迁移不买第二次发送。

这些测试并不覆盖新FIFO尚未存在的真实driver各阶段取消，也没有完整覆盖create提交ACK丢失、notification event→outbox中间崩溃与跨入口source proof→writer新交错。V01–V50明确保这些实施验证；不能把阅读写成新架构测试通过。第2轮已经全文的crash-window/process-crash/media-cleanup测试不重复执行或累加通过数。

## 新 main 的 36 文件 delta 独立核查

`git diff --name-only cdb49ead c2ce7da`实际36个文件，其中源码11个、C域9个，未增加/删除C路径。下表D=完整变更hunk核查；T=文档按持久化/运行链相关章节及调用面复核，非全文。新增大测试的源正文按函数分段补读，旧文件只核变更，不冒充整个旧文件全文。未运行这些测试，也不引用别的会话通过数作本轮验收。

| 文件 | 核查 | 对数据库/本任务的影响 |
| --- | --- | --- |
| `.github/workflows/quality.yml` | D | APT源修订，无数据库语义 |
| `config/SOUL.md` | D | 表达指导，无持久writer或发送新门槛 |
| `config/autonomous-model.example.json` | D | 清退continuation_review_span，不改SQLite结构 |
| `docs/architecture/README.md` | D | 文档索引与交付状态，非DB入口 |
| `docs/architecture/Yuki-JEV稀疏复核与闲聊收束任务书-2026-10-22.md` | T | 稀疏调度/运行资料/升级/性能合同；无主库新writer设计 |
| `docs/architecture/autonomous-participation-model.md` | D | 删额度、保真实来源顺序和稀疏复核 |
| `docs/architecture/main-agent-runtime.md` | D | normal请求尾部最小事实，精确paid续接不插入 |
| `docs/architecture/semantic-participation.md` | D | 读原admission/当前同人，writer核真实身份，不缓存许可 |
| `docs/operations/sparse-jev-implementation-20261022.md` | T | 实施证据与边界独立，旧性能/测试报告不是本轮验收 |
| `pyproject.toml` | D | 参与库pin→addfcdde，与lock一致 |
| `src/yuki/container.py` | D | 装配participation_runtime_context回调，不另建Database |
| `src/yuki/prompting/contracts.py` | D | 参与反馈/表达指导，无DB |
| `src/yuki/runtime/work_control.py` | D | 自然提示文字，不改writer/owner |
| `src/yuki/services/agent_runner.py` | D | Backend新增只读participation_context接口 |
| `src/yuki/services/chat.py` | D | 传入查询回调，无新writer |
| `src/yuki/services/main_agent_backend.py` | D | 正常请求读原绑定事实，visible_event_ids反馈 |
| `src/yuki/services/participation_identity.py` | D | 批读当前人物与boundary actor，不写原作者 |
| `src/yuki/services/participation_ordinary.py` | D | 新runtime_context纯读ordinary.get+actor_identity；quiet覆实际已读同activation，再独立save |
| `src/yuki/services/processor.py` | D | 开放群implicit资格归一；供给writer的continuation_current沿同session |
| `src/yuki/services/semantic_participation.py` | D | source失效前批备当前身份，scene采样/旧check_budget清退 |
| `src/yuki/services/turn_execution.py` | D | 仅本来存在的normal request读资料，不改paid精确恢复/预算/冻结前缀 |
| `tests/integration/test_participation_foreground_cost.py` | D | 测试变更核到真实SQLite/原identity/paid或无writer读路径，未执行 |
| `tests/integration/test_participation_parallel_review.py` | D | 测试变更核到真实SQLite/原identity/paid或无writer读路径，未执行 |
| `tests/integration/test_participation_person_identity.py` | D | 测试变更核到真实SQLite/原identity/paid或无writer读路径，未执行 |
| `tests/integration/test_participation_projection_cost.py` | D | 测试变更核到真实SQLite/原identity/paid或无writer读路径，未执行 |
| `tests/unit/test_acp_adapter.py` | D | 测试变更核到真实SQLite/原identity/paid或无writer读路径，未执行 |
| `tests/unit/test_open_group_semantic_qualification.py` | D | 测试变更核到真实SQLite/原identity/paid或无writer读路径，未执行 |
| `tests/unit/test_ordinary_input_handoff.py` | D | 测试变更核到真实SQLite/原identity/paid或无writer读路径，未执行 |
| `tests/unit/test_participation_context_scope.py` | D | 407行新增全文：不取observer锁/不save无DML、原讨论/同人/未接纳邀请与Boundary身份 |
| `tests/unit/test_participation_foreground_preparation.py` | D | 测试变更核到真实SQLite/原identity/paid或无writer读路径，未执行 |
| `tests/unit/test_participation_ordinary_feedback.py` | D | 测试变更核到真实SQLite/原identity/paid或无writer读路径，未执行 |
| `tests/unit/test_participation_pipeline_retirement.py` | D | 测试变更核到真实SQLite/原identity/paid或无writer读路径，未执行 |
| `tests/unit/test_participation_profile_upgrade.py` | D | 测试变更核到真实SQLite/原identity/paid或无writer读路径，未执行 |
| `tests/unit/test_participation_quiet_coverage.py` | D | 110行新增全文：只覆盖实际已派发同actor/activation，重复response与hydrate不扩大basis |
| `tests/unit/test_participation_runtime_tail.py` | D | 317行新增全文：有/无Work正常请求尾部、原签名/配对、pause/paid恢复不插入 |
| `uv.lock` | D | 相同git pin与包来源，无SQLAlchemy/aiosqlite升级 |

独立pin核查使用本机参与库git对象，不按其工作树HEAD：`438f204f9ebe2b8c1b698b68c0f1b3ac8b4facef..addfcddecb09f93ad8846db508879c451bc4bc47` 的 `src/yuki_participation/store.py` diff为空；对addfcdde源码git grep sqlite3/BEGIN IMMEDIATE，仅该store拥有SQLite。其完整正文已核：JSON编码在BEGIN IMMEDIATE前，save按revision CAS+实际byte_size/配额+UPSERT一事务，异常rollback；delete_scope认原revision。Host专用线程的load/save/close owner保持。新controller/queue升级可能改变保存频率或JSON量，不能因此断言快照成本没变；也没有把26文件依赖变更称逐文件全文评审。

## 211 路径最终分类

F=本轮全文；M=实际DB方法/SQL事务块、必要helper/消费链语义复核，范围逐项列明，非全文；R=间接委托的实际调用、实参和目标owner核对，无自管事务，不把其它业务分支计全文；D=表/索引/约束/真实安装入口按Schema分类；S=纯类型/计算/格式/网络/文件/内存状态的结构分类排除。M不等于只扫描BEGIN，R/S也不冒充源码全文。表内保前轮级别便于累计复核，不把累计全文改写成每轮全文。

| 文件（src/yuki/下） | 当前行数 | R2 | R3 | 本轮范围/排除依据 |
| --- | ---: | --- | --- | --- |
| `runtime/__init__.py` | 26 | S | S | 复核完整导入/函数与实际调用面：纯类型、格式/计算、网络/文件或进程内状态；无自管SQLite/隐式ORM写 |
| `runtime/activation_bindings.py` | 62 | S | S | 复核完整导入/函数与实际调用面：纯类型、格式/计算、网络/文件或进程内状态；无自管SQLite/隐式ORM写 |
| `runtime/activation_outcome.py` | 171 | F | F | 全文；实际DB、委托、或明确纯处理性质见正文 |
| `runtime/activation_tasks.py` | 67 | I | R | 仅task owner/drain与取消，不存SQLite；实参/owner核对，非全文 |
| `runtime/context_preparation.py` | 116 | F | F | 全文；真实多查询快照/原receipt与同session helper或源谓词已独立补读，不作结构排除 |
| `runtime/contracts.py` | 27 | S | S | 复核完整导入/函数与实际调用面：纯类型、格式/计算、网络/文件或进程内状态；无自管SQLite/隐式ORM写 |
| `runtime/delivery.py` | 14 | I | R | 协议接口；实际delivery writer在delivery_intents/Social；实参/owner核对，非全文 |
| `runtime/delivery_intents.py` | 124 | F | F | 全文；真实多查询快照/原receipt与同session helper或源谓词已独立补读，不作结构排除 |
| `runtime/effect_outcomes.py` | 421 | I | R | typed outcome与历史receipt纯解释；不自行读写数据库；实参/owner核对，非全文 |
| `runtime/effect_queries.py` | 75 | F | F | 全文；真实多查询快照/原receipt与同session helper或源谓词已独立补读，不作结构排除 |
| `runtime/effect_schema.py` | 45 | S | D | 字段/FK/unique/check/index与安装函数分类复核；DDL installer见正文，不当纯类型排掉 |
| `runtime/errors.py` | 15 | S | S | 复核完整导入/函数与实际调用面：纯类型、格式/计算、网络/文件或进程内状态；无自管SQLite/隐式ORM写 |
| `runtime/execution_receipts.py` | 27 | I | R | confirm调用SandboxContinuationRepository.observed；无自身事务；实参/owner核对，非全文 |
| `runtime/lease_heartbeat.py` | 119 | F | F | 全文；实际DB、委托、或明确纯处理性质见正文 |
| `runtime/observability.py` | 77 | I | R | ContextVar/上下文关联，不是持久writer；实参/owner核对，非全文 |
| `runtime/origin.py` | 28 | S | S | 复核完整导入/函数与实际调用面：纯类型、格式/计算、网络/文件或进程内状态；无自管SQLite/隐式ORM写 |
| `runtime/principal.py` | 25 | S | S | 复核完整导入/函数与实际调用面：纯类型、格式/计算、网络/文件或进程内状态；无自管SQLite/隐式ORM写 |
| `runtime/protocol_schema.py` | 49 | S | D | 字段/FK/unique/check/index与安装函数分类复核；DDL installer见正文，不当纯类型排掉 |
| `runtime/protocol_store.py` | 677 | F | M | publication/publish_refs 390–490、_gc_page/unlink/cleanup 497–677、_finish_thread 62–80；文件准备接口及调用面复核，非全文 |
| `runtime/subagent_repository.py` | 725 | F | M | 全部 writer SQL块、maintain 159–357、message/resume/cancel/finish 467–725，读接口/原父子来源复核；非全文 |
| `runtime/subagent_scheduler.py` | 137 | I | M | loop 89–137实际只读候选及维护委托；关闭/执行接口复核 |
| `runtime/subagent_schema.py` | 42 | S | D | 字段/FK/unique/check/index与安装函数分类复核；DDL installer见正文，不当纯类型排掉 |
| `runtime/subagent_tools.py` | 155 | I | M | execute_subagent 55–155全文；WorkQueries/children委托、实际原owner与cancel/resume |
| `runtime/trigger.py` | 104 | I | R | 激活协议/上下文标识，无自管连接；实参/owner核对，非全文 |
| `runtime/work_activation.py` | 206 | F | R | 实际持久调用委托：repository.acquire, repository.active, repository.transition, repository.renew, repository.lease_expiry, repository.valid, repository.release；对应owner已列，非全文 |
| `runtime/work_budget.py` | 64 | F | F | 全文；实际DB、委托、或明确纯处理性质见正文 |
| `runtime/work_budget_schema.py` | 21 | S | D | 字段/FK/unique/check/index与安装函数分类复核；DDL installer见正文，不当纯类型排掉 |
| `runtime/work_context_note.py` | 190 | F | F | 全文；真实多查询快照/原receipt与同session helper或源谓词已独立补读，不作结构排除 |
| `runtime/work_control.py` | 1136 | F | M | _control全部writer块739–747/842–902：accept+charge+effect、manage原子；其余调用面及本次delta |
| `runtime/work_journal.py` | 757 | F | M | save/invalidate/recovered_inputs 313–520、record_effect/effect_state 713–757，协议ref/paid/source组合；非全文 |
| `runtime/work_management.py` | 356 | F | M | stop_owned_execution/manage_work 124–273：原子撤权树、mailbox/waits、checkpoint；非全文 |
| `runtime/work_media.py` | 43 | F | S | 复核完整导入/函数与实际调用面：纯类型、格式/计算、网络/文件或进程内状态；无自管SQLite/隐式ORM写 |
| `runtime/work_observation_schema.py` | 10 | S | D | 字段/FK/unique/check/index与安装函数分类复核；DDL installer见正文，不当纯类型排掉 |
| `runtime/work_queries.py` | 234 | I | F | 全文；实际DB、委托、或明确纯处理性质见正文 |
| `runtime/work_query_schema.py` | 89 | S | D | 字段/FK/unique/check/index与安装函数分类复核；DDL installer见正文，不当纯类型排掉 |
| `runtime/work_recovery_schema.py` | 44 | S | D | 字段/FK/unique/check/index与安装函数分类复核；DDL installer见正文，不当纯类型排掉 |
| `runtime/work_repository.py` | 2781 | F | F | 全文；实际DB、委托、或明确纯处理性质见正文 |
| `runtime/work_scheduler.py` | 358 | M | M | health 104–140完整只读SQL；维护/候选231–358与owner委托复核 |
| `runtime/work_schema_v1.py` | 120 | S | D | 字段/FK/unique/check/index与安装函数分类复核；DDL installer见正文，不当纯类型排掉 |
| `runtime/work_session.py` | 1055 | F | M | 919–1055完整save/_save，paid publication的BUSY/cleanup/source分支；其它DB调用委托到journal/repository逐项核 |
| `runtime/work_source_guard.py` | 391 | F | F | 全文；实际DB、委托、或明确纯处理性质见正文 |
| `runtime/work_supervisor.py` | 267 | F | M | recover_failure 53–180完整writer与原recovery/accepted control；其余错误分类接口复核 |
| `runtime/work_tree.py` | 69 | F | F | 全文；真实多查询快照/原receipt与同session helper或源谓词已独立补读，不作结构排除 |
| `runtime/work_wait.py` | 1016 | F | M | register/cancel/match/deliver全部writer块与in-session helpers、只读预筛、_observe_page批查询，实际SQL/原子性复核；非全文 |
| `runtime/work_wait_schema.py` | 48 | S | D | 字段/FK/unique/check/index与安装函数分类复核；DDL installer见正文，不当纯类型排掉 |
| `model_runtime/__init__.py` | 63 | S | S | 复核完整导入/函数与实际调用面：纯类型、格式/计算、网络/文件或进程内状态；无自管SQLite/隐式ORM写 |
| `model_runtime/capacity.py` | 264 | S | S | 复核完整导入/函数与实际调用面：纯类型、格式/计算、网络/文件或进程内状态；无自管SQLite/隐式ORM写 |
| `model_runtime/db_models.py` | 61 | S | D | 字段/FK/unique/check/index与安装函数分类复核；DDL installer见正文，不当纯类型排掉 |
| `model_runtime/dispatch_guard.py` | 26 | F | S | 复核完整导入/函数与实际调用面：纯类型、格式/计算、网络/文件或进程内状态；无自管SQLite/隐式ORM写 |
| `model_runtime/executor.py` | 965 | M | M | 495–622模型结果/usage与_record_invocation错误出口；其余计费/模型调度委托复核，无自管writer |
| `model_runtime/models.py` | 214 | S | S | 复核完整导入/函数与实际调用面：纯类型、格式/计算、网络/文件或进程内状态；无自管SQLite/隐式ORM写 |
| `model_runtime/pool.py` | 172 | I | R | Provider客户端池/关闭，无数据库连接；实参/owner核对，非全文 |
| `model_runtime/profiles.py` | 240 | S | S | 复核完整导入/函数与实际调用面：纯类型、格式/计算、网络/文件或进程内状态；无自管SQLite/隐式ORM写 |
| `model_runtime/repository.py` | 274 | F | F | 全文；实际DB、委托、或明确纯处理性质见正文 |
| `model_runtime/request_accounting.py` | 92 | F | S | 复核完整导入/函数与实际调用面：纯类型、格式/计算、网络/文件或进程内状态；无自管SQLite/隐式ORM写 |
| `model_runtime/routes.py` | 32 | S | S | 复核完整导入/函数与实际调用面：纯类型、格式/计算、网络/文件或进程内状态；无自管SQLite/隐式ORM写 |
| `model_runtime/search_defaults.py` | 110 | S | S | 复核完整导入/函数与实际调用面：纯类型、格式/计算、网络/文件或进程内状态；无自管SQLite/隐式ORM写 |
| `model_runtime/secrets.py` | 80 | S | S | 复核完整导入/函数与实际调用面：纯类型、格式/计算、网络/文件或进程内状态；无自管SQLite/隐式ORM写 |
| `model_runtime/structured.py` | 393 | I | R | 模型执行委托ModelExecutor与已付Work计费；不自管SQL；实参/owner核对，非全文 |
| `execution_trace/__init__.py` | 1 | S | S | 复核完整导入/函数与实际调用面：纯类型、格式/计算、网络/文件或进程内状态；无自管SQLite/隐式ORM写 |
| `execution_trace/db_models.py` | 63 | S | D | 字段/FK/unique/check/index与安装函数分类复核；DDL installer见正文，不当纯类型排掉 |
| `execution_trace/payload.py` | 184 | S | S | 复核完整导入/函数与实际调用面：纯类型、格式/计算、网络/文件或进程内状态；无自管SQLite/隐式ORM写 |
| `execution_trace/phases.py` | 150 | S | S | 复核完整导入/函数与实际调用面：纯类型、格式/计算、网络/文件或进程内状态；无自管SQLite/隐式ORM写 |
| `execution_trace/recorder.py` | 521 | F | F | 全文；实际DB、委托、或明确纯处理性质见正文 |
| `social/__init__.py` | 1 | S | S | 复核完整导入/函数与实际调用面：纯类型、格式/计算、网络/文件或进程内状态；无自管SQLite/隐式ORM写 |
| `social/agent_adapter.py` | 216 | I | F | 全文；实际DB、委托、或明确纯处理性质见正文 |
| `social/confirmed_content.py` | 35 | F | F | 全文；真实多查询快照/原receipt与同session helper或源谓词已独立补读，不作结构排除 |
| `social/content.py` | 331 | I | M | prepare_for_entry 180–246实际名字/账号/歧义只读SQL完整；纯渲染结构排除 |
| `social/db_models.py` | 89 | S | D | 字段/FK/unique/check/index与安装函数分类复核；DDL installer见正文，不当纯类型排掉 |
| `social/emoji_intents.py` | 376 | F | M | admit/select/close全部writer块及supplied-session source helper/读取接口；非全文 |
| `social/history.py` | 386 | I | F | 全文；实际DB、委托、或明确纯处理性质见正文 |
| `social/models.py` | 81 | S | S | 复核完整导入/函数与实际调用面：纯类型、格式/计算、网络/文件或进程内状态；无自管SQLite/隐式ORM写 |
| `social/repository.py` | 709 | F | M | 全部owner writer块、claim/finish/get/find301–426完整；当前target/payload/source委托核查，非全文 |
| `social/service.py` | 1811 | M | M | target/context/contacts/routes/reply 151–770；_effect/_dispatch_claimed1296–1618；实际writer callbacks与错误出口完整，非全文 |
| `social/source_keys.py` | 22 | S | S | 复核完整导入/函数与实际调用面：纯类型、格式/计算、网络/文件或进程内状态；无自管SQLite/隐式ORM写 |
| `social/tools.py` | 167 | S | S | 复核完整导入/函数与实际调用面：纯类型、格式/计算、网络/文件或进程内状态；无自管SQLite/隐式ORM写 |
| `plugin_host/__init__.py` | 5 | S | S | 复核完整导入/函数与实际调用面：纯类型、格式/计算、网络/文件或进程内状态；无自管SQLite/隐式ORM写 |
| `plugin_host/admission_adapter.py` | 172 | I | S | 复核完整导入/函数与实际调用面：纯类型、格式/计算、网络/文件或进程内状态；无自管SQLite/隐式ORM写 |
| `plugin_host/approval.py` | 66 | I | R | 持久审批委托store.save/delete，实际owner在安装仓库；无另一个数据库；实参/owner核对，非全文 |
| `plugin_host/audit.py` | 42 | F | R | 实际持久调用委托：self._repository.record；对应owner已列，非全文 |
| `plugin_host/background_turns.py` | 456 | F | M | 实际reader块167–178/232–233，原Job/source/Presence，及执行/恢复委托；非全文 |
| `plugin_host/canonical_projection.py` | 57 | I | S | 复核完整导入/函数与实际调用面：纯类型、格式/计算、网络/文件或进程内状态；无自管SQLite/隐式ORM写 |
| `plugin_host/capability_adapter.py` | 375 | I | M | _media_available/_available/_record 260–354完整DB读/当前权限/审计；execute实际委托及错误出口复核 |
| `plugin_host/command_adapter.py` | 206 | I | R | Control principal读取与插件callback scope；不在writer里运行handler；实参/owner核对，非全文 |
| `plugin_host/config.py` | 128 | F | F | 全文；实际DB、委托、或明确纯处理性质见正文 |
| `plugin_host/configuration_service.py` | 210 | F | M | save 158–225完整validator→write callback；_read/revision查询依赖与调用面复核 |
| `plugin_host/db_models.py` | 635 | S | D | 字段/FK/unique/check/index与安装函数分类复核；DDL installer见正文，不当纯类型排掉 |
| `plugin_host/direct_command_router.py` | 91 | I | S | 复核完整导入/函数与实际调用面：纯类型、格式/计算、网络/文件或进程内状态；无自管SQLite/隐式ORM写 |
| `plugin_host/direct_delivery.py` | 163 | F | F | 全文；真实多查询快照/原receipt与同session helper或源谓词已独立补读，不作结构排除 |
| `plugin_host/discovery.py` | 87 | I | S | 复核完整导入/函数与实际调用面：纯类型、格式/计算、网络/文件或进程内状态；无自管SQLite/隐式ORM写 |
| `plugin_host/event_bus.py` | 297 | I | S | 复核完整导入/函数与实际调用面：纯类型、格式/计算、网络/文件或进程内状态；无自管SQLite/隐式ORM写 |
| `plugin_host/extension_registry.py` | 283 | I | S | 复核完整导入/函数与实际调用面：纯类型、格式/计算、网络/文件或进程内状态；无自管SQLite/隐式ORM写 |
| `plugin_host/facades.py` | 3260 | M | M | _send_onebot 2548–2730正常/补记完整；其余SDK实际持久委托及supplied-session调用面；非全文 |
| `plugin_host/http_client.py` | 314 | I | S | 复核完整导入/函数与实际调用面：纯类型、格式/计算、网络/文件或进程内状态；无自管SQLite/隐式ORM写 |
| `plugin_host/loader.py` | 86 | I | S | 复核完整导入/函数与实际调用面：纯类型、格式/计算、网络/文件或进程内状态；无自管SQLite/隐式ORM写 |
| `plugin_host/main_turn.py` | 714 | M | M | 全部实际reader块60–76/269–282/595–596，原plugin Work身份、actor/presence及入口error映射；非全文 |
| `plugin_host/manager.py` | 876 | F | M | 生命周期锁、DB状态委托与外部hooks的分段调用复核；非全文 |
| `plugin_host/manifest.py` | 239 | S | S | 复核完整导入/函数与实际调用面：纯类型、格式/计算、网络/文件或进程内状态；无自管SQLite/隐式ORM写 |
| `plugin_host/media_artifacts.py` | 263 | F | F | 全文；实际DB、委托、或明确纯处理性质见正文 |
| `plugin_host/models.py` | 42 | S | S | 复核完整导入/函数与实际调用面：纯类型、格式/计算、网络/文件或进程内状态；无自管SQLite/隐式ORM写 |
| `plugin_host/notification_delivery.py` | 633 | F | M | 450–638已发后结算与_record_outbound完整，prepare/claim接口及其owner复核；非全文 |
| `plugin_host/notification_manifest.py` | 109 | S | S | 复核完整导入/函数与实际调用面：纯类型、格式/计算、网络/文件或进程内状态；无自管SQLite/隐式ORM写 |
| `plugin_host/notification_repository.py` | 2231 | F | M | 全部自管writer块193–1603、同session/retry/finish与当前fence调用面；publication/Work/link/outbox原子性逐项核，非2231行全文 |
| `plugin_host/ownership.py` | 517 | F | F | 全文；真实多查询快照/原receipt与同session helper或源谓词已独立补读，不作结构排除 |
| `plugin_host/prompt_adapter.py` | 52 | I | S | 复核完整导入/函数与实际调用面：纯类型、格式/计算、网络/文件或进程内状态；无自管SQLite/隐式ORM写 |
| `plugin_host/repository.py` | 942 | F | M | 全部自管writer SQL块、_compare_and_set_canonical 361–439完整；config/state lineage/CAS与审计读入口复核，非全文 |
| `plugin_host/secrets.py` | 48 | S | S | 复核完整导入/函数与实际调用面：纯类型、格式/计算、网络/文件或进程内状态；无自管SQLite/隐式ORM写 |
| `plugin_host/session_facade.py` | 93 | F | S | 复核完整导入/函数与实际调用面：纯类型、格式/计算、网络/文件或进程内状态；无自管SQLite/隐式ORM写 |
| `plugin_host/session_repository.py` | 493 | F | M | 全部create/append/close/reset/delete/expire/ephemeral实际writer块及读入口；非全文 |
| `plugin_host/storage.py` | 154 | F | F | 全文；实际DB、委托、或明确纯处理性质见正文 |
| `sandbox/__init__.py` | 1 | S | S | 复核完整导入/函数与实际调用面：纯类型、格式/计算、网络/文件或进程内状态；无自管SQLite/隐式ORM写 |
| `sandbox/acp_config.py` | 73 | S | S | 复核完整导入/函数与实际调用面：纯类型、格式/计算、网络/文件或进程内状态；无自管SQLite/隐式ORM写 |
| `sandbox/acp_manager.py` | 477 | F | M | 全部DB事务原文/裸SQL，原close/dispatch/_purge及外部post_request顺序；非477行全文 |
| `sandbox/acp_peer.py` | 636 | I | S | 复核完整导入/函数与实际调用面：纯类型、格式/计算、网络/文件或进程内状态；无自管SQLite/隐式ORM写 |
| `sandbox/client.py` | 217 | I | R | tasks.prepare/bind/checkpoint/receive后socket/recover；提交未知不另造request_id；实参/owner核对，非全文 |
| `sandbox/completion_receiver.py` | 140 | F | F | 全文；实际DB、委托、或明确纯处理性质见正文 |
| `sandbox/completions.py` | 54 | F | F | 全文；实际DB、委托、或明确纯处理性质见正文 |
| `sandbox/continuation_worker.py` | 62 | I | R | ready/rotate与Work.route_child_completion委托；不自管事务；实参/owner核对，非全文 |
| `sandbox/continuations.py` | 51 | F | M | rotate自管writer与ready/observed/finish supplied-session调用边界逐项核；非全文 |
| `sandbox/control_completions.py` | 94 | F | M | record_control_completion 18–94原请求/audit/source和原子completion完整；非全文 |
| `sandbox/db_models.py` | 58 | S | D | 字段/FK/unique/check/index与安装函数分类复核；DDL installer见正文，不当纯类型排掉 |
| `sandbox/environment_supervisor.py` | 198 | I | S | 复核完整导入/函数与实际调用面：纯类型、格式/计算、网络/文件或进程内状态；无自管SQLite/隐式ORM写 |
| `sandbox/environment_tools.py` | 138 | I | S | 复核完整导入/函数与实际调用面：纯类型、格式/计算、网络/文件或进程内状态；无自管SQLite/隐式ORM写 |
| `sandbox/execd.py` | 48 | I | S | 复核完整导入/函数与实际调用面：纯类型、格式/计算、网络/文件或进程内状态；无自管SQLite/隐式ORM写 |
| `sandbox/external_agents.py` | 252 | I | R | tasks原request/run/peer准备与checkpoint，外部调用在原DB阶段之外；实参/owner核对，非全文 |
| `sandbox/manager.py` | 90 | I | R | PersistentManager生命周期/服务入口；具体jobs库见persistent；实参/owner核对，非全文 |
| `sandbox/persistent.py` | 1275 | F | M | 所有with self.db事务原文及裸DB调用SQL；构造DDL/commit、dispatch/file/service/receipt边界与await关系，非1275行全文 |
| `sandbox/resource_cleanup.py` | 144 | F | F | 全文；实际DB、委托、或明确纯处理性质见正文 |
| `sandbox/task_repository.py` | 324 | F | F | 全文；实际DB、委托、或明确纯处理性质见正文 |
| `services/__init__.py` | 29 | S | S | 复核完整导入/函数与实际调用面：纯类型、格式/计算、网络/文件或进程内状态；无自管SQLite/隐式ORM写 |
| `services/admin/__init__.py` | 13 | S | S | 复核完整导入/函数与实际调用面：纯类型、格式/计算、网络/文件或进程内状态；无自管SQLite/隐式ORM写 |
| `services/admin/config_admin.py` | 108 | I | R | snapshot/get/history与override/delete/rollback委托RuntimeConfigService；实参/owner核对，非全文 |
| `services/admin/control_auth.py` | 73 | I | S | 复核完整导入/函数与实际调用面：纯类型、格式/计算、网络/文件或进程内状态；无自管SQLite/隐式ORM写 |
| `services/admin/group_admin.py` | 84 | I | F | 全文；实际DB、委托、或明确纯处理性质见正文 |
| `services/admin/group_recovery.py` | 379 | F | M | enable 107–255当前writer完整；外部prepare与当前source/route/absence实参核查，非全文 |
| `services/admin/memory_admin.py` | 1208 | M | M | 治理/授权542–1208全文，含隐式_actor_person_id reader；前半mutation/legacy callback的DB委托/原target选择复核，非全文 |
| `services/admin/preference_admin.py` | 167 | I | F | 全文；实际DB、委托、或明确纯处理性质见正文 |
| `services/agent_runner.py` | 1110 | I | R | paid response/paired交WorkSession；工具实际执行交InvocationService/WorkControl；实参/owner核对，非全文 |
| `services/agent_tools.py` | 2227 | M | M | SQLAlchemyError映射930–1020与_result2140–2227完整；全部实际持久工具分派/读入口函数定位并核owner，非全文 |
| `services/automation_commands.py` | 121 | I | R | list/history/pause/resume/cancel/run_now交AutomationService；无自管事务；实参/owner核对，非全文 |
| `services/autonomous_groups.py` | 326 | I | R | 运行配置读与语义接纳委托；debounce只在内存，不持DB事务；实参/owner核对，非全文 |
| `services/chat.py` | 1688 | M | M | validate_turn_snapshot/run_effect1312–1344完整；before_publish源与ordinary/current callback、实际出站账本及本次delta，非全文 |
| `services/chat_preparation_timings.py` | 110 | F | F | 全文；实际DB、委托、或明确纯处理性质见正文 |
| `services/client_link.py` | 104 | F | F | 全文；实际DB、委托、或明确纯处理性质见正文 |
| `services/command_service.py` | 231 | I | R | Work.cancel/People.delete_person_id及各领域管理委托；cancel join在writer之外；实参/owner核对，非全文 |
| `services/commands/__init__.py` | 1 | S | S | 复核完整导入/函数与实际调用面：纯类型、格式/计算、网络/文件或进程内状态；无自管SQLite/隐式ORM写 |
| `services/commands/admin.py` | 154 | I | F | 全文；实际DB、委托、或明确纯处理性质见正文 |
| `services/commands/conversation.py` | 117 | I | F | 全文；实际DB、委托、或明确纯处理性质见正文 |
| `services/commands/group.py` | 14 | I | S | 复核完整导入/函数与实际调用面：纯类型、格式/计算、网络/文件或进程内状态；无自管SQLite/隐式ORM写 |
| `services/commands/help.py` | 153 | I | S | 复核完整导入/函数与实际调用面：纯类型、格式/计算、网络/文件或进程内状态；无自管SQLite/隐式ORM写 |
| `services/commands/identity.py` | 216 | I | R | 全部真实query/revision/prepare_chat_command/identity mutations实参核对；owner在admin operations；实参/owner核对，非全文 |
| `services/commands/memory.py` | 564 | I | R | 全部MemoryAdmin/Rebuild/Dream/embedding实际命令委托；没有隐藏begin；实参/owner核对，非全文 |
| `services/commands/models.py` | 46 | F | F | 全文；实际DB、委托、或明确纯处理性质见正文 |
| `services/commands/plugins.py` | 81 | I | R | plugin query/config/revision/control owner委托；无自管事务；实参/owner核对，非全文 |
| `services/commands/work.py` | 49 | I | R | Work query/revision/control owner委托；无自管事务；实参/owner核对，非全文 |
| `services/concurrency.py` | 95 | I | R | 内存任务/信号量；无数据库；实参/owner核对，非全文 |
| `services/config_commands.py` | 286 | I | R | Config revision/override/unset/rollback与原control request委托；实参/owner核对，非全文 |
| `services/context_assembler.py` | 501 | I | R | ledger.read_version_matches/read_scope_context与scopes.prepare_prompt_snapshot/get；无新增writer；实参/owner核对，非全文 |
| `services/context_boundary.py` | 55 | F | S | 复核完整导入/函数与实际调用面：纯类型、格式/计算、网络/文件或进程内状态；无自管SQLite/隐式ORM写 |
| `services/context_compaction.py` | 652 | M | M | CompactReceipt写入138–172完整writer及读/prepare/publication依赖；容量/格式结构排除，非全文 |
| `services/deduplication.py` | 14 | S | S | 复核完整导入/函数与实际调用面：纯类型、格式/计算、网络/文件或进程内状态；无自管SQLite/隐式ORM写 |
| `services/document_reader.py` | 214 | S | S | 复核完整导入/函数与实际调用面：纯类型、格式/计算、网络/文件或进程内状态；无自管SQLite/隐式ORM写 |
| `services/durable_invocations.py` | 299 | F | M | 恢复原Work/原not_before只读SQL及activation/runner委托，已付身份与原source实参复核，非全文 |
| `services/effect_gate.py` | 65 | I | R | 内存permit与validate(snapshot)委托；不持DB连接等锁；实参/owner核对，非全文 |
| `services/evidence_observation.py` | 92 | I | F | 全文；实际DB、委托、或明确纯处理性质见正文 |
| `services/evidence_state.py` | 39 | I | S | 复核完整导入/函数与实际调用面：纯类型、格式/计算、网络/文件或进程内状态；无自管SQLite/隐式ORM写 |
| `services/execution_sources.py` | 599 | M | M | 全部自管reader SQL块235–599及原event/Automation/SELF/background source/身份谓词，非纯AST排除 |
| `services/group_recovery.py` | 131 | F | S | 复核完整导入/函数与实际调用面：纯类型、格式/计算、网络/文件或进程内状态；无自管SQLite/隐式ORM写 |
| `services/history_projection.py` | 221 | I | F | 全文；实际DB、委托、或明确纯处理性质见正文 |
| `services/history_query.py` | 73 | I | F | 全文；实际DB、委托、或明确纯处理性质见正文 |
| `services/http_logging.py` | 40 | S | S | 复核完整导入/函数与实际调用面：纯类型、格式/计算、网络/文件或进程内状态；无自管SQLite/隐式ORM写 |
| `services/image_preprocessor.py` | 212 | I | S | 复核完整导入/函数与实际调用面：纯类型、格式/计算、网络/文件或进程内状态；无自管SQLite/隐式ORM写 |
| `services/invocation_context.py` | 54 | F | S | 复核完整导入/函数与实际调用面：纯类型、格式/计算、网络/文件或进程内状态；无自管SQLite/隐式ORM写 |
| `services/invocation_service.py` | 328 | M | M | invoke76–328全文：T1/T2/外部/T3、typed outcome/unknown、late receipt、audit顺序 |
| `services/main_agent_backend.py` | 753 | I | F | 全文；实际DB、委托、或明确纯处理性质见正文 |
| `services/main_agent_contract.py` | 153 | F | R | 实际持久调用委托：self.chat._runtime_config.snapshot；对应owner已列，非全文 |
| `services/main_agent_turns.py` | 664 | M | M | compose全部publication/observation/prepare/commit callbacks176–510，adopt/read_result/run512–664；writer内仅已备publication，非全文 |
| `services/media_process.py` | 25 | I | S | 复核完整导入/函数与实际调用面：纯类型、格式/计算、网络/文件或进程内状态；无自管SQLite/隐式ORM写 |
| `services/media_resolver.py` | 625 | I | S | 复核完整导入/函数与实际调用面：纯类型、格式/计算、网络/文件或进程内状态；无自管SQLite/隐式ORM写 |
| `services/message_splitter.py` | 358 | S | S | 复核完整导入/函数与实际调用面：纯类型、格式/计算、网络/文件或进程内状态；无自管SQLite/隐式ORM写 |
| `services/model_context_projection.py` | 10 | S | S | 复核完整导入/函数与实际调用面：纯类型、格式/计算、网络/文件或进程内状态；无自管SQLite/隐式ORM写 |
| `services/native_media.py` | 126 | I | S | 复核完整导入/函数与实际调用面：纯类型、格式/计算、网络/文件或进程内状态；无自管SQLite/隐式ORM写 |
| `services/native_tool_binder.py` | 87 | I | S | 复核完整导入/函数与实际调用面：纯类型、格式/计算、网络/文件或进程内状态；无自管SQLite/隐式ORM写 |
| `services/participation_diagnostics.py` | 232 | I | F | 全文；实际DB、委托、或明确纯处理性质见正文 |
| `services/participation_feedback.py` | 669 | F | M | _read_facts349–513/_commit_pending516–560与reconcile完整提交边界；完整效果读集与64-ref写页区别，非全文 |
| `services/participation_identity.py` | 36 | F | F | 全文；实际DB、委托、或明确纯处理性质见正文 |
| `services/participation_ordinary.py` | 590 | M | F | 全文；实际DB、委托、或明确纯处理性质见正文 |
| `services/participation_parameters.py` | 13 | S | S | 复核完整导入/函数与实际调用面：纯类型、格式/计算、网络/文件或进程内状态；无自管SQLite/隐式ORM写 |
| `services/participation_snapshot.py` | 96 | F | F | 全文；实际DB、委托、或明确纯处理性质见正文 |
| `services/participation_trace.py` | 91 | F | F | 全文；实际DB、委托、或明确纯处理性质见正文 |
| `services/plugin_events.py` | 64 | I | F | 全文；实际DB、委托、或明确纯处理性质见正文 |
| `services/plugin_sessions.py` | 356 | F | M | 实际require_live_actor reader134–140、session repository调用与模型在事务外的链路；非全文 |
| `services/policies.py` | 171 | I | S | 复核完整导入/函数与实际调用面：纯类型、格式/计算、网络/文件或进程内状态；无自管SQLite/隐式ORM写 |
| `services/processing_status.py` | 148 | F | S | 复核完整导入/函数与实际调用面：纯类型、格式/计算、网络/文件或进程内状态；无自管SQLite/隐式ORM写 |
| `services/processor.py` | 2121 | M | M | ordinary准备/回调和supplied-session continuation_current、ASR/策略实际DB委托及本次delta，非全文 |
| `services/profile_commands.py` | 137 | I | F | 全文；实际DB、委托、或明确纯处理性质见正文 |
| `services/prompt_composer.py` | 159 | F | S | 复核完整导入/函数与实际调用面：纯类型、格式/计算、网络/文件或进程内状态；无自管SQLite/隐式ORM写 |
| `services/prompt_registry.py` | 102 | S | S | 复核完整导入/函数与实际调用面：纯类型、格式/计算、网络/文件或进程内状态；无自管SQLite/隐式ORM写 |
| `services/qq_face_resolver.py` | 55 | S | S | 复核完整导入/函数与实际调用面：纯类型、格式/计算、网络/文件或进程内状态；无自管SQLite/隐式ORM写 |
| `services/rate_limit.py` | 79 | S | S | 复核完整导入/函数与实际调用面：纯类型、格式/计算、网络/文件或进程内状态；无自管SQLite/隐式ORM写 |
| `services/renderer.py` | 82 | S | S | 复核完整导入/函数与实际调用面：纯类型、格式/计算、网络/文件或进程内状态；无自管SQLite/隐式ORM写 |
| `services/semantic_participation.py` | 1509 | M | M | start/close194–237、health/scene293–419、retire/load/save427–580、hydrate/source682–926、admit/dispatch/reconcile/tick927–1504全部实际DB函数；非纯控制器分支全文 |
| `services/subagent_execution.py` | 385 | M | M | 全部实际reader SQL块、外部cancel委托的会话关闭边界与原source恢复实参，非全文 |
| `services/turn_coordinator.py` | 377 | I | R | 内存消息版本/取消/join；类型导入不等于writer；实参/owner核对，非全文 |
| `services/turn_execution.py` | 1679 | M | M | paid恢复/compact publication与WorkSourceGuard DB调用链、正常请求participation读取及本次delta，非全文 |
| `services/turn_transcript.py` | 243 | I | S | 复核完整导入/函数与实际调用面：纯类型、格式/计算、网络/文件或进程内状态；无自管SQLite/隐式ORM写 |
| `services/video_frames.py` | 142 | S | S | 复核完整导入/函数与实际调用面：纯类型、格式/计算、网络/文件或进程内状态；无自管SQLite/隐式ORM写 |
| `services/vision_rate_limit.py` | 62 | S | S | 复核完整导入/函数与实际调用面：纯类型、格式/计算、网络/文件或进程内状态；无自管SQLite/隐式ORM写 |
| `services/vision_service.py` | 609 | I | M | _analyze244–465全文：media/provider在外、find_cached/save owner调用与失败出口，非全文 |
| `services/work_resume.py` | 439 | M | M | 全部reader SQL块、原recover_source/lease/journal/runner委托，非全文 |
| `services/yuki_runtime.py` | 87 | I | R | 生命周期委托Workers，最后drain；没有自管DB；实参/owner核对，非全文 |

分类计数：F 43、M 47、R 32、D 14、S 75，合计211。

截至本轮，没有仍未定位实际owner的主库/独立库调用；前轮点名的真实DB遗留函数已补，不再仅标I待接力。纯结构排除不用于证明事务语义，间接消费者由表内实际owner承担，未把全部211称逐字读过。

仍未解决的是实施与实证问题：锁定driver取消后的物理退出、unknown跨工具/SDK传播、完整跨源复核SQL与成本、notification合并提交/旧状态兼容、plugin媒体提交未知保文件、规模下前后台延迟。它们已经列为任务书实施/验证事项，静态审查不能提前消除。本文不对新FIFO宣称通过、合并或上线。
