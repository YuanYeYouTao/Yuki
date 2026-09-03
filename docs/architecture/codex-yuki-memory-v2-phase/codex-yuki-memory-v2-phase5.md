> **历史设计/非现行合同。** 本文保留当时的方案或实测口径，不用于当前授权、调度或使用率判断。
> 当前实现以 [Memory 架构](../memory-v2.md)、[检索合同](../memory-v2-retrieval.md)
> 和 [P1 治理任务书](../Yuki-Memory-P1治理任务书.md) 为准；本文不在当前实施导航内。

# Codex 任务：Yuki Memory V2 第五阶段——从事件账本受控重建

> 实施状态：已在 `3.0.0rc1` 完成；Alembic head 为 `0024`。

你是一名资深 Python、SQLAlchemy、SQLite、异步后台任务、结构化 LLM 工作流、事件溯源、断点续跑、数据审阅、历史事实重建、RAG 与对话记忆架构工程师。

请在仓库：

`YuanYeYouTao/Yuki-QQbot`

当前最新 `main` 基础上开发：

`Yuki-QQbot 3.0.0rc1`

本版本对应：

`docs/architecture/memory-v2-roadmap.md`

中的：

`阶段五：从事件账本受控重建`

当前预期基线是：

- 版本 `3.0.0b2`
- 最新提交包含 `feat(memory): add conflict-aware lifecycle`
- Alembic head 为 `0023`
- Memory V2 第一至第四阶段已经完成

---

## 一、任务性质

本版本为 Memory V2 增加一个**显式启动、可预览、可暂停、可审阅、可提交、可断点续跑**的历史重建系统。

它只读取永久事件账本：

```text
chat_events
```

并通过现有 Memory V2 身份安全链路重新提取历史事实。

它不读取：

- Memory V1 表；
- 旧导出文件；
- Prompt 里的历史摘要；
- 其他用户的向量结果；
- 外部知识库；
- 网页；
- MCP；
- 插件私有存储。

本版本绝对不能在：

- Alembic 升级；
- 应用启动；
- Bot 重启；
- Memory Worker 启动；

时自动开始历史重建。

只有真实 `SUPERUSERS` 通过确定性命令或管理员 Tool Kernel 明确创建并启动某个 rebuild run，才允许执行。

---

## 二、前置条件

开始开发前必须确认当前仓库已经完整具备：

### Memory V2 核心

- `memory_facts`
- `memory_evidence`
- `memory_jobs`
- `MemoryFactService`
- `MemoryWorker`
- `SubjectResolver`
- `MemoryClaimValidator`
- `MemoryTemporalResolver`
- `MemoryConflictCandidateResolver`
- `MemoryRelationClassifier`
- `MemoryResolutionPolicy`

### 查询与索引

- `memory_facts_fts`
- 查询驱动 FTS 检索
- Qwen Embedding 派生索引
- 混合 RAG
- 人物与群硬过滤

### 冲突与生命周期

- `active / contested / superseded / invalidated`
- authority
- 多证据聚合
- `memory_fact_relations`
- `memory_fact_state_events`
- 第三方事实仅来自真实 mention/reply
- `MemoryMaintenanceWorker`
- Memory Audit

如果以上任一关键能力不存在：

1. 记录真实缺失项；
2. 停止第五阶段实现；
3. 不在旧版本上额外复制一套临时重建逻辑；
4. 不绕过已有 Memory V2 领域服务直接写数据库。

---

## 三、开始前必须阅读

至少阅读：

- `docs/architecture/memory-v2-roadmap.md`
- `docs/architecture/memory-v2.md`
- `docs/architecture/memory-v2-retrieval.md`
- `docs/architecture/memory-v2-embedding.md`
- `docs/architecture/memory-v2-conflicts.md`
- `docs/architecture/memory-v2-lifecycle.md`
- `docs/architecture/memory-v2-third-party-facts.md`
- `src/qq_ai_bot/memory/enums.py`
- `src/qq_ai_bot/memory/models.py`
- `src/qq_ai_bot/memory/extraction.py`
- `src/qq_ai_bot/memory/subjects.py`
- `src/qq_ai_bot/memory/validation.py`
- `src/qq_ai_bot/memory/temporal.py`
- `src/qq_ai_bot/memory/candidates.py`
- `src/qq_ai_bot/memory/classifier.py`
- `src/qq_ai_bot/memory/resolution.py`
- `src/qq_ai_bot/memory/service.py`
- `src/qq_ai_bot/memory/repository.py`
- `src/qq_ai_bot/memory/worker.py`
- `src/qq_ai_bot/memory/audit.py`
- `src/qq_ai_bot/memory/maintenance.py`
- `src/qq_ai_bot/memory/embedding/`
- `src/qq_ai_bot/persistence/event_repository.py`
- `src/qq_ai_bot/persistence/repository_helpers.py`
- `src/qq_ai_bot/persistence/repository_records.py`
- `src/qq_ai_bot/persistence/models.py`
- 当前 MessageProcessor 中 Memory Job 入队边界
- 当前 `/ai forgetme`
- 当前管理员记忆命令
- 当前 Tool Kernel 管理员工具
- 当前 LifecycleRegistry
- 当前 RuntimeConfig 和 Settings
- 当前全部 Memory V2 测试
- Alembic `0020` 至 `0023`

开始前记录：

1. 当前 HEAD commit。
2. 当前项目版本。
3. 当前 Alembic head。
4. 当前 Memory V2 包结构。
5. 当前 Memory Worker 的提取与提交流程。
6. 当前 live Memory Job 的入队资格。
7. 当前 EventRecord 可用字段。
8. 当前 MemoryFactService 的事务边界。
9. 当前 FTS 与 Embedding 同步方式。
10. 当前 Memory V2 测试数量。
11. 当前完整质量检查结果。

---

## 四、核心目标

本版本必须实现：

1. `memory_rebuild_runs`。
2. 历史事件范围规划。
3. 无 LLM 的 dry-run。
4. 事件快照。
5. 逐事件身份安全提取。
6. 暂存 claim，不立即修改事实。
7. 分页审阅。
8. 批准或拒绝 proposal。
9. 按历史顺序提交批准的 proposal。
10. 复用实时 Memory Worker 的提取组件。
11. 复用实时 Memory V2 的候选、分类、策略和事实服务。
12. 暂停。
13. 恢复。
14. 取消。
15. 进程重启后的持久状态。
16. 断点续跑。
17. 事件级幂等。
18. proposal 级幂等。
19. 模型用量和统计。
20. 与 `/ai forgetme` 一致的隐私删除。
21. 与 FTS 和 Embedding 一致的派生索引更新。
22. 历史事实不得覆盖时间上更新的当前事实。
23. 不自动重建。
24. 不重新引入 Memory V1。
25. 不建立第二套事实系统。

---

## 五、总体流程

```text
超级管理员创建 selection
        ↓
MemoryRebuildPlanner
        ↓
无模型 dry-run
        ↓
memory_rebuild_runs(status=planned)
        ↓
超级管理员 start
        ↓
MemoryRebuildWorker：extract phase
        ↓
EventLedger 分页扫描固定快照
        ↓
MemoryEventExtractor
        ↓
SubjectResolver + ClaimValidator
        ↓
memory_rebuild_proposals
        ↓
run status = review
        ↓
超级管理员审阅、批准、拒绝
        ↓
超级管理员 commit
        ↓
MemoryRebuildWorker：commit phase
        ↓
按 occurred_at / event_id / claim_index 排序
        ↓
MemoryClaimProcessor
        ↓
CandidateResolver
        ↓
必要时 MemoryRelationClassifier
        ↓
HistoricalResolutionGuard
        ↓
MemoryResolutionPolicy
        ↓
MemoryFactService
        ↓
事实、证据、关系、状态事件
        ↓
FTS 触发器 + Embedding Job
        ↓
event receipt
        ↓
run status = completed
```

---

## 六、关键语义

### 6.1 plan 不调用模型

`plan` 只做：

- 规范化 selection；
- 固定事件快照；
- 统计匹配事件；
- 统计已处理事件；
- 统计可重建事件；
- 统计消息字符数；
- 估计 extraction 请求数量；
- 创建 planned run。

`plan` 不得：

- 调用 Memory Extraction 模型；
- 调用 Memory Consolidation 模型；
- 写入 MemoryFact；
- 写入 MemoryEvidence；
- 创建 Embedding；
- 修改 live `memory_jobs`；
- 自动 start。

### 6.2 extract 只产生 proposal

extract 阶段：

- 调用 `ModelTask.MEMORY_EXTRACTION`；
- 使用当前 SubjectResolver；
- 使用当前 ClaimValidator；
- 使用当前 TemporalResolver；
- 将合法 claim 暂存；
- 不解析冲突候选；
- 不调用 `MEMORY_CONSOLIDATION`；
- 不修改事实；
- 不创建 evidence；
- 不创建 embedding job。

### 6.3 review 审阅的是 claim

管理员审阅：

- 主体；
- 作用域；
- operation；
- kind；
- memory_key；
- category；
- content；
- confidence；
- authority；
- 时间范围；
- 来源事件摘要。

review 不承诺最终数据库 action。

最终：

```text
create
confirm
supersede
contest
invalidate
noop
```

必须在 commit 时根据**当前数据库事实状态**重新计算。

### 6.4 commit 才修改事实

commit 阶段：

- 重新加载真实 source event；
- 重新验证 subject；
- 重新验证 claim；
- 重新解析当前候选；
- 必要时调用 relation classifier；
- 应用 HistoricalResolutionGuard；
- 应用当前 MemoryResolutionPolicy；
- 调用 MemoryFactService；
- 记录实际 action 和 fact_id；
- 完成 event receipt。

---

## 七、共享处理管线重构

当前 `MemoryWorker` 将以下逻辑集中在一个类中：

- 构造 extraction input；
- 调用 extraction model；
- 验证 claim；
- 查候选；
- 调 relation classifier；
- 生成 resolution plan；
- 调 MemoryFactService。

本阶段必须提取两个共享组件。

### 7.1 `MemoryEventExtractor`

建议接口：

```python
class MemoryEventExtractor:
    async def extract(
        self,
        event: EventRecord,
        *,
        context_limit: int,
        processing_source: MemoryProcessingSource,
    ) -> MemoryEventExtractionResult:
        ...
```

返回：

```text
event_id
claims
model
prompt_fingerprint
schema_fingerprint
input_characters
usage
```

职责：

- 构造主事件；
- 读取同一精确 conversation 的更早上下文；
- 构造 available_subjects；
- 调用 Memory Extraction；
- 返回原始严格 claim。

不负责：

- 写事实；
- 查候选；
- 冲突分类；
- 事实状态。

实时 Worker 和 Rebuild Worker 必须共同使用它。

### 7.2 `MemoryClaimProcessor`

建议接口：

```python
class MemoryClaimProcessor:
    async def process(
        self,
        claim: MemoryClaim,
        *,
        event: EventRecord,
        processing_context: MemoryProcessingContext,
    ) -> MemoryClaimProcessingResult:
        ...
```

职责：

- claim validation；
- temporal resolution；
- candidate resolution；
- relation classification；
- resolution policy；
- historical guard；
- MemoryFactService；
- 返回实际 action 和 fact。

实时 Worker 和 rebuild commit 必须共同使用它。

### 7.3 `MemoryProcessingContext`

至少包含：

```text
source = live / rebuild
rebuild_run_id
rebuild_proposal_id
source_event_time
capacity_policy
```

模型不能填写。

### 7.4 实时行为保持

重构后 live Memory Worker 行为必须保持：

- 每个事件独立；
- 当前主事件是唯一证据；
- live claim 立即提交；
- live job 正常重试；
- live job 不进入 rebuild review；
- 现有 live Memory 测试继续通过。

禁止维护：

```text
live pipeline
rebuild pipeline
```

两套独立冲突和写入实现。

---

## 八、新增领域枚举

至少增加：

### `MemoryProcessingSource`

```text
live
rebuild
```

### `MemoryRebuildRunStatus`

```text
planned
extracting
extraction_paused
review
committing
commit_paused
completed
cancelled
failed
```

### `MemoryRebuildItemStatus`

```text
pending
extracting
staged
no_claims
skipped
failed
committed
```

### `MemoryRebuildReviewStatus`

```text
pending
approved
rejected
```

### `MemoryRebuildCommitStatus`

```text
pending
committed
skipped
failed
```

### `MemoryRebuildJobOutcome`

```text
claims_applied
no_claims
all_rejected
already_processed
```

### `MemoryRebuildThirdPartyMode`

```text
disabled
trusted_metadata
```

### `MemoryRebuildExpiredClaimPolicy`

```text
skip
stage_invalidated
```

不要使用自由字符串替代稳定状态。

---

## 九、Run 状态机

允许转换：

```text
planned
→ extracting
→ extraction_paused
→ extracting
→ review
→ committing
→ commit_paused
→ committing
→ completed
```

任意非 completed 状态可转：

```text
cancelled
```

不可恢复错误可转：

```text
failed
```

限制：

1. planned 才能 start。
2. extracting 才能 pause extraction。
3. extraction_paused 才能 resume extraction。
4. review 中才能 approve/reject。
5. review 中且无 pending proposal 才能 commit。
6. committing 才能 pause commit。
7. commit_paused 才能 resume commit。
8. completed/cancelled 不得重新 start。
9. cancelled 后已提交事实不自动撤销。
10. failed 只能通过明确 retry/recover 操作恢复。
11. 不允许同时运行两个 extracting/committing run。
12. planned 和 review run 可以同时存在。
13. 状态转换必须由 Repository 条件更新保证。
14. 不能只靠 Python 内存状态。

---

## 十、数据库迁移

创建下一条 Alembic 迁移。

当前预期 head：

```text
0023
```

本阶段预计：

```text
0024
```

以真实 head 为准。

迁移必须是非破坏性的。

### 10.1 `memory_rebuild_runs`

至少包含：

```text
id
public_id
status
selection_json
selection_hash
snapshot_max_event_id
snapshot_created_at
scan_checkpoint_occurred_at
scan_checkpoint_event_id
commit_checkpoint_event_id
commit_checkpoint_claim_index
created_by_user_id
extraction_fingerprint
plan_statistics_json
error_category
created_at
updated_at
started_at
review_ready_at
commit_started_at
completed_at
cancelled_at
```

约束：

- public_id 唯一；
- status 使用稳定枚举；
- selection_hash 非空；
- snapshot_max_event_id 为正整数或 0；
- created_by_user_id 指向 people；
- 不保存 API Key；
- 不保存模型原始响应；
- 不保存完整 conversation context。

索引：

```text
status + created_at
public_id
created_by_user_id
```

### 10.2 `memory_rebuild_items`

至少包含：

```text
id
run_id
event_id
status
source_event_hash
attempts
claim_count
error_category
created_at
updated_at
```

约束：

- run_id + event_id 唯一；
- run 删除时 item 删除；
- event 删除时 item 删除；
- status 使用稳定枚举；
- attempts 非负；
- source_event_hash 非空。

索引：

```text
run_id + status + event_id
event_id
```

### 10.3 `memory_rebuild_proposals`

至少包含：

```text
id
run_id
item_id
claim_index
claim_json
claim_hash
scope_type
subject_user_id
group_id
operation
kind
authority
confidence
review_status
commit_status
actual_fact_id
actual_action
actual_reason_code
attempts
error_category
created_at
updated_at
reviewed_at
committed_at
```

约束：

- item_id + claim_index 唯一；
- run 删除时 proposal 删除；
- item 删除时 proposal 删除；
- subject_user_id 可为空；
- group_id 可为空；
- actual_fact_id 可为空，事实删除时 SET NULL；
- confidence 0 到 1；
- review_status、commit_status 使用稳定枚举；
- claim_json 是经过 ClaimValidator 的 canonical JSON；
- 不保存模型原始输出；
- 不保存完整 source message；
- 不保存 conversation context。

索引：

```text
run_id + review_status
run_id + commit_status
item_id + claim_index
subject_user_id
group_id
```

### 10.4 扩展 `memory_jobs`

增加：

```text
processing_source
rebuild_run_id
outcome
completed_at
```

语义：

- 现有行迁移为 `processing_source=live`；
- rebuild commit 完成事件后写 `processing_source=rebuild`；
- rebuild_run_id 指向 rebuild run，run 删除时 SET NULL；
- outcome 记录事件处理结果；
- status=done 继续是“该事件已完成记忆处理”的权威 receipt。

不要新建第二张与 `memory_jobs` 重复的事件 receipt 表。

### 10.5 downgrade

本阶段可以 downgrade：

- 删除 proposal；
- 删除 item；
- 删除 run；
- 删除 memory_jobs 新列。

downgrade 不删除：

- facts；
- evidence；
- relations；
- state events；
- FTS；
- Embedding；
- live memory jobs 原有字段。

如果存在仍处于 extracting/review/committing 的 run，downgrade 必须明确拒绝。

---

## 十一、Rebuild Selection

定义严格模型：

```text
MemoryRebuildSelection
```

至少支持：

```text
all_events
bot_user_ids
scope_types
sender_user_ids
group_ids
after
before
minimum_event_id
maximum_event_id
maximum_events
include_failed_live_jobs
third_party_mode
expired_claim_policy
```

### 11.1 范围语义

- `all_events=false` 时至少提供一个范围条件；
- `all_events=true` 表示显式选择整个可用事件账本；
- after/before 使用 ISO 8601；
- minimum/maximum_event_id 使用整数；
- scope_types 仅 private/group；
- group_ids 只对 group 生效；
- sender_user_ids 是真实发送者过滤；
- maximum_events 为空表示不增加数量限制；
- 不静默 clamp。

### 11.2 固定条件

所有 rebuild event 必须：

```text
direction = inbound
sender 不是 Bot
sender_user_id != bot_user_id
content 非空
event 仍存在
```

默认只处理与 live Memory 入队资格一致的事件。

### 11.3 已处理事件

默认排除：

- memory_jobs.status=done；
- memory_jobs.status=pending；
- memory_jobs.status=processing。

`include_failed_live_jobs=true` 时才允许处理 failed live job。

### 11.4 事件快照

plan 时记录：

```text
snapshot_max_event_id
snapshot_created_at
```

之后：

- 新插入事件 ID 大于 snapshot_max_event_id，不属于该 run；
- selection 不可修改；
- resume 不扩大范围；
- commit 不扫描新事件。

---

## 十二、统一事件资格策略

新增：

```text
MemoryEventEligibilityPolicy
```

实时 Memory Job enqueue 和 Rebuild Planner 必须共享同一领域规则。

它至少判断：

- inbound；
- human sender；
- non-empty content；
- allowed origin；
- exact scope；
- not self message。

SQL 扫描必须实现同等条件。

增加测试验证：

```text
domain eligibility
=
repository SQL eligibility
```

不要让 live 和 rebuild 对同一事件给出不同资格结论。

---

## 十三、EventLedger 分页 API

扩展 EventLedgerRepository。

新增类似：

```python
async def count_rebuild_candidates(
    selection: MemoryRebuildSelection,
    *,
    snapshot_max_event_id: int,
) -> MemoryRebuildPlanStatistics:
    ...

async def list_rebuild_candidates(
    selection: MemoryRebuildSelection,
    *,
    snapshot_max_event_id: int,
    after_occurred_at: datetime | None,
    after_event_id: int | None,
    limit: int,
) -> tuple[EventRecord, ...]:
    ...
```

要求：

1. 使用 keyset pagination。
2. 排序：

```text
occurred_at ASC
id ASC
```

3. 不使用 OFFSET 扫描大表。
4. 查询只返回当前 selection。
5. 排除已处理 memory jobs。
6. 不一次加载全部事件。
7. count 和 list 使用同一过滤构造器。
8. 新事件被 snapshot_max_event_id 排除。
9. 删除事件后自然不再返回。
10. 不把完整事件正文写入 plan statistics。

---

## 十四、Dry-run Plan

命令：

```text
/ai memory rebuild plan <selection-json>
```

plan 必须：

1. 验证 selection。
2. 记录当前 snapshot_max_event_id。
3. 计算：
   - ledger 匹配事件数；
   - eligible 数；
   - live done 数；
   - live pending/processing 数；
   - failed 数；
   - private/group 数；
   -总输入字符数；
   - 最早和最晚事件时间；
   - 预计 extraction requests。
4. 创建 planned run。
5. 返回 public run ID。
6. 不创建 item。
7. 不创建 proposal。
8. 不调用任何模型。
9. 不写 facts/evidence。
10. 不创建 embedding job。

不要把字符数声称为 Token 数。

只有 Provider 真实返回 usage 后才能记录 Token。

---

## 十五、Source Event Fingerprint

新增：

```text
MemorySourceEventFingerprint
```

使用 canonical JSON + SHA-256。

至少包含：

- event id；
- bot_user_id；
- platform_message_id；
- scope_type；
- sender_user_id；
- group_id；
- private_peer_user_id；
- direction；
- content；
- occurred_at；
- origin；
- mentioned_user_ids；
- reply_sender_user_id。

排除：

- visual_summary；
- derived cache；
- mutable分析字段。

extract 时保存 hash。

commit 时：

1. 重新加载 event；
2. 重新计算 hash；
3. 不一致则 proposal 标记 failed/stale；
4. 不写事实；
5. event 不存在则 proposal/item 被跳过；
6. 不从 proposal 里的旧正文伪造 evidence。

---

## 十六、历史 mention/reply 解析

Rebuild 必须继续使用当前 `SubjectResolver`。

### 16.1 trusted metadata

`third_party_mode=trusted_metadata` 时，第三方主体只来自：

1. 持久化 `yuki_context.mentioned_user_ids`；
2. 持久化 `yuki_context.reply_sender_user_id`；
3. 可选的确定性历史兼容解析：
   - OneBot `at` segment 中的数字 QQ；
   - `reply_to_message_id` 对应同一个 Bot、同一个精确 conversation 的真实事件作者。

### 16.2 禁止

禁止：

- 从正文名字猜人；
- 从昵称全库搜索；
- 从 FTS 找人物；
- 从向量相似度找人物；
- 跨群解析 reply；
- 将 Bot 作为第三方主体；
- 将私聊中的第三方文本写为人物事实。

### 16.3 disabled

`third_party_mode=disabled` 时，available subjects 只有：

```text
speaker
group
```

---

## 十七、Extraction 阶段

命令：

```text
/ai memory rebuild start <run_id>
```

进入：

```text
extracting
```

### 17.1 扫描

Worker：

- 从 checkpoint 后读取下一页；
- upsert rebuild item；
- 设置 extracting；
- 调用共享 `MemoryEventExtractor`；
- 逐 claim 验证；
- 写 proposal；
- 更新 item；
- 更新 checkpoint。

### 17.2 提取上下文

与 live Worker 一致：

```text
同一个 Bot
同一个精确群或私聊
只读取主事件之前
有界数量
```

上下文只辅助消歧。

不能从 context 单独产生 proposal。

### 17.3 source_type

对于历史 claim：

```text
模型 explicit
→ source_type=explicit

模型 automatic
→ source_type=rebuild
```

不要把历史自动 claim 伪装成 live automatic。

### 17.4 evidence relation

commit 时：

- explicit → explicit_command；
- correct → correction；
- retract → retraction；
- 普通历史陈述 → rebuild。

authority 仍由主体关系决定：

- speaker → self_report；
- group → group_report；
- mentioned/reply → third_party；
- explicit → explicit。

### 17.5 不调用 consolidation

Extraction 阶段不得：

- 查冲突候选；
- 调 relation classifier；
- 生成数据库 resolution plan；
- 预测实际 fact_id；
- 修改 facts。

### 17.6 空结果

无合法 claim：

```text
item.status = no_claims
```

仍保留该 event item，等待 commit 阶段写入 event receipt。

---

## 十八、Proposal 内容

Proposal 只保存经过后端验证的 claim。

必须保存：

- operation；
- subject_ref；
- resolved scope；
- resolved subject_user_id；
- resolved group_id；
- kind；
- memory_key；
- category；
- content；
- importance；
- confidence；
- source_type；
- authority；
- temporal_mode；
- valid_from；
- valid_until；
- source event ID；
- source event hash；
- claim hash。

不保存：

- 模型原始响应；
- conversation context；
- candidate facts；
-向量；
- classifier 输出；
- resolution plan；
- API Key；
-隐藏推理。

---

## 十九、Extraction Fingerprint

每个 run 固定一个：

```text
extraction_fingerprint
```

至少由以下组成：

- ModelTask；
-实际模型名称；
- extraction prompt version；
- extraction Schema version；
- SubjectResolver version；
- ClaimValidator version；
- source adaptation version。

如果 extracting/paused 期间 fingerprint 发生变化：

- 不允许继续混合写入同一 run；
- run 转 extraction_paused；
- error_category=extraction_fingerprint_changed；
- 管理员创建新 run 或明确重新开始。

不要在同一 run 混用两个提取契约。

---

## 二十、Review 阶段

Extraction 完成后：

```text
run.status = review
```

所有 proposal 默认：

```text
review_status = pending
```

### 20.1 review 命令

```text
/ai memory rebuild review <run_id> [page]
```

输出有界：

- proposal ID；
- event time；
- event ID；
- sender；
- group；
- subject；
- scope；
- operation；
- kind；
- key；
- content；
- confidence；
- authority；
- temporal range；
- source excerpt；
- review status。

source excerpt 来自 source event，使用配置长度。

不显示：

- 完整 conversation context；
-模型 Prompt；
-模型隐藏内容；
-其他人的无关消息。

### 20.2 approve/reject

```text
/ai memory rebuild approve <run_id> <all|proposal_ids|filter-json>
/ai memory rebuild reject <run_id> <all|proposal_ids|filter-json>
```

支持过滤：

- scope；
- operation；
- kind；
- authority；
- group；
- subject；
- confidence 范围。

要求：

- approve/reject 只在 review；
- 每个 proposal 只能有一个 review 状态；
- 再次执行同样操作幂等；
- review actor 和时间记录；
- 不修改 claim 内容；
- 不允许 approve 后修改 subject。

### 20.3 commit 前置

只有：

```text
pending proposal count = 0
```

才允许 commit。

管理员可以显式：

```text
approve all
reject all
```

本版本不自动批准 proposal。

---

## 二十一、Commit 顺序

命令：

```text
/ai memory rebuild commit <run_id>
```

批准的 proposal 必须按：

```text
source_event.occurred_at ASC
source_event.id ASC
claim_index ASC
```

提交。

不能按：

- proposal 创建时间；
- proposal ID；
-审核时间；
- importance；
- confidence；

改变历史顺序。

同一个 event 的所有批准 proposal 必须连续处理。

---

## 二十二、Commit 阶段

对于每个批准 proposal：

1. 重新加载 source event。
2. 验证 source event hash。
3. 验证 event 资格。
4. 验证 event 尚未被 live/rebuild receipt 完成。
5. 重新构造 MemoryClaim。
6. 重新运行 SubjectResolver。
7. 确认 resolved target 与 proposal 一致。
8. 重新运行 ClaimValidator。
9. 重新运行 TemporalResolver。
10. 调用共享 `MemoryClaimProcessor`。
11. 查当前事实候选。
12. 必要时调用 `MEMORY_CONSOLIDATION`。
13. 应用 HistoricalResolutionGuard。
14. 应用 MemoryResolutionPolicy。
15. 调用 MemoryFactService。
16. 记录实际 fact_id、action、reason。
17. proposal commit_status=committed。

Review 批准的是 claim，而不是旧的 resolution preview。

Commit 必须根据当前数据库状态重新计算最终 action。

---

## 二十三、HistoricalResolutionGuard

新增：

```text
MemoryHistoricalResolutionGuard
```

这是本阶段的关键组件。

### 23.1 禁止时间倒置

若候选事实：

```text
candidate.last_confirmed_at > historical_claim.occurred_at
```

则历史 claim 不能：

- supersede 该候选；
- invalidate 该候选；
- 将该候选从 clear 改为 contested；
- 降低该候选 authority；
- 将 last_confirmed_at 向过去移动。

### 23.2 相同历史证据

历史 claim 与当前事实相同：

```text
追加 rebuild evidence
重新聚合 confidence
last_confirmed_at = max(current, historical_event_time)
保持当前状态
```

### 23.3 历史旧版本

历史 claim 表示一个较早、后来已变化的状态：

- 可以创建 `superseded` 历史 fact；
- 不占 active 唯一槽位；
- 可以建立 refines/contradicts 关系；
- 不改变较新 active fact；
- reason_code 使用稳定历史代码。

### 23.4 历史撤回

历史 retract 发生在当前事实最近确认之前：

- 不能 invalidated 较新的 active fact；
- 可以作用于当时已有的更早历史版本；
- 找不到安全目标时实际 action=noop；
- proposal 仍记录 committed/noop。

### 23.5 历史 correction

若当前 active fact更晚：

- 不能反向 supersede；
- 可以保存为历史 superseded fact；
- 不让旧状态重新成为当前 active。

### 23.6 时间确认修复

当前 `last_confirmed_at` 更新逻辑必须改为：

```text
max(existing.last_confirmed_at, new_evidence_time)
```

该规则同时适用于 live 和 rebuild。

历史证据不能把确认时间改早。

---

## 二十四、过期 Claim

Selection 包含：

```text
expired_claim_policy
```

### `skip`

若 proposal 在 snapshot time 前已经：

```text
valid_until <= snapshot_created_at
```

则默认：

- proposal 可以展示；
-默认 review 建议为 rejected；
- commit 时 skipped；
- 不创建 active fact。

### `stage_invalidated`

commit 时允许：

- 创建 invalidated 历史 fact；
- invalidated_reason=expired；
- 保留 evidence；
- 不进入普通检索；
- 不占 active 容量。

不得将已经过期的历史 temporary fact创建为 active。

---

## 二十五、容量规则

Rebuild 不能为了写入旧历史事实而自动淘汰当前事实。

要求：

1. Rebuild 不调用会驱逐现有 active fact 的 `make_room` 路径。
2. 若当前目标 active 容量已满：
   - identical/confirm evidence 仍可合并；
   -历史非 active 版本仍可保存；
   -创建新 active fact时 proposal commit 失败为 `rebuild_capacity_preserved`；
   -管理员可调整容量、清理事实或拒绝 proposal 后 retry。
3. 不静默失效当前 active fact。
4. 不因历史 rebuild 改变 explicit fact。
5. live Memory Worker 继续使用原容量规则。

---

## 二十六、事件级 Receipt

`memory_jobs.status=done` 是事件已经完成 Memory 处理的权威 receipt。

### 26.1 Rebuild 完成 event

当一个 item 的所有 proposal 均为：

- committed；
- rejected；
- skipped；

且没有 pending/failed proposal 时：

```text
memory_jobs.status = done
processing_source = rebuild
rebuild_run_id = 当前 run
completed_at = now
outcome = claims_applied / no_claims / all_rejected
```

### 26.2 已有 receipt

Commit 前发现：

```text
memory_jobs.status = done
```

则：

- proposal commit_status=skipped；
- actual_reason_code=already_processed；
- 不重复处理；
- 不重复 evidence。

### 26.3 pending/processing live job

不得抢占。

该 event：

- item skipped；
- reason=live_job_active。

### 26.4 failed live job

只有 selection：

```text
include_failed_live_jobs=true
```

才允许 rebuild 接管。

成功后更新为 done + rebuild source。

### 26.5 no claims / rejected

即使没有创建事实，也要写 done receipt，避免以后重复消耗模型。

---

## 二十七、Proposal 幂等

要求：

1. `item_id + claim_index` 唯一。
2. claim_hash 使用 canonical JSON SHA-256。
3. 同一个 extraction retry 不重复 proposal。
4. 同一个 proposal commit 不重复执行。
5. evidence 的 `fact_id + event_id` 唯一继续生效。
6. commit crash 后根据 commit_status 继续。
7. actual_fact_id 存在时再次执行直接返回已提交。
8. event receipt 完成后不重新提交。
9. 不根据文本相似度判断 proposal 是否已提交。
10. 不允许 Gateway、Plugin 或主模型创建 rebuild proposal。

---

## 二十八、Pause、Resume、Cancel

### 28.1 pause extraction

```text
/ai memory rebuild pause <run_id>
```

行为：

- 不再调度新 event；
- 已开始的 extraction 可以完成；
- checkpoint 持久化；
- status=extraction_paused。

### 28.2 pause commit

- 不再开始新 proposal；
- 当前短事务完成；
- status=commit_paused。

### 28.3 resume

从持久 checkpoint 继续。

不重新扫描已完成 item。

不重新提交已完成 proposal。

### 28.4 cancel

- 停止调度；
-取消可取消的 in-flight extraction model call；
- 不回滚已提交事实；
- 不删除 proposal；
- status=cancelled；
- completed facts 保留；
-管理员可审计或显式 purge staging。

### 28.5 进程重启

应用启动时发现：

```text
extracting
committing
```

必须转为：

```text
extraction_paused
commit_paused
```

reason=`process_restart`。

不得自动恢复昂贵历史任务。

管理员必须显式 resume。

---

## 二十九、Worker 设计

新增一个：

```text
MemoryRebuildWorker
```

不要创建多个互相竞争的 rebuild worker。

职责：

- 监听可执行 run；
- extract phase；
- commit phase；
- checkpoint；
- pause/cancel；
-持久重试；
- metrics。

### 29.1 Extraction 并发

可配置并发。

每个 event 独立调用 extraction。

Proposal 最终按 event order 持久展示，不按模型返回顺序改变。

### 29.2 Commit 并发

Commit 默认串行。

原因：

- 历史顺序；
-同一事实版本链；
-冲突状态；
-事件 receipt；
- SQLite 写事务。

不要并发提交同一 run 的多个历史 proposal。

### 29.3 与 live worker

Rebuild Worker 不阻止 live Memory Worker。

但：

- snapshot 不包含新事件；
- commit 使用当前事实状态；
- HistoricalResolutionGuard 防止旧事实覆盖新事实；
- live active job 的 event 不被 rebuild 抢占。

---

## 三十、统计

Run status 至少显示：

### Plan

- snapshot max event ID；
- matched events；
- eligible events；
- already processed；
- live pending/processing；
- failed live jobs；
- private/group counts；
- input characters；
- earliest/latest event；
- estimated extraction requests。

### Extraction

- scanned events；
- staged items；
- no-claim items；
- failed items；
- staged proposals；
- extraction requests；
-真实 input/output tokens；
- last checkpoint；
-当前状态。

### Review

- pending；
- approved；
- rejected；
- 按 operation/kind/authority/scope 分类数量。

### Commit

- committed proposals；
- skipped；
- failed；
- facts created；
- evidence merged；
- facts superseded；
- contested；
- invalidated；
- noop；
- receipts completed；
- embedding jobs created；
- last checkpoint。

不得保存或输出完整 claim 内容到普通 status。

---

## 三十一、管理命令

实现：

```text
/ai memory rebuild list
/ai memory rebuild plan <selection-json>
/ai memory rebuild start <run_id>
/ai memory rebuild status <run_id>
/ai memory rebuild pause <run_id>
/ai memory rebuild resume <run_id>
/ai memory rebuild cancel <run_id>
/ai memory rebuild review <run_id> [page]
/ai memory rebuild approve <run_id> <all|proposal-ids|filter-json>
/ai memory rebuild reject <run_id> <all|proposal-ids|filter-json>
/ai memory rebuild commit <run_id>
/ai memory rebuild retry <run_id>
/ai memory rebuild purge <run_id>
```

所有命令只允许：

```text
当前真实发送者属于 SUPERUSERS
```

`purge`：

- 只允许 completed/cancelled/failed；
- 删除 run、item、proposal staging；
- 不删除已经提交的 facts/evidence；
- memory_jobs.rebuild_run_id 设为 null；
-不取消 event receipt。

---

## 三十二、管理员 Tool Kernel

增加与命令服务共用同一后端的管理员工具：

```text
admin_memory_rebuild_plan
admin_memory_rebuild_start
admin_memory_rebuild_status
admin_memory_rebuild_pause
admin_memory_rebuild_resume
admin_memory_rebuild_cancel
admin_memory_rebuild_review
admin_memory_rebuild_approve
admin_memory_rebuild_reject
admin_memory_rebuild_commit
```

要求：

- 真实 superuser；
-明确 run ID；
-不维护第二套实现；
- Planner 只能缩小工具范围；
- 工具不能接受任意 SQL；
- 工具不能跳过 review；
- commit 工具要求 pending=0。

不要向 Plugin SDK 暴露 rebuild API。

---

## 三十三、配置

按当前 Settings/RuntimeConfig 风格增加。

建议启动配置：

```text
MEMORY_REBUILD_ENABLED=false
MEMORY_REBUILD_WORKER_INTERVAL_SECONDS=5
MEMORY_REBUILD_SCAN_BATCH_SIZE=100
MEMORY_REBUILD_EXTRACTION_CONCURRENCY=2
MEMORY_REBUILD_COMMIT_BATCH_SIZE=20
MEMORY_REBUILD_CONTEXT_EVENT_LIMIT=8
MEMORY_REBUILD_RETRY_ATTEMPTS=5
MEMORY_REBUILD_RETRY_INITIAL_SECONDS=30
MEMORY_REBUILD_REVIEW_PAGE_SIZE=20
MEMORY_REBUILD_SOURCE_EXCERPT_CHARACTERS=500
MEMORY_REBUILD_MAX_EVENTS_PER_RUN=
```

要求：

- 默认不启动 run；
- enabled 只表示功能可用；
-空 max events 表示不增加限制；
-非法值明确失败；
-不静默 clamp；
-默认值只出现一次；
-不在业务代码写第二层固定上限；
-模型 Secret 沿用现有 ModelRuntime；
-不增加 rebuild 专用 LLM API Key。

---

## 三十四、模型调用与成本

Extraction 使用：

```text
ModelTask.MEMORY_EXTRACTION
```

Commit 冲突分类使用：

```text
ModelTask.MEMORY_CONSOLIDATION
```

不新增第三个 rebuild 专属模型任务。

要求：

1. plan 无模型。
2. 一个 event 最多一次 extraction request，除非明确 retry。
3. 一个 claim 最多一次 consolidation request。
4. 真实 usage 可记录。
5. 没有 usage 时只记录字符数。
6. 不把字符数说成 Token。
7. pause/cancel 能停止后续模型请求。
8. 不因模型失败自动切换 Pro。
9. 不调用主聊天 Agent。
10. 不让模型生成命令。

---

## 三十五、FTS 与 Embedding

Commit 调用现有 MemoryFactService 后：

- FTS 通过现有触发器更新；
- active 新 fact 创建 Embedding job；
-历史 superseded/invalidated fact不要求普通语义索引；
- identical evidence merge 不重复建向量；
- authority/confidence 变化不重建向量；
- content 不原地修改。

Run completed 不代表 Embedding 全部完成。

status 应显示：

```text
embedding_jobs_created
```

但不等待 Embedding Worker。

Embedding API 不可用：

- 不阻止 rebuild commit；
- facts 仍提交；
- jobs 后台重试；
- FTS 立即可用。

---

## 三十六、隐私与 `/ai forgetme`

### 36.1 Event FK

Rebuild item 必须引用真实 event，并使用删除级联。

Event 被删除：

- item 删除；
- proposal 删除；
-不能继续 commit；
-不保留 source excerpt。

### 36.2 Person 删除

`/ai forgetme` 必须：

1. 删除该人物事件，级联 staging。
2. 删除以该人物为 proposal subject 的 staging。
3. 取消仅针对该人物的非终态 run。
4. 对包含该人物 selection 的 run：
   - 删除或脱敏 selection；
   -不保留精确 QQ。
5. 已提交事实按现有 forgetme 删除。
6. 关联 rebuild item/proposal 不得留下人物正文。
7. 聚合统计可以保留，但不得含 QQ。

### 36.3 日志

不得记录：

- source message；
- claim content；
- proposal content；
- QQ；
- group ID；
- selection 完整 JSON；
-模型完整输入；
-模型完整输出。

允许记录不可逆 hash、run ID、proposal ID、数量和错误分类。

---

## 三十七、健康检查

新增：

```text
MemoryRebuildHealth
```

至少包含：

- enabled；
- planned runs；
- extracting runs；
- paused runs；
- review runs；
- committing runs；
- failed runs；
- oldest active run；
- active in-flight calls；
- pending items；
- pending proposals；
- failed items；
- failed proposals；
- last successful extraction；
- last successful commit；
- last error category。

healthz：

- 只读本地数据库；
-不调用模型；
-不扫描全部事件；
-不自动 resume。

---

## 三十八、指标

新增不含正文的指标：

```text
rebuild_runs_planned
rebuild_runs_started
rebuild_runs_completed
rebuild_runs_cancelled
rebuild_events_matched
rebuild_events_eligible
rebuild_events_scanned
rebuild_events_no_claims
rebuild_events_skipped_processed
rebuild_events_failed
rebuild_proposals_staged
rebuild_proposals_approved
rebuild_proposals_rejected
rebuild_proposals_committed
rebuild_proposals_failed
rebuild_facts_created
rebuild_evidence_merged
rebuild_facts_superseded
rebuild_facts_contested
rebuild_facts_invalidated
rebuild_noops
rebuild_extraction_requests
rebuild_consolidation_requests
rebuild_input_tokens
rebuild_output_tokens
rebuild_latency
```

不要记录正文、用户 ID、群 ID和向量。

---

## 三十九、不要进行防御性编程

禁止：

1. plan 后自动 start。
2. extraction 后自动 approve。
3. review 未完成时自动 commit。
4. 应用启动自动 resume。
5. commit 时使用旧 resolution preview。
6. 将 staged claim 直接 insert 到 facts。
7. 复制一套 MemoryResolutionPolicy。
8. 复制一套 SubjectResolver。
9. 复制一套 ClaimValidator。
10. 从名字文本猜第三方人物。
11. 全库加载 events。
12. 使用 OFFSET 扫描大表。
13. 全库加载 vectors。
14. Embedding 失败时加载全部事实。
15. 历史 claim 覆盖较新事实。
16. 历史 evidence 把 last_confirmed_at 改早。
17. Rebuild 自动驱逐当前 active facts。
18. cancel 自动撤销已提交事实。
19. purge 删除已提交事实。
20. 进程重启自动继续模型请求。
21. 记录模型原始输出。
22. 将 API Key 写入 run。
23. 同一 event 重复生成 receipt。
24. 通过文本相似度判断 event 是否已处理。
25. 修改 Plugin API 主版本。
26. 读取 Memory V1。
27. 自动扫描聊天历史。
28. 使用主聊天 Agent 执行 rebuild。
29. 建立第二套事实表。
30. 建立第二套向量索引。

---

## 四十、本版本不做

明确不实现：

- 自动夜间重建；
- 启动时自动重建；
- 定时全库重建；
- 从 Memory V1 导入；
- 从聊天导出文件导入；
- 多 Bot 远程分布式 rebuild；
- WebUI；
- GitHub Artifact 审阅；
- 自动 rollback 已提交 rebuild；
- 通过网页核实历史事实；
- MCP 历史验证；
- LLM 自动 approve；
- 全历史 summary；
- 图片历史事实提取；
- 语音历史事实提取；
- OCR 历史重建；
- 多模型投票；
- Pro 模型复核；
- Plugin API rebuild；
- Memory MCP Server。

这些不属于第五阶段。

---

## 四十一、迁移测试

至少覆盖：

1. 从 `0023` 升级到新 head。
2. 现有 facts 保留。
3. 现有 evidence 保留。
4. relations 保留。
5. state events 保留。
6. FTS 保留。
7. Embedding 保留。
8. 新 rebuild 表存在。
9. memory_jobs 新字段存在。
10. 现有 memory_jobs source=live。
11. 迁移不创建 run。
12. 迁移不扫描 chat_events。
13. 迁移不调用模型。
14. 迁移不调用 Embedding API。
15. active run 存在时 downgrade 拒绝。
16. 无 active run 时 downgrade 不删除事实数据。

---

## 四十二、Plan 测试

至少覆盖：

1. plan 不调用模型。
2. plan 不写事实。
3. plan 不写 evidence。
4. plan 不创建 proposal。
5. plan 固定 snapshot max event ID。
6. plan 统计 private/group。
7. plan 排除 outbound。
8. plan 排除 Bot。
9. plan 排除空消息。
10. plan 排除 done job。
11. plan 排除 pending/processing job。
12. failed job 默认排除。
13. include_failed 后允许 failed。
14. after/before 正确。
15. sender filter 正确。
16. group filter 正确。
17. event ID filter 正确。
18. all_events 必须显式。
19. 无范围且 all=false 时失败。
20. 新事件在 plan 后不进入 run。
21. 字符数不标记为 Token。
22. selection canonical hash 稳定。

---

## 四十三、分页与 Checkpoint 测试

至少覆盖：

1. 使用 keyset pagination。
2. 相同 occurred_at 按 event ID 稳定。
3. 不使用 OFFSET。
4. checkpoint 之后继续。
5. crash 后不重复已 staged item。
6. 删除 event 后继续。
7. 新 event ID 超过 snapshot 被排除。
8. 批量大小由配置控制。
9. 不一次加载全部事件。
10. count 和 list 过滤一致。
11. 多群选择顺序稳定。
12. 私聊和群聊混合顺序稳定。

---

## 四十四、Extraction 测试

至少覆盖：

1. Rebuild 使用共享 MemoryEventExtractor。
2. Live Worker 也使用共享 Extractor。
3. 每个 event 独立模型请求。
4. context 只来自同一 conversation。
5. context 只包含更早事件。
6. context 不产生独立 claim。
7. rebuild automatic 转 source_type=rebuild。
8. explicit 保持 explicit。
9. extraction 不查候选。
10. extraction 不调用 consolidation。
11. extraction 不写事实。
12. extraction 不写 evidence。
13. extraction 不创建 embedding。
14. no claim item 正确。
15. invalid claim 不创建 proposal。
16. proposal claim JSON 严格。
17. 模型原始输出不保存。
18. fingerprint 变化暂停 run。
19. CancelledError 原样传播。
20. source event hash 正确。

---

## 四十五、Third-party 历史主体测试

至少覆盖：

1. yuki_context mention 被识别。
2. yuki_context reply author 被识别。
3. 旧 at segment 可确定性解析。
4. 旧 reply ID 只在同一精确 conversation 解析。
5. 跨群 reply 被拒绝。
6. 普通名字文本不生成主体。
7. Bot 被排除。
8. speaker 去重。
9. third_party disabled 时不生成。
10. third_party 只生成 person_group。
11. 私聊不生成第三方事实。
12. 不从 FTS/Embedding 查人物。

---

## 四十六、Review 测试

至少覆盖：

1. extraction 完成进入 review。
2. proposal 默认 pending。
3. review 分页。
4. review 输出有界 source excerpt。
5. review 不输出 conversation context。
6. approve 单个。
7. reject 单个。
8. approve all。
9. reject all。
10. filter approve。
11. 重复 approve 幂等。
12. approved proposal 不可修改 subject。
13. pending 存在时 commit 失败。
14. review actor 和时间记录。
15. 普通用户无权限。
16. cancelled run 不可 review 修改。

---

## 四十七、Commit 测试

至少覆盖：

1. commit 按 occurred_at/event_id/claim_index。
2. commit 重新加载 source event。
3. commit 验证 source hash。
4. source event 缺失时不写事实。
5. subject 变化时不写事实。
6. commit 重新运行 validator。
7. commit 使用当前 candidate state。
8. commit 必要时调用 consolidation。
9. commit 使用共享 MemoryClaimProcessor。
10. commit 使用 MemoryFactService。
11. proposal 记录实际 fact ID。
12. proposal 记录实际 action。
13. rejected proposal 不提交。
14. no claim event 写 receipt。
15. all rejected event 写 receipt。
16. event 完成后 job status=done。
17. processing_source=rebuild。
18. rebuild_run_id 正确。
19. outcome 正确。
20. FTS 正确更新。
21. embedding job 正确创建。
22. commit 不等待 embedding 完成。

---

## 四十八、Historical Guard 测试

至少覆盖：

1. 历史 identical claim 合并 evidence。
2. last_confirmed_at 不向过去移动。
3. 历史 claim 不能 supersede 较新 active。
4. 历史 claim 不能 invalidate 较新 active。
5. 历史 claim 不能 contest 较新 active。
6. 历史旧版本可以保存为 superseded。
7. 历史 correction 不恢复旧状态为 active。
8. 历史 retract 不撤销较新状态。
9. 较新 candidate 不降低 authority。
10. 较早 claim 在没有较新事实时可以 active。
11. expired skip 正确。
12. stage_invalidated 正确。
13. 历史 fact 不驱逐当前 active。
14. 容量满时返回明确失败。
15. live behavior 不受历史 guard 影响。

---

## 四十九、幂等测试

至少覆盖：

1. 同 run 同 event 只有一个 item。
2. 同 item 同 claim index 只有一个 proposal。
3. extraction retry 不重复 proposal。
4. commit retry 不重复 fact。
5. commit retry 不重复 evidence。
6. done memory job 使 event 跳过。
7. pending live job 不被抢占。
8. processing live job 不被抢占。
9. failed live job仅在显式配置下接管。
10. no-claim event 不重复提取。
11. all-rejected event 不重复提取。
12. crash 在 fact commit 后仍可恢复 proposal 状态。
13. event receipt 最终一致。
14. actual_fact_id 使 commit 幂等。
15. purge run 不删除 receipt。

---

## 五十、Pause、Resume、Cancel 测试

至少覆盖：

1. extracting pause 停止新请求。
2. in-flight extraction 可完成。
3. extraction checkpoint 持久。
4. resume 不重复 item。
5. commit pause 不开始新 proposal。
6.当前事务完成。
7. commit resume 继续下一个。
8. cancel extraction 取消可取消模型调用。
9. cancel commit 不回滚已提交事实。
10. cancelled run 不自动 resume。
11. restart 将 extracting 转 paused。
12. restart 将 committing 转 paused。
13. planned/review 不受 restart 影响。
14. 同时只执行一个 run。
15. pause/cancel 状态转换使用条件更新。

---

## 五十一、隐私测试

使用明显测试值。

至少覆盖：

1. event 删除级联 item。
2. event 删除级联 proposal。
3. proposal 不保留 source full text。
4. `/ai forgetme` 删除人物 staging。
5. `/ai forgetme` 取消该人物非终态 run。
6. selection 中人物 ID 被删除或脱敏。
7. 已提交事实按现有 forgetme 删除。
8. memory job receipt 不泄露人物正文。
9. 日志不含 claim content。
10. 日志不含 event content。
11. 日志不含 QQ/group。
12. status 不含 proposal content。
13. healthz 不含 selection JSON。
14. purge 不删除事实。
15. purge 不删除 evidence。

---

## 五十二、共享管线回归测试

至少覆盖：

1. Live Worker 行为与重构前一致。
2. Live Worker 立即提交事实。
3. Live Worker 不创建 rebuild proposal。
4. Live Worker 继续使用 live source。
5. Rebuild extraction 不提交事实。
6. Rebuild commit 使用同一 ClaimProcessor。
7. 同一个 claim 在 live/rebuild 走相同 identity validator。
8. 同一个 claim 走相同 candidate resolver。
9. 同一个 claim 走相同 resolution policy。
10. Rebuild 只额外应用 Historical Guard。
11. 不存在第二套 SubjectResolver。
12. 不存在第二套 MemoryFactService。

---

## 五十三、FTS 与 Embedding 测试

至少覆盖：

1. Rebuild active fact进入 FTS。
2. Rebuild active fact创建 embedding job。
3. Rebuild superseded fact不进入普通 retrieval。
4. Rebuild invalidated fact不进入普通 retrieval。
5. evidence merge 不重复 embedding。
6. authority/confidence 变化不重建向量。
7. Embedding API 失败不回滚 rebuild fact。
8. FTS 可在 commit 后立即使用。
9. Run completed 不等待 embedding。
10. status 显示 embedding jobs created。
11. 不进行全库 vector search。
12. 人物硬过滤保持。

---

## 五十四、管理员命令和工具测试

至少覆盖：

1. list。
2. plan。
3. start。
4. status。
5. pause。
6. resume。
7. cancel。
8. review。
9. approve。
10. reject。
11. commit。
12. retry。
13. purge。
14. 全部要求真实 superuser。
15. Admin Tool Kernel 使用同一服务。
16. 自然语言工具不能跳过 review。
17. Plugin 无 rebuild 接口。
18. 普通用户不能查看 run。
19. Secret 不进入命令输出。
20. 大 review 结果正确分页。

---

## 五十五、性能测试

构造：

- 十万条 chat_events；
- 多个群；
- 多个私聊；
- 已处理和未处理事件；
-相同时间事件；
-历史 correction/retract；
-第三方 mention/reply。

验证：

1. plan 使用 SQL count。
2. scan 使用 keyset。
3. 不把十万事件加载到内存。
4. extraction 并发不超过配置。
5. commit 串行稳定。
6. pause 延迟有界。
7. status 查询不扫描完整正文。
8. Review 分页有索引。
9. source hash 计算有界。
10. 相同 event 不重复模型请求。
11. 模型用量有统计。
12. 没有正文日志。

不写未经测量的绝对延迟承诺。

---

## 五十六、完整集成测试

新增端到端离线测试：

```text
历史事件账本
→ plan
→ start
→ extraction
→ review
→ approve/reject
→ commit
→ FTS/Embedding job
→ status completed
```

场景至少包含：

1. 张三早年说住福州。
2. 张三后来修正住上海。
3. 当前 Memory 已有上海 active fact。
4. Rebuild 处理福州旧事件时不能覆盖上海。
5. 福州可以作为 historical superseded fact或被安全处理。
6. 后来的历史确认上海合并 evidence。
7. 李四同样文本不串给张三。
8. 群 A 第三方事实不进入群 B。
9. 一个 event 无 claim。
10. 一个 proposal 被拒绝。
11. 一个 run 中途 pause/restart/resume。
12. commit 中途 cancel，已提交事实保留。
13. 再次计划同一范围时已 receipt 的 event 被跳过。
14. `/ai forgetme` 清理 staging 和已提交人物事实。

所有模型使用 Fake Provider。

CI 不调用真实模型。

---

## 五十七、实施顺序

1. 记录当前 `3.0.0b2` 基线。
2. 阅读 live Memory Worker 与 EventLedger。
3. 提取 `MemoryEventExtractor`。
4. 提取 `MemoryClaimProcessor`。
5. 完成 live 回归测试。
6. 增加 rebuild 枚举和领域模型。
7. 创建 `0024` 迁移。
8. 实现 Rebuild Repository。
9. 实现 Selection 和 Eligibility Policy。
10. 扩展 EventLedger keyset API。
11. 实现 Plan。
12. 实现 Source Event Fingerprint。
13. 实现 Extraction phase。
14. 实现 proposal staging。
15. 实现 Review。
16. 实现 Commit phase。
17. 实现 HistoricalResolutionGuard。
18. 扩展 memory_jobs receipt。
19. 实现 pause/resume/cancel/restart。
20. 接入 `/ai forgetme`。
21. 接入 FTS/Embedding。
22. 增加管理员命令和 Tool Kernel。
23. 增加 health 和 metrics。
24. 完成迁移、状态机、幂等、历史时序和隐私测试。
25. 更新文档和版本。
26. 运行完整质量检查。
27. 提交代码。

---

## 五十八、版本与文档

将版本提升为：

```text
3.0.0rc1
```

更新：

- `pyproject.toml`
- `src/qq_ai_bot/__init__.py`
- `CHANGELOG.md`
- `README.md`
- `.env.example`
- `docs/architecture/memory-v2-roadmap.md`
- `docs/architecture/memory-v2.md`
- 新增 `docs/architecture/memory-v2-rebuild.md`
- 管理员命令文档
- 运维和故障排查
- 隐私说明
- 升级指南
- `docs/architecture/codex-yuki-memory-v2-phase/codex-yuki-memory-v2-phase5.md`

路线文档标记：

```text
阶段一：已完成
阶段二：已完成
阶段三：已完成
阶段四：已完成
阶段五：已完成
阶段六：未开始
```

文档必须明确：

1. 升级不会自动重建。
2. plan 无模型。
3. start 只暂存 proposal。
4. review 后才可 commit。
5. cancel 不回滚已提交事实。
6. 重启不会自动 resume。
7. rebuild 只读 chat_events。
8. 不读取 Memory V1。
9. 历史事实不能覆盖较新事实。
10. Rebuild 不自动淘汰当前事实。
11. FTS 与 Embedding 是派生索引。
12. Run completed 不代表 Embedding 已完成。
13. 所有 rebuild 操作仅限真实超级管理员。

---

## 五十九、质量检查

必须运行并报告真实结果：

```bash
uv sync --all-extras
uv run ruff format --check .
uv run ruff check .
uv run mypy src
uv run pytest
uv run alembic upgrade head
docker compose config
docker compose build bot
```

额外运行：

```bash
uv run pytest -q tests/unit -k "memory and rebuild"
uv run pytest -q tests/integration -k "memory and rebuild"
```

检查不存在自动启动：

```bash
grep -R "rebuild.*start" src/qq_ai_bot/application src/qq_ai_bot/main.py
```

人工确认：

- Lifecycle 只启动 Worker 循环，不创建或自动恢复 run；
- run 必须由管理员显式 start/resume；
- CI 不调用真实 LLM；
- 不存在 Memory V1 引用；
- 不存在 Offset 大表扫描；
- 不存在 proposal 直接写 fact；
- 不存在历史 claim覆盖较新 fact。

---

## 六十、完成报告

完成后输出：

1. 开始 HEAD commit。
2. 最终 commit。
3. 当前项目版本。
4. 当前 Alembic head。
5. 新建和修改文件。
6. 新增枚举。
7. `memory_rebuild_runs` 表结构。
8. `memory_rebuild_items` 表结构。
9. `memory_rebuild_proposals` 表结构。
10. `memory_jobs` 新字段。
11. MemoryEventExtractor 抽取结果。
12. MemoryClaimProcessor 抽取结果。
13. Live Worker 如何复用共享管线。
14. Rebuild Selection Schema。
15. Event eligibility 规则。
16. Plan 统计。
17. Snapshot 规则。
18. Keyset pagination。
19. Source Event Fingerprint。
20. Historical mention/reply 解析。
21. Extraction phase。
22. Proposal staging。
23. Review 语义。
24. Approve/reject 语义。
25. Commit 顺序。
26. Commit 重新验证流程。
27. HistoricalResolutionGuard 规则。
28. last_confirmed_at 修复。
29. 过期 claim 策略。
30. 容量保护规则。
31. Event receipt 语义。
32. Proposal 幂等。
33. Pause/resume/cancel。
34. Process restart 行为。
35. `/ai forgetme` 接入。
36. FTS/Embedding 接入。
37. 管理员命令。
38. Admin Tool Kernel。
39. 新增配置。
40. Health 和 metrics。
41. 迁移测试结果。
42. Plan 测试结果。
43. Pagination/checkpoint 测试结果。
44. Extraction 测试结果。
45. Third-party 历史主体测试结果。
46. Review 测试结果。
47. Commit 测试结果。
48. Historical Guard 测试结果。
49. 幂等测试结果。
50. Pause/resume/cancel 测试结果。
51. 隐私测试结果。
52. 共享管线回归结果。
53. FTS/Embedding 测试结果。
54. 命令和权限测试结果。
55. 性能测试结果。
56. 完整集成测试结果。
57. 全部测试数量和结果。
58. Ruff 结果。
59. mypy 结果。
60. Alembic 结果。
61. Docker 结果。
62. 是否运行真实模型测试。
63. 尚未完成事项。
64. 是否存在升级或启动时自动重建。
65. 是否存在 review 未完成就 commit 的路径。
66. 是否存在 proposal 直接写 fact 的路径。
67. 是否存在第二套 SubjectResolver。
68. 是否存在第二套 MemoryResolutionPolicy。
69. 是否存在全表 OFFSET 扫描。
70. 是否存在历史事实覆盖较新事实。
71. 是否存在历史 evidence 将 last_confirmed_at 改早。
72. 是否存在 rebuild 淘汰当前 active fact。
73. 是否存在同一 event 重复 evidence。
74. 是否存在 cancel 自动删除已提交事实。
75. 是否读取 Memory V1。
76. 是否修改 Plugin API 主版本。

第 64 项预期：

```text
不存在。Run 只能由真实超级管理员显式 start 或 resume。
```

第 65 项预期：

```text
不存在。Commit 要求所有 proposal 已批准或拒绝。
```

第 66 项预期：

```text
不存在。Commit 通过共享 MemoryClaimProcessor 和 MemoryFactService。
```

第 67 项预期：

```text
不存在。
```

第 68 项预期：

```text
不存在。
```

第 69 项预期：

```text
不存在。事件账本使用 occurred_at + event_id 的 keyset pagination。
```

第 70 项预期：

```text
不存在。HistoricalResolutionGuard 保护更新的当前事实。
```

第 71 项预期：

```text
不存在。确认时间使用 max(existing, evidence_time)。
```

第 72 项预期：

```text
不存在。Rebuild 不调用当前事实驱逐路径。
```

第 73 项预期：

```text
不存在。Event receipt、proposal 状态和 evidence 唯一约束共同保证幂等。
```

第 74 项预期：

```text
不存在。Cancel 只停止后续处理。
```

第 75 项预期：

```text
没有。
```

第 76 项预期：

```text
没有，仍为 Plugin API 1.0。
```
