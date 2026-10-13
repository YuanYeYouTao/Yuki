# #269 实施与验证记录

分支：`codex/issue-269-forward-history`。


## 基线与工作树

2026-10-13 从最新 `origin/main` `c14623c521dab39af702159f0f6460ad87e45730` 开工。
相较 v16 任务书的 `d75a8946`，main 新增 #311：当前材料在触发之前组装。
三条 issue 分支均已快进到该 main，并各自使用独立工作树；旧 3483 的既有源码、任务书、
验收表和 artifacts 均保留。它只从原提交脱离旧 #267 分支，以释放该分支供新工作树使用，
没有移动、清理或重置原文件。

本记录保存提交 PR 前的本地实现和已观察验证；提交、推送及 PR 状态以 GitHub 为准。没有部署。
原日期任务书保留当时的设计与状态；本轮结果由这里及对应 evidence 记录证明。


## 实际改动

扩展固定 read_conversation_history 到 schema_version="2"，event_id key 存在则沿事件模式，
只接受真正整数，独立 operation_id anchor 冲突；合法未用 limit/selector 不改来源。
执行处从可信 invocation runtime 取得原 canonical Conversation/generation/读授权，
原 keeper/message 的入站 ingress 或出站 author Presence 决定精确 QQ 账号。
委托读取保同 owner 范围，群不额外因作者 Person 停用而拒绝，原 SELF 两种执行身份保留。

get_forward_msg 固定映射读取原外层 locator；各 segment 保原顺序/位置，同 invocation
同 locator 复用一次读取，多 locator 并行。先核 provider status/retcode，再选首个实际 list；
空 list、标量/null/未知字段保留。部分成功保真实成功组，全失败沿真实 error；不递归读取
内层，不建立人物、记忆或内层事件，不增加节点/正文帽。真实 generation/privacy/owner
失效拒绝新正文，普通文字/ASR/visual 派生与读完暂离线不抹掉结果。

移除入场 expand_forwarded 及 forwarded_inputs.py；真 IMAGE/VIDEO/FILE 在 queue/limiter
前选取，纯 forward 不消耗媒体队列，混合只处理实际外层附件。新采用外层 event 表示留一次
[含合併轉發] 与原内部 ID；原选取与已付请求不追溯改写。
Runner 同批 signature 字典查、写都遵守 cacheable，非缓存新调用分别读，原缓存工具和旧
alias 恢复保留。完整结果继续沿既有预算/artifact/Work/Code 链，不为此功能新增恢复框架。

## H1–H16 责任与证据

| 责任 | 实际证据与边界 |
| --- | --- |
| H1–H7 | 7 类 envelope、空列表、坏旁栏、provider 明确失败、原超旧帽 raw、同 locator/部分组、入站/出站原 Presence、真 generation/privacy、正常派生、停用作者、合法/非法 event 参数和委托 owner 范围回归通过。 |
| H8/H9/H16 | 纯 forward 满队列/limiter 仍返回空媒体；原 event renderer 当前/历史标记通过。DeepSeek 真 Main 按实际内部 ID 读取一次第一层，内层 locator 零读取；原公共材料/媒体套件通过。既有选取与动态材料没有新增回填。 |
| H10 | 实际固定 Main 同批两个非缓存历史 call 分别执行；原跨轮历史读取用例、缓存/旧 alias Code 恢复套件通过。 |
| H11/H12/H13/H14/H15 | 既有完整 tool artifact/授权/Work journal/原生 Code、SELF automation 与 continuation 套件通过；本功能没有改这些通用合同。没有为全部极端存储故障或每种新 event×SELF Code 排列额外创建矩阵。 |

## 已执行检查

- Ruff、mypy（632 source files）通过。前端原 18 项测试、安装和构建通过。
- 全量 1613 passed、4 failed、63 skipped；其中 3 项是 main #311 使旧任务完整性测试的文本
  前缀假设失效，已只修测试按实际 JSON 定位完整任务，复验通过。余下为未改 Linux /proc
  监督器在 macOS 的 terminal_exec 取消用例，没有削弱检查。
- 原生 Monty Code 系列＋新 forward/完整指令回归：125 passed。
- 加入同批非缓存真实 Main 回归后 forward 专项：23 passed。

DeepSeek 实际 API 3 次：read_conversation_history→send_message→内部结束。
fake QQ get_forward_msg 只取原外层 outer-test 一次；内层 inner-not-fetched 没有读取；
Main 向 fake 群网关发送第一层校验词“蓝鲸731”。这是原 Main/工具/结果链的真实模型调用，
不代表真实 QQ 已发送。API/usage/实际网关动作见 [证据](issue-269-evidence/live-api.json)。
自然 QQ、真实网关 envelope 覆盖、超大结果实际峰值与长期生产效果未在本轮验收。
