# 当前聊天媒体与发送合同

这页描述源码中的当前实现。上线状态以实际镜像、数据库迁移和切换记录为准。

## 普通文件与交付快照

普通文件的读写、搜索、目录、移动、删除和局部编辑统一走持久终端，使用正常 Linux
文件语义；不再提供 `workspace_list/read/write/search/patch/mkdir/move/delete`，也不保留
旧 artifact 的公开目录、分页文本读取和删除兼容接口。终端操作不要求工作区版本或 CAS。

工作区只保留 `workspace_inspect` 和 `workspace_publish` 两条桥。前者将选定的真实像素交给
原模型，后者把终端文件制作成不可变 artifact，返回内部 ID 后再由 `send_message` 发送。
终端后来改写或删除原文件，不改变已经发布的快照与投递回执。发布与普通 artifact 读回
没有固定 200 MiB 单文件、512 MiB 总量或 1000 对象配额；真实磁盘和平台传输错误照实返回。
模型看图的字节与帧数预算属于实际输入准备，不等于发布文件的容量。

不可变存储仍保存原来源、内容哈希与引用，供 Social 发送、插件媒体投递、Client 下载和
短状态使用。旧导入迁移仍可列举内部快照；引用回收不依赖已删除的对象配额。普通文件
工具退出不删除历史快照、私有工具结果或原已确认执行回执，旧外置页按原 `result_ref`
读取，不重新读取今天的文件。

## 媒体引用与读取

入站消息在原位置留下 `[图片附件0]`、`[视频附件0]` 或 `[文件附件0：名称]` 等稳定文字标记。编号对应 `chat_events.id + attachment_index`：接入在同一短事务写入真实 segment 位置、canonical Conversation、generation 和媒体元数据。标记只说明附件存在，不表示 Yuki 已看过内容。公共历史不放 URL、网关文件 ID、Base64 或缓存路径；当前激活的附件索引可提供共享沙箱的真实文件路径或尚在下载的目录，不把待缓存冒充已读。

原生编辑在同一个原事件更新正文、提及与附件索引。仅 caption 或位置变化时复用同一媒体的原缓存期限；替换媒体后当前索引改指新原件，旧下载不得覆盖新来源。历史附件解析前后核当前来源、路径及内容版本，文档正文不能搭配另一版本的哈希。已经冻结的请求与工具结果保留原件；旧版本缓存沿原文件寿命回收，来源被遗忘或删除时不继续保留。

当前消息入账后，Host 为每个实际图片、视频、文件与音频准备同一份本次原件引用，供缓存、明确读取、ASR 和原生表情收藏共享下载。大件流式落盘，各消费者按自己的真实输入预算读取；不按 URL 跨事件缓存。Main 首次请求不等待下载、像素解码、视频采帧、文档解析、永久收藏或索引描述。后台发表仍核对原实际来源，编辑不能把旧字节接到新附件上；实际下载、复制与清理沿原 owner 收拢，不让取消后的物理 I/O 脱离生命周期。

Bot 是原件缓存的唯一 writer 与清理 owner。Host 的 `data/conversation-media-cache` 同份目录只读映射到共享环境 `/inputs/media-cache`，各会话的 Agent 可直接读或复制，Main 的终端不获得 Bot 数据库、Host 私有目录或凭据。普通缓存文件位于 `v1/<conversation-id>/<event-id>/<index>-<sha256>.<verified-ext>`，App 原上传沿 `v1/uploads/` 保存。没有双份持久缓存，也不把媒体原件塞进持久 HOME。部署 Manager 需显式配置 `YUKI_MANAGER_MEDIA_CACHE`；已有环境的挂载不会因更新 Python 文件自动出现，升级按原私有 checkpoint 保留 rootFS 后明确替换一次环境，见[持久环境](../operations/persistent-environment.zh-CN.md)。

成功缓存后固定保留 24 小时，访问不续期；启动及周期清理按原期限删除文件。删除、遗忘导致原来源消失时，元数据与托管缓存一并清退；已经显式复制到共享工作区或图库的普通文件沿自己的生命周期保存。不再有单文件 200 MiB、单会话 512 MiB、全局 4 GiB 或待处理达到 32 就跳过的接纳门槛。真实网络、平台及磁盘错误照实返回，不从网关容器本地路径读文件。期限是文件生命周期：清理前仍存在的原件可读，实际删除后不重新下载以重置寿命，也不为终端叠加到期读取门禁。

选读成功的像素属于该次工具结果；原 Work 的不可变协议资料可继续保存和恢复，不依赖临时缓存文件仍存在。模型派发仍核原事件、附件归属、内容 hash 和实际来源版本；遗忘、删除原事件或替换原件会使旧来源失效。新一次读取仍核真实缓存及期限，不能凭已归档图片重新下载过期原件。共享图片不导入另一 Conversation 的文本历史，也不以当前 Conversation 再次拒绝已获准的文件读取。

平台实际提供的贴纸缩略图由同一缓存 owner 在后台保存到原件所在目录，文件名保留预览元数据 hash 与原件名。显式读取、原生表情收藏的可选描述共享本次预览下载；缓存命中后不要求原接收连接在线。预览沿原件的 `cached_at` 和 24 小时期限清理，首次或重复查看不续期，遗忘同时清掉原件和预览。只有图注或位置变化时复用原预览，真实缩略图变化不复用旧像素。缩略图下载失败只影响预览，已经成功缓存或收藏的原件继续保留；首次 Main 不等待这项后台工作。

清理每轮最多只读发现 128 条过期元数据，短 writer 重新核验状态、期限、generation 和已发布
文件名后标为 expired。文件 GC 在线程中按固定目录层级流式推进，每页最多 128 项、路径容量
64 KiB，并在文件系统调用之间检查 100 ms 时间预算；不读取媒体正文或一次加载全部缓存路径。
已经开始的单个系统调用仍受操作系统 I/O 时延限制。扫描位置只保存在进程内，重启重新扫描；
积压通过后续轮次清理，已删或被遗忘的来源不能触发重新下载。
每页只查询候选内部事件及文件名的引用。GC 与最终发布共用锁，并在删除前复核 lstat 的文件
身份、类型、大小和修改时间；未过期引用、刚替换的文件和其他来源仍保留。取消等待中的 GC
先结束当前线程页，再释放锁，不能让后台 unlink 与新发布竞争。正在写入的暂存文件由实际 owner 引用保留，mtime 变旧不证明写入已经结束。

主 Agent 的 `query_chat_history` 按 recent/search/around 返回真实内部事件 ID，聊天上下文与历史读取仍认原 Conversation 和 generation floor。共享原文件是独立的文件访问范围：`inspect_conversation_attachment` 与保存桥按内部 event/index 找到真实入站 keeper，不另加当前 Conversation、generation 或 floor 的读取限制，也不向调用者拼入另一会话的历史。图片与视频选读后才进入原 Main 的原生视觉输入；文档沿本地有界 reader 解析。附件工具不调用独立 Qwen，也不扣辅助模型请求，结果标明真实来源、hash 和处理模式。

已知引用复用原 keeper 的内部事件与附件；Yuki 的出站图片沿真实不可变 artifact。原消息从未入账但当前确实观察到引用附件时，`source_snapshot_json` 只保存这一实际 segment，并在本次真实事件下追加附件槽，当前索引标明 `reply_observed`。不从平台消息号制造过去事件、作者或对话。快照在首次 writer 前序列化，编辑沿原内部引用事实处理，不在写事务解析整份历史。

附件工具命中缓存时直接读取原件；未缓存的原生文件按原事件的 `ingress_presence_id` 取得原接收连接，由 Provider 的 `media(handle)` 下载。当前工具所在账号不能替代原接收账号；公共 URL、内联资料和已有缓存不要求原连接在线。

## 已观察媒体与历史

合并转发在外层原事件保留标记、原 segments 和内部事件 ID。默认模型不再请求远端
转发节点；公共历史只读已入账事件，明确 observed 覆盖。原已付请求与历史工具结果
不追溯改写，退役能力不据此重新派发。

入场不展开 forward，不递归读取内层媒体、人物或记忆。全部外层图片、视频、贴纸和文件在首次 Main 输入只提供实际附件元信息；有正文、无正文和明确引用都沿同一份索引，附件存在不等于已经读取。旧 `AttachmentInputService`、自动 NativeInput、文档预加载及固定视觉失败回复整条链已退役，没有新的自动开关或关键词分流。

共同媒体链消费 PlatformMediaGateway 的真实字节或可关闭流。QQ get_image/get_record、SILK、Telegram getFile 与凭据留在适配器；共同缓存和 ASR 复用原件，不伪造 OneBot 调用，也不把平台 file_id 跨端当文件。QQ `get_record(out_format=mp3)` 缓存平台实际交付的音频，不声称取得原 SILK。TG voice/audio 保存实际下载字节。

## Agent 选取媒体与主链输入

明确选读当前、引用、历史附件与工作区看图复用 `NativeMediaPreparer`、已有图片解码器和视频采帧器；首次 Main 不自动调用这些准备器。
裸图片字节由同一解码器计算原件哈希，包装层不再预先重复扫描；下载器提供的期望哈希仍核验，
不改变像素、来源、帧次序或预算。
GIF 按真实字节识别，包含 Telegram animation 的缺 MIME 或误扩展名情况，保留有界代表帧；视频保留实际采帧秒数；视频不声称分析音轨。历史视频使用本轮运行时
时长、间隔与帧数预算。图片原件读取最多 20 MiB，prepared 图片仍受配置的字节预算；缓存
没有经验接纳配额不代表一次看图可以无限读取内存。解码、文件读取和采帧不进入
SQLite writer。没有 `image_input` 时如实报告未读，不暗中调用 Qwen 或换模型。

`workspace_inspect` 接受 `path` 或 `artifact_id` 二选一和 `question`；path 可带已选定文件的
`expected_version`，不要求先调用已退役的文件读取工具；`/inputs/media-cache/` 与 `/workspace/` 都沿现有桥读取。Manager 通过实际源目录 FD 核验路径、软链接/硬链接和真实内容版本，
返回冻结字节；这次读取不发布 artifact，也不修改文件。已发布 artifact 必须不可变。
工作区视频因控制通道的读取预算同样限制为 20 MiB；超限明确拒绝。Agent 可自行先用终端
生成较小文件/代表帧，再选择它查看，Host 不为其自动扫描全部工作文件或图片。

私有 `PreparedMediaData` / `ToolExecutionResult.images` 将像素与小型 JSON 回执分开。Runner
先按原工具调用顺序配对本批全部文字回执，再追加含原 call_id 的 Host 媒体观察，交给原固定
主模型继续。承载图片的 `user` 是协议角色，不新增真人消息、账本事件或用户授权。工具
缓存、同批别名和恢复携带原不可变媒体；公开文字回执、普通历史和展示不包含 Base64。

派发前在模型名额取得后再次核验选中媒体的真实 keeper、源版本和 hash，已选像素不要求临时原件文件仍存在；共享媒体不再被本轮 Conversation、generation 或 floor 拦截。上下文来源检查只核实际聊天输入，不因附图而把另一会话的事件变成本轮历史依赖。工作区路径仍核实际版本，artifact 保不可变。Work journal 保存选中图片、原 call_id 与顺序，工具回执已落地而 paired 检查点未落地时按原回执恢复，不重新下载、搜索或执行已确认效果。普通下一轮不自动带入旧图，私有检查点只用于原执行恢复。

获准工具图片 使用同一纯准备器进入原生输入，工具图片归入原
读取授权的私有 artifact。`read_tool_artifact(operation="image")` 返回像素；text/get/search
仅返回图片 manifest。来源权限、owner、generation、有效期和字节完整性在读取前后及派发前
复核，不把任意 URL 当成可读取文件。插件自己拥有的媒体 handle 不因此开放跨插件读取；
SDK `vision.analyze_current_media` 是另一个明确授予 `VISION_ANALYZE` 的辅助合同。
它按实际原件内容 hash、问题和模型配置复用观察；同一事件编辑换图后读取新原件，同图不重复购买分析。
插件工具本次显式返回 `PluginResult/ToolResult.media_artifacts` 才接入主模型；普通插件媒体句柄不自动附图。Host 保留实际插件、manifest revision、原工具与 TTL/hash，
派发前重验当前批准、精确委托及原文件；归档副本不延长授权。该桥接已完成本地定向测试，
真实 API 与生产验收另记。

Bot 与 sandbox Manager 的媒体读取实现须配套更新。来源、文件版本、GIF、采帧与
原生请求配对可离线核验；实际 Provider 图片能力、端到端速度和 QQ 自然流量分别验收。

`save_conversation_attachment_to_workspace` 按同一真实内部来源显式复制到全局持久工作区；也可直接用终端从共享缓存 `cp` 到自己的目录。临时原件仍按原时间清理，持久副本自由编辑。保存桥沿原 request 和快照回执处理，不受已删除的工作区单文件配额阻挡；真实磁盘或导入失败照实返回。共享 Agent 文件范围不增加插件 SDK 的批准权限，插件 handle 仍沿自身合同。

## 入站语音

ASR 沿配置的下载大小、单段时长、队列并发及整个入站准备截止时间处理音频。
按真实附件顺序转写当前和引用语音，不固定只取三段，也不把已识别的长文字报告为无语音。
`asr.protocol` 明确选择 `chat_completions`（默认 Qwen `input_audio`）或
`audio_transcriptions`（`/audio/transcriptions` multipart 已转码音频、model 与纯转写 prompt，读取 `text`）；
连接和模型沿实际配置，不按模型名猜协议，不失败改路或重试付费调用。ASR 配置重启后生效。
Provider 响应继续受实际读取字节预算和完整性检查约束；保存及读取原转写保留全部文字和来源。
部分失败保留已完成转写并报告真实错误，空转写仍为 no_speech。
ASR 与视频的临时目录、文件读写和清理在共享事件循环外完成；取消等实际文件操作收拢后再清理原件。

## 共享表情目录与后台配图

平台明确标记的表情从本次共享原件直接收藏到持久环境的 `/workspace/stickers/<platform>/files/`，不再次下载；QQ 只有编号的内置 face 保留文字语义。原生贴纸标记和实际 MIME/动画格式分开保存，Telegram WEBP/TGS/WEBM 原件不以静态图片解码成功作为收藏条件。普通图片可通过现有保存桥显式放入对应平台目录，原目的路径不被静默改写。平台目录是编目用途，原事件/人物来源独立保留，不按账号或群再拆库。旧 collector、worker、候选池、采用/作用域、容量替换、冷却、拼图和专属插件 facade 已退役。

原图与 `stickers/<platform>/index/<asset-id>.json` 是唯一图库资料源，短 tags 与长 description/OCR 分开。Main 与 Qwen 自由编辑相同文件；Yuki 主业务库不保存资产、标签、来源或倒排资料。每平台 `search.sqlite3` 是可删除重建的派生缓存。独立 helper 在原持久环境运行，Main 可直接 update/rebuild/search；Manager 调同一实现，Host 不直接连接用户可编辑路径的 SQLite。任意终端编辑后新标签在 update/rebuild 完成后可搜；命中项重读当前 JSON 和原件，不全库扫描、不从缓存回写旧资料。冷构建不冒充无候选，也不阻塞正文。自动选图只使用明确的 goal/emotion，中文连续段与其他语言词、原生 emoji 分别取词，不把混合标签中的英文拆成单字；分词格式变化沿派生缓存版本重建，原图与 JSON 不变。

Main 主动收藏聊天图片时，直接沿 `save_conversation_attachment_to_workspace` 把原件存入 `stickers/<platform>/files/<filename>`，不必为了保存再先看图。已有工作区图片也可用普通 `cp` 放进去。随后在自己的终端增量编目这一张：

```sh
python /opt/yuki-runtime/sticker_index.py --workspace /workspace update stickers/qq/files/开心.png --tag 开心 --tag 赞同
```

`update` 接受原图或已有的 `index/<asset-id>.json`，平台取实际目录名，无 QQ/TG 白名单。返回内部资产 ID 和真实文件路径；同平台同字节保同一份资料。显式 `--tag` 替换短 tags，可重复指定；不带 `--tag` 保留当前 tags、description 和 OCR，不清空人工资料。Main 可直接编辑同一个 JSON 的标签、描述和 OCR 后执行 `update`，不需要 Host、额外工具、Qwen 或重新付费。

维护跟随实际文件操作：单图收藏或单份 JSON 编辑后 `update`；批量复制、删除、移动、改名后执行 `rebuild --platform <platform>`。例如：

```sh
python /opt/yuki-runtime/sticker_index.py --workspace /workspace rebuild --platform qq
python /opt/yuki-runtime/sticker_index.py --workspace /workspace search --platform qq 开心
```

这些是显式文件操作的收尾，不新增 watcher、定时扫描或维护状态表。删除图片后已失效候选会在命中时剔除；新图片或新标签在实际 `update/rebuild` 后进入检索。终端 `search` 和后台选图一样，缺失、损坏或尚未完整构建的 cache 按实际本地重建结果继续；热查询不重建、不扫描全库。批量跨平台复制时同时复制相应 JSON 可主动携带原标签；仅复制原图只建立最小资料。表情选择与发图不等待 Main 做额外维护。

首次完整 JSON 原子创建的赢家可沿现有明确 Vision 配置后台补一次描述；没有配置、解码失败、请求失败或关闭不阻止收藏，不隐式重试 HTTP。合法大尺寸原件保存和最小编目不依赖像素解码成功。普通文件保存没有图库后置 hook，Main 自行用终端编目。共享复制前只读核原事件/附件/segments，会话重置不撤销原件的收藏资格；接纳后普通完成或遗忘不回滚共享原件，不另建来源表或接纳状态。原件访问已按用户要求统一共享，各 Conversation 的文本历史与人物归属仍按原合同。

`send_message.emoji` 表示可选配图意图。图文的配图输入与首条正文发送 claim 在同一短事务保存原来源，后台沿原文本回执等待正文派发结束，再选图、publish 和发送图片；Main 不等待选图或发图，正文后也不另开接纳写事务。正文的明确失败或未知不会删除配图意图；分段后续预检退出沿原父回执记录实际停止，不把未派发子项伪装成已发送。纯配图返回接纳事实，不能冒充已投递。图片沿原 Social 服务使用独立稳定派生键与不可变 artifact，发送结果只认原 Social 回执，不追加进已开始的正文分段计划。普通 Main/Work 完成不撤销已接纳配图，真实取消、换代和撤权在原 claim 边界处理；已 claim 中断保留未知，不自动重发。公共图片记录仅有表情标记和短 tags，长描述不进入公共历史或自动摘要。恢复与 Work 效果查询复用原 Social 实际分步回执，父 PREPARED 不否认已成功子项；已派发或未知不因图库或 Manager 失效而重发。纯图提及/引用交给原图片计划，有正文时仅正文提醒一次。

普通聊天的图文与纯配图都不要求先登记 Work。工具结果采集中的空 Work ID 只表示没有任务，不参与任务取消检查或 Work 效果绑定；配图仍保存原 Social 意图与发送回执。存在真实 Work 时保留原任务身份、效果归属及实际取消事实。

明确指定的 artifact 图片和普通文件保持原同步发送语义。QQ 小表情沿 OneBot 原生 `image.data.sub_type=1`，普通图片不附此字段。Telegram 原生贴纸使用 `sendSticker`，GIF 配图使用动画发送，明确的 GIF 文件仍使用 `sendDocument`；无原生跨端转换的格式按实际传输结果报告，不伪造静态替代图。

图片、视频、原生贴纸及普通文件入站均只提供附件资料，后台保原件缓存；Main 按需沿既有 inspect 或沙箱路径读取，也可直接保存原件。没有自动像素准备、视频采帧或文档展开，不隐式调用独立视觉模型。WebM 与 MP4 沿共用视频采帧器产生真实帧。TGS 原件继续保存和原生发送；Main 只读取平台实际提供并随原附件保存的缩略图，明确标注静态预览，不把动画 JSON 冒充图片。缺少预览时保留原生 emoji 与格式资料，不能宣称已看过像素；后续读取仍沿原内部事件、附件索引、来源版本和实际文件。

旧 Telegram 事件即使保存为 IMAGE、文件名为 sticker 且没有 MIME，当前、引用、缓存、历史与 Workspace 仍按原件字节识别 TGS/WebM。读取修正不改写原事件 segments、冻结正文或已付请求；TGS 无真实预览时只提供元信息，WebM 使用共用实际采帧器。

## 定时发送

普通图片仅在传输派发时构造 OneBot 媒体段；公开聊天账本与日志不保存 Base64 或本地路径。
私有恢复媒体按原 Work 的配额、引用和备份合同保存，不据此取得再次发送资格。
Genie 出站 record 构造已退出；历史 record、真实转写和已接受回执继续按原来源读取。

PR #139 已将新建静态提醒编译为 `social.send_message`，SELF 和用户使用 canonical Social 路由与持久回执。`delivery=none` 不发消息。自动化 DSL 不再注册旧 OneBot 私聊/群聊、语音、表情直接发送和通用 OneBot 调用；模型需要表情时使用主 Agent 的 `send_message` 结构化参数；语音合成与出站录音已退役，入站 ASR 和历史 record 继续读取。历史终态脚本和回执只读保留；迁移 `0072` 在存在启用或暂停的旧发送脚本时拒绝升级，需先人工核验，不猜测或重发外部效果。自动化主 Agent 的 OneBot 辅助网关禁止直接 `send_*` 和 `upload_*`。插件通知仍走插件自身的持久 outbox 和授权传输，属于独立的通知合同。
