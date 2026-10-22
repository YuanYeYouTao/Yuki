# SQLite 实现后复审 R3：Work、Social 与全局交付

起点为 `1fb34b85031dc3a7df1f8f5c7e7f0cdf60fc6966`。本轮轮转回查原 C 的 211 个路径；
root 负责 runtime、model_runtime、execution_trace、social 的实际 DB 方法和执行消费者。
services 由 [A](sqlite-writer-review-r3-a-20261023.md) 补审，Admin/commands/部分执行入口由
[B](sqlite-writer-review-r3-b-20261023.md) 补审；plugin_host、sandbox 由
[C](sqlite-writer-review-r3-c-20261023.md) 补审。各报告会明确自己的路径与分级，不以一张搜词清单替代源审查。

R3 及补审已收束，SQLite 阶段最后产品快照为 `d23a487d`；`c0af6e82` 保留为上一完整 48 轮性能快照。
最后 PluginManager、空路径与 eager 调度修改均修复后再读，尚待最终性能确认与全量 CI。
本报告的 97 个 migration、head `0147` 等是该阶段记录；后续 #494/#497 的 99 个 revision、
head `0149` 及新增代码审查另见[合流记录](open-pr-integration-20261023.md)。
合流后的 journal/人物查询删除与 reader 关闭顺序另经独立复读及真实 SQLite 探针，公共 47 项复跑通过；
实际结果与未达到平均加速的条件见[最终性能记录](sqlite-writer-performance-20261023.md#合流后的最终热路径)。

## 本轮删除与核查

- Work 每次扣费原先先查 root 的已有使用量，再 INSERT budget。改为原 writer 内 `INSERT … SELECT`，
  删除一次 SQL 往返和 Python Row；仍由原父链解析 root，首次初始化取当前计数，已存在的共享预算、限额与扣费不重置。
  invocation crash window、CodeMode composition、native Social 与 Person runtime merge 四文件的非 native 选择
  共 44 passed、13 deselected。这里的 native Social 指实际 Social 领域测试，不冒充已构建 Monty native worker。
- 公共 owner 的 probe、重复 raw-get task 删除及 reader 物理取消修复由 B 实施，root 再读完整当前 Database diff。
  reader 的实际 checkout、取消后的 invalidation/close/physical_exit 使用原 driver；没有另建连接注册表或读队列。
  取消不能让尚未退出的线程继续在下一 writer 身后写。正常 close 仍归还池，不逐次关闭原生连接。
- A 的 ClientWorks 正常结果不再被 finally 的独立 lease release 失败替代；root 核实际 try/except/else。
  确认提交后的原 UUID/payload/receipt 优先，取消继续传播。没有把未确认业务提交当作成功。
- C 的 Memory operation receipt 在全部原恢复位置比对真实 payload fingerprint；root 核 prepare、writer 竞争赢家和
  ACK 丢失后的原回执读取，不用无效 repository 参数暗示已经比较。不同 payload 不冒领同一 operation 的成功。
- Automation 的 create_run ACK 丢失修复按原 `(automation.id, scheduled_for)` 收尾；root 核原 RUNNING 与 Work-owned
  exclusion 仍在。没有重造发生、重置预算或重新派发模型。
- 再读 Work input 与 Protocol publication，删除无 SQL 的 reader 和空 refs 的线程准备。
  前者沿原 before_publish 条件，后者保原 publication 文件围栏及成功清理；有材料的查询、文件核验与当前 proof 不变。
  四份 preparation/storage/source 回归 49 passed。
- `_complete` 局部采用 Python 3.12 的 eager task factory；仍由同一个屏蔽任务持有原 awaitable，
  同步完成时省去初始排队和 shield callback。root 的真实取消/commit/reader/启动/lifecycle 组 47 passed，
  A 独立再读并补跑 4 项真实取消/ACK 回归通过。缓存 checkout 与空 flush 的临时调度探针由 4 个 callback
  变为 0；真正挂起仍等待原 Future，没有减少 Task 对象。全局 task factory 不变。
- root 补读 PluginManager 的实际 start/stop、MainTurn 和 DirectDelivery，发现 stopping 元数据写失败会在
  managed 对象出队后跳过插件与 Host scheduler、后台 DB 任务关闭。已交 B 在原生命周期收尾中修复，
  使用真实 SQLite ABORT、实际 COMMIT 后丢 ACK 与原取消回归，不建立新的插件重试或执行身份。
  B 修复后 root 重新读完整实际 delta，确认逐实例收尾继续到其他原插件、内层启动 rollback 完整退出，
  最初取消/DB phase 保留。五文件 19 passed、1 skipped；跳过为本机未构建的 pinned Monty，交 Native CI。

## root 实际审查边界

| 范围 | 本轮核查 |
| --- | --- |
| WorkRepository | 全部具名 DB 方法：lease/accept/derive、state/checkpoint/communication、enqueue/prepare/stage/consume、cancel/privacy、child completion、输入恢复、media orphan、T1/T2/domain ref 与 late effect CAS。供给 session 不另开 owner，真正执行资格取当前 writer；旧 paid/receipt 的晚到确认不被新 lease 一概拒绝。 |
| WorkSession/Journal | 恢复先选原私有 paid protocol；source guard、contract、original pending call 与 readonly link；原 manifest/媒体准备在外。dispatched/response/paired、Compact 与 publication 保原配对；只确认回滚的 BUSY 可重备同一 DB 发布，unknown 不回到模型。 |
| WorkSourceGuard | 原显式 reader snapshot 中 hash/完整源读取；现有 conversation revision 与身份/privacy、原 observation 权限。当前 writer 对 selected cross input 原字段逐值比较并核原 mailbox/Person；不把一条新增非相关输入变成全库 revision 门槛。 |
| WorkWait | register/reply/match/prepare/current/apply_event/deliver_due 全 DB 方法。匹配集合含新增成员；owned-run 原完整集合与 completion bytes，Internal event 引用、水位、原 mailbox 同事务。到期驱动的 writer 取当前时间；relative wait 保原请求时间语义。 |
| WorkControl/Supervisor | 原 accept/update/derive/handoff/wait/control、request reservation、input consumption、settle/recover。paid budget 在 dispatch 前，实际失败 phase/三态传 ResultCapture；真实 accepted 控制和已发生效果决定收尾，不另加汇报/轮数资格。 |
| Work management/query/tree | 当前 subtree、原 revision/owner/source 分类、budget root/descendants；cancel 只撤销未来资格，不宣称外部进程已经停。reader 目录的 limit 仅展示，不拿它作执行全集；源大正文不进入管理 writer。 |
| Activation/heartbeat/schedulers | 原 lease SQL-time/下界、FIFO 等待后实际资格；有效租约内已确认 BUSY 才重试。停止接纳后 join 原任务；Work/child/time-driver 独立推进，后台模型等待不包 writer。 |
| SubagentRepository | start/acquire/maintain/archive/message/resume/cancel/finish。原 parent source、brief 和大正文在外备；整树资源准备与当前 selected eligibility/retention/新增 request 同 writer 复核。原 result 与 parent mailbox 同笔，目录分页不等于执行全集。 |
| ProtocolStore | put/record/snapshot/manifest/hydrate/publication/publish_refs/GC。不可变文件准备、hash 和 thread join 在外；publication 文件围栏→reader→writer，GC mark 先释放 writer→文件围栏/reader/unlink→释放围栏→ack。无相反持锁等候。 |
| ModelInvocation/Trace | record/direct fallback、conditional diagnostic insert、stats、coverage、TTL、trace span；压缩线程在 writer 外。生产共享队列可丢诊断，不可丢执行回执；telemetry 失败不推翻已完成 Provider，缺 cache 字段仍未知。 |
| ModelExecutor | 核实际调用边界、前后台 slot/quiet、HTTP 及诊断出口，纯 profile/capacity 编码按无 DB 排除。所有等待和 Provider 都在业务 writer 外；付费 dispatch 仍走原 request/account callback，不因数据库异常扩大 transport retry。 |
| SocialRepository/EmojiIntent | prepare/sequence/claim/finish/recover/receipt/facts 全 DB 方法；hash/JSON 布局在外，claim 必须先提交再发送。已有原 receipt、计划完整性、当前 target 同人关系仍按实际值。selected intent 不冒充已发；已发 unknown 不重新认领。 |
| SocialService | 核实际 route/actor/source、准备、claim、物理发送、正常 settlement、补记和 replay。正常 ledger+Social+Work receipt 同笔，任何一步失败整笔回滚；已经物理发送后的失败只能补原事实，不重发。confirmed content 原存在性 SQL 保当前源/隐私。 |

声明、枚举、schema 字段、固定工具文案、纯哈希/JSON/媒体 codec、内存 coordination 只核其 imports/调用与 I/O
边界；没有把它们写成逐字算法全文审查。实际 schema installer、Protocol quota trigger、effect indexes 属于真实
DDL，归初始化/迁移 owner 交叉核，不因文件名带 schema 而排除。

## 性能方法复核

同一 `scripts/benchmark_sqlite_writer.py` 从选定 checkout 导入所有 yuki/tests 源并核真实路径、commit、dirty
状态与脚本 SHA。模型和平台固定 Fake，不访问生产、不调用收费模型。到达时间预先排定，热身和逻辑准备不计时，
最后诊断排空保在计时内。原输入到成功回执通过实际模型 call key 与 Social receipt 关联，不能凭最新 event ID 猜。

普通 chat 三个交叉周期，每周期旧→新→新→旧；每版本共 72 条输入，所有运行保留。其余保持相同 fixture/到达
负载，包括 mixed、150ms stress、1024 尾部、Memory/forget/merge/GC 和两 logical CPU 场景。
SQL Engine events 看不到 native pre_ping 或隐式 BEGIN；新旧 acknowledged-write interval 起点不同，不能拿来
声称总持锁时长倍数。20Hz callback 采样也不是连续最大事件循环停顿。各项实际结果另列，不能只取最快一轮。

## 本轮后续

完整三轮及 supplemental 的实际 DB/调用者审查已经完成，已确认的问题均已修复并复读。
最终源码全仓 Ruff、mypy（646 source files）、lock、diff 检查通过，97 个冻结 revision 无 diff，head 仍 0147。
剩余是固定源码性能确认、Linux 全量/Native CI 与 PR 交付。当前没有合并、Release、镜像或部署。
最终交付逐项对照见[验收对照](sqlite-writer-acceptance-20261023.md)。

## 全量 CI 后的复验

首次 Linux CI 的 Native CodeMode 93 passed，unit 3546 passed / 6 failed，integration 831 passed / 2 failed。
两份 integration 测试仍调用旧 continuation 接口，改用现有 reader 准备与当前 writer 匹配后，相关 23 项通过。
两项 notification 并发测试的真实 barrier 已在正确 reader 边界，失败只来自末尾断言调用已删除 helper；
断言改读当前 production publication 数据路径，原 grant/source/segment bytes/paid/receipt 断言全部保留，相关 6 项通过。

另外四个参数的 Rebuild SQL 数量回归暴露了一个实际冗余：准备期 `_read_batch` 查询 MemoryJob receipt 后，
caller 丢弃它，writer 又读取一次。已删除准备期 receipt 查询，将共享的两组 metadata 读取命名为 `_read_items`，
回执只在原 writer 的 run/selection 核对之后读取一次；source/trusted/item 与首 DML 前的证明保持。
测试的每页 `SELECT <= 10` 未放宽，四个并发注入 hook 仅迁名，receipt finalization/trusted/privacy 三文件
35 passed，单模块 mypy、Ruff/format 通过。B 独立复读实际顺序与原 paid/live receipt 归属，未发现语义缺口。

这些是全量暴露后的修正和定向复验，最终合流源码仍须完整 CI；没有把上述分段数字合成一次全绿。
