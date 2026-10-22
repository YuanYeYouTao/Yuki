# SQLite 实施后第 1 轮交叉复审：C 域（B reviewer）

基线：`109cbd40274ca8db1a0b381ac2db1e35c3cf1df3`。本轮检查的是实现后的源码与测试；此前三轮任务书审查不计入本轮。修复位于共享分支工作树，本文不代表提交、PR、合并或上线。未运行付费模型、生产数据库或真实平台消息。

已读 AGENTS、development-contract §6、main-agent-runtime、execution-trace、插件现行架构及 C 实施记录。检查包含未修改调用者、供给 session、实际回执消费与独立库。固定原子单位继续沿原业务边界；不为缩短事务拆原有 paid、claim、ledger 或 receipt。

## 发现与修复

1. **非 SQLAlchemy 异常丢失提交三态**（U14/U15，W25–27/W32–33，V10/V17）。`services/agent_tools.py:1035` 抽出原数据库错误结果映射；带公共 write phase 的 RuntimeError/OSError、未知或已提交的 ValueError 也保实际 certainty。普通参数错误保持原错误。`runtime/activation_outcome.py:95` 同时保 JournalUnavailable cause 与 WorkConflict 的 phase/三态；未知提交不授予 source_changed 重试资格。

2. **按异常名盲重备**（U15/U17，W27/W30）。ClientLink、WorkSession journal、abandoned input repair 在确认回滚后才准备同一原请求。删 WorkSession 自行扫描 rollback/invalidation 字符串的旧判断，直接用公共结果。Processor admission、Work resume 与 SDK resume 不再把有 phase 的未知/已提交异常改写成普通 duplicate/source_changed/busy。纯读准备拒绝照旧。

3. **派生 cleanup 覆盖已确认结果**（W25–27）。`bind_work_activation` 的 settle/release 清理现在同样识别公共 phase 的非 SQLA 错误；原业务异常与取消继续传播。只记录 deferred，不重发效果或重置预算。

4. **Social writer 重做正文哈希、解原完整计划、再复制一份正文**（U11/U12，W32–34，V12/V41）。prepare/prepare_sequence 在 reader/准备阶段按原持久 target 算父子 digest。writer 只核当前真实 Person family、原 target/call/hash/parts；并发第一次选了另一原 target 时先退出空 writer 再准备。finish 返回 `SocialSettlement`：小运输事实和同一原行 raw prepared_json；正常 ledger+Social receipt+Work delivery 仍同提交。完整结果退出 writer 后从原 raw JSON 形成，不另查库。Work delivery 回执删掉没有消费者的 prepared 正文副本；原 Social 行、原物理计划和旧恢复 reader 保留。

5. **Emoji claim 读完整旧 DTO，text claim 内才做 hash/encode**（W34/W39）。`emoji/delivery.validate_claim` 改为只投影原 target/route 与 generation/cancel/work/event/person 字段，仍在同 writer 核真实撤权。真实新 `PreparedEmojiIntent` 在 reader 后准备原 ID/hash/source/intent；writer 核当前源并沿原 call/digest 接纳，返回小 admission 事实，删无消费者的整 intent 解码。select/close 的编码与 DTO 解码移出 owned writer。generic annotated 错误保三态；confirmed rollback 为 not_accepted，未知结果仍 unknown，原 owner wake 与原 intent ID 保留。

6. **媒体额度与到期使用排队前事实，取消并未等待文件线程退出**（U22，W39/W71，V10/V34–35）。create 在 writer 内重核已有额度与原绝对 expires_at，不重置 TTL。文件发布被取消时 join 同一个线程，确认从未尝试登记后清理原 UUID 文件；第二次取消也不能放走原线程。未知提交仍只查原 handle，查不到不 unlink。没有新 orphan journal 或按 mtime 猜测补写。

7. **插件原过期条件使用旧时钟**（W37–38，V44）。State version=0 删除到期原值、session create/reset 的到期判定改为实际 writer 准入后取时钟；显式测试/调用传入的 now 语义保留。已完成模型返回跨过 session expiry 仍照原合同写回，不加新的状态门槛。

8. **Work 恢复只需少数控制字段，却解整份 checkpoint**（W26/W30）。recover_failure 用 SQL 提原 failure code / accepted action；commit_state 的普通子工作通知用 SQL 提原 sync_result。原 ending、模型/工具预算、递归撤权和 parent mailbox 原子关系不变。

9. **Wait 仅在 reader 预筛，writer 仍重扫旧 conditions/completion JSON**（U17/W30）。本轮发现后交 root 完整迁移 prepare/current；通知 publication/event/wait/outbox 继续共享原 writer。本人适配 `SandboxTaskRepository.receive_permission:185`：reader 显式 BEGIN 读原 task 投影并准备原 active wait/absence，任何 DML 前重核；变化先退出空 writer 再准备原 request/event/run。Manager ACK 仍在主库提交后。此项代码 owner 与专门回归见 root 实施记录，不以本人的发现代替 root 的修复证据。

10. **Feedback 的 supplied writer 尚触发 payload/run DTO 编解码**（U19/W35）。与 A reviewer 交叉确认后由 A 收口 prepared feedback 与原 run raw 核验；同一原 64 refs 页仍是原子单位，真实后到效果/计费 ordinal/sequence 不从诊断窗口推断。本人未并发改该文件。

11. **普通参与接纳在 writer 内收集整段控制器的人物历史**（W35/W70）。`processor._ordinary_admission_allowed` 原来供给 writer 给 `continuation_current→actor_identity`，后者遍历 events/boundaries 并解析 checkpoint。现仅把 actor 集合收集移到准备阶段；writer 按固定内部 ID 批量读取当前 roots，继续原 invitation/Controller 当前资格谓词。原 SELF target、review resolved target 和 proposal target 即使原始 human event 已退役也进入准备集合；已查无 Person 的 ID 保原样。当前算法实际遇到新 actor 才退出空 writer 重备，只有公共 committed=False 才重试。没有新控制器 revision、状态图证明、跨 FIFO 的全局 observer 锁或额外到期门槛。库现有当前 unit/effect/anchor/boundary/候选解释及有限 checkpoint 解析保留在判定处：它们不是 DB 历史扫描，也未声称成员数绝对固定；本轮没有为追求零 CPU 复制库算法。`_sources_current` 的正文 hash/SELF facts 链不由此 writer 回调调用。A reviewer 独立读 pinned 算法并补出了 SELF/review/proposal target 的集合遗漏，已一并补齐。

## 调用与保留边界

- Protocol publication 继续 file fence→writer；GC 分 mark writer、file fence 内 reader/unlink、ack writer，未发现反序等待。已有线程 join 与原 deleting/refs 事实保留，不引第二套队列。
- Social 正常 confirmed 路径三方同事务；ACK 后异常先读原 receipt，已确认不降级。只能补 receipt、无法再发原正文的路径如实保留缺 ledger，不重发。SDK 两个 finish 调用忽略返回值，仍同原 ledger writer；原状态与历史读取均已回归。
- C 域所有直接 read_version_matches 调用与 WorkSourceGuard.validate_observations 都在 reader/prepare；未向 DAG 验证传 writer。projection publication 的领域内部由 A reviewer 复核。
- ModelInvocation/Trace 正常 DI 共享 droppable writer；无队列 fallback 和清理也用公共 owner。诊断失败不购买模型或推翻成功响应，业务 paid/receipt 没有入诊断队列。
- 独立 PersistentManager jobs.sqlite3 不接入 Bot 主库 FIFO；全部同步 DB transaction block 经 AST 核验没有 await 跨越。文件/RPC/Docker等待与其同步 DB 段分开。独立参与库保单 owning thread、原 snapshot CAS 和 close-after-queued-operations；未宣称跨两个 SQLite 的原子事务。
- quiet 继续共用 foreground activity/provider priority；观察普通聊天准备、工具和交付期间都算前台。SELF worker 的取消生命周期由原 worker 持有，不能把取消 await 当线程或模型已经退出。

## 本地验证

各组有重叠，不相加成独立案例总数。日志中的 baseline schema-cycle、SQLite datetime/index reflection warning 未当作新失败。

| 组合 | 结果 |
| --- | --- |
| tool failure + SQLite classifier | 49 passed |
| ClientLink + journal source retry + classifier | 38 passed |
| media publication/cleanup + journal source/cleanup | 33 passed |
| 首组 Social/source/media/classifier/tool | 118 passed，17.01s |
| Work settlement/owner/BUSY/source/protocol preparation/continuity | 201 passed，102.86s |
| Social settlement + ordinary handoff/history + media + ClientLink/source | 120 passed，67.41s |
| ACP/external agent/Wait/notification/preparation/平台入口 | 226 passed，96.04s |
| plugin create/reset/State 过期 + session integrity/person material + Social/protocol | 76 passed，33.39s |
| Emoji generic ACK/原 owner wake + plugin material/tool | 107 passed，4 POSIX skip，22.22s |
| Emoji codec/dispatch + ordinary admission + Social/protocol | 103 passed，4 POSIX skip，43.61s |
| 最终 Social/Emoji/media/tool/classifier/ClientLink/journal 9 文件 | 182 passed，4 POSIX skip，33.71s |
| 参与/普通接纳/快路/quiet/冷恢复/上下文 14 文件最终联合 | 130 passed，75.14s；包含 FIFO 中迟到 quiet、writer 当前 Person root、SELF target 变化确认回滚后同原 admission 重备 |
| 最终产品类型与风格 | 21 产品文件独立 cache mypy 通过；33 产品/测试文件 Ruff check 与 format --check 通过 |

新增案例使用真实隔离 SQLite 提交/回滚、FIFO 等待、线程 barrier、真实 reader/writer 和 fake gateway/provider。包括物理 commit 后同类异常、原 paid journal 不再发布、已登记媒体不误删、并发额度、绝对 TTL、重复取消、原事件/原 request 不变。真实 POSIX fcntl 文件锁四项在 Windows 跳过；没有假装在本机完成 Linux 验收。性能 paired 全局验证由 root 单独负责，本报告没有以测试总耗时推断线上延迟。

## 逐文件覆盖

F = 本轮全文核读；D = 按列出的方法/SQL/调用边界作语义核验，未声称整文件非 DB 业务全文；I = 结构、导入、调用索引与领域 owner 分类，未声称其每行实现都已审读；P = 数据模型、schema、导出或纯转换，未据名称把实际 reader/writer 排除。D/I 不是“全文通过”的替代名。下表保留全部 211 个路径，额外跨域 `emoji/delivery.py` 已全文核相应 source/admit/claim/worker 边界。

| 路径（src/yuki/） | 级别 | 本轮范围 / 排除理由 |
| --- | --- | --- |
| `runtime/__init__.py` | P | 本轮复核为schema/类型/固定声明/纯转换；不自行取得主库session，未把此级别计全文 |
| `runtime/activation_bindings.py` | P | 本轮复核为schema/类型/固定声明/纯转换；不自行取得主库session，未把此级别计全文 |
| `runtime/activation_outcome.py` | F | 全文；实际领域事务、外部效果、三态/锁序/取消按正文记录 |
| `runtime/activation_tasks.py` | I | AST/导入/实际调用入口与冻结diff归类；间接操作沿相应领域repo，不按execute名称冒认SQL；未逐行全文 |
| `runtime/context_preparation.py` | F | 全文；实际领域事务、外部效果、三态/锁序/取消按正文记录 |
| `runtime/contracts.py` | P | 本轮复核为schema/类型/固定声明/纯转换；不自行取得主库session，未把此级别计全文 |
| `runtime/delivery.py` | I | AST/导入/实际调用入口与冻结diff归类；间接操作沿相应领域repo，不按execute名称冒认SQL；未逐行全文 |
| `runtime/delivery_intents.py` | F | 全文；实际领域事务、外部效果、三态/锁序/取消按正文记录 |
| `runtime/effect_outcomes.py` | F | 全文；实际领域事务、外部效果、三态/锁序/取消按正文记录 |
| `runtime/effect_queries.py` | F | 全文；实际领域事务、外部效果、三态/锁序/取消按正文记录 |
| `runtime/effect_schema.py` | P | 本轮复核为schema/类型/固定声明/纯转换；不自行取得主库session，未把此级别计全文 |
| `runtime/errors.py` | P | 本轮复核为schema/类型/固定声明/纯转换；不自行取得主库session，未把此级别计全文 |
| `runtime/execution_receipts.py` | I | AST/导入/实际调用入口与冻结diff归类；间接操作沿相应领域repo，不按execute名称冒认SQL；未逐行全文 |
| `runtime/lease_heartbeat.py` | F | 全文；实际领域事务、外部效果、三态/锁序/取消按正文记录 |
| `runtime/observability.py` | I | AST/导入/实际调用入口与冻结diff归类；间接操作沿相应领域repo，不按execute名称冒认SQL；未逐行全文 |
| `runtime/origin.py` | P | 本轮复核为schema/类型/固定声明/纯转换；不自行取得主库session，未把此级别计全文 |
| `runtime/principal.py` | P | 本轮复核为schema/类型/固定声明/纯转换；不自行取得主库session，未把此级别计全文 |
| `runtime/protocol_schema.py` | P | 本轮复核为schema/类型/固定声明/纯转换；不自行取得主库session，未把此级别计全文 |
| `runtime/protocol_store.py` | D | _finish_thread、put/publication/publish_refs、GC discovery/mark/file fence/unlink/ack；publication→writer与GC分段顺序 |
| `runtime/subagent_repository.py` | D | start/acquire/maintain与related/list/message/resume/cancel/finish；原结果JSON reader准备、当前revision/parent/原bytes CAS |
| `runtime/subagent_scheduler.py` | D | close/loop/_run；先取消并join实际worker，DB候选reader，执行在事务外 |
| `runtime/subagent_schema.py` | P | 本轮复核为schema/类型/固定声明/纯转换；不自行取得主库session，未把此级别计全文 |
| `runtime/subagent_tools.py` | I | AST/导入/实际调用入口与冻结diff归类；间接操作沿相应领域repo，不按execute名称冒认SQL；未逐行全文 |
| `runtime/trigger.py` | I | AST/导入/实际调用入口与冻结diff归类；间接操作沿相应领域repo，不按execute名称冒认SQL；未逐行全文 |
| `runtime/work_activation.py` | F | 全文；实际领域事务、外部效果、三态/锁序/取消按正文记录 |
| `runtime/work_budget.py` | F | 全文；实际领域事务、外部效果、三态/锁序/取消按正文记录 |
| `runtime/work_budget_schema.py` | P | 本轮复核为schema/类型/固定声明/纯转换；不自行取得主库session，未把此级别计全文 |
| `runtime/work_context_note.py` | F | 全文；实际领域事务、外部效果、三态/锁序/取消按正文记录 |
| `runtime/work_control.py` | D | reserve/accept/control与预算、原接纳决定、effect refresh/settlement；新ID参与的小回执编码保留 |
| `runtime/work_journal.py` | D | save/invalidate/recovered_inputs/resolve_effect/record_effect；文件准备、publication、原paid checkpoint、original domain receipt只读恢复 |
| `runtime/work_management.py` | D | resume_blocker/stop_owned_execution/manage_work；同writer原树撤权与回执保留 |
| `runtime/work_media.py` | I | AST/导入/实际调用入口与冻结diff归类；间接操作沿相应领域repo，不按execute名称冒认SQL；未逐行全文 |
| `runtime/work_observation_schema.py` | P | 本轮复核为schema/类型/固定声明/纯转换；不自行取得主库session，未把此级别计全文 |
| `runtime/work_queries.py` | D | get/available/recent/list；只读、bounded展示不作执行集合 |
| `runtime/work_query_schema.py` | P | 本轮复核为schema/类型/固定声明/纯转换；不自行取得主库session，未把此级别计全文 |
| `runtime/work_recovery_schema.py` | P | 本轮复核为schema/类型/固定声明/纯转换；不自行取得主库session，未把此级别计全文 |
| `runtime/work_repository.py` | D | lease、accept/derive/transition/commit_state、checkpoint/patch、input prepare/repair、effect T1/T2/receipt、completion与media；写后同步结果窄SQL，reader准备与原bytes CAS |
| `runtime/work_scheduler.py` | D | health/dispatch_once/_run_scope；读选候选、维护/恢复与实际lease分开，不持writer等resume |
| `runtime/work_schema_v1.py` | P | 本轮复核为schema/类型/固定声明/纯转换；不自行取得主库session，未把此级别计全文 |
| `runtime/work_session.py` | D | restore/save/_save/paid publication；只在确认回滚后重备同一journal，不购买第二份响应 |
| `runtime/work_source_guard.py` | F | 全文；实际领域事务、外部效果、三态/锁序/取消按正文记录 |
| `runtime/work_supervisor.py` | D | recover_failure和accepted ending；原failure code/action用窄SQL，不解整个checkpoint |
| `runtime/work_tree.py` | D | ancestors/descendants 实际递归 SQL 与原树撤权调用者；只读与同 writer 复用，没有历史正文解码 |
| `runtime/work_wait.py` | D | register/match_input/match_event/_deliver/due/external_action；发现reader只预筛而writer仍解旧JSON，交root完整prepare/current迁移并联合验证 |
| `runtime/work_wait_schema.py` | P | 本轮复核为schema/类型/固定声明/纯转换；不自行取得主库session，未把此级别计全文 |
| `model_runtime/__init__.py` | P | 本轮复核为schema/类型/固定声明/纯转换；不自行取得主库session，未把此级别计全文 |
| `model_runtime/capacity.py` | P | 本轮复核为schema/类型/固定声明/纯转换；不自行取得主库session，未把此级别计全文 |
| `model_runtime/db_models.py` | P | 本轮复核为schema/类型/固定声明/纯转换；不自行取得主库session，未把此级别计全文 |
| `model_runtime/dispatch_guard.py` | I | AST/导入/实际调用入口与冻结diff归类；间接操作沿相应领域repo，不按execute名称冒认SQL；未逐行全文 |
| `model_runtime/executor.py` | D | foreground_activity/quiet等待、execute/_execute/priority/provider slots、_record_invocation/close；模型与诊断写释放分开 |
| `model_runtime/models.py` | P | 本轮复核为schema/类型/固定声明/纯转换；不自行取得主库session，未把此级别计全文 |
| `model_runtime/pool.py` | I | AST/导入/实际调用入口与冻结diff归类；间接操作沿相应领域repo，不按execute名称冒认SQL；未逐行全文 |
| `model_runtime/profiles.py` | P | 本轮复核为schema/类型/固定声明/纯转换；不自行取得主库session，未把此级别计全文 |
| `model_runtime/repository.py` | F | 全文；实际领域事务、外部效果、三态/锁序/取消按正文记录 |
| `model_runtime/request_accounting.py` | I | AST/导入/实际调用入口与冻结diff归类；间接操作沿相应领域repo，不按execute名称冒认SQL；未逐行全文 |
| `model_runtime/routes.py` | P | 本轮复核为schema/类型/固定声明/纯转换；不自行取得主库session，未把此级别计全文 |
| `model_runtime/search_defaults.py` | P | 本轮复核为schema/类型/固定声明/纯转换；不自行取得主库session，未把此级别计全文 |
| `model_runtime/secrets.py` | P | 本轮复核为schema/类型/固定声明/纯转换；不自行取得主库session，未把此级别计全文 |
| `model_runtime/structured.py` | I | AST/导入/实际调用入口与冻结diff归类；间接操作沿相应领域repo，不按execute名称冒认SQL；未逐行全文 |
| `execution_trace/__init__.py` | P | 本轮复核为schema/类型/固定声明/纯转换；不自行取得主库session，未把此级别计全文 |
| `execution_trace/db_models.py` | P | 本轮复核为schema/类型/固定声明/纯转换；不自行取得主库session，未把此级别计全文 |
| `execution_trace/payload.py` | P | 本轮复核为schema/类型/固定声明/纯转换；不自行取得主库session，未把此级别计全文 |
| `execution_trace/phases.py` | P | 本轮复核为schema/类型/固定声明/纯转换；不自行取得主库session，未把此级别计全文 |
| `execution_trace/recorder.py` | D | coverage/append/_prepare_write/source guards/cleanup/trace_span；队列是可丢诊断，500行页仅清理固定到期窗口 |
| `social/__init__.py` | P | 本轮复核为schema/类型/固定声明/纯转换；不自行取得主库session，未把此级别计全文 |
| `social/agent_adapter.py` | D | invoke_social实际owner/permission/target调用；所有当前身份reader，不传writer给模型/网关 |
| `social/confirmed_content.py` | F | 全文；实际领域事务、外部效果、三态/锁序/取消按正文记录 |
| `social/content.py` | D | prepare_for_entry实际查询与Social调用点；提及解析/渲染在准备reader，不在claim writer |
| `social/db_models.py` | P | 本轮复核为schema/类型/固定声明/纯转换；不自行取得主库session，未把此级别计全文 |
| `social/emoji_intents.py` | D | 全部admit/prepare/source validation/select/close/project/pending/effect_facts；新的准备只保原领域字段，不加持久状态 |
| `social/history.py` | D | 只读历史/回执投影调用链；不把历史查询送writer；真实Social事实仍在原行 |
| `social/models.py` | P | 本轮复核为schema/类型/固定声明/纯转换；不自行取得主库session，未把此级别计全文 |
| `social/repository.py` | D | prepare/prepare_sequence/claim/finish/recover_interrupted/delivery_facts；新原target/child hash准备与旧回执reader、SocialSettlement保原raw计划 |
| `social/service.py` | D | prepare/route/person/self/target checks、_effect/_dispatch_claimed/_record_work_delivery、replay、emoji admission；网关外置与正常三方原子提交 |
| `social/source_keys.py` | P | 本轮复核为schema/类型/固定声明/纯转换；不自行取得主库session，未把此级别计全文 |
| `social/tools.py` | P | 本轮复核为schema/类型/固定声明/纯转换；不自行取得主库session，未把此级别计全文 |
| `plugin_host/__init__.py` | P | 本轮复核为schema/类型/固定声明/纯转换；不自行取得主库session，未把此级别计全文 |
| `plugin_host/admission_adapter.py` | I | AST/导入/实际调用入口与冻结diff归类；间接操作沿相应领域repo，不按execute名称冒认SQL；未逐行全文 |
| `plugin_host/approval.py` | I | AST/导入/实际调用入口与冻结diff归类；间接操作沿相应领域repo，不按execute名称冒认SQL；未逐行全文 |
| `plugin_host/audit.py` | I | AST/导入/实际调用入口与冻结diff归类；间接操作沿相应领域repo，不按execute名称冒认SQL；未逐行全文 |
| `plugin_host/background_turns.py` | D | prepare/_run/renew/failure与Work原预算/原Job attempt；插件/模型执行在writer外 |
| `plugin_host/canonical_projection.py` | I | AST/导入/实际调用入口与冻结diff归类；间接操作沿相应领域repo，不按execute名称冒认SQL；未逐行全文 |
| `plugin_host/capability_adapter.py` | D | _available 读取当前安装/批准/superuser 与 facade 构造；实际执行再核权，目录不产生写许可 |
| `plugin_host/command_adapter.py` | I | AST/导入/实际调用入口与冻结diff归类；间接操作沿相应领域repo，不按execute名称冒认SQL；未逐行全文 |
| `plugin_host/config.py` | P | 本轮复核为schema/类型/固定声明/纯转换；不自行取得主库session，未把此级别计全文 |
| `plugin_host/configuration_service.py` | D | read/save/_revision；schema/plugin validator在writer外，实际当前revision/owner在writer |
| `plugin_host/db_models.py` | P | 本轮复核为schema/类型/固定声明/纯转换；不自行取得主库session，未把此级别计全文 |
| `plugin_host/direct_command_router.py` | I | AST/导入/实际调用入口与冻结diff归类；间接操作沿相应领域repo，不按execute名称冒认SQL；未逐行全文 |
| `plugin_host/direct_delivery.py` | D | prepare_delivery/validate_delivery_source；当前源及原actor、Presence、Binding，claim再次核 |
| `plugin_host/discovery.py` | I | AST/导入/实际调用入口与冻结diff归类；间接操作沿相应领域repo，不按execute名称冒认SQL；未逐行全文 |
| `plugin_host/event_bus.py` | I | AST/导入/实际调用入口与冻结diff归类；间接操作沿相应领域repo，不按execute名称冒认SQL；未逐行全文 |
| `plugin_host/extension_registry.py` | I | AST/导入/实际调用入口与冻结diff归类；间接操作沿相应领域repo，不按execute名称冒认SQL；未逐行全文 |
| `plugin_host/facades.py` | D | Memory supplied reader、SDK prepare/send/settle与resume；callback/network在外，finish原行与ledger同writer，不吞有phase的未知resume |
| `plugin_host/http_client.py` | I | AST/导入/实际调用入口与冻结diff归类；间接操作沿相应领域repo，不按execute名称冒认SQL；未逐行全文 |
| `plugin_host/loader.py` | I | AST/导入/实际调用入口与冻结diff归类；间接操作沿相应领域repo，不按execute名称冒认SQL；未逐行全文 |
| `plugin_host/main_turn.py` | D | 原Work准备/resume、read_version_matches；没有supplied writer调用DAG验证 |
| `plugin_host/manager.py` | D | 发现/definition注册/启动/关闭hook调用与安装repo边界；外部hook在writer外，Manager锁与主库owner不等同 |
| `plugin_host/manifest.py` | P | 本轮复核为schema/类型/固定声明/纯转换；不自行取得主库session，未把此级别计全文 |
| `plugin_host/media_artifacts.py` | F | 全文；实际领域事务、外部效果、三态/锁序/取消按正文记录 |
| `plugin_host/models.py` | P | 本轮复核为schema/类型/固定声明/纯转换；不自行取得主库session，未把此级别计全文 |
| `plugin_host/notification_delivery.py` | D | _deliver/_record_outbound；原attempt sent和prepared ledger同writer，gateway ACK与提交不明分清，原消息ID不重发 |
| `plugin_host/notification_manifest.py` | P | 本轮复核为schema/类型/固定声明/纯转换；不自行取得主库session，未把此级别计全文 |
| `plugin_host/notification_repository.py` | D | grant/revoke/publish、outbox claim/finish/retry、Job claim/renew/settlement、current context；Wait接入与迟到时钟由root本轮修正 |
| `plugin_host/ownership.py` | D | 当前canonical owner/同人family解析是SQL helper；对应supplied reader/writer沿调用者session，无外部工作 |
| `plugin_host/prompt_adapter.py` | I | AST/导入/实际调用入口与冻结diff归类；间接操作沿相应领域repo，不按execute名称冒认SQL；未逐行全文 |
| `plugin_host/repository.py` | D | Installation/Config/State/Audit入口与CAS；JSON外备，原owner/row ID+version在writer；State过期判断时钟改writer后 |
| `plugin_host/secrets.py` | P | 本轮复核为schema/类型/固定声明/纯转换；不自行取得主库session，未把此级别计全文 |
| `plugin_host/session_facade.py` | I | AST/导入/实际调用入口与冻结diff归类；间接操作沿相应领域repo，不按execute名称冒认SQL；未逐行全文 |
| `plugin_host/session_repository.py` | D | create/append/get/list/reset/close/expiry；序号+正文同writer，create/reset到期判定在准入后，已付结果过期不丢 |
| `plugin_host/storage.py` | F | 全文；实际领域事务、外部效果、三态/锁序/取消按正文记录 |
| `sandbox/__init__.py` | P | 本轮复核为schema/类型/固定声明/纯转换；不自行取得主库session，未把此级别计全文 |
| `sandbox/acp_config.py` | P | 本轮复核为schema/类型/固定声明/纯转换；不自行取得主库session，未把此级别计全文 |
| `sandbox/acp_manager.py` | D | 独立Manager jobs/ACP intent/dispatch/control/purge；同步DB短段与实际RPC/文件阶段分开 |
| `sandbox/acp_peer.py` | I | AST/导入/实际调用入口与冻结diff归类；间接操作沿相应领域repo，不按execute名称冒认SQL；未逐行全文 |
| `sandbox/client.py` | I | AST/导入/实际调用入口与冻结diff归类；间接操作沿相应领域repo，不按execute名称冒认SQL；未逐行全文 |
| `sandbox/completion_receiver.py` | F | 全文；实际领域事务、外部效果、三态/锁序/取消按正文记录 |
| `sandbox/completions.py` | D | 独立Manager completion outbox；原finish同connection、ACK独立事实 |
| `sandbox/continuation_worker.py` | I | AST/导入/实际调用入口与冻结diff归类；间接操作沿相应领域repo，不按execute名称冒认SQL；未逐行全文 |
| `sandbox/continuations.py` | D | pending/mark：reader选原request，writer原state条件确认，无新run |
| `sandbox/control_completions.py` | D | record_control_completion；原控制receipt/audit身份当前核验，已编码原结果摘要外置 |
| `sandbox/db_models.py` | P | 本轮复核为schema/类型/固定声明/纯转换；不自行取得主库session，未把此级别计全文 |
| `sandbox/environment_supervisor.py` | I | AST/导入/实际调用入口与冻结diff归类；间接操作沿相应领域repo，不按execute名称冒认SQL；未逐行全文 |
| `sandbox/environment_tools.py` | I | AST/导入/实际调用入口与冻结diff归类；间接操作沿相应领域repo，不按execute名称冒认SQL；未逐行全文 |
| `sandbox/execd.py` | I | AST/导入/实际调用入口与冻结diff归类；间接操作沿相应领域repo，不按execute名称冒认SQL；未逐行全文 |
| `sandbox/external_agents.py` | I | AST/导入/实际调用入口与冻结diff归类；间接操作沿相应领域repo，不按execute名称冒认SQL；未逐行全文 |
| `sandbox/manager.py` | I | AST/导入/实际调用入口与冻结diff归类；间接操作沿相应领域repo，不按execute名称冒认SQL；未逐行全文 |
| `sandbox/persistent.py` | D | 独立jobs.sqlite3 constructor/receipt/completion/file request/dispatch边界；AST核全部with self.db无await，主库FIFO不接管此进程 |
| `sandbox/resource_cleanup.py` | F | 全文；实际领域事务、外部效果、三态/锁序/取消按正文记录 |
| `sandbox/task_repository.py` | D | prepare/checkpoint/receive_permission/receive/bind/reject；原task投影+Wait准备前置，任何DML前核当前集，ACK仍在commit后 |
| `services/__init__.py` | P | 本轮复核为schema/类型/固定声明/纯转换；不自行取得主库session，未把此级别计全文 |
| `services/admin/__init__.py` | P | 本轮复核为schema/类型/固定声明/纯转换；不自行取得主库session，未把此级别计全文 |
| `services/admin/config_admin.py` | I | AST/导入/实际调用入口与冻结diff归类；间接操作沿相应领域repo，不按execute名称冒认SQL；未逐行全文 |
| `services/admin/control_auth.py` | I | AST/导入/实际调用入口与冻结diff归类；间接操作沿相应领域repo，不按execute名称冒认SQL；未逐行全文 |
| `services/admin/group_admin.py` | I | AST/导入/实际调用入口与冻结diff归类；间接操作沿相应领域repo，不按execute名称冒认SQL；未逐行全文 |
| `services/admin/group_recovery.py` | D | enable原actor/绑定、远端成员证明在writer前，写时原state/connection/receipt再核 |
| `services/admin/memory_admin.py` | D | 真实三组prepare_write+write_session与原audit原子边界，supplied身份helper复用session |
| `services/admin/preference_admin.py` | I | AST/导入/实际调用入口与冻结diff归类；间接操作沿相应领域repo，不按execute名称冒认SQL；未逐行全文 |
| `services/agent_runner.py` | I | AST/导入/实际调用入口与冻结diff归类；间接操作沿相应领域repo，不按execute名称冒认SQL；未逐行全文 |
| `services/agent_tools.py` | D | execute/_database_failure与workspace/Social内层catch；公开phase+三态对非SQLA也保留，纯参数ValueError不改成DB故障 |
| `services/automation_commands.py` | I | AST/导入/实际调用入口与冻结diff归类；间接操作沿相应领域repo，不按execute名称冒认SQL；未逐行全文 |
| `services/autonomous_groups.py` | I | AST/导入/实际调用入口与冻结diff归类；间接操作沿相应领域repo，不按execute名称冒认SQL；未逐行全文 |
| `services/chat.py` | D | stage/ready/discard input、actor read、source/lease准备；quoted wait/pending work不借平台messageID补所有权 |
| `services/chat_preparation_timings.py` | P | 本轮复核为schema/类型/固定声明/纯转换；不自行取得主库session，未把此级别计全文 |
| `services/client_link.py` | F | 全文；实际领域事务、外部效果、三态/锁序/取消按正文记录 |
| `services/command_service.py` | I | AST/导入/实际调用入口与冻结diff归类；间接操作沿相应领域repo，不按execute名称冒认SQL；未逐行全文 |
| `services/commands/__init__.py` | P | 本轮复核为schema/类型/固定声明/纯转换；不自行取得主库session，未把此级别计全文 |
| `services/commands/admin.py` | D | group_revision reader与prepare_chat_command→操作executor；不把外部command execute误认SQL |
| `services/commands/conversation.py` | D | status reader只投影checkpoint元数据，无内容重建或写 |
| `services/commands/group.py` | I | AST/导入/实际调用入口与冻结diff归类；间接操作沿相应领域repo，不按execute名称冒认SQL；未逐行全文 |
| `services/commands/help.py` | I | AST/导入/实际调用入口与冻结diff归类；间接操作沿相应领域repo，不按execute名称冒认SQL；未逐行全文 |
| `services/commands/identity.py` | I | AST/导入/实际调用入口与冻结diff归类；间接操作沿相应领域repo，不按execute名称冒认SQL；未逐行全文 |
| `services/commands/memory.py` | I | AST/导入/实际调用入口与冻结diff归类；间接操作沿相应领域repo，不按execute名称冒认SQL；未逐行全文 |
| `services/commands/models.py` | P | 本轮复核为schema/类型/固定声明/纯转换；不自行取得主库session，未把此级别计全文 |
| `services/commands/plugins.py` | I | AST/导入/实际调用入口与冻结diff归类；间接操作沿相应领域repo，不按execute名称冒认SQL；未逐行全文 |
| `services/commands/work.py` | I | AST/导入/实际调用入口与冻结diff归类；间接操作沿相应领域repo，不按execute名称冒认SQL；未逐行全文 |
| `services/concurrency.py` | I | AST/导入/实际调用入口与冻结diff归类；间接操作沿相应领域repo，不按execute名称冒认SQL；未逐行全文 |
| `services/config_commands.py` | I | AST/导入/实际调用入口与冻结diff归类；间接操作沿相应领域repo，不按execute名称冒认SQL；未逐行全文 |
| `services/context_assembler.py` | D | 两个read_version_matches调用与历史装配在reader，没有supplied writer DAG |
| `services/context_boundary.py` | P | 本轮复核为schema/类型/固定声明/纯转换；不自行取得主库session，未把此级别计全文 |
| `services/context_compaction.py` | D | PublicCompactReceipt read/write与publication；大payload编码在writer前，原priv/source/current ownership在写时 |
| `services/deduplication.py` | P | 本轮复核为schema/类型/固定声明/纯转换；不自行取得主库session，未把此级别计全文 |
| `services/document_reader.py` | P | 本轮复核为schema/类型/固定声明/纯转换；不自行取得主库session，未把此级别计全文 |
| `services/durable_invocations.py` | D | lease/原source/恢复/只读结果；不创建第二套重放身份或模型账本 |
| `services/effect_gate.py` | I | AST/导入/实际调用入口与冻结diff归类；间接操作沿相应领域repo，不按execute名称冒认SQL；未逐行全文 |
| `services/evidence_observation.py` | I | AST/导入/实际调用入口与冻结diff归类；间接操作沿相应领域repo，不按execute名称冒认SQL；未逐行全文 |
| `services/evidence_state.py` | I | AST/导入/实际调用入口与冻结diff归类；间接操作沿相应领域repo，不按execute名称冒认SQL；未逐行全文 |
| `services/execution_sources.py` | D | recover_*源ID/原Person/Presence/generation/automation budget owner；只读装配，实际效果仍当前核 |
| `services/group_recovery.py` | I | AST/导入/实际调用入口与冻结diff归类；间接操作沿相应领域repo，不按execute名称冒认SQL；未逐行全文 |
| `services/history_projection.py` | I | AST/导入/实际调用入口与冻结diff归类；间接操作沿相应领域repo，不按execute名称冒认SQL；未逐行全文 |
| `services/history_query.py` | I | AST/导入/实际调用入口与冻结diff归类；间接操作沿相应领域repo，不按execute名称冒认SQL；未逐行全文 |
| `services/http_logging.py` | P | 本轮复核为schema/类型/固定声明/纯转换；不自行取得主库session，未把此级别计全文 |
| `services/image_preprocessor.py` | I | AST/导入/实际调用入口与冻结diff归类；间接操作沿相应领域repo，不按execute名称冒认SQL；未逐行全文 |
| `services/invocation_context.py` | P | 本轮复核为schema/类型/固定声明/纯转换；不自行取得主库session，未把此级别计全文 |
| `services/invocation_service.py` | D | T1/T2调用、typed capture、unknown效果、late original receipt/media、audit；实际模型/工具执行在writer外 |
| `services/main_agent_backend.py` | I | AST/导入/实际调用入口与冻结diff归类；间接操作沿相应领域repo，不按execute名称冒认SQL；未逐行全文 |
| `services/main_agent_contract.py` | P | 本轮复核为schema/类型/固定声明/纯转换；不自行取得主库session，未把此级别计全文 |
| `services/main_agent_turns.py` | D | compose/prepare_boundary/publish/commit_boundary/read_result；来源guard/DAG在reader，原publication共享writer |
| `services/media_process.py` | I | AST/导入/实际调用入口与冻结diff归类；间接操作沿相应领域repo，不按execute名称冒认SQL；未逐行全文 |
| `services/media_resolver.py` | I | AST/导入/实际调用入口与冻结diff归类；间接操作沿相应领域repo，不按execute名称冒认SQL；未逐行全文 |
| `services/message_splitter.py` | P | 本轮复核为schema/类型/固定声明/纯转换；不自行取得主库session，未把此级别计全文 |
| `services/model_context_projection.py` | P | 本轮复核为schema/类型/固定声明/纯转换；不自行取得主库session，未把此级别计全文 |
| `services/native_media.py` | I | AST/导入/实际调用入口与冻结diff归类；间接操作沿相应领域repo，不按execute名称冒认SQL；未逐行全文 |
| `services/native_tool_binder.py` | I | AST/导入/实际调用入口与冻结diff归类；间接操作沿相应领域repo，不按execute名称冒认SQL；未逐行全文 |
| `services/participation_diagnostics.py` | D | 诊断窗口不构成业务执行集合；NULL/cache缺样不补零，原owner只读 |
| `services/participation_feedback.py` | D | _read_facts/_feedback_current/_commit_pending/reconcile_page；page实际证据/迟到absence/paid ordinal与原seq；新增payload/run raw改动由A reviewer收口 |
| `services/participation_identity.py` | F | 全文；actor_ids/actor_identity 与当前 roots；普通接纳历史 actor 收集外提，保 SELF/review/proposal 的真实 target |
| `services/participation_ordinary.py` | D | event/admitted_scope、invitation/binding/continuation、runtime_context、观察回调及 promotion；原当前资格留库谓词，准备固定 actor ID 后 writer 窄身份读取，变化仅确认回滚后重备 |
| `services/participation_parameters.py` | P | 本轮复核为schema/类型/固定声明/纯转换；不自行取得主库session，未把此级别计全文 |
| `services/participation_snapshot.py` | F | 全文；实际领域事务、外部效果、三态/锁序/取消按正文记录 |
| `services/participation_trace.py` | D | trace coverage/source provenance只读，不能因文件名或旧P分类排除真实reader |
| `services/plugin_events.py` | I | AST/导入/实际调用入口与冻结diff归类；间接操作沿相应领域repo，不按execute名称冒认SQL；未逐行全文 |
| `services/plugin_sessions.py` | D | isolated history append→模型→原assistant append；原父Work reserve请求在实际模型dispatch前，无事务跨模型 |
| `services/policies.py` | I | AST/导入/实际调用入口与冻结diff归类；间接操作沿相应领域repo，不按execute名称冒认SQL；未逐行全文 |
| `services/processing_status.py` | P | 本轮复核为schema/类型/固定声明/纯转换；不自行取得主库session，未把此级别计全文 |
| `services/processor.py` | D | canonical/admission/work handoff、ordinary prepare/commit、quiet foreground与privacy gate调用；有writer phase的未知不装成source_changed/duplicate |
| `services/profile_commands.py` | I | AST/导入/实际调用入口与冻结diff归类；间接操作沿相应领域repo，不按execute名称冒认SQL；未逐行全文 |
| `services/prompt_composer.py` | P | 本轮复核为schema/类型/固定声明/纯转换；不自行取得主库session，未把此级别计全文 |
| `services/prompt_registry.py` | P | 本轮复核为schema/类型/固定声明/纯转换；不自行取得主库session，未把此级别计全文 |
| `services/qq_face_resolver.py` | P | 本轮复核为schema/类型/固定声明/纯转换；不自行取得主库session，未把此级别计全文 |
| `services/rate_limit.py` | P | 本轮复核为schema/类型/固定声明/纯转换；不自行取得主库session，未把此级别计全文 |
| `services/renderer.py` | P | 本轮复核为schema/类型/固定声明/纯转换；不自行取得主库session，未把此级别计全文 |
| `services/semantic_participation.py` | D | scene/source/host proposal/hydrate/reconcile/save；主库反馈先提交，独立snapshot原revision CAS随后，非跨库事务 |
| `services/subagent_execution.py` | D | cancel_commands/run恢复身份与模型边界；原run控制在主库reader外发出，未重跑原命令 |
| `services/turn_coordinator.py` | I | AST/导入/实际调用入口与冻结diff归类；间接操作沿相应领域repo，不按execute名称冒认SQL；未逐行全文 |
| `services/turn_execution.py` | D | _PrimaryDispatch.admit和compact_current；真正付费dispatch围栏、原paid响应保存/恢复、publication共享session |
| `services/turn_transcript.py` | I | AST/导入/实际调用入口与冻结diff归类；间接操作沿相应领域repo，不按execute名称冒认SQL；未逐行全文 |
| `services/video_frames.py` | P | 本轮复核为schema/类型/固定声明/纯转换；不自行取得主库session，未把此级别计全文 |
| `services/vision_rate_limit.py` | P | 本轮复核为schema/类型/固定声明/纯转换；不自行取得主库session，未把此级别计全文 |
| `services/vision_service.py` | I | AST/导入/实际调用入口与冻结diff归类；间接操作沿相应领域repo，不按execute名称冒认SQL；未逐行全文 |
| `services/work_resume.py` | D | 原owner/automation/plugin/self/message恢复与prep failure；有phase的WorkConflict不能吞为普通busy |
| `services/yuki_runtime.py` | I | AST/导入/实际调用入口与冻结diff归类；间接操作沿相应领域repo，不按execute名称冒认SQL；未逐行全文 |

本轮覆盖标签统计：{'P': 64, 'F': 19, 'I': 67, 'D': 61}。这些是审读粒度，不是验收分数。

## 后续交叉复审入口

第二、三轮应独立复核本轮新增 PreparedEmojiIntent / SocialSettlement 的真实消费者、所有原 target 合并/迁移及 late receipt；特别看 R1 的 I 行是否有间接 DB/外部效果漏入其他 owner。没有把未全文的 I 段写成全文完成，也没有将其从后轮覆盖清单删除。Wait/Feedback 跨 owner 修复需随最终共享树复跑；最终类型、Ruff 与全局验收见 root 汇总。本文不提前标记整个项目 R1 完成。

本轮本人修复与上述定向验证已收束，停止产品编辑，等待共享树冻结后轮转。独立 mypy cache 为 `D:/Temp/yuki-sqlite-validation/mypy-b`；本人测试日志归档在 `D:/Temp/yuki-sqlite-validation/r1-b-20261023`，不加入仓库。后续两轮仍须独立核当前算法必要 CPU 与真实未决成员规模，不把“保留原算法”当性能测量。
