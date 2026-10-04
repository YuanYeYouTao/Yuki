# Pi 内核与 Code Mode 实施合同

基线：`8204b28ebc8939213dae60dbab94ab1c16d1263a`。本页规定正在实施的目标，
运行状态见 [交付记录](pi-codemode-delivery.md)，不将设计当作功能验收。

## 目标和现有事实源

采用固定 Pi core 的 Python 语义移植作为唯一模型循环；`AgentRunner` 最终仅保留兼容门面。
Code Mode 使用固定 Monty 原生 Rust 子进程与手动挂起接口，生成语言为受限 Python。
保留现有 Provider、`YukiRuntime`、来源、领域授权、Work、根预算及 Social/Manager 回执。
短聊和单次发送继续使用直接工具；`execute_code` 必须已有原已接纳 Work。

完整冻结声明与 Python wrapper 从同一 descriptor 投影。目录查询、脚本、结果和快照
均不能授予权限。主、child 和独立插件计算仍保留各自原合同；无工具计算不开放 bridge。
Provider 原生工具仍由 Provider 执行，不虚构本地逐次授权回调。

新调用由 Host 提供原执行、chain、request sequence 和原 Provider call ID。
参数摘要仅检查同 ID 的内容冲突，不能合并副作用。同响应重复 ID 拒绝，跨响应重用 ID 合法。
旧 Work 的 `chain:sequence:call` 保留。脚本子身份绑定原父、feed 和 engine call 的私有映射。

## 持久化和恢复

唯一业务事实源仍是 `runtime_work_effects` 和领域回执。不增加 CodeWorkScheduler、
第二个效果数据库或独立恢复租约。`receipt_json` 增加版本化 invocation/composition 元数据，
历史 `work_schema_v1.py` 和已发布迁移不改写。程序快照复用私有 ProtocolStore 对象与 GC。

快照和文件准备在写事务外；T1 原子发布父边界与子意图；T2 原子登记预算与派发标记；
事务外调用 binding；T3 先存真实 outcome；最后答复 VM。未知副作用保留原 ID，
停止该脚本，不能重跑整段代码。取消先关闭接纳，已发生效果仍按原 ID 结算。

父未结算且原 journal 仍 pending 才能恢复同一 composition。已配对 partial 的父不能再续 VM
或追加第二个 Provider 工具结果。控制等待、交接和终态须与父回执/必要协议配对原子发布。
记忆一次写授权保留，获准写后强制交回模型；脚本 catch 不能解除宿主关闭状态。

外层 composition 不额外扣业务工具额度，子调用在 T2 逐项计量。已接纳失败/unknown 不退款，
恢复和历史回执不重复扣费。Monty suspension 限额与业务额度分开，恢复不充值宿主累计资源。
同 activation 外层代码顺序执行；只读有界并发，发送/写入为屏障，外层不占子调用信号量。

## 实际入口覆盖

| ID | 当前入口与真实执行路径 | 保留的特殊边界 |
| --- | --- | --- |
| E01 | `services/chat.py` → `MainAgentTurnService.run` → `AgentRunner.run` | 不强制聊天建 Work；显式发送 |
| E02 | 原 `WorkControl/WorkSession` → 同一 Runner | 原租约、ID、journal、预算恢复 |
| E03 | Chat SELF → 同一 TurnService | 稳定 SELF 和原 initiative，不借真人 |
| E04 | `automation/handlers.py` → 同一 TurnService | SELF run/step、原场景 |
| E05 | 同上，Person automation | 创建者当前权限 |
| E06 | Automation 固定步骤 → Social | 静态提醒不进模型循环 |
| E07 | PluginBackgroundTurnWorker → 主入口 | 原 job/event、批准与恢复 owner |
| E08 | `plugin_host/main_turn.py` → TurnService | `DurableInvocations` 保留同步调用结果 |
| E09 | `services/plugin_sessions.py` → 独立 Runner 实例 | 同一循环实现，`tools=None/max_tool_calls=0` |
| E10 | `services/subagent_execution.py` → 共享 Runner | child lease、原 WORKER_NAMES/root |
| E11 | 原 Work/subagent control | 父验收、分页、通信和主发送 |
| E12 | `control_plane/surface.py::mutate_work` | 管理者不变成执行 actor |
| E13 | 原 control/trace 只读查询 | 查询不唤醒 Agent |
| E14 | `cli.py` 管理与诊断 | 不新增通用模型 prompt 入口 |

逐工具声明导出使用 `python -m scripts.export_pi_codemode_inventory --output <path>`。
它仅创建临时合成 SQLite，使用实际 core/admin/automation schema 和生产声明装配；
声明 fixture 的 handler 禁止执行。外部插件/MCP 是部署相关清单，必须通过其绑定 fixture
覆盖，未经授权不采集生产配置。导出成功不表示其中工具的行为验收成功。

Provider 覆盖沿现有 Chat 15 个 vendor、Responses、Claude、Gemini 与显式搜索桥。
验收比较真实 serializer payload、opaque、顺序和费用口径；当前 Provider 对 Yuki 暴露 complete，
合成 streaming frame 测试不等于上游 token streaming 已支持。

## Code Mode 合同（P05 实施）

- 主合同 version 11 加入固定 `execute_code`；`MainAgentContract.script_api` 随冻结声明生成，
  其 digest（API revision + manifest revision + wrapper 映射）写入父 composition 的
  `api_revision`。digest、engine digest 或 dump format 不符时恢复结算为 partial，不跑旧码。
- 子调用 T1 元数据追加 `arguments_ref`（ProtocolStore 私有对象），恢复只按保存的映射重建，
  不按名称+参数寻找相似调用。未 T2 的版本化 intent 不计入未知效果围栏；旧行保守保留。
- 生命周期控制子调用使用 `admit_dispatch(charge=False)`：有派发标记、不扣业务额度。
- 外层 composition 永不计业务工具；`usage` 区分 `business_admitted`、`control_calls`、
  `rejected_before_dispatch`、`reused_receipts`，仅本 activation 增量。
- 段额度让出返回内部哨兵，外层 call 不配对；原 Work 下一段 restore 识别
  `PendingComposition`，在任何模型请求前续跑同一程序。
- worker 合同的子集投影尚未接入（P07）；没有主合同投影时 `execute_code` 返回
  `code_engine_unavailable`。

## 删除和切换边界

P01 删除可变 `_batch` 作为身份来源；P02 统一原子 admission；P03 移植 Pi 控制流；
P04/P05 接入手动 Monty 驱动和完整工具；P06–P09 完成领域、入口、协议、迁移与隔离；
P10 删除旧生产循环和临时接口。参照来源与差异见 [provenance](pi-port-provenance.md)。

离线验收不能代替真实外部或生产验收。已有新子 effect 后，不认识它的旧 binary 不能接管
Work；回退保留新消息、文件、预算和回执，使用兼容 reader 收拢，不能恢复旧数据库覆盖事实。
