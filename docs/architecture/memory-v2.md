# Memory 当前架构

本文是 canonical 架构下的现行 Memory 合同；版本基线见 [README](../../README.md)。
日期任务书与交付回执记录各自当时的实施和验收，不覆盖本页现行合同。

## 所有权与记忆层次

一个数据库是一个永久 Yuki。Person、Space 使用内部 ID；接入层通过明确平台的 Binding
解析账号和群，已知主体沿调用链直接传递内部 ID。Presence/Provider 是传输身份，不划分记忆所有权。
同一 Person 的事实由其获准入口读取，Conversation 历史仍按各自实际入口隔离；
同一 Space 的多个 topic 也不合并 Conversation。更换网关实现或 Yuki 账号
不重建记忆或 Conversation 所有权，也不因账号切换重置聊天 generation；
Route 有独立的 `route_generation`，有效发送路由迁移、暂停或恢复可推进它。

事实、原事件和历史成员关系保留各自原 canonical 所有者。账号换人只影响新发生的事情，
历史平台选择器按原平台与事件所属时段解析；现代 canonical 作者/提及原件优先，包括明确的空提及。
同一人合并后，当前事实主体、private SELF 可见性、本人权限与共同群关系使用 Person 归并集合，
包括没有 Binding 的来源人物；SQL 直接查同一份 Person 事实，不复制事实或维护授权缓存。
原 subject 与 visibility 是两个独立角色，分别归并，不用证据作者替代第三方事实目标。
App/TG 主体不需要 QQ 投影才能查询、反思、重建或写入获准事实。

SELF 主入口、Work 和保留的子任务沿实际发起来源解析分区：主动轮用原 initiative，定时任务用原 occurrence、版本和 execution。原主动轮结束、任务后来被编辑不单独阻止已接纳任务继续读取。仍核对真实 run 归属、当前 generation、Space、Presence 与 Binding，执行授权由原 Work 租约和领域入口核验；不重开祖先或把目标 Person 当作私聊授权。

终态 Work 清理明细时，已有 Memory evidence 实际引用的执行保留原来源身份，复用现有归档记录；不因工作结束删除永久事实的出处，也不保留整份公共运行资料。

| 分类 | 所有者与含义 |
|---|---|
| Person | 人物的结构化事实、持续偏好及有意义经历 |
| PersonGroup | 某人物在某 canonical Space 的事实与第三方报告 |
| Group | canonical Space 的共同事实与经历 |
| SELF | Yuki 的动态自我事实、偏好、经历；global/current-private/current-group 可见性 |

History 是不可变事件账本；公共 compact 检查点压缩当前上下文，不等于长期事实。
事实的版本、证据、来源、可信度、争议和 canonical 所有权保留在长期 Memory。
external_event 保持原事件类型，不伪装成人类聊天，不自动进入人物记忆。
插件的受控读取也不产生普通用户社会关系授权。

## 提取与事实写入

Worker、Main Agent、自省和 Dream 的新事实复用 [变更合同](memory-change.md)。提取保留真实主体、
实际来源引用、原模型 confidence 和原 evidence；不设 retention/importance 最低价值门槛或待确认
候选评分队列。无事实可记录时允许正常空输出。

事实正文使用真实身份或名片。历史引用继续匹配原实际来源，不用当前展示名改写旧 quote。
同 owner/key 可以创建独立 active 事实；纠正和撤回按实际 fact ID 执行。0102 只删除四个单 active
唯一索引，保留已部署 0101 的全部事实与历史回执。
自动提取与 Rebuild 只生成 Person、PersonGroup、Group 新事实，不声明 SELF、确认、纠正或撤回操作。
SELF 的新资料由当前自省或 Main 的明确变更入口创建；普通提取不再暂存 SELF 候选或等待自省裁决。
纠正和撤回仍由实际 fact ID 的完整变更入口执行。

提取、自省和 Dream 分别绑定 `memory_extraction`、`memory_self_reflection`、`memory_dream`
任务，可各自选择 Profile，也可共享配置；Rebuild 复用提取，Embedding 独立配置。结构化
输出沿 Profile 显式模式，不按 Provider 或 Memory 任务强制 JSON Schema。
自省生成结构与实际来源预算见 [自省](self-reflection.md)，Dream 运行见 [运维](../operations/memory-quality.md)。
自省只消费本批新事件、工具回执、核心人格与必要衔接上下文，不回灌正式旧事实或旧 Episode。
Dream 对已有正式记忆做去重、合并与重组，不接管历史 pending candidate，也不负责恢复失效事实。

增量规划跳过签名完全未变化的分区；有变化时仍保留该分区的未变化邻居。每个候选向量只归一化一次，
相似度使用归一化向量点积；全量与增量的剩余聚类计算都在工作线程完成，避免阻塞共享事件循环。
精确两两比较仍有二次计算量，不以事实数量上限截断规划。
自省所有结果都可保存完整真实引用，
不强制把同一来源复制成 episode 和 proposal。SELF kind/category/key 不设固定标签白名单；
私聊资料向 global 传播继续核验真实可见范围。

## 历史共同群读取

所有普通用户结构化读取使用后端 `MemoryReadScopeResolver`。设请求者 R，目标人物 P，
G(X) 为数据库记录的 X 整个 Person 归并集合的历史 canonical membership（同 Space 去重）：

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
模型使用后端提供的内部主体引用；名称只是查找线索，不承担授权证明。

`search_memory` 和 fact detail 复用相同政策；旧的“只凭当前群 evidence
投影 Person”授权路径已经删除。evidence 仍走本人/显式管理授权边界。
无真实用户主体的 Plugin、Automation、System 使用既有受控目标；不能伪造 actor 自行扩权。
Control Plane 仍要求 capability。

## 检索与使用

[检索合同](memory-v2-retrieval.md)定义 Query Plane、主动查询参数、检索与排序。
主 Agent 不再每轮自动注入长期事实；需要过去事实时由模型调用统一
`search_memory`。无显式目标时，后端以本次真实主体的 canonical 身份和历史成员关系，
在数据库层筛选全部获准 Person、PersonGroup、Group 与当前可见 SELF；历史 owner 即使没有
活跃平台 Binding 仍可检索。词法和向量候选在授权范围内全局排序，不按 owner 截断。
全局候选或向量扫描触及工作预算时回执标记 `truncated=true`、`exhaustive=false` 和原因；
实际返回条数上限也另行标记。真实中文检索集校准和上线验收仍在
[search_memory 任务书](Yuki-search-memory-taskbook-2026-09-29.md)中跟踪。

新工具调用重新查询当前事实与 SQL 授权，不跨调用复用完整检索结果或 fact detail。
相同原调用的持久回执保留恢复语义；新的 call ID 即使参数相同也要重新执行读取。
纯 query embedding cache 只按 Profile 指纹和查询文本复用向量，后续事实、成员关系与证据权限仍按本次查询核验。
Embedding 请求、缓存命中、错误和已知 usage 分别记录，缺失的 Provider Token 字段保持未知，
不能由缓存计数推算模型前缀命中率。

## 维护与变更边界

维护只根据事实自身 valid_until 处理到期失效，不按年龄、固定置信度权重或 scope 容量淘汰历史。
模型审计、重复合并治理队列、归因与强化运行链已退休；管理权限和真实 SQL 来源审计保留。

- 证据明细及 readable evidence count 共用同一 SQL 来源谓词，按 canonical event、普通
  tool receipt 或无事件 SELF 执行的原回执核验来源、owner、隐藏状态及 SELF 可见范围。
  明细、证据新增与既有 SELF 运行的 Memory 依赖核验共用同一 SQL 来源条件，不逐 evidence 查来源。
  `read_self_initiative_facts` 只接受明确 fact ID，核验 active、verified、有效时间、当前群或可见 SELF 范围，
  并要求至少一条当前可读证据；不以最新 32 条证据或 32 个 fact ID 判断可读性。
  后台 Memory 种子扫描、分页游标和无 ID 候选枚举已删除，主 Agent 的按需查询、自省、Dream 与真实 SELF 来源解析保留。
  内部证据聚合读取全部可读来源，不以固定条数截断历史尾部；原始 confidence/authority
  和来源资格保留。无消费者的完整 lineage 装配入口已删除，原 operation/result/source 账本保留。
  Memory mutation 与 Dream 先用 `Database.read_session()` 的显式只读 BEGIN
  准备实际证据、复制目标和两跳关系，再关闭 reader，进入统一 FIFO 的 `immediate_session()`。
  writer 在取得连接前排队，不把 reader 升级为 writer，也不再由 Memory 自己重试 BUSY。
  当前事务重新核验原证据 ID/字段、来源资格、复制目标与关系成员，不能只比较条数或 fact.updated_at。
  不相关的数据库提交不会使准备自动失效；实际来源、选择或权限改变仍拒绝原变更。
  同一归并集合用于当前人物权限，原 raw owner、来源和指纹保持原件。
  事实、证据、状态事件与原 mutation/operation/request 回执一次提交；未知提交先读原回执，
  不重跑模型或外部效果，取消继续传播，embedding 调度在提交后。
  偏好按键删除、到期维护只依赖当前小列与关系 SQL，直接在短 writer 处理；不准备证据正文。
  到期候选写时仍须处于 active/contested 且已到期，不能覆盖发现后形成的 superseded 状态。
  Dream 操作失败先核对原 cluster 的 committed operation 或 applied preview 回执，
  空 actions 同样按已应用的原结果恢复，保留实际提交计数和累计预算。
  已保存的 ReflectionCheckpoint 与匹配来源指纹的 Dream ready preview 可继续纯数据库收尾；
  模型额度只约束新的实际 Provider 请求，不阻止原已付结果提交。没有保存的输出不能凭 usage
  重建；未决提交不授予重放资格。
  原来源隐藏、擦除或当前授权、执行 generation 实际失效时拒绝旧计划，不能写入一个无证据事实。
  账号换绑本身不改写原 canonical owner，也不单独增加旧计划的拒绝条件。
  Dream 将实际模型输入、选中证据身份与内容及 canonical 分区纳入输入指纹，持久 preview
  复用同一指纹；读阶段核模型输入，writer 复核对应实际来源，证据数量不变不能证明原来源有效。
  正常运行的新模型输出也先写入现有 ready preview，再按原输入提交；准备或写入失败后复用
  该原件，不重新购买模型结果。应用精确对应原 preview 身份，与事实变更一起标为 applied。
  临时工具回执清理保留现存 ReflectionCheckpoint 的 tool_map 实际引用，未写完的普通自省
  不会因回执到期丢掉已付输出的来源；解除引用后按原到期规则清理。
  准备材料仅供本次领域提交消费，提交或回滚后释放，不增加持久 revision 或事实源。
  后续写入只累计实际新增证据；版本复制同时批量核验原来源与新目标资格，批量插入后
  更新临时计数，写入期间不重新扫描历史。必要的主键、状态、来源／owner 最终
  复核及两跳关系短查询保留，不以 writer 内零 SELECT 作为验收条件。
  共享事务调用者在 reader 准备完整批次，通过 `prepare_write` / `use_prepared_write`
  将领域证据交给同一个 writer；供给的 session 一直复用，writer 不另开 reader 扫描历史。
  Control 的确认／隔离保留原能力判定；revision 核验、savepoint、审计和回执仍共用
  同一事务，证据准备早于本次首写，不拆分提交。旧管理入口和偏好入口通过 Memory
  mutation 提交事实与原变更回执，完成后不另写一笔管理审计；未装配 mutation 的旧管理
  fallback 仍把事实变更与管理审计放在同一个数据库事务中。
  新自省结果以实际 run ID 原子登记，不从旧 delegation_mode 字符串反推归属。
- 永久证据不按样本数压缩删除。Evidence compaction 的新任务、装配和配置已退出运行链；
  原表、run/item 和删除回执仅保留真实历史诊断。Dream 的模型输入可按请求预算选择证据摘录，
  合并或重组事实时仍复制全部当前获准原证据。显式隐私删除与未引用临时回执清理按各自权限和引用规则处理。
- 不用 /ai new、清空事实或重建 embedding 掩盖队列/召回问题。
- 管理入口按真实消费者调用领域服务；元数据、正文与修改分别授权，secret 不返回。
- [第三方事实写入](memory-v2-third-party-facts.md)、
  [质量架构](memory-v2-quality.md)仍是对应领域合同。
  历史验收记录不覆盖本页。

Memory mutation 的来源与候选可并行准备，不使用进程全局锁跨外部查证串行所有变更。
提交使用主库统一 writer、当前来源/owner 复核与原 mutation receipt；重查必要身份关系复用当前 session，embedding 在提交后。

Hygiene 因缺少有效来源失效的 fact 使用 `invalid_provenance`，与状态事件一致。
原历史原因仍可读，不回写旧事件。

Rebuild 提案保存经原 Claim 校验的 canonical Person/Space 目标，第三方目标不等于来源作者。人物遗忘的默认和显式装配都在同一 WAL 读快照准备完整人物族、原账号时段、实际相关源和 selection，
再进入短 writer 核对实际相关 selection 集合、原 JSON 与来源后清退；包含新匹配行的复核，不以全库隐私 token 或无关 run 变化拒绝写入。
每次抽取的 proposals、item 与 usage 一次暂存，失败收尾只处理原 extracting 状态，不能把已暂存结果
改回 pending。最终 receipt sweep 在 writer 核原正文、ASR、segments 等实际输入字段，不能仅凭
source hash、数量或会话身份推进完成。
删除覆盖无 Binding 的归并人物，跨平台同号或账号后来的其他真人不扩大删除范围。
Sandbox 的原私有任务和 continuation 随实际来源清退；其他作者的引用断开但正文保留。
退役的 SELF 候选保留历史读取；显式遗忘按实际事件和证据引用清退相关候选正文，
不会只删除 evidence 留下父候选，也不按旧外部数字标签扩大到其他平台同号人物。
群自省检查点中的实际被擦来源及旧事实/候选副本同样清退：首写前按内部来源准备 run ID，
短 writer 只清其 checkpoint JSON，保原请求、运行状态、累计预算及已提交结果，不清同群无关原件。
主动任务的实际被擦除来源及其敏感回执清退；只有 target_person_id 命中时置空目标，保留其他群 SELF 原经历、证据与反馈。迁移 `0127` 仅凭原内部作者、提及目标、同会话引用或已提交 fact 回填旧目标，不用今日账号绑定猜测旧提案归属。

Dream 新预览指纹使用实际模型输入、canonical fact 与原证据。旧已付预览的传输标签只通过原主体历史绑定重建旧编码，并精确匹配完整指纹；真实证据变化仍使原预览失效。Rebuild 已提交提案的数据库收尾核原来源和回执，不要求今日外部 Binding 继续属于原人物；两者均不重置预算或重复购买模型结果。

当前检索结果按归并后的人物组织展示与排序，原 fact/evidence 对象及 ID 保留。名称只是当前查找与展示资料，不参与原已付模型输入的证明。自省仍按原 raw Person/Space 的源水位独立扫描和恢复，Main、工具和 Dream 的当前可见 Memory 读取使用归并语义；不拼接已执行 batch、游标或回执。
