# 工具面切换准备：RPC 方法名与历史只读映射（2026-11-07）

依据[文件膨胀与函数爆炸删减任务书](../architecture/Yuki-文件膨胀与函数爆炸删减任务书-2026-11-07.md) §6.6–6.10。本批只解开与模型工具名耦合的两处，**模型可见的工具名、参数与结果不变**，Manager、Connector、Desktop 不改，权限不改。29 → 27 的改名、提示与测试改写留给切换 PR。

## 改动

### A. 沙箱 RPC 方法名与模型工具名分开

- `src/yuki/sandbox/client.py` 新增 `MODEL_TOOL_RPC_METHODS`（模型工具 → Manager RPC 方法），当前五项都映射到原值：`terminal_exec`、`terminal_read`、`terminal_control`、`environment_status`、`environment_service`。
- `src/yuki/services/agent_tools.py` 派发环境工具时经该映射取 RPC 方法，不再把模型工具名直接写上 wire。
- `SandboxClient.execute` 内部只按 RPC 方法判断：
  - `is_execution(name, args)`（模型工具语义）换成 `name in EXECUTION_TOOLS`（Manager 已用的执行方法集合 `{"terminal_exec"}`）；该入口只收到 RPC 方法，`external_agent` 从不经此处，结果相同。
  - 提交后无应答时的 uncertain 判定改为显式的 RPC 方法集合 `_MUTATING_METHODS`（`terminal_exec`、`terminal_control`、`environment_service`、`workspace_copy`、`workspace_publish`）加 `acp_*`。原集合由模型工具集合拼出，其中 `save_conversation_attachment_to_workspace` 是模型工具名，从未作为 RPC 方法发送（`workspace/service.py` 改用 `workspace_copy`），去掉它不改变任何可达行为。
  - 回执暂存判定改为显式的 `_TERMINAL_METHODS`，值与原 `SANDBOX_TOOLS` 相同。
- 任务行 `{"tool": name, "arguments": ...}`（`client.py`、`external_agents/local_device.py`）中的 `name` 就是 RPC 方法，值不变；其哈希是原 request ID 的幂等依据，只加注释说明。
- `sandbox/persistent.py`（Manager 进程）未改动；它 import 的 `environment_tools.EXECUTION_TOOLS` 值仍为 `{"terminal_exec"}`，已注明这是 Manager wire 常量，不随模型工具改名。

### B. 历史读取的只读映射（单一来源）

`src/yuki/runtime/effect_outcomes.py` 新增：

```python
RECORDED_TOOL_NAMES = MappingProxyType({
    "send_message": "send",
    "web_read": "read_webpage",
})

def recorded_tool(name: str) -> str: ...
```

含义：原持久记录里的工具名 → 语义相同的现役工具。只服务原回执与历史读取；新调用不经它解析；记录本身保留原名（证据的 `tool` 字段仍是原名）。

经它解析的读取位置：

| 位置 | 原写法 | 现写法 |
| --- | --- | --- |
| `effect_outcomes.execution_evidence` | `tool in {"send","send_message"}`（×2）、`tool == "external_agent"`、`tool in {"external_agent","local_device"}`（×2） | 先 `reading = recorded_tool(tool)`，判断按 `reading`；输出 `tool` 仍为原名 |
| `effect_outcomes.historical_evidence` | `invocation.tool_id == "external_agent"` 时写死 `"external_agent"` | `recorded_tool(tool_id) == "external_agent"` 时写回原 `tool_id`（当前值相同） |
| `client_api/work_projection.public_step` | `{"read_webpage","web_read"}`、`{"send","send_message"}` | 先 `recorded_tool(tool_id)`，再按现役名分类；`is_execution` 也用解析后的名字 |
| `client_api/work_projection.wait_view` | `invocation.tool_id` 原值集合 | 解析后的集合 |
| `runtime/work_repository.resolve_run_effects` | `previous["tool"]` 原值 | `recorded_tool(previous["tool"])`；写回的 `merged["tool"]` 仍为原名 |

无需改动、已自然经映射的位置：`work_journal.effect_result` 把原名传给 `historical_evidence`；`work_repository.record_effect` 把原名传给 `execution_evidence`。`capabilities/invocation.py:80` 的 `tool_id` 是新调用写入，不是历史读取。`memory_tool_receipts.tool_name` 的读取只做原值比对与 SELF 输入展示，不按名字分支，本批保持原值（见下方切换事项）。

**不并入映射的旧名**：`local_device`、`run_python`、`environment_packages`、`terminal_write`、`cancel_code_run`、`get_code_run` 是没有后继的退役名，不是别名。例如 `local_device` 的 Desktop 分类是独立的“本機操作（歷史）”，与 `external_agent` 不同，映射成 `external_agent` 会改投影；`run_python` 映射成 `terminal_exec` 会把步骤从“其他/成功”改成“終端/未知”。它们按各自原事实在原处读取。`effect_outcomes` 中 `tool == "send_message"` 的两处是按原回执形状解读（单文件 + caption、无 `attachment_kind` 的纯文字），也不是别名，保留原名判断。

## 留给切换 PR 的事项

1. **映射表补入旧名**：29 → 27 的旧名加入 `RECORDED_TOOL_NAMES`。拆分的工具（`external_agent`、`task_control`、`subagent_control`、`memory_change`）只能映射到一个现役名，建议映射到承载 action 语义的那个（如 `external_agent → use_coding_agent`），读取处按原 `action` 区分。
2. **RPC 映射改键**：`MODEL_TOOL_RPC_METHODS` 的键改为 `run_command`、`read_command`、`control_command`、`get_environment`；删除 `environment_service` 模型工具时删掉对应键。值与 `_MUTATING_METHODS`、`_TERMINAL_METHODS`、`EXECUTION_TOOLS` 不改。
3. **历史读取处的现役名比较随改名更新**：`work_projection.public_step` 分类链（`startswith("terminal_")`、`web_search`、`read_webpage`、`WORKSPACE_*`、`workspace_publish`、`external_agent`、`"memory" in tool`、`send`）、`wait_view` 的 `terminal_exec` / `external_agent`、`capabilities/executions.is_execution`、`effect_outcomes` 的 `send` / `external_agent` 判断、`work_repository.resolve_run_effects` 的 `terminal_control` 集合。Desktop 只读 `category` / `label` / `run_kind`，经映射后不受影响。
4. **`runtime/work_control.py`（本批未改，避开 #577）**：`:331-339` 按 `effect["tool"]` 调 `is_execution` 并比较 `external_agent`、`environment_packages`、`run_python`；`:571` 用 `WORK_CONTROL_NAMES`。切换时 `:331` 应改为 `recorded_tool(effect.get("tool", ""))`。
5. **`capabilities/results.process_receipt`（`:495-499`）**：历史回执经 `normalize_legacy_result(tool_name=原名)` 进入，集合改成新名后需对历史路径先 `recorded_tool`。
6. **SELF 输入中的原工具名**：`memory/self_reflection/service.py:351` 把 `memory_tool_receipts.tool_name` 原值展示给 SELF 模型；若切换后不希望旧名出现在模型面前，在此投影处用 `recorded_tool`，`self_reflection/repository.py` 的检查点比对继续用原值。
7. **Code Mode**：包装名由工具名直接生成（`codemode/api_projection.encode_wrapper_name`），本批不变；切换时确认没有进行中的 Code 快照（快照存 `api_revision`）。

### 剩余硬编码工具名集合（本批不收敛）

| 位置 | 内容 |
| --- | --- |
| `codemode/tool_visibility.py:14-41` | `DIRECT_TOOL_NAMES` |
| `capabilities/provider.py:24-83` | `_CORE_METADATA`（权限元数据，按名字） |
| `services/evidence_observation.py:15-23` | `EVIDENCE_TOOLS` |
| `capabilities/results.py:169,221,235,245` | `workspace_inspect`、`read_tool_artifact`、`web_search`/`read_webpage` 等按名字的结果处理 |
| `capabilities/results.py:495-499` | `process_receipt` 的执行类工具集合（含退役 `run_python`、`get_code_run`） |
| `capabilities/results.py:503,552-553,710` | `external_agent`、`SANDBOX_TOOLS | WORKSPACE_TOOLS | {external_agent, task_control}`、`send` |
| `capabilities/results.py:795-813` | 记忆与网页的可信 grounding/evidence 集合 |
| `runtime/work_control.py:23` | `WORK_CONTROL_NAMES` |
| `services/agent_tools.py:122-127,820,891` | `_OBSERVED_MEMORY_READS`、`SANDBOX_TOOLS`/`external_agent`/`WORKSPACE_TOOLS` 派发 |
| `services/invocation_service.py:308` | `get_code_run`、`terminal_read`、`cancel_code_run`、`terminal_control` |
| `services/main_agent_contract.py:39-55` | `SANDBOX_TOOLS | WORKSPACE_TOOLS` 健康检查 |
| `sandbox/environment_tools.py:11-21`、`workspace/tools.py:8-11` | `READ_TOOLS`、`ENVIRONMENT_TOOLS`/`SANDBOX_TOOLS`、`WORKSPACE_*`（模型工具集合；`EXECUTION_TOOLS` 是 Manager wire 常量，不在此列） |
| `capabilities/executions.py` | `is_execution` 的 `terminal_exec` / `external_agent` |

## 验证

先列出可能失败的情况，再写测试，再改代码。新增 `tests/integration/test_tool_surface_history_prep.py`，覆盖：

- F1/F2：经 `AgentToolService` 调五个环境工具，真实 Unix socket 上的假 Manager 记录每个 RPC 方法名与参数，与改前一致；`terminal_exec` 任务行载荷仍为 `{"tool": "terminal_exec", ...}`。
- F3：提交后 Manager 不应答时，各 RPC 方法（terminal_*、environment_*、workspace_copy/publish/media_read、sticker_index、acp_*）的 uncertain 不变。
- F4–F6：真实 SQLite + Client API `/client/v1/works/{id}/steps`，固定的 17 条历史回执样本（含 `send_message`、`web_read`、`local_device`、退役 `run_python`、prepared 行、无 invocation 的旧行）投影出的 category/label/status/artifacts 与 owned run 的 `run_kind` 不变。
- F7：经 `WorkJournal.effect_result`（原恢复读取器）读取旧名回执，`send_message` 旧结果的完整证据（含 `tool` 原名、`delivered_artifacts`、`caption_delivered`）不变。
- F8/F9：Code Mode 包装名不变；映射表中的名字都不是已声明工具。

产物：`test-results/tool-surface-prep/{rpc-wire,history-projection,code-mode-wrappers}.json`（含 commit 与 worktree 状态）。同一测试先在基线 `ce518d99` 上运行（除引用新符号的 F8/F9 外全部通过），保存产物；改动后再运行，`history-projection.json` 与 `rpc-wire.json` 除 commit 与时间戳外逐字段相同。

命令与结果（macOS 本机）：

- `uv run ruff check .`：通过。
- `uv run mypy --platform linux src`：698 个源文件无问题。
- `uv run pytest tests/unit tests/integration -q -m "not native_worker"`：4768 passed、22 skipped、16 failed。16 个失败在基线 `ce518d99` 上以同样参数逐个复跑，同样失败；原因均为本机环境：7 个 `AF_UNIX path too long`（basetemp 路径过长）、4 个 BSD `script` 不支持 `-c`、1 个缺 `/proc/meminfo`、1 个 `unsafe_workspace_root`（`test_plugin_frozen_binding`）、3 个本机进程/连接断言（`test_terminal_environment` 显式超时、`test_work_execution_receipt_regressions` 恢复取消、`test_file_copy_e2e` 离线首调）。其中 `test_workspace_copy_receipts` 的 socket 丢 ack 用例因路径过长未能在本机执行，同一行为由新测试 F3 覆盖。
