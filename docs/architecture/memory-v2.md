# Memory 当前架构

本文是 canonical 架构下的现行 Memory 合同；版本基线见 [README](../../README.md)。实施与验收见
[Yuki Memory P1 治理任务书](Yuki-Memory-P1治理任务书.md)。

## 所有权与记忆层次

一个数据库是一个永久 Yuki。Person、Space 使用 canonical UUID；QQ 号仅通过 Binding
解析，Presence/Provider 是传输身份，不划分记忆所有权。更换 NapCat、SnowLuma 或 Yuki 账号
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

## 自动提取与价值

普通 Memory Job 按 canonical 所有者聚合，数据库是批次就绪的唯一真源：
30 秒轮询；累计 12 条、8,000 字符或最老事件等待 3,600 秒之一满足即可领取；
单批最多 12 条、8,000 字符，提取输出预算 4,096 tokens。
不跨 Person/Space 拼批，不因 Presence 改变拆队列。一小时是到期领取条件，不是积压或宕机
情况下的完成承诺，也不是 Rollup、反思、lease 或重试间隔。

领取返回的 `updated_at` 是该次 Memory Job 的执行身份。事实、候选、变更回执及完成/失败
状态都在各自的短提交事务内核验原领取身份；超时重领后，旧工作者丢弃尚未提交的结果，
不能覆盖新领取或重新打开完成任务。已提交回执保留，新工作者按原事件幂等恢复；模型调用
和身份解析仍在取得写锁前完成，不为维持领取身份长期占用 SQLite 写锁。

自动输出必须显式给出 retention、source_style、importance、confidence 和简短 value_reason。
仅 durable/meaningful_episode 且 importance ≥ 3 的候选进入主体、来源、证据与可信度流程。
有意义的一次性共同经历可以达标；问候、临时要求、无进展的调侃主要留在 History/Rollup。
数值门槛只是结构执行合同，不声称代替模型的语义判断。

低价值直接正常跳过，不转入另一条候选队列。高价值但主体/可信度待确认的内容使用已有候选
机制。后台来源不能通过填写 explicit 获得用户权威。反思的新 SELF 内容复用价值门槛；
无价值新内容允许 noop，完成批次并推进水位。已有事实的纠正、撤回、证据补强、去重合并
和 Dream 维护不受首次写入最低价值阻碍，也不能借维护引入未经验证的新事实。
不批量重新审判或删除旧事实。

明确的用户记住、纠正、删除请求继续由完整 Main Agent 调用即时
[`memory_change`](memory-change.md)，不等待聚合窗口，不用自然语言正则认定显式权威。
主体、第三方写入、证据、SELF 可见性与受保护键规则保持独立。

### 自省的配置与结构化安全

运行、预算、持久重试和管理报告见 [Self Reflection](self-reflection.md)。

自省按 canonical 会话所有者读取配置：群任务使用 Space，私聊任务使用 Person。
首条 evidence 即使是 Yuki 的旧/新 Presence 或工具回执，也不承担配置主体角色。
配置读取允许已有停用所有者，任务准入仍独立判断；不存在或类型错误的引用失败关闭。

自省 schema 和本地校验一致限制最多 8 个 proposals、1 个 episode；episode 使用
整条最多16个唯一、真实允许的 evidence alias，每段1–8个。模型输出1–8个连续passages，每段包含
evidence_refs和content；后端按顺序以换行连接正文、按首次出现合并引用，整条正文
仍最多4000字符、不同来源最多16个。模型不另写未绑定来源的总述，旧的整条content/
evidence_refs输出不再接受。一次输出的引用先整体校验，再执行 mutation。
片段绑定只检查结构与真实引用，不证明每句话被语义支持；该生成策略仍须真实验收。
最多一次定向模型修复，携带原任务、原输入和作为不可信资料的失败输出；超长资料明确
标记截断。非法输出不删字段冒充成功，无值得记录的内容允许正常 noop。

本轮后续召回及 Dream 变更的验收要求见
[记忆可靠性与强相关召回任务书](Yuki-记忆可靠性与强相关召回任务书.md)，该任务书状态为
实施中时，不代表其中所有目标已上线。

### Dream 的意义与预算

Dream 完整保留输入事实正文，超预算先移除可选 evidence excerpt；仍超限则失败保留原事实，
不以截断源文再替换完整事实的方式继续。新正文最多四条、单条 800 字、合计 1600 字，
生成与正式 mutation 使用同一长度校验；0.45 压缩比只作软目标，未达到不触发重试。
默认输出预算 4096 tokens，移除旧 0.70 硬压缩比。

每簇最多两次生成，首次和修复都预占持久预算，每轮默认 12 簇/24 次。schema、来源或绝对
长度不合格时最多一次定向修复，保留原任务、原输入及不可信失败输出；两次均失败不合成
keep 成功。显式事实、来源覆盖、scope、重复/未知引用与原子 mutation 保护不变。

增量优先未尝试或指纹变化的簇，已尝试按最久未尝试优先；预算延期不当作已执行，不推进
成功 checkpoint。输入超限、执行失败和预算延期有独立的错误类别。

Dream 计划先在写事务外准备每簇的 canonical subject/visibility shape 和 fact 版本，
服务复用已加载的事实；独立仓库调用只批量读取必要的 owner、scope、kind、状态和
`updated_at` 列，不为 owner 解析完整 DTO 或统计可读 evidence。共享写会话必须提供
已准备的计划。短写事务按有界主键批量复核 shape 和版本后，原子登记 run 与全部
clusters；事实删除、owner 或版本变化时拒绝，不遗留半个 run。FULL 仍先为 PLANNED，
完整计划确认后才 start；INCREMENTAL 仍按原 RUNNING 和持久预算执行。

baseline 和 checkpoint 按 256 条批量 upsert，初始化 marker 在对应 checkpoint 全部
可用后写入，同一事务失败整体回滚。重启恢复按 128 个 processing clusters 聚合
committed operations 并批量读取 runs，全部读取完成后才更新状态和计数；已提交
operation 仍是恢复事实源，不重放 mutation、不重置模型预算或原回执 ID。


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

内部预取、`search_memory` 和 fact detail 复用相同政策；旧的“只凭当前群 evidence
投影 Person”授权路径已经删除。evidence 仍走本人/显式管理授权边界。
无真实用户主体的 Plugin、Automation、System 使用既有受控目标；不能伪造 actor 自行扩权。
Control Plane 仍要求 capability。

## 检索与使用

[检索合同](memory-v2-retrieval.md)定义 Query Plane、结构化 intent、检索与排序。
当前开发分支的主 Agent 不再每轮自动注入长期事实；需要过去事实时由模型调用统一
`search_memory`。无显式目标时，后端以本次真实主体的 canonical 身份和历史成员关系，
在数据库层筛选全部获准 Person、PersonGroup、Group 与当前可见 SELF；历史 owner 即使没有
活跃 QQ Binding 仍可检索。词法和向量候选在授权范围内全局排序，不按 owner 截断。
全局候选或向量扫描触及工作预算时回执标记 `truncated=true`、`exhaustive=false` 和原因；
实际返回条数上限也另行标记。真实中文检索集校准和上线验收仍在
[search_memory 任务书](Yuki-search-memory-taskbook-2026-09-29.md)中跟踪。

候选、实际注入、完成使用评估是三件不同的事。
零注入轮有 receipt，但不调用 attribution；旧 used=false 是未知而非确认未使用。
只有成功评估的 item 才进入使用率分母，失败、跳过、取消和重启中断单独统计。
Plugin/Admin 纯查询不产生强化或使用回执。详见
[指标口径](memory-v2-quality-metrics.md)、[质量运维](../operations/memory-quality.md)。

## 维护与变更边界

Activation 缺失修补按主键扫描有界来源窗口；每轮固定 high-water，游标按已扫描的来源推进，
即使没有缺失项也推进，扫到边界后回绕。新来源和状态变化在后续轮次回访；页内优先处理 active，
不保证每周期一定修满原 batch 数量。游标只在 worker 内存中保存，不是业务事实。
空修补与空治理恢复只读返回。过期治理 job 按 `status/claimed_at/id` 索引取候选，短写重核原 claim、
状态、期限及 attempts；Dream processing cluster 按 `status/id` 分页，在读快照准备完整聚合，
517 只重备原 operation 的数据库计划。0090 仅增加这两个候选索引。

- 证据明细及 readable evidence count 共用同一 SQL 来源谓词，按 canonical event、普通
  tool receipt 或无事件 SELF initiative 分支核验来源、owner、隐藏状态及 SELF 可见范围。
  明细在 SQL 中过滤后排序，公开分页继续应用请求的 limit，不逐 evidence 查来源。
  内部聚合和完整 lineage 读取全部可读证据；移除旧查询的 100000 条保护截断，因此超过
  该规模的历史尾部现在也参与聚合与复制。这是极端规模的行为修正，权重乘积公式、
  authority 继承、authority cap 和来源资格保持原 policy。
  Memory mutation、Dream 与维护批次在同连接显式只读 BEGIN 中先准备完整证据、聚合、
  来源和目标归属，再升级为短写事务。SQLite WAL 快照是本次准备的完整依赖围栏；
  任何竞争提交都使旧快照的首次写入被拒绝，不能仅用 fact.updated_at 推断来源未改变。
  只对原生 SQLITE_BUSY_SNAPSHOT（517）结束整个失败事务并用新 session 至多重备 3 次。
  重备仅执行纯数据库单元，复用原 mutation/operation/request ID，不重跑 classifier、
  模型或外部效果；提交后的 embedding 调度不在重试范围内。模型判断所引用的事实还须
  在准备阶段比较原 fact signature 和 canonical target，拒绝已经改变的候选；请求目标与
  操作人的 canonical owner、原内部事件和 tool receipt／SELF 来源证明也必须与原计划一致。
  ORM flush属于该纯数据库单元；物理commit/rollback确认错误不作为可安全重备的517。
  Dream三次操作级517经确认回滚后，按原cluster核对committed operation回执再登记
  失败或保留实际提交计数。登记及未决提交错误结束当前worker，health显示任务异常，
  后续自动tick不能把processing改回pending并再次调用模型；不重置原run预算。
  来源隐藏、擦除、换绑或会话 generation 变化时拒绝旧计划，不能成功写入一个无证据事实。
  Dream 将实际模型输入、选中证据身份与内容及 canonical 分区纳入输入指纹，持久 preview
  复用同一指纹；每次新快照首写前核验，证据数量不变不能证明原模型来源仍然有效。
  准备缓存和聚合乘积仅属于当前事务，提交或回滚后释放，不增加持久 revision 或事实源。
  后续写入只累计实际新增证据；版本复制同时批量核验原来源与新目标资格，批量插入后
  更新临时计数和聚合，写入期间不重新扫描历史。必要的主键、状态、来源／owner 最终
  复核及两跳关系短查询保留，不以 writer 内零 SELECT 作为验收条件。
  共享事务调用者必须在其最早的领取围栏、
  回执或状态写入前准备整个批次，缺少准备时拒绝，不能退回写后历史读取。
  Control 的确认／隔离保留原能力判定；revision 核验、savepoint、审计和回执仍共用
  同一事务，证据准备早于本次首写，不拆分提交。旧管理入口的新增／修改／删除同样
  将记忆变更和管理审计作为一个纯数据库单元准备与提交。
  Evidence compaction 也使用同连接的 SQLite WAL 显式读快照：删除集合、保留证据聚合
  和 Dream provenance 回写资料均在首个 DELETE 前准备。若任意并发提交使快照过期，
  写入升级失败并整体回滚，最多重新准备三次；不重新领取 item、不更换 operation ID。
  DELETE 后只应用已准备的聚合与 provenance，不扫描证据历史。反思结果回填最多读取
  200 个 receipt，准备时过滤缺失或非唯一 run 映射；无剩余项只读返回。留下项在短
  writer 中复核身份及原唯一映射，单次批量插入；歧义来源不推断归属。
    候选的已处理过滤在 LIMIT 前完成，避免不可缩减前缀阻塞后续 fact。
    `0093` 的 `(fact_id,evidence_before,status)` 完整非唯一索引支持精确终态回执查找；
    原 `(run_id,fact_id)` 唯一约束、状态和删除级联保持，证据聚合与来源排序仍读完整输入。
    没有候选且没有原 running run 的空轮询只读返回，不创建空 run；既存 run 继续按原
    ID 恢复和结束，没有 processing item 时不执行空的恢复 UPDATE。
- 不用 /ai new、清空事实或重建 embedding 掩盖队列/召回问题。
- 0051 仅增加 recall 观测列；不改事实、证据、身份、正文或路由。
- 未来 WebUI 复用 Control Plane，不直接查询 ORM；读取、content、mutation、destructive
  能力边界继续分离，secret 永不返回。
- [第三方事实写入](memory-v2-third-party-facts.md)、
  [质量架构](memory-v2-quality.md)仍是对应领域合同。
  phase/roadmap/旧任务书仅供历史参考，不覆盖本页。

## 后台归因与关系评估

关系评估和记忆归因都使用 BEST_EFFORT_BACKGROUND，在共享 Executor admission 中排队，
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

旧 Planner 的工具组筛选已删除，主 Agent 工具声明保持固定。Issue #22 的旧 Planner
验收不适用于当前入口；correct 的强化系数固定为零。隐式语义召回需另在排除近期
历史提示的样本中检验，不把本次调度修复当作语义质量已经全部达标。
