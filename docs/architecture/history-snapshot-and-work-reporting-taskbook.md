# 历史快照与长任务交互 Harness 修复任务书

## 1. 状态、基线与范围

| 项目 | 内容 |
| --- | --- |
| 日期 | 2026-10-02，Asia/Taipei |
| 仓库 | `YuanYeYouTao/Yuki`，本地目录 `D:\Code\My_Github_Repo\Yuki-QQbot` |
| 核查基线 | `513a263519f1c4d237840ffe5f27ea0697797db5`；编制时与远端 main 一致 |
| 接续会话 | [核对杂项审计与修复方案](codex://threads/01a0f2b1-f312-71b1-ae8a-a3b10026e33f) |
| 本次交付 | 最后全量核查与任务书小修，然后实施、验证、PR、合并及 Bot-only 上线；实施状态另记交付记录 |
| 本轮授权 | 用户已授权自主实施、按既有流程合并上线、最多子 Agent 并行；具体能力验收由用户完成，不擅自发送 QQ 测试消息 |
| 生产证据 | 本轮只读复核 `ops-ea446d6`、schema 0088、原 Compose 标签与 SnowLuma ID；自然流量缓存基线见 2.3 |
| 本次修订 | 根据 dsh/pi 源码、Claude Code 公开资料及用户复核收窄设计；steer 接入与恢复为已交付基础，阶段汇报不建立事项状态机 |

用户确认的目标：历史保留按时间关联的聊天记录和当时必要的动态资料快照；长任务先用 `send_message` 交代目标与下一步，再开始实质执行；重要环节主动报告；用户询问或改向经 steer 接入后及时用 `send_message` 回应，并继续原工作。仅增强提示词不足以作为交付，机制也不能变成每一步都要发言的僵硬流程。

实施时先重读[共同架构约束](development-contract.md)、[主 Agent 合同](main-agent-runtime.md)、[Conversation Rollup](conversation-rollup.md)、[持久工作者](persistent-subagents.md)、[Tool Kernel](tool-kernel.md)及[Provider 合同](model-providers.md)。本文是特定基线的修复规格，实施状态见交付记录，不覆盖现行合同；政策改变须与实现同时更新合同。

## 2. 前序工作与实施前缺口

### 2.1 已有基础，不重复重构

| 记录 | 已完成基础 | 本轮处理 |
| --- | --- | --- |
| [PR #213](https://github.com/YuanYeYouTao/Yuki/pull/213)，`b1382cf` | SQLite 写前准备、诊断异步化、输入准备让出、既有效果保留 | 继续遵守短事务、来源复核及前台容量；不重做延迟改造 |
| [PR #214](https://github.com/YuanYeYouTao/Yuki/pull/214)，`fcc02e8` | 共享 `YukiRuntime`、主入口、root/child 激活骨架、关闭与恢复所有权 | 复用同一执行循环，不增设汇报 Agent 或另一套长任务 runtime |
| [PR #215](https://github.com/YuanYeYouTao/Yuki/pull/215)，`ffca08a` | 最近 Work 视图、`task_control.get/list`、终态可查询 | 追问按原 Work 查询，不把进度询问接纳成新任务 |
| [PR #216](https://github.com/YuanYeYouTao/Yuki/pull/216)，`ea446d6` | 原结果与未决效果保留、持久分页压缩、完整请求容量、无限累计默认值、真实子任务并行、Gemini 普通前缀保留 | 补实际漏接及交互反馈，不恢复旧步数上限或通用阶段完成门禁；一般 queued 间隙补齐不能据此认定已交付 |
| [PR #217](https://github.com/YuanYeYouTao/Yuki/pull/217)，`513a263` | schema `0088` 与 `ops-ea446d6` 部署、原 Work 对账和未决投递记录 | 属于历史部署证据，不代表本轮修复已经上线 |

前序任务书：[共享持久 Runtime 主干重构](persistent-runtime-refactor-taskbook.md)、[长任务 Harness 与上下文压缩重构](long-task-harness-compaction-taskbook.md)。对应[Runtime 实现记录](../operations/persistent-runtime-20261001.md)与[Harness 交付记录](../operations/long-task-harness-2026-10-01.md)明确区分离线回归、部署健康与真实 QQ 验收。

前一会话先观察到部分稳定前缀，随后发现持久投影没有更新并修正归因。因此不能沿用“缓存差距主要在上游、Harness 已完整验收”的早期结论。历史 Gemini/DeepSeek 缓存比例只是不同时间与请求的对照；本任务不以 97.5% 为正确性门禁，也不制造保温调用。

### 2.2 编制基线确认的缺口

| 编号 | 证据与边界 | 修复目标 |
| --- | --- | --- |
| H1 | `services/chat.py::_context_validator` 校验后仅在 `control is None` 时调用 `commit_projection`；普通聊天开启 Work runtime 也可持有尚未接纳任务的控制对象 | 是否提交由请求来源和准备结果决定，不能由控制对象是否存在决定 |
| H2 | `MainAgentTurnService.compose` 已有 `append_current` 和投影回调；`_run_prepared` 又追加运行状态。`AgentRunner.prepare_dispatch` 是实际 admission 后、模型派发前的边界 | 明确冻结本次获准普通输入及必要状态的范围；不能漏掉实际追加的资料，也不能把私有执行尾部全并入普通群史 |
| H3 | `test_gemini_history_prefix.py` 使用 `make_settings(database.url)`；`Settings.runtime_work_enabled` 默认 False | 现有七请求回归没有覆盖线上开启 Work runtime 的组合；需真实入口、配置开启、SQLite 重开与实际 serializer 回归 |
| C1 | `prompting/contracts.py` 的开始说明仍是“按需要”；阶段报告与子任务汇报主要为静态指引 | 开始说明补窄的顺序控制；阶段报告保留模型判断，在已有安全点提供事实与有限反馈，不建立阶段事项登记 |
| C2 | `WorkControl.take_inputs/confirm_inputs` 将输入 staged/consumed；Runner 在模型响应后 consume | consumed 证明输入的协议处理，不证明模型理解、答复或用户收到；沟通结果必须另关联真实发送回执 |
| C3 | `MainAgentBackend.response_feedback` 的直接用户漏发纠正排除了已有活动 Work；已有发送尝试也能绕过普通聊天纠正 | Work 内的新询问需要自己的答复关联，不能让很早的开工消息或失败发送替代它 |
| C4 | Runner 无工具响应可进入 Work 收尾；`complete` 的部分判断读取任意已送达消息 | 核查开始/阶段/steer 答复与最终交付的混淆；传输成功不证明目标完成，不自动把一次汇报算成任务结束 |

H1/H3 是编制基线的静态漏接，C2/C3 是当时保证的缺口，C4 是收尾核查项。修复与验证见[交付记录](../operations/history-interaction-harness-2026-10-02.md)，不把静态风险全部写成已发生的事故。

**steer 不是本轮待新建功能。** #214 已统一活动绑定和原 Work 恢复，真实匹配输入的来源、媒体准备、staged/consumed 和完成 CAS 继续复用。后续源码核查未确认任意 queued 间隙聊天可自动绑定；不能把该历史设计或定向测试当作一般群聊补齐已交付。本轮交互修复在真实接入边界上核查答复证据与回答后的原任务续行。

### 2.3 最后全量核查：复用、强化与局部重构

三个子 Agent 分别核对历史/协议、工作/回执、执行循环/验收，主 Agent 复核部署与自然流量。核查结论：

| 分类 | 现有机制 | 本轮实际切口 |
| --- | --- | --- |
| 直接复用 | PreparedHistory、FrozenFragments、epoch/revision CAS、actor/read-scope 与隐私隔离 | 使用原不可变准备及短事务提交；聊天账本不复制，持久线索来源由新上下文任务书定义 |
| 局部重构 | TranscriptRequest/validating_request、WorkSession.restore | 标识实际提交来源；未决协议核原 journal，正常业务续跑提交当前合法视图，不能混接旧 opaque 或错误 composition 闭包 |
| 强化 | 运行状态组装与普通历史提交闭包 | 冻结 fresh 普通轮获准的初始运行状态；活动 Work 的目标/工具/steer/签名继续私有，不把所有可序列化尾部共享 |
| 直接复用 | WorkSession.execute、prepared/accepted effect、Social 分条回执及 uncertain 围栏 | send 参数中的本地 report 复用原记录；网关消息字段已有筛选，不扩展平台协议 |
| 强化 | 原 effect outcome 与索引查询 | 派发前保存合法用途，实际回执后补传输事实；start/final 查询不依赖最近 64 条展示缓存 |
| 局部重构 | WorkRepository.checkpoint 的替换写入 | 只保留/更新有界 communication 子路径，wait/need_input/fail 不擦除 reporting 或纠正标记；不开放模型任意覆盖 checkpoint |
| 直接复用并强化 | Coordinator 原序串行及并行 READ 段；Runner 独立 Work-control 分支 | 开始门在 effect/执行计量前返回未执行结果，覆盖 subagent 委派分支；多个拒绝只计一批纠正 |
| 直接复用 | 已有 steer 接入、准备、staged/consumed、等待与完成 CAS | 只补输入批次答复机会，原输入语义、路由和恢复不变 |
| 强化 | Runner 原配对/追加反馈与 Work complete | 合并有限提醒、原身份下恢复；interactive 不由无工具正文自动完成，最终用途与真实投递分别核对 |
| 直接复用 | 固定主/子合同、合同 hash、新链边界、现有实际 serializer 测试 | schema 增量在部署时统一冻结；合同变化一次显式新链，不每轮改工具或系统前缀；通用 Runner 合并声明保留原对象和顺序，只追加缺项，删除 continuation 后重排序/覆盖旧声明的路径 |

2026-10-02 03:48（Asia/Taipei）的只读基线：上一版部署后成功 chat_agent 59 次，全部属于 `connection_2d4ba9aad301`；53 次 cache 已知、6 次未知。已知 input 2,042,270、cached 1,793,068、未缓存 249,202，输入加权已知命中率 87.80%；未知全按 miss 的保守下界 79.18%，不代表实际 miss。所有现存 prompt_projections 均为失效占位，符合 H1 的静态缺口；不证明每个缓存 miss 都由 H1 导致。其他层的不同样本窗口只能检查计量缺失口径，不能直接与 Bot 比命中率。

实现优先采用上述最小切口；若新字段只放 WorkSession.progress，合同/来源新链可能丢掉标记，因此不得以一次普通重启测试替代跨链恢复验收。工具合同升级有一次预期的新链/冷请求成本；先核对完整旧输入、tools/native_tools 和设置在后续链内稳定，再按同 Profile 自然请求评估命中与未缓存 token。

## 3. 外部 Harness 的观察与本轮取舍

采用可验收的交互行为作为目标：执行前让用户知道下一步；有实质成果、阻塞或方向变化时更新；执行中可接入追问和更正；回答插话后继续原目标；压缩、分段和重启后不重复效果或遗忘尚待答复的输入。

本次核对 dsh `639ed015397290b3745d163aafe02ffee4aa3f84`、pi `bc2d8dc1c46c50f2c6a0f3237e6a2453817e51a1` 的公开源码。以下是检查路径的证据，不把核心循环的观察扩大为全部插件或全部模型的保证：

| 对象 | 实际机制 | Yuki 的取舍 |
| --- | --- | --- |
| [dsh 执行循环](https://github.com/deepseek-ai/deepseek-harness/blob/639ed015397290b3745d163aafe02ffee4aa3f84/packages/core/agent-loop/src/agent.ts#L425-L540) | 输出 chunk 先发布给界面，assistant 消息提交后执行工具；steer 进入 next-step | 复用消息/工具交替循环与已有安全点；不另设汇报执行线程 |
| [pi 执行循环](https://github.com/earendil-works/pi/blob/bc2d8dc1c46c50f2c6a0f3237e6a2453817e51a1/packages/agent/src/agent-loop.ts#L176-L295)、[默认规则](https://github.com/earendil-works/pi/blob/bc2d8dc1c46c50f2c6a0f3237e6a2453817e51a1/packages/coding-agent/src/core/system-prompt.ts#L85-L124) | 文本增量产生 message_update；响应后执行工具、再取追加输入；检查路径未见通用的执行前发言门禁 | 何时汇报由模型判断，不能宣称开源工具普遍用硬门禁保证主动发言 |
| [Claude Code 消息流](https://code.claude.com/docs/en/agent-sdk/streaming-output)与[steer 说明](https://code.claude.com/docs/en/how-claude-code-works#interrupt-and-steer) | 中间 AssistantMessage 与最终 ResultMessage 分开；文本块后可以执行工具、再输出文本；工具结束后同轮接入排队更正 | 一次发言不代表任务结束；公开资料不足以证明其内部采用某种发言状态机 |

这些工具可直接显示 assistant 文本，Yuki 只通过显式 `send_message` 外发。不能把 UI 的流式展示照搬为 QQ 自动转发，也不能把工具日志、思考或内部正文当作进度消息。长阻塞工具中的状态展示与模型的自然语言发言是不同机制；需要交流时复用既有后台执行和 run_id 查询/等待，而非再启动同一 Work 的 Agent。

既有任务书引用的 OpenAI 资料仍作为交互目标背景：

- Codex 的 Queue 留到下一轮，Steer 注入正在进行的工作。Yuki 保留现有群聊接入方式，不照搬移动端 UI。[OpenAI Docs：Queue 与 Steer](https://developers.openai.com/blog/mastering-codex-remote-for-engineering)
- OpenAI 区分 assistant 的 `commentary` 与 `final_answer` 并建议在历史中保留该区别。Yuki 对应“阶段沟通与最终交付分开”，不能据此自动转发模型正文。[OpenAI Docs：消息 phase](https://developers.openai.com/api/docs/guides/deployment-checklist)
- 输入被接受不等于已被模型应用；steer 不撤销先前效果。Yuki 应保留接入与处理边界，不因插话重跑命令或重置预算。[OpenAI Docs：Mid-turn steering](https://developers.openai.com/api/docs/guides/steering)

以上支持交互设计，不证明 Codex 内部存在某种汇报状态机。当前 Gemini/DeepSeek 路由不因此获得 OpenAI 原生 steer 或 phase 能力。本任务不引入 Responses WebSocket、不换 Provider、不修改人格配置，也不借 `tool_choice=required/指定工具/none` 强制行为。借鉴责任分工与行为，默认不复制外部实现；如实施确需复制源码，另核所复制文件的许可证与署名要求。

## 4. 历史快照的修复合同

### 4.1 按当时输入保存，按当前权限读取

逻辑上下文保持：

```text
固定系统与工具合同
会话摘要及其覆盖、来源和缺失说明
冻结的获准历史：旧聊天 + 当时已提交的必要资料快照
本轮追加：当前必要资料 + 当前聊天事件
原执行私有尾部：工具回合、steer、Provider continuation
```

“历史”包含两个不同事实：原始账本记录发生了什么；冻结输入记录当时为模型准备/提交了什么。快照不是群成员说过的话，也不是保证正确的长期 Memory。将快照明确标为当时的观察/读取结果，当前资料明确标为本轮状态；新结论和更正追加，不能用最新昵称、关系、short_state 或任务目录重写旧块。

优先复用已有事件时间、内部 event/input/execution ID 和运行元数据建立关联。迟到事件注明发生时间与首次观察顺序；不为了日期排序插回已冻结前缀。快照的出处与时间必须可解释，不每轮复制整个数据库。摘要不得把历史快照中的未验证陈述升级成已经证实的事实。

冻结在派发前完成，仍可能遇到网络失败或取消。准备成功不等于模型确实看见，模型响应不等于用户收到。使用原 request/journal 状态说明证据范围，不用 prompt projection 证明模型执行或平台交付，不新造第三套请求回执。

### 4.2 接回原派发边界

实现先明确区分两种已有来源，而非新增聊天/工作分类器：

1. **新组装的普通输入**：携带不可变的准备结果和原投影提交动作。开启 Work runtime、拥有控制对象、在此轮随后 accept 都不取消应有的初始提交。
2. **原 Work 的恢复请求**：保持原执行身份和预算，先核对未决协议；正常业务续跑采用当前获准聊天及必要任务材料。不把旧 composition 回调套到另一份输入，也不把旧 opaque 拼到新的编排；当前事件只呈现一次。具体实施以 [Work 上下文任务书](work-context-and-chat-continuation-taskbook.md) 为准。

将准备结果的生命周期随原调用传到 `AgentRunner.prepare_dispatch`；源码实现可用小型类型/字段表达来源，不能根据 `control.current` 某个时刻的真假猜测整条请求的来源。不要仅删掉 `and control is None` 而缺少恢复隔离测试。

严格顺序：锁外完成来源与身份查证、冻结编码 → 实际模型 admission → 在原 guard 核对权限、generation、read/source revision → 短事务 CAS 提交获准投影 → 释放 writer → 原请求派发。冲突沿原来源变化/恢复路径处理，不丢异常后继续发旧请求。

重试复用同一不可变准备，不能重复 append、重复扣预算或替换并发较新投影。投影容量/图片/协议限制仍建立显式边界；实际权限核验和业务事实不因投影失败而绕过。

### 4.3 冻结范围与可见范围

- 对齐 `compose` 的当前资料 envelope 与 `_run_prepared` 的运行状态，逐项决定哪些是本次普通输入快照，哪些仅属 activation/Work。验收比较实际发送内容，不仅比较 compiler 初始 messages。
- 普通投影只保留原读范围认可的聊天和必要快照；每轮最新状态追加。完整工具正文、私有思考、签名与原生工具尾部仍归原 Work，不以格式可序列化作为允许共享的依据。
- 保留 per-actor/read-scope、Conversation、generation 和隐私删除边界。换人不继承前一人的私有快照；能看某个 Work 的安全目录不等于能读取它的 journal。
- 压缩、权限收窄、来源编辑/删除、Profile/合同变化、媒体表示或容量变化记录明确的新链原因。稳定前缀不得绕过当前授权。
- 没有保存的旧快照不能由当前资料或摘要回填成“当时原文”。必要时明确 bootstrap/缺失范围；诊断日志不是持久恢复权威。
- 本任务不把投影缓存升级为永久全量提示词档案。容量淘汰后只能保证账本事实可回查，不能承诺旧动态快照无限保存；需检查压缩/重建是否仍保留目标、关键更正和必要来源。若提出永久保留全部快照，应另列存储与隐私设计。

### 4.4 后续上下文修复

后续业务续跑、正文退出、普通同轮整理和观察覆盖统一由
[Work 上下文与普通聊天续接任务书](work-context-and-chat-continuation-taskbook.md) 定义。
删除保留 H0 再补聊天的旧实施方案，不同时维护两套要求。
软硬水位分离、当前聊天续跑和普通同轮整理已纳入后续实施；水位配置本身不证明这些能力或缓存收益。
历史政策值和技术证据不代表当前部署状态，实施、上线和真实验收分别报告。

## 5. 长任务沟通的最小 Harness 设计

### 5.1 分工和强度

模型判断任务是否需要持续交互、某项成果是否值得报告、怎样措辞、是否将相邻更新合并。Harness 复用原输入、journal 和发送回执，补首次执行顺序、关联答复检查与有界反馈。阶段成果不形成新的待办生命周期；不得通过关键词扫描消息或工具次数推断语义阶段，也不增加第二个分类/裁判模型。

| 情境 | 要求 | 约束强度 |
| --- | --- | --- |
| 已明确承担的交互式长任务开始 | 先 send_message 简述目标和下一步，再派发实质业务工具/长计算/子任务 | 一次明确顺序约束；准备和 accept 不算已经开工 |
| 实质阶段结果、重要阻塞、目标调整 | 提供证据、余项和下一步；可合并相邻相关变化 | 模型语义判断；已有事实可给一次提醒，不登记逐阶段事项、不设工具门禁 |
| 原任务内直接追问/方向更正 | 安全点优先处理，回应所问或确认调整，然后继续原 Work | 未决输入的沟通检查；不由 consumed 自动结清 |
| 短回答、短操作、自然闲聊 | 保留直接回答及原节奏 | 不强制开工宣告、计划、委派或多次消息 |
| 明确安静工作、SELF 合法沉默、delivery=none、子任务 return_to_caller | 遵守来源交付要求；子任务向父任务内部汇报 | 不因存在 work_id 强制群发，不增设发送渠道 |

“交互式长任务”是原 Work 上的一个可选 reporting 属性，不是另一种业务身份或 runtime。真实用户要求、可信交付合同与 Agent 的任务解释共同确定该属性；后端只验证来源、结构和原属性，不声称能够机械识别任意自然语言中的长任务。普通短 Work 可直接完成并交付；模型不能将明确的汇报要求改成静默来绕过顺序，违反语义要求要在行为验收中暴露。执行方案转成长计算、多步调查或委派时，在首次实质扩展前补开始说明。

### 5.2 只保存必要关联，不建汇报状态机

继续使用 `task_control`、`send_message` 和 `subagent_message`。本轮默认不新增工具、表、队列、ProgressManager 或通用阶段状态机。当前 `task_control` 没有可任意写入 checkpoint 的模型参数，不能开放整份 checkpoint 写入。

本轮最小参数增量如下；实际支持以代码、验证和部署状态分别确认：

| 已有工具 | 可选增量 | 作用与限制 |
| --- | --- | --- |
| `task_control.accept/update` | `reporting: interactive / quiet` | 在原 Work checkpoint 保存本任务沟通方式。省略保留既有开始/阶段节奏，不把所有 Work 强制变成长任务；quiet 不屏蔽新的真人追问。update 省略 goal 时只允许更新 reporting，不顺带改目标、解除等待或转为 running；初次可选择 quiet，之后 quiet→interactive 可增强沟通；已采用 interactive 的原 Work 不接受模型自行降为 quiet，避免绕过顺序或清除已有要求；空 update 仍拒绝 |
| `send_message` | `work_report: {kind: start / progress / reply / final, reply_to_event_ids?: [...]}` | 用途是 Agent 的声明；事件 ID 为内部账本 ID，后端核验是原 Work 来源的 trigger_event_id 或属于原 Work 的既有接入输入。数组最多 8 项，一次答复可关联多条输入；start/final 同样可关联追问。不向平台传输这段元数据 |

普通阶段汇报直接 `send_message(text=...)` 即可；不要求先 update、登记阶段、发送后再 update。`kind=progress` 是可选诊断标签，不驱动调度、完成或待办。无 Work 的普通发言保留原参数与行为。

发送用途和输入关联随原 call 参数在副作用派发前持久化，再由真实 effect/平台回执派生结果。对 start 的 Work 归属由执行上下文绑定，不让模型任意指定另一 Work。初始事件只存于 source_json 而没有 inputs 行时，按原来源核验，不为关联补造 WorkInput。模型提供的事件 ID 不能扩大权限；既非原来源又无 Work 输入归属时拒绝该关联，不能回退到正文或平台消息 ID 搜索。认可的答复目标来自原会话/交付合同，发往另一目标的成功不能因同属 Work 就计作本次说明或答复。关联失败在真实发送之前返回结构化未执行结果。

原 Work 只需保存 reporting、稀疏开始/阶段/退出反馈标记和追加输入的提醒水位。水位仅表示已给予一次提醒，不表示输入已回答、语义已解决或用户已收到。沿原有序输入批次推进，非连续输入用原批次身份，不靠最后一个 event_id 猜覆盖。已发送关联从原效果记录读取，不另写 delivered/unknown 状态副本，也不保存每条输入的回复/不回复决定。历史输入和要求继续归原输入日志与任务资料；压缩按原来源保留，不能靠展示窗口隐去要求。

字段在主工具定义、SDK 包装、Social 参数解析和 worker 子集统一核对；默认放在原 journal/checkpoint 和 effect 参数中，只有证据证明现有存储无法满足恢复一致性时才提出最小迁移。`_send_message_attempted`、本激活的 messages_sent 或任意旧发送不能作为开始/本次询问的确认。

实际发送已成功、后续 checkpoint 保存失败时，恢复先查原 call/effect 与发送用途，不能因反馈标记未清除而发第二遍。提醒水位和发送用途不能解除 unknown、来源限制或业务效果围栏。是否应该回应、答复是否到位由模型与真实行为验收判断，不再增加裁判模型。

声明变化按部署冻结新的主/子工具合同，沿既有新链边界升级；不在本轮运行中删除业务工具或临时缩成只有 send_message。非 OpenAI 协议用本地用途元数据表达，不伪造不支持的 phase 字段。

### 5.3 开始顺序与批次执行

开始说明使用 Agent 自己生成的内容和既有发送工具。采用 reporting=interactive 后，第一次可控业务派发检查原 start 发送证据；未处理时返回 executed=false 的顺序反馈，并将回执配对到同一 Runner。对原 Work 的同一开始缺口最多一次模型纠正，以一批响应为单位，不按被拒工具数量递增；标记随原检查点恢复。再次遗漏沿原无进展/错误退出规则收束，不无限轮询或重新唤醒催发。

采用两条明确的批次规则，保持模型调用与工具结果的原协议顺序：

1. start 发送在业务调用之前：协调器将其作为串行屏障，取得真实发送结果后按以下失败/未知规则决定是否放行业务；发送屏障不能混进并行 READ 段。
2. 业务调用在 start 发送之前：不偷偷重排调用。前面的业务调用返回未执行回执，之后的 start 可照常发送；模型看到配对结果后重新提出尚未执行的业务调用。没有 start 调用的批次同样拒绝首次业务派发。

登记/更新任务、合法查询原 Work 与本次沟通关联属于准备；其他 investigation/计算/委派等业务工具不靠“只读”标签绕过开始检查。顺序门仅作用于已采用 interactive 的原 Work，不影响普通闲聊、短 Work 和来源禁止外发的工作。

无需再次向用户申请工作批准。开始说明不宣称尚未执行的操作已经发生；此前已真实发送且能关联到原 Work 的说明可复用，重启/让出不重复开工消息。

此顺序控制可严格拦截客户端业务工具，包括当前 Gemini bridge 的本地搜索调用。Provider 执行的原生工具可能在响应返回前已开始，后端不能伪装为拥有逐次拦截权。原生工具 Profile 在派发前提供沟通机会，但其是否遵守仍需协议能力与真实模型验收；不能靠删除固定声明、强制 tool_choice 或偷偷换路由补出保证。T05 的确定性断言针对可控客户端工具，原生工具另记保证边界。

发送确认成功才可称为用户已收到。确定失败、权限拒绝、部分失败或未知回执均不满足成功屏障，停止本批后续依赖业务，将真实结果返回原 Agent 作出降级决定。已有该次结果不再被开始检查当成“未尝试”反复纠正；保留交付未确认，不自动重发、不找备用目标。后续安全准备严格限上文登记/元数据更新、合法任务查询等准备操作，不能把任意只读调查改名为准备。扩大继续范围必须符合原用户要求和来源交付合同，并经过原效果围栏；reporting 参数本身不授予此资格。用户明确要求“收到说明后再执行”时，保存原工作并停止依赖该要求的动作。

### 5.4 阶段汇报复用模型循环

阶段汇报继续由代码管理的行为合同约束：有实质发现、重要阻塞、目标调整时说明证据、余项和下一步。普通成果直接呈现在原工具结果中；子任务结果、真实失败/等待、权限或资源阻塞可在原结果旁追加一条沟通提示，不新增事件发现器、阶段目录或第二个模型请求通道。

提示在原结果配对后的安全点出现，和原证据一起进入同一请求链。模型可发送、合并到马上发出的最终交付，或继续执行；不要求解释每一次不发言。工具成功、计数增加、量子让出和压缩成功不是自动汇报触发器；不强制持久保存每个阶段的发送/延后决定。

对 interactive Work，本次响应只产生非空内部正文却没有本次相关发送时，可给一次非阻断提醒：“正文未对外发送；需要汇报请使用 send_message，内部结果或无需更新则继续原任务”。不靠正文措辞识别阶段，不由旧开工消息免除检查；NO_REPLY、内部返回产物及来源禁止外发的结果不强制发送。先保留原 assistant/continuation，再追加提醒；遇到 Claude pause_turn 等要求精确重放的协议轮，先完成原 opaque replay，新增输入和沟通提示延后到下一个合法安全点；不自动转发，不因普通阶段不发送阻断业务。该提醒和下节的新输入提醒及收尾反馈在同一响应合并；保留一个原检查点反馈标记，纠正响应再次只给正文时不重新产生同一机会。新真实业务结果/输入才允许新的机会，单纯新 request_id 不刷新它；interactive 的明确退出检查另按 5.6 执行。

普通阶段提示不形成硬门禁，不因模型没有汇报每个结果就将 Work 判 failed 或阻止工具。真正的阶段报告质量由行为样本验收；这一点与可确定测试的开始顺序保证分开报告。

不新增每 N 步/每 X 秒自动群发器。群聊刷屏控制靠语义合并和原事实去重；时间指标用于诊断与验收，不形成额外平台发送配额。

### 5.5 在已有 steer 上补答复与续行

接入与恢复已经交付，继续复用 `stage_work_input`、原输入日志及 source/generation/actor 验证。staged/consumed 仍表示原协议处理，不改其语义或游标；本轮新增关联仅说明发送的用途和来源。

```text
真实入站事件 → 原 Work 输入持久接入
            → 在下一可用安全点提供给原 Agent
            → Agent 决定回答、改向、补资料、取消或不需要回应
            → 必须答复时调用 send_message，并关联原输入
            → 后端从真实回执确认送达/失败/未知
            → 继续原目标；任务完成另行核验
```

新增真人输入在原安全点呈现时，明确提示“需要回复则使用 send_message 并关联 event_id；回答后继续原目标”。模型判断是追问、更正、资料还是无需发言，不要求所有群消息登记处理决定。信号通知与其他群成员的无关聊天不因接入就变为必须回答或新的授权。

本批新增真人输入展示时给一次答复机会；原响应结束、下一批可控实质执行或收尾前，若尚无相关发送，可合并给一次非阻断提醒。提醒沿原 journal 的输入批次水位恢复，consume 不等于答复，不每轮催发；模型可以回答、合并到交付或合理不答，无须另外调用 update 登记不答理由。提示中保留原事件引用，无法确认答复时只记为“无相关发送证据”，不伪造已答、不因此把所有输入变成必须结清的任务清单。已报告 failed/unknown 的相关发送保留交付受阻事实，不再次当成“未尝试”催发。

该答复机会适用于新 interactive、旧 Work、reporting 省略和 quiet Work 中的真实追加输入；本特性首次恢复旧 Work 时以原 consumed 输入水位建立基线，不倒追旧聊天催答，未纳入原 journal 的 pending/staged 输入不被跨过；已保存于原 journal 的 staged 按既有恢复规则转为 consumed，属于 legacy 已展示基线，不代表已经答复。quiet 只影响主动阶段汇报，不能覆盖用户新的明确询问。模型遗漏明确追问是行为失败，后端不能机械证明语义正确。一次很早的消息、不同目标的消息、仅发送尝试或失败回执不证明本次问题已答复。多个问题可以一条答，后来的更正不能无证据地删掉先前询问。

在下一批实质业务执行前优先给 Agent 回答机会；正在执行且不可安全中断的模型/原生工具/命令按原合同走到安全点。已经派发的操作不会因 steer 被撤销。长运行命令必须保留 run_id，通过原查询/等待取得进度，而非为回答用户另起同一命令。

输入在收尾检查后、状态提交前到达的竞态必须进入现有 CAS/未消费输入检查；不能把 consumed 或提醒水位记为已答证据。完成 CAS 保住尚未接入的迟到输入；对已经展示的输入，答复是否必要、是否到位仍由模型判断并列入行为验收，不增加逐输入结清门禁。新输入与完成并发时，不丢事件、不复活已结束工作、不重复 accept；终态问题可以在普通轮查询并答复。

waiting_external 原条件和多候选消歧继续复用；任意 queued 间隙聊天自动绑定不是已确认能力，一般聊天补齐由新上下文任务书处理。无执行中的 activation 时，不启动第二个持有同 Work 租约的汇报线程。可由原前台轮读取获准安全状态回答，但修改原目标仍走正式接入与授权。

### 5.6 汇报不是完成

开始、阶段汇报和 steer 答复不能自动充当最终交付证明。对 interactive Work，本轮内部无工具正文不能独自触发 completed；模型继续原工具调用，或通过已有 task_control 显式 complete/wait/need_input/fail 说明退出意图。缺少意图只给一次续行反馈；仍无进展按既有有界退出规则处理，不能后台永续空转。普通短 Work 和其他来源保持原收尾合同。

kind=final 只声明交付意图，不能单凭它认定完成；kind=start/progress/reply 则不能用作明确最终答复的回执。新 interactive Work 的明确最终答复要求须关联 final 用途及真实回执，不再读取任意已发送消息来满足它。带 work_report 的纯沟通发送不充当 state_change 的业务修改证据；真实业务、文件及合法静默仍分别遵循原交付合同，不一律强制新增 final 消息。未标用途的历史回执只保留其真实传输事实，不通过内容关键词倒推成某种用途。

收尾同时考虑原目标/用户更正、真实未决效果、未结束子任务、未处理输入、明确交付要求及沟通关联。只对已经登记的交付要求做客观核验，不恢复“所有模型计划阶段都必须打勾”的通用硬门禁，也不宣称后端能机械证明语义目标已达成。

子任务在重要环节用 `subagent_message` 向父任务报告，父任务决定对外整合。子任务没有 QQ 发送权限；一次并行委派可由父任务统一说明，不让每个子任务单独刷群消息。

普通聊天的 WorkControl 尚未 accept 时仍按普通轮处理：真实发送成功后遇到空响应可按原规则收束，此判断不能套用到已接纳 interactive Work。普通同轮整理沿现行主 Agent 合同，在完整配对安全点建立合法新请求；若真实容量仍不足，保留发送事实并准确停止，不重发、自动 accept 或把部分发送当作完成。窗口调整本身不证明整理质量或缓存收益。

### 5.7 正常路径与所需保证

下列参数展示本轮增量合同，省略现有目标/附件等字段；以实现、测试和部署状态分别确认支持：

```text
首次长调查：
  task_control(accept, goal=..., output_kind=answer, reporting=interactive)
  → send_message(text=目标和下一步, work_report={kind:start})
  → 按真实发送结果执行原业务工具

阶段发现：
  原工具结果 → 模型判断值得说明 → send_message(text=发现、余项和下一步)
  → 继续原业务工具；不先登记阶段、不另调用汇报模型

原 Work 内追问：
  已有 steer 接入 event_id → 原安全点展示
  → send_message(text=答复, work_report={kind:reply, reply_to_event_ids:[原事件ID]})
  → 继续原任务；确已完成时单独 task_control(complete)
```

正常答复不需要 update→send→update，不回复也不要求登记例外。模型报告内容的质量、是否选择 interactive、是否合理回应追加输入仍须语义验收。后端可确定保证的是可控工具顺序、关联合法性、真实发送状态、原身份恢复及不得由一次中间发言自动完成。

## 6. 实施切口与删除要求

| 切口 | 主要文件 | 交付 |
| --- | --- | --- |
| A：普通投影 | `services/chat.py`、`main_agent_turns.py`、`history_projection.py`、`conversation/frozen_fragments.py`、`projections.py`、Runner dispatch 边界 | 请求来源明确、Work runtime 开启仍提交、恢复隔离、实际动态快照完整且获准 |
| B：最小关联 | `runtime/work_control.py`、`work_session.py`、原 effect/输入查询、主工具定义、Social 参数解析与 SDK 包装 | 可选 reporting、原发送用途/输入关联、有限反馈水位；不建立逐阶段/逐输入处理台账 |
| C：原循环补丁 | `services/agent_runner.py`、`main_agent_backend.py`、`capabilities/coordinator.py` | 开始屏障、已有 steer 上的答复检查、interactive 明确收尾；阶段报告复用原结果与提示 |
| D：合同与验收 | 现行主/子/历史/Provider 文档、相关 tests、日期交付记录 | 准确记录保证强度、必要新链、缺样和未完成在线验收 |

A 与 B/C 已按明确文件所有权并行实施；A 的独立验证与 B/C 的交互验证分别成立，可以同一 PR 交付，不能用一个切口的成功替代另一个验收。删除以 control 是否存在决定投影、以曾经发送任何消息替代本次询问答复等错误判断。不要在旧类外包新汇报总管，或保留两套长期活跃投影/沟通写路径。B/C 不包含 steer 接入重构、任务目录重构、压缩器重构或子任务调度重构；发现相关回归只修实际破坏的边界。

删除上一版拟议的“每个阶段登记待沟通事项、每次发送/延后都持久决定、每批输入必须登记处理决定”。本版保留的稀疏关联不得在实现中重新膨胀为同类状态机。若新增字段不足以解决实际复现，先呈现失败路径与最小缺失事实，再调整设计。

旧 Work 保留原 ID、预算、输入与效果；不追补开工消息，但新接入的追问走本版有限答复机会。无法从旧回执确定某条消息用途时标明 legacy/关联未知，不靠正文关键词倒推，不批量补发开工或进度消息。若确需新 schema，迁移号以实施时 Alembic head 为准；新链/协议变更和数据库迁移分开验证。所有编码、外部工作和有界来源读取在首次 DML 前完成。

## 7. 验收矩阵

| 编号 | 场景 | 必须证明 |
| --- | --- | --- |
| T01 | Work runtime 开/关；开启但未 accept 的普通聊天 | 开启组合也真实提交投影；下一同 actor 请求包含原快照与新快照，当前事件只出现一次 |
| T02 | 首请求、accept 后、同链读工具/发送、重启与 HTTP 重试 | 原输入不重渲染；投影 revision 正确；模型/工具预算不重复；原 Gemini 签名留原链 |
| T03 | 恢复已接纳 Work；来源变化与投影 CAS 竞争 | 原未决协议先核对；安全业务续跑采用当前聊天，不混接旧 opaque 或旧提交闭包；真实来源冲突阻止失效请求，原目标、输入及回执保留 |
| T04 | 换 actor、权限收窄、删除、generation/Profile/合同变化、图片与容量边界 | 不泄露旧私密快照；必要新链原因真实；不伪造缺失的旧快照 |
| T05 | interactive 长任务；start 在业务调用前/后、没有 start、同批多业务与并行 READ | start 前业务不执行；屏障不被并行穿越；不重排调用；原协议配对完整；整批只计一次纠正并跨重启保留；原生工具限制单列 |
| T06 | 开始发送确定失败、拒绝、部分失败和 unknown；崩溃在成功回执后 | 失败/未知不满足成功屏障；安全准备不越白名单；模式变化不解除用户要求/unknown；不盲重发、不换通道、不重跑已执行业务 |
| T07 | 阶段成果、阻塞、子任务结果与目标调整 | 原结果与必要提示进入同一循环；不新增阶段登记/模型通道，静默不阻断工具；报告质量另用真实行为样本评价 |
| T08 | 新/旧、interactive/quiet/省略 reporting 的 Work 中询问、更正及连续 steer | 消费/提醒水位不替代答复；给有限非阻断机会；关联原 trigger 与接入输入均合法，不补造输入；回答后续原 Work，无新 accept/预算重置 |
| T09 | steer 在模型调用、长命令、分段间隙、压缩、重启及 complete CAS 期间到达 | 不丢未决询问、不反复回答、不重跑原命令；迟到输入与完成有明确结果 |
| T10 | 开工/进度/steer 回复后内部 final；最终产物附言 | interactive 无退出意图不自动 complete；有限续行、不永续空转；final 标签不证明语义完成；文件/附言不重复发送 |
| T11 | 短聊天、短操作、省略 reporting、SELF 沉默、quiet、return_to_caller、delivery=none、其他群成员；普通轮真实发送后空响应或续接容量不足 | 原开始/阶段节奏保留；不强迫规划/开工/群发，不要求逐输入回复/不回复登记；新追问机会另验 T08；元数据更新不覆盖目标或解除等待；中性 control 不多重试，容量不足不重发或假称完成，已接纳交互任务仍需明确退出 |
| T12 | 两个并行子任务、前台聊天同时运行 | 子任务汇报归父任务；主执行器唯一；前台容量保持；不重复创建 child |
| T13 | Chat/Responses/Claude/Gemini 实际 serializer | system、完整 tools/native tools、设置与旧普通输入正确；私有 continuation 按原协议保存，pause replay 不注入新尾部；反馈只追加。Claude 既有 cache_control 断点移动单列验证，不能声称全 raw 请求字节不变 |
| T14 | 有界元数据、非法关联、超长输入、未核对候选、保存失败及隐私删除 | 跨 Work/未获准事件/平台 ID 冒充/超过 8 项在发送前拒绝且无副作用；不复制所有输入决定；窗口不抹要求；实际发送后保存失败不重发；按原删除合同清理 |

测试必须让故意遗漏 send_message 的 fake 模型走真实 Runner 和回执路径，证明 harness 实际反馈或阻止错误顺序；仅测试新提示词含某段文字不算验收。涉及持久提交/竞态使用文件 SQLite WAL、独立物理连接、确定性交错和重新打开服务；禁止 sleep 猜测竞态。

以现有 `test_gemini_history_prefix.py`、`test_runtime_work.py`、`test_work_input_preparation.py`、`test_main_agent_entrypoints.py`、`test_work_delivery_ownership.py` 和 `test_public_tool_surface.py` 为切入点，按改动风险补回归。开发期只跑相关验证；最终 head 的必要 CI 一次完成，不重复无关全量检查。schema 变更才要求 fresh/升级/回退演练。

另做模型行为验收：长调查、包含修改的长任务、并行任务及运行中插话，记录 interactive 选择是否符合用户要求、开始发送顺序、实质报告质量、steer 答复遗漏/延迟、合理不回复、重复消息、提前结束和普通聊天体验。先固定带明确目标/更正/预期回应的场景，在隔离环境检查真实模型、工具和回执，再按授权观察自然 QQ 流量；不得为验收擅自发测试群消息。离线脚本正确不等于模型稳定遵守；真假线上样本与模型/路由/版本逐项标明。

## 8. 观测、部署与完成定义

诊断只记录原 Work/input/call/request ID、沟通用途、边界时间、关联回执、纠正次数及新链原因；不把资料正文和聊天内容写进常驻生产日志。真实回执为权威，trace 只作诊断。交互指标优先由已标注行为样本评估，生产侧没有语义标签时不伪造“必须汇报事项总数”。静默与平台交付受阻单列；开始顺序、响应延迟及重复发送分别统计，不以“发过消息的 Work 占比”代替交互质量。

缓存观察在 A 修复后，用同 Profile 的自然流量分别统计同链/新普通轮/压缩新链，比较实际最长共同前缀、完整工具与设置、未缓存绝对 token 和计量覆盖率。缺失字段保留未知，不能只靠 hash 或固定前 64 项认证完整前缀；不承诺某个跨供应商缓存比例。

本轮已获实施、PR 合并和上线授权；执行前核对当时镜像/Compose/schema，不因任务书旧的文档阶段说明再次请求批准。按现行运维流程保留 Bot-only、停写一致性备份、原 Work/输入/回执/预算对账及必要离线迁移；保留同机其他服务，不以本任务顺带换路由。旧 uncertain 投递不能被新汇报政策解除。代码回退不覆盖上线后新增消息或业务事实。

完成必须分别报告：代码与合同、定向验证、最新 head CI、PR/合并、实际部署，以及真实交互/缓存观察。A 修复通过只能声明历史投影漏接修复；B/C 通过可以声明开始顺序、答复关联和原任务续行的执行保障，不能声明模型已稳定主动报告所有重要阶段。具体能力验收由用户完成；本轮负责技术验证、部署对账和有界自然流量缓存观察，不为制造样本追加模型请求或群消息。没有可比新样本时缓存改善保持待观察。

本任务的完成目标是：必要的历史快照在原授权下正确冻结和追加；interactive 长任务在可控边界先说明再执行；阶段汇报依托原结果和模型循环；既有 steer 上的用户追问有答复核查且回答后继续原目标；重启、压缩与分段不重复执行或发送。不能承诺模型永不漏答、摘要无损或达到 Codex 相同水平；必须交付可靠的事实、执行顺序、有限反馈和可测的交互行为。
