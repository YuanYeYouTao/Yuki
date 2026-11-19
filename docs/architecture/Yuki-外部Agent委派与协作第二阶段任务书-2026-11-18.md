# Yuki 外部 Agent 委派与协作第二阶段任务书（2026-11-18，用户已定 Q1–Q6）

**状态**：用户已决定合成一个大 PR 实施（§8）；实施、验证、复审与发布边界见 §16。**没有合并或上线。**第一阶段（防卡）见[第一阶段任务书](Yuki-外部Agent操作事件化与防卡改造任务书-2026-11-18.md)，其复审原文见[Codex 复审](Codex-事件化任务书独立复审原文.md)。

**基线**
- 后端 `origin/main` 为 `ad59a686`（含第一阶段 #637 `cf4dc201` 与库体积 #638）。下文 `文件:行` 均指这个提交。
- 迁移 head `0158`，工具合同 39（`src/yuki/services/main_agent_contract.py:122-124`），Client API revision 13，设备协议 4，连接器 `0.8.0`（`packages/yuki-agent-connector/pyproject.toml:3`）。
- Yuki-Desktop main `66c6ead`（#85 已合入），合同固定点 `ad59a686`、apiRevision 13，`REQUIRED_API_REVISION = 11`（`src/shared/api.ts:8`）。
- 本机 Claude Code 2.1.311、Codex CLI 0.169（daemon 0.170）、`claude-agent-sdk 0.2.172`。
- DeepSeek 原型 `deepseek-ai/dsh-superagent-prototype` 提交 `8c32a9dc`（2026-10-31，单次提交），读了 docs 000、001、002、003、004、007。

**标注约定**（沿用第一阶段）

| 标注 | 含义 |
|---|---|
| 【事实】 | 有运行记录或实验为证 |
| 【代码事实】 | 读源码可以确定，未在运行中观察 |
| 【协调者实验】 | 协调者 2026-11-18 派独立 agent 在隔离环境做的实验（Claude Code 2.1.311、Codex 0.169），本书未复现 |
| 【推断】 | 由代码或文档推出，未经实测 |
| 【待验证】 | 需要 §9 的实验确认 |

---

## 0. 用户目标与已定决定

### 0.1 目标（协调者转述，非原话）

Yuki 在多个会话、多个外部 Agent、多台设备之间切换，同时推进好几件事。外部 Agent（Claude Code、Codex、其他 ACP Agent）是 Yuki 的子 agent，Yuki 是路由者。**不是"Yuki 再派一个自己的 Yuki 子 agent 去看着 Claude"**：外部 Agent 本身就是子 agent。

### 0.2 已定决定（用户决定，2026-11-18，经协调者转达）

| 编号 | 决定 | 本书落点 |
|---|---|---|
| D1 | **一套工具。** 删除 `use_coding_agent`／`get_coding_agents`，并入 `start_subtask`／`message_task`／`manage_task`／`wait`／`get_tasks`，用执行者参数区分 Yuki 自己与某台设备上的 Claude／Codex／ACP。删除优先，两套不共存。会话浏览（列会话、读历史）保留为查看的一部分。提示、文档、测试、CI 一起改。部署时工具合同只变一次（一次缓存失效），之后冻结 | §4.2、§7.4、§8 |
| D2 | **委派时固定权限范围**（如目录、跑测试、git）。范围内不弹确认；范围外先到 Yuki，再到用户；用户可在 Desktop「等你回答」入口批准或回答。按执行者映射：Claude 用 permission mode + allowed tools；Codex 用 approval policy + sandbox mode；ACP 没有标准范围，由连接器按委派范围自动批准或上报 `session/request_permission` | §4.4 |
| D3 | **不设硬并发上限**（否决每设备 3 个的提议）。理由：卡住的委派会永远占着名额；上限是 AGENTS.md 禁止的经验阈值。改为：卡片上的事实（每台设备在跑什么、是否卡住）、用户在 Desktop 取消、提示引导（不是门槛）、原生"每会话同时一个回合"作为事实 | §4.12 |
| D4 | **前缀缓存不能被破坏。** 生产 chat_agent 命中率平时 86–97%，compact 抖动时降到 77%。硬规则：system、tools、compact 检查点、历史跨请求逐字节稳定；动态状态只进末尾 `<runtime_context>` 或工具结果；工具按部署冻结；只追加；结果有界以少触发 compact；顺序和键序确定。验收用真实请求前缀字节的事实测试（委派、完成、唤醒）；命中率只在前后记录，不当门槛 | §5 |
| D5 | **按协议能力，不按厂商。** Yuki 看到"执行者 + 声明的能力"（list／get／watch／prompt／steer／queue／respond／cancel／detach），工具和提示里没有 Claude／Codex 特例 | §4.2、§4.14 |
| D6 | **三种关系不混用。** observe 只看：绝不发送输入，绝不回答或拒绝权限与提问，只通知用户（"你那个 Claude 在等你批准 xxx"）；不阻止 Work 结束；是显式的单独动作，不是启动子任务的副作用。delegate：Yuki 拥有，在委派范围内执行。takeover：只有用户明确说时才做。回归验收：用户在自己终端各开一个 Claude 和 Codex 会话，Yuki 观察；出错或弹权限时 Yuki 通知用户，不注入输入，权限留给用户。要核实并写明第一阶段"无主权限只能由模型 deny"能否触及被观察的会话；第二阶段必须让被观察的会话永远不可回答、不可拒绝 | §2 G1、§4.3 |
| D7 | **实时共享会话。** 用户想要"终端继续跑，Yuki 也来帮忙"。只用原生机制，**不自建共享会话系统**；没有原生机制时提供 observe + 交接。另含交接流程（用户停下，Yuki 按 ID 带完整历史接手，用户可再接回） | §4.11 |
| D8 | **Codex 与 ACP 同等对待。** 第一阶段连接器侧的修复（无主权限 deny-only、queue、回放核对）只做了 Claude；服务器侧非阻塞派发是通用的。第二阶段对 Codex、ACP 做同样的实验（无人等待时能否开回合、能否发权限），能就同样处理。E2E 至少覆盖 Claude、Codex 和一个真实 ACP Agent（如 ACP 模式的 Gemini CLI） | §4.8、§9、§10 |
| D9 | **站点特定功能留在插件，不进核心** | 本书不新增站点功能 |

D7 的实验事实（【协调者实验】，Claude Code 2.1.311、Codex CLI 0.169／daemon 0.170）：

- **Codex 支持实时共享。** 默认 TUI 跑在共享的 app-server daemon 上（`--managed-daemon`，socket 在 `~/.codex/app-server-control/app-server-control.sock`）。第二个客户端经 `codex app-server proxy` + `thread/resume` 共享同一活线程，双方实时看到对方的回合。`codex queue --thread <id> --message` 也可用。独立的第二个写入者被拒绝（"thread-store conflict: already has an active writer"），所以不会分叉。例外：`--no-daemon` 启动的 TUI 只能交接。连接器的 codex 驱动本来就经 app-server proxy 连接（`drivers/codex.py:91-124`【代码事实】），所以主要是端到端验证和产品接线。
- **Claude Code 不支持。** 从另一个进程恢复正在运行的会话（SDK `resume=` 或 `claude -p --resume`）会静默地往同一个 JSONL 追加一条 parentUuid 分叉的分支，双方都看不到对方；官方文档 https://code.claude.com/docs/en/sessions.md 证实。**这是 Yuki 当前就存在的隐患**（§2 G8）。官方只读命令 `claude agents --json` 列出活会话（pid／sessionId／status）。最接近原生"在旁边帮忙"的是跨会话消息（ListAgents／SendMessage，走本机 UDS，2.1.224 起默认开启，https://code.claude.com/docs/en/cross-session-messaging.md）：已验证 headless／SDK 进程可以向活的终端会话 SendMessage 并收到回复；限制是只有文本、接收方把它当队友而不是用户、接收方的权限弹窗不变、`crossSessionInbound` 有 accept／hold／refuse、没有给第三方的公开原始 socket 协议。Remote Control 没有第三方 API；Channels 要在启动时加参数。
- **ACP 只能交接。** 一个 Agent 进程只有一个客户端；`session/load`／`session/resume` 用于重连或交接。

### 0.3 本书结论

1. **方向成立，但要换一个关键说法。** 协调者的分析写"每个委派是自己的 Work，外部进展只进这个 Work，它睡到被唤醒"。如果这个 Work 有 Yuki 自己的模型循环，它就是用户否决的"Yuki 看着 Claude"。本书建议：**委派是一条没有 Yuki 模型循环的子 Work 记录**，状态由原生事实确定性地结算；需要 Yuki 判断时只唤醒**发起它的父 Work**（§4.1）。这样复用了 Work 的父子、等待、取消、ACL、Desktop 列表和提示，而不多一个"看护者"。
2. **今天的 watch 不是 observe。** 观察关系在代码里就是 `watch`，而 watch 收到的权限事件可以被完整回答（Codex 连 allow 都可以），Desktop 也会给出回答按钮（§2 G1）。第一阶段的"无主权限只能 deny"触及不到用户终端里的 Claude 会话，但这不是因为有保护，而是因为连接器根本收不到终端会话的回调（§2 G2）。
3. **今天普通进展会唤醒 Work，而且全文进上下文。** 每条带 run 的外部事件都会唤醒正在 `wait(run)` 的 Work（`repository.py:1161` → `work_wait.py:410-415`），事件 JSON 原样追加为一条用户消息、没有逐条上界（`work_control.py:656-690`）。这就是"聊天 Work 里轮询 Claude 导致上下文暴涨"的代码根源（§2 G4）。
4. **不需要新的事件总线、新调度器或并发上限。** 需要：一处迁移（§6）、一次工具合同变更（§4.2）、连接器 0.9.0（§7.2）、Client API 14（§7.3），以及一次设备协议 5 的硬切（§4.10，Q4 已定）。

---

## 1. 第一阶段之后的现状

### 1.1 已交付（第一阶段 §21，已上线）

| 能力 | 位置 | 性质 |
|---|---|---|
| 改动类动作只在激活里登记，写出、回复与结算由服务的后台任务承接 | `src/yuki/external_agents/service.py:857-907`、`:204-275` | 代码事实 |
| 重连后按原 ID 读回，不重发；not_found 只是本连接的诊断 | `service.py:106-130, 277-321`；`repository.py:916-967` | 代码事实 |
| 显式 detach 可在离线或设备无记录时只解除 Work 关系 | `service.py:707-718, 911-950` | 代码事实 |
| 控制命令结算后以私有输入告知发起它的 Work | `repository.py:969-1036` | 代码事实 |
| Claude：按回放 UUID 接纳；自发回合让会话变忙；queue；无人接收的等待只传元信息、只能 deny | `drivers/claude.py:213-293, 440-547, 597-647, 731-765` | 代码事实 |
| 会话级 `get` 返回 `pending_unowned_requests`；会话目标的 answer 只接受 deny | `service.py:571-574, 694-817`；`repository.py:188-238` | 代码事实 |
| 工作步骤 `kind`／`tool`／`action`／`detail`（Client API 13） | `src/yuki/client_api/work_projection.py:43-135` | 代码事实 |
| 前缀字节 E2E | `tests/integration/test_external_agent_prompt_prefix_e2e.py` | 事实（第一阶段 §21.8） |

### 1.2 工具与 Work 现状

| 项 | 现状 | 位置 | 性质 |
|---|---|---|---|
| 两套工具 | 子任务五件套（`work_control.py:48-50, 211-330`；`runtime/subagent_tools.py:13-55`）与编码 Agent 两件套（`external_agents/tools.py:10, 24-100`）并存 | 同左 | 代码事实 |
| 子任务就是子 Work | `start_subtask` 返回子 Work ID，`wait(run=id)` 可等它，`manage_task(cancel/resume, id)` 管它（`subagent_tools.py:58-118`；`work_control.py:960-1000`）；父子关系在 `runtime_work.parent_work_id`，执行者资料在 `runtime_subagents`（`runtime/subagent_schema.py:8-21`），与开发约束 §5"工作者表仅保存执行者资料"一致 | 同左；`development-contract.md` §5 | 代码事实 |
| 委派外部 Agent 要先有 Work | prompt／queue／watch 要求当前 Work（`service.py:629-640`）；`start_subtask` 同样要求（`subagent_tools.py:59-60`） | 同左 | 代码事实 |
| watch 不阻止 Work 完成 | 进度里 `ownership=observe` 的 run 不计入所属执行（`runtime/work_repository.py:557-571`；`repository.py:506`） | 同左 | 代码事实 |
| 每轮运行资料 | 激活开始时 `runtime_state()` 放进 `<runtime_context>`，没有当前 Work 时带 `available_work`（上限 `PROMPT_AVAILABLE_LIMIT = 16`）与 `recent_work`（`work_control.py:1340-1363`；`runtime/work_queries.py:31, 256-271`；`services/turn_execution.py:837-846`）；之后的参与资料只追加（`turn_execution.py:855-860`） | 同左 | 代码事实 |
| 控制结果走统一结果预算 | #636 之后 `get_tasks` 步骤不再内嵌只读控制结果（`git show 0aa5f390`） | 提交说明 | 事实 |
| 模型准入全局并发 | `global_llm_concurrency: int = 4`（`src/yuki/config.py:157` → `application/modules/model_runtime.py:85`） | 同左 | 代码事实 |
| 子任务调度并发 | `SubagentScheduler(max_concurrency=1)` 默认值（`runtime/subagent_scheduler.py:38-46`），实际装配值未核 | 同左 | 代码事实（装配值【待核】） |

### 1.3 驱动现状

| 项 | Claude | Codex | ACP |
|---|---|---|---|
| 会话所有者 | 连接器自己的 SDK 进程（`claude.py:310-359`）；独立终端只读，watch 返回 `not_live`（`claude.py:674-677`） | 用户的 app-server daemon，连接器经官方 proxy 接入（`codex.py:91-124`） | 连接器自己启动的 ACP 进程（`acp.py:90-120`） |
| 列会话 | SDK 只读发现，`status` 一律 `unknown`、`live_attach: false`（`claude_inspect.py:41-73`） | `thread/list`（`codex.py:817-855`） | 协商出 `list` 时才有（`acp.py:117-119`） |
| 恢复已有会话 | `resume=True` 直接 `_open_once`，**不检查会话是否在别处活着**（`claude.py:597-611`） | `thread/resume`（`codex.py:1016`） | `resume_session` 或 `load_session`（`acp.py:458-467`） |
| watch 收到权限请求 | `_publish` 发给 owner 和所有匹配的 watch（`claude.py:164-198`） | 所有匹配的 watch 都收到 server request（`codex.py:680-724`） | `_publish` 发给 owner 和 watch（`acp.py:241-270`） |
| respond 是否区分 watch | 只核原 SDK owner、请求仍挂起、unowned 只 deny（`claude.py:731-765`） | 只核连接、请求存在、线程与回合（`codex.py:1153-1185`），**不区分 watch，allow 也行** | 同 Claude 形态，不区分 |
| 无人接收的权限 | 只传元信息、只能 deny（`claude.py:258-260`） | 无人观察的会话不上传（`codex.py:697-713`），请求停在本地（无人应答时 app-server 是否等这个客户端【待验证 E1】） | `request_permission` 不看接收者是否为空，future 一直等（`acp.py:295-330`） |
| 自发回合 | 标为 `self-started:{uuid}`，operation 为空（`claude.py:493-503`） | 无人观察时丢弃 | session_update 在无 operation 时无接收者 |
| 权限范围 | `ClaudeAgentOptions` 只给 `can_use_tool`，`setting_sources=["user","project","local"]`，没有 `permission_mode`／`allowed_tools`／`add_dirs`（`claude.py:337-349`）；SDK 0.2.172 有这些字段（本机安装的 `claude_agent_sdk/types.py`） | `thread/start`／`turn/start` 只传 cwd、input（`codex.py:1018-1034`）；0.169 schema 的 `ThreadStartParams`／`TurnStartParams`／`ThreadResumeParams` 都有 `approvalPolicy` 与 `sandbox`／`sandboxPolicy`，`WorkspaceWriteSandboxPolicy` 有 `writableRoots`、`networkAccess`（本机 `codex app-server generate-json-schema` 生成） | 连接器实现的客户端回调 `read_text_file`／`write_text_file`／`create_terminal` **不核任何范围**（`acp.py:586-640`） |
| 原生事件进 Work | 每条 assistant 文本和工具调用都是一条 progress（`claude.py:534-547`） | `item/completed`、`turn/started`、`turn/plan/updated` 等都是 progress（`codex.py:740-779`） | `tool_call`／`plan`／`usage_update` 都是 progress（`acp.py:280-293`） |

### 1.4 Desktop 现状（Yuki-Desktop `66c6ead`，只读）

- 工作步骤按服务端 `kind` 显示（`src/renderer/views/Works.tsx:76-122`），但 `detail.operation_id` 从不渲染，没有从步骤跳到外部 Agent 操作的链接；步骤上没有外部 Agent 实时状态。
- Works 列表有「需要你」分区（`state === 'waiting_user' || help_requested`，`Works.tsx:218, 275-280`），只针对 Yuki 自己 Work 的提问；外部 Agent 的权限或提问不在这里。
- 「这台电脑」只看本机（`ConnectorTasks.tsx:29-58`），没有运行中／卡住的筛选，没有多设备视图（`connections.ts:1404-1425` 只保留本机条目）。
- 那里的 prompt／watch／queue／steer／detach／cancel／回答**都不直接调 `/agents/v1`**，而是发一条带 `agent_target` 的聊天消息（`app.ts:611-621`；`ConnectorTasks.tsx:137-184`）。服务端把它渲染成 `use_coding_agent` 的参数名写进正文（`src/yuki/client_api/agent_target.py:35-65`）。现行合同明确"不由个人 HTTP 另建执行驱动"（`docs/architecture/client-api.md:174`）。
- `NativeRequest.tsx` 对所选操作的任何 permission／question 事件都给回答按钮，不区分 watch（`ConnectorTasks.tsx:471-478`）；`pending_unowned_requests` 在 Desktop 中不存在。
- 变化通知走 SSE 提示 `work`／`work_steps` 等（`Works.tsx:381-409`），没有外部 Agent 操作专用提示，没有定时轮询。
- 随附连接器版本不写在受跟踪文件里，打包时按合同固定点构建（`build-tools/prepare-connector.mjs`、`build-tools/connector-signing.mjs:9-22`）；更新需要用户点击（`AgentConnector.tsx:359-381`）。

### 1.5 第一阶段的事故证据（用于说明为什么要分层）

- **get_tasks 滚雪球**（#636）：`get_tasks(id=自身)` 逐次嵌入上一份只读控制结果，单项约 97.5 万估算 token，Work 因 `model_request_capacity` 挂起（`git show 0aa5f390` 提交说明）【事实】。
- **聊天 Work 里轮询 Claude**：事故 Work `d8cc09a4` 共 71 步，其中 `wait` 18、`use_coding_agent` 10、`get_coding_agents` 5（第一阶段 §17.3）【事实】。§2 G4 的代码路径说明每条进展都会唤醒并全文进入上下文【代码事实】，两者吻合；逐步对应未核【推断】。
- **两天三次卡住**：协调者转述，本书未逐一核对。

---

## 2. 缺口

| 编号 | 缺口 | 证据 | 性质 |
|---|---|---|---|
| G1 | **watch 可以回答权限。** 三个驱动都把权限请求发给 watch（§1.3）；服务端只核"事件属于这个操作"（`service.py:818-832`），watch 操作就是一个操作；Codex 的 respond 连 allow 都接受（`codex.py:1153-1185`）；Work 输入带 `permission_id`（`repository.py:869-889`），提示模型去答；Desktop 也给按钮。D6 要求的"observe 绝不回答"今天不成立 | 同左 | 代码事实 |
| G2 | **第一阶段的 deny-only 触及不到用户终端里的 Claude。** 它只作用于连接器自己 SDK owner 的回调（`claude.py:231-293`）；终端会话的权限弹在终端里，连接器收不到（watch 直接 `not_live`，`claude.py:674-677`）。但 Yuki 自己 resume 过的会话（含 G8 的静默分叉）、被 detach 的委派，其等待会变成无主，模型可以 deny。Codex 与 ACP 没有 deny-only，watch 就能答（G1） | 同左 | 代码事实 |
| G3 | **终端里的 Claude 根本无法 observe。** 没有 SDK owner 就不能 watch；列会话的状态一律 unknown（`claude_inspect.py:52-53`）。要通知"你那个 Claude 在等你批准"，需要一个只读的原生事实来源【待验证 E3】 | 同左 | 代码事实 |
| G4 | **普通进展会唤醒并全文进入上下文。** 事件只要操作仍 pending 就写 Work 输入（`repository.py:869-890`），`_input` 对带 run 的操作调用 `signal_external_action`（`:1161`），有等待绑定且条件里有这个 run 就 `_deliver(..., "requires_action")`（`work_wait.py:410-415`），即唤醒。注册等待时还会对所有待处理的外部 run 输入补发一次（`work_wait.py:296-318`）。输入按 8192 字节分批，但"一条超大输入保持完整"（`work_control.py:680-686`），每条以 `[Work 信号 …]` 加完整 JSON 追加 | 同左 | 代码事实 |
| G5 | **只等消息的根 Work 收不到权限。** `waiting_user` 时 `signal_external_action` 直接返回（`work_wait.py:404-409`）；根 Work 只 `wait(message)` 时映射为 `need_input`（`work_control.py:1132-1136`） | 同左 | 代码事实（第一阶段 L8 的遗留） |
| G6 | **自发回合没有归属。** Claude 的 ScheduleWakeup、后台任务通知开启的回合 operation 为空（`claude.py:493-503`），其进展无人接收，权限变成无主（第一阶段 §7.3 未做） | 同左 | 代码事实 |
| G7 | **执行者消失只体现为进展。** SDK 进程退出发 `native_sdk_connection_lost`（`claude.py:375-436`），ACP 进程退出发 `native_acp_process_exited`（`acp.py:126-140`），Codex daemon 断开发 `connector/ownerDisconnected`（`codex.py:221-232, 640-672`），设备离线写 `device_offline` 输入（`repository.py:1038-1091`）。它们和普通进展走同一路径（G4），没有"执行者已不在"的委派状态，也不保证告诉用户 | 同左 | 代码事实 |
| G8 | **恢复会静默分叉用户的活 Claude 会话。** `resume` 不查会话是否在别处运行（`claude.py:597-611`）；Claude Code 对两进程同时写同一会话不报错（【协调者实验】＋官方文档） | 同左 | 代码事实＋协调者实验 |
| G9 | **释放后 Yuki 的 Claude 进程还占着会话。** detach 只清 watch 与 operation（`claude.py:650-673`），SDK 进程留着；用户此时在终端 `--resume` 就是 G8 的分叉。交接回用户需要 Yuki 先放手 | 同左 | 代码事实＋推断 |
| G10 | **权限范围不存在。** 三个驱动都没有把范围传给原生（§1.3）；ACP 的文件与终端回调对任意路径和命令都执行（`acp.py:586-640`）；Claude 读取用户的 user／project／local 设置（`claude.py:343`），用户自己的 allow 规则会不会先于委派范围生效【待验证 E6】 | 同左 | 代码事实 |
| G11 | **Codex 与 ACP 的无主等待没有处理。** Codex 对无人观察的会话不上传、请求留在 `self.requests` 不应答（`codex.py:697-718`）；ACP 的 `request_permission` 在接收者为空时也一直等（`acp.py:295-330`）。是否会卡住原生取决于原生是否只等这个客户端【待验证 E1、E5】 | 同左 | 代码事实＋待验证 |
| G12 | **没有委派级的状态与时间线。** 现在的单位是"操作"，一次委派会产生 prompt、queue、answer、cancel 等多个操作；没有把它们串起来的委派 ID，也没有"派出、接纳、进展、提问、谁回答了、完成"的时间线 | `db_models.py:47-94` | 代码事实 |
| G13 | **会话级事件查询靠 JSON 扫描。** `unowned_requests` 用 `json_extract` 过滤 `instance_id`／`session_id`（`repository.py:198-220`），事件表只有 `(operation_id, id)` 索引（`db_models.py:80-84`） | 同左 | 代码事实（第一阶段 §21.7 留下） |
| G14 | **Desktop 的本机 Agent 操作绑在旧工具名上。** `agent_target` 渲染成 `use_coding_agent` 的 `action`／`target`／`question`／`answer`（`agent_target.py:35-65`）；删除旧工具必须同时改这段 | 同左 | 代码事实 |
| G15 | **人随时能插话没有被写成验收。** 会话锁覆盖整次激活（第一阶段 L2），模型准入全局并发默认 4（§1.2）。后台 Work 的模型请求会与人的消息竞争准入；准入是否先到先得【待验证 E9】 | 同左 | 代码事实＋待验证 |

---

## 3. 与 DeepSeek 原型对照

### 3.1 对方怎么做（只引原文能确认的）

- **路由者只路由。**"The main Session only routes: it forwards the original Trigger and the routing facts, and makes no technical decisions."；执行者读原始来源、判断范围、实现与验证（`004-prompts.md`）。主会话不读编码提示和仓库 `AGENTS.md`，这是提示规则不是工具限制（`007-architecture.md`）。
- **说话的规矩。**"It confirms a request once and names what it will do. It stays quiet while it works. It writes again when a result is ready, when it needs a decision, or when it cannot finish."（`000-philosophy.md`）
- **领域对象。** Ticket 是"a unit of authorized work"，状态自定义、不跟随执行者会话；Assignment 把 Ticket 绑定到一个执行者实例，只有它的最终报告能在执行者侧结束 Ticket；Mandate 不可变，"wider scope means a new Ticket"（`001-domain.md`）。
- **授权在入口，不逐个动作审批。** 执行者以操作员的 OS 用户无审批运行，两个预设都是 `danger-full-access`；包装和禁用命令"establish defaults, not a security boundary"（`002-authorization.md`、`003-execution.md`）。
- **只在需要时唤醒路由者。** 唤醒来源是候选、follow_up、`worker_report`、`worker_attention`；问题立即转发，"ordinary progress [stays] on its Ticket"；`ticket_ended` 等注入但不唤醒（`003-execution.md`、`007-architecture.md`）。
- **有界卡片与按 ID 读。** `worker_report` 摘要最多 600 字符，截断时带 `chars` 和读指针；明细经 `orchestrator_read` 按 `ticket/<id>/result` 等读取（`007-architecture.md`）。
- **前缀缓存。** Policy 段每次请求重新生成，放在最后，"so a Policy change leaves the preceding prompt text unchanged"（`004-prompts.md`）。
- **并发与卡住。** 配置的 `maxWorkers` 名额，另留一个给人的消息或状态查询；回合结束后后台工作最多 30 分钟，到时杀掉 shell 作业并报 waiting；瞬时失败重试 3 次（`003-execution.md`）。
- **没有事件总线作骨干。** DomainEvents 多数没有消费者，副作用走持久 effect 表（`007-architecture.md`）。

### 3.2 采纳什么、不采纳什么

| 对方做法 | 本书 | 理由 |
|---|---|---|
| 路由者／执行者注意力分层，进展留在 Ticket | **采纳**：委派记录承接进展，父 Work 只见卡片（§4.1、§4.6、§4.7） | 与 G4 的根因直接对应 |
| 唤醒只限结果、问题、阻塞 | **采纳**，并加"执行者消失"事实（§4.6） | 第一阶段 §7.7 已定方向 |
| 有界卡片 + 按 ID 读 | **采纳**，长度沿统一结果预算，不另设常数（§4.7） | #636 同一原则 |
| 确认一次、干活时安静 | **采纳**为提示规则（§4.14） | 不是门槛 |
| 动态段放最后 | **采纳**（§5） | Yuki 已有同样结构（`turn_execution.py:837-860`） |
| 无审批、full access | **不采纳**，改为委派时固定范围（D2） | Yuki 操作的是**用户自己的电脑**；对方执行者跑在操作员授权的专用环境 |
| `maxWorkers` 名额、给人留一个名额 | **不采纳**名额（D3）；"人随时能插话"改写成不依赖名额的不变量（§4.13） | 名额是经验阈值，卡住的委派会占住它 |
| 30 分钟后台上限、重试 3 次、2 s 轮询 | **不采纳** | 经验阈值；Yuki 的"未知不重跑"更严（下行） |
| 对方的 Ticket 状态不跟随执行者会话 | **采纳**：委派状态由 Yuki 按原生事实结算，不照搬原生状态字符串 | 与第一阶段 §7.5 证据分级一致 |

### 3.3 为什么 Yuki 必须比对方更收敛上下文

- **路由者是跨很多群的社交人格。** 对方的主会话只服务一个团队的工单入口；Yuki 的 Main 在每个群、每个私聊里都是同一个角色，上下文同时承载聊天历史、记忆、参与判断和任务。每多一条外部进展，就挤掉一段对话，或多触发一次 compact（D4 的 77% 就是 compact 抖动）。
- **Yuki 操作的是用户自己的电脑。** 所以"委派时固定范围"代替"完全不审批"（D2），范围外的请求要能被发现、被回答，并且回答绑定原请求（第一阶段 §7.4）。

### 3.4 Yuki 已经更严的地方

- **未知结果不重跑。** 对方重启后让执行者"检查目录、git 和 GitHub 再决定是否重复外部动作"；Yuki 对已派发但回执未知的操作只按原 ID 查证，绝不重发（第一阶段 §21.2 B2b／B2c；`development-contract.md` §5）。
- **回执。** 每个原生操作有自己的持久回执、run 与事件序号（`repository.py:483-566, 717-891`），控制命令先结算目标再写回执（`service.py:242-275`）。

---

## 4. 设计

### 4.1 总体：委派记录不是第二个 Yuki

**三种对象**

| 对象 | 是什么 | 有没有 Yuki 模型循环 |
|---|---|---|
| 父 Work | 用户的请求（"让 Claude 修 X"） | 有。只在卡片唤醒、用户输入时运行 |
| 委派记录 | 子 Work，执行者为某台设备上的某个外部 Agent，关系为 delegate 或 takeover | **没有**。状态由原生事实确定性结算 |
| 观察记录 | Work，执行者为外部 Agent，关系为 observe | **没有**。注意事项通过路由者通知用户（§4.3） |

**为什么用子 Work 而不是新表**（删除优先、复用已有机制）【推断，基于下列代码事实】
- 父子关系、`wait(run=子 Work ID)`、`manage_task(cancel/resume, id)`、子树撤权已经存在（§1.2）。
- 子 Work 已有"结果摘要、通知水位和父 Work 输入一起提交"的通知路径（`persistent-subagents.md` 第 27-31 行），卡片可以沿它交给父 Work。
- Desktop 的 Works 列表、「需要你」分区、取消按钮、`work` 提示、`work_access` ACL 都按 Work 工作（§1.4），委派记录作为 Work 自动获得。
- 子 Work 在 `waiting_user` 时 `task_view` 显示为 `waiting_parent`（`work_control.py:112-121`），正好表达"范围外请求先到 Yuki"。

**需要新增的只有一处判断**：子任务调度器不为执行者是外部 Agent 的子 Work 启动 Main（`runtime_subagents` 新列 `executor_json` 非空即外部，§6）。

**委派记录的状态**

| 状态 | 进入条件（原生事实） | 对父 Work |
|---|---|---|
| running | 操作已登记或原生已接纳 | 不唤醒 |
| waiting_user（父视角 `waiting_parent`） | 有待回答的提问，或范围外的权限请求 | 唤醒，卡片带请求 |
| completed | 原生终态 completed | 唤醒，卡片带结果摘要 |
| failed | 原生终态 failed，或执行者消失且已确认无法继续（§4.9） | 唤醒 |
| cancelled | 取消已被原生确认，或显式释放 | 唤醒（由父自己发起时只结算，不唤醒） |
| 未知（`reason` 记执行者消失的事实） | §4.9 的事实 | 唤醒一次，告诉用户 |

委派记录的状态是 Yuki 的结算结果，不是原生状态字符串的照搬（§3.2）。完成后可以 `manage_task(resume, id, text)` 在同一原生会话上继续，沿子 Work 现有的 resume 语义。

**同一原生会话同时只有一个委派拥有者。** 第二个委派指向同一会话时返回 `conflict`，附现有拥有者的委派 ID。这是事实，不是上限：原生本来就是"一个会话同时一个回合"（`claude.py:612-616`；`codex.py:1014-1015`；`acp.py:471-472`）。

### 4.2 一套工具（工具合同 40，D1、D5）

**删除**：`use_coding_agent`、`get_coding_agents`、`EXTERNAL_AGENT_TOOLS`、`coding_agent_arguments`、`coding_agent_view`（`external_agents/tools.py` 全文），以及它们在 `codemode/tool_visibility.py:42-43`、`capabilities/executions.py:8-10`、`capabilities/invocation.py:79-84`、`capabilities/results.py:528-587`、`work_control.py:522`、`client_api/work_projection.py:45-46, 135, 277`、`services/main_agent_backend.py:786` 中的现役引用。

**保留**：历史回执按原名读取。`tool-kernel.md:402` 的规则是"没有后继的退役名按各自原事实读取"；新工具语义与旧工具不同，所以 `use_coding_agent`／`get_coding_agents` 列为**没有后继的退役名**，不加别名。`effect_outcomes.py`、`work_journal.py:616-640`、`reconcile_completed_children`（`work_control.py:502-579`）中按记录名读取的分支保留，新调用不经它解析（§7.4）。

**新声明**（参数名为草案，以执行者能力为准，不出现厂商名）

| 工具 | 新增 | 语义 |
|---|---|---|
| `start_subtask` | `executor?: {device, agent}`；`session?`；`relationship?: "delegate" \| "observe" \| "takeover"`；`scope?`（§4.4） | 省略 `executor` 时与今天相同（Yuki 子 Work）。给 `executor` 时创建委派或观察记录。`session` 指定已有原生会话（继续、接手或观察），按 §4.11 的实时性规则核对。`relationship` 默认 delegate；observe 不要求当前 Work（§4.3）；takeover 只在用户明确要求时使用（提示规则，§4.14） |
| `message_task` | `answer?`；`mode?: "queue" \| "steer"` | 对委派：只给 `text` 时，原生空闲则开新回合，运行中按 `mode` 追加（默认 queue）；`reply_to` + `answer` 回答待处理的提问或权限请求。能力不支持时在执行处返回 `unsupported_action` 与可用动作（沿第一阶段 B5 的实时声明拦截） |
| `manage_task` | action 增加 `release` | 对委派：`cancel` 精确取消原生回合并结束关系；`resume` 带 `text` 在同一会话继续；`release` 只结束 Yuki 的拥有关系，在回合边界关闭 Yuki 自己持有的原生进程（§4.11 交接），不取消原生工作。对观察记录：`cancel`／`release` 都只是停止观察，绝不发原生控制 |
| `wait` | 无 | `for=[{run: 委派 ID}]` 沿子 Work 等待；委派产生唤醒类卡片时满足（§4.6） |
| `get_tasks` | `agents?: {device?, agent?, session?}`；`history?` | 不给 `agents` 时与今天相同，委派和观察记录作为任务出现，`id` 读卡片与时间线、`cursor` 翻页。给 `agents` 时浏览：省略内容列设备与 Agent（含声明能力和实时事实），给 `agent` 列会话，给 `session` 读最近历史。这是现有 `get_coding_agents` 的查看部分，`list` 与会话级读取仍是在线读（第一阶段 B11） |

**合同只变一次**：所有 schema 变化（含 observe、takeover、release）在同一个 PR 进入合同 40（§8 第 6 段），提示的静态规则同一次修改，保证部署只有一次 system／tools 缓存失效（D4）。

**目录与执行处**：工具声明不读设备在线状态或能力（第一阶段 §21.8 第 2 点保持）；能力只在执行处核验。

### 4.3 三种关系（D6）

| | observe | delegate | takeover |
|---|---|---|---|
| 谁拥有原生会话 | 用户 | Yuki（这个委派） | Yuki（从用户手里接过） |
| 发送输入 | **永不** | 范围内 | 范围内 |
| 回答／拒绝权限与提问 | **永不**（连 deny 都不） | 范围内自动，范围外上报 | 同 delegate |
| 原生控制（cancel／steer） | **永不** | 可以 | 可以 |
| 阻止父 Work 结束 | 否 | 是（同所属执行） | 是 |
| 怎么开始 | 显式 `start_subtask(relationship="observe")` | `start_subtask(executor=…)` | `start_subtask(relationship="takeover", session=…)`，仅在用户明确要求时 |
| 怎么结束 | 显式停止、会话消失 | 完成、失败、取消、释放 | 同 delegate；释放即交还 |

**observe 的实现**
1. **服务端**：观察记录下的操作只允许 `get` 与 observe 型 watch。`respond`／`prompt`／`queue`／`steer`／`cancel` 以观察记录或其操作为目标时，在执行处拒绝（`observe_only`），不登记、不派发。观察记录的事件输入**不带 `permission_id`**，卡片只写"在本机等你处理"。
2. **连接器**（0.9.0）：watch 增加 `observation: "observe"` 标记。observe watch 收到的权限与提问只上传元信息（工具名、时间、原生请求 ID），**不登记为可由 Yuki 回答的请求**；Codex 驱动收到的 server request 不放进 `self.requests` 给这个 watch 用。respond 指向 observe watch 的请求时返回 `observe_only`。
3. **Claude 终端会话**：连接器没有它的回调（G2、G3）。observe 的事实来源是只读的会话注册表（`$CLAUDE_CONFIG_DIR/sessions/<pid>.json`，即 `claude agents --json` 的来源）：`status` 为 idle／busy／waiting，等批准时带 `waitingFor:"permission prompt"`，约 1.6 秒内出现（§15 E3）。连接器只能得知"在等"，看不到也答不了终端里的批准框，正合 observe 的要求。transcript 只作活动与回合结束的弱补充，不能区分"等批准"与"长工具"。不靠 hooks 修改用户配置。
4. **通知谁**：观察记录没有父 Work（它不能依附某个会结束的 Work，否则 Work 结束通知就丢了）。唤醒类事实作为一条输入交给原会话的路由者，以现有的无用户事件入口（与定时任务同类的可信执行身份，`development-contract.md` §1）启动一次普通 Main，由她用 `send` 告诉用户【推断，具体入口实施时核对】。观察记录独立存在，直到用户显式停止或原生会话消失（Q3 已定）。
5. **Desktop**：观察记录在 Works 列表中显示为"Yuki 正在看"，取消即停止观察；它的请求在「等你回答」里显示为"在终端处理"，没有回答按钮。

**回归验收（D6 原文）**：用户在自己终端各开一个 Claude、一个 Codex 会话；Yuki 观察。出错或弹权限时 Yuki 通知用户；**没有任何输入被注入**；权限请求由用户在终端处理。产物见 §10.3。

**第一阶段的 deny-only 能否触及被观察的会话**（D6 要求写明）
- 用户终端里的 Claude：**不能**。连接器收不到它的回调（`claude.py:674-677`）。
- Yuki 用 watch 观察 Yuki 自己 SDK owner 下的 Claude 会话：watch 收到完整请求（`claude.py:164-198`），**可以 allow 或 deny**；所有接收者被 detach 后才变成无主 deny-only（`claude.py:661-667`）。
- Codex 与 ACP 的 watch：**可以 allow 或 deny**（G1）。
- 所以第二阶段要做的不是"补一个 deny-only"，而是让 observe 关系在服务端和连接器两端都不可回答（上面 1、2）。

### 4.4 委派时固定的权限范围（D2）

**范围的形状**（与厂商无关，字段以 E6 结果定稿）：`write_paths`（可写目录，默认为会话 cwd）、`network`（是否允许联网）、`commands`（允许无需确认的命令前缀，如 `pytest`、`git commit`）。

**映射原则**：能用原生配置表达的交给原生；原生表达不了的，由连接器在回调处按范围核对；**任何映射都不扩大范围**；映射不了就上报，不默默放行。

| 执行者 | 映射 | 范围外怎么来 |
|---|---|---|
| Claude | `cwd` + `add_dirs=write_paths`；保留用户的 `setting_sources`；用 `settings` 覆盖 `permissions.ask`（Bash、Edit、Write、NotebookEdit、WebFetch、SendMessage 等有副作用的工具），让每次调用都进 `can_use_tool`，由连接器按范围自动批准或上报；范围外绝对禁止的可加 `disallowed_tools`（路径规则写成 `Edit(//绝对路径/**)`）。E6 证实：只用 acceptEdits＋`allowed_tools` 时，用户已 allow 的范围外写入会绕过委派范围；`ask` 覆盖压过用户 allow（§15） | 每次副作用调用都到连接器：范围内自动批准，范围外上报。不清空 `setting_sources`（用户的 CLAUDE.md、hooks、MCP 照常加载），只是委派会话里用户的 allow 规则不再越过委派范围 |
| Codex | Yuki 的回合传 `sandboxPolicy: workspaceWrite{writableRoots, networkAccess}` 与 `approvalPolicy: "on-request"`（0.169 schema）。**E6 推翻了"按回合传、不影响用户回合"**：策略会写进线程设置，留给之后不带策略的回合（包括用户 TUI 的回合，额外可写根会外泄给用户回合）。所以连接器在 Yuki 回合结束或释放时，把线程设置恢复为 Yuki 第一次接触该线程前的值；恢复不了时只接受不超出线程现有策略的范围（不加额外可写根），并在回执中说明（§15） | 越出沙箱时原生发 approval request，连接器只对 Yuki 自己开启的回合按范围自动批准或上报；用户回合的请求永不回答（app-server 不区分回合归属，§15 E1(c)） |
| ACP | 没有标准范围。**E5 推翻了"在回调处核对"**：两个真实 Agent（claude-agent-acp、codex-acp）从不调用 `fs/write_text_file`／`terminal/create`，文件与命令由原生直接执行；而且会话默认继承用户的 auto／guardian 模式，范围外写入不发 `request_permission`。所以带范围的委派在 `session/new`／`load` 后显式 `session/set_mode` 到会询问的模式（claude-agent-acp 的 `default`，codex-acp 的 `read-only`／`workspace-write`），再按 `session/request_permission` 判断：toolCall 的 kind、locations，以及 locations 缺失时的 `rawInput.command`／`rawInput.file_path`／`rawInput.cwd`；范围内选原生给出的 allow 选项。回调仍按范围核对（防御的不是这两个 Agent，而是会调用回调的 Agent）。没有会询问的模式时如实拒绝带范围的委派（§15） | 权限请求上报；codex-acp 的拒绝选项 `cancel` 会结束整轮，上报时如实标出 |

**范围外请求的路径**
1. 委派记录进入 `waiting_user`，父 Work 被唤醒，卡片带请求摘要（工具、目标路径或命令的必要元信息）。
2. 父 Work 中的 Yuki 可以：批准；拒绝；回答澄清问题；拿不准时向用户说明并请用户决定（**Q1 已定：Yuki 可以批准范围外请求**，否则工作会卡住）。每次批准与拒绝作为事实写进委派时间线：谁答的（Yuki 或用户）、答了哪个选项、Yuki 给出的理由、原请求摘要；Desktop 在该委派的时间线中显示，事后可查。批准只对这一个原生请求生效，不改写委派范围（改范围仍是新委派）。
3. 同一请求同时出现在用户 Desktop 的「等你回答」（§4.15）。这是用户自己设备上的请求，设备所属人物随时可以回答，这是授权事实；不另设"Yuki 转交后才显示"的状态（删除优先）。原生只消费一次，先答者生效，另一方看到真实结果（第一阶段 §7.4）。
4. 用户从 Desktop 回答时由服务端**直接结算**到原生请求，不经 Main、不花模型调用（**Q2 已定**）；实施时同步修改 `client-api.md:174`「不由个人 HTTP 另建执行驱动」一条，限定为：设备所属人物对自己设备上原生请求的回答直接结算。用户在聊天里让 Yuki 回答仍然可用，走 Main。

**范围随委派记录持久保存**（`runtime_subagents.executor_json`，§6），每次派发写进命令；改范围就是新委派，不改原记录（与对方"wider scope means a new Ticket"一致，§3.1）。

### 4.5 事件归属与分类（不新建事件总线）

**统一归属规则**：每条外部事件要么有拥有者（委派或观察记录，经操作的 `work_id`），要么作为**无主事件交给原设备所属人物的路由者**。不按"同一会话""最后一个 Work"或之后才加的 watch 回填（第一阶段 §7.3 第 4 点保持）。

**分类**

| 类别 | 事件 | 是否唤醒 |
|---|---|---|
| 唤醒 | 终态（completed／failed／cancelled）；question；需要回答的 permission；执行者消失（§4.9）；卡住事实（下行） | 是 |
| 进展 | 文本、工具调用、计划、`item/completed`、`turn/started`、`disposition`（merged／started_new_turn）、`permission_resolved`、排队回执、`device_connected` | 否，只进时间线 |

**观察记录的唤醒更窄**（协调者建议，2026-11-18 向用户说明，用户未否决；用户可随时改）
- 「出错」只指整轮失败、Agent 自身卡死、进程消失；单个工具或命令报错（测试失败、命令非零退出）是进展，不唤醒。写代码时这类报错是常态，逐条提醒会变成骚扰。
- 观察记录默认**不在回合结束时提醒**：用户自己的会话是一问一答，一轮结束不等于事情做完，用户通常就在终端前。只有用户明确说「做完告诉我」时，观察记录才把回合完成算作唤醒类（存为观察记录的一个开关，Yuki 建立观察时按用户原话设置）。
- 同一待回答请求只提醒一次；提醒尚在发送时到达的新唤醒类事件并入同一次（沿 Work 输入合并），不连续启动多次 Main。
- 「盯住某个会话」由提示规则引导为 observe，不再建定时任务轮询（§4.14）；定时任务只用于按时间的事。

**卡住按事实定义，不按时长**
- 原生 `requires_action`（Claude `session_state_changed`、待回答的回调）且没有可回答的拥有者；
- 排在待回答权限后面的输入（第一阶段实验 X3）；
- 执行者消失的事实（§4.9）。

已过去多少时间只作为展示信息，不触发任何状态（D3、AGENTS.md）。

**落点**
- 所有事件照常入 `external_agent_events`（第一阶段已有，`repository.py:812-833`），新增 `instance_id`、`native_session_id` 两列与索引（§6）。
- 进展**不再写 Work 输入、不再调 `signal_external_action`**（改 `repository.py:869-890`、`:1161`）。
- 唤醒类事件结算委派记录状态，并沿子 Work 通知路径给父 Work 一张卡片（§4.7）。
- 第一阶段的 `control_settled`（`repository.py:969-1036`）只在控制结果改变委派状态时产生卡片。

**委派时间线**：由该委派下的全部操作（`ix_external_agent_operations_work`）及其事件（`ix_external_agent_events_operation`）拼出，不新增表：派出（prepare）、写出、连接器记账、原生接纳、进展、提问、**谁回答的**（回答操作的 `actor_person_id` 与来源：父 Work 或 Desktop）、完成。Desktop 与 `get_tasks(id)` 读同一投影。

### 4.6 唤醒规则（第一阶段 §7.7）

- **只有唤醒类事件唤醒**（§4.5）。进展不消费任何等待，也不打断时间、插件或 `all` 等待（G4 的修正）。
- **唤醒谁**：委派的唤醒类事件唤醒父 Work（委派记录本身没有模型循环）；观察记录的唤醒类事件交给路由者（§4.3）；无主事件交给路由者（§4.5）。
- **四种等待形态**：
  - 父 Work `wait(run=委派 ID)`：沿子 Work 完成与 `waiting_parent` 通知交付；
  - 父 Work 没有等待、处于 `waiting_external`：排队唤醒（现有 `work_wait.py:417-425` 分支）；
  - 根 Work 只等消息（`waiting_user`）：**委派的提问或范围外请求要唤醒它**（G5 的修正：它在等"用户"，但委派的提问正是需要它处理的输入）；普通进展不唤醒；
  - `suspended` 与终态：不唤醒；终态父 Work 下的委派按子 Work 现有规则处理（问题保存，不复活父 Work，`persistent-subagents.md` 第 57-59 行）。
- **两种先后顺序**：先等后到走实时通知；先到后等时，注册等待只按委派状态匹配（委派已处于唤醒类状态即满足），不再对所有待处理外部 run 输入补发（改 `work_wait.py:296-318` 的外部 run 补发，委派记录不再产生这类输入）。
- **防反馈环**：查询不产生唤醒类事件；`previous_sdk_owner_not_attached` 是进展（第一阶段 §7.5）。

### 4.7 有界卡片

**卡片字段**（键序固定，见 §5）：`id`、`executor`（设备名、Agent 名）、`relationship`、`state`、`status_line`（一句话）、`summary`（截断）、`pending`（待回答请求：类型、工具、必要元信息、请求 ID）、`counts`（操作数、事件数）、`last_event_at`、`read`（`get_tasks(id=…)` 与游标）。

**长度**：沿 `tool-results.md` 的统一结果预算与 artifact 预览（#636 的同一机制），不为卡片另设常数。截断只影响展示，不是完成门槛。全文按 ID 读。

**出现的地方**
- 父 Work 的唤醒输入：只放卡片，不放原生事件 JSON（G4 的修正）。
- `get_tasks`：列表里每个委派一张卡片；`id` 读卡片加一页时间线。
- 路由者的 `<runtime_context>`：沿 `available_work`（现有上限 16），委派与观察记录以一行状态出现（§5 的动态段）。
- 完成结果：委派完成时把原生最终文本作为子 Work 结果保存（沿 `subtask_result`，`subagent_tools.py:121-133`），父 Work 卡片只带摘要。

### 4.8 自发回合的因果归属（第一阶段 §7.3，D8）

**改用"进程归属"而不是"任务链"**（E7 证实：后台任务通知与 ScheduleWakeup 开启的回合都发生在创建该会话的 SDK owner 进程里，没有 user 帧也没有回放帧；识别依据为"没有本方未取用输入时出现 `session_state_changed=running`"，后台任务回合还带原 `tool_use_id`）
- Claude 的自发回合（ScheduleWakeup、后台任务通知）只会发生在某个 CLI 进程里；Yuki 侧的 CLI 进程是连接器为某个委派打开的 SDK owner（`claude.py:310-359`）。**这个进程存在，就是因为那个委派**。所以 SDK owner 在委派拥有期间开启的回合，归该委派。这与第一阶段撤销的"同一 session／最后一个 Work"不同：依据是进程的创建来源，而不是会话 ID 或时间先后。
- 委派释放后（§4.11），连接器在回合边界关闭这个进程，之后不再有它的自发回合；关闭之前出现的回合是无主事件，交给路由者，其等待沿第一阶段规则只能 deny。
- 第一阶段撤销 C1 的三条理由（单执行绑定冲突、同一 task 多次通知、`scheduled-trigger` 子类型）都不再适用：不改写原 prompt 的执行绑定；归属在 owner 层，不依赖 task-id；定时触发的回合本来就属于这个 owner。自发回合在服务端登记为该委派下的一条"后续执行"操作（确定性 ID，由 owner 的 connection_id 与原生回合 ID 派生），有自己的 execution 与回执，不塞进原 prompt 的执行槽。
- **Codex**：线程属于用户的 daemon，回合由客户端开启；队列项开启的回合按 `clientUserMessageId` 归属（`codex.py:686-694`）。用户自己 TUI 的回合不归 Yuki。无人观察时开启的回合与权限请求如何路由【待验证 E1】。
- **ACP**：Agent 进程是连接器自己的，只有一个客户端。无 operation 时的 session_update 与权限请求是否出现【待验证 E5】；出现则同 Claude 处理：拥有期间归委派，释放后为无主。

### 4.9 执行者消失（第一阶段 §7.6）

| 事实 | 来源 | 委派状态 | 告诉用户 |
|---|---|---|---|
| 设备离线 | 传输事件 `device_offline`（`repository.py:1038-1091`） | 保持，`reason=device_offline`；原生可能仍在跑（断网不停原生执行，`local-agent-connector.zh-CN.md` 第 162 行） | 是，一次 |
| 原生进程退出 | Claude `native_sdk_connection_lost`、ACP `native_acp_process_exited`、Codex `connector/ownerDisconnected` | 未知，`reason` 为原事实 | 是 |
| 会话不存在 | 恢复时 `session_not_found`／`not_live` | failed | 是 |
| 设备授权撤销 | `require_device` 失败 | 未知，之后的新效果一律拒绝 | 是 |
| 连接器重启 | journal unknown 规则 | 未知，按原 ID 查证 | 只在查证无果时 |

- 未知不等于失败，也不重派（`development-contract.md` §5）。用户或 Yuki 可以显式释放（第一阶段 B3 的离线释放沿用）。
- 设备重新上线、查证得到真实终态时，委派按真实终态结算并唤醒。
- 不加心跳、超时或看门狗（第一阶段方案 D 的否决理由保持）。

### 4.10 回复走事件流（第一阶段 §7.2，设备协议 5）

- 连接器 `journal.complete` 在同一事务生成可重传的正式回执事件；服务器在可幂等恢复的提交边界完成结算后才 ACK；删除改动类命令的 RPC 应答处理（第一阶段 §7.2、§21.6）。
- **这同时是一次删除**：回执事件随事件流重传到 ACK 为止，第一阶段为了补"回复丢失"而做的重连读回（`service.py:127-130, 277-321`；`repository.unconfirmed`）对发送回执事件的连接器不再需要，可以删除。
- 来源清理路径（`service.py:1032-1092`）同时改为"登记后派发"（第一阶段 §7.2 末段）。
- **Q4 已定：一次性硬切**，与一次连接器发版同时上线（沿往例），不做新旧并行。

### 4.11 实时共享会话与交接（D7）

**按执行者**

| 执行者 | "终端继续跑，Yuki 也帮忙" | 做法 |
|---|---|---|
| Codex（用户 TUI 跑在 managed daemon 上） | **支持** | delegate 或 takeover 指向该线程：连接器经 proxy `thread/resume`（已有）。Yuki 的新输入一律用 `thread/queue/add`（空闲时立即开回合，忙时排在后面），按 `clientUserMessageId` 归属；**不用 `turn/start`**：E2 证实对运行中的线程 `turn/start` 不被拒，而是并入用户的回合并返回用户的回合 ID，会把用户回合误登记为 Yuki 的。steer 仍用 `turn/steer expectedTurnId`。双方实时看到对方的回合【协调者实验】。Yuki 只回答自己开启的回合的请求（`turn_operations`），**绝不回答用户回合的请求**；用户回合的请求在 delegate 关系下也只作为"用户在终端处理"的事实 |
| Codex（`--no-daemon` TUI） | 不支持 | thread-store 冲突即报 `session_live_elsewhere`，只能交接 |
| Claude | **不支持** | 恢复前查活会话：用官方只读 `claude agents --json` 核对 sessionId（在该实例的 `CLAUDE_CONFIG_DIR` 下）。活着就拒绝，返回 `session_live_elsewhere` 和可选做法：显式分叉（`fork_session=True`，得到新会话 ID，原会话不动）、等用户交接、或跨会话消息（Q5）。检测与打开之间的竞态写进风险（§11），不对用户文件加锁 |
| ACP | 不支持 | 只交接；E5 发现第二个进程对正在使用的会话 `session/load` 也被接受，是与 G8 同类的静默分叉，连接器只对自己持有的进程做交接，用户自己编辑器里的 ACP 会话无法检测，写进风险 |

**交接流程**（三种执行者都验收）
1. 用户在终端停下会话（退出 TUI 或 CLI）。
2. 用户让 Yuki 接手：`start_subtask(relationship="takeover", session=…)`。连接器按原生方式带完整历史恢复（Claude `resume`、Codex `thread/resume`、ACP `session/resume` 或 `session/load`），先过上面的实时性检查。
3. Yuki 在范围内工作。
4. 用户要接回：`manage_task(release, id)`。连接器在当前回合结束后关闭 Yuki 持有的原生进程（Claude SDK 进程退出；ACP 进程退出或会话关闭；Codex 只放开 Yuki 的客户端，不动 daemon，`codex.py:236-253`）。这一步修正 G9。
5. 用户在终端 `claude --resume <id>`／`codex resume <id>` 接回，看到包括 Yuki 回合在内的完整历史，没有分叉（E8 已证实：Claude 在回合边界关闭 SDK owner 后单链无分叉；Codex 用户经同一 daemon `resume --remote` 接回，线程 ID 不变；ACP 两个进程都退出后 `session/load` 回放并继续，`session/resume` 不回放）。

**不做**：自建共享会话、中继、合并两个 JSONL 分支（D7）。

### 4.12 并发：不设上限（D3）

- **不设**每设备、每 Agent 或全局的委派上限。
- **卡片上的事实**：每台设备当前 running／waiting／未知的委派数与各自状态（§4.7）；同一会话已有拥有者时的 `conflict`（§4.1）。
- **原生事实**：每个会话同时一个回合，忙时 prompt 冲突或改 queue（第一阶段 C1、C3）。
- **用户取消**：Desktop 任意委派可取消（§4.15）。
- **提示引导**：同一台设备上再派之前先看在跑什么，不是门槛（§4.14）。

### 4.13 人随时能插话（验收不变量）

**不变量**：来自用户或群成员的消息，无论后台有多少委派、观察或子任务在跑，都按正常路由处理。

用事实而不是时长表达：
1. **没有任何激活等待外部 Agent 的 I/O**（第一阶段 V3 已覆盖改动类动作；本阶段把 `get_tasks(agents=…)` 的在线读也纳入核对范围，若仍保留在线读则如实标出）。
2. **后台进展不产生任何模型请求、不持有会话锁**：委派记录没有模型循环，进展只入时间线（§4.5）。可验证的等式：父 Work 的激活次数 = 唤醒类事件数 + 用户输入数 + 它自己的等待到期数。
3. **模型准入**：后台 Work 与人的消息共用全局准入（默认 4，§1.2）。若 E9 证实准入先到先得，人的消息可能排在已排队的后台请求之后。**Q6 已定**：若 E9 证实准入先到先得，人的消息在准入队列中优先（只调整排序，不预留名额、不新增上限）；排队事实记录进验收产物，不用延迟阈值判定通过与否。

### 4.14 提示规则（静态，随合同 40 一次修改）

- **干活时安静**：委派后，只在有结果、需要决定或受阻时说话；确认一次（"我让 X 去做 Y 了"）。不轮询：等委派用 `wait(run=id)`，不反复 `get_tasks`。
- **observe 永不回答**：观察到的等待只告诉用户在哪里处理。
- **takeover 只在用户明确要求时**。
- **按能力说话**：可用动作以卡片和目录里的能力为准，不按 Agent 名字猜。
- **同一设备再派前先看在跑什么**（引导，不是门槛）。
- 这些都是程序规则，独立于 SOUL（AGENTS.md 角色条款）；位置在静态 system 段，部署后不再随请求变化。

### 4.15 Desktop

| 编号 | 内容 | 依赖 |
|---|---|---|
| DT1 | **按设备的运行视图**：所有本人设备（不只是本机）上的委派与观察记录，按设备分组，显示 running／waiting／未知与卡住事实；数据来自 Works API（委派是 Work）加设备目录 | Client API 14 |
| DT2 | **随处取消**：Works 列表与详情的取消按钮对委派即"取消原生回合并结束关系"，对观察记录即"停止观察"；「这台电脑」同样可用 | 现有取消命令 |
| DT3 | **「等你回答」入口**：「需要你」分区纳入有待回答原生请求的委派；按钮来自原请求的选项（复用 `NativeRequest`）；观察记录与无主等待分别显示"在终端处理"与"只能拒绝"；已撤回、已回答、owner 已换显示真实状态 | Q2、Client API 14 |
| DT4 | **步骤上的实时 Agent 状态**（第一阶段 §17 T2）：父 Work 中派出或指挥委派的步骤显示委派当前状态并链接到委派详情；刷新沿现有 `work` 提示（委派是 Work，状态变化即有提示），不加轮询 | Client API 14 |
| DT5 | **第一阶段 §17 T1 剩余**：分类从工具声明派生、`plugin`／`retired`、wait 与子任务明细、从现在起记录 action（删除 `effect_outcomes.py:238-242` 的工具名白名单）、步骤展开与跳转（`detail.operation_id` 现在不渲染） | 服务端投影 |
| DT6 | **「这台电脑」改用新工具术语**：`agent_target` 的渲染改成新工具参数（G14） | 合同 40 |

### 4.16 第一阶段复审遗留项（§21.7）

| 遗留项 | 第二阶段处理 |
|---|---|
| 新 queue 带已结束原操作的 `operation_id` 时连接器返回误导的 `instance_unavailable` | 委派按委派 ID 定位会话，不再拿原操作 ID 定位；`message_task` 对已结束操作的委派按会话写入。Codex 的 queue 同一路径一起核对 |
| 权限回调与消息流之间无顺序保证 | 回执事件（§4.10）带原生序号；回调先于回放时仍退化为只能 deny（安全方向），在时间线如实标出 |
| 只读视图忽略排队中的输入 | 委派卡片的 `pending` 含已写入未取用的输入（Claude `session.inputs`、Codex 队列 ID） |
| `close()` 期间新建的后台任务可能不被等待（`service.py:90-92` 只取一次快照） | 改为关闭标记后循环收集，直到没有新任务；与 `drain()` 同一写法 |
| `unconfirmed`、`unowned_requests` 的扫描范围 | `unowned_requests` 改用 §6 的新列与索引；`unconfirmed` 在协议 5 后对发送回执事件的连接器删除（§4.10） |
| X2（只有一个长工具的回合） | 并入 E7 |

### 4.17 运维风险（只记录）

Bot 在自动化运行中途重启，该次运行丢失（协调者转述的运维教训）。停机顺序可以另立一项，本书不展开。

### 4.18 边界

- **Yuki 作为 ACP 服务端**（`src/yuki/acp_adapter/`，方向 A）是相反方向：别的客户端驱动 Yuki。不在本书范围，不受本书改动影响；本书的 ACP 均指 Yuki 作为客户端驱动本机 ACP Agent。
- 站点特定能力不进核心（D9）。

---

## 5. 前缀缓存规则与验收（D4）

### 5.1 规则

| 规则 | 本阶段怎么守 |
|---|---|
| system、tools 逐字节稳定 | 工具合同 40 一次变更后冻结；声明不读设备、能力、在线状态（§4.2） |
| 提示静态规则一次改完 | §4.14 的规则与合同 40 同一次部署 |
| 动态状态只进末尾 `<runtime_context>` 或工具结果 | 卡片、设备、队列、范围、待回答请求只出现在：激活开始的 `runtime_state`（`turn_execution.py:837-846`）、之后追加的运行资料（`:855-860`）、工具结果、唤醒输入 |
| 只追加 | 卡片作为新输入追加，不改写已有输入或 transcript；委派状态变化不回写已发出的卡片 |
| 结果有界，少触发 compact | 唤醒输入只放卡片（§4.7）；进展不进上下文（§4.5） |
| 顺序与键序确定 | 卡片字段固定构造顺序并以 `sort_keys` 编码；列表按 `(created, id)` 排序；时间统一格式 |
| compact 检查点稳定 | 本阶段不改 compact；进展不再进入上下文，compact 只会更少 |

### 5.2 验收：真实请求前缀字节的事实测试

扩展 `tests/integration/test_external_agent_prompt_prefix_e2e.py`：

| 场景 | 通过条件 |
|---|---|
| P1 委派 | 父 Work 调 `start_subtask(executor=…)` 前后的请求：system 与 tools 字节完全相同，且不含设备、实例、会话、操作、run ID |
| P2 进展 | 委派产生 N 条进展：父 Work **没有新请求**；若父 Work 恰好因别的原因运行，前一请求逐字节是后一请求的前缀，进展不出现在其中 |
| P3 完成唤醒 | 唤醒后的第一个请求：之前的公共历史逐字节相同，卡片只出现在末尾新增部分 |
| P4 提问唤醒 | 同 P3，卡片带待回答请求 |
| P5 观察通知 | 路由者的通知激活：system、tools 与之前的请求相同 |
| P6 并发 | 两个委派交错完成：两次唤醒的卡片分别只出现在各自新增部分，且两次运行的卡片编码字节确定（同输入同字节） |

- 编码器：生产的 Claude Messages 与 Chat Completions 编码器（沿第一阶段 §21.8）。OpenAI Responses 由 runner 生成 continuation 形态，第一阶段没覆盖；本阶段尝试用能产生该形态的模型替身补上，做不到就如实写明。
- 产物：`test-results/external-agent-prompt-prefix/<protocol>.json`，逐条哈希与共同前缀长度。
- **命中率**：上线前后各记录一次生产 chat_agent 的上游真实 usage 命中率，只作记录，不作门槛（D4、`development-contract.md` 第 81 行"缓存使用上游真实 usage 核对"）。

---

## 6. 数据模型与迁移（`0159`）

| 变更 | 内容 | 理由 |
|---|---|---|
| `runtime_subagents.executor_json` | 可空。为空即 Yuki 子 Work；非空存 `{device_id, instance_id, native_session_id?, relationship, scope}` | 开发约束 §5：执行者资料在工作者表；调度器据此不为外部执行者启动 Main |
| 观察记录 | 根 Work（`parent_work_id` 为空）＋一行 `runtime_subagents`（`executor_json.relationship = "observe"`） | 不新增表 |
| `external_agent_events.instance_id`、`native_session_id` | 可空列；新事件入账时写入（服务端入账时已解析出实例，`repository.py:786-794`）；迁移从 `payload_json` 回填 | G13 |
| 索引 `ix_external_agent_events_session` | `(instance_id, native_session_id, kind, id)` | 会话级查询（无主等待、观察记录时间线） |
| 委派与操作的关联 | 沿 `external_agent_operations.work_id = 委派 Work ID`，不新增列 | 已有 `ix_external_agent_operations_work` |

- 迁移冻结，不依赖可变 ORM（`CLAUDE.md` 持久化约定）。回填在迁移中执行，不在运行期写事务里扫描。
- 写事务内不等待模型、网络或文件 I/O（`development-contract.md` §5）；委派结算沿第一阶段"reader 先准备、writer 复核"的写法（`repository.py:763-790`）。
- `docs/architecture/README.md` 的 schema 版本同步更新。

---

## 7. 兼容与发布

### 7.1 Bot

- 合同 40 一次部署（§4.2）。部署时仍在跑的旧 Work：已登记的 `use_coding_agent` 操作沿原 run 与回执结算（`SandboxTaskRun`、`control_settled` 不变），可以继续 `wait(run)`；不能再发起旧工具调用（按未声明工具拒绝，`tool-kernel.md:402`）。
- 冷恢复按记录名读取旧回执（`work_journal.py:616-640`），不重派。

### 7.2 连接器 0.8.0 → 0.9.0

- 0.9.0 新增：observe watch（§4.3）、范围映射（§4.4）、Codex 与 ACP 的无主等待处理（§4.8）、自发回合按 owner 归属、Claude 活会话检测与显式分叉（§4.11）、释放即在回合边界关闭 owner、ACP 回调核范围。全部经 `capabilities` 与 `device_capabilities` 声明，设备协议仍为 4（协议 5 另见 §4.10、Q4）。
- **新 Bot + 0.8.0**：0.8.0 没有声明范围能力，带范围的委派在执行处返回 `connector_outdated`，**不在没有范围的情况下执行**（否则就是默默放宽 D2）；observe 由服务端单边保证不可回答（§4.3 第 1 点），0.8.0 的 Codex watch 只会把请求留在本地不应答，不影响终端用户回答【推断，E1 核对】；Desktop 横幅提示更新。
- **旧 Bot + 0.9.0**：新增能力不被使用，0.9.0 的行为退回 0.8.0 等价（跨版本 E2E 覆盖）。
- 更新仍需用户在 Desktop 点击（第一阶段 §21.5）。
- **实施注（§16）**：上面两条写于 Q4 定稿前，按"设备协议仍为 4"设想。Q4 定为一次硬切后，0.9.0 讲协议 5：新 Bot 与 0.8.0、旧 Bot 与 0.9.0 都在握手时被拒，连接器停在 `protocol_unsupported`、不循环重连；跨版本 E2E 按此断言。"带范围的委派对 0.8.0 返回 `connector_outdated`"仍在服务端实现（实时声明里没有 `scope` 就拒绝），只是 0.8.0 已连不上新 Bot。

### 7.3 Client API 13 → 14

- 只加字段：Work 上的 `executor`、`relationship`、委派卡片、待回答原生请求；设备目录的运行事实。
- 旧 Desktop（合同 13）连新服务端：委派与观察记录作为普通 Work 显示目标与状态；「这台电脑」的操作仍经 `agent_target` 发出，服务端改按新工具术语渲染（DT6），旧请求格式继续接受。
- 新 Desktop 连旧服务端：缺字段时退回现有显示；`REQUIRED_API_REVISION` 保持 11。

### 7.4 删除清单（D1：提示、文档、测试、CI 一起）

| 类别 | 内容 |
|---|---|
| 源码 | §4.2 所列；`agent_target.tool_arguments` 改为新术语 |
| 提示 | `main_agent_backend.py:786` 的错误提示；工具描述 |
| 现行文档 | `development-contract.md:49`；`main-agent-runtime.md:3, 467-473`；`tool-kernel.md:107, 370, 387, 402`；`client-api.md:12, 42, 293`；`docs/operations/desktop-computer.zh-CN.md:36`；`docs/operations/local-agent-connector.zh-CN.md:253-263`；`persistent-subagents.md`（子任务可指定外部执行者） |
| 测试 | 引用旧工具名的 14 个测试文件（`git grep -l -e use_coding_agent -e get_coding_agents -- tests`）：按新工具改写或删除；冻结旧语义的断言删除 |
| CI | `.github/workflows/quality.yml:168-209` 的 `claude-cross-version`：组合改为新 Bot × 0.8.0／0.9.0、旧 Bot × 0.9.0，并加入 Codex 与 ACP 的组合，产物数量断言同步 |
| 历史文档 | 日期任务书与 `CHANGELOG.md` 的旧名保留原样（任务书按当时基线保留） |

---

## 8. 交付方式：一个大 PR（用户决定，2026-11-18）

用户原话："我建议合成大pr"。沿 Computer Use（#622 任务书与实现同一 PR）的做法：

- **后端一个 PR**：本任务书 + 全部实现（Bot、连接器 0.9.0、设备协议 5 硬切、合同 40、Client API 14、迁移 `0159`、旧工具删除）+ 实验产物 + E2E。即本 PR（#639）继续在同一分支上提交。
- **Desktop 一个 PR**（另一仓库）：DT1–DT6，合同固定点到后端合并提交。
- **PR 内按提交分段**，便于审查，顺序如下：
  1. 实验 E1–E9 与产物：在隔离目录、私有 tmux、新会话中进行，不触碰用户正在运行的会话；
  2. observe 安全修复（原计划 PR1）；
  3. 唤醒分类与有界卡片（原计划 PR2）；
  4. 迁移 `0159`（原计划 PR3）；
  5. 连接器 0.9.0（原计划 PR4）；
  6. 一套工具与删除旧工具（原计划 PR5）；
  7. 协议 5 硬切（原计划 PR7）；
  8. 第一阶段遗留项（原计划 PR8）；
  9. 文档与 E2E。
- 实验推翻设计时，先修订本书再实现（§9 末段）。
- **上线是一次性的**：Bot 与 Desktop（含连接器 0.9.0）同一窗口发布。协议 5 硬切意味着 Bot 上线后、用户点击「更新连接器」前，设备连接不可用（§11）。

---

## 9. 先做的实验（§8 第 1 段）

全部在隔离目录、新会话中进行；E2、E3、E8 需要用户自己的终端会话，事先征得同意；付费模型调用需要授权（第一阶段 §11 V6 的规矩）。每项产出原始帧序列（类型、ID、相对时间，不含正文）与执行命令。

| 编号 | 实验 | 要取得的事实 | 影响 |
|---|---|---|---|
| E1 | **Codex 无主回合与权限**：(a) Yuki 开启的回合在 detach 后发 approval request；(b) 无人观察时队列项开启回合；(c) 同一线程两个客户端（TUI＋连接器）时，approval request 发给谁、谁能答、`serverRequest/resolved` 是否两边都收到；(d) 连接器不应答时 app-server 是否一直等它 | Codex 是否存在与 Claude 同类的"没人能答"的卡住 | §4.3、§4.8、§4.11、G11 |
| E2 | **Codex 实时共享 E2E**：TUI 在 managed daemon 上，连接器经 proxy `thread/resume`；空闲时 `turn/start`；TUI 回合在跑时 queue 与 steer；双方看到对方回合；`--no-daemon` TUI 时的冲突错误 | 支持面与错误码 | §4.11 |
| E3 | **Claude 活会话检测**：`claude agents --json` 的字段与 status 取值（空闲、运行、等权限）；终端启动到出现在列表的间隔；多 profile（`CLAUDE_CONFIG_DIR`）；Windows；能否据此及时得知"终端在等批准"；transcript 文件变化能否作为补充事实 | G3、G8 的可行性与竞态窗口 | §4.3 第 3 点、§4.11 |
| E4 | **Claude 跨会话消息**：headless／SDK 进程向活的终端会话 SendMessage；回复如何回来；`crossSessionInbound` 三种设置；是否必须由一个 Yuki 持有的 Claude 会话来发（即每条消息要花一个 Claude 回合） | 能否作为"在旁边帮忙"的产品路径 | Q5 |
| E5 | **ACP**（ACP 模式的 Gemini CLI；本机当前未安装 `gemini`）：detach 后的 `request_permission`；无 prompt 时的 session_update；`session/load` 与 `session/resume` 交接；单客户端限制；toolCall 的 kind 与 locations 是否足以判断范围 | G11、§4.4 的 ACP 映射 | §4.4、§4.8 |
| E6 | **范围优先级**：Claude SDK 的 `allowed_tools`／`disallowed_tools`／`permission_mode`／`add_dirs` 与用户 user／project／local 设置中 allow 规则的先后；用户已 allow 的命令是否还会进 `can_use_tool`。Codex 按回合传的 `sandboxPolicy`／`approvalPolicy` 是否覆盖用户 config。ACP 回调拒绝时 Agent 的反应 | §4.4 能否不放宽地落地 | §4.4、G10 |
| E7 | **Claude 自发回合归属**：SDK owner 下的 ScheduleWakeup 与后台任务通知（含"同一 task 先 stopped 后 completed"）；第一阶段留下的 X2（只有一个长工具的回合） | §4.8 的 owner 归属是否足够 | §4.8 |
| E8 | **释放与交还**：Claude SDK 进程在回合边界退出后，终端 `--resume` 是否看到含 Yuki 回合的完整历史、无分叉；Codex 放开客户端后 TUI 恢复；ACP 进程退出后用户侧加载 | §4.11 交接流程 | §4.11、G9 |
| E9 | **模型准入顺序**：读 model executor 的准入实现并用替身复现：后台 Work 请求已排队时，人的消息的请求排在哪里 | §4.13 第 3 点 | Q6 |

实验结论可能推翻 §4.3 第 3 点、§4.4、§4.8、§4.11 的部分做法；推翻时修订本书，不在实施中静默改设计。

---

## 10. 测试与验收

### 10.1 原则

- **E2E 优先**，每个 E2E 结束时产出可验证、可重复复现的产物：按 `work_id / operation_id / run_id / execution_id / permission_id / event_id / state_sequence` 串起原生观察、journal、服务端事件、委派状态、父 Work 输入、等待与投递回执（沿第一阶段 §11 的产物格式）。
- **需要隔离测试时先写全部失败情况**（AGENTS.md），见 §10.2。
- 不用"正常延迟内""命中率达到 X"作为通过条件。

### 10.2 先写的失败情况（隔离测试用，写在实现之前）

| 编号 | 失败情况 |
|---|---|
| F1 | observe 记录下 answer（allow 与 deny）、prompt、queue、steer、cancel 任一被执行 |
| F2 | observe 记录的事件输入带 `permission_id`，或 Desktop 给出回答按钮 |
| F3 | 一条进展唤醒了父 Work，或打断了时间、插件、`all` 等待 |
| F4 | 进展的原生 JSON 出现在父 Work 的模型请求里 |
| F5 | 根 Work 只等消息时，委派的提问没有唤醒它；或普通进展唤醒了它 |
| F6 | 先到后等：委派已处于唤醒类状态时注册 `wait(run)` 没有立即满足；或旧进展输入被补发 |
| F7 | 第二个委派抢占已有拥有者的同一会话 |
| F8 | 带范围的委派在 0.8.0 连接器上被执行 |
| F9 | 范围映射扩大了范围（Claude 用户 allow 规则绕过、Codex 沙箱放宽、ACP 回调写出范围外文件） |
| F10 | Yuki 批准或拒绝范围外请求后，委派时间线缺少回答者、选项、理由或原请求摘要；或批准被当成扩大了委派范围，影响之后的请求 |
| F11 | Claude 恢复一个活的终端会话时没有拒绝，产生分叉 |
| F12 | Codex delegate 回答了用户 TUI 回合的请求 |
| F13 | 释放后 Yuki 的 Claude 进程仍持有会话，用户 `--resume` 产生分叉 |
| F14 | 执行者消失后委派一直停在 running，没有告诉用户 |
| F15 | 执行者消失后委派被改判为失败并重派，或设备重连后真实终态没有覆盖未知 |
| F16 | 自发回合的进展或权限归到了别的委派或别的 Work |
| F17 | 卡片编码在同输入下字节不同，或改写了已发出的卡片 |
| F18 | 旧 Work 在部署后继续调用 `use_coding_agent` 被执行，或其已登记操作的结算丢失 |
| F19 | 用户从 Desktop 回答后，原生没有消费、或消费了两次（含 Yuki 同时回答） |
| F20 | 同一设备上并发多个委派时，某个委派的事件串到另一个委派的时间线 |

### 10.3 端到端验收

| 编号 | 场景 | 通过条件 |
|---|---|---|
| A1 | **两台设备 × 多 Agent**：设备 1 上 Claude 与 Codex，设备 2 上 Codex 与一个真实 ACP Agent。群里用户一次提出三件事，Yuki 分别委派给三个执行者，各带范围。期间群里持续有人说话 | 三个委派并行；群消息都按原路由得到回复（§4.13 第 1、2 点的事实成立，第 3 点的排队事实写入产物）；一个委派发范围外请求 → 父 Work 被唤醒 → Yuki 向用户说明 → 用户在 Desktop 回答 → 原生消费并继续；设备 2 中途断网 → 委派显示设备离线、Yuki 告诉用户 → 恢复后按真实终态结算；三个完成各唤醒一次；父 Work 激活次数满足 §4.13 第 2 点的等式 |
| A2 | **observe 回归**（D6 原文）：用户终端各开一个 Claude 与 Codex 会话，Yuki 观察 | 出错或弹权限时 Yuki 通知用户；没有任何输入注入（终端 transcript 与 Codex 线程中无 Yuki 输入）；权限由用户在终端处理；服务端与连接器日志中没有针对这两个会话的 respond 或输入命令 |
| A3 | **前缀字节**（§5.2 P1–P6） | 见 §5.2 |
| A4 | **Codex 实时共享** | 用户 TUI 与 Yuki 同一线程交替与排队；双方看到对方回合；Yuki 只回答自己回合的请求 |
| A5 | **Claude 活会话** | 恢复活会话被拒绝并给出选项；显式分叉得到新会话、原会话不变 |
| A6 | **交接往返**（Claude、Codex、ACP 各一次） | 用户停 → Yuki 接手带完整历史 → 释放 → 用户接回，无分叉、无重复执行 |
| A7 | **执行者消失**：进程被杀、会话被删、设备授权撤销 | 委派进入相应状态，用户被告知一次，之后没有新效果，未知不重派 |
| A8 | **重启窗口**（沿第一阶段 S04–S07，套用到委派记录） | 原生命令次数、原 ID、原预算、委派状态、父 Work 输入在每个崩溃窗口后一致 |
| A9 | **跨版本**：新 Bot × 0.8.0／0.9.0；旧 Bot（`ad59a686`）× 0.9.0；旧 Desktop × 新服务端 | §7.2、§7.3 描述的行为逐项成立；进 CI（§7.4） |
| A10 | **删除核对** | `git grep -e use_coding_agent -e get_coding_agents -- src docs/architecture/*.md docs/operations` 只剩历史读取分支与日期文档；工具清单中没有旧名 |

**生产对照**（上线后，只读）：上线前后各 7 天 chat_agent 的上游 usage 缓存命中率、每个委派父 Work 的模型请求数分布，作为记录，不作门槛。

---

## 11. 风险

- **E3 可能找不到可靠的 Claude 终端事实来源**：Claude 终端会话的 observe 只能缩小为"看历史与是否活着"（§4.3 第 3 点）。
- **活会话检测有竞态**：检测之后、打开之前用户在终端启动同一会话，仍会分叉。只能缩短窗口并在说明中写明，不对用户文件加锁。
- **E6 可能显示用户设置会先于委派范围放行**：要做到 D2，可能必须改变连接器对用户 Claude 设置的读取方式，这会影响用户已有的本地习惯，实施前报告。
- **一套工具这一段体量大**：合同只能变一次，删除与新增必须同一次部署；以分段提交和充分的 E2E 缓解。
- **连接器更新需要用户点击**：在更新之前，带范围的委派不可用（§7.2）。
- **委派记录没有模型循环**：一切判断都推给父 Work。若父 Work 已终态，委派的提问按子 Work 规则保存但不复活父 Work（§4.6），需要路由者或用户另行处理；验收覆盖这一路径。
- **协议 5 硬切（Q4 已定）**：从 Bot 上线到用户更新连接器之间整个设备 WSS 不可用（第一阶段 Q6 的代价）。上线时与连接器发版同一窗口，并提前告知用户点击更新。
- **模型准入竞争**（§4.13 第 3 点）若 E9 证实先到先得，人的消息仍可能排在后台请求之后。

---

## 12. 用户决定（2026-11-18，经协调者转达）

| 编号 | 问题 | 决定 |
|---|---|---|
| **Q1** | 范围外的权限请求，Yuki 可以批准吗？ | **可以。** 用户原话："权限以外的请求，应该可以让Yuki批准？不然工作会卡住啊"。Yuki 拿不准时再问用户；每次回答写进委派时间线（回答者、选项、理由、原请求摘要），只对该请求生效，不改写范围（§4.4） |
| **Q2** | Desktop「等你回答」的回答怎么送达 | **服务端直接结算**到原生请求，不经模型；聊天里让 Yuki 回答仍可用（§4.4 第 4 点） |
| **Q3** | observe 的生命周期 | **独立存在**，直到用户显式停止或原生会话消失（§4.3 第 4 点） |
| **Q4** | 回执走事件流（设备协议 5）怎么切 | **一次性硬切**，与一次连接器发版同时上线（§4.10、§11） |
| **Q5** | Claude 跨会话消息要不要做成产品路径 | **第二阶段只做 E4 实验**，按结果再决定 |
| **Q6** | 人的消息要不要在模型准入队列中优先 | **要**，前提是 E9 证实现在先到先得；只调排序，不预留名额、不加上限（§4.13） |

---

## 13. 本书对任务输入的修正

| 输入中的预期 | 代码事实 | 本书处理 |
|---|---|---|
| 委派 Work"睡到被唤醒"再处理 | 若委派 Work 有模型循环，就是用户否决的看护者 | 委派记录没有模型循环，唤醒父 Work（§4.1） |
| 第一阶段的无主 deny-only 可能触及被观察的会话 | 触及不到终端 Claude（收不到回调）；但普通 watch 本身就能 allow／deny，Codex 尤甚，Desktop 也给按钮 | 修 watch 而不是补 deny-only（§4.3、§8 第 2 段） |
| observe 可以覆盖用户终端里的 Claude | 终端 Claude 现在无法 watch（`not_live`），列会话状态一律 unknown | 需要 E3 的只读事实来源，否则缩小承诺 |
| 唤醒规则是"待扩大到权限／提问" | 实际是过度唤醒：每条进展都唤醒等待中的 Work 并全文进上下文；同时只等消息的根 Work 收不到权限 | 两个方向都改（§4.6、§8 第 3 段） |
| ACP 的范围由连接器处理 `session/request_permission` | 连接器自己的文件写入与终端回调完全不核范围，这比权限请求更直接 | 范围在回调处核对（§4.4） |
| Codex 与 ACP 的无主处理可能需要 | ACP 的权限在无接收者时与 Claude 当初一样一直等；Codex 把请求留在本地不应答 | E1、E5 后同样处理（§4.8） |
| Claude 交接只需"检测活会话" | detach 不关闭 Yuki 的 SDK 进程，用户接回时同样分叉 | 释放即在回合边界关闭 owner（§4.11） |
| Desktop 回答走某个回答接口 | Desktop 的所有本机 Agent 操作都是带 `agent_target` 的聊天消息，渲染成旧工具参数名；没有操作级 REST 接口 | 删除旧工具时一并改（DT6）；回答路径见 Q2 |
| 人随时能插话主要靠非阻塞派发 | 还受全局模型准入（默认 4）影响 | 写入不变量的第 3 点与 Q6 |

---

## 14. 事实与推断汇总

- **代码事实**：§1.2–§1.4 各表；G1–G14；§4.3 末段关于 deny-only 的三条；§4.16 的 `close()` 快照。
- **协调者实验**：§0.2 中 D7 的全部实验事实。
- **事实**：#636 的滚雪球数据；第一阶段 §17.3 的步骤统计；Codex 0.169 schema 字段（本机生成）；SDK 0.2.172 的 `ClaudeAgentOptions` 字段（本机安装文件）；DeepSeek 文档引文。
- **推断**：子 Work 复用的收益（§4.1）；owner 归属的充分性（§4.8）；观察通知经路由者的入口（§4.3 第 4 点）；新 Bot + 0.8.0 下 observe 的单边保证（§7.2）；事故 Work 步骤与 G4 的逐步对应。
- **待验证**：E1–E9。

---

## 15. 实验结果与设计修订（§8 第 1 段，2026-11-18 实施前）

**做法**：全部在会话临时目录的隔离项目里进行，私有 tmux 服务器（`-L p2claude`、`-L p2codex`），新开的一次性会话；Claude 用 `--model haiku`，Codex 用自启的 `codex app-server --listen unix://…`（不连用户的 managed daemon），模型 gpt-6-luna、effort low。没有接触、恢复、发消息给或结束用户正在运行的 Claude／Codex 会话与进程。结束后 `claude purge`、`thread/delete` 清理了实验会话，`~/.claude.json` 与 `~/.codex/config.toml` 与实验前一致。

**产物**：[`docs/operations/external-agent-phase2-experiments/`](../operations/external-agent-phase2-experiments/)，`claude/` 与 `codex-acp/` 下每项一个目录，含 `frames*.jsonl`（帧类型、各类 ID、相对时间，不含正文、命令文本与密钥；临时路径已替换为 `<scratch>`）、`commands.sh` 与驱动脚本，以及逐项记录事实、版本与未验证项的 `summary.json`。E9 的产物由 `tests/unit/test_model_admission_order.py` 生成（`test-results/model-admission/e9.json`）。

**版本**：Claude Code 2.1.311，claude-agent-sdk 0.2.172；codex-cli 0.169.0；ACP：claude-agent-acp 0.108.0（@agentclientprotocol，内置 claude-agent-sdk 0.3.326，用现有 Claude 登录与 haiku）、codex-acp 2.7.0（@agentclientprotocol，用现有 Codex 登录）、Gemini CLI 0.68.0（无凭据，只到能力层：`session/new` 返回 "Gemini API key is missing"）。

### 15.1 事实

| 编号 | 观察到的事实 | 用真实 Agent 验证了什么 |
|---|---|---|
| E1 Codex 无主 | (a) 审批挂起时客户端断开：回合保持 inProgress，app-server 不自动拒绝；之后任一客户端 `thread/resume` 立即收到**同一个** requestId 并可回答。(a2) 没有任何客户端时回合照常运行，审批照常挂起等待下一个 resume 的客户端。(b) 空闲线程 `queue/add` 立即开回合；发起方断开后排队项照样在前一回合后开回合，`userMessage.clientId` 即 `clientUserMessageId`。(c) **app-server 不区分回合归属**：每个订阅者都收到对方回合的 server request，脚本拒绝了 TUI 回合的请求且被接受，两边都收到 `serverRequest/resolved`，先答者生效。(d) 150 秒内没有超时或改派 | codex-cli 0.169 自启 app-server＋TUI `--remote`；更长等待未观察 |
| E2 Codex 共享 | 空闲时第二个客户端 `turn/start`、运行中 `turn/steer`（错的 expectedTurnId 得 -32600 "expected active turn id"）、`queue/add` 都可用，双方实时看到对方回合。**对运行中的线程 `turn/start` 不被拒，返回活动回合 ID、并入用户回合**（相当于 steer）。`--no-daemon` TUI 显示 "This conversation is open in another app… r retry / f fork"；第二个 app-server `thread/resume` 得 -32600 "thread … already has an active writer" | 同上；用户真实的 managed daemon 按规定未使用，以第二个自启 app-server 代替 |
| E3 Claude 活会话 | `claude agents --json` 每行有 pid、cwd、kind、startedAt、sessionId、name、status：空闲 `idle`，回合中 `busy`（回车后 0.14 s），弹批准框 `waiting` 且 `waitingFor:"permission prompt"`（约 1.6 s），回答后 0.13 s 回到 busy。数据来自 `$CLAUDE_CONFIG_DIR/sessions/<pid>.json`（另有 `entrypoint` cli／sdk-py）。SDK 会话也列出，`can_use_tool` 挂起时同样 `waiting`。信任对话框确认后 0.7 s 出现；`--resume` 后带被恢复的 sessionId。换 `CLAUDE_CONFIG_DIR` 列表为空。等批准期间 transcript 末行只是没有 tool_result 的 tool_use，与长工具分不开 | Claude Code 2.1.311；已登录的第二个 profile 与 Windows 未验证 |
| E4 跨会话消息 | 默认设置下 SDK 进程 SendMessage 送达终端会话：接收方开一个 `promptSource=system`、`turnOrigin=peer` 的回合并在自己那边作答，发送方 85 s 内收不到回复（接收方没有回发）。`crossSessionInbound` refuse：接收方不变、发送方多一个系统通知回合；hold：接收方向用户显示暂存、不开回合。CLI 没有发消息子命令，每条消息至少花发送方一个 Claude 回合。**默认权限模式下 SDK 会话的 SendMessage 不经过 `can_use_tool`** | 同上；未使用未公开的 UDS 原始协议 |
| E5 ACP | 能力：claude-agent-acp 有 loadSession、list／resume／fork／close／delete、模式 default／acceptEdits／plan／auto／bypassPermissions；codex-acp 有 loadSession、list／resume／fork／close、模式 read-only／workspace-write／agent／agent-full-access；Gemini 只有 loadSession。**会话模式继承用户设置**：claude-agent-acp 默认 `auto`，cwd 外写入不发 `request_permission`；codex-acp 默认 `agent` 由自身 guardian 自动批准。显式 `session/set_mode` 到 default／workspace-write 后才发 `request_permission`。**两个 Agent 都不调用 `fs/write_text_file`／`terminal/create`**。权限无人回答时一直等（120 s）；`session/cancel` 使 Agent 发 `$/cancel_request`、工具调用标 failed、prompt 以 cancelled 返回。prompt 结束后后台 Bash 完成会自开回合，含 tool_call 与一次 `request_permission`。codex-acp 的 execute 权限没有 locations，只有 `rawInput.command`／`cwd`，拒绝选项 `cancel` 会结束整轮。第二个进程对正在用的会话 `session/load` 被接受且能 prompt，是静默分叉；两进程都退出后 `session/load` 回放并继续，`session/resume` 不回放 | claude-agent-acp 0.108.0、codex-acp 2.7.0；Gemini 的回合、权限、回调未验证（无 API key）；codex-acp 的无 prompt 回合与活会话 load 未验证 |
| E6 范围 | Claude：0.8.0 现状（模式 default）下用户已 allow 的命令与范围外写入都不经回调就执行；acceptEdits＋`add_dirs`＋`allowed_tools` 仍放过用户已 allow 的范围外写入；`setting_sources=[]` 能挡住但丢掉用户的 CLAUDE.md、hooks、MCP；`disallowed_tools` 压过用户 allow（路径规则须写 `Edit(//…)`）；**保留全部 setting_sources、只加 `settings` 的 `permissions.ask:["Bash","Edit","Write"]` 时，每次 Bash／Write（含用户已 allow 的与只读 ls）都进 `can_use_tool`**。Codex：在模拟的宽松配置上按回合传 on-request＋workspaceWrite，越界写发审批、不写出；额外 `writableRoots` 生效；**策略粘在线程上**，下一个不带策略的回合（含用户 TUI 回合）仍按它执行，额外可写根外泄给用户回合。ACP 见 E5 | Claude 用项目级 local 规则代替用户真实 `~/.claude/settings.json`（未改用户配置，优先级链相同）；用户 PreToolUse hook、MCP 工具、Codex `networkAccess` 未验证 |
| E7 自发回合 | 后台任务通知：回合 idle 约 7 s 后出现 `task_notification(completed, task_id, 原 tool_use_id)`、`session_state_changed=running`、新的 init、assistant（`parent_tool_use_id` 为空）、result、idle，没有 user 帧与回放帧。回合内 TaskStop 只产生一条 stopped 通知，之后没有 completed 也没有自发回合（"先 stopped 后 completed"未复现）。ScheduleWakeup 在 headless 下约 62 s 后 `session_state_changed=running`、`command_lifecycle started`（前面没有本方 queued 与回放）。X2（只有一个前台长工具 sleep 20）：只有一条 `task_started`，之后 16 s 静默无心跳；6.3 s 写入的输入立即 `queued`，22.5 s tool_result 之后才回放（同 UUID）并入同一回合，只有一个 result | Claude Code 2.1.311、SDK 0.2.172 |
| E8 交还 | Claude：SDK owner 在回合边界关闭后终端 `--resume` 看到 SDK 回合，transcript 单链、分叉点 0；负对照（终端活着时 SDK 恢复同一 ID）不报错、产生 1 个分叉点（子节点 `[sdk-py, cli]`），恢复前注册表那一行正带着该 sessionId；`fork_session=True` 得新 ID，原 transcript 的 sha256 与行数不变。Codex：脚本放开后 TUI `resume --remote` 看到全部历史、线程 ID 不变；最后一个客户端 unsubscribe 约 60 s 后 daemon 发 `thread/closed`。ACP：见 E5 | 同上 |
| E9 模型准入 | 生产 `TaskModelExecutor`＋门控替身（容量 1）：等待中的后台请求（子任务 worker、定时任务、SELF）让给后到的人的请求（现有前台优先规则）。被卡片唤醒的根 Work 沿原入口的前台优先级运行，与人的消息同属前台；两者之间的先后取决于条件变量先唤醒谁，**不是先到先得**（本次复现中后到的人先被接纳） | 替身复现，不涉及真实模型 |

### 15.2 设计修订（已改入正文）

1. **§4.3 第 3 点**：Claude 终端会话的 observe 有可靠只读来源（会话注册表的 `status`／`waitingFor`），不必缩小承诺；transcript 只作补充。
2. **§4.4 Claude 行（G10、§11 第 3 条风险）**：不需要清空 `setting_sources`，也就不改变对用户本地设置的读取；连接器只在委派会话里用 `settings` 的 `ask` 覆盖，让副作用调用都经过连接器按范围裁决。这确实意味着委派会话里用户自己的 allow 规则不再越过委派范围，这正是 D2 的含义，记在这里而不是另立一项决定。
3. **新风险（E4）**：委派出去的 Claude 默认可以不经批准给用户的其他 Claude 会话发跨会话消息。委派会话的 `ask` 覆盖包含 SendMessage，范围内也不自动批准，一律上报。
4. **§4.4 Codex 行（E6）**：范围粘在线程上。连接器在 Yuki 回合结束或释放时恢复线程原设置；恢复不了时只接受不超出线程现有策略的范围。
5. **§4.4 ACP 行（E5）**：控制点是 `session/set_mode` 与 `session/request_permission`（含 rawInput），不是回调；带范围的委派必须显式切到会询问的模式，否则就是 F9 的默默放宽。
6. **§4.11 Codex（E2）**：Yuki 的新输入一律 `thread/queue/add`（按 clientId 归属），不用 `turn/start`；F12 完全由连接器按 `turn_operations`／clientId 保证。
7. **§4.11 ACP（E5）**：并发 `session/load` 静默分叉，写进风险；连接器只交接自己持有的进程。
8. **§4.8（E7）**：owner 归属成立，识别依据改为"没有本方未取用输入时出现 `session_state_changed=running`"。
9. **Codex 与 ACP 的无主等待（G11）**：Codex 的审批一直挂起并广播给之后的每个客户端，终端用户随时能答，**不需要** deny-only，也不得替用户回答；ACP 的进程归连接器，无主的 `request_permission` 没有别人能答，按 Claude 的做法只传元信息、只能 deny（或 `session/cancel`）。
10. **E9 与 Q6**：Q6 以"E9 证实先到先得"为前提；E9 显示后台请求已经让给人，而被唤醒的根 Work 与人的消息之间既不是先到先得、也没有保证的先后。前提不成立，本阶段**不改准入排序**，把事实交用户决定是否仍要"人优先"（实现只需给被唤醒的 Work 一个只调排序的优先级，不预留名额）。
11. **E4 与 Q5**：跨会话消息每条至少花发送方一个 Claude 回合，回复不会自动回到发送方，接收方把它当同伴消息；按 Q5，本阶段只记录，不做成产品路径。

### 15.3 未验证（如实）

- Gemini CLI 的回合、权限与回调（没有 API key）；本阶段"真实 ACP Agent"的验收改用 claude-agent-acp 0.108.0 与 codex-acp 2.7.0，二者都是 ACP 协议的真实 Agent，但底层仍是 Claude／Codex。
- 用户真实的 managed daemon（按规定不碰）；已登录的第二个 Claude profile；Windows。
- 用户 PreToolUse hook 返回 allow、MCP 工具与 WebFetch 在 `ask` 覆盖下的表现；Codex `networkAccess=false` 是否被执行；超过 150 s 的挂起。
- "同一 task 先 stopped 后 completed"未复现。

---

## 16. 实施、复审与发布边界（2026-11-18）

### 16.1 交付与分段

后端 PR YuanYeYouTao/Yuki#639（分支 `docs/external-agent-delegation-phase2`）按 §8 分段提交；Desktop 为 Institute-of-Agent-Life/Yuki-Desktop#87（分支 `feat/delegation-phase2`）。没有合并，没有上线。

| 段 | 提交 | 内容 |
|---|---|---|
| 1 实验 | `79f585ef`、`280c2b27` | E1–E9 产物；按实验修订本书（§15） |
| 2 observe 安全 | `0c565ce9` | watch 即观察：输入、回答（含 deny）、原生控制一律 `observe_only` |
| 3 唤醒与卡片 | `1fd5f4d5` | 进展不写输入不唤醒；唤醒类事实以有界、键序确定的卡片进入 Work；只等消息的根 Work 被请求唤醒 |
| 4 迁移 0159 | `f5e5ecf2` | `executor_json`；事件 `instance_id`、`native_session_id` 与会话索引 |
| 5 连接器 0.9.0 | `aea3db90`…`9f9f2c23` | 协议 5 回执、observe、范围映射、Codex 归属、ACP 无主、自发回合、活会话检测与分叉、释放 |
| 6 一套工具 | `c6bbf452` | 合同 40、委派/观察记录、Client API 14 |
| 7 协议 5 | `cb0e46ff` | 回执走事件流、删除重连读回、清理登记后派发 |
| 8 遗留项 | `cf812f57` | `close()` 循环收集；file_copy 测试竞态 |
| 9 文档与 CI | `da5df536`、`1432dce4` | 现行文档、更新日志、跨版本 CI |
| 复审处理 | 见 §16.4 | 连接器 `e3b8f340`…`eb5641d8`；服务端随后的提交 |

### 16.2 与设计的差异（如实）

- **Codex 范围**：0.169 的 `thread/queue/add` 不接受策略参数，范围只能经 `thread/settings/update`（不开回合）施加，回合结束或释放时按记录的原设置恢复，恢复失败以 `scope_restore_failed` 上报并在下次重试；接口不可用、或 `write_paths` 不覆盖线程 cwd（workspaceWrite 总把 cwd 设为可写）时返回 `scope_unsupported`，不在放宽的范围下执行。§4.4 写的"恢复不了时只接受不超出现有策略的范围"没有实现，改为拒绝。设置在 Yuki 的输入排队时就生效，紧接着开始的用户回合会沿用收窄后的设置：这是记录的竞态，不加锁。`thread/settings/update` 只在测试替身中验证过。
- **Claude 范围**：`settings` 的 `ask` 覆盖 Bash、Edit、Write、MultiEdit、NotebookEdit、WebFetch、WebSearch、SendMessage；MCP 工具不在清单内（§15.3 未验证）。SDK 进程开时没带范围、之后来了带范围的输入时返回 `scope_conflict`。
- **一次性回答**：连接器拒绝持续性的选项（ACP `allow_always`／`reject_always`，Codex `acceptForSession`、带策略修订的决定），返回 `one_time_only`，落实"批准只对这一个请求生效"。
- **ACP 自发回合**：ACP 不报告自发回合何时结束；Yuki 结束它时（下一次 prompt 或释放）以 `cancelled`、`reason: native_turn_end_not_reported` 收尾，不编造完成。
- **协议 5 与跨版本**：见 §7.2 实施注。
- **E9／Q6**：前提不成立，不改准入排序（§15.2 第 10 条），交用户决定。
- **E4／Q5**：只记录。
- **前缀字节**：§5.2 的 P1–P4 进 E2E（`tests/integration/test_external_agent_prompt_prefix_e2e.py`，Claude Messages 与 Chat Completions）；P5（观察通知）、P6（两个委派交错完成）与 OpenAI Responses 没有覆盖。P6 中"同输入同字节"由单元级 F17 覆盖。
- **Desktop**：DT5 的 `plugin`／`retired` 分类没有做（服务端 kind 列举里没有这两个值）。
- **Codex 共享回合（已知限制，验收 P1）**：用户在 TUI 里 steer 进 Yuki 的回合、或排队项与 Yuki 的排队项被 Codex 合并成一轮时，这一轮整体归 Yuki，其中由用户输入引出的审批也会作为 Yuki 回合的请求出现（范围内由连接器按范围裁决，范围外交给 Yuki 或用户）。app-server 不按输入区分请求来源，连接器无法逐个请求归属；记录为风险，不另建机制。

### 16.3 测试

- 失败情况先写：F1–F6、F17（`test_external_agent_observe_and_wake.py`）；F7、F8、F10、F14–F16、F18–F20、观察、释放（`test_external_agent_delegation.py`）；协议 5（`test_external_agent_protocol5.py`）；迁移（`tests/unit/test_migration_0159.py`）；连接器 F1、F9、F11–F13、F16 与协议 5（`packages/yuki-agent-connector/tests/test_*_phase2.py`、`test_protocol5_receipts.py`）；复审发现（`test_external_agent_review_fixes.py`、连接器 `test_review_fixes.py`）。各文件都先在实现前确认失败。
- E2E：前缀字节（A3）、跨版本（A9，CI `claude-cross-version`）、删除核对（A10，`git grep` 只剩历史读取分支与日期文档）、Codex 委派（`test_codex_connector_delegation_e2e.py`）。真实 Agent 的 A1、A2、A4–A8 见 §16.5。

### 16.4 三路独立复审与处理

三路复审由新开的只读 agent 完成（服务端／运行时、连接器、Desktop），只给代码与本书，不给结论。每项修复都先写失败测试并确认失败。

**服务端（1 个 blocker，其余为 major 与 minor）**

| 发现 | 处理 |
|---|---|
| B：首次派发被拒（如 `session_live_elsewhere`）后记录永不结算，completion 还把记录改成 queued | 已修：终结记录运行的回执按终态结算记录；记录拥有的运行不再走父子 completion 路由。测试 `test_refused_start_…`、`test_a_finished_run_never_queues_…` |
| Codex 整数 requestId 让回答去重与 pending 失效 | 已修：按文本比较。`test_integer_native_request_ids_…` |
| 重复上报的请求唤醒两次；`serverRequest/resolved` 不让记录恢复 | 已修：按原生请求 ID 去重；解决按与 pending 同一套判断。`test_redelivered_request_…` |
| 执行者消失后卡片仍显示 running（F14 断言接受了它） | 已修：等待执行者期间任何丢失事实都是 unknown；断言收紧 |
| message_task／resume 没派发却报成功 | 已修：未派发即失败结果；resume 被拒时记录回到原状态 |
| 无主等待没人处理，deny 入口随旧工具消失 | 已修：交给最后持有该会话的记录（委派 → 父 Work 卡片，`answer_at: deny_only`；观察 → 通知）；`message_task`／Desktop 回答可 deny。`test_unowned_wait_…` |
| 连接器重启的快照恢复（unknown）被当成执行者消失 | 已修：`native_snapshot_restored` 不唤醒（§4.9）。丢失按操作只告诉一次 |
| 观察通知逐条启动 Main | 已修：排队中的通知 Work 合并新事实 |
| 被拒的清理控制永不重试 | 已修：每个连接一次尝试 |
| release 的 detach 可能因记录先结束而不发出 | 已修：结束关系的 detach 不受记录终态限制 |
| 写事务中按记录读取事件与 json 查询 | 部分保留：结算时读取的是该记录自己操作下的行（经操作与事件索引），不是历史扫描；`session_owner` 先查操作的会话索引。记录在此 |
| 回执不匹配使设备永久断开 | 已修：确认并丢弃，记日志 |
| Desktop 回答离线时误报成功 | 已修：返回错误 |
| 旧 Desktop 的 agent_target 不可用 | 已修：记录按所代表的人物授权（不要求从父 Work 操作）；不属于任何委派的旧操作渲染为说明而非工具参数 |
| 父 Work 自己结束（取消、释放、派发失败）仍被结果唤醒 | 已修：只结算不通知 |
| F2（Desktop 对观察请求）、F6（先提问后等待）、F15（离线→上线→真实终态）缺测试 | 已补 |

**连接器**

| 发现 | 处理 |
|---|---|
| B1：空闲委派的 release 到不了连接器，SDK owner／ACP 会话一直占着 | 已修：服务端对记录的全部未解除操作发带 `release` 的 detach；连接器对已结束的操作也交给驱动，`_held()` 不计已解除的 owner |
| B2：范围只随第一条 prompt 下发 | 已修：追加与恢复都带范围 |
| M1：Codex workspaceWrite 隐含 cwd 与 /tmp | 已修：设 `excludeSlashTmp`／`excludeTmpdirEnvVar`；不覆盖 cwd 时 `scope_unsupported` |
| M2：线程设置取消时不恢复、恢复失败无声、重启后丢失 | 已修：取消即恢复；失败上报并重试；原设置随回执持久化；竞态写进说明 |
| M3：ACP 把模式更新当自发回合，且自发回合永不结束 | 已修：只有回合内容开后续执行；Yuki 结束时给终态 |
| M4：Claude 自发回合吸收输入后归属断开 | 已修：整个原生回合固定后续执行身份 |
| M5：可以回答"总是允许"，扩大范围 | 已修：`one_time_only` |
| M6：Codex 重启不恢复排队中的 prompt | 已修 |
| S4／S7／S8（Windows 存活判断、detach 进入派发中、资源泄漏） | 已修 |
| S1–S3（MCP 不在 ask 清单、ACP default 模式仍加载用户 allow、`thread/settings/update` 未经真实 Codex 验证） | 记录为风险与未验证，不在本阶段改变用户设置读取方式 |

**Desktop**：见 Desktop PR 的复审处理记录（实时状态改随 `refresh` 刷新、查询键、旧服务端回退、`unknown` 提交不确定、同一请求两处回答共用一次尝试、失败提示、F19 测试强度、三语状态文案、读屏、只有 `answer_at = yuki_or_user` 可答）。

### 16.5 真实 Agent 端到端验收（A1、A2、A4–A8）

`tests/acceptance/`（标记 `real_agent`，只有 `YUKI_REAL_AGENT_E2E=1` 才运行，CI 不收集），脱敏产物与复现命令在 [`docs/operations/external-agent-phase2-acceptance/`](../operations/external-agent-phase2-acceptance/README.md)。真实部分：Bot 容器、SQLite、Main／Work、Client API、设备 WSS（协议 5）、进程内连接器 0.9.0；Claude Code 2.1.311（haiku）、自启 codex app-server 0.169（gpt-6-luna／low）、claude-agent-acp 0.108.0。替身：只有 Main 模型是脚本。两台设备是同一台 Mac 上的两个连接器配对；设备 2 断网用本机 TCP 中继切断。

| 场景 | 结论 |
|---|---|
| A1 | 通过；"父 Work 激活次数 = 唤醒类 + 用户输入 + 到期"严格等式不成立：先后约 1 s 到达的两张卡被合并进同一次激活（激活 5，输入 6），每张卡恰好被消费一次 |
| A2 | 通过：没有任何输入注入，权限由用户在终端处理，回答接口对观察请求返回 `observe_only` |
| A4 | 部分通过：交替、只用 `queue/add`、只答自己回合已验证；`not_owner` 没能用真实 Agent 触发（用户输入总被并进 Yuki 的回合，见 §16.2 已知限制） |
| A5 | 通过 |
| A6 | 通过（Claude、Codex、ACP 各一次，无分叉、无重复执行） |
| A7 | 通过；会话删除后恢复得到 `cwd_unknown`（不是 `session_not_found`） |
| A8 | 通过（W1–W4） |

验收发现 P2（委派结束后无法排在用户运行中的回合后面）与 P3（Bot 停机被记成设备离线并唤醒父 Work）已修，失败测试先写；P1 见 §16.2。

### 16.6 验证与 CI

- 本机（macOS arm64）全量：单元 3750 通过，失败 10 条与干净基线逐条相同（manager_deployment×2、shared_media_cache_mount、sticker×2、terminal_environment、work_execution_receipt_regressions、workspace_copy×2、workspace_publish，macOS 环境问题）；集成通过，唯一失败 `test_plugin_frozen_binding` 同为基线（macOS `/var` 软链）；连接器包 315 通过，6 个 `test_claude_discovery` 失败同为基线（本机有 Homebrew claude）。ruff、`mypy --platform linux src`、连接器 ruff／mypy 通过。
- PR #639 Quality 的 16 个 job 全部通过（`fc0be380` 一次全绿；`86ae135e` 上 Windows 连接器 job 的 `test_chrome_e2e` 真实 Chrome 一次未在时限内打开调试端口，本 PR 未改 `chrome.py`，重跑通过）。其中 `Connector across Bot and connector versions` 跑三种组合与 Codex 委派 E2E。
- Desktop PR #87：结果见 PR；e2e 中出现过的偶发与本 PR 无关的项在 PR 里逐项记录。

### 16.7 发布边界（只记录，没有上线）

- **一次上线**：Bot 与 Desktop（随附连接器 0.9.0）同一窗口发布；合同 40 一次部署，system／tools 缓存失效一次。迁移 0159 在 `yuki init-db` 时执行（加列、回填、建索引，不重写已有行）。
- **协议 5 硬切**：Bot 上线后、用户在 Desktop 点击「更新连接器」到 0.9.0 之前，所有本机连接器连不上（握手被拒，连接器停在 `protocol_unsupported`）。上线前提醒用户；更新需要用户点击。
- **重启窗口**：部署前按运维约定核对即将到期或正在运行的定时任务（中途重启会丢失该次运行）。Bot 停机本身不再写设备离线事实。
- **Desktop 合同固定点**在评审期间指向后端分支 head，后端合并后由协调者改为合并提交。
- **生产对照**：上线前后各记录一次 chat_agent 的上游真实 usage 命中率与委派父 Work 的模型请求数分布，只作记录（§5.2、§10.3）。

### 16.8 没有完成的部分

- §5.2 P5、P6 与 OpenAI Responses 的前缀字节场景（§16.2）。
- A4 的 `not_owner` 没有用真实 Agent 触发（§16.5）；Gemini CLI 无凭据，ACP 用 claude-agent-acp。
- Q6（准入排序）按 E9 结论未实施，待用户决定；Q5 只有 E4 记录。
- Codex 范围的 `thread/settings/update` 只在替身中验证；MCP 工具不在 Claude 的 ask 清单；ACP `default` 模式仍加载用户 allow 规则（§16.4 S1–S3）。
- 写事务中按记录读取自身事件的做法保留（§16.4）。
