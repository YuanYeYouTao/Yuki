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
CPU2 chat 平均多 25.2ms，mixed 多 52.2ms；最终空路径删除后会追加确认组，原四轮数据保留，不用后续
最快一轮替换本表。

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
   移到 context 外，有数据时原 SQL、source 与权限条件保持。其后确认测量另记。

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
