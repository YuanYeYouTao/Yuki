# 消息身份入口与 Plugin API 9.0 实施复审

日期：2026-10-28。工作区：`Yuki-id-delivery-implementation-20261028`。本文记录入口、原生协议、Host/SDK、自动化读取与调用者配套；数据库迁移、Social 原子发表和通知媒体的整体验收由各自实施报告负责。

## 已落实的合同

- `InboundMessage` 删除 `message_id`，显式保存可空 `ingress_key` 和 `native_message_id`。QQ、Telegram、飞书/Lark 的 normalizer 同时给出真实接入键和原生引用；App/ACP 仅给接入键，原生字段为空。
- 接入 receipt 查询使用原 Account、原聊天/话题 namespace、完整 ingress key。`require_compatible_v2_live` 只用 `origin_key` 比较原信封，取消旧 128 字符截断。
- 首次原生回复查询只读同 Conversation、相同 native key 的候选，并连到候选的原接收 Account。QQ/飞书/Lark 的 namespace 包含接收 Account；Telegram 同 chat 的真实消息可跨接收账号引用。账号换任沿同一个 Account，不被 assignment 更换截断。已有内部 `reply_to_event_id` 不再查原生号。
- namespace 计算放在各 Provider 已有的 Social operations：`native_reference_namespace(account_id=..., scope=...)`。Ingress 和 Social 复用它，没有平台策略表、新 ID 框架或额外缓存。App 返回空 namespace。
- QQ file upload 的 `{}` 和仅有 `file_id` 的真实成功保留成功，不制造 `file:` 消息号；真实 `message_id` 仍可引用。线程交接写入的实际 await 和目录准备 OSError 均归为确定未发送；仍等待原物理线程完成后清理文件。
- 删除 `ToolRuntime.trigger_message_id` 和 `ToolActor.platform_message_id`。内部 Work/插件恢复使用原事件/原执行 ID，`ingress_key=None`，真实 native 引用沿原 EventRecord 保留。Host 的旧 actor JSON 仅在 reader 投影掉退休字段，不改已保存 JSON。管理审计仍可保存实际存在的原生出处，不再构造 `plugin-task`。
- 删除本地回执伪装原生字段的旧兼容 property；本地回执只有真实 event ID。SDK major 由 8.0 升为 9.0。`CurrentMessage` 删除旧字段，新增可空、无 128 字符上限的 `native_message_id`；Host 当前/历史/回复投影、normalized/triggered hook、示例、随仓 manifest 和文档同步。入账前 hook 的 event ID 仍可为空；没有提前创建内部事件。Client HTTP revision 保持 7。
- 自动化活 DTO 和新 authority 删除 `created_from_message_id`。唯一新行 writer 显式给历史 NOT NULL 冷列写空串；历史冷列、原 authority JSON、版本和 script hash 不改。活 reader 只投影掉这个明确退休的字段；原创建定义比较仍读取原始 JSON。实际接收 Person 的 `list_current`/`list_completed` 参数改为 `creator_person_id`。
- App request 查询沿原 receipt namespace 读取 ingress key；Work input 的原请求一致性比较使用 origin key，不与 native 字段混淆。

## 三轮复审与修正

### 第一轮：字段与协议边界

逐个查生产构造点及字段读者，完成六个 Inbound 构造点、SDK 投影与两类死运行时字段删除。定向执行 normalizer、飞书 Social、SDK major admission 和 plugin admission：**34 passed**。

专项回归直接重现并验证：QQ 换接收账号不串引用、Telegram 同 chat 跨账号仍引用原件、129 字符且同前 128 字符的两个 ingress key 分别接纳/同键重投复用原件、QQ file 三类成功 ack、实际线程 OSError 的零 API 调用、App adapter 不再把 noop 报成功、SDK 空/长 native 字段、旧 authority 只读投影。

### 第二轮：恢复、原件与实际 SDK 使用

沿 Processor → Main/子执行/恢复 → Host CurrentMessage 检查来源。修正了机械删除时误伤的 Host 审计出处：能取得真实 native 时保存，缺少时留空，不加回 ToolActor 死字段。原始创建定义读取不做活 DTO 投影，避免改变旧创建证明。

运行平台来源、自动化定义存储、插件 Memory facade 和 echo 示例：**46 passed**。自动化专项另外验证新行冷列为空、旧冷列原件保留、旧 JSON 和原 script hash 逐字节不变。

### 第三轮：完整调用面与可扩展协议

取消共享 Ingress 中的 Telegram 特判，改为 Provider 已有 operations 的纯 namespace codec。补齐同 Account 新 assignment、不同飞书 app 的相同 Open ID、已解析内部回复无需 native 再解析的回归。旧事件的云平台事实从其原 Account 读取，不根据 `om_` 前缀或今日默认路由推断。

全库 AST 检查发现并闭合 12 个文件里的旧位置参数 Inbound 构造、40 个 `dataclasses.replace(..., message_id=...)`，以及剩余旧属性读者；管理审计和通知回执的同名真实历史字段按用途保留。

- namespace 专项、canonical ingress、原生 mention command、Provider 合同：**72 passed**（随后新增的飞书应用 namespace 回归在下一组覆盖）。
- namespace/飞书应用专项、第三方 opaque 协议、Telegram 编辑、飞书真实 Processor/Main/Social、平台来源、插件 Work 恢复：**52 passed**。
- 本专题生产代码和专项测试 Ruff 检查通过；上述测试组有重复覆盖，不将数字累加为唯一用例总数。

ACP 取消回归也迁到真实本地发表边界：取消 join 用已提交 event/receipt/intent 之后的原 activation 屏障，快照刷新用原子 append 前屏障，不再挂起 App 的旧 noop send。原文件 **8 passed**，覆盖原 Work、子执行、已完成父任务、取消 HTTP waiter 和保留 WAL 快照；没有改产品取消流程。

Linux 全量复核另发现 cold identity writer 内候选 codec 使用 `_event_record` 解码原 segments 的边界回归。已将原生候选 SQL 收窄为 id、author kind、原始 chat scope、原接收 Account/platform 和原 Conversation.thread_id；codec 不再加载/解码历史消息正文或 segments，内部 event 查询也只取 id/kind。不加新缓存、prepared 状态或 writer。

最后边界修正后的专项、canonical ingress 与 identity preparation：**55 passed**；删除本地回执兼容 property 和 SDK 9 REPLY_SENT 字段对应的 App/normalizer/admission 回归：**33 passed**，新增 hook 回归同时覆盖远端与本地。最终关键源码 MyPy 定向检查 **4 files passed**，Ruff 通过。

## 独立 GitHub Monitor 配套

已读取独立仓库实际 main：`432b1da8`，原工作目录停在旧 API 7 分支，因此没有拿旧 checkout 当现行 main。新工作区为 `D:/Code/My_Github_Repo/Yuki-GitHub-Monitor-api9-20261028`，分支 `codex/plugin-api9-identity-20261028`。

Monitor 本身没有使用退休 CurrentMessage 字段或旧自动化 API。已将真实 manifest、产品版本 1.2.3、SDK 9 文档和契约测试同步，Host 最低范围仍为 `>=4.0.0`。以本次候选 SDK 9 在独立验证环境执行 **24 passed**，Ruff 通过；未复制 SDK 源码到插件仓库。

主仓 SDK 9 尚未产生可公开锁定的提交时，插件 `pyproject.toml`/`uv.lock` 仍指向原 SDK 8 归档。配套交付必须在主仓提交后换成真实 SHA、重新生成 lock 并以该固定依赖复验；不能把当前路径验证写成固定归档 CI 已通过，也不伪造 approved manifest hash 或改已有 grants/KV/outbox。

## 交付与验收边界

本文对应本地修改和定向验证；没有提交、推送、合并、发布或上线，没有向真实 QQ/TG/飞书发送测试消息。SDK major 升级后的现网插件重新发现/批准、现网账号换任/跨 app 引用、真实平台 upload ack 以及完整两仓端到端验收仍须在发布流程按真实状态记录。原本机 Vec1 扩展仅用在验证进程的路径补丁，没有复制 DLL、改产品 fallback 或改生产数据库。
