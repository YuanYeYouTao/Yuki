# 2026-10-26 算法与资源优化实施记录

本地实施与三轮交叉复审已完成，后续 A12 删减也完成三份独立复核。本文记录提交 PR 时的源码、实验与验收；CI、合并和生产回执接续记录在本次 PR 及本机部署报告中。独立参与库已合并。

主仓起始基线 `75e21a5c824876485d75e7fcd58e8111bdddeb4d`，任务书提交 `7c0e53aa`；已同步 `62506334` 的本机 Agent 统一接入及 `7b657956` 的 Manager 安装修正。集成分支 `codex/algorithm-performance-20261026`；原用户工作区未改。使用三个同级子代理及 root 并行，不使用 Astra。

## 范围与进度

| 工作包 | 实际进度 | 尚需完成 |
| --- | --- | --- |
| P01–P07 参与循环/窄快照 | [库 PR #27](https://github.com/Institute-of-Agent-Life/Yuki-Semantic-Participation/pull/27)已绿合并，固定`5d9ea2ca`；Host为`382ef6a7`，原seed/逐tick/恢复保留；库298项通过 | 主仓CI与部署 |
| A05/A06 分页与读取 | `77a897c9`已合流，按需JSON计量、稳定前K、两个预算修复、无query文本流式；巨大limit的小预算临时副本已另修 | CI；实际Monty VM |
| A07 Dream/数值 | `22ef3de4`已合流稀疏精确图、零degree singleton尾段、一次norm准备/map乘积；240组旧dense oracle一致 | CI |
| A08 Vec1 | `22ef3de4`已合流0153搬迁、真实SQL授权IN、原分数精排、索引/claim/遗忘同事务和覆盖口径 | CI、部署备份迁移与线上核验 |
| A09 回执来源 | `f82e0856`，实际Conversation先读并持强引用、错误kind仅在缺Conversation时查；原写时来源/隐私回归通过 | CI |
| A10.1 普通协议记录 | `f82e0856`，digest/size弱引用跟随原不可变record生命周期；删正文LRU及其容量/chain状态，恢复/GC/取消已复核 | CI |
| A10.2 累积opaque | 按任务书保留后续研究，不属本轮实施承诺 | 后续独立设计 |
| A11 媒体/诊断 | Pillow按实际解码导入；Artifact复用一次UTF-8；现诊断资源/取消/原件合同保留 | Linux实际媒体CI |
| A12 插件/外部退休/进程树 | `886a75c2`已合流页内批SQL、窄ID+online/在途排除、父子邻接遍历；相关回归通过 | CI |
| A12 automation/subagent | `810b89c2`起边claim边yield；`ffc24680`删除维护临时执行lease，三条writer SQL原子发布；未改源同页通知顺序保留 | CI与生产观察 |
| Native打包 | 固定Vec1/headers，复用上游SIMD dispatch；FMA、实际单架构macOS标签、无VCS源码包与Docker产物边界已修；真实wheel/load及源码重建通过 | 三平台CI、最终镜像和部署 |

## 已取得的验证

- 分页分支：67项通过，16项缺实际Monty worker而跳过；2700个Artifact与1200个CodeMode旧版差分相同，两预算bug另测新合同。冻结锁文件环境的mypy/Ruff通过。
- 参与分支：原pin源码逐tick/original UUID/hash oracle及38字段冻存/恢复；保存await期间cold变更与取消不丢失。阶段结果尚未当成最终合流验收。
- root首批：81项通过、35项Linux媒体测试在Windows跳过；另协议大前缀的弱引用生命周期测试通过。相关mypy通过。
- 合流定向验证：Memory/Dream/原协议恢复/插件/退休/分页204项通过，参与与配置127项通过；同步最新main后`mypy --platform linux src`的686个源码文件通过。各阶段测试有重叠，按各自覆盖范围记录。
- 插件/退休阶段：原插件13项、131个job跨页幂等、外部26项及131个原native operation不重复启动场景通过。进程树Windows跳过，WSL实际源码的1000/2000/4000链闭包正确，耗时约0.56/0.86/1.70毫秒。
- Native：Windows构建的wheel可在独立临时目录用stdlib SQLite载入；DLL仅依赖KERNEL32/msvcrt，不依赖MinGW运行库。WSL SQLite3.46.1实际加载fat binary，报告AVX2。Linux正式wheel将由auditwheel标实际ABI，macOS目标与Python平台tag对齐。
- A12最终删减：作者80项回归通过；两路独立增量验收分别20项及11项通过；另以真实SQLite trigger让父输入INSERT失败，确认结果/水位/输入整事务回滚，恢复后并发维护仅发布一次。原执行lease和预算不重建。
- 无VCS源码包：实际复现预编译DLL重复纳入wheel及sdist夹带缓存，修后源包702项中无native/cache编译物，vendor、hook、构建脚本齐；独立解包重建wheel与stdlib SQLite带过滤查询通过。

性能原件留在本机 `D:/Code/Reports/Yuki/`。以下只为合成实验：

| 路线 | 本地结果 | 范围限制 |
| --- | --- | --- |
| 1511×1024 Memory完整Repository管线 | 中位440.94→63.02ms；traced peak7.51→1.50MB；8组ID及原score相同 | 不含query embedding、FTS、RRF；不是线上整次请求七倍 |
| 4000项Artifact/同117项输出 | 旧3885次编码/45.3M字符，按需计量消除反复前缀编码 | tracemalloc下单次局部数据，不套到整机 |
| 8MB无query Unicode文本/100字符页 | traced peak56.0→0.469MB | 带query与JSON仍沿各自读取合同 |
| 8scope×80tick参与保存，无belief baseline | 编码78.17→0.261MB，WAL6.26→2.64MB，0.640→0.316s | 真实旧Host编码/adapter解析/Store再编码对新freeze/Store；不含executor调度 |
| 同上，每scope保留64个belief baseline | 编码90.38→2.255MB，WAL11.60→2.64MB，1.464→0.959s | 单次本地样本，不是全Host CPU |
| 32scope×80tick、各64个baseline | 编码361.53→9.02MB，WAL46.41→10.55MB，5.676→4.070s | 同真实保存管线，数值fold及head提交次数保持 |
| Dream500×1024，单分区稀疏/密集 | 8.19/8.66→3.77/4.38s，分组一致；峰值32.10/29.51→0.255/9.496MB | 局部planner；新归一化准备位于loader，另见报告范围 |
| 140个结果+每5ms计划到达50个前台写入 | 三组维护2.31–2.70→0.17–0.35s；业务SQL2102→47；writer420→9；完整owner总占用1.74–2.03→0.044–0.086s | 新前台p95仍增加4.5–17.1ms；FIFO与批提交集中造成，未宣称所有尾延迟领先 |

Vec1正式初始模式依据当前规模采用flat cosine原生NN，仍原算法精排。合成IVF/PQ质量不如该模式，未借ANN名称夸大收益；有限候选的近似性与真实索引故障分开报告。新profile/已有模型沿原profile所有权，不增按数量自动切换策略。

可移植数据：[分页计量](paging-scaling-20261026.json)、[Artifact](artifact-performance-benchmark-20261026.json)、[参与8scope](participation-snapshot-benchmark-20261026.json)、[参与1/32scope](participation-scope-scaling-20261026.json)、[Dream](dream-scaling-20261026.json)、[维护及前台等待](maintenance-performance-20261026.json)。Windows的原始CPU时钟读数不用于推断真实CPU使用量，计量范围和样本限制随各报告保存。

## 复审与交付

- 第一轮（完整合流`810b89c2`）：交叉核算法、dirty/恢复、来源/事务、分页与原生构建。分批至合流修正文本巨大limit的小预算副本、Vec1健康统计漏raw、FMA和macOS universal2错误标签；16MiB文本返回相同512字符信封，Python峰值33.57MB降至0.141MB。
- 第二轮（`cd1609a9`至`b75f4a0c`）：轮换核A12、参与、Memory及协议发布；移除三重eligible引用，证明真正终态不可变后省去大JSON叶解析，再删除不可达CASE。可变等待结果仍按真实叶CAS重备，跨改源不要求孩子ID全序。
- 第三轮（`b75f4a0c`）：再核所有原值消费者、迁移取消、数学/随机流、原回执、部署及实际源码包。发现并修正无VCS打包重复和编译缓存混入，`a9d4b408`后独立完成sdist→wheel→SQLite加载。
- 后续性能复核定位到临时执行lease的三次多余往返，`ffc24680`删除该链；root及两路代理分别复核完整增量，补真实expired owner、重新执行、generation、同revision改reason、归档与事务失败测试。没有新增执行身份、持久模式、并发上限或调度队列。固定压力样本的前台p95残留回归已单独保存。

后续顺序：独立库PR/CI/合并并固定主仓pin → 主仓精确head的相关及完整必需CI → PR合并main → 一致备份验证 → Bot-only同源部署/原数据迁移 → 自然流量、Manager/helper与peer核验 → 线上旧备份、部署临时物、废弃镜像与可重建缓存清理。正式Release或真实模型/平台测试不自动冒称完成。
