# 参与循环的精确等价、恢复与存储布局反证（2026-10-26）

状态：研究与本机合成实验，未修改业务代码、依赖或生产。Host 基线 `75e21a5c824876485d75e7fcd58e8111bdddeb4d`；独立库固定 `79d266918dc6ce19b7d81568565deaed450301d5`。独立库源码从该 Git 对象只读导出到本地报告目录，未使用另一个分支的工作区内容。

本轮要求保留旧 `sample_sequence` 哈希、概率、浮点更新顺序与冷恢复行为。因此不采用 10/25 研究中的时间随机键、跳过无新贡献重锚或改变跨 tau 冷恢复数值路径。该旧建议只能作为历史研究，不能继续充当本方案的隐含前提。

遵循[开发合同](../development-contract.md)、[参与接入](../semantic-participation.md)和[连续概率模型](../autonomous-participation-model.md)。本附录负责独立反证与 WAL 布局实验；完整 State 字段变化矩阵另由并行审查核对，不能把以下示例误称为已穷尽全部写入入口。

## 1. 可行性与最大风险

可以精确减少重复分组、排序、UTF-8 处理、Pydantic/JSON 往返，以及每 tick 重新绑定和写入冷正文。不能同时承诺严格保旧恢复和零空闲持久化。

最大风险有三个：

1. 把 `replay_after`、baseline 的值和时间当作无用时钟缓存，跳过旧程序的逐 tick 运算。
2. 只保存随机序号而不保存与之配套的实际数值状态，恢复时拼成从未存在的 State。
3. 同行增加 `hot` 列后，仅凭 SQL 没绑定冷 BLOB 就声称物理 WAL 只写小 header。SQLite record 长度变化可以重写冷 BLOB 的 overflow 页。

目标应是保留原计算、原保存点与原 CAS，让真实热态的编码和物理存储不再拖上整份冷材料。

## 2. 哪些旧操作必须保持

### 2.1 采样序号由真实分支决定

`controller.py:2437-2498` 先处理 epoch，再按当前 Host/pending/capacity 判 `can_propose`，更新 now 后调用 `_prune()`。无资格、无 scores、明确邀请等分支各有自己的 `last_sample_at` 更新。

有非空随机 scores 时，旧代码先计算 total/dt，再递增 `sample_sequence`，最后判 arrival。即使 `dt=0` 或 total 为零，序号也已增加。不能套用“没有时间前进就不抽”的新规则，也不能从 tick 数、墙钟或数据库 revision 反推序号。明确邀请、无 scores 和 epoch 切换又不按这个路径递增。

`_sample():2429-2435` 的哈希材料、两条 stream、八字节提取和浮点转换必须原样保留。先遍历 `scores.values()` 求 total，之后按原排序扣 cursor；不能为合并排序顺便改变求和顺序。proposal/run 的真实身份和接纳回执继续沿原 owner。

### 2.2 实数等式不能替代旧浮点步骤

`_advance_replay_boundary():1829-1908` 不是只有新贡献时才写值。只要 boundary 前进，它会把已存在的 belief 和五种 trace 按旧调用顺序重新计算并重锚，更新 `replay_after`。这在浮点上不是一次大步衰减的逐位等价形式。

固定提交的真实 Controller 本机反例：初始 activity 为 `0.3@400`、replay_after=400，now=1000；Host 不可用，每两秒 advance 至 1020，没有新消息：

| 路径 | 最终 activity | `float.hex()` |
|---|---:|---|
| 1002、1004、…、1020，十次旧 advance | 0.21495939317213694 | `0x1.b83ca15d237ffp-3` |
| 直接一次 advance 到 1020 | 0.21495939317213697 | `0x1.b83ca15d23800p-3` |

差一 ULP 已足以否定严格浮点等价，不需要宣称这次一定翻转了真实群聊决策。重复的排序或来源准备可以减少；这条旧数值更新链本轮不能跳过、融合或改结合顺序。

### 2.3 旧 tau 冷恢复保存的是已算出的数值

`restore():2795-2828` 保留原 State 数值与序号，将 now/last_sample_at 设为恢复时刻，再用恢复时配置运行原 prune，不补停机机会。参数本来不写快照；这不等于恢复时可以丢掉旧配置运行期间已经折入 baseline 的数值。

上述合成轨迹先用旧 activity tau=60 运行，恢复到 1100 时换 tau=30：

- 保留旧逐 tick 数值再 restore：activity `0.014936120510359197`。
- 空闲期间不落热态，只用较旧 cold snapshot restore：`0.010702198004175741`。

要精确保留旧行为，只需保存原程序已经算出的 baseline/value/at 与边界；不需要新增参数历史、猜测配置修改时间或新业务 epoch。不能改为“按新 tau 解释更老锚”后继续声称等价。

## 3. 零空闲持久化与旧游标恢复不能普遍兼得

取一个非空、极低 rate 的合法随机分支，确保所有 arrival 都未产生 proposal。没有新事件或外部效果，旧 Controller 仍在五次 advance 后把 sequence 从 0 推到 5。

本机使用真实 advance，只把 rate 函数替换为固定 `1e-100` 以隔离游标：

| 恢复事实 | 保存序号 | 下一次 arrival 随机数 |
|---|---:|---:|
| 保存旧程序已推进的游标 | 5 | 0.4518963454237354（sequence=6） |
| 不保存热态，恢复较旧 cold | 0 | 0.854291853456234（sequence=1） |

一般反证：两段没有业务输入、但走过不同数量采样分支的历史，在“零空闲持久化”下可以留下同一份磁盘信息。它们又可以在同一时刻、同一配置下崩溃恢复。恢复算法得到的输入完全相同，却被要求分别恢复两个不同旧序号，不可能同时正确。若记录 tick/序号日志，那已经不是零空闲持久化，而且只是给同一事实换了容器。

只有证明某次转换没有改变任何未来需要恢复的事实时才能免写；“无新聊天”“没有发言”“rate 很小”“结果为 None”都不是这个证明。

反例原件：本地 `D:/Code/Reports/Yuki/algorithm-20261026/participation_equivalence_counterexamples.json`。全部使用合成 State、固定库纯函数，没有生产操作或付费调用。

## 4. 可以精确删除的工作及条件

| 操作 | 可减成本 | 必须保留/证明的条件 |
|---|---|---|
| feedback 内反复 any(effects)，`:2748-2752` | 同次 prune 预先形成 recent run 集，`O(FE)→O(F+E)` | 保 effect `>=cutoff`、feedback `<cutoff`，在旧删除 effects 的位置之前准备；无浮点运算变化。 |
| advance 与 opportunity_scores 重复 eligible groups，`:2462/2058` | 无 await 的同一次 advance 共享已求出的组 | 同一 State、now、过滤参数和默认身份语义；保持字典次序，不能跨来源变动复用。 |
| opportunity 与 intrinsic 重复 pressure | 同次、同一 State/now 复用已得 float | 是复用完全相同的结果，不是变换公式或求和顺序；参数或 State 已变不能命中。 |
| activity/social_context 重复排序 human 时间，`:2146-2166` | 同一数值准备阶段只构造排序列表一次 | 后续每种 trace 仍各按旧 tau、increment 和原顺序算，不把两个数值过程融合。 |
| belief action 列表反复 sorted，`:1810` | 同一组 actions 的顺序只准备一次 | 每次调用仍从原 baseline 出发，执行旧 decay/observe 顺序；prune 删源和重锚之后不能直接当成同一份输入。 |
| baseline eviction sort，`:1887` | 输入的 key/last_action 排序键未变时，复用已有 canonical 顺序；或按相同 key 选原 top64 | 不跳过每项数值重锚；新贡献跨界、单位变化、修订撤回及参数调用后重核，保 eviction 计数和最终字典顺序。 |
| raw events 排序、UTF-8 计量，`:2700-2714` | 同一次 prune 中计量长度一次供 sum/淘汰共用；来源集合不变时可复用已排序的同 owner 索引 | 晚到、同时间 ID、正文修订、删除和容量裁剪都要覆盖；不能假设 dict 插入顺序即时间顺序。不得按 key/count/id(dict) 判定内容没变。 |
| `_retire_participation`/checkpoint 重建 | 不跨退休边界且实际依赖集合不变时，可跳过结构重建 | `_advance_replay_boundary` 的旧数值折叠仍照做；真实引用、seen、hint、表达证明和边界变化须使准备失效。 |
| State→JSON→State→JSON | producer 冻结一次 bytes；原 Store 保存，不再恢复模型后重编码 | 同时冻结原 scope/generation/expected_revision，冷加载接回 scope 核验；取消确认真实 commit 后更新 revision。 |

最早退休时间只是局部可重建索引，不能替代当前来源或权限。先实现函数内共享，再根据实际所有变更入口证明跨 tick 复用；不增加全库 revision、永久授权缓存、每条来源计时表或新的 GC 字典。

## 5. 最小持久事实与热态大小

State 仍是一份逻辑快照，不拆成两套可独立恢复的业务状态。热态应包括原随机游标、逐步数值及实际边界；冷态是本次没有改变的事件、观察正文、原效果/提议与来源材料。完整字段归属须以逐入口变化矩阵决定。

至少不能漏：

- 原 scope/generation、唯一 revision，及原 epoch/pending 等与本次保存共同成立的来源。
- `sample_sequence`，以及原 now/last_sample_at 的快照字段；restore 仍按旧逻辑重置恢复时钟，不用它补抽。
- 实际 `replay_after`、belief baseline 的值/at/last_action、五种 trace 的值/at；相关计数不能因表示变更重置。
- 本次真实变化的冷材料必须和上述热态同一次提交。`attentions` 等无消费者字段若另经证明清退，可以减少热态，但不能默默漏存仍有消费者的字段。

主方案把 key/thread/target 和原字典顺序只存一份冷布局，热区按该位置保存明确类型的数值；last_action 属于热数值，原顺序真的变化则 full save，不另加排列索引。热态大小记为 H，冷体为 C；热态编码/绑定成本是 `O(H)`，不是 O(1)。H 仍随实际数值成员、attention 数量、整数十进制位数和浮点 token 长度增长。正常 belief 路径保留64项，不代表迁移时可裁剪旧可读状态。必须报告 H 的最小/中位/最大、各字段字节占比及多个 scope 的总 H，不能只展示五个标量。

原容量继续按旧完整 State 编码计算。冷准备取得原 typed 完整编码长度 L 与 typed 热编码长度 H，保存有符号差额 `K=L-H`；布局、存在性和各动态标量的原类型不变时，热保存的原逻辑长度是 `K+len(new_hot_bytes)`。迁移初始占用保留旧 payload 实际长度，第一次原语义 save 才更新为该规范编码长度；不能用冷热物理字节之和替代旧容量。完整类型、负零、非有限值和任意长整数对照见主方案。

所有派生顺序或字节数缓存放在原 Controller/Session 生命周期内，随原对象删除自然释放。不要另建按 scope/event 的全局缓存再增加独立扫描和 GC。保留旧 `_Session.saved` 全量字符串再增加 hot/cold 两份编码，也不是减少常驻内存；替换后要明确哪份原副本被删除。

## 6. SQLite 布局实验：同行两列并没有隔离物理大 BLOB

独占本地脚本 `D:/Code/Reports/Yuki/algorithm-20261026/participation_wal_layout.py`，结果同目录 `.json`。本机 Debian SQLite **3.46.1**、4 KiB 页、WAL、synchronous=FULL，数据库放 `/tmp`，实验中关闭自动 checkpoint 以数提交产生的 WAL 帧。没有连接服务器。

比较三种布局：

1. 原整 payload：scope/generation/revision/payload 一行。
2. 同行两列：scope/generation/revision/body/hot 一行，仅 UPDATE hot/revision。
3. 同 Store 竖向两表：head 保存 scope/generation/唯一 revision/hot，body 表只有同 PK 与 body，无独立 revision。只对 head 做 CAS；full save 在同一事务写两表，load 用原 key 驱动的单个一致性 LEFT JOIN 取得两部分。两侧均无才是无快照，仅缺一侧报真实错误，不能用 INNER JOIN 的空结果静默重建 Controller。

每种做12次提交，revision 从122到134；真实 JSON 数字和合成 header 长度会变化。下表 WAL 是新增帧数，每帧为 4,120 字节，另有 WAL 文件头，不是 Python 绑定量：

| 冷 JSON | 热 JSON 大小 | 原整 payload 帧数 | 同行两列帧数 | 竖向两表帧数 |
|---:|---:|---:|---:|---:|
| 65,610 B | 459–461 B | 312 | 312 | 12 |
| 65,610 B | 16,587–16,589 B | 384 | 384 | 93 |
| 524,362 B | 459–461 B | 2,328 | 2,328 | 12 |
| 524,362 B | 16,587–16,589 B | 2,400 | 2,400 | 93 |

512 KiB 冷体、小 header 的12次 Blob 绑定量：原整 payload 6,297,849 B，另外两种均为5,517 B。同行两列确实省了 Python 绑定冷体，却没有省本例的物理 WAL；竖向布局才把冷 overflow 页留在不更新的 row 中。16.6 KiB header 自身也会占 overflow 页，不能保证每次总是一页。

再单独隔离 record 宽度：冷体约512 KiB、hot固定462 B，revision 126→127只写1帧，127→128却让同行布局写258帧；竖向始终1帧。revision 固定在两字节整数范围内、hot每次只增1 B，同行每次258帧，竖向每次1帧。把不断变化的唯一 revision 留在冷大 row，即使 hot 移走也没有解决这个风险。

主方案已选第三种布局，不上线三套策略或密度开关。它是同一 State 的物理字段搬家，同一数据库、同一 Store、同一线程/事务/保存生命周期、同一个 CAS，不是第二事实源、独立热态进度或新工作队列。

## 7. 原子性、延迟与批量的边界

本地 SQLite 对照已经验证：

- full save 更新 head、尚未改 body 或尚未 commit 时，另一个连接的单 JOIN 只看到旧配对。
- rollback 后仍为旧 head+body；commit 后为同次新 head+body。
- stale expected revision 使 CAS 不更新，完整 State 保持原件。

还需在实际 Store 改动中覆盖取消、异常、scope 不符、完整容量统计、迁移、删除 scope 和重开数据库。producer 先取得原 `_save_lock`，再在同一无 await 片段捕获并取走本批 dirty、冻结当前 expected_revision 与不可变热/冷 bytes；不能先捕获 revision 再等锁。冻结/编码/入队前异常及已确认回滚把本批 flags 并回当前 flags，不恢复旧 State。await 期间新变化留下下一批 dirty；成功只确认本批 revision/codec 元数据，不把旧 State 或布局写回当前 Controller。

原事务只在 producer 已冻结材料后开始；同一 CAS 失败不能留下新冷体。取消继续等待原 operation，只有实际 future 成功才确认 revision、随后传播取消。实际异常不等于已确认回滚，也不假定现有 Store 已有一个读回查证“未知提交”的恢复器；错误沿原异常/CAS 事实暴露，不自动重放冻结 payload。

旧格式迁移只做一次原子格式切换。停止旧 writer 后，在事务外按固定旧 schema 读取、准备全部配对数据；不运行 Controller.restore/advance/prune，不提前重锚或重置时钟。迁移写事务只核原 `(scope,generation,revision)` 集合，发布 head/body 并清退旧 snapshots 表。原 Store 没有格式标记；事务性表结构已区分提交前的旧表示和提交后的新表示，不新增格式版本或逐 scope 进度。中断回滚后仍为旧表；提交后仅新表。正常加载重组后才运行一次原 restore。准备峰值内存需实测，不新增容量门槛。

不要为省 fsync 先增加等待窗口或延长保存周期。原保存点包含当前随机游标和数值，延后持久化会扩大 crash 丢失区间。多 scope tick 还有并发二和观测外部等待，等待“本轮所有 scope 都准备好”会把快 scope 的提交拖到慢 scope 后面。

先选竖向布局、保原及时单次提交，再看真实 fsync/commit 是否仍是瓶颈。若以后研究批量，只能复用原 Store 对已经就绪的操作，逐个 completion 要在实际 commit 后兑现，并明确一个 scope 的 CAS 失败如何不篡改另一个原已确认结果；不增第二队列、定时 coalescer 或后台协调器。本轮不以批量作为收益前提。

实验记录了编码次数、绑定字节、每次帧数、codec 与事务时间；时间只是本机单次结果，不能当生产 p95。原整 payload 的 JSON 往返用 stdlib JSON 模拟两次编码/一次解析，**不是 Pydantic 的性能测量**；布局事务计时没有加入原容量元数据查询。因此这里优先用确定的绑定量和 WAL 帧对比，不承诺线上倍数。

## 8. 必须通过的严格对照

1. 同一固定 seed、State、事件/反馈、参数变化及 tick 时间轴，逐 tick 对比所有数值 `float.hex()`、sequence、arrival/selection、排序和结果；随机 UUID 注入同一来源，不用忽略 proposal ID 掩盖差异。
2. 覆盖 dt=0、total=0、无 scores、Host 不可用、capacity/pending、明确邀请、epoch切换与相同时刻输入。不能把所有“没提议”都看成同一无变化分支。
3. 无新消息但有非零 baseline、退休边界前/等于/后、同时间 source、晚到效果、撤回与修改；旧浮点更新顺序与来源退休都保留。
4. 在每个旧保存点之后 crash，再用同恢复时刻和旧/新 tau 恢复；hot+cold 重组结果必须与原完整快照恢复一致，而非仅“能够启动”。
5. 竖向布局 full/hot save、CAS 冲突、commit/取消、迁移前后中断重开及单次一致读取；两侧均无、head-only、body-only 分别核验。格式拆分不 restore/prune，正常加载才恢复一次。header 大小、热/冷字段、revision 跨宽度、SQLite 页与 WAL 同时量，不能仅统计 Python 编码次数。
6. 多 scope 同时保存和一个 scope 很慢，核对无新增等待；比较相同时间轴的 wall、CPU、峰值内存、SQL/绑定字节和 WAL，既不改 tick 节奏，也不增加独立 GC/缓存维护成本。

本附件完成的是研究和本地反证。后续实现、业务回归、PR、合并和生产效果分别验收。
