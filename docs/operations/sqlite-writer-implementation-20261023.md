# SQLite统一写入实施记录

源码起点：`c2ce7da66ab54efe192993b9615df48266a62137`。分支
`codex/sqlite-writer-runtime-20261023`。完整范围认[任务书](../architecture/Yuki-SQLite统一写入与短事务重构任务书-2026-10-22.md)。

状态：首轮产品实现与调用方迁移完成，进入全局验证及实现后的三轮交叉复审。未提PR、未合并、未部署。

## 分工

- 主审：Database/driver、optional_session、CommitHints、SQLite diagnostics、装配/健康/生命周期、
  tool_results/Codemode、Automation全链、Self repository、全局测试与性能、共同文档、最终PR。
- A：[身份/入账/控制面实施](sqlite-writer-implementation-a-20261023.md)，包含People、route、
  canonical/projection、App/配置和独立库，Automation已交主审。
- B：[Memory实施](sqlite-writer-implementation-b-20261023.md)，包含mutation/Dream/Rebuild/
  Embedding/quality、Self control与quiet调度合流；Self initiative由A完成，repository由主审完成。
- C：[Work/Social/插件/Services实施](sqlite-writer-implementation-c-20261023.md)。

## 公共入口当前变化

Database拥有FIFO准入，writer在checkout/BEGIN之前等待；read_session不排队、不自动commit，
只对自己创建的reader关闭autoflush。optional_session复用供给session，owned write进入统一入口。
正常连接池继续复用，业务仍在原协程执行。公开sessions别名、apply_snapshot_write、Memory apply_evidence_write及其first-write重试机制已删除。

aiosqlite 0.22.1的stop返回实际worker退出future，SQLAlchemy强制退出会丢掉该future。
SQLiteConnection保留同一次close-and-stop完成，公共writer等其结束；正常写不关闭连接。
SQLAlchemy flush取消还可能被内部rollback错误替代，公共边界保原异常链中的真实取消。

错误分类保原异常，使用实际phase、rollback/commit确认，公开committed三态与uncertain/
retryable查询。未知不因BUSY代码变成未提交。根commit成功且owner释放后同步发布原hint，
删HintSession；live span通知保留。SQLiteDiagnostics删holders/first-write/物理ID和全部
观察用dialect/raw_connection包装，只保SQL与公共owner阶段的内存计时。

健康分别说明读可用和writer是否接纳。大生成JSON清单可由基线源码重建，不进入PR；
保审查结论、生成脚本和材料校验记录。

## 已执行的公共验证

`tests/unit/test_database_writer.py --confcutdir=tests/unit`：14项通过，文件WAL真实worker
barrier覆盖FIFO/排队取消、并行读、autoflush、根提交hint、BEGIN/SQL/自动flush/flush/commit
取消及重复取消、BEGIN外部BUSY、commit确认丢失、不同文件独立和正常连接复用。
另覆盖rollback失败后等待真实物理关闭、物理关闭失败停止准入、shutdown重复取消；不代表项目全量通过。

全仓Ruff及mypy src --platform linux通过（648个源码文件；Windows原生平台缺POSIX接口，目标运行平台为Linux）。C首批147项定向回归
及B/A各自结果见其报告；不累计为尚未运行的全仓验收。

## 剩余交付

- 实现后全范围复审，继续修发现的语义缺口和多余机制；全仓旧factory已迁，legacy产品入口已删。
- 公共关闭/rollback与driver关闭失败验证、剩余测试fixture/API迁移。
- Schema及现行合同一致性、同负载基线/新实现性能对照。
- 实现完成后至少三轮全范围交叉复审；修正实际发现后再查，不以次数替代覆盖。
- 全局质量/真实行为验证、提交和PR；CI/合并/部署分别报告，本次只授权提PR。
