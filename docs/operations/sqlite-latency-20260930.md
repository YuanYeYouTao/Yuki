# SQLite 与后端延迟修复范围

本轮对应 `755f725` 审计任务书的已确认路径，源码版本仍是未发布的 3.9.0 开发基线，数据库 head 为 `0084`。本文说明实现与验证范围，不是上线证明。

| 审计项 | 当前修复 |
| --- | --- |
| F01 | 过期 outbox processing 转 uncertain，保留原尝试，不自动重发；同尝试且凭据一致的迟到回执可确认；未分类 transport 异常不重试，仓库也检查安全未发送类别 |
| F02 | 申请 writer 后取领取时间；租约 UPDATE 的有效期条件使用 SQLite 执行时钟，排队期间到期的租约不能续期或提交 |
| F03 | 显式 BEGIN 只读快照读取固定内部事件和 rollup 后计算指纹；短 writer 内复核租约、generation、来源 revision 和 canonical owner；0082 闭合指纹来源变化的 revision 覆盖 |
| F04 | 执行轨迹和物理模型调用诊断共用有界异步 consumer，不在模型结果返回前等待 SQLite writer；冻结原身份、时间、用量和隐私代次，满载或失败可缺样本 |
| F05 | Provider 容量与前台预留统一由 Executor admission 控制；会话层只跟踪取消，不重复限流；排队后重新核验 dispatch 来源，普通后台 Work 不被抢占 |
| F06 | 已存在 Relationship 只读返回；缺失才申请 writer 并重新解析 owner 与创建，不使用旧只读快照重建关系 |
| F07 | 旧 prefix 的 JSON 解码和 wire 比较在 writer 前完成；写内复核同一 epoch/revision/source/contract，容量原子计算，淘汰只读 metadata |
| F08 | 同连接显式只读 snapshot 批量准备证据、计数、完整聚合及复制；首 DML 升级遇 native517 整体回滚、最多三次纯数据库重备；原 target/actor/source、候选签名和 Dream 实际模型输入冻结，变化明确拒绝，不重跑 classifier/model。移除内部 100000 截断，公开分页仍有界；保留必要 PK/source/owner 最终守卫和短邻接查询 |
| F09 | Dream owner/fact 准备在首次 writer 前完成，必要列批量读取并在写内按 PK、版本和归属复核；run/cluster 创建失败不留半条 run |
| F10 | Rebuild 只收尾当前 proposal 批次和有界无 proposal 小页；批量计数与回执查证，写内 CAS 和实际 UPSERT 结果决定状态；已 committed/skipped 二次收尾零写。trusted 引用依赖批量冻结并写内复核，单项来源失效不阻塞整页 |
| F11 | 空 notification、Emoji、Work repair/reclaim 和纯未来 wait 不申请 writer；owned_run 查询真实 Work state；wait 轮询失败单独报告，不阻止后续 automation claim |
| F12 | Work、子 Agent 和 Automation 监督同一心跳循环；明确 BUSY 只在确认期限内重试，LOCKED、续租失效及 meter 错误停止父执行并交回原恢复；自有取消不泄漏到上下文外 |
| F13 | SQLite holder 在真实 DBAPI commit/rollback 完成后清除，失败保留至成功 rollback、失效或连接退出；不访问失效连接的 info |
| F14 | 参与 snapshot connection 的创建、load、save、close 在同一专用线程；冻结保存 payload，提交取消后按真实结果更新 revision，缓存 pin 与淘汰共同串行 |
| F15 | 插件 per-key 更新和删除在最终 SQL 中核对 canonical owner、稳定 row ID 和 expected version；首次创建由唯一约束产生单一赢家 |

| 附录项 | 当前范围 |
| --- | --- |
| A01 | 明确空 nickname/card 保持 known；成功空值仅缓存作用域 known 标记，256 项/60 秒，命中不续期；复用原 runtime snapshot，迟到资料按原 Person 核验；可选人物/群名查询各限 0.25 秒 |
| A02 | Profile 在 stage 前读齐 owner、关系、别名、Space 和 Membership，短 immediate 事务序列化首次观察；未变昵称和群名不增加 revision，last_seen 仍保留 |
| A03 | metadata-only 插件通知进入有界队列，冻结 payload 与订阅身份，退出/撤权停止投递；SDK 显式 publish 仍等待结果 |
| A04 | Social 路由和 probe 在 writer 前完成，原回执 CAS 认领发送权，writer 复核 canonical/route/SELF，并在 claim 与真实调用前复核原 connection；scheduled SELF 按原冻结场景及 generation 核验，reset 后不发送 |
| A05 | context/rollup 准备移出 effect gate，完成后短源复核；首入尚无原 Work 时释放空 activation 再按同 source 激活，已有原 Work 的 required 压缩用原 checkpoint 等待、释放 lease，由原 job 与有界 wake 恢复，预算/history/receipt 不重建 |
| A06 | 未准备的首个输入立即让原 Work yield，取消 15 秒/200ms 轮询；只由顺序首个 ready 输入唤醒，媒体使用原 Work 持久引用，跨 activation/重启仍可恢复 |
| A07 | 原 call_key effect 先持久 accepted 再做派生审计，审计失败仅报告缺证，不改成功为 uncertain；冻结派发前隐私代次与 Conversation/owner，旧来源不得因延后审计回填或改归新 Binding，SDK 无 Work 强来源合同仍同步 |
| A08 | 条件历史重算、派生文本计算在显式只读快照完成，短 writer 复核来源、coverage、revision；晚 ASR 与 rollup 失效同提交。journal 仅批量新增缺失 blob 和引用差集，不重复写同一图片 |
| B01 | Dream checkpoint 批量写入且 marker 最后提交；恢复每页最多 128 个 cluster，按原已提交 operation 汇总，不重放效果 |
| B02 | 证据压缩和 backfill 的 source、keep、聚合及 Dream provenance 首 DELETE 前准备；同 SQLite snapshot 升级失败完整回滚、至多三次纯数据库重备 |
| B03、B04 | SelfReflection receipt 每页最多清理 500 条，写时重核引用及未完成窗口；候选只处理当前 fingerprint 的过期状态，保留原 ID 与幂等；0083 仅增加 receipt 引用索引 |
| B05 | Embedding claim/recover/reconcile/fail/显式 retry 使用有界必要列及原 status/hash/attempt/time CAS；原领取身份迟到不能改新领取，显式重试保留现行预算 policy 且更新时间严格递增 |
| B06 | Rebuild privacy selection 锁外批量解析全部别名；完整目录元数据与单项 CAS 拒绝过期计划，整个隐私事务回滚后有限重备。hygiene 显式维护由调用者选择 FTS 重建，不悄悄在普通清理中全量重建 |
| B08、B09 | artifact hash/fsync 在 writer 外；Manager 原线程共享 connection 将发布与终态原子提交，取消只回收 pending，commit 确认丢失保留已发布 blob；GC 按真实引用复核，独立 bridge cache 命中只读 |
| D01 | 创建时在最终 writer 内重核 canonical 权限、来源和 active 上限；同 key 幂等结果先于容量拒绝。resume/run_now 保留原产品 policy |
| D02 | 普通 create/update/pause/resume/cancel/run_now 的领域写入和强制审计共享事务；审计失败同回滚，审计 before 使用 writer 当前版本 |
| D03 | SEND/MUTATE 派发后无终态证据保留 uncertain，READ/派发前超时保留失败；原 cursor/receipt 不重建，真实接受和已完成请求用量不丢失、不重复累计 |
| E01 | 两个 delay key 当前配置只索引一次，按原 USER/GROUP/GLOBAL 继承计算极值，O(N+U+G)，领域写入与审计原子性不变 |
| E02 | plugin session/state 首 DML 前查证身份并准备 DTO；sequence 与 message 同事务，保留原主体与 session ID；周期 session/state 过期清理按有界页复核，启动 ephemeral 清理保留完整生命周期语义 |
| E03、E04 | Emoji 迟到 analysis/complete/fail 核原 claim token，replacement 后 remove/adopt 同短事务；Speech 首写前准备可信来源及 references；Media save 的序列化锁外，保留精确 key 查询和未绑定条件关联 |
| E05 | 隐私仍为大原子事务；plugin 按 canonical owner/原 grant 集合删除，JSON/audio 解析后规范化脱敏，候选包含转义键和值，旧损坏 JSON 整体回滚；不拆 commit 或新增分批隐私状态 |
| E06 | 元数据每页最多 128 条只读发现、条件写重核期限/status；文件提交后 GC 在线程内以行数/路径字节/时间小页推进，保留 publish 锁、真实引用与文件身份；0084 最小清理和 Work wait 索引消除已实测 full scan+排序 |

## 验证方式

开发中仅运行各模块直接相关的既有测试及新增真实 SQLite 锁竞争、取消、迟到回执、隐私删除和 CAS 回归。最终 PR CI 执行完整检查，不在开发中反复跑全量套件。

回归分别验证：持有其他 writer 时模型结果与后续调用仍可返回；已有关系无 DML；future wait/空队列无 BEGIN IMMEDIATE；snapshot 保存等待时事件循环仍响应；来源读取后发生删除、元数据修改、owner 变化或租约到期时不能 dispatch；混合后台请求仍保留前台容量。合成结果不替代生产负载或真实 QQ 验收。

诊断队列过载、失败和退出时可能缺少样本，统计为最终可见且 coverage 可不完整。聊天账本、模型预算、Work journal、权限核验和外部效果回执继续走原持久化合同。详见 [执行诊断](../architecture/execution-trace.md)。

## 附录 C 入口覆盖

以下均为仓库内真实入口/数据库与受控 fake transport 回归，最终 CI 核验当前提交；它们不发送真实 QQ 消息、不作为 Provider 实际耗时证明。

| 入口 | 代表性回归文件 | 保留条件 |
| --- | --- | --- |
| 普通聊天，Work 开与关 | `test_chat_context_preparation_gate.py`、`test_runtime_work.py` | 原入站内部 ID、PromptProjection 或 Work journal，准备等待不阻 reset |
| SELF、主动发起 | `test_self_initiative_main_entry.py`、`test_scheduled_self_social_fence.py` | 原 Presence/Space、NO_REPLY、silent completion；不借真人权限 |
| 模型自动化和 DSL | `test_automation_unified_delivery.py`、`test_self_automation_delivery.py`、`test_automation_timeout_certainty.py` | 原 run/step/cursor，直接发送不伪造模型请求；未知效果不重发 |
| 插件主生成 SDK | `test_main_agent_entrypoints.py`、`test_plugin_reply_causality.py` | 同主入口；pending 句柄不等于实际发出 |
| 插件自主轮和通知 | `test_plugin_background_worker_recovery.py`、`test_plugin_notification_idempotency.py` | 原 job/outbox/source，领取恢复与结果未知分别处理 |
| plugin agent sessions | `test_storage_writer_boundaries.py`、`test_canonical_conversation_runtime.py` | 独立计算会话，sequence/message 原子，canonical 主体不分裂 |
| SDK 直接发送 | `test_plugin_facades.py`、`test_tool_effect_audit.py` | 原 callback ordinal、Social receipt；稳定 replay 不是正文去重 |
| 子 Agent | `test_subagents.py`、`test_runtime_work.py`、`test_rollup_scheduling.py` | 原根预算/child journal；不可取得主 Agent 发送权限，后台容量不挤占前台预留 |
| ASR、vision、rollup | `test_asr.py`、`test_derived_text_transactions.py`、`test_work_context_rollup_wait.py` | 内部事件、原 generation、provider admission；派生更新不得覆盖 reset |
| 管理 reset/privacy | `test_runtime_work.py`、`test_privacy_preparation.py`、`test_privacy_json_review.py` | 确定性管理权限和完整隐私事务，不额外调用模型 |

固定版本 semantic-participation 包仅核对宿主及 SnapshotStore 选定路径，不把本轮报告写成外部包全部算法审计。

## 部署与验收边界

按生产手册检查最终 Compose 镜像、保留旧镜像与一致性数据库备份，只替换 Bot，核对 `0084`、OneBot 重连、健康及重启次数。旧镜像严格校验 `0081`，代码回退前需用新镜像将 schema downgrade 到 `0081`；仅撤销新增索引和触发器，不能用旧数据库备份覆盖新消息与回执。

任务书逐项实现与正确边界见上表。F08 不只用 fact.updated_at 作为聚合围栏，原只读 snapshot 的升级覆盖 evidence 级联、源隐藏和 owner 变化；重备也复核原模型判断的依赖，不能把旧内容归给新的 Binding。内部聚合与 compaction 使用完整可读证据，超过旧 100000 cap 的结果有意纠正，不宣称极端截断口径等价。B07、D04、E07 所列正确恢复边界继续保留：Manager commit 后才 ACK，Control mutation/audit/receipt 原子，unknown 外部控制与发送不自动改 pending，正式 MCP manage 无持 writer 网络调用。Provider 自身响应、网络与产品发送节奏不属于本轮消除的等待；Emoji cache hit_count 是现行 policy，本轮保留。

已删除被替代的逐条 evidence 过滤循环、Dream owner 查询循环、单条 checkpoint 包装、三个独立 pulse、会话层重复 admission 和候选全局过期扫描；同时删除无生产调用的 profile 删除别名、Automation owner 包装及 owns 路由、Rebuild proposal_counts 与两层 forget_person 包装、Embedding pending_count、plugin session list_scope、Speech expire_before 和旧审计 helper。仍有生产调用的 canonical guard、历史兼容和原回执恢复代码保留。
