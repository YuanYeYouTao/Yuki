# #501 原调用回执恢复：实施与复审记录

日期：2026-10-23。对应[任务书](../architecture/Yuki-501原调用身份与回执恢复删减修复任务书-2026-10-23.md)。

D01–D06、实现后交叉复审、最终 CI、[PR #504](https://github.com/YuanYeYouTao/Yuki/pull/504) 合并与 Bot-only 上线闭环已完成，配套 Connector 已通过服务端接纳，上线后清理已完成。自省沿用原配置关闭，真实业务验收边界另列。调研固定在 `8aef3f0b`，施工中用户合并 #502，实施分支已更新到 `dad3c4564a6c297b5dcc2c2c54c44229776b0f2d`，重新核对原生监督、拒绝结果、在线查询与新测试 fixture。最终交付分支又纳入 #503 的 `90e60b6850389a0a60341290277a38fb0559f99d`，保留原自省 owner 游标修复及原生事件测试修正。

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

本任务三路源码最终复审未发现剩余阻断项。#502 的纯结果投影和当前连接在线判断已按职责分开，持续监督 execution 投影及原拒绝结果保留。

## 验证

本地使用独立工作树及 `uv sync --frozen --all-extras --python 3.12` 建立锁定依赖环境。早期借用旧工作树环境时，类型检查因旧 participation 包出现五处 API 不匹配；切换本树锁定依赖后通过，没有为环境差异修改业务代码。

- `uv run --no-sync ruff check .`：通过。
- `uv run --no-sync mypy src --platform linux`：659 个源码文件通过。
- Memory 三模块：21 passed。
- native repository：30 passed；六个 native 相关模块此前在 #502 基线上 69 passed，数字存在包含关系，不相加为总数。
- `test_work_domain_receipts.py`：19 passed。原成功、contested、无变化、拒绝、processing、缺失/错引用，以及 native 控制/归属/观察矩阵。
- `test_domain_receipt_recovery.py`：4 passed。实际 Main/worker 与 Code 恢复消费，原来源、父子关系、预算、配对和效果语义。
- 上述两模块与 `test_tool_effect_audit.py` 联合：35 passed。
- 身份、Social、目录/历史及字段消费者扩大回归：14 个模块 169 passed。
- PR 首轮 Linux integration：879 passed、1 skipped（显式付费真实 Codex）、94 deselected；Native CodeMode：94 passed，真实隔离检查通过。
- PR 首轮 unit：3491 passed、1 failed。失败为 `test_sticker_library.py::test_concurrent_first_create_has_one_optional_caption`，测试桥的文件复制与生产不同，修复与复审另列下文；不能将首轮写成全绿。

最终提交 `d9ce2a1385c094aa0e2425773e30c0793d2d2bd0` 已包含 #503 与测试桥修复，[Quality run 39950928666](https://github.com/YuanYeYouTao/Yuki/actions/runs/39950928666) 全部通过：

| 最终 CI | 实际结果 |
| --- | --- |
| [Unit](https://github.com/YuanYeYouTao/Yuki/actions/runs/39950928666/job/120404904962) | 3494 passed，893 warnings，1105.06 秒 |
| [Integration](https://github.com/YuanYeYouTao/Yuki/actions/runs/39950928666/job/120404905278) | 879 passed、1 skipped、94 deselected，38 warnings，599.45 秒；唯一 skipped 为需显式付费授权的真实 Codex 测试 |
| [Native CodeMode](https://github.com/YuanYeYouTao/Yuki/actions/runs/39950928666/job/120404905072) | 94 passed、4374 deselected，2 warnings，52.05 秒；实际原生构建与隔离验证通过，原进程全部退出 |
| 静态检查与三平台 Connector | checks、Linux、macOS、Windows Connector 均通过 |

这里的 deselected 是 workflow 按 `native_worker` 分组选择测试，不是失败或跳过原生验证。以上是最终提交的独立结果，不与前述包含关系不一的本地测试数累加。

最初扩大回归曾有 1 failed、163 passed、42 skipped；失败是已修正的测试旧字段访问。Windows 的 POSIX workspace 与原生 Monty 参数跳过，不能视为通过；Linux 全量与原生二进制测试以本 PR CI 为准。已有 SQLAlchemy 循环外键排序 warning 未由本次引入。

恢复测试重建真实的领域提交后、Work T3 前持久边界；Runner、WorkSession、CodeModeDriver、领域仓库真实执行，模型和 Code VM 为确定性替身。没有做 OS kill、真实模型/设备或 QQ 消息测试，离线绿灯不等于自然流量验收。

## 首轮 CI 的测试桥修复

首轮 unit 的唯一失败来自图库目标文件的并发读取：`LocalDirectoryBridge.workspace_copy`
用 `write_bytes()` 原地截断重写，而生产使用 `FileWorkspace.copy()` 的临时文件与原子替换。
测试桥现直接复用真实复制方法，删除自制复制和返回值；并发测试先一次性准备稳定共享原件，
保留两个不同请求的并发收藏及单次 caption 断言。没有修改图库生产代码、关闭完整性检查、
串行化两次收藏或增加自动重试。

WSL Debian 的独立锁定 Python 3.12 环境运行整份 `test_sticker_library.py`：35 passed；
另以 30 个独立临时目录运行同一并发案例：30/30 通过。该验证证明修正后的真实复制桥
正常并发通过，不证明任意人为文件替换时序都必须成功；实际文件在读取中发生变化时，
原检查仍可拒绝。独立 POSIX 定点实验还确认，同内容原子替换也可能使旧 FD 的链接数与 ctime 改变，从而触发保守拒绝；它是既有可选 collection 边界，不由 #501 引入，本次未扩大修改生产文件完整性规则。修复经过另一名 agent 独立复核后随 PR 重跑最终 CI。

## 代码量与交付

PR #504 最终提交 `d9ce2a13` 相对 #503 主线，生产源码增加 294 行、删除 172 行，净增 122 行（16 个文件）；测试增加 988 行、删除 37 行。该提交的文档及归档探针约增加 955 行，主要是之前未提交的任务书、调查与证据，本身不计入运行代码；不包含随后 delivery-record 分支追加的交付记录。新增的是缺失的领域读取和结果接线；删掉的是重复 active 裁决、纯投影副本和 Code 恢复分支。净增落在调研估算内，但行数不是验收门槛，不能宣称总代码减少。

本修复无新 schema/协议；部署基线含 #502 的迁移 0150、Connector 0.3.0/协议 3。生产若尚未升级，需保留其配套条件。部署继续 Bot-only、先备份、保留 peers，不发送真实 QQ 测试消息；健康验证后清理旧备份、临时包和无引用旧镜像/可重建缓存。

PR #504 于 2026-10-23 20:27:28（UTC+08）合并，主线提交为 `985bf6fe3d45becbbd7dd893165c83e2a0a53f1b`。它与最终受测提交 `d9ce2a1385c094aa0e2425773e30c0793d2d2bd0` 的 Git tree 均为 `83f52838b3d2edbb68e085e5a2c8c7bcaaf0eac6`，逐文件差异为空。运行镜像与容器核验结果另列下表，源码 tree 一致本身不代替部署验收。

#503 修复了启动扫描按今日 hash 查找历史自省状态而漏掉同一 Person/Space、继而撞 owner 唯一约束的问题。私聊和群聊回归保留原 state ID、历史 hash、游标及原 run，并验证失败窗口继续使用原 run 和完成后的待处理尾部；该修复已进入上述最终 CI。它覆盖这一具体启动失败原因，不代表所有自省异常均已排除。线上仍需核对 `memory_self_reflection_started` 和详细健康状态，启动成功也不等于真实模型自省已经完成。

## 生产切换、清理与验收边界

| 项目 | 已取得的事实 |
| --- | --- |
| 停机备份 | 一致备份 `integrity_check=ok`、`foreign_key_check=[]`，备份数据库 revision 为 0149 |
| 实际数据库 | 随本次部署基线迁移至 0150；该迁移来自 #502，不是 #501 新增 |
| Bot 镜像 | 标签 `ops-d9ce2a13`，镜像 ID `sha256:d71968571bf878ca0df1adeee371563aa0d64c77fcfbb1696cfc1752aec8fd61`，revision 为 `d9ce2a13` |
| 容器启动 | 新 Bot ID 前缀 `eedec83b`，`StartedAt=2026-10-23T13:21:46.486031317Z`，即本地 21:21:46；`RestartCount=0` |
| 基础健康 | live 为 alive；详细 health 的 status/database/read 为 ok，writer accepting，OneBot 已连接，Work/automation running。后续复查 Bot 保持 healthy、restarts=0，未见启动 traceback |
| 服务范围 | 只替换 Bot，四个同行容器 ID 保持不变 |
| #502 配套 Connector | 本机原 0.1／协议 1 已升级为 0.3／协议 3 并实际运行；原配置 hash 与 8 个 operation 保留，SSH 与两个原生 Codex daemon 未动，没有重派原操作。原 2 个 watch ID 均保留，按迁移合同保持 execution 范围 |
| 服务端接纳 | 2026-10-23 13:22:27 UTC（本地 21:22:27），`service.connect` 在协议 3 校验、catalog 和 attach 后写入 `device_connected`；目录有 1 个 Codex available、error_type 为 null，同秒收到原生 progress。这些证据证明原连接与进度通路恢复，不代表新任务完成 |
| #503 自省 | 沿用旧 Compose 显式设置 `MEMORY_SELF_REFLECTION_ENABLED=false`，当前 health 的 enabled/running 均为 false。修复源码和测试已交付，但在线 worker 启动尚未验收；已询问用户是否恢复启用，尚未收到新选择，因此保持原配置，不属于 D01–D06 实现缺项 |
| 清理对象 | 核对引用后已删除本次备份（占用 4038238208 B）、上传包（占用 255025152 B，归档文件长度 255020839 B）和无引用旧 `ops-0aa3e08b` 镜像；Docker Build Cache 原为 0，没有将不存在的缓存计为回收。本机 Connector 临时备份已删除 343585 B；旧 uv runtime 仍被 SSH 引用，因此保留 |
| 清理结果 | 磁盘可用空间增加 4483842048 B，约 4.18 GiB／4.48 GB；当前可用 22174961664 B。四个同行容器仍为原 ID，原媒体、数据库、journal 和工作区保留 |

代码与部署闭环已经完成；没有新建原生测试任务、没有发送 QQ 测试消息，也未创建正式 Release。已有自然连接与 progress 不外推为真实模型自省、新原生任务完成或线上 #501 恢复效果通过。TCP 连通、协议接纳、Worker 启动与真实任务完成分别记录，不以基础健康代替业务效果。
