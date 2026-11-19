# 外部 Agent 委派第二阶段：真实 Agent 端到端验收（§10.3 A1、A2、A4–A8）

2026-11-18，macOS（arm64），本机隔离运行。本地运行的验收（真实 Agent，按需付费调用），产物随 PR #639 提交；没有上线。复现命令见 [`commands.sh`](commands.sh)，测试在 `tests/acceptance/`（标记 `real_agent`，只有 `YUKI_REAL_AGENT_E2E=1` 才运行，CI 不收集）。本目录的 JSON 是 `test-results/external-agent-phase2-acceptance/` 经 `python -m tests.acceptance.sanitize` 处理后的副本：只有 ID、状态、事件种类与时间，临时路径换成 `<scratch>`／`<home>`，不含消息正文、命令全文与密钥。

## 做法与如实说明

- **真实的部分**：Bot 的 `ApplicationContainer`、SQLite、Main／Work、Client API、设备 WSS（协议 5）、连接器 `yuki_agent_connector.Connector`（0.9.0，进程内）与 journal、全部回执；原生 Agent 为 Claude Code 2.1.311（SDK 0.2.172，`ANTHROPIC_MODEL=haiku`）、codex-cli 0.169.0（**自启的** `codex app-server --listen unix://<scratch>/codex.sock`，gpt-6-luna／low；从未连接用户的 managed daemon 或 ChatGPT 应用）、ACP 用 claude-agent-acp 0.108.0（npm 装在 scratch，沿用现有 Claude 登录，haiku）。
- **替身的部分**：只有 Main 模型是脚本（`tests/acceptance/harness.py` 的 `ScriptedMain`，按数据库事实选择工具调用）；群消息经真实 Telegram 入口进入，HTTP 用离线 MockTransport。
- **两台设备** = 同一台 Mac 上的两个连接器配对（两个 device token、两个 journal），任务书允许。设备 2 前面有一个本机 TCP 中继（`NetworkGate`）：`cut()` 中断 WSS 并拒绝重连，连接器与 ACP 进程照常运行，模拟真实断网；不是"停掉连接器"（那会杀掉连接器自己的 ACP 进程，变成执行者消失）。
- **"用户的终端"** = 私有 tmux 服务器 `tmux -L p2acc` 里的一次性会话；ACP 的"用户编辑器"是测试里一个最小的 stdio ACP 客户端。
- 为不读取用户真实会话列表，测试用与 `get_tasks(agents={agent})` 相同的 `remember_sessions` 映射只登记本次 tmux 会话的原生 ID。
- Gemini 无凭据（§15.3），真实 ACP Agent 用 claude-agent-acp。
- Bot 与连接器在同一事件循环里，测试本身也在轮询数据库；任何时长只作记录，不作通过条件。

## 结果

| 场景 | 结论 | 关键事实（产物） |
|---|---|---|
| A1 两设备 × 三执行者 | **通过（一条等式如实不成立，见下）** | 三个委派（Claude／Codex 在设备 1，ACP 在设备 2）在 0.2 s 内登记，`dispatch.status=unknown, pending=true`，激活不等原生 I/O；群里 8 条闲聊全部各得一条回复，模型准入 30 次全部前台、最大等待 0.039 s；Claude 的范围外 Write → 父 Work 被卡片唤醒 → 群里说明 → `POST /client/v1/works/{id}/native-requests/{permission_id}/answer` 200 → 原生消费、范围外文件写出、委派完成；设备 2 断网 → 委派保持 `waiting_external` + `device_offline`、父 Work 被告知一次（`offline_notice` 1 次）→ 恢复后按真实终态 `completed` 结算；三个结果各被父 Work 恰好消费一次。父 Work 激活 5 次，唤醒类输入 5 + 用户输入 1：**一次激活同时交付了两张卡**（先后 ~1 s 到达的输入合并进同一激活），所以"激活数 = 唤醒 + 输入 + 到期"严格等式不成立，`each_wake_input_consumed_once=true`（`A1.json` → `equation`） |
| A2 observe 回归（D6） | **通过** | 终端 Claude（manual 模式）与 Codex TUI 各弹一次权限 → 两个观察记录各收到一条只有元信息的请求（`answer_at: native_terminal`，无可答选项）→ 各起一个通知 Work，脚本 `send` 告诉群；Client API 回答两条都是 409 `observe_only`；用户在 tmux 里批准；连接器 journal 与服务端对这两个会话只有 `watch`／`detach`；Claude transcript 只有 2 条 `entrypoint=cli, promptSource=typed` 的用户输入，Codex 线程只有 TUI 自己的 2 条 userMessage（`A2.json`） |
| A4 Codex 实时共享 | **部分通过** | 通过：同一线程上 TUI 与 Yuki 交替（turn 序列 TUI→Yuki→TUI→Yuki→〔Yuki 排队项＋TUI 排队项合并为一轮〕→Yuki，`thread_turns`）；连接器只用 `thread/queue/add`，从未 `turn/start`；TUI 屏幕上能看到 Yuki 的回合；Yuki 在自己回合里的审批由 Yuki 回答。**未验证**：TUI 自己回合的请求返回 `not_owner`——两次尝试里用户的输入都被 Codex 并进了 Yuki 的回合（Enter=steer：`A4-run2-tui-steer-merged.json`；Tab=排队后与 Yuki 的排队项合并成一个回合：`A4.json`），没能产生"Yuki 记录存活 + 纯 TUI 回合请求"的窗口；同时暴露问题 P1、P2 |
| A5 Claude 活会话 | **通过** | 恢复 tmux 里的活会话 → 记录 `failed`，操作错误 `session_live_elsewhere`，`details.options=["fork","handoff"]`；显式 `fork=true` → 新会话 ID 完成；原 transcript sha256 与行数前后一致（`A5.json`） |
| A6 交接往返 | **通过（Claude、Codex、ACP 各一次）** | 用户停 → `relationship=takeover` 按原会话接手（同一会话 ID）→ `manage_task release`（连接器 detach + 回合边界关闭）→ 用户接回。Claude：transcript 单链、分叉点 0，入口序列 `cli→sdk-py→cli`，takeover.txt 的写入 1 次；Codex：同一线程 ID，三个 userMessage 依次为 TUI／Yuki／TUI，`thread/settings/update` 收窄后恢复、`thread/unsubscribe` 放手；ACP：编辑器进程退出后接手，释放后编辑器 `session/load` 继续，transcript 单链、分叉点 0。每个记录 1 次 prompt（`A6-*.json`） |
| A7 执行者消失 | **通过** | 杀掉连接器自己的 Claude CLI（SIGKILL 本测试 PID）→ 记录保持 `waiting_external`，reason `native_sdk_connection_lost`（卡片状态未知），告诉一次，不改判失败、不重派（journal 1 次 prompt）；释放后删除 scratch 会话 transcript 再恢复 → `failed`（错误码 `cwd_unknown`，不是 `session_not_found`），告诉一次；撤销设备授权 → 新输入在执行处被拒（`unauthenticated`），连接器进入 `authorization_lost`，记录 reason `device_offline`，告诉一次，无新效果（`A7.json`） |
| A8 重启窗口 | **通过** | W1 已登记未写出 → 重启后原生命令 0 次、原 operation／run ID 不变、未重派、保持未知；W2 连接器已开始 → 重启后按原 ID 结算 `completed`，原生 prompt 1 次；W3 journal 有回执但 Bot 未提交 → 重启后重传结算一次；W4 已提交但 ACK 丢失 → 重传不产生第二个完成事件或第二份结果输入。父 Work 预算计数只随真实激活增长（`A8.json`） |

A3（前缀字节）、A9（跨版本）、A10（删除核对）不在本次范围。

## 发现的问题

处理：P2、P3 已在随后的提交中修复（失败测试先写，见任务书 §16.4）；P1 记录为已知限制（§16.2）。


- **P1 Codex：用户与 Yuki 的输入落进同一原生回合时，整轮归 Yuki，Yuki 回答了用户输入引出的审批。** `packages/yuki-agent-connector/src/yuki_agent_connector/drivers/codex.py:889-894` 只要回合里出现 Yuki 的 `clientId` 就把整轮记入 `turn_operations`；之后请求按"有主"上传（`:1023-1028`），`respond` 也只核 `turnId in turn_operations`（`:1450`）。复现：Yuki 回合运行中，用户在 TUI 输入一个要审批的命令（Enter 会 steer 进 Yuki 回合；Tab 排队后会与 Yuki 的排队项合并成一轮），该命令的审批被 Yuki 回答（`A4-run2-tui-steer-merged.json` permission 9；`A4.json` permission 13）。与 F12 同类的风险。
- **P2 Codex 共享线程：委派结束后，Yuki 无法在用户回合运行时排队追加。** `manage_task resume` → 连接器 prompt 在线程运行中返回 `conflict: Native session is active; choose steer or queue`（`drivers/codex.py:1290-1301`；`A4-run1-resume-conflict.json`）；`message_task`（默认 queue）对已结束的记录返回 `external_agent_source_obsolete`（`src/yuki/external_agents/service.py:654-656`；`A4.json` → `follow_up_behind_tui_turn`）。与 §4.11"Yuki 的新输入一律 queue/add，忙时排在后面"不一致。
- **P3 Bot 自己停机被记成设备离线并唤醒父 Work。** `src/yuki/external_agents/service.py:79-87`（`close()` 对每个活连接写 `connection_event(..., False)`）→ `repository.py:1292` `transport:<conn>:device_offline` → `delegation.py` `device_offline` → `_tell_parent`。每次 Bot 重启／部署，所有有在跑委派的父 Work 都会被唤醒一次并（按提示）告诉用户"设备离线"（`A8.json` 每个窗口的 `delegation:…:device_offline` 输入，父 Work `model_requests` 各 +1）。
- **观察（非本阶段）Telegram 新群的第一条消息偶发被丢弃**：`ProcessResult(handled=False, reason="ordinary_source_changed")`（`src/yuki/services/processor.py:1279` 一带），无回复也不进入后续批次。进程内 `ApplicationContainer` + `TelegramGateway` 复现，非稳定；harness 因此对每个新群先发一条预热消息（`group_warmup` 标记）。harness 未启动完整 lifecycle，是否产品问题待确认。
- **观察**：设备离线期间 Yuki 回答 ACP 的请求不登记，设备恢复后也不会再唤醒父 Work；该请求仍可由设备所属人物在 Desktop 回答（A1 的早期运行中出现，最终流程由人物在恢复后经 Client API 回答）。设备授权撤销后，已有 WSS 直到连接器下一条事件才被拒绝断开（`connector_disconnected … rejected:unauthenticated`，撤销后约 37 s），其间原生的完成事实不被记录，记录停在 `device_offline`。

## 清理

见报告；本次只结束自己的进程（私有 tmux、自启 app-server、连接器派生的 CLI／proxy／ACP），删除自己的 Codex 线程，`claude purge` 自己的 scratch 项目，恢复 `~/.codex/config.toml` 中本次新增的信任项，删除 scratch 内 npm 安装。
