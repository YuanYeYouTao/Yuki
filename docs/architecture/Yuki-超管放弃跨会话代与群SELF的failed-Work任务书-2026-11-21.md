# Yuki 超管放弃跨会话代与群 SELF 的 failed Work 任务书（2026-11-21）

状态：**第二阶段：决定已定（§2，用户 2026-11-21 批准 Q1 选 A、Q2–Q11 按推荐），已在原分支实施，PR #674 保持 draft 待用户核验，未合并**。对应 [#673](https://github.com/YuanYeYouTao/Yuki/issues/673)，接续 [#666](https://github.com/YuanYeYouTao/Yuki/issues/666) / [#671](https://github.com/YuanYeYouTao/Yuki/pull/671)。分支 `docs/673-superadmin-abandon-failed-work-spec`，基线 origin/main `db7b5c26`（#671 已合入；迁移 head `0161`）。第一阶段调研见 §1，实施记录见 §8。

标记约定沿用 #666 任务书：【实证】是源码或现有测试查到的事实；【issue】是 #673 给出的生产数据，本阶段**没有**到生产只读复核；【假设】是尚未查实、需在实施或 E2E 中确认的判断；【决定】是用户已批准的取舍（第一阶段的【推荐】已全部按 §2 批准）。路径省略 `src/yuki/`，行号以 `db7b5c26` 为准。

## 1. 现状与证据

### 1.1 现象【issue】

#671 上线后，所有者让 Yuki 放弃三个持有协议存储最多的 failed Work，Yuki 一个也没取消成功，最后由 operator CLI（`yuki admin work cancel`）取消：

| Work | 会话 | 来源 | Yuki 能否管理（issue 描述） |
|---|---|---|---|
| `8713d099` | 所有者私聊，第 7 代（当前第 7 代） | 所有者消息 | 能，但要完整 ID |
| `dc6b3247` | 同上 | 所有者消息 | 能，但要完整 ID |
| `6cd62f2f` | 群会话 `5b234414`，第 32 代（当前第 44 代） | SELF 自主唤醒 | 任何非 operator 入口都不能 |

### 1.2 入口与授权现状【实证】

| # | 事实 | 依据 |
|---|---|---|
| E1 | `get_tasks(id)` 读**全局安全目录**，不按创建者、会话或 generation 过滤；所以 Yuki 能看见 `6cd62f2f` 的状态和 revision，看不见的是进度（`progress_reason=original_private_source_not_authorized`） | `runtime/work_queries.py:117-150`；`docs/architecture/main-agent-runtime.md:107-112` |
| E2 | `manage_task` 对非当前目标的 cancel/fail/resume 先用 `WorkQueries.get(..., local=True)` 取候选；`local` 要求**同一 conversation、同一 generation、会话仍在该代**，且 `SOURCE_SCOPE_FIELDS`（`actor_person_id` 同人族、`origin`、`plugin_id`、`delegation_id`、`execution_boundary`、`principal_kind`、`initiative_run_id`）逐项相等；工作者租约再限于自身子树。取不到就是 `work_not_found_or_not_authorized` | `runtime/work_queries.py:99-121`；`runtime/work_query_schema.py:9-17`；`runtime/work_control.py:1064-1072` |
| E3 | SELF Work 的来源是 `principal_kind='self'`、`origin='self_initiative'`、带**本次** `initiative_run_id`，不带 `actor_person_id`。因此它只能被**同一次 initiative run** 的激活管理：所有者的消息（`principal_kind='person'`）匹配不上，后来的 SELF 唤醒（另一个 `initiative_run_id`）也匹配不上。会话换代后连那次 run 也不在当前代 | `runtime/work_repository.py:343-388`；E2 |
| E4 | 委派记录走 `authorize_record`：只认记录的 `actor_person_id` 与当前 actor 同人族，否则 `subtask_not_owned` | `external_agents/subtasks.py:487-499`；`runtime/work_control.py:1015-1034` |
| E5 | 个人设备 API 的 `personal_work` 用 `work_access`：Work 的 `actor_person_id` 属本人族，**或** Work 在本人的私聊会话里；不过滤 generation，也不过滤父子。所以 issue 中“只覆盖本人发起、不含 SELF Work”不完全准确：**本人私聊里的 SELF Work 和旧代 Work 都覆盖**；群里的 SELF Work 不覆盖 | `client_api/works.py:73-114,984-1033` |
| E6 | **已有超管入口一：管理聊天命令**。`/ai admin work cancel <完整ID>` 经 `ControlAccess.principal_for_person` 解析发送者原 Person，超管得到 `SUPERUSER_CAPABILITIES`（含 `control.work.mutate`、`control.work.read`），`authorize_current` 在执行处再核 `person_is_superuser`；随后调用同一个 `manage_work`（全局，不限会话/代/来源）。#671 的 S4 已覆盖“超管聊天命令取消 failed Work” | `services/command_service.py:152-153`；`services/commands/work.py`；`admin/control_resolution.py:99-121`；`admin/operations/base.py:46-73`；`admin/authority/capabilities.py:55-56`；`tests/integration/test_failed_work_abandon_storage_e2e.py`（S4） |
| E7 | **已有超管入口二：超管设备 token 调管理 API**。超管 Person 的个人聊天设备可用原 `purpose=chat` token 调 `/admin/v1`，能力等于 `SUPERUSER_CAPABILITIES`，执行处核设备撤销、原/根 Person 有效性和当前资格。`POST /admin/v1/works/{id}/cancel` 同样走 `manage_work`。现有测试只用 operator token 覆盖这条 HTTP 路径，**没有**用超管设备 token 取消 failed Work 的用例 | `admin_api/http.py:160-178,648-660`；`docs/architecture/admin-api.md:19-25`；`tests/unit/test_admin_control_review.py:290-308` |
| E8 | 生产 operator TOML 缺 `control.work.read` 只影响 operator 凭据；E6/E7 的超管能力来自 `SUPERUSER_CAPABILITIES`，不读该 TOML【实证，生产文件内容为 issue】 | `admin/control_resolution.py:126-137` |
| E9 | Desktop 已有指向 `admin/v1` 的 ApiClient（模型设置、平台等超管页面在用），生成的 admin 合同含 `/admin/v1/works`、`/works/{id}`、`/works/{id}/{action}`；任务页只调个人 API，取消按钮条件仍是 `!terminal(data)` | Yuki-Desktop `92cc412`：`src/main/connections.ts:413`，`src/shared/admin-contract.gen.ts:710-760`，`src/renderer/views/Works.tsx:498,1087` |
| E10 | 现行先例：安排（schedules）“普通用户只能管理自己的任务，超级管理员可以管理全部任务；管理员更新不改变 canonical 创建者，后续执行仍按原创建者的当前权限”。执行处用 `person_is_superuser(actor.person_id)` 当场核验 | `docs/architecture/main-agent-runtime.md:275-279`；`automation/service.py:420-443` |
| E11 | 授权合同：超管资格归 Person；当前授权保原 actor Person，核原人物与当前 root 的有效性和当前资格；角色不改固定工具声明；Work 归属不因超管改变；SELF 不借真人或超管身份 | `AGENTS.md`；`docs/architecture/development-contract.md:31-33,53`；`identity/authority.py:10-26` |
| E12 | `manage_work` 在一个 writer 里：核 revision（CAS）、只放行 `cancel/resume failed`、取消非终态后代、写 cancelled、给父 Work mailbox 写 `worker-result`（不复活挂起或终态父 Work）；`protocol_refs_closed_work` 触发器同事务释放 refs。#671 已验证委派记录 cancel 不调用原生服务 | `runtime/work_management.py:190-260`；#671 任务书 C1、C18、Q6 |
| E13 | 看门狗主动回合不绑定 WorkControl，`manage_task` 返回 `work_control_unavailable`；放弃只能在所有者随后的普通轮里以所有者授权执行 | `docs/architecture/main-agent-runtime.md:283-297` |
| E14 | 工作者租约（`WorkLease.work_id` 非空）只能管理自身子树；Main 激活的租约不带 `work_id` | `runtime/subagent_repository.py:155`；`runtime/work_repository.py:204`；`main-agent-runtime.md:113-114` |
| E15 | `get_tasks`/`manage_task` 只接受完整 UUID；短 ID 返回“任务不存在或无权操作”【实证 + issue】 | `runtime/work_control.py:975` |

### 1.3 结论

1. **缺口只在 Yuki 的 `manage_task`**。服务端已有两条超管入口（E6 聊天命令、E7 设备 token 调管理 API）能放弃任意 failed Work，包括群 SELF 与旧代 Work；它们核验的事实正是“原 Person + 当前超管资格 + 执行处核验”。所有者这次没用到它们，是因为 #666 定下的流程是“看门狗报告 → Yuki 用自己的话告诉所有者 → 所有者同意后由 Yuki 用 Work 管理工具取消”，而 `manage_task` 的 `local` 范围（E2/E3）把旧代与 SELF Work 挡在外面。
2. 所以有两种解法：**零代码**（所有者用 `/ai admin work cancel`，或 Desktop 改用 admin API）与**放宽 `manage_task`**（给当前超管一条只做 failed → cancelled 的路径）。见 §2 Q1。
3. 【假设，未核实】生产上 E6 聊天命令可用（`/ai` 命令前缀和私聊接入正常）。本阶段没有去生产验证，实施前若选零代码方案，需要所有者在私聊里对一个 failed Work 实测一次。

## 2. 决定【决定，用户 2026-11-21：Q1 选 A，Q2–Q11 按推荐】

下表保留第一阶段的选项与理由；“推荐”一列现为已批准的决定。

| # | 问题 | 选项 | 决定与理由 |
|---|---|---|---|
| Q1 | **入口放在哪里** | A. 只放宽 Yuki 的 `manage_task`；B. 零代码：所有者用现有 `/ai admin work cancel`，看门狗/Yuki 只提示命令；C. Desktop 任务页改用 admin API；D. A + C | **推荐 A**。#666 已决定“由 Yuki 在普通轮里用现有 Work 管理工具取消”，B 让这个流程在一部分 Work 上失效；A 只放宽 `manage_task` 的一个条件，不新增入口或状态。C 不需要服务端改动（E7、E9），但属于 Desktop 仓库，本书只列出（§3.5），可另开 Desktop issue。B 作为实施前的临时办法保留，不写成正式流程 |
| Q2 | **授权按哪些事实核验**（核心） | 见 §3.1 的 A1–A7 | **推荐 §3.1 全部七条**：当前超管资格按原 actor Person 在写入的 writer 里核验；当前轮必须是这个人自己的直接输入；只在 Main 激活；目标在同一 writer 里仍是 failed；revision 走现有 CAS。不看目标的创建者、会话、generation 或 SELF/人物来源 |
| Q3 | **超管路径开放哪些动作** | A. 只开放 cancel，并且只对 failed → cancelled；B. 也开放 resume；C. 也开放 cancel 运行中的 Work | **推荐 A**。issue 要的是放弃恢复；resume 会以别人的原来源继续执行，产生新效果，必须按原来源授权（E11），超管资格不能代替；取消运行中的 Work 是另一种权力扩展。超管对其他状态的需求已有 E6/E7 |
| Q4 | **在哪类会话里可用** | A. 只在超管本人的私聊（含 App/ACP 个人会话）；B. 私聊和群聊都可以 | **推荐 A**。核验事实：当前会话 `kind='private'`，会话人物与 actor 同人族（与 `work_access`、主动回合“会话人物仍属收件人人物族”同一口径）。【假设】群轮次可能把其他成员的插话接纳进同一轮（实施时用 F6 确认），模型可能被他人的输入引向这次放弃，而授权却按超管计算；私聊里只有超管本人的输入。所有者在私聊就能完成 issue 的全部场景 |
| Q5 | **哪些来源的轮次可以带这项授权** | A. 只有该人物的直接输入（`user_message`、App `client_input`、ACP 个人输入）；B. 也包括该人物创建的定时任务、插件会话 | **推荐 A**。安排先例（E10）对任何人物轮次都核资格，但定时任务是“原创建者过去的指示”，插件会话是插件代发；放弃是不可逆的，应来自本人当前的同意。SELF、主动私聊、`system_task` 本就没有人物 actor，不适用 |
| Q6 | **工作者与委派记录作为目标** | A. 任何 failed Work（根、工作者、委派记录）都用同一条 `manage_work` cancel；B. 只允许根 Work | **推荐 A**。看门狗 `failed_holders` 本就可能列出工作者（带 `parent_work_id`）；admin API 与个人 API 对三者都直接调用 `manage_work`，#671 已验证委派记录 cancel 不发原生调用、父 Work 只收 `worker-result` 不被复活（E12）。三入口语义一致 |
| Q7 | **写进目标 Work 的取消原因** | A. 固定原因码 `superuser_abandoned`，不写模型给的 `text`；B. 沿现有 `manage_task`，用 `text` 作为原因 | **推荐 A**。目标可能属于别人或 SELF，`runtime_work.reason` 会出现在对方的视图和看门狗事实里；自由文本不该写进别人的 Work。谁放弃的由超管自己 Work 的工具回执（原触发事件 + 原 Person）记录；原失败码仍在 `failure_json`（#671 C10） |
| Q8 | **短 ID** | A. 不改，仍要求完整 UUID；B. 支持 8 位前缀 | **推荐 A**（issue 也说不一定要改）。看门狗事实给的是完整 ID；前缀匹配会引入歧义与猜测，放弃又不可逆 |
| Q9 | **普通人物跨代放弃自己的 failed Work**（个人 API 已允许，`manage_task` 不允许） | A. 不在本 issue；B. 一并放宽 | **推荐 A**，另开 issue。issue 要求“不能借此扩大其他操作”；本书只处理超管 |
| Q10 | **生产 operator TOML 缺 `control.work.read`** | A. 由所有者按当前 `deployment_setup` 重新生成或手工补上，不进本 PR；B. 不处理 | **推荐 A**。这是部署配置，不是代码；本任务不改生产配置 |
| Q11 | **工具声明** | A. 不改 `manage_task`/`get_tasks` 的说明与 schema；B. 在说明里加超管能力 | **推荐 A**。合同要求角色不改固定工具声明（E11）；权限在执行处核验。失败时用工具结果文案说明原因（§3.3） |

## 3. 方案（按 §2 决定）

### 3.1 授权核验的事实（Q2）

只在**现有 local 路径找不到目标**、且动作是 `cancel` 时才进入超管路径；现有本人/本会话路径（E2、E4）的行为、错误码和顺序不变。

| # | 核验的事实 | 来源 | 在哪里核 |
|---|---|---|---|
| A1 | 当前轮的原 actor Person 当前是超管：`person_is_superuser(source.actor_person_id)`，即原 Person 与 root 都启用、root `is_superuser`（合并保资格、撤销不复活、账号转移不携带） | `WorkControl.source.actor_person_id`（入口写入，模型改不了） | 与写入**同一个 writer**，在第一次写之前读（单行主键读取，不扫描历史，不等外部） |
| A2 | 当前轮是这个人的直接输入：`principal_kind='person'`、`origin ∈ {user_message, client_input, ACP 个人输入}`、`trigger_event_id` 非空（Q5） | `WorkControl.source` | 进入路径前（纯内存） |
| A3 | 当前会话是该人物的私聊：`kind='private'` 且会话人物与 A1 的 actor 同人族（Q4） | `canonical_conversations` | 进入路径前的 reader，再由 A6 的租约核验保证仍是同一会话同一代 |
| A4 | 当前是 Main 激活：`lease.work_id is None`（工作者仍限于子树，E14） | `WorkControl.lease` | 进入路径前 |
| A5 | 目标此刻是 `failed`；动作只有 cancel（Q3） | `runtime_work.state` | 同一 writer，在 `manage_work` 之前读 state 与 revision |
| A6 | 当前激活租约仍有效（`_assert_lease`），防止 `/new`、取消或换代后旧激活继续写 | `WorkLease` | 同一 writer |
| A7 | revision：本次激活用 `get_tasks(id)` 读到 failed 时，期望值是读到的 revision（沿用 #671 Q8 的 `observed_revisions`）；没读过时用 A5 在 writer 里读到的 revision | `observed_revisions` / writer | `manage_work` 的 CAS |

**不核验**（这正是要放宽的条件）：目标的 `actor_person_id`、`principal_kind`、`initiative_run_id`、`conversation_id`、`generation` 和父子关系。目标的归属与原来源保持不变（E11），只改变它的状态。

理由：
- A1 对应合同“当前授权保原 actor Person，核原人物与当前 root 的有效性和当前资格”，与 E6/E7、E10 的核验一致；放在 writer 里，资格在读与写之间被撤销也不会生效。
- A2/A3/A4 对应“当前授权核原来源”：授权来自超管本人此刻在自己私聊里的输入，不来自 SELF、定时、插件、工作者或群里其他人。
- A5 让超管路径只做“放弃恢复”。因为写入前在同一 writer 里核实 failed，#671 Q8 的剩余缺口（没先查看就取消已被恢复的 Work）在这条路径上不存在：目标已被恢复就不再是 failed，直接拒绝。
- A7 不新增机制，复用现有 CAS。

### 3.2 写入（与 admin API、个人 API 同一条 `manage_work`）

```
reader（进入前）: A2、A3、A4；目标存在（全局读，不要求 local）
writer（一个 BEGIN IMMEDIATE）:
  _assert_lease(lease)                     # A6
  person_is_superuser(actor_person_id)     # A1
  SELECT state, revision FROM runtime_work WHERE id = target   # A5
  manage_work(writer, target, observed 或 revision, "cancel", reason="superuser_abandoned")  # A7、Q7
```

- 后续效果都沿 E12：非终态后代被取消（failed 下通常没有，#671 C8）；终态后代不改写（#671 Q5）；有父 Work 时写 `worker-result`，不复活挂起或终态父 Work；触发器同事务释放 refs，GC 在 24 h 宽限期后回收。
- 委派记录（Q6）也走这里，不经 `manage_external`，不调用原生服务。
- 成功后清除 `observed_revisions[target]`（同 #671）；失败保留，未重新读取就重试仍会冲突。
- 记录：结果 `{id, status: cancelled}` 作为该次工具调用的结果，留在超管本轮的请求轨迹里（与原触发事件同轮）；目标 Work 只写原因码，不记录超管 ID（Q7）。【实证，实施时更正】只由 Work 控制工具组成的批次不触发私聊 Host 接纳（`main-agent-runtime.md:555-556`），所以这类轮次没有超管自己的 Work 效果回执；本次不为此新增回执机制。
- 不需要迁移，不需要新索引（目标按主键读取）。

### 3.3 错误与文案（工具结果，不是工具声明）

| 情况 | 结果 |
|---|---|
| 不是超管 / 资格已撤销 / Person 停用 | 沿现有 `work_not_found_or_not_authorized`（与今天相同，不新增区分；全局目录本就能看到存在性） |
| 是超管，但不满足 A2/A3/A4 | 新码 `superuser_abandon_private_only`：“替别人或 SELF 放弃 failed 任务只能在你本人的私聊里由你直接要求；也可以用 `/ai admin work cancel <id>`。” |
| 是超管，目标不是 failed | 新码 `superuser_abandon_failed_only`：“只能放弃已失败的任务；它当前是 {state}，先用 get_tasks 查看。” |
| 动作不是 cancel（resume/fail/release） | 沿现有错误，不进入超管路径 |
| revision 不符 | 沿现有 `version_conflict` 文案 |

实施保留这两个错误码：它们只对**已核实为当前超管**的人返回，说明拒绝的真实原因（来源/会话不对，或目标状态不对）；非超管一律得到今天的原错误，不泄露这条路径。

### 3.4 文档（与代码同一 PR）

- `docs/architecture/main-agent-runtime.md:648-664`：补“当前超管在本人私聊的直接输入中，可以对任何 failed Work 执行 `manage_task cancel`（放弃恢复）；核验 A1–A7；不开放其他动作；归属与原来源不变”。
- `docs/architecture/development-contract.md:33`：在超管共享管理资格的列表里加入“放弃 failed Work”。
- `docs/architecture/persistent-subagents.md`：工作者与委派记录可被超管放弃，语义同 admin API。
- `docs/architecture/README.md`：本书索引；顺手更正第 3 行 #671 的状态（已合并）。

### 3.5 Desktop（只列出，不实现；Q1 选 C/D 时另开 Desktop issue）

- 任务页对 `me.is_superuser === true` 的设备增加“全部任务”视图，读 `GET /admin/v1/works?state=failed`，详情读 `GET /admin/v1/works/{id}` 的 `actions.cancel`，放弃调用 `POST /admin/v1/works/{id}/cancel`（带 `expected_revision`，409 时刷新）。服务端、OpenAPI、Client API revision 都不变（E7、E9）。
- 文案沿 #671 §3.6 的“放弃恢复”。

## 4. 失败情况（先写，再写代码）

| # | 失败情况 |
|---|---|
| F1 | 超管在本人私聊里放弃群 SELF failed Work（旧代）仍被拒绝（现状） |
| F2 | 超管放弃本人私聊里的旧代 failed Work 仍被拒绝（现状） |
| F3 | 非超管放弃别人的 failed Work 成功；或错误码与今天不同 |
| F4 | 资格在 reader 核验后、writer 前被撤销，放弃仍然生效；合并后的原 Person（root 为超管）被拒绝；账号已转给别人后，新持有人借账号获得资格；Person 停用仍放行 |
| F5 | 超管在群轮次、定时任务轮次、插件轮次、SELF 轮次或工作者激活中放弃了别人的 Work |
| F6 | 群轮次里其他成员的插话被接纳进同一轮，触发了超管路径 |
| F7 | 目标不是 failed（queued/running/waiting/suspended/completed/cancelled）却被取消；或在 reader 读到 failed 后被别处 resume，writer 仍取消（应 `superuser_abandon_failed_only` 或 `version_conflict`） |
| F8 | 超管路径放行了 resume/fail/release；或对别人的 failed Work 执行 resume 以原来源继续执行 |
| F9 | `get_tasks(id)` 读到 failed（r）后被别处推进到 r+1 仍 failed，放弃仍按 r+1 成功（应 `version_conflict`）；冲突后不重新读取的重试成功 |
| F10 | 并发：同一 revision 下所有者 resume 与超管放弃都成功，或输家不是 `version_conflict` |
| F11 | 委派记录放弃时向原生服务发出任何调用或登记新操作；工作者放弃后父 Work 没收到 `worker-result`，或挂起/终态父 Work 被复活 |
| F12 | refs 没释放；误释放其他 Work 的 refs；共享对象被 GC 删除；宽限期内被删 |
| F13 | 目标 Work 的 `actor_person_id`、`source_json`、`conversation_id`、`generation`、`failure_json`、journal、effects 被改写；`reason` 出现模型的自由文本 |
| F14 | 现有本人/本会话路径的行为或错误码变化（回归：#671 S2/S6/S7 场景照旧通过） |
| F15 | 工具声明或 manifest 变化 |
| F16 | 旧激活（`/new` 或会话换代之后）仍能写入 |
| F17 | 把超管 ID 或模型的 `text` 写进目标 Work（并入 F13 验证） |

## 5. E2E 与可复现产物

文件：`tests/integration/test_superuser_abandon_failed_work_e2e.py`（文件头先写失败清单与 A1–A7，再写场景）。复用看门狗 E2E 的 Scene：真实 SQLite、真实 `ProtocolStore` 文件与 GC、真实看门狗、真实 Main 私聊与群轮次、真实定时发生（`AutomationWorker`）；只有 provider 是 fake。SELF Work 按 `accept_in_session` 写入的来源形状直接插入（没有驱动一次真实 initiative run 失败），群换代用真实 `bump_canonical_generation`。产物 `test-results/superuser-abandon-failed-work-e2e.json`，只含标签和关系事实，末尾 `results_sha256`。

- **S1 生产形态**：群 SELF failed Work 挂上协议对象 → 群 `/ai new` 进入旧代 → 新一代里一个活 Work 共享其中一个对象 → 存储越过告警线，看门狗 facts 列出它（`origin=self_initiative`）→ 所有者私聊：`get_tasks(id)` + `manage_task(cancel, id, text=…)` → cancelled，revision +1，`reason=superuser_abandoned`（`text` 未写入），来源、会话、generation、父、goal 不变，`failure_json` 保留，refs 释放、活 Work 保留共享 ref → 宽限期内 GC 不删，宽限期外删除，usage 下降量等于无主字节，共享对象保留。（F1、F12、F13、F17）
- **S2 本人旧代、工作者、别人的委派记录**：本人私聊旧代 failed Work；挂起的 SELF 父 Work 下的 failed 工作者（父收到 1 条 `worker-result`，仍是 suspended）；另一人旧代群里的 failed 委派记录（替身原生服务零调用、无新操作，`actor_person_id` 不变）。三者都是 cancelled + `superuser_abandoned` + refs 清空。（F2、F11）
- **S3 拒绝且不写入**：非超管（Work 得 `work_not_found_or_not_authorized`，记录得 `subtask_not_owned`）；超管群轮次（`superuser_abandon_private_only`）；超管路径的 resume/fail/release（原错误）；资格在 reader 核验后、writer 前被撤销（`work_not_found_or_not_authorized`）；超管本人在私聊里的定时发生（`superuser_abandon_private_only`）。每一步前后比较目标的 state/revision/reason/source/会话/generation/refs 与全部 mailbox 行数。（F3、F4、F5、F8、F16）
- **S4 只放 failed、只认读到的 revision**：queued/running/suspended/completed/cancelled 五种目标都得 `superuser_abandon_failed_only` 且不写入；读到 failed 后被别处 resume（queued）→ `superuser_abandon_failed_only`，保持 queued；读到 failed 后被推进但仍 failed → `version_conflict`，不重读重试仍 `version_conflict`；未读过、resume 落在 reader 与 writer 之间 → `superuser_abandon_failed_only`；重新读取后放弃成功。（F7、F9）

**计划中但没有做成 E2E 的项**（如实列出）：合并到超管 root 的原 Person、账号转移后的新持有人、停用 Person（由 `person_is_superuser` 的现有语义保证，本 E2E 只覆盖“撤销”）；插件轮次与工作者激活（由 A2 的 `origin` 与 A4 的 `lease.work_id` 判断，未单独驱动）；`asyncio.gather` 的真并发（改用 reader/writer 之间与两次调用之间的确定性插入）；`/new` 之后旧激活的写入（沿用现有 `_assert_lease`，未单独构造）；群里他人插话被接纳（群轮次已整体拒绝）。工具声明与 manifest 未改动（`work_control_tools()` 不在 diff 中）。

可复现验收：

```bash
uv run pytest tests/integration/test_superuser_abandon_failed_work_e2e.py -q
cp test-results/superuser-abandon-failed-work-e2e.json /tmp/run1.json
uv run pytest tests/integration/test_superuser_abandon_failed_work_e2e.py -q
cmp /tmp/run1.json test-results/superuser-abandon-failed-work-e2e.json
```

## 6. 不做

超管路径的 resume、fail、release 或取消运行中 Work；改目标 Work 的归属或来源；普通人物跨代放弃自己的 Work（Q9，另开）；短 ID（Q8）；工具声明变更；迁移、回填或批量清理生产数据；修改生产 operator TOML（Q10，所有者自行处理）；Desktop 实现（§3.5）；看门狗获得管理权；合并、部署、重启。

## 7. 实施顺序

1. 更新本书为决定已定；写 E2E 文件头失败清单与全部场景，在旧代码上确认失败。
2. 改 `runtime/work_control.py`：`_manage` 在 local 路径找不到目标（普通 Work 的 `work_not_found_or_not_authorized`、委派记录的 `subtask_not_owned`）且动作为 cancel 时进入 `_superuser_abandon`；不改 `manage_work`、触发器、GC、schema、工具声明。
3. 改现行文档与 CI 产物检查。
4. 跑验证；产物两遍逐字节比对；普通提交（不加署名）；推送原分支，PR #674 保持 draft 等 CI 与用户核验。不合并、不部署。

## 8. 记录

### 8.1 第一阶段（调研）

- 只读调研：#673、#666、#671 正文；#671 任务书；`work_queries`、`work_control`、`work_management`、`work_repository`（SELF 接纳）、`external_agents/subtasks`、`client_api/works`、`admin/*`、`services/command*`、`identity/authority`、`automation/service`；现行文档 `development-contract`、`main-agent-runtime`、`admin-api`、`client-api`、`control-plane-foundation`；Yuki-Desktop `92cc412` 的 admin 客户端与任务页。
- 未访问生产；§1.1 与 E8 的生产事实来自 #673。

### 8.2 第二阶段（实施）

- **改动范围**：`runtime/work_control.py`（`_superuser_abandon` 与两处进入点、两条错误文案）；新增 E2E `tests/integration/test_superuser_abandon_failed_work_e2e.py`；CI `quality.yml` integration 套件增加产物存在检查；文档 `main-agent-runtime.md`、`development-contract.md`、`persistent-subagents.md`、`README.md`（本书索引；顺带把 #671 索引状态改为已合并）。无迁移，无工具声明变更，无配置变更，无 Desktop 改动。
- **先红后绿**：E2E 先于实现写好。在 `db7b5c26` 的 `work_control.py` 上 4 个场景都在预期位置失败：S1 放弃群 SELF 旧代 Work 返回 `work_not_found_or_not_authorized`（F1）；S2 本人旧代 Work 同样被拒（F2）；S3 超管群轮次得到原错误而不是 `superuser_abandon_private_only`；S4 非 failed 目标得到原错误而不是 `superuser_abandon_failed_only`。S3 中非超管一段在旧代码上已通过（原错误不变，符合预期）。实现后 4 passed。中途修正过三处测试搭建（不是实现问题）：`/ai new` 会取消旧代的非终态 Work，所以共享对象的活 Work 与挂起的父 Work 改放到新一代；场景设置默认关闭自动化，定时场景用 `automation_enabled=True` 的设置副本；revision 断言改为与读到的值比较。
- **变异核对**（每次改完都还原，`cmp` 确认）：去掉 writer 内的超管复核 → S3 撤销一段失败；去掉 failed 判断 → S4 失败；去掉私聊/直接输入判断 → S3 群轮次失败；忽略读到的 revision → S4 读后推进一段失败；不写固定原因码 → S1、S2 失败。
- **产物**：`test-results/superuser-abandon-failed-work-e2e.json`（目录被 gitignore，与其他 E2E 一致），连续两次运行逐字节相同，`results_sha256 = ede0da9c374361563d84404c6b16d9017cacb7a299d473913ce99b5588dd5421`。
- **其余验证**：`ruff check .`、`ruff format --check`（改动文件）、`mypy --platform linux src` 通过；#671 E2E、看门狗 E2E 与 6 个相关单测文件 188 passed；引用 `manage_task`/`WorkControl` 的其余 96 个测试文件 1199 passed、3 skipped、1 failed。失败的是 `test_work_execution_receipt_regressions.py::test_manager_restores_dispatched_run_without_replaying_request[False-terminal_exec]`，换回基线 `work_control.py` 同样失败（#671 已记录的本机 macOS 沙箱问题），以 CI（Linux）为准。
- **合并与上线**：未合并、未部署；以 PR 与 CI 为准，本书不提前写成已合并。
