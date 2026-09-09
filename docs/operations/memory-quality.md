# Memory V2 质量运维

普通自动提取最多等待一小时（3600 秒）；达到 12 条或 8000 字符仍可提前领取，
30 秒轮询不是 30 秒模型调用。未到期且无失败的 pending 是正常聚合，不是阻塞。
processing lease、重试、即时 memory_change、Rollup、自省间隔不受此窗口影响。

自动首次写入采用精选事实与共同经历：明确声明 retention/source_style/importance/
confidence/value_reason，importance 至少 3；有意义的一次性经历也可为 3。低值正常
跳过，不进候选队列；空结果仍推进水位。后台来源由后端固定，不能自报 explicit。
维护、纠正、删除不受首次收录门槛限制，旧事实不会因此被删或无法读取。

完整操作手册见 [Memory V2 质量、审计与显式治理](memory-v2-quality.md)。本文件提供正式版
稳定入口，避免运维脚本依赖旧文件名。

查看最近 24 小时的真实统计时必须显式指定数据库：

```bash
uv run qq-ai-bot-cli memory stats --database-url <database-url> --hours 24
```

结果区分正常等待、可领取 owner、失败/过期 lease、自动零注入、已评估使用率和主动读取结果。
同轮缓存命中只增加 duplicate，不再重复增加 success/empty，也不代表新数据库或 embedding 查询。
统计不输出消息、记忆正文或外部账号。

主动读取由当前 TurnMemorySession 关联回执，不再使用普通聊天路径中的旧空 memory_turn_id。
没有预取回执时创建零暴露 agent_tool 回执；执行查询、进入下一次模型请求和 attribution
必须分开看。查询成功不代表模型收到结果，收到结果也不代表最终使用。
观测存储失败只记录关联 ID、工具名和异常类别，不把正常读取变成工具失败。
memory_read_intent 在目标解析之前记录显式字段存在性、枚举和数量，拒绝/歧义也有参数形状；
memory_tool_read 记录结果类别与数量，不保存原始参数。

明确日期采用 valid_from 的严格半开区间 `[start_at,end_at)`；先检查模型的当地零点和时区
是否正确，再检查后端传递与候选筛选。strict 返回空时不得自动放宽为 soft，也不能把
自动回执的 purpose 当成模型填写率。当前补充验证状态见
[意图使用审计](../architecture/memory-intent-usage-audit.md)。

## 自省与 Dream 的恢复边界

强相关召回使用 `memory.automatic_topic_threshold`、
`memory.automatic_background_threshold` 与 `memory.automatic_calibrated_profile`。
先冻结真实样本、分组标注和校准，再在独立验收集通过后设置；不能直接把默认 0.90 当成
已验证阈值。主题门槛不得低于背景，profile 不符/不可用时自动仅精确匹配，主动 lexical
查询仍可用。默认四条、背景最多一条补位，不是必须注入四条。旧的明确数量配置需审计后
定向调整，不会被新默认值覆盖。

质量回放不得改变生产 Memory、History、Rollup 或 embedding。原始样本只留在仓库外受限
目录；不能还原历史状态的样本只计入冻结 corpus 对照，不计严格历史指标。

自省的 `enabled=true` 不代表调度器仍存活，必须同时检查 `running`、最近 run 的终态和
`committed_count`。后台批次遇到未预料的异常时按持久化 result checkpoint 恢复：已有提交
保留并推进水位；没有提交则记录失败，后续周期可重试。异常不能杀死整个调度循环，取消仍
正常传播。诊断只记录异常类别及函数/行号，不打印可能包含记忆正文的数据库异常。

Dream 默认每轮最多 12 个 cluster、24 次模型请求，给每个 cluster 的一次格式修复留出预算。
已有环境显式设置的 `MEMORY_DREAM_MAX_MODEL_CALLS_PER_RUN=12` 不会被默认值覆盖，需要
操作者修改并重启 Bot。未开始便耗尽预算的 cluster 标记 `skipped/budget_deferred`，单独统计
`budget_deferred_clusters`，不计执行失败、不推进事实 checkpoint，下次增量规划仍可选中。
已执行但输出无效的 cluster 仍是失败；增加预算不能解决所有格式或语义问题。

召回使用率只衡量记忆是否实质支持最终回复，不把改变语气或泛泛说“我记得”算作使用。
应同时报告成功评估覆盖率、抢占/跳过与未评估数；不能把失败判定算作未使用，也不应为了
提高百分比放松事实使用判定或强迫回复引用记忆。

发布前依次执行：

```bash
uv run qq-ai-bot-cli memory quality validate-dataset
uv run qq-ai-bot-cli memory quality run --suite full
uv run qq-ai-bot-cli memory quality compare
uv run qq-ai-bot-cli memory release-check
```

真实数据库审计必须显式提供 `--database-url`。`memory audit` 与 `release-check` 只读；需要治理
时先执行 `memory hygiene scan` 保存 fingerprint，再由管理员人工审阅并显式执行
`memory hygiene apply <fingerprint>`。fingerprint 变化会拒绝执行，explicit fact、ambiguous
evidence 与 contested conflict 永不自动处理。
