# 当前聊天媒体与发送合同

这页描述源码中的当前实现。上线状态以实际镜像、数据库迁移和切换记录为准。

## 媒体引用与读取

入站消息在原位置留下 `[图片附件0]`、`[视频附件0]` 或 `[文件附件0：名称]` 等稳定文字标记。编号对应 `chat_events.id + attachment_index`：接入在同一短事务写入真实 segment 位置、canonical Conversation、generation 和媒体元数据。标记只说明附件存在，不表示 Yuki 已看过内容。文本上下文不放 URL、网关文件 ID、Base64 或缓存路径。

Bot 在消息入账后异步预取原件，不调用视觉模型。缓存目录为 `data/conversation-media-cache/v1/<conversation-id>/<event-id>/<index>-<sha256>.<verified-ext>`，容器内目录由 Compose 持久挂载。成功缓存后固定保留 24 小时，访问不续期；启动、每分钟及读取时核验到期。单文件上限 200 MiB，单会话接纳预算 512 MiB，全局 4 GiB；空间不足会拒绝新缓存，不提前清除尚未到期的文件。失效的网关 URL 或不支持的类型会返回明确类别，不从网关容器本地路径读文件。

主 Agent 的 `get_recent_chat_history`、`search_chat_history` 和附近查询返回真实内部事件 ID，并受当前 generation floor 约束。插件在当前会话的历史搜索也遵守同一 floor。模型可按语义、发送者和消息顺序选择候选，再调用 `inspect_conversation_attachment`。执行处核验事件为当前 canonical Conversation 的入站 keeper、generation 未变化且文件未过期；模型不能提供路径、平台消息 ID 或别的会话 ID 代替来源。图片、视频帧和受支持文档按需解析，结果标明来源、hash 和处理模式。当前/显式回复附件仍可通过主 Agent 的原生输入立即读取；历史媒体工具的视觉结果是有来源的结构化观察。

`save_conversation_attachment_to_workspace` 先执行相同的临时读取授权，再显式复制到全局持久工作区。临时副本仍按原时间过期；已提升的工作文件由工作区规则管理，可在其他会话共享。提升受工作区单文件容量限制，失败不能声称文件已共享。独立插件计算会话没有主聊天临时媒体的读取权。

## 一次性切换

新代码首次上线前，在 Bot 停止接纳后保存一致的数据库与缓存/工作区备份。`python -m qq_ai_bot.operations.reset_conversations --batch-id <唯一批次>` 只读预览；加 `--apply-offline` 才对每个 canonical 私聊、群聊执行一次与 `/ai new` 相同的 generation/reset 领域操作。批次表记录每个会话的前后 generation 和事件起点，重复同批次不会再重置。仍启用或暂停、且授权快照本来属于旧 generation 的 SELF 定时任务在同一事务中重绑到新 generation；已经失效的快照不会被复活。该操作不调用模型、不发送 QQ 消息、不改写旧历史；旧原始事件和长期 Memory 保留。只有全量完成并核对后才重启 Bot。仍启用任务的运行中自动化、执行中或等待外部效果的 Work 会阻止切换；旧 generation 的暂停 Work 按 `/ai new` 语义取消，历史运行回执不改写。

## 定时发送

PR #139 已将新建静态提醒编译为 `social.send_message`，SELF 和用户使用 canonical Social 路由与持久回执。`delivery=none` 不发消息。自动化 DSL 不再注册旧 OneBot 私聊/群聊、语音、表情直接发送和通用 OneBot 调用；模型需要语音或表情时使用主 Agent 的 `send_message` 结构化参数。历史终态脚本和回执只读保留；迁移 `0072` 在存在启用或暂停的旧发送脚本时拒绝升级，需先人工核验，不猜测或重发外部效果。自动化主 Agent 的 OneBot 辅助网关禁止直接 `send_*` 和 `upload_*`。插件通知仍走插件自身的持久 outbox 和授权传输，属于独立的通知合同。
