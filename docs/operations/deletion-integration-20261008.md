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

相对 Pi 固定输入的源码提交 `bcb43a74` 统计（包含 main 集成，非全部归因于新重构）：产品 `src` 新增 4689 / 删除 12010，净减少 7321 行；迁移新增 270 / 删除 27；scripts 新增 125 / 删除 122；tools 新增 49 / 删除 1299。测试搬迁、冻结历史迁移 SQL 和必要保护不能冒充产品净删除。最终提交可直接用上述固定输入复算。

## 生产准备与回滚边界

现生产库为 0096，旧 run_python 全终态、无旧插件执行 cohort；两运行插件仅迁 API 3.3 并按原精确权限重新批准。Manager 保留原 jobs.sqlite3、home、workspace、completion outbox 和 quarantined 记录。部署只替换 Bot 及其所需 Manager 代码，不重建 SnowLuma，不发送额外 QQ 测试消息。

在线只读 SQLite 一致快照已完成并 quick_check 通过；后续在网络禁用、无生产数据挂载的容器中对独立副本做迁移和原列数据哈希对比。正式升级须在停 Bot 后另建数据库及相关文件恢复点；0097 删除能力数据的回退依赖整份冷备，不宣称 downgrade 可以恢复已删除事实。


## 末尾复审补漏（合并前）

- API-03：直调协调、结果缓存、TurnExecution 和 Code 子调用里仍有从展示 JSON 推断 executed / pending / committed 的消费点。迁为既有 typed outcome 与原 durable evidence；展示内容不决定执行状态，原 ID 与预算不退款。历史缺失或错型 outcome 的保守判据另行覆盖。
- MEM-01：Runner 与 Code 中重复的 memory 独占批次门退出；direct 同批 send_message 必须先观察 Memory 回执的门、Code 真正 Memory 写入之后的 Host stop、unknown 围栏和每次授权保留。死 locator 异常、无 caller 的 score 兼容别名和旧图片禁写 docstring 同步清理。
- AUTO-01：无执行 caller 的 automation_text_generation 路由枚举及安装/示例/检查链退出。旧 TOML 明确拒绝；部署仅移除该条路由，不重写其它模型配置或历史 telemetry task。
- APP-08：上一代 decoder 的 provider_summary=None 省略分支退出；实际生产旧 SHA 964dadef 的 WebSearchResponse 已有该字段，不需要该分支维持当前回退读取。
- 现行 help、SDK 迁移入口、运行时政策和发布草案同步；历史任务书和旧部署报告仍标明其历史范围，不把原证据改写为当前通过。

第三轮 Windows 全量结果为 4669 passed、257 skipped、1 failed（Social 并发测试在 2 秒准备等待中超时）；该完整文件不修改测试或超时后复验 9 passed。Windows/POSIX/native 跳过由 Linux 专项矩阵补充。WebUI 在 CI 的原 5 秒默认预算下全通过；本机重负载下 timeline 单项超时，单独以 15 秒进程测试预算复验 4 passed，不修改产品或仓库测试超时。最后新增源改动另有定向回归，最终 CI 按实际提交核对。

生产一致副本 0096→0099 已通过：135 个保留表原列及行数 SHA256 完全相同，新增 projection 列全 NULL，quick_check=ok、foreign_key_check=0、正式 schema guard 通过。MCP 两表在原 0096 已不存在；退役 speech 表的原行数为 108/1/6/0，原备份文件前后 SHA 相同。证据含初次环境配置缺失导致的零写入失败及修正后完整成功日志，保留在私有运维目录。

部署前语音文件盘点：以原一致数据库记录逐条解析 107 条非空相对路径（6 个参考文件及 101 个生成文件），实际绑定的 `/opt/yuki-qqbot/data/speech` 中均已不存在，原目录只保留 japanese_frontend。此次盘点未删除文件，不能宣称把这些缺失 WAV 纳入了冷备。原表、路径和缺失事实另存私有 manifest，正式冷备仍保存全部现存 data；历史 accepted/unknown 发送回执按原身份保留，不因源 WAV 缺失推翻已发生效果。

终审补改验收：直调 typed / 原回执 144 passed；运行时最后观察边界与隐藏拒绝 19 passed；Code 单元 251 passed，Linux 三入口与 control 30 passed、追加 control 完整 11 passed；历史 outcome 完整回归 85 passed、补充严格边界 22 passed；退役模型路由 44 passed。历史回执展示字节不改，未知不取得重执行资格。

最终历史容器边界再审 90 passed，260 组 Python/SQLite 差分一致。有效 typed 拒绝重放的测试后端补齐真实 side_effecting 布尔角色，联合 72 passed；没有为错型 null 角色放宽生产 unknown 保护。

`bcb43a74` 源码的 direct 镜像确认不含 Monty binding、Settings 默认关闭 Code；codemode 镜像已在实际生产内核通过无 bind mount 的打包隔离探针，worker/launcher 全部来自镜像，见 [打包探针原件](deletion-codemode-packaged-20261008.json)。任务索引 68 项源码及专项验收均已标记；最终 CI、上线仍单独记录。
