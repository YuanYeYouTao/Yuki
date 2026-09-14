# Yuki Self Reflection 积压治理与 Responses 结构化输出任务书

> 状态：代码已部署，真实 manual 与固定调度窗口验收待完成。见 [交付记录](self-reflection-delivery-2026-09-15.md)。
>
> 范围：仅 Self Reflection
>
> 本文保留原验收要求；当前实现与线上证据以交付记录和现行 self-reflection.md 为准。

## 1. 背景与目标

生产环境中的 Self Reflection 扫描水位能够追上 `chat_events`，调度器也仍在运行，但“发现了新事件”不等于“已经完成反思”。当前主要问题是：

1. 高活跃群的事件流入速度超过固定三次调度的最大理论吞吐量，形成真实的可执行积压。
2. Self Reflection 通过 DeepSeek Chat Completions 的 Function Tool 模式要求模型恰好调用一次 `emit_result`，但适配器又移除了 `tool_choice`，该合同无法由 Provider 强制保证。
3. 输出预算只有 4096 tokens，已经出现完成长度精确达到上限的请求，校验修复仍可能失败。
4. 单批失败后，当前轮次会排除整个会话，下一次重试只能等下一个固定时间窗口。
5. 健康状态只报告“待处理会话数”，把真实可执行积压、等待重试、尚未到期和策略不适用混在一起。
6. `/ai memory self-reflection run` 只返回很少的本轮汇总，不能回答处理了多少事件、积压下降多少、失败是否推进水位、何时重试等运维问题。

本任务的目标是：

- 将 Self Reflection 改为独立的 DeepSeek Responses + `json_schema` 结构化输出链路。
- 将 Self Reflection 输出预算提高到 **32768 tokens**，并把再次撞满视为异常信号，而不是继续无上限扩容。
- 提高单轮、单会话和每日处理上限，并增加按积压水位触发的后台排空机制。
- 对失败批次做持久化、退避和可恢复重试；不错误推进检查点。
- 让手动命令成为可追踪、可查询、可恢复的内部工作，并在开始及结束后提供不含正文的运行报告。
- 保留现有 Pydantic schema、证据引用范围、可见性、所有权、mutation 权限和幂等性校验。

## 2. 不在本任务范围内

- Rollup、Dream、普通聊天、Memory Extractor、Classifier 等其他模型任务的预算或调度修改。
- 修改 Self Reflection 的事实价值标准、proposal 上限、episode 上限或内容长度上限。
- 绕过现有 Memory mutation 权限检查。
- 通过直接修改数据库水位、删除失败记录或把 pending 强制清零来释放积压。
- 为了追求吞吐量而让后台反思抢占前台聊天。
- 在协议适配器内按内容识别 Self Reflection 并隐式改路由。

## 3. 当前生产证据

以下快照采集于 2026-09-15 凌晨，时间均按 Asia/Shanghai 表述：

| 项目 | 观察结果 | 结论 |
| --- | --- | --- |
| 扫描水位 | `last_scanned_event_id=53687`，等于当时 `chat_events.max(id)=53687` | 扫描器没有堵死 |
| 数字生命研究所 | 1467 个 pending events，约 52593 字符，自 2026-09-14 15:16 起等待，且含 Yuki 回复/可信工具结果 | 属于真实可执行积压 |
| 新宿マッド之江环 | 26 个事件、720 字符，含 Yuki 回复 | 尚未达到正常触发条件 |
| 远野私聊 | 11 个事件、101 字符 | 最近产生，尚未到期 |
| 冰冰のimpart小队 | 1 个事件、10 字符，无 Yuki 回复和可信工具结果 | 策略不适用，不应与可执行积压混报 |
| 当日运行 | 9 个批次：6 completed、3 failed | 不是完全停机 |
| 当日额度 | 36 次上限未耗尽 | 失败不是由日额度耗尽引起 |
| 失败类型 | 2 次 `LLMEmptyResponseError`，1 次 `StructuredTaskError:tool_call_count` | Provider 输出合同和恢复策略均需修复 |
| 失败批次 | 数字生命研究所的 52128–52227 批次在 20:02 失败 | 失败后该会话被当前轮排除，积压延续 |
| 输出长度 | 一次输出精确达到 4096 tokens；校验修复仍失败 | 当前输出预算过紧 |

当前默认配置为：每批最多 100 个事件/8000 字符；每轮最多 12 批；每会话每轮最多 7 批；每日最多 36 次；输出最多 4096 tokens；固定在 04:00、12:00、20:00 调度。

按理想上限计算，一个会话每天最多处理：

```text
100 events × 7 batches × 3 windows = 2100 events/day
```

数字生命研究所曾在约 8 小时 20 分钟内新增约 1463 个事件；若该速率持续，日流入可超过 4000 个事件。即使所有请求成功，旧上限也可能长期追不上流入。

## 4. 根因判断

### 4.1 Function Tool 合同不能稳定成立

`StructuredTaskRunner` 在 `FUNCTION_TOOL` 模式下声明唯一的 `emit_result`，设置 `tool_choice="required"`，并要求响应中恰好有一次工具调用。

但当前 DeepSeek 适配路径基于旧兼容性判断移除了或不发送 `tool_choice`。因此本地校验要求“恰好一次”，Provider 侧却没有对应的强制合同。`tool_call_count` 是这一矛盾的直接表现，不应仅靠增加 validation repair 重试掩盖。

Self Reflection 的目标是获得结构化数据，不是执行外部函数。把输出伪装成 Function Tool 没有必要，且把结构化生成成功与工具调用行为耦合在一起。

### 4.2 4096 输出预算已经成为真实边界

一次相关请求的完成长度精确达到 4096 tokens，说明截断风险不是理论问题。Self Reflection 的 schema、证据列表、proposal/episode 结构和 reasoning 都会占用输出预算；校验修复还会携带错误说明和前一次响应，4096 对该任务过紧。

### 4.3 固定调度和失败隔离策略放大积压

固定三个时间窗适合平稳低流量，不适合突发高活跃群。单批失败后把整个会话放入 `failed_conversation_keys`，虽然避免了当前轮热循环，却也意味着一次格式错误可能让大量后续批次等到 8 小时后的下个时间窗。

### 4.4 健康口径混合了不同状态

“可执行但未处理”“失败等待重试”“事件太少/太新”“没有 Yuki 自身证据”四类状态的处置不同。只报告 `pending_conversations` 会造成两种误判：

- 把策略不适用的会话误判为堵塞。
- 看不到高活跃会话具体积压了多少事件，以及积压是在上升还是下降。

## 5. 必须保持的安全与架构约束

1. 原始 `chat_events` 账本保持权威，不删除、不改写正文。
2. 扫描、批次、retry 和手动运行均使用内部事件 ID、canonical owner 和内部 run ID；不得用平台消息 ID 重建工作所有权。
3. 失败批次不得推进该批覆盖范围的 Self Reflection 检查点。
4. 已成功提交的 mutation 必须通过持久回执恢复，不得因进程重启盲目重放。
5. Provider 调用、等待和大范围扫描不得发生在 SQLite 写事务中。
6. 模型输出必须依次通过 JSON 解码、Pydantic、引用范围、可见性、所有权、mutation 权限和幂等性校验。
7. Self Reflection 并发维持为 1；后台工作使用后台优先级，前台聊天保持优先。
8. 日志、指标和管理员报告不得包含消息正文、记忆正文、证据摘录、模型 reasoning、原始 Provider 响应或密钥。
9. proposal 最多 8 条、episode 最多 1 条、episode 拼接内容最多 4000 字符等质量边界保持不变。

## 6. 目标模型链路

### 6.1 独立路由

为 `ModelTask.MEMORY_SELF_REFLECTION` 配置独立模型 profile，不复用普通 `flash` 的结构化合同：

```toml
[profiles.self_reflection]
provider = "deepseek"
protocol = "responses"
model = "deepseek-v4-flash"
structured_output = "json_schema"
thinking_enabled = true
reasoning_effort = "low"
timeout_seconds = 180
max_retries = 1

[tasks]
memory_self_reflection = "self_reflection"
```

模型名以部署时 DeepSeek 账户实际可用名称为准，不在协议适配器中做内容路由或隐式替换。

DeepSeek 官方 Responses 文档已声明支持 Function Tools、`tool_choice`、`json_object` 和 `json_schema`。本任务选择 `json_schema`，原因是 Self Reflection 只需要结构化结果，不需要执行函数。参考：

- <https://api-docs.deepseek.com/guides/responses_api/>
- <https://api-docs.deepseek.com/api/create-response/>

### 6.2 Responses `text.format` 序列化

修复 Responses 适配器的 JSON Schema wire payload。当前 Runner 产生 Chat 风格的嵌套结构：

```json
{
  "type": "json_schema",
  "json_schema": {
    "name": "emit_result",
    "strict": true,
    "schema": {}
  }
}
```

发送到 Responses API 时必须规范化为：

```json
{
  "text": {
    "format": {
      "type": "json_schema",
      "name": "emit_result",
      "strict": true,
      "schema": {}
    }
  }
}
```

规范化必须是协议层的通用格式转换，不得通过识别 prompt 内容来特判 Self Reflection。

### 6.3 解码和受控降级

正常路径：

```text
Responses text.format(json_schema)
  -> 提取最终 output_text
  -> 严格 JSON 解码
  -> SelfReflectionOutput Pydantic 校验
  -> 引用/可见性/所有权/mutation/幂等性校验
  -> 写入
```

不得继续要求 `emit_result` Function Tool，因此 Self Reflection 正常路径不再产生 `tool_call_count`。

仅当 Provider 明确返回“不支持 `json_schema`”且配置显式允许受控降级时，才可在**零工具调用**并且正文是单个严格合法 JSON object 时退化为 `text_json` 解码。以下情况一律失败：

- JSON 前后包含解释、Markdown fence 或其他文本。
- JSON 顶层不是 object。
- Pydantic 或任一本地安全校验失败。
- 响应状态为 incomplete，或无法证明完整输出。

降级必须有独立指标和告警，不得静默发生。

### 6.4 输出预算异常分类

Self Reflection 的 `max_output_tokens` 提高到 **32768**。同时解析 Responses 的 `response.incomplete` 和 token usage：

- 若 `incomplete_reason=max_output_tokens` 或完成长度触及 32768，分类为 `output_budget_exhausted`。
- 32768 再次耗尽时，视为 prompt/schema/reasoning 或 Provider 行为异常，保留失败批次并告警，不继续自动放大预算。
- timeout、empty response、schema validation、reference validation、mutation failure 分别计数，不合并成笼统的 `failed`。

## 7. 配置修改

### 7.1 第一阶段目标值

| 配置 | 当前值 | 目标值 | 说明 |
| --- | ---: | ---: | --- |
| `MEMORY_SELF_REFLECTION_MAX_OUTPUT_TOKENS` | 4096 | **32768** | 用户明确指定；再次撞满视为异常 |
| `MEMORY_SELF_REFLECTION_TIMEOUT_SECONDS` | 共用全局 timeout | **180** | 新增 Self Reflection 专用超时 |
| `MEMORY_SELF_REFLECTION_MAX_EVENTS` | 100 | **200** | 提高单批事件吞吐 |
| `MEMORY_SELF_REFLECTION_MAX_CHARACTERS` | 8000 | **16000** | 与事件上限同步提高 |
| `MEMORY_SELF_REFLECTION_MAX_BATCHES_PER_RUN` | 12 | **32** | 提高单轮排空能力 |
| `MEMORY_SELF_REFLECTION_MAX_BATCHES_PER_CONVERSATION_PER_RUN` | 7 | **16** | 允许高积压会话持续推进 |
| `MEMORY_SELF_REFLECTION_MAX_DAILY_CALLS` | 36 | **96** | 容纳固定调度与排空运行 |
| validation repair | 1 | **1** | 不通过堆叠重试掩盖合同错误 |
| concurrency | 1 | **1** | 保持串行，控制写入和模型负载 |

新配置必须加入 `Settings`、领域设置投影、`.env.example`、配置文档和启动日志，并校验：

- `max_characters` 足以容纳 `max_events` 的实际序列化结果。
- `max_batches_per_conversation_per_run <= max_batches_per_run`。
- 专用 timeout 大于 0，并只作用于 Self Reflection profile。
- 每日额度统计所有 scheduled/manual/drain Provider 请求，包括 validation repair；不得只统计成功批次。

### 7.2 吞吐目标

目标配置下，一个会话按三个固定时间窗的名义能力为：

```text
200 events × 16 batches × 3 windows = 9600 events/day
```

该数字只是容量上限，不是模型调用承诺。自然间隔切批、字符上限、失败重试、每日额度和其他会话公平性都会降低实际吞吐。验收应看长期 `ingress_rate` 与 `drain_rate`，不能只看理论乘积。

## 8. 自适应积压排空与失败重试

### 8.1 调度层级

保留 04:00、12:00、20:00 三个固定时间窗，并增加后台排空：

| 状态 | 条件 | 行为 |
| --- | --- | --- |
| 正常 | actionable pending < 500 | 仅固定窗口运行 |
| 排空 | actionable pending >= 500 | 每 10 分钟尝试一轮后台排空 |
| 严重积压 | actionable pending >= 1000 | 标记 critical，继续后台排空并告警 |
| 退出排空 | actionable pending < 100 | 停止额外排空，回到固定窗口 |

500/1000/100/10 分钟必须配置化。高水位只提高后台任务的运行频率，不把它提升为前台优先级。

### 8.2 公平性

- 每轮采用跨 canonical conversation 的轮转，不允许一个高流量群永久占满 32 个批次。
- 在完成每个会话的基础份额后，剩余额度可按 actionable pending 降序分配。
- `failed_conversation_keys` 只隔离本轮中失败的**源批次**，不把同一会话后续所有可独立批次永久排除。
- 一个会话失败不得阻止其他会话继续推进。

### 8.3 持久重试

失败源批次以 canonical owner、`first_event_id`、`last_event_id` 和输入指纹形成稳定键，持久化：

- `attempt_count`
- `last_error_category`
- `next_attempt_at`
- `first_failed_at` / `last_failed_at`
- 原批次内部 run ID 和最后持久回执

退避时间为 5、15、30 分钟。连续 3 次失败后进入 `isolated`：

- 不推进该批检查点。
- 不忙循环重试。
- 产生管理员告警，并显示错误类别和可安全公开的批次范围。
- 允许后续通过同一内部执行 ID 恢复，不创建重复 mutation。

### 8.4 无自身证据状态

对超过 `max_wait` 且没有 Yuki 回复/可信工具结果的会话：

- 不调用模型，不写入记忆。
- 将 Self Reflection 投影推进到当前已检查范围，并记录 `no_self_evidence`。
- 原始 `chat_events` 保持不变。
- 健康状态计入 `policy_ineligible`，不计入 actionable backlog。

## 9. `/ai memory self-reflection run` 运行报告合同

### 9.1 命令行为

提高批次和 timeout 后，一次手动运行最坏可能持续较长时间。命令处理器不得一直占用平台消息处理生命周期等待最多 32 个批次。

将该命令改为持久化的内部工作：

1. 超级管理员执行 `/ai memory self-reflection run`。
2. 以收到该命令的**内部 source event ID** 创建或复用一个 manual cycle；重复投递不得启动第二份工作。
3. 立即返回“已开始”回执，包含 run ID、开始前积压、当日额度和本轮边界。
4. worker 在后台执行有界周期；重启后按原 cycle/run ID 恢复。
5. 完成后向原管理员会话发送一次最终报告；投递也必须有持久回执，避免重启后重复发送。
6. 新增只读命令 `/ai memory self-reflection status [run_id]`，用于查询当前或最近一次手动运行。查询不得触发新运行。

若已有统一的持久工作、结果投递和回执设施，应复用该设施；不得另建一套用平台消息 ID 做幂等键的旁路队列。

### 9.2 开始回执

开始回执至少包括：

- manual run ID。
- 登记时间。
- 当前状态：queued/running，或复用已有运行。
- 开始前 actionable events/conversations。
- retry batches、policy-ineligible conversations、recent/not-due conversations。
- 今日已用/上限/剩余额度。
- 本轮最多批次、单会话最多批次、单批事件/字符边界。

示例：

```text
Self Reflection 手动运行已登记（run sr_01...）。
开始前：可执行积压 1467 个事件/1 个会话；待重试 1 批；策略不适用 1 个会话；尚未到期 2 个会话。
今日额度 9/96，剩余 87；本轮最多 32 批，单会话最多 16 批。
完成后将返回汇总，也可用 /ai memory self-reflection status sr_01... 查询。
```

### 9.3 最终报告

最终报告必须是 content-free 的运行事实，至少包括：

- run ID、最终状态和 trigger（manual）。
- 开始时间、结束时间、总耗时。
- 尝试/成功/失败/延后批次数。
- 实际覆盖的事件数和字符数。
- 生成 proposal 数和实际 committed 数。
- actionable events/conversations 的 before、after 和 delta。
- retry/isolated/policy-ineligible/recent-not-due 数量。
- 失败类别及数量；存在重试时报告下一次重试时间。
- 今日模型请求已用/上限/剩余；明确 repair 也消耗额度。
- 是否命中本轮批次边界、每日额度或输出预算。
- 每个失败批次是否推进检查点。正常要求应为“未推进”；若从持久回执恢复为 completed，必须明确为 recovered completed。

示例：

```text
Self Reflection 手动运行完成（run sr_01...，耗时 3分42秒）。
批次：尝试 12，成功 11，失败 1，延后 0；覆盖 1084 个事件/41220 字符。
结果：生成 proposal 9 条，实际写入 7 条。
可执行积压：1467 -> 383（减少 1084）；待重试 1 批，隔离 0 批；策略不适用 1 个会话，尚未到期 2 个会话。
失败：json_schema_validation 1；失败批次未推进检查点；下次重试 00:25。
今日模型请求 21/96，剩余 75；未命中输出预算。
```

若本轮没有处理任何批次，不能只说“未处理”。必须给出可判定原因，例如：

- `no_actionable_backlog`
- `daily_limit_reached`
- `another_cycle_running`
- `all_due_batches_waiting_retry`
- `all_pending_policy_ineligible`
- `all_pending_recent_not_due`

### 9.4 报告隐私与长度

报告可以展示管理员已有权限可见的 scope display name 和内部批次范围，但默认只列失败/隔离项和积压最高的前 5 个 scope。不得展示：

- 消息或记忆正文。
- evidence 摘录。
- 模型原始输出或 reasoning。
- Provider request/response body。
- API key、完整输入指纹或其他敏感配置。

超出消息长度时，主消息提供总览和前 5 项，其余内容通过 `status <run_id>` 分页查询；不得静默截断关键失败信息。

## 10. 持久化与状态模型

新增 manual/scheduled/drain cycle 聚合记录，或在现有持久工作设施中表达等价状态。建议最小字段：

```text
cycle_id / public_run_id
mode: scheduled | manual | drain | retry
source_event_id (manual 必填)
canonical_request_conversation_id (manual 必填)
status: queued | running | completed | partial_failed | failed | cancelled
started_at / completed_at
attempted / completed / failed / deferred batches
processed_events / processed_characters
proposal_count / committed_count
actionable_before / actionable_after
retry_count / isolated_count / policy_ineligible_count / recent_not_due_count
daily_requests_before / daily_requests_after
limit_flags
final_report_delivery_state / durable receipt
```

现有 batch run 记录增加 `cycle_id` 关联。失败批次的 retry state 使用稳定源范围键，不把正文保存进监控字段。

所有状态转换使用短事务：

```text
claim/commit transaction
  -> transaction ends
  -> Provider call
  -> validate
  -> mutation short transaction
  -> finalize/report receipt short transaction
```

## 11. 健康状态与可观测性

将当前单一 `pending_conversations` 拆分并同时提供事件数与会话数：

- `actionable`
- `waiting_retry`
- `isolated`
- `policy_ineligible`
- `recent_not_due`
- `processing`

增加至少以下指标：

```text
self_reflection_actionable_events
self_reflection_oldest_actionable_age_seconds
self_reflection_ingress_events_total
self_reflection_processed_events_total
self_reflection_batches_total{status,trigger,error_category}
self_reflection_provider_requests_total{attempt_kind,status}
self_reflection_output_tokens
self_reflection_output_budget_exhausted_total
self_reflection_retry_batches
self_reflection_isolated_batches
self_reflection_cycle_duration_seconds{trigger}
self_reflection_report_delivery_total{status}
```

告警建议：

- actionable >= 1000。
- oldest actionable age > 8 小时。
- 连续 3 个周期 `drain_rate < ingress_rate`。
- 任一 `output_budget_exhausted`（32768）。
- isolated batch > 0。
- 调度 worker 退出或连续两个固定窗口无运行记录。
- 最终管理员报告投递失败或重复投递。

## 12. 积压释放流程

修复上线后，通过正常 worker 释放现有积压：

1. 确认扫描水位追上 `chat_events.max(id)`，并保存上线前 content-free 健康快照。
2. 执行一次 `/ai memory self-reflection run`，记录开始回执中的 run ID。
3. 等待最终报告或用 `status <run_id>` 查询。
4. 核对 processed events 与 actionable delta 一致；失败批次检查点不得推进。
5. 若仍有 actionable backlog，允许下一轮自适应 drain 或再次手动运行；每次均创建独立可追踪 cycle。
6. 直到 actionable backlog < 100，退出额外排空。
7. 保留 retry/isolated 记录，按错误类别修复；不得用水位跳过。

禁止以下“清积压”操作：

- 直接把 pending 设为 0。
- 手工推进 `last_event_id`。
- 删除 failed run、retry 或 mutation receipt。
- 直接重跑可能已提交但未完成 finalize 的 mutation。
- 为了追平积压临时取消证据、权限或 Pydantic 校验。

## 13. 实施工作包

### WP1：Responses JSON Schema 合同

- 为 Self Reflection 建立独立 Responses profile。
- 修复 Responses `text.format` wire 序列化。
- Self Reflection 切换为 `JSON_SCHEMA`，移除 Function Tool 依赖。
- 正确解析 Responses output、incomplete reason 和 token usage。
- 保留全部本地 schema、安全和 mutation 校验。
- 实现显式开关控制的严格 `text_json` 降级；默认不静默降级。

### WP2：预算与配置

- 输出预算改为 32768。
- 增加 Self Reflection 专用 180 秒 timeout。
- 按第 7 节提高事件、字符、批次和每日额度。
- 更新示例配置、设置投影、启动日志和文档。

### WP3：排空、重试与公平性

- 增加高/低水位 drain 调度。
- 增加源批次级持久 retry 和 5/15/30 分钟退避。
- 三次失败后 isolate 并告警。
- 实现跨会话公平分配。
- 实现 `no_self_evidence` 的无模型推进。

### WP4：手动运行与报告

- 将 `/ai memory self-reflection run` 改为持久化、可恢复、幂等的 manual cycle。
- 增加立即开始回执、一次性最终报告和 `/status [run_id]`。
- 统计事件/字符、积压 before/after、失败类别、额度和边界命中情况。
- 持久化最终报告投递回执，防止重启后重复发送。

### WP5：健康检查与运维指标

- 拆分 pending 状态。
- 增加 backlog age、ingress/drain rate、retry/isolate 和报告投递指标。
- 更新管理员 health/doctor 输出和告警规则。

### WP6：迁移、回归与上线

- 添加必要 Alembic migration。
- 增加 Provider wire、worker、repository、command 和重启恢复测试。
- 在冻结的生产数据副本上执行真实模型探针。
- 仅替换 Bot 容器并验证；不改 SnowLuma、NapCat 或 Docker 基础设施。

## 14. 测试与验收标准

### 14.1 Provider 合同

- 最终 DeepSeek Responses payload 的 `text.format` 与官方格式一致。
- Self Reflection payload 不声明虚假的 `emit_result` Function Tool。
- `max_output_tokens=32768`、reasoning low、专用 timeout 180 秒准确落到 wire 请求。
- 合法 JSON Schema 输出通过；Markdown fence、额外解释、非 object 和截断输出失败。
- `response.incomplete` 能区分 budget、timeout/Provider 等原因。
- Self Reflection 不再产生 `tool_call_count` 类错误。

### 14.2 安全校验

- 未知 evidence ref、context ref 冒充 evidence、越权 scope、非法 visibility 和无权限 mutation 均被拒绝。
- proposal/episode/content 上限保持不变。
- validation repair 最多一次，并计入每日 Provider 请求额度。
- Provider 调用期间没有 SQLite 写事务保持打开。

### 14.3 Worker 与积压

- 在模拟持续流入超过 4000 events/day 时，目标配置下 drain rate 长期高于 ingress rate。
- actionable >= 500 时能进入 drain；下降到 <100 后退出。
- 单会话失败不阻止其他会话。
- 同一失败源批次按 5/15/30 分钟重试，三次后 isolate。
- 失败不推进检查点；已提交 mutation 可从持久回执恢复而不重复写入。
- 重启后 cycle、batch、retry 状态正确恢复。
- 无自身证据的过期会话记录 `no_self_evidence`，不调用模型、不写记忆。

### 14.4 手动命令与报告

- 非超级管理员不能启动或查询敏感运行详情。
- 相同内部 source event 重放只产生一个 manual cycle。
- 命令快速返回开始回执，不被 32 批 × 180 秒的最坏等待阻塞。
- 最终报告只发送一次；发送前后重启均不重复。
- `status` 是只读的，返回与持久记录一致。
- 报告数字满足基本守恒：

```text
attempted = completed + failed + deferred（按最终定义）
actionable_before - processed_successfully + newly_ingressed = actionable_after
committed_count <= proposal_count
daily_remaining = max(0, daily_limit - daily_requests_after)
```

- 报告不含任何消息/记忆正文、证据摘录、reasoning 或 Provider body。
- 失败报告明确检查点是否推进、下一次重试时间和边界命中情况。

### 14.5 生产验收

- Bot 健康、restart count 不增长，Self Reflection worker 存活。
- 扫描水位持续追上原始事件账本。
- 对数字生命研究所执行一次手动运行，收到开始回执和最终报告。
- actionable backlog 连续下降；最终低于 100 或明确由 retry/isolated 阻挡。
- 至少观察跨过一个固定调度窗口，确认 scheduled 与 drain/manual 不重复 claim。
- 32768 未被耗尽；若耗尽，必须产生 `output_budget_exhausted` 告警而不是继续提高预算。

## 15. 上线与回滚

### 15.1 上线前

- 备份 Bot 数据库、WAL/SHM、当前配置和旧镜像标识。
- 在生产数据副本上跑 migration、repository 回归和真实 DeepSeek Responses JSON Schema 探针。
- 记录上线前六类 backlog 健康快照。
- 确认每日额度从“批次成功数”改为“实际 Provider 请求数”后不会被误算。

### 15.2 上线顺序

1. 部署 schema migration。
2. 部署 Responses 结构化合同和解析。
3. 启用新预算/批次上限，但先保持 drain 关闭，完成一轮真实手动验收。
4. 验证报告和检查点后启用自适应 drain。
5. 观察错误分类、积压下降、前台延迟和 Provider 使用量。

仅替换 Yuki Bot 容器；除非另有独立任务，不修改 SnowLuma、NapCat 和 Docker 基础设施。

### 15.3 回滚

- 优先回滚 Bot 镜像和关闭 drain，保留新增表/字段与持久回执。
- 不因代码回滚删除 cycle、retry、run 或 mutation receipt。
- 不把数据库恢复到旧备份来“消除”已经提交的新记忆，除非发生明确的数据破坏并经过单独授权。
- 回滚后如旧 Function Tool 链路仍不可靠，应暂停 Self Reflection 写入并保留积压，不以跳水位方式恢复服务。

## 16. 完成定义

只有以下条件全部成立，才可将本任务报告为完成：

- 代码、migration、配置示例和架构文档均已更新。
- 所有第 14 节测试通过，并记录真实 Provider wire 证据。
- 修改已明确说明是本地完成、已提交、已推送、已合并还是已部署。
- 生产 Bot 完成一次可追踪的手动 Self Reflection run，管理员收到开始回执和最终运行报告。
- 数字生命研究所的 actionable backlog 有可核对的下降，失败批次没有错误推进检查点。
- 经过一个固定调度窗口后，没有重复执行、重复写入、重复报告或前台明显受阻。

