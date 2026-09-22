# 架构文档入口

开发先读 [共同架构约束](development-contract.md)，再读涉及的模块合同。
Yuki 正在逐层解耦：永久主体、内部事件和工作身份由核心持有，平台凭据停留在传输边界。
存量代码不自动等于正确设计，发现残留平台回查或隐藏 Provider 分流时应修正。

| 现行文档 | 范围 |
| --- | --- |
| [共同架构约束](development-contract.md) | 不可混用的 ID、依赖方向、固定合同、持久续跑、事务和交付原则 |
| [Canonical runtime](canonical-runtime.md) | Yuki、Person、Space、Presence、Conversation 和网关边界 |
| [主 Agent 执行与恢复](main-agent-runtime.md) | 公共执行器、来源授权、恢复所有者与预算 |
| [持久工作者](persistent-subagents.md) | 子 Agent 生命周期、权限、根预算和缓存 |
| [Conversation Rollup](conversation-rollup.md) | 原始事件、历史投影、压缩与 generation |
| [Self Reflection](self-reflection.md) | 自省 Responses 合同、后台周期、预算、重试与运维报告 |
| [Memory](memory-v2.md) | 记忆范围、证据与权限 |
| [Tool Kernel](tool-kernel.md) | 固定主声明、目录查询与执行授权 |
| [MCP 架构](../mcp/architecture.md) | 外部工具、固定清单和执行授权 |
| [插件架构](../plugin-development/architecture.md) | SDK、Host 与隔离插件会话 |
| [持久环境](../operations/persistent-environment.zh-CN.md) | 工作区、终端、Manager、文件交付和恢复 |
| [搜索适配器](../deepseek-search-bridge.md) | 临时协议适配和真实失败兜底 |

本目录中的“任务书”“验收”“进度”“请求对照报告”以及 operations 下的日期记录
属于特定基线的设计或证据，不是现行开发合同。releases 和 upgrade 文档描述各自版本。
需要核对历史原因时查这些记录；实施时不能照搬其中已经替换的入口、迁移版本或恢复流程。

历史设计：[主 Agent 全入口执行、恢复与交付统一任务书](main-agent-entrypoint-unification-taskbook.md)。
基于 2026-09-13 自动化与插件入口审计完成实现，已合并部署；定向验证、CI、迁移及观察边界见
[交付记录](../operations/main-agent-entrypoints-2026-09-14.md)。

历史设计：[Runtime 异常恢复与连续执行任务书](Yuki-Runtime异常恢复与连续执行任务书.md)。
该轮已实施，验收和部署证据见 [交付记录](../operations/runtime-recovery-2026-09-13.md)；
此前的验收不代表上述全入口缺口已经解决。

本文是开发导航，不是上线证明；实际部署状态须核对当前镜像、数据库版本与部署记录。
