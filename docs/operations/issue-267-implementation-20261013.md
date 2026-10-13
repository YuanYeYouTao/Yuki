# #267 实施与验证记录

分支：`codex/issue-267-auto-reply-debounce`。


## 基线与工作树

2026-10-13 从最新 `origin/main` `c14623c521dab39af702159f0f6460ad87e45730` 开工。
相较 v16 任务书的 `d75a8946`，main 新增 #311：当前材料在触发之前组装。
三条 issue 分支均已快进到该 main，并各自使用独立工作树；旧 3483 的既有源码、任务书、
验收表和 artifacts 均保留。它只从原提交脱离旧 #267 分支，以释放该分支供新工作树使用，
没有移动、清理或重置原文件。

本记录保存提交 PR 前的本地实现和已观察验证；提交、推送及 PR 状态以 GitHub 为准。没有部署。
原日期任务书保留当时的设计与状态；本轮结果由这里及对应 evidence 记录证明。


## 实际改动

真人 keeper 入账后先尝试原 Work/wait，保留 None、有效部分匹配 0 与正 input ID。
原输入不依赖新语义资格、当前 token 或去抖窗口。只有新开放群聊/续聊机会进入原
AutonomousGroup worker；已核验邀请仍即时。删除候选 preflight、全会话 tail/head CAS、
新增的整份 raw 重验与死字段，保留真实来源、代次、人物/Presence 和明示中断合同。

live 与 repair 共用 OrdinaryAdmissionRepository.publisher，ready/admission/wake 同事务。
repair 在 reader 准备真实来源，只接手先前进程 prepare_owner 的未发布输入，不以 120 秒
抢占当前准备。清退只改本次未发布输入；work_input_preparing 沿原 ending/reason 结算。
原 complete/fail 在准备停放期间保留；同一已 fulfilled wait 的旧决定沿 wait_id 撤下。
conversation 发布锚点改为本次实际触发事件，信号保全部 matched 引用；已展示 anchor
不会吞掉 wait_id/reason/conditions 与仍获准的正文。原正文不再被额外 5000/7000 字符帽截断。

## 任务书责任与证据

| 责任 | 实際证据与边界 |
| --- | --- |
| W1–W3、I1–I6：原输入与入口 | 原 wait 优先、部分匹配与即时原输入回归；完整原 runtime/automation/plugin 套件。没有复活 preflight；未逐项穷举原所有传输场景。 |
| W4：迟到低 ID 与信号 | 新真实 SQLite 用例先匹配高 ID plugin，再由低 ID conversation 发布；实际 take_inputs 保原低 ID、两个 conditions、wait_id/reason 与双方正文，已展示低 ID 仍不丢信号。 |
| I7/I8：发布、repair、清退、原控制 | 真 publisher 故障后 ready/admission/wake 全回滚，再提交成功；ready 输入不被 discard。repair 真来源和长正文、当前老时间 owner 不被抢占；complete/fail 与清退两种顺序、fulfilled wait 先于首次结算均通过。 |
| C1/C4/C7/C11：worker | 三碎片合并、零窗口、旧分类不覆盖较新来源、@ 取消未开始来源、窗口末 require_mention 当前值、原 wait 不进入窗口等回归通过。 |
| C2/C3/C5/C6/C8–C10 与 X1–X3 | 相关原套件覆盖现有身份/来源/执行回执正常路径；代码核对没有新增全群最新消息 gate。#268 在自身分支整合产品键；#269 在自身分支整合显式 forward。没有把三个分支的结果冒充已合并的一体发布。 |

## 已执行检查

- Ruff 与 mypy 通过（633 source files）。前端安装、18 项测试和构建通过。
- 全量：1608 passed、1 failed、63 skipped。唯一失败是 macOS 没有 Linux `/proc`，
  原 environment_supervisor 无法把终端子进程回执收为 cancelled；该监督器未修改。
- 用已有匹配的原生 Monty worker/binding 补跑 Code/冻结插件恢复：90 passed。
- 新增发布/清退/worker 相关回归：71 passed；随后补入的真实 wait 来源用例 1 passed。
- main #311 改变当前材料次序，旧测试按 user 文本前缀定位任务而失败；改为定位实际完整 JSON
  再核任务全文，三种长度通过，没有改生产提示或缩短完整指令。

DeepSeek 实际 API：2 次请求，固定 40 工具的 Main 循环；3 条隔离合成消息均
ordinary_deferred，落一份新 admission，模型调用 send_message 后结束。
该初次报告未记录 SocialOperation 状态；MemorySender 的计数 0 不代表 fake Social 网关未发送，
也不能据此宣称真实 QQ 已发送。API/usage 和实际接纳见 [证据](issue-267-evidence/live-api.json)。

自然 QQ、真实 Jev 的分类准确率与长期运行效果没有在本轮验收；没有把 W/I/C/X 全部组合
逐一执行或勾成完整自然验收。现行文档已按真实入口、writer 与恢复行为更新。

## 维护者复审修复

复审以 main `19b7af77` 为准，合入 #312、#316、#317 和 #334，自动化完整指令测试保留
main 的原指令定位与尾标记验证。先前日期记录仍属于原提交的验证。

修复 conversation wait 在转写和记忆入队前发布 ready 的顺序；只在实际匹配来源时，于
reader/writer 之外准备原语音，原 writer 再核来源、代次和等待并发布原接纳。部分等待先保
转写，后到信号读取原事件的 current/reply ASR 标签；引用语音不作为当前人的记忆证据。
开放群聊逐条准备原语音和记忆，再合并新 Main 机会；前条语音不会随最终文字候选丢失。
未匹配且无接话资格的消息不额外买 ASR，取消或代际失效不发布未准备输入。

本地原三个失败探针已转绿，正式来源回归 17 项通过；上下文、共同历史、私有来源、原
Work 结算、统一出口和完整指令的定向验证 166 项通过。Ruff 全仓通过；634 份源码按
Ubuntu CI 的 Linux 平台进行 mypy 核验通过。Windows 默认平台检查的 30 项 POSIX API
缺失属于既有沙箱/工作区文件，本轮没有将其改为假兼容。更新 PR 后的 CI、三份 PR 组合
复审和自然流量分别核验，本段不代表已合并、部署或取得线上缓存指标。

复审期间 main 前进到 `ad913a62`（#318–#329），本分支继续采用新主线的工具失败/未知
回执、当前恢复授权、记忆 ASSERT 提取和超长消息反馈。超长来源不发布原等待信号，
无接话资格且没有原等待的群消息仍只观察；来源及超长输入交叉回归 21 项通过。
