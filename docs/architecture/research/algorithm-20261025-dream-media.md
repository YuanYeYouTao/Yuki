# Dream、Embedding 与媒体编码算法研究（2026-10-25）

本文是主任务书的研究附件，尚未修改业务实现。10-26的现行修订以[总任务书A07/A08](../Yuki-算法与资源开销优化任务书-2026-10-25.md)为准：Dream新增零degree尾段，Memory改为ANN选型与融合精确基线；不能继续把暂不考虑ANN当现行决定。基线为 `75e21a5c824876485d75e7fcd58e8111bdddeb4d`；本文涉及的 Memory、Dream、诊断、工具 artifact 和 emoji 业务源码与生产 `35b49fe9ee001c8f51ec5a5c59a41687130493bc` 无差异。本次只读源码、使用本机合成输入实验；没有调用模型、发送 QQ、运行线上 Dream 或重建索引。

遵循 [开发合同](../development-contract.md)、[Memory](../memory-v2.md)、[Embedding](../memory-v2-embedding.md)、[诊断](../execution-trace.md)与[媒体](../chat-media-workspace.md)。内部身份、来源、分区、原请求与回执、授权 SQL、浮点阈值和排序事实继续成立；精确基线不通过删事实、截断分区或长期缓存冒充同结果加速；ANN按本轮新方向单独评估召回差异。

## 1. Dream 当前真正消耗在哪里

源码位置：`memory/dream/service.py:546-644`、`memory/dream/repository.py:134-287`、`memory/embedding/codec.py:15-53`。

当前流程先按事实所属主体、可见范围和 kind 等真实分区，再按事实 ID 排序。增量模式跳过完全未变化的分区；一个分区有变化时，仍保留其中未变化的邻居。每个向量在该次分区计算中归一化一次，随后计算全部无序向量对，将每一对的浮点分数都保存到 Python `dict[(fact_id, fact_id), float]`。

每轮选择当前剩余图中 degree 最大的 seed，degree 相同选较小 fact ID。把其他节点按 `(-seed_similarity, fact_id)` 排序，依次接纳与组内**每一项**都达到阈值的节点，直到既有 cluster 大小合同。去掉整组后，减去它对剩余节点的 degree 贡献。最后保留涉及 changed fact 的组，并按组内 fact ID 序列排序。

- 向量部分是 `Θ(Σ nₚ²D)`；当前方案没有改变这个距离计算阶数。
- 全 pair Python 字典额外占 `Θ(max nₚ²)` 个 tuple、字典槽和 float 对象。
- 原候选排序把必然失败的负边也排了序；degree 更新又遍历每个 removed × remaining，查询已知不合格的 pair。
- 计算已经通过 `asyncio.to_thread()` 放在线程，不能把“再线程化一次”当优化。Python 点积循环仍消耗 CPU，并会争用 GIL。

主调查给出的当时规模是 1,519 条候选、102 个真实分区、最大分区 324。324 条对应 **52,326 对**，按 1,024 维为约 5,358 万次乘积；不能把 1,519 条跨分区合起来虚构一次全局聚类，也不能仅凭这些数量认定当前 RSS 或历史卡顿由 Dream 导致。

## 2. 精确删掉负边，不重新计算 seed 点积

令 `N(v) = {u | score(v,u) >= threshold}`。主任务书 D03 已采用只保存阈值正边及其**原始精确浮点 weight**的双向邻接 dict：

1. 原来每个无序 pair 仍只计算一次。小于阈值的分数立即释放；正边同时登记邻接关系、weight 和初始 degree。
2. seed 选择仍使用 `(current_degree, -fact_id)`。
3. 只对 `N(seed) ∩ remaining` 按原来的 `(-weight, fact_id)` 排序。
4. complete-link 只判断邻接；接纳后继续收窄合法候选集合。
5. 删除一组时，只遍历被删节点与剩余节点之间的正边，更新实际 degree。
6. changed 判断、分区顺序、组大小、组内 ID 顺序、最终排序及持久化保持原合同。

等价理由：旧算法的 group 第一个成员就是 seed。任何小于阈值的 seed 负边，无论原排序中排在哪里，都不可能通过 `all(score(candidate, member) >= threshold)`，因此不会改变后面的 group。保留正边的精确分数，seed 邻居之间的次序与 tie 不变。degree 只统计正边；complete-link 只需要知道边是否存在。这些条件对阈值相等、分数相等和 degree 相等都适用。

不要为了减少 pair 内存，在每次挑 seed 时重新算它的整行点积。那会增加距离计算，稀疏图的内存收益不能用近乎翻倍的 CPU 换取。

### 已选双向邻接 dict，bitset 仅作研究对照

| 表示 | 保存方式 | 优点与代价 |
|---|---|---|
| 双向邻接 dict | 每个节点保存 `{neighbor_id: exact_weight}`，两方向引用同一 float | 改动较少；成员判断、degree 更新直接沿邻居完成。稀疏图很省，稠密时两个字典槽有成本。 |
| bitset + 单份 weight | 邻接用 Python int 位集；weight 只在 `weights[min_id][max_id]` 保存一次 | complete-link 用候选位集交集；degree 更新枚举 `adjacency[removed] & remaining_mask` 的实际置位。稠密图比双向 dict 更省；多了位置映射与位操作。 |

bitset 的候选集合初始为 `adjacency[seed] & remaining_mask`，每接纳一个成员再与其邻接位集求交。遍历次序仍认已经按精确 weight 排好的候选序列，不能改成位次序。degree 更新按实际边枚举，不能又遍历全部 remaining 测位。

稀疏权重图空间是 `O(n + E)`；bitset 另有 `O(n² / word_bits)` 存储，不能笼统称其空间也是纯 `O(n + E)`。稠密时 `E=Θ(n²)`，最坏二次空间仍存在。若沿用 `max(remaining, ...)` 选 seed，仍可能累计 `O(n²)` seed 扫描；不能把整个后处理宣称线性。此处没有必要先增加 heap 的迟到条目和失效维护；先测是否真的成为剩余热点。

## 3. 本机等价与结构实验

实验文件在本地报告 `D:/Code/Reports/Yuki/algorithm-20261025/dream_graph_experiment.py`，结果为同目录 `dream_graph_experiment.json`，未纳入业务源码。

- 600 组合成图覆盖随机 ID、负分数、全正边、零阈值、精确阈值及 `nextafter()` 两侧、平分 degree、平分 weight 和多种 cluster 大小；双向 dict 与 bitset 共 **1,200 次**完整聚类结果一致。
- 另外直接调用当前 `DreamService._cluster_candidates()`，用合成 pair 分数核对 120 组 full/incremental，共 **240 次**。changed 过滤与最终组顺序一致；未变化分区仍跳过。
- 每个 324 节点分区三种算法都严格调用 **52,326 次** pair score，没有 seed 行重算。

下表是合成 pair 权重的**结构大小与本地原型耗时**，包含图构造和后处理，不含真实向量点积。结构大小按去重对象递归 `sys.getsizeof()` 计，不是 RSS 或峰值测量：

| 正边数 | 原 all-pair | 双向 dict | bitset + 单份 weight |
|---:|---:|---:|---:|
| 541（约 1%） | 6.51 MiB / 133 ms | 0.11 MiB / 7.6 ms | 0.12 MiB / 7.8 ms |
| 5,255（约 10%） | 6.51 MiB / 122 ms | 0.51 MiB / 16.8 ms | 0.39 MiB / 14.0 ms |
| 52,326（全正边） | 6.52 MiB / 102 ms | 4.11 MiB / 62.8 ms | 3.13 MiB / 62.0 ms |

这证明有可删的对象和后处理工作，不能据此声称线上完整 Dream 加速十几倍。真实点积很可能仍占大头。主任务书 D03 已选择双向邻接 dict；bitset 数据仅保留为研究对照，不再形成待定分岔、运行时密度开关或第二套实现。

## 4. 点积可先减少 Python 解释器工作

当前 `dot_normalized()` 消费 `math.fsum(a * b for a,b in zip(..., strict=True))`。候选优化为 `math.fsum(map(operator.mul, left.values, right.values))`，保持原 dimensions 核验、同序 binary64 乘积、`fsum` 和最终 `[-1,1]` clamp。

`EmbeddingVector` 在构造时校验长度、有限数和非零，且对象 frozen，见 `embedding/models.py:17-18,67-79`。合法输入下 map 不会改变乘积顺序和舍入方式；不能把严格长度检查删除后接收任意无校验序列。也不要悄悄改成不同精度、不同累加顺序或近似矩阵乘法。

本机 1,024 维、3,000 次点积：generator 约 164 ms，map/mul 约 89 ms；1,001 组输入的结果 `float.hex()` 相同。这只是微基准，不保证端到端减少 46%。新增依赖、ANN 或新的长期计算服务都不是这项改动的前提。

## 5. Memory Embedding、检索与重建的相同问题

### 授权语义检索是线性扫描，不是全对聚类

`embedding/semantic.py:44-137` 在 SQL 授权/profile/有效事实过滤后，对每一行重新准备文档 hash、解码 vector、归一化并与 query 点积，再用 `heapq.nsmallest()` 排前 k。复杂度为 `O(ND + N log k)`，不是 `O(N²D)`。

现有 query 只归一化一次，已使用堆选 top-k；不应把它改回全量排序。当前 `scored` 先保存所有 `(id, score)`，可直接把有序评分迭代器交给 nsmallest，并在同次遍历累计真实 stale/coverage 统计，删掉 `O(N)` 分数列表。授权 SQL、内容 hash 校验、相同分数按 fact ID 排、coverage/truncated 事实仍保留。

`load_authorized_vectors()` 当前 `.all()` 物化最多 scan_limit+1 行（默认 50,001），同时带 vector BLOB 与事实小列，见 `embedding/repository.py:56-117`。它是已有明确扫描预算，不是无限扫描；不能再添固定 fact 上限。10-26已确认这是主要内存来源：五万条1024维BLOB本身约195.3 MiB，尚未计正文；只删评分列表抓小放大。A08已改为ANN选型，并以同一显式读取快照内的真实stream/keyset融合扫描作精确基线；普通execute内部fetchall不能靠换fetchmany消除。同Session不自动等于SQLite多语句同快照，长reader的WAL回收代价需同时量。

### 解码和归一化存在重复维度扫描

`codec.decode()` 已 unpack、构造并校验 EmbeddingVector、计算 norm 以验证 float32 BLOB 近似单位长度。Dream 再 normalize；semantic.py:76-79、121-124 也 decode 后再 normalize：重复计算同一个 norm，并再次经过对象校验。

不能直接删除 normalize：float32 存储的近似单位向量与原重新归一化向量，在阈值附近可能给不同分数。可把“解码、验证存储 norm、供本次评分归一化”收成一次私有数值准备，复用已经算出的 norm，构造一次评分向量。一般 decode 的对外合同不暗改；非法 BLOB、维度和真实数学错误继续如实失败。先做 `float.hex()`、阈值/tie 与检索结果对照，不能只比 `isclose()`。

Dream 候选解码发生在 repository 的 async 读取阶段，`dream/repository.py:259-265`；语义检索循环同样在事件循环。把已经取到的不可变数值材料交给既有线程可以改善响应调度，但不降低乘法次数，也不保证 GIL 下 CPU 吞吐提高。数值去重和相同乘积的更少解释器操作应先于“更多线程”。

### Embedding 重建已经按页，不能写成未分页的全库大事务

`embedding/jobs.py:80-147,232-245` 已按 128 行 keyset 页准备当前 profile 的任务；常态只对 vector 缺失或事实较新者算 hash。worker 每批受真实 Provider HTTP batch 能力约束，调用后立即按原 claim 保存，并沿原 attempts/updated_at 恢复。

这里主要是每页 `O(text_bytes)` 的 Unicode 投影/hash 和每批 `O(batch×D)` 编码，不是 all-pairs。文档 hash 保护索引不陈旧，不能按 updated_at 相同就删真实内容校验。可以在**同一次准备和提交**间复用已构建文档/UTF-8，不跨新调用缓存正文或事实权限。

历史 Memory Rebuild 则沿事件分页、既有提取并发与原 item/preview 回执工作；`rebuild/service.py:544-618` 的 event hash 已在 claim 循环外准备，不能误报为每个 claim 重算整份事件。这里没有发现第二处 Dream 式全对向量计算。

## 6. 媒体与诊断：已确认的固定浪费及现有边界

### 6.1 暖 sticker 查询无需 Pillow

`emoji/media.py:15` 的模块级 Pillow 导入只在 `media_facts()` 的实际位图解码段 96-114 使用。`extension()` 只是字典；`inspect_path(inspect_pixels=False)` 会在 `media_facts()` 的 93-94 返回，不需要 Image 或 UnidentifiedImageError。WEBM/TGS 识别分支也在前面返回。

最小改动是删除模块顶层导入，把同一 import 放到 `if not inspect_pixels: return known` 之后、真正 `Image.open()` 的 try 前。真实解码保持原错误与格式语义，不加依赖缺失兜底，也不增加长期进程。Python 已导入模块本身会复用。

主调查单次线上只读 probe：Docker/Python noop 1.527 秒；helper `--help` 含 importtime 总 2.307 秒，其中 PIL.Image 累计约 0.804 秒、media 约 0.958 秒。两条纯 SQL 排序约 158/17 ms，32 KB/450 KB 原图 hash 约 9.83/6.05 ms，JSON 读取约 8.04/1.43 ms。说明**当时的新进程与导入比实际查询、文件 hash 大**；importtime 含依赖与观测扰动，不能保证每次精确省 0.8 秒，也不能倒推此前 20.8 秒全部由导入造成。

暖搜索按 term 主键倒排匹配后 GROUP/ORDER，再读取 limit=1 候选，不遍历全图库，不持 flock、不写 journal_mode。候选/索引失效才可能修缓存或自动 rebuild。不要为了计时调用可能写缓存的业务 search()/ready()。

### 6.2 诊断不是无限队列，也不是完整 prompt 多份深拷贝

`execution_trace/payload.py:26-93` 先估算准入再复制容器，str/bytes 可以共享。调用端两次树遍历有 `O(nodes)` 成本，节点/深度和 32 MiB 共享资源预留已约束它；大字符串不先全量编码再拒绝。消费者才在线程中做 JSON、脱敏、hash 和 gzip，`encode_payload()` 仍有 `O(bytes+nodes)` 工作及短时多份容器/字符串。

`DiagnosticWriter` 已限制 256 条、32 MiB，包含 active item；消费者空闲 `del item`，关闭等待真实线程完成后才释放预留，SQL 编译缓存也禁用以免保留首份 literal body。`model_invocations` 只有 usage/路由/延迟等小元数据，没有重复保存 prompt。不能把这些已回收的暂时拷贝叫内存泄漏，不能再建另一套队列或盲加限额。

仍可测的重复成本：业务解析 HTTP JSON，诊断线程又解析同一 response bytes；脱敏引用也会序列化媒体字段来计算摘要。是否值得改变所有权边界，应先看现有 snapshot/encode_execution 计时，保留不可变 bytes 与实际隐私围栏，不能用“线程里就没 CPU 成本”解释。

### 6.3 媒体 artifact 的重复 UTF-8 和全量读取

`tool_results/artifacts.py:364-365` 先 JSON 序列化图片，再 `len(content.encode())` 校验；随后 `write_artifact():228` 又 encode 同一字符串。这里可以复用一次确切编码结果和长度，不增加永久状态。图片沿实际 `vision_max_prepared_bytes`/`vision_max_frames_per_turn` 配置：应用 Settings 默认 16 MiB/16 帧，container.py:134-139 显式注入，不能误用 Repository 独立构造时的 6 MiB 默认值判断生产。普通 artifact 有 64 MiB 单件/512 MiB 存储边界；这些是已有合同，不等于所有并发操作的总内存只有这么多。

`read():443-453,511-512` 每次分页前先完整读文件、decode，JSON 操作再全量 parse；单次 `O(B)`，读取许多页时累计 `O(pages×B)`。大 JSON 会产生 bytes、str 与解析树并存的短时峰值，不能称泄漏。page fitting 的逐项缩页二次编码问题见主任务书；仅换线程不能消除重复编码。

## 7. 交给实现的顺序与验收

1. 先做无语义差异、少代码的 Pillow 按需导入与同乘积 map/fsum；分别量 import 和 dot，不能把微基准直接换算线上延迟。
2. Dream 按主任务书 D03 实现双向邻接 dict，删除负边存储/排序/degree 比较，正边精确权重只算一次；对旧算法做随机、阈值边界、tie、分区、incremental changed、cluster 大小和输出顺序逐项对照。
3. Memory按修订A08研究ANN并建立融合精确基线；复用norm和struct.unpack原float tuple，私有prepare产出后不再第二次normalize。精确基线保SQL权限、coverage与原top-k；ANN召回差异另测。
4. 复用媒体的一次 UTF-8 编码；诊断先用已有阶段计时确认占比，不恢复旧重复观测或全请求哈希。
5. 每项比较同输入、同维度、同分区的 dot 次数、Python 对象/峰值内存、线程执行时间和前台延迟。记录 exact-score/cluster 差异为错误，不能用平均余弦接近代替。

本附件完成的是研究和本机实验；代码修改、测试、PR、合并和生产验证尚未发生。

10-26补充：degree=0尾段须按原changed规则输出singleton并更新clustered_ids，不能丢为isolated。207组真实方法配对结果一致；324节点无边的seed扫描52,650→324，dot仍52,326。数值融合合法输入微测只说明局部删减收益，错误、阈值与ANN选型未据此验收。
