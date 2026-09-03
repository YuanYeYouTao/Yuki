> **历史设计/非现行合同。** 本文保留当时的方案或实测口径，不用于当前授权、调度或使用率判断。
> 当前实现以 [Memory 架构](../memory-v2.md)、[检索合同](../memory-v2-retrieval.md)
> 和 [P1 治理任务书](../Yuki-Memory-P1治理任务书.md) 为准；本文不在当前实施导航内。

# Codex 任务：Yuki Memory V2 第一阶段

你是一名资深 Python、SQLAlchemy、Alembic、LLM 结构化提取和对话记忆架构工程师。

请在仓库：

`YuanYeYouTao/Yuki-QQbot`

当前 `main` 基础上实现 Memory V2 第一阶段。

先读取并遵守：

`docs/architecture/memory-v2-roadmap.md`

若该文件尚未存在，先将随任务提供的路线文档保存到该路径，再开始开发。

---

## 一、任务性质

这是一次已经决定的、不可逆的数据库和记忆子系统重构。

不要提出保留旧表、双写、灰度迁移或自动导入旧记忆的方案。

第一阶段完成后：

- 旧记忆表及数据不存在；
- 新记忆库为空；
- 新消息开始使用 Memory V2；
- 历史聊天仍保存在 `chat_events`；
- 不自动从历史聊天重建记忆。

目标版本：

`3.0.0a1`

开始前记录：

- 当前 HEAD commit；
- 当前项目版本；
- 当前 Alembic head；
- 当前记忆相关测试数量。

---

## 二、必须先阅读的代码

至少阅读：

- `src/qq_ai_bot/persistence/models.py`
- `src/qq_ai_bot/persistence/memory_repository.py`
- `src/qq_ai_bot/persistence/repository_records.py`
- `src/qq_ai_bot/persistence/repositories.py`
- `src/qq_ai_bot/services/memory_worker.py`
- `src/qq_ai_bot/services/context_assembler.py`
- `src/qq_ai_bot/services/agent_tools.py`
- 管理员记忆服务和命令
- `src/qq_ai_bot/plugin_host/facades.py`
- `src/yuki_plugin_sdk/context.py`
- 所有 memory 相关测试
- 所有 Alembic 迁移
- 组合根与生命周期注册
- `.env.example`
- `README.md`
- `CHANGELOG.md`

使用当前真实代码结构，不按旧任务书猜路径。

---

## 三、破坏性迁移

创建下一条 Alembic 迁移。

迁移必须删除：

- `person_memories`
- `group_memories`
- `person_group_memories`
- `person_preferences`
- `memory_jobs`

不迁移任何旧数据。

随后创建：

- `memory_facts`
- `memory_evidence`
- 新版 `memory_jobs`

### `memory_facts`

至少包含：

- `id`
- `scope_type`
- `subject_user_id`
- `group_id`
- `kind`
- `memory_key`
- `category`
- `content`
- `normalized_content`
- `importance`
- `confidence`
- `source_type`
- `status`
- `supersedes_id`
- `valid_from`
- `valid_until`
- `created_at`
- `updated_at`
- `last_used_at`

约束：

```text
scope_type = person
→ subject_user_id 非空
→ group_id 为空

scope_type = person_group
→ subject_user_id 非空
→ group_id 非空

scope_type = group
→ subject_user_id 为空
→ group_id 非空
```

字段约束：

```text
importance: 1 到 5
confidence: 0 到 1
kind: fact / preference / episode
status: active / superseded / invalidated
source_type: automatic / explicit / rebuild
```

为三个作用域分别建立“一个主体、kind、memory_key 只能存在一个 active 事实”的唯一索引。

### `memory_evidence`

至少包含：

- `id`
- `fact_id`
- `event_id`
- `source_speaker_user_id`
- `relation`
- `excerpt`
- `created_at`

约束：

- `fact_id + event_id` 唯一；
- 事实删除时证据删除；
- `event_id` 指向真实 `chat_events`；
- `source_speaker_user_id` 指向真实人物。

### 新版 `memory_jobs`

至少包含：

- `id`
- `event_id`
- `conversation_key`
- `status`
- `attempts`
- `next_attempt_at`
- `created_at`
- `updated_at`
- `error_category`

`event_id` 唯一。

### downgrade

`downgrade()` 必须明确抛出异常，说明：

```text
Memory V2 cutover is irreversible; restore the pre-upgrade database backup.
```

不要重建旧表。

---

## 四、删除旧实现

删除或完全替换：

- 旧记忆 SQLAlchemy 模型；
- 旧 `MemoryRepository`；
- 旧 `MemoryWorker`；
- 旧 `MemoryRecord` 和 `PreferenceRecord` 依赖；
- 旧记忆任务处理；
- 旧表测试；
- 所有旧记忆兼容代码。

禁止保留：

- `LegacyMemoryRepository`
- `MemoryV1Adapter`
- 双写
- 旧表只读查询
- 旧数据导入
- 启动时自动 backfill
- 兼容视图

完成后全仓库搜索旧表名，业务代码、文档和测试中不得继续依赖它们；破坏性迁移和升级文档允许提及旧表名。

---

## 五、新领域包

创建：

```text
src/qq_ai_bot/memory/
├── __init__.py
├── enums.py
├── models.py
├── extraction.py
├── subjects.py
├── validation.py
├── repository.py
├── service.py
├── worker.py
├── context.py
└── testing.py
```

职责：

### `enums.py`

定义：

- `MemoryScopeType`
- `MemoryKind`
- `MemorySourceType`
- `MemoryStatus`
- `MemoryEvidenceRelation`
- `MemoryJobStatus`

### `models.py`

定义严格领域对象：

- `MemoryFact`
- `MemoryEvidence`
- `MemoryFactCreate`
- `MemoryFactQuery`
- `MemoryJob`
- `MemoryContextBlock`

不要让领域模型依赖 MCP、NoneBot 或 Plugin SDK。

### `repository.py`

只负责持久化：

- 查询 active facts；
- 创建事实；
- 增加证据；
- 同键事实替代；
- 失效事实；
- 任务 enqueue / claim / complete / fail。

不要把 LLM Prompt 和上下文组装写进 Repository。

### `service.py`

实现事实业务规则：

- 相同主体、作用域、kind、memory_key；
- 规范化内容相同则复用事实并增加证据；
- 内容变化时创建新事实并 supersede 旧事实；
- automatic 不得替代 explicit；
- 失败不留下半写入状态。

---

## 六、身份安全提取

第一阶段只允许提取：

1. 当前消息发送者关于自己的事实；
2. 当前群本身的群事实。

暂不支持：

- 群友描述第三个人；
- 通过昵称猜人物；
- 通过语义相似度猜人物；
- 从最近发言者列表选择主体；
- 从历史上下文单独生成事实。

### 主事件

每次模型提取只处理一个主事件。

可以附带同一 conversation 的少量前文，但前文仅用于理解主事件，不能作为独立事实来源。

禁止将多个主事件合并到同一个 `MemoryExtractionOutput`。

### 模型输入

后端生成：

```text
primary_event
available_subjects
conversation_context
```

`available_subjects` 第一阶段最多包含：

```text
speaker
group
```

不要给模型可自由提交的 QQ 号、群号或 event_id 参数。

### 模型输出

定义严格 Pydantic Schema：

```text
MemoryExtractionOutput
  claims: tuple[MemoryClaim, ...]

MemoryClaim
  subject_ref
  scope_type
  kind
  memory_key
  category
  content
  importance
  confidence
  source_type
```

禁止字段：

- `user_id`
- `group_id`
- `source_event_id`
- `source_speaker_user_id`
- `status`
- `supersedes_id`
- 时间字段

### 后端映射

`SubjectResolver` 必须确定性映射：

```text
speaker
→ 主事件 sender_user_id

group
→ 主事件 group_id
```

验证：

- 私聊没有 group；
- `person` 只能使用 speaker；
- `person_group` 只能使用 speaker 且主事件在群里；
- `group` 只能使用 group；
- 证据 event_id 固定为主事件；
- 证据 source_speaker_user_id 固定为主事件发送者。

模型返回未知 subject_ref 时拒绝整条 claim，不得猜测。

---

## 七、Memory Worker V2

实现新的 Worker：

1. 只为真实入站、非 Bot 消息创建任务。
2. 每个 event 只有一个 job。
3. claim 可以一次取得多个 job，但必须逐个调用提取器并逐个提交。
4. 不同 event 不能共享一个结构化输出。
5. 前文只能来自同一精确 conversation。
6. 每次调用只允许主事件产生证据。
7. `CancelledError` 原样传播。
8. LLM/结构化输出失败使用现有任务重试机制。
9. 日志不记录完整用户文本和完整模型输出。
10. 完成后保存事实与证据，再将 job 标为 done。

保留现有 `ModelTask.MEMORY_EXTRACTION` 路由，继续使用 Flash 模型。

不要加入 Embedding。

---

## 八、上下文改造

更新 `ContextAssembler`。

第一阶段只注入：

- 当前人物 `person` active facts；
- 当前人物在当前群的 `person_group` active facts；
- 当前群 `group` active facts。

默认不注入：

- 最近发言群友的 person facts；
- 最近发言群友的 person_group facts；
- 未被当前消息明确询问的其他人物事实。

历史发言人的身份元数据应由 `chat_events` 保存并随每条消息直接投影，不设置独立人物列表；
其他人的长期事实仍只在当前消息明确提及、回复或询问时加载。

上下文必须按实体块输出，例如：

```json
{
  "current_person": {
    "user_id": "10001",
    "facts": []
  },
  "current_person_in_group": {
    "user_id": "10001",
    "group_id": "20001",
    "facts": []
  },
  "current_group": {
    "group_id": "20001",
    "facts": []
  }
}
```

每条事实至少包含：

- fact_id
- kind
- category
- content
- importance
- confidence
- source_type
- updated_at

第一阶段仍按重要度和更新时间取有限数量；查询驱动检索属于第二阶段。

---

## 九、现有功能接入

必须更新以下调用方，不能简单删除功能：

### Core Agent Tools

更新：

- `get_person_memories`
- `get_group_memories`

使用 Memory V2 Repository/Service。

输出中使用新 `fact_id`。

### 管理员记忆功能

更新：

- 添加显式人物事实；
- 修改显式人物事实；
- 删除或失效人物事实；
- 列出人物事实；
- 查看事实证据。

管理员显式写入：

```text
source_type = explicit
```

### Plugin API

保持 Plugin API 主版本不变。

`MemoryFacade` 继续支持：

- list_person
- list_group
- search
- add
- update
- delete

内部改用 Memory V2。

对插件返回兼容字段，同时增加：

- `fact_id`
- `confidence`
- `status`
- `evidence_count`

插件不能指定任意 source_event_id。

### Context 与 Prompt

更新 Prompt 规则：

```text
每条事实只属于所在 entity block。
不得把 current_group 或其他人物的信息归给 current_person。
没有事实时不得猜测。
```

---

## 十、版本和文档

将项目版本调整为：

`3.0.0a1`

更新：

- `CHANGELOG.md`
- `README.md`
- `.env.example`
- `docs/architecture/memory-v2-roadmap.md`
- 记忆架构文档
- 升级指南

升级指南必须明确：

```text
升级会永久删除所有旧人物记忆、群记忆、群内人物记忆、偏好和旧记忆任务。
不会删除聊天事件账本。
不会自动重建历史。
唯一回退方式是恢复升级前完整数据库备份。
```

不要将该说明弱化为普通补丁提示。

---

## 十一、第一阶段不做

禁止在本任务加入：

- Embedding；
- BGE-M3；
- 向量数据库；
- sqlite-vec；
- Qdrant；
- FTS5；
- BM25；
- Reranker；
- 历史全量重建；
- 自动 third-party person memory；
- 复杂冲突检测 Agent；
- Memory UI；
- 新 Plugin API 主版本。

这些已经写入后续路线。

---

## 十二、测试要求

### 破坏性迁移

1. 从旧 2.1.2 schema 创建测试数据库。
2. 写入：
   - people；
   - groups；
   - memberships；
   - chat_events；
   - 旧人物记忆；
   - 旧群记忆；
   - 旧群内人物记忆；
   - 旧偏好；
   - 旧 memory_jobs。
3. 执行 `alembic upgrade head`。
4. 验证：
   - 旧五张表不存在；
   - 新三张表存在；
   - 新记忆表为空；
   - people/groups/memberships/chat_events 保留；
   - 关系、自动化、插件和其他非记忆表不受影响。
5. 验证 downgrade 明确失败。

### 身份归属

6. 张三说“我准备考研”，只能写入张三。
7. 张三说“李四准备考研”，第一阶段不能写入李四。
8. 两个不同群的消息不会进入同一个提取输出。
9. 两个不同私聊用户不会进入同一个提取输出。
10. 模型返回任意 user_id 字段时 Schema 校验失败。
11. 模型返回未知 subject_ref 时不写数据库。
12. 私聊产生 group claim 时不写数据库。
13. source_event_id 始终等于主事件。
14. source_speaker_user_id 始终等于真实发送者。

### 事实版本

15. 同一事实重复出现时增加证据，不重复建立 active fact。
16. 同键内容变化时创建新 fact，并 supersede 旧 fact。
17. automatic 不能替代 explicit。
18. 事务失败不会留下事实无证据或证据无事实。

### 上下文

19. 当前人物只看到自己的 person facts。
20. 当前群只看到该群 group facts。
21. 当前人物群内事实只在正确群出现。
22. 最近发言群友的长期事实默认不进入上下文。
23. 不同人物的 facts 位于不同实体块。
24. 没有任何 V1 MemoryRecord 被上下文读取。

### 现有接口

25. Core memory tools 使用 V2。
26. 管理员添加、修改、删除使用 V2。
27. Plugin MemoryFacade 使用 V2。
28. 现有非记忆测试继续通过。
29. 应用在空新记忆库下可启动和聊天。
30. Memory Worker 仍使用 Flash `ModelTask.MEMORY_EXTRACTION`。

### 删除验证

31. 业务源码中不存在旧表模型引用。
32. 业务源码中不存在旧 `MemoryRepository`。
33. 业务源码中不存在旧 `services/memory_worker.py`。
34. 不存在 legacy adapter、dual-write 或 old-memory importer。
35. 不存在启动时历史 backfill。

---

## 十三、质量检查

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

额外执行：

```bash
grep -R "PersonMemoryModel\|GroupMemoryModel\|PersonGroupMemoryModel\|PersonPreferenceModel" src tests
grep -R "class MemoryRepository" src
grep -R "services.memory_worker" src tests
```

除破坏性迁移和历史说明文档外，不应再有旧实现依赖。

---

## 十四、实施顺序

1. 保存路线文档。
2. 添加 Memory V2 枚举和领域模型。
3. 添加新数据库表。
4. 编写破坏性 Alembic 迁移。
5. 实现 V2 Repository。
6. 实现 SubjectResolver 和 ClaimValidator。
7. 实现 V2 Service。
8. 实现 V2 Worker。
9. 更新组合根和生命周期。
10. 更新 ContextAssembler。
11. 更新 Core Tools。
12. 更新管理员服务。
13. 更新 Plugin Facade。
14. 删除旧模型、Repository、Worker 和测试。
15. 增加迁移与身份隔离测试。
16. 更新版本和文档。
17. 运行全部质量检查。
18. 提交代码。

不要先删除旧代码再让项目长时间处于无法导入状态。按可测试的小步提交，但最终分支中不保留旧实现。

---

## 十五、完成报告

完成后输出：

1. 开始 HEAD commit。
2. 最终 commit。
3. 当前 Alembic head。
4. 删除的表。
5. 新建的表。
6. 删除的源码文件。
7. 新建的源码文件。
8. Memory V2 领域模型。
9. SubjectResolver 的允许主体。
10. 模型不再能填写哪些身份字段。
11. Worker 如何保证每个事件独立提取。
12. ContextAssembler 如何隔离人物。
13. Core/Admin/Plugin API 如何接入 V2。
14. 旧数据是否被迁移。
15. 是否存在双写或旧兼容层。
16. 是否自动重建历史。
17. 测试总数和结果。
18. Ruff 结果。
19. mypy 结果。
20. Alembic 旧库升级测试结果。
21. Docker 构建结果。
22. 未完成事项。
23. 是否加入了 Embedding、FTS 或向量数据库。
24. 全仓库仍存在的旧记忆实现引用。

第 14 项预期：

```text
没有。旧记忆和偏好数据被不可逆删除。
```

第 15 项预期：

```text
不存在。
```

第 16 项预期：

```text
没有。第一阶段只处理升级后的新消息。
```

第 23 项预期：

```text
没有。Embedding 与混合 RAG 属于后续阶段。
```
