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

## 环境文件与旧脚本结果

普通文件内容由终端读取、搜索与过滤，八项通用工作区工具已退出新声明与执行。媒体仍用 `workspace_inspect` 交给原模型；文件用 `workspace_publish` 固化不可变版本，再沿统一 `send_message` 交付。普通终端文件不附加CAS、分页文本协议或经验文件配额。

旧 Code 调用的 workspace_read 外置页仍按已接受子调用的原 result_ref 取回，核原授权、generation、隐私、完整性和VM返回容量；不能重新读取今天的文件或重做原调用。原件丢失、损坏或无权读取时保缺失事实，不伪造空页。新调用不再生产这套工作区分页回填。

终端查询保原run、进度游标和completion；读取成功不等于进程成功，空stdout不等于结束，非零退出也不能否定已写文件。展示预算与进程寿命分开，完整结果和原回执仍由各自存储保留。

## 文本兼容

省略 `operation` 时保持旧的文本读取行为，继续支持 `offset`、`limit` 和 `query`。非 JSON Artifact
使用结构化操作时会返回 `artifact_not_json`，并明确提示改用文本模式。Handle 由数据库映射文件并在
保留期后清理。

## 存储与来源

共享实现位于 `tool_results/`，供 Core、Plugin 与 Work 使用。保留原工具句柄、`data/tool_artifacts` 路径、媒体授权、隐私删除和回收边界。Work 产物沿原 parent 关系查询全部祖先的实际保留状态，祖先仍在执行时不因直属父已结束而过早回收；这只决定保留，不扩大读取授权。工具调用的强制来源证据与可丢诊断分别结算；可丢诊断继续共用有界异步 writer。

人物归并后的读取授权使用共同人物语义，仍核原Conversation、generation、实际读取scope及隐私代次；
ArtifactAccess、context_note和原Memory工具回执的来源字段与hash不重写。原事件分区继续是历史原件，
不按今日账号或当前根重新编码；已经接纳的内部工作不为写入真实工具证据重新挑选一个活跃账号。
原人物及当前根人物的真实禁用仍独立生效。
