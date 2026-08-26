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
3.8.0 存量计数必须在停写副本和 live 数据库上执行
`qq-ai-bot-cli conversation recount-uncovered` 后才能由 3.8.1 恢复写入。

事件 floor 和字符预算共同决定 protected tail：

- 长消息先碰字符上限时，允许保留少于事件 floor 的尾部并压缩更早前缀。
- 大量短消息受事件 floor 保护时，Prompt 可暂时超过字符 target。
- target 是压缩目标，不是 fail-closed 上限；最终是否需要前台压缩使用 admit/trigger。
- 不能因为 target 过小就反复 fallback，也不能为了压回 target 丢掉受保护尾部。
- protected tail 取最近 N 条可见 message；夹在这些 message 之间或之后的 external keeper 随后缀
  一起受保护。位于 eligible prefix 的外部风暴仍可触发压缩，但不能跨过受保护消息切 batch。

模型配置的最大输出字符必须足以容纳结构化摘要。若 Provider 上限、请求上限或本地
`summary_max_characters` 不一致，应在调用前按最小有效上限校验并记录无正文错误类别；不能先让
模型稳定截断，再把低质量 fallback 当作正常结果。

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
type、时间、summary 都在 `content_trust=external_untrusted` 载体内。Host 的
`external_event_policy` 是不含插件身份与正文的恒定可信句子。当前通知若触发独立后台 Agent turn，
只在该 turn 的 current input 出现一次，不能再从 recent digest 重复注入。

external append 的前台 Prompt 字符增量为零，也不以字符阈值 `force_existing` 唤醒已有后台 job；
但 keeper 事件计数仍推进。digest 必须在最终 coverage/raw-tail 确定后重新选取，不能从切窗前的
过期 tail 复用。压缩输入只包含有界 summary 与来源元数据，不包含 external payload。

缓存诊断只能记录不含正文的 prefix/request-shape/snapshot hash。这些 hash 不发送给模型，不作为
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

Rollup schema 属于 3.8 canonical database。`0049` 不提供 downgrade；数据库问题必须停止所有
写入并恢复升级前同一时点 DB/WAL/SHM 快照。
