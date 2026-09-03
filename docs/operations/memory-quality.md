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
主动读取的 duplicate 可与 success/empty 同时计数；统计不输出消息、记忆正文或外部账号。

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
