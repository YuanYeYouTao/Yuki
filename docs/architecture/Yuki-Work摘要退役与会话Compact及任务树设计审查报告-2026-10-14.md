# Work 摘要退役、会话 Compact 与任务树设计审查报告

日期：2026-10-14。关联 [Issue #417](https://github.com/YuanYeYouTao/Yuki/issues/417)、[Issue #418](https://github.com/YuanYeYouTao/Yuki/issues/418)。

**最高开发约束：禁止防御性编程。能通过删除或放宽条件解决的问题，不新增替代机制；不把摘要、汇报、经验阈值和辅助状态写成任务完成门槛。** 注重可扩展性和可维护性，不把代码写死。真实身份、授权、来源和执行回执按事实处理，不借此扩大限制。共同规则见[开发约束](development-contract.md)。

**状态：源码调研、设计讨论和实施范围整理；尚未实施 #417/#418 的运行代码替换。** 本报告的提交不代表代码已删除、测试通过、部署完成或生产已省钱。后文 D、V、S 项均为待实施或待验收项。既有模块文档描述当前实现，本报告描述替换方向；实施时同步改写相关现行合同，不能把两套压缩规则同时保留。

## 1. 本次决定与范围

旧的独立 Work 摘要、ordinary 摘要链和 Conversation Rollup 一起退役。需要压缩时，统一采用 #418 的当前会话 compact：沿当前实际模型、消息、系统说明、工具合同和协议上下文产生可续接检查点，并让它真正替换所覆盖的旧输入。保存、等待、恢复和结束 Work 不额外购买摘要。

保留一份 Conversation 公共历史。Work 保存自己的原目标、追加要求、执行阶段、工具与投递回执、必要任务发现及产物引用。它们可以持久化，但持久化不等于每次把整份档案送给模型，也不等于把私有工具执行写成公共聊天。

保留既有 Work tree、原任务切换、等待、子任务结果回送和调度能力。任务树表示目标归属，串行、并行或等待表示执行方式。W1 暂停期间继续聊天，之后恢复 W1，是正确行为。删除的是重复摘要和冗余输入，不是这种能力。

用户在报告细化期间追加明确要求：**子 Agent 完整继承主 Agent 的所有能力，不保留子 Agent 专属能力限制。** 该要求与复杂 Work 的派生、等待、恢复、交付一起纳入附录 B，不只是把白名单加几个工具。主子共用完整能力合同，按真实来源、授权和回执执行；不因 worker 身份永久禁用 QQ、Memory 写入、自动化、插件或继续派子 Agent。

本轮只写报告并提交文档。没有修改生产数据库、取消 Work、发送 QQ 测试消息、调用付费模型或部署。后续实施需要一起完成 #417 和 #418，不能只调低阈值、关闭 Rollup worker、加开关，或把旧服务改名后继续使用。

## 2. 固定基线与证据边界

| 对象 | 本次核查基线 | 用途与边界 |
| --- | --- | --- |
| Yuki 当前 main | [`fb8823f53f2134e4029bc6f5f206e7eb87a468d1`](https://github.com/YuanYeYouTao/Yuki/commit/fb8823f53f2134e4029bc6f5f206e7eb87a468d1) | 以下当前源码索引固定于该提交；不是部署版本证明 |
| Pi 本地仓库 | `D:/Code/My_Github_Repo/pi`，[main `3daa20280f6a7c7cbe0935680c9a38fdc3863005`](https://github.com/earendil-works/pi/commit/3daa20280f6a7c7cbe0935680c9a38fdc3863005) | 本次讨论期间更新并核查 clean main；未安装依赖、构建或升级已安装 CLI |
| 历史 PR #228 | [PR 正文与文件](https://github.com/YuanYeYouTao/Yuki/pull/228)，merge `3d9e2733991fe00f7774ed83c2c4acf9eaafdb01` | 用于追溯原目的，不能直接作为当前实现或生产收益 |
| #417 样本 | [Issue 原文](https://github.com/YuanYeYouTao/Yuki/issues/417)中的实际请求记录 | 确认摘要输入与下一 Main 的正文重叠；附件不含响应用量及账单 |
| #418 | [当前 Issue 合同](https://github.com/YuanYeYouTao/Yuki/issues/418) | 当前模型、当前上下文、允许暂停、全链路退役；不是已实现状态 |

“源码有该路径”“测试覆盖该路径”“真实模型请求成功”“QQ 实际送达”“CI 通过”“提交或部署完成”是不同结论，分别记录。以下涉及当前链路的判断来自固定源码；没有将它们冒称为本轮线上验收。

## 3. PR #228 当初解决什么，哪些判断需要纠正

### 3.1 原目标是恢复时不让聊天倒退

[PR #228：Keep current chat history across Work resumes and compact context safely](https://github.com/YuanYeYouTao/Yuki/pull/228) 于北京时间 2026-10-03 05:47:32 合并。原任务书要求使用真实冻结聊天、恢复 W1 时接入当前群聊、把完整研究资料留在档案中按需回读，并接受为了有效信息失去部分缓存。普通聊天不强制创建 Work，观察笔记也不要求每次工具后调用模型生成。见[历史任务书开篇](https://github.com/YuanYeYouTao/Yuki/blob/3d9e2733991fe00f7774ed83c2c4acf9eaafdb01/docs/architecture/work-context-and-chat-continuation-taskbook.md#L13)。

其 [W1 → W2 → W1 示例](https://github.com/YuanYeYouTao/Yuki/blob/3d9e2733991fe00f7774ed83c2c4acf9eaafdb01/docs/architecture/work-context-and-chat-continuation-taskbook.md#L195)主要解决：W1 恢复不能回到旧群史，不能为了命中率永久保留无关研究资料。它并未证明把所有交叉 Work 轨迹常驻共享更便宜。

PR 同时引入笔记、观察与历史选取、业务恢复、普通轮压缩、工具资料回读、容量与协议修复，因此不能以整条 PR 回滚作为删除方案。按当前调用者划定范围，已经退役的机制不再次“修复”。

### 3.2 一万多行新增的实际组成

完整文件统计为 **121 个文件，新增 13,462 行、删除 949 行，净增 12,513 行**。以下使用完整分页文件数据，避免把只返回前 100 个文件的接口结果当成全量。

| 类别 | 文件数 | 新增 | 删除 |
| --- | ---: | ---: | ---: |
| `src/` 源码 | 48 | 4,434 | 616 |
| `tests/` 测试 | 53 | 6,455 | 292 |
| 文档：`docs/`、双 README、探测说明 | 16 | 962 | 41 |
| 手动探测工具 `.py` | 2 | 1,477 | 0 |
| 数据库迁移 | 1 | 131 | 0 |
| `.env.example` | 1 | 3 | 0 |

源码净增 3,818 行，不能把 13,462 行全部叫作运行时设计，也不能据此决定应删除多少。[PR 完整文件范围](https://github.com/YuanYeYouTao/Yuki/pull/228/files)。

### 3.3 后续已经发生的删除与任务树来源

| 变化 | 实际情况 | 本次处理 |
| --- | --- | --- |
| #228 的公共观察与 Work 笔记发布 | [PR #316](https://github.com/YuanYeYouTao/Yuki/pull/316)已清退公共 work-note、按 reader 的历史分叉与个人时区旧注入链 | 不恢复公共自动观察，不按人重建公共历史 |
| 私有付费观察与原件引用 | [PR #317](https://github.com/YuanYeYouTao/Yuki/pull/317)保留原付费记录、来源与引用事实；当前仍有私有消费者 | 新摘要生成链退役时核对实际 reader，不能把“已付记录”当垃圾清库 |
| 当前统一业务任务树 | 来自 [PR #286](https://github.com/YuanYeYouTao/Yuki/pull/286)，源提交 [`82bb386ae0251f55914b105605ee82f08f6f3d52`](https://github.com/YuanYeYouTao/Yuki/commit/82bb386ae0251f55914b105605ee82f08f6f3d52)，迁移 `0105` | 保留真实 `Work.parent_work_id`、祖先预算、取消和结果归属 |

早期工作者也有父关系，但把普通 Work 与工作者统一到业务树不是 #228 的工作。删除摘要不要求删除这棵树。[0105 迁移](https://github.com/YuanYeYouTao/Yuki/blob/fb8823f53f2134e4029bc6f5f206e7eb87a468d1/migrations/versions/0105_work_tree_and_retired_completion_policy.py#L13)。

### 3.4 旧缓存实验没有证明当前生产节省

旧实验的 110 次物理调用使用当时 64 个核心工具与合成前缀；部分摘要失败，真实图片/MCP 的 C07 未执行，Runner 恢复使用合成的已接受页，只有一次真实 Main 请求。上线观测窗口里只有四次 Dream，缓存字段为空，没有普通 Main 样本。它们不能代替当前工具合同、真实聊天或账单验收。

出处：[历史实验记录](https://github.com/YuanYeYouTao/Yuki/blob/3a16dc7d885659fbb5188dcc328dbebe5421e753/docs/operations/work-context-cache-experiment-2026-10-03.md)、[实施阶段交付快照](https://github.com/YuanYeYouTao/Yuki/blob/3a16dc7d885659fbb5188dcc328dbebe5421e753/docs/operations/work-context-delivery-2026-10-03.md)、[实际合并与部署后的 PR 评论](https://github.com/YuanYeYouTao/Yuki/pull/228#issuecomment-5962225448)。固定提交中的交付快照本身尚未记录实际上线，四次 Dream 的线上结论来自最后一个评论。

## 4. #417 重复摘要的事实与源码原因

### 4.1 真实样本说明什么

新短 Work 的记录中 `sequence=0`，`model_observations`、`effects`、`task_inputs`、`recent_task_inputs` 均为空，但仍先执行辅助摘要。

| 数据 | 实际观测 | 可得结论 |
| --- | --- | --- |
| 辅助摘要输入 | 552 条 records：system 1、user 314、assistant 237 | 不是 552 次模型调用 |
| 随后 Main | 378 个 contents：user 141、model 237；550 个 parts | Provider 的角色合并会改变容器数量 |
| 文本重叠 | Main 的 549 条正文逐字存在于摘要输入，另有一个摘要 part | 摘要读过大量后来仍原样发送的材料 |
| 请求声明的 `content-length` | 215,949 与 205,359 bytes | 系统说明、工具、编码不同，不能作为严格压缩率或 token 比例 |
| 响应用量、时延、账单 | 原附件没有 | 费用翻倍、缓存全失效、零命中都不能据此认定 |

这个样本属于后续 #417，不是 #228 的验收数据。`552−549` 也不表示成功压缩了三条记录。已经确定的问题是：多发一条辅助请求读取公共原文，下一条 Main 却继续保留这些原文，达不到“压缩后旧内容退出输入”的目标。[#417 原始证据](https://github.com/YuanYeYouTao/Yuki/issues/417)。

### 4.2 不是只改一句提示词

| 当前源码索引 | 当前行为 | 问题与处理方向 |
| --- | --- | --- |
| [WorkSession.summary_source，648–678](https://github.com/YuanYeYouTao/Yuki/blob/fb8823f53f2134e4029bc6f5f206e7eb87a468d1/src/qq_ai_bot/runtime/work_session.py#L648) | 从 `public_records()` 取来源，仅排除传入 preserved messages | 初始公共前缀没有被完整排除 |
| [TurnExecution，935–940](https://github.com/YuanYeYouTao/Yuki/blob/fb8823f53f2134e4029bc6f5f206e7eb87a468d1/src/qq_ai_bot/services/turn_execution.py#L935)；[AgentRunner，347–350](https://github.com/YuanYeYouTao/Yuki/blob/fb8823f53f2134e4029bc6f5f206e7eb87a468d1/src/qq_ai_bot/services/agent_runner.py#L347) | 向摘要器传入本轮追加的 `public_tail` | 不等于初始保留的整份共同历史 |
| [WorkSession.compact，1259–1370](https://github.com/YuanYeYouTao/Yuki/blob/fb8823f53f2134e4029bc6f5f206e7eb87a468d1/src/qq_ai_bot/runtime/work_session.py#L1259) | candidate 保留 anchor，再加入 `explicit_context_compaction` | 先概括初始群史，却没有让它退出候选 |
| [TurnExecution._finalize_initial_layout，737–783](https://github.com/YuanYeYouTao/Yuki/blob/fb8823f53f2134e4029bc6f5f206e7eb87a468d1/src/qq_ai_bot/services/turn_execution.py#L737) | 恢复后插入当前 Host/运行资料，更新 initial messages | 旧 anchor 不一定仍是当前序列的连续前缀 |
| [ordinary_compaction，51–55、153](https://github.com/YuanYeYouTao/Yuki/blob/fb8823f53f2134e4029bc6f5f206e7eb87a468d1/src/qq_ai_bot/services/ordinary_compaction.py#L51) | 另有排除 initial 的 tail 摘要，再保留初始 head | 是不同实现；同样属于 #418 合并退役范围 |

不能只用 `len(anchor)` 截断来源，Host 插入后 anchor 可能不连续；也不能只排除摘要来源却忘记新窗口是否真正替换旧内容。本次不继续修补三套摘要器，而是把调用、来源选择、替换和恢复一起收束到 #418。

## 5. Yuki 已有的持久化与结果恢复能力

### 5.1 保存 Work 不需要模型帮忙记住

| 已有能力 | 源码索引 | 本次决定 |
| --- | --- | --- |
| transcript、phase、source、工具调用、inputs、协议原件引用持久化 | [WorkSession，1517–1607](https://github.com/YuanYeYouTao/Yuki/blob/fb8823f53f2134e4029bc6f5f206e7eb87a468d1/src/qq_ai_bot/runtime/work_session.py#L1517)、[WorkJournal，376–514](https://github.com/YuanYeYouTao/Yuki/blob/fb8823f53f2134e4029bc6f5f206e7eb87a468d1/src/qq_ai_bot/runtime/work_journal.py#L376) | 保留；原件不必每轮全部注入 |
| `dispatched / response / paired` 分别持久 | [主运行合同](main-agent-runtime.md#所有者与恢复)、[WorkJournal，184–187](https://github.com/YuanYeYouTao/Yuki/blob/fb8823f53f2134e4029bc6f5f206e7eb87a468d1/src/qq_ai_bot/runtime/work_journal.py#L184) | 恢复用原 ID；响应未知不能盲目重购 |
| 真实工具结果及 receipt 保存 | [InvocationService，288–297](https://github.com/YuanYeYouTao/Yuki/blob/fb8823f53f2134e4029bc6f5f206e7eb87a468d1/src/qq_ai_bot/services/invocation_service.py#L288) | 发送成功不会被后来模型失败改成失败 |
| 业务恢复接入当前公共历史 | [WorkSession.rebase_business，400–447](https://github.com/YuanYeYouTao/Yuki/blob/fb8823f53f2134e4029bc6f5f206e7eb87a468d1/src/qq_ai_bot/runtime/work_session.py#L400) | 保留原目标与必要任务事实，退出旧群史副本 |
| 尚未观察的最后工具回合 | [WorkSession，449–517](https://github.com/YuanYeYouTao/Yuki/blob/fb8823f53f2134e4029bc6f5f206e7eb87a468d1/src/qq_ai_bot/runtime/work_session.py#L449) | 已有保留路径，不伪造结果或重新执行 |
| goal、追加要求、未覆盖输入、可选笔记、效果证据 | [WorkSession，519–568](https://github.com/YuanYeYouTao/Yuki/blob/fb8823f53f2134e4029bc6f5f206e7eb87a468d1/src/qq_ai_bot/runtime/work_session.py#L519) | 不购买独立 Work 摘要，不把 note 缺失设为暂停条件 |
| 有来源的可选 `context_note` | [work_context_note，125–169](https://github.com/YuanYeYouTao/Yuki/blob/fb8823f53f2134e4029bc6f5f206e7eb87a468d1/src/qq_ai_bot/runtime/work_context_note.py#L125)、[WorkControl，617–660](https://github.com/YuanYeYouTao/Yuki/blob/fb8823f53f2134e4029bc6f5f206e7eb87a468d1/src/qq_ai_bot/runtime/work_control.py#L617) | 可随正常输出留必要发现；不是每次结束前的额外模型调用 |

当前运行资料可以留在原已派发请求的 journal 里作证据，这不代表应再次成为未来业务知识。时钟、当前参与状态、无工作状态和临时身份包装不进入公共历史，也不作为新的语义 compact 材料反复积累。

### 5.2 “存了”不等于模型能按需读到

本次不能简单删掉全部旧工具结果，再声称 Agent 自己随时能查回来。已经找到以下缺口：

| 路径 | 当前事实 | 后续最小处理 |
| --- | --- | --- |
| `task_control.get` | [work_queries，46–72、103–128](https://github.com/YuanYeYouTao/Yuki/blob/fb8823f53f2134e4029bc6f5f206e7eb87a468d1/src/qq_ai_bot/runtime/work_queries.py#L46)返回目标、状态、关系、时间、预算/等待等元数据，不返回通用 journal 或所有结果正文 | 不能把查询状态描述成通用结果回读 |
| `WorkJournal.effect_result` | [554–600](https://github.com/YuanYeYouTao/Yuki/blob/fb8823f53f2134e4029bc6f5f206e7eb87a468d1/src/qq_ai_bot/runtime/work_journal.py#L554)有内部恢复 reader | 不等于已有对模型公开的任意 Work 结果工具 |
| `readonly_result_keys` | [WorkSession，77–80](https://github.com/YuanYeYouTao/Yuki/blob/fb8823f53f2134e4029bc6f5f206e7eb87a468d1/src/qq_ai_bot/runtime/work_session.py#L77)、[AgentRunner，1129–1144](https://github.com/YuanYeYouTao/Yuki/blob/fb8823f53f2134e4029bc6f5f206e7eb87a468d1/src/qq_ai_bot/services/agent_runner.py#L1129)是 activation 内的复用缓存 | 不能当成跨重启完整查询索引 |
| 搜索/网页 artifact | [results，198–208](https://github.com/YuanYeYouTao/Yuki/blob/fb8823f53f2134e4029bc6f5f206e7eb87a468d1/src/qq_ai_bot/capabilities/results.py#L198)在已配置存储时会归档非空搜索/网页结果；其他小结果不保证都有 artifact | 不假设任何结果都有句柄 |
| artifact handle 可达性 | [InvocationService，294](https://github.com/YuanYeYouTao/Yuki/blob/fb8823f53f2134e4029bc6f5f206e7eb87a468d1/src/qq_ai_bot/services/invocation_service.py#L294)把 handle 存在 receipt 顶层；[effect_evidence，2670–2674](https://github.com/YuanYeYouTao/Yuki/blob/fb8823f53f2134e4029bc6f5f206e7eb87a468d1/src/qq_ai_bot/runtime/work_repository.py#L2670)把完整 receipt 交给 `historical_evidence` | [effect_outcomes 的该转换](https://github.com/YuanYeYouTao/Yuki/blob/fb8823f53f2134e4029bc6f5f206e7eb87a468d1/src/qq_ai_bot/runtime/effect_outcomes.py#L246)取 outcome 等选定事实，返回时未合并 receipt 顶层 handle；文件存在也可能无从告诉模型怎么读 |

复用 `read_tool_artifact`、`workspace_read`、`terminal_read(run_id)` 等现有读取能力，保留必要发现或把原句柄交回已有恢复材料。缺口沿真实调用者补通，不新造任务记忆管理器、全 journal 注入器或第二套观察历史。读取工具会增加后续模型轮次，成本对照必须计入这些轮次。

这与子 Work 结果回送是两回事。普通子 Work 与工作者的终态通知已有结果内容，不应把“通用查询拿不到任意旧结果”误报成“子任务从来不会通知父任务”。

## 6. Work tree、任务切换与结果回送

### 6.1 三种请求必须分清

| 实际用户/Agent 行为 | 正确归属 | 当前依据 |
| --- | --- | --- |
| “继续 W1，把文件内容改成 B” | 原 W1 的新输入，原预算和来源 | [Chat，638–693](https://github.com/YuanYeYouTao/Yuki/blob/fb8823f53f2134e4029bc6f5f206e7eb87a468d1/src/qq_ai_bot/services/chat.py#L638)、[WorkControl，842–868](https://github.com/YuanYeYouTao/Yuki/blob/fb8823f53f2134e4029bc6f5f206e7eb87a468d1/src/qq_ai_bot/runtime/work_control.py#L842) |
| W1 拆出一个为 W1 服务的 W2 / 派子 Agent | 子目标，真实 `parent_work_id` | [WorkControl.derive，781–801](https://github.com/YuanYeYouTao/Yuki/blob/fb8823f53f2134e4029bc6f5f206e7eb87a468d1/src/qq_ai_bot/runtime/work_control.py#L781)、[WorkRepository.derive，458–499](https://github.com/YuanYeYouTao/Yuki/blob/fb8823f53f2134e4029bc6f5f206e7eb87a468d1/src/qq_ai_bot/runtime/work_repository.py#L458)、[SubagentRepository，72–83](https://github.com/YuanYeYouTao/Yuki/blob/fb8823f53f2134e4029bc6f5f206e7eb87a468d1/src/qq_ai_bot/runtime/subagent_repository.py#L72) |
| 做 W1 时收到另一个无关请求 W2 | 两个独立根；是否让出/排队是调度问题 | [WorkControl._queue_work，962–1006](https://github.com/YuanYeYouTao/Yuki/blob/fb8823f53f2134e4029bc6f5f206e7eb87a468d1/src/qq_ai_bot/runtime/work_control.py#L962)，`accept` 的 parent 默认空；handoff 不替无关目标建立父关系 |

只有真实共同父目标，节点才是兄弟。Conversation 不是必须存在的常驻父 Work，聊天也不应被强制接纳为任务。[现有普通轮处理](https://github.com/YuanYeYouTao/Yuki/blob/fb8823f53f2134e4029bc6f5f206e7eb87a468d1/src/qq_ai_bot/runtime/work_control.py#L803)。

强行把所有后来请求变成 W1 的孩子，会改变祖先累计预算、取消范围和授权来源，不能以“看起来更像任务树”为理由这样做。[祖先预算](https://github.com/YuanYeYouTao/Yuki/blob/fb8823f53f2134e4029bc6f5f206e7eb87a468d1/src/qq_ai_bot/runtime/work_budget.py#L21)、[子树管理](https://github.com/YuanYeYouTao/Yuki/blob/fb8823f53f2134e4029bc6f5f206e7eb87a468d1/src/qq_ai_bot/runtime/work_management.py#L125)。

### 6.2 W1 → W2 → W1 的含义与现有调度

它表达“W1 让出执行，处理别的事，后来继续 W1”，不是必须建两份公共历史，也不是强制并发。原 handoff 检查点与恢复已有路径：[WorkRepository，435–454](https://github.com/YuanYeYouTao/Yuki/blob/fb8823f53f2134e4029bc6f5f206e7eb87a468d1/src/qq_ai_bot/runtime/work_repository.py#L435)、[WorkControl，536–549](https://github.com/YuanYeYouTao/Yuki/blob/fb8823f53f2134e4029bc6f5f206e7eb87a468d1/src/qq_ai_bot/runtime/work_control.py#L536)、[WorkSupervisor，212–216](https://github.com/YuanYeYouTao/Yuki/blob/fb8823f53f2134e4029bc6f5f206e7eb87a468d1/src/qq_ai_bot/runtime/work_supervisor.py#L212)。

同一 Conversation Main 沿原租约与 in-flight 调度顺序执行，父子关系不会自动获得第二条并发 Main。子工作者有各自的 lease，可以由现有工作者调度器并行。见 [Main 租约](https://github.com/YuanYeYouTao/Yuki/blob/fb8823f53f2134e4029bc6f5f206e7eb87a468d1/src/qq_ai_bot/runtime/work_repository.py#L137)、[WorkScheduler，267–327](https://github.com/YuanYeYouTao/Yuki/blob/fb8823f53f2134e4029bc6f5f206e7eb87a468d1/src/qq_ai_bot/runtime/work_scheduler.py#L267)、[worker lease](https://github.com/YuanYeYouTao/Yuki/blob/fb8823f53f2134e4029bc6f5f206e7eb87a468d1/src/qq_ai_bot/runtime/subagent_repository.py#L124)、[worker 调度](https://github.com/YuanYeYouTao/Yuki/blob/fb8823f53f2134e4029bc6f5f206e7eb87a468d1/src/qq_ai_bot/runtime/subagent_scheduler.py#L105)。

### 6.3 子结果和等待不是本次要重写的缺失功能

| 路径 | 已有行为 | 源码索引 |
| --- | --- | --- |
| 普通派生 Work 终态 | 给父 inbox 写 `kind=completion`，带原 child/state/reason/result；稳定结果键含 child ID 与 revision | [WorkRepository，788–820](https://github.com/YuanYeYouTao/Yuki/blob/fb8823f53f2134e4029bc6f5f206e7eb87a468d1/src/qq_ai_bot/runtime/work_repository.py#L788) |
| 工作者终态 | 单独写 worker-result 通知原父输入 | [SubagentRepository.finish，545–621](https://github.com/YuanYeYouTao/Yuki/blob/fb8823f53f2134e4029bc6f5f206e7eb87a468d1/src/qq_ai_bot/runtime/subagent_repository.py#L545) |
| 父收到 completion | 渲染 Work 信号，继续原任务 | [WorkControl，472–518](https://github.com/YuanYeYouTao/Yuki/blob/fb8823f53f2134e4029bc6f5f206e7eb87a468d1/src/qq_ai_bot/runtime/work_control.py#L472) |
| 父等待外部且无其他 active wait | completion 可在同次提交把父入队 | [WorkRepository，822–831](https://github.com/YuanYeYouTao/Yuki/blob/fb8823f53f2134e4029bc6f5f206e7eb87a468d1/src/qq_ai_bot/runtime/work_repository.py#L822) |
| 显式等子 Work ID | 核实际后代、登记等待，子终态信号匹配后唤醒原父 | [WorkControl，893–904](https://github.com/YuanYeYouTao/Yuki/blob/fb8823f53f2134e4029bc6f5f206e7eb87a468d1/src/qq_ai_bot/runtime/work_control.py#L893)、[WorkWait，698–771](https://github.com/YuanYeYouTao/Yuki/blob/fb8823f53f2134e4029bc6f5f206e7eb87a468d1/src/qq_ai_bot/runtime/work_wait.py#L698) |

`derive` 本身不等于自动登记 wait；其他未满足的真实等待不会被 completion 随意清掉，suspended 或终态父也不自动复活。`background_state` 主要描述实际 worker，不能据此认定普通派生结果丢失。以上是源码事实，本轮未运行新的群聊验收。

## 7. Pi durable 实际做法与可借鉴边界

Pi 的普通 coding-agent CLI 与 durable runtime 分开研究。本报告比较的是 durable 的持久 entry、模型 view、任务调度与检查点；不把 CLI JSONL 行为或实验模块当成 Yuki QQ 生命周期的完整答案。

普通 CLI 同样保存 JSONL 原 entry，模型窗口采用最新 compact 与保留尾。[窗口投影](https://github.com/earendil-works/pi/blob/3daa20280f6a7c7cbe0935680c9a38fdc3863005/packages/coding-agent/src/core/session-manager.ts#L476)、[原件保存](https://github.com/earendil-works/pi/blob/3daa20280f6a7c7cbe0935680c9a38fdc3863005/packages/coding-agent/src/core/session-manager.ts#L1172)。durable 的主要差异是执行阶段和 owned task 的持久恢复，不是天然更省输入或免费。

### 7.1 原件、窗口、执行状态分离

| Pi 能力 | 固定源码 | 对 Yuki 的启示 |
| --- | --- | --- |
| immutable entry、head、edits 与持久事务 | [types，332–379](https://github.com/earendil-works/pi/blob/3daa20280f6a7c7cbe0935680c9a38fdc3863005/packages/durable/src/types.ts#L332)、[context，67–188](https://github.com/earendil-works/pi/blob/3daa20280f6a7c7cbe0935680c9a38fdc3863005/packages/durable/src/harness/context.ts#L67) | 原件保存和模型当前能看到什么，不是同一件事 |
| generation / tool task 检查点 | [generation，52–93、177–221](https://github.com/earendil-works/pi/blob/3daa20280f6a7c7cbe0935680c9a38fdc3863005/packages/durable/src/harness/generation.ts#L52) | 记录阶段、输入切面和真实结果，不靠每次摘要恢复 |
| 已完成工具与嵌套 task 的稳定 key 复用 | [tool，156–185、360–387、601–731](https://github.com/earendil-works/pi/blob/3daa20280f6a7c7cbe0935680c9a38fdc3863005/packages/durable/src/harness/tool.ts#L156) | 学结果复用；不把恢复当重新执行 |
| 保存工具输出 | [tool，491–548、830–853](https://github.com/earendil-works/pi/blob/3daa20280f6a7c7cbe0935680c9a38fdc3863005/packages/durable/src/harness/tool.ts#L491) | 已经经过输出限额，不是无限原始外部资料库；不能夸大可回读完整性 |

Yuki 的原 journal、ProtocolStore、业务输入、artifact 与公共事件已经承担相近职责。应复用它们，不再叠加 entry 管理器、历史同步器或另一套任务档案。

### 7.2 Pi compact 真正替换 view，但仍另发摘要请求

Pi 从当前 conversation model view 选旧前缀，保存 `tail / firstKept / model / options`，之后按选定尾重读来源。摘要请求实际是专用 summary system 加序列化历史 user，再调用 `completeSimple`；代码带 `cacheRetention="none"`，但这不证明每个实际 Provider 的隐式缓存都被关闭。见 [compaction，115–213](https://github.com/earendil-works/pi/blob/3daa20280f6a7c7cbe0935680c9a38fdc3863005/packages/durable/src/harness/compaction.ts#L115)。

产物追加为 `pi.compaction(head=firstKept)`，下一 model view 使用最新 head 的贡献、保留区间与后来条目。原 entry 不被覆盖或删除；再次 compact 把有效旧摘要纳入一个新 head，不让多个旧摘要的贡献无限常驻。见 [compaction，417–446](https://github.com/earendil-works/pi/blob/3daa20280f6a7c7cbe0935680c9a38fdc3863005/packages/durable/src/harness/compaction.ts#L417)、[context，117–147、250–252](https://github.com/earendil-works/pi/blob/3daa20280f6a7c7cbe0935680c9a38fdc3863005/packages/durable/src/harness/context.ts#L117)。

可借鉴的是“原件不动、有效窗口真正替换”。不能照搬 dedicated summary 调用：不新建 summary conversation，也仍可能是独立 summary request，违反 #418 选择的当前 Main 实际上下文合同。

### 7.3 不照搬 Pi 的限制、补造与未知重发

| Pi 细节 | 风险与本次决定 |
| --- | --- |
| 不从 tool result 中间切，检查 user 切点之后是否仍有前一 assistant 的结果 | 可借鉴真实协议完整边界。[compaction，256–306](https://github.com/earendil-works/pi/blob/3daa20280f6a7c7cbe0935680c9a38fdc3863005/packages/durable/src/harness/compaction.ts#L256) |
| view 中补 missing-result 文本、丢弃不匹配结果 | 不照搬。Yuki 必须保留真实配对，不能伪造工具结果。[context，255–293](https://github.com/earendil-works/pi/blob/3daa20280f6a7c7cbe0935680c9a38fdc3863005/packages/durable/src/harness/context.ts#L255) |
| 固定摘要模板、工具文本 2000 字符截断、clean-stop 分类、背景/阻塞整理策略 | 不搬成新业务规则或摘要成功门槛。[compaction，357–399](https://github.com/earendil-works/pi/blob/3daa20280f6a7c7cbe0935680c9a38fdc3863005/packages/durable/src/harness/compaction.ts#L357) |
| stale head 主要防止新 head 退回有效区间之前 | 不足以证明摘要来源仍有效；Pi 自己提示异步选源后的编辑或遗忘可能被旧摘要带回。[inbox，89–109](https://github.com/earendil-works/pi/blob/3daa20280f6a7c7cbe0935680c9a38fdc3863005/packages/durable/src/harness/inbox.ts#L89)、[spec，5203–5212](https://github.com/earendil-works/pi/blob/3daa20280f6a7c7cbe0935680c9a38fdc3863005/packages/durable/docs/spec.md#L5203) |
| compact 响应返回后才在同一 commit 保存 usage、placement、terminal | commit 前崩溃仍可能回到 summarize 再付费；不是已付请求恰好一次。[compaction，164–213](https://github.com/earendil-works/pi/blob/3daa20280f6a7c7cbe0935680c9a38fdc3863005/packages/durable/src/harness/compaction.ts#L164) |
| generation 的 request 阶段恢复可再次发 `streamResponse` | 不能移植成 Yuki unknown-paid 自动重试；只有已有 deferred handle 另走 poll。[generation，196–221、458–476](https://github.com/earendil-works/pi/blob/3daa20280f6a7c7cbe0935680c9a38fdc3863005/packages/durable/src/harness/generation.ts#L196) |

Yuki 复用已有来源、generation、隐私与 owner 核验。已取得响应先沿原 journal 保存，用原响应重备数据库发布，不重新购买；新聊天只接入未覆盖尾部，不因数量增长使原 compact 无效。没有可查询回执的未知响应继续记为未知，不新造补发器。

### 7.4 Pi 的任务树与用户业务 Work 不是同一层

Pi durable task owner 可以是 conversation 或 parent task，fork parent 与执行 owner 也不同。内置 generation/tool/compaction 是执行节点；`taskGraph` 只列 live tasks，terminal 后移除，不是完整业务任务档案。[types，273–313](https://github.com/earendil-works/pi/blob/3daa20280f6a7c7cbe0935680c9a38fdc3863005/packages/durable/src/types.ts#L273)、[registry](https://github.com/earendil-works/pi/blob/3daa20280f6a7c7cbe0935680c9a38fdc3863005/packages/durable/src/harness/registry.ts#L1)、[task-graph，171–180](https://github.com/earendil-works/pi/blob/3daa20280f6a7c7cbe0935680c9a38fdc3863005/packages/durable/src/harness/task-graph.ts#L171)。

前台 subagent 示例在子 conversation 执行，默认复制 Agent 配置而非整份父 transcript；需要历史时才显式 fork。等待可以等其他已有 task，fail-fast 才约束直接孩子。子任务不能简单切换为 background；后台示例使用立即完成的 anchor 与 reporter，是示例组合，不要求每个会话建立一个永久父任务。见 [前台示例](https://github.com/earendil-works/pi/blob/3daa20280f6a7c7cbe0935680c9a38fdc3863005/packages/durable/test/examples/22-subagent-foreground.ts#L48)、[后台示例](https://github.com/earendil-works/pi/blob/3daa20280f6a7c7cbe0935680c9a38fdc3863005/packages/durable/test/examples/23-subagent-background.ts#L56)、[background 限制](https://github.com/earendil-works/pi/blob/3daa20280f6a7c7cbe0935680c9a38fdc3863005/packages/durable/src/session/transaction.ts#L389)、[等待实现](https://github.com/earendil-works/pi/blob/3daa20280f6a7c7cbe0935680c9a38fdc3863005/packages/durable/src/harness/scheduler.ts#L1472)。

Pi 的 steer 在 postTools 接入原 generation，followUp 等最终回答；它没有等同 Yuki 的逐业务任务暂停 API。close/reopen 保留 checkpoint，abort 则终止当前执行，不等同 #418 只暂停当前会话作 compact。[inbox，63–109](https://github.com/earendil-works/pi/blob/3daa20280f6a7c7cbe0935680c9a38fdc3863005/packages/durable/src/harness/inbox.ts#L63)、[scheduler，288–329](https://github.com/earendil-works/pi/blob/3daa20280f6a7c7cbe0935680c9a38fdc3863005/packages/durable/src/harness/scheduler.ts#L288)。

### 7.5 其他持久化系统的共同点

[Temporal 的事件历史](https://docs.temporal.io/encyclopedia/event-history)记录执行事实，signal/update 把真实新输入交给原 workflow；[child workflow](https://docs.temporal.io/develop/python/workflows/child-workflows)可以启动后继续工作、稍后等待，也可以直接等待。父子关系与并发方式依然是两件事，不需要把无关请求强制挂成孩子。[消息传递说明](https://docs.temporal.io/encyclopedia/workflow-message-passing)。

[LangGraph subgraphs](https://docs.langchain.com/oss/python/langgraph/use-subgraphs)允许在命名空间内保留私有状态，并用输入输出映射交流；[checkpointer](https://docs.langchain.com/oss/python/langgraph/checkpointers)可保留已完成节点写入。它的 [interrupt](https://docs.langchain.com/oss/python/langgraph/interrupts)恢复会从节点逻辑重新进入，不能推导任意外部效果自动恰好一次。

这些架构支持“执行档案、模型窗口、归属分开处理”的方向，不证明 Yuki 应引入它们的整套框架或重建任务树。

## 8. #418 的目标设计：一个入口，沿原 owner 换窗

### 8.1 公共历史、当前资料、任务私有资料的布局

示意中的 H 是当前会话共同公共窗口，W 是原任务必要材料，R 是本次运行资料。它们是现有资料的含义，不是新数据库类型或新管理器。

```text
普通 Main：固定系统/完整工具 → H_now → R_current → 当前用户消息/触发
继续 W1：固定系统/完整工具 → H_now → 必要 W1 材料 → R_current → 当前触发
同次执行中的工具回合：沿实际协议有序追加原 call/result 与新输入
```

公共历史不按当前说话人、actor 或 Work 重渲染；不同入口的真实触发和私有资料不同，不代表可以改写较早的公共事件。R 有清晰边界，位于当前正文上方，不进入公共历史或未来语义摘要。保存的真实派发请求仍作原件证据。

聊天、SELF、自动化、SDK、插件与子工作沿真实主体、授权和工具合同进入；不能因为统一 compact 复制父私有资料。用户后续明确要求增强子 Agent 能力，见附录 B：工具范围按真实授权扩展，删除仅因 worker 身份施加的永久禁令。长期 Memory 仍通过原工具按需读写，不把 compact 产物自动写成记忆。

### 8.2 手动与自动使用相同 Main 维护路径

1. 在当前会话已有执行边界固定实际输入与覆盖切面：真实来源尾、当前 owner、系统/工具合同、模型 Profile、协议状态及本次输出空间。
2. 只暂停该会话的下一次正常推理和工具推进。其他会话继续；新输入正常落原账本，按真实顺序待接续，不重新 accept 原 Work。
3. 沿当前 Provider/Profile/model 和实际消息顺序追加可信 compact 要求。不把历史改装成一个 `records` user 包，不另建汇总 Agent，不绕开完整工具合同另发空工具冷读请求。
4. 原模型响应和实际 usage 沿既有 journal/owned Run 持久保存，再发布该 owner 的有效窗口。DB 发布失败只重备原数据库变更，提交确认不明先核原 ID。
5. 新窗口使用有效 compact 检查点、确需保留的近期原文、未处理输入和精确协议状态。已覆盖原文退出模型窗口；旧原件继续留在原账本与档案。
6. compact 之后接续未覆盖的新输入。失败不回到 Rollup worker、overlay、旧 ready job，也不抹掉已成功的外部效果。

自动触发依据完整最终请求、实际模型容量和本次输出预留，包含 system、完整工具、原生工具、消息、媒体及协议内容。不要用“群史超过某经验水位”替新短 Work 再买一份未替换原文的摘要；不新增按轮数、时长、固定压缩率或缓存命中率触发的整理任务。

手动 `/compact` 也不以先创建 Work 为条件。普通轮无已接纳 Work 时，使用已有 Conversation/owned Run 保存维护响应；具体落点实施时沿当前调用者确认，不虚构一个业务任务兜底。

### 8.3 当前协议续跑与稍后业务恢复是两个切面

**同一 W1 活跃执行中的 compact：** 当前窗口可能是 `H_old + W1_private + tools`。所得 `C_mix` 只属于 W1 原执行，用它与必要尾部继续当前协议阶段。不能把混合产物提升为全群公共摘要，即使模型按要求标了“公共/私有”。模型标签不授予发布权限。

**W1 暂停后再次业务恢复：** 使用 `H_now + 必要 W1 原任务事实`。不能同时注入整个 `C_mix` 和 H_now，因为 C_mix 已含旧 H，这会再次形成公共摘要与含公共历史的私有摘要双重常驻。必要任务事实来自 goal、追加输入、原 journal、回执、产物与可选已有 note；不再购买独立 Work 摘要。

**普通公共会话的 compact：** 只有合法公共来源形成的产物才能成为共同公共检查点。统一的是入口、调用与发布骨架，不是把每个混合请求的结果都共享给所有人。

需要检查仍未被消费的精确工具回合、不可分割 opaque 如何继续；不能只删除全部尾部、让模型猜进度，也不能为此恢复旧完整群史副本。

### 8.4 原生能力与协议约束

原生 compaction 只有在当前连接实际支持时才接入现有适配边界；其他协议沿当前模型上下文总结。不得为了压缩按内容切换 Provider。opaque/encrypted state 是原协议、原 owner 的续跑资料，不能当通用可移植摘要跨 Provider 发布。

工具调用与结果保留真实配对、原 ID、签名、原生 continuation、已付检查点。pending 时的合法换窗边界由实际协议确定，不补造结果，不伪造真人“继续”消息。当前 runtime 的语义来源排除与原生不可检查 opaque 的可分割性，需要真实路线验证；不能声称任何 opaque 内的临时内容都能任意摘除。

#418 引用的公开 compact 行为和 API 能力，不证明 Claude Code、Codex 每个版本的内部请求与缓存方式。Pi 同样不能证明 Yuki 当前网关支持这些能力。本报告采用 #418 明确选择的合同，不套用不透明内部实现。

## 9. 删除与同步修改清单

以下均为**待实施**。行号固定于 §2 基线，实施前随 main 复核。沿实际 import、调用、存储及测试追到最后一个消费者；表中未列出的调用者同样需清退。保留历史迁移与有独立证据的历史报告，不等于保留兼容运行链。

| 编号 | 当前入口与代码索引 | 删除或重写的内容 | 应复用/保留的部分 |
| --- | --- | --- | --- |
| D01 | [Rollup service，200–267](https://github.com/YuanYeYouTao/Yuki/blob/fb8823f53f2134e4029bc6f5f206e7eb87a468d1/src/qq_ai_bot/conversation/rollup/service.py#L200)、同目录 worker/summary/renderer/models/coverage/holds/signals | 删除 dedicated summary 请求、envelope、分页 job、coverage/hold、schema、overlay 与 worker 算法 | 公共原始事件；新的有效 compact 窗口 |
| D02 | [Rollup repository，494 起](https://github.com/YuanYeYouTao/Yuki/blob/fb8823f53f2134e4029bc6f5f206e7eb87a468d1/src/qq_ai_bot/conversation/rollup/repository.py#L494)及真实调用者 | 拆除旧 Rollup 仓储、计数、水位、任务与租约 | 同文件 `ConversationScopeRepository` 是 canonical 身份/generation 真消费者，不能整文件误删；沿现有身份归属整理 |
| D03 | [canonical_rollup，32、68](https://github.com/YuanYeYouTao/Yuki/blob/fb8823f53f2134e4029bc6f5f206e7eb87a468d1/src/qq_ai_bot/conversation/canonical_rollup.py#L32)、[scoped_event_uow，418、1043](https://github.com/YuanYeYouTao/Yuki/blob/fb8823f53f2134e4029bc6f5f206e7eb87a468d1/src/qq_ai_bot/persistence/scoped_event_uow.py#L418) | 删除入账后的 Rollup signal/counter/job 更新、drain 及 notifier | 真实事件、来源、会话身份、原事务边界 |
| D04 | [ConversationModule，130–148、274–277](https://github.com/YuanYeYouTao/Yuki/blob/fb8823f53f2134e4029bc6f5f206e7eb87a468d1/src/qq_ai_bot/application/modules/conversation.py#L130)、应用容器/持久装配/运行生命周期 | 删除 Rollup 构造、启动/关闭、并发、轮询、lease/heartbeat、重试与专用 health | 现有 Main 生命周期；Memory EvidenceCompactionWorker 是不同功能，不能跟名删除 |
| D05 | [context_preparation，77–138](https://github.com/YuanYeYouTao/Yuki/blob/fb8823f53f2134e4029bc6f5f206e7eb87a468d1/src/qq_ai_bot/runtime/context_preparation.py#L77)、[rollup_wakeup，43–122](https://github.com/YuanYeYouTao/Yuki/blob/fb8823f53f2134e4029bc6f5f206e7eb87a468d1/src/qq_ai_bot/services/rollup_wakeup.py#L43) | 删除 ContextRollupRequired、defer/finish、ready/stamp 轮询、旧准备水位等待 | 原业务等待与本次会话 compact 的执行边界，不新增第二套唤醒器 |
| D06 | WorkRepository 的 context_rollup checkpoint、WorkScheduler 的 wake_rollups、Chat/SELF/automation/plugin/SDK 准备调用 | 删除旧 job 等待、旧恢复分支和来源专属调度耦合 | 各真实入口、原 Work/source/run 及事件接续；停止旧等待不取消业务任务 |
| D07 | [ContextAssembler](https://github.com/YuanYeYouTao/Yuki/blob/fb8823f53f2134e4029bc6f5f206e7eb87a468d1/src/qq_ai_bot/services/context_assembler.py)、[HistoryProjection](https://github.com/YuanYeYouTao/Yuki/blob/fb8823f53f2134e4029bc6f5f206e7eb87a468d1/src/qq_ai_bot/services/history_projection.py)、[PromptComposer](https://github.com/YuanYeYouTao/Yuki/blob/fb8823f53f2134e4029bc6f5f206e7eb87a468d1/src/qq_ai_bot/services/prompt_composer.py) | 重写 rollup_text/selected-summary/raw-tail 选择、覆盖与替换；清退旧读版本/epoch 原因 | 一个 Conversation 的真实冻结公共历史；当前资料只当前注入；新 checkpoint 的实际覆盖 |
| D08 | [AgentRunner._compact_work，274 起](https://github.com/YuanYeYouTao/Yuki/blob/fb8823f53f2134e4029bc6f5f206e7eb87a468d1/src/qq_ai_bot/services/agent_runner.py#L274)、[WorkSession.summary_source，648 起](https://github.com/YuanYeYouTao/Yuki/blob/fb8823f53f2134e4029bc6f5f206e7eb87a468d1/src/qq_ai_bot/runtime/work_session.py#L648)、compact 1259 起 | 删除独立 Work 摘要生成、旧分页 staging、覆盖类别/结构门槛及循环触发 | 原 goal/input、journal、真实 paid response/refs/opaque 的读取与恢复 |
| D09 | [ordinary_compaction，35、191 起](https://github.com/YuanYeYouTao/Yuki/blob/fb8823f53f2134e4029bc6f5f206e7eb87a468d1/src/qq_ai_bot/services/ordinary_compaction.py#L35)、[TurnExecution，902–1058](https://github.com/YuanYeYouTao/Yuki/blob/fb8823f53f2134e4029bc6f5f206e7eb87a468d1/src/qq_ai_bot/services/turn_execution.py#L902) | 删除普通轮独立 summary dispatch、旧 soft 水位/staging 旁路，与 D08 统一 | 实际最终请求容量与当前 Main dispatch/journal；普通轮不强制 accept |
| D10 | [work_compaction，14 起](https://github.com/YuanYeYouTao/Yuki/blob/fb8823f53f2134e4029bc6f5f206e7eb87a468d1/src/qq_ai_bot/runtime/work_compaction.py#L14)、work_context_note 的 import 与字段 | 删除旧 CompactionSummary、validate_summary 及无消费者生成格式 | `SourcedFact` 仍被 WorkContextNote 使用，不能只按文件名整删；付费原件仍可读 |
| D11 | §5.2 的效果结果、artifact_handle、tool result、Work/private observation consumers | 删除重复文本镜像、旧摘要自动注入与无用包装；补通必要结果发现/句柄现有读取路径 | 真实 source、产物引用、未知/已成功回执；不要通过重查外网补旧结果 |
| D12 | ModelTask.CONVERSATION_COMPACTION、Profile/policy/admin 配置、.env/model_profiles/deployment_setup/frontend | 删除独立摘要任务路由、Rollup 输出/超时/worker/批次/等待字段及 UI 展示；同步处理旧配置 | 当前 Main 的真实 Provider/Profile 容量；不创建新的摘要 provider 类别 |
| D13 | 数据库旧 Rollup 表、专属列/索引/引用、旧 context_rollup/staging 存储 | 用向前迁移清退 rollup/jobs/emergency_overlays 与失效等待；转存合法仍有用的原检查点 | 原聊天账本、有效 Work/预算/回执/付费对象；历史迁移继续可从旧生产库升到新 head |
| D14 | 旧 CLI/命令/API、health 与运维探针 | 删除 job 队列控制、旧 ready 状态和无消费者默认产物入口；接入同一手动 compact | 真实状态与原调用查询，不新增强制 QQ 广播 |
| D15 | README/README.en、开发约束、main-agent/conversation-rollup/provider/worker 文档、.env/配置说明、升级/运维 | 重写当前合同与用户说明，清理旧“独立摘要/后台整理/分页恢复”建议 | 有独立证据的历史实验按版本保留并标过时，不覆盖历史事实 |
| D16 | 旧 Rollup/Work/ordinary 测试、fixture、tools/cache_probe.py/runtime.py、CI 引用 | 删除只维持退役实现的断言/脚本，改真实入口、payload、向前迁移与结果复用验证 | 通用质量 CI；不删除所有 journal、身份、预算或协议测试，不增加固定缓存命中门槛 |

`ConversationScopeRepository`、MemoryEvidenceCompactionWorker、SourcedFact、ProtocolStore、私有 paid observation 及 Work tree 是明确的误删风险点。它们的真实职责需保留或按现有 owner 移动，不能为了 Rollup 引用归零丢失业务能力。

迁移不能改写冻结的旧 migration，也不能以 `/ai new`、清库、取消所有 Work 或从备份恢复旧账本代替升级。原已付摘要可以作为原执行档案读到，但新代码不得重新启动旧分页算法、旧 worker 或隐藏 fallback。

退旧表之前，同步迁移仍引用 Rollup/overlay 的已付 source manifest 与读取依赖，保留原来源、指纹和真实撤权事实。当前 [WorkSourceGuard，197–285](https://github.com/YuanYeYouTao/Yuki/blob/fb8823f53f2134e4029bc6f5f206e7eb87a468d1/src/qq_ai_bot/runtime/work_source_guard.py#L197)仍可能读取旧 semantic/overlay 表，并把摘要 tuple 纳入 fingerprint；只保留 opaque/journal 却删掉其 source 依赖，不能保证旧请求恢复。也不能放宽 guard 让已经失效的来源假装有效。保留原事实的升级读取不允许再派发旧摘要。

### 9.1 需要顺着调用清退的精确索引

下表 `S/` 表示 `src/yuki/`。数字是固定基线的源码行号，不是新设计要求的阈值。与 D01–D16 合并执行。

| 对应项 | 精确消费者索引 | 核查要点 |
| --- | --- | --- |
| D01–D02 | `S/conversation/rollup/models.py:13–35,60,75–149`；`repository.py:494–520,698,784,854,1078,1114,1398–1437,1558,1671`；`service.py:60–415`；`worker.py:33–257`；`coverage.py:21–57`；`holds.py:10`；`metrics.py:9`；`origins.py:6`；`summary.py:26–122`；`signals.py:7–18`；`errors.py:4–20` | canonical scope 类型与 reader 移到其真实归属；旧算法、claim 与 writer 退役 |
| D01、D07 | `S/conversation/rollup/prompt_accounting.py:23,47,66,89–133`；`renderer.py:23,50,62,66,87,109,141`；`S/persistence/repository_helpers.py:27` | 保留消息正文、内部 ID、时间、AT/引用及可见性语义，只删旧 Rollup 会计/包装 |
| D03 | `S/identity/canonical_uow.py:24–25,66–70,277–279`；`S/persistence/scoped_event_uow.py:94,305–349,360–418,1041–1043`；`event_repository.py:12,56,97`；`database.py:82`；`rollup_holds.py:17–53` | 清信号与旧 coverage hold；不删原入账事务、事件 revision 或真实插件 source hold |
| D04 | `S/application/modules/persistence.py:11–14,55–56,114–128,136–143,148–149,162–168`；`conversation.py:15–16,61–62,130–148,166–167,236–237,274–277`；`S/container.py:133,394–395,533,547,550,579,601,1005`；`S/application/modules/automation.py:15`；`S/automation/worker.py:20` | 自动化只是 scope reader 依赖需迁移；长期记忆 worker 保留 |
| D05–D06 | `S/services/rollup_wakeup.py:22,40,43,102–106,118,122`；`chat.py:31–36,248–249,291–298,320–322,748–773`；`autonomous_groups.py:96`；`turn_coordinator.py:29`；`S/runtime/context_preparation.py:77–93,117–138`；`work_repository.py:1282–1396,1398`；`work_scheduler.py:57,214–215` | 只删 Rollup 等待；`context_preparation.py:41–74` 的未决协议恢复保留 |
| D06–D07 | `S/plugin_host/notification_repository.py:18–22,162,1454–1456`；`background_turns.py:15–16`；`S/runtime/work_source_guard.py:15–18,77,197–246,262–266,282–351` | 迁当前 source/read-version 与旧付费依赖，不保留旧 Rollup writer；真实来源/权限不凭摘要标签扩大 |
| D07 | `S/services/context_assembler.py:146–155,213,303–317,445,580,734–763,878–940,1010–1012`；`history_projection.py:57–60,96–110,156–190,203–223,244`；`prompt_composer.py:13,138–146,159`；`main_agent_turns.py:128,164,169,404–407` | 保一份公共 H、真实 adopted source 与 FrozenFragments；不是整删投影能力 |
| D08–D09 | `S/services/agent_runner.py:137,199–242,274–392`；`turn_execution.py:95–103,119–168,317,510–514,903–1081,1181–1189,1357–1359`；`main_agent_turns.py:507–510`；`subagent_execution.py:339`；`ordinary_compaction.py:28–188,191–306` | 所有 origin 的 helper dispatch/brief 与普通摘要分页一起退出 |
| D08、D10 | `S/runtime/work_session.py:88,166–170,280–283,433,648–1115,1259–1432,1601–1603,1656–1707`；`work_compaction.py:14–50`；`work_context_note.py:11,17–21` | 旧 staging 不再续买；真实 paid/opaque 原件及 note 的 SourcedFact 依赖保留 |
| D12 | `S/config.py:174,176–180,312–325`；`settings_domains.py:80,101–107,131–144`；`admin/models.py:161–168`；`admin/config_specs_hot.py:17–25,83–99,121–138`；`admin/config_service.py:1374–1391`；`.env.example:89,95–99,228–250`；`frontend/src/config-files.tsx:41` | 删独立水位/超时/批次/worker 配置；`.env` 的 foreground_max_batches 已是过期字段 |
| D12 | `S/model_runtime/models.py:27`；`profiles.py:95,133`；`executor.py:296,311,540–541`；`S/application/modules/model_runtime.py:83`；`S/deployment_setup/service.py:50,237–239`；`config/model_profiles.example.toml:52`；`config/model_profiles.providers.example.toml:70` | TOML 与生成器同步，否则旧 enum task 解析失败；历史 usage.task 字符串账保留 |
| D13 | `S/conversation/canonical_db_models.py:195–200,230–232,305–438`；`S/persistence/schema_guard.py:14,113`；`S/conversation/hydrate.py:265,270–286`；`S/identity/canonical_repository.py:25,552`；`S/persistence/people_repository.py:23,728,1014` | 新 migration 退投影专属字段；真实 canonical 身份/generation、隐私与 reset 仍工作 |
| D14、D16 | `S/application/control_health.py:61–63`；`S/health.py:53,74,126`；`S/services/command_service.py:19–20,67,90,216,245–258`；`S/cli.py:23,477`；`S/conversation/offline_recount.py:83,132,168,195`；`scripts/context_cost_simulation.py:42,60–62`；`tools/cache_probe.py:44–45,148,232`；`tools/cache_probe_runtime.py:317`；`tools/cache_probe.md` | 移旧 backlog/recount 与 probe 假设，真实 health/status 留在原边界 |

现有发布边界可复用：[projections，261–314](https://github.com/YuanYeYouTao/Yuki/blob/fb8823f53f2134e4029bc6f5f206e7eb87a468d1/src/qq_ai_bot/conversation/projections.py#L261)在原 writer 核 generation/source/privacy/epoch；[WorkJournal，430–513](https://github.com/YuanYeYouTao/Yuki/blob/fb8823f53f2134e4029bc6f5f206e7eb87a468d1/src/qq_ai_bot/runtime/work_journal.py#L430)合并 publication、protocol refs 和 journal。沿这些边界改旧 Rollup 语义，不新建历史协调器。

### 9.2 向前升级与历史 schema 依赖

| 冻结迁移，位于 `migrations/versions/` | 历史结构/依赖索引 | 本次要求 |
| --- | --- | --- |
| `0048_canonical_3_8_baseline.py` | `17–18,231,250,644,660,679,1892` | 保留旧库合法起点，新 head 退表 |
| `0049_canonical_only_bridge.py` | `21–22,29–31,68–77` | 保旧桥接事实，不继续旧运行链 |
| `0059_runtime_recovery.py` | `8–9,20` | 含真实 Work 恢复结构，不能随 Rollup 无差别回滚 |
| `0082_prompt_source_revision_closure.py` | `1,5,7–8` | 保原升级及 schema reader |
| `0089_context_observation_sources.py` | `6,15–25,28–29,47–50,99,118,120` | 原付费来源 DAG/撤权依赖不能丢 |
| `0107_query_workload_indexes.py` | `6–7,24–28` | 后续新迁移撤旧索引，不改历史升库文件 |
| `0112_common_public_history.py` | `5–6,210–232,243–248` | 保公共历史统一的升级事实 |

历史 schema 文件还有明确 reader：`0054_prompt_projections.py:6` 读 projection_schema，`0082:5` 读 projection_revision_schema，`0089:6` 读 observation_schema，`0108_material_delta_projection.py:10–11` 读 observation_schema/projection_material_schema，`0109_current_activation_material.py:10–11` 读 projection_current_schema/projection_material_schema。不能把这些 schema 快照一起删除。

当前 live 安装读取 `S/conversation/projection_models.py:10`、`S/persistence/schema_guard.py:13–14` 的 `projection_history_schema.PROJECTION_TRIGGERS_0113`；旧 Rollup owner triggers 在 `projection_history_schema.py:193–209`，由未来新 head 撤销并同步当前安装器。0105 任务树与 0113 保留私有原件的升级事实不属于摘要退役对象。

## 10. 实施时继续核验的协议问题

这些是具体正确性问题，不是新的审批表、经验门槛或保护层。沿已有调用点解决，能删则删，避免新建协调器。

| 编号 | 要核清的事实 | 设计方向 |
| --- | --- | --- |
| Q01 | 各实际 Provider 在当前 Main 追加 compact 指令时，完整工具/native tools 与 continuation 如何保持 | 对比真实 serializer payload；不靠相同 session 字符串宣称一致 |
| Q02 | pending call/result、签名与不可分割 opaque 允许在哪个边界替换窗口 | 沿原协议阶段；不伪造、截断或重发已成功工具 |
| Q03 | 混合 W1 compact 后稍后业务恢复，哪些任务事实当前可达 | `H_now + 必要事实`；不再注入整个含旧 H 的 C_mix；核 §5.2 句柄路径 |
| Q04 | 普通轮未接纳 Work 时，compact 的响应与发布由哪个已有 Run/journal 承接 | 保留普通 Main，不把内部维护登记成业务 Work |
| Q05 | 选源后编辑、隐私删除、generation 或授权发生真实变化 | 复用现有来源与 owner 核验；失效内容不能被摘要带回；普通新聊天只追加 |
| Q06 | response 已付但发布失败、提交成功确认不明、进程丢失未落盘响应 | 有持久响应复用响应；不确定先核原 ID；无法查回的 paid 状态不假定可重购 |
| Q07 | 旧数据库中合法 paid 页、旧准备等待与引用 | 向前迁移保事实、退役旧执行路径；有效 Work 原位继续，不复制新任务 |
| Q08 | 原生 compact opaque 内是否带入临时 runtime 内容，是否可独立换窗/跨业务恢复 | 实际能力验证；不可分割就如实限定 owner 与协议用途，不声称自动去掉 |
| Q09 | 普通 Main 也可能含私有工具资料与 R，哪一个当前实际来源切面可形成合法公共检查点 | 混合产物归原 owner；沿既有公共 Main 来源边界核可共享产物，不能用模型标签授权，也不能为提取公共 H 再冷包装发第二份摘要 |

## 11. 成本、缓存与最省方案的判断

### 11.1 优先去掉可以确定的重复工作

保存、暂停、续跑和结束用原数据库记录与回执，不需要摘要请求。可容纳的新短 Work 不因群史长触发独立 helper。需要 compact 才沿当前上下文调用模型，并让覆盖的原文真正退出后续窗口。恢复只带必要任务信息；不为保缓存永久冻结全量工具轨迹。

这可以去掉已确认的冗余调用，但**不能保证每个未来请求总价都低于所有备选设计**。当前路线、缓存期限、实际 hit/miss、读结果后的续轮和 compact 产出长度都影响总账单。

### 11.2 命中率、输入长度与总价不同

对原执行的全部实际物理请求汇总：

```text
模型费用 = Σ[(miss_tokens × miss_price
            + hit_tokens × hit_price
            + billable_output_tokens × output_price) / 1,000,000]
总费用 = 模型费用 + 实际工具费 + 实际缓存存储费等已核实费用
```

纳入 Main、compact、失败/重试、读取工具后的续轮。thinking/reasoning 若已包含在 billable output 中，不重复计费。缓存字段缺失记为未知，不自动补零。AGM 的配额/计价没有核实合同前，只给实际 usage 或归一单位，不套官方直连价格当生产账单。

假设每千 miss 费用为 1、每千 hit 为 0.1，输出完全相同：100k 输入/90k 命中费用为 19；50k 输入/10k 命中费用为 41。短输入在这个热样本反而贵；两者完全冷时分别为 100 与 50。**只是数学例子，不是 Yuki 实测、AGM 价格或节省比例。**

在同次执行中追加原 call/result 有利于保持已有前缀。如果替换私有尾部 T 前面的 H，T 即使逐字相同，也会失去原长前缀中的复用位置；本方案实际业务恢复采用 `H_now + 必要任务事实`，并不搬回整份旧 T。相同 session ID 不会把没有发送的内容自动变成可免费使用的服务端记忆。[Gemini 缓存说明](https://ai.google.dev/gemini-api/docs/generate-content/caching)、[DeepSeek KV cache 说明](https://api-docs.deepseek.com/guides/kv_cache/)。

“保留 W1 原轨迹，再把暂停期间未见聊天追加末尾”可在某些热样本中便宜，但私有轨迹尾部继续增长，长暂停后缓存可能不在，不同 Work 中同一公共消息的位置也会不同。理论上可以复用既有 event 渲染与已观察水位，不一定需要另建游标；仍需明确差分与 compact 接续。它与当前业务恢复读取最新 H 的合同不同，不作为本次实施方案。可以在隔离重放中作为成本对照，不未经实验宣称它必然更贵或必须另造历史协调器。

### 11.3 网关与缓存的责任边界

沿 Yuki 最终 Provider payload 与 AGM 实际转出 payload 比较 system、tools、消息顺序、思考/输出设置、模型、Profile、协议和可用的 session 元数据。Yuki 输入稳定不等于网关转出稳定；转出稳定也不保证 Provider 每次命中。

本次不自动改 AGM fork，也不把第三方缓存协调器放进 Yuki。若后续实测确认网关独立改写导致分叉，记录准确转出差异与责任仓库，再修那个实际调用者。新 namespace、首请求、短间隔或同 session ID 都不能单独作为冷/热证明，依实际 cache usage 分类。不存在从这些样本证明“固定 3k 尾部必不缓存”的结论。

## 12. 待实施验证、真实实验与数据表

### 12.1 先用真实入口与 serializer 重放，再做实际路线验收

| 编号 | 场景 | 离线验证重点 | 后续真实证据 |
| --- | --- | --- | --- |
| V01 | 普通群聊、长公共史、新短 Work，完整请求可容纳 | 旧 Work/ordinary/Rollup 零派发；不强制创建 Work；原目标不丢 | 实际物理调用数与无重复 helper 的自然请求 |
| V02 | 多工具长尾达到真实容量，手动/自动 compact | 同一当前 Main 路径，沿原上下文；已覆盖原文退出；不是 records 冷读 | compact 与后续完整执行的 usage、延迟及有效结果 |
| V03 | 新用户/不同 actor 在同一 Conversation 发言 | 较早公共事件表示不变；当前资料在当前正文上方且边界清晰 | 实际入站/派发与网关转出对照，不只比哈希 |
| V04 | W1 补充要求、独立 W2、W1 恢复 | 原 ID/来源/预算，最新 H；含旧 H 的 C_mix 不再双重注入 | 分别记录任务让出、恢复与实际输入 |
| V05 | 普通 derive、子 Agent、父等待 child | 原父子关系、completion/result 与 wait 唤醒；无摘要门槛 | 原父/child/revision 结果，最终状态与真实效果 |
| V06 | SELF、automation、GitHub/插件、SDK、worker | 共同入口与各自真实合同；完整指令和私有范围；无旧调度等待 | 各入口分别采样，普通聊天不能代替其验收 |
| V07 | compact 期间新输入、steer、迟到输入 | 原覆盖尾固定，后来输入按内部事件顺序接入且不重复 | 队列、compact 暂停与后续首次响应时延 |
| V08 | 工具 pending、原生搜索、媒体、签名/opaque | 原真实配对与媒体/对象恢复；合法边界换窗，不补造结果 | 各实际协议可续接请求及对应回执 |
| V09 | compact 前后崩溃、重启、response 保存/发布失败 | 已 durable response 复用；DB-only 重备；unknown 不盲发；成功效果不重做 | 原 call/request/response/预算与真实重试数 |
| V10 | pause/cancel、来源/权限/privacy/generation 变化 | 原 owner、输入和撤权事实；失效源不被带回；不要求额外摘要 | 取消及后续授权新输入的原记录 |
| V11 | 旧数据库升级、有旧 Rollup 等待/付费页 | 原数据库向前迁移；有效 Work 可继续；旧 worker/job/overlay/ready 不复活 | 新镜像 migration head、原任务与对象读取 |
| V12 | 结果档案回读、缓存失去、长暂停 | 必要发现与原句柄可达；不重查已取得外部结果；计额外续轮 | 实际 artifact/terminal/文件读取与整体 usage |
| V13 | 未触发的群聊与其他会话并发 | 只落账消息零模型请求；compact 不暂停其他会话 | 自然流量中的未触发/失败样本也进入统计 |
| V14 | 全仓残余、配置、API/UI、文档/工具/CI | 现行消费者逐项删除或迁移，旧配置不再造旧服务；无保温/门槛机制 | 最终提交、定向检查、CI、部署各自独立回执 |

离线重放验证输入与恢复正确性，不证明上游缓存命中。真实路线分别测 **Gemini 经 AGM、DeepSeek Chat、DeepSeek Responses** 的实际启用连接；路线未启用记为未测，不能用别的协议替代。使用当前完整系统/工具合同和真实上下文，旧合成脚本不直接沿用；不启动第二个生产 Bot，不制造 QQ 探针或缓存保温请求。自然流量只观察并脱敏。

### 12.2 每次物理请求与每项业务执行记录什么

| 证据组 | 字段 |
| --- | --- |
| 版本与身份 | 源码/部署 commit、实际 route/model/profile/protocol、Conversation/generation、内部 event/execution/Work/Run/call、checkpoint |
| 实际输入 | system、完整 tools/native tools、设置、消息顺序、可复用前缀、覆盖来源与 cut、旧原文是否退出；公开报告只放脱敏统计/证据引用 |
| 请求与计量 | Main/compact/读取续轮类别、实际物理调用与重试数、HTTP/模型结果、prompt/hit/miss/completion/reasoning、字段缺失覆盖 |
| 时间与费用 | 排队、派发、首字节、首次可见内容、完成、compact/工具等待；真实计价合同/账单/配额；未知项明确标记 |
| 业务结果 | 原发送/效果回执、输入消费、父子结果、最终状态；区分 FakeBot、接口成功和自然 QQ 送达 |

按每个原执行汇总，再按入口、上下文长度、是否 compact、短间隔/长暂停等实际场景分组，保留失败、缺 usage 和未知回执样本。仅挑成功高命中样本会掩盖真实费用。

### 12.3 完成后填回报告的数据

| 指标 | 旧基线 | 新版 | 当前状态 |
| --- | --- | --- | --- |
| 退役摘要物理请求数 | 待按真实调用统计 | 待统计 | 未实施/未测 |
| 重复原文 parts、tokens 与覆盖退出范围 | #417 已知 549 条正文重叠，tokens 未知 | 待同口径核验 | 不能由条数换算金额 |
| 全执行 input、hit/miss、output、缺字段比例 | 待真实路线测量 | 待测量 | 未知 |
| compact 请求及后续读取/续轮费用 | 待测量 | 待测量 | 未知 |
| 排队、首响应、总耗时、暂停时间 | 待测量 | 待测量 | 未知 |
| 总费用/配额与可归因节省 | 无现成可比账单 | 待实际计价 | 不填写推测节省百分比 |
| 输入/目标/结果/送达正确性 | 待定向重放与自然样本 | 待验收 | 源码对照不代替验收 |

有实际 baseline 和新值后记录绝对差及比例、样本数、字段覆盖与适用场景。不能用请求 bytes 代替 token、命中率代替账单、同 session 代替前缀稳定、CI 代替真实 QQ 或文档提交代替部署。

### 12.4 测试与 CI 的同步范围

测试重心是实际入口、最终 payload、原结果复用、向前迁移和 ownership；删除只证明旧 helper/worker/schema 还存在的断言。Work journal、工具配对、真实预算/来源、树与结果回送的有效测试继续保留或更新。测试不应复刻新函数内部逐行实现。

现有通用 `quality.yml` 包含 ruff、mypy、frontend 与 pytest，不是专属 Rollup job，不能因为删除 Rollup 顺手砍掉整条质量 CI。旧手动 `cache_probe` 若仍构造专用摘要或过时工具合同，删掉或改成当前真实入口重放；不把真实付费缓存实验变成每次 CI 的固定耗时或命中率门槛。

具体消费者如下，路径以 `tests/` 为根；更新退役依赖，保留能证明真实行为的覆盖：

| 分组 | 文件与行号 | 处理 |
| --- | --- | --- |
| 通用 CI | `.github/workflows/quality.yml:21–30`（仓库根路径） | 保留通用检查；无专属 Rollup job |
| 共享 fixture | `conftest.py:15–21,87,137–151,183`；`support/conversation_rollup_370_helpers.py:1,15`；`unit/rollup_test_helpers.py:1,15`；`support/work_compaction_capacity_helpers.py:66,100`；`support/work_effect_results_helpers.py:30`；`support/work_compaction.py` | 删旧 summary helper/fixture，迁真实调用者 |
| Rollup 行为 | `unit/test_rollup_complete_sources.py:45,97,110,121,163,206,261,284`；`unit/test_rollup_commit_snapshot.py:59,175,199,215,244,286,304` | 删 job/overlay/watermark 假设，保真实 source、引用、CAS/SQLite517 不重购 |
| Work 压缩与原件 | `unit/test_work_compaction_anchor_recovery.py:62,91,124,168,184,252,348,388,481`；`unit/test_work_compaction_public_delta.py:23` | 改真实 compact 候选/换窗，保 paid/媒体/未决原件 |
| 协议与源 | `unit/test_work_protocol_continuity.py:236,1039,1243,1372,1432,1526`；`unit/test_protocol_recovery_preparation.py:78,134,154,167,208,240,309,322,344`；`unit/test_provider_native_result_boundaries.py:84,108,125,190,847,974,1044,1170`；`unit/test_work_source_guard.py`；`unit/test_work_journal_source_retry.py` | 真实签名、结果、source、预算、恢复保留；旧 Rollup 耦合迁走 |
| 公共 H 与当前材料 | `integration/test_current_material_main_contract.py:125,226,406,603`；`integration/test_provider_session_auxiliary.py:43,189`；`unit/test_work_scope_contention.py:22,97,175`；`unit/test_history_time_projection.py:9,36,52,68,97`；`unit/test_runtime_config_read_snapshot.py:28,52,58–67` | 核统一 H、当前 R、原租约/快照；不是按人或按 Work 分公共史 |
| 其他入口/fixture | `integration/test_self_common_capabilities.py`；`support/social_identity_cases.py`；`unit/test_canonical_ingress.py`、`test_derived_text_transactions.py`、`test_external_event_prompt_isolation.py:14–15,100–101,128`、`test_external_event_runtime_fences.py`、`test_history_missing_body.py`、`test_material_observation_storage.py`、`test_migration_0110.py`、`test_plugin_reply_causality.py`（后七项同属 unit） | 清 import/fixture 的旧链耦合；真实 ingress/可见性/插件来源/历史原文覆盖不删除 |

这些索引只说明消费者，不能当作本轮已跑测试。实施后适配变更的定向检查和必要 CI，结果回填 D/V。

## 13. 分步执行与交付记录方式

| 阶段 | 应交付的具体结果 | 当前状态 |
| --- | --- | --- |
| A. 设计与源码盘点 | 本报告、真实聊天摘录、代码索引、删除范围、验证与成本边界 | 本轮整理完成；D/V 均未执行 |
| B. 统一当前会话 compact | 当前 Main 入口、同 owner 响应持久与真正窗口替换；核 Q 项 | 待实施 |
| C. 全链路退役与迁移 | D01–D16 所有消费者、配置、旧等待、数据库/对象升级与文档 | 待实施 |
| C2. 子 Agent 完整能力继承 | 附录 B 的 S01–S10；主子共用能力合同，删除声明/执行/依赖与提示词降级 | 待实施 |
| D. 定向重放与 CI | V01–V14 按覆盖范围填写；实际 payload 与恢复证据 | 待验证 |
| E. 实际 Provider 与生产验收 | 各路线计量、自然流量、原任务与效果；填 §12.3 | 待授权实施阶段推进 |

实施时每完成一项，在对应 D/V/S 行记录实现提交和验证证据；未测或受阻项保持原状态。终审再次全仓搜索 Rollup、旧摘要任务/dispatch、context_rollup、staging、selected-summary 与旧配置消费者，同时追查子 Agent 白名单/执行拒绝/缺依赖，解释历史引用及真实 reader；不能单凭搜索词归零宣布完成。

本次文档按用户授权直接提交并推送 main，并向 #417/#418 补固定版本链接。这一步只完成 A，不代表 B–E。后续提交、PR/合并、镜像、迁移、部署、自然验收分别记录，不把修改一个断言或文档当作全绿上线。

## 附录 A：本会话真实聊天摘录

以下为真实讨论的相关摘录，**用户消息保留完整原文，助手长回复选取相关原文段落**。不是完整聊天导出，没有补造时间或消息 ID。助手历史建议只记录决策演变；本报告现行方向以 §1 与 #418 为准。

### A1. 找到原 PR

**用户：**

> 我记得之前我去把work塞入上下文去持久化，为了让agent能够读所有交叉work的编排结果，提了一个PR，想为了省钱提高命中率，但是实际上这个方法提高不了多少命中率，所以请你把那个pr帮我找出来

**助手，相关原文段落：**

> 找到了，是 **[PR #228：Keep current chat history across Work resumes and compact context safely](https://github.com/YuanYeYouTao/Yuki/pull/228)**，北京时间 **10 月 3 日 05:47** 合并。
>
> 它新增了 `context_note`、持久观察记录和历史选取机制，将工作线索冻结进后续上下文，供后续聊天及其他 Work 使用。原任务书还明确画出了 **W1 → W2 → W1 共用历史**的例子，与你描述的目的吻合。[原任务书](https://github.com/YuanYeYouTao/Yuki/blob/3d9e2733991fe00f7774ed83c2c4acf9eaafdb01/docs/architecture/work-context-and-chat-continuation-taskbook.md#L195)
>
> 但它的验收说明也承认：**受控实验没有证明生产缓存命中率提高**，上线时缺少足够的普通聊天样本。因此，当时不能把“已经更省钱”视为完成验收。[PR 验收记录](https://github.com/YuanYeYouTao/Yuki/pull/228)

后续核查已在 §3 纠正第一段对原目的的概括：重点是恢复不倒退，而非为了所有 Work 共享轨迹提高缓存。

### A2. 是否整条回滚

**用户：**

> 你觉得是不是该全部删掉，像这种W1 to W2 to W1 这种共用历史的例子，我当初到底是为了干什么，任务书里写了什么，这个pr有一万多行新增啊，是不是都得删掉

**助手，相关原文段落：**

> **我支持大删，但删除范围应按功能划定，不能把 #228 整个回滚。** 它把聊天恢复、观察笔记、压缩、资料回读等几件事绑在了一起。
>
> 需要修正我上一条的表述：**W1 → W2 → W1 的例子，主要是在解决“恢复任务时聊天倒退”的问题。**

### A3. 省钱与正确恢复

**用户：**

> 你调研一下怎么改最省钱，W1暂停后继续聊天，恢复W1这个行为是完全正确的，但具体在于我们应该去如何设计历史。调研这种持久化任务设计

**助手，相关原文段落：**

> 调研后，我建议采用：**一份会话公共历史，Work 独立存档；执行中保留工具上下文，恢复时带回必要任务发现。保存和恢复不额外购买摘要。**
>
> 这符合你说的行为：W1 暂停期间群聊继续，恢复 W1 时看到最新聊天，同时知道自己做到了哪一步。

### A4. 整个旧摘要模块退役，并与 #418 一起处理

**用户：**

> 我建议是彻底删掉整个摘要模块，这个摘要是不应该出现的，rollup将在418换成真正的compact，如何压缩这个问题这个需要和418一起改，并且调研学习一下pi的压缩模式，毕竟pi在处理持久化work上面有经验，我记得在PR228中还做了work tree 的设计，我觉得work tree确实是可以留下来的，一个问题：w1 to w2 to w1这个链路重要吗，像是在进行work1的时候开始执行work2这种应该使用work tree结构吧，而不是两个并行work，我的理解可能会有问题，你需要继续去调研pi或者说其他有持久化能力的agent

**助手，相关原文段落：**

> **同意：旧的独立 Work 摘要、ordinary 摘要链，应与旧 Rollup 一起退役，压缩统一按 [#418](https://github.com/YuanYeYouTao/Yuki/issues/418) 设计。** 我上一条继续保留 Rollup 的建议不再适用。任务保存、等待和恢复继续使用原 journal 与回执，不需要额外总结。
>
> 你的理解需要调整一处：**任务树与并行执行不是二选一。** 树表示目标归属；串行、并行、等待表示执行方式。

当前统一任务树来源也已在 §3.3 纠正为 #286。Pi 并没有现成解决 Yuki 共享群史与多 Work 的全部组合，具体差异见 §7–8。

### A5. 文档与发布授权

**用户，连续相关消息原文：**

> 针对这两个问题的讨论整理一份简要的报告，先给我预览一下，报告中需要附上我们的真实聊天

> 把这段补在对应的issue下面

> 现在开始细化一下报告，然后放在项目的文档中好吗，报告需要包含一切

> 现在开始细化一下报告，然后放在项目的文档中好吗，先写，写完直接提交到main，并且在这两个issue后面附加上链接

> 用满subagent

已先发布的简要讨论：[#417 评论](https://github.com/YuanYeYouTao/Yuki/issues/417#issuecomment-6153321632)、[#418 评论](https://github.com/YuanYeYouTao/Yuki/issues/418#issuecomment-6153324917)。本报告展开其源码、方案与待验收范围，固定版本链接在文档推送后另行补到两个 Issue。

### A6. 用户在报告细化期间追加的子 Agent 要求

**用户，完整原文：**

> 以及子agent的能力需要加强，现行的子agent完全就是一个阉割版本，子agent搭配这种复杂work才能真正发挥其能力

> 子agent需要完美继承主agent的所有能力，不应该在能力上有任何的限制，懂吗

这两条把子 Agent 的目标明确为完整能力继承，附录 B 按该要求新增；不是沿用旧合同中的缩减工具子集。

## 附录 B：子 Agent 完整能力继承与复杂 Work

### B1. 目标与现有能力差距

**子 Agent 完整继承主 Agent 所有能力，删除子 Agent 专属能力限制。** 共用 Main 的完整冻结工具合同、direct / Code 模式、插件工具、当前连接原生能力、核心系统约束和执行后端。主 Agent 以后新增能力，子 Agent 沿同一合同取得，不能再维护一份默认缩减名单、子专用工具总数或另一套能力开关。

子目标、父子关系、原 child Work ID、当前输入与结果归属是这次执行的资料。它们不应成为禁用工具的理由，也不要求复制父 Agent 的全量私有轨迹或 opaque。执行照主 Agent 的同一规则处理实际 actor/source、target、授权、原回执和累计预算；同一获准操作不能仅因处在 worker 中就被拒绝。能力一致，不代表把子节点的执行冒记为父节点、伪造超级用户或把子任务费用再计一次。

能力继承用于共同合同下的新执行与新协议链。已付未决请求继续按原已提交声明、配对与 opaque 恢复，不事后改写其真实派发内容；后续能力更新也沿 Main 的同一合同，不给子 Agent 另设升级规则。

当前子 Agent 已有终端、工作区/媒体、网络、历史/Memory 读取、artifact、`task_control.derive/wait` 和父通信，因此不能说它完全不会执行任务。问题是固定工具上限、执行拒绝和资料准备共同把 Main 的许多能力剔掉，嵌套工作者还被旧通信方向逻辑卡住。不是只改 prompt 就能完成。

主声明来源是 [MainAgentContract.definitions，55–96](https://github.com/YuanYeYouTao/Yuki/blob/fb8823f53f2134e4029bc6f5f206e7eb87a468d1/src/qq_ai_bot/services/main_agent_contract.py#L55)。以下列的是已定位差集，不是未来允许继承的封闭名单：

| Main 已有而当前 worker 静态名单排除 | 声明索引，`S/` = `src/yuki/` |
| --- | --- |
| `get_my_capabilities` | `S/services/agent_tools.py:450` |
| `read_conversation_history`、`send_message`、`poke_person`、`get_group_members`、`recall_own_message` | `S/social/tools.py:56,104,163,182,196`；`S/services/agent_tools.py:823` |
| `memory_change` 与 Main 的真实 Memory 写入能力 | `S/services/agent_tools.py:574–743` |
| `call_onebot_api` | `S/services/agent_tools.py:802–818`；与 Main 一样按实际超级用户授权执行，不对子身份另禁 |
| 自动化创建/查询/修改/诊断/暂停/恢复/取消/立即运行/历史与 `time_get_current` | `S/automation/tools.py:172–267`；`S/services/chat.py:576–582` |
| `update_short_state` | `S/workspace/short_state.py:14–32`；`S/services/main_agent_contract.py:74` |
| `subagent_start`、`subagent_control`，以及对自己的下级工作者补充输入/读取结果 | `S/runtime/subagent_tools.py:79–105`；`S/services/main_agent_contract.py:75–78` |
| 当前部署的全部获准插件工具 | `S/services/chat.py:584–622`；`S/services/main_agent_contract.py:89–96` |

不写固定差集总数，因为插件和现行主合同会变化；实现从主合同继承，不逐个向 WORKER_NAMES 加名字。

### B2. 已定位的缩减链路

| 层次 | 当前索引 | 待处理 |
| --- | --- | --- |
| 工具声明 | [subagent_tools，45–72](https://github.com/YuanYeYouTao/Yuki/blob/fb8823f53f2134e4029bc6f5f206e7eb87a468d1/src/qq_ai_bot/runtime/subagent_tools.py#L45)的 WORKER_NAMES/OPTIONAL/REQUIRED；[SubagentExecution，87–118](https://github.com/YuanYeYouTao/Yuki/blob/fb8823f53f2134e4029bc6f5f206e7eb87a468d1/src/qq_ai_bot/services/subagent_execution.py#L87)取交并生成另一个 worker ScriptApi，300–306 再传子集 ceiling | 删除缩减 producer，共用 Main 已冻结完整声明、模式与 API |
| 工具执行 | [MainAgentBackend，271–280](https://github.com/YuanYeYouTao/Yuki/blob/fb8823f53f2134e4029bc6f5f206e7eb87a468d1/src/qq_ai_bot/services/main_agent_backend.py#L271)在真实绑定前按 child 与名单返回 worker_tool_not_declared | 退 worker 专属拒绝。通用真实授权/来源/已声明工具核验仍与 Main 共用，不另加子策略 |
| 生命周期/查询 | `S/services/main_agent_backend.py:139–160`；`S/services/agent_runner.py:862–883`；`S/runtime/work_control.py:728–746`；`S/runtime/work_queries.py:83–100,145–146` | 删仅按 child 身份缩减的控制/目录分支；已登记本 Work 不重复 accept 的事实保留，不禁止创建真正的新目标 |
| Code Mode | `S/services/agent_runner.py:779–785,986–990,1088–1092`；`S/codemode/tool_visibility.py:84–92` | 复用同一 Main API/目录/执行后端，退子专用目录与 fallback；direct 与 Code 都能实际执行完整能力 |
| 实际执行资料 | [SubagentExecution，273–306](https://github.com/YuanYeYouTao/Yuki/blob/fb8823f53f2134e4029bc6f5f206e7eb87a468d1/src/qq_ai_bot/services/subagent_execution.py#L273)当前 gateway=None、allow_generic_onebot=False、worker key 与子集 backend | 核齐共同 Social、网关、Memory、automation、插件执行所需的真实来源/target/依赖；只扩大声明而缺执行资料仍不算完成 |
| 提示词与文档 | [subagent_tools，18–32](https://github.com/YuanYeYouTao/Yuki/blob/fb8823f53f2134e4029bc6f5f206e7eb87a468d1/src/qq_ai_bot/runtime/subagent_tools.py#L18)固定“QQ发送与长期记忆变更交给主Yuki”；development-contract.md:33；persistent-subagents.md:10–15,27–40,86–90 | 删除永久禁令和低能力规则；复用 Main 核心约束、检索与 Memory 规则。角色说明只交代子目标/关系，不成为完成前汇报门槛 |
| 递归与通信方向 | [subagent_tools，125–167](https://github.com/YuanYeYouTao/Yuki/blob/fb8823f53f2134e4029bc6f5f206e7eb87a468d1/src/qq_ai_bot/runtime/subagent_tools.py#L125)把 worker 的 parent_id 固定成自己的父，identity 固定自己，永久拒 subagent_control；SubagentRepository.message:390–393 也按 worker 决定方向 | 根据操作和真实 sender/parent/child 选 owner：向父问问题、向自己的子目标补充要求是不同方向；不因为自己是孩子就不能管理自己的孩子 |
| 来源冷恢复 | [execution_sources，351–378](https://github.com/YuanYeYouTao/Yuki/blob/fb8823f53f2134e4029bc6f5f206e7eb87a468d1/src/qq_ai_bot/services/execution_sources.py#L351)、`S/services/subagent_execution.py:182–224` | 当前 reader 主要覆盖 message/SELF/automation；SDK/插件复杂子任务沿其现有真实 owner 补通，不伪造真人事件或把父 inbound 复制成新入站 |

容器已经让工作者复用 Main definitions 与同一个后端入口：[container，367–372](https://github.com/YuanYeYouTao/Yuki/blob/fb8823f53f2134e4029bc6f5f206e7eb87a468d1/src/qq_ai_bot/container.py#L367)。优先删中途取子集及其配套分支，不另造“增强 worker”服务。

执行依赖还需区分，不能只归因 `gateway=None`：

- 普通 send/poke/recall 走共享 Social service 与实际 Presence 路由，不直接依赖该 gateway 字段。当前子执行却固定 `allow_generic_onebot=False`，未传 `actor_is_superuser` 而落默认 False，故真实超级用户的 generic OneBot 路径仍不可达。沿 Main 相同来源与 Presence 绑定恢复实际权限，不把所有子节点提升为超级用户。索引：`S/services/agent_tools.py:163,946,2393–2395`；`S/social/agent_adapter.py:18–49,156`。
- Memory 在消息来源中已经按同主体打开 session，原 origin 的读写合同与真实事件/actor/Conversation/Presence 是 Main 共有规则；解除 worker 名单和禁令，沿同一 mutation/evidence 入口记录子执行，不借父名义伪造证据。索引：`S/services/subagent_execution.py:252–269`；`S/memory/runtime/resolver.py:27–28,66–101`；`S/memory/runtime/capability_view.py:31–60`；`S/services/agent_tools.py:2006–2084`；`S/memory/mutation/service.py:1017–1062`。
- 自动化使用已有服务与活动绑定的 actor/creator；short_state 是同一全局资料，不能为每个 child 再复制一份。索引：`S/automation/tools.py:270–275,456–462`；`S/automation/service.py:754–778`；`S/workspace/short_state.py:14–32`；`S/services/main_agent_backend.py:322–339`。
- 当前 direct/Code 模式已跟 MainContract.mode，原生搜索也走同一 Runner、Profile/protocol/config；没有证据支持“worker 原本完全没有原生搜索”的说法。保持这些已共享能力，删除其他缩减环节即可，不造替代能力 Binder。索引：`S/services/subagent_execution.py:83–85,327`；`S/services/agent_runner.py:398–427`；`S/services/turn_execution.py:538–552`；`S/container.py:366,373`。

真实权限资料对照：普通 Main 的 `S/services/chat.py:1010–1028` 与自动化 Main 的 `S/services/work_resume.py:286–306`传入实际 gateway、allow_generic_onebot、actor_is_superuser；子 Agent 对齐这些现有绑定即可，不需要新的 worker 权限模型。

递归不能只开放工具名。`SubagentRepository.start:35–36` 核当前 child lease 与新目标直属父是否相同；旧 wrapper 却把该父选成当前节点的父。正确做法是把当前真实 Work 作为新工作者直属父，再复用原仓储/树/预算/租约。原 `start:54–59`、`related:305`、`resume:508–513` 的真实关系与 source/generation 核验不需另建体系。现有 derive/wait 与结果回送见 §6。

### B3. Pi 的完整配置继承，以及不能照搬的示例削减

Pi 核心 `createAgent` 默认整体复制 owner conversation 的 AgentState，字段包含 model、thinkingLevel、extensions、tools、modelTools、instructions、cwd，再由同一解析器生成工具、nested callable 与系统 sections。见 [agent，159–169、185–243](https://github.com/earendil-works/pi/blob/3daa20280f6a7c7cbe0935680c9a38fdc3863005/packages/durable/src/harness/agent.ts#L159)、[AgentState，405–425](https://github.com/earendil-works/pi/blob/3daa20280f6a7c7cbe0935680c9a38fdc3863005/packages/durable/src/harness/types.ts#L405)。这是普通 owned child 创建时的配置副本，不能声称父配置后来的任意变更会自动更新所有子 Agent；显式 fork 的 as-of 配置也不能与普通新 child 的 owner 配置混同。

owned child 创建也经过同一 createAgent hook，owner edge 与可选 fork parent 分开，Provider sessionId 按新 conversation 建立。能力继承不要求复用父 Provider continuation 或完整私有 transcript。[harness，341–371](https://github.com/earendil-works/pi/blob/3daa20280f6a7c7cbe0935680c9a38fdc3863005/packages/durable/src/harness/harness.ts#L341)、[transaction，301–324](https://github.com/earendil-works/pi/blob/3daa20280f6a7c7cbe0935680c9a38fdc3863005/packages/durable/src/session/transaction.ts#L301)、[provider，6–19](https://github.com/earendil-works/pi/blob/3daa20280f6a7c7cbe0935680c9a38fdc3863005/packages/durable/src/harness/provider.ts#L6)。

但两个示例随后主动移除再派子 Agent 的能力：前台删 Subagent extension，后台删 SubagentTools 并写“Subagents cannot start subagents”，还可替换 instructions。它们是示例应用策略，**本次不照搬**，也不能把示例称为“子 Agent 已完美继承且可递归”的证据。[前台示例，56–59](https://github.com/earendil-works/pi/blob/3daa20280f6a7c7cbe0935680c9a38fdc3863005/packages/durable/test/examples/22-subagent-foreground.ts#L56)、[后台示例，190–196](https://github.com/earendil-works/pi/blob/3daa20280f6a7c7cbe0935680c9a38fdc3863005/packages/durable/test/examples/23-subagent-background.ts#L190)。

Pi 多 child 先建再等与 sequential/parallel 执行是分开的能力，可借鉴其 owned task、稳定请求/结果 key，而不移植 anchor/reporter 第二管理器或 child 禁 background 策略。[child tasks 示例，66–76](https://github.com/earendil-works/pi/blob/3daa20280f6a7c7cbe0935680c9a38fdc3863005/packages/durable/test/examples/24-child-tasks.ts#L66)、[generation，245–262、587–618](https://github.com/earendil-works/pi/blob/3daa20280f6a7c7cbe0935680c9a38fdc3863005/packages/durable/src/harness/generation.ts#L245)。这些是源码对照，本轮未跑新的递归模型样本。

### B4. 与复杂 Work 一起完成的范围

以下全部**待实施/待验收**，并入 D/V；不以完成本报告代替实现。

| 编号 | 具体工作 | 需要证明的行为 |
| --- | --- | --- |
| S01 | 删除 WORKER_NAMES/REQUIRED 子集及 worker 专属能力合同/API producer，共用 Main 完整声明/模式 | 当前和以后新增的 Main 工具自动被子 Agent 取得；direct/Code 的声明、目录与实际执行一致 |
| S02 | 清退后端及 Runner 的 worker 身份 ceiling、专属控制/查询降级 | 同一来源下同一个合法操作，主子沿同一执行后端成功；不只比较工具名称 |
| S03 | 补齐现有 Social/网关/联系人/历史/消息发送/撤回等执行资料，删除永久交父禁令 | 子 Agent 可按任务自己完成原获准 QQ 操作；记录自身原 effect/call/发送回执；后续失败不重发 |
| S04 | 完整继承 Memory、short_state、automation/time 与插件能力及执行所需依赖 | 使用 Main 相同事实/授权/版本规则完成读写或安排；不因 worker 身份改成只读、不清预算 |
| S05 | 修正嵌套 subagent_start/control/message 的真实 parent/target/direction | 子派孙、对自己下级追加/查询/停止/恢复、对父提问均落正确节点；不变成父的兄弟或管理自己 |
| S06 | 主子共用核心系统约束、检索规则、原生能力和完整工具用法；删缩减 prompt 与文档 | 子角色资料只表达目标/关系，不能写“不能联网/发送/写记忆/再派 Agent”等永久禁令 |
| S07 | 子/孙 Work 使用共同 compact、journal、原 paid response 与业务恢复 | 等待、暂停、重启、改向、结束都保原目标/输入/回执/结果；无额外摘要门槛或完整公共史重复副本 |
| S08 | 复核多层树的等待、结果、取消与完成通知；沿现有预算/调度 | 结果回原父且不重复，父继续其他工作不丢 child；无“所有子节点成功才准完成”或固定递归深度新条件 |
| S09 | 同步测试/fixture/CI/配置与现行能力文档，删坚持子工具排除的断言 | 定时、SELF、用户、SDK/插件来源派子后均实际可用；授权变动与原效果保持真实 |
| S10 | 用 §12 的同口径记账验证复杂树，而非固定更多 token 换能力 | 每次物理请求/重试/结果读取计一次；祖先累计预算与实际 usage 不重算；QQ、Memory、automation 外部效果只在后续获准验收中执行 |

明确要更新的旧断言：[scheduled_subagent_recovery，396–397](https://github.com/YuanYeYouTao/Yuki/blob/fb8823f53f2134e4029bc6f5f206e7eb87a468d1/tests/integration/test_scheduled_subagent_recovery.py#L396)要求 worker 不含 subagent_start/send_message；[self_initiative_runtime，484–561](https://github.com/YuanYeYouTao/Yuki/blob/fb8823f53f2134e4029bc6f5f206e7eb87a468d1/tests/unit/test_self_initiative_runtime.py#L484)依赖 WORKER_NAMES 与 backend 子集。改为真实主子能力一致、合法递归目标与效果执行，不保留旧削减方案为“安全测试”。结果 checkpoint、原来源授权、累计预算和重启不重做的有效覆盖保留，不能顺带整文件删除。

增加真实主 → 子 → 孙的定向恢复样本，检验完整工具实际调用、parent/ancestor 关系、输入/结果 revision、completion/wait、cancel/restart/unknown-paid。三级只是验证样本，不是固定递归深度。`S/runtime/work_budget.py:21–64`沿树找到 root 后只更新一条根累计预算，当前 Work checkpoint 只更新自己的本地次数；费用按唯一 model invocation 的实际物理 attempts 汇总，不能再加 root 预算与子结果统计重复收费。本地查询/结果通知自身不自动算一轮模型调用，只有实际派发的新轮才计费。

可复用现有 ModelInvocation 的 physical_request_count/unknown_usage_request_count 与 ProviderAttemptCounter，不新建计费管理器。既有 `tests/unit/test_subagent_result_checkpoint.py`、`test_worker_hot_limits.py`、`tests/support/subagents_helpers.py`、`tests/integration/test_scheduled_subagent_recovery.py`的原身份/结果/预算/恢复覆盖保留并适配；当前仓库没有 `tests/unit/test_subagents.py`，不要按过时文档去维护不存在的测试。

扩大完整能力合同可能增加子 Agent 首次请求的工具输入；这是待测费用，不作为再次削减子能力的理由。优先沿同一冻结顺序、原生模式、Code API 和实际前缀减少重复装配与模型往返，依任务只读取需要的资料。不能通过强制更大 prompt、复制父全轨迹或新的缓存保温任务实现“增强”。
