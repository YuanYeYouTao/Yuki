# 文件复制后端与运行资料当前时间实施记录（2026-11-07）

基线：main `69420ce8`。分支 `feat/file-copy-backend-20261107`。本记录只写本地修改与本地验证；提交、PR、合并与上线分别以 Git 与部署回执为准。

对应任务书：[Yuki 服务器文件传到本人电脑任务书](../architecture/Yuki-服务器文件传到本人电脑任务书-2026-11-07.md) 的 F01–F03 与 F06 中 Bot 侧部分；[#566](https://github.com/YuanYeYouTao/Yuki/issues/566) 运行资料当前绝对时间。

## 1. 范围

本次是 **Bot-only** 改动。没有改宿主 Manager（`sandbox/persistent.py`、`workspace/files.py` 等环境侧代码）、`packages/yuki-agent-connector` 或 Yuki-Desktop，它们不需要重新部署。

没有改工具名。模型可见的 `save_conversation_attachment_to_workspace` 只做最小扩展：新增可选 `path`、`artifact_id`，`event_id`/`attachment_index` 不再必填，`device_id` 仍指写入目标电脑。服务层 `WorkspaceService.copy_file` 已按最终参数实现（来源三选一，`device` 指源电脑，`to_device` 指目标电脑）。工具面一次切换时把声明改名为 `copy_file` 并直接接上这组参数，不再改服务层。

`time_get_current` 保留，删除放在工具面切换。

## 2. 改动

### 文件复制（F01–F03、F06 Bot 侧）

- F01 `LocalDeviceService.upload(device_id, actor, stream, size, sha256, destination, request_id=…)`：调用方给出按字节数读取的流、size 与 sha256；删除内部整文件预哈希与 fstat stamp 校验。读不满所需字节报 `source_stream_incomplete`。整文件内容仍由 Connector 末片按传入的 sha256 核对后才 `os.replace`。
- 同时修正一个既有缺陷：Connector 0.4.3 把非末片写入的回执折叠进 journal 状态，`status` 仍为 `unknown`，旧代码据此在第一片后返回 unknown，**大于 64 KiB 的文件写入本人电脑从未完成**（包括原有附件分支）。现在以回执中记录的 `next_offset` 确认非末片；真正未确认的片仍返回原 unknown。该缺陷由新 E2E 首跑发现。
- F02 `WorkspaceStore.open_blob(artifact_id)`：返回 metadata 与已打开的 blob 流，由调用方关闭；`read_bytes` 复用它。复制沿用 `send(artifact_id)` 的 immutable 条件，可变旧 artifact 报 `artifact_not_immutable`。授权与 `send(artifact_id)` 相同，凭 ID 读取，不新增可见范围门。
- F03 `WorkspaceService.copy_file`：
  - 来源三选一，执行处单点检查，错误 `choose_one_source`。源电脑（`device`）和 artifact → Yuki 环境报 `source_not_supported_for_destination`（需要 Manager 新方法，第二期）。
  - 写入本人电脑：`transfer_id` 为原 invocation key。先查 `upload_result`，成功原样返回、不读源；从未记录才读源；其余保持 unknown。
  - `path` → 电脑：经 Manager 现有 `files_read` 流，直接按头部 size/sha256 交给 upload，Bot 不落临时文件。
  - `artifact_id` → 电脑：F02 的 blob 流。附件 → 电脑 / 环境：保持原分支。
  - `path` → Yuki 环境：复用 Manager `workspace_copy`，`source_key` 为 `path:<path>`，由 Manager 原回执保证同 request 不重做。
  - 未确认的电脑写入在原回执上附 `source_sha256`（已读源时）和说明：目标目录可能残留 `.yuki-transfer-*.part`，可在本机终端核对 sha256，不按失败重传同一调用。
  - 权限体系未动：path 来源同终端与 `send(path)`，artifact 来源同 `send(artifact_id)`，目标电脑仍走原设备授权。
- F06 Bot 侧：`SandboxClient.stream` 在 Manager 未回头部即关闭连接时报 `stream_closed_before_header`，不再抛 JSON 解析异常；复制把它与头部错误统一为 `source_file_unavailable`，连接失败为 `sandbox_unavailable`。流内容按头部 size 读满，内容由 Connector 末片按头部 sha256 核对，Bot 不另做整文件哈希。

### #566 运行资料当前时间

- `yuki.time.formatting.runtime_current_time` 统一以 UTC+8 显示，如 `2026-11-07 21:30:05 星期六 (UTC+8)`。
- `TurnExecution._runtime_message` 在每份运行资料前加 `current_time`，取准备该资料时的系统时钟。首个新请求因此总有运行资料块（以前全空时不展示）。
- 自动化 `activation_context` 删除自己的 UTC `current_time` 与 `now` 参数，四个调用方直接传方法；运行资料只有一个时间来源。
- 新时间来源：每次激活（用户消息、Work 等待唤醒后的恢复、自动化到期、SELF）的首个新请求准备一次；同一激活中程序追加新运行资料（参与资料）时随之刷新；工具往返和 HTTP 重试保留已提交原件，不改写旧请求。长 Work 中途需要新时间调用 `time_get_current`。精确恢复原已付请求不另加资料。没有新增状态。
- 系统提示 `prompting/contracts.py` 说明 `current_time` 的含义和中途取新时间的方法。

### 文档

- `chat-media-workspace.md`：附件保存的三种来源；更正原第 163 行把该工具的 `device_id` 写成“原文件所属电脑”的错误（它指写入目标电脑），补写传输恢复语义与错误码。
- `tool-kernel.md`、`docs/operations/local-agent-connector.zh-CN.md`：同步 `device_id` 方向与新来源。
- `main-agent-runtime.md`：运行资料当前时间与新时间来源。

## 3. 验证

在本 worktree：`uv sync --all-extras --managed-python`。

- `uv run ruff check .`：通过。
- `uv run mypy --platform linux src`：通过（698 个源文件）。
- E2E `tests/integration/test_file_copy_e2e.py`：隔离 SQLite、桥接 TLS WSS、真实 Connector 0.4.3 / 协议 4、`PersistentManager(testing=True)` 的 Unix socket；主要用例经模型可见声明的 JSON Schema 校验后由 direct 工具分发器调用。产物 `test-results/file-copy-<commit12>.json`（`test-results/` 不入库），见 §4。
- #566：`tests/integration/test_current_material_main_contract.py` 经真实 Chat 部署与 Gemini / DeepSeek Responses 线路请求核对首个请求的 `current_time` 格式、星期与请求时间窗口，产物 `test-results/runtime-current-time/{gemini,deepseek}.json`；自动化到期测试同步为新格式；`test_participation_runtime_tail` 中“无资料不加块”改为“只有当前时间”。
- 冻结旧 upload stamp 行为的 `test_upload_source_change_stops_before_repeating_an_eof_chunk` 已删除（源变化由 E2E 用例 5 覆盖）；其余旧签名测试改为新签名。
- 全量：见 §5。

提交：`43e82df6`（文件复制）、`4ccef416`（#566）、`f58dee80`（#566 测试修正）及本记录所在提交。

macOS 上 pytest 默认临时目录过长，Unix socket 会报 `AF_UNIX path too long`，本地运行需加短 `--basetemp`（例如 `/tmp/ykt/...`）；这与本次改动无关。

## 4. E2E 产物摘要

产物 `test-results/file-copy-4ccef416bc34.json`：commit `4ccef416bc34887e282a941bd9ba2efbf96798b3`，工作树干净，Connector 0.4.3 / 协议 4，本机 loopback（与全量测试并行运行，耗时仅作量级参考）。

| 用例（任务书 §8） | 结果 |
| --- | --- |
| 1 固定种子 12341 B 中文 Markdown、1 MiB+1、0 B、恰好 128 KiB：path → 电脑、artifact → 电脑、path → Yuki 环境 | 12 次全部成功，源与目标 sha256、size 一致；单次 0.005–0.22 s |
| 1 附件（12420 B PNG）→ 电脑、→ Yuki 环境 | 成功，sha256 一致 |
| 2 成功后删除源，以同一 request_id 重放 path / artifact / 环境复制 | 返回原回执；`files_read`、`open_blob`、附件读取被替换为断言失败仍通过，即未读源；目标 inode 不变 |
| 3a 末片之前断开 WebSocket | unknown，回执附 `source_sha256` 与 `.part` 说明；重连后同 request_id 恢复仍为 unknown，不读源、不重写，目标不存在，残留 1 个 `.yuki-transfer-*.part` |
| 3b 末片已写入、确认丢失（Bot 侧模拟回执丢失） | 首次 unknown；同 request_id 恢复读到 Connector succeeded，未读源，inode 不变 |
| 4 关闭完整访问 | 新写入报 `full_access_disabled`，目标不存在；`get` 仍读到原回执 |
| 5 传输中改写源 | 目标为读取开始时的快照（Manager 先快照再发头部），无混合内容 |
| 6 覆盖已有目标 / 目标为已存在目录 / 父目录缺失 | 覆盖成功；目录与缺失父目录按 0.4.3 现状为 unknown（见部署注意） |
| 7 首次调用设备离线；符号链接、硬链接源；可变 artifact；两个来源；artifact → 环境；同目标并发 | unknown；`source_file_unavailable`；`artifact_not_immutable`；`choose_one_source`；`source_not_supported_for_destination`；并发两次均成功，最终目标等于其中一份完整源 |
| 8 64 MiB path → 电脑 | 成功，sha256 一致；6.2 s（10.3 MiB/s，与全量测试并行）；单独运行时 4.5 s（14.4 MiB/s） |
| 9 旧名历史回执投影 | 不在本次范围（工具面切换） |

#566 产物 `test-results/runtime-current-time/{gemini,deepseek}.json`：首个请求运行资料为 `{"current_time":"2026-11-08 06:03:01 星期日 (UTC+8)","state":"no_active_work",…}`，落在记录的请求时间窗口内。

## 5. 全量测试

`uv run pytest tests/unit tests/integration -q -m "not native_worker"`（macOS，`--basetemp` 为短路径）在提交 `4ccef416` 的代码上：**4758 passed、13 failed、22 skipped、102 deselected**，用时 21 分 35 秒。

- 5 项由本次 #566 引起：`test_compact_trigger::test_ordinary_invalid_paid_checkpoint_is_reused_after_cold_restore`、`test_external_event_prompt_isolation::test_external_wakeup_and_ordinary_turn_send_the_same_provider_shape`、`test_plugin_session_integrity` 三项。它们冻结了“没有其他资料时不加运行资料块”。现在每个新请求都带 `current_time`，测试改为认这一块；冷恢复测试另以每次递增的假时钟确认：恢复后新请求准备自己的时间，其余消息与原请求一致，已付压缩检查点仍复用、不再次请求模型。修正后这三个文件 34 项通过（提交 `f58dee80`）。
- 8 项在基线 `69420ce8` 上同样失败，属于本机 macOS 环境：`test_release_platforms` 四项（BSD `script` 不支持 `-c`）、`test_shared_media_cache_mount`（读 `/proc/meminfo`）、`test_terminal_environment::test_native_explicit_timeout_keeps_written_file_and_real_exit`、`test_work_execution_receipt_regressions::…[False-terminal_exec]`、`test_plugin_frozen_binding::test_approved_hot_upgrade_does_not_replace_frozen_binding`。

修正后没有再跑一次全量；Linux CI 结果以 PR 检查为准。

## 6. 部署注意

- 只需重新部署 Bot 镜像；Manager、Connector、Desktop 不变。
- 生产仍是 Connector 0.4.3：目标为已存在目录（如 `~/Documents`）时 `os.replace` 失败、父目录不存在时 staging 打开失败，都回报 unknown，可能残留 `.part`。模型需写完整文件路径。
- `files_read` 只接受 `/workspace` 下普通文件；`/inputs/media-cache` 内文件写入电脑请走附件来源，或先在终端复制到 `/workspace`。
- 首次调用时设备离线，回执也是 unknown（Connector 无记录可查）。

## 7. 后续

- Manager：serve 捕获 `WorkspaceError` 并回原错误码（现为断开或 `invalid_request`，Bot 统一为 `source_file_unavailable`），随下一次 Manager 部署。
- Connector 0.4.4：目录目标写入“目录/源文件名”、offset 0 时 `mkdir(parents=True)`、replace 前 OSError 转 DriverError；非末片回执是否显式带确认状态一并评估（Bot 已按 `next_offset` 读取，不依赖它）。Desktop 重新打包。
- 工具面一次切换：`save_conversation_attachment_to_workspace` 改名 `copy_file`，参数 `device`（源）/`to_device`（目标）/来源三选一，旧名进入统一只读映射；同时删除 `time_get_current` 并改写系统提示中取新时间的说明。
- 第二期：artifact → Yuki 环境、本人电脑 → Yuki 环境（Manager 新方法）。
