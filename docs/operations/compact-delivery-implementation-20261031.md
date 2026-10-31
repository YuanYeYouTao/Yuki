# Compact 分流与工具回执修复实施记录

日期：2026-10-31。对应[任务书](../architecture/Yuki-会话Compact统一换窗与复读修复任务书-2026-10-31.md)。用户已授权实施、循环复审、PR／合并和备份后的 Bot-only 上线；以下各阶段分开记录。

## 基线与范围

- 实施基线：`59ad21816d079236a6ecf037db984b95cfc1c978`，已重新 fetch，当前分支与 origin/main 无差异。
- 分支：`codex/compact-taskbook-20261031`。
- 直接复用现有公共／执行 planner、投影和恢复。修改限于自动分流、未观察回执保留、执行目标说明及直接配套测试、CI、文档；不新增压缩协调器、状态表、持久格式或经验门槛。
- 本地脱敏测试产物保存于 `test-results/compact/`；生产取证 `.cache/fanout-20261031/` 不提交。
- 上线前只读快照：Bot 为 `desktop-8ab2d12106a8099c1ae628dd35bf8c74411078bf`，健康；SnowLuma、environment、Mihomo 与 sandbox-egress 的身份已取得。该快照不代替部署前的实时核对。

## 实施与验证

先建立失败场景后修改运行时。旧实现已复现普通发送后下一正常 Main 丢回执、未知容量的原生结果丢失，以及换链后旧键被重造；混合批次的已知原键被整批省略也先取得失败证据。每次重跑独立保存 attempt，不覆盖先前失败事实。

实际修改仍沿现有机制：

1. 删除 initial 单独越线才选公共压缩的条件及其重复计量。合法公共规划优先；自动维护规划失败才尝试原执行路径，手动命令仍只公共范围。
2. 区分摘要切点和原结果保留起点，原工具轮可以进入摘要输入，但下一正常 Main 仍收到原事实；未知／已知容量及 raw／native 都按实际保留起点处理。
3. 执行摘要使用程序持有的当前 Work／source event 归属，普通问答明确无当前 Work；公共指令及公开范围不变，不注入私有目标。
4. 在换链前把原键记入既有 Host observation 的 results，只接续最后未观察轮，仍用原不可变观察对象存储。再次冷恢复从原观察生成 portable，不依赖 user 正文标签。旧换链记录没有原键证明时逐项省略该可选键，有证明的项保留；不扫描旧历史或伪造来源。

| 任务 | 实际落实与验证入口 |
| --- | --- |
| T01 | 新 `test_compact_delivery_e2e.py` 沿 Processor、Main 合同、Social 发送、SQLite 与 Chat Completions wire 复现；旧实现已红 |
| T02 | `TurnExecution.compact_current` 复用 P／E，真实 E2E 包含自动 P 失败退 E 与手动不退 E |
| T03 | `execution_compact_plan` 原 raw／native 索引按真实保留起点接续；原生协议、同批多结果和 portable 重复恢复均有实际后续 Main 请求 |
| T04 | 原换窗、layout、公共采用未重写；普通发送后覆盖正文退出，未覆盖 initial 保留 |
| T05 | E 指令和私有检查点接续改认本轮来源；真实背景 Work 的 ID／状态／预算不被新普通问答接管 |
| T06 | Work 双次冷恢复与普通 E 的 response／paired 恢复；原调用、原输出、原键与累计预算按事实接续 |
| T07 | 复用原新输入、source/privacy、generation、公共发布与原生签名／unknown 回归，不另建调度或恢复系统 |
| T08 | 修正原预算夹具与实际指令成本，保有效 E initial／原模型／ID 断言；递减 opaque 场景改用真正能容纳必留尾的预算 |
| T09 | 原 direct CI 的 always 上传加入 `test-results/compact/`；产物含请求、响应、wire、账本／协议及源版本／差异和实际新增测试源快照 |
| T10 | 更新 `conversation-compact.md`、`main-agent-runtime.md` 与架构索引；无新增配置、迁移或独立 planner |
| T11 | 下列交叉复审和各交付阶段分开记录，不以本地通过代替 CI 或生产验收 |

提交前本地验证：

- 新增真实 Main E2E：`7 passed, 0 skipped`；完整定向 compact／恢复回归：`81 passed`。
- 全仓 Ruff 通过；受影响 10 个 Python 文件格式检查通过；Mypy Linux 目标下 `690 source files` 通过。
- 相关协议／恢复扩展检查 `142 passed`。本地完整 direct unit／integration 在 Windows 上启动后，电脑文件测试因平台不提供 POSIX `os.O_DIRECTORY` 失败；首个失败已单独定位，没有为平台差异修改产品或旧测试，已停止不适用的本地全量运行。完整检查交由确切 PR head 的 Linux CI，不将这次运行记为通过。
- `test-results/compact-final-focused.xml`、`compact-delivery-main.xml` 与 `test-results/compact/` 保存本地证据，CI 沿原上传入口留存其实际运行产物。
- 用独立导出的 `59ad2181` 旧源码重放同一个 E01，确认 `1 failed, 6 deselected`：初始估值 90,542、发送后 98,166，旧分流选择 E 后下一正常 Main 丢成功回执。运行时字节与导入路径、测试／helper 快照及三阶段回执均已核验，产物为 `test-results/compact/old-source-replay-d155d3a178/verified-red-summary.json`；记录器前两次环境失败没有算作产品失败。

这些是合成同形场景和协议／恢复验证，不证明真实上游模型质量、缓存命中或生产延迟。生产不主动发送 QQ 测试或购买模型探测。

## 复审与交付

交叉复审已逐项修正：自动 P 失败不能遮住 E、手动不能落到私有 E、portable 双次冷恢复、旧键不可按新链重造、混合批次逐项保已知键，以及本地新增测试的产物源快照。复用原 planner、journal 和观察对象，未增加 seen 表、兼容执行别名、冷却、压缩率或限发门槛。

PR #544 首次 head `b268293e` 的完整 CI 等待期间，继续复审发现硬容量组合缺口，并独立有界复现；这不等于已证明旧 unit 耗时的原因。新增 `test_compact_hard_capacity.py` 先取得两负向失败、两合法分段正向通过：全部旧源已覆盖后仍复购；尚有旧源、但下一候选只容检查点自身时也复购。修复沿原切点保留“是否仍有未覆盖执行前缀”的瞬时事实，并排除只覆盖程序当前已持有检查点的重复候选；不按字数、角色、压缩率或次数判断，不改合法响应的 valid 或原预算，不增加持久格式。修复后四例全部通过，合法已观察 user／assistant 原文仍可分段。

最新补修保留先前 source／paid／unknown 合同；最终合并依据更新后确切 head 的完整 CI，不能沿用旧 head 的结果。

同轮又先红验证 native E 换新链后 P 刚变合法的交接：共享原 eligibility 判断，仅对“旧请求不合法、E 后候选刚合法”的 P 留原硬容量循环机会，未重开本来已失败的 P。新增硬容量文件共五例（两容量负向、两合法分段、一个真实 Gemini E→P），逐项红绿及 source／paid／原参数事实均有产物。旧 run `41196004159` 最终为 cancelled，十项成功结果已保留，未完成的 unit 没有完整日志／产物，不据此推断其耗时原因，也不记录为通过。

补修后定向合跑 `86 passed`（`test-results/compact-final-86.xml`）；Ruff、受影响文件格式及 Mypy `690 source files` 再次通过。新两处 runtime 差异与五个硬容量场景已独立复审，无新增已知缺口。

旧版本若早已丢掉 Host 观察元数据，本轮不宣称补造过去缺失事实；部署准备的只读快照没有非终态 Work。该快照不形成等待所有任务终态的上线条件，部署前仍重新取得实际状态。

本记录对应提交前本地阶段。PR／确切 head CI、合并、精确镜像、健康、自然流量和实际清理结果在交付时分别记录；尚未修改生产。既有 `desktop-image.yml` 在 main 合并后构建精确 SHA 镜像，不为本修复新增发布工作流或正式版本标签。
