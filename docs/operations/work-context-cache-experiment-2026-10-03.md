# Work 上下文与聊天续接缓存核查记录

对应任务书：[聊天续接与工作区](../architecture/work-context-and-chat-continuation-taskbook.md)。
手动入口与范围：[cache_probe](../../tools/cache_probe.md)。

## 状态

脚本已完成首轮手动 API 核查；实现和组合验收仍在进行。本文未声明已提交、合并、上线或真实 QQ 验收。
离线普通整理 17 项、Gemini 缓存脚本 14 项通过，均使用 FakeProvider/MockTransport；
其中普通整理包含两个真实 Social accepted 回执、付费摘要计数、原调用不重发的完整
Processor/Runner/Gemini 协议组合。这里的“真实回执”是隔离数据库和假网关，未发 QQ。
DeepSeek Chat 与 Responses 累积历史扩展离线 2 项也已通过；增加纯离线合成引用诊断后，
当前缓存脚本共 17 项。引用诊断保存 page/section/允许引用与来源分类，不保存正文；
非标准引用只保留散列。既有 16 项和新增 1 项分别通过。
另有完整 Main 公共 H 的 W1→W2→W1 回归 1 项：两个 Processor 原 Work 与 note、
SQLite/Main 服务重开、原 WorkResumer、Gemini wire，公共片段原样顺序保留，固定 system/tools 不变。
另有多人输入真实入口组合 5 项：两个 steer 与两个未触发群消息按实际到达顺序追加，各一次；
普通后续讨论、SQLite 重开、Work 恢复保持原预算和回执。未获 admission 的候选取消后，
公共投影不消费该群消息；重开后的 Main 仍按原 event ID 首次观察它。
另注入真实 SQLite dispatched journal 写事务失败，公共投影、原 guard 和 journal 同时回滚，
没有新增 HTTP；实际已预留的请求计数保留，原 accepted 回执和已消费输入不回退。
提交后的实际 HTTP 超时则保留已派发观察和原 dispatched/native checkpoint；SQLite 重开
只读恢复能重建相同 wire，不执行工具或模型、不把未知发送认定为未发送。以下真实报告逐轮保留。
新增普通整理交叉回归先取得发送回执，再在后续 HTTP 期间收到群消息；真实付费摘要后
仍保留原群消息呈现，后续请求不重复追加，两个发送各一次。旧实现的真实 wire 缺少该消息，
修复后普通整理与多人组合共 22 项通过；摘要无效后的小增长也不会立即再付费整理。

## 首轮真实实验

开始时间 UTC 2026-10-02 18:29:46，运行于本次未提交工作树，不能称最终合并版本验收。
报告 `.cache/manual-cache-live-20261003.json`；不跟踪凭证或请求正文。
选定 `connection_2d4ba9aad301` / Gemini 协议 / `gemini-3.8-flash`，
经本地 SSH 隧道 `127.0.0.1:18045` 转发既有配置路由；不是上游原生 Gemini 直连。
固定输出预算 8192、temperature 0.7、reasoning low、timeout 600、最多重试 2，
本轮实际未重试。完整隔离核心合同 64 tools，manifest SHA256
`260a4271781d323fb8813a929bba87a1c486dd7f6e4b31f7b794ba6fe831fe09`；
不声称等于生产扩展合同 77 tools。各场景合成前缀 12000 字符，预热 1、后续采样 3，
C01 另有独立前缀对照；C06 失败后保留原结果，未继续热采样。

23 个逻辑调用与 23 个 HTTP 物理调用，全部 HTTP 200；含 1 个辅助摘要。
总 input 485280、output 17518、total 502798，仓库与 HTTP 观察计数一致。
总 token 未知 0；cached 字段已知 12、缺失 11、显式零 0。
已知输入 237433、cached 193655，已知输入加权比例 81.562%。
缺 cached 的其余输入不加入这个比例，不能当零；不能据此计算全部流量命中率。

| 场景 | 本轮实际结果 | 热缓存已知样本 / 全部热样本 | 已知输入加权比例 |
| --- | --- | --- | --- |
| C01 | 重复前缀完成；独立对照 cached 缺字段，不能认定关闭缓存 | 2 / 3 | 83.743% |
| C02 | 模型未选择读取工具，工具续接部分未验收 | 3 / 3 | 81.121% |
| C03 | W1/W2/W1 与新聊天追加完成，模型未发布线索，完整场景未验收 | 3 / 3 | 83.016% |
| C04 | 模型真实按 handle 读取本地原件；新请求退出正文与旧工具尾部 | 2 / 3 | 82.245% |
| C05 | 真读取回执；SQLite pool/repository/session 重开，原 effect 对账一致，预算保留 | 2 / 3 | 77.497% |
| C06 | 真实付费摘要被 `work_compaction_invalid_directive_source` 拒绝；原 Work 保留 | 0 / 0 | 不可计算 |

本轮发现合成 system 对“修改工具”的措辞与 context_note 更新冲突；补测将精简该说明，
保留首轮全部用量。C03 首版脚本的 complete 状态仅证明切换循环结束；当前脚本已把未发布线索
单列为未完成场景。C06 是来源引用验证失败，不能归因为缓存问题，也不伪造可通过的摘要。

## Gemini 补测

五轮均以 `7d26e2212c690d85e90cba436b2c498603602558` 为工作树基线，
使用当时未提交的实现，不是这个基线提交自身的能力，也未记录每轮完整源码 tree。
工具、主模型设置、12000 字符合成前缀、预热 1、测量 3 与首轮一致，请求间隔 0。
每轮新 namespace；失败场景停止该场景热采样，全部冷、辅助和失败调用仍计入总数。

| 报告 `.cache/manual-cache-…-20261003.json` | UTC 开始→结束 | 逻辑 / 物理 | input / output / total | cache 已知 / 未知 |
| --- | --- | --- | --- | --- |
| `live` | 18:29:46→首版未记录结束 | 23 / 23 | 485280 / 17518 / 502798 | 12 / 11 |
| `supplement` | 18:40:11→18:43:27 | 10 / 10 | 253441 / 43559 / 297000 | 6 / 4 |
| `work-summary-diagnostic` | 18:48:39→18:49:48 | 2 / 2 | 71088 / 23829 / 94917 | 0 / 2 |
| `ordinary-c06` | 18:56:35→18:57:26 | 5 / 5 | 157867 / 10851 / 168718 | 3 / 2 |
| `work-summary-unwrapped` | 18:56:59→18:58:54 | 2 / 2 | 69498 / 40720 / 110218 | 0 / 2 |

补测 `supplement` 的 C02 模型选择了原读取工具，原生签名与配对结果继续进入 serializer；
3 个热样本均 cache 已知，64781/84758 = 76.431%。C03 实际保存并发布一个 context_note，
W1→W2→W1 后仍可选取该观察，追加 3 个真实合成账本聊天事件；3 个热样本均已知，
48433/58936 = 82.179%。这里只驱动实际仓库和 WorkSession，不能代替完整 Main 公共历史组合。

首轮 C06 是 Work 来源引用错误；第二轮是结构错误。第三轮安全诊断证明返回整个外层
code fence（4033 字符、原 JSON 解析失败）。实现随后只解包整个单一外层 json/bare fence，
不搜索子串、不修坏字段、不追加修复模型。最后一轮 Work 输出虽已解包（2617 字符），
仍缺 version/completed/pending/failures/artifacts，directive 缺 text/refs，且有未知字段和
next_steps 类型错误；原 strict schema 拒绝，Work model_requests=2、tool_calls=0，未发布伪摘要。
请求向配置 endpoint 传入了 schema，但 AGM 最终上游 schema 未观测，不能据此归责 AGM 或 Provider。

`ordinary-c06` 则完成实际 `compact_ordinary`：一个付费摘要、四个主请求、created_works=0，
显式退出旧工作尾部并新建模型链。摘要整个 fence 解包后通过 strict schema；
3 个热样本都 cache 已知，56671/79412 = 71.363%。每个主请求的完整固定部分 hash 一致。
辅助摘要按合同无工具，其相邻首差异不能误称主合同变化；当前脚本另按同场景主/辅助序列比较。
摘要之后两个主续接请求的相同输入 parts 数依次为 3、28，首次差异分别在新尾部位置。

五轮合计 42 个逻辑与 42 个物理请求，全部 HTTP 200，实际重试 0，辅助摘要 5；
reported input=1037174、output=136477、total=1173651，总 usage 未知 0。
cached 已知 21、缺失 21、显式零 0；已知输入 460539、cached 363540，加权比例 78.938%。
这是保留失败的多场景实验合计，不能当成功场景均值、自然群聊平均或全部输入命中率。
原首版逻辑调用数组遗漏辅助项，但原 ORM 计数已包含；当前脚本已逐辅助执行记录逻辑与物理明细。

## DeepSeek 同场景对比

`manual-cache-deepseek-20261003.json`，UTC 19:09:17→19:09:30，另一个独立 namespace。
使用现有合法 legacy 配置经官方 Profile loader 解析：`deepseek-flash` / Chat Completions / 
FUNCTION_TOOL，原 API 直连；8192、temperature 0.7、reasoning low、timeout 120、重试上限 2。
完整 64 tools 与 Gemini manifest hash 相同，场景 C01/C02/C06、12000 字符、1 预热+3 测量、间隔 0。
模型、服务路线和 timeout 不同，不能把结果归因为一种 Provider 的普遍优势。

11 个逻辑/物理调用，HTTP 200 为 10、HTTP 400 为 1，实际重试 0；
reported input=227974、output=1333、total=229307。
cache 已知 10（正数 9、显式零 1）、未知 1；该未知是辅助摘要 400 且无 usage，
未知的潜在用量不造零。已知 cached/input=175616/227974=77.033%。
仓库逻辑、物理与总 usage 未知计数全部与 HTTP 观察一致。

| 场景 | 实际结果 | 热样本 cache 已知 / 全部 | 已知输入加权比例 |
| --- | --- | --- | --- |
| C01 | 固定前缀完成；独立对照也有部分缓存，不称 cache off | 3 / 3 | 98.791% |
| C02 | 实际读取调用及配对结果续接完成 | 3 / 3 | 89.880% |
| C06 | 主读取成功；ordinary 摘要 HTTP 400，被拒绝后未继续热采样 | 0 / 0 | 不可计算 |

初轮 C06 辅助请求使用 json_schema，Profile 则声明 FUNCTION_TOOL；原报告没有保存错误正文，
只能确证 HTTP 400 / LLMInvalidRequestError，不能反推出具体上游错误句子。
共享摘要实现随后按真实 Profile 模式选择：只有 JSON_SCHEMA 带 response_format；
FUNCTION_TOOL/TEXT_JSON 的独立无工具摘要使用 schema 文本与 strict 本地校验，
不添加专用 emit 工具或路由/自动修复调用。

唯一修后复测 `.cache/manual-cache-deepseek-structured-20261003.json`，
UTC 19:26:13→19:26:45，同场景、相同 manifest/Profile/前缀长度/样本数，另用新 namespace。
11 逻辑/物理均 HTTP 200，重试 0，usage 未知 0；
reported input=280714、output=5666、total=286380。
cache 已知 11（正数 10、显式零 1），cached/input=188416/280714=67.120%。
C01/C02 完成；C06 辅助请求实际无 response_format，返回合法 JSON 和三个正确字段，
但 facts[0].refs、pending[0].refs 为空，strict 校验报 too_short，
保留 `ordinary_compaction_invalid_structure`，未发布伪来源或继续热采样。
HTTP 合同已修正不等于模型一定给出合法引用，失败后不盲重试。

两轮 DeepSeek 共 22 物理调用，reported total=515687，另有首轮摘要 400 的 usage 未知 1。
与五轮 Gemini 合计 64 物理调用，reported input=1545862、output=143476、total=1689338，
另有一个 usage 未知；该累计不作混合 Provider 命中率比较。

## DeepSeek Responses 与累积聊天补测

前述两轮实际是 `OpenAICompatibleProvider(provider_name=deepseek)` / Chat Completions，
并非 Responses。两个 namespace 分别为 `404da320644049e1965ddd569aa022e4`、
`3f54f247fd66452893918352d3bab96e`。合计 input=508688、cached=364032、
miss=144656、output=6999、total=515687，已知加权 71.563%；与用户所示时段数字逐项相同。
该合计包含冷、热、独立前缀对照和摘要，不能当自然流量或热样本平均。

显式 `--provider deepseek --protocol responses` 使用 `DeepSeekResponsesProvider`，
保留原 `main` Profile 的 Chat 协议元数据并标出实验覆盖；不修改生产 catalog。
API origin 为 `https://api.deepseek.com`。仍为相同 64 工具合同、模型与设置，
不添加 Provider session header，也没有把不支持的 Responses 请求改投 Chat。

| 报告 `.cache/manual-cache-…-20261003.json` | UTC 开始→结束 | 逻辑 / 物理 | input / cached / output / total |
| --- | --- | --- | --- |
| `deepseek-responses` | 19:38:06→19:38:40 | 11 / 11 | 280626 / 188672 / 6356 / 286982 |
| `deepseek-cumulative-responses` | 19:40:51→19:41:04 | 9 / 9 | 187745 / 178560 / 881 / 188626 |

两轮所有 HTTP 200、重试 0、总 usage 与 cache 全部已知，逻辑/物理/未知计数与仓库一致。
第一轮 namespace=`2eed24309eb14505981a396104770748`；C01 热 60288/61023=98.796%，
C02 真工具配对递增热 77184/85655=90.110%。主请求固定部分 hash 不变；
C01 相同公共输入片段 35752 字节，C02 相邻相同片段 35849→72658→73609 字节。
这里是 endpoint wire 解析后规范化 JSON 的片段长度，不能换算成 Provider token。

Responses C06 仍未验收：一次独立无工具 paid 摘要 input=52593、cached=0、output=5189，
返回合法 JSON，三个合法顶层字段加一个未知字段，严格报 `extra_forbidden`，
保留 `ordinary_compaction_invalid_structure`，没有发布摘要或进入 hot。
它与上一 Chat 复测的空 refs/`too_short` 是不同失败；不关校验、不追加语义修复请求。
11 次全计入得到 188672/280626=67.233%，不能拿这总率否定主热前缀比例。

C08 另用 namespace=`782d52c627dd4a92bd6767949806c464`，内部 chain 始终为
`074f363a639341448c1cd8de2ceabae3`；一预热加八后续请求，实际路径 `/responses`。
保留每次完整 assistant 输出及 opaque item，再追加下一条 user；不混 summary/control。
它经真实 TaskModelExecutor/Provider，使用合成公共前缀，不能冒称完整 Main/router 自然聊天。
各次 input 20350→20511→20627→20745→20861→20988→21109→21212→21342，
cached 12800→20352→20480→20608→20608→20736→20864→20992→21120。
每次旧 input parts 全部留作新前缀，固定 system/tools/设置的首差异均为 null；
相邻完整 input 规范化 JSON 公共字节 35881→37025→37952→38925→39846→40838→41791→42689。
新 namespace 的首样本仍命中共享工具/系统片段，不称完全关闭缓存：12800/20350=62.899%。
热八轮 165760/167395=99.023%，全部九轮 178560/187745=95.108%。没有固定通过率门槛。

全部九轮实验合计 84 物理调用：Gemini 42、DeepSeek Chat 22、DeepSeek Responses 20。
reported input=2014233、output=150713、total=2164946，另保留一次旧 HTTP400 无 usage；
不能将其潜在用量造零。每次完整报告与失败样本保留，不选最佳轮次，不做跨 Provider 总命中率。
C01/C02/C03/C04/C05 的成功证据范围见各轮；Gemini 普通 C06 已成功，Work C06 与
DeepSeek C06 仍严格失败；C07 未运行真实图片/MCP，不声称通过。缓存实验仅手动运行。

## 追加 Gemini 实测

用户另授权更多手动 Gemini 样本，沿原路由、64 核心工具、隔离数据库与假业务继续。
每轮新 namespace，预热 1、后续 2；仍为当时未提交源码。未更改生产路由或关闭 strict 校验。

| 报告 `.cache/manual-cache-…-20261003.json` | UTC 开始→结束 | 逻辑 / 物理 | input / output / total | cache 已知 / 未知 |
| --- | --- | --- | --- | --- |
| `gemini-additional-main` | 20:14:33→20:16:56 | 13 / 13 | 314194 / 15199 / 329393 | 7 / 6 |
| `gemini-additional-work` | 20:18:05→20:19:08 | 2 / 2 | 69821 / 8199 / 78020 | 0 / 2 |
| `gemini-work-shape-instruction` | 20:22:53→20:24:29 | 4 / 4 | 93854 / 13637 / 107491 | 0 / 4 |
| `gemini-work-ref-provenance` | 20:32:20→20:33:07 | 2 / 2 | 69822 / 5621 / 75443 | 0 / 2 |
| `gemini-work-input-scope` | 20:36:07→20:36:34 | 2 / 2 | 69827 / 3095 / 72922 | 0 / 2 |

五轮全部 HTTP 200、无重试，总 usage 全部已知，仓库逻辑/物理计量与 HTTP 一致。
主组 C02 实际工具续接，C03 保存一个 context_note 并在 W1→W2→W1 中选取，
C05 SQLite 重开后原 read receipt 相同且原业务调用一次，C06 普通 paid 摘要严格通过并
进入两个后续主请求，未创建 Work。主请求固定部分相同，辅助请求单独记录。
热已知比例分别为 C02 40490/55627=72.788%、C03 32285/38971=82.844%、
C05 16166/23091=70.010%（另一个热样本 cached 缺失）、C06 32285/40343=80.026%。
普通辅助摘要 input=59230，cached 缺失；不能计为零。C03 的真实仓库 driver 仍不代替完整
Main 公共 H 的离线入口组合，也不代表自然群聊平均。主组全量已知加权 121226/158032=76.710%。

第二轮 Work paid 摘要返回合法 JSON，但有未知字段、`input_dispositions.reason` 与事实
`text` 缺失，严格报 `work_compaction_invalid_structure`。因此补了精简准确的嵌套字段说明，
第三轮独立对照的三个实际 paid 辅助页均为九个合法顶层字段，strict schema 全部通过；
最终仍报 `work_compaction_invalid_directive_source`。结构闭环改善不等于任务引用语义验收。
两轮均不发布最终候选、不进入热采样，原 Work 分别保留 models=2/4、tools=0；失败样本
和全部辅助用量保留。这五轮的 function_tool Profile 工具外摘要使用 TEXT_JSON，
实际 Gemini responseSchema 未发送；未将配置模式当作上游 schema 保证。

第四轮增加合成引用诊断；旧三页报告没有生成引用值，不能从旧错误码反推具体 offending ref。
本轮实际 `task_directives.refs=[goal,event:1]`，均获供应，任务引用错误没有重现。
但源 `task_input_refs=[]`，模型仍生成输入分类，被 `work_compaction_invalid_input_disposition`
拒绝。首版诊断未记录该分类的具体 ref，故不能给出其实际字符串；空新输入集合与错误码
只证明这次分类不是合法原 Work 新输入。诊断随后补了分类 ref/类型/非空原因布尔值，
第五轮针对明确新增的空输入分类说明独立对照：仅分类 `task_inputs`，空集合保持 `[]`。
本轮仍是九个合法顶层字段和合法 JSON，但 `next_steps[0]/[1]` 为非事实对象，严格
`model_type` → `work_compaction_invalid_structure`。这次在 schema 阶段拒绝，不能宣称
输入分类已经通过。原 Work models=2/tools=0、无 hot，缓存两个 NULL；停止进一步格式校准。
模型输出及来源呈现边界需要分别核查，不能把所有拒绝称为同一故障。

累计十四轮 107 物理调用：Gemini 65、DeepSeek Chat 22、DeepSeek Responses 20；
reported input=2631751、output=196464、total=2828215，另一次旧 HTTP400 usage 未知保留。
旧九轮 84 调用的表与截图归因仍有效；新增样本不能并回旧时段或按 Provider 混算命中率。
Work C06 的来源语义及 DeepSeek C06 仍未通过，C07 仍未运行真实图片/MCP；不以最佳样本替代全部结果。

## 原生 schema 兼容核查

另一次独立合成请求使用完整 `CompactionSummary` 原生 JSON Schema，未简化字段、
未更改生产 Profile 或路由，实验重试为 0。实际客户端 Gemini wire 包含
`responseMimeType=application/json`、完整 `responseJsonSchema`，schema 2139 字节，
有九个顶层字段、三个 `$defs`，完整 body 2908 字节。报告为
`.cache/manual-cache-gemini-native-work-schema-20261003.json`，
UTC 20:53:40→20:56:21，namespace `256c439d03924b6d8109b2a4f25feb04`。

这一次物理请求 HTTP 200，但返回仍带代码围栏，解包后的严格结构校验不通过：
`version` 等字段缺失、`task_directives` 项为非事实对象、输入分类的 `kind/reason` 缺失。
原报告的 `ValueError` 是引用诊断函数遇到合成 records 与索引数量不一致；
它发生在 `validate_summary` 前，不冒充生产 Work 错误码。独立 `summary_shape` 已按
`CompactionSummary` 检出上述结构错误，没有发布 Work 候选或重发 API。
usage 为 input=188、output=23453、total=23641，cached 缺失；不能计为零，
也不把输出超过客户端 8192 当作客户端预算已获上游遵守。

离线新增格式选择只在 Gemini、声明结构化能力、原模式为 FUNCTION_TOOL 的无工具辅助
请求中使用 JSON_SCHEMA；显式 TEXT_JSON 和其他协议保留原模式，Main 原工具合同及
Profile revision 不变。15 项新离线回归通过，连同原模式、pending 输入恢复和手动脚本
离线验证共 45 项通过。该证据不证明当前 AGM 执行完整 schema。
官方 Google 文档支持原生 schema，但明确有 schema 子集、复杂度及语义校验限制。
[官方结构化输出说明](https://ai.google.dev/gemini-api/docs/generate-content/structured-output?hl=en)。

本地 AGM 官方源码 `6e8b982` 的原生 Gemini wrapper 克隆请求，未找到
`responseJsonSchema` 到 `responseSchema` 的映射；OpenAI 路径则清洗 `$defs/$ref` 后
写入 `responseSchema`。这构成网关方言兼容候选；没有最终上游 wire，不能确定本次
哪个环节忽略了 schema，也没有修改 AGM 或用删字段重试。
[Gemini wrapper](https://github.com/lbjlaq/Antigravity-Manager/blob/6e8b982aee7e3d3d53501825431142eea9a3f9ab/src-tauri/src/proxy/mappers/gemini/wrapper.rs#L40)、
[OpenAI schema 映射](https://github.com/lbjlaq/Antigravity-Manager/blob/6e8b982aee7e3d3d53501825431142eea9a3f9ab/src-tauri/src/proxy/mappers/openai/request.rs#L1038)。

包括本次共 108 物理调用：Gemini 66、DeepSeek Chat 22、DeepSeek Responses 20；
reported input=2631939、output=219917、total=2851856，旧 HTTP400 usage 未知仍保留。
前十四轮和旧截图时段的计量不变；本次兼容核查不并入主会话热率。

## 显式 schema 方言对照与 Runner 恢复

根据上述源码差异，另做一次独立 `responseSchema` 方言对照，实验重试为 0，
没有改变生产 catalog。报告 `.cache/manual-cache-gemini-agm-response-schema-20261003.json`，
UTC 21:01:29→21:01:46，namespace `b97cbc85b4dd4140b55093216c3719a2`。
它保留完整原 `CompactionSummary` 及本地引用校验；仅在 wire 展开 `$defs/$ref`，
采用 `type/description/properties/required/items/enum/title` 子集，删除的协议字段路径逐项记录。
客户端 body 3351 字节，schema 2619 字节，九个顶层字段及 required 保留。
一次 HTTP 200，完整 strict 结构与 `validate_summary` 引用校验均通过，
input=198、output=2288、total=2486，cached 缺失。没有发布真实 Work 或执行业务。
它是一次兼容成功样本，不证明全部 Work C06 分页和语义稳定，也不证明前一失败的唯一原因。

本地实现新增显式 `wire_options.gemini_schema_format=response_schema`；默认仍为
`response_json_schema`，不按 URL 或内容猜路由，主 Agent 声明和工具参数格式不变。
新方言只允许 Gemini，有限展开拒绝递归、未知引用和组合 schema；本地原 schema、
非空 refs 及来源校验不变。离线完整 Work 转换结果与上述成功实验的 schema 字节同形：
SHA256=`6cf8fe0f8a0d48141a2562198cba726be41290285708510152dcc904c0991568`。
这个离线比较不冒充新 serializer 再发过辅助请求。默认字段不进入旧 Profile fingerprint，
显式方言参与 fingerprint；38 项定向回归、相关 Ruff/format 及六个源文件 mypy 通过。

再用新 GeminiProvider 的显式配置运行一次真实 `AgentRunner.run` 恢复主请求。
报告 `.cache/manual-cache-gemini-paid-fixture-resume-20261003.json`，
UTC 21:16:29→21:18:01，namespace `da2ef3155ee24d0a824187ce61a502e1`。
原 paid page 是 strict-valid 合成候选，保存失败后保留原 staging，再加入隔离账本 steer；
合成辅助调用不产生 Gemini HTTP 或供应商 usage，不称真实 Gemini 摘要成功。
恢复没有重购辅助页，实际一个主 HTTP 200：input=7908、output=13012、total=20920，
cached 缺失。原 Work ID、业务回执和原调用一次均保持；model 预算 1→2、tools=1，
steer 在实际请求中一次并 consumed，staging 退休，固定 system/tools 相同。
没有新业务 dispatch、QQ 或第二 Bot。主请求不带摘要 schema，因此不能用它证明
上游执行了辅助 schema。它验证恢复路径与预算连续性，不代替模型任务完成的语义验收。

全部十七轮共 110 物理调用：Gemini 68、DeepSeek Chat 22、DeepSeek Responses 20；
reported input=2640045、output=235217、total=2875262，旧 HTTP400 usage 未知仍保留。
三次新增请求 cached 都缺失；没有把它们计零或并入主会话热率。
此前失败全部保留，没有常驻付费测试或为追求成功率自动重试。

## 验收边界

实验隔离真实业务，不发 QQ，不启动第二 Bot，不读取生产聊天内容或业务数据库。
真实 serializer 和持久机制的证据与完整 Main 编排、隐私负例、重启恢复等离线组合分开。
手动 C06 直接调用整理 helper，失败状态不等于生产 Runner 已暂停工作。完整原请求仍
hard-fit 时 Runner 保留原配对序列继续，真实 hard 超限另行停止；该分支有离线回归。
AGM 转发后的最终上游 payload 未观测时保留未知；自然群聊、计费与模型语义质量另验。
没有常驻付费测试、自动启动或固定缓存命中率承诺。
