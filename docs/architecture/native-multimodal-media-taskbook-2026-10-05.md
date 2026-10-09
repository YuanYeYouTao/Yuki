# Agent 原生多模态实施与现场样本记录（2026-10-05）

日期：2026-10-05（Asia/Taipei）。状态：用户已授权实施；主链统一正在本地实现和离线验证，尚未提交、合并或部署。

本任务书由主会话汇总三个 subagent 的历史媒体、Provider、运行时恢复审查。用户先要求查缺口和制定任务书，随后明确要求“修”，已进入实施阶段。现行边界以 [共同开发约束](development-contract.md)、[主 Agent](main-agent-runtime.md)、[模型供应商](model-providers.md)、[聊天媒体](chat-media-workspace.md) 为准。下方状态区分源码实现、本地验证、真实 API 验收和部署，不把任一阶段代替其他阶段。

## 1. 目标与基线

Agent 自己检索历史、选择真实事件附件或工作文件；Host 只负责来源授权、读取、类型核验和有界预处理；选定的图片或视频帧进入**原主 Agent、原模型连接、原请求链的原生多模态输入**。当前消息、明确引用、历史附件、工作区文件及获准工具图片共享这套输入语义，不再先调用 Qwen 生成描述，再让主模型阅读描述。

文字文档继续使用本地有界解析。原生图片输入不等于已经支持原生音频、视频文件或 PDF；视频先复用现有采帧，音频 ASR 不因本任务被删除。

源码基线为 `d23530b01b8a8139a539c8cef38e58016163f898`，与部署修订 `de9d0e3ae1d682ec6411df4d628db4ba061a5fd8` 的 Git tree 无差异。部署 Bot StartedAt 为 `2026-10-05T08:57:20.05019237Z`。以下线上数据是本次已读取样本，不是未来配置承诺：主 Profile 为 Gemini 3.8 Flash，声明 `image_input`；独立视觉配置为 Qwen `qwen3.7-plus`。

### 已核实与未完成

- [x] 三个 subagent 完成源码审查，覆盖历史媒体、工作区、协议输入、工具回执和恢复。
- [x] 当前/引用图片的原生主模型适配存在，Gemini 线上 Profile 已声明图片能力。
- [x] 历史附件和工作区 inspect 固定调用独立视觉 Provider；不属于 Gemini 失败后的自动降级。
- [x] 这条历史工具路径不检查主 Profile 的图片能力，DeepSeek 调用相同工具也受影响。
- [x] 已记录 MCP 图片结果文本化、工作区路径缺口、来源复核和恢复缺口。
- [x] 已实现历史/工作区/当前与引用图片的共享纯准备和 Runner 私有媒体交接；实际 Provider/线上验收仍单列。
- [ ] 完成协议、并发、恢复、权限及资源回放验证。
- [ ] 提交、PR、合并、部署和真实媒体回复验收；各状态分别记录。

### 现场样本与因果范围

内部来源事件 `85236` 对历史图片 `85227/0` 调用 inspect。工具总耗时 24.001 秒，Qwen 视觉分析阶段记录 23.276 秒；图片此前已经缓存。该视觉阶段计时包含 Provider 分析接口内部的准备、排队、传输和结果解析，不能全部称为上游生成时间。它也不是数据库锁等待。

前后四轮读取同一张图片，四个问题文本均不同；不能仅按图片 hash 把不同问题的视觉摘要视为同一答案。图像原件/准备结果可以按内容版本复用，语义判断由当前主 Agent 做。

该轮另有 19.259 秒会话锁等待，与上一轮搜索到结束的时间对齐。会话串行、过期轮次退出是独立问题：本任务不得凭媒体改造直接删除锁、取消或权限围栏，也不得把本轮合理的联网搜索列为错误策略。

## 2. 缺口清单

下表源码路径以 `src/qq_ai_bot/` 为根，行号仅用于定位；实现前复核。

下表保留修改前的缺口与证据，不能作为当前仍调用 Qwen 的证明；实现状态见第 6 节和现行模块合同。

| 编号 | 入口/层次 | 已发现缺口 | 主要证据 |
| --- | --- | --- | --- |
| G01 | 当前/明确引用附件 | 已能进入主模型，但媒体准备逻辑与历史 inspect 分离 | `container.py:672–684`；`services/processor.py:1572–1580`；`services/chat.py:1566–1589` |
| G02 | 历史图片、以文件发送的图片 | `inspect_conversation_attachment` 直接调用独立 `provider.analyze`，返回观察 JSON | `workspace/service.py:72–86`；`conversation/media_service.py:375–470` |
| G03 | 历史视频 | 固定 6 帧/120 秒/15 秒采样后再调用独立视觉；与当前附件的运行时预算不一致 | `conversation/media_service.py:433–461`；`services/attachment_inputs.py` |
| G04 | 已发布工作区图片 | `workspace_inspect` 只读 immutable artifact，持全局 semaphore 等待独立视觉模型 | `workspace/inspect.py:20–45`；`workspace/tools.py:45–51` |
| G05 | Agent 自己找到的普通图片文件 | `workspace_read` 为文字读取；图片要先 publish 才能 inspect，不能直接选择 path/内容版本看图 | `workspace/tools.py:59–65,108–112` |
| G06 | 工具结果与 Runner | `FunctionCallOutput.output`、`append_result`、Agent 工具后端只传字符串，没有选定媒体随回执进入主请求的通道 | `domain/messages.py:341–345`；`services/turn_transcript.py:92–97`；`services/agent_runner.py:1697` |
| G07 | 来源表示 | `ChatImage.source` 只允许 current/reply；历史/工作区/tool 来源无法准确表达 | `domain/messages.py:250–262` |
| G08 | MCP 截图/图片 block | image 保留为 dict 后整体 JSON 化，可能成为大结果 artifact，未进入原生图片输入 | `mcp/result_normalizer.py:20–52`；`capabilities/results.py:125–169` |
| G09 | 工具 artifact | `read_tool_artifact` 只支持 text/JSON inspect/get/search，图片缺少有权的原生读取方式 | `services/chat.py:666–680`；`mcp/repository.py:874–948` |
| G10 | 插件图片 | plugin-owned media handle 主要用于通知投递；不能由通用图片读取器任意跨插件打开 | `plugin_host/facades.py:1986–2007`；`plugin_host/media_artifacts.py:109–129` |
| G11 | 请求时来源核验 | 历史工具读取后选中的事件不会自动进入下一实际 dispatch 的来源依赖；读取后忘记、重置或改文件可能使数据失效 | `conversation/media_service.py:335–365`；`runtime/work_source_guard.py:102–132`；`services/agent_runner.py:1120–1159` |
| G12 | 去重与恢复 | 字符串结果缓存/同批别名不携带媒体；临时共享图片队列会在 cache hit、并发或重启时丢图、错配或重读 | `services/agent_runner.py:2042–2056,2132–2145`；`runtime/work_journal.py` |
| G13 | 普通历史与私有协议 | 普通冻结历史拒绝 ephemeral images；实际请求中的媒体与公开历史不能直接共用同一持久表示 | `conversation/frozen_fragments.py:193–197`；`runtime/work_session.py:467–481` |
| G14 | 独立视觉装配与预算 | 历史/工作区 inspect 注入同一 Qwen Provider，且分别扣辅助模型请求；改配置为 Gemini 仍会产生额外模型往返 | `container.py:311–325`；`application/modules/media.py:91–105`；两个 inspect 的 `reserve_request(auxiliary=True)` |
| G15 | 当前/引用图片的旧前台回退 | 主 Profile 没有图片能力时仍进入 `VisionService.analyze`；这条路径也要改为明确未读，不能留隐式 Qwen 回退 | `services/processor.py:1616–1649` |
| G16 | 插件显式视觉能力 | SDK `vision.analyze_current_media` 经 `VISION_ANALYZE` 授权调用旧视觉服务；与主 Agent 看图及 plugin-owned handle 权限不同，装配清理前须明确保留或迁移 | `plugin_host/facades.py:1932–1959` |

### Provider 能力的准确范围

四种主协议已经能编码用户图片：Gemini `inlineData`、DeepSeek/OpenAI Responses `input_image`、Chat `image_url`、Claude base64 image block。证据分别见 `llm/gemini.py:69–81`、`llm/deepseek_responses.py:313–328`、`llm/openai_responses.py:11–35`、`llm/openai_compatible.py:59–74`、`llm/anthropic_messages.py:211–232`。

因此 DeepSeek 的问题是历史工具没有走这套已有适配，不是所有 DeepSeek 型号都没有视觉，也不是已证明任意 DeepSeek endpoint 均可看图。每个实际 Profile/路由仍需声明和验证图片能力；本次线上样本使用 Gemini，未做 DeepSeek 真实 API 验收。

官方协议还支持部分多模态工具回执：[DeepSeek Responses](https://api-docs.deepseek.com/api/create-response/) 的 `deepseek-flash` 支持工具 output 中的 `input_image`；[Gemini 3 function calling](https://ai.google.dev/gemini-api/docs/generate-content/function-calling?hl=en) 支持 functionResponse 的多模态 parts。现有 Yuki 工具回执没有适配这些结构，实际代理支持也未核验。模型可接图片不等于所有兼容协议都可接相同的图片工具回执。

## 3. 统一设计

### 3.1 模型选资料，Host 准备媒体

```text
当前/引用附件 ───────────────┐
Agent 选历史 event/index ────┤
Agent 选 workspace path/version 或 artifact ─┤
获准 MCP/插件工具图片 ───────┘
        ↓ 可信来源与读取授权
        共享本地媒体准备：真实类型、下载/缓存、解码、采帧、尺寸/字节限制
        ↓ 小型文本回执 + 私有的有来源媒体引用
        原 Runner / TurnTranscript / WorkJournal
        ↓ 原模型连接、同一请求链的原生图片输入
        主 Agent 判断内容、继续任务、显式 send_message
```

不自动搜索全部历史媒体，不把附件目录/全量图片塞入上下文，不用新的模型判断器替 Agent 选文件。历史检索保持现有有界查询和真实内部事件 ID。图片准备不生成视觉描述；本地文档解析、视频采帧和图片解码按真实格式执行，不因扩展名猜内容。

### 3.2 工具合同与文件选择

- 保留 `inspect_conversation_attachment` 的名称及 event/index 选择语义；执行改为授权媒体准备。`question` 保留为当前任务的数据说明，传给原主 Agent，不再作为第二个视觉模型的提示词。
- `workspace_inspect` 支持 `artifact_id` 或 `path` 二选一；可变 path 读取时冻结实际内容版本，接受 `expected_version` 防止把 Agent 看到的版本替成新文件。既有 immutable artifact 调用继续可用；不要求为每次查看修改或发布用户文件。
- 工作文件必须经 Manager/现有工作区能力读取；Bot 不借工具参数读取任意宿主路径。路径逃逸、软链接、设备文件及版本变化沿已有授权与文件合同处理。
- MCP 图片从已授权工具结果取得；大图使用已有 artifact/协议对象机制保留私有媒体，`read_tool_artifact` 增加明确的媒体读取操作。执行复核原 handle 权限、owner 和有效期，不把任意 URL 当成获准文件。
- 插件图片只处理原能力已授权给当前执行的结果或 handle。没有委托的跨插件图片继续拒绝；通用入口不授予 plugin-owned 存储新的读取权。
- 名称、schema、说明或媒体语义变化按工具合同版本建立新链边界；当前循环不按工具类型临时增删声明，旧 Work 保留原 ID、预算和已确认效果。

### 3.3 文本回执与原生媒体分开

复用 `ToolExecutionResult` 和已有协议/媒体对象，建立 Host 可信的类型化媒体部分；不要再建一个媒体数据库、视觉 Agent 或共享临时队列。文本回执只含原 call_id、来源、处理状态、数量/尺寸等小型元数据；Base64、像素正文和私有缓存路径不进入工具 JSON、普通日志、WebUI 展示结果或文字预算。

首期采用现有四协议都支持的输入方式：Runner 先按原工具调用顺序完整配对本批文本回执，再追加明确标注“Agent 选取的资料”的 `ChatMessage(images=...)`，进入原主链的下一次请求。该 `user` 是协议承载角色，不是新增真人消息，不生成 InboundMessage、聊天账本事件或用户授权；其业务来源仍是原工具 call 和执行 ID。

扩展或伴随 `ChatImage` 的可信来源信息，准确区分 current/reply/history/workspace/tool，保留事件/index、artifact/内容版本、原 call_id、hash 与采帧时间。历史图片不得伪标 current。

首期不要求新增四套多模态工具回执编码。以后如采用 Gemini functionResponse.parts 或 Responses output[]，应作为显式、固定协议策略验证，不自动尝试失败后改 wire 或切 Qwen。两种承载形式都必须指向同一个有来源媒体结果，不能形成不同业务链。

### 3.4 单一准备与资源预算

从 `AttachmentInputService`、`ImagePreprocessor`、视频采帧和文档 reader 提取/复用现有纯准备能力。current/reply/history/workspace/tool 使用相同真实类型检查、解码上限和运行时准备预算；不复制历史路径当前的固定 6 帧策略。

数量、帧数、解码像素、总 prepared bytes、输入图片与工具声明必须按下一次**完整实际请求**计量，不能每个并行 inspect 各自拿满额度。可变文件先冻结内容；大文件有界读取，不能把 200 MiB 缓存接纳上限当作一次图片内存预算。缓存仅复用相同版本的原件和纯准备结果，不按 hash 共享授权。

本任务不制造新的输出 token 上限，也不降低保护来提速。真实联合窗口不足时，沿现有合法容量/压缩边界处理，不偷偷丢图或声称已读。

## 4. 来源、续跑与持久恢复

1. 历史附件保留内部 event/index、同 Conversation、keeper/inbound、generation/floor 与到期核验；网络工作前后复核。工作区冻结为内容版本；插件/MCP 保留原执行授权。
2. 选定媒体的依赖纳入当前实际请求的来源 guard：在 Provider 名额取得后、HTTP dispatch 前，核验来源仍获准、隐私删除代次、generation、hash/版本与有效期。工具读取成功不等于模型已经看到。
3. 工具回执、选定媒体及其顺序必须形成一致的可恢复事实。复用 WorkJournal、ProtocolStore、`work_media.externalize/hydrate` 的内容寻址对象；准备和文件 I/O 在 writer 前，短事务只复核与发布引用。
4. 覆盖“工具回执已落地、paired journal 尚未落地”的断点。恢复按原 call_id 找回已准备媒体，不重新下载现在的 URL、不重新读取变动文件、不重做搜索/模型/已确认发送。
5. 同批去重、别名 call、只读缓存命中都必须携带同一不可变媒体结果，由 Runner 按原顺序消费；同一请求内可去除同源同版本重复像素，保留每项真实回执及来源。失效来源不能被另一份同 hash 的有效来源替代。
6. 私有实际请求保持 Gemini thoughtSignature、Claude signed thinking、Responses reasoning/opaque 的原顺序。图片追加不改已提交前缀，不把新媒体塞回初始消息或重建已提交协议。
7. 普通历史、Rollup、Memory 和公开展示只保留真实工具选择及来源/处理元数据，不自动附加旧图。私有 journal 只服务原执行恢复，不变成下一普通轮的全量媒体历史。
8. 新的主动读取仍受临时附件期限；已合法派发请求的私有检查点恢复与重新读取应区分。来源忘记、权限/generation 失效仍使后续派发失效，不能靠持有 hash 继续暴露内容；GC 按真实引用回收。
9. 去掉两个 inspect 的独立视觉模型调用及 `reserve_request(auxiliary=True)`；后续主请求正常扣原根预算。重启、分段、换链不退还或重置累计预算。
10. Profile 没有 `image_input` 时明确返回 `image_capability_unavailable`，模型不得声称已看图；不暗中换模型或调用 Qwen。正在运行的链按原 pinned Profile，热更新只影响合法新链。

## 5. 删除范围与旁路消费者

必须删除主 Agent 历史附件、工作区看图中的独立视觉请求、辅助请求扣费及仅用于包装视觉摘要的分支，也删除普通主 Agent 当前/引用图片在缺少原生图片能力时的隐式 `VisionService.analyze` 回退；工具媒体准备不得依赖 Qwen 的开关、密钥或生命周期。不得用“把独立视觉 Provider 改成 Gemini”替代主链统一。

删除旧服务前逐项确认其他消费者：表情后台分类 `emoji/classifier.py:92`、发送准备中的表情候选视觉选择 `emoji/selector.py:99–105`、视觉与表情缓存桥 `services/vision_service.py:532,672–693`、插件 SDK 显式 `vision.analyze_current_media`（要求 `VISION_ANALYZE` 权限）。这些不是普通 Agent 看图的隐式兜底；各自结构化输出和 SDK 兼容合同应明确保留或迁移，不能被解绑误删。代表 Yuki 生成/发言的插件仍共用主 Agent 原生输入，不借显式辅助接口建立第二个主 Agent。

当前基线没有 `emoji_inspect` 工具。表情库若通过获准文件或工具结果给 Agent 提供图片，同样进入统一输入；本任务不把未存在的工具写成已实现入口。

ASR 可能复用 Qwen 连接配置（`config.py:928–938`），且这项借用不依赖 `vision_enabled`；先检查实际凭据来源，不能连带关闭语音识别。删配置、容器初始化或共享 Provider 只在调用者全部迁走/明确保留后执行，旧用户配置迁移须报告真实影响。

单列后续资源缺口：`conversation/media_service.py:298–306` 在冷缓存发布时通过全局和会话 `rglob` 统计占用。应在有界容量维护任务中解决；缓存命中不走此段，不能用它解释本次 23 秒视觉分析，更不能混入“原生多模态已提速”的证明。

## 6. 实施阶段与交付物

- [x] P1：已定义 `PreparedMediaData`、私有 `ToolExecutionResult.images` 和真实 `ChatImage` 来源，当前/引用/历史/工作区复用纯准备；Manager 读取冻结真实文件版本，文档保持本地有界解析。工具图片与控制通道另有明确单结果容量限制，原请求继续执行累计预算。
- [x] P2：Runner 已在整批文字回执配对后追加有序原生媒体，纯 Chat 与 opaque continuation 共用该入口；同批别名、缓存及并发回执保持像素和顺序。五类 Provider 使用真实 Runner/HTTP MockTransport 的实际 payload 回放通过；不代表付费上游验收。
- [x] P3：WorkJournal 已保存私有图片对象及原 effect/call_id，accepted→paired 断点按原回执恢复；来源/隐私失效拒绝派发或晚到发布。无 Work 普通轮使用相同配对顺序；每次取得模型名额后复核全请求媒体容量与来源，包括私有 continuation 中保留的依赖。本地 Work/协议恢复回放通过，真实进程/生产演练仍在 P7。
- [x] P4：历史图片/图片文件/GIF/视频帧和工作区 path/artifact 已切换到原生输入，删除这些入口的 Qwen 调用、辅助请求扣费和 Provider 装配依赖。动态视频设置与真实时间戳、文件版本、来源失效已做定向本地测试。
- [x] P5：MCP 图片、大结果私有 artifact 和获准插件显式选图接入同一通道。SDK `media_artifacts` 只选择本插件拥有的 Host 句柄；SDK MCP 不自动注入。实际派发前复核原插件/工具、批准 revision、精确委托、原句柄 TTL/hash/文件，归档副本不扩权。本地授权/失效/回执测试通过，真实 API 与生产验收在 P7/P8。
- [x] P6：当前/引用图片的隐式视觉回退已删除；历史/工作区准备脱离独立 Provider 生命周期，后台表情/选择器、SDK 显式视觉和 ASR 保留。工具说明、workspace schema、主合同 revision 和现行媒体/Provider/Tool Kernel 文档已更新；未关闭仍被非主 Agent 消费者使用的凭据或配置。
- [ ] P7：完成下表回放及可控真实请求验收，记录未覆盖协议/路由，不以单一 Gemini 成功宣称所有 Provider 成功。
- [x] P8：PR #247 已通过六项 CI、合并并配套部署 Bot 与 sandbox Manager，生产 revision `f2d06f12e73894f8df7c78880a77b9539753b556`。实际 Bot UID 10001 的私有读图、来源验证和版本冲突拒绝通过；QQ 网关及持久环境的容器 ID/启动时间保持。自然 QQ 图片回复速度及 P7 全路由验收仍未全部覆盖。上线后发现的投影重建缺口另列于下面的补修记录，不由此勾选推定已修复。

建议并行实现分工：媒体授权/纯准备，Runner/协议输入，恢复/权限/资源测试；主会话持有共同类型、工具合同及文档整合。先确定文件归属，再并行修改；交叉依赖先固定接口，不各造图片队列。

## 7. 必要验证与完成标准

现有 `test_conversation_media.py`、`test_workspace.py` 对独立 observation/Qwen 计费的断言应替换为真实主请求媒体验收，不能只改 Mock 后宣称完成。复用 `test_attachment_dynamic_capability.py`、`test_work_journal_media_delta.py`、Provider 协议回放和现有竞态测试。

| 验证组 | 必须证明 |
| --- | --- |
| 来源覆盖 | 当前、明确引用、历史图片、文件中的图片、GIF、视频帧、工作区 path/artifact、MCP image、工具 artifact、获准插件/表情图片进入主模型原生输入 |
| 协议回放 | Gemini、DeepSeek Responses、OpenAI Responses、Chat、Claude 的实际 payload 有正确图片块；原签名/opaque、工具全清单、call_id 和请求顺序保持；不可依赖 DeepSeek tool_choice 强制行为 |
| 删除额外模型 | 上述 Agent 看图入口独立 `VisionProvider.analyze` 调用数为 0；主模型继续请求计入原预算，无暗中 Qwen 兜底 |
| 去重与并发 | 多工具先完整配对再附图；返回先后不改变原调用顺序；缓存命中、别名和恢复不丢图/重复附图；不同来源同 hash 不继承授权 |
| 断点恢复 | 工具完成/paired/dispatched 各阶段中断后原图字节、call_id、位置可恢复；文件变化或 URL 失效不令旧调用重读新内容；不重做模型、搜索和发送效果 |
| 失效竞态 | 读取后忘记、撤回、重置、权限变化、TTL/GC、文件版本修改、插件 owner 变化：实际派发前拒绝失效媒体，不能只在工具开始检查 |
| 资源与容量 | 多图/大图/解压炸弹/视频帧/并行 inspect 都遵守共享预算；实际请求计量含图片；准备不在 SQLite writer 内等待 I/O；不足时明确未读、不伪造成功 |
| 私有数据边界 | 日志、普通历史、Rollup、Memory、WebUI 和 tool JSON 无 Base64、像素、私有路径或凭据；可恢复私有对象按真实来源及引用回收 |
| 非图片消费者 | 本地文档解析、后台表情任务和 ASR 在实际保留配置下可用；关闭独立视觉后 Agent 看图仍可运行 |
| 可控真实验证 | 对 Gemini 实际代理、获准 DeepSeek 图片路由分别核验主模型收到像素及无辅助视觉调用；图片含无法从文件名/提示得知的随机图形，用来验证真实读取；无能力模型明确未读 |
| 速度验收 | 分开记录授权/缓存、预处理、排队、主模型、首次投递；证明额外视觉请求消失并报告端到端变化，不承诺统一后必然减少完整 23 秒 |

可控真实测试优先使用隔离的同配置环境和自制无隐私图片；生产自然样本只读沿内部执行 ID 对齐，不把旧测试授权无限扩大到新的公开群发送。若后续实施需要生产测试，先明确原会话、真实执行 ID 和发送范围，不发给未指定群。

最终验收以实际 Provider 请求和恢复事实为准，不以类名、配置 `image_input`、成功文本或 hash 相同代替。审查勾选与实施勾选分别位于对应章节；实施勾选只表示所列本地源码/验证已完成，不代表上线。全部 P1–P8 与验收组未通过前，不称“所有图片链路统一”。

### 验证记录（提交时尚未部署）

最终整合组收集 325 项：323 passed、1 个 Windows POSIX skip，一处精简 WorkControl stub 兼容错误。修正后对相关主入口、只读复用、原生回执与 Runner 四组复核，41 passed；其余整合项原本通过。Linux 一次性容器的定向组另有 62 passed、0 skipped，覆盖 POSIX Manager 有界 FD、软链接/路径逃逸和版本冲突。完整源码 Linux mypy（701 文件）、Ruff 及格式检查（1060 文件）、3.9.0 release baseline 通过。

真实上游使用自制随机图片和隔离数据库/Manager，不连接生产 QQ 或生产数据库。原生产 CHAT_AGENT 路由 Gemini 3.8 Flash 已通过 Agent 自选工作文件、Manager 读取像素、同模型第二轮识别随机数字和形状的真实验证：7.582s、2 个 HTTP 200、1 次工具调用、1 次来源复核、0 辅助模型。首次隔离网络失败保留为上游送达未知，修正 transport 后完成验证，累计 HTTP 尝试 3 次。其他四类协议只有 HTTP 回放验证，未调用其实际付费上游；P7 全路由验收仍保留未覆盖项。部署与自然 QQ 证据另外保存于本次 worktree `.cache`，不能由隔离测试推定自然回复速度。

最终复核另修复冷下载缓存的整块内存读取：最大 200MiB 的缓存接纳文件改为固定 64KiB 分块，只保留文件头、摘要、大小和稳定性凭证，发布前再次核验；源哈希、缓存命名和容量规则不变。新回归与既有缓存组共 10 passed，包含冷图片/视频/文件下载、禁止整块读取、空/超限拒绝、授权代次和发布前版本变化。冷缓存容量 `rglob` 仍是另列问题，本次没有引入新的缓存 accounting 或迁移。

2026-10-05 定向媒体/工作区测试共 30 passed、4 skipped，覆盖历史图片和图片文件、GIF、本地文档、真实采帧设置、当前/引用能力切换、忘记/重置来源、工作文件版本及已有 WebUI 下载行为。Windows 跳过的 POSIX 目录 FD/Manager 测试需 Linux CI 执行；不能标成已验证。相关 7 个源文件使用 Linux 平台类型检查通过，定向 Ruff 与 diff whitespace 检查通过。

主会话另一组本地整合回放共 174 passed，包含 `test_native_tool_media_runner`（9 项，其中五类 Provider 实际 HTTP payload 回合）、既有 Provider/DeepSeek 协议、Work 协议连续性、journal 媒体 delta、原生媒体回执与主 Agent 入口。HTTP 使用 MockTransport，没有调用付费上游。新增恢复/插件边界还继续整合验证，最终完整结果另记；实际代理/API、自然 QQ 与速度验收未进行。主 Agent 原生看图的额外 Qwen 请求已在源码删除，不由此推定其他搜索、模型排队或会话锁耗时已经消失。

交叉复核补修只读别名/缓存 accepted→paired 崩溃恢复：执行前原 response/pending 保存精确原回执 key，恢复复核同 Work/链、只读 accepted 状态和参数签名。新增 `test_work_readonly_reuse` 5 项通过，包括同批别名、跨批缓存、未知原执行、变造参数及错误借用副作用回执；与原生回执/Runner 相关组合共 25 passed。没有额外执行或预算，也没有另建媒体队列或 ledger。相关 3 个源文件 Linux 平台 mypy、Ruff 与 whitespace 检查通过。这些数量属于定向复核，不与此前重叠测试相加。

P5 插件桥接定向本地测试 `test_plugin_native_media` 及既有 facade、notification、cleanup、result-access 共 85 passed；涵盖明确选图、SDK MCP 只返回 owned handle、跨插件/假 JSON 拒绝、权限与 manifest 变化、过期/删除/字节变化在有效归档副本仍存在时由实际 dispatch hook 拒绝、原 TTL 不被延长及图片出版失败不重跑原效果。新增精确委托和 origin 测试另在最终整合记录。SDK 文档与迁移说明已更新。冷缓存容量 `rglob` 为另列的资源问题，未在本次原生媒体改造中宣称已解决。

### 上线后补修：合同切换的旧选取回放

2026-10-05 14:52:46 UTC 有界生产日志观察到插件后台唤醒 `ProjectionConflict`：当前触发事件已存在于 frozen input，随后 `append_current` 再次追加同一内部事件。失败位于主 Agent 上下文装配，不能归因于 Qwen 或本次 Manager 读图接口。

主工具合同 10→11 会改变投影合同 revision。`prepare_history` 原先只在装入旧 projection 时检查重复 current；bootstrap/capacity/contract_changed 从不可变选取记录重新恢复片段后漏掉同一检查，因此本次正常升级可能触发现有缺口。尚未读取该生产样本的实际 epoch reason，不把这一机制当成该样本的唯一原因。

补修复用既有 deliberate repeat/source_changed 边界：恢复选取后若包含当前 trigger，排除整个含 current 的旧片段，其余 frozen 片段保持原文；同组其余获准历史由新边界补齐，当前输入只追加一次。选取原件保持不变，不放宽重复事件保护，不重置 Work 或预算，不取得重跑已确认效果的资格。覆盖三个重建原因、混合 group、未变片段和无 current 的 Work 恢复。该补丁的验证、合并与 Bot-only 部署另记；Manager 协议保持，不再更新或重启。

新增 9 项真实 SQLite 回放通过；与既有选取 delta、准备复用、容量 I/O、来源读取及历史时间投影组合共 47 passed。覆盖有效旧投影和真正缓存缺失的 source_changed、新边界中混合 group 的历史补齐，以及显式 invalidated capacity 保留原 CAS reason；没有额外查询或写入机制。Ruff、格式、单源文件 Linux mypy 和 diff 检查通过。全量 CI 与这份补丁的生产部署尚待完成。
