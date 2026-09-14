# Memory 当前架构

本文是 canonical 3.8.1 的现行 Memory 合同（schema 0051）。实施与验收见
[Yuki Memory P1 治理任务书](Yuki-Memory-P1治理任务书.md)。

## 所有权与记忆层次

一个数据库是一个永久 Yuki。Person、Space 使用 canonical UUID；QQ 号仅通过 Binding
解析，Presence/Provider 是传输身份，不划分记忆所有权。更换 NapCat、SnowLuma 或 Yuki 账号
不重建记忆，也不改变 Conversation、Route 或 Rollup generation。

| 分类 | 所有者与含义 |
|---|---|
| Person | 人物的结构化事实、持续偏好及有意义经历 |
| PersonGroup | 某人物在某 canonical Space 的事实与第三方报告 |
| Group | canonical Space 的共同事实与经历 |
| SELF | Yuki 的动态自我事实、偏好、经历；global/current-private/current-group 可见性 |

History 是不可变事件账本；Rollup 是短期上下文压缩，不等于长期事实。
事实的版本、证据、来源、可信度、争议和 canonical 所有权保留在长期 Memory。
external_event 保持 external_untrusted，不伪装成人类聊天，不自动进入人物记忆或关系。
Plugin API 2.0 的受控读取也不产生普通用户社会关系授权。

## 自动提取与价值

普通 Memory Job 按 canonical 所有者聚合，数据库是批次就绪的唯一真源：
30 秒轮询；累计 12 条、8,000 字符或最老事件等待 3,600 秒之一满足即可领取；
单批最多 12 条、8,000 字符，提取输出预算 4,096 tokens。
不跨 Person/Space 拼批，不因 Presence 改变拆队列。一小时是到期领取条件，不是积压或宕机
情况下的完成承诺，也不是 Rollup、反思、lease 或重试间隔。

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

自动预取、人物/群工具的列表与搜索、fact detail 复用相同政策；旧的“只凭当前群 evidence
投影 Person”授权路径已经删除。evidence 仍走本人/显式管理授权边界。
无真实用户主体的 Plugin、Automation、System 使用既有受控目标；不能伪造 actor 自行扩权。
Control Plane 仍要求 capability。

## 检索与使用

[检索合同](memory-v2-retrieval.md)定义 Query Plane、结构化 intent、检索与排序。
自动预取只以当前人物、当前群、真实 mention/reply 等现有目标为起点，默认最多四条，可以零条；
“有权查”不等于每轮扫描历史群和全部群友。当前主题优先，剩余位置最多一条相关人物背景，
没有主题不靠背景凑数。门槛必须绑定已校准 embedding profile，未校准或故障只接受精确匹配。
本轮不增加冷却或最低配额；旧 P1“不新增相关性门槛”的阶段约束由强相关召回任务书取代。
更广范围主要通过完整 Main Agent 的主动查询意图进入；不增加识别/裁判 Agent 或独立短上下文。

候选、实际注入、完成使用评估是三件不同的事。
零注入轮有 receipt，但不调用 attribution；旧 used=false 是未知而非确认未使用。
只有成功评估的 item 才进入使用率分母，失败、跳过、取消和重启中断单独统计。
Plugin/Admin 纯查询不产生强化或使用回执。详见
[指标口径](memory-v2-quality-metrics.md)、[质量运维](../operations/memory-quality.md)。

## 维护与变更边界

- 不用 /ai new、清空事实或重建 embedding 掩盖队列/召回问题。
- 0051 仅增加 recall 观测列；不改事实、证据、身份、正文或路由。
- 未来 WebUI 复用 Control Plane，不直接查询 ORM；读取、content、mutation、destructive
  能力边界继续分离，secret 永不返回。
- [第三方事实写入](memory-v2-third-party-facts.md)、
  [质量架构](memory-v2-quality.md)仍是对应领域合同。
  phase/roadmap/旧任务书仅供历史参考，不覆盖本页。
