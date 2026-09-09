> **历史设计/非现行合同。** 本文保留当时的方案或实测口径，不用于当前授权、调度或使用率判断。
> 当前实现以 [Memory 架构](../memory-v2.md)、[检索合同](../memory-v2-retrieval.md)
> 和 [P1 治理任务书](../Yuki-Memory-P1治理任务书.md) 为准；本文不在当前实施导航内。

# Codex 任务：Yuki Memory V2 第二阶段——查询驱动的词法检索

你是一名资深 Python、SQLAlchemy、SQLite FTS5、信息检索、LLM Agent 上下文工程和测试工程师。

请在仓库：

`YuanYeYouTao/Yuki-QQbot`

当前 `main` 基础上开发：

`Yuki-QQbot 3.0.0a2`

本版本对应：

`docs/architecture/memory-v2-roadmap.md`

中的：

`阶段二：查询驱动的词法检索`

当前已知基线：

- 当前版本：`3.0.0a1`
- 当前基线提交：`16af16721513367e69129bbf986518f58661d240`
- 第一阶段已经完成 Memory V2 的不可逆切换、身份安全提取、事实证据模型和实体块隔离
- 当前 `ContextAssembler` 仍按重要度与更新时间读取大量 active facts
- 当前尚未加入 FTS、Embedding、向量数据库和历史重建

若实际仓库 HEAD 已经变化，先记录真实状态，再在最新代码上完成同等目标。不得覆盖后来已经存在且正确的功能。

---

## 一、版本目标

本版本把 Memory V2 从：

```text
按主体读取固定数量事实
→ 再依靠 ContextBudgeter 截取
```

升级为：

```text
当前消息
→ 后端确定检索主体
→ 人物/群作用域硬过滤
→ SQLite FTS5 词法候选
→ 确定性排序
→ 每个实体独立选择
→ ContextBudgeter
→ 只标记真正注入的事实为已使用
```

核心目标：

1. 根据当前问题选择相关事实，不再默认加载当前人物和当前群的大量长期事实。
2. 任何检索都先按人物和群硬过滤，再做词法匹配。
3. 同一句内容即使存在于多个人物名下，也只能返回目标人物的事实。
4. 当前人物、当前人物群内事实、当前群和明确引用的其他人物继续使用独立实体块。
5. 默认不加载最近发言群友的长期事实。
6. 明确提及或回复某人时，才允许检索该人的长期事实。
7. 增加可重建的 SQLite FTS5 派生索引。
8. 普通检索不增加任何 LLM 调用。
9. 为下一阶段 Embedding 和混合 RAG 保留候选与排序接口，但本版本不实现向量检索。
10. 保持 Memory V2 的身份安全写入规则不变。

---

## 二、必须保持的不变量

以下规则来自 `3.0.0a1`，不得削弱：

1. 模型不能提交任意 QQ 号、群号或证据事件 ID。
2. 每个主事件独立提取，不同事件不共享结构化输出。
3. 自动记忆第一阶段仍只允许 `speaker` 和 `group`。
4. `memory_facts` 是事实真相来源。
5. `memory_evidence` 是证据来源。
6. FTS 索引只是派生索引，可以删除并完整重建。
7. `person`、`person_group`、`group` 三种作用域继续严格隔离。
8. `active`、`superseded`、`invalidated` 状态继续有效。
9. 自动事实不能覆盖显式事实。
10. 默认不向当前人物上下文注入其他群友的长期事实。
11. 不读取或恢复任何 Memory V1 数据。
12. 不启动历史聊天重建。
13. Plugin API 主版本继续保持 `1.0`。

---

## 三、先阅读当前代码

开始开发前至少阅读：

- `docs/architecture/memory-v2-roadmap.md`
- Memory V2 当前架构文档
- `src/qq_ai_bot/memory/enums.py`
- `src/qq_ai_bot/memory/models.py`
- `src/qq_ai_bot/memory/repository.py`
- `src/qq_ai_bot/memory/service.py`
- `src/qq_ai_bot/memory/context.py`
- `src/qq_ai_bot/memory/worker.py`
- `src/qq_ai_bot/services/context_assembler.py`
- `src/qq_ai_bot/services/agent_tools.py`
- 管理员记忆服务和命令
- `src/qq_ai_bot/plugin_host/facades.py`
- `src/yuki_plugin_sdk/context.py`
- `src/qq_ai_bot/persistence/models.py`
- Alembic `0020`
- 现有 `chat_events_fts` 迁移和查询实现
- 所有 Memory V2 测试
- 当前运行时配置系统
- 当前组合根与生命周期注册

开始前记录：

- 当前 HEAD commit
- 当前项目版本
- 当前 Alembic head
- `memory_facts`、`memory_evidence`、`memory_jobs` 当前结构
- 当前 Memory V2 测试数量

---

## 四、总体架构

新增以下真实组件：

```text
MemoryQueryBuilder
MemoryTargetResolver
MemoryLexicalIndex
SQLiteMemoryFTSIndex
MemoryRetriever
MemoryRanker
MemoryContextService
MemoryRetrievalMetrics
```

目标调用链：

```text
InboundMessage
    ↓
MemoryQueryBuilder
    ↓
MemoryTargetResolver
    ├── current_person
    ├── current_person_in_group
    ├── current_group
    └── explicitly_referenced_person
    ↓
MemoryRetriever
    ├── always-on explicit preferences
    ├── FTS5 lexical candidates
    └── overview candidates
    ↓
MemoryRanker
    ↓
MemoryContextService
    ↓
ContextAssembler
    ↓
ContextBudgeter
    ↓
mark_used(final_fact_ids)
```

不要在 `ContextAssembler` 中直接拼接 FTS SQL。

不要把检索逻辑重新堆进 `MemoryFactService.list_person()`。

`MemoryFactService` 继续负责事实生命周期；`MemoryRetriever` 负责读取相关事实。

---

## 五、建议目录

在当前 `src/qq_ai_bot/memory/` 中增加：

```text
query.py
targets.py
fts.py
retrieval.py
ranking.py
metrics.py
```

根据实际代码可以调整文件边界，但必须保持：

```text
身份目标解析
词法索引
候选检索
排序
上下文投影
```

职责分离。

不要建立功能重复的第二个 Memory Repository。

---

## 六、领域模型

在 `memory/models.py` 或独立查询模型文件中增加严格模型。

### `MemoryRetrievalMode`

```text
relevant
overview
```

含义：

- `relevant`：根据当前请求检索相关事实。
- `overview`：用户明确询问“你记得我什么”“关于我知道什么”等记忆概览时，按重要度、置信度和时间返回事实。

### `MemoryTargetRole`

至少包含：

```text
current_person
current_person_group
current_group
referenced_person
referenced_person_group
```

### `MemoryEntityTarget`

字段建议：

```text
role
scope_type
subject_user_id
group_id
block_id
```

必须继续通过 `MemoryFactCreate` / `MemoryFactQuery` 的作用域约束。

### `MemoryQuery`

字段建议：

```text
text
normalized_text
mode
targets
kinds
candidate_limit
limit_per_target
```

模型必须是后端生成的，不允许主模型直接填写。

### `MemoryLexicalCandidate`

字段建议：

```text
fact_id
target
fts_rank
exact_match
matched_terms
```

### `MemoryRetrievalHit`

字段建议：

```text
fact
target
rank
lexical_score
exact_match
matched_terms
selection_reason
```

### `MemoryRetrievalResult`

字段建议：

```text
blocks
hits
candidate_count
selected_count
query_hash
mode
```

不要在这些模型中提前加入真实向量或 Embedding Provider。

可以让后续阶段增加新的候选来源，但本版本不建立空的向量实现。

---

## 七、MemoryTargetResolver

实现确定性目标解析。

### 始终允许的目标

私聊：

```text
current_person
```

群聊：

```text
current_person
current_person_group
current_group
```

### 其他人物目标

只有以下可信来源可以增加其他人物：

1. 当前真实 OneBot 事件中的 `mentioned_user_ids`；
2. 当前真实回复消息的 `reply_sender_user_id`。

排除：

- 当前发送者；
- Bot 自己；
- 不属于当前群的无效人物；
- 重复 ID。

对于群内明确引用的人物，可以增加：

```text
referenced_person
referenced_person_group
```

第一版不要使用：

- LLM 判断人物；
- Embedding 猜人物；
- 最近发言者自动成为检索目标；
- 模糊昵称匹配；
- 对常见中文词进行人名猜测；
- 全群成员扫描。

没有可信 mention/reply 时，其他人物事实不能进入候选范围。

---

## 八、MemoryQueryBuilder

根据以下可信输入建立查询：

- 当前真实消息文本；
- 当前真实回复文本；
- Planner 已生成的简短 intent；
- 当前 scope；
- 当前消息中的 mention/reply 身份元数据。

查询文本建议：

```text
current message
+ 有界 reply text
+ 有界 planner intent
```

不要加入：

- 完整聊天历史；
- 全部人物事实；
- 系统提示词；
- 其他群聊天；
- 模型隐藏推理。

### 概览模式

集中定义一组有测试覆盖的记忆概览表达，例如：

```text
你记得我什么
关于我你知道什么
我之前说过什么
你还记得哪些关于我的事
你对这个群记得什么
```

匹配概览意图时使用：

```text
MemoryRetrievalMode.OVERVIEW
```

普通消息使用：

```text
MemoryRetrievalMode.RELEVANT
```

概览判断不得调用 LLM。

不要把概览表达散落在多个业务文件。

---

## 九、SQLite FTS5 派生索引

创建下一条 Alembic 迁移，预计为 `0021`。

新增：

```text
memory_facts_fts
```

推荐使用 SQLite FTS5 外部内容表：

```sql
CREATE VIRTUAL TABLE memory_facts_fts USING fts5(
    content,
    memory_key,
    category,
    content='memory_facts',
    content_rowid='id',
    tokenize='trigram'
);
```

根据当前 SQLite 和已有 `chat_events_fts` 的实际实现调整语法，但必须支持中文子串检索。

建立同步触发器：

- `memory_facts` INSERT
- `memory_facts` DELETE
- `memory_facts` UPDATE

迁移时将当前 Memory V2 facts 回填到 FTS 索引。

注意：

- 这不是旧记忆迁移；
- 只索引现有 `memory_facts`；
- 事实内容仍以 `memory_facts` 为准；
- FTS 表可以完整删除和重建；
- `status`、人物、群、置信度等权威字段不存进 FTS 文本。

`0021` 可以提供可逆 downgrade：

```text
只删除 FTS 表和触发器
不删除 memory_facts
```

`0020` 的不可逆性质不变。

---

## 十、MemoryLexicalIndex

定义小而真实的协议：

```text
search
rebuild
health
```

### `search`

输入：

- 已确定的单个 `MemoryEntityTarget`
- 安全生成的词法查询
- candidate limit
- 可选 kinds

输出：

- `MemoryLexicalCandidate`

必须在同一个 SQL 查询中硬过滤：

```text
scope_type
subject_user_id
group_id
status = active
valid_until 尚未过期
可选 kind
```

然后再执行 FTS 匹配和排序。

禁止：

```text
先全库 FTS 搜索
→ 再在 Python 中删除其他人物
```

最终结果即使性能优化变化，也不得包含其他主体候选。

### 安全查询生成

不得把用户原文直接作为 FTS5 查询语法。

实现集中式安全词法查询构造：

1. Unicode NFKC；
2. casefold；
3. 压缩空白；
4. 移除或转义 FTS 运算符；
5. 提取有界数量的词项；
6. 由后端生成引号和 OR 表达式。

### 短查询

`trigram` 对短于 3 个字符的查询可能无法正常工作。

允许在已经完成主体和作用域硬过滤后，使用有界：

```text
LIKE
memory_key 精确匹配
category 精确匹配
```

作为短查询候选来源。

禁止对整个 `memory_facts` 表做无主体过滤的 LIKE 扫描。

---

## 十一、MemoryRanker

本版本使用确定性排序，不调用 LLM。

排序优先级建议：

```text
1. memory_key 精确匹配
2. normalized_content 完整短语匹配
3. FTS5 bm25 排名
4. importance
5. confidence
6. updated_at
7. fact_id 作为稳定最终排序键
```

要求：

- 相同输入得到稳定顺序；
- 不依赖数据库未定义顺序；
- 不让高重要度但完全不相关的事实压过明显词法命中；
- 不把其他人物相似事实纳入排序；
- 记录每个命中的确定性原因。

为下一阶段混合 RAG 保留“候选来源”和“排序组件”的清晰接口，但不要实现：

- semantic score；
- embedding score；
- reranker model；
- 向量归一化。

---

## 十二、无匹配和常驻偏好

普通 `relevant` 模式：

- 没有词法命中时，不得退回“加载该人物全部事实”；
- 不得随机选择高重要度事实；
- 可以只保留少量常驻显式交互偏好。

常驻偏好规则：

```text
kind = preference
source_type = explicit
status = active
```

数量来自统一配置。

常驻偏好与词法结果去重。

不要把所有自动推断偏好永久注入每轮。

`overview` 模式：

- 不要求 FTS 命中；
- 在每个目标内按 importance、confidence、updated_at 选择有界事实；
- 仍然保持主体和作用域隔离；
- 当前用户询问“你记得我什么”时，不得返回其他人物事实。

---

## 十三、MemoryRetriever

`MemoryRetriever` 是本版本唯一的普通相关事实入口。

职责：

1. 接收 `MemoryQuery`。
2. 对每个 `MemoryEntityTarget` 独立检索。
3. 获取常驻显式偏好。
4. relevant 模式使用 FTS。
5. overview 模式使用结构化概览查询。
6. 调用 `MemoryRanker`。
7. 每个 target 独立执行 limit。
8. 合并为独立 `MemoryContextBlock`。
9. 输出检索解释和指标。

公平性要求：

- 多人物查询时，每个 target 都有独立候选和返回上限；
- 不能让一个人物的大量命中挤掉另一个明确引用人物的全部事实；
- current_person、current_group、referenced_person 必须保持独立 block。

---

## 十四、MemoryContextService

新增一个面向聊天上下文的服务，组合：

```text
MemoryQueryBuilder
MemoryTargetResolver
MemoryRetriever
```

`ContextAssembler` 改为依赖该服务，不再直接调用：

```text
list_person
list_person_group
list_group
```

建议接口：

```python
async def retrieve_for_turn(
    *,
    inbound,
    content,
    planner_intent,
    runtime,
) -> MemoryRetrievalResult
```

`ContextAssembler` 继续负责：

- 人物基础身份；
- 关系；
- scene；
- 最近事件；
- ContextBudgeter；
- 最终历史消息。

MemoryContextService 只负责 Memory V2 事实检索。

---

## 十五、只标记实际使用的事实

`memory_facts.last_used_at` 只能在事实真正进入最终模型上下文后更新。

不要在以下阶段更新：

- FTS 候选命中；
- MemoryRanker 排名；
- 被 ContextBudgeter 删除；
- 管理员纯诊断列表；
- FTS rebuild。

改造 `ContextAssembler._fit_metadata()`，使其同时返回：

```text
metadata_payload
selected_fact_ids
```

随后通过 Memory V2 Service/Repository 一次性执行：

```text
mark_used(selected_fact_ids)
```

要求：

- 单次 SQL UPDATE；
- fact ID 去重；
- 只更新 active facts；
- 空集合不执行 SQL。

普通 Agent 工具主动返回给模型的相关事实，也可以标记为已使用。

---

## 十六、上下文格式

继续使用实体块，不得恢复平铺格式。

建议输出：

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
  },
  "referenced_people": [
    {
      "user_id": "10002",
      "person_facts": [],
      "group_facts": []
    }
  ]
}
```

允许根据当前 ContextContribution 结构调整，但必须保证：

- 每个事实所在 block 有明确 `user_id` / `group_id`；
- referenced person 不与 current person 混在同一 facts 数组；
- 不存在主体不明的事实；
- 当前群事实不能被包装成人物事实。

每条事实可以增加有界：

```text
retrieval_reason
```

但不要把原始 FTS 查询、SQL 或长分数解释发送给主模型。

---

## 十七、Core Agent Tools

更新：

```text
get_person_memories
get_group_memories
```

输入增加可选：

```text
query
mode
limit
```

规则：

- 提供 query 时使用 `MemoryRetriever`；
- mode=`overview` 时使用概览；
- 未提供 query 时保持确定性列表能力，主要供明确管理/查看场景；
- 普通聊天相关检索应优先由 ContextAssembler 自动完成；
- 工具不能跨人物权限范围；
- 输出包含：
  - fact_id
  - scope
  - subject
  - content
  - confidence
  - status
  - evidence_count
  - retrieval_reason

不要让模型提交 FTS 语法。

---

## 十八、管理员功能

增加确定性诊断：

```text
/ai memory search person <QQ> <query>
/ai memory search group <群号> <query>
/ai memory index status
/ai memory index rebuild
```

根据现有命令系统调整语法，但保持一个明确入口。

要求：

- search 使用同一个 `MemoryRetriever`；
- index rebuild 只重建派生索引；
- rebuild 不修改事实、证据和状态；
- status 显示：
  - fact count
  - indexed row count
  - missing row count
  - orphan row count
  - last rebuild time（若实现持久状态）
- 不输出完整用户查询到普通日志。

管理员直接列出全部事实时，不更新 `last_used_at`。

---

## 十九、Plugin API v1

保持现有 API 主版本。

`MemoryFacade.search()` 改为使用 `MemoryRetriever`，不能继续：

```text
读取固定 100 条
→ Python substring
```

Plugin Facade 必须：

- 先执行当前插件已有的人物/群作用域校验；
- 再调用硬过滤检索；
- 不允许插件通过 query 跨人物召回；
- 返回有界结果；
- 不接受原始 FTS 查询表达式。

`list_person()` 和 `list_group()` 可以继续用于确定性完整列表。

不要在本版本增加 Plugin API v2。

---

## 二十、配置

按照当前 Settings/RuntimeConfig 风格增加 Memory Retrieval 配置。

建议配置项：

```text
memory.retrieval_enabled
memory.lexical_candidate_limit
memory.context_limit_per_entity
memory.overview_limit_per_entity
memory.always_on_explicit_preference_limit
memory.query_term_limit
memory.short_query_fallback_enabled
```

环境变量名称按现有命名规则生成。

要求：

- 默认值只在配置模型中定义一次；
- 所有数量必须为正整数或明确允许 0；
- 不在业务代码中重复默认值；
- 不静默 clamp；
- 运行时非法值明确失败；
- 关闭 retrieval 时仍使用 Memory V2 的身份隔离，并回退到有界的当前主体列表，不得恢复其他群友记忆。

不要在本版本加入 Embedding 配置。

---

## 二十一、检索指标

新增不记录正文的指标：

```text
mode
query_hash
target_count
candidate_count
selected_count
context_selected_count
fts_latency
total_latency
overview_used
short_query_fallback_used
referenced_person_count
```

不得记录：

- 原始用户查询；
- 事实正文；
- 证据摘录；
- QQ Token；
- Prompt；
- 完整模型输入。

Debug 日志也只记录 hash 和数量。

---

## 二十二、索引健康与生命周期

FTS 索引应在以下情况保持正确：

- 新 fact 创建；
- fact 内容更新；
- fact superseded；
- fact invalidated；
- fact 删除；
- 数据库升级回填；
- 手动 rebuild。

即使 superseded/invalidated 事实仍存在于 FTS 物理索引，普通查询也必须通过 `memory_facts.status = active` 排除。

`MemoryLexicalIndex.health()` 至少检查：

- active fact 数；
- 可检索索引行数；
- missing facts；
- orphan index rows。

应用启动不应每次全量 rebuild。

索引损坏时：

- health 明确报告；
- 管理员可执行 rebuild；
- 不静默把全库事实塞进上下文。

---

## 二十三、不要进行防御性编程

禁止：

1. 为了“稳定”把所有事实作为 FTS 无匹配后的 fallback。
2. 先全库搜索再在 Python 中过滤人物。
3. 使用 LLM 判断目标人物。
4. 使用 Embedding 猜目标人物。
5. 为每种 scope 写三套重复检索实现。
6. 在多个文件复制查询正规化逻辑。
7. 捕获所有异常并返回空事实。
8. FTS 失败时静默退回全表扫描。
9. 在代码中写死大批中文关键词作为事实类别。
10. 建立第二套 Memory Fact 真相来源。
11. 改写 Memory V2 写入身份规则。
12. 自动扫描或重建旧聊天。
13. 提前加入向量依赖。
14. 修改 Plugin API 主版本。
15. 在普通日志中记录查询和事实全文。

必要错误应明确暴露为：

```text
memory_index_unavailable
memory_query_invalid
memory_target_invalid
memory_index_inconsistent
```

---

## 二十四、本版本不做

明确禁止实现：

- Embedding；
- BGE-M3；
- 向量数据库；
- sqlite-vec；
- Qdrant；
- pgvector；
- semantic score；
- cross-encoder reranker；
- LLM rerank；
- 历史聊天重建；
- 第三方人物事实写入；
- 模糊昵称人物识别；
- 全群人物事实自动加载；
- 冲突事实自动推理；
- Memory WebUI。

这些属于后续阶段。

---

## 二十五、数据库迁移测试

至少覆盖：

1. 从 `0020` 升级到新 head。
2. 现有 Memory V2 facts 被回填到 FTS。
3. `memory_facts` 数据完全保留。
4. `memory_evidence` 数据完全保留。
5. 新 fact 自动进入索引。
6. 内容更新后旧词不再命中，新词可以命中。
7. fact invalidated 后普通检索不返回。
8. fact superseded 后普通检索不返回旧版本。
9. 删除 fact 后无 orphan FTS 结果。
10. FTS rebuild 幂等。
11. `0021 downgrade` 只删除派生索引，不删除 facts。
12. `0020` 仍然不可逆。

---

## 二十六、核心检索测试

### 身份硬过滤

1. 张三和李四都有“喜欢数学”，检索张三只返回张三。
2. 两个群都有同名群事实，检索只返回当前群。
3. 同一人物在两个群有不同群内事实，只返回当前群。
4. 当前消息只提到自己时，不返回其他人物。
5. 最近发言者不自动成为事实检索目标。
6. `mentioned_user_ids` 中的人可以独立检索。
7. `reply_sender_user_id` 可以成为独立检索目标。
8. Bot 和当前发送者不会重复成为 referenced target。
9. 私聊不能检索 group/person_group。
10. query 不能改变 subject_user_id 和 group_id。

### FTS

11. 中文三字以上词组能够命中。
12. 英文词和数字能够命中。
13. memory_key 精确匹配优先。
14. category 精确匹配优先。
15. 两字短查询只在硬过滤范围内 fallback。
16. 标点、引号、星号、括号等不能注入 FTS 语法。
17. 空查询在 relevant 模式不执行全表搜索。
18. 无命中时不返回无关高重要度事实。
19. overview 模式可以返回有界事实。
20. explicit preference 常驻集合有界并去重。

### 排序

21. 精确匹配优于模糊匹配。
22. 明确词法命中优于完全不相关的高 importance 事实。
23. 词法相同后按 importance、confidence、updated_at 排序。
24. 完全相同条件下 fact_id 保证稳定顺序。
25. 每个 target 独立 limit。
26. 一个 target 的大量命中不能挤掉另一个 target。

---

## 二十七、上下文测试

1. 普通聊天只注入与当前问题相关的 current_person facts。
2. 当前群事实只进入 current_group block。
3. 当前人物群内事实只进入 current_person_in_group block。
4. mentioned person 使用独立 referenced block。
5. replied person 使用独立 referenced block。
6. 没有 mention/reply 时，其他人物事实不进入上下文。
7. ContextBudgeter 删除的 fact 不更新 last_used_at。
8. 最终注入的 fact 更新 last_used_at。
9. 同一 fact 在多个贡献中只更新一次。
10. 不同 block 的 fact 不发生归属混淆。
11. overview 请求不返回其他人物。
12. query-driven retrieval 关闭时仍保持身份隔离。

---

## 二十八、接口测试

1. Core `get_person_memories(query=...)` 使用同一 Retriever。
2. Core `get_group_memories(query=...)` 使用同一 Retriever。
3. Core Tool 不能提交 FTS 语法绕过。
4. 管理员 memory search 使用同一 Retriever。
5. Plugin `MemoryFacade.search()` 使用同一 Retriever。
6. Plugin 不能通过 search 跨用户或跨群。
7. list 接口仍可确定性列出 active facts。
8. evidence 接口保持正常。
9. Memory Worker 写入后可以立即检索。
10. Fact supersede 后只返回新版本。

---

## 二十九、性能与 Token 测试

构造至少：

- 100 个用户；
- 每个用户 100 条 person facts；
- 多个群；
- person_group facts；
- 相同关键词跨人物重复。

验证：

1. 检索不会返回其他人物。
2. SQL 不加载全库 facts 到 Python。
3. Context 中事实字符数低于 `3.0.0a1` 的固定列表方式。
4. 普通检索不调用任何 LLM。
5. 多 target 检索延迟有界。
6. 查询日志没有事实正文。
7. FTS health 不扫描或输出事实正文。

性能测试只要求稳定回归，不写未经测量的绝对毫秒承诺。

---

## 三十、实施顺序

1. 记录当前基线。
2. 阅读路线和 Memory V2 实现。
3. 增加查询和目标领域模型。
4. 增加 `MemoryTargetResolver`。
5. 增加 `MemoryQueryBuilder`。
6. 创建 FTS5 Alembic 迁移。
7. 实现 `SQLiteMemoryFTSIndex`。
8. 实现短查询安全 fallback。
9. 实现 `MemoryRanker`。
10. 实现 `MemoryRetriever`。
11. 实现 `MemoryContextService`。
12. 更新 ContextAssembler。
13. 实现最终选中 fact 的 `mark_used`。
14. 更新 Core Tools。
15. 更新管理员命令。
16. 更新 Plugin MemoryFacade。
17. 增加指标和 index health/rebuild。
18. 更新配置。
19. 完成迁移、身份、检索、上下文和性能测试。
20. 更新文档和版本。
21. 运行完整质量检查。
22. 提交代码。

---

## 三十一、版本和文档

将版本提升为：

`3.0.0a2`

更新：

- `pyproject.toml`
- `src/qq_ai_bot/__init__.py`
- `CHANGELOG.md`
- `README.md`
- `.env.example`
- `docs/architecture/memory-v2-roadmap.md`
- Memory V2 架构文档
- 配置文档
- 管理命令文档
- Plugin API MemoryFacade 文档

路线文档中标记：

```text
阶段一：已完成
阶段二：已完成
阶段三：未开始
```

文档必须明确：

1. 词法检索不调用 LLM。
2. FTS 是派生索引。
3. 检索先硬过滤人物和群。
4. 无匹配不会加载全部事实。
5. 明确 mention/reply 才能加载其他人物事实。
6. Embedding 尚未实现。
7. 历史重建尚未实现。

---

## 三十二、质量检查

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
uv run pytest -q tests/unit -k memory
uv run pytest -q tests/integration -k memory
```

检查没有新增向量依赖：

```bash
grep -R "bge\|embedding\|qdrant\|pgvector\|sqlite_vec\|sqlite-vec" pyproject.toml uv.lock src
```

路线文档中的未来规划文本可以出现 `Embedding`，业务实现和依赖中不得出现。

---

## 三十三、完成报告

完成后输出：

1. 开始 HEAD commit。
2. 最终 commit。
3. 当前 Alembic head。
4. 新建和修改的文件。
5. FTS5 表和触发器结构。
6. FTS 回填方式。
7. MemoryQuery 模型。
8. MemoryTargetResolver 的目标来源。
9. 硬过滤发生在哪个 SQL 边界。
10. 安全 FTS 查询构造方式。
11. 短查询 fallback 方式。
12. MemoryRanker 的确定性排序规则。
13. relevant 与 overview 的差异。
14. 常驻显式偏好规则。
15. ContextAssembler 的新调用链。
16. referenced person 的加载条件。
17. last_used_at 的更新时机。
18. Core/Admin/Plugin 接入方式。
19. FTS health 和 rebuild 方式。
20. 新增配置。
21. 迁移测试结果。
22. 身份隔离测试结果。
23. FTS 与排序测试结果。
24. 上下文测试结果。
25. 性能回归结果。
26. 全部测试数量和结果。
27. Ruff 结果。
28. mypy 结果。
29. Alembic 结果。
30. Docker 结果。
31. 尚未完成事项。
32. 是否加入任何 Embedding 或向量依赖。
33. 是否存在先全库检索再按人物过滤的路径。
34. 是否存在 FTS 无命中后加载全部事实的路径。
35. 是否默认加载最近发言群友的长期事实。
36. 是否调用额外 LLM 完成普通记忆检索。

第 32 项预期：

```text
没有。
```

第 33 项预期：

```text
不存在。人物和群硬过滤在词法候选 SQL 中完成。
```

第 34 项预期：

```text
不存在。
```

第 35 项预期：

```text
没有。
```

第 36 项预期：

```text
没有。普通记忆检索完全由后端确定性完成。
```
