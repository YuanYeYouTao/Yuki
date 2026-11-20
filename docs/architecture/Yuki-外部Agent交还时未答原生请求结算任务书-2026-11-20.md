# Yuki 外部 Agent 交还时未答原生请求结算任务书（#645，2026-11-20，修订 3）

状态：**已实施（修订 3，精简版）**。修订 2 的待结算表（迁移 0160）、持久退避与运维告警经用户决定删除，重试改为按事实触发（§4.3）；实现、验证与交付见 §6。[第二阶段任务书](Yuki-外部Agent委派与协作第二阶段任务书-2026-11-18.md) 保留当时基线，不回改。权限卡延迟是独立问题，见 [委派权限卡并行处理与等待死锁任务书](Yuki-委派权限卡并行处理与等待死锁任务书-2026-11-20.md)。

标记：**【实证】** 有生产记录、代码行或已运行测试为据；**【假设】** 尚未验证，列出验证实验；**【决定】** 需审查者拍板。

## 1. 现象（生产只读查证）【实证】

委派 `8331a5f7`（父 Work `8713d099`，Claude 会话 `9ea5c710-…`），UTC：

| 时间 | 事实 | 来源 |
|---|---|---|
| 09:47:20.401 | 范围外 Bash 权限请求 `toolu_01CwwKAZ…`，属运行中的操作 `resume:f1653e05` | `external_agent_events` 3501 |
| 09:47:21–22 | 同一条助手消息里另两个并行 Bash 排在后面 | 事件 3502、3503 |
| 09:47:35–40 | `manage_task(release)`：对记录 10 个操作逐个发带 `release` 的 detach，最后回执 `closed: after_turn` | `external_agent_operations` `release:8d13231d…` |
| 09:47:39.116 | 该请求被重新发布为无主等待（`unowned: true`、`options: ["deny"]`） | 事件 3504 |
| 09:47:39 | 父 Work 收到 `answer_at: deny_only` 卡片 | `runtime_work_inputs` 7663 |
| 09:47:40 → 09:56:08 | 无任何 respond；回合不到边界 | 操作表 |
| 09:56:08 | 用户退出 Desktop，回调撤回（`outcome: withdrawn`） | 事件 3505 |

## 2. 协议与代码事实

### 2.1 连接器（生产 0.9.1）【实证】

| # | 事实 | 依据 |
|---|---|---|
| C1 | 生产 Desktop 内置连接器与 main 源码逐文件一致；版本 0.9.1，claude-agent-sdk 0.2.172；main 上连接器最后一次改动是 #643 | `diff -rq ~/Applications/Yuki.app/Contents/Resources/agent-connector/python/lib/python3.12/site-packages/yuki_agent_connector packages/yuki-agent-connector/src/yuki_agent_connector` 无差异；bundle 内 `yuki_agent_connector-0.9.1.dist-info`、`claude_agent_sdk-0.2.172.dist-info`；`git log origin/main -- packages/yuki-agent-connector` |
| C2 | detach 时拥有该请求的接收者被移除，若无剩余接收者，请求被重新发布为无主、只允许 deny | `claude.py` detach 分支（约 1009–1019）、`_unowned`；`acp.py` 703–712 |
| C3 | detach 立刻丢掉委派范围；owner 为空时 `_inside_scope` 恒为 False，之后每个有副作用调用都成为新的无主等待 | `claude.py` 979–981、343–356、391–394 |
| C4 | respond 只作用于 **(连接 ID, 原生请求 ID)** 完全匹配且仍挂着的那一个回调；不匹配或不再挂着返回 `request_resolved` | Claude `claude.py` 1115–1123；ACP `acp.py` 897–906 |
| C5 | 连接 ID 每个 owner 一个 uuid4，不复用 | Claude `ManagedSession.connection_id`（`claude.py` 117）；ACP `self.owner`（`acp.py` 139） |
| C6 | respond 成功仅在回调**真正消费**答案之后返回 `status: response_submitted, response_submitted: true`；未消费返回 `status: unknown`（uncertain） | `claude.py` 1133–1145；`acp.py` 926–940 |
| C7 | 无主等待被回答或撤回时，连接器发会话级 `permission_resolved` 事件（`outcome: answered/withdrawn`） | `claude.py` 409–424；`acp.py` 541–548 |
| C8 | `owner_disconnected`（ACP 进程未连接、Claude owner 已退出）、`session_not_loaded`、`not_live` 是"此刻没有可用 owner/会话"，不是"该请求已结束"的事实 | `acp.py` 738、468–469；`claude.py` 1053–1056 |
| C9 | 每条命令独立起任务并发执行；同一连接上命令的**执行顺序不保证**等于发送顺序 | `connector.py` 731–732 `_spawn(self._command(...))` |
| C10 | 命令按 ID 记入落盘 journal（SQLite `synchronous=FULL`）；同 ID、同参数再发**不再执行**，只重发原回执；参数不同报错 | `journal.py` 78–92、202–224；`connector.py` dispatch 中 `previous` 分支（约 509–521） |
| C11 | 事件持久在 journal，直到服务端 ACK；每次连接从头重发未 ACK 事件 | `connector.py` 695–709；`journal.py` 459–470 |
| C12 | 交还后回合结束时 `completed` 只发给仍在的操作或 followup；全部 detach 后**服务端收不到 owner 关闭的事实** | `claude.py` `_publish` 256–309、740–743 |
| C13 | Codex 不产生无主等待；审批留在用户 daemon，可在 TUI 回答 | `codex.py` 无 `unowned`；第二阶段任务书 §15 E1 |

### 2.2 服务端【实证】

| # | 事实 | 依据 |
|---|---|---|
| S1 | 交还：`release()` 对记录每个未解除操作先登记、再后台派发带 `release` 的 detach；记录状态 `cancelled/released` 在全部 detach 之后才写 | `service.py` `release()`；`subtasks.manage_external` |
| S2 | 无主等待交给最后持有会话的记录（按操作 `created_at` 最新） | `delegation.py` `Delegations.unowned` |
| S3 | 已登记的命令派发前经 `dispatchable()`：记录已终结时只放行 detach | `repository.py` `dispatchable` |
| S4 | 命令"从不重发"：派发写入失败或送达未知时，只靠重连后按原 ID 回读（`get operation_id`） | `service.py` `_dispatch`、`_query_original` |
| S5 | 现有维护循环每 2 s 调 `external.reconcile()`（出错退避至 30 s），是唯一的外部 Agent 维护调度点 | `sandbox/completion_receiver.py` 14、44–63 |
| S6 | 操作表索引：`(session_id, action)`、`(work_id)`；事件表索引：`(instance_id, native_session_id, kind, id)`、`(operation_id, id)` | `db_models.py` |
| S7 | 生产只有一个 bot 进程写库（单容器、SQLite 单写者） | 部署 `yuki-local-bot-1`；`persistence/database.py` |

### 2.3 假设与验证实验【假设】

| # | 假设 | 为何重要 | 实验 |
|---|---|---|---|
| H1 | Claude CLI 在 `can_use_tool` 挂着时不会取用队列中的新输入（新输入只在工具边界并入） | 决定接管竞态能否只在 bot 侧解决（§4.2） | E-A：真 Claude CLI、临时目录、一次性会话；回合卡在权限上时 queue 一条输入，观察回放帧出现在回答前还是后 |
| H2 | 被 deny（不中断）后，真 Claude 回合会在有限步内结束，而不是持续发新请求 | 决定只 deny 是否足够让 owner 关闭 | E-B：同上，交还后对每个请求 deny，记录回合内请求次数与是否出现 `result` |
| H3 | 连接器进程正常退出时回调撤回并发 `permission_resolved`；崩溃（kill）时 SDK 子进程随之退出且**不**发该事件 | 决定"过时等待"是否会永远不终结（§4.3 的重试终止条件） | E-C：两种退出方式各一次，看事件与子进程 |
| H4 | ACP Agent 对无主 deny 的行为与 Claude 一致 | ACP 是否同样被覆盖 | 只读过代码（`acp.py`）；E-D：fake ACP agent 走一次 |

E-A、E-B、E-C 用真 Claude 会产生少量模型费用，且需在本机起 Claude 进程（不碰用户已有会话）；**执行前需审查同意**。未做之前，相关结论在报告中一律写"假设"。

## 3. 结论

#645 真实存在（§1）。根因由 C2、C3 构成：交还后，挂着的或新出现的请求都没有回答者；用户在终端里够不到 Yuki 进程里的回调。服务端只靠 S2 把它交给父 Work，父 Work 不回答就会一直挂着。

## 4. 设计（待审查）

原则：交还以 **deny** 结算交还后无人可答的等待（用户原话"不要 cancel、等这一回合结束"）。只使用 0.9.1 已有协议（C4、C6、C7、C10），只部署 bot。

### 4.1 可信内部授权

- 结算**不经** `execute()`，也不由调用方拼 source。新增仓库内部方法 `register_release_answer`，只由服务端结算流程调用（工具、HTTP 都够不到）。
- 登记时的授权链取自事实行：交还的 detach 操作行（`action=detach`、命令带 `release`、`work_id=记录`、`session_id=会话`）。结算操作的 `actor_person_id` 取该 detach 行的 actor（交还时已按人物授权，`authorize_record`）。source 只写引用：`{"settled_by": "release", "release_operation_id": <detach 行 ID>, "work_id": 记录}`。
- 内容约束复用现有无主应答校验（事件属本设备、本实例、本会话，`unowned: true`，只能 deny、不带 `response`），抽成共用函数，不复制。
- `dispatchable()` 放行条件，全部按事实：
  1. `action=respond`，命令里 `option_id=deny`、无 `response`、无 `operation_id`；
  2. source 引用的 detach 行存在，且 `work_id`、`session_id` 与本行相同，命令带 `release`；
  3. 该记录的交还仍生效（修订 3）：会话最近一次带 `release` 的 detach 之后，没有能回答的记录（委派或接管，不含观察）再给该会话 prompt/queue/steer/watch。答复（含交还自己的 deny）不改变持有者；观察只看不答，不终止交还；同一记录 resume 即终止。

  标记本身不授予任何权限：伪造 `settled_by` 而无对应 detach 行、或用于其他 action，一律不放行。
- 第一版用 `answered_via == "release"` 放行的写法作废。

### 4.2 接管与发送的原子性

- **bot 内**：服务按内部会话 ID 持一把 asyncio 锁（S7：单进程单写者）。
  - 结算：持锁 → 读事务核对（最后持有者仍是交还记录、交还 detach 存在、等待未终结）→ 写事务复核"该会话没有比预备时更新的操作"（走 `(session_id, action)` 索引，沿用 `prepare_external_action`/`external_action_current` 的"读预备、写比对"模式，写事务内不等外部工作、不扫历史）→ 登记 → `dispatchable` → 发送 → 释放锁。
  - 同一会话的新输入（prompt/queue/watch/接管）也持同一把锁完成"登记 → 发送"。
  - 因此在 bot 内，"核对仍为交还状态"与"发出 deny"之间不会插入新的接管输入。
- **设备侧**：C9 说明设备不保证执行顺序，但 C4 保证 deny 只作用于原 owner 的那一个回调，不会落到新委派自己的请求上。唯一的交叉风险是：新委派的输入在 deny 落地前已并入同一个卡住的回合。H1 成立则此风险不存在。
- **H1 不成立时**：bot-only 无法做到设备侧原子；需要连接器提供"仅当 owner 仍无操作时才 deny"的条件应答，即连接器发版。届时作为单独决定提交审查，不在本 PR 静默处理。【决定】

### 4.3 结算判据与重试（修订 3）

每次尝试是一行操作（持久，ID `release:{记录}:deny:{事件ID}:{n}`）。判据：

| 结果 | 依据 | 处理 |
|---|---|---|
| 成功 | 回执 `status: response_submitted` 且 `response_submitted: true` | 结束 |
| 等待已结束 | 会话级 `permission_resolved` 事件（该原生请求 ID，任何 outcome） | 结束 |
| 回调已不存在 | 拒绝码 `request_resolved` | 结束（连接 ID 不复用，C5） |
| 接管 | 会话最后持有者不再是交还记录 | 不再结算，归新持有者的 deny-only 卡片 |
| 送达未知 | 尚无回执 | 设备重连时以**同一操作 ID** 重发（C10：连接器去重） |
| 其他拒绝 | `owner_disconnected`、`session_not_loaded`、`not_live`、`unknown`（未消费）等 | 只说明此刻没有 owner；在下一个事实时以新 ID 再试 |

- **触发**：该会话又到一个无主等待（只结算事件所属会话，后台触发，不在 ACK 路径）；设备连接（`connect()` 后后台结算该设备全部会话，含以原 ID 重发未确认答复；此时若连接已被取代或不再可派发，不写任何回执，因为设备可能已执行上一次写出）。维护循环与定时器不参与。
- **部署时**：首次连接会结算生产库中所有交还仍生效会话里未解决的无主等待；部署前只读统计其数量。
- **不保存结算状态**：每次按事实计算——设备的会话中，最后持有者带 `release` detach 的那些，读其仍未解决的无主等待（`unowned_requests`）与已有尝试行。重启即一次重连。
- 删除的修订 2 机制及理由：待结算表与迁移 0160（状态可由事实算出）；持久退避与告警（C8 的拒绝只说明此刻无 owner；owner 进程退出时回调撤回并发 `permission_resolved`，等待自然结束，不需要定时重试）。
- **过时等待**（H3 崩溃场景，未验证）：若没有 `permission_resolved`，每次重连多一条 deny 尝试，直到 `request_resolved`；代价随重连次数，不随时间。

### 4.4 父 Work 通知

交还结算不唤醒父 Work（不发 deny-only 卡片）。结算操作挂在被交还记录上，时间线显示 `answered_by: yuki`（交还是 Yuki 的动作）、`answered_via: release`、理由、原请求摘要。

终结类失败（`request_resolved`、接管）与其他拒绝都只记入被交还记录的时间线，不通知父 Work 或用户（修订 3 决定）。

### 4.5 生产旧连接器兼容

只使用 0.9.1 的 respond（`session_id`、`permission_id`、`connection_id`、`option_id: deny`）和 journal 去重（C1、C4、C6、C7、C10）。不需要新能力声明。

协议 4 的 0.8.0 连接器按 Q4 硬切换无法配对（现有 cross-version E2E 已覆盖）。验收中加一条：E2E 以 **Desktop 内置的实际 0.9.1 包路径** 作为连接器 `pythonpath` 运行。

## 5. 失败情况与测试（先写失败测试，再实现）

| # | 失败情况 | 测试 | 层 |
|---|---|---|---|
| F1 | 交还时挂着的请求无人应答 | `test_release_settles_a_pending_native_request_and_the_owner_closes` | E2E 真连接器 |
| F2 | 交还后同回合新请求无人应答 | 同上（请求 B） | E2E |
| F3 | 交还结算唤醒父 Work 或发 deny-only 卡片 | 同上；`test_unowned_wait_after_release_is_denied_by_the_release` | E2E、集成 |
| F4 | 结算不在被交还记录时间线、或写成别人的回答 | 同上 | 集成 |
| F5 | 结算发 allow 或带 response | 负向：直接调内部登记方法传 allow/response → 拒绝 | 集成 |
| F6 | 未交还的无主等待被自动拒绝 | `test_unowned_wait_without_release_reaches_the_parent_deny_only` | 集成 |
| F7 | 事件重投（ACK 丢失）重复派发 | 同一事件 ID 重投 → 仍一条 respond | 集成 |
| N1 | 伪造标记：source 写 `settled_by: release` 但无对应交还 detach → `dispatchable` 放行 | 构造行 → False | 集成 |
| N2 | 标记用于其他 action（queue/cancel/steer/prompt/带 operation_id 的 respond）→ 放行 | 每种构造行 → False | 集成 |
| N3 | 交还 detach 属于别的记录或别的会话 → 放行 | 构造 → False | 集成 |
| N4 | 未交还而终结的记录（completed/cancelled 非 released）→ 放行 | 构造 → False | 集成 |
| R1 | 接管竞态：交还后新委派接管，结算仍发出 | 新委派登记后才到的无主等待 → 无结算、新父 Work 收 deny-only 卡 | 集成 |
| R2 | 接管竞态：结算已登记未发出时被接管 → 仍发出 | 设备 hold 住发送窗口，新委派登记 → 结算判定不发、记为接管终结 | 集成 |
| R2' | 核对后、登记前被接管：交还仍发 deny，且尝试行使交还记录重新成为持有者 | `test_a_takeover_during_the_settlement_pass_wins` | 集成 |
| R4 | 交还后开始观察，结算停止，回合重新卡住 | `test_an_observation_after_the_release_does_not_stop_the_settlement` | 集成 |
| R5 | 同一记录交还后 resume，旧交还仍自动 deny | `test_a_resume_after_the_release_ends_it` | 集成 |
| T1' | 重连重发写不出时记成"未执行"（设备可能已执行） | `test_an_unwritable_resend_is_not_recorded_as_not_executed` | 集成 |
| T4' | 其他会话的等待触发本会话重试 | `test_a_wait_in_another_session_does_not_retry_this_release` | 集成 |
| R3 | bot 内锁失效：结算与新输入交错 | 未单独测试：同进程会话锁保护“核对→写出”，写出时 `dispatchable` 再按事实核验（R2 覆盖） | — |
| T1 | 送达未知（发出后断线）→ 永不重试或换 ID 重复执行 | 设备收到后断线 → 重连后同 ID 重发；连接器 journal 去重（真连接器 E2E 断言只执行一次） | 集成 + E2E |
| T2 | 其他拒绝被当终态，或被每次维护重试 | 维护 tick 不重试；同会话新等待与重连各以新 ID 重试，直到成功 | 集成 |
| T3 | `request_resolved` / `permission_resolved` 后仍重试 | 回终结码或发解决事件 → 不再有尝试 | 集成 |
| T5 | 连接初始化被同步结算阻塞 | `connect()` 期间设备不回执 → `connect` 立即返回、catalog_ack 不延迟 | 集成 |
| T6 | 重启恢复 | 无内存状态，重启即重连，由 T1 覆盖 | 集成 |
| P1 | 生产连接器组合 | F1 E2E 以 Desktop 内置 0.9.1 包为连接器路径运行 | E2E |
| P2 | Codex 交还不受影响 | 现有 `test_codex_connector_delegation_e2e.py` 复跑 | E2E |
| E-A…E-D | §2.3 假设 | 真 CLI 实验（审查同意后） | 实验 |

## 6. 命令与结果（修订 3，2026-11-20）

```bash
W=/Users/yuanyeyoutao/Developer/personal/Yuki-645
uv run --directory $W --with claude-agent-sdk==0.2.172 pytest -q -p no:cacheprovider -m "not native_worker" \
  tests/integration/test_external_agent_release_settlement.py tests/integration/test_external_agent_review_fixes.py \
  tests/integration/test_external_agent_delegation.py tests/integration/test_external_agent_bridge.py \
  tests/integration/test_claude_connector_cross_version_e2e.py tests/integration/test_claude_release_pending_request_e2e.py \
  tests/integration/test_codex_connector_delegation_e2e.py tests/unit/test_client_works.py \
  tests/unit/test_external_agent_work.py tests/unit/test_external_event_prompt_isolation.py tests/unit/test_local_device_bridge.py
# P1：连接器换成 Desktop 内置 0.9.1 包
uv run --directory $W --with claude-agent-sdk==0.2.172 pytest -q -p no:cacheprovider \
  -o pythonpath="src /Users/yuanyeyoutao/Applications/Yuki.app/Contents/Resources/agent-connector/python/lib/python3.12/site-packages" \
  tests/integration/test_claude_release_pending_request_e2e.py
# 自然 E2E（真 Claude，haiku，不加载用户设置，不删除文件；按需）
YUKI_TEST_REAL_CLAUDE=/opt/homebrew/bin/claude uv run --directory $W --with claude-agent-sdk==0.2.172 pytest -q \
  -p no:cacheprovider -o pythonpath="src packages/yuki-agent-connector/src" tests/integration/test_claude_release_natural_e2e.py
uv run --directory $W ruff check . && uv run --directory $W mypy --platform linux src
```

结果：

- 独立审查（只读）发现 R2'、R4、R5、T1'、T4' 五处缺陷，先写失败测试再修。修复后上述 11 个文件 157 passed、1 skipped（cross-version 中"协议不同"分支按设计跳过）；ruff、mypy 通过。
- T1 的"连接器按 ID 至多执行一次"仅由连接器代码（C10）支撑，集成测试的假设备没有 journal，只断言同一 ID。
- P1 通过：请求 A（交还时挂着）与 B（同回合新请求）均由交还 deny，`answered_via: release`，产物 `test-results/claude-release-pending-request/result.json`。
- 自然 E2E 通过（修复前后各一次）：真 Claude 的 transcript 中挂着的 `tool_use` 收到连接器 deny 的 `tool_result`；回合结束后 CLI 进程退出；父 Work 无新输入；被拒命令未在外部目录建文件。产物 `test-results/claude-release-natural/`。
- 实验（修订 2 期间，单次观察）：H1 成立（deny 落地前排队输入不并入卡住的回合）；H2 成立（haiku）；H3、H4 未验证。

## 7. 提交文件

- `src/yuki/external_agents/{service,repository,delegation}.py`：结算、授权、会话锁、判据、按事实触发。
- `tests/integration/test_external_agent_release_settlement.py`（F1–F7、N1–N4、R1–R2、T1–T3、T5）、`test_claude_release_pending_request_e2e.py`（F1、F2、P1）、`test_claude_release_natural_e2e.py`（按需）、`test_external_agent_review_fixes.py`（无主等待不经交还时仍归父 Work）。
- `.github/workflows/quality.yml`（交还 E2E 进 claude-cross-version 任务）。
- 本任务书；`docs/architecture/main-agent-runtime.md` 无主等待一段。无迁移。

## 8. 决定（已定）

1. 不加退避参数；重试按事实触发（修订 3）。
2. 不新增待结算表与迁移 0160（修订 3）。
3. 结算的任何失败只记时间线，不通知父 Work 或用户。
4. 真 Claude 实验：E-A、E-B 与自然 E2E 已各执行一次（少量费用）；E-C 未执行。
5. H1 成立，不需要连接器发版。

## 9. 部署前审查清单

- 本书 §8 全部有决定；§5 测试全部通过，含 P1；CI 全绿后合并，不用 `--admin`。
- 部署前：
  - 父 Work 与本机委派均停在等待里（不在模型请求中），没有即将到点或运行中的自动化；
  - 备份数据库，记录当前镜像 `yuki:local-a98194f6` 与 compose 备份；
  - 若含迁移 0160，确认可回滚（新表只增不改，回滚镜像可忽略该表）。
- 只部署 bot（runtime target），构建后清理 builder cache；连接器、Desktop 不动。
- 部署后：`/healthz`、日志无 ERROR、连接器重连；交还场景若无现成实例，报告"生产行为待下次交还观察"。
