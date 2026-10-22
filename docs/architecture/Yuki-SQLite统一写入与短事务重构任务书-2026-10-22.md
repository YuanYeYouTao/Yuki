# SQLite 统一写入与短事务重构任务书

初稿日期：2026-10-22。扩审跨日到2026-10-23。最终源码基线：远端 `main`，
`c2ce7da66ab54efe192993b9615df48266a62137`。前两轮固定在`cdb49ead`；同步main后对新增
36文件差异另做三位审查人的独立核查，不把旧基线报告冒充新源码已审。

设计基线：本任务书在10-22完成设计与源码审查。10-23的实现、验证与实现后交叉复审见[实施记录](../operations/sqlite-writer-implementation-20261023.md)。自省调度修复在独立分支
`codex/self-reflection-quiet-priority-20261022`，已完成本地定向验证，未合并、未部署。
不能把该分支的静默等待当作当前 main 或线上已有行为。

本次扩审按用户要求覆盖全仓数据库相关代码，已完成三轮：资源与调用清点、事务语义交叉审查、
全局交错与迁移终审。取证、逐文件覆盖等级和每轮发现保存在
[审查目录](sqlite-writer-audit-2026-10-22/README.md)。静态候选不是已确认 writer；
类型/纯计算、历史迁移、测试与运行时代码分别分类，不把扫描数量冒充逐行验收。

用户确认当前延迟可接受。本轮目标是让持久化职责统一、旧机制能删除、后续维护更清楚，
同时保住当前交互速度。不能用“必须先解决严重延迟”来解释重构，也不以新增调度功能扩大范围。

证据见[当日写锁与自省调度调查](../operations/sqlite-write-contention-20261022.zh-CN.md)。
实施前阅读[共同开发约束](development-contract.md)、[Canonical](canonical-runtime.md)、
[Memory 变更](memory-change.md)、[Main](main-agent-runtime.md)、
[Self Reflection](self-reflection.md)、[执行诊断](execution-trace.md)。

## 1. 结论

建议把所有运行时写事务收口到现有的 `Database.immediate_session()`，由同一数据库资源
统一安排写入。继续保留连接池和并行读，writer 事务只做当前状态复核、必要的小查询和原子变更。

业务层不再分别解决“谁先写、用什么事务、忙锁后怎么重新开始”这几个公共问题。
简单的读改写直接在短事务中完成；只有跨模型、外部操作或大量证据准备的工作才保留领域准备数据。
不要求每个 UPDATE 都先造一个 Plan 类。

这项重构的完整目标是：在当前单进程 Bot 的受控写入口内，让写入在应用中有序等待，
消除相互抢 writer 和旧读快照升级引起的日常 `5/517` 冲突，并删掉相应的重复重试代码。
排队本身不会使长事务变快；缩短热点事务是同一任务的一部分。

不能承诺消除所有 SQLite 问题。其他进程直接写库、慢 SQL、磁盘等待、长读快照和
驱动取消后的未决操作仍有各自边界。设计从完整的目标架构出发，实施可以分工和分提交，
但不把半覆盖或新旧两套写机制长期并存当成交付。需要大范围删改调用者，就直接改调用者。

用户要求同时关注速度：统一以后不能明显拖慢正常聊天。性能作为设计和审查重点，
用同负载前后数据判断，不另设随意的业务接纳阈值。把 BUSY 变成更长的不可见排队，不算优化完成。

## 2. 为什么当前修复越来越绕

固定线上窗口为 UTC+8 的 20:07–21:07：88 次失败 SQL，其中 78 次发生在
`canonical_event_receipts` 首次写入，9 次是自省状态更新，1 次是主动反馈。
失败 SQL 耗时中位数 1 ms、最长 24 ms；可见 writer 持锁观测下界最高 3.177 秒。
这些分别是语句结果和事务观测，不能互相替代，也不能推成 88 次业务失败。

初审基线`cdb49ead`同时存在几种事务组织方式；后续main差异见审查目录：

| 形态 | 基线 AST 调用点 | 当前职责或问题 |
| --- | ---: | --- |
| `Database.immediate_session()` | 152 | 短写入口；各连接仍直接去争 SQLite writer |
| `Database.apply_snapshot_write()` | 14 | 显式 BEGIN，先读后写；忙锁后回滚并重做纯数据库操作 |
| `MemoryFactRepository.apply_evidence_write()` | 22 | Memory 自己维护另一份近似的回滚重试循环 |
| SQLAlchemy `Session.begin()` | 100 | 混有读事务、写事务和包装层，须按真实效果分类 |
| 显式 `text("BEGIN")` | 59 | 多数与一致读有关，不能全部当成写入口删除 |

这些是调用形态的统计，不是五组互斥事务或待修改文件数量。另有 Core `engine.begin()`，
ORM 的 SELECT 前 autoflush、commit 前 flush 也可能写入；只搜索 INSERT 会漏。

问题主要在四处：

1. 事务入口不统一，诊断、入账、后台任务和租约维护各自打开写连接。
2. 需要写入的操作先建立读快照，别人提交后再升级，只能回滚重读。
3. 部分 writer 内仍有身份准备、聚合、关联读取和派生数据维护，实际占用不够短。
4. SQLite 返回 BUSY 后，各层分别重试，调度、回滚和原业务恢复容易混在一起。

已有改进继续使用：诊断异步队列、编码准备与 SQL 分离、Social 原回执快路、Dream 的
部分预序列化、事务计时和物理退出观测均已在当前 main。旧 `memory-writer-boundaries`
和 `sqlite-diagnostics-fastpaths` 分支基于旧目录与旧基线，不整枝搬入，也不重复列为新工作。

## 3. 方案选择

| 方案 | 判断 |
| --- | --- |
| 继续逐仓库补 BUSY/517 重试 | 能处理局部失败，但重复代码和重复读取继续增长 |
| 全局锁直接包住旧业务 callback | 可能把大量准备、文件工作一起串行，旧快照也不会自动更新；不采用 |
| 把连接池缩为一条连接 | 连读也排队，嵌套持有 session 时还可能等不到连接；不采用 |
| 新增常驻 writer actor、命令总线、持久写队列 | 增加任务身份、取消、恢复和提交确认的第二套生命周期；本轮不采用 |
| 共享 writer 准入、短 IMMEDIATE、沿用仓库和 session | 本轮采用，复用现有 Database 与事务组合能力 |
| 更换 PostgreSQL | 可以作为多进程并发写需求下的另一次选型，本轮不承担迁库范围 |

也比较了保留两个永久写入口的方案：一个 IMMEDIATE，一个读快照升级并重试。
它迁移较少，却仍需同时解释两套开始时机、准备范围和重试行为。本任务书选择更明确的最终形态：
**所有运行时写提交从一个短 writer 入口进入；一致读快照保留为准备能力，不再升级为写事务。**

`apply_snapshot_write()` 在迁移中可以暂时保留，逐个替换真实调用者；不把它机械改名、
整体套锁或直接把 BEGIN 改为 BEGIN IMMEDIATE。最后一个调用者迁走后删除该入口。

### 3.1 以替换和删除完成统一

现有代码不是本轮必须保留的资产。先确定谁拥有准备、事务、提交与恢复，再按新边界
重写需要变化的调用链；不围着旧 API 做永久适配层。实现期间可以保留尚未迁移的正确路径，
但它表示任务未完成，不能以“兼容旧入口”为理由留进最终架构。

| 删除或重写对象 | 最终去向 |
| --- | --- |
| `Database.apply_snapshot_write()` 与通用读快照升级写循环 | 按调用者拆只读准备或直接短读改写；所有运行时写入走 `immediate_session()`，随后删除旧入口 |
| `Memory.apply_evidence_write()` 的独立 BUSY 循环、混合读写事务包装 | 改为领域证据准备、当前依赖复核和原子变更；删除第二套事务调度 |
| 仅为旧同 session 准备/首写分界维护的监听器、session.info 标志及缓存 | 新调用链不再使用的直接删除；仍有真实证据含义的数据归领域 owner，不换名保留旧机制 |
| 仓库自己拥有的写 `session.begin()` 与隐式写分支 | 外层拥有统一 writer，内层显式接收同 session；取消自行再开事务的分支 |
| 同时决定是否拥有事务、是否写入、是否重新准备的层层包装 | 收回外层提交 owner，删除已无真实调用者的可选策略；不再复制一套 writer facade |
| 把普通 SQLite 竞争当日常控制流的局部 catch/retry | 调用迁走后删除；仅保留外部竞争分类和真正的领域恢复 |
| 同文件重复 Database/engine 装配与独立 writer 锁 | 合并资源 owner，删除重复生命周期与锁 |
| 为锁内扫描、重复序列化或重复关联查询服务的中间对象 | 准备前移、批量读取或原子 SQL 后删除，不追求一一对应的新 DTO |
| 只验证旧 wrapper 被调用、固定 retry 次数或旧标志存在的测试 | 删除或改为真实并发、身份、原子性、取消和恢复行为验证 |
| 为猜多个竞争writer而维护的holders/first-write/physical-ID观测与driver monkeypatch | 由公共writer的已知owner与实际阶段计时替换；删除仅为观测而patch dialect/raw_connection的代码，真正driver退出确认由DB正确性边界负责 |
| HintSession与after_commit监听 | 全量迁完后由公共writer根commit成功后同步publish原hint；显式live span通知保留，不新增可靠通知设施 |
| Memory job claim的`updated_at=updated_at`、Dream.create_run的`UPDATE … WHERE false()` | 删仅为抢writer而发的空UPDATE；公共IMMEDIATE已取得资格，原claim/source/双owner条件仍在同writer核实 |
| 对旧升级重试流程的现行文档、配置与无消费者诊断项 | 同步删改；日期事故报告保留当时事实 |

删减量以完整替换为准，不用减少行数冒充正确，也不因涉及文件多而退回逐点补丁。
不新增通用 Plan/Command、持久 writer 队列、另一份提交 journal 或新的运行时审批状态。
原事件、Person、来源与可见范围、已付检查点、预算和外部效果回执继续归原领域所有。

## 4. 最终执行顺序

```mermaid
flowchart LR
    A[模型、文件和较重的证据准备] --> B[完成并关闭读 session]
    B --> C[等待共享 writer 准入]
    C --> D[新 session：BEGIN IMMEDIATE]
    D --> E[按当前事实短重查、CAS、写入]
    E --> F[确认 commit 或 rollback 清理完成]
    F --> G[释放 writer 准入]
    G --> H[现有回执、通知和文件回收]
```

### 4.1 普通短写

```python
# 示意：沿用现有入口和领域方法，不新增事务命令类型。
async with database.immediate_session() as session:
    row = await session.get(CurrentState, internal_id)
    # 在这个新事务里判断当前状态，再执行条件变更。
    await repository.update_current(row, session=session)
```

按主键或已有索引读取当前一行、判断、更新，本身就是短事务，不必额外拆成 prepare/apply。
调用者不在队列里携带带事务的 session，也不把另一个 session 的 live ORM 对象拿来修改。

### 4.1.1 明确读入口，收起公开 factory

最终由持久化层内部 `_sessions` 管理 factory，应用代码使用 `read_session()` 和
`immediate_session()`。前者只管理自己新建的读 session，设置 `autoflush=False`、不自动
commit；后者保留正常 writer 的 flush/commit 语义。外层已经传入的 writer session 直接复用，
不在子仓库把其 autoflush 关闭，也不额外开 reader。批内新建的 Account、Assignment、alias
及待 flush 数据仍须在同一事务可见。

只读入口不是拦截 SQL 的防御性子类。是否读写由真实调用和组合事务确定，不依赖方法名字。
`CanonicalIngressResolver.pre_admit()` 的冷身份/Conversation 创建就不是纯读，不能机械
替换为 read_session。schema/setup/test helper 的原始连接另有明确生命周期。

Python 3.12 SQLite legacy 模式中，SQLAlchemy autobegin 不等于数据库已经建立一致快照。
Ordinary admission、projection、Memory 等已有显式只读 BEGIN 的路径保留真实多查询一致性。
不要让所有单 SELECT 都额外执行 BEGIN；先按需要保留现有读快照，避免增加聊天 SQL 往返。

### 4.2 确实需要较重准备的写入

```python
# 示意：复用领域已有的输出、证据或发布准备对象。
prepared = await service.prepare(original_output, original_source)
# prepare 结束时已退出只读快照。
async with database.immediate_session() as session:
    await repository.revalidate(prepared, session=session)
    await repository.apply(prepared, session=session)
```

准备只产生本次操作需要的数据，不建立通用 Plan/Command 框架。已付模型输出、事件、
Person、run、mutation 和外部效果 ID 沿原身份保留；需要稳定生成的 ID 在可重备范围外分配。

现有准备接口也要改：`identity/person_state`和`routing.prepare_person_merge_routes`的apply closure
会捕获reader session和live ORM，
projection/publication有caller session归属。它们不能原封不动从旧reader带进新writer；保实际
准备字段和源依赖，由新writer的方法使用自己的session写入，不再保第三个混合事务入口。
Rebuild.plan未被当前调用者消费的supplied-session形态可直接删，或统一用已有prepared core，
不留下一个未来在writer中另开reader/扫历史的同名入口。

写时核对完整的真实依赖：来源是否仍可读、归属、权限、generation、相关 revision、
租约与目标状态。不能只比较行数、updated_at 或一个自造 hash，就认定所有依赖未变。
没有可靠修订号的依赖，读取本次真正依赖的相关行并比较实际字段；不得伪造版本。

依赖还包括原查询的完整选择谓词、集合成员和“不存在”条件。只重读准备时的旧 ID，
会漏掉后来新增的 evidence、关系、Assignment 或可见范围变化。需要按原谓词验证当前集合，
或使用确实覆盖该集合所有变更的已有修订；单行 fact.updated_at 不代表其证据集合未变。

这里的集合必须是**提交语义实际依赖的集合**。Memory selector 已在准备阶段唯一定位成
原 fact_id 后，后续 mutation 就认这个 ID；不能因为后来出现另一条同 key 的独立事实，又
重新跑 selector 并拒绝原操作。尚未定位的 selector 才需要按现行规则唯一解析。来源、权限、
证据集合与准备依赖仍须复核，不使用全库 revision/count/max/hash 把无关变化变成阻塞条件。

源发生变化时沿领域已有结果退出或重备纯数据库准备，不重调模型、重发消息。
若某条 Memory 准备目前不能在新事务中完整复核，就保留其原正确路径并把它列为未完成，
不能为了统一接口删掉来源保证。应调整相关查询、索引或领域依赖表达，使复核可行，
而不是永久保留旧 writer 或在短 writer 内加回全量历史扫描。该路径迁完之前，任务仍未完成。

### 4.3 保持原子组合

外层服务拥有 writer 和提交，子仓库复用传入的同一个 session。现有 `optional_session`
在有 session 时只复用，在自行拥有写事务时进入统一入口。

不做可重入 writer 锁。外层持一个连接，内层另开连接，放行可重入锁也不能让两个 SQLite
writer 同时成立；应直接修正调用关系。事务内不调用另一个自行开 writer 的入口。

## 5. 共享准入的职责

### 5.1 一个数据库资源 owner

- 继续由 `PersistenceModule` 装配 `Database`，所有仓库与诊断复用它。
- 一个运行进程、一个实际数据库文件对应一个 writer 准入 owner。
- 优先修正重复 Database/engine 的装配；确有同文件多 wrapper 时显式共享同一 owner。
- 不新建按 URL 字符串猜同库的全局注册表，也不在每个实例各放一把互不相关的锁后称其统一。
- 不同数据库文件互不排队；纯读继续用现有连接池。

准入在 checkout writer 连接、开启事务之前等待。只读准备 session 要先退出，避免排队时
占着旧快照或连接，另一条路径又等这条连接。

### 5.2 先采用 FIFO

用一个进程内等待原语安排调用者，不再建另一份任务队列、数据库排队表或 writer daemon。
执行仍在原协程，保留原 ContextVar、来源和取消归属。初版按 FIFO 取得资格。

自省的安静期和聊天退让在后台 worker 开始准备、领取批次之前完成。已有 DiagnosticWriter
继续限量异步消费；真正提交时也进入同一个 writer。两者都不另开“绕过公共入口的后台连接”。

心跳、已发送效果结算、请求检查点和取消收尾是必要持久化，不因任务来自后台就当成可丢诊断。
不先增加严格“前台永远插队”，以免饿死现有租约与效果回执。若公平短写仍不能满足真实需求，
再按同一窗口的等待证据评估调度，不能靠主观权重掩盖长事务。

已经开始的原子事务不抢占。前台到来时等它完成，后续后台工作在批次边界让路。

### 5.3 准入一直持有到真实退出

基线依赖为 `aiosqlite 0.22.1`、`SQLAlchemy 2.0.51`。aiosqlite 的 SQL/commit 在线程队列
执行，awaiter 被取消不等于队列中的函数停止；部分 SQLAlchemy 强制关闭路径也不能直接
作为物理关闭已经完成的证明。

因此，BEGIN、SQL、flush、commit、rollback 和必要的连接关闭由一个清理边界负责。
取得准入后，只有确认前一事务的驱动操作与清理退出，才允许下一 writer 进入。
不能在捕获 CancelledError 或调用 invalidate 后立即释放，留下后台仍在运行的写操作。
正常提交完成并归还 session 后继续复用连接池，不为每次写入关闭物理连接。
物理关闭确认用于确实进入失效/取消关闭的路径，不能把连接重建成本加给所有正常写入。

沿现有连接生命周期维护收尾任务和事实，不另造持久“writer blocked”业务状态。
若无法确认释放，按数据库连接失效与生命周期语义处理，不猜已回滚或授予业务重放资格。
通过现有数据库/应用生命周期停止该 owner 接纳新写并报告故障或退出；不能一直占着准入
却继续报告写入健康，也不能仅调用 invalidate 就放行。不新增持久故障状态表或人工解锁流程。
驱动适配集中在持久化层；实现前用锁定版本做取消复现，再决定最小适配或依赖修订，
不在每个仓库复制 shield、close 和异常分支。

主库writer的收尾不替代所有只读取消。`event_repository._history_read`会对正在执行的历史SQL
发interrupt并等原读取结束；它不是旧抢写锁重试。保其实际取消语义与query_history/history_around
的同快照上下文，不能在删除诊断用driver包装时一起删掉，也不把历史分词和正文匹配放进writer。

### 5.4 重试收口后的边界

| 情况 | 处理 |
| --- | --- |
| 同一 owner 下的正常并发写 | 在取得 writer 前排队，不把 BUSY 当正常调度 |
| BEGIN 尚未取得 writer 就遇到外部 BUSY | 由公共层识别真实阶段，沿当前取消和原执行恢复处理 |
| 准备的数据与当前事实不再一致 | 领域复核决定退出或重备，不重做已付或外部工作 |
| commit 回执不确定 | 复用已有执行回执或当前状态语义确认；无法确认时保留未知，不重跑整段 callback，也不新增通用提交 journal |
| rollback/close 未确认 | 不认定安全重试，也不提前让下一 writer 接替 |
| Work 租约已过期 | 停止原受保护执行，不因排队或重试延长资格 |

已有 5 秒 busy timeout 可作为外部竞争的驱动等待配置，不加长它来冒充架构修复。
本轮不新增通用自动重试次数、重试层级或后台排空状态机。

租约失效不授予新的执行或续租资格；它也不等于原外部效果没有发生。已付结果和真实发送
回执仍按各自现有结算条件保存，不能新增一条“当前lease不活就拒存所有结果”的公共规则。

## 6. 需要迁移的真实边界

### 6.1 数据库资源与进程边界

“统一 writer”指同一实际文件、同一运行进程的 writer owner，不是把所有 SQLite 都集中到
一个全局锁。以下全部进入本轮审查；主库写入全量迁移，独立存储保其真实 owner，只有发现
与本轮边界相关的问题才改变实现。跨文件/跨进程不能声称由同一内存队列提供原子性。

| 资源 | 实际入口与运行域 | 最终安排 |
| --- | --- | --- |
| `DATABASE_URL` 主业务库 | Bot 的 SQLAlchemy Database；消息、人物、Work、Memory、Social、插件、配置、诊断共享 | 唯一 writer 准入；并行读；收起公开 factory |
| Schema 验证连接 | `schema_guard.require_canonical_schema`，Bot 启动前 NullPool 只读 Schema | 不纳入运行时写队列；关闭临时 engine |
| Alembic / init-db / 治理 / reset | CLI/升级进程；当前 head **0147**；0145 有离线历史搜索转换 | 原停写维护范围；核现有 application lock，不新建持久维护协调器 |
| `participation.sqlite3` | 当前依赖 `addfcdde` SnapshotStore，宿主 AsyncSnapshotStore；原审`438f204f`到新pin的store.py无差异 | 保单专用线程、CAS revision 与取消后的真实收尾；不同于主库 writer |
| `web-search-bridge.sqlite3` | `web/bridge_state.py`；原生搜索/网页提取与多个调用实例 | 按该文件核短事务、首次 schema 初始化及并发；网络和正文提取不进其事务 |
| 表情 search/index SQLite | `emoji/sticker_index.py`；共享环境可重建检索索引 | 按真实路径/原子替换/现有锁核查，不与主库共排队；原图片文件保留 |
| workspace / short state | `workspace/store.py`、`workspace/short_state.py`；共享持久环境 | 区分查询与建表/首次写，核独立进程访问和原事实，不伪装主库事务 |
| 客户端 Outbox / ACP 本地状态 | `client_sdk/outbox.py`、`acp_adapter/agent.py`；用户客户端进程 | 保请求 ID、发送状态、续读游标；不接入 Bot 的进程内队列 |
| Sandbox `jobs.sqlite3` / Manager 状态 | `sandbox/persistent.py` 及其进程；主库另存 submission/completion/continuation | 各自短事务；提交回执/ACK 顺序按原接口，不能伪造跨进程一次事务 |
| 实验、打包与快照工具 | scripts/tools 中 sqlite3 或独立 Database | 核目标、副本/只读/维护模式；不因代码在仓库中就当 Bot 运行时 writer |

参与快照已通过单专用线程隔离同步 I/O，不再重复实现另一套线程队列。跨库反馈以主库原
feedback sequence/效果回执和 snapshot revision 接续；主库提交与 snapshot 保存之间崩溃
仍按原序号幂等追赶，不新建跨库提交 journal。

### 6.2 主库迁移总表

| 范围 | 当前入口和要做的事 | 不能破坏的事实 |
| --- | --- | --- |
| Canonical 入账 | `identity/canonical_uow.py` 的快照 callback；把较重账号观察与转换前移，新 writer 短核身份、路由、去重和引用 | 原 canonical ID、内部事件、入账回执与唯一性 |
| 共享事件写入 | `persistence/scoped_event_uow.py` 已在外分词，但 writer 内仍有身份、别名与会话准备；按真正依赖缩短 | 跨仓库账本、媒体、FTS 与水位的原子性 |
| Social | `social/repository.py`、`social/service.py` 的 prepare/claim/settlement 分别收口 | 网关只调用一次；成功账本、Social receipt、Work delivery 同事务 |
| 媒体与身份状态 | `media_repository.py`、`identity/ingress.py`、routing/metadata 的裸 begin 分类迁移 | 下载/复制在事务外；绑定变化不能重归历史 |
| 自省与 Memory | checkpoint、状态更新和 mutation 共用 writer；证据准备保只读，短写核来源与原逐项结果 | 原 run、已付输出、全部真实证据、mutation 回执和预算 |
| 诊断 | `DiagnosticWrite.commit()` 进入公共短写；现有有界消费保留 | 诊断可缺样，但不能阻断或推翻已确认业务效果 |
| Work/心跳/协议 | 状态、续租和 journal 使用同一 writer；文件准备与 protocol 锁顺序另保 | 原执行、真实租约到期、已付协议、文件与 journal 引用一致 |
| Dream、embedding、维护 | 分类现有 begin、领取和分页更新；没有候选不申请 writer | 不整体搬旧分支，不用快照重试替换已经正确的有界状态写 |

该表不是最终调用清单。以下方法级工作单元补齐原稿遗漏，每项同时核事务 owner、SQL/隐式
flush、锁序、同事务组合、来源重核、当前时钟、已发生外部效果和真正可删除的旧逻辑。

| 单元 | 具体实现与拆分点 | 必须保持的提交单位 / 核查重点 |
| --- | --- | --- |
| U01 主库装配 | `container.create/__init__`、`PersistenceModule.build`、所有 application modules | 主库同实例；启动恢复、诊断、插件不能另开同文件 owner |
| U02 入站前置身份 | `identity/ingress.py: pre_admit/_admit/_hydrate_for_message` | 保已有回执/热路由纯读快路；冷探针在外；Person/Assignment/Conversation/alias 的真实创建短写，写内核当前连接和不存在条件 |
| U03 Canonical receipt | `identity/canonical_uow.py`、canonical repository 的 compose 方法 | 原 ingress ID 在重备范围外；去重和关联事件同提交，网络不重跑 |
| U04 事件账本 / FTS | `scoped_event_uow._append_canonical`、event repository、history index | canonical 关联、ChatEvent、媒体索引、FTS 与水位原子；解析/分词在外，写内不扫历史 |
| U05 身份控制与账号转移 | identity repository、people_repository、admin/person/control resolution | Person 稳定；当前 Assignment 与历史原 owner 分开；原 request receipt 与 mutation 原子 |
| U06 配置 / 管理审计 | `RuntimeConfigService._mutation_session`、AdminAudit.transaction、control receipts | 调用者已有锁按实际顺序处理，RuntimeConfig本身无配置mutex；保现有业务/audit/control原子组合，PreferenceAdmin按下文真实两阶段；文件校验/插件 validator 在外 |
| U07 启动 / 元资料 | `bootstrap_settings_identity`、space_metadata、membership/观察名、Character/SOUL grants | 启动写与通知回调也进 owner；不为鉴权、SSE 读取重新加 last_used 写 |
| U08 普通接纳 / projection | ordinary admission、projection repository、hydrate/reset、Compact | 多查询真实只读快照保留；候选准备在外，源 revision/head/CAS 在提交时核；无变化快路不写 |
| U09 外部工具证据 | `tool_results/recorder.py:record_receipt` | 不是可丢诊断；来源/SELF/隐私谓词、原 effect/call key、唯一性保留，补记不重调工具 |
| U10 Artifact | `ToolArtifactRepository.write_artifact/add_refs/release_refs/cleanup` | 文件准备在外；metadata/真实 owner refs 原子；标 deleting→unlink→确认删除，提交不明不删可能已拥有文件 |
| U11 Social 正常结算 | `social/service.py`、repository、emoji_delivery、plugin facade | 网关调用在外；能确认的正常路径 outgoing ledger + receipt + Work delivery 同事务 |
| U12 Social 失败后补记 | `social/service.py` 已发后的 fallback；`plugin_host/facades.py` 对应 fallback | 现实现可只补 succeeded receipt、缺 ledger event；明确平台已确认与本地落账不明的区别，不重发，不虚构完整账本 |
| U13 媒体收取 / 回收 | ConversationMedia、ConversationUploads、MediaAnalysis.save/associate_event | 原件下载/写文件在外；media lock→writer；原媒体、引用事件、分析关联和 24h 清理分别保真实事实 |
| U14 Work 基础状态 | WorkRepository acquire/renew/release/accept/transition/checkpoint/stage/consume/cancel 等 | 同 writer 内核 fence/owner/cancel_epoch/generation；取得 writer 后取真实时间；same-session helper不再排队；record_effect内部原receipt-bytes CAS不是BUSY循环，不误删 |
| U15 Work 已付发布 | work_session.save/_publish 与 work_journal 请求/响应提交 | paid 输出、预算、原协议与 original IDs 保留；数据库重备与重新调用模型分开 |
| U16 Protocol / Journal | ProtocolStore.publication、work_journal.record_effect/invalidate/recovered_inputs、GC | protocol lock→writer，GC 各阶段 writer 退出后才删文件；不能反序等锁或删已引用对象 |
| U17 Work 输入 / 等待 / 交付 | WorkWait.deliver_due、prepare input、effect/delivery receipts | 事件匹配、输入消耗、交付意图按真实原子单位；计数/lease用实际提交时钟，不每段重置预算 |
| U18 Subagent 归档与资源 | SubagentRepository.maintain、ResourceCleanupPreparation | 大任务树/JSON 不因候选只有 16 个就视为短；先关 peers 是已发生效果；新增后代/request/peer 的缺席依赖也重核 |
| U19 主动反馈 / 跨库 | `participation_feedback._commit_pending/_read_facts` | 目前显式 BEGIN 后聚合再写；拆只读准备与短重核；核 feedback seq、真实 effects、消费集合，随后原 snapshot CAS |
| U20 模型诊断 | DiagnosticWrite.commit、ModelInvocation.record、TraceRecorder 与 cleanup | 生产 DI 共享有界 DiagnosticWriter；writer=None fallback 也收口；成功模型不会因诊断失败被推翻；cache缺失仍NULL |
| U21 插件安装 / 配置 / KV | plugin repositories/state/configuration/storage | validator 与外部 hook 在外；插件状态 revision、audit/control receipt 按原事务；不把插件 KV 迁到新库 |
| U22 插件会话 / Job / 通知 | session_repository、background turns、notification intents/outbox/grants、notification_delivery、media_artifacts | Job↔原Work、批准route、publication/receipt不拆散；通知外发后当前_record_outbound与finish_outbox是两次提交，最终合成一个prepared短writer，保已知message_id的uncertain，不因缺outbox完成重发 |
| U23 Sandbox Host 账本 | task_runs、completions、continuations、peer admission | 意图先存，再远程submit；completion先持久化再ACK；不让本地重试重发原任务 |
| U24 Memory 基础写 | MemoryFactRepository / MemoryJobRepository 的短状态、目标、关系与来源操作 | 简单按键读改写直接进 writer；全部 in-session helper 归同一个外层 owner |
| U25 Memory Mutation | mutation/service、provenance、source_resolver、ownership | 证据完整集合与源状态在新 writer 核；same_targets/person_owns_fact 不隐式另开 reader；保新增 evidence/relation IDs 和原 receipt |
| U26 Self Reflection | scan/claim_due/claim_initiative/save_checkpoint/complete/recover/_advance_state | 正文渲染/hash/剩余历史计数移出 writer；原 batch/run/paid输出/逐项mutation结果保持；不把安静等待包在事务内 |
| U27 Dream | dream repository/service、consistency、候选/prepare/rollback | 双owner、来源重绑、依赖不存在、后续 mutation 与 relation 所有权都要重核；rollback不是通用数据库回滚 |
| U28 Embedding | embedding jobs/backfill/vector_store/runtime、worker | HTTP和hash/编码在外；原claim/fact字段CAS、批量upsert、过期claim保语义；读向量/health不进 writer |
| U29 Rebuild | rebuild repository/service/worker、staged adoption/finalization | 扫历史/抽取在外；原scan水位、target、付费结果、adoption receipt、隐私清理与最终完成凭事实，不新增经验完成门槛 |
| U30 管理治理 / 清退 | Memory maintenance、quality/hygiene、privacy erase、各 cleanup | 不删除真实原件/回执；治理原子性不按页拆坏；非原子维护可按有真实候选的页提交 |
| U31 Schema 隐式效果 | ORM/Core schemas、FTS/projection/quota/artifact triggers、FK cascades | 触发器成本算在事务内；保真实约束；97个冻结revision不改写，只有真实结构变更才加迁移 |
| U32 生命周期 / 健康 / CLI | lifecycle/main/container、应用锁、admin API/CLI、schema guard、tests fixtures | 先停产生者再收尾，再排空诊断，最后关库；只读健康不证明 writer 可用；不同进程如实分流 |
| U33 Automation定义与执行 | automation/repository、service、worker、run_utils | 定义/版本/审计与真实run/occurrence分别保原子；create/status/cancel/resume/schedule/publish/claim/release/renew/finish逐方法迁移，不以Work已收口代替 |
| U34 CommitHints/SSE唤醒 | persistence/commit_hints.py、client_api/views、TraceRecorder live span与事件订阅 | 现HintSession.after_commit在savepoint release也触发，不能当root commit证明；最终统一writer确认根commit成功并释放owner后同步publish，无可取消后置task；live span显式通知保留，不新建可靠通知账本 |
| U35 主库旧派生资料维护 / ASR | web_repository.cleanup_expired、media_repository、processor._prepare_audio_source、scoped._set_derived_text | web source index已退役、没有save_run，只保历史行cleanup；ASR付费后写audio_transcript/FTS，模型不重做；独立BridgeState不是这张旧表 |

Work 的 publication 先完成文件准备和核验，再取得数据库 writer；现有协议锁保护到
refs/journal 提交。实现时列出全部相关调用的锁顺序，禁止出现一条路径 protocol→writer、
另一条 writer→protocol。持 writer 时不等待文件锁、读文件或扫描协议目录。

Work publication 重发同一已付结果、心跳在原租约内重试，与普通数据库 callback 重试不是
同一个职责。它们可以共享错误分类与 writer 入口，不能为了删重复代码丢掉各自恢复语义。

### 6.3 跨资源锁顺序

以已经存在的资源锁为准，不另造“全局业务锁”。需要在实现中列出真实组合调用：

| 路径 | 顺序及 writer 中允许做的事 |
| --- | --- |
| 协议发布 | 文件编码/准备→protocol lock→只读准备退出→writer→refs/journal提交→解锁 |
| Protocol GC | gc lock下候选→短writer标记→writer外protocol lock与unlink→退出文件锁→短writer确认 |
| Artifact 发布/GC | artifact storage lock→准备/文件操作→短writer；引用helper只用传入session |
| 媒体收取/过期 | media lock→文件准备→短writer；writer不反向请求media lock |
| 配置与控制 | RuntimeConfig现有_mutation_session不额外加进程mutex；沿调用者writer复用，核涉及的配置/控制锁真实顺序，不发明不存在的config锁 |
| SOUL 文件变更 | SOUL lock→reserve writer退出→文件发布/热装配→finish writer；不把整个文件操作合进一个writer |
| 参与快照 | session/save lock→独立SnapshotStore线程；主库feedback提交结束后再推进snapshot，不能同时持主writer等snapshot线程 |
| 关闭 | 仍可能写的产生者完成→原状态收尾→诊断排空→Database结束；writer不等另一个需要writer才能退出的worker |

真实锁表与原子关系由逐文件报告补充；若发现调用形成反向边，改调用关系，不用可重入锁遮住。

### 6.4 原子单位按领域事实划分

统一提交入口不等于把一整轮模型、一个后台cycle、一个列表都合成一笔大事务。以下单位决定
拆分位置；既不能扩大持锁范围，也不能拆散原本必须同时成立的事实。

| 操作 | 一次提交的实际单位 | 相邻阶段 |
| --- | --- | --- |
| Canonical入账 | 当前receipt、对应chat event与该次FTS/媒体/水位效果 | 前置冷身份创建有其已有独立边界，不让网络探针进入writer |
| 数据库管理命令 | 领域变更+audit+原request receipt；局部可缓存失败用原savepoint | 变更回滚后仍可保存真实失败receipt |
| 外部管理 / SOUL | reserve、effect/文件、settle是三阶段 | 只有两个DB阶段各自短事务，外部效果不重放 |
| App Work input | 入站event+mailbox接纳+personal request receipt | waiting/lease重认领按原Work恢复，不换UUID |
| Work协议发布 | 该次journal、准备好的protocol/media refs与指定publication | 文件准备在外，paid模型与预算各阶段按原事实，不合成全回合事务 |
| Live Memory提取 | 每个claim的事实/关系+mutation receipt+原job claim fence | 多claim逐项提交，之后jobs.complete；不改成整批模型输出一起提交 |
| Self Reflection | 每条proposal/episode的mutation+ReflectionResult | paid输出checkpoint、已完成条目、run完成与水位另有阶段；失败保部分进度 |
| Dream决策 | 一个cluster的全部actions、operation/mutation及preview消费 | rollback_run逐operation回退，不把所有cluster合一大事务 |
| Rebuild | 单proposal采用的事实变化+原proposal receipt；receipt sweep每页完整finalization | 不虚构这条路径存在通用MemoryMutationReceipt；扫描/付费抽取、暂存、采用、隐私清退和run完成各自依原状态 |
| Embedding | 当前页各fact/claim条件下的vector upsert与job完成 | Provider/hash/编码在外，原有页边界保留 |
| Social正常已确认 | outgoing ledger+Social receipt+Work delivery | 平台调用在外；补记缺ledger/unknown另认真实事实 |
| 插件notification publication | external event+原wait匹配+outbox/background job | 已有原子组合保留 |
| 插件notification外发后 | 计划改为outgoing ledger+finish_outbox一次提交 | 保已有两提交间崩溃的旧状态读取，不据message_id缺口重发 |
| Sandbox完成/许可事件 | 当前终态/原input/wait/continuation的既有Host原子单位 | Bot持久化后才ACK Manager；两份数据库不合并 |
| 主库feedback与参与snapshot | feedback sequence/refs先在主库提交 | snapshot独立CAS；失败按原seq追赶，不作跨库事务 |
| Artifact/Protocol/媒体GC | 短事务标记与后续确认各自独立 | 文件unlink在相应writer之外；引用/版本条件保留 |

自省`_advance_state`的准备还要考虑本次将提交的状态：complete/recover当前会先将run在ORM
中变成completed，再计算仍未完成窗口与剩余事件。移出准备后要按“本次run完成后的逻辑结果”
计算，不能在旧processing状态上生成假空洞。写时核真实水位、gap集合、相关源与generation；
先查仅作预筛/统计的辅助字段是否可删除，避免把整份历史搬到writer里重扫。保留真实失败空洞
与批次证据，不为避免重扫伪造一个全库revision或再加影子计数账本。

## 7. 实施任务

以下是实施工作单；本次只完成调研和任务书，不据此提前勾选产品实现。每项的源码范围见
U01–U35，验证场景见 V01–V50。可按文件归属并行实现，依赖按下表顺序处理；不新增运行时门槛。

### A. 共同边界（先完成，再迁移各领域）

| 编号 | 实施内容与交付物 | 依赖 / 验证 |
| --- | --- | --- |
| W01 | 以实施时 main 重新生成文件、方法、SQL/Schema 清单；标读、写、组合、独立库、离线、排除及理由 | U01–U35；不能只搜 BEGIN |
| W02 | 画出实际文件/进程/Database/worker装配图，修正主库重复owner；独立库分开 | W01；V08/V19 |
| W03 | 收起公开factory，建立read_session；按真实读效果迁移，不改写writer autoflush | W02；V06/V20/V21 |
| W04 | 在现有immediate_session接FIFO，等待发生在checkout/BEGIN前；连接复用 | W02；V01/V07/V08 |
| W05 | 固定driver下复现排队/BEGIN/SQL/flush/commit/rollback/close取消，决定最小收尾实现 | W04；V09–V11 |
| W06 | 保原错误码、阶段、rollback/invalidation事实；unknown沿工具/SDK/Work保uncertain=True与mutation_committed=None，不误记False或允许重派 | W05；V12/V22 |
| W07 | 调整optional_session及自管UoW：外层拥有提交，内层必复用session | W03–W06；V05/V21 |
| W08 | 列全部业务锁→writer边；删嵌套writer与反序调用；不加可重入锁 | W07；V14/V23 |
| W09 | 调整启动失败/正常关闭/重复取消收尾顺序，停止接纳时点放在真实产生者退出后 | W05/W08；V24/V25 |
| W10 | 复用现有计时，内存聚合准入等待/事务占用/收尾；区分读取可用和writer停止 | W04/W09；V15/V26 |

### B. 入站、控制面、公共历史和媒体

| 编号 | 实施内容与交付物 | 依赖 / 验证 |
| --- | --- | --- |
| W11 | U02 保已有receipt/hot route纯读快路，拆冷身份/Assignment创建与外部探针 | A；V27/V28 |
| W12 | U02 将真实Conversation/alias初始化归明确短写，写内核不存在谓词 | W11；V03/V27 |
| W13 | U03 原ID一次分配；canonical入账准备/短重核/写入；去旧快照升级 | W11/W12；V01/V02/V28 |
| W14 | U04 账本/媒体/FTS/水位一次提交；历史转换前移，保触发器效果 | W13；V05/V29 |
| W15 | U05 Person/账号/Assignment/合并与原control receipt同事务；拆person_state与route旧session闭包，按下列真实删除/合并依赖核全集与absence | A；V03/V28/V30 |
| W16 | U06 Config mutation锁序、revision CAS、audit/control原子性；validator/file准备在外；legacy rollback的merge配置谱系按下文拆准备 | A；V23/V30 |
| W17 | U07 bootstrap、元资料、名称观察、Character/grants收口；鉴权/SSE保持读路径 | W15/W16；V20/V31 |
| W18 | U08 ordinary admission只读快照与短认领分开；来源/activation唯一性保留 | W13/W14；V02/V32 |
| W19 | U08 projection读取/append/reset/invalidate、Compact当前head CAS完整迁移 | W14/W18；V03/V29/V32 |
| W20 | U09 tool receipt条件INSERT迁移，保source_call_key/SELF/privacy；原工具结果补记 | A；V12/V17/V33 |
| W21 | U10 Artifact prepare/publish/refs/GC各阶段收口；保文件与主库引用生命周期 | W08/W20；V14/V34 |
| W22 | U13 ConversationMedia/Uploads原件与过期阶段收口，media lock顺序核全 | W08/W14；V23/V35 |
| W23 | U13 MediaAnalysis save/associate_event/job_claims/cleanup真实写迁移，模型在外 | W22；V05/V35 |
| W24 | U06/U07 同步管理HTTP与客户端错误映射，未知提交不变成可重发unavailable | W06/W15/W17；V22/V36 |
| W67 | U33 Automation定义/版本/create/set_status/cancel/resume/schedule_now/publish_task迁移；JSON/hash外备，审计原子 | A；V30/V37 |
| W68 | U33 claim_due/release/renew/create_run/finish_run/occurrence与重启settlement逐项迁移；核until是否排队前计算 | W25/W67；V13/V37/V38 |
| W69 | U34 hint归根commit确认后、owner释放后同步publish；取消已到但确认commit成功也先publish再传播取消；删HintSession监听，保两处live span显式通知 | W04/W57；V20/V24/V30/V36 |

### C. Work、Social、插件与Sandbox

| 编号 | 实施内容与交付物 | 依赖 / 验证 |
| --- | --- | --- |
| W25 | U14 acquire/renew/release/accept/transition等全部owner方法收口，writer后取时钟 | A；V13/V37 |
| W26 | U14 checkpoint/patch/stage/consume/cancel及in-session组合方法逐项迁移 | W25；V05/V17/V37 |
| W27 | U15 paid响应发布保原stage/finalize/journal，不把未知commit送回整段重试 | W06/W26；V10/V17/V38 |
| W28 | U16 protocol publication/record_effect先文件准备再writer；所有refs/journal同提交 | W08/W27；V14/V34 |
| W29 | U16 Protocol GC保原标记/线程unlink/确认阶段，改掉任何反序等待 | W28；V14/V23 |
| W30 | U17 input prepare/enqueue/consume/wait/effect/delivery核原子单位与当前lease | W26/W28；V05/V37/V39 |
| W31 | U18 Subagent maintain/归档与cleanup准备拆分；核新增后代/request/peer与已发生close | W25/W30；V03/V40 |
| W32 | U11 Social prepare/claim/dispatch/settle，正常confirmed路径三方原子提交 | W14/W26；V12/V41 |
| W33 | U12 Social/插件已发后的补记收口，缺ledger与平台unknown如实保留，不重发 | W06/W32；V12/V41 |
| W34 | U11 emoji delivery、媒体路径发送、跨入口回执复用进入原Social owner | W22/W32/W33；V35/V41 |
| W35 | U19 feedback完整读准备与短重核拆开；保原seq/消费集合后再save独立snapshot | W26/W32；V03/V42 |
| W36 | U20 ModelInvocation与Trace所有fallback/cleanup也进公共writer，生产DI仍共享诊断队列 | W04/W10；V15/V43 |
| W37 | U21 插件安装/config/KV/audit的自管事务收口，原validator外置不重复造Plan | W16；V05/V44 |
| W38 | U22 插件session/background job/notification/grant/publication/outbox完整迁移 | W26/W32/W37；V17/V44 |
| W39 | U22 plugin媒体Artifact与真实publication引用关系核当前文件/源/许可 | W21/W34/W38；V34/V35 |
| W40 | U23 Sandbox host的submission/completion/continuation/peer短写与ACK顺序逐项核 | W26/W30；V17/V45 |
| W41 | 保独立Manager jobs与AsyncSnapshotStore现有owner，补取消/恢复跨界验证 | W35/W40；V19/V42/V45 |
| W70 | U22 notification_delivery先备出站event/分词，再同writer写ledger+finish_outbox，删除两次独立提交；补旧中间崩溃恢复 | W14/W33/W38；V12/V41/V44 |
| W71 | U22 plugin media_artifacts.create改按原handle确认提交或交孤儿维护；提交未知时不能直接unlink已可能登记的文件 | W06/W21/W39；V10/V34/V35 |
| W72 | U20 删旧多writer holders/first-write/物理ID观测和仅为观测的dialect/raw_connection patch；保简单SQL计时/TimingSummary，writer阶段由owner记录 | W05/W10/W57；V10/V11/V26 |

### D. Memory全链路

| 编号 | 实施内容与交付物 | 依赖 / 验证 |
| --- | --- | --- |
| W42 | U24 基础fact/job/关系/目标短写统一；简单操作直接读改写，无新DTO | A；V04/V05 |
| W43 | U25 对每一种mutation写清真实证据、owner/target集合、缺席依赖和当前源复核SQL | W42；V03/V46 |
| W44 | U25 修same_targets/person_owns_fact等校验的隐式新reader，全部用当前writer session | W43；V05/V21/V46 |
| W45 | U25 拆重准备/新writer完整核验；保新增evidence/relation IDs、copy来源与原operation receipt | W43/W44；V03/V17/V46 |
| W46 | U25 迁完调用者再删Memory第二套BUSY循环、旧mixed transaction、仅首写分界的监听/标志及job claim空UPDATE；保原claim条件 | W45；V02/V06/V46 |
| W47 | U26 scan/claim_due/claim_initiative移正文/hash/阈值计数准备；writer核当前run/完整候选 | W42/W45；V03/V47 |
| W48 | U26 checkpoint/complete/fail/defer/recover/_advance_state，保paid输出与水位推进真实范围 | W47；V17/V47 |
| W49 | U26 与独立静默分支合流时核主入口/取消/关闭，不重复实现调度，不把quiet等待放事务内 | W48；V13/V24/V47 |
| W50 | U27 Dream候选/claim/prepare/finished/rollback核双owner、来源变化和完整关系依赖；删create_run仅抢writer的WHERE false空UPDATE | W43/W45；V03/V46/V48 |
| W51 | U27 将Dream回退需要的原mutation/evidence/relation receipt保为领域数据，删旧锁包装 | W50；V17/V48 |
| W52 | U28 embedding hash/HTTP/encode保持writer外，批量claim/upsert只核原字段与lease | W42；V03/V37/V49 |
| W53 | U28 vector read/health/backfill按真实读写分流，保无变化快路，避免新增逐行验证SQL | W52；V07/V20/V49 |
| W54 | U29 rebuild扫描/抽取/暂存/采用/完成分别列事务，重核完整target与源选择集合 | W43/W45；V03/V17/V50 |
| W55 | U29 最终完成、receipt认领、源隐私变化/擦除，保付费结果和已完成项，无新增经验门槛 | W54；V17/V50 |
| W56 | U30 maintenance/治理/隐私清退按业务原子性区分；大量可独立维护分页，不拆坏整次授权变更 | W42/W45/W55；V03/V29/V46 |

### E. 全量删除、验证与交付

| 编号 | 实施内容与交付物 | 依赖 / 验证 |
| --- | --- | --- |
| W57 | 逐项迁完所有apply_snapshot_write调用，删公共快照升级循环；剩余纯读BEGIN逐项确认 | W11–W56/W67/W68/W70/W71/W73；V02/V06 |
| W58 | 删仅旧锁竞争用的wrapper/首写flag/重试/中间缓存/重复owner；保来源、paid、真实receipt与业务CAS | W46/W51/W57；删除账逐项回查 |
| W59 | 对schema trigger/FK/FTS与head0147/create_schema/guard做一致性检查；真实索引变更才新migration | U31；V29 |
| W60 | 独立库、CLI/offline/打包/快照工具与test fixtures改新API；不把它们错误并进主库队列 | U32；V16/V19/V25 |
| W61 | 更新现行开发合同与各模块文档；删旧流程的现行说明，日期历史报告不改验收事实 | W57–W60 |
| W62 | 文件型WAL、多连接、真实driver取消与故障场景回归；复用已有测试并补实际缺口 | V01–V50按领域映射 |
| W63 | 同数据、同到达负载、同mock模型/传输的前后对照；分别报告quiet调度与writer收益 | §9；V18 |
| W64 | 对实施代码再做三轮全范围审查：入口与删除、语义与交错、性能与剩余绕行 | W62/W63；不拿本次计划审查代替实施审查 |
| W65 | 提交、PR、CI按授权与事实推进；最终包含全部迁移与旧机制删除 | W61–W64；不独立部署半覆盖 |
| W66 | 上线沿现有停旧Bot/升级/启动流程；自然流量核writer与延迟，分开记录merge/release/deploy | W65后另按实际授权；不发真实测试消息 |
| W73 | U35 主库旧web源/media缓存清理纳入owner，保无候选纯读与分页cascade；ASR沿现有预分词/源重核，不恢复退役索引写 | W14/W23；V20/V29/V35 |

错误阶段不能只在DB底层区分。`client_api/http_errors`、`services/agent_tools`的工具错误映射、
SDK facade的错误类别、`lease_heartbeat`、`work_session._save`和Work failure/recovery都必须
保留同一个真实阶段。旧循环仅看BUSY code或cleanup note不足以确认没有提交；不能把unknown
洗成`retryable=True/database_failure`让模型或客户端重新派发原效果。尤其`agent_tools`的旧
`_result`默认`uncertain=False`，会进一步生成`mutation_committed=False`；backend的
`resolve_mutation_commit`先看uncertain，再对not ok直接判False。unknown必须沿typed outcome
保留uncertain=True与commit=None，
不能只把retryable改成False却仍宣称确定未写入。`record_effect`的原receipt
bytes CAS循环与SQLite异常重试不同，保其迟到事实，不要求新的活lease。

同session审查也不限于W44的两个helper：`services/admin/memory_admin`的授权方法会先调用
`_actor_person_id`，缺原actor.person_id时经ledger.get_event隐式开reader。把原内部事件actor
解析放在准备阶段，或沿完整helper链传当前session；不以今日平台账号补造原人物。

W15的forget准备/提交复核不能只列Person family IDs和旧行CAS。实际依赖包括private
Conversation/Assignment族、由mentions派生的原正文redaction与Presence平台、tool/artifact/Work
复制来源、initiative sources/support_refs、SELF checkpoint四类map与旧rendered envelope、
audit JSON实际解码匹配和原快照，以及将删除的resource closure。按这些原谓词核当前相关集合，
避免拆掉同一WAL快照后漏清新出现的引用；不能用全库世代代替，也不强迫writer重新解码全历史。
merge还要核两root资料、目标config key/plugin对象absence、source旧名alias absence和配置版本。
route继承取决于source/target route、目标Person enabled、Binding/Presence及Registry真实候选；
只核route不存在或observed值不等于核全候选。外部membership探针继续在writer之外，沿当前
`_provision`已有的binding版本/原连接/候选Presence复核方式处理真实依赖，不新增路由策略。

W16还包括`config_service._matches_original_state`：旧audit无row_id、配置又随Person合并迁移时，
当前会扫描同人族成功merge的after_json并解码configuration_moves谱系，且可能位于
database_command的writer/savepoint内。将原audit与相关谱系准备放在一致reader，writer核
实际audit、配置行/版本、当前root与相关move集合/absence；保有row_id的现有快路。不能把
整个rollback套IMMEDIATE后宣称短写，也不用全局配置版本或新journal代替其真实来源。

管理侧也按实际组合迁移：GroupAdmin._set_enabled在AdminAudit.transaction内用同session改行
和记audit；PreferenceAdmin的事实变更、后置audit目前是两个阶段，分别收口。不要在外层
writer调用整个公开Memory/Admin服务，再由里面另开writer；有原子组合的链路传同session，
不把当前两个阶段写成已经同笔提交，也不借统一准入扩大成另一项全管理流程改造。

W42/W43的`delete_preference`当前按人物读全部preference再用Python筛key。实施时按原
target/kind/key直接查询歧义判断所需候选，不把全人物正文扫描机械搬进IMMEDIATE。
lease/claim资格和续期需要准入后时钟；WorkWait.register的after_seconds、time_due等是原请求
约定的时间基准，不能因为统一writer就全部重算为排队后的时间。原计划/发生时间按事实保留。

WorkSourceGuard检查跨入口client_input原事件后，writer不能只核本Conversation的revision。
W27/W28须列当前实际选定的cross-source身份/小列/源版本与隐私依赖，按真实关系在写时重核；
新聊天追加与已选原事件被修改/擦除不同。不在writer重扫整份transcript，也不新增全库版本门槛。

热点只决定实现顺序，不能缩小最终覆盖范围。分工/分提交可并行，完整替换一起交付；不把
“只接入公共锁、仍有裸写”作为单独上线版本，也不建立永久的新旧双入口。

编号用于跟踪同一项修订，不按数字推断依赖或优先级；新增Automation项放在其实际所属阶段。

## 8. 验收矩阵

| 编号 | 场景 | 要确认的行为 |
| --- | --- | --- |
| V01 | 同文件 WAL、多连接并发入账与后台写 | 受控路径有序进入，原事件和回执唯一；语句冲突不再充当内部调度 |
| V02 | barrier 复现旧快照后另一连接提交 | 旧实现稳定复现 517；新路径退出准备快照并按当前来源重核 |
| V03 | 原模型输出准备后，来源擦除、owner/权限/generation 改变；新增相关证据或关系而旧 fact 行不变 | 按原选择谓词和完整依赖集合复核，不写回旧来源、不漏新成员；保原输出与已发生结果，不重调模型 |
| V04 | 普通主键读改写 | 无多余 DTO/快照循环，条件变更及返回值正确 |
| V05 | 一个服务组合多个仓库 | 传递同 session；任何一步失败，全部原子回滚，无嵌套排队 |
| V06 | dirty ORM 后 SELECT、commit flush、Core 写 | 全部实际写入都在所属 writer 边界内，读 session 不意外写入 |
| V07 | writer 活动时另一个 session 纯读 | WAL 下仍可读取已提交内容，未把读池变为单队列 |
| V08 | 同库多个 wrapper、不同数据库文件 | 前者显式共享 owner，后者互不阻塞；测试不冒充跨进程协调 |
| V09 | 排队时取消 | 不留队列/名额，不发起 SQL，其他等待者继续 |
| V10 | SQL、flush、commit 阶段取消或重复取消 | 下一 writer 等前一物理操作和清理退出；原错误和提交不确定性保留 |
| V11 | rollback 或关闭失败 | 不声称安全重试，不让旧驱动仍写时放行新 writer |
| V12 | Social 已发送或发送结果未知，本地提交确认丢失 | 分别确认平台效果与本地结算；缺失本地回执不证明没发，不据此重发，也不把未知写成已结算成功 |
| V13 | 前台持续写、后台准备、心跳并发 | FIFO 可推进；自省先退让；过期租约不能因排队获得新资格 |
| V14 | Protocol publication 与 GC 并发 | 文件锁与 writer 无反向等待，已引用文件不被删，journal 保原已付配对 |
| V15 | 诊断满队列、失败和关闭 | 报告缺样，不延迟或推翻已经成功的回复、模型和业务提交 |
| V16 | 外部进程持有 IMMEDIATE | 如实等待/报忙；不把进程内准入说成覆盖了外部写者 |
| V17 | 重启与已付检查点恢复 | 原 ID、预算、mutation/发送回执不变，不重放外部操作 |
| V18 | 当前热点的前后对照与规模数据 | 报总操作数、吞吐、准入排队、事务持有、响应延迟和冲突结果，证明少重试未换来更长前台队列 |
| V19 | 各独立SQLite、同文件多实例、不同进程、首次“读” | 按资源表分别核owner/短写/建表/已有OS锁；不让主库队列阻塞独立库，不声称内存锁跨进程 |
| V20 | 只读鉴权、GET/SSE、配置initialize、向量/FTS/health | 无新增业务DML或writer等待；已有多查询一致读保留，单查询不机械多BEGIN |
| V21 | 同session新建Account/Assignment/alias后又查询，组合repo被调 | 内层不另开连接/不改变autoflush/不再接管提交，批内可见性和唯一性保持 |
| V22 | BEGIN已确认失败、SQL失败已回滚、commit ACK丢失、rollback/invalidate失败 | HTTP/工具typed outcome/SDK/Work保原阶段；unknown不变成uncertain=False或mutation_committed=False，不重派原效果；只有事实允许的纯DB重备 |
| V23 | protocol、artifact、media、SOUL、config、关闭各锁交错 | 真实锁序无环；writer内不等待反向资源锁、worker或需要writer才能退出的任务 |
| V24 | 启动部分成功后失败、正常关闭、关闭重复取消、某close抛错 | 写入产生者与原结算先退出，诊断再排空，最后关DB；聚合错误不能代替物理退出确认 |
| V25 | init-db、setup在线委托/离线bootstrap、hygiene、reset、schema guard | 离线维护和临时只读生命周期明确；沿已有部署/应用锁避免双owner，setup不嵌套取得同一锁 |
| V26 | SELECT 1成功但writer物理退出未确认或准入停止 | 健康如实区分读取与写入事实；计时不新增逐写同步SQL |
| V27 | 已有receipt/hot identity与首次人物/Conversation/alias建立同时发生 | 热路由纯读可推进；首次创建不重复；写时核原连接、当前归属和不存在谓词 |
| V28 | 入账准备后账号转移、Person合并/forget、路由重绑、新增目标配置/alias/来源引用、重复同平台消息 | 原内部事件/Person/receipt ID不变；按W15实际集合/absence复核，原隐私清退完整；不以route CAS代替候选、不用当前账号重建历史 |
| V29 | 实际Schema/FTS/projection/quota/ref触发器和FK cascade | 与head0147/create_schema/guard一致；修改/删除/清退的派生效果完整，成本计入writer而非只数显式SQL |
| V30 | 管理database_command的savepoint失败与external_command | 真实变更回滚后仍能存原失败receipt；外部操作reserve→执行→settle，不在writer内等网络、不重放 |
| V31 | 名称/元资料更新、SOUL reserve后文件完成但finish提交不明 | 原UUID/文件/已启用事实各自可查；不重复写文件、不把所有阶段合成一个长事务 |
| V32 | ordinary接纳/公共projection/Compact与新输入、generation变化并发 | 共用公共历史、原paid私有尾部和当前head/source CAS保持；不冻结过期源或重建已提交请求 |
| V33 | 工具结果已发生，原源隐藏/擦除/换owner/SELF run变化，再补工具证据 | source/privacy条件在同次writer执行；原call去重，不向可丢诊断降级，也不重调工具 |
| V34 | Artifact/Protocol文件已准备，提交ACK丢失、GC、新引用同时出现 | 保metadata/ref/journal原子性；不误删可能已拥有的文件；unlink在相关writer之外 |
| V35 | 媒体上传/收取/分析/发送与大批过期、取消、unlink失败 | 原件与已引用内容按真实策略保留；文件准备已在外的继续保留；maintenance分页不挡全部聊天 |
| V36 | App/ACP submit/cancel与客户端ACK丢失、重启、cursor续读 | event/mailbox/personal receipt原子；原UUID/scope/payload保留，cancel在writer外join模型/激活 |
| V37 | Work/Automation在队列等待时租约过期、时钟推进、必要收尾继续 | 不用排队前now续租/执行新动作；heartbeat监督保留；已发生效果结算按原允许条件，不一概用旧lease拒存 |
| V38 | 同一paid响应发布遇外部BUSY、源变化、提交不明、重复取消 | 重备纯DB或查原回执；模型不重调，原预算/配对/ID不变，未知不进入整段publication重放 |
| V39 | Wait/输入匹配在准备后新增、消费、被撤销、回复引用变化 | 完整匹配集合与原输入接纳/水位/交付意图正确；跨repohelper沿同writer |
| V40 | Subagent大树，准备后新增后代/request/peer，已有close peers效果 | 重核完整选择和缺席条件；不重放原close，归档/refs/子树清理原子；不因候选数量少假定工作量小 |
| V41 | Social平台确认、平台结果未知、本地完整settle失败及补记 | confirmed正常三方原子；补记可能无ledger如实保留；缺本地记录不推定未发送，不重发 |
| V42 | 主库feedback提交与独立snapshot保存各阶段崩溃/取消 | 原seq/已消费refs/CAS追赶；snapshot实际revision先认领再传播取消；不造跨库事务 |
| V43 | 生产共享DiagnosticWriter与writer=None构造/直接record/expiry | 都经过公共writer；只SQL部分排队；缺样不影响真实效果，missing cache仍NULL |
| V44 | 插件validators/hooks、KV revision、session、notification publication/job/outbox | validators/模型/传输在外；批准/Work/link与event/wait/outbox原子；通知已发后出站ledger+finish_outbox合一；保旧两提交间崩溃、有message_id但uncertain的真实恢复 |
| V45 | Sandbox提交/completion/permission事件，Bot/Manager各阶段崩溃 | 意图先存；completion/input/wait先存后ACK；独立jobs/completion outbox保原request，不重复远程提交 |
| V46 | Memory copy/merge/resolve/forget，准备后新增匹配事实、证据、关系、selection | 按原产品selector语义和真实完整依赖复核；source/result双重授权、原新增ID/receipt和原子mutation保留 |
| V47 | 自省初次baseline、mixed多ordinary/initiative run恢复、失败gap、空白/语音尾部、晚tool receipt、quiet分支 | _advance_state看本批全部staged completion后的结果；准备扫历史不占writer，源/窗口/水位完整重核；原run/paid输出/逐项结果保存，安静等待不持事务 |
| V48 | Dream preview、cluster多action提交、rollback遇后续dependency | 当前ready/stale真实写分开；原输入fingerprint/预算/operation和added IDs保留；不把业务rollback当DB retry |
| V49 | Embedding profile变化/claim过期/结果返回后源变化、大量cascade purge | 原fact/hash/profile/claim CAS和批量upsert保持，Provider结果不重买；只读向量查询并行 |
| V50 | Rebuild proposal commit、receipt sweep、privacy forget、完成判定各竞态 | 三条路径各自有实际源/target/批准/live receipt/absence证明；原selection成员变化不遗漏；付费结果与完成项保留 |

取消验收使用真实 driver 线程或受控 barrier，证明“被取消的 await 已返回、底层操作尚未结束”
时的所有权仍正确。不能只断言 Lock.locked，也不能只 mock 一个立即结束的 commit。
故障注入只在隔离数据库和本地容器完成，不向生产库制造锁冲突。

这些是行为场景，不要求机械新增50个同名测试。逐项对应现有真实测试，保留其正确行为；
只补尚未覆盖的交错与故障。当前审查的测试阅读不等于上述新架构已经运行通过。

不要求人为“所有 SQL 小于 100 ms”或固定 p95 才算业务完成。记录相同负载的前后结果，
检查真正的长尾与排队来源；正常数据规模、未完成任务和部分结果不因此被拒绝。

## 9. 性能设计与前后对照

### 9.1 设计时就控制额外成本

- 并行读继续使用连接池，writer 准入不覆盖模型、网络、文件准备和历史扫描。
- 简单状态更新直接短读改写，避免统一后每个调用都多出一遍准备查询、DTO 编码和校验。
- 复杂操作只准备一次外部或已付结果；写时只核真正依赖，不重复整套准备和无关扫描。完整真实依赖仍须复核，必要时通过索引、批量查询或有真实变更依据的修订降低成本。
- 需要的集合复核走可用索引与合适的批量查询；无法低成本复核时改查询或数据表达，不用假版本省略校验。
- 删除 BUSY 后的重复准备、空周期、无变化写入和重复关联读取，作为共享准入增加成本的主要抵消来源。
- 维护按有实际数据的页提交，避免一个长 writer 堵住全部聊天；事务拆分不能破坏真实业务原子性。
- 诊断仍异步消费，新增的排队计时在内存聚合；不能为了观察 writer 再多造同步数据库写入。
- writer 内 CPU、JSON、SQL 往返、commit 与清理都计成本；不能只把等待搬到别的阶段就声称更快。

privacy forget/人物merge等整次授权变更和FTS rebuild不能都拆成独立小页。准备、解码与来源
整理在writer外，当前集合复核和变更尽量用有索引的集合SQL，减少逐行往返；必须原子的
大变更仍可能占用较久，不能承诺靠FIFO消掉这段时间。性能对照单列大人物族/多引用清退、
配置回滚与索引维护对并发聊天的影响，再按实际SQL改进，不新增聊天门槛或偷偷拆坏原子性。

### 9.2 使用同一负载比较

基线固定本任务书的 main；自省调度变更另设对照，不能把“停掉大量自省”带来的全部收益
记给 writer 重构。使用隔离数据副本或合成真实结构数据，不调用付费模型或真实平台发送。
前后保持相同到达序列、到达速率和操作构成，延迟从输入到达开始计；不能让慢实现少投请求，
或从取得 writer 后才计时，把负载降低和排队时间隐藏成优化。

至少比较以下组合：普通单聊/群聊入账、重复回执、发送结算、媒体入库、诊断持续写入、
自省 checkpoint/证据 mutation、Work 心跳与协议提交，以及这些工作同时发生的场景。
包括接近当前业务库规模的数据和受限 CPU/内存下的场景，不只测空库或无竞争的单条 UPDATE。

每组报告样本数、运行时长、错误与缺样，并比较：

| 指标 | 用来判断什么 |
| --- | --- |
| 入站到可开始 Main、入站到完整回复的 p50/p95/尾部样本 | 用户实际等待是否明显增长；离线模型/传输用相同固定行为 |
| writer 准入等待与事务持有分布 | 冲突是否只是变成了更长队列，长事务来自哪条领域路径 |
| 单位时间完成的业务操作、积压增长与后台进度 | 是否牺牲吞吐或让必要持久化长期得不到处理 |
| 每项业务的 SQL 次数、读取/写入量、纯数据库重备次数 | 新复核是否抵消甚至超过删重试的收益 |
| 事件循环响应、CPU、内存、I/O 与 WAL/checkpoint 行为 | 是否多了同步工作、连接开销或磁盘压力 |
| BUSY/517、已确认业务失败、重复效果 | 性能收益是否建立在跳过正确工作或丢失事实之上 |

不先拍一个允许变慢的百分比。若完整聊天延迟或吞吐出现明显回退，就按配对样本定位
准入、复核、SQL、提交或清理中的成本，修改设计或实现后重新对照。
报告保留改善项和退化项，不能只给 BUSY 减少或一个平均值。

## 10. 计时与完成口径

保留 TimingSummary 和仍有消费者的简单SQL计时。统一后owner已知，不再让旧诊断从多个连接
猜谁拿着writer。`sqlite_diagnostics`中的writers字典、sequence/coroutine/last-operation、
first_write/holders JSON、held_lower_bound/release_unknown，以及仅为观测而patch dialect
do_commit/do_rollback/do_close/do_terminate和engine.raw_connection的包装，随新阶段计时删除。
用于实际正确性的driver收尾不能随着诊断一起丢掉；也不拿诊断探测driver私有字段当释放证明。

公共入口直接记录一次写入的边界：

- `writer_admission_wait`：开始等待公共准入到取得资格。
- `writer_transaction`：开始取得 SQLite 写资格到实际 commit/rollback 退出；包含 SQL 和提交，不能与子项相加。
- `writer_cleanup`：异常/取消后的收尾调用，注明与前述事务计时是否重叠。
- SQL、连接获取、commit/rollback与本次owner阶段分别计时；历史`held_seconds`下界只在日期报告保留，不把旧下界改名为精确锁等待。

只保内部操作类别、阶段、结果和错误码，不保存正文、SQL 参数、Provider token 或平台凭据。
诊断样本可能缺失，未知字段仍为未知；不得用告警样本算全体 SQL 的 p95 或失败率。

| 交付范围 | 可以写什么 |
| --- | --- |
| 基础入口完成，仍有裸写/旧快照调用 | 实施中，列出实际覆盖范围和剩余路径，不称统一完成 |
| 热点迁移完成但全量尚未收口 | 实施中的对照结果，不作为最终架构或独立上线方案 |
| 全量调用迁移及真实并发回归完成 | 当前单进程、该数据库 owner 内的写入已统一 |
| 上线后固定自然流量窗口复核完成 | 报实际冲突、排队和聊天延迟；不能只给 healthz=ok |

本次任务书交付不修改运行代码、数据库或线上配置，不新增永久排队表。
实现可按真实查询与依赖需要调整索引或结构；如确需变化，说明其解决的实际问题，沿现有迁移合同实施。

## 11. 官方依据与实现约束

- [SQLite 事务](https://www.sqlite.org/lang_transaction.html)：WAL 仍然只有一个 writer；IMMEDIATE 在新事务开始时取得写资格。
- [SQLite 快照隔离](https://www.sqlite.org/isolation.html)、[517 定义](https://www.sqlite.org/rescode.html#busy_snapshot)：旧读快照不能靠继续等待升级为当前写事务。
- [busy handler](https://www.sqlite.org/c3ref/busy_handler.html)：配置超时不保证每次调用 handler，不能用毫秒失败推断 timeout 太短。
- [SQLAlchemy flush](https://docs.sqlalchemy.org/en/20/orm/session_basics.html#flushing)：显式 DML 之外也有 autoflush/commit flush，迁移按真实事务效果完成。
- [SQLite driver legacy 事务](https://docs.sqlalchemy.org/en/20/dialects/sqlite.html#legacy-transaction-mode-with-the-sqlite3-driver)：SELECT不自动建立跨语句BEGIN，已有多查询一致读不能随read_session迁移丢掉。
- [aiosqlite 0.22.1 worker](https://github.com/omnilib/aiosqlite/blob/9b127cecc7de843bf4f343b6da63b641866533fb/aiosqlite/core.py#L48-L66)、[排队与关闭](https://github.com/omnilib/aiosqlite/blob/9b127cecc7de843bf4f343b6da63b641866533fb/aiosqlite/core.py#L150-L214)：取消等待不代表函数从线程队列消失。
- [SQLAlchemy 2.0.51 aiosqlite 关闭](https://github.com/sqlalchemy/sqlalchemy/blob/c8e26d5c9ffcde0a82b4b7fa5de27f0c6bc46bec/lib/sqlalchemy/dialects/sqlite/aiosqlite.py#L345-L361)、[terminate 分支](https://github.com/sqlalchemy/sqlalchemy/blob/c8e26d5c9ffcde0a82b4b7fa5de27f0c6bc46bec/lib/sqlalchemy/connectors/asyncio.py#L397-L421)：清理确认须按锁定版本验证，不能凭一个关闭调用推断物理完成。

迁移、索引重建与离线写工具仍按已有维护边界运行；运行期只读调查使用短只读事务。
不把持续变化的生产库以 immutable 方式打开来跳过锁或变化检测。
