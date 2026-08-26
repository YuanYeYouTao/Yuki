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

未覆盖计数、触发、protected tail 和候选批次都使用与 Main Agent 实际历史渲染同源的 Prompt
字符尺子。`rollup_source_projection` 只服务压缩模型与 extractive，不作为前台水位尺子。

事件 floor 和字符预算共同决定 protected tail：

- 长消息先碰字符上限时，允许保留少于事件 floor 的尾部并压缩更早前缀。
- 大量短消息受事件 floor 保护时，Prompt 可暂时超过字符 target。
- target 是压缩目标，不是 fail-closed 上限；最终是否需要前台压缩使用 admit/trigger。
- 不能因为 target 过小就反复 fallback，也不能为了压回 target 丢掉受保护尾部。

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

缓存诊断只能记录不含正文的 prefix/request-shape/snapshot hash。这些 hash 不发送给模型，不作为
业务身份，也不进入高基数 metrics label。

## generation 与效果围栏

每轮捕获不可变 Conversation snapshot。每次模型请求、工具、回复、语音、图片和插件副作用前都
重验 generation；外部效果通过进程内 EffectGate 获得一次性 permit。

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
