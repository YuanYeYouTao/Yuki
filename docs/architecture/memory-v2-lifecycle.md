# Memory 到期维护

`MemoryMaintenanceWorker` 只根据事实明确的 `valid_until` 处理到期失效，使用
`expired` 原因。来源类型、年龄、importance、confidence 与读取频率不构成自动淘汰依据；
没有期限的事实不会因为陈旧自动失效。事实、证据、版本和状态事件保留。

维护是有界的纯数据库工作，不调用模型、Embedding 或网络，不扫描聊天历史。
候选发现和完整证据准备在首写前完成；短事务复核事实与到期时间，并原子保存失效及审计。
SQLite WAL 快照竞争仅重备原纯数据库批次，不更换操作身份或重做外部效果。
关闭沿当前 Worker 生命周期处理；批次由自身锁串行，不与 Dream 共持跨模型长锁。

## 配置与管理

```dotenv
MEMORY_MAINTENANCE_ENABLED=true
MEMORY_MAINTENANCE_INTERVAL_SECONDS=300
MEMORY_MAINTENANCE_BATCH_LIMIT=100
```

这三项通过 RuntimeConfig 更新；批次大小和周期是运行预算，不是内容价值标准。

```text
/ai memory maintenance status
/ai memory maintenance run
/ai memory doctor
```

状态和 doctor 显示数量、到期积压及来源审计问题，不输出正文或凭据。同 key 多条独立
active 事实是合法状态。发现来源或队列问题时按原内部 ID 和回执核查，不清库或重写历史。
统一变更及恢复边界见 [Memory](memory-v2.md) 和 [变更合同](memory-change.md)。
