# 个人客户端 API

个人入口使用原 Bot HTTP 服务的 `/client/v1`。operator 与个人设备使用不同凭据，
设备凭据解析当前 Person，但保留原设备与 App 入口，不具有管理或设备执行器权限。公开服务沿部署提供 HTTPS。

## 配对与账号绑定

第一台设备可由 operator 执行 `qq-ai-bot-cli admin person create`，再执行
`qq-ai-bot-cli admin client pair --person <id>` 取得绑定码；已有 QQ 或 Telegram 用户也可在自己的私聊
发送 `/ai link`。码有效期十分钟，仅存 SHA-256，一次兑换。过期码沿原维护循环清理。

| 请求 | 授权与结果 |
| --- | --- |
| `POST /pair {code, device_label}` | 只接受码，不接受 Authorization；返回独立 `device_id` 与 token |
| `GET /me` | 设备 Bearer；当前 Person、凭据来源、实际 App 入口、原请求 namespace、绑定与设备列表 |
| `POST /link-codes {request_id?}` | 设备 Bearer；为本人签发新码，明文仅首次返回 |
| `DELETE /devices/{id}` | 设备 Bearer；撤销本人的指定设备，其他设备继续可用 |
| `POST /messages {request_id, text, attachments:[media_id,...]}` | App 普通消息；返回内部 event_id、conversation_id 与 replay |
| `GET /requests/{request_id}` | 原消息／命令查询，先核本人归属，不重新派发 |

每个原 App 入口由服务器生成账号、Binding 和独立 Conversation；多设备进入同一入口时共用会话。
人物归并保留各原 App Binding、Conversation 和设备所属，不自动建新 App 或切换默认会话。
一个当前 Person 可以拥有多个原 App 入口；各入口的上下文、generation 和 session 继续独立。
默认入口仍是设备配对时的原 App；新设备使用所兑换绑定码的原 App 来源，缺少入口时才由配对创建。

所有设备鉴权请求可带 `X-Yuki-Entry: <内部 App Binding ID>`，显式选择当前本人拥有的入口；
该选择同时用于发送、Work 输入、上传、原请求查询及快照，不按今日默认入口猜测旧请求。
`/me.person_id` 是当前最终 Person，`source_person_id` 是原设备所属，`request_person_id` 是所选
App 的原 Person namespace，`binding_id` 和 `conversation_id` 是实际入口。绑定列表的
`source_person_id` 保该 Binding 原所属。主展示名来自独立 Person 名称，账号昵称仍属于各账号。
来源 Person 显式禁用或设备撤销不会因归并到启用的 Person 而复活。
仅停用 App Binding 不撤销设备／人物身份：资料、原回执和内部 Work 管理继续可用；
新的普通 App 消息、Work inputs 仍由所选实际入站检查停用状态，新配对也须有可用入口。
原 input 回执可重放；新补充可显式选择本人另一个合法 App 入口，接续原 Work，保持原来源与预算。
设备 token 只存哈希；码、token 和密钥不写聊天账本、Main、Memory 或命令回执。
配对响应丢失时原码已使用，重新签发码；未领取的设备可从设备列表撤销。
未知设备与他人设备均返回 `not_found`；失效凭据统一返回 `unauthenticated`。

新平台账号可经其认证网关私聊发送 `/ai link <code>`，在普通入账和自动建 Person 前兑换。
持有证明核实际连接、Presence 与平台；App 聊天正文仍不执行平台控制命令，配对使用个人 API。
这条控制命令不进入聊天、Main 或 Memory。已有同一 Person 绑定时消耗码并返回已绑定；
已有账号属于另一 Person 时，真实持有证明确认两者为同一人，归并到绑定码所指的当前 Person。
原账号、App、历史事件、执行和设备来源不改写；当前人物资料和权限按归并集合读取。
这与 operator 明确转移账号给另一人的操作不同；转移与 Person 遗忘命令见[管理 API](admin-api.md)。

## 历史与隐私

迁移 0121 保留旧绑定行和原事实，新列 `history_starts_at` 对迁移前行为空。
此后每条新绑定的起点都等于自身创建时间。转移时旧行标记 `transferred`，不改 Person；
旧行的 `updated_at` 是区间终点，新行的起点是转移时刻。原主动路由若指向旧行则暂停。
账号查询排除 transferred，停用绑定仍阻止自动创建新身份。

平台私聊历史只返回严格晚于起点所在秒的记录；该秒及更早、无法确定时间的记录不返回，计入
`withheld_unattributed`。新的平台历史读取按实际账号区间授权；已发生的发送回执仍沿原内部来源、
调用参数与派发事实读取，不以今日 Binding 或 Presence 可用性重新裁定原效果。
迁移前不设界的绑定保持原读取行为。前任被遗忘后，现任起点仍在，无须保存前任或墓碑。

遗忘按当前 Person 的归并集合删除作者事件、全部所属私聊、Work、Memory、设备、码和个人命令回执；
历史原所属用于定位删除资料，不在读取时重写。
`/ai forgetme` 只负责从当前认证账号解析一次 Person；operator 可按 Person ID 删除没有
可用账号的原身份。归属有界的账号不参与跨时期账号字符串清理；号码在正文、转写和
segments 中的脱敏只覆盖其拥有区间。账本的服务器创建时间为 `observed_at`，平台提供的
`occurred_at` 不用于判断归属。消息正文在写事务前准备，短事务复核来源元数据并应用。
Memory 重建按来源事件处理提案，保留有界账号的运行选择集，不按运行创建时间推断归属。

这些命令复用原审计与 `control_command_receipts`。SQLite 确认未提交的 busy 返回
`unavailable`；提交确认丢失返回 `unknown`，保留原请求号查询，不创建替代效果。
迁移 0121 保账号区间，0129 增加人物归并与设备请求原 scope；原 NULL scope 回执保留，
按原设备所属证明兼容读取，不重算指纹。迁移按现行升级链前进，不将本地验证写作生产上线。

普通消息号由服务器生成：`<app 绑定外部 ID>:<request_id>`。每个原 App 来源有独立请求号空间，
同一入口的多设备共享 UUID，不同原入口即使归并为当前同一人也不相互覆盖。
命令回执保存原 `request_person_id`；同一原入口内消息与命令仍共享 UUID，不按操作另开空间。
重放先核当前本人访问权与原事件作者／App 会话，再核规范化正文和附件，
已有请求不构造新入站、不通知轮次。并发首次提交在原入账处遇到 receipt_conflict 时，
回到同一授权与内容核对；同号不同操作或正文返回 request_conflict。正文是个人聊天输入，
不在个人 API 中执行 QQ `/ai` 管理命令。附件使用本人已登记的 media_id，经原媒体链准备。

App 经过原 Processor、Main、Social 与 Memory；配置、读取与执行来源使用 canonical Person
及真实入口平台。设备不会因同 Person 的 QQ 管理账号而取得管理权限。HTTP 只等待原入账，
Main 的生命周期由服务器持有；客户端关闭连接不会终止 Main。App 本地发送的成功回执与
出站事件同事务提交，外部引用为空，内部消息号沿 `social-operation:<id>`。
QQ 已派发消息也按派发时的绑定记录事实，发送途中转移不会将出站事件改记到新 Person。


## 原 Work 的个人访问与补充

`GET /home` 列出本人发起的私聊／群 Work，`GET /works/{id}` 返回来源入口、结果投递
位置、状态、revision、目标摘要、等待类型、该 Work 已确认发送的问题／结果、子 Work 数
与原预算。群聊其他消息、工具参数、checkpoint 和 journal 不进入个人投影。

`POST /works/{id}/inputs {request_id,text}` 接受与原发起者当前为同一 Person 的设备。App 的 `work_input`
事件、原 mailbox 的 ready 正文与 `client.work_input` 原回执在同一短事务写入。
运行中只入队；等待中先取得原 scope 租约，再将同一个 Work 置为 queued。租约繁忙时
返回 unavailable 且不写入。原会话、generation、session、合同与预算继续保留；
输入不通知 App Main。终态返回 precondition_failed 与原状态。

请求 UUID 与普通消息共享所选原 App 空间；改操作、目标 Work 或正文返回 request_conflict。
另一台本人设备重放返回原 event_id／input_id，并复核事件、mailbox 与原请求的对应关系。
WorkSourceGuard 只接纳本 Work mailbox 证明的跨入口事件；仍检查原来源、隐私版本、
原租约与追加事件摘要。App 主会话重建不重建 QQ Work，输入被改写或遗忘则来源失效。

`POST /works/{id}/cancel {request_id,expected_revision}` 沿原 manage_work 执行与保存回执。
App 补充不会自动发往群里；群 Work 后续仍可能按任务内容向原群投递结果，投影明确显示
结果位置。快照、SSE、文件与参考 SDK 的合同如下。

## 快照、事件与提示

`GET /home` 在同一读快照返回 main、其他入口 entries、本人 Work 与 snapshot_at。
`GET /conversations/{id}/events?after=&before=&limit=` 以内部事件 ID 分页，最多 50 条，
只读当前本人归并集合的原私聊账本；不同原入口各自保留，群记录与无关人物的会话均为 not_found。公开事件只含正文、方向、
作者类别、入口与附件引用，不返回运行提示、路径或网关凭据。

`GET /activity?conversation_id=` 读取实际进入的本进程执行 span 与诊断类别，
只报告 executing 或 insufficient，不以历史开始记录推断仍在执行；不返回提示与工具参数。
`GET /stream` 是 SSE：登记订阅后先发 sync（各入口游标与 Work revision），随后发送
conversation、work、activity 变化提示。事实仍须经对应 GET 读取。Last-Event-ID 不作为
持久事实游标；每次重连都重新 sync。15 秒静默时发送 keepalive 注释。

原数据库 session 的 after_commit 只唤醒进程内订阅，不执行 I/O；回滚无提示。
提示队列溢出会结束该订阅，包括被丢弃的是最后一条提示的情况。新连接重新登记与核对，
客户端在重连／回前台时用持久事件 ID 和 Work revision 补读。撤销／遗忘使后续读取与流失效。

## 上传与原文件下载

`POST /uploads?request_id=&sha256=&size=&filename=&kind=` 使用原始文件字节作为请求体，
kind 为 file、image、video 或 record。服务器流式写设备／请求暂存文件并计算摘要与长度，
核验后原子发布到媒体内容寻址路径，最后在短事务登记 `client.upload` 原命令回执。
回执同时保存媒体引用的归属与元数据，不新增上传去重表或虚构聊天事件。
成功返回 media_id 与原摘要、长度、类型和期限，之后由 `POST /messages` 的 attachments 引用它，
才实际发送消息。media UUID 和上传指纹以原 App namespace 与原 UUID 生成，人物归并不重算。
同 UUID、原参数的上传 POST 与请求 GET 都读取原回执；临时文件到期或实际 cleanup 后仍返回原结果，
不重读请求体、不重新登记文件。过期资源用于一条新的附件消息时仍返回 not_found。

上传与原聊天媒体共用单文件／总容量限制和 24 小时期限；维护清理会在删除前核实际回执引用。
图片、视频、文档和语音经服务器授权的临时本地来源进入原 Native／文档／ASR 链，
账本仅保存 media_id 和元数据。媒体字节、路径及 Base64 不进入公开事件。

`GET /artifacts/{id}?version=<sha256>` 只读取本人可见出站或本人 Work 结果引用的那个
不可变 artifact 和内容版本。App 本地 Social 投递和 QQ artifact 投递都将真实摘要写入
原出站事件；不因工作文件后来变更而改读新版本。无引用、他人引用、版本不符或来源删除
均为 not_found。文件读取在数据库写事务外，读取前后复核个人授权。

## Python 参考客户端与 CLI

主包提供 client_sdk.PersonalClient，所有操作经 HTTP；不直接访问服务器数据库、模型或工具。
yuki client（同一入口也保留 qq-ai-bot-cli client）支持 pair、me、home、send、input、cancel、upload、artifact、request、
retry、pending、watch、link-code、revoke。URL 取 YUKI_CLIENT_URL，设备凭据取
YUKI_CLIENT_TOKEN；配对码取 YUKI_CLIENT_CODE，pair 的首次响应返回设备 token。
凭据不放命令参数或 outbox。--state 选择本地 outbox 文件，文件权限为 0600。
`--entry-binding <内部 App Binding ID>` 或 `YUKI_CLIENT_ENTRY_BINDING` 显式选择原入口；
Python 构造器对应 `entry_binding_id=`。每个入口使用自己的 outbox 文件。

```sh
qq-ai-bot-cli client --state ./phone.sqlite send '继续这项任务' --draft
qq-ai-bot-cli client --state ./phone.sqlite retry <原请求UUID>
qq-ai-bot-cli client --state ./phone.sqlite request <原请求UUID>
qq-ai-bot-cli client --state ./phone.sqlite input <WorkID> '补充要求'
qq-ai-bot-cli client --state ./phone.sqlite watch
qq-ai-bot-cli client --entry-binding <AppBindingID> --state ./other-entry.sqlite home
```

首次提交前持久保存 UUID、目标、规范化正文及附件引用；同号修改正文／操作会拒绝。
outbox 绑定服务器和原 App 来源 Person，并保存实际 Binding／Conversation；不绑定当前 root。
人物归并后当前本人其他设备也可恢复该原入口队列，原 UUID、正文、目标、指纹及 media UUID 不变。
冷启动旧 outbox 依据旧 owner 和服务器返回的原 App 绑定补齐入口；同进程旧 `_me` 也刷新真实入口。
不能证明原入口或凭据属于无关人物／服务器时保留原草稿，不派发或迁移队列。
显式切换入口时使用对应入口的独立 outbox，已有文件不会改绑到另一原 scope。
四态为 draft（本地草稿）、submitted（已提交待确认）、accepted（服务器已接纳）、
result（已有可核结果）。普通消息的 result 要有原 Social 成功回执对应的结果事件；
Work 是否完成仍读取自己的 state。传输异常保留 submitted，先按原 UUID 查询及核对
操作、目标、payload_hash，再决定同号重试；已接纳的操作不会产生第二次派发资格。

GET /requests 在授权后附 payload_hash：SHA-256(canonical JSON [operation, target,
normalized payload])，JSON 按键排序、默认 ASCII 转义、无多余空白。普通消息 operation
为 message、target 为原 App 会话、payload 为 text 与 attachments；上传 target 为原请求来源 Person，
其余命令 target 沿原回执。当前 Person 只负责授权，不参与重算已有请求。
结果事件 ID 只取原 Social 来源和本人可读出站，不从平台消息号重建关联。

watch 每次连接先消费 sync，再用公开 GET／事件 keyset 重建本人全部入口与 Work 快照；
提示不作为事实。结束、超时或 unavailable 会重连，即使丢的是最终提示也会重新核对。
历史删除或变化会重读投影；身份／权限错误停止并交给调用者处理。上传保留文件摘要，
重传前检查本地原件，下载核对收到字节的原版本摘要。

本地验收使用假模型、假 QQ 与临时服务；生产迁移、真实 QQ／模型和部署另行执行。
个人 UI、设备执行器与真机阶段按原任务书的后续边界保留。ACP 方向 A 的接入见下节；方向 B 仍由 #456 跟踪。

App 主会话直接回复时，沿原 mailbox 匹配本会话、本人最近的 waiting_user Work；
其他入口的等待不会因此被匹配，跨入口接续仍使用明确的 Work inputs 接口。

## ACP 方向 A

`yuki acp`（同入口 `qq-ai-bot-cli acp`）是本地 stdio Agent，使用官方 Python SDK
`agent-client-protocol==0.12.1`、稳定 wire `protocolVersion=1`。它持现有个人设备凭据连接服务器，
不在客户端进程加载 Bot 数据库、模型或服务器工具。凭据沿 `YUKI_CLIENT_TOKEN`，地址沿
`YUKI_CLIENT_URL` 或 `--url`；原请求 Outbox 放在 `--state-dir` 指定的私有目录。

```sh
yuki acp --url https://yuki.example --state-dir ~/.local/state/yuki/acp
```

| API | 真实语义 |
| --- | --- |
| `POST /acp/sessions {request_id}` | 现有设备凭据与原 App 请求空间创建独立 ACP Conversation，返回 session_id、conversation_id、binding_id、generation；同原请求号只读取原创建回执 |
| `GET /acp/sessions/{id}` | 核当前本人、实际 ACP Binding 和该 Conversation，不接受本人 QQ/App 会话冒充 ACP session |
| `X-Yuki-Conversation: <session_id>` | 让已有消息、请求、上传、事件与 SSE 使用指定本人 ACP 会话；与 X-Yuki-Entry 同时提供时须对应同一实际 Binding |
| `POST /acp/sessions/{id}/requests/{request_id}/cancel` | 收拢该原 prompt 的现场执行及精确关联 Work，返回原请求投影和实际取消结果，不取消本人其他会话或所有 Work |

这里的路径均位于 `/client/v1`。`sessionId` 就是服务器的内部 Conversation UUID；每个原 Person
有自己的稳定 ACP Binding，每次 new 只建立一个新 Conversation。私聊唯一性沿 Binding、Presence
和已有 `thread_id`，各 session 的 generation、历史、Work、检查点与模型协议独立。人物归并保留
原入口，设备撤销使后续认证和读取失效，不把设备 token 当作 Person 或全部 Work 的生命周期。

ACP 的命令请求在原回执表使用 `request_conversation_id` 保存 Conversation＋原 UUID 空间；
原 App/旧设备回执保留 NULL scope、原 `request_person_id`、指纹和真实 device actor。
ACP 消息号、Work input source key、上传 UUID 与查询同时携带该真实 scope，不能只改某一层。
Python 参考客户端可用 `entry_conversation_id=` 选择 ACP 会话，每个会话使用独立原 Outbox 文件。

GET 原消息请求仍返回入账与已确认的 Social 结果，另有 `turn.state`（running、ended、unknown）
与真实 `turn.outcome`；精确关联的原 Work 另以 `work_id/state/revision/reason/active` 返回。Main 结束、
Work 完成和消息已发出是不同事实。`work.active` 只反映原 Work 尚未退出的现场 activation，终态也要等该原执行收拢、读取已准入的尾部发送回执后再结束 ACP prompt。没有现场任务且缺持久结束事实时保持 unknown；入账 ACK、
模型 final、无 activity 或静默时间都不当作完成，不补跑原模型。普通轮结束投影复用原命令回执，
不建第二套任务、请求日志或完成计数器。

适配器 live 只发送本 session 中已确认 Social 出站，原模型 final、思考、提示、工具参数不作为
用户消息。`session/load` 分页回放完整当前可见账本中的用户与确认出站，完成回放后才返回；
它不执行 Main、不重启 Work、不推进 generation。`session/prompt` 等原 turn，必要时继续读原 Work；
等待用户、暂停、失败与 unknown 如实处理。cancel 等后台实际收拢并读完已有确认输出后，
原 prompt 才返回 cancelled；关闭 stdio 或 HTTP 连接不自动发取消命令。

首次提交前适配器持久保存原 Yuki UUID；HTTP 确认丢失先查同号，重传仍用原正文和原 UUID。
标准 ACP prompt 没有跨重启业务请求号，JSON-RPC id 只关联一次连接；相同文字的新 RPC 可能是
新意图，不按内容去重。可选 `_meta["yuki.requestId"]` 传回原 UUID，标准客户端不强制使用该扩展。

能力按实际实现声明：本轮支持 initialize、new、load、prompt、cancel，不声明 resume/close；
cwd 是客户端目录资料，不改服务器工作区，也不授予客户端 terminal/设备执行能力。
文本、嵌入文本与 ResourceLink 保原输入含义；图片、音频与嵌入二进制显式不支持，客户端文本文件读取只使用已协商的只读回调。
已确认的文件出站在 live/load 中保留原 artifact ID 与版本，ResourceLink 指向个人 API 实际下载地址，仍需原设备凭据；链接本身不携 token。旧媒体只有 media ID、没有独立下载 API 时保留原可见引用并说明不能下载，不伪造 URL。
Yuki 已退役 MCP，本轮只接受空 `mcpServers`，非空显式返回不支持。当前规范要求 stdio MCP，
因此这是已知互操作限制，不宣称覆盖所有 ACP MUST；不为对接恢复动态工具或新的审批框架。

实施范围与两轮审查直接记录在[原控制面任务书 §10](Yuki-控制面重构与多客户端接入任务书-2026-10-14.md#10-455-两轮审查与实施)。
