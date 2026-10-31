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

模型 bundle 的 `[embedding]` 选择 `qwen_dashscope` 或 `openai_compatible`，配置端点、模型、
维度、`api_key_env` 别名与 `enabled`。段存在时以段为准；`enabled=false` 明确关闭，不回退到
旧设置。段尚不存在的部署暂沿 `MEMORY_EMBEDDING_*` 初始化，迁移退出条件见模型配置升级说明。
密钥由同目录 `model_profiles.secrets.json` 或所引用的进程环境变量提供，不进入可读配置、profile、
数据库、日志、命令输出或健康响应。profile 指纹保持现有 provider、端点、模型、维度和模板身份。

非聊天连接变化及其密钥轮换如实报告 `apply_mode=restart`；已经启动的连接与原向量 profile
保持原事实，重启后按新配置生成新的不可变 profile，旧向量不覆盖或自动删除。聊天连接的原热应用
路径保留。OpenAI 兼容端点使用 `/embeddings`，真实协议错误只按类别报告，不尝试另一种协议或端点。

## 存储与任务

- `memory_embedding_profiles`：不可变配置指纹及非密钥能力信息。
- `memory_embeddings`：`fact_id + profile_id` 唯一，保留原 id、内容哈希、时间和真实
  fact/profile 外键；原始向量正文不再放在这张普通表。
- `memory_vectors_<profile_id>`：同一 SQLite 数据库中的 Vec1 虚表。rowid 为原 embedding id，
  metadata fact_id 供本次授权 SQL 下推；vector 保存原 little-endian float32 BLOB。每个不可变
  profile 一张表，不复制人物权限，也不保存第二份应用向量。
- `memory_embedding_jobs`：持久化 pending/processing/failed/done 状态、原 claim、重试时刻、
  尝试次数与无正文错误类别。

事实写入事务提交后才排队。排队和启动协调统一选择当前 active、contested 事实，排除隔离和
过期事实；使用当前 profile，不读取聊天历史。调度故障在事实服务中记录一次，不改变已提交的结果。
文档内容变化会产生新哈希并重新生成；事实删除通过外键级联删除向量和任务。模型或模板配置
变化会建立新 profile，旧 profile 数据保持隔离，直到管理员显式清理。
元数据删除触发器在同一事务清理对应 Vec1 值，包含事实/人物清退的 FK 级联；purge-old
先完成原级联，再删除旧 profile 的触发器和虚表。回滚保留原元数据与向量配对。

固定 SQLite 官方 Vec1 0.8 源码随平台 wheel/镜像构建；每个物理连接在迁移、启动检查和业务
SQL 之前加载包内扩展，随后关闭扩展装载能力。开发环境先运行 `python scripts/build_vec1.py`；
运行时不编译、不下载，也不在缺少原生模块时换另一套存储。关闭 Embedding HTTP 服务不会
关闭数据库的 Vec1 能力，已有删除触发器仍可工作。

正式 profile 使用 flat cosine 原生 NN，无启动训练或按规模自动切换。SQL 当前授权/profile/
类型/日期/有效性限定 fact ID，metadata IN 在原生 top-k 选择内生效；同一个显式读取快照中
计量事实与索引行并召回四倍语义候选，只有候选正文与向量进入原 hash、codec 和 fsum 精排。
输出分数仍来自原 codec。有限候选可能漏掉旧 binary64 分数的全局最优或同分项，因此以
`semantic_approximate` 独立说明；不把原生 f32 候选精排宣传为原算法的全局精确 top-k。
coverage 对照当前合法事实与 Vec1 值的数量，并核候选内容 hash；不在每次查询全库 hash 正文，
不声称证明了未召回项的内容新鲜度。正常近似/结果上限不是故障；缺失向量、已发现 stale hash
和实际原生查询失败分别报告，失败继续沿既有 FTS 降级链。

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
与 failed 任务保留原领取和尝试记录；可恢复失败按配置间隔重新领取原任务，不按次数封口。
显式 rebuild 也不会夺取相同内容的在飞请求。

每次领取不超过当前 Provider 声明的一次 HTTP 批容量；一批返回后立即按原 claim 保存，
下一批失败不会撤销已经保存的向量。领取满批时继续排空就绪任务，不等待普通轮询间隔；
失败批次仍沿原任务、尝试次数和重试时间恢复。

完成按 128 条页一次读取任务、一次读取事实小列，hash 在锁外计算；短 writer 复核原 claim 的
updated_at 和 attempts，仅成功 CAS 的结果发布元数据及 Vec1 值。Vec1 替换与元数据 upsert
共用同一个 writer/事务；虚表不假装支持 SQLite UPSERT。late complete/fail/skip 都必须携带
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
- `retry`：按 128 条页把当前 profile 的失败任务重新排队，显式重置其尝试计数。
  原状态和时间戳条件写入，新的时间戳严格晚于旧值；墙上时钟停滞或回拨时不能复用旧 claim。
- `rebuild`：为当前 active facts 建立当前 profile 的任务，不修改事实或 FTS。
- `purge-old`：删除非当前 profile 的旧向量、任务和 profile。

升级 0153 前停止旧 Bot writer、备份 `data/`，用含固定扩展的新构建执行
`uv run alembic upgrade head`，再只重建 Bot。迁移保全部原 embedding id/profile/hash、任务和
原始向量字节，不重购 Embedding；普通表旧 vector_blob 列在同次迁移清退。`--sql` 不能执行
这次数据搬迁，迁移中断回滚保旧表示。

```bash
docker compose up -d --build --no-deps bot
```

只重建 Bot 时，QQ 网关容器与登录态无需重建。外部 API 故障不会令健康检查主动访问网络，也不会阻止 Bot
启动；可通过 status/doctor 和不含正文的计数判断积压，再在恢复后执行 retry。

所有主库写入使用共享 `immediate_session()`，在连接获取前排队；普通查询使用不自动提交的
`read_session()`。embedding HTTP、文档 hash 和 vector 编码继续在 writer 外完成，原 claim/profile
字段在短提交内核验，不新增逐事实远程验证。
原生计算沿 SQLite driver 线程执行；取消等实际操作/physical_exit 结束后才释放连接与 writer，
不承诺取消协程会立即终止扩展内部距离循环。手动训练可在 reader/离线环境生成模型，安装模型
的 rebuild 是受控维护写操作，不混进普通查询或每次启动。
