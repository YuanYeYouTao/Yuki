# Work 与 Memory 多余约束删除审查

## 最高约束

去掉限制后仍能完成真实工作的，优先删除。能删或放宽现有条件解决，就不新增替代代码或机制；不把固定次数、长度、标签、状态整齐或全量完成写成强制政策。为真实部分成果和未决事项留出继续、退出和以后的变化空间。

检查完整链路：声明、提示、参数、执行、持久化、恢复、收尾、现行文档及测试。只放宽入口、保留下游拒绝不算完成；不为已删除限制保留空参数、兼容别名或新开关。

## 基线与生产证据

- 本地实施基线 main `fa505a8c`；生产源码 `0046d394`、数据库0103。修改位于 `codex/remove-work-memory-policy-gates`，本报告的本地完成不代表已经推送或上线。
- 2026-10-10 01:14–01:15（Asia/Taipei）只读生产核查：Memory unhealthy唯一触发项为162条superseded缺后继链，全部8月更新，任何方向关系及原状态回执均为零；其余完整性触发项为零。25条active contested不参与healthy。Dream71个failed cluster是历史累计，本次部署后新增失败零。
- 清退旧812条Work后，新Work `229788eb-7765-4170-aed9-594e8aa6e508` 在00:47:35创建，00:47:52日志确认挂起。原recovery记为 `checkpoint_capacity` / `work_protocol_reference_deleting`，attempts=1、retryable=false；01:15采样仍有96条协议对象deleting标记。不是只根据旧挂起数量猜重构失败。
- 初次只读核查由三名新的gpt-6.1-sol/high子智能体分工，主会话核协议回收、配置及schema；该次核查未修改生产或调用模型。后续正式控制API群测与部署另列证据。

## 任务索引

| 编号 | 删除范围 | 本地实施与验证 |
| --- | --- | --- |
| C00 | 原因与完整调用链查证 | 已完成：生产只读元数据、当前源码与历史记录分别核对 |
| C01 | 完成标签、reporting模式、目标/原因/产物/报告事件数量帽 | 已完成：模型complete/final沿真实回执收尾，删final标签和quiet拒绝，长文案/多事件场景通过 |
| C02 | 重试耗尽、来源变化任意效果即暂停、取消误分类 | 已完成：原Work重排和原回执接续；连续五次失败、已执行效果、取消真实派发计数场景通过 |
| C03 | 旧delivery存在即拒绝当前合法上下文 | 已完成：两处拒绝直接删除；4个原来源失效/旧回执确认或未知场景通过，旧草稿不发送 |
| C04 | 等待条件数量、1秒–1年、过期deadline及局部JSON帽 | 已完成：即时/短时/长期/过去deadline和300条all条件真实登记、唤醒场景通过 |
| C05 | Work全局128、会话16、收件箱128和子任务入队数量帽 | 已完成：130项Work、129输入、十个子任务排队与原ID幂等场景通过 |
| C06 | 子任务文案、文件数、固定8/8预留及最低模型并发 | 已完成：上下游及死字段删除；真实TaskModelExecutor并发一、SubagentScheduler/SubagentExecution完成并通知通过 |
| C07 | JSON大小帽、空兼容参数及工具回执二次字节帽 | 已完成：改实际encode_json、全部调用同步，删除旧64KiB/1MiB与48KiB回执二次帽；大中文资料真实保存和原artifact完整存档通过 |
| C08 | 协议对象deleting导致拒绝及误归capacity | 已完成：已验证同hash的新引用撤旧删除资格，GC文件锁内复核；复用低配额旧对象、竞争回收与usage不重复计量通过 |
| C09 | 数据库固定64MiB检查点上限 | 已完成：0104真实0103→head升级，保留原media与实际回执，删除无消费者配额表/六个记账trigger，quick_check/FK通过 |
| C10 | Memory历史缺链/异步维护一票否决和旧质量报警 | 已完成：保留诊断事实，删除整体healthy否决及旧profile/待审核/一小时/终态staging/权威等级政策 |
| C11 | Dream操作分类、唯一性、最多四条、全集覆盖、key改名、证据帽 | 已完成：按实际引用部分整理，同key/同正文/多输出及回滚通过；13条证据在输入采样为1时仍完整复制 |
| C12 | 普通Memory提取三次即FAILED | 已完成：原job四次Provider失败后第五次无事实正常DONE，原claim/计数保留；19项相关场景通过 |
| C13 | 当前合同、配置、提示与陈旧测试同步及最终整合验证 | 已完成：旧说明与冻结断言同步；全量1104通过/50跳过，最终补删支路另行验证，Ruff/格式/Mypy/版本校验通过 |
| C14 | Memory Embedding与Rebuild重试次数封口 | 已完成：次数封口、提交准备后max1、死形参/返回值/配置及指数等待删除；23项场景验证超过原次数仍可沿原ID完成，提交确认丢失不重复写 |
| C15 | 配置之间的固定大小/顺序/比例政策 | 已完成：删除RuntimeConfigService跨key校验与错误包装、Settings重复校验、摘要target/trigger运行时拒绝及相关死字段；原配置读快照场景通过 |
| C16 | 辅助摘要与context_note格式/全集/重复政策 | 已完成：格式封口删除，23项摘要/恢复场景通过；省略旧要求不丢数据，19输入跨页不越缺口，原链引用不混用，缺省观察保留而显式空数组可更新 |
| C17 | 本地截断响应已经paired后仍永久暂停 | 已完成：本地未执行回执沿原Continue/segment，带或不带WorkControl均核真实native声明；相关回归通过 |
| C18 | 实际效果accepted但存档/展示失败仍判死 | 已完成：已持久fallback的普通异常沿原结果返回，真实容量错误/媒体/未知/取消回归通过 |
| C19 | 子任务conditions等待禁令与可选work_report拒绝 | 已完成：原child timer→信号→完成→父通知通过；无Work/neutral/已接纳发送及重复ID关联回归通过 |
| C20 | SELF普通输入无自身回执跳过、领取后字符预算封口 | 已完成：删除入场策略及展示消费者，普通incoming/大事件全文提取与原run恢复场景通过 |
| C21 | 旧generation无活来源的失败run阻塞当前输入 | 已完成：原live来源SQL加于两个原run查询；5个旧/live/跨generation场景通过，原run事实保留 |
| C22 | Rebuild部署总量二次拒绝与去重后反拒绝 | 已完成：删部署额外总量帽、唯一配置及重复selector拒绝；真实plan→提取→提交及请求None/1范围场景通过 |
| C23 | CodeMode必要结果超过展示软目标就暂停 | 已完成：纯删最终code_result_capacity；真实Driver/SQLite/父子回执验证通过，未宣称本机nativeVM验收 |
| C24 | 终止reason/等待键长度与状态变更结果文案门槛 | 已完成：缺/空/长reason、长transition、1500字符调用键幂等及真实修改无额外prose完成场景通过 |
| C25 | Dream至少两来源的额外拒绝 | 已完成：纯删两行；单源模型→preview→service→写入→恢复保留原证据/回执且不多调用场景通过 |
| C26 | 合法工具schema被深度/节点/正则/方言政策隔离 | 已完成：删除五类先验政策，沿原validator_for/check_schema；真实catalog→授权→执行→原键重入18项通过 |
| C27 | 把16条展示摘要误作恢复授权全集 | 已完成：按原ID与原source查询；130项最后Work可续，展示仍16，跨source不误续 |
| C28 | all等待被单个failed/cancelled成员提前闭合 | 已完成：删除member_failed短路；按原any/all聚合，失败事实仍在原条件回执中 |
| C29 | Dream无消费者的anchor/content/importance形状拒绝 | 已完成：schema与RECOMPOSE领域二次拒绝一起删除；KEEP/CONTEST/部分RECOMPOSE真实commit/rollback通过 |
| C30 | 已获准actorless自省操作五项白名单与跨操作误去重 | 已完成：删白名单；指纹复用原operation/fact/merge payload，metadata→invalidate→restore分别生效且分别幂等 |
| C31 | 完整合法结构化JSON仅因INCOMPLETE标签被拒绝 | 已完成：四处先验status门纯删；真实内容/schema/source验证决定结果，46项专项通过 |
| C32 | Work暂停升级为整个automation BLOCKED并关闭原run | 已完成：真实Worker保持原run/cursor；两次poll不多请求，原Work恢复后同run完成，相关46项通过 |
| C33 | 已保存Artifact读取再叠4MiB/路径32/搜索词256拒绝 | 已完成：声明与读取链同步删除；大中文原件、40层路径、300字查询、原字符串分页及GC三项通过 |
| C34 | 实际合法group_report correction被质量audit报错 | 已完成：真实GROUP CORRECT保留原quote，无旧authority误error；C29/C30/C34共8项事务场景通过 |
| C35 | 管理参数失败后锁死同操作，连合法get都拒绝 | 已完成：真实set错误→get读当前值→set成功→原效果重入，slot/helper/目录与控制消费者同步删除，相关24项通过 |
| C36 | SELF工具回执领取与呈现使用不同预算，未见输入被推进游标 | 已完成：修改前两个反例均复现；8k/1k分原窗口完整处理，改预算重试保持原run/range/fingerprint，15项通过 |

### P项任务索引

| 编号 | 删除/接线范围 | 状态 |
| --- | --- | --- |
| P01 | JSON深度帽 | [x] 本地完成，对应P项列源索引及实际验证 |
| P02 | 主SELF真实回执记忆写入 | [x] 本地完成，对应P项列源索引及实际验证 |
| P03 | Artifact扫描/分页帽 | [x] 本地完成，对应P项列源索引及实际验证 |
| P04 | watchdog/feed顺序门 | [x] 本地完成，对应P项列源索引及实际验证 |
| P05 | canonical多binding来源 | [x] 本地完成，对应P项列源索引及实际验证 |
| P06 | report kind和子结果裁切 | [x] 本地完成，对应P项列源索引及实际验证 |
| P07 | Dream实际来源占用 | [x] 本地完成，对应P项列源索引及实际验证 |
| P08 | Dream重复alias/元数据 | [x] 本地完成，对应P项列源索引及实际验证 |
| P09 | Reflection领域操作接线 | [x] 本地完成，对应P项列源索引及实际验证 |
| P10 | ordinary/Rollup摘要格式门 | [x] 本地完成，对应P项列源索引及实际验证 |

## 可追踪代码索引

以下行号由当前本地源码 AST 生成并逐项核验，是本轮快照；后续编辑以符号名为稳定检索键。已删除条件索引指向保留的执行方法，删除前原句可对照基线 `fa505a8c` 的同路径 Git 历史。箭头列出审计涉及的入口、下游及回执消费者；并列基础设施并不表示直接互调。测试链接指向真实既有测试，不代表本轮重跑了全部链接文件。

### C00 代码索引

调用/持久化/恢复：[src/qq_ai_bot/runtime/work_recovery_schema.py:1](../../src/qq_ai_bot/runtime/work_recovery_schema.py#L1) → [src/qq_ai_bot/memory/models.py:297 (MemoryConsistencyHealth.healthy)](../../src/qq_ai_bot/memory/models.py#L297)。

验证入口：[tests/unit/test_runtime_recovery.py:78 (test_repeated_transient_failure_preserves_original_work_and_can_complete)](../../tests/unit/test_runtime_recovery.py#L78)。

### C01 代码索引

调用/持久化/恢复：[src/qq_ai_bot/runtime/work_control.py:1046 (WorkControl._prepare_completion)](../../src/qq_ai_bot/runtime/work_control.py#L1046) → [src/qq_ai_bot/runtime/work_control.py:306 (WorkControl.validate_work_report)](../../src/qq_ai_bot/runtime/work_control.py#L306) → [src/qq_ai_bot/runtime/work_repository.py:673 (WorkRepository.accept_control)](../../src/qq_ai_bot/runtime/work_repository.py#L673) → [src/qq_ai_bot/social/tools.py:6 (social_tool_definitions)](../../src/qq_ai_bot/social/tools.py#L6)。

验证入口：[tests/unit/test_work_settlement_writer.py:150 (test_answer_completion_does_not_require_send_classification)](../../tests/unit/test_work_settlement_writer.py#L150)；[tests/unit/test_work_settlement_writer.py:319 (test_report_can_associate_all_admitted_inputs_and_rejects_foreign_event)](../../tests/unit/test_work_settlement_writer.py#L319)。

### C02 代码索引

调用/持久化/恢复：[src/qq_ai_bot/runtime/work_supervisor.py:69 (recover_failure)](../../src/qq_ai_bot/runtime/work_supervisor.py#L69) → [src/qq_ai_bot/runtime/activation_outcome.py:127 (classify_failure)](../../src/qq_ai_bot/runtime/activation_outcome.py#L127) → [src/qq_ai_bot/model_runtime/executor.py:467 (TaskModelExecutor._execute)](../../src/qq_ai_bot/model_runtime/executor.py#L467) → [src/qq_ai_bot/services/concurrency.py:44 (ConcurrencyManager.run_llm)](../../src/qq_ai_bot/services/concurrency.py#L44)。

验证入口：[tests/unit/test_runtime_recovery.py:229 (test_source_change_after_accepted_automation_preserves_receipt_without_replay)](../../tests/unit/test_runtime_recovery.py#L229)；[tests/unit/test_runtime_recovery.py:78 (test_repeated_transient_failure_preserves_original_work_and_can_complete)](../../tests/unit/test_runtime_recovery.py#L78)。

### C03 代码索引

调用/持久化/恢复：[src/qq_ai_bot/runtime/work_journal.py:165 (WorkJournal._load)](../../src/qq_ai_bot/runtime/work_journal.py#L165) → [src/qq_ai_bot/runtime/work_session.py:117 (WorkSession.restore)](../../src/qq_ai_bot/runtime/work_session.py#L117)。

验证入口：[tests/unit/test_work_protocol_continuity.py:332 (test_work_changes_from_deepseek_to_gemini_without_replaying_old_effect)](../../tests/unit/test_work_protocol_continuity.py#L332)。

### C04 代码索引

调用/持久化/恢复：[src/qq_ai_bot/runtime/work_wait.py:42 (normalize_conditions)](../../src/qq_ai_bot/runtime/work_wait.py#L42) → [src/qq_ai_bot/runtime/work_wait.py:163 (WorkWaitRepository.register)](../../src/qq_ai_bot/runtime/work_wait.py#L163) → [src/qq_ai_bot/runtime/work_wait.py:639 (WorkWaitRepository._observe_due)](../../src/qq_ai_bot/runtime/work_wait.py#L639)。

验证入口：[tests/unit/test_work_settlement_writer.py:368 (test_existing_timer_can_resolve_all_conditions_without_delay_policy)](../../tests/unit/test_work_settlement_writer.py#L368)。

### C05 代码索引

调用/持久化/恢复：[src/qq_ai_bot/runtime/work_repository.py:308 (WorkRepository.accept_in_session)](../../src/qq_ai_bot/runtime/work_repository.py#L308) → [src/qq_ai_bot/runtime/work_repository.py:1134 (WorkRepository.enqueue)](../../src/qq_ai_bot/runtime/work_repository.py#L1134) → [src/qq_ai_bot/runtime/subagent_repository.py:83 (SubagentRepository.start)](../../src/qq_ai_bot/runtime/subagent_repository.py#L83)。

验证入口：[tests/unit/test_work_settlement_writer.py:472 (test_retained_work_and_pending_inputs_do_not_block_new_intents)](../../tests/unit/test_work_settlement_writer.py#L472)。

### C06 代码索引

调用/持久化/恢复：[src/qq_ai_bot/runtime/subagent_tools.py:117 (execute_subagent)](../../src/qq_ai_bot/runtime/subagent_tools.py#L117) → [src/qq_ai_bot/runtime/subagent_scheduler.py:89 (SubagentScheduler.loop)](../../src/qq_ai_bot/runtime/subagent_scheduler.py#L89) → [src/qq_ai_bot/runtime/work_budget.py:16 (charge)](../../src/qq_ai_bot/runtime/work_budget.py#L16) → [src/qq_ai_bot/services/concurrency.py:44 (ConcurrencyManager.run_llm)](../../src/qq_ai_bot/services/concurrency.py#L44)。

验证入口：[tests/unit/test_subagent_result_checkpoint.py:166 (test_children_queue_without_admission_quota_and_keep_execution_concurrency)](../../tests/unit/test_subagent_result_checkpoint.py#L166)。

### C07 代码索引

调用/持久化/恢复：[src/qq_ai_bot/runtime/work_repository.py:96 (encode_json)](../../src/qq_ai_bot/runtime/work_repository.py#L96) → [src/qq_ai_bot/runtime/work_journal.py:123 (WorkJournal.load)](../../src/qq_ai_bot/runtime/work_journal.py#L123) → [src/qq_ai_bot/capabilities/results.py:129 (ToolResultBudgeter.render)](../../src/qq_ai_bot/capabilities/results.py#L129)。

验证入口：[tests/unit/test_work_native_media_receipts.py:1](../../tests/unit/test_work_native_media_receipts.py#L1)；[tests/unit/test_protocol_recovery_preparation.py:1](../../tests/unit/test_protocol_recovery_preparation.py#L1)。

### C08 代码索引

调用/持久化/恢复：[src/qq_ai_bot/runtime/protocol_store.py:439 (ProtocolStore.publish_refs)](../../src/qq_ai_bot/runtime/protocol_store.py#L439) → [src/qq_ai_bot/runtime/protocol_store.py:547 (ProtocolStore._unlink_objects)](../../src/qq_ai_bot/runtime/protocol_store.py#L547) → [src/qq_ai_bot/runtime/work_session.py:1469 (WorkSession.save)](../../src/qq_ai_bot/runtime/work_session.py#L1469)。

验证入口：[tests/unit/test_work_protocol_continuity.py:111 (test_new_work_reuses_prepared_object_marked_for_deletion)](../../tests/unit/test_work_protocol_continuity.py#L111)；[tests/unit/test_work_protocol_continuity.py:144 (test_gc_rechecks_object_reused_before_file_fence)](../../tests/unit/test_work_protocol_continuity.py#L144)。

### C09 代码索引

调用/持久化/恢复：[src/qq_ai_bot/runtime/work_schema_v1.py:1](../../src/qq_ai_bot/runtime/work_schema_v1.py#L1) → [migrations/versions/0104_remove_checkpoint_byte_ceiling.py:11 (upgrade)](../../migrations/versions/0104_remove_checkpoint_byte_ceiling.py#L11)。

验证入口：[tests/unit/test_work_protocol_continuity.py:192 (test_checkpoint_upgrade_removes_fixed_ceiling_and_preserves_media)](../../tests/unit/test_work_protocol_continuity.py#L192)。

### C10 代码索引

调用/持久化/恢复：[src/qq_ai_bot/memory/models.py:297 (MemoryConsistencyHealth.healthy)](../../src/qq_ai_bot/memory/models.py#L297) → [src/qq_ai_bot/memory/quality/audit.py:80 (MemoryProductionQualityAudit._checks)](../../src/qq_ai_bot/memory/quality/audit.py#L80) → [src/qq_ai_bot/memory/audit.py:121 (MemoryAuditService.health)](../../src/qq_ai_bot/memory/audit.py#L121)。

验证入口：[tests/unit/test_memory_mutation.py:154 (test_agent_can_correct_its_visible_self_memory)](../../tests/unit/test_memory_mutation.py#L154)。

### C11 代码索引

调用/持久化/恢复：[src/qq_ai_bot/memory/dream/models.py:124 (DreamAction._shape)](../../src/qq_ai_bot/memory/dream/models.py#L124) → [src/qq_ai_bot/memory/dream/service.py:791 (DreamService._select_evidence)](../../src/qq_ai_bot/memory/dream/service.py#L791) → [src/qq_ai_bot/memory/mutation/service.py:203 (MemoryMutationService.mutate_dream)](../../src/qq_ai_bot/memory/mutation/service.py#L203) → [src/qq_ai_bot/memory/mutation/service.py:877 (MemoryMutationService._dream_evidence_bundle)](../../src/qq_ai_bot/memory/mutation/service.py#L877)。

验证入口：[tests/unit/test_memory_dream.py:444 (test_recompose_is_atomic_partial_one_to_many_and_reversible)](../../tests/unit/test_memory_dream.py#L444)。

### C12 代码索引

调用/持久化/恢复：[src/qq_ai_bot/memory/repository.py:2172 (MemoryJobRepository.fail)](../../src/qq_ai_bot/memory/repository.py#L2172) → [src/qq_ai_bot/memory/worker.py:175 (MemoryWorker._process_jobs)](../../src/qq_ai_bot/memory/worker.py#L175)。

验证入口：[tests/unit/test_job_claim_transactions.py:1](../../tests/unit/test_job_claim_transactions.py#L1)。

### C13 代码索引

调用/持久化/恢复：[scripts/release_validate.py:1](../../scripts/release_validate.py#L1)。

验证入口：[tests/unit/test_work_effect_lifecycle_repository.py:1](../../tests/unit/test_work_effect_lifecycle_repository.py#L1)。

### C14 代码索引

调用/持久化/恢复：[src/qq_ai_bot/memory/embedding/jobs.py:477 (MemoryEmbeddingJobRepository.fail)](../../src/qq_ai_bot/memory/embedding/jobs.py#L477) → [src/qq_ai_bot/memory/embedding/worker.py:1](../../src/qq_ai_bot/memory/embedding/worker.py#L1) → [src/qq_ai_bot/memory/rebuild/repository.py:399 (MemoryRebuildRepository.fail_item)](../../src/qq_ai_bot/memory/rebuild/repository.py#L399) → [src/qq_ai_bot/memory/rebuild/repository.py:810 (MemoryRebuildRepository.fail_proposal)](../../src/qq_ai_bot/memory/rebuild/repository.py#L810) → [src/qq_ai_bot/memory/rebuild/service.py:681 (MemoryRebuildService.process_commit_once)](../../src/qq_ai_bot/memory/rebuild/service.py#L681)。

验证入口：[tests/unit/test_embedding_claim_boundaries.py:1](../../tests/unit/test_embedding_claim_boundaries.py#L1)；[tests/unit/test_memory_rebuild.py:1](../../tests/unit/test_memory_rebuild.py#L1)。

### C15 代码索引

调用/持久化/恢复：[src/qq_ai_bot/admin/config_service.py:661 (RuntimeConfigService.set_override)](../../src/qq_ai_bot/admin/config_service.py#L661) → [src/qq_ai_bot/admin/config_service.py:813 (RuntimeConfigService.delete_override)](../../src/qq_ai_bot/admin/config_service.py#L813) → [src/qq_ai_bot/admin/config_service.py:980 (RuntimeConfigService.rollback)](../../src/qq_ai_bot/admin/config_service.py#L980) → [src/qq_ai_bot/config.py:1](../../src/qq_ai_bot/config.py#L1) → [src/qq_ai_bot/settings_domains.py:1](../../src/qq_ai_bot/settings_domains.py#L1)。

验证入口：[tests/unit/test_runtime_config_read_snapshot.py:1](../../tests/unit/test_runtime_config_read_snapshot.py#L1)。

### C16 代码索引

调用/持久化/恢复：[src/qq_ai_bot/runtime/work_compaction.py:50 (validate_summary)](../../src/qq_ai_bot/runtime/work_compaction.py#L50) → [src/qq_ai_bot/runtime/work_context_note.py:24 (validate_note)](../../src/qq_ai_bot/runtime/work_context_note.py#L24) → [src/qq_ai_bot/runtime/work_session.py:816 (WorkSession._source_page)](../../src/qq_ai_bot/runtime/work_session.py#L816) → [src/qq_ai_bot/runtime/work_session.py:1063 (WorkSession.next_summary_source)](../../src/qq_ai_bot/runtime/work_session.py#L1063) → [src/qq_ai_bot/runtime/work_session.py:1207 (WorkSession.compact)](../../src/qq_ai_bot/runtime/work_session.py#L1207) → [src/qq_ai_bot/runtime/work_session.py:117 (WorkSession.restore)](../../src/qq_ai_bot/runtime/work_session.py#L117)。

验证入口：[tests/unit/test_work_compaction_anchor_recovery.py:1](../../tests/unit/test_work_compaction_anchor_recovery.py#L1)；[tests/unit/test_work_compaction_public_delta.py:1](../../tests/unit/test_work_compaction_public_delta.py#L1)。

### C17 代码索引

调用/持久化/恢复：[src/qq_ai_bot/services/turn_execution.py:1200 (TurnExecution.settle_truncated)](../../src/qq_ai_bot/services/turn_execution.py#L1200) → [src/qq_ai_bot/services/turn_execution.py:1288 (TurnExecution.execute_tools)](../../src/qq_ai_bot/services/turn_execution.py#L1288) → [src/qq_ai_bot/runtime/work_journal.py:744 (WorkJournal.record_effect)](../../src/qq_ai_bot/runtime/work_journal.py#L744)。

验证入口：[tests/unit/test_provider_native_result_boundaries.py:1](../../tests/unit/test_provider_native_result_boundaries.py#L1)。

### C18 代码索引

调用/持久化/恢复：[src/qq_ai_bot/services/invocation_service.py:75 (InvocationService.invoke)](../../src/qq_ai_bot/services/invocation_service.py#L75) → [src/qq_ai_bot/tool_results/artifacts.py:193 (ToolArtifactRepository.write_artifact)](../../src/qq_ai_bot/tool_results/artifacts.py#L193) → [src/qq_ai_bot/capabilities/results.py:129 (ToolResultBudgeter.render)](../../src/qq_ai_bot/capabilities/results.py#L129)。

验证入口：[tests/unit/test_runtime_recovery.py:112 (test_artifact_publication_failure_preserves_typed_effect_without_replaying_business)](../../tests/unit/test_runtime_recovery.py#L112)；[tests/unit/test_work_native_media_receipts.py:1](../../tests/unit/test_work_native_media_receipts.py#L1)。

### C19 代码索引

调用/持久化/恢复：[src/qq_ai_bot/runtime/work_control.py:797 (WorkControl._control)](../../src/qq_ai_bot/runtime/work_control.py#L797) → [src/qq_ai_bot/runtime/work_control.py:306 (WorkControl.validate_work_report)](../../src/qq_ai_bot/runtime/work_control.py#L306) → [src/qq_ai_bot/runtime/work_wait.py:306 (WorkWaitRepository._deliver)](../../src/qq_ai_bot/runtime/work_wait.py#L306) → [src/qq_ai_bot/services/main_agent_backend.py:376 (MainAgentBackend.execute_call)](../../src/qq_ai_bot/services/main_agent_backend.py#L376)。

验证入口：[tests/unit/test_no_work_after_send_recovery.py:1](../../tests/unit/test_no_work_after_send_recovery.py#L1)；[tests/unit/test_subagent_result_checkpoint.py:1](../../tests/unit/test_subagent_result_checkpoint.py#L1)。

### C20 代码索引

调用/持久化/恢复：[src/qq_ai_bot/memory/self_reflection/repository.py:429 (SelfReflectionRepository.claim_due)](../../src/qq_ai_bot/memory/self_reflection/repository.py#L429) → [src/qq_ai_bot/memory/self_reflection/service.py:316 (SelfReflectionService._input)](../../src/qq_ai_bot/memory/self_reflection/service.py#L316) → [src/qq_ai_bot/memory/self_reflection/models.py:109 (SelfReflectionProposal._shape)](../../src/qq_ai_bot/memory/self_reflection/models.py#L109) → [frontend/src/reflection.tsx:1](../../frontend/src/reflection.tsx#L1) → [src/qq_ai_bot/services/profile_commands.py:183 (ProfileCommandHandler._memory_diagnostics)](../../src/qq_ai_bot/services/profile_commands.py#L183)。

验证入口：[tests/unit/test_self_initiative_memory.py:809 (test_incoming_reflection_keeps_complete_source_without_own_reply)](../../tests/unit/test_self_initiative_memory.py#L809)。

### C21 代码索引

调用/持久化/恢复：[src/qq_ai_bot/memory/self_reflection/repository.py:181 (SelfReflectionRepository._advance_state)](../../src/qq_ai_bot/memory/self_reflection/repository.py#L181) → [src/qq_ai_bot/memory/self_reflection/repository.py:262 (SelfReflectionRepository._reconcile_generation_boundary)](../../src/qq_ai_bot/memory/self_reflection/repository.py#L262) → [src/qq_ai_bot/memory/self_reflection/repository.py:429 (SelfReflectionRepository.claim_due)](../../src/qq_ai_bot/memory/self_reflection/repository.py#L429)。

验证入口：[tests/unit/test_self_initiative_memory.py:870 (test_reflection_only_live_source_ranges_block_claim_and_cursor)](../../tests/unit/test_self_initiative_memory.py#L870)。

### C22 代码索引

调用/持久化/恢复：[src/qq_ai_bot/memory/rebuild/models.py:38 (MemoryRebuildSelection._canonical_ids)](../../src/qq_ai_bot/memory/rebuild/models.py#L38) → [src/qq_ai_bot/memory/rebuild/service.py:341 (MemoryRebuildService.start)](../../src/qq_ai_bot/memory/rebuild/service.py#L341) → [src/qq_ai_bot/admin/config_specs_restart.py:1](../../src/qq_ai_bot/admin/config_specs_restart.py#L1) → [src/qq_ai_bot/config.py:1](../../src/qq_ai_bot/config.py#L1)。

验证入口：[tests/unit/test_memory_rebuild.py:1](../../tests/unit/test_memory_rebuild.py#L1)。

### C23 代码索引

调用/持久化/恢复：[src/qq_ai_bot/codemode/driver.py:933 (CodeModeDriver._bounded_result)](../../src/qq_ai_bot/codemode/driver.py#L933) → [src/qq_ai_bot/codemode/driver.py:845 (CodeModeDriver._settle)](../../src/qq_ai_bot/codemode/driver.py#L845)。

验证入口：[tests/integration/test_codemode_output_recovery.py:1](../../tests/integration/test_codemode_output_recovery.py#L1)。

### C24 代码索引

调用/持久化/恢复：[src/qq_ai_bot/runtime/work_control.py:797 (WorkControl._control)](../../src/qq_ai_bot/runtime/work_control.py#L797) → [src/qq_ai_bot/runtime/work_control.py:1046 (WorkControl._prepare_completion)](../../src/qq_ai_bot/runtime/work_control.py#L1046) → [src/qq_ai_bot/runtime/work_repository.py:483 (WorkRepository.transition)](../../src/qq_ai_bot/runtime/work_repository.py#L483) → [src/qq_ai_bot/runtime/work_wait.py:163 (WorkWaitRepository.register)](../../src/qq_ai_bot/runtime/work_wait.py#L163)。

验证入口：[tests/unit/test_work_settlement_writer.py:285 (test_detailed_target_and_wait_reason_are_preserved)](../../tests/unit/test_work_settlement_writer.py#L285)；[tests/unit/test_subagent_result_checkpoint.py:215 (test_verified_mutation_child_completes_without_extra_result_prose)](../../tests/unit/test_subagent_result_checkpoint.py#L215)。

### C25 代码索引

调用/持久化/恢复：[src/qq_ai_bot/memory/dream/models.py:124 (DreamAction._shape)](../../src/qq_ai_bot/memory/dream/models.py#L124) → [src/qq_ai_bot/memory/mutation/service.py:203 (MemoryMutationService.mutate_dream)](../../src/qq_ai_bot/memory/mutation/service.py#L203)。

验证入口：[tests/unit/test_memory_dream.py:149 (test_single_source_dream_uses_saved_model_output_and_original_receipt)](../../tests/unit/test_memory_dream.py#L149)。

### C26 代码索引

调用/持久化/恢复：[src/qq_ai_bot/capabilities/validation.py:43 (JsonSchemaCapabilityValidator.admit)](../../src/qq_ai_bot/capabilities/validation.py#L43) → [src/qq_ai_bot/capabilities/catalog.py:187 (ToolProviderRegistry.catalog)](../../src/qq_ai_bot/capabilities/catalog.py#L187) → [src/qq_ai_bot/capabilities/runtime.py:31 (TurnCapabilityRuntime.__init__)](../../src/qq_ai_bot/capabilities/runtime.py#L31) → [src/qq_ai_bot/services/main_agent_backend.py:376 (MainAgentBackend.execute_call)](../../src/qq_ai_bot/services/main_agent_backend.py#L376)。

验证入口：[tests/unit/test_capability_runtime_security.py:1](../../tests/unit/test_capability_runtime_security.py#L1)。

### C27 代码索引

调用/持久化/恢复：[src/qq_ai_bot/runtime/work_control.py:797 (WorkControl._control)](../../src/qq_ai_bot/runtime/work_control.py#L797) → [src/qq_ai_bot/runtime/work_queries.py:136 (WorkQueries.available)](../../src/qq_ai_bot/runtime/work_queries.py#L136) → [src/qq_ai_bot/runtime/work_queries.py:101 (WorkQueries.get)](../../src/qq_ai_bot/runtime/work_queries.py#L101)。

验证入口：[tests/unit/test_work_settlement_writer.py:1](../../tests/unit/test_work_settlement_writer.py#L1)。

### C28 代码索引

调用/持久化/恢复：[src/qq_ai_bot/runtime/work_wait.py:639 (WorkWaitRepository._observe_due)](../../src/qq_ai_bot/runtime/work_wait.py#L639) → [src/qq_ai_bot/runtime/work_wait.py:306 (WorkWaitRepository._deliver)](../../src/qq_ai_bot/runtime/work_wait.py#L306) → [src/qq_ai_bot/runtime/work_control.py:797 (WorkControl._control)](../../src/qq_ai_bot/runtime/work_control.py#L797)。

验证入口：[tests/unit/test_work_settlement_writer.py:546 (test_wait_conditions_follow_mode_with_failed_member)](../../tests/unit/test_work_settlement_writer.py#L546)。

### C29 代码索引

调用/持久化/恢复：[src/qq_ai_bot/memory/dream/models.py:124 (DreamAction._shape)](../../src/qq_ai_bot/memory/dream/models.py#L124) → [src/qq_ai_bot/memory/dream/service.py:879 (DreamService._run_model)](../../src/qq_ai_bot/memory/dream/service.py#L879) → [src/qq_ai_bot/memory/dream/service.py:908 (DreamService._anchor)](../../src/qq_ai_bot/memory/dream/service.py#L908) → [src/qq_ai_bot/memory/mutation/service.py:203 (MemoryMutationService.mutate_dream)](../../src/qq_ai_bot/memory/mutation/service.py#L203)。

验证入口：[tests/unit/test_memory_dream.py:1](../../tests/unit/test_memory_dream.py#L1)。

### C30 代码索引

调用/持久化/恢复：[src/qq_ai_bot/memory/mutation/service.py:965 (MemoryMutationService.mutate_resolved)](../../src/qq_ai_bot/memory/mutation/service.py#L965) → [src/qq_ai_bot/memory/mutation/service.py:1025 (MemoryMutationService._prepare_self_origin)](../../src/qq_ai_bot/memory/mutation/service.py#L1025) → [src/qq_ai_bot/memory/mutation/service.py:1190 (MemoryMutationService._commit_prepared)](../../src/qq_ai_bot/memory/mutation/service.py#L1190) → [src/qq_ai_bot/memory/mutation/service.py:1982 (MemoryMutationService._apply)](../../src/qq_ai_bot/memory/mutation/service.py#L1982)。

验证入口：[tests/unit/test_self_initiative_memory.py:293 (test_actorless_reflection_uses_unified_mutation_and_readable_evidence)](../../tests/unit/test_self_initiative_memory.py#L293)。

### C31 代码索引

调用/持久化/恢复：[src/qq_ai_bot/model_runtime/structured.py:99 (StructuredTaskRunner.run_with_response)](../../src/qq_ai_bot/model_runtime/structured.py#L99) → [src/qq_ai_bot/model_runtime/structured.py:244 (_decode_response)](../../src/qq_ai_bot/model_runtime/structured.py#L244) → [src/qq_ai_bot/services/agent_runner.py:289 (AgentRunner._compact_work)](../../src/qq_ai_bot/services/agent_runner.py#L289) → [src/qq_ai_bot/services/ordinary_compaction.py:149 (summarize_records)](../../src/qq_ai_bot/services/ordinary_compaction.py#L149) → [src/qq_ai_bot/conversation/rollup/service.py:252 (ConversationRollupService._summarize_source)](../../src/qq_ai_bot/conversation/rollup/service.py#L252)。

验证入口：[tests/unit/test_work_compaction_anchor_recovery.py:1](../../tests/unit/test_work_compaction_anchor_recovery.py#L1)；[tests/unit/test_rollup_complete_sources.py:1](../../tests/unit/test_rollup_complete_sources.py#L1)。

### C32 代码索引

调用/持久化/恢复：[src/qq_ai_bot/services/durable_invocations.py:29 (DurableInvocations.run)](../../src/qq_ai_bot/services/durable_invocations.py#L29) → [src/qq_ai_bot/automation/handlers.py:107 (AutomationCapabilityHandlers.agent)](../../src/qq_ai_bot/automation/handlers.py#L107) → [src/qq_ai_bot/automation/executor.py:163 (AutomationExecutor._execute)](../../src/qq_ai_bot/automation/executor.py#L163) → [src/qq_ai_bot/automation/work_cursor.py:31 (save)](../../src/qq_ai_bot/automation/work_cursor.py#L31) → [src/qq_ai_bot/automation/worker.py:148 (AutomationWorker._process)](../../src/qq_ai_bot/automation/worker.py#L148) → [src/qq_ai_bot/automation/repository.py:706 (AutomationRepository.resumable_run)](../../src/qq_ai_bot/automation/repository.py#L706) → [src/qq_ai_bot/automation/repository.py:786 (AutomationRepository.finish_automation_run)](../../src/qq_ai_bot/automation/repository.py#L786)。

验证入口：[tests/integration/test_person_automation_final_feedback.py:124 (test_suspended_agent_preserves_automation_run_until_original_work_resumes)](../../tests/integration/test_person_automation_final_feedback.py#L124)。

### C33 代码索引

调用/持久化/恢复：[src/qq_ai_bot/services/chat.py:431 (ChatService._build_tool_registry)](../../src/qq_ai_bot/services/chat.py#L431) → [src/qq_ai_bot/tool_results/artifacts.py:193 (ToolArtifactRepository.write_artifact)](../../src/qq_ai_bot/tool_results/artifacts.py#L193) → [src/qq_ai_bot/tool_results/artifacts.py:351 (ToolArtifactRepository.read)](../../src/qq_ai_bot/tool_results/artifacts.py#L351) → [src/qq_ai_bot/tool_results/artifacts.py:808 (_get_json)](../../src/qq_ai_bot/tool_results/artifacts.py#L808) → [src/qq_ai_bot/tool_results/artifacts.py:923 (_search_json)](../../src/qq_ai_bot/tool_results/artifacts.py#L923)。

验证入口：[tests/unit/test_tool_artifact_orphans.py:68 (test_stored_result_can_be_read_by_deep_path_and_full_query)](../../tests/unit/test_tool_artifact_orphans.py#L68)。

### C34 代码索引

调用/持久化/恢复：[src/qq_ai_bot/memory/validation.py:133 (MemoryClaimValidator._validate_claim)](../../src/qq_ai_bot/memory/validation.py#L133) → [src/qq_ai_bot/memory/claim_processor.py:116 (MemoryClaimProcessor.resolve)](../../src/qq_ai_bot/memory/claim_processor.py#L116) → [src/qq_ai_bot/memory/claim_processor.py:178 (MemoryClaimProcessor.apply_resolution)](../../src/qq_ai_bot/memory/claim_processor.py#L178) → [src/qq_ai_bot/memory/quality/audit.py:80 (MemoryProductionQualityAudit._checks)](../../src/qq_ai_bot/memory/quality/audit.py#L80)。

验证入口：[tests/unit/test_memory_mutation.py:567 (test_self_create_is_atomic_receipted_and_deduplicated)](../../tests/unit/test_memory_mutation.py#L567)。

### C35 代码索引

调用/持久化/恢复：[src/qq_ai_bot/admin/capabilities.py:308 (AdminCapabilityService.execute)](../../src/qq_ai_bot/admin/capabilities.py#L308) → [src/qq_ai_bot/admin/config_service.py:661 (RuntimeConfigService.set_override)](../../src/qq_ai_bot/admin/config_service.py#L661) → [src/qq_ai_bot/services/main_agent_backend.py:376 (MainAgentBackend.execute_call)](../../src/qq_ai_bot/services/main_agent_backend.py#L376) → [src/qq_ai_bot/services/main_agent_backend.py:166 (MainAgentBackend.work_control_allowed)](../../src/qq_ai_bot/services/main_agent_backend.py#L166) → [src/qq_ai_bot/services/main_agent_backend.py:181 (MainAgentBackend.work_query_allowed)](../../src/qq_ai_bot/services/main_agent_backend.py#L181) → [src/qq_ai_bot/services/main_agent_backend.py:226 (MainAgentBackend.definitions)](../../src/qq_ai_bot/services/main_agent_backend.py#L226)。

验证入口：[tests/unit/test_capability_runtime_security.py:204 (test_admin_parameter_correction_can_read_before_retry_and_reuse_original_effect)](../../tests/unit/test_capability_runtime_security.py#L204)。

### C36 代码索引

调用/持久化/恢复：[src/qq_ai_bot/memory/self_reflection/initiative.py:60 (claim_initiative)](../../src/qq_ai_bot/memory/self_reflection/initiative.py#L60) → [src/qq_ai_bot/memory/self_reflection/service.py:316 (SelfReflectionService._input)](../../src/qq_ai_bot/memory/self_reflection/service.py#L316) → [src/qq_ai_bot/memory/self_reflection/repository.py:763 (SelfReflectionRepository.tool_receipts)](../../src/qq_ai_bot/memory/self_reflection/repository.py#L763) → [src/qq_ai_bot/memory/self_reflection/repository.py:832 (SelfReflectionRepository.complete)](../../src/qq_ai_bot/memory/self_reflection/repository.py#L832) → [src/qq_ai_bot/memory/self_reflection/initiative.py:26 (advance_cursor)](../../src/qq_ai_bot/memory/self_reflection/initiative.py#L26)。

验证入口：[tests/unit/test_self_initiative_memory.py:208 (test_silent_receipt_windows_keep_large_source_complete_and_remaining_source_pending)](../../tests/unit/test_self_initiative_memory.py#L208)；[tests/unit/test_self_initiative_memory.py:250 (test_silent_original_receipt_window_retries_without_reselecting_new_budget)](../../tests/unit/test_self_initiative_memory.py#L250)。

## 删除链路与原因

### Work收尾与队列

`work_control`、`work_repository`、`social.tools`、`work_resume`、`subagent_tools/repository/scheduler`与`work_wait`一起核对。reporting是交互元数据，不决定任务能不能结束；发送用途不额外充当完成资格。目标、结束原因、文件列表和已接纳事件不因经验数量或文案长度拒绝。源于这些政策的startup追踪、入队计数扫描和死装配字段同步删除。

实际执行沿既有调度器排队，登记更多工作不会建立第二套调度。等待定时器直接支持到期条件；不在它前面叠数量八、至少一秒或至多一年规则。根/子任务继续共享原累计计量，不再暗扣八次模型和八次工具额度。

### 失败恢复与协议存储

`work_supervisor`删除固定三次封口，以及只要有过效果便禁止新链的整体政策。`work_journal`删除旧delivery存在就拒绝当前上下文的两处判断。取消保留已有HTTP计数，恢复继续沿原预算和回执；没有用重置任务绕开原事实。

ProtocolStore原来对同hash对象使用INSERT忽略冲突，旧deleting标记留下；WorkSession再将其误包装为容量错误，supervisor第一次就暂停。现在原INSERT只统计真实新增对象，同一writer更新已有对象准备时间、撤标记并登记引用；GC取得已有文件锁后核对当前删除资格，不采用已过期的候选。未添加新协调器。交叉终审发现“upsert把已有对象误当新增容量”回归后，已改回仅真实INSERT计新增；现有对象大于新配额仍可复用。

JSON编码删除独立字节拒绝与未使用的limit参数；0104撤旧数据库64MiB CHECK。新迁移只改自己的计量表与原六个delta触发器，原数据和计数保留，旧迁移不被改写成伪升级。

配置入口删除回复延迟端点必须排序、单对象额度必须小于总额、摘要target必须小于trigger及心跳必须低于lease三分之一的联动政策。相关Settings、runtime配置对象、摘要后置拒绝和管理修改/删除/回滚错误包装一起清退，没有留下空校验器。

### Memory与Dream

162条旧缺链仍由原SQL诊断可查询，没有伪造后继事实或清除历史。整体healthy不再被历史谱系缺口或异步过期维护一票否决。质量audit删除旧profile、等待审核、固定一小时、终态暂存和固定权威等级排序的政策。

Dream模型到执行两层一起删除kind与operation绑定、focus/正文唯一、最多四输出、强制全集覆盖和同key SHA后缀。部分recompose仅替换实际引用的来源，操作、checkpoint和回滚沿原结果记录，未处理源保持原样。复制新事实证据不再套模型输入的采样数，移除12条、merge两条和每fact采样帽；模型输入查询也撤100000条截断。

普通提取Job删除固定三次转FAILED，已有空输出或无可接纳claim仍按原完成路径结束。原历史FAILED不改成伪成功。

最终检索继续覆盖Embedding及Rebuild：删除Embedding的attempts封口、worker/runtime传递的max_attempts和两项专属重试次数配置；Rebuild的item/proposal失败只回原pending，取消exhausted返回值和commit_prepared后固定max1政策。等待直接沿原配置initial间隔，不留下失去次数上限后的指数长等待。提交继续先核原proposal的持久COMMITTED回执；提案与事实原子提交不改为新的执行链。

## 第二轮逆向核查

继续从“缺少这个条件是否仍能沿原链完成”反查后置拒绝：摘要/备注的固定字段、版本、重复与全集要求；本地模型截断已经保存未执行配对回执之后的永久暂停；真实工具效果成功并存fallback后仅按异常类型拒绝；CodeMode必要执行身份JSON因展示软目标而拒绝；SELF无自身回复即跳过及领取后字符封口；旧generation占游标空洞；Rebuild部署总量帽；可选汇报与结束文案反过来阻断发送/结束。

摘要原件、输入账本和实际回执保持原入口。已付的pending等观察写入已有paid_observations槽位，避免换业务链时丢失；没有新增状态或调度机制。子任务等待复用原WorkWaitRepository和原通知；SELF跨generation复用原live事件谓词，不恢复被删除的来源、不创造清障记录。各项的真实验证与最后冻结状态按上表记录。

交叉终审具体复现了放宽摘要后两个问题：省略旧要求却把原输入无条件记成covered，导致恢复读原文被跳过；paid_observations的旧record别名未继承，新链还可能重用编号。现以原字段保留未被实际替代的要求、未概括输入原文与未提供的旧观察；按真实引用推进连续前缀，实际提供输入的翻页独立继续。record/observation引用携带原chain ID，并继承旧paid来源，恢复不重复呈现输入。没有恢复全集摘要拒绝。

## 第三轮沿代码索引追查

C26 从工具声明追到编译、授权投影与执行：原schema深度12、节点256、正则长度256、嵌套量词猜测和仅2020-12方言先验将合法工具隔离，最终表现成 `capability_no_longer_authorized`。现在交原JSONSchema库选择并核验；合法声明沿原授权进入实际绑定，原效果重入不再调用。库真正不能编译的schema另按原错误处理，没有新隔离框架。

C27 从提示展示追到resume：`available()`的16条列表原本被误当全部合法Work。现用已有 `get(local=True)`按原ID/source查证，最后第130项可恢复，不扩大展示列表。C28从条件信号追到timer：`member_failed`短路违反 `wait_mode=all`，现失败/取消仍记录为真实matched状态，由原any/all决定是否齐备。

C29删除没有执行消费者的Dream元数据形状门，及RECOMPOSE领域的二次拒绝；原 `_anchor()`和outputs仍决定真实写入。C30删除获准actorless自省的独立五项操作白名单，同时发现同一来源/正文的metadata、invalidate、restore被过粗 `claim_fingerprint`互相吞为deduplicated。指纹直接采用已有包含operation/fact/merge的原payload，原idempotency值不变；四个原回执分别生效、分别重入，不另建去重状态。主SELF工具能否直接写和背景模型Enum仍是P02/P09，不能用领域案例冒充两条入口已打通。

C31从已付响应追到解码：完整合法JSON因 `INCOMPLETE`标签在解码前被拒绝，影响Memory、Work摘要、ordinary摘要和Rollup。四处先验门删除，实际少右括号、非法字段或真实来源引用错误仍由原内容校验报告；三种structured mode合法案例各只付一次请求，不补买同一合法结果。

C32从Work暂停追到自动化worker和run权限：Provider认证失败使Work suspended，上层却报 `agent_work_blocked`、结束run为BLOCKED，随后原run又不再具备恢复来源资格。现在把suspended沿既有pending/cursor返回；worker释放原claim，原run保持running，两次poll没有新Provider请求。显式恢复同一Work后同一run完成，模型用量等于原Work实际计数，没有重置预算或建立第二套scheduler。

C33从已保存原件追到schema与读取：存储配置已接纳的6MiB中文JSON又被固定4MiB结构读取帽拒绝，合法深路径与原长query也另拒。已同步删除三种拒绝及声明maxItems；原大字符串按原offset/next_offset取回，过大命中对象保留可读取的原path，不把完整原件塞进模型。搜索扫描与固定页帽尚在P03，不能据此宣称Artifact限制全部删除。

C34沿真实GROUP CORRECT领域写入查到质量报表：合法 `correction + group_report`带真实quote成功创建事实，却被旧 `evidence_relation_authority_mismatch`枚举报error。已删除这一个质量政策块；源quote的真实校验与记录不改，不凭标签把本来合法事实记作损坏。

C35从管理员失败回执追到下次查询与目录：真实set参数类型错误返回validation_error，此时get已通过schema/authority却因不同操作报 `retry_scope_violation`并关闭工具。删除专属slot、匹配helper、目录/控制消费者和死字段；原同主体可get当前值，再修正set，重入原效果不产生第二次修改。

C36沿SELF工具receipt领取、模型实际呈现、原window和完成游标核查：修改前真实8k/1k两原回执在4k预算被一同领取，模型仅见4k/0；改预算重试又假报source_changed。现新窗口用真实已存excerpt长度，首条大来源完整输入；既定window按原range读取，不因新budget或limit重选；二次trim与死import一起删除。15项真实来源场景通过，未宣称主SELF写入接线已完成。

## 后续P项执行结果

### P01 合法JSON的深度政策

状态：本地实施完成；统一验证、PR与上线按最终交付记录核定。

代码索引：[src/qq_ai_bot/codemode/engine_monty.py:127 (strict_json)](../../src/qq_ai_bot/codemode/engine_monty.py#L127) → [src/qq_ai_bot/codemode/engine_monty.py:426 (MontyRun._advance)](../../src/qq_ai_bot/codemode/engine_monty.py#L426) → [src/qq_ai_bot/codemode/engine_monty.py:526 (MontyRun._completed)](../../src/qq_ai_bot/codemode/engine_monty.py#L526) → [src/qq_ai_bot/control_plane/json_types.py:14 (freeze_json_value)](../../src/qq_ai_bot/control_plane/json_types.py#L14) → [src/qq_ai_bot/control_plane/wire.py:84 (decode_command)](../../src/qq_ai_bot/control_plane/wire.py#L84)。

本地完成：strict_json及控制JSON删除自设64/32层拒绝和depth形参；65层合法资料沿原真实字节预算完整往返，过小字节预算仍给实际错误。未运行native VM，不以Host成功冒充VM验收。

### P02 主SELF记忆写入缺少来源接线

状态：本地实施完成；统一验证、PR与上线按最终交付记录核定。

代码索引：[src/qq_ai_bot/services/chat.py:1543 (ChatService.open_self_memory_session)](../../src/qq_ai_bot/services/chat.py#L1543) → [src/qq_ai_bot/memory/runtime/turn_session.py:67 (TurnMemorySession.open_self_origin)](../../src/qq_ai_bot/memory/runtime/turn_session.py#L67) → [src/qq_ai_bot/memory/runtime/capability_view.py:31 (build_capability_view)](../../src/qq_ai_bot/memory/runtime/capability_view.py#L31) → [src/qq_ai_bot/services/agent_tools.py:2024 (AgentToolService._memory_change)](../../src/qq_ai_bot/services/agent_tools.py#L2024) → [src/qq_ai_bot/memory/mutation/service.py:1025 (MemoryMutationService._prepare_self_origin)](../../src/qq_ai_bot/memory/mutation/service.py#L1025)。

本地完成：主SELF声明及执行接到原initiative/原execution真实工具回执，复用已有SELF领域事务；重复quote按实际回执顺序查来源，未消费额外ref不阻断。真ChatService主循环两次成员工具→memory_change→NO_REPLY，一条mutation receipt、实际工具证据、无真人event/发送、Work及租约收尾；第二合法群projection不阻断。

### P03 Artifact搜索无后续游标的扫描截断

状态：本地实施完成；统一验证、PR与上线按最终交付记录核定。

代码索引：[src/qq_ai_bot/tool_results/artifacts.py:923 (_search_json)](../../src/qq_ai_bot/tool_results/artifacts.py#L923) → [src/qq_ai_bot/tool_results/artifacts.py:351 (ToolArtifactRepository.read)](../../src/qq_ai_bot/tool_results/artifacts.py#L351)。

本地完成：删除64层、50000节点及固定100项帽和scan_truncated死展示。65层及晚于50001节点的真实存档查询可命中，120项请求页后续9项可完整续读；仍沿实际返回预算。

### P04 CodeMode watchdog强制长于feed

状态：本地实施完成；统一验证、PR与上线按最终交付记录核定。

代码索引：[src/qq_ai_bot/codemode/limits.py:30 (CodeModeLimits.__post_init__)](../../src/qq_ai_bot/codemode/limits.py#L30) → [src/qq_ai_bot/codemode/limits.py:40 (CodeModeLimits.from_settings)](../../src/qq_ai_bot/codemode/limits.py#L40)。

本地完成：删除watchdog/feed跨key先验顺序拒绝，5秒watchdog/10秒feed沿各自执行超时配置。构造及Host回归通过，不冒充native短代码验收。

### P05 同一canonical人跨平台账号被Work当成不同来源

状态：本地实施完成；统一验证、PR与上线按最终交付记录核定。

代码索引：[src/qq_ai_bot/runtime/work_control.py:1092 (WorkControl._queue_work)](../../src/qq_ai_bot/runtime/work_control.py#L1092) → [src/qq_ai_bot/runtime/work_activation.py:1](../../src/qq_ai_bot/runtime/work_activation.py#L1) → [src/qq_ai_bot/runtime/work_query_schema.py:11 (SOURCE_SCOPE_FIELDS)](../../src/qq_ai_bot/runtime/work_query_schema.py#L11)。

本地完成：新内部event按author_person_id核canonical所有者，来源保留该事件真实发送账号；activation/query scope去raw账号条件，恢复/记忆证据一起按canonical人查证。多binding同Person排队、恢复及合法工具调用通过，另一Person不混用。0104同步重建查询索引。

### P06 可选report kind枚举与子结果裁切

状态：本地实施完成；统一验证、PR与上线按最终交付记录核定。

代码索引：[src/qq_ai_bot/runtime/work_control.py:306 (WorkControl.validate_work_report)](../../src/qq_ai_bot/runtime/work_control.py#L306) → [src/qq_ai_bot/social/tools.py:6 (social_tool_definitions)](../../src/qq_ai_bot/social/tools.py#L6) → [src/qq_ai_bot/runtime/work_repository.py:939 (WorkRepository.communication_reports)](../../src/qq_ai_bot/runtime/work_repository.py#L939) → [src/qq_ai_bot/services/work_reporting.py:27 (stage_feedback_opportunity)](../../src/qq_ai_bot/services/work_reporting.py#L27) → [src/qq_ai_bot/runtime/subagent_repository.py:504 (SubagentRepository.finish)](../../src/qq_ai_bot/runtime/subagent_repository.py#L504) → [src/qq_ai_bot/runtime/subagent_tools.py:117 (execute_subagent)](../../src/qq_ai_bot/runtime/subagent_tools.py#L117)。

本地完成：删除report kind声明/执行/查询白名单和子结果UTF8[:12000]二次裁切。自定义kind真实回执可查，长中文子结果、父通知与原checkpoint一致；按原ToolResultBudgeter呈现。

### P07 Dream部分来源仍被视作全部占用

状态：本地实施完成；统一验证、PR与上线按最终交付记录核定。

代码索引：[src/qq_ai_bot/memory/dream/models.py:151 (DreamOutput._disjoint)](../../src/qq_ai_bot/memory/dream/models.py#L151) → [src/qq_ai_bot/memory/dream/service.py:375 (DreamService._commit_cluster_decision)](../../src/qq_ai_bot/memory/dream/service.py#L375) → [src/qq_ai_bot/memory/mutation/service.py:203 (MemoryMutationService.mutate_dream)](../../src/qq_ai_bot/memory/mutation/service.py#L203)。

本地完成：Dream DTO与事务used集合沿outputs实际消费的来源计算。A声明1/2但只使用1，B可继续处理2；真实双动作事务、原operation receipt及重新打开仓库回放通过。

### P08 Dream重复alias和未知元数据

状态：本地实施完成；统一验证、PR与上线按最终交付记录核定。

代码索引：[src/qq_ai_bot/memory/dream/models.py:99 (DreamRecomposeOutput._unique_sources)](../../src/qq_ai_bot/memory/dream/models.py#L99) → [src/qq_ai_bot/memory/dream/models.py:114 (DreamAction._unique_sources)](../../src/qq_ai_bot/memory/dream/models.py#L114) → [src/qq_ai_bot/memory/dream/models.py:52 (_DreamModel)](../../src/qq_ai_bot/memory/dream/models.py#L52)。

本地完成：输出DTO对无消费metadata放宽extra，alias沿现dict.fromkeys规范化；实际持久来源仍核原fact ID及snapshot。真实Dream输出解析/事务/重启回执覆盖重复alias和额外元数据，不粗改输入及持久类型。

### P09 背景SELF模型声明仍限制领域已实现操作

状态：本地实施完成；统一验证、PR与上线按最终交付记录核定。

代码索引：[src/qq_ai_bot/memory/self_reflection/models.py:92 (SelfReflectionProposal)](../../src/qq_ai_bot/memory/self_reflection/models.py#L92) → [src/qq_ai_bot/memory/self_reflection/models.py:92 (SelfReflectionProposal)](../../src/qq_ai_bot/memory/self_reflection/models.py#L92) → [src/qq_ai_bot/memory/self_reflection/service.py:476 (SelfReflectionService._apply)](../../src/qq_ai_bot/memory/self_reflection/service.py#L476) → [src/qq_ai_bot/memory/self_reflection/worker.py:1](../../src/qq_ai_bot/memory/self_reflection/worker.py#L1)。

本地完成：删除重复SelfReflectionOperation枚举，模型提案复用MemoryMutationOperation及原noop。RESTORE/UPDATE_METADATA沿真实Reflection模型→领域事务→checkpoint恢复通过；原已失效及争议事实可见，不凭领域测试冒充模型入口。

### P10 ordinary/Rollup摘要重复格式门槛

状态：本地实施完成；统一验证、PR与上线按最终交付记录核定。

代码索引：[src/qq_ai_bot/services/ordinary_compaction.py:28 (OrdinarySummary)](../../src/qq_ai_bot/services/ordinary_compaction.py#L28) → [src/qq_ai_bot/services/ordinary_compaction.py:149 (summarize_records)](../../src/qq_ai_bot/services/ordinary_compaction.py#L149) → [src/qq_ai_bot/conversation/rollup/summary.py:69 (_ids)](../../src/qq_ai_bot/conversation/rollup/summary.py#L69) → [src/qq_ai_bot/conversation/rollup/summary.py:75 (parse_summary)](../../src/qq_ai_bot/conversation/rollup/summary.py#L75) → [src/qq_ai_bot/conversation/rollup/service.py:252 (ConversationRollupService._summarize_source)](../../src/qq_ai_bot/conversation/rollup/service.py#L252)。

本地完成：ordinary缺省字段、无消费metadata和Rollup重复refs/固定schema/extra门放宽，空正文不假推进覆盖。真实compact/commit→新Repository→prompt回归保留未引用原聊天和调用配对；未另建纠正循环。

## 验证与交付状态

上一轮全量pytest：1104 passed、50 skipped、956 warnings，退出码0，耗时613.99秒。跳过项为本机未构建的Monty binding/worker及POSIX文件描述符场景；不记作已验收。最终追加的C14在该全量运行启动后实施，另以23项专项、44项恢复/生命周期/隐私/可信回执场景核对最终源码，各次结果分别记录，不相加冒充一次全量数量。两个旧配置及max_attempts/exhausted/指数尝试等待在Memory相关源代码、测试、现行文档和env的全链检索无残留。

真实0103→0104升级及相关协议/配置/Memory整合57项通过；跨review的原来源失效/旧回执恢复4项通过。Ruff全仓检查、644份源码Mypy、相关文件格式及diff检查通过；release_validate校验3.9.0/0104通过，仅核源码身份，未创建tag或Release。

上一轮冻结源码src/qq_ai_bot涉及44份文件：新增193行、删除832行，净删639行。唯一新增生产文件为45行0104迁移，计入后生产代码净删594行。第二轮新增删除与最终验证另记，不将上一轮检查冒充当前全部验收。既有测试同步覆盖真实恢复、排队、部分整理与升级行为；未另建测试或运行框架。现行SDK仍3.4，源码head0104；生产版本仍由实际镜像/数据库核定。

第二轮最终冻结快照统一回归：18份已改测试文件及配置读快照、SELF runtime、公开摘要增量、WebUI自省查询共430 passed、3 skipped、418 warnings，退出码0，286.10秒。仅3个本机未构建Monty binding场景跳过；实际Driver/SQLite/原父子回执案例运行通过，不冒充nativeVM验收。前端18项测试、TypeScript/Vite构建通过；全仓Ruff、76份改动Python格式检查、644份源码Mypy、release_validate及diff检查通过。该第二轮快照后继续实施C26–C36，旧验收不冒充第三轮最终源码。

第二轮冻结快照累计src/qq_ai_bot的57份文件新增355行、删除1025行，净删670行；计入45行0104迁移后后端及迁移净删625行。前端删除1个旧策略展示项，env删除4行旧配置。摘要恢复用已有材料字段保留真实未处理信息，SELF用原live查询放开旧generation占坑；没有增加替代框架、协调器或新的状态机制。C00–C25均本地完成。

第三轮冻结源码统一回归：14份相关既有测试文件共201 passed、3 skipped、174 warnings，退出码0，135.51秒。三项跳过均为本机未构建Monty binding；真实Host/SQLite/原效果与窗口回执场景已运行。不将分散专项与该统一结果相加。全仓Ruff、89份改动Python格式检查、diff检查通过；按部署目标Linux进行Mypy检查，626份源码无错误。Windows默认平台的全仓Mypy报告30个POSIX属性错误，均位于4份未改平台实现；全仓格式探测另有2份未改测试文件不符合格式，本轮未为其扩大编辑。没有把这两种探测写成全仓通过。

第三轮旧冻结快照索引：C00–C36共37项均有入口与验证位置，含136个源码锚点与48个测试锚点；加上10组明确未完成的P项，当时共222个相对链接逐一检查文件与行号。每项保留符号检索键；行号不是未来编辑后的不变身份。P01–P10当时仅为候选；现已实施并单独列结果。

第三轮旧冻结快照累计后端源码66份文件新增400行、删除1241行，净删841行；计入唯一新增的45行0104迁移后后端与迁移净删796行。该旧快照的C00–C36本地实施与各自验证已登记，P项当时未完成；本次后续执行状态以P/W结果和最后交付记录为准。

本轮尚未提交、推送、创建PR、合并或部署。不操作SnowLuma或QQ登录状态。


## 第三轮 Work 全生命周期专项

用户明确要求在数字生命研究所群（1049765710，Conversation 5b234414-7537-4f1f-8f27-d03c2c0949c7，generation 20）核验主动醒来、创建、开始、暂停、自动续跑和结束，再执行本文未完成项、同步测试/CI、终审及交付。群目标取代早先的私人会话；没有创建私人测试任务。

| 编号 | 全链缺口与删除范围 | 实施和验证状态 |
| --- | --- | --- |
| W01 | 主动SELF模型wait因派生source字段被完整JSON比较拒绝 | [x] 本地实施及对应回归完成；最终统一检查与新线上验收见交付记录 |
| W02 | expired running无新输入被判interrupted永久停住 | [x] 本地实施及对应回归完成；最终统一检查与新线上验收见交付记录 |
| W03 | 公开resume不支持原automation owner | [x] 本地实施及对应回归完成；最终统一检查与新线上验收见交付记录 |
| W04 | scheduled SELF子任务来源恢复ValueError、首次模型请求为零 | [x] 本地实施及对应回归完成；最终统一检查与新线上验收见交付记录 |
| W05 | 空final即便真实mutation已提交仍不得完成 | [x] 本地实施及对应回归完成；最终统一检查与新线上验收见交付记录 |
| W06 | active前32、initiative前128截断导致饥饿 | [x] 本地实施及对应回归完成；最终统一检查与新线上验收见交付记录 |
| W07 | 暂停/等待SELF跨Controller pending、Host busy、unique index占住新机会 | [x] 本地实施及对应回归完成；最终统一检查与新线上验收见交付记录 |
| W08 | 无消费者checkpoint quota、delivery target/window/not_before、无用outcome字段 | [x] 本地实施及对应回归完成；最终统一检查与新线上验收见交付记录 |
| W09 | 正常cancel被automation汇总为blocked/error | [x] 本地实施及对应回归完成；最终统一检查与新线上验收见交付记录 |
| W10 | yuki.agent原Work预算又被外层DSL默认配额拒绝 | [x] 本地实施及对应回归完成；最终统一检查与新线上验收见交付记录 |
| W11 | 外围强制send及无人调用的200余行二次发送重分类 | [x] 本地实施及对应回归完成；最终统一检查与新线上验收见交付记录 |
| W12 | 未归档树七天/其他暂停树封口、paused child强制新指令、子取消stale control | [x] 本地实施及对应回归完成；最终统一检查与新线上验收见交付记录 |

### 生产核验第一轮（旧镜像0046d394 / schema0103）

以下时间为2026-10-10 Asia/Taipei。均通过现运行Bot的正式控制API创建一次性自动化、原owner/主Agent/真实Provider运行；不是另一份Bot或伪造真人入站。所有测试最终释放scope租约与automation claim。SnowLuma镜像、启动时间和restart数未变。

| 路径 | 原ID与实际结果 | 判定 |
| --- | --- | --- |
| 自动创建→开始→timer wait→信号→原任务续跑→complete | automation115/run166/Work e75b7fdf-810f-48df-bf82-7e15cd18c40f；03:35:59创建、03:36:08等待、03:36:18信号、03:36:23完成；model2、send0，wait delivered、input consumed | 通过scheduled SELF；不冒充semantic主动SELF模型wait |
| 创建→need_input→公开resume→cancel | automation116/run167/Work 135e3d1f-0c1f-4a44-8a1c-e4f9605e0f47；waiting_user后resume HTTP503 operation_unavailable；正式cancel成功、model1/send0；原run却blocked/agent_work_blocked | resume与汇总失败，cancel与释放通过 |
| 父创建child→等待→结果→完成 | automation117/run168/parent393d3544-f189-4a6d-8277-3b6b76095752/child82c37d91-5311-486f-8138-c75823074416；child两次ValueError且model0，父最终取消child并自行返回12；父model10/send0 | 失败，父文本与completed不能替代child执行证据 |
| 自动创建→真实send→complete | automation118/run169/Work d8c8c2fd-13c6-4c4a-8445-7531c0bdeb5a；原effect accepted，model2/tool1/send1；固定群消息“【Work核验】自动轮发送与收尾测试完成。”仅一次 | 通过，原回执与终态共同证明 |

第一轮测试115–118均为终态，max_runs1且无next_run/claim。失败路径保留真实记录，不把数据改成成功。证据在本地ignored .cache/work-round3-*-final-snapshot.json及child日志；不提交账号token或聊天原文。

### 隔离矩阵与其边界

真实MessageProcessor/SQLite journal/effect/control API模拟数据库关闭重开、原ID恢复，确认无新输入orphan失败、公开automation resume失败、33项筛选饥饿；未知效果未重放。实际ChatService→主Runner→FakeLLM的主动SELF空来源proposal→outbox→scheduler→NO_REPLY/fail/cancel/timer核验，并发现模型wait被派生source误拒绝。19项父子控制/迟到输入/通知修复、2项归档GC、1项SELF主循环、4项原owner Job崩溃收尾合为26条API结果；现有16份定向测试分批341通过。它们不是真实Provider/QQ、OS硬杀或插件worker模型全链的通过证明。

专项源码与验证索引已按最终AST更新。部署与真实模型验收单独记录，不由本地测试代替。

### Work专项最终代码与验证索引

本地最终源码行号按AST核对；已删除门可从基线同路径diff定位。

#### W01

源码：[src/qq_ai_bot/runtime/work_wait.py:163 (WorkWaitRepository.register)](../../src/qq_ai_bot/runtime/work_wait.py#L163) → [src/qq_ai_bot/services/chat.py:1441 (ChatService._run_agent)](../../src/qq_ai_bot/services/chat.py#L1441)。

验证：[tests/integration/test_self_initiative_lifecycle.py:34 (test_intrinsic_to_real_chat_and_terminal_feedback)](../../tests/integration/test_self_initiative_lifecycle.py#L34)。

#### W02

源码：[src/qq_ai_bot/services/work_resume.py:71 (WorkResumer.resume)](../../src/qq_ai_bot/services/work_resume.py#L71) → [src/qq_ai_bot/runtime/activation_outcome.py:127 (classify_failure)](../../src/qq_ai_bot/runtime/activation_outcome.py#L127) → [src/qq_ai_bot/runtime/work_supervisor.py:69 (recover_failure)](../../src/qq_ai_bot/runtime/work_supervisor.py#L69)。

验证：[tests/unit/test_work_owner_recovery.py:104 (test_expired_original_work_recovers_without_new_input)](../../tests/unit/test_work_owner_recovery.py#L104)。

#### W03

源码：[src/qq_ai_bot/runtime/work_management.py:54 (resume_blocker)](../../src/qq_ai_bot/runtime/work_management.py#L54) → [src/qq_ai_bot/automation/worker.py:148 (AutomationWorker._process)](../../src/qq_ai_bot/automation/worker.py#L148)。

验证：[tests/integration/test_person_automation_final_feedback.py:124 (test_suspended_agent_preserves_automation_run_until_original_work_resumes)](../../tests/integration/test_person_automation_final_feedback.py#L124)。

#### W04

源码：[src/qq_ai_bot/services/execution_sources.py:149 (recover_automation_source)](../../src/qq_ai_bot/services/execution_sources.py#L149) → [src/qq_ai_bot/services/subagent_execution.py:148 (SubagentExecution.run)](../../src/qq_ai_bot/services/subagent_execution.py#L148)。

验证：[tests/integration/test_scheduled_subagent_recovery.py:1](../../tests/integration/test_scheduled_subagent_recovery.py#L1)。

#### W05

源码：[src/qq_ai_bot/runtime/work_control.py:1046 (WorkControl._prepare_completion)](../../src/qq_ai_bot/runtime/work_control.py#L1046) → [src/qq_ai_bot/automation/handlers.py:107 (AutomationCapabilityHandlers.agent)](../../src/qq_ai_bot/automation/handlers.py#L107) → [src/qq_ai_bot/automation/executor.py:163 (AutomationExecutor._execute)](../../src/qq_ai_bot/automation/executor.py#L163)。

验证：[tests/unit/test_work_settlement_writer.py:228 (test_empty_final_can_complete_answer_without_send_or_result_gate)](../../tests/unit/test_work_settlement_writer.py#L228)；[tests/integration/test_person_automation_final_feedback.py:75 (test_unsent_person_notification_final_stops_without_courtesy_correction)](../../tests/integration/test_person_automation_final_feedback.py#L75)。

#### W06

源码：[src/qq_ai_bot/runtime/work_repository.py:463 (WorkRepository.active)](../../src/qq_ai_bot/runtime/work_repository.py#L463) → [src/qq_ai_bot/conversation/autonomy_repository.py:456 (AutonomyRepository.list_active)](../../src/qq_ai_bot/conversation/autonomy_repository.py#L456)。

验证：[tests/unit/test_work_owner_recovery.py:216 (test_exact_33rd_input_is_selected_after_32_paused_works)](../../tests/unit/test_work_owner_recovery.py#L216)；[tests/unit/test_participation_feedback.py:291 (test_active_outbox_lists_all_retained_runs_past_former_128_limit)](../../tests/unit/test_participation_feedback.py#L291)。

#### W07

源码：[src/qq_ai_bot/conversation/autonomy_repository.py:198 (AutonomyRepository.accept_host_proposal)](../../src/qq_ai_bot/conversation/autonomy_repository.py#L198) → [src/qq_ai_bot/services/participation_feedback.py:1](../../src/qq_ai_bot/services/participation_feedback.py#L1) → [src/qq_ai_bot/conversation/self_initiative.py:13 (validate_self_initiative)](../../src/qq_ai_bot/conversation/self_initiative.py#L13)。

验证：[tests/unit/test_participation_feedback.py:182 (test_retained_wait_releases_new_opportunity_without_revoking_original_self)](../../tests/unit/test_participation_feedback.py#L182)。

#### W08

源码：[src/qq_ai_bot/runtime/work_recovery_schema.py:1](../../src/qq_ai_bot/runtime/work_recovery_schema.py#L1) → [src/qq_ai_bot/runtime/delivery_intents.py:19 (reserve)](../../src/qq_ai_bot/runtime/delivery_intents.py#L19) → [src/qq_ai_bot/persistence/schema_guard.py:1](../../src/qq_ai_bot/persistence/schema_guard.py#L1) → [migrations/versions/0104_remove_checkpoint_byte_ceiling.py:11 (upgrade)](../../migrations/versions/0104_remove_checkpoint_byte_ceiling.py#L11)。

验证：[tests/unit/test_work_protocol_continuity.py:192 (test_checkpoint_upgrade_removes_fixed_ceiling_and_preserves_media)](../../tests/unit/test_work_protocol_continuity.py#L192)。

#### W09

源码：[src/qq_ai_bot/automation/handlers.py:107 (AutomationCapabilityHandlers.agent)](../../src/qq_ai_bot/automation/handlers.py#L107) → [src/qq_ai_bot/automation/repository.py:786 (AutomationRepository.finish_automation_run)](../../src/qq_ai_bot/automation/repository.py#L786)。

验证：[tests/integration/test_person_automation_final_feedback.py:202 (test_public_cancel_settles_owning_run_without_error_or_new_request)](../../tests/integration/test_person_automation_final_feedback.py#L202)。

#### W10

源码：[src/qq_ai_bot/automation/validator.py:74 (AutomationValidator.validate)](../../src/qq_ai_bot/automation/validator.py#L74) → [src/qq_ai_bot/automation/executor.py:951 (AutomationExecutor._enforce_runtime_limits)](../../src/qq_ai_bot/automation/executor.py#L951)。

验证：[tests/integration/test_person_automation_final_feedback.py:84 (test_agent_script_uses_original_work_budget_without_outer_flag)](../../tests/integration/test_person_automation_final_feedback.py#L84)。

#### W11

源码：[src/qq_ai_bot/runtime/effect_queries.py:24 (RuntimeEffectQueries.inspect_social_operation)](../../src/qq_ai_bot/runtime/effect_queries.py#L24) → [src/qq_ai_bot/automation/executor.py:163 (AutomationExecutor._execute)](../../src/qq_ai_bot/automation/executor.py#L163)。

验证：[tests/unit/test_work_effect_lifecycle_repository.py:1](../../tests/unit/test_work_effect_lifecycle_repository.py#L1)。

#### W12

源码：[src/qq_ai_bot/runtime/subagent_repository.py:33 (SubagentRepository.reopen_parent)](../../src/qq_ai_bot/runtime/subagent_repository.py#L33) → [src/qq_ai_bot/runtime/subagent_repository.py:144 (SubagentRepository.acquire)](../../src/qq_ai_bot/runtime/subagent_repository.py#L144) → [src/qq_ai_bot/runtime/subagent_repository.py:483 (SubagentRepository.cancel)](../../src/qq_ai_bot/runtime/subagent_repository.py#L483) → [src/qq_ai_bot/runtime/subagent_tools.py:117 (execute_subagent)](../../src/qq_ai_bot/runtime/subagent_tools.py#L117)。

验证：[tests/unit/test_subagent_result_checkpoint.py:256 (test_retained_completed_tree_reopens_original_budget_after_eight_days_with_paused_root)](../../tests/unit/test_subagent_result_checkpoint.py#L256)；[tests/unit/test_subagent_result_checkpoint.py:316 (test_paused_child_resumes_original_goal_without_a_new_instruction)](../../tests/unit/test_subagent_result_checkpoint.py#L316)；[tests/unit/test_subagent_result_checkpoint.py:343 (test_child_cancellation_retires_accepted_control_and_retains_real_receipts)](../../tests/unit/test_subagent_result_checkpoint.py#L343)。

## 最终冻结验证与交付

C00–C36、P01–P10、W01–W12本地执行完成，索引行逐项标记。新增SELF/scheduled来源接线沿原主Runner、领域事务及持久回执；没有新增调度器、重试框架或伪造真人事件。控制器依赖PR16已合并，精确pin855f407d61590bcd689e2535c0c30b867560aa66，实际安装与依赖205项/CI均通过。

统一最终pytest：1239 passed、50 skipped、1073 warnings，退出0，658.10秒。49项原生Monty未构建、1项POSIX文件描述符场景跳过；不记作native验收。第一遍2项失败记录保留：CodeMode控制fixture漏真实canonical Person、独立Memory升级测试冻结全表布局；各自修正且最终统一通过。全仓Ruff、123份改动Python格式、Linux Mypy644源码、diff检查、release_validate3.9.0/0104通过；前端18项测试及TypeScript/Vite构建通过。

CI逐项回看quality/release：现行Python/前端全量入口已覆盖修正回归，没有旧删除政策独立workflow或多余新增job；原生CodeMode与direct默认部署分开，不增加耗资源VM构建。工作树src共91文件+933/-1933，净删1000行；加已跟踪0056迁移净增6行和唯一新增40行0104后，生产代码与迁移净删954行。计数不含文档、测试和依赖仓库。

0103→0104真实升级保留原媒体和事实，删除无消费者quota/trigger、投递死列/窗口索引及主动轮单活动索引，原索引更新到canonical Person；合法automation取消如实为cancelled且step/run均无error。所有269个源码/测试锚点检查文件/行号存在；删除前条件可按基线fa505a8c定位。交叉终审去掉多projection、mutable goal、重复quote、子任务误算busy及保留树时间/顺序门，实际来源、未知效果和原ID恢复反例均有回归。

Yuki PR、合并、本地镜像部署和生产复测将在后续交付记录填写；旧线上测试失败不改写成成功。
