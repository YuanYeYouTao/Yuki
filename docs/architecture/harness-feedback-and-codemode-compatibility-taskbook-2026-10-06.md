# Harness 当前模式反馈、恢复与数据库防回归任务书

日期：2026-10-06，按本轮用户范围修订。状态：**已授权实施，当前模式修复与定向本地回归完成，组合检查与交付进行中；提交、合并、部署和真实 API 验收分别记录。**

本轮实施目标以主线 `f508f64980d5c6fe2a81ca0300909e5852b904cf` 的当前模式为源码基线。按用户要求，测试分支已单独拉取到同级目录 `D:/Code/My_Github_Repo/Yuki-QQbot-codemode-reference`，detached HEAD 为 `d763b294e612f2ec5c81f29cf027381262a43c40`；该提交已经合入上述主线，只读用于理解未来方向。原仓库的测试分支引用未改，远端分支未推送。文档写入本地主目录不表示该目录已切到主线；实施前重新核验基线。

上一轮对 `0dc4dbcd…` 的兼容缺失结论已过时，新参考提交已经补入相关主线修复。本次不再把这些历史差异列作当前移植待办，也不将参考分支的设计当作当前生产合同。

## 1. 目标与边界

依据[共同开发约束](development-contract.md)、[主 Agent 执行与恢复](main-agent-runtime.md)、[Tool Kernel](tool-kernel.md)、[Provider 合同](model-providers.md)、[输出边界](provider-output-boundary.md)和[持久工作者](persistent-subagents.md)，先处理当前模式的错误反馈、执行事实与恢复缺口，重点防止数据库等待回归。MCP 整体重构和 Code Mode 移植留待后续，本轮只保留扩展空间，不为未来模式提前建立机制或整体验收工程。

修复对象是事实和反馈语义，不是为 Gemini 增加独立 Harness。Provider 负责协议转换，公共执行层负责权限、工具执行、结果分类、累计预算、原 Work 与效果回执。适配不能根据正文或错误关键词改变 Provider、工具声明或业务策略。

长任务汇报沿已有完整工具结果、安全输入接入和显式结束边界提供机会。**不新增定时汇报器、阶段登记台账、强制每轮正文、额外总结模型或“报告真实性”分类器。** 无实质进展可以不发送；普通聊天、quiet、SELF、child、内部返回来源继续各自合同。已存在的开始/结束政策与阶段提醒分开，不在本轮机械强化，也不为了方便删除身份、权限、预算或未知效果围栏。

优先复用 `ToolExecutionResult`、原 typed evidence、Work inputs、journal、effect 和 Social receipt。首轮不新增数据库表、永久错误账本、统一重试状态机或第二套 Agent loop。Code Mode 属于调用和执行方式的变化，不是新的业务事实源。

不写死经验参数：重试次数、并发量、分页大小、显示预算、汇报节奏和软水位按现有配置/政策处理，验证采用当时真实设置；当前采用的数字和类/方法结构不是永久架构规则。请求链内的身份、授权、回执和已付预算仍必须正确，工具合同随明确版本/新链合法演进，不暗改已提交请求。

## 2. 证据等级与已完成审查

| 等级 | 含义 | 能证明什么 |
| --- | --- | --- |
| A | 当前源码及局部原函数离线重放 | 确认条件分支和投影行为；不证明完整入口或真实上游行为 |
| B | 按原内部 ID 有界读取的历史现场证据 | 确认该样本的回执、路由、续接记录和实际交付；不补造缺失的上游 wire |
| C | 静态候选或政策问题 | 先全链重放再决定是否修改；不能写成已发生事故 |
| M | 测试分支与主线的源码差异 | 只读方向参考；不构成本轮移植要求或分支验收 |

- [x] 核验主线与现行合同；拉取同级只读参考目录，确认新 Code Mode 提交已整合主线，保护原本地修改及测试分支。
- [x] 复核此前已经实现的汇报、回执、预算和 Gemini 有界纠正，避免重做整套机制。
- [x] 对照现行 SQLite 合同及相关历史 PR 的正文、改动文件和当前实现，并运行修复后的查询计数、索引计划和竞争回归；线上性能验收另行记录。
- [x] 完成阶段反馈、Gemini 结果投影、五类协议、Code Mode receipt 和 MCP 不确定性等局部重放；它们使用合成控制对象/响应，不是完整模块测试或真实模型验收。
- [x] 历史“测试通过”样本中，实际失败为 pytest 缺失/exit 127；后续探测的 exit 0 来自 fallback echo。生成错误报告时，Host 原生 continuation 仍保留两条反证，可排除本例在 Yuki 侧裁剪/压缩丢失这两条证据。
- [x] 使用修复后的完整入口、实际 HTTP 序列化的 MockTransport 和隔离数据库做验收；真实上游 API 单列。
- [ ] 在信息、预算、工具合同相同的前提下进行真实模型对照，判别反馈质量与模型误判。当前没有该验收结果。

诊断中没有找到该样本的 `model_start/provider_start` 请求快照，不等于没有请求。Host checkpoint 不等于 AGM 或任何外部网关最终发往上游的 wire；不能据此宣称网关无责任，也不在 Yuki 仓库加入外部网关源码或运维材料。

## 3. 已确认问题与执行清单

下列当前模式修复已完成本地实现与定向回归。勾选只表示对应本地实现/验证完成，不代表合并、上线或真实模型验收。P1 为优先正确性或直接反馈缺口，P2 为可读性与机会；优先级不把经验政策升级为永久不变量。

### H01 / P1：参数错误给出可纠正的安全反馈（A）

定位：`capabilities/validation.py:103–109`、`capabilities/runtime.py:126–135`、`services/main_agent_backend.py:516–524`。缺字段、错误类型、错误枚举最终都可能只剩 `tool_input_validation_failed`；原 schema 的字段路径和错误类别被丢弃。

- [x] 从已冻结 schema 生成有界字段路径、校验类别和期望摘要，透传到原工具结果；例如说明 `run_id` 必填或已声明枚举范围。
- [x] 不回显用户参数值、原始异常、凭据或未经筛选的未知字段名；保留 strict validation，不靠类型强制转换或放宽 schema 隐藏问题。
- [x] 使用现有错误结果结构，区分参数可修正、权限拒绝、环境依赖缺失、服务故障和效果未知，不把所有拒绝都描述为可重试。
- [x] 当前主 Agent、core/plugin 绑定和五类协议看到同一事实，仅 wire 编码不同；不为此改造 MCP 架构或增加 Code Mode wrapper 的移植工作。

### H02 / P1：明确前置拒绝尚未执行，预算口径分开（A）

定位：上述 backend 前置返回、`capabilities/coordinator.py:123–131`、`runtime/work_session.py` 的 `execute`、`runtime/effect_outcomes.py`。当前仅 `ok:false,error` 的前置拒绝可被 coordinator 和 durable evidence 记录成 `executed:true`，虽然业务 `ok` 仍为 false。`accepted` 在这里表示错误回执已保存，不是操作成功。

- [x] 仅对确知在 binding/外部派发前发生的拒绝统一标 `executed:false` 和未提交，保留原 call 配对。
- [x] 遵守原 admission/paid reservation 计量：已计量或已付费的失败尝试不因反馈修复被抹掉，未获 admission 的拒绝不因此新增收费；段内实际执行计数另有口径。不能借本修复重置预算或笼统退还所有失败尝试。
- [x] 重放缺参数、权限变化、新输入到达、限额拒绝、实际执行失败和未知效果，验证计量及完成判定均不把拒绝当业务证据。

### H04 / P1：拒绝同一响应中的重复工具 call ID（A）

定位：`llm/deepseek_responses.py` 的 `_parse_response`（OpenAI Responses 复用）、Runner 工具签名映射及 coordinator 的按 ID 结果字典。原方法重放接受两个不同 function_call item 使用同一 `call_id`，后续映射可能覆盖。

- [x] 在业务工具派发前验证同一响应的本地调用身份唯一性；覆盖相同和不同参数，不按内容哈希合并潜在写操作。
- [x] Chat、Claude、Gemini 的既有重复 ID 拒绝保持；OpenAI/DeepSeek Responses 补齐等价行为。
- [x] 对包含上游原生工具的异常输出，不能仅因本地 ID 无效推断原生效果未发生；保留原 Provider 状态和实际计量。
- [x] 畸形响应不执行任何有歧义的本地工具，已确认效果不重跑；是否可纠正由具体响应的已知效果边界决定。

### H05 / P1：原生工具事件与“空响应”恢复分开（A，线上影响待验）

定位：Claude/Gemini `_parse`、Responses `_parse_response`、Runner 的 `LLMEmptyResponseError` 恢复。合成响应已经包含 server/native tool 事件但没有正文或本地调用时，适配器仍可抛 empty，新的 checkpoint/events 未返回；Runner 的一般 empty 恢复可能再次请求原生工具。传输层只请求一次，不保证 Runner 没有再请求。

本次 native fixture 仅模拟 Provider 声明 completed，未提供可信搜索来源；它证明解析/恢复分支，不证明搜索真实成功或线上已重复搜索。

- [x] 分开确认无效果的空结果、有原生事件的无正文结果、不完整/被阻断结果、传输未知；保留真实 usage、事件及合法私有状态。
- [x] 有原生事件不自动视为“搜索成功”：完成声明、可信来源、目标完成分别核验。
- [x] 可安全续接时继续原协议链；不能证明可安全续接时明确等待/失败，不伪造本地原生工具回执或盲重跑。
- [x] 实际执行器→Runner→Supervisor 重放原生超时、断连与不可用：只对真实 HTTP 派发后的未知结果停止重排；派发前拒绝、本地请求与认证保持原类别和预算。
- [x] 同时覆盖 Claude、Gemini、OpenAI Responses、DeepSeek Responses；不通过禁用搜索或偷偷换 Provider 绕过问题。

### H06 / P2：命令结果在所有投影中保持清楚（A/B）

定位：当前 `sandbox/environment_tools.py`、`capabilities/results.py`、`runtime/effect_outcomes.py`。终端工具成功取得结果与进程执行失败是不同事实；现有外层 `ok:true`、内层 `status:failed,exit_code:127` 容易被忽略。本轮修当前结果的可读性，不改未来 VM 投影。

- [x] 从终端所属服务的可信结果提取进程状态、exit code、pending、run ID 和实际输出摘要；短、长、artifact/minimal 投影保持一致。
- [x] 明确“调用返回正常”“进程失败”“效果未知”“验证目标完成”的层次，不增加另一个真实执行账本。
- [x] **不把 exit 非零机械改为 `mutation_committed:false`。** 命令可能先写文件再失败；没有确认全部效果时保持原事实和查询边界。
- [x] `terminal_read` 成功不等于被查询进程成功；探测命令退出 0 不等于测试已运行。结果正文属于外部材料，不从成功关键词推断验收。

### H07 / P2：补齐非阻断的阶段汇报机会（A）

定位：`services/work_reporting.py:40–87`。纯工具轮正文为空会提前退出；`WORK_CONTROL_NAMES` 整类排除也包含子结果；同批普通闲聊发送和失败发送可以压掉提醒。

- [x] 阶段机会依据原批次真实业务结果，而不是要求模型先写正文。子结果读取与纯生命周期控制分别处理。
- [x] 提供一次自然的“是否有值得说明的发现、阻塞或调整”机会，由 Agent 判断是否发送；没有新信息继续工作。
- [x] 区分任务进度与同会话闲聊，优先使用已有 `work_report` 和回执事实，不建立语义分类器，也不强制每条闲聊附标签。
- [x] 阶段提醒不阻断后续工具、不自动替写消息、不每轮增加总结请求；未知发送仍按原 ID 核对，不用提醒触发重发。
- [x] 覆盖当前真实执行循环的安全边界；表达公共反馈语义，不绑定某个 Runner 私有方法名，也不提前接入 VM hook。

### H08 / P2：追问提醒、发送尝试与送达分别解释（A）

定位：`work_reporting.py` 的 `append_input_feedback`。带原事件关联的失败/unknown 发送也会抑制本次答复机会并推进提醒水位。推进“已提供提醒”不等于用户收到答复。

- [x] 继续使用原 Work input/event ID 和现有回执，明确失败、未知、已送达；不新增答复状态机或重新接纳 Work。
- [x] 确定失败可提供说明阻塞或纠正机会；unknown/uncertain 先查询原发送，禁止盲重发。
- [x] 资料补充无需每条回复；quiet、SELF、内部返回来源不被强制群发。
- [x] 失败提醒后的恢复、重复/迟到输入、多条 steer、同批发送与工具配对，以及进程重开必须回放。
- [x] 补测同一输入先发送失败、随后真实成功的情况；不能把最早一个失败见证当成从未送达，也不能为寻找后续成功全量扫描旧效果正文。

### H09 / P2：删除过时执行指令，保持合同一致（A）

定位：`prompting/contracts.py:50` 要求“可用 answer 回应”；公开 task_control action 已无该动作，`persistent-subagents.md:10–12` 要求主 Yuki 使用 `send_message`。

- [x] 修改这条过时指令，区分合法 `output_kind=answer` 与已删除的 action；不重新开放旧 action 来迁就错误提示。
- [x] 对照已冻结 schema 扫描提示、工具说明和现行文档的其余动作与回执措辞；历史 worker 兼容路径需真实调用者与退出条件。
- [x] 技术执行合同留代码和开发文档，不写进用户人格 prompt，不堆叠重复“不要撒谎”指令。

### H10 / P1：恢复分类区分 SQLite BUSY 和 LOCKED（A）

定位：`runtime/activation_outcome.py:classify_failure` 当前把两种主码及相关错误文本一起归为 `sqlite_busy/retryable`；`work_supervisor.recover_failure` 因而自动重排。这与现行 SQLite 合同不符。

- [x] 仅可核验 BUSY/合法扩展码进入有界数据库重备；LOCKED 与内部故障单独分类，不以错误字符串统一代替错误码。
- [x] 恢复只重备可安全重复的数据库计划，保留原 Work/operation、已付请求与工具回执；租约失效停止受保护执行。
- [x] 实际 Runner/WorkSession/Supervisor 复现已返回 response 保存 BUSY 后重复购买模型的缺口；同份 journal 有界新事务重备，response/paired 保存耗尽后沿既有不可用路径暂停，不退款、不重购、不声称新结果已持久化。
- [x] 用相关真实 SQLite 竞争及错误分类回归验证，不加长 busy timeout、不增加无限重试，也不把本问题写成已证明的慢回复主因。

### H11 / P2：普通聊天和 SELF 区分未派发发送与已尝试发送（A）

定位：`services/main_agent_backend.py:411–414,917–959`。`begin_batch` 在真正执行前仅看到 `send_message` 就设置尝试标记；即使随后是参数/预算前拒绝，未送达最终正文的纠正机会也会关闭。原错误仍会回模型，缺的是模型忽视拒绝后写内部 final 的有限机会。

- [x] 结合已有真实结果区分未派发、派发失败、未知与送达，不用“批次含发送”代替实际派发事实；不得自动把内部 final 发出。
- [x] 确认未派发的参数错误可以提供原链内的有限纠正机会；已成功不重发，unknown 先查询原 ID。SELF 明确沉默和 `NO_REPLY` 仍是合法结果，不强迫每轮发言。
- [x] 同步核对 H07/H08 的尝试/送达口径，不为每个入口再造一个发送状态机；旧“有尝试即不纠正”政策变更必须覆盖成功、明确失败和未知三个负向场景。

## 4. Provider 分工与共同验收

共享层提供同一真实调用和结果事实；不要求所有 Provider 使用同一 HTTP API、状态字段或历史形状。

| 协议/实现 | 本地工具回执 | 失败表达与本轮要求 | 必须保留 |
| --- | --- | --- | --- |
| Chat Completions | `role=tool` + `tool_call_id` | 保留可读结构化错误；不虚构 `is_error` 协议字段 | assistant 调用、reasoning 方言、原顺序及已提交完整声明 |
| OpenAI Responses | `function_call_output` + `call_id` | 无统一原生错误位，错误仍在 output；补重复调用 ID 拒绝 | reasoning/output items、配置内的状态承载方式、原 output item 身份 |
| DeepSeek Responses | 对应 Responses 方言的调用输出 | 不假定与 OpenAI 全部能力相同；不能依赖 `tool_choice` 强制保证 | 原私有 items/opaque、显式能力和已知工具限制 |
| Claude Messages | `tool_result` + `tool_use_id` | 已知工具失败可按可信语义设置原生 `is_error`；不是仅按正文猜测 | thinking/signature、server tool 和引用、合法内容块顺序 |
| Gemini GenerateContent | `functionResponse` + 匹配调用 | 已知失败研究采用 `error`/结构化结果；当前全部塞 output 不等于错误已丢失 | functionCall/Response、thoughtSignature、原生事件、原 profile |

Gemini **不是 Responses API**。共享异常/结果分类不能引用 Responses 专属 item 格式；适配器按真实协议编码。两次 malformed 纠正仅在 Gemini 已确认未执行的本地形状使用，不能扩展成所有 Provider 的两次无条件重发。Claude 的 server tool、Gemini 的 native event、Responses 的原生事件不能伪装成本地效果。

H01/H06 的协议投影可以利用各协议已有失败表达，但不能统一改顶层 `ok` 后丢失命令局部效果或未知状态。新增或变更请求语义要检查合法新链/合同 revision 及旧 Work 恢复；已提交 checkpoint 不原地改写。

- [ ] 单轮多个调用：前置拒绝、读成功、写失败、pending/unknown 混合，结果各按原 call ID 配对，完成顺序不改变请求顺序。
- [ ] 当前同一事实经短结果、长结果/artifact 和恢复后回执，执行状态与读取权限不变；Code Mode 投影留待后续阶段。
- [ ] 下一次真实 HTTP payload 的错误字段、纠正位置、固定声明和签名/opaque 保留符合各协议；不能仅检查 Python 对象或哈希。
- [ ] 已产生或可能产生原生效果、截断、空结果、认证失败和能力不支持分别处理；模型/工具根预算持续计量。
- [ ] 重复本地 call ID 与原生结果同一响应出现，或出现未声明原生事件时，不执行有歧义工具，也不丢失已付请求与真实原生状态；不因校验失败伪造无效果。
- [ ] Profile/权限/generation 变化走合法边界；测试不能跨协议复用不兼容私有状态。

## 5. 延后范围与必要扩展空间

**H03（MCP）移出本轮实施清单。** 派发后异常收口的历史研究证据保留在外部研究目录；不继续扩展 MCP 调度、注册、schema、wrapper 或恢复架构，不为它建立新的整体验收工程。未知效果不能当确定失败重放仍是现行合同；若当前使用中出现真实事故，按原回执做定点处理，不能以“未来重构”为由重播效果。

Code Mode 本次只作只读设计参考：新同级参考提交 `d763b294…` 已包含主线 `f508f649…`，此前列出的 Gemini 纠正、写操作反馈、记忆反馈例外、journal 分类等缺失已从源码确认补入，撤下旧待办；这不是分支整体验收或上线声明。

仅保留四条扩展原则，不预先实现：

1. 领域执行结果与 Provider 编码分开，公共层不引用某个协议的专属格式。
2. 原身份、授权、累计预算、回执及其 owner 可继续复用，不建立第二效果账本。
3. 共用 schema 和反馈语义，当前实现不绑旧 Runner 的内部结构，未来也不机械移植旧 batch 标记。
4. 本轮不加入 VM、hook、Code Mode 迁移或第二循环；测试分支继续自己的测试，不修改或合并它。

VM 输出截断、父子投影、script retry 等旧候选保留在研究材料，不进入当前待办或要求当前代码兼容未上线的全部实现细节。

## 6. 待重放问题，不能先写成待修 Bug（C）

- [x] 已离线重放失败后 70 次读取及压缩来源：旧确定失败退出近期 64/32 展示窗口，但原 effect key 的持久回执和首轮压缩原工具 record 仍在。refs 只核验来源，不能证明模型结论正确；未确认 Host 丢证据，不为此全量加载历史或新增摘要判真机制。真实压缩误报仍须对应具体响应取证。
- [x] 已经真实 Runner/HTTP 重放同 Work 一次 final 纠正后发生实质修改、随后再次内部 final：现有整 Work 一次政策仍收束，原成功回执、计量与结果保留。这是已验证的政策范围，未作为丢事实 Bug；未自动放宽或要求每阶段报告，后续是否调整按产品策略处理。
- [x] 修 H01 后已用真实 Runner/HTTP 重放重复缺必填字段：下一次请求含具体字段/原因，派发为零，原尝试预算保留；仍重复相同错误时按原 no-progress 收束。该样本不能再归因于缺少纠正反馈，未知/写操作仍不得重播。
- [x] 已复现 `WorkNoProgress` 的三个已有原因在分类中丢失；已通过原 failure diagnostics 与暂停通知保留“开始未送达”“缺少明确退出”“相同工具结果”的安全说明，未知异常文本不透传。隔离 SQLite 原 Work/通知回归通过，没有新增暂停状态、表或重试资格。
- [x] Person automation 的明确通知缺口已通过真实入口重放并修复：沿原获准 `delivery_target` 给 root Person scheduled 的未派发非空 final 一次纠正，私聊保留原创建者目标；none/旧来源缺策略/内部返回/child/SELF 不因此群发，已尝试/未知不盲重发。14 项专项通过，包含先提出 complete 后显式发送；原外围送达核验保留。
- [x] Responses 已离线 HTTP 核对合法旧调用跨 80 条记录仍在完整 ordered input。人工孤儿输出可被适配器序列化，但未找到真实入口生成该孤儿，未加新的永久 guard。当前 OpenAI 使用 `store=false`，不使用 `previous_response_id`；不能仅因近期页没有调用就拒绝合法旧回执，也不能把人工局部 fixture 写成线上事故。
- [ ] 请求诊断缺样的具体原因。保留小型阶段/计量/覆盖元数据，离线构建实际 wire；不要为每轮抓完整敏感正文而增加在线编码、I/O 或 writer 竞争，更不能让诊断成为恢复事实源。

条件成立才提出对应最小修复，并说明删除/复用的机制、真实调用者和退出条件。不得把这些候选直接编码成永久 guard 或新状态。

## 7. 报告可信度与模型判别验收

Work `completed`、文件发布成功、发送确认和内容语义正确是四件事。完成控制继续核验原目标的已登记客观要求；不承诺通过关键词门禁保证任意自然语言报告真实。

- [ ] 固定失败样本：pytest 缺失/127；测试真实失败；pending 无输出；输出轮转；探测 fallback 退出 0；先写文件再退出非零。
- [ ] 各模型在相同原始事实、工具合同、顺序和预算下比较现有反馈与清晰反馈；先排除 Host 丢信息、协议转换、压缩和网关差异，再比较模型证据解释。
- [ ] 正确报告区分未运行、运行失败、运行中、输出不完整、通过；成功探测/文件存在/报告已发送都不单独证明测试通过。
- [ ] 确认反馈已正确可见而模型仍误报，记录模型质量问题与适用范围，不靠删校验、换 Provider 暗兜底或另一总结模型遮掩。
- [ ] 若用户要求真实验证，使用隔离/明确授权环境，限制请求和副作用；样本标明实际 profile、protocol、revision、usage 与覆盖范围，不能用简单短问答承诺长任务整体可靠率。

## 8. 实施顺序与验收范围

1. 先处理当前模式 H01/H02/H04/H05/H10 的错误、身份与恢复正确性；复用共同执行和结果语义，协议差异在适配层验证。H03/MCP 和 Code Mode 不进入本轮实施。
2. 处理 H06/H09，再补 H07/H08/H11 的非阻断机会。修改现行文档时直接替换过时规则，不叠加相反说明。
3. 跑直接相关当前 Host/协议 HTTP/SQLite 回归，并依下节检查数据库防回归；MCP 沿已有保护，不开展其架构或 Code Mode 集成测试，不机械重复无关全量检查。
4. 对第 6 节候选做专门重放，成立才进入实现。独立真实模型验收与自然聊天/长任务验收分别记录。
5. 用户已授权按本任务书修复并沿原流程交付；定向回归、审查和 CI 通过后推进提交、PR、合并、Bot-only 部署与隔离真实验证，按实际证据分别报告。不创建正式版本 Release，不迁移 MCP/Code Mode。

测试维度按当前改动风险组合，而非穷举所有笛卡尔积：五类协议；chat/accepted Work/SELF/child/automation/内部返回；短/长/截断/opaque/native；未执行/failed/pending/unknown/已确认；同轮/新输入/压缩/重启/权限变化。每个修复有直接相关行为回归，入口/持久化变化才增加必要的真实入口和竞争验证，不给无关小改动机械加整套测试。

必须保持：普通聊天不强制 Work；已提交请求合同不暗改，版本更新可按明确边界演进；身份/来源不重建；原操作和已计量预算不重置；已确认发送不重发；unknown 不默认失败；原生能力不伪装本地工具；生成正文不自动外发；已完成文件不因修复回退丢失。

## 9. 数据库防回归：保持已有修复，验证实际改动

依据现行[开发约束第 6 节](development-contract.md#6-sqlite-与诊断)。下面是本轮实现及验收要求，不是在任务书阶段已经完成的性能优化。旧 PR 的测试或现场数字仅说明当时样本，不作为本轮通过或当前线上延迟的证明。

### 9.1 已有修复与本轮关联

| 历史修复 | 已修复的路径 | 本轮应保持的行为 |
| --- | --- | --- |
| [#213](https://github.com/YuanYeYouTao/Yuki/pull/213)、[#229](https://github.com/YuanYeYouTao/Yuki/pull/229) | 有界异步诊断、写前准备、一致快照、原回执恢复，以及实际请求容量与整理软水位分开 | 反馈不追加同步诊断等待；请求能装入时不等软整理；数据库重备不重做模型或外部效果 |
| [#232](https://github.com/YuanYeYouTao/Yuki/pull/232) | artifact actor 按实际归档需求解析；小结果避免多余身份查询 | 错误/终端短结果不额外查身份或递归归档；展示失败不推翻已确认发送 |
| [#236](https://github.com/YuanYeYouTao/Yuki/pull/236)、[#238](https://github.com/YuanYeYouTao/Yuki/pull/238)、[#244](https://github.com/YuanYeYouTao/Yuki/pull/244) | 有界维护、准备复用、空闲/无变化路径减少 writer，启动空探测保持只读 | 不因提醒、校验或恢复探测重新增加空写入；真实变更仍使用原条件更新和原子发布 |
| [#239](https://github.com/YuanYeYouTao/Yuki/pull/239) | 普通聊天无 Work 的 source guard 从无变化更新改为一致只读核验 | 不恢复 `UPDATE fence=fence`；真正写入的 owner/source/lease 围栏继续保留 |
| [#241](https://github.com/YuanYeYouTao/Yuki/pull/241) | 诊断核验与编码移出回复关键路径，同视图准备与渲染复用，冷入站外部探测释放连接后执行 | 不为每个反馈再读来源、重复编码整段历史；模型响应和名额释放不等可丢诊断 writer |
| [#242](https://github.com/YuanYeYouTao/Yuki/pull/242) | 私聊原任务抢占/取消在会话 admission 等待前完成，空轮询使用待处理输入索引 | 不扩大串行会话锁范围；取消先完成原任务/传输清理，保留发送与原生效果保护窗口；空恢复不扫描全历史 |
| [#245](https://github.com/YuanYeYouTao/Yuki/pull/245) | 配置和 owner 同视图复用、仅补缺失历史正文、社交来源覆盖索引 | 新反馈不全量重载冻结正文；实际来源查询继续走当前合适索引，不靠时间或平台号重建身份 |

### 9.2 按修复位置控制新增工作

- [ ] **H01/H02/H06/H11：优先零新增数据库查询和 writer。** 字段错误来自本次冻结 schema；执行状态、exit、pending、未知及发送阶段来自原调用和 typed result。不要为错误格式化再查 owner/source、读取整段 stdout 或写错误台账。若确实需要额外访问，说明缺失事实、最小查询、owner 与必要性后再设计，不把“零查询”变成阻碍正确性的永久禁令。
- [ ] **H07/H08：先用原批次结果及已有获准上下文计算机会。** 提醒水位继续并入原 dispatched journal 的 `communication_updates`，不为每个阶段、每条结果另开事务或独立提交提醒。保存失败时，不提前推进已观察/已提醒水位；既有 checkpoint 与提醒变更保持原子性。
- [ ] **H08：查送达见证要正确且有界。** 限定原 Work、输入事件与合法目标，读取必要的小型状态和原回执关联。当前“每输入取首个匹配结果”的失败见证不能替代独立成功见证；必须覆盖先失败后成功、未知后确认、多次尝试及迟到回执。使用索引存在性/定向查询或完整有界续页，不能把截断页当精确完整证据，也不能全量读取旧 effect 正文找成功。
- [ ] **H10：只修错误码与恢复语义。** 不顺便新增续租、来源查询、状态提交或全局重试器。BUSY/合法扩展码的安全重备使用新事务、原执行身份和原事实；LOCKED 与内部故障单列。提交确认未知先核对原操作，不能借重排再次调用模型、重发消息或重跑终端。
- [ ] **H04/H05 与协议投影：复用现有请求检查点和效果 owner。** 重复调用 ID、原生空正文或协议错误不建立新事件账本；已付请求、真实原生状态与未知效果不能当可丢诊断。恢复增加持久事实时说明为什么原检查点不足，避免为了统一格式另建并行协议。

### 9.3 查询、事务、连接与锁边界

- [ ] 历史扫描、证据聚合、大正文转换、协议/媒体准备与外部 I/O 在首次 DML/`BEGIN IMMEDIATE` 前完成，并检查 autoflush。writer 内仅保留必要当前状态复核和原子变更；必要小型 JSON/DTO 用实际数据量、次数和持锁成本判断，不为机械外移引入新状态。
- [ ] 只读准备使用一致快照或完整依赖版本；真正发布仍复核 owner、来源、权限、generation、revision 与 SQL 执行时的有效租约。不得为减少等待删除这些检查，也不得用提前读取的租约或哈希替代它们。
- [ ] 无变化路径只读返回；维护使用索引、有界候选和写时复核。沿用当前配置与查询语义，不把旧页大小写成永久规定；精确证据必须完整，分页不拆坏原子提交或遗漏对象。本轮不以新增表、迁移、加长 timeout 或扩大重试次数作为默认解决办法。
- [ ] 有必要新增/改变查询时，记录实际 ORM SQL 形状、内部 ID/owner 范围、投影列、索引与 `EXPLAIN QUERY PLAN`；显式分页/`LIMIT`，时间仅在确定范围内过滤。参数、正文与账号身份不进入诊断。索引存在不等于实际使用，仍需核对真实计划与读取量。
- [ ] 不把普通聊天强制登记 Work，也不为每个结果增加独立 commit。账本、预算、journal、权限核验和效果回执保持持久性；可丢诊断从准备到提交继续有界异步，与业务结果隔离。
- [ ] 会话互斥、Provider admission、连接取得、SQL 执行、commit/rollback、连接/线程清理、实际 writer 持有和上游耗时分别解释。长 preparation 差额不能冒充 SQLite 锁等待；换页/I/O 压力只在有同期证据时归因。
- [ ] 同步数据库工作隔离覆盖完整生命周期；取消时等待真实线程、连接和事务清理后再释放相关锁/租约。不要只在线程中执行 SQL，却把提交、关闭或失败清理留在事件循环。

### 9.4 定向验收与证据

按真正修改的路径选择既有回归并补缺口，不为错误文案机械重跑全部数据库/迁移测试。

| 触及的路径 | 相关既有回归 | 必须保留或补测的行为 |
| --- | --- | --- |
| 来源核验、等待与恢复分类 | `test_work_source_guard_readonly.py`、`test_lease_heartbeat.py`、`test_runtime_recovery.py`、`test_work_journal_source_retry.py` | 另一 WAL writer 持有时只读核验可完成；writer 等待后租约过期不复活；来源变化拒绝旧提交；BUSY_SNAPSHOT 同 ID 新事务纯数据库重备，LOCKED 不冒充 BUSY |
| 汇报、steer 与 journal | `test_work_reporting_runner.py`、`test_work_communication.py`、`test_work_feedback_cursor.py`、`test_work_boundary_dispatched_save.py` | 同输入先失败后确认成功；未知不重发；保存失败不推进水位；重启不重复已确认发送；提醒与原 journal 一起提交或回滚 |
| typed result 与展示 | `test_work_effect_results.py`、`test_tool_effect_audit.py`、`test_plugin_result_access.py` | 大/小结果同事实；归档/展示失败保留原执行与已确认效果；未派发、执行失败、未知有别；没有额外身份查询 |
| 上下文准备、历史和来源查询 | `test_private_dispatch_source_reads.py`、`test_projection_source_reads.py`、`test_history_missing_body.py`、`test_social_source_lookup_index.py` | 同视图复用；只补缺失正文；冻结 warm 输入不全量重读；变化来源仍阻止旧发布；改动查询使用实际索引计划 |
| 诊断、取消与私聊 admission | `test_diagnostic_preparation_lifecycle.py`、`test_runtime_diagnostic_phase_boundaries.py`、`test_private_turn_preemption.py` | 编码池饱和不阻塞下一请求；成功响应不等诊断；取消清理完成再放行；真实发送/原生效果窗口不被抢占 |
| 若改动恢复发现查询 | `test_work_abandoned_input_index.py` | 实际空闲 ORM 查询和 SQLite VM 指令量有界；持 writer 时空读取可结束；发现后写时复核；提交丢确认不重播 |

- [ ] 同一离线 fixture 对比修改前后 session/SELECT/DML/`BEGIN IMMEDIATE`/commit 数、实际查询计划及正文读取量。只保留脱敏 SQL 形状、计数和固定小型阶段值；不增加永久观测设施来完成一次验证。
- [ ] 结果字段/文案修复不增加回复路径 writer；涉及准备复用的变化不增加整份历史读取。存在必要成本变化时说明事实和代价，而不是只用耗时偶然下降证明正确。
- [ ] 使用相关真实 SQLite 竞争与取消回归验证新增持久化/恢复行为；确定性行为和计数优先，不把机器特定毫秒数写成架构门禁。
- [ ] 后续真实验证分别记录版本、样本范围、连接/SQL/提交/会话 admission/模型与首条送达时间。自然流量只按可信内部 ID 有界取新增必要元数据，不启动第二个主动 Bot 写生产库，不用反复全库审计给负载增加压力。

本轮已运行相关真实 SQLite 竞争、原输入/效果恢复、现有索引计划及小型投影回归；交叉审查补齐了实际 SQLite VM 工作量、无排序见证与临派发新输入竞态；早命中可早停，晚命中/不存在仍须检查原 Work 索引范围。部署与真实 API/自然流量验收尚未完成。不能从局部 helper 或历史 PR 的测试次数宣称本轮已经降低延迟。

## 10. 研究材料与复核

本地脱敏研究材料位于 `C:/Users/bymay/Documents/Codex/yuki-harness-research-20261006/`，不提交原私聊、凭据或生产完整 payload：

- `harness-feedback-replay.py/json`、`harness-broad-replay.py/json`：阶段机会、输入关联、未执行状态、恢复分类。
- `gemini-result-projection-replay.py/json`、`protocol-result-replay.py/json`：原方法局部投影及五协议负向条件。
- `mcp-uncertainty-replay.py/json`：派发前/后异常与 typed evidence。
- `pi-receipt-replay.py/json`、`audit-codemode-broad.md`：旧 `0dc4dbcd…` 的测试分支投影和差异，属于历史材料；当前合并状态以新同级参考 `d763b294…` 的源码复核为准，不转成移植待办。
- `database-pr-history.json`、`db-guard-current-harness.txt`：本轮历史 PR 与当前反馈路径的数据库防回归审查，未替代修复后测试。
- `model-feedback-evidence.json`：原内部 ID 有界查询、失败回执保留及新输出误报的精简标志。

这些脚本是外部研究 fixture，不是源码修复或全链验收。未来可把有价值的场景改写成正式定向回归，不能把 AST/helper 重放的通过替代真实模块、HTTP、崩溃恢复和自然流量证据。

参考 pi 仅借用反馈与循环设计：[Google 错误投影](https://github.com/earendil-works/pi/blob/428a12bc775145afa342530a9eaa652efb3e4422/packages/ai/src/api/google-shared.ts#L300)、[Durable 工具结果与诊断提交](https://github.com/earendil-works/pi/blob/9fba660cf1caca0ade5bea72269352416e595a19/packages/durable/src/harness/tool.ts#L435)。Pi TUI 工具事件可见不等于 QQ 已送达，`agent_end` 不证明目标完成；动态工具、内存队列和泛化 safe replay 不替代 Yuki 的固定合同、持久输入与原效果回执。
