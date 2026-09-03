# Yuki 3.8 canonical runtime

本文描述 Yuki 3.8 的现行架构合同，不是迁移任务书。3.8 运行时只支持 canonical schema，
Alembic head 为 `0050`。

## 永久主体与身份

一个数据库对应一个永久 Yuki。Yuki 的人格、SELF、记忆、关系、设置、插件状态和自动化不属于
某个 QQ 号，也不属于 NapCat、SnowLuma 或某条 WebSocket 连接。

核心身份对象如下：

- `Person`：一个永久的人类主体。关系、人物记忆和长期偏好按 Person 保存。
- `IdentityBinding`：Person 在某个平台上的外部账号。一个 Person 可以有多个 Binding。
- `Space`：永久共享空间；QQ 群只是它的一种外部表现。
- `SpaceBinding`：Space 与外部群号的绑定。
- `Presence`：Yuki 自己的平台账号。Presence 不是 Person。
- `CanonicalConversation`：私聊按 Person 唯一，群聊按 Space 唯一。显式 `/ai new` 才改变
  Conversation generation。

第三方机器人使用 `external_bot` 作者类型，不创建 Person、人物关系或人物记忆。事件作者只有
`person`、`yuki`、`external_bot`、`system` 四类；命令、插件和自动化属于 event origin，
不是作者类型。

## Conversation 与事件账本

OneBot 原始消息先投影正文、附件、按位置保序的 mention 和 reply，再解析 Person、Space、
Presence 与 canonical Conversation。事件账本保留平台 message ID、sender/group 外部 ID、Provider
和 Gateway provenance，以便幂等、审计和故障定位；这些字段不再承担业务所有权。

历史 alias 只用于稳定兼容键。多个 alias 可以指向一个 canonical Conversation，插件 API 2.0
始终读取固化的 primary alias。换 QQ、换 Provider、连接重建或路由接管都不得更改 Conversation
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

## 三类持久路由

运行时只保留三类业务路由：

1. Person 主动路由：决定主动私聊通过哪个 IdentityBinding 和 Presence 发送。
2. SpaceBinding ingest 路由：决定一个外部群的唯一入站 Presence，阻止多账号扇出重复处理。
3. Space 主动路由：决定向 canonical Space 主动发送时使用的 SpaceBinding 和 Presence。

路由变化只增加 RouteGeneration。路由暂停时失败关闭；群 ingest 不匹配的事件在策略、账本正文、
Agent 和 Memory 之前丢弃。事件触发的即时回复优先复用本次 ingress 连接，主动发送才读取持久路由。

群内超管的精确 `/ai on` 使用独立的确定性控制入口，不是绕过 ingest 的聊天事件：QQ adapter
验证真实事件连接与管理员 Binding，恢复服务保留健康接入 pin，否则仅接受唯一通过实时成员
探针的候选。群启用、必要的两张群路由变更、审计和幂等回执在同一个 `BEGIN IMMEDIATE` 中提交；
重验身份、路由 revision 与连接快照，冲突整笔退出。命令及回执不进聊天账本、模型、Memory 或
Relationship，不修改 ConversationGeneration。普通消息、插件和其他管理命令仍受原围栏限制。

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

关系、偏好、自动化目标、插件状态、Emoji、Speech、MCP 和配置投影均使用 canonical owner。
自动化在实际发送时解析当前路由，因此创建任务后更换 Yuki QQ 仍可沿新 Presence 投递。

Plugin API 保持 `2.0`。旧插件可继续使用 primary `conversation_key`；新 SDK 可读取可选的
person、space、conversation 和 presence ID。插件不能伪造管理员、绕过 Capability 或直接选择
任意 GatewayConnection。

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

## 数据库与安全边界

- 新数据库从无父 revision 的 `0048` canonical baseline 创建最终表，再升级到 `0049 -> 0050`。
- 历史桥接只接受已经完成 canonical v2 的旧 `0048` 数据库；更早或过渡态数据库失败关闭。
- 运行时没有 v1、dual-write、backfill 或 cutover 分支。
- `0049` 不提供 downgrade；`0050` 追加 `chat_events.caused_by_event_id` 及其索引。生产回退仍须
  停止写入并恢复升级前同一时点的 DB/WAL/SHM 快照。
- `/healthz` 保持公开瘦载荷；管理健康和连接详情只能通过授权后的控制面查询。
- secret 永不回读；日志与错误不输出 token、Cookie、完整外部 ID、消息正文或本地敏感路径。

部署与数据升级分别见 [SnowLuma Provider 部署与切换](../deployment/snowluma.md) 和
[Yuki 3.8.1 升级指南](../upgrade-3.8.1.md)。
