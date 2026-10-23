# 本机 Agent 持续监督、断流恢复与汇报清理任务书

日期：2026-10-23。状态：调查与设计完成，尚未实施。

施工基线为 Yuki main `0aa3e08bd3b5a78677b7831c7a22301966526adf`。本轮重新拉取 main，已核对本机安装的连接器关键源码与该版本一致。另审查了尚未合并的 [PR #500](https://github.com/YuanYeYouTao/Yuki/pull/500)，审查 head 为 `ae45ba148a42a6df0c4d6c33c71a73980fb4f7b8`；它改到了同一批文件，正式实施必须先按届时 main 合流，不能覆盖其启动隔离和 journal 游标改动。

桌面端对照组织仓库 [Yuki-Desktop](https://github.com/Institute-of-Agent-Life/Yuki-Desktop) main `72c6cbf`。协议对照本机 Codex 0.163.0、0.164.0 离线导出的 schema；没有向原生会话发送新输入、接管或重连。本轮也没有改线上配置、调用模型或发群消息。

## 1. 这次要解决什么

用户让 Yuki 盯着电脑上的 Codex，把任务做完，有进展就告诉他。当前问题是：原生连接已经断了，工具还在返回旧的 running；模型不断查询，却没有明确告诉用户自己已经失去观察。另外，一次 watch 只覆盖一轮执行，难以表达“把整个会话持续盯下去”。

改完后应能做到：

- 原生断线、电脑到服务器断线、某次执行结束，各自按实际发生的事情处理；查询、推送和目录不会互相矛盾。
- Codex 连接恢复后，查证原执行、恢复原订阅；原输入不会再发一次。
- 盯整个会话时，下一轮执行也能继续观察；盯指定执行时，仍只跟那一次。
- 有重要进展、失联、需要用户决定或最终结果时，Main 有真实材料可汇报；没有新内容就等待。
- 删除独立的 `reporting=quiet/interactive` 字段，删除重复 diff 推送及旧状态拼接，不引入新的汇报模式、催报定时器或完成门槛。

共同边界沿[开发约束](development-contract.md)、[主 Agent](main-agent-runtime.md)和[本机连接器](../operations/local-agent-connector.zh-CN.md)。本任务书说明拟修改的部分，不把计划写成现行实现。

## 2. quiet 到底是什么，谁写的

这个字段不是用户设置页面上的开关。它是 `task_control.accept/update` 暴露给模型的可选参数，保存位置是 `runtime_work.checkpoint_json.communication.reporting`。恢复 Work 时，该值又展示给模型。当前发送执行器不靠它禁止发送。

历史可以追到 10 月 2 日的 [#218 / 8ca2e17a](https://github.com/YuanYeYouTao/Yuki/commit/8ca2e17a)。当时它配合一套长任务汇报规则引入。10 月 10 日的 `56c2da8f` 放宽了相关限制，10 月 11 日的 `82bb386a` 删除了 `services/work_reporting.py` 和完成汇报门槛，但保存、修改和展示这个枚举的链路留了下来。

这次查到了原始模型响应和后续请求中配对的工具结果，时间均为 UTC+8：

| 时间 | 事实 |
| --- | --- |
| 14:46:27 | 模型 accept 监督任务，goal 明确包含同步关键进展；没有提交 reporting 参数 |
| 15:21:52 | 原生事件流异常断开 |
| 15:28:27 | 模型调用 `task_control.update(reporting="quiet")`，工具返回 `ok: true` |
| 15:38:21 | 用户再次要求盯着会话，有进度汇报 |
| 15:40:28 | 模型再次调用同一个 quiet 更新，工具仍返回成功 |

对应模型响应 trace 为 308991、309305；配对结果保存在后续请求 trace 308993、309307。它确实是模型主动写入，并非数据库默认值或用户切换了某个模式。**quiet 出现在断流之后，不能用来解释断流前少汇报的全部原因。**

修复不采用“禁止模型从 interactive 改成 quiet”这一旧限制。任务要求已经保存在 goal、真实用户输入和 context_note 中，再存一个容易过期的二选一字段没有必要。用户要求安静执行、SELF 安静工作和后台维护的原指令仍然有效，只删除这个独立参数。

## 3. 已确认的问题与待验风险

现场证据截至 16:44；reporting 来源补查截至 17:11。源码缺口不等于每一项都在这次事故中触发。脱敏数据见[本轮证据](../operations/evidence/native-agent-supervision-20261023/audit-evidence.json)，本地隔离复现见[探针结果](../operations/evidence/native-agent-supervision-20261023/supplemental-probes.json)。

| 编号 | 问题 | 证据与处理范围 |
| --- | --- | --- |
| F01 | Codex reader 退出后没有原生重连和订阅恢复 | 15:21 的 ConnectionClosedError 已入 Work；服务器连接仍在，原生事件没有恢复。修 connector/driver 生命周期 |
| F02 | 断流、恢复和 get 使用不同状态来源 | Journal 的 COALESCE 留第一条 progress；本机和服务器都用 admitted=true 决定是否更新非终态；服务端 get 收到 unknown 还保留旧 status。统一事实投影 |
| F03 | 正常 EOF 没有失联通知 | Codex 的接收循环自然结束不发告警；Claude 接收流自然结束也保留 running，active 却变 false。隔离探针确认；本次实际是异常 EOF |
| F04 | 尚在队列里的 Codex 操作漏失联通知 | ownerDisconnected 只遍历 watches 和 turn_operations，漏 queue_operations；探针输出 0 条告警 |
| F05 | 查不到指定执行却返回 completed | get 的 selected 为空时回退到 thread idle；探针以不存在的 turn 得到 completed/pending=false。必须修正 |
| F06 | 活跃目录只读第一页；状态查询反复读完整历史 | watch/recover 忽略 loaded/list 的 nextCursor，第二页的会话被判 not_live。0.163/0.164 均有分页接口；read(includeTurns=true) 也需要改为按需分页 |
| F07 | detach 后又恢复观察 | 对 prompt 的 detach 只删 watches，未移除原 turn 的上传归属；下一条 admitted 事件把 journal.observing 从 0 改回 1。探针确认 |
| F08 | 单轮 watch 与用户的持续监督目标不匹配 | 当前 turn/completed 会删除 watch；这是现行设计范围，需要明确调整会话监督语义 |
| F09 | reporting 枚举与真实用户要求冲突 | 两次模型 quiet 调用及成功回执已经找到。删除整条独立模式链 |
| F10 | 重要事件选择不完整 | 子 Agent 活动、部分动态工具和 MCP 进展不在当前映射里。按真实稳定事件补齐，不上传 token 和思考流 |
| F11 | 重复 diff 占据大部分推送 | 当前 watch 163 条事件共 954,219 字节，其中 69 条 diff 更新占 857,803 字节，约 89.9%；代码又将同一 diff 放在 native.diff 和 diff 两处。删除这类重复主动推送 |
| F12 | 一个实例初始化卡住会阻塞整台设备接入 | main 的 gather 启动屏障存在；#500 已改为独立启动和动态目录。衔接该改动，不另做一套 |
| F13 | 事件发送任务死亡，连接仍可假活 | connection 单独创建 events task，没有随它的异常退出；隔离探针确认 sender 失败后 connection 仍运行。#500 的 catalog task 也需纳入统一生命周期 |
| F14 | 恢复后的原生问题是否仍可回答，缺少连接身份约束 | Codex requests 按原 RPC ID 存在内存；重连后不能沿用旧连接的问题 ID。属于恢复实现必须补齐的测试项，未判定本次发生误答 |
| F15 | 积压传输的修复可能降低吞吐 | #500 删除了反复读取全部积压的逻辑，但采用逐事件等待远端 ACK。需测实际 RTT 和恢复吞吐，不能以消除全量扫描代替性能验收 |
| F16 | Claude/ACP 也走同一套过期状态链 | 它们的 unknown 事件同样不带 admitted；共用 Journal 和服务器投影照样可能保留旧 running。修共同状态，恢复能力仍按各协议实际支持处理 |
| F17 | 丢失接纳回复的原操作可能根本没进入恢复集合 | Journal.complete 只在 result 带 session_id 时设 observing；unknown 回复没有它，即使原 command 已知 session，observations 也不返回。现场后一次 unknown prompt 就是 observing=0。prompt 无 execution 时也未像 queue 一样按 clientUserMessageId 查证 |

本机新 0.164.0 daemon 在 15:21:35–36 启动，断流发生于 15:21:52；连接器配置仍指向 0.163.0 的版本目录。时间吻合，但没有旧进程退出日志，不能写成已经证明了唯一触发原因。

## 4. 状态只在一个地方归并

### 4.1 保留哪些事实

原操作、原生执行、观察关系、设备连接、聊天送达各有自己的事实来源。不要再让一个 running 同时表达它们。

| 事实 | 来源和寿命 |
| --- | --- |
| 命令是否已提交、原接纳结果 | 本机 Journal 原 command/response；恢复时不修改原请求身份 |
| 原操作的当前可确认状态 | 本机将原生事件和查证结果归并到原 receipt；事件 ACK 后仍保留 |
| 服务器可离线读取的状态 | 原 operation.result_json 保存上述投影，标记原事件顺序；不再另猜一套状态 |
| 原生连接当前是否可用 | 所属 driver/RPC 的实际生命周期；进程存在或设备 WS 在线不能代替它 |
| 用户是否收到汇报 | 现有 send 和 Social 成功回执；不新建汇报台账 |

原 command/response 是派发证据，receipt 是后续事实，服务器结果是跨设备读取所需的投影。除此之外不新建任务表、监控数据库或另一套 runtime。

### 4.2 归并规则

归并函数放在独立连接器内，以原 receipt 和新事实为输入；`append`、恢复快照和 `get(operation_id)` 共用它。删除 COALESCE 保第一条进度的做法。服务端只校验原设备、操作与执行身份，然后应用连接器给出的完整状态投影，删除对 admitted 的第二套状态推断。

- 已确认接纳的 admitted=true 不因断流变成 false；断流也不能把从未确认的接纳变成 true。
- 失去观察时，非终态操作返回 unknown、原原因和 pending=true；原 running/queued 继续保留在历史事实中，不作为当前状态冒充确定结果。
- 普通文字、diff 或工具进展不隐式将所有操作改成 running。运行、排队、等待问题、原生终态以对应协议事实为准。
- 已确认的原执行终态不会被迟到的接纳响应、旧查询或失联通知降回 running/unknown。
- 断开观察只结束观察关系，不能把原生执行判为成功或取消。
- 原执行在原生存储中找不到，返回查证缺口；不借用会话状态补造结果。
- 恢复后查到原执行在运行，可以展示 running，同时保留实际发生过的 stream gap；这不证明断线期间的每条里程碑都找回来了。

事件顺序复用本机 journal 已有的 sequence，把它带入事件及状态投影；服务器在原结果内保留最后采用的序号，按同一设备 namespace 比较。UUID 仍用于事件去重，时间戳不用于裁决新旧。派发前用短事务登记 command，原生返回后另用短事务保存 response；每次事件及由它生成的 receipt/sequence 在同一个短事务内提交，不能跨原生 RPC 持锁。服务器写入状态、原事件及 Work 输入成功后才 ACK。

普通 get 返回同一投影。确需对账时由连接器查询原 session/execution 并将查证事实写回原链，不能只给一次临时修饰结果、下次又读回旧 running。服务端不得因查到 unknown 就继续保留一个不带解释的旧 status。

电脑到服务器的连接断开，是服务器自己知道的传输事实。此时不能等电脑发来原生失联事件：服务端读取视图对非终态明确返回 unknown/device_offline，并将原投影标作最后确认结果；已确认终态照常可查。这个展示由现有连接注册表派生，不把“设备不可达”写成原生执行失败，也不覆盖本机权威 receipt。重连后采用按原序号恢复的事实。

设备断开/恢复沿现有连接生命周期向活动观察所属 Work 提供有来源、可去重的控制输入；复用原输入链，不新增定时监控。旧 WS 退出时先核是否已被新连接替换，不能把新连接误报为离线；完成或失效的 Work 不复活。关闭服务时沿已有生产者收拢顺序写完必要事实，再关闭数据库。

### 4.3 协议与历史数据

以 #500 的协议 2 为合流基线。本次状态投影及跨轮 watch 改变了线协议语义，计划统一升级协议 3，由服务端和连接器同批交付；不长期维护两套当前语义。具体版本在实施时按届时 main 校准。

本机 schema 需要升级时，在 Journal 原数据库内做一次结构/数据迁移；服务器使用现有冻结迁移。已有 pending、unknown、terminal 和 detach 回执原样保全。旧事件可以通过明确的升级转换进入新投影，不重新执行原命令。迁移编号由实施时 main 分配，不在任务书占号。

## 5. 原生断流与恢复

### 5.1 一个实例一个生命周期

沿 #500 的实例独立启动继续做。每个 driver 的连接任务负责启动、接收、退出、重新连接和停止；Connector 只维护实例目录、服务器传输和 journal。删除启动成功后永久把对象存在视为 available 的判断。

正常 EOF、异常 close、proxy 退出、握手失败都必须结束当前连接，结算当时未确认的 RPC，并更新真实实例状态。主动停止沿原停止事实收尾，不假报新的事故。events、catalog、WS 接收任务共同拥有当前传输生命周期；其中一条失败不能留下一个表面在线的空壳。

重连只连接用户已选的同一实例/profile/owner。清退自己的 reader、relay 和 proxy 后再建新连接；不关闭用户 daemon、不自动安装/更新 Codex、不换 CODEX_HOME、不偷偷另开服务器执行。

重连使用传输层的普通退避，成功后复位，停止时立即取消；退避用于避免失败连接忙转，不是用户任务的尝试次数或执行期限。实例间不互相等候，失败实例也不阻塞已有原回执上传。

### 5.2 恢复原监督

按本机原 journal 找到仍需观察的操作，逐项查询原 session、原 execution 或原 queue submission。恢复集合必须包括已登记但接纳回复未知的原操作，不能只选 observing=1；原 command 中已有的 session 仍可用于查证。复用已有 recover 入口，修正其只在整个连接器启动时调用的限制。查证放在数据库写事务外。

- 明确的原 turn 仍运行：重新订阅同一线程，保留原 operation/run/execution。
- 原 turn 已结束：写入查到的原终态，结束对应单执行观察。
- 原 queue 仍在队列或已消费：沿 queued_submission_id/clientUserMessageId 找回原提交；不新建 queue。
- prompt 接纳回复丢失且还不知道 execution：也沿原 command 的 session 和原 clientUserMessageId 查证，不能只有 queue 支持这条恢复路径。连 session 都无法确定时继续保留缺口，不搜索相似正文猜归属。
- 原结果未知：保留 unknown 和具体原因，不能通过新的 prompt 来“恢复”。
- 恢复会话监督：接回原会话，提交当前快照及 gap 事实；不把别的会话或旧目标当当前任务。
- 已 detach、来源撤销或 Work 已结束：不恢复该观察，不复活旧任务。

loaded/list 必须读取实际分页结果。读取执行状态优先使用元数据和 `thread/turns/list`，只读取查证需要的页和 itemsView；显式历史读取才加载所需正文。不要在每次 get 中展开完整 thread 历史。

原生待回答的问题属于其原 RPC 连接。旧连接消失后，不保留可误用的 requests 映射；重连若再次收到问题，按真实新请求建立关联。旧 permission/question 的 respond 返回无法确认或已结束，不把旧批准交给复用同一数字 ID 的新问题，也不自动替用户批准。

### 5.3 可执行文件与跨协议边界

不在产品或部署脚本中硬编码某个 `releases/0.163.0` 目录。显式 endpoint、socket、profile 和 command 仍由用户选择。优先使用所选安装提供的稳定原生入口；若该发行只能使用版本目录，升级流程明确更新对应实例配置，不按目录时间猜“最新版本”。旧路径尚能运行，不证明它连接的 owner 仍健康。

Codex 可连接现存 daemon；Claude SDK/ACP 的进程则可能由连接器拥有。统一的是断流事实、回执和目录，不承诺把任意 Claude 独立终端或任意 ACP 进程重新接成原执行。协议无法证明恢复时保持 unknown；新任务是否允许启动沿该实例真实能力处理。

## 6. 持续盯会话，不再让单轮结束代替监督结束

复用固定 `external_agent.watch`，不新增监督工具，也不增加让模型选择的 reporting/follow 模式：

- 用户指定 execution_id：只观察这次执行，原终态结束该观察。
- 用户指定 session_id、未指定 execution_id：持续观察该会话中的后续轮次，直到 detach、所属 Work 结束或来源失效。

工具描述、返回结果和运维文档同时写清楚。会话可以暂时 idle，idle 不等于监督完成，也不需要每隔几分钟重新发一次 watch。

作用域在原命令里保存一次，属于请求目标事实。会话观察不将第一轮 execution_id 锁成整个 operation 的身份；每条原生事件仍绑定它自己真正的 execution_id，登记到现有原生执行资源中。原 run_id 继续指向这一条观察关系。

对于会话观察，turn/completed 是“这一轮已结束”的 progress/执行事实，不是观察操作 completed；单执行观察和 prompt/queue 的终态结算保持原意。不能简单放宽所有 operation 的 execution mismatch 检查。

旧 watch 继续按原单执行范围处理：迁移在原 command 内标明旧范围，已选中的 execution 保留；旧 idle watch 不自动扩成无限期会话订阅。新会话监督才采用上述规则。没有新任务表或复制原生会话。

detach 清掉指定操作的观察/上传关系。prompt/queue 对应的运行对象可以继续存在，用于实际执行与必要资源释放，但不得继续借已 detach 的 operation 向服务器上传新进展，Journal 也不能被后续事件重新设为 observing。其他仍合法观察同一会话的操作继续工作。

## 7. 删除 reporting，保留真实任务要求

一次删完以下消费者：

- `work_control_tools()` 中 reporting 参数及说明。
- WorkControl 的 communication/reporting getter、patch_communication、accept/update/derive 转发和运行资料展示。
- WorkRepository 的 reporting 参数、专属验证、初始 communication 构造、patch 写入与专属保留逻辑。
- 主提示词中要求设置 interactive/quiet 的句子、当前文档与测试夹具中的模式设置。

保留“有实质进展、阻塞、需要决定时简短报告；没新进展不刷屏”和 SELF 安静工作的实际指令。用户的通知要求放在原 goal、真实追加输入和必要的 context_note；新的明确要求覆盖旧的任务解释。不能再造一个自动分类器，按内容推断并锁死任务模式。

旧的已付请求、调用配对及回执不重写。已执行的旧 reporting 更新仍读取原回执；尚未执行的旧调用按现有恢复入口结算，不为退役字段恢复一次数据库写入。只含退役字段的 update 可以无变更返回，删除专门为该字段设的校验，不新建永久兼容工具或新一轮模型请求。

旧 checkpoint 中残留的 reporting 不再读取、展示或继续专门保留；在已有升级/正常 checkpoint 写入边界去除该废弃子键，不为清理历史 JSON 增加启动全库扫描。冻结迁移及历史证据中的旧字段照实保留，不改写过去。

## 8. 进展进入 Main 后怎么处理

持续监督使用原 Work 和原事件输入。等待时让模型登记对应 run 的 `owned_run` 条件，可以与 conversation 组成 any；确实还要定期查看外部 CI 时再加 time_due。已有字段名 owned_run 不表示把被观察的原生执行变成 Yuki 所有。

修复并验证事件在 wait 登记前、登记中、登记后到达的竞态。不能要求模型反复 get 才能看到已收到的进度，也不能因活跃 wait 只写了时间就宣称 native 事件已实时唤醒。用户明确设置的其他等待语义不被无条件覆盖。

保留 Main 自己组织汇报、统一 send 和真实送达回执。失联和恢复也是进展事实，用户不能只看到“继续盯着”而不知道监督已经失效。内部最终正文不等于对用户发出消息。

不新增固定周期催报、每 N 个事件总结、额外总结模型、每阶段登记表或“发完报告才准完成”的检查。任务完成与消息送达分别记录；已有成功发送不重发，未知发送按原效果查询。

## 9. 事件内容和传输成本

先删除，再补必要事件：

1. 删除 `turn/diff/updated` 的完整 diff 主动持久推送及其重复字段。已经有文件变化、命令结果、计划和 agentMessage 可表达重要进展；确需查看补丁由显式读取原生资料或项目文件完成。
2. 共用同一份 item 映射处理事件和显式历史视图，删除白名单在两处重复维护的部分。
3. 补齐真实子 Agent、动态工具、MCP 工作中的稳定阶段/完成事件；保留其原生 ID、执行关系和结果。token delta、思考内容和整份 transcript 不成为持久事件。
4. 对能替换的计划/状态更新沿其原生项目身份处理；不另做语义压缩模型，不以任意字数或条数丢弃重要问题与终态。

#500 已用顺序读取替换了 Journal 全量 pending 扫描，应保留这个方向。但逐事件等远端 ACK 会把积压恢复的速度限制在一条/RTT 左右：假设 RTT 50 ms，10,000 条仅网络等待就约 500 秒，尚未算提交。这是算法上界分析，不是本机实测结果。

实施时比较顺序发送和管线发送。若 RTT 成为主耗时，利用 WebSocket 背压和原 sequence 发送有序批次、合并确认，继续保证每个事件只有在服务器实际提交后才确认；不要把一个事件的窗口大小固化成新的产品门槛。批次需要释放服务器 writer，不能把全部离线积压塞进一次长事务。读取、编码和网络等待全部在 writer 外。

固定工具数量不增加；reporting 参数减少。实施后测实际固定工具 token、事件字节、接收至 Work 可见耗时、重连恢复时间和积压排空速度。缓存命中率只看真实 usage，减少字节不直接宣称命中率提高。

## 10. 分批实施与文件范围

| 批次 | 做什么 | 主要文件 | 退出条件 |
| --- | --- | --- | --- |
| A | 与 #500 合流，固定状态/观察语义与迁移 | connector、journal、external_agents service/repository/http、迁移、现行文档 | 状态表、协议版本、旧 pending/unknown/terminal/detach 升级用例一致 |
| B | 一处归并原状态，修 false completed、分页和 detach 反开 | `journal.py`、`drivers/codex.py`、服务器 repository/service | 同一操作在推送、get、重启恢复中给出相同事实；指定执行缺失不报完成 |
| C | 原生生命周期与恢复；连接任务共同退出 | `drivers/__init__.py`、三个 driver、`connector.py`、config/CLI 必要部分 | 正常/异常断线都可见；Codex 恢复原观察；某实例失败不拖其他实例 |
| D | 会话监督、单执行监督和问题回传 | Codex/Claude/ACP 实际支持部分、server identity 映射、tools、Work wait 接入 | 跨两轮仍观察，单轮不串轮；旧问题不误答；结束监督不取消 native |
| E | 删除 reporting、重复事件和死代码 | WorkControl/Repository、prompting/contracts、测试夹具、item 映射、文档 | 无活动消费者再读写 reporting；真实通知要求继续保留；无 diff 双存 |
| F | 全链验收、性能对照和发行准备 | connector 测试、bridge/Work 集成、wheel、CLI、Desktop 薄入口 | 下表通过，记录真实模型与实机范围，形成可审查 PR |

每批先落可复现的失败场景，再改实现，最后删掉替换下来的分支和说明。主工作区有用户未提交文档，本任务书在独立分支 `codex/native-supervision-plan-20261023`，不得覆盖原工作区。

桌面端当前只展示连接器安装、配对、用户服务与实例清单，没有真正展示原生订阅健康。优先复用服务端设备目录和本次真实状态；不要把 CLI `service.running` 改名成“Codex 正常”。若需要显示新状态，更新 `src/shared/connector.ts`、`src/main/agent-connector.ts` 和 `AgentConnector.tsx` 的薄投影，不在 Desktop 另写 driver、轮询守护程序或任务存储。服务端不可达时就显示无法取得该层状态。

## 11. 验收矩阵

下表是实施后的验收要求，本轮没有宣称修复通过。

| 编号 | 场景 | 应看到的结果 |
| --- | --- | --- |
| T01 | running 后异常 close | 本机 receipt、服务器 get、目录、Work 输入均能看到失联；接纳事实保留 |
| T02 | 正常 EOF、proxy 退出、初始化失败 | 失败/退出路径各有真实状态，不假活；主动 stop 正常收尾 |
| T03 | 队列尚未生成 turn 时断线 | 原 queue 有失联状态；恢复沿原 queued ID/clientUserMessageId，不重复排队 |
| T04 | Codex 恢复、原 turn 仍运行 | 相同 operation/run/session/execution；恢复订阅，无新的 turn/start |
| T05 | 离线期间原 turn 已完成 | 查到原终态并写入；旧 running 响应不能覆盖；重复恢复不重复 Work 输入 |
| T06 | 查不到原执行 | unknown/未找到原执行的事实，不借 idle 判完成；不重发 prompt |
| T07 | 活跃会话在 loaded/list 第二页 | 找到并订阅；不以第一页缺席判 not_live |
| T08 | 历史很长、目标执行不在首个历史页 | 按游标查证到目标或确实查完；普通状态读不传整份历史 |
| T09 | 只断电脑到 Yuki 的 WS | native 继续；重连只重传未 ACK 的原事件；身份和真实效果不变 |
| T10 | 断线时还有未确认 RPC | 原调用结果保持 unknown；get 原操作能查证，派发次数不增加 |
| T11 | 同会话两轮执行和一段 idle | 会话监督持续，两个执行 ID 不串；idle 不结束观察 |
| T12 | 明确盯旧 execution，另一个执行开始 | 只接旧执行，终态后结束；不暗中转盯新轮次 |
| T13 | 旧版 idle/single-turn watch 升级 | 原作用域保留，不被迁移扩大成会话监督 |
| T14 | detach 后又来 agentMessage/终态 | 不重开 observing、不再向旧 Work 上传；其他合法观察正常，native 未取消 |
| T15 | 多实例中一个握手阻塞/失败 | 其他实例目录、命令与历史回执仍可用；失败实例状态真实 |
| T16 | event/catalog sender 或 native reader 任务异常 | 所属连接结束/恢复，异常被观察；无后台死任务造成假活 |
| T17 | 原生问题未回答时断线、恢复后复用数字 RPC ID | 旧问题不能答到新请求；可恢复的问题使用实际当前关联 |
| T18 | 终态、迟到接纳、进度、重复事件交错 | 终态不倒退，原 sequence 去重；冲突的事件不伪 ACK |
| T19 | 撤销设备、Work 完成、generation 变化 | 停止新的观察/效果，不复活任务；原已发生回执继续保留 |
| T20 | Claude 正常 EOF、ACP 进程退出 | unknown 和实例能力/可用性一致；不假称能恢复协议不支持的原进程 |
| T21 | 用户要求汇报，随后又追加“有进度告诉我” | 最新真实要求保留，运行资料不再出现 reporting；有里程碑时 Main 调用 send |
| T22 | 用户要求安静执行、SELF 无需发言 | 可安静工作；不因删除枚举增加周期消息；真人追问仍处理 |
| T23 | 进展在 wait 登记前/中/后到达 | 原 run 的输入被领取并能续原 Work；无漏唤醒、无假用户输入 |
| T24 | Main 收到失联事实，随后收到恢复事实 | 用户可得到简短准确说明；不把“连接器服务在线”当订阅恢复 |
| T25 | send 已成功、失败、结果未知与重启 | 按原 Social 回执处理，不用内部正文冒充已发送，不重复已成功消息 |
| T26 | 旧已付请求含 reporting 参数恢复 | 原工具配对完整，已执行结果不重跑，退役字段不恢复旧模式 |
| T27 | 重要事件过滤与 diff 清理 | 命令/文件/子 Agent/问题/终态保留，token/思考/重复完整 diff 不进入持久洪流 |
| T28 | 0、1、100、10,000 条积压；不同模拟 RTT | 记录时间、CPU、事件循环延迟、内存峰值和 writer 持有；RPC 查询不被积压饿死 |
| T29 | 原版本与候选版本同负载 | 比较 p50/p95 和恢复吞吐；若变慢，说明瓶颈并修正，不能只报 SQLite 错误减少 |
| T30 | 独立 wheel 安装、CLI、Desktop 展示 | 包内包含迁移和新协议；服务存活、服务器连通、native 状态各有正确展示 |
| T31 | 电脑断网，或旧 WS 关闭晚于新 WS 接入 | 非终态查询明确失去当前观察，原 Work 得到断开/恢复事实；旧连接不能覆盖新连接的可用性 |
| T32 | 原 prompt 已登记，但接纳回复丢失，observing 尚未置位 | 原操作进入查证集合；沿原 clientUserMessageId 找回执行或保持 unknown，不再次派发 |

测试从现有 `test_codex_driver.py`、`test_journal_connector.py`、`test_connector_startup_owner.py`、Claude/ACP driver 测试、`test_external_agent_bridge.py`、`test_external_agent_work.py` 和 Work settlement/wait 用例扩展。已验证的原终态优先规则、精确身份、撤销、ACK 后回执保留继续回归，不为删除代码丢掉它们。

分层记结果：纯状态/假协议探针；真实数据库和 WS 的集成；实际 Provider 是否选择 send；真实原生 owner 与平台送达。脚本化模型可以验证整条送达链，不能证明真实模型自然选择汇报。真实模型/原生升级/平台发送在后续实施获得对应授权后验收，不拿本轮只读调查代替。

## 12. 删除清单和交付

必须删干净的内容：reporting 参数及专属读写/提示/测试夹具；第一条进度永久充当当前状态的 COALESCE；把 admitted 当成一切状态更新前提的分支；每次 get 全量读取历史；重复 diff 推送；detach 后残存的上传关联；启动完成后永久 available 的推断；事件任务失败不被 owner 观察的孤立 task。

不删除真实原操作、原事件、授权、发送回执及冻结迁移事实；不再恢复旧 work_reporting.py、开始/完成汇报门槛或第二套后台任务调度。

实施完成后至少三轮顺序复审：

1. 从原生事实追到本机 journal、传输、服务器状态、Work、模型和 send，逐段核同一身份与顺序。
2. 按断线、重启、升级、取消、detach、问题回传、多实例和跨轮监督检查，尤其复查新旧回执交错。
3. 检查删除残留、固定工具、文档/发行、真实验收缺项和同负载性能；发现新缺口就修，再复审受影响链路。

交付现行文档、必要迁移、定向回归、性能数据、安装包验证与 PR。合并、发布、服务器部署、本机连接器更新和真实用户验收分别记录。升级部署需要同时处理服务端协议和本机包；保留原 journal/数据库，先恢复原状态再继续监督，不能让用户再发一次原任务来掩盖恢复失败。

## 13. 本轮任务书复审记录

- 第一遍，事实与来源：从现行代码和线上回执对齐原操作、事件 #441、Work 输入和发送记录；找到 quiet 的两次实际写入及成功结果，修正“quiet 可能解释断流前少报”的推断。
- 第二遍，生命周期与边界：本地隔离复现队列漏告警、指定执行缺失却完成、loaded 分页遗漏、detach 反开、旧 running、Claude 正常 EOF 和发送任务假活；确认原终态优先规则当前已有保护。没有调用 native 或模型。
- 第三遍，施工与合流：核对 #500 的独立启动、动态目录、顺序积压读取和协议 2；保留这些已有工作，把逐事件 ACK 的吞吐成本列入实测。补齐旧 watch 范围迁移、旧 reporting 调用恢复、问题 RPC 身份、Desktop 薄入口和真实模型汇报验收；修正跨网络事务的表述，并补上设备 WS 离线的服务端观察事实、旧连接迟到退出、observing=0 的未知接纳查证。上述缺口已写回任务和 T31/T32。

本轮复审完成的是任务书和缺口盘点。源码修复、候选版测试、合并和上线仍待实施。

## 参考

- [本机 Agent 接入与 ACP 统一重构原任务书](Yuki-本机Agent接入与ACP统一重构任务书-2026-10-23.md)：了解已交付范围，现行合同优先。
- [OpenAI App Server 文档](https://learn.chatgpt.com/docs/app-server)：thread/read 不订阅事件，thread/resume 接入线程；当前历史接口提供 turn/item 分页。具体字段同时按本机 0.163.0、0.164.0 schema 核验，文档更新不替代固定版本测试。
- 本机完整只读事故报告：`D:/Code/Reports/Yuki/codex-watch-20261023/README.md`。原始调查材料保留本地，不把凭据、完整配置、模型思考、native transcript 或无关群聊正文加入本任务书。
