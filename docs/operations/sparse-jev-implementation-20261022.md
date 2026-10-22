# JEV 稀疏复核与闲聊收束实施记录

日期：2026-10-22。对应[任务书](../architecture/Yuki-JEV稀疏复核与闲聊收束任务书-2026-10-22.md)。

授权范围：实施、验证并提 PR；不合并、不部署、不发送真实平台测试消息。`send` 的工具描述、schema、发送链及统一出口保持现状。

## 实现

- 普通自动续聊保留，删除未核验跨度额度及 `check_budget` 双路径；保真实 `continuation_refs`、已接纳身份与纠偏来源。
- 后台将已唯一匹配单元的普通续聊复核合并成最新待办，未匹配来源与优先焦点独立保留。原在途 Snapshot 不变，失败不复活被替换的旧待办。统一复用 3/12/20 秒档，删除活跃时自动加密。
- quiet 不再主动申请 JEV。对本体实际已读的同 activation、同讨论、当前同人来源，扩充原 `hint_basis`，清理已被意愿覆盖的旧复核；原自报触发源、时间、actor 不改。仅排队、未实际派发或另一个人的输入不取得覆盖证明。
- 语义开启时开放群复用普通隐式资格；关闭时保原开放群行为。排队的续聊和语义邀请均重新确认当前机会，writer 仍核当前开关与原资格，防止旧邀请绕过较新刹车。
- 有/无 Work 的正常模型请求经共同 Backend 读取局部参与资料。首次和原运行资料共同前置，之后仅追加有事实的尾部；保 Gemini 思考签名、原工具配对、已付请求与固定声明。pause 和首次精确恢复不插入资料，不为反馈另开模型轮次，不新增业务 mailbox 或发送 veto。
- 参与资料只读原 admission 及当前同人关系，投影相关 SelfReport、UnitSemanticReview 和未释放 Boundary，不获取 JEV 应用锁、不 hydrate、不写快照、不扫永久历史。自身意愿的 basis 保留实际覆盖证明，避免把原触发 A 的 quiet 错当成尚未看过 B。
- 默认 SOUL、系统约束与 JEV 问句按自然收束修订；保公开信息查证和真实任务实质进展必须报告。JEV 固定问句使用当前角色名称展开，协议键及真实历史姓名不改。
- 人物合并只改变当次资格和边界的当前同人投影。新邀请及其来源撤回后的边界重算使用同一份当前身份，不改原作者、停止来源或回执，避免已合并人物一边唤起、一边仍被旧人物的局部停止收起。

## 升级与依赖

Host 基线为 `cdb49eadc0c7cd16060bf0329234ae69611e62bd`，原参与库 pin 为 `438f204f9ebe2b8c1b698b68c0f1b3ac8b4facef`。新 pin 为 `addfcddecb09f93ad8846db508879c451bc4bc47`，对应[参与库 PR #25](https://github.com/Institute-of-Agent-Life/Yuki-Semantic-Participation/pull/25)。Host 的 pyproject、lock 与实际安装归档一并核对，不依赖 editable 安装。

旧参数清退 `continuation_review_span`，其他有效配置保留。旧 checkpoint 的 fast input 来源转换为 `continuation_refs`，清退配额 sequence，保原单元、quiet、语义判断、已知顺序与回执。本地旧 Snapshot 的废弃配额元字段可清退，原已付 HTTP 正文和签名不改；不宣称旧本地 JSON 字节完全相同。

## 审查修正

| 轮次 | 实际检查与修正 |
| --- | --- |
| 一 | 对照当前 Host 与实际参与库 pin，拆分直接入口、原 Work、普通激活和 JEV；不删除快路 |
| 二 | 分别实现并交叉核对队列/恢复、Main 正常请求、提示词/名称；删除旧额度与 quiet 补评入口 |
| 三 | 修正同时间 source_order、同 Main quiet 覆盖补充、跨作者讨论停止的反馈、旧语义邀请排队后重验 |
| 四 | 核对人物合并后的原 actor 与当前同人、hint 幂等和实际派发覆盖；修正合并后新邀请无法释放原局部停止、撤回后重算丢失同人释放依据的问题；不靠全群最大事件号清场，不增加发送门禁 |
| 五 | 原 pin 与正式新归档联测，检查报告是否把 observer 当成 Main 或付费效果；清退 send 删除清单残留，保留调用到期与采样延迟。真实 SQLite 回归补齐未接纳邀请和仅剩 Boundary 的人物解析；核对 ACP 测试代理环境泄漏，修测试隔离，不改生产代理 |

## 验证边界

正式新归档依赖下，Host 最终受影响集合 **239 项通过**（172.74 秒），覆盖全部参与测试、普通交接/防抖、Main 发送观察、当前激活资料、Work SOUL 恢复，以及 ACP 环境隔离后的全部 client continuation 和 SOUL HTTP 测试。参与库最终 **276 项通过**，对应提交的两项 GitHub CI 均通过。定向集合互有重叠，不与此前 85、26 等中间结果相加。

Host Ruff、Linux 平台目标 Mypy（647 个源文件）、`uv lock --check`、`git diff --check` 和 6 份文档的本地链接检查通过。最终 Host 测试有两条 warning：既有 SQLAlchemy 表排序循环，以及 aiosqlite 测试线程在事件循环关闭后的回调；未掩盖为无 warning，也未以此修改生产生命周期。

可重跑参与及交接集合，再附 ACP/客户端文件：

```powershell
$targets = @(rg --files tests/unit tests/integration | Where-Object {
    $_ -match 'test_participation|test_open_group_semantic|test_ordinary_input_handoff|test_auto_reply_merge|test_main_agent_send_observation_boundary|test_current_activation_material|test_soul_work_recovery|test_acp_adapter|test_client_continuation|test_soul_reference_server'
})
uv run --no-sync pytest @targets -q --strict-markers
uv run --no-sync ruff check .
uv run --no-sync mypy --platform linux src
```


新增回归覆盖稀疏调用、原快照恢复、失败/取消与较新焦点、真实双路刹车、旧邀请失效、实际已派发输入证明、多人/多讨论/同人合并，以及真实 Gemini adapter 的固定前缀、签名与 function response 配对。

真实 Session 到合成 observer 的匿名时钟回放同时比较原版、仅队列合并、完整稀疏三版本；它度量实际 observer 启动次数，不购买模型。各版本使用同一份匿名来源和模拟时间，模拟单次观察耗时约 6 秒、每 2 秒调度、最后继续排空 90 秒。

| 场景 | 来源数 | 原版调用 | 只合并队列 | 完整稀疏 |
| --- | ---: | ---: | ---: | ---: |
| 突发续聊后停下 | 12 | 10 | 1 | 1 |
| 持续续聊 | 90 | 20 | 6 | 5 |
| 零星续聊 | 3 | 3 | 3 | 3 |
| 独立来源 | 10 | 10 | 10 | 9 |
| 本体 quiet 已覆盖续聊 | 12 | 10 | 1 | 0 |
| 一次观察超时 | 30 | 15 | 7 | 4 |
| 交流对象转向别人 | 30 | 17 | 16 | 10 |

转话题样本从发生变化到真实控制器收起快路分别为 **12、16、20 秒**。完整稀疏的独立来源有 **1 条触及既有 75 秒有效期**，未按续聊合并，但也没有得到观察；转话题组另有 6 条到期。调用减少同时来自合并、较稀疏调度及来源到期，不能统称为算法节省。持续续聊原版另有 55 条到期、15 条被原队列上限丢弃，完整稀疏这组两者均为 0。

这份回放不运行 Host 的 Main、实际发送或由旧 Host quiet 新增的补评；不能把 `fast_admitted` 当作 Main 请求数、普通激活数或发送条数。真实 token、缓存和语义准确率未测。固定版本的[原始数据](https://github.com/Institute-of-Agent-Life/Yuki-Semantic-Participation/blob/addfcddecb09f93ad8846db508879c451bc4bc47/docs/evidence/sparse-review-2026-10-22.json)与[回放脚本](https://github.com/Institute-of-Agent-Life/Yuki-Semantic-Participation/blob/addfcddecb09f93ad8846db508879c451bc4bc47/scripts/replay_sparse_review.py)可复核。

开放群的入口成本另用真实 MessageProcessor、Conversation worker、SQLite admission 和 Fake Main 核对。两版均输入同一条普通消息，语义开启，资格分类器返回没有当前邀请或续聊，Main 若启动则返回 `NO_REPLY`：

| 群模式 | 版本 | ordinary admission | Main 请求 | 发送 | 资格分类调用 |
| --- | --- | ---: | ---: | ---: | ---: |
| 无需提及 | 原 main | 1 | 1 | 0 | 0 |
| 无需提及 | 本次实现 | 0 | 0 | 0 | 1 |
| 需要提及 | 原 main | 0 | 0 | 0 | 1 |
| 需要提及 | 本次实现 | 0 | 0 | 0 | 1 |

这验证开放群不再把暂无机会的普通消息直接交给 Main。此处资格分类使用 stub，**JEV provider 请求未测（null）**，不能据此宣称总模型成本下降；JEV 调度启动数由上方真实 Session 回放单列。已有续聊和新邀请仍可在原普通防抖后形成一次 admission/Main 请求；明确 @ 保即时入口。关闭语义时保原开放群行为。

少调用可能延后语义纠偏。未匹配的独立来源不合并，但较稀疏调度仍可能触及原 75 秒来源有效期；应分别报告到期与替换，不能声称每条邀请都得到评分。Main 自身 quiet 是独立刹车，不保证零延迟。

SQL 读取按原 admission 主键、当前人物批读及当前有限参与状态进行；调度键替换平均 O(1)，take/清理仍随当前待办数 P，身份准备与资料投影随当前有限事件 E、边界 B、参与单元 U 和相关来源 R，不宣称整条链 O(1)。不新增历史全扫或 SQLite writer 中的模型/网络等待。

完整非 native Host 回归曾得到 3,979 通过、33 失败、136 跳过、93 按 marker 排除（源码仍在实施期间）。33 项中 5 项为本轮参与变更的旧断言，已逐项修正；其余 28 项使用原 main 与原依赖核对，其中 14 项缺 POSIX `fcntl`、8 项为 Windows 预览长路径错误、6 项来自既有 ACP 单测遗留代理环境。四类代表在原基线均可复现；代理泄漏已在原测试内隔离，并同序重跑 ACP、SOUL HTTP 和 CLI。未把这次旧全量结果称为最终全绿，最终归档下的受影响集合单列。

Windows 默认平台的 mypy 会报既有 Linux 专用 API 缺失；本轮使用 `--platform linux` 核对与正式 Linux CI 相同的类型目标。自然群聊收束、真实 JEV 语义准确率、线上延迟、token 和缓存命中率仍未验收，不从合成调用次数推算。

## GitHub CI 跟进

实现提交 `56b96ef7` 的静态检查、3337 项单元测试和 821 项集成测试均通过。Native 首轮未进入构建：APT 通过 `mirror+file:/etc/apt/apt-mirrors.txt` 继续选取 Azure，95 MB 依赖下载持续约 20 分钟；原工作流仅替换 `ubuntu.sources` 中的 Azure URL，没有命中镜像列表 URI。单独重跑 Native 后进一步定位原因，没有将取消的作业当成通过。

测试矩阵和 Native 的两处安装脚本直接将 Ubuntu 的 `URIs:` 指向官方 HTTPS 源，删除无效的字符串替换，不增加镜像选择、条件分支或失败兜底。该 CI 修正提交仍需在 GitHub 完整检查通过后合并；不以旧提交的通过结果替代新提交 CI。

## 交付

Host 分支为 `codex/sparse-jev-runtime-20261022`，以固定新参与库提交交付；配套[参与库 PR #25](https://github.com/Institute-of-Agent-Life/Yuki-Semantic-Participation/pull/25)保持开放。Host GitHub CI 由该分支 PR 运行，不以本地 Windows 回归代替 Ubuntu 完整检查或 Native 检查。

本轮未合并、未部署，生产配置与 SOUL 原件未修改，未发送真实平台测试消息。
