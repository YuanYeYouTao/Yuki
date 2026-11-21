# Yuki failed Work 放弃恢复与协议存储持有者报告任务书（2026-11-20）

状态：**第二阶段：决定已定（§2.2，Q6 按用户收窄更新），已实施，PR 待 CI，未合并**。第一阶段调研见 §1，实施记录见 §8。对应 [#666](https://github.com/YuanYeYouTao/Yuki/issues/666)。分支 `feat/666-cancel-failed-work`，基线 origin/main `9d5242ea`（#664 看门狗、#665、#667 已合入；迁移 head `0161`）。

标记约定同看门狗任务书：【实证】是源码或现有测试里查到的事实；【issue】是 #666 给出的生产数据，本阶段没有再去生产只读复核；【假设】是还没查实、需要在实施或 E2E 中确认的判断；【决定】是用户已定的取舍；第一阶段的【推荐】已由用户逐项决定，见 §2.2。文中路径省略 `src/yuki/`，行号以 `9d5242ea` 为准。

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
| C17 | `get_tasks` 的模型视图（`task_view`）不含 revision；`manage_task` 对非当前目标的 cancel/fail/resume 在本次调用内用 reader 读出候选行，再把它的 revision 交给 `manage_work` 做 CAS。所以“读到写”之间的并发 resume 会得到 `version_conflict`；但如果 resume 发生在 Yuki 查看之后、调用 `manage_task` 之前，这次调用会读到新的 revision，照样取消已被恢复的 Work | `runtime/work_control.py:114-128,1030-1056` |
| C18 | 委派原生回合的取消只由 `ExternalAgentService.reconcile` 的退役扫描发出：只针对 `SandboxTaskRun.status='waiting'`、未 detach、所属 Work 已终态的操作，并且只在 `_view` 仍是 `pending` 时发送 cancel。failed 和 cancelled 都属终态，所以 failed → cancelled 不会让已结束的原生回合重新进入扫描；`manage_external` 的 cancel 分支本身也不调用原生服务 | `external_agents/service.py:1494-1570,1602-1655`；`external_agents/subtasks.py:423-455` |

## 2. 决定

### 2.1 第一阶段已定【决定，用户】

1. 采用 #666 方案 2：放宽条件，允许 `failed → cancelled`，语义是“放弃恢复”。由现有触发器释放 refs，现有 GC 在宽限期后回收。
2. 看门狗只报告持有协议存储最多的 failed Work，**不自动取消**。所有者同意后，由 Yuki 在普通轮次里用现有 Work 管理工具取消。
3. Work 管理工具、管理端和个人设备 API 三个入口的行为一致。
4. 不做：按时间自动过期 failed Work、自动取消、新的保留期配置或阈值。
5. Desktop 如果需要改动，本书只列出，不实现。如果需要迁移，编号用 0162。

### 2.2 第二阶段逐项决定与理由【决定，用户 2026-11-20 审查】

| # | 决定 | 理由 |
|---|---|---|
| Q1 | **不暴露 goal**。按现有看门狗隐私合同（#664 §4.6），持有者只给内部 ID、来源分类码（`origin`/`owner`）、会话 ID、原因码、时间、计数、字节和 revision。需要知道任务内容时，Yuki 在普通轮里用 `get_tasks(id)` 查询，由执行处按授权判断 | 收件人是所有超管，而 failed Work 可能属于别人的私聊或群；`facts_json` 会持久化，并在激活时进入模型请求。这里**有意不满足** issue“目标摘要”这一项，改用分类码加授权查询：保守的隐私取舍，不另设按人分开计算 facts 的机制 |
| Q2 | **只附在现有容量事实里**（`protocol_storage_near_cap`、`protocol_storage_capacity`），不新增问题键或阈值。**低于阈值不会报告**：按 #666 的数据，生产约 1.43 GB，低于 2 GiB × 0.8 ≈ 1.72 GB，现在不会触发；现存的 1141 MB 由主人直接让 Yuki 取消 | 与 issue 的“附在协议存储事实里”一致；开发约束禁止新增经验阈值；平时的 tick 不付持有者查询的成本 |
| Q3 | 列表**按 `held_bytes` 降序**排列（相同时按 `exclusive_bytes` 降序，再按 `work_id`），符合 issue 的“持有最多”。另给 `exclusive_bytes` 和 `failed_only_bytes`，并在 facts 中写明：两者只是**观察时刻的可回收估算或上界**，会受共享引用、24 h GC 宽限期和之后的新引用影响，**不保证实际回收量** | “持有”指引用量，按它排序与 issue 一致；独占量能帮主人判断取消的实际效果，但内容寻址的对象可能被其他 Work 共享或之后再次引用，所以只能写成估算 |
| Q4 | **最多列 5 条**；总数 `failed_holders_total` 和上界 `failed_only_bytes` 精确给出 | 每条带 10 个字段，5 条足够主人决策，也控制了运行资料的长度 |
| Q5 | **不级联改写终态后代**。沿用现有 cancel 的子树语义：只取消非终态后代；failed 或 completed 的工作者保持原状态，它们的 refs 在根取消 7 天后随工作者归档释放 | 不改原事实，不新增机制；根取消后，现有归档会接手剩下的 refs（C6） |
| Q6 | **修复 failed 委派记录的虚假成功，并且只对 failed 记录开放 cancel**（用户收窄后的决定）。`manage_task cancel` 把 failed 记录真实写入 cancelled（走 `manage_work`），并在同一个结算 writer 里核对读到的 revision。对**当前是 failed** 的委派记录调用 release 时，在任何原生操作之前直接拒绝，提示“已失败的委派先用 cancel 放弃恢复；需要交还会话的话，之后再 release”。cancelled 记录的 release 保持 main 原行为（释放 Yuki 的操作，记录不变）；停止观察不改（沿 main）。个人设备 API 和管理端只有 cancel，直接调用 `manage_work`，结果同样是 cancelled，三个入口一致。验证：已结束的原生回合**不会再收到取消**（不调用原生服务，不登记新操作） | 当前返回 `cancelled` 而状态仍是 failed，是对事实的错误报告；放宽 C2 之后，个人设备 API 会真的改成 cancelled，如果 `manage_task` 不改，入口之间会出现新分歧。release 必须先向原生服务发出 detach，这个外部调用不能和状态核对放进同一个写事务（第三轮 F21 的窗口）；只开放 cancel，就让“放弃恢复”完全落在一个写事务里，交还会话改为 cancel 之后的普通 release，不需要新机制。completed 记录的同类问题不在本 issue 范围，见 §6 |
| Q7 | **不改工具合同**：`manage_task` 和 `get_tasks` 的声明（说明和参数 schema）不变，manifest 不变 | 声明已经覆盖“cancel 终止 id 指定的任务”；修改声明会让固定合同和缓存前缀变化，而本问题不需要 |
| Q8 | **facts 可能过时**（问题打开时冻结，C12）。普通轮执行所有者同意的“放弃恢复”之前，必须核对当前状态和 revision，防止目标在此期间被并发 resume 后误取消正在运行的任务。设计用现有版本保护：`WorkControl` 记下本次激活里 `get_tasks(id)` **读到 failed 状态**的 Work 的 revision，之后对同一非当前目标调用 `manage_task` 的 cancel/fail/resume（包括子任务 cancel）时，把这个 revision 作为期望值交给 `manage_work` 的 CAS；revision 已变化则返回 `version_conflict`，提示先用 `get_tasks` 重新查看，不写任何东西。Yuki 自己成功管理目标后记录即失效；再次读到非 failed 状态时也清除；记录只在本次激活内有效，不跨轮。只记 failed 的原因：failed Work 不执行，只有管理操作会改它的 revision；运行中 Work 的 revision 会随自身执行不断推进，绑定它会误伤正常取消（实施时被现有测试 `test_subagent_lifecycle.py::test_foreground_manages_original_child_without_accepting_another_work` 发现，见 §8）。独立审查后，这一核对覆盖普通 Work、子任务、委派记录和中性前台 resume 四条路径；失败后的重试同样受保护；委派记录的 cancel 在结算写事务里核对、resume 在派发前的重开写事务里核对；failed 委派记录只开放 cancel（§3.2、§3.3，F20、F21）。没有先查看时，行为与现在相同（调用内现读）。看门狗不获得任何管理权。facts 每条带上观察时的 `revision`，供管理端和个人设备按现有 `expected_revision` 操作 | 保守选择：所有者同意的是放弃“那个 failed 的 Work”；把 Yuki 实际看到的 revision 绑定到现有 CAS，就不会误取消被别人恢复的任务。不改工具声明（Q7），不新增状态机或锁，只复用 `manage_work` 已有的 revision 检查。**剩余缺口**：如果 Yuki 没有先用 `get_tasks` 查看就直接取消，而目标在此之前已被恢复，这次取消仍会生效，这与现有取消运行中 Work 的语义相同。完全消除这个缺口需要 `manage_task` 接受显式 revision 参数，属于工具合同变更，已被 Q7 排除 |
| Q9 | **不需要迁移，不回填** | 触发器已覆盖 `failed → cancelled`，索引已存在（C1、C14）；哪些 failed Work 要放弃由主人逐个决定，程序不批量清理生产数据 |
| Q10 | **不升个人 Client API revision** | 请求和响应形状不变，只是 failed 从 409 变成成功；Desktop 更新按钮条件即可（§3.5，只列出） |

## 3. 方案

### 3.1 放宽 failed 的取消前提（删除限制）

- `work_management.py:200` 改为：终态里只放行 `resume failed` 和 `cancel failed`；`fail failed`，以及任何作用于 `completed`/`cancelled` 的操作，仍返回 `precondition_failed`。`cancel` 分支原样复用：`stop_owned_execution`（只取消非终态后代，Q5）、写入 `cancelled`、`revision+1`、清 `accepted_control`；有父 Work 时，给父 mailbox 写 `worker-result`（父 Work 处于等待外部且没有活动等待时才排队，挂起或终态的父 Work 不复活）。
- `management_view` 改为 `"cancel": not terminal or row["state"] == "failed"`。
- 触发器、GC、索引和 schema 都不改（Q9）。refs 在同一写事务中由 C1 的触发器删除；对象在宽限期 `GC_GRACE_SECONDS=86400` 之后由维护 tick 回收，`runtime_protocol_usage` 按删除触发器扣减。原失败码保留在 `runtime_work_recovery.failure_json`（C10）。

### 3.2 failed 委派记录（Q6）

- 结算 writer 的条件由 `row["state"] not in TERMINAL` 改为“非终态，或者（cancel、非观察、且当前是 failed）”：只有 cancel 会把 failed 记录真实写入 cancelled（走 `manage_work`）。cancel 分支原本就不调用原生服务（C18）。
- release（非观察）在调用 `service.release` 之前先读当前状态，是 failed 就返回 `failed_record_release`（文案见 Q6），不做任何原生操作。cancelled/completed 记录的 release、活记录的 release，以及停止观察，都与 main 相同。

### 3.3 普通轮的版本核对（Q8）

`WorkControl` 增加一个只在本次激活内有效的 `observed_revisions: dict[str, int]`：

- `_get_tasks` 按 id 读到 failed 时记下该行的 `revision`，读到其他状态时清除记录（列表读取不带 revision，不记录）。
- **记录只在调用成功或重新读取时结束**。任何失败（包括 `version_conflict`）都保留记录，所以同一激活里不重新读取就重试，会再次得到 `version_conflict`，不会改用最新 revision 绕过保护。
- `_manage` 的四条路径都把读到的 revision 交给真正写入的地方：
  - **普通 Work**（cancel/fail/resume）：作为 `manage_work` 的期望 revision，在同一个 writer 里做 CAS。
  - **子任务**：作为 `manage_subtask(..., revision=)` 的显式参数。cancel 交给 `SubagentRepository.cancel` 的 `manage_work` CAS；resume 在 `SubagentRepository.resume` 的 writer 里比对；带 text 的 resume 在 `SubagentRepository.message` 的 writer 里比对。`manage_subtask` 把 `WorkManagementError` 转为工具错误（以前会让整轮以 `internal_failure` 结束）。
  - **委派记录**：作为 `manage_external(..., revision=)`。cancel 在结束记录的同一个结算 writer 里比对（核对与状态修改同一事务）；resume 的重开写入加上 `revision` 条件，没有命中则在派发前返回 `version_conflict`。release 与停止观察不使用读到的 revision：failed 记录的 release 直接拒绝，其余 release 与停止观察沿 main。
  - **中性前台 resume**（没有当前 Work、入队交给原执行链）：入队前先比对候选行，再把读到的 revision 作为 `enqueue(..., resume_revision=)` 传入；在插入输入、改为 queued 的同一个 writer 里再比对一次，不符则整个事务回滚，什么都不写。
- `_ERROR_MESSAGES` 增加 `version_conflict` 的文案：“该任务在你查看后状态已变化，操作未执行；先用 get_tasks 重新查看再决定。”这是工具结果文案，不属于工具声明（Q7）。
- **设计限制**（不新增机制，如实说明）：
  1. **release 窗口已按用户决定消除，而不是如实告知**。第三轮发现：release 必须先向原生服务发 detach（release 时还会让连接器在回合结束后关闭 Yuki 打开的原生进程），这个外部调用不能和版本核对放进同一个写事务，所以“读取后被其他入口 resume、再 release”会先产生原生副作用。按用户收窄，failed 记录只开放 cancel：放弃恢复由 cancel 在一个写事务里完成（核对 + 写入），release 不再参与 failed 记录的放弃，也不使用读到的 revision。剩下的情况都是 main 原行为：读到 failed 之后，若记录在 release 之前被别处恢复，此时的 release 就是对活记录的普通 release；若记录在拒绝检查之后才变成 failed，release 照 main 释放操作、不改状态。
  2. 中性前台 resume 的比对覆盖到入队 writer 为止（同一事务）；入队之后的执行由原执行链按来源与状态重新核验。
  3. 没有先用 `get_tasks(id)` 读取时，照旧按调用时的当前 revision 执行；这是 Q7（不改工具声明）的限制。
- **三个入口**：管理端和个人设备 API 对委派记录只有 cancel，直接调用 `manage_work`（带 `expected_revision` 的 CAS），不调用原生服务；Work 管理工具 `manage_task` 的 cancel 也在一个写事务里核对并写入，结果都是 cancelled。release/停止观察只存在于 `manage_task`（Desktop 的“这台电脑”操作经 `agent_target` 映射为同一个 `manage_task`），failed 记录的 release 被拒绝，其余沿 main。
- `_ERROR_MESSAGES` 增加 `version_conflict` 的文案：“该任务在你查看后状态已变化，操作未执行；先用 get_tasks 重新查看再决定。”这是工具结果文案，不属于工具声明（Q7）。

### 3.4 看门狗附上 failed 持有者（只读）

`runtime/protocol_store.py` 新增只读函数 `failed_holders(reader, limit)`，与 `stalled_unowned_object` 并列。`_check_works` 只在 `used ≥ cap × warn_ratio` 或存在容量挂起时，在同一个 reader 里调用它；结果写进 `storage`，两个存储问题都会带上：

```
"failed_holders": [            # held_bytes 降序，再按 exclusive_bytes 降序、work_id；最多 5 条
  {"work_id", "parent_work_id", "revision", "origin", "owner", "conversation_id",
   "reason", "failed_at", "model_requests", "held_bytes", "exclusive_bytes"}
],
"failed_holders_total": <有 refs 的 failed Work 数>,
"failed_only_bytes": <所有引用都来自 failed Work 的对象字节>,
"reclaim_note": "exclusive_bytes/failed_only_bytes 是观察时的可回收估算或上界，受共享引用、GC 宽限期和之后的新引用影响，不保证实际回收量"
```

- SQL：`runtime_work(state='failed')` 走 `(state, updated)` 索引，join `refs`（主键前缀），再 join `objects`；`exclusive` 用 `NOT EXISTS` 检查同一 sha256 是否有其他 work_id 的 ref（走 `ix_protocol_refs_digest`）；`failed_only` 从 failed 的 digest 集合出发，用 `NOT EXISTS` 排除被非 failed Work 引用的对象。
- 不读 goal、source 正文或 checkpoint；`origin`/`owner`/`reason` 经过 `_code()`（Q1）。查询失败时沿用现有的 undetermined 逻辑。
- `CONTENT` 指示不改。看门狗不调用 `manage_work`，也不写 `runtime_work`。

### 3.5 文档（与代码同一 PR）

- `docs/architecture/main-agent-runtime.md:650-654`：failed 可以 cancel 放弃恢复，`failed → cancelled` 后释放 refs；看门狗在容量事实中列出 failed 持有者（估算口径）；普通轮的取消绑定 `get_tasks` 读到的 revision。
- `docs/architecture/persistent-subagents.md:130-131`：同步“failed 仍可 resume，保留引用；取消即放弃恢复并释放”。
- `docs/architecture/admin-api.md:123`、`docs/architecture/client-api.md:206`：cancel 接受 failed；其余终态仍返回 `precondition_failed`。
- `docs/architecture/README.md`：更新本任务书索引状态。迁移 head 不变。

### 3.6 Desktop（只列出，不实现）

- `src/renderer/views/Works.tsx:498,1087`：`!terminal(data)` 改为 `!terminal(data) || data.state === 'failed'`；failed 时按钮文案用“放弃恢复”，`CancelWorkSheet` 说明“取消后不能再恢复，已发送内容与回执保留”；请求照旧带 `expected_revision`，409 `version_conflict` 时刷新。
- `src/shared/locales/{en,zh-Hans,zh-Hant}/works.ts` 新增文案；`tests/e2e/work.spec.ts` 增加 failed → 放弃恢复 → cancelled 的用例。
- OpenAPI 与 `contract.gen.ts` 不变（Q10）。

## 4. 失败情况（先写，再写代码）

### 4.1 取消一侧

| # | 失败情况 | 场景 |
|---|---|---|
| F1 | failed Work 在任一入口取消都返回 `precondition_failed`（现状） | S1、S3、S4 |
| F2 | 入口之间不一致：某个入口仍拒绝，或者状态、revision 增量、回执、重放结果不同 | S3、S4 |
| F3 | `management_view` 对 failed 给出 `cancel:false`，或对 completed/cancelled 给出 `cancel:true` | S3 |
| F4 | `failed → cancelled` 后 refs 没释放；误删了别的 Work 的 refs；或另一个活 Work 仍引用的共享对象被 GC 删除 | S1、S3 |
| F5 | GC 在宽限期内就删除对象，或者过了宽限期仍不删除；`runtime_protocol_usage` 的下降量不等于实际回收的字节 | S1 |
| F6 | 取消后仍能 resume | S5 |
| F7 | 同一 revision 下 resume 与 cancel 并发：两者都成功，或者输家得到的不是 `version_conflict`，或者最终状态与赢家的回执不符 | S5 |
| F8 | revision 过期仍然写入或改动了 refs；个人设备没有记录 problem 回执 | S3、S5 |
| F9 | `fail` 作用于 failed、或任何操作作用于 completed/cancelled 被放行 | S3、S5 |
| F10 | 非终态后代没有被取消；终态后代（failed/completed 工作者）的原状态或 refs 被改动（Q5） | S5 |
| F11 | 取消 failed 工作者时，父 Work 没收到 `worker-result`；挂起的父 Work 被复活 | S5 |
| F12 | 已被 7 天归档的 failed 根（journal、refs 已删）无法取消 | S5 |
| F13 | 残留的 pending 输入或活动等待在取消后把 Work 再次唤醒 | S5 |
| F14 | `failure_json`、journal 行、effects 被删改（自动化运行和插件作业只从运行中结算，由 C9 代码事实保证，不单独构造） | S1、S5 |
| F15 | failed 委派记录取消后返回 cancelled，状态却仍是 failed；或者 `manage_task`、个人设备 API、管理端的结果不同（Q6） | S2、S3 |
| F16 | 越权：个人设备取消别人的 failed Work 没有返回 `not_found` | S3 |
| F17 | Yuki 用 `get_tasks` 看到 failed（revision r）后，另一个入口执行了 resume（r+1），随后的 `manage_task cancel` 取消了已排队的 Work（Q8） | S2 |
| F18 | 版本核对误伤：Yuki 自己在同一激活中成功管理目标后，后续合法操作被旧 revision 拒绝；没有先查看的取消被新增门槛拒绝；记录跨轮泄漏到下一次激活；读取时不是 failed 的 Work（其 revision 随自身执行推进）也被绑定，导致之后无法取消 | S2；现有 `test_subagent_lifecycle.py` |
| F19 | 取消 failed 委派记录时，向原生服务发出取消/release，或者登记了新的原生操作（Q6） | S2 |
| F21 | failed 委派记录开放了 cancel 以外的操作：对当前 failed 记录的 release 到达原生服务（或结束记录），而不是在任何原生操作之前带着“先 cancel”提示被拒绝；cancelled 记录的 release 或停止观察与 main 不同；读到 failed 后记录被其他入口结束，cancel 却回答 cancelled 而不是 `version_conflict` | S7 |
| F22 | 中性前台 resume 通过读取比对后、入队 writer 之前另一入口 resume 了该 Work，Yuki 的输入仍写入并再次排队 | S7（在 `enqueue` 前推进 Work，确定性复现） |
| F20 | Q8 被绕过（独立审查发现）：委派记录的 cancel/resume 不使用读到的 revision，被其他入口推进（再次运行或再次失败）后仍被取消或重开；子任务在调用前就清除记录，`version_conflict` 后不重新读取就重试，结果按最新 revision 取消；子任务 resume（含带 text）和中性前台 resume 不比对 revision；子任务的 `WorkManagementError` 让整轮崩溃 | S6 |

### 4.2 看门狗一侧

| # | 失败情况 | 场景 |
|---|---|---|
| W1 | 存储达到告警线时事实里没有 failed 持有者；或者低于阈值时也去计算、写入（Q2） | S1 |
| W2 | 不按 `held_bytes` 降序；字节数错误（共享对象被算进 `exclusive_bytes`）；`failed_holders_total`、`failed_only_bytes` 不精确；超过 5 条；缺少估算口径说明（Q3、Q4） | S1 |
| W3 | facts、`facts_json` 或模型请求中出现 goal、source 正文或自由文本原因（Q1） | S1 |
| W4 | 看门狗修改了 Work：tick 前后 `runtime_work` 的 state/revision 或 refs 发生变化 | S1 |
| W5 | 观察阶段占用了 writer | S1 |
| W6 | 持有者查询失败时把存储问题误清除，或者检查循环停止 | S1 |
| W7 | 所有者同意后的取消使用了看门狗来源的授权，而不是所有者普通轮的授权 | S1 |

## 5. E2E 与可复现产物

文件：`tests/integration/test_failed_work_abandon_storage_e2e.py`。文件头先写 §4 的失败清单，再写代码。产物写到 `test-results/failed-work-abandon-storage-e2e.json`。

- **S1 生产形态全链路**（复用看门狗 E2E 的 Scene：真实 Main、Social、主动私聊、`PresenceRouter`；fake provider；偏移时钟）：主人的私聊里经真实轮次制造三个 failed Work（模型返回 `LLMInvalidRequestError`），并给它们挂上大小不同的协议对象，其中一个对象与一个活 Work 共享 → 低于阈值 tick：没有存储问题，持有者查询没有调用 → 越过告警线 tick：facts 里的持有者按 `held_bytes` 降序，字节与独立 SQL 手算一致，不含任何 goal 特征串，观察前后 Work 和 refs 不变，观察阶段没有打开 writer，主动回合只汇报 → 主人普通轮：`get_tasks(A)` + `manage_task(cancel, A)`，回执的 actor 是主人 → A 的 refs 释放，`failure_json` 保留 → 宽限期内 GC 不删；宽限期外 GC 删除，usage 下降量等于取消时 A 的独占字节，共享对象保留 → 注入一次持有者查询异常，存储问题保持打开。
- **S2 普通轮版本核对与委派记录**（同一 Scene）：Yuki `get_tasks(B)` 之后，另一入口 resume B → `manage_task cancel B` 返回 `version_conflict`，B 仍是 queued、refs 保留（F17）；同一激活内自己 resume 一个 failed 的 C 后再 cancel C 成功；下一轮不先查看直接 cancel，行为照旧（F18）。failed 委派记录：`manage_task cancel` 后真实变成 cancelled，替身原生服务没有收到任何调用，没有新增原生操作（F15、F19）。
- **S3 个人设备与管理端**（复用 `app_env`：真实 HTTP、个人设备、管理端令牌）：同形的 failed Work 分别经 `POST /client/v1/works/{id}/cancel` 和 `POST /admin/v1/works/{id}/cancel` 取消，比较状态、revision 增量、refs 是否释放、回执、重放；`GET /admin/v1/works/{id}` 的 `actions.cancel`；completed/cancelled 仍返回 409；过期 revision 返回 409，不改 refs，个人设备记录 problem 回执；别人的 failed Work 返回 `not_found`；failed 委派记录经两个入口都变成 cancelled。
- **S4 管理聊天命令**（复用 `stack`：真实 ApplicationContainer）：`/ai admin work cancel <id>` 取消 failed Work，结果与 S3 一致。
- **S5 状态矩阵**（真实 SQLite 与 ProtocolStore，直接调用 `manage_work`/`WorkControl`）：F6、F7（`asyncio.gather` 并发 resume 与 cancel）、F8、F9、F10、F11、F12、F13、F14。

- **S6 每条管理路径**（同一 Scene，替身原生服务）：普通 Work；子任务 cancel/resume/带 text 的 resume；委派记录 cancel/resume，分别对“再次运行”（`waiting_external`）和“再次失败”（状态仍是 failed、revision 变了）两种推进；中性前台 resume。每种情况都是先读到 failed，再由另一入口推进，然后调用一次、不重新读取再重试一次；两次都必须是 `version_conflict`，且没有状态、revision、mailbox 写入，没有原生调用，没有新增原生操作（F20）。
- **S7 failed 委派记录与中性 resume 窗口**：failed 记录 release 两次都被拒绝（`failed_record_release`，文案逐字一致），没有原生调用；随后 cancel 成功；cancelled 记录的 release 沿 main（原生 release 一次，`close_owner=True`，记录不变）；停止观察 failed 记录沿 main（原生 release 一次，`close_owner=False`，状态仍是 failed）；读到 failed 后被其他入口结束，cancel 返回 `version_conflict`（F21）。在 `enqueue` 之前推进 Work，中性 resume 返回 `version_conflict`，没有写入（F22）。

产物只包含标签和关系事实：Work ID、sha256、时间都映射成稳定标签或布尔关系（例如“下降量 = 独占字节”），不写生成时间。末尾附 `results_sha256`。**可复现验收**：连续跑两次，产物文件逐字节相同：

```bash
uv run pytest tests/integration/test_failed_work_abandon_storage_e2e.py -q
cp test-results/failed-work-abandon-storage-e2e.json /tmp/run1.json
uv run pytest tests/integration/test_failed_work_abandon_storage_e2e.py -q
cmp /tmp/run1.json test-results/failed-work-abandon-storage-e2e.json
```

其余验证按风险挑选：`ruff`、`mypy`；`tests/unit/test_closed_work_protocol_refs.py`、`test_work_settlement_writer.py`、`test_admin_control_review.py`、`test_client_works.py`；`tests/integration/test_watchdog_capacity_incident_e2e.py`、`test_protocol_gc_backlog_e2e.py`、`test_acp_work_cancel.py`。不跑无关的全量检查。

## 6. 不做

按时间自动过期 failed Work；自动取消；新的保留期或阈值配置；看门狗管理权；修改触发器或 GC；回填或批量清理生产数据；修改工具声明；Desktop 实现；completed 委派记录被 `manage_task cancel` 时返回 `cancelled` 的同类问题（不是 failed，另开 issue）；合并、部署、重启服务。

## 7. 实施顺序

1. 写 E2E 文件头的失败清单（§4）和全部场景，先确认它们在旧代码上失败。
2. 改 `work_management.py`（§3.1）、`subtasks.py`（§3.2）、`work_control.py`（§3.3）、`protocol_store.py` 与 `watchdog.py`（§3.4）。
3. 改 §3.5 列出的文档。
4. 跑 §5 的验证；产物跑两遍逐字节比对；普通提交（不加署名）；推送分支；开 PR 并等待 CI。不合并、不部署。

## 8. 实施记录

- **提交范围**：`runtime/work_management.py`（放宽前提，修改 `management_view`）、`external_agents/subtasks.py`（Q6）、`runtime/work_control.py`、`runtime/subagent_tools.py`、`runtime/subagent_repository.py`（Q8）、`runtime/protocol_store.py`（`failed_holders`）、`application/watchdog.py`（持有者事实）；文档按 §3.5 修改；新增 E2E `tests/integration/test_failed_work_abandon_storage_e2e.py`。CI `quality.yml` 的 integration 套件增加一步，检查本 E2E 产物存在（与看门狗 E2E 的做法相同）。没有迁移，没有修改工具声明，没有修改 Desktop。
- **先红后绿**：E2E 先于实现写好。在旧代码上 5 个场景全部在预期位置失败：S1 没有 `failed_holders`；S2 failed Work 取消返回 `precondition_failed`；S3 管理端 `actions.cancel=false`；S4 聊天命令返回 `precondition_failed`；S5 `manage_work` 拒绝。实现后 5 个场景全部通过。
- **变异核对**：去掉 Q8 绑定后，S2 中“读到 failed 后被别处 resume”的取消会真的取消已排队的 Work（断言失败）；绑定所有读取（不限 failed）后，S2 的非 failed 读取场景返回 `version_conflict`（断言失败）；把 Q6 改回 `not in TERMINAL` 后，S2 的委派记录仍是 failed（断言失败）。三处都已还原。
- **与本书第一版设计的偏差**：Q8 第一版对所有 `get_tasks` 读取都做绑定，现有测试 `test_subagent_lifecycle.py::test_foreground_manages_original_child_without_accepting_another_work` 发现，运行中的子任务在读取后被自己 resume、发消息并开始执行，revision 推进，之后的取消被误判为 `version_conflict`。已改为只绑定读到 failed 的 Work，并在 S2 补充对应用例（F18 最后一项）。
- **回归中发现并修正**：第一版在 `manage_subtask` 内直接读 `control.observed_revisions`，`test_subagent_result_checkpoint.py` 的 6 个用例以最小控制对象调用它而失败；改为由 `WorkControl._manage` 以显式参数传入，`manage_subtask` 对调用方的依赖不变。`test_work_execution_receipt_regressions.py::...[False-terminal_exec]` 在本机失败，但在基线 `9d5242ea` 的临时工作树中同样失败（沙箱进程在 macOS 上的既有问题），与本改动无关，以 CI（Linux）结果为准。
- **独立审查后的第二轮**：审查指出两处可能绕过 Q8：委派记录路径没有传入读到的 revision，且 release/停止观察有先行副作用；子任务在调用前就清除记录，`version_conflict` 后可以不重新读取就按最新 revision 重试。按全路径复查后，另外发现子任务 resume（含带 text）和中性前台 resume 都不比对 revision，子任务的 `WorkManagementError` 会让整轮以 `internal_failure` 结束。先写 S6（F20），在旧实现上失败（第一处在子任务阶段整轮崩溃）；修复见 §3.3。逐项变异核对都会让 S6 失败：委派记录不传 revision 时取消已被恢复的记录；子任务先清除记录时重试取消成功；去掉中性 resume 比对；去掉委派记录的副作用前比对时 release 被发往原生服务。剩余设计限制见 §3.3。
- **第三轮（同一问题第三轮）**：审查指出 release/停止观察“先发 release、后在 writer 比对”的窗口，并要求核查中性 resume 的“比对与入队”窗口。两者都先写确定性复现。F22 把比对移进入队 writer（同一事务），完全关闭。F21 在当时的设计下无法在范围内关闭，按三轮限制停下，留待用户决定。
- **用户收窄后续做**：failed 委派记录只开放 cancel（核对与状态修改同一写事务）；当前 failed 的 release 在任何原生操作之前拒绝；cancelled 记录的 release 与停止观察沿 main；删除第三轮的 `_RELEASED_MOVED` 与 release 版本冲突分支，以及第二轮放在 `manage_external` 开头的共用比对；保留中性 resume 的写事务复核。先改 E2E 清单与用例（S6 委派记录只剩 cancel/resume，并增加“再次失败”推进；S7 改为 F21 新含义），在旧实现上 S7 失败，再改代码。变异核对：不拒绝 failed release；cancel 不在 writer 比对；中性 resume 不在入队 writer 复核；resume 重开不带 revision。每一种都会让 S6 或 S7 失败（最后一种只有“再次失败”的用例能抓到，因此新增了该用例）。
- **产物**：`test-results/failed-work-abandon-storage-e2e.json`（目录被 gitignore，与其他 E2E 一致）。连续两次运行产物逐字节相同（`cmp`），`results_sha256` 记在产物末尾。
- **验证、CI、合并**：见 PR 描述与最终报告；本书不提前写成已合并或已上线。
