# Memory V2 受控历史重建

Yuki 可以从永久事件账本 `chat_events` 重新提取历史事实。重建是管理员主动发起的
离线工作流，不是启动任务：Alembic、应用启动、Bot 重启和 Worker 启动都不会创建或恢复 run。
`MEMORY_REBUILD_ENABLED=true` 只开放入口，仍需当前真实消息发送者属于 `SUPERUSERS` 并显式
执行 start 或 resume。

## 数据与状态

- `memory_rebuild_runs` 固定 selection、事件 ID 快照、提取契约指纹、扫描/提交 checkpoint 和
  run 状态，并累计持久化 extraction 请求数、供应商返回的 token 数和延迟。
- `memory_rebuild_items` 保存每个真实事件的 source hash、提取状态、尝试次数和错误类别。
- `memory_rebuild_proposals` 只保存已通过后端主体与 Claim 校验的 canonical claim，不保存模型
  原始输出或完整上下文。
- `memory_jobs.status=done` 仍是一个事件已完成记忆处理的唯一 receipt；`processing_source` 标明
  live 或 rebuild。没有第二套事实表或 receipt 表。
- 回执收尾只处理 staged/no_claims 的 item；已 committed/skipped 的 item 不重扫、不重写，
  同一 run 的已提交 receipt 不会被再次解释为实时任务冲突。重复收尾保留原状态、item/job ID 和回执。

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
  → MemoryFactService
```

extract 每个事件独立调用 `ModelTask.MEMORY_EXTRACTION`，只暂存 proposal。当前事件是唯一证据；
`evidence_quote` 必须逐字存在于当前事件且与 claim 语义锚定。同会话较早事件只按
`current_speaker / other_member / bot` 提供消歧，不得独立产生事实。回复方式、称呼、格式、
语音和表情等交互要求由提取结果表达，不按固定风格分类硬拒绝。

`third_party_mode=trusted_metadata` 只接受持久 `yuki_context`、OneBot 数字 at 段，以及同一 Bot、
同一精确群的回复事件作者；不按正文姓名、昵称、FTS 或向量猜人。disabled 模式只提供 speaker
和 group。

## 审阅与提交

提取结束进入 review。proposal 默认 pending，可按 ID、scope、operation、kind、authority、
group、subject 和 confidence 范围批准或拒绝。批准的是 claim，不是预先计算的数据库 action。
已批准子集可以先 commit；未决 proposal 保留，子集提交收尾后回到 review，不要求本轮全部闭合。

commit 按 `source occurred_at → event_id → claim_index` 串行执行，并重新：

1. 加载真实 source event 并验证 SHA-256 指纹；
2. 验证事件资格、Bot 身份和 live/rebuild receipt；
3. 运行 SubjectResolver、Claim Validator 与 Temporal Resolver；
4. 按当前有效主体、来源与批准记录构造事实；
5. 通过共享 MemoryFactService 原子写事实、证据、状态事件和 proposal 提交回执。

CREATE 创建独立事实，不按同 key 搜索旧值、调用 consolidation 或强制合并历史；
纠正和失效沿真实 fact ID。过期 claim 默认 skipped；`stage_invalidated` 可以保存
带 expired 原因的历史事实。来源和回执复核防止越权或重复提交，不增加 active 容量淘汰。

FTS 由现有触发器同步；active 新事实只排队现有 Embedding job。Embedding 故障不会回滚事实，
run completed 也不表示异步向量已经生成完毕。

每批 proposal 提交后，仅收尾该批涉及的 item，并额外处理一页可以收尾的 item，页大小不超过
`MEMORY_REBUILD_COMMIT_BATCH_SIZE`。额外扫尾覆盖全部拒绝和 no_claims 的事件，不新增持久 cursor。
只读准备批量加载 proposal 状态计数、事件与 canonical owner、已有实时回执；短写事务重新核验，
再按真实回执结果更新 item。pending、processing、done 的实时任务不得被覆盖；只有 selection
明确包含 failed live jobs 时才允许接管 failed。
来源正文和指纹也在只读阶段按候选页核验；`trusted_metadata` 继续复用既有主体补全，
可能增加只读查询，不在 writer 内补全或解析正文。没有候选或重复收尾时不申请 writer。
可信引用的 Person、Binding、Presence 和内部 reply 事件元数据也纳入同一页的核验，
包括被过滤的原始引用；准备后引用状态改变时延后该 item，下一轮重新准备。
单项主体失效只将该 item 标记为来源变化，不阻塞其他 item，也不清空引用或改写来源哈希。

已批准子集提交后，失败 proposal 进入 commit_paused，未决 proposal 回到 review；
仍有待提取、失败或提取中的 item 时进入 extraction_paused。只有剩余提取、审核、提交和
item 回执都已收尾，run 才能进入 completed。
单轮返回值仍是处理的 proposal 数；只有回执扫尾的轮次可以返回 0 并继续保持 committing，
后续轮询继续处理剩余页。中途暂停或重启按已有 run、item 与 receipt 恢复，不重新执行已提交事实。

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

Tool Kernel 的 `admin_memory_rebuild_*` 工具共用同一服务和真实事件权限绑定，不能
跳过 review。插件是否可见仍由当前 SDK 和 capability 清单决定，不能借管理工作流绕过真实授权。

## 配置

所有配置见 `.env.example` 的 Memory rebuild 区。默认关闭，提取并发默认 2，提交始终串行。
`MEMORY_REBUILD_MAX_EVENTS_PER_RUN` 留空表示不增加部署上限；配置了上限时 selection 必须显式
提供不超过该值的 `maximum_events`，不会静默截断。

## 隐私、运维与排障

日志、health 和普通 status 不记录事件正文、claim 正文、selection JSON、QQ、群号、模型完整
输入输出或密钥。review 是超级管理员主动请求的有界审阅页。`/ai forgetme` 会由事件外键级联
清理 staging，删除以该人物为 subject 的 proposal，取消仅针对该人物的非终态 run，并从其余
selection 中删除精确 QQ；已提交人物事实继续按现有 forgetme 规则删除。
所有别名合并为一个 readonly prepared plan，精确匹配 JSON 数组成员并在 writer 前完成 selection
重写和 hash。共享隐私 writer 必须传入 prepared；writer 复核完整 run 目录的 id/hash/status/
updated_at，再按原行 token 删除 proposal 和更新 selection。目录或原行变更会使整个隐私事务
回滚并要求重新准备；不通过 LIKE 匹配平台 ID，也不将多个别名拆为独立提交。


`status` 的 token 数仅累计供应商实际返回的 usage；供应商不返回时保持 0，不做字符数伪估算。
原 extraction 指纹保留为历史说明；换模型、Prompt 或 Schema 不封死原 run 的恢复，
提交仍核验已存 proposal 的 source hash、当前 owner、来源、批准状态与实际回执。
延迟以累计毫秒记录，Embedding 任务数按本 run 提交后实际关联的新任务统计。

常见状态：

- `process_restart`：这是预期的安全暂停，检查状态后显式 resume。
- `source_event_changed`：事件在审阅后被修改或兼容主体元数据变化，proposal 会跳过。
- `live_job_active` / `already_processed`：实时 Worker 正在处理或已经完成，Rebuild 不抢占。
- `historical_claim_expired`：selection 使用默认 skip，过期历史不会成为 active。

升级按生产手册停止写入并保存一致性数据库与配置备份，schema 以当前包的 Alembic head 为准。
代码回退核对相应迁移的降级能力与镜像要求，不能覆盖上线后新增的消息、事实和回执。
