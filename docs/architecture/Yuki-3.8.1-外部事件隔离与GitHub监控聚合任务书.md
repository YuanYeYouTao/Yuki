# Yuki 3.8.1 泛用外部事件隔离与 GitHub Monitor 安全合批任务书

> **历史合同提示：** 本任务书的 external event 隔离、GitHub WAL、幂等和合批边界继续有效；
> 其中“独立后台 Agent turn”生成层已被后续任务书取代。当前实现不得使用专用短上下文或
> tool-free/read-only Agent，而应只用临时 current user 尾部唤醒完整正常 Main Agent，并保存主动
> 回复因果。见
> [Yuki 插件唤醒 Main Agent 与主动回复因果修复任务书](Yuki-插件唤醒Main-Agent与主动回复因果修复任务书.md)。

## 0. 文档信息

- **任务性质**：在冻结的 3.8.0 canonical 运行时上修补外部事件投影、后台 freshness、Host 通知幂等，以及 GitHub Monitor 插件内的连续性队列与安全合批。
- **目标版本**：Yuki 3.8.1
- **基线版本**：Yuki 3.8.0，`main@d2331b9`，Alembic head `0049`，Plugin API `2.0`
- **审查状态**：2026-08-26 已按基线代码完成对抗审计并冻结；本文是实施合同，不是讨论稿。
- **目标仓库**：`YuanYeYouTao/Yuki-QQbot`
- **交付方式**：单一 PR，内部按 C0–C6 分提交；禁止覆盖已不可变的 3.8.0 tag / 镜像 / Release 资产。
- **数据策略**：不新增、不修改数据库 schema；必须在停写副本上 recount 现有 `uncovered_character_count`。账本行不可变。
- **镜像策略**：只在本机构建 `linux/amd64` 并 `docker save`；生产只 `docker load` 后 bot-only 替换。SnowLuma / NapCat 不变。

文档优先级：在外部事件投影、Rollup 计量、后台 turn freshness、Host `publish()` 幂等和 GitHub Monitor 队列范围内，本文取代既有 GitHub 监控任务书与 `conversation-rollup.md` 的冲突条款。历史任务书、发布说明和 3.8.0 升级指南作为证据保留，C0 不得改写它们。实现完成后由 C6 把稳定合同回写到版本化文档。

一句话合同：

> 账本继续完整记录插件外部事件；主会话 Prompt 历史只投影真正的 `event_kind=message`；外部来源只以不可信 user 载体和覆盖后必填摘要进入模型；GitHub 业务只留在插件的 CAS 队列里。

---

## 1. 缺陷与目标

当前 Plugin API 2.0 已把外部通知持久化为不可变 `external_event`（`direction=external`，`author_kind=system`）。损坏层是投影、计量、消费者和幂等，不是账本种类，也不是 GitHub。

必须关闭的行为：

1. 插件摘要被投影为会话中段 `role=system`，污染普通轮次前缀和指令权威。
2. 同一外部事件同时进入普通历史和 `recent_external_events`，且 digest 可能在 coverage 确定前计算。
3. TRUSTED 策略携带插件身份字段；payload 进入 Prompt。
4. 外部行占用 protected tail / 前台 fit 的事件槽，GitHub 风暴会压缩近期人类消息。
5. 外部 append 以非零 Prompt 字符唤醒已有 Rollup job。
6. Automation、MessageFacade、Memory rebuild 邻居和 Agent 历史 JSON 把源行当成普通发言。
7. 后台 job 在 `/ai new`、人类插话或 coverage 推进后仍可能调用模型或投递 `agent_reply`。
8. `publish()` 在已存在事件上补写缺失 Outbox / job；媒体 `part_key` 含随机 handle，重试会双发。
9. GitHub 插件用业务对象 ID 做幂等键、字符串排序、截新弃旧、cursor 缺口时间跳跃，并使用 last-write-wins `storage.set`。

完成后必须成立：

- 核心对一切 Plugin API 外部事件通用，零 GitHub 事件类型。
- 普通主历史前缀在无关外部事件之间保持稳定。
- 可见 QQ 投递仍是 Yuki outbound 历史。
- 静默或失败事件不造成账本空洞，也不伪造 outbound。
- GitHub 突发只在插件内合批；Host 仍接收普通 `PublishNotificationRequest`。

---

## 2. 强制原则

### 2.1 必须做到

1. 目标版本 3.8.1；一个 PR；本机 amd64 镜像；不覆盖不可变 3.8.0。
2. 持久化分类保持 `event_kind=external_event`、`direction=external`、`author_kind=system`。
3. Plugin API 2.0 `PublishNotificationRequest` / `publish()` 签名不变。
4. 核心不得出现仓库、PR、Issue、Push、GitHub API 或 GitHub 卡片逻辑。
5. Provider 当前外部触发恰好一次，且为不可信 `role=user` 载体。
6. 普通主历史排除外部源行。
7. 最终 coverage 之后重算必填 `recent_external_events`：只含 Host 盖章的 source / plugin / type / time 与有界 summary，永不含 payload。
8. TRUSTED 外部策略只含 Host 恒定文本。
9. Rollup coverage 与 `last_event_id` 对全部 keeper ID 连续。
10. 后台 freshness 使用三次短事务围栏，禁止把模型调用包进数据库事务。
11. `agent_reply` 发送占用已有 conversation effect-gate，并与 `/ai new` 线性化。
12. 直接文字/媒体通知与 generation 无关；只有平台回执才追加 outbound。
13. Host 已存在事件路径只读：精确匹配去重，任何不一致冲突，永不补写子对象。
14. 不新增 schema；升级前停写 recount。
15. 诊断、日志、metrics 不含正文、token、payload 或高基数身份。

### 2.2 明确禁止

1. 禁止把外部内容写入 provider `system` / `developer` / instructions。
2. 禁止把 payload、原始 GitHub body 或未裁剪评论写入 Prompt、TRUSTED 策略或 Rollup 摘要源。
3. 禁止删除、改写或回放突变账本行。
4. 禁止为 GitHub 在核心增加事件种类、开关或特例分支。
5. 禁止扩展 Plugin SDK `CurrentMessage` 来“暴露”外部行；MessageFacade 只返回 `event_kind=message`。
6. 禁止外部事件创建 MemoryJob、RelationshipJob 或当成人称证据。
7. 禁止在已存在通知上补 text / media / `ask_agent` job。
8. 禁止用 `storage.set` 作为 GitHub 队列权威写入。
9. 禁止静默丢事件、时间跳跃填 cursor 缺口、或截取最新 N 条而放弃更老事件。
10. 禁止第一版聚合卡片；禁止把 Push / Release / PR / Issue / 评论 / Review 合批。
11. 禁止在生产机构建镜像、`uv sync` 或覆盖 3.8.0 镜像。
12. 禁止在外部副作用发生后盲目恢复快照并自动重试。

---

## 3. 非目标

- 不改 Plugin API 版本，不改 `PublishNotificationRequest` 字段。
- 不新增 Alembic revision，不改 `0048`/`0049`。
- 不实现 GitHub Webhook、GitHub App、写操作或跨仓库合批。
- 不把外部事件自动写入 Memory V2。
- 不把 Commit 作者映射为 Person。
- 不承诺 Provider 前缀缓存命中率。前缀稳定后命中改善是预期，Provider 侧缓存策略仍是假说；诊断必须无正文。
- 不在 C0–C5 改版本号、README、升级指南或 Release 资产；这些只属于 C6。
- 不把聚合卡片、Push 合批或可配置任意事件类型合批作为本版本范围。

---

## 4. 双投影与 Prompt 合同

### 4.1 账本不变

唯一持久事实仍是 `chat_events` keeper 行。外部事件继续：

```text
event_kind = external_event
direction = external
author_kind = system
origin = plugin_background
```

唯一键仍为 `(source_plugin_id, external_event_key, scope_type, external_target_id)`。重复 `publish()` 不得插入第二行。`/ai new` 只推进 `starts_after_event_id` 并丢弃该 Conversation 的 Rollup 投影；不得把 generation 编进唯一键。旧行落在新 generation 边界之后即不可再摄入。

### 4.2 三条互斥出现通道

对任意外部事件 ID，同一模型请求中最多走其中一条：

| 通道 | 何时出现 | Provider 角色 | 内容 |
|---|---|---|---|
| 当前触发载体 | 本轮 `ask_agent` 的 source event | 不可信 `user`，恰好一次 | Host 信封 + summary，无 payload |
| 普通主历史 | 永不 | 不投影 | 源行不是 speaker |
| 覆盖后 digest | 最终 coverage 之后、当前 ID 排除 | 不可信 CONTEXT | 见 4.4 |

可见插件通知（已有平台回执的 outbound `message`）仍是普通历史。源行本身不是 speaker。

### 4.3 当前触发

`ask_agent=true` 的当前外部事件：

- 不进入 `main_agent_history`；
- 不进入 `recent_external_events`；
- 以不可信 `role=user` 出现恰好一次；
- 不得再用 `event_kind=external_event → role=system`。

`ask_agent=false` 不启动后台模型，但仍写入账本，并在之后普通轮次的 digest 中按 4.4 可见。

### 4.4 必填 recent digest

在最终 rollup / coverage / protected tail 选择完成之后重算，禁止使用压缩前 snapshot。

每条只含 Host 盖章字段：

```text
source
source_plugin_id
event_type
occurred_at
summary
content_trust = external_untrusted
```

规则：

- 永不包含 `payload`。
- 排除当前触发 ID。
- 只含仍位于最终 raw tail、且 `id > effective coverage` 的外部行。
- 越过 watermark 的外部事件只作为有损 Rollup 摘要的一部分存活，不再保留结构化 digest。
- 单项 `summary` 上限为设置项，默认 800 字符。
- 总量上限为设置项，沿用 `plugin_external_event_context_characters` 或其等价后继，不得另做无界拼接。
- contribution `required=True`。编译器预算必须按该小对象分配；不得因旧的 4000 字 summary 或 payload 使 required 动态段超预算。
- 条数上限沿用 `plugin_external_event_context_limit`。

### 4.5 TRUSTED 策略

`runtime.external_event_policy` 只含 Host 恒定句子。禁止放入 `source_plugin_id`、`external_source`、`event_type` 或任何事件正文。身份与时间只出现在不可信载体或 digest。

---

## 5. Rollup 计量尺

账本尺子与 Prompt 尺子分离。禁止再用单一 `len(events) + prompt_accounting_characters(all keepers)` 同时驱动 coverage、protected tail、前台 fit 和压缩批次。

### 5.1 三套计量

| 计量 | 外部源行 | 普通 message | 用途 |
|---|---|---|---|
| `last_event_id` / `covered_through_event_id` / 连续 coverage | 计入 | 计入 | 全部 keeper ID 连续，无洞 |
| 原始耐久 `uncovered_event_count` | 计入全部未覆盖 keeper | 计入 | 诊断与原始耐久计数；**不是**前台 fit 谓词 |
| 触发 / 停止 | 只计 **eligible prefix** 内的 keeper | 同左 | 见 5.2 |
| protected tail / 前台 fit / 可见条数 / Prompt 字符 | 不计（投影字符为 0） | 只计实际 `event_kind=message` 投影 | 前台窗口与 cache 前缀 |
| 压缩批次 | 计入 raw 条数上限；字符用 `rollup_source_projection` | 同左 | 送给压缩模型的真实源成本 |
| `RollupCandidate.projection_characters` | 源成本 | 源成本 | 不得用前台 history 成本冒充 |

### 5.2 protected tail 与触发边界

```text
protected_tail_start
  = 包含最近 N 条 Prompt 可见 event_kind=message 投影的连续 raw-ID 后缀的起始位置
```

- N 为现有 raw tail 事件设置。
- 该后缀内交错的外部行随后缀受保护，不单独占用“可见消息”名额。
- 外部风暴不得吞掉受保护的消息后缀。

高低水位触发与停止 **只** 作用于该边界之前的 eligible raw keeper 前缀：

- 前缀内全部 keeper（含外部行）都可计入事件耐久；
- 前缀内 Prompt 字符只计实际 message 投影（外部为 0）；
- 存储的 `uncovered_event_count` 仍可以是全部未覆盖 keeper，但前台 fit 必须改用计算出的可见消息条数和字符，不得再拿该存储值与 `trigger_events` / admit 比较。

### 5.3 外部 append

外部行成功 append 时：

- Prompt 字符增量 = 0；
- 不得 `force_existing=True` 唤醒已有 job；
- 不得因为单条外部行把 coverage 或 fingerprint 算成“已变化的人类前缀”。

新 job 仍可在 eligible prefix 越过事件高水位时出现；这是耐久策略，不是前台字符策略。

### 5.4 压缩源

`rollup_source_projection` 保持现有不可信 user 摘要信封，只使用 summary，禁止 payload。批次：

- 仍有 raw 事件条数上限；
- 字符切割使用真实 `rollup_source_projection` 成本，而不是前台 history 成本；
- `candidate.projection_characters` 必须等于该源成本。

禁止用“外部 history 成本为 0”绕过 `batch_max_characters` 向压缩模型送入超大源。

### 5.5 离线 recount

现有生产 `uncovered_character_count` 把外部行算进了前台尺子。C1 必须提供停写 recount：

- 复用并修正 `recount_canonical_uncovered`，使字符改为 message 投影尺子；
- 事件计数仍为未覆盖 keeper 总数；
- 在 rehearsal 与 live 升级开始前，对停写副本执行；
- 未完成 recount 不得恢复流量。

不需要 schema migration。数据 recount 是硬门。

---

## 6. 后台 freshness 与效果门

### 6.1 三次短事务，而不是跨模型长事务

后台 `ask_agent` job 的权威围栏：

1. **Claim 事务**：校验 source 仍属于当前 generation（`source_event.id > starts_after_event_id`）、source 之后无人类 inbound、source 高于当前有效 semantic / 有效 overlay coverage；返回 `generation + attempt` token。失败则 terminal，零请求零回复。
2. **模型调用前立刻再验**：同样条件，短事务。失败则 terminal，零请求零回复。
3. **Finish CAS**：要求 job 仍为 processing、attempt 未变、generation 未变、freshness 未变。失败则不得插入 `agent_reply`。

禁止把模型调用放进数据库事务。Claim 不持有跨模型 lease 以外的写锁。

有效 coverage 必须复用最终 Prompt / compact 的同一 id 水位，不得维护第二套账本。source `id <= effective coverage` → `superseded_covered`，零请求零回复。

### 6.2 `/ai new` 与过期 job

| 时机 | Provider 费用 | 回复 |
|---|---|---|
| 模型调用前 reset / stale / covered / 人类插话 | 必须为零请求 | 必须为零回复 |
| 模型调用中 reset | 允许已发生的 Provider 费用 | 必须为零回复 |
| 延迟过期 job | 零请求 | 零回复，terminal supersede |

同 generation 中 source 之后出现人类 inbound，不得用过期 snapshot 继续生成，也不得“跟着最新 admission 重组后再说一次”。该 job terminal。

### 6.3 agent_reply 与直接通知

`agent_reply`：

- 占用已有 conversation effect-gate 锁；
- 在锁内再次校验 generation 与“source 之后无人类 inbound”；
- 在锁内发送；
- 与 `/ai new` 线性化。失去 fence 的结果不得提交。

直接 text / media Outbox：

- 与 generation 无关，是通知而非会话回复；
- 只有平台回执才把 outbound `message` 写入账本；
- 静默、失败、uncertain 不得伪造 outbound。

### 6.4 消费者隔离

| 消费者 | 合同 |
|---|---|
| Automation | `role=system` 只含固定系统策略（可含可信时间）；近期资料进入不可信 user。不得把 `direction=external` 标成 user 发言。 |
| Plugin MessageFacade `get_recent` / `search_history` | SQL 在 cursor / order / limit 之前过滤 `event_kind=message`。不扩展 SDK。 |
| Memory live 与 rebuild 邻居 | 同样在 SQL 过滤 `event_kind=message` 后再取邻域。 |
| Agent 历史 JSON | 标注 `event_kind` / `author_kind` / `source` / `content_trust`。普通消息数组不含外部源行。 |

---

## 7. Host 通知幂等

不改 schema。权威算法：

### 7.1 规范请求清单

对每次 `publish()` 构造不可变 manifest，至少包含：

- 规范化 UTC `occurred_at`；
- canonical JSON（summary + payload）；
- text；
- 有序媒体 `(index, sha256)`；
- `ask_agent`；
- `agent_intent`；
- target。

并比较 `external_source` 与 `external_event_type`。

### 7.2 已存在事件路径只读

查到同一唯一键：

- manifest 逐字段精确匹配 → `deduplicated`，不写子对象；
- 任一项不一致 → `receipt_conflict`，失败关闭；
- **禁止**为缺失的 text / media / job 补行。静默之后再 `ask_agent=true` 必须冲突，不得升级。

### 7.3 媒体身份

新 part key：`media:{index}:{sha256}`，SHA 来自已存 `plugin_media_artifacts.sha256`。

Cutover 双读：若只存在旧 `media:{index}:{handle_id}`，且该 handle 的存储 SHA 与当前字节相同，视为同一 part；否则冲突。禁止因新 handle 再插入第二个 media part。

---

## 8. GitHub Monitor C4：单例连续性队列

全部 GitHub 逻辑只存在于 `plugins/github-monitor/**`。核心仍然只看到普通 `publish()`。

### 8.1 单一 CAS 值

每个仓库一个 `QueueState`，一次 `storage.get` 的原始 JSON 就是下一次 `compare_and_set` 的 `expected`。禁止把模型 dump 再编码后当作 expected。禁止用 `storage.set` 写权威队列。

该值拥有：

- `accepted_cursor` 与 `committed_cursor`；
- pending FIFO；
- 最多一个不可变 inflight 单元；
- 每目标 `prepared` / `attempting` / `completed` / `skipped`；
- 永久 key 过渡边界。

### 8.2 协调锁

每个仓库一个 coordinator lock（进程内 `asyncio.Lock` 加 CAS）。覆盖：

- 后台 poll；
- `/github sync` / `add` / `remove` / `pause` / `resume`。

CAS 防丢失更新；锁负责串行化网络、渲染和 `publish()`。`/sync` 在 pending / inflight 非空时必须先 drain 或拒绝，禁止 `delete_repository_state()` 丢掉队列。

### 8.3 WAL 顺序

严格顺序：

```text
CAS accept + 推进 accepted_cursor
  → seal 当前 inflight
  → publish 尚未完成的目标
  → CAS 记录 completion
  → dequeue + 推进 committed_cursor
```

崩溃后从 inflight / pending 恢复，不得重做已 accepted 的 ingest，也不得在 completion 前丢弃成员。

### 8.4 身份、顺序、缺口

- 新单例 Host key：`github:<repo>:event:<id>`。
- `id <=` 永久过渡边界的已发布事件继续使用 legacy key，禁止改写已进入 Host 的旧键。
- 聚合命名空间与单例命名空间分离。
- `monitor_enabled` 必须持久化 activation id 与时间，不得每次 poll 生成新键。
- 消费最老优先，有界分页；禁止截取最新 N 条。
- 页面重叠用身份 + payload 核对；同 ID 不同 payload → 失败关闭。
- 非数字 ID、冲突、cursor 不在重叠窗口 → 失败关闭，doctor 可见，只允许显式 rebaseline / replay，禁止时间跳跃。

C4 只 **创建** 单例单元，但必须能 **排空** 多成员单元（供 C5 回滚）。Legacy `RepositoryState` 在 drain 后导入并只镜像，不再作为权威写入。

---

## 9. GitHub Monitor C5：安全合批规划器

C5 只改变封批规划。C4 队列、WAL、CAS、锁不变。Host 仍按单元 `publish()`；合批后仍是一次 Host 通知，而不是核心可识别的 GitHub 类型。

### 9.1 允许合批的相邻规则

只合并 pending FIFO 中 **相邻**、未越过不兼容事件的成员。禁止重排。

| 类型 | 额外相等字段 | 第一版 |
|---|---|---|
| CreateEvent / DeleteEvent | 同 actor、同 `ref_type`、同 repo、同 type、同 target 快照 | 可合批 |
| WatchEvent / ForkEvent | 同 repo、同 type、同 target 快照 | 可合批 |
| PushEvent | — | **故意单例** |
| Release / PR / Issues / comments / reviews / Discussion / `monitor_enabled` / `/github test` | — | 单例 |

第一版禁用聚合卡片。文本与 payload 必须由成员列表确定性生成；payload 保留全部 source event ID，不得保存原始 body。

批次 Host key：`version + repo + hash(有序单例 key)`。独立于 API 页、cursor 和处理时间。

### 9.2 目标策略冻结

- 首次 attempt **之前**：当前配置可以把某目标标为 `skipped`（移除目标或关闭 `ask_agent` / `send_text` / `send_card`）。
- 进入 `attempting` 之后：完整 prepared 请求字节、请求哈希、媒体 handle + SHA 不可变。配置只能 pause / revoke，不得改同一请求。
- 未知结果只允许字节等价重试。
- 单例媒体在首次 attempt 前渲染一次，TTL 覆盖重试窗口；过期失败关闭并由 doctor 暴露。随机 handle 不得在相同幂等键下重新生成。

### 9.3 运行时开关与回滚

- `coalesce=false`：未 seal 的事件按单例排空；已 seal 单元身份不变，继续完成。
- `pause`：同时停止 ingest 与 drain。
- 代码从 C5 回退到 C4：C4 必须继续排空已存在的多成员单元，不得因 cursor 已推进而失联。
- 完整旧版本回滚只能在队列排空后进行；紧急硬回退使用部署前同一时点快照，并遵守第 11 节。

---

## 10. 提交序列

一个 PR，七个提交。后一提交不得提前混入前一提交的职责。每个提交必须带行为测试与回滚说明。

### C0 — `docs(architecture): freeze external event isolation contract`

- 仅新增本任务书。
- 回滚：删除该文件并还原本提交。

### C1 — 泛用 Prompt / Rollup 投影与 recount

**允许改动面（预期，非授权扩大范围）：** `event_prompt.py`、`context_assembler.py`、`prompt_composer.py`、`conversation/rollup/*`、`scoped_event_uow.py` 中外部 append 计量、recount、对应测试。

**必须实现第 4–5 节。**

**行为测试至少覆盖：**

1. 外部行不再投影为历史 `role=system`；普通历史 JSON / Chat Completions / Responses 输入都不含该源行。
2. 当前 `ask_agent` 触发以不可信 `user` 出现恰好一次，且不在 digest 中重复。
3. digest 在最终 coverage 后重算；无 payload；单项 summary ≤ 800；总量受设置约束；required 贡献在预算内。
4. TRUSTED 策略为恒定 Host 文本，不含插件身份字段。
5. mixed 序列中 coverage 连续跨过外部 ID；checkpoint 与 raw tail 无洞。
6. protected tail 按最近 N 条 message 投影计算；后缀内外部行随行受保护。
7. GitHub 式外部风暴留在 eligible prefix 时可以触发压缩，但不得把受保护消息后缀吃进 batch。
8. 外部 append Prompt 字符 = 0，且不 `force_existing` 唤醒已有 job。
9. 压缩批次按 raw 条数上限 + 真实 `rollup_source_projection` 字符切割；`projection_characters` 等于源成本。
10. recount 把存量 `uncovered_character_count` 修成 message 尺子；append 后与 recount 一致。
11. 前台 fit 使用可见消息条数/字符，不使用存储 `uncovered_event_count` 作为 admit 谓词。
12. 前缀诊断 hash 无正文；同 snapshot 重试的消息序列稳定。

**回滚：** 还原 C1。未 recount 的数据库不得搭配 C1 镜像开写。C1 镜像是安全回退下限的候选（见第 11 节）。

### C2 — 消费者隔离与后台 / 效果 freshness

**必须实现第 6 节。**

**行为测试至少覆盖：**

1. Claim 返回 generation + attempt token；source 不在 generation、source 后有人类 inbound、source 已被 coverage → 不领取或 terminal，零请求。
2. 模型前短事务再验失败 → 零请求零回复。
3. Finish CAS 在 processing / attempt / generation / freshness 任一变化时拒绝写入回复。
4. `/ai new` 发生在模型前：零请求零回复。
5. `/ai new` 发生在模型中：允许费用，零回复，不得发送。
6. covered / stale job 为 terminal `superseded_covered`。
7. `agent_reply` 与 `/ai new` 争用同一 effect-gate；reset 胜出则不发送。
8. 直接 text/media 在 `/ai new` 之后仍可按通知投递；无回执则无 outbound。
9. Automation system/data 分离；外部行不出现在 system 历史里。
10. MessageFacade 在 SQL limit 前过滤 `event_kind=message`；外部行不能靠插在最近消息之间挤进窗口。
11. Memory live 与 rebuild 邻居同样先过滤。
12. Agent 历史 JSON 含 kind / author_kind / source / content_trust。

**回滚：** 还原到 C1。C1/C2 镜像必须保留为生产安全回退下限；3.8.0 镜像会把已有外部行再次投影为 `system`，不得作为本任务完成后的安全 floor。

### C3 — Host 发布幂等

**必须实现第 7 节。**

**行为测试至少覆盖：**

1. 完全相同的第二请求 → `deduplicated`，零新 Outbox，零新 job。
2. `external_source` 或 type 不一致 → conflict。
3. summary / payload / time / text / ask_agent / intent / 媒体 SHA 不一致 → conflict。
4. 已存在事件缺失 text 或 job 时不得补写。
5. 新 media key 为 index+SHA；旧 handle key 在 SHA 相同时双读成功。
6. 同一逻辑图、不同 handle、相同 SHA → 不双发。
7. SHA 不同 → conflict。
8. 无 schema 变更；唯一键不变。

**回滚：** 还原到 C2。已按新 part key 写入的 Outbox 依赖双读兼容；回退 C3 前须确认旧代码仍能双读或队列已排空。

### C4 — GitHub 单例连续性队列

**只改 `plugins/github-monitor/**`。** 必须实现第 8 节。C4 只创建单例，但要能 drain 多成员单元。

**行为测试至少覆盖：**

1. 同 Issue 两条评论、同 PR 多次 Review 不再互相去重。
2. 页面重叠只接受一次；同 ID 不同 payload 失败关闭。
3. 数值 ID 顺序正确（`"99"` 在 `"100"` 前）。
4. 非数字 ID 失败关闭。
5. 最老优先；超过单轮上限时后续轮次继续，无静默丢失。
6. cursor 缺口：不发布、不推进、doctor 可见，需显式 rebaseline/replay。
7. 过渡边界之前用 legacy key；之后用 `github:<repo>:event:<id>`。
8. `monitor_enabled` 复用持久 activation id/time。
9. 原始 `get` JSON 作为 CAS expected；模型重序列化不得冒充 expected。
10. poll 与 `/github sync|add|remove|pause|resume` 互斥。
11. WAL 崩溃点：accept 后 cursor 前、seal 后 publish 前、第一目标成功后、publish 成功但 completion 未写回、completion 后 dequeue 前。
12. Legacy state drain 后只镜像。

**回滚：** 排空队列后还原到 C3。禁止在 pending 非空时删除 KV。

### C5 — GitHub 安全合批

**只改插件。** 必须实现第 9 节。

**行为测试至少覆盖：**

1. 同 actor 连续删除多个分支 → 一个通知、每目标至多一次 Agent job。
2. 不同 actor / 不同 `ref_type` / 中间插入 Release 或 PR → 不跨越合并。
3. Watch / Fork 可计数合批；Push / Release / 评论 / Review 始终单例。
4. 批次 key 不因分页重叠或重启而改变。
5. 文本、payload、source ID 列表确定性；无聚合卡片；无原始 body。
6. 首次 attempt 前配置可将目标 skipped；attempting 后请求字节/哈希/媒体不可变。
7. 媒体 TTL 过期失败关闭并进入 doctor。
8. `coalesce=false`：未 seal 变单例，已 seal 不变。
9. `pause` 停止 ingest 与 drain。
10. 将代码回退到 C4 后，多成员 inflight 仍能排空。

**回滚：** `coalesce=false` 或部署 C4 插件并 drain。完整插件回退前必须排空。

### C6 — 版本 / 文档 / 质量 / 发布 / 部署门

- 产品版本、健康端点、CHANGELOG、Release notes、升级指南改为 3.8.1。
- 回写 `conversation-rollup.md` 与插件文档中被本任务书取代的条款。
- 不改 Alembic head。
- 完整 Quality：`ruff format --check`、`ruff check`、`mypy src`、全量 pytest、example plugin、`test_migration_0049`、Memory quality validate/run/compare、installer syntax。
- 收集测试数若超过当前 CI 预算（现为 800），必须在本提交把预算调整为记录值，不得删无关测试凑数。
- 本机 `docker buildx build --platform linux/amd64 --load`，记录 revision / version label，`docker save`；生产 `docker load` 后 `--no-deps --no-build --force-recreate bot`。
- 不得覆盖 3.8.0 镜像或 tag。

**回滚：** 见第 11 节。未发布前还原本提交即可。已替换 bot 后禁止盲目快照回灌。

---

## 11. 部署、预演与回滚

### 11.1 升级前

1. 记录当前 3.8.0 版本、镜像 digest、Compose、插件版本。
2. 停止 bot 与所有写库 worker；确认无其他进程持有 SQLite。
3. 同一时点复制 `data/qq_ai_bot.db`、`-wal`、`-shm`；WAL/SHM 缺失必须记入清单。
4. 同时快照 `.env`、`config/`、Compose、镜像 digest、插件 KV / pending、相关媒体资产。
5. 在 **停写副本** 上 rehearsal：加载 3.8.1 代码路径、执行 recount、跑 doctor / 队列导入，不接流量。
6. rehearsal 通过后再对 live 停写库执行同一 recount。
7. 只替换 bot 容器。SnowLuma 与 NapCat 不变。
8. 保留一套至少含 C1/C2 安全投影的回退镜像。3.8.0 镜像不是本任务后的安全 floor。

### 11.2 外部副作用之后

一旦出现平台发送、Agent 调用或 cursor 推进：

1. 禁止盲目把快照拷回并自动重试；
2. 先停 worker；
3. 对账 receipt、Outbox、pending、accepted/committed cursor；
4. 只重试未知结果且字节等价的请求；
5. 已确认成功的目标不得再发。

### 11.3 失败关闭医生口

至少暴露（均无正文）：coverage 漂移、recount 未完成、cursor gap、CAS 冲突、媒体过期、receipt_conflict、superseded_covered、pending 积压。

---

## 12. 质量门与测试预算

每个实现提交在合并前必须本地通过与其改动面相关的测试，C6 必须通过完整 Quality workflow。

缓存与前缀断言：

- 只比较无正文的 prefix / request-shape / snapshot hash；
- 这些 hash 不发给模型，不进高基数 label；
- 不得把“Provider 返回 cache hit”写成验收硬条件。

独立红队验收（C6 前，只读对照本任务书）：

- 恶意外部 summary 只出现在不可信载体，且每轮至多一次；
- 外部风暴不删除受保护人类尾部；
- `/ai new` 与 agent_reply 线性化；
- GitHub 合批不越过 PR/Release；
- Push 仍为单例；
- 无 schema 变更。

---

## 13. 验收清单

1. 核心零 GitHub 类型。
2. 持久化 `external_event` / `direction=external` / `author_kind=system` 与 Plugin API 2.0 不变。
3. 普通主历史不含外部源行。
4. 当前外部触发恰好一个不可信 user 载体。
5. 必填 digest 仅 Host 盖章字段 + 有界 summary，无 payload。
6. TRUSTED 策略为 Host 常量。
7. coverage 连续；recount 已在停写库完成。
8. protected tail 按 message 投影；触发只在 eligible prefix。
9. 外部 append 字符为 0 且不强行唤醒 job。
10. 三次 freshness 围栏成立；stale/covered terminal。
11. `agent_reply` 与 `/ai new` 经 effect-gate 线性化。
12. 直接通知与 generation 无关；outbound 仅回执。
13. MessageFacade / Memory SQL 先过滤 message。
14. Host 已存在路径只读精确匹配或冲突。
15. GitHub 单例队列 oldest-first、缺口失败关闭、CAS WAL。
16. 第一版无聚合卡片；Push 单例。
17. C5 回退 C4 仍能 drain。
18. 版本 3.8.1；3.8.0 镜像未被覆盖。
19. 本机 amd64 构建；生产 bot-only 替换。
20. 单元 / 集成 / ruff / mypy / Quality / 插件测试全部通过。

---

## 14. PR 要求

- 分支：`codex/generic-external-event-isolation`（或同等专用分支）。
- 单一 PR，提交顺序固定为 C0–C6。
- PR 正文必须列出：缺陷、双投影、三套 Rollup 尺子、三次 freshness 围栏、只读幂等、C4 WAL、C5 合批边界、recount、3.8.1 与 3.8.0 不可变关系。
- 必须声明：无 schema migration；必须数据 recount。
- 必须声明：聚合卡片首版禁用；Push 故意单例。
- 必须声明：Provider cache hit 不是硬验收，诊断无正文。
- 禁止把半成品合入 main。

---

## 15. 最终架构定义

> Yuki 3.8.1 把插件外部事件当作不可变账本源行而不是聊天发言：coverage 连续走过全部 keeper，Prompt 历史与前台窗口只看见 `message`，外部感知只通过覆盖后的有界 Host 摘要和单次不可信当前载体；GitHub Monitor 在插件私有 CAS 队列里完成连续性和安全合批，再以普通 Plugin API 2.0 通知进入 Host。
