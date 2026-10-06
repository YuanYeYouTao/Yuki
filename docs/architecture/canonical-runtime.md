# Yuki 3.8 canonical runtime

本文描述 Yuki 3.8 的现行架构合同，不是迁移任务书。3.8 运行时只支持 canonical schema，
数据库版本以随包 Alembic head 为准，启动由 `persistence/schema_guard.py` 读取；
当前应用与数据库基线统一见 [README](../../README.md)。
跨模块开发遵循 [共同架构约束](development-contract.md)。

## 永久主体与身份

一个数据库对应一个永久 Yuki。Yuki 的人格、SELF、记忆、关系、设置、插件状态和自动化不属于
某个 QQ 号，也不属于 NapCat、SnowLuma 或某条 WebSocket 连接。

核心身份对象如下：

- `Person`：一个永久的人类主体。关系、人物记忆和长期偏好按 Person 保存。
- `IdentityBinding`：Person 在某个平台上的外部账号。一个 Person 可以有多个 Binding。
- `Space`：永久共享空间；QQ 群只是它的一种外部表现。
- `SpaceBinding`：Space 与外部群号的绑定。
- `Presence`：Yuki 自己的平台账号。Presence 不是 Person。
- `CanonicalConversation`：私聊按 Person 唯一，群聊按 Space 唯一。显式 `/ai new` 或隐私遗忘
  等上下文边界操作会推进 Conversation generation；更换账号本身不推进它。

第三方机器人使用 `external_bot` 作者类型，不创建 Person、人物关系或人物记忆。事件作者只有
`person`、`yuki`、`external_bot`、`system` 四类；命令、插件和自动化属于 event origin，
不是作者类型。

## Conversation 与事件账本

OneBot 原始消息先投影正文、附件、按位置保序的 mention 和 reply，再解析 Person、Space、
Presence 与 canonical Conversation。事件账本保留平台 message ID、sender/group 外部 ID、Provider
和 Gateway provenance，以便幂等、审计和故障定位；这些字段不再承担业务所有权。

入账后沿运行时快照和工作来源传递内部 `trigger_event_id` / `source_event_id`，按
`chat_events.id` 定位并核验会话。不得用平台 message ID 反查已有内部事件或猜测来源。
`canonical_event_id` 是另一种关联标识，不能与整数账本主键混用。

历史 alias 只用于稳定兼容键。多个 alias 可以指向一个 canonical Conversation，当前插件 SDK
读取固化的 primary alias。换 QQ、换 Provider、连接重建或路由接管都不得更改 Conversation
generation。

Rollup 是 Conversation 的可重建提示投影：原始 `chat_events` 始终是证据源，摘要不进入
Memory，也不被当作可信指令。热尾同时受事件数和实际 Prompt 字符预算约束；后台模型不可用时
可以使用 emergency overlay，但不会覆盖语义检查点。

插件外部事件保持 `external_event` 独立账本类型，不伪装为 Person 发言。插件的主动触发只是一个
可靠 WakeupRequest：它加载同一 canonical Conversation 下普通聊天使用的完整稳定 snapshot、
Rollup、raw history、Memory、Prompt compiler、工具 schema 和模型 profile，事件摘要只作为本轮
最后一条临时 user input，既不落入聊天历史，也不形成插件专属短上下文。Yuki 成功发送的主动消息
仍是普通 outbound `message`，并通过 `caused_by_event_id` 永久指向来源 external event。模型历史与
Rollup source projection 会显示有界因果标签，平台正文不被改写。

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
重验身份、路由 revision 与连接快照，冲突整笔退出。命令及回执不进聊天账本、模型、Memory 或
Relationship，不修改 ConversationGeneration。普通消息、插件和其他管理命令仍受原围栏限制。
尚无 SpaceBinding 的新群也只可由上述超管控制入口首次登记：按 Registry 中当前 QQ 连接的内部
Presence ID 有界读取并在写事务前完成实时成员核验，要求唯一候选就是事件所在账号。writer 重验
原连接、Presence revision 和外部绑定仍不存在，再将新内部 Space/Binding、两条路由、审计和回执
一起提交；竞态或多个候选不创建身份，也不采纳或替换期间新出现的 owner。已有绑定继续按原
内部 Space 恢复；首次登记不创建聊天事件或会话，后续普通消息才走正常入库与会话建立。
原 Context 的同一内部 request 重放先按 principal/request 索引核验原回执及其内部 Space owner、
载荷 hash，再返回已处理；请求 ID 被不同载荷复用则拒绝，不依外部群号重建已登记的 owner。

## Gateway Registry 与正式 Provider

GatewayConnection 只存在于进程内 Registry。Registry 保存 Provider、Presence、连接句柄、能力、
健康状态和 ConnectionGeneration；连接对象、token、Cookie 和 QQ 登录数据不进入数据库。

NapCat 与 SnowLuma 是同层正式 QQ/OneBot v11 Provider：

- 不同 QQ 可以分别通过两个 Provider 同时在线。
- 同一 QQ 只能有一条活动连接；重复连接在 Adapter 和 Registry 两层拒绝，新连接不会挤掉旧连接。
- 切换同一 QQ 必须先停止旧 Provider、确认连接注销，再启动新 Provider。
- 切换只改变 GatewayConnection 和 ConnectionGeneration，不创建 Presence，不改变 Conversation、
  Memory 或 RouteGeneration。

Provider 只承诺 Yuki 使用的 OneBot 核心能力。Provider 私有 action 不进入跨 Provider 合同，
不支持时必须显式失败。

SnowLuma noVNC 与 WebUI 的宿主监听分别由 `SNOWLUMA_NOVNC_BIND_ADDRESS` 和
`SNOWLUMA_WEBUI_BIND_ADDRESS` 控制，默认均为 `127.0.0.1`。设为 `0.0.0.0` 属于显式扩大攻击面，
部署方必须同时提供强密码、防火墙和可信来源限制；OneBot HTTP/WS 与 VNC 原始端口不得公网暴露。

## Memory、关系与扩展

Memory 使用 canonical owner 分区：SELF 属于永久 Yuki，PERSON 属于 Person，GROUP 属于 Space，
PERSON_GROUP 表示某 Person 在某 Space 中的共同经历。证据保留真实事件来源，读取仍受作用域、
权限和内容能力约束。Presence 或 Provider 变化不会复制或迁移记忆。

关系、偏好、自动化目标、插件状态、Emoji、Speech 和配置投影均使用 canonical owner。
Person 自动化在实际发送时解析当前路由。SELF 自动化固定创建时的群和 Presence；
该场景或代际变化后阻止执行，不借新的 Yuki QQ 账号或真人身份投递。

Plugin API 当前为 `3.0`，Host 只加载精确匹配的插件。兼容键仍使用 primary
`conversation_key`，SDK 还可读取可选的 person、space、conversation 和 presence ID。
插件不能伪造管理员、绕过 Capability 或直接选择任意 GatewayConnection。

## Control Plane 与未来 WebUI

Control Plane 是未来 WebUI 的唯一后端业务边界：

```text
future HTTP/WebUI | current CLI | current QQ commands
                         |
                         v
               transport adapters
                         |
                         v
        ControlPlaneBundle / application services
                         |
                         v
          repositories + runtime registries
```

未来 HTTP 层只能把已认证身份转换为 `ControlPrincipal`、`DecisionContext` 和 canonical target，
然后调用同一 Query/Command 服务。它不得直接查询 ORM、调用 OneBot 私有 API、读取 secret 或把
QQ 消息证明伪造成 Web 请求。分页使用 opaque cursor；mutation 使用 request ID、expected revision、
同步审计和幂等回执。

3.8 不提供管理 HTTP API、登录或前端，也不开放新管理端口。新增 WebUI 时应实现 transport、
认证、CSRF 和内容脱敏，而不是复制业务服务。

`ApplicationContainer.control_plane` 已装配共享 Query/Command 服务及运行中的配置、连接、
自动化、插件和 Memory 依赖；access 从服务器配置认证 CLI/Web operator，
公开 wire 合同仅转换已核验的 DTO，不直接开放 HTTP。现有 QQ/CLI 仍有直接调用共享领域服务的入口，图中边界是
统一接入方向，不表示所有入口已经迁入 ControlPlaneBundle。
配置、revision、分页、事务外执行、原操作查询和管理 HTTP 后续边界见
[Control Plane 地基](control-plane-foundation.md)。

## 数据库与安全边界

人物资料刷新复用入站已取得的 runtime snapshot。已确认的空 nickname/card 仅以
bot、账号和群为键缓存 known 标记，最多 256 项、60 秒，命中不续期；该缓存不保存
名字或身份权威。明确入站资料优先，网络异常不产生成功空值。异步资料返回后的写入
核对原 Person，遗忘并重建同一 QQ Binding 后不能写入迟到的旧资料。

普通人物观察在原事务维护群 Binding 的 last-seen。额外群名刷新仅在取得非空群名时
写入，不因刷新冷却、空返回或没有解析器再占 writer；真正刷新在短 writer 中读取当前
Space/Binding，不改变启用开关。可选展示资料的数据库写入失败记录类别后继续处理，
后续可信身份、ingest 路由和账本写入的拒绝仍按原合同执行。

人物遗忘保留一整个隐私事务，包含 Work、投影失效、trace 隐私代次、领域删除与脱敏。
Rebuild selection 在锁外一次准备全部 Binding 别名，写入前核原 Person、别名和完整
Rebuild 目录；变化则整个事务回滚并有限重备，不逐个别名在 writer 中解析历史。
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
  `0056` 增加持久工作运行时，`0057` 增加子 Agent。已发布迁移不能原地修改。
- secret 永不回读；日志与错误不输出 token、Cookie、完整外部 ID、消息正文或本地敏感路径。

部署与数据升级分别见 [SnowLuma Provider 部署与切换](../deployment/snowluma.md) 和
[Yuki 3.8.2 升级指南](../upgrade-3.8.2.md)。

## 后台领取的事务边界

关系评估和普通记忆批次先在只读会话中准备事件、身份投影与候选，再用短写事务按
任务 id/status/updated_at 条件领取。竞争失败的候选不进入执行；live Memory 在提交时
重新核验事件仍位于当前 generation 水位之后。历史扫描不发生在写事务内。
关系上下文按 canonical Conversation 与 Person 取最近五条有效入站消息，不按当前
QQ Binding 丢弃同一人的其他账号证据。0065 添加对应的有序复合索引。
关系任务在同一查询读取 trigger 与会话 generation，并在领取 UPDATE 中复核；
准备期间发生遗忘或重置时不返回旧正文，之后可重读当前历史再领取。此处 generation
只保护准备快照，不把关系历史改成 Memory 的水位语义，也不是整个执行期间的租约。
这不延长旧任务的五分钟 processing 恢复窗口，也不宣称已经定位所有历史长锁事件。
