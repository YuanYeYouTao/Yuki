# Memory V2 质量与治理架构

## 自动首次收录的现行合同

自动提取采用必填价值声明（retention、source_style、importance、confidence、
value_reason），与内部 mutation DTO 分开，避免让删除/纠正多出无意义字段。语义由
已有提取模型判断，不加正则或新 Agent。importance 1–2 不自动长期保存，>=3 的稳定
事实和有意义单次经历可收录；低价值直接跳过，不能转移为候选积压。后台不能自封 explicit。

self-reflection 新增同样执行价值门槛，正常跳过推进水位；已有事实维护不强制重新收录。
提取最长等待一小时，数量/字符仍可提前触发。这不是 Rollup/反思的间隔，不影响即时工具。

Memory V2 正式版用四层机制防止“把人记串”：

1. 版本化合成 fixture 描述事件、Fake Model 输出、预期事实、证据、检索、上下文与 rebuild。
2. `MemoryQualityRunner` 为每个 case 建立一个已迁移到 Alembic `0051` 的独立临时 SQLite，
   复用生产 EventExtractor、ClaimProcessor、FactService、FTS、Fake Embedding、Retriever、
   ContextService 和 rebuild 状态机。
3. Evaluator 只做 symbolic stable key 精确比较；Metrics 按固定分母聚合；外部 TOML 门禁与
   baseline comparator 共同阻止绝对质量或相对性能回退。
4. Production Audit 只输出 issue code/count/有限行 ID；Hygiene 需要先 scan，再以完全匹配的
   fingerprint 显式 apply。启动、healthz 与 release-check 都不会自动修复数据库。

合成数据不会包含真实 QQ、群号、聊天、向量、Secret 或临时路径。确定性 CI 只使用
`memory-quality-fake-model-v1` 与 `fake-embedding/local-test/v1`，不调用 DeepSeek、Qwen 或网络。

质量数据集 `memory-v2-quality-v2` 还冻结历史共同群读取：直接共同群可读 Person、Group 与
PersonGroup；无直接关系和传递关系拒绝。它不以合成 fixture 中的目标字段替代后端授权。

正式契约由 `config/memory_contracts.toml` 和
`tests/contracts/memory_v2/contracts.json` 冻结。Plugin API 保持 `2.0`，插件只能通过受作用域
限制的 MemoryFacade list/search/add/update/delete；不能访问向量、rebuild、全局 audit、其他人物
证据、质量数据集或 Provider Secret。

## 性能基准

baseline 保留旧版固定 100 用户、10,000 facts、10 个群和 100,000 条事件的数值快照，仅用于
证明发布合同没有丢失。依赖 pre-3.8 carrier 表的生成器已随 canonical-only 收口退役，当前 CLI
不提供 `memory quality performance`。不能为了重跑旧数值恢复 `people/groups` 等旧表；未来的
大规模 runner 必须直接生成 canonical 身份、会话和事件。当前 CI 使用完整确定性套件的质量、
请求数及宽松延迟门禁，跨硬件不设绝对毫秒 SLA。微基准延迟只有同时超过相对比例与 20ms
绝对增量才构成回归，避免把 SQLite/调度抖动误报为性能问题；质量、污染和请求数门禁不受影响。

运行和故障处理参见 [Memory V2 质量运维](../operations/memory-quality.md)，指标定义参见
[质量指标与分母](memory-v2-quality-metrics.md)。
