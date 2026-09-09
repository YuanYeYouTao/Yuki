> **历史设计/非现行合同。** 本文保留当时的方案或实测口径，不用于当前授权、调度或使用率判断。
> 当前实现以 [Memory 架构](../memory-v2.md)、[检索合同](../memory-v2-retrieval.md)
> 和 [P1 治理任务书](../Yuki-Memory-P1治理任务书.md) 为准；本文不在当前实施导航内。

# Codex 任务：Yuki Memory V2 第三阶段——Qwen Embedding 与混合 RAG

你是一名资深 Python、SQLAlchemy、SQLite、异步任务、向量检索、RAG、远程 Embedding API 和 LLM Agent 上下文架构工程师。

请在仓库：

`YuanYeYouTao/Yuki-QQbot`

当前 `main` 基础上开发：

`Yuki-QQbot 3.0.0b1`

本版本对应：

`docs/architecture/memory-v2-roadmap.md`

中的：

`阶段三：Embedding 与混合 RAG`

本任务书提前为第二阶段完成后的开发准备。

---

## 一、前置条件

开始开发前必须确认当前仓库已经完成 Memory V2 第二阶段。

预期基线：

- 项目版本：`3.0.0a2`
- Memory V2 第一阶段已经完成不可逆切换与身份安全提取
- Memory V2 第二阶段已经完成查询驱动的词法检索
- 存在可工作的 `MemoryQueryBuilder`
- 存在可工作的 `MemoryTargetResolver`
- 存在可工作的 `MemoryRetriever`
- 存在 SQLite FTS5 记忆索引
- 存在确定性 `MemoryRanker`
- 人物和群硬过滤发生在词法候选 SQL 中
- 默认不加载最近发言群友的长期事实
- `last_used_at` 只在事实真正进入模型上下文后更新
- 当前没有 Embedding、向量数据库或 LLM rerank

开始前记录：

1. 当前 HEAD commit。
2. 当前项目版本。
3. 当前 Alembic head。
4. 当前 Memory V2 包结构。
5. 当前 FTS 表、触发器与健康检查。
6. 当前 `MemoryQuery`、候选模型、命中模型和排序接口。
7. 当前 Memory V2 测试数量。
8. 当前质量检查结果。

如果第二阶段尚未完整完成：

- 列出缺失的前置条件；
- 停止本阶段开发；
- 不把第二阶段和第三阶段合并成一次大范围补写；
- 不在缺少人物硬过滤的情况下提前加入向量检索。

若实际仓库已经超过 `3.0.0a2`，先阅读后来已有实现，再在最新结构上完成同等目标，不得覆盖已存在且正确的功能。

---

## 二、版本目标

本版本将 Memory V2 从：

```text
人物/群硬过滤
→ SQLite FTS5 词法候选
→ 确定性排序
→ 上下文
```

升级为：

```text
当前问题
→ 后端确定人物和群目标
→ 词法候选
→ Qwen query embedding
→ 目标范围内的语义候选
→ Reciprocal Rank Fusion
→ 确定性排序
→ 独立实体块
→ ContextBudgeter
```

核心目标：

1. 使用 Qwen Embedding API 为 Memory V2 事实建立可重建的向量索引。
2. 文档向量异步生成，不能阻塞事实写入和正常聊天。
3. 查询向量按需生成，每个相关检索请求最多生成一次。
4. 人物和群硬过滤必须发生在语义相似度计算之前。
5. 不允许全库向量搜索后再判断事实属于谁。
6. 将词法检索与语义检索融合，不以向量检索取代 FTS。
7. 使用确定性的 Reciprocal Rank Fusion，避免直接混合不可比较的 BM25 和余弦分数。
8. Embedding API 不可用时，明确降级为第二阶段的词法检索。
9. 关系数据库仍是唯一事实源。
10. `memory_embeddings` 只是派生索引，可以删除并重建。
11. 模型或维度变化时，新旧向量不能混用。
12. 不在主聊天上下文中暴露原始向量、API 请求或检索内部评分。
13. 不增加额外聊天 LLM 或 rerank LLM 调用。
14. 保持 Memory V2 身份安全写入规则不变。
15. 保持 Plugin API 主版本 `1.0`。

---

## 三、默认 Embedding 方案

本版本实现一个正式生产 Provider：

```text
provider_id = qwen_dashscope
model_id = qwen3.7-text-embedding
dimensions = 1024
output_type = dense
```

调用阿里云百炼 DashScope 原生文本向量 HTTP API。

默认采用：

- 文档事实：`text_type=document`
- 当前查询：`text_type=query`
- 查询可使用有界英文 `instruct`
- 输出只使用 dense vector
- 1024 维作为默认配置
- Provider 通过批量请求索引事实
- Provider 使用 `httpx.AsyncClient`
- 不增加 `dashscope` Python SDK 依赖
- 不增加 OpenAI SDK 依赖
- API 地址、模型、维度、批量大小全部可配置

截至任务书编写时，官方文档说明：

- `qwen3.7-text-embedding` 默认支持 1024 维；
- 支持 `text_type=query/document`；
- 支持 `instruct`；
- 单次列表输入最多 20 条；
- 支持 dense、sparse 和 dense&sparse；
- 本版本只使用 dense。

真实服务限制只能在一个 Provider 能力声明中维护，不得在 Worker、配置、测试和业务服务中复制多套相同常量。

本版本只实现 Qwen DashScope Provider 和 Fake Provider。

不要同时实现多个云厂商 Provider。

---

## 四、必须保持的不变量

以下规则来自 Memory V2 第一、第二阶段，不得削弱：

1. 模型不能提交任意 QQ 号、群号或证据事件 ID。
2. 每个主事件独立提取。
3. 自动记忆仍只允许后端提供的主体引用。
4. `memory_facts` 是事实真相来源。
5. `memory_evidence` 是证据来源。
6. `memory_facts_fts` 是可重建派生索引。
7. 新增向量索引同样是可重建派生索引。
8. person、person_group、group 三种作用域严格隔离。
9. active、superseded、invalidated 状态继续有效。
10. 自动事实不能覆盖显式事实。
11. 默认不加载最近发言群友的长期事实。
12. 其他人物只能来自当前真实 mention 或 reply 身份。
13. 当前人物、当前群和引用人物继续使用独立实体块。
14. FTS 无命中时不加载全部事实。
15. Embedding 无命中时也不加载全部事实。
16. 不读取 Memory V1 数据。
17. 不自动重建历史聊天。
18. Plugin API 主版本保持 `1.0`。
19. Planner 和主模型不能选择人物主体。
20. 向量相似度不能改变人物归属。

---

## 五、开始前必须阅读

至少阅读：

- `docs/architecture/memory-v2-roadmap.md`
- 当前 Memory V2 架构文档
- 第二阶段词法检索文档
- `src/qq_ai_bot/memory/enums.py`
- `src/qq_ai_bot/memory/models.py`
- `src/qq_ai_bot/memory/query.py`
- `src/qq_ai_bot/memory/targets.py`
- `src/qq_ai_bot/memory/fts.py`
- `src/qq_ai_bot/memory/retrieval.py`
- `src/qq_ai_bot/memory/ranking.py`
- `src/qq_ai_bot/memory/repository.py`
- `src/qq_ai_bot/memory/service.py`
- `src/qq_ai_bot/memory/context.py`
- `src/qq_ai_bot/memory/worker.py`
- `src/qq_ai_bot/memory/metrics.py`
- `src/qq_ai_bot/services/context_assembler.py`
- Core 记忆工具
- 管理员记忆命令
- Plugin MemoryFacade
- RuntimeConfig 与 Settings
- 组合根和生命周期系统
- 当前数据库模型
- Memory V2 Alembic 迁移
- 当前 FTS5 迁移
- 当前测试 Fixture 和 Fake 服务
- 当前 HTTP Client、Secret 和日志脱敏实现

路径应以当前仓库真实结构为准。

---

## 六、总体架构

目标架构：

```text
MemoryFactService
    ↓
MemoryEmbeddingJobRepository
    ↓
MemoryEmbeddingWorker
    ↓
EmbeddingDocumentBuilder
    ↓
QwenDashScopeEmbeddingProvider
    ↓
MemoryEmbeddingRepository
    ↓
memory_embeddings
```

查询链：

```text
MemoryQueryBuilder
    ↓
MemoryTargetResolver
    ↓
MemoryRetriever
    ├── MemoryLexicalIndex
    └── MemorySemanticIndex
            ↓
       EmbeddingQueryBuilder
            ↓
       QwenDashScopeEmbeddingProvider
    ↓
MemoryHybridRanker
    ↓
MemoryContextService
    ↓
ContextAssembler
```

组件建议：

```text
EmbeddingProvider
EmbeddingProviderProfile
EmbeddingProviderCapabilities
QwenDashScopeEmbeddingProvider
FakeEmbeddingProvider
EmbeddingDocumentBuilder
EmbeddingQueryBuilder
Float32VectorCodec
MemoryEmbeddingRepository
MemoryEmbeddingJobRepository
MemoryEmbeddingWorker
MemorySemanticIndex
MemoryHybridRanker
MemoryEmbeddingHealthService
MemoryEmbeddingMetrics
```

不要在 `MemoryRetriever` 中直接发送 HTTP 请求。

不要在 `ContextAssembler` 中计算向量。

不要在数据库 Repository 中拼接 DashScope 请求。

---

## 七、建议目录

在当前 `src/qq_ai_bot/memory/` 下增加或扩展：

```text
embedding/
├── __init__.py
├── models.py
├── provider.py
├── qwen.py
├── fake.py
├── text.py
├── codec.py
├── repository.py
├── jobs.py
├── worker.py
├── semantic.py
├── health.py
└── metrics.py
```

混合排序可以放在：

```text
memory/ranking.py
```

或增加：

```text
memory/hybrid.py
```

根据第二阶段实际结构选择一个位置。

必须保持以下职责分离：

```text
远程 API
向量编码
持久化
后台任务
语义候选
混合排序
上下文投影
```

不要建立与现有 MemoryRetriever 平行的第二套完整检索系统。

---

## 八、Embedding 领域模型

定义严格模型。

### `EmbeddingProviderProfile`

至少包含：

```text
provider_id
model_id
dimensions
output_type
document_template_version
endpoint_identity
fingerprint
```

`fingerprint` 必须由以下非 Secret 信息稳定计算：

```text
provider_id
model_id
dimensions
output_type
document_template_version
endpoint_identity
```

不得包含 API Key。

### `EmbeddingProviderCapabilities`

至少包含：

```text
max_batch_size
supports_query_document_type
supports_instruct
supported_dimensions
```

Qwen Provider 的真实服务限制集中在这里。

### `EmbeddingUsage`

至少包含：

```text
input_count
input_tokens
request_id
```

未知 Token 时允许为 `None`。

### `EmbeddingVector`

至少包含：

```text
values
dimensions
```

向量模型必须验证：

- 维度正确；
- 所有值为有限浮点数；
- 向量非零；
- 输入顺序与输出索引一致。

### `EmbeddingBatchResult`

至少包含：

```text
vectors
usage
```

### `MemoryEmbeddingRecord`

至少包含：

```text
fact_id
profile_id
content_hash
vector
created_at
updated_at
```

### `MemorySemanticCandidate`

至少包含：

```text
fact_id
target
cosine_similarity
semantic_rank
```

### `MemoryHybridHit`

扩展当前命中模型，至少支持：

```text
lexical_rank
semantic_rank
lexical_score
semantic_score
fusion_score
sources
selection_reason
```

不要把向量本身放入普通命中结果。

---

## 九、EmbeddingProvider 协议

定义：

```python
class EmbeddingProvider(Protocol):
    @property
    def profile(self) -> EmbeddingProviderProfile: ...

    @property
    def capabilities(self) -> EmbeddingProviderCapabilities: ...

    async def embed_documents(
        self,
        texts: tuple[str, ...],
    ) -> EmbeddingBatchResult: ...

    async def embed_query(
        self,
        text: str,
    ) -> EmbeddingBatchResult: ...

    async def close(self) -> None: ...
```

要求：

1. 文档与查询接口明确分开。
2. Provider 返回向量顺序必须与输入顺序一致。
3. Provider 自己负责按真实服务上限分批。
4. Worker 不复制 Provider 的最大批量常量。
5. Provider 不能访问 Memory Repository。
6. Provider 不能记录原始文本。
7. Provider 错误使用稳定错误模型。
8. Provider 必须支持取消。
9. `CancelledError` 原样传播。
10. API Key 不出现在 repr、日志、异常、数据库和指标中。

---

## 十、Qwen DashScope Provider

实现：

```text
QwenDashScopeEmbeddingProvider
```

使用共享：

```text
httpx.AsyncClient
```

不要为每次调用创建新 Client。

### 请求地址

配置保存 DashScope API base URL，例如：

```text
https://<workspace-id>.cn-beijing.maas.aliyuncs.com/api/v1
```

Provider 在一个集中函数中构造：

```text
POST {base_url}/services/embeddings/text-embedding/text-embedding
```

不要在多个方法重复拼接 URL。

### 文档请求

请求体语义：

```json
{
  "model": "qwen3.7-text-embedding",
  "input": {
    "texts": [
      "..."
    ]
  },
  "parameters": {
    "text_type": "document",
    "dimension": 1024,
    "output_type": "dense"
  }
}
```

### 查询请求

请求体语义：

```json
{
  "model": "qwen3.7-text-embedding",
  "input": {
    "texts": [
      "..."
    ]
  },
  "parameters": {
    "text_type": "query",
    "dimension": 1024,
    "output_type": "dense",
    "instruct": "Retrieve personal memory facts relevant to the conversational query."
  }
}
```

`instruct` 来自配置，只在 query 请求使用。

### 响应

解析：

```text
output.embeddings[*].embedding
output.embeddings[*].text_index
request_id
usage
```

必须：

- 按 `text_index` 恢复输入顺序；
- 拒绝缺失索引；
- 拒绝重复索引；
- 拒绝数量不匹配；
- 拒绝维度不匹配；
- 拒绝 NaN 和 Infinity；
- 不接受 sparse-only 返回；
- 不将原始响应写日志。

### 错误分类

至少区分：

```text
embedding_authentication_failed
embedding_rate_limited
embedding_timeout
embedding_provider_unavailable
embedding_invalid_request
embedding_invalid_response
embedding_cancelled
```

错误模型至少包含：

```text
code
public_message
retryable
```

只有后台文档索引任务使用重试。

实时 query embedding 不在同一聊天轮次内循环重试多次。

---

## 十一、文本构造与隐私

新增：

```text
EmbeddingDocumentBuilder
EmbeddingQueryBuilder
```

### 文档文本

事实文档只允许使用：

- `kind`
- `category`
- `memory_key`
- `content`

建议稳定模板：

```text
Kind: {kind}
Category: {category}
Key: {memory_key}
Fact: {content}
```

模板版本写入：

```text
document_template_version
```

模板变化必须产生新的 Provider Profile fingerprint，并触发重新索引。

禁止发送：

- QQ 号；
- 群号；
- fact_id；
- evidence_id；
- source_event_id；
- source_speaker_user_id；
- 关系分数；
- 好感度；
- 完整聊天历史；
- 证据摘录；
- 系统提示词；
- 其他人物事实；
- API Key。

### 查询文本

查询只允许使用第二阶段已经确定的：

- 当前消息文本；
- 有界回复文本；
- 有界 Planner intent。

不要加入：

- 全部上下文；
- 全部事实；
- QQ 号；
- 群号；
- 隐藏推理；
- Tool 输出；
- 其他群历史。

### 长度

事实本身已经有数据库长度约束。

Embedding 文本长度限制必须通过统一配置与 Provider 能力处理。

不要在 DocumentBuilder、Provider 和 Worker 分别写三个长度常量。

---

## 十二、向量编码

本版本不引入：

- sqlite-vec；
- Qdrant；
- pgvector；
- Milvus；
- FAISS；
- NumPy。

使用：

```text
SQLite BLOB + float32
```

实现：

```text
Float32VectorCodec
```

规则：

1. 写入前进行 L2 归一化。
2. 使用稳定 little-endian float32 编码。
3. 解码时验证字节长度等于 `dimensions * 4`。
4. 拒绝非有限值。
5. 拒绝零向量。
6. 余弦相似度对归一化向量使用点积。
7. 点积使用稳定、可测试的实现。
8. 不在数据库中保存 JSON 浮点数组。
9. 不在普通日志中打印向量。
10. 不把向量返回给 Agent、管理员普通列表或插件。

选择 SQLite BLOB 的原因：

- 当前每个人物和群的事实数量有界；
- 查询先进行人物和群硬过滤；
- 每次只加载目标范围内的向量；
- 不需要全库近似最近邻；
- 避免引入额外向量服务。

---

## 十三、数据库迁移

创建下一条 Alembic 迁移。

若第二阶段为 `0021`，本阶段预计为：

```text
0022
```

以当前真实 head 为准。

新增：

### `memory_embedding_profiles`

至少包含：

```text
id
fingerprint
provider_id
model_id
dimensions
output_type
document_template_version
endpoint_identity
created_at
```

约束：

- `fingerprint` 唯一；
- dimensions 为正整数；
- 不保存 API Key；
- 不保存完整 Authorization Header。

### `memory_embeddings`

至少包含：

```text
id
fact_id
profile_id
content_hash
vector_blob
created_at
updated_at
```

约束：

- `fact_id + profile_id` 唯一；
- fact 删除时 embedding 删除；
- profile 删除时 embedding 删除；
- `content_hash` 非空；
- `vector_blob` 非空。

### `memory_embedding_jobs`

至少包含：

```text
id
fact_id
profile_id
content_hash
status
attempts
next_attempt_at
created_at
updated_at
error_category
```

状态：

```text
pending
processing
done
failed
```

约束：

- `fact_id + profile_id` 唯一；
- fact 删除时 job 删除；
- profile 删除时 job 删除；
- attempts 非负；
- 不保存事实正文；
- 不保存 API 请求和响应。

### 迁移行为

迁移只建立表和索引。

迁移不能调用远程 API。

应用首次启动当前 Embedding Profile 时：

```text
创建或读取 profile
→ 找出当前 active facts 中缺少正确向量的事实
→ 只创建 pending jobs
```

这属于派生索引回填，不属于聊天历史重建。

### downgrade

本阶段迁移可以 downgrade：

- 删除 embedding jobs；
- 删除 embeddings；
- 删除 profiles；
- 不删除 memory_facts；
- 不删除 memory_evidence；
- 不删除 FTS。

Memory V2 `0020` 仍保持不可逆。

---

## 十四、Profile 与模型切换

必须支持：

```text
模型变化
维度变化
Endpoint identity 变化
文档模板版本变化
```

这些变化产生新的 profile fingerprint。

检索只使用当前配置对应的一个 active profile。

禁止：

- 混合不同模型向量；
- 混合不同维度向量；
- 将旧 profile 向量当作新 profile 使用；
- 在配置变化后原地修改旧向量的 profile 元数据。

新 profile 启用流程：

```text
创建新 profile
→ 为 active facts 创建 jobs
→ 新向量逐步生成
→ 当前查询在有覆盖时使用新 profile
→ 无覆盖事实仍由 FTS 检索
```

旧 profile 向量可以保留供诊断，管理员命令可以清理非当前 profile。

不要在应用启动时同步等待所有事实向量化完成。

---

## 十五、Embedding Job

实现持久队列。

### enqueue

以下情况创建或更新 job：

1. 新 active fact 创建；
2. 新 fact supersede 旧 fact；
3. explicit fact 更新为新版本；
4. 当前 profile 首次启用；
5. content_hash 与现有 embedding 不一致；
6. 管理员执行 embedding rebuild。

相同：

```text
fact_id + profile_id
```

只能存在一个 job。

### claim

Worker 可以一次 claim 多个 job。

要求：

- 按 profile 分组；
- 按 Provider capabilities 分批；
- 只索引 active fact；
- content_hash 必须仍与当前 fact 一致；
- stale job 不调用 API，直接重新 enqueue 当前 hash 或标记跳过；
- 不把不同 profile 放进同一 API 请求。

### complete

成功后在同一事务中：

```text
upsert memory_embedding
→ job status = done
```

### fail

失败后：

- retryable 使用配置化退避；
- 非 retryable 标记 failed；
- 错误只保存稳定 error_category；
- 不保存 API 响应正文；
- 不保存原始事实文本。

### reconciliation

应用启动后进行有界数据库协调：

```text
当前 active facts
- 当前 profile 的有效 embeddings
→ enqueue missing/stale jobs
```

协调只写 job，不调用 API。

不要每次启动删除全部向量。

---

## 十六、MemoryEmbeddingWorker

新增独立 Worker。

职责：

1. 读取 pending jobs。
2. 加载对应 facts。
3. 使用 `EmbeddingDocumentBuilder` 生成文本。
4. 按 Provider profile 批量调用 API。
5. 验证输出。
6. 编码 float32 BLOB。
7. 写入 embedding。
8. 完成 job。
9. 更新指标。
10. 支持优雅关闭和取消。

要求：

- 事实写入不等待 Worker；
- Worker 与 Memory Extraction Worker 分开；
- 一个 Worker 失败不影响聊天；
- `CancelledError` 原样传播；
- 停止时不把已取消任务记为永久失败；
- 不在日志中输出事实正文；
- 不在日志中输出 API Key；
- 不使用主聊天 LLM 并发槽位；
- 可以使用独立 HTTP 并发配置；
- 批量大小来自 Provider capabilities 和运行配置；
- 不能超过 Provider 的真实最大批量；
- Provider 内部仍负责最终拆批。

---

## 十七、事实生命周期接入

事实服务仍是唯一写入入口。

新增一个小型协议，例如：

```text
MemoryEmbeddingScheduler
```

MemoryFactService 在成功提交事实后通知调度器。

调度失败不能回滚事实。

正确语义：

```text
事实事务提交成功
→ 尝试 enqueue embedding job
→ enqueue 失败由 reconciliation 修复
```

不要让：

```text
Embedding API 失败
→ 事实写入失败
```

对于：

- superseded fact；
- invalidated fact；

普通语义检索通过 facts.status 过滤排除。

可以保留其旧向量，不必同步删除。

事实删除时通过外键级联删除向量与 job。

---

## 十八、MemorySemanticIndex

实现：

```text
MemorySemanticIndex
```

建议接口：

```python
async def search(
    *,
    target: MemoryEntityTarget,
    query_vector: EmbeddingVector,
    profile: EmbeddingProviderProfile,
    candidate_limit: int,
    kinds: tuple[MemoryKind, ...],
) -> tuple[MemorySemanticCandidate, ...]
```

SQL 必须先硬过滤：

```text
scope_type
subject_user_id
group_id
status = active
valid_until
kind
profile_id
content_hash 与 fact 当前内容一致
```

然后只把目标范围内的向量加载到 Python。

禁止：

```text
读取全库向量
→ Python 计算相似度
→ 再删除其他人物
```

禁止让 query vector 或 cosine score参与人物目标决定。

语义候选排序：

```text
cosine_similarity DESC
fact_id ASC
```

相似度最低阈值来自统一配置。

低于阈值的候选不返回。

不要在代码中复制多套阈值。

---

## 十九、查询向量

普通 `relevant` 模式：

1. `MemoryQueryBuilder` 产生一个 normalized query。
2. 查询非空且 semantic retrieval 启用时调用一次 `embed_query`。
3. 一个 turn 的所有 target 共用同一个 query vector。
4. 每个 target 使用相同 query vector，在各自硬过滤范围内搜索。
5. 不为每个 target 再调用一次 API。

`overview` 模式：

- 默认不调用 Embedding API；
- 继续使用第二阶段的结构化概览；
- 不把“你记得我什么”做成语义搜索全库。

短查询：

- 可以生成 query embedding；
- FTS 短查询 fallback 与语义检索并行存在；
- 人物与群硬过滤保持不变。

空查询：

- 不调用 Embedding API；
- relevant 模式不做语义搜索。

---

## 二十、混合检索

扩展当前 `MemoryRetriever`。

目标流程：

```text
lexical_candidates = FTS
semantic_candidates = Embedding
always_on_preferences = explicit preferences
hybrid_hits = fuse(lexical, semantic)
```

### 融合算法

使用：

```text
Reciprocal Rank Fusion
```

不要直接线性相加：

```text
BM25 原始分数 + cosine
```

因为两者量纲不同。

建议：

```text
fusion_score =
    lexical_weight / (rrf_k + lexical_rank)
  + semantic_weight / (rrf_k + semantic_rank)
```

规则：

- 未出现在某个候选源时，该源贡献为 0；
- lexical_weight、semantic_weight、rrf_k 来自配置；
- exact match 继续保留确定性优先级；
- importance、confidence、updated_at 作为融合后的稳定 tie-break；
- fact_id 作为最终稳定排序键；
- 同一 fact 只返回一次；
- 每个 target 独立 limit；
- 一个 target 不能挤掉另一个 target。

### 候选来源

命中结果至少记录：

```text
lexical
semantic
hybrid
always_on_preference
overview
```

主模型只看到简短：

```text
retrieval_reason
```

不要发送：

- cosine 原始向量；
- FTS 查询；
- RRF 公式；
- API request_id；
- 内部 profile fingerprint。

---

## 二十一、降级语义

Embedding 是派生能力。

以下情况必须使用第二阶段词法结果继续工作：

- Embedding 功能关闭；
- API Key 未配置；
- Query API 超时；
- 429；
- Provider 5xx；
- 当前 profile 覆盖率不足；
- 某些事实尚未完成向量化；
- 某个 target 没有向量候选。

要求：

1. 降级不加载全部事实。
2. 降级不跨人物。
3. 降级不改变 FTS 排序语义。
4. 降级记录指标和有界日志。
5. 降级不向用户暴露内部 API 错误，除非用户明确执行管理员诊断。
6. Query embedding 失败时，本轮不重复调用多次。
7. 文档 embedding 失败由后台 job 重试。
8. 不静默切换到另一个 Embedding 模型。
9. 不混用旧 profile。
10. 不因为 Embedding 不可用而阻止聊天。

`MemoryRetrievalResult` 可以增加：

```text
semantic_status
semantic_degraded
embedding_profile
```

这些字段仅供后端指标和诊断，不全部进入主模型上下文。

---

## 二十二、常驻偏好与 overview

第二阶段已有常驻显式偏好规则继续保留。

常驻显式偏好：

```text
kind = preference
source_type = explicit
status = active
```

不要求向量命中。

与混合结果去重。

`overview` 模式继续按：

```text
importance
confidence
updated_at
```

有界选择。

不要让 overview 默认调用 Qwen Embedding API。

---

## 二十三、上下文接入

`MemoryContextService` 继续是聊天上下文唯一入口。

`ContextAssembler` 不直接依赖 Embedding Provider。

调用关系：

```text
ContextAssembler
→ MemoryContextService
→ MemoryRetriever
→ lexical + semantic
```

实体块继续保持：

```text
current_person
current_person_in_group
current_group
referenced_people
```

每条事实允许增加：

```text
retrieval_reason
```

不要增加：

- semantic_score；
- lexical_score；
- fusion_score；
- profile_id；
- content_hash；
- vector。

`last_used_at` 仍然只更新最终进入上下文的 fact IDs。

---

## 二十四、Core、Admin 与 Plugin API

### Core Agent Tools

现有：

```text
get_person_memories
get_group_memories
```

提供 query 时自动使用混合 Retriever。

不要增加“使用向量”之类模型可选参数。

Embedding 是否启用由后端配置决定。

模型不能要求：

```text
semantic_only
ignore_identity_filter
global_vector_search
```

### 管理员命令

新增或扩展：

```text
/ai memory embedding status
/ai memory embedding doctor
/ai memory embedding retry
/ai memory embedding rebuild
/ai memory embedding purge-old
```

语义：

- `status`：显示当前 profile 与覆盖率；
- `doctor`：用固定无隐私测试文本验证 Provider；
- `retry`：重新激活 failed jobs；
- `rebuild`：为当前 active facts enqueue 当前 profile jobs；
- `purge-old`：删除非当前 profile 的 embeddings 和 jobs。

这些命令不直接修改 facts。

### Plugin API v1

保持 API 主版本。

Plugin `MemoryFacade.search()` 自动受益于混合检索。

插件不能：

- 获取原始向量；
- 指定 Provider；
- 指定 profile；
- 请求全库向量搜索；
- 关闭身份过滤；
- 访问 API Key。

本版本不向 Plugin SDK 公开 EmbeddingProvider。

---

## 二十五、配置

按当前 Settings 和 RuntimeConfig 风格实现。

### 启动 Secret 与连接配置

建议：

```text
MEMORY_EMBEDDING_ENABLED=false
MEMORY_EMBEDDING_PROVIDER=qwen_dashscope
MEMORY_EMBEDDING_BASE_URL=
MEMORY_EMBEDDING_API_KEY=
MEMORY_EMBEDDING_MODEL=qwen3.7-text-embedding
MEMORY_EMBEDDING_DIMENSIONS=1024
MEMORY_EMBEDDING_OUTPUT_TYPE=dense
MEMORY_EMBEDDING_DOCUMENT_TEMPLATE_VERSION=1
MEMORY_EMBEDDING_QUERY_INSTRUCT=Retrieve personal memory facts relevant to the conversational query.
MEMORY_EMBEDDING_REQUEST_TIMEOUT_SECONDS=20
```

Secret 只能来自启动环境。

API Key 不进入 RuntimeConfig 数据库。

### Worker 配置

建议：

```text
MEMORY_EMBEDDING_WORKER_ENABLED=true
MEMORY_EMBEDDING_WORKER_INTERVAL_SECONDS=5
MEMORY_EMBEDDING_WORKER_CLAIM_LIMIT=100
MEMORY_EMBEDDING_RETRY_ATTEMPTS=5
MEMORY_EMBEDDING_RETRY_INITIAL_SECONDS=30
MEMORY_EMBEDDING_HTTP_CONCURRENCY=2
```

### 检索配置

建议：

```text
memory.semantic_enabled
memory.semantic_candidate_limit
memory.semantic_min_similarity
memory.hybrid_lexical_weight
memory.hybrid_semantic_weight
memory.hybrid_rrf_k
```

要求：

- 默认值只在配置模型中定义一次；
- dimensions 必须为 Provider 支持值；
- output_type 第一版只能是 dense；
- 配置非法时明确失败；
- 不静默 clamp；
- Secret 不可热更新；
- model、dimensions、template version 变化产生新 profile；
- 检索权重可以热更新；
- 所有数量和阈值由配置声明；
- 业务代码不得再增加第二层固定上限。

---

## 二十六、指标与日志

新增不记录正文的指标：

```text
embedding_profile
document_jobs_pending
document_jobs_failed
document_embeddings_ready
document_embedding_coverage
document_embedding_requests
document_embedding_input_count
document_embedding_input_tokens
query_embedding_requests
query_embedding_input_tokens
query_embedding_failures
semantic_candidate_count
semantic_selected_count
hybrid_selected_count
semantic_degraded_count
embedding_latency
semantic_search_latency
hybrid_rank_latency
```

允许记录：

- profile fingerprint 前缀；
- provider_id；
- model_id；
- dimensions；
-数量；
-延迟；
-稳定错误分类；
- API request_id。

不得记录：

- API Key；
- Authorization Header；
- facts 正文；
- query 正文；
- evidence；
- QQ 号；
-群号；
-原始向量；
-完整 API 响应。

query 只能记录稳定 hash。

---

## 二十七、健康检查

`MemoryEmbeddingHealthService` 至少检查：

```text
enabled
provider_configured
current_profile
active_fact_count
ready_embedding_count
coverage_ratio
pending_job_count
processing_job_count
failed_job_count
stale_embedding_count
orphan_embedding_count
old_profile_count
last_success_at
last_error_category
```

应用 `healthz` 只能读取本地状态。

`healthz` 不调用远程 Qwen API。

`doctor` 才允许执行一次远程固定文本测试。

固定测试文本不得包含用户资料。

---

## 二十八、不要进行防御性编程

禁止：

1. Embedding 失败时加载该人物全部事实。
2. Embedding 失败时全库 FTS。
3. Embedding 失败时切换另一个模型。
4. 先全库向量搜索再在 Python 中过滤人物。
5. 为每种 scope 编写三套向量检索。
6. 在多个文件重复向量归一化。
7. 在多个文件重复 Provider 批量限制。
8. 在事实写入事务中调用远程 API。
9. 在应用启动时同步等待全量向量化。
10. 将 API 原始错误正文写入数据库。
11. 捕获所有异常并返回空结果。
12. 将非有限向量替换为零。
13. 将不同维度向量截断或补零。
14. 把旧 profile 标记成新 profile。
15. 引入无真实需求的向量数据库。
16. 在 Plugin SDK 暴露 API Key 或向量。
17. 让主模型选择 Provider 或检索模式。
18. 使用额外 LLM 做普通检索 rerank。
19. 修改 Memory V2 主体写入规则。
20. 自动重建历史聊天。

必要错误必须明确分类。

---

## 二十九、本版本不做

明确不实现：

- BGE-M3 本地部署；
- OpenAI Embedding Provider；
- 其他云厂商 Provider；
- sparse embedding；
- dense&sparse；
- qwen3-rerank；
- cross-encoder；
- LLM rerank；
- sqlite-vec；
- Qdrant；
- pgvector；
- FAISS；
- 全库 ANN；
- 第三方人物事实写入；
- 自动冲突推理；
- 历史聊天重建；
- Memory WebUI；
- Plugin API v2；
- 多活 Embedding Profile；
- 多模型投票。

这些属于以后版本。

---

## 三十、数据库迁移测试

至少覆盖：

1. 从第二阶段 Alembic head 升级到新 head。
2. memory_facts 完整保留。
3. memory_evidence 完整保留。
4. memory_facts_fts 完整保留。
5. 新三个 embedding 表存在。
6. 迁移不调用远程 API。
7. 新 embedding 表初始为空。
8. 应用启动 reconciliation 创建 pending jobs。
9. downgrade 只删除 embedding 表。
10. downgrade 不删除 facts、evidence 和 FTS。
11. `0020` 仍不可逆。
12. API Key 不写入数据库。

---

## 三十一、Provider 测试

使用：

```text
httpx.MockTransport
```

禁止默认测试访问真实 DashScope。

至少覆盖：

1. document 请求使用 `text_type=document`。
2. query 请求使用 `text_type=query`。
3. query 请求包含配置化 instruct。
4. 输出类型为 dense。
5. dimensions 正确传递。
6. 输入超过 Provider 批量限制时正确拆批。
7. 输出按 text_index 恢复顺序。
8. 缺失 index 被拒绝。
9. 重复 index 被拒绝。
10. 数量不匹配被拒绝。
11. 维度不匹配被拒绝。
12. NaN 被拒绝。
13. Infinity 被拒绝。
14. 零向量被拒绝。
15. 401 映射 authentication_failed。
16. 429 映射 rate_limited。
17. timeout 映射 timeout。
18. 5xx 映射 provider_unavailable。
19. 非法 JSON 映射 invalid_response。
20. CancelledError 原样传播。
21. API Key 不进入 repr。
22. API Key 不进入日志。
23. 原始文本不进入日志。
24. Client 正确关闭。

---

## 三十二、向量编码测试

至少覆盖：

1. float64 输入编码为 float32。
2. little-endian 格式稳定。
3. 解码维度正确。
4. 非法字节长度被拒绝。
5. 写入前 L2 归一化。
6. 解码后向量范数接近 1。
7. 点积等于归一化余弦。
8. 零向量被拒绝。
9. NaN 和 Infinity 被拒绝。
10. 编码结果不会出现在普通日志。

---

## 三十三、Worker 测试

至少覆盖：

1. 新 fact 创建后产生 job。
2. 事实写入不等待远程 API。
3. Worker 批量索引多个事实。
4. 不同 profile 不同批。
5. stale content_hash 不写旧向量。
6. 成功后 embedding 与 job 同事务完成。
7. retryable 错误重新排队。
8. 非 retryable 错误标记 failed。
9. CancelledError 不标记 failed。
10. Worker 重启后继续 pending jobs。
11. reconciliation 补齐遗漏 job。
12. 已有正确 embedding 不重复调用 API。
13. content 变化产生新 hash 和新 embedding。
14. superseded fact 不作为当前 active backfill 目标。
15. invalidated fact 不作为当前 active backfill 目标。
16. fact 删除后 embedding 和 job 级联删除。
17. model 切换创建新 profile。
18. dimensions 切换创建新 profile。
19. template version 切换创建新 profile。
20. 旧 profile 不进入当前检索。

---

## 三十四、语义身份隔离测试

构造多个用户和群，并使用可控 Fake Embedding。

至少覆盖：

1. 张三和李四有语义完全相同事实，检索张三只返回张三。
2. 两个群有语义相同事实，只返回当前群。
3. 同一人物两个群的 person_group 事实严格隔离。
4. 私聊不加载 group 或 person_group。
5. 当前消息无 mention/reply 时不检索其他人物。
6. mention 人物进入独立 target。
7. reply 人物进入独立 target。
8. Bot 不成为 target。
9. query vector 不含人物 ID。
10. document embedding 请求不含人物 ID。
11. SQL 先过滤 target，再加载向量。
12. Python 不接收全库向量。
13. Semantic threshold 不改变人物过滤。
14. 相似度最高的其他人物事实仍被排除。
15. 缺少当前 profile 向量的事实仍可由 FTS 命中。

---

## 三十五、混合检索测试

至少覆盖：

1. 同义表达无词面重叠时可由 semantic 命中。
2. 精确 memory_key 词法命中仍优先。
3. lexical-only fact 可以进入结果。
4. semantic-only fact 可以进入结果。
5. 同时命中时只返回一个 fact。
6. RRF 顺序稳定。
7. lexical_weight 变化按配置生效。
8. semantic_weight 变化按配置生效。
9. rrf_k 变化按配置生效。
10. exact match 保持确定性优先。
11. importance 只作为相关性后的 tie-break。
12. confidence 只作为相关性后的 tie-break。
13. updated_at 只作为相关性后的 tie-break。
14. fact_id 保证最终稳定顺序。
15. 每个 target 独立 limit。
16. 一个 target 不挤掉另一个 target。
17. 常驻 explicit preference 去重。
18. overview 不调用 Embedding API。
19. 空 query 不调用 Embedding API。
20. query embedding 每轮只调用一次。

---

## 三十六、降级测试

至少覆盖：

1. Embedding disabled 时结果等于第二阶段词法路径。
2. API Key 缺失时应用可在 embedding disabled 状态启动。
3. API Key 缺失且 embedding enabled 时启动明确失败。
4. query API 超时后使用 lexical 结果。
5. query API 429 后使用 lexical 结果。
6. query API 5xx 后使用 lexical 结果。
7. query API 失败后同一轮不重复调用。
8. 文档 embedding 失败不影响事实写入。
9. 向量覆盖率为零时仍可使用 FTS。
10. 部分覆盖时混合已就绪向量与完整 FTS。
11. 降级不加载全部事实。
12. 降级不跨人物。
13. 降级记录指标但不泄露正文。
14. 不自动切换模型。

---

## 三十七、上下文测试

至少覆盖：

1. 语义命中进入正确 current_person block。
2. current_group 命中只进入 current_group block。
3. person_group 命中只进入正确群内 block。
4. referenced person 使用独立 block。
5. 主模型上下文不包含 vector。
6. 主模型上下文不包含 semantic score。
7. 主模型上下文不包含 profile fingerprint。
8. retrieval_reason 有界。
9. ContextBudgeter 删除的事实不更新 last_used_at。
10. 最终注入事实更新 last_used_at。
11. semantic-only 命中可以更新 last_used_at。
12. lexical fallback 命中可以更新 last_used_at。
13. 同一事实只更新一次。
14. 其他人物事实不进入当前人物 block。

---

## 三十八、接口测试

至少覆盖：

1. Core person memory query 自动使用混合检索。
2. Core group memory query 自动使用混合检索。
3. Core Tool 不能指定 Provider。
4. Core Tool 不能指定 profile。
5. Core Tool 不能请求全库向量搜索。
6. 管理员 memory search 使用同一 Retriever。
7. Plugin MemoryFacade.search 使用同一 Retriever。
8. Plugin 不能获取原始向量。
9. Plugin 不能跨用户或跨群。
10. list 和 evidence 接口保持正常。
11. Embedding 管理命令不修改 facts。
12. rebuild 只创建 jobs。
13. purge-old 不删除当前 profile。
14. doctor 使用固定无隐私文本。

---

## 三十九、性能与成本测试

构造：

- 100 个用户；
- 每个用户 100 条 person facts；
- 多个群；
- person_group facts；
- 大量跨人物语义相似文本。

验证：

1. 语义搜索只加载目标主体向量。
2. 不把全库向量载入 Python。
3. 查询只调用一次 query embedding。
4. 文档索引使用批量 API。
5. API 调用数量有统计。
6. API Token 使用量有统计。
7. query 文本和 fact 文本不进入日志。
8. 向量 BLOB 大小符合 dimensions * 4。
9. 1024 维单条向量占用约 4096 字节，不保存 JSON 数组。
10. 混合检索延迟有界。
11. Embedding 关闭时没有任何远程请求。
12. 普通 overview 没有远程请求。
13. Token 和上下文字符数不因原始向量增加。

性能测试只建立稳定回归，不写未经测量的绝对延迟承诺。

---

## 四十、真实 API 可选测试

真实 DashScope 测试必须标记：

```text
@pytest.mark.qwen_embedding_integration
```

只有以下环境变量存在时运行：

```text
QWEN_EMBEDDING_INTEGRATION_ENABLED=true
MEMORY_EMBEDDING_BASE_URL=...
MEMORY_EMBEDDING_API_KEY=...
```

真实测试只使用固定无隐私文本。

CI 默认不得调用真实 API。

真实测试验证：

- document embedding；
- query embedding；
- 1024 维；
- dense 输出；
- query/document 区分；
- Provider health。

不得发送真实用户记忆。

---

## 四十一、实施顺序

1. 记录当前基线与第二阶段完成状态。
2. 阅读 Memory V2、FTS 和检索实现。
3. 增加 Embedding 领域模型和错误模型。
4. 实现 `EmbeddingProvider` 协议。
5. 实现 `FakeEmbeddingProvider`。
6. 实现 `Float32VectorCodec`。
7. 创建 embedding 数据库迁移。
8. 实现 profile、embedding 和 job Repository。
9. 实现 `EmbeddingDocumentBuilder`。
10. 实现 `EmbeddingQueryBuilder`。
11. 实现 Qwen DashScope Provider。
12. 实现 `MemoryEmbeddingWorker`。
13. 实现启动 reconciliation。
14. 将 fact 生命周期接入 job enqueue。
15. 实现 `MemorySemanticIndex`。
16. 扩展 MemoryRetriever。
17. 实现 RRF 混合排序。
18. 接入 MemoryContextService。
19. 更新 Core/Admin/Plugin 接口。
20. 增加状态、doctor、retry、rebuild 和 purge-old。
21. 增加健康检查和指标。
22. 完成迁移、Provider、Worker、身份、融合和降级测试。
23. 更新文档与版本。
24. 运行完整质量检查。
25. 提交代码。

---

## 四十二、版本与文档

将版本提升为：

```text
3.0.0b1
```

更新：

- `pyproject.toml`
- `src/qq_ai_bot/__init__.py`
- `CHANGELOG.md`
- `README.md`
- `.env.example`
- `docs/architecture/memory-v2-roadmap.md`
- Memory V2 架构文档
- Memory Retrieval 文档
- Embedding 配置文档
- 管理命令文档
- Plugin MemoryFacade 文档
- 隐私说明
- 部署说明

路线文档标记：

```text
阶段一：已完成
阶段二：已完成
阶段三：已完成
阶段四：未开始
```

文档必须明确：

1. Qwen API 只接收有界事实文本和当前检索问题。
2. 不发送 QQ 号、群号、证据和完整聊天历史。
3. 向量是派生索引。
4. 数据库事实仍是唯一真相来源。
5. 人物和群过滤发生在语义相似度计算之前。
6. API 不可用时退回 FTS。
7. 不混用不同模型和维度。
8. 不自动扫描历史聊天。
9. 当前没有 rerank 模型。
10. 当前没有向量数据库服务。

---

## 四十三、质量检查

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
uv run pytest -q tests/unit -k "memory or embedding"
uv run pytest -q tests/integration -k "memory or embedding"
```

检查没有引入额外向量数据库：

```bash
grep -R "qdrant\|pgvector\|sqlite_vec\|sqlite-vec\|faiss\|milvus" pyproject.toml uv.lock src
```

检查没有在日志中打印向量和正文：

```bash
grep -R "logger.*embedding\|logger.*vector\|logger.*fact.content\|logger.*query.text" src/qq_ai_bot/memory
```

人工检查：

- API Key 不在仓库；
- base URL 示例不含真实 Workspace ID；
- 测试 Fixture 不含真实人物记忆；
- CI 不调用真实 DashScope；
- 应用在 Embedding disabled 时正常启动。

---

## 四十四、完成报告

完成后输出：

1. 开始 HEAD commit。
2. 最终 commit。
3. 当前项目版本。
4. 当前 Alembic head。
5. 新建和修改文件。
6. EmbeddingProvider 协议。
7. Qwen Provider 请求格式。
8. Qwen Provider 错误分类。
9. 当前 Provider Profile。
10. Profile fingerprint 组成。
11. 向量 BLOB 编码格式。
12. 新数据库表和索引。
13. Worker 的批量与重试方式。
14. Fact 写入如何与 embedding 解耦。
15. Reconciliation 如何补齐遗漏任务。
16. 人物和群硬过滤发生的位置。
17. 语义候选如何计算。
18. FTS 与语义候选如何融合。
19. RRF 公式和配置。
20. Query embedding 每轮调用次数。
21. Overview 是否调用 Embedding。
22. API 故障时的降级路径。
23. Core/Admin/Plugin 接入方式。
24. 健康检查和管理员命令。
25. 新增配置。
26. API 隐私边界。
27. 迁移测试结果。
28. Provider 测试结果。
29. Worker 测试结果。
30. 身份隔离测试结果。
31. 混合检索测试结果。
32. 降级测试结果。
33. 上下文测试结果。
34. 性能与成本回归结果。
35. 全部测试数量和结果。
36. Ruff 结果。
37. mypy 结果。
38. Alembic 结果。
39. Docker 结果。
40. 真实 Qwen API 测试是否运行。
41. 尚未完成事项。
42. 是否引入向量数据库。
43. 是否存在全库向量搜索后再过滤人物的路径。
44. 是否存在 Embedding 失败后加载全部事实的路径。
45. 是否混用不同模型或维度。
46. 是否在事实写入事务中调用远程 API。
47. 是否向主模型、插件或日志暴露原始向量。
48. 是否调用额外 LLM 完成 rerank。
49. 是否自动扫描历史聊天。
50. 是否把 API Key 写入数据库、RuntimeConfig、日志或 Prompt。

第 42 项预期：

```text
没有。当前使用 SQLite BLOB 保存当前人物或群范围内的派生向量。
```

第 43 项预期：

```text
不存在。SQL 先按人物、群、状态和 profile 硬过滤，再加载目标范围向量。
```

第 44 项预期：

```text
不存在。Embedding 不可用时只使用第二阶段 FTS 结果。
```

第 45 项预期：

```text
没有。检索只使用当前配置对应的单一 profile。
```

第 46 项预期：

```text
没有。事实先提交，后台 Worker 再生成向量。
```

第 47 项预期：

```text
没有。
```

第 48 项预期：

```text
没有。混合排序使用确定性 Reciprocal Rank Fusion。
```

第 49 项预期：

```text
没有。历史重建属于后续阶段。
```

第 50 项预期：

```text
没有。
```
