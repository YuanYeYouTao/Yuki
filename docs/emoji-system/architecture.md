# 表情文件库与后台配图

原件位于持久沙盒的 `/workspace/stickers/<platform>/files/`，每份 `index/<asset-id>.json` 保存短 tags、长 description、OCR 与真实文件资料。原图和 JSON 是唯一资料源，Main 和后台 Qwen 自由操作同一份文件。平台目录不按账号、人物或群拆分，增加平台不用修改 QQ/TG 白名单。Yuki 主业务库不保存图库资产、来源、标签或倒排表。

同目录 `search.sqlite3` 只是可删除重建的检索缓存。Main 用普通终端编辑文件，然后调用独立 helper 的 update 或 rebuild；它不需要 Bot 数据库、Host operator 或管理凭据。任意外部改标签后，新标签的搜索生效点是 update/rebuild 完成。命中候选再核当前 JSON 和真实文件，失效项移出缓存后继续查，正常选图不扫描全库。首次冷构建与真正无候选分开，构建不会阻塞正文发送。索引写协调只覆盖派生缓存，不给用户文件增加权限或 CAS。

QQ/TG 明确标记的贴纸复用本次 `PreparedAttachment` 的真实原件收藏，不重复下载，不等待临时缓存、像素解码或辅助模型。入账来源在共享复制前由 Host 媒体 owner 只读核验；已接纳副本独立于原私人附件和 Work。图库 JSON 不保存私聊、人物、平台 file_id 或凭据，相同 hash 不开放原私人消息。普通图片可用既有附件保存能力和终端收藏，普通保存成功不等待图库编目。

同平台同字节使用稳定内部资产 ID。Main 可改名、移动、删除或复制：主动跨平台复制图和 JSON 后保留复制的文字资料并重算目标事实；只复制图不暗取源平台标签。Qwen 仅在首次完整资料原子创建的实际赢家后台尝试描述一次，沿显式视觉配置，不做采用裁决、不隐式重试 HTTP。失败或没有配置仍保原图与最小资料。在途 Qwen 与 Main 遵循普通文件最后写入语义，缓存不能把旧标签写回文件。

`send_message.emoji` 提交配图含义，例如 `{"text":"这个确实好笑","emoji":{"goal":"捂嘴偷笑"}}`，也允许只有 `{"emoji":{"goal":"捂嘴偷笑"}}`。文字先沿原 Social 完成，再持久接纳配图输入并返回；后台按原实际目标平台选图，发布不可变快照，经同一 Social 出口发送。Main 不等选图、重建、Qwen 或图片网络，也不为配图多跑一轮模型。接纳只表示已保存输入，正文与图片分别认原回执。

有正文时提及和引用只在正文使用；纯图交给原图片发送计划。QQ 原生小表情保 `image.data.sub_type=1`；TG WEBP/TGS/WebM 走 `sendSticker`，必要的提及文本由原平台计划分步发送，不伪造同 payload。GIF 配图走动画，显式 GIF 文件仍走文件接口。显式看图、发布和指定 artifact 发送保持各自原合同。

每个后台意图独立前进；持久防重与恢复继续认原 Social CAS 和完整分步回执向量，不能只看 sequence 父行。已有派发或未知效果不因图库、Manager 或文件不可用而重发。普通 Main/Work 完成不撤销已接纳配图，真实取消、换代与撤权仍在原 claim 边界处理。公共历史只保存这次发送的短 tags，长描述与 OCR 不自动进入摘要；文件后来修改不改写旧事件、快照或已付请求。

旧偷图 collector/worker、分类采用、候选池、配额/替换/冷却、视觉拼图、专属插件 EmojiFacade、图库主库和隐式保存编目均已退役。管理图库使用原终端，独立 helper 在持久环境内运行，Host 只用现有 FD 文件桥，不直接打开用户可改的 cache 路径。

附件来源与寿命见[聊天媒体](../architecture/chat-media-workspace.md)，环境和 helper 部署见[持久环境](../operations/persistent-environment.zh-CN.md)，插件迁移见[API 5.0](../plugin-development/api-5.0-migration.md)。本页描述源码合同；验证、合并与上线见[实施记录](../operations/sticker-163-implementation-20261019.md)。
