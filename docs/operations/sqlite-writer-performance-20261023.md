# SQLite 重构的性能对照

旧版为 `c2ce7da66ab54efe192993b9615df48266a62137`，R2 为 `1fb34b85`，删除重复工作后的 R3
快照为 `c0af6e8240548acbbd816edc07a3b964d1a16657`。R3 的 48 轮已完成，共 576 条输入，
每版本 288 条。逐轮命令、每输入关联与统计保存在[原始摘要](../architecture/sqlite-writer-audit-2026-10-22/performance/c0af6e82.json)，
[R2 对照摘要](../architecture/sqlite-writer-audit-2026-10-22/performance/r2-summary.json)单独保留。

普通聊天三组交叉对照，共 144 条输入：成功回执平均 **397.2 → 366.1ms**（减少 31.0ms，7.8%），
P95 **517.3 → 438.4ms**。处理函数 entry→return 平均 **476.7 → 428.8ms**（减少 47.9ms，10.1%）。
R2 同口径还平均慢 6.5ms；本轮没有剔除任何慢轮次。双核、GC 和尾部恢复仍有退步，不能把普通聊天结果扩成所有场景都更快。

## 同负载的定义

- Windows、Python 3.12.13，SQLAlchemy 2.0.51、aiosqlite 0.22.1。同一 Python 环境与同一测量脚本；
  `--checkout` 在导入前选择代码，核每个已导入 yuki/tests 模块的实际路径、commit 与源码 dirty 状态。
- 每个场景固定旧→新→新→旧；普通聊天重复三个交叉周期。每次新建同一逻辑 fixture 和文件型 WAL 数据库，
  不复用上一轮业务状态；准备、schema 与热身在计时外，前后 DiagnosticWriter 都排空，最后排空计入 elapsed。
- 主聊天链走真实 processor、canonical、Work、Journal、Social 与持久回执。Provider 是固定 FakeLLM，
  平台为内存传输；没有真实模型请求、平台发消息或生产访问，也没有真实模型耗时/网络 RTT。
- 输入到达时间事先排定，晚唤醒不重置原计划。成功完成按实际模型观察的内部 event 与 call key，关联
  原 Social `succeeded` 提交后的确认时间；一个合流回复可能覆盖多个确实被模型观察的输入。
- `handler` 是实际 `processor.handle` entry→return，可以早于合流生产者完成。它与“到达→成功回执”分别报告，
  不能用提前返回的 handler 当已经完成回复。单次运行的 P95 不平均成整体 P95；回执 P95 从全部输入样本重算。
- mixed 同时跑原 SELF scan/claim/checkpoint/complete 与同一 Work lease renew；特意不靠 quiet 调度少做事，
  因此这里只比较 writer 改动。quiet 本身的让步由独立回归覆盖，不把两类收益相加。
- CPU2 只把同一进程限制在两个原本获准的 logical CPU，实际 affinity 写入 JSON。没有更换业务参数或加入产品限制。

## 场景与结果

| 场景 | 相同负载 | 旧平均 / P95（ms） | 新平均 / P95（ms） |
| --- | --- | ---: | ---: |
| chat | 每轮 12 输入、间隔 1s；三个交叉周期 | 397.2 / 517.3 | 366.1 / 438.4 |
| mixed | 12 输入、间隔 1s；SELF 与 renew 各 12 次 | 427.6 / 569.6 | 424.2 / 688.7 |
| mixed-stress | 24 输入、间隔 150ms；同后台负载 | 5977.0 / 8096.2 | 2177.3 / 3792.6 |
| memory | 12 输入、间隔 1s；128×4096 字符原 evidence | 462.6 / 848.5 | 460.3 / 779.3 |
| forget | 12 输入、间隔 1s；128 规模、四人族及实际 privacy 引用 | 1314.4 / 2087.9 | 536.7 / 1360.0 |
| merge | 相同规模及实际配置/审计/引用谱系 | 381.3 / 445.0 | 375.6 / 468.6 |
| gc | Work/Protocol/真实 PNG Artifact 与 pending Outbox 保护引用 | 402.7 / 617.6 | 408.5 / 573.3 |
| chat-cpu2 | 普通聊天，两个 logical CPU | 376.2 / 480.0 | 401.4 / 484.6 |
| mixed-cpu2 | 同 mixed，两个 logical CPU | 763.7 / 2378.4 | 815.8 / 2811.4 |

普通聊天每版本 72 输入，stress 每版本 48 输入，其余有输入场景每版本 24 输入。全部输入均获得真实成功
回执关联，没有 workload error。Engine 记录旧版 717 次 BUSY/BUSY_SNAPSHOT 语句错误，新版为 0；
这些是包含重试的语句错误次数，不能称作 717 条消息失败。高并发成功回执平均减少 63.6%，隐私清理减少 59.2%。
mixed 平均只改善 3.4ms、P95 变差；Memory/merge 的几毫秒差距也不足以宣称稳定胜出。

尾部场景没有聊天输入：每版本两次原 paid run 恢复、1024 条尾部，原 cursor/pending/characters/checkpoint 全部
核对通过。平均恢复 **53.4 → 72.0ms**。新版增加了原选择及字段的完整当前来源证明，仍有约 18.6ms 代价；
R1 的约 193ms 已降下来，但不称作比旧版快。只有两个样本，不将其 max/P95 当生产长尾结论。

本次两个 logical CPU 可能是同一物理核的 sibling，记录的是实际 affinity，不把它宣称成两颗独立物理核。
这个快照中的 CPU2 chat 平均多 25.2ms，mixed 多 52.2ms；最终空路径删除后的确认组见下节，
本表原四轮数据继续保留。

## 最终热路径确认：d23a487d

空 reader、空 Protocol 准备与局部 eager 调度完成后，固定最终产品快照 `d23a487d94b7be682a3cfdf651c0edde15806b7b`，
追加 44 轮交叉对照。chat、chat-cpu2、mixed-cpu2 各三个旧→新→新→旧周期，mixed 和 tail 各一个。
每版本 240 条输入，全部关联成功回执，所有源码/导入检查通过，没有 workload error；
逐轮参数、样本和关联见[完整摘要](../architecture/sqlite-writer-audit-2026-10-22/performance/d23a487d.json)。

| 场景 | 每版本输入 | 旧平均 / P95（ms） | 新平均 / P95（ms） |
| --- | ---: | ---: | ---: |
| chat | 72 | 367.4 / 493.2 | 342.7 / 453.6 |
| chat-cpu2 | 72 | 356.8 / 470.8 | 331.8 / 428.2 |
| mixed-cpu2 | 72 | 647.7 / 2806.6 | 656.2 / 2676.4 |
| mixed | 24 | 418.4 / 530.8 | 400.4 / 637.8 |

普通聊天成功回执平均减少 **24.7ms（6.7%）**，双 logical CPU 减少 **25.0ms（7.0%）**。
相应 handler 平均分别是 443.1→402.0ms、441.8→403.9ms。之前 CPU2 普通聊天的回退已在这组扭转。
CPU2 后台混跑平均仍多 8.5ms（1.3%），P95 减少 130.2ms；mixed 平均减少 18.0ms，但 P95 变差。
本组旧版 57 次 BUSY/BUSY_SNAPSHOT 语句错误，新版 0。它们仍是包含重试的语句次数。

tail 每版本两次，实际原 paid/cursor/checkpoint 核对均通过，恢复平均 38.3→59.9ms，仍有 21.6ms 代价。
本组沿用同一个测量脚本 SHA `2775b8452519bc7c06c99c7b96ff5aa55ed96dde28473d77befed91e5e201ffd`。
前表是上一完整场景窗口，本表是最终热路径确认，各自与同时段旧版配对，不跨窗口拼平均值。

## 512 MiB 与双 logical CPU

另在本机 WSL Debian 的原生文件系统上，使用同提交的干净 Linux checkout、同 SHA `2775b845…e201ffd`
脚本和同一 Python 环境，执行 mixed 的旧→新→新→旧。每轮通过独立 cgroup v2 叶子实际设置
`memory.max=536870912`、`memory.swap.max=0`，进程 affinity 为 `[0,1]`；不更改父组或宿主限制。
具体参数、每轮完整 benchmark 与资源 counter 见[四轮材料](../architecture/sqlite-writer-audit-2026-10-22/performance/memory-cap-d23a487d.json)，
复现入口见[资源限制说明](../architecture/sqlite-writer-audit-2026-10-22/performance/cgroup-memory-cap.md)。

每版本 24 个输入，均有成功回执关联，无 workload error；原 SELF/renew 负载相同。
成功回执平均 **289.6→279.6ms（减少 3.4%）**，P95 **457.0→495.3ms**；handler 平均 352.4→332.0ms。
后台提交平均 49.0→65.2ms，renew 10.7→11.2ms。旧版 17 次 BUSY/BUSY_SNAPSHOT 语句错误，新版 0。
本组样本较少，平均改善与尾部退步同时保留，不和 Windows 组拼均值。

各轮 cgroup peak 为 158.5–179.9 MiB，主进程 peak RSS 为 166.8–177.5 MiB，memory max/oom/oom_kill
事件均为 0、swap 为 0，叶子均已移除。实际上限生效，但没有逼近上限；这不是内存耗尽测试。
sidecar 的 CPU、RSS、process I/O 与 cgroup device I/O 包含导入和 fixture，业务计时仍认原 benchmark JSON。

前一组尝试因 Linux Git 无法解析 Windows worktree 的 `.git` 绝对路径，在计时前全部失败，0 个输入。
其中两个新版进程在 metadata 失败后留下了测试 driver 线程，按原 PID 收尾，实际退出 -15；原尝试完整单列，
未混入均值。随后改用同提交的原生 Linux checkout，重新执行完整四轮。测量后将 Git metadata 前移到任何
数据库资源创建之前，schema/seed 纳入原 finally，修复的是探针准备期的资源生命周期，业务计时与完成关联未改。

## 大历史场景

线上调查时业务库约 2.50 GiB。新增可选 `--history-rows` 在计时前批量准备真实 ChatEvent、transport receipt
和 history FTS，保实际索引、FK、trigger 与 durability。数据由 128 种合成讨论正文组成；同一行数、同一模板，
原 `/ai new` generation fence 保留冷历史，活跃输入仍按原参数投递。

校准 8192 行实际分配 103702528 bytes，三类行数均为 8192、checkpoint 成功；新旧小规模真实链路冒烟通过。
正式组预先固定为 **212992 行**，不会按版本分别填到某个大小。合流源码 `2f5f7b0d` 已执行完整
旧→新→新→旧。旧库每轮 2583711744 bytes，新库 2583810048 bytes，约 **2.41 GiB**；实际比线上
调查的 2.51 GiB 略小。三类历史行数各 212992，真实 FK、FTS、索引与触发器保留，checkpoint 后 WAL
为零。合成数据只含一个人物、一个会话和 128 种重复模板，规模接近线上，不代表线上内容和归属分布。

每版本 24 个输入全部对应成功回执，workload error 为零；成功回执平均 **530.8→486.6ms（减少 8.3%）**，
P95 **717.8→837.2ms**，handler 平均 643.0→567.1ms。旧版 10 次 BUSY/BUSY_SNAPSHOT 语句错误，新版零。
准备历史的 179–188 秒不计入业务测量。该组是 metadata fixture，记录源迁移 head 为旧 0147、新 0149，
不冒充对线上数据库执行过迁移。

同一冻结提交还执行 chat、chat-cpu2 各一个完整 ABBA，共计 12 轮、144 个输入，全部来源检查与完成关联通过。
chat 每版本 24 个输入：平均/P95 为 389.5/477.2→393.7/489.7ms；CPU2 为 281.5/340.7→310.4/382.6ms。
这次短样本分别多 4.2ms 和 28.9ms，和前一组 d23 的改善同时保留，不跨时段挑出较快样本。
完整材料见 [合流十二轮](../architecture/sqlite-writer-audit-2026-10-22/performance/combined-2f5f7b0d.json)。
它采用增加 untimed 历史准备后的测量脚本 SHA `4e887c821a1e9ece195c291333d43aa9e27169c66758c40a83256b60a589987b`，
新旧每轮相同，原业务计时和成功回执关联不变。

## 合流后的最终热路径

三处查询精简后的 `48f621f1` 执行 chat、chat-cpu2 各三组 ABBA，24 轮、288 个输入，全部完成。
普通聊天平均/P95 为 372.2/471.4→384.9/440.3ms，双核为 281.7/345.9→297.4/363.6ms。
原材料见 [48f 的二十四轮](../architecture/sqlite-writer-audit-2026-10-22/performance/hot-48f621f1.json)。

随后调整 reader 的原连接关闭顺序，冻结 `8bc4d437` 再执行相同的完整 24 轮、288 个输入。
每场景、每版本 72 个输入，source/import/成功回执关联全部通过，workload 与 SQLite 错误均为零。
不把不同时段的旧样本合到一起；以下只比较最后窗口的新旧：

| 场景 | 旧成功回执平均 / P95（ms） | 新成功回执平均 / P95（ms） | 旧 / 新 handler 平均（ms） |
| --- | ---: | ---: | ---: |
| chat | 379.3 / 467.9 | 381.6 / 471.2 | 464.9 / 444.7 |
| chat-cpu2 | 279.0 / 364.9 | 296.5 / 357.5 | 327.1 / 339.8 |

普通聊天成功回执平均多 **2.3ms（0.6%）**，handler 减少 **20.3ms（4.4%）**。
双核平均仍多 **17.5ms（6.3%）**，P95 减少 **7.4ms**。前一组 d23 的平均改善、48f 的退步以及
本组结果全部保留；最终不能概括为每个条件下都更快。
[最后完整二十四轮](../architecture/sqlite-writer-audit-2026-10-22/performance/reader-8bc4d437.json)沿用同一
`4e887c82…89987b` 测量脚本。后续 `d80b9d8c` 只修独立连接器的 Windows 类型分支，Main 测量的
`src/yuki` 与 `tests` 没有变化；大历史和内存组仍按各自提交报告，不改写成最后提交的测量。

## 改动的实际成本

1. 原连接池每次 checkout 额外探活。原生 SQLite trace 的同五个 context、同五条业务 SELECT、同一条复用
   connection 对照中，`pre_ping=True` 额外产生四条 SELECT 1；删除后为零。这里测的是原生 trace，
   不是 SQLAlchemy Engine event（后者看不到这些探针）。主库没有网络连接的失活探测需求，原显式
   invalidation/连接重建仍保留；不加替代探针或重连循环。
2. writer 取得同一 raw driver 的第二层异步包装删除。owned reader 为正确覆盖首次 connector/configure
   的取消，持有实际 checkout 的完成；这是同一次 checkout，既没有读队列，也没有额外业务 BEGIN。
   任务增减不能直接当端到端胜负，以上完整聊天组已包含实际代价。
3. 扣费时原已有计数 SELECT 与 INSERT 合为同 writer 的 `INSERT … SELECT`，每次删除一次往返；原共享
   root budget、首次实际计数、限额与幂等扣费不变。ordinary 身份/来源的多次点读、Social 来源存在性等
   也已合成保持原条件的 SQL，不能只拿更少 SELECT 推断实际延迟。
4. SELF 原完成窗口对每条 event 重复遍历全部区间，改为排序区间与有序事件一起推进：
   `O(E×R)` → `O(R log R+E+R)`。原 cursor/pending 计数只读实际消费列；writer 用固定形状 json_each
   与主键查询精确核完整原选择及字段，不按 event 数量膨胀 SQL 绑定，也不以 hash/count 代替来源。
5. JSON、hash、历史、分词与文件准备前移；真正当前身份、来源成员、absence、lease、业务 CAS、paid/发送
   receipt 继续保在原子单位。显式 FTS rebuild 和整次隐私清退仍可能占用 writer；不以拆坏业务原子性换数字。
6. 持有 reader 的实际 checkout 后，原来没有 SQL 的空 context 也开始有借还成本。最终复审逐处删除这些
   空路径，包括 Work input、空 Protocol refs、空 initiative 集合、SELF/Dream 与无 scope 查询；只把原纯条件
   移到 context 外，有数据时原 SQL、source 与权限条件保持。完整聊天结果见上节最终确认组。
7. 局部采用 Python 3.12 的 [eager task factory](https://docs.python.org/3.12/library/asyncio-task.html#eager-task-factory)，
   让缓存 checkout、空 flush 等立即完成的操作省去首轮排队与 shield 回调。临时实际调度探针从 4 个 callback
   降到 0；仍创建一个任务，真正 I/O 仍等待原 Future。47 项公共取消/提交/关闭回归及独立 4 项复核通过。
8. 合流后再次删除 journal 每次保存的前置租约 reader，原 writer 首项仍按同一 fence 与 SQL 时间核验；
   媒体 refs 直接沿原 `ON CONFLICT` 插入，不先读取全部旧 refs 做差集；模型前人物资格用已有 predicate
   一次查询原人物与当前归并根的 enabled。没有新增缓存、重试或条件层，原 paid/来源/隐私校验不变。
   六文件定向首次 77 passed、1 项旧入口计数断言失败；修正该断言后所在文件 18 passed，原 checkpoint、
   模型次数和预算断言全部保留。独立复核未发现身份、媒体追加或 unknown 重放缺口，收益按后续冻结组另记。
9. reader 已持有的原 AsyncConnection 先关闭，再结束原 Session，让 SQLAlchemy 在原 root transaction
   尚挂接时识别已完成的 rollback。真实 worker 探针中，七种 reader 场景每次 native rollback 排队从
   2 次降为 1 次；有事务时两版都只有一条真实 ROLLBACK，脏 ORM、嵌套、异常、重复取消和连接复用均通过。
   默认 pool reset 继续保留：真实 deferred-FK COMMIT 失败证明 writer 仍需要它完成清理。
   [原始探针材料](../architecture/sqlite-writer-audit-2026-10-22/performance/reader-close-native.json)区分 worker
   排队和实际 SQL；[SQLAlchemy 官方说明](https://docs.sqlalchemy.org/en/20/core/pooling.html#reset-on-return)
   说明 Connection 与 Pool 的 rollback 分工。该修改后公共生命周期、真实取消/ACK、离线 owner 和
   Automation 共 47 项通过，原 physical_exit 等待、首取消及 Session 清理全部保留。

## 如何重跑

在已同步本分支依赖的环境，两个源码 checkout 使用同一个脚本，例如：

```powershell
uv run --no-sync python scripts/benchmark_sqlite_writer.py `
  --checkout C:/path/to/baseline --scenario chat --messages 12 --interval 1 `
  --output D:/Temp/sqlite-old.json --scratch D:/Temp/sqlite-paired
uv run --no-sync python scripts/benchmark_sqlite_writer.py `
  --checkout C:/path/to/new --scenario chat --messages 12 --interval 1 `
  --output D:/Temp/sqlite-new.json --scratch D:/Temp/sqlite-paired
```

按旧、新、新、旧重复完整参数，不拿不同时段的一条旧样本与多条新样本比较。
规模场景用 `--scale 128`，尾部 `--scenario tail --tail 1024`，混跑加 `--background 12`，
CPU2 加 `--cpu-cores 2`。原 JSON 中保参数、源路径/commit、导入检查、每输入成功回执关联、SQL/错误与计时摘要。

## 测量边界

Engine SQL 计数不包括原生探活、SQLite 隐式 BEGIN 或 trigger 内部全部工作。entry wait 包 checkout/BEGIN，
explicit writer scope 的新旧覆盖不同；acknowledged-write interval 新版起于 BEGIN 确认后，旧版可能起于
第一条 DML 确认后，都是实际持锁的下界，不能报告成完整持锁时长的直接倍数。

事件循环只用 20Hz 观察线程投递 callback 的 dispatch delay；它不含 GIL 阻止观察线程投递的时间，
不是连续最大 stall。Windows process CPU time 较粗，少量样本不能据此宣称 CPU 使用率的确定变化。

以上是本机合成负载，不能当生产延迟承诺或真实网络模型 benchmark。早期 dirty 树与旧完成关联方法的结果
只用于定位，包括早期约 34ms 的单批差距；最终结果按固定源码、相同完成口径重新完整交叉测量。
