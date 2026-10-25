# Artifact 分页与协议重复处理：算法研究

日期：2026-10-25，10-26补充。本文是研究输入，不代表实现、性能验收或线上根因结论。

现行任务见[总任务书](../Yuki-算法与资源开销优化任务书-2026-10-25.md)：A05改为按需饱和计量与稳定前K选择，A06先做无query文本；A10.1保留普通record优化，累积opaque的A10.2改为后续研究，不能把下文旧原型当完整实现设计。

代码基线为 main `75e21a`。本文涉及的业务文件与线上 `35b49fe9ee001c8f51ec5a5c59a41687130493bc` 对照无差异。遵守[开发合同](../development-contract.md)、[结果预算合同](../tool-results.md)；不新增经验上限、读取门禁、专用存储或后台回执链。

## 1. 优先级与证据边界

| 优先级 | 问题 | 触发范围 | 结论 |
| --- | --- | --- | --- |
| P0 | inspect/get 从尾部逐项删，再编码整个候选页 | 任何 Provider 读取相应 Artifact 时 | 源码可证明最坏平方级重复处理；尚无线上该分支的耗时采样 |
| P0 | search 逐项追加并编码整个前缀；最终游标未参与相同计量 | 结构化 search | 重复处理可证明；最终页超过预算已在本地合成输入复现 |
| P0 | 字符串二分假设最终页大小单调 | 文本读取、JSON 字符串 get，接近 EOF 且游标较长 | 本地已复现存在能返回的最后一页，却报预算不足 |
| P1 | 每页全文件读取、哈希、解码、JSON 全解析及多份对象并存 | 大 Artifact 反复读小页 | 全量处理和同时持有可直接确认；不是本次历史 14 秒长尾的已证根因 |
| P1 | Work 协议记录缓存顺序扫描抖动 | Work 保存，稳定输入工作集超过现有 LRU 容量 | 源码及合成缓存实验成立；普通非 Work 聊天不走此保存路径 |
| P1，按实际启用安排 | 累积 opaque checkpoint 再深拷贝、全部保留 | 原生 Gemini，或返回 opaque 状态的 Chat Completions 等 | 条件性风险；本次线上 AGM Chat Completions 响应没有触发字段 |
| P2 | Code Mode operation 前缀逐项全量编码 | 开启 Code Mode，组合结果接近展示预算 | 同类算法；当前 direct 调用路线没有触发 |
| P2 | 预算与来源检查中的额外线性复制 | 大结果或来源发生变化时 | 可以削减副本；不能把必要检查和所有线性遍历都列为缺陷 |

线上最近 100 个 Artifact 的元数据中最大原件为 2,856,586 字节，说明大结果真实存在，不证明这些原件发生了上述分页循环。近期 `provider_start` 诊断编码有一次约 1.229 秒墙钟耗时，也不能归到 Artifact 分页。Memory 工具回执入口后的历史 14.3347 秒长尾仍缺内部阶段计时，不能用本文算法倒推。

## 2. 现有分页为什么变慢

源码位置：`tool_results/artifacts.py:806–987` 的 `_inspect_json`、`_get_json`，以及 `990–1087` 的 `_search_json`。

设请求候选有 n 项、全部候选编码量为 B、最终仅容纳 k 项。inspect/get 先造 n 项候选，失败后删一项，再造 n−1 项候选。等长项时处理量为 n+(n−1)+…+k，即最坏 O(n²)；数组还有反复 `selected_values[:-1]` 的列表复制。嵌套大对象会随每个候选反复编码，实际代价按字节量累计，而非只按项目数累计。

search 方向相反：`[*rendered, candidate]` 每次复制前缀，并重复编码已经通过的结果。若最终能放入 m 项，前缀处理为 1+2+…+m；这里的平方量是 m，不应把所有未展示匹配数 N 都算进这一项。全树遍历、匹配去重和最终排序另外存在。

本地隔离计数保持真实外层编码，只统计辅助函数处理量：`get(list(range(n)), offset=0, limit=n, budget=800)` 在 n=1000/2000/4000 时均返回 117 项，但分别编码 885/1885/3885 次，累计处理 2,660,252 / 10,881,252 / 45,323,252 字符。输入翻倍，编码工作量约变四倍。未接数据库、磁盘或模型；该数值不是线上耗时。

### 2.1 计量对象必须是完整工具信封

`capabilities/results.py:311–324` 的 `artifact_page_fits` 实际构造 `ToolExecutionResult(...).model_payload()`，再执行：

```python
json.dumps(payload, ensure_ascii=False, default=str)
```

预算是返回字符串的 Python `len`，不是 UTF-8 字节数，不是 token 数，也不是内部 `value` 的长度。默认分隔符为 `", "` 和 `": "`。`model_payload()` 会删省略字段，并带上真实的 `ok/retryable/mutation_committed/provider_id/tool_name` 等外层信息。优化不能只测内层页，也不能改成 compact JSON 后冒充相同计量。

上述现有 oracle 构造的是成功页。错误页在 `services/chat.py:415–436` 会把 `error_code/detail` 拆成 `ok=False/error_code/public_message`，计量时必须经过真实错误包装，不能沿用成功模板。配置允许任意正整数预算，小于最小完整错误信封时沿原结果预算器处理，不新增最低配置门槛，也不许诺这时仍能返回完整错误 JSON。

Code Mode `codemode/driver.py:898–956` 则使用 `separators=(",", ":")`；复用算法时传递实际编码选项，不能共享一个错误的固定常数。

### 2.2 需要修复的两个边界错误

以下实验只调用当前纯辅助函数，未调用模型、数据库或业务接口。

1. search：`value=['x']*10002`，`offset=9999`，`limit=1`。候选用 `next_offset=None` 计量，返回换成 `10000`。使用同一真实外层信封，预算 351，实际输出 352 字符，`artifact_page_fits=False`。这要求修正最终元数据计量，不能要求优化版与旧版这个错误输出相同。
2. 文本末页：`content='x'*100001`，`offset=99999`，`limit=2`。完整末页恰好 271 字符；预算 271 时，旧版先试 1 字符，其六位游标使候选反而更大，最后报 `artifact_budget_too_small`。JSON 字符串同样成立：`'x'*1000001`，`offset=999999`，`limit=2`，完整末页 389 字符，旧版仍报错。JSON 字符串的 `truncated=true→false` 也已计入实验。

所以不能在未经证明时对“整页大小”做二分。数据增长是单调的，但整数游标变 `null`、`truncated` 的变化可能缩小元数据。保留完整候选快路，或逐个前缀以常数代价核算真实元数据，都可以解决；不需要重试或新阈值。

## 3. 精确预算的线性方案

目标：数组分页为 O(B+n)，对象分页另加既有稳定排序的 O(n log n)；构造返回容器一次。这里 B 是实际需要检查的候选子树编码量。算法是小范围 JSON 计量与前缀选择，不是新预算框架。

1. 完整值快路保持原条件/原信封，用同codec有界计数到budget+1；fits才完整输出原值。未装下时转分页，不先dump完整值再计量。
2. 当前单元按需饱和计量，累计已选数据D(k)。不预建全部候选长度数组或全树memo，不因后面一个未选巨型字符串白扫所有转义。
3. 固定真实信封F只准备一次。允许前缀到不了EOF时，整数cursor位数不减，当前不fit可停止；能到EOF时，用当前整数态和真实末页null/truncated态metadata的最小值m(k)作未来下界。若F+D(k)+m(k)>budget，则所有后续前缀也放不下，含末页也可安全停止；等号合法。
4. 数据长于请求limit不等于到EOF。get原无元数据完整快路、分页原metadata、CodeMode compact格式各用真实编码，不手抄成功模板常数，不为查末页先扫描全部大value。
5. 对象键和search匹配路径只选前K=offset+limit项，用nsmallest与原stable key；全匹配扫描/去重仍保留。search先按原record_path查重再构造dict/path list，保首个matched_path；单条value_omitted仍按单条全预算分类，再核aggregate，不用剩余预算改变内容。
6. 只构造最终返回页。饱和值只服务预算决定，原错误若返回estimated_characters仍需完整精确值，不用budget+1冒充。两种metadata下界和Unicode按真实oracle验收。

原初版前缀预处理可消平方，但仍可能无用扫描大值；本轮采用上述更直接的按需形式，不上线两套算法。最坏计量仍随实际访问内容增长，小预算可以早停。

### 3.1 编码一致性

- JSON 来源图只有 dict/list/字符串/数值/bool/null；以实际 JSON 编码器为准，不能用 `str(value)` 估计嵌套 JSON。
- `ensure_ascii=False` 时，非 BMP 字符在 Python `len` 中通常仍计一个字符；引号与反斜杠计两个输出字符；短控制转义计两个，其他 C0 控制字符的 `\u00xx` 计六个。斜杠不额外转义。不能按 UTF-8 宽度、UTF-16 单元或 casefold 后长度计算页偏移。
- 字符串单次扫描原字符，仅累计当前转义长度并记录最佳可用位置，计量状态为 O(1)，不物化逐字符 Python int 前缀数组。每个候选仍计真实游标与其他元数据，完整末页单独核对；返回偏移仍按原字符串字符。不创建每个候选的切片和转义全文，最终只切一次选中页面；巨型字符串验收同时检查额外峰值内存。
- 对数值、非有限浮点值、负零以及宿主模板中的 `default=str`，保留实际编码语义；不要因为换计量器暗改接受范围。编码错误仍按原错误处理。
- 对象键保持 `sorted(keys, key=lambda key: str(key).casefold())` 的稳定性，包括 casefold 相等时的原顺序。公开 JSON 文件的键是字符串；若纯辅助函数仍接受非字符串键，其 `str(key)` 碰撞行为须单独测试，不能在优化中悄悄重定义。
- 不把所有子树各自完整 dump 后再相加，否则深嵌套会重读同一后代。首轮可以对互不重叠的分页单元各编码一次；搜索当前实际候选可以按需后序计量，父大小由已访问子大小相加；只复用本次真正访问到的结果，不先构建整棵树的长度memo。`JSONEncoder.iterencode` 可以避免整份编码字符串，但对巨型单个字符串仍可能产生大块；不能据此宣称常量内存。

### 3.2 大单项、搜索与错误页

首项本身过大时，不从 JSON 中间截断，不跳过首项冒充已前进。保留 `artifact_value_too_large`、`value_shape`、原 path；已有 `estimated_characters` 可复用本次计量，删去再 dump 整个大对象的重复工作。空页、offset 越尾和预算无法容纳一个字符的语义保持原合同。

search 保留匹配去重、第一次 `matched_path`、完整记录与 `value_omitted` 的已有判断，以及 `_path_sort_key` 的真实顺序。现有整数 path 排序是类型名和字符串组成的字典序，并非自然数字序；优化不顺便改成数字排序。

先计量单条匹配、确定是否省略 value，再累加结果前缀；最终游标必须纳入相同计量。单次后序子树长度可以避免多个命中反复序列化共同父对象。不要为了提速只返回首批扫描命中，改变全局排序、`scanned_nodes` 或后续 cursor。

另一个可删的复制来自 `walk` 的 `(*item_path, key)`：每一层都复制整条祖先 path，深链输入可产生 O(depth²) 的路径引用处理，即使没有命中。可改为单次遍历的 path 栈，仅在真正记录匹配时固化原路径；保持相同访问顺序和首次命中。无需添加深度上限。实际 JSON 解析器自身的深度行为仍须保持一致。

## 4. 大文件读取：能省什么，不能承诺什么

`artifacts.py:443–449,512` 当前在 I/O 后继续同时持有 `raw bytes`、UTF-8 `content str`、`json.loads(content)` 对象图，分页过程中这些局部变量仍活着。文件读入在线程，解码、解析和分页目前仍回到调用线程；大文件会带来事件循环延迟和多份内存。

### 第一阶段：不改格式即可实施

- 在线程内完成文件读取、原长度与摘要校验、解码及 JSON 解析，并在阶段结束立即释放不用的 raw/content；只把结构化读取需要的对象或最终页带回。不要把大对象又跨层复制一次。
- 可以在解码后释放 raw，再解析字符串；解析后释放字符串。这样真实减少三个大表示长期并存和后续页构造的峰值。解析过程中仍至少有输入字符串与对象图，空间复杂度仍 O(B)，不能写成 O(page)。仅把 `json.loads` 放到线程不等于降低内存。
- 按操作选择表示：文本读取不创建 JSON 图；结构化读取不继续保存文本原件；私有媒体仍按原媒体授权和结果处理。
- 文本可增量 UTF-8 解码和完整哈希，累计原字符数，保留命中位置附近及请求页内容，而不是全文、全文后缀、整个 casefold 后缀都留下。带 query 时使用流式匹配并保留 casefold 扩展到原字符坐标的映射；现有 `ß/İ/ﬃ` 结果不能变化。完整长度和完整性验证仍要求读完整文件，不能承诺只读页大小的磁盘量。
- 哈希与长度验证不省略；授权、generation、隐私的读前读后核验不删除。不要新增一个“校验过就永久可信”的第二套缓存。此次没有证据需要改 Artifact 文件格式。

### 真正取消 JSON 全图解析的条件

`json.load(file)` 在 CPython 并不自动成为流式 JSON 查询；mmap、to_thread 或把 bytes 传给 loads 也不会消灭完整对象图。若第一阶段后仍由全图解析主导，需要单独验证同一不可变文件上的流式解析/临时 span 索引：跳过无关 value，只物化选中的值；不是再建一套持久结果存储。

其成本下界必须写清：完整摘要校验仍 O(B)；任意无序对象的稳定键排序至少要保留键与位置等 O(keys) 元数据；search 的完整扫描和排序也不可能自动变成 O(page)。必须处理转义、UTF-8、嵌套结构、重复键的最后值与原插入位置、浮点解析以及整份 JSON 有效性，返回前不能只证明选中片段有效。此路线先做等价性实验，再决定是否值得增加解析代码；不把自制 JSON 词法器当成小改，也不先承诺引入新依赖。

## 5. 同类协议与上下文扫描

### 5.1 Work 协议缓存抖动

`runtime/work_journal.py:333–355` 每次 save 遍历当前请求的全部 messages/items。`runtime/protocol_store.py:55–56,159–185` 为不可变、无媒体记录提供 1024 条、合计 2 MiB 的身份 LRU。缓存未命中会编码、哈希；逐项触碰的稳定前缀大于容量时，前一轮尾部被本轮头部依次逐出，可能每轮全部 miss。

既有隔离实验只替换持久 put 为内存编码计数：80×约20KB（约1.6MB）第二遍新增编码 0 次；120×约20KB（约2.4MB）第二遍新增编码 120 次。不是磁盘或线上 benchmark，也不代表当前所有 Work 都达到阈值。

当前缓存已属于单个WorkSession，并非全局正文缓存。复用原链已发布manifest的稳定record digest，让元数据跟随原transcript/record生命周期，只编码真正delta，不保第二份正文。不能简单提高缓存容量来掩盖顺序扫描，更不能给可变 opaque/media 记录套用普通不可变记录命中规则。仍须保原 chain、记录顺序、原 digest、来源与完整性核验、发布及 GC 引用；未知/失效来源不能重放成成功。

### 5.2 累积 opaque checkpoint

`services/turn_transcript.py:172–179` 每次 accept 都 deepcopy 并追加 checkpoint；`217–244` 实际请求只取最新 checkpoint 与其后 delta，但旧 checkpoint 仍保留。`llm/protocol_state.py:13–24` 又复制原 payload；原生 Gemini 在 `gemini.py:173–185,471` 形成累积 tail。若第 t 轮 checkpoint 含 O(t) 项，全部旧 checkpoint 的容器与引用保留为 O(T²)，复制工作同样累计。不可变字符串通常共享，不能说全部正文的每个字节都复制了 T 次。

隔离实验使用现有 `checkpoint_items+accept`：16/32/64 轮保留 tail 记录数为 136/528/2080，tracemalloc 当前分配约 0.38/1.37/5.22 MB。只说明容器增长，未包含真实模型网络和图片。

当前线上 profile 是 AGM `chat_completions`，不是原生 Gemini。`openai_compatible.py:265–292` 只有 `reasoning_details/encrypted_content/reasoning`、list content、已有 continuation 或 native_tools 时才创建 continuation；`reasoning_content` 单独存在不会触发。近期核查的两次工具响应只有 `role/reasoning_content/tool_calls`、最终响应为普通字符串 content，因此本轮明确没有该累积 checkpoint 风险的触发证据。

不能直接把旧 checkpoints 全删：`TurnTranscript.prefix(boundary)` 在 `156–164` 深拷贝前缀，context compaction 依赖合法旧边界；首个 checkpoint 位置还决定初始 messages。先证明哪些是重复容器、哪些是合法历史边界，保原已付请求、opaque 字节/插入顺序、工具配对、恢复和 compact。静态无服务器状态协议本就需要发送累积输入，每轮网络编码的总量可能仍为平方累计；优化不能承诺所有 T 轮工作总量都线性。

10-26复核后，A10.2改为后续研究。原不可变块/前缀引用只解决容器共享，`context_compaction.py:514–536`仍遍历旧checkpoint，`capacity.py:165–168`逐个计量opaque，可能继续平方扫描。真正实施需把原Provider语义、精确token/媒体成本、旧边界、journal、恢复与GC在同一owner内闭合。当前AGM未触发，不把跨Provider通用prefix表示强塞进本轮普通record优化。旧边界和已付事实继续保留。

### 5.3 Code Mode 与普通结果预算

`codemode/driver.py:951–955` 逐项 append operation 再 encode 整个 body，存在同类前缀重复编码。复用前述精确长度与前缀选择，不引入第二套分页服务。其字符串裁剪也可按一次转义长度扫描替代反复整 body 编码；真实 control、原 operation ID 和 leaf result_ref 不裁坏。当前 direct 路由不走这段。

`capabilities/results.py:200,579–607` 的完整结果编码、`_largest_collection`、摘要投影是数次线性处理，不是同样的缩页平方循环。可删的具体副本包括 `_bounded_payload` 对 dict 的完整 `list(item.items())` 后再切前 limit，以及 `_largest_collection` 的星号展开临时 tuple；按真实配置需要的前缀迭代即可。完整原件归档仍需一次真实编码，不应为了局部摘要省掉完整原件。

### 5.4 历史与来源：已有削减机制，不能倒退

`conversation/scope_repository.py:174–282` 复用历史 source，并按 `scanned_through` 只读取新增事件；冷源读的是 compact 覆盖之后的窗口，不是永久账本全史。`prompt_history.py:150–196` 常驻缓存剥离正文、媒体和 external_payload，只留渲染所需元数据。`projections.py:114–152` 已有单份 LRU 与共享预算，不是无限增长 map。`frozen_fragments.py:53–82,224–243` 是持久 AVL ID 集，增量插入不再每条复制整个旧 set。

`runtime/work_source_guard.py:247–280` 同数据库/版本对象、revision、身份、隐私与依赖未变时跳过全行核验；变化时才重读选中来源。`29–48` 的 `_source_value→json.dumps→UTF-8→sha256` 有多份线性表示，可以研究一次流式摘要消除中间树/字符串，但必须生成完全相同的类型标记、字段顺序与 digest。禁止删当前来源检查或用聊天 ID/今日账号反推旧执行身份。

这些热路径先以真实命中/未命中、读取字节量和调用次数分段采样。既有增量索引与来源复用保留；不另起历史缓存，不把已有线性冷启动统称为平方级算法。

## 6. 实施与验收建议

建议顺序：先完成 Artifact 的精确前缀算法与两个边界修复；再删读取中的重复大表示、移动纯 CPU 准备到线程；随后针对 Work manifest 与实际启用的 opaque 路线做单独变更。Code Mode 共用小算法，但不借此重做组合执行和回执框架。

正确性验证使用真实 `ToolExecutionResult.model_payload()` 编码结果核对，不冻结内部调用次数。覆盖数组/对象/inspect/search/文本与字符串、完整值快路、空容器、越尾 offset、首项过大、预算刚好够/差一个字符、next_offset 位数变化和末页 null、转义字符、中英文/emoji/组合字符/lone surrogate、casefold 相等键、数值与非有限值、嵌套大单项，以及 query casefold 扩展。除两个已证旧错误外，新旧值、排序、path、shape、cursor 和错误语义应等价。

性能实验分别报告：只计量与选页的 CPU/墙钟、峰值分配、编码总字符数、实际一次读取的磁盘字节量；同一冷/热条件下比较多种 n/B/depth。对固定预算而请求前缀不断增大的场景，计量处理量应近线性；字典还含排序项。JSON 全解析仍 O(B) 必须直说，不能只展示页构造变快。需要最终真实大 Artifact 的只读采样才能声称线上收益。

协议验证覆盖原 chain 恢复、prefix/compact、opaque 顺序与签名、工具调用配对、原 manifest/digest、source/privacy 失效、媒体非缓存分支和 GC。性能优化不改变执行、投递或任务终态，不重跑业务、不重置预算、不创建新结果 ID。

至少三轮复审分别检查：算法与边界等价性；原件/来源/协议/回执与恢复；真实配置和当前路由的性能数据及文档是否过度承诺。不要用重复全量测试代替这三种不同问题的复审。
