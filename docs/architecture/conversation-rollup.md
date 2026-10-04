# Yuki 3.8 canonical Conversation Rollup

Rollup 是 canonical Conversation 的可重建 Prompt 投影。`chat_events` 始终是唯一原始证据源；
摘要不写入 Memory，也不拥有 Conversation。

## Conversation 身份

- 私聊：一个 Person 对应一个 canonical private Conversation。
- 群聊：一个 Space 对应一个 canonical space Conversation。
- 多个历史或 Provider alias 可以指向同一 Conversation，primary alias 首次创建后固定。
- Presence、Provider、GatewayConnection 和当前发言 Binding 都不参与 Conversation 身份。
- 只有显式 `/ai new` 改变 ConversationGeneration。

MemoryPartitionKey 使用 SELF、PERSON、GROUP 或 PERSON_GROUP owner，不得用 Conversation UUID
代替。

## 持久状态

每个 canonical Conversation 至多有：

- 一个语义 Rollup checkpoint。
- 一个 emergency overlay。
- 一个 signal-only Rollup job。

这些行都可从事件账本重建。job 使用 signal revision、owner、lease token 和 expiry 处理并发；模型
调用期间不持有数据库事务。提交前必须重验 Conversation generation、来源 fingerprint 和 lease。

## 连续覆盖

Rollup 只覆盖当前 generation 的连续前缀：

```text
starts_after_event_id <= effective_coverage <= last_event_id
raw tail = (effective_coverage, snapshot.last_event_id]
```

checkpoint 与 raw tail 不能重叠或留洞。当前触发事件只在 current message 出现一次，不得同时
进入历史。duplicate/suppressed canonical event 不参与候选或 Prompt。

后台模型返回版本化 JSON 摘要，不开放工具。旧摘要、新事件、外部事件和 visual observation 都放在明确的
不可信 input envelope。模型 timeout、空响应、超长或质量失败可以写 emergency overlay；overlay
不能覆盖或伪装语义 checkpoint。

## 活动窗口与压缩容量

聊天活动窗口默认 96000 token，Work 活动窗口默认 128000 token；它们是可热配置的容量上界，
不要求填满窗口。实际模型 Profile 的输入/上下文限制、输出预留、固定系统合同、工具 schema、
当前动态内容及媒体负担共同约束完整请求。发送前还要检查完整请求的容量；不能只检查历史字符。
历史整理另用 `context.compaction_window_tokens`，默认 90000 token；Rollup 在初始装配和每次
scope 热读取时都取它与 `context.window_tokens` 的较小值。上调容量上界不会同时抬高历史
整理触发点，较小的容量上界仍限制整理预算；这些配置也不能代替模型真实容量声明。

容量估算按模型输入视图计量：保留完整工具参数 schema、媒体负担、协议签名和工具回执，
排除仅供 Host 使用的工具目录元数据、请求诊断字段及不会发送的空默认字段。
此视图不修改实际提交的请求或前缀，也不是 Provider 的精确 token 计量；缓存命中的输入仍占上下文。

群聊窗口及压缩参数通过 RuntimeConfig 的 `context.window_tokens`、
`context.compaction_window_tokens`、
`context.compaction_trigger_ratio`、`context.compaction_target_ratio`、
`context.rollup_output_tokens` 和 `context.rollup_summary_characters` 热更新。
后台每次检测信号、锁定候选时从 canonical Conversation 的固定 primary alias 解析相应 scope，
再读取热配置；候选携带不可变 policy，模型执行和结果校验使用同一快照，不共享会话可变 policy。

只保留一套历史整理策略：默认按较小的整理预算乘 90% 启动后台压缩、乘 60% 计算目标；
默认基准 90000 时分别为 81000 / 54000 token。目标是整理水位，不是主请求可用容量上限。
删除事件数量寿命上限、覆盖前后两套 near/admit/target 分支。大量短消息可以超过 512 条继续保留；
后台信号不能仅因未覆盖 keeper 总数或外部通知风暴启动模型。前台最终 fit 使用实际分组消息，
连同摘要和当前消息计算 token。后台按逐条可见消息做保守容量估算，查询按内部事件 ID 分页，
证明达到触发容量后即停止读取。最新巨大消息保持完整，不能以截头文本冒充完整原文。

`uncovered_event_count` 统计全部 keeper，`uncovered_character_count` 统计逐条可见消息字符，
两者仍用于状态与漂移核验，不再决定 Prompt 窗口寿命。外部事件不进入普通历史，
但作为连续候选来源时具有真实成本。候选有 `batch_max_events`/`batch_max_characters` 的
物理资源边界；这些是读取和模型来源批次边界，不是会话寿命或语义覆盖上限。

原始历史读取是有界的整条消息后缀。若更早可见原文未读完，snapshot 标记 `raw_complete=False`；
前台先按本次请求的实际剩余容量，在一致快照中分页补齐来源，不能把不完整后缀直接当作完整历史。
checkpoint 与实际使用的 raw tail 仍不重叠、不留洞。当前触发消息只出现一次。
启动和重启后的历史预取也按较小的整理预算有界读取，不随容量上界增长而预取整段巨量群史。
预取预算仅控制初次读取；需要时才扩展到真实请求容量，按实际分组后的消息计量，保留整条事件。
完整 raw 加摘要、当前消息仍装入实际剩余容量时，前台直接继续，不购买或等待辅助摘要。
已有后台 Rollup 独立整理，完成不改本轮输入、不自行发消息或续跑。
补读后仍不完整或真正超限时，才走原连续覆盖准备与必要等待；DURABLE 保存实际剩余容量，
不以软目标决定必须等待的覆盖量。当前必需资料超过软目标也不为不可达目标反复整理。
来源、generation、隐私或 Rollup 安全错误仍沿原失败路径处理，不能当作软回退吞掉。

Main Turn 的可重建投影缓存使用独立的物理资源上限：单视图默认 8 MiB、全局 16 MiB，
最多 128 个视图。它不从启动时的聊天窗口推导容量，热配置上调不会被旧字符上限拦住；
超出缓存物理资源时明确拒绝提交，不裁剪语义历史或伪装连续前缀。

压缩来源保留内部 event/person ID、说话人、direction、reply_to_event_id、事件时间、提及及
派生视觉/语音内容。压缩提示词要求保留决定、否定约束、最新纠正、开放问题和可检索引用。
新语义摘要使用 `conversation_rollup_v1`：`continuity` 连续叙述、`source_event_ids` 内部来源、
`open_issues` 开放事项及 `corrections` 最新更正。开放事项、更正各最多 16 项，单项正文最多
1024 字符，全摘要最多 128 个不同内部事件引用，仍受候选热配置的总字符/输出 token 限制。
每项携带来源；更正另有 `supersedes_event_ids`，明确旧说法的来源（未知时留空）。提示词要求
更新已解决事项、让新更正替换连续叙述中的旧说法，不能将它们无限累积为原始事实日志。
这些都是可重建派生视图，不新建 Memory、Work 或独立事项表。

模型输出需通过 schema、条数、字段类型、引用与总容量检查。引用只能来自本批事件或原结构
摘要携带的内部引用；提交前按有界主键集合核验引用仍属于当前 Conversation/generation 的
有效覆盖范围。该查证与 JSON 校验发生在首次 DML 前，并沿用原来源 fingerprint、lease、
generation 和 hold CAS。引用存在不证明摘要忠实，更不替代完整来源覆盖校验。
辅助请求同时使用既有 `response_format=json_schema`、`structured_output=True`，不添加结果工具。
Gemini 适配器转换为 `generationConfig.responseMimeType=application/json` 与 `responseJsonSchema`；
Responses/Chat/Claude 使用各自既有 schema 格式。有效 Profile 必须支持 structured output，
不支持时由既有执行器明确拒绝，不能改能力声明或隐式换路由。Provider 的结构约束不替代
本地有界/合法来源校验；代理源码保留该格式也不等于实际上游已验收其支持。
2026-10-01 的一次无 QQ、无工具微小能力请求通过当时的 Gemini 3.8/上游代理路由接受
`responseMimeType`/`responseJsonSchema` 并返回可校验 JSON；它只确认结构输出能力，
不构成群史摘要质量、长窗口容量或缓存改善验收。
历史自由文本 checkpoint 继续作为标注“来源引用未验证”的不可信叙述读取；下一次成功
模型压缩才写结构摘要，不猜测历史引用，不重置 coverage。非法新结构或引用不提交语义水位。

超大单条来源按完整字符串分块，全部块成功后才能提交连续语义覆盖；任意块失败都不提交。
不增加普通事件分片持久状态，也不截头后宣称覆盖整个事件。每块的输入包含前块所得摘要，
以完成当前批次；摘要质量仍需针对真实长会话验收，完整读取不等于无损摘要。

模型输出预算默认 32768 token，包含思考与最终 JSON；摘要正文默认最多 16384 字符。
增加生成预算不增加落账摘要的默认字符上限，二者独立。WebUI 的群史/Work 摘要输出预算
不另设 32768 的界面上限，最小值仍为 1024；有效 Profile 的输出上限与实际输入/联合窗口
仍由执行器核验。显式保存的热配置和部署环境变量继续覆盖默认值，不自动改写。
超长、纯 reasoning、空正文、不完整 Provider 响应不能提交语义 checkpoint；正文不裁剪后落账。
应急 tail overlay 单独显示“不完整应急视图”，提示模型按内部引用查询缺失事实，不能冒充完整语义摘要。

Rollup 模型调用期间不持有 SQLite 事务；候选、计数差额和 protected suffix 在首次写入前准备，
提交准备使用显式 deferred SQLite 读快照，重验 generation、lease、fingerprint 和持久来源 hold。
首次条件 DML 在 SQL 执行时核验原租约及有效期；领取和续租的时刻在取得 writer 后确定。
517 快照升级竞争完整回滚并有限重备纯数据库计划，复用本次模型输出；追加消息后计数按新快照
重算，来源修改、reset、hold 或租约失效拒绝旧候选。其他错误不统一重试，提交确认异常不假定未提交。
本次调用外未持久化的候选不承诺跨崩溃免重新付费。计数从已核验候选精确扣减，不在每次
提交后扫描完整剩余历史。来源 hold（包括原 Work 的事件）仍可限制推进，不得绕过。

`CONVERSATION_ROLLUP_MODEL_TIMEOUT_SECONDS` 默认 600 秒，同时用于专用 Provider 请求、
整批候选（包括多块来源）及前台压缩等待；所有前台批次共用一次期限，不按块重置。
租约仍通过 heartbeat 续期，不将 180 秒租期当作模型完成期限；批次、窗口和来源保护不变。
Work compaction 使用原任务 Profile 的超时，没有独立 Rollup 超时覆盖；需在该 Profile
明确配置足够的时间，不另建摘要路由。更大的输出预算仍会占用联合窗口中的预留输出容量。

前台超过容量时，所有批次共用一次有界等待期限。优先等待同 Conversation/generation 的现有
claim；没有活动 claim 才执行 required 语义压缩并保持 heartbeat。失败、已有失败退避或超时
才写应急 overlay。超时先取消本地请求，并最多等 5 秒确认原 worker 持久提交；无法确认释放
时失败关闭。普通聊天不抢占语义 Rollup 租约；维护请求仍共享全局模型容量。
原 Work 的持久准备条件保存本次完整请求剩余的历史 token 余额；后台候选读取同 Conversation/
generation 的未过期条件，按最小有效余额保留尾部。不能重新用全聊天窗口保护一个实际请求
无法接纳的尾部，也不能重设原准备期限。条件过期或 Work 终止后不继续使用其旧余额。

## Prompt 顺序与缓存

Provider 输入顺序固定为：

```text
TRUSTED STATIC INSTRUCTIONS
UNTRUSTED ROLLUP SUMMARY INPUT
FROZEN AUTHORIZED CHAT AND SCOPED OBSERVATIONS
CURRENT ACTOR DYNAMIC ENVELOPE
CURRENT MESSAGE
```

Rollup 永不进入 system instructions。冻结历史保留所选聊天与观察的原顺序，新事件在安全点追加。
昵称、群名片和正文来自落账时事件；当前 Actor 的关系、
权限和必要场景资料只进入当前 envelope。长期记忆由 Main Agent 按需调用记忆检索工具，
不在每轮自动预取或注入。

普通输入派发前冻结获准聊天和当时必要资料；旧昵称、关系及状态快照保留当时值，
后来的更正追加而不重写旧块。开启 Work runtime 但尚未 accept 的普通轮仍提交该投影，
已采用的动态资料作为有来源的作用域观察冻结；运行状态属于临时执行材料，不追加到
公共历史。当前 Work 的目标、工具/steer 尾部及不透明签名
继续属于原执行的私有 journal，不并入共享普通历史。
派发明确标识新 composition 或恢复 journal 的来源；恢复、合同/来源改变及旧检查点
缺失后的恢复路径，不能调用另一份新 composition 的提交闭包。批准 suffix 必须与实际
请求的对应位置完全一致，CAS、actor/read-scope、容量及隐私围栏继续生效。
投影证明准备和派发边界的输入，不证明模型响应或消息送达。

普通投影在派发前按完整初始 composition 与固定函数工具，使用 Runner 同一请求估算器
核对真实容量；压缩比例及 4096 token 余量仅用于安排整理，不能成为另一层硬容量。
旧冻结快照真正超限时，沿 `capacity` 原因建立新 epoch，使用当前获准的 raw/rollup；
不按 `window_tokens * 3` 字符数认证中文请求容量。Runner 仍对加入原生工具、运行状态或
续接尾部后的实际请求做最终预检；完整配对的普通工具尾部可用纯摘要构造合法新输入，
保留原执行回执和预算，不改旧 opaque 或拆 call/result。摘要无效且原请求仍装得下时
保留原输入；真正容量不足才停止。
下一次正常主激活达到软整理条件，且后台已发布更新的合法语义摘要时，以 `rollup_ready`
原因一次采用新摘要及覆盖点之后的全部聊天，之后继续稳定追加。后台尚未完成而原输入能 fit，
仍保留原输入先回复；当前激活中的工具往返不会因此切换前缀。没有可回收旧材料、或 fresh
本身已超真实容量时不制造容量 epoch，交最终预检准确停止。
Rollup 只整理公共聊天来源，不能证明私有观察线索已被覆盖。线索优先复用已准备的作用域摘要；
没有就绪摘要而原请求 fit 时保留线索继续，真正超限才沿原 scope-summary 边界整理。
普通工具尾部的显式纯摘要继续保留回执、内部 ID 和预算，不自动 accept 或重发。
私有工具回合与签名仍归原执行，不共享到群史。不因重启或容量上界上调清空合法冻结前缀。

插件通知以 canonical `external_event` 落账，不作为普通 user/assistant/system history；
旧外部事件也不再以 `recent_external_events` 摘要自动附在本轮资料中。既有稳定 `CORE_CONTRACT`
统一约束插件资料不得授予权限，不再存在插件专属 system policy。当前通知触发 Worker 时，Worker
必须唤醒正常 Main Agent，而不是建立独立短上下文或特殊 Agent。它复用与普通聊天相同的
Conversation snapshot、有效 Rollup、canonical raw history、按需记忆工具、工具 schema、模型 profile
和 Prompt compiler；当前 external event 只在最后一条临时 user input 出现一次，不写回 history。

插件主动回复以普通 outbound message 保存，并用 `caused_by_event_id` 指向来源 external event。
历史分组键包含 origin class 与 cause ID，因此主动消息不会被并入上一条普通 Yuki 回复，不同事件
触发的主动消息也不会互相串联；同一事件的多个发送 part 可以组成同一 episode。模型历史与
`rollup_source_projection` 使用有界因果标签，平台实际正文保持不变。

external append 的前台 Prompt 字符增量为零，也不以字符阈值 `force_existing` 唤醒已有后台 job；
但 keeper 事件计数仍推进。digest 必须在最终 coverage/raw-tail 确定后重新选取，不能从切窗前的
过期 tail 复用。压缩输入只包含有界 summary 与来源元数据，不包含 external payload。

pending/processing WakeupRequest 对其 `source_event_id` 建立 coverage hold：Rollup 的提交水位必须
严格小于同一 Conversation 最早活动 source ID。候选选择和最终事务提交都要复核该 hold；模型调用
期间出现新任务时，旧候选必须回滚。completed、silent、cancelled、abandoned 或 terminal failed
会解除 hold，保留的 signal job 随后继续推进。该机制不改变 generation，也不改变普通聊天的
protected-tail 规则。

缓存诊断只能记录不含正文的 prefix/request-shape/snapshot hash，以及归一化 Provider 请求的
instructions、tools/native tools、input-without-current-tail 与完整 cache-shape hash。这些 hash
覆盖实际 model/profile/protocol、reasoning、output limit 和 response format；不发送给模型，不作为
业务身份，也不进入高基数 metrics label。

## generation 与效果围栏

每轮捕获不可变 Conversation snapshot。每次模型请求、工具、回复、语音、图片和插件副作用前都
重验 generation；外部效果通过进程内 EffectGate 获得一次性 permit。

后台通知 turn 在 claim 后、模型调用前和 finish/side effect 前至少三次复核 generation、授权目标、
Principal 与 immutable notification identity。直接文字/媒体通知不依赖 Conversation generation；
只有确认平台发送成功后写 outbound message，过期的 Agent reply 不得落账。

`/ai new` 在同一事务中写入命令事件、增加 generation、更新边界并删除该 Conversation 的
checkpoint/overlay/job。已失去 generation fence 的旧结果不能提交。

同一 SQLite 数据库只允许一个主动 Bot Application。SQLite CAS 测试不能替代跨进程外部效果
线性化；禁止双活实例同时写同一数据库。

## 运维与恢复

Rollup 健康应区分 backlog、processing lease、model failure、policy-ineligible、overlay 和
source mismatch，不输出正文。重建或维护命令必须默认 dry-run，并受 Control Plane capability
和审计约束。

Rollup schema 属于 canonical database，当前版本以 schema guard 和迁移链为准。
代码回退保留新写入的事件和工作回执；旧数据库恢复须单独评估数据损失并停止所有写入。
跨模块约束见 [开发合同](development-contract.md)。


## 压缩变化后的聊天补触发

未登记工作的聊天若在组装历史或模型调用前因来源变化中断，保留原快照的语义摘要与
emergency overlay revision。只有确认摘要版本改变、generation 与 reset 边界未变，且
原轮次未开始任何变更工具调用时，才允许补触发。其他来源修改、普通取消不进入此路径。
已登记工作仍由持久工作恢复接管，不重新执行入站事件。

每个 Conversation 合并为一个待唤醒标记；后台压缩释放本次 claim 后通知检查，仍有
压缩 job 或 emergency overlay 时继续等待，不按 batch 反复唤醒。若提交先于登记，登记后
立即检查当前状态，避免漏通知。后续正常聊天已接手时取消较早标记；最多补触发一轮。
恢复保留原发起者、内部事件 ID 和权限，重新验证 generation，并沿原主入口读取最新历史，
包括等待期间已入库的消息。无需新增工具、修改固定提示词或逐条重放消息。

这份短期唤醒状态仅在进程内保留，最多 256 个等待会话、每项最长 15 分钟；停止时释放，
不跨 Bot 重启恢复，也不代表消息已成功回复。原始事件始终保留。超时和再次来源变化记录
无正文诊断，避免无限生成循环。摘要变更后的首次请求重新编译；后续请求仍只追加，
工具合同不变，不宣称压缩前后完整历史前缀相同。
