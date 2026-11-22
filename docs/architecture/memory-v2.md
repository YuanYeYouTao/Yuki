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
提取合同为 `memory-v2-extraction-v8` / schema 8（live 批量与 Rebuild 共用，直接生效、无开关）。指令写明：只为当前这件事给出、
指向某个 PR、issue、分支、文件、任务书、当次交付物或当次会话的要求、决定或安排是当次任务指令，不提取，
它们留在会话历史与任务记录里；以后都适用或一般做事方式才作为长期偏好；同一批同一个意思只输出一条。
这是模型按内容判断，不是后端门槛，没有按字段丢弃 claim 的逻辑。跨批的同义条目交给 Dream 现有 merge。
提取输出不带 `category`（模型多给的忽略），写入取 kind 的值；存量事实的 `category` 不改。

事实正文使用真实身份或名片。历史引用继续匹配原实际来源，不用当前展示名改写旧 quote。
提取输入带 `bot_name`（当前角色名），Yuki 的原生提及按 `@bot_name` 渲染，她自己的消息署名
也用角色名，不用群名片或兜底的 “Agent”。新 quote 可以摘自入库原文，也可以摘自这份渲染文：
两者都属于同一事件；没有原生自提及的普通文字不会凭空多出 `@bot_name`。
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

增量规划每次运行至多 `memory_dream_max_clusters_per_run` 个簇（默认 24，与调用预算相同）；
active / 争议且没有 checkpoint 或签名已变的事实数由 `DreamHealth.unreviewed_facts` 报告。
增量规划跳过签名完全未变化的分区；有变化时仍保留该分区的未变化邻居。每个候选向量只归一化一次，
相似度使用归一化向量点积；全量与增量的剩余聚类计算都在工作线程完成，避免阻塞共享事件循环。
精确两两比较仍有二次计算量，不以事实数量上限截断规划。
每个 pair 只算一次原 fsum 分数，仅保存达到原阈值的双向精确权重边。seed、分数 tie、
complete-link 与原顺序不变；最大 degree 已为零时一次输出剩余的原 singleton 分组，
不再逐 seed 重扫无边尾段。稠密图内存仍可能二次，不将这项优化称为 ANN。
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
本会话不可见的 evidence、其他人的 private SELF，也不是第三方写入授权。

成员关系不依赖实时网关群列表、群 enabled、路由 paused、Provider 在线或当前 Presence。
退群但历史记录仍在时关系仍有效；不做朋友的朋友等传递授权。forget 删除关系后下一次查询
依据剩余数据库记录重新判断，不缓存永久许可。当前在群 G，也可查历史共同群 H。
模型使用后端提供的内部主体引用；名称只是查找线索，不承担授权证明。
这条取舍只适用于按需读取。常驻记忆段与经历目录不带私聊来源的他人事实，群聊里只带证据全部来自本群的事实。

`search_memory` 和按 `fact_id` 读取单条事实复用相同政策（原 `read_memory` 已并入；evidence 视图已删除）；旧的“只凭当前群 evidence
投影 Person”授权路径已经删除。按 `fact_id` 读取时结果另附本会话可见的证据原文与隐藏条数，
可见范围按当前会话而非提问人，规则见[按编号读取的证据](memory-v2-retrieval.md#按编号读取的证据)；
它不改变哪些事实可读。其余 evidence 仍走本人/显式管理授权边界。
无真实用户主体的 Plugin、Automation、System 使用既有受控目标；不能伪造 actor 自行扩权。
Control Plane 仍要求 capability。

## 检索与使用

[检索合同](memory-v2-retrieval.md)定义 Query Plane、主动查询参数、检索与排序。
主 Agent 每轮只带[常驻记忆段](#常驻记忆段)里的少量稳定条目与[经历目录](#经历目录)里的经历标题；其余过去事实由模型调用统一
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

## 常驻记忆段

每一轮主 Agent 请求的 system 消息末尾可以带一段确定性渲染的“常驻记忆”（`memory.resident`，
静态贡献，排在 SOUL、记忆依据规则与核心合同之后）。同一会话内它最多随每日挑选变化一次，因此进入
会话的缓存前缀；它不在 `invariant_contributions()` 里，恢复合同哈希不含它；不新增缓存断点。
内容变化时，该会话的历史缓存失效一次。
渲染由 `memory/resident.py` 的 `ResidentMemory` 完成：只用一个只读会话，不调用模型，不写库；
每轮直接渲染，没有运行时开关。子 Agent 与插件轮不带常驻段。

**内容**。会话身份取自 `canonical_conversations` 的 `kind`、`person_id`（取人物根）与 `space_id`，
不按入口类型区分：

| 会话 | 小节（固定顺序） |
|---|---|
| 与 P 的私聊 | `[对方明确要求记住的]`：P 的 person / person_group 明确事实（`source_type` 或 `authority` 为 explicit），按 ID 升序，最多 30 条，超出时加一行“另有 N 条”；`[对方·公共画像]`、`[对方·私聊画像]`（见[人物画像](#人物画像)）；`[我自己的原则]` |
| 群 G | `[本群]`：G 的核心集；`[群成员画像]`：G 成员名单上每人的公共画像前 5 条；`[我自己的原则]` |

群聊不按当前发言人或被 @ 的人挑选内容。事实小节每行是事实正文（连续空白折成一个空格）加 `（fact:ID）`，
可用 `search_memory(fact_id=…)` 读取正文与本会话可见的证据原文；记忆依据规则写明这一点。没有条目的小节不输出；全部为空时整段不输出。
事实小节里同一 fact 只出现一次，先出现的小节保留；画像是概括，不参与这项去重。

**过滤**（每轮重新判定，核心集不需要随事实变化同步）：
- 共同条件：active、`conflict_state=clear`、未隔离、未过期；kind 为 fact 或 preference；
  人物事实的 authority 为 explicit 或 self_report，主人人物（原人物与根人物）enabled。
- 核心集条目必须属于该行的主人：群行只收该群的 group 事实，SELF 行只收 SELF 事实。
- 私聊：任一证据（聊天事件，或工具回执的触发事件）来自别人的私聊即排除；没有证据行的事实允许。
- 群聊：至少有一条证据，且每条证据都直接指向一条聊天事件、该事件所在会话的 `space_id` 是本群。
- SELF 原则：visibility 为 global，私聊里另含可见人物是对方的 private，群聊里另含可见群是本群的 group。
- 列表里的 ID 不再 active 时，沿 `supersedes_id` 指向它的最新事实、否则沿已提交 Dream 操作的输出，
  走到 active 的后继再套用全部条件；没有后继就跳过。

**核心集**存于 `memory_resident_sets`（schema `0162`，`0164` 起只允许群与 SELF）：一行一个主人（群或 SELF），
有序 fact ID 列表最多 10 条、来源 `dream` / `owner`、挑选水位 `considered_through_fact_id`、
列表每变一次加 1 的 `revision`。表里没有记忆正文，是可删除重建的派生数据。写入先在 reader 里校验
条数、主人一致与共同条件，再用一个短写事务提交；SELF 行只由所有者写。
Dream 每日批次在全部簇处理完后对到期主人运行一次挑选步骤（候选为共同条件下、属于该主人且非明确事实的事实），
`source='owner'` 的行跳过；所有者通过 `yuki memory resident` 子命令查看、渲染、改写、锁定与解锁。
设计依据与验收见[记忆分层 P1 实施任务书](Yuki-记忆分层P1实施任务书-2026-11-21.md)。
人物核心集（原 `[关于对方]`）已由人物画像替代：迁移 `0164` 删除 `owner_kind='person'` 的行并收窄 CHECK，
挑选步骤、CLI `--person` 与人物合并时的水位清零一并删除。

## 人物画像

每个人物根至多两份画像（`memory/profiles.py`，schema `0164`）：公共画像与私人画像，各至多 10 条、
每条正文至多 40 字（连续空白折成一个空格后计数），小节可选（身份 / 关系 / 兴趣 / 做事方式 / 相处），
每条带全部依据 fact ID。每份另有至多 4 个**可查话题**（schema `0166`，每个至多 8 字、各带依据 fact ID）：
条目没写到、但依据事实里还记着的具体话题，提示模型遇到相关话题时值得 `search_memory`。画像只引用该人物（按根，含并入的人物）的 **person** 事实，共同条件同常驻段；
person_group 事实与经历不进画像。

**可见条件**（`memory/resident.py`，每轮与每次维护重新判定）：
- 公共：至少一条证据直接指向任一群（不是私聊）的聊天事件，且没有任何证据来自别人的私聊或无法追溯的会话；
  本人私聊证据与群里的工具回执不排除（`public_profile_evidence`）。
- 私人：每条证据都来自本人私聊（直接事件或在那里触发的回执），没有证据行的事实也收；明确事实不进私人画像，
  它们照旧逐条常驻（`private_profile_evidence`）。
- 健康、性、财务类内容两份画像都不写，只靠维护指令与所有者过目，程序不校验。

**存储**。`memory_profiles` 一行一份画像（`(person_id, audience)` 唯一）：当前版本号（0 表示还没有）、
上次成功处理时可用事实集合的签名（升序 ID 用逗号连接后的 SHA-256）、上次尝试时间与所有者锁。
条目只存在 `memory_profile_versions`：每个版本一份全文快照（条目 `entries_json` 与话题 `topics_json`；
`0166` 之前的版本话题为 NULL），来源 `dream` / `owner` / `program` 与原因，只追加不改写。整人遗忘在同一事务里删除被遗忘者及其事实受影响（被删除、或证据来自其回执或被抹除事件）
的人物的整份画像与全部版本；单条事实失效只影响渲染，快照保留；人物并入另一根后，旧 ID 下的画像行留作历史，读写都按根解析，不渲染、不清理。`memory_space_members` 每群至多 5 行
（名额 1–5，人物外键级联），来源 `dream` 或所有者固定的 `owner`。三张表都是派生数据，删除不丢事实。

**渲染**（常驻段内、`[我自己的原则]` 之前）：私聊渲染对方两份画像的全部条目；群渲染存储的名单顺序，
每人一行名字 `· 主名（person:根 ID；同一人另有 person:旧 ID…）`（历史消息信封用事件当时的 Person ID），
再列公共画像前 5 条；没有可显示条目的人不输出。条目行 `- [小节] 正文（fact:a,b）` 只显示前 2 个编号。
渲染时每个编号沿后继解析到 active 事实并套用该画像的可见条件，**任一编号不通过整条不渲染**；
下一次激活起生效，同一激活内已组装的 system 消息不变。当前消息不放画像。
话题按同一规则逐个复核，通过的在该画像条目后合成一行 `- 可查话题：A、B、C`（不显示编号）：私聊里对方两份
画像各一行，群里每位名单成员只有公共画像一行；条目与话题都没有通过的成员不输出。私聊来源的话题因此只进本人
私聊。依据规则写明话题与当前话题相关时可以 `search_memory`。

**维护**（Dream 全部簇处理完、`finalize_run` 之前，与群核心集同一挑选步骤，`memory/profile_selection.py`）：
1. 名单：窗口为上一次已收尾运行（completed / partial_failed）的 `started_at` 至本次 `started_at`，
   没有时取之前 24 小时；一次只读查询按（群，作者人物根）计数窗口内的入站人物消息（keeper 的 message，
   排除 Yuki 与停用人物，合并人物按根合计），每群取前 5、同数按根 ID；窗口内没有消息的群与所有者固定的群不变。
2. 程序清理：每份有条目或话题的画像（含锁定的）按渲染规则校验，失效条目与话题整条删除、编号换成后继，
   写 `program` 版本（原因列出失效 ID），不调模型、不改签名。
3. 到期：公共画像维护名单上的人与窗口内有私聊的人，私人画像只维护后者；可用集合非空、未锁定，且签名变了
   或当前版本还没有话题（`topics_json IS NULL`，`0166` 之前写的版本，只到期这一次）即到期。
   群核心集先按 P1 顺序，再按上次尝试时间（从未尝试的在前）、人物 ID 排画像，同一人的私人画像紧跟其公共画像；
   每次运行至多 `MEMORY_RESIDENT_MAX_SELECTIONS_PER_RUN`（默认 24）个主人，每个至多一次 `memory_dream` 结构化调用。
4. 调用：公共输入只有 `cur_N`（现有条目）、`current_topics`（现有话题）与全部公共可用事实 `f_N`；私人输入另有
   只作参考的 `ref_N`（本人明确事实与本轮之后的公共画像条目）。输出 `entries`（结构化约束：至多 10 条、正文至多
   40 字、小节枚举、facts 至少 1 个）、`topics`（至多 4 个、每个至多 8 字、facts 至少 1 个）与 `reason`。程序再校验
   正文非空无换行、话题不重复、facts 不重复且都是本次 `f_N`、不引用 `ref_N`、reason 非空，并在 reader 里按渲染规则
   复核条目与话题；任一不满足整份作废，只写尝试时间，
   记 `memory_profile_rejected code=…`。提交事务比对调用开始时的版本号与锁，所有者在调用期间改动或锁定时
   放弃写入（`memory_profile_abandoned`），保持到期。条目与当前相同只更新签名与尝试时间。
`DreamHealth.resident_pending_owners` 与 `resident_oldest_pending_at` 同时计入到期画像。

**读写代价**。每轮渲染的画像部分是常数条语句（私聊与群各约 6–9 条，与历史、画像、条目、名单人数无关）：
名单按 `(space_id, position)` 一次取并解析到根，当前条目与话题按 `(person_id, audience)` 与 `(profile_id, version)`
同一条语句一次取，话题与条目在同一批复核里核验，被引编号的后继与事实按主键批量取、证据按 `fact_id` 一次取，名字与并入 ID 一次取；读的行数是
本轮条目与编号数。名单计数按 `ix_chat_events_scope_time (scope_type, occurred_at)` 只读窗口范围；
到期判定一次取全部被维护人的 person 事实（`ix_memory_facts_scope_status_updated`）与证据；程序清理对全部
画像一次批量校验。写事务只按主键 / 唯一索引写画像行与版本行；整人遗忘按 `person_id` 删除画像，不读版本 JSON。
规模实测与语句计划见 `tests/integration/test_memory_profiles_scale_e2e.py`（`YUKI_PROFILE_SCALE_E2E=1`）。
设计依据见[记忆分层 P4 人物画像任务书](Yuki-记忆分层P4人物画像任务书-2026-11-22.md)；可查话题见
[记忆意图召回评估](Yuki-记忆意图召回评估-2026-11-22.md)。

## 经历目录

常驻段之后、system 消息的最后一段可以带“经历目录”（`memory.episode_index`，静态贡献，priority 5），
当前消息里另有“今天新记下的经历”（`memory.episodes_recent`，TURN 贡献，priority 84，紧跟当前场景资料）。
两者都不在 `invariant_contributions()` 里，恢复合同哈希不含它们；不新增 system 消息或缓存断点。
渲染由 `memory/episode_index.py` 的 `EpisodeIndex` 完成：一个只读会话，不调用模型，不写库；每轮直接渲染，
没有运行时开关。子 Agent 与插件轮不带；渲染失败只在 `ContextAssembler` 一处接住，这一轮少这两段，记 `memory_episode_index_render_failed`。
目录只有标题，正文按 `fact:ID` 用 `search_memory(fact_id=…)` 主动读取；不按相似度挑选，不注入正文。

**标题**存于 `memory_facts.title`（schema `0163`，只有 episode 使用）。由写这条经历的那次模型调用给出：
自省 `episodes[].title` 必填；提取 claim 的 `title` 在 kind=episode 时保存，其他 kind 丢弃；Dream recompose
的 episode 输出取该输出的 `focus`，synthesize 沿用锚点标题；Main `save_memory` 不带标题。空标题按无标题存 NULL，
不会让整次输出校验失败（缺标题不是准入条件）。
标题不进 FTS、embedding 文档或 Dream 的事实签名。

**可见范围**与常驻段同一套证据规则（`memory/resident.py` 的 `evidence_origins`、`private_evidence_visible`、
`space_evidence_visible`）；长期印象的候选、比较输入与渲染三处都先过这套判定：
- 共同条件：kind=episode、active、`conflict_state=clear`、未隔离、未过期、authority 不是 third_party，
  person／person_group 经历的主人人物（原人物与根）enabled。
- 与 P 的私聊：P（按根）的 person／person_group 经历与 visibility 为 private、可见人物根为 P 的 SELF 经历；
  任一证据来自别人的私聊或无法追溯的会话即排除，没有证据行的允许。
- 群 G：G 的 group 经历、主题群为 G 的 person_group 经历、visibility 为 group 且可见群为 G 的 SELF 经历；
  至少一条证据，且每条都直接指向 G 的聊天事件（工具回执证据不算）。群里不列 person 范围的经历。

**存储**：`memory_episode_indexes`（schema `0165`）每个会话受众一行（私聊按对方人物根，群按 space；两条部分唯一索引），
存“最近”列表（按排序键升序）、至多 10 条长期印象（`fact_id`、进入时间 `since`、来源 `dream`／`owner`）、
按月计数、`through_fact_id`（这一行整理到的最大 fact ID）、`locked` 与每次写入加一的 `revision`。
行是派生数据，只存 fact ID；删掉整张表不丢事实，下一次 Dream 收尾或 `generate` 可重建。
私聊行随人物遗忘级联删除；人物合并后按根取行，被并入者的旧行不再读到，其早于 `through` 的经历只计入月份。

**格式**：日期取 `coalesce(valid_from, created_at)` 的部署时区（`memory_dream_timezone`）日期，排序键为
`(日期, fact_id)`。目录首行写“截至 MM-DD 的每日整理”（行的 `as_of`），其后三节：`[长期印象]`（按排序键）、
`[最近的经历]`（“最近”列表的最后 15 条，按排序键）、`[其余]`（行里存的按 `YYYY-MM` 计数，即本受众可见、
不在前两节的经历）；某节为空不输出，三节都空时没有目录。与 `as_of` 同年的日期显示 `MM-DD`，否则 `YYYY-MM-DD`。
同一条经历只出现一次，长期印象优先。今天新记下的经历是 `fact_id > through` 且目录没列的可见经历，列最后 20 条，
超出时第二行写“另有 N 条”。没有行的受众目录为空，今天新记下的经历从最近一次已收尾 Dream 运行的
`snapshot_max_fact_id` 之后算。标题折叠连续空白、不截断，为空时显示“（未命名经历）”。

**每轮渲染**的语句数是常数：受众（私聊再取人物根）、本受众那一行、一条
`id IN (已列 ID) OR id > through` 的主键查询（资格条件作为列取回，在 Python 里判断，否则规划器改走 status 索引）、
一条证据查询；已列 ID 不再 active 时才沿后继查（`ix_memory_facts_supersedes_id`）。读的行数只与已列 ID 和
上次整理以来写入的事实有关，与历史长度无关。渲染时重新校验每一条：已列经历失效、被取代、证据变化或主人停用，
下一次激活起不显示；长期印象被改写或合并时显示后继，“最近”里的条目只由它自身的改写接替。

**Dream 收尾**（`memory/episode_selection.py`，在 `ResidentSelection.run` 里群核心集之后、画像之前，三者共用
`memory_resident_max_selections_per_run`）：
- 只读准备：全部行、最大 fact ID、`id > min(through) AND kind='episode'` 的主键范围（判断新经历：沿 `supersedes_id`
  回到根，根大于这一行 `through` 才是新经历，改写出来的新版本不是）、全部 active 经历及其证据（两条语句，
  按受众分组判定可见并重算月份）。语句数与受众数、离开者数无关。
- 每个受众：`S` = “最近”列表（后继按根处理：自身改写接替原位；并入或与别条合成的直接删除；证据不再合本受众、
  过期或没有 active 后继的删除）∪ 新经历，两者都去掉已在长期印象里的 ID，按排序键排；最后 15 条留下，其余是离开者。
  争议、隔离与主人停用是可恢复的隐藏：这样的条目（长期印象与“最近”里的，以及此时写入的新经历）留在行里原位，
  不渲染、不计入月份、不进候选池；隐藏的离开者留在列表前部，恢复可见后再比较一次；隐藏的长期印象原样保留。
- 有可进池的离开者且未锁定：一次 `memory_dream` 结构化调用（温度 0.1）。候选池 `c_N` = 现有可见长期印象 ∪ 可见离开者，
  按排序键编号，每条只有 `ref`、`title`、`content`、`date`，不标新旧、不给 importance、进入时间或任何次数；
  超过 `memory_dream_max_input_characters` 时离开者只给放得下的前缀（至少一条），其余留在列表前部、不渲染、计入月份，
  下次再比。输出 `selected`（至多 10 个别名）与 `reason`；别名未知、重复、超过 10 个或 reason 为空即整次作废，
  这一行完全不动（记 `memory_episode_memorable_rejected`）。池里没选上的都归档，以后不再进任何比较输入；
  新选上的 `since` 为本次，留任的不变。空位不从归档回填。
- 锁定的受众不调模型，离开者直接归档；没有离开者的不调模型。需要调用而没轮到的受众这一轮完全不变。
- 写入：每个受众一个短写事务，按主人取行、比对读到的 `revision` 与 `locked`（所有者在调用期间改过即放弃），
  写入两个列表、月份计数、`through`（本次读到的最大 fact ID）、`as_of`、`revision+1`；内容没变的行只把 `through`
  推进（按主键的 UPDATE，不加 `revision`，目录字节不变）。事务内不等模型、不查证据、不扫描历史。
- 被想起不等于更重要：聊天提到、`search_memory` 读到都不写任何东西；归档的经历不会回到窗口；Dream 把重提
  合并回原条时原条 ID 不变（merge、synthesize、recompose 的锚点不由模型选：来源里有显式记忆时取它，否则取 ID 最小的一条），合并出的新版本按根判断不算新经历。
  Dream 指令写明“后来重提、回忆或复述一件已有记忆的经历……merge 到原条”，比较指令写明重提不因此更值得记得。

所有者通过 `yuki memory episodes` 子命令渲染、检查、查看／改写／锁定长期印象、生成与写入首版、回填与改写标题、
查看状态（见[Memory 质量运维](../operations/memory-quality.md)）。设计依据与验收见
[记忆分层 P2 经历目录任务书](Yuki-记忆分层P2经历目录任务书-2026-11-22.md)与
[P2.1 经历目录精简任务书](Yuki-记忆分层P2.1经历目录精简任务书-2026-11-23.md)。

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
  mutation 提交事实与原变更回执，完成后不另写一笔管理审计。MemoryAdmin 构造必须提供
  mutation 与原 ledger，不再保留第二套事实写入模式；不存在的事实沿原接口返回 None/False。
  新版本先登记本次已验证证据，再复制其他原来源；同一事件的多次独立修改各认原调用 ID，
  不用旧版本的 evidence confidence/关系覆盖新版本的真实修改。原事实与原证据保持原件。
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
