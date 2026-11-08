# 工具面切换准备（第二批）：内部读取改读 typed outcome，Host 不经模型工具名

日期：2026-11-07。依据：[文件膨胀与函数爆炸删减任务书](../architecture/Yuki-文件膨胀与函数爆炸删减任务书-2026-11-07.md) §6.7“结果改投影前须先迁移的内部读取”与 §6.10“直接调用核心工具的位置”。本批不改模型可见的工具名、参数与结果投影，不改权限。

## 写代码前列出的可能失败情况

1. web_search 现场成功：Work 的跨模型搜索观察（portable_search）应来自 typed outcome；模型看到的结果文本逐字节不变。
2. web_search 复用原回执（只读复用或恢复）：没有现场 typed outcome，仍须从原回执记录同样的来源，不重新派发。
3. web_search 失败（ok=false）：不记录来源。
4. 来源项畸形（非 dict、缺 url、title/snippet 非字符串）：与原来一样跳过。
5. 带图片结果，模型无图片输入能力或超出帧/字节预算，且副作用已提交：保留原结果并附 `media_read=false`/`media_error`，不得被改写成失败。
6. 同上但副作用未提交：改写成 `{"ok":false,"error":…,"read":false}`。
7. 恢复时从 journal 读出的带图片原回执：是否已提交按原回执的 typed 证据判断。
8. `execute_code` 带图片：仍按 Code 自己结果的 `executed`（Code Mode 本期不改）。
9. SELF 引文出现在真实结果数据里：新 excerpt 接受；旧行 excerpt 原值保留并照旧可匹配。
10. SELF 引文只出现在展示信封（provider_id、tool_name、retryable 等）里：新 excerpt 不再包含信封，不再匹配；旧行不改写。
11. SELF 引文不存在：两种 excerpt 都拒绝。
12. 自省读取 tool receipt：新行展示真实结果，旧行展示原值。
13. 模型调用 `task_control(accept)`：模型可见结果与落盘 `result` 逐字节不变；回放原回执返回原文。
14. 插件主调用 Host 接纳 Work：不经 `task_control` 工具名与结果文本，Work 目标、状态与预算计费不变。
15. 外部事件持久调用 Host 接纳：同上。
16. Host 接纳失败（来源失效、目标为空白、写入失败）：原先被模型工具包装吞成一段被丢弃的结果文本，直接调用领域服务后按原异常抛出。
17. 插件 `group.list_members`：返回同样的成员项；仍需 `GROUP_MEMBERS_READ` 与群范围核验。
18. App ingress、表情、自省报告与插件发送：读的是 SocialService 的领域回执，不是模型结果文本。
19. Provider 适配器（Anthropic `is_error`、Gemini `error`）：仍按工具消息文本判断失败，本批不改，列入切换事项。

## 实施后与上表不同的结论

- **情况 2（复用原回执）**：只读回执重读只带历史证据，没有现场 typed outcome。前台循环里这类结果不再从原文本补记来源；来源已在原执行时记录。agent_runner 的恢复路径（`_pending_results`）在改前也不记录搜索来源，行为不变。不为此保留文本解析。
- **情况 9–12（SELF 引文）**：本批不改 `result_excerpt`。它存的就是模型实际看到的有界文本，SELF 引文按它匹配，即“证据随模型看到的内容走”：现在信封仍在投影里，只出现在信封中的引文（如 `get_group_members`）照旧被接受；结果投影切换去掉信封后，该匹配随之消失，无需另行迁移。E2E 的 `presentation-only` 用例因此断言“当前仍接受”，冻结的是这一事实而不是新规则。旧行不改写。
- **情况 12 的另一半**：SELF 自省输入展示 `memory_tool_receipts.tool_name` 时经 `recorded_tool` 映射（`send_message → send`、`web_read → read_webpage`），存储行与检查点比对仍用原名。这是第一批“留给切换 PR 的事项 6”，本批完成。

## 改动

| 位置 | 原写法 | 现写法 |
| --- | --- | --- |
| `services/turn_execution.py` 搜索来源 | `json.loads(result)["data"]["sources"]` | `CoordinatedToolResult.outcomes[call.id].data["sources"]`；成功仍认 typed 证据 `ok` |
| `capabilities/coordinator.py` | 只回传证据 dict | 另回传现场 `outcomes`（按 call id）；Code Mode 混合批次合并时一并传递 |
| `services/agent_runner._budget_tool_media` | 解析结果文本的 `mutation_committed` | 读 typed 证据；恢复路径从 journal 原回执取历史证据（`ResultCapture`） |
| `runtime/work_control.py` | `{"ok": True, **result}` 写两处 | `control_result_payload` 一处生成，现场与回执共用；`accept` 的写入抽为 `_admit_goal` |
| `plugin_host/main_turn.py`、`services/durable_invocations.py` | `execute("task_control", {"action":"accept"}, "host-invocation-admission")` | `WorkControl.admit_goal(goal)` |
| `plugin_host/facades.py` 成员目录 | `social.execute("get_group_members", …)` | `social.group_members(…)`；模型路径的 `execute("get_group_members")` 也转到它 |
| `memory/self_reflection/service.py` | 原 `tool_name` | `recorded_tool(tool_name)` |

## 行为变化

1. **Host 接纳失败改为抛出**：原先经 `WorkControl.execute` 包装，`ValueError`/`WorkConflict`（目标为空、来源失效）与已判定的写入失败被转成一段 `{"ok": false, …}` 文本，Host 调用方丢弃它并在 `current is None` 下继续执行。现在 `admit_goal` 按原异常抛给插件主调用或持久调用的调用方。
2. **Host 接纳不再写 effect 回执**：原先 `call_key="host-invocation-admission"`、`invocation=None`，本就不写回执；现在显式不经工具包装，Work 目标、状态与预算计费不变（E2E 产物逐字节相同）。
3. **SELF 自省看到现役工具名**：旧行 `send_message`、`web_read` 展示为 `send`、`read_webpage`。
4. **只读复用回执不补记搜索来源**：见上节情况 2。

模型可见的工具名、参数、结果文本、落盘回执与回放均不变。

## 留给切换 PR 的事项

1. 结果投影（`model_payload`、`social/history.py` 的 render）可以改了：本批之后 Host 内部不再解析模型结果文本中的 `data.sources`、`mutation_committed` 与控制结果；`execute_code` 的 `executed` 随 Code Mode 任务另议。
2. Provider 适配器的失败标记（Anthropic `is_error`、Gemini `error`）随新失败结果形状 `{"ok": false, "error": …}` 一并调整。
3. `social.execute("send")` 的调用方（插件发送、App ingress、表情、自省报告）参数形态随新 `send` schema 调整。

## 验证

先列出上面的失败情况，再写 `tests/integration/test_tool_surface_prep2_e2e.py`，再改代码。用例走真实 SQLite、真实 Work/journal、`FakeLLMProvider` 与真实结果投影：

- `test_web_sources_and_media_budget_follow_typed_outcomes`：情况 1、3、4、5、6；
- `test_same_sources_from_two_searches_are_recorded_once`：来源去重；
- `test_model_accept_keeps_visible_result_receipt_and_replay`：情况 13；
- `test_plugin_host_admission_search_and_member_directory`：情况 14、17（插件 SDK 主调用 + web_search + 成员目录）；
- `test_main_self_quote_is_verified_against_the_real_result[data|presentation-only|absent]`：情况 9–11；
- `test_self_reflection_reads_recorded_tool_names_through_the_map`：情况 12；
- `test_host_admission_writes_no_model_receipt_and_raises_real_failures`：情况 16 与“不写回执”。

产物：`test-results/tool-surface-prep2/<label>/*.json`（`PREP2_LABEL` 指定标签，UUID、时间、哈希已归一化）。同一测试先在基线 `f61092c4` 上运行（`before`，引用新符号的 `admit_goal` 用例除外），再在改动后运行（`after`），对比写入 `test-results/tool-surface-prep2/comparison.json`：8 个场景中 7 个逐字节相同；唯一差异是 `self-reflection-tool-names` 的 `shown` 列从 `web_read`/`send_message` 变为 `read_webpage`/`send`，即行为变化 3。

情况 7（journal 恢复的带图原回执）经 `_pending_results` 的 `ResultCapture` 读历史证据，情况 15（外部事件持久调用）与 14 共用 `admit_goal`，由现有插件事件与恢复测试在全量套件中覆盖；情况 8、18、19 的代码本批未改。

命令与结果（macOS 本机，`--basetemp` 位于会话 scratch 目录）：

- `uv run ruff check .`：通过；`uv run mypy --platform linux src`：698 个源文件无问题。
- `uv run pytest tests/integration/test_tool_surface_prep2_e2e.py -q`：9 passed。
- `uv run pytest tests/unit tests/integration -q -m "not native_worker"`：4830 passed、22 skipped、16 failed。16 个失败在基线 `f61092c4` 上以同样参数复跑同样失败，与第一批记录的本机环境原因相同（7 个 `AF_UNIX path too long`、4 个 BSD `script` 不支持 `-c`、1 个缺 `/proc/meminfo`、1 个 `unsafe_workspace_root`、3 个本机进程/连接断言）。
