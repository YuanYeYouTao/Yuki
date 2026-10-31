# Yuki 任务完成与子 Agent 提示词整理任务书

日期：2026-11-01，时间统一为 UTC+8。

状态：**D01–D07 已在独立工作树完成本地实施与定向验证。** §8 保留提交前本地验证快照，§9 记录 PR 提交准备；真实模型对照未执行，生产配置未修改。

## 1. 目标、基线与证据边界

本轮改善模型对任务完成、异步等待、父子任务接续及对外交付的理解。先删除范围不清的说法，再在最接近调用位置的工具说明中补齐必要用法，不靠堆叠重复系统规则解决问题。

说明更清楚，理论上可以提高模型选对工具和参数的概率；这不是已测得的收益，也不保证模型总会正确执行。实际完成通知能否触发模型，仍取决于现行 Work 输入、等待和恢复链。提示词不能代替调度机制。

### 1.1 源码基线

| 对象 | 固定版本与用途 |
| --- | --- |
| 事故生产源码 | `2a0ba7215f6a398e3b9bc7417ded18d49fc3ce12`；镜像 digest `sha256:e438a43ac3410dd095ab389b2c9a75c0dbf5282dd4a54ffd3ef6d3bd30860782`，以本次只读检查时为准 |
| 本次现行源码审查 | `93200d12a8b4584de932cf41c977f75d24fd917b`；当时位于 `codex/usage-ledger-query-20261101`，不在该会话工作树实施修改 |
| 本次实施基线 | `2a0ba7215f6a398e3b9bc7417ded18d49fc3ce12`；重新 fetch 后从当时的 `origin/main` 建立 `codex/task-prompt-20261101` 独立工作树，目标源码与上述审查版本一致 |
| 文档交付目录 | 用户原仓库 `D:/Code/My_Github_Repo/Yuki-QQbot/docs/architecture/`；该 checkout 仍为旧的 `25cd6083015924bf80405ca7c346f84a995f6ebb`，有既有未提交资料 |

已比较两份源码：`prompting/contracts.py`、`runtime/work_control.py`、`runtime/subagent_tools.py`、`runtime/subagent_repository.py`、`runtime/work_session.py`、`services/turn_execution.py` 在事故版本与现行审查版本之间没有差异。

**下文 `src/yuki/...` 路径、行号和拟议 diff 指现行审查版本，不指文档交付目录的旧源码。** 后续实施应从届时确认的现行基线建立独立工作树，重核差异；不能向旧 checkout 的 `src/qq_ai_bot/...` 直接套用本方案。

开发遵循[共同架构约束](development-contract.md)。涉及模块以实施基线的[主 Agent 合同](https://github.com/YuanYeYouTao/Yuki/blob/93200d12a8b4584de932cf41c977f75d24fd917b/docs/architecture/main-agent-runtime.md)和[持久工作者合同](https://github.com/YuanYeYouTao/Yuki/blob/93200d12a8b4584de932cf41c977f75d24fd917b/docs/architecture/persistent-subagents.md)为准；旧任务书与旧目录中的权限、预算、工具名称不覆盖现行实现。

### 1.2 本次事故已证实的事实

父 Work：`756dcd74-db34-4955-9d36-a5bd4a4b8248`；子 Work：`edc2d617-ae40-4244-9d33-aecf8e9a661c`。

| 时间 | 原始事实 |
| --- | --- |
| 01:14:18 | `subagent_start` 成功，返回原 `child_id`，状态 queued |
| 01:14:39 | 请求记录 `execution_trace_entries.id=377109` 包含当轮 system、29 项工具及原子任务回执 |
| 01:14:47–48 | 对应响应 `377113` 选择 `task_control(action=wait, conditions=[{kind:conversation}], wait_mode=any)`；等待没有 deadline |
| 01:24:46.164 | 子任务已完成，父输入 `2900` 保存 `worker-result:<child_id>:4` 及图片路径；普通完成通知没有打断父任务的有效等待 |
| 01:28:32 / 01:28:37.048 | 群里再次提及 Yuki，事件 `126062` 命中 conversation 等待；父输入 `2901` 保存原等待信号 |
| 01:29:02 | 主 Agent 在同次续跑中消费子结果与信号后显式 `send`；图文事件 `126063`，发送回执 succeeded，平台消息号 `567575985` |

事故当轮实际工具描述与审查版本一致，不只是依据文档推测。请求中已明确要求交互任务交付结果、外发使用 `send`，但没有解释 `child_id` 与等待参数的关系，也没有解释 active conversation wait 对子完成通知的影响。

这证明了模型登记了不合原目标的等待条件，以及完成通知未立即进入模型处理；**不能据此证明模型读到结果后选择静默，也不能据此估算换模型或改提示后的成功率。** 本次没有额外购买模型请求、主动发送 QQ 测试消息或验证图片画面。

## 2. Pi 与其他实现：可以学习什么

以下均为原始源码或官方说明的静态核查，没有运行上游 demo。外部实现只作设计与文案参考，不加入 Yuki 依赖。

| 来源 | 固定版本 / 原始依据 | 可学习的内容 | 不直接迁入的部分 |
| --- | --- | --- | --- |
| Pi core / coding agent | `earendil-works/pi@200387122ca450d6387f033949423114a270b96c`；[system-prompt.ts](https://github.com/earendil-works/pi/blob/200387122ca450d6387f033949423114a270b96c/packages/coding-agent/src/core/system-prompt.ts#L74)、[README](https://github.com/earendil-works/pi/blob/200387122ca450d6387f033949423114a270b96c/packages/coding-agent/README.md#L12) | 核心提示短，操作细节靠当前工具的说明与 guidelines；精简规则、就近解释用法 | 该版本默认不内置 subagents / plan mode；不能把提示短等同于已经有完整长任务产品语义 |
| Pi Durable | 同一官方仓库 `9fba660cf1caca0ade5bea72269352416e595a19`；[官方介绍](https://earendil.com/posts/pi-durable/)、[prompt 组装](https://github.com/earendil-works/pi/blob/9fba660cf1caca0ade5bea72269352416e595a19/packages/durable/src/harness/agent.ts#L196)、[后台子任务示例](https://github.com/earendil-works/pi/blob/9fba660cf1caca0ade5bea72269352416e595a19/packages/durable/test/examples/23-subagent-background.ts#L68) | 官方实验包确实面向持久任务；应用提供 sections / conversation instructions。示例的持久 Reporter 等待子结果，再提交内部 follow-up 输入，闲置时开新 turn，忙时排到当前回答之后 | 这是应用示例，不是统一“长任务万能提示词”。不迁入 Reporter / Chord，不在 Yuki 原 Work inputs 外新增第二队列；示例中的无需回复附言和取消政策不照搬 |
| Pi 第三方子任务扩展 | `nicobailon/pi-subagents@a37a28853822d470d15ae1764c97aed258087699`；[父任务行动提示](https://github.com/nicobailon/pi-subagents/blob/a37a28853822d470d15ae1764c97aed258087699/src/shared/parent-wake.ts#L2)、[等待工具](https://github.com/nicobailon/pi-subagents/blob/a37a28853822d470d15ae1764c97aed258087699/src/runs/background/wait-tool.ts#L18) | 完成消息直接解释父任务下一步：读取已有成果，继续原授权工作或交付；工具说明说清宿主的通知能力和等待参数 | 它依靠 triggerTurn 安排模型运行。静默后追加提醒、额外续跑和通知重试不属于本次文案整理 |
| OpenClaw | `a31d9039928ca8fc9055e25e940fd0a4136c51b4`；[完成指导](https://github.com/openclaw/openclaw/blob/a31d9039928ca8fc9055e25e940fd0a4136c51b4/src/agents/subagents/completion/subagent-completion-instructions.ts#L4)、[spawn 回执说明](https://github.com/openclaw/openclaw/blob/a31d9039928ca8fc9055e25e940fd0a4136c51b4/src/agents/subagents/spawn/subagent-system-prompt.ts#L179) | 区分单个 child 结束、原请求完成、内部 final 与对外发送；按原目标整合，避免重复已经送达的结果；在工具说明/回执附近明确下一步 | 不迁入自动外发、delivery fallback、额外重试、模式管理或将原结果当作新授权 |

Pi core 的 [followUp](https://github.com/earendil-works/pi/blob/200387122ca450d6387f033949423114a270b96c/packages/agent/src/agent.ts#L303) 本身是内存入队，不能单凭它声称闲置时自动唤醒。Pi Durable 示例用 [Conversation.submit](https://github.com/earendil-works/pi/blob/9fba660cf1caca0ade5bea72269352416e595a19/packages/durable/test/examples/23-subagent-background.ts#L103) 投递到仍存在的主 Conversation；这不等同于复活 Yuki 已 completed / cancelled 的父 Work。

本轮吸收的是：**就近说明参数、结果的真实语义与下一步；将持久保存、安排续跑、模型观察、用户交付分别核对。** 不照搬上游运行时或将其能力写成 Yuki 已有保证。

## 3. 现有提示审查与裁决

| 编号 | 当前事实 / 位置 | 证据强度与裁决 |
| --- | --- | --- |
| P01 | `subagent_start` 返回 `child_id`，`task_control` 只列出 `owned_run(run_id)`，`run_id` 没有 description | 已确认说明缺口；补齐原 ID 对应和直接等待简写 |
| P02 | conversation 说明只说等新输入、无人物/主题筛选，没有说明普通子完成通知不打断 active wait | 已确认说明缺口；明确真实语义，并提供 any 组合 |
| P03 | any 命中一次后，模型可能处理群消息，再次遗忘仍未完成的 child | 当前说明没有交代剩余依赖如何继续；这是待验证的相邻风险，加入群消息先到的场景 |
| P04 | “不把工具完成通知当成新请求”缺少沿原目标处理的正向连接 | 授权边界正确；存在潜在误读，补一句即可，不声称它导致本次延迟 |
| P05 | “可以不发”没有在该句限定范围；其他段落已说明闲聊/SELF/静默要求 | 优先删除泛化句，保留已有明确作用域；没有证据证明这句话压过任务交付要求 |
| P06 | “完成后交付结果”与 complete / 最终正文登记结束的说明并列 | 可能把业务完成理解为先结束 Work 再交付；改清用户要求的交付属于原目标，不规定 complete 必须最后调用 |
| P07 | “后台维护时安静”与“后台子任务”可能混读 | 澄清后台执行方式不改变原用户的反馈约定；不能据本例认定 SELF 安静规则是根因 |
| P08 | `subagent_control` 简介称“完成后可继续原上下文” | 对象不清，易误读为恢复整段旧工具往返；改成按原 ID 管理/恢复目标 |
| P09 | 完成输入 detail 只提示可查询 result | 收到结果后的行动提示可更直接，但通用输入前缀已经要求继续原目标；只替换现有 detail，不扩建提示注入层 |
| P10 | `work_session.py` 的恢复资料已说目标仍需跟进用 wait、无需发言不等于目标结束 | 保留。没有证据需要重写整份恢复资料或再次复制同一规则 |

已经清楚的引导也保留：显式 `send`、内部 final 不自动外发、交互任务实质进展反馈、父任务继续整合、已确认效果不重做、用户继续聊天不取消后台任务。

## 4. 拟议 diff

以下保留实施前核定的候选改动片段；**实际采用情况见 §8**。`@@` 只表示片段分界；不含行数的片段不是可直接运行的完整补丁。

### D01：澄清通知和发送范围，删除泛化静默句

文件：`src/yuki/prompting/contracts.py`，`_CORE_START`。

```diff
--- a/src/yuki/prompting/contracts.py
+++ b/src/yuki/prompting/contracts.py
@@
-    "虚构事件编号，也不把自己的假设或工具完成通知当成新请求。\n\n"
+    "虚构事件编号，也不把自己的假设或工具完成通知当成新请求或新授权。"
+    "工具和子任务结果用于继续原目标，按实际成果与未完成事项接续。\n\n"
@@
-    "可以不发。没有要交回的内部结果时，正文留空。\n\n"
+    "没有要交回的内部结果时，正文留空。\n\n"
```

保留日常不接话、用户要求安静、SELF 默认静默和已交付不重复的具体说明。删除一句无范围的许可不意味着每条内部通知都必须群发。

### D02：以原目标描述完成与等待，明确交付责任

文件：`src/yuki/prompting/contracts.py`，`_CORE_START` / `_CORE_END`。

```diff
--- a/src/yuki/prompting/contracts.py
+++ b/src/yuki/prompting/contracts.py
@@
-    "整个目标结束时用 complete，可在 result 中交付内部结果。"
+    "complete 提交原目标的结束决定；result 是内部结果，不替代用户要求的实际交付。"
@@
-    "没有登记结束决定时，最终正文也会结算当前 Work；持续目标尚需跟进就登记 wait。"
+    "没有登记结束决定时，最终正文也会结算当前 Work。"
+    "原目标仍需跟进时继续可执行工作；当前无可执行步骤时，"
+    "按原 ID 等待所需执行，或登记实际需要的时间、会话及插件事件条件。"
+    "无需发言不等于原目标结束。"
@@
-    "用户委托的交互式任务有实质进展、阻塞或需要决定时，必须简短报告并继续处理，完成后交付结果。"
+    "用户委托的交互式任务有实质进展、阻塞或需要决定时，简短报告并继续处理。"
+    "用户要求的成果与实际交付属于原目标，不以委派成功或单个子任务结束代替。"
@@
     "用户要求安静、SELF 自主工作和后台维护时安静处理，按原通知约定反馈，新追问照常处理。"
+    "用户委托的工作在后台运行，仍按原反馈约定处理。"
```

这里说明模型如何理解目标，不新增完成审批。允许已有合同中的同批 `send → complete` 和 `complete → send`：普通 complete 不截断原批次中仍获准的调用。子任务部分失败、暂停或还有无需等待的旁支，不自动否决父任务业务完成。

### D03：把子任务 ID 与父任务等待直接连接

文件：`src/yuki/runtime/subagent_tools.py`，`subagent_tools()`。

```diff
--- a/src/yuki/runtime/subagent_tools.py
+++ b/src/yuki/runtime/subagent_tools.py
@@
-            "将复杂工作交给后台工作者，立即返回 ID；可以继续聊天。"
+            "将复杂工作交给后台工作者，立即返回 child_id；父任务可继续处理其他输入。"
             "需要当前 Work；尚未登记时先用 task_control accept 父任务。"
             "派出的子任务已经登记工作，直接执行；结果由最终正文或 complete.result 回传父任务。"
+            "仍需这个子任务的结果且当前无可独立推进的工作时，"
+            "用 task_control(action=wait, run_id=child_id) 等待。"
+            "父任务按原目标整合结果和处理交付；子任务结束不代表父目标已经完成。"
             "已有子任务用 subagent_control，勿重复派生。",
@@
-            "查询、取消或继续自己的持久子任务；完成后可继续原上下文，已归档会明确返回。"
+            "按原 child_id 查询、取消或恢复自己的持久子任务；已归档会明确返回。"
```

不改变子任务可用工具或执行权限。当前子 Agent 按原授权可以调用 `send`；委派下载并回传路径、委派直接交付是不同目标，按原 brief 执行。父任务沿实际发送回执整合，不把“父负责整合”写成“子 Agent 禁止发送”。

### D04：解释真实等待方式、一次性 any 与参数含义

文件：`src/yuki/runtime/work_control.py`，`work_control_tools()` 的 description / 参数说明。

```diff
--- a/src/yuki/runtime/work_control.py
+++ b/src/yuki/runtime/work_control.py
@@
-                "conversation 等当前会话下一条输入，没有人物、主题筛选；定期查证用 time_due。"
+                "conversation 只等当前会话下一条输入，没有人物、主题筛选；"
+                "只登记它不会因普通子任务完成通知而唤醒。"
+                "既要接收会话输入又要处理子结果时，用 any 组合 conversation 与 "
+                "owned_run(run_id=child_id)；all 等全部条件满足。"
+                "任一信号唤醒后，处理当前输入并继续可执行工作；"
+                "仍依赖未完成子任务且当前无可执行步骤时，按原 ID 等待剩余结果，不重新派生。"
+                "定期查证用 time_due。"
@@
-                    "run_id": {"type": "string"},
+                    "run_id": {
+                        "type": "string",
+                        "description": (
+                            "wait 的简写参数：所属执行的原 run_id 或子 Work 的 child_id；"
+                            "与 conditions 二选一。"
+                        ),
+                    },
@@
                     "conditions": {
                         "type": "array",
+                        "description": (
+                            "一次性等待条件。等子任务用 "
+                            "{kind:owned_run,run_id:child_id}；同时等会话输入时追加 "
+                            "{kind:conversation} 并用 wait_mode=any。"
+                        ),
                         "minItems": 1,
```

仅补模型可见说明，不收紧当前 schema 受理范围，不新增字段、工具、默认 deadline、轮询或 fixed delay。

用法资料应把工具回执与后续参数连起来。下面的 UUID 是说明用的虚构示例，实际调用须复制本次原工具回执中的真实 ID，不执行或复用该示例 ID。

先收到 `subagent_start` 回执：

```json
{
  "ok": true,
  "child_id": "5f6fbd06-4264-4fb3-b11a-b345fdaec203",
  "state": "queued",
  "pending": true
}
```

随后需要同时等待该子任务与会话输入时，使用同一个 ID：

```json
{
  "action": "wait",
  "wait_mode": "any",
  "conditions": [
    {"kind": "owned_run", "run_id": "5f6fbd06-4264-4fb3-b11a-b345fdaec203"},
    {"kind": "conversation"}
  ]
}
```

没有继续接收群消息的需要时，直接 `task_control(action=wait, run_id=原child_id)` 即可。不要把 any 组合规定成所有任务的必选动作；显式 all 的部分满足仍保留原合同。

### D05：在既有完成输入 detail 中说明父任务下一步

文件：`src/yuki/runtime/subagent_repository.py`，`_prepare_result()`；只替换原 detail 文案，不增字段或新通知。

```diff
--- a/src/yuki/runtime/subagent_repository.py
+++ b/src/yuki/runtime/subagent_repository.py
@@
                     "detail": (
-                        "按需用 subagent_control(action=result, child_id=原ID) "
+                        "这是该子任务的结果，结合原父目标继续处理或按约定交付。"
+                        "需要明细时再用 subagent_control(action=result, child_id=原ID) "
                         "查询原结果与执行回执。"
                     ),
```

保持原 child / revision 唯一输入、原状态、原结果和原回执；说明文字不证明结果已被模型观察，不改变通知对 active wait 或终态的处理。已有 text 足够时不要求先查 result；子任务没有额外正文也不因此变为完成失败。

`WorkControl.take_inputs()` 已提示结合原目标继续处理，`WorkSession` 恢复材料已有目标/等待说明。两处默认不扩写，避免同一规则再复制三份。D05 不加“静默就再提醒”或“多请求一次模型”的补救链。

### D06：同步现行模块文档并清理矛盾说明

在实施基线更新 `docs/architecture/persistent-subagents.md`、`main-agent-runtime.md` 的相应小节：

- 明确 `child_id → owned_run.run_id`、直接等待与 any 组合、一次命中后剩余依赖的接续。
- 分开结果持久化、父任务入队、模型观察和用户交付；普通通知不当新授权。
- 如实保留现行 active wait、终态和已接纳结束的语义，不声称 D01–D05 改变了调度。
- 删除或直接修正不符合实施基线的说明，不把旧任务书、旧 worker 权限或旧工具名叠加为新合同。

## 5. 实施任务表与修改边界

| 项目 | 状态 | 交付 |
| --- | --- | --- |
| R01 事故请求与发送回执核查 | [x] | §1.2；读取实际事故请求，核对原等待与最终发送 |
| R02 现行固定提示、工具说明及恢复附言审查 | [x] | §3；与事故版本对照，无上述源码差异 |
| R03 Pi / Pi Durable / 第三方扩展 / OpenClaw 原始来源核查 | [x] | §2；固定 SHA，文案与运行时分别说明 |
| D01 通知/发送范围文案整理 | [x] | §4 D01，§8 |
| D02 完成/后台工作/交付责任文案整理 | [x] | §4 D02，§8 |
| D03 subagent 工具说明整理 | [x] | §4 D03，§8 |
| D04 wait 工具参数说明整理 | [x] | §4 D04；参数例改为就近字段说明，见 §8 |
| D05 既有结果 detail 文案替换 | [x] | §4 D05，§8 |
| D06 现行文档同步 | [x] | `main-agent-runtime.md`、`persistent-subagents.md` |
| D07 按先写失败场景、后实施的顺序完成定向验证 | [x] | §8；覆盖与未测边界分别记录，不包含 §6.2 的真实模型对照 |

实施范围限上述模型可见文案、必要字段 description、模块说明与匹配验证。人格原件 / SOUL、模型路由、预算、tool_choice、工具权限、等待状态机、自动发送、调度器及数据库 schema 不包含在该拟议 diff 内。

不增加“每条通知必须发群”“完成必须先汇报”“所有子任务都成功才能 complete”“每次必须查询 result”“complete 必须最后调用”等门槛。用户要求的真实交付按原目标和回执处理；需要继续的工作按原授权接续。

如果后续证据证明模型已经构造正确等待，但结果仍无法被观察，应另列运行时问题及原始证据；不能用继续堆提示词、循环提醒或伪造群消息掩盖。

## 6. 验证设计：先列失败，再实施

本节是**实施前确定的验证设计**，实际结果与覆盖边界见 §8。先确定下面的失败场景及可复现 artifact，再实施文案。优先沿原真实 Work、输入、等待、调度、工具执行和投递回执做集成/E2E；不把提示字符串快照或固定工具调用次数当成功。

下面是失败场景清单，不要求每行新建一套测试。复用已有真实链验证，合并相关场景；未改变的原机制以匹配风险的定向对照核验，不重复无关全量测试。

### 6.1 失败场景与观察项

| 编号 | 场景 | 应观察的行为 / artifact |
| --- | --- | --- |
| V01 | 用户要求找图并发出；child 完成后再无群消息 | 使用原 child 等待；完成信号使父任务有机会观察结果；有且仅有对应的成功图片交付回执。记录源事件、父/子 ID、wait、输入、实际请求及 send 回执 |
| V02 | any 先被群消息唤醒，child 稍后完成，没有第二条群消息 | 处理输入并继续可独立推进的工作；需要等待时按原 child 等剩余结果，不重新派生；后续仍能完成原交付 |
| V03 | 多个 child / all 等待，只有一个先结束 | 不伪造全部条件满足；剩余真实条件满足后继续原目标 |
| V04 | 原任务明确同时等 child 和将来时间 | 真实 all 语义保留，普通完成通知不偷换时间条件；区分提示选择与运行时信号处理 |
| V05 | 子结果在父任务普通等待期间到达，未注册 active wait | 核已有入队路径；入队、是否调用模型、是否交付分别记录，不将 queued 当成模型已看到 |
| V06 | 父任务已经接纳 complete，等待执行收拢 | 如实保留当前 accepted_control 行为；辅助结果不被文案测试偷偷改成新业务输入。不得把这个分支测成“必然再次调用模型” |
| V07 | child 已按授权成功交付结果 | 父任务依据原回执整合，不重复发同一文件或已送达附言 |
| V08 | child 失败、取消、部分成果或提问 | 按真实结果继续、报告问题或提出必要问题；不把失败当成功，不要求每个 child 成功才能结束父目标 |
| V09 | 结果内容已足够 / 需要原详细回执 | 前者可直接继续，后者可按原 ID 查询；不强制一次额外 result 查询 |
| V10 | 重复通知或恢复后再次读到同一结果 | 原 child/revision、原执行及送达回执可追溯，已确认效果不重放 |
| V11 | 发送结果 uncertain / 确认丢失 | 查询原发送回执，不盲重发，不把未知状态写成成功 |
| V12 | 用户明确要求安静、普通闲聊无需接话、SELF 无待反馈变化 | 合理静默仍可发生；用户委托工作没有因“后台”二字被自动归入静默 |
| V13 | 父任务已 cancelled / failed / completed，或来源 generation 失效、独立 suspended | 不因新增说明复活原任务或突破来源；记录输入实际保留/消费情况 |
| V14 | 用户中途修改目标、继续聊天、要求沿已有 child 补充 | 按原身份和真实新输入处理；不创建重复根目标，不把 child 结果当作新授权 |
| V15 | 同批 send→complete / complete→send，及内部空 final | 原有合法顺序可用；无公开答复需求时允许内部结果为空，不新增完成审批 |
| V16 | Main / worker、direct / Code 及实际 Provider 序列化 | 在各模式实际 Provider 请求中确认所需引导可见；同入口与同部署配置的 Main/worker 复用相应业务规则，direct/Code 保留各自合法声明与序列化差异，不要求整个请求字节相同。只在合法新链应用变更，不追改原已付请求 |

使用脚本化/固定响应 Provider 可以验证等待、续跑、回执和重复处理的机制，但**不能证明新提示会提高真实模型选对等待条件的概率**。若个别场景主要验证模型选择，应在结果中标明模型尚未验证，而不是用预先写对的工具调用冒充模型成功。

artifact 至少能关联原场景与父/子/等待/输入/发送记录，包含未通过和缺样；具体采用仓库已有测试 artifact 方式，不新建生产证据账本。隔离执行不发送真实 QQ、不更改生产库、不购买模型请求。

### 6.2 模型提示前后对照（独立边界）

真实模型对照另行安排并明确请求/费用范围，不把它隐式加入本轮任务书编制或普通代码验收。安排时固定场景、工具结果、模型 route、参数与评分方式；旧/新提示使用独立原始记录，保留实际工具选择、成功/失败样本、请求次数与成本。

重点观察：是否选择正确原 ID 等待、群消息先到后是否继续等待、结果到达后是否按原目标交付、是否提前结束、是否重复发送。报告样本量与成功率；未经对照不得写“稳定解决”或百分比提升。即使有限样本全部成功，也不代替线上自然流量验收。

## 7. 初次编制记录

初次编制时已完成事故与提示审查、上游参考核查和本任务书的候选 diff 整理，当时 D01–D07、模型对照均未实施，没有新提交、PR、合并、Release 或部署。用户随后授权执行任务书，本地实施状态见 §8。

文档已核对 UTF-8 无 BOM、代码围栏、相对链接和空白格式；5 个候选 diff 块中的 12 处原文片段与固定源码 `93200d12` 匹配。该核对只证明任务书片段有对应基线，不代表补丁已应用、代码测试通过或模型行为改善。

后续提交、推送、CI、合并、上线和自然用户验收分别记录；不由本地验证推断生产或真实模型行为已改善。

## 8. 本地实施与验证记录（2026-11-01）

### 8.1 实施位置与实际改动

- 基线：重新获取的 `origin/main`，`2a0ba7215f6a398e3b9bc7417ded18d49fc3ce12`。
- 分支：`codex/task-prompt-20261101`。
- 独立工作树：`C:/Users/bymay/.codex/worktrees/task-prompt-20261101/Yuki-QQbot`。原 `D:/Code/My_Github_Repo/Yuki-QQbot` 保持旧源码，仅同步本任务书及其入口说明。
- D01、D02：删除无范围的“可以不发”，说明结果沿原目标接续、实际交付属于原目标、后台执行保留反馈约定；保留合法静默、内部空结果和既有调用顺序。
- D03、D04：工具说明连接原 `child_id` 与 `run_id`，解释 conversation、any/all 和剩余依赖。D04 的组合示例改为工具及字段 description 中的直接说明，避免伪 JSON 被理解成额外参数；没有新增参数、限制或校验。
- D05：只替换既有结果 detail，说明结合原目标继续或交付，需明细时再查原 ID；没有新增通知或提醒链。
- D06：更新现行两份模块合同及架构入口；保留 active wait、accepted complete、终态和来源语义。

源码只改 `prompting/contracts.py`、`runtime/subagent_tools.py`、`runtime/work_control.py`、`runtime/subagent_repository.py` 四处文案/description。复核 diff，并剔除字符串值与 description 元数据后比较 AST，四文件的语句及字段结构均未变；检查记录为 `test-results/task-prompt-source-structure.json`。`work_session.py` 和通用输入恢复提示保持原样。没有变更等待、调度、发送或结算算法，也没有增加汇报/完成门槛。

### 8.2 先失败场景，后实施

源码修改前先新增真实父子接续场景，并扩展现有 Provider 请求验证。环境准备完成后，原机制定向基线 20 项通过；新增 4 个参数场景按预期在缺少新引导的断言处失败，保存了原始请求及 evidence。

两条交付场景在旧提示下也已沿脚本预设的正确等待完成发图，各有一个成功回执；基线失败说明的是**模型可见用法缺失**，不能反推运行时原来无法交付。环境缺失 vec1 或子进程导入失败不计为有效的预期失败。

有效 baseline 的四份 evidence 位于 `test-results/tool-result-delivery/`：

| 场景 | 节点目录 / baseline attempt |
| --- | --- |
| child 完成后无群消息 | `c6f819fae075b9c4392aaeb0cdf433b2bc8066c17260f9cfdd344eeba6b9af52/attempt-rrbbudqi` |
| 群消息先到，再等原 child | `1e0832d764339579f82655a4eac0096529447c750b09370f7ca593bb7bbbd32f/attempt-2kl56d05` |
| direct 的 Main 三入口实际 wire | `db6c3088543a3c13c080a9cba20051384ff71d68e36bc9f30402d6ebc402eb35/attempt-ong5y0dn` |
| Code 的 Main 三入口实际 wire | `6101f027f90846a71f8d56afa8c286ff164d3e67f95454ba0ec2333c18c77bbe/attempt-zmx5pzmg` |

实施后，新增完成链验证保存父子 Work、原 source、wait、输入、实际 ChatRequest、effects、Social 回执、原 PNG 和哈希。群消息先到的场景沿真实入站和 scheduler 推进独立工作，再按同一 child ID 等待；没有直接写 Work 终态，也没有在 child 完成后补群消息。

V16 随后补齐同一应用的真实 child executor：direct / Code 每种部署各捕获 Chat、SELF、schedule、worker 四次 Gemini 序列化请求。worker 自然完成，保留原事件和父子身份；四入口均能看到新引导。使用 MockTransport 返回 `NO_REPLY`，没有联网模型请求或执行 Monty 程序。

### 8.3 验证结果与 artifact

以下路径相对实施工作树根目录；`test-results/` 是本地验证产物，不随 Git 提交，重跑会生成新的 attempt。汇总入口为 `test-results/task-prompt-summary.json`，包含四份有效 baseline、四份最终 evidence、请求与图片哈希、源码前后哈希、JUnit 及未测边界。由于变更尚未提交，baseline 与实施后 evidence 的 HEAD 相同，不能仅凭 `source_revision` 区分；应结合汇总中的源码哈希与请求正文核查。

| 验证 | 实际结果 / 产物 |
| --- | --- |
| 新增完成链两场景、Main direct/Code 请求 | 4 passed；`test-results/task-prompt-guidance.xml` |
| 相关生命周期、等待、结果、结算、交付及来源恢复回归 | 初跑 192 passed，1 项子进程导入失败；补齐继承的 `PYTHONPATH` 后该项 1 passed。原记录与单项复跑分别为 `task-prompt-regression.xml`、`task-prompt-regression-retry.xml` |
| 当前 Provider 合同完整测试文件，含新增 worker wire 和既有原请求/回执保护 | 10 passed；`test-results/task-prompt-wire-final.xml` |
| 按测试节点去重后的最终结果 | **205 项通过，未跳过；环境失败项复跑已通过**。上述报告有 2 项重叠，不相加为 207 项独立测试 |
| Ruff check / 修改 Python 文件 format check / diff check | 通过 |
| `mypy --platform linux src` | 697 个源文件通过；`test-results/task-prompt-mypy.log` |

环境使用 Python 3.12 与冻结依赖；先以 `CC=gcc` 从仓库固定源构建本地 vec1。默认 Windows mypy 会在未修改的 POSIX 模块报告平台 API 类型差异，验证改用生产/CI 的 Linux 目标，没有为此改业务源文件。测试仍有现存 canonical Conversation 循环外键排序警告；本次未改数据库结构。

核心复现命令（PowerShell，先进入上述独立工作树；依赖与本地 vec1 已就绪）：

```powershell
$env:PYTHONPATH = (Join-Path (Get-Location) 'src') + ';' + (Join-Path (Get-Location) 'packages/yuki-agent-connector/src')
.venv\Scripts\python.exe -m pytest tests/integration/test_subagent_completion_prompt_delivery.py tests/integration/test_current_material_main_contract.py -q --strict-markers --junitxml=test-results/task-prompt-reproduce.xml
.venv\Scripts\python.exe -m pytest tests/unit/test_subagent_lifecycle.py tests/unit/test_subagent_maintenance_pages.py tests/unit/test_subagent_result_checkpoint.py tests/unit/test_work_settlement_writer.py tests/unit/test_work_wait_supervision_regression.py tests/unit/test_work_contract_cutover.py tests/unit/test_work_delivery_ownership.py tests/integration/test_platform_prompt_context.py tests/integration/test_subagent_common_capabilities.py tests/integration/test_subagent_sdk_recovery.py tests/integration/test_scheduled_subagent_recovery.py -q -m "not native_worker" --strict-markers --junitxml=test-results/task-prompt-regression-reproduce.xml
.venv\Scripts\ruff.exe check .
.venv\Scripts\mypy.exe --platform linux src
```

### 8.4 场景覆盖与尚未验证的部分

这里的机制验证使用脚本 Provider 或实际序列化的模拟传输，不将预设的工具选择当作模型能力提升。

| 场景 | 实际覆盖与边界 |
| --- | --- |
| V01–V02 | 新增 `test_child_result_reaches_original_parent_request_and_image_receipt` 两参数：原 child 完成→真实输入→父请求观察→唯一图片回执，以及群消息先到后继续工作和等待。图片为隔离生成的 PNG，不是实际找图或画面核验 |
| V03–V04 | 复用 `test_large_owned_completions_keep_original_wait_after_source_changes`、`test_original_sandbox_completion_keeps_remaining_all_wait_conditions`、`test_wait_conditions_follow_mode_with_failed_member`；覆盖多个 Sandbox run 的 all、失败/取消 child 与 timer。未另列两个成功 worker 先后完成或成功 worker＋timer 的完整链 |
| V05 | `test_parent_wait_keeps_its_original_subscription[False]` 验证无 active wait 时的持久通知与入队；该例不把入队算作父模型已观察或交付 |
| V06 | `test_auxiliary_input_does_not_withdraw_accepted_completion`、`test_recovery_commits_accepted_complete_and_publishes_result_atomically`；辅助结果不撤销已接纳 complete |
| V07 | `test_child_executes_main_business_tools_with_own_receipts` 与 delivery ownership 验证 child 自有发送回执及幂等；父模型读到已交付结果后决定不重发的选择率未评测 |
| V08–V09 | 失败/取消等待、`test_child_question_wakes_parent_from_owned_wait_then_answer_resumes_original_child`、`test_large_note_completion_notifies_and_result_reads_original_checkpoint`；新增链直接使用已够用结果，无强制 result 查询 |
| V10–V11 | maintenance/checkpoint 与 delivery ownership 组核对原 revision、执行和确认丢失；`test_file_sent_caption_unknown_can_explain_and_complete_without_reupload` 保留未知状态且不重复上传 |
| V12 | `test_empty_final_can_complete_answer_without_send_or_result_gate` 及实际 wire 的 Main/SELF/schedule/worker `NO_REPLY` 保留合法静默；用户静默意图的语言理解未评测 |
| V13–V14 | terminal parent、generation 变化、独立 suspended、新业务输入撤回旧决定及 `test_foreground_manages_original_child_without_accepting_another_work`；原身份和已有 child 管理保留，未为所有状态组合另建全链 |
| V15 | direct send→complete、真实 worker complete→send 尾部和空 final 的既有测试通过，没有新增调用顺序或结果门槛 |
| V16 | 两部署模式的 Main 三入口与真实 worker 均捕获 Gemini HTTP wire；Code 声明、原来源及原请求保护通过，未执行 Monty。完成链另保留 Main/worker 的实际 ChatRequest |

本地实施已完成。**真实模型提示前后 A/B、付费请求、真实 QQ 投递、线上自然流量和模型正确率提升均未验证。** 本节记录本地验证结束时的快照：当时尚未提交、推送、创建 PR、运行远端 CI、合并、发布或部署，没有生产数据变更。用户随后授权提交 PR，准备记录见 §9。

## 9. PR 提交准备（2026-11-01）

用户授权提交 PR 后，分支快进到最新 `origin/main`：`618a48a3a8f0208020a1cc1b809571c37cafa865`。本任务的四份源码、两份测试及相关模块文档与原验证基线之间没有上游改动；§8 的源码与测试哈希仍匹配。

在该新基线重跑 `test_subagent_completion_prompt_delivery.py` 与 `test_current_material_main_contract.py`，**12 项通过**，记录为 `test-results/task-prompt-pr-base.xml`。Ruff 与面向 Linux 的全 src 类型检查也通过；类型记录为 `test-results/task-prompt-pr-mypy.log`。这 12 项与 §8 的测试重叠，不增加独立用例数。

提交范围为四处源码文案/description、两份集成测试和四份文档/入口，共 10 个文件；本地 `test-results/` 保持忽略。提交、PR 和远端 CI 的最终状态以仓库实际记录为准。本次授权不包含合并、发布或部署。
