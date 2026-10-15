# 独立原生搜索

主对话与搜索分别选择连接。例如，主 Agent 通过 AGM 的 Chat Completions 执行完整函数合同，
其 `web_search` 单独请求同一网关的 Gemini Google Search；也可使用显式 Anthropic Messages
或 Responses 搜索连接。搜索只收到查询及检索条件，不携带主对话历史、私有协议或函数清单。
本文件保留原有路径，旧 DeepSeek 搜索文档链接继续有效。

## 选择连接

在 WebUI“模型接入与任务用途”中配置搜索模型，核对供应商、协议、地址、模型及密钥来源。
“本地 web_search 后端”选择“独立原生搜索模型”，再选择搜索连接。对应 TOML：

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
`search_backend="tavily"` 明确选择 Tavily 搜索；未写该字段时沿用部署设置，
仅预选搜索连接不会突然接管搜索。全局 `WEB_MODE=disabled` 仍禁用联网能力。

AGM 创建预设默认 `protocol="gemini"`、`search_mode="bridge"`，协议仍可独立改成 Chat。
已有 AGM 连接可明确创建 Gemini 搜索连接，沿同一 API key 引用；标准 `/v1` 地址转换为
`/v1beta`，自定义路径需按网关实际配置核对。修改供应商名称不会重设已有协议或连接。

独立搜索支持 Gemini GenerateContent、Anthropic Messages 和 Responses。Responses 也可
在主对话中内联原生搜索，两种用途分开配置。普通 Chat 的标准函数调用本身不提供搜索，
主 Agent 不因使用某种兼容协议就获得服务端搜索能力。DeepSeek Responses 原生搜索仍未
经过可用性验证，不自动启用；DeepSeek 搜索使用明确的 Anthropic 搜索连接。

## 来源、读取与失败

- Gemini 只认 `groundingMetadata.groundingChunks[].web` 的真实 URL/标题。
- Anthropic 只认与原 `server_tool_use` 配对的 `web_search_tool_result` 及结构化引用。
- Responses 识别真实 `web_search_call.action.sources` 和引用 annotations。

模型正文的链接、总结或“已经搜索”不证明来源。没有真实来源或请求失败时直接报错，
独立模型搜索不会退到 Tavily。显式选择 Tavily 后端才由 Tavily 搜索。

`read_webpage` 是单独的页面提取；只读取用户明确发送或本轮搜索真实返回的 URL。
Tavily 可用于页面提取，不参与模型搜索失败降级；未配置 Tavily 时复用公共地址校验、
DNS 固定和重定向检查的 HTTP 下载器提取 HTML 或纯文本。提取失败时明确返回错误，
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

保存成功只证明配置已加载，不证明上游模型的原生搜索、全文读取或缓存计费已验收。
真实端点能力、QQ 工具回合和费用分别核实。完整约束见[模型供应商说明](architecture/model-providers.md)。
