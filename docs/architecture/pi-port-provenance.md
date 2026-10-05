# 设计参考与 Monty 依赖来源

取得日期：2026-10-04；参考关系于 2026-10-05 按用户要求澄清。Pi 只作为设计思路和行为
对照的参考，不作为安装、运行或构建依赖。Yuki 的 Python 核心使用自有消息模型、固定
执行边界、持久回执及预算机制；Monty 则是实际构建、加载和执行的第三方组件。

| 项目 | 固定 SHA | 关系 |
| --- | --- | --- |
| Yuki | `8204b28ebc8939213dae60dbab94ab1c16d1263a` | 本项目基线；保留原项目许可 |
| Pi | `200387122ca450d6387f033949423114a270b96c` | 设计参考与行为对照；不加入依赖清单或制品 |
| Monty | `3f9d6ef413fb951e5b80113b7088d535bd028fcb` | 实际依赖，MIT，Copyright Pydantic Services Inc.；598 个锁定依赖已逐归档校验；typeshed 原 Apache 2.0 许可已保留，8 个完整 notice 缺口仍记录在审计中 |

早期记录中的“语义移植”统一改为“参考行为、在 Yuki 合同下实现”。当前没有 Pi SDK、
TypeScript 源文件或 Pi 的依赖链进入运行环境。下表保留查阅对象以便复核思路，不将
行为相似或名称对应当成包依赖。历史构建曾包含 Pi 许可文本，原证据仍保留；当前
wheel 和 Dockerfile 已取消这项打包配置。本次改动不改变模型循环的可执行逻辑。

旧参照 `755f7250e0ac465e57e748ea2e6583d1a76353b0` 是当前基线祖先，实际相差 65 提交。
旧文件较短不表示较新，也不能据旧包缺失删除当前 `DurableInvocations` 所有者。

## 参考行为与现行实现

| 固定参考 | 参考行为 / 实际接口 | 目标 | 当前状态 |
| --- | --- | --- | --- |
| Pi `packages/agent/src/agent-loop.ts` L102-L327 | 响应、工具结果和下一请求的轮次顺序；Yuki 用三个固定执行边界与请求预算实现 | `agent_core/loop.py` | 已实现；`test_agent_core_loop.py`、`test_agent_core_differential.py` |
| 同上 L381-L470 | 完整 / 不完整响应的区分；Yuki `collect_response` 仅组装合成 frame，真实请求仍经 TaskModelExecutor | `agent_core/model_boundary.py` | 已实现（仅合成 frame）；`test_partial_frames_are_frozen_and_only_done_completes` |
| 同上 L478-L503、L689-L691 | 截断调用不执行、批次结算后决定继续；实际回执和派发仍由 Coordinator＋InvocationService 持有 | `agent_core/loop.py`、`InvocationBoundary` | 已实现；`test_truncated_response_fails_every_call_in_band_without_dispatch` |
| Pi `packages/agent/src/agent.ts` L565-L611 | 事件投影思路；Yuki 使用不可变状态和有界同步队列，不采用 Pi 的可变 Agent 实例与异步流 | `agent_core/state.py`、`events.py` | 已实现；`test_event_order_and_state_reduction`、`test_event_backpressure_drops_diagnostics_never_execution` |
| Pi `packages/agent/src/types.ts` L147、L514-L529 | 继续 / 结束和轮次事件的思路；Yuki 使用自有 dataclass、ChatResponse 和 ToolCall | `agent_core/types.py` | 已实现 |
| Monty `crates/monty-python`（`pydantic-monty-client` 1.0.1） | AsyncMonty、Function/NameLookup/FutureSnapshot、手动 resume、dump/load_snapshot | `codemode/engine_monty.py` | 已接入，真实 worker 验证 |
| Monty `crates/monty-pool/src/worker.rs:185-194` | 原生 subprocess：`env_clear`、piped stdio、`kill_on_drop` | 经绑定使用，不另实现传输 | 已验证（worker-only 构建） |

Pi SDK、pi-ai Provider、durable/chord、CLI/TUI/RPC 均不进入本交付。
实际 Monty 依赖及其传递依赖的来源、许可和缺口继续按原审计保留。

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

Pi 上游自带 TypeScript 测试未运行；本轮核验 Yuki Python 实现、原行为黄金样本与实际应用装配，
不宣称上游测试通过。本地依赖安装/编译已获授权。

## P10 后的生产调用与责任边界

`AgentRunner._run`、生产 Callbacks bag/union、`begin_batch`、工具 legacy execute adapter、
Runner/Turn/Coordinator 的 duck-typed fallback、Worker 动态 `__getattr__` 已删除。
所有主入口仍经 MainAgentTurnService→Runner→TurnExecution→`run_agent_loop`。
生产没有旧/新循环选择 flag，没有备用 model loop。

- **唯一核心**：迭代、turn 顺序、完整响应工具顺序、截断不执行和 `agent_end`；不依赖 Work、
  SQLite、QQ、长期记忆或发送器。模型/调用/结算由三个固定 typed protocol 表达，不是可注册 hooks。
- **TurnExecution**：声明/来源/Work 与普通输入、恢复检查点、响应观察和业务结算；`TurnState`
  38 项状态归一次激活。原请求的准备、派发和观察是独立方法。
- **Admission**：主请求 `_PrimaryDispatch`、普通摘要 `_OrdinarySummaryDispatch`、Work 页
  `_WorkSummaryDispatch` 各持有原候选/计量状态，HTTP 重试复用 paid reservation；辅助页不覆盖主 journal。
- **调用**：Coordinator→InvocationService→明确 `execute_call(Invocation)`；Code Host 用同一
  原身份、授权、T1/T2/T3、控制和回执路径。Worker 有固定 forwarders 与独立权限/声明子集。
- **测试兼容**：Core Callbacks 只在 tests/support；Fake Provider 在 Runner 外显式规范化。
  原异常/容量 fixture 迁到新边界，原断言保留。

差分黄金样本 `tests/fixtures/agent_core/runner_golden.json` 取自核心拆分前 `b4fdef7d`，未重生成。
P10 的四组比较临时读取同一固定历史 SHA，主迭代不改，两组共享 Invocation/Code kernel，
不把旧 loop 放进生产或作为永久 CI 依赖。结构原始证据在
`pi-codemode-evidence/p10-retirement.json`，四组实际计量在 `p10-comparison.json`。

## P09 固定分发与许可证据（2026-10-05）

共用 `scripts/build_monty_distribution.sh` 从精确 Monty SHA 和记录的唯一补丁构建
worker-only runtime 与 CPython 3.12 binding；固定 Rust 1.96.0 / maturin 1.9.6。
本机重复构建 native SHA256 相同：
`af76448a14fe980c823f1c6092692f90a8ddc6341f236f17d6d47d72f49548e8`。
本轮 wheel SHA256 为 `5f77cfbcf15ca0e0bb405d6d586aeb98aacbbcfc10555ba0b882c1b73868aa6f`；
wheel 包装时间可改变归档哈希，不能据 worker 相同宣称 wheel 逐字节可复现。

实际第三方组件完整许可在 `vendor/monty/LICENSE` 和 `vendor/monty/TYPESHED-LICENSE`。
P09 的历史构建也曾打包 Pi 许可文本并核对全文；这是该次构建的实测事实，当前参考关系
澄清后不再将这份文本作为 Yuki wheel 或镜像的必需内容，旧报告不覆盖更新后的制品。
`vendor/monty/THIRD_PARTY_NOTICES.json` 是 Darwin 实际 worker/wheel 对应的审计：
598 个锁定包逐 `.crate` 校验 Cargo checksum，352 个当前 normal/build 目标依赖，
321 份去重完整 notice；不将锁内所有包冒充镜像实际依赖。原 SPDX 选择表达式保留，
不替许可人选择。typeshed 固定 `0e16ea31d2e188fdc126cb31e7c4fcc6b5a8da96`。

8 个包没有可核对的完整文本，其中实际建置目标包含 quote-use 和 quote-use-macros
0.8.4；准确清单、精确 source commit 和缺口在 JSON 与根 THIRD_PARTY_NOTICES.md。
审计明确 `license_text_audit_complete=false`，没有合成版权署名；外部发布未执行。
Linux 原生分发已构建/安装，353 个目标依赖及实际 hashes 见 `pi-codemode-evidence/p09-linux-distribution.json`；321 份全文逐字节与共享审计核对一致。
应用与验证镜像构建及包装探针通过，证据见 `pi-codemode-evidence/p09-container-packaging.json`。
两个镜像保留各自实际 worker/wheel hashes；不把 native Host 的隔离通过写成默认容器隔离通过。
