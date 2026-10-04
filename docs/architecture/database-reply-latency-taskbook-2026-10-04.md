# 数据库回复延迟：后台对账、投影与 Work 存储修复任务书

日期：2026-10-04，Asia/Taipei（UTC+08）。状态：设计复核完成，已获用户授权开始修复；实现和验证进行中。

用户在本任务书完成后授权“开始修复，老规矩”，允许使用 subagent。实现从最新 origin/main
`678adab8e72fffd3f39ca50fa3fde5fadbdd0ca1` 的独立 worktree 开始。具体提交、合并和
上线范围按本次用户后续确认记录；本文不是修复完成或生产验收证明。

## 1. 基线、目标和现行合同

本次生产核验到 2026-10-04 14:46，Bot 镜像 revision 为
`8d7fa982e1af68e935cb5db4f2f1a77d8e8727ef`，schema 为 `0091`；启动时间
`2026-10-04 02:44:12 +08`，重启次数为 0。第二次核验镜像及启动时间未变化。
当前文档工作区为 detached `af022cc9fb9b556e048712a35eb3f38c19eef466`，比生产旧。
下文源码定位均属于生产 revision，不能把工作区同名文件的行号当成线上代码。
实施前重新读取目标分支、生产镜像、迁移头和已有改动，不在这个旧基线上直接修运行代码。

现行依据：

- [共同开发约束](development-contract.md)，尤其 §7 的短事务、来源复核、维护分页和真实诊断，以及 §9 的最小充分设计。
- [主 Agent 执行与恢复](main-agent-runtime.md)：原 Work、预算、租约、journal、效果回执和显式发送。
- [语义参与](semantic-participation.md)：先持久 Host 反馈，再回放原 scope/generation 控制器；终态迟到效果不复活任务。
- [Conversation Rollup](conversation-rollup.md)：实际请求和冻结历史的完整性、来源保护及明确的压缩边界。
- [执行轨迹](execution-trace.md)和[Work 证据备份](../operations/work-evidence-backup.md)：可丢诊断与不可丢恢复事实分离，文件按真实引用回收。

上述文档在本工作区可能比生产旧；复查时同时读取了生产 revision 的对应版本。历史
`foreground-rollup-and-sqlite-contention-taskbook-2026-10-03.md`
在生产 revision 存在，本工作区尚无该文件；其已实现的修复不重复列为本轮待办。

目标是减少无变化后台工作和重复持久化，使前台回复获得数据库与事件循环资源，同时
修复终态迟到回执的轮询遗漏。保留原事实、身份、权限、实际 Provider 输入和恢复能力。
本轮不增加新的恢复状态机、持久扫描水位、授权缓存或全局数据库调度层。

## 2. 已核验证据与结论边界

### 2.1 线上观察

| 证据 | 本次观察 | 可以说明什么 |
| --- | --- | --- |
| 本次启动后的日志 | 130 条 `sqlite_slow_write`，20 条 `sqlite_write_contended` | 存在真实慢写及 BUSY 竞争；不是 20 个唯一事故或 130 个唯一事务 |
| transaction 11927 | 13:27:45 提交；持有观测下限 39.100 秒；其中 `UPDATE prompt_projections` 3.133 秒 | 同一事务长时间占 writer；不能将全部 39 秒归给这条 UPDATE |
| transaction 11479 | 持有下限 11.433 秒；`UPDATE canonical_conversations` 7.452 秒 | 入站热路径也受拖延；并不证明它触发了隐私清理 |
| 全过程最长持有观测下限 | 40.172 秒 | 延迟不是只出现一次 |
| 诊断累计 | uptime 42,303 秒，rollback 13,509,110，commit 15,330 | 每秒约 319 次 rollback 调用；包含读 session 清理和 pool reset，不是失败次数 |
| initiative | completed 335、no_reply 164、interrupted 7；accepted/running 均 0 | 没有活动 initiative 时仍存在持续终态对账负担 |
| 终态尾页 | 128 条，更新时间 09-30 08:20 至 10-04 13:07；第二次核验均为当前 generation | 几天前的终态仍每 tick 重新读；本样本没有旧代次积压 |
| Social 表 | 8,859 行；实际完整行查询计划为 `SCAN social_operation_receipts` 与临时排序 | 按 scope/时间的常规轮询缺少匹配索引 |
| 控制器快照 | 最新 4 个 scope，candidate 总数 0，boundary 去重来源合计 90 | 排除“数百候选逐条查询”作为本次观测解释；boundary 核验仍有成本 |
| 投影/协议规模 | 最大 projection 232,381 bytes；selection 5,844 行；协议对象 6,002 个、129,217,732 bytes；refs 25,447 条 | 不能以单个 8 MiB 投影解释事故；协议和选择记录有重复处理成本 |
| 主机资源复查 | RAM 1,612 MiB，swap 使用约 1,622 MiB；采样换入 3,700/1,808 KiB/s；I/O full PSI avg300 32.12% | 换页和存储竞争仍持续；不是仅凭 swap 已分配量推断压力 |
| 消息分条政策 | 当前全局热配置 1–2 秒/间隔，未发现这两个 key 的群/人物覆盖 | 三条分条的政策等待约 2–4 秒；不将它等同数据库耗时，也不据此推断过去配置 |

示例普通轮总耗时 136.89 秒，对应已关联模型调用合计约 16.86 秒，差值约 120.03 秒。
差值还包含工具、准备、调度、取消/发送等阶段，不能全部记作数据库等待。

SQLAlchemy 2.0.51、aiosqlite 0.22.1 与线上版本一致的隔离 SQLite 核验中，4 个稳态
只读 session 产生 4 次查询和 8 次 dialect rollback；首次连接另有初始化调用。
这支持读 session 清理的量级解释，但不能把全进程计数精确归因给单个服务。

首次检查的结构化摘要及日志保存在本地忽略目录
`.cache/db-latency-audit-20261004/`，其中 `deployed-source/` 是明确版本的源码副本。
这些缓存不作为仓库交付依赖；本任务书已保留主要事实和核验方法，公开材料不包含消息正文或凭据。

### 2.2 分类与优先级

| 编号 | 判断 | 优先级及范围 |
| --- | --- | --- |
| D01 | 终态 128 条重复逐 run 对账：每 run 至少 4 个读 session，最低 512 sessions/tick；tick 后 sleep 2 秒 | 首批修复。静态路径和线上满页已确认，负载量级吻合；贡献百分比未测得 |
| D02 | 最新 128 条之外的终态迟到回执可能永久不被发现 | 与 D01 同修，属于正确性缺口；不能只加 TTL 或降低频率 |
| D03 | Social scope 轮询全表扫描/排序，普通 admission、event、basis 逐项重复查证 | 首批修复；索引计划已在线确认，近期普通反馈增加了读取工作 |
| D04 | Protocol GC 全表扫描/排序、全局锁内逐对象 writer；文件准备重复处理全 transcript | 第二批修复；查询计划和锁生命周期已确认，独占事故贡献未测得 |
| D05 | projection 重写完整 payload，并把全部历史 selection 再 INSERT/唯一检查 | 第二批差集优化；JSON 准备已在 writer 外，不把已修内容重新写成缺陷 |
| D06 | 来源 observation 反向 JSON 依赖清理可能扩大删除事务；历史 metadata/正文读取增长 | 定向测量及风险登记；没有证据将其归因于本次普通入站慢 UPDATE，不扩大全库图模型 |
| D07 | 主机换页、磁盘/事件循环压力及等待分解不足 | 全程采样及交付限制；不自动扩容、迁库、重启其他服务或改消息节奏 |

D01/D02 首引入 `afb7123e`（2026-09-22），前次修复基线 `b1382cf8` 已有。
近期 `2e6425f8`（2026-10-03）增加普通参与反馈的逐项核验。
本轮未发现 F08 evidence snapshot 修复被直接撤销；生产 activation 修补已先只读分页，
Dream restart 聚合也已前移。结论是遗留读放大叠加新成本与主机压力，不是已经证明某次 Work 重构单独造成所有延迟。

## 3. D01/D02：按原事实批量对账，并公平覆盖终态

定位：`services/semantic_participation.py:1316,1398-1407`；
`conversation/autonomy_repository.py:453-465`；`services/participation_feedback.py:270-388`。

现有 `list_recent()` 按可变 `updated_at DESC LIMIT 128` 固定取尾页，无游标。
Social/MemoryTool 的新回执不推进 initiative 更新时间；只有进入对账后才会写 feedback。
隔离真实 SQLite 的 129 个终态 fixture 中，向第 129 条插入迟到 Social 回执不改变尾页；
主键 keyset 轮转能覆盖全部对象。这是查询遗漏的复现，不是生产误发送或延迟验收。

最小实现：

1. 活跃 run 仍沿原 run/Work ID 高频恢复与对账，不等待终态维护扫描结束。
2. 终态用稳定 run 主键 keyset 轮转：有界页、固定一轮扫描高水位、进程内游标和维护节奏。
   UUID 主键按稳定数据库排序使用；本轮在已扫位置新插入或新转终态的对象，下轮回访。
   不按 `updated_at` 游标；不增加终态 TTL、当前 owner/generation 过滤或持久“已扫完”水位。
3. 先把现有多次逐 run 读取收敛为页批读，再引入较低终态扫描频率，分别记录两项效果。
   页大小、间隔、单轮页/时间预算及最大无故障巡回发现延迟必须给出可复现实测依据。
4. 一页在短生命周期显式读快照中批量读取 run、原 `initiative:<run_id>` Work、效果、
   工具回执、child 计量与**全部已有 feedback 页**的必要列。参数集合分块，避免 SQLite 变量上限。
   数据量很大的单 run 可沿真实 effect/feedback ID 分页；不能把分页首段当完整计量。
5. 完整准备并编码新增反馈后，保留原 `feedback_sequence`、终态、唯一键和计量口径，
   只对有变更的 run 使用短写入。保留每个反馈页内 run sequence/state、该页 feedback 和
   原 source claims 的原子提交；现行 64 effects/页本来逐页提交，不扩大成整 run/全部 run 长事务。
   无变化页零 DML、零 IMMEDIATE；部分页已提交不等于该 run 已完整对账。
6. 每个新增反馈页的读取/编码在同连接显式快照准备，首次 DML 前核对原 run/sequence/终态
   及真实 receipt 关联。继续同快照升级，或证明完整依赖复核；不能关 reader 后只查 sequence
   就声称冻结了所有依据。冲突回滚失败页并有限重备纯数据库计划，已提交前页按原事实保留。
   原 run/effect/call ID 不变，模型、发送和工具不重跑。确认丢失先查持久事实，不盲目重复追加。
7. Host feedback 提交后才回放控制器及保存独立 snapshot；不在 writer 中等待控制器锁、
   文件或 participation SQLite。旧 generation 的真实回执仍按原 run 入账，不创建旧 proposal、
   不把它交给新 generation，不重新 dispatch 终态 Work。
8. 同一原 scope/generation 的本页已提交反馈可聚合回放、批量准备一次 controller 保存，
   保留 pin、sequence、save lock 和取消收尾。冷加载仍须补放已有 Host 反馈，不能只看本轮新增。
   复用现有 payload 无变化不保存逻辑，避免每 run 重复编码同一快照。
9. 游标只是调度位置。失败页保留可重试范围，同时有界退避/轮转其他页，不能永久饿死后页。
   进程退出后从头扫描，原 feedback/effect 幂等事实防止重复累计。

复用原 `record_feedback` 原子语义、Controller restore/sequence 去重和现有测试，删除被替代的
逐 run 四 session 路径；不再包一套并行 reconciliation 框架。
若只读快照重备或 controller 补放需要额外完整 journal，按真实需要读取；无 controller 且不需要
恢复自报时不加载完整 journal，但不能因此跳过 Host 迟到效果和计量入账。

## 4. D03：Social 查询索引与本轮批量来源核验

定位：`services/participation_feedback.py:53-68,135-151,211-230`；
`services/semantic_participation.py:713-790,1325-1366`；
`conversation/ordinary_admission.py:138-198`。

迁移添加匹配实际 `source_conversation_id = ? AND updated_at >= ? ORDER BY updated_at DESC`
的索引。若读取改为稳定 `(updated_at, id)` 分页，索引应同时支持该真实排序；不要为假想查询
增加多套索引。迁移编号从实施时最新头分配，冻结旧迁移，验证已有对象形状及 downgrade 所有权。

批量核验每个不跨外部等待的核验阶段/scope 实际需要的 admission、event、memory、basis 与 boundary refs，
按内部 ID 去重；复用 `current_admissions` 的完整 JOIN/owner/Presence/generation/source revision
条件。Memory 按原 `_seed_query`、review/lifecycle/visibility 与 evidence lineage 条件分块读取，
不能简化成 `fact.updated_at` 或主键存在，也不能扩大 SELF/Person 可读范围。

`SourceRef.revision` 是 controller 版本号，Host `source_revision` 是真实来源指纹，不能互换。
继续按 `source_versions` 的 `[原 ref.revision, 当前真实 digest]` 双重核验；缺失/失效来源
走原 `observe_source_change`，不能把批读缺项默认为可用。

一个显式读快照只准备当前派生观察；不跨 tick 缓存授权。发生 Jev/模型/网络 await 或来源
变化后，后续消费来源重新读取；写与派发边界仍用原 generation、revision、owner、privacy、
租约及权限核验。普通 admission、已发送锚点和 SELF 不能混用。

当前近 600 秒/2048 行属于既有产品窗口和物理上限。批量优化必须保持分条、文件附言、
原 turn/call、逻辑发送归并及 target 事实，不通过删窗口内回执让统计变快。
若未来改为增量游标，须另证 late update、时间戳相同和重启完整性，不能仅凭当前无新消息跳读。

## 5. D04：有界 Protocol GC 与准备复用

定位：`runtime/protocol_store.py:65-75,109-128,156-216,250-307`；
`runtime/work_journal.py:356-357`；`runtime/work_scheduler.py:164-170`。

### 5.1 查询访问量与 GC 临界区

现有 `unowned AND (deleting OR expired) ORDER BY prepared_at LIMIT 128` 线上计划为
全对象扫描、相关 refs 查询和临时排序；LIMIT 只限返回，不能限制 owned 对象的扫描量。

采用两个真实状态分支：`deleting = 1` 和 `deleting = 0 AND prepared_at < cutoff`。
先按索引取有界 metadata 页，再批量读取该页 refs 并筛 unowned。必要时用
`(deleting, prepared_at, sha256)` 支持稳定 keyset；隔离 SQLite 已确认现有双列索引对
完整 keyset 排序仍有末项临时排序，三列候选可消除它。最终迁移以完整实际 SQL 的计划为准。

维护使用进程内游标、固定 cutoff/扫描高水位、行数及时间预算；每个状态分支分别冻结
同排序的 `(prepared_at, sha256)` 词典序高水位，按 `cursor < key <= high_water` 读取，
不能用两个独立 MAX 拼水位。页内全 owned 也推进；状态变化或同 timestamp 后插对象次轮回访。
重启重扫，deleting 恢复和普通过期扫描各有预算，避免互相饥饿。

GC 分三段，复用现有 `deleting` 和真实 refs，不新加业务状态：

1. 锁外读候选；短 writer 按原 digest、当前期限/状态和 unowned CAS 标记 deleting，提交释放 writer。
2. 仅实际文件核验/unlink 阶段持协议锁，不持 SQLite writer 等协议锁；孤儿文件同样先短写登记屏障，
   不按旧目录扫描结果直接删除后来已发布的文件。
3. 实际 unlink 成功后，锁外短事务批量删除仍 deleting 且 unowned 的 metadata；失败对象保留原屏障供恢复。

publication 先提交时，refs 使 GC CAS 失败；GC 先标记时，现有 `publish_refs` 在 writer 中的
`deleting=False` 全集合检查拒绝新引用。文件检查至 journal/ref commit 的 publication 保护仍保留。
metadata 缺失不等于文件缺失；只有真实文件在保护内验证存在后，才能合法恢复 metadata。

不能只把 publication 改成“先 writer 再协议锁”，也不能检查文件后裸释放锁再等待 writer。
前者与旧路径可能形成锁环，后者允许 GC 删除后重插缺文件引用。本轮先缩小 GC 临界区；
publication 等 writer 时的跨会话阻塞仍是剩余限制。若复测证明它仍主导延迟，再单独设计
非等待 writer 领取/锁外有界重试，验证连接 timeout 恢复和取消，不顺手建立新锁调度器。

### 5.2 checkpoint 重复准备与取消

缓存仅复用原 Work/chain 内已证明不可变普通条目的 digest、byte_size 和已核文件身份，
优先避免复制整段编码增加 swap 压力；额外缓存有明确条目/字节政策上限，不能仅以 transcript
有界代替额外内存预算。缺失或变化时从原记录重编码；恢复、压缩和新 chain 时重建。
不按 Python object ID 缓存含可变字典的 opaque continuation/
`response_item`；继续保留协议字段及顺序。优先批量文件检查，避免逐条跨线程调度。

在同一 publication 协议锁保护内、首次 SQLite writer 前在线程中批量核验真实文件身份；
缺失重新准备，身份改变重新核 digest，损坏明确拒绝。
缓存或 `is_file()` 不能成为绕过必要完整性查证的永久事实。新增这类查证的 I/O 成本单独测量，
不得为消除编码反而每次全量读取所有 opaque 文件。

`to_thread(unlink/_publish)` 在调用方取消后仍可能运行。取消时必须等待原线程真正收尾后再
释放原保护，不重新开一次删除或发布。任务书登记“取消 GC、第二轮 GC 收尾、新 publication、
旧线程迟到”的竞争风险；普通单次取消后的 publication 目前受 deleting 屏障保护，
没有证据宣称线上已经误删。实现前用 thread Event/barrier 固定交错验证。

## 6. D05：Selection 差集与无效正文读取

定位：`conversation/projections.py:293-318,425-536`；
`conversation/observations.py:193-274,408-418`；`services/main_agent_turns.py:439-486`。

在明确读快照中准备本次 `(view_key, source_key)` 缺失集合及原顺序，writer 只插新项。
利用现有已核验 frozen prefix 和必要来源 metadata；不能为减少 INSERT 又无条件加载全部旧 payload。
需要精确内容核对的候选才读取正文，并将这种准备成本计入前后测量。

writer 保留 canonical generation/starts_after/source revision/privacy、projection epoch/revision CAS，
Work 原租约、取消和来源围栏。准备阶段缺失不等于授权；来源/parent edit、删除、改绑、reset、
privacy 变化或合法同 view publication 竞争必须拒绝旧计划，避免 stale selection 获得新来源。

同 epoch 普通 append 保持旧 fragment 完全冻结。同 source key 的不一致 owner/scope/generation/事件身份或
意外呈现冲突不能靠 `ON CONFLICT DO NOTHING` 隐藏。显式 capacity/contract 边界可合法改变
呈现；先固定测试当前 first-selection 保留与 coverage 口径，不把所有 payload 差异一律变成失败，
也不顺便修改 selection 身份为多版本模型。
显式边界的合法变化可能同时涉及旧 snapshot pointer 与 raw chat 的观察关联，不仅是正文文本；
按 first-selection 和完整 coverage 口径分类核验，不能将所有观察关联差异一律认作身份冲突。

摘要 parent refs 只处理本次首次 selected 的摘要：锁外解析、去重 parent IDs，writer 内按
owner 索引批读仍有 refs 的集合，空集零 DELETE，非空批量释放。不能凭锁外空 refs 放过
后来新增引用。projection、selection、parent refs 和 dispatched journal 保持原同事务发布；
任何后续 journal 失败都一起回滚，不单独提前提交 selection。
parent refs 释放前核验每个真实 handle 已由对应选中 summary 保留，不能只确认“某个摘要有引用”。
source/privacy revision 不覆盖任意 tool artifact refs 的新增；准备后新挂且未转移的 handle
须拒绝/重备纯数据库转移计划，不能随批量 DELETE 丢失其唯一拥有者，不增加跨主体引用权限。

减少无效正文读取时，先按 actor/read-scope/generation 和引用 metadata 分页判断 coverage、
有效 roots，再批量取实际需要的 payload，保持 selection ID 顺序及完整来源闭包。
尚未 selected 的新 work-note 仍可合法进入下一请求，不能只从旧 projection roots 取正文。
未 selected snapshot/paid summary 不能伪装 observed coverage；root、parent 闭包和合法新项分开核验。
分页只控制单次资源，当前 JSON parent 图的 metadata 总访问量仍可能增长；本文不宣称已经
彻底限制全代次读取，也不加隐式 LIMIT/TTL 丢历史。反向依赖规范化或来源 GC 新协议另立设计。

## 7. 测量、复用与范围限制

复用现有 SQLiteDiagnostics、DiagnosticWriter 和 execution trace，以固定操作类别、固定桶、
有界采样记录对账页数/读 session/SQL 次数、DB 时间、protocol 锁等待/持有、准备线程时间及缺样。
不把 run/effect ID 作为无限增长的 metrics label；不保存正文、SQL 参数、文件内容或凭据。
只读 rollback 不改 commit，指标不承担计费、恢复、权限或效果事实。

分别测量入站至首个模型派发、Provider、工具/协议准备、writer 获取、SQL、真实 commit/rollback、
首条实际发送及整轮结束。第一条发送和整轮结束可能不同；合法分条等待单列。
driver queue/pool wait/SQLite wait 无法区分时保留 unknown，不相减伪造精确锁等待。
健康查询自己的负担和超时单列，避免用高频重型健康接口制造观测负载。

只读线上复测使用 `mode=ro`、`query_only`、短查询期限和必要索引 metadata；在线 WAL 不用
`immutable=1`。完整性/FTS/正文规模扫描及压力 fixture 在一致离线副本或隔离库中进行。
不能启动第二个主动 Bot 写同一库，也不发送合成 QQ 探测消息。

不在本轮实施：迁移 PostgreSQL、增加永久 writer 队列、统一长事务重试、扩大 busy timeout、
更改模型/Provider/消息节奏、关闭 Work/Jev 功能、重设预算、删除聊天/终态事实、重建来源 ID、
重构整个 provenance 图或恢复旧数据库。主机扩容属于另行运维决策；代码修复不保证消除所有 I/O 压力。

## 8. 验收矩阵

使用真实隔离 SQLite/WAL、Repository 和受控 Provider/transport，事件/barrier 定位竞争；
不要以随机 sleep 或硬编码 CI 墙钟阈值作为主要正确性断言。

| 验收 | 必须证明 | 优先复用 |
| --- | --- | --- |
| V01 无变化对账 | 128 个终态页的 sessions/SQL 按页批量增长，零 DML；页间前台入账能推进 | `test_participation_feedback.py`，SQLite 诊断测试 |
| V02 公平及迟到 | 129+ run 中尾页之外的 Social/MemoryTool/child 效果最终发现；新插入、故障页、重启不遗漏；旧 generation 仍原 ID 入账 | feedback/snapshot 测试 |
| V03 原计量/终态 | 64+ effects多页反馈；第2页失败保留已提交第1页，重启继续不重复；root/child不双加，零Provider/发送/dispatch，终态不复活 | 既有120 compute与暂停Work迟到回执用例 |
| V04 准备/提交竞态 | 真第二连接追加回执或争 feedback sequence；517/CAS 后仅重备 DB，原 ID/预算不变；确认丢失核事实 | feedback、Work source guard 测试 |
| V05 反馈/快照崩溃 | Host 已提交而 controller 未保存；恢复原 sequence/effect，不重发、不制造旧 proposal | `test_participation_snapshot.py` |
| V06 普通来源批读 | hide/edit/rebind/reset、Memory quarantine/evidence hide 后拒绝旧来源；Jev await 后重验；不缓存授权 | ordinary feedback、ordinary admission/source 测试 |
| V07 实际索引 | 完整 Social 查询使用匹配 SEARCH，无全表排序；GC 两状态/keyset计划、相同 timestamp/多数 owned 页有界且最终回访 | 索引迁移/真实 EXPLAIN 测试 |
| V08 GC/publication 竞争 | publication先提交及GC先mark两胜负；最后owner移除、孤儿注册、隐私/new refs、mark后重启；收尾commit确认丢失查原事实；unlink阶段与引用围栏正确 | `test_work_protocol_gc.py`、协议连续性测试 |
| V09 线程取消 | 暂停原unlink/publish线程后取消；第二GC与新publication不能受旧线程迟到破坏；收尾后才释放保护 | 协议GC/存储延迟测试 |
| V10 checkpoint复用 | 旧普通条目不逐次重备；新增真落盘；缺失/替换/损坏文件不能用cache通过；opaque字段顺序与恢复输入不变 | `test_work_protocol_continuity.py`、prepared protocol 测试 |
| V11 selection差集 | N旧+1新只插1项；同view并发CAS、source/parent/reset/privacy变化拒绝；合法epoch呈现边界仍成功 | projection/observation writer fastpath 测试 |
| V12 原子refs转移 | 重复摘要零重复DELETE；重叠parents批量释放；新增parent handle未被对应summary持有时不删除；journal失败同回滚 | `test_work_boundary_dispatched_save.py`、observation source 测试 |
| V13 实际请求与读取 | 有效正文和合法新note载入，metadata分页完整；断言payload列/参数bytes/行数减少，不要求metadata总SQL常数；旧来源不丢不重，实际wire及opaque顺序不变 | Gemini wire、history projection、Work恢复测试 |
| V14 诊断与负载 | 正常读清理≠失败；慢DBAPI尾部不提前清holder；unknown/缺样保留；同时记录PSI/swap/负载及首派发/首送达 | SQLite phase/physical exit 及诊断队列测试 |

V13 优先用既有 fake Gemini 和另一个已有实现的协议做确定性 wire 对比；真实付费 Provider
请求不自动进入 CI。合成通过不等于真实群聊验收，也不要求靠额外真实请求保温缓存。

## 9. 实施顺序、文件所有权与交付

1. 在重新确认的最新基线上锁定 D01/D02 回归，完成页批读及公平轮转，保留活跃恢复优先。
2. D03 索引及本轮来源批读联动，核验当前普通接话、SELF、历史恢复入口；先做高频负载的最小修复。
3. 复测数据库调用量及前台阶段等待；再实施 D04 的 GC 查询/临界区和必要 checkpoint 复用。
4. D05 selection差集与parent refs优化，先锁定epoch/来源/原子边界，再改读取；D06只登记测量结论。
5. 汇总 V01–V14、相关格式/类型/集成检查，更新现行模块合同；稳定代码不反复跑无关全量测试。

建议分工：对账/ordinary来源、Protocol存储、projection各自独立文件所有权；主会话持迁移
编号、装配接口、任务书和集成验证。多人不得并行编辑同一文件；独立审查验证原事实与原子性。

实施交付记录逐项说明本地修改、定向测试、提交、推送、CI、合并、部署和自然聊天效果。
本任务书不替代用户对交付范围的授权。未来有部署授权时，按生产手册重新读取真实 Compose、保留
可回退镜像并做一致性备份，只按批准范围替换 Bot；保留新消息/预算/回执，不默认恢复旧数据库。

完成条件：

- D01–D05 每项有已删除的重复路径、真实 SQLite 回归及查询/调用量证据；剩余限制有明确记录。
- 没有新增永久扫描水位、授权缓存或第二套恢复/发送链；原 ID、预算、未知效果、隐私和强制审计均保留。
- 实际输入、projection/journal原子发布及文件引用验收通过，迁移/降级只修改本轮所有对象。
- 同 fixture、同模型/profile/请求容量和相近负载对照，报告 p50/p95/max、样本数和缺样，
  不把 Provider 或正常分条节奏误记为 SQLite 时间。主机压力差异不可比时明确标注。
- 上线后若没有足够自然聊天样本，只报告技术验证/观测结果，不宣布“回复慢已全部解决”。

## 10. 复核用只读查询

连接要求见 §7；`?` 由受信当前 scope/时间注入，不打印参数或正文。

```sql
EXPLAIN QUERY PLAN
SELECT * FROM social_operation_receipts
WHERE source_conversation_id = ? AND updated_at >= ?
ORDER BY updated_at DESC LIMIT 2048;

SELECT state, count(*) FROM autonomy_initiative_runs GROUP BY state;

SELECT count(*), min(updated_at), max(updated_at)
FROM (
    SELECT updated_at FROM autonomy_initiative_runs
    WHERE state NOT IN ('accepted', 'running')
    ORDER BY updated_at DESC LIMIT 128
);

PRAGMA index_list(social_operation_receipts);
PRAGMA index_list(runtime_protocol_objects);
```

实施后对完整实际 ORM SQL重新 EXPLAIN。不以仅查询主键的 covering 计划替代完整热路径，
也不通过扩大 LIMIT、重复全库扫描或在线压测来获得更漂亮的诊断。
