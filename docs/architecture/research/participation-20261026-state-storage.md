# 参与状态的物理拆分：保留旧数学，只减少重编码与写盘

日期：2026-10-26。状态：研究与本地构造实验；未修改运行时代码、依赖、数据库或生产。

本轮替代10-25研究中“改随机键、长期不重锚”的参与存储方向。这里保留原
`sample_sequence`、SHA256抽样流、原baseline逐tick更新、原浮点运算顺序和冷恢复参数
解释。优化的是状态表示与持久化，不拿数学轨迹变化换性能。

基线是Yuki `75e21a5c824876485d75e7fcd58e8111bdddeb4d`及固定参与库
`79d266918dc6ce19b7d81568565deaed450301d5`。已读现行[开发约束](../development-contract.md)、
[语义参与](../semantic-participation.md)与[数学合同](../autonomous-participation-model.md)。

## 1. 可行性与边界

可把原State分成两块：每tick变化的数学进度，以及只在真实内容变化时重写的body。
它们共同组成原来的一个逻辑State，由原SnapshotStore在同一数据库、同一事务、同一
revision下保存。加载时还原后交给原Controller。不是两套状态，不新增执行队列、owner、
随机游标表或独立恢复器。

保留旧抽样轨迹意味着每tick仍须保存已经推进的游标；不能一面承诺崩溃后继续旧hash流，
一面完全不持久化这些变化。正确目标是**不再因为几百字节到数KB数学进度，重编码和
重写几十万字节冷body**。新消息、来源退休、真实回执或观察队列变化时，body仍按事实写。

同一SQLite row新增一个hot列并不自动等于物理窄写。原记录总长度和整数编码宽度变化
会影响BLOB的overflow布局；本机初测也遇到了这一点。本轮按另一组SQLite WAL
实验选定同owner同DB竖向两表、只有一个revision。不能把“UPDATE只写一列”当作
已经验证的磁盘收益。

## 2. 本地实验怎样保证用了正确代码

本机主仓库venv里的参与包与固定提交不同。实验没有直接导入它：从79d266的git树读取
全部12个Python模块，临时装入独立目录，再构造合成State。临时目录结束即删除，没有
访问生产、发送HTTP、调用模型或发送消息。

可复现脚本和元数据证据：

- `D:/Code/Reports/Yuki/participation-state-storage-20261026/state_diff_experiment.py`
- `D:/Code/Reports/Yuki/participation-state-storage-20261026/state-diff-evidence.json`

证据记录了12个源码文件的hash、38个State字段的完整分类、合成转换前后变化路径、热冷
编码长度及精确还原结果。脚本中的split/join只是度量原型，会完整dump State；**不能
把这段度量代码搬进每tick运行路径**，否则只是换个地方全量编码。

## 3. 原tick究竟改哪些字段

原State定义在固定库 `controller.py:179–217`，共38个字段。下面分类已由实验对
`State.model_fields` 做全集比对，既不遗漏也不重复。

| 原字段 | 原变化原因 | 物理归属 |
| --- | --- | --- |
| now、last_sample_at | `advance:2451/2457/2460/2475/2483/2489`推进时钟 | 热标量，原值保存 |
| sample_sequence | 有scores的非明确邀请分支增加；即使total为0也增加（`:2488–2490`） | 热整数，保旧序列 |
| skipped_seconds | 单次elapsed>5的原累计（`:2454–2455`） | 热标量，先不借此存储改动删诊断 |
| replay_after | `_advance_replay_boundary:1906`每tick推进原实际边界 | 热标量，不改为新虚拟fence |
| baseline_evictions、baseline_invalidations | 原资源裁剪/来源撤销计数；前者可能随fold重复增加 | 可放热标量；不据此推断body未变化 |
| belief_baselines | 原每tick重建，at/value持续变化；key及身份通常不变 | 数学数值热，身份布局冷，见下一节 |
| trace_baselines | 每次边界推进重建compute/activity/social_context/no_reply/human_activity | 热数学结构，保持原5种计算次序 |
| attentions | `opportunity_scores:2064`写当前合法candidate注意值 | 数值热，原key顺序与集合冷 |
| reception_baseline | 真实reception过期或资源裁剪时折叠（`:2675–2691`） | 小数学结构可放热；同时可能有body变更 |
| scope、epoch | 真实会话身份与控制器代次；epoch分支会清pending | body，不为储存生成新业务epoch |
| events、observations、candidates | 新来源/解释，或原600秒/容量退休 | body |
| seen、consumed、invalidated | 来源版本、入场与撤销；退休后清理 | body |
| feedback、effects、proposal_runs、proposals、pending | 真实提议、接纳、执行回执或原清理 | body |
| boundaries | 原停止、释放及来源变化 | body |
| last_accepted、last_human_at、last_self_message_at、human_activity_initialized | 真实接纳、输入/发送或一次初始化 | body |
| capacity_blocked | 原集合数量计算；值改变意味着原结构变化 | body；不当作省略事实的门槛 |
| observer_checkpoint | queue/在途请求/health/retry/真实失败变化；空闲重建对象不等于内容变 | body，见§5 |
| host_checkpoint | source_versions、参与单元、retired_refs、表达关联等 | body；time retirement也会真正修改它 |
| self_reports、engagement_report | 真实自报、原清理或顺序变化 | body |
| familiarities | 实际熟悉度更新时写；查询时衰减但不每tick重锚 | body，保原基线 |
| work_pulses、outbound_anchors、receptions | 真实活动/发送/反馈；到期/归档也会改集合或值 | body |

“冷”只表示不被普通时钟连续重写，不表示只随新聊天变化。实验跨600秒时确实修改
events/seen；有原单元时还修改host_checkpoint；跨两天时修改receptions及
work_pulses.archived_positive。只有热变化时的快速保存，必须由本次实际转换确认。

## 4. 数学区不是几个定长整数

### 4.1 所有baseline都保留

| 状态 | 当前正常算法的保留规模 | 是否每tick改数值 |
| --- | --- | --- |
| belief_baselines | `_advance_replay_boundary:1904`保留64项 | 是，原at/value持续更新 |
| trace_baselines | 旧字段读取过滤到5种（`:290–295`），边界推进时生成这5种 | 是；human_activity在boundary还未到其初始化at时保原对象 |
| familiarities | 原更新路径`:1745–1747`超过64时清最旧项 | 否，留body，不能漏存 |
| reception_baseline | 可选一条(at,value) | 只有实际reception折叠才改 |

这些是现行正常生成路径的规模，不是新设schema容量。State字典字段本身没有统一的
`max_length`；迁移不能把旧可读取状态当作恰好已经归一化成64项，也不在迁移中裁剪。
正常加载后仍由原restore/prune处理，不增加新的拒绝上限。

### 4.2 身份长串不能塞进“固定小头”

BeliefBaseline除了7个数值（at、5维belief、last_action），还带thread、target；外层key
也可能重复编码这些身份。它们没有字符串长度上限。attentions的原event key同样不是
固定长度。把原belief map完整移到hot JSON，长身份仍会每tick被重编码。

建议保留一份冷身份布局：

- 冷区保存一份belief的key、thread、target布局；热区按该布局保存at、5维value及
  last_action。**last_action不是纯冷身份。** 一条因feedback或表达证明仍被保留的
  self message effect跨过fold边界时，last_action会更新，但原effects/body可能完全不变。
- 原prepared按`(-last_action,key)`排序，这时belief字典次序也会改变。冷布局不跟随
  数值自行另排：直接保持原字典当前顺序。该顺序实际改变时，算一次冷layout变化，
  在原事务full save新body与数学区。不新增排列数组或slot映射；不能为保持冷bytes
  不变而丢掉旧计算所产生的原顺序。
- attentions按原顺序保存一份冷key列表，热区保存对应数值。
- trace保原存在性与5个固定名称；reception baseline保原None或(at,value)。

列表位置只是这一份存储表示中的位置，不是新事件ID、人物ID、业务版本或第二份
所有权。身份、集合或原字典顺序改变就同时保存新布局与数学区。
禁止让旧布局配新数值，也不能按另一个排序重排effect/event等字典，改变原求和顺序。

64条belief、每条7个热double，原始binary64数值体为3,584字节，另有容器和热标量。
本轮JSON原型的64条热区为8,568字节。这个数字不是热区硬上限：attention可能有较多
项，整数的十进制长度会增长，schema未限定的旧内容也不能静默截掉。

尤其 `sample_sequence`、各计数器原本是Python整数，并没有规定uint64。不能为了做
定长header引入64位溢出或新的计数上限。`now`是float也不能因此把整个header称为
固定长度。优先使用可变长但只含实际数学数据的编码，按实际字节验收，不增加预留大
空洞或新经验容量。

## 5. observer checkpoint的变化不要误判

固定库 `ObservationSession.checkpoint:69–82` 每次创建新的dict和queue投影。
`evaluate_due:177–179` 在退避判断之前调用它；Host的 `_save` 又调用一次。
但ProviderHealth的failed_since、last_success、retry_after并不随每tick自动更新。

本地实验发现：

- 空队列evaluate_due创建了新的observer_checkpoint对象，完整内容没有变化。
- 尚在退避、pending仍新鲜时，完整内容也没有变化。
- pending在75秒过期后，queue条目和expired计数真实改变，即使仍在退避也须写body。

因此不能用对象地址变了、`checkpoint()`被调用、或evaluate_due返回True/False判断
body。改为在原队列/health真实转换处返回“持久内容改变”的事实；同一次调用里复用
准备结果。它只是原调用栈的结果，不新增全局dirty版本或后台通知链。
原在途Snapshot、失败retry_after、queue原sequence、替换顺序及真实晚到结果都留在
原body和原恢复合同；不能为了少写而丢掉下一次评分所需的材料。

## 6. 唯一快照的物理读写合同

主方案选定两张竖表：窄head保scope/generation、**唯一revision**和数学进度；body表
只保对应冷内容，二者仍由同一个SnapshotStore连接owner管理。不要求为了形式上的
“一row”承担大BLOB更新，不新增逐scope迁移表或第二个revision。

写入仍是原BEGIN IMMEDIATE事务和原expected_revision：

1. 先取得原 `_save_lock`，随后在同一段无await代码中物化本次确实变化的checkpoint、
   捕获dirty并冻结本次热区；只有真实冷变化时才编码、冻结body。冻结scope、generation、
   当前expected_revision与布局必须来自同一份State，不能在等锁之前捕获revision，
   不到worker里读取变化中的对象。pins只防退休，不是修改锁。
2. Store核原revision，写数学进度并将该revision加一；有body变化时，在同一事务写新
   body。body没有第二个独立revision，不存旧新两份来猜哪个更新。
3. 加载以请求的原key驱动单个一致性LEFT JOIN获得两部分和原revision：两侧均无才是
   无快照；仅缺head或仅缺body报真实错误，不能用INNER JOIN空结果悄悄新建Controller。
   合成原State后在控制器owner一次校验并接回scope相等核验，不分两次读取拼不同提交。
4. 取消继续等实际已入队operation；只有实际future成功才确认提交、先更新item.revision
   再传播取消。future异常不能假装确认回滚，沿原异常/CAS事实暴露，不自动重放冻结
   payload，不重跑模型、不补发，也不编造现有代码没有的“未知提交恢复器”。
5. close沿原单线程顺序排在待完成操作之后，不加第二worker或写队列。

Typed对象的`frozen=True`只是浅冻结，State中的dict、嵌套list仍可修改。传一个frozen
模型或装有dict的dataclass给worker不算已经冻结；取得原锁后、下一次await之前，
必须把本次提交变成不可变bytes/字符串及其原键、revision和布局。若编码安排在线程，
也只能输入已冻结的原件，不能让线程回读正在变化的State。

**dirty必须先捕获，不能在提交返回后清掉新变化。** 在取得原锁后的同一同步片段准备
这一份head/body与布局，并捕获、清走本次body dirty。等待实际提交期间的新输入、queue、hint
或来源变化仍由原入口重新置dirty。成功后只接回本次revision，不无条件把dirty清False。
本批已提交codec元数据可接回，但不把旧State或布局覆盖回当前Controller。冻结/编码/
任务提交前异常，以及已确认回滚，都只把本次flags与当前新flags合并，不恢复旧State。
未确认的错误沿实际future异常/CAS事实暴露，不自动重放本批。
原 `_save_lock`/单owner提交顺序继续使用，不新加变更版本或第二队列。前台需要同步
持久化的新hint仍须等自己的那份保存，不以较早一次head提交冒充其完成。

冷body不因每次热提交而重编码、解码、hash或读取完整BLOB。运行态也不常驻第二份
整份JSON来比较。布局小描述可以复用原已装载的不可变身份资料；字段变化由真实变更
入口接续，不通过每tick完整 `model_dump(exclude=...)` 检查出“没变化”。

这里的body dirty认实际内容变化，不是执行了一个setter。原hydrate可能重复给
consumed写同一个revision，prune也反复计算同一个capacity_blocked值；这种同值写入
不该触发body。就在原字段/条目的变更处比较实际旧新值并返回本次变化，不为此增加
通用可观察dict、代理状态树或另一份完整快照。现有bool含义不足的函数仍需区分
“已观察/已应用”和“持久内容确有变化”，尤其返回False却补source_order的原路径。

主方案已选择保留原1MiB/scope、16MiB总逻辑容量。一次cold/full save用原Pydantic完整
编码计量得到长度L，同时用明确类型的热表示编码得到H，将有符号`K=L-H`随该冷body
原子保存。hot save只编码新热区，以`K+len(new_hot_bytes)`得到精确原State逻辑长度。
冷布局、成员/顺序、容器形状或存在性改变时full save重算K。这里承认cold/full save
包含一次原完整计量及一次新body编码，不为追求字面“只编码一次”新增JSON拆片器。

这个长度等式要求两种表示的全部动态标量token相同。热数值必须用明确的字段/向量
类型与原Pydantic配置编码，不能拿泛型Any或stdlib JSON替代；例如原float字段的默认
值在Python中可能是int 0，原编码仍是0.0。未知扩展留原body，转义/负零/非有限值及
任意长整数分别验证。wrapper与身份差异由K承接，不逐叶另存一套长度账本。

head保存该revision对应的`logical_bytes`。全局总量在原写事务内对**全部scope/generation
的head.logical_bytes**聚合，不能漏body对应的逻辑内容或历史代次；以旧组合被新组合
替换的真实差额计算。没有第二份增减维护的全局used计数。K属于该冷body的编码元数据，
不是业务版本；hot save可读K的窄元数据，不读取/解码body正文。迁移时head的初始
logical_bytes保留旧payload实际长度，首次原语义save才按重组原State的Pydantic长度
更新；迁移产生的K仍按原typed State的L-H计算，不能把旧JSON空白永久计入差额。

引用、索引页及WAL不是旧payload预算，另报告真实磁盘消耗。不得把热冷物理长度相加
冒充旧逻辑长度，也不得以新的小header字节上限拒绝原本可保存的状态。

## 7. 如何保住旧数学和恢复

保留原Controller计算，存储分片不改变如下事实：

- `_sample:2429–2435` 仍用原scope/generation/epoch/sample_sequence/stream构造SHA256。
- `advance:2485–2498` 的dt、total、arrival比较、selection排序与原短路顺序不改。
  total=0时原代码仍递增sequence，不能认为“没有机会就不必存游标”。
- `_advance_replay_boundary` 仍在原时刻按原顺序算belief和5条trace；不以闭式衰减换
  掉反复重锚，不改变原浮点归约次序。不引入有效拒绝fence替换实际fold fence。
- 原排序、dict迭代、float的binary64值与负零保留；编码解码验证数值用float.hex。
- `Controller.restore:2804` 仍在恢复时重置now和last_sample_at，并保原sample_sequence；
  用恢复时当前配置运行原`:2828`的prune。由于保存的是原每tick锚，而非更早的稀疏锚，
  跨配置冷恢复也能与旧实现逐值对照，不再需要改变冷恢复产品语义。
- 参数热更仍调用原set_parameters路径，不插入新的tau折叠、旧核参数或配置历史。

旧单payload格式升级只把已有字段分放，数值和身份不删除。停止旧writer后，在事务外
读取旧payload并按固定旧schema准备配对数据；格式拆分不调用restore/advance/prune，
不重置旧时钟、抽样序号或提前重锚。随后用一个迁移写事务只核原key/revision集合，
创建/填充配对新表并删除旧snapshots表，不在写事务里解析历史正文。

提交前中断并回滚后仍为旧表示，可重新执行同一格式升级；提交后只留新表示。没有
额外格式标记、逐scope迁移状态、自动猜测reader或独立恢复器，以事务性表结构识别
格式；已有库半对或新旧混合布局报实际错误，不补空状态。原revision/generation/epoch及原执行
身份继续；未知扩展保留原reader语义，损坏不伪装成空State。迁移准备峰值内存单列
测量，不新增容量门槛；原容量政策和旧payload长度仍按§6处理。实际Controller.restore
只在格式升级完成后的正常加载入口运行，不能混进迁移准备。

## 8. 本地diff与精确复原结果

以下全是合成State。带200,000字节padding的body只是为了观察大小关系，不代表线上
存在同名字段或相同大小。热冷是实验表示的编码长度，不是RSS、WAL或生产耗时。

| 场景 | tick实际变化 | 热区字节 | 冷区字节 | 冷字节不变 |
| --- | --- | ---: | ---: | --- |
| 空scope、Host不可提议 | now/last_sample/replay/trace | 505 | 601 | 是 |
| 空scope、intrinsic允许但无真人活动 | 上项加sample_sequence | 505 | 601 | 是 |
| 10条未到期原事件 | now/last_sample/replay/trace | 505 | 4,681 | 是 |
| 一个合法开放候选 | 上项加sequence/attention | 524 | 3,017 | 是 |
| 原事件跨600秒 | 还改events/seen | 554 | 603 | 否 |
| 有原单元、来源跨600秒 | 还改belief身份布局/host_checkpoint/events | 634 | 1,176 | 否 |
| reception跨两天 | 还改receptions/work_pulses/reception baseline | 535 | 675 | 否 |
| 被保留self effect跨fold边界 | last_action与原belief顺序均改变；后者触发新layout保存 | 752 | 1,272 | 否 |
| 64 belief、身份各16字符、带padding | belief/trace数值与时钟 | 8,568 | 208,518 | 是 |
| 64 belief、身份各1024字符、同padding | 同上，热数值数量相同 | 8,568 | 466,566 | 是 |
| 空观察队列checkpoint | 内容无变化，只重建对象 | 505 | 859 | 是 |
| pending新鲜且退避 | 内容无变化 | 505 | 1,422 | 是 |
| pending在退避期间过期 | observer_checkpoint真实变化 | 505 | 1,296 | 否 |

所有13个场景的分片编码→解码→合成State，与原State的model_dump_json完全相同，
并逐float.hex检查，未使用数值容差。六个后续原tick在重组前后State也逐值相同。
恢复时更换human_activity tau后，原整份State恢复与分片State恢复逐值相同；64个
旧arrival/selection流的hash取样结果逐hex相同。这是表示可无损的局部证据，不是新
运行时、生产性能或全部时间/并发路径的验收。

## 9. 实施时需要的验证

| 用例 | 核对什么 |
| --- | --- |
| 原38字段任意真实变化 | 完整分类；热、冷或两者正确保存，不拿消息水位替代 |
| 零rate、无scores、明确邀请、pending、epoch变化 | 原sequence/last_sample更新分支及hash完全一致 |
| 64 belief、长身份、attention增删/换序 | 无新header上限；身份布局与数值同提交，原dict顺序保持 |
| 保留self effect只推进last_action及排序 | 数值进热区；原字典顺序改变触发body+热区full save，无新排列机制 |
| 原浮点临界、负零、冷热编码重开 | float.hex、全部State和原抽样路径精确一致 |
| 600/3600/两天/社会pulse边界、容量退休 | 原冷集合、proof及热baseline同时落下，不只更新时钟 |
| observer空闲、fresh backoff、expiry、失败、在途与晚到结果 | 空闲不重写body；真实队列/health变化保存 |
| body变化与热提交交错、CAS冲突、提交确认丢失 | 一个revision、一个owner、同一原操作；无拼接旧新body |
| 两侧均无、head-only、body-only、布局不配对 | key驱动LEFT JOIN分别识别，不把残缺快照当成新Controller |
| 冻结后await期间再有输入/hint/queue变化 | 成功只确认捕获那份提交，不清掉新dirty；后到事实由自己的保存接回 |
| 保存取消、close、冷恢复不同参数 | 原revision确认/关闭顺序、原恢复数值和执行事实 |
| 原单payload旧数据、未知扩展、单事务升级前后中断 | 拆分不restore/prune；提交前旧表示、提交后新表示，无逐scope进度链 |
| 1/8/32scope、body小/大、热区不同规模 | JSON编码与扫描工作量、WAL、CPU、事件循环延迟分别记录 |

性能目标是热提交的编码量随真实热数值规模增长，不随大body长度增长。物理写量以
实际SQLite页/WAL实验为准；不能把减少JSON字符串长度直接冒充减少相同数量的磁盘写。
