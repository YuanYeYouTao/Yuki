# “工作开始说明尚未确认送达”调查

2026-10-07（Asia/Taipei），用户授权只读调查，Genie 退役继续独立推进。
线上 Bot revision 为 `964dadef1e72e862bf543b7caba800420be2e040`。未改生产配置、数据库、服务或恢复状态，未调用模型/QQ，也未修补工作汇报代码。

这句话是后端固定的暂停通知。`activation_outcome.py:92` 将 `WorkNoProgress` 的
`work_start_not_delivered` 原因映射为它；文案来自 `fff48d872`（2026-10-06）。
开始说明门禁来自 `8ca2e17a8`（#218，2026-10-02）：只有关联原 Work、当前目标、
`work_report.kind=start` 的成功送达回执才放行业务工具；同一缺口纠正一次后仍未满足便暂停。

本次查询绑定内部 Work `1f294ea0-67b8-48e1-b38b-9c575069605d`、触发事件 `89777`、
原效果键与原 trace operation，使用 SQLite 只读连接、query_only、精确 ID 和 LIMIT。
未输出聊天正文、平台账号、凭据或实际平台消息号。

| 时间（台北） | 原事实 | 后果 |
| --- | --- | --- |
| 19:58:58 | start 调用引用内部事件 89777，返回 `reply_event_unavailable`，ok=false、mutation_committed=false、pending=false、uncertain=false | 没有送达开始消息。原工具结果已完成记录，效果行 accepted 不能被误读为发送成功 |
| 19:59:29 | 模型去掉引用并再次请求 start | `validate_work_report` 只看“已有 start 记录”，返回 `work_start_delivery_unconfirmed`，executed=false；未进入发送 |
| 19:59:55–58 | 一次 kind=reply 的发送成功，新增内部事件 89799/89800 | 是插话答复，代码不把它当 start 送达证明 |
| 20:00:14 | terminal_exec 再遭开始门禁拒绝 | 第一次纠正标记已记录，按 WorkNoProgress 收束 |
| 20:02:36–37 | 续接仍试 start，仍被拒；Work 现为 suspended，reason=WorkNoProgress | 开始状态缺口保留，生成固定暂停说明 |

首个失败的直接代码条件：`social/service.py:648–659` 要求
`event.platform_message_id.isdigit()`。事件 89777 存在、kind=message、group 且属于原
Conversation，但平台消息号是**负整数**；该检查必然不通过。读取 89791/89799/89800
也证实线上实际 ID 同时存在负整数与正整数。本次没有用平台号查业务身份。

补充源码复核：`adapters/onebot/sender.py:118–121` 的普通出站引用也有 `.isdigit()`
限制；后续若修负整数引用，须一起核这条路径，不能只改 Social 预检。此次首个调用
已在 Social 预检返回，未进入这条下游路径。

第二处独立问题：`work_control.py:334–339` 对任何 previous start，未成功就统一拒绝。
它不区分此次已经确认没有提交副作用的参数/引用失败与真正 unknown/pending。
`tests/unit/test_work_communication.py` 的 `test_failed_start_cannot_be_retried_or_cleared_by_quiet`
还把这种行为作为固定断言，所以不是偶发模型话术。

我的判断：当前门禁超出了“开始前向用户说明”的交流需求，要求特殊报告标签，且把
可纠正的已知失败锁成持久阻塞。固定暂停文案只是该机制的外显。修复至少应分别处理
引用的整数格式和真实发送结果分类；不能取消 unknown 发送的原效果围栏、强行重置
Work/预算或盲重发，也不应把本次调查混入 Genie 功能删除。后续设计应先明确是否
保留这种强制 start 登记门禁。

源码定位（本次提交中的相对路径）：

- [固定提示](../../src/qq_ai_bot/runtime/activation_outcome.py#L92)
- [开始记录拒绝](../../src/qq_ai_bot/runtime/work_control.py#L334)
- [业务执行门禁及一次纠正](../../src/qq_ai_bot/services/work_reporting.py#L101)
- [引用消息校验](../../src/qq_ai_bot/social/service.py#L641)

## dot 后续工作的输入

用户要求 dot 制定本次工作开始门禁问题的修复方案及后续重构计划，关注旧代码堆积和
过多防御性编程。本记录只冻结复现、源码和实际回执，未实施该问题修复或重构。

修复方案应区分平台引用格式校验、真实效果结果分类和工作交流策略。重构调查应按现行架构合同
回读源码和真实调用链，不能把历史任务书或模型叙述当作现行合同。
实际内部身份、当前授权、未知副作用回执和累计预算属于已有架构不变量，具体约束见
[共同开发合同](../architecture/development-contract.md)；重构范围由后续方案确定。

Genie 删除已在独立分支 `codex/remove-genie-speech-output` 实施，保留 ASR/历史音频。
迁移 0097 的生产旧执行核验、专属事实及 WAV 一致冷备、实际 operator/plugin 清退、
部署与服务器文件删除仍未执行，见 [语音输出任务书](../architecture/genie-speech-output-removal-taskbook-2026-10-07.md)。
本次源码的真实提交身份以该分支 Git 记录为准。
