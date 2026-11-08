# 工具面一次切换（2026-11-07）

依据[文件膨胀与函数爆炸删减任务书](../architecture/Yuki-文件膨胀与函数爆炸删减任务书-2026-11-07.md) §6（作者已批准），承接[第一批准备](tool-surface-prep-20261107.md)与[第二批准备](tool-surface-prep2-20261107.md)。本次把模型工具面从 29 个一次切换到 27 个；不保留旧名适配层，旧名只出现在只读的历史映射里。权限设计、Code Mode 设计（#573）、Manager/Connector wire 与 Desktop 不改。

## 写代码前列出的可能失败情况

### A. 模型可见声明

1. 声明里仍出现旧名（`task_control`、`external_agent`、`query_chat_history` 等）或旧工具与新工具并存；目录、`DIRECT_TOOL_NAMES`、合同指纹与 provider 实际收到的列表不一致。
2. 删除的 `environment_service`、`get_my_capabilities`、`time_get_current` 仍被某个 provider 声明（自动化 provider、权限目录、沙箱工具组）。
3. 新工具参数与 §6.8 不符：残留 `kind/target_id`、`attachment_kind`、`view`、`operation`、`mode`、`offset`、`native_cursor`、`request_id`、`outcome`、`context_note.version` 等分派或别名参数。
4. `target` 不是单键对象：同时给两个键、空对象或未知键时未被 schema 拒绝。
5. `device` 与 `to_device` 方向反了（`copy_file` 旧参数 `device_id` 表示目标）。
6. 工具名冲突或 Code Mode 包装名冲突（`yuki_<name>`）；合同版本没有升级导致旧冻结清单被复用。

### B. 执行与权限

7. 改名后执行处的权限元数据（`_CORE_METADATA` 的 namespace/effect/risk）丢失，读工具被当成写工具（并行、缓存、`mutation_committed` 推断变化）或写工具被当成只读。
8. 合并工具（`find_contacts`+`find_conversations`、`view_file`、`get_tasks`、`search_memory`+`read_memory`）只搬运了其中一个来源的权限核验。
9. 记忆写入因选择器删除而新增或收紧授权（作者已放宽，不得加门）；或反过来放宽了真实来源核验（证据须为真实入站事件，SELF 须 `quote` 子串核验）。
10. 沙箱 RPC 方法名随模型名变化（Manager 只认 `terminal_exec` 等原方法），或任务行 `{"tool": …}` 的值变化导致原 request ID 幂等哈希变化。
11. 外部 Agent、终端的参数翻译把新参数名（`id`、`device`）直接写上 wire，Connector/Manager 不识别。
12. 新调用经历史映射被解析（例如模型调用旧名 `task_control` 被映射到 `manage_task` 执行）。旧名调用必须按未知工具失败。
13. `wait` 的条件少于 `work_wait` 现有全部条件（time_due、conversation、plugin_event、owned_run、deadline、any/all）。
14. `start_subtask`/`message_task`/`manage_task(cancel/resume)` 对子任务的语义与原 subagent_* 不同（ask、reply_to、resume 新要求）。

### C. 结果

15. 成功结果仍带信封（`ok`、`provider_id`、`tool_name`、`retryable`、`mutation_committed`），或把 null 字段输出给模型。
16. 失败结果不是 `{"ok": false, "error", "message"}`；可能已产生效果的失败没有 `status: "unknown"`。
17. Provider 失败标记：Anthropic `is_error`、Gemini `error` 依赖 `tool_result_failed`；新形状下 `ok:false` 与 `process.succeeded=false` 仍须识别；成功结果的业务数据里自带 `ok` 字段时不得被误判。
18. 截断结果的预览与 `read_more` 不能原样传给 `read_result`（参数改为 `handle/path/cursor`）；Artifact 根节点从信封 `data` 变为结果本身，旧 Artifact 按原根读取。
19. Code Mode `receipt_view` 从结果文本取 `data`：新形状下成功数据整体就是结果，不能只取 `data` 键；`read_more` 参数随 `read_result` 更新。
20. SELF 引文按 `memory_tool_receipts.result_excerpt` 子串核验：去掉信封后只在信封中的引文不再匹配（预期变化）；旧行原值不改写。
21. 结果中不再出现平台账号号码、提供者名、工具名与内部 ID 别名，但内部读取（Work journal、effect 回执、Client API）仍读 typed outcome，不受投影影响。
22. 媒体结果（图片）在新投影中丢失 `media_read`/`media_error` 或 `media_artifact_handle`。

### D. 历史与恢复（原事实不改）

23. 已落盘回执、journal、`memory_tool_receipts.tool_name`、`invocation.tool_id` 被改写为新名。
24. Client API `/works/{id}/steps` 对旧名回执的 category/label/status/run_kind 变化；对新名回执分类错误（例如 `run_command` 不再归“终端”、`use_coding_agent` 不归外部 Agent）。
25. 进行中 Work 的 journal 含旧名调用：恢复时原回执经 `WorkJournal.effect_result` 读取；没有回执的旧名调用不得以新名重放，只能按未知工具失败。
26. `work_repository.resolve_run_effects`、`work_control` 的 owned run/`is_execution`、`process_receipt` 对旧名历史回执不再识别为执行（owned run 关系、等待唤醒丢失）。
27. Code Mode 快照存 `api_revision`，按旧包装名恢复失败（切换时须无进行中快照；`CODE_API_REVISION` 随之升级）。
28. SELF 自省输入展示旧名（应经映射展示现役名）。

### E. 内部调用者与外部消费者

29. `social.execute("send")` 的调用方（插件发送、App ingress、表情、自省报告）仍按旧 send 参数（`target.kind/target_id`、`emoji`、`reply_to_event_id`、`attachment_kind`）传参。
30. 插件 SDK 或插件提示文本引用旧工具名（subscription-monitor 只引用 `send`，保留）。
31. 提示词（`prompting/contracts.py`、`character/` 程序规则、其他内嵌提示与工具描述交叉引用）仍提到旧名或已删除参数；SOUL 人格不改。
32. 运行资料不再有 `time_get_current`：长 Work 中途或等待唤醒后的时间来源须在提示中说明（每次激活重新准备 current_time）。
33. Desktop/Client 只读 `run_kind`、`category`、`label`；Client API 能力目录（`client_api/capabilities.py`）与权限目录（`admin/permission_catalog.py`）引用旧名。
34. 冻结旧名的测试未改写；旧名适配层被保留下来以通过测试。
35. 配置、`.env.example`、`config/`、CI 中引用旧名或已删除工具。

## 名称对照（只读历史映射）

`runtime/effect_outcomes.RECORDED_TOOL_NAMES`：原记录名 → 语义承接的现役工具。只服务原回执与历史投影；新调用不经它解析，记录本身保留原名。

| 原名 | 现役 |
| --- | --- |
| `send_message` | `send` |
| `recall_own_message` | `recall_message` |
| `query_chat_history` | `read_chat` |
| `find_conversations` | `find_contacts` |
| `get_group_members` | `get_members` |
| `read_memory` | `search_memory` |
| `memory_change` | `edit_memory` |
| `update_short_state` | `set_short_note` |
| `web_read` | `read_webpage` |
| `workspace_inspect`、`inspect_conversation_attachment` | `view_file` |
| `save_conversation_attachment_to_workspace` | `copy_file` |
| `read_tool_artifact` | `read_result` |
| `terminal_exec` / `terminal_read` / `terminal_control` | `run_command` / `read_command` / `control_command` |
| `environment_status` | `get_environment` |
| `task_control` | `manage_task` |
| `subagent_start` | `start_subtask` |
| `subagent_control` | `message_task` |
| `automation_query` / `automation_manage` | `get_schedules` / `manage_schedule` |
| `external_agent` | `use_coding_agent` |

没有后继的退役名（`environment_service`、`get_my_capabilities`、`time_get_current`，以及第一批列出的 `local_device`、`run_python` 等）不进映射，按各自原事实在原处读取。

## 分领域补充的失败情况（各领域写代码前列出）

### 会话（send、recall_message、read_chat、find_contacts、get_members）

- S1 清理后为空返回 `message_empty`／`empty_message_after_sanitization`；`NO_REPLY` 被拒。
- S2 `file` 须恰有 `path` 或 `artifact_id` 之一（`choose_one_source`）；`device` 只配 `path`（`device_requires_path`）。
- S3 真实图片字节默认显示为图片；`as_file=true` 按文件发送（QQ `upload_*_file`）；非图片始终按文件；选中的表情始终是图片。
- S4 `file` 与 `sticker` 同给返回 `message_media_conflict`。
- S5 `target` 恰含一个键（person_id／space_id／conversation_id／binding_id），双键由 schema 在执行前拒绝；send 不接受 operation_id。
- S6 conversation_id 解析为该会话自己的人物或群并经该入口发送；未知返回 `target_not_found`。
- S7 binding_id 经该人物入口发送；未知 `binding_unavailable`，非人物入口 `binding_requires_person`。
- S8 App/ACP 入口只给 person_id 时仍返回 `binding_required` 与候选入口。
- S9 `reply_to` 不在目标会话内返回 `reply_event_unavailable`。
- S10 部分或未知送达保留各分条状态，未知显示 `status: "unknown"` 并提示不要重发。
- S11 Host 读取者（effect_outcomes、Work 投递）仍读完整 typed 回执，只缩短模型 view。
- S12 同一调用重放返回原回执。
- S13 内部调用方（插件发送、App ingress、表情投递、自省报告）改用新参数。
- S14 插件权限判断语义不变。
- S15 插件后台带 target 发送仍为 `permission_denied`。
- R1 `recall_message` 改名；存储的社交动作名仍为 `recall_own_message`（回执是历史）。
- H1 无 target、无筛选读当前会话；H2 任一筛选（query、author、has、platform、scope、since、until）检索全部获准聊天；
  H3 since/until 须带时区，until 不含；H4 around 与筛选、cursor、limit、order 同给返回 `around_with_filters`；
  H5 around 不给 context 用配置缺省值，给出时前后对称；H6 author 须为 Person ID 或 `yuki`；
  H7 cursor 是不透明数字串，没有更多时省略 next_cursor；H8 来源会话之外的 `target.operation_id` 返回 `history_receipt_unavailable`；
  H9 委托与插件后台范围仍为 `history_scope_denied`；H10 事件视图每项事实一个名字、无 null，只在跨会话时带 conversation_id；
  H11 `source_event_ids` 与隐私代次照常附带；H12 插件 SDK `search_history` 不变。
- C1 合并原 find_contacts 与 find_conversations，先人物后群、同一 cursor 翻页；C2 显式 target 直接读取，停用人物与已结束入口仍可发现；
  C3 委托 `read_target_id` 只开放该主体，其他目标 `history_scope_denied`；C4 权限 namespace 仍为 `social.contacts`。
- M1 get_members 的 target 为 space_id／binding_id／conversation_id，当前群可省略；M2 模型 view 去掉 null 与内部归属／Presence ID，
  以 `yuki: true` 标出自身账号；插件 `group.list_members` 仍拿完整条目。

### 记忆（search_memory、save_memory、edit_memory、set_short_note）

- M1 无 target 仍读完整获准范围（含插件 `memory_allowed_scopes` 限制）；M2 `{person_id}`／`{space_id}`／`{self:true}` 保原读取核验；
  M3 0 个或 2 个键、未知键返回 `invalid_target`；M4 since/until 无时区或 since ≥ until 返回 `invalid_time` 并指明参数；
  M5 `fact_id` 按同一 SQL 授权读单条，无权为 `memory_not_found`，与 query/target/时间同给或两者皆无为 `invalid_arguments`；
  M6 结果无 `memory_ref` 别名、无 `evidence_state`、无 null。
- S1 任意真实 Person/Space ID 都可作主体（包括本轮未出现的人），不加授权；S2 不存在的 ID 返回 `target_not_found`；
  S3 `group` 配人物 target 写 PersonGroup，配群或 self 为 `invalid_arguments`；S4 SELF 只写自我记忆；
  S5 SELF 须给 `quote` 且出现在本轮 `result_excerpt` 中，只出现在已删除信封里的引文不再匹配；
  S6 人类的证据事件须是本人在本会话的入站消息（`untrusted_evidence_event`），缺失为 `evidence_event_not_found`；
  S7 人类用 `tool_N` 证据被拒（`unsupported_evidence_reference`），引文不在证据消息中被拒；
  S8 私聊来源的 SELF global 记忆仍只允许 preference；S9 按 operation_id 的幂等重放不变。
- E1 correct 须给 content，invalidate 不接受 content；E2 未知事实被拒；E3 撤回他人事实仍被拒（`operation_not_allowed_for_target`）；
  E4 结果保留 `applied` 与 `outcome`，区分 contest／noop 与真实写入。
- P1 插件批准映射不变：save_memory 与 correct 需 MEMORY_WRITE，`edit_memory(action=invalidate)` 需 MEMORY_DELETE。
- N1 `set_short_note` 保持原执行围栏与缓存失效。
- R1 `memory_change`／`read_memory`／`update_short_state` 历史回执经映射读取，存储行不改写；R2 调用旧名按未声明工具失败。

### 文件、电脑与编码 Agent

- C1 view_file 零个或多个来源、event_id 与 attachment 只给其一未被拒为 `choose_one_source`。
- C2 view_file 的 path+device 未翻成 device_id，破坏 `results.render` 的本机媒体归档判断。
- C3 0 基附件序号翻错，或附件不存在时报错不对。
- C4 copy_file 丢失旧 device_id（目标）的含义（现为 `to_device`），或把源 `device` 当目标而不是本期拒绝。
- C5 复制的幂等键或回执形状变化导致恢复时重传，或未确认写入没有显示为 status unknown。
- C6 read_result 不能原样接受 read_more `{handle, path, cursor}`；非数字 cursor 崩溃；structure+query 未拒；
  图片或文本 Artifact 未自动识别；旧信封 Artifact 读不了。
- C7 隐藏页面中的 provider_id/tool_name 破坏 Code Mode 的页面判断。
- C8 run_command 的 device→device_id 未翻译破坏本机路由，或 RPC 方法／任务行哈希变化破坏幂等。
- C9 read/control 的 id→run_id、cursor 字符串→整数缺失。
- C10 get_environment 的 device 未翻译，或 `environment_service` 仍在某处声明。
- C11 改名后的模型工具集合仍在某处按旧名判断，导致路由错误或被当成未知工具。
- C12 命令 typed 数据变化破坏 execution_evidence、owned run 或 process_receipt（`process.succeeded`），或 view 泄露 request_id/state_sequence/admitted。
- C13 截断的命令输出在信封中丢失 run_id。
- C14 get_coding_agents 的 namespace 改变插件权限判断；list/get 不再无副作用。
- C15 use_coding_agent 的单键 target、answer→respond、question→permission_id、answer→option_id/response 或 cursor 合并有误；prompt/queue 不再算执行。
- C16 历史记录的 `respond` 不再恢复；新记录的 `answer` 不被识别。
- C17 参数错误丢失失败形状或 unknown 状态。
- C18 冻结旧名或旧参数的测试残留，或测试中作为 Manager wire 的 `terminal_*` 被误改。

### 工作与安排

36. 旧名仍声明，或新控制不在 `WORK_CONTROL_NAMES`，被当作业务工具计数、并行或 Host 接纳。
37. manage_task start/resume 丢失中性前台检查，导致接纳两个 Work。
38. 结束与只读判断错误：finish、fail、cancel、wait 须结束本轮；get_tasks 只读。
39. 受限执行无法调用 manage_task、wait 或 get_tasks。
40. 畸形 `for` 项未被拒：多个键、`message:false`、无时区 `at`、plugin_event 缺字段、非布尔 `all`、无时区 `deadline`。
41. `{run}` 的所属核验丢失。
42. 存储的等待条件形状变化。
43. 取消或恢复子任务／其他 Work 走了与原来不同的路径。
44. 发往父任务的 message_task、`ask` 或 `reply_to` 丢失。
45. 主任务无法向用户提问。
46. 无当前任务时 start_subtask 报错不清。
47. 结果中仍有旧 `ok` 信封、内部字段或裸错误码。
48. start 接纳 Work 时写入的回执与现场结果不同。
49. journal 中的旧名调用：有回执的回放原回执；无回执的按未声明工具失败，不以新名重派。
50. 旧 task_control/subagent_* 效果被当作业务证据（`observe_evidence` 先经 `recorded_tool` 映射）。
51. 子任务结果丢失或私有进度泄露。
52. `done` 未映射到原终态集合。
53. 安排工具缺 namespace/effect。
54. 拍平的安排参数构造错误：省略 name 或 max_runs 保留原值、null 清除，且 `persisted` 与真实 `id` 必须保留。
55. 异常类名被当作错误码。
56. TimeService 或 Code Mode 名单被破坏。
57. 提示文本仍写旧工具名。

## 改动

### 共同（父分支）

- **结果形状**：`ToolExecutionResult.model_payload()` 成功直接返回业务数据（顶层 null 省略，无数据时 `{"ok": true}`，
  非对象为 `{"result": …}`）；失败为 `{"ok": false, "error", "message"}`，可能已产生效果时加 `"status": "unknown"`，并带领域其余事实；
  观察到的进程只附 `process.succeeded`。`retryable`、`mutation_committed`、`provider_id`、`tool_name` 不再展示。
  新增 `view` 字段：领域 typed `data` 保持不变供 Host 读取，模型看 `view`。截断预览保留必要执行事实与
  `read_more: [{handle, path, cursor}]`；Artifact 根为投影本身，旧信封 Artifact 仍按原 `data` 根读取。
  `normalize_legacy_result` 同时读旧信封与新失败形状。Code Mode `receipt_view` 按新形状取数据。
- **evidence_state 删除**：`services/evidence_state.py`、`ToolExecutionResult.evidence_state／memory_grounding_policy` 删除，
  它们只服务模型展示。
- **声明自带策略**：`ChatTool` 增加 `effect`，与既有 `namespace` 一起由 Core／自动化 provider 读取；删除
  `_CORE_METADATA`、`_AUTOMATION_READ` 等按名字的表。namespace 值与原值一致，插件领域授权不变。
- **可读的错误**：schema 校验错误改为一句中文（哪个参数、期望什么、单键对象应给哪些键、多余参数名）；
  派发前拒绝带说明（未声明的名字说明只调用当前声明的工具）；领域未分类异常返回 `tool_failed`／`superseded`，
  不再以异常类名作错误码。
- **其他**：本轮附件索引的键 `attachment_index` 改为 `attachment`（与 view_file/copy_file 参数一致）；主合同版本 35；
  `DIRECT_TOOL_NAMES` 改为新名；`get_my_capabilities` 及 `CapabilityReport.to_model_dict`、AgentToolService 与
  ConversationModule 的 permission_catalog 装配删除；web_search 参数改为 `query, recency?, news?, since?, until?`，
  结果去掉 domain、provider_score 与固定说明句；历史读取者（work_projection、effect_outcomes、work_repository、
  work_journal、work_control、executions、invocation）按现役名并经 `recorded_tool` 读取旧名；提示词按新工具重写。

### 会话

- send 参数改为 `text, file{path|artifact_id, device, name}, as_file, sticker{goal, emotion}, reply_to, target`（单键）；
  删除 `attachment_kind`、`expected_version`、`emoji`、`reply_to_event_id`、平铺的 path/artifact_id/device_id/name 与
  `target.kind/target_id`。模型 view 为 `{status, operation_id, event_id | parts[{status, event_id, error}], …}`，不回显正文。
  “人物 + 另一入口”的组合目标无法再表达。
- read_chat 结果为 `{events, context?, matched_fields?, next_cursor?, receipt?, around_status?, sources[…]}`；只返回消息事件
  （`event_kind` 删除）；`limit` 无上限，`context` 取代前后两对参数。
- 配置 `CONVERSATION_HISTORY_AROUND_BEFORE／AFTER` 改为 `CONVERSATION_HISTORY_AROUND_CONTEXT=6`。
- find_contacts 结果为 `items[{type, id, name, entries[{binding_id, platform}], conversations[…]}]`；删除 subject_ref、
  display_name、kind、source 与 operation_id 目标，以及 `SocialService.contacts`。
- 插件 `social.send` 权限：给 file 或 sticker 需媒体权限；群／私聊由 space_id、person_id/binding_id、conversation_id 的
  实际类型或当前群决定，语义不变。社交错误说明集中在 `SOCIAL_ERROR_MESSAGES`。

### 记忆

- search_memory 取代 read_memory（evidence 视图删除）；删除 purpose、temporal_constraint、start_at/end_at 与全部名称选择器。
- save_memory／edit_memory 取代 memory_change 的 create／correct／invalidate；其余操作只留在管理、Client API 与 Dream。
- `memory_key`／`category` 由 kind 生成（SELF 为 `self_<kind>`）；因此 SELF 不再能写 global 的 `self_reflection`／
  `self_principle`，私聊来源只有 `self_preference` 可为 global。
- 结果：检索 `{memories[{fact_id, scope, kind, content, importance, confidence, status, last_confirmed_at, …}], exhaustive, …}`；
  变更 `{applied, outcome, fact_id, replaced_fact_id?, deduplicated?, candidates?}`；拒绝说明按真实原因。
- set_short_note 取代 update_short_state。

### 文件、电脑与编码 Agent

- run_command／read_command／control_command／get_environment 取代 terminal_*／environment_status，参数在边界翻成原
  wire 参数，RPC 方法与任务行逐字节不变；`environment_service` 不再是模型工具。命令 view 为 `{id, status, output, exit_code, next_cursor, …}`。
- get_coding_agents（namespace `external_agent.read`）与 use_coding_agent 取代 external_agent；删除 request_id、outcome、
  run_id 别名，native_cursor 并入 cursor；`effect_queries` 把记录的 `answer` 当作 `respond` 查询。
- view_file 合并 workspace_inspect 与 inspect_conversation_attachment（删除 expected_version）；copy_file 取代
  save_conversation_attachment_to_workspace，目标电脑为 `to_device`，源 `device` 本期返回
  `source_not_supported_for_destination`；未确认复制为失败加 `status: "unknown"`。
- read_result 取代 read_tool_artifact，按内容类型自动选择读取方式；structure+query 为 `invalid_arguments`，坏 cursor 为
  `invalid_cursor`；页面不再展示 provider_id、tool_name、logical_root、offset 等，给 `next_cursor`。
- 恢复的领域回执（记忆、本机、原生控制）以 typed `execution_evidence` 交给 historical_evidence，不再解析文本。

### 工作与安排

- get_tasks、manage_task、start_subtask、message_task、wait 取代 task_control 与 subagent_*；删除 derive、wait_status、
  cancel_wait、need_input，以及 `WorkRepository.derive`、`WorkWaitRepository.cancel`。wait 项翻成原存储条件种类。
- 只等 `{"message": true}` 的根任务 wait 结束为 waiting_user（ACP 与 Client 的提问卡片依赖它）；其他等待为 waiting_external。
- 子任务提问用 `message_task(ask=true)` 并等待父任务（waiting_parent）；start_subtask 是唯一的派生方式。
- 子任务的 cancel/resume 走子任务路径（resume 可带 `text`），其他 Work 照旧走 manage_work；fail 始终走 manage_work。
- get_tasks 的 `parent` 使用通用任务目录（原 task_control list 已如此，未新增暴露）。
- get_schedules／manage_schedule 取代 automation_query／automation_manage，参数拍平为 goal/trigger/name/max_runs，结果用 `id`；
  get_schedules 列出全部状态。`time_get_current` 工具及只为它存在的 `AutomationService.current_time`、`time/models.py`、
  `TimeContextService.current_default/at` 删除。
- 存储数据仍写 `state`，模型看到 `status`。

## 风险与上线前核对

- **进行中的 Work**：上线前确认没有处于等待或排队、journal 含旧名待执行调用的 Work；有回执的旧调用回放原回执，
  无回执的旧名调用按未声明工具失败，不以新名重放。待恢复的旧形状 `send` 会被新 schema 拒绝，也不翻译。
- **进行中的表情发送**：延迟配图改用 `{"conversation_id": …}` + `{"file": {"artifact_id"}}`，请求哈希与切换前的已准备回执不同，
  重放可能遇到 `idempotency_conflict`；上线前先结清。
- **Code Mode 快照**：包装名随工具名变化，旧快照按 `code_api_changed` 结算，不重跑。
- **read_chat 不再返回插件外部事件**（event_kind 删除）。
- **SELF 记忆**：不能再写 global 的 `self_reflection`／`self_principle`；SELF 引文只能匹配模型实际看到的业务数据。
- **配置**：`CONVERSATION_HISTORY_AROUND_BEFORE／AFTER` 被 `CONVERSATION_HISTORY_AROUND_CONTEXT` 取代，旧值静默忽略。
- **等待状态**：根任务 `wait({"message": true})` 为 waiting_user；`get_schedules` 列出全部状态（暂停的安排也会出现）。
- **历史插件 allowlist**：已存的 SDK `allowed_tools` 子集可能含旧名（如 environment_status），其边界哈希未改写。
- **原生 Code Mode 测试**只在 CI 的 native job 运行，本机未跑。

## 验证

命令与结果（macOS 本机，基于 `b61c9a37` rebase 后；`--basetemp` 位于会话 scratch 目录，每次运行后删除）：

- `uv run ruff check .`：通过；`uv run mypy --platform linux src`：696 个源文件无问题。
- `uv run pytest tests/unit tests/integration -q -m "not native_worker"`：4818 passed、22 skipped、16 failed。
  16 个失败在 `origin/main`（`b61c9a37`）上以同样参数逐个复跑，同样失败，均为本机环境原因（与前两批记录相同）：
  `AF_UNIX path too long`（test_manager_deployment ×2、test_sticker_library、test_sticker_publication_recovery、
  test_workspace_copy_receipts ×2、test_workspace_publish_versions）、BSD `script` 不支持 `-c`（test_release_platforms ×4）、
  缺 `/proc`（test_shared_media_cache_mount）、`unsafe_workspace_root`（test_plugin_frozen_binding）、本机进程/连接断言
  （test_terminal_environment 显式超时、test_work_execution_receipt_regressions 恢复取消、test_file_copy_e2e 离线首调）。
- 原生 Code Mode（`native_worker`）用例已按新名改写，本机未构建 Monty worker，未运行；由 CI 的 native job 核验。

E2E 与产物（`test-results/` 已在 `.gitignore`）：

- `tests/integration/test_tool_surface_cutover_e2e.py`：离线完整应用冻结清单恰为 27 个工具（direct 与 Code Mode
  两种模式）、旧名与删除名都不在声明中、映射后继全部已声明、注册工具在声明上带 namespace/effect；同一批旧名调用
  （task_control、terminal_exec、query_chat_history、memory_change、time_get_current）都按未声明工具拒绝且没有以
  任何新名执行；结果形状与 Provider 失败标记；现役名回执在 Client API 中与原记录名同类。产物
  `test-results/tool-surface-cutover/{manifest,old-name-calls,result-shapes,history-steps}.json`。
- `tests/integration/test_work_tool_surface_e2e.py`：Work/安排工具，产物 `test-results/tool-surface-cutover/work-tools/*.json`。
- `tests/integration/test_tool_surface_history_prep.py`：旧回执投影、恢复读取与 RPC wire 不变（第一批产物格式）。

声明度量（`manifest.json`，direct 模式完整声明，与 §6.1 生产基线对比）：

| 项 | 切换前（§6.1） | 切换后 |
| --- | --- | --- |
| 工具数 | 29 | 27 |
| 参数数 | 176 | 132 |
| 工具 JSON（UTF-8） | 约 45.8 KB | 约 25.9 KB |
| 主描述字数 | 7427 | 3158 |

除 `read_chat`（13 个可选筛选参数）外，单个工具不超过 8 个参数。§6.11 要求的三种 Provider 格式 14 天调用统计、
真实模型（gemini-3.8-flash）对比与生产库副本上的离线历史验证未在本 PR 中执行，属上线前核对项。
