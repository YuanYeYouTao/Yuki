# 后端审计对应的测试与 CI 整理

基线为 `1d831151d37718bb9f320020ada1a15b7277edec`；对应[五轮审计](backend-five-pass-audit-20261020.md)。本轮只改测试、pytest 标记和 Quality 工作流，未修产品源码、迁移或生产配置，未提交、推送、创建 PR 或上线。

## 测试原则与已知缺陷

测试断言应有的正常行为，使用实际公开入口、原内部人物/事件/执行、临时 SQLite、Fake 模型和 MockTransport。禁止把“当前错误确实发生”当成产品正确行为；不依赖 `.cache` 中的诊断脚本。

本次增加 **100个参数化用例：49个正常控制、51个已知缺陷回归**。后者使用标准 `pytest.mark.xfail(strict=True, raises=...)`，原因注明审计编号和报告路径。关闭 xfail 后，51项均已分别核实在目标断言/实际错误处失败；它们不是修复已绿。正常授权、原件和回执对照不标 xfail。异常类型收窄到实际已知失败，不使用异常通配；非指定异常仍报错，同类型的fixture错误不能仅凭xfail自动辨别，因此另核实际失败阶段。

后续修复对应产品后，strict XPASS 会使该测试失败，要求一并移除过期标记；不能扩大 xfail/skip 来保绿。本轮没有增加忽略清单、自定义已知失败执行器或新的产品门槛。

## 覆盖矩阵

| 审计项 | 永久测试 | 验证目标与保留对照 |
| --- | --- | --- |
| F01 | [正文保留](../../tests/unit/test_message_body_preservation.py) | Issue编号、时间和代码正文原样保留；实际历史 envelope、`yuki-state` 尾协议、控制字符和真空输出分别验证 |
| F02 | [插件已知结果](../../tests/unit/test_plugin_known_result_audit.py) | 后置审计失败不重跑 handler、不推翻明确 mutation；真实 OneBot ACK 与 SQL audit 异常；实际未确认派发、停用/撤销启用和非管理员仍拒绝 |
| F03 | [Embedding部分成果](../../tests/unit/test_embedding_partial_results.py) | 实际 Provider HTTP 批、jobs/worker、profile/hash/claim；部分成功不丢、不重复请求；兼容按 Provider 实际能力缩小 claim 的最窄修法 |
| F04 | [Session资料与结果](../../tests/unit/test_plugin_session_material_contract.py) | 旧资料/DTO、已返回模型结果保存、保存后返回分别覆盖；新 run 资格、他场景来源和每消息 metadata 保留。没有为测试虚构 SDK get API |
| F05 | [真实换图](../../tests/integration/test_plugin_vision_source_edits.py) | TG原生编辑同事件换图，公共Plugin Vision读取新 hash；原图、只改caption及相同hash复用不重购 |
| F06/F08/F09 | [身份与读取](../../tests/integration/test_audit_identity_contracts.py) | 原生提及与编辑投影一致；实际App配对→合法解绑→旧设备新消息不造新人，原回执继续读；旧Social资料不依赖目标新发送资格，真实source/委托scope仍有效 |
| F07 | [Unicode原文坐标](../../tests/unit/test_tool_artifact_text_offsets.py) | `ß`、`İ`、`ﬃ` 前缀扩展不改变原文offset/next_offset；中文、普通分页、无匹配和匹配内容扩展对照 |
| F10 | [schema资源边界](../../tests/unit/test_capability_schema_resources.py) | 六个漏检位置均不外取；examples.$defs不提升为活动schema；local defs继续校验类型，普通examples/const里的$ref不误拒，禁止真实网络 |
| F11 | [临时媒体清理恢复](../../tests/unit/test_plugin_media_cleanup_recovery.py) | 一次unlink失败保留可恢复原路径；正常过期、pending/processing outbox及跨插件读取对照。范围不涉及收到的原件/持久表情库 |
| F12 | [贴纸来源](../../tests/integration/test_sticker_collection_source.py) | 真实TG入站与共享下载屏障中执行 `/ai new` 后仍收藏；无变化、edit、forget及原event/segments/SHA/可读原件对照 |
| P01/P02 | [个人API查询增长](../../tests/unit/test_client_projection_costs.py) | 精确原件授权不读全部历史、不逐Work重建UI；home/sync保完整输出并批读辅助事实；原设备/版本/人物授权沿相邻既有回归验证 |
| P03 | [ACP](../../tests/unit/test_acp_adapter.py) 新增空增量用例 | 无result delta不重复拉无关尾页；保原POST/查询/原回执与输出结束语义 |
| P04 | [群恢复](../../tests/unit/test_group_route_recovery.py) 新增异步屏障用例 | 仅无关TG断开不使QQ恢复冲突；原目标连接变化仍拒绝，原恢复回执不改 |
| P05/P06/P11 | [Work查询规模](../../tests/unit/test_work_query_scaling.py) | 调度名额相同、定点run相同、归档空轮没有新工作，增加无关Work/兄弟/旧归档树不应同比放大；保真实Conversation外键、waiting后代与原结果 |
| P07 | [工作区发布规模](../../tests/unit/test_workspace_publication_cost.py) | 发布新不可变文件不反复扫描全部永久manifest；原blob与新发布字节均保持 |
| P08 | [媒体I/O调度](../../tests/unit/test_media_io_scheduling.py) | 同一loop回调应能在受阻文件操作结束前运行；Timer仅防测试死锁，不用毫秒阈值。ASR/视频输出和解码协议另有正常控制 |
| P09 | [Dream规划](../../tests/unit/test_dream_planning_cost.py) | 完全不变分区不做pair计算；有变分区仍保全部未变邻居，不能只聚类changed子集 |
| P10 | [自动化等价查询](../../tests/unit/test_automation_equivalence_cost.py) | 实际完整规范比较与owner-independent目录保留；50→1000个候选允许线性增长余量，拒重复OFFSET的超线性工作 |

SQL性能测试使用同一SQLite版本上的相对增长或辅助查询增长，不冻结精确VM、SQL文本、固定调用次数或墙钟耗时。[SQLite辅助探针](../../tests/support/sqlite_cost.py)捕获实际读取SQL，在不变临时库中以 `mode=ro` 重放并完整取回结果，统计SQLite VM steps；用于查询复杂度，不用于证明平台入站授权。调度/Work测试隔离真实查询，实际身份由HTTP/入站集成用例另验。

## 删除与改写旧测试

删除两项旧case，净增加98项：

- `test_shared_inbound_media.py` 删除 `reset` 后必须停止表情收藏的参数及分支。它冻结F12的错误门槛，改由真实 `/ai new` 入站合同回归替代；delete/edit/unindex三个原来源控制保留。
- `test_vision_retry_configuration.py` 删除仅反射两个退休配置字段不存在的测试；实际HTTP失败只请求一次、thinking协议选择继续验证。

另外整理历史增量测试：删除无消费者的 `captured.yuki_account_ids` 推断断言，保原位提及、回复、冻结capture、补洞与overlay的实际字节；把“只构造一次消息”改为复制总量随输出线性增长，允许实现改用其他构造方式。相应测试重命名，避免继续承诺已无消费的前驱/Bot推断语义。

[Compact缓存驱逐测试](../../tests/unit/test_compact_checkpoint_regressions.py)也去掉重复准备：4字节预算由第三份空projection触发驱逐，不再为该参数构造257份；无字节预算的默认128份容量测试仍保留257份输入。补上公开读取的实际 `capacity` 驱逐断言，再检查已付费checkpoint、coverage和raw tail，不仅凭checkpoint幸存就声称驱逐路径已验。四个参数数量不变，最终版本Linux定向4 passed。

[HTTP容量测试](../../tests/unit/test_web_http_transport.py)纠正两段超时混用：前半仅pool等待保留50毫秒，连接/读写采用正常1秒；后半验证client关闭后原排队请求得到closed，移除与1秒pool计时器的竞争。10秒看门狗仅约束测试等待排队结果，不宣称能强制截断有资源所有权的cleanup。原完整组的失败堆栈确认为后半PoolTimeout与closed不符。另用一次性受控探针延迟实际pool关闭1.1秒，旧测试重现相同失败，最终版本通过；永久测试不依赖该探针，没有新增重试或xfail。

没有为了凑删除数量移除有用途的升级reader、原claim/hash、真实授权、跨场景、已知/未知回执和历史容量测试。九个旧身份helper及无效Session字段的产品链尚未删除，本轮不冒称已完成这些源码清理。

## 数据库测试准备

[共享夹具](../../tests/conftest.py)原来每例重新创建相同schema和固定seed。在同一Linux环境的两次诊断中，重复准备耗时约1.55–1.70秒；关闭的同一初始库复制后建立新连接约26–29毫秒。这是准备阶段测量，不代表整套测试或生产数据库的加速比例。

现在每次pytest运行在session专用loop内真实建库、seed一次，checkpoint并关闭所有连接后把模板设为只读。每个测试仍复制到独立临时目录、创建新Database/engine/连接，身份写设置也仍逐例复原；不缓存到下次运行，不共享可写库或跨loop连接。显式测试初始化和迁移的用例仍自行真实建库。

新增[三个隔离回归](../../tests/unit/test_database_fixture_isolation.py)，核对完整sqlite_master、模板与副本互不污染、设置复原、实际外键拒绝和正常写入，以及chat/memory两套FTS触发器的增改删。它们不冻结表、索引或触发器数量。Windows三项通过，Linux相关组合33 passed/2 known xfailed。

## CI 的实际调整

[Quality工作流](../../.github/workflows/quality.yml)原来虽安装all-extras，但未构建/安装原生worker和binding，相应CodeMode测试被跳过。现在给实际native项显式加 `native_worker` 标记，收集结果为 **90项**：

- direct单元/集成两组运行 `-m "not native_worker" --strict-markers`，保存JUnit；正常本地缺worker时原有事实性skip仍保留。
- 独立native job构建现有固定Monty发行，安装真实binding和root-owned worker/launcher；加载仓库现有AppArmor策略，再执行实际隔离verifier，最后运行90项native测试。没有关闭Host sysctl、使用unconfined、无约束worker或失败后跳过的替代路径。
- native verifier失败就停止该job；不会把镜像/编译成功当作执行与隔离验收。wheel安装后不再 `uv sync`，避免被锁文件同步移除。
- 两组与native的JUnit/隔离结果作为CI artifact保留。各job日志仍区分正常通过、已知xfail、实际失败与未执行。

支持模块中无消费的 `pytestmark = requires_worker` 也已删除；native分类放在真正收集的测试/参数上，不靠导入helper暗示已标记。

## 验证与交付状态

- 新增100项按分区定向执行：49项正常控制通过、51项严格xfail；51项全部单独关闭xfail核到真实失败。A/B/C互相及主审交叉核测试目标、原件/权限控制和删除后果；新夹具再经两位审查者独立复核。
- 既有相邻回归：A合并组55 passed/13 xfailed，B相邻37 passed，C合并组40 passed/15 xfailed且无skip，主审历史/正文等组19 passed/7 xfailed。这些是重叠的定向记录，不相加冒充独立总数。
- 隔离Linux容器缺Docker Compose CLI，对应16项配置解析用例在Windows宿主的真实CLI下另跑，16 passed；没有启动业务容器，也不把该宿主结果计入Linux通过数。
- Linux完整单元组中的三项打包用例遇到Windows工作树 `.git` 绝对路径不可读，原环境失败日志保留。用同一基线的真实浅克隆提供有效Git元数据和索引，仍读取 `/repo` 的现有源码后，打包模块Linux复验4 passed；没有修改断言、加skip或改生产脚本。Windows真实工作树的打包/安装器组合另有14 passed，包含容器缺PowerShell而跳过的四项；这些重叠结果分别记录。
- 全仓Ruff、`git diff --check`、Quality YAML/actionlint及native/known-defect收集已通过。最终收集为3851项，其中native90项、已知xfail51项。
- 新夹具下的Linux完整direct集成组：704 passed、9 known xfailed、90 native deselected，无非预期失败；耗时1229.94秒。
- Linux完整direct单元组的原始结果：2982 passed、42 known xfailed、20 skipped、4 failed；耗时2356.89秒。三项失败为上述Git挂载环境，已在有效Git元数据下Linux复验4 passed；另一项为上述HTTP测试竞争，最终模块32 passed，受控延迟对照也完成。20项skip全部是容器缺Compose或PowerShell，均有上述Windows真实CLI补验。不把原始完整组改写成全绿，也不相加重叠定向结果。
- 最后两处窄改（Compact四参数及HTTP模块）发生在完整组已加载原用例之后，分别使用最终文件定向复验；没有再跑一次无关完整组。更早、优化夹具前的首次完整组已中断，不算完成。全部失败日志保留。测试数据及pytest cache均在容器 `/tmp`，只有源码只读挂载，无生产挂载。
- 本地验证容器未安装Monty发行，未运行其AppArmor原生验收；新native工作流尚未在远端执行，不能声称90项原生测试或CI已通过。

仍未修产品缺口。本轮没有提交/PR/合并/部署；对应交付应在后续授权时另行记录。
