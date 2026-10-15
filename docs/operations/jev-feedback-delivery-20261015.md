# 记忆种子退役与普通参与反馈交付记录

日期：2026-10-15。实施基线为 Yuki `66cc08ba` 与当时实际固定的参与库 `9e60cb8d`。
范围与逐项验收见[任务书](../architecture/Yuki-记忆种子全链删除与参与状态反馈任务书-2026-10-15.md)。
本记录区分本地验证、固定依赖、CI、合并和生产部署；未发生的阶段不提前标记完成。

af0770fc 的首次上线记录保留如下。后续终审发现同 Main 已选择补充的后置资格竞态，
已以最小删除和真实 Main 回归修复；最终 CI、合并 SHA、运行镜像、新备份与自然验收统一见
[#446 的交付回执](https://github.com/YuanYeYouTao/Yuki/pull/446)，以实际阶段为准。

## 产品变化

- 后台 Memory 分页、seed 观察、recall/contact 新机会及其专用摘要、去重和参数退出。
  真人观察与 conversation/intrinsic 保留，主 Agent 仍可按需查询 Memory。
- 原已接纳 run、普通 admission、Memory 来源和真实回执继续读取。退役来源只按明确
  fact ID 核验原可见范围、active/verified、有效时间和当前可读证据；不再扫描新候选。
- 原 JEV 请求附带取出时冻结的相关 engage。局部意愿与 SELF 整体意愿分别投影，
  不把 quiet 当真人停止；不为状态本身新建评分请求，保留原真人焦点的必要补评。
  投影为 `semantic-state-v2`，rubric 为 `v6-zh-5`。
- 普通快接不等待 JEV，同时进入原观察队列复核。有效局部 quiet 与真实语义纠正各自能
  收起未来和未选择 continuation，已接纳 Main、预算与已确认效果保持原事实。
- `continuation_review_span` 记录未经语义确认的快捷外推跨度；明确请求、原 Work/wait
  和同一 Main 的已选择补充接纳保持及时。选择前仍筛选 quiet/语义；同 Main 已选入序列的
  补充不再因 K 或后到 quiet/语义被 writer 否决；该后置资格门槛已删除。
  原来源与授权继续核验，新 activation 达到跨度时回到语义入口。
  Main 自己的 stay、发送、unknown 或失败不补充确认进度。
- 普通 admission 的原 `basis_json` 兼容旧列表与含真实 reason 的新对象，原内部事件 ID
  作为本地 source_order、普通自报带原 SourceRef。同时间乱序不靠事件名或响应到达顺序判断。
  未新增业务表或数据库迁移，迁移 head 仍为 `0119`。
- 删除无消费者 mood，旧报告只清退该字段并保留 engage、原身份与顺序。
- 共同固定提示要求日常简短、留出接话空间，同时有进展就报告任务进展。没有后端发送
  次数或字数硬上限。连续五六条的示例收拢为两三条，实际挂载自定义提示已在备份后定点更新。

原有 `allowed_kinds` 采样前过滤来自 Hilbert-beinghappy 的参与库 PR #18。本次从该实际 pin
继续实施并保留原能力；删除与反馈修复按维护者的任务书由 Codex 实施和复审。

## 验证证据

以下本地测试使用隔离合成输入；没有调用付费模型或 QQ 网关。测试中的真实
Host/Main 指实际业务服务、原接纳仓库和 Main runner，不表示对线上模型准确率的实测。

| 范围 | 证据与结果 |
| --- | --- |
| Memory、SELF 来源与自动化 | 精确来源、SELF 自动化和 initiative Memory 三组共 55 passed；含权限、证据撤销、有效时间与大量明确 ID |
| 普通接纳与 #439 | 原 unit/wire 27 passed；原输入、activation 和 steer/排队保持一次接纳 |
| 首轮 Host 反馈 | ordinary feedback、direct、scope、产品开关及新增 pipeline 共 57 passed |
| 新恢复和顺序接线 | origin、pipeline、legacy run、旧 mood journal 与真实普通 callback 最终组 20 passed；包括 17 项新增行为与 3 项现有 callback/兼容回归，补齐无真人 tick 不读 Memory、旧普通 basis 含 Memory 与在途来源撤回 |
| Main/JEV 并行双退出 | 新 integration 2 passed；实际 Main runner 网络等待期间语义结果可先关闭快路，自身 quiet 也可先于在途正结果关闭；原快照不变，不重购 Main、不重发 |
| 旧参数热加载与保存 | 新热配置回归 1 passed；保留 source_interval=45 等实际覆盖，移除三个退役键，K 可热更、旧 revision 仍冲突，未知拼写仍拒绝 |
| 上下文与恢复不变量 | 当前资料、公共 Compact、检查点和原 Work 恢复 34 passed |
| 真实旧控制器隔离升级 | 29/29 份只读捕获快照经严格解析、restore、session checkpoint、CAS 保存及重读；移除 40 条 seed 正文，保留 246 条 human/self 文本、14 份自报、238 个单元的读取身份及 1 份已接纳 proposal；源备份摘要不变 |
| 工具合同 | 六个 Social 工具顺序、schema 和 result_cacheable 与基线一致；仅 send_message 描述更新。完整声明的 revision 因描述变化预期更新，不能声称 hash 不变 |
| 前端 | Node 24 下 6 个文件、27 tests passed，构建通过 |
| 静态检查 | Host Ruff 全量通过；Mypy 按 CI 的 Linux 平台检查 606 源文件通过；库 Ruff check/format 36 文件及 strict mypy 12 源文件通过 |
| 参与库最终全套 | 235 passed，0 skipped；包括同时间反馈、旧 State 顺序补读与在途请求原件保持 |
| Host Linux 全量 | 实施中版本 1964 passed、94 skipped，1808 个原警告，1339.37 秒；未构建的 Monty 原生运行时与缺少 PowerShell 按环境跳过，最后 metadata/来源补修另有定向回归，最终 CI 按固定版本全套验证 |
| 固定依赖安装 | pyproject/uv.lock 固定公开归档 d85bb39a5d16e1846390489b95fc3af8bc2f5627；frozen sync 完成，已安装 12 个库模块与提交逐字节一致，29 份旧快照再次升级通过 |
| 固定 pin 的 Host 定向回归 | 原接纳、#439、普通与 SELF 反馈、退役来源、真实并发、热配置共 154 passed，154 个原 SQLite warning，112.89 秒；使用公开归档安装，无 editable 覆盖 |
| Host #445 Quality | PR [#445](https://github.com/YuanYeYouTao/Yuki/pull/445) 的最终 CI 通过：[1975 passed、90 skipped、1815 warnings，1238.34 秒](https://github.com/YuanYeYouTao/Yuki/actions/runs/38748817435)。属于首轮历史证据；最终修复当前 CI 状态见 [#446](https://github.com/YuanYeYouTao/Yuki/pull/446)。与实施中全量和固定归档定向分别记录，不相加作独立用例总数 |
| F09 最小竞态修复 | 73 项相关回归通过；实际 Main/发布器回归文件 4 例全过，其中 2 例先在旧源码复现失败、修复后转绿；Mypy 606 源文件与 Ruff 全绿。两组测试分别记录，不相加作独立总数 |

已有 SQLite metadata 排序/反射 warning 仍按各组日志保留，不写成零告警。复核跨度默认 4，
独立库的 192 条回放涵盖约 6 秒延迟、2 秒 tick、两种原队列档位和连续交流/unknown/换题；
90 秒输入与 22 秒排空分开记录。活跃档 5 秒一个输入时 18/18 可快接，高密换题及真实 session
timeout 不续确认。回放不模拟 Main 和语义邀请 promotion，不据此宣称线上最优或模型更准。
#445 固定归档的安装回归与 Quality 已通过；早期 editable 联调只保留为开发过程证据，
不代替固定安装或 CI 验证，也不代替 #446 修复后的新验证。

## 升级和回退边界

生产挂载参数原有 45 项覆盖，已清退 `independent_source_factor`、
`recall_source_decay_seconds`、`contact_source_decay_seconds`，其余 42 项保留，再加入复核跨度。
本次保留生产自定义提示和其他参数，先备份再定点替换，并记录前后文件摘要。

新 reader 可读旧快照；更旧的严格 reader 不承诺读取新增 DTO 字段。已经产生新消息、
执行或投递事实时，使用兼容新格式的修复镜像，保留新数据库和引用文件。恢复旧备份仅适用
升级后没有需要保留的新事实且符合[备份恢复合同](work-evidence-backup.md)的场景。

## 交付阶段

参与库产品提交为 `d85bb39a5d16e1846390489b95fc3af8bc2f5627`，
PR [#19](https://github.com/YuanYeYouTao/Yuki-Semantic-Participation/pull/19) 的 push/PR 两次
CI 均通过，合并提交为 `65ca174df49cf5fe196334b8888c8a9270ebd374`。
原 PR #18 也随其原提交进入 main 并显示已合并，贡献没有丢失。
Host PR [#445](https://github.com/YuanYeYouTao/Yuki/pull/445) 的最终 Quality 已通过，
并于 2026-10-15 14:31:35 UTC（台北时间 22:31:35）合并，提交为
`af0770fcd54a5fad78771ae02004f1f483d8eaad`。公开归档的固定依赖安装回归、最终 CI 与合并分别记录。

首轮产品已完成合并和上线初验，最终修复交付见 [#446](https://github.com/YuanYeYouTao/Yuki/pull/446)。生产 Bot 于
2026-10-15 14:57:12.583544935 UTC（台北时间 22:57:12）启动合并版本；只替换 Bot，
四个受保护 peer 的身份未变化。后续自然流量的长期效果与缺样范围另行保留。

## af0770fc 首次上线记录

| 项目 | 实际记录 |
| --- | --- |
| 产品提交 | `af0770fcd54a5fad78771ae02004f1f483d8eaad` |
| 运行镜像 | `ghcr.io/yuanyeyoutao/yuki-qqbot:ops-af0770fc` |
| 镜像 ID | `sha256:b9074e0bedb582f267dac12ebe5cac4398d2354097aa02f2d08340b81e31586a` |
| 打包验证 | 618 份 Python 源文件匹配，3.9.0、43 项参数、K=4；direct 包装验证通过，Code 模式未启用 |
| 镜像归档摘要 | `e7c6ced976bc64e757b62a61a2f3ef7d22086e1e70bd9f4f2ec5429bdc4e35c3` |
| 成功操作目录 | `/opt/yuki-qqbot/ops/jev-feedback-af0770fc-20261015T1448Z` |
| 运行健康 | health/database 为 ok，OneBot 已连接；Work、wait、maintenance、subagent 错误类别均为 null，部署后抽取的错误类别为空 |
| 主工具声明 | 56 个工具，direct；实际 manifest revision 为 `99e9956957652fd6de4424734a236e1767b2d7e59f267fa41c161b5c8b66a26d` |

首次 `1435Z` 尝试在完整归档后，用目标镜像只读挂载校验 WAL 备份副本时遇到
`OperationalError: unable to open database file`。helper 在替换前自动重启原 Bot，
未替换镜像或挂载文件。准备脚本已将独立备份副本整理为 DELETE journal 并关闭；真实只读
挂载 fixture 复现旧失败并验证修复通过。随后 `1448Z` 尝试重新停止 Bot，采集最新一致备份后再替换。
备份没有冻结 peer 服务，外部 workspace 副本不宣称全环境原子一致。

成功备份在目标镜像内通过 integrity_check，foreign_key_errors=0、schema=`0119`，
备份包含 30 份 controller 快照，18,343 份协议引用和 34 份工具引用通过核验。
备份数据库的 journal 为 DELETE，在线数据库继续使用 WAL。成功备份的文件摘要如下：

| 文件 | 字节数 | SHA-256 |
| --- | ---: | --- |
| `qq_ai_bot.db` | 1,981,579,264 | `af326aa41d32a5c6dc9861a50d2c615057c1f39981cc4a4647e0e2bd4894bc65` |
| `participation.sqlite3` | 5,369,856 | `17b7c39cd56f51496388e249b7b2a28e69b32bec0c10c309e8a23b2c156689b5` |
| `bot-state.tar.gz` | 2,141,138,314 | `08584d656e18d0416ef41c7b7bdf99b3a8806e84cff021906feffa05b4442f76` |

启动后与该备份逐项比较，原 544 个 Work、531 份预算、114 个 inputs、704 个 effects、
612 个 delivery intents 和 22,485 份 Social 回执均保留原身份与计数。累计用量没有下降，
预算上限未改，已有 JSON 和回执事实没有变化。

成功后仅清理首次 `1435Z` 尝试的重复 backup，共 4,987,859,814 字节；成功的 `1448Z`
备份保留。清理后可用空间为 8,228,519,936 字节。

## 挂载资料、实际配置与前端

| 挂载文件 | 更新前 SHA-256 | 更新后 SHA-256 |
| --- | --- | --- |
| `webui-config/system_prompt.md` | `97e70ad55b8c2974327e44dbe130db78b9341813d9db331ee94999b5f9eba9af` | `f73a6b549ae2436735b1598dd5a1b85dd80b208c4363fd6f71cdc3fe0bd74a82` |
| `webui-config/autonomous-model.json` | `172ffb18a846a93686da5bc17f069055cd57fa83d8a6de7006fd19fab55261c0` | `dd10338dcbe02762355873f6881ea384d5a2f6e63a2c2950fc6f6095b6f59671` |

通过运行中服务的真实鉴权只读 API，确认参数有效且 matches_loaded=true：schema 和已载入
参数均为 43 项，`continuation_review_span=4`、`source_interval_seconds=45`，退役键均不存在；
参与服务正在运行，实际 model_profile 为 `dd10338dcbe0`，model_config_error=null。
该证据来自实际进程查询；临时鉴权会话已注销，没有发起配置命令、模型或 QQ 测试。

25 份前端文件在目标镜像内、运行容器内以及运行中 HTTP 返回中均匹配审阅清单，
清单 SHA-256 为 `396ae159df321cde1230740cbbddbf3bb41f2b34d362c04c2481636de3689bb4`。
enabled 群组当前代次的 3 份可读 controller 快照没有 seed 正文或退役键；其他休眠、旧代次载体按历史保留。

## 首窗自然流量与未知项

观察窗口为 2026-10-15 14:57:12.583544–15:00:50.156132 UTC，起始 trace 水位为 244897。
自然流量保留 2 份 Social 成功回执和 2 次成功 Main 调用；回执数不冒充 QQ 可见单条消息数。
两次 Main 均报告 prompt tokens，合计 45,109；其中只有一次同时报告完整缓存配对：
prompt 22,595、cached prompt 20,499，另一次 cached 字段缺失。仅报告该已知样本，
不将已知缓存 token 除以两次总 prompt 当作总体命中率。

本窗 JEV 观测保留样本为 0，实际 Snapshot、JEV usage 与判断表现未知。
健康、部署和测试不证明刷屏已经下降，也不证明模型语义更准；长期接话频率、真实 JEV 反馈
和缓存效果仍需自然样本，缺失字段保持未知。

以上生产证据保存在本地 `.cache/jev-deployment-receipt/{before,after,verified,materials-applied}.json`、
`jev-image-package.json`、`jev-postdeploy-consistency.json`、`jev-postdeploy-frontend.json`、
`jev-postdeploy-profile.json`、`jev-postdeploy-observation-first.json`、
`jev-backup-ro-validation.json` 和 `jev-aborted-artifact-cleanup.json`。

## 终审补充：删除已选择输入的后置资格门槛

独立源码终审发现，af0770fc 的 #439 补充路径仍把 continuation 资格回调留在发布 writer：
A 已付模型请求并选入 C 后，后到 quiet 或语义纠正可否决 C，并使 A 抛出
`HistorySourceChangedError`。这违反了已选择输入和在途 Main 的边界。

修复只删除该后置资格检查。选择前和新 activation 继续使用当前快捷资格；同一 Main 已选择的
C 沿原序列唯一提交，K、后到 quiet 和语义纠正不再成为本轮发布门槛。原来源、身份和授权
仍按事实核验，不新增恢复、重试、丢 C 后重装配或协调链。73 项相关回归与实际 Main/发布器
回归文件 4 例通过，其中后到 quiet/语义两例在旧源码失败、修复后转绿。

只读自然反馈补充保存在 `.cache/jev-natural-feedback-acceptance.json/.md`。其中
245092–245094 的 `TurnSupersededError` 只有类别，没有具体 epoch/lease/source 原因；同焦点
JEV 的开始晚于错误。该线上样本保持 unknown，不能直接归因为本竞态或 quiet 回归。
最终修复的当前 CI、合并提交、运行镜像、新备份和自然验收由
[#446 交付回执](https://github.com/YuanYeYouTao/Yuki/pull/446)按实际阶段记录；本节的 af0770fc
运行记录与 #445 CI 仍是首轮历史证据。

## 独立后续

任务书 A01–A05 仍是单独的后续事项，本次不把受托插件自报、子工作者 short_state 装配、
普通局部意愿的自主率公式扩展，或正常裁剪后的首条迟到效果反馈冒称已修复。

A05 的只读证据位于本地 `.cache/jev-a05-diagnostics-20261015.json`。旧 Bot 在
2026-10-15 14:12:16–14:27:51 UTC 保留 27 条带 observation_id 的
`semantic_observation_end`，其 `record_failures` 合计 54；同窗日志明确记录
`diagnostic_dropped`：`semantic_observation_start` 27、`semantic_result` 27，另有
`provider_start` 10。两种语义记录的丢弃数恰好对应 54，`execution_trace_write_failed`
和 `diagnostic_commit_failed` 日志均未出现。这证明已有诊断接纳丢弃，不能把缺样当作
零次 JEV 或执行失败。日志未记录丢弃原因及当时占用，无法区分队列、字节预留或结构界限；
该旧窗口缺失的 Snapshot、实际 profile 与 usage 均保持未知。四个相关诊断源文件从实施基线至
产品合并未改；本次未修复该独立可观测性问题，也不以此新增上线等待。
