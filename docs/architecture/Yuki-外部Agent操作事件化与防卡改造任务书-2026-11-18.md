# Yuki 外部 Agent 操作事件化与防卡改造任务书（2026-11-18，修订 6）

**状态**：修订 4 及之前只有调研和计划。修订 5 记录了隔离工作树中的第一批实现（§20，部分内容已被 §21 取代）。修订 6 起，按经协调者转达的用户决定，本改造分两阶段交付：**第一阶段（防卡）** 已提交到分支 `fix/external-agent-nonblocking-dispatch`（Yuki）与 `feat/work-step-kinds`（Yuki-Desktop）并开 PR，**未合并、未上线**；第二阶段留下的范围见 §21.6。第一阶段**不是完整修复**。实施记录、验证、复审与发布边界见 §21。

**修订记录**

| 版本 | 内容 |
|---|---|
| 初稿 | 02:00 前后完成 |
| 修订 1 | 并入初稿复审（§16）和 02:26 现场处置结果 |
| 修订 2 | 新增桌面端任务工具调用展示改造（§17） |
| **修订 6** | 按经协调者转达的用户决定分两阶段交付：记录第一阶段的完整实施、真实 CLI 实验事实、测试与跨版本验收、三路独立复审及处理、file_copy 定性、发布边界与第二阶段清单（§21）；更正 §20.3 第 2 项与 §20.4、§20.5 中已过时的内容；更正 §19、§20.3 中对两次提问结果和代答 deny 的归属 |
| **修订 5** | 逐项核证 Codex 终审（`/tmp/yuki-codex-event-review-final.md`（当时的本机临时文件，未入库）），更正 §17 的 ACL 和 §19 的证据表述；记录在隔离工作树中的实施、验证、设计取舍、剩余范围与发布边界（§20） |
| **修订 4** | 回答远野的追问「不支持直接追加指令的問題，任務書是不是有納入修復」：新增 §19，分层查清 Claude 不能 steer 的根因；新增 L17、Q14–Q16，并在 §9 增加阶段 0 的实验项。修订 3 之前的版本**没有**纳入这一问题 |
| **修订 3** | 逐项核证 Codex 独立复审原文 [Codex-事件化任务书独立复审原文.md](Codex-事件化任务书独立复审原文.md)（核证记录见 §18），据此重写 §1–§13，并修正 §17 中与之相关的部分 |

**修订 3 的主要变化**
- 防卡边界扩大到整个派发路径，包括 socket 写入和在线查询（§7.1）。
- 撤销 C1、C2 两种归属做法，改为保留原绑定、显式表达因果后续执行（§7.3）。
- 新增控制命令结算（§7.2）、权限可回答性（§7.4）、状态证据分级（§7.5）。
- 改写唤醒规则（§7.7）和验收（§11）。
- 把几处证据不足的说法降级为推断，并修正前后矛盾。

**决定边界**
- 远野说过"按他的推荐来，我认同"。本书只把这句话理解为认可方向：把外部 Agent 的原生确认移出 Work 激活，复用现有事件流。
- 远野没有就任何具体设计作出决定。§13 的各项建议，包括修订 3 改过的推荐，都不能当成已定。
- Codex 复审的结论是"退回修订 1，修改后再作为实施依据"。修订 3 就是这次修改，是否可以作为实施依据，仍待远野审阅。

**基线**
- 服务端 `origin/main` 为 `77e3b508`；工作树为 `fix/httpcore-proxy-tunnel-leak` 分支的 `c9f88934`。据 Codex 核对，所涉外部 Agent、Work 和连接器路径在这两个提交之间没有差异。
- 连接器 `0.7.0`，设备协议 `4`。
- `claude-agent-sdk==0.2.172`，本机 Claude Code `2.1.311`。
- Yuki-Desktop 为 `a5687a6`。

**标注约定**

| 标注 | 含义 |
|---|---|
| 【事实】 | 有原始记录（transcript、journal、生产库、进程）为证 |
| 【代码事实】 | 读源码可以确定，但没有在运行中观察到 |
| 【推断】 | 由代码或文档推出，未经实测 |
| 【待验证】 | 需要实验确认 |

时间均为 UTC+8。

---

## 0. 远野原话与本书结论

> 「你认为大改还是小改，我认为不小，所以是不是得把外部agent操作变为事件化？用当前的event流？这样就防止卡住？」
>
> 「所以你直接把我的想法的原话发给Claude吧，让它调研一下，整理一份任务书，你发给我，因为我说的可能是错的，所以请让Claude认真整理和check，需要说清楚当前的限制和问题以及应该怎么大改」
>
> 「按他的推荐来，我认同，现场中断那个会话，然后我建议他复审一下任务书，看看是不是符合我一贯的想法，然后你再把他的意见发过来」
>
> 「既然要改桌面端，那么再补充一个问题，桌面端的任务的工具调用显示太粗糙了，就那几种，发送消息，调用agent，剩下很多都是其它，一起改了。」

**结论**

1. **"不小"这个判断成立，修订 3 认为比初稿估计的还大。** 涉及面包括：
   - Bot、连接器、Desktop 三个发行单元，设备协议从 4 升到 5；
   - 整个派发路径：socket 写入和在线查询都要改；
   - 控制命令和它所控制目标的结算；
   - 原生权限请求的"可回答性"；
   - Work 的唤醒和退出语义；
   - 工具合同、提示和文档。

   预计不需要新调度器，也不需要新的事件总线。是否需要数据库迁移，要看会话级权限入口怎么设计（§7.4、Q4），尚未定。
2. **"用当前 event 流"方向成立，但只是答案的一部分。**
   - 现有的连接器 journal、事件 ACK、Work 私有输入、完成回执都可以复用（§2）。
   - 光把"回复"改成事件不够，还要做到下面几点：
     - 原激活不在任何 socket 写入或在线 RPC 上等待（§7.1）；
     - 控制命令的结果结算到它的目标（§7.2）；
     - 跨回合的归属可以证明（§7.3）；
     - 权限请求能回答到原生那一端（§7.4）。
3. **"这样就防止卡住"只成立一半。**
   - 做到 §7.1，能防止"对端不读、不答时，原激活被占住、群消息排着没人处理"（验收见 §11 的 V1、V3）。
   - Claude 在后台自发回合中等权限、无人回答的问题，派发事件化单独解决不了。必须同时做到"原请求可被发现、可被回答、回答被原生消费"（§7.4）。只做到"可见"时，要如实缩小承诺。
4. **不推荐的做法**：通用工具超时、看门狗、新事件总线、"同一个 session／最后一个 Work"之类的猜测式归属（§6、§7.3）。

---

## 1. 事故事实链（2026-11-18，群「数字生命研究所」）

| 时间 | 内容 | 证据 | 性质 |
|---|---|---|---|
| 01:33:06 | 工作 `d8cc09a4` 对 Claude 会话 `ae154b01`（原生 ID `e4e2d50f`）发 prompt，操作 `b39f7ea8` | `external_agent_operations` | 事实 |
| 01:35:21 | Claude 用后台 Bash（task `bxogl883w`）等待 CI | transcript 第 993 行 | 事实 |
| 01:35:30 | 第一条 prompt 正常结束，`b39f7ea8` 的 journal 终态为 `completed/end_turn`。之后连接器清空了会话的 operation 和非持续 watch | 连接器 journal；`claude.py:293-317` | 前半事实，后半代码事实 |
| 01:35:57 | **同一** task `bxogl883w`、同一 tool-use-id，第一次通知：`stopped` | transcript 第 1005 行 | 事实 |
| 01:38:41 | 同一 task 第二次通知：`completed`。随后有模型请求、Read，以及 Bash `gh run view …`（01:38:48） | transcript 第 1011、1021 行 | 事实 |
| 01:38:48 起 | 这次 Bash 之后没有匹配的 `tool_result`。代码路径是：会话没有 operation，也没有 watch，`_publish` 的接收者集合为空，不发事件；`_permission` 等待一个 future，这个 future 只会在答复到达或驱动退出时结束（`claude.py:273-286` 退出时会取消 future） | transcript；`claude.py:104-133, 146-163` | **推断**（最符合证据）。没有 `can_use_tool` 的原始帧或协程栈 |
| 01:39:26 | 远野发出 #151781，唤醒了工作 | `runtime_work_waits` `69aed5bc` | 事实 |
| 01:39:33–34 | 模型调用 `use_coding_agent prompt`，操作 `8cf9ae9a`。连接器 journal 中 response 为 NULL，回执为 `original_dispatch_not_confirmed`。transcript 第 1022 行记录了入队（enqueue） | 连接器 journal；transcript | 事实 |
| 01:39:34 起 | 连接器停在 `await session.admitted`（`claude.py:416`），Bot 停在 `connection.request`（`service.py:653` → `transport.py:46`） | 代码 | 等待位置是代码推定；"阻塞"这个后果有事实支持（下行） |
| 至 02:26 处置前 | `8cf9ae9a` 的 `result_json` 和 `native_execution_id` 都是 NULL，run 为 `waiting`，Work 为 `running`。三条群消息和一条子任务结果处于 pending | 处置前只读查询 | 事实 |
| 日志 | 查过 bot 容器最近 40,000 行日志（UTC 17:33:58–17:52）。corr=772ed3cf 最后可见的一条是 17:39:33.612 的 `response_received`，之后没有工具或请求记录 | `docker logs` 只读 | 事实 |
| 01:46:51 | 私聊那边的工作代为在群里说"Claude 仍在运行"。session 查询返回的是乐观的 `active` 值 | `claude.py:403, 367-374` | 推断为误报 |
| **02:26:35** | 核对目标后，只对 PID 14950（`--resume=e4e2d50f…`，无子进程）发送 SIGTERM，进程退出，连接器 899 仍在 | `ps` | 事实 |
| 随后数秒 | `8cf9ae9a` 收到 `status:"unknown"`，之后回执为 `previous_sdk_owner_not_attached`／`ProcessError`／`pending:true`。原激活返回，四条积压输入被消费 | 连接器 journal、生产库 | 事实 |
| 02:26:42–02:27:01 | 产生四条 outbound 聊天记录（`chat_events` 151832–151835）。服务器侧没有新的 prompt 或 queue | 生产库 | 只证明 Bot 记录了发送；平台送达未核对投递回执 |
| 02:27:16 | 注册等待 `a695a79d`（300 秒、会话消息、owned_run `f195a13e` 三者任一）。这是 02:27 的快照，**不代表当前服务器状态** | 生产库 | 事实 |
| 处置后 | transcript 在第 1022 行的入队之后，直到文件末尾都没有出队，也没有新的用户回合 | transcript | 只能说"这份 transcript 未见该 prompt 开始执行，回执未知"，**不能据此判定未执行或可以重派** |

**排除范围**：同一设备上另一个 Claude 会话同期工作正常，只能排除设备级的全面中断。

**处置说明的是什么**：SDK 宿主进程退出后，驱动的 `_serve` 收尾，令待确认的 admitted 以 uncertain 失败，于是原 RPC 返回（`claude.py:273-286`、`connector.py:544-565`）。处置**不能**证明"WSS 一断开就解卡"。

---

## 2. 现状：已经存在的事件流

| 环节 | 现有能力 | 位置 |
|---|---|---|
| 连接器事件持久化 | 驱动 `emit` 后写入 journal 的 `events`，每条带 sequence 和完整 `operation_state` | `connector.py:323-353`、`journal.py:346-369` |
| 发送、ACK、重传 | 按 sequence 依次发送。服务器提交后才 ACK。断线重传未 ACK 的事件，不重新派发原生操作 | `connector.py:642-656`、`journal.py:380-382` |
| 服务器入账 | 先插入 `external_agent_events`（`device_id,event_id` 去重），再按 `state_sequence` 单调投影到原操作。没有 `operation_id` 的事件**入库但不投影到 Work**。带不存在的 operation ID 会报错 | `repository.py:732-800` |
| 转成 Work 私有输入 | 操作仍未完成时生成 `control` 输入（`external-agent:{event_id}:{op}`），权限类事件附带 `permission_id` | `repository.py:809-837, 895-965` |
| 唤醒 | `signal_external_action`，分三支（见 L8） | `work_wait.py:394-426` |
| 完成 | 只有带 run 的操作（prompt/queue/watch）在终态时产生 `completion:{request_id}` | `repository.py:460-510, 589-662` |
| 等待原语 | `wait(for=[run/message/seconds/at/plugin_event])`，`owned_run` 覆盖外部 Agent 的 run | `work_control.py:180-202, 1080-1131` |
| 重启恢复 | 已派发的调用不重跑。冷恢复从效果回执或 `SandboxTaskRun` 重建结果 | `work_session.py:344-396`、`work_journal.py:661-730` |

**现有事件流覆盖不到的地方【代码事实】**

1. **派发回复不进事件流。** `journal.complete` 用"插入占位行再删除"的方式占一个 sequence（`journal.py:215-221`），回复本身只走 RPC 应答。服务器按 `type=response` 分流（`http.py:302-312`）。
2. **Claude 的接收者集合为空时不发事件**（`claude.py:107-133`）。
3. **Codex 对未观察的会话不上传**（`codex.py:696-711`）。这个分支还负责**缓冲已提交 prompt 早到的通知**，不能整段删掉。
4. **控制命令没有自己的 run。** answer/steer/cancel/detach 的结果目前靠同步回复后由服务层 `_control_result`／`_detached` 结算到目标（`service.py:652-735`）。事件入口只投影事件自己所属的操作；`pending:false` 的事件不进普通私有输入（`repository.py:816`）。
5. **来源清理路径**（`service.py:740-800`）用派生 ID 直接 `connection.request`，没有先 `prepare`。

---

## 3. 当前限制与问题（修订 3 已按核证结果修正）

| 编号 | 问题 | 证据 | 性质 |
|---|---|---|---|
| L1 | 原生改动命令在 Work 激活内 `await connection.request`。它先 `await send`：要等同设备共享的 `_send_lock` 和 `socket.send_json`，可能遇到背压；再等回复 future。两段都没有上界。断线时 future 会以 `DeviceOffline` 结束 | `transport.py:29-47, 66-69`；`service.py:652-656` | 代码事实。本次解卡是 SDK 宿主进程退出所致，不是断线 |
| L1b（新增） | `get(operation_id)` 在操作仍 pending 时，先做一次在线 RPC，再读服务器已持久化的事件。所以权限可能已经入库，查询它的 Main 却又挂在同一设备上。按实例 list、按会话 get 也要在线等待 | `service.py:247-300` | 代码事实 |
| L2 | 会话锁覆盖整次激活。新消息只在下一次模型请求之前接入，正在 await 的调用不会被重新检查 | `loop.py:65-71`、`main_agent_backend.py:271-298`、`main-agent-runtime.md:544-550`（有意取舍） | 代码事实 |
| L3 | Claude 驱动收到 `UserMessage`、`AssistantMessage`、`ResultMessage` 中任意一种，或任一权限回调，就判为已接纳，不核对 uuid；什么都收不到就一直等 | `claude.py:142-144, 289-292, 404-416` | 代码事实 |
| L3a（修正） | 后果比初稿写的更重：旧回合的 Result 一到，可能直接把新 prompt **投影为完成**。原因是 `_publish` 对非 unknown 负载默认写 `admitted:true`，journal 折叠时会保留它，`completed` 也设了 admitted | `claude.py:104-106, 293-317`；`journal.py:39-50` | 代码推断 |
| L4 | 在没有匹配 watch 时，`active` 只反映连接器自己发起的回合。CLI 自发的回合不会置 active，于是新 prompt 不报冲突，进了 CLI 队列 | `claude.py:398-416` | 代码事实；本案有入队记录 |
| L5 | 在没有匹配 watch 时，一轮结束后清空 operation，之后自发回合的进度和权限都没有接收者 | `claude.py:293-317, 104-133` | 代码事实；本案中是否停在这里属于推断（§1） |
| L6 | 无主权限请求即使上报了，也无法回答：服务器要求请求属于原操作，驱动要求请求属于当前回合，并且 `connection_id` 指的是 SDK `ManagedSession` 的 ID（不是 Bot WSS 的连接 ID） | `service.py:559-588`；`claude.py:40-48, 469, 487-505` | 代码事实 |
| L7 | 卡住之后，从 Yuki 侧 cancel 原操作，预计返回 `execution_mismatch` | `service.py:546-556`；`claude.py:469` | 有依据的反事实推断，没有尝试 |
| L8 | `signal_external_action` 分三支：有 binding 时，只对匹配的 `owned_run` 调用 `_deliver`；没有 binding 且处于 `waiting_external` 时排队；处于 `waiting_user`、`suspended` 或终态时提前返回。根 Work 只等消息时会被映射成 `waiting_user`（`need_input`）。注册 wait 时回看已到的输入，也只按 run ID 匹配 | `work_wait.py:297, 394-426, 492`；`work_control.py:1120-1126` | 代码事实 |
| L9 | session 查询返回乐观的 `active`；operation 查询走的是 journal | `claude.py:367-374`；`connector.py:502-512` | 代码事实 |
| L10 | Codex 的 prompt 除了 `turn/start`，还要等 `thread/read`、`resume` 或 `start`；ACP 也等 admitted。这些 RPC 都没有上界 | `codex.py:1003-1051`；`acp.py:470-487` | 代码事实 |
| L11 | 本机设备工具共用同一传输。问题是"确认没有上界"，不是"每个本机动作都不会返回" | `local_device.py:63,140,356,512,562` | 代码事实；本期不改业务合同 |
| L12 | 卡住期间的日志范围见 §1：两端没有新的记录，只有 journal 里的 `original_dispatch_not_confirmed` | 只读日志 | 事实 |
| L13 | prompt 的 `ownership=execute`，run 处于 `waiting` 时会推迟 Work 的正常 completed，转为 `work_owned_execution_pending`。现有 detach 依赖设备：设备离线时走不到；设备 journal 里没有记录时报 `original_operation_not_found`；`_detached` 会给**原 prompt** 写 `admitted:true` | `repository.py:452-457`；`work_repository.py:555-575, 647-655`；`connector.py:232-235, 513-525`；`service.py:692-735` | 代码事实；本案 run 仍为 waiting |
| L14（新增） | 服务器对 prompt 操作强制"一个操作只绑一个原生 execution"：`_bind_native` 在序号过滤之前检查，不一致时抛 `ValueError`。连接直接以 1011 关闭，连接器重连后重发同一事件，可能妨碍同一设备后续所有事件的交付 | `repository.py:54-63, 533-554, 809`；`http.py:302-330` | 代码事实 |
| L15（新增） | `_open(resume=True)` 要先等 `_inspect`，之后才登记 session。两条并发的 resume 可能各自建一个 SDK owner，所以"每条命令一个独立 task"不能保证"一个会话同时只有一个回合" | `claude.py:182-225, 384-395` | 代码推断 |
| L16（新增） | 事件状态投影被跳过（序号旧）之后，仍会为尚 pending 的操作插入 Work 输入。旧事实可以留档，但已经解决的问题可能被再次要求处理 | `repository.py:809-837` | 代码事实 |
| L17（修订 4） | 对运行中的 Claude 回合无法追加指令。工具对所有实例都声明 steer/queue；服务端不按实例能力拦截；连接器 Claude 驱动一律拒绝 steer/queue；Claude 回合仍在进行时 prompt 返回冲突。各层分析见 §19 | `tools.py:55-68`；`service.py:413-659`；`claude.py:398-399, 424-427`；17:50:48 的现场 steer 操作 `fe56ccac` 返回 `unsupported_action` | 代码事实＋现场事实 |

---

## 4. 故障链（区分已证实与推断）

1. 第一条 prompt 正常结束。Claude 留下的后台 Bash 先报 stopped、后报 completed，第二次通知唤起了新的回合【事实】。
2. 新回合调用 Bash 后再也没有结果。最符合证据的解释是：权限回调的接收者集合为空，没有人能回答【推断】。
3. 新 prompt 只进了 CLI 队列【事实】。由于 L3 和 L1，连接器和 Bot 都在等确认【代码推定的等待位置，后果是事实】。
4. 由于 L2，群消息只能排队。
5. 02:26 SDK 宿主进程退出，原 RPC 以 unknown 返回，积压被消费【事实】。由于 L13，原 run 仍未结算。

---

## 5. "事件化能防卡吗"：承诺范围

| 目标 | 需要的条件 | 本书能承诺的范围 |
|---|---|---|
| 原激活不被外部确认占住 | §7.1：激活只提交本地持久登记就返回，socket 写入交给传输生命周期；查询优先返回持久事实 | 已登记的原生改动命令，**即使对端不读、不答**，也不占用原激活（V3） |
| 群消息有人处理 | 激活释放之后，沿现有路由：先匹配显式回复和会话等待（命中就回到原 Work）；没有匹配、也没有活动 Work 接管时，再按普通入口另起 Main（`chat.py:592-612`） | "按原路由，接收者能接收并回复，只投递一份"，原 Work 和新 Main 两条路分别验收（V1）。**不再写"一定由新 Main 回复"** |
| Claude 卡在没人收到的权限上 | §7.3 的因果归属，加上 §7.4 的可回答性 | 只有做到"原请求可发现、可回答、回答被原生消费，并有后续原生事实"，才能说防住了原生卡住；只做到可见时，承诺相应缩小 |
| SDK 宿主退出后 run 悬空 | §7.6 显式解除所属关系 | 在线、离线、设备从未记账三种情况分别写明能做到什么 |

---

## 6. 方案比较

| 方案 | 结论 |
|---|---|
| A：只修 Claude 驱动 | 必要，但不够 |
| B：只把回复改成事件 | 不够：§7.1 的 socket 写入和在线查询仍会占住激活，控制命令也无法结算 |
| **C（修订 3）：§7.1 至 §7.8 全部做** | **推荐**。远野认可的是方向，具体设计待审 |
| D：通用超时或看门狗 | 否决：经验阈值违反开发约束 §0 和 §5（`development-contract.md:87-91`） |
| E：新事件总线 | 否决：和现有事件流重复 |
| F：拆会话锁 | 本期不做（Q8） |
| ~~C1／C2 归属~~ | **修订 3 撤销**，理由见 §7.3 和 §18 |

---

## 7. 推荐设计（修订 3）

### 7.1 防卡边界：整个派发路径

**承诺**：已登记的原生改动命令（prompt/queue/watch/steer/answer/cancel/detach），即使对端不读也不答，也不占用原激活。

**做法（建议）**
1. **激活内只做本地登记。** 激活内只执行现有的 `prepare`：写事务内登记原操作，有 run 的动作同时登记 run。然后**从持久事实返回**，不在激活内 `await` 任何 socket 写入或回复。
2. **写出交给现有传输生命周期。** 由设备连接已有的生命周期（与 `events()` 和读循环同属这条连接）把已登记、尚未写出的命令写出去。
   - 写出之前，在 reader 中重新核对：原来源仍然有效、连接是当前连接、设备授权仍有效。对应现有的派发前二次核验（`service.py:619-651`）。
   - 写出之后再登记"本地 socket 已写完"。建议改为在真正写出时才写 `dispatched_at`；现在 `prepare` 时就写了它，同时写 `created_at`（`repository.py:481-495`）。这只是语义修正，不需要新列，需要核对 `dispatched_at` 现有的读者。
   - 不增加通用超时，也不增加另一套调度器。
3. **原操作查询优先返回持久事实。** `get(operation_id)` 先返回服务器已持久化的回执、事件和最近一次确认点。确实需要在线获取的原生历史，与"查证这次操作"分开：可以是显式的在线刷新，或另一个读路径。按实例 list、按会话 get 也一样，不在激活内无界等待。是否保留某种同步在线读，以及保留后 V3 缩小到什么范围，见 Q1。
4. **改动类命令的回执只走持久事件，见 §7.2。** 修订 1 已撤销"保留 RPC 应答"的设想。

### 7.2 回执事件与控制命令结算

**连接器**
- `journal.complete` 在同一事务内生成一条**可重传的正式事件**，不再只占序号。提交之后唤醒 `_events`。
- 线上消息格式要能区分 `type=event, kind=response` 和现有的 `type=response`（`http.py:302-312`）。

**服务器**收到 response 事件时，从**原控制命令**解析出目标，分别落到三处：
1. 控制操作本身的回执；
2. 目标操作或目标 run 的变化。例如 detach 写 `detached_at`、保留 `native_status`、完成原 run；cancel 记录终态。这对应现在 `_control_result`／`_detached` 的语义；
3. 发起方 Work 的私有通知。发起方可能是另一个 Work，也可能没有 Work，不能把两个 Work 混为一体。

**ACK 时机**：上述结算在一个可幂等恢复的提交边界内完成之后才 ACK。

**来源清理路径**（`service.py:740-800`）也要走"登记后派发"，或者有等价的持久回执，否则删掉 RPC 应答之后它就等不到结果。按原 ID 上传的事件还可能触发 `external_agent_operation_unknown`。

**控制命令的返回值（Q3）**：返回控制 operation 的 ID，并分别记录三件事：控制是否被接纳、实际影响了哪个目标、目标后来是否结束。没有 Work 的控制命令也能按原 ID 查询。

### 7.3 跨回合归属：保留原绑定，显式表达因果后续执行

**撤销 C1、C2 的理由【代码事实＋事故事实】**
- **C1 与绑定规则冲突。** 给自发回合分配新 execution 后，如果沿原 prompt operation 上报，会撞上 L14 的单执行绑定，导致事件事务回滚、连接以 1011 关闭、同一事件反复重传。
- **C1 的任务跟踪会提前丢失来源。** 本案同一 task 先报 stopped、后报 completed。按第一条终态通知清掉关联，在第二条通知唤起 Bash 之前来源就已经丢了。SDK 自己也写明"已知活动任务集合为空，不等于运行结束"（SDK `_internal/query.py:870-886`）。另外 `TaskStarted.tool_use_id` 可能缺失。
- **C1 对 origin 的判断写错了。** `scheduled-trigger` 是 `task-notification` 的 subkind（SDK `types.py:1301`），只看 `origin.kind` 会把定时任务误认成本次委托。
- **C2 删两行实现不了。** completed 转 idle 只对 `session.watches[op] is None` 生效，普通 prompt 仍然会完成。接收者集合为空时，去掉过滤也不会凭空出现接收者。删掉 Codex 的空集合分支，还会破坏早到通知的缓冲（`codex.py:675-711`），并可能扩大对独立本机会话的采集。
- **不接受"C1 实验不可靠就自动换 C2"。**

**修订 3 的建议**
1. **保留原 prompt 与原 execution 的历史绑定。** 不改写原操作的执行归属。
2. **因果后续执行用显式结构表达。** 只有可以验证因果来源时，才登记"这次后续执行由哪个原操作引起"。
   - 例如：由服务器按确定性 ID 派生一条"后续执行"记录，链接原操作和原 Work，有自己的 execution、回执和 run 语义，而不是塞进原 prompt 的 execution 槽。
   - 因果证据要能经得起"同一 task 多次通知"：不以第一条终态通知清除关联，要求 task-id、tool-use-id 和原 execution 能对得上。
   - CLI 的 `session_state_changed`（`idle`／`running`／`requires_action`，SDK `_internal/query.py:244-249, 446-490, 924-926`）可以作为会话忙闲和"正在等待操作"的原生事实来源。2.1.311 在本连接方式下是否发出这类帧【待验证】。
   - 后续执行是否有 run、是否阻止 Work 完成、怎样结束，属于实施设计，见 Q9。
3. **已有的会话级 watch 按其原授权观察。**
4. **证据不足时，保留原回合终态，归属标为未知。** 不按"同一 session""最后一个 Work"或之后才加的 watch 回填历史。要不要给本人提供会话级入口，是单独的产品决定（Q4、Q12）。
5. **ACP 和 Codex**：保留各自协议的能力差异。Codex 的未观察过滤不在本期删除。

### 7.4 权限请求的可回答性

下面四件事是不同的结果，要分开表述、分开验收：
1. 请求已保存；
2. 本人能发现这条请求；
3. 请求仍然可以回答；
4. 原生已经收到并消费了回答。

**回答绑定原 callback**
- 回答时要核对：原 SDK owner（`ManagedSession` 的 `connection_id`）、session、原回合的 execution、原生请求 ID、当前 Person、设备授权。
- 不要求请求属于"当前回合"，但原 owner 已经重建时，旧 callback 不能被新 owner 接手。
- WSS 重连可以保留原 callback。

**只消费一次**
- 同一个原生请求经 prompt 和多个 watch 扇出时，服务器侧会有多个事件 ID，但原生决定只能消费一次。
- 已撤回、重复回答、owner 换了之后才回答，都要返回真实的结果。

**V2 验收的依据**：`consumed=True` 只能证明回调消费了答案。"原生继续执行"还需要后续匹配的原生事实。

**会话级入口（Q4、Q12）**
- 要先决定：哪些已授权的会话可以上传哪些字段，本人怎样发现和处理，离线时怎样展示原请求。原生权限参数里可能有命令、路径和正文，扩大上传范围会改变隐私边界。
- 现有事件表没有单独的 session 列和 session 分页索引（`db_models.py:77`）。事件查询只按 operation ID 分页（`repository.py:967-996`）。Desktop 只渲染所选 operation 的事件（`ConnectorTasks.tsx:471`）。
- 是否需要迁移，由最终的查询、授权和清退路径决定。
- 只做到"可见"时，承诺缩小为"可发现"，不宣称能防住原生卡住。

**watch 不能补发**：权限请求发出之后再加 watch，没有补发这个 pending future 的路径（`claude.py:443-458`）。

### 7.5 状态按证据分级

每一级只由对应的证据支持，不能互相推断：

| 状态 | 证据 |
|---|---|
| Bot 已登记 | `prepare` 提交 |
| 本地 socket 已写完 | 传输生命周期确认写出（§7.1） |
| 连接器已记账 | 连接器 journal `begin`，经 response 事件回传 |
| 原生已排队 | 只限原生有精确入队回执的驱动，例如 Codex 的队列 ID（`codex.py:1072`） |
| 该 UUID 已被回放 | Claude 的 `replay-user-messages`（SDK `client.py:367-380`），回放时机【待验证】 |
| 执行已开始 | 原生给出的开始事实 |
| 终态 | 原生给出的终态 |

**几个必须保持一致的地方**
- **空回执的现状**：现在空回执的视图是 `status=unknown`、`pending=true`、`uncertain=true`、`admitted=false`（`repository.py:354-372`），而模型视图会把 `admitted`、`uncertain` 过滤掉（`tools.py:144-160`）。工具合同必须写清"返回时已经发生到哪一级"。首次返回、同 ID 重入、冷恢复（`work_journal.py:661-730` 会从 `SandboxTaskRun` 重建）和 Desktop 查询四个地方的解释必须一致，不能只改 `_view`。
- **`previous_sdk_owner_not_attached` 不能证明进程已退出**：Claude 的 `recover()` 自己就会产生这个原因（`claude.py:87-102`），`get(operation_id)` 在 unknown 时还可能触发它（`connector.py:502-512`）。本案另有 PID 退出的独立证据。
- **防反馈环**：如果普通进度都能唤醒 Work，会形成"查询 unknown → 产生新进度 → 又唤醒 Work 去查询"的循环。所以唤醒只限需要回答的事件（§7.7）。

### 7.6 unknown 操作的收尾（L13）

**语义**：显式"解除 **Work 所属关系**"，与原生状态分开。原 prompt 的 unknown 状态、原 ID、"原生是否投递尚未确认"这些事实都保留，可以查回。

**三种情况分别写明能力**
1. 设备在线、journal 有记录：沿现有 detach，同时通知原生停止观察。
2. 设备离线：只在服务器解除所属关系，原生侧的状态保持 unknown。
3. 设备从没记账（Bot 在 `prepare` 之后、设备 `begin` 之前崩溃）：同 2。

如果只允许设备确认之后才能解除，就必须写明：离线或缺少原记录时，Work 按现路径无法完成。

**`admitted:true` 的归属**：现有 `_detached` 写的 `admitted:true`，要明确指向"解除关系"这个控制操作，不能让历史看起来像原 prompt 被接纳过。

**不自动 detach**：`previous_sdk_owner_not_attached` 本身不触发自动 detach（Q11）。

### 7.7 唤醒规则（L8，Q2）

- **只扩大到需要实际回答的事件**（permission／question）。普通进度留在收件箱，沿现有等待合同处理。
- **四种等待形态逐一写明语义**：
  - 有 binding：交付给当前等待。注意 `_deliver` 会消费一次性 wait，不能让普通进度打断时间、插件或 `all` 等待；
  - 无 binding 的 `waiting_external`：已经会排队；
  - 根 Work 只等消息的 `waiting_user`：权限请求是否应当唤醒它，需要明确；
  - `suspended` 和终态：不唤醒。
- **两种先后顺序都要处理**：
  - 先等后到：实时的 `signal_external_action`；
  - 先到后等：注册 wait 时回看已到的输入，目前只按 run ID 匹配（`work_wait.py:297`），要补上按权限请求匹配。

### 7.8 驱动正确性（Claude）

- **接纳改按 uuid 判断**，并且覆盖所有会写接纳状态的位置：`session.admitted`、`_publish` 默认写入的 `admitted:true`、journal 的折叠逻辑、`completed` 设置的 admitted。要在归属确定之后，才把 prompt 的 UUID、回合、回执和控制目标绑在一起。
- **串行化同一会话的 open/resume**（L15）。覆盖：两个 Work 同时 prompt、并发 resume、旧 owner 的迟到事件。
- **cancel 必须证明实际中断的是哪个目标。** `interrupt()` 作用于当前 SDK 回合，返回 `cancel_requested`，原生终态稍后才知道。不能用一次会话级 interrupt 冒充"已撤回排队中的 prompt"：在本案这种情况下，它可能打断的是正在等权限的前一个回合。
- **会话忙时（Q5）**：
  - 已证实原生忙、驱动能在发送前拒绝时，报 `conflict`。证实方式：Claude 用 `session_state_changed`【待验证】，Codex 用 `thread/read`；
  - 原生有精确入队回执时，报 `queued`；
  - 写出之后只有沉默时，保留"待确认"；
  - Claude 目前明确不支持精确的 queue/steer（`claude.py:424-427`），保留这个能力差异。
- **旧事件不再要求行动**：投影已被跳过的旧事件，不再生成要求处理的输入，只留档（L16）。

---

## 8. 状态、权限、并发、去重、恢复与安全

- **状态**：按 §7.5 分级，各级不互相推断。
- **权限**
  - 在执行处核验当前人物授权。
  - 回答绑定原 callback（§7.4）。
  - 不自动批准，也不把普通操作升级为人工审批。
  - 设备撤销后，每批事件入账前都会 `require_device`（`repository.py:719-720`）。撤权前已提交的事实保留，撤权后的新上传和新效果一律拒绝。
- **并发**
  - 激活内不等待任何没有上界的外部写入或确认。
  - 同一会话的 open/resume 串行（§7.8）。
  - 一个原生请求只消费一次答复。
- **去重**：沿用事件 ID 去重、`state_sequence` 单调、`source_key` 唯一。旧事件不再生成需要处理的输入。
- **恢复**
  - 已登记但未写出的命令，由传输生命周期写出。
  - 已写出但回执未知的命令，按原 ID 查证，不重新派发。
  - 连接器重启时沿用 unknown 规则。
- **安全**
  - 只上传里程碑，不进入公共聊天历史。
  - 扩大权限参数上传范围属于隐私边界变化，需要远野决定（Q4）。

---

## 9. 分阶段改造

**阶段 0：先写失败场景，并做 SDK 实验，不写产品代码。**
- 失败场景见 §11。
- SDK 实验内容：
  - 回放时机；
  - `session_state_changed`；
  - Task* 帧和 `origin` 子类型的实际输出；
  - 权限回调前各字段是否齐全；
  - "同一 task 多次通知"的帧序列；
  - （修订 4）§19.4 的追加实验 X1–X6。

**阶段 1：连接器。**
- response 正式事件；
- 按 uuid 接纳，覆盖所有写入点；
- 串行化 open/resume；
- 精确 cancel；
- 会话忙判断；
- 因果后续执行的上报（以阶段 0 的结论为前提）。

**阶段 2：Bot。**
- §7.1 只提交本地登记、由传输生命周期写出，查询优先返回持久事实；
- §7.2 控制结算与清理路径；
- §7.4 回答绑定；
- §7.6 解除所属关系；
- §7.7 唤醒；
- 工具合同与提示；
- 模块文档（`local-agent-connector.zh-CN.md`、`main-agent-runtime.md:464-468`）。

**阶段 3：Desktop。**
- §17 的 T1／T3；
- 如果 Q12 选择做会话级入口，再加上会话级权限的发现与回答入口。

**阶段 4：删除旧路径。**
- 激活内的 `connection.request`；
- 乐观接纳；
- 改动类命令的 RPC 应答处理；
- 冻结同步语义的测试夹具；
- "返回即已接纳"的文档。
- 删除时要按 `resource`（设备工具）和原生 Agent 动作分开，**不能误删设备工具的 RPC**（Q7）。

**阶段 5：配套发布与上线（Q6、Q10）。**
- 先改模块合同。共同约束（`development-contract.md:87-91`）里已有"原意图登记、等待释放、来源与回执"等条文，暂不追加覆盖所有工具的绝对承诺。

---

## 10. 迁移与兼容

- **数据库**：服务器侧的变化主要是语义：`dispatched_at` 的时机、控制结算、因果后续执行记录、解除所属关系。
  - 因果后续执行如果作为新记录，需要评估表结构；
  - 会话级权限入口如果需要按 session 分页，需要新索引或新列；
  - 两项都等 Q4／Q9 决定后再评估迁移，**不再断言"不需要迁移"**。
- **协议**：建议硬切到 5（Q6）。
  - 代价：从 Bot 上线到用户手动更新连接器之间，**整个设备 WSS 都不可用**，终端、文件、computer、browser 也包括在内。连接器收到 `1008/unsupported_connector_protocol` 后停止重连，等待更新（`connector.py:713`）。
  - 要测：两个方向的版本错配、手动更新后恢复、v4 遗留的未 ACK 事件、只有旧 response 的记录。
- **在途操作**：原 ID、回执和 journal 都保留。按 §7.5 分级查证，不重新派发。
- **跨仓**：Desktop 的任务步骤（§17）。如果 Q12 选择做会话级权限入口，Desktop 还要补：会话级发现、原请求目标、失效状态和回答回执；现有 `NativeRequest` 只处理所选 operation 的事件。

---

## 11. 测试与验收（修订 3 按 Codex 建议重写）

**现有测试夹具的边界【代码事实】**
- `tests/integration/test_external_agent_bridge.py` 用真实的 TLS/WSS、数据库和 WorkControl，但 `NativeComputer` 是可控对端，不能证明生产连接器 journal、Claude SDK 回调、Main 调度和聊天投递已经串通（`:104`）。
- 连接器包的 Claude 测试用真实 SDK 解析，但 `NativeTransport` 收到用户消息后立即回放，恰好绕过了本案最关键的"入队"时序（`packages/yuki-agent-connector/tests/test_claude_driver.py:38-60`）。
- 两套夹具都可以复用，但要补齐下表的时序。V5（真实 CLI 时序）单列；V6（付费）需单独授权。

| 场景 | 验收事实 |
|---|---|
| S01–S03、V1、V3 | 按本案的顺序："同一 task 先 stopped 后 completed"。再分别加四种情况：不回放 UUID、旧 Result 先到、callback 先于回放、设备停止读取。让对端保持未确认。用事件的先后关系证明：这期间激活已经释放、后续输入能被消费、相应的发送回执已取得。原 Work 回复和新 Main 回复两条路由分别验收，各只投递一份。**不用"正常延迟内"作标准** |
| S04–S07、S14–S15 | 分开验证五个崩溃窗口：Bot 已登记但未发送；连接器 begin 后、原生结果前；journal 写了回执但未 ACK；服务端已提交但 ACK 丢失；v4 的未决记录在升级后。核对：原生命令次数、原 ID、原预算、原始事件、最终投影、Work 输入。已有的终态能恢复；确实没有证据时才保留 unknown |
| S08–S09 | 权限请求分别早于和晚于 wait 注册。覆盖：无 binding 的 `waiting_external`、只等消息的 `waiting_user`、`all`、暂停、终态、generation 变化。核对普通进度不会消费无关的等待；终态只保存允许留存的事实 |
| S10–S12、V2 | 分别验证：当前回合能精确中断；只在排队中的 prompt 无法撤回；两个 watch 回答同一请求；原生撤回请求；SDK owner 更换；WSS 单独重连；detach 之后又来通知。"控制已提交""callback 已消费""原生继续或实际终态"三者分别记录依据 |
| S13、S16 | 撤权前已提交的记录仍按既有授权规则处理；撤权后的新上传和新控制被拒。显式解除所属关系分四种情况验证：设备在线、有 journal、没有 journal、设备离线。原 prompt 的 unknown 和原生待确认的事实，在收尾之后仍能查回 |
| 控制与清理 | Work A 发出 prompt，由 Work B 或无 Work 的入口执行 answer/cancel/detach。再覆盖：来源终止、隐私清理、迟到回执。核对发起方、目标 run、completion 各只结算一次，兄弟会话继续工作 |
| 归属 | 同一会话两个 Work 同时 prompt、并发 resume、旧 owner 的迟到事件、定时触发的回合（`scheduled-trigger`）不被误认为委托 |
| 传输 | 设备停止读取，或前一次写入被背压时，激活仍然释放；`get(operation_id)` 在设备不答时返回持久事实 |

**产物**：保留可重放的输入顺序和生成命令。用 `operation_id / run_id / execution_id / permission_id / event_id / state_sequence` 把以下各层串起来：原生观察、journal、服务端事件、Work 输入、等待、投递回执。

**最有判别力的三条通过条件**（按 Codex 建议）
1. 故意让原生确认一直不来，群消息仍能按原路由得到回复。
2. 故意让旧回合的权限请求与新 prompt 交叉，只有真正的原请求能被回答。
3. 故意在 ACK 前后重启，既不重复原生效果，也不丢掉原 Work 的退出路径。

---

## 12. 风险

- **因果后续执行可能找不到可靠证据。** 证据不足时只能标为未知归属，"防原生卡住"的承诺随之缩小。
- **硬切协议期间，整个设备 WSS 不可用。**
- **激活不再等写出，模型的行为会变。** 模型需要按工具合同理解"返回时发生到哪一级"，提示要用真实对话复核，避免反复查询。
- **会话级入口改变隐私边界。**
- **删除旧路径可能误删设备工具的 RPC**（Q7）。
- 阶段 0 的 SDK 实验结论可能推翻 §7.3、§7.8 的部分做法。

---

## 13. 待决问题（"建议"均为复审后的建议，不是决定）

| 编号 | 问题 | 修订 3 建议 |
|---|---|---|
| Q1 | 提交与确认分离；激活返回时，最多保证到哪一级（Bot 已登记还是 socket 已写完）？是否保留同步在线读？ | 分离；激活只保证 Bot 已登记；写出交给传输生命周期；改动类命令的回执只走持久事件；查询优先返回持久事实。若保留同步在线读，V3 缩小到实际覆盖的范围 |
| Q2 | 唤醒范围 | 只扩大到权限／提问；四种等待形态和两种先后顺序分别写明语义 |
| Q3 | 控制类命令异步化 | 同意；返回控制 operation ID；"接纳／影响目标／目标终结"三者分别记录；无 Work 的控制命令也可查 |
| Q4 | 无主权限请求做到什么程度 | 可选：经授权让本人可见；若要解除原生阻塞，还需绑定原 callback 的回答入口，并显示撤回、已答、owner 失联。只做到可见时缩小承诺。隐私范围待定 |
| Q5 | 会话忙时怎么报 | 按证据分级：能在发送前证实忙就报 conflict；有精确入队回执才报 queued；只有沉默就保留待确认。各驱动的能力差异保留 |
| Q6 | 协议 5 是否硬切 | 可以硬切；代价是整个设备 WSS 不可用，需测版本错配和遗留记录 |
| Q7 | 设备工具是否纳入 | 不改业务合同；共享传输和 `_command` 的分流必须回归，避免误删 |
| Q8 | 会话锁 | 本期保留 |
| Q9 | 跨回合归属 | 保留原绑定，显式表达因果后续执行，证据不足就标为未知；**撤销 C1／C2，不允许自动切换**；后续执行的 run 和完成语义随实施设计一起定 |
| Q10 | 发布与共同约束 | 一次配套发布，Desktop 随附包和更新清单按实际发行链核对；先改模块合同，暂不追加共同约束 |
| Q11 | unknown 怎么收尾 | 显式解除 Work 所属关系，保留 unknown 和原 ID；在线、离线、从未记账三种情况分别写明能力；不自动 detach |
| Q12 | Desktop 会话级权限入口 | 如果需要本人看见或回答，列为明确的跨仓交付（会话级发现、原请求目标、失效状态、回答回执） |
| Q13 | 现场遗留的 run `f195a13e` | **本轮保持现场**。02:27 的快照不代表现在。如果另行授权处置，先只读核对原 run、原操作、真实 CI 和 PR 状态，再决定是否仅解除所属关系。恢复原 session、发新 prompt、重跑 CI、合并、上线，是分别产生效果的动作，不能从"认可方案方向"推出 |
| QD1–QD6 | 桌面端步骤展示 | 见 §17；修订 3 的影响见 §17.11 |
| **Q14**（修订 4） | Claude 的追加指令提供到哪一级 | 推荐 §19.5 的方案 B（追加输入，由原生决定落在哪个回合）作为 Claude 的 `queue`，并如实报告状态。`steer` 对 Claude 继续不支持，除非阶段 0 实验能证明可以把输入**绑定到指定回合**。详见 §19.5 |
| **Q15**（修订 4） | 服务端是否按实例目录中的能力，在派发前拒绝不支持的动作 | 建议拒绝：直接返回 `unsupported_action`，附该实例真实支持的替代动作，不登记操作，也不往返设备。目录以连接器最新上报为准，连接器仍是最终裁决 |
| **Q16**（修订 4） | 是否使用 CLI 中未公开的排队优先级（静态字符串里有 now/next/later） | 不用：未公开、未写入 SDK 合同、版本间不稳定 |

---

## 14. 现场处置（02:26 已执行）

- **处置前核对**
  - PID 14950 的命令行含 `--resume=e4e2d50f…`，父进程是连接器 899，没有子进程。
  - transcript 在入队之后只有计费记录。
  - 操作 `8cf9ae9a` 的回执为空，Work 为 running，积压四条输入。
- **操作**
  - 只对 14950 发送 SIGTERM。
  - 没有用 Yuki 侧 cancel：按 L7 推断会失败，而且按 §7.8，会话级 interrupt 也不能证明作用于哪个目标。
  - 没有重启连接器：会连带其他会话。
- **结果**：见 §1。
- **仍待处理（Q13）**
  - run `f195a13e` 处于 unknown／waiting，按 L13 会推迟 Work 完成。
  - 原 Claude 会话没有恢复。
  - CI 重跑、合并、上线都没有发生。
  - 本书只记录到 02:27 的快照，没有再查现场。

---

## 15. 事实与推断汇总

- **事实**
  - §1 中标为事实的各行，包括同一 task 两次通知、入队、处置结果；
  - §2 的表格；
  - L12，以及 L13 中"本案 run 仍为 waiting"。
- **代码事实**：L1、L1b、L2–L6、L8–L11、L13 中的阻止完成和 detach 依赖、L14、L16。
- **推断**
  - 权限回调卡住；
  - L3a、L7、L15；
  - §5 中两条路由的回复行为（未实测）。
- **待验证**
  - 回放时机；
  - `session_state_changed` 的实际输出；
  - Task* 帧的字段是否齐全；
  - ACP 的自发回合；
  - `dispatched_at` 现有读者的语义。

---

## 16. 复审记录（修订 1，保留）

修订 1 对照的是远野的原话，以及仓库中可查到的他一贯的明确约束。以下保留当时的结论，括注修订 3 的变化。

**A. 一致**
- A1 否决超时：AGENTS.md；`development-contract.md` §0、:91；`main-agent-runtime.md:544-550`。
- A2 复用事件流：`development-contract.md:49`、§8；`local-agent-connector.zh-CN.md:312-313`。
- A3 原 ID 查证、不重发。
- A4 不自动批准：`main-agent-runtime.md:466`。
- A5 协议硬切：10-19 人物合并任务书 :25（当时是"用户明确允许"）、10-23 本机 Agent 任务书 :229、`upgrade-code-deletion-20261023.md`。
- A6 E2E 先列失败场景。
- A7 不拆会话锁。

**B. 已修改**
- B1 撤回"保留 RPC"。
- B2 补充 C2。（修订 3 已连同 C1 一并撤销，见 §18。）
- B3 Q5 改为中立。（修订 3 改为"按证据分级"。）
- B4 唤醒规则改写成删除条件。（修订 3 改为只扩大到权限／提问，并分形态写明。）
- B5 合并发布。
- B6 共同约束可选。（修订 3 建议暂不追加。）

**C. 证据不足**
- 远野对 Claude 后台自发回合的偏好；
- 他是否接受模型行为变化；
- SDK 字段的实际输出；
- 新 Main 回复未实测；
- L11 的风险。

**D. 遗漏补充**：L13、Desktop 范围、现场事实。

---

## 17. 修订 2：桌面端任务工具调用展示改造

### 17.1 基线与调查方式【事实】

- **代码版本**
  - 服务端 `origin/main` 为 `77e3b508`。
  - 桌面端 `Yuki-Desktop` 的本地 main 与 `origin/main` 都是 `a5687a6`（2026-11-18 `git fetch` 后核对）。
  - 桌面端固定的 Client API 合同来自服务端 `df133fbc`（4.0.2），apiRevision 12（`contracts/source.json`）。
- **调查方式**：只读源码；只读查询生产库 `runtime_work_effects`，只统计工具名、字段名和字段类型，不读参数和结果正文。

### 17.2 现状数据链【事实】

1. **数据源**：`GET /client/v1/works/{id}/steps`（`client_api/http.py:546-550` → `works.py:520-560`）。只读取该 Work 的 `runtime_work_effects` 中 `kind='tool'` 的行。访问权限：Work 的 `actor_person_id` 等于当前 Person（`works.py:62` 的 `personal_work`）。
2. **投影**：`client_api/work_projection.py:43-119` 的 `public_step` 返回：`step_id`（HMAC 不透明 ID）、`category`、`label`、`record`、`status`、`created_at`／`updated_at`、`artifacts`。
   - 分类在 `:67-93` 中按工具名逐个硬编码成 11 类：terminal、search、web、file、agent、device、memory、message、computer、browser、other。
   - `label` 是写死的繁体中文。
   - 文件头注明 "without tool payloads"。
   - 现行合同（`docs/architecture/client-api.md:32`）：不返回私有工具参数和原内部调用键；DTO 由服务端统一导出，客户端不再维护另一份产品模型。
3. **桌面端渲染**：`Yuki-Desktop/src/renderer/views/Works.tsx:60-77, 424-447`。
   - 桌面端自己维护一份同样的 11 类清单（`stepCategories`）。
   - 已知分类显示本地化名称（`src/shared/locales/*/works.ts:52-65`），未知分类回退到服务端的繁体 `label`。
   - 每行只显示"分类名 + 状态 + 时间"。服务端已经返回的 `artifacts`、`record`、`created_at` 没有展示，也没有链接到外部 Agent 操作、终端或子工作。
4. **桌面端另有一套外部 Agent 视图**：「这台电脑」页的 `ConnectorTasks.tsx`／`NativeRequest.tsx` 通过 `src/main/app.ts:540-576` 读取服务端的 `/agents/v1/operations?work_id=…` 和 `/operations/{id}`（访问权限按设备归属的 Person，`external_agents/http.py:126-170`）。它和任务步骤之间没有互通。

### 17.3 生产实际分布（只读统计）【事实】

| 范围 | 工具步骤数 | "其他"占比 | "其他"中最多的工具 |
|---|---|---|---|
| 全量 | 10,609 | 44% | `manage_task` 3064、`get_schedules` 407、`wait` 310、`time_get_current` 292、`read_chat` 249、`read_result` 172、`get_environment` 145 |
| 11-01 起（UTC） | 4,221 | 24% | `manage_task` 433、`wait` 310、`get_schedules` 95、`read_chat` 69、`get_environment` 52 |
| 11-15 起（UTC） | 2,060 | 21% | `wait` 299、`manage_task` 55、`read_chat` 53、`get_tasks` 10、`find_contacts` 9 |

以事故工作 `d8cc09a4` 为例，71 个步骤中：
- `wait`（18）、`manage_task`（3）、`start_subtask`（1）共 22 个显示为"其他"；
- `use_coding_agent`（10）和 `get_coding_agents`（5）都显示成"委托其他 Agent"。

这与远野的描述一致。

### 17.4 问题清单

| 编号 | 问题 | 证据 | 性质 |
|---|---|---|---|
| D1 | 分类按工具名硬编码，现役的 `wait`、`manage_task`、`read_chat`、`get_tasks`、`find_contacts`、`get_environment`、`message_task`、`start_subtask`、`get_schedules`、`manage_schedule`、`set_short_note`、`read_result` 都没有分类 | `work_projection.py:67-93`；§17.3 | 事实 |
| D2 | 只读的 `get_coding_agents` 和 `use_coding_agent` 的各种操作同显示为"委托其他 Agent" | `:80-81` | 事实 |
| D3 | 只显示分类、状态、时间，没有对象、摘要、耗时和链接；`artifacts` 未展示 | `Works.tsx:432-447` | 事实 |
| D4 | 分类清单两端各维护一份；服务端 `label` 写死繁体 | 同上 | 事实 |
| D5 | 任务步骤和外部 Agent 操作及其事件隔开 | `Works.tsx`；`app.ts:540-576` | 事实 |
| D6 | 参数只存摘要哈希；`action` 只有 `use_coding_agent`／`get_coding_agents`／`local_device` 记录（`effect_outcomes.py:238-242`）；`result` 是交给模型的结果正文，属于私有内容 | 生产回执字段统计；`effect_outcomes.py:225-245` | 事实 |
| D7 | 已有的可安全展示的结构化字段从未投影：`run_command` 的 `process.exit_code`／`status`／`output_lost` 与 `device_id`；`send` 的 `delivery_target{kind,id}` 与 `delivered_message`；`read_chat` 的 `source_event_ids`（可以只给条数）；`use_coding_agent` 的 `action`／`operation_id`／`session_id`；`error_code`、`pending`、`uncertain` | 生产回执字段统计 | 事实 |

### 17.5 改造方案

**原则**
- 服务端是唯一的分类来源。
- 只投影结构化事实，不投影正文。
- 明细通过链接到自带权限核验的已有资源页查看。
- 新工具自动获得分类。

#### T1 服务端投影（不依赖事件化，可以先发）

1. **分类从工具声明派生，删除逐工具名的硬编码。**
   - 现役核心工具在声明中带有 `namespace` 和 `effect`。核心工具缺少这两项时，`capabilities/provider.py:88-89` 不会为它生成能力描述。
   - 历史名称先经 `RECORDED_TOOL_NAMES`（`effect_outcomes.py:48-74`）映射到当前工具。
   - 投影返回：`kind`（稳定的、面向用户的、可扩展的封闭枚举）、`tool`、`effect`（read/write）、有记录时返回 `action`。
   - 建议的 `kind` 词表（由 QD2 定）：terminal、computer、browser、message、chat、search、web、file、agent_read、agent_control、memory、schedule、task、wait、environment、plugin（附插件显示名）、device_legacy。
   - 已移除、也没有映射的工具，返回 `kind="retired"` 并保留原名。
   - 插件工具按声明的 namespace 处理（默认 `plugin.<id>`，`provider.py:96`）。
   - 运行时类工具的 namespace，实施时逐个核对【待核】。
2. **结构化 `detail`**，只放已有的结构化字段：

   | 类型 | detail 内容 |
   |---|---|
   | terminal | 执行位置（Yuki 自己的电脑，或本人设备名；只解析查看者本人同人族名下的设备）、退出码、`output_lost`、`run_id` 链接 |
   | message | 是否送达、目标类型；目标会话名按 QD3 |
   | chat | 读取条数 |
   | agent_read／agent_control | `action`、`operation_id` 链接、取自操作回执的当前状态（访问权限同 `/agents/v1/operations`） |
   | wait | 条件种类。可按 `runtime_work_waits.call_key = "wait:{work}:" + sha256(调用键)`（`work_control.py:1131`）关联，调用键是否就是该效果的 `operation_id`【待核】；也可以复用现有 `wait_view`（`work_projection.py:149-192`） |
   | task | 子工作链接，按 `parent_work_id` 关联；`start_subtask` 回执里的 `run_id` 为空，不能用 |
   | file | 现有 `artifacts` |
   | 全部 | `error_code`、耗时 |

3. **从现在起记录 action（QD4）。** 删除 `effect_outcomes.py:238-242` 的工具名白名单。历史记录不回填。
4. **`label` 字段保留**，作为旧桌面的回退。

#### T2 外部 Agent 实时状态（随事件化发布；修订 3 已按 §7 改写）

- `agent_control` 步骤显示 §7.5 的分级状态，以及 §7.2 的控制结算结果：控制是否被接纳、影响了哪个目标、目标是否已经结束。
- 只有在 §7.4 能证明请求"仍可回答"时，步骤上才标出"等待你回答"，并链接到 `NativeRequest`。已撤回、已回答、owner 已失联的请求，显示真实状态，不提示去回答。
- 操作回执或事件变化时，是否会发出 `work_steps` 提示【待核】。如果不会，就在现有提示通道上增加一种原因，不加轮询。
- 归属未知的会话级事件（§7.3 第 4 点）不出现在任务步骤里。是否在「这台电脑」中提供会话级入口，见 Q4／Q12。

#### T3 桌面端（Yuki-Desktop）

1. 删除 `stepCategories`，改用生成合同中的 `kind` 枚举。en、zh-Hans、zh-Hant 三种语言都配文案。遇到未知 `kind` 时显示"工具：{tool}"。
2. 每一行显示：分类与动作、一句结构化摘要、状态、时间。点击后展开 `detail` 并提供链接。
3. 是否折叠，由 QD2 决定。

### 17.6 边界与隐私

- 不返回参数正文和结果正文（QD1 为例外开关）；不返回内部调用键。
- 访问权限沿用现有 Work ACL：来源 actor 与查看者属于同一人物族，**或** Work 所在私聊的所有者与查看者属于同一人物族（`client_api/works.py:57-67` 的 `work_access`）。修订 5 更正：原文写"只有 actor 本人"是错的。超管看不到更多。能读 Work 的人未必能读目标设备、Agent 操作或发送会话，所以每一项跨资源 `detail` 都要在**生成之前**核对该资源当前的读取权限，不能等点开链接时才拒绝（Codex 终审 P0-4）。
- 链接不是权限：每个资源页各自核验。设备名只显示查看者本人设备的；会话名只在查看者能读该会话时显示。
- 执行 trace 只对管理端开放（权限 `control.execution.content.read`），本方案不向个人客户端开放。
- 历史不改写、不回填。

### 17.7 兼容性

- Client API 从 revision 12 升到 13，只新增字段，保留 `category`／`label`／`record`／`status`。这一部分没有数据库迁移。
- 旧桌面连新服务端：仍按旧词表显示，不会变差。
- 新桌面连旧服务端：缺少 `kind` 时回退旧逻辑。如果远野选择要求 revision 13，见 QD5。
- 合同同步：按 `contracts/source.json` 固定到新的服务端提交，重新生成 `contract.gen.ts`。
- T1 和 T3 不需要协议 5，不受硬切影响（QD6）。T2 依赖 §7。

### 17.8 测试与验收

**失败场景**

| 编号 | 场景 |
|---|---|
| DS1 | 每个现役核心工具，加一个插件工具 |
| DS2 | 历史名称 |
| DS3 | 已移除且无映射的工具 |
| DS4 | 没有记录 action 的旧回执 |
| DS5 | running、unknown、recorded 三种状态 |
| DS6 | 参数和正文中埋入标记，检查是否泄露 |
| DS7 | （修订 5 更正）分别验证原 actor、同人物族、私聊所有者、真正无权者（返回 404），以及"能读 Work 但不能读目标设备或会话"的人看不到这些跨资源明细 |
| DS8 | 设备不属于查看者 |
| DS9 | 查看者无权读取发送目标会话 |
| DS10 | 外部 Agent 操作的各级状态和"等待你回答"（T2，依 §7.4） |
| DS11 | 本案 S01 |
| DS12 | 新旧版本互连 |

**验收方式**
- **服务端 E2E**：导出 `/steps` 的 JSON 快照作为产物。现役核心工具不得出现在 `other`／`retired` 中；不得泄露埋入的标记；访问权限必须正确。
- **桌面端 E2E**：用 Playwright 覆盖三种语言的渲染和点击跳转，产出截图和 DOM 快照。
- **生产对照**：重跑 §17.3 的统计，近 7 天现役工具的"其他"占比为 0，报告作为产物。
- **联合验收**：S01、S02 场景中，任务步骤上的"等待你回答"只针对仍可回答的原请求。

### 17.9 风险

- 词表写进合同后，改名就是合同变更，所以 `kind` 要稳定、要粗。
- 设备名和会话名不能解析时只显示类型。
- 历史记录没有 action。
- 多种资源的权限核验要批量只读完成，避免 N+1 查询。

### 17.10 修订 2 的事实与推断

- **事实**：§17.1–17.4。
- **待核**：wait 调用键的关联方式；运行时类工具的 namespace；`work_steps` 提示的触发条件。
- **推断**：新旧版本互连的兼容行为。

### 17.11 修订 3 对 §17 的影响

- Codex 复审没有否定 §17.1–17.4 的事实。
- T2 已按 §7.2、§7.4、§7.5 改写：不能把"已登记""可见"显示成"已接纳""可回答"。
- 阶段安排见 §9（阶段 3）。

---

## 18. Codex 独立复审核证与处理（修订 3）

**原文**：[Codex-事件化任务书独立复审原文.md](Codex-事件化任务书独立复审原文.md)。

**核证方式**：只读重新打开 Codex 引用的 transcript 行、源码行和本机 SDK 文件。没有连接生产，没有调用原生 Agent，没有运行测试。

| # | Codex 意见 | 核证结果 | 处理 | 理由／落点 |
|---|---|---|---|---|
| 1 | 同一 task `bxogl883w` 先 stopped（第 1005 行）、后 completed（第 1011 行），第二次通知之后才有 Bash | transcript 复核一致 | 采纳 | §1、§4；也是撤销 C1 的依据之一 |
| 2 | "卡在无人收到的权限回调"只是推断；"永久"一词过强 | 没有 can_use_tool 原始帧；驱动退出时 future 会被取消，`claude.py:273-286` 复核一致。"SDK 处理原生撤回"本次未单独核对 | 采纳 | §1、§4 降为推断，删去"永久" |
| 3 | "确定未执行"不成立，应写成"未见开始执行，回执未知" | 是证据范围问题，成立 | 采纳 | §1、§14 |
| 4 | 处置只证明 SDK owner 退出后原 RPC 返回，不能证明"WSS 一断就解卡"；outbound 记录只能证明 Bot 记录了发送 | 成立 | 采纳 | L1 改写；§1 注明投递回执未核 |
| 5 | L3 的准确范围、L3a 的后果、L4/L5 依赖 watch、L7 是反事实推断、L9 两条查询路径、L10 Codex 还有其他等待、L11 措辞、L12 写明日志范围 | 逐条对源码复核一致 | 采纳 | §3 逐行修正 |
| 6 | P0：`send` 本身会等 `_send_lock` 和 socket 写入；`get(operation_id)` 先做在线 RPC | `transport.py:29-47`、`service.py:277-300` 复核一致 | 采纳 | 新增 L1b；§7.1 把边界扩大到整个派发路径；V3 改写 |
| 7 | P0：C1 撞上单执行绑定，导致 1011 重连循环；C2 删两行无效；Codex 的空集合分支负责缓冲；`scheduled-trigger` 是 subkind；SDK 自己说空任务集合不代表运行结束 | `repository.py:54-63, 533-554`、`http.py:323-330`、`claude.py:115-124`、`codex.py:675-711`、SDK `types.py:1301`、`_internal/query.py:870-886` 复核一致 | 采纳 | 新增 L14；撤销 C1/C2；§7.3 重写；Q9 |
| 8 | 建议保留原绑定、显式表达因果后续执行、证据不足就标未知、不自动切 C2 | 与现行归属合同（`main-agent-runtime.md:464`）一致 | 采纳 | §7.3。另补充：CLI 的 `session_state_changed` 可作为原生事实来源（核 SDK `_internal/query.py:244-249, 446-490`，实际输出待验证） |
| 9 | P0：控制命令没有 run；`_control_result`／`_detached` 依赖同步回复；`pending:false` 不进输入；清理路径没有 prepare；journal 只占序号 | `repository.py:460, 816`、`service.py:652-735, 740-800`、`journal.py:215-221` 复核一致 | 采纳 | §2、§7.2 |
| 10 | P0：§2 说"无操作事件被丢弃"不准确；可保存、可发现、可回答、原生已收到是四件不同的事；`connection_id` 属于 SDK 会话；扇出时只能消费一次；没有 session 索引；Desktop 只看 operation；S13 撤权后应拒绝新上传 | `repository.py:719-800, 967-996`、`claude.py:40-48, 135-180, 443-505`、`db_models.py:77`、Desktop `ConnectorTasks.tsx:471` 复核一致 | 采纳 | §2 改正；§7.4；§8；S13 |
| 11 | P1：空回执现状为 unknown/uncertain，而模型视图过滤掉这两项；需要七级状态；`previous_sdk_owner_not_attached` 不能证明进程退出；查询会产生进度，有反馈环风险 | `repository.py:354-372`、`tools.py:144-160`、`work_journal.py:661-730`、`claude.py:87-102`、`connector.py:502-512` 复核一致 | 采纳 | §7.5；撤回修订 1 §7.5 中"该原因说明进程已退出"的说法 |
| 12 | P1：L13 收尾覆盖不全（离线、未记账），`_detached` 写的 admitted 可能误导 | `connector.py:232-235, 513-525`、`service.py:692-735` 复核一致 | 采纳 | L13 扩写；§7.6；Q11 |
| 13 | P1：L8 有三支；只等消息时为 waiting_user；先到后等的情况；`_deliver` 会消费一次性等待 | `work_wait.py:297, 394-426, 492`、`work_control.py:1120-1126` 复核一致 | 采纳 | L8 改写；§7.7；撤回修订 1"删除第 411–426 行条件"的建议 |
| 14 | P1：释放锁后回复的不一定是新 Main，会先匹配等待 | `chat.py:592-612` 复核一致 | 采纳 | §5；V1 改为两条路由分别验收 |
| 15 | P1：UUID 修复须覆盖所有写接纳的位置；并发 resume 可能建出两个 owner | `claude.py:104-106, 182-225`、`journal.py:39-50` 复核一致 | 采纳 | §7.8；新增 L15（推断） |
| 16 | P1：cancel 必须证明目标；旧事件仍会生成输入 | `claude.py:468-485`、`repository.py:809-837` 复核一致 | 采纳 | §7.8；新增 L16 |
| 17 | Q1–Q13 的建议 | 逐条对照上述核证 | 采纳为"复审后建议"，不视为决定 | §13。Q6 补充"整个设备 WSS 不可用"的代价（`connector.py:713`）；Q13 改为"本轮保持现场" |
| 18 | 验收重写：现有夹具绕过入队时序；按 ID 串联产物；三条判别条件 | `test_external_agent_bridge.py:104`、`tests/test_claude_driver.py:38-60` 复核一致 | 采纳 | §11 重写 |
| 19 | "建议退回修订 1，修改后再作为实施依据" | 属于流程意见 | 部分采纳 | 本书以修订 3 回应。是否可以作为实施依据，由远野审阅决定，本书不自行宣布 |
| 20 | 基线说明：本地 main 为 `8e6594cf`，与 `77e3b508` 之间相关路径无差异 | 与本书基线不冲突 | 采纳 | 基线段落 |

**未采纳**：没有完全不采纳的条目。第 19 条只是部分采纳，理由是：能否作为实施依据，不由本书判断。

**仍未决**：Q1–Q13、QD1–QD6；§9 阶段 0 的 SDK 实验；§15 列出的待验证项。

---

## 19. 修订 4：向运行中的 Claude 回合追加指令（steer／queue）

**远野原话**：「不支持直接追加指令的問題，任務書是不是有納入修復」

**回答**：修订 3 之前没有纳入。修订 3 只在 §7.8 中写了"Claude 当前明确不支持精确 queue/steer，保留能力差异"，没有调查根因，也没有给出修复。本节补上。

**取证范围**
- 只读源码（服务端、连接器、Desktop 未涉及）。
- 本机已安装的 SDK 0.2.172 源文件。
- 本机 CLI 2.1.311 二进制的静态字符串匹配，没有运行。
- 只读事故会话 `e4e2d50f` 的 transcript，只看记录类型、时间和长度，不看正文。
- 本次会话中已经取得的只读结果。

本节**没有**扫描其他会话的 transcript，也**没有**再次查询生产库。（修订 6 删去原写的"为尊重被拒绝的两次请求"：那两次请求被拒的来源与含义未核实，不作为任何决定的依据。）

### 19.1 分层结论

| 层 | 能力 | 证据 | 性质 |
|---|---|---|---|
| **Claude CLI 原生** | 有消息队列。回合进行中到达的消息会先入队。在交互式 CLI 中，排队的人类消息会在**下一次工具结果之后**以 `queued_command` 附件并入**同一回合**的下一次模型请求：transcript 第 308–315 行，16:35:43 入队，16:35:45 `remove`，附件 `commandMode:"prompt"`、`origin.kind:"human"`、`humanTurn:true`，下一次请求同时包含 tool_result 和这段文本。任务通知也以同样方式并入（第 616–622 行）。回合空闲时，排队消息由 `dequeue` 开启新回合（第 807–808、940–941 行） | 事故 transcript | 交互式 CLI（entrypoint=cli）下为**事实** |
| 同上，SDK 模式 | 同一会话改由 SDK 驱动（entrypoint=sdk-py）后，01:39:34 的 prompt 同样**入队**（第 1022 行）；但当时前一回合停在 Bash 上，从未到达工具边界，所以没有并入 | 同上 | 入队是事实；SDK 模式下回合中途是否并入【待验证】 |
| 同上，限制 | 只能在工具边界并入：回合卡在权限回调或一个很长的工具上时，追加的输入会一直排着。CLI 内部有 now/next/later 优先级和"立即发送"之类的逻辑（二进制静态字符串） | 静态字符串 | 未公开，不是合同（Q16） |
| **SDK 0.2.172** | 公开接口只有：`query()` 写入一条用户消息，写进 stdin 即返回；`interrupt()` 中断当前回合；`stop_task(task_id)`。控制请求类型只有 interrupt、can_use_tool、initialize、set_permission_mode、hook_callback、mcp_message、rewind_files、mcp_reconnect、mcp_toggle、stop_task（`types.py:2798-2866`）。**没有**"指定回合追加""撤回已排队消息""并入了哪个回合的回执"。可选参数 `replay-user-messages` 会回放带 uuid 的用户消息（`client.py:367-380`；CLI 静态字符串中有 `isReplay:!0` 和 `uuid:r.uuid`）。回放时机，以及回放能否区分"并入当前回合"和"开启新回合"【待验证】 | SDK 源码；CLI 静态字符串 | 公开接口为事实，回放语义待验证 |
| **连接器 Claude 驱动** | 对 `queue`／`steer` 一律返回 `unsupported_action`（`claude.py:424-427`）。连接器自己发起的回合进行中时，`prompt` 返回 `conflict`（`claude.py:398-399`）。CLI 自发回合进行中时，`prompt` 会被 CLI 静默排队（L4，即本次事故）。实例目录上报的能力为 list/get/watch/prompt/respond/cancel/detach（`claude.py:52-60`；本次会话早前查询的目录与此一致） | 源码；现场 | 事实 |
| **服务端／工具** | `use_coding_agent` 的说明对所有实例都声明 queue 和 steer（`tools.py:55-68`）。目录会把实例的 capabilities 返回给模型（`service.py:153-183`），但 `execute` 不按能力拦截（`service.py:413-659`），会先登记操作，再往返设备，才拿到拒绝。现场证据：17:50:48，Yuki 对本次调查会话执行 steer，操作 `fe56ccac` 返回 `admitted:false`、`unsupported_action`、"This SDK does not provide precise native steer/queue" | 源码；本次会话早前取得的只读结果 | 事实 |
| **Codex（对照）** | 精确 steer：`turn/steer` 带 `expectedTurnId`，回合 ID 不符时由原生拒绝。原生队列：`thread/queue/add` 返回 `queuedSubmission.id`，可以按 ID 取消（`codex.py:1053-1100`） | 源码 | 事实 |

**根因**
- Claude 不能 steer，**不是**原生完全不能追加：交互式 CLI 已经证明可以在工具边界并入。
- 真正的原因有三点：
  1. 公开的 SDK 合同中，没有"把输入绑定到指定回合"、"确认并入了哪个回合"、"撤回已排队输入"这三项能力；
  2. 连接器据此选择拒绝，这与现行合同"不支持时不暗中改成取消重启或新建会话"（`local-agent-connector.zh-CN.md:126`）一致；
  3. 工具层对所有实例都声明了这两个动作，服务端又不按能力拦截，模型要往返一次设备之后才知道不支持。
- 事故中"prompt 被静默排队"的直接原因是 L4（驱动不知道会话正忙），不是 steer 本身。

### 19.2 能不能"准确追加到正在运行的回合"

- **依赖公开 SDK，目前不能做到 Codex 那样的精确 steer。** 也就是"只追加到指定回合，回合已变则拒绝"。
- **能做到的是"追加输入，由原生决定落点"。** 输入写入后，可能在当前回合的下一个工具边界并入，也可能在当前回合结束后开启新回合。如果当前回合停在权限请求或长工具上，输入会一直排着。
- 落点只能事后确认，可选依据有：
  - `replay-user-messages` 的回放【待验证】；
  - `session_state_changed`【待验证】；
  - 本机 transcript 中带同一 uuid 的 `remove`／`queued_command` 或 `dequeue`。transcript 是只读本地文件，但它的格式不是公开合同，只能作为辅助证据。

### 19.3 语义：结果未知、排队、并入、取消

| 状态 | 证据 | Yuki 侧怎么处理 |
|---|---|---|
| 已登记／已写出 | §7.1、§7.5 | 不重发；按原操作 ID 查询 |
| 已写出但没有回执 | 没有回放，也没有其他原生事实 | 保持"待确认"，**不能**改判为失败或可重发 |
| 原生排队中 | 写出成功；会话忙的事实（`session_state_changed=running/requires_action`【待验证】）；尚无回放 | 报 `queued`，并写明"Claude 无法撤回单条排队输入"。如果会话状态是 requires_action，说明排在一个等权限的回合后面，要提示先处理权限（§7.4） |
| 并入当前回合 | 回放或 transcript 能把本次 uuid 与当前 execution 对上 | 记为"已并入执行 X"，不新建执行 |
| 开启新回合 | `dequeue` 后出现新的 execution | 新 execution 归属本次追加操作（§7.3 的因果后续执行） |
| 取消 | 只有 `interrupt()`，而它作用于**当前回合** | 不能把它当成"撤回排队输入"。中断之后排队输入是否还会执行【待验证 X5】。按 §7.8 报告实际中断了哪个目标 |

### 19.4 阶段 0 追加实验（SDK 模式，CLI 2.1.311，隔离目录，不碰用户会话）

| 编号 | 实验 | 要取得的事实 |
|---|---|---|
| X1 | 运行一个包含多次工具调用的回合，期间用 `query()` 追加一条带 uuid 的消息 | SDK 模式下是否在下一个工具边界并入；stdout 是否回放、何时回放；`session_state_changed` 的变化序列 |
| X2 | 回合只有一个长工具 | 追加的消息是否一直排着，工具结束后是否并入 |
| X3 | 回合停在权限回调上 | 追加的消息是否一直排着；回答权限后是否并入（复现本次事故的形态） |
| X4 | 回合在追加之后、到达工具边界之前就结束 | 是否以 `dequeue` 开启新回合，新回合能否对应到本次 uuid |
| X5 | 有排队消息时执行 `interrupt()` | 排队消息是被丢弃、被保留，还是立即执行 |
| X6 | 连续追加两条 | 顺序是否保持；是否合并为同一次请求 |

实验产物：stdout 帧序列（类型、uuid、时间）、对应 transcript 记录的类型和时间（不含正文）、执行命令。实验结论决定 Q14 能否从"追加输入"提升为"精确 steer"。

### 19.5 方案比较与推荐

| 方案 | 内容 | 优点 | 限制 | 结论 |
|---|---|---|---|---|
| A 维持现状 | Claude 继续拒绝 steer 和 queue | 语义诚实 | 远野的需求得不到满足；模型要往返设备才知道 | 不推荐单独采用 |
| **B 追加输入（推荐，作为 Claude 的 `queue`）** | 用 `query()` 写入一条带 uuid 的用户消息，按 §19.3 如实报告各状态 | 用的是公开接口；与 CLI 原生行为一致；不中断正在进行的工作 | 落点由原生决定，只能事后确认；不能撤回；回合卡住时会一直排着 | **推荐**，前提是 X1、X4 证明落点能事后确认。如果确认不了，状态只能停在"已写出、落点未知"，需要远野决定是否仍然开放（Q14） |
| C 中断后重发 | `interrupt()`，等原生终态后再 `prompt` 一条包含修正的指令 | 语义明确 | 会丢掉当前回合的进行中工作；在事故那种形态下可能中断错的回合；需要两次操作 | 可以作为显式的"纠偏并重来"组合，由模型或用户明确选择，不作为 steer 的隐式替代 |
| D 使用未公开的 priority:"now" | 依赖 CLI 内部字段 | 可能实现立即插入 | 未公开、不稳定 | **否决**（Q16） |
| E 精确 steer | 只有当阶段 0 实验证明能把输入绑定到指定回合（回合已变就拒绝或报告）时才提供 | 与 Codex 语义一致 | 目前没有证据 | 等待实验结果 |

**配套修改**
1. **服务端按能力拦截（Q15）。** `execute` 按实例目录中的 capabilities 判断，直接返回 `unsupported_action` 和该实例支持的替代动作（例如 Claude 没有 steer 时提示 queue 或 cancel+prompt），不登记操作，也不往返设备。目录以连接器最新上报为准，连接器仍然是最终裁决。
2. **工具说明。** 不再给所有实例统一声明 steer。说明改为"可用动作以目录中该实例的 capabilities 为准"。这一点与固定工具合同（`development-contract.md:51`：声明可见不等于授权）一致：工具声明不变，执行处核验能力。
3. **连接器。**
   - Claude 的 capabilities 增加 `queue`，并附语义标注，例如 `queue_semantics:"native_decides_turn"`、`queue_withdraw:false`。只有方案 E 成立时才增加 `steer`。
   - 追加输入同样要遵守 §7.8：按 uuid 接纳、同一会话的打开串行、回合忙时的判断。
   - 回执走 §7.2 的事件。
4. **Desktop（§17）。** 追加操作的步骤显示"已追加，落点：当前回合／新回合／未知"，不显示"已纠偏"。

### 19.6 端到端验收（在 §11 基础上新增）

| 编号 | 场景 | 通过条件 |
|---|---|---|
| ST1 | X1 的形态：多工具回合中追加 | 回执随原生事实依次推进为 queued、并入执行 X；Work 只收到一次结果；产物中能按 uuid 串起原生帧、journal、服务端事件和 Work 输入 |
| ST2 | X3 的形态：卡在权限上时追加 | 不挂起激活（V3）；状态为 queued，并提示前面有待处理的权限请求；回答权限后才并入；没有重发 |
| ST3 | X4 的形态：回合在追加前结束 | 新 execution 归属本次追加操作；原回合终态不变 |
| ST4 | 有排队输入时取消 | 报告实际中断的是哪个回合；排队输入的去向以 X5 的事实为准，不伪造"已撤回" |
| ST5 | 对 Claude 发 steer | 服务端立即返回 `unsupported_action` 和替代动作，没有往返设备 |
| ST6 | Codex 的 steer 和 queue 回归 | `expectedTurnId` 不符时被拒绝；排队可以按 ID 取消 |
| ST7 | 有排队输入时连接器重启 | 回执为 unknown；不重发 |
| ST8 | 两个 Work 同时向同一 Claude 会话追加 | 顺序与归属可以区分；不串到另一个 Work |

### 19.7 证据边界与待验证

- **事实**：交互式 CLI 在工具边界并入排队消息（事故 transcript）；SDK 模式下会入队；SDK 的公开接口；连接器拒绝 steer/queue；服务端不拦截；现场 steer 被拒；Codex 的精确语义。
- **待验证**：X1–X6；回放的时机和语义；`session_state_changed` 在 SDK 模式下是否发出。
- **不作依据**：CLI 内部的 priority 字段；transcript 文件格式只作辅助证据。
- **未纳入的证据**：其他会话的 transcript；生产库中其余 steer/queue 操作的统计。（修订 6 删去原写的"请求被拒"：相应请求被拒的来源与含义未核实，不作为任何决定的依据。）

---

## 20. 修订 5：Codex 终审核证、本轮实施与剩余范围

### 20.1 Codex 终审逐项处理

原文：`/tmp/yuki-codex-event-review-final.md`（当时的本机临时文件，未入库）。下表中"核证"栏是本轮重新读代码、transcript 和 SDK 的结果。

| 项 | Codex 意见 | 核证 | 处理 |
|---|---|---|---|
| P0-1 | "补发未写出"与"未知不重发"不能同时成立；写完 socket 到提交时间戳之间崩溃，无法区分是否已送达 | 成立。`prepare` 同时写 `created_at` 与 `dispatched_at`（`repository.py:481-495`）；`journal.begin` 只在同一份 journal 内对同 ID 去重 | **采纳并改设计**：本轮**不做任何自动补发**。已登记但回执为空的操作，只在设备重连后按原 ID 查证（`get`）；设备答 not_found 只记诊断，不算成功，也不重发。原生投递不明时，Work 可以显式 detach 退出（§20.2 B3） |
| P0-2 | §19 只支持"向会话写入"，不足以支持 `queued`、并入或精确 steer；808／941 行不是交互式 CLI 的证据；"公开接口只有三种"不准确；时间应按 UTC+8 写 | 成立。308 行时间 `16:35:43Z` 即 UTC+8 次日 00:35:43；808、941 行的入口是 `sdk-py`，且没有输入 UUID；SDK 客户端还有 `set_model` 等方法 | **采纳**：§19 中这几处结论以本表为准。`queued` 必须有该条输入自己的原生入队证据；精确 steer 的条件是原生在产生效果**之前**校验目标回合。Claude 追加语义（Q14）本轮不实现 |
| P0-3 | 从 Desktop 对已有操作追加时可能绑错执行；B 的追加可能让 B 取消 A | 成立。新 queue 操作会继承原操作的 `native_execution_id`（`service.py` 原 675-683 行）并预先绑定 | **部分实施**：新的 prompt/queue 不再预先绑定目标操作的原生执行，只用它定位会话（§20.2 B4）。完整的四方归属表（提交操作、所选目标、实际执行、执行所属 Work）和权限扇出仍待做（§20.4） |
| P0-4 | §17 写错步骤 ACL | 成立（`works.py:57-67`） | **采纳**：§17.6、DS7 已更正。Desktop 分类实施留在后续 PR（§20.4） |
| P1 权限有效性 | 不能用操作总序号判断权限是否仍有效；旧 owner 迟退可能误清新 owner | 读代码成立（`claude.py:135-180, 273-286`） | 留待连接器第二阶段（§20.4） |
| P1 控制结算 | 控制完成、目标终态、Work 退出要分别结算 | 成立 | 本轮控制类动作仍同步执行（理由见 §20.3），现有 `_control_result` 结算不变 |
| P1 Q15 | 按最新目录拒绝会误伤历史操作 | 成立：本轮第一版就曾按已移除实例的过期目录误拒，被集成测试发现 | **采纳**：只认**当前连接**对当前可用实例的声明，原操作的控制不拦（§20.2 B5） |
| P1 §17 分类来源 | 分类不能全从今天的声明推出；`wait_view` 只读当前 active wait；子任务不能只按 `parent_work_id` 关联等 | 读代码成立 | 并入 §17 后续实施，本轮未改 |
| P1 T2 刷新 | `steps_token` 不随原生操作变化；旧 Desktop 不认识新状态会显示 running；Desktop 最低连接版本实为 11 | 读代码成立（`Yuki-Desktop/src/shared/api.ts:8`） | 并入 §17 后续实施 |
| 验收表 | V1/V3、S04–S07、S08–S13、X1–X6/ST1–ST8、DS1–DS12 的改写 | 同意 | 本轮已覆盖的部分见 §20.2；其余留作后续验收条件 |

### 20.2 本轮实施（隔离工作树，未提交）

| 编号 | 改动 | 位置 | 说明 |
|---|---|---|---|
| B1 | prompt/queue/watch 在 Work 激活里只做登记和来源、设备核验，随后返回原操作的持久视图（`status=unknown`、`pending=true`，带 `run_id`）。写出和等待设备回复交给服务自有的后台任务，回复到达后写入原回执；错误回复照常结算原 run 并产生 completion | `src/yuki/external_agents/service.py` | 写出前在后台再核一次：当前连接、设备授权、原 Work 非终态且 generation 一致、操作未 detach（`repository.dispatchable`）。不符则不写出，回执记为未派发 |
| B2 | 原操作查询（`get_coding_agents` 指定 operation，以及 Desktop 读取 `/agents/v1/operations/{id}`）立即返回持久事实，在线查证放到后台；同一操作同时只有一次查证，派发未完成时不发查证 | 同上 | 设备答 not_found 时作诊断标注（`reconciliation_status`），不是回执 |
| B2b | 设备重连时，只对"回执为空、带 run、Work 仍活着、未 detach"的操作按原 ID 查证；不重发 | `service.connect`、`repository.unconfirmed` | 覆盖 Bot 重启或断线导致回复丢失的窗口 |
| B3 | 显式 detach 时，如果设备离线、或设备确认没有原记录（`original_operation_not_found`），只在服务器解除 Work 所属关系：回执写 `status=released`、`released_without_device=true`、`executed=false`，原 run 以 detached 收尾 | `service._release` | **取舍**：采用 Q11 的推荐（显式解除、保留未知）。系统的来源清理路径仍需要设备，未改 |
| B3b | `_detached` 不再把原 prompt 一律写成 `admitted:true`，改为保留原值 | `service._detached` | 回应 Codex P1：解除关系不能让历史看起来像原 prompt 被接纳过 |
| B4 | 新的 prompt/queue 不再预先绑定目标操作或执行的原生回合 | `service.execute` | 避免把另一操作（或另一 Work）的回合记到新输入名下，也避免回合不同时触发 `execution_mismatch` 和 1011 重连 |
| B5 | 新的 prompt/queue/watch/steer 只在**当前连接**对当前可用实例声明过能力、且该动作不在声明里时，才提前返回 `unsupported_action`（附可用动作）；不登记、不派发。存数据库的旧目录、已移除或离线的实例、原操作的控制都不拦，交给设备裁决 | `service.execute`、`update_catalog`、`transport.DeviceConnection.declared` | 声明来自本次连接的 hello 或最新目录 |
| B6 | `drain()`：等待本进程在途的后台任务（关闭和测试用）。只等待未完成的任务，避免空转 | `service.drain` | 第一版在这里有忙等，测试中出现 100% CPU 卡住，已修复 |
| C1 | 连接器 Claude 驱动：Claude 自己发起的回合（例如后台任务通知）到来时，会话视为忙，新 prompt 立即返回冲突，不再静默写进 CLI 队列、一直等不到接纳 | `packages/yuki-agent-connector/src/yuki_agent_connector/drivers/claude.py` | 对应 L4，是事故里"prompt 只入队"那一环。需要连接器发版并由用户手动更新，不属于 bot-only 上线 |

**测试（在当时的本机临时目录 `/tmp/yuki-dispatch-logs/`（未入库）保存完整日志和真实退出码）**

- **新增 Bot E2E**（真实 TLS/WSS 和 SQLite，`tests/integration/test_external_agent_bridge.py`），覆盖：
  - 设备收到 prompt 不回复时，激活立即返回，重连后按原 ID 查证；
  - 设备答 not_found 时不重发；
  - 迟到的拒绝能结算原 run；
  - 当前声明拦截，能力不明确时交给设备；
  - 过期目录不误拒，原操作的控制不拦；
  - 新输入不认领原回合；
  - 离线或无原记录时显式 detach。

  原实现会卡在第一个用例上，所以测试能区分新旧行为。
- **更新的旧测试**：只把断言改成"等后台事实落库后再断言"，原断言全部保留，并补强了几处（例如重连查证只能针对原操作 ID）。`test_native_offline_detach_keeps_real_device_receipt_boundary` 按 B3 的取舍改写了离线显式 detach 的期望，来源清理路径的期望不变。单元夹具的能力声明改为真实 Codex 实例的完整列表。
- **结果**：
  - Bot 外部 Agent 单元加集成 159 个通过（exit=0）；
  - ruff 通过（exit=0），mypy `src` 无错（exit=0）；
  - 连接器 243 个通过，新 L4 测试修复前失败、修复后通过；
  - 连接器另有 6 个 `test_claude_discovery` 失败：这些测试和被测代码都没有改动，本机在固定位置装有 `/opt/homebrew/bin/claude`，去掉 Homebrew 的 PATH 后仍然失败，属于环境原因，待 CI 核实；
  - 全量单元和集成套件的结果见 §20.5。

### 20.3 设计取舍与理由（按已授权自主决定）

1. **不自动补发（P0-1）**：服务器无法区分"从未送达"和"送达后、写时间戳前崩溃"。遵守"未知不重发"，以显式退出来代替补发。
2. **（修订 6：已被取代，见 §21.2 B7）控制类动作本轮仍同步**：在连接器侧，respond 只等本地回调消费答案，detach 是本地操作，cancel 等 CLI 的中断应答，都不等原生接纳。异步化需要"发起方通知"和"结算完成后再 ACK"两条配套路径，它们属于协议 5 的回复事件。所以本轮 V3（激活不被设备占住）的覆盖范围**只限 prompt/queue/watch 与原操作查询**，这一点在验收中如实写明。
3. **离线时允许显式 detach（Q11）**：否则未知操作会让 Work 永远无法完成（L13）。只解除关系，不宣称原生已停止或已接纳。之后到达的设备事件照常保存，但不再投影到 Work。
4. **能力拦截只认当前连接的声明（Q15）**：Codex 指出目录可能过期，本轮实测也复现了这个问题。
5. **无主权限请求（Q4／Q12）、Claude 追加语义（Q14）**：修订 5 写"两次就此提问都被拒绝，按最保守的默认处理"。**修订 6 更正归属**：那两次提问的结果（`invalid_option`／显示为拒绝）以及 Yuki 为解除阻塞代答的 deny，都**不是用户的决定，也不是否决实施**；本书不据此认定任何用户意向。随后的任务指令要求：无主权限按隐私最小化处理，至多上传必要元信息、不传命令路径或正文，并验证 ACL；运行中追加先在隔离目录做有限费用的真实 CLI 实验，不能把 queue 冒充精确 steer；遇到实质扩大隐私或风险时停下说明。本书只记录这些指令要求，不写成用户已明确授权了具体的隐私、实验或扫描范围。实施结果见 §21。

### 20.4 剩余范围与依赖

（修订 6：本表是修订 5 时的状态，现状以 §21.4、§21.6 为准。）

| 部分 | 内容 | 依赖／发布单元 | 状态 |
|---|---|---|---|
| 控制类异步化（Q3） | answer/steer/cancel/detach 不占激活；发起方通知；控制回执、目标变化、发起 Work 三方结算 | 协议 5 的回复事件（Bot 加连接器） | 未做 |
| 回复走事件流（协议 5） | `journal.complete` 生成正式事件，服务器结算完成后再 ACK，删除改动类命令的 RPC 应答 | Bot 加连接器配套发布；协议硬切（Q6）会让整个设备 WSS 在更新前不可用 | 未做 |
| 跨 Work 归属表（P0-3 剩余） | 区分提交操作、所选目标、实际执行、执行所属 Work；并入他人执行时只给本条输入的处置回执 | Bot 加 Desktop | 未做 |
| 权限有效性（P1） | 按原 SDK owner、session、请求 ID 核对真实待答 callback；只消费一次；旧 owner 迟退不误清新 owner | 连接器 | 未做 |
| Claude 接纳与归属（§7.3、§7.8） | 按 uuid 判断接纳（需先做 X1）、串行化会话的 open/resume、精确 cancel、因果后续执行 | 连接器；SDK 实验需授权 | 只做了 C1 |
| 无主权限入口（Q4／Q12） | 本机可见或上传元数据 | 需要远野决定隐私范围 | 待决 |
| Claude 追加（Q14） | 较弱的"追加输入"语义 | 需要远野决定，并做实验 | 待决 |
| Desktop 分类（§17） | 服务端投影（ACL 先行、只用定义明确的公开字段）、Desktop 渲染 | Bot（可单独上线）加 Desktop 发版 | 未做 |
| 文档 | `local-agent-connector.zh-CN.md`、`main-agent-runtime.md:464-468` 更新为"登记后返回、按原 ID 查证、显式解除" | 与 Bot PR 同发 | 未做 |

### 20.5 发布边界与兼容

（修订 6：本节是修订 5 时的判断，现状以 §21.5 为准。）

- **本轮 Bot 改动（B1–B6）**：兼容现网连接器 0.7.0（协议 4）和现有 Desktop，可以 bot-only 上线；数据库没有迁移。
  - 行为变化一：prompt/queue/watch 返回时回执可能还是 unknown，模型需要按工具说明用 `wait(run)` 等待，或之后再查。
  - 行为变化二：离线显式 detach 会解除 Work 所属关系。
- **连接器改动（C1）**：要连接器发版，经 Desktop 随附，由用户点击更新，不属于 bot-only 上线。它与协议 4 兼容，可以单独发布。
- **上线前条件**：完整修复所需的剩余部分（§20.4）尚未完成，本轮成果**不能当作完整修复上线**。是否把 B1–B6 作为"防卡第一阶段"单独合并上线，取决于：
  1. 全量测试和 CI 通过；
  2. 独立审查结论；
  3. 远野是否接受这个分阶段方案。

---

## 21. 修订 6：第一阶段（防卡）实施、验证与发布边界

### 21.1 交付决定与归属

- **来源**：本节依据的分阶段决定，是协调者在 2026-11-18 转达的用户决定，原文要点如下：
  - 先上线解决卡死的部分，作为第一阶段；
  - 合并前必须完成：三路独立复审并处理发现的问题、file_copy 定性、本节与修订 6、所列小问题的修复、跨版本 E2E 进 CI（或作为手动产物附证据）、补「旧 Bot + 0.8.0 Claude 驱动」的 E2E；
  - 技术取舍：用"协议 4 + 后台 RPC + 重连查证"代替协议 5，Client API 升到 13，Desktop 最低修订保持兼容，无主权限请求只由模型 deny、Desktop 暂不加入口；
  - 连接器 0.8.0 和 Desktop 的打包、签名、安装由用户在合并后自己完成。
- 此前 AskUserQuestion 的 `invalid_option`，以及 Yuki 为解除阻塞代答的 deny，**都不是用户的决定**（见 §20.3 第 5 项）。
- **第一阶段不是完整修复**。PR 与本书都如实标明第二阶段的范围（§21.6）。

### 21.2 实施内容（分支已合入 `origin/main` 67101c10，含 #634、#635、#636）

**Bot（分支 `fix/external-agent-nonblocking-dispatch`，PR YuanYeYouTao/Yuki#637；Desktop 为 Institute-of-Agent-Life/Yuki-Desktop#85，分支 `feat/work-step-kinds`）**

| 编号 | 改动 | 说明 |
|---|---|---|
| B1 | prompt／queue／watch 只在激活里登记，然后返回原操作的持久视图 | 写出、回复与结算交给服务的后台任务；写出前在 reader 中复核当前连接、设备授权，以及原 Work 是否非终态、generation 是否一致、操作是否未 detach |
| B2 | 原操作查询先返回持久事实 | 在线查证放到后台；同一操作同时只有一次查证，派发未完成时不发查证 |
| B2b | 设备重连后按原 ID 读回 | 不重发。带 run 的操作只在 Work 未终态时读回；控制只在目标未结算或发起它的 Work 未终态时读回，之后保持 unknown 回执（修订 6 收窄，避免只增不减） |
| B2c | not_found 只是诊断 | 只作为**这条连接**的诊断（`DeviceConnection.reconciliation`），连接结束即失效；它不是回执、不是成功，也不授权重发（修订 6，替代进程级字典） |
| B3 | 显式 detach 只解除 Work 所属关系 | 设备离线、或设备答 `original_operation_not_found` 时适用；回执写 `released_without_device`；`_detached` 保留原 `admitted`，不伪造接纳 |
| B4 | 新的 prompt／queue 不预绑定原生回合 | 所选操作或执行只用来定位会话（P0-3 的一部分） |
| B5 | 能力提前拒绝只认当前连接的实时声明 | 库里的旧目录、已移除或离线的实例、原操作的控制都不拦，交给设备裁决 |
| B6 | `drain()`／`close()` | 只等待未完成的后台任务；`close()` 幂等 |
| B7 | **控制类（steer／answer／cancel／detach）也移出激活**（取代 §20.3 第 2 项） | 后台派发；先结算目标，再写控制回执，最后经 `control_settled` 以私有输入告知发起它的 Work。目标 run 仍在 waiting 且属于同一 Work 时立即唤醒，否则由正常 completion 路径匹配。中途重启时控制回执为空，重连后按原 ID 读回、幂等重算 |
| B8 | 无主等待 | 会话级 `get` 返回 `pending_unowned_requests`，只含元信息，修订 6 改为在 SQL 中按设备、实例、会话收窄；会话目标的 `respond` 只接受 deny，绑定该事件的设备、实例、会话和 `unowned` 标记，ACL 沿用电脑所属人物族 |
| B9 | 步骤投影 | `/works/{id}/steps` 新增 `kind`／`tool`／`action`／`detail`。外部 Agent 的 `operation_id` 在生成前按查看者对该电脑的当前读取权限核验。Client API revision 13，只加字段 |
| B10 | 工具合同 39 | `use_coding_agent` 的说明写明：登记即返回；Claude 的 queue 语义，且不是 steer；能力以目录为准；无主等待只能 deny |
| B12 | （复审后）控制的读回与通知 | 设备 journal 对"已开始、尚无结果"的控制给出的占位（`original_dispatch_not_confirmed`）不当作回执，也不通知 Work，之后再读回；写出前复核失败的控制也通知发起 Work；当前连接放在写出前最后、中间无 await 地复核；按 execution 选中原操作的 steer 不被提前拒绝；离线释放的控制回执 `admitted:false` |
| B11 | 文档与 V3 收窄 | `list` 与会话级 `get` 仍是等设备回复的在线只读查询（连接器从本机记录读取，不经过原生 SDK 会话或权限回调；连接断开即失败），不在"不占住工作"的承诺范围内（修订 6，按用户决定如实收窄，不新增超时） |

**连接器 0.8.0（同一分支；设备协议仍是 4）**

| 编号 | 改动 |
|---|---|
| C1 | Claude 自己开启的回合（例如后台任务通知）会让会话变忙，新 prompt 立即返回 conflict |
| C2 | 每条输入带自己的 UUID 写入，开启 `replay-user-messages`。CLI 回放该 UUID 才算接纳；prompt 写入后立即返回 `submitted`，不再在连接器内等待接纳 |
| C3 | 新增 `queue`。落点由 CLI 决定：并入当前回合时回执为 `merged_into_running_turn`（只给本条输入的处置，不转交该回合的结果、问题或控制）；开启新回合时为 `started_new_turn`（新回合归追加它的操作）。steer 仍不支持；对排队中输入的 cancel 返回 `queued_input_not_withdrawable` |
| C4 | 同一会话的 open／resume 串行（`_open_once`）。callback 绑定原 SDK owner；owner 退出时只撤回自己的 callback，从未被取用的输入在各自的操作上报 unknown |
| C5 | 无人接收的权限／提问只上传元信息（permission_id、native_request_id、connection_id、tool、`options:["deny"]`、`unowned`），不传命令、路径或正文；只接受 deny；结束时上报 `permission_resolved`。回答核对原 connection_id 与仍挂起的 callback，只消费一次 |
| C6 | （复审后）操作被 detach 时：它写入、但 CLI 尚未取用的输入改为无主；回合的全部接收者都被 detach 后，仍挂起的权限转为只含元信息的无主等待 |
| C7 | （复审后）带 `parent_tool_use_id` 的子代理帧不算自发回合；写入的 prompt 并入无主的自发回合时，接管该回合余下部分（`merged_into_unowned_turn`） |
| C8 | （复审后）启动时读取 CLI 的 `-v`：低于 2.1.311 时不声明 prompt／queue（返回 `cli_outdated`），查询与控制照常；读不到版本时保持声明。会话仍在打开时，后到的输入等它连上再写；打开锁用完即释放；owner 退出时未见回放的输入报 `replay_observed:false`，不再断言"未取用" |

### 21.3 实验事实（Claude Code 2.1.311，SDK 0.2.172，haiku，隔离临时目录，新会话）

| 编号 | 观察到的帧序列（相对时间） | 结论 |
|---|---|---|
| X1 | 首条输入 0.3s 写入，2.02s 回放（`isReplay`，同 UUID）。第二条输入 3.02s 在 Bash `sleep 6` 运行中写入，得到 `command_lifecycle queued`；8.08s 在该工具结果之后回放，回合继续，只有 1 个 result | SDK 模式下，在运行中回合的下一个工具边界并入；回放能确认是哪条输入被取用 |
| X3 | 1.87s `session_state_changed=requires_action` 加 `can_use_tool`（权限挂起）；2.87s 写入的输入 `queued`。5.88s 回答权限后，5.96s 回放并入，result 中可见追加内容 | 排在挂起权限之后的输入会一直排着，回答后才并入（与事故形态一致） |
| X4 | 两条输入相继写入：第一条 1.59s 回放、1.6s result；第二条 2.36s 回放，2.37s 第二个 result | 回合结束后，排队输入开启新回合 |
| X5 | 有排队输入时 `interrupt`：当前回合 3.3s 以 `error_during_execution` 结束；排队输入 5.44s 回放并照常执行，产生第二个 result | interrupt 只停止当前回合，不丢弃排队输入，因此不能用作"撤回一条输入" |
| X6 | 回合运行中连续追加两条，7.98s 按写入顺序先后回放，并入同一次请求 | 顺序保持，可能合并到同一次模型请求 |
| X2 | 未做 | 留到第二阶段 |

实验在 SDK 层看到的事实是：`isReplay`、`session_state_changed`、`command_lifecycle`（SDK 解析器会丢弃这类帧，驱动只依赖 UserMessage 的 uuid）。原始帧日志保存在临时目录，修订 6 清理时已删除，上表就是保留下来的摘要。精确 steer（原生在产生效果前校验目标回合）**没有证据**，所以不提供。

### 21.4 验证

**本机（macOS arm64；日志在会话临时目录，结论如下）**
- 全量套件，提交 `0fe2278a`（复审前）：
  - 单元：10 failed／3727 passed。10 条与干净基线逐条相同（manager_deployment×2、shared_media_cache_mount、sticker×2、terminal_environment、work_execution_receipt_regressions、workspace_copy×2、workspace_publish），都是 macOS 环境问题，Linux CI 通过；
  - 集成：1 failed／1322 passed。唯一失败是 `test_plugin_frozen_binding`（macOS 的 `/var`→`/private/var` 软链触发 `unsafe_workspace_root`，干净基线同样失败，Linux CI 通过）；
  - ruff、mypy 通过。
- 合入 main（#636、#634）之后，在 `5040fa00` 上重跑全量：单元 10 failed（与基线逐条相同）／3727 passed；集成 1 failed（`test_plugin_frozen_binding`，同上）／1327 passed，`test_file_copy_to_actual_connector` 通过；连接器 6 failed（discovery，与基线相同）／254 passed。
- 外部 Agent 专项（bridge、repository、supervision、final_audit、argument_contract、desktop_native_routes、subagent_cancel、local_device_transport、file_copy、client_management）：141 passed。
- 连接器：254 passed，另有 6 个 `test_claude_discovery` 失败，与基线相同（本机 `/opt/homebrew/bin/claude` 存在），CI 的连接器 job 通过；ruff、mypy 通过。
- **失败场景先写后修**：本轮新增 F8–F11 与 R1–R8 测试，均先确认在修复前失败（失败原因与预期一致），修复后通过。

**file_copy 定性**：`test_file_copy_to_actual_connector` 在分支（`14c07d32`）与基线（main `81088aa6`）上**各单独连跑 3 次，6 次全部通过**（期间系统负载 5.6–12.7，另一个 agent 的全量套件在并行运行）。在 full1、full2、`0fe2278a` 全量中也通过，只在 full4 失败过一次。失败点是测试主动断开 socket 后、调用 `connections.get` 前，连接器已经重连成功，于是得到 succeeded 而不是 unknown。这是设备文件 RPC 测试本身的竞态，所在路径（`local_device.file_request`）本 PR 没有改动。**结论：偶发，不是本 PR 引入的回归。**

**跨版本 E2E**（真实 Bot、真实连接器、真实 SDK 0.2.172，CLI 一侧用受控的 stream-json；新增 CI job `claude-cross-version` 自动运行，本机也逐一运行通过）：

| 组合 | 结果 |
|---|---|
| 本 Bot + 连接器 0.8.0 | CLI 不取用输入时 prompt 照样立即返回；queue 写入 CLI；自发回合的 Bash 权限只上传元信息（无命令、无路径），经会话 `pending_unowned_requests` 发现，以 deny 解除，CLI 实际收到 `deny`；原 prompt 由该回合的 result 结算 |
| 本 Bot + 已发布连接器 0.7.0（df133fbc 源码） | prompt 立即返回（0.7.0 自己仍在连接器内等待接纳，但不再占住 Bot）；0.7.0 没有声明 queue，派发前即拒绝；没有无主等待上报（0.7.0 本来就不上报）；原 prompt 由 result 结算 |
| 生产 Bot（8e6594cf 源码）+ 连接器 0.8.0 | prompt 立即返回 `submitted`；queue 照常写入；原 prompt 由 result 结算；无主等待只入库不投影，连接保持在线、事件已 ACK，没有 1011 重连循环；旧 Bot 没有 deny 路径，CLI 继续等待（与现状相同，不是新回归） |
| 生产 Bot + 0.7.0 | 即现网会卡住的部署，跳过不跑 |

产物：`test-results/claude-connector-cross-version/bot-<kind>-connector-<version>.json`（CI artifact `claude-cross-version`），记录实际加载的 Bot `service.py` 与连接器 `claude.py` 的路径。

**CI**

- 后端 YuanYeYouTao/Yuki#637，head `f21d1d41`：Quality 的 16 个 job 全部通过，包括：
  - `checks`、`tests (unit)`、`tests (integration)`；
  - 新增的 `Claude connector across Bot and connector versions`：三种组合各 1 passed，3 份产物；
  - `Local Agent Connector` 的 macOS、Ubuntu、Windows 三个 job；
  - `Real headed desktop environment`、两种 `Native CodeMode`、两种 `Sandbox install with Compose`、三种 `Vec1`。
  - Windows 连接器 job 在上一个提交时因新测试的替身 CLI 是 shell 脚本而失败，已改为在 Windows 跳过这一条（`f21d1d41`）。
  - main 上 #635 那次 run 的 headed desktop 失败（`test_computer_runtime` KeyError），在本 PR 上没有复现。
- Desktop Institute-of-Agent-Life/Yuki-Desktop#85，head `c9ace19`：除 `e2e (mac-arm64 2/3)` 外全部通过。这一片的失败是 `languages.spec.ts:79` 超时，与 main 上 #83 那次 run（43723533754）的同一分片、同一测试失败一致，属于既有问题。重跑后仍以同样方式超时。该测试不涉及工作步骤，在 Linux、Windows 分片中通过；main 的 HEAD（a5687a6，#83）上同一分片同样失败。

### 21.5 发布边界、兼容与上线顺序

- **Bot**：兼容现网连接器 0.7.0（协议 4）、现有 Desktop 和 Client API 11／12 客户端，没有数据库迁移，可以单独上线（bot-only）。
  - 只上线 Bot，解决的是"Yuki 的激活被占住、群消息排着"这一层。
  - 0.7.0 连接器内部仍会等 Claude 接纳，也不上报无主等待。所以"Claude 卡在没人回答的权限上"，要等用户装上 0.8.0，才有经 Yuki deny 解阻的路径。
  - 行为变化：所有改动类动作返回时回执可能还是 unknown／pending，模型按工具说明用 `wait(run)` 等待或之后查询；离线时显式 detach 会解除 Work 关系。
- **连接器 0.8.0**：由 Desktop 随附，用户点击更新；用户已决定在合并后自己打包、签名、安装。0.8.0 与生产 Bot（8e6594cf）兼容，见跨版本 E2E。
- **Desktop**：合同固定点先指向后端分支 head，后端合并后改为合并提交；这个提交同时决定随附连接器的版本。`REQUIRED_API_REVISION` 仍是 11；新字段缺失时退回旧分类显示。
- **顺序**：后端合并 → bot-only 上线 → Desktop 改固定点到合并提交 → 本地打包、签名、安装 → 用户更新连接器。

### 21.6 第二阶段（本阶段不含）

- §7.2 回复走事件流（设备协议 5），服务器结算完成后再 ACK，删除改动类命令的 RPC 应答；
- §7.3 自发回合的因果后续执行（显式归属记录）；
- §7.7 权限／提问的唤醒规则（四种等待形态、两种先后顺序）；
- 完整的跨 Work 四方归属表（P0-3 剩余）；
- §17 T2（步骤上的外部 Agent 实时状态、"等待你回答"及 `work_steps` 刷新），以及 T1 剩余部分（按声明派生分类、plugin／retired、wait／子任务明细、从现在起记录 action）；步骤展开与跳转；
- 实验 X2（只有一个长工具的回合）；
- 复审中留到第二阶段的项（见 21.7）。

### 21.7 三路独立复审与处理

三路复审由新开的独立 agent 只读完成：服务端、连接器、Desktop 各一路，只给代码，不给结论。

**服务端（无 blocker，1 major，6 minor）**

| 发现 | 处理 |
|---|---|
| M：控制第一次读回到设备 journal 的占位 unknown 时，被写成控制回执并通知 Work，之后真实结果被吞掉 | 已修（B12），测试 F10 |
| 写出前复核失败的控制不通知发起 Work | 已修，测试 F11 |
| 复核通过后、写出前连接被替换，被记成结果未知 | 已修：当前连接放在最后、写出前无 await 地复核（无专门测试：时序难以确定地构造） |
| 按 execution_id 选中原操作的 steer 被今天的声明提前拒绝 | 已修，测试并入 Q15 回归测试 |
| `unconfirmed`、`unowned_requests` 的扫描范围；CLI 进程消失后未收到 `permission_resolved` 的等待会一直列出 | 留到第二阶段：`unconfirmed` 只在重连时运行；`unowned_requests` 已按会话收窄，加索引需要迁移 |
| 文档把"重启后重算"写成无条件 | 已改文档与 docstring |
| 离线释放的控制回执写 `admitted:true` | 已改为 false |
| `close()` 期间新建的后台任务可能不被等待 | 留到第二阶段（只影响进程退出时的同一瞬间） |

**连接器（3 major，若干 minor）**

| 发现 | 处理 |
|---|---|
| M1：排队输入的操作被 detach 后，它开启的回合归已 detach 的操作，权限对谁都不可见 | 已修（C6），测试 R1；同类的 owner 被 detach 时挂起的权限也已修，测试 R8（同时覆盖 connection_id 不符被拒、只消费一次） |
| M2：子代理帧被当成自发回合，会话一直忙 | 已修（C7），测试 R2 |
| M3：CLI 不按输入 UUID 回放时没有兜底；owner 退出时把"未见回放"写成"未取用" | 已修（C8）：版本门槛 + `replay_observed:false`，测试 R3 |
| 写入的 prompt 并入无主的自发回合时，回答没人收到 | 已修（C7），测试 R7 |
| 会话仍在打开时，后到的命令写入未连接的 client | 已修，测试 R5 |
| `_opening` 只增不删 | 已修，测试 R5 |
| 新 queue 带已结束原操作的 `operation_id` 时，连接器返回误导的 `instance_unavailable` | 留到第二阶段：属于服务端对 queue 的定位方式，Codex 的 queue 也走同一路径，需一并核对 |
| 权限回调与消息流之间无顺序保证 | 留到第二阶段：回调先于回放时会退化为只能 deny（安全方向）；先于上一回合 result 的概率极低 |
| 只读视图忽略排队中的输入 | 留到第二阶段 |
| 测试缺口：空闲但有未取用输入时 prompt 冲突、R8 的 connection_id／只消费一次 | 已补（R1、R8） |

**Desktop（无 blocker，1 major，若干 minor）**

| 发现 | 处理 |
|---|---|
| M：`native-work-layout.spec.ts` 断言 title 等于 textContent，带 detail 的行必然失败 | 已改：摘要单独标记 `data-step-detail`，测试分开取名称和摘要再比较 |
| 生成类型把旧服务器没有的字段标成必填，fixture 用强转掩盖 | 已改：`WorkStep` 按线上实际形状把四个字段设为可选，fixture 恢复 `satisfies` |
| 服务器字符串作为 defaultValue 时会被再次插值 | 已改：`skipInterpolation`；tool 为空时显示"其他" |
| 时长一律按秒显示，未完成步骤的跨度易误导 | 已改：只对已完成步骤显示，并按秒／分钟／小时格式化 |
| 措辞（agent 种类与动作重复、Exit code） | 已改 |
| E2E 未覆盖 detail 与"不显示正文" | 已补：送出事实可见、发送正文不出现 |
| 读屏把名称与摘要连读 | 已加分隔 |
| admin 合同新增的 `prompt_cache_breakpoints` 没有文案 | 已补三语 |
| 合同固定点指向分支 head，后端合并后要改成合并提交 | 按用户安排，由用户在后端合并后改 |

### 21.8 前缀缓存核对（用户要求）

1. **稳定前缀里没有动态内容**。
   - 本阶段没有改提示装配：`prompting/`、`agent_core/`、`runtime/work_session.py`、对话历史与 compact 检查点的代码都没有改动。
   - 动态事实只出现在工具结果或 Work 私有输入里，包括 operation 状态、设备、排队、`pending_unowned_requests`、目录 capabilities。
   - `kind`／`detail` 只属于 Client API 的 `/steps`，不进入模型请求。
2. **工具说明**。
   - 合同版本 38→39 只改了 `use_coding_agent` 的静态说明字符串，所以上线后工具清单的缓存失效一次。
   - 工具清单和说明是固定合同，不读设备在线状态或能力声明。能力声明只在执行处用来提前拒绝，不改工具声明。
3. **只往末尾追加**。
   - 后台结算写的是操作回执和 SandboxTaskRun。
   - `control_settled` 与事件经现有的 `_input` 插入新的 Work 输入（`on_conflict_do_nothing`），不改写已有输入或 transcript 条目。
   - 唤醒沿现有的 `signal_external_action`。
4. **E2E 核对**：`tests/integration/test_external_agent_prompt_prefix_e2e.py`。
   - 跑真实 App／Main／Work 和外部操作派发，在模型边界捕获该 Work 的全部实际请求，再用生产的 Claude Messages 与 Chat Completions 编码器编码成请求载荷。比对时去掉会移动的缓存标记，它们不是提示内容。
   - 结果一：每个请求的 system 和 tools 字节完全相同，且不含 operation、设备、实例、run ID。
   - 结果二：同一激活内，后一个请求逐字节延续前一个。派发的 receipt 只出现在末尾新增的条目里。
   - 结果三：跨过释放激活的 wait 之后，之前的公共历史逐字节相同，结算与唤醒只出现在其后。
   - 同一测试在 main（`81088aa6`）上也通过，跨 wait 处的共同前缀长度完全一样（Claude 2 项、Chat Completions 4 项）。这说明跨 wait 时激活尾部重新布局是现有的 Work 续跑结构，不是本 PR 引入的。
   - 产物在 `test-results/external-agent-prompt-prefix/<protocol>.json`，内容是逐条哈希。
   - OpenAI Responses 没有放进这个测试：Responses 由 runner 生成 continuation 形态的请求，测试用的模型替身不产生这种形态。
