# Memory V2 质量指标与分母

本页是质量指标的权威定义。评测只比较版本化合成数据中的结构化 expected、observed 与
forbidden 值，不使用 LLM 裁判或模糊相似度。分母为零时值必须是 JSON `null`；完整发布套件
若某个受门禁指标没有分母，门禁失败。子套件会将无关指标标为 not applicable。

## 身份

| 指标 | 分子 | 分母 |
|---|---|---|
| `subject_attribution_accuracy` | event/key/content 相同且主体准确的 claim | 有预期主体的 claim |
| `scope_attribution_accuracy` | event/key/content 相同且 scope 准确的 claim | 有预期 scope 的 claim |
| `cross_person_contamination_rate` | 跨人物 forbidden fact/retrieval/context 命中 | 跨人物案例的 observed fact/context 项 |
| `cross_group_contamination_rate` | 跨群 forbidden fact/retrieval/context 命中 | 跨群案例的 observed fact/context 项 |
| `third_party_global_leak_rate` | 第三方事实错误进入全局 person 的项 | third-party observed facts |
| `bot_subject_rate` | Bot 事件产生的 claim | Bot 事件 |
| `unknown_subject_acceptance_rate` | unknown subject 产生的 claim | unknown-subject 案例 |

## 事实、证据和状态

| 指标 | 分子 | 分母 |
|---|---|---|
| `fact_accuracy` | stable key 完全匹配的事实 | 全部预期事实 |
| `evidence_provenance_accuracy` | fact/event/speaker/relation/excerpt 完全匹配的证据 | 全部预期证据 |
| `fact_without_evidence_rate` | pipeline 新建但没有证据的 automatic/rebuild fact | pipeline 新建的 derived facts |
| `duplicate_evidence_rate` | 重复的 fact/event 证据 | observed evidence |
| `source_event_mismatch_rate` | event/speaker/excerpt 不匹配来源事件的证据 | observed evidence |
| `outbound_evidence_rate` | outbound 事件产生的证据 | outbound 来源事件 |
| `bot_evidence_rate` | Bot 事件产生的证据 | Bot 来源事件 |
| `blank_evidence_rate` | 空事件产生的证据 | 空来源事件 |
| `fact_state_accuracy` | status 与预期相同的 observed fact | 可映射到预期的 observed fact |
| `correction_resolution_accuracy` | 通过的 correction 案例 | correction 案例 |
| `retraction_resolution_accuracy` | 通过的 retraction 案例 | retraction 案例 |
| `conflict_resolution_accuracy` | 通过的 conflict 案例 | conflict 案例 |
| `conflict_coactivation_rate` | 冲突案例中错误共激活 | conflict 案例 |
| `duplicate_active_fact_rate` | 同主体/kind/key 多出的 active fact | observed active facts |
| `historical_regression_rate` | 较新 fact 被历史 rebuild 降级或时间倒退 | historical-guard 案例 |
| `idempotency_failure_rate` | 未通过的幂等案例 | idempotency 案例 |

## 检索与上下文

`precision_at_k` 是 top-k 中相关事实数除以实际返回数；`recall_at_k` 是 top-k 中相关事实数
除以 expected relevant facts。`mean_reciprocal_rank` 对有预期命中的查询取首个相关结果倒数
排名的平均值；`ndcg_at_k` 使用二元相关性与 `1/log2(rank+1)` 折损。

| 指标 | 分子 | 分母 |
|---|---|---|
| `wrong_target_retrieval_rate` | forbidden target 命中 | 实际检索结果 |
| `empty_query_fact_leak_rate` | 空查询返回事实 | 空查询案例 |
| `context_precision` | context 中相关事实 | 实际 context facts |
| `context_recall` | context 中相关事实 | 预期 context facts |
| `wrong_subject_context_rate` | forbidden person fact 命中 | 实际 context facts |
| `wrong_group_context_rate` | forbidden group fact 命中 | 实际 context facts |
| `contested_context_leak_rate` | contested fact 泄漏 | contested 案例 |
| `third_party_misattribution_rate` | 第三方归属失败 | third-party observed facts |

## Rebuild 与工程指标

Rebuild 分别测量 review bypass、重复 commit、历史覆盖、receipt 准确率和断点恢复准确率；
分母都是对应 rebuild 案例。工程指标包括 `pipeline_error_rate`、每事件提取请求、每 claim
consolidation 请求、每 query embedding 请求、平均上下文字数，以及 extraction/retrieval/
context 的 p50/p95 延迟。`total_model_requests` 和 `total_query_embedding_requests` 用于宽松的
baseline 比例回归。

`quality_suite_total_ms` 是各异构 case 单次 wall-clock observation 的总和，只用于报告趋势，
不参与 baseline 回归判定；它会把单个 runner 的调度或 SQLite 抖动累加成伪回归。延迟门继续由
extraction/retrieval/context 的 p50/p95 指标承担，大规模性能变化另由固定 100,000 事件场景衡量。

p95 至少需要 20 个实际 observation；样本不足时按统一空分母规则输出 `null`，不能把单次最大
耗时伪装成尾延迟。p50 没有该最小样本限制。100,000 事件性能场景固定执行 50 次检索，因此会
独立提供可比较的 retrieval/context p95。

门禁来自 `config/memory_quality_gates.toml`，Python 不写死阈值。变更门禁必须评审配置、
显式执行 `memory quality update-baseline` 并在 CHANGELOG 说明；普通测试不会更新 baseline。
