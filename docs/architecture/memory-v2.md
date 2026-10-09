# Memory 当前架构

本文是 canonical 架构下的现行 Memory 合同；版本基线见 [README](../../README.md)。实施与验收见
[Memory 删除优先任务书](Yuki-Memory-Provider分工与后台收尾审查任务书-2026-10-09.md)。

## 所有权与记忆层次

一个数据库是一个永久 Yuki。Person、Space 使用 canonical UUID；QQ 号仅通过 Binding
解析，Presence/Provider 是传输身份，不划分记忆所有权。更换网关实现或 Yuki 账号
不重建记忆或 Conversation/Rollup 所有权，也不因账号切换重置聊天 generation；
Route 有独立的 `route_generation`，有效发送路由迁移、暂停或恢复可推进它。

| 分类 | 所有者与含义 |
|---|---|
| Person | 人物的结构化事实、持续偏好及有意义经历 |
| PersonGroup | 某人物在某 canonical Space 的事实与第三方报告 |
| Group | canonical Space 的共同事实与经历 |
| SELF | Yuki 的动态自我事实、偏好、经历；global/current-private/current-group 可见性 |

History 是不可变事件账本；Rollup 是短期上下文压缩，不等于长期事实。
事实的版本、证据、来源、可信度、争议和 canonical 所有权保留在长期 Memory。
external_event 保持 external_untrusted，不伪装成人类聊天，不自动进入人物记忆或关系。
插件的受控读取也不产生普通用户社会关系授权。

## 提取与事实写入

Worker、Main Agent、自省和 Dream 的新事实复用 [变更合同](memory-change.md)。提取保留真实主体、
实际来源引用、原模型 confidence 和原 evidence；不设 retention/importance 最低价值门槛或待确认
候选评分队列。无事实可记录时允许正常空输出。

事实正文使用真实身份或名片。历史引用继续匹配原实际来源，不用当前展示名改写旧 quote。
同 owner/key 可以创建独立 active 事实；纠正和撤回按实际 fact ID 执行。0102 只删除四个单 active
唯一索引，保留已部署 0101 的全部事实与历史回执。

提取、自省和 Dream 分别绑定 `memory_extraction`、`memory_self_reflection`、`memory_dream`
任务，可各自选择 Profile，也可共享配置；Rebuild 复用提取，Embedding 独立配置。结构化
输出沿 Profile 显式模式，不按 Provider 或 Memory 任务强制 JSON Schema。
自省生成结构与实际来源预算见 [自省](self-reflection.md)，Dream 运行见 [运维](../operations/memory-quality.md)。
自省所有结果都可保存完整真实引用，
不强制把同一来源复制成 episode 和 proposal。SELF kind/category/key 不设固定标签白名单；
私聊资料向 global 传播继续核验真实可见范围。

## 历史共同群读取

所有普通用户结构化读取使用后端 `MemoryReadScopeResolver`。设请求者 R，目标人物 P，
G(X) 为数据库记录的 X 的历史 canonical membership：

| 目标 | 允许条件 |
|---|---|
| R 本人的 Person | 本人 |
| 他人的 Person | G(R) 与 G(P) 存在直接交集 |
| Group H | H 属于 G(R) |
| PersonGroup(P,H) | H 同时属于 G(R)、G(P) |
| SELF | 原有 global/current-private/current-group 规则，不按历史群扩权 |

**有共同群关系即可读取目标完整 Person 结构化事实，包括私聊来源的事实。**
这是明确接受的隐私取舍；在群聊里查询可能向其他成员展示这些事实。它不开放原始私聊、
完整 evidence、其他人的 private SELF，也不是第三方写入授权。

成员关系不依赖实时网关群列表、群 enabled、路由 paused、Provider 在线或当前 Presence。
退群但历史记录仍在时关系仍有效；不做朋友的朋友等传递授权。forget 删除关系后下一次查询
依据剩余数据库记录重新判断，不缓存永久许可。当前在群 G，也可查历史共同群 H。
Binding ID、群号、昵称、工具参数都只是选择器，不是模型自己声明的权限。

`search_memory` 和 fact detail 复用相同政策；旧的“只凭当前群 evidence
投影 Person”授权路径已经删除。evidence 仍走本人/显式管理授权边界。
无真实用户主体的 Plugin、Automation、System 使用既有受控目标；不能伪造 actor 自行扩权。
Control Plane 仍要求 capability。

## 检索与使用

[检索合同](memory-v2-retrieval.md)定义 Query Plane、主动查询参数、检索与排序。
主 Agent 不再每轮自动注入长期事实；需要过去事实时由模型调用统一
`search_memory`。无显式目标时，后端以本次真实主体的 canonical 身份和历史成员关系，
在数据库层筛选全部获准 Person、PersonGroup、Group 与当前可见 SELF；历史 owner 即使没有
活跃 QQ Binding 仍可检索。词法和向量候选在授权范围内全局排序，不按 owner 截断。
全局候选或向量扫描触及工作预算时回执标记 `truncated=true`、`exhaustive=false` 和原因；
实际返回条数上限也另行标记。真实中文检索集校准和上线验收仍在
[search_memory 任务书](Yuki-search-memory-taskbook-2026-09-29.md)中跟踪。

## 维护与变更边界

维护只根据事实自身 valid_until 处理到期失效，不按年龄、固定置信度权重或 scope 容量淘汰历史。
模型审计、重复合并治理队列、归因与强化运行链已退休；管理权限和真实 SQL 来源审计保留。

- 证据明细及 readable evidence count 共用同一 SQL 来源谓词，按 canonical event、普通
  tool receipt 或无事件 SELF initiative 分支核验来源、owner、隐藏状态及 SELF 可见范围。
  明细在 SQL 中过滤后排序，公开分页继续应用请求的 limit，不逐 evidence 查来源。
  内部聚合和完整 lineage 读取全部可读证据；移除旧查询的 100000 条保护截断，因此超过
  该规模的历史尾部现在也参与复制。这是极端规模的行为修正；原始 confidence/authority 和来源资格保留。
  Memory mutation、Dream 与维护批次在同连接显式只读 BEGIN 中先准备完整证据、
  来源和目标归属，再升级为短写事务。SQLite WAL 快照是本次准备的完整依赖围栏；
  任何竞争提交都使旧快照的首次写入被拒绝，不能仅用 fact.updated_at 推断来源未改变。
  只对原生 SQLITE_BUSY_SNAPSHOT（517）结束整个失败事务并用新 session 至多重备 3 次。
  重备仅执行纯数据库单元，复用原 mutation/operation/request ID，不重跑模型或外部效果；提交后的 embedding 调度不在重试范围内。模型判断所引用的事实还须
  在准备阶段比较原 fact signature 和 canonical target，拒绝已经改变的候选；请求目标与
  操作人的 canonical owner、原内部事件和 tool receipt／SELF 来源证明也必须与原计划一致。
  ORM flush属于该纯数据库单元；物理commit/rollback确认错误不作为可安全重备的517。
  Dream 操作失败先核对原 cluster 的 committed operation 回执，保留实际提交计数和累计预算。
  未提交的派生模型判断可由既有恢复流程重新生成；未决提交不授予重放资格。
  来源隐藏、擦除、换绑或会话 generation 变化时拒绝旧计划，不能成功写入一个无证据事实。
  Dream 将实际模型输入、选中证据身份与内容及 canonical 分区纳入输入指纹，持久 preview
  复用同一指纹；每次新快照首写前核验，证据数量不变不能证明原模型来源仍然有效。
  准备缓存仅属于当前事务，提交或回滚后释放，不增加持久 revision 或事实源。
  后续写入只累计实际新增证据；版本复制同时批量核验原来源与新目标资格，批量插入后
  更新临时计数，写入期间不重新扫描历史。必要的主键、状态、来源／owner 最终
  复核及两跳关系短查询保留，不以 writer 内零 SELECT 作为验收条件。
  共享事务调用者必须在其最早的领取围栏、
  回执或状态写入前准备整个批次，缺少准备时拒绝，不能退回写后历史读取。
  Control 的确认／隔离保留原能力判定；revision 核验、savepoint、审计和回执仍共用
  同一事务，证据准备早于本次首写，不拆分提交。旧管理入口的新增／修改／删除同样
  将记忆变更和管理审计作为一个纯数据库单元准备与提交。
  Evidence compaction 也使用同连接的 SQLite WAL 显式读快照：删除集合、保留证据和元数据
  和 Dream provenance 回写资料均在首个 DELETE 前准备。若任意并发提交使快照过期，
  写入升级失败并整体回滚，最多重新准备三次；不重新领取 item、不更换 operation ID。
  DELETE 后只应用已准备的元数据与 provenance，不扫描证据历史。旧 delegation_mode 解析反推 run 的结果回填已删除；新自省结果以实际 run ID 原子登记，
  不根据旧字符串推断归属。
    候选的已处理过滤在 LIMIT 前完成，避免不可缩减前缀阻塞后续 fact。
    `0093` 的 `(fact_id,evidence_before,status)` 完整非唯一索引支持精确终态回执查找；
    原 `(run_id,fact_id)` 唯一约束、状态和删除级联保持，证据聚合与来源排序仍读完整输入。
    没有候选且没有原 running run 的空轮询只读返回，不创建空 run；既存 run 继续按原
    ID 恢复和结束，没有 processing item 时不执行空的恢复 UPDATE。
- 不用 /ai new、清空事实或重建 embedding 掩盖队列/召回问题。
- 未来 WebUI 复用 Control Plane，不直接查询 ORM；读取、content、mutation、destructive
  能力边界继续分离，secret 永不返回。
- [第三方事实写入](memory-v2-third-party-facts.md)、
  [质量架构](memory-v2-quality.md)仍是对应领域合同。
  历史验收记录不覆盖本页。

## 后台关系评估

关系评估使用 BEST_EFFORT_BACKGROUND，在共享 Executor admission 中排队，
没有额外会话层 semaphore。前台与后台由同一容量计数原子准入；并发大于 1 时，
非前台最多占总容量减 1，排队前台优先。BEST_EFFORT 请求仍可被真正前台抢占，
普通 durable 后台 Work 不因该预留而被抢占；总并发为 1 时不能保留额外前台名额。
关系批次被抢占或工作者关闭时释放原 claim，立即回到 pending，30 秒后具备再次领取资格；
实际领取仍受轮询和前台负载影响，不承诺 30 秒内恢复。这些让出不增加失败 attempts。
关闭工作者会取消等待中或执行中的关系评估，不等待无关前台请求结束；取消原样传播，
不会被转换成模型失败。真实模型、校验或提交冲突继续按原有界失败预算处理。

延后、完成和失败都核验原 claim 的领取时间，迟到工作者不能覆盖新领取者或终态。
自动关系变更先读身份、来源和当天额度，再在短写事务内核验 claim 和人物关系版本，
原子提交完成状态、分值与审计事件。相同 Person 的并发旧快照冲突时整体回滚并有界重试，
不覆盖新分值，也不在写锁内重扫当天历史。新建关系行的唯一约束冲突同样回滚。

关系 claim 同时携带证据所属会话的 generation。共享模型排队结束和每次实际 HTTP 请求前
只读复核原 claim 与 generation；落分的首个条件写再次原子核验。重置会话或忘记同群其他人
使旧证据失效时，不再用这份缓存资料发起请求或提交评分。失效任务以明确原因结束，不消耗
模型失败次数；同批仍有效的任务延期后重新领取。检查不会改变提示词、工具声明或计费身份，
也不能撤回失效前已经发送到 Provider 的请求；其迟到结果仍不得落分。

主 Agent 工具声明保持固定；目录查询不改变已提交声明。
