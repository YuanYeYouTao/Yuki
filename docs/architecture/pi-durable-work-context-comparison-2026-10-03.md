# Pi Durable 与 Yuki Work 上下文设计对照

状态：2026-10-03，Pi 源码静态核查，未独立运行 Pi demo。Yuki 对应实施已随 PR #228 合并部署，证据见[交付记录](../operations/work-context-delivery-2026-10-03.md)；本文不证明真实群聊能力或缓存收益。

Pi 源码固定在 [`9fba660cf1caca0ade5bea72269352416e595a19`](https://github.com/earendil-works/pi/tree/9fba660cf1caca0ade5bea72269352416e595a19)，比较 [Work 上下文任务书](work-context-and-chat-continuation-taskbook.md) 与 Yuki 的实施。Pi 1.0 和实验性 Pi Durable 的状态见[官方发布说明](https://earendil.com/posts/pi-1-0/)。

## 结论与裁决

Pi 已提供持续会话、独立后台任务、持久输入队列、位置化提示词变化和明确压缩边界。借用这些机制及验收方法，不换语言、引入 Pi 运行依赖或另建通用 TaskEngine。框架本身不替应用决定群聊资料、研究原件、线索和外部发送回执策略。

下表链接均固定在上述修订；源码位置简称在后面的来源表展开。

| 本次问题 | Pi 实际做法 | Yuki 的决定 |
| --- | --- | --- |
| 恢复工作退回旧群史 | Conversation.parent 是历史祖先，owner 是任务归属；二者独立（types.ts285） | 采用职责划分：根 Work 沿当前主聊天继续，目标/预算/回执归原 Work，不回到创建时整份 H0 |
| 持久内容是否都进提示词 | Entry.data 与 Entry.model 分开，可只保存应用记录（types.ts318） | 复用聊天账本、观察来源、Artifact 和 effect，不另复制一份通用 Entry/状态账本 |
| 大原文是否自动只留线索 | 普通工具结果仍写入模型贡献；可截断、压缩或显式编辑（tool.ts439） | 保留本任务书：原件先外存，首次交回必要内容，模型选择有用线索，正文按需回读。这不是 Pi 默认已经提供的资料策略 |
| 内容怎样退出 | 不可变 ContextEdit 用 omit/replace 改模型贡献，原条目不改（context.ts67） | 借用原件与视图分离；使用现有公共投影和新私有工作区。删除请求中间内容仍损失其后的缓存，不称为无成本追加 |
| 多人输入怎样进入 | requestId 持久去重；busy 入 inbox；postTools 接 steer，回答结束接 follow-up（submissions.ts144、inbox.ts59） | 复用原 event/input 身份，不搬第二队列。当前激活的一般群聊增量已接安全边界，真实多人 wire/重开组合通过；最新全量、合并与上线仍分别核验，不把一般闲聊伪装成 steer 或自动唤醒信号 |
| 主 Agent 聊天而后台工作继续 | background 任务不使主会话持续 busy；示例用持久阶段和固定回报身份（23-subagent-background.ts61） | 继续使用唯一 YukiRuntime、原 Work/child 调度，不让普通聊天取消、恢复或重建原 Work |
| 崩溃后是否重做工具 | 意图先提交，保存和当前策略都 safe 才重跑，其他报告 interrupted（tool.ts84） | 借用故障注入矩阵，继续查原 effect/run/Social receipt。interrupted 不证明外部效果没有发生，不新加通用 replay-safe 开关 |
| 压缩是否删原历史 | 新摘要 head 指向保留段，原条目留存；cut 不撕裂工具配对（compaction.ts253） | 采用明确整理边界和配对验收；保留原协议存档、paid 页、来源及引用，不新增另一套 head 存储 |
| 后台压缩的竞态 | 先生成候选，边界放置；过期候选可判 stale（README Compaction） | 复用本轮候选/来源 CAS，补竞态测试；完整异步压缩调度不悄悄加入这次最小修复 |

## 缓存边界

Pi 位置化 system sections 只有在协议支持时才能原位追加。其 Gemini 适配先 collapseSystemMessages；折叠会把最新 sections 合并到开头（google-generative-ai.ts65、utils/transcript.ts108）。动态 section 改变仍可能改头；Yuki 固定系统和完整工具、动态资料放尾部的合同应保留。不要照搬当前时间 system section 或动态工具策略。

Pi Gemini usage 把未报告缓存视为 0，并从 input 扣 cached（google-generative-ai.ts231）。Yuki 保留 NULL 与显式 0，按原 Provider 字段含义算分母。官方 cache-affinity E2E 检查可用性，不是多人群聊自然命中率基准。

还须区分应用会话和供应商缓存亲和字段。`ai/openai-responses.ts334` 的 `prompt_cache_key` 来自调用者 `options.sessionId`；测试显式提供这个值，却只断言请求成功，不断言命中 token 或命中率。固定修订的 Durable `ConversationStreamOptions`（types.ts350）不含 `sessionId`，其默认 generation 直接传这组选项，未自动把 Conversation ID 设置成缓存键。自定义 ModelRegistry 可以另外处理，但不能据此声称 Durable 默认已经做了这种绑定。Yuki 对照须实际检查同一会话逐轮累积的 messages 与 Provider wire；不向不支持的后端强加缓存字段。

## Durable 源码的具体提交顺序

以下核查针对 `packages/durable`，不是 coding-agent 的 SessionManager 或普通 agent-loop。

1. `generation.ts165–183` 在提交提示词变化后保存 `request` 检查点：模型、thinking、streamOptions 和历史截止条目 `cutoff`。`185–211` 按这个截止点读取上下文；同一请求准备后新到的聊天不会被偷偷塞进其历史中段。`context.ts19–39` 在 Session 串行边界捕获 head/tail，随后在边界外扫描不可变条目，避免持锁扫描整份历史。
2. `generation.ts542–585` 一次提交 assistant 条目、工具调用顺序和待处理位置。各工具完成时提交自己的结果；`593–642` 等这一轮工具全部终态后运行 `afterTools`，再一次提交 `postTools` 输入接纳、原 run 输入关联和下一次 generation 交接。由此新输入在完整工具往返后接入，不插进未完成的 call/result 之间。Yuki 借用这个边界，在原 Runner 上补齐一般群聊增量，不另建 generation 状态机。
3. `compaction.ts107–160` 先选保留条目，保存 `tail`、`firstKept`、模型、选项和尝试号；摘要阶段再按保存的 tail 读取同一不可变范围。摘要使用独立 system/user 请求，并明确设置 `cacheRetention: "none"`；它不负责把主聊天缓存预热。后台候选仍须在放置边界检查是否过期，不能把后来变化的历史认证成旧摘要来源。
4. 稳定截止点不等于扩展生成的整个请求都自动冻结。`generation.ts198–202` 在恢复请求时仍调用 `beforeRequest`；`docs/spec.md2514–2551` 明确未完成消费提交的 hook 可能重跑，替换只用于当前请求。需要稳定的外部选择应由 hook 使用任务 memo 固定。Yuki 保留原已派发协议及不透明字段，不能以“Pi 会恢复”为理由重新生成已经付费或产生效果的请求。

另一个局部清理机会是 Yuki 正常业务恢复仍可能先 hydrate/decode 原协议，再舍弃旧 H。当前结果已经沿当前 H 恢复，剩余是读取成本；精确恢复、发送收口、child 和 paid staging 仍需要旧协议。可参考 Pi 的阶段检查点以后按需读取，不能为删代码通删这些分支。本次不因此增加第二份状态账或阻塞已有修复。

## 直接借用的验收

1. 忙碌边界组合：多个 steer、一般群聊和后续输入，检查 call/result 配对、实际观察顺序、原 input 消费及重开后恰好一次。参考 harness-inbox.test.ts203；不搬 writes-first 的跨类型重排。
2. 重开与去重：原输入身份和已完成结果不变。参考 harness-submissions.test.ts179，映射原 event/input/effect，不另造 requestId 系统。
3. 编辑、压缩和恢复：应用原件仍在，模型选取改变有明确边界；候选不重新认证已变化的旧来源。使用真实 Main、SQLite 与 Provider wire。

这是设计和测试思路借用，未复制实质 TypeScript 代码。Pi 为 MIT；若以后复制实质代码，随代码保留版权与许可证声明。当前不引入 Pi 运行依赖。

## 固定源码来源

- [Conversation、ContextEdit、EntryRecord](https://github.com/earendil-works/pi/blob/9fba660cf1caca0ade5bea72269352416e595a19/packages/durable/src/types.ts#L285)
- [模型上下文推导](https://github.com/earendil-works/pi/blob/9fba660cf1caca0ade5bea72269352416e595a19/packages/durable/src/harness/context.ts#L67)
- [请求截止点与工具轮次交接](https://github.com/earendil-works/pi/blob/9fba660cf1caca0ade5bea72269352416e595a19/packages/durable/src/harness/generation.ts#L165)、[恢复 hook 合同](https://github.com/earendil-works/pi/blob/9fba660cf1caca0ade5bea72269352416e595a19/packages/durable/docs/spec.md#L2514)
- [工具恢复和结果提交](https://github.com/earendil-works/pi/blob/9fba660cf1caca0ade5bea72269352416e595a19/packages/durable/src/harness/tool.ts#L84)
- [输入持久去重](https://github.com/earendil-works/pi/blob/9fba660cf1caca0ade5bea72269352416e595a19/packages/durable/src/harness/submissions.ts#L144)、[边界入队](https://github.com/earendil-works/pi/blob/9fba660cf1caca0ade5bea72269352416e595a19/packages/durable/src/harness/inbox.ts#L59)
- [工具配对压缩切点](https://github.com/earendil-works/pi/blob/9fba660cf1caca0ade5bea72269352416e595a19/packages/durable/src/harness/compaction.ts#L253)
- [后台子任务示例](https://github.com/earendil-works/pi/blob/9fba660cf1caca0ade5bea72269352416e595a19/packages/durable/test/examples/23-subagent-background.ts#L61)
- [Gemini 系统折叠](https://github.com/earendil-works/pi/blob/9fba660cf1caca0ade5bea72269352416e595a19/packages/ai/src/api/google-generative-ai.ts#L65)、[折叠实现](https://github.com/earendil-works/pi/blob/9fba660cf1caca0ade5bea72269352416e595a19/packages/ai/src/utils/transcript.ts#L108)、[缓存计量](https://github.com/earendil-works/pi/blob/9fba660cf1caca0ade5bea72269352416e595a19/packages/ai/src/api/google-generative-ai.ts#L231)
- [Responses 缓存亲和字段](https://github.com/earendil-works/pi/blob/9fba660cf1caca0ade5bea72269352416e595a19/packages/ai/src/api/openai-responses.ts#L327)、[Durable 请求选项](https://github.com/earendil-works/pi/blob/9fba660cf1caca0ade5bea72269352416e595a19/packages/durable/src/harness/types.ts#L350)
- [忙碌输入顺序测试](https://github.com/earendil-works/pi/blob/9fba660cf1caca0ade5bea72269352416e595a19/packages/durable/test/harness-inbox.test.ts#L203)、[重开去重测试](https://github.com/earendil-works/pi/blob/9fba660cf1caca0ade5bea72269352416e595a19/packages/durable/test/harness-submissions.test.ts#L179)、[cache-affinity E2E](https://github.com/earendil-works/pi/blob/9fba660cf1caca0ade5bea72269352416e595a19/packages/ai/test/openai-responses-cache-affinity-e2e.test.ts#L5)
- [MIT 许可证](https://github.com/earendil-works/pi/blob/9fba660cf1caca0ade5bea72269352416e595a19/LICENSE)
