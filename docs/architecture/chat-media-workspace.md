# 当前聊天媒体与发送合同

这页描述源码中的当前实现。上线状态以实际镜像、数据库迁移和切换记录为准。

## 媒体引用与读取

入站消息在原位置留下 `[图片附件0]`、`[视频附件0]` 或 `[文件附件0：名称]` 等稳定文字标记。编号对应 `chat_events.id + attachment_index`：接入在同一短事务写入真实 segment 位置、canonical Conversation、generation 和媒体元数据。标记只说明附件存在，不表示 Yuki 已看过内容。文本上下文不放 URL、网关文件 ID、Base64 或缓存路径。

Bot 在消息入账后异步预取原件，不调用视觉模型。缓存目录为 `data/conversation-media-cache/v1/<conversation-id>/<event-id>/<index>-<sha256>.<verified-ext>`，容器内目录由 Compose 持久挂载。成功缓存后固定保留 24 小时，访问不续期；启动、每分钟及读取时核验到期。忘记用户导致来源事件删除时，索引随事件删除，清理器移除失去来源的缓存文件。单文件上限 200 MiB，单会话接纳预算 512 MiB，全局 4 GiB；空间不足会拒绝新缓存，不提前清除尚未到期的文件。失效的网关 URL 或不支持的类型会返回明确类别，不从网关容器本地路径读文件。

清理每轮最多只读发现 128 条过期元数据，短 writer 重新核验状态、期限、generation 和已发布
文件名后标为 expired。文件 GC 在线程中按固定目录层级流式推进，每页最多 128 项、路径容量
64 KiB，并在文件系统调用之间检查 100 ms 时间预算；不读取媒体正文或一次加载全部缓存路径。
已经开始的单个系统调用仍受操作系统 I/O 时延限制。扫描位置只保存在进程内，重启重新扫描；
积压通过后续轮次清理，不因此重新开放已过期或忘记来源的附件。
每页只查询候选内部事件及文件名的引用。GC 与最终发布共用锁，并在删除前复核 lstat 的文件
身份、类型、大小和修改时间；未过期引用、刚替换的文件和其他来源仍保留。取消等待中的 GC
先结束当前线程页，再释放锁，不能让后台 unlink 与新发布竞争。清理不改变接纳容量和授权规则。

主 Agent 的 `get_recent_chat_history`、`search_chat_history` 和附近查询返回真实内部事件 ID，并受当前 generation floor 约束。插件在当前会话的历史搜索也遵守同一 floor。模型可按语义、发送者和消息顺序选择候选，再调用 `inspect_conversation_attachment`。执行处从本轮运行时取得当前 canonical Conversation；入站消息使用已接纳的会话 ID，不依赖主动轮专用字段。随后核验事件为该会话的入站 keeper、generation 未变化且文件未过期；模型不能提供路径、平台消息 ID 或别的会话 ID 代替来源。图片和视频帧进入原主 Agent 的原生图片输入；文档仍由本地有界 reader 解析。历史工具不调用独立 Qwen，也不扣辅助模型请求；结果标明真实来源、hash 和处理模式。

## Agent 选取媒体与主链输入

当前、明确引用、历史附件与工作区看图复用 `NativeMediaPreparer`、已有图片解码器和视频采帧器。
GIF 保留有界代表帧，视频保留实际采帧秒数；视频不声称分析音轨。历史视频使用本轮运行时
时长、间隔与帧数预算。图片原件读取最多 20 MiB，prepared 图片仍受配置的字节预算；缓存
可以接纳 200 MiB 原件不代表一次看图可以读取同样多的内存。解码、文件读取和采帧不进入
SQLite writer。没有 `image_input` 时如实报告未读，不暗中调用 Qwen 或换模型。

`workspace_inspect` 接受 `path` 或 `artifact_id` 二选一和 `question`；path 可带刚读取的
`expected_version`。Manager 通过现有工作区目录 FD 核验路径、软链接/硬链接和真实内容版本，
返回冻结字节；这次读取不发布 artifact，也不修改文件。已发布 artifact 必须不可变。
工作区视频因控制通道的读取预算同样限制为 20 MiB；超限明确拒绝。Agent 可自行先用终端
生成较小文件/代表帧，再选择它查看，Host 不为其自动扫描全部工作文件或图片。

私有 `PreparedMediaData` / `ToolExecutionResult.images` 将像素与小型 JSON 回执分开。Runner
先按原工具调用顺序配对本批全部文字回执，再追加含原 call_id 的 Host 媒体观察，交给原固定
主模型继续。承载图片的 `user` 是协议角色，不新增真人消息、账本事件或用户授权。工具
缓存、同批别名和恢复携带原不可变媒体；公开文字回执、普通历史和展示不包含 Base64。

派发前在模型名额取得后再次核验历史来源的 keeper/inbound、Conversation、generation、floor、
TTL 与 hash；工作区路径重新核验版本，artifact 核验仍存在、未过期且不可变。不同来源的相同
hash 不共享授权。Work journal 保存选中图片、原 call_id 与顺序，工具回执已落地而 paired
检查点未落地时按原回执恢复图片，不重新下载、搜索或执行已确认效果。普通下一轮不自动
带入旧图，私有检查点只用于原执行恢复。

获准工具图片 使用同一纯准备器进入原生输入，工具图片归入原
读取授权的私有 artifact。`read_tool_artifact(operation="image")` 返回像素；text/get/search
仅返回图片 manifest。来源权限、owner、generation、有效期和字节完整性在读取前后及派发前
复核，不把任意 URL 当成可读取文件。插件自己拥有的媒体 handle 不因此开放跨插件读取；
SDK `vision.analyze_current_media` 是另一个明确授予 `VISION_ANALYZE` 的辅助合同。插件工具
本次显式返回 `PluginResult/ToolResult.media_artifacts` 才接入主模型；普通插件媒体句柄不自动附图。Host 保留实际插件、manifest revision、原工具与 TTL/hash，
派发前重验当前批准、精确委托及原文件；归档副本不延长授权。该桥接已完成本地定向测试，
真实 API 与生产验收另记。

Bot 与 sandbox Manager 的媒体读取实现须配套更新。来源、文件版本、GIF、采帧与
原生请求配对可离线核验；实际 Provider 图片能力、端到端速度和 QQ 自然流量分别验收。

`save_conversation_attachment_to_workspace` 先执行相同的临时读取授权，再显式复制到全局持久工作区。临时副本仍按原时间过期；已提升的工作文件由工作区规则管理，可在其他会话共享。提升受工作区单文件容量限制，失败不能声称文件已共享。独立插件计算会话没有主聊天临时媒体的读取权。

## 定时发送

普通图片仅在传输派发时构造 OneBot 媒体段；公开聊天账本与日志不保存 Base64 或本地路径。
私有恢复媒体按原 Work 的配额、引用和备份合同保存，不据此取得再次发送资格。
Genie 出站 record 构造已退出；历史 record、真实转写和已接受回执继续按原来源读取。

PR #139 已将新建静态提醒编译为 `social.send_message`，SELF 和用户使用 canonical Social 路由与持久回执。`delivery=none` 不发消息。自动化 DSL 不再注册旧 OneBot 私聊/群聊、语音、表情直接发送和通用 OneBot 调用；模型需要表情时使用主 Agent 的 `send_message` 结构化参数；语音合成与出站录音已退役，入站 ASR 和历史 record 继续读取。历史终态脚本和回执只读保留；迁移 `0072` 在存在启用或暂停的旧发送脚本时拒绝升级，需先人工核验，不猜测或重发外部效果。自动化主 Agent 的 OneBot 辅助网关禁止直接 `send_*` 和 `upload_*`。插件通知仍走插件自身的持久 outbox 和授权传输，属于独立的通知合同。
