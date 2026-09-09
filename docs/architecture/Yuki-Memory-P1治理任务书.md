# Yuki Memory P1 治理任务书

状态：实现与本地验收完成，待 PR / 上线。基线 main/624fbd4，版本 3.8.1，schema
0051；工作分支 `codex/memory-p1-governance`。本文件是本轮验收合同；历史施工文档
不能覆盖它。

## 目标和边界

修复普通长期提取的小批次成本、自动写入价值过低、读取入口权限不一致及召回统计
失真。第三项不包含后台冷却、新的相关性阈值或抑制策略，不以降低注入数量验收。

- 保留精选事实、持续偏好、有意义的一次性共同经历；不要求所有经历重复发生。
- 普通自动提取最长等待一小时；明确记住、纠正、删除继续即时执行。
- 保留 Person / PersonGroup / Group / SELF 分类，按历史共同群授权结构化读取。
- 不改系统人格、不加披露人格指令，不新增意图 Agent、相关性 Agent 或短上下文。
- 真实模型验证最多 24 次请求（含重试），只用合成数据，不写生产记忆。
- 实现阶段只做本地提交；完成验收后可按后续明确授权推送开发分支。未经独立授权不部署、
  发布或涨版本。不改 `.env`、用户人格、生产数据和现有未跟踪文件。不清记忆、不
  `/ai new`、不重建 embedding，不改任何会话、路由、Rollup generation。

## 实现合同

### A. 一小时聚合

沿用 canonical 所有者分组的可靠 Memory Job 队列，不建第二套队列。

| 配置 | 默认值 |
| --- | ---: |
| 轮询 | 30 秒 |
| 数量触发 / 单批上限 | 12 / 12 |
| 字符触发 / 单批预算 | 8000 |
| 最老 pending 等待 | 3600 秒 |
| 提取输出预算 | 4096 tokens |

数量、字符、等待年龄满足任一条件即可领取。一小时是正常运行时的到期领取条件，
不是积压或宕机下的完成承诺。不能跨 Person/Space 拼批，换 Presence 不拆队列。
数据库是就绪真源；修复超时小批次完成后内存唤醒计数残留。轮询没有就绪任务不调用
模型。processing lease 和失败重试间隔不随等待窗口延长。no_claims 正常完成并推进
进度，不为了产出再试。保留幂等、部分失败恢复和历史重放保护。健康报告不得把未到期
且无错误的 pending 判成阻塞。一小时不适用于即时 mutation、Rollup 或 reflection。

### B. 自动价值门槛

复用 retention、source_style、importance、confidence、主体与证据校验，不使用中文
正则判断价值，不新建语义分类系统。提取输出必须明确填写这些判断字段并提供简短
value_reason；移除乐观缺省值，更新 Prompt/Schema 版本，不使用条件 JSON Schema。
value_reason 不成为记忆正文、不写入日志。

自动首次写入需：证据和主体正确；retention 为 durable/meaningful_episode；
importance >= 3；通过现有可信度要求和来源限制。1–2 是琐碎临时内容，3 是影响未来
理解/选择/回忆的内容，4–5 是显著影响/承诺/里程碑。一件有意义的单次经历可以是 3。
低价值直接正常跳过，不建低价值候选队列；有价值但低可信继续既有候选机制。

Worker/Reflection 的来源由后端固定，模型自报 explicit 不得绕过门槛或获取显式权威。
真实用户轮 Main Agent 的 USER_REQUESTED 保持即时，不用文本正则判定。读取扩权不
改变写入权限。self-reflection 新 fact/preference/episode 和 Agent 主动新增复用价值
标准；proposal reason 可复用，episode 补价值理由；无产出 noop 仍推进水位。SELF
visibility、受保护键和 evidence alias 保持。纠正/撤回/删除/补证/合并不受首次价值
门槛阻拦。Dream 已有事实的重组仍是维护，不借维护产生未经证据支持的新事实。不批量
复审/删旧数据，不在读时套写入门槛。模型负责语义判断，后端校验不等于语义保证。

### C. 历史共同群读取

后端统一 Memory Read Scope Resolver 产生 ResolvedReadScope。模型只能表达查询，
不能声明权限。真实请求者 R、目标 P、后端历史 canonical 群集合 G(X) 的规则：

| 对象 | 条件 |
| --- | --- |
| 本人 Person | 保持本人能力 |
| 他人 Person | G(R) 与 G(P) 有交集 |
| Group H | H 属于 G(R) |
| PersonGroup(P,H) | H 属于 G(R) 与 G(P) 的交集 |
| SELF | 既有 global/current-private/current-group |

历史 membership 是证据，不用网关实时交集，不因退群、关闭群、暂停路由、离线或换号
撤销；无传递授权。forget 后重新判断，不能缓存永久授权。当前群 G 中可查询共同群 H。
**允许读取有共同群目标的完整 Person 结构事实，包括私聊来源；在群中查询可能向其他
成员披露这些事实。这是已确认的产品取舍。**不开放原始私聊、完整 evidence、他人的
private SELF。Plugin/Automation/System 不能冒充用户；控制面 capability 保留。
自动召回、搜索、列表、fact detail 使用同一规则。删除旧的“当前群本人 evidence
投影 Person”授权分支，保留来源数据。不得复用读取扩权去放宽 mutation 或 evidence。

### D. 意图和工具

统一路径：真实请求者/目标 → Read Scope Resolver → MemoryQueryIntent +
ResolvedReadScope → MemoryQueryPlane → 原检索排序。自动预取保持 background/
continuation，不增模型调用。Main Agent 提供 purpose/entities/time/kinds；后端
用已解析目标补 subjects，不作为权限。保留 lexical/hybrid/overview、FTS、embedding、
rerank，不把 RRF 当概率。

人物工具保留真实 mention/reply、名称、兼容 ID；名称在已授权历史范围内精确唯一解析，
歧义返回有限候选。无需模型提供共同群号证明权限。无群选择器时查询 Person 与获准
PersonGroup，保持数量上限；支持可选群名/ID 过滤。群工具群聊省略目标默认当前群，
私聊无唯一默认时要求指定；历史群支持名称/兼容 ID。fact detail 一致，evidence 不扩权。

后台仍只从当前人/群、真实 mention/reply 选目标，不预加载整个历史社会图；广域查询
由主动意图工具触发。后台最多两条，可零条，无最低配额、冷却或新阈值。

工具 schema 部署级固定，不嵌动态群名/ID/昵称。Group 读取加入默认固定首轮集合，
保留已有 Person/SELF 工具与用户显式配置。空结果是成功；歧义、无授权、基础设施错误
分开，不可重试结果 retryable=false。同轮相同读取复用，不自动重试已确定拒绝；保留
总轮数限制，不加少量读配额或强制结束对话。验证固定前缀结构，不承诺固定缓存命中率。

### E. 可观测性与 0051

既有 Worker/模型观测/job outcome 增加无正文批次关联、触发原因、事件数、字符数、
等待年龄、真实请求与重试次数、extracted/validated/applied/candidate/rejected/no_claims
分类。区分事件 job、提取 batch、真实模型 request，不新建通用执行器/批次业务表。

0051 只加 recall receipt/item 观测字段：unknown/pending/succeeded/failed/skipped、
完成时间、脱敏原因、item 是否实际评估。成功无使用不是超时/抢占/队列满/禁用。
历史 used=false 保持未知。零注入预取也记回执但不调用 attribution；主动工具暴露关联
当前轮。失败、取消和重启遗留 pending 不永久处理中。Plugin/Admin 读不强化。

报告包含自动轮数、零注入比例、候选/注入/评估数、已评估使用率、attribution 覆盖率
与失败、同会话同事实重复分布、主动查询成功/空/歧义/拒绝/重复数。不把未知算无用，
不强迫回复引用记忆刷分。fresh/head、0050/0051 校验 schema/FTS/trigger/FK，关键业务
行与内容摘要不变，不改事实/证据/会话/身份/路由。

## 文档与旧逻辑

对应行为提交同步文档，不把矛盾说明留到最后：

- memory-v2.md / memory-v2-retrieval.md 改为 canonical 四类、批次提取、社会读取与
  Query Plane；删现役描述里的单事件、旧身份键、Plugin API v1。
- memory-change.md / 第三方事实文档明确读放宽而写不变，保留 PersonGroup 第三方写、
  evidence、SELF 边界，禁止全局替换“隔离”。
- quality 架构/指标/运维同步一小时、价值、batch/job/request、未知判定与使用率分母。
- README/help/canonical-runtime/工具帮助说明当前能查什么并链接唯一当前合同。
- roadmap、phase1–6、Adaptive 草案/实施计划、SELF/reflection 旧任务书、历史实测报告
  原地标为历史非现行合同，链接当前合同，移出当前实施导航。
- CHANGELOG 和已发布 notes 不改写历史。

删无调用的旧授权投影/重复分支，不扩展到无关清理。人工核对当前指南不再宣称永远禁止
他人 Person/跨群读取，同时保留写入和 evidence 的严格限制。

## 提交和验收

1. C1 feat(memory): distinguish recall evaluation outcomes — 本任务书、0051、零注入、
   attribution 生命周期、批次统计及指标文档。
2. C2 fix(memory): retain valuable memories in hourly batches — 等待、唤醒计数、价值、
   来源、自省、质量/运维文档。
3. C3 refactor(memory): unify historical social read access — resolver、搜索列表详情、
   去旧投影、读写边界文档。
4. C4 refactor(memory): reuse intent queries across read entrypoints — 统一查询、工具
   选择器、意图、固定 Group 工具、去重与帮助。
5. C5 docs(memory): retire obsolete policies and validate behavior — 历史文档、导航、
   数据集及最终报告。

每次目标 pytest、ruff format --check、ruff check、mypy src、git diff --check。
最终完整 pytest（最多 800，替换旧规则测试，不删安全门/不靠巨大无关循环作弊）、
memory quality validate-dataset/run --suite full/compare、example/GitHub 插件契约、
fresh/0050 迁移与完整性、Compose/release smoke。数据集读取政策显式升级，不能为绿灯
随意改性能基线。

必须验证：时间/数量/字符领取、即时 mutation、空结果进度、重试幂等；低值拒绝、稳定
事实和单次经历接受、自封 explicit 无效、维护不阻断；私聊/跨群历史读取、无共同关系
拒绝、不传递、forget 撤销、不同入口一致、原始私聊/evidence/SELF/写权限不放宽；
插件不能冒充、歧义/空结果准确；意图字段入核、不加模型和动态 schema、不全图预取、
零注入统计、判定失败与未知区分、取消恢复及日志无敏感内容。

真实验证先冻结合成样本，8 次批提取、4 次 reflection、2 个主动工具场景共预留 4 次
Main Agent 请求；余 8 次修正/重试，总计最多 24。检索夹具本地，不额外调用模型裁判。
人工核对低价值排除 >=90%、应保留 >=80%、权限/证据违规为零。用完仍失败则报告问题，
不加调用、不宣称通过。

完成不意味着使用率必然提高；本轮不抑制后台，重复注入留待真实统计评估。未来另行
授权上线，先生产副本验证 0051 与备份，再**本地构建**部署；不远端构建，不清上下文
掩盖问题。上线观察成本、队列年龄、价值拒绝、主动读取和判定覆盖率。

## 最终验收记录

- 静态与确定性测试通过：Ruff format/check、mypy、800/800 pytest、84 项示例与 GitHub
  插件契约测试；测试收集量保持在预算上限 800。
- Memory quality 数据集为 19/19 case、38/38 query；发布检查的版本、Alembic 0051、
  数据集、质量/性能基线、合同、迁移和 4 个 Plugin API 2.0 manifest 必选门全部通过。
- fresh -> 0051 与 0050 -> 0051、schema/FTS/trigger/FK、Compose production/dev 配置、
  本地镜像的 source-free 完整 release smoke 均通过。
- 有限真实模型验证严格用满 24 次请求（重试计入）：8 次批提取达到应保留内容召回
  100%、低价值排除 100%；reflection 的初始 4 次及有界 4 次复测覆盖有价值 episode、
  重复/noop 和琐碎/noop；两个主动读取场景共使用 8 次 Main Agent 请求，最终实际调用
  `get_person_memories` 与 `get_group_memories`，均注入合成事实并产生非空回复。
- 主动读取临时验证脚本最后的聚合布尔值曾因读取了与 AgentToolService 不同的
  `MemoryContextService.metrics` 实例而显示 false；工具调用、注入日志与回复结果本身
  均成功。该项记录为测试夹具的观测实例限制，没有追加超过 24 次预算的调用。
- 全部模型验证使用合成数据；未修改生产记忆、生产数据库、人格、`.env`、会话、路由或
  Rollup generation。当前记录不代表已经部署生产。
