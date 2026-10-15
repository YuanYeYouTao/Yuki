# 架构文档入口

开发先读[共同开发约束](development-contract.md)，再读涉及模块。以下是当前源码合同；发现源码不符合合同，记录具体缺口，不把历史设计或验收结论当成现行实现。

本入口按 2026-10-15 的 3.9.0 源码整理，迁移 head 为 `0119`、Plugin API 为 `3.5`、Main 固定工具合同为 `19`、Code API 为 `yuki.codemode.api.v2`。完整合并范围见[3.9.0 发布说明](../releases/v3.9.0.md)，实际升级与配置变更见[升级指南](../upgrade-3.9.0.md)。正式发行与具体生产部署分别记录。

| 现行文档 | 范围 |
| --- | --- |
| [开发约束](development-contract.md) | 删除优先、身份/授权、恢复、事务和交付原则 |
| [Canonical runtime](canonical-runtime.md) | Yuki、Person、Space、Presence、Conversation 与网关 |
| [主 Agent](main-agent-runtime.md) | 统一循环、Work 控制、恢复、预算和交付 |
| [Tool Kernel](tool-kernel.md) | 固定完整清单、direct / Code Mode、目录与执行授权 |
| [模型供应商与协议](model-providers.md) | 显式 Profile、结构化模式、思考、方言和私有续跑状态 |
| [Provider 输出](provider-output-boundary.md) | 思考、正文、真实工具调用与显式发送 |
| [持久工作者](persistent-subagents.md) | 子任务生命周期、Main 完整能力、真实来源恢复与根预算 |
| [工具结果](tool-results.md) | 结果预算、共享 artifact、媒体和证据 |
| [会话 Compact](conversation-compact.md) | 真实容量触发、公共检查点、私有执行尾部与已付恢复 |
| [聊天媒体](chat-media-workspace.md) | 附件索引、缓存、工作区提升与发送 |
| [Memory](memory-v2.md) | 独立事实、证据、读取和写入权限 |
| [记忆变更](memory-change.md) | 按 fact ID 的日常修改和原回执 |
| [Self Reflection](self-reflection.md) | Profile 输出、周期、工具回执、预算与原批次恢复 |
| [语义参与宿主](semantic-participation.md) | 普通持续接话、SELF 自主行动、Jev 与真实反馈 |
| [自主参与模型](autonomous-participation-model.md) | 连续状态、回执与机会参数 |
| [Control Plane](control-plane-foundation.md) | 管理主体、装配、执行回执与业务服务 |
| [WebUI](webui-console.md) | 3.9.0 临时管理界面、认证、业务页面和实际覆盖 |
| [执行过程](execution-trace.md) | 实际模型请求、工具、消息与投递的限期查询 |
| [插件架构](../plugin-development/architecture.md) | Plugin API 3.5、Host、SDK 与隔离插件会话 |
| [持久环境](../operations/persistent-environment.zh-CN.md) | 工作区、终端、Manager 与交付 |
| [Code Mode 运维](../operations/pi-codemode-operations.md) | 可选构建、隔离、原数据和切换 |
| [独立原生搜索](../deepseek-search-bridge.md) | 独立搜索连接、三种原生协议、网页提取与升级 |

## 设计报告与后续计划

- [10-15 记忆种子全链删除与参与状态反馈任务书](Yuki-记忆种子全链删除与参与状态反馈任务书-2026-10-15.md)：已授权实施；删除后台记忆 seed 全链，已有 JEV 请求附带相关 engage；保留普通快路，补并行语义复核、未核验快接计数与双重退出，并更新发言节奏提示。原执行回执、明确请求和 Work 保留，不因状态变化单独评分；验证、合并和上线分别记录。
- [10-15 #439 普通输入 Steer 与排队接纳统一修复任务书](Yuki-439普通输入Steer与排队接纳统一修复任务书-2026-10-15.md)：复用原 admission 统一轮内补充与排队接纳，保留公共冻结前缀、真实媒体来源、Work/wait 和原执行授权；实现与验证正在收口，PR、合并和上线分别记录。
- [10-15 Work 收尾截断删除任务书](Yuki-Work收尾截断删除任务书-2026-10-15.md)：记录 Triple Town 未发送的原回执、共同执行链、子任务与本地 Pi 对照；D01–D13 已实施，PR #437 的 Quality 通过并合并，产品提交 `030917b1` 已完成 Bot-only 部署。原问题消息未重发，生产自然投递与真实缓存 usage 另按观测记录。
- [#417/#418 终审缺口与修复任务书](Yuki-417-418终审缺口与修复任务书-2026-10-14.md)：原终审复现、开发约束审查与 F01–F09 的修复实现；源码、CI、合并和上线分别记录。
- [#417/#418 实施与验证](../operations/compact-417-418-delivery-2026-10-14.md)：分阶段 PR、CI 修复、冷恢复与实际 DeepSeek 调用；合并与上线分别核对。

- [10-14 Work 摘要退役、会话 Compact 与任务树设计审查报告](Yuki-Work摘要退役与会话Compact及任务树设计审查报告-2026-10-14.md)：关联 Issue #417/#418，包含 PR #228 追溯、当前源码与 Pi durable 对照、旧摘要/Rollup 全链路删除索引、公共与私有窗口、原任务恢复、子 Agent 完整继承 Main 能力、待实施验证与成本数据表，以及真实聊天摘录。报告保留当时设计与未实施基线；现行实现见上方会话 Compact 和持久工作者合同，生产节省仍需真实测量。
- **下一大版本多客户端重构**：3.9.0 现有 WebUI 是临时管理界面；下一大版本将彻底重构为多客户端。当前 WebUI 与控制面仍有效，这项未来规划不表示现有实现已退役或已经提供新客户端接口。调研与分阶段方案见 [#299 控制面重构与多客户端接入任务书](Yuki-控制面重构与多客户端接入任务书-2026-10-14.md)（未实施）。

## 当前查询审查

- [10-12 资料与数据库查询删减优化任务书](Yuki-资料与数据库查询删减优化任务书-2026-10-12.md)：44项实施与16项候选调查已终审，当前源码索引与SQLite3.46.1核验见§11；PR #298/#302及0107 Bot部署已完成，[交付记录](../operations/query-optimization-20261012.md)单列补修CI和待验收的真实QQ。
- [10-13 本轮资料仅当前注入与主入口一致性任务书](Yuki-本轮资料仅当前注入与主入口一致性任务书-2026-10-13.md)：普通运行资料只用于当前激活，退出公共首份与差量冻结；所有定时安排到点激活共同 Main，SELF、SDK 和下游消费者删除来源专属禁令。实施、真实模型实验和交付分别记账。
- [10-13 公共历史冻结核查](../operations/public-history-freeze-audit-20261013.md)：保留实际d75按说话人分别冻结的原证据；Y08–Y13已随PR316/317交付ec568631/0113，公共历史沿同一Conversation，当前资料只在本次正文上方。生产迁移、全18会话重置与新版请求核验分别记录，不将单人自然样本冒称跨人通过。
- [10-12 本轮资料差量冻结与上下文删减任务书](Yuki-本轮资料差量冻结与上下文删减任务书-2026-10-12.md)：保留该次实施及测量证据；普通运行资料的设计已由10-13任务书替代，不再作为当前开发规则。

## 本次实施与交付

- [10-11 自动化删减与指令完整性任务书](Yuki-自动化删减与指令完整性任务书-2026-10-11.md)：D01–D63完成、V01–V32按对应范围验收；保原截断与旧红CI事实，记录实现PR、新版CI、0106上线及新群128/129完整指令/真实回执。

- [Work与Memory多余约束删除审查](Yuki-Work与Memory多余约束删除审查-2026-10-10.md)：按删除优先核对结束、失败、队列、证据、JSON与真实数据库上限；本地、最终验证及部署状态分别记录。

- [旧人物评分关系系统删除任务书](Yuki-旧关系好感度系统彻底删除任务书-2026-10-09.md)：R00–R23逐项记录删除、验证、PR与上线状态；旧Work清退仅适用于本次明确授权。

- [10-09 NapCat 删除与既有网关抽象保留任务书](Yuki-NapCat删除与既有网关抽象保留任务书-2026-10-09.md)：N00–N13 已实施、验证、合并与上线，[生产及清理回执](../operations/napcat-retirement-20261009.md)保留当时部署基线；退役变更已纳入 3.9.0，既有 Registry/Memory 未决项按各自后续证据读取。任务书不新增或替代现行合同。

- [10-10 Work 内核任务书](Yuki-Work生命周期删减与Pi-Durable重构任务书-2026-10-10.md)：删除收尾门槛、复用原冻结与持久回执、统一父子关系；完成状态逐项记录。
- [Work 全景盘点](Yuki-Work现状全量盘点-2026-10-10.md)：固定基线和 Pi durable 对照，不以旧描述覆盖现行实现。

## 历史设计与交付证据

本目录剩余日期审查、任务书、实验和 operations 日期记录按其基线保留。它们用于追查历史事实，不是新的开发规则。已被现行合同替代且无独立证据的施工方案已删除；不要从旧报告恢复退役入口、经验阈值或迁移编号。

- [10-09 Memory 删除任务书](Yuki-Memory-Provider分工与后台收尾审查任务书-2026-10-09.md)：D01–D33 原实施、验证、合并与部署回执。
- [10-09 残留与文档核查](../operations/documentation-residual-audit-20261009.md)：该轮清理、逐份文档结果与当时的缺口；SELF 旧代次领取的后续处理见本轮 Work 与 Memory 审查 C21。
- [Work 生命周期审查](work-lifecycle-audit-20261008.md)：审查基线、后续实现及仍需核对的运行问题。
- [Pi/Code 移植来源](pi-port-provenance.md)：设计参考、实现来源与第三方许可；Code Mode 已合入主线，不是待移植实验入口。

真实 Provider 测量、日期部署和自然 QQ 验收按各自证据读取。版本/迁移以实际源码或镜像为准，正式发行范围见[3.9.0 发布说明](../releases/v3.9.0.md)与[升级指南](../upgrade-3.9.0.md)；不改写旧 Release 或历史部署的基线。
