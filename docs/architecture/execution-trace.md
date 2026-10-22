# 执行过程查看合同

本页遵守 development-contract；源码实现、合并和线上部署分别验收。

## 问题与最小充分方案

聊天事件、Work 和真实效果回执已经是事实源；恢复 journal 会覆盖、压缩和回收，
不能充当逐轮调试历史。新增有保留期限的执行过程记录，用于查看真实调用，
不建立第二套 Work、恢复器、发送器或权限模型。

过程记录属于有期限的诊断证据，不是业务 Durable State：删除它不改变权限、预算、
承诺、任务或真实效果。禁止从记录重放工具或投递。历史没有记录的请求不能事后补写。

## 记录与身份

- 主 Agent 普通聊天、自主轮、自动化、插件与子工作复用原 Runner。
- Jev 真实观察与 Host proposal 接纳复用同一有期限的诊断库：保存经过原适配器
  准备的实际 Snapshot、返回的全部概率/无效维度/usage、观察耗时及原接纳结果。
  只记录真正发生的观察或接纳尝试，不按采样 tick 制造空记录；不创建第二个 observer。
  来源沿原 `event:<chat_events.id>` 或 memory ref；无来源提议不伪造聊天事件。
  不记录鉴权头、原 Provider 错误正文或凭据；旧快照不回填为完整判定历史。
- 实际物理 HTTP attempt 保存一份请求与返回；逻辑模型只记录当前定位需要的路由、状态、真实 usage 和阶段耗时。turn 不再复制初始上下文，逻辑模型不再复制请求或返回正文。
- 工具参数和结果只在单项调用记录，不再复制工具批次的整份参数与结果；空的 Provider 成功结束事件不保存。Jev Snapshot 保存一次，观察结果不再携带同一 Snapshot。
- 旧工具统计 `tool_invocations`、回合统计 `runtime_turn_observations` 没有现行读者，整链与两张表退役。真实 MemoryToolReceipt、原效果/发送回执、模型用量和关联 ID 保留；CodeMode 子操作结果只保存在原调用 span，不额外复制一条结果事件。
- HTTP 重试分别记录；请求准备、响应收到、上游拒绝和取消分别标明，不把准备当作送达。
- 保存 Provider 实际返回的可读思考；没有返回时不伪造。加密思考、签名与不透明续跑块
  只留类型/摘要提示，原恢复 journal 保持原样。
- 按内部 turn、Work、activation、execution、chain 与 call ID 关联；源消息使用
  chat_events.id，不用平台 message_id 猜测归属。没有聊天事件的后台工作不伪造事件。
- 接收与已发送正文继续查询 chat_events；投递状态继续查询原 Social 持久回执。
  Agent 结束不等于消息送达，工具返回文本不等于外部效果已确认。
- Social 成功投递及事件入账提交之后，可添加 `social_delivery` 诊断；
  `delivered_event_id` 由原成功回执与出站账本校验，保留原 source_event_id。
  查询已发送事件可进入真正执行轮次；跨会话投递按目标事件所属会话核验后显示原轮次，
  该轮的原会话仍单独显示。关联写入失败不降级成功，不补发；旧记录不回填。

## 内容、生命周期与失败

正文 gzip 保存；序列与 JSON 字段顺序不改写实际请求。记录器复制 payload，
不修改原请求、工具合同、模型参数或续跑状态。
不保存 HTTP 鉴权头、API key 或完整网关原始对象。图片/视频的内联字节和临时传输地址
只留有来源的媒体描述，不增加永久媒体副本；临时媒体仍按原 24 小时合同过期。
显式保存到长期工作区的文件继续使用工作区合同。

默认保留 30 天，单条正文上限 16 MiB；部署配置可调整。
配置键 execution_trace.retention_days / execution_trace.max_payload_bytes 注册到现有配置目录，
按重启生效模式应用；环境变量使用 EXECUTION_TRACE_RETENTION_DAYS /
EXECUTION_TRACE_MAX_PAYLOAD_BYTES。超过上限保留明确 omitted 元数据，
不静默截断为完整记录。诊断写入失败不重跑已完成效果，不丢弃成功模型响应；
生产模型调用与过程记录复用一个有界、进程内诊断队列。调用端先冻结原
turn、Work、activation、execution、Conversation/事件、发生时间及原 privacy generation，
在原始资源准入检查之后才复制独立的 dict/list/dataclass 快照；不可变字符串和 HTTP 返回
bytes 可共享，消费者不持有原 response、session、WorkControl 或 live ContextVar。
空 Context 消费者先完成正文编码与 SQL 准备，原 ID 类型、存在、归属、隐私代次及
Social 回执/出站事件条件直接在 INSERT 中原子核验，删除逐条独立来源 preflight 查询。
不能补填当前 owner、Work 或 privacy generation。发生时的 Work/activation/generation
是历史元数据，不以消费时 lease 仍相同为条件。条件不符只丢该诊断，不恢复业务，
不重试未知诊断提交。

队列最多等待 256 条，共享 32 MiB 原始资源预留（包含 active item）。预留涵盖独立快照、
容器与媒体/不透明字段引用、JSON/string/UTF-8/gzip 临时峰值及输出；每条包含 512 KiB
codec 工作空间下界，再按结构、路径和字符数保守估计。深度超过 48 或节点超过 65536
直接丢诊断，这不是业务消息上限。队列已满、原始大字符串/媒体、不可压缩或原始巨大但
压缩很小的输入，在 deepcopy/JSON/hash 前拒绝；这类丢样的 payload_bytes/payload_sha256
未知，只通过固定缺口日志和 dropped 计数报告，不冒充完整记录。编码后的 16 MiB 上限
仍保留明确 omitted 元数据。调用端的有界快照 CPU 有成本，writer health 的 snapshot_call
计量它；不能称为零成本。模型返回及 Provider 名额释放不等待可丢来源读取、编码或写锁。

固定慢准备日志位于消费者：encode_call_inclusive 包含线程排队/事件循环恢复，
encode_execution 是成功编码在线程内的执行耗时，prepare 不包含最终 INSERT。
不能相减宣称精确线程队列等待。writer 的 phase_timings 记录
snapshot_call/encode_call_inclusive/encode_execution/diagnostic_write；已删除的
独立 source_validation 阶段不再计量。commit_call 表示完整消费者调用，不能解释为纯 commit。
这些观测不写 SQLite、不带正文、参数或凭据。未进入或未完成的编码不伪造完整执行耗时。
模型用量统计和过程面板因此是最终可见的诊断视图，不能作为完整计费或真实效果账本。
轮次结束信息只报告当时已知的缺口；稍后提交失败由诊断日志与生命周期 health 中的
failures 报告，不能将早期的零缺口解释为全部记录已提交。
关闭时先停止生产者，按原两秒政策等待排空，再取消剩余消费者并统计丢样；已经启动的
编码线程须 shield/join 到真实完成后才释放引用与资源预留，关闭总耗时可能超过两秒。
取消 await 不等于线程退出；消费者空闲后也不保留上一条 payload。重启不恢复队列。
取消和进程终止继续传播，不保证此时仍能记录/交还结果。业务账本、效果回执和 Work
检查点继续走原同步持久化合同，禁止放入此诊断队列。清理复用现有维护循环，
先由 reader 选择到期 ID，空批不请求 writer；每批最多 500 条，在 writer 中按原 ID 和
当前期限复核删除，批间释放写锁，排空本次到期窗口，不把每批上限变成每小时清理上限。
隐私删除在原删除事务中清空全部可丢弃诊断，并递增诊断删除代次；正在记录的旧轮次
通过原子代次校验，不能把旧提示词或返回重新写回；排队中的模型调用元数据也检查同一代次，
避免重新插入删除前的关联。新轮次正常记录。
代次仅控制诊断的保留，不参与业务权限、Work 状态或恢复。

## 管理查询

管理 API 与 `admin trace <turn|work|event> <id>` 提供原执行轨迹的只读查询。
`event` 使用内部 `chat_events.id`；按真实 turn、Work 或来源事件筛选，并按诊断保留期过滤。
元数据使用 `control.execution.metadata.read`；显式请求正文另需
`control.execution.content.read`。元数据查询不加载 gzip 正文；显式读取正文时按原压缩格式核验大小与摘要。

`after` 是原 trace 行 ID，返回最多 `limit` 项及下一游标。不从平台消息号重建内部事件身份。
不公开私有 journal、媒体宿主路径、HTTP 凭据或 Provider 不透明恢复状态。
查询不触发模型、重跑或真实效果，诊断缺失不改变 Work 的持久执行事实。
当前路由、授权与错误语义见[管理 API](admin-api.md)。

现行查询使用 Conversation、turn、Work、source event、已投递事件和 expiry 索引。
0134 删除没有现行消费者的 roots、operation_id 两条索引，保留历史行和正文。
实际 HTTP 请求与响应用于明确的故障查证；不再常态运行另一套全量输入哈希、
链路 LRU 或逐次前缀比较日志。上游缓存仍以真实 usage 和缺样覆盖率为准。

## 验收

普通聊天首次主模型运行前可记录一条 `chat_preparation` 元数据：使用单调时钟统计
固定准备阶段及总耗时，细分阶段归属于其父阶段，不能重复相加。记录继承原内部轮次、
来源事件和隐私代次，不带正文或权限信息，复用现有有界诊断队列；不为每个阶段创建
写事务或新的业务状态。诊断失败不改变准备结果，取消继续传播。该记录只描述处理
开始后的准备，不能冒充入站前等待、Provider 网络时间或 SQLite 独立锁等待。
初次上下文校验的计时不包含 Runner 首次 dispatch 内真正的 projection 发布；
后者位于 turn_start 之后，仍按实际模型/请求与SQL诊断核对。

`/healthz` 的 `sqlite_diagnostics` 保留 SQL 固定分桶，并由 Database owner 直接记录
`writer_admission_wait`、`writer_connection_acquisition`、`writer_transaction`、
`commit`、`rollback` 和 `writer_cleanup`。队列等待发生在 checkout 和 BEGIN IMMEDIATE
之前；connection acquisition 包含 checkout/pre_ping，transaction 包含当前事务的真实执行，
cleanup 包含回滚和关闭。这些阶段不能冒称纯 SQLite 锁等待，也不能相减推算驱动或 pool
内部耗时。诊断写入器自身的 `queue_wait`、`commit_call` 仍只描述诊断队列。旧的 holder、
首次 deferred 写入和持有下界推算已删除。计量只在内存中聚合，不带正文、参数或凭据。

新增 `model_phases` 使用 phase_version=1：logical_call 从共享 execute 入口到结果/异常
准备完成，success/error/cancel 使用同一单调边界；preparation、slot_wait、slot_hold、
response_preparation 是互斥区间。slot_hold 在实际 Provider 名额取得/释放处切换，保留整次
complete 与重试生命周期。nested_seconds 中的 attempt_dispatch_preparation、payload_preparation、
trace_snapshot、transport、响应准备和 retry budget/backoff 是嵌套明细，不与主阶段相加；
HTTP transport 含 httpx 自身连接池/调度，不等于精确网络服务时间。physical_attempt_count
只计实际 HTTP post。配置/容量拒绝可为零，native 工具不确定运输按原单次请求合同处理。
旧记录没有 phase_version/分段时标 unknown，不反推历史分段。

conversation_lock 记录 coordinator 与 conversation 的 request→acquire 单调等待；固定
runtime_lock_timing 日志还报告等待中取消/拒绝。chat_processing 原起点仍在取得锁之后，
不能将入口到处理的全部时差归为会话锁。phase_metrics 按同 turn 记录 F/E、实际
item/byte 和 protocol lock wait/held，来源计数可缺失，不伪造零。
这些累计计数不参与授权、预算、回放或 cache。跨进程 age 继续使用原 UTC/epoch 时间。

入站准备的数值计时暂存在原 RuntimeTurnCorrelation，Main 进入既有 trace 后并入
phase_metrics；普通 observe-only 输入仍不因此创建 trace。inbound_admission、
participation_binding/feedback、wait_matching、attachment_preparation 分别描述原应用阶段，
不覆盖 adapter 在 matcher 前的引用查询。Social 的 preparation、claim、transport、settlement
分开记录，transport 等到平台 ACK，settlement 是 ACK 后原回执提交；客户端显示时刻未知。
tool_admission 计原 T2 接纳，tool_result_persistence 计正常结果原件保存；外层 tool span
仍包含完整调用，不能重复相加，也不能把缺样的异常路径补成零。计时不改变 writer、预算、
来源核验或回执顺序，不含正文和参数。

真实 Runner + 隔离 SQLite + 假 Provider/网关验证完整轮次，四种 HTTP 协议验证实际请求。
覆盖工具拒绝/复用/并行、重试、截断、取消、重启、journal 删除后仍可查、媒体过期语义、
权限拒绝、跨会话 cursor、隐私删除与清理。不得用付费模型或真实 QQ 消息完成这些检查。
