# Pi 与 Monty 固定来源

取得日期：2026-10-04。以下源码已用 Git 按精确 SHA 获取；尚未完成的移植不标已实现。

| 项目 | 固定 SHA | 许可 |
| --- | --- | --- |
| Yuki | `8204b28ebc8939213dae60dbab94ab1c16d1263a` | 保留原项目许可 |
| Pi | `200387122ca450d6387f033949423114a270b96c` | MIT，Copyright 2025 Mario Zechner |
| Monty | `3f9d6ef413fb951e5b80113b7088d535bd028fcb` | MIT，Copyright Pydantic Services Inc.；Cargo.lock 598 个转依赖与 vendored typeshed 的 notice 汇总待 P09 镜像打包时生成 |

旧参照 `755f7250e0ac465e57e748ea2e6583d1a76353b0` 是当前基线祖先，实际相差 65 提交。
旧文件较短不表示较新，也不能据旧包缺失删除当前 `DurableInvocations` 所有者。

## 源符号与目标责任

| 固定源 | 采用符号/结构 | 目标 | 当前状态 |
| --- | --- | --- | --- |
| Pi `packages/agent/src/agent-loop.ts` L102-L327 | `runAgentLoop/runAgentLoopContinue/runLoop` → `run_agent_loop` | `agent_core/loop.py` | 已移植；`test_agent_core_loop.py`、`test_agent_core_differential.py` |
| 同上 L381-L470 | `streamAssistantResponse` → `collect_response`（合成 frame）；真实请求仍由 Runner `_request` 经 TaskModelExecutor | `agent_core/model_boundary.py` | 已移植（仅合成 frame）；`test_partial_frames_are_frozen_and_only_done_completes` |
| 同上 L478-L503、L689-L691 | `failToolCallsFromTruncatedMessage` → `fail_truncated_calls`；`shouldTerminateToolBatch` → `ToolBatchOutcome.terminate`；prepare/execute/finalize 保留原 Coordinator＋InvocationService | `agent_core/loop.py`、`InvocationBoundary` | 已移植；`test_truncated_response_fails_every_call_in_band_without_dispatch` |
| Pi `packages/agent/src/agent.ts` L565-L611 | `processEvents` → `state.reduce`；listener fan-out → `EventStream`（有界、不阻塞） | `agent_core/state.py`、`events.py` | 已移植；`test_event_order_and_state_reduction`、`test_event_backpressure_drops_diagnostics_never_execution` |
| Pi `packages/agent/src/types.ts` L147、L514-L529 | `AgentTurnDecision` → `Continue/End`；`AgentEvent`；`AgentToolCallOutcome` → `ToolCallOutcome` | `agent_core/types.py` | 已移植 |
| Monty `crates/monty-python`（`pydantic-monty-client` 1.0.1） | AsyncMonty、Function/NameLookup/FutureSnapshot、手动 resume、dump/load_snapshot | `codemode/engine_monty.py` | 已接入，真实 worker 验证 |
| Monty `crates/monty-pool/src/worker.rs:185-194` | 原生 subprocess：`env_clear`、piped stdio、`kill_on_drop` | 经绑定使用，不另实现传输 | 已验证（worker-only 构建） |

Pi 完整 SDK、pi-ai Provider、durable/chord、CLI/TUI/RPC 不进入本交付。
实际移植文件必须带原 MIT 许可和署名，并在本页记录目标符号、源范围与语义测试。

## 刻意差异

- Yuki 固定声明，不采用 Pi 的动态 `declareToolChanges` 策略。
- 保留严格 JSON Schema，不移植 TypeBox 数值/布尔/null coercion。
- 有界读取并发；修改、发送和控制保留 Yuki 屏障，不能采用默认无限并发。
- 普通失败保存 receipt；取消、租约和存储故障继续交原 owner，不能统一扁平化错误。
- Work inputs 是持久输入真源，不复制 Pi 内存 queue 作为恢复账本。
- 当前 complete Provider 映射完整 frame；partial 只用于边界测试且不得执行工具。
- Provider 原私有签名/opaque 不经业务 JSON canonicalizer 重写。
- 原领域回执是效果证据；Pi 事件、nested calls 和 VM return 不承担持久真源。

各差异的 fixture（`tests/unit/test_agent_core_differences.py`，除注明外）：

| 差异 | fixture |
| --- | --- |
| 固定声明，无 `declareToolChanges` | `test_declarations_are_fixed_not_dynamically_announced` |
| 严格 JSON Schema，无 coercion | `test_strict_schema_never_coerces_like_typebox` |
| 有界读取并发，非 `Promise.all` | `test_parallel_reads_are_bounded_not_promise_all` |
| 发送屏障；控制必须单独成批 | `test_send_is_a_barrier_between_read_stretches`、`test_lifecycle_control_cannot_share_a_batch` |
| 业务失败保持原 receipt；Host 异常交 owner | `test_business_failure_receipt_reaches_model_unflattened`、`test_host_exceptions_go_to_their_owner_not_into_a_tool_result` |
| partial 不执行工具 | `test_agent_core_loop.py::test_incomplete_frames_through_loop_never_execute` |
| 核心不依赖平台/数据库 | `test_agent_core_loop.py::test_core_has_no_platform_or_database_dependencies` |

Pi 上游自带测试未在本机运行（需要 npm 依赖安装，未授权），不宣称上游测试通过。

## P03 后 Runner 仍保留的职责

`AgentRunner._run` 不再自己推进循环：迭代、turn 顺序、截断不执行、`agent_end`
均由 `run_agent_loop` 拥有。Runner 以 `Callbacks` 绑定三个边界，以下内容留在边界闭包中：

- `_begin`/`_steer`/`_request`（模型边界）：辅助请求与段预算、公开观察边界、Work 输入、
  声明与 native 合并、Work/普通 compaction、`dispatch` 与持久 `dispatched` 存档、空响应有界重试。
  这些依赖 TaskModelExecutor、WorkSession 与 ContextBoundary，核心不得导入。
- `_execute_tools`（调用边界）：原 `_execute_tool_batch`、Coordinator、InvocationService。
- `_settle_*`/`_finish_tool_turn`/`_exhausted`（回合结算）：提及占位、未支持终答、交互退出、
  隐式 complete 校验、证据与 Work 存档、重复批次检测。

原单一作用域的跨步变量（约 25 个）由闭包 `nonlocal` 共享；P05/P08 收敛为显式 turn state 时
需再拆分，并删除 `begin_batch` 兼容调用（P10）。差分黄金样本
`tests/fixtures/agent_core/runner_golden.json` 取自移植前 `b4fdef7d` 的 Runner 循环。
