# DeepSeek Flash 搜索临时适配器

主 Agent 保持 Flash Responses。设置 `WEB_MODE=tavily`、
`WEB_SEARCH_BACKEND=deepseek_anthropic` 后，已有 `web_search` 工具单独请求
官方 `/anthropic/v1/messages` 的 Flash 原生搜索；主 Agent、子 Agent 的工具
名称、参数、顺序和历史序列均不改变。搜索请求只包含查询和检索限制，不传聊天历史。

适配器仅接受服务端 `web_search_tool_result` 中关联真实调用的来源，
不把模型自行生成的网址或回答当成搜索结果。网页正文沿用公共地址校验、DNS 固定
和重定向检查的下载器读取；读取失败可由原 Tavily 提取兜底。
所有搜索（包括新闻和日期范围要求）都先调用 DeepSeek，查询条件原样送入检索请求。
只有实际请求失败、返回无效响应或没有真实来源时才使用 Tavily，不按内容强制分流。
日期要求由检索模型处理；来源日期未经核实时不能宣称严格满足日期范围。
未配置 Tavily 时，无法完成的操作明确报错。没有新增 Tavily 调用额度限制。

检索结果缓存保存在 `WEB_SEARCH_BRIDGE_STATE_PATH`：10 分钟过期、最多
128 条、单条最多 32 KiB。只复用首选后端的成功结果；Tavily 兜底结果不缓存，
也不复用旧版本已缓存的 Tavily 结果，避免暂时失败变成持续绕过首选后端。
这只是减少重复检索，与 Provider 的前缀缓存分开。
网页文本是外部资料，继续走现有不可信工具结果边界和来源交付链路。
实际发出的辅助模型请求计入当前任务的根预算；失败请求也计入。

启动时要求主模型配置指向官方 DeepSeek，复用该配置的密钥。不会将其他 Provider
的密钥发送到 DeepSeek。默认仍为原 Tavily 后端；开启适配器需要重启 Bot。

待官方 Flash Responses 的原生搜索经过真实调用验证后，可切换原生联网配置，
移除 `deepseek_bridge.py` 和 WebModule 中的适配分支。仅回退这次补丁时，
将 `WEB_SEARCH_BACKEND` 改回 `tavily` 即可，保留主 Agent 的现有外部工具合同。
缓存文件可在 Bot 停止后删除，不涉及聊天数据库、任务回执或工作区文件。
