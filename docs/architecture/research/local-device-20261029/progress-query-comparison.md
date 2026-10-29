# 主动进度查询：实现来源、公开项目对照与最小修订

日期：2026-10-29

性质：T14 的补充调查。三名同级子代理并行查 Yuki Git 来源、Memoh 及其他公开项目，主审复核关键源码和本机已生成的 Codex 0.165.0 schema。这里只修改文档；没有实施查询修复、运行第三方服务或模型、访问用户本机 Agent，也没有再次查询生产私聊。

## 1. 全量历史读取来自哪里

现行 Yuki 普通 `get` 不默认拉完整历史；**显式 `include_history=true` 会从第一页遍历到最后，并把每页 full items 合成一个完整数组**。一次模型询问可以因此触发多页 RPC 和百万字符结果，之后转存 Artifact 只改变结果呈现，没有省去前面的读取、传输和数组组装。

| 提交及时间（UTC+8） | 已核事实 |
| --- | --- |
| `2428ace998e8d051fb26227469a8ab329b72c3e5`，10-23 03:50 | 初版原生驱动直接调用 `thread/read(includeTurns=true)`；没有请求历史时在返回前移除 turns，仍已读取历史 |
| `9c67628064b83cdb2b3733102b61589bc7927038`，10-23 18:37 | `thread/read` 改为不带 turns，状态查证仍按需读取 turn 资料；显式历史读取改成 `_turns(sortDirection=desc)` 迭代全部页并组装数组 |
| `dad3c456`，10-23 19:23 | 上述后续修复通过 [PR #502](https://github.com/YuanYeYouTao/Yuki/pull/502) 合并到 main；全量历史组装保留到本次调查基线 |

原始代码见[倒序迭代](https://github.com/YuanYeYouTao/Yuki/blob/9c67628064b83cdb2b3733102b61589bc7927038/packages/yuki-agent-connector/src/yuki_agent_connector/drivers/codex.py#L321)及[完整数组组装](https://github.com/YuanYeYouTao/Yuki/blob/9c67628064b83cdb2b3733102b61589bc7927038/packages/yuki-agent-connector/src/yuki_agent_connector/drivers/codex.py#L740)。Git author/committer 均署名用户“遠野”，仅凭提交署名不能还原当时具体由哪个人或代理编写；可确认它属于此前原生 Agent 接入及 #502 那轮，而非本次文档修订新增。

上层已经将 `native_cursor` 传为 Connector 的 `arguments.cursor`，但 Codex `get` 没有消费它，内部 `_turns` 每次从 `None` 开始；`list` 才使用该游标。这里是没有接完的真实分页能力，不是上游要求客户端必须拉全历史。[当前服务传参](../../../../src/yuki/external_agents/service.py#L286)、[当前驱动](../../../../packages/yuki-agent-connector/src/yuki_agent_connector/drivers/codex.py#L339)

历史现场已见[私聊取证](progress-query.md)：Main 从倒序数组的尾部取旧 turn 并描述成最后工作。本次比较不重新宣称已复现当前 main，也不将全部历史误答和 #514 归为同一根因。

## 2. 公开实现对照

固定提交是本轮读取证据的版本，不承诺它们永远是各项目最新版本。其他项目的页大小、保留期和等待时限仅为事实，不成为 Yuki 的任务完成条件。

| 项目 / 基线 | 当前可确认的实现 | 可借用的思路与边界 |
| --- | --- | --- |
| Memoh `bbedfb5f50e41ab42279211a7a57ea657040fa61` | 近期消息取最新一页，返回顺序明确；后台任务状态另查；原工具结果可精确寻址分页 | 分开状态、近期内容与原件读取；其自有任务/容器 ACP 不等于接管用户桌面已有 Codex |
| Codex `a29c7b97e88367044ae84c28c85451ec3e72af9f` | 原生 turn 与 item 分页，明确排序、详略和游标 | 已是 Yuki 的上游，优先接好现有原语；实际 owner 版本及 store 的支持仍须验证 |
| Happy `2cab104da57898360eea11cee423b6a57d9e50d7` | 首次打开取最新窗口；更晚增量与更旧历史有不同方向；连接同步另处理 | 接近本机 Agent 远程桥接，但日志同步不等于业务进度判断，不另抄同步缓存 |
| OpenCode `4b60235a518a0157f282e6fc671b1561e3e04175` | 消息接口单页读取，游标携带原顺序和方向 | 调用者不猜数组最新端；不能据有界响应直接宣称底层磁盘只读一页 |
| OpenClaw `2df9f694f2fe996279f0ef2fce6b3f35ec783f04` | history 给模型最新有界窗口并可续页，附截断/分页事实；状态另查 | 借读取合同和未完整返回的明确表达，不照搬其硬字节门槛或整个会话管理框架 |
| ChatGPT Dot，10-29 官方文档 | 可连接个人电脑、创建或继续本机 Codex 任务 | 产品目标相似；本轮未找到可核查的官方进度查询源码，不推断内部算法 |

### 2.1 Memoh：近期消息、后台状态和原件各有入口

`get_messages` 默认取最近 30 条，实现上限 100；最新查询先取倒序窗口，再将这一页调整为 oldest-first，工具描述直接写出返回方向。查旧页可传 `before`，查确定消息可传 `message_id`，不自动循环耗尽所有历史。[工具与近期读取](https://github.com/felinics/Memoh/blob/bbedfb5f50e41ab42279211a7a57ea657040fa61/internal/agent/tool/history.go#L105)

`get_background_status(task_id)` 直接返回原任务状态、时间和结果。命令任务有 `output_tail`、输出文件和实际退出码；Agent 任务关联子 session，`AgentReport` 为结果字段，不能当作任意活跃 Agent 的实时行动细节。[任务状态投影](https://github.com/felinics/Memoh/blob/bbedfb5f50e41ab42279211a7a57ea657040fa61/internal/agent/tool/background.go#L283)

完整工具原件通过 `ref=<message id>:<part index>` 与 `offset/max_bytes` 精确回读，返回 `total_bytes/next_offset`。Yuki 已有 Artifact，只需借查询语义，无需复制存储。[原件分页](https://github.com/felinics/Memoh/blob/bbedfb5f50e41ab42279211a7a57ea657040fa61/internal/agent/tool/history.go#L291)

其 ACP Codex/Claude 运行于 bot 的容器 workspace，本次核到的自有后台任务及历史查询不能直接证明它支持监督用户桌面原有 owner/session。[官方 ACP 范围](https://docs.memoh.ai/guides/acp)

### 2.2 Codex：上游已经提供分页

`thread/read` 可以只读 metadata，无需 resume。`thread/turns/list` 默认最新优先，可指定 `limit/cursor/itemsView`；`summary`、`notLoaded`、`full` 描述不同内容详略。`thread/items/list` 可限定一个 turn 再分页，默认方向是 asc，与 turns 默认 desc 不同，不能混淆。[官方读取合同](https://learn.chatgpt.com/docs/app-server#list-thread-turns)、[固定 turn 参数](https://github.com/openai/codex/blob/a29c7b97e88367044ae84c28c85451ec3e72af9f/codex-rs/app-server-protocol/schema/json/v2/ThreadTurnsListParams.json)

本机已生成的 0.165.0 schema 也含 turns 的 limit、cursor、itemsView、backwardsCursor 及 items 分页参数，说明这些不是只能在未来版本设想的接口。schema 存在不证明每台设备/历史 store 已支持，尤其 item 分页的真实可用性应在 T01/T14 核验，不自动另建兼容或后备实现。[固定 item 参数](https://github.com/openai/codex/blob/a29c7b97e88367044ae84c28c85451ec3e72af9f/codex-rs/app-server-protocol/schema/json/v2/ThreadItemsListParams.json)

### 2.3 Happy：最新窗口与日志接续分开

App 初次访问走最新页，并分别保存最大/最小 seq，用于后续增量和旧页读取；源码注释提到旧实现从 seq=0 拉完所有页，长会话因此卡住。当前另有后台历史预取，所以不能说它绝不预取。[App 同步实现](https://github.com/slopus/happy/blob/2cab104da57898360eea11cee423b6a57d9e50d7/packages/happy-app/sources/sync/sync.ts#L2261)

v3 API 的 `after_seq` 用 ASC 读更晚消息，`before_seq` 用 DESC 回看，取 `limit+1` 后返回一页及 `hasMore`。省略方向默认从 `after_seq=0` 开始，调用者仍须选择正确入口。[v3 消息查询](https://github.com/slopus/happy/blob/2cab104da57898360eea11cee423b6a57d9e50d7/packages/happy-server/sources/app/api/routes/v3SessionRoutes.ts#L90)

CLI 的 `while(hasMore)` 从 `lastReceivedSeq` 补齐和路由消息，服务连接接续，不能复制到每次业务进度询问。`session-alive/thinking` 是临时活动信号，也不能回答具体完成了哪项工作。[CLI 接续与活动](https://github.com/slopus/happy/blob/2cab104da57898360eea11cee423b6a57d9e50d7/packages/happy-cli/src/api/apiSession.ts#L604)

### 2.4 OpenCode 与 OpenClaw：让调用者知道读到了哪一页

OpenCode 消息 handler 默认 50 条和 desc，游标含消息 ID、原 order 及 previous/next 方向，直接调用分页读取后返回该页，没有在 handler 内循环组装全历史。[固定源码](https://github.com/anomalyco/opencode/blob/4b60235a518a0157f282e6fc671b1561e3e04175/packages/server/src/handlers/message.ts#L12)

OpenClaw 的 `sessions_history` 合同明确最新窗口及向旧页续读；模型结果保留分页和是否截断等事实，`includeTools` 单独决定工具结果。可借明确合同；代码还有内容裁切和字节限制，不能把这些全部照搬成 Yuki 的业务限制。[读取合同](https://github.com/openclaw/openclaw/blob/2df9f694f2fe996279f0ef2fce6b3f35ec783f04/docs/concepts/session-tool.md#L125)、[实际结果装配](https://github.com/openclaw/openclaw/blob/2df9f694f2fe996279f0ef2fce6b3f35ec783f04/src/agents/tools/sessions-history-tool.ts#L475)

该合同另区分绑定外部 CLI transcript 的合并快照：普通数字 offset 分页不适用，`hasMore=false` 也不保证历史完整。因此这里能借的是有界窗口和不完整事实的表达，不能把它当作已经实现外部 Codex 原生分页的证据。[外部历史边界](https://github.com/openclaw/openclaw/blob/2df9f694f2fe996279f0ef2fce6b3f35ec783f04/docs/concepts/session-tool.md#L125)

### 2.5 Dot：功能可参考，内部实现不能补写

按本会话上下文，本次将 Dot 理解为 ChatGPT dots。官方文档明确其云端与本人电脑的连接、创建/继续本机 Codex 任务等产品行为。[官方说明](https://learn.chatgpt.com/docs/dots/computers-and-apps)

本轮没有找到该进度查询的官方公开源码。名称相近的独立 OpenDot/open-dot 项目不是其官方实现，不能借其代码推断 Dot。Yuki 的分页与查询选择应据已核源码决定，不以 Dot 的实际体验代替算法证据。

## 3. 对 T14 的具体修订

1. **删除进度路径的全量组装。** 原生历史一次返回调用者所需的一页和真实 next cursor；不在该次调用内自行翻到结尾。原参数语义、类型和实际消费者同步更新，不能只在文档里写分页，驱动仍把游标吃掉。
2. **当前与最近行动直接可读。** 问当前进展时选原 session 的活跃/最近相关 turn，取得必要 items；空闲时仍可查最近已结束轮。明确查旧 execution 时继续查旧轮，不偷换为最新轮，不用标题、在线或 completed 推定实际工作。
3. **详略按需，连单个大 turn 也不盲拉。** 利用上游摘要/条目分页等真实能力；当前问题无需的历史和大工具原件延后按 ID 读取。原生显示摘要不是新的 LLM 全史摘要任务，不能为查询再购买一次后台总结。
4. **保原确认与恢复。** 原 operation/run 查询仍确认原回执；专为定位指定旧执行、恢复原观察使用的已有扫描不因“取消全量进度读取”被一刀切删除。服务器事件 cursor、原生历史 cursor、Artifact offset 保各自用途和原排序。
5. **结果经过投影仍能解释。** ID、事实时间、状态、历史方向及直接细节/续读入口在短结果中保留；完整原件继续沿现有 Artifact。保原数组位置，不在通用 Artifact 中写死 Codex 或翻转所有数组。
6. **复用原执行链。** 不加进度数据库、同步缓存、监督器或定期全史摘要；原 Work 私有证据按真实读取授权使用。其他项目的页大小不照抄为任务门槛。

必要验收补充到 V28–V32：没有请求续页时不自动耗尽后续页；续读实际消费原生游标且不漏/串页；大单轮原件可按需读取；指定旧执行仍能跨页定位；短结果中的排序与事实时间可见。原历史误读、当前接口回归和自然回答准确性仍分别记录，不能只通过静态 prompt 检查宣称修复。

## 4. 收益与代码量判断

原实现显式读历史的成本随全部 N 轮内容、P 页 RPC 增长；调用内保留完整数组，后续 Artifact 不消除该成本。接成按需分页后，Connector 本次读取和传输量随实际所需页/条目变化，通常不再必然随完整历史增长；不能据此宣称上游磁盘索引或扫描成本为常数，也不能承诺每次永远只需一条 RPC。

这是能通过删除急切聚合、接好已有参数来改善的问题。结果页、续读投影与必要回归仍有接线代码，总代码行数不保证减少；无需新增依赖、数据库或第二套恢复链。未实测延迟、CPU、RSS、流量和最终 diff，不填写节省百分比或伪精确行数。

本轮成果是公开实现对照和任务书修订。具体查询实现、跨平台/版本验证、模型实际观察及实现后复审仍为待实施交付。
