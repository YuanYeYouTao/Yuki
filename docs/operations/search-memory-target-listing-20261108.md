# search_memory 省略 query 时列出记忆（2026-11-08）

来源：线上（部署 `de27ffa6` 后）模型两次以 `{"target":{"person_id":…}}` 调用 `search_memory`，
意图是“我记得这个人什么”，得到 `invalid_arguments`「提供非空 query 搜索，或用 fact_id 读取一条记忆」。
冻结声明不要求 `query`（`fact_id` 是另一种用法），执行处却要求非空 query：声明与执行不一致。

决定：放宽，不收紧。省略 `query`（且不给 `fact_id`）时列出给定范围内可读的记忆；读取授权、
可见性与 scope 解析与 query 路径完全相同；不新增索引、排序、状态或游标。

状态：本地修改与验证见 §3；提交与 PR 见报告。**未合并、未上线。**

## 1. 改代码前列出的可能失败

### 1.1 范围

- F1 空范围：target 是有共同群关系、但没有任何记忆的人物 → 返回 `memories: []`、`exhaustive: true`，
  不是错误；无授权的人物仍是 `permission_denied`（不能变成“空列表”掩盖拒绝）。
- F2 Space 与 Person：`{person_id}` 沿 `_person_memory_selection` 列出该人 Person 与共同群的 PersonGroup
  事实；`{space_id}` 只列该群的 Group 事实，不混入群内个人事实。两者解析与 query 路径相同。
- F3 `{"self": true}`：只列当前场景可见的 SELF（global + 当前群可见，或私聊中对当前人物可见）；
  自我记忆未启用仍为 `self_memory_unavailable`。
- F4 群场景下的私密记忆：无 target 列出与 query 路径共用 `authorized_fact_condition`。群场景
  `current_private_person_id` 为空，请求者自己的 private SELF 不出现；他人的 private SELF 永不出现。
  有共同群关系即可读目标完整 Person 事实（含私聊来源）是 memory-v2 已接受的取舍，不在此收紧。
- F5 无 target 的默认范围：沿用现状——有真实社交请求者时为其全部获准 Person/PersonGroup/Group 与当前
  可见 SELF；SELF 执行（无请求者）只到当前群 Group 与可见 SELF（`current_group_only`）。不发明新范围。
- F6 插件 `memory_allowed_scopes`：显式 target 的 scope 门槛与无 target 的 `allowed_scopes` 不变。

### 1.2 过滤、排序与分页

- F7 只给 `since`/`until`：沿用严格时间条件（`valid_from` 非空且落在 [since, until)）；无 `valid_from`
  的事实不出现，与 query 路径相同；格式错误仍为 `invalid_time`。
- F8 已被取代、已撤回、隔离或已到期（`valid_until` 过去）的事实不出现：沿用 `status=active`、
  `review_state!=quarantined`、`valid_until` 条件。
- F9 排序：复用 `MemoryFactRepository.list_overview` 的既有顺序（importance、confidence、updated_at 降序，
  id 升序）。SELF 执行显式 target 走的 `retrieve()` OVERVIEW 分支本来就用这条路径；有授权范围的路径
  需要同一顺序、同一条件的 `authorized_fact_condition` 版本。不另建排名。
- F10 分页：本工具没有游标，约定只有 `limit`（1–100，缺省 `context_limit_per_entity`）加
  `truncated`/`exhaustive`。多于 limit 时 `truncated=true`、`exhaustive=false`；模型可加大 limit 或用
  since/until 缩窗。单次结果过大仍由现有结果交付（artifact + `read_result`）续读。不新增游标。
- F11 列出不调用 Embedding（不产生付费调用），`semantic_status="overview"`，不报 `semantic_degraded`。
- F12 结果形状：与 query 搜索相同的 `{"memories":[…], "exhaustive":…, "truncated"?}`，条目同为
  `_memory_json`；观察记录（`_capture_memory_tool_result`）同样登记读到的事实。

### 1.3 参数与合同

- F13 `fact_id` 与 query/target/时间同给：仍为 `invalid_arguments`。这是跨字段规则，JSON Schema 只能用
  顶层 `oneOf`/`not`/`dependentSchemas` 表达，各 Provider 方言对顶层组合关键字支持不一，保留执行处检查。
- F14 仅空白的 `query`（`"  "` 满足 `minLength: 1`）：`parse_memory_tool_intent` 已将其视为 OVERVIEW，
  现在按列出处理，不再报错。
- F15 未知参数：`additionalProperties: false`。业务工具的直调、Code Mode 子调用和恢复重派都经
  `MainAgentToolBackend.execute_call` → `TurnCapabilityRuntime.validate_call_result` 校验；须核实后再删
  `_search_memory` 顶部的手写未知参数检查。`limit` 的手写 1–100 检查同理（schema `integer` 不接受布尔；
  `5.0` 由 `MemoryReadRequest` 的 int 字段收为 5）。
- F16 说明文字变化 → 冻结合同 revision 变化：上线前未结算 journal 在 `WorkJournal._load` 判
  `contract_changed`，未配对调用只审计、不重派；已有回执（包括旧的 `invalid_arguments`）原样保留。

## 2. 核实结论

- F15：`execute_call` 中 `capability_runtime` 只在 `prepare`/`refresh_catalog` 安装，同时设置 `_catalog`；
  未安装时 `_catalog` 为空，调用在 binding 前以 `unknown_capability`/`capability_not_allowed` 拒绝，
  不会无校验执行。Code Mode 子业务调用（`agent_runner.execute_business`）调 `tools.execute_call`；恢复
  重派经 `_execute_tool_batch` → `ToolCallCoordinator.execute_batch` → `backend.execute_call`。
  `AgentToolService.execute` 在生产中只由 core provider binding 调用。两处手写检查删除。
- F13：保留 `fact_id` 排他检查。
- F9/F5：有授权范围的列出新增 `MemoryFactRepository.list_overview_authorized`，与 `list_overview` 共用
  同一条件与排序，只把单目标条件换成 `authorized_fact_condition(scope)`。`MemoryRetriever.retrieve_authorized`
  在 OVERVIEW 模式走它；`MemoryContextService.search_authorized` 接收 `MemoryQueryPlane` 已有的
  `retrieval_mode_for_request` 结果，不再固定 RELEVANT。

- F10：本工具保持无游标；`limit` 之外由 `truncated`/`exhaustive` 说明是否已列全。
- F11：列出走 OVERVIEW，`for_targets` 对 OVERVIEW 意图关闭语义检索，不调用 Embedding。

## 3. 验证

- E2E `tests/integration/test_search_memory_listing_e2e.py`：真实群会话中的定时任务驱动真实 Main
  后端（冻结 schema 校验 → core binding → Memory 读取）。改代码前在 `origin/main` 上两例均失败
  （target-only 返回 `invalid_arguments`），改后通过。产物
  `test-results/search-memory-listing/{person-group-scene,self-execution}.json`（gitignored），
  两次运行逐字节相同。
- `uv run ruff check .`、`uv run mypy --platform linux src` 通过；Memory 与工具相关单元/集成 335 项通过。
- 全量 `pytest tests/unit tests/integration -m "not native_worker"`：4827 passed、16 failed，16 项全部在
  已知本机 macOS 失败清单内（manager_deployment、release_platforms、shared_media_cache_mount、
  sticker_*、terminal_environment、work_execution_receipt、workspace_copy/publish、file_copy_e2e、
  plugin_frozen_binding）。
