# 生命周期控制调用走冻结 schema 校验实施记录（2026-11-08）

来源：[B5 下游重复校验删减记录](bloat-b5-20261108.md) §2 与“已知风险与后续”；任务书 [文件膨胀与函数爆炸删减任务书](../architecture/Yuki-文件膨胀与函数爆炸删减任务书-2026-11-07.md) §4.4。基线 main `b6c2ec68`。

状态：本地修改、验证（§5）、提交并开 PR；**未合并、未上线**。

目标：五个生命周期控制工具（`get_tasks`、`manage_task`、`wait`、`start_subtask`、`message_task`）的参数与业务工具一样，用部署冻结合同已编译的 `MainAgentContract.schema_view` 校验，失败形状与业务工具相同（`{ok:false, error:"tool_input_validation_failed", message:<一句中文>, executed:false}`）；再删除 `work_control.py`、`subagent_tools.py`、`work_wait.normalize_conditions` 中被 schema 对每个调用方都覆盖的手写形状检查。不新增校验器或机制，不改权限与授权。

## 1. 改代码前列出的可能失败

### 1.1 入口与次序

- F1 汇合点：直调（`_execute_tool_batch` 的写控制分支与只读 `get_tasks` 分支）、Code Mode 控制子调用（`CodeHost.execute_control`）和恢复重派都经 `AgentRunner._execute_control_call`。若校验放在任何一个分支而不是这里，另两条仍无校验。
- F2 回执先于校验：直调和恢复经 `InvocationService.invoke`，它在调用 `execute` 前按原 effect key 返回已有回执（`prepare_effect` 失败且非未派发意图 → `journal.effect_result`）。若校验放在 `invoke` 之前，已有回执的调用会被当前 schema 重新校验，返回校验失败而不是原回执——违反“已有回执只返回原结果”。校验必须放在交给 `invoke` 的 `execute` 闭包内。
- F3 Code Mode 子调用不传 `invocation`：`CodeHost._dispatch_control` 自己 `admit_dispatch`（已派发或已结算时取原回执，不再调用 `execute_control`），再调 `_execute_control_call(..., invocation=None)` 直接执行闭包。校验放在同一闭包内即对子调用生效，原回执路径不受影响。
- F4 typed outcome：`InvocationService.invoke` 要求现场执行发布 typed outcome（`capture.outcome`），否则抛 `TypeError("live tool execution did not publish a typed outcome")`。校验失败的结果必须像业务拒绝一样写入当前 `ResultCapture`。
- F5 授权与可用性先于 schema：`work_control_unavailable`（未声明、无 WorkControl、后端不允许）与 `work_query_not_authorized`（只读控制的执行处授权）保持在 schema 之前且不写回执，与现状相同；不因加校验而放宽或收紧。
- F6 原 JSON 对象检查（`invalid_work_arguments`）在派发前返回、不写回执；冻结 schema 同样拒绝非对象与坏 JSON（“参数必须是 JSON 对象。”）。改为在闭包内由 schema 拒绝后，直调会经 `invoke` 写一份未执行的回执（与业务工具对坏 JSON 的处理相同）。
- F7 合同来源：`schema_view` 只在 `MainAgentContract.definitions()` 冻结后存在。生产中 Provider 请求前必先冻结；测试中直接给 `fixed_tools`、不冻结合同或用 `SimpleNamespace` 代替合同的夹具会在校验处缺属性。按冻结合同修夹具，不在生产代码加“没有合同就跳过”的分支。
- F8 Host 直调：`WorkControl.admit_goal`（Host 自动接纳）不经 `execute`，不做 schema 校验；它的 `goal` 来自 Host 外部输入，`_admit_goal` 的“非空字符串”检查必须保留。`admit_execution` 的目标由 Host 构造，不受影响。
- F9 测试与 Host 代码直接调用 `WorkControl.execute`：不经 schema。删除形状检查后，这些调用方传畸形参数得到的是后续步骤的异常或事实拒绝；生产代码中除 `_execute_control_call` 外没有调用 `execute` 的位置（须 grep 核实）。

### 1.2 恢复

- F10 切换前的旧 journal（#583 之前的旧名或旧形状参数、只读映射 `RECORDED_TOOL_NAMES`）：合同 revision 是冻结清单（含参数 schema）的哈希，旧链在 `WorkJournal._load` 判为 `contract_changed`，未配对调用只按原 effect key 审计（`replay_forbidden`），不重派，不经 `_execute_control_call`。本批若改动控制 schema，revision 再变一次，同理。
- F11 同一合同下的未决控制调用（只有 T1 意图、没有回执）在恢复时重派：它是在当前合同下由模型给出的参数，按当前 schema 校验是一致的；校验失败写一份未执行回执，不扣预算、不改 Work 状态。
- F12 已有回执（accepted/unknown）的控制调用：按原 effect key 返回原回执，不重新校验、不重派；包括切换前写下的控制拒绝回执（原领域错误码文本保持原文）。
- F13 Code Mode 恢复：已配对的子调用读原回执；`code_stop` 旧回执保持原事实。校验只影响此后新派发的子调用。

### 1.3 批次与预算

- F14 同一响应混合合法与不合法的控制调用：控制是有序屏障，`_execute_tool_batch` 把批次切成单个控制段逐个执行。前一个控制校验失败不阻止后一个合法控制执行（与业务工具一项失败不撤销其余调用一致）；校验失败不设 `ending`、不接纳 accept/resume。
- F15 #577 的接纳：`TurnExecution.admit_private_execution` 只看名称是否属于 `WORK_CONTROL_NAMES`，以及 `manage_task` 的 `action` 是否为 start/resume（`_needs_neutral_foreground`，只读 JSON 预判，不执行）。`manage_task(action=start, goal=123)` 仍走模型自己的控制路径而不接纳 Host Work，随后被 schema 拒绝——与改前被 `_admit_goal` 以 `work_goal_required` 拒绝时的接纳决定相同；`action` 非法时仍按 Host 接纳，随后控制被 schema 拒绝，与改前 `task_action_invalid` 时相同。
- F16 预算：业务工具的校验失败发生在 `invoke` 的 T2（`admit_dispatch(charge=counts_toward_business_limit)`）之后，回执记录未执行，协调器不把它计入本批执行数。控制调用在 `invoke` 中恒以 `charge=False` 准入（Code Mode 子调用同样 `charge=False`），因此校验失败不消耗业务工具预算，与合法控制相同；模型请求预算在请求时已付，不因结果退款或加扣。批次 `executed_count` 对控制段恒为 0。
- F17 `executed` 标志：`_execute_control_call` 返回的布尔值在 `invoke` 路径恒为 True（与改前领域拒绝相同），只在授权/可用性拒绝时为 False；校验失败的 typed outcome 自身带 `executed=false`、`mutation_committed=false`，回执证据以它为准。

### 1.4 模型可见与外部消费者

- F18 每个控制工具的错误码与文本会变化：形状错误从领域码（`wait_for_invalid`、`invalid_wait_delay`（非数字时）、`invalid_wait_plugin_filter`、`invalid_task_query`、`task_id_required`、`task_action_invalid`、`invalid_task_text`、`work_result_invalid`、`invalid_message_arguments`、`invalid_work_arguments`）统一为 `tool_input_validation_failed` 加一句说明参数和期望。依赖事实的错误码不变。
- F19 ACP、Client API、Desktop、提示词、插件是否读取上述旧码：须全仓 grep（含 `clients/`、`packages/`、`plugins/`、`docs/`）。
- F20 错误先后：多处错误同时存在时，schema 先报第一处形状错误，事实错误（例如 `no_active_work`）只在形状通过后出现；改前某些输入先报事实错误（如 `wait` 无当前任务时先报 `no_active_work`，现在先报形状）。属模型可见变化，记录。
- F21 JSON Schema 语义与 Python 类型不完全一致：`integer` 接受 `5.0`，`number` 接受 Python `json.loads` 解析出的 `Infinity`/`NaN`，二者都排除布尔。依赖这些差异的手写检查不是重复：`seconds` 的有限性必须保留；`limit=5.0` 须确认下游 `WorkQueries` 自己的整数检查仍拒绝而不是崩溃。
- F22 schema 未声明的手写限制：`plugin_event` 的 `plugin_id`、`event_type` 1–128 字与 `source` ≤128 字；`goal`、`text` 的“非空白”；`id` 的“非空”。逐项判定是否真实：真实的移入声明，任意的删除。
- F23 schema 变更改变合同 revision（F10），模型看到的 `wait` 声明随之变化。

### 1.5 测试

- F24 断言旧码或依赖 `execute` 自行拒绝畸形参数的测试：`test_work_tool_surface_e2e::test_wait_for_subtask_or_message_or_plugin_event`（直调 `execute` 传畸形 `for` 项）、`test_work_settlement_writer`（`normalize_conditions` 以 `True` 作为非法 seconds）、`test_tool_surface_prep2_e2e`（Host `admit_goal` 空白 goal，保留）。
- F25 原生 Code Mode（`native_worker`，只在 CI）：`codemode_runner_helpers.runner_env` 用 `SimpleNamespace` 代替合同且经真实 `_execute_control_call`，须补 `schema_view`；`codemode_cases.build_host` 直接调 `control.execute`，不经 `_execute_control_call`，只用合法参数。逐个读原生用例，确认没有断言旧控制错误码。

## 2. 核实结论（对应 §1）

- F1–F4：校验放在 `_execute_control_call` 交给 `InvocationService.invoke` 的 `execute` 闭包内（Code Mode 子调用无 `invocation` 时直接执行同一闭包）。失败经 `_unexecuted_tool_result` 写入当前 `ResultCapture`，模型投影与业务工具 `_refused_result` 相同的四个键 `{ok, error, message, executed}`；typed outcome 带 `mutation_committed=false`。
- F5、F6：`work_control_unavailable` 与 `work_query_not_authorized` 不变；原 `invalid_work_arguments` 删除，坏 JSON 与非对象由 schema 返回“参数必须是 JSON 对象。”，直调经 `invoke` 写一份未执行回执（同业务工具）。
- F7：生产中 `TurnExecution.activate` 在会话恢复前用 `model_definitions()` 冻结合同；子任务的 `fixed_tools` 来自 `main_agent_contract.definitions`；插件 `fixed_tools=()` 没有声明任何工具，在 schema 前就以 `work_control_unavailable` 返回。只修测试夹具：`work_runner_helpers.case`、`test_work_query_authority` 夹具与 `test_soul_work_recovery` 替换合同后各调用一次 `definitions()`；原生 Code Mode 的 `codemode_runner_helpers.runner_env` 给 `SimpleNamespace` 合同补 `schema_view`（由该夹具声明的 `FIXED_TOOLS` 编译）。
- F8：`admit_goal` → `_admit_goal` 的“goal 为非空白字符串”检查保留（Host 输入，且 start 无 goal 是跨字段规则）。
- F9：`src` 中 `WorkControl.execute` 的调用方只有 `_execute_control_call`。
- F10：`WorkJournal._load` 在合同 revision 变化时返回 `contract_changed`，未配对调用只审计、`replay_forbidden`；本批给 `plugin_event` 加了长度声明，revision 随之改变，上线前未结算的 journal 同样只审计，不按新 schema 重派。
- F11–F12：`_resume_tool_response` 对已配对或已有回执（且非未派发意图）的调用走 `recorded()` 直接读原回执，不进入 `_execute_tool_batch`；只有 `effect_state is None` 或未派发意图的调用重派，并在闭包内按当前 schema 校验。E2E `recovery-replay` 证明：改写成旧领域码文本的回执原样配对，恢复时只校验两个未派发的 `manage_task`。
- F13：Code Mode 已配对子调用读原回执（`CodeHost._dispatch_control` 的 `admit_dispatch` 失败分支），本批不改 `codemode/`。
- F14：控制屏障逐个执行；E2E `invalid-controls` 中七个非法控制之后的 `update`、`finish` 仍按原顺序执行。
- F15：`_needs_neutral_foreground` 只做接纳路由的预判，决定与改前相同（§1.3）。
- F16、F17：E2E 中七个拒绝前后 Work `tool_calls` 均为 0；控制段 `executed_count` 恒为 0。业务工具的校验失败在 T2 已按 `counts_toward_business_limit` 记一次准入、协调器不计入本批执行数；控制调用恒 `charge=False`，因此“校验失败不额外扣预算”对两者一致，差别只来自控制本来不计业务预算。
- F19：全仓 grep（`src`、`packages`、`plugins`、`clients`、`docs`、`tests`）旧码只出现在 `work_control.py`、`subagent_tools.py`、`work_wait.py`、日期文档与两个测试；ACP、Client API、Desktop、提示词与插件都不读取这些码。
- F21：`limit=5.0` 通过 schema（JSON Schema 的整数），由 `WorkQueries` 原有 `type(limit) is not int` 检查以 `invalid_work_query_limit` 拒绝，不会崩溃；`seconds` 为 `Infinity`/`NaN` 时仍由 `invalid_wait_delay` 拒绝。
- F24、F25：见 §4。原生用例中经真实 `_execute_control_call` 的只有 `runner_env` 一处（已补 `schema_view`），`codemode_cases.build_host` 直接调 `control.execute` 且只用合法参数；没有原生用例断言旧控制错误码。

## 3. 删除与保留

### 3.1 删除（schema 对每个模型调用方都已保证）

| 位置 | 删除的检查（原错误码） | 覆盖它的声明 |
| --- | --- | --- |
| `agent_runner._execute_control_call` | 参数须为 JSON 对象（`invalid_work_arguments`） | schema 校验器的 JSON 对象检查 |
| `WorkControl._get_tasks` | `limit` 为 1–50 的整数、`cursor` 为非空字符串、`status` 枚举、`parent` 为字符串（`invalid_task_query`） | `limit` integer 1–50、`cursor`/`parent` string、`status` enum；`limit=5.0` 由 `WorkQueries` 拒绝，空 `cursor` 由游标解析以 `invalid_work_query_cursor`/`invalid_work_progress_cursor` 拒绝 |
| 同上 | `id` 为非空白、≤36 字的字符串（`task_id_required`） | `id` string maxLength 36；空或空白 id 查不到任务，返回 `work_not_found_or_not_authorized` |
| `WorkControl._manage` | `action` 枚举（`task_action_invalid`） | `action` enum，required |
| 同上 | `id` 为非空字符串（`task_id_required`） | `id` string；空 id 查不到任务，返回 `work_not_found_or_not_authorized` 或 `resume_work_not_authorized` |
| 同上 | `text` 为字符串（`invalid_task_text`）；`result` 为字符串（`work_result_invalid`）；update 的 `goal` 为字符串 | `text`、`result`、`goal` string |
| `WorkControl._wait` | `all` 为布尔、`deadline` 为字符串（`wait_for_invalid`） | `all` boolean、`deadline` string |
| `_wait_conditions` | `for` 为非空数组、每项恰一个键、`message` 为 true、`seconds` 为数字、`plugin_event` 为对象且无多余键、未知键（`wait_for_invalid`、`invalid_wait_delay`、`invalid_wait_plugin_filter`） | `for` minItems 1，`items.oneOf` 五种单键对象，各自 `additionalProperties:false`，`message` const true，`seconds` number |
| `work_wait.normalize_conditions` | 非空列表、项为对象、`at`/`after_seconds` 二选一、`at` 为字符串、`seconds` 为非布尔数字、`conversation` 无多余键、`plugin_event` 字段类型与多余键、`owned_run` 的 `run_id` 为非空字符串与多余键、未知 kind（`wait_conditions_required`、`invalid_wait_condition`、`wait_time_requires_at_or_after`、`invalid_wait_time`（类型）、`invalid_wait_delay`（类型）、`invalid_wait_plugin_filter`、`invalid_wait_run`、`unknown_wait_condition`） | 唯一调用方 `_wait` 与其 `register` 只传 `_wait_conditions` 由 schema 已校验的项构造的条件；`run` minLength 1 |
| `subagent_tools.message_task` | `text` 为非空白字符串（`message_text_required`） | `text` string minLength 1；空白正文由 `SubagentRepository.message` 原有检查以 `invalid_subagent_message` 拒绝（中文说明从旧码移到此码） |
| 同上 | `ask` 为布尔、`reply_to` 为字符串（`invalid_message_arguments`） | `ask` boolean、`reply_to` string |
| `subagent_tools.owned_child_parent` | `identity` 为非空字符串（`task_id_required`） | 调用方传入的 id 来自 schema string 或已查到的任务；空 id 查不到子任务，返回 `subtask_not_owned` |
| `_ERROR_MESSAGES` | 上述不再产生的码的中文说明 | — |

### 3.2 移入声明（真实限制，原先只在隐藏检查中）

| 限制 | 判定 | 处理 |
| --- | --- | --- |
| `plugin_event.plugin_id`、`event_type` 1–128 字，`source` ≤128 字 | 真实：插件 id 由 manifest 限 ≤128，SDK 发布事件的 `event_type` 为 1–128 字；超出或为空的 `event_type` 永远等不到匹配事件，没有 `deadline` 的等待会一直挂起 | 写入 `wait` 的声明 schema（`minLength`/`maxLength`），删除隐藏检查；模型从声明看到限制 |

### 3.3 删除（任意限制）

逐项核对后没有只存在于手写检查、又属于任意限制的项：`message_task` 的“text 非空白”是领域层 `SubagentRepository.message` 同一规则的重复（列入 3.1），`plugin_event` 长度是真实限制（3.2）。

### 3.4 保留（依赖状态或事实，或 schema 无法表达）

| 位置 | 检查 | 原因 |
| --- | --- | --- |
| `_execute_control_call` | 声明、WorkControl、后端许可（`work_control_unavailable`）；`get_tasks` 读取授权（`work_query_not_authorized`） | 执行处授权与可用性 |
| `WorkControl.execute` | `validate()`、lease 有效（`work_activation_obsolete`）；未知名称（`unknown_work_control`） | 当前租约事实；名称分派 |
| `_manage` | 无当前任务、子任务登记、resume 需要中性前台、目标存在与授权、子任务归属、目标状态 | 当前 Work 状态与归属 |
| `_manage` update / `_admit_goal` / `_queue_work` | `goal` 非空白（`work_goal_required`）；start 必须给 goal | 内容规则与跨字段规则（schema 中 goal 不是必填，只在 start 时需要）；`_admit_goal` 还服务 Host `admit_goal` 的外部输入 |
| `_manage` resume | 未给 `id`（`resume_work_not_authorized`） | 跨字段规则 |
| `_wait` | 无当前任务；`run` 必须是自己的子任务或命令（`waiting_requires_owned_execution`） | 当前状态与归属 |
| `normalize_conditions` / `_timestamp` | `at`、`deadline` 可解析且带时区（`invalid_wait_time`、`wait_time_requires_timezone`）；`seconds` 有限（`invalid_wait_delay`） | schema 只声明字符串与数字；Python JSON 解析会产生 `Infinity`/`NaN` |
| `WorkWaitRepository.register` | 主体、Work 来源、插件后台授权、会话存在 | 当前事实与授权 |
| `validate_note` | pydantic `ContextNote` 解析与 refs 来源核验（`invalid_note` 等） | 声明 schema 由同一模型生成；refs 对当前来源核验 |
| `message_task` | 子任务中联系父任务时须有父任务（`subtask_parent_unavailable`）；子任务归属（`subtask_not_owned`） | 当前状态与归属 |
| `SubagentRepository.start` / `.message` | goal、text 非空白（`invalid_subagent_goal`、`invalid_subagent_message`） | 领域层规则，同时服务 Host 与 resume 路径 |
| `WorkQueries.list` / `.get` | `limit` 为 Python 整数 1–50（`invalid_work_query_limit`）；游标可解析 | JSON Schema 的 integer 接受 `5.0`（F21）；游标是本仓库生成的不透明值 |

## 4. 模型可见变化与测试改写

### 4.1 模型可见

| 工具 | 改前（形状错误） | 改后 |
| --- | --- | --- |
| 全部五个 | 非 JSON 对象：`invalid_work_arguments`，“调用没有执行。”，不写回执 | `tool_input_validation_failed`，“参数必须是 JSON 对象。”，写未执行回执 |
| `get_tasks` | `invalid_task_query`（limit/cursor/status/parent）、`task_id_required`（id 类型或过长） | `tool_input_validation_failed` + 一句说明（例：“参数 limit：不能小于 1”）；空 cursor 改为 `invalid_work_query_cursor`/`invalid_work_progress_cursor`，空白 id 改为 `work_not_found_or_not_authorized` |
| `manage_task` | `task_action_invalid`、`task_id_required`、`invalid_task_text`、`work_result_invalid`；`note` 中未声明的 `version` 等多余键曾被 pydantic 静默接受 | `tool_input_validation_failed` + 说明；`note` 多余键按声明拒绝（例：“参数 note：不接受参数 version”）；空 id 改为 `work_not_found_or_not_authorized`/`resume_work_not_authorized` |
| `wait` | `wait_for_invalid`、`invalid_wait_delay`（非数字）、`invalid_wait_plugin_filter` | `tool_input_validation_failed` + 说明（单键项不符时为“参数 for[0]：须且只能给出以下一种：run；message；seconds；at；plugin_event”）；`Infinity`/`NaN`、无时区或无法解析的时间、非自己的 run 仍为原领域码 |
| `wait` 声明 | `plugin_event` 的三个字段只有 `string` | `plugin_id`、`event_type` 为 `minLength 1, maxLength 128`，`source` 为 `maxLength 128`；合同 revision 随之改变 |
| `start_subtask` | 缺 goal 时由领域层 `invalid_subagent_goal` 拒绝 | `tool_input_validation_failed`，“缺少必填参数 goal”（空白 goal 仍为 `invalid_subagent_goal`） |
| `message_task` | `message_text_required`、`invalid_message_arguments`、`task_id_required` | `tool_input_validation_failed` + 说明；空白 text 为 `invalid_subagent_message`（中文说明“text 不能为空。”沿用），空 id 为 `subtask_not_owned` |

错误先后（F20）：形状错误先于一切事实错误报告，例如无当前任务时调用畸形 `wait` 先得到形状说明而不是 `no_active_work`。单键对象内部字段错误（如 `event_type` 超长）沿用校验器对 `oneOf` 的现有说明，只列可选键、不指出内部字段，与业务工具 `target` 等单键对象相同；本批不改校验器文案。

消费者：ACP、Client API、Desktop、提示词与插件都不读取上述旧码（F19）。

### 4.2 测试改写

| 测试 | 改写 | 原因 |
| --- | --- | --- |
| `tests/integration/test_control_call_schema_e2e.py`（新增） | 真实 Main 循环驱动五个控制的非法参数（含超长 `event_type` 与坏 JSON）、混合合法控制、恢复重派与 Code Mode 子入口；产物写入 `test-results/control-call-schema/` | §1 全部可直接驱动的情形 |
| `test_work_tool_surface_e2e::test_wait_for_subtask_or_message_or_plugin_event` | 直调 `execute` 的三个形状拒绝移到上面的 E2E；保留无时区 `at` 与他人 run 两个事实拒绝 | `execute` 不再自行校验形状（F9、F24） |
| `test_work_settlement_writer` | `normalize_conditions` 的非法值去掉 `True`，保留 `inf`、`nan` | 布尔由 schema 拒绝；有限性仍由领域检查 |
| `test_work_protocol_continuity::test_private_note_keeps_normal_resume_without_public_history_publication` | 模型脚本里的 `note` 去掉未声明的 `version` | 声明 schema 不含 `version`（#583），现在按声明拒绝 |
| `tests/support/work_runner_helpers.case`、`test_work_query_authority` 夹具、`test_soul_work_recovery`（替换合同后） | 调用一次 `main_contract.definitions()` 冻结合同 | F7 |
| `tests/support/codemode_runner_helpers.runner_env`（原生，CI） | `SimpleNamespace` 合同补 `schema_view` | F7、F25 |
