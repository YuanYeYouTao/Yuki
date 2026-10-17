# 独立原生搜索

主对话与搜索分别选择连接。例如，主 Agent 通过 AGM 的 Chat Completions 执行完整函数合同，
其 `web_search` 单独请求同一网关的 Gemini Google Search；也可使用显式 Anthropic Messages
或 Responses 搜索连接。搜索只收到查询及检索条件，不携带主对话历史、私有协议或函数清单。
本文件保留原有路径，旧 DeepSeek 搜索文档链接继续有效。

## 选择连接

安装向导、Admin API 与 `admin model apply` 使用同一套模型搜索配置生成和校验逻辑。
未配置的搜索项按主连接补齐；显式关闭、搜索模式、外部后端和手选连接保留原选择。
默认生成沿用用户模型、密钥引用与超时，在配置加载或保存时完成，不在搜索失败后重猜路由。
全局 `WEB_MODE=native` 表示启用模型搜索，连接的 `search_mode="native"` 则专指内联原生工具。

| 主连接 | 模型搜索默认配置 |
| --- | --- |
| 官方 DeepSeek Chat Completions / Responses | 保留主协议，生成 DeepSeek Anthropic Messages 独立搜索连接，共用原密钥引用，无需 Claude 账户 |
| Gemini GenerateContent | `search_mode="bridge"`，通过自身连接发起独立 Google Search 请求 |
| Anthropic Messages | `search_mode="native"`，声明该连接的原生搜索工具 |

自定义网关按实际协议、地址、模型和密钥来源配置。不会根据 Provider 名称将密钥发送到官方服务；
无法确定的搜索地址应明确配置独立搜索连接。普通 Chat 连接没有可用的模型搜索时，搜索如实不可用，
不因缺少无关的 Tavily 密钥阻止 Bot 启动。声明能力及本地配置验证不证明端点真实可用。

可用 `admin model show` 读取现行配置与 revision，修改完整 TOML 后执行
`admin model apply ./models.toml --revision <revision>`。例如，独立 Gemini 搜索连接：

```toml
schema_version = 3
search_backend = "model"
search_connection = "google_search"

[profiles.google_search]
provider = "agm"
protocol = "gemini"
base_url_env = "GEMINI_BASE_URL"
api_key_env = "AGM_API_KEY"
model_env = "GEMINI_MODEL"
timeout_seconds = 180.0
max_retries = 0
default_temperature = 0.1
default_max_output_tokens = 8192
thinking_mode = "enabled"
reasoning_effort = "medium"
```

这是新增搜索连接的片段，保留原有任务 routes 和主 Profile。主 Chat 可设置
`search_mode="bridge"` 明确使用该搜索连接，或通过 `external` 使用顶层选定的后端。
主 Profile 内的 `search_connection="连接ID"` 为本地搜索单独选择模型连接，适用于
`bridge`、`external` 和 `both`，优先于顶层后端及连接；`native` 继续使用内联搜索。
`bridge` 两处都未指定连接时，仅支持原生搜索的连接可直接沿用自身。默认生成的独立搜索连接
绑定在对应主 Profile，聊天与插件使用不同模型时分别保留各自的搜索模型及凭据。
`search_backend="tavily"` 明确选择 Tavily 搜索；缺省后端为 `model`，不会从旧默认值或
Tavily 密钥是否存在推断外部搜索。全局 `WEB_MODE=disabled` 仍禁用联网能力；
未设置开关或旧 `WEB_ENABLED=true` 使用 `native`，旧 `WEB_ENABLED=false` 继续关闭。
显式 `WEB_MODE=tavily` 或 `search_backend="tavily"` 保留 Tavily 选择。

网关可明确创建 Gemini 搜索连接，沿该网关的 API key 引用；`/v1beta` 等路径需按网关实际配置核对。
修改供应商名称不会重设已有协议、地址或连接。

独立搜索支持 Gemini GenerateContent、Anthropic Messages 和 Responses。Responses 也可
在主对话中内联原生搜索，两种用途分开配置。普通 Chat 的标准函数调用本身不提供搜索，
主 Agent 不因使用某种兼容协议就获得服务端搜索能力。DeepSeek Responses 原生搜索仍未
经过可用性验证，不自动启用；DeepSeek 搜索由配置生成的或用户手选的 Anthropic 搜索连接执行。

## 来源、读取与失败

- Gemini 只认 `groundingMetadata.groundingChunks[].web` 的真实 URL/标题。
- Anthropic 只认与原 `server_tool_use` 配对的 `web_search_tool_result` 及结构化引用。
- Responses 识别真实 `web_search_call.action.sources` 和引用 annotations。

模型正文的链接、总结或“已经搜索”不证明来源。没有真实来源或请求失败时直接报错，
独立模型搜索不会退到 Tavily。显式选择 Tavily 后端才由 Tavily 搜索。

`read_webpage` 是单独的页面提取；只读取用户明确发送或本轮搜索真实返回的 URL。
模型搜索和未配置搜索后端时的网页读取复用公共地址校验、DNS 固定和重定向检查的 HTTP
下载器，提取 HTML 或纯文本，不因存在 Tavily 密钥而切换提取后端。
该下载器默认使用标准系统/环境代理，支持 HTTP、HTTPS 与 SOCKS5 代理；网页下载沿
`WEB_TIMEOUT_SECONDS`，不借用搜索模型的超时。原网站域名用于 `NO_PROXY` 和 TLS 校验，
网络连接仍固定在已校验的公网 IP。原生启动、Docker 与 Mihomo 示例见[代理配置](operations/http-proxy.zh-CN.md)。
显式 Tavily 后端保留其搜索与页面提取；返回内容保持原来源。
提取失败时明确返回错误，
不把 `provider_summary` 冒充网页原文；总结始终作为上游模型生成的外部不可信资料展示。

## 预算、缓存与升级

搜索使用所选 Profile 的超时、输出预算与显式限额，实际付费调用计入原 Work 的辅助请求
预算。保留真实 request ID、来源和 token/cache 用量；上游未报告的部分仍为未知。
超时与未知结果不授予重新付费请求的资格，也不重做已确认效果。

检索结果缓存位于 `WEB_SEARCH_BRIDGE_STATE_PATH`，与模型前缀缓存分开，按实际搜索连接、
模型、参数及凭据隔离。缓存不代替执行处的来源、权限或预算核验。配置热保存只影响新
Runner，运行中的模型和搜索连接保持原固定版本，不在同一轮中切换。

旧 `WEB_SEARCH_BACKEND=deepseek_anthropic` 与原 `search_connection` 由升级 reader 保持
原有官方 Anthropic 地址、`deepseek-flash` 和密钥来源，不改写主对话 Profile 的协议。
新配置使用独立的 Anthropic 搜索 Profile，模型、地址都来自显式配置，不在桥中写死。
旧 `WEB_TIMEOUT_SECONDS` 显式值继续生效，不因升级自动覆盖。
退役的 `native_with_tavily_fallback` 不再接受；配置应明确选择 `native`、`tavily`、`both`
或 `disabled`。`both` 只声明两种可用工具，不表示失败后切换后端或重新付费请求。

保存成功只证明配置已加载，不证明上游模型的原生搜索、全文读取或缓存计费已验收。
真实端点能力、QQ 工具回合和费用分别核实。完整约束见[模型供应商说明](architecture/model-providers.md)。

协议参考：[DeepSeek Anthropic 兼容接口](https://api-docs.deepseek.com/guides/anthropic_api/)、
[Gemini Google Search](https://ai.google.dev/gemini-api/docs/google-search/)、
[Anthropic Web Search](https://platform.claude.com/docs/en/agents-and-tools/tool-use/web-search-tool)。
协议文档不证明某个自定义端点已通过真实搜索或计费验收。
