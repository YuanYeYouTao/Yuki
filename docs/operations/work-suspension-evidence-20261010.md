# 旧 suspended Work 清理前取证

取证时间：2026-10-10 00:05:48 +08:00。时区统一为 Asia/Taipei（UTC+08:00）。通过 SQLite URI mode=ro，只读当前生产库的 13 条 suspended Work、关联 recovery 和 journal 的有限字段，以及小型 metadata 对象。没有 DML、重启或源码修改。

时间证据：created 是任务创建时间。表中“暂停记录更新时间”来自 recovery.updated，所有对应 exit_reason=paused；这是最后一次暂停状态落盘/观察的 B 级证据。schema 没有 suspended_at，recovery 行可被后续观察覆盖，故不能认定它是历史第一次暂停时刻。Work.updated 仅是最后修改时间；journal.updated 仅是私有执行检查点更新。13 条原始暂停时刻均未得到独立日志确认。

当前 Bot 容器创建于 2026-10-09 21:41:41 +08:00，启动于 2026-10-09 21:41:46 +08:00，晚于全部 13 条记录。限定 docker logs --timestamps --tail 20000 未发现这些内部 Work ID 的 work_activation_exit，旧容器日志不在此容器中，不能据此确认更早暂停细节。

| 内部 Work ID | 原任务摘要 | 创建时间 | 暂停记录更新时间（B） | 原因 |
| --- | --- | --- | --- | --- |
| 1c8c2296-5b02-42b9-9ccd-f6e164762954 | 检查沙箱 GitHub CLI 并提交 issue | 2026-09-26 04:16:09 | 2026-09-26 04:20:02 | work_turn_changed |
| e250731b-d55a-4bae-8d54-e7ab29a17f82 | 自主参与当前群聊 | 2026-09-28 05:38:29 | 2026-09-28 05:39:35 | paused |
| 4e745dc7-5087-4a1e-a8e3-e80d119d3293 | 自主参与当前群聊 | 2026-09-28 21:33:55 | 2026-09-28 21:35:00 | ValueError |
| 1daebf3c-8344-4d73-94b9-3582998e477c | 自主参与当前群聊 | 2026-09-29 08:27:46 | 2026-09-29 08:28:01 | LLMInvalidRequestError |
| c5ca7cd4-2bf2-4755-869c-b9190a836e9b | 自主参与当前群聊 | 2026-09-29 09:11:20 | 2026-09-29 09:11:47 | LLMInvalidRequestError |
| 8bbf3c6d-418b-4a89-b81c-07cefcdf491b | 自主参与当前群聊 | 2026-10-04 18:21:33 | 2026-10-04 18:24:12 | ValueError |
| 581e02a5-8f64-4f43-a5ca-da5c98bdf3cb | 自主参与当前群聊 | 2026-10-05 08:01:40 | 2026-10-05 08:13:40 | paused |
| 98030262-518f-40a0-9fac-cb3379671215 | 自主参与当前群聊 | 2026-10-05 16:22:51 | 2026-10-05 16:24:37 | ValueError |
| 089bc7ff-a41a-45f0-abfd-8e42f02c8236 | 自主参与当前群聊 | 2026-10-05 18:28:11 | 2026-10-05 18:28:55 | gateway_disconnected |
| 800fb4b7-67c0-437f-85b3-f0347f6cdcaa | 自主参与当前群聊 | 2026-10-05 18:52:42 | 2026-10-05 18:54:20 | JournalUnavailable |
| 7656c430-82b8-414f-88c0-3434f35a4cb9 | 对主会话关系和仓库事件作自然反应 | 2026-10-09 01:04:55 | 2026-10-09 01:05:09 | LLMEmptyResponseError |
| 70bd6a53-19fd-471d-979a-ce4966f53cfe | 分析 1–31 报数博弈必胜策略 | 2026-10-09 09:51:01 | 2026-10-09 12:35:40 | work_activation_interrupted |
| 2cbd0ed0-ef72-4f66-91d1-5f4364c0b955 | 对主会话关系和仓库事件作自然反应 | 2026-10-09 18:47:23 | 2026-10-09 18:47:56 | RequestCancelledError |

原因统计：3 条 ValueError，2 条 LLMInvalidRequestError（均 HTTP 400），2 条无异常的明确 suspended 结束决策；work_turn_changed、gateway_disconnected、JournalUnavailable、LLMEmptyResponseError、work_activation_interrupted、RequestCancelledError 各 1 条。

- `1c8c2296-5b02-42b9-9ccd-f6e164762954`：原会话回合边界发生变化，运行时检测到 work_conflict，停止继续执行以保留原执行事实。具体是哪次新输入改变边界未在此有限取证中查出。 journal.phase=paired，journal.ending=；model_requests=23，tool_calls=16，sent_messages=7。
- `e250731b-d55a-4bae-8d54-e7ab29a17f82`：没有 failure 异常；journal metadata.ending=suspended，表明运行结束决策明确要求暂停。暂停的自然语言理由未单独持久化，不能补写成故障。 journal.phase=paired，journal.ending=suspended；model_requests=2，tool_calls=1，sent_messages=2。
- `4e745dc7-5087-4a1e-a8e3-e80d119d3293`：activation 阶段发生不可自动重试的 ValueError。持久 failure 只保留异常类型，未保留异常消息，不能确定具体校验条件；部分 journal 有压缩进度，只是背景，不能据此认定压缩为根因。 journal.phase=dispatched，journal.ending=；model_requests=2，tool_calls=1，sent_messages=2。
- `1daebf3c-8344-4d73-94b9-3582998e477c`：provider 请求收到 HTTP 400，并分类为不可自动重试的模型请求错误。错误正文只存摘要哈希，具体参数不兼容原因在本次证据中不可恢复。 journal.phase=dispatched，journal.ending=；model_requests=2，tool_calls=1，sent_messages=0。
- `c5ca7cd4-2bf2-4755-869c-b9190a836e9b`：provider 请求收到 HTTP 400，并分类为不可自动重试的模型请求错误。错误正文只存摘要哈希，具体参数不兼容原因在本次证据中不可恢复。 journal.phase=dispatched，journal.ending=；model_requests=2，tool_calls=1，sent_messages=0。
- `8bbf3c6d-418b-4a89-b81c-07cefcdf491b`：activation 阶段发生不可自动重试的 ValueError。持久 failure 只保留异常类型，未保留异常消息，不能确定具体校验条件；部分 journal 有压缩进度，只是背景，不能据此认定压缩为根因。 journal.phase=paired，journal.ending=；model_requests=2，tool_calls=1，sent_messages=2。
- `581e02a5-8f64-4f43-a5ca-da5c98bdf3cb`：没有 failure 异常；journal metadata.ending=suspended，表明运行结束决策明确要求暂停。暂停的自然语言理由未单独持久化，不能补写成故障。 journal.phase=paired，journal.ending=suspended；model_requests=6，tool_calls=2，sent_messages=4。
- `98030262-518f-40a0-9fac-cb3379671215`：activation 阶段发生不可自动重试的 ValueError。持久 failure 只保留异常类型，未保留异常消息，不能确定具体校验条件；部分 journal 有压缩进度，只是背景，不能据此认定压缩为根因。 journal.phase=paired，journal.ending=；model_requests=2，tool_calls=1，sent_messages=2。
- `089bc7ff-a41a-45f0-abfd-8e42f02c8236`：gateway 连接断开；虽可重试，但同类失败 attempts=4，超过前三次自动重试额度，转为 suspended；模型/工具/发言计数均为 0。 journal.phase=，journal.ending=；model_requests=0，tool_calls=0，sent_messages=0。
- `800fb4b7-67c0-437f-85b3-f0347f6cdcaa`：恢复或读取 Work 私有协议 journal 时不可用，失败记录仅留 JournalUnavailable 类型；不能确定当时缺失、损坏或媒体缺失。当前 metadata 对象仍可读，不证明当时恢复成功。 journal.phase=response，journal.ending=；model_requests=4，tool_calls=1，sent_messages=3。
- `7656c430-82b8-414f-88c0-3434f35a4cb9`：provider 返回未能形成有效响应的空结果，分类为不可自动重试。journal 曾标记 completed，但 caller_completion_pending_result 仍在进度键内且未发送消息，持久 Work 仍 suspended，不能把意图当任务成功。 journal.phase=dispatched，journal.ending=completed；model_requests=2，tool_calls=0，sent_messages=0。
- `70bd6a53-19fd-471d-979a-ce4966f53cfe`：调度恢复检测到原 running Work 的 owner 已缺失或租约过期，记录中断后暂停，防止无原异常事实地重跑。journal 最后更新在 09:52:56，12:35:40 是发现/记录中断时刻，实际进程停止时刻未知。 journal.phase=paired，journal.ending=；model_requests=9，tool_calls=6，sent_messages=0。
- `2cbd0ed0-ef72-4f66-91d1-5f4364c0b955`：本次 activation 的请求被取消后分类为不可自动重试并暂停。failure 未保存具体取消发起者或触发事件，不能确定是用户取消、新输入抢占还是其他取消源。 journal.phase=paired，journal.ending=；model_requests=3，tool_calls=1，sent_messages=0。

长期留存原因：suspended 是保存执行事实、协议检查点和回执的恢复边界；正常调度主要选择 queued/running，suspended 只会为待送暂停通知等有限恢复工作被选择，因此不会因为后续时间流逝自动清除。该解释来自当前 work_scheduler.py、work_supervisor.py、work_resume.py 的合同，具体历史版本的异常语义仍以生产持久记录为准。

已知缺口：历史第一暂停时刻；3 条 ValueError 的异常消息；HTTP 400 的详细服务端原因；JournalUnavailable 的具体失效条件；取消的发起者；owner 中断的实际发生时刻。无需读取全聊天正文或将旧 Work 盲目重跑来补这些缺口。
