# 聊天媒体缓存与工作区提升任务书（历史设计）

> 状态：2026-09-24 的设计基线；现行实现合同见[聊天媒体与发送合同](chat-media-workspace.md)，提交、合并和上线状态另行核验。对应 [Issue #142](https://github.com/YuanYeYouTao/Yuki/issues/142)。Memory、表情库与工具结果 Artifact 均不在本任务范围内。

## 1. 目标与确定的产品规则

1. 用户先发图片、视频或文件，后续不引用地自然提问时，Yuki 能从当前 canonical Conversation 的真实事件选择候选，按需读取原件。语义、发送者、话题和消息顺序共同用于指代；不固定选“最新附件”。
2. 每条新媒体消息在聊天文本的原位置显示稳定的类型和附件序号。历史里可看到“有这份媒体”，实际内容仍在读取后才可声称看过。不每轮追加动态媒体清单。
3. 聊天媒体原件放在**按 canonical 会话分目录的临时缓存**。每个成功保存的文件从 `cached_at` 起保留 **24 小时**；读取不续期，到期自动删除。预算不足时不提前驱逐未过期文件，改为明确记录“未缓存”。
4. 从临时缓存读取只能用于它所属的当前会话。要跨会话处理，Yuki 必须先显式把附件提升到持久工作区；工作区继续全局共享，提升后的文件不按原会话再加一层读取限制。
5. 此功能上线切换时，对**全部 canonical 群聊和私聊 Conversation**执行一次真正的 `/ai new` generation 重置语义。重置不触发模型、不向 QQ 群发送文本、不回放旧历史。事件账本与长期 Memory 不删除；插件独立 AI 会话不属于 canonical 主聊天，不冒充其重置对象。

## 2. 当前实现与已确认缺口

- `normalizer._extract_segments` 提取媒体附件；`AttachmentInputService.prepare` 和 `VisionService.select_references` 只读本轮或显式回复附件。后续普通文字没有引用时不会自动获得原件。
- `event_prompt.event_content` 可附加旧 `visual_summary`，它是历史观察，不代表本次重新读取。`get_recent_chat_history` 的网关路径只返文字，且回填会去掉媒体定位信息。账本路径的 `_event_json` 已提供内部事件 ID 和附件序号。
- 现有 `workspace_import_attachment` 可按内部事件 ID 导入同一会话附件，但会直接写长期文件；`workspace_inspect` 只检查已发布图片快照。它们不是普通对话中的临时媒体读取接口。
- 当前模型协议只允许把原生图片放在 `user` 消息里。历史媒体工具第一版若走视觉 Provider，应向主 Agent 返回有来源的结构化观察，不得把文字观察说成主模型直接获得了像素。
- 此项是 PR #139 合并前的基线描述：当时静态提醒编译器仍生成 `onebot.send_*`。PR #139 已将新建静态提醒改为 `social.send_message`；本任务继续清理其余旧 DSL 出口。
- `EventLedgerRepository.list_scope_around` 会检查 generation floor；`list_scope_recent` 当前没有同等过滤。全量 `/ai new` 后，所有供模型挑媒体的近期/搜索入口都必须核验新 generation，不能让旧媒体从另一入口回流。

## 3. 事件、上下文与具体文件如何索引

### 3.1 稳定引用

入站事件先落 `chat_events`。在同一短事务登记每个附件的元数据，顺着原始 segment 顺序产生 `attachment_index`，同时记录真实 `segment_index`。唯一业务引用是：

`(source_event_id, attachment_index)`

Host 索引 `conversation_media_items` 至少保存：内部事件 ID、canonical Conversation UUID、generation、附件序号、segment 序号、媒体类型、净化后的展示名、声明大小、入账时间、缓存状态、内容 hash、`cached_at` 和 `expires_at`。`(source_event_id, attachment_index)` 唯一，按会话和事件顺序索引。`chat_events` 是来源事实，索引故障不能靠平台消息 ID 猜另一条事件。

新消息在媒体原来的位置显示稳定标记，例如 `“看看这个” [图片附件0] [文件附件1：报告.pdf]`。主聊天事件封装已有 `#内部事件ID`，两者合起来就是模型可调用的引用。标记只取入账时稳定的类型与有界展示名；不放缓存状态、临时路径、URL、Base64、网关文件 ID、分析结果或随访问变化的字段。

会话全量 `/ai new` 后，普通 Prompt 与媒体候选从新 generation 边界开始。旧 `chat_events` 留作审计，不批量改写正文、重新识别旧图片或重跑 Rollup。网关后补的新事件须先在传输边界落账并拿到内部 ID；当时可安全下载的原件可以缓存，随后剥离定位信息。旧消息即使在一般历史审计中可见，也不能绕过新 generation 的媒体读取边界。

### 3.2 临时目录格式

Host 配置独立持久磁盘根目录，不使用全局 `/workspace` 项目目录或仅 128 MiB 的 `/tmp` 保存大视频。建议格式：

`<MEDIA_CACHE_ROOT>/v1/<canonical-conversation-uuid>/<source_event_id>/<attachment_index>-<sha256>.<verified-ext>`

下载中的文件在同目录使用随机 `.part` 名称，校验大小、真实类型与 hash 后原子发布。磁盘名只用内部 ID 和经验证的后缀，不使用 QQ 原始文件名。目录对 Bot 服务账户私有；路径不交给模型、插件或用户输入。不同会话即使上传相同字节，也各有自己的临时对象；不能因为内容 hash 相同而把另一会话的临时路径直接返回。安全的分析缓存可在已验证各自来源后复用相同内容的观察。

每个文件的 `expires_at = cached_at + 24h`，固定不滑动。入站后在大小、并发及全局/单会话磁盘接纳预算内异步保存字节，**不因此调用视觉模型**。预算不够时不抢删还在 24 小时内的文件；索引保持“未缓存”，后续首次查看可以尝试从可信网关取回。缓存失败不阻止消息入账。

清理器在读取时、周期任务和启动恢复时校验到期：到期即拒绝新读取，并删除文件和空的事件/会话目录。已有有界读取先完成或安全终止，然后尽快释放打开的句柄；不得靠访问续命或让失败租约无限拖延。`systemd-tmpfiles` 可作为孤儿 `.part`/失联文件的兜底，不能代替应用索引与 24 小时判定。索引保留事件与“原件已过期”的事实，不因删文件伪装成从未有附件。

### 3.3 读取权限的必要程度

**临时缓存只做会话级逻辑隔离。** 不为每个群运行独立容器，也不引入每个成员的额外 ACL；同一群会话中的 Yuki 工作可读取该群临时媒体，私聊按 canonical 私聊 Conversation 隔离。

`inspect_conversation_attachment(event_id, attachment_index, question?)` 必须在执行处查询真实事件，复核当前 Work/ToolRuntime 的 canonical Conversation、generation floor、入站消息与 keeper 状态，然后定位该会话目录中的对应文件。内部事件 ID 是递增整数，而且可能出现在历史或回执中；“别人不知道文件 ID”不构成授权。不能接受模型传来的任意缓存路径、URL、平台消息 ID 或另一个会话 ID。目录分开便于清理和审计，真正的权限判定在服务层。

`save_conversation_attachment_to_workspace(event_id, attachment_index, destination)` 复用上述来源核验，在 24 小时内把原件安全复制/校验到全局 `/workspace`，返回路径与实际版本。提升后旧临时副本仍按原到期时间删除；长期文件按现有全局工作区规则管理，所有会话可通过工作区能力访问，无原群的附加 ACL。**读取临时媒体不要求工作环境可用；提升要求工作环境可用。** 提升失败不能谎称文件已经共享。

独立插件 AI 会话没有主聊天事件读取权限；插件唤醒的主 Agent、SELF 与自动化使用它们真实的当前 Conversation，不继承最近真人或超管身份。插件或自动化若提供来源 ID，也走同一执行校验。

## 4. 统一图片、视频和文件读取

重写当前/回复/历史媒体的选择与解析接口，避免 `AttachmentInputService`、`VisionService`、`workspace_import_attachment` 各自维护一套事件取回、下载和鉴权。选择来源与解码分开：

| 来源 | 身份 | 内容处理 |
| --- | --- | --- |
| 本轮与显式回复 | 可信当前/回复事件 | 保持现有即时读取体验，使用共享解码和预算 |
| 历史无引用 | 当前 generation 内的 `event_id + attachment_index` | 主 Agent 语义选候选，再按需读取 |
| 已提升工作区文件 | 工作区路径和版本 | 全局可读，不重新套原会话 ACL |

图片验证原件后走现有预处理/视觉模型；视频沿用可下载地址、大小、时长、帧数和抽帧预算，未处理音轨要明说；文件用实际类型与现有文档读取器，报告不支持、加密或截断。重复观察按内容 hash、问题/模式、Provider 与提示版本复用分析缓存；原件缓存与模型观察缓存区分。网关容器的本地路径绝不当作 Bot 文件打开。

所有工具回执带内部事件 ID、附件序号、实际 hash、读取方式、分析版本和截断/抽帧范围。原件失效或缓存到期时说明原因；历史 `visual_summary` 和文件名只可作明确标注的线索，不能冒充本次读取。多候选可检查多个合理对象；仍不清楚才请用户指明，不暗中总选最新一个。

新主工具合同部署时冻结；`request_tools` 只查用法。旧 `workspace_import_attachment` 与 `workspace_inspect` 若保留名称，内部必须改接单一服务；未完成旧 Work 的兼容调用只能转发到新实现，不能留下第二条媒体读取通道。完成兼容迁移后，旧入口从新声明与执行路由清除，并用可达性测试验收。

## 5. 全会话 `/ai new` 上线切换

这是**一次部署切换操作**，不在写任务书时运行。范围为数据库内全部 canonical 群聊与私聊 Conversation；多个 Presence/旧 alias 指向同一 Conversation 时只重置一次。插件独立会话、Memory、表情库资产和未来自动化定义不清空。

1. 在测试副本统计目标会话、当前 generation、Rollup/checkpoint、运行中 Work、待处理插件唤醒和自动化执行；演练完整重置与回退，估算重置后首次自然聊天的 Prompt 成本。
2. 部署窗口暂停新入站与新 Work 接纳，等待/中断在途模型调用，先按原执行 ID 和持久发送回执处理已产生的效果；做数据库与文件一致性备份。不能将已送达结果重发。
3. 实现可重入的一次性管理切换：每个 canonical Conversation 调用与 `/ai new` **相同的领域 generation/reset 逻辑**，记录可信的管理来源、切换批次及审计事件；不伪造某个用户发送的 QQ 命令，不向平台群发 `/ai new`。现有入口依赖 `InboundMessage`，实现时须抽出共用的 reset 服务，并在共同架构合同中说明显式管理员批量重置是用户授权的 `/ai new` 等价操作。每会话短事务、持久进度和幂等键；中断后只续未完成项。
4. 重置全部完成且核验 generation、起点、Rollup/投影、Work 围栏后才开放新版本入站。失败的部分重置不能被宣称为全量完成；暂停接纳、按进度恢复或回退代码，保留所有新消息和回执。
5. 重置本身**不调用模型**、不重跑旧聊天、不补分析旧媒体；下一次真实消息按新的空 generation 开始。新工具合同导致自然首次请求的缓存前缀变化需测量，但不会在上线时把所有旧历史重写后逐会话跑模型。

历史审计可以保留重置前事件；供主 Agent 选择媒体的 `get_recent_chat_history`、搜索、附近查询及新媒体目录必须一致地应用 generation floor。现行 `list_scope_recent` 未过滤 floor，属于实施时要补的缺口。旧 `event_id` 即使能从审计接口得知，也不能拿来读取当前会话的临时媒体。

## 6. 插件与自动化残留整改

媒体能力覆盖普通聊天、主动 SELF、自动化主 Agent、插件唤醒主 Agent和持久续跑，执行处均用真实来源授权。逐入口比较固定声明、请求输入、ToolRuntime、错误回执与恢复；不能因普通聊天通过就认为全部入口通过。

自动化旧发送链目前可达，本任务必须收口：

- `AutomationTaskCompiler` 停止生成 `onebot.send_private_message` / `onebot.send_group_message`；新的 DSL 明文投递也使用与 `send_message` 共用的 SocialService 路由、幂等与持久回执。
- 检查 registry、validator、executor、handlers、model_delivery、权限目录、SDK/Facade、存量脚本与测试。新建脚本拒绝旧名称；已有脚本迁移或用明确的旧 Work 适配完成未执行步骤，保留 step/work ID 与已确认送达回执，绝不二次发送。
- 兼容期结束后旧名称不可从新创建、模型声明、插件 Facade、自动化编译或执行分派到达。旧历史 action 可只读解释。验收实际可达性和失败/未知效果恢复，不把 `rg` 命中数当唯一证明。
- 插件主调用检查新媒体工具与 `send_message` 的同源权限；插件直接通知检查自身媒体 handle/过期/发送回执；独立插件会话不得借工作区路径或事件 ID 读取主聊天临时媒体。
- SELF 自动化必须使用 SELF 主体与原群场景，不通过旧发送别名继承创建者或超管权限。

旧 DSL 收口若需要独立迁移或 PR，也是本任务上线前的必过依赖，不留一个“以后再清理”的运行入口。

## 7. 验收

- 无引用连续对话中的图片、视频和受支持文件；间隔多条消息、多人交错、多附件歧义；本轮和显式回复回归。
- 索引 `event_id + attachment_index` 与真实 segment 一一对应；跨群/私聊、另一 generation、非 keeper、被删事件和插件独立会话拒绝；只知道递增事件 ID 不得跨会话读取临时缓存。
- 缓存目录按 canonical Conversation 分开；同内容跨会话不复用临时路径；`cached_at + 24h` 到期拒读并自动删文件/空目录，访问不续期，未过期不因容量抢删。缓存未接纳、原件失效、坏文件、视频超限、文档截断分别如实回执。
- 提升前不能跨会话处理；提升后目标工作文件的内容 hash/版本正确，全局工作区可从另一会话读取，旧临时副本仍按时清理。持久环境不可用时普通临时读取仍可用。
- 全量 reset 在测试副本覆盖私聊、群聊、多 alias、长/短历史、Rollup、活动 Work、插件唤醒和自动化；每 Conversation 恰好一次，失败可续；重置时零模型调用、零 QQ `/ai new` 发送、零旧历史批量重识别。首次自然请求的完整 Provider `messages/input`、`tools`、`native_tools` 与 continuation 对照，报告真实缓存变化。
- 自动化明文、模型生成、SELF、插件主调用与直接通知分别覆盖成功、失败、未知回执和重启恢复；已送达消息不重发，旧发送名称不可新建且最终不可达。
- 上线前按既有 Bot-only 规程备份与回退演练；上线报告区分代码、测试、PR、合并、镜像替换及真实 QQ 效果。测试不主动向真实 QQ 群发消息。

## 8. 实施顺序

1. 媒体索引、会话分目录的 24 小时缓存与稳定消息标识；补齐生成边界的历史媒体查询。
2. 统一读者与“按需查看／显式提升”工具，收掉重复旧媒体路径，覆盖所有主 Agent 入口。
3. 自动化旧发送 DSL 与插件入口审计、迁移和回执测试。
4. 在测试副本完成全量 `/ai new` 等价切换与成本测量；部署窗口备份、排空/围栏、一次性重置并启用新代码。
5. 线上只读状态和安全探针验收，再观察真实无引用聊天效果与 24 小时清理。
