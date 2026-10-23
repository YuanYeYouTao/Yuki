# 本机 Agent 监督与断流恢复：实现及验证记录

本次以合并后的 #500（`8aef3f0b`）为基线，删除任务 reporting 字段，修复本机 Agent 断流后仍显示 running、监督一轮就结束以及 detach 后继续上传的问题。Bot 和独立 Connector 0.3.0 使用协议 3，需要一起升级。

## 实现

- Work 不再保存或向模型展示 quiet/interactive 汇报模式。旧字段在正常 checkpoint 写入时清理，不扫描历史；原已付请求和工具回执不改写。聊天 engage=quiet 和 SELF 安静工作的要求保留。
- 本机 journal 持有最新状态，事件和响应共用持久序号，服务器按序接纳同一份投影。断流记录 unknown，原接纳、执行 ID 和终态保留；未知结果不重发。
- Codex 连接正常关闭、异常关闭和 proxy 退出统一处理，按原 owner 重连，再按原 ID 查证和恢复观察。旧连接的问题不能拿来批准新连接中重复的数字 ID。
- 新 session watch 可以跨轮持续监督；显式 execution watch 只跟指定轮。迁移 0150 保留旧 watch 的单轮范围。detach 收拢原恢复查询和上传映射，不结束用户的原生会话。
- 设备离线和原生实例离线分别显示，并向原 Work 送入状态事实。原 owned_run 等待继续接收进度，不增加汇报定时器或另一套任务系统。
- 删除完整 diff 进度上传，保留文件路径、命令结果、计划、问题和工具里程碑。事件流水发送，提交 ACK 删除本机原件，不再每发一条就等待一个 RTT。

## 验证及任务书覆盖

以下都是具体测试范围；模型是否主动汇报与平台消息是否送达分别核对。

| 任务书 | 证据入口 | 覆盖 |
|---|---|---|
| T01–T06 | `test_driver_supervision.py`、`test_receipt_projection.py`、`test_external_agent_supervision.py` | 正常/异常断流、排队、原执行恢复、终态不回退、缺失执行 unknown |
| T07–T08 | `test_driver_supervision.py` | loaded 和 turns 双分页，原执行/原 client ID 查证，idle 不扫描全历史 |
| T09–T10 | `test_journal_connector.py`、`test_receipt_projection.py` | WS 重连、未 ACK 重传、未知接纳不重派 |
| T11–T14 | driver、receipt、server supervision 测试及 `test_migration_0150.py` | 跨轮、单轮、旧 watch、detach 与恢复交错 |
| T15–T16 | `test_connector_startup_owner.py`、`test_receipt_projection.py` | 实例独立启动、重复取消收尾、发送者失败结束连接 |
| T17–T20 | `test_driver_supervision.py`、server supervision 测试 | 旧问题连接身份、乱序/迟到回执、撤销后无新效果、Claude/ACP EOF |
| T21–T22 | `test_retired_work_reporting.py`、Work settlement/effect/recovery 测试 | 删除旧模式、保原 paid 输入/回执、正常 checkpoint 清理；真实 Main 回放另记 |
| T23–T26 | server supervision、final audit、原 bridge 与 Work recovery 测试 | 等待登记前/中/后不丢唤醒，状态进入 Main take_inputs，原投递和旧 paid 调用不重放 |
| T27 | driver supervision 测试 | 稳定工具/子 Agent/文件里程碑，删除全量 diff |
| T28–T29 | `evidence/native-agent-supervision-20261023/transport-results.json` | 相同积压下 0/1/100/10000 条与 ACK 延迟测量，方法限制见下 |
| T30 | 独立 wheel 安装、CLI status、真实 Codex bridge | 不安装 Bot 的干净环境安装与版本核对；桌面、服务和上线另记 |
| T31–T32 | server supervision、receipt、driver supervision 测试 | 设备断线视图、旧连接退出、未知接纳原命令恢复 |

本地已完成：Connector 68 项测试通过；服务端定向 86 项通过；reporting 相关 163 个不同用例通过。34 个本地 Monty 用例因没有构建原生 worker 跳过，由 Native CodeMode CI 验证。项目 659 个源码文件按 Linux 目标 mypy 通过，Connector 12 个源码文件 mypy 通过。最终提交的 CI 结果另核，不以本地计数代替。

真实 Codex 验证使用当前用户的 `codex-cli 0.164.0`，在独立临时目录和新原生线程运行两条精确 PowerShell 命令。经过实际 TLS WebSocket、Connector、SQLite、原生审批和工具执行，文件写入/读回、原执行终态及原 Work continuation 均通过。只清退测试自己的线程，没有操作用户已有线程或发送真实 QQ 消息。缓存字段未提供，记录为 unknown。最终原始证据在同目录的 live JSON 中保存。

## 三轮复审

1. 逐条核原身份、状态和恢复链：补齐原 command 顶层身份；修 early progress 先于 admission 时状态被 unknown 覆盖；给响应也分配持久序号；修旧事件已提交但 ACK 丢失的升级回放。
2. 核生命周期和并发：detach 的目标回执推进序号，取消并等待原 recovery；已完成执行 detach 后保终态且停止上传；命令响应保存失败关闭连接。session watch 的 respond 取原问题的真实轮次和 connection ID。
3. 核查询快照、当前事实与使用方：过时的非终态恢复快照不覆盖实时进度；原执行的已确认终态仍可入账；恢复旧 turn 不广播给同会话其他观察。事件对 Main/工具暴露内部 execution ID，原生错误没有 code 仍可读取。随后交叉检查还修复了初始恢复时 socket 已关闭导致实例 owner 被停止、原生离线时 detach 被健康条件挡住的问题；detach 只要求真实设备授权和可达的设备连接，不要求原生 Agent 健康。

## 性能观察

基线是 #500 合并提交。脚本使用真实 SQLite FULL journal，相同预写积压和进程内 ACK 延迟对端；它测传输排空、ACK p50/p95、CPU、Python 分配峰值和本机 durable append 耗时，不测真实生产聊天延迟或服务器 writer 持有时间。

一次测量中，10000 条零延迟积压排空从 4.45 秒到 4.05 秒。100 条、模拟 ACK 延迟 50ms 的场景从 6.21 秒到 0.44 秒，说明去掉逐条 RTT 门槛有效。后一场景的并发传输分配峰值增加（约 30KB 到 413KB）；没有物化整个 journal 积压。真实 WebSocket 由写入背压控制内存。

新投影需额外保存当前状态。本机 10000 条 append 的中位数约 0.30ms 到 0.36ms；个别场景存在明显磁盘/调度波动，不能据单轮结果推导生产响应快慢。这里不声称整个 Bot 已提速，也不把这一测量当缓存命中证据。

## 真实旧安装与桌面兼容

生产 journal 的只读备份在私有副本上实际升级：8 个原操作、原响应和 namespace 保留，6 个已确认终态不复活，旧 watch 固定为 execution，原 observing=0 的 unknown prompt 纳入按原 ID 恢复。整个副本核验没有创建 driver 或调用模型，摘要见 `real-journal-upgrade.json`。

桌面 `Institute-of-Agent-Life/Yuki-Desktop@72c6cbf` 的真实 AgentConnector 适配器已对接 0.3.0 CLI 的隔离 status JSON，未配对与已配对状态均兼容。UI 显示用户服务状态，未声称原生 Agent 可用；目前也没有原生健康展示。这次无需桌面代码 PR，此探针不冒充完整 Electron 实机验收。

## 合并与上线

#500 已合并，本次实现已提 [#502](https://github.com/YuanYeYouTao/Yuki/pull/502)。最终 CI、合并 SHA、Bot 镜像、本机服务重启与健康结果在完成后追加。升级保留设备凭据和原 journal，服务器只替换 Bot，SnowLuma、持久环境及代理保持原状。健康后按现有维护约定清理旧部署临时文件和无引用镜像。
