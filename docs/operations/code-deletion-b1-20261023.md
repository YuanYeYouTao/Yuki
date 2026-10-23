# B1 代码删除与清理实施记录（2026-10-23）

执行《Yuki 代码删除与清理任务书（2026-10-23）》的 B1，源码基线为
`0aa3e08bd3b5a78677b7831c7a22301966526adf`，分支为
`codex/code-deletion-b1-20261023`。B2–B6 和独立设计项未纳入本批。

## 删除范围

| 任务 | 实施结果 |
| --- | --- |
| F24 | 删除两层 `mark_injected` 写入口及 MemoryContext 未使用的 FactService 依赖，移除所有构造传参。历史 `last_injected_at` 列、投影和审计读取保留。 |
| F25 | 删除无人调用的 rebuild start/cancel core；现役类方法、原 run 与取消收拢、付费提取结果及恢复路径保留。 |
| F26 | 删除无人调用的 Dream `defer_pending` 整 run 跳过策略；现役 cluster 预算处理、历史 `budget_deferred` reader/statistics 和付费结果收尾保留。 |
| F27 | 删除五个旧账本查询包装；test-only 消费迁到现役 context/delta reader，历史性能对照的分页 SQL 仅留在测试中。 |
| F28 | 删除未调用的 generation helper 和四处无效 mock；真实 snapshot/read-version/generation 围栏保留。 |
| F29 | Candidate 包装收窄为原 `MemoryFact` tuple；删除未读字段、恒真 create 标记及无生产构造的 Plan 分支。保留候选 ID/同 owner 验证、CONTEST 关系、真实 NOOP、过期历史时间与旧 SELF checkpoint reader。 |
| F33 R01–R05 | 删除孤立结构化格式 helper、工具 token helper、连接池计数属性、未读诊断参数及索引 DDL 展示 helper。实际模型格式、完整容量计量、连接池关闭、原异常、诊断缺样和真实 Index 定义保留。 |
| F33 R06–R08 | 删除孤立 correlation/scope helper、导出与未读 latest-feedback 查询；原事件关联、hydrate 校验和真实反馈写入/消费保留。 |
| F33 G01–G04 | 删除配置、部署、权限旧投影及 artifact refs 的孤立 helper。真实配置、权限目录、引用表/trigger、Work 引用清退与 GC 保留。 |
| F33 B01–B05 | 删除 Task 孤立来源/checkpoint 包装、无效装配参数、旧通知 alias 包装及 reader 中丢弃结果的 grant 查询。writer 的当前 owner/Conversation/Presence/真实 grant 核验保留。 |
| F33 D01–D07 | 删除 Memory 入队无效字符参数、rebuild 孤立计数、evidence metadata 包装、媒体孤立关联入口和 test-only 管理包装；测试改用现役事实创建/失效接口。原 ASR/证据正文、媒体归属、review_state 与版本复制保留。 |

F33 D08 仍依赖 F15 的备用链退出，按任务书留到后续批次，未当作无消费者方法删除。

## 合同与接口

本批没有数据库 schema 或冻结迁移变更，也不改写已有事实、原事件、Work、请求/执行 ID、
预算、协议检查点或效果回执。主库 FIFO writer、组合 session 和不可变 artifact 发布继续沿
现行 owner 执行。没有访问生产或调用真实模型、订阅端点、原生用户任务或聊天发送。

本批有意撤销以下 Host Python 表面，不保兼容别名：

- `MemoryCandidate`、`MemoryResolutionPlan.create_new_fact`，以及 `apply_claim` 的 Candidate 包装输入；新输入直接使用 `MemoryFact` tuple。
- `MemoryContextService(facts=...)` 和 `SandboxTaskRepository(settings=..., lookup_plugin_host=...)` 的无效构造参数。
- `ModelClientPool.connection_pool_count`，correlation/scope 的已删除导出，以及表中列明的旧 Repository/Service helper。
- `MemoryWorker.enqueue(content_characters=...)` 的未读参数。

插件 SDK 的公开输入、metadata、批准、执行 grants 和独立 Plugin session 未改动。
日期任务书、旧审计和验收中的历史符号按当时事实保留，不改写为今日接口。

## 行为验证

定向回归覆盖 Memory 检索、原 mutation receipt、重建取消与已付收尾、Dream、历史隔离、
上下文、通知并发、Task/外部执行回执、媒体、Provider、容量、查询索引和 artifact GC。
新增候选 ID 越界/跨 owner 的原子拒绝回归；历史读取测试继续验证 reset floor、排序、
消息过滤先于 limit 和 read-version，通知合并/regrant 的 barrier 移至真实只读准备边界。

本地定向回归结果：

| 分组 | 结果 |
| --- | --- |
| Memory | 151 passed；新增 Plan ID/owner 原子拒绝回归另有 2 passed |
| 历史与上下文 | 192 passed；性能对照 reader 最后调整后重跑该文件 27 passed |
| Task、通知、Memory 入队与媒体 | 251 passed、4 skipped；跳过项需要 POSIX Manager/Supervisor |
| Provider、容量、索引、配置、artifact | 248 passed |
| 示例与订阅插件 | 27 passed；订阅插件 mypy 与两插件 `yuki plugin test` 生命周期合同均通过 |

全仓 ruff 和 Linux mypy 通过（658 个源文件）。Windows mypy 与固定 main 同为 7 文件
32 条 POSIX API 类型错误，按路径和错误内容对照一致，没有增加类型错误。

全量验证使用仅属于本任务的 Linux 临时容器，分别装入固定 main 与候选源码，按同一
`uv.lock` 执行 `uv sync --frozen --all-extras --python 3.12`，不使用生产数据：

```sh
uv run --no-sync pytest tests/unit -q -m "not native_worker" --strict-markers
uv run --no-sync pytest tests/integration -q -m "not native_worker" --strict-markers
```

全量结果与 Native CodeMode、三系统连接器结果以本分支 PR 的验收说明和对应 Quality
运行记录为准；上述定向结果不代表全量或 Native 已通过。本地日志位于 `.cache/b1-validation/`。

合并、Release、部署及自然流量验收不属于本批交付。
