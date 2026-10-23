# 全仓测试精简记录（2026-10-23）

按用户追加要求审查旧测试及本轮新增测试，基线为 `1cff2d50`。与 B1–B6 源码实施分开提交，
不修改产品源码或主库迁移。总交付见[代码清理执行记录](code-deletion-20261023.md)。

## 审查与实际变化

清单覆盖根目录、Connector 和订阅插件的 **498 个测试文件**。逐文件检查测试定义、参数轴、
断言及实际调用；对拟删除、合并的候选读取完整代码并核对保留的消费路径。未宣称逐行阅读
所有未变 fixture。四个分区的完整清单、原节点与合并去向保存在本地 `.cache/deletion-all/`
的 `test-audit-*-results.json`、`test-audit-summary.json` 及 retirement map 中。

| 变化 | 实数 |
| --- | ---: |
| 保留的测试文件 | 351 |
| 精简的测试文件 | 138 |
| 删除的测试文件 | 9 |
| 测试文件净减少 | 4,163 行 |
| 删除无消费者 VM 计量 helper | 41 行 |
| 合计净减少 | **4,204 行** |

删除和合并实现镜像、mock 后恒成立的断言、冻结迁移与当前私有函数相等、SQL 文本/查询计划
布局、精确私有调用次数及 VM 倍数门槛；缩减无交互的笛卡尔矩阵和重复环境。
成本观察留作诊断证据。没有通过增加 skip、放宽产品授权或改变执行状态来通过测试。

根测试集实际收集由 **4,658 → 4,409**：unit `3663 → 3442`，integration `901 → 873`，
Native **94 → 94**。Connector `36 → 35`，订阅插件 `30 → 27`；均为 pytest 节点数，
不是函数数，也不代表执行通过。最终结果以新提交的 Quality 和独立 Linux 全量为准。

## 整文件删除与保留覆盖

| 删除文件 | 去向或理由 |
| --- | --- |
| `test_participation_foreground_cost.py` | 退出重复 SQL/时间采样；prepared record/runtime 路径并入 `test_ordinary_preparation_cost.py`，实际 hint、quiet、冷恢复仍由原行为测试覆盖。 |
| `test_projection_capacity_cost.py` | 退出 VM 比例对照；保 `test_compact_checkpoint_regressions.py` 的真实 count/byte 驱逐和 `test_projection_chunk_storage.py` 的原件/前缀/引用行为。 |
| `test_work_context_note_metadata.py` | 合入 `test_work_settlement_writer.py` 的实际 task_control：version 2、空正文、重复引用、未知可选字段不拒绝、原私有 Work 与非零预算。 |
| `test_work_scheduler_scope_selection.py` | 退出 SQL/VM 增长计量；保实际 busy/deferred cursor、末槽公平选择及 scope writer 竞争。 |
| `test_identity_point_reads.py` | 退出四类点读成本矩阵；实际账号观察、canonical ingress、Person/路由/Presence 行为保留。 |
| `test_codemode_child_query_cost.py` | 退出 SQL 计划和 VM 镜像；CodeMode 原 child 恢复、authority、history source 与不重派由真实执行用例覆盖。 |
| `test_client_projection_costs.py` | 退出投影成本矩阵；ClientFiles 原件/授权和 ClientViews 私有视图的真实接口仍验证。 |
| `test_database_fixture_isolation.py` | 退出测试 fixture 自检；实际 Database writer、事务隔离、取消和 owner 生命周期测试保留。 |
| `test_workspace_publication_cost.py` | 退出 VM 倍数门槛；实际旧库、manifest/文件发布、CAS、引用、原件和 rollback 继续验证。 |

`tests/support/sqlite_cost.py` 在最后一个纯 VM 探针退出后没有消费者，一并删除。

保留了原身份/执行 ID、历史回执与 unknown 不重放、真实 FIFO 撤权、原证据、事务回滚、
不可变前缀/发布、物理资源关闭与重复取消，以及实际旧 paid 格式恢复的独立反例。

## 验证边界

精简后全仓 ruff、Linux-target mypy（659 源文件）及根集收集通过。各分区定向已运行，
日志单独保留；最终新提交全量运行后在 PR 中记录准确通过/跳过数。
Windows 媒体路径的 POSIX/路径问题单独查证，不能以窗口环境失败冒充 Linux 通过，
也不为此向产品添加 fallback。没有运行真实模型、订阅端点、用户 Agent 任务或 QQ 消息。
