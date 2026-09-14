# Yuki Rollup 预算与调度修正策略

日期：2026-09-14  
状态：本策略已完成代码实现及定向验证，正在交付；下文保留诊断基线。现行合同见 [Conversation Rollup](conversation-rollup.md)，上线状态以部署记录为准。

## 1. 范围

本文只处理 canonical Conversation Rollup 的模型输出预算、超时和执行优先级问题。

本次明确不包含：

- Self Reflection 的积压原因、调度和修复；
- Memory Extraction、Dream、Consolidation 等其他后台模型任务；
- Rollup 摘要格式、Conversation 身份、连续覆盖和持久化数据模型的重构；
- 通过清理历史、删除任务或重启其他服务规避问题。

Self Reflection 必须在后续查明实际原因后单独制定方案，不能直接套用本文结论。

## 2. 当前配置与实现

生产环境检查时，Rollup 相关有效参数为：

| 项目 | 当前值 | 含义 |
| --- | ---: | --- |
| `conversation_rollup_summary_max_characters` | 1200 | 最终可提交摘要的正文字符上限 |
| Rollup `max_output_tokens` | 4096 | 模型推理与最终正文共享的输出预算 |
| `conversation_rollup_batch_max_characters` | 32768 | 单批压缩来源的最大字符数 |
| 模型思考模式 | `enabled / low` | 所有生成任务必须满足的现行最低思考合同 |
| Provider timeout | 30 秒 | 单次 Flash 请求超时 |
| Rollup 执行优先级 | `BEST_EFFORT_BACKGROUND` | 任意前台模型请求均可主动取消 |

`max_output_tokens=4096` 不是生产配置文件中的独立参数。当前代码通过
`max(4096, summary_max_characters)` 在请求构造时计算，并在 Rollup 请求中显式传给 Provider，
因此 Profile 的默认输出预算不会覆盖它。

Settings、RollupService、模型路由和 Provider client 均在 Bot 启动时构造。当前没有能够安全重建
这些长生命周期对象的 Rollup 热重载入口。

## 3. 已确认的问题

### 3.1 输出预算不足

Rollup 使用开启思考的 DeepSeek Flash。推理 token 与最终摘要正文共享 4096 token，而最终提交又要求
正文不超过 1200 字符。4096 并不是摘要正文上限，而是“思考过程 + 最终正文”的总生成上限。

生产记录中已有一次成功 Rollup 恰好消耗 4096 completion tokens，耗时约 25.8 秒，已经同时接近
输出预算和 30 秒超时。当前 Flash 路由的 Conversation Compaction 观察到：

- 成功 34 次；
- 空响应 67 次；
- 被前台抢占 80 次。

空响应不等同于 Provider 完全没有生成。对于无工具的 Rollup，如果模型只返回 reasoning、尚未来得及
产生最终正文，本地解析会将其归类为空响应。因此，现有数据与“推理耗尽共享输出预算”的故障模式一致。

### 3.2 前台无条件抢占导致语义 Rollup 饥饿

ConversationRollupService 将所有语义 Rollup 固定标记为 `BEST_EFFORT_BACKGROUND`。共享执行器在任意
前台或 exclusive 请求到达时，都会主动取消当前正在执行的后台 Provider task。

这不是偶发的系统资源竞争，而是当前实现的明确行为。活跃群持续产生前台请求时，同一个 Rollup 可以
多次生成到一半后被取消，造成：

- 已消耗的 Provider 时间和 token 被浪费；
- 语义 checkpoint 迟迟不能推进；
- 同一批来源被反复请求；
- 失败、空响应与抢占相互放大；
- 前台最终更多依赖 emergency extractive overlay，能够保住上下文边界，但语义质量下降。

2026-09-14 03:50 至 03:59 UTC 附近已观察到一轮 Rollup 在空响应和抢占之间反复尝试，约九分钟后
才成功提交。这证明当前调度策略存在实际的饥饿和调用抖动，而不只是理论风险。

### 3.3 单独增大 token 仍不完整

如果只把输出预算增大到 16384 或更高，但继续保留 30 秒超时和无条件抢占：

- 请求可能在生成最终正文前先超时；
- 请求仍可能被任意前台消息取消；
- 更大的单次预算反而可能增加每次被取消时的浪费。

因此，输出预算、超时和调度优先级必须作为同一个 Rollup 修正一起处理。

## 4. 修改策略

### 4.1 将生成预算与摘要正文上限拆开

新增独立设置，例如：

```text
conversation_rollup_max_output_tokens = 16384
```

约束如下：

- 初始值不低于 16384；
- 该值只控制 Provider 的生成预算；
- `summary_max_characters` 继续控制最终写入 Prompt 的摘要正文；
- 不再通过增大 `summary_max_characters` 间接取得更多生成 token；
- 请求前校验 Provider 上限、本地生成预算和摘要字符上限；
- metrics 记录实际生效的输出预算，但不记录正文。

生产初始策略：

```text
max_output_tokens = 16384
summary_max_characters = 1200
batch_max_characters = 32768
```

暂不把摘要正文提高到 16384 字符。摘要正文属于后续每轮都会携带的稳定历史前缀，直接扩大到该数量级
会持续侵占主 Agent 上下文。若后续质量验收证明 1200 字符不足，应在 1200 至 2400 字符范围内单独评估，
不能与 reasoning token 预算混为一项。

### 4.2 同步提高 Provider 超时

Rollup Flash 请求的 timeout 初始调整为 90 秒，并与其他任务的 timeout 分离，避免为了 Rollup 修改
整个 Profile 的通用超时语义。

要求：

- 超时期间不持有 SQLite 写事务；
- lease heartbeat 必须继续有效；
- 超时后保留现有有界重试和退避，不立即形成高频重试；
- 记录 timeout、empty、preempted 和 successful completion 的独立结果类别。

### 4.3 将 Rollup 改为分级优先级

Rollup 不应永久高于所有聊天，也不应永久处于可被任意请求取消的最低级。采用以下三级语义：

1. **未达到触发水位**  
   不启动语义 Rollup，不与前台争抢资源。

2. **已达到触发水位，Prompt 尚未超过 admit 上限**  
   Rollup 进入 maintenance priority。可以等待执行槽，但 Provider 请求一旦开始，不再被普通前台请求
   取消。不同 Conversation 的前台请求在仍有全局容量时可以并行，不应因为一个 Rollup 全局停摆。

3. **当前 Conversation 已超过前台 admit 上限**  
   该 Conversation 的 Rollup 成为当前轮次的有界前置依赖。优先等待或共同接续已经存在的同一
   Rollup，不重复发起相同来源请求；语义压缩明确失败或超时后，才使用 emergency extractive overlay
   保证 Prompt 有界。

实现上应新增明确的 maintenance/required priority，或为 Rollup 预留受限执行容量。不能继续使用
“任何前台进入就 `cancel()` 当前后台请求”的全局规则。

### 4.4 合并相同 Conversation 的压缩需求

同一 Conversation 同一 generation 只能有一个有效语义 Rollup 请求。前台发现已有请求时应等待或
订阅其结果，不得另外生成同一来源批次。

继续保留现有约束：

- `chat_events` 是唯一原始证据源；
- checkpoint 只覆盖连续前缀；
- 模型调用期间不持有数据库事务；
- 提交前重验 generation、来源 fingerprint、coverage 和 lease；
- emergency overlay 不得伪装或推进 semantic checkpoint；
- 失败恢复不跨 generation，不盲目重放事件。

### 4.5 热修与上线方式

该问题不能通过只修改 `.env` 或 `model_profiles.toml` 实现无重启热修，原因是：

- 4096 由代码直接计算并写入每次请求；
- 请求显式值覆盖 Profile 默认值；
- 长生命周期 Settings 和执行器没有 Rollup 热重载入口；
- 临时扩大 `summary_max_characters` 会错误改变摘要正文合同。

正确交付方式是代码补丁、定向测试、本地构建新 Bot 镜像，然后只替换 Bot 容器。无需重启或替换
NapCat、SnowLuma、Docker daemon 和持久环境。替换前应等待当前 Provider 调用结束或明确记录其中断；
Rollup job、事件账本和 checkpoint 均按现有持久机制恢复。

## 5. 验收标准

### 5.1 请求与输出合同

- Conversation Compaction 的最终 Provider payload 中 `max_output_tokens >= 16384`；
- reasoning 保持 `enabled`，effort 不低于 `low`；
- 最终提交摘要仍不超过 `summary_max_characters`；
- 超长、纯 reasoning、无正文和 Provider 截断分别产生可诊断结果；
- 不把 reasoning 文本写入摘要、Memory 或普通日志。

### 5.2 调度合同

- 达到触发水位且已经开始的 Rollup 不会被普通前台请求取消；
- 活跃群持续来消息时，语义 checkpoint 仍能在有界时间内推进；
- 同一 Conversation 不并发生成相同 coverage 的两个 Rollup；
- 未达到水位的 Conversation 不抢占前台容量；
- 超过 admit 上限时，当前轮只等待有界时间，失败后可以安全使用 emergency overlay；
- exclusive 维护操作仍可按明确合同阻止或取消 Rollup。

### 5.3 数据正确性

- checkpoint 与 raw tail 无重叠、无缺口；
- generation/reset、来源 fingerprint 或 lease 变化后旧结果无法提交；
- 抢占、超时和 Bot 替换不会重复推进 coverage；
- emergency overlay 不修改 semantic checkpoint；
- 不删除历史事件，不通过清理队列制造“积压消失”。

### 5.4 生产观察

部署后至少检查：

- Rollup success、empty、timeout、preempted 的增量；
- oldest pending、processing lease 和 retry age；
- 活跃群 checkpoint 推进速度及 eligible prefix；
- emergency overlay 的新增频率；
- Provider completion tokens、耗时和 30/60/90 秒分位；
- 主 Agent 前台延迟是否出现不可接受回归。

目标不是把所有后台任务都提升为前台优先级，而是消除“已到水位的语义 Rollup 被普通聊天无限取消”
这一饥饿路径。

## 6. 非目标与后续事项

Self Reflection 的积压目前不在本文结论范围内。后续必须独立核对其 job 状态、模型结果、重试、预算、
租约和调度证据，再决定是否需要输出预算或优先级调整。本文不得作为 Self Reflection 修复的直接依据。
