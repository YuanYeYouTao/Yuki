# Memory V2 受控历史重建

Yuki 3.0.0rc1 可以从永久事件账本 `chat_events` 重新提取历史事实。重建是管理员主动发起的
离线工作流，不是启动任务：Alembic、应用启动、Bot 重启和 Worker 启动都不会创建或恢复 run。
`MEMORY_REBUILD_ENABLED=true` 只开放入口，仍需当前真实消息发送者属于 `SUPERUSERS` 并显式
执行 start 或 resume。

## 数据与状态

- `memory_rebuild_runs` 固定 selection、事件 ID 快照、提取契约指纹、扫描/提交 checkpoint 和
  run 状态，并累计持久化 extraction/consolidation 请求数、供应商返回的 token 数和延迟。
- `memory_rebuild_items` 保存每个真实事件的 source hash、提取状态、尝试次数和错误类别。
- `memory_rebuild_proposals` 只保存已通过后端主体与 Claim 校验的 canonical claim，不保存模型
  原始输出或完整上下文。
- `memory_jobs.status=done` 仍是一个事件已完成记忆处理的唯一 receipt；`processing_source` 标明
  live 或 rebuild。没有第二套事实表或 receipt 表。

状态流为：

```text
planned → extracting ⇄ extraction_paused → review
review → committing ⇄ commit_paused → completed
非 completed 状态可 cancel；不可恢复错误进入 failed，必须显式 retry
```

进程启动只把遗留的 `extracting` / `committing` 改成相应 paused，并记录 `process_restart`。
Worker 只轮询已由管理员置为执行态的 run。

## 安全处理链

plan 只规范化 selection、固定 `snapshot_max_event_id` 并用 SQL 统计，不调用任何模型，也不创建
item、proposal、事实、证据或向量。扫描使用 `(occurred_at, event_id)` keyset，不使用大表
OFFSET，也不会读入全部账本。

实时 Worker 与 Rebuild Worker 共用：

```text
MemoryEventExtractor
  → SubjectResolver
  → MemoryClaimValidator / MemoryTemporalResolver
  → MemoryClaimProcessor
  → CandidateResolver / RelationClassifier / ResolutionPolicy
  → MemoryFactService
```

extract 每个事件独立调用 `ModelTask.MEMORY_EXTRACTION`，只暂存 proposal。当前事件是唯一证据；
`evidence_quote` 必须逐字存在于当前事件且与 claim 语义锚定。同会话较早事件只按
`current_speaker / other_member / bot` 提供消歧，不得独立产生事实。回复方式、称呼、格式、
语音和表情等交互要求强制归为 preference。

`third_party_mode=trusted_metadata` 只接受持久 `yuki_context`、OneBot 数字 at 段，以及同一 Bot、
同一精确群的回复事件作者；不按正文姓名、昵称、FTS 或向量猜人。disabled 模式只提供 speaker
和 group。

## 审阅与提交

提取结束进入 review。proposal 默认 pending，可按 ID、scope、operation、kind、authority、
group、subject 和 confidence 范围批准或拒绝。批准的是 claim，不是预先计算的数据库 action；
仍有 pending 时 commit 会失败。

commit 按 `source occurred_at → event_id → claim_index` 串行执行，并重新：

1. 加载真实 source event 并验证 SHA-256 指纹；
2. 验证事件资格、Bot 身份和 live/rebuild receipt；
3. 运行 SubjectResolver、Claim Validator 与 Temporal Resolver；
4. 读取当前事实候选，必要时执行 consolidation；
5. 应用 HistoricalResolutionGuard 和当前 ResolutionPolicy；
6. 通过共享 MemoryFactService 写事实、证据、关系、状态事件与派生索引任务。

历史相同事实只合并证据，`last_confirmed_at` 取现值与历史事件时间的最大值。比当前 active 事实
更早的冲突或修正只能保存为 superseded 历史版本或 noop，不能反向 supersede、contest、
invalidate 或降低当前事实。过期 claim 默认 skipped；`stage_invalidated` 只创建带 expired 原因的
invalidated 历史事实。Rebuild 不调用 active 容量驱逐路径；容量已满时仅拒绝新 active 事实，
相同证据和非活跃历史版本仍可保存。

FTS 由现有触发器同步；active 新事实只排队现有 Embedding job。Embedding 故障不会回滚事实，
run completed 也不表示异步向量已经生成完毕。

## 管理命令

```text
/ai memory rebuild list
/ai memory rebuild plan <selection-json>
/ai memory rebuild start <run_id>
/ai memory rebuild status <run_id>
/ai memory rebuild pause <run_id>
/ai memory rebuild resume <run_id>
/ai memory rebuild cancel <run_id>
/ai memory rebuild review <run_id> [page]
/ai memory rebuild approve <run_id> <all|proposal-ids|filter-json>
/ai memory rebuild reject <run_id> <all|proposal-ids|filter-json>
/ai memory rebuild commit <run_id>
/ai memory rebuild retry <run_id>
/ai memory rebuild purge <run_id>
```

示例：只规划某个 QQ 的历史入站消息，不会调用模型：

```text
/ai memory rebuild plan {"sender_user_ids":["123456789"],"third_party_mode":"disabled"}
```

只有 completed/cancelled/failed run 可 purge。purge 只删除 staging；已提交事实、证据和事件
receipt 保留。cancel 只停止后续处理，不回滚已提交事实。

Tool Kernel 还提供十个 `admin_memory_rebuild_*` 工具，共用同一服务和真实事件权限绑定；工具不能
跳过 review。Plugin API 保持 2.0，未暴露 rebuild。

## 配置

所有配置见 `.env.example` 的 Memory rebuild 区。默认关闭，提取并发默认 2，提交始终串行。
`MEMORY_REBUILD_MAX_EVENTS_PER_RUN` 留空表示不增加部署上限；配置了上限时 selection 必须显式
提供不超过该值的 `maximum_events`，不会静默截断。

## 隐私、运维与排障

日志、health 和普通 status 不记录事件正文、claim 正文、selection JSON、QQ、群号、模型完整
输入输出或密钥。review 是超级管理员主动请求的有界审阅页。`/ai forgetme` 会由事件外键级联
清理 staging，删除以该人物为 subject 的 proposal，取消仅针对该人物的非终态 run，并从其余
selection 中删除精确 QQ；已提交人物事实继续按现有 forgetme 规则删除。

`status` 的 token 数仅累计供应商实际返回的 usage；供应商不返回时保持 0，不做字符数伪估算。
延迟以累计毫秒记录，Embedding 任务数按本 run 提交后实际关联的新任务统计。

常见状态：

- `extraction_fingerprint_changed`：模型路由、Prompt、Schema、主体解析或校验契约已变化；不要在
  同一 run 混用，创建新 plan。
- `process_restart`：这是预期的安全暂停，检查状态后显式 resume。
- `source_event_changed`：事件在审阅后被修改或兼容主体元数据变化，proposal 会跳过。
- `live_job_active` / `already_processed`：实时 Worker 正在处理或已经完成，Rebuild 不抢占。
- `rebuild_capacity_preserved`：当前 active 容量已满；调整容量、清理事实或拒绝 proposal 后重试。
- `historical_claim_expired`：selection 使用默认 skip，过期历史不会成为 active。

升级前必须停止写入并同步备份 DB/WAL/SHM。当前 canonical head 为 `0051`；`0049` 不提供
downgrade，生产回退只能恢复升级前同一时点的完整快照，不能依赖旧施工期 revision 的降级语义。
