# 架构文档入口

开发先读[共同开发约束](development-contract.md)，再读涉及模块。以下是当前源码合同；发现源码不符合合同，记录具体缺口，不把历史设计或验收结论当成现行实现。

| 现行文档 | 范围 |
| --- | --- |
| [开发约束](development-contract.md) | 删除优先、身份/授权、恢复、事务和交付原则 |
| [Canonical runtime](canonical-runtime.md) | Yuki、Person、Space、Presence、Conversation 与网关 |
| [主 Agent](main-agent-runtime.md) | 统一循环、Work 控制、恢复、预算和交付 |
| [Tool Kernel](tool-kernel.md) | 固定完整清单、direct / Code Mode、目录与执行授权 |
| [模型供应商与协议](model-providers.md) | 显式 Profile、结构化模式、思考、方言和私有续跑状态 |
| [Provider 输出](provider-output-boundary.md) | 思考、正文、真实工具调用与显式发送 |
| [持久工作者](persistent-subagents.md) | 子任务生命周期、权限与根预算 |
| [工具结果](tool-results.md) | 结果预算、共享 artifact、媒体和证据 |
| [Conversation Rollup](conversation-rollup.md) | 原始事件、真实历史投影、压缩与 generation |
| [聊天媒体](chat-media-workspace.md) | 附件索引、缓存、工作区提升与发送 |
| [Memory](memory-v2.md) | 独立事实、证据、读取和写入权限 |
| [记忆变更](memory-change.md) | 按 fact ID 的日常修改和原回执 |
| [Self Reflection](self-reflection.md) | Profile 输出、周期、工具回执、预算与原批次恢复 |
| [语义参与宿主](semantic-participation.md) | 普通持续接话、SELF 自主行动、Jev 与真实反馈 |
| [自主参与模型](autonomous-participation-model.md) | 连续状态、回执与机会参数 |
| [Control Plane](control-plane-foundation.md) | 管理主体、装配、执行回执与业务服务 |
| [WebUI](webui-console.md) | 同源管理界面、认证、业务页面和实际覆盖 |
| [执行过程](execution-trace.md) | 实际模型请求、工具、消息与投递的限期查询 |
| [插件架构](../plugin-development/architecture.md) | SDK、Host 与隔离插件会话 |
| [持久环境](../operations/persistent-environment.zh-CN.md) | 工作区、终端、Manager 与交付 |
| [Code Mode 运维](../operations/pi-codemode-operations.md) | 可选构建、隔离、原数据和切换 |
| [搜索适配](../deepseek-search-bridge.md) | 显式独立连接、协议适配与真实失败兜底 |

## 施工中的任务书

- [10-09 NapCat 删除与既有网关抽象保留任务书](Yuki-NapCat删除与既有网关抽象保留任务书-2026-10-09.md)：本地实施中；任务状态与验证按实际证据记录，尚不表示合并、发行或上线。任务书不新增或替代现行合同。

## 历史设计与交付证据

本目录剩余日期审查、任务书、实验和 operations 日期记录按其基线保留。它们用于追查历史事实，不是新的开发规则。已被现行合同替代且无独立证据的施工方案已删除；不要从旧报告恢复退役入口、经验阈值或迁移编号。

- [10-09 Memory 删除任务书](Yuki-Memory-Provider分工与后台收尾审查任务书-2026-10-09.md)：D01–D33 原实施、验证、合并与部署回执。
- [10-09 残留与文档核查](../operations/documentation-residual-audit-20261009.md)：本轮清理、逐份文档结果，以及 SELF 旧代次领取/禁用语义 scope 尚未修复的缺口。
- [Work 生命周期审查](work-lifecycle-audit-20261008.md)：审查基线、后续实现及仍需核对的运行问题。
- [Pi/Code 移植来源](pi-port-provenance.md)：设计参考、实现来源与第三方许可；Code Mode 已合入主线，不是待移植实验入口。

真实 Provider 测量、日期部署和自然 QQ 验收按各自证据读取。版本/迁移以实际源码或镜像为准，正式发行范围见[3.9.0 草案](../releases/v3.9.0.md)与[升级指南](../upgrade-3.9.0.md)；当前 head `0102` 不改变旧 Release 的历史基线。
