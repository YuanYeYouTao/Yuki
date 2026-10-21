# #417/#418 统一 Compact 实施任务书

本文保留 `0119` 的历史设计与实施基线；自 `0130` 起，旧 Host 聊天/Work 双窗口及“自动 compact 只按硬容量触发”的说明由[现行会话 Compact 合同](conversation-compact.md)取代，以下原阶段事实不作为当前配置规则。

日期：2026-10-14。关联 [Issue #417](https://github.com/YuanYeYouTao/Yuki/issues/417)、[Issue #418](https://github.com/YuanYeYouTao/Yuki/issues/418)，设计来源为[Work 摘要退役、会话 Compact 与任务树设计审查报告](Yuki-Work摘要退役与会话Compact及任务树设计审查报告-2026-10-14.md)（下称“审查报告”），含附录 B。

**最高开发约束：禁止防御性编程。能通过删除或放宽条件解决的问题，不新增替代机制；不用调阈值、加开关或改名保留旧机制。** 身份、授权、真实来源、预算和执行回执按事实处理。共同规则见[开发约束](development-contract.md)。

**状态：任务书。** 本轮只调研当前源码并撰写本文，没有修改运行代码、迁移、测试、配置或现行合同文档，没有提交、推送、部署、调用付费模型或发送 QQ 消息。下文 T、M、V、R 各项均为待实施或待验收。

## 1. 范围与已定决策

两个 Issue 作为同一项工程实施，不分批上线半套链路：

1. **退役三套旧压缩。** 独立 Work 摘要（`AgentRunner._compact_work` 与 `WorkSession` 的 summary/staging/compact）、ordinary 摘要链（`ordinary_compaction`）和 Conversation Rollup 全链路一起删除。
2. **一个 compact 入口。** 手动与自动 compact 共用当前 Main 路径：当前 Provider/Profile/模型、同一系统说明、同一完整工具声明、原消息顺序，在合法边界追加可信 compact 指令。结果真正替换所覆盖的旧输入，原件留在账本与 journal。
3. **保存、等待、恢复不买摘要。** 保留共同公共历史、原任务事实、回执、预算、Work tree、任务切换和子结果回送。
4. **子 Agent 完整继承 Main 能力。** 按审查报告附录 B 删除专属白名单、执行拒绝、提示词禁令和递归方向错误，补齐执行依赖与来源恢复。

已由合同和讨论确定、实施时不再讨论的事项：

| 决定 | 依据 |
| --- | --- |
| 自动 compact 只按**真实请求容量**触发，删除维护水位、触发比例和目标比例 | #418 第 6 条；[开发约束 §4](development-contract.md#4-上下文与历史)“完整请求可用时不因经验水位暂停” |
| 成本控制使用已有的显式窗口配置 `CONTEXT_WINDOW_TOKENS` / `context.work_window_tokens`，不另设 compact 水位 | 窗口是运维声明的真实容量上限，不是经验阈值 |
| compact 请求沿用 Main 的 `max_output_tokens`、思考与温度设置，不再有独立 compact 输出上限 | 保持请求设置一致，删除 `compaction_output_tokens` |
| 公共历史只有一份，compact 检查点复用现有 `prompt_projections.selected_summary_*` 发布 | 已有 generation/source/epoch CAS，不新建表或协调器 |
| Work tree、`parent_work_id`、祖先预算、completion/wait 唤醒不改 | 审查报告 §6 |
| 不保留旧 Rollup 的兼容运行链；冻结历史迁移和 schema 快照保留 | 审查报告 §9.2 |

## 2. 当前基线

| 项目 | 值 |
| --- | --- |
| 分支 | `codex/417-418-session-compact`，工作树开始时 clean |
| 源码提交 | `2bc2b497`（docs: credit contributors for every 3.9.0 PR） |
| 迁移 head | `0118_index_pending_sandbox_runs`；本工程新迁移编号预定 `0119` |
| 审查报告源码基线 | `fb8823f5`，是当前 HEAD 的祖先 |

`fb8823f5..HEAD` 只合入 PR #346（删除执行时间上下文管线），触及 `chat.py`、`context_assembler.py`、`agent_runner.py`、`subagent_execution.py`、若干测试及 `tools/cache_probe_runtime.py`，改动量为 33 个文件、+124/−121。审查报告的结论仍成立，但行号有少量漂移。**下文行号全部按 `2bc2b497` 重新核对；实施前仍以符号为准再定位。**

### 2.1 重新核对后新增或修正的事实

| 编号 | 事实 | 位置 | 影响 |
| --- | --- | --- | --- |
| F01 | `_PreparedRequest.compacting` 恒为 `False`，`observe_response(compacting=)` 不读取它 | `services/turn_execution.py:115,793,1082,1090` | 是死参数，随 T05 删除；新 compact 不复活它，也不改成新的 fence 名称 |
| F02 | 公共历史已经有“检查点 + 冻结原文片段”的承载：`prompt_projections.selected_summary_text/coverage/kind/renderer`，`prepare_commit` 在原 writer 中核 generation、source revision、epoch、revision | `conversation/projections.py:123–430`、`services/history_projection.py:38–68,155–198` | 新的公共 compact 直接发布到这里，替代 Rollup 表 |
| F03 | 普通轮（无已接纳 Work）也持有中立 `WorkControl` 与 `WorkSession` journal；ordinary 压缩结果已回写 `control.session.transcript` | `services/turn_execution.py:1063–1064` | 审查报告 Q04 有现成答案：普通轮 compact 的响应与发布沿中立 session journal，不登记业务 Work |
| F04 | 工作者嵌套派生当前**必然失败**：`execute_subagent` 把 worker 的 `parent_id` 取成它自己的父，`SubagentRepository.start` 要求 `lease.work_id == parent_id`，抛 `subagent_not_owned` | `runtime/subagent_tools.py:142`、`runtime/subagent_repository.py:35–36` | 附录 B S05 不只是开放工具名，必须改 parent 选择 |
| F05 | `recover_execution_source` 只分派 automation、SELF 与 `user_message/autonomous_group`；其他 origin 抛 `not_a_message_task` | `services/execution_sources.py:351–378` | #417 样本的 `plugin_background` Work 若派子 Agent 无法冷恢复，属于 S07/S09 的必要补通 |
| F06 | Control Plane 对外暴露 Rollup 覆盖字段 `covered_through_event_id`、`uncovered_event_count` | `control_plane/query_types.py:801–850`、`persistence/control_query.py:1324–1326` | 审查报告未列出；随列退役一起删，属于 API 变更，需在升级说明列出 |
| F07 | `model_profiles.toml` 的任务路由按 `ModelTask(task_name)` 解析，旧文件里的 `conversation_compaction = ...` 在 enum 删除后会让启动抛 `ValueError` | `model_runtime/profiles.py:133`、`config/model_profiles.example.toml:52` | 升级必须处理，见 §7.4 与未决问题 U4 |
| F08 | `.env` 使用 `extra="ignore"`，旧 `CONVERSATION_ROLLUP_*` 行不会阻止启动；热配置存于 `runtime_config_overrides`，已有迁移按 key 删除退役项的先例 | `config.py:52–56`；`migrations/versions/0062`、`0063`、`0103`、`0107` | 环境变量只需删文档；热配置 key 由 0119 删除 |
| F09 | `WorkSourceGuard` 在 `selected_summary_text is None` 时读取 Rollup 表并把整行元组纳入指纹；经 `prepare_history` 的 Main 请求总会写入字符串（可为空串），只有绕过该函数的入口才会是 `None` | `runtime/work_source_guard.py:197–266`、`services/history_projection.py:233–245` | 影响面仅限少数入口的在途精确恢复，升级前需只读盘点，见 §7.3 |
| F10 | 新增 Rollup 消费者：`services/processor.py:42–43,324,368,431`、`services/work_resume.py:15`、`prompting/contracts.py:125`、`conversation/__init__.py:1`、`conversation/projections.py:31–32` 的 `REBUILD_REASONS` | 同左 | 并入 T09–T13 |

## 3. 问题根因

**根因一：压缩与“替换窗口”是分离的两步。** `AgentRunner._compact_work`（`services/agent_runner.py:274–392`）另建 tool-free 请求，把 `WorkSession.summary_source()`（`runtime/work_session.py:648–811`）从 `public_records()` 生成的 records 包冷读一遍；`WorkSession.compact()`（`:1259–1373`）随后用 `compaction_anchor` 重建请求，anchor 已含完整公共历史。被摘要的公共前缀没有退出下一次 Main，这就是 #417 的 552/549。

**根因二：触发由维护水位驱动，不看可替换的部分。** `TurnExecution.prepare_request`（`services/turn_execution.py:902–960`）以 `min(input_budget, compaction_window_tokens) × work_compaction_trigger_ratio` 为门槛，对整个请求估算。长公共历史 + 新短 Work 没有任何可替换的执行尾，仍会触发。ordinary 链（`:984–1081`）同理。

**根因三：公共历史的压缩是第三套独立系统。** Rollup 由后台 worker 用专用 system/user、空工具声明和 `ModelTask.CONVERSATION_COMPACTION` 生成（`conversation/rollup/service.py`），通过信号、job、lease、overlay、前台等待（`context_assembler.py:819–958`、`runtime/context_preparation.py:77–142`、`services/rollup_wakeup.py`）接入上下文。它与 Main 不共享请求前缀，又让 Work 为“等 Rollup”停泊（`runtime/work_repository.py:1282–1445`）。

三者都在 Main 之外重读历史；本工程用一个沿 Main 前缀的 compact 替换它们，并让结果实际占据被覆盖的位置。

## 4. 目标调用链

### 4.1 窗口布局

```text
普通 Main：  [system, tools] → H → R_current → 当前触发
Work 执行：  [system, tools] → H → W 材料 → 工具回合… → 新输入
H = 公共检查点 C_pub（可无） + 覆盖点之后的冻结公共原文
```

H 只有一份，来自 `prompt_projections`。R 是本次运行资料，只属当前激活。W 材料沿用 `_append_business_material`（`runtime/work_session.py:519–598`）。

### 4.2 两种覆盖范围，一个入口

compact 入口唯一：`TurnExecution.prepare_request` 中的一个函数（建议名 `compact_current`，由 `_compact_work` 与 `compact_ordinary` 的调用点合并而来）。它按**被覆盖内容的归属**选择切面，而不是另建两套服务：

| 范围 | 何时需要 | compact 请求 | 结果归属与去向 |
| --- | --- | --- | --- |
| **公共段 P** | H 本身使完整请求放不下；或手动 `/ai compact` | 当前 Main 请求按原顺序截到 H 的某个合法事件边界，之后追加 compact 指令。system、tools、native tools、模型、思考设置、`provider_session_id` 与 Main 相同；不含 R、触发和私有尾 | 只来自合法公共来源，作为新 projection epoch 的 `selected_summary_text`，`coverage` 为截断点事件 ID。新 Main 用 `C_pub + 覆盖点后原文` |
| **执行尾 E** | H 之后的私有执行尾使请求放不下 | 当前完整请求（含原 continuation/opaque 和已配对工具回合），在最后一个已配对边界追加 compact 指令，要求只整理 H 之后的任务进展 | 归原 owner：Work 写入该 Work 的 `WorkSession`；普通轮写入中立 session（F03）。新窗口为 `[system, tools] → H → C_exec → 保留的近期原文/未观察回执 → 新输入` |

这样 H 在 E 型 compact 前后保持同一前缀，缓存位置不变；P 型 compact 才真正替换 H。#417 的“长公共史 + 新短 Work”没有执行尾，不会走 E；若 H 自身放得下，什么也不发生；放不下时走 P，并且被概括的原文确实退出。

两种范围都不是“冷读 records 包”：请求从 Main 的实际前缀出发，不改装成单个 user 资料，不另建汇总 Agent，也不清空工具声明。

### 4.3 触发与切点

- **自动触发**：`estimate_request_tokens(capacity_request(main_request)) > input_budget`，其中 `input_budget = capacity.input_budget(window, output_tokens=llm.max_output_tokens)`，`window` 取 `context.window_tokens` 或 `context.work_window_tokens`。这是现有判断（`turn_execution.py:893–900`），删除其后的维护门槛即可。
- **切点**：在 compact 请求自身能放入 `input_budget` 的前提下，选最靠后的合法边界。公共段的合法边界是冻结片段之间（`FrozenFragments` 已按事件分组）；执行尾的合法边界是 assistant 与其全部工具结果配对之后、或原生 `pause_turn` 检查点之外。切点之后的原文作为“近期原文”原样保留，不要求 compact 结果短于固定比例。
- **先 P 后 E**：若 H 占满容量，先做 P；仍放不下再做 E。两者都无法让请求放进容量时，按现有 `WorkCapacityError("model_request_capacity")` 结束，不回退到任何旧摘要。
- **手动**：`/ai compact [重点]` 进入同一入口，范围为当前会话的公共段 P（见 U3）。

### 4.4 compact 的执行、持久化与发布

1. compact 请求走现有 `_PrimaryDispatch` 路径：同一会话的 `run_llm(conversation_key)` 槽位、`reserve_request()`、`before_model_request` 来源校验、`WorkJournal` 的 `dispatched → response` 阶段。不再有 `_WorkSummaryDispatch` / `_OrdinarySummaryDispatch` 这类“不发布 journal”的旁路。
2. 会话暂停由现有 per-conversation 槽位实现：compact 占用该会话的 Main 槽位，期间新消息正常落账、按内部事件顺序在 compact 后接续；其他会话不受影响。不新增暂停状态或锁。
3. 响应写入 journal 后，再在一个短写事务中发布：
   - P：`PromptProjectionRepository.prepare_commit(rebuild_reason="compact", selected_summary_*=…)`，原 CAS 核 generation、source revision、epoch；同事务核 `ExecutionTraceStateModel.privacy_generation`（与 `WorkJournal` 的 `compaction_versions` 同一套检查，`runtime/work_journal.py:451–466`）。
   - E：`WorkSession.save("paired", compaction_versions=…)`，新 transcript 链写入 `chain_links`，原链存为 `previous_protocol_ref`（沿用 `compact()` 现有的 `journal.objects.put` 做法）。
4. 发布冲突（来源、隐私代次、generation 变化）时不重买：若冲突是“新消息追加”，P 的覆盖点仍有效，按新 epoch 重备同一响应；若是真实删除、隐私清退或 generation 变化，丢弃该 compact 结果，按原合同返回来源变化，不把失效内容带回。
5. 冷恢复：journal 处于 `response` 且 progress 标记本次为 compact 时，用已存响应重做第 3 步，不再派发；处于 `dispatched` 而无响应时按现有“未知已付”处理，不盲发。

### 4.5 业务恢复与 Work 切换

- W1 暂停后恢复：`rebase_business()` 继续使用 `H_now + work_current_material + 未观察回执`（`runtime/work_session.py:400–447`）。W1 最近一次 E 型 compact 的 `C_exec` 作为 W1 私有材料的一部分放入 `work_current_material`（见 U2）；不注入整份旧窗口。
- W1 → W2 → W1：`handoff`、`queue`、`derive`、completion/wait 唤醒保持原样，不因 compact 改父子关系或预算。
- 子任务恢复：worker 沿同一入口执行 E 型 compact；`compaction_brief` 与 anchor 机制删除，子 Agent 的固定前缀就是它自己的 system、tools 和 brief。

### 4.6 子 Agent 与主 Agent 共用一套能力合同

worker 使用 `MainAgentContract` 已冻结的完整声明、同一 direct/Code API、同一 `MainAgentBackend` 执行路径与核心系统说明；子目标、关系与 cwd 只作为 brief 资料。执行时按 worker 自己的真实来源、target 与授权核验，费用按唯一 model invocation 记一次，预算沿 `work_budget` 累计到根。

## 5. 施工项（按依赖顺序）

每项列出文件/符号、删改内容、下游影响和验收方式。`S/` 表示 `src/yuki/`。前一阶段完成并通过对应定向测试后再进入下一阶段；中间提交可以不上线，但不能留下同时运行两套压缩的状态。

### 阶段 A：建立唯一 compact 入口（先建后删，保证 Main 可用）

**T01 统一触发判断，删除维护水位**
- 文件/符号：`S/services/turn_execution.py` `TurnExecution.prepare_request` 的 `maintenance_budget`、`compaction_threshold`、`ordinary_threshold`、`ordinary_maintenance`、`compaction_request_tokens` 相关分支（`:902–1007`）。
- 删改：只保留 `predicted_tokens > input_budget` 一个触发条件，加手动请求入口参数。删除 `progress["compaction_request_tokens"]` 的写入与读取（还在 `runtime/work_session.py:444,1352`）。
- 下游：`WorkSession.rebase_business` 不再 pop 该键；`AgentRuntime` 无变化。
- 验收：V01 中新短 Work + 长 H 但请求可容纳时，物理请求只有 Main。

**T02 实现 `compact_current`（执行尾 E）**
- 文件/符号：`S/services/turn_execution.py`（新函数取代 `:933–983` 与 `:1007–1079` 两个分支）；复用 `_PrimaryDispatch`、`TurnTranscript`、`WorkSession.save`、`journal.objects.put`。
- 删改：在最后一个已配对边界追加一条可信 compact 指令（固定文本，放 `prompting/contracts.py` 旁的模块常量即可），以当前请求派发；响应正文为 `C_exec`。新 transcript = 原初始前缀（system + H + R/W 材料）+ `{"kind":"context_compaction","summary":C_exec,"covered":…,"previous_protocol_ref":…}` + 切点后原文 + 未观察回执。compact 响应若含工具调用，不执行，按“本次 compact 无结果”处理：请求仍放得下就继续原轮，放不下按容量错误结束。
- 下游：`observe_response` 不把 compact 响应计入 `model_observations` 与 `ordinary_observations`；`provider_pause_replay` 期间不 compact（原协议检查点优先）。
- 验收：V02、V08、V09。

**T03 实现公共段 P 与发布**
- 文件/符号：`S/services/main_agent_turns.py`（`:120–175` 的 planning/maintenance budget）、`S/services/history_projection.py` `prepare_history`/`PreparedHistory.prepare_commit`、`S/conversation/projections.py` `prepare_commit`、`REBUILD_REASONS`。
- 删改：`REBUILD_REASONS` 去掉 `rollup`、`rollup_ready`，加 `compact`。P 型 compact 请求取 Main 的 `[system, tools] + H 截断前缀 + 指令`，响应作为 `selected_summary_text`，`selected_summary_kind="compact"`，`selected_summary_coverage` 为切点事件 ID，通过新 epoch 发布。`prepare_history` 中“ready_rollup”判断删除；保留“已选检查点 + 覆盖后缺失原文”的扩展逻辑（`:158–192`），它正是新窗口的读取方式。`SUMMARY_RENDERER_VERSION` 升到 2，旧 Rollup 检查点按 §7.2 迁移后改写为新 kind。
- 下游：`AssembledContext.rollup_text` 改名为 `checkpoint_text`（随 T09 一起删除 Rollup 字段时统一处理，不另做别名）；`PromptComposer` 用现有 renderer 位置渲染检查点。
- 验收：V01、V03、V10。

**T04 手动 `/ai compact`**
- 文件/符号：`S/services/policies.py` `CommandName` 增加 `COMPACT`；`S/services/command_service.py` `execute`、`may_write`；入口在 `S/services/chat.py` 的命令分派处转入 Main 路径（命令服务本身不调用模型）。
- 删改：群聊需超级管理员（与 `NEW` 相同的现有授权分支），私聊允许本人。参数作为“compact 重点”拼入同一指令。执行时占用该会话 Main 槽位，结果回执用现有命令回复，不发公共聊天正文。
- 下游：`/ai help` 文案；WebUI 若要按钮，复用 Control API 的现有命令/动作通道，本工程不新做页面。
- 验收：V11。

**T05 删除死参数与旧 dispatch 包装**
- `S/services/turn_execution.py`：`_PreparedRequest.compacting`、`observe_response(compacting=)`、`_OrdinarySummaryDispatch`（`:119–168`）、`TurnState.ordinary_compaction_tokens`、`ordinary_observations`、`ordinary_evidence`（若仅 ordinary 摘要消费）、`deferred_paid_compaction`、`paid_staging` 及 `settle_final`/`:1357–1359` 的 `retire_paid_compaction` 调用。
- `S/services/agent_runner.py`：`_WorkSummaryDispatch`（`:199–221`）、`_compact_work`（`:274–392`）、`AgentRuntime.compaction_brief`（`:135`）。
- 验收：`rg "_compact_work|_WorkSummaryDispatch|_OrdinarySummaryDispatch|compacting=" src` 无现行消费者。

### 阶段 B：公共历史改接 projection 检查点，拆除 Rollup 读链

**T06 迁出 `ConversationScopeRepository` 与提示快照读取**
- 文件/符号：`S/conversation/rollup/repository.py:494–518` 的 `ConversationScopeRepository`、`:290–372` 的 `_load_prompt_tail_events`/`_load_bounded_prompt_tail`、`:698–782` 的 `load_prompt_snapshot`、`ConversationScopeState`（`rollup/models.py`）。
- 删改：移到 `S/conversation/scope_repository.py`（或并入 `hydrate.py` 所在的身份域），`load_prompt_snapshot` 只读 `canonical_conversations` 与 projection 检查点覆盖点之后的事件，删除 semantic/overlay/coverage 分支和 `rollup_stamp`。
- 下游：`S/services/work_resume.py:15`、`S/automation/worker.py:20`、`S/application/modules/automation.py:15`、`S/plugin_host/background_turns.py:15–16`、`S/services/processor.py:42–43,324,368,431` 改 import；`ConversationCoverageError` 移到同一模块（`rollup/errors.py` 退役）。
- 验收：身份、generation、reset 相关现有测试通过；`rg "conversation.rollup" src` 只剩待删文件。

**T07 改写 `ContextAssembler`**
- 文件/符号：`S/services/context_assembler.py`：import（`:15–19,40`）、构造参数 `rollup_repository/rollup_service`（`:142–150`）、`_load_history_snapshot`（`:712–751`）、`_ensure_uncovered_fits_budget`（`:819–958`）、`_ensure_turn_generation`、`_uncovered_tokens`、插件上下文（`:279–355`）、`AssembledContext`/`ContextMetrics` 的 `rollup_*` 字段。
- 删改：不再等待或驱动任何覆盖；历史放不下时交给 Main 的 P 型 compact，而不是在装配阶段阻塞。插件上下文的“requires explicit compaction”错误改为同一容量错误路径。
- 下游：`S/services/prompt_composer.py:13,138–159`、`S/services/main_agent_turns.py:164–175`、`S/services/history_projection.py`、`S/runtime/work_source_guard.py`。
- 验收：V03、V06；插件上下文的读取授权测试保持。

**T08 删除前台等待与 Work 停泊**
- 文件/符号：`S/runtime/context_preparation.py` 的 `ContextRollupRequired`、`prepare_context` 中的 defer/finish 分支（`:77–142`），保留 `select_protocol_recovery`（`:41–74`）与 `ContextPreparationMode` 中仍有消费者的部分（FALLBACK 若只服务 Rollup 一并删除）；`S/runtime/work_repository.py` `defer_context_rollup`/`finish_context_rollup`/`wake_context_rollups`（`:1282–1445`）；`S/runtime/work_scheduler.py:57,214–215`；`S/services/rollup_wakeup.py` 整文件；`S/services/chat.py:316–318,744–807` 的 wakeup ticket；`S/services/autonomous_groups.py:96` `consume_rollup_history`；`S/services/turn_coordinator.py:29` 的 rollup 恢复分支。
- 下游：`prepare_context` 的调用者（Chat、SELF、automation、插件、SDK）不再接收 `WorkInputsPreparing("work_context_preparing")`。
- 验收：V06、V12；`rg "context_rollup|rollup_wakeup|ContextRollupRequired" src` 为零。

**T09 改写 `WorkSourceGuard`**
- 文件/符号：`S/runtime/work_source_guard.py:15–20,77,197–266`。
- 删改：summary 组件只取 `version.selected_summary_text`（冻结读取的检查点）；删除 semantic/overlay 读取与 `rollup_stamp`。在途旧指纹按 §7.3 处理，不放宽比较。
- 验收：`tests/unit/test_work_source_guard.py`、`test_work_journal_source_retry.py` 更新后通过；V10。

### 阶段 C：删除 Work 与 ordinary 摘要链

**T10 WorkSession 摘要/staging/anchor**
- 文件/符号：`S/runtime/work_session.py`：`compaction_anchor`、`_compaction_source`（`:88–90`）、`restore(compaction_brief=)` 与 `_compaction_anchor`/`_decode_compaction_anchor`（`:117–170,280–283,1656–1707`）、`public_records`/`summary_source`/`_source_page`/`compaction_ready_summary`/`_compaction_guard`/`stage_compaction`/`validate_compaction_source`/`next_summary_source`/`compact`/`require_compaction_anchor`/`retire_paid_compaction`（`:633–1431`）、`_save` 中的 `compaction_anchor` 写入（`:1601–1603`）、`restore` 与 `rebase_business` 中的 `compaction_staging` 条件（`:340–360,411`）。
- 保留：`task_inputs`、`compaction_evidence`（改名 `execution_evidence`，它被 `_append_business_material` 使用）、`_unobserved_tool_round`、`rebase_business`、`_append_business_material`、`task_material` 读取（旧 Work 的已付整理事实仍有效，见 §7.2）。
- 验收：V04、V09。

**T11 `work_compaction.py` 与 `ordinary_compaction.py`**
- 删 `S/services/ordinary_compaction.py` 整文件。
- `S/runtime/work_compaction.py`：删 `CompactionSummary`、`validate_summary`、`summary_json_text` 等只服务旧摘要的符号；`SourcedFact` 仍被 `S/runtime/work_context_note.py:11,17–21` 使用，移入 `work_context_note.py` 后删除原文件。
- 验收：`rg "work_compaction|ordinary_compaction|validate_summary|CompactionSummary" src` 为零。

**T12 子 Agent 的 compaction_brief**
- `S/services/subagent_execution.py:335` 的 `compaction_brief=brief_message`、`S/services/main_agent_turns.py:507–510` 的 brief 推断一并删除（随 T05、T10）。

### 阶段 D：拆除 Rollup 写链、装配、配置与运维面

**T13 入账写链**
- `S/conversation/canonical_rollup.py` 整文件；`S/persistence/scoped_event_uow.py` 的 signal/counter/drain/notifier（审查报告 §9.1 D03 所列，当前在 `:94,305–349,360–418,1041–1043` 附近）；`S/identity/canonical_uow.py` 中的 rollup 计数；`S/persistence/event_repository.py`、`database.py:82`、`repository_helpers.py:27` 中仅为 Rollup 的字段；`S/persistence/rollup_holds.py` 只删 coverage hold，插件来源 hold 若有真实消费者则保留在原文件改名前先核对。
- 下游：`S/conversation/hydrate.py`、`S/persistence/people_repository.py`、`S/identity/canonical_repository.py`、`S/conversation/offline_recount.py`（整文件退役，CLI 子命令一并删）。
- 验收：入账、撤回、reset、隐私删除测试通过；入账事务里不再写 Rollup 表。

**T14 装配与生命周期**
- `S/application/modules/conversation.py`（Rollup service/worker/notifier/并发/轮询/health）、`S/application/modules/persistence.py:114–143` 的 `RollupPolicyConfig` 构造、`S/container.py` 中的 `conversation_rollup_worker` 等字段、`S/application/modules/model_runtime.py:83` 的 `compaction_timeout_seconds`、`S/services/chat.py:31–36,246–298` 的构造参数。
- 保留：Memory `EvidenceCompactionWorker`（不同功能，不按名删）。
- 验收：应用启动不再构造 Rollup 对象；`health` 与 control health 无 rollup 键。

**T15 模型任务与 Profile**
- `S/model_runtime/models.py:27` `ModelTask.CONVERSATION_COMPACTION`、`profiles.py:95`、`executor.py:541`、`S/deployment_setup/service.py:50`、`config/model_profiles.example.toml:52`、`config/model_profiles.providers.example.toml:70`。
- 旧配置文件处理见 §7.4 与 U4。历史 usage 记录中的 `conversation_compaction` 字符串保留。

**T16 配置**
- `S/config.py:174–180,312–325`、`S/settings_domains.py:101–107`（及同文件 `:131–144` 的 Rollup 字段）、`S/admin/models.py:160–168`（删 `compaction_trigger_ratio`、`work_compaction_trigger_ratio`、`work_compaction_target_ratio`、`compaction_output_tokens`、`compaction_window_tokens`；保留 `window_tokens`、`work_window_tokens`）、`S/admin/config_specs_hot.py:17–25,76–138`、`S/admin/config_service.py:1373–1391`、`S/services/chat.py:345–388` 的 maintenance 预算、`.env.example:89,95–99,228–250`、`frontend/src/config-files.tsx:41`。
- 验收：前端 `npm test && npm run build`；热配置页面不显示已删 key；V14。

**T17 命令、CLI、health、Control API**
- `S/services/command_service.py:19–20,67,90,216,245–258`（`/ai status` 改为显示最近一次 compact 的覆盖点与时间，数据取自 projection，不再显示 job 队列）；`S/cli.py:23,477`；`S/health.py:53,74,126`；`S/application/control_health.py:61–63`；`S/control_plane/query_types.py:801–850` 与 `S/persistence/control_query.py:1324–1326` 删 `covered_through_event_id`、`uncovered_event_count`（F06）。
- 验收：Control API 契约测试更新；升级说明列出字段删除。

**T18 删除 `conversation/rollup/` 包**
- `coverage.py`、`errors.py`（`ConversationCoverageError` 已在 T06 迁出）、`holds.py`、`metrics.py`、`models.py`、`origins.py`、`prompt_accounting.py`（`prompt_visible_event_count` 若仍被 `context_assembler` 使用，迁到 `event_prompt` 旁）、`renderer.py`（检查点渲染迁到 `prompt_composer`）、`repository.py`、`service.py`、`signals.py`、`summary.py`、`worker.py`、`__init__.py`。
- 文字残留：`S/prompting/contracts.py:125` 的 “History/Rollup” 改为 “History/compact 检查点”；`S/conversation/__init__.py:1` docstring。
- 验收：`rg -i rollup src` 只剩冻结 schema 快照模块（`projection_*_schema.py`、`observation_schema.py`）中的历史常量。

### 阶段 E：数据库迁移

**T19 迁移 `0119_retire_rollup_unify_compact`**（细节见 §7）
- 删除三张 Rollup 表、`canonical_conversations` 的 `covered_through_event_id`、`uncovered_event_count`、`uncovered_character_count` 及引用它们的 check constraint（SQLite 需 `batch_alter_table` 重建）、两个 Rollup owner trigger；0107 的两个 Rollup 索引随 job 表删除。
- 迁移前把有效旧检查点写入 `prompt_projections`；清理 Work checkpoint 的 `$.context_rollup` 并重新排队；删除已退役热配置 key。
- 同步当前安装器：`S/conversation/canonical_db_models.py:56–59,305–438`、`S/persistence/schema_guard.py:13–14,113`、`S/conversation/projection_models.py:10` 改用新的 trigger 集（新建 `PROJECTION_TRIGGERS_0119`，旧 `0113` 常量保留给历史迁移）。
- 验收：V13（旧库升级）、`alembic upgrade head` 从 0048 基线与 0118 实库副本各跑一次。

### 阶段 F：子 Agent 完整能力继承（附录 B S01–S10）

**T20 声明与 API 共用 Main 合同（S01）**
- `S/runtime/subagent_tools.py:45–72` 删 `WORKER_NAMES`、`WORKER_OPTIONAL_NAMES`、`WORKER_REQUIRED_NAMES`；`S/services/subagent_execution.py:83–118` `required_names`/`prepare` 改为直接使用 `MainAgentContract` 的已冻结 definitions 与 `script_api`，删除 worker 专用 `project()` 与 `incomplete_worker_tool_manifest`；`:300–306` 不再传子集 ceiling。
- 验收：Main 新增一个工具后，worker 不改代码即可声明并执行。

**T21 执行与控制降级（S02）**
- `S/services/main_agent_backend.py:139–178` `work_control_allowed`/`work_query_allowed` 的 child 分支、`:271–276` 与 `:696–713` 的 `worker_tool_not_declared`；`S/services/agent_runner.py:771–845,986–990,1089` 的 worker Code Mode 分支；`S/codemode/tool_visibility.py`（审查报告 B2 所列 worker 目录分支）；`S/runtime/work_queries.py`（child 查询缩减）。
- 保留：`S/runtime/work_control.py:728–730` 的 “已登记 Work 不重复 accept”。`main_agent_backend.py:539,563,592` 的 `lease.work_id` 判断逐一核对：SELF 自报告、participation 证据属于“当前激活是否主会话轮”的事实判断，不是能力限制，按事实保留或改写。
- 验收：同一来源下同一合法操作，主子沿同一后端得到同一结果。

**T22 执行依赖与权限（S03、S04）**
- `S/services/subagent_execution.py:273–306` 的 `ToolRuntime`：`gateway`、`allow_generic_onebot`、`actor_is_superuser` 按 `S/services/work_resume.py:289–296` 与 `S/services/chat.py` 普通 Main 的同一绑定方式，从 worker 自己恢复的来源取得；Memory、automation、short_state 走 Main 相同入口，记录 worker 自己的 execution_id 与回执。
- 验收：V15；不把所有子节点提升为超级用户。

**T23 提示词共用（S06）**
- `S/runtime/subagent_tools.py:18–41` 删除 `_WORKER_BASE` 的永久禁令（“QQ 发送与长期记忆变更交给主 Yuki”等），worker 的 system 使用 `PromptComposer.static_messages()` 的同一核心说明，brief 只表达子目标、关系、cwd 与验收项。
- 验收：worker 首个请求的 system 与 Main 一致，差异只在 brief。

**T24 嵌套 parent/方向（S05，F04）**
- `S/runtime/subagent_tools.py:125–225` `execute_subagent`：`subagent_start` 的 parent 取当前 Work 自己（`control.current["id"]`）；`subagent_message` 根据 `child_id` 是否给出决定“向父提问”或“向自己的子目标补充”；`subagent_control` 对自己的后代开放。`S/runtime/subagent_repository.py:390–393` `message` 的方向改由调用者传入的真实 sender/target 决定，仍核 lease、conversation、generation。
- 验收：V16（主 → 子 → 孙）。

**T25 来源冷恢复（S07–S09，F05）**
- `S/services/execution_sources.py:351–378` 补齐 `plugin_background` 等现有 Work origin 的 reader，沿其原 owner（插件后台 job/SDK 调用记录）核验，不伪造入站消息。逐个 origin 核对 `TurnOrigin` 成员，未有 Work 来源的 origin 不补。
- 验收：V16 的插件/SDK 来源分支。

### 阶段 G：文档、测试、工具与 CI

**T26 现行合同与用户文档**
- `docs/architecture/development-contract.md`：§2 “工作者不继承主 Yuki 的发送、长期记忆写入和自动化权限”改为与附录 B 一致；§4 “账本、Rollup、Memory…”改为 compact 检查点。
- `docs/architecture/conversation-rollup.md`：改写为“会话 Compact”现行合同（建议改名 `conversation-compact.md` 并更新所有链接）。
- `docs/architecture/main-agent-runtime.md`、`model-providers.md`、`canonical-runtime.md`、`persistent-subagents.md`、`docs/architecture/README.md` 索引、`README.md`、`README.en.md`、`docs/help.md`、`docs/upgrade-3.9.0.md`（或下一版本升级指南）。
- 日期报告与 `docs/operations/*` 历史记录按基线保留，不改写。
- 验收：`rg -i "rollup" docs/architecture/*.md README*.md docs/help.md` 只剩历史任务书、日期报告与“已退役”说明。

**T27 测试**：见 §8 的覆盖要求与 §6.4 的文件清单。

**T28 工具与脚本**
- `tools/cache_probe.py`、`tools/cache_probe_runtime.py`、`tools/cache_probe.md`、`tests/unit/test_cache_probe.py`：改为对当前真实入口与最终 payload 的重放，或整体删除；不保留构造专用摘要请求的路径。
- `scripts/context_cost_simulation.py`：删除 Rollup 假设或整体删除。
- CI：`.github/workflows/quality.yml` 无 Rollup 专属 job，保持原样，不加缓存命中门槛。

## 6. 配套处理汇总

### 6.1 装配

`ConversationModule`、`PersistenceModule`、`ModelRuntimeModule`、`AutomationModule`、`container.py`、`ChatService` 构造参数、`MessageProcessor`、`SubagentExecutionDependencies`。删除后用一次全应用启动测试确认没有悬空依赖（T14、T20）。

### 6.2 配置

| 类别 | 删除 | 保留 |
| --- | --- | --- |
| 环境变量 | `CONVERSATION_ROLLUP_*` 全部、`CONTEXT_COMPACTION_WINDOW_TOKENS`、`WORK_COMPACTION_MAX_OUTPUT_TOKENS`、`WORK_COMPACTION_TRIGGER_RATIO`、`WORK_COMPACTION_TARGET_RATIO`、Rollup trigger/target ratio | `CONTEXT_WINDOW_TOKENS`、Work 窗口、`MEMORY_EVIDENCE_COMPACTION_*`、`SUBAGENTS_ENABLED` 与并发 |
| 热配置 key | `context.compaction_window_tokens`、`context.compaction_trigger_ratio`、`context.compaction_target_ratio`、`context.work_compaction_trigger_ratio`、`context.work_compaction_target_ratio`、`context.compaction_output_tokens`、`context.rollup_output_tokens`、`context.rollup_summary_characters`（以实施时 `config_specs_hot.py` 的全部 Rollup/compact spec 为准） | `context.window_tokens`、`context.work_window_tokens` |
| Profile 路由 | `conversation_compaction` | 其余任务 |

### 6.3 API / UI

- `/ai status` 输出改为 compact 检查点信息；新增 `/ai compact`。
- Control API `ConversationView` 删两个覆盖字段（F06）；health 删 `conversation_rollup`。
- WebUI 配置页随热配置 spec 自动减少；`frontend/src/config-files.tsx:41` 删 Rollup 文案。不新增页面。

### 6.4 测试文件清单（当前仓库实际存在）

| 分组 | 文件 | 处理 |
| --- | --- | --- |
| 共享 fixture | `tests/conftest.py`、`tests/support/conversation_rollup_370_helpers.py`、`tests/unit/rollup_test_helpers.py`、`tests/support/work_compaction.py`、`tests/support/work_compaction_capacity_helpers.py`、`tests/support/work_effect_results_helpers.py`、`tests/support/social_identity_cases.py` | 删 Rollup/摘要 fixture；容量 helper 改为真实 compact |
| 只证明旧实现 | `tests/unit/test_rollup_complete_sources.py`、`test_rollup_commit_snapshot.py`、`test_work_compaction_public_delta.py` | 删除；其中真实来源/CAS/SQLITE_BUSY 不重购的断言迁到 compact 发布测试 |
| 改写 | `test_work_compaction_anchor_recovery.py`、`test_work_protocol_continuity.py`、`test_protocol_recovery_preparation.py`、`test_provider_native_result_boundaries.py`、`test_work_source_guard.py`、`test_work_journal_source_retry.py`、`test_work_scope_contention.py`、`test_history_time_projection.py`、`test_runtime_config_read_snapshot.py` | 保留签名、配对、来源、预算、恢复断言，改掉 Rollup 耦合 |
| 入口/集成 | `tests/integration/test_current_material_main_contract.py`、`test_provider_session_auxiliary.py`、`test_self_common_capabilities.py` | 断言“无辅助摘要请求”与 compact 请求前缀一致 |
| 只改 import/fixture | `test_canonical_ingress.py`、`test_derived_text_transactions.py`、`test_external_event_prompt_isolation.py`、`test_external_event_runtime_fences.py`、`test_history_missing_body.py`、`test_material_observation_storage.py`、`test_migration_0110.py`、`test_plugin_reply_causality.py`、`test_memory_partition.py`、`test_event_id_history.py`、`test_job_claim_transactions.py` | 去掉覆盖列与 Rollup 依赖 |
| 子 Agent | `tests/integration/test_scheduled_subagent_recovery.py:396–397`、`tests/unit/test_self_initiative_runtime.py:484–561` | 改为主子能力一致；`test_subagent_result_checkpoint.py`、`test_worker_hot_limits.py`、`tests/support/subagents_helpers.py` 保留并适配 |
| 新增 | compact 发布/冷恢复、0119 迁移、嵌套子 Agent | 见 §8 |

## 7. 旧数据库升级、有效 Work、compact 发布与冷恢复

### 7.1 升级原则

向前迁移，原地保留账本、Work、journal、回执、预算与付费对象。不使用 `/ai new`、清库、取消 Work 或从备份恢复代替升级。迁移只用 SQL/Core，不 import 可变 ORM 或运行时函数。

### 7.2 0119 的数据步骤（按顺序）

1. **旧公共检查点转存。** 对每个 conversation，若 `prompt_projections` 的当前行 `selected_summary_text IS NULL` 而同 generation 的 `canonical_conversation_rollups` 有效（kind 为 model/extractive/migration），把其 `summary_text`、`covered_through_event_id` 写入 `selected_summary_text/coverage`，`selected_summary_kind` 保留原 kind，`selected_summary_renderer=1`，并把 `payload_json` 置为 `'[]'`、`invalidated_reason='compact'` 让下一次读取按新 epoch 重建覆盖点后的原文。emergency overlay 不转存（它是临时覆盖，下一次超容量时由 P 型 compact 取代）。已有 `selected_summary_text` 的 projection 不动。
2. **旧 Rollup 等待中的 Work。** `runtime_work` 中 `state='waiting_external'` 且 `json_extract(checkpoint_json,'$.context_rollup') IS NOT NULL` 的行：`json_remove` 该键，`state='queued'`、`reason='context_prepared'`、`revision+1`，与现有 `wake_context_rollups` 的效果相同。
3. **热配置。** `DELETE FROM runtime_config_overrides WHERE config_key IN (…)`，列表见 §6.2。
4. **结构。** drop 两个 owner trigger → drop 三张表（0107 建在 job 表上的 `ix_canonical_rollup_pending_due`/`ix_canonical_rollup_processing_due` 随表删除）→ batch 重建 `canonical_conversations` 去掉三列与 check constraint。
5. `downgrade()` 抛错，说明退役表不可重建（与 0117 一致）。

Work journal 里的 `compaction_staging`、`compaction_anchor`、`compaction_request_tokens` 不在迁移中改写：它们是已付记录的一部分。新代码不读这些键；`WorkSession` 在下一次 `save` 时只写自己维护的键，旧键随新记录自然消失，旧记录原样保留作证据。已存在于 Work transcript 中的 `explicit_context_compaction` 胶囊是普通历史消息，新 compact 可以把它们覆盖进去，不需要转换。`progress.task_material` 仍由 `_append_business_material` 读取，旧 Work 的已付整理结果继续可见。

### 7.3 在途精确恢复的来源指纹

T09 之后，`selected_summary_text IS NULL` 的已存 guard 若当时读取了 Rollup 行，指纹会对不上（F09），恢复走 `work_source_changed`。处理步骤：

1. 上线前对生产库副本只读执行：非终态 Work 中，最新 journal 的 `metadata.source_guard` 里 `read_version.selected_summary_text` 为 null、且该 conversation 存在 Rollup 行的数量。
2. 数量为零：无需额外处理，直接按 7.2 迁移。
3. 数量非零：这些记录的 Work 走业务恢复（`H_now` + 原任务材料 + 已存回执），它们的已付响应仍沿原 call key 读取，不重新购买。若其中有 `phase='dispatched'` 且无响应的记录，按现有未知已付处理。实施时把盘点结果写回本任务书，不为这种情况加兼容 reader。

### 7.4 旧 `model_profiles.toml`

已部署实例的 Profile 文件可能有 `conversation_compaction = "..."`，删除 enum 后启动失败（F07）。推荐默认见 U4：Profile 读取器对这一个已退役任务名记录一次 warning 并跳过，同时 `deployment_setup` 不再生成该行、升级指南要求删除。这属于真实升级 reader，不保留任何旧路由。

### 7.5 compact 发布与冷恢复

| 崩溃点 | 恢复行为 |
| --- | --- |
| compact 请求 `dispatched`，未取得响应 | 按现有未知已付处理：先按原 request/call ID 查，无可查回执则记未知，不自动重发 |
| 响应已存 journal，发布未提交 | 用已存响应重做发布（新 session、同计划、同 ID），不重买 |
| 发布事务提交结果不明 | 先读 projection epoch / journal 记录确认，再决定是否重备 |
| 发布遇来源追加 | P：覆盖点仍有效，换新 epoch 重备；E：新输入在新窗口尾部接续 |
| 发布遇删除、隐私清退、generation 变化 | 丢弃该 compact 结果，不发布；原 Work 按来源变化处理，已成功的外部效果和回执不受影响 |
| compact 后、下一次 Main 前崩溃 | 新窗口已持久，恢复直接使用，不再 compact |

## 8. 离线验证矩阵

全部用真实 Runner、真实 serializer 与 FakeProvider 记录物理请求，不以函数内部调用次数代替。

| 编号 | 场景 | 必须证明 |
| --- | --- | --- |
| V01 | 长公共史 + 新短 Work（#417 原样本形状）、请求可容纳 | 只有 Main 请求；无任何辅助/compact 请求；原目标与回执不丢 |
| V02 | 长工具尾（多轮 terminal/web 结果）达到真实容量 | E 型 compact 一次；compact 请求的 system/tools/前缀与上一 Main 逐字节一致；下一 Main 不含被覆盖的工具原文，含 `C_exec`、保留尾与未观察回执 |
| V03 | 长公共史本身超容量；不同说话人连续发言 | P 型 compact 一次；下一 Main 的 H 为 `C_pub + 覆盖点后原文`；较早公共事件的渲染不按当前说话人改变；R 位于正文上方且不进入 `C_pub` |
| V04 | W1 补充要求、独立 W2、W1 恢复 | W1 原 ID/来源/预算；恢复时为 `H_now + work_current_material（含最近 C_exec）`；无整份旧窗口 |
| V05 | 普通 derive、父等待 child、child 完成 | completion 回父 inbox、wait 唤醒、结果 revision 不变；无摘要门槛 |
| V06 | SELF、automation、GitHub/插件后台、SDK 各入口 | 各自真实合同与授权；无旧等待；需要时走同一 compact 入口 |
| V07 | compact 期间新消息、steer、迟到输入 | 新输入正常落账，compact 后按内部事件顺序接入一次；同会话 Main 不并发；其他会话不阻塞 |
| V08 | 工具 pending、原生搜索 `pause_turn`、媒体、Gemini 签名、Responses opaque | pending 时不 compact；原配对/签名/continuation 完整；不补造结果 |
| V09 | compact `dispatched` 后崩溃、`response` 后崩溃、发布冲突、提交不明 | 与 §7.5 一致；物理请求计数证明不重买 |
| V10 | 选源后删除消息、隐私删除、generation 变化、权限变化 | 失效内容不进入发布的检查点；原 Work 按来源变化处理 |
| V11 | `/ai compact` 私聊、群聊（管理员/非管理员）、空闲与有 Work 时 | 授权与 `NEW` 一致；结果为 P；不发公共正文；有 Work 时按 U3 的决定行为 |
| V12 | 旧 Rollup 等待中的 Work（迁移前后） | 0119 后 Work 被排队并继续；无 `work_context_preparing` |
| V13 | 0048 基线 → head、0118 实库副本 → head | 表/列/trigger/索引符合 §7.2；有效旧检查点转为 projection；`alembic check` 与 schema_guard 一致 |
| V14 | 全仓残余与配置 | §10 的检索清单只剩允许项；前端 build 通过；旧 `.env` 行不影响启动 |
| V15 | 子 Agent 执行完整能力 | 同来源下 `send_message`、`memory_change`、automation 创建、插件工具、`call_onebot_api`（真实超级用户来源才可）与 Main 结果一致；回执记在 worker 自己的 execution |
| V16 | 主 → 子 → 孙（message/SELF/automation/插件来源各一） | 孙的 `parent_work_id` 为子；子对孙 message/control 成功；孙向子提问；cancel 子树、重启恢复、unknown-paid 不重发；根预算只累计一次 |
| V17 | 子/孙长执行触发 E 型 compact | 使用同一入口；无 brief/anchor 依赖；恢复后继续原目标 |

定向测试通过后跑一次完整 `quality.yml` 等价检查：`ruff`、`mypy src`、前端 `npm test && npm run build`、`pytest -q`。

## 9. 后续真实验收（需单独授权）

这些项目不属于离线验证，不能用 FakeProvider、CI 或本地重放代替。每项按审查报告 §12.2 的字段记录。

| 编号 | 内容 | 证据 |
| --- | --- | --- |
| R01 | 真实模型路线：Gemini 经 AGM、DeepSeek Chat、DeepSeek Responses 中**实际启用**的连接，各做一次 E 型与 P 型 compact | 最终 Provider payload 与网关转出 payload 对照；usage 的 hit/miss/output；未启用路线记“未测” |
| R02 | 同路线下 #417 形状的自然短 Work | 物理请求数、usage、首响时延与旧基线对比 |
| R03 | 子 Agent 真实执行 QQ 发送、Memory 写入、automation 创建 | 在测试群/测试账号，按原回执核对一次送达，不重发 |
| R04 | 主 → 子 → 孙真实复杂任务 | 结果回送、预算累计与费用不重复 |
| R05 | 生产库副本升级演练，再部署 | 迁移耗时、0119 后在途 Work 继续情况、§7.3 盘点结果 |
| R06 | 上线后自然流量观察 | 按执行汇总 compact 次数、成本与失败样本，填审查报告 §12.3；不挑选样本，不填推测节省比例 |

不启动第二个生产 Bot，不制造 QQ 探针或缓存保温请求。

## 10. 完工检索清单

终审时在 `src/`、`config/`、`.env.example`、`frontend/src/`、`tools/`、`scripts/`、`tests/` 中检索，以下词只允许出现在冻结迁移、历史 schema 快照常量和历史文档中：

`rollup`、`Rollup`、`CONVERSATION_COMPACTION`、`conversation_compaction`、`_compact_work`、`compact_ordinary`、`summary_source`、`compaction_staging`、`compaction_anchor`、`compaction_brief`、`ContextRollupRequired`、`context_rollup`、`rollup_wakeup`、`selected-summary`、`emergency_overlay`、`covered_through_event_id`、`uncovered_event_count`、`WORKER_NAMES`、`worker_tool_not_declared`、`subagent_parent_control_only`、`compaction_trigger_ratio`、`compaction_window_tokens`、`compaction_output_tokens`。

检索归零不单独证明完成；还需 §8 的行为证据。

## 11. 未决设计问题

只列会改变实施结果、需要维护者拍板的问题。每项给出推荐默认，实施者在未获答复时按默认推进。

| 编号 | 问题 | 推荐默认 | 理由 |
| --- | --- | --- | --- |
| U1 | 公共段 P 的 compact 请求是否允许“截到 H 边界”，即不带 R、触发和私有尾 | **允许。** 它与 Main 共享 system/tools/H 前缀，只是不带后面的私有内容 | 带上私有尾的产物不能发布为公共检查点（审查报告 Q09）；另造冷读包又违反 #418。截断前缀是同时满足两者的唯一现有手段 |
| U2 | W1 业务恢复时是否带回最近一次 `C_exec` | **带回，放进 `work_current_material`。** | 指令要求 `C_exec` 只整理 H 之后的任务进展；它是 W1 自己的私有材料，不发布。若不带，长任务恢复后只能靠回执重读，后续轮次更多 |
| U3 | 有 Work 正在执行时，手动 `/ai compact` 是否也让该 Work 压缩执行尾 | **不。** 手动命令只做公共段 P；执行尾仍按真实容量自动处理 | 让命令打断运行中 Work 需要新的信号通道，属于新增机制；用户的直接需求是会话窗口变小 |
| U4 | 旧 `model_profiles.toml` 中 `conversation_compaction` 行 | **读取器跳过该退役名并 warning 一次**，生成器与示例删除该行 | 否则已部署实例升级后直接无法启动；跳过不保留任何旧路由 |
| U5 | 子 Agent 与主会话并行时能直接发 QQ 消息，是否接受同一群里两个执行体交错发言 | **接受。** 继承即包含发送；顺序由各自回执记录 | 用户要求“完整继承、无能力限制”；按发送方加串行锁属于新增限制 |

## 12. 建议施工顺序

1. **A 阶段（T01–T05）**：先让 compact 入口和真实容量触发可用，V01、V02、V08、V09 在旧 Rollup 仍存在时就能验证。
2. **B 阶段（T06–T09）**：公共历史改接 projection，删 Rollup 读链；V03、V10。
3. **C 阶段（T10–T12）**：删 Work/ordinary 摘要；V04、V05。
4. **D、E 阶段（T13–T19）**：删写链、装配、配置、运维面，写 0119；V06、V07、V11–V14。D 与 E 必须在同一提交序列里完成，避免出现“代码不写 Rollup 但表和 trigger 仍在”的中间上线状态。
5. **F 阶段（T20–T25）**：子 Agent 完整继承；依赖 A–C 的统一 compact 与恢复，V15–V17。
6. **G 阶段（T26–T28）**：文档、工具、测试收尾与全量检查。
7. 本地全部通过后，分别记录提交、PR、合并、镜像、迁移、部署与 §9 的真实验收；任何一步未做都按“未做”记录。
