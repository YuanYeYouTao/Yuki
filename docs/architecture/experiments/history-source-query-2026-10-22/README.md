# 来源定位与单游标查询的隔离实测

日期：2026-10-22。本目录归档成功运行的数据库均由探针创建于临时目录，运行后删除；没有读取生产聊天、
调用模型、访问平台或发送测试消息。这些数据验证查询实现，不冒充线上延迟验收或普遍最快。

## 环境与复现

Linux 开发容器 `yuki448-dev-20261022`，Python 3.12、SQLite **3.46.1**，与本次调查时
生产 SQLite 版本一致。容器原 `/work` 挂载属于旧任务，实测使用当前分支复制到的
`/tmp/yuki-history-query-impl1`，解释器为 `/opt/yuki-448-venv/bin/python`；补充的WAL探针使用
`/tmp/yuki-history-query-impl3` 与 `/opt/yuki-history-final-venv/bin/python`。
JSON 保存实际源文件 SHA-256 和版本；不把旧挂载、Windows SQLite 3.53.1 或不同快照混为一组。
容器与其他验证共享 CPU/磁盘，未做资源隔离；耗时不设固定通过线。

在当前仓库根目录、已安装项目及测试依赖的 Linux 环境执行：

```sh
PYTHONPATH=src:. python docs/architecture/experiments/history-source-query-2026-10-22/source_probe.py --entries 20000 --threads 8 --out docs/architecture/experiments/history-source-query-2026-10-22/source-results-20000.json
PYTHONPATH=src:. python docs/architecture/experiments/history-source-query-2026-10-22/source_probe.py --entries 80000 --threads 8 --out docs/architecture/experiments/history-source-query-2026-10-22/source-results-80000.json
PYTHONPATH=src:. python docs/architecture/experiments/history-source-query-2026-10-22/query_stream_probe.py --rows 5000 --rows 20000 --out docs/architecture/experiments/history-source-query-2026-10-22/query-stream-results.json
PYTHONPATH=src:. python docs/architecture/experiments/history-source-query-2026-10-22/media_predicate_probe.py --out docs/architecture/experiments/history-source-query-2026-10-22/media-predicate-results.json
PYTHONPATH=src:. python docs/architecture/experiments/history-source-query-2026-10-22/wal_probe.py --out docs/architecture/experiments/history-source-query-2026-10-22/wal-results.json
```

## 真实来源 SQL 与普通索引

`source_probe.py` 从空库执行正式迁移至 **0148**，直接生成受原外键和阶段约束保护的
Person、Account、Assignment、Space、Binding、Conversation 和别名。没有聊天正文；
每个 Binding 有八个 thread，会话一半私聊、一半群聊，QQ/TG 各半。
使用产品 `history_source_statement`，分别测最小范围投影和完整目录展示投影。

“之前”只在同一隔离库删除0148新增的三个加速索引，其余结构、事实和 SQL 相同；测量使用
新连接，避免 DDL 前缓存的 EXPLAIN 结果。每项七次，首样本单列，表中为后六次中位数。
首样本也不是磁盘冷缓存：数据库刚装载，操作系统页缓存已经受前序工作影响。

| 实际范围查询 | 来源总数 | 删除新索引 ms | 使用新索引 ms | 近似 VM：之前→之后 | 返回会话 |
| --- | ---: | ---: | ---: | ---: | ---: |
| Space，最小范围 | 20,000 | 2.983 | 0.011 | 80,400→400 | 8 |
| Space，最小范围 | 80,000 | 10.903 | 0.011 | 320,400→400 | 8 |
| 私聊 Binding，最小范围 | 20,000 | 3.138 | 0.013 | 120,400→500 | 8 |
| 私聊 Binding，最小范围 | 80,000 | 11.902 | 0.013 | 480,400→500 | 8 |
| 群 Binding，最小范围 | 20,000 | 3.105 | 0.013 | 120,400→500 | 8 |
| 群 Binding，最小范围 | 80,000 | 11.524 | 0.013 | 480,400→500 | 8 |

计划显示 Space 使用 `(space_id,id)` seek；Binding OR 使用两个原 Binding 字段的索引
seek，再只排序匹配结果。关联的 Assignment、Account、SpaceBinding 等按主键 SEARCH，
没有因为共享展示查询出现笛卡尔积。精确 Conversation ID 仍使用原主键，暖样本约0.005ms。

平台全范围仍需要确定来源集合：80,000个来源中取40,000个TG来源，最小范围投影约101ms；
新增索引未改变这条扫描计划。只取目录前21项可早停，本次约0.14ms。不能把平台全范围
包装成 O(log N)，也不能把来源 SQL 的单独耗时说成完整工具调用耗时。

该组近似 VM 每100条指令回调计数，只用于解释规模变化，不是物理页读取数或复杂度硬上界。
正式迁移及实际产品 SQL 另在 Linux 3.46.1 上通过
`tests/integration/test_history_scope_indexes.py` 的7项验证：旧数据、原 schema、外键和
多入口/thread 保持不变，测试建表与迁移的索引一致。

## 候选流、精核与装配

`query_stream_probe.py` 使用完整产品测试 schema、四个真实 Conversation、原生 FTS 和
媒体索引。合成正文重复出现 `abc ... bcd`，故搜 `abcd` 会产生全部 gram 假阳性；最早一条
另含稀有词。每条保存4KiB附件 JSON，用来区分“取到原行”和“构造完整事件”的成本。

每种条件分别启动新子进程。样本0包含该进程第一次连接及 OpenCC 初始化；后两次是
同进程暖查询，操作系统缓存没有清空。JSON记录完整调用、SQL数量、fetch次数、精核行数、
完整 EventRecord 数、EQP、开流/首fetch前的工作量及 Linux VmRSS/VmHWM。
VM采样不包含FTS内部全部posting/页工作，VmHWM也不是本次查询的独占内存。

`query-stream-before-topn-results.json` 保留首次实现的真实读数及源码hash，含宽媒体IN和
无关键字路径尚待细化的阶段；后续产品修订不覆盖这份对照。完整假阳性和仅排除条件都必须
耗尽合法候选，不能把取消、截断或扫描帽冒充零结果。

最终候选数据在 `query-stream-results.json`，对应 `event_repository.py` SHA-256
`0bfecbfb5cab7e6ef5c814a522cb7159a693eab991554e539ee2fd128eec8a7f`。
表中为各子进程后两次查询中位数，包含观测钩子的开销：

| 条件 | 5,000条 ms | 20,000条 ms | 20,000条精核/完整事件 | 候选SELECT |
| --- | ---: | ---: | ---: | ---: |
| 无文字条件近期页 | 3.70 | 4.02 | 0 / 21 | 1 |
| 稀有词 | 4.19 | 3.57 | 1 / 1 | 1 |
| 稀有词＋宽日期 | 4.52 | 6.23 | 1 / 1 | 1 |
| 稀有词＋宽日期＋图片 | 4.73 | 5.05 | 1 / 1 | 1 |
| 常见词，取20条 | 4.79 | 4.74 | 21 / 21 | 1 |
| 全部gram假阳性 | 115.09 | 440.00 | 20,000 / 0 | 1 |
| 仅排除词 | 138.57 | 532.60 | 20,004 / 4 | 1 |

无文字条件SQL行本身就是命中，可以保留SQL `LIMIT N+1`，避免为了共用流式循环而失去
SQLite top-N 优化；正词或排除词仍由Python精核，不用候选LIMIT假冒完整页。
仅排除条件剩下的4条是准备语料用的真实template消息，另外20,000条均被排除。

首进程查询在本组约90–147ms；不能用暖样本承诺冷启动。20,000条的仅排除条件首fetch前
约121ms，其计划含临时排序：流式分块限制应用层取回量，不保证引擎首次返回前不做工作。
全部假阳性仍是线性精核，但不再为每个假候选解析4KiB附件和构造完整事件；该样本
VmHWM约119MiB，不能由此外推完整Bot/其他worker内存或任何负载下的上界。

## 媒体条件的实际SQL取舍

`media_predicate_probe.py` 在20,000条语料上分别保留密集图片和仅一条图片，使用实际仓库，
通过显式 predicates 对比一次媒体IN集合与按原事件索引核实的EXISTS。四次样本中取后
三次中位数；正文与媒体条件同时生效，不通过改limit或截断结果缩短时间。

| 图片分布/文字 | IN ms | EXISTS ms |
| --- | ---: | ---: |
| 密集/稀有词 | 15.31 | 3.25 |
| 密集/常见词 | 15.63 | 5.06 |
| 稀疏/稀有词 | 3.54 | 4.07 |
| 稀疏/常见词 | 35.13 | 37.24 |
| 稀疏/无文字 | 2.40 | 44.54 |

因此实现保留两个由真实查询能力决定的形状：有正文字候选时，用相关EXISTS核该事件的
媒体，不先装载整个媒体集合；没有正词倒排候选时，保留媒体IN让SQLite从真实媒体候选
定位消息。不保存统计、密度阈值或查询计划状态，也没有宣称这个固定取舍在所有分布都
最快。尤其常见词与稀疏媒体仍有倒排遍历成本，不能只汇报稀有词的最好样本。

## 调用者读快照与 WAL：极小行为探针

`wal_probe.py` 在真迁移至0148的隔离库中建立8个Conversation，执行实际
`history_source_statement`，显式分块读到一个来源后关闭游标。另一个
`Database.immediate_session` 提交该来源名称更新，再观察原读快照和独立连接的
`wal_checkpoint(PASSIVE)`。这是一次很小的事务行为验证，**不是负载、锁等待或WAL增长率benchmark**。

SQLite 3.46.1、页大小4096字节的实际结果如下：

| 阶段 | 读事务仍活动 | checkpoint `(busy,log,checkpointed)` | WAL字节 |
| --- | --- | --- | ---: |
| 初始TRUNCATE | 否 | `(0,0,0)` | 0 |
| 已关游标、另一writer已提交、原owner仍持快照 | 是 | `(0,1,0)` | 4,152 |
| 原owner结束自己的事务后PASSIVE | 否 | `(0,1,1)` | 4,152 |
| 最后TRUNCATE | 否 | `(0,0,0)` | 0 |

游标关闭后，原事务再次执行SQL仍读到旧名称 `Group 3`；新读者已读到writer提交的新名称。
因此writer成功不证明旧reader退出，`busy=0`也不证明所有WAL帧已checkpoint。
owner结束后checkpoint可以推进，但PASSIVE并不因此把WAL文件物理缩为0。
最后的TRUNCATE仅用于这个隔离探针核对文件，不加入产品的每次查询路径。

结果见 `wal-results.json`，含真实源文件hash及 `temporary_database_removed=true`。
该轮没有重跑大语料测量、增加产品状态或触及此前被执行审查拦截的本地临时路径。
