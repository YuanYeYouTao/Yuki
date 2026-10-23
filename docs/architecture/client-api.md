# 个人客户端 API

个人入口使用原 Bot HTTP 服务的 `/client/v1`。operator 与个人设备使用不同凭据，
聊天设备凭据解析当前 Person，但保留原设备与 App 入口，不具有管理或电脑控制权限。公开服务沿部署提供 HTTPS。

## 本机 Agent 设备

凭据资格由共同 identity 服务处理，App／ACP 再附加聊天入口。`client_devices` 与一次性码使用明确的 `purpose=chat|connector`；迁移 0149 将旧行全部保为 chat，不自动扩大授权。`POST /link-codes` 可显式提交 `purpose="connector"`，operator 对应 `yuki admin client pair --person <id> --purpose connector`。现有设备列表展示 purpose，原撤销入口同时管理本人这两类设备。

电脑使用独立 `/agents/v1/pair` 兑换 connector 码，再用该 token 建立 `/agents/v1/connect` WSS；不能使用聊天 token。电脑归原 Person，不依赖 App Binding、Conversation 或其 generation；同人合并保原 device namespace，账号转移不转电脑。电脑连接只传输原生控制和重要事件，不自行发聊天消息。

`GET /agents/v1/devices`、`/agents/v1/instances/{instance_id}/sessions?cursor=...`、`/agents/v1/operations?work_id=...&cursor=...&limit=...` 和 `/agents/v1/operations/{id}?cursor=...` 使用现有 chat token 的共同人物资格，不为了查询另建或启用一个聊天会话。目录只返回本人设备，原操作查询保内部资源 ID、原回执及分页事件。设备目录包含尚无实例的本人 connector 设备，online 仅来自当前服务端连接；实例 `local_id` 仅用于本地配置匹配，控制仍使用服务端内部 `id`。会话目录 cursor 沿该实例原 native 分页，查询不启动任务。操作列表按原 created_at/id 分页，cursor 为上一页原 operation ID，默认每页 50 条，next_cursor 为 null 表示当前已读完；不另建持久游标。操作列表复用原持久回执和离线 unknown 投影，不把 accepted 当作已完成。新控制沿 Main 固定 `external_agent` 执行处核本人设备与当前真实来源，详见[连接器操作说明](../operations/local-agent-connector.zh-CN.md)。

## 角色资料与单 SOUL

`GET /meta` 公开当前角色名称、API revision `6` 和实际能力声明，不推断未实现的客户端功能。
设备读取 `GET /character`；获准 owner 使用 `PATCH /character`，按原 UUID 和 profile revision 修改名称等资料。
owner 资格归 Person，账号转交不转交角色编辑权；operator 通过独立管理入口授权。

`GET /character/soul`、`PUT /character/soul` 和 `POST /character/soul/reload` 只处理同一份 `SOUL_FILE`。
保存使用原 UUID、文件 hash CAS 和原子替换；写入与当前生效 hash 分别报告。
空正文合法，`{{name}}` 只在装配时精确展开。改名或改人格影响后续合法新激活，原已付输入和历史保持原件。
SDK 的 SOUL 受控写在确认丢失后先查原 UUID，恢复仍用原 payload；不把一般未知外部效果重新派发。
旧双文档路径与 key 不保留。安装、旧草稿整理与权限见[升级说明](../operations/soul-deployment-20261022.md)。

## 配对与账号绑定

第一台设备可由 operator 执行 `yuki admin person create`，再执行
`yuki admin client pair --person <id>` 取得绑定码；已有 QQ 或 Telegram 用户也可在自己的私聊
发送 `/ai link`。码有效期十分钟，仅存 SHA-256，一次兑换。过期码沿原维护循环清理。

| 请求 | 授权与结果 |
| --- | --- |
| `POST /pair {code, device_label}` | 只接受码，不接受 Authorization；返回独立 `device_id` 与 token |
| `GET /me` | 设备 Bearer；当前 Person、凭据来源、实际 App 入口、原请求 namespace、绑定与设备列表 |
| `POST /link-codes {request_id?}` | 设备 Bearer；为本人签发新码，明文仅首次返回 |
| `DELETE /devices/{id}` | 设备 Bearer；撤销本人的指定设备，其他设备继续可用 |
| `POST /messages {request_id, text, attachments:[media_id,...], agent_target?}` | App 普通消息；返回内部 event_id、conversation_id 与 replay |
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
App／ACP 新入站携带原认证 Binding 的 Person 与实际 Conversation，在普通接纳处核对原阶段、账号和入口；不从今日账号重新创建或选择人物。
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

Account 保稳定原生账号，Assignment 保每次实际归属阶段。转移、真解绑和人↔Yuki 结束旧阶段，
创建新阶段；停用保归属，恢复不创建阶段。当前查询只排除 ended，不把 disabled 当作解绑。
起止时间只保存有证据的事实；旧阶段结束但准确时刻未知时保持空值，不用升级时刻或 updated_at 填造。
原主动路由若指向结束阶段则暂停，原 Person、设备、Conversation 与回执不改归新持有人。

新的平台历史读取按原阶段的真实区间授权，不用今日账号反推过去的人物。首次迟到消息按可证明的
原生时间与阶段入账；无法证明原人物时保 Account 来源，不造人物或临时私聊 Conversation。
没有原生时间时保真实接收时间，不声称已辨清历史归属。重复接入先读原回执，已发生的发送
继续沿原内部来源、调用参数与派发事实读取，不以今日 Binding 或 Presence 可用性重裁原效果。

遗忘按当前 Person 的归并集合删除作者事件、全部所属私聊、Work、Memory、设备、码和个人命令回执；
历史原所属用于定位删除资料，不在读取时重写。
`/ai forgetme` 只负责从当前认证账号解析一次 Person；operator 可按 Person ID 删除没有
可用账号的原身份。归属有界的账号不参与跨时期账号字符串清理；号码在正文、转写和
segments 中的脱敏只覆盖其拥有区间。账本的服务器创建时间为 `observed_at`，平台提供的
`occurred_at` 保存原生发生时间，有证据时用于查原归属阶段；不把接收时刻假装原生时间。消息正文在写事务前准备，短事务复核来源元数据并应用。
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
App 消息与 `POST /works/{id}/inputs` 可附显式 `agent_target`：本人设备、实例、会话、执行、原 operation、项目 cwd，以及可选 intent、permission_id、option_id、response JSON 对象。intent 不默认为 prompt；没有显式意图时以用户正文为准。目标在首次接纳前沿原 Person／资源／当前 connector 授权查证，随原事件与 Work 输入进入 Main，不由个人 HTTP 另建执行驱动。目标、意图和原问题答案都参与同 UUID 一致性；未提供 target 的旧输入保持原正文和指纹。原请求查证先返回原回执，不依赖该电脑当前在线或目录就绪。此选择不构成整个 Work 后续工具操作的固定锁；实际控制继续核原资源、原问题和真实执行回执。

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
Work 的来源、预算、子任务数和原投递结果按这批 Work 一起读取；同步中的入口活动也批量投影，不逐项水合辅助资料。
`GET /conversations/{id}/events?after=&before=&limit=` 以内部事件 ID 分页，最多 50 条，
只读当前本人归并集合的原私聊账本；不同原入口各自保留，群记录与无关人物的会话均为 not_found。公开事件只含正文、方向、
作者类别、入口与附件引用，不返回运行提示、路径或网关凭据。

`GET /activity?conversation_id=` 读取实际进入的本进程执行 span 与诊断类别，
只报告 executing 或 insufficient，不以历史开始记录推断仍在执行；不返回提示与工具参数。
`GET /stream` 是 SSE：登记订阅后先发 sync（各入口游标与 Work revision），随后发送
conversation、work、activity 变化提示。事实仍须经对应 GET 读取。Last-Event-ID 不作为
持久事实游标；每次重连都重新 sync。同一轮已排队的唤醒合成一次最新快照，不重复扫描中间状态。15 秒静默时发送 keepalive 注释。

主库 owner 在根事务确认提交并释放 writer 后同步唤醒进程内订阅，不执行 I/O；
savepoint 释放和回滚不发提示。取消若在物理提交后到达，先发布这次已确认的提示，再继续传播取消。
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

上传与原聊天媒体共用 24 小时原件缓存，不设单文件、会话或全局经验容量配额；真实磁盘与平台传输失败照实返回。维护清理会在删除前核实际回执引用。
`GET /conversations/{conversation_id}/events/{event_id}/attachments/{attachment_index}` 在原个人事件可见范围内下载原件，返回强 ETag；读取后重新核当前本人／入口／原事件授权及字节摘要。缺失或已经清理的过期原件返回 not_found，不通过重新下载来重置期限。事件 attachment_index 是媒体序号，segment_index 是原消息段序号；ASR 的 segment_index 只与后者关联。普通事件投影不为可用性提示扫描或 hash 原文件；客户端实际下载以该 GET 回执为准。

已接纳消息的原件通过只读目录映射在共享沙箱 `/inputs/media-cache`，各会话的 Agent 均可按需读取或复制收藏。Main 首次输入仅有附件索引和资料，图片、视频、贴纸及普通文件均不自动展开；语音沿原 ASR 配置独立处理。
账本仅保存 media_id 和元数据，沙箱路径仅在当前运行资料提供；媒体字节及 Base64 不进入公开事件。设备上传、绑定与下载 API 的身份仍沿原合同，不因共享 Agent 环境改为匿名上传或公开下载。

`GET /artifacts/{id}?version=<sha256>` 只读取本人可见出站或本人 Work 结果引用的那个
不可变 artifact 和内容版本。App 本地 Social 投递和 QQ artifact 投递都将真实摘要写入
原出站事件；不因工作文件后来变更而改读新版本。无引用、他人引用、版本不符或来源删除
均为 not_found。账本触发器从原 segments 提取 event／artifact／version 引用；插入、编辑和删除同步维护这份索引，不复制人物或授权。下载只按原 artifact 与版本定点检查原出站及 Work 回执，不扫描无关历史或构建整个 Work 页面。文件读取在数据库写事务外，读取前后复核个人授权。

## Python 参考客户端与 CLI

主包提供 client_sdk.PersonalClient，所有操作经 HTTP；不直接访问服务器数据库、模型或工具。
yuki client 支持 pair、me、home、send、input、cancel、upload、artifact、request、
retry、pending、watch、link-code、revoke。URL 取 YUKI_CLIENT_URL，设备凭据取
YUKI_CLIENT_TOKEN；配对码取 YUKI_CLIENT_CODE，pair 的首次响应返回设备 token。
凭据不放命令参数或 outbox。--state 选择本地 outbox 文件，文件权限为 0600。
`--entry-binding <内部 App Binding ID>` 或 `YUKI_CLIENT_ENTRY_BINDING` 显式选择原入口；
Python 构造器对应 `entry_binding_id=`。每个入口使用自己的 outbox 文件。

```sh
yuki client --state ./phone.sqlite send '继续这项任务' --draft
yuki client --state ./phone.sqlite retry <原请求UUID>
yuki client --state ./phone.sqlite request <原请求UUID>
yuki client --state ./phone.sqlite input <WorkID> '补充要求'
yuki client --state ./phone.sqlite watch
yuki client --entry-binding <AppBindingID> --state ./other-entry.sqlite home
```

首次提交前持久保存 UUID、目标、规范化正文及附件引用；同号修改正文／操作会拒绝。
outbox 绑定服务器和原 App 来源 Person，并保存实际 Binding／Conversation；不绑定当前 root。
人物归并后当前本人其他设备也可恢复该原入口队列，原 UUID、正文、目标、指纹及 media UUID 不变。
冷启动旧 outbox 依据旧 owner 和服务器返回的原 App 绑定补齐入口；同进程旧 `_me` 也刷新真实入口。
不能证明原入口或凭据属于无关人物／服务器时保留原草稿，不派发或迁移队列。
显式切换入口时使用对应入口的独立 outbox，已有文件不会改绑到另一原 scope。
四态为 draft（本地草稿）、submitted（已提交待确认）、accepted（服务器已接纳）、
result（请求已有真实结束或失败事实）。普通消息按原 turn.ended 收敛；静默结束不要求额外发送，Social 事件独立表示已交付，Work 是否完成仍读取自己的 state。running/unknown 保持 accepted 并保留已确认输出，不因静默或断线推断成功。传输异常保留 submitted，先按原 UUID 查询及核对
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
个人 UI、设备执行器与真机阶段按原任务书的后续边界保留。ACP 方向 A 的接入见下节；
方向 B 已有通过 `external_agent` 访问服务器持久环境内 Agent 的实现，见[主 Agent](main-agent-runtime.md#预算等待与异常)。
服务器访问用户本机原生 Agent 的设备通道、授权与生命周期解耦尚未实施，见[本机 Agent 任务书](Yuki-本机Agent接入与ACP统一重构任务书-2026-10-23.md)。

App/ACP 主会话直接回复时，沿原 mailbox 匹配本会话、本人最近的 waiting_user Work；
其他入口的等待不会因此被匹配，跨入口接续仍使用明确的 Work inputs 接口。

## ACP 方向 A

`yuki acp`是本地 stdio Agent，使用官方 Python SDK
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
Work 完成和消息已发出是不同事实。`work.active` 只反映原 Work 及其后代尚未退出的现场 activation，终态也要等该原执行收拢、读取已准入的尾部发送回执后再结束 ACP prompt。没有现场任务且缺持久结束事实时保持 unknown；入账 ACK、
模型 final、无 activity 或静默时间都不当作完成，不补跑原模型。普通轮结束投影复用原命令回执，
不建第二套任务、请求日志或完成计数器。

适配器 live 只发送本 session 中已确认 Social 出站，原模型 final、思考、提示、工具参数不作为
用户消息。`session/load` 分页回放完整当前可见账本中的用户与确认出站，完成回放后才返回；
它不执行 Main、不重启 Work、不推进 generation。`session/prompt` 等原 turn，必要时继续读原 Work；
等待用户、暂停、失败与 unknown 如实处理。cancel 等后台实际收拢并读完已有确认输出后，
原 prompt 才返回 cancelled；关闭 stdio 或 HTTP 连接不自动发取消命令。

取消按原事件关联的 Work 子树撤权；父 Work 已完成或失败时保留其终态，只取消仍可运行的后代。
现场模型请求按原 activation owner 捕获，不能按当前同名 scope 取消后来接管的新 Work。
已 claim 的 Social 发送继续沿原回执结算；外部精确取消保留 CancelledError，不冒充新消息替换。

首次提交前适配器持久保存原 Yuki UUID；HTTP 确认丢失先查同号，重传仍用原正文和原 UUID。
标准 ACP prompt 没有跨重启业务请求号，JSON-RPC id 只关联一次连接；相同文字的新 RPC 可能是
新意图，不按内容去重。可选 `_meta["yuki.requestId"]` 传回原 UUID，标准客户端不强制使用该扩展。

能力按实际实现声明：本轮支持 initialize、new、load、prompt、cancel，不声明 resume/close；
cwd 是客户端目录资料，不改服务器工作区，也不授予客户端 terminal/设备执行能力。
文本、嵌入文本与 ResourceLink 保原输入含义；图片、音频与嵌入二进制显式不支持，客户端文本文件读取只使用已协商的只读回调。
已确认的文件出站在 live/load 中保留原 artifact ID 与版本，ResourceLink 指向个人 API 实际下载地址，仍需原设备凭据；链接本身不携 token。普通原件使用原 conversation/event/attachment_index 下载接口；无法定位原事件或原件已清理时保留原可见引用并说明不可用，不伪造 URL 或重新下载续期。
Yuki 已退役 MCP，本轮只接受空 `mcpServers`，非空显式返回不支持。当前规范要求 stdio MCP，
因此这是已知互操作限制，不宣称覆盖所有 ACP MUST；不为对接恢复动态工具或新的审批框架。

实施范围与两轮审查直接记录在[原控制面任务书 §10](Yuki-控制面重构与多客户端接入任务书-2026-10-14.md#10-455-两轮审查与实施)。


## 控制面重构后的个人入口

App/ACP 与其他 Main 入口共用当前 Main 固定工具合同，执行处核本人来源；保留原 Work 普通回答匹配与显式 Binding 路由，每个 ACP Conversation 独立。按 Binding 读取个人入口遇到多个 Conversation 时返回真实候选，调用者使用已有 conversation_id 明确选择，不任意读取首条。

Person 的管理员资格直接保存于人物，但个人设备正文仍不解析 `/ai admin` 或插件直达命令。真正管理走既有 operator/平台命令入口。鉴权不再写设备 last_used_at；GET/事件读取与初始 SSE sync 的调用链为只读，仍即时核当前撤销、人物启停与入口归属。

ACP 用户回答等待问题时，经原 ordinary admission 进入同一 Work；第二请求投影、live 输出与取消均关联该原 Work 及其子树。原请求结束与 Work 是否完成独立。已知失败重放仍能展示先前已确认输出，沿原 UUID 查证，不重新购买模型请求。


### 可选配对链接与真实参考服务

显式配置 `CLIENT_PUBLIC_URL` 后，私聊 `/ai link` 在原一次性 chat 码旁附 `yuki://pair?server=...&code=...`；未配置时保留原裸码输出，不猜服务器地址。链接只帮助填写已有信息，purpose 与 Person／owner 权限不变。公开地址可包含反向代理前缀；生产 HTTPS/WSS 沿同一入口，不因 Desktop 新开电脑入站端口。

个人 API 的 me、home、events、activity、Work、request、upload、pair 和 link-code 使用真实投影的响应模型；保留现有公开扩展字段，不因类型收敛静默删字段。新增可选输入与只读路由保持 revision 6 兼容。

`python -m tests.support.desktop_reference_server --root DIR --state-file FILE` 仅为临时隔离测试：装配原 ApplicationContainer、SQLite、App/Work/Social、个人与 connector HTTP 路由，模型出口采用脚本化 provider，不访问付费模型或生产。旧 soul_reference_server 保持原入口。测试 state 中 artifact_id/artifact_version 来自真不可变快照与已成功 Social 发送，授权只属于 fixture 原 Person。`POST /__test/work` 在该隔离 Person 原事件下沿 WorkRepository 接纳、Social 发送问题并转 waiting_user，供真实输入、取消与跨设备回执联调；这些测试控制不进入正式应用。
