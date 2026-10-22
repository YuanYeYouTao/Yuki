# 2026-10-22 SQLite 写入竞争调查

这次竞争主要出现在消息入账和后台写入重叠时：入账先建立读快照，随后插入回执；期间其他事务已经写入或提交，入账就可能在升级为写事务时遇到 `SQLITE_BUSY` 或 `SQLITE_BUSY_SNAPSHOT`。线上已有回滚后重做纯数据库操作的处理，因此日志里的失败次数不能直接当作消息丢失或业务失败次数。

自省状态写入也出现了竞争，但九条日志还不能定位到具体仓库方法。后台 SELF 调度需要让前台聊天优先，这项整改与数据库事务问题分别记录。

本文记录当日只读调查。整理时的远端 `main` 为 `cdb49eadc0c7cd16060bf0329234ae69611e62bd`；线上采样、源码判断、本地修复和部署状态分开说明。

## 固定窗口与统计口径

日志窗口为 **2026-10-22 12:07–13:07 UTC，即 UTC+8 的 20:07–21:07**。统计失败 SQL，不把不同时间窗口的数字混在一起。

| 原生错误码 | 含义 | 失败语句数 |
| --- | --- | ---: |
| `5` / `SQLITE_BUSY` | 当前写入与其他连接的事务发生竞争 | 58 |
| `517` / `SQLITE_BUSY_SNAPSHOT` | 已建立的 WAL 读快照无法继续升级为写事务 | 30 |
| 合计 | | 88 |

| 失败语句 | 数量 |
| --- | ---: |
| `INSERT canonical_event_receipts` | 78 |
| `UPDATE memory_self_reflection_runs` | 9 |
| `INSERT autonomy_initiative_feedback` | 1 |

这些失败语句的记录耗时中位数为 **1 ms**，最长 **24 ms**。它们大多很快返回错误；这不是“每条 SQL 都等了两三秒锁”的证据。

同一窗口也有成功写入耗时约 1–2.2 秒。当前诊断没有拆出驱动排队和 SQLite 内部等待，`driver_queue_seconds`、`sqlite_wait_seconds` 都是 `unknown`，不能把这部分耗时全部算作锁等待。

一次入账可能回滚后再次尝试；一项业务也可能包含多条 SQL。因此，这 **88 条失败 SQL 不等于 88 条消息丢失，也不等于 88 个业务执行失败**。是否最终成功，需要沿原事件、run 和持久回执逐项关联。

## 日志里同时出现了哪些 writer

`sqlite_write_contended` 会记录进程内当时仍被诊断器追踪的 writer。出现较多的候选如下；表格不是全部失败样本的互斥归因。

| 候选操作／协程 | 出现次数 |
| --- | ---: |
| `social_operation_receipts` / `TaskHandle._run_coro` | 22 |
| `conversation_media_items` | 7 |
| `execution_trace_entries` / `DiagnosticWriter` | 6 |
| `space_bindings` | 4 |
| `BEGIN IMMEDIATE` | 3 |
| `canonical_event_receipts` | 3 |
| `chat_events` | 3 |
| `canonical_conversations` | 3 |

另有 **29 条记录的 holder 为空**。不能据此认为当时没有竞争：例如返回 `SQLITE_BUSY_SNAPSHOT` 时，使旧快照失效的其他写事务可能已经提交，不再出现在当前 holder 列表中。

有值样本中，`held_seconds` 中位数为 **2.156 秒**，最大 **3.177 秒**。诊断字段明确标记 `precision=lower_bound_after_first_write`：它从已观察到的成功写入开始计时，是当时事务占用时间的下界。

这些 holder 是调查候选，还不是逐条锁冲突的阻塞者实锤。不能把它们的次数当作各模块“造成业务失败”的次数，也不能用 `held_seconds` 代替失败 SQL 的等待时间。

诊断实现见 [`sqlite_diagnostics.py`](../../src/yuki/persistence/sqlite_diagnostics.py)。

## 消息入账为什么容易撞上竞争

消息入账由 [`canonical_uow.py`](../../src/yuki/identity/canonical_uow.py) 调用 [`Database.apply_snapshot_write`](../../src/yuki/persistence/database.py)：

1. 开启 session，并在 SQLite 上显式执行 `BEGIN`。
2. 查询已有事件、回执、Presence 等入账依赖。
3. 写入事件和 `canonical_event_receipts`，最后提交。

第二步已经建立读快照。其他入账、Social 回执、媒体、诊断或后台任务可能同时使用别的连接写入同一个数据库。

- 其他 writer 仍在写，当前连接可能返回 `SQLITE_BUSY`。
- 其他 writer 已经提交，当前读快照落后于数据库，升级写事务时可能返回 `SQLITE_BUSY_SNAPSHOT`。

后一种情况不能靠继续等当前快照解决，需要结束旧事务，用新快照重新检查依赖。部分 `SQLITE_BUSY` 也可能在读事务升级时立即返回；仅凭错误码 5 不能为全部 58 条指定同一种内部等待过程。

现有 `apply_snapshot_write` 会在操作阶段遇到 BUSY 类错误、原事务回滚并退出 session 后，开启新 session 重做同一个纯数据库操作。内部事件 ID 和入账计划保留，不重新接收一次平台消息，不重新调用模型，也不重新执行外部效果。提交阶段失败不在这条操作重试边界内。

所以，78 条回执插入失败与这条并发写入路径相符；它们说明数据库竞争仍然存在，不能仅凭数量推断消息入账最终失败。

本次检查没有发现入账写事务持锁等待模型或外部网络。模型调用和外部准备在相关写事务之外，不能把这次竞争描述成“拿着 SQLite 写锁调用模型”。

## 自省的缺口还在哪里

[`SelfReflectionRepository.save_checkpoint`](../../src/yuki/memory/self_reflection/repository.py) 已使用 `apply_snapshot_write`，在同一快照中检查来源并发布检查点。

同一仓库的其他部分写入方法仍使用普通 `session.begin()`，包括完成、失败和恢复等状态更新。它们没有因此自动获得 `apply_snapshot_write` 的操作级回滚重试行为。

这里还要区分 SQLAlchemy 的事务范围与 SQLite 的物理事务：线上 Python 3.12 使用 sqlite3 的 legacy transaction control 时，普通 `session.begin()` 中执行 `SELECT`，不一定已经向 SQLite 发出物理 `BEGIN`。因此，不能看到“先 SELECT、后 UPDATE”就认定这九次都是旧读快照升级失败。

**当前只确认九条 `UPDATE memory_self_reflection_runs` 失败，没有确认各条出自哪个方法、是否沿原 run 恢复，以及最终业务结果。** 这部分需要按原 run、调用链和状态回执继续关联，不能直接归给 `save_checkpoint`。

## SELF 调度的线上情况

以下数据来自 **2026-10-22 22:03:29 UTC+8** 的只读检查，覆盖近 24 小时，和前面的 SQLite 一小时窗口分别统计。

| 调度记录 | 数量／结果 |
| --- | --- |
| `scheduled` cycle | 3 |
| `retry` cycle | 252 |
| 其中未处理批次的 `retry` cycle | 188 |
| 其中有处理的 `retry` cycle | 64，合计 107 次 batch attempt、33 次 deferred |
| 当前 waiting run | 14，均为 `failed/preempted`；最老来自 10 月 14 日，只有 2 个来自近 24 小时 |

`retry_due` 只检查是否存在到期的 waiting run；真正的 `claim_due` 还要检查来源是否有效、当前状态和领取阈值。两处资格不同，旧 waiting 可以一直触发新 retry cycle，却始终领不到批次。这解释了大量空转，不能把 252 次 retry cycle 都当作实际重新处理了旧批次。

有实际处理的 cycle 也存在让路不彻底的情况：21:40 附近一轮连续处理 7 批，其中 4 次 `preempted`，循环仍继续推进。现有抢占覆盖 Provider 请求期间，Main 的工具执行阶段没有同等覆盖；线上也没有配置一段持续静默时间作为后台继续条件。

这些事实说明 SELF 会在固定时段之外频繁启动，且被前台抢占后还会继续下一批，增加后台与前台重叠的机会。它们没有建立与那 88 条 SQL 失败逐条对应的因果关系，但调度本身已有明确可删的多余工作。

## 服务器资源的影响

资源检查在 **13:13 UTC（21:13 UTC+8）** 左右完成，晚于上述固定窗口，属于相邻时段采样。

| 项目 | 当时结果 |
| --- | --- |
| Bot CPU 配额 | 无限制；`cpu.max=max`，`nr_throttled=0` |
| Bot 内存限制事件 | `high/max/oom/oom_kill` 均为 0 |
| 宿主内存 | 总量约 1.57 GiB，可用约 668 MiB |
| Swap | 使用约 1 GiB；六秒采样内存在换入、换出 |
| I/O | 六秒采样的 I/O wait 为 2–9%，存在 I/O pressure |
| 数据库挂载 | `/app/data` 对应宿主 `/opt/yuki-qqbot/data`，位于本地 `/dev/vda3` 的 ext4 |
| 磁盘 | 剩余约 20 GB，使用率 48%，inode 使用率 10% |
| 当天内核日志 | 未见 ext4 错误、I/O 错误、磁盘重置／超时或 OOM |

从运行 Bot 的 Python 进程文件描述符确认，实际业务库为 **`/app/data/qq_ai_bot.db`**，当时约 **2.50 GiB**，WAL 约 **7.31 MiB**。`participation.sqlite3` 是同进程另外打开的库，不能拿它的大小代表 Yuki 业务库。

换页和 I/O 压力可能延长事务占用时间，从而放大并发写入的影响；当前没有 CPU 配额限流、OOM、磁盘空间不足或存储错误的证据。这些资源样本也不能证明内存泄漏，或单独解释上述 88 次失败。

## 调度整改要求与当前交付状态

本次确定的整改如下，**尚未部署到线上**：

1. 删除固定时段之外自动创建 retry cycle 的入口。旧 run、waiting 状态、检查点、累计预算和回执保留，后续由 scheduled 或 manual cycle 按原身份恢复。
2. 遇到 `preempted` 就结束本 cycle，不继续领取或执行下一批。
3. 复用现有模型 executor 的调度能力，加入全局静默等待，默认 300 秒；前台活动覆盖 Main 整轮，包括准备、模型请求和工具执行，持续活动期间不让后台抢先继续。
4. SELF worker 在准备、领取和每批执行前等待静默；service 在已有检查点落盘后、mutation 前再次等待，避免模型请求结束后紧接着与前台争写。

恢复继续使用原执行、原已付输出和已保存检查点，不靠重做模型或外部操作追进度。原已提交请求和已发生效果仍按事实保存。默认 300 秒属于后台调度配置，不是模型请求的硬截止时间，也不是新增的业务完成条件。

这项调度调整减少后台与前台重叠的机会，但数据库里的短事务、正确回滚和原身份恢复仍各有职责。不能把降低后台活动量当成事务竞争已经修好。

截至本文初稿：

- 固定窗口统计、线上环境检查和上述源码核对已完成，调查期间没有修改线上数据库、配置或服务。
- 新的 SELF 低优先级整改已在独立分支完成本地实现、定向验证和交叉审查，**尚未提交或部署**；上述 300 秒静默、前台活动退让和遇抢占退出均不能写成已经在线生效。
- 本地实现、验证、提交、PR、合并和部署结果由本次修复的交付记录补充。
- 上线后需要在自然聊天流量下，沿固定窗口复核竞争日志、前台响应和原执行恢复情况；健康接口正常不代替这项验收。

后续排查重点是：补齐九条自省状态更新的具体调用来源与最终结果，缩短已观察到的 writer 占用，并在同一时间窗口关联失败操作、候选 holder 和最终业务回执。

后续统一架构设计见 [SQLite 统一写入与短事务重构任务书](../architecture/Yuki-SQLite统一写入与短事务重构任务书-2026-10-22.md)。这份任务书尚未实施，不以自省调度修复代替 writer 重构。

事务与恢复要求见[开发架构约束 §6](../architecture/development-contract.md#6-sqlite-与诊断)；自省的原 run、预算及检查点合同见 [Self Reflection](../architecture/self-reflection.md)。
