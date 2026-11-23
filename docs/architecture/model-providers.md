# 模型供应商与协议合同

## 非聊天模型连接

可编辑文档的 Profile 可选字段以 JSON `null` 表示“未設定”：保存时省略对应 TOML 字段，
解析继续使用 `ModelProfile` 的现有默认；明确的 `false`、推理 `none` 档位等值原样保留。
关闭尚未配置的 Embedding 可只提交 `enabled=false`；其未填写的 provider/维度由后端现有
DashScope 默认提供，不要求前端虚构连接。启用时仍需要真实的端点和模型。

同一 v3 `model_profiles.toml` bundle 可包含 `[embedding]`、`[vision]`、`[asr]` 与 `[search]`。
前三段显式保存 `enabled`、端点、模型和 `api_key_env`；视觉使用现有 Chat Completions 适配，
ASR 可明确选择 `chat_completions` 或 `audio_transcriptions`，Embedding 使用 DashScope 原生
或 OpenAI 兼容 `/embeddings`。`[search]` 保存 `tavily_api_key_env` 与可选的 `mode`；
`mode` 使用原有 `disabled/native/tavily/both`，与已有搜索路由共同确定运行方式。

段存在时采用该段，不用旧设置补全；`enabled=false` 明确关闭，不退回另一套连接。
段缺失的既有部署暂保留旧初始化来源，按升级命令迁移后退出。并发、超时、保留期限等原行为设置
继续沿现行来源；客户端不另存配置或预设表。密钥同样只进 sidecar 或实际环境来源，不在可读文档
或接口中回传。Settings 已读取的旧凭据别名在迁移期与进程环境、sidecar 沿同一解析链处理。

一次性离线 `yuki admin model migrate-legacy`、旧聊天参数退役及下一版本的删除条件见
[模型配置升级](../upgrade-model-config-20261031.md)。普通 Main 请求直接使用所选 Profile 的
默认温度、输出额度与思考配置，调用者的显式单次参数仍保留；不再叠加旧 `llm.*` 运行覆盖。

这些连接及其密钥变更需要重启，接口准确返回 `apply_mode=restart`、磁盘/已加载配置状态；
重启前保留原运行资源与已开始的请求。未改变非聊天连接时，聊天模型原热应用路径继续使用。
更换 Embedding 保留原不可变 profile 与旧向量，建立新 profile，不覆盖旧索引。

模型由 `ModelTask → ModelProfile → Provider` 显式绑定。Provider 是连接名称，协议由 Profile 显式选择；
主 Agent、插件、自动化、记忆、媒体与 Work 共用原执行层，不按消息内容自动选择模型。
每个用途还可显式配置一条有序备用链，只在请求确定未被受理时使用，见[显式备用链](#显式备用链)。
部署向导可选择主模型供应商和协议；多个 Profile 可分别配置 endpoint、密钥环境变量和任务路由。
默认示例仍是 DeepSeek，不会自动覆盖部署中的 `.env`、模型路由或人格提示词。
部署文件统一为 `webui-config/model_profiles.toml`，API Key sidecar 与它同目录；旧部署
`config/model_profiles.toml` 的迁移步骤见[运维说明](../operations/model-profile-path-migration.md)。
指定的模型文件缺失会阻止启动；配置必须是完整 v3 TOML。环境变量仍可由
`base_url_env/model_env/api_key_env` 显式引用，不再提供无 TOML 路由回退。
管理 API 成功保存模型连接与任务路由后，新任务立即使用新配置；已经开始的模型请求固定原连接，
持久 Work 的下次激活因 Profile revision 变化显式开启新链，保留原 Work ID、预算和执行证据，
不重跑已完成的工具效果。
Gemini、Claude 原生协议的连接品牌名只作展示；修订按实际原生参数预设计算，改名不改变
原请求合同或续接归属。endpoint、model、密钥引用、headers、显式 wire 等真实配置变化仍
参与修订；Chat 的供应商参数预设影响实际请求，继续参与修订。

## 协议与能力

| Yuki 能力 | Chat Completions | Responses | Claude Messages | Gemini GenerateContent |
| --- | --- | --- | --- | --- |
| 固定工具声明、并行调用与逐项回执 | 支持 | 支持 | 支持 | 支持 |
| 图片输入、按需媒体工具 | 支持，需声明模型能力 | 同左 | 同左 | 同左 |
| 结构化任务 | function tool / JSON Schema | 同左 | 同左 | 同左 |
| 截断判定、同 Work 续跑与预算 | 支持 | 支持 | 支持 | 支持 |
| 独立思考通道与协议状态 | reasoning_content / reasoning_details / encrypted_content | reasoning items | 签名 thinking blocks | thoughtSignature parts |
| 外部搜索、插件、终端等本地工具 | 支持 | 支持 | 支持 | 支持 |
| 上游原生搜索 | 显式配置的搜索专用 Profile | 依 Provider 能力；DeepSeek 主调用关闭 | Claude `web_search_20250305`，需 Profile 声明且部署搜索模式为 native | Gemini 3 GenerateContent 的 Google Search，需 Profile 声明 |

这里的支持指适配器与 Yuki 执行合同通过离线协议回放，不代表任意同名模型都支持这些功能，
也不代表各家服务已完成真实 API 或 QQ 验收。Profile 的能力声明必须符合实际模型。

主 Agent 当前/引用图片、`view_file`（历史附件、工作区或本人电脑文件）
和获准工具图片共用原主模型图片输入。历史/工作区工具只准备像素，不再调用独立
Qwen；DeepSeek 使用这些工具也走自身已声明的 `image_input`，并非固定先读取视觉摘要。
本次采用各协议已有的用户图片编码：整批文字工具回执配对后追加 Host 媒体观察，保留原
call_id、工具顺序、签名和 opaque continuation。不自动尝试多模态 functionResponse 或
function_call_output.output[]，也不在上游拒绝图片后切换 Qwen。没有图片能力明确未读。
各方言的续写载荷都由 Host 重放（Responses `store: false`），已发出的观察图像随载荷重发；
超出帧/字节上限的最旧观察在发出的请求及其载荷中改为本方言的文字部分（`llm/request_media.py`），
见[结果预算](tool-results.md#工具图像与长循环)。

`user` 图片观察属于 Agent 已选取资料的协议承载，不产生新用户事件或额外授权。主请求
继续使用原 pinned Profile 和原预算；图片准备没有第二次视觉模型请求。配置图片能力仅
说明声明，不能证明实际端点可读取。五类适配器的 HTTP payload 可离线回放；本次真实
Gemini/DeepSeek API 图片验收与生产速度验收尚未完成。插件自有 handle 与 插件显式选图
桥接已按其 owner/委托合同做本地验证，不从通用编码推定真实上游已验收。

独立视觉连接服务首次收藏表情的可选后台描述和 SDK 显式 `VISION_ANALYZE`；选图读取本地文件索引，不调用视觉模型。Qwen 的一次分析只发一次 HTTP 请求，超时、连接失败、429 和 5xx 均不隐式重发。它不再
作为普通主 Agent 当前/引用图片缺少原生能力时的隐式回退。ASR 仍可能借用 Qwen URL/key，
且借用不依赖 `vision_enabled`。保留这些消费者期间不能删除共享密钥或声称可整体关闭
独立视觉配置。详见[聊天媒体](chat-media-workspace.md)。

原生搜索仍由供应商执行。内联方式将原生工具放入主模型请求；独立方式保留主 Agent 的
`web_search` / `read_webpage` 函数合同，仅在执行 `web_search` 时另发搜索专用请求，
不传主聊天历史或函数清单。两个入口复用原协议适配器，不按查询内容或供应商品牌改路由。
Gemini 的 Google Search 与函数工具混用曾在 Cloud Code 代理上返回 400，独立搜索可避免
这一组合；兼容协议或能力声明都不证明当前端点真实可用。

顶层 `search_backend="model"` 与 `search_connection="连接ID"` 明确选择本地搜索的独立模型；
`search_backend="tavily"` 则选择 Tavily 外部搜索。缺省后端为 `model`，不从 Tavily 密钥或
旧隐式默认值选择外部搜索。`[search].mode` 明确设置时为运行模式来源，`disabled` 关闭
联网工具；选择后端或填入密钥不会自动开启。模式、密钥和非聊天连接变更均需要重启。
该字段缺失或为 `null` 的过渡文档继续使用原 `WEB_MODE`；原开关也未设置时，旧
`WEB_ENABLED=true` 使用 `native`、`false` 关闭。文档标准化、Provider 构造、工具目录及
每轮运行快照使用同一已选模式，TOML 后端、主连接搜索模式与独立搜索连接保留原选择。
每个主连接的 `search_mode` 可选 `external`、`bridge`、`native` 或在协议允许时选 `both`：

- `external` 使用本地搜索后端；主 Profile 的 `search_connection` 优先选择该独立模型，
  未指定时使用顶层后端及连接，可以是独立原生搜索模型，也可以是 Tavily。
- `bridge` 强制使用指定的独立原生搜索连接，主协议可为 Chat、Gemini、Anthropic 或 Responses。
  主 Profile 的 `search_connection` 优先于顶层选择；两处都未指定时，仅自身支持原生搜索的
  Profile 可沿用自身，普通 Chat 主连接必须明确选择搜索连接。
- `native` 将搜索内联到当前模型请求，`both` 在协议允许时保留内联搜索和本地搜索函数。
  `both` 的本地搜索也优先使用主 Profile 的 `search_connection`，`native` 不使用该本地连接。
  内联须声明 `native_web_search` 能力。Claude 原生工具与本地 `web_search` 同名，不能同时声明。

独立搜索连接支持 Gemini GenerateContent、Anthropic Messages 与 Responses，不要求主模型
切换协议。选择该连接即是明确的搜索配置，不要求为了独立搜索向主请求增加原生工具。
Responses 既能内联搜索，也能作为独立搜索连接；Chat 主 Agent 继续使用完整函数合同，
不把 Chat 搜索专用模型或 `web_search_options` 当作通用函数与搜索混用接口。
安装向导、Admin API / CLI 与配置加载使用共享后端逻辑补齐缺省搜索项。官方 DeepSeek
Chat / Responses 保留主协议，生成共用原密钥引用的独立 Messages 搜索 Profile；无需 Claude
账户。Gemini 默认 `bridge` 并沿自身连接独立搜索，Anthropic Messages 默认 `native`。
显式搜索项不被这些默认覆盖。DeepSeek 的 Responses 内联原生搜索仍未验证，不自动开放。
默认独立搜索连接绑定到对应主 Profile，聊天和插件路由使用不同模型时各用自己的搜索模型与凭据。
旧 `deepseek_anthropic` 部署由升级 reader 保留原地址、模型与密钥来源。
自定义网关保留实际 endpoint 和凭据，不按供应商名称将 key 发往另一服务；不确定的搜索
路径由用户显式配置。普通 Chat 没有模型搜索默认映射时，搜索不可用，不要求 Tavily key
才能启动 Bot；明确配置的无效搜索连接仍返回真实配置错误。

真实搜索来源只来自上游结构化证据：Gemini 的
`groundingMetadata.groundingChunks[].web`，Anthropic 中与 `server_tool_use` 关联的
`web_search_tool_result`，以及 Responses 的 `web_search_call.action.sources` 和引用 annotations。
正文 URL 或“已搜索”的陈述不生成来源。没有真实来源或搜索请求失败时直接返回错误，
**独立模型搜索绝不退到 Tavily 搜索**；显式选择 Tavily 后端仍正常执行 Tavily 搜索。
`provider_summary` 作为上游模型总结，不冒充网页原文或逐来源摘录。

搜索和网页读取分开。搜索只返回供应商已有的真实来源、内容和总结，不自动追加 HTTP 抓页或
Tavily Extract，也不要求供应商返回 raw_content。`read_webpage` 明确读取选定的公开 URL，
可读取历史链接，无须先在本轮重新搜索；
模型搜索及无搜索后端时的网页读取复用经过公共地址、DNS 与重定向检查的 HTTP 下载器，
不按 Tavily key 自动选择提取后端。显式 Tavily 后端保留其搜索与提取，返回正文保持原来源，
不因提取失败改变搜索供应商或推翻原搜索结果。插件 `WEB_SEARCH` 与 `WEB_READ` 继续分别授权。
本机网页下载使用标准代理发现，按每跳原网站匹配 `NO_PROXY`，保持校验后的固定公网 IP、
原 Host 与目标 TLS 名称；HTTPS 代理的 TLS 独立校验代理域名。网页沿已有 Web 超时，模型
仍沿 Profile 超时，公共页面缓存不因代理切换而失效。启动配置见[代理说明](../operations/http-proxy.zh-CN.md)。
提取失败明确报错，
不将搜索总结充当网页正文。来源、上游已返回内容和总结保留原件，再由既有结果预算器
整理展示或保存 artifact，不为保有来源额外抓页。

搜索请求沿搜索 Profile 的超时、输出预算与显式限额，沿原 Work 计入辅助请求预算；
超时或未知付费结果不盲目重试。Tavily/网页请求的 `WEB_TIMEOUT_SECONDS` 默认 180 秒，
显式旧值继续生效。模型配置热保存同时切换独立搜索连接，已开始的 Runner 保持原固定连接
至该轮结束。配置示例、协议边界和升级方法见[独立原生搜索](../deepseek-search-bridge.md)。

## Chat 供应商预设

`provider` 用于选择参数方言，不通过模型名称猜测，不在上游 400 后自动删参数重试。

管理模型快照中的 `provider_presets` 与 `yuki setup provider-presets --json` 共用
`model_runtime/provider_presets.py` 的只读预设；初次部署向导也从这里读取供应商与默认地址。
每项包含显示名称、协议、默认地址、密钥环境变量建议、官方文档与模型清单支持情况。
同一供应商可以提供多个协议或地址，`provider` 不是预设的唯一标识。
这些字段不写入模型 TOML，已有连接继续保留实际地址和显式协议。
自定义与 Azure 资源地址留空，由用户填写；本机 Ollama、LM Studio 使用各自默认端口。

`embedding_provider` 与 `embedding_defaults` 给出已经接入的向量协议及其地址，
`supports_embedding` 由是否有该配置派生。Qwen 聊天兼容地址为
`https://dashscope.aliyuncs.com/compatible-mode/v1`，原生向量地址为
`https://dashscope.aliyuncs.com/api/v1`，两者不互换。预设不证明任意模型支持向量或其他能力。
快照的 `task_requirements` 直接沿用任务路由的能力要求，另列向量、独立图片理解、语音和搜索用途；
模型 ID 仍由用户或上游清单提供，能力由实际配置声明。

供应商名称和接口协议分别配置。AGM、OpenRouter 等中转连接及自定义名称使用显式
`protocol` 的原适配器；没有 AGM 专用执行器或按品牌自动换协议的分支。修改连接名称时
保留原 endpoint、model、密钥引用、协议和参数，预设只用于明确创建新连接。

| provider | 思考参数与差异 |
| --- | --- |
| openai、azure_openai | reasoning_effort、max_completion_tokens；不回传非标准 reasoning_content |
| deepseek | thinking.type=enabled + reasoning_effort；不发送 tool_choice |
| qwen | enable_thinking + thinking_budget |
| moonshot、zhipu | thinking.type=enabled；不同型号的 effort 支持须显式配置 |
| doubao | thinking.type=enabled + reasoning_effort；保留 encrypted_content |
| minimax | 声明思考专用模型；reasoning_split=true，完整保留 reasoning_details |
| openrouter | reasoning.effort、reasoning.exclude=false，保留 reasoning_details |
| groq | reasoning_effort、max_completion_tokens、include_reasoning=true；默认针对 GPT-OSS 方言 |
| mistral | 显式 reasoning_effort，保留 thinking 内容块；须选支持该字段的模型 |
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
```

可用 reasoning 方言：`effort`、`thinking`、`enable_thinking`、`openrouter`、`builtin`；
Claude 使用 `effort`（adaptive）或 `budget`，Gemini 使用 `gemini`（thinkingLevel）或 `budget`。
Responses 不使用此配置表；Claude 接受 reasoning/budget，Gemini 另接受 send_temperature。
Gemini 原生 Schema 直接使用 responseJsonSchema，不再投影另一套 responseSchema。
思考开关和强度沿显式请求或 Profile；省略时使用 Profile 默认，不强制开启、不设置 low 下限。
显式 false 省略启用参数，不再被后台改成 true；供应商未收到参数后的默认行为由该 Provider 决定，不保证所有协议在物理上关闭思考。
Profile 的默认 effort 为 medium；全局 LLM_REASONING_EFFORT 只作显式覆盖，未配置时保留 Profile 的选择。显式空值不补档位，显式预算不依赖 effort。
供应商名称选择现有参数预设，明确的 protocol 选择适配器；自定义名称沿同一协议并保留其续跑归属。
`thinking` 可用 `send_reasoning_effort=true` 表明该型号同时支持 effort。
`builtin` 只适用于已经验证、始终思考且不接受思考控制参数的模型，不能用来接入无思考模型。
没有 effort 控制的 `thinking` / `builtin` 方言拒绝高于 low 的请求，不静默降低要求。
显式 effort 原样发送，不按供应商整族抬档；实际型号不支持时如实返回上游错误。
Groq 的其他型号可显式设置 `reasoning_format="parsed"`；此时不发送 include_reasoning，
两种字段不混用，不按模型名猜测方言。

预算接口直接使用 `thinking_budget_tokens` 的显式值，不从 effort 推算倍数。
Claude 手动思考预算必须小于输出预算；实际协议参数、认证和续跑归属继续核验。

旧型号 Claude 可配置 `reasoning="budget"`；Gemini 2.5 可配置同一方言。
Gemini 3 各型号支持的 thinkingLevel 可能不同，不支持的档位由服务明确拒绝。
Claude 思考开启时不支持强制调用特定工具，适配为 auto；结构化任务仍由现有 Runner
严格检查恰好一个 emit_result 及 schema，不增加隐藏模型调用。

Gemini 组装一次请求时转换一次基础历史，并在该 payload 内按原 call ID 索引工具调用，
将工具回执接回原生名称、ID 和签名。索引不跨请求保存，不重新解释已付 continuation，
不改变 opaque parts、回执顺序或模型实际收到的历史字节。

`headers` 支持 OpenRouter 的站点标识、Anthropic beta 等非鉴权 Header；不能覆盖认证字段，
不能放换行或把密钥写入 TOML。`api_key_env` 是密钥引用；可读取进程环境变量，或由管理 API
将 operator 输入的 API Key 保存在模型文件同目录的私有 `model_profiles.secrets.json` 中。
显式 `api_key_env = ""` 表示该连接无需鉴权；Pool 允许调用，HTTP 不添加空 Bearer／原生 API Key Header。有声明的密钥环境变量仍必须提供真实值，缺失时在请求前报错。不按本机地址、Provider 名称或模型内容推断鉴权方式，也不填写伪造 Key。
管理查询不会回传密钥；客户端不跨供应商或密钥来源共享。
新增供应商示例见 [多供应商配置](../../config/model_profiles.providers.example.toml)。
Gemini 3.8 Flash 的官方模型 ID 是 `gemini-3.8-flash`。Google Gemini 连接示例使用
`https://generativelanguage.googleapis.com/v1beta` 与原生 GenerateContent，采用该 ID、
`medium` 思考强度及文字、图片、工具、结构化输出能力。Gemini 3.8 的 Yuki 连接使用
所选思考参数；不按型号名称前缀提前禁止显式预算。代理转发仍需另行核对，不能把
代理改写误认为 Yuki 请求。适配器保留工具回合的 thought signature，
按上游 `cachedContentTokenCount` 统计缓存。Google 搜索桥须在此连接明确选择；旧连接保留
原搜索选择，未配置可用搜索后端时明确不可用。此连接不实现 Interactions API 或 Live/TTS；这些能力不能因为模型
本身支持就标成已接入。
TOML 中的显式 protocol 选择协议。额外命名的 endpoint/model/key 环境变量需要
存在于进程环境；Docker Compose 的 env_file 会加载 `.env`。本地 CLI 的标准
LLM/LLM_FLASH 设置可作为 TOML 引用值，其他变量须先导出，不扫描额外密钥文件。

OpenAI／兼容 Responses 的函数工具显式发送 `strict: false`，保留固定工具合同的
可选参数与原 `required`。省略此字段会让上游尝试严格化 schema，把可选的 `device`
等字段变成必填；模型因此填入的空字符串会改变真实目标选择。工具执行仍沿现有
schema 校验与授权，不将空设备号改路由到沙箱；原生工具不添加函数专属字段。
DeepSeek Responses 与其他协议保留各自的声明方式。

## 私有状态与恢复

公共和私有 compact 都使用当前 Main Profile 的超时、输出与思考设置，沿原 system、
工具、会话元数据和协议派发。没有独立 conversation_compaction 路由或摘要输出预算；
旧 TOML 路由跳过并警告一次。自动 compact 由统一的 `context.compact_trigger_tokens`
提前触发，默认 `96000`（`CONTEXT_COMPACT_TRIGGER_TOKENS`）；触发值不充当容量上限或摘要目标。
手动命令仅处理公共历史。硬容量由 Profile 的 `max_input_tokens`、`context_window_tokens`
和本次输出额度计算，未声明项保持未知，不从模型名称猜容量，也不补隐含默认窗口。
执行器沿已知容量核验，不静默减量或换模型；仍可容纳的请求不因软触发未产生有效检查点而失败。

- 每次响应的工具 ID、签名思考和原生块按原顺序保存在私有 ProviderContinuation；
  工具回执和用户改向按到达顺序追加，不重建已有调用，不重发已确认效果。
- Gemini 无原生 call ID 时，仅在接纳响应时生成确定性内部调用 ID；原始 thoughtSignature
  原样保存，内部映射不发给上游。它不是平台 message_id。
- WorkJournal 保存完整检查点；进程重启保持执行身份、请求链、预算、调用 ID 和实际 HTTP 请求。
  协议或实际 Profile 修订变化必须显式开新链，不能把旧签名状态拼入新模型。
  原生协议的品牌改名不改写已存 provider、签名或 opaque 原件，也不阻断同一合同的续接。
- 各协议复用同一 Conversation 的公共原事件投影；说话人、入口、Profile 和工具不另建历史视图。
  本轮资料只在当前激活准备，
  不冻结首份、差量或自动 snapshot，也不进入公共历史与摘要。工具、签名、opaque 和
  continuation 属于原执行，不扩展为公共历史；已有私有检查点沿原合同保留。
  下一普通轮复用公共原消息，context_note 沿原 Work 私有资料入口读取。
  实际私有协议合同或来源变化按原原因建立新链，不改写旧聊天事件，
  不跨 Work 搬运原生块。
- Chat `length`、Claude `max_tokens`、Gemini `MAX_TOKENS` 均转为 INCOMPLETE；
  Runner 的既有截断处理不会执行其中的工具。不自动增加预算。
- 协议正常结束但正文为空时，适配器保留实际响应、usage 和私有 continuation，交给调用者处理，
  不因没有可见文字判为错误。Main 的合法无工具 final（包括空正文）沿共同结束路径，
  不要求先发送，也不因一次 send 成功就截断后续工作；内部结束不证明消息已经交付。
  Compact 仍要求有效摘要，空结果不替换原上下文。畸形工具调用和非 pause 的不完整响应
  按真实错误收尾，不追加强制纠正轮或重置预算。
  截断调用仍保存未执行回执。Claude pause 使用原 checkpoint 在主循环继续，适配器不购买额外请求；
  直接结构化任务收到 INCOMPLETE 时返回真实失败，不能假装有主循环恢复。
- 文本中的 DSML 不再转为工具调用或生成协议记录；工具执行只接受实际协议 tool_calls/function_call。
- reasoning、签名、完整工具回执不进入对外消息或普通运行日志；只有显式 send 交付。
- [执行诊断](execution-trace.md) 单独保存实际返回的可读思考和工具结果；正文权限查询，按期清理。
  不透明签名/加密状态只留摘要，原恢复 journal 继续按协议私有合同保存。
- 请求原生服务端工具时，传输结果不明不自动重试；普通有界传输重试仍计入 Work 请求预算。
- 共享执行器将真实 HTTP 派发及未知用量计数传到异常诊断。Runner 对已实际派发的原生
  超时/不可用结果停止自动重排；尚未派发的拒绝、认证失败和本地工具请求保持各自分类，
  不从工具声明或错误文字猜测效果已经发生。
- 原生事件没有正文/本地调用，或声明原生工具却未取得可确认结果，不作为无效果的空响应
  自动重发。保留实际用量和可取得的私有检查点；仅现有合法 `pause_turn` 按原协议续接，
  其他情况明确收束。Provider 声明 completed 不独立证明搜索来源或目标完成；检查点保存
  失败也不因此获得重新派发上游请求的资格。
  没有原生事件的合法空 final 沿 Main 的共同结束路径，保留付费检查点和原生状态；
  原生工具声明本身不代表已执行搜索。发送回执、截断与阻断仍保留各自事实，不能由
  空正文推断交付或原生搜索成功。
- Responses 同一响应的本地 `call_id` 重复时不派发歧义工具，保留原响应、原生状态和计量，
  非重试错误停止原链。该规则不把原生工具伪装为本地调用，也不改写原签名或 opaque。
- 错误回执依据 Host 结构化事实投影：Claude 使用 `tool_result.is_error`，Gemini 使用
  `functionResponse.response.error`，其余协议保留原结果字符串。已知进程失败可表达为错误，
  原 `ok`、进程状态、部分修改与未知事实完整保留；不按外部正文关键词猜错误，不据错误位
  授权重发。Gemini 继续使用 GenerateContent，不套用 Responses 专属字段。

## 传输超时与断连

Profile 的 `timeout_seconds` 限制单次物理请求等待回复、写请求体和等连接池名额；可选的
`connect_timeout_seconds` 只限制建立连接：TCP 连接、TLS 握手，以及经 HTTP 代理时 CONNECT
成功后隧道内的 TLS 握手。未设置时取 10 秒（`DEFAULT_CONNECT_TIMEOUT_SECONDS`），任何取值
都不超过 `timeout_seconds`。四种协议共用同一传输层，连接池按端点共享，超时随每个请求携带。
连接阶段失败（连接失败、连接超时、代理以非 2xx 拒绝 CONNECT 的 `ProxyError`）时请求还没到达
上游，按传输重试规则重发（`max_retries`，退避上限 2 秒，第 2 次起计入 Work 辅助请求预算），最坏约等 `connect_timeout_seconds ×
(max_retries + 1)` 加退避。该字段不进入 Profile revision，调整它不会让持久 Work 换链。
httpcore 读代理 CONNECT 回复时用的是读超时：代理接受 TCP 后一直不回 CONNECT 的情况仍按
`timeout_seconds` 等待。

已发出请求后连接中途断开（`RemoteProtocolError`、`ReadError` 等）时，上游可能已受理并计费，
结果未知。httpcore 在请求体写失败时会继续读回复，断开同样表现为 `RemoteProtocolError`，
所以异常类型不能证明请求体没写完，不另分发送阶段。适配器把它们统一包装为
`LLMUnavailableError`（原异常留在 `__cause__`），不新增重试：Responses 只重发连接失败、代理拒绝和超时，
Chat Completions、Claude Messages、Gemini 沿用原有的全部传输错误有界重试。Runner 走现有传输
失败路径：带原生服务端工具且已有物理派发时改为 `LLMNativeToolError`，journal 停在
`dispatched`，不自动重放；普通请求与读超时相同，journal 存 `paired`，Work 进入可重试的
provider 失败，沿原 Work 与累计预算继续。

## 显式备用链

`model_profiles.toml` 的可选 `[fallbacks]` 按用途列出有序备用 Profile，例如
`chat_agent = ["backup"]`；缺省或空列表就是没有备用，行为与没有该段时相同，无运行开关。
链只挂在 `ModelTask` 路由上；独立视觉、ASR、Embedding 与搜索连接没有备用链。

切换只在执行器（`TaskModelExecutor`）里发生，适配器只多记一个“未受理”计数：

- 只在请求**确定未被上游受理**时切到下一跳：本跳每一次物理尝试都是连接失败、连接超时、
  代理拒绝 CONNECT、本地连接池等待超时，或上游明确返回 429、5xx（504 除外）、401/403
  且回复未报告非零用量；以及 0 次物理请求的密钥缺失。每跳先按自身 `max_retries` 做有界传输重试，耗尽后才切换。
- 504 网关超时、读超时、读写中断、回复中途断开等结果未知的失败不切换（504 仍按现有传输
  重试规则在本跳内重发），沿上文的传输失败与回执路径；
  400/404 等请求形状问题、空回答、截断、畸形工具调用、内容拒绝与容量超限也不切换。
  取消、被新 Turn 取代和后台让路不是 Provider 故障。于是一次逻辑请求最多只有一跳可能计费。
- 只有不带 ProviderContinuation（`continuation`、消息或 `continuation_items` 里的私有项）的请求走链（新链首请求、结构化任务的每次调用、公共 compact）。
  带续接状态的请求只发给产生它的那个 Profile（必须仍在链内），失败照常失败，不把私有状态发往
  别处；链内跟随由续接状态决定，没有计时、冷却或健康探测。每条新链都先试链首。
- 链首（或续接所属 Profile）不满足请求能力或容量时照旧报错，不因此改选别的 Profile；
  只有切换途中的备用跳在缺能力（含原生搜索）或已知容量放不下时记为跳过。
- 密钥缺失只在该用途配了链时记一行 0 次物理请求的失败调用；未配链时照旧直接报错、不记录。
- 每跳是一次独立逻辑调用，单独写 `model_invocations` 行；第二跳起若之前已有物理派发，
  按辅助请求计入原 Work 根预算（与传输重试同一入口），预算耗尽即停止。Main 的派发守卫与
  journal `dispatched` 只记一次，冷恢复沿原回执，不因配了链而重新派发。
- 全部跳失败时抛出最后一个真正派发过（物理请求数大于 0）的跳的原异常，诊断附 `fallback_hops`，
  `physical_request_count` 为各跳之和。日志记一行 `model_fallback task= from= to= reason=`。

请求按链首生成（结构化模式、原生搜索、工具），所以读配置时要求每个备用 Profile 满足该用途的
默认能力和链首结构化模式所需能力（`function_tool` 需 `tools`，`json_schema` 需
`structured_output`）；备用不得重复或等于链首，用途名必须已知。不要求原生搜索：链首是原生搜索
时也可以配置没有 `native_web_search` 的备用，带原生搜索工具的请求在运行时跳过它（Responses
链首默认即原生搜索，所以这类备用多半只接住不带原生搜索的请求）。不比较上下文窗口：备用窗口较小只在单次请求放不下时跳过；Main 的
容量规划仍按链首，备用的上下文窗口建议不小于自动整理触发值加输出额度。

Profile revision 覆盖整条链：改链或改备用 Profile 后，持久 Work 下次激活显式开新链；链为空时
revision 与没有备用链时相同。Admin 快照回传 `fallbacks`，Desktop 原样保存不会丢链；提交的文档不含该字段（旧客户端）
时保留已存链，显式空映射才清除；热保存后
新请求立即用新链，已 pin 的激活保持原链。`yuki model routes` 显示 `fallback_profile_ids`。
空链在保存时不写 `[fallbacks]` 段；本功能之前的代码会拒绝含该段的文件，回滚代码前须先删掉该段。
看门狗对配了链的用途读最近一次成功调用：落在备用上时报告 `component:model_fallback:<task>:<profile>`，
链首再次成功即清除，不设时间窗。

## 用量与 HTTP 请求口径

适配器先准备完整 httpx Request，包括路径、Header 和 JSON 编码，再沿原派发核验与预算登记进入 send。准备失败不登记实际请求，也不调用上一条请求的失败收尾；登记后 HTTP 段的异常仍按原请求收尾。

同一次逻辑调用的协议 payload、编码字节与 wire fingerprint 只准备一次，原传输重试复用它们；
每个物理 attempt 仍分别核当前来源、登记预算和 usage，保留各自 trace 与未知事实。
Provider 的 `complete` 实现在真实派发边界调用共同 dispatch guard，非 HTTP/Fake 实现也遵循此合同；
Executor 负责原名额和逻辑优先级，不再重复核同一个物理派发。
成功 body 解码一次，以该 attempt 独有的只读 JSON 树交给 usage、finish hook 和协议 parser。
保留原 response 字节与既有 opaque 隔离复制；parser 不修改共享树，解析失败仍保存实际已报告用量。

Main 容量准备通过 `PreparedModelInput` 显式交给 Executor。只有同一归一化请求对象与原 Profile
才能复用该完整估值；请求或 Profile 改变则按实际输入重新准备，其他 Executor 入口继续核已知容量。
准备对象归本次调用所有，嵌套声明和 opaque 不在交接后原地修改；不使用全局缓存或 hash 代替输入。

`model_invocations` 一行表示一次逻辑模型调用，`calls` 继续按该行计数。`physical_request_count`
只统计实际进入 HTTP 客户端的请求尝试，包括传输重试和 Claude 原生搜索暂停后的续发；
路由、配置或本地校验失败不计入。`unknown_usage_request_count` 统计其中未获得上游总 Token
报告的尝试，不能按零 Token 或零费用处理。每次物理响应（包括兼容错误包）已明确报告的 usage 由 transport 记录，executor 是逻辑
归并的唯一边界；同一次解析重复上报不重复计数，Claude pause 每次物理请求分别记账。
输入/缓存字段只有全部物理尝试均报告该字段时才有完整总量，任一缺项保持 NULL，避免用部分
输入与完整缓存构造错误覆盖率。输出/总 Token 保留已报告的小计，并同时记录未知物理请求
数，不把小计称为完整账单。显式零仍为零，缺失仍为未知；这些合成兼容错误形状不
证明官方错误响应格式或实际账单。

历史调用的两个字段为 NULL，无法从原逻辑记录
反推出真实 HTTP 次数。缓存率只使用上游明确报告缓存量的输入作分母，并同时保留未报告计数。

同一 Main 链里的请求另记 `request_kind` 与 `request_chain_id`（0161）。`request_kind` 为
`main`、`execution_compact` 或 `public_compact`，由发起处按实际规划传给 executor，不进入
`ChatRequest`、请求正文或请求头，也不改变 compact 回执与输入摘要；`request_chain_id` 是该次
请求已有的 `ChatRequest.request_chain_id`。compact 请求带被压缩的旧链，其后的 Main 带新链；
Work 的 `runtime_work_journal.chain_id` 与进度里的 `chain_links` 记录同一换链，可据此把调用
关联到 Work。记忆、SELF、Dream、搜索桥等其他任务两列为 NULL，由 `task` 区分；0161 之前的
行同样为 NULL，表示未知，不回填。写入仍是原诊断 writer 的单行条件 INSERT，不读历史；
`model_phases` 诊断带同样两项。管理用量的四维聚合不变。

管理用量按记录发生时的 task/provider/model/profile_id 四维，从一次只读快照聚合。
今日按调用方明确的 IANA 时区，近 24h/7d 为滚动窗口；全部留存不等于完整生命周期。
同一实际模型的不同 Profile 可合并，曾用同一 Profile 的不同模型不能合并；不查当前配置
重写来源，退休任务与未知标签仍贡献总量。筛选后的摘要、组与占比使用同一结果集。
逐字段 coverage 区分缺报告与显式零，缓存率只取输入和缓存均已报告的同样本。
HTTP 用量的 unknown_usage 还计入已有 unknown_usage_request_count>0 的逻辑调用，
不以已报告总量小计掩盖其他尝试未知；原零/未知输入不会生成无定义百分比。
参数、响应与修订 9 的协调发布合同见[管理 API](admin-api.md#用量读取)。

原生搜索是否启用以当次请求合同记录；Google/Claude 的原生搜索可能另有工具费用，当前账本
没有供应商账单或可靠的原生工具价格，费用显示为未知，不从 Token 用量推算为零。
独立原生搜索另记一条 `model_invocations.task=web_search`，归入搜索连接与模型，
记录上游返回的输入、输出、缓存 Token 和实际 HTTP 请求数；无可信 grounding 记失败，
失败不会转为 Tavily 搜索；没有上游用量时保留未知。

缓存查证使用实际物理 HTTP 请求及上游返回的 usage，缺字段保留未知。
删除旧应用层 cache-shape 代理哈希与常态 WireRequestObserver 全量哈希/链路缓存；
需要比较前缀时在获准查询的原请求上离线比较，不为每次业务调用重复序列化历史。
相同 messages 的不同原生签名仍可能改变最终输入，代理上游最后一跳不在本机观测内。
主 Agent 从真实 Conversation、generation 与当前 Work 分支传递 `x-session-id` 请求元数据；
编号不写进模型正文，不使用每次激活的 chain UUID 或 prompt hash。每个请求单独建 Header，
不修改共享连接。没有真实代次的独立调用不借其他会话编号。
用量记录保留上游实际返回的 cache creation 与 cached input 字段；未返回的字段在原记录中
保持为空，不从 TTL 子项推断缺项。`model stats` 使用原领域聚合，原记录用于核对未知覆盖。

支持显式缓存断点的 Responses 连接在 Profile 的 `capabilities` 中声明
`prompt_cache_breakpoints`（例如 OpenAI GPT-6.1 Terra）。未声明时保留原序列化，
适配器不按模型名称猜测支持情况，也不向 DeepSeek、Gemini 或 Claude 注入 OpenAI 参数。
CLI 与 Desktop 新安装向导仅在明确选择 `provider="openai"`、`protocol="responses"`、
官方 `https://api.openai.com/v1` 地址（可带尾斜杠），且型号属于官方文档支持的
GPT-5.6 及以后版本时，默认生成该 capability。其他型号、兼容／自定义地址和 Chat
不自动声明；Chat 适配器目前不传递此断点，不能靠添加 capability 开启。
这个默认值只在生成新 Profile 时使用；正常升级、加载和重配置不为已有 Profile 补写
capability，原先未声明的连接仍不发送显式点。旧安装需核对实际协议、端点和型号后
自行显式启用；已声明的连接在更换型号或端点时也需重新核对支持范围。支持范围与内容块格式见
[OpenAI 缓存文档](https://developers.openai.com/api/docs/guides/prompt-caching)。
OpenAI／兼容 Responses 适配器将初始静态说明放入可标记的 developer 内容块，
并标记 Main 已有公共历史的最后一条 user 消息；后续 assistant 原文、本轮资料、
当前触发和 Host 初始化仍在其后。Responses 的 output_text 不支持缓存断点，
不向 assistant 内容块注入该字段；只有 assistant 的公共历史仍使用静态 developer 断点。
继续使用默认 implicit 缓存，工具回合沿原协议尾部追加；显式点不改变消息正文、
固定工具或执行顺序。缓存提示随原私有消息持久化，公共 FrozenFragments 只保留原事件内容；
重新装配和 compact 沿实际公共边界产生新提示，不常驻旧运行资料、不新增缓存账本。

Responses 的 `input_tokens_details.cache_write_tokens` 直接记入
`cache_creation_input_tokens`，成功与失败回执共用该来源。30 分钟／12 小时子项不映射成
Claude 的 5 分钟／1 小时字段。短固定前缀可能达不到上游缓存起点，单次结构化任务的
不同 user 批次也未必复用；不为命中率填充文本或改变业务历史。命中率和写入量须分别读取
真实 usage，受控实验不能代替生产流量或账单。

## 验证边界与协议来源

定向测试覆盖参数、图片/schema、截断、私有签名、工具结果顺序、SQLite 重启后的 HTTP 字节一致性，
以及真实 Runner/隔离数据库/假网关中的可见输出边界。Gemini 独立桥已用现有连接凭据
在无 QQ/Work 的合成请求中得到上游可信 grounding 与用量；Bot 容器用现有连接地址的
搜索专用请求也返回可信来源。当前生产 Gemini 连接已配置 `search_mode="bridge"`；
这项配置与合成请求成功不能替代真实 QQ 工具回合验收。
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
