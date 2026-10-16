# 平台适配与升级

本页描述 #299 与多平台解耦分支的源码合同，不代表正式 Release 或生产部署。

当前实现尚有未满足合同的部分，见[实现后复审记录](Yuki-299与多平台解耦复审记录-2026-10-16.md)；身份清退、已付恢复、TG 编辑/记账及健康等缺口尚未修复。

## 同一个领域运行时

QQ、Telegram 和个人客户端共用身份、事件账本、Main、Work、Memory、自动化与插件 Host。后续平台继续实现同一 `GatewayProvider`，加入已有 `GatewayProviderCatalog`；不复制领域服务，不在公共服务里增加平台名称分支。

Provider 提供真实连接资料、入站投影和 `PlatformSocialOperations`。发送计划 `plan_send` 是无 I/O 的协议布局，每一项对应 Social 原操作下的一个实际派发与持久回执。`send` 每次只执行一项；原生长度限制、mentions、引用、文件上传及响应解析由适配器完成。公共 Social 管授权、原来源、预算、claim、事件和回执。媒体接口 `PlatformMediaGateway` 同样只在边界解析平台文件，并返回真实字节及媒体类型。

新增平台至少覆盖原生入站、接纳后确认、重复/编辑、连接启停、媒体、实际发送/撤回和失败事实。没有某项原生能力时，按真实不可用处理，不伪造 OneBot action 或成功结果。默认模型工具合同固定，目录查询不改变声明，权限在实际执行时核验。

## 身份与入口

业务参数使用内部 Person、Space、Presence、Binding、Conversation、事件与执行 ID。平台账号和消息定位保留在原生传输及历史 provenance。平台数字相同不证明同一人物或授权；只有真正关联的 Person 共享人物事实。

私聊以 Binding＋Presence 区分 Conversation；群以 SpaceBinding＋topic 区分 Conversation。同群切换接纳 Presence 不拆群身份。个人客户端同入口的多个设备共享会话。人物关联不合并不同入口的聊天历史。

旧 Conversation、primary alias、generation、Work、已付请求和真实效果回执继续沿原 ID。旧多 Bot 私聊只通过原内部事件和真实旧 alias 证明入口，不能按今天的主动路由重建归属。当前新消息按新入口规则接纳。

## 公共工具

`send_message` 共用参数表达文字、图片、文件、真实提及和内部事件引用。平台计划可以拆为数项；部分成功分别保留，结果未知不自动重发。App 文件及说明原子交付；QQ 上传与附言分别确认；Telegram 按实际文本和 caption 限制生成原生派发。

`read_conversation_history` 只读取授权的内部账本。默认当前入口；多入口候选返回内部 Conversation ID，由调用者选择。原 operation 可读取其真实出站会话，但不能冒领其他来源的回执。`get_group_members` 返回已观察人物，`coverage=observed` 不代表完整平台名册。查询不隐式请求远端历史、展开合并转发或刷新平台群名单。

默认模型的任意 OneBot action、戳一戳和远端/raw 历史入口已退役。独立批准的 QQ SDK 传输扩展保留明确的 QQ 边界，不能替代通用发送出口。历史已执行工具和回执继续可读。

新 call 每次读取当前数据并核当前权限；同参不复用另一 call 的业务结果。纯向量、不可变媒体和原 call 的持久回执继续复用。内部 Work、子任务和自动化不要求在线网关或相同连接代数；真实平台操作才取得当前连接。

## Telegram 首期接入

在部署使用的环境文件设置 `TELEGRAM_BOT_TOKEN`。源码使用 long polling，支持普通 Bot 私聊、群及 topic、文字和常见媒体、引用、命令与原执行来源。QQ-only 可不配置此项；App/TG-only 不需要虚填 QQ 管理员或 SnowLuma 凭据。

平台更新确认表示事件已按原入口入账或明确忽略，不表示 Main 或平台发送已经完成。原更新重投返回原事件；编辑更新原事件，不再次启动 Main 或重发。原生群迁移保留真实 SpaceBinding/Space。已接纳 Work 沿原 journal 和回执恢复；普通无 Work 聊天遇进程中断，不自动重新购买模型请求或重发。Channel、Business 和 inline 不伪装成普通用户会话。

Bot 必须具备实际群权限及所需可见性。未收到的消息不属于已观察历史。Token、下载凭据和平台 file_id 留在 Telegram 边界，不进入模型工具结果；跨平台传文件沿工作区快照/字节重新准备。

## 从已有数据升级

迁移顺序为 `0122` 会话入口、`0123` Memory 操作身份、`0124` 通知 Conversation 和 `0126` 监控目标。保留既有库、挂载和配置，运行真实随包迁移；不能 `stamp` 跳过，也不能覆盖旧库来回退代码。

Plugin API 升为 **4.0**，Main 合同为 **20**，Code API 仍为 `yuki.codemode.api.v2`。新插件的 Person/Space 参数使用内部 ID，通知可明确原 Conversation；同名参数旧的裸平台号语义不能静默沿用，详见 [API 4.0 迁移](../plugin-development/api-4.0-migration.md)。旧 frozen 请求、checkpoint、通知 source、outbox 和回执不改写成新调用。

监控旧状态只按真实历史 grant 迁移。来源未知、存在歧义或目标合并冲突时，完整保留源 JSON 并报告具体升级问题，不能创建空状态覆盖它，也不能用今天的同数字账号猜 owner。监控迁移保原游标、源指纹、正文、notification/source_event ID、状态和已接受投递，不能重新抓取或重发代替恢复。

养鲲插件按用户要求移到独立 [Yuki-Kun-Game](https://github.com/YuanYeYouTao/Yuki-Kun-Game) 仓库；主仓库不再附带游戏代码、玩法文档、测试或专用数据库迁移。旧游戏 KV 不删除，离线升级脚本由独立插件仓库维护。

本地验证与实际平台、付费模型、合并、Release、上线分别记录。离线 payload 和缓存字段回归不证明真实缓存命中率；usage 缺字段时保持未知。实施与当前验证见[实施记录](Yuki-299与多平台解耦实施记录-2026-10-16.md)。
