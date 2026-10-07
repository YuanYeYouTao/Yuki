# 删除导向重构：运行时组本地证据（2026-10-08）

本文件记录集成工作树的源码和定向回归；不表示已提交、部署或真人线上验收。任务书索引由主会话统一更新。

| 项目 | 删除对象与最后调用方迁移 | 保留的事实所有者 |
| --- | --- | --- |
| RUN-01 | 删除 before_work_tool、start_feedback_updates 及 start 硬门；Runner/Code 直接执行既有 admission | 原 Work/Social 回执、发送串行屏障、权限与预算 |
| RUN-02 | waits/inputs 不再保存 matched.text；旧 JSON 文本只移除，请求侧依引用读取完整正文 | canonical ChatEvent、generation、可见性和原输入水位 |
| RUN-03 | 删除 meter_active_time、metered_at、heartbeat meter、checkpoint active_seconds 写入口 | 原 lease SQL-time/fence；历史 active_seconds 可读但不更新；Automation 游标不变 |
| RUN-05 | 删除 response_feedback、UnsentFinalResponseError、结束礼仪重试与普通重复止损分叉 | 无工具 final 调同一 complete 判据；真实 final 回执与有限模型预算 |
| RUN-06 | 内部 final、显式 complete、caller 恢复沿 WorkControl；writer 复核晚到 input/effect/child。Deferred 由新 fence 结算；orphan-running 由原 Resumer 保守暂停且不启动模型、工具或通知 | WorkRepository CAS、原 recovery 行、原操作 ID 和累计预算 |
| RUN-07 | WorkSession 消费已有 ProtocolRecoveryPreparation.snapshot，删除相同 hydrate；完整确定性类型/值编码替代 repr(Row) | 原 journal/read set；codec 2，旧 hash 不能升级为可信；dispatch freshness 保留 |
| RUN-08 | charge_tools、corrections、checkpoint evidence、receipt_key、restart signal、Code remaining_calls 纯转发删除；WorkSession.execute 必须传 Invocation | 原 admission、operation_id、effect/journal；旧 outcome/Code 持久格式 reader 保留 |
| RUN-09 | AgentState/reducer 和合成 Frame API 移至 tests/support；默认 EventStream 不积存 response | 真实 ChatResponse、核心 loop 边界、原 journal |
| RUN-10 | 删除 artifact_ids 八件上限与自动截尾；只读 lookup/Work 查询不占业务数量或写屏障 | 每件 artifact 的真实交付核验、全请求容量、业务 admission |
| RUN-11 | 仅消费非空新输入时清 fingerprint/repeats 和本轮派生值 | 原 staged/consumed 输入、历史工具事实、预算 |
| CTX-06 | 首次 prepare_request 一次生成 Host envelope，顺序 H/S/完整当前输入；精确恢复/handoff 保持原链 | 公共 projection 只冻结原 composition；私有 journal 和 context-layout:2 |
| ID-02/07 | 删除可选 canonical 旁路、ProcessedEvent/DedupService、旧 append_inbound/new_generation 包装和重复 repair metric | CanonicalIngressResolver + Admission/UOW；测试通过真实 canonical fixture |

跨组桥接：API-02 删除 backend schema restart 与 append_only；API-03/ID-03 使用 typed Invocation/唯一 ToolRuntime actor；MEM-01 删除 backend exclusive/locator 门；APP-05 删除旧 delta 参数；APP-07 每次物理重试重新验 freshness，reserve/journal 只一次；DEP-01 无 worker/禁用 Code 仍沿原 Driver 恢复，不改原 ID。

## 已取得的定向结果

- agent_core_loop、work_source_guard、self_automation_and_work_wait、lease_heartbeat：39 passed。
- work_reporting_runner、work_state_tools、work_communication、work_source_guard、deletion_runtime_contract（当时版本）：77 passed。
- harness_predispatch_feedback、work_effect_results、work_delivery_ownership、lease_heartbeat：52 passed。
- speech_contract_upgrade_recovery、runtime_work、deletion_runtime_contract：57 passed，唯一失败为已删除 active_seconds 参数的旧 fixture；迁为 SQL 注入历史值后，该测试与 private_dispatch_source_reads、work_boundary_dispatched_save 和当时 deletion_runtime_contract 合跑 16 passed。
- 最新 deletion_runtime_contract：17 passed。覆盖长字段中部变更、旧指纹拒绝、首次布局及精确恢复、3000/7000 中文等待事件、非空输入清重复历史、有效新 owner 单次 Deferred 结算、后继 owner/新 revision 不覆盖、晚到 pending/unknown effect writer 围栏、九件 artifact 正反例、journal hydrate 一次，以及 orphan-running 非零预算/原 pending 回执且不调用服务或计划通知。
- tests 全树 compileall 通过。最终全量、协议矩阵及发布验证由主会话继续记录；这里不声称以上定向样本覆盖所有任务书组合。

## 删除统计与兼容边界

产品删除与测试搬迁分开统计。AgentState 原 51 行、合成 Frame/collect_response 约 63 行及导出退出产品；必要测试语义在 tests/support，不能将搬迁冒充仓库净删除。运行时其余净行数以最终合并 diff 为准，共享文件包含跨组修改，不按本地中间 diff 重复计收益。

保留历史 reader：旧 effect 无 outcome 的保守解释、Code active composition/旧 lifecycle 回执、旧 active_seconds 展示、Work journal/opaque；历史 unknown 不变为未执行。原 operation/Work/投递 ID 不重建，测试 fixture 显式构造 Invocation，生产 fallback 已删除。

## 后续联合回归

- `runtime_recovery`、`runtime_scheduler_boundary`、`protocol_recovery_preparation`、`deletion_runtime_contract`：58 passed。
- `caller_work_completion`、`self_initiative_main_entry`、`self_initiative_runtime`、`deletion_runtime_contract`：56 passed。空 return_to_caller 没有原 confirmed 交付时结束为原空输出失败，保留未决围栏；SELF NO_REPLY 继续可静默完成。首次 SELF 登记以原 Work writer 的 `initial_state=queued` 原子写入，避免误当 orphan；不以零预算猜是否启动。
- `commands_and_chat` 31项已通过：plain final 不隐式发送、不触发礼仪请求；空 Provider 仍原空内容处理。disabled canonical owner 正常转入口拒绝，未绕过 admission。
- Code Linux隔离配置与原生证据见 `deploy/security/README.md`：32底层 worker + 4真实子任务入口 + 1双记忆写入授权用例 passed；namespace/capability/环境/挂载/网络/取消探针 passed。AppArmor仅离线解析，实际组合enforce尚待部署门槛，不据此声称已上线。

## 第二轮全量失败闭合

- completion_confirmation、no_progress_feedback、prompt_state、reporting_feedback_states、四协议 reporting wire 与 Gemini wire：55 passed。旧礼仪暂停原因移除；显式 complete 前仍逐轮验证原工具声明、调用/回执与签名前缀不变。
- work_pause_notices：14 passed。历史 active_seconds 通过原表种子验证保留；通知恢复不新增模型或业务执行。
- agent_core_differences、agent_core_differential、agent_core_loop、work_query_authority：31 passed。golden 仅更新 RUN-01 空 final 不再礼仪重试与 CTX-06 首次 Host 布局；其余原 baseline 保留。只读目录使用原 call ID，不计业务工具数；伪造 SELF actor 在构造入口即拒绝。
- subagents：28 passed，3 条 native Code 场景在 Windows 跳过，另行在 Linux pinned worker 验证。work_compaction_capacity/mixed_input_timeline/source_guard_readonly/storage_policy 联合组此前 86 passed、3 skipped；其余两处为测试包装后的 AsyncMock 引用和旧 freshness 次数断言，修复后与查询/存储等定向 26 passed。
- 退役 memory finalizer 的 TerminalFinalizationSource、授权 gate、旧 ToolBatchExecutionResult 及 UntrustedFinalizationError 经全树 caller 调查仅有定义，整体删除；真实 agent_core ToolCallOutcome 与原 typed effect 回执不变。

- 最终稳定源码下 agent_receipt_loop：25 passed；两条 reported_failed/unknown 定向 2 passed。失败发送 fake 直接产出同一 typed 失败，避免先捕获成功后只替换 display 造成矛盾；真实 WorkSession 一致性拒绝不放宽。
- Linux `test_subagents.py -k business`：3 passed（chat_completions / responses / native responses），真实 pinned worker + launcher，40 原终端调用在 32 次段预算后沿原执行恢复；Windows对应3 skip已用此实际结果补齐。
