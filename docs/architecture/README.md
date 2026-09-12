# 架构文档入口

开发先读 [共同架构约束](development-contract.md)，再读涉及的模块合同。
Yuki 正在逐层解耦：永久主体、内部事件和工作身份由核心持有，平台凭据停留在传输边界。
存量代码不自动等于正确设计，发现残留平台回查或隐藏 Provider 分流时应修正。

| 现行文档 | 范围 |
| --- | --- |
| [共同架构约束](development-contract.md) | 不可混用的 ID、依赖方向、固定合同、持久续跑、事务和交付原则 |
| [Canonical runtime](canonical-runtime.md) | Yuki、Person、Space、Presence、Conversation 和网关边界 |
| [持久工作者](persistent-subagents.md) | 子 Agent 生命周期、权限、根预算和缓存 |
| [Conversation Rollup](conversation-rollup.md) | 原始事件、历史投影、压缩与 generation |
| [Memory](memory-v2.md) | 记忆范围、证据与权限 |
| [MCP 架构](../mcp/architecture.md) | 外部工具、固定清单和执行授权 |
| [插件架构](../plugin-development/architecture.md) | SDK、Host 与隔离插件会话 |
| [持久环境](../operations/persistent-environment.zh-CN.md) | 工作区、终端、Manager、文件交付和恢复 |
| [搜索适配器](../deepseek-search-bridge.md) | 临时协议适配和真实失败兜底 |

本目录中的“任务书”“验收”“进度”“请求对照报告”以及 operations 下的日期记录
属于特定基线的设计或证据，不是现行开发合同。releases 和 upgrade 文档描述各自版本。
需要核对历史原因时查这些记录；实施时不能照搬其中已经替换的入口、迁移版本或恢复流程。

本文是开发导航，不是上线证明；实际部署状态须核对当前镜像、数据库版本与部署记录。
