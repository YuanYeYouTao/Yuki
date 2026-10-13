# #268 实施与验证记录

分支：`codex/issue-268-group-wakeup`。本分支已整合 #267 的实际 ordinary/Work 必要接点，
所以其差异包含 #267 依赖；后续评审/合并应先接 #267，再接 #268。


## 基线与工作树

2026-10-13 从最新 `origin/main` `c14623c521dab39af702159f0f6460ad87e45730` 开工。
相较 v16 任务书的 `d75a8946`，main 新增 #311：当前材料在触发之前组装。
三条 issue 分支均已快进到该 main，并各自使用独立工作树；旧 3483 的既有源码、任务书、
验收表和 artifacts 均保留。它只从原提交脱离旧 #267 分支，以释放该分支供新工作树使用，
没有移动、清理或重置原文件。

本记录保存提交 PR 前的本地实现和已观察验证；提交、推送及 PR 状态以 GitHub 为准。没有部署。
原日期任务书保留当时的设计与状态；本轮结果由这里及对应 evidence 记录证明。


## 实际改动与两仓来源

新增 auto_reply_enabled/proactive_wakeup_enabled：Settings/env/spec/runtime/snapshot 一致，
global/group、HOT、默认 true，unset 回继承。普通新接纳与 SELF 新 run 的 writer 读当前
产品键；existing/payload 对账先行，原 Work/wait/repair、明确 @/引用/命令及定时工作不加 veto。
Space.autonomous_enabled 继续作为新 SELF 场景资格。flags 不改变 binding epoch。

canonical inspect_configs 先解析 canonical key，沿指定 session 只查所需 global/owner rows；
空 keys 不扫描全表。owner 存在性、secret/protected、版本、saved/effective/restart 保留。
设置页按选定 scope 与 saved row 区分本地覆盖；继承值新建用 0、本地 edit/unset 用 row.version，
没有本地 row 时不能 unset，false/0 沿原 DTO 保留。修正原 autonomous/semantic/去抖文案。

参与库基线 `63522288e9829fc71dce955e041d5ae49f919c5a`；新增 allowed_kinds 在原
_eligible_groups/opportunity_scores/advance 的分组和率之前排除种类。
None 保原全部来源，空集不授权来源；intrinsic 由原独立 flag 控制。候选、反馈与原采样
时间继续演化，禁用时间不补抽。Host 先用当前 scene/runtime 决定 promotion、seed 与采样种类，
再由原新接纳 writer 裁决当前产品键；pending 依原 discard 返回值及原 proposal/run 对账。
停用群新增刷新曾错误读取群在线配置，已修回原全局策略关闭/收尾路径。

用户明确批准发布后，经标准 fork 发布同一提交
`9e60cb8d59fbab4cacee9af2fbe1ed2932d68c80`，上游
[PR #18](https://github.com/YuanYeYouTao/Yuki-Semantic-Participation/pull/18) 已开、尚未合并。
终端账号无上游写权限，连接器也不具文件写授权；未更改权限或切换账号。
上游原仓的该 SHA archive 已真实下载（HTTP 200、含新接口），pyproject 已更新，uv 生成 lock；
lock 仅新 URL/source/hash 三处变化。uv sync --frozen --all-extras 已安装该正式来源，
不再依赖临时 /tmp 库。补丁为无上下文 diff，应用时用 git apply --unidiff-zero；原/新 SHA、archive hash 和 installed-source 均在
[来源记录](issue-268-evidence/participation-source.json)及 evidence 中。

## F1–F15 责任与证据

| 责任 | 实际证据与边界 |
| --- | --- |
| F1/F2/F4 | 四组 flags、canonical alias/重复键、空 keys、不同 owner、停用 owner、传入 writer 通过；原配置解析/secret/restart 套件覆盖未改合同。 |
| F3 | SettingsPage 两种 scope row 回归通过；真实 ASGI→ControlCommand→SQLite 验 global v5 群继承、新建 v0、并发建立冲突、local v1→v2 edit、unset 回 global。 |
| F5 | 独立库 203 passed、6 optional observer skipped；新用例验分组/rate 前过滤、None/空集、时间推进和重开，ruff/mypy 12 source files 通过。 |
| F6/F7/F8/F9 | 真 SQLite/barrier 验开关关闭与 ordinary 接纳两种提交先后，原 admission 重入不倒销；四组普通 flags 与明确调用、四组实际 SELF Host intrinsic 接纳通过。原 publisher 不接产品 predicate；相关原权限/场景套件通过。 |
| F10/F11/F12/F14/F15 | 原 scope 停用/恢复、反馈/outbox、SELF lifecycle、generation、legacy/semantic 和调度套件通过；本轮没有穷举所有种类×开关×外部延迟排列或真实 Jev 观测。 |
| F13 | 真已发布 SHA、archive 下载、uv lock/frozen、installed-source 已核对；上游 PR 尚未合并，Yuki 分支未推送。 |

## 本地验证与 API

最终 Ruff、mypy（633 source files）通过；前端 20 passed，构建通过。
首次全量 1600 passed、8 failed、63 skipped，其中 7 项为实际停用读取或旧测试合同；
修复后相关含原生 Code/插件恢复 179 passed，追加后最终普通/配置/等待/停用回归 124 passed，
完整四组 ordinary/SELF flags 12 passed。正式 pin 后全量（含原生 Code）：1682 passed、1 failed，无跳过；
四组实际 SELF 新用例另在完整 flags 12 项回归中通过，未把重叠测试次数相加。
失败仍为下述 macOS /proc 原监督器问题，详见 pinned-full.log.gz。

DeepSeek 共 11 次真实请求，分四次记录：初次合并 2 次、0.05 秒短窗口 5 次、1 秒窗口 2 次、
正式 pin 复验 2 次。短窗口运行中第一机会已开始后下一消息到达，产生两份 admission；
这保留在报告中，符合“合并尚未开始机会”的合同，不能声称此尝试三条只开一轮。
1 秒窗口及正式 pin 场景均验证关闭 auto/proactive 时请求/admission 增量 0；重新开启 auto、
保持 proactive=false 后三条新输入落一份 admission，实际 Main 调用 send_message，
fake 网关 1 次、Social 回执 succeeded。见 [正式 pin API 证据](issue-268-evidence/live-api-pinned.json)。
所有请求走真实 TaskModelExecutor/Profile/ModelClientPool，SQLite 和 QQ 传输为隔离合成环境；
密钥从项目实战的 deepseek.md 在进程内读取，没有放入源码、报告或 PR。
原生绑定用现有匹配 worker/binding 复验，未修改依赖锁来加入测试用原生包。

自然 QQ、真实 Jev 准确率、长期线上效果仍没有验收。唯一已知本机全量失败是未修改的
Linux /proc 监督器在 macOS 无法收取 terminal_exec 子进程取消；没有跳过或削弱该测试。

## 维护者复审同步

同步 #313 的原语音、记忆入队和等待发布修复，并合入 main `19b7af77`。全局/群的两个
开关、原 run/Work 的恢复及独立库 `9e60cb8d` 的真实 pin 保持；修复不把开关写进固定
模型工具声明，也不改变共同公共历史。开关、来源准备、普通反馈、scope 生命周期及配置
读取的本地定向验证 67 项通过，Ruff 与 Linux 平台 mypy 全仓通过。三 PR 组合、更新后
CI 与自然缓存指标分别核验，旧日期验证保留原基线。

继续同步 #313 对 main `ad913a62`（#318–#329）的冲突处理，保留超长消息原反馈以及新
主线的回执与授权行为。正式 pin、四组开关、逐条来源准备、超长输入和配置快照的交叉
验证 50 项通过；Ruff 与 Linux 平台 mypy 全仓通过。
