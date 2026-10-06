# 模型供应商与协议合同

模型由 `ModelTask → ModelProfile → Provider` 显式绑定。更换供应商只影响协议转换；
主 Agent、插件、自动化、记忆、媒体与 Work 共用原执行层，不按消息内容自动选择模型。
部署向导可选择主模型供应商和协议；多个 Profile 可分别配置 endpoint、密钥环境变量和任务路由。
默认示例仍是 DeepSeek，不会自动覆盖部署中的 `.env`、模型路由或人格提示词。
部署文件统一为 `webui-config/model_profiles.toml`，API Key sidecar 与它同目录；旧部署
`config/model_profiles.toml` 的迁移步骤见[运维说明](../operations/model-profile-path-migration.md)。
指定的模型文件缺失会阻止启动；没有 TOML 的旧环境变量路由只允许显式临时开启
`MODEL_PROFILES_LEGACY_COMPATIBILITY=true`，不能作为无声回退。
WebUI 成功保存模型连接与任务路由后，新任务立即使用新配置；已经开始的模型请求固定原连接，
持久 Work 的下次激活因 Profile revision 变化显式开启新链，保留原 Work ID、预算和执行证据，
不重跑已完成的工具效果。

## 协议与能力

| Yuki 能力 | Chat Completions | Responses | Claude Messages | Gemini GenerateContent |
| --- | --- | --- | --- | --- |
| 固定工具声明、并行调用与逐项回执 | 支持 | 支持 | 支持 | 支持 |
| 图片输入、按需媒体工具 | 支持，需声明模型能力 | 同左 | 同左 | 同左 |
| 结构化任务 | function tool / JSON Schema | 同左 | 同左 | 同左 |
| 截断判定、同 Work 续跑与预算 | 支持 | 支持 | 支持 | 支持 |
| 独立思考通道与协议状态 | reasoning_content / reasoning_details / encrypted_content | reasoning items | 签名 thinking blocks | thoughtSignature parts |
| 外部搜索、MCP、插件、终端等本地工具 | 支持 | 支持 | 支持 | 支持 |
| 上游原生搜索 | 显式配置的搜索专用 Profile | 依 Provider 能力；DeepSeek 主调用关闭 | Claude `web_search_20250305`，需 Profile 声明且部署搜索模式为 native | Gemini 3 GenerateContent 的 Google Search，需 Profile 声明 |

这里的支持指适配器与 Yuki 执行合同通过离线协议回放，不代表任意同名模型都支持这些功能，
也不代表各家服务已完成真实 API 或 QQ 验收。Profile 的能力声明必须符合实际模型。

主 Agent 当前/引用图片、历史 `inspect_conversation_attachment`、工作区 `workspace_inspect`
和获准 MCP 工具图片共用原主模型图片输入。历史/工作区工具只准备像素，不再调用独立
Qwen；DeepSeek 使用这些工具也走自身已声明的 `image_input`，并非固定先读取视觉摘要。
本次采用各协议已有的用户图片编码：整批文字工具回执配对后追加 Host 媒体观察，保留原
call_id、工具顺序、签名和 opaque continuation。不自动尝试多模态 functionResponse 或
function_call_output.output[]，也不在上游拒绝图片后切换 Qwen。没有图片能力明确未读。

`user` 图片观察属于 Agent 已选取资料的协议承载，不产生新用户事件或额外授权。主请求
继续使用原 pinned Profile 和原预算；图片准备没有第二次视觉模型请求。配置图片能力仅
说明声明，不能证明实际端点可读取。五类适配器的 HTTP payload 可离线回放；本次真实
Gemini/DeepSeek API 图片验收与生产速度验收尚未完成。插件自有 handle 与 SDK MCP 显式选图
桥接已按其 owner/委托合同做本地验证，不从通用编码推定真实上游已验收。

独立视觉连接仍服务后台表情分类、表情发送候选选择和 SDK 显式 `VISION_ANALYZE`；它不再
作为普通主 Agent 当前/引用图片缺少原生能力时的隐式回退。ASR 仍可能借用 Qwen URL/key，
且借用不依赖 `vision_enabled`。保留这些消费者期间不能删除共享密钥或声称可整体关闭
独立视觉配置。详见[聊天媒体](chat-media-workspace.md)。

原生搜索不能假装为客户端函数；Claude 搜索结果以 server tool 回执和引用记录，Gemini
Google Search 的工具调用、结果和 thought signature 按原样保存在私有续跑状态中。
Gemini 的 Google Search 与函数工具同请求在当前 Cloud Code 代理路由上返回 400，
因此 Gemini 主 Agent 应选 `search_mode="bridge"`：保留固定 `web_search` 与
`read_webpage` 函数声明，只有执行 `web_search` 时另发一条只含 `googleSearch`
的 Gemini 请求。桥只采纳上游 `groundingMetadata.groundingChunks[].web` 来源；
模型正文里的 URL 不算来源。无可信来源或请求失败时显式退到已配置的 Tavily，
回退结果不进桥缓存。此模式要求部署的 WebMode 为 tavily/both 且 Tavily 凭据可用，
以便读网页与降级；模型配置保存时按发起工具调用的任务连接热切换桥。
已开始的 Runner 将搜索后端与模型连接一起固定至该轮结束；热保存只影响后续 Runner。
Gemini 搜索桥使用原连接的 `timeout_seconds` 和 `default_max_output_tokens`，不额外
施加 Web 超时或 2048 token 输出上限；DeepSeek 桥同样沿所选搜索 Profile 配置，
保留显式输出限额核验。Tavily/网页请求的 `WEB_TIMEOUT_SECONDS` 默认 180 秒；
部署中显式配置的旧值需单独调整。
每个模型连接的 `search_mode` 可选 `external`、`bridge`（Gemini）、`native` 或在协议允许时选 `both`；
旧文件未填写时沿用部署搜索模式。直接 `native/both` 须声明 `native_web_search` 能力；
`bridge` 在独立请求中使用 Google Search，不向主请求声明该能力或内联原生工具。
Claude 原生工具与本地 `web_search` 同名，原生模式不向 Claude 声明外部搜索函数。
全局搜索禁用仍会关闭所有联网工具；外部模式还需要部署中确实配置外部搜索后端。
主 Agent 使用完整函数合同，Chat 搜索专用模型不能混用该合同；它们也不能通过 `tool_choice=none`
保证禁用服务端搜索。需要联网的主 Agent 可配置现有外部搜索工具。

## Chat 供应商预设

`provider` 用于选择参数方言，不通过模型名称猜测，不在上游 400 后自动删参数重试。

| provider | 思考参数与差异 |
| --- | --- |
| openai、azure_openai | reasoning_effort、max_completion_tokens；不回传非标准 reasoning_content |
| deepseek | thinking.type=enabled + reasoning_effort；不发送 tool_choice；合法整段 DSML 转为声明内工具调用 |
| qwen | enable_thinking + thinking_budget |
| moonshot、zhipu | thinking.type=enabled；不同型号的 effort 支持须显式配置 |
| doubao | thinking.type=enabled + reasoning_effort；保留 encrypted_content |
| minimax | 声明思考专用模型；reasoning_split=true，完整保留 reasoning_details |
| openrouter | reasoning.effort、reasoning.exclude=false，保留 reasoning_details |
| groq | reasoning_effort、max_completion_tokens、include_reasoning=true；默认针对 GPT-OSS 方言 |
| mistral | reasoning_effort 最低发 high，保留 thinking 内容块；须选支持该字段的模型 |
| siliconflow、together、xai、openai_compatible | 通用 reasoning_effort 方言，必须选支持该字段的模型或显式覆盖 |

Azure 仅接入 `/openai/v1/` API，model 填部署名；旧的 deployment URL 和 api-version 路径不在此预设内。
本适配器使用 Bearer key，Azure v1 支持该鉴权方式。
Chat 默认省略 temperature，避免思考模型不支持或仅支持固定温度；需要时显式启用。
上游支持多种接口不意味着可以跨协议续跑同一私有状态。

### 按模型覆盖参数

在原 Profile 内添加 `wire_options`，只覆盖声明的字段，其余仍用供应商预设。例如：

```toml
[profiles.main.wire_options]
reasoning = "effort"             # 例如使用 reasoning_effort 的 Kimi 型号
token_field = "max_completion_tokens"
send_temperature = false
effort_levels = ["low", "high", "max"] # 可选，按具体型号声明支持的档位
```

可用 reasoning 方言：`effort`、`thinking`、`enable_thinking`、`openrouter`、`builtin`；
Claude 使用 `effort`（adaptive）或 `budget`，Gemini 使用 `gemini`（thinkingLevel）或 `budget`。
Responses 不使用此配置表；Claude 只接受 reasoning/budget/effort_levels，Gemini 另接受
send_temperature，以及 `gemini_schema_format`。后者默认 `response_json_schema`；显式
`response_schema` 将结构化输出投影为 Gemini Schema 方言，仅 Gemini 允许。投影保留可表示的
结构和引用展开，额外属性、长度和数值限制继续由本地原 schema 与来源校验执行；不支持的
组合或递归引用在提交前拒绝。此字段不改变主 Agent 工具参数、Provider 或任务路由，
显式修改进入恢复合同 hash。填入另一协议的字段在配置加载时拒绝，不会悄悄忽略。
`thinking` 可用 `send_reasoning_effort=true` 表明该型号同时支持 effort。
`builtin` 只适用于已经验证、始终思考且不接受思考控制参数的模型，不能用来接入无思考模型。
没有 effort 控制的 `thinking` / `builtin` 方言拒绝高于 low 的请求，不静默降低要求。
声明 `effort_levels` 后，取不低于请求的最小支持档位，没有更高档位则在请求前拒绝。
因此 Mistral 支持 none/high 的型号用 high 满足 low 下限，DeepSeek medium 映射 high。
Groq 的其他型号可显式设置 `reasoning_format="parsed"`；此时不发送 include_reasoning，
两种字段不混用，不按模型名猜测方言。

预算接口用 `thinking_budget_tokens` 表示 low 的预算（默认 4096，至少 1024）；
medium/high/xhigh/max 分别为该基数的 2/4/8/16 倍。这是一项明确的单调转换策略，
不声称与供应商 effort 精确等价。Claude 手动思考预算必须小于输出预算，否则在请求前拒绝；
其他供应商的型号上限也须自行核对，不能靠降低预算掩盖不支持的配置。

旧型号 Claude 可配置 `reasoning="budget"`；Gemini 2.5 可配置同一方言。
Gemini 3 各型号支持的 thinkingLevel 可能不同，不支持的档位由服务明确拒绝。
Claude 思考开启时不支持强制调用特定工具，适配为 auto；结构化任务仍由现有 Runner
严格检查恰好一个 emit_result 及 schema，不增加隐藏模型调用。

`headers` 支持 OpenRouter 的站点标识、Anthropic beta 等非鉴权 Header；不能覆盖认证字段，
不能放换行或把密钥写入 TOML。`api_key_env` 是密钥引用；可读取进程环境变量，或由 WebUI
将 operator 输入的 API Key 保存在模型文件同目录的私有 `model_profiles.secrets.json` 中。
WebUI 查询不会回传密钥；客户端不跨供应商或密钥来源共享。
新增供应商示例见 [多供应商配置](../../config/model_profiles.providers.example.toml)。
Gemini 3.8 Flash 的官方模型 ID 是 `gemini-3.8-flash`。WebUI 的 Google Gemini 预设使用
`https://generativelanguage.googleapis.com/v1beta` 与原生 GenerateContent，预填该 ID、
`low` 思考强度及文字、图片、工具、结构化输出能力。Gemini 3.8 的 Yuki 连接使用
`thinkingLevel`，拒绝该型号的固定 `thinkingBudget` 配置；代理转发仍需另行核对，不能把
代理改写误认为 Yuki 请求。适配器保留工具回合的 thought signature，
按上游 `cachedContentTokenCount` 统计缓存。Google 搜索桥须在此连接明确选择；默认仍走
部署配置的外部搜索。此连接不实现 Interactions API 或 Live/TTS；这些能力不能因为模型
本身支持就标成已接入。
无 TOML 的兼容配置也使用同一个客户端池；显式 `LLM_PROVIDER=anthropic/gemini`
分别采用对应原生协议，其他兼容供应商保持 Chat。额外命名的 endpoint/model/key 变量需要
存在于进程环境；Docker Compose 的 env_file 会加载 `.env`。本地 CLI 若只使用 Settings
读取 `.env`，其默认 LLM/LLM_FLASH 字段可用，额外变量须先导出，不会偷偷扫描其他密钥文件。

## 私有状态与恢复

群史 Rollup 默认使用 600 秒专用请求/整批等待期限，独立于主任务 Profile 超时；
Work compaction 继续使用原任务 Profile 的超时。两类摘要的生成预算默认 32768 token，
包含思考与最终结构输出；群史摘要正文默认上限仍为 16384 字符。输出预算热配置没有
额外的 32768 界面上限，但执行器继续拒绝超过 Profile `max_output_tokens_limit` 或真实
输入/联合窗口的请求，不静默减量或更换模型。已有显式配置不会因默认值调整自动改变。

- 每次响应的工具 ID、签名思考和原生块按原顺序保存在私有 ProviderContinuation；
  工具回执和用户改向按到达顺序追加，不重建已有调用，不重发已确认效果。
- Gemini 无原生 call ID 时，仅在接纳响应时生成确定性内部调用 ID；原始 thoughtSignature
  原样保存，内部映射不发给上游。它不是平台 message_id。
- WorkJournal 保存完整检查点；进程重启保持执行身份、请求链、预算、调用 ID 和实际 HTTP 请求。
  供应商、协议或 Profile 修订变化必须显式开新链，不能把旧签名状态拼入新模型。
- 普通聊天与获准动态快照可在各协议保留。公共投影扩展原生块仅支持 Responses；
  其他协议的工具、签名和 continuation 私有检查点只属于当前 Work。
  非 Work 普通投影遇到不可共享的原生块时结束该视图的扩展；开启 Work runtime 时只冻结获准的
  初始普通输入，其后的工具和 continuation 留原 journal，下一普通轮仍复用普通投影。
  合同或来源变化按原原因建立新链，不改写旧聊天事件；各协议的原生块跨 Work 复用并不等价。
- Chat `length`、Claude `max_tokens`、Gemini `MAX_TOKENS` 均转为 INCOMPLETE；
  Runner 的既有截断处理不会执行其中的工具。不自动增加预算。
- Gemini 明确返回 `MALFORMED_FUNCTION_CALL`，且请求没有原生服务端工具、响应仅含空文本而无
  可执行调用或原生效果证据时，Runner 在原链追加工具格式纠正反馈，最多纠正两次。
  每次仍走原请求接纳、来源核验和累计预算；Work journal 保存纠正次数和反馈，重启不重置。
  不构造缺失的工具调用，不改工具声明或重发已确认效果；安全拦截、非法 JSON、矛盾响应和
  结果不明的传输失败不进入此纠正路径。已有确认交付的普通聊天按原收尾规则结束。
- reasoning、签名、完整工具回执不进入对外消息或普通运行日志；只有显式 send_message 交付。
- [执行诊断](execution-trace.md) 单独保存实际返回的可读思考和工具结果；正文权限查询，按期清理。
  不透明签名/加密状态只留摘要，原恢复 journal 继续按协议私有合同保存。
- 请求原生服务端工具时，传输结果不明不自动重试；普通有界传输重试仍计入 Work 请求预算。

## 用量与 HTTP 请求口径

`model_invocations` 一行表示一次逻辑模型调用，`calls` 继续按该行计数。`physical_request_count`
只统计实际进入 HTTP 客户端的请求尝试，包括传输重试和 Claude 原生搜索暂停后的续发；
路由、配置或本地校验失败不计入。`unknown_usage_request_count` 统计其中未获得上游总 Token
报告的尝试，不能按零 Token 或零费用处理。历史调用的两个字段为 NULL，无法从原逻辑记录
反推出真实 HTTP 次数。缓存率只使用上游明确报告缓存量的输入作分母，并同时保留未报告计数。

原生搜索是否启用以当次请求合同记录；Google/Claude 的原生搜索可能另有工具费用，当前账本
没有供应商账单或可靠的原生工具价格，费用显示为未知，不从 Token 用量推算为零。
Gemini 独立搜索桥另记一条 `model_invocations.task=web_search`，归入当前连接与模型，
记录上游返回的输入、输出、缓存 Token 和实际 HTTP 请求数；无可信 grounding 记失败，
随后 Tavily 降级不伪装成 Gemini 命中。

## 验证边界与协议来源

定向测试覆盖参数、图片/schema、截断、私有签名、工具结果顺序、SQLite 重启后的 HTTP 字节一致性，
以及真实 Runner/隔离数据库/假网关中的可见输出边界。Gemini 独立桥已用现有连接凭据
在无 QQ/Work 的合成请求中得到上游可信 grounding 与用量；Bot 容器用现有连接地址的
搜索专用请求也返回可信来源。生产主 Agent 尚未启用桥，因此桥的真实 QQ 工具回合仍待验收。
模型效果、长上下文缓存和实际账单计费需独立验收。

- [OpenAI Chat 参考](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)
- [DeepSeek 思考模式](https://api-docs.deepseek.com/guides/thinking_mode/)
- [Qwen 思考参数](https://www.alibabacloud.com/help/en/model-studio/deep-thinking)
- [Kimi 思考模型](https://platform.kimi.ai/docs/guide/use-thinking-models)
- [MiniMax OpenAI 接口](https://platform.minimax.io/docs/api-reference/text-openai-api)
- [Groq 思考协议](https://console.groq.com/docs/reasoning)
- [Mistral 思考内容块](https://docs.mistral.ai/studio/conversations/reasoning)
- [GLM 思考模式](https://docs.z.ai/guides/capabilities/thinking-mode)
- [Claude adaptive thinking](https://platform.claude.com/docs/en/build-with-claude/adaptive-thinking)
- [Claude 结构化输出](https://platform.claude.com/docs/en/build-with-claude/structured-outputs)
- [Gemini thought signatures](https://ai.google.dev/gemini-api/docs/generate-content/thought-signatures)
- [Azure v1](https://learn.microsoft.com/en-us/azure/ai-foundry/openai/api-version-lifecycle?tabs=key)
