# Memory V2 Embedding 与混合 RAG

## 定位与检索

Embedding 是事实库的可选派生索引；`memory_facts`、真实证据和 canonical owner 仍是事实源。
向量、profile 和任务可以重建，不引入第二套记忆所有权。

主 Agent 通过 `search_memory` 按需查询，不执行每轮自动预取。
授权 SQL 先筛选人物、群及可见 SELF，词法与向量候选在获准范围内全局排序并用 RRF 合并。
显式目标和内部精确目标读取仍限制到解析后的目标，语义相似不能扩大权限。
检索合同见 [当前检索](memory-v2-retrieval.md)。

同一 query embedding 可在合法查询范围间复用，profile/query 哈希键的有界进程缓存不把
原查询写入数据库。overview 不生成 query embedding；语义服务失败时明确标记降级，
继续返回词法候选。扫描、候选数量和索引覆盖不足均明确报告，空结果不证明全库无记忆。

## 文档模板与隐私

文档模板 v1 投影 `kind`、`category`、`memory_key` 和 `content`，不另取 owner ID、
证据正文、聊天历史、系统提示词或管理员权限。正文可能含真实姓名；当前投影会用 `[id]`
替换匹配的数字 ID，并按 `MEMORY_EMBEDDING_MAX_TEXT_CHARACTERS` 限制发给 Embedding
的文档和查询，不修改事实正文。该投影上限与主检索 query 不截断是不同环节。
日志、健康检查和指标只记录数量、耗时、错误类别、profile 指纹等无正文元数据。

DashScope API Key 仅通过 `MEMORY_EMBEDDING_API_KEY` 读取，不进入 profile、数据库、日志、
命令输出或健康响应。profile 指纹由 provider、端点身份、模型、维度、输出类型、模板版本和
query instruct 等非密钥配置生成。

## 存储与任务

- `memory_embedding_profiles`：不可变配置指纹及非密钥能力信息。
- `memory_embeddings`：`fact_id + profile_id` 唯一，保存内容哈希、维度和 little-endian
  float32 BLOB。
- `memory_embedding_jobs`：持久化 pending/running/retry/failed/done 状态、租约、尝试次数与
  无正文错误类别。

事实写入事务提交后才排队。启动时只协调当前 active facts 与当前 profile，不读取聊天历史。
文档内容变化会产生新哈希并重新生成；事实删除通过外键级联删除向量和任务。模型或模板配置
变化会建立新 profile，旧 profile 数据保持隔离，直到管理员显式清理。

按 fact ID 的 correction 会创建新 fact，因此生成新的 FTS row 和 Embedding job；旧版本进入
superseded 后不会参与普通语义检索。只改变 authority、confidence、conflict_state 或
last_confirmed_at 不改变文档正文，不会重复向量化。Embedding 故障也不会阻断修正、证据保存或
状态事件事务。

## 配置

新部署默认请求启用；旧部署显式设置 `false` 时继续关闭：

```dotenv
MEMORY_EMBEDDING_ENABLED=true
MEMORY_EMBEDDING_PROVIDER=qwen_dashscope
MEMORY_EMBEDDING_BASE_URL=
MEMORY_EMBEDDING_API_KEY=
MEMORY_EMBEDDING_MODEL=qwen3.7-text-embedding
MEMORY_EMBEDDING_DIMENSIONS=1024
MEMORY_EMBEDDING_OUTPUT_TYPE=dense
MEMORY_EMBEDDING_DOCUMENT_TEMPLATE_VERSION=1
MEMORY_EMBEDDING_QUERY_INSTRUCT=Retrieve personal memory facts relevant to the conversational query.
MEMORY_EMBEDDING_REQUEST_TIMEOUT_SECONDS=20
MEMORY_EMBEDDING_MAX_TEXT_CHARACTERS=4000
MEMORY_EMBEDDING_WORKER_ENABLED=true
MEMORY_EMBEDDING_WORKER_INTERVAL_SECONDS=5
MEMORY_EMBEDDING_WORKER_CLAIM_LIMIT=100
MEMORY_EMBEDDING_RETRY_ATTEMPTS=5
MEMORY_EMBEDDING_RETRY_INITIAL_SECONDS=30
MEMORY_EMBEDDING_HTTP_CONCURRENCY=2
MEMORY_EMBEDDING_QUERY_CACHE_TTL_SECONDS=600
MEMORY_EMBEDDING_QUERY_CACHE_MAX_ENTRIES=512
```

混合检索可热更新：

```dotenv
MEMORY_SEMANTIC_ENABLED=true
MEMORY_SEMANTIC_CANDIDATE_LIMIT=50
MEMORY_HYBRID_LEXICAL_WEIGHT=1.0
MEMORY_HYBRID_SEMANTIC_WEIGHT=1.0
MEMORY_HYBRID_RRF_K=60
```

启用但缺少 base URL 或 API Key 时，Bot 仍可启动，向量状态为 `not_configured`，
查询继续使用 FTS，结果标记为非穷尽。只有实际配置了两项凭据才创建 Embedding Provider。
管理页可保存全局开关，重启 Bot 后生效；旧部署显式关闭的配置优先于新默认值。
当前实现只接受 `qwen_dashscope`、dense 与 1024 维，避免 profile 声明和真实向量不一致。
查询缓存只存在于 Bot 进程内，重启即清空；TTL 和容量是启动配置，不影响数据库 schema。

普通 reconcile 按内部 fact ID 做 128 条 keyset 页，集合连接当前 profile 的任务与向量；已有
向量只在事实 updated_at 较新时重新准备 hash。每页在只读连接读取小列并算 hash，writer 只按
事实字段快照及任务 id/profile/content_hash/status/updated_at 做 CAS。相同内容的 processing
与 failed 任务保留原领取和尝试预算；显式 rebuild 也不会夺取相同内容的在飞请求。

完成按 128 条页一次读取任务、一次读取事实小列，hash 在锁外计算；短 writer 复核原 claim 的
updated_at 和 attempts，仅成功 CAS 的结果批量 upsert 向量。late complete/fail/skip 都必须携带
原 claim。输入在准备后变化时只将该原领取返回 pending，下一次重新读取，不写旧向量。
启动恢复单独按 128 条页接管 interrupted processing，保留 attempts；同一 worker 重复 start
不执行恢复，普通 reconcile 也不会把在飞任务重置。

## 运维命令

```text
/ai memory embedding status
/ai memory embedding doctor
/ai memory embedding retry
/ai memory embedding rebuild
/ai memory embedding purge-old
```

- `status`：查看开关、当前 profile、覆盖率和任务计数。
- `doctor`：用固定无隐私测试文本执行一次 Provider 远程连通性与维度检查。
- `retry`：按 128 条页把当前 profile 的失败任务重新排队，显式重置其重试预算。
  原状态和时间戳条件写入，新的时间戳严格晚于旧值；墙上时钟停滞或回拨时不能复用旧 claim。
- `rebuild`：为当前 active facts 建立当前 profile 的任务，不修改事实或 FTS。
- `purge-old`：删除非当前 profile 的旧向量、任务和 profile。

部署时先备份 `data/`，执行 `uv run alembic upgrade head`，再只重建 Bot：

```bash
docker compose up -d --build --no-deps bot
```

NapCat 容器与 QQ 登录态无需重建。外部 API 故障不会令健康检查主动访问网络，也不会阻止 Bot
启动；可通过 status/doctor 和不含正文的计数判断积压，再在恢复后执行 retry。
