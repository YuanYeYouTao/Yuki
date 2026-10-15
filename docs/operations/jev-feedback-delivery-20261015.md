# 记忆种子退役与普通参与反馈交付记录

日期：2026-10-15。实施基线为 Yuki `66cc08ba` 与当时实际固定的参与库 `9e60cb8d`。
范围与逐项验收见[任务书](../architecture/Yuki-记忆种子全链删除与参与状态反馈任务书-2026-10-15.md)。
本记录区分本地验证、固定依赖、CI、合并和生产部署；未发生的阶段不提前标记完成。

## 产品变化

- 后台 Memory 分页、seed 观察、recall/contact 新机会及其专用摘要、去重和参数退出。
  真人观察与 conversation/intrinsic 保留，主 Agent 仍可按需查询 Memory。
- 原已接纳 run、普通 admission、Memory 来源和真实回执继续读取。退役来源只按明确
  fact ID 核验原可见范围、active/verified、有效时间和当前可读证据；不再扫描新候选。
- 原 JEV 请求附带取出时冻结的相关 engage。局部意愿与 SELF 整体意愿分别投影，
  不把 quiet 当真人停止，也不因状态变化新增请求。投影为 `semantic-state-v2`，rubric 为 `v6-zh-5`。
- 普通快接不等待 JEV，同时进入原观察队列复核。有效局部 quiet 与真实语义纠正各自能
  收起后续和未接纳 continuation，已接纳 Main、预算与已确认效果保持原事实。
- `continuation_review_span` 记录未经语义确认的快捷外推跨度；明确请求、原 Work/wait
  和同一 Main 的已选中补充接纳保持及时。新 activation 达到跨度时回到语义入口。
  Main 自己的 stay、发送、unknown 或失败不补充确认进度。
- 普通 admission 的原 `basis_json` 兼容旧列表与含真实 reason 的新对象，原内部事件 ID
  作为本地 source_order、普通自报带原 SourceRef。同时间乱序不靠事件名或响应到达顺序判断。
  未新增业务表或数据库迁移，迁移 head 仍为 `0119`。
- 删除无消费者 mood，旧报告只清退该字段并保留 engage、原身份与顺序。
- 共同固定提示要求日常简短、留出接话空间，同时有进展就报告任务进展。没有后端发送
  次数或字数硬上限。连续五六条的示例收拢为两三条，实际挂载自定义提示另做定点更新。

原有 `allowed_kinds` 采样前过滤来自 Hilbert-beinghappy 的参与库 PR #18。本次从该实际 pin
继续实施并保留原能力；删除与反馈修复按维护者的任务书由 Codex 实施和复审。

## 已取得的本地证据

所有新增模型/语义结果均为隔离合成输入；没有调用付费模型或 QQ 网关。测试中的真实
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

已有 SQLite metadata 排序/反射 warning 仍按各组日志保留，不写成零告警。复核跨度默认 4，
独立库的 192 条回放涵盖约 6 秒延迟、2 秒 tick、两种原队列档位和连续交流/unknown/换题；
90 秒输入与 22 秒排空分开记录。活跃档 5 秒一个输入时 18/18 可快接，高密换题及真实 session
timeout 不续确认。回放不模拟 Main 和语义邀请 promotion，不据此宣称线上最优或模型更准。
最终固定修订的 Host 定向组与 CI 在交付阶段继续记录，不以早期 editable 联调代替最终安装验证。

## 升级和回退边界

生产挂载参数原有 45 项覆盖，只清退 `independent_source_factor`、
`recall_source_decay_seconds`、`contact_source_decay_seconds`，其余 42 项保留，再加入复核跨度。
不以仓库示例覆盖生产自定义提示或其他参数。部署前后记录实际文件摘要，先备份再定点替换。

新 reader 可读旧快照；更旧的严格 reader 不承诺读取新增 DTO 字段。已经产生新消息、
执行或投递事实时，使用兼容新格式的修复镜像，保留新数据库和引用文件。恢复旧备份仅适用
升级后没有需要保留的新事实且符合[备份恢复合同](work-evidence-backup.md)的场景。

## 交付阶段

参与库产品提交为 `d85bb39a5d16e1846390489b95fc3af8bc2f5627`，
PR [#19](https://github.com/YuanYeYouTao/Yuki-Semantic-Participation/pull/19) 的 push/PR 两次
CI 均通过，合并提交为 `65ca174df49cf5fe196334b8888c8a9270ebd374`。
原 PR #18 也随其原提交进入 main 并显示已合并，贡献没有丢失。
Host 已同步固定 pin 与 lock 并完成 154 项归档安装回归，提交/PR、Host 最终 CI、合并提交、确切镜像、完整备份、
Bot-only 替换、挂载资料摘要、健康/OneBot、受保护 peer 身份与自然回执，按实际发生后填写。
健康与测试不等于刷屏已下降；真实 JEV 判断、频率与 cache usage 保持自然样本的范围和缺样说明。

任务书 A01–A04 仍是单独的后续事项，本次不把受托插件自报、子工作者 short_state 装配、
普通局部意愿的自主率公式扩展，或正常裁剪后的首条迟到效果反馈冒称已修复。
