# 删除任务 API / 工具 / 控制面证据（2026-10-08）

本记录针对 `codex/deletion-refactor` 集成工作树的本地实现。提交、合并、生产部署、Linux 验收由主会话单独记录；以下不等同于上线完成。

| 任务 | 唯一 owner 与删除闭包 | 本地证据 |
|---|---|---|
| API-01 | 删除 capabilities/registry.py 的死 CapabilityRegistry 与 results.CapabilityResult；保留 Automation 同名领域 DTO | package/tool catalog 和 capability_runtime_security 回归通过 |
| API-02 | MainAgentContract 声明不变；TurnCapabilityRuntime 仅冻结目录及当前 callable IDs；删 append_only/schema ledger/restart flag/mark_side_effect 死链 | 授权、撤销与 revision 测试；旧 rebuild 自证测试删除 |
| API-03 | core/admin/automation handler 及 binding 使用 ToolExecutionResult；SDK PluginResult 在 Host 单点映射；historical decoder 拒错型布尔 | 真假布尔、typed binding、media/evidence/结果预算回归；活运行的展示文本不得反推效果 |
| API-04 | Automation facade 三动作显式 service 方法；真实入站一次绑定 ToolActor | pause/resume/cancel 及无权限拒绝参数合同测试 |
| RES-01 | ArtifactRepository 选页使用真实模型信封与 escaped durable bytes；item 限额约束业务页；删除 chat 二次 fitter 与 agent_tools 展示预裁剪 | 对象/数组/string、中文/引号/反斜线、item_limit=5、超小预算明确错误；标量分页完整重建；VM 配套由主会话验证 |
| RES-02 | 可选文本归档 OSError 返回原效果加 result_unavailable；原授权/取消继续传播 | disk-full 已提交效果不降级；Work/Code 配套由 runtime/主会话验证 |
| FILE-01 | WorkspaceStore 只 snapshot；FileWorkspace 只可变路径；附件 fd streaming + checkout；同请求核来源/name/hash/size；旧 mutable control 写入口删除，workspace_write schema=2 | >4MiB 附件成功、同源重复一份 snapshot、异源同 bytes 冲突；original artifact_snapshots 扩 source_key，无新表；Windows 跳过 POSIX；后续 Linux 真文件矩阵已补，见下文 |
| TERM-01 | 唯一 PersistentManager；旧一次性 Manager、run_python 接纳/code/input/export 删除；shared receipt/outbox/get/ack 保留 | Manager protocol 拒旧入口、原结果等待/取消/outbox回执；validate_runtime 改显式 isolated socket 的 terminal_exec + workspace_publish；后续 Linux 原回执与唯一 Manager 矩阵已补，见下文 |
| TERM-02 | 与 DEP-01 共享交付 | 主会话负责 VM 依赖和环境运行时验证 |
| CTL-01 | ControlMethod 描述符显式 service binding；HTTP 仅边界解码；生成前端 QueryMethod/CommandMethod；删除 storage_scope_id 别名 | descriptor binding/生成列表一致性；frontend build；WebUI HTTP 权限/CSRF/query/command 回归 |
| CTL-03 | ControlWorkspace 直接返回 ManagementMutation；删除 gateway tuple 拆装 | 原 accepted terminal 回执、完成不触发聊天、文件写与幂等回归 |
| CTL-02 | pure 参数/上传校验放 writer 前，随后先查原 receipt 再新接纳；execute 使用同一 prepared 参数 | 真实 Control external writer/副作用/unknown fencing 回归 |
| AUTO-01 | 删除 generated 新策略、yuki.generate 新注册/执行；只保留历史模型输出尾随发送阻断 reader | validator 当前 agentic 合同；旧 stored-script retirement 负例保留 |
| AUTO-02 | create/update 共用原 service 的验证/commit 规则；Control create/update 明确 script envelope | mutation-boundary 由主会话结合 ID09 检查 |
| SDK-01 | Plugin API 3.3 去 LLMFacade、ctx.llm/fake/权限；agent.run 接 context_profile；保留 result/resume/独立 sessions | Host facade 回归，旧 SDK wire fixture 迁 agent.run；版本/生产插件迁移由主会话负责 |
| CLI-01 | 在线管理只 authenticated Control；revision/explicit permissions/原 request；setup 持原应用锁才离线 bootstrap；release_smoke 不再健康后改插件 | MockTransport 验证 Cookie/CSRF/权限/request/revision/断线单次提交；HTTP 回归；quickstart 更新 |
| ID-03 | InvocationContext 仅 ToolRuntime + host operation_id；ToolRuntime 入站首次绑定 ToolActor，五份 actor 身份改只读投影；external 无假 Person，独立 target_presence_id | 九入口构造由相应 owner 迁移；更广入口矩阵纳主会话全量验证 |
| ID-09 协作 | PluginInvocation actor_person_id 与 target Person 分离；authoritative current account 对 canonical creator；历史 creator_user_id 保留 | 原账号 A→当前 B 允许，A 转属其他 Person 拒绝；完整数据库重绑定由主会话负责 |
| DB-04 协作 | control_command 删除 SQLAlchemyError→STATE_MISMATCH 泛化 | 数据库系统故障原异常传播，不误报 409 |

## 已运行检查

- API/工具/Workspace/Sandbox/Control/Automation validator/Plugin Facade 的 11 组：116 passed，1 skipped（POSIX FileWorkspace）。该次之后新增布尔错型与运行时 typed evidence 回归，最终结果需查看主会话全量记录。
- `test_deletion_tool_contracts.py` 增加 exact bool、原 source snapshot、CSRF/no retry、超小页错误和 DB 故障测试。
- `test_issue262_readback.py -k 'scalar_string or json_read'`：4 passed；其余文件测试必须 Linux。
- `test_deletion_tool_contracts.py + test_webui_http.py` 在当时版本：47 passed。
- 定向 mypy `--platform linux --follow-imports=silent`：47 个源文件通过。
- `frontend npm ci --ignore-scripts` 与 `npm run build` 通过；方法类型来自 `scripts/generate_control_methods.py`，不维护另一份人工目录。

## 生产只读前置（主会话提供）

Bot sandbox_task_runs 634 全 completed；Manager jobs succeeded 556 / failed 59 / cancelled 13；run_python environment jobs succeeded 75 / failed 7 / cancelled 2，全终态且无 orphan；completion_outbox 为 0，quarantined 2 保留。活 Automation 无 yuki.generate，原 running/uncertain run 不换 ID、不重跑。ToolArtifact、Workspace 和旧 job 查询历史不清表。两运行插件按原精确批准权限迁到 API 3.3，不能自动扩大权限。

## 最后补充的本地回归

- API-03 活链：Coordinator 捕获 typed outcome，WorkSession 将同一 outcome 交给父捕获并在 journal 接受后从 execution_evidence 更新缓存；TurnExecution 不再从模型展示反推效果。normalize_legacy_result 只保留 Work 历史恢复 reader，测试旧回调的 decode 仅在 tests/support 中。
- 普通 typed capture 不含 durable owner，Social/Memory/Artifact 只有非空 work_id + effect_key 才绑定持久效果；普通发送/后台摘要/Work continuity 联合 56 passed。
- typed runtime 7 个源文件 mypy 通过。
- Sandbox unknown lifecycle 15 passed：原 request 查回、不重发、终态回执、错误来源拒绝；移除 run_python 的新提交正例，历史读取继续保留。
- API/Automation/Plugin 定向 89 passed、1 Monty skip 后的两个剩余 case 已逐一通过：插件 frozen binding 使用 canonical error_code；多协议 main-agent SDK wire 使用真实入站 canonical runtime alias。
- Control plugin configuration 的 24 个 fixture 改 API 3.3 后全部通过；撤销/manifest revision 阻断回归通过；AUTO 当前策略 auto/agentic 与历史 yuki.generate 拒绝边界分别保留。
- release_smoke 删除无人调用的 pending 写入 helper，检查运行 Bot 不再改变插件状态或 recreate；相关回归通过。Release README schema baseline 跟随主会话的 migration 版本统一验证。

- 集成树 `.venv` 最终定向七组（deletion_tool_contracts / automation_runtime / automation_unified_delivery / control_external_execution / plugin_facades / cli_entrypoint_contract / setup_web_defaults）：113 passed；日志 `.cache/api-final-focused.log`。

## 全量后第二轮闭合

- 精确失败节点二轮先通过 69 例；随后 Control Automation/config/MCP、Plugin manifest/native-media、Speech retirement、Code contracts 完整七文件通过 128 例、2 个 POSIX skip；剩余五例逐项补齐：Linux path 模拟和 Settings 显式容量、当前 canonical delegation。Code contracts + media 42 例和最后 delegation 1 例通过。
- 四协议 × pending/settled 的 Code interleaved recovery 8 例全部通过：测试 FakeDomain 使用生产 WorkSession 时明确发布 typed outcome，保留完整原 journal/结果唯一性/跨入站可见性断言。
- oversized Code JSON 的 8MiB 文本/百万数组测试保留完整负载，新增短 pytest 参数 ID，避免 Windows PYTEST_CURRENT_TEST 超 32767 字符造成非业务错误。
- Control typed descriptor 未知方法测试使用 KeyError；Plugin 3.3 内嵌 fixture/目录文档/所有 bundled manifest 同步。
- tool_effect_audit 的真实 actor fixture 由 data_context 补全后 15 例通过，原有 telemetry 失败和事件迁移隔离断言均保留。


## 独立任务书边界复核（Linux）

- `.cache/api-linux-boundaries.log`：136 passed。实际 POSIX Workspace/CAS/UTF-8 文件分页、持续环境、Sandbox、Control Workspace、issue262 Code 读回、语音退休历史、Code contracts/worker/resource policy。
- `test_deletion_file_bootstrap_boundaries.py`：10 passed（包含 `.cache/api-linux-entrypoints2.log`）。真实 0 / 4 MiB / 4 MiB+1 / 200 MiB 文件使用有限分块建文件、fd snapshot 和 persistent checkout；200 MiB+1 拒绝且不落 artifact。流式复制中断不留 metadata 或 pending 文件。
- 附件 checkout 提交后模拟响应丢失：原 request 恢复原 receipt，后来的 Terminal 路径修改不被再次覆盖；当前附件访问撤销后即使相同 request 也拒绝。该验证发现并修复 PersistentManager 缓存 file receipt 缺少 external_untrusted 标记的问题，历史结果/ID 不重写。
- 无 Control 会话的首次 bootstrap 在原 SQLiteApplicationLock 内执行；原锁冲突时仅尝试在线 Control，在线不可用则停止，不调用离线 writer。
- 旧 run_python 终态 cohort 仅以原 request/job/run ID 查询，原结果完整，队列为空，新 run_python 提交拒绝。
- `.cache/api-linux-files3.log`：上述前 8 例加 source-key 6 例合计 14 passed；后加的超限和 lost-reply/撤权 2 例在 entrypoints2 中通过。
- `.cache/api-linux-public-matrix.log`：Mock HTTP DeepSeek acceptance harness 12 例全部通过，无付费 Provider 调用。真实 worker 测试显式 pin launcher 路径/hash；公共入口恢复场景继续单独闭合，不能将此轮 20 个失败记为通过。
- `.cache/api-longvoice.log`：退休 voice 回执 succeeded/uncertain/executing × short/128/UTF-8 超长 call key 共 9 passed；重复回放保留原 source 和唯一效果，冲突 payload 拒绝。
- `.cache/api-webmemory-final.log`：11 passed。最终 Web provider summary envelope ≤2400，完整原 summary/source evidence 从 artifact 分页重建；MEM exclusive dead链及三个旧 get_*_memories budget 分类已删除。
- `.cache/api-feedback-native4.log`：36 passed。typed pre-dispatch refusal 保留原尝试计数和 journal executed=false；五 Provider 原配对回执后选择像素，逐请求验证来源。
- `.cache/api-final-types.log`：capabilities、PersistentManager、ToolRuntime 共 20 源文件 mypy 通过。

- `.cache/api-linux-entrypoints3.log`：Chat 8 + Automation 8 + Plugin SDK 4 个真实 Monty 公共入口共 20 passed。恢复后的原工具回执由 Host initial_runtime_context 明确 observations 信封读取；原 ID、结果数量、一次副作用和预算断言保持。SDK 测试等待已存在主任务达到量子边界，不重派发。

- 最终全量残差复核：`.cache/api-sandbox-final2.log` 15 passed；`.cache/api-kernel-work-final.log` Tool kernel + Work execution receipt regressions + Work execution dependencies 完整 39 passed。旧 get_person_memories 的预算断言迁到当前 search_memory；真实未知接纳、原 request 恢复及错误来源隔离断言保持。
