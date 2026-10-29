# 进度查询续审：分页之外的目标、内容与时间语义

日期：2026-10-29

性质：T14 的只读续审，记录分页之外的查询合同和理论缺口。基线沿主任务书；三名同级子代理分别查目标身份、时间并发、Claude/ACP/Work，主审查内容投影并做一次离线纯函数样例演算。没有连接生产或用户 daemon、调用模型、修改运行时或运行完整测试。

此前已证实的历史误读见[私聊取证](progress-query.md)，实现来源和其他项目做法见[公开实现对照](progress-query-comparison.md)。本文的源码事实及理论推演不能冒充新的线上复现，也不用于推算误答频率。

## 1. 是否要重写工具

需要实质修订 `external_agent` 的查询合同与实现，不需要推倒一个工具、三套 driver、设备身份、WSS、journal 或 Main/Work 重建。现在的问题跨越目录选目标、查询资源、读取内容、分页和短结果投影；只改 `_turns` 循环或加一句提示不能覆盖。

优先复用原查询和真实能力，删错误的全量组装及状态推断，补没有接通的原参数和事实读取。跨 driver 统一的是查询意图、来源和不完整事实，不是宣称每种上游都有相同历史 API。

## 2. 源码已确认的缺口

### Q01：目录第一页不等于当前行动，标题和 cwd 也不是目标身份

Codex list 只传 `cursor/limit/useStateDbOnly`，未指定排序键和来源集合；本机 0.165.0 schema 的默认是创建时间排序及交互来源。一个很早创建、刚恢复活跃的会话可能在后续页；未覆盖的来源也不能被说成不存在。目录投影只保 `id/label/status/cwd`，不保原生已有的近期时间、来源和 parent/fork 线索。[目录查询](../../../../packages/yuki-agent-connector/src/yuki_agent_connector/drivers/codex.py#L697)

最小处理：已有原 Work/session ID 就按原 ID 查，不重新凭标题选目标。确需发现当前活动会话时，明确列表覆盖及排序，读取必要的原生分页/过滤；保足够的时间、来源和分叉线索。多个真实候选才消歧，不因信息少就默认第一条，也不为了查询遍历所有设备和会话。

### Q02：历史内容在 Artifact 之前已被裁过，后续再读 Artifact 取不回被删字段

Codex 的 `item_view` 只保命令、路径、状态、退出码等少量字段，去掉命令输出；MCP 条目不保结果和错误正文；未列出的 item 类型返回 None。`turn_view` 不保 `itemsView` 或遗漏范围。虽然上游读取请求了 full，云端得到的仍是投影。[条目与轮次投影](../../../../packages/yuki-agent-connector/src/yuki_agent_connector/drivers/codex.py#L217)

Claude 的历史内容也只保 `type/text/id/name/tool_use_id`，去掉 `tool_use.input`、`tool_result.content/is_error`。工具名只能说明曾调用哪个工具，不能证明具体修改、输出或成功原因。[Claude 历史投影](../../../../packages/yuki-agent-connector/src/yuki_agent_connector/claude_inspect.py#L49)

裁减本身不等于错误；问题是必要行动结果没有可按原 ID 追读的明确入口，且读取范围不清楚。最小方向是保所问行动的必要结果/错误，或按原 item/消息 ID 回读当前来源获准读取的必要原件，并明确这是投影。不能把“full”当完整回执，也不为补齐内容恢复私有推理、opaque 或无关全部字段；未知 item 不应直接令整次查询失败。

离线样例仅执行当前源码中的三个纯函数 `item_view/turn_view/native_status`，通过 AST 提取，不导入或启动 Connector。输入三项：失败命令含 `aggregatedOutput`、失败 MCP 调用含 error、一个 review item；输出保命令退出码和失败状态，丢输出/错误正文，三项变两项，`itemsView=full` 不再出现。该结果证明投影行为，不证明模型必然将失败说成成功。

### Q03：Claude 首次历史查询取得最早 30 条，不是最近 30 条

Yuki 使用 `offset=0, limit=30`。锁定 SDK 0.2.172 的 `get_session_messages` 明确按时间正序返回，offset 从头开始；因此长会话的首次历史查询会得到最早窗口。结果又没有排序、后续 cursor 或完整范围说明。[Yuki 调用](../../../../packages/yuki-agent-connector/src/yuki_agent_connector/claude_inspect.py#L44)、[锁定 SDK 合同](https://github.com/anthropics/claude-agent-sdk-python/blob/v0.2.172/src/claude_agent_sdk/_internal/sessions.py#L1284)

还有性能边界：该 SDK 先读取完整 JSONL、构建 parentUuid 会话链，再切 offset/limit。只返回 30 条不能证明磁盘/CPU 只处理 30 条，反复逐页调用还可能重复解析。[SDK 实际读取](https://github.com/anthropics/claude-agent-sdk-python/blob/v0.2.172/src/claude_agent_sdk/_internal/sessions.py#L1333)

最小处理：按实际 SDK 能力选最近相关材料并明确读取范围，不能套 Codex 的倒序。核真实成本后决定是否需调整原读取方式；不为追求常数复杂度先建第二份全史索引、常驻缓存或后台摘要。现有 profile 隔离进程有真实用途，不能改成并发修改全局环境变量来省一次启动。

### Q04：ACP 当前 get 没有历史能力，统一 get 成功不等于都能解释行动

ACP get 只读取本进程已附着 session 的 execution/status/pending，忽略 `include_history/cursor`；未附着时明确 `session_not_loaded`。原 plan/tool 更新已走 progress 事件，可以按原事件及回执查询，但当前 session get 不给这些行动内容。[ACP get](../../../../packages/yuki-agent-connector/src/yuki_agent_connector/drivers/acp.py#L463)、[原进展事件](../../../../packages/yuki-agent-connector/src/yuki_agent_connector/drivers/acp.py#L273)

最小处理：按实际 driver 呈现能读什么、读到了哪一层和缺失内容；有原事件就复用，能力缺失按事实说明。不能为了查看历史自动 load/resume、另开会话或启动新执行。固定工具声明不随目录变化重写。

### Q05：状态会被误用来证明具体业务完成

Codex 将原生 idle 映射为 completed、active 映射为 running；目录还未保 active flags。原生空闲不能证明原 Work 目标完成、最后操作时间或没有待用户事项。[状态映射](../../../../packages/yuki-agent-connector/src/yuki_agent_connector/drivers/codex.py#L200)

Yuki 自身 `task_control get/list` 也主要是目标、生命周期、等待和计数。原 Work 接续已有私有执行证据，但全局安全目录不因此变成全部私有回执目录。[Work 查询](../../../../src/yuki/runtime/work_queries.py#L35)、[原工作材料](../../../../src/yuki/runtime/work_session.py#L530)

最小处理：session、turn、原 operation、Work 和送达分别认事实；具体行动用原步骤和回执解释。允许如实说空闲、未加载、等待或未知，不另建完成判断器，也不把 active session 变成所有 Work 无法完成的条件。

## 3. 代码路径可触发、尚未运行复现的情形

### Q06：指定执行的参数可能没有参与实际读取

服务 `_get` 优先选择 operation/run 分支。session-scope watch 同时带同一 session 的旧 execution=A，校验可以通过，但后续只查询 operation journal；session watch 的结果又可能已经指向较新 B。因此“问 A 却返回当前观察 B”是需要受控验证的推演，不能只修分页。[分支选择](../../../../src/yuki/external_agents/service.py#L202)、[监督结果投影](../../../../src/yuki/external_agents/repository.py#L215)

Claude/ACP get 本身也不使用传入 execution_id，而是返回当前 session.execution。A 与 B 不同时服务已有 mismatch 保护；当前 execution 为 None 时则被接受，返回 execution_id 变成 None，缺少指定旧执行未确认的明确说明。[Claude get](../../../../packages/yuki-agent-connector/src/yuki_agent_connector/drivers/claude.py#L349)、[服务结果核验](../../../../src/yuki/external_agents/service.py#L292)

最小处理：确认原操作与查询指定执行的选择语义明确，显式目标不能被忽略或默默降为会话查询；既有 mismatch 保护保留。不要全局禁止所有混参或固定多查一次，按真实调用者修最小分支/合同。

### Q07：几种游标同名，续读旧页可能被当成刷新当前进度

输入 `cursor` 是服务器事件整数，`native_cursor` 是原生 opaque cursor；目录和 operation 输出却都叫 `next_cursor`。跨分支填错可能触发类型或上游游标错误，也可能造成错误续读或空页；跨 operation 复用高整数 cursor 还可能跳过另一操作较早的事件。Codex desc 历史的 next cursor 指向更旧内容；反复用它回答“现在”会越查越旧。[服务输入/输出](../../../../src/yuki/external_agents/service.py#L252)、[原生目录游标](../../../../packages/yuki-agent-connector/src/yuki_agent_connector/drivers/codex.py#L719)

最小处理：返回并在短结果中保留游标的原资源、方向及对应续读参数。刷新当前、续读历史、指定旧执行分别表达，不改旧 Artifact offset，不新增游标账本或时效阈值。没有下一历史页不等于活跃轮没有新内容。

### Q08：多次读取没有共同瞬时快照，晚收到也不等于刚发生

Operation get 先取/查证 view，再独立取 event_page；期间新事件可以入账，形成顶层状态与事件页来自不同读取时刻。Codex session get 的 metadata、活跃轮和历史也由多次 RPC 取得；中间可能开始新轮次。[服务读取顺序](../../../../src/yuki/external_agents/service.py#L221)、[原生读取顺序](../../../../packages/yuki-agent-connector/src/yuki_agent_connector/drivers/codex.py#L744)

通用事件不自动携原生发生时间；服务器已有 created_at 是入账时刻，event_page 又没有给出这个时间。断线积压后，刚收到的可能是旧行动。[事件入账](../../../../src/yuki/external_agents/repository.py#L652)、[事件页](../../../../src/yuki/external_agents/repository.py#L861)

最小处理：保原 ID、sequence、原生已有时间和读取范围；必要时复用短只读事务取得服务器内一致视图，但不跨外部 RPC 加全局锁或长事务。查询观察时间可作响应事实，不能冒充发生时间，不为承诺绝对瞬时最新建立快照数据库。保留 Journal 的 snapshot_sequence 和服务端 state sequence 乱序保护，不重复发明版本账本。

## 4. 应保留的行为及受控验收

原设备/Person 授权、稳定内部 ID、目标 mismatch、连接替换、原 journal、提交后 ACK、回执去重和未知查证均有真实职责，不随查询修订删除。查询 unknown 时恢复原观察、写回查证结果，不等于重做业务效果。

真人进度追问仍可能合法命中原 conversation wait。查询动作不重派效果，不代表整条真人输入必须让 wait 永远保持不变。实际命中时按原匹配交付，回答后继续原监督目标；与 T13 联合回归，不另 accept、重置预算或把回答结束当持续目标完成。

T14/V28–V32 应按已有 fixture 补以下真实反例，不另建测试框架或增加固定检查频率：

- 已有长会话重新活跃、同名同 cwd 或分叉会话，目标仍认原 ID 和真实目录范围。
- 最新轮含失败命令、MCP/Claude 工具结果及未完整投影项，能查必要事实或明确缺失，不仅凭工具名猜进展。
- Claude 超过一页历史时取到最近相关材料，真实测量 SDK 全文件解析成本；ACP 明确状态与历史能力差异，不因 get 触发执行。
- 指定旧 A、监督最新 B，以及 driver 返回 execution=None，分别验证目标与未确认事实。
- 刷新当前与续读旧页分开，游标不跨资源误用，短结果保方向和读取范围。
- 查询过程中开始新轮、迟到事件及断线重连，时间和状态按原来源解释；追问后原监督按合同继续。

当前结论是查询层还需补齐，不是需要重写所有工具。以上除纯投影样例外均为源码及理论审查，完整功能、模型实际回答、跨平台和线上验收未执行。
