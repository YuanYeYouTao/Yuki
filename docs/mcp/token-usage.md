# Token 使用

MCP Token 消耗包括冻结清单中的完整工具 Schema。普通聊天未调用 MCP 也会携带这些声明，
不能按零 Token 估算。固定前缀可以复用 Provider 缓存，但实际命中与计费须读取 Provider 指标。
Capability Search 使用本地目录检索，不替代主 Agent 的固定完整声明。

目录检索的数量和 Schema 预算不应用来逐轮裁剪主 Agent 清单。移除不再使用的 Server 或工具
属于显式部署配置变更，需要生成新工具合同；不能为了节省本轮 Token 临时改变声明。

复杂任务按分段与根任务总预算管理，休眠和继续不重置总额。参见
[持久工作者](../architecture/persistent-subagents.md)，不要只提高单轮限制而忽略总预算。
