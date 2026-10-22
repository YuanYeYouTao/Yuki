# SQLite 实现后交叉复审 R2：C 域（A reviewer）

冻结基线 `f8fbe14d`。本轮 reviewer 原先实现 A、R1 复审 Memory，现在独立轮转 C。
这不是基于 diff 的复审；全部 211 路径逐项保留，实际 DB 方法与间接编排按源核读。
本轮已完成全部 211 路径的数据库实现、间接调用或明确排除分类。第一批 paired 期间只读；root 放行后完成下述修复与定向。未运行真实模型、外部平台网络或生产。产品编辑与测试现已停止，可冻结进入第三轮。

## 已确认发现与处理

1. Subagent 原 message 双层正文、Work accept/derive 原 source、Work failure 已知正文仍在 writer 内编码。
   已把原已知 body/source 编码移到准入前；`accept_in_session` 要求调用者给 `prepared_source_json`，
   六处实际消费者全部迁移，没有留锁内编码 fallback。新 event ID、revision 和真实结果的小回执仍在原事务生成。
2. Notification 原正文准备存在多处锁内解码：检查 message 键解整个 prepared plan、claim 返回时解 outbox DTO，
   publication 新 event DML 后重新解/写 segments metadata，并发旧 publication 路径还解原 manifest。
   已改窄 SQL 键存在判断、writer 退出后的 DTO、提前 prepare_append；并发旧 publication 空 writer 退出，
   由 reader 消费原 event/句柄。原 event/wait/job/outbox 原子单位和过 TTL 的原 receipt 资格保留。
3. Plugin KV CAS、PluginAuditRepository 和 session append 返回时解大 JSON 仍持 writer。
   已把原 row→DTO 转换放在 owned writer 出口之后；原版本/owner/TTL/序号仍同一事务核验。
4. MainAgentBackend 的泛异常路径按工具名义 effect 猜 uncertain，WorkControl 的业务异常转换也可盖掉真实提交阶段。
   现在 Main binding 和 AgentTools 共用真实 phase/committed/retryable 映射；WorkControl 在 actual phase 未确认回滚时
   保三态进入 ResultCapture。确认提交后呈现/关闭失败仍保 True；失去 commit ACK 保 None，不赋 False 或自动重放。
   真实 Automation create/pause binding 丢 ACK 回归由 D reviewer 编写，已纳入本轮联合定向。
5. Social 正常发送结算 supplied writer 内，Work delivery 回执错误被日志化后可提交部分 ledger/Social 状态。
   已让同事务的 Work 回执错误传播并回滚；已有物理发送的失败收尾仍使用原 confirmed receipt 记录，
   不生成虚假的 outbound event、不重复网络发送。独立的原 receipt 对账保原有 best effort 行为。
6. Social source 查询为读取原 publication actor 装载完整 event/segments，来源存在性也分成多次点读。
   已以原 plugin/source_person JSON scalar 与 Conversation 一次窄 join，来源存在性以同笔 EXISTS 合并；
   原 actor、target、event、privacy、当前 background grant 资格均保留。Work management 只取原需要的
   classification scalar，不再装载 checkpoint 并解完整 source。没有删除真实证明来换 SQL 数量。
7. Emoji 独立 intent 对带真实 write phase 的 OSError/RuntimeError 当业务失败关闭，公共恢复循环又只识别 SQLAlchemyError。
   已传播实际数据库失败并沿原恢复循环等待；确认回滚的 SocialError/WorkspaceError 仍关闭真正失效 intent。
   公共 loop 不增加新重试层、预算或 replay 权限。三处实际调用者的恢复事实见下表。
8. MemoryAdmin mutation 分支完成后再写三笔独立管理审计，Preference set/delete 另写两笔；后置审计失败可覆盖
   已经提交的原事实/receipt。经 root 与 D reviewer 独立核消费者后，删除这五处后置写及 Preference 专属注入/计时，
   顺删 delete_memory 仅供旧审计使用的 get_fact。保 fallback 的事实+audit 同 session 和 Control 真实审计。
   原 mutation/fact/event 留 actor/operation/owner/内容演进；少掉的 capability/target/duration 等摘要没有生产消费者，
   不声称两套审计逐字段相同。当前 memory-v2 合同已同步真实边界。

没有为这些改动新增全库 revision、通用 Plan、writer 内历史 fallback、第二条重试队列或持久辅助资格。

## 当前语义核查记录

- WorkWait：reader 解原 conditions/completion，writer 复核实际 active wait 集合（包含新增匹配成员）和 owned-run 集合/原 payload。
  `_deliver` 与原 mailbox/status/wake 同提交；due 在获得 writer 后取时钟；`register` 的 `after_event_id` 在原 writer 取最大原事件号。
  match/reply 保内部 event_id；小型当前 predicate 和新 signal 编码没有被替换成通用 proof framework。
- Subagent：维护准备真实整树/当前候选，writer 重查 selected eligibility、root retention 和 ResourceCleanup 来源；目录页未作为执行全集。
  finish reader 解原 checkpoint，再按原 revision/state/parent/bytes CAS；原 result 与 parent 输入同事务。
- Protocol：publication file fence → reader 现有 refs → writer；GC mark writer 先释放，再 file fence 下 reader/unlink，再释放 fence 后 ack writer。
  实际线程取消 join 保留；GC 不持 file fence 等待 SQLite writer。
- WorkSourceGuard：hash 与完整来源读在显式 reader snapshot；writer 只核 scalar/current selected cross facts。
  原 canonical revision/identity/privacy 证明不被新的全库 revision 替代；跨会话原消息与当前已接纳 mailbox 身份同时核验。

## 恢复调用者与交叉核验

| recover_database_loop 实际调用者 | 原恢复依据与 unknown 后行为 |
| --- | --- |
| EmojiDeliveryService._run → _run_queue | 只分页读取原 pending intent ID；原进程 `_deliveries` 拥有 task，不在队列扫描中调用模型或发送。 |
| EmojiDeliveryService._run_intent → resume/_deliver | 重新读取同一 intent，已选择 asset/sha 原件复用；先看 Social 原执行 receipt，executing/uncertain 不重新 claim/send。发布沿原 request_id 和不可变 snapshot。真实 selection commit 丢 ACK 只选一次，claim commit 丢 ACK 保 executing 并投影 uncertain，未发送图片。 |
| PluginNotificationOutboxWorker._run → _run_queue | 原 pending→processing claim/attempt/lease；丢 claim ACK 后不是 pending，不再派发；原 lease 到期转 uncertain。发送后原 outbox sent/uncertain 均不可重 claim，原 ledger/outbox receipt 不被新异常改写。无模型调用。 |

- Ordinary continuation：R1 的 actor 集合补集和 strict resolver 重新核对；当前 unit/boundary/candidate 谓词仍由原 controller
  在 writer 检查，历史 actor 收集在 reader。没有 state pointer gate、checkpoint witness 或 controller 副本。
- B 删除 feedback 外层重复 run source 证明后，本轮读完整 `participation_feedback.py`：writer 仍核真实 privacy、Work、
  选中的原 Social/tool/model refs；无真实 effect 时的 no_reply absence 仍包括新增匹配 receipt；repository 的原 source/sequence/replay 同提交。
- B 后续优化 ordinary 时曾漏 Work repair 的 nested import。现已独立读修正后的 `repair_abandoned_inputs`：
  同 reader 核原 event、Conversation/generation/keeper/floor 及共用纯 SQL 身份谓词；body 准备在 reader，
  原 publisher 在 writer 当前核验，unknown 不 discard 或另造身份。该修复与 83 项定向归 B。
- Plugin capability 后置 audit 疑点经追实际服务排除：`PluginAuditService.record` 本来捕获全部 Exception 并日志化，
  不用 fake audit 抛异常给 adapter 新增防御分支；原 PluginAudit 行/health/SDK 合同保留。
- 命令适配器保既有泛“未完成/执行失败”提示。这只是呈现，不作为确认 rollback、typed committed=False 或重派依据；
  本轮没有为所有命令增加新错误框架。Model/Manager/Protocol 的实际持久 receipt 和权威字段仍逐项核验。

## 本轮验证

以下组有重叠，不能累加为总通过数；全部本地固定模型/平台 fixture，没有真实付费调用。

| 定向组 | 实际结果 | 覆盖 |
| --- | --- | --- |
| notification/idempotency/source + subagent + Social + Automation unknown binding | 101 passed / 26.16s | notification/source/幂等、子任务、Social交付、真实 Main binding 丢 ACK |
| subagent + Social prepared + notification（加入新原 body/结算测试后） | 91 passed / 19.99s | 20K child原正文、writer前source、原outbox大计划、三方结算回滚与不重发 |
| storage writer + Plugin session/material/config/person merge + Social | 46 passed / 18.17s | 600KB KV值、原session metadata在writer外解码，原资格与person lineage |
| subagent + Work settlement/owner recovery + ACP Work cancel | 129 passed / 104.10s | 原parent/children/lease/budget/ACK、外部资源清理恢复 |
| Emoji recovery全文件 + 新Admin原receipt + background worker recovery | 34 passed / 20.24s | OSError/RuntimeError×selection/claim 真实提交后丢ACK；五次真实Memory/Preference mutation不再调用后置audit |
| WorkControl实际unknown ACK + 上述Emoji4项追加小组 | 5 passed / 6.46s | 实际目标已写入、一次commit、ResultCapture phase/None语义 |

- 修改源静态检查：先后 8 源、5 源和最终 MemoryAdmin/Preference/装配/Emoji/common loop/WorkControl 6 源 mypy 通过。
  Ruff/format按本轮改文件通过；全仓静态/整体回归由 root 统一执行，不能把局部结果写作全仓验收。
- 只有原有 metadata 表 FK 环 SAWarning，未出现测试失败。开发新回归时曾复用同内部 event 导致原mutation幂等结果，
  已改成五个真实独立入站 event，验证五笔原 receipt；不据测试夹具误用修改产品幂等语义。
- 没有性能胜负结论：本轮做源语义与定向。paired 到达负载/完成事实和计时由根报告汇总，不能凭少 BUSY 宣称提速。

## 范围结论

211 路径全部给出等级，没有“待审”。F 为本轮源全文，D 为全部实际 DB 方法/具体事务与调用边界，
I 为无自有数据库事务的间接 owner 编排；S 为逐函数 imports/完整调用树排除的类型、纯计算、内存或文件/网络层。
D/I/S 不伪称整个纯算法或展示文案逐字全文。表中具名边界与 caller 覆盖是语义范围，AST 只用于定位和排除导航。
另补跨域 `emoji/delivery.py` 与经 root 授权的公共 `persistence/worker_recovery.py` 实际消费者。
没有留给下一轮的未具名真实 DB 实现方法；Memory 内部 prepared-count fallback 与 SELF 优化由 root 独立 R2 所有，
本报告不代替 root 的 Memory 全域结论。第三轮应再挑战本轮全部修复及 root/B 同笔优化后的真实源证明与吞吐。

## 逐路径状态

F = 本轮全文；D = 全部实际 DB 方法及其调用边界（不声称非 DB 业务全文）；
I = 间接编排与实际 owner 调用已核；S = 明确排除的类型/声明/纯计算、文件网络与内存协调。

| 路径（src/yuki/） | 等级 | 本轮范围 |
| --- | --- | --- |
| `runtime/__init__.py` | S | 结构排除：本轮AST逐项核imports/所有调用，仅export/字段/约束/索引/默认值构造，无业务DB会话或执行入口；不计DB语义全文 |
| `runtime/activation_bindings.py` | F | 全文；进程内 owner/完成通知，不裁决持久 lease |
| `runtime/activation_outcome.py` | F | 全文失败分类 actual phase/三态/BUSY 原码；无重放推断 |
| `runtime/activation_tasks.py` | F | 全文；停止接纳并 join 原 tasks，释放持久资源前收尾 |
| `runtime/context_preparation.py` | F | 全文 1-116；先选原paid protocol/guard，再构造当前上下文，无writer |
| `runtime/contracts.py` | S | 全文纯 MemoryCapabilityView 声明，无 DB 调用 |
| `runtime/delivery.py` | S | 全文 delegation 数据 |
| `runtime/delivery_intents.py` | F | 全文 reserve/record；同 lease/current Work/原 key/payload，late accepted 可升级 unknown |
| `runtime/effect_outcomes.py` | F | 全文 ResultCapture/evidence/readonly signature/历史真实 receipt 投影，无 owned DB |
| `runtime/effect_queries.py` | F | 全文 inspect_social_operation 原 reader snapshot/facts |
| `runtime/effect_schema.py` | F | 全文 install_indexes 与真实 effect invocation SQL installer；DDL owner 仍归初始化/迁移 |
| `runtime/errors.py` | S | 全文错误类型，无 DB 调用 |
| `runtime/execution_receipts.py` | F | 全文 confirm 原 request→observed→Work receipt，不在 writer 外重发 |
| `runtime/lease_heartbeat.py` | F | 全文 1-118；原到期下界、只识别 retryable BUSY、实际 worker join |
| `runtime/observability.py` | S | 全文 ID/hash/ContextVar/计时结构，无 DB I/O |
| `runtime/origin.py` | S | 全文 origin enum，无 DB I/O |
| `runtime/principal.py` | S | 全文可信 principal 数据，无 DB I/O |
| `runtime/protocol_schema.py` | F | 全文 install_quota→QUOTA_SQL；objects/refs/quota 当前触发器，未混称冻结迁移等价性 |
| `runtime/protocol_store.py` | F | 全文 1-677；对象准备/publication/publish_refs/metadata GC/文件 fence/ack 与取消 join |
| `runtime/subagent_repository.py` | F | 全文 1-740；start/acquire/maintain/archive/related/list/unfinished/message/resume/cancel/finish，当前集合与原结果 |
| `runtime/subagent_scheduler.py` | F | 全文 1-137；候选 reader、当前 acquire、取消 join 和通知恢复 |
| `runtime/subagent_schema.py` | S | 结构排除：本轮AST逐项核imports/所有调用，仅export/字段/约束/索引/默认值构造，无业务DB会话或执行入口；不计DB语义全文 |
| `runtime/subagent_tools.py` | I | 全文调用实际 repository/WorkControl，result只解原结果，不再入writer |
| `runtime/trigger.py` | S | 全文 host trigger 字段合同，无 DB I/O |
| `runtime/work_activation.py` | F | 全文 1-214；租约、实际owner、heartbeat与settle/release失败三态 |
| `runtime/work_budget.py` | F | 全文原 ancestor root 与 budget SQL 原子扣费，供给 session 不重开 |
| `runtime/work_budget_schema.py` | F | 全文 current budget schema installer，真实独立初始化 connection |
| `runtime/work_context_note.py` | F | 全文 validate_note/visible_context_note；reader snapshot 原 refs、来源/隐私，writer 发布由 repo 核 |
| `runtime/work_control.py` | F | 全文 1-1160；全部输入/receipt/control/accept/derive/wait/handoff；unknown/committed phase不降成False |
| `runtime/work_journal.py` | F | 全文 1-766；load/private hydration、source guard、save/publication、invalidate、original effect result、媒体与未知回执恢复 |
| `runtime/work_management.py` | F | 全文 resume_blocker/stop_owned_execution/manage_work/view；原子树+原管理CAS，恢复窄classification替代完整source解码 |
| `runtime/work_media.py` | S | 全文纯 immutable media hash/externalize/hydrate，无 DB I/O |
| `runtime/work_observation_schema.py` | S | 结构排除：本轮AST逐项核imports/所有调用，仅export/字段/约束/索引/默认值构造，无业务DB会话或执行入口；不计DB语义全文 |
| `runtime/work_queries.py` | F | 全文 get/available/recent/list，实际 scoped 目录只读，状态分页同 snapshot，不用于管理执行全集 |
| `runtime/work_query_schema.py` | F | 全文 scope 字段 SQL/当前索引生成，生产改表仍沿冻结迁移 |
| `runtime/work_recovery_schema.py` | S | 结构排除：本轮AST逐项核imports/所有调用，仅export/字段/约束/索引/默认值构造，无业务DB会话或执行入口；不计DB语义全文 |
| `runtime/work_repository.py` | D | 全部具名 DB 方法 100-2844；lease/accept/derive/commit_state/checkpoint/inputs/privacy/completion/T1-T2/effect CAS 与 original bytes |
| `runtime/work_scheduler.py` | F | 全文维护/候选/lease调度/取消join；每scope顺序、原scheduler owner不持DB等模型 |
| `runtime/work_schema_v1.py` | S | 结构排除：本轮AST逐项核imports/所有调用，仅export/字段/约束/索引/默认值构造，无业务DB会话或执行入口；不计DB语义全文 |
| `runtime/work_session.py` | F | 全文 1-1049；restore/rebase/paid response save、readonly原键、context compact与确认回滚后的同journal重备 |
| `runtime/work_source_guard.py` | F | 全文 1-442；真实 read snapshot、现行 scalar proof、cross selected values 和 writer 验证 |
| `runtime/work_supervisor.py` | F | 全文 recover_failure/settle；原accepted控制优先、未知付费receipt保留；已知failure JSON移writer前 |
| `runtime/work_tree.py` | F | 全文递归 SQL 父链与 descendants/ancestor/budget 原关系；同 session 小查询 |
| `runtime/work_wait.py` | F | 全文 1-1230；register/cancel/prepare-current/apply_event/reply/due 的原 source、phantom、时钟与同 mailbox 原子单位 |
| `runtime/work_wait_schema.py` | S | 结构排除：本轮AST逐项核imports/所有调用，仅export/字段/约束/索引/默认值构造，无业务DB会话或执行入口；不计DB语义全文 |
| `model_runtime/__init__.py` | S | 逐项核imports/函数与全调用树：声明、纯计算/内存协调或文件转换，不接主库/独立DB，不计DB语义全文 |
| `model_runtime/capacity.py` | S | 全文容量/JSON token 计算，无 DB I/O |
| `model_runtime/db_models.py` | S | 结构排除：本轮AST逐项核imports/所有调用，仅export/字段/约束/索引/默认值构造，无业务DB会话或执行入口；不计DB语义全文 |
| `model_runtime/dispatch_guard.py` | I | 全文实际 provider dispatch ContextVar callback，不持 DB owner |
| `model_runtime/executor.py` | F | 全文 1-1021；provider/quiet/slots 均在主 DB writer 外，诊断失败不撤销模型成功 |
| `model_runtime/models.py` | S | 逐项核imports/函数与全调用树：声明、纯计算/内存协调或文件转换，不接主库/独立DB，不计DB语义全文 |
| `model_runtime/pool.py` | S | 逐函数/全调用树核无数据库；Provider HTTP pool资源与关闭，纯网络owner排除 |
| `model_runtime/profiles.py` | S | 逐项核imports/函数与全调用树：声明、纯计算/内存协调或文件转换，不接主库/独立DB，不计DB语义全文 |
| `model_runtime/repository.py` | F | 全文 record/prepare_write/stats/recent_errors；预冻结诊断经原 privacy/source guards，读取纯 reader |
| `model_runtime/request_accounting.py` | S | 全文 physical attempt usage，缺字段未知，无 DB I/O |
| `model_runtime/routes.py` | S | 逐项核imports/函数与全调用树：声明、纯计算/内存协调或文件转换，不接主库/独立DB，不计DB语义全文 |
| `model_runtime/search_defaults.py` | S | 逐项核imports/函数与全调用树：声明、纯计算/内存协调或文件转换，不接主库/独立DB，不计DB语义全文 |
| `model_runtime/secrets.py` | S | 逐函数/全调用树核为专属secret原子文件读写，无数据库或主库事务 |
| `model_runtime/structured.py` | I | run_with_response102–262完整；序列化/模型外部工作不持writer，paid次数与诊断委托已审ModelExecutor，不改变显式validation_retries |
| `execution_trace/__init__.py` | S | 结构排除：本轮AST逐项核imports/所有调用，仅export/字段/约束/索引/默认值构造，无业务DB会话或执行入口；不计DB语义全文 |
| `execution_trace/db_models.py` | S | 结构排除：本轮AST逐项核imports/所有调用，仅export/字段/约束/索引/默认值构造，无业务DB会话或执行入口；不计DB语义全文 |
| `execution_trace/payload.py` | S | 逐项核imports/函数与全调用树：声明、纯计算/内存协调或文件转换，不接主库/独立DB，不计DB语义全文 |
| `execution_trace/phases.py` | S | 全文内存计时/ContextVar，不包装DBdriver或增加holder状态 |
| `execution_trace/recorder.py` | F | 全文 freeze/prepare/conditionalINSERT/coverage/TTL/trace scope；实际压缩threadjoin，诊断可丢不阻塞paid成功 |
| `social/__init__.py` | S | 结构排除：本轮AST逐项核imports/所有调用，仅export/字段/约束/索引/默认值构造，无业务DB会话或执行入口；不计DB语义全文 |
| `social/agent_adapter.py` | D | execute完整，原reply/同人目标固定ID reader授权，再委托Social原receipt，不持writer调用平台 |
| `social/confirmed_content.py` | F | 全文；合并原所有存在事实为同笔 bool EXISTS，不加载原正文/ORM |
| `social/content.py` | D | 全部固定SQL常量23–111与prepare_for_entry180–246；名字/账号原成员、歧义近期top1 indexed查询，全部writer前；纯render已核无DB |
| `social/db_models.py` | S | 结构排除：本轮AST逐项核imports/所有调用，仅export/字段/约束/索引/默认值构造，无业务DB会话或执行入口；不计DB语义全文 |
| `social/emoji_intents.py` | F | 全文 1-410；source/admit/select/close；预编码意图、当前授予，选择不冒充发送 |
| `social/history.py` | F | 全文 read_history；原 reply/transport/receipt 与当前 ACL 读投影，BEGIN 一致 snapshot，无 owned writer |
| `social/models.py` | S | 逐项核imports/函数与全调用树：声明、纯计算/内存协调或文件转换，不接主库/独立DB，不计DB语义全文 |
| `social/repository.py` | F | 全文 1-716；原 prepared/claim/finish/恢复/sequence/facts，原 receipt DTO 解码与付费效果分离 |
| `social/service.py` | F | 全文 1-1870；实际 route/authority/claim/send/settle/replay/recall，修 supplied Work receipt 错误吞掉及原 publication decode |
| `social/source_keys.py` | S | 逐项核imports/函数与全调用树：声明、纯计算/内存协调或文件转换，不接主库/独立DB，不计DB语义全文 |
| `social/tools.py` | S | 逐项核imports/函数与全调用树：声明、纯计算/内存协调或文件转换，不接主库/独立DB，不计DB语义全文 |
| `plugin_host/__init__.py` | S | 结构排除：本轮AST逐项核imports/所有调用，仅export/字段/约束/索引/默认值构造，无业务DB会话或执行入口；不计DB语义全文 |
| `plugin_host/admission_adapter.py` | I | collect/_collect_one/_scope/_invoke_provider完整；插件hook无owned DB事务，optional signal failure不构造执行receipt |
| `plugin_host/approval.py` | I | 全文 approval protocol/in-memory test store；真实 manifest 权限事实传仓库 |
| `plugin_host/audit.py` | I | 全文 best-effort audit facade，失败不裁决业务结果 |
| `plugin_host/background_turns.py` | F | 全文claim/validate/park/defer/resume/settle，同原job+Work，prepare/provider不持DBwriter |
| `plugin_host/canonical_projection.py` | S | 全文仅复制 host stamp，不反造 Person/Conversation |
| `plugin_host/capability_adapter.py` | D | execute140–241/current availability280–305/media授权/record338–354完整；reader资格后handler，实际PluginAuditService全Exception日志化，排除后置audit覆盖假阳性 |
| `plugin_host/command_adapter.py` | I | execute/direct/_execute_registered_command/_scope全实际调用边界；原Principal/current Plugin scope/handler，无本文件事务，泛失败仅展示不重写receipt |
| `plugin_host/config.py` | F | 全文 scoped facade/schema validator writer外，沿实际config CAS，不另开嵌套事务 |
| `plugin_host/configuration_service.py` | F | 全文 read/save；schema/plugin validator writer 外，writer 核原配置行集合/版本并原子替换 |
| `plugin_host/db_models.py` | S | 结构排除：本轮AST逐项核imports/所有调用，仅export/字段/约束/索引/默认值构造，无业务DB会话或执行入口；不计DB语义全文 |
| `plugin_host/direct_command_router.py` | S | 逐项核imports/函数与全调用树：声明、纯计算/内存协调或文件转换，不接主库/独立DB，不计DB语义全文 |
| `plugin_host/direct_delivery.py` | F | 全文 prepare_delivery/validate_delivery_source；reader 准备原作者/当前 scope，claim writer 消费原 check |
| `plugin_host/discovery.py` | S | 逐项核imports/函数与全调用树：声明、纯计算/内存协调或文件转换，不接主库/独立DB，不计DB语义全文 |
| `plugin_host/event_bus.py` | I | 所有hook/notification调度与await回调边界；内存队列、active任务停止join，本文件不持SQLite事务/裁决业务receipt |
| `plugin_host/extension_registry.py` | S | 逐项核imports/函数与全调用树：声明、纯计算/内存协调或文件转换，不接主库/独立DB，不计DB语义全文 |
| `plugin_host/facades.py` | D | 全部实际DB方法及消费者：784-1567、1739-2032、2435-2723、3077-3108；history/People/Memory/Agent/Notification/Automation/KV与同Session send settlement |
| `plugin_host/http_client.py` | S | 逐函数全调用树排除；HTTP/DNS/权限/限流网络owner，无数据库调用 |
| `plugin_host/loader.py` | S | 逐项核imports/函数与全调用树：声明、纯计算/内存协调或文件转换，不接主库/独立DB，不计DB语义全文 |
| `plugin_host/main_turn.py` | F | 全文原 plugin source/approval/执行ID/付费恢复；模型/历史准备无DBwriter，原lease与同Work budget保留 |
| `plugin_host/manager.py` | D | 全部实际安装/批准/启停/审计方法225-379、451-859；lifecycle锁外于各SQLite事务，插件callbacks不持DBwriter |
| `plugin_host/manifest.py` | S | 逐项核imports/函数与全调用树：声明、纯计算/内存协调或文件转换，不接主库/独立DB，不计DB语义全文 |
| `plugin_host/media_artifacts.py` | F | 全文 create/resolve/read_owned/cleanup；hash/file 外提，writer recheck quota/expiry/ref，原注册未知回执不重做 |
| `plugin_host/models.py` | S | 结构排除：本轮AST逐项核imports/所有调用，仅export/字段/约束/索引/默认值构造，无业务DB会话或执行入口；不计DB语义全文 |
| `plugin_host/notification_delivery.py` | F | 全文 1-639；prepare/claim/transport/finish+ledger 原子，已确认发送不重发 |
| `plugin_host/notification_manifest.py` | S | 全文原 publication identity/hash 纯准备 |
| `plugin_host/notification_repository.py` | F | 全文 1-2272；grant/publish/wait/outbox/job、original source/usage/原receipt/route helper全方法；记录3处原JSON准备遗漏 |
| `plugin_host/ownership.py` | F | 全文 1-517；实际 owner/族/lineage/absence/current grant 点查 |
| `plugin_host/prompt_adapter.py` | S | 逐项核imports/函数与全调用树：声明、纯计算/内存协调或文件转换，不接主库/独立DB，不计DB语义全文 |
| `plugin_host/repository.py` | F | 全文 1-944；installation/config/state/audit 全部方法；新值 encode 外提，CAS DTO decode 移到 owned writer 后 |
| `plugin_host/secrets.py` | S | 逐项核imports/函数与全调用树：声明、纯计算/内存协调或文件转换，不接主库/独立DB，不计DB语义全文 |
| `plugin_host/session_facade.py` | I | 全文 SDK session authority→原 service/repository，未另建 DB owner |
| `plugin_host/session_repository.py` | F | 全文 create/get/actor/append/list/close/reset/delete/expiry；原 owner+sequence 原子，metadata DTO decode 移 writer 后 |
| `plugin_host/storage.py` | F | 全文 KV facade；原 quota/CAS/namespace，facade 锁在 writer 外，无网络 |
| `sandbox/__init__.py` | S | 结构排除：本轮AST逐项核imports/所有调用，仅export/字段/约束/索引/默认值构造，无业务DB会话或执行入口；不计DB语义全文 |
| `sandbox/acp_config.py` | S | 逐项核imports/函数与全调用树：声明、纯计算/内存协调或文件转换，不接主库/独立DB，不计DB语义全文 |
| `sandbox/acp_manager.py` | F | 全文独立Manager sqlite3、原request/peer/prompt/control、launch receipt/close tombstone/事件ACK；文件清退先于短metadata事务 |
| `sandbox/acp_peer.py` | S | 逐函数全调用树排除数据库；ACP子进程/终端/文件JSON协议，持久receipt由已审Manager ACP owner处理 |
| `sandbox/client.py` | F | 全文执行意图先落库后 socket；原 request probe，不重发未知执行；ACK 与 Task receipt 分离 |
| `sandbox/completion_receiver.py` | F | 全文原事件数据库 commit 后 Manager ACK；拒绝项不 ACK，不让毒前缀饿死后页 |
| `sandbox/completions.py` | F | 全文 Manager 独立 sqlite3；terminal jobs+outbox caller 同事务、consumer receipt 后原 run ACK |
| `sandbox/continuation_worker.py` | I | 全文按原 request route_child_completion，不起独立 Agent 或重置预算 |
| `sandbox/continuations.py` | F | 全文 ready/observed/rotate；真实 Work completion 委托 |
| `sandbox/control_completions.py` | F | 全文 operator completion 原 request/run/hash 与 audit 同提交，不创建聊天事件 |
| `sandbox/db_models.py` | S | 结构排除：本轮AST逐项核imports/所有调用，仅export/字段/约束/索引/默认值构造，无业务DB会话或执行入口；不计DB语义全文 |
| `sandbox/environment_supervisor.py` | S | 逐函数全调用树排除；进程/spec/output文件supervisor，无SQLite |
| `sandbox/environment_tools.py` | S | 逐函数全调用树排除；namespace文件/exec工具，无SQLite |
| `sandbox/execd.py` | S | 全文网络exec client，无SQLite |
| `sandbox/external_agents.py` | F | 全文252；Bot Task来源/原Work授权与prepare/checkpoint/bindrun在外部ACP动作前后独立提交；原request/run/permission/ACK/unknown恢复，不凭platform ID重建owner |
| `sandbox/manager.py` | F | 全文独立进程装配：原 jobs.sqlite owner、worker stop→Manager.close，不并入Bot Database |
| `sandbox/persistent.py` | D | 所有实际DB方法1-314、recover327-492、launch570-689、file_request840-953、status963-1080、handle/worker/prune/close1082-1275；无DB事务等待网络/线程 |
| `sandbox/resource_cleanup.py` | F | 全文 prepare/validate/verify；原完整 requests/Work/Conversation 集合与 phantom，None 身份填充保留 |
| `sandbox/task_repository.py` | F | 全文 prepare/checkpoint/permission/bind/reject/receive；请求/完成原 receipt，permission wait 源完整复核 |
| `services/__init__.py` | S | 逐项核imports/函数与全调用树：声明、纯计算/内存协调或文件转换，不接主库/独立DB，不计DB语义全文 |
| `services/admin/__init__.py` | S | 结构排除：本轮AST逐项核imports/所有调用，仅export/字段/约束/索引/默认值构造，无业务DB会话或执行入口；不计DB语义全文 |
| `services/admin/config_admin.py` | I | 全文thin config entrypoints，复用原RuntimeConfigService owner |
| `services/admin/control_auth.py` | S | 全文纯已解析principal/target授权判断，无DBI/O |
| `services/admin/group_admin.py` | F | 全文原group flag+audit同supplied session；决策在执行入口，不加新事务层 |
| `services/admin/group_recovery.py` | F | 全文379；reader现有路由→writer外线上资格→writer原actor/source/absence/route复核，group+审计+receipt同笔，无writer网络 |
| `services/admin/memory_admin.py` | F | 全文原1247行，现1206；原 mutation/receipt 与 fallback supplied writer，授权/evidence 当前核验；删除三笔无消费者后置审计，fallback 同 session 原子 audit 保留 |
| `services/admin/preference_admin.py` | F | 全文preference读/实际mutation service/derived audit；无reader嵌writer |
| `services/agent_runner.py` | I | 全部持久/paid编排run273–388、resume474–578、execute batch757–1061；WorkSession/Journal实现另逐项审；外部执行不持本文件DB事务，纯catalog/render排除 |
| `services/agent_tools.py` | D | 实际 DB/Memory/identity/授权工具方法778–2289及 ToolRuntime/source；固定 schema 文案排除。实际 write phase 三态映射共享 Main binding，日志不另写Memory |
| `services/automation_commands.py` | I | 逐具名方法及全部await owner调用/上下文核查；无自有DB事务，委托现行领域服务/Control准备与原receipt；解析/格式化排除；现有泛失败文案不等于确认rollback、不触发重派 |
| `services/autonomous_groups.py` | D | 所有owner编排_after_silence/_run_latest/_admit_latest/close；原quiet-slot/最新内部event与ordinary callback，配置reader/插件signal/feature在无writer状态 |
| `services/chat.py` | D | 实际 reader/current资格、Work stage/ready569–660、receipt936–986、source validator1153–1323、confirmed outbound1599–1668；其余模型/平台编排I，固定工具schema/纯渲染排除 |
| `services/chat_preparation_timings.py` | S | 逐项核imports/函数与全调用树：声明、纯计算/内存协调或文件转换，不接主库/独立DB，不计DB语义全文 |
| `services/client_link.py` | F | 全文124；reader BEGIN准备原claim/Person merge，writer同proof/原receipt；仅真实未提交准备变化重新prepare，unknown不重领 |
| `services/command_service.py` | I | 逐具名方法及全部await owner调用/上下文核查；无自有DB事务，委托现行领域服务/Control准备与原receipt；解析/格式化排除；现有泛失败文案不等于确认rollback、不触发重派 |
| `services/commands/__init__.py` | S | 结构排除：本轮AST逐项核imports/所有调用，仅export/字段/约束/索引/默认值构造，无业务DB会话或执行入口；不计DB语义全文 |
| `services/commands/admin.py` | D | execute全部owner分支与group_revision具名reader；命令执行委托Control原source/receipt，泛错误提示不作为rollback事实 |
| `services/commands/conversation.py` | F | 全文117；状态Projection窄列reader，后续自动化/配置读分开，展示计数不当资格 |
| `services/commands/group.py` | I | 逐具名方法及全部await owner调用/上下文核查；无自有DB事务，委托现行领域服务/Control准备与原receipt；解析/格式化排除；现有泛失败文案不等于确认rollback、不触发重派 |
| `services/commands/help.py` | S | 逐项核imports/函数与全调用树：声明、纯计算/内存协调或文件转换，不接主库/独立DB，不计DB语义全文 |
| `services/commands/identity.py` | I | 逐具名方法及全部await owner调用/上下文核查；无自有DB事务，委托现行领域服务/Control准备与原receipt；解析/格式化排除；现有泛失败文案不等于确认rollback、不触发重派 |
| `services/commands/memory.py` | I | 逐具名方法及全部await owner调用/上下文核查；无自有DB事务，委托现行领域服务/Control准备与原receipt；解析/格式化排除；现有泛失败文案不等于确认rollback、不触发重派 |
| `services/commands/models.py` | S | 逐项核imports/函数与全调用树：声明、纯计算/内存协调或文件转换，不接主库/独立DB，不计DB语义全文 |
| `services/commands/plugins.py` | I | 逐具名方法及全部await owner调用/上下文核查；无自有DB事务，委托现行领域服务/Control准备与原receipt；解析/格式化排除；现有泛失败文案不等于确认rollback、不触发重派 |
| `services/commands/work.py` | I | 逐具名方法及全部await owner调用/上下文核查；无自有DB事务，委托现行领域服务/Control准备与原receipt；解析/格式化排除；现有泛失败文案不等于确认rollback、不触发重派 |
| `services/concurrency.py` | S | 逐项核imports/函数与全调用树：声明、纯计算/内存协调或文件转换，不接主库/独立DB，不计DB语义全文 |
| `services/config_commands.py` | I | 逐具名方法及全部await owner调用/上下文核查；无自有DB事务，委托现行领域服务/Control准备与原receipt；解析/格式化排除；现有泛失败文案不等于确认rollback、不触发重派 |
| `services/context_assembler.py` | D | 所有实际DB/快照编排112–362、397–453；read_scope/prepare_prompt_snapshot/current read_version，writer外渲染，纯展示helper排除 |
| `services/context_boundary.py` | S | 逐项核imports/函数与全调用树：声明、纯计算/内存协调或文件转换，不接主库/独立DB，不计DB语义全文 |
| `services/context_compaction.py` | D | CompactReceipt read/write85–172、current_public_compact175–210/public_compact_publication213–238全部DB方法；正文编码和plans在writer外，原隐私/代际/revision与publication/receipt同笔 |
| `services/deduplication.py` | S | 逐项核imports/函数与全调用树：声明、纯计算/内存协调或文件转换，不接主库/独立DB，不计DB语义全文 |
| `services/document_reader.py` | S | 逐函数imports/完整调用树核查，无数据库实现/持久写入口；纯文件网络/内存协调排除，不声称纯算法全文 |
| `services/durable_invocations.py` | F | 全文原invocation boundary/paid结果/绑定plugin job Work与原source，不重置budget或重调已有结果 |
| `services/effect_gate.py` | S | 逐项核imports/函数与全调用树：声明、纯计算/内存协调或文件转换，不接主库/独立DB，不计DB语义全文 |
| `services/evidence_observation.py` | S | 逐项核imports/函数与全调用树：声明、纯计算/内存协调或文件转换，不接主库/独立DB，不计DB语义全文 |
| `services/evidence_state.py` | S | 逐项核imports/函数与全调用树：声明、纯计算/内存协调或文件转换，不接主库/独立DB，不计DB语义全文 |
| `services/execution_sources.py` | F | 全文599行；四种来源恢复、原person/space/presence/run/权限读及current source复核，无writer |
| `services/group_recovery.py` | D | enable/hint/_connection全部实际调用边界；平台connection与当前Principal/Space委托恢复owner；本文件不持writer，business提示不授权重放 |
| `services/history_projection.py` | F | 全文221；原frozen fragment/read_scope/prepare_commit，历史渲染与hash在writer前，publish函数消费领域准备 |
| `services/history_query.py` | S | 逐项核imports/函数与全调用树：声明、纯计算/内存协调或文件转换，不接主库/独立DB，不计DB语义全文 |
| `services/http_logging.py` | S | 逐项核imports/函数与全调用树：声明、纯计算/内存协调或文件转换，不接主库/独立DB，不计DB语义全文 |
| `services/image_preprocessor.py` | S | 逐项核imports/函数与全调用树：声明、纯计算/内存协调或文件转换，不接主库/独立DB，不计DB语义全文 |
| `services/invocation_context.py` | S | 逐项核imports/函数与全调用树：声明、纯计算/内存协调或文件转换，不接主库/独立DB，不计DB语义全文 |
| `services/invocation_service.py` | F | 全文T1/T2/domain/T3、原dispatch预算、typed capture/publication失败fallback；audits在原effect commit后 |
| `services/main_agent_backend.py` | F | 全文固定catalog/binding/Host state/typed outcome/result/derived trace；实际DB三态优先于descriptor猜测 |
| `services/main_agent_contract.py` | I | definitions实际RuntimeConfig快照与注册工具调用；固定声明不随当前授权变化，不新持有DB事务 |
| `services/main_agent_turns.py` | D | compose103–569所有嵌套publication/boundary，read_result571–598/run600–666；prepare reader→同writer publication+ordinary+原Work dispatch，paid original recovery保留 |
| `services/media_process.py` | S | 逐项核imports/函数与全调用树：声明、纯计算/内存协调或文件转换，不接主库/独立DB，不计DB语义全文 |
| `services/media_resolver.py` | S | 逐函数imports/完整调用树核查，无数据库实现/持久写入口；纯文件网络/内存协调排除，不声称纯算法全文 |
| `services/message_splitter.py` | S | 逐项核imports/函数与全调用树：声明、纯计算/内存协调或文件转换，不接主库/独立DB，不计DB语义全文 |
| `services/model_context_projection.py` | S | 逐项核imports/函数与全调用树：声明、纯计算/内存协调或文件转换，不接主库/独立DB，不计DB语义全文 |
| `services/native_media.py` | S | 逐项核imports/函数与全调用树：声明、纯计算/内存协调或文件转换，不接主库/独立DB，不计DB语义全文 |
| `services/native_tool_binder.py` | S | 逐项核imports/函数与全调用树：声明、纯计算/内存协调或文件转换，不接主库/独立DB，不计DB语义全文 |
| `services/participation_diagnostics.py` | F | 全文232；纯reader限窗聚合，窗口/未知coverage明确，不把诊断状态用作admission门槛 |
| `services/participation_feedback.py` | F | 全文859（B删除重复source后）；同reader真实Work树/receipt/paid count准备，writer当前privacy/work/social/tool/no_reply absence与原feedback repo收口；独立participation保存在主库提交后 |
| `services/participation_identity.py` | F | 全文 actor roots 批量解析，严格补集仅 prepare 当前原请求 |
| `services/participation_ordinary.py` | F | 全文 1-636；actor 历史收集 reader，writer roots+重新取当前 session+原 public 当前算法 |
| `services/participation_parameters.py` | S | 结构排除：本轮AST逐项核imports/所有调用，仅export/字段/约束/索引/默认值构造，无业务DB会话或执行入口；不计DB语义全文 |
| `services/participation_snapshot.py` | F | 全文独立store connection/read/write/close同一owner线程；取消保真实revision并join后关 |
| `services/participation_trace.py` | F | 全文当前actor roots reader与实际observer；trace不裁决、不重试观察 |
| `services/plugin_events.py` | I | 逐具名方法及全部await owner调用/上下文核查；无自有DB事务，委托现行领域服务/Control准备与原receipt；解析/格式化排除；现有泛失败文案不等于确认rollback、不触发重派 |
| `services/plugin_sessions.py` | F | 全文356行；session owner/actor/current Work 读、parent budget reserve，模型与平台在 owned DB 事务外 |
| `services/policies.py` | S | 逐项核imports/函数与全调用树：声明、纯计算/内存协调或文件转换，不接主库/独立DB，不计DB语义全文 |
| `services/processing_status.py` | I | _run路由reader→platform typing循环→finally stop，已有owner前后无writer，装饰性状态失败不作为执行失败回执 |
| `services/processor.py` | D | 直接DB方法attachment_metadata177–244/person authority352–356/ordinary1114–1164及准备→writer调用1076–1758；原入站/命令/发送通过已审owner，纯policy与展示段排除 |
| `services/profile_commands.py` | I | 逐具名方法及全部await owner调用/上下文核查；无自有DB事务，委托现行领域服务/Control准备与原receipt；解析/格式化排除；现有泛失败文案不等于确认rollback、不触发重派 |
| `services/prompt_composer.py` | S | 逐项核imports/函数与全调用树：声明、纯计算/内存协调或文件转换，不接主库/独立DB，不计DB语义全文 |
| `services/prompt_registry.py` | S | 逐项核imports/函数与全调用树：声明、纯计算/内存协调或文件转换，不接主库/独立DB，不计DB语义全文 |
| `services/qq_face_resolver.py` | S | 逐项核imports/函数与全调用树：声明、纯计算/内存协调或文件转换，不接主库/独立DB，不计DB语义全文 |
| `services/rate_limit.py` | S | 逐项核imports/函数与全调用树：声明、纯计算/内存协调或文件转换，不接主库/独立DB，不计DB语义全文 |
| `services/renderer.py` | S | 逐项核imports/函数与全调用树：声明、纯计算/内存协调或文件转换，不接主库/独立DB，不计DB语义全文 |
| `services/semantic_participation.py` | D | 全部DB/store与编排方法start/scene/session-load/save/binding/hydrate/sources/admit/dispatch/tick/advance；原controller当前谓词，无writer历史准备 |
| `services/subagent_execution.py` | D | cancel_commands/run 完整90–385；原Task run/source与当前 lease/来源/owner恢复；模型/外部cancel在reader退出后 |
| `services/turn_coordinator.py` | S | 逐函数imports/完整调用树核查，无数据库实现/持久写入口；纯文件网络/内存协调排除，不声称纯算法全文 |
| `services/turn_execution.py` | D | admit/activate/request/prepare_request/compact_current/observe/settle/finish/exhausted全部具名DB编排方法；原预算与paid journal先后、received响应不重买；纯渲染helper排除 |
| `services/turn_transcript.py` | S | 逐项核imports/函数与全调用树：声明、纯计算/内存协调或文件转换，不接主库/独立DB，不计DB语义全文 |
| `services/video_frames.py` | S | 逐项核imports/函数与全调用树：声明、纯计算/内存协调或文件转换，不接主库/独立DB，不计DB语义全文 |
| `services/vision_rate_limit.py` | S | 逐项核imports/函数与全调用树：声明、纯计算/内存协调或文件转换，不接主库/独立DB，不计DB语义全文 |
| `services/vision_service.py` | D | _analyze244–465全部分析缓存读/写消费者；下载/CPU/model/原body编码先完成，再单次repository.save；singleflight不持DB事务 |
| `services/work_resume.py` | D | resume69–108、failure recovery110–186、scene189–246、全部resume257–442；原source/lease/phase/paid结果恢复，不凭异常重买 |
| `services/yuki_runtime.py` | F | 全文execution owner stop/drain→资源关闭，不把进程内activation当DB完成事实 |
