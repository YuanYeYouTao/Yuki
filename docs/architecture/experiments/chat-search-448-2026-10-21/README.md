# #448 全链路查询与分页审查证据（2026-10-21）

这是本轮新做的离线实验，不是 10-16 的旧探针，也不是产品实现。源码基线为 main `62bbacec`，任务书前版提交 `31507101`。未访问真实聊天、生产数据库或外部模型。

## 两组实验

| 组 | 文件 | 实际覆盖 |
| --- | --- | --- |
| A：相关 DDL / 查询形状 | `query_probe.py`、`id_probe.py`、`around_probe.py`、`metadata_probe.py`、`author_dense_probe.py` 与对应 JSON | Python 3.12.13、SQLite 3.53.1；50k/200k/400k 合成事件，100 个交错 Conversation；另有所有事件集中一个 Conversation 的作者测试。 |
| B：完整 gram 的候选局部性 | `scoped_grams_probe.py` / `scoped_grams_results.json`、`scope_and_probe.py` / `scope_and_probe.json` | Python 3.12.14、SQLite 3.53.1，内存库；目标 Conversation 恒定 200 条，其他 Conversation 从 99 增至 999。 |
| B：离线切换机制 | `offline_switch_probe.py` / `offline_switch_probe.json` | FTS 重命名、shadow 对象及缺表触发器的最小机制实验。 |

10-22 任务书终审已删除强制影子交换：全程停写时直接装载最终命名 FTS，完成后记录最终迁移版本即可。离线切换探针仅保原机制证据，不是实施要求；本轮未改脚本或重跑旧结果。

A 使用当前相关列、索引和查询谓词，省略无关列、CHECK/FK 及应用写触发器；不是完整生产 schema。A 的 scope gram 仅保存探针词，不用于估算完整索引大小或写成本。B 生成其合成正文的完整 1/2/3-gram，但没有接入真实 ASR/OpenCC、授权或同步 writer。

## 主要结果

### 查询形状：VDBE 工作量

| 形状 | 50k | 200k | 400k |
| --- | ---: | ---: | ---: |
| 当前 global trigram＋Conversation＋时间排序 | 约 1,006,800 | 约 4,032,600 | 约 8,067,100 |
| scope 前缀 gram＋rowid 顺序取 21 条 | 约 300 | 约 300 | 约 300 |
| 同一候选改为 occurred_at 排序 | 约 11,000 | 约 44,000 | 约 88,000 |
| 既有单列 Conversation 索引，ID 首页/深页 | 约 200 | 约 200 | 约 200 |

计数粒度为 100 条指令。**VDBE 不覆盖 FTS 虚表内部的全部 doclist/页访问，不能用 300 推导全文物理工作上界。** 这里证明排序和 SQL 回表形状差异，不证明线上固定延迟。

`around_results.json` 的逐指令计数还显示：400k 总量、4k 目标会话，后侧 `id>floor AND id>anchor` 的最新锚点无结果是 20,018 步，合成 `id>max(floor,anchor)` 后是 17 步；中部取六条是 10,443→102 步。两者 EQP 都可能只显示 `rowid>?`。此探针固定既有 Conversation 索引以隔离重复边界，目标运行镜像仍须验证。

作者族：同一大 Conversation、4 个作者，普通 IN＋作者索引在三个规模均为 458 步；每作者 top-N UNION 约 2054 步。不能只因 EQP 显示 TEMP B-TREE 就断言全量排序、另造合并器。

日期：400k/目标4k，早期十秒窗按 ID 返回，强制 Conversation 索引 51,235 步，日期范围索引 497 步；不存在日期分别 52,018 / 20 步。媒体复用现有索引取自身图片的 21 个候选为 585 步。以上精确计数见 `metadata_results.json`。

### 固定目标会话，增加无关会话

“仅外域”在目标没有出现：

| 候选方案 | 总 20k 中位 ms | 总 200k 中位 ms | 200k 索引字节 | 构建秒 |
| --- | ---: | ---: | ---: | ---: |
| global gram，回表再筛 scope | 2.6434 | 29.5814 | 约 17.44 MB | 约 6.27 |
| global gram＋独立 scope token AND | 0.3096 | 1.32605 | 约 18.58 MB | 约 5.64 |
| 每 gram 带 scope 前缀 | 0.00835 | 0.0105 | 约 65.52 MB | 约 11.97 |

表中 MB 使用原结果字节除以 1,000,000，取约数；不是生产容量估计。独立 scope token 已能避免跨会话回表，不能说它必然线性扫描全部 posting；前缀方案进一步减少全局交集工作，代价是本样本约 3.5 倍索引和约两倍构建成本。

候选计数、账本谓词调用计数分别执行；计数 UDF 不计入耗时，也不统计隐藏的 FTS 内部工作。计时 20 次、使用已热内存库，不能外推磁盘、WAL、writer 等待或真实回复速度。

## 重现与边界

把本目录的 `.py` 复制到一个新的空临时目录，使用支持 FTS5 与 contentless-delete 的 Python/SQLite。A 依次运行 `python query_probe.py 50000 200000 400000`、`python id_probe.py`、`around_probe.py`、`metadata_probe.py`、`author_dense_probe.py`；会生成约 2 GB 的独立合成库，输出仍在该临时目录，不提交数据库文件。脚本不是幂等维护程序，重新实验使用另一个空目录。

B 先运行 `scoped_grams_probe.py --out scoped_grams_results.json`，再运行 `scope_and_probe.py`；离线切换探针可单独运行。全部只依赖标准库，不导入 Yuki，不连接生产。归档时按现行 Ruff 整理排版、导入与同步回调局部绑定，SQL、数据生成和测量口径保留；JSON 为实际实验输出，未因格式整理重测。现有质量 CI 检查脚本静态规范，不新增 CI 大规模实验任务。

- 原始环境、SQL、参数、计划和计数保留在 JSON；A 的汇总为 `all_results.json` / `all_id_results.json`，不是仅保一张结论表。
- 没有测完整真实文本的词元量、OpenCC、首次入口准备、物理取消、并发写锁或生产磁盘。
- 删除后 MATCH、rowid 和 vocab 不再返回旧行，不等于 SQLite 内部字节完成取证级擦除；不由此新增每次查询 VACUUM/巡库。
- “缺表时引用它的触发器会报错”来自最小机制探针，不是要求支持任意破坏 schema。正式虚表、触发器与升级必须按同一数据结构维护。
- 实施验收须在实际部署镜像、真实领域入口与固定公开语义上补足；不冻结这里的精确 VM 数字或本机毫秒数为产品门槛。
