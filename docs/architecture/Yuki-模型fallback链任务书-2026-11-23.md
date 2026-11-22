# Yuki 模型 fallback 链调研与任务书（2026-11-23）

状态：**调研完成，任务书第 1 版，待所有者批准；只写文档，未实施**。§9 每题标【推荐】，所有者未另行表态即按推荐执行。

基线 origin/main `2084a282`（线上版本），schema `0164`。路径省略 `src/yuki/`，行号以 `2084a282` 为准。标记约定沿用近期任务书：【实证】源码或生产只读查询可复核；【假设】未经真实 API 核实；【决定】本书定下的边界；【推荐】待决问题的默认选项。生产数字均为 2026-11-22 只读查询的聚合值，不含聊天、记忆正文、密钥或内网地址。

## 1. 要解决什么，不解决什么

### 1.1 现象【实证】

- 主模型 `chat_agent` 现路由到 OpenAI Responses `gpt-6.1-terra`（Profile `openai_terra_low`），经本机代理出网。一个用途只绑一个 Profile，路由器的注释写明“without fallback”（`model_runtime/routes.py:11`）。主模型连不上时整轮失败，Work 进入 failed，看门狗报告。
- 近 14 天（11-08 至 11-22）`model_invocations` 中可归因于网络或上游的逻辑调用失败：
  - `chat_agent`：`openai_terra_low` 80 次（`LLMUnavailableError` 44、`LLMTimeoutError` 32、裸 `RemoteProtocolError` 4），旧 Gemini 中转 Profile 21 次；同期 `chat_agent` 成功约 1.74 万次，失败率约 0.6%，但集中成段出现。
  - `memory_extraction`：旧 Gemini 中转 67 次，全部在 11-16。
- 按物理 attempt 看 `provider_error` 诊断（每次 HTTP 尝试一条，排除取消、Turn 被取代等非网络类别）：

  | 日期 | 类别 | 次数 | 协议 | 是否已送达上游 |
  | --- | --- | ---: | --- | --- |
  | 11-08 至 11-16 | HTTP 503（带 `retry_after`） | 242 | Chat Completions / Gemini（旧中转） | 上游已明确拒绝 |
  | 11-08 至 11-16 | HTTP 500 | 53 | 同上 | 上游已明确拒绝 |
  | 11-17 | `ConnectError` 8、`ConnectTimeout` 12、`PoolTimeout` 89 | 109 | Responses | 未送达 |
  | 11-21 | `ConnectError` | 131 | Responses | 未送达 |
  | 11-12 至 11-21 | `RemoteProtocolError` | 6 | Responses 4、Chat 2 | **未知** |

  11-17 的 `PoolTimeout` 是 httpcore 代理隧道泄漏（已由 #634 修复）。代理隧道连不上时，每次逻辑失败的延迟：`ConnectError` 平均 19.5 秒（3 次尝试），`LLMTimeoutError` 平均 1764 秒、最长 1802 秒，因为 Profile 的 `timeout_seconds=600` 同时用作连接超时，且 `max_retries=2`。
- 11-21 的 44 次 `chat_agent` 网络失败分布在 18 条请求链（`request_chain_id`）里：9 次是链的第一次请求（9 条链）；35 次之前同链已有成功请求（9 条链，包括 `execution_compact`）。也就是说，**只在链首切换**能覆盖一半的受影响链，覆盖不到故障开始时正在进行的链。

### 1.2 本书做【决定】

1. 每个用途（`ModelTask`）可以**显式配置**一条有序备用 Profile 列表；不配置就是空链，行为与今天完全一致。没有运行时开关。
2. 只在**确定未被上游受理**的失败后切到下一跳：连接没建立、本地连接池超时、上游明确返回 429/5xx/401/403、密钥缺失；绝不按请求或回答的内容切换。
3. 只在**新链的第一次请求**切换，即请求还不带任何私有协议状态（`ProviderContinuation`）。链一旦拿到某个 Profile 的响应，后续请求都跟着那个 Profile。
4. 无状态回切：每条新链都从链首开始试；不设 cooldown、熔断计时或探测周期。
5. 每一跳是一次独立的逻辑调用：单独写 `model_invocations` 行，计入原 Work 的根预算；`model_phases` 诊断写明走了哪几跳、为什么。
6. 看门狗按状态报告“当前在用备用模型”：某用途最近一次成功调用不是链首时报告，链首恢复后清除。
7. Profile 增加可选的 `connect_timeout_seconds`，默认不设、保持现状；所有者显式设置后，连接类故障能在秒级切到备用。

### 1.3 本书不做

- 不在链中途切换 Provider，不做跨协议的私有状态转换（加密推理、签名、thought signature 都不搬运），见 §5.4 与 Q3。
- 不对结果未知的失败（请求已发出、没收到完整响应）切换，见 §5.2 与 Q2。
- 不按内容审查拒绝、上下文超限、空回答、畸形工具调用或截断切换。
- 不给独立视觉（Qwen）、ASR、Embedding、独立搜索连接加备用链：它们不是 `ModelTask` 路由；合同已明确删除视觉和 Tavily 的隐式回退（`docs/architecture/model-providers.md:75-76`、`:122`）。Embedding 换模型会换向量空间，本来就不能互换。
- 不做负载均衡、按价格或延迟排序、多 key 轮换。
- 不做 Desktop 编辑界面（Admin API 必须原样保留该字段，见 §6.3）。

## 2. 现状要点【实证】

### 2.1 路由与配置

- `ModelTask` 只有 6 个：`chat_agent`、`memory_extraction`、`memory_self_reflection`、`memory_dream`、`plugin_agent_session`、`utility_structured`（`model_runtime/models.py:17-25`）。视觉、ASR、Embedding、搜索是 `[vision]`/`[asr]`/`[embedding]`/`[search]` 独立连接（`model_runtime/profiles.py:62-65`）。
- `ModelRoute` 只有 `task`、`profile_id`、`required_capabilities`（`models.py:174-179`）；TOML 的 `routes` 是 `dict[str, str]`（`profiles.py:58`），校验要求 6 个用途都有路由、Profile 存在、能力满足（`profiles.py:95-110`）；默认能力要求 `chat_agent`/`plugin_agent_session` 需要 `tools`（`profiles.py:114-121`）。
- `ModelRouter.route` 按请求追加的能力要求（工具、结构化、图片、原生搜索）核验，缺能力直接 `ValueError`（`routes.py:19-32`，追加逻辑在 `executor.py:443-463`）。
- 热更新：Admin 保存后 `apply_catalog` 原子替换 router/pool（`executor.py:340-349`，调用点 `admin/config_files.py:498`，`apply_mode=hot_reload` 见 `:222`）；`pin()` 让一次激活内的所有请求固定在旧 catalog（`executor.py:269-291`）。Admin 快照只回传 `profiles/search_*/routes`（`admin/config_files.py:276-289`），**不认识的顶层字段保存时会被丢掉**。
- 连接池按 `(provider, base_url, api_key_env)` 共享（`model_runtime/pool.py:132-148`）：不同端点的备用 Profile 不共用出故障的池。

### 2.2 一次请求怎么执行

- `TaskModelExecutor._execute` 只解析一个 Profile（`executor.py:462-463`）。请求带私有续接状态时，Profile 或协议不一致直接拒绝（`executor.py:472-484`），`TurnTranscript.accept` 也禁止一轮内换续接合同（`services/turn_transcript.py:179-186`）。
- 容量：只按当前 Profile 的显式 `max_input_tokens`/`context_window_tokens` 核验，超出抛 `LLMUnsupportedFeatureError`（`executor.py:485-513`）。
- 记账：每次逻辑调用写一行 `model_invocations`，失败行的 `error_category` 是最外层异常类名（`executor.py:572-608`、`:651-673`）。`ProviderAttemptCounter` 统计物理请求数和未报告用量数（`model_runtime/request_accounting.py:25-78`）。
- 适配器只做非流式请求（`llm/base.py:84-90`；Responses 显式 `"stream": False`，`llm/deepseek_responses.py:84`），**Yuki 没有“流式中途断开”这个状态**；对应的情况是响应体没读完（`RemoteProtocolError`/`ReadError`），属于结果未知。

### 2.3 现有重试与错误分类

- 共同传输层：同一份已编码 payload 重发最多 `max_retries` 次，指数退避上限 2 秒（`llm/json_http.py:293-328`）；请求带原生服务端工具时不重试（`:297-298`）；第 2 次起每次重试按辅助请求计入 Work 预算（`:316-319`）。
- 重试的错误类型：Chat/Claude/Gemini 是所有 `httpx.TransportError`（`json_http.py:83`），包括 `ReadTimeout`、`RemoteProtocolError` 这类结果未知的错误；Responses 只重试 `ConnectError` 和 `TimeoutException`（`deepseek_responses.py:50-51`），所以 `RemoteProtocolError` 不重试，并以裸 httpx 异常冒出，这就是生产里 `error_category=RemoteProtocolError` 的来源。
- HTTP 状态：5xx → `RetryableProviderError`（会重试），401/403 → `LLMAuthenticationError`，429 → `LLMRateLimitError`（不重试），400 → `LLMInvalidRequestError`（`llm/http_errors.py:47-57`）。重试耗尽后包装成 `LLMUnavailableError` 或 `LLMTimeoutError`（`json_http.py:322-328`），原因只留在 `__cause__` 和诊断里。
- `httpx.Timeout(timeout_seconds)` 把连接、读、写、池等待四项都设成 Profile 超时（`json_http.py:104`），生产所有 Profile 都是 600 秒。

### 2.4 回执与恢复（不重发合同）

- Main 的派发守卫在第一次真实 HTTP 之前预留请求预算，并把 Work journal 存为 `dispatched`（`services/turn_execution.py:162-233`，预留在 `:176`，存盘在 `:199-203`）。传输重试不会再走一次这里（`:166-167`、`self.prepared`）。
- 主循环收到 `LLMTimeoutError`/`LLMUnavailableError`：请求带原生工具且已有物理派发时改抛 `LLMNativeToolError`（结果未知，不重放）；否则存 `paired` 后抛出（`turn_execution.py:686-706`）。
- 冷恢复时 journal 停在 `dispatched` 且合同相同：没有新业务输入就 `JournalUnavailable("work_response_not_persisted")`，有新输入就以 `lost_response` 开新链（`runtime/work_journal.py:178-181`，`services/work_resume.py:169-170`）。compact 的 `dispatched` 回执同样视为结果未知，不重买（`turn_execution.py:413-414`）。
- Work 合同指纹包含 `profile_revision(task)`（`services/agent_runner.py:230-256`）；修订变了，下次激活显式开新链，保留 Work ID、预算和证据（`docs/architecture/model-providers.md:36-39`）。`profile_revision` 只覆盖当前路由的 Profile（`executor.py:853-895`）。
- 业务续跑换链：`rebase_business` 只在 Work 的恢复 transcript 上、协议完全配对时退役旧链，只带最后一轮未观察的可移植工具回合，不搬 opaque（`runtime/work_session.py:445-487`）。
- 公共 compact 请求不带续接状态（`services/context_compaction.py:349`、`:367`），执行 compact 带原链续接状态（`:765-766`）。

### 2.5 结构化任务

- `StructuredTaskRunner` 在调用执行器之前按链首 Profile 的 `structured_output_mode` 生成请求：`function_tool` 发 `emit_result` 工具和 `tool_choice=required`，`json_schema` 发 `response_format`（`model_runtime/structured.py:119-152`）；校验失败的修复重试每次都是一次新的 `execute`（`:163-248`）。执行器据此追加 `tools`/`structured_output` 能力要求（`executor.py:444-447`）。

## 3. 同类产品调研

### 3.1 对象与版本

只读浅克隆，没有运行仓库里的任何脚本；闭源产品只看官方文档。

| 对象 | 版本 / 来源 |
| --- | --- |
| SuInk/Diana | `9ba924c9`（2026-11-22）；以下路径相对 `model/assistant/`，`llm/` 指 `model/llm/` |
| LiteLLM Router | `90dce4b9`（v1.112.0），路由核心在 `litellm/rust/crates/router-core` |
| OpenRouter | 官方文档 Model Fallbacks、Provider Selection、Streaming |
| LangChain / Vercel AI SDK | langchain-core 1.8.4 `8ff3cd30`；ai 7.0.179 `d55f3cfc` |
| AstrBot | `22834e62`（4.30.0-beta.1） |
| Claude Code | 官方文档 Model configuration 的 Fallback model chains 一节 |

### 3.2 Diana 的做法（重点）

- **触发**：`llm_error.go:40-105` 的 `llmErrorPolicy` 按错误分类给出 retry / failover / cooldown / immediate。会切换的有硬超时、402 与额度类、429（先原地重试）、401/403、模型不存在、所有 5xx 和 408、无状态码的传输错误（EOF、reset、TLS 错误，`:110-121`）。还有三种看输出的触发：空输出（`:56-57`）、截断且无文本（`:53-54`），以及模型回答与一段固定网关拒绝文案精确相等（`llm/rejection_notice.go:8-22`，每次成功响应都检查，`llm_retry.go:178-183`），最后这一种是按内容触发。内容安全拒绝和上下文超限不切换；后者把上下文上限减半原地重试，最多 4 次（`llm_retry.go:194-208`）。
- **链配置**：机器人 × 用途 × 分组，`ModelRole{ProfileID, Model, Fallbacks, RoutingStrategy, …}`（`types.go:1097-1125`），查找顺序是用途 → 分组 → 继承分组 → chat（`model_binding.go:211-245`）。**没有绑定时，把本分组全部 profile 按列表顺序当作隐式降级链**（`runtime_llm.go:337-342`、`:381-393`）。
- **粘性与回切**：跨请求按时间冷却，默认额度/限流/鉴权 60 秒、5xx 10 秒、超时 5 分钟，上限 30 分钟；同一候选 10 分钟内连续失败 3 次才冷却，鉴权与额度类 1 次即冷却，重复失败冷却翻倍（`llm_cooldown.go:19-42`、`:286-312`）。成功清除冷却（`:322-331`）。默认 smart 策略只把冷却中的候选后移，冷却结束主模型回到首位（`model_routing.go:98-113`）。单次运行内记住成功的那一档，后续工具步从它开始（`llm_retry.go:396, 413`）。冷却表只在进程内。
- **流式中途**：流式先攒成完整响应再交给 Agent；有可见文本推给用户就标记 committed，之后既不重试也不切换（`llm_streaming.go:185-223`、`llm_error.go:23-30`）。Telegram 草稿遇传输错误按“可能已送达”处理（`reply_streaming.go:254-262`）。
- **跨协议**：每条 assistant 消息带 `ContinuationScope = hash(provider, baseURL, apiFormat, model)`（`llm/continuation.go:14-20`）；发往不同 scope 时剥掉 Claude thinking、reasoning、Responses 原生输出和 ToolCall 的 thought signature（`:28-45`）；目标模型要求回传推理时把外来工具调用和结果改写成叙述文本（`:68-124`）。failover 只在一个模型步内发生，已执行的工具结果不重跑（`llm_retry.go:320-322`）。
- **结果未知**：没有回执或幂等，硬超时、header 超时都会原地重试或切换（`llm_error.go:44, 59-60, 96-97`），可能重复计费。
- **成本**：只有次数上限（每候选原地重试 2 次，`runtime.go:51-52`），每候选单独 5 分钟超时，整条链无总期限和预算。
- **可观测性**：切档日志 from/to/err（`llm_cooldown.go:564-587`）、运行事件 `llmEventFailover`、每档失败一条 `llm_call_failed` 带 `recovered` 标志（`llm_call_failures.go:76-110`）、控制台健康快照与手动清冷却。

### 3.3 对比

| 对象 | 触发条件 | 配置粒度 | 粘性与回切 | 流式/工具中途失败 | 跨协议转换 | 结果未知 | 成本控制 | 可观测性 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Diana | 错误分类 + 空输出 + 固定拒绝文案（含内容） | 机器人×用途×分组；无绑定时隐式成链 | 时间冷却（10s/60s/5m，3 次/10 分钟）；运行内粘在成功档 | 有可见输出即 committed，不切；工具结果不重跑 | 按 scope 剥离推理与签名，必要时把工具调用改成文本 | 超时重发，无回执 | 仅次数 | 日志、事件、每档失败记录、健康面板 |
| LiteLLM | 通用 / 上下文窗口 / 内容策略三套列表；拒答也主动切（`host.rs:531-538`） | model group；请求级覆盖；默认 `*` | `cooldown_time` 5s、`allowed_fails` 3 或 50% 比例（`config.rs:17-23`、`health.rs:24-29`）；每请求从头 | 首块前可切；首块后默认抛出，可选 prefill 续写（默认关） | 统一转 OpenAI 格式 | 超时重发 | 无链级预算 | 响应头 attempted-fallbacks、事件 |
| OpenRouter | 任意错误，含上下文长度和 moderation | 请求级 `models` + provider 选项 | 隐式健康降权；缓存热时粘 provider | 中途返回 200 + error chunk，未说明切换 | 服务端处理 | 未说明 | 只按最终使用的模型计费；`max_price` | 响应 `model` 字段 |
| Vercel AI SDK | 核心只有同模型重试 408/409/429/5xx；Gateway 有链和按回答条件的 `when` | 调用级 | 无 | `streamRetries` 只重跑当前步，已发出部分不撤回 | 无 | 重发 | `maxRetries` | warnings |
| LangChain | `exceptions_to_handle`，默认任何异常 | Runnable / middleware | 无，每次从头 | 只在首块前切（`fallbacks.py:489-526`） | 只剥缓存参数 | 重发 | 无 | callbacks |
| AstrBot | 任何异常或 err 响应 | Agent Runner，默认空 | 运行内切走后不回主模型；无冷却 | 中途异常也切，重复输出（缺陷，`tool_loop_agent_runner.py:879`） | 无 | 重发 | 无 | warning 日志 |
| Claude Code | 只在过载、模型不可用等不可重试服务端错误；限流、鉴权、计费、传输错误不切 | 会话/设置，默认空，最多 3 个 | 只在当前轮有效，下一轮回主模型；无冷却 | 未说明 | compaction 不回退到窗口更小的模型 | 未说明 | 链长上限 | 切换时 notice |
| **本书** | 只切确定未受理（连接、池、429、5xx、401/403、密钥缺失）；不看内容 | 用途，默认空 | 链内跟随续接状态；新链从链首；无冷却 | 无流式；链中途不切 | 不转换，只在无私有状态时切 | **不切**，沿原回执 | 计入原 Work 根预算；最多一次可能计费 | 每跳一行 + `route_hops` + 看门狗状态 |

### 3.4 借鉴与不照搬

借鉴：

1. Claude Code 的“只在当前轮有效、下一轮回主模型、默认空链”：对应本书的无状态回切与显式配置。它在 compaction 时不回退到窗口更小的模型，对应 Q9 的容量处理。
2. Diana 的 committed 闸门与 LangChain“首块后不切”：送达或受理不确定时按已发生处理。本书推进一步：请求已发出、结果未知就不切。
3. Diana 的 ContinuationScope：私有状态按 Provider 归属，不同归属不携带。Yuki 已有 `ProviderContinuation.profile_id`（`executor.py:480-484`），本书直接用它做链内跟随，不另建 scope。
4. 按模型步重做、已执行工具不重跑（Diana、Vercel）：Yuki 的工具回执本来就按原执行键保存。
5. 每一档落一条记录并能标出第一个失败的档（Diana `llm_call_failed`、LiteLLM attempted-fallbacks）：对应每跳一行 `model_invocations` 和 `route_hops`。

不照搬：

1. 按内容触发：LiteLLM 内容策略列表与拒答切换、OpenRouter moderation、Vercel `when`、Diana 固定拒绝文案和空输出切换。
2. 时间冷却与健康重排：Diana smart、LiteLLM cooldown、OpenRouter 降权。它们让“现在用哪个模型”取决于进程内历史，属于隐式策略，所有者也不要时间阈值。
3. 未配置时自动成链（Diana 分组隐式链）。
4. 结果未知照样重发或切换（Diana、LiteLLM、Vercel、LangChain）：隐藏重复计费。
5. 流式中途另起输出或 prefill 拼接（AstrBot 重复输出、LiteLLM 续写多付一次费用），以及运行内永久切走不回主模型（AstrBot）。

## 4. 设计总览

```
execute(task, request)
  hops = [head] + fallbacks(task)          # 来自 pin 住的 catalog，O(k)
  if request 带续接状态(profile X):
      hops = [X]                          # X 必须在链内，否则沿现有 ValueError
  for hop in hops:
      能力不满足 / 已知容量放不下 → 记 skipped，下一跳
      非链首 → 按辅助请求预留 Work 预算（与传输重试同一入口）
      调用 hop（hop 内部照旧做有界传输重试）
      成功 → 返回（响应的续接状态归 hop）
      失败 → 写该跳的 model_invocations 行
             本跳所有物理尝试都“确定未受理”且还有下一跳 → 继续
             否则 → 抛出本跳原异常（附各跳摘要）
```

要点：

- **位置**：只改 `TaskModelExecutor._execute` 的单跳逻辑为一个循环；协议适配器、Main 循环、journal 不改分支。适配器仍不知道有链（满足“协议适配器不加入路由”）。
- **链内跟随**：续接状态记录了 `profile_id`（`executor.py:629-633`），下一次请求直接走那个 Profile。这是链内唯一的“粘性”，由协议事实决定，不是计时。
- **回切**：新链（新激活、新 Turn、结构化任务的每次调用、公共 compact）都从链首开始。
- **合同指纹**：`profile_revision(task)` 改为覆盖整条链（路由 + 每跳 Profile 的序列化）。改链或改备用 Profile 时，持久 Work 下次激活显式开新链，冷恢复不会把 B 的续接状态拼到改过的 B 上。

## 5. 设计细节

### 5.1 触发分类【决定】

判断依据是**本跳全部物理尝试**的失败类别，不是最外层包装类名。在 `JSONHTTPProvider._post` 的失败分支（`json_http.py:258-263`）按下表给 `ProviderAttemptCounter` 记 `not_accepted += 1`；执行器只在 `counter.requests == counter.not_accepted`（包括 0 次物理请求的派发前失败）时允许切到下一跳。Claude pause 第二次续发失败时，第一次已付费，条件不成立，不会切。

| 失败 | 触发 | 理由 |
| --- | --- | --- |
| `httpx.ConnectError`、`ConnectTimeout`、`ProxyError` | 是 | 连接或代理隧道没建立，请求字节没离开本机；11-17、11-21 两段故障都是这一类 |
| `httpx.PoolTimeout` | 是 | 本地连接池等待超时，没有派发 |
| 派发前 `LLMConfigurationError`（密钥缺失） | 是 | 0 次物理请求；看门狗报告持续 fallback，不会被掩盖 |
| HTTP 429 | 是 | 上游明确拒绝受理；`retry_after` 留在诊断，不据此计时 |
| HTTP 5xx（`RetryableProviderError`，本跳重试耗尽后） | 是 | 上游返回了拒绝状态；11-16 的 503 属于这一类。504 网关超时可能是上游已处理【假设】，见 Q2 |
| HTTP 401/403 | 是 | 上游拒绝凭据，没有受理；看门狗持续报告 |
| `ReadTimeout`、`ReadError`、`WriteError`/`WriteTimeout`、`RemoteProtocolError` | **否** | 请求可能已被上游受理并计费，结果未知；沿现有规则（本跳有界重试或直接失败，存 `paired` / native 改抛 `LLMNativeToolError`） |
| HTTP 400（含上下文超限、内容审查、参数错误）、404/422 等其他 4xx | 否 | 请求形状、配置或内容问题；换 Provider 等于按内容路由 |
| 200 但非 JSON / 解析失败 / 空回答 / 畸形工具调用 / 截断 / 原生工具无结果 | 否 | 已收到并已付费的响应，按现有收尾 |
| 链首本地容量超限（`LLMUnsupportedFeatureError`） | 否 | 容量由 compact 处理；按请求大小换模型就是按内容路由 |
| 取消、`TurnSupersededError`、`BackgroundModelPreempted` | 否 | 不是 Provider 故障 |

### 5.2 结果未知与持久回执【决定】

- 一次模型请求本身没有外部副作用；副作用只来自 Host 收到响应后执行的工具调用。所以切 Provider 的风险是**重复计费**，不是重复副作用；带原生服务端工具（服务端搜索）的请求例外，它的副作用发生在上游。
- 只对确定未受理的失败切换，于是链上**最多只有一次可能计费的请求**：前面各跳都没被受理，最后一跳成功、失败或结果未知都照原规则处理。这样“增加付费调用”不会发生，Q5 说明计费口径。
- 回执不变：派发守卫的 `dispatched` 只存一次（`turn_execution.py:199-203`），备用跳的物理请求和传输重试一样复用 `self.prepared`，不再存第二个 `dispatched`。响应到达后存 `response`/`paired`，续接状态归实际回答的那一跳。冷恢复按原 journal：停在 `dispatched` 照旧是结果未知，**不因为配了链就重新派发**。
- 原生工具请求：执行器的触发条件已经保证只有 0 物理请求或全部未受理时才切；结果未知时 `turn_execution.py:686-699` 的现有判断仍然生效。

### 5.3 每一跳的派发前检查

依次检查，不满足就记为 `skipped` 跳过，不算触发：

1. 能力：用现有 `router.route(..., required_capabilities)` 的同一核验，换成该跳的 Profile。
2. 容量：用该跳的显式容量（`executor.py:491-513`），未声明保持未知。
3. 原生搜索：请求带 `native_tools` 而该跳没声明 `native_web_search` 即跳过。

全部跳过或失败时，抛出最后一个真正派发过的跳的异常；从未派发则抛链首的异常。`diagnostics["fallback_hops"]` 附上每跳的 `profile_id`、结果、类别、物理次数；`physical_request_count` 取各跳之和，供 `turn_execution.py:690-699` 判断“是否已有派发”。

### 5.4 跨协议连续性【决定】

- 只在不带续接状态的请求上切换。此时请求只有 `ChatMessage`（公共历史投影、本轮资料、当前输入），四个适配器本来就各自序列化这一层（`docs/architecture/model-providers.md:262`），不需要新的转换。
- Responses 加密推理、Claude 签名 thinking、Gemini thoughtSignature 都属于原链，永远不搬到另一个 Profile；Yuki 用 `store: false` 自己重放（`llm/openai_responses.py:33-34`），没有 `previous_response_id` 要迁移。
- 链中途链首故障：本版沿现有失败处理（存 `paired`、Work 进 failed、看门狗报告、所有者可在私聊恢复），恢复时新激活就是新链，会从链首开始、必要时切备用。中途切换的选项与风险见 Q3。

### 5.5 回切与延迟

- 无状态：每条新链先试链首。链首恢复后的第一条新链就回到链首，不需要计时或探测。
- 代价：链首持续故障时，每条新链先付链首的失败延迟。连接类故障的延迟由三项决定：连接超时 × (`max_retries`+1) + 退避（每次最多 2 秒）。现状 600 秒连接超时下最坏约 30 分钟（11-17 实测平均 1764 秒），备用链没有意义。
- 因此新增 Profile 可选字段 `connect_timeout_seconds`（`ModelProfile`，`models.py:97` 旁），只用于 `httpx.Timeout(timeout_seconds, connect=...)`；不设就和今天一样。所有者给链首设一个值（例如 10 秒）、并可显式调低链首的 `max_retries`，连接类故障就能在几十秒内切到备用。这两项都是显式配置，不是自动调节。连接失败不产生上游费用，所以先试链首没有额外付费。

## 6. 配置与管理

### 6.1 TOML【推荐，Q1、Q8】

```toml
[routes]
chat_agent = "openai_terra_low"

[fallbacks]                       # 可选；缺省或空列表 = 没有备用
chat_agent = ["deepseek_flash"]  # 有序，逐跳尝试

[profiles.openai_terra_low]
connect_timeout_seconds = 10     # 可选；不设 = 与 timeout_seconds 相同（现状）
```

`routes` 保持 `dict[str, str]`，旧文件不用改；`ModelRoute` 增加 `fallback_profile_ids: tuple[str, ...] = ()`。

### 6.2 校验（`ModelProfileCatalog._validate_routes`，`profiles.py:82-111`）

- 键必须是已知 `ModelTask`；每跳 Profile 存在、不等于链首、不重复。
- 每跳满足该用途的默认能力要求（`profiles.py:114-121`）。
- 结构化模式：链首是 `function_tool` 时每跳要有 `tools`，是 `json_schema` 时每跳要有 `structured_output`（因为请求按链首模式生成，见 Q6）。
- 原生搜索：链首 `search_mode` 是 `native`/`both` 时，每跳要声明 `native_web_search`。
- 不比较上下文窗口：备用窗口小只在单次请求放不下时跳过（§5.3），校验只记日志提示。
- 校验在读配置时一次完成，O(k)；失败沿现有 `ModelRuntimeConfigurationError`，Admin 保存时拒绝。

### 6.3 Admin / CLI / 热更新

- Admin 快照与保存加入 `fallbacks`（`admin/config_files.py:276-289`；`admin_api/model_models.py:74` 旁加 `fallbacks: dict[str, list[str]]`），**否则 Desktop 保存一次就会把链删掉**（§8 F18）。
- `yuki model routes`（`cli.py:1370-1380`）多输出 `fallback_profile_ids`。
- 热更新沿 `apply_catalog`：新链立即用新配置，已 pin 的激活保持旧链；无需重启。

## 7. 可观测性

- `model_invocations`：每跳一行（已有列 `profile_id`、`success`、`error_category`、物理次数），不加列、不迁移。同一次 `execute` 的各跳在 Main 里共享 `request_chain_id`。失败行的 `error_category` 继续写最外层类名，不改统计口径。
- `model_phases` 诊断（一次 `execute` 一条）增加 `route_hops: [{profile_id, outcome, category, physical}]`，`outcome` 取 `success`/`failed_not_accepted`/`failed`/`skipped_capability`/`skipped_capacity`；`category` 写真正的原因（如 `ConnectError`、`HTTP503`）。不保存请求正文。
- 日志一行：`model_fallback task=… from=… to=… reason=…`。
- 看门狗新增 `model_fallback:{task}`：用现有索引 `ix_model_invocations_task_created` 读该用途最近一次成功调用的 `profile_id`，不等于当前链首即为 present，等于链首即清除。只看状态，不设时间窗；通知节奏沿看门狗现有规则。
- `yuki model stats` 原四维聚合即可看到备用 Profile 的调用量与 token。

## 8. 失败情况与 E2E 用例

先列全部失败情况（F），再写 E2E。E2E 在隔离数据库、真实 Runner、本地假 HTTP 上游（按场景返回连接拒绝、429、503、200、读中断）上跑，不调付费 API。每个用例结束时导出产物 `artifacts/model-fallback/<用例>.json`：各跳 `model_invocations` 行、`model_phases.route_hops`、Work journal 阶段与预算计数、假上游收到的请求次数与 payload 摘要。重复跑必须得到同样的产物（时间与随机 ID 归一化后）。

| # | 失败情况 | 期望 |
| --- | --- | --- |
| F1 | 链为空，链首连接失败 | 与今天逐字节相同：同样的异常、行数、journal 阶段 |
| F2 | 链首 `ConnectError`（重试耗尽），备用成功 | 返回备用的响应；链首失败行 + 备用成功行；备用请求只到达一次 |
| F3 | 链首 429 / 503 / 401 | 同 F2 |
| F4 | 链首 `PoolTimeout` / 密钥缺失（0 次物理请求） | 同 F2，链首行 `physical_request_count=0` |
| F5 | 链首 `ReadTimeout` / `RemoteProtocolError` / 读中断 | **不切**；沿现有异常；备用收到 0 次请求 |
| F6 | 链首第 1 次 `ReadTimeout`、重试时 `ConnectError` | 不切（本跳有一次结果未知） |
| F7 | 链首 400 / 上下文超限 / 内容审查 / 空回答 / 截断 | 不切 |
| F8 | 请求带续接状态（链中途），续接所属 Profile 连接失败 | 不切，沿现有失败；不把续接状态发往别的 Profile |
| F9 | 链首切到 B 后，同一激活的下一次请求 | 直接走 B，不先试链首 |
| F10 | B 成功后进程重启，Work 冷恢复 | 按原 journal 续在 B；停在 `dispatched` 时照旧结果未知，不重发 |
| F11 | 下一条新链（下一轮）且链首已恢复 | 先试链首并成功；B 收到 0 次 |
| F12 | 备用缺能力（图片、原生搜索、结构化）或已知容量放不下 | 跳过并记 `skipped_*`，不算触发 |
| F13 | 所有跳都失败 | 抛最后一个派发过的跳的原异常，`fallback_hops` 完整，`physical_request_count` 为各跳之和 |
| F14 | 原生工具请求，链首连接失败 / 结果未知 | 连接失败可切（备用要有能力）；结果未知改抛 `LLMNativeToolError`，不切 |
| F15 | Work 根预算只剩 1 次请求，链首未受理 | 备用跳的预留失败，按预算耗尽收尾，不越过预算 |
| F16 | 取消或 Turn 被取代发生在链首等待中 | 不进入下一跳 |
| F17 | 热更新改链时有 pin 住的激活 | 旧激活走旧链；新链用新配置 |
| F18 | Admin/Desktop 保存不含 `fallbacks` 字段的快照 | 链不被删除（快照必须回传该字段） |
| F19 | 配置非法：未知用途、Profile 不存在、等于链首、重复、缺能力、结构化或原生搜索不匹配 | 读配置失败，Admin 拒绝保存 |
| F20 | 改了备用 Profile 后持久 Work 下次激活 | `profile_revision` 变化，显式开新链，Work ID 与预算不变 |
| F21 | 结构化任务（记忆提取），链首 503 | 同一模式的请求发到备用，校验通过；修复重试再从链首开始 |
| F22 | 公共 compact 请求链首连接失败 | 可切；执行 compact（带续接）不切 |
| F23 | 看门狗：最近一次成功在备用 → 报告；之后链首成功 → 清除 | 只读，无时间窗 |
| F24 | 后台任务（SELF best effort）被前台抢占 | `BackgroundModelPreempted` 不触发 |

E2E 用例：

- **用例 1（F1–F4、F13）** 群聊一轮、无工具：假上游依次模拟各类未受理失败，核对返回、行数、跳序。
- **用例 2（F5、F6、F7、F14）** 结果未知和非触发类：核对备用请求计数为 0、journal 阶段与今天相同。
- **用例 3（F8、F9、F10、F20）** 私聊 Work，多轮工具：首请求切到 B，后续请求留在 B；中途 kill 进程后冷恢复；再改 B 配置后验证开新链。
- **用例 4（F11、F17、F18、F23）** 两轮之间恢复链首并热更新配置；Admin 读取再保存后链保持；看门狗状态出现再清除。
- **用例 5（F12、F15、F16、F21、F22、F24）** 能力和容量跳过、预算边界、取消、结构化任务、公共 compact、后台抢占。
- 配置校验 F19 是纯函数，用一个参数化测试先写全部非法配置，再写实现。

真实 API 抽检（实施后、上线前，所有者同意时做一次）：把链首 `base_url` 指向一个必然拒绝连接的本地端口，跑一轮真实私聊，确认备用 Profile 的真实响应、usage 与诊断；不制造真实 5xx。

## 9. 待决问题（默认按推荐执行）

**Q1 粒度：按用途还是按 Profile？默认空链是否满足“不扩大为隐式策略”？**【推荐：按用途，默认空】
理由：路由本来就是 `ModelTask → Profile`，用途决定所需能力与结构化模式，链挂在路由上校验最直接；按 Profile 配会让同一 Profile 在不同用途下备用相同，但能力要求不同，校验做不完整。默认空链就是没有 fallback；只有所有者写了 `[fallbacks]` 才生效，触发条件是固定的失败类别，不看内容、日期、品牌，满足“显式配置、不扩大为隐式策略”。

**Q2 触发条件，尤其结果未知。**【推荐：§5.1 表；结果未知不切】
理由：只切确定未受理的失败，链上最多一次可能计费，与“未知不授予重放资格”（`development-contract.md:115`）一致。代价：`RemoteProtocolError` 这类断连不被覆盖，近 14 天只有 6 次物理尝试。504 先按 5xx 处理（已被现有传输重试视为可重发）；如果所有者认为 504 等于可能已计费，就把 504 移到“否”。备选：链配置里加 `on_unknown=true`，让所有者显式接受可能双重计费，本版不做。

**Q3 链中途能否切换？**【推荐：本版只在链首切；中途失败沿现有恢复】
理由：中途切换要把链内已有的工具回合改写成可移植消息。Gemini 3 对当前轮缺 thoughtSignature 的 functionCall 可能返回 400，Claude 开思考做工具续写需要上一条 assistant 的签名 thinking（官方文档，本仓未用真实 API 验证【假设】）；Gemini 适配器序列化可移植工具调用时不带签名（`llm/gemini.py:151-163`）。现有 `rebase_business` 只服务 Work 恢复且只带最后一轮。持续故障时，新激活都从链首开始、快速失败后切到备用，中途失败只出现在故障开始时已在运行的链；11-21 的数据是 18 条链中 9 条。若上线后中途失败仍是主要损失，第二阶段再在 Work 恢复边界复用 `rebase_business`（新链、可移植回合、不带 opaque），并先在三种目标协议上做真实 API 验证。

**Q4 粘性与回切。**【推荐：链内跟随续接状态；每条新链从链首开始；不设 cooldown】
| 方案 | 利 | 弊 |
| --- | --- | --- |
| A 每条新链从链首开始（推荐） | 无状态、无时间阈值；链首恢复后立即回切；链首缓存保持热 | 持续故障时每条新链先付一次链首失败延迟（连接失败不计费）；需要 `connect_timeout_seconds` 才能控制在几十秒内 |
| B 进程内记住“最近一次成功的跳”，新链从它开始 | 持续故障时没有额外延迟 | 回切需要有人再试链首：要么周期探测（时间阈值），要么每 N 次试一次（经验阈值），都违背所有者偏好；进程重启后状态丢失，行为不可复现 |
| C 以“下一次成功探测”为准的后台探测 | 回切准确 | 额外请求或额外机制；连接成功不代表模型请求成功 |

**Q5 预算与计费。**【推荐：备用跳按辅助请求计入原 Work 根预算；显式配置视为授权使用该 Provider】
理由：备用跳和传输重试同一入口（`json_http.py:316-319` 的 `reserve_request(auxiliary=True)`），不重置预算、不另开账本；预算耗尽就不进入下一跳（F15）。前面的跳都没被受理，逻辑调用总共最多一次可能计费，所以不“增加付费调用”；备用的单价可能不同，所有者配置链即授权在链首未受理时使用它。缓存：备用首次是冷缓存，回切后链首缓存仍在（TTL 内），`model stats` 可分别看到两者的缓存率。`model_invocations` 每跳一行，未受理行的 token 为空、物理次数如实记录。

**Q6 结构化任务。**【推荐：请求按链首的结构化模式生成，校验要求每跳支持该模式；允许给记忆用途配链，但优先给 `chat_agent` 配】
理由：两种模式四种协议都已支持（`docs/architecture/model-providers.md:50`），按链首模式发即可，不需要按跳重建请求；Claude 开思考时 `tool_choice=required` 被适配成 auto，现有 Runner 严格校验恰好一个 `emit_result`，不加隐藏调用。记忆任务失败本来会留在批次里下次再处理，备用价值低于主对话。

**Q7 可观测性。**【推荐：§7；不加迁移】
理由：每跳独立行加诊断里的 `route_hops` 已能回答“走了哪一跳、为什么”；看门狗按状态报告持续 fallback，不需要时间窗。若日后要对 `model_invocations` 做按跳聚合，再加列。

**Q8 配置与管理。**【推荐：TOML `[fallbacks]` + Admin 字段 + CLI 显示，热更新；Desktop 界面后续做】
理由：保持 `routes` 不变，旧文件和 Desktop 不受影响；Admin 必须回传字段（F18）。校验见 §6.2。

**Q9 与 compact、容量的关系。**【推荐：容量不足不是触发条件；备用放不下就跳过】
理由：链首放不下由 compact 处理（自动整理触发值是全局的 96000，`docs/architecture/conversation-compact.md:7`）；按请求大小换模型就是按内容路由。备用窗口更小时，只在那次请求放不下时跳过。已知局限：激活被续接状态固定在 B 后，Main 的容量规划仍按链首查询（`executor.py:901-916`），B 的容量更小时后续请求可能撞到 B 的容量上限，按现有容量失败收尾；配置时建议备用的上下文窗口不小于自动整理触发值加输出额度。

**Q10 删除优先。**【推荐：没有可以删除的现有备用逻辑；改掉“without fallback”等陈述】
理由：已核对，Yuki 现在没有模型级备用；传输重试处理同一跳的瞬时抖动且不冷缓存，不能被链替代；视觉、Tavily 的隐式回退已删除，本书不恢复。需要改的是 `routes.py:11` 的注释、`model-providers.md` 与 `development-contract.md` §3 的“显式配置”描述，补上显式链的边界。

**Q11 性能。**【推荐：如下，不新增写事务】
选链 O(k)，k 为配置跳数，数据来自已 pin 的内存 catalog，不读库。每跳最多一次 `model_invocations` 写入（沿现有诊断写入，不在模型等待期间持有 writer），非链首跳一次 Work 预算预留（与传输重试相同的单行短事务）。没有任何写事务等待网络。看门狗每个用途一次带索引的只读查询（`ix_model_invocations_task_created`，O(log n)）。

**Q12 `connect_timeout_seconds` 是否进入本版？**【推荐：进入，可选字段，默认不设】
理由：没有它，连接超时类故障要 30 分钟才轮到备用（§1.1）。只是把 httpx 已有的连接超时从总超时里分出来，由所有者显式填写，不是自动调节。

## 10. 硬约束逐条对照

| 约束 | 怎么满足 |
| --- | --- |
| 协议适配器不加入按内容强制选择 Provider 的路由；失败兜底不得扩大为隐式策略 | 链只在执行器；适配器只多记一个“未受理”计数。触发只看失败类别；默认空链；配置显式可见；看门狗持续报告 |
| 不按内容隐式切换 Provider 或增加付费调用 | 不看请求或回答内容；只在未受理后切，逻辑调用最多一次可能计费；结果未知不切 |
| 任务恢复按原执行 ID 和持久回执，不重置预算、不盲目重跑或重发 | journal 只存一次 `dispatched`；续接状态记录实际 Profile；冷恢复沿原规则；备用跳计入原根预算 |
| 禁止防御性编程、删除优先 | 不加开关、熔断器、健康探测、cooldown；复用路由核验、辅助预算、pin、调用记录；无可删的旧备用 |
| 写事务内不等外部 I/O | 选链不读库；预算预留与记录沿现有短事务 |
| 固定工具合同 | 各跳收到同一份已声明工具；不因换跳改声明 |

## 11. 上线与回滚

- 上线：合并后 Bot-only 部署，不需要迁移。上线时链为空，行为不变。所有者再显式写 `[fallbacks]`（建议先只配 `chat_agent`，备用选一个**不走同一出网代理**的 Profile，否则代理故障时两跳一起失败），并给链首设 `connect_timeout_seconds`。部署前按惯例检查即将到期或正在运行的自动化。
- 观察：上线后 7 天对比 `chat_agent` 网络失败的逻辑调用数、备用调用数与 token、看门狗 `model_fallback` 次数。
- 回滚：删掉 `[fallbacks]` 即关闭（热更新）；代码回滚不涉及 schema。已经在 B 上的持久 Work 因 `profile_revision` 变化下次激活开新链，不丢 Work。

## 12. 要改的文件

- `model_runtime/models.py`：`ModelRoute.fallback_profile_ids`；`ModelProfile.connect_timeout_seconds`。
- `model_runtime/profiles.py`：`[fallbacks]` 解析与 §6.2 校验。
- `model_runtime/routes.py`：返回有序候选（链首 + 备用），去掉“without fallback”。
- `model_runtime/executor.py`：`_execute` 改为逐跳循环；`profile_revision` 覆盖整条链；`route_hops` 诊断与日志。
- `model_runtime/request_accounting.py`：`not_accepted` 计数。
- `llm/json_http.py`：失败分支记未受理；`httpx.Timeout` 接 `connect_timeout_seconds`（各 native 适配器构造参数随之传入，`model_runtime/pool.py`）。
- `admin/config_files.py`、`admin_api/model_models.py`、`admin_api/openapi.py`：`fallbacks` 回传与保存。
- `cli.py`：`yuki model routes` 显示链。
- `application/watchdog.py`：`model_fallback:{task}` 检查。
- `config/model_profiles*.example.toml`：注释示例（默认不启用）。
- 文档：`docs/architecture/model-providers.md`（显式链、触发表、计费口径）、`development-contract.md` §3 一句边界、`docs/architecture/README.md` 索引、`execution-trace.md`（`route_hops`）。
- 测试：§8 的 E2E 与配置校验参数化测试；CI 沿现有 unit/integration 套件，无新 job。

## 13. 实施清单

1. 先写 §8 F19 的参数化校验测试与 5 个 E2E 用例（预期失败），再写实现。
2. 配置模型与校验（models、profiles），Admin 回传与 CLI 显示。
3. 传输层未受理计数与连接超时字段。
4. 执行器逐跳循环、合同指纹、诊断与日志。
5. 看门狗检查。
6. 文档同步（§12），README 索引。
7. 本地 ruff、mypy、unit、integration；E2E 产物归档。
8. PR、CI、合并、Bot-only 部署分别记录；部署后由所有者决定是否写入 `[fallbacks]`。
