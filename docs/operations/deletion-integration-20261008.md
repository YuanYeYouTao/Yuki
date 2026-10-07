# 删除导向重构集成记录（2026-10-08）

本记录对应任务书 68 项。任务索引的勾选表示实现及对应定向验收已完成；生产部署和最终全量结果单独记录，不能用勾选代替上线证明。

## 集成边界

- 以 main `aecc09d621067b7782c2b89e919fe2a7b2c9e5fa` 与 Pi `e7bc7d3275b09ddc5363bbb1eb9d2ee255f276ed` 合并输入实施，保留现行开发合同。
- 用户确认只有当前 main 系谱 0096 生产数据库需要升级，无需保留 Pi 0097/0098 数据库。0097 为 main 语音退役，0098 为 MCP 状态退役，0099 为 summary representation 冻结；不重写历史已部署 revision。
- MainAgentContract revision 16、SDK 3.3、context layout 2、Code API v1 分属各自 owner。实际 Provider profile 保持既有独立 revision。
- 旧 accepted / unknown / dispatched Social 与 Work 回执按原身份查询；新目录无语音、MCP、run_python 或 SDK LLMFacade。旧 active_seconds 只读，原预算、请求、任务和投递 ID 不重置。
- Code Mode 可关闭，关闭时不构造新 Code 引擎，旧调用安全拒绝或按历史回执恢复；终端能力仍是直接工具。启用时使用 pinned 原生 Monty worker、固定 launcher 及 AppArmor/seccomp 隔离。

## 分项证据

- [API、文件、终端、Control、Automation、SDK、CLI](deletion-api-tools-20261008.md)
- [Runtime、恢复、身份和首次上下文布局](deletion-runtime-20261008.md)
- [数据库、Memory、上下文和 Artifact](deletion-data-context-20261008.md)
- [Code 原生隔离合同与真实服务器 canary](../../deploy/security/README.md)
- [真实 enforce 探针原始结果](deletion-codemode-isolation-20261008.json)

最终联合定向检查：语音表退役、历史 receipt 恢复、合同升级、Social 长键及 Code 合同合计 126 passed；真实 Linux Code runner 11 passed；Chat / Automation / Plugin SDK 三类入口 20 passed。纯终端/文件真实 Linux 矩阵和 200MiB 附件、停机 bootstrap 锁见 API 证据。历史 0081、0091、0096 的真实旧 producer 写入原事实后升级路径分别通过。

`ruff check .`、`ruff format --check .`、Linux 目标 `mypy src`（683 个文件）与 `release_validate --tag v3.9.0` 已执行。源码冻结后的全量回归、最终镜像及生产迁移验收尚在进行，完成后在本记录追加确切结果。

## 删除量口径

相对 Pi 固定输入的实现阶段统计（包含 main 集成，非全部归因于新重构）：产品 `src` 新增 4120 / 删除 11641，净减少 7521 行；测试新增 5375 / 删除 2924；迁移新增 270 / 删除 27；scripts 新增 123 / 删除 121；tools 新增 49 / 删除 1299。测试搬迁、冻结历史迁移 SQL 和必要保护不能冒充产品净删除。最终提交可直接用上述固定输入复算。

## 生产准备与回滚边界

现生产库为 0096，旧 run_python 全终态、无旧插件执行 cohort；两运行插件仅迁 API 3.3 并按原精确权限重新批准。Manager 保留原 jobs.sqlite3、home、workspace、completion outbox 和 quarantined 记录。部署只替换 Bot 及其所需 Manager 代码，不重建 SnowLuma，不发送额外 QQ 测试消息。

在线只读 SQLite 一致快照已完成并 quick_check 通过；后续在网络禁用、无生产数据挂载的容器中对独立副本做迁移和原列数据哈希对比。正式升级须在停 Bot 后另建数据库及相关文件恢复点；0097 删除能力数据的回退依赖整份冷备，不宣称 downgrade 可以恢复已删除事实。
