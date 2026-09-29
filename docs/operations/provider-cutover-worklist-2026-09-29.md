# 多 Provider 切换、Work 迁移与 WebUI 验收清单

更新：2026-09-29。`[x]` 只表示该项代码已实现且对应验证完成；提交、合并、上线和真实调用另列，不由代码测试代替。

## 当前事实

- PR [#170](https://github.com/YuanYeYouTao/Yuki/pull/170) 已通过 Linux CI 并 squash 合并至 `main`（`a330ee6fd37e8813aecee54d8981f10bbeea52a5`）。Bot 镜像 `ops-7f42b26` 已单独部署，容器健康，数据库升级到 `0079`；这不代表代理或真实 QQ 场景全部验收。
- 跟进 PR [#171](https://github.com/YuanYeYouTao/Yuki/pull/171) 最新 Linux CI 全绿并 squash 合并到 `main`（`b5c51b3994028412a327392855a6350ed1c71857`）。代码相同的 Bot 镜像 `ops-c30f220` 已在 `/opt/yuki-qqbot/backups/pre-ops-c30f220-20260929T024221Z` 做 SQLite 在线备份与配置备份后单服务部署；新容器 `dcd3577bc837…` healthy，数据库仍为 `0079`，SnowLuma 与 mihomo 未重建。
- 生产 WebUI 已将 Gemini 3.8 Flash 的 13 个任务用途保留，并热保存 `reasoning_effort=low`；回执 `applied`，磁盘版本与已加载版本一致。保存前后的 Bot 容器 ID 相同，随后新模型请求的 Yuki 出站为 `thinkingLevel: low`，旧请求为 `medium`。
- 生产 WebUI 曾将该 Gemini 连接热保存为 `search_mode=native`、`native_web_search=true`，回执 `applied`；随后无 QQ 完整主链探针发现当前 Cloud Code 路径不能把固定函数与 `googleSearch` 放在同一请求。已立即按原文件版本通过控制面热保存回退到沿用 Tavily，回执 `applied`（版本 `2444569520056773`），再读 `matches_loaded=true`；Bot 容器 ID 未变且健康，`reasoning_effort=low` 保留。中间直接恢复磁盘文件只用于准备回退，最终控制面保存才使内存态生效。
- 已备份配置及数据库后，经 WebUI 热保存移除无新路由、无活动 Work 精确引用的 `flash` 和 `self_reflection`；旧 `pro` 仍被 5 个暂停 Work 的 journal 引用，其中 3 个有未知或待核效果，暂须保留。删除前后的 Bot 容器 ID 相同且健康。
- 另一台 `antigravity-server` 的 Gemini 代理已按最小补丁仅更新 Antigravity Manager 服务：当前镜像 `antigravity-manager:gemini-grounding-v4.8.4`（`sha256:d5b9abad…cab781`），容器健康；保留旧镜像、切换前备份及 `/opt/antigravity-manager/patches/v4.8.4-gemini-fixes-20260929/ROLLBACK.md`。旧末尾失败报文在当前代理重放 200，签名与 low 保留；原生搜索合成请求返回可信 grounding，但自然 QQ 续接和生产主链原生搜索尚未验收。
- **高危缓存观察**：只读核对线上 16 条 Gemini 调用明细后，输入合计 **362,512**、已报告缓存命中 **76,926**（占已记录输入 **21.2%**），与用户供应商截图的约 362.5K / 76.9K 相符。旧 Yuki 页面所报 **71.5%** 仅以报告缓存量的 3 次调用为分母，其余 13 次未报缓存量，不能代表总体。主对话 5 次输入 210,408、命中 60,571；自我反思 8 次输入 142,986、命中 16,355；另 3 次记忆任务输入共 9,118、没有缓存回执。16 次中仅 3 次低于 Gemini 3.8 Flash 官方 4,096 Token 隐式缓存门槛，其他未命中须逐调用核对前缀与服务端回执。

## 本轮状态快照

| 编号 | 本地状态 | 还不能打总勾的原因 |
| --- | --- | --- |
| 1 搜索与工具 | DeepSeek 官方搜索桥真实调用返回服务端搜索事件；Gemini 非流式 SSE collector 已修补并单服务部署，单独搜索回包有可信 grounding；混合主链失败后已热回退到 Tavily | 当前 Cloud Code 路径的固定函数与 `googleSearch` 混用返回 400；需独立搜索请求方案及验收，Claude 真实代理、费用与无来源回退也未验收 |
| 2 多模态 | 附件路由与预算通过；当前 Gemini/DeepSeek 两连接的图片、双视频帧和函数工具经真实 API 成功，Gemini 结构化及 DeepSeek 函数式结构化探针成功 | 真实 QQ 附件、长上下文、未配置 Provider 和原生音视频尚未验收 |
| 3 热配置 | 生产 WebUI 两次保存均回执 `applied`，已加载版本立即一致；后续 Gemini 请求由 medium 变 low，Bot 未重启 | 在途请求隔离、保存失败回滚由定向测试覆盖；排队旧纯文本媒体仍需重新引用 |
| 4 Work 接续 | 双向 Gemini/DeepSeek 的 SQLite + Runner 隔离回放通过，同一 Work ID/预算/确认回执保留、工具过滤与来源迁移有效，40 项回归通过 | 生产真实暂停 Work 的未知效果须原 ID 对账，不能靠模拟恢复；自然热切换仍待验收 |
| 5 用量口径 | 分层缓存率、失败响应用量、HTTP 请求尝试数及未知用量已实现 | 原生搜索额外费用和真实账单未核对 |
| 6 用量页面 | 生产真实 24 小时数据下，宽幅双图在浅色/深色主题均无图内横向滚动；本地三主题 hover 已验证，深色提示字对比修正代码已随新镜像上线 | 深色提示字尚待新镜像页面目视复核 |
| 7 旧配置 | 6 项无效选择配置已删除；`flash`、`self_reflection` 已备份并热移除 | 5 条 suspended Work 精确引用旧 `pro`，其中 3 条有未知发送或待核效果；须证据对账后才能删 `pro` |
| 8 上线验收 | 后端全量 1,654 通过/7 个 Windows 跳过，前端 85 通过；PR #171 最新 Linux CI 全绿并合并，新 Bot 单服务部署 healthy，生产配置与运行态一致 | 原生 Gemini 搜索桥、跨供应商自然切换、真实 QQ 多模态与账单等验收未完 |
| 9 状态与收发消息 | 新镜像上线后，生产外部事件 #72188 经控制面查到 3 个可信执行轮次，原先方向误拒已修；本地长轮分页与授权详情通过测试 | 生产长轮页面、授权正文和交互仍待目视复核 |
| 10 高危：跨供应商缓存 | Gemini 67 条 Yuki/代理调用逐条匹配；新版同链两次明确命中 40,268/42,551 与 40,323/43,834，旧 Dream 动态 system 已修；代理 `countTokens` 400 已最小修补并在生产返回 200 | 该 Cloud Code 计数接口只覆盖 `contents`，不能当作完整前缀计数；长期命中、账单、其他 Provider 与新版 Dream 批次仍待验收 |
| 11 高危：Gemini 代理抓包 | 用户确认是清理前旧请求；已核对结构、线上 Base URL，以及代理入站/转发/响应三栏 | 需确认最终发往 Google 的请求；当前字段顺序是否影响缓存不能凭单次抓包判断 |
| 12 高危：Gemini 思考档位与代理改写 | Yuki 生产热保存 low 后连续请求确实发送 low；当前生产代理的合成 low/medium/high 请求均保留对应档位且无固定预算，尾段旧报文重放 200 | 仍需自然 Yuki 尾段与配置热改档的全链验收 |
| 13 高危：本轮运行资料夹杂弱相关内容 | 本地不再自动附旧事实/事件摘要；统一 `search_memory` 有全局授权候选；Person 7+7 探针未复现编造；紧凑历史合成 A/B 输入 Token 在 Gemini/DeepSeek 降 33–36% | 记忆缺人工金标/完整 Agent 补查；DeepSeek 缓存也减半，尚不能证明紧凑历史真实费用与工具轮次净收益 |
| 14 记忆向量服务启用与降级 | 生产记忆页显示启用、配置齐全、1,158/1,158 活跃事实覆盖；隔离探针中缺凭据、超时与 503 均保留词法结果并标记非穷尽 | 生产断网/重启后的开关生效与人工金标精度仍未验收 |
| 15 高危：末尾工具回执后模型请求 400 | 14/14 历史同类 400 均发生在末尾函数回执；代理补丁同报文重放 200。随后两条自然 QQ 轮次各一次 `send_message` 后 Gemini 最终请求成功并 `turn_end`，合计 5 个不同的已记录投递事件，无重复发送工具执行 | 已覆盖所报末尾路径的自然样本；继续观察其他未出现的对话形态 |

## 任务与完成标准

- [ ] **1. 供应商搜索与工具清单**：按连接/协议明确选择原生搜索或外部搜索；Gemini、OpenAI Responses、DeepSeek、Anthropic 的请求格式与能力声明一致；当前供应商不可用的工具不得出现在请求的 tools 中；搜索事件、失败和来源可追踪。当前：各连接模式、声明过滤已通过定向验证；DeepSeek 官方搜索桥真实调用有 `server_tool_use` 与 `web_search_tool_result`。Gemini 单独原生搜索可返回可信 grounding，但当前 Cloud Code 路径混合固定函数会 400，生产已恢复 Tavily，独立搜索请求尚待实现。
  - [x] Gemini 原生 Google Search 与 Claude server-side web search 的协议适配和离线回放已实现；Gemini/Claude 续接保留服务端片段。
  - [x] 每连接搜索策略、协议能力校验和当前 wire tool 声明过滤已实现；Provider 定向测试 94 项通过。
  - [x] 删除 `request_tools` 声明、执行路径、检索状态和插件事件；能力运行时改为完整稳定声明，权限只在执行处收窄。联网集成 12 项、能力/社交/跨轮定向 34 项通过；全量回归继续进行。
  - [x] DeepSeek 搜索桥使用单独选择的官方 DeepSeek 连接，不再从 `chat_agent` 路由偷取密钥；新保存显式校验连接和密钥并预建后热激活，缓存按密钥隔离。在途搜索完成后才关闭旧后端，多次热切不会累积 HTTP 客户端。后端 38 项、前端 14 项定向及构建通过；旧配置仅在聊天路由本身是 DeepSeek 时兼容启动。
  - [x] 修补前的 Gemini 3.8/2.5 Flash 强制搜索探针：Forwarded 均保留 `googleSearch`、HTTP 200/STOP，却无 `groundingMetadata`；不能把文本或其中 URL 当作原生搜索成功。隔离直连同一 Cloud Code 账号与模型时，上游 SSE 真正返回查询、来源块及支持关系，证明上游路径可用。
  - [x] 根因是 AGM 对客户端非流式请求强制走 SSE 后由 collector 合并 JSON，而旧 collector 丢弃 candidate 的 `groundingMetadata`。最小修补按事件顺序保留查询、来源块和支持关系，Rust 3 项回归通过；修补版 canary 和当前生产代理的合成搜索均返回 1 query/1 chunk/4 supports，Yuki `GeminiProvider._parse` + `recover_native_web_response` 得 1 个受信来源且 `partial_failure=false`。尾段签名、LOW 无固定预算、`countTokens` 200 回归通过；代理只更新 AGM 服务并保留回退。来源来自 Google 元数据而非正文 URL。
  - [x] 生产 Gemini 曾短时热保存 `search_mode=native`、`native_web_search=true`，无 QQ 合成主链确认 Yuki 正确排除了外部 `web_search`/`read_webpage`，却在 109 个固定函数加 `googleSearch` 的混合请求上从 AGM/Cloud Code 收到 HTTP 400；缩到 1 个函数、尝试透传 camel/snake `includeServerSideToolInvocations` 仍 400。已通过控制面热保存回退沿用 Tavily，回执 `applied`、有效文件与内存态 `matches_loaded=true`，Bot 未重建。不可据单独 `googleSearch` 成功宣称主链可用；需要独立搜索调用或其他已验路由。
  - [x] 使用生产 DeepSeek 官方搜索连接作不经 QQ 的真实 API 探针，HTTP 200，回包有服务端搜索调用与搜索结果事件；额外费用仍须账单核对。原生来源解析不再把模型正文中的任意 URL 当作搜索来源；无受信来源时回执明确为部分失败，新增 3 项回归。
  - [ ] 在生产 Gemini 主链按连接策略验收原生搜索、工具声明与来源；Claude 真实代理、无来源回退和额外服务端费用/账单仍待核对。
- [ ] **2. 多模态与协议能力**：核对图片、视频帧、语音、文档从入口到各 Provider 的传递；不支持的能力在保存或执行前明确拒绝；工具调用、结构化输出、思考状态的协议回放分别验证。当前两条生效连接的图片、双视频帧、函数工具和函数式结构化输出已有无 QQ 真实 API 证据；长上下文、真实 QQ 附件、未配置供应商及原生音视频尚未验收。
  - [x] 图片与视频帧输入能力改为读取当前模型路由；动态开关回归 1 项通过。语音仍由独立 ASR 转文字；没有原生音频/视频输入，不能宣称已接入。
  - [x] 续跑消息和续跑条目的图片现同样经过 `image_input` 能力检查，纯文本连接无法绕过；协议、媒体定向 9 项及 Ruff 通过。文件仍解析为文本，视频仍采样为图片帧。
  - [x] 当前消息与引用消息的附件按顺序共用同一件数、视频帧数及字节预算；混合附件中可恢复的单件读取失败保留前面已读图片/帧，给出附件序号、来源与失败原因，而不抹掉整个媒体输入。附件定向 5 项及 Ruff check/format 通过。真实 QQ 附件仍未验收；语音仍依赖独立 Qwen ASR。
  - [x] 使用生产 Gemini 代理对合成 PNG 与两帧图片请求进行无 QQ 副作用的真实 API 探针，均为 HTTP 200 且返回与图像相符的描述；文档仍按文本解析，语音走 Qwen ASR，视频走抽帧图片。21 项协议/媒体/ASR/文档定向验证通过；真实 QQ 附件与其他 Provider 仍待验收。
  - [x] 只读核对当前真正加载的 `webui-config/model_profiles.toml`，只有旧 `pro` DeepSeek Responses `deepseek-flash` 与 Gemini 3.8 Flash 两条连接；服务器旧 `config/model_profiles.toml` 不是当前有效文件。用现有 DeepSeek 密钥发送合成 32×32 红色 PNG 至官方 Responses，HTTP 200 且正确识别红色，因此当前 `deepseek-flash` 的 `image_input` 声明有真实依据，不能按旧印象强制拒绝。
  - [x] 生产连接无 QQ 合成探针：DeepSeek Responses 经 Yuki 适配器识别红/蓝双帧、发起一次 `lookup_code` 工具调用，low reasoning 有响应，`emit_result` 函数返回 `value=7`；Gemini 适配器同样发起工具并返回结构化 `value=7`，此前图片/双帧也成功。DeepSeek 当前未声明 `structured_output`，故路由不会把需要该能力的任务交给它；两连接的 tools/image_input 声明与已测一致。long_context 未用大输入实测；文档与语音仍为 Yuki 文本解析/独立 ASR，其他供应商未配置。[DeepSeek Vision](https://api-docs.deepseek.com/guides/vision/)、[Responses](https://api-docs.deepseek.com/guides/responses_api/)。
- [x] **3. 热配置**：WebUI 保存连接和路由后，运行中立即、原子地用于新任务/新请求；失败不切换；已开始的模型请求保持原连接；WebUI 明确显示当前已加载版本。生产两次模型配置保存均为 `applied`、加载版本立即一致，Bot 容器 ID 未变；旧请求发 medium，热保存后的新请求发 low。失败回滚与在途旧连接隔离由定向测试覆盖。
  - [x] 本地保存入口预构建新连接，成功写盘后切换执行器；同一 Agent 激活固定旧版本；保存失败保持旧路由。对应定向测试通过。
  - [x] 管理查询读取已加载目录，保存回执标记为已生效，WebUI 文案说明新激活立即切换。对应定向测试通过。
  - [x] 入站媒体准备到模型请求固定同一目录；排队 Work 输入若遇旧纯文本连接，明确说明图片未读取，不向不支持的 Provider 发送。媒体及 Work 定向 32 项通过。
  - [x] 生产 WebUI 原管理 HTTP 保存 `low` 和移除闲置连接后均立即加载；新模型请求确实采用 low。排队媒体在旧纯文本激活中被消费后不自动重取，需重新引用或由历史附件工具读取，这是明确的输入边界。
- [ ] **4. 原 Work 跨 Provider 接续**：沿原 work/run/execution ID、预算、已完成或未知效果回执，在显式新链边界恢复；共同工具可复用，同类工具仅经显式语义映射迁移；不得回放已发送消息或把旧供应商的不透明续接片段交给新供应商。当前双向 Gemini/DeepSeek 的隔离 SQLite + Runner 回放已验证保留 Work ID/预算/回执、工具过滤与公开来源迁移；生产暂停 Work 的未知效果仍须原 ID 对账，自然热切换未验收。
  - [x] 公开搜索来源按 URL 和标题受限保存，换 Provider 后作为不可信来源摘要进入新链；本地 Work 会话测试通过。
  - [x] Claude `pause_turn` 的原生搜索续接保存原服务端工具片段，不注入合成恢复消息；离线适配器和 Runner 测试通过。
  - [x] 只读代码审计确认 `profile_revision(CHAT_AGENT)` 包含路由/连接合同；切换后旧 journal 走 `contract_changed`、以 compaction anchor 在新 Provider 建链，原 Work ID 和预算不重置，也不回放旧供应商不透明续接片段。真实暂停任务仍须逐项验证回执与授权来源。
  - [x] 跨 Provider 的模拟 Work + SQLite 回放确认：同一 Work ID/预算 3→1、已确认效果不重放、共享 `workspace_read` 继续可用、旧供应商搜索工具不出现在新链、Gemini 原生搜索与 low 档请求正确；公开 URL/标题/有界片段与已证实结果可迁移，私有/本地 URL 与不透明续接被拒。旧 journal 的 pending 效果按原 effect_key 只读对账，PREPARED/UNKNOWN 保留不确定围栏并拒绝新副作用；Work 两组定向 31+27 项通过。
  - [x] 新增反向 Gemini→DeepSeek 的隔离 SQLite + 真实 Runner 回放：同一 Work ID 与预算继续累计、确认发送只执行一次、共同 `workspace_read` 继续、公开来源进入新链、旧私有签名/本地 URL/原生工具不进入 DeepSeek 请求；连同已有反向与 UNKNOWN 围栏回归 40 项通过。未发送真实 QQ 消息。
  - [ ] 生产自然热切换时核对 Work ID、预算、效果回执、共同工具和同类搜索来源；先按原 ID 取得未知/待核效果的外部证据，不能为了验收恢复或重发这些 Work。
- [ ] **5. 用量和缓存口径**：各 Provider 的输入、输出、推理、缓存读取/写入与缺失值核对；总览、模型、用途、连接、时间桶都展示缓存命中率及分母；失败请求与额外服务端搜索调用不伪装成零消耗。当前：本地增加了模型/小时分桶及分层缓存口径；协议和实际成本仍待验收。
  - [x] 本地增加按供应商/模型/小时分桶及各层缓存命中率；缓存数或输入数缺失时标记未知，不算已知未命中。统计定向测试 2 项通过。
  - [x] 适配器提供失败响应已知 usage，执行器将非负已知数字写入失败调用账本；定向测试通过。
  - [x] 用量页把“已确认缓存占已记录输入”和“仅已报缓存量调用的样本命中率”分开，避免把 Gemini 的 71.5% 误当全部输入的命中率；前端 78 项通过并构建通过。
  - [x] 逻辑调用与实际 HTTP 请求尝试分开记录；重试和 Claude 原生搜索续发分别计数，未收到上游 Token 用量的请求标未知，历史行不推算请求次数。新增 0079 迁移和 WebUI 口径，后端定向 32 项、前端 1 项、TS/Ruff/Prettier 通过。
  - [ ] 按供应商账单核对原生搜索额外费用和物理请求计数；Token 回包不能推算原生搜索费用。
- [ ] **6. 用量页面重做**：按参考图制作宽幅双图、24 小时趋势、输入命中缓存/未命中/输出的分段量、悬浮明细；图表在正常桌面宽度不出现内部横向滚动，小屏有明确响应布局。生产真实数据下浅色/深色宽幅双图与无内滚动已验收；深色统计说明的对比度补丁已随 `ops-c30f220` 上线，待新镜像目视复核。
  - [x] 本地完成加宽响应布局、无图内横向滚动、每模型双图、缓存命中/明确未命中/未知/输出分段与键盘可用明细；前端构建、定向测试与格式检查通过。
  - [x] 浏览器预览在 649px、1280px 未出现图内横向滚动；浅色、深色、月夜主题的柱位悬浮背景和提示框实际显示，颜色均随主题变量变化。
  - [x] 部署后以真实 24 小时用量在完整 WebUI 核对浅色和深色主题：双图在正常桌面宽度均完整显示，无图内横向滚动；统计区显示缓存已确认比例、样本比例与未知调用数。柱位悬浮明细已在本地三主题浏览器测试覆盖；深色说明文字对比度另作跟进。
- [ ] **7. 旧连接清理与配置解释**：核查存量 Work 对旧连接 ID 的引用；安全迁移或明确归档闲置连接；生效配置按供应商/模型名称和新任务用途展示，不让历史调用记录误导为当前路由。生产 `flash` 和 `self_reflection` 已备份并热删除；5 条 suspended Work 的 journal 精确引用旧 `pro`，其中 3 条有未知发送或待核效果，不能直接删除。
  - [x] 本地删除 4 项已失效的工具首轮选择设置和 2 项已失效的 MCP 选择预算；管理配置入口同步删除，配置定向测试 47 项通过。
  - [x] WebUI 已有“全部用途使用当前模型连接”的一次性路由操作；新增前端定向测试确认保存请求中的全部任务都改到选中连接。
  - [x] 只读生产核对模型配置与 5 条未终结 Work journal：Gemini 连接承担 13 个新任务路由；旧 `pro` 承担 0 个新路由但被 5 条 suspended Work 以 `profile_id` 引用；旧 `flash`、`self_reflection` 既无新路由也无活动 journal 的精确引用。历史用量记录不作为删除阻碍或当前路由证明。
  - [x] 只读生产列举 `runtime_config_overrides.config_key`，没有本轮移除的 6 项旧工具选择覆盖；未读取覆盖值或密钥。
  - [x] 只读核对 5 条 suspended Work：均有 compaction anchor、无待处理模型工具调用；其中 2 条各有一条 `runtime_delivery_intents.state=unknown`，另 1 条有一条 `runtime_work_effects.state=prepared`，管理恢复入口会拒绝带未知/待核效果的任务。按原 function_call 参数和旧分段算法重算，这 3 条 sequence 的 manifest 哈希均与持久父回执吻合。两条 UNKNOWN 分别卡在 seq:0 与 seq:1，缺平台回执/账本事件，后续分段无发送回执；不能由缺记录推断未发送。第三条 PREPARED 的 4 个计划分段全部有 succeeded 回执、出站事件与平台 ID 逐条匹配，但 Work effect 仍是 prepared、回执为空且 journal 明确 replay_forbidden；需按原 effect key 审定并原子补录结算，绝不能重发。
  - [x] 两条 uncertain 发送的原 operation ID 在现存社交回执、精确账本关联及现存网关/Bot 日志中没有可证明已送达或未发送的证据；OneBot 调用没有携带该 ID，当前网关无法据此直接查询。不能按正文/时间猜测内部事件身份或改写为成功/失败。另两条无未决发送的 Work 因 `gateway_disconnected`、`ValueError` 暂停，可作配置迁移候选，但须分别处理原错误；旧 `pro` 仍须保留以维护 5 条 journal 的恢复边界。
  - [x] 备份后通过 WebUI 热保存移除 `flash`、`self_reflection`；回执 `applied`，磁盘与运行中版本相同，Bot 容器未重启。
  - [ ] 对两条 UNKNOWN 取得传输层确证，或在保留未知状态的前提下设计并审定归档；对一条 PREPARED 以原 effect key 设计可审计的结算；另两条处理原暂停错误。完成安全迁移/归档后才能清理旧 `pro`，不得猜测未发送或重发。
- [ ] **8. 集成、上线与真实验收**：完成定向测试、最新提交 CI、PR/合并；Bot 单独部署并验证健康、路由、WebUI 状态、Gemini/其他供应商实际调用和缓存统计。PR #170 与 #171 均已通过最新 Linux CI 并合并；`ops-c30f220` Bot 已单服务部署 healthy，数据库 `0079`，生产配置读回 `matches_loaded=true` 且 Gemini low/外部搜索保留。本地后端全量 **1,654 通过、7 个 Windows 平台跳过**，前端 **85 项通过**、构建与 Ruff 全绿；前轮记忆质量集 **19/19**。自然 QQ 末尾工具续接已有两条成功样本；Gemini 搜索桥、跨供应商自然切换、真实 QQ 多模态与账单仍待完成。
- [ ] **9. 本轮状态、收到和发出的消息**：按内部事件与执行 ID 关联真实收发消息；每个状态可展开具体动作、工具参数或结果的授权摘要、失败原因和时间；正文只在相应权限下读取，不能以 `redacted` 或阶段标签代替可核查的操作。生产聊天页已按真实事件显示收发方向、昵称和源事件；执行详情测试覆盖长轮分页。`ops-c30f220` 上线后，控制面按 `direction=external` 查询事件 #72188 返回 3 个可信轮次，原先方向误拒已消除；新镜像页面的长轮交互和授权正文仍待目视复核。
  - [x] 状态页在原权限边界内显示发送者昵称或群名片、内部事件、跨会话目标、确认投递、正文截断标记和同一操作的耗时；参数与结果按需授权读取。后端 6 项、前端 10 项定向通过；前端全量 78 项与构建通过。
  - [x] “本轮具体操作”汇总现包含 `model_start`、`provider_start`、`provider_response`，能按现有权限展开实际输入、发给 Provider 的请求和响应；前端定向 11 项与格式检查通过。
  - [x] 长轮可逐页加载更早的状态与收发消息，`turn_id + before_step_id` 成对校验原会话和真实 root，按轮次索引每页最多 32 步；正文权限独立。后端定向 7 项、WebUI HTTP 1 项、前端 12 项以及 TS/Ruff/Prettier 通过。
  - [x] 对生产内源事件 #72154 和外部事件 #72188 只读核对：前者仅有 3 条语义观察、没有 Runner 根，因此“无执行轮次”正确；后者有 `turn_start`、模型和工具等 60 条轨迹。修复查询对 `direction=external` 的误拒，后端定向 8 项、含 external 的前端 18 项通过。
  - [x] 跟进镜像上线后，生产控制面以外部事件 #72188 的原 conversation ID 和 `direction=external` 返回总数 3、当前页 3 个可信执行轮次；配置、数据库版本和容器健康也已核对。真实长轮的页面交互与正文授权呈现仍待目视复核，不能把 API 成功当作完整视觉验收。
- [ ] **10. 高危：其他 Provider 的缓存命中与前缀稳定性**：以实际供应商账单/原始 usage 对齐总输入、缓存读写和缺失字段；逐一审计 Gemini、Anthropic、OpenAI 等请求的固定 system/tool 前缀、消息顺序、动态时间/上下文插入位置、工具声明和切换续接；按供应商官方缓存语义优化，并以重复请求的真实命中和成本验收。当前：Gemini 外部图曾约 21.2%、最新约 14.0% 的已报告缓存占总输入，与 Yuki 线上 71.5% 的“已报告子集命中”分母不同；先修统计展示并定位请求前缀，不能把显示比例当成优化成效。
  - [x] 代理管理页 2026-09-29 约 07:47 的只读汇总显示 38 次 `gemini-3.8-flash` 请求、约 809.6K 输入与 113.3K 已报告缓存，已报告缓存约占总输入 14.0%；比先前 16 次调用的 21.2% 快照更低。该页面聚合值经过取整，且未知缓存回执仍需单列，不能把余量自动解释为明确未命中或由 Yuki 前缀造成。
  - [x] 修复 Claude 原生搜索追加到工具列表后缓存断点未落在最终工具上的问题；协议定向 7 项、Ruff 通过。
  - [x] Gemini 适配器的静态系统说明、动态用户上下文位置和 `cachedContentTokenCount` 映射已审计；未发现有证据的序列化错误。官方文档指出 Gemini 3.8 Flash 隐式缓存最低 4,096 Token，依赖相同的大前缀和短时间重用，不保证命中。
  - [x] 对旧抓包与现行 PromptCompiler 核对：固定系统说明独立于消息历史；`contents` 按旧到新排列，运行时间等逐轮资料附在当前用户消息中，因而不会每轮改写系统说明。会话摘要位于历史最前端；为控制上下文长度而重写摘要或裁剪历史时，共同前缀变化属于必要代价，应在分析命中率时单独标记，不能为了缓存阻止正确的摘要更新。工具声明虽在 JSON 对象中列于 `contents` 后，不能据此推断模型按该文本顺序读取或缓存。
  - [x] 只读生产 trace 12076→12086→12091 属于同一轮/事件 72110，间隔约 38 秒、20 秒；系统说明、114 项工具声明和 generationConfig 的哈希均不变，旧 `contents` 前缀依次完整保留 60/62 项。三个响应的 `cachedContentTokenCount` 依次缺失、36,384、缺失；第三次无缓存回执不能归因于已证明稳定的 Yuki 前缀，也不能把缺失当作零。旧连接仍为 medium；代理不在 Yuki 宿主，尚未读到最终 Forwarded。
  - [x] 用户切回代理“官方自适应”后的只读生产 trace 12099→12112→12123→12136→12147 中，Yuki 出站的 system、tools、generationConfig 哈希一直相同；相邻历史 `contents` 的完整共同前缀分别为 60/60、59/62、62/62、61/64 项。后两次差异发生在历史尾部，前五项保持相同；另一个新轮次 trace 12169 的历史首项不同，不能把跨轮摘要/裁剪差异误认成同轮序列化抖动。这些真实调用的缓存 usage 多数缺失，不能证明命中为零，也不能证明代理已保留 low。
  - [x] 只读生产还发现 `memory_dream` 的系统说明逐簇变化；本地已将 Episode 每簇原文字数/软压缩目标移到当前输入，系统说明在同类簇间固定。压缩判定定向 2 项与 Ruff 通过；实际缓存增益待上线验证。
  - [x] 部署后只读对齐 Yuki 与代理 67 条 Gemini 调用。旧版 62 次中 50 次成功，成功输入 1,304,888 Token、明确报告缓存 366,830（占 28.1%），其余缺回执不能算零。新版同一聊天链连续三次请求的 system、tools、generationConfig 哈希相同；后两次明确命中 40,268/42,551 和 40,323/43,834 Token。旧版 12 次 Dream 指令在逐簇数字处变化，已部署修复移到动态输入；新 Dream 批次还未出现。
  - [ ] 按单次真实请求核对前缀长度、前缀 hash、时间间隔和服务端缓存回执；区分必要的摘要重写/历史裁剪与摘要未变时的非预期前缀变化。旧自我反思固定说明以**普通 user 内容**送入当前代理 `countTokens` 约 2,542 Token；完整旧请求在该接口是 18,383，而同一成功 `generateContent` 实际 input_tokens 为 20,927、cache 为 16,355。Cloud Code v1internal 对直接 `request.systemInstruction` 计数不变，对公共 REST `generateContentRequest` 则报 400 unknown field；因此当前代理计数只能标为 `contents-only`，不能以相加近似冒充完整前缀、工具或系统说明的精确 Token 数。测量依据已记录于代理服务器 `COUNT_TOKENS_SCOPE.md`，布局决策须看真实调用 usage 与稳定前缀。
  - [ ] 对比逐供应商的真实费用与命中，再决定是否引入有存储费用、需维护 TTL/模型/工具版本的 Gemini 显式缓存；逐供应商验收优化后的命中率。
- [ ] **11. 高危：Gemini 代理请求格式与顺序**：用户确认这份抓包是删除 `request_tools` 前的旧请求。顶层为 `_session_thinking_id`、`thinkingConfig`、`systemInstruction`、`contents`、`tools`，缺少仓库适配器对函数请求会构造的 `generationConfig` 和 `toolConfig`；共有 56 条历史 `contents`、114 项函数声明。线上连接的 Base URL 指向抓包 Host 的 `/v1beta`；仓库 HEAD 和本地新适配器均把思考参数放在官方 `generationConfig.thinkingConfig`，没有 `_session_thinking_id`。JSON 属性排列本身不改变协议语义，但 Google 未承诺隐式缓存如何按原始 JSON 字节计算；需识别抓包是在旧部署出站、代理入口还是代理改写后，并核对最终上游请求的字段和值。
  - [x] 仅元数据方式检查抓包：历史按旧到新放在 `contents`，本轮运行资料放最后一个 user 消息尾部；未见仅因消息数组顺序就颠倒新旧上下文的证据。当前配置和本地代码与抓包差异已定位，不把代理差异误记为本地已修复。
  - [x] 代理截图明确区分入站 Request、转发 Forwarded 与 Response：该次请求返回 200，代理把模型名映射到 `gemini-3.8-flash-tiered`，并把 `thinkingLevel: medium` 改为 `includeThoughts: true`、`thinkingBudget: 4096`；该次显示约 46.9% 缓存命中。这说明代理可处理此请求，不证明转发页所示 JSON 原样到达 Google，也不证明其他请求的缓存表现。
  - [x] 对 2026-09-29 07:47 左右的生产 `provider_start` 12169 仅核对字段名：Yuki 记录的出站 body 顶层是 `contents`、`generationConfig`（含 `thinkingConfig`）、`systemInstruction`、`toolConfig`、`tools`，没有顶层 `_session_thinking_id` 或 `thinkingConfig`；同时间代理 Request 页面却展示后两者。两侧可见格式不一致，代理的 Request 展示不能直接当成 Yuki 原始 HTTP 字节；须在部署后以稳定请求关联进一步定位展示/转换层。
  - [ ] 用相同执行 ID 对照 Yuki 出站、代理入站/出站和 Google 回执；确认是否真的发送了非官方顶层字段，以及旧工具声明是由哪个已部署版本注入。新版本部署后再抓包验收。
- [ ] **12. 高危：Gemini 3.8 默认使用 low，且不得被固定预算覆盖**：用户要求 Gemini 3.8 默认以 `thinkingLevel: low` 思考，而不是 `medium`；代理把档位改成固定 `thinkingBudget` 也不可接受。WebUI 预设及现有生产连接均已改为 low，生产 Bot 新请求出站为 low；代理最小补丁已部署，合成 low/medium/high 请求均保留对应档位且无固定预算。旧连接环境覆盖、保存失败与在途隔离有定向测试；自然 Yuki 尾段和真实热改档全链仍待验收。`thinkingBudget: 4096` 是输出思考预算，**不是** Gemini 隐式缓存的 4,096 输入 Token 门槛。
  - [x] 新连接默认 `low`；WebUI 可直接调整思考档位并解除旧环境变量覆盖；Yuki 保存和发请求时拒绝 Gemini 3.8 固定预算方言。后端 7 项、前端 13 项及类型/Ruff 检查通过。
  - [x] 只读生产确认现有 `connection_2d4ba9aad301` 仍为 `gemini-3.8-flash` + `reasoning_effort=medium`，没有 `reasoning_effort_env` 覆盖；因此仅改新建预设不足以完成现有连接切换，部署时还要热保存为 low。
  - [x] 用户最新代理截图曾显示“自定义思考预算模式”，Flash Low/Medium 为 32,768、High 为 65,536；用户随后确认已改回“官方自适应”，并要求 Yuki 仍显式发送 `low`。此处只记录用户设置反馈，不当作代理 Forwarded 已验收。
  - [x] 只读查看代理最新 2026-09-29 07:47:57 的同一请求详情：入站仍是旧部署的 `thinkingLevel: medium`，Forwarded 仍把它变成 `thinkingBudget: 32768`。可确认现有请求没有达到目标；尚不能从 medium 推断部署后 low 的代理行为，须按同一请求三段链路复核。
  - [x] 为排除 medium 旧配置因素，使用服务器当前 Gemini 连接凭据发送一条无 QQ 副作用的合成 `thinkingLevel: low` 小请求，代理 07:58:53 返回 200，入站显示 low，Forwarded **仍是 `thinkingBudget: 32768`**。代理设置页同时显示 Flash“默认模式（官方自适应）”已选且文案称不注入预算；实际转发与设置文案矛盾。Yuki 侧 low 修复仍须上线，但代理固定预算是独立未解的阻断，不能勾整项或推断 Google 实际收到 low。
  - [x] 生产 WebUI 热保存现有 Gemini 连接为 low，回执 `applied` 且 Bot 容器未重启；后续 Yuki 出站 trace 均为 `thinkingLevel: low`。Antigravity Manager v4.8.4 最小补丁已单服务部署并通过健康检查；把同一条旧失败请求直接重放到生产代理得到 HTTP 200，Forwarded 保留 LOW 且没有 `thinkingBudget`，`functionCall` 保留原签名。该重放不执行 QQ 工具，不代替自然消息验收。
  - [x] 当前生产代理的合成 medium/high 小请求分别 HTTP 200，Forwarded 保留 MEDIUM/HIGH、均无固定 `thinkingBudget`；low 的同报文生产重放也已通过。未修改 Yuki 当前 low 配置。
  - [ ] 以自然 Yuki 请求核对代理入站/Forwarded 继续保留 low；验证显式改为 medium/high 后的热配置边界与原有在途请求隔离。
- [ ] **13. 高危：取消自动注入旧外部事件和记忆事实**：用户提供的一条外部 GitHub 事件唤醒输入共约 7,561 字符，其中动态资料约 7,161 字符；`context.people_and_scene` 约 6,207 字符，包含 4 条群记忆、4 条自我记忆和 10 条外部事件；8 条记忆均标为 `lexical_match`，部分与当前分支创建事件明显弱相关；`runtime.short_state` 还带了 3 个空文本槽。用户决定将自动附加的旧外部事件摘要和自动召回记忆事实全部移出本轮运行资料，记忆仅在 Agent 判断当前问题依赖过去事实时按意图调用记忆检索工具；已知事实 ID 可用 `get_memory_fact`；`search_chat_history` 查询聊天账本，不等于长期记忆检索。保留当前触发事件、必要的当前场景身份、已确认交付回执及非空短期状态；不删除持久记忆或历史账本。实施时核对所有正常聊天、SELF、外部唤醒和 Work 入口，确保工具仍可读且权限/回执不退化；比较模型是否主动补查、错误率、输入 Token 与缓存。当前本地已切断自动召回与旧外部事件追加；真实请求对比未做。
  - [x] 正常聊天、SELF、外部唤醒和 Work 共用的上下文组装不再预取旧事实，也不追加旧外部事件摘要；`short_state` 只向模型注入非空槽。外部唤醒定向测试及短期状态测试通过。
  - [ ] 研究并实现模型侧单一 `search_memory` 检索入口，收拢现有 Person/Group/SELF 三个列表工具；无目标提示时搜索调用者当下全部有权读取的长期记忆，不额外限制在当前人、群或最近场景。按需检索和多次补查由主 Agent 发起，后端完成授权范围过滤、消歧与全局排序；资源截断必须明示，不能静默缩小范围。保留按 fact ID 精确读取和证据读取的独立语义；词法与向量保留独立候选通道，以中文真实样本校准高精度筛选。旧工具引用、稳定工具合同、子任务、观测与回执需一起迁移。具体工作包、默认授权范围、降级与验收见 [search_memory 按需检索任务书](../architecture/Yuki-search-memory-taskbook-2026-09-29.md)。本地实现已覆盖 canonical 全局候选和插件权限，仍待中文真实标注集精度、模型回放与上线验收，不能勾整项。
    - [x] 本地新声明只暴露 `search_memory`，旧三个工具仅留历史执行兼容；无目标枚举所有可解析的获准历史人物/群和当前可见 SELF，不做前 N 目标截断。跨历史群与陌生人拒绝定向测试、Ruff/mypy 通过；当前回执明确 `truncated=true`、`exhaustive=false`。
    - [x] 无目标查询改为 canonical SQL 授权与全局 FTS/向量候选池，不按 owner 截断；覆盖无活跃 QQ Binding 的历史 owner；Person-only/Group-only 插件可用同一工具且由后端限制 scope。跨 owner Top-1、无 Binding、disabled space、无关 owner 与插件授权的定向 4 项及 Ruff/mypy 通过；候选预算截断会报告 `truncated=true`、`exhaustive=false`。
    - [x] 详情工具 `get_memory_fact` 现与无目标搜索共用当前 `AuthorizedMemoryScope` 的 SQL 授权，修复 SELF 用已知 fact ID 读取同群个人 PersonGroup、group-only 插件扩大 PersonGroup 的边界；SELF 当前群 Group 仍可读。授权负例与 Ruff 通过，生产未改。
    - [x] 对上一轮 SELF 6+2 以外的生产 Person 事实做只读独立探针：固定种子抽 8 个主体，剔除不合格生成后保留 7 个有答案、7 个无答案问句；有答案直接证据 Top1/Top10 均 7/7，无答案也全有候选。单轮模型看前 5 候选时有答案 7/7 回答、无答案 7/7 弃答，未复现编造。问题生成、全主体事实支持校验与回答均用同一 DeepSeek 模型，非人工独立金标；只覆盖 Person-only 小事实集和检索核，不算完整主 Agent 回放。
    - [ ] 用独立人工中文金标校准高精度，并回放模型能否主动检索、补查、正确处理歧义和资源截断。
  - [ ] 核对并改进历史聊天成本：同一发送者相邻事件在本地投影中可合并文本，但 Gemini 适配器把相邻 `user` 消息合为一个 `contents` 条目时只是延长 `parts` 数组，没有合成一段逐行紧凑文本。用户旧抓包有 56 个 `contents`（25 user、31 model），user 内共 202 个 `text` part、文本约 25,938 字符；其中一个 user 条目有 64 个 part。DeepSeek Responses 把每条投影后的消息写成独立 `input` 项，DeepSeek Chat Completions 把每条写成独立 `messages` 项。系统说明约 6,218 字符，114 项工具声明的 JSON 约 61,993 字符，不能把高输入量全归咎于历史。当前同一发送者/五分钟的历史已逐行合成一条模型消息并保留内部事件 ID；100 条同人合成样本的 Gemini JSON 4,954→1,786 字符，DeepSeek Responses 6,636→1,785；交替发送者不合并以保留轮次语义。尚需真实 `countTokens`/usage、模型理解和缓存对比，不能以 JSON 字符节省当作 Token 节省。Gemini 连续 user 合并会改变末尾 Content 对象，需在缓存审计中核对；DeepSeek 线上逐字请求尚无本轮抓包。
  - [x] 用 18 条同发送者、保留内部事件 ID 与发送者的合成同结构样本，经生产连接做无 QQ 副作用 A/B：Gemini GenerateContent 输入 901→578 Token（−35.8%），DeepSeek Responses 与 Chat Completions 均 722→484（−33.0%）；四项目标事实在三种协议的新旧格式均被正确识别。DeepSeek 已报告缓存同时 512→256，未命中输入由 210 增至 228；单次输出 Token 有波动，不足以证明净费用降低。该试验未改代码或开发约束。
  - [ ] 跨 Provider 完整验收与约束：以真实生产工具合同和重复会话链核对 Token、缓存、工具轮次/回复关系、输出质量及实际费用；确认紧凑逐行投影有净收益且不损坏内部 ID、发送者和工具语义后，才把通过验证的历史投影与 Provider 适配责任写入 `docs/architecture/development-contract.md`。

- [ ] **14. 记忆 Embedding 默认启用、管理页开关与明确降级**：默认请求启用独立 Qwen DashScope Embedding；缺少地址或密钥时不阻止 Bot 启动，检索退回 FTS，并在记忆页写明 `not_configured`、本地覆盖量、失败任务与待重启状态。管理员可在记忆页保存全局启停，原配置回执和版本围栏保持不变；旧部署显式 `false` 优先于新默认值。代码已部署，生产记忆页显示 1,158/1,158 活跃事实覆盖、0 待办/失败；隔离缺凭据/超时/503 都保留 FTS 并标记非穷尽。生产断网、重启后开关生效及人工金标精度仍待验收。
  - [x] 默认启用意图、无凭据降级、全局 restart-required 配置和管理页操作已实现；配置单测 32 项、Control Plane 单测 11 项、记忆页 3 项、前端构建和 Ruff 通过。
  - [x] 只读生产基线确认 `MEMORY_EMBEDDING_ENABLED=true`、Base URL 与 Key 非空；生产库当前 1,158 条 active 事实均有最新 profile 向量，1,854 个向量任务均为 done，无未完成任务。只读取布尔值和计数，未输出凭据或事实正文。
  - [x] 生产 WebUI 记忆页只读验收启用、已配置、1,158/1,158 覆盖、0 待办/失败，并提供“关闭向量检索”管理动作；使用服务器现有凭据的合成中文语义探针召回 5/5。未点击停用开关，故障降级和真实无答案精度仍未验收。
  - [x] 隔离临时 SQLite + MockTransport 验收缺 provider、Embedding 超时及 HTTP 503：三种情况均保留同一获授权 FTS 事实，语义状态分别为 `not_configured`、`embedding_timeout`、`embedding_provider_unavailable`，且 `exhaustive=false`；管理接口区分当前运行配置与保存关闭但待重启状态，定向探针通过。未改生产配置或发送 QQ。
  - [ ] 生产断网与重启后开关生效未实测；Embedding 地址和 Key 仍由服务器启动环境提供，完整的 WebUI 凭据接入需沿私有密钥文件和权限边界设计，不能写入普通配置覆盖表。

- [x] **15. 高危：近期对话末尾工具回执后出现“模型请求或功能配置不兼容”**：旧生产 14/14 条 Gemini 3.8 Flash `thought_signature` HTTP 400 的入站最后一个 `contents` 均为 `functionResponse`，倒数第二个均为带签名的 `functionCall`，其余位置为 0，与用户观察一致。AGM v4.8.4 把签名移到前面的普通 text part 并在错误重试时清除，导致上游 400；最小代理补丁保留当前轮首个函数调用的签名和客户端 low 档。旧失败报文在修补版生产代理重放 200；补丁部署后两条自然 QQ 轮次各一次 `send_message`，随后 Gemini 最终模型请求成功并 `turn_end`，没有通用不兼容错误或重复工具执行。该验收覆盖已观察到的末尾路径，未来不同形态仍按失败回执继续追踪。
  - [x] 用生产日志和代理 `proxy_logs.db` 仅提取状态、结构与签名存在性，未输出凭据、签名、聊天正文；同一请求核对 Yuki 400、代理入站、最终 Forwarded 和上游错误。Google 官方要求当前轮首个 `functionCall` 的签名在原位置回放，代理 v4.8.4 自称有签名修复仍不能替代本机实测。[官方签名规则](https://ai.google.dev/gemini-api/docs/generate-content/thought-signatures)、[代理版本变更](https://github.com/lbjlaq/Antigravity-Manager/blob/main/CHANGELOG_EN.md)。
  - [x] 对照 Antigravity Manager v4.8.4 源码和 v4.8.5-beta.10 源码：新版已改为 Gemini 有 `functionCall` 时以首个调用为签名锚点，说明 v4.8.4 的“首个非思考 part”规则过宽。升级新版同时涉及模型映射、思考预算和所有协议出口，不能未回归就替换生产；优先在隔离构建中只修 Gemini 签名锚点，并用生产报文的**脱敏结构**回放。
  - [x] v4.8.4 的最小补丁将有 `functionCall` 的轮次以首个调用为签名锚点，并保留客户端指定的 low 档；4 项定向 Rust 测试和隔离 canary 均通过。原生产失败报文在旧代理 canary 复现 HTTP 400，在修补版 canary 和生产代理重放均为 HTTP 200；生产只更新 Antigravity Manager 一个服务，健康检查通过，账号与模型映射数未变。首次旧版上游请求的完整字节并未持久化，根因由源代码转换规则、代理错误与同报文 A/B 重放共同支持。
  - [x] 无 QQ 副作用地把同一末尾失败报文重放到当前生产代理，再用 Yuki 当前 `GeminiProvider._parse` 离线解析原始 HTTP 响应，得到 completed、正文和可保留续接状态，未触发本地工具调用；临时响应已删除。这验证 Yuki 适配器能消费修复后的回包，不等于真实 QQ 完整轮次成功。
  - [x] 自然 QQ 验收：2026-09-29 02:14 与 02:28 UTC 两轮各只有一次成功的 `send_message` 工具执行，分别记录 3 个与 2 个不同的投递事件；工具结束后第二次 Gemini 调用均成功，轮次分别以 `turn_end` 完成，未出现 `turn_error` 或模型不兼容状态。两轮发生在代理补丁部署之后，且无主动测试 QQ 消息；中间轮与跨 Provider 换链继续由定向回归覆盖。

官方依据：[Gemini 缓存](https://ai.google.dev/gemini-api/docs/generate-content/caching)、[Claude 工具缓存断点](https://platform.claude.com/docs/en/agents-and-tools/tool-use/tool-use-with-prompt-caching)、[OpenAI 前缀缓存](https://developers.openai.com/api/docs/guides/prompt-caching)、[DeepSeek 上下文缓存](https://api-docs.deepseek.com/guides/kv_cache/)。这些机制的门槛、TTL 和费用不同，不能要求相同命中率。

## 本轮分工结果

- 已集成：Provider 原生搜索及工具过滤、用量图表与缓存分层、`request_tools` 删除、过时工具选择配置删除。
- 已修复并定向验证：子任务固定工具合同的 5 项同源回归；媒体准备与热切换的竞争边界；本轮状态和收发消息的具体展示。最新后端全量 1,641 项通过、7 项跳过，其后的附件修复另有 5 项定向通过；前端 84 项、构建与格式通过。
- 高危优先处理：Gemini 等 Provider 缓存命中偏低与统计分母不一致，先用请求和 usage 证据定位，再做供应商专属前缀优化。
- 主任务继续：合并回归结果、更新清单、提交和后续上线状态核验。

每次更新勾选时，在本文件记录验证和代码位置；PR、合并与生产验收分别写明，避免把本地通过写成已上线。
