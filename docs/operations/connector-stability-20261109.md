# 连接器稳定性实施记录（阶段 0，Connector 0.4.4）

日期：2026-11-09

依据：[统一 Computer Use 与 Browser Use 任务书](../architecture/Yuki-统一ComputerUse与BrowserUse任务书-2026-11-09.md) §4.2、§4.3、§5.5 第 5 项、§5.9、§7.5、§8“阶段 0”。服务端两项（连接/断开日志、握手后服务端失败关 1011）已由 #590 合并，本记录只覆盖连接器包；Desktop 监督在 Yuki-Desktop 仓库单独交付。

## 1. 失败清单（写代码前列出）

按 §7.5 中落在连接器上的条目展开。每条写明“错误行为”，括号内为本次应有的行为。

- S1 重部署期间握手得到 HTTP 502/503，连接器退出或不记日志（按 websockets 自带退避重试，`connector.log` 有每次重试的记录，stdout 状态行为 `disconnected`）。
- S2 代理返回 404/421/426 等非 5xx 状态时连接器抛出 `InvalidStatus` 结束进程（同 S1，401/403 以外的 `InvalidStatus` 都重试）。
- S3 握手成功后服务端关 1011 时连接器停住（重连；状态行 `disconnected` → `connected`）。
- S3a 握手成功后服务端以 1008 + 其他原因（例如 `connector_hello_required`）关闭、或以 4401/4403/4001/4003 关闭时连接器当作授权丢失而永久等待（重连；服务端从不发送 `revoked` 消息与 4403，4401 在 `accept()` 前发出，uvicorn 转为握手 HTTP 403）。
- S3b 1008 + `unsupported_connector_protocol` 被当作普通断开而反复重连（明确停止重连，状态行 `protocol_unsupported`，`connector.log` 写原因，进程保持到 Desktop 关闭 stdin）。
- S4 握手 HTTP 401/403 被重试，或停住却不告知 Desktop（停止重连，状态行 `authorization_lost`，`connector.log` 写原因，进程保持到 Desktop 关闭 stdin，原生 owner 不因此被停止）。
- S5 `run` 中出现 `OSError`/`ValueError`/`httpx` 以外的异常时只打到 stderr、`connector.log` 无记录（`logger.exception` 写入 `connector.log` 后非零退出；`OSError`/`ValueError` 同样先写栈再走原有 `problem` 输出）。
- S5a 状态行与普通输出混在一起无法解析、或缓冲到退出才写出（每次状态变化立即写一行 JSON 并 flush；同一状态连续出现只写一次）。
- S5b 非 `--desktop-stdin` 的 `run` 在 stdout 打出状态行，污染 CLI/用户服务输出（只在 `--desktop-stdin` 下打）。
- S7 Desktop 关闭 stdin 后连接器在授权丢失或协议不匹配的等待中不退出（EOF 取消运行，退出码 0）。
- S9 重连或重启后重派进行中的终端/Agent 操作（不改 journal 规则：执行中断的回执保持 `unknown`，同 id 返回原回执）。
- S10 连接器侧日志无法与服务端 `connector_connected/disconnected` 对时（每次状态变化在 `connector.log` 记一行 `connector_state`，带关闭码或 HTTP 状态）。

不在连接器范围：S6（Desktop 退避与计数）、S7 的 Desktop 一侧、S8 睡眠唤醒（依赖 20 s/20 s 心跳，未改动）。

## 2. 改动

- `connector.py`：授权丢失只认握手 HTTP 401/403。删除 `revoked` 消息分支（含 `credential_revoked` DriverError）与 1008/4001/4003/4401/4403 关闭码分支；保留 1008 + `unsupported_connector_protocol` 的明确停止，其他关闭都重连。`connect(process_exception=…)` 让 401/403 以外的 `InvalidStatus` 与 websockets 默认可重试的网络错误都按 websockets 自带退避重试；删除原 `run()` 对其他 `InvalidStatus` 的重新抛出。`refresh_config()` 不加“失败沿用旧配置”的兜底。
- 状态行：`Connector` 记录当前连接状态，变化时在 `connector.log` 写 `connector_state …` 并交给 `report`；`cli.py` 只在 `--desktop-stdin` 下把它写成 stdout JSON 行并立即 flush。格式见[本机连接器 · 桌面监督与状态行](local-agent-connector.zh-CN.md#桌面监督与状态行)。
- `cli.py` 的 `run`：日志配置之后的任何 `Exception`（含取安装锁、打开 journal）先 `logger.exception` 写入 `connector.log` 再抛出；`OSError`/`ValueError` 仍走原 `problem` 输出并返回 1，其余异常由解释器打印栈并以 1 退出。`KeyboardInterrupt`/`SystemExit` 不经此路径。
- 版本 0.4.3 → 0.4.4（`pyproject.toml`、`__init__.py`、`uv.lock`、README、运维文档）；协议仍为 4。`build_runtime.py` 与 `verify_wheel.py` 从 `pyproject.toml` 读版本，无需改动。
- 运维文档：状态行、服务器主机上的回环地址（`http://127.0.0.1:18768` → `ws://127.0.0.1:18768/agents/v1/connect`），以及第 11、19 行切换前的工具名改为 `run_command`/`get_environment`/`read_command`/`control_command`/`view_file`/`send`/`copy_file`。
- 测试：删除冻结旧行为的两处断言（`close(4401, "revoked")`、`close(1008, "revoked")` 视为撤销），改为握手 HTTP 403。新增 E2E `tests/test_connector_stability_e2e.py`；开发依赖加入 `uvicorn`（锁文件中原已有，仅作为测试服务端）；CI 连接器三平台 job 上传 `test-results/connector-stability/`。

## 3. 验证

E2E 启动真实连接器进程（`python -B -m yuki_agent_connector --data-dir … run --desktop-stdin`，Windows 用 `pythonw.exe`），对进程内 uvicorn（`websockets-sansio`）上的脚本化 ASGI 服务端，每次连接尝试取一条应答；用环境变量把 websockets 退避缩短到 0.1–0.2 s。

| 运行 | 服务端依次应答 | 断言 |
| --- | --- | --- |
| retry_then_protocol_mismatch | 502、404、426、接受后 1011、接受后 1008 `connector_hello_required`、接受后 4401、接受后 1008 `unsupported_connector_protocol` | 7 次尝试后不再连接（等待 1 s 复核）；状态行 `disconnected(http_502)`→`connected`→`disconnected(close_1011)`→…→`protocol_unsupported`；日志含每次 `HTTP <状态> … reconnecting in`、`connector_state` 与协议不匹配原因；关闭 stdin 后退出码 0 |
| handshake_403 | 关闭于 `accept()` 之前（与生产路由相同，uvicorn 转为 HTTP 403） | 只尝试 1 次、无 hello；状态行仅 `authorization_lost`；日志含 `Server authorization ended`；关闭 stdin 后退出码 0 |
| handshake_401 | 502、401 | `disconnected(http_502)`→`authorization_lost`，之后不再尝试 |
| unexpected_exception | 接受后发送缺 `event_id` 的 `ack` | 进程在 stdin 仍打开时自行以非零码退出；`connector.log` 有 `Connector run failed` 与 `KeyError` 栈；stderr 有栈 |
| 普通 `run` | 握手 403 | stdout 为空（状态行只给 Desktop） |

产物：`test-results/connector-stability/classification.json`（每次运行的状态行与到达时间、服务端尝试时间线、退出码、stderr）与每次运行的 `*.connector.log`；`test-results/` 不入库，CI 以 `connector-stability-<os>` 上传。

本地（macOS arm64）：`ruff check .`、`mypy src` 通过；`pytest` 159 通过、2 跳过，6 个 `test_claude_discovery` 失败来自本机 PATH 上的 Homebrew `claude`（与本改动无关，CI 环境无此程序）。三平台结果以 PR CI 为准。

## 4. 已知与未覆盖

- websockets 在一次成功握手后把退避归零，所以“接受后立即关闭”的服务端会被立即重连（E2E 中相邻两次约 3 ms）。服务端只在自身处理失败时关 1011；若服务端持续如此，连接器会高频重连。本次按任务书保持 websockets 原语义，未另加机制，记录待观察。
- Desktop 一侧（退避重启、`connector-stderr.log`、「這台電腦」页状态与诊断导出）、S8 睡眠唤醒与 Mac mini 实机演练（杀子进程、断网、bot 重启、睡眠唤醒的三方时间线）不在本 PR，见 Yuki-Desktop 对应 PR 与后续实机记录。
