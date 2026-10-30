# 结果预算与 Artifact

`ToolResultBudgeter` 对所有 Provider 生效。默认单次展示预算为 **64000 个序列化 JSON 字符**，
包括信封与转义；显式 `tooling.result_token_budget` 按既有四倍字符换算覆盖 agent 字符预算。
Main、公开 reader 和 CodeHost 共用 `effective_result_character_budget`，Memory 不再提前删尾或
按展示长度否定成功读取。它不是实际 tokenizer 计数；真实模型输入容量仍沿原请求合同核验。

预算内直接交付实际正文。Web 搜索／读取保留原来源归档，但归档不再使小结果只剩目录。
超长原件保存在 `data/tool_artifacts/`；模型收到实际有界 `data`、`truncated`、原执行事实和
`artifact_handle`。`read_more` 是可直接传给 `read_tool_artifact` 的参数列表，各分支分别给准确
path／offset。字符串按原文 Unicode 字符，数组只展示完整子项，部分字典沿 reader 的稳定键序。
原查询的 has_more／next_cursor 与这份固定原件的展示 offset 分开。

首个数组项放不下时，数组不切坏；`preview={path,value}` 在明确子路径展示真实部分内容，
其字符串续读仍按原件路径与已展示字符数定位。标量无法容纳时返回真实 oversized 事实，
不把巨大数值原样塞出预算。必要错误、原 ID、进程状态及发送的逐步 `delivery` 回执先于正文保留。
极小预算不足以容纳必要执行信封时沿原软目标语义保事实，不撤销已确认效果。

文件路径不暴露给模型。Artifact 关闭、I/O 失败或实际存储容量不足时仍交付真实有界内容，
没有成功保存的句柄就不承诺续读；身份、来源和隐私拒绝继续拒绝。原件与 typed outcome 保完整来源，
完整 inline 保原 evidence_state，部分预览不宣称全文引用已被模型看见。

## 结构化 JSON

Core Tool `read_tool_artifact` 支持三个通用操作，不依赖任何外部服务的业务字段：

- `inspect`：查看对象键、数组长度和浅层类型；对象键使用稳定排序。
- `get`：按 `path` 精确读取对象、数组元素或分页后的数组区间；字符串按 Unicode 字符分页，
  返回 `value/offset/next_offset/total_characters/offset_unit=characters`。
- `search`：在键和标量值中搜索，返回包含命中的完整、有界对象及其准确路径。

`path` 是字符串和整数组成的数组，例如 `["meals", 27]`，不执行 JSONPath、
JMESPath 或任意表达式。标准工具结果外层的 `ok/provider/tool/data` 信封会被保留，结构化读取的
逻辑根节点为其中的 `data`，路径相对该节点；`[]` 表示逻辑根，不再额外加一层 `data`。
公开模型工具省略 operation 时默认 get，给出的 path 会实际生效；inspect 按需使用。

Artifact 存储沿配置计量；结构读取不再叠加固定文件大小、路径深度或搜索词长度拒绝。搜索遍历已保存JSON；分页沿请求和当前结果预算，没有另设扫描深度、节点数或固定100项帽。单个对象超过预算时
返回 `artifact_value_too_large` 和对象结构，不会从 JSON 中间截断。Artifact 本地读取不占业务工具
调用额度，但仍占模型请求，因此继续受 Agent 总循环和最终回复预算限制。
公开读取 limit 默认随当前有效预算，没有额外 32000 上限。inspect／get 集合按键／子项计数，
search 按匹配对象计数，get 字符串／text 按字符计数；实际整页仍受包含信封的有效字符预算约束。
持久回执不叠加展示字节拒绝。非空字符串必须前进，预算不足明确报错，不能把空页当结束。
字符串页直接返回，不再生成套娃 Artifact。

分页按实际 `ToolExecutionResult.model_payload()` 信封的 JSON 字符数计量，成功与错误分别
使用各自的包装。内容按需计量到能够判定预算的位置，只累计已访问的分页单元；不反复编码、
删减整个候选页，也不预编码剩余的大值。对象只选稳定排序的前 `offset+limit` 个键，完整值
快路仍保原键序。搜索保原完整遍历、第一次命中路径和按路径去重，单条记录先按完整预算
决定是否省略值，再计算整页预算。整数游标、末页 `null` 和字符串 `truncated` 都计入预算；
末页元数据可能变短，因此普通前缀不合预算时仍会核实际末页的可行性。

Code Mode 使用相同 JSON 计量原语和自己的 compact 编码，删除固定半预算预裁剪，按完整父结果
决定展示。超长父结果只归档一次，summary_ref 指父原件，result_ref 与 result_path 定位程序值；
result_preview 是 JSON 摘录，其 read_more 从真实 result 路径的 0 开始，不冒充已经读取的页。
原 ID、control、stdout 截断事实、累计预算和旧 accepted 恢复保持，不为凑预算只留身份 hash。

## 环境文件与旧脚本结果

普通文件内容由终端读取、搜索与过滤，八项通用工作区工具已退出新声明与执行。媒体仍用 `workspace_inspect` 交给原模型；文件用统一 `send(path=..., attachment_kind=...)` 内部固化并交付，也可发送已有不可变 artifact。实际快照 ID 与逐步发送状态沿原回执进入效果证据，不以原参数缺少 ID 漏记交付。普通终端文件不附加 CAS、分页文本协议或经验文件配额。

旧 Code 调用的 workspace_read 外置页仍按已接受子调用的原 result_ref 取回，核原授权、generation、隐私、完整性和VM返回容量；不能重新读取今天的文件或重做原调用。原件丢失、损坏或无权读取时保缺失事实，不伪造空页。新调用不再生产这套工作区分页回填。

终端查询保原run、进度游标和completion；读取成功不等于进程成功，空stdout不等于结束，非零退出也不能否定已写文件。展示预算与进程寿命分开，完整结果和原回执仍由各自存储保留。

## 显式文本读取与历史 reader

公开工具使用 `operation=text` 读取文本，继续支持 `offset`、`limit` 和 `query`。底层库的历史
text／8000 默认不机械修改，原 accepted 调用与旧 Code 回填仍按原合同恢复。非 JSON Artifact
使用结构化操作时会返回 `artifact_not_json`，并明确提示改用文本模式。Handle 由数据库映射文件并在
保留期后清理。
文本 query 不区分大小写；命中、offset 和 next_offset 均按原文 Unicode 字符定位，大小写折叠产生的扩展不改变原文坐标或内容。
无 query 的文本读取分块核完整长度和 SHA-256、增量解码 UTF-8，仅保留请求字符范围。
读取最多为登记字节数加一；长度或摘要不符先报完整性错误，再判断 UTF-8。带 query 的文本
仍按完整原文搜索，结构化操作仍完整解析 JSON；这两种读取在 worker 内完成，并在解析后
释放原始文本，未引入随机 JSON 读取或另一套索引。需要来源授权的读取均在 worker 返回后
重新核当前来源、generation、隐私代次及保留状态，才交给调用者。媒体 Artifact 在容量核验与发布间
复用同一份 UTF-8 字节，不重复编码。

## 存储与来源

共享实现位于 `tool_results/`，供 Core、Plugin 与 Work 使用。保留原工具句柄、`data/tool_artifacts` 路径、媒体授权、隐私删除和回收边界。Work 产物沿原 parent 关系查询全部祖先的实际保留状态，祖先仍在执行时不因直属父已结束而过早回收；这只决定保留，不扩大读取授权。工具调用的强制来源证据与可丢诊断分别结算；可丢诊断继续共用有界异步 writer。

人物归并后的读取授权使用共同人物语义，仍核原Conversation、generation、实际读取scope及隐私代次；
ArtifactAccess、context_note和原Memory工具回执的来源字段与hash不重写。原事件分区继续是历史原件，
不按今日账号或当前根重新编码；已经接纳的内部工作不为写入真实工具证据重新挑选一个活跃账号。
原人物及当前根人物的真实禁用仍独立生效。

聊天历史结果的 Host 来源另存实际 `source_event_ids` 与读取隐私代次，不进入公开工具载荷或
Main 激活事件集合。新调用、组合调用和原 accepted 结果重读均传递这份来源；Artifact 页保原
引用。遗忘复用现行快照清退与发布核验，覆盖跨 Conversation 的 Work、Memory 工具回执和
Artifact 副本，迟到提交不重新发布旧正文。已发送效果与原调用键不因此重放或改判。
