# 2026-11-07 普通执行连续性与插话接续实施记录

任务书：[Yuki 普通执行连续性与插话接续任务书](../architecture/Yuki-普通执行连续性与插话接续任务书-2026-11-07.md)（作者决定 D1=路线 A、D2=插件显式上限迁入根 Work 预算并整套删除分段机制）。关联 #560。

基线 main `69420ce8`；分支 `feat/execution-continuity-20261107`。本记录区分本地修改、验证、提交与推送；**未合并、未部署、未在生产验证**。本次只改 bot 进程内代码：宿主 Manager（`src/yuki/sandbox/persistent.py` 等）、`packages/yuki-agent-connector` 与 Yuki-Desktop 未改，上线为 bot-only。

## 1. 实际改动

| 编号 | 内容 | 主要文件 |
| --- | --- | --- |
| C01 | `development-contract.md` §2 改写为“模型不必 accept；私聊多步执行由 Host 登记；不设段上限，显式额度只在根 Work 预算”；`main-agent-runtime.md` 同步接纳时机、插话、失败重试、`RUNTIME_WORK_ENABLED` 依赖与段机制删除 | 文档 |
| C02 | 私聊（含 App 私聊）Main 在首个含 `send` 以外调用的工具批次执行前（含 `execute_code`），Host 先 `protect_dispatch` 标记已开始效果，再经 `validate_turn_snapshot` 核对版本，然后以预生成 Work ID 在**同一 writer**（`WorkSession.save("response", publication=…)` → `WorkJournal.save`）提交 accept、接纳前已付模型计量与首份 `phase=response` journal，最后才为整批绑定调用身份。goal 为 `完成本人私聊请求 event:<触发事件 ID>`，不复制正文。纯 `send`、无工具、群聊、批次内已有模型自己的 `accept` 时不接纳。模型 accept 与 Host 接纳共用 `WorkControl._admit_in_session` | `turn_execution.py`、`work_control.py`、`work_session.py`、`main_agent_backend.py` |
| C03 | 整套删除分段机制（见 §2） | 见 §2 |
| C04 | 接纳前仍由版本栅栏与 `preempt_private` 抢占；接纳后新消息经既有 `preserve_active` 进入同一 Work，同批未执行的副作用调用返回 `new_input_before_execution`，下一次请求前接入新输入。`preempt_private` 保留（只省取消飞行中请求的时间，`preserve_active` 不能替代），`test_turn_coordinator_cancellation.py` 语义不变、无需修改 | 无新增代码（由 C02 生效） |
| C05 | `AutomationWorker` 与 `WorkResumer._scene` 在已有 `ConcurrencyManager.conversation(key)` 会话锁内取得 Work 租约并激活，同会话用户 Main 排队等锁，不再因 `conversation_activation_busy` 被丢弃；`validate_turn_snapshot` 对 `automation_run_id` 来源只核 generation，不受此后用户输入版本作废；无 Work 的激活异常时把实际已接纳请求数挂在异常上（`record_model_requests`），`settle_interrupted_occurrences(llm_calls=…)` 写入 `llm_calls`。到期指令文本未改 | `automation/worker.py`、`automation/repository.py`、`services/work_resume.py`、`services/chat.py`、`runtime/activation_outcome.py`、`agent_runner.py`、装配 |
| C06 | E2E 先验证“`recent_work` 给 work_id、模型再 `task_control get`”：同来源（用户消息）足够；但定时发生的来源 `origin` 不同，`recent_work` 看不到用户的 Work，`get` 进度也按原私有来源不授权。因此 `WorkQueries.recent` 不再按 `origin` 区分（人物、SELF 场景、插件、委托、执行边界仍分开），并内联最近 8 条原回执事实 `recent_steps`（tool、ok/pending/uncertain、status、run_id、error_code、artifacts、delivered，无参数原文）。`task_control get` 授权未改 | `runtime/work_queries.py` |
| C07 | 文档与提示同步（见 §4） | 文档、`work_control.py` 的 `task_control` 描述、`prompting/contracts.py` |

插件 D2：`ctx.agent.run(max_model_requests=, max_tool_calls=)` 签名不变，值写入 Work 来源的 `model_limit`/`tool_limit`，首次接纳时与 Work 同一事务经 `work_budget.limit` 写入根预算 `runtime_work_budgets.model_limit/tool_limit`（该两列已有执行处，此前生产全为空）。旧来源中的 `max_*` 字段只读保留，`resume_plugin_work` 不再读取。独立会话 `RunAgentSessionRequest.max_model_requests` 字段保留：会话为无工具单次请求，该值总被满足，嵌套请求仍计入发起方根 Work。

## 2. 删除清单

- 配置：`Settings.agent_max_tool_calls`、`agent_max_model_requests`；`ConversationSettings` 同名字段；热配置 `agent.max_tool_calls`、`agent.max_model_requests`；`AgentRuntimeConfig.max_tool_calls/max_model_requests`；`.env.example` 两行。`Settings` 为 `extra="ignore"`，旧环境变量残留不会导致启动失败；数据库残留覆盖行按未知键被 `_valid_stored_record` 忽略。
- 运行时：`AgentRuntime.max_tool_calls/max_model_requests`；`InvocationContextFactory.from_tools` 两个参数；`WorkControl.segment_model_limit`；`SegmentBudgetReached` 及 supervisor 分支；`run_agent_loop(max_requests)`、`STOP`、`LoopSignal`、`TurnSettlement.exhausted()`、`ToolBatchOutcome.terminate`；`TurnExecution.begin()` 的段上限 STOP 与 `work_segment_handoff` 提示、`segment_handoff_index`、`prepare_request` 两处 STOP、`exhausted()`（含 queued 分支与 `agent_segment_budget_exhausted`）；`ToolInvocationCoordinator.execute_batch(remaining_calls)` 与 `tool_limit_exceeded`；`_execute_tool_batch(remaining_calls)`。
- 插件：`facades.run()` 对配置默认值取 min 的段上限；`_agent_dependencies` 的段上限；`main_turn` 写入来源的 `max_*` 与恢复时 `min(...)`；`plugin_sessions.run(max_model_requests)` 与 `session_facade` 传参。
- Code Mode：设计未改（#573 不处理）；`CodeHost.tool_limit` 改为可空，主 Agent 传 `None`，`driver.py` 比较前判空，`agent_runner` 删去对 `max_tool_calls` 的断言。
- 测试：删除冻结热配置段上限的 `tests/unit/test_worker_hot_limits.py`；其余测试删去 `max_*`/`remaining_calls` 参数，冻结段语义的断言按新合同改写（见 §3）。

## 3. 验证

本地环境：`uv sync --all-extras --managed-python`（Python 3.12.16，macOS arm64）。

以下均为本地运行结果（macOS arm64），CI 结果以 PR 为准。

### 3.1 静态检查

- `uv run ruff check .`：通过。
- `uv run mypy --platform linux src`：通过（698 个源文件）。

### 3.2 E2E

`uv run pytest tests/integration/test_execution_continuity_e2e.py -q`（12 passed；须单进程运行，xdist 下各 worker 会各写一份不完整产物），产物写入 `test-results/execution-continuity-e2e.json`（`test-results/` 被 `.gitignore` 忽略，按此命令重新生成；每次运行覆盖）。隔离 SQLite、`FakeLLMProvider`、假 QQ 网关，走真实 processor / Main / Work / Social 路径。场景与 §8 对应：

| 场景 | 结论 |
| --- | --- |
| 事故重放：私聊 30 次工具 + send + 结束，共 32 次请求 | 完成，1 个 Work `completed`，`model_requests=32`、31 条 effect 全归该 Work，首请求无 owner、之后全为该 Work，journal 存在，私聊发送 1 次 |
| `RUNTIME_WORK_ENABLED=false` 私聊 28 次请求 | 完成，不建 Work，未截断 |
| 群聊直接 @ 28 次请求 | 不接纳 Work，未截断 |
| 接纳后模型再 accept / update | accept 返回 `work_already_active_use_update`，仍只有 1 个 Work，update 改写 goal |
| 并行读取批次 + `execute_code` | 同一 Work、同一 request_sequence；`execute_code` 不再返回 `accept_work_before_execution`（本机无 Monty worker，返回 `code_engine_unavailable`） |
| 接纳提交后、首个工具前崩溃（Runner 级受控硬退出） | 崩溃后 Work `running`、`model_requests=1`、journal `phase=response`、无 effect；恢复后沿原 response 依次执行 3 个原调用各一次，只再购买 1 次请求，`model_requests`/根预算 `models`=2，effect owner 均为原 Work |
| 接纳后插话 | 第二条消息 `work_input_queued`，同批旧写入返回 `new_input_before_execution`，下一请求含新消息，仍 1 个 Work、4 次请求、1 次发送 |
| 接纳前插话 | 旧轮 `turn_interrupted`，新消息独立 Main 处理，旧响应未执行 |
| 定时发生运行期间用户私聊 | 用户消息等待会话锁而非丢弃，发生结束后处理（`chat`）；发生 `succeeded`、`llm_calls=2`；发生首请求的 `recent_work` 含上一轮私聊 Work 及 `update_short_state`/`automation_manage`/`send` 原回执 |
| 发生异常 | `uncertain`，`llm_calls=2` 等于实际请求数 |
| 下一轮“刚才做了什么” | `recent_work` 给出原 work_id 与 `recent_steps`，`task_control get` 返回原步骤 |
| 插件显式上限 | `max_model_requests=2, max_tool_calls=5` 写入根预算，第 3 次请求前 Work 进入 `suspended` |

### 3.3 回归

- 命令：`uv run --with pytest-timeout --with pytest-xdist pytest tests/unit tests/integration -q -m "not native_worker" --timeout 120 -n 10`（`pytest-timeout`/`pytest-xdist` 只为本地限时与并行临时加入，未改锁文件）。
- 结果：**4765 passed、22 skipped、15 failed**。15 项失败均为本机 macOS 环境问题，与本改动无关：`AF_UNIX path too long`（Manager/Workspace socket 类 7 项）、BSD `script` 不支持 `-c`（安装脚本平台选择 4 项）、缺 `/proc/meminfo`、`/var`→`/private/var` 符号链接触发 `unsafe_workspace_root` 等；同一批测试在 `69420ce8` 原始源码副本上同样失败或报错。`native_worker` 标记的真实 Monty worker 测试未在本机运行。
- `tests/unit/test_turn_coordinator_cancellation.py`：16 passed，未修改。
- 冻结段语义的测试改写：原用 `request_limit`/`agent_max_model_requests` 让激活在 N 次请求后“段让出”的恢复类测试，改为测试辅助 `yield_activation_after` / `yield_each_activation_after`：第 N+1 次请求在派发准入前以可重试的 `LLMUnavailableError` 结束激活（不购买请求），Work 走真实 RETRY 路径进入 `queued`，`retry_due_now` 只把该合成退出改为立即到期的首次重试；依赖单次请求停止的 Runner 级测试改为在根 Work 写显式总额度（结果为 `suspended`）。删除 `tests/unit/test_worker_hot_limits.py`。

## 4. 文档与提示

- `development-contract.md` §2；`main-agent-runtime.md`（运行状态 `recent_work`、`RUNTIME_WORK_ENABLED`、预算段、私聊接纳/插话/失败重试、定时发生与会话锁、普通回答、验收说明）。
- `tool-kernel.md`、`persistent-subagents.md`、`pi-codemode-delivery.md`（历史记录加注）、`upgrade-4.0.0(.en).md`、插件文档 `service-facades.md`、`api-reference/facades.md`、`api-reference/results.md`、`api-7.0-migration.md`。
- `task_control` 工具描述与系统提示“持续工作”：私聊执行工具时 Host 已登记，已在工作中用 update，不再 accept。

## 5. 部署注意

- 生产 compose 显式设置的 `AGENT_MAX_MODEL_REQUESTS=24`、`AGENT_MAX_TOOL_CALLS=32` 应在部署时删除。残留不会导致启动失败（被忽略），但属于陈旧配置。
- 无数据库迁移；`runtime_work_budgets.model_limit/tool_limit` 为既有列。
- 行为变化：私聊执行失败不再静默结束，接纳后按 2/10/30 秒由 WorkScheduler 后台重试；普通执行不再在 24 次请求处停止，只有显式根预算、`/ai stop` 与 Provider 真实失败会结束执行。
- 依赖 `RUNTIME_WORK_ENABLED=true`；关闭时私聊不接纳 Work，但同样不再按段截断。

## 6. 提交与推送

- 本地提交：`d5304c0b`（E2E）、`78f7e76d`（Host 接纳与删除段机制）、`cd6b9903`（自动化排队与 recent_work）、`f03e73bc`（文档）及本记录更新。
- 已推送 `feat/execution-continuity-20261107` 并开 PR 到 main；未合并、未部署、无生产验证。

## 7. 未做与后续

- 任务书 5.1“待核实（推测）”两项未改：`stage_work_input` 只按 Person 匹配，若定时发生已成为 Work，用户私聊消息可能并入提醒任务；会话中有 SELF Work 时 `preserve_active` 的归属。需要单独核实后再处理。
- 版本核对与接纳提交之间到达的新输入：旧轮已受保护不被取消，新消息在旧轮结束后作为独立 Main 处理（不并入 Work），此窗口未专门构造 E2E。
- 插件后台 turn、Client API 等其他持有会话 Work 租约但不经会话锁的入口，用户 Main 仍可能遇到 `conversation_activation_busy`；本次只覆盖自动化与 WorkScheduler 恢复。
- Code Mode（#573）未处理：段让出路径保留但主 Agent 不再触发。
- 重复创建相同提醒属模型行为，本期不加门槛，待 `recent_work` 上线后复查。
- 生产验证、合并与部署均未执行。
