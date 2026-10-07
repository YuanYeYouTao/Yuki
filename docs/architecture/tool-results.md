# 结果预算与 Artifact

`ToolResultBudgeter` 对所有 Provider 生效。结果超过字符或条目预算时，完整结果写入
`data/tool_artifacts/`，模型只收到不可猜测的 `artifact_handle`、根类型、浅层结构和可用读取操作。
文件路径不会暴露给模型。关闭 Artifact 时仍返回明确的有界结果，不会把超长原文直接注入模型。

## 结构化 JSON

Core Tool `read_tool_artifact` 支持三个通用操作，不依赖任何外部服务的业务字段：

- `inspect`：查看对象键、数组长度和浅层类型；对象键使用稳定排序。
- `get`：按 `path` 精确读取对象、数组元素或分页后的数组区间。
- `search`：在键和标量值中搜索，返回包含命中的完整、有界对象及其准确路径。

`path` 是字符串和整数组成的数组，例如 `["data", "meals", 27]`，不执行 JSONPath、
JMESPath 或任意表达式。标准工具结果外层的 `ok/provider/tool/data` 信封会被保留，结构化读取的
逻辑根节点为其中的 `data`；返回路径仍从 `data` 开始，避免模型混淆。

结构读取受最大文件大小、路径深度、分页条数、扫描节点数和输出字符数约束。单个对象超过预算时
返回 `artifact_value_too_large` 和对象结构，不会从 JSON 中间截断。Artifact 本地读取不占业务工具
调用额度，但仍占模型请求，因此继续受 Agent 总循环和最终回复预算限制。

## 文本兼容

省略 `operation` 时保持旧的文本读取行为，继续支持 `offset`、`limit` 和 `query`。非 JSON Artifact
使用结构化操作时会返回 `artifact_not_json`，并明确提示改用文本模式。Handle 由数据库映射文件并在
保留期后清理。

## 存储与来源

共享实现位于 `tool_results/`，供 Core、Plugin 与 Work 使用。保留原工具句柄、`data/tool_artifacts` 路径、媒体授权、隐私删除和回收边界。工具调用的强制来源证据与可丢诊断分别结算；可丢诊断继续共用有界异步 writer。
