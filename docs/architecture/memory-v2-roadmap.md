> **历史设计/非现行合同。** 本文保留当时的方案或实测口径，不用于当前授权、调度或使用率判断。
> 当前实现以 [Memory 架构](memory-v2.md)、[检索合同](memory-v2-retrieval.md)
> 和 [P1 治理任务书](Yuki-Memory-P1治理任务书.md) 为准；本文不在当前实施导航内。

# Yuki Memory V2：不可逆重构开发路线

> 状态：已决定  
> 目标版本：Yuki 3.0.0  
> 第一阶段目标版本：3.0.0a1；第二阶段目标版本：3.0.0a2；第三阶段目标版本：3.0.0b1；第四阶段目标版本：3.0.0b2；第五阶段目标版本：3.0.0rc1
> 基线：当前 `main` 的 Yuki 2.1.2  
> 核心决定：旧记忆表及其中数据不迁移、不兼容、不双写，关系型事实库重新建立；RAG 与 Embedding 作为后续派生检索层加入。

> 实施状态（2026-08-01）：第一阶段已在 `3.0.0a1` 完成，第二阶段查询驱动词法检索已在
> `3.0.0a2` 完成，第三阶段 Qwen Embedding 与混合 RAG 已在 `3.0.0b1` 完成，第四阶段
> 冲突、修正、证据聚合与生命周期已在 `3.0.0b2` 完成，第五阶段受控历史重建已在
> `3.0.0rc1` 完成。第六阶段质量评测、治理收敛与契约冻结已在 `3.0.0` 完成，Memory V2
> 路线关闭。

---

## 1. 决策摘要

Yuki 的旧记忆数据已经发生大面积人物归属错乱，本次不再修补旧表，也不尝试从旧记忆记录中推断真实归属。

本次重构采用以下原则：

1. 删除旧记忆表和旧记忆任务数据。
2. 保留不可变聊天事件账本，未来只能从 `chat_events` 重新提取记忆。
3. 第一阶段先解决“事实属于谁”，暂不加入 Embedding。
4. 关系数据库是记忆事实的唯一真相来源。
5. FTS、Embedding、向量索引都是可删除、可重建的派生索引。
6. 任何检索必须先按人物和群硬过滤，再进行关键词或向量相似度搜索。
7. 主模型不能决定任意 QQ 号、群号和证据消息 ID。
8. 当前人物、当前群和其他人物的记忆必须以独立实体块进入上下文。
9. 默认不加载“最近发言群友”的长期记忆。
10. 旧数据库只能通过完整备份恢复，不提供 Alembic downgrade。

---

## 2. 当前问题

当前记忆链路存在四类结构性问题：

### 2.1 模型可以填写人物 ID

旧 `MemoryOperation` 允许模型直接输出：

- `user_id`
- `group_id`
- `source_event_id`

后端没有将人物归属严格限制为当前真实事件中的主体，模型一旦把内容和 QQ 号错配，错误事实就会永久写入另一个人的记录。

### 2.2 全局任务批次混合不同人物和会话

旧任务队列按任务顺序取一批事件，可能把：

- 不同群；
- 不同私聊；
- 不同发送者；

放进同一次模型提取请求。

即使事实内容提取正确，人物 ID 也可能在批次中错位。

### 2.3 上下文同时平铺多个人的记忆

旧上下文会同时注入：

- 当前人物记忆；
- 当前群记忆；
- 当前人物群内记忆；
- 最近发言群友的人物记忆；
- 最近发言群友的群内记忆。

模型即使读取到正确数据，也可能把其他人物的事实归给当前用户。

### 2.4 检索只按重要度和时间

旧查询主要按：

- `importance DESC`
- `updated_at DESC`

取固定数量，没有根据当前问题筛选相关记忆。

Embedding 可以改善相关性，但不能修复错误人物归属，因此不能作为第一阶段。

---

## 3. 不可逆边界

### 3.1 必须删除

第一阶段迁移必须删除以下旧表及全部数据：

- `person_memories`
- `group_memories`
- `person_group_memories`
- `person_preferences`
- `memory_jobs`

同时删除：

- 旧 SQLAlchemy 模型；
- 旧 `MemoryRepository`；
- 旧 `MemoryWorker`；
- 旧记录模型和旧测试；
- 所有旧表兼容视图、双写路径和导入器。

### 3.2 必须保留

以下数据不属于旧记忆事实，必须保留：

- `people`
- `person_aliases`
- `groups`
- `memberships`
- `chat_events`
- 人物关系与关系事件
- 群和私聊设置
- 自动化任务
- 插件数据
- MCP 数据
- 表情、视觉和语音数据
- 管理员配置与审计

### 3.3 不允许迁移旧记忆

禁止：

- 将旧记忆复制到新表；
- 根据旧 `memory_key` 自动猜测主体；
- 把旧偏好转成新偏好；
- 启动后自动重放全部聊天历史；
- 保留旧表只读兼容层；
- 维持新旧双写。

未来重建只能读取 `chat_events`。

### 3.4 回退方式

Alembic `downgrade()` 必须明确失败。

唯一回退方式：

```text
停止 Yuki
→ 恢复升级前完整数据库备份
→ 恢复与该数据库匹配的旧代码
```

---

## 4. Memory V2 总体架构

```text
QQ 真实消息
    ↓
不可变 chat_events
    ↓
MemoryJob V2（每个主事件独立）
    ↓
SubjectResolver（后端确定主体）
    ↓
Flash 结构化提取
    ↓
MemoryClaimValidator
    ↓
MemoryFactService
    ├── memory_facts
    └── memory_evidence
    ↓
MemoryRetriever
    ├── 第一阶段：关系型硬过滤
    ├── 第二阶段：FTS/BM25
    └── 第三阶段：Embedding
    ↓
EntityContextProjector
    ↓
主聊天 Agent
```

核心不变量：

```text
先确定人物和群
→ 再检索内容
```

禁止：

```text
全库向量搜索
→ 再猜这条记忆属于谁
```

---

## 5. 新数据模型

## 5.1 `memory_facts`

一条记录表示一个具有明确主体和作用域的事实。

建议字段：

| 字段 | 含义 |
|---|---|
| `id` | 整数主键 |
| `scope_type` | `person` / `person_group` / `group` |
| `subject_user_id` | 人物主体；群事实为空 |
| `group_id` | 群作用域；跨群人物事实为空 |
| `kind` | `fact` / `preference` / `episode` |
| `memory_key` | 同一主体下的稳定事实键 |
| `category` | 用于治理和展示的分类 |
| `content` | 规范化事实文本 |
| `normalized_content` | 去空白、统一格式后的比较文本 |
| `importance` | 1～5 |
| `confidence` | 0～1 |
| `source_type` | `automatic` / `explicit` / `rebuild` |
| `status` | `active` / `superseded` / `invalidated` |
| `supersedes_id` | 新事实替代的旧事实 |
| `valid_from` | 可选有效时间 |
| `valid_until` | 可选失效时间 |
| `created_at` | 创建时间 |
| `updated_at` | 更新时间 |
| `last_used_at` | 最近被检索时间 |

作用域约束：

```text
person:
  subject_user_id 非空
  group_id 为空

person_group:
  subject_user_id 非空
  group_id 非空

group:
  subject_user_id 为空
  group_id 非空
```

同一作用域、主体、`kind` 和 `memory_key` 只允许一个 `active` 事实。

偏好不再使用单独表，统一保存为：

```text
kind = preference
```

## 5.2 `memory_evidence`

一条事实可以由多条真实消息支持。

建议字段：

| 字段 | 含义 |
|---|---|
| `id` | 主键 |
| `fact_id` | 对应事实 |
| `event_id` | 真实 `chat_events.id` |
| `source_speaker_user_id` | 证据消息发送者 |
| `relation` | `self_statement` / `explicit_command` / `correction` / `rebuild` |
| `excerpt` | 有界证据摘要 |
| `created_at` | 创建时间 |

第一阶段只提取当前发送者关于自己的事实，因此自动记忆通常使用：

```text
relation = self_statement
```

## 5.3 `memory_jobs`

旧表删除后，以 V2 契约重新创建。

建议字段：

| 字段 | 含义 |
|---|---|
| `id` | 主键 |
| `event_id` | 每条事件唯一 |
| `conversation_key` | 诊断和分组信息 |
| `status` | `pending` / `processing` / `done` / `failed` |
| `attempts` | 已尝试次数 |
| `next_attempt_at` | 下次执行时间 |
| `created_at` | 创建时间 |
| `updated_at` | 更新时间 |
| `error_category` | 有界错误分类 |

第一阶段每个主事件单独调用一次提取模型，不把不同事件合并成同一输出对象。

---

## 6. 身份安全提取契约

## 6.1 第一阶段允许的主体

第一阶段只允许：

- `speaker`：当前真实消息发送者；
- `group`：当前真实群本身。

暂不从普通群友消息中提取“关于第三个人”的人物事实。

例如：

```text
张三：我准备考研
```

可以写入张三。

```text
张三：李四准备考研
```

第一阶段不写入李四。

这种数据缺失可以在后续补充，人物错写不能接受。

## 6.2 模型输入

后端向提取模型提供：

- 主事件文本；
- 主事件发送者的匿名 `subject_ref`；
- 当前群 `subject_ref`；
- 同一会话的少量前文，仅用于理解；
- 可选作用域；
- 明确说明前文不能单独产生事实。

模型不接收可自由使用的 QQ 号或群号字段。

## 6.3 模型输出

模型只允许输出：

```text
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

模型不得输出：

```text
user_id
group_id
source_event_id
source_speaker_user_id
created_at
status
supersedes_id
```

这些字段全部由后端确定。

## 6.4 后端验证

`MemoryClaimValidator` 必须确认：

1. `subject_ref` 存在于当前请求；
2. `speaker` 只能映射到主事件真实发送者；
3. `group` 只能映射到主事件真实群；
4. 私聊不能产生 `group` 或 `person_group` 事实；
5. `group` 事实描述群整体，而不是单个人；
6. `source_event_id` 固定为主事件；
7. `source_speaker_user_id` 固定为主事件发送者；
8. 自动事实不能覆盖明确事实；
9. 空内容、空键和非法作用域拒绝写入。

---

## 7. 上下文身份隔离

Memory V2 的上下文必须按实体分块：

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

第一阶段禁止默认注入其他人物的长期事实。

聊天事件应直接保存发送者在发言时的 QQ 号、昵称和当前群名片；Prompt 历史从事件快照直接
渲染，不通过独立的人物元数据块补全身份，也不附带其他人的长期记忆。

后续只有在用户明确提及、回复或询问某人时，检索器才可加载该人物事实。

---

# 8. 开发阶段

## 阶段一：不可逆切换与身份安全核心

目标版本：`3.0.0a1`

状态：**已完成**

完成：

1. 编写本路线文档并纳入仓库。
2. 删除旧记忆表、旧模型、旧 Repository 和旧 Worker。
3. 创建 `memory_facts`、`memory_evidence` 和 V2 `memory_jobs`。
4. 新建 `qq_ai_bot.memory` 领域包。
5. 实现 `SubjectResolver`。
6. 实现只允许 `speaker` 和 `group` 的提取契约。
7. 每个事件独立提取，不混合不同人物和会话。
8. 实现事实写入、证据追加和基础替代规则。
9. 更新 `ContextAssembler`，只注入当前人物、当前人物群内事实和当前群事实。
10. 删除其他群友长期记忆的默认注入。
11. 更新核心记忆工具、管理员记忆工具和 Plugin MemoryFacade。
12. 不从旧表迁移任何数据。
13. 不自动重建历史。
14. 完成破坏性迁移和完整测试。

第一阶段不实现：

- FTS；
- Embedding；
- 向量数据库；
- 全历史重建；
- 第三方人物事实；
- 自动冲突推理；
- 语义重排。

验收结果：

```text
升级后旧记忆全部为空
新消息只能写入正确的当前发送者或当前群
主聊天默认看不到其他人物的长期事实
```

---

## 阶段二：查询驱动的词法检索

目标版本：`3.0.0a2`

状态：**已完成**

增加：

1. `MemoryQuery`。
2. `MemoryRetriever`。
3. SQLite FTS5 派生索引。
4. 关键词与精确实体检索。
5. 按当前问题选择事实，而不是固定加载全部高重要度事实。
6. 只有明确提及或回复某人时，才增加该人物硬过滤范围。
7. 记录 `last_used_at`。
8. 结果按独立实体块注入。
9. 增加检索解释信息和命中来源。

检索顺序：

```text
subject/scope 硬过滤
→ status=active
→ FTS 候选
→ importance/confidence/recency 排序
→ 上下文预算
```

---

## 阶段三：Embedding 与混合 RAG

目标版本：`3.0.0b1`

状态：**已在 3.0.0b1 完成**

增加：

1. `EmbeddingProvider`。
2. `VectorIndex`。
3. `memory_embeddings` 派生表或独立索引。
4. 使用 DashScope OpenAI-compatible Embedding API 的 `qwen3.7-text-embedding`；默认关闭，
   不增加本地大模型常驻资源。
5. 保存：
   - `fact_id`
   - `embedding_model`
   - `dimensions`
   - `content_hash`
   - `vector`
   - `updated_at`
6. 后台增量索引。
7. 模型版本变化后的完整重建。
8. 混合排序：
   - FTS/BM25；
   - 向量相似度；
   - importance；
   - confidence；
   - recency。
9. 可选轻量重排。

硬规则：

```text
人物和群过滤发生在向量搜索之前。
```

向量索引不能决定事实属于谁，也不能成为事实源。

---

## 阶段四：冲突、修正与生命周期

目标版本：`3.0.0b2`

状态：**已在 3.0.0b2 完成**

增加：

1. 事实状态机：
   - active
   - contested
   - superseded
   - invalidated
2. 同一主体同一 `memory_key` 的修正链。
3. 新事实与旧事实冲突检测。
4. 明确事实保护。
5. 有效期和时间事件。
6. 多证据聚合。
7. 置信度更新。
8. 自动合并同义事实。
9. 自动事实衰减和清理。
10. 第三方人物事实支持：
    - 明确提及；
    - 回复作者；
    - 证据关系；
    - 不与本人陈述混为同一可信等级。

实现边界：关系型事实仍是唯一真相来源；LLM 只分类有限候选之间的语义关系，最终状态由后端
确定性策略决定。第三方主体只能来自当前真实 mention/reply，且只进入当前群 person_group。
contested claim 默认不进入普通上下文；维护 Worker 不调用模型、不扫描历史、不物理删除事实。

---

## 阶段五：从事件账本受控重建

目标版本：`3.0.0rc1`

状态：**已在 3.0.0rc1 完成**

增加：

1. `memory_rebuild_runs`。
2. 从 `chat_events` 按时间和会话重建。
3. 支持：
   - dry-run；
   - 范围选择；
   - 断点续跑；
   - 暂停；
   - 取消；
   - 统计；
   - 审阅后提交。
4. 重建仍使用 V2 SubjectResolver。
5. 不读取任何旧记忆表。
6. 不自动在升级时启动。
7. 重建和实时 Worker 使用同一写入服务。
8. 避免同一事件重复生成证据。

管理命令示例：

```text
/ai memory rebuild plan
/ai memory rebuild start
/ai memory rebuild status
/ai memory rebuild pause
/ai memory rebuild resume
/ai memory rebuild cancel
```

---

## 阶段六：治理、评测与正式发布

目标版本：`3.0.0`

增加：

1. 记忆审计：
   - 查看事实；
   - 查看证据；
   - 失效；
   - 修正；
   - 合并；
   - 查看替代链。
2. 记忆检索解释。
3. 多人物群聊测试集。
4. 质量指标：
   - 人物归属准确率；
   - 跨人物污染率；
   - 相关记忆召回率；
   - 错误主体上下文注入率；
   - 冲突事实同时激活率。
5. Token 和延迟指标。
6. Embedding 索引健康检查。
7. 文档和升级指南。
8. 完成 3.0.0 正式发布。

---

## 9. 包结构规划

第一阶段建议建立：

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

后续阶段增加：

```text
src/qq_ai_bot/memory/
├── query.py
├── retrieval.py
├── fts.py
├── embedding.py
├── vector_index.py
├── ranking.py
├── conflict.py
├── consolidation.py
├── rebuild.py
├── audit.py
└── metrics.py
```

数据库模型仍放在统一 persistence 模型层，但领域行为必须进入 `qq_ai_bot.memory`，不能重新堆回一个大型 Repository 文件。

---

## 10. 版本与兼容策略

| 阶段 | 版本 |
|---|---|
| 旧系统最后版本 | 2.1.x |
| 第一阶段 | 3.0.0a1 |
| 词法检索 | 3.0.0a2 |
| 混合 RAG | 3.0.0b1 |
| 冲突治理 | 3.0.0b2 |
| 历史重建 | 3.0.0rc1 |
| 正式发布 | 3.0.0 |

第六阶段没有新增生产表，Alembic head 保持 `0024`。正式质量门禁使用版本化合成数据、Fake
Model 与 Fake Embedding；生产 audit/release-check 只读，hygiene 只能由运维人员显式 apply。

Plugin API 可以继续保持 `1.0`，但所有内置插件的 `yuki_requires` 必须在正式 3.0.0 发布前重新审查。

旧数据库与新代码不兼容。

---

## 11. 最终完成标准

Memory V2 完成时必须满足：

1. 模型不能写任意人物 ID。
2. 不同会话不会进入同一个提取输出。
3. 每条事实都有明确主体、作用域和证据。
4. 其他人物事实不会默认进入当前人物上下文。
5. 关系型数据库是事实源。
6. Embedding 索引可以随时删除并重建。
7. 检索先按人物和群过滤。
8. 冲突事实不会同时作为有效事实注入。
9. 历史重建只读取事件账本。
10. 可以解释每条记忆来自哪条真实消息。
11. 多人物测试集中的跨人物污染率达到可接受阈值。
12. 项目中不存在旧记忆表、旧 Worker 和旧兼容路径。
