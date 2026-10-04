# 数据库回复延迟修复交付记录

日期：2026-10-04。实现基线 `678adab8e72fffd3f39ca50fa3fde5fadbdd0ca1`。
规格及只读生产证据见[任务书](../architecture/database-reply-latency-taskbook-2026-10-04.md)。

当前状态：独立 worktree `codex/database-reply-latency` 本地实现及独立复审完成，全量验证进行中。
提交、推送、CI、合并及部署尚未完成；此处不是线上效果验收。

## 本地修改范围

- 活跃参与 run 保持恢复优先，终态按稳定 ID 和固定巡回上界分页，不再反复对账最近 128 条。
  原 scope/generation、run、反馈 sequence 和效果身份保留；迟到反馈不复活终态 Work。
- 无变化页在显式读快照批读 Work、Social、MemoryTool、child 计量及全部已提交反馈，
  不申请 writer。新增反馈仍按原 64 effects/页提交；单 run 本轮最多 4 页，剩余下巡回继续。
  Host 提交后按原控制器聚合回放并保存，冷加载补放原反馈；517 仅有限重备数据库计划。
- 普通 admission、boundary、event 和 Memory 来源按核验阶段批读，保留原可读范围、
  Evidence lineage、版本及外部等待后再查证，不跨 tick 缓存授权。
- Protocol GC 两个索引分支先读 metadata 页再批查 refs，短 writer 提交 deleting 屏障，
  文件删除仅持协议锁，最后批量确认 metadata。取消等待原文件线程收尾后释放保护。
  普通不可变记录可有界复用 digest/size；opaque、媒体外置规则及发布前文件完整性保持原合同。
- Projection 只准备和插入新增 selection，保留同 epoch 冻结前缀、显式 epoch 边界、来源 CAS
  和 dispatched journal 原子发布。首次 selected 摘要写时核验并批量转移真实 parent refs。
  Observation 先查 scope metadata 和依赖闭包，再读合法正文，保留未选新 work-note。
- `0092` 只增加 `ix_social_operation_scope_updated(source_conversation_id, updated_at)` 与
  `ix_protocol_objects_gc_cursor(deleting, prepared_at, sha256)`。旧 GC 索引保留；迁移预校验
  所有同名对象形状，downgrade 只删除本轮索引，不修改业务事实。

## 当前验证证据

| 范围 | 结果与边界 |
| --- | --- |
| 0092 迁移 | 首轮 23 项及新增预校验/幂等 1 项通过；完整 Social/GC SQL 使用匹配 SEARCH，无 TEMP B-TREE；真实升降级保留协议对象、配额及旧 GC 索引 |
| 既有迁移 | 0090/0091/0084/0085/Work result 59 项通过；未修改冻结旧迁移 |
| 诊断边界 | SQLite diagnostics/phase/physical exit/DiagnosticWriter 20 项通过；正常 read rollback 不当失败，真实 DBAPI 收尾及可丢诊断保持原合同 |
| 参与反馈 | 新对账文件11项、新来源文件12项；受影响85项及最终反馈45项通过。冷快照缺proposal只恢复原已提交effect，receipt-first与直接cold hydrate均不提前失效合法来源；现存proposal冲突拒绝不被兜底绕过，commit确认丢失下轮查原64refs继续 |
| Protocol | 协议/媒体/存储/boundary组合70项通过；新增文件最终16项通过，主会话追加 missing-source 分类与零引用重建回归1项通过 |
| Projection | 初轮54项通过；宽parent补修后组合51项通过，真实SQLite变量上限300下验证640父节点、paid summary发布及selection引用转移。source/privacy、循环拒绝、逐summary-parent持有及journal回滚覆盖；主会话追加130项bootstrap各INSERT≤999绑定变量且顺序完整的回归通过 |
| 静态检查 | 全仓Ruff check/format通过；本机按生产Linux目标执行 `mypy --platform linux src`，697源文件通过。原生Windows目标报29项既有POSIX API缺失，不改运行接口压过类型错误 |
| 集成、CI | 尚未完成 |

各子组次数不相加冒充最终冻结代码的全量结果。只有固定最终提交上的验证可以作为交付门槛。
所有本地竞争用隔离 SQLite/WAL、fake Provider/transport；没有发送合成 QQ 探测消息。

## 性能口径与剩余限制

同一真实 SQLite fixture：128 个终态、每 run 已有空反馈、无 Work/controller。基线
`678adab8` 模块与新代码分别运行同一对账，均零 DML/IMMEDIATE：

| 观测 | 基线逐run | 本轮批页 |
| --- | --- | --- |
| session | 512 | 8 |
| SELECT | 640 | 40 |
| 单次总时间 | 0.7166s | 0.0392s |

该墙钟只有1个样本，仅作本机诊断，不给p95或线上改善比例。带真实 Work、child 与
已加载控制器的路径另由完整反馈和恢复回归验证，不能套用此fixture的绝对SQL数量。

Projection fixture：20旧+1新只INSERT1条selection、新正文85bytes；重复已选summary
零selection INSERT、零parent DELETE。270个covered parent共1,109,160bytes正文无需加载，
只读取有效root及新note2行、46bytes，metadata仍完整分3页。prepare 12样本本机p50
7.638ms、p95/max8.886ms；这些数字没有前后线上同负载对照，也不作为CI墙钟门槛。

生产审计发现 506 个终态 run（335 completed、164 no_reply、7 interrupted），旧尾页128条
不能保证覆盖迟到效果。16条/页的无故障巡回为32个tick；原2秒间隔之外仍有每tick实际
工作和调度时间，不能称为64秒墙钟硬上界。单run证据量仍按完整真实事实读取与计量。

Projection 的完整冻结 JSON 仍需更新；减少 selection 参数与无效 payload 不等于消除所有
写入成本。JSON parent 图的 metadata 总量仍可能增长。publication 从文件核验至原 ref/journal
提交仍持协议锁；本轮缩短 GC 临界区，没有宣称消除所有跨会话 writer 等待。

此前生产已有明显 swap-in、I/O PSI 与内存压力。Provider、工具及合法分条等待单独计量，
不把整轮非模型耗时都归给数据库。隔离 fixture 的调用量或墙钟结果不能冒称线上 p95；
上线后需在自然聊天中分别观察首模型派发、首发送及整轮结束，并报告缺样和不可比负载。

## 迁移与回退

部署前重新核对真实 Bot 镜像和 Compose 文件，按现行手册做一致 DB/协议文件备份及引用校验。
如有部署授权，仅替换 Bot，保留 SnowLuma 与原服务配置，不创建正式 Release。
旧镜像严格核验 schema head，不能直接读取0092；回退时停 Bot，用本轮代码执行
`alembic downgrade 0091` 后切回已保留的兼容0091镜像。保留升级后新消息、预算、回执
和协议文件，不能用旧数据库备份覆盖。
