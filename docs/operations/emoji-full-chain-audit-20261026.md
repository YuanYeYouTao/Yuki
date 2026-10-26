# 2026-10-26 表情包全链路审查

基线：`d6b4ec167f5c982eed7f62bb216c5e987b529af0`。审查开始及收尾均核实远端 main 为此提交，生产 Bot 的 OCI revision 也一致。使用独立审查 worktree，未改原用户工作区。

这次完成了入站、原件与预览、收藏与描述、文件编目、检索、发布、目标路由、平台发送、回执恢复和清理的三轮核查。只新增本报告；业务源码、运行配置和生产数据未修改，没有发测试消息、购买模型请求、重放旧意图或重建线上图库。

结论：正常路径有真实送达记录和较完整回归，但存在一个高优先级的目标入口错误，以及若干入口不一致、失败分类和生命周期缺口。现有测试通过与这些缺口同时成立。

## 1. 最值得先处理的问题

| ID | 优先级 | 已确认的触发与结果 | 范围 |
| --- | --- | --- | --- |
| F01 | P1 | 在 TG 话题 101 中明确指定向话题 202 发纯表情，最终 `sendPhoto` 发到 101，回执仍为 succeeded；同目标的文字加配图会在前台报 route_changed | 显式目标 Conversation/话题在后台配图中丢失；本轮已证同 TG 群不同 topic |
| F02 | P2 | 已批准、已授予目标的 App 插件图片通知接纳后，outbox 变为 uncertain/platform_receipt_missing，出站图片事件为 0 | App 本地通知；普通 Main/Social App 发图有自己的正常入账路径 |
| F03 | P2 | 飞书只在图片上传阶段超时、尚未调用 messages；或 QQ 文件本地准备失败、OneBot 调用为 0，Social 都记消息发送未知 | 发送阶段分类不准确；真实消息 API 调用后的未知仍需保留 |
| F04 | P2 | TG 普通图片下载中仅编辑 caption，同原件的后台预取不能发表；显式读取需第二次下载 | 共享媒体缓存；没有外推为原生 TGS/QQ 表情可编辑 |
| F05 | P2，条件性 | 图库 helper 接受软/硬链接候选，首次发布的 FD 桥却拒绝该路径 | 终端用链接整理图库、且尚无同字节 artifact 时 |
| F06 | P2，取消边界 | QQ 文件交接协程取消返回后，未结束的写线程仍创建文件，留下临时文件 | 普通 file 路径；当前 emoji image 的 Base64 路径不经过它 |
| F07 | P2，异常退出 | snapshot 发布进程崩溃后，无 manifest 行的 .pending 不在当前 cleanup 候选内，持续占盘 | 进程 kill/宿主崩溃；正常取消收拢与此不同 |
| F08 | P3 | reader 基于旧损坏判断等待锁，另一个 writer 已修好缓存，旧 reader 随后仍 unlink 新缓存 | 并发冷恢复，导致额外重建和 I/O；原图/JSON 未丢 |

八项是不同的功能、事实或生命周期问题；同一根因的两个平台反例没有重复计数。资源开销和文档问题另列。

## 2. F01：后台配图没有保留完整目标入口

位置：`src/yuki/emoji/delivery.py:84–168,317–345`，`src/yuki/social/service.py:584–625,997–1004`，`delivery.py:226–281`。

原 `send` 接受明确的 `target.conversation_id`。捕获配图来源时保留原绑定、Presence 和平台，却没有保存完整目标 Conversation/thread。后台 `_send_input` 重建 target 时仅保 kind/target_id，person 另保 binding_id。随后 `send_route` 认为目标与原 source 属于同一个 Space，沿 source Conversation 重新选入口。

claim 复核了目标、绑定、Presence 和平台，但没有原目标 Conversation/thread 可比对。于是绑定和机器人都没变，图片却可以进入另一个话题，仍被正确记录为该次实际调用成功。

隔离证明使用真实 SQLite、当前身份创建/合并、两条原生 TG thread 的接纳事件、路由 CAS、当前 ToolActor 和内部 trigger event；仅平台 HTTP 换为本地接收参数的假客户端。请求 202，实际 `sendPhoto.message_thread_id=101`，最终 succeeded。没有人工改写目标路由记录。它不是主 Agent/模型质量测试，也不是生产错投证据。

同一真实夹具再发文字加 emoji，`service.py:1003–1004` 将解析出的 conversation route 改成目标 kind，却保留 conversation 的 route_generation=0；当前 Space 路由代次不同，前台以 route_changed 拒绝。该问题也影响共享 Social 的显式入口发送，不仅是后台配图。

最小方向：删除将真实 conversation route 改回默认 kind 的转换；后台复用已有 `_route_payload` / `_stored_route` 思路，保原实际目标入口，claim 继续核当前授权和连接。显式目标路由与原回复来源分别保留，不通过改 source 修正目标。source Conversation、原 actor/event、turn/call 和回执 ID 均保原值。旧记录没有的目标事实不能用当前默认入口反推。

证据：本机 `test_runtime_probes.py`、`runtime-routing-probe.json`、`runtime-probes.xml`。

## 3. F02/F03：本地投递与实际派发阶段没有接齐

### F02：App 插件通知要求了不存在的外部消息号

位置：`src/yuki/gateway/providers/app/social.py:18–21`，`src/yuki/plugin_host/notification_delivery.py:229–255,409–522`。

App/ACP 的消息由 canonical ledger 本地入账，再供 Client 观察；adapter 正常返回 `TransportReceipt(None)`。普通 Social 能在同一 writer 中完成这件事。插件 outbox 却先要求外部 reference，抛 platform_receipt_missing 后不进入 `_record_outbound`，所以客户端没有可见事件。

最终反例包含实际 App 配对与 Client 消息建立入口、插件安装批准/启用/运行、`media.artifact.create` 和 `notification.publish` 权限、受信管理准备的目标 grant，以及真实 SDK `media.create_artifact` / `notifications.publish`。PNG 通知已接纳并 claim，最终 uncertain，新增出站图片事件为 0。没有模拟管理员 GUI 的 grant 表单；没有把早期 fake-router 探针作为最终依据。

最小方向：复用现有本地账本发表与 artifact 投影，保持原 notification/outbox 身份并原子结算。删除本地投递必须有外部消息号的假设；不能伪造一个 native ID，也不能只移除第一层判断后把空号交给仍要求非空的后续函数。

### F03：进入 adapter 不代表已经调用消息 API

位置：`src/yuki/social/service.py:1423–1442,1522–1533`，`gateway/providers/feishu_social.py:120–164`、`feishu.py:103–104,154–164`，`adapters/onebot/social.py:46–79`。

两条真实 Social 结算反例：飞书 `/images` 超时，`/messages`/`reply` 调用为 0；QQ 普通文件没有 transfer root，在任何 OneBot 调用前失败。两者均被记为整条消息 uncertain。飞书上传资源本身可能未知，但该次消息确定尚未派发。

最小方向：沿 adapter 已有阶段保留事实，已知未调用消息 API 的准备/上传失败使用现有 FAILED/错误结果；真正消息 API 断流、超时或缺确认继续保 UNCERTAIN。不新增重试器，不借此重发原未知效果。

证据：`app-notification-actual-repro.json`、`feishu-upload-social-repro.json`、`qq-file-preflight-social-repro.json`、`egress-counterexamples-final.xml`。

## 4. F04：caption 编辑撞上下载时丢掉自动预取

位置：`src/yuki/conversation/media_service.py:539,691–740`，`gateway/providers/telegram_ingress.py:442–451`，`persistence/scoped_event_uow.py:430–458`。

cache owner 以整份 event.segments_json 为下载/发表依赖。TG 普通 photo 只改 caption、段位置变化时，实际图片 file_id 和字节不变，旧下载却被当旧源拒绝。edit 入口更新原事件后返回，没有使这次同原件预取继续。后台结束仍 uncached；显式读取再下载一次才恢复。

最终探针使用原生 photo+caption 形状，原消息和编辑均没有 photo+text 的不实组合。相同 file_id，下载次数 1→2，最终能读回原件。已缓存后的 caption 编辑、真实换图、预览替换与 TTL 用例正常；没有证据说原件永久丢失或所有贴纸都能被 TG 编辑。

最小方向：删除原件 owner 对整条消息正文/Host 编辑资料的依赖，复用现有 event/index 与实际附件事实核源。真实换图、删除和遗忘仍使旧字节无效；不增加编辑专用队列或经验重试次数。

## 5. F05–F08：可发布文件与物理生命周期

- **F05 / 候选可发布性。** `emoji/sticker_index.py:100–108,129–141,279–305,487–492` 的 Path.open/原路径保留接受内部链接；`workspace/files.py:78–84` 的实际发布 FD 打开拒绝 symlink/nlink>1。真实 helper 的 catalog/rebuild/search 都可返回 alias，首次发布随后失败；已有同 hash 快照可复用。应统一候选与既有可读文件语义，让正常普通文件候选继续有机会，保 Host FD 边界。这不是宿主越界漏洞。
- **F06 / 取消后仍在写。** `adapters/onebot/social.py:45–63` 取消 to_thread 不结束线程，finally 先 unlink；探针暂停在线程实际打开前，取消返回目录为空，放行线程后变成 1 个残留。复用项目已有的实际 I/O owner 收拢方式，等待真实线程退出后清理。只影响 file 交接，不能解释当前 Base64 emoji 直发。
- **F07 / 崩溃 pending。** `workspace/store.py:214–225,275–276` 在正常 finally 清 pending；cleanup `138–158` 只枚举 blob。本地发布子进程在写完首块后被 kill，重新打开 Store 并 cleanup，0 行 artifact 仍遗留 65,536B pending。应在既有双 owner 停写维护窗口清确证无主文件，或收拢现有发布临时文件生命周期；不能在热 GC 只凭 mtime 删除可能在复制的文件。本轮未扫生产 pending。
- **F08 / 旧损坏观察删新 cache。** `sticker_index.py:207–224,449–468` 在发现损坏与取得删除锁之间，另一 writer 可完成 rebuild；旧 reader 随后无条件 unlink。实际边界调度复现了 ready=True 的新 cache 被删。应在既有 writer 锁内依据当时事实决定删除，不加 cache revision/新状态表。原图和 JSON 不受影响。

证据：`catalog_probes.py` / `catalog-probes.json`、`egress_edge_probe.py` / `egress-edge-probe.json`。

## 6. 资源与重复开销

| ID | 当前成本 | 实测或来源 | 删除式方向 |
| --- | --- | --- | --- |
| R01 | 首次收藏无 Vision 也把整个原件驻留在 Bot，随后 copy/catalog 仍核原 SHA | `library.py:149–173`；8MiB 原件进入 copy 前仍驻留约 8.43MB，峰约 8.59MB | 保存路径流式处理；只有真实首次描述赢家准备必要模型输入，保已有 copy 的预期 SHA |
| R02 | TGS 识别为取几个字段完整解压并构造 JSON，暖 helper 的 inspect_pixels=False 也经过它 | `media.py:62–76,91`；8233B gzip→约8MiB JSON，Python 峰约16.84MB；B 独立探针同样复现 | 从必经收藏/识别中去掉无用途的整份对象构造，按实际读取目的消费格式资料；不增加原件经验上限 |
| R03 | 每次 wake 先对所有 pending 逐项查正文回执，外层才排除在发送项；暖 search 先 ready 再另开连接排名 | `delivery.py:286–305`、`emoji_intents.py:301–369`、`sticker_index.py:212,479` | 将已有活动集合的排除前移，合并本次只读连接；沿原队列/reader，无新队列或固定并发阈值 |

R01/R02 是已测量资源开销，不是已定位线上 OOM。R03 对积压查询与小额连接开销有直接源码依据，没有冒称单张发送能快几十秒。QQ Base64/JSON 的线性副本仍存在；先前本机 1MiB 合成编码中位约 7.18ms，额外 Python 峰约4.67MiB，不能用它解释全部平台尾延迟。

正常暖选图已经使用倒排索引，没有全图库扫描或另一个选图模型。Pillow 懒加载、描述/OCR 不回传、重复收藏复用、已有不可变快照复用已上线，不重复算成未来优化。

## 7. 文档与待核项

- **D01 / P3。** `docs/architecture/chat-media-workspace.md:158` 仍说自动化 `delivery=none` 不发消息，当前 TaskSpec 已没有这个执行模式。应删除旧句，不给模型重新增加参数。旧回执 hash 兼容读取不等于现行功能。
- **U01 / 未定性。** 插件通知从 resolve 后 local_path.read_bytes，SDK 直接 send_image 则使用 verified reader 核 size/SHA。只有 Host 私有媒体被改/损坏时才会显现差异；本轮没有证明合法插件能改该私有文件，也未注入这种损坏。暂保完整性疑点，不记成已证授权绕过或线上故障。若处理，复用已有 verified reader。

## 8. 全链路覆盖与三轮方法

| 链段 | 主要源码与核查内容 | 证据归属 |
| --- | --- | --- |
| 平台接收与标记 | OneBot normalizer；Telegram/Feishu/App ingress；原生贴纸、普通图片、引用、编辑与实际内部来源 | ingress-review |
| 共享原件与预览 | media_index/media_service、PreparedAttachment、media_resolver；单次原件、真实 thumbnail、固定 TTL、取消、替换/遗忘 | ingress-review |
| 收藏与描述 | EmojiLibrary、native_media、Qwen provider；首次赢家、重复素材、原格式、失败保原件、无隐式付费重试 | ingress-review、catalog-files-review |
| 文件与检索 | StickerIndex、media/models；JSON 真源、update/rebuild、标签编辑、排序、冷库、无效候选和并发 | catalog-files-review |
| 文件桥与发布 | FileWorkspace、WorkspaceStore、WorkspaceService、Manager/client；原 request、同 FD/原 SHA、取消与未知、快照引用/GC | catalog-files-review |
| 接纳与派发 | EmojiDelivery、EmojiIntentRepository、SocialService/repository/db_models/source_keys；纯图、图文、分步、claim、topic、身份与撤权 | root runtime 回归及 F01 探针 |
| 平台出口 | QQ/Gateway doctor 合同、TG sticker/animation、Feishu upload/message、App 本地入账；mentions/reply/thread/错误分类 | egress-review |
| SDK/通知/自动化 | 插件原 grant/owned media/outbox、直接发送、TaskSpec/worker、固定 send schema/提示 | egress-review |
| Work/恢复与关闭 | 原执行预算/效果绑定、父子回执向量、已 claim 未知、已成功不重发、Main/Work 正常结束不撤销配图、真实取消与启动依赖 | root runtime 回归及三路逆向复核 |
| 清理与部署接缝 | 临时缓存与永久图库/不可变快照分离、Bot/Manager 实际 owner、helper 同源、目录挂载与清理引用 | ingress/catalog、现行 runbook 与生产 revision 元数据 |

第一轮顺着真实入口走到回执和清理，并核当前源码/文档；第二轮运行定向回归，对编辑、故障、取消、并发、重启和原件边界做隔离反例；第三轮互换负责人反查。第三轮把 caption 探针改为真实 native caption 形状、把 topic 探针绑定真实 actor/event、把 App 反例加强到实际已批准 SDK。未把重复跑同一组用例当作三轮审查。

## 9. 实际验证与生产边界

- 离线 Linux，生产依赖来自已有本地镜像，测试工具取自现有镜像；没有从外网装包。精确 d6 源只读挂载，再复制到容器临时目录；runner 断言 import 来源，并复用同源 native 扩展。
- 现有目标共执行 **524 个用例记录，按完整 testcase ID 去重为 506 项，全部通过**。重复18项没有累计成新增覆盖。外置反例断言的是现存缺口，另列，不计入这506项。
- 当前尚未跑的两类：两个大磁盘容量验收（3×201MiB、1001次snapshot）；缺 trustme/cryptography/cffi 的 TG TLS 环境模块。没有写成已通过；它们不是本轮业务断言失败。
- 独立反例保存原输出和脚本；没有在生产压测、kill 进程、写磁盘、模拟断网、发送消息或试模型。
- 2026-10-26 04:52 UTC 的生产只读元数据：Bot revision=d6b4ec16；未关闭 emoji intent 查询为空，最近8笔已关闭意图均找到 succeeded、内部 event 和平台 reference。两种状态查询均实际使用 ix_social_emoji_pending，回执走原唯一键；没有全表扫描、读取正文或重算素材 hash。
- 最新现存 QQ 表情回执发生在 SnowLuma 重登后、这次 d6 Bot 部署前。这能补足“QQ 重登后暂无新样本”的旧结论，不能当作 d6 全平台上线验收，也不能从8条记录推总体成功率。

详细原件保存在本机 `D:/Code/Reports/Yuki/emoji-full-audit-20261026/`：`validation-summary.json`、各路 review、JUnit、当前生产元数据和外置探针。文件不含生产聊天正文、平台凭据或原始图片。部分文件名标 early/final，是审计脚手架完善过程；正式结论以上述加强后的反例为准。

## 10. 修改顺序建议

先处理 F01 的真实目标入口、F02 的 App 账本落点和 F03 的派发事实；随后收拢 F04/F05/F06 的来源/文件 owner；再处理崩溃清理、并发冷恢复和资源删减。每批保持原内部身份、不可变快照、执行与发送回执，不引入通用重试器、新状态表或经验完成门槛。

本报告没有提交、推送、提 PR、合并或部署修复。它是审查结果，不是修复验收。
