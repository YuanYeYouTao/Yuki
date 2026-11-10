# 文件复制第二期：artifact 与本人电脑写入 Yuki 环境（2026-11-10）

基线：main `10170aff`。对应[服务器文件传到本人电脑任务书](../architecture/Yuki-服务器文件传到本人电脑任务书-2026-11-07.md) F08。本记录先列失败情况，再写实现与验证；只记本地修改与本地验证，提交、PR、合并与上线以 Git 与部署回执为准。

动机：2026-11-10 生产中 Yuki 用 `copy_file(path, device, destination)` 把本人 Mac 上的文件放进自己的环境，得到 `source_not_supported_for_destination`。

## 1. 失败情况（实现前列出）

范围：`artifact_id` → Yuki 环境、`path`+`device`（本人电脑）→ Yuki 环境，以及随之可用的 `path`+`device` → `to_device`（电脑到电脑）。工具 schema 不变（任务书 §4.1）。

| # | 情况 | 期望事实 |
| --- | --- | --- |
| E1 | 同一 request_id 在成功后重放（源已删除或改变） | 先读 Manager 原回执，原样返回；不读 artifact blob、不 stat 设备文件；目标 inode 不变 |
| E2 | Manager 在接收字节途中重启 | 回执在读第一个字节前已持久为未知；恢复返回 `file_mutation_unknown_read_current_version`、`uncertain`，不读源、不重写；目标目录可能残留 `.yuki-write-*`，提示可在环境终端核对 sha256 |
| E3 | Bot 在推送途中断开（Bot 重启或源读取出错） | Manager 读到短流，记 `source_stream_incomplete`，临时文件删除，目标不变；同 request_id 恢复返回这一确定失败，不重跑 |
| E4 | 字节已全部送达、Bot 未收到结果 | 首次为未知（`sandbox_unavailable`、`uncertain`）；同 request_id 恢复等在途写入结束并读到原成功回执，不读源 |
| E5 | 收到的字节与声明的 sha256 不符 | `version_conflict`，临时文件删除，目标不变 |
| E6 | 设备源文件在读取途中改变 | Connector 报 `file_changed`；目标不变；Manager 回执为 `source_stream_incomplete` |
| E7 | 源电脑离线 | `device_file_read_unavailable`；Manager 不产生回执，目标不存在 |
| E8 | 源电脑关闭完整访问 | `full_access_disabled`；目标不存在 |
| E9 | 可变旧 artifact | `artifact_not_immutable`；Manager 不产生回执 |
| E10 | 不存在的 artifact | store 原错误；Manager 不产生回执 |
| E11 | 目标在 `/workspace` 之外、目标是已有目录、父目录不存在、目标已存在 | `path_outside_workspace` / `IsADirectoryError` 确定失败且目标不变 / 自动建父目录 / 原子覆盖 |
| E12 | 同 request_id 不同参数 | `idempotency_conflict` |
| E13 | 两个调用并发写同一环境目标 | 都成功；最终目标等于其中一份完整源 |
| E14 | 0 B、恰好 128 KiB、1 MiB+1、12 KB 中文 Markdown | 源与目标 sha256、size 一致 |
| E15 | 环境盘满（ENOSPC/EDQUOT） | 沿用 `workspace_full`（与 `workspace_copy` 同一映射）；不另测 |
| E16 | 电脑 → 电脑 | 经 `upload` 原分片写入，sha256 一致；先查 `upload_result`，成功不读源 |
| E17 | `device` 与 `artifact_id` 或附件同给 | `choose_one_source` |
| E18 | Bot 在读源阶段（Manager 尚未收到请求）重启 | 恢复时 `workspace_copy_result` 为未找到，重新读源并写入，结果正确 |
| E19 | 64 MiB 吞吐 | 记录耗时，不设门槛 |

## 2. 改动

### Manager（`sandbox/persistent.py`，需 Manager 同源部署）

- 新私有流 `workspace_write`：请求行带 `args = {destination, source_key, size, sha256}` 与原 `request_id`。
  - 同一 request_id 有在途写入时先等它结束；已有回执直接回原回执（同 ID 不同参数为 `idempotency_conflict`），不收字节。
  - 否则在 `environment_mutations` 写入未知回执（与 `workspace_copy` 同一格式，`workspace_copy_result` 原样可查），再回 `{"ready":true}`。
  - 字节经原 `FileWorkspace._write` 写入目标目录的 `.yuki-write-*`，核整文件 sha256（即声明的 sha256）后原子替换；不符为 `version_conflict`，流短为 `source_stream_incomplete`，盘满沿用 `workspace_full`；结果更新原回执后回给 Bot。
  - 早期失败（目标越界、目标为目录等）在回执落定后收完声明的字节再回答，Bot 读到确定失败而不是断流。
- 写入任务登记在原 `_file_tasks`/`_file_requests`：连接断开不取消写入，`workspace_copy_result` 等在途写入结束，Manager 关闭等它收尾。

### Bot

- `SandboxClient.write(args, stream, request_id)`：打开 `workspace_write`，收到 `ready` 后按 64 KiB 推送 `size` 字节；连接失败报 `sandbox_unavailable`（发出请求后为 `uncertain`）。源读取出错直接抛出原错误并关闭连接。
- `LocalDeviceService.open_file`：设备文件的 stat（size、sha256、版本）加按需读取原分片的读取器，结束 release；`download()` 改为在其上落临时文件并核哈希（`view_file`、`send(device)` 行为不变）。
- `WorkspaceService.copy_file`：删除 `source_not_supported_for_destination`；`device` 只能配 `path`，否则 `choose_one_source`。
  - path/附件 → 环境：原 `workspace_copy` 不变。
  - artifact、电脑 path → 环境：先 `workspace_copy_result`（source_key `artifact:<id>` / `device:<设备>:<path>`），找到即返回；否则打开源（blob 或 `open_file`）并 `SandboxClient.write`。
  - 任一来源 → 电脑：原 `upload_result` → `upload`，新增电脑 path 来源（电脑到电脑）。
  - 环境写入未确认时的说明改为残留 `.yuki-write-*`、在自己环境的终端核对。
- 工具 `copy_file` 描述：`path（可带 device 表示源在本人电脑）`；schema 不变。删除 `source_not_supported_for_destination` 的模型说明。

### 文档

`chat-media-workspace.md`、`tool-kernel.md`、`local-agent-connector.zh-CN.md` 同步新方向与恢复语义；任务书头部指向本记录。

## 3. 失败情况 → 测试

E2E `tests/integration/test_file_copy_phase2_e2e.py`：隔离 SQLite、TLS WSS 桥、真实 Connector 包（0.5.0 / 协议 4）进程内运行、`PersistentManager(testing=True)` 的 Unix socket；E14/E16 经模型声明的 JSON Schema 校验后由 direct 工具分发器调用，其余直接调服务。产物 `test-results/file-copy-phase2-<commit12>.json`（`test-results/` 不入库）。

| # | 用例 |
| --- | --- |
| E1 | 删除设备源，`open_blob`/`open_file` 换成断言失败，同 ID 重放 artifact→环境、电脑→环境、电脑→电脑：原回执，inode 不变 |
| E2 | 第二片读取挂起时，在同一 root 上新起一个 Manager（模拟重启）并经它恢复：`file_mutation_unknown_read_current_version` + `uncertain` + `.yuki-write-*` 说明，不读源；重启时刻目录中确有 `.yuki-write-*` |
| E3 | 放行后源读取报错：Bot 抛 `device_file_read_unavailable`；同 ID 恢复得到确定的 `source_stream_incomplete`，无残留 |
| E4 | Manager 写完后丢弃结果行：首次 `sandbox_unavailable` + `uncertain` + `source_sha256`；同 ID 恢复读到成功，inode 不变 |
| E5 | 直接 `SandboxClient.write` 声明错误 sha256：`version_conflict`，原文件不变，无残留 |
| E6 | 第二片前改写设备源：`file_changed`；Manager 回执为 `source_stream_incomplete` |
| E7 | Connector 未连接：`device_file_read_unavailable`，Manager 无回执 |
| E8 | `full_access=False`：`full_access_disabled` |
| E9/E10 | 可变 artifact `artifact_not_immutable`、未知 artifact `artifact_not_found`，Manager 无回执 |
| E11 | 1 MiB+1 artifact 写 `/etc/x`：确定的 `path_outside_workspace`（非未知）；目标为目录：`IsADirectoryError`，目录保留、无残留；`in/a/b/c/` 自动创建；已有文件被覆盖 |
| E12 | 成功的 request_id 换 destination：`idempotency_conflict` |
| E13 | artifact 与电脑文件并发写同一目标：均成功，最终 sha256 一致 |
| E14 | 四种尺寸 × artifact→环境、电脑→环境（经模型工具），sha256 一致 |
| E15 | 未测（沿用 `workspace_copy` 的盘满映射） |
| E16 | 四种尺寸电脑→电脑（经模型工具），sha256 一致 |
| E17 | `device` + `artifact_id`：`choose_one_source` |
| E18 | 设备 stat 挂起时取消调用，同 ID 再调：读源一次并成功 |
| E19 | 64 MiB artifact→环境、电脑→环境，记录耗时 |

`tests/integration/test_file_copy_e2e.py` 删除冻结旧拒绝的两处断言（artifact→环境、源 `device`）。

## 4. 部署

- **需要 Bot 镜像与 Manager 同源部署**：Manager 代码由 `deploy/sandbox/sync-manager.py` 从所选 Bot 镜像导出，之后重启 `yuki-sandbox`。
- 只更新 Bot、Manager 仍为旧版时：`workspace_write` 被旧 Manager 回 `unknown_method`，复制确定失败、不写任何文件；其他功能不受影响。
- 环境容器、Connector（0.4.3 / 协议 4 起即可，只用原 stat/read/release）与 Desktop 都不需要改动。
