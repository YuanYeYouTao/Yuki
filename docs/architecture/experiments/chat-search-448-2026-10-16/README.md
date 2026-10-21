# #448 聊天记录检索：旧设计实验档案

**历史资料，不是当前实现或本版验收。** 本目录来自 `codex/issue-448-chat-search-taskbook` 的 `50d55bfbf95046672663ebe5fd44262cc04cc7d5`，对应已被替换的旧任务书。2026-10-21 修订本说明，并仅调整旧脚本的一处导入排序以通过现行 Ruff；实验结果未修改，旧探针未重跑。

现行计划见 [#448 任务书](../../Yuki-448聊天记录检索增强任务书-2026-10-16.md)。其中的统一工具、人物族、账号归属、实时分页和匹配定义均不能由本目录的旧实验替代验证；旧 HMAC、计价、frontier、排名分组和重启方案也不再是施工要求。

## 原始文件与环境

| 文件 | 当时用途 |
| --- | --- |
| `chat_search_probe.py` | 合成资料和最小表上的机制探针；包含旧包 `qq_ai_bot`、旧投影触发器和旧设计的假设。 |
| `results.json` | 保留的实验输出，含旧基线及 E7/E14/E15 的历史补测；不是本次审查生成的数据。 |

结果记录环境为 Apple M4、macOS、Python 3.12.7、SQLite 3.45.3；旧说明记录 `unicodedata` 15.0.0、SQLAlchemy 2.0.51、aiosqlite 0.22.1。主要规模为 20 万条合成消息、400 个查询、30 次计时重复，不能据此承诺当前 4.0.0 的耗时或锁等待。

只在原提交及其依赖环境中复现旧实验；不要在当前 main 安装旧包、执行旧触发器或把脚本接入 CI。若要评估现行方案，应针对当前 schema 和匹配定义另做离线测量，不覆写这份历史输出。

## 实验结果索引

| 旧编号 | `results.json` 键 | 当时检查的机制 |
| --- | --- | --- |
| E1 | `trigram_semantics` | 旧 trigram、短词和字面匹配行为。 |
| E2 | `index_options`、`highlight_examples`、`v0_invariant` | 合成数据上的索引、偏移与候选完整性。 |
| E3 | `time_binding` | 特定旧类型和参数路径的时间绑定。 |
| E4 | `contentless_delete_operations`、`enqueue_write_lock` | 最小表的操作与批量入队持锁。 |
| E5 | `bm25` | 旧排名方向及无关插入影响。 |
| E6 | `grouping` | 已撤下的跨消息证据组方案。 |
| E7 | `second_draft_scan_no_progress`、`paging_engine` | Python 内分页计价与推进行为。 |
| E8 | `queue` | 最小表上的队列与源行竞争。 |
| E9 | `snapshot` | 旧仓库 Database 配置下的读快照。 |
| E10 | `normalization` | 旧正规化与高亮映射定义。 |
| E11 | `relevance_revision` | 旧投影修订触发器。 |
| E12 | `lifecycle` | 最小表的生命周期模拟。 |
| E13 | `plugin` | Python 内 Plugin 分页模拟。 |
| E14 | `relevance_recompute_cost` | 最小 SQL 表的排名范围与预算重算。 |
| E15 | `cursor_integrity` | 合成授权 fixture 和 HMAC codec；不是真实身份/权限整合。 |

## 解释边界

- T2S 仅有 12 字样表，不能证明完整繁简支持。旧 fold、高亮和跨消息匹配也不是本版匹配定义。
- E7/E13 未经过 SQLite；模拟计价和返回行数不能证明物理扫描、延迟或写锁上界。
- E8/E12/E14 使用最小表；E14 时间跨度全部设为 0，未覆盖真实时间排序、取消或运行时权限。
- E15 的权限、版本和查询绑定由合成 fixture 提供；不能证明当前 Person/Assignment、撤权、持久回执或 Plugin 在持续更新中能终止。
- 源行仍存在的回写检查不能覆盖原地隐私脱敏；这些数据也未证明搜索结果在 journal、artifact、协议资料中的清退。
- 合成分布、局部抽样和本机耗时不能当真实群聊或当前生产验收。原始旧任务书的章节号与详细解释请查上述提交的 Git 历史。
