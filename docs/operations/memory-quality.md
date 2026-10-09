# Memory 来源审计与维护

以下命令必须指向明确的数据库。来源 audit 只读，不自动修复历史缺链，也不将同 key 独立事实当成冲突。

```bash
qq-ai-bot-cli memory audit --database-url <database-url>
qq-ai-bot-cli memory stats --database-url <database-url> --hours 24
qq-ai-bot-cli memory hygiene scan --database-url <database-url>
qq-ai-bot-cli memory hygiene apply <fingerprint> --database-url <database-url>
qq-ai-bot-cli memory hygiene rebuild-fts <fingerprint> --database-url <database-url>
```

apply 和全量 FTS 重建是明确的维护动作；先查 scan，再核对原指纹和备份。不要通过清库、重置预算或重新调用模型掩盖已提交效果和历史缺链。

自省和 Dream 的请求登记发生在实际 HTTP 派发前。payload 构造失败不产生请求计数，HTTP 重试逐次登记，累计额度和原操作回执仍约束恢复。未选择事实不自动写处理 checkpoint，批准的 Rebuild 子集可提交，未决项保持 REVIEW。

维护只按事实的 valid_until 处理到期；不按来源、置信度、年龄或 scope 容量隐式遗忘。归因、强化、Activation、自动注入、synthetic quality/baseline/release-check 均已退休。统计或健康检查不证明模型实际使用或自然聊天质量。

当前合同见 [Memory](../architecture/memory-v2.md)与[来源审计](../architecture/memory-v2-quality.md)。
