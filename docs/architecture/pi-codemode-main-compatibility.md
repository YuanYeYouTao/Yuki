# Code Mode 与 main 兼容修复

2026-10-05。实验分支起点 `3d1d983828207049cb5e0806ad01b5008c841591`，
接入 main `de9d0e3ae1d682ec6411df4d628db4ba061a5fd8`。用户要求保留实验版本和
新版兼容，便于随后 merge；仅在 `codex/pi-codemode-experiment` 整合，main 不写入。

## 实际改动

- 接入 main 的来源、配置与历史只读优化、SQLite 维护索引、空记忆重建恢复和 Gemini
  有界并行提取；上游迁移 `0092`—`0095` 保持原文。保留实验分支唯一新循环、Monty
  VM 快照、父子调用身份、原效果回执、累计预算和段末结果交接。
- 将 main 的 `return_to_caller` 完成提议与原回执复核移入 `TurnExecution`。跨段恢复
  不直接认定完成；新输入、来源失效或未知回执可撤销提议，空内部结果须有原 confirmed
  发送事实。真实原生工具派发前由 admission 保护原私聊 token，不恢复旧 `_run`。
- Protocol Store 同时保留 CodeSnapshotBinding/二进制快照和 main 的不可变记录缓存、
  有界 GC、文件身份核验及取消时等待线程结束的文件围栏。WorkSession 的来源读取优化
  与原未观察回执交接共存。
- 两边历史 `0092` 含义不同。实验调用索引移至 `0096`，接在 main `0095` 后；迁移核验
  同名索引完整形状、补齐旧实验库缺失的 main `0092` 索引，不改写业务数据。保留新
  invocation/composition 事实时拒绝 downgrade；无事实时仅删除自有三个调用索引。
- 九处文本冲突已处理；运行时行为按现行合同移入新循环。升级说明、README、发布草案
  与包装检查脚本的当前 head 同步至 `0096`。历史验收记录与原始 API 测量保持原样。

## 验证

两批针对性检查分别 53 项（17.12 秒）和 70 项（38.63 秒）通过，覆盖 main 的调用方
收尾、私聊抢占、Protocol Store 延迟/GC、来源只读检查及 Code Mode wire/聊天入口。
新迁移测试使用两个实际旧提交的源码创建数据库和领域事实，再执行正常 Alembic 升级、
启动 schema 核验、重复升级和回退护栏；原 Work、预算、Social/工具回执及 JSON 不变。
索引 JSON 路径大小写、唯一性、谓词和列顺序漂移的反例保持拒绝。

新增两个真实 worker 组合回归（1.27 秒）：三个独立 Social 发送跨原 VM 续接，再跨段
收尾并返回非空或空内部结果。同一 Work ID、三次模型请求和三个业务调用累计，重复
调用不增加任何发送。下游为隔离 fake，测试没有真实消息外发。编写此组合夹具时先修正
不存在的测试属性引用，再将原单发送夹具的固定 Social call ID 改为真实子调用身份；
两次失败属于新夹具接线，不是新增生产失败，原成功/不重发断言全部保留。

本轮未删除或放宽既有测试。既有迁移链的 head 断言从 `0092` 更新为 `0096`，
保留全部原事实、不重置预算及旧 reader 拒绝的断言，并在测试标注原因。
main 自带的普通聊天测试也保留其最新版本：无候选 Work 时不再先申请/释放空激活，
清理失败不覆盖已派发结果；这些变化来自上游接纳优化，不是为使合并测试通过而改断言。

首次全量 **3919 通过、1 失败、1 跳过，868.45 秒**。唯一失败来自 main 的
`test_social_source_lookup_index.py` 将 `upgrade head` 的结果固定断言为 `0095`；
合并后正常结果为 `0096`，其前一步严格 schema 启动检查实际已通过。修正夹具时将
`0095` 自有索引检查明确固定至 `0095`，保留原全库事实、完整 schema 差异及 downgrade
不变断言；旧 head 启动仍须拒绝，再单独正常升级至当前 head 并验证原业务事实不变。
相关 78 项全部通过（19.80 秒），没有改生产迁移或放宽检查。

最终全量 **3926 通过、1 跳过，813.13 秒**；ruff check、format（1188 文件）和
mypy（724 源文件）通过。真实 Monty worker 测试无跳过，唯一跳过项是未提供私有
生产备份路径。最终测试源码与[整合校验](pi-codemode-evidence/main-compatibility-integration.json)
中的 hash 一致。命令、退出码、耗时、worker SHA 和压缩日志保存在
[首次全量](pi-codemode-evidence/main-compatibility-regression.json)和
[最终全量](pi-codemode-evidence/main-compatibility-accepted.json)。首次失败记录保留。
中间复验在 361 项通过、1 项跳过时主动中止（128.15 秒），因为最后对照 main 时发现
非空内部结果不必额外查询 confirmed 发送事实；生产条件已收窄到与 main 相同的空结果
兜底，避免无用的来源/回执读取。17 项调用方及真实 worker 组合回归通过（6.95 秒），
随后以最终源码重新验证。[中止记录](pi-codemode-evidence/main-compatibility-regression-final.json)
与原退出码、日志均保留，不算通过。

最后迁移审查又复现一处新增程序核验缺陷：普通字符串替换会抹去列名中的
`ifnotexists`，导致错误列被误认成原索引。两个真实 SQLite 升降级反例实际失败，
已改成只忽略声明前缀的 `IF NOT EXISTS`、仅归一 ASCII 格式；Unicode 列名和
JSON 路径保持原样。增加错误列名、Unicode 大小写及 Unicode 空格的拒绝回归，
DDL 前拒绝并保留原 schema。87 项迁移联合验证通过（28.22 秒），真实旧分支升级
仍通过。原[程序缺陷反例](pi-codemode-evidence/main-compatibility-index-drift.json)保留。
该轮完整复验在 2715 项通过、1 项跳过时主动中止（566.01 秒），
[中止记录](pi-codemode-evidence/main-compatibility-verified.json)保留，不算全量通过。
修复后按最终源码重新运行完整检查。

## 交付与范围

本地整合与验证已完成，源码和证据作为本轮测试分支提交交付；提交号和远端推送核对
见最终回报。后续合回 main 不在本次范围。目标 main 是本次整合的合并父提交，
可按 `de9d0e3a` 核对祖先关系。

本轮未运行付费 API、真实 QQ、生产数据库、Linux VM、镜像重新装配或部署。包装
检查脚本的 head 已更新，但本次没有重新执行 Docker 包装验收。以前的长任务费用、
时间和完成率仍属于原实验提交，没有拿来宣称本次相对最新 main 的性能优势。

四个全量验证自有临时数据库根目录已删除，原始日志压缩并核 SHA 后保留；目录删除前
的 du 分配量合计约 19.287 GiB，非瞬时净磁盘释放测量。开发 worker/binding 保留，
未清理用户原有数据或共享 pytest 临时根。详情见[清理记录](pi-codemode-evidence/main-compatibility-cleanup.json)。

后续正式 merge、发布及部署需用户另行授权。原私有生产备份测试仍缺数据，本轮未运行。


## 2026-10-05 第二次兼容：main 原生多模态媒体

从测试分支 `c4afe62d` 接入 main `3ca36510`（PR #247、#248，包含 `14c69804`、
`8e462e60`、`dc68e1e6`）。这次 main 没有新增迁移，正常迁移 head 保持 `0096`。新的媒体能力
进入现有 `TurnExecution`，不恢复已退休的旧循环和 `execute(name, args, runtime)`。

- 主模型收到全部配对的工具回执后，按原 call ID 接收 Host 选取的历史、工作区、MCP
  或插件图片；每次实际派发前重新核验全链媒体预算、来源、文件版本和权限，包括
  已收入 opaque continuation 的图片。主合同 version 更新为 `12`。
- `MediaResultText` 的私有图片贯穿 typed Invocation、子 Agent 与 Code Mode，公开
  JSON 和 VM 的工具结果仍不含像素。组合父回执聚合原子调用图片并归档，恢复读取
  原回执、不重查图片或执行工具；来源隐私版本在组合起点固定并持久化，迟到父回执
  不能借用删除后的新版本重新发布旧图。
- 私有媒体引用与原效果回执 CAS 在同一短写事务提交。保留测试分支的 invocation /
  composition 元数据、冲突拒绝及已接纳事实；文件准备和序列化在 writer 之外。
- 只读别名和跨批缓存先保存原 receipt key，再派发。崩溃恢复复核名称、参数、只读
  属性及原链身份，成功副作用使缓存失效，不重扣预算、不把未知回执恢复成成功。
- 主线附件缓存冷哈希的有界内存实现、原生媒体准备、插件/MCP 来源权限和 WebUI
  图片选择逻辑完整保留。图片预算拒绝不会抹去组合已执行操作和原副作用事实。

本轮没有删除或放宽原断言。main 新增的三份测试夹具按当前 typed Invocation 合同
更新：`test_native_tool_media_runner.py`、`test_work_readonly_reuse.py` 和
`test_native_tool_media.py`。前两份第一次出现八项旧接口缺字段失败，后一份出现
`begin_batch` 旧接口失败；均保留媒体配对、原回执、不重跑和费用预算的原断言，
没有给生产代码添加兼容旧工具入口。适配后的相关检查通过。新增四个真实 worker
回归覆盖直接/分段图片聚合、原父回执重读、删除围栏及主循环收到组合图片。

最新主线三份增量原样接入；历史投影重建排除当前 trigger，九项原回归通过。
中止记录保留。首轮全量 4007 通过、2 失败、1 跳过，950.15 秒；两项失败均为历史
对照装配缺少 `_budget_tool_media`，当前恢复内核调用该方法时抛出 AttributeError。
只补齐对照装配的显式共享方法，24 项相关检查通过，没有修改或放宽验收断言。

最终联合复验 **4022 通过、1 跳过，960.10 秒**；ruff check、format（1201 文件）及
mypy（726 源文件）通过。真实 worker 测试无跳过；唯一跳过仍是未提供私有生产备份。
原命令、退出码、worker SHA、完整源码 hash 和压缩日志见
[最终联合证据](pi-codemode-evidence/native-media-main-compatibility.json)。其中 pytest
进程总耗时 971.08 秒，960.10 秒是 pytest 自报的测试耗时，二者不混用。

Python、运行时和测试源码在最终复验后全部保持相同 hash。复验过程中只更新安装器
PowerShell 的原生参数引号及旧 WSL 查询错误兜底；初始 hash 保留，最终 hash 和
补充验证单列，没有将它改写成开跑时的源码。最终脚本实际经过 PowerShell 7.6.6
的 Legacy 参数模式检查、八组原生参数、Bash 引号、状态续写、拷贝中断/重试、
保留现有编辑与 WSL 查询失败检查，另有 14 项安装器回归、Bash 语法和 Compose
配置检查通过；范围与记录见[安装器验证](pi-codemode-evidence/windows-deployment-verification.json)。

真实供应商图片/工具/原回执续接验证通过：原生媒体适配器两次请求 18.499 秒，
最终安装器同一探针两次请求 5.845 秒。它们属于真实 API 连通与协议验收，不是
与旧版的性能对照；原始记录见[原生媒体](pi-codemode-evidence/native-media-provider-probe.json)和
[安装器 API](pi-codemode-evidence/windows-deployment-provider-probe.json)。API 密钥未进入源码或报告。

此轮没有真实 QQ 发送、生产访问、PR、main 合并、镜像发布或部署。
Windows 一键安装器已准备，私有包使用联合验收通过的源码提交；目标 Windows 运行门
由安装器现场检查，当前不把 macOS 脚本检查当成 Windows 部署成功。
