# 2026-10-26 算法与资源优化实施记录

状态：实施与合流中。本文只记录已有证据；CI、PR、合并与生产阶段未完成，不以局部测试代替交付。

主仓基线 `75e21a5c824876485d75e7fcd58e8111bdddeb4d`，任务书提交 `7c0e53aa`。集成分支 `codex/algorithm-performance-20261026`；原用户工作区未改。使用三个同级子代理及root并行，不使用Astra。

## 范围与进度

| 工作包 | 实际进度 | 尚需完成 |
| --- | --- | --- |
| P01–P07 参与循环/窄快照 | 独立库与Host实现中，原seed/逐tick/恢复保留；库295与Host131项阶段测试通过 | 最终提交/依赖pin、独立复审、集成验收 |
| A05/A06 分页与读取 | `77a897c9`已合入本地集成，按需JSON计量、稳定前K、两个预算修复、无query文本流式 | 合流复审/CI；Monty实际VM另核 |
| A07 Dream/数值 | Memory分支已实现稀疏精确图、零degree singleton尾段、一次norm准备/map乘积 | 最终提交/合流与旧oracle |
| A08 Vec1 | Memory分支已实现0153搬迁、真实SQL授权IN、原分数精排、索引/claim/遗忘同事务和覆盖口径 | 最终提交/合流、部署备份迁移与线上核验 |
| A09 回执来源 | `f82e0856`，实际Conversation先读并持强引用、错误kind仅在缺Conversation时查；保原写时来源 | 集成后的来源/隐私回归 |
| A10.1 普通协议记录 | `f82e0856`，digest/size弱引用跟随原不可变record生命周期；删正文LRU及其容量/chain状态 | 合流后的恢复/GC/取消复审 |
| A10.2 累积opaque | 按任务书保留后续研究，不属本轮实施承诺 | 后续独立设计 |
| A11 媒体/诊断 | Pillow按实际解码导入；Artifact复用一次UTF-8；现诊断资源/取消/原件合同保留 | Linux实际媒体及合流检查 |
| A12 插件/外部退休/进程树 | root已写页内批SQL、窄ID+online/在途排除、父子邻接遍历；阶段测试通过 | 提交、合流复审与CI |
| A12 automation/subagent | 独立分支实施：边claim边yield、原通知叶及lease页内结算 | 最终提交/合流与回归 |
| Native打包 | `22bac452`起固定Vec1/headers，复用上游SIMD dispatch；Windows和Linux构建/load通过；wheel已验证 | 三平台CI、Docker实际镜像和部署 |

## 已取得的验证

- 分页分支：67项通过，16项缺实际Monty worker而跳过；2700个Artifact与1200个CodeMode旧版差分相同，两预算bug另测新合同。冻结锁文件环境的mypy/Ruff通过。
- 参与分支：原pin源码逐tick/original UUID/hash oracle及38字段冻存/恢复；保存await期间cold变更与取消不丢失。阶段结果尚未当成最终合流验收。
- root首批：81项通过、35项Linux媒体测试在Windows跳过；另协议大前缀的弱引用生命周期测试通过。相关mypy通过。
- 插件/退休阶段：原插件13项、131个job跨页幂等、外部26项及131个原native operation不重复启动场景通过。进程树Windows跳过，WSL实际源码的1000/2000/4000链闭包正确，耗时约0.56/0.86/1.70毫秒。
- Native：Windows构建的wheel可在独立临时目录用stdlib SQLite载入；DLL仅依赖KERNEL32/msvcrt，不依赖MinGW运行库。WSL SQLite3.46.1实际加载fat binary，报告AVX2。Linux正式wheel将由auditwheel标实际ABI，macOS目标与Python平台tag对齐。

性能原件留在本机 `D:/Code/Reports/Yuki/`。以下只为合成实验：

| 路线 | 本地结果 | 范围限制 |
| --- | --- | --- |
| 1511×1024 Memory完整Repository管线 | 中位440.94→63.02ms；traced peak7.51→1.50MB；8组ID及原score相同 | 不含query embedding、FTS、RRF；不是线上整次请求七倍 |
| 4000项Artifact/同117项输出 | 旧3885次编码/45.3M字符，按需计量消除反复前缀编码 | tracemalloc下单次局部数据，不套到整机 |
| 8MB无query Unicode文本/100字符页 | traced peak56.0→0.469MB | 带query与JSON仍沿各自读取合同 |
| 8scope×80tick参与保存 | 编码39.09→0.261MB，WAL6.26→2.64MB，0.376→0.286s | 保存路径合成实验，不是全Host CPU |

Vec1正式初始模式依据当前规模采用flat cosine原生NN，仍原算法精排。合成IVF/PQ质量不如该模式，未借ANN名称夸大收益；有限候选的近似性与真实索引故障分开报告。新profile/已有模型沿原profile所有权，不增按数量自动切换策略。

## 复审与交付

三轮实施后复审待全部合流后进行，分别核算法/数学边界、来源/事务/回执/恢复、真实构建/配置/部署与性能。任何修正都要复核相关路径；未完成项不会提前勾选。

后续顺序：独立库PR/CI/合并并固定主仓pin → 主仓精确head的相关及完整必需CI → PR合并main → 一致备份验证 → Bot-only同源部署/原数据迁移 → 自然流量、Manager/helper与peer核验 → 线上旧备份、部署临时物、废弃镜像与可重建缓存清理。正式Release或真实模型/平台测试不自动冒称完成。
