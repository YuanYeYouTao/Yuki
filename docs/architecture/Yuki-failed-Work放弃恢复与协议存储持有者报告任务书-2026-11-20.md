# Yuki failed Work 放弃恢复与协议存储持有者报告任务书（2026-11-20）

状态：**第一阶段（调研与任务书），待批准，未实施**。对应 [#666](https://github.com/YuanYeYouTao/Yuki/issues/666)。分支 `feat/666-cancel-failed-work`，基线 origin/main `9d5242ea`（#664 看门狗、#665、#667 已合入；迁移 head `0161`）。

标记约定同看门狗任务书：【实证】是源码或现有测试里查到的事实；【issue】是 #666 给出的生产数据，本阶段没有再去生产只读复核；【假设】是还没查实、需要在实施或 E2E 中确认的判断；【决定】是用户已定的取舍；【推荐】是本书提议、等待批准的取舍。文中路径省略 `src/yuki/`，行号以 `9d5242ea` 为准。

## 1. 问题与证据

### 1.1 现象【issue】

2026-11-20 生产协议存储为 1.43 GB，其中 1141 MB 被 55 个 failed Work 持有。持有最多的三个分别是 `8713d099`（RemoteProtocolError，627 MB）、`dc6b3247`（PermissionError，285 MB）和 `6cd62f2f`（LLMInvalidRequestError，156 MB）。#656 之后，无引用对象的 GC 工作正常。

### 1.2 根因【实证】

| # | 事实 | 依据 |
|---|---|---|
| C1 | 触发器 `protocol_refs_closed_work` 只在 Work 进入 `completed`/`cancelled` 时删除 refs；只要 Work 进入 `cancelled`，就会无条件删除它的 refs（`failed → cancelled` 同样会触发，`OLD.state IS NOT NEW.state`） | `runtime/protocol_schema.py:44-50`；`migrations/versions/0158_closed_work_protocol_refs.py:17-20` |
| C2 | `manage_work` 把 `failed` 当作终态，只放行 `resume`；对 failed Work 执行 `cancel`/`fail` 都返回 `precondition_failed` | `runtime/work_management.py:200` |
| C3 | `management_view` 对任何终态都给出 `cancel: not terminal`，所以 failed 显示为不可取消 | `runtime/work_management.py:332-334,377` |
| C4 | 四个入口都汇到 `manage_work`：Work 管理工具 `manage_task`（`runtime/work_control.py:1030-1056`；子任务走 `subagent_tools.manage_subtask` → `SubagentRepository.cancel`，`subagent_repository.py:657-666`）、管理端 HTTP/CLI（`admin/operations/work.py:26-40`）、管理聊天命令 `work cancel`（`services/commands/work.py:35-49`，同样走 `WorkOperations.work_action`）、个人设备 API `POST /client/v1/works/{id}/cancel`（`client_api/works.py:984-1033`）。所以放宽 C2 和 C3 后，四个入口的行为天然一致 | 同左 |
| C5 | 没有工作者的 failed 根 Work，其 refs 不会被任何路径释放；唯一的出口是删除整个 Conversation | C1 + C2 |
| C6 | **反差**：有工作者的 failed 根 Work 在根进入终态 7 天后会被工作者归档。归档会把整棵树（包括根）的 refs、journal、effects 和 inputs 一起删掉（`TERMINAL` 包含 failed）。归档后根仍是 `failed`，但 `resume_blocker` 会因为 journal 缺失返回 `state_mismatch` | `runtime/subagent_repository.py:214,235-246,341-366`；`work_management.py:110-112` |
| C7 | 只有执行路径会读协议对象：`work_session.py:869` 和 `work_journal.py:189` 的 hydrate，以及 Code Mode 的 `store.get`。cancelled 的 Work 不会再执行，所以释放后没有读者（0158 已对 cancelled 验证过这一点） | `git grep ProtocolStore\|hydrate` |
| C8 | Work 进入 failed 时，`commit_state` 已经通过 `stop_owned_execution` 把**非终态**后代置为 cancelled，并撤销它们的租约、待处理输入和活动等待。所以一个 failed Work 下面通常只剩**终态**后代 | `runtime/work_repository.py:762-765`；`work_management.py:117-145` |
| C9 | `failed → cancelled` 不会改写已经结算的原事实：自动化运行只从 RUNNING 结算（`automation/repository.py:840-866`）；插件后台作业只从 pending/processing 结算（`plugin_host/notification_repository.py:1031-1060`）；`manage_work` 不动 `runtime_work_recovery.failure_json`、effects 和 journal 行 | 同左 |
| C10 | 失败原因码在 `runtime_work.reason`（`failure.code`）里。取消后它会被覆盖成取消原因，但原失败码仍保留在 `runtime_work_recovery.failure_json.code` | `runtime/work_supervisor.py:166-175` |
| C11 | 对 failed 的委派记录（`children.executor_json` 非空）执行 `manage_task(cancel)` 会进入 `manage_external`。终态记录会跳过状态改写，却返回 `{"status":"cancelled"}`，与事实不符。委派记录从不运行 Yuki loop，所以没有 journal，也没有协议 refs | `external_agents/subtasks.py:431-455`；`external_agents/delegation.py:150-157` |
| C12 | 看门狗的协议存储事实只在 `usage ≥ cap × warn_ratio`（默认 2 GiB × 0.8 ≈ 1.72 GB）或存在容量挂起时才会出现，内容是 `usage_bytes`、`cap_bytes` 和挂起 ID。`facts_json` 在问题打开时写入一次，之后不刷新 | `application/watchdog.py:427-457,314-326`；`config.py:275,282` |
| C13 | 看门狗的隐私合同规定 facts 只含内部 ID、状态、原因码、计数和时间，**不含 Work goal**，原因码经 `_code()` 过滤 | 看门狗任务书 §4.6（`Yuki-看门狗与主动私聊任务书-2026-11-20.md:277`）；`watchdog.py:613-619` |
| C14 | 已有索引足够：`runtime_work (state, updated)`（0084/0085），`runtime_protocol_refs` 主键 `(work_id, sha256)`，以及 `ix_protocol_refs_digest`。持有者查询**不需要迁移** | `migrations/versions/0084_cache_cleanup_indexes.py:14`；`runtime/protocol_schema.py:18-28` |
| C15 | 没有任何现有测试冻结“cancel failed → precondition_failed”。`test_closed_work_protocol_refs.py:70` 冻结的是“failed 保持 refs”，这在新方案下仍然成立（不取消就保留） | `git grep manage_work( tests`；`git grep precondition_failed tests` |
| C16 | Desktop 的取消按钮条件是 `!terminal(data)`，而 `terminal` 包含 failed，所以 failed Work 看不到取消入口 | Yuki-Desktop `3adff8e`：`src/renderer/views/Works.tsx:57,498,1087` |

## 2. 已定决定【决定，用户】

1. 采用 #666 方案 2：放宽条件，允许 `failed → cancelled`，语义是“放弃恢复”。由现有触发器释放 refs，现有 GC 在宽限期后回收。
2. 看门狗只报告持有协议存储最多的 failed Work，**不自动取消**。所有者同意后，由 Yuki 在普通轮次里用现有 Work 管理工具取消。
3. Work 管理工具、管理端和个人设备 API 三个入口的行为一致。
4. 不做：按时间自动过期 failed Work、自动取消、新的保留期配置或阈值。
5. Desktop 如果需要改动，本书只列出，不实现。如果需要迁移，编号用 0162（本方案推荐不需要，见 Q9）。

## 3. 方案

### 3.1 放宽 failed 的取消前提（删除限制，不新增机制）

- `work_management.py:200` 改为：终态只放行 `resume failed` 和 `cancel failed`，其余组合（`fail failed`、任何作用于 `completed`/`cancelled` 的操作）仍返回 `precondition_failed`。只需改这一行条件，`cancel` 分支原样复用：`stop_owned_execution`、写 `cancelled`、`revision+1`、清 `accepted_control`，有父 Work 时给父 mailbox 写 `worker-result`。
- `management_view` 改为 `"cancel": not terminal or row["state"] == "failed"`（与 `resumable` 同形）。
- 触发器、GC、索引和 schema 都不改。refs 在同一写事务中由 C1 的触发器删除，对象在宽限期 `GC_GRACE_SECONDS=86400` 之后由维护 tick 回收，`runtime_protocol_usage` 按删除触发器扣减。
- 四个入口（C4）都不需要单独改代码，原回执、重放和 `version_conflict` 合同照旧。管理端和客户端的取消原因仍分别是 `operator_cancelled` 和 `person_cancelled`，`manage_task` 仍是 `text`；原失败码保留在 `failure_json`（C10）。
- 委派记录（C11）见 Q6。

### 3.2 看门狗附上 failed 持有者（只读）

在 `_check_works` 的 `storage` 事实中加入以下字段，`protocol_storage_near_cap` 和 `protocol_storage_capacity` 两个问题都会带上：

```
"failed_holders": [            # 按 exclusive_bytes 降序，再按 held_bytes、work_id；最多 N 条（Q4）
  {"work_id", "parent_work_id", "origin": _code(source.origin|owner), "conversation_id",
   "reason": _code(work.reason), "failed_at": _iso(updated), "model_requests",
   "held_bytes",               # 该 Work 引用的全部对象字节
   "exclusive_bytes"}          # 只被该 Work 引用的字节 = 取消它最终能回收的量
],
"failed_holders_total": <有 refs 的 failed Work 数，精确>,
"failed_only_bytes": <所有引用都来自 failed Work 的对象字节，精确，即全部放弃时的上界>
```

- 查询放在一个 reader 里，只在已经满足上面阈值的那次检查中才计算（Q2），平时的 tick 不付这份成本。检查失败时沿用现有的 undetermined 逻辑。
- SQL 草图：`runtime_work(state='failed')` 走 `(state, updated)` 索引，join `refs`（主键前缀），再 join `objects`；`exclusive` 用 `NOT EXISTS (refs r2 WHERE r2.sha256=r.sha256 AND r2.work_id<>r.work_id)`（走 `ix_protocol_refs_digest`）；`GROUP BY work_id`。
- 不读 goal、source 正文或 checkpoint（Q1）。`origin` 只取 `CLASSIFICATION_KEYS` 的分类码。
- `CONTENT` 指示不改，它已经要求“写出处理需要的内部 ID、只汇报、同意后在普通对话里处理”。看门狗本身不调用 `manage_work`，也不写 `runtime_work`。

### 3.3 文档（与代码同一 PR）

- `docs/architecture/main-agent-runtime.md:650-654`：在“failed 的 Work 仍可 resume … 保留 refs”后补一句：所有者可以 `cancel` 放弃恢复，`failed → cancelled` 后释放 refs；看门狗在存储接近上限时列出 failed 持有者。
- `docs/architecture/persistent-subagents.md:130-131`：同步“failed 仍可 resume，保留引用；取消即放弃恢复并释放”。
- `docs/architecture/admin-api.md:123`、`docs/architecture/client-api.md:206`：cancel 接受 failed（放弃恢复），其余终态仍返回 `precondition_failed`。
- `docs/architecture/README.md`：索引加入本任务书。迁移 head 不变。

### 3.4 Desktop（只列出，不实现）

- `src/renderer/views/Works.tsx:498,1087`：`!terminal(data)` 改为 `!terminal(data) || data.state === 'failed'`；failed 时按钮文案用“放弃恢复”，`CancelWorkSheet` 说明“取消后不能再恢复，已发送内容与回执保留”。
- `src/shared/locales/{en,zh-Hans,zh-Hant}/works.ts` 新增文案。
- `tests/e2e/work.spec.ts` 增加用例：failed Work 显示放弃恢复，取消后显示 cancelled。
- OpenAPI 与 `contract.gen.ts` 不变（请求和响应形状不变）。个人 Client API revision 不升（Q10）。

## 4. 失败情况（先写，再写代码）

### 4.1 取消一侧

| # | 失败情况 | 验证方式 |
|---|---|---|
| F1 | failed Work 在任一入口取消都返回 `precondition_failed`（现状） | S2：四个入口逐个取消 failed Work |
| F2 | 入口之间不一致：某个入口仍然拒绝，或者状态、revision、回执、重放结果不同 | S2：对比四个入口的结果与回执；同一 `request_id` 重放返回原结果 |
| F3 | `management_view` 对 failed 给出 `cancel:false`，或对 completed/cancelled 给出 `cancel:true` | S2：管理端 `GET /admin/v1/works/{id}` 的 actions |
| F4 | `failed → cancelled` 后 refs 没释放；或误删了别的 Work 的 refs；或另一个活 Work 仍引用的共享对象被 GC 删除 | S1：两个 failed Work 和一个 running Work 共享一个对象 |
| F5 | GC 在宽限期内就删除对象，或者过了宽限期仍不删除；`runtime_protocol_usage` 的下降量不等于实际回收的字节 | S1：宽限期内外各跑一次维护 tick，核对文件和计数 |
| F6 | 取消后仍能 resume；或者 resume 之后（queued/running）的 cancel 与现有取消不同 | S3 |
| F7 | resume 与 cancel 用同一 revision 并发：两者都成功，或者输家得到的不是 `version_conflict`，或者最终状态与赢家的回执不符 | S3：同一 revision 下 `asyncio.gather` 两个 writer |
| F8 | revision 过期仍然写入或改动了 refs；客户端没有记录 problem 回执 | S3 |
| F9 | `fail` 作用于 failed、或任何操作作用于 completed/cancelled 被放行；同一状态重复写入时触发器又跑一遍 | S3 |
| F10 | 后代处理不当：非终态后代没有被取消；终态后代（failed/completed 工作者）被改写了原状态；后代的 refs 被提前或错误地删除 | S3：failed 根下挂 failed 工作者和 completed 未归档工作者（Q5） |
| F11 | 取消一个 failed 工作者时：父 Work 没收到 `worker-result`；或者挂起、终态的父 Work 被复活；或者父 Work 在有活动等待时被错误地排队 | S3 |
| F12 | 已经被 7 天归档的 failed 根（journal、refs 已删）无法取消，或者取消时报错 | S3：构造 C6 形态 |
| F13 | failed Work 残留的 pending 输入或活动等待在取消后又把它唤醒 | S3 |
| F14 | 改写了原事实：已结算为 failed 的自动化运行、插件作业或 SELF 运行被改成 cancelled；`failure_json`、effects、journal 行或已确认发送被删改 | S1/S3：核对行快照 |
| F15 | failed 委派记录取消后返回 `cancelled`，状态却仍是 failed（现状 C11） | S3（取决于 Q6） |
| F16 | 越权：个人设备取消别人的 failed Work 没有返回 `not_found`；`manage_task` 取消不可见的 Work 没有返回 `work_not_found_or_not_authorized` | S2 |

### 4.2 看门狗一侧

| # | 失败情况 | 验证方式 |
|---|---|---|
| W1 | 存储达到告警线时事实里没有 failed 持有者；或者在阈值以下也去计算、写入 | S1：阈值上下各跑一次 tick；用 SQL trace 计数 |
| W2 | 排序或字节数错误：共享对象被算进 `exclusive_bytes`；`failed_holders_total`、`failed_only_bytes` 不精确；列表超过 N 条 | S1：手算期望值对比 |
| W3 | facts 或模型请求中出现 goal、source 正文或自由文本原因（隐私，C13） | S1：在 goal 和失败消息中埋入特征串，断言 `facts_json` 与 fake provider 收到的请求中都没有 |
| W4 | 看门狗修改了 Work：tick 前后 `runtime_work` 的 state/revision/updated 或 refs 发生变化，或者调用了 `manage_work` | S1：tick 前后对行做快照比对 |
| W5 | 持有者查询占用 writer | S1：tick 期间 writer 计数（沿用看门狗 E2E 的 F7 手法） |
| W6 | 持有者查询失败时把存储问题误清除，或者整个检查循环停止 | S1：注入查询异常 |
| W7 | 通知回合本身获得了管理权，或在回合里取消了 Work | S1：沿用看门狗 F5 断言 |
| W8 | 所有者同意后，Yuki 在普通轮里取消了错误的 ID，或者用了看门狗来源的授权 | S1：主人普通轮中 fake provider 调用 `manage_task(cancel,id)`，核对回执上的 actor 是主人 |

## 5. E2E 设计与可复现产物

文件：`tests/integration/test_failed_work_abandon_storage_e2e.py`。文件头先写 §4 的 F1–F16 和 W1–W8，再写代码。产物写到 `test-results/failed-work-abandon-storage-e2e.json`。

运行环境：独立的 SQLite 文件；真实 `ProtocolStore` 文件；真实 `WorkControl.recover_failure` 制造 failed Work（同 `test_work_settlement_writer.py:398-400`）；真实 `Watchdog`、Main（`generate_origin_turn`）和 Social；fake provider（沿用 `test_watchdog_capacity_incident_e2e.py` 的 harness）；真实 `WorkScheduler` 维护 tick 负责 GC（沿用 `test_protocol_gc_backlog_e2e.py` 把 `prepared_at` 往前拨的方式模拟过宽限期）。时间只通过偏移时钟移动。

- **S1 生产形态全链路**：按 #666 表格的比例造三个持有对象的 failed Work（另有一个对象与一个 running Work 共享），设置 `cap` 使 usage 越过告警线 → 看门狗 tick，facts 列出持有者，主动私聊回合只汇报 → 主人普通轮回复“放弃第一个”，fake provider 调用 `manage_task(cancel, id)` → refs 释放 → 宽限期内跑维护 tick，不删 → 宽限期外跑维护 tick，文件删除，usage 下降量正好等于 `exclusive_bytes` → usage 回到告警线以下，看门狗问题在 `no_progress_seconds` 后清除。
- **S2 入口一致性**：四个入口（`manage_task`、管理端 HTTP、管理聊天命令、个人设备 API）各取消一个同形的 failed Work，比较 `{state, revision 增量, reason 类别, refs 是否释放, 回执 operation/problem, 重放结果}`；再加上 F3、F16。
- **S3 失败矩阵**：F6–F15 每项一个子场景，断言状态、refs 和原事实行。

产物结构：每个场景记录 `{case, before, after, receipts, usage_bytes, refs_by_label, files_present}`。Work ID、sha256 和时间都映射成稳定标签或相对偏移，所以产物与运行无关。末尾附上整份规范化 JSON 的 `sha256`。**可复现验收**：连续跑两次，两份产物的 `sha256` 相同：

```bash
uv run pytest tests/integration/test_failed_work_abandon_storage_e2e.py -q
cp test-results/failed-work-abandon-storage-e2e.json /tmp/run1.json
uv run pytest tests/integration/test_failed_work_abandon_storage_e2e.py -q
diff <(jq -S . /tmp/run1.json) <(jq -S . test-results/failed-work-abandon-storage-e2e.json)
```

其余验证按风险挑选：`ruff`、`mypy`；`tests/unit/test_closed_work_protocol_refs.py`、`test_work_settlement_writer.py`、`test_admin_control_review.py`、`test_client_works.py`；`tests/integration/test_watchdog_capacity_incident_e2e.py`、`test_protocol_gc_backlog_e2e.py`、`test_acp_work_cancel.py`。不跑无关的全量检查。

## 6. 待决问题（每项附推荐，等用户决定）

- **Q1 “目标摘要”与看门狗隐私合同冲突。** #666 要求附上目标摘要，但 #664 §4.6 明确规定 facts 不含 Work goal，而且收件人是所有超管，Work 可能属于别人的私聊。**推荐**：不放 goal，只放 `origin` 分类码、`conversation_id`、`model_requests`、原因码和时间；Yuki 在普通轮里可以按执行处授权用 `get_tasks(id)` 查看。备选：只有当 Work 发起人就是收件超管本人时才附 goal，但这要把 facts 按人分开计算，增加复杂度，不推荐。
- **Q2 什么时候报告持有者。** #666 写的是“附在协议存储事实里”，也就是只在 usage 达到 80% 或出现容量挂起时才报。按 #666 的数据，生产现在约 1.43 GB，低于约 1.72 GB 的告警线，**目前不会触发**。**推荐**：按 #666 只附在现有两个存储问题里，不新增问题键，也不新增阈值（开发约束禁止经验阈值）；现存的 1141 MB 可以由主人现在直接用 `manage_task` 取消。备选：新增独立问题键 `protocol_failed_holders`，但需要一个新阈值，不推荐。
- **Q3 字节口径。** **推荐**同时给出 `held_bytes`（引用量）和 `exclusive_bytes`（取消后实际能回收的量），按后者排序，再加 `failed_only_bytes` 作为全部放弃时的上界。
- **Q4 列几条。** **推荐**最多 5 条；总数和总字节精确给出。`_LISTED_IDS=20` 用于纯 ID 列表，每条带多个字段时 5 条已足够让主人决策。
- **Q5 终态后代。** 取消 failed 根时，`stop_owned_execution` 只取消非终态后代。failed 或 completed 的工作者保持原状态，它们的 refs 在根取消 7 天后随工作者归档一并释放（C6）。**推荐**：沿用现有取消的同一子树语义，不级联改写终态后代（不改原事实，也不加机制）。备选：级联把 failed 后代也置为 cancelled，可以立即释放，但会改写它们的原状态。
- **Q6 failed 委派记录（C11）。** 记录没有协议 refs，与存储无关；但通过 `manage_task` 取消时，会出现“返回 cancelled、状态仍是 failed”的不一致，属于“Work 管理工具行为一致”的范围。另外，个人设备 API 取消委派记录时直接调用 `manage_work`（`client_api/works.py:999`），放宽 C2 之后它会真的把 failed 记录改成 cancelled，而 `manage_task` 不会，两个入口会出现新的分歧。**推荐**：在本 PR 中把 `subtasks.py:435` 的 `not in TERMINAL` 放宽为 `not in {"completed","cancelled"}`，让 failed 记录真实地变成 cancelled（复用 `manage_work`，原生回合已经结束，不再发送原生 cancel）。备选：不在本 issue 处理，另开 issue。
- **Q7 工具说明是否改。** `manage_task` 的说明已写明“cancel 终止 id 指定的任务”，没有排除 failed。**推荐**不改说明，避免固定工具合同和 manifest 变动；错误码文案也不变。
- **Q8 持有者列表过时。** `facts_json` 在问题打开时冻结（C12）。在主人回复之前，列出的 Work 可能已被 resume 并正在运行，此时取消的是一个运行中的 Work。**推荐**：不加任何机制。facts 带有观察时间，取消发生在主人明确同意之后、由主人授权的普通轮里，`manage_task` 按当时的真实状态执行。
- **Q9 迁移。** 触发器已经覆盖 `failed → cancelled`，索引也已存在（C1、C14）。**推荐**不需要迁移，0162 不占用；也不回填，生产上哪些 failed Work 要放弃由主人逐个决定。
- **Q10 个人 Client API revision。** 请求和响应形状不变，只是 failed 从 409 变为成功。**推荐**不升 revision；Desktop 更新按钮条件即可（§3.4，只列出）。

## 7. 不做

按时间自动过期 failed Work；自动取消；新的保留期或阈值配置；看门狗管理权；改触发器或 GC；回填或批量清理生产数据；Desktop 实现；合并、部署、重启服务。

## 8. 实施顺序（批准后）

1. 写 E2E 文件头的失败清单（§4），并补全场景骨架。
2. 改 `work_management.py` 两处条件；按 Q6 的决定改 `subtasks.py`。
3. 加看门狗持有者查询（放在 `runtime/protocol_store.py` 中，与 `stalled_unowned_object` 并列），并接入 `_check_works`。
4. 改 §3.3 列出的文档。
5. 跑 §5 的验证，产物跑两遍比对 `sha256`；推送分支、开 PR，不合并、不部署。
