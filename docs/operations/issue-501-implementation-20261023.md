# #501 原调用回执恢复：实施与复审记录

日期：2026-10-23。对应[任务书](../architecture/Yuki-501原调用身份与回执恢复删减修复任务书-2026-10-23.md)。

本地实现和实现后交叉复审已完成；PR、CI、合并、生产部署分别记录。调研固定在 `8aef3f0b`，施工中用户合并 #502，实施分支已更新到 `dad3c4564a6c297b5dcc2c2c54c44229776b0f2d`，重新核对原生监督、拒绝结果、在线查询与新测试 fixture。

## 实际改动

| 项目 | 结果与对应验证 |
| --- | --- |
| D01 | 删除 MemoryAdmin 和 SDK 两层 active 列表后置裁决及 helper。原 mutation 的结果 ID 直接返回；当前授权和真实归属仍在原入口。冷重建后双修正、后续失效、插件撤权、真实人物隐私删除及 Person/Group 命令修正对应 V01–V03。 |
| D02 | 原 receipt repository 增加 mutation ID 查询，原结果字段移到 `MemoryMutationResult.tool_payload()` 共用。Work 根据绑定 ref 与原调用指纹读原 outcome；processing、错引用和缺回执保持未知，已知拒绝/无变化不变成未知。领域矩阵和 Main/worker 恢复对应 V04–V05。 |
| D03 | 原 repository 精确查询原 operation/Work/action/actor，复用一份本地投影；公开服务保留 #502 的当前在线包装。原生四类控制、原身份冲突、停用人物/撤设备、缺失和未知回执对应 V06–V08。 |
| D04 | 原 WorkJournal 接入领域结果，完整 T3 原件仍优先。历史效果使用原工具和 action；Code 已派发 child 按真实回执继续或停止，不重新派发。Main/worker Memory 与 Code native 确认/未知对应 V09，Social/现有 run 回归对应 V10。 |
| D05 | 内部 `ToolInvocationContext.call_id` 改为 `invocation_key`，同步七个生产文件的真实消费者。Provider/Social 字段、持久 operation_id、原 hash 编码不变。原身份测试和逐字 diff 对应 V11。 |
| D06 | 同步 Main、Tool Kernel、Memory、插件 Facade、native 合同；研究附件保留修复前固定基线及原失败输出。 |

没有新增表、迁移、全局操作表、注册中心、后台重试或回填。纯 reader 不写、不联网、不调模型；读回后原 Runner/Code/worker 仍可保存配对、检查点并进行合法后续请求，不设置整轮“零增长”门槛。

## 复审发现及修正

实施前由三名子 agent 分别终审 Memory、native、恢复与验收范围。实施后交换方向复审，主审整合并验证了以下问题：

1. accepted 但缺完整 result 时，模型已读到领域结果，内部 capture 却仍消费旧空记录。领域恢复显式以读回结果生成效果证据；保留完整 accepted 原件的优先级。
2. Code 的 T2 已派发 child 即使读到成功回执仍无条件停止。合并重复回执读取分支，仅真实 unknown 保留原停止语义，确认回执继续原 VM。
3. 上述 Code 恢复还需保留父 composition 的 `side_effecting`；新增断言验证真实已执行子效果不被标为纯读。
4. 已接纳控制可能同时携带 pending/unknown 原生观察。复用既有 `execution_evidence` 区分控制接纳与执行终态，使模型顶层和内部证据一致；原领域 data 保留原观察。该组合由持久仓库实验验证，未声称线上已发生。
5. 字段改名遗漏一个测试中的 `current_invocation` 别名消费者。修正后重新扫描真实访问点，不改 Social/Provider 的合法同名字段。

三路最终复审未发现剩余阻断项。#502 的纯结果投影和当前连接在线判断已按职责分开，持续监督 execution 投影及原拒绝结果保留。

## 验证

本地使用独立工作树及 `uv sync --frozen --all-extras --python 3.12` 建立锁定依赖环境。早期借用旧工作树环境时，类型检查因旧 participation 包出现五处 API 不匹配；切换本树锁定依赖后通过，没有为环境差异修改业务代码。

- `uv run --no-sync ruff check .`：通过。
- `uv run --no-sync mypy src --platform linux`：659 个源码文件通过。
- Memory 三模块：21 passed。
- native repository：30 passed；六个 native 相关模块此前在 #502 基线上 69 passed，数字存在包含关系，不相加为总数。
- `test_work_domain_receipts.py`：19 passed。原成功、contested、无变化、拒绝、processing、缺失/错引用，以及 native 控制/归属/观察矩阵。
- `test_domain_receipt_recovery.py`：4 passed。实际 Main/worker 与 Code 恢复消费，原来源、父子关系、预算、配对和效果语义。
- 上述两模块与 `test_tool_effect_audit.py` 联合：35 passed。
- 身份、Social、目录/历史及字段消费者扩大回归：14 个模块 169 passed；Linux 全量与原生二进制 CI 待 PR 验证。

最初扩大回归曾有 1 failed、163 passed、42 skipped；失败是已修正的测试旧字段访问。Windows 的 POSIX workspace 与原生 Monty 参数跳过，不能视为通过；Linux 全量与原生二进制测试以本 PR CI 为准。已有 SQLAlchemy 循环外键排序 warning 未由本次引入。

恢复测试重建真实的领域提交后、Work T3 前持久边界；Runner、WorkSession、CodeModeDriver、领域仓库真实执行，模型和 Code VM 为确定性替身。没有做 OS kill、真实模型/设备或 QQ 消息测试，离线绿灯不等于自然流量验收。

## 代码量与交付

相对 #502 主线，生产源码增加 294 行、删除 172 行，净增 122 行（16 个文件）；测试增加 965 行、删除 18 行。文档及归档探针约增加 940 行，主要是之前未提交的任务书、调查与证据，本身不计入运行代码。新增的是缺失的领域读取和结果接线；删掉的是重复 active 裁决、纯投影副本和 Code 恢复分支。净增落在调研估算内，但行数不是验收门槛，不能宣称总代码减少。

本修复无新 schema/协议；部署基线含 #502 的迁移 0150、Connector 0.3.0/协议 3。生产若尚未升级，需保留其配套条件。部署继续 Bot-only、先备份、保留 peers，不发送真实 QQ 测试消息；健康验证后清理旧备份、临时包和无引用旧镜像/可重建缓存。

提交、PR、CI、合并 SHA、镜像、备份、线上健康和清理证据待实际操作后追加，不提前写成已上线。
