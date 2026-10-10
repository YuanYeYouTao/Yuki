# 结果预算与 Artifact

`ToolResultBudgeter` 对所有 Provider 生效。结果超过字符或条目预算时，完整结果写入
`data/tool_artifacts/`，模型只收到不可猜测的 `artifact_handle`、根类型、浅层结构和可用读取操作。
文件路径不会暴露给模型。关闭 Artifact 时仍返回明确的有界结果，不会把超长原文直接注入模型。

## 结构化 JSON

Core Tool `read_tool_artifact` 支持三个通用操作，不依赖任何外部服务的业务字段：

- `inspect`：查看对象键、数组长度和浅层类型；对象键使用稳定排序。
- `get`：按 `path` 精确读取对象、数组元素或分页后的数组区间；字符串按 Unicode 字符分页，
  返回 `value/offset/next_offset/total_characters/offset_unit=characters`。
- `search`：在键和标量值中搜索，返回包含命中的完整、有界对象及其准确路径。

`path` 是字符串和整数组成的数组，例如 `["data", "meals", 27]`，不执行 JSONPath、
JMESPath 或任意表达式。标准工具结果外层的 `ok/provider/tool/data` 信封会被保留，结构化读取的
逻辑根节点为其中的 `data`；返回路径仍从 `data` 开始，避免模型混淆。

Artifact 存储沿配置计量；结构读取不再叠加固定文件大小、路径深度或搜索词长度拒绝。搜索遍历已保存JSON；分页沿请求和当前结果预算，没有另设扫描深度、节点数或固定100项帽。单个对象超过预算时
返回 `artifact_value_too_large` 和对象结构，不会从 JSON 中间截断。Artifact 本地读取不占业务工具
调用额度，但仍占模型请求，因此继续受 Agent 总循环和最终回复预算限制。
读取 `limit` 默认 8000、工具声明上限 32000；返回长度受当前结果字符预算控制，持久回执不再叠加字节拒绝。非空字符串必须前进，预算不足明确报错，不能把空页当结束。
字符串页直接返回，不再生成套娃 Artifact。

## 工作区文件与脚本数据

`workspace_read` 的 `limit` 是每页字节数，范围 4–32768、默认 32768。
`offset/next_offset/size` 的单位是字节，UTF-8 尾部不完整字符留到下一页。
后续页传首读的 `expected_version`；文件变化返回 `version_conflict`，读取期间变化返回
`file_changed_during_read`。已发布 artifact 快照也接受该校验。

文件页明确返回 `read_state=inline/binary`、`text`、`eof` 和 `truncated`。
模型结果超过预算时 `read_state=externalized`、`text=null`，保留原文件大小、版本、
页游标和 artifact 引用。真正空文件是 `size=0/text=""/eof=true`；文件末尾的空页仍有原大小。
错误使用 `ok=false/error_code`，不伪装成成功空文本。

Code Mode 需要的数据页与给主模型的摘要分别限额。Host 只为 `workspace_read` 从该子调用
已接受回执绑定的 artifact 取回完整页，核验当前读取授权、generation、隐私代次、完整性和
256 KiB VM 返回上限；分段或中断恢复使用原页，不读取后来改动的工作文件。
丢失、损坏或无权读取时保留 `complete=false/text=null/result_ref`，不重跑原读取。
`complete` 表示当前回执页完整，整个文件结束以 `eof` 为准；模型仍只收到有界的程序结果。

## 文本兼容

省略 `operation` 时保持旧的文本读取行为，继续支持 `offset`、`limit` 和 `query`。非 JSON Artifact
使用结构化操作时会返回 `artifact_not_json`，并明确提示改用文本模式。Handle 由数据库映射文件并在
保留期后清理。

## 存储与来源

共享实现位于 `tool_results/`，供 Core、Plugin 与 Work 使用。保留原工具句柄、`data/tool_artifacts` 路径、媒体授权、隐私删除和回收边界。Work 产物沿原 parent 关系查询全部祖先的实际保留状态，祖先仍在执行时不因直属父已结束而过早回收；这只决定保留，不扩大读取授权。工具调用的强制来源证据与可丢诊断分别结算；可丢诊断继续共用有界异步 writer。
