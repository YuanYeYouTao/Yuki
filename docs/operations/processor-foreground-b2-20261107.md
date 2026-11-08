# 前台处理链删减（B2，processor 部分）实施记录

日期：2026-11-07

依据：[删减任务书](../architecture/Yuki-文件膨胀与函数爆炸删减任务书-2026-11-07.md) §4.2、§5 B2。基线 main `ce518d99`（任务书行号基于 `5af28501`，本链在两者间未变）。账本 4 个死参数已随 B3（#575）删除，本批只处理 `services/processor.py`。

状态：本地修改、验证、提交并开 PR；**未合并、未上线**。

## 1. 改动

只改 `src/yuki/services/processor.py`；新增一份失败矩阵测试。不新增生产文件、包装层或配置项，不改权限。

- **删除转发层**：删除 `_handle_foreground`。它的加锁（`_ordinary_locks` 按 event id 取锁）与已接纳检查（`ordinary_already_admitted`）移到 `_prepare_foreground` 开头，原函数体整体置于同一把锁内，锁的范围与原先完全相同。两个调用方（`_handle_admitted`、`_promote_observed_input`）直接调用 `_prepare_foreground`。
- **删除 `admin_candidate` 透传**：它在 `_handle_admitted` 中计算并读取 4 次（是否拒绝、是否直接轮、触发通知、自治观察），这部分保留；进入前台链后只被转发、从未读取，删除该参数。
- **删除常真的 `direct_turn` 参数**：`_handle_admitted` 中 `if not direct_turn:` 分支以 `return` 结束，`_promote_observed_input` 传字面量 `True`，所以到达前台链时恒为真。唯一读取处 `created and (direct_turn or existing_work)` 化简为 `created`。
- **写入失败守卫合一**：`_prepare_foreground` 内 6 段相同的 `write_failure_phase(...) is not None and write_failure_committed(...) is not False: raise` 收为局部函数 `abandon(exc)`：结果未确认回滚的写入失败原样传播；否则 Duplicate → `ordinary_already_admitted`（不丢弃暂存输入），Conflict → 丢弃后 `ordinary_source_changed`，中断类 → 丢弃后 `turn_interrupted`。第一个 `try` 的三个 except 子句合为一个。覆盖任务书点名的 4 段，以及同形的再次 prepare（只可能抛 Conflict，`ordinary_admission.py:292-341` 无 Duplicate）与 `ready_work_input`（只捕获 Duplicate）两段。提交循环中的第 7 段语义不同（不丢弃、且先处理 `ordinary_participation_preparation_changed` 重试），保留原样。
- **音频失败判断合一**：`audio.error and not audio.transcript and not has_media_input and not text.strip()` 原有 3 份（`_prepare_foreground` 两处、`_handle_admitted` 的开放群发言一处），收为模块级 `_audio_failed(audio, text, has_media_input)`。

### `started` 评估：保留透传

`started` 在 `_handle_admitted` 开头取值（`_promote_observed_input` 当场取值），经 `_prepare_foreground` → `_handle_command` / `_handle_privacy_command` / `_handle_direct_plugin_command` → `_deliver_command_execution` → `_log_result`，以及 `CommandService.execute`。真实读取有三处：

1. `command_service.py:193` 的 ping 回显 `pong (x ms)`；
2. `_log_result` 的 `total_latency_seconds`；
3. `TURN_CLOSED` 通知的 `latency_ms`。

三者都是“自本处理器收到消息起”的总耗时，包含入账写入、限流和效果门等待。在使用处就近取时间会把这些阶段排除在外，ping 回显会变成命令执行自身的耗时，改变其语义；按任务书要求保留。本批随删除转发层少了一跳；没有引入替代存放位置。

## 2. 失败矩阵（先写，再改代码）

改代码前先提交 `tests/integration/test_processor_foreground_outcomes.py`（提交 `test(processor): pin foreground admission write-outcome matrix`），在基线上 41 例全过，再改代码。每例把观察到的结果写入各自 tmp 目录的 `outcome.json`。

| 组 | 情况 | 期望 |
| --- | --- | --- |
| 已接纳重复投递 | 同一私聊消息投递两次 | 第一次 `chat`；第二次 `ordinary_already_admitted`，未处理、无发送，模型只请求一次 |
| 暂存（`stage_work_input`） | Duplicate / Conflict / Interrupted / Superseded / WorkConflict × 无写入注记 / 已确认回滚 | 分别为 `ordinary_already_admitted`（不丢弃）/ `ordinary_source_changed`（丢弃）/ `turn_interrupted`（丢弃） |
| | 同上 × 已确认提交 / 提交结果未知 | 原异常对象传播，不丢弃 |
| 输入整理（`_prepare_human_input`） | 三类中断 × 四种注记 | 前两种 `turn_interrupted` 并丢弃；后两种传播、不丢弃 |
| 再次 prepare | Conflict × 四种注记 | 前两种 `ordinary_source_changed` 并丢弃；后两种传播 |
| 就绪（`ready_work_input`，暂存 id 为 7） | Duplicate × 四种注记 | 前两种 `ordinary_already_admitted` 不丢弃；后两种传播 |

其余路径由既有测试覆盖，改后均通过：直接私聊与群 @（`test_telegram_runtime`、`test_empty_input_processor`）、命令与 ping 回显（`test_group_route_recovery` 断言 `pong` 前缀、`test_participation_latency_commands::test_ping_finishes_without_waiting_for_unconsumed_binding`、`test_manual_compact_authority`、`test_compact_delivery_e2e`）、开放群与接续（`test_ordinary_input_handoff`、`test_auto_reply_merge`、`test_participation_latency_*`）、音频转写失败（`test_auto_reply_merge::test_failed_or_cancelled_wait_voice_does_not_publish_unprepared_input` 断言 `asr_no_speech` 且无接纳、无输入、无记忆任务）、超长输入（`test_overlong_work_input`）、群恢复与崩溃窗口（`test_native_group_recovery`、`test_invocation_crash_windows`、`test_invocation_process_crash`）。管理员候选的计算与读取全在 `_handle_admitted`，本批未改其逻辑，只删除了下游未读的参数，由 mypy 核对全部调用。

## 3. 度量（Python `ast`，参数不计 self/cls，行数 `end_lineno-lineno+1`）

| 项 | 改前 | 改后 |
| --- | --- | --- |
| `_handle_foreground` | 18 参数 / 46 行 | 删除 |
| `_prepare_foreground` | 18 参数 / 450 行 | 16 参数 / 444 行（含移入的加锁与检查） |
| `_handle_admitted` | 348 行 | 341 行 |
| `_promote_observed_input` | 90 行 | 88 行 |
| 写入失败守卫表达式 | 7 处 | 2 处（`abandon` 与提交循环） |
| 音频失败判断 | 3 处 | 1 处（`_audio_failed`） |
| `processor.py` | 2208 行 | 2151 行（−57） |

`git diff -w`（忽略缩进变化）：processor `+47 / −104`。函数体移入锁内使含缩进的 diff 为 `+403 / −460`。

## 4. 验证

本机 macOS（Darwin arm64），`uv sync --all-extras --managed-python` 后：

- `uv run ruff check .`：通过；`ruff format --check` 两个改动文件：通过。
- `uv run mypy --platform linux src`：698 个源文件无问题。
- 新增失败矩阵：基线 41 passed；改后 41 passed。
- 任务书 B2 指定的 `test_telegram_runtime`、`test_compact_delivery_e2e`、`test_native_group_recovery`、`test_invocation_crash_windows`、`test_invocation_process_crash`，连同新增矩阵与另外 15 个 processor 相关测试文件（含 §2 所列，以及 `test_overlong_work_input`、`test_asr_admission`、`test_sticker_text_admission_recovery`、`test_work_delivery_ownership`、`test_ordinary_input_handoff_wire`、`test_compact_cold_public_ingress`），共 21 个文件：235 passed。
- `uv run pytest tests/unit -m "not native_worker"`：3583 passed、10 skipped、14 failed。
- `uv run pytest tests/integration -m "not native_worker"`：1215 passed、12 skipped、102 deselected、1 failed。

15 个失败在基线 processor（`ce518d99`）下单独重跑结果相同，均与本机环境有关，与本改动无关：`test_manager_deployment` 2 例、`test_release_platforms` 4 例（安装脚本平台探测）、`test_shared_media_cache_mount`、`test_sticker_library`、`test_sticker_publication_recovery`、`test_terminal_environment`、`test_work_execution_receipt_regressions[False-terminal_exec]`、`test_workspace_copy_receipts` 2 例、`test_workspace_publish_versions`，以及集成 `test_plugin_frozen_binding::test_approved_hot_upgrade_does_not_replace_frozen_binding`（`WorkspaceError: unsafe_workspace_root`）。Linux CI 结果以 PR 检查为准。

未做：headed 桌面套件、线上部署验证。
