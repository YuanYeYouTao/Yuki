# 个人客户端 API

个人入口使用原 Bot HTTP 服务的 `/client/v1`。operator 与个人设备使用不同凭据，
设备凭据只代表其 Person，不具有管理或设备执行器权限。公开服务沿部署提供 HTTPS。

## 配对与账号绑定

第一台设备可由 operator 执行 `qq-ai-bot-cli admin person create`，再执行
`qq-ai-bot-cli admin client pair --person <id>` 取得绑定码；已有 QQ 用户也可在自己的私聊
发送 `/ai link`。码有效期十分钟，仅存 SHA-256，一次兑换。过期码沿原维护循环清理。

| 请求 | 授权与结果 |
| --- | --- |
| `POST /pair {code, device_label}` | 只接受码，不接受 Authorization；返回独立 `device_id` 与 token |
| `GET /me` | 设备 Bearer；本人的 Person、App 会话、绑定与设备列表 |
| `POST /link-codes {request_id?}` | 设备 Bearer；为本人签发新码，明文仅首次返回 |
| `DELETE /devices/{id}` | 设备 Bearer；撤销本人的指定设备，其他设备继续可用 |
| `POST /messages {request_id, text, attachments:[]}` | App 普通消息；返回内部 event_id、conversation_id 与 replay |
| `GET /requests/{request_id}` | 原消息／命令查询，先核本人归属，不重新派发 |

每个 Person 有一个服务器生成账号的 `app` 绑定和一个主会话，多设备共用。
设备 token 只存哈希；码、token 和密钥不写聊天账本、Main、Memory 或命令回执。
配对响应丢失时原码已使用，重新签发码；未领取的设备可从设备列表撤销。
未知设备与他人设备均返回 `not_found`；失效凭据统一返回 `unauthenticated`。

新 QQ 账号可经认证网关私聊发送 `/ai link <code>`，在普通入账和自动建 Person 前兑换。
这条控制命令不进入聊天、Main 或 Memory。已有同一 Person 绑定时消耗码并返回已绑定；
属于其他 Person 时，只转移今后的账号控制，原历史、执行与 App 设备留在原 Person。
operator 的转移与 Person 遗忘命令见[管理 API](admin-api.md)。

## 历史与隐私

迁移 0121 保留旧绑定行和原事实，新列 `history_starts_at` 对迁移前行为空。
此后每条新绑定的起点都等于自身创建时间。转移时旧行标记 `transferred`，不改 Person；
旧行的 `updated_at` 是区间终点，新行的起点是转移时刻。原主动路由若指向旧行则暂停。
账号查询排除 transferred，停用绑定仍阻止自动创建新身份。

平台私聊历史只返回严格晚于起点所在秒的记录；该秒及更早、无法确定时间的记录不返回，计入
`withheld_unattributed`。旧回执锚点失去有效绑定后返回 `binding_unavailable`。
迁移前不设界的绑定保持原读取行为。前任被遗忘后，现任起点仍在，无须保存前任或墓碑。

遗忘按 Person ID 删除作者事件、全部所属私聊、Work、Memory、设备、码和原命令回执。
`/ai forgetme` 只负责从当前认证账号解析一次 Person；operator 可按 Person ID 删除没有
可用账号的原身份。归属有界的账号不参与跨时期账号字符串清理；号码在正文、转写和
segments 中的脱敏只覆盖其拥有区间。账本的服务器创建时间为 `observed_at`，平台提供的
`occurred_at` 不用于判断归属。消息正文在写事务前准备，短事务复核来源元数据并应用。
Memory 重建按来源事件处理提案，保留有界账号的运行选择集，不按运行创建时间推断归属。

这些命令复用原审计与 `control_command_receipts`。SQLite 确认未提交的 busy 返回
`unavailable`；提交确认丢失返回 `unknown`，保留原请求号查询，不创建替代效果。
迁移 0121 提供前向升级；已转移的库不能退回旧账号唯一约束。没有执行生产迁移或部署。

普通消息号由服务器生成：`<app 绑定外部 ID>:<request_id>`，多设备共享本人请求号空间，
不同 Person 互不冲突。重放先核原事件作者与 App 会话，再核规范化正文和附件，
已有请求不构造新入站、不通知轮次。并发首次提交在原入账处遇到 receipt_conflict 时，
回到同一授权与内容核对；同号不同操作或正文返回 request_conflict。正文是个人聊天输入，
不在个人 API 中执行 QQ `/ai` 管理命令。当前只接收文本，附件在 P9 接入。

App 经过原 Processor、Main、Social 与 Memory；配置、读取与执行来源使用 canonical Person
及真实入口平台。设备不会因同 Person 的 QQ 管理账号而取得管理权限。HTTP 只等待原入账，
Main 的生命周期由服务器持有；客户端关闭连接不会终止 Main。App 本地发送的成功回执与
出站事件同事务提交，外部引用为空，内部消息号沿 `social-operation:<id>`。
QQ 已派发消息也按派发时的绑定记录事实，发送途中转移不会将出站事件改记到新 Person。

Work、快照、SSE、附件与参考 SDK 随 P7–P11 补充；当前收发不代表这些链路已验收。

## 原 Work 的个人访问与补充

`GET /home` 列出本人发起的私聊／群 Work，`GET /works/{id}` 返回来源入口、结果投递
位置、状态、revision、目标摘要、等待类型、该 Work 已确认发送的问题／结果、子 Work 数
与原预算。群聊其他消息、工具参数、checkpoint 和 journal 不进入个人投影。

`POST /works/{id}/inputs {request_id,text}` 只接受原发起 Person。App 的 `work_input`
事件、原 mailbox 的 ready 正文与 `client.work_input` 原回执在同一短事务写入。
运行中只入队；等待中先取得原 scope 租约，再将同一个 Work 置为 queued。租约繁忙时
返回 unavailable 且不写入。原会话、generation、session、合同与预算继续保留；
输入不通知 App Main。终态返回 precondition_failed 与原状态。

请求 UUID 与普通消息共享本人空间；改操作、目标 Work 或正文返回 request_conflict。
另一台本人设备重放返回原 event_id／input_id，并复核事件、mailbox 与原请求的对应关系。
WorkSourceGuard 只接纳本 Work mailbox 证明的跨入口事件；仍检查原来源、隐私版本、
原租约与追加事件摘要。App 主会话重建不重建 QQ Work，输入被改写或遗忘则来源失效。

`POST /works/{id}/cancel {request_id,expected_revision}` 沿原 manage_work 执行与保存回执。
App 补充不会自动发往群里；群 Work 后续仍可能按任务内容向原群投递结果，投影明确显示
结果位置。尚未完成的快照、SSE、文件与参考 SDK 见后续阶段。

## 快照、事件与提示

`GET /home` 在同一读快照返回 main、其他入口 entries、本人 Work 与 snapshot_at。
`GET /conversations/{id}/events?after=&before=&limit=` 以内部事件 ID 分页，最多 50 条，
只读本人的私聊账本；群记录与他人的旧会话均为 not_found。公开事件只含正文、方向、
作者类别、入口与附件引用，不返回运行提示、路径或网关凭据。

`GET /activity?conversation_id=` 读取实际进入的本进程执行 span 与诊断类别，
只报告 executing 或 insufficient，不以历史开始记录推断仍在执行；不返回提示与工具参数。
`GET /stream` 是 SSE：登记订阅后先发 sync（各入口游标与 Work revision），随后发送
conversation、work、activity 变化提示。事实仍须经对应 GET 读取。Last-Event-ID 不作为
持久事实游标；每次重连都重新 sync。15 秒静默时发送 keepalive 注释。

原数据库 session 的 after_commit 只唤醒进程内订阅，不执行 I/O；回滚无提示。
提示队列溢出会结束该订阅，包括被丢弃的是最后一条提示的情况。新连接重新登记与核对，
客户端在重连／回前台时用持久事件 ID 和 Work revision 补读。撤销／遗忘使后续读取与流失效。
