# 平台适配与升级

本页描述当前源码合同；正式 Release 与实际生产部署版本分别记录。

实现后13项复审缺口已本地补修，见[复审记录](Yuki-299与多平台解耦复审记录-2026-10-16.md)及其正式验证；真实平台、原生Code VM及合并上线状态另列。

## 同一个领域运行时

QQ、Telegram 和个人客户端共用身份、事件账本、Main、Work、Memory、自动化与插件 Host。后续平台继续实现同一 `GatewayProvider`，加入已有 `GatewayProviderCatalog`；不复制领域服务，不在公共服务里增加平台名称分支。

Provider 提供真实连接资料、入站投影和 `PlatformSocialOperations`。发送计划 `plan_send` 是无 I/O 的协议布局，每一项对应 Social 原操作下的一个实际派发与持久回执。`send` 每次只执行一项；原生长度限制、mentions、引用、文件上传及响应解析由适配器完成。公共 Social 管授权、原来源、预算、claim、事件和回执。媒体接口 `PlatformMediaGateway` 同样只在边界解析平台文件，并返回真实字节及媒体类型。

已由原 Conversation/Presence 或明确主动路由选定的群发送不再预先查询 Bot 自身成员状态；成员查询不能证明实际可发言。Social 复用本次准备的目标和路由，短 writer 核验真实 Binding、Presence、连接与原 claim，平台网络在 writer 外执行，ACK 后原子保存出站事件与成功回执。多候选发现仍按真实成员资料消歧，实际发送失败或结果未知不换账号补发。分条继续按原配置间隔顺序派发；读取 sequence 回执批量获取原操作和账本，保留来源、隐私及部分成功事实。

Provider 的 `media(handle)` 从该原生连接提供媒体适配器。附件工具按所选原事件的 `ingress_presence_id` 取原接收连接，不读取工具上下文的当前网关，也不按平台账号数字补归属。已有缓存、公共 URL 和内联资料不要求原连接在线；只有解析原生文件引用才取得原 Presence 的连接，不把旧 file_id 交给另一 Bot。App 上传沿已保存缓存读取，没有远端文件查询能力时返回真实不可用。

新增平台至少覆盖原生入站、接纳后确认、重复/编辑、连接启停、媒体、实际发送/撤回和失败事实。没有某项原生能力时，按真实不可用处理，不伪造 OneBot action 或成功结果。默认模型工具合同固定，目录查询不改变声明，权限在实际执行时核验。

## 身份与入口

业务参数使用内部 Person、Space、Presence、Binding、Conversation、事件与执行 ID。平台账号和消息定位保留在原生传输及历史 provenance。平台数字相同不证明同一人物或授权；只有真正关联的 Person 共享人物事实。

私聊以 Binding＋Presence 区分 Conversation；群以 SpaceBinding＋topic 区分 Conversation。同群切换接纳 Presence 不拆群身份。个人客户端同入口的多个设备共享会话。人物关联不合并不同入口的聊天历史。

旧 Conversation、primary alias、generation、Work、已付请求和真实效果回执继续沿原 ID。旧多 Bot 私聊只通过原内部事件和真实旧 alias 证明入口，不能按今天的主动路由重建归属。当前新消息按新入口规则接纳。

## 事件携带的展示资料

QQ 普通消息直接读取 SnowLuma 已附带的 `group_name` 与 `sender.nickname/card`；
Telegram 普通消息直接读取 `chat.title` 和发送者名称。真实文字或媒体照常进入原处理链，
展示字段缺失不发起额外远端资料查询。人物观察与首次入账一起完成，未知值不擦除已有资料，
明确空群名片保持清空语义；Person 独立展示名仍遵守首次补齐与显式改名规则。

QQ `notice/notify` 的 `sub_type=group_name` 使用原注册连接和既有群绑定更新群名。
Telegram `new_chat_title` 只更新原群资料；这类资料通知不伪装真人空聊天，不进入聊天账本、
JEV、Main、Work 或等待输入。资料更新不启用群、不转移绑定或改变路由。
原事件与已冻结历史的名称保持不变；没有接收到的资料保持未知，不在回复路径补查。

原生提及保留原句位置、重复次数及目标。QQ 从原 `at` segment 捕获目标，Telegram 从
`mention` / `text_mention` 捕获实体，按 UTF-16 偏移定位正文或 caption；普通 `@文字`
不生成原生提及。入账前用本次携带名称和既有本地资料生成 `@名字` 与内部身份快照，
不等待平台补查。未知名字照常接纳，不把其他成员统一写成“成员”，也不把引用中的提及
当成本条消息对 Yuki 的提及。历史和 JEV 直接消费原快照，不增加接话门禁。

## 空输入与处理状态

Telegram 没有可投影的当前正文或受支持附件时静默忽略；已有正文或媒体的引用仍保留原关系，单独的原生 reply 字段不构成当前新发言。群迁移仍沿原专用分支处理，确认更新不产生固定空输入回复。共用 Processor 对完全没有正文、附件、Bot 提及、引用内容或引用 ID 的输入，在模型固定、入账、通知当前轮和 Work/JEV 接纳之前返回，不打断正在执行的工作。QQ 的纯附件、只提及 Bot 和真实引用继续走原路径；引用解析后仍没有可处理内容时静默结束，不把旧消息自动当成新问题。媒体格式与历史投影继续遵守[媒体合同](chat-media-workspace.md)。

正在输入属于临时平台状态。`GatewayProvider.activity(handle, target)` 返回可选 `PlatformActivity`，由原 Registry/Catalog 透传；适配器提供刷新间隔、`start` 和 `stop`。原连接能力声明用 `typing_private`、`typing_group` 表达支持范围；已知 Presence 时先从内存 Registry 判断，不支持便跳过提示的路由查询和平台请求。支持或尚无 Presence 时仍用原 Conversation、Presence 和内部 source event 解析实际连接与目标。该能力仅描述连接的原生 UI 动作，不改变固定模型工具声明或提示词。QQ 当前只在私聊使用 SnowLuma 的 `set_input_status`，开始为 `event_type=1`，最后退出后由后台清理发送 `0`；QQ 群和 App 没有此原生动作时返回无实现。Main 开始和结束都不等待这些平台请求；状态服务保留清理任务，相邻同入口的提示只在后台等待原停止请求完成或超时后再开始。

Telegram 使用 `sendChatAction(action=typing)`，保真实 chat 与 topic，每 4 秒刷新；Main 退出后请求停止续期，在途 UI 请求与停止仅由后台收尾，不虚构停止 API。Telegram 的状态最多保留 5 秒，Bot 发消息时也会清除，见[原生 API](https://core.telegram.org/bots/api#sendchataction)。提示失败只记诊断并结束该提示，不改变 Main 结果或发送回执；可见状态不能证明模型成功、消息已发送或工作已完成。

生命周期与刷新参考 Memoh 本地快照 `7e40b52ec46d076c17d93e578f5fc18f88742f97` 的[适配接口](https://github.com/felinics/Memoh/blob/7e40b52ec46d076c17d93e578f5fc18f88742f97/internal/channel/adapter.go#L38-L60)、[运行时状态生命周期](https://github.com/felinics/Memoh/blob/7e40b52ec46d076c17d93e578f5fc18f88742f97/internal/channel/processing_status.go#L31-L121)和[Telegram 刷新实现](https://github.com/felinics/Memoh/blob/7e40b52ec46d076c17d93e578f5fc18f88742f97/internal/channel/adapters/telegram/telegram.go#L2428-L2484)。Yuki 的接线归属与生命周期见[主 Agent 合同](main-agent-runtime.md)。

## 公共工具

`send_message` 共用参数表达文字、图片、文件、真实提及和内部事件引用。平台计划可以拆为数项；部分成功分别保留，结果未知不自动重发。App 文件及说明原子交付；QQ 上传与附言分别确认；Telegram 按实际文本和 caption 限制生成原生派发。

`read_conversation_history` 只读取授权的内部账本。默认当前入口；多入口候选返回内部 Conversation ID，由调用者选择。原 operation 可读取其真实出站会话，但不能冒领其他来源的回执。`get_group_members` 返回已观察人物，`coverage=observed` 不代表完整平台名册。查询不隐式请求远端历史、展开合并转发或刷新平台群名单。

默认模型的任意 OneBot action、戳一戳和远端/raw 历史入口已退役。独立批准的 QQ SDK 传输扩展保留明确的 QQ 边界，不能替代通用发送出口。历史已执行工具和回执继续可读。

新 call 每次读取当前数据并核当前权限；同参不复用另一 call 的业务结果。纯向量、不可变媒体和原 call 的持久回执继续复用。内部 Work、子任务和自动化不要求在线网关或相同连接代数；真实平台操作才取得当前连接。

## Telegram 首期接入

在部署使用的环境文件设置 `TELEGRAM_BOT_TOKEN`。源码使用 long polling，支持普通 Bot 私聊、群及 topic、文字和常见媒体、引用、命令与原执行来源。QQ-only 可不配置此项；App/TG-only 不需要虚填 QQ 管理员或 SnowLuma 凭据。

Telegram API 与平台文件下载共用同一原生 HTTP 客户端，沿标准 `HTTP_PROXY`、`HTTPS_PROXY`、`ALL_PROXY`、`NO_PROXY` 和 CA 环境配置；修改后重新创建 Bot 容器。无需 Telegram 专属代理开关，代理失败不自动改成直连，详见[代理配置](../operations/http-proxy.zh-CN.md)。

平台更新确认表示事件已按原入口入账或明确忽略，不表示 Main 或平台发送已经完成。原更新重投返回原事件；编辑更新原事件，不再次启动 Main 或重发。原生群迁移保留真实 SpaceBinding/Space。已接纳 Work 沿原 journal 和回执恢复；普通无 Work 聊天遇进程中断，不自动重新购买模型请求或重发。Channel、Business 和 inline 不伪装成普通用户会话。

Bot 必须具备实际群权限及所需可见性。未收到的消息不属于已观察历史。Token、下载凭据和平台 file_id 留在 Telegram 边界，不进入模型工具结果；跨平台传文件沿工作区快照/字节重新准备。

### 固定工具的 Telegram 适配范围

TG 与 QQ 使用同一份平台 Main 工具合同，默认 direct 为 29 项；不按当前平台动态增删声明。

| 工具 | Telegram 路径与范围 |
| --- | --- |
| `send_message` | 原生文字、图片和文件；真实提及、内部事件引用、topic 与 caption 由适配器转换，每次派发保原回执。 |
| `recall_own_message` | 内部出站事件定位原发送 Presence，再执行 `deleteMessage`，当前主动账号不能替代原账号。 |
| `query_chat_history` / `read_conversation_history` | 授权内部事件账本；已观察范围，不查询 Telegram 服务器完整历史。 |
| `find_contacts` / `get_group_members` | 内部人物、群与已观察成员目录；不冒充 TG 完整成员列表。 |
| `inspect_conversation_attachment` / `save_conversation_attachment_to_workspace` | 原事件附件与真实接收账号；命中缓存可离线使用，原生文件未缓存时才经原账号 `getFile` 下载字节。 |
| 记忆、终端、工作区、搜索、任务、子 Agent 与自动化 | 共用领域服务、内部执行和来源，不复制 TG 专属工具；仍需各功能正常配置与真实权限。 |

`get_my_capabilities` 查询当前主体的权限和配置能力，不代表 Telegram 机器人管理员权限探测。原生协议离线回归不等于已有真实 TG 账号收发验收；Token、实际网络、群权限与自然运行分别核对。

## 从已有数据升级

迁移顺序为 `0122` 会话入口、`0123` Memory 操作身份、`0124` 通知 Conversation、`0126` 监控目标、`0127` Rebuild 原 canonical 目标与 `0128` 通知原媒体 handle。保留既有库、挂载和配置，运行真实随包迁移；不能 `stamp` 跳过，也不能覆盖旧库来回退代码。

当前 Plugin API 为 **5.0**，Main 合同为 **24**，Code API 仍为 `yuki.codemode.api.v2`。插件的 Person/Space 参数使用内部 ID，通知可明确原 Conversation；同名参数旧的裸平台号语义不能静默沿用，身份升级见 [API 4.0 迁移](../plugin-development/api-4.0-migration.md)，旧表情 facade 退役见 [API 5.0 迁移](../plugin-development/api-5.0-migration.md)。旧 frozen 请求、checkpoint、通知 source、outbox 和回执不改写成新调用。

监控旧状态只按真实历史 grant 迁移。来源未知、存在歧义或目标合并冲突时，完整保留源 JSON 并报告具体升级问题，不能创建空状态覆盖它，也不能用今天的同数字账号猜 owner。监控迁移保原游标、源指纹、正文、notification/source_event ID、状态和已接受投递，不能重新抓取或重发代替恢复。

养鲲插件按用户要求移到独立 [Yuki-Kun-Game](https://github.com/YuanYeYouTao/Yuki-Kun-Game) 仓库；主仓库不再附带游戏代码、玩法文档、测试或专用数据库迁移。旧游戏 KV 不删除，离线升级脚本由独立插件仓库维护。

本地验证与实际平台、付费模型、合并、Release、上线分别记录。离线 payload 和缓存字段回归不证明真实缓存命中率；usage 缺字段时保持未知。实施与当前验证见[实施记录](Yuki-299与多平台解耦实施记录-2026-10-16.md)。

本轮发送链优化保留原 Conversation 执行顺序、模型逻辑请求名额及分条间隔；未改变退避期间
的 slot 调度。OneBot 在 matcher 前解析引用的查询仍保留，模型响应沿原非流式协议完成后
执行工具；现有读取并行段和发送屏障也保留。锁收窄、独立附件并行和流式均只是先计量的
候选，没有等价与收益证据时不以新增调度机制代替删除重复工作。
