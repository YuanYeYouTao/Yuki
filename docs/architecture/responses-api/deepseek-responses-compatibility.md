# DeepSeek Responses API 兼容性记录

本记录只保存脱敏后的契约结论。真实 API 探针必须显式启用，默认测试仅使用
`tests/fixtures/deepseek_responses/`，不得提交密钥、私人提示词、QQ 号或完整网页正文。

## 已确认的首期契约

- 端点为 `POST /responses`，首期使用 `stream=false`。
- Function Tool 使用扁平的 `type/name/description/parameters` 结构。
- 本地函数结果使用 `function_call_output`，并复用原 `call_id`。
- `reasoning.effort` 使用 Responses 通用枚举
  `none|minimal|low|medium|high|xhigh|max`；DeepSeek 将兼容档映射到自身实际档位。
  关闭思考时不发送 `reasoning`，避免已知的 `effort=none` 可见输出异常。
- 原生联网工具定义为 `{"type":"web_search"}`，服务端可能连续产生 `search` 和
  `open_page` action。
- `web_search_call` 是服务端已执行事件，不能转换为本地 Function Call。
- 一个响应可能含多个 message item；最终正文取最后一个非空 assistant message。
- `status=incomplete` 必须保留部分事件和 usage，但不能当成完整回答；只允许一次有界恢复。
- `status=failed` 转换为异常。
- annotations 可能为空；来源依次从 annotation、成功的 `open_page` URL 和最终正文 URL
  恢复。
- GitHub 并非整体不可访问：公开仓库主页或 raw URL 可能失败，而具体 blob 页面可能成功。
  因此单个 `open_page` 失败不能直接判定整个原生搜索失败。

## 已知限制

- DeepSeek Responses 当前按无状态方式接入，工具循环需要在当前 Agent turn 内回传必要
  output items；不能依赖跨请求的服务端会话状态。
- 首期不实现 SSE、MCP、`custom_tool_call`、独立 `web_open`、图片或文件输入。
- DeepSeek 对未支持字段可能忽略；权限和费用控制不能依赖被忽略的 Provider 参数。
- `deepseek-v4-pro` 的 Responses 支持情况没有在本记录中宣称为已验证。

## 夹具覆盖

- `text_completed.json`：文本、reasoning item 和 usage 映射。
- `function_calls.json`：同一响应中的多个本地 Function Call。
- `native_web_incomplete.json`：搜索、打开页面、部分失败、空 annotations、多个 message、
  incomplete 和高 Token 用量。
- `failed.json`：失败状态。

HTTP 400、401/403、429、5xx、超时，以及第二轮 `function_call_output` 的请求结构由本地
MockTransport 测试构造，避免把请求中的工具参数或提示词保存成仓库夹具。
