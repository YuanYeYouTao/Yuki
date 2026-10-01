# 长任务 Harness 与上下文压缩重构任务书

## 1. 基线、授权与交付边界

- 编制日期：2026-10-01。代码基线：`ffca08a1255ea8b4fff8d403c7467e7c43cc51ef`。
- 开发分支：`codex/long-task-harness-compaction`。
- 本文状态：本轮实施已通过最终提交 CI，PR #216 已合并为 `ea446d6`，Bot 已部署至 schema `0088`。本文中的原代码证据保留其基线，迁移对账、实际热参数与尚未完成的真实长任务/缓存验收见[交付记录](../operations/long-task-harness-2026-10-01.md)。
- 用户已同意取消累计步数等硬上限、增强长任务能力，允许子 Agent 协助；既有交付授权为新分支开发、PR、合并及按现行运维流程上线。
- 测试按风险定向执行；终局之前不跑全量测试。普通群聊效果、正在回复时的 steer 和统一执行入口必须保留。
- 用户说明反复压缩失败可能已经修复。本轮审查针对压缩逻辑及长期运行边界，不将历史现象认定为当前线上故障。
- 本文已按 2026-10-01 第二轮架构复评修订：替换存储压力驱动摘要的旧职责，扩大可配置工作窗口，删除通用阶段完成门禁及重复效果权威副本。允许整段替换模块和删除旧实现，优先降低最终结构复杂度。

实施前及每次设计变化先核对 [共同架构约束](development-contract.md)、[主 Agent 合同](main-agent-runtime.md)、[持久工作者](persistent-subagents.md)、[Conversation Rollup](conversation-rollup.md)、[Provider 合同](model-providers.md)、[Tool Kernel](tool-kernel.md) 与 [持久环境](../operations/persistent-environment.zh-CN.md)。任务书不得覆盖现行合同；需要改变政策的地方必须同步修改合同和实现。

### 已读取的生产快照

2026-10-01 19:49 台北，现有 Bot 健康接口为 `ok`，数据库版本为 `0085`；恢复检查点及媒体计量约 42.1 MiB。最近 12 份 journal 均为 completed，约 74–146 KiB，未观察到其中的压缩链边界。这个小样本不能验证长任务能力。

19:56 台北，当前容器从 17:56 启动，RestartCount=0，`/livez` 为 200；读取最近 24 小时、最多 40000 行现容器日志，压缩、Rollup、context/journal 容量相关关键词匹配为 0。旧容器日志与未打印的失败不在此证据范围内。没有线上重复失败的确认，也没有压缩质量正确的证明。本轮生产操作只读。

20:17 台北再次读取实际 Settings、Profile 与无正文的调用计量：线上 `MAX_CONTEXT_CHARACTERS=131072`、Work 默认窗口 131072 token、Rollup 摘要上限 **1200 字符**（代码默认 2400），其余主要 Rollup 水位见下表。最近 200 次 chat_agent 调用均记录 Gemini 3.8 Flash，其中 197 次有 token 数，输入中位数 34867、P90 41286、最大 44854；最新成功请求为 20:13，输入 29572 token。这个有界样本证明当前实际路由与常见输入规模，不证明代理可以接纳更大请求。

## 2. 目标与架构决定

所有会话共用一个 `YukiRuntime`，显式隔离 Conversation、actor、generation、Work 和执行身份。普通聊天、SELF、自动化、插件主调用使用同一个主 Agent 执行链；普通聊天不强制登记 Work。模型自行判断如何聊天、是否接纳工作、是否分解任务，不增加闲聊/工作意图分类器或第二套长任务运行时。

本轮把长任务所需的可靠性落实到现有执行层：原 Work 持续推进，有界上下文能够反复更新，原始证据可回查，追加要求在安全边界生效，结束必须符合已登记目标和执行事实。进度报告与主动分工属于代码管理的行为合同，使用既有工具；不改人格提示词，不增加定时汇报器。

必须区分以下三种数据：

| 层次 | 权威来源 | 压缩与保留规则 |
| --- | --- | --- |
| 业务事实 | 原始事件、Work 输入、effect/发送/命令回执、工作产物、已登记目标与约束 | 不由模型摘要改写；恢复和完成判断按原内部 ID 查询 |
| 模型上下文 | Conversation Rollup、Work 执行摘要、近期完整消息和工具回合 | 可有损缩减，但必须标明来源、缺失及覆盖边界，能回到事实来源 |
| Provider 私有状态与诊断 | 实际协议 continuation/opaque items；有期限的 execution trace | 协议状态按适配器合同保存；诊断不能充当业务恢复库，opaque 状态不能假装为明文摘要 |

Conversation Rollup 与 Work compaction 共用必要的来源、容量和提交原则，仍各自持有投影：前者服务群聊历史，后者服务某个任务执行。它们不能互相替代，也不能成为第二份长期 Memory。

### 2.1 自评与最终结构

第一版可作故障清单，但不足以直接作为最终架构：过多沿用整体 JSON journal 和旧窗口控制，只补围栏容易留下错误职责；通用计划完成门禁把模型建议升级为业务权威；把所有 Rollup 增强都绑定无限 Work 上线也扩大了关键路径。修订版要求主动删除这些设计。

最终只保留三类职责，复用现有 owner，不增加压缩总管或长任务 loop：

1. **执行事实与结果存储**：effect/run 回执持有唯一执行状态及原正文引用，输入日志持有用户要求；文件 store 提供证据内容与留存。checkpoint、摘要和模型视图不再双写整份效果事实。
2. **请求窗口与恢复视图**：按所选 Profile 规划实际请求容量；Work 与 Conversation 分别构造自己的上下文。保留可用恢复点，显式替换链，历史证据按需读取。共享真正相同的容量/来源 helper，不建立万能 ContextManager。
3. **激活调度**：现有 Runtime/Runner/child scheduler 管理公平分段、并行、输入交付与关闭；次数计量与可选预算在原所有者上持续。

原 Work、内部事件、权限、租约、真实回执和已接受效果是保留对象；旧的巨型恢复 JSON、重复安全状态、字符窗口分支和串行调度实现可以替换。新接口必须删除已有重复职责，不能只在旧类旁再加一层。

### 2.2 水位与窗口评估

| 当前限制 | 线上/代码数值 | 评估与目标 |
| --- | --- | --- |
| 前台历史与动态资料字符池 | 131072 字符；编译器惯用 chars/4 估算 | 约 32768 是估算 token，不是 128k token；未覆盖全部 static/tools/media，替换为完整请求 token 预算 |
| 原始历史二次 admit | 正 coverage 时最多 512 可见事件、102400 字符，另受总余额限制 | 提高字符池也绕不开此上限；`coverage_end>0` 还不能可靠证明存在摘要，删除这些生命周期分支，统一按容量和来源规划 |
| Rollup 触发/整理目标 | eligible prefix 384 事件或 81920 字符触发；stop=0，尾部目标 128 事件/20480 字符 | 在模型远未到窗口时大幅压缩群史；事件计数改为查询/延迟政策，不能强制把低 token 负载压至极短尾部 |
| Rollup 叙述 | 线上 1200 字符，代码默认 2400 | 对多人多主题明显偏紧，改为 token 预算内的叙述及必要来源投影，预算随来源和总窗口分配，不追求每次填满 |
| Work token 水位 | 上次 prompt usage 达 131072 的 85%，约 111411 token | 85% 本身不算过紧，错在窗口来源和增量预测；改为下一实际请求对有效容量的判断 |
| journal 存储水位 | 单份 3 MiB 触发、4 MiB 拒绝；全局 56/64 MiB，计入媒体 | 不能解释模型语义容量；移出摘要触发逻辑，替换整体 JSON 存储，磁盘/对象 admission 单独治理 |

Google 官方为 Gemini 3.8 Flash 列出输入 1048576、输出 65536 token；目前实际 Profile 只有 LONG_CONTEXT 标签、默认输出 8192，没有数值输入窗口。[模型规格](https://ai.google.dev/gemini-api/docs/models/gemini-3.8-flash) 当前代理的有效上限必须显式核实，不能直接把官方上限当代理实测值。

聊天与持续 Work 分别配置活动输入预算，262144 token 只是模拟候选上界，不是预定的最终默认值。初始值按 §2.5 的真实流量成本模型选择，仍受已核实的所选模型/代理输入上限约束。预算包含固定合同、工具、媒体、历史、摘要和本轮输入，不是仅给 history 的配额。普通群聊无需填满预算，也不改变发送风格。

有效输入预算为所选连接输入上限、操作方活动预算及总窗口扣除输出预留（若该协议有共享总窗口约束）的最小值，再留估算误差与下一批输入增长余量。输入/输出独立限额的协议不能重复扣输出。本次比例校准后，聊天 96k 采用 0.90/0.60，Work 128k 使用独立 0.90/0.50；约 1.8 估算/usage 比例下分别保留约 32k/35.6k usage，满足模拟 31,064 usage 的假设下限。Work 不沿用更高聊天 target，以减少无依据的热输入携带；新 Work 策略减少该模拟压缩次数，但并不保证所有比例费用更低。比例 2 的聊天目标仍低于该假设，不能声称全部流量保持已证明。它们是可调启动政策，不是正确性不变量；硬 Profile 容量先核验，新一批结果预计越界时可提前整理。

摘要使用独立 token 预算，首版可从 4k–8k token 上限评估，按实际开放事项和来源规模生成；不能因为把窗口放大就把每轮摘要或 QQ 回复写得更长。计数优先使用可用的 tokenizer/Provider countTokens，后备估算按实际 usage 校准；中文、工具 schema、媒体和 opaque 协议项不得统一视为 chars/4。[Google token 计量](https://ai.google.dev/gemini-api/docs/tokens) 不为每次请求无条件增加一次远端计数调用，可对增量和稳定前缀缓存计算结果。

### 2.3 缓存与稳定前缀验收

2026-10-01 20:31 台北只读生产计量，主聊天最近 1/6/24 小时：有 prompt 与 cache 两项计量的样本按 `sum(cached)/sum(prompt)` 为 82.18%/85.86%/85.67%。24 小时共 475 次、466 次有输入计量、401 次同时有缓存计量；已上报 cached 合计 11751222，全部已上报输入合计 15812497，因此全部有输入计量样本的已确认缓存份额为 74.32%。缺失缓存字段不是已确认零，不能只报完整样本比例而不披露覆盖率，也不能将已确认份额当精确实际命中率。

同一任务 chat_agent、各取最近 200 次：Gemini（10 月 1 日 10:46–20:13）168 次有完整缓存计量，按该子集加权 85.05%，全部已上报输入的已确认份额 72.69%；DeepSeek（9 月 28 日 18:46 至 9 月 29 日 03:16）199 次有完整计量，加权 97.23%。样本时期、协议和请求内容不同，是生产历史对照，不能冒充同 payload A/B 实验。

抽查三轮真实 Gemini 执行的四对同链 Provider 请求：systemInstruction、完整 tools/toolConfig 与 generationConfig 一致，旧 contents 均为下一请求的完整前缀；诊断中 opaque 项按原哈希引用比较，没有输出私密正文。另抽查同一 Conversation 的三对新聊天轮，静态字段一致，历史分别有 107/108、109/110、111/112 项相同前缀，上一轮尾部变化。原实现遇到非 Responses 私有 continuation 即使普通历史投影视图整体失效，下轮重渲染早先已提交的公开输入，这是本轮确认并修复的不必要跨轮重建路径。

本地修复在 native tail 边界停止扩展普通投影，保留之前已提交且当前 read scope 仍认可的普通输入前缀；签名、思维和工具调用/回执仍留在原 Work journal 和原 provider chain，不写入公共群史，也不假装为公开摘要。`test_gemini_history_prefix.py` 经真实入口、Gemini HTTP serializer 和 SQLite 重开，以七次请求覆盖读工具、发送回执、同 actor 下一轮和换 actor：旧输入 parts 逐字保留，新的公开事件与当前动态资料追加，system/tools/toolConfig/generationConfig 一致，旧 signature 不进入新普通轮，换 actor 不继承旧动态 envelope。容量、图片、generation/来源编辑、合同/Profile/权限及当前 read scope 缩窄仍是必要边界；此项本地回归不能替代上线后请求序列和缓存计量验收。

实施与验收要求：

- 主/子各自固定合同在同链稳定，真实协议续跑只追加；协议对象外置须原序列化还原，不能改变 tools 顺序/schema、system、签名状态或请求设置。
- 新普通聊天轮的可共享历史保持原有获准事实投影；变化的本轮 actor/时间/Memory 等动态资料放末尾。核对 Gemini 跨轮视图重建中可避免的历史变化，不能为缓存跨 actor 带入旧私密资料、丢弃签名约束或无限积累旧动态输入。
- 只有容量压缩、合同/Profile/权限或明确来源变化等真实边界重建链，记录原因；摘要不更新旧链前部。扩大窗口旨在减少过早重建，不保证 Provider 命中率单调上升。
- 定向验收比较实际 Provider payload 的最长相同前缀、完整 tools/native tools、system 与设置，并覆盖同链工具回合、重启、steer、同 actor 新聊天轮、换 actor/图片/权限和压缩后新链。不能只验 stable_prefix_hash；新链首请求与其后续请求分开计量。
- 上线前后对同 Profile 分任务/同链与新链统计 token 加权命中率、计量覆盖率和未缓存绝对 token；记录窗口扩容后是否减少重建。不能把不同模型的历史比值设为机械 CI 门禁。

Gemini 隐式缓存由上游决定，官方明确无节省保证；显式缓存属于独立能力，当前代理未核实支持，本轮不凭猜测启用，也不靠额外保温请求刷命中率。[Google 缓存合同](https://ai.google.dev/gemini-api/docs/generate-content/caching)

### 2.4 Antigravity Manager 代理侧核查（本轮必做）

只读核查证据见 [代理逐跳审计（2026-10-01）](../operations/provider-compaction-audit-2026-10-01.md)。真实同一 turn 的三请求已关联至代理 UUID 与最终发送点，并逐项核对 payload/usage；报告另列脱敏、原始 Google wire usage、实际容量认证和上线后自然缓存观察的证据边界。`countTokens` 可达但当前实测只数 contents，列表 `inputTokenLimit` 不能当完整实际请求容量认证；这些能力边界不冒充已完成在线容量/缓存验收。

用户已指定实施期间读取代理服务器数据，不能只凭 Yuki 侧日志归因缓存差距。2026-10-01 已从本机以 `ssh antigravity-server` 只读连通，当前容器为 `antigravity-manager`、镜像标签 `antigravity-manager:gemini-request-correlation-v4.8.4`，同机有 `mihomo-host`；当前挂载为宿主 `/opt/antigravity-manager/data` → 容器 `/root/.antigravity_tools`。这些是入口快照，采集前再次核对实际镜像 digest、容器、挂载与服务配置。历史补丁/回滚记录见 [供应商切换记录](../operations/provider-cutover-worklist-2026-09-29.md)；不照搬历史 schema、版本或账户状态，也不重复引入已修复的缓存缺失/显式零混同。

采集复用现有代理请求日志/数据库和最终序列化审计，时间有界、查询分页，生产只读。核查以下事实并形成可复查的逐跳对账：

1. Yuki 实际 Profile endpoint → SSH tunnel → AGM → Google 的有效链路。确认模型映射、协议转换、重试及账号选择，不将配置文件的名义路由等同于最终请求。
2. Yuki 原 request/turn/execution ID、代理日志 UUID 与已有最终发送点关联摘要。优先沿现有 request correlation 对账；时间只能筛候选，不能作为唯一匹配依据。多次转发/重试逐项记录，不把一次 Yuki 逻辑请求当作仅一次上游请求。
3. 对照客户端输入与 AGM 最终发送结构：system、工具/schema 顺序、contents 顺序、thinking/signature、模型/请求设置，核查新增包装、默认字段、裁剪及跨轮变化。沿既有脱敏摘要与受控本地比较，不输出密钥、OAuth token 或私密聊天正文；比较不到完整上游字节时明确证据边界。
4. 对照 Google 原 usage、AGM 保存与返还 usage、Yuki model_invocations：input/cached/output/thinking/total 各字段是否准确映射，缺失是否曾转换成零、缓存字段是否被漏记、思考 token 是否重复合计。缓存率统一用 token 加权并披露各层计量覆盖率；缓存缺失不是自动命中或自动零。
5. 核查路由/账号切换、间隔、重试与命中变化的关联，以及代理支持的实际输入限额、countTokens/显式缓存端点能力。先查已有数据与实现，不为测缓存向群里发消息、不制造保温流量，不凭账号切换的相关性断言它必然造成缓存失效。

代理补丁属于发现真实转换/计量缺陷后的授权修复范围：按其独立源仓库/开发约束定向验证、版本化补丁与回滚记录，部署只替换 AGM 服务，保留 Mihomo 和 Yuki/SnowLuma；不将代理变更混入 Yuki 镜像发布。无必要缺陷则只交付审计结论。最终报告区分 harness 前缀变化、代理转换/计量问题、上游缓存行为和仍无法归因部分；上线后重复同口径自然流量观察，不承诺恢复某个固定命中率。

### 2.5 成本建模、模拟与热配置（新增必做）

用生产 usage 和代理审计数据分别建模自然群聊与持续 Work，不把 256k 当固定默认。比较 64/96/128/160/192/256k 窗口及多组触发/目标比例；费用包含缓存命中输入、未命中输入、摘要输入/输出、频繁压缩导致的前缀重建。缓存字段缺失保留未知，并给价格比例、未知缓存与长期输入增长的敏感性区间。群聊回放和合成长任务模拟明确分开，不能把有限自然流量称为长期任务实测。

优化受完整当前输入、近期原文、目标/约束/未决事实、压缩次数与延迟约束限制；最短窗口的最低费用不是可用方案。报告给参数候选、数据范围、假设、可复现脚本、成本差异及推荐初始值，无法从数据识别的最优值明确说明。AGM 订阅/代理真实账单与官方 API 公开参考价分开，不能冒称已验证实际费用。

窗口、触发/目标水位和摘要输出预算接现有热配置目录及 WebUI；聊天和 Work 独立配置。热更改作用于下一次准备的请求/activation，已经提交的工具、协议前缀和 source candidate 不被改写。模型输入/输出及联合窗口数字上限另放连接 Profile，容量核验不能被政策上调绕开；跨作用域水位关系在配置写入前验证。
聊天/群史使用 `context.compaction_trigger_ratio`/`context.compaction_target_ratio`；Work 使用
`context.work_compaction_trigger_ratio`/`context.work_compaction_target_ratio`。每对独立校验 user/group/global
继承后的 target < trigger，修改一对不改变另一对，也不改已持久候选或效果。

### 本轮动态资料精简补充

按数字生命研究所 Yuki 的真实建议（内部事件 78944、78945、78947、78948），每轮无活动 Work
时的最近工作投影只保留内部 work_id、目标摘录、state、model_requests/tool_calls/sent_messages
简略统计及 creator_display_name。创建/更新时间、revision、creator_person_id、conversation_id、
generation、来源等审计详情仍在后端和既有 get/list 目录中，不删除业务身份或授权链。
活动 Work 的原目标、恢复身份和执行状态保持完整；已提交 Provider 前缀不重写。

可续接目录同样精简为内部 ID、状态、目标摘录、创建者显示名和是否有活动等待，至多 16 项。
目标最多 160 字符、显示名最多 64 字符，`goal_complete=false` 标识不完整摘录；摘录不是目标
覆盖或新的恢复授权。不预载完整等待条件，既有 get 按需读取完整 goal 和 wait，list 保留原
目录详情。查询使用有界 SQL 列投影和活动等待 EXISTS，避免先全读16项大目标和等待JSON再裁。
原活动 Work 的完整目标与恢复 anchor 不受这个显示预算影响。

检查所有当前动态块后，删除人物块里与可信 `context.relationship` 重复的关系数值；保留阶段和
风格，详情通过 `get_relationship`。时间、权限、投递、当前媒体、事件绑定引用和短期状态保留；
ShortState 的整体 512 UTF-8 字节资源限及 CAS revision 继续生效。文件/终端/自动化总目录和记忆
全文本来没有每轮载入，不新增意图分类或另一套状态装配。插件片段按已有注册和资源预算处理，
没有证据证明片段冗余时不删除。

采用实际序列化数据和公共 token 估算器衡量，不能沿用模型口头估值。代表样例（短中文目标、
真实目录字段形状）最近 Work 原目录 536 字符/212 估算 token。短目标优化只是小幅节省，核心
是让16项大目标目录保持有界；数字仅为合成字段样例，生产目标长度和语言分布另计。8192 字符
完整中文目标仍会占明显容量，不能为节省显示费用偷偷截断当前 Work 的约束。少量回归验证原
稳定前缀不变、终态仍可查询、16项完整大目标及等待按需可回查、动态目录有界、活动目标不被
截断和关系只有一个常驻风格块。

## 3. 当前代码审计

下面的“确定缺口”指代码行为或合同不一致已确认，未声称所有场景均已在线上触发。恢复检查点 64 MiB 配额不包含全部 effect、工具文件、workspace 和沙箱日志。

| 编号 | 级别与触发 | 代码证据（本任务书基线） | 影响与修复方向 |
| --- | --- | --- | --- |
| F1 | P0，溢出的非 sandbox 工具结果含 `uncertain` | `capabilities/results.py:147,361`；`runtime/work_control.py:413` | 大结果 manifest/minimal 丢掉 `uncertain`、`error_code`；完整文件存在仍不能补偿本地安全判定丢字段。保留不可裁剪 outcome envelope，安全判定读取真实回执 |
| F2 | P0，未决效果之后继续 64 次只读调用 | `runtime/work_control.py:426,664`；`runtime/work_session.py:438`；`runtime/work_repository.py:1311` | 最后 64 项展示窗口同时用于未知效果围栏和完成判断；旧未决项可被挤出，表内原记录未删除。按原 Work 精确查未解决效果，显示窗口不作安全权威 |
| F3 | P0，中文 JSON 未到字符上限但超过回执字节上限 | `config.py:355`；`capabilities/results.py:109`；`runtime/work_repository.py:67,1350`；`runtime/work_session.py:501` | 24000 字符与 65536 UTF-8 字节不一致，可在真实执行后无法保存 accepted 结果。统一字节预算，持久化可存储的 envelope 与原结果引用 |
| F4 | P0，Work 等待超过一天 | `config.py:361,374`；`mcp/repository.py:621,676,759` | 工具大结果默认 86400 秒到期，读取拒绝、清理无活动 Work 引用保护。原 handle 和 journal 并不保证正文仍可取回 |
| F5 | P1，新增大结果/steer 后下一次请求 | `services/agent_runner.py:418,648,970`；`runtime/work_journal.py:253` | token 判定取上次响应；3 MiB 触发、4 MiB 硬限之间可能先保存失败。需要下一请求及下一次保存的容量预测与外置策略 |
| F6 | P1，主模型窗口不同于默认值 | `services/agent_runner.py:418`；`services/subagent_execution.py:356` | 主 Agent 未注入实际窗口，使用 131072 默认；压缩时机不可信。从实际 Profile 能力配置获得窗口，记录估算误差 |
| F7 | P1，多次执行压缩 | `runtime/work_session.py:333–369`；`services/agent_runner.py:424,650` | 原始 anchor + 自由摘要 + 当前 effects 替换全部 transcript，没有近期完整回合；追加目标依赖摘要，校验只有完成、非空和大小。摘要递归易漂移，未校验缩减后可执行空间 |
| F8 | P1，摘要非完成、夹带调用、为空或过大 | `services/agent_runner.py:650`；`runtime/activation_outcome.py:149` | 统一 ValueError/nonretryable，缺少压缩专用原因、纠正和失败提交边界。压缩沿原主合同仍声明 native tools，提示“不要调用”不构成服务端禁止 |
| F9 | P1，长子任务多轮响应 | `services/agent_runner.py:632` | `cache_samples` 无界增长，不随 transcript 压缩；必须有界聚合，不能解除步数后留下隐蔽最终容量上限 |
| R1 | P1，少于保护事件数的长消息仍超预算 | `conversation/rollup/repository.py:427–445`；`services/context_assembler.py:1853` | 尾部选择忽略 `raw_tail_characters`，无连续可压缩前缀时前台拒绝；与现行长消息可降低事件 floor 的合同冲突 |
| R2 | P1，同名主体、仅提及、跨批回复 | `conversation/rollup/renderer.py:26–46`；`event_prompt.py:355,496` | 压缩来源只含名字/时间/正文，缺原内部事件、Person、方向、回复/提及；仅提及内容可确定性丢失。复用稳定事实投影，不能只靠提示词纠正 |
| R3 | P1，单事件超过来源 cap | `conversation/rollup/repository.py:466–480,1273`；`conversation/rollup/renderer.py:55–66` | 允许超大 singleton，再截掉来源尾部，却推进整个事件的 semantic coverage。禁止部分读取冒充完整语义覆盖 |
| R4 | P1，模型故障走 emergency overlay | `conversation/rollup/renderer.py:112,149`；`services/prompt_composer.py:267` | 默认最多 2400 字符尾部（当前线上 1200），模型侧仍统一标为 Conversation summary；需要明确 emergency、不完整来源和可回查范围 |
| R5 | P1，结构性语义风险，多轮多人、多主题群史压缩 | `conversation/rollup/service.py:122,174` | 固定摘要上限与仅形式验证已确认，默认 2400 字符、线上 1200，具体漂移未实测。早期遗漏不会由后续自然补回；连续性摘要与开放事项/来源引用应分工 |
| R6 | P1，大量未覆盖事件积压 | `conversation/rollup/repository.py:1150,1242`；`conversation/canonical_rollup.py:108` | 候选/复核反复加载未覆盖全集；batch 有界不等于查询有界。候选发现、计量与最终 CAS 分离，使用索引和有界分页 |
| T1 | P2，immutable workspace artifact 大于首段 | `workspace/service.py:137`；`workspace/store.py:466` | artifact_id 路径未使用声明的 offset，只读首 32768 字节；修复真实分页读取，保持不可变引用 |

补充边界：沙箱 stdout 为滚动两段、每段 4 MiB，旧 cursor 会返回 `output_lost`，结束日志有容量与时间回收；它不是完整长期日志。工作区普通 artifact 默认无 TTL，但存在容量限制。execution trace 会裁剪/脱敏且有期限，不能作为完整原结果兜底。

现有正确围栏必须保留：相同合同恢复实际协议、pending calls 读取原 effect 而不重做调用；迟到 accepted 不被 unknown 覆盖；Rollup generation、lease token、fingerprint、连续 coverage 和活动 source hold 复核；emergency 不提升为 semantic checkpoint。不要为重构删除这些保护。

## 4. Codex、Claude Code 的可借鉴机制

资料检索于 2026-10-01，仅使用官方文档与 OpenAI 官方源码；以下行为不等同于这台桌面应用或所有 Provider 的实现承诺。

### Codex

官方配置提供自动压缩 token 阈值和模型上下文窗口。[配置说明](https://learn.chatgpt.com/docs/config-file/config-reference)

官方公开 `compact.rs` 中，一条压缩路径使用专门摘要请求，重建有预算的用户消息和摘要，按显式边界重新注入初始上下文、替换并持久记录历史、重算 token；记录压缩状态与窗口元数据。上下文超限处理与传输重试单独分类。这证明 Codex 也需要 harness 管理压缩，而非仅有一条总结提示词；不意味着用户消息或历史无损保留，也不照搬其删旧项策略。[官方源码](https://github.com/openai/codex/blob/main/codex-rs/core/src/compact.rs)

OpenAI Responses 另外提供服务端阈值压缩与 `/responses/compact`，返回包含 opaque/encrypted compaction item 的窗口。独立 compact 的完整输出应作为后续规范窗口，不能任意裁剪；该调用输入本身仍须能放进模型窗口。API 能力不能直接当作每种 Codex 接口都使用它的证据。[Responses compaction](https://developers.openai.com/api/docs/guides/compaction)

### Claude Code

官方说明接近窗口时先清掉较旧工具输出，再在需要时总结对话；早期细节仍可能丢失。支持指定压缩重点；出现大内容导致压缩后立即填满等反复情形，会停止自动压缩并报错。子 Agent 把过程隔离在自己的上下文中，父任务接收结果摘要。[工作机制](https://code.claude.com/docs/en/how-claude-code-works)

根 `CLAUDE.md` 会在压缩后重新加载，保证持久规则不完全依赖会话摘要。这可借鉴为 Yuki 的代码管理合同和任务约束恢复，但不能把外部聊天、工具输出升级成系统指令。[规则与压缩](https://code.claude.com/docs/en/memory)

Claude API 的服务端 compaction 与按规则清理工具/思考块是两类能力；原生返回块及保留近期回合的协议要求依功能而异。它们不是对 Claude Code 所有内部算法的公开说明。[Compaction](https://platform.claude.com/docs/en/build-with-claude/compaction)、[Context editing](https://platform.claude.com/docs/en/build-with-claude/context-editing)

### Yuki 的取舍

借鉴显式压缩边界、容量重估、近期原始输入保留、持久规则与任务事实重建、子任务上下文隔离和压缩结果提交。Yuki 必须同时服务多个 Provider；本轮可移植压缩是必做交付，原生 compaction 是后续按显式能力接入的增强项，不作为本轮上线依赖。不得强制切 Provider，也不把 opaque item 翻译成普通用户文本。Claude 的延迟加载工具策略不照搬，Yuki 主工具合同仍冻结。

## 5. 实施设计

### A. 先修事实保留与工具结果合同（P0）

1. 执行层接收类型化 outcome；保留 `ok/status/uncertain/error_code/executed/mutation_committed`、原 call/effect/run 身份及真实结果引用。业务安全判断不能反解析为模型节省 token 的字符串。所有 manifest/minimal 投影保留这些字段，缺字段不能默认确认成功。
2. UTF-8 字节预算统一用于可持久回执；模型上下文另用 token/字符预算。大正文在返回给模型前写成有界回执 + 完整结果对象引用。文件 I/O 在 SQLite 写事务外完成，以可恢复的对象发布/引用提交与孤儿回收处理文件和 DB 非原子问题。执行是否确认与正文是否可读是两个维度：正文保存失败不能把已有 accepted 投递/执行事实改为 unknown；无确认的效果仍保留 unknown 和原执行身份。两类情况都不能自动重跑真实操作。
3. 复用 effect 查询边界，从持久事实精确判断原 Work 是否存在未解决效果和未确认交付。不能只查 effect 行的 prepared/unknown：已 accepted 的工具回执仍可能报告 pending/uncertain；类型化状态须持久并按原 effect/run 对账，只读查询同一 run 的结果不能抹掉它的副作用归属。末尾 64 项仅为展示缓存；未决项不能靠缓存淘汰获得完成或新副作用资格。读取使用索引存在性查询、分页明细，禁止靠任意 LIMIT 推断集合已空。
4. 完整结果对象建立原 Work/effect 的引用生命周期；活动、暂停及可恢复依赖的对象不因一天 TTL 被读拒绝或清理。父子结果交接继续保护仍被父 Work 使用的对象。终态释放及留存遵循现有归档/删除授权，不能永久无限保留；只延长全局 TTL 不是修复。
   引用保护不等于无限文件容量：明确单对象、总体文件存储 admission 与清理策略，尽量在派发前检查可用空间；结果实际返回后仍可能超限，须保留已知执行事实、原身份及真实正文缺失状态，并以可解释容量原因暂停，不能重做效果填补正文。优先扩展现有 artifact metadata，不新建通用对象系统。
5. 压缩后仍需的媒体/产物按业务引用保留，不能仅按新 transcript 是否含内联内容回收。普通媒体缓存与任务证据的生命周期分开。
6. immutable artifact 支持真实 offset/limit。日志读取明确 `output_lost`，需要完整长输出的任务写持久工作区日志并发布稳定引用；本轮不承诺所有历史 stdout 均可复原。

### B. Work 压缩改为可恢复的上下文替换（P1）

续跑窗口由以下内容构成：稳定主合同与来源；现有 Work 上的有界任务资料；来源可追溯的结构化执行摘要；最近完整输入/工具回合；必要的真实未决效果、等待及产物引用。相同字段只出现一次，既有原始任务 anchor 不再与多份累计摘要反复堆叠。

任务资料包含当前目标、用户明确追加限制/交付要求及来源 input/event ID。输入日志是原要求的来源；小型资料为其可追溯投影，不再独立复制每项事实。必要的版本更新复用原 Work checkpoint 子路径/CAS，避免与 journal/等待字段整份覆盖；不增加 Goal 表、通用 stages 状态机或每步思考持久化。模型计划与下一步建议是可更新、可重建的上下文，不是完成门禁。自由摘要不得覆盖用户要求、生命周期或原回执；结构化资料也不能自动证明语义完备。

本轮实现：`WorkSession` 在原 journal progress 保存 `task_material/covered_input_id`，摘要源只读取水位后的已 staged/consumed 真人输入；原 `runtime_work_inputs` 全部正文不改写，普通续跑仍追加原输入。当前资料保存原始 immutable goal/anchor、有效约束及 input 引用、明确更正记录和最新两份完整原文。辅助输出使用现有 `json_schema` 格式与严格本地 schema：派生事实、未决问题、失败/未知、产物和下一步均有快照内来源引用，逐项说明新输入属于约束、更正或普通上下文；已有有效约束须保留原 text/refs，更正须引用新增 input 且保留改前资料。普通进度/继续输入不自动变成累计永久约束。候选只带一份本地任务资料；执行状态仍从真实 effect/run/回执读取，摘要不能解除未决围栏。结构/引用/遗漏或最终存储失败保留原 paired checkpoint 与 progress，Profile/合同变化在来源未变时保留原资料，来源变化不迁移旧资料；旧自由文本 checkpoint 的恢复仍兼容，新辅助输出不接受自由文本兜底。

资料整理使用有界分页：每页新增输入最多 16 条且完整原文总计 64 KiB，未读的下一条留给后页，不截断单条。首页发送原 records/effects，后页只带原 goal、上一页生成的资料与派生观察、该页新增原文；独立辅助 chain 逐页计原模型预算，全部成功后才一次提交新 paired/progress。每页验证后，在原 paired journal 的非权威 `progress.compaction_staging` 保存下一页资料/水位、冻结 input 上界及原 chain/sequence、canonical transcript 指纹、contract/Profile、source revision/generation、privacy generation 与 source scope。partial 不进入主模型的生效业务上下文；原 activation 公平 quantum 不变，让出或重启后只在同源标记匹配时续下一页，不重复支付已验证页面。末页也先保存已验证候选，最终提交失败可继续原候选；成功换链时一次启用完整资料并清除 staging。真实边界变化丢弃 partial，原事实仍按原 ID 恢复；非法或失败页面不覆盖主 transcript/生效资料，已成功的 partial 和已发生的模型计量仍保留。最新两份原文及最多 32 项有效约束、最近 16 项更正共同受 64 KiB 资料预算约束，每条派生事实正文上限 1024 字符、最多八个原引用；摘要本身与最终完整请求另行计量。真实必要资料或单条输入超容量时明确暂停，不截断后冒充覆盖完整来源；页大小不是任务寿命。schema/引用只能验证所供来源与结构，不能证明模型提取约束或更正的语义完整性。定向回归覆盖 20 次长 steer/压缩与三次重启、34 次仅上下文追加无累计寿命门槛、17/40 条短 steer 一次分页整理和第二页失败后重启续页、普通及 Gemini opaque checkpoint 在小 quantum 让出后重启仅支付剩余页面、原负约束、明确更正与 Profile 边界，以及坏引用、漏约束/输入、虚构生命周期字段和保存失败不覆盖。

staging 与最终候选在事务外完成 manifest/文件准备后，原短 writer 在发布 refs/journal 前再次核对冻结 source revision/generation 与 privacy generation，包括后台 child lease。准备期间发生真实边界变化会拒绝发布，保留原 paired/已提交 staging 与实际页预算；回归覆盖真实 child 的 stage/final × source/privacy 四种竞态及原链恢复。

整体 JSON journal 存储作为本轮替换目标：数据库检查点保留原 phase、chain、logical sequence、request/call/effect key、source/contract 版本、输入消费水位、必要 pending calls 和稳定协议对象引用；较大的实际协议内容按不可变段/对象保存于现有文件存储，保持字段顺序、opaque 原貌及 call/result 配对。新对象 ID 不能代替原 `chain_id + sequence + call_id` 执行键。对象化覆盖 dispatched、response、paired 各提交边界，不只保存已配对回合；写增量对象、短 CAS 发布可恢复视图。正常模型请求仍发送符合其协议的活动窗口，不承诺 Provider 原生增量传输。对象发布失败不切换恢复点，读取有完整性校验，文件 I/O 不进入写事务。存储配额与引用清理由原 Work 所有者决定，不再以 64 MiB 全局媒体压力要求其他 Work 写摘要。

压缩流程：

1. 在安全边界快照来源、chain/revision、已消费输入水位与效果引用；每次请求前估算下一份实际序列化输入（固定工具、native 声明、文本、媒体、追加输入、输出预留）。语义整理只由请求容量/明确质量政策决定；同时独立计算对象保存字节与 admission，不能把字节压力作为同一个摘要触发器。
2. ModelProfile 增加明确的输入 token 上限、输出上限/预留及必要总窗口约束，由选定连接配置提供；公共模型执行层向主/子入口注入同一容量合同。没有可信窗口时提供显式配置与可解释错误，不能从模型名字猜或把 131072 当全部模型的真实窗口。
3. 在已配对工具回合后压缩；不能切开 call/result，不能改写已提交的旧请求前缀。压缩期间到达的输入保存在原 mailbox，提交后按原顺序追加，不能被快照遗漏或重复消费。
4. 使用独立 chain ID、无业务工具和无 native 声明的辅助摘要请求，复用已选择连接及公共模型执行/计量边界，不携带原主 continuation，也不把 opaque 状态转译成摘要正文。它不发 Yuki 的声音、不驱动工具、不创建另一恢复 loop；主 Agent 冻结合同不按压缩阶段变动。不能依赖 DeepSeek 的 `tool_choice=none` 达成工具禁用。
5. 可移植摘要采用有界结构：已做事实与原证据引用、未决问题、失败/未知、产物、下一步建议、来源范围。不让模型填造生命周期；确定性任务资料与回执在本地合成。保留预算允许的近期完整回合，保留具有未决约束的输入引用和可读来源。
6. 候选通过结构/引用与协议配对检查后，计算最终窗口容量和缩减效果。来源超大须分批/外置正文，保留引用及显式不完整性；不提交“截头后当全文压缩”。若稳定合同、必要任务资料和近期输入已超过可执行窗口，明确容量原因，保留原 Work 等待处理。
7. 候选摘要形成与新链激活有明确提交点；来源 revision/lease/权限/generation 再核验，提交新 paired journal 与原 Work 链边界。辅助请求只共享模型 admission 和计量，其 dispatch/response 记录不得覆盖主 journal；禁止直接复用会调用主 `session.save("dispatched")` 的流程，最后可用 paired 保留到新候选 CAS 提交。崩溃前后恢复都不能重做业务效果。摘要请求允许在可计量、可解释的恢复策略下重做，不伪装为只调用了一次。
8. 分类处理摘要不完整、夹带调用、结构无效、窗口不足、存储配额与传输失败。只允许有界纠正/重试；无新输入且容量未改善，不得立刻再次压缩同一快照形成循环。不可压缩、无改善及容量不足以原 Work 现行可解释容量暂停结算，保留最后可用 journal 和必要业务事实；不能伪造 waiting_external 或立即 queued 重试同一快照，恢复按原暂停原因核验。

对单批工具结果的容量突增，先外置正文、保存可配对 manifest 与原效果回执，再构造压缩候选。删除 3/4 MiB 作为模型压缩/寿命的隐性硬线；单对象和磁盘限额仍有明确可配置政策。缓存样本移出业务 journal 或改有界聚合；统计元数据、chain links 和显示窗口均有明确上限及独立事实来源。

### C. Conversation Rollup 保留来源完整性与群聊连续性（P1）

1. 替换 assembler 的多套 near/admit/target/coverage 字符和事件分支，由实际完整请求 token 余额规划连续保护后缀与压缩前缀；事件数只作为有界查询和策略限制，不充当模型上下文上限。先保证必要来源、当前输入一次、活动 source hold 和连续 coverage，再保留预算允许的近期完整历史。单条输入本身超过可接纳窗口时显式处理，不靠覆盖旧消息伪装修复。
2. 来源投影与 raw history 共用事件事实口径：原内部 event ID、Person/author、方向、时间、回复目标、提及以及正文/必要派生内容。所有聊天和外部内容仍是 untrusted data；不得把平台 ID、昵称或模型总结当业务身份。
3. 超大原事件分片读取/总结并在完整来源就绪后推进事件级 semantic coverage；片段边界与来源指纹可恢复。若暂不能完整处理，只允许显式不完整 fallback，不能把未读尾部覆盖掉。
4. 用连续性叙述与有界开放事项/关键更正/来源引用表达群史，避免只有一条无限递归自由摘要。开放事项是派生视图，不是新 Memory 或 Work；原始账本始终可按内部引用有界回查。模型质量不可能由形式校验保证，但重要来源不能在输入投影时被确定性丢弃。
   本地实现使用 `conversation_rollup_v1` JSON：连续叙述、内部来源、最多各 16 项开放事项/更正（单项 1024 字符，全局 128 个不同引用）。更正携带新来源和已知旧来源，提示词要求更新叙述/已解决事项。新输出校验结构、引用来源集合与总容量，提交前核验事件真实存在及当前 Conversation/generation/覆盖归属，原 fingerprint/hold CAS 继续负责完整来源。历史自由文本仍以来源未验证标记读取，在下次成功压缩替换；不猜测语义或历史引用。递归更正回归验证结构和 carry 链，不代替真实 LLM 群史质量或上线验收。
5. 主模型可见 envelope 区分 semantic/emergency，说明来源范围、缺失及回查方式；emergency 仍不晋升为 semantic。不能让截断尾部宣称完整历史已总结。
6. 候选读取、尾部计量和提交复核改为有索引的有界区间/分页及 revision/CAS；模型、文件处理和历史扫描在写事务外。不得用过小 LIMIT 放弃连续性证明。
7. 前台保留现有 required 调度优先级与有界等待。记录等待原因、批数、容量变化和错误类别，不能把所有几十秒等待归为 Provider 慢；不新建压缩专用 runtime。

### D. 解除累计限制，保留公平调度与显式预算（P2）

- 默认新 Work 的累计模型/业务工具次数无限制；计数仍真实持久。采用明确的无限表示（如 nullable limit 的公开合同），不能换成巨型整数或每段清零。
- 原有显式预算保持原值和已用计数；没有真实来源能区分旧默认与用户指定的旧值时不得静默改写。旧工作解除限制使用可审计的管理迁移/操作，不由模型自行充值。
- 根/子任务共享计量、自动化 run 跨步骤累计限制、嵌套调用必须一起调整，避免只拆掉最外层 120/160。有限预算才保留必要收尾预留。
- 每段模型/工具 quantum 仍用于让出执行名额和保存检查点，不作为任务寿命、不触发强制报告/总结。原 Work ID、预算使用量、已接受回执跨段不变。
- 保留单请求超时、上下文/文件/存储容量、并发 admission、权限、取消、未知效果和精确 no-progress 围栏；逐项说明其安全或资源目的，清除以累计步数伪装任务寿命的重复限制。
- 移除根任务累计只能登记四个 child 的历史假设，改为活动/排队容量；全局目录真实分页，完成判断精确查询未结束子任务。资源耗尽仍应给出可读原因。
- 子调度器按已配置容量真实并行 claim/run；复用 Runtime 生命周期和执行监督，取消/关闭须 join。清理 `SubagentExecution.last_error` 等跨任务共享可变结果，不能靠增加并发参数掩盖串行实现。
- 使用既有模型池，保留前台名额，后台不能抢占全部群聊容量。共享 workspace 的子任务明确目录/文件责任，并发不是自动文件隔离。

### E. 长任务目标、进度与 steer（P2）

主 Agent 对复杂任务形成可更新的计划，按用户目标与真实证据判断完成；工具成功只是一个效果。自动结束与显式 `task_control.complete` 共用真实未决效果、未结束子任务、未消费输入及明确要求的产物/交付校验。删除通用“所有计划阶段 done/dropped 才能 complete”的硬门禁：模型清单不能证明语义完成，也不能因忘记更新清单卡住实际已完成工作。用户明确要求的里程碑按原承诺验收，不能泛化成所有 Work 的阶段状态机。阻塞则明确等待/问题，不把每次 final response 无条件重新排队造成空转。

在取得实质阶段成果、发现阻塞或目标变化时，主动用 `send_message` 报告已完成、证据、余项与下一步；报告回执与任务事实分开。独立且值得并行的子问题优先派出，父任务继续整合。无需每 N 步或每几分钟发言；安静任务和普通聊天仍合法，不复活 `report_progress` 旁路。

保留执行中的真实追加输入链。在已接纳 Work 的分段排队间隙，可对同来源、同 Conversation/generation 且唯一可续接候选追加输入；来源匹配 canonical actor Person、principal、委托及插件来源合同，不要求新旧 source_key 相同，输入沿真实新 event/input ID 幂等追加。多个候选必须显式定位，不能选“最近一个”。queued 也不能抹掉此前等待匹配的条件；`waiting_external` 仍按原等待匹配或显式恢复，不能因任意群消息提前唤醒；暂停、unknown 与权限围栏不自动解除。发送及外部调用已开始的效果不能靠 steer 撤回。

## 6. 迁移、删除与数据库规则

迁移号以实施时实际 Alembic head 为准；`0085` 仅是上述生产快照。计划的小型 Work 资料复用既有 checkpoint，结果引用/未决查询索引仅在现有结构不足时增加，不先造通用事件系统或另一套任务表。

无限 limit、对象引用和摘要 schema 均显式版本化，旧活动 Work 按原 ID、输入、回执与计数恢复。合同变化走新链，不能以恢复为名清空 journal 或重新接纳。已有已过期/丢失正文返回真实缺失状态，不能由摘要生成“原文”。旧 journal 能读取和受控转换，不永久保留两套活跃写路径。

必须删除或替换：把末尾效果窗口当权威的判定、从模型字符串反解析安全状态、checkpoint/journal 双写完整 known_effects、压缩摘要 effects 驱动授权、丢执行字段的 manifest、部分源截断却完整推进 semantic 的路径、自由摘要独占续跑事实的路径、整体巨型 JSON journal、全局存储压力驱动语义压缩、assembler 多重字符/coverage 窗口分支、无界统计数组、累计四个 child 的实现假设、重复累计预算。删除串行 child 运行实现及无调用者兼容写路径。旧测试若固化错误合同，应更新验收，不当作保留旧行为的理由。

效果状态与原结果引用只在 effect/run 事实链维护；上下文按需投影。普通无 Work 的大结果可继续有期限缓存，Work 的证据用途替换为所属结果存储；父子引用优先利用已有关系，仅在确实存在多 Work 复用同一对象的调用者时增加引用表。不要扩展为通用对象图、独立对象任务调度器、对象 outbox 或另一套 owner/epoch/generation。

同步修改现行模块文档与共同架构约束中的数据库/恢复规则：

1. 展示窗口和模型上下文限额不能裁剪业务安全事实集合；未决效果必须精确可查询。
2. 真实结果的可持久大小与引用发布必须在回执提交前确定，执行状态 envelope 不受正文裁剪影响。
3. 外置证据依赖原 Work 生命周期，清理按引用、代次和留存规则，不能只看对象创建时间。
4. 候选来源发现、身份解析、JSON 编码、文件 I/O 与模型工作在首次 DML 前完成；短 writer 只做必要引用/版本/所有权 CAS。重点修正 journal 保存中效果 JSON 在写入后编码的残留。
5. 清理分批读取候选，先短 CAS 核验无有效引用并取得删除所有权、阻止新引用，再在事务外删除文件，最后短写确认；中断可以恢复。禁止先 unlink 再核验引用的竞态；发布也需处理文件已落盘而引用未提交的孤儿。不得在持写锁时等待文件删除或重复扫描历史，也不得靠有界显示条数证明“所有任务都结束”。

## 7. 实施顺序与最小验证

| 阶段 | 交付 | 通过条件 |
| --- | --- | --- |
| P0 | 类型化 outcome、未知效果查询、字节预算、活跃证据引用、artifact 分页 | 原效果执行一次，正文可回查，未决围栏跨重启/长读序列不消失 |
| P1-W | Profile 容量合同、协议对象引用检查点、Work 窗口重建与压缩提交 | 多次压缩后目标/steer/原引用仍可核查；近期回合配对，没有无改善压缩循环；语义压缩不受其他 Work 媒体压力驱动 |
| P1-C | Rollup 来源/窗口/模式/有界查询替换 | 超大来源不假覆盖、身份关系不预先丢失，模型窗口可实际利用，原连续 coverage/hold 保留 |
| P2 | 默认无限累计执行、真实子任务并行、事实完成校验/阶段报告、分段间隙 steer | 计量不清零、可显式限额、父子正确收束，群聊容量和当前 steer 保留 |
| P3 | 删除旧路径、合同更新、终局检查、PR 合并及 Bot 部署 | 最新 head 检查通过；记录迁移、镜像、健康与恢复事实，真实 QQ 效果单独记录 |

无限累计执行上线依赖 P0 和 P1-W；P1-C 的确定性覆盖/来源缺陷分别验证，额外群史效果增强不阻塞 Work 的次数政策。按效果证据、容量与压缩、Rollup、预算及运维划分审查范围和验证证据。实际实现共用容量合同、配置及迁移链时，可在一个完整 PR 内交付，避免提交无法运行的中间基线；PR 数量以依赖能否独立成立为准。

只为真实风险设计定向回归，不为简单文档/可逆修改堆测试：

- 一组结果/恢复回归覆盖超大 `uncertain` 结果、后续超过 64 次只读、重启后完成/副作用仍受围栏；中文结果低于字符限而超过字节限时，原执行一次且结果可回查。
- 用模拟时间验证活动 Work 超过 24 小时、父子交接、压缩/媒体引用、终态释放和显式删除的清理行为；immutable artifact 多页读回原正文。
- 三至五次压缩、两次重启与中途 steer，核查原目标/新增限制/否定条件、原 call/run ID、未决状态、近期完整 call/result、产物引用和恢复输入次序。对 Chat、Responses、Claude、Gemini 比较真实协议序列化，不只比较哈希。
- 新增大结果/媒体/输入突增越过窗口或 journal 余量时，先保存可恢复结果再压缩；无效摘要、没有缩减、不可压缩 anchor 与配额压力有明确结局。模拟多轮统计增长验证 metadata 有界，不花真实模型请求刷上千步。
- Rollup 用少量长消息、超大事件尾部关键限制、同名不同主体、仅提及、跨批回复、旧 generation/来源变化、emergency fallback 与递归更正验证；当前事件一次、覆盖连续、未读尾部不消失。分片中断/重启只能提交完整事件；分片期间晚 ASR/视觉或来源改变使旧候选失效，模型运行期间新增 source hold 仍能阻止最终越界提交。
- 两个 child 真正同时运行、累计超过四个 child、目录跨页、取消/关闭 join、父任务未决阻止结束；前台仍可取得预留容量。
- 根/子/自动化 run 计数越过旧 120/160 后可继续；明确有限预算仍停在原身份且不重置。分段 quantum 只让出；现有群聊 steer、静默、显式发送、等待匹配和未知投递恢复做相关回归。

日常实施只跑受改动影响的组合；没有新改动/失败不重复已经通过的检查。终局运行一次仓库要求的完整检查及最新提交 CI；后续若新增代码，仅补相关检查并确认最新 head CI。线上不启动第二个 Bot，不发未经明确授权的真实 QQ 消息或戳人；采用已有真实会话/任务的只读观察，记录哪些效果尚未真人验收。

## 8. 合并、上线与回退

新分支提交与 PR 说明围绕最终行为和实际验证；合并前确认最新 head CI，合并后检查 main 对应构建。部署读取现有 Compose labels/覆盖文件，按现行运维手册只停止并替换 Bot：一致性 DB/配置及协议对象/证据目录备份、随目标包迁移至实际 head、固定镜像、恢复启动及健康/网关/Manager/工作区检查。停 Bot 后按同一快照备份数据库和引用文件，校验 manifest 引用对应文件存在且内容完整；扩展现行运维脚本，不另建备份服务。协议外置后不能把单份 DB backup 当作完整 Work 恢复备份。不执行 `down`、`--remove-orphans`，不替换 SnowLuma。

活动 Work 上线前记录 ID、状态、等待、未决效果与必要对象引用，升级后按原身份核对；不把容器健康当任务恢复和群聊效果验收。回退保留上线后的数据库、文件和回执，使用兼容存储合同的镜像；不能拿旧 DB 覆盖新执行事实。上线记录明确本地、定向验证、全量检查、提交、推送、PR、合并、镜像、部署与真人验收状态。

本轮完成的定义：已确认的事实保留缺口和压缩覆盖错误得到修复，长期任务没有默认累计寿命上限，有限资源有可解释的边界，多轮压缩/重启/steer 后仍沿原 Work 稳定推进，普通群聊与现有发送恢复合同通过相关回归。无法保证模型永远不遗漏细节、每次都主动分工或语义上完全正确；harness 必须提供可靠事实、反馈、恢复和核验能力，而不是把这些风险交给一条更强硬的提示词。
