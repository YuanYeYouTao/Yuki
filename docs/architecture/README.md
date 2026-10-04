# 架构文档入口

开发先读 [共同架构约束](development-contract.md)，再读涉及的模块合同。
Yuki 正在逐层解耦：永久主体、内部事件和工作身份由核心持有，平台凭据停留在传输边界。
存量代码不自动等于正确设计，发现残留平台回查或隐藏 Provider 分流时应修正。

| 现行文档 | 范围 |
| --- | --- |
| [共同架构约束](development-contract.md) | 不可混用的 ID、依赖方向、固定合同、持久续跑、事务和交付原则 |
| [Canonical runtime](canonical-runtime.md) | Yuki、Person、Space、Presence、Conversation 和网关边界 |
| [Control Plane 地基](control-plane-foundation.md) | 可信管理主体、共享装配、读写版本、外部执行回执、公开协议与完整 WebUI 后续边界 |
| [WebUI](webui-console.md) | 正式手帐前端、同源 HTTP、浏览器会话、消息/轨迹/附件、业务页面与剩余建设范围 |
| [执行过程查看](execution-trace.md) | 有期限的实际请求、可读思考、工具结果、消息与投递回执查询 |
| [主 Agent 执行与恢复](main-agent-runtime.md) | 公共执行器、来源授权、恢复所有者与预算 |
| [Provider 输出边界](provider-output-boundary.md) | 思考与正文通道、显式发送及上游异常的验收范围 |
| [模型供应商与协议](model-providers.md) | Chat/Responses/Claude/Gemini、供应商方言、能力边界与私有状态恢复 |
| [持久工作者](persistent-subagents.md) | 子 Agent 生命周期、权限、根预算和缓存 |
| [Conversation Rollup](conversation-rollup.md) | 原始事件、历史投影、压缩与 generation |
| [聊天媒体与发送合同](chat-media-workspace.md) | 会话内媒体索引、24 小时缓存、工作区提升、切换重置与定时 Social 发送 |
| [Self Reflection](self-reflection.md) | 自省 Responses 合同、后台周期、预算、重试与运维报告 |
| [语义参与宿主接入](semantic-participation.md) | Jev 稀疏触发和纠正、真人普通接话、SELF 自主行动、原接纳与真实反馈；部署与自然 QQ 验收分别核验 |
| [自主参与连续决策模型](autonomous-participation-model.md) | 连续状态与回执决定自主 SELF 思考时机；参数与合成回放验收边界 |
| [Memory](memory-v2.md) | 记忆范围、证据与权限 |
| [Tool Kernel](tool-kernel.md) | 固定主声明、目录查询与执行授权 |
| [MCP 架构](../mcp/architecture.md) | 外部工具、固定清单和执行授权 |
| [插件架构](../plugin-development/architecture.md) | SDK、Host 与隔离插件会话 |
| [持久环境](../operations/persistent-environment.zh-CN.md) | 工作区、终端、Manager、文件交付和恢复 |
| [搜索适配器](../deepseek-search-bridge.md) | 临时协议适配和真实失败兜底 |

本目录中的“任务书”“验收”“进度”“请求对照报告”以及 operations 下的日期记录
属于特定基线的设计或证据，不是现行开发合同。releases 和 upgrade 文档描述各自版本。
需要核对历史原因时查这些记录；实施时不能照搬其中已经替换的入口、迁移版本或恢复流程。

设计基线：[SELF 主体、自动化与 Work 信号等待任务书](Yuki-SELF主体自动化与Work信号等待任务书-2026-09-24.md)。
实现已进入当前开发分支；合并与上线状态以实际 PR 和部署记录为准。

当前自主频率设计与联测范围见[自主参与反馈模型任务书](autonomous-participation-social-feedback-taskbook.md)；
现行算法以[自主参与连续概率模型](autonomous-participation-model.md)和独立库固定修订为准。

本轮设计与验收清单：[语义参与与持续接话重构任务书](semantic-participation-continuation-taskbook-2026-10-03.md)。
联合修改 Host 和独立语义库，分开真人持续接话与新 SELF 自主行动，复用既有状态和唯一 Runtime；
包含普通反馈、Jev 稀疏矫正、开关、快照兼容、固定依赖与文档清理；实现和部署状态以交付记录为准。

历史设计：[主 Agent 全入口执行、恢复与交付统一任务书](main-agent-entrypoint-unification-taskbook.md)。
基于 2026-09-13 自动化与插件入口审计完成实现，已合并部署；定向验证、CI、迁移及观察边界见
[交付记录](../operations/main-agent-entrypoints-2026-09-14.md)。

历史设计：[Runtime 异常恢复与连续执行任务书](Yuki-Runtime异常恢复与连续执行任务书.md)。
该轮已实施，验收和部署证据见 [交付记录](../operations/runtime-recovery-2026-09-13.md)；
此前的验收不代表上述全入口缺口已经解决。

本文是开发导航，不是上线证明；实际部署状态须核对当前镜像、数据库版本与部署记录。

本轮实现任务书：[共享持久 Runtime 主干重构](persistent-runtime-refactor-taskbook.md)。
现行执行与恢复规则仍以主 Agent 合同和共同架构约束为准；任务书中的阶段验收不能替代实际 CI 与部署证据。
实际入口与职责删除见[实现记录](../operations/persistent-runtime-20261001.md)。

历史实施任务书：[长任务 Harness 与上下文压缩重构](long-task-harness-compaction-taskbook.md)。
包含工具证据保留、Work 压缩、Conversation Rollup、累计预算和子任务并行的代码审计与实施范围；
本轮实现及逐项验证见[交付记录](../operations/long-task-harness-2026-10-01.md)。合并与上线状态以该记录的实际证据为准。

历史实施任务书：[历史快照与长任务交互 Harness](history-snapshot-and-work-reporting-taskbook.md)。
基于 2026-10-02 的投影漏接与汇报边界核查，补普通历史冻结、长任务开始顺序和既有 steer 上的答复/续行保障；
阶段汇报复用模型循环，不重新实现 steer 接入与恢复，也不增加逐阶段沟通状态机。
实现与部署证据见[交付记录](../operations/history-interaction-harness-2026-10-02.md)，不替代自然 QQ 能力验收。

V6 迁移链为 `0065`（关系历史索引）→ `0066`（autonomy 接纳）→
`0067`（SELF 证据与自省水位）→ `0068`（内部引用事件）→
`0069`（Social 回执内部事件关联）→ `0070`（无来源机会与讨论线程）。
仓库迁移头不等于生产数据库版本；合成 Jev 与控制器回放也不等于真实 QQ 社交验收。

普通搜索续接的容量计量修正与等长缓存窗口核查：
[2026-10-02 核查记录](../operations/compact-capacity-accounting-2026-10-02.md)。
前台回复与 SQLite 写锁本轮实现及验证状态见
[2026-10-03 交付记录](../operations/foreground-rollup-sqlite-contention-2026-10-03.md)。

搜索桥使用连接预算、摘要等待及输出预算的放宽设计见
[2026-10-02 策略调整](../operations/search-compaction-limits-2026-10-02.md)。

实施规格：[Work 上下文与普通聊天续接修复任务书](work-context-and-chat-continuation-taskbook.md)。
以当前获准聊天和必要观察线索继续工作，原始研究资料外存并按需读取；纳入经源码确认的
普通同轮容量整理，替换恢复整份旧群史的旧设计。PR #228 已合并；验证与部署证据见
[实施交付记录](../operations/work-context-delivery-2026-10-03.md)。

实施规格：[前台回复、后台历史整理与 SQLite 写锁修复任务书](foreground-rollup-and-sqlite-contention-taskbook-2026-10-03.md)。
基于 `3d9e273` 的代码核查和隔离 SQLite 竞态复现，规定实际容量内先回复、后台整理及明确采用边界，
并修复 Rollup 提交快照、周期空写和锁内准备。PR #229 已合并为 `b8adc49`，Bot-only 部署及重试证据见
[交付记录](../operations/foreground-rollup-sqlite-contention-2026-10-03.md)；自然聊天延迟、摘要质量与缓存改善仍须分别观察。

本轮修复规格：[数据库回复延迟任务书](database-reply-latency-taskbook-2026-10-04.md)。
包含参与反馈批读与公平对账、Protocol GC 和回复投影的数据库优化；用户已授权实施。
验证、提交、合并、部署与自然聊天效果见[交付记录](../operations/database-reply-latency-2026-10-04.md)，
按各自实际证据分别记录。
