# SQLite 统一写入实施：C 域

日期：2026-10-23。工作分支 `codex/sqlite-writer-runtime-20261023`，产品基线
`c2ce7da6`，任务书提交 `4bffe24f`。本文只记本域实际进度；尚在实施，未提交、未提 PR、未上线。

## 范围和分段

本域负责 runtime、model_runtime、execution_trace、social、plugin_host、sandbox、services
及对应回归。公共 Database、unit_of_work、CommitHints、生命周期由 root 修改；A/B 领域 owner
各自负责其持久化实现。独立 Manager jobs.sqlite3 与参与快照线程不接入主库队列。

| 阶段 | 任务书 / 验证 | 实际状态 |
| --- | --- | --- |
| 入口迁移 | W25–W41；V05–V08/V20/V21 | 已迁 55 个产品文件的 198 处 owned reader、38 处简单 owned writer；原供给 session 继续复用。仍在逐方法检查，不能据此称全覆盖完成。 |
| Work / paid / 来源 | W25–W31；V10/V13/V17/V34/V37–V40 | 已删除 `_assert_lease` 抢锁同值 UPDATE，保当前 SQL fence/expiry；heartbeat 与 paid publication 接公共提交三态。跨入口原事件/邮箱/Person 在 journal writer 中重核。子任务结果与 Sandbox completion 在外准备，按原来源字段提交。 |
| 工具错误 | W06；V12/V22/V38 | agent_tools 保存公共提交三态、retryable 和原阶段；unknown 保 uncertain=True / mutation_committed=None。已确认 commit 不因 error 展示改成未提交，JournalUnavailable 保底层数据库阶段。五组三态工具场景通过。 |
| 通知 / 文件 | W38–W41/W70/W71；V34/V35/V41/V44/V45 | ledger + finish_outbox 同 writer；四个 supplied ledger 消费者显式 prepare_append；media create 按原 handle 确认，只有确认回滚才 unlink。媒体按 128 个真实候选分页，失败 unlink 不遮挡后页。未知且无登记的文件保留，见下文边界。 |
| 授权 / 反馈 | W35/W44；V21/V42/V46 | MemoryAdmin 显式 prepare_write、同 writer 复用授权；ClientLink 采用原 request ID 的账号认领准备。反馈完整只读准备，writer 只核本页见证、原 Work/seq/privacy；迟到真实效果阻止过时 no_reply。 |
| SELF 前台退让 | W49；V46/V47 | 合入旧 quiet 工作区三处精确补丁：executor、main_agent_turns、processor，保同一个活动计数和 best-effort slot。四个 quiet priority 测试通过。Self service/worker 与配置由 B、root 负责。 |
| 全域验证与三轮复审 | W62–W64；V01–V50 的本域场景 | 第一轮实现与定向自检已完成；宽回归修复后复跑中。三轮全范围交叉复审由 root 待各域收束后轮转，目前未完成，不把定向自检算作三轮。 |

本次不运行真实模型、生产库或平台消息。用户认可当前延迟，性能目标是保交互速度；前后数据
须用同样到达负载和固定模型/传输行为，不能只以 BUSY 减少或检查项数量称提速。

## 实际边界

`runtime` 的 Work、journal、effect、paid、delivery、wait 和 subagent 自管写统一使用主库
writer；现有 supplied session 继续沿调用链传递。租约的准入/续期以 writer 中执行 SQL 的
数据库时钟为准，Wait 的 after_seconds 仍以原请求时间归一化，不随排队延后。
`derive` 的来源解码、handoff checkpoint 的更新、子任务结果编码、Sandbox completion 解码
和 ACP permission payload 准备已从 writer 移出或改为原 SQL 小列操作。

`work_source_guard` 的跨入口准备只保存实际选定原事件的本地 proof。`WorkJournal.save`
在原 publication writer 内核对相同事件原值、隐私代次、client_input 邮箱文本/来源与当前同人
归属；恢复后的 guard 先重新证明原事实。没有新增全局来源版本，也不把新聊天替换成旧请求输入。

`participation_feedback` 仍读取完整 run、Work 树计费、Social 与 MemoryTool receipts，解析和
分页在 reader 中完成。每页至多 64 个原 effect refs，各页独立提交；writer 核真实选定见证、
Work 所属关系及已付 ordinal、原 seq 与隐私代次。原有四页每 tick 是公平调度，未读效果留在
原表中继续处理。终态 no_reply 额外检查当前是否出现真实效果，避免迟到回执被永久归为未回复。
参与状态的独立快照继续在业务 commit 后保存，没有并入主库事务。

通知发送后只进行一次原平台调用。正常路径将原 outbox attempt 的 sent 与内部出站事件一起
提交；账本故障回滚后只登记原消息号和 uncertain，不能再 claim 重发。grant 的初次 owner、
entry、presence 与 lineage 解析在 reader；writer 复核当前身份与原 entry。撤销 grant 对所属
plugin_event waits 使用同一条有真实 JSON 条件的 UPDATE，删除 writer 中逐条解析循环。

媒体 create 丢失提交 ACK 后按原 handle 点读：有登记即返回同一对象，确认未提交才回收准备
文件。结果未知且查不到登记时保留文件、抛出原异常，不重写或补登记。当前 cleanup 仅处理
数据库登记的过期文件；未知且未登记的孤儿文件仍保留，这是实际边界，本次没有新增按 mtime
猜测的文件删除机制。已登记清理保留原 path，先核当前 outbox 引用、再在 writer 外 unlink、
最后删 metadata；取消和 unlink 失败仍能从同一登记续清。

独立 Manager `jobs.sqlite3`、workspace artifact SQLite 和参与快照 owner 不接入 Bot 的主库
FIFO。源码中它们的连接、锁与文件生命周期保留原职责；没有把所有 SQLite 文件塞进一个全局锁。

## 验证记录

以下为离线 SQLite/假 Provider/假传输结果，不是生产或真实 QQ 验收；各组包含重复用例，不相加为唯一数量。

| 验证组 | 已见结果 |
| --- | --- |
| Work settlement/delivery/native media/notification idempotency 四文件 | 147 passed；随后接口变化由相关回归继续覆盖 |
| feedback/ordinary/source guard/notification/media/tool 八文件 | 86 passed |
| 新增跨入口 writer 竞争、通知原子回滚、反馈选定来源变化与迟到 no_reply | 七场景，随 feedback/Work/ACP 组合通过 |
| 原 handle unknown/rollback 与工具提交三态 | 媒体三场景、工具五场景通过 |
| quiet priority + ClientLink + tool | 29 passed |
| plugin 原 Work 观察/subagent 来源恢复/history preparation/SQLite failure | 修正旧 fixture 后 24 passed |
| Work owner/compact/delivery/subagent/ACP 七文件 | 109 passed |
| Work scope/input/protocol/new source/ACP 六文件 | 93 passed |
| Social concurrency/integrity + Work input/settlement + new source + ACP | 102 passed |
| 113 文件宽回归初次 | 1250 collected：1187 passed、31 failed、32 skipped；所有失败已定位并修正，最终复跑仍在进行 |

宽回归发现的旧测试假设也已具体修改：`Database.close()` 现在是最终停止 owner，测试若
只要断开连接池再复用同一批服务，使用 `engine.dispose()`；真正生命周期测试不改。
分词观察点跟随统一 prepare_append，owned reader 数量观察改为 read_session，FIFO 等待观察
改为入队，fake Runner 补同一 ModelExecutor 合同。Windows 上四个真实 `fcntl` 表情文件锁场景
明确跳过，既有 native Monty、Unix socket 和安全 FD 用例仍按真实环境要求跳过。

本域产品 Ruff 与 compileall 已通过。首次 Windows mypy 的 POSIX 平台属性告警单独识别，
并修正本次反馈 proof 新增的三处类型问题；Linux 目标类型检查正在运行。性能比较由 root
以同样负载在基线独立 checkout 和当前分支运行，本文尚无延迟结论。
