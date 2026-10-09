# Code Mode 正确性修复记录（2026-10-07）

本轮依据用户提供的最小修复任务书及证据 ZIP，修复实验分支
`0f24a3b590d103eb547483161fb5873d5a78d032`，整合 main
`25cd6083015924bf80405ca7c346f84a995f6ebb`。该轮固定合同为 revision 14；
Pi 语义实现仍是本地代码，Monty 是实际 worker 依赖。没有部署或生产 QQ 验收。

## 复现与修复

| 项 | 实际改动与正式回归 |
| --- | --- |
| F1 | 控制停止语义写入原 accepted 子回执；恢复先检查停止、未知及观察门，再 settle VM。need_input、wait、complete、fail、交接分别覆盖假引擎及真实 worker；独立 Host 进程在 accepted 后退出，再恢复原 journal。 |
| F2 | 接入 PR258 的 owned execution / observation 和 explicit execution_finished，失败查询/控制不释放依赖；保留 code composition 排除、版本及 never-dispatched 条件、原 mutation/source/run/request 和累计预算。 |
| F3 | 自有 pending 执行允许语义 wait，沿现有 WorkControl 核验所有权；complete、新写入及未知效果保护保留。 |
| F4 | 正文、coverage、kind、renderer version 一起冻结；旧缺元数据投影走显式新 epoch。SQLite emergency overlay 追平/连接池重启、正反 mode 和四协议最终 payload 回归；补 NULL/旧 renderer 边界。正常主入口的 projection append 同样传入冻结元数据。 |
| F5 | 固定声明允许独占的 2–10 项纯 lookup 批次，零业务计量、按原序配对；混合写入拒绝，child 不读取主目录。查询不改变工具声明。 |
| F6 | 应用层 cache shape 明示不覆盖原生 continuation；四协议 messages 相同/continuation 不同反例由最终 wire observer 检出。完整 AppContainer 清单重新导出。 |
| F7 | 累计 stdout/截断标志保存到同一可信 boundary 的私有对象，复用原授权/绑定/GC；分段、取消、反复 journal restore 不重印、不丢失。包含真实 worker。 |
| F8 | 父 JSON 整体按实际序列化字符数限额，operations 提供计数、截断与原结果引用；完整原子回执不裁剪。2000/24000 和四协议 payload 均测试；并发/partial 仍按原根预算。 |
| F9 | 所有 pending 父调用配对保存后，在下一次 dispatch 前复用 business rebase。当前获准公共历史、任务/媒体与尚未观察的 portable 父证据进入新链，旧 opaque 留存原 journal。四协议 pending/settled 对照均检查后续两个请求、一次父结果、子效果与预算；另测双父配对前/途中、Provider pause/compaction 保护。 |
| F10 | 冻结插件批准 revision、元数据/schema/handler 合同指纹；派发前及 scope 等待后重新核验。真实 PluginManager 同合同重启、禁用、READ→MUTATE、schema 升级及等待中替换回归，旧链不执行新实现；热变化标 restart_required。补真实 Monty 子调用/Work 回执/原父重入，确认拒绝为 not_executed、零变更、不重复计量。 |
| F11 | UI 缓存比例使用同一完整 input 覆盖集合的 cache_reported_cached_tokens，原 cached_input_tokens 独立保留。真实 React DOM 总览/模型卡组合回归。 |
| F12 | 每个物理响应 usage 只归并一次，包含错误重试及 Claude pause；完整 input/cache 覆盖缺失保留未知，output/total 已知小计保留，另列 unknown 请求数。四协议 503→成功/失败、未知错误、显式零、单次、pause+retry 回归。 |

证据包 SHA256 为
`09e2436808bdcb9f03d30c738af436ec8e8ab98e485a68113f7fc38e941f1724`。
分支红色回归在修改前的清洁 worktree 运行；main 对照、插件及摘要两树对照使用精确
Git archive，并核验 import 路径，main 没有导入实验源码。
原始 lifecycle 2 失败、PR258 9 失败/2 通过（main 同 11 通过）、输出 3 失败/3 通过、
交错恢复 4 失败/4 通过。原插件和 rollup 审计的断言用于确认缺陷，因此旧树运行通过
代表缺陷复现，不是修复验收。初次插件路径检查因 `/var` 与 `/private/var` 别名误判，
修正路径核验后重跑，保留两次记录，不增加独立案例数。
见[基线证据](pi-codemode-evidence/correctness-20261007/baseline.json)、
[插件基线](pi-codemode-evidence/correctness-20261007/plugin-baseline-verified.json)、
[摘要基线](pi-codemode-evidence/correctness-20261007/rollup-baseline.json)。

## main 与数据库兼容

主线 PR255 的 native paid uncertainty 防重放检查移植到 TurnExecution；旧 `_run` 不恢复。
保留主线 API 3.1 和 MCP 移除，artifact/media 使用共享 `tool_results` 模块。
不能为了实验分支保留已经退役的 MCP manager 或动态工具注入。

已发布的分支 `0096_invocation_effect_indexes` 不修改。新增 `0097` 对两种实际旧数据库
做静态形状核验：兼容主线已退役 MCP 元数据的 0096，或分支仍有完整旧表的 0096；
只退役派生表、补必要 invocation indexes，拒绝半缺表/未知形状。`0098` 添加摘要表示元数据。
迁移测试从真实旧 producer 脚本创建数据库，正常 upgrade head，无 stamp、业务事实保留；
旧版本回退拒绝仍保留。0098 downgrade 只退役派生投影，用原生 drop column 保留外部触发器。

完整服务清单为 **68 项完整执行合同 / 39 项模型直调政策**。不启用 web 的隔离装配为37，
旧 inventory fixture37 缺 memory_change/read_tool_artifact；历史 fixture33 不代表当前部署。
见该轮完整清单及 wrapper 映射（历史库存已退出）。

## 测试代码与测量修正

下列合同变更均加注释，原身份、预算、来源、次数和结果断言保留：

- main 新增旧 Backend.execute 夹具转成 typed execute_call；sandbox/harness 夹具移除已删除 begin_batch，保留原准入与发送断言。
- 当前摘要夹具明确 kind/model 和 renderer1；旧 NULL 元数据不再伪装为当前 producer。
- automation/background/plugin/self/worker/runner/handoff 测试按 portable 父证据读取结果；旧 role=tool 限定不适用新链。pending 与 settled 都在配对后切新链。
- 旧大 opaque 的容量样本可合法 business rebase，新增精确原结果一次/旧 reasoning 不移植断言；原真实 compaction 回归保留。
- benchmark 的 tool_receipt_characters 补计新链 portable 父结果，避免任务完成却报零回执字符。历史 benchmark 报告不改写。
- MCP 专属 manager/HTTP 测试随主线功能退役；实验补充测试核验旧模块不可导入及原 Work unknown/no-replay，不声称它等价覆盖插件 HTTP 重试。
- retirement/migration 夹具分别对准0097、最终head0098；真实 producer0096 仍用原脚本。summary downgrade 曾暴露触发器丢失，修复产品迁移，而非删除触发器断言。
- 最终全量发现的 source-index 迁移样本只在 upgrade head 后排除已退役的两个空 MCP 派生表；仍核验退役发生及其余139张表的精确计数/hash。新 tool_results 两个旧 Backend 夹具转 typed 调用，原并发/顺序/预算断言保留。README 中英文发布 marker/head 更新0098，不改发布检查。

排错还区分了两个测试问题：journal restore 夹具需要把原调用 context 重新绑定当前 Host
（生产入口已有此步骤）；原生 stdout sink 按 feed 提交块，截断测试必须在 overflow 前
提交保留的第一段。修正夹具，保留两次 restore、原调用身份和 exact stdout 断言。

## 验证与覆盖范围

最终验证结果与日志在[验证记录](pi-codemode-evidence/correctness-20261007/verification.json)；
[固定源码及 worker hash](pi-codemode-evidence/correctness-20261007/final-source.json)
用于核对运行后没有无记录替换。

完整运行原始结果 **4702通过、5失败、1跳过，1105.52秒**。5失败均为上述合同夹具/README
发布基线，修正后整份相关模块与新增边界 **125通过**。全量之后仅修改四个已有测试模块、
添加两个测试模块及文档，产品Python源码未变；source delta在验证记录中逐文件列出。
这是完整运行与所有修改/依赖模块复测的联合证据，没有声称另跑一次全量并零失败。
Ruff check/format（1233文件）、mypy（715源文件）通过。前端102项及build通过，
最终仅格式化新增UI测试并复测7项，format/lint通过，保留7个既有lint警告。

macOS 上运行真实固定 Monty worker、真实 journal restore 和独立 Host 进程死亡；
这些通过不等于 Linux root-owned immutable worker、非 root Host/bwrap 部署验收。
Linux 原生部署恢复本轮未运行，原 worker 信任/权限约束没有放松。
唯一预期私有生产备份检查缺条件；不读取生产数据来消除跳过。
F9/F10 的新组合测试证明本文列出的具体触发点，既有全套覆盖其他来源/CAS/媒体/暂停/取消，
没有将所有组合或小时/天级运行概括成已测试。

Provider 真实验收在正确性回归通过后使用先前明确授权的 DeepSeek 凭据，限合成数据与私有
临时 SQLite。当前 route 和固定39项 Provider 清单、每场景预热1次及测量3次，
最终 body 观测不保存认证 header/密钥/真实聊天。实际结果另列，不以缓存命中率判定正确性，
不推断生产比例、上游最后一跳或 SSE。

本轮[真实wire记录](pi-codemode-evidence/correctness-20261007/live-cache.json)：
DeepSeek Flash / Chat Completions / `https://api.deepseek.com`，六场景全部完成，26次物理请求
（包括一次辅助压缩和一次独立前缀对照），每场景预热1+后续3。usage读入与缓存读取均完整，
unknown请求0，显式缓存零2次；缓存创建token字段未报告，保留None。
主请求固定工具hash一致；26次逻辑/物理账本与最终wire记录一一一致。C05只重开本地DB/session，
不当作进程死亡；进程恢复另由 native Host 测试证明。短间隔复用可观察到缓存读取，但独立
前缀仍复用公共工具声明，不能当作完全无缓存控制。没有长间隔、最后一跳或SSE测量。

本轮自建测试/对照临时根及已归档日志清理见
[清理清单](pi-codemode-evidence/correctness-20261007/scratch-cleanup.json)。统计的原文件
allocated bytes不等于文件系统净释放量；原始用户ZIP、附件、已构建worker/wheel、共享缓存及
AppleDouble文件保留。原压缩日志和最终hash可从提交重新核对。
