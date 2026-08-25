# Codex 任务：Yuki Memory V2 第六阶段——质量评测、治理收敛与 3.0.0 正式发布

你是一名资深 Python、SQLAlchemy、SQLite、异步 Agent、RAG 评测、数据质量、回归基准、隐私治理、CI/CD、发布工程和对话记忆架构工程师。

请在仓库：

`YuanYeYouTao/Yuki-QQbot`

当前最新 `main` 基础上开发：

`Yuki-QQbot 3.0.0`

本版本对应：

`docs/architecture/memory-v2-roadmap.md`

中的：

`阶段六：治理、评测与正式发布`

当前预期基线：

- 项目版本：`3.0.0rc1`
- 最新提交：`d1ab3189b30811f5e5d5c19858e008675ceab670`
- 最新提交说明：`feat(memory): add controlled event-ledger rebuild`
- Alembic head：`0024`
- Memory V2 第一至第五阶段已经完成
- 当前 release report 记录：`700 passed, 1 skipped`
- Plugin API 主版本仍为 `1.0`

---

## 一、任务性质

本版本不是继续扩张 Memory V2 功能。

本版本的目标是：

1. 建立可重复、可版本化、可在 CI 中阻止质量回退的 Memory V2 评测体系。
2. 将“Yuki 是否还会把人记串”变成可测量指标，而不是依赖人工感觉。
3. 建立生产数据库的无正文质量审计和发布检查。
4. 修复基准测试暴露出的真实缺陷。
5. 固化 Memory V2 的公共契约。
6. 完成从 `3.0.0rc1` 到 `3.0.0` 的正式发布收敛。

不要在本版本加入新的大功能：

- 不设计 Memory V3；
- 不引入知识图谱；
- 不更换数据库；
- 不更换 Embedding Provider；
- 不增加新人物推理规则；
- 不增加自动夜间历史重建；
- 不增加 WebUI；
- 不发布 Plugin API v2。

任何代码改动都必须服务于：

```text
评测
治理
一致性
回归防护
可观测性
正式发布
```

---

## 二、开始前必须核查

开始开发前必须读取并记录：

1. 当前 HEAD commit。
2. 当前项目版本。
3. 当前 Alembic head。
4. `docs/releases/v3.0.0rc1.md` 中的实际测试结果。
5. 当前 GitHub Actions workflow。
6. 当前 Memory V2 全部模块。
7. 当前 Memory Audit、Health、Metrics。
8. 当前 rebuild 状态机与 receipt。
9. 当前 FTS、Embedding、冲突、生命周期和上下文实现。
10. 当前 Plugin MemoryFacade。
11. 当前全部 Memory V2 测试。
12. 当前 migration 测试矩阵。
13. 当前内置插件 manifest 的 `yuki_requires`。
14. 当前日志脱敏规则。
15. 当前 `/ai forgetme`。
16. 当前 `qq-ai-bot-cli` 的结构。
17. 当前质量检查的真实结果。

至少阅读：

- `docs/architecture/memory-v2-roadmap.md`
- `docs/architecture/memory-v2.md`
- `docs/architecture/memory-v2-retrieval.md`
- `docs/architecture/memory-v2-embedding.md`
- `docs/architecture/memory-v2-conflicts.md`
- `docs/architecture/memory-v2-lifecycle.md`
- `docs/architecture/memory-v2-third-party-facts.md`
- `docs/architecture/memory-v2-rebuild.md`
- `docs/releases/v3.0.0rc1.md`
- `.github/workflows/quality.yml`
- `src/qq_ai_bot/memory/`
- `src/qq_ai_bot/services/context_assembler.py`
- `src/qq_ai_bot/health.py`
- `src/qq_ai_bot/cli.py`
- `src/qq_ai_bot/plugin_host/facades.py`
- `src/yuki_plugin_sdk/context.py`
- `migrations/versions/0020*`
- `migrations/versions/0021*`
- `migrations/versions/0022*`
- `migrations/versions/0023*`
- `migrations/versions/0024*`

如果第五阶段尚未完整存在：

- 列出真实缺失项；
- 停止第六阶段开发；
- 不在缺少受控 rebuild、历史保护或 receipt 的情况下发布 3.0.0。

---

## 三、正式发布不变量

以下不变量必须继续成立：

1. 模型不能提交任意 QQ 号、群号或 event ID。
2. 每个主事件独立提取。
3. 当前主事件是唯一自动证据来源。
4. 其他人物只能来自真实 mention 或 reply。
5. 第三方人物事实只能进入当前群 `person_group`。
6. `memory_facts` 是唯一事实来源。
7. FTS 和 Embedding 是可重建派生索引。
8. 人物和群硬过滤发生在词法与向量相似度之前。
9. 无命中时不加载全部事实。
10. Embedding 故障只降级到 FTS。
11. LLM 只分类语义关系，不直接决定数据库状态。
12. 好感度与 trust 不决定事实真伪。
13. contested claim 默认不进入普通上下文。
14. 修正不原地改写旧事实正文。
15. 普通撤回不物理删除证据。
16. 历史 rebuild 不覆盖较新事实。
17. 历史证据不把 `last_confirmed_at` 改早。
18. rebuild 不淘汰当前 active fact。
19. rebuild 不自动启动或恢复。
20. review 未完成不能 commit。
21. `/ai forgetme` 能清理事实、证据、索引和 rebuild staging。
22. Plugin API 主版本保持 `1.0`。
23. Memory V1 不重新出现。
24. 应用启动不自动扫描聊天历史。

---

## 四、核心交付

本版本必须交付以下六部分：

### 4.1 Memory Quality Benchmark

一个完全版本化、可重复运行的 Memory V2 离线评测系统。

### 4.2 Quality Gates

基于配置文件的质量门槛，CI 失败时阻止合并。

### 4.3 Production Quality Audit

针对真实数据库只输出计数、ID 和状态，不导出聊天正文。

### 4.4 Provenance Hygiene

检测并显式处理不合法来源的自动事实，不在启动时自动修改数据。

### 4.5 Stable Contract Freeze

固化 Memory V2 领域、Plugin API、迁移和配置契约。

### 4.6 3.0.0 Release Gate

只有完整测试、质量基准、迁移矩阵、Docker 和文档都通过时，才将版本提升为 `3.0.0`。

---

## 五、建议包结构

新增：

```text
src/qq_ai_bot/memory/quality/
├── __init__.py
├── models.py
├── suite.py
├── loader.py
├── runner.py
├── evaluator.py
├── metrics.py
├── gates.py
├── baseline.py
├── report.py
├── audit.py
├── hygiene.py
├── release_check.py
├── contracts.py
└── fake.py
```

根据当前架构可调整文件数量，但必须保持以下职责分离：

```text
数据集
执行
评分
门槛
报告
生产审计
显式修复
发布检查
```

不要把评测代码塞入：

- `MemoryRetriever`
- `ContextAssembler`
- `MemoryWorker`
- `MemoryFactService`
- `health.py`

生产代码不能依赖测试包。

评测包可以依赖正式 Memory V2 公共服务。

---

## 六、评测数据集

新增版本化数据集：

```text
tests/fixtures/memory_quality/v1/
├── manifest.toml
├── identity/
├── extraction/
├── third_party/
├── correction/
├── conflict/
├── temporal/
├── retrieval/
├── context/
├── rebuild/
├── privacy/
└── idempotency/
```

每个案例使用严格 JSON 或 TOML。

推荐 JSON。

### 6.1 数据集必须完全合成

只允许使用：

- 虚构 QQ 号；
- 虚构群号；
- 虚构昵称；
- 虚构聊天；
- 固定时间；
- Fake Model 输出；
- Fake Embedding。

禁止把真实：

- QQ；
- 群号；
- 用户记忆；
- 聊天正文；
- API Key；

提交到仓库。

### 6.2 符号身份

案例中优先使用符号：

```text
person_a
person_b
group_a
group_b
bot
```

Fixture Loader 再确定性映射成测试 QQ 和群号。

期望值使用符号主体，不直接依赖随机 ID。

### 6.3 Case Schema

定义：

```text
MemoryQualityCase
```

至少包含：

```text
schema_version
case_id
category
description
events
fake_model_outputs
initial_facts
initial_relations
initial_state_events
queries
expected_claims
expected_facts
expected_evidence
expected_relations
expected_retrieval
expected_context
forbidden_facts
forbidden_context
expected_rebuild
tags
```

不是每个 case 都必须填全部字段。

Pydantic 必须：

```text
extra = forbid
frozen = true
```

### 6.4 Case 稳定性

- 每个 `case_id` 全局唯一；
- 数据集 manifest 有版本；
- case 排序稳定；
- 时间固定；
- 不使用 `datetime.now()`；
- 不使用非固定随机数；
- Fake Embedding 结果稳定；
- Fake Model 结果稳定；
- 数据集更改必须更新 manifest hash。

---

## 七、覆盖矩阵

数据集必须覆盖以下风险。

### 7.1 身份归属

- 私聊本人事实；
- 群聊本人事实；
- 当前群事实；
- 当前人物群内事实；
- 两人说相同内容；
- 两群说相同内容；
- mention 第三方；
- reply 第三方；
- 普通名字文本不成为主体；
- Bot 不成为主体；
- 跨群 reply 不成为主体。

### 7.2 写入污染

- 模型试图输出 user_id；
- 模型试图输出 group_id；
- 模型试图输出 event_id；
- 未知 subject_ref；
- context 独立产生事实；
- outbound 消息；
- Bot 消息；
- 空消息；
- 当前事件证据不匹配。

### 7.3 修正与冲突

- identical evidence merge；
- self correction；
- explicit correction；
- third-party contradiction；
- self refutes third-party；
- equal-authority contested；
- coexists；
- retract；
- restore；
- merge；
- active unique slot。

### 7.4 时间

- persistent；
- temporary；
- episode；
- valid_from；
- valid_until；
- expired；
- stale；
- `last_confirmed_at`；
-历史证据时间；
-历史 guard。

### 7.5 检索

- 精确 key；
- FTS；
- semantic-only；
- lexical-only；
- hybrid；
-短查询；
-无命中；
- referenced person；
- current group；
- person_group；
- overview；
- explicit preference。

### 7.6 上下文

- 当前人物实体块；
- 当前群实体块；
-群内人物实体块；
- referenced person 独立块；
- wrong-subject 禁止；
- contested 禁止；
- third-party reported 标记；
- ContextBudgeter；
- `last_used_at`。

### 7.7 Rebuild

- plan 无模型；
- fixed snapshot；
- review；
- reject；
- commit；
- pause；
- restart；
- idempotency；
- historical guard；
- old fact 不覆盖 new fact；
- no-claim receipt；
- all-rejected receipt；
- forgetme。

### 7.8 隐私

- 不向 Embedding 发送 QQ；
- 不向 consolidation 发送 QQ；
- 日志无正文；
-报告无真实数据；
- forgetme 无 staging 残留；
- review 权限。

---

## 八、评测运行模式

定义：

```text
MemoryQualitySuiteMode
```

至少支持：

```text
structural
pipeline
retrieval
context
rebuild
full
```

### 8.1 structural

不调用任何模型。

验证：

- 数据库约束；
- identity boundary；
- Repository；
-状态机；
-派生索引一致性；
- audit；
- privacy；
- migration。

### 8.2 pipeline

使用 Fake Model。

流程：

```text
events
→ EventExtractor
→ ClaimProcessor
→ FactService
```

### 8.3 retrieval

预置 facts，使用：

- 实际 FTS；
- Fake Embedding；
- 实际 RRF；
- 实际 Retriever。

### 8.4 context

使用实际：

```text
MemoryContextService
ContextAssembler
ContextBudgeter
```

### 8.5 rebuild

使用实际：

```text
plan
extract
review
commit
receipt
```

全部模型为 Fake。

### 8.6 full

运行所有 deterministic suite。

---

## 九、可选真实模型评测

支持显式 opt-in：

```text
MEMORY_QUALITY_REAL_MODEL_ENABLED=true
```

真实模型评测：

- 只使用合成数据；
- 使用现有 `ModelTask.MEMORY_EXTRACTION`；
- 使用现有 `ModelTask.MEMORY_CONSOLIDATION`；
- 可以选择是否使用真实 Qwen Embedding；
- 默认不在 CI 运行；
- 默认不作为 merge gate；
- 报告必须标明 Provider、Model 和时间；
- 真实模型结果与 deterministic baseline 分开保存。

禁止：

- 发送真实数据库内容；
- 发送真实聊天；
- 将真实模型结果覆盖 deterministic baseline；
- 因真实模型波动自动修改质量门槛；
- 失败时切换 Pro。

---

## 十、评测运行器

新增：

```text
MemoryQualityRunner
```

职责：

1. 载入 suite。
2. 为每个 case 创建独立临时 SQLite 数据库。
3. 升级到当前 Alembic head。
4. 安装 Fake Model 与 Fake Embedding。
5. 构造真实 Memory V2 服务。
6. 执行指定阶段。
7. 收集结构化 observation。
8. 交给 Evaluator。
9. 聚合 Metrics。
10. 生成 JSON 与 Markdown 报告。
11. 不修改开发者真实数据库。

每个 case 独立数据库，避免相互污染。

不要使用一个大数据库顺序跑全部 case。

---

## 十一、Evaluator

新增：

```text
MemoryQualityEvaluator
```

它只比较：

```text
observed
expected
forbidden
```

不要调用 LLM 评分。

不要使用文本“感觉相似”代替结构化比对。

语义 retrieval case 使用固定 Fake Embedding 期望。

### 11.1 Fact 比对

按以下稳定键：

```text
scope_type
symbolic_subject
symbolic_group
kind
memory_key
normalized_content
status
authority
conflict_state
```

不要按数据库自增 ID 比对。

### 11.2 Evidence 比对

按：

```text
fact stable key
source event symbolic ID
relation
authority
```

### 11.3 Context 比对

按：

```text
block_id
fact stable key
```

### 11.4 Retrieval 比对

按：

```text
target
ordered stable fact keys
```

---

## 十二、质量指标

定义：

```text
MemoryQualityMetrics
```

至少包含以下指标。

### 12.1 身份指标

```text
subject_attribution_accuracy
scope_attribution_accuracy
cross_person_contamination_rate
cross_group_contamination_rate
third_party_global_leak_rate
bot_subject_rate
unknown_subject_acceptance_rate
```

### 12.2 证据指标

```text
evidence_provenance_accuracy
fact_without_evidence_rate
duplicate_evidence_rate
source_event_mismatch_rate
outbound_evidence_rate
bot_evidence_rate
blank_evidence_rate
```

### 12.3 状态指标

```text
fact_state_accuracy
correction_resolution_accuracy
retraction_resolution_accuracy
conflict_resolution_accuracy
conflict_coactivation_rate
duplicate_active_fact_rate
historical_regression_rate
idempotency_failure_rate
```

### 12.4 检索指标

```text
precision_at_k
recall_at_k
mean_reciprocal_rank
ndcg_at_k
wrong_target_retrieval_rate
empty_query_fact_leak_rate
```

### 12.5 上下文指标

```text
context_precision
context_recall
wrong_subject_context_rate
wrong_group_context_rate
contested_context_leak_rate
third_party_misattribution_rate
```

### 12.6 Rebuild 指标

```text
rebuild_review_bypass_rate
rebuild_duplicate_commit_rate
rebuild_historical_overwrite_rate
rebuild_receipt_accuracy
rebuild_resume_accuracy
```

### 12.7 工程指标

```text
pipeline_error_rate
average_extraction_requests_per_event
average_consolidation_requests_per_claim
average_query_embedding_requests_per_query
average_context_characters
p50_latency
p95_latency
```

没有足够样本时指标应为 `null`，不能伪造 0。

---

## 十三、指标定义

必须在文档与代码中统一定义分母。

示例：

### `cross_person_contamination_rate`

```text
观察到归属于错误人物的 fact 或 context item 数
/
全部人物 fact 或 context item 数
```

### `conflict_coactivation_rate`

```text
同时进入普通上下文的互相 contradicts facts 对数
/
全部 conflict cases
```

### `historical_regression_rate`

```text
历史 rebuild 使较新事实被 superseded、invalidated 或 contested 的次数
/
全部 historical guard cases
```

### `recall_at_k`

```text
top-k 中相关事实数
/
expected relevant facts 数
```

分母定义不能散落在测试代码。

新增：

```text
docs/architecture/memory-v2-quality-metrics.md
```

作为权威说明。

---

## 十四、Quality Gates

新增：

```text
config/memory_quality_gates.toml
```

以及示例：

```text
config/memory_quality_gates.example.toml
```

Python 代码不能写死发布门槛。

建议结构：

```toml
schema_version = 1

[deterministic.identity]
subject_attribution_accuracy.min = 1.0
scope_attribution_accuracy.min = 1.0
cross_person_contamination_rate.max = 0.0
cross_group_contamination_rate.max = 0.0
third_party_global_leak_rate.max = 0.0

[deterministic.context]
wrong_subject_context_rate.max = 0.0
wrong_group_context_rate.max = 0.0
contested_context_leak_rate.max = 0.0

[deterministic.state]
conflict_coactivation_rate.max = 0.0
duplicate_active_fact_rate.max = 0.0
historical_regression_rate.max = 0.0
idempotency_failure_rate.max = 0.0

[deterministic.retrieval]
recall_at_k.min = 0.95
precision_at_k.min = 0.95

[regression]
maximum_absolute_drop = 0.01
maximum_latency_ratio = 1.25
maximum_model_request_ratio = 1.10
```

具体默认值必须根据最终 deterministic suite 的真实基线确定。

不要为了通过测试降低门槛。

门槛变更必须：

- 修改配置；
- 更新基线；
- 在 CHANGELOG 说明原因。

---

## 十五、Baseline

新增：

```text
tests/benchmarks/memory_v2/v1/baseline.json
```

Baseline 保存：

- suite version；
- commit；
- Python；
- SQLite；
- metrics；
- case count；
- gate config hash；
- dataset manifest hash；
- timestamp；
- deterministic Provider IDs。

不保存：

-聊天正文；
-QQ；
-群号；
-向量；
-API Key；
-临时路径。

新增：

```text
MemoryQualityBaselineComparator
```

比较：

- 当前 metrics；
- absolute gates；
- baseline regression。

Baseline 更新必须由显式 CLI 完成。

普通测试运行不能自动改写 baseline。

---

## 十六、报告

生成：

```text
artifacts/memory-quality/report.json
artifacts/memory-quality/report.md
artifacts/memory-quality/junit.xml
```

报告至少包含：

- suite version；
- commit；
- gate config hash；
- dataset hash；
- case totals；
- passed/failed；
- metrics；
- gate failures；
- regression failures；
- latency；
-模型请求数量；
- failed case IDs。

Markdown 报告可以显示合成 case 描述，但不得显示真实生产内容。

JUnit 用于 GitHub Actions 注释。

---

## 十七、CLI

按当前 CLI 结构实现：

```text
qq-ai-bot-cli memory quality run
qq-ai-bot-cli memory quality run --suite full
qq-ai-bot-cli memory quality compare
qq-ai-bot-cli memory quality update-baseline
qq-ai-bot-cli memory quality report
qq-ai-bot-cli memory quality validate-dataset
qq-ai-bot-cli memory release-check
qq-ai-bot-cli memory audit
qq-ai-bot-cli memory hygiene scan
qq-ai-bot-cli memory hygiene apply <fingerprint>
```

### 17.1 `quality run`

- 默认 deterministic full；
- 不访问真实 API；
- 输出报告；
- 根据 gates 返回退出码。

### 17.2 `update-baseline`

- 必须显式；
- 必须先通过 absolute gates；
- 写入 baseline；
- 显示变化；
- 不隐藏质量下降。

### 17.3 `release-check`

组合：

- quality full；
-数据库完整性；
- Alembic head；
- FTS；
- Embedding；
- rebuild；
- conflict；
- migration contract；
- Plugin contract。

### 17.4 生产数据库参数

针对真实数据库的 CLI 必须显式传入：

```text
--database-url
```

不要默认误用开发数据库。

---

## 十八、Production Quality Audit

扩展或组合现有：

```text
MemoryAuditService
```

新增：

```text
MemoryProductionQualityAudit
```

只读取本地结构，不调用模型。

至少检查：

### 18.1 事实

- active 唯一槽位；
- facts without evidence；
- invalid status combinations；
- invalid authority；
- invalid temporal range；
- invalidated without reason；
- superseded without chain；
- conflict state mismatch。

### 18.2 证据

- orphan evidence；
- source event missing；
- outbound evidence；
- Bot evidence；
- blank source event；
- source speaker mismatch；
- evidence relation 与 authority 不一致；
- excerpt 不属于 source event；
- same event duplicate。

### 18.3 关系

- orphan relation；
- cross-target relation；
- self relation；
- invalid relation type；
- contradicts 状态不一致。

### 18.4 FTS

- missing FTS rows；
- stale FTS rows；
- orphan FTS rows；
- trigger 缺失；
- query smoke test。

### 18.5 Embedding

- current profile；
- missing active vectors；
- stale content hash；
- dimension mismatch；
- orphan vectors；
- orphan jobs；
- failed jobs；
- old profiles。

### 18.6 Rebuild

-多个 active run；
- stuck processing；
- pending review；
- committed proposal 无 receipt；
- receipt 无 proposal；
- source hash mismatch；
- terminal run 有 in-flight item。

### 18.7 隐私

- 已删除人物仍有 staging；
- 已删除人物仍有 facts；
- 已删除 event 仍有 evidence；
- selection 中孤立精确身份。

Audit 输出：

```text
issue_code
severity
count
sample_ids
```

不输出正文。

---

## 十九、Hygiene

针对 `3.0.0rc1` 实施报告中提到的旧 outbound/空事件污染，新增显式治理。

### 19.1 `MemoryHygieneService`

只处理确定性问题。

`scan()` 返回：

- issue counts；
- bounded fact IDs；
- fingerprint；
-建议 action。

### 19.2 可自动处理的问题

只允许自动处理：

1. automatic/rebuild fact 没有有效 evidence；
2. automatic/rebuild fact 的全部 evidence 只来自 outbound；
3. automatic/rebuild fact 的全部 evidence 只来自 Bot；
4. automatic/rebuild fact 的全部 evidence 只来自空消息；
5. FTS 派生索引缺失或 stale；
6. current profile missing embedding job；
7. orphan derived rows；
8. terminal rebuild staging purge。

### 19.3 不自动处理

以下只报告：

- active slot conflict；
- cross-target relation；
- explicit fact 来源异常；
- contested 冲突；
- ambiguous evidence；
- third-party 真实性；
-需要内容判断的问题。

### 19.4 apply

```text
scan
→ fingerprint
→ apply fingerprint
→ 重新 scan
→ fingerprint 一致才执行
```

执行前重新验证所有事实和状态。

对不合法自动事实：

```text
status = invalidated
invalidated_reason = administrator_invalidated
state event reason_code = invalid_provenance
```

不要物理删除事实或证据。

FTS/Embedding 只修复派生索引。

### 19.5 权限

- CLI 显式执行；
- QQ 命令只允许真实超级管理员；
- Tool Kernel 使用同一服务；
- 不在启动时自动 apply；
- 不在 healthz 自动 apply。

---

## 二十、发布检查

新增：

```text
MemoryReleaseCheck
```

检查结果分：

```text
pass
warning
fail
```

### 20.1 必须 fail

- deterministic quality gate failure；
- cross-person contamination > gate；
- wrong-subject context > gate；
- active slot conflict；
- cross-target relation；
- historical regression；
- migration failure；
- fresh install failure；
- Plugin contract failure；
- Alembic 不是 head；
- database foreign key failure；
- FTS trigger 缺失；
- unresolved source code import error；
- Ruff/mypy/test failure。

### 20.2 warning

- Embedding disabled；
- Embedding coverage below optional gate；
- failed embedding jobs；
- pending live memory jobs；
- contested facts；
- review-stage rebuild run；
- real-model benchmark not run；
- old embedding profiles。

Warning 不应伪装为 pass，但不一定阻止正式发布。

阻止规则来自 quality gate config，不在代码中写死。

---

## 二十一、数据库完整性

`release-check` 必须运行：

```text
PRAGMA integrity_check
PRAGMA foreign_key_check
Alembic current/head
MemoryAudit
FTS audit
Embedding audit
Rebuild audit
```

不要在 release-check 自动修复数据库。

只生成报告。

---

## 二十二、迁移矩阵

正式发布必须测试：

### 22.1 Fresh install

```text
empty database
→ alembic upgrade head
```

### 22.2 2.1.2 → 3.0.0

验证：

- Memory V1 被不可逆删除；
- chat_events 保留；
- people/groups/relationships/automation/plugins 保留；
-新 Memory V2 表建立；
-不会自动 rebuild。

### 22.3 3.0.0a1 → 3.0.0

### 22.4 3.0.0a2 → 3.0.0

### 22.5 3.0.0b1 → 3.0.0

### 22.6 3.0.0b2 → 3.0.0

### 22.7 3.0.0rc1 → 3.0.0

如果本版本不新增数据库表：

```text
Alembic head 保持 0024
```

优先不创建 `0025`。

只有发现确实需要持久化的生产契约时才新增迁移，并在完成报告说明原因。

评测报告和 baseline 不得存进生产数据库。

---

## 二十三、契约冻结

新增：

```text
MemoryContractCatalog
```

记录稳定版本：

```text
memory_fact_schema = 2
memory_evidence_schema = 2
memory_query_schema = 1
memory_context_schema = 1
memory_embedding_profile_schema = 1
memory_rebuild_selection_schema = 1
memory_rebuild_proposal_schema = 1
plugin_memory_facade_schema = 1
quality_suite_schema = 1
quality_report_schema = 1
```

具体数字应根据当前真实历史确定。

不要为了形式随意改版本。

### 23.1 Contract Tests

验证：

- Pydantic JSON Schema 快照；
- Plugin MemoryFacade 返回字段；
-管理员命令输出基础字段；
- rebuild selection/proposal；
- quality report；
-错误码；
- enum values。

Schema 快照变更必须显式更新并记录。

---

## 二十四、Plugin API

Plugin API 主版本保持：

```text
1.0
```

完成：

1. 检查所有内置插件 `yuki_requires`。
2. 检查示例插件。
3. 检查 MemoryFacade：
   - list；
   - search；
   - add；
   - update；
   - delete。
4. 插件不能访问：
   -原始向量；
   - rebuild；
   - quality dataset；
   - production audit 全局信息；
   -其他人物证据；
   - API Key。
5. 增加正式 Plugin API contract suite。
6. 更新插件开发文档。

不要为了 3.0.0 修改 Plugin API 主版本。

---

## 二十五、在线可观测性

扩展现有无正文指标，但不建立用户内容遥测。

至少增加聚合计数：

```text
memory_live_claims
memory_rebuild_claims
memory_cross_target_rejections
memory_unknown_subject_rejections
memory_context_target_count
memory_context_fact_count
memory_contested_context_suppressed
memory_retrieval_empty_count
memory_retrieval_fts_count
memory_retrieval_semantic_count
memory_retrieval_hybrid_count
memory_fact_state_transitions
memory_audit_issue_count
memory_hygiene_invalidated_count
```

不得记录：

- query 正文；
- fact 正文；
- evidence；
- QQ；
-群号；
-向量；
- proposal 内容。

---

## 二十六、GitHub Actions

在 `.github/workflows/quality.yml` 增加独立 job：

```text
memory-quality
```

步骤：

1. checkout；
2. setup uv；
3. `uv sync --frozen --all-extras`；
4. validate dataset；
5. run deterministic full suite；
6. compare baseline；
7. apply quality gates；
8.生成 JSON/Markdown/JUnit；
9. upload artifact。

要求：

- 不调用真实 LLM；
- 不调用真实 Qwen；
- 不需要 Secret；
-失败阻止 merge；
-报告 artifact 即使失败也上传；
- job 不重复运行全部普通 pytest；
-使用独立临时数据库。

增加 migration matrix job 或测试。

不要把 real-model benchmark 放入普通 CI。

---

## 二十七、Release Artifact

新增：

```text
docs/releases/v3.0.0.md
```

至少记录：

- Memory V2 六阶段概览；
-最终架构；
- migration head；
-测试总数；
-质量指标；
-质量门槛；
-性能测试；
-隐私边界；
-真实模型测试状态；
-升级路径；
-已知限制；
-Plugin API；
-回退方式。

发布报告中的数字必须来自真实运行。

不要预先写“全部通过”。

---

## 二十八、正式版本文档

更新：

- `README.md`
- `CHANGELOG.md`
- `.env.example`
- `docs/architecture/memory-v2-roadmap.md`
- `docs/architecture/memory-v2.md`
- `docs/architecture/memory-v2-quality.md`
- `docs/architecture/memory-v2-quality-metrics.md`
- `docs/operations/memory-quality.md`
- Memory V2 历史升级指南（已在 3.8 canonical-only 收口时移除）
- Plugin 文档
-管理命令文档
-隐私说明
-故障排查
-Release checklist

路线文档标记：

```text
阶段一：已完成
阶段二：已完成
阶段三：已完成
阶段四：已完成
阶段五：已完成
阶段六：已完成
Memory V2：正式发布
```

---

## 二十九、版本

只有全部 release gates 通过后，才将：

```text
3.0.0rc1
```

提升为：

```text
3.0.0
```

不要在任务开始时立即改版本。

流程：

```text
实现
→ 测试
→ benchmark
→ quality gates
→ migration matrix
→ release-check
→ 文档
→ version bump
→ final full check
```

---

## 三十、禁止防御性编程

禁止：

1. 为通过 benchmark 修改 expected 使错误变正确。
2. 自动降低 quality gates。
3. benchmark 失败时跳过 case。
4. 将缺失指标记为 0。
5. 使用真实用户数据做 fixture。
6. 在评测中调用主聊天 Agent 评分。
7. 用 LLM 判断回答“看起来对”。
8. 在生产启动自动运行 full benchmark。
9. 在 healthz 运行 benchmark。
10. 在 release-check 自动修复数据库。
11. Hygiene 自动修改 explicit fact。
12. Hygiene 自动解决 contested conflict。
13. Hygiene 物理删除事实和证据。
14. 将真实数据库内容写入报告。
15. 将 API Key 写入 baseline。
16. 将向量写入 baseline。
17. Quality Runner 使用开发者真实数据库。
18. 自动更新 baseline。
19. 将 real-model 波动作为 deterministic gate。
20. 因 Embedding 未启用而假装 semantic 指标通过。
21. 新建第二套 Memory Pipeline。
22. 新建第二套 MemoryRetriever。
23. 新建第二套 SubjectResolver。
24. 新建第二套 MemoryResolutionPolicy。
25. 修改 Plugin API 主版本。
26. 自动扫描历史聊天。
27. 引入 Memory V1。
28. 增加外部向量数据库。
29. 使用好感度决定质量。
30. 未运行检查却声称通过。

---

## 三十一、本版本不做

明确不实现：

- Memory V3；
-知识图谱；
- Neo4j；
-在线 A/B 实验平台；
-用户画像 UI；
-浏览器管理面板；
-多模型自动投票；
- Pro 模型质量裁判；
-网页事实验证；
-MCP 事实验证；
-自动夜间 rebuild；
-自动 baseline 更新；
-生产聊天内容遥测；
-跨实例分布式 benchmark；
-Plugin API v2；
-新的 Embedding Provider；
-新的向量数据库；
-图片长期记忆；
-语音长期记忆。

---

## 三十二、数据集验证测试

至少覆盖：

1. manifest schema。
2. case schema。
3. case ID 唯一。
4. symbolic identity 完整。
5. event ID 唯一。
6.时间固定。
7. expected/forbidden 不冲突。
8. referenced fact 存在。
9. referenced target 存在。
10. dataset hash 稳定。
11.未知字段拒绝。
12.真实 QQ 模式检测。
13.疑似 Secret 检测。
14.空 case 拒绝。
15. suite 分类完整。

---

## 三十三、Runner 测试

至少覆盖：

1. 每 case 独立数据库。
2. 当前 Alembic head。
3. Fake Model。
4. Fake Embedding。
5. 不访问网络。
6. 不使用真实 DATABASE_URL。
7. structural mode。
8. pipeline mode。
9. retrieval mode。
10. context mode。
11. rebuild mode。
12. full mode。
13. case failure 不污染后续 case。
14. deterministic 顺序。
15.报告生成。
16.退出码正确。
17.临时数据库清理。
18.取消传播。

---

## 三十四、Evaluator 测试

至少覆盖：

1. symbolic subject comparison。
2. symbolic group comparison。
3. fact stable key。
4. evidence comparison。
5. relation comparison。
6. state comparison。
7. retrieval order。
8. context block。
9. forbidden fact detection。
10. forbidden context detection。
11. duplicate active detection。
12. no evidence detection。
13. missing expected detection。
14. extra unexpected detection。
15. null metric behavior。

---

## 三十五、指标测试

至少覆盖：

1. subject accuracy。
2. scope accuracy。
3. cross-person rate。
4. cross-group rate。
5. third-party global leak。
6. evidence provenance。
7. duplicate evidence。
8. state accuracy。
9. correction accuracy。
10. conflict coactivation。
11. historical regression。
12. precision@k。
13. recall@k。
14. MRR。
15. nDCG@k。
16. wrong subject context。
17. rebuild receipt accuracy。
18. idempotency failure。
19.空分母返回 null。
20.聚合可重复。

---

## 三十六、Gate 测试

至少覆盖：

1. min gate。
2. max gate。
3. null metric。
4. missing metric。
5. absolute gate。
6. baseline regression。
7. latency ratio。
8. model request ratio。
9. config hash。
10.非法配置。
11. gate failure exit code。
12. baseline 不自动修改。
13. gate 修改可审计。
14. deterministic 和 real-model 分离。

---

## 三十七、生产 Audit 测试

至少覆盖：

1. active slot conflict。
2. fact without evidence。
3. orphan evidence。
4. outbound evidence。
5. Bot evidence。
6. blank evidence。
7. source speaker mismatch。
8. excerpt mismatch。
9. cross-target relation。
10. self relation。
11. invalid status。
12. missing FTS。
13. stale FTS。
14. orphan vector。
15. stale vector。
16. failed embedding job。
17. invalid rebuild state。
18. committed proposal no receipt。
19. deleted person staging。
20.无正文输出。

---

## 三十八、Hygiene 测试

至少覆盖：

1. scan 不修改数据。
2. fingerprint 稳定。
3.数据变化后 fingerprint 失效。
4. automatic fact no evidence 可失效。
5. rebuild fact invalid provenance 可失效。
6. explicit fact 不自动处理。
7. ambiguous issue 只报告。
8. FTS 可重建。
9. embedding missing job 可补齐。
10. orphan derived row 可清理。
11.终态 rebuild staging 可 purge。
12.事实不物理删除。
13. evidence 不物理删除。
14. state event 记录。
15.只有真实超级管理员可通过 QQ apply。

---

## 三十九、Release Check 测试

至少覆盖：

1. PRAGMA integrity。
2. foreign key。
3. Alembic head。
4. quality gate。
5. baseline regression。
6. Memory Audit。
7. FTS。
8. Embedding。
9. Rebuild。
10. Plugin contract。
11. migration matrix。
12. fail/warning/pass。
13. warning 不伪装 pass。
14. JSON report。
15. Markdown report。
16.退出码。
17.不自动修复。
18.不访问网络。

---

## 四十、迁移矩阵测试

至少覆盖：

1. fresh install。
2. 2.1.2 → 3.0.0。
3. a1 → 3.0.0。
4. a2 → 3.0.0。
5. b1 → 3.0.0。
6. b2 → 3.0.0。
7. rc1 → 3.0.0。
8. Memory V1 删除语义。
9. chat_events 保留。
10.关系保留。
11.自动化保留。
12.插件数据保留。
13.不会自动 rebuild。
14. current head 正确。
15. fresh/upgrade schema 等价。

---

## 四十一、端到端质量案例

必须包含以下离线完整案例。

### 41.1 两个人说相同事实

```text
张三：我喜欢泛函分析
李四：我喜欢泛函分析
```

验证：

- 两个人各自拥有事实；
-检索张三不返回李四；
-上下文张三不出现李四。

### 41.2 两个群相同群事实

验证严格群隔离。

### 41.3 第三方事实

张三 @ 李四：

```text
李四喜欢摄影
```

验证：

-只写当前群 person_group；
-李四本人确认后 authority 升级；
-不进入全局 person。

### 41.4 修正

```text
我住福州
不是，我现在住上海
```

验证版本链和 context。

### 41.5 冲突

第三方说法与本人说法冲突。

验证 contested 不进入普通上下文。

### 41.6 Semantic retrieval

无词面重叠同义事实正确命中。

### 41.7 无命中

无关问题不加载高重要度事实。

### 41.8 历史重建

旧事实不能覆盖较新 active。

### 41.9 Pause/restart

Rebuild 断点续跑。

### 41.10 Forgetme

facts、evidence、indexes、staging 全部清理。

---

## 四十二、性能回归

建立稳定性能场景：

- 100 个用户；
- 每人 100 facts；
-多个群；
- person_group；
- FTS；
- Fake Embedding；
- conflicts；
- rebuild；
- 100,000 chat events。

测量：

```text
plan latency
keyset scan throughput
retrieval p50/p95
context assembly p50/p95
quality suite total
memory peak
model request count
embedding query count
```

Gate 不使用未经测量的绝对值。

Baseline 记录当前机器类别和结果。

CI 只比较宽松回归比例。

---

## 四十三、CI 测试

GitHub Actions 新 job 必须通过：

```text
dataset validation
deterministic full benchmark
quality gates
baseline comparison
report artifact
```

普通 Python job继续运行：

```text
Ruff
mypy
pytest
Alembic
plugin contract
prompt benchmark
model routes
```

Docker job继续运行。

不要删除现有检查来缩短时间。

---

## 四十四、实施顺序

1. 记录 `3.0.0rc1` 真实基线。
2. 阅读完整 Memory V2。
3. 定义 quality suite Schema。
4. 建立合成 fixture v1。
5. 实现 Loader。
6. 实现 Runner。
7. 实现 Evaluator。
8. 实现 Metrics。
9. 实现 Quality Gates。
10. 实现 Baseline。
11. 实现 Report。
12. 增加 CLI。
13. 扩展 Production Audit。
14. 实现 Hygiene scan/apply。
15. 实现 Release Check。
16. 增加 Contract Catalog。
17. 增加 Schema 快照测试。
18. 增加 Plugin API contract。
19. 增加 migration matrix。
20. 增加 GitHub Actions job。
21. 运行 benchmark。
22. 修复 benchmark 暴露的真实缺陷。
23. 重新运行全部检查。
24.生成最终 baseline。
25. 更新文档。
26. 生成 `docs/releases/v3.0.0.md`。
27. 全部 gates 通过后提升版本为 `3.0.0`。
28. 运行最终完整检查。
29. 提交代码。

---

## 四十五、质量检查

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
uv run qq-ai-bot-cli memory quality validate-dataset
uv run qq-ai-bot-cli memory quality run --suite full
uv run qq-ai-bot-cli memory quality compare
uv run qq-ai-bot-cli memory release-check
```

若 CLI 实际调用方式不同，使用真实命令，但必须提供等价能力。

禁止声称未运行的检查通过。

---

## 四十六、完成报告

完成后输出：

1. 开始 HEAD commit。
2. 最终 commit。
3. 当前项目版本。
4. 当前 Alembic head。
5. 新建和修改文件。
6. Quality Suite Schema。
7. Fixture 目录与 case 数。
8. Symbolic identity 设计。
9. Runner 架构。
10. Evaluator 比对规则。
11. 完整指标列表。
12. 指标分母定义。
13. Quality Gate 配置。
14. Baseline 格式。
15. Dataset hash。
16. Gate config hash。
17. Report 格式。
18. CLI 命令。
19. Production Audit 新检查。
20. Hygiene 可处理问题。
21. Hygiene 不可自动处理问题。
22. Hygiene fingerprint 规则。
23. Release Check 项目。
24. Contract Catalog。
25. Plugin API contract。
26. Migration matrix。
27. GitHub Actions memory-quality job。
28. 身份归属指标。
29. cross-person contamination。
30. cross-group contamination。
31. evidence provenance。
32. correction accuracy。
33. conflict coactivation。
34. retrieval precision/recall/MRR/nDCG。
35. wrong-subject context。
36. historical regression。
37. idempotency。
38.性能回归。
39. deterministic benchmark case 数。
40. deterministic benchmark结果。
41. real-model benchmark是否运行。
42.全套 pytest 数量和结果。
43. Ruff 结果。
44. mypy 结果。
45. Alembic 结果。
46. Docker 结果。
47. release-check 结果。
48. 真实数据库 audit 是否运行。
49. 已修复的真实缺陷。
50. 尚未完成事项。
51. 是否自动降低过 quality gates。
52. 是否修改 expected 以掩盖错误。
53. 是否使用真实用户数据。
54. 是否在 CI 调用真实模型或 Qwen。
55. 是否在生产启动自动运行 benchmark。
56. 是否由 release-check 自动修改数据库。
57. 是否存在全库向量搜索后过滤人物。
58. 是否存在无命中时加载全部事实。
59. 是否存在 contested claim 进入普通上下文。
60. 是否存在历史事实覆盖较新事实。
61. 是否存在 rebuild 自动启动。
62. 是否重新引入 Memory V1。
63. 是否修改 Plugin API 主版本。
64. 是否新增生产数据库迁移。
65. Memory V2 路线是否全部标记完成。

第 51 项预期：

```text
没有。
```

第 52 项预期：

```text
没有。
```

第 53 项预期：

```text
没有。数据集全部为版本化合成数据。
```

第 54 项预期：

```text
没有。CI 使用 Fake Model 与 Fake Embedding。
```

第 55 项预期：

```text
没有。
```

第 56 项预期：

```text
没有。修复必须使用显式 hygiene apply。
```

第 57 项预期：

```text
不存在。
```

第 58 项预期：

```text
不存在。
```

第 59 项预期：

```text
不存在。
```

第 60 项预期：

```text
不存在。
```

第 61 项预期：

```text
不存在。
```

第 62 项预期：

```text
没有。
```

第 63 项预期：

```text
没有，仍为 Plugin API 1.0。
```

第 64 项预期：

```text
优先没有，Alembic head 保持 0024；若确有迁移，必须在报告中说明真实必要性。
```

第 65 项预期：

```text
是。
```
