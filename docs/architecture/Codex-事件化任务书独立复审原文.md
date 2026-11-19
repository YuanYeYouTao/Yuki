> 入库说明（2026-11-18）：本文是对[第一阶段任务书](Yuki-外部Agent操作事件化与防卡改造任务书-2026-11-18.md)修订 1 的复审原文，正文未改。源码链接已由本机绝对路径改为仓库相对路径，行号对应复审时的工作树 `c9f88934`，不对应当前源码；transcript、已安装 SDK 与 Yuki-Desktop 的引用只保留文件名和行号，原文件不在本仓库。

**复审结论：认可把外部 Agent 的原生确认移出 Work 激活，但建议退回修订 1，修改后再作为实施依据。** 现有 journal、事件 ACK、Work 私有输入和完成回执可以复用。任务书低估了三处问题：`send` 本身仍可能占住会话锁，C1/C2 都与当前“一次 prompt 绑定一个原生执行”的合同冲突，控制命令改走事件后，现有代码不会自动把结果结算到原 run。照原文实施，可能出现群仍然不回复、权限交给错误的 Work、或 detach 已成功而 Work 仍无法完成。

本次只读核对了工作树 `c9f88934`、本机安装的连接器及 SDK、事故会话 transcript、连接器 journal、本地留存的现场查询回显，以及相邻 Desktop 仓库。任务书的 `77e3b508` 对应本地保存的 `origin/main`，本地 `main` 为 `8e6594cf`；所核对的外部 Agent、Work 和连接器相关路径在 `77e3b508` 与工作树 HEAD 之间无差异。未连接生产、调用原生 Agent、运行测试或执行现场处置。以下时间沿用任务书的 UTC+8。

## 一、事故原因：哪些已经证实

| 判断 | 复审结果 |
|---|---|
| 第一条 prompt 正常结束，随后出现后台通知 | **有原始证据。** `b39f7ea8` 的 journal 终态为 `completed/end_turn`。transcript 第 993 行记录后台 Bash。更重要的是，**同一个 `task-id=bxogl883w`、同一个 `tool-use-id`，01:35:57 先报告 `stopped`，01:38:41 又报告 `completed`**。第二次通知后确有模型请求、Read 和 Bash。任务书遗漏了这个直接影响 C1 的时序。后台任务发起（本机 Claude 会话 transcript `e4e2d50f-52d8-49ec-817c-4b714add4d7c.jsonl:993`，未入库） · 第一次通知（本机 Claude 会话 transcript `e4e2d50f-52d8-49ec-817c-4b714add4d7c.jsonl:1005`，未入库） · 第二次通知（本机 Claude 会话 transcript `e4e2d50f-52d8-49ec-817c-4b714add4d7c.jsonl:1011`，未入库） |
| Claude 卡在无人收到的权限回调 | **最符合证据的根因，仍属推断。** 原记录止于 Bash `tool_use`，未见匹配的 `tool_result`。代码确实会在没有 operation/watch 时发布零条事件，随后等待 `future`。但本次可见记录没有这一次 `can_use_tool` 原帧或协程栈，无法把“进入了这一行且一直停在这里”列为现场事实。SDK 处理原生撤回、驱动退出时取消 future，因此“永久”也过强。最后的 Bash（本机 Claude 会话 transcript `e4e2d50f-52d8-49ec-817c-4b714add4d7c.jsonl:1021`，未入库） · [发布与权限回调](../../packages/yuki-agent-connector/src/yuki_agent_connector/drivers/claude.py#L104) · [退出清理](../../packages/yuki-agent-connector/src/yuki_agent_connector/drivers/claude.py#L273) |
| 第二条 prompt 只入队，原 Work 被占住 | **入队与阻塞后果有证据，精确等待点由代码推定。** 第 1022 行有 enqueue，其后至文件末尾没有相应 dequeue 或用户回合。处置前的现场查询显示 `8cf9ae9a.result_json=NULL`、`native_execution_id=NULL`、run 为 `waiting`，原 Work 为 `running`，三条群消息和一条子任务结果 pending。应写成“这份 transcript 未见该 prompt 开始执行，回执未知”；不能据缺失记录签发“确定未执行”的可重派结论。入队记录（本机 Claude 会话 transcript `e4e2d50f-52d8-49ec-817c-4b714add4d7c.jsonl:1022`，未入库） · 处置前查询回显（本机 Claude 会话 transcript `2053c8c4-2ee9-486b-aed2-98910b4b275d.jsonl:540`，未入库） |
| 02:26 解除阻塞 | **现场回显支持。** 原 PID 14950 收到 SIGTERM 后退出，连接器 899 仍在。随后原调用得到 `unknown`，四条积压输入被消费，有四条 outbound 聊天记录，之后注册了 `a695a79d`。这支持“SDK 所有者退出使原 RPC 得以返回”，不支持 L1 所写的“WSS 一断开就解卡”。同设备其他会话可工作，也只能排除设备级全面中断。四条 outbound 记录可证明 Bot 侧记录了发送；V1 若要求平台送达，仍应核对对应投递回执。进程回显（本机 Claude 会话 transcript `2053c8c4-2ee9-486b-aed2-98910b4b275d.jsonl:545`，未入库） · 操作及输入回显（本机 Claude 会话 transcript `2053c8c4-2ee9-486b-aed2-98910b4b275d.jsonl:548`，未入库） · 发送与等待记录（本机 Claude 会话 transcript `2053c8c4-2ee9-486b-aed2-98910b4b275d.jsonl:559`，未入库） |

对 L1–L13 的归纳也应调整。L2、L3、L6 及 L13 中“`waiting` 的 execute run 会推迟 Work 的正常 `completed`”有源码支持。L3 的精确范围是 `UserMessage`、`AssistantMessage`、`ResultMessage` 或权限回调。L3a 还低估了后果：旧回合的 Result 到达时，可能把新 prompt **直接投影为完成**。L4/L5 取决于是否有匹配的 watch。L7 的 `execution_mismatch` 是有依据的反事实推断，未尝试取消。L9 的误报适用于 session 查询，operation 查询另走 journal。L10 中 Codex 除 `turn/start` 外还等待 `thread/read`、`resume` 或 `start`。L11 是共享传输没有确认上界，不能推成每个本机动作本身都永不返回。L12 建议写明查询过的日志范围与最后可见记录。L8 的例外及 L13 的退出缺口见下文。[Claude 消息与终态处理](../../packages/yuki-agent-connector/src/yuki_agent_connector/drivers/claude.py#L288) · [两种查询路径](../../src/yuki/external_agents/service.py#L247) · [Codex 派发](../../packages/yuki-agent-connector/src/yuki_agent_connector/drivers/codex.py#L1003) · [Work 完成判定](../../src/yuki/runtime/work_repository.py#L647)

## 二、需要修改的设计

### P0 · 防卡的边界必须落在整个派发路径

`connection.request` 先 `await self.send(command)`，再等待回复 future。§7.1 改用 `await connection.send` 只消除了后半段。`send` 仍等待同设备共享的 `_send_lock` 和 `socket.send_json`，ACK、目录应答等也使用该发送路径。设备不再读取或前一个写入被背压时，Work 依旧可能持锁。另一个直接入口是工具说明推荐的 `get(operation_id)`：只要原操作 pending，它就先做在线 RPC，返回后才读取服务器已持久化的事件。结果是 **权限可能已经入库，查询它的 Main 却再次挂在同一设备上**。按实例 list、按会话 get 也保留在线等待。[传输的两个等待点](../../src/yuki/external_agents/transport.py#L29) · [pending 查询](../../src/yuki/external_agents/service.py#L277) · [ACK 路径](../../src/yuki/external_agents/http.py#L278)

**建议**把可验收承诺写成“已登记的原生改动命令，即使对端不读或不答，也不占用原激活”。由现有传输生命周期承接写入，保留原 ID，实际写前核原来源、当前连接和设备授权；原激活可以从持久事实返回。原操作查询优先返回本地回执、事件及最近确认点，将确需在线取得的原生历史与这条查证路径区分。若选择保留激活内 socket 写入或同步在线读，V3 必须缩到实际覆盖的范围。无需为此增加通用超时或另一套调度器。[派发前二次核验](../../src/yuki/external_agents/service.py#L619) · [共同合同 §5](../../docs/architecture/development-contract.md#L87)

### P0 · C1/C2 都缺少可成立的跨回合归属

服务端只对 `action=watch` 且 `observation_scope=session` 放宽单执行绑定。普通 prompt 已绑定 execution A 后，若按 C1 给自发回合分配 B 并沿原 operation 上报，`_bind_native` 会抛 `external_agent_response_execution_mismatch`。绑定检查早于状态序号过滤。事件事务回滚，服务器不 ACK 并以 1011 关闭，连接器重连后会再发同一条；一个归属错误就可能妨碍同设备后续事件交付。C2 也不能靠删除两行实现：`completed → idle` 只对 `session.watches[op] is None` 生效，普通 prompt 仍会完成；空接收者集合即使去掉过滤也不会凭空产生接收者。[持续观察判定](../../src/yuki/external_agents/repository.py#L54) · [执行绑定](../../src/yuki/external_agents/repository.py#L533) · [事件处理与重连](../../src/yuki/external_agents/http.py#L323) · [Claude 观察者集合](../../packages/yuki-agent-connector/src/yuki_agent_connector/drivers/claude.py#L104)

C1 的另一问题已有本次事故样本：按第一条终态通知清掉关联，会在第二条通知唤起 Bash 前丢失来源。SDK 自己也写明“已知活动任务集合为空”不能证明后续回合结束，且其内部延迟结束逻辑特意排除了可能长期运行的后台 shell。本次恰好是后台 shell。`TaskStarted.tool_use_id` 可缺失；`scheduled-trigger` 是 `task-notification` 的 **subkind**，只看 `origin.kind` 会把定时任务或其他会话消息认成此次委托。transcript 能对上此次 task/tool ID，尚不能证明实际 SDK 流在权限回调发生前总能给齐这些字段。已安装 SDK 的任务跟踪说明（当时连接器 0.7.0 随附的 SDK `claude_agent_sdk/_internal/query.py:870`，本机安装文件，未入库） · SDK 的 origin 定义（当时连接器 0.7.0 随附的 SDK `claude_agent_sdk/types.py:1301`，本机安装文件，未入库）

**建议优先保留原 prompt 与原 execution 的历史绑定。** 后续回合有可验证的因果来源时，显式表达“这次后续执行由哪个原操作引起”；已有会话级 watch 按其原授权观察。证据不足时保留原回合终态和未知归属，由产品决定是否提供本人可见的会话级入口。不要用“同一 session”“最后一个 Work”或后来的 watch 回填历史，更不要在实验失败时自动切 C2。Codex 的空集合分支还负责缓冲已提交 prompt 的早到通知，整段删除既可能破坏关联，也可能扩大对独立本机会话的采集。[现行归属合同](../../docs/architecture/main-agent-runtime.md#L464) · [Codex 缓冲与观察过滤](../../packages/yuki-agent-connector/src/yuki_agent_connector/drivers/codex.py#L675)

### P0 · 回执事件必须结算控制命令与它的目标

目前 `prompt/queue/watch` 才创建 `SandboxTaskRun`。`answer/steer/cancel/detach` 有自己的 operation，但没有自己的 run。同步回复回来后，服务层先记录控制操作，再用 `_control_result` 更新目标；detach 还会写 `detached_at`、保留 `native_status` 并完成原 run。现有事件入口只投影事件自己的 operation，`pending:false` 不进普通私有输入，无 run 也不会产生 completion。因此只把回复封成事件，会丢掉 §7.1 承诺的发起 Work 回执，并可能使 S16 中的原 run 永远留在 `waiting`。此外，清理路径以固定派生 ID 直接 `connection.request`，没有为这条清理命令调用 `prepare`。删除改动类 RPC 应答后，旧清理调用会等不到回复；若原样按其 ID 上传事件，又可能触发 `external_agent_operation_unknown`。[哪些动作有 run](../../src/yuki/external_agents/repository.py#L460) · [控制结果与 detach](../../src/yuki/external_agents/service.py#L652) · [事件输入条件](../../src/yuki/external_agents/repository.py#L815) · [来源清理的独立入口](../../src/yuki/external_agents/service.py#L763)

**建议**从原控制命令解析目标，明确三项各自的落点：控制操作回执、目标操作或 run 的变化、发起 Work 的私有通知。服务器须在可幂等恢复的提交边界完成该事件负责的结算后再 ACK。控制命令可以由另一 Work 或无 Work 入口发起，不能把两个 Work 混为一体。连接器 `journal.complete` 目前在事务里插入并删除占序号的占位行，且可能同时修改原目标。应在同一事务生成可重传的正式事件，提交后唤醒 `_events`；线上包须能区分 `type=event, kind=response` 与现有 `type=response`。只改正常 `execute` 不足以删除旧路径。[journal 事务](../../packages/yuki-agent-connector/src/yuki_agent_connector/journal.py#L215) · [设备发件循环](../../packages/yuki-agent-connector/src/yuki_agent_connector/connector.py#L642) · [服务器分流](../../src/yuki/external_agents/http.py#L302)

### P0 · 一次权限请求必须有可回答的原目标

§2 写“无操作事件被服务器丢弃”不准确。服务器会先插入 **`external_agent_events`**，`operation_id IS NULL` 时才跳过 Work 投影；带有不存在的 operation ID 则报错。现有 session get 查原生驱动，事件分页只按 operation ID，answer 也明确要求原 operation。**保存、本人可发现、仍可回答、原生已收到回答**是四个不同结果。S11 若只要求“可见”，原生仍可能一直等待。已有权限请求发出后才加 watch，也没有自动补发该 pending future 的路径。[事件入库顺序](../../src/yuki/external_agents/repository.py#L755) · [事件查询](../../src/yuki/external_agents/repository.py#L967) · [answer 目标核验](../../src/yuki/external_agents/service.py#L559) · [watch 登记](../../packages/yuki-agent-connector/src/yuki_agent_connector/drivers/claude.py#L443)

原连接也要写清：Claude 的 `connection_id` 属于 SDK `ManagedSession`，不同于 Bot WSS 连接 ID。WSS 重连可以保留原 callback，SDK 所有者重建后，同名会话也不能承接旧 callback。现有请求表主要保存 session、future、consumed；拟放宽“必须是当前回合”时，需要从原事件和实际 pending callback 核对原 owner、session、执行、请求 ID、当前 Person 和设备授权。一个原生请求经 prompt 和多个 watch 扇出时，会有多个服务器事件 ID，原生决定只能消费一次；已撤回、重复回答、换 owner 后迟答均要返回真实结果。`consumed=True` 最多证明回调消费了答案，V2 的“原生继续”需要后续匹配的原生事实。[请求登记](../../packages/yuki-agent-connector/src/yuki_agent_connector/drivers/claude.py#L135) · [回答消费](../../packages/yuki-agent-connector/src/yuki_agent_connector/drivers/claude.py#L487) · [Person 与设备核验](../../src/yuki/external_agents/repository.py#L70)

如采用会话级入口，应先决定哪些已授权管理会话可上传哪些字段、本人如何发现和处理、离线时怎样展示原请求。原生权限参数可以包含命令、路径和正文，扩大上传范围会改变隐私边界。现有事件表没有独立 session 列或 session 分页索引，Desktop 也只从所选 operation 的 events 渲染 `NativeRequest`。是否需要迁移应由实际查询、授权和清退路径决定。S13 同样应修正：每批事件入账前都会 `require_device`，已撤销设备的新上传会被拒绝。应验证**保留撤权前已提交的事实、拒绝撤权后的新上传和新效果**，而非要求服务器接受撤权后到达的一切数据。[事件表](../../src/yuki/external_agents/db_models.py#L77) · [收件授权](../../src/yuki/external_agents/repository.py#L719) · Desktop 权限入口（Yuki-Desktop 仓库 `src/renderer/views/ConnectorTasks.tsx:471`）

### P1 · `submitted`、`unknown` 和 `detach` 必须经得起重启

`prepare` 在实际发送前就同时写 `created_at`、`dispatched_at` 和 `waiting` run。空回执的实际视图现在即可确定：`status=unknown`、`pending=true`、`uncertain=true`、`admitted=false`；模型视图又会滤掉 `admitted` 和 `uncertain`。所以“空回执 + waiting = 已提交”如果指已送到设备，就是错误事实；若仅指 Bot 已登记，则应在工具合同中说清，并保证初次返回、同 ID 重入、冷恢复和 Desktop 查询的解释一致。恢复路径还会从 `SandboxTaskRun` 重建结果，不能只改 `_view`。[登记点](../../src/yuki/external_agents/repository.py#L481) · [当前视图](../../src/yuki/external_agents/repository.py#L354) · [模型过滤](../../src/yuki/external_agents/tools.py#L144) · [冷恢复](../../src/yuki/runtime/work_journal.py#L665)

应区分 Bot 已登记、本地 socket 写完、连接器已记账、原生已排队、该 UUID 已被回放、该执行已开始、该执行有终态。每条证据只支持相应状态。`previous_sdk_owner_not_attached` 是 Claude `recover()` 能主动产生的进度，单凭该字段不能证明进程死亡；本案有独立 PID 退出证据。`get(operation_id)` 在 unknown 时还可能调用 `recover` 并产生新的进度事件。若再把所有进度都改为唤醒条件，会形成“查询 unknown → 新进度 → 又唤醒查询”的反馈。[查证可产生事件](../../packages/yuki-agent-connector/src/yuki_agent_connector/connector.py#L502) · [Claude 恢复实现](../../packages/yuki-agent-connector/src/yuki_agent_connector/drivers/claude.py#L87)

L13 所述完成阻塞成立，但 §7.5 的退出方案覆盖不全。Bot 在 `prepare` 后、设备记账前崩溃时，设备 journal 可以根本没有这个 ID。现有 detach 会先查原 journal，缺行抛 `original_operation_not_found`；设备离线时甚至到不了这一步。另一个细节是现有 `_detached` 会写 `admitted:true`。对未确认 prompt，必须让这一事实明确指向**解除关系的控制操作**，不能使历史看起来像原 prompt 被接纳。建议设计显式解除 **Work 所属关系** 的语义，另行保留原生状态和投递未知；若只允许设备确认后解除，则须写明离线或原记录缺失时 Work 仍不能按当前路径完成。[原 owner 查找](../../packages/yuki-agent-connector/src/yuki_agent_connector/connector.py#L232) · [detach 分支](../../packages/yuki-agent-connector/src/yuki_agent_connector/connector.py#L513) · [服务器 detach 投影](../../src/yuki/external_agents/service.py#L692)

### P1 · 唤醒、群消息路由和并发不能用一句“立即返回”代替

- **L8 有三个分支。** 有显式 wait 时，现行为匹配的 `owned_run` 调用 `_deliver`；没有 binding 的 `waiting_external` 已会排队；`waiting_user`、`suspended` 和终态提前返回。根 Work 的纯 `wait(message)` 专门映射为 `waiting_user`。只删第 414 行条件，S08 的“纯等消息”仍可能失败。注册 wait 时还会回看先到的 pending run 输入，目前这一分支只拿 run ID。权限优先唤醒必须同时处理“先到后等”和“先等后到”。`_deliver` 会消费一次性 wait，普通进度不应无条件打断时间、插件或 `all` 等待。[实时与登记时唤醒](../../src/yuki/runtime/work_wait.py#L297) · [状态分支](../../src/yuki/runtime/work_wait.py#L394) · [一次性交付](../../src/yuki/runtime/work_wait.py#L492) · [纯消息等待](../../src/yuki/runtime/work_control.py#L1120)
- **释放锁后，回复者未必是新 Main。** `stage_work_input` 先匹配显式回复与会话等待，命中即把输入交回原 Work。任务书自己推荐的 `wait(run,message)` 就可能走这一路。没有匹配等待、也没有活动 Work 接管且满足普通入口条件时，才检验新 Main。V1 应验“符合原路由的执行者能够接收并回复，且只有一份投递”，分别覆盖原 Work 与新 Main。[消息路由顺序](../../src/yuki/services/chat.py#L592) · [会话条件匹配](../../src/yuki/runtime/work_wait.py#L684)
- **UUID 修复须覆盖所有写状态的位置。** `_publish` 对非 unknown 负载默认写 `admitted:true`，journal 又保留已确认的 admitted，`completed` 也会设置 admitted。只改 `session.admitted.set_result`，旧回合的权限、进度或 Result 仍可确认甚至结束新操作。同时 `_open(resume=True)` 在注册 session 前等待 `_inspect`，两条并发 resume 可能各建一个 SDK owner；命令独立 task 并不能保证“一个会话一个回合”。应在真实归属确定后再绑定各自的 prompt UUID、回合、回执和控制目标，覆盖两 Work 同时 prompt、resume 以及旧 owner 的迟到事件。[乐观接纳](../../packages/yuki-agent-connector/src/yuki_agent_connector/drivers/claude.py#L104) · [打开会话](../../packages/yuki-agent-connector/src/yuki_agent_connector/drivers/claude.py#L182) · [journal 折叠](../../packages/yuki-agent-connector/src/yuki_agent_connector/journal.py#L39)
- **cancel 必须能证明实际中断目标。** Claude `interrupt()` 面向当前 SDK 回合，返回的是 `cancel_requested`，原生终态稍后才知道。事故中第二条 prompt 如果只在队列里，中断会话可能打断正在等权限的前一回合。不能用一次会话级 interrupt 伪造“已撤回排队 prompt”。同理，S07 的单调序号能保护状态投影，但 `_project` 跳过旧状态后，调用方仍可能为尚 pending 的操作插入旧事件输入；迟到事实可以留档，已解决的问题不应再次要求行动。[Claude cancel](../../packages/yuki-agent-connector/src/yuki_agent_connector/drivers/claude.py#L468) · [投影后写输入](../../src/yuki/external_agents/repository.py#L809)

## 三、Q1–Q13 的建议

以下均为复审建议。原先对方案 C 方向的认可，不代替这些具体决定。

| 问题 | 建议 |
|---|---|
| **Q1 提交与确认** | **同意分离，改动类回执可只走持久事件。** 先定义返回时已发生哪一步，覆盖 socket 背压、崩溃窗口、原 ID 查证和所有改动类调用入口。 |
| **Q2 唤醒范围** | **只扩大需要实际回答的权限／提问。** 普通进度保留在收件箱，沿既有等待合同处理。纯等用户、人工暂停及 `all` 等待被中断后的语义要分别明确。 |
| **Q3 控制类异步** | **同意。** 返回控制 operation ID。分别记录控制是否被接纳、实际影响了哪个目标、目标后来是否结束；无 Work 的控制也应能按原 ID 查询。 |
| **Q4 无主权限** | **支持经授权的本人可见。** 若产品目标包含解除原生阻塞，还须提供绑定原 callback 的回答入口，并能显示已经撤回、已回答或 owner 已失联。仅可见时应如实缩小“防原生卡住”的承诺。 |
| **Q5 冲突或排队** | 已证实原生忙、驱动能在发送前拒绝时，可报 `conflict`；原生有精确入队回执时可报 `queued`；写出后只有沉默则保留待确认。Claude 当前明确不支持精确 `queue/steer`，Codex 有原生队列 ID，两个驱动应保留能力差异。[Claude 能力](../../packages/yuki-agent-connector/src/yuki_agent_connector/drivers/claude.py#L424) · [Codex 队列回执](../../packages/yuki-agent-connector/src/yuki_agent_connector/drivers/codex.py#L1072) |
| **Q6 协议硬切** | **可选硬切。** 代价是整个设备 WSS 不可用，涉及终端、文件、computer、browser。连接器收到 `1008/unsupported_connector_protocol` 后会等待停止。要测两个方向的错配、手动更新后恢复、v4 遗留未 ACK 事件和只有旧 response 的记录。[协议拒绝行为](../../packages/yuki-agent-connector/src/yuki_agent_connector/connector.py#L713) |
| **Q7 本机设备工具** | 同意不改其业务合同。共享传输、journal 和 `_command` 的分流仍须回归，按 `resource` 与原生 Agent 动作区分，避免误删设备工具 RPC。 |
| **Q8 会话锁** | 同意本期保留。修正造成原激活无法退出的外部等待，验收沿现有消息路由完成。 |
| **Q9 C1/C2** | **不接受“C1 实验不可靠就用 C2”。** 优先验证可证因果的后续执行，利用已有显式会话 watch。证据不足时保留未知归属。是否让本人主动选择长期监督，是单独的产品决定。 |
| **Q10 发布与约束** | 支持一次配套发布，Desktop 随附包或更新清单按实际发行链核对。建议先改模块合同；共同约束已有原意图登记、等待释放、来源与回执条文，暂不追加覆盖所有工具的绝对承诺。[共同合同](../../docs/architecture/development-contract.md#L87) |
| **Q11 unknown 收尾** | 支持显式解除所属关系，保留 unknown 和原 ID。明确在线、离线、设备从未记账三种情况的能力；`previous_sdk_owner_not_attached` 本身不触发自动 detach。 |
| **Q12 Desktop** | 若需要本人看见或回答无主请求，列为明确的跨仓交付。可复用现有 `NativeRequest`，但要补会话级发现、原请求目标、状态失效与回答回执；现有 UI 只处理所选 operation 的事件。 |
| **Q13 现场遗留** | **本轮保持现场。** `f195a13e` 是待处理的旧 run；02:27 的快照不能代表现在的服务器状态。若另行授权处置，先只读核原 run、原操作、真实 CI 与 PR 状态，再决定是否仅解除所属关系。恢复原 session、发新 prompt、重跑 CI、合并、上线是分别产生效果的动作，不能从“同意方案 C”推得。 |

## 四、验收应怎样改

现有 `test_external_agent_bridge.py` 使用真实 TLS/WSS、数据库和 WorkControl，但 `NativeComputer` 是可控对端，不能证明生产连接器 journal、Claude SDK 回调、Main 调度和聊天投递已串通。连接器包的 `NativeTransport` 使用真实 SDK 解析，但收到用户消息就立即回放，恰好绕过本案最关键的入队时序。两套夹具可复用，V5 的真实 CLI 时序需单列，付费 V6 继续单独授权。[Bot 桥接夹具](../../tests/integration/test_external_agent_bridge.py#L104) · [SDK 回放夹具](../../packages/yuki-agent-connector/tests/test_claude_driver.py#L38)

| 场景 | 建议验收事实 |
|---|---|
| **S01–S03、V1/V3** | 使用本案“同一任务先 stopped 后 completed”的顺序，加上不回放 UUID、旧 Result 先到、callback 先于回放、设备停止读取四种情况。让对端保持未确认，在此期间证明激活已释放、后续输入能消费，并取得相应发送回执。用事件先后关系取代“正常延迟内”。 |
| **S04–S07、S14–S15** | 分开验证 Bot 登记后发送前、连接器 begin 后原生结果前、journal 写回执后 ACK 前、服务端提交后 ACK 丢失，以及 v4 未决记录升级。核对原生命令次数、原 ID、原预算、原始事件、最终投影和 Work 输入；已有终态能恢复，确无证据才保留 unknown。 |
| **S08–S09** | 权限分别早于和晚于 wait 注册；覆盖无 binding 的 `waiting_external`、纯消息 `waiting_user`、`all`、暂停、终态及 generation 变化。核对普通进度不会消费无关等待，终态只保存允许留存的事实。 |
| **S10–S12、V2** | 分别验证当前回合可精确中断、仅排队无法撤回、两个 watch 回答同一请求、原生撤回、SDK owner 更换、WSS 单独重连、detach 后再来通知。记录“控制已提交”“callback 已消费”“原生继续或实际终态”各自的依据。 |
| **S13、S16** | 撤权前已提交的记录仍可按既有授权规则处理，撤权后的新上传与新控制被拒。分别验证设备在线、有 journal、没有 journal、设备离线时的显式解除；原 prompt 的 `unknown` 和原生待确认事实能在收尾后查回。 |
| **控制与清理补项** | 用 Work A 的 prompt，由 Work B 或无 Work 入口 answer/cancel/detach。再覆盖来源终止、隐私清理与迟到回执。检查发起者、目标 run、completion 各自只结算一次，兄弟会话继续工作。 |

每个 E2E 产物建议保留可重放的输入顺序和生成命令，并按 `operation_id / run_id / execution_id / permission_id / event_id / state_sequence` 串起原生观察、journal、服务端事件、Work 输入、等待和投递回执。**最有判别力的通过条件是：故意让原生确认一直不来，群消息仍能按原路由得到回复；故意让旧回合的权限与新 prompt 交叉，只能回答真正的原请求；故意在 ACK 前后重启，既不重复原生效果，也不丢掉原 Work 的退出路径。**