# SQLite 与后端延迟修复范围

本轮对应 `755f725` 审计任务书的已确认路径，源码版本仍是未发布的 3.9.0 开发基线，数据库 head 为 `0083`。本文说明实现与验证范围，不是上线证明。

| 审计项 | 当前修复 |
| --- | --- |
| F01 | 过期 outbox processing 转 uncertain，保留原尝试，不自动重发；同尝试且凭据一致的迟到回执可确认；未分类 transport 异常不重试，仓库也检查安全未发送类别 |
| F02 | 申请 writer 后取领取时间；租约 UPDATE 的有效期条件使用 SQLite 执行时钟，排队期间到期的租约不能续期或提交 |
| F03 | 显式 BEGIN 只读快照读取固定内部事件和 rollup 后计算指纹；短 writer 内复核租约、generation、来源 revision 和 canonical owner；0082 闭合指纹来源变化的 revision 覆盖 |
| F04 | 执行轨迹和物理模型调用诊断共用有界异步 consumer，不在模型结果返回前等待 SQLite writer；冻结原身份、时间、用量和隐私代次，满载或失败可缺样本 |
| F05 | Provider 容量与前台预留统一由 Executor admission 控制；会话层只跟踪取消，不重复限流；排队后重新核验 dispatch 来源，普通后台 Work 不被抢占 |
| F06 | 已存在 Relationship 只读返回；缺失才申请 writer 并重新解析 owner 与创建，不使用旧只读快照重建关系 |
| F07 | 旧 prefix 的 JSON 解码和 wire 比较在 writer 前完成；写内复核同一 epoch/revision/source/contract，容量原子计算，淘汰只读 metadata |
| F08（部分） | 证据明细和计数共用 SQL 可读性谓词，三种来源链按索引核验，去掉逐项 event/receipt/owner 查询；聚合仍在原 mutation 事务中 |
| F09 | Dream owner/fact 准备在首次 writer 前完成，必要列批量读取并在写内按 PK、版本和归属复核；run/cluster 创建失败不留半条 run |
| F10 | Rebuild 只收尾当前 proposal 批次和有界无 proposal 小页；批量计数与回执查证，写内 CAS 和实际 UPSERT 结果决定状态；已 committed/skipped 二次收尾零写。trusted 引用依赖批量冻结并写内复核，单项来源失效不阻塞整页 |
| F11 | 空 notification、Emoji、Work repair/reclaim 和纯未来 wait 不申请 writer；owned_run 查询真实 Work state；wait 轮询失败单独报告，不阻止后续 automation claim |
| F12 | Work、子 Agent 和 Automation 监督同一心跳循环；明确 BUSY 只在确认期限内重试，LOCKED、续租失效及 meter 错误停止父执行并交回原恢复；自有取消不泄漏到上下文外 |
| F13 | SQLite holder 在真实 DBAPI commit/rollback 完成后清除，失败保留至成功 rollback、失效或连接退出；不访问失效连接的 info |
| F14 | 参与 snapshot connection 的创建、load、save、close 在同一专用线程；冻结保存 payload，提交取消后按真实结果更新 revision，缓存 pin 与淘汰共同串行 |
| F15 | 插件 per-key 更新和删除在最终 SQL 中核对 canonical owner、稳定 row ID 和 expected version；首次创建由唯一约束产生单一赢家 |

| 附录项 | 当前范围 |
| --- | --- |
| A01（部分） | 入站明确给出的空 card 保持 known，不逐条重复查网关；可选人物和群名网络查询各自最多等待 0.25 秒。API 空值负缓存及重复 runtime snapshot 尚未改动 |
| A02 | Profile 在 stage 前读齐 owner、关系、别名、Space 和 Membership，短 immediate 事务序列化首次观察；未变昵称和群名不增加 revision，last_seen 仍保留 |
| A03 | metadata-only 插件通知进入有界队列，冻结 payload 与订阅身份，退出/撤权停止投递；SDK 显式 publish 仍等待结果 |
| A04 | Social 路由和 probe 在 writer 前完成，原回执 CAS 认领发送权，writer 复核 canonical/route/SELF，并在 claim 与真实调用前复核原 connection；scheduled SELF generation 补充围栏仍在复审 |
| B01 | Dream checkpoint 批量写入且 marker 最后提交；恢复每页最多 128 个 cluster，按原已提交 operation 汇总，不重放效果 |
| B03、B04 | SelfReflection receipt 每页最多清理 500 条，写时重核引用及未完成窗口；候选只处理当前 fingerprint 的过期状态，保留原 ID 与幂等；0083 仅增加 receipt 引用索引 |
| D01 | 创建时在最终 writer 内重核 canonical 权限、来源和 active 上限；同 key 幂等结果先于容量拒绝。resume/run_now 保留原产品 policy |
| D02 | 普通 create/update/pause/resume/cancel/run_now 的领域写入和强制审计共享事务；审计失败同回滚，审计 before 使用 writer 当前版本 |
| D03 | SEND/MUTATE 派发后无终态证据保留 uncertain，READ/派发前超时保留失败；原 cursor/receipt 不重建，真实接受和已完成请求用量不丢失、不重复累计 |

## 验证方式

开发中仅运行各模块直接相关的既有测试及新增真实 SQLite 锁竞争、取消、迟到回执、隐私删除和 CAS 回归。最终 PR CI 执行完整检查，不在开发中反复跑全量套件。

回归分别验证：持有其他 writer 时模型结果与后续调用仍可返回；已有关系无 DML；future wait/空队列无 BEGIN IMMEDIATE；snapshot 保存等待时事件循环仍响应；来源读取后发生删除、元数据修改、owner 变化或租约到期时不能 dispatch；混合后台请求仍保留前台容量。合成结果不替代生产负载或真实 QQ 验收。

诊断队列过载、失败和退出时可能缺少样本，统计为最终可见且 coverage 可不完整。聊天账本、模型预算、Work journal、权限核验和外部效果回执继续走原持久化合同。详见 [执行诊断](../architecture/execution-trace.md)。

## 部署及剩余范围

按生产手册检查最终 Compose 镜像、保留旧镜像与一致性数据库备份，只替换 Bot，核对 `0083`、OneBot 重连、健康及重启次数。旧镜像严格校验 `0081`，代码回退前需用新镜像将 schema downgrade 到 `0081`；仅撤销新增索引和触发器，不能用旧数据库备份覆盖新消息与回执。

本轮没有完成整个审计任务书。F08 的全历史 aggregate 尚未移出 writer；仅 fact.updated_at 不能覆盖 evidence 级联删除、源隐藏与 owner 变化，不能使用不完整围栏写回旧聚合。A05—A08、B02、B05、B06、B08、B09 的效果 gate、未准备输入轮询、历史重算、Embedding 和其他存储路径仍待分别处理；A07 强制审计不能随 telemetry 一起丢弃。B07、D04 所列正确恢复边界继续保留。Provider 自身响应、网络与产品发送节奏不属于本轮消除的等待。

已删除被替代的逐条 evidence 过滤循环、Dream owner 查询循环、单条 checkpoint 包装、三个独立 pulse、会话层重复 admission 和候选全局过期扫描；同时删除无生产调用的 profile 删除别名、Automation owner 包装及 owns 路由、Rebuild proposal_counts。仍有生产调用的 canonical guard、历史兼容和原回执恢复代码保留。
