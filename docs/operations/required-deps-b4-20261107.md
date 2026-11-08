# B4 只为测试保留的可选依赖删减实施记录（2026-11-07）

任务书：[文件膨胀与函数爆炸删减任务书](../architecture/Yuki-文件膨胀与函数爆炸删减任务书-2026-11-07.md) §4.3、§5 B4。基线 `6581eced`。

范围：`MessageProcessor`（`services/processor.py`）、`ChatService`（`services/chat.py`）、`CommandService`（`services/command_service.py`）及其直接下游 `AutomationCommandHandler`、`ConversationCommands`、`ModelCommands`。只改 bot 进程内代码；不新增文件、抽象层、配置项，不改权限。

## 1. 改代码前列出的可能失败

逐项改为必填前先写下以下失败情形，实施后逐条核对（结论见 §3）。

### 1.1 生产装配是否给齐

- F1 `container.py` 构造 `MessageProcessor` 时漏传某个改为必填的依赖，`import yuki.container` 成功但 `ApplicationContainer(...)` 构造时报 `TypeError`。
- F2 某依赖在生产中由 bundle 字段提供，而该字段类型本身是 `X | None`（如 `self.vision`），改为必填非空后 mypy 报错或运行期遇到 `None`。
- F3 `conversation.py` 构造 `ChatService` 时未传 `context_assembler`、`event_publisher`；若误改为必填，生产构造失败。
- F4 `MessageProcessor` 的 `automation_*` 三个参数与 `memories` 在类内无读取，只在装配处传入；删除后 `container.py` 仍传入导致 `TypeError`。

### 1.2 setter 注入的时序

- F5 `ChatService.event_publisher` 由 `MessageProcessor.set_event_publisher` 在 processor 构造时注入；ChatService 构造后、processor 构造前若有 turn 发布通知会读到 `None`。改为“processor 必填 event_publisher 并无条件注入”后需确认 chat 侧仍允许构造期为空，且构造期没有 turn。
- F6 `set_participation`、`set_automation_tools`、`set_plugin_tools` 为装配环（participation 需回调 container）而存在；本批不动，删除构造参数时不得误删这些 setter。

### 1.3 测试 fake 是否覆盖原 `None` 分支行为

`tests/conftest.py::build_harness` 原先少传依赖，下列分支在测试中走 `None` 路径；改为必填后 harness 必须提供保持原行为的 fake，或提供生产等价对象并确认测试结果不变：

- F7 `asr_service is None` → `AudioInput(error="not_configured")`。fake 用 `provider=None` 的真实 `ASRService`；若测试设置 `asr_enabled=False` 会变成 `disabled`，需核对 `asr_*` 失败原因断言。
- F8 `conversation_media is None` → 跳过媒体缓存与后台提交。真实服务会下载/缓存附件并启动后台任务，测试需原样返回 message 的 fake。
- F9 `autonomous_groups is None` → `_defer_ordinary` 立即同步 promote 并返回实际结果；真实服务进入静默窗口并返回 `ordinary_deferred`。依赖同步结果的 group_open 测试会失败。
- F10 `emoji_library is None` → 不提交表情候选；真实库会做视觉分析。
- F11 `group_recovery is None` → 不识别恢复请求、drop 时不回提示；真实恢复可能改变 `ProcessResult.reason` 和发送条数。
- F12 `client_links is None` → `/ai link` 落入普通命令；真实 link 命令会改变结果。
- F13 `direct_plugin_commands is None` → 不匹配直达命令；无绑定的真实 `DirectCommandRouter` 也不匹配。
- F14 `event_publisher is None` → 不发布通知；空 `PluginEventBus` 无订阅者，不应改变结果；chat 侧也会拿到 publisher。
- F15 `admission_signals` 只用于 `configure_timeout`；真实 `PluginAdmissionSignalAdapter` 的超时须为正数。
- F16 `memory_worker` 原由 processor 内部构造（未传 `people`）；生产传入的 worker 带 `people`。harness 构造方式不同不应改变测试。
- F17 `ChatService.tool_artifacts is None` → 不注册 artifact 读取 provider、不配置保留期；改为真实仓库后 registry 多一个 provider，可能改变断言工具集合的测试；仓库根目录必须在测试临时目录内。
- F18 `ChatService.tool_receipts is None` → 不写工具回执；改为真实仓库后测试库会多 `memory_tool_receipts` 行，可能改变计数断言。
- F19 `ChatService.processing_status` 透传到 `MainAgentTurnService`；真实服务每个 turn 起后台 typing 任务，测试 bot 可能收到额外平台调用。
- F20 `ChatService.memory_context` 原在 `None` 时内部构造，`open_memory_session`/`open_self_memory_session` 的 `is None` 分支实际不可达；删除不改变行为。
- F21 `CommandService` 的 `automation_*` 为 `None` 时 `/ai automation` 返回“未启用”、`/ai status` 显示 0；改为真实 bundle 后输出变化，需核对断言。
- F22 `plugin_commands is None` → `/ai plugin` 返回“插件系统当前未启用”，直达命令返回“当前不可用”；真实 adapter 在插件系统关闭时输出可能不同。
- F23 `model_invocations is None` → 返回“尚未初始化”。
- F24 `turn_coordinator is None` → `/ai stop` 不取消可中断 turn；真实 coordinator 会取消。

### 1.4 关闭某功能的配置下依赖是否仍存在

- F25 插件系统关闭（`plugin_system_enabled=False`）时 `PluginModule.build()` 是否仍构造 `events/direct_commands/commands/admission_signals`。
- F26 ASR 未配置时 `build_asr` 是否仍返回 `ASRService`。
- F27 自动化、语义参与、视觉未配置时对应对象是否仍存在；视觉未配置时 `media.vision` 为 `None`，必须保持可选。

## 2. 改动

### 2.1 `MessageProcessor`

- 改为必填：`people`、`memory_worker`、`autonomous_groups`、`runtime_config`、`asr_service`、`conversation_media`、`direct_plugin_commands`、`turn_coordinator`、`admission_signals`、`event_publisher`、`emoji_library`、`group_recovery`、`client_links`。生产唯一构造点 `container.py` 原本全部给齐且均非 `None`（`build_asr` 必返回 `ASRService`；插件 bundle 在插件系统关闭时仍构造 events/direct_commands/admission_signals）。
- 删除死参数：`memories`（只用于内部兜底构造 `MemoryWorker`）、`automation_service`、`automation_repository`、`automation_worker`（类内无读取）；`container.py` 同步不再传入。
- 删除 5 处 `x or 内部构造` 兜底（people、memories、memory_worker、runtime_config、turn_coordinator）。
- 删除 11 处 `is None`/`is not None` 分支：`_defer_ordinary` 无 autonomous 时立即 promote、client link、group recovery 启用请求与 drop 提示、直达插件命令、媒体缓存、`observe_context`、`observe`、表情候选提交、ASR 未配置、构造期 event publisher；drop 提示删分支后与相邻的同条件 `if limited.allowed` 合并。
- 删除只被构造函数调用的 `set_event_publisher`，构造时直接注入 `chat.set_event_publisher(...)`。

### 2.2 `ChatService`

- 改为必填：`memory_context`、`prompt_composer`、`turn_coordinator`、`conversation_scopes`、`effect_gate`、`processing_status`（`conversation.py` 原本全部给齐）。
- 删除参数：`context_assembler`（无调用方传入，始终内部构造）、`event_publisher`（生产从不经构造传入，只由 `MessageProcessor` 构造时经 setter 注入；`_event_publisher` 保留 `| None` 初值以表达 ChatService 构造到 processor 构造之间的窗口，该窗口内没有 turn）。
- 删除 6 处兜底构造（conversation_scopes、effect_gate、memory_context、context_assembler 的 if/else、prompt_composer、turn_coordinator）和 2 处本就不可达的 `_memory_context is None` 分支。
- `ConversationModule` 的 `tool_artifacts`、`tool_receipts`、`processing_status` 同步改为必填（唯一构造点 `container.py` 给齐）。

### 2.3 `CommandService` 及下游

- 改为必填：`automation_service`、`automation_repository`、`automation_worker`、`turn_coordinator`、`plugin_commands`、`model_invocations`；`AutomationCommandHandler`、`ConversationCommands`、`ModelCommands` 对应参数同步去掉 `| None`。
- 删除 9 处分支：直达插件命令不可用、`/ai stop` 无 coordinator、`/ai plugin` 未启用、`/ai automation` 未启用、模型统计未初始化、`/ai status` 中 automation repository ×3 与 worker ×1。

### 2.4 保留可选的依赖

| 依赖 | 理由 |
| --- | --- |
| `CommandService.vision_service` / `ConversationCommands.vision` | 生产 `media.vision` 在视觉未配置时为 `None`（F27），真实可选；类型保留 `| None`，只去掉 `= None` 默认值（唯一生产构造点显式传入）。 |
| `ChatService.tool_artifacts`、`tool_receipts` | 生产总是给齐，但 `None` 状态被 `services/main_agent_backend.py`（`artifact_available`、回执记录守卫）读取，该文件属进行中的工具面准备二；且真实 `ToolArtifactRepository` 构造时写 `database.work_result_store`，会改变所有 harness 测试的 Work 结果存储与 `read_tool_artifact` 暴露（F17/F18）。待该 PR 合并后与 backend 的两处判断一起处理。 |
| `MessageProcessor._participation`（setter） | 不是构造默认值；`SemanticParticipationService` 回调 container，经 setter 装配，本批不动（F6）。 |
| `MainAgentTurnService.processing_status` | 不在本批三类内；其直接测试构造有 3 处依赖 `None`。 |

### 2.5 测试

`tests/conftest.py::build_harness` 改为显式提供全部必填依赖，均保持原 `None` 分支的可观察行为：

- 生产同类对象：`MemoryWorker`（与原 processor 内部兜底同参）、`ASRService(provider=None)`（有语音时仍得 `not_configured`）、`PromptComposer(settings)`、`memory_context` 复用 `AgentToolService` 已构造的同一实例（与生产共享 persistence.memory_context 一致）、`AutomationModule(...).build()`（worker 不启动）、无绑定的 `DirectCommandRouter`、插件系统关闭的 `PluginCommandAdapter`、`PluginAdmissionSignalAdapter`、空 `PluginEventBus`、`ModelInvocationRepository`。
- 测试 fake（保持“不存在”语义）：`conversation_media`（原样返回 message、不提交）、`emoji_library`（不提交）、`client_links`（不识别 link）、`_AbsentGroupRecovery`（不识别启用、不提示）、`processing_status`（`nullcontext`）、`_ImmediateOrdinaryInput`（无静默窗口，收到 ordinary attempt 即调度）。
- `test_admin_canonical_owner.py` 改从 `processor._chat._memories` 读（processor 不再持有 memories，harness 中是同一对象）。

## 3. 失败清单核对

| 编号 | 结论 |
| --- | --- |
| F1 | `python -c "import yuki.container"` 通过；构造 `ApplicationContainer` 的装配测试（feishu/sticker/dependency failure/current material/group route recovery）通过。 |
| F2 | 改为必填的依赖在 container 中的来源类型均非 Optional，mypy strict 通过；`self.vision` 为 Optional，保持可选。 |
| F3 | `context_assembler`、`event_publisher` 删除构造参数而非改必填，生产构造不变。 |
| F4 | `container.py` 同步删掉 4 个实参，装配测试通过。 |
| F5 | `ChatService._event_publisher` 仍允许构造期为 `None`；processor 构造时无条件注入，container 中 processor 构造后才注册 worker/启动 lifecycle。 |
| F6 | `set_participation`、`set_automation_tools`、`set_plugin_tools` 未动。 |
| F7–F16、F19–F24 | harness 按 §2.5 提供 fake 或生产等价对象，全量测试结果见 §5。 |
| F17、F18 | 保留可选，见 §2.4。 |
| F20 | 两处分支删除前已不可达，行为不变。 |
| F25–F27 | `PluginModule.build()` 无条件构造四个插件对象；`build_asr` 未配置也返回 `ASRService`；`AutomationModule.build()` 无条件构造；视觉保持可选。 |

## 4. 度量

构造参数（`ast` 统计 `__init__` 参数；“默认 None”为 `= None` 默认值个数，“可选类型”为注解含 `None` 的个数）：

| 类 | 参数 改前→改后 | 默认 None 改前→改后 | 可选类型 改前→改后 |
| --- | --- | --- | --- |
| `MessageProcessor` | 29 → 25 | 17 → 0 | 17 → 0 |
| `ChatService` | 22 → 20 | 10 → 2 | 12 → 4 |
| `CommandService` | 22 → 22 | 7 → 0 | 7 → 1 |
| `AutomationCommandHandler` | 2 → 2 | 0 → 0 | 1 → 0 |
| `ConversationCommands` | 10 → 10 | 0 → 0 | 3 → 1 |
| `ModelCommands` | 2 → 2 | 0 → 0 | 1 → 0 |

`ChatService` 剩余 2 个默认 None 为 `tool_artifacts`、`tool_receipts`；剩余可选类型另含 `participation_runtime_context` 的返回值注解等非依赖项。

删除的分支：`is None`/`is not None` 分支 22 处（processor 11、chat 2、command 侧 9），`x or 构造` / if-else 兜底构造 11 处（processor 5、chat 6），死参数 6 个（processor 4、chat 2），以及 processor 的 `set_event_publisher`。

行数：`src/` +76 −178（净 −102）；`tests/` +106 −6（harness 显式装配）。

## 5. 验证

本机 macOS（Darwin 27），均为本地验证；未在 CI 上运行。

- `uv sync --all-extras --managed-python`：通过。
- `uv run ruff check .`：通过。`ruff format --check` 报告 10 个未触及文件的既有格式差异，与本批无关。
- `uv run mypy --platform linux src`：698 个源文件无问题。
- `uv run python -c "import yuki.container"`：通过；构造完整 `ApplicationContainer` 的装配测试（`test_feishu_container_assembly`、`test_container_dependency_failure`、`test_sticker_container_assembly`、`test_group_route_recovery`、`test_current_material_main_contract`）62 项通过。
- 全量 `uv run pytest tests/unit tests/integration -q -m "not native_worker"`：4809 passed、16 failed、22 skipped、102 deselected。16 项失败把 `src/`、`tests/` 恢复为 `6581eced` 原文件后在同一 worktree 逐项重跑，16 项全部以相同方式失败，均为本机环境相关的既有失败：`test_release_platforms`×4（macOS `script` 无 `-c`）、`test_manager_deployment`×2、`test_workspace_copy_receipts`×2、`test_workspace_publish_versions`、`test_work_execution_receipt_regressions`、`test_terminal_environment`、`test_sticker_library`、`test_sticker_publication_recovery`、`test_shared_media_cache_mount`（`/proc/meminfo`、AF_UNIX 路径长度等）、`test_plugin_frozen_binding`、`test_file_copy_e2e`。
- `_ImmediateOrdinaryInput` 覆盖面：临时给该 fake 记录命中后跑全量（不改行为，记录代码未提交），全套只有 `test_participation_person_identity.py::test_native_telegram_participation_reads_real_internal_space_configuration` 一项进入 `ordinary_attempt` 路径，该测试不断言 processor 返回原因，结果仍为通过。其他 group_open/续接测试本来就自行注入真实 `AutonomousGroupService` 或 participation。
- 任务书 §5 列出的有头桌面套件需要 `YUKI_TEST_CHROMIUM` 与桌面环境镜像，本机未跑，留给 CI。

### 已知风险

- `_ImmediateOrdinaryInput` 与原 `autonomous is None` 分支不完全等价：原分支同步 promote 并返回实际结果；fake 把 attempt 交给后台 task，processor 返回 `ordinary_deferred`，task 不在测试结束时等待。今后新增依赖 harness 默认 autonomous 且断言 group_open 同步结果的测试，需要像 `test_auto_reply_merge` 一样注入真实服务并等待。
- `main_agent_backend.py` 中 `_tool_artifacts is not None`、`_tool_receipts is not None` 两处判断本批未动（文件属进行中的 PR）。
