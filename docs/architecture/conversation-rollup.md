# Yuki 3.8 canonical Conversation Rollup

Rollup 是 canonical Conversation 的可重建 Prompt 投影。`chat_events` 始终是唯一原始证据源；
摘要不写入 Memory，也不拥有 Conversation。

## Conversation 身份

- 私聊：一个 Person 对应一个 canonical private Conversation。
- 群聊：一个 Space 对应一个 canonical space Conversation。
- 多个历史或 Provider alias 可以指向同一 Conversation，primary alias 首次创建后固定。
- Presence、Provider、GatewayConnection 和当前发言 Binding 都不参与 Conversation 身份。
- 只有显式 `/ai new` 改变 ConversationGeneration。

MemoryPartitionKey 使用 SELF、PERSON、GROUP 或 PERSON_GROUP owner，不得用 Conversation UUID
代替。

## 持久状态

每个 canonical Conversation 至多有：

- 一个语义 Rollup checkpoint。
- 一个 emergency overlay。
- 一个 signal-only Rollup job。

这些行都可从事件账本重建。job 使用 signal revision、owner、lease token 和 expiry 处理并发；模型
调用期间不持有数据库事务。提交前必须重验 Conversation generation、来源 fingerprint 和 lease。

## 连续覆盖

Rollup 只覆盖当前 generation 的连续前缀：

```text
starts_after_event_id <= effective_coverage <= last_event_id
raw tail = (effective_coverage, snapshot.last_event_id]
```

checkpoint 与 raw tail 不能重叠或留洞。当前触发事件只在 current message 出现一次，不得同时
进入历史。duplicate/suppressed canonical event 不参与候选或 Prompt。

后台模型只返回纯文本，不开放工具。旧摘要、新事件、外部事件和 visual observation 都放在明确的
不可信 input envelope。模型 timeout、空响应、超长或质量失败可以写 emergency overlay；overlay
不能覆盖或伪装语义 checkpoint。

## Prompt 字符与事件预算

3.8.1 明确区分三套不能互换的尺子：

- **前台历史尺子**：只计算 `event_kind=message` 的分组 `main_agent_history`；外部事件为零。
- **持久水位尺子**：`uncovered_event_count` 仍统计所有未覆盖 keeper，
  `uncovered_character_count` 只累加逐条 message 投影字符；它不依赖相邻分组。
- **压缩来源尺子**：按真实 `rollup_source_projection` 序列化成本切分，外部事件在这里有成本，
  不得因为前台为零而绕过 `batch_max_characters`。

触发、protected tail 和前台 fit 使用可见 message 投影；候选覆盖仍沿原始 keeper ID 连续推进。
主 Agent 的消息首行显示内部事件时间的本地 `时:分:秒`；相同发送者等既有分组条件下，
组内事件距首条最多五分钟，跨本地日期或事件时间倒退时另起组。历史仍按内部事件 ID 排序。
这个时间包装只用于模型可见消息；持久水位继续使用不带时间的单条事件尺子，
已冻结的旧模型输入不追溯改写。
3.8.0 存量计数必须在停写副本和 live 数据库上执行
`qq-ai-bot-cli conversation recount-uncovered` 后才能由 3.8.1 恢复写入。

事件 floor 和字符预算共同决定 protected tail：

- 长消息先碰字符上限时，允许保留少于事件 floor 的尾部并压缩更早前缀。
- 大量短消息受事件 floor 保护时，Prompt 可暂时超过字符 target。
- target 是压缩目标，不是 fail-closed 上限；最终是否需要前台压缩使用 admit/trigger。
- 不能因为 target 过小就反复 fallback，也不能为了压回 target 丢掉受保护尾部。
- protected tail 取最近 N 条可见 message；夹在这些 message 之间或之后的 external keeper 随后缀
  一起受保护。位于 eligible prefix 的外部风暴仍可触发压缩，但不能跨过受保护消息切 batch。

模型生成预算与摘要字符上限独立。`conversation_rollup_max_output_tokens` 默认 16384，包含推理
和最终正文；`summary_max_characters` 继续限制落入历史前缀的摘要正文，不能靠增大正文换取
推理预算。若 Profile 配置了 `max_output_tokens_limit`，共享执行器在发送前拒绝超限请求；
未配置 Provider 上限时该上限未知，不推测一个硬编码模型限制。超长、无正文、纯 reasoning 和
不完整响应不能写入语义 checkpoint，错误类别及 token/耗时日志不含正文或推理内容。

`conversation_rollup_model_timeout_seconds` 默认 90 秒，同时设置 Rollup 的 Provider 客户端超时
和单次排队/模型执行的总等待上限；聊天及其他后台任务沿用各自 Profile 超时。
达到触发水位后的 Rollup 使用 maintenance 优先级：全局最多一个保护中的维护模型调用，
共用原全局容量，普通前台不取消它，剩余容量允许其他会话聊天。exclusive 操作可以取消维护请求。
低于触发水位不启动后台语义请求。

前台超过 admit 时，所有批次共用一次有界等待期限。优先等待同 Conversation/generation
的现有 claim 提交；没有活动 claim 才以 required 身份执行语义压缩，期间维持 heartbeat。
失败、已有失败退避或等待超时后才使用应急 overlay。超时先取消本地请求，并最多再等 5 秒
让原 worker 完成持久提交；无法确认释放时失败关闭，不与原提交竞争。未超过 admit 的会话
不会全局停聊。普通聊天不再抢占语义 Rollup 的租约。

前台压缩必须有界。达到 trigger 后压向 stop，重新读取一致 snapshot；来源缺口、计数漂移或
压缩后仍超过 admit 时失败关闭，不拼接不连续摘要。

## Prompt 顺序与缓存

Provider 输入顺序固定为：

```text
TRUSTED STATIC INSTRUCTIONS
UNTRUSTED ROLLUP SUMMARY INPUT
CANONICAL RAW HISTORY INPUT
CURRENT ACTOR DYNAMIC ENVELOPE
CURRENT MESSAGE
```

Rollup 永不进入 system instructions。昵称、群名片和正文来自落账时事件；当前 Actor 的关系、
Memory、权限和动态插件资料只进入当前 envelope。

插件通知以 canonical `external_event` 落账，不作为普通 user/assistant/system history。前台完成
history fit 后，最多追加一份有条数和字符上限的 `recent_external_events` digest；source、plugin、
type、时间、summary 都在 `content_trust=external_untrusted` 载体内。既有稳定 `CORE_CONTRACT`
统一约束插件资料不得授予权限，不再存在插件专属 system policy。当前通知触发 Worker 时，Worker
必须唤醒正常 Main Agent，而不是建立独立短上下文或特殊 Agent。它复用与普通聊天相同的
Conversation snapshot、有效 Rollup、canonical raw history、Memory、工具 schema、模型 profile
和 Prompt compiler；当前 external event 只在最后一条临时 user input 出现一次，不能再从 recent
digest 重复注入，也不写回 history。

插件主动回复以普通 outbound message 保存，并用 `caused_by_event_id` 指向来源 external event。
历史分组键包含 origin class 与 cause ID，因此主动消息不会被并入上一条普通 Yuki 回复，不同事件
触发的主动消息也不会互相串联；同一事件的多个发送 part 可以组成同一 episode。模型历史与
`rollup_source_projection` 使用有界因果标签，平台实际正文保持不变。

external append 的前台 Prompt 字符增量为零，也不以字符阈值 `force_existing` 唤醒已有后台 job；
但 keeper 事件计数仍推进。digest 必须在最终 coverage/raw-tail 确定后重新选取，不能从切窗前的
过期 tail 复用。压缩输入只包含有界 summary 与来源元数据，不包含 external payload。

pending/processing WakeupRequest 对其 `source_event_id` 建立 coverage hold：Rollup 的提交水位必须
严格小于同一 Conversation 最早活动 source ID。候选选择和最终事务提交都要复核该 hold；模型调用
期间出现新任务时，旧候选必须回滚。completed、silent、cancelled、abandoned 或 terminal failed
会解除 hold，保留的 signal job 随后继续推进。该机制不改变 generation，也不改变普通聊天的
protected-tail 规则。

缓存诊断只能记录不含正文的 prefix/request-shape/snapshot hash，以及归一化 Provider 请求的
instructions、tools/native tools、input-without-current-tail 与完整 cache-shape hash。这些 hash
覆盖实际 model/profile/protocol、reasoning、output limit 和 response format；不发送给模型，不作为
业务身份，也不进入高基数 metrics label。

## generation 与效果围栏

每轮捕获不可变 Conversation snapshot。每次模型请求、工具、回复、语音、图片和插件副作用前都
重验 generation；外部效果通过进程内 EffectGate 获得一次性 permit。

后台通知 turn 在 claim 后、模型调用前和 finish/side effect 前至少三次复核 generation、授权目标、
Principal 与 immutable notification identity。直接文字/媒体通知不依赖 Conversation generation；
只有确认平台发送成功后写 outbound message，过期的 Agent reply 不得落账。

`/ai new` 在同一事务中写入命令事件、增加 generation、更新边界并删除该 Conversation 的
checkpoint/overlay/job。已失去 generation fence 的旧结果不能提交。

同一 SQLite 数据库只允许一个主动 Bot Application。SQLite CAS 测试不能替代跨进程外部效果
线性化；禁止双活实例同时写同一数据库。

## 运维与恢复

Rollup 健康应区分 backlog、processing lease、model failure、policy-ineligible、overlay 和
source mismatch，不输出正文。重建或维护命令必须默认 dry-run，并受 Control Plane capability
和审计约束。

Rollup schema 属于 canonical database，当前版本以 schema guard 和迁移链为准。
代码回退保留新写入的事件和工作回执；旧数据库恢复须单独评估数据损失并停止所有写入。
跨模块约束见 [开发合同](development-contract.md)。


## 压缩变化后的聊天补触发

未登记工作的聊天若在组装历史或模型调用前因来源变化中断，保留原快照的语义摘要与
emergency overlay revision。只有确认摘要版本改变、generation 与 reset 边界未变，且
原轮次未开始任何变更工具调用时，才允许补触发。其他来源修改、普通取消不进入此路径。
已登记工作仍由持久工作恢复接管，不重新执行入站事件。

每个 Conversation 合并为一个待唤醒标记；后台压缩释放本次 claim 后通知检查，仍有
压缩 job 或 emergency overlay 时继续等待，不按 batch 反复唤醒。若提交先于登记，登记后
立即检查当前状态，避免漏通知。后续正常聊天已接手时取消较早标记；最多补触发一轮。
恢复保留原发起者、内部事件 ID 和权限，重新验证 generation，并沿原主入口读取最新历史，
包括等待期间已入库的消息。无需新增工具、修改固定提示词或逐条重放消息。

这份短期唤醒状态仅在进程内保留，最多 256 个等待会话、每项最长 15 分钟；停止时释放，
不跨 Bot 重启恢复，也不代表消息已成功回复。原始事件始终保留。超时和再次来源变化记录
无正文诊断，避免无限生成循环。摘要变更后的首次请求重新编译；后续请求仍只追加，
工具合同不变，不宣称压缩前后完整历史前缀相同。
