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
- 保存初始编译消息、逐次模型调用、实际 HTTP JSON 请求与返回、工具批次及逐项结果。
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
每条来源的窄标量 SQL、原 Social 回执/出站事件核验、正文编码及短条件 INSERT 均在同一个
空 Context 的消费者中执行。来源只读 session 结束后才进入 writer；只接受原 ID 的类型、
存在及归属，不能补填当前 owner、Work 或 privacy generation。发生时的 Work/activation/
generation 是历史元数据，不以消费时 lease 仍相同为条件。来源改属/删除及隐私删除代次
还在 INSERT 中原子复核，失败只丢样，不恢复业务、不重试未知诊断提交。

队列最多等待 256 条，共享 32 MiB 原始资源预留（包含 active item）。预留涵盖独立快照、
容器与媒体/不透明字段引用、JSON/string/UTF-8/gzip 临时峰值及输出；每条包含 512 KiB
codec 工作空间下界，再按结构、路径和字符数保守估计。深度超过 48 或节点超过 65536
直接丢诊断，这不是业务消息上限。队列已满、原始大字符串/媒体、不可压缩或原始巨大但
压缩很小的输入，在 deepcopy/JSON/hash 前拒绝；这类丢样的 payload_bytes/payload_sha256
未知，只通过固定缺口日志和 dropped 计数报告，不冒充完整记录。编码后的 16 MiB 上限
仍保留明确 omitted 元数据。调用端的有界快照 CPU 有成本，writer health 的 snapshot_call
计量它；不能称为零成本。模型返回及 Provider 名额释放不等待可丢来源读取、编码或写锁。

固定慢准备日志位于消费者：encode_call_inclusive 包含线程排队/事件循环恢复，
encode_execution 是成功编码在线程内的执行耗时，source_validation 包含只读 session
退出，prepare 不包含最终 INSERT。不能相减宣称精确线程队列等待。writer 的
phase_timings 分别计 snapshot_call/source_validation/encode_call_inclusive/encode_execution/
diagnostic_write；原 commit_call 字段现在表示完整消费者调用，不能解释为纯 commit。
这些观测不写 SQLite、不带正文、参数或凭据。未进入或未完成的编码不伪造完整执行耗时。
模型用量统计和过程面板因此是最终可见的诊断视图，不能作为完整计费或真实效果账本。
轮次结束信息只报告当时已知的缺口；稍后提交失败由诊断日志与生命周期 health 中的
failures 报告，不能将早期的零缺口解释为全部记录已提交。
关闭时先停止生产者，按原两秒政策等待排空，再取消剩余消费者并统计丢样；已经启动的
编码线程须 shield/join 到真实完成后才释放引用与资源预留，关闭总耗时可能超过两秒。
取消 await 不等于线程退出；消费者空闲后也不保留上一条 payload。重启不恢复队列。
取消和进程终止继续传播，不保证此时仍能记录/交还结果。业务账本、效果回执和 Work
检查点继续走原同步持久化合同，禁止放入此诊断队列。清理复用现有维护循环，
每批最多 500 条、批间释放写锁，排空本次到期窗口，不把每批上限变成每小时清理上限。
隐私删除在原删除事务中清空全部可丢弃诊断，并递增诊断删除代次；正在记录的旧轮次
通过原子代次校验，不能把旧提示词或返回重新写回；排队中的模型调用元数据也检查同一代次，
避免重新插入删除前的关联。新轮次正常记录。
代次仅控制诊断的保留，不参与业务权限、Work 状态或恢复。

## 管理查询

通过现有 Control Plane 的可信 operator 和公开 DTO 提供消息、过程与 Social 回执查询。
元数据读取和正文读取分别授权；正文/思考/工具参数默认不随列表下发。
元数据查询不从数据库加载压缩正文；读取正文时校验保存状态、完整性与摘要，缺失不冒充空白内容。
WebUI 本轮面板用元数据展示收发消息、Token 与状态；有执行正文权限时，用户可主动展开
最近的关键操作，或逐步读取工具参数和结果、模型路由及回复、投递与错误记录。
面板初次只取最近 32 步；较早步骤及其收发事件由用户按同一内部轮次 ID 和步骤 ID
逐页加载，每页最多 32 步，并核验原会话的真实根记录。聊天正文仍按独立权限读取。
开始和结束记录按同一 operation_id 配对；页面不自动批量读取正文。
媒体、不透明 Provider 字段、过大未保存和到期内容须明确标出，不能作为完整操作展示。
分页 cursor 绑定资源、会话及筛选范围；查询不触发模型、重跑或任何真实效果。
当前执行面板只取最近的真实 Runner 根记录，事件到轮次的关联先按内部
`source_event_id` 或已确认的 `delivered_event_id` 查候选，再核验同一原会话的根记录。
0077 为根记录和非空来源事件增加部分索引；查询使用与索引一致的固定谓词，
避免活跃群的大量普通步骤使每次页面轮询扫描整段诊断历史。索引只加速诊断读取，
不改变记录保留期、事件归属或执行状态。
`ExecutionTraceFilter.origin` 可按实际来源筛选 Jev 观察或 Host 接纳，游标同样绑定来源。
不公开原 journal、媒体缓存宿主路径、HTTP 凭据和不透明 Provider 恢复状态。
正文权限可查看真实提示词和工具参数中的工作区路径及用户内容；它不是公开访问接口。
HTTP 登录和正式 WebUI 接线遵守 control-plane-foundation，当前实现见 [WebUI](webui-console.md)。

## 验收

普通聊天首次主模型运行前可记录一条 `chat_preparation` 元数据：使用单调时钟统计
固定准备阶段及总耗时，细分阶段归属于其父阶段，不能重复相加。记录继承原内部轮次、
来源事件和隐私代次，不带正文或权限信息，复用现有有界诊断队列；不为每个阶段创建
写事务或新的业务状态。诊断失败不改变准备结果，取消继续传播。该记录只描述处理
开始后的准备，不能冒充入站前等待、Provider 网络时间或 SQLite 独立锁等待。
初次上下文校验的计时不包含 Runner 首次 dispatch 内真正的 projection 发布；
后者位于 turn_start 之后，仍按实际模型/请求与SQL诊断核对。

`/healthz` 的 `sqlite_diagnostics` 记录固定分桶的 read_sql/other_sql、应用到 pool 的
connection_acquisition（起点早于 checkout/pre_ping）、写 SQL、显式 writer 获取、首次 deferred 写入、
真实 commit/rollback 及可观测持有下界。首次写入的驱动排队、SQLite 等待和执行无法精确拆开；
`driver_queue_seconds`、`pool_wait_seconds` 与 `sqlite_wait_seconds` 保持未知，不能相减推算。
holder 仅覆盖已安装钩子的 engine，不代表系统所有连接。诊断 writer 的 `queue_wait` 与 `commit_call`
是自身队列和提交调用耗时，不能冒称数据库锁等待。计量不写 SQLite，不包含正文或参数。
物理 close 实际完成后才确认释放；强制 queued stop 或关闭失败无法确认释放时结束观测，
单列 `held_release_unknown` 并标记 `release_confirmed=false`，不计入确认完成桶。
因此空 holder 列表不能证明不存在尚未确认关闭的 writer。

新增 `model_phases` 使用 phase_version=1：logical_call 从共享 execute 入口到结果/异常
准备完成，success/error/cancel 使用同一单调边界；preparation、slot_wait、slot_hold、
response_preparation 是互斥区间。slot_hold 在实际 Provider 名额取得/释放处切换，保留整次
complete 与重试生命周期。nested_seconds 中的 dispatch、attempt dispatch、payload/wire、
trace_snapshot、transport、response parse、retry budget/backoff 是嵌套明细，不与主阶段相加；
HTTP transport 含 httpx 自身连接池/调度，不等于精确网络服务时间。physical_attempt_count
只计实际 HTTP post。配置/容量拒绝可为零，native 工具不确定运输按原单次请求合同处理。
旧记录没有 phase_version/分段时标 unknown，不反推历史分段。

conversation_lock 记录 coordinator 与 conversation 的 request→acquire 单调等待；固定
runtime_lock_timing 日志还报告等待中取消/拒绝。chat_processing 原起点仍在取得锁之后，
不能将入口到处理的全部时差归为会话锁。phase_metrics 按同 turn 记录固定 N/F/E/K、
item/byte、candidate/estimate 次数和 protocol lock wait/held，来源计数可缺失，不伪造零。
这些累计计数不参与授权、预算、回放或 cache。跨进程 age 继续使用原 UTC/epoch 时间。

真实 Runner + 隔离 SQLite + 假 Provider/网关验证完整轮次，四种 HTTP 协议验证实际请求。
覆盖工具拒绝/复用/并行、重试、截断、取消、重启、journal 删除后仍可查、媒体过期语义、
权限拒绝、跨会话 cursor、隐私删除与清理。不得用付费模型或真实 QQ 消息完成这些检查。
