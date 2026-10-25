# Memory Vec1 与 Dream 算法实施记录

日期：2026-10-26。范围：A07/A08 的业务实现、0153 迁移与本地验证。尚未经过合流 CI、PR、合并或生产验收；固定原生扩展的跨平台构建和发行文件由集成分支一起交付。

## 改动

- 原 Database 物理连接、Alembic 和启动 schema 检查都先装载包内 Vec1，不在运行时编译或换存储。
- 0153 将原 BLOB 原样搬到各 profile 的 Vec1 表，保留 embedding id、fact/profile 外键、hash、时间和原任务，删除普通表旧列。升级不购买模型调用。
- 成功 claim 后的元数据和向量共用原 writer 事务；原 FK 级联和删除触发器覆盖事实删除、遗忘和 purge-old。Dream、质量审计、hygiene 和普通 reconcile 共用新的实际向量所有者。
- 检索在同一显式读取快照下用当前授权 fact ID 的 metadata IN 过滤原生候选，随后只读取候选正文和向量做原 hash/codec/fsum 精排。正式选 flat cosine NN，不训练或按数量暗切模式。
- `semantic_approximate` 与真实 `partial_failure` 分开。有限原生候选不能保证原 binary64 全局 top-k；正常候选截断不被标成基础设施故障。候选外 hash 新鲜度不冒充已查证。
- Dream 每个 pair 仍算一次原分数，只保阈值以上双向权重。complete-link、seed/score tie、changed 过滤和 singleton 均保留；degree 为零的尾段一次处理。候选载入时验证原 norm 并复用归一化，不在规划阶段重复计算。
- 浮点乘法改用 `map(mul,...)` 交给原 fsum；不换结合顺序、不引入 ANN Dream 或事实数量上限。

## 本地证据

环境为 Windows Python 3.12、SQLite 3.53.1，固定 Vec1 0.8 的上游 SCALAR/AVX2 构建，本机命中 AVX2。没有生产写入、真实聊天材料、Embedding API 调用或 QQ 消息。

1511 条合成随机 1024 维向量，k=50、原生候选200：

| 管线 | 查询中位 | Python 分配峰值 |
| --- | ---: | ---: |
| 原全量 SQL/BLOB/hash/decode/normalize/fsum | 440.94 ms | 7,509,845 B |
| 当前 Repository 授权 SQL/Vec1/候选原值精排 | 63.02 ms | 1,495,727 B |

8 个查询的返回 ID 和分数全部一致。旧对照未包含原 active_count 查询；没有给新版虚加省掉它的收益。这是本机合成管线测试，不是线上七倍，不包含远程 query embedding、FTS、RRF 或完整聊天延迟；tracemalloc 也不是 native RSS。

另一组12查询的模式对照中，原生 NN+精排中位24.72 ms、recall@50=1；IVF16桶/nprobe25% 为25.76 ms、平均召回0.348，PQ32为23.87 ms/0.303。随机高维数据不代表真实语义分布，但不足以证明当前规模值得引入训练模式，因此正式使用 flat NN。未压缩 IVF 和 PQ32 下全部原 BLOB 逐字节读回不变。

可移植原件：[实际管线](memory-vec1-pipeline-benchmark-20261026.json)、[模式对照](memory-vec1-mode-benchmark-20261026.json)。本机可复现脚本保留于 `D:/Code/Reports/Yuki/ann-20261026/`。

## 验证范围

相关回归覆盖原 claim/late complete/ack 丢失、profile 权限与级联回滚、Dream/full/incremental、Memory 身份与当前 SQL 授权、FTS 降级、质量审计、hygiene，以及启动和迁移取消。

新增真实 native 回归覆盖 metadata IN 的实际查询计划和 top-k 前过滤、共同群撤销无需重建向量、缺失原值的审计与排队、reader/writer 取消等真实 driver 结束、0153 中断回滚/重开/原始字节/外键级联、原 norm 与 float.hex。Dream 稀疏图另与原 dense-pair 算法做240组完整分组对照，含负阈值、同分和 degree=0 尾段。

本地空库完整升级0048→0153及 canonical schema 检查通过。最终同批147项相关回归通过；Ruff和相关32个源码文件的严格类型检查通过。合流结果仍以最终PR记录为准，本文不把局部通过冒充完整发行验收。

## 部署顺序

先停止旧 writer、备份原数据库，用包含固定平台扩展的新构建运行 `alembic upgrade head`，再启动新 Bot。所有物理连接需要同一扩展；不能先删普通表列却运行未带 Vec1 的旧进程。正常启动不做全库训练或自动重建。

0153 不支持 `--sql` 数据搬迁或将活库降回旧格式。生产切换仍由原 Bot/Manager/helper 同源部署流程处理。本记录没有声称这一步已经上线。
