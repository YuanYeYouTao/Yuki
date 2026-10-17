# Yuki canonical runtime

本文描述永久主体与 canonical 运行时合同；运行时只支持 canonical schema，
数据库版本以随包 Alembic head 为准，启动由 `persistence/schema_guard.py` 读取；
当前应用与数据库基线统一见 [README](../../README.md)。
跨模块开发遵循 [共同架构约束](development-contract.md)。

## 永久主体与身份

一个数据库对应一个永久 Yuki。Yuki 的人格、SELF、记忆、设置、插件状态和自动化不属于
某个 QQ 号、网关实现或某条 WebSocket 连接。

核心身份对象如下：

- `Person`：一个永久的人类主体。人物记忆和长期偏好按 Person 保存。
- `IdentityBinding`：Person 在某个平台上的外部账号。一个 Person 可以有多个 Binding。
- `Space`：永久共享空间；QQ 群只是它的一种外部表现。
- `SpaceBinding`：Space 与外部群号的绑定。
- `Presence`：Yuki 自己的平台账号。Presence 不是 Person。
- `CanonicalConversation`：私聊按 IdentityBinding＋Presence 区分，群聊按 SpaceBinding＋topic 区分。
  同一 Person 的多个入口分别保有历史、generation、检查点与模型 session；Memory 仍归 Person。
  `/ai new` 只推进命令所在会话的 generation。

第三方机器人使用 `external_bot` 作者类型，不创建 Person 或人物记忆。事件作者只有
`person`、`yuki`、`external_bot`、`system` 四类；命令、插件和自动化属于 event origin，
不是作者类型。

## 人物归并、账号转移与名称

`persons.merged_into_person_id` 只表示经认证证明属于同一人的归并，直接指向最终人物；
目标没有指针。A→B→C 时原 A/B 都指向 C，重复归并只读返回。Person 可以没有账号。
原 Binding、事件、私聊 Conversation、原 Work/source、已付模型输入与效果回执继续保存原 Person ID；
当前目录、人物资料、获准记忆、管理权及配置查询才解析当前人物，不批改历史。
没有第二套人物映射、全库迁移开关、身份缓存或惰性资料搬迁。

已认证 `/ai link` 同人关联调用人物归并；operator `binding transfer` 明确把账号转给另一人，
结束旧 Binding 时段、创建新 Binding，原历史仍归原人。已停用人物、设备、grant 或执行的
撤权事实不会随归并复活；当前人物停用影响其全部入口，来源已停用也不误停目标正常入口。
QQ 管理员授权仍来自真实配置的有效 QQ 绑定；设备凭据不成为 operator 凭据。

人物名称是可空、可修改、允许重名的 `Person.display_name`，不参与身份或授权。
首次非空观察补齐空名，后续账号昵称变化只更新账号与别名；旧数据按最近全局 nickname 回填。
明确改名保旧名为别名；归并保目标主名，目标空时采用来源主名，来源主名入原人物别名。
目录与资料按当前人物读取主名，无账号人物也能读取；未命名显示占位不落库。
群名片、Space 范围别名与事件当时名称仍保原来源，同人目录命中及共同群按人物去重。

人物资料只观察当前真实事件携带的昵称、群名片和已知标记，在首次入账的同一事务更新
原人物、账号、别名与 Membership。缺失字段不擦除已有资料；明确的空群名片可以清空
当前名片。事件当时名称仍按原消息保存，不用库中的旧名字补写该事件，也不改过去的
公共历史。缺少名称不阻止接纳或回复，人物身份与权限继续使用内部 ID。

群名来自普通入站消息或真实平台资料通知，更新原平台的既有 SpaceBinding 与 Space。
普通消息随首次入账观察群名和 last-seen；独立资料通知遇到相同或空群名只读返回。
改名不修改启用设置、路由或聊天历史。独立资料通知不建立聊天事件，
不触发模型，也不要求先远端补齐资料。没有资料时保留已知值，不另设刷新缓存、TTL 或 worker。

当前 Runtime/Plugin USER 配置保目标有效键，来源独有有效键连原行 ID/版本迁入目标。
来源冲突行只保历史，不作 fallback；unset 和再次归并不会使它复活。原配置审计仍可查，
rollback 核对原行与修订，旧审计跨归并只沿实际配置移动审计确认，不凭相同值和版本冒领另一行。
Plugin-global KV 不改键值、版本或原所属。目标已有主动路由优先，未设置时只继承来源已有路由，
不为归并猜一个账号；当前路由 SQL 与派发复核认可同人来源 Binding，原物理发送证明保持原件。

归并在一致 WAL 读视图准备本人物关系、配置、别名和路由，再在同事务短写入。
原生 BUSY 只在纯数据库操作整体回滚后重备原 ID；不持 writer 做外部或全库历史扫描。
人物遗忘按完整归并族清退敏感资料，原无正文 Social 回执可保原来源 ID；
已确认发送可结算原成功且 `event_id=None`，不重建正文、人物、会话或重发。

迁移 `0129` 增加归并、人物名称及原 App 请求 scope，并解除 Social 原来源会话的生存依赖；
保原事件、请求 hash、UUID、预算和回执。App 各入口与稳定原请求 namespace 见[个人客户端合同](client-api.md)。

## Conversation 与事件账本

平台适配器先将原始消息投影为正文、附件、按位置保序的 mention 和 reply，再解析 Person、Space、
Presence 与 canonical Conversation。事件账本保留平台 message ID、sender/group 外部 ID、Provider
和 Gateway provenance，以便幂等、审计和故障定位；这些字段不再承担业务所有权。

入账后沿运行时快照和工作来源传递内部 `trigger_event_id` / `source_event_id`，按
`chat_events.id` 定位并核验会话。不得用平台 message ID 反查已有内部事件或猜测来源。
`canonical_event_id` 是另一种关联标识，不能与整数账本主键混用。

历史 alias 只用于稳定兼容键。多个 alias 可以指向一个 canonical Conversation，当前插件 SDK
读取固化的 primary alias。新入口使用 `conversation:<id>` 作为 primary alias；私聊解析统一经过
Presence 的平台和对方账号所对应的有效绑定，旧 transport alias 只作历史定位。
已有 turn 或读版本直接使用其 Conversation ID。连接重建、Provider 更换或路由接管
不推进 generation；自动化沿原创建会话执行，投递目标不改换创建者的来源。

迁移 0120 保留旧会话及全部事件、Work、回执与检查点 ID，把旧会话归到 primary alias
对应的绑定；不能唯一确定时中止。其他绑定下次入站建立新会话，旧 alias 不搬迁。
Person 隐私遗忘覆盖其所有入口会话。

公共历史由该 Conversation 的已选 compact 检查点和覆盖点后的原消息组成；原始
`chat_events` 始终是证据源。检查点是不可信输入，不进入 Memory，也不授予权限。
容量判断沿 Main 的真实完整请求，装配阶段不截短历史或等待独立后台摘要。

插件外部事件保持 `external_event` 独立账本类型，不伪装为 Person 发言。插件的主动触发只是一个
可靠 WakeupRequest：它加载同一 canonical Conversation 下普通聊天使用的完整稳定 snapshot、
compact 检查点、raw history、按需 Memory 工具、Prompt compiler、工具 schema 和模型 profile，事件摘要只作为本轮
最后一条临时 user input，既不落入聊天历史，也不形成插件专属短上下文。Yuki 成功发送的主动消息
仍是普通 outbound `message`，并通过 `caused_by_event_id` 永久指向来源 external event。模型历史与
公共消息渲染保留因果来源标签，平台正文不被改写。

## 平台入口与原生适配

私聊以内部 Binding＋Presence 确定入口，群以 SpaceBinding＋topic 确定入口。每个入口各有 Conversation；同一 Person/Space 的长期身份和已定义记忆语义共享，聊天上下文不串入口。旧记录、冻结 primary alias 和 generation 不重写。

GatewayProvider 提供连接事实、接入投影和实际原生社交操作。公共 Social 准备内部目标、核授权、claim 原操作并记录回执；各适配器编码文本、提及、引用、文件及协议实际限制。Provider 的纯发送计划不发送消息，每个实际平台步骤各有原回执。公共层不调用任意 OneBot action，也不按 Telegram/QQ 分叉一套领域服务。

Telegram token 配置后使用 long polling，原生 getMe 确认 Bot，实际 getChatMember 用于新群接纳与发送探针。消息引用在适配器中用 chat＋message 定位，进入业务后传内部事件 ID；topic、编辑和群迁移沿原绑定。媒体 file/token URL 留在 Telegram 适配器，公共媒体只接收有界字节。

polling 偏移确认表示原事件已入账或已明确不接纳，不表示 Main 已完成。普通无 Work 对话在进程中止后不自动重买模型或补发；已接纳的 Work、原工具和未知效果按各自持久执行/回执恢复。不能把 native ACK、事件入账、Work 接纳和平台送达混称完成。

公共会话历史和成员目录只返回已观察账本与内部人物 ID，明确标注 observed。QQ 远端/raw 转发展开不再作为默认模型工具；原确认事实与独立批准的 QQ SDK 传输扩展保留。跨平台同数字不会继承 QQ 忽略机器人、超管或隐私归属。

## 三类平台收发路由

平台收发使用三类持久路由；这不是根据问题内容选择模型或搜索服务的路由器：

1. Person 主动路由：决定主动私聊通过哪个 IdentityBinding 和 Presence 发送。
2. SpaceBinding ingest 路由：决定一个外部群的唯一入站 Presence，阻止多账号扇出重复处理。
3. Space 主动路由：决定向 canonical Space 主动发送时使用的 SpaceBinding 和 Presence。

路由变化只增加 RouteGeneration。路由暂停时失败关闭；群 ingest 不匹配的事件在策略、账本正文、
Agent 和 Memory 之前丢弃。事件触发的即时回复优先复用本次 ingress 连接，主动发送才读取持久路由。
已由接入层验证的群消息（包括 `/ai new`）在写入时复核持久 ingest 路由与 Presence；
处理期间原 WebSocket 断开不撤销已收到的消息，路由暂停或改绑仍拒绝写入。
真实已认证入站连接对应既存、未暂停的同 Presence ingest pin 时，在当前入站会话内只读核验
Binding、Presence、Registry 当前连接代次与能力，无需再次远端查询自身群成员资格。
此结果不授予写权限；账本首次写入仍执行原持久路由围栏。冷路由、其他 Presence 的路由恢复、
主动发送与显式恢复继续使用实时成员探针及原 CAS，不缓存成员资格或引入隐式接管。

冷 ingest 恢复只保留短生命周期计划：原 authenticated connection 快照、Presence revision 和
SpaceBinding owner/revision。外层数据库会话结束后才执行成员探针及恢复；准备/探测阶段总计
10 秒 deadline 不包围物理提交。路由写入的新事务核验这些版本及原 route CAS，完成后 fresh
admission 再确认原连接与 pin。探测或 CAS 前取消不安装路由；提交确认未知直接传播，不盲重试
或反向删除可能已提交的路由。数值日志分别报告初读、探测准备、恢复和 fresh recheck，
恢复包含 CAS，不能将其耗时称作纯数据库锁等待。

群内超管的精确 `/ai on` 使用独立的确定性控制入口，不是绕过 ingest 的聊天事件：QQ adapter
验证真实事件连接与管理员 Binding，恢复服务保留健康接入 pin，否则仅接受唯一通过实时成员
探针的候选。群启用、必要的两张群路由变更、审计和幂等回执在同一个 `BEGIN IMMEDIATE` 中提交；
重验身份、路由 revision 与连接快照，冲突整笔退出。命令及回执不进聊天账本、模型或Memory，不修改 ConversationGeneration。普通消息、插件和其他管理命令仍受原围栏限制。
尚无 SpaceBinding 的新群也只可由上述超管控制入口首次登记：按 Registry 中当前 QQ 连接的内部
Presence ID 有界读取并在写事务前完成实时成员核验，要求唯一候选就是事件所在账号。writer 重验
原连接、Presence revision 和外部绑定仍不存在，再将新内部 Space/Binding、两条路由、审计和回执
一起提交；竞态或多个候选不创建身份，也不采纳或替换期间新出现的 owner。已有绑定继续按原
内部 Space 恢复；首次登记不创建聊天事件或会话，后续普通消息才走正常入库与会话建立。
原 Context 的同一内部 request 重放先按 principal/request 索引核验原回执及其内部 Space owner、
载荷 hash，再返回已处理；请求 ID 被不同载荷复用则拒绝，不依外部群号重建已登记的 owner。

## Gateway Registry 与 Provider

GatewayConnection 只存在于进程内 Registry。Registry 保存 Provider、Presence、连接句柄、能力、
健康状态和 ConnectionGeneration；连接对象、token、Cookie 和 QQ 登录数据不进入数据库。

Yuki 不再支持 NapCat，当前内置 SnowLuma QQ/OneBot v11、原生 Telegram 与进程内 App Provider。既有 GatewayProvider、
GatewayConnectionProfile、Catalog、Registry 和 ProviderOneBotAdapter 继续承载可替换网关，
provider 标识仍是通用字符串，不把业务核心改成品牌专用逻辑：

- 不同 QQ 可以通过 SnowLuma 同时在线。
- 同一 QQ 只能有一条活动连接；重复连接在 Adapter 和 Registry 两层拒绝，新连接不会挤掉旧连接。
- 同一 Provider 的重连、迟到断连和重复 socket 仍沿原连接合同处理。
- 更换网关实现保留原 Presence 与业务 owner；连接变化不复制 Conversation、Memory 或历史。
- App 使用迁移 0121 的 Presence 与配对生成的绑定，登记本地连接；它不表示 QQ 已连接。
  连接必须显式选择 provider_id；目录只负责实际适配器，不按正文猜平台。恢复工作者启动前登记 App 连接。
  App 普通消息先提交 canonical 事件，只有首次创建再通知原轮次协调器和 Main；HTTP 返回或
  客户端断开不取消处理。输入文本走个人聊天，平台 `/ai` 控制命令仍在平台入口使用。
  App 出站由原 Social 事务写账本与回执，不调用 OneBot，不伪造外部消息 ID；确认丢失时查原回执。

历史账本的 `ingress_provider` 保留当时真实来源，旧 `napcat` 字段不迁移成 `snowluma`。
读取历史不会重新装配退役 Provider，也不能据此重发或重跑旧效果。

QQ Provider 只承诺 Yuki 使用的 OneBot 核心能力。Provider 私有 action 不进入跨 Provider 合同，
不支持时必须显式失败。

SnowLuma noVNC 与 WebUI 的宿主监听分别由 `SNOWLUMA_NOVNC_BIND_ADDRESS` 和
`SNOWLUMA_WEBUI_BIND_ADDRESS` 控制，默认均为 `127.0.0.1`。设为 `0.0.0.0` 属于显式扩大攻击面，
部署方必须同时提供强密码、防火墙和可信来源限制；OneBot HTTP/WS 与 VNC 原始端口不得公网暴露。

## Memory 与扩展

Memory 使用 canonical owner 分区：SELF 属于永久 Yuki，PERSON 属于 Person，GROUP 属于 Space，
PERSON_GROUP 表示某 Person 在某 Space 中的共同经历。证据保留真实事件来源，读取仍受作用域、
权限和内容能力约束。Presence 或 Provider 变化不会复制或迁移记忆。

偏好、自动化目标、插件状态、Emoji、Speech 和配置投影均使用 canonical owner。
定时 Main 保留真实创建主体、原触发场景和 generation；SELF 保自身主体和来源，
Person 按原永久身份与当前同人关系核权限，纯内部工作无需当前账号或发送路由。发送由共同 Social 按明确目标和当前路由处理，
触发群与 Presence 不构成额外的发送目标菜单。路由暂停只控制实际发送，不阻止内部 Main 工作。

Plugin API 当前为 `4.0`，Host 只加载 API 与已批准 manifest 精确匹配的插件。兼容键仍使用 primary
`conversation_key`，SDK 还可读取可选的 person、space、conversation 和 presence ID。
插件不能伪造管理员、绕过 Capability 或直接选择任意 GatewayConnection。

## 管理入口与客户端接入

当前管理入口沿同一个 Bot 的领域服务：

```text
QQ / Telegram / App → MessageProcessor → Main / Work / Social / Memory / Workspace
operator CLI → Admin API ────────────────┘
```

管理身份从服务器 operator 声明与 Bearer 凭据得到。`ApplicationContainer.admin_api`
复用运行中的领域服务、模型文件服务和原命令回执；QQ `/ai` 仍调用原领域入口。
请求不能提交主体或借管理权限以 Person 发言。正文权限、短事务、事务外效果和原操作查询
见[管理 API](admin-api.md)与[管理控制面合同](control-plane-foundation.md)。

旧浏览器入口、前端与旧控制面方法表已从源码删除。个人客户端接入同一事实源，
使用 Person 设备身份，提供消息、原 Work 接续、SSE 与文件访问；HTTP-only 参考客户端
复用这些接口。合同见[个人客户端 API](client-api.md)，本地验收与部署状态分别记录。

## 数据库与安全边界

人物遗忘保留一整个隐私事务，包含 Work、投影失效、trace 隐私代次、领域删除与脱敏。
Rebuild selection 在锁外一次准备全部 Binding 别名，写入前核原 Person、别名和完整
Rebuild 目录；变化则整个事务回滚后重备同一纯数据库计划，保原 ID，不设经验次数封口，
取消继续传播；提交或回滚确认未知不授予重放资格。不逐个别名在 writer 中解析历史。
插件队列使用 canonical Person 或同 plugin/Space 的原创建者 grant 集合删除。
JSON 候选检查解码后的键和值，脱敏先解析再序列化，数字账号与转义文本保持有效 JSON；
损坏的旧 JSON 导致整个遗忘回滚，不能报告部分遗忘成功。

- 新数据库从无父 revision 的 `0048` canonical baseline 创建表，再依次执行后续迁移至当前 head。
- 历史桥接只接受已经完成 canonical v2 的旧 `0048` 数据库；更早或过渡态数据库失败关闭。
- 运行时没有 v1、dual-write、backfill 或 cutover 分支。
- `0049` 不提供 downgrade；`0050` 追加事件因果引用，`0051` 仅追加 recall 评估观测列。
  不修改事实、证据、身份、正文或路由；历史 used=false 保持未知口径。
- 代码回退必须核验数据库兼容性，保留新写入的消息、文件、预算和回执。恢复旧数据库是
  独立的数据恢复操作，必须停止写入并评估恢复点之后的数据损失，不能作为默认回退步骤。
- `/healthz` 保持公开瘦载荷；管理健康和连接详情只能通过授权后的控制面查询。
- `0052` 增加无正文社交操作回执；发出后结果不确定时禁止自动重发，不提供丢弃回执的 downgrade。
- `0053` 增加沙箱任务记录，`0054` 增加提示投影，`0055` 增加语音转写，
  `0056` 增加持久工作运行时，`0057` 增加子 Agent。
- secret 永不回读；日志与错误不输出 token、Cookie、完整外部 ID、消息正文或本地敏感路径。

部署与数据升级分别见 [SnowLuma Provider 部署](../deployment/snowluma.md) 和
[3.9.0 升级指南](../upgrade-3.9.0.md)。

## 后台领取的事务边界

普通记忆批次先在只读会话中准备事件、身份投影与候选，再用短写事务按
任务 id/status/updated_at 条件领取。竞争失败的候选不进入执行；live Memory 在提交时
重新核验事件仍位于当前 generation 水位之后。历史扫描不发生在写事务内。
