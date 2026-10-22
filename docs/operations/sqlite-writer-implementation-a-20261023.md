# SQLite writer 重构：A 域实施记录

基线：`c2ce7da66ab54efe192993b9615df48266a62137`。分支：`codex/sqlite-writer-runtime-20261023`。
范围对应[统一写入任务书](../architecture/Yuki-SQLite统一写入与短事务重构任务书-2026-10-22.md)。
本文是本地实现首轮记录，不代替后续三轮全域复审、全量检查、PR 或线上验收。
没有运行真实模型、生产或平台消息。

| 工作项 | 已实现的 A 范围 | 定向验证和当前状态 |
| --- | --- | --- |
| W11–12 | `ingress` 已有身份/Conversation/alias 走显式 reader 快照；缺失目标才进 writer，重新核身份/路由及不存在条件。原外部探针在 reader 归还后执行。 | `identity_preparation_boundary`、`duplicate_receipt_reads` 已通过；原 WAL 升级竞争测试改为新 reader 退出后并发变更/新 writer 复核。 |
| W13–14 | `canonical_uow` 预备原 UUID、正文/mention、分词、指纹与 transient event；观察闭包在 writer 重绑当前行。`scoped_event_uow.prepare_append` 提前编码正文/外部 payload，supplied writer 显式带准备，C 的 4 处生产组合调用已接入。原 receipt、event、媒体、FTS、水位仍同笔。 | ingress+config reader 一组 28 passed；duplicate/derived/media-cleanup/projection 一组 49 passed；另组人物/projection/ingress 59 passed。媒体/客户端最后批次 26 passed 后遇到 3 项失败，后续定位和复验见文末。 |
| W15 | merge/forget reader 准备，当前 writer 复核实际相关集合；删 A 全部 `apply_snapshot_write`。三处账号/人物/route closure 不持旧 reader ORM 或 session。 | privacy 原流程 9 passed；ACP/人物/ordinary 混合组 88 passed，唯一 C regrant 准备时序问题由 C 修复并复测。新增 privacy 3 与 merge 3 个实际源/phantom/无关变化回归已通过。 |
| W16 | Config/Audit/控制回执用统一 owner；旧 rollback 的原审计和实际配置 move 谱系在 reader 解码，writer 只取成员/比较结果。现代 row_id 保原快路。 | `test_person_identity -k rollback` 10 passed，包含新增原审计变化、相关 move phantom、无关 move 不阻断 3 项。 |
| W17 | bootstrap、元资料、Character/SOUL 的短写进 owner；鉴权、SSE/目录保持 reader。相同/空群名保纯读返回。SOUL 文件工作仍在 writer 外。 | 客户端身份、Character/SOUL 与 API 曾在 59 passed 组覆盖；其中唯一失败是新增测试漏传 Plugin CAS row_id，已修并单独通过。 |
| W18–19 | ordinary 原 source/activation 认领同 writer；segments 比较用 SQL JSON，小列实际身份复核沿同 session。Projection 编码/冻结在外，当前 head/CAS、privacy、实际既有 selection payload 在 writer 复核。Autonomy source_guard 的 hash/JSON 在 reader，writer 核原语义字段和 mention 投影，忽略无关 segment 变化。 | ordinary/projection 定向及混合组通过。Autonomy/SELF sources/lifecycle+metadata 一组 46 passed，唯一旧 517 断言已改新边界并在后续组通过。 |
| W22–23/W73 | MediaAnalysis、ConversationMedia、Uploads、job claims、退役 Web 清理统一短写；原文件下载/hash/rename/unlink 保在 writer 外，媒体锁在 writer 之前。无候选 media/web 清理保纯读。ASR 原预分词/精确 source CAS 保留。 | derived/media-cleanup 组通过；最后媒体/客户端组 26 passed，preview 文件短 basetemp 独立复验 8 passed，未改产品路径规则。 |
| W24 | HTTP 错误映射用 owner 的 `write_failure_retryable`，不凭 SQL 文本猜未提交；SDK `resolve_mutation_commit` 由 C 改为保显式 confirmed outcome。 | 原 pair lost COMMIT ACK、真实外部 BUSY 与一次性 code 已在客户端 API 组覆盖。 |
| W67–68 | Automation 三文件及相关验证已移交 root；A 只完成其 API 首次迁移。 | root 已报告首轮 71 passed；不重复计为 A 独立验证。 |
| W69 | reader/SSE 消费者改新 API；根 commit hint 的发布和取消语义由 root 实现。 | root 的公共 owner/hint 定向覆盖；A 现行文档已删 after_commit listener 说明。 |
| W60 | 独立资源不并入主库 writer；A 未改 ACP/outbox、workspace manifest、StickerIndex、BridgeState 的独立 owner。离线 FTS rebuild 保停止生产 writer 后的原同步事务与分页准备。 | 全局 fixture/离线脚本迁移与独立库组合验证由 root/C 汇总；A 新增 test-only merge seed helper，不留产品兼容层。 |

## 实现边界

`PersonForgetPreparation` 记录本次人物族、Assignment 历史、私聊、脱敏字段和分词，及真实的
相关 SQL selection proof。writer 对实际选择成员及原字段做 SQL 比较，覆盖 event/mentions、
tool/artifact/Work 复制来源、initiative sources/support、SELF 四类 map 与旧 rendered envelope、
审计 JSON 和 Sandbox resource closure。它不靠最大 ID、行数、时间戳或全库世代证明完整性。
Memory Rebuild 与 ResourceCleanup 的同 session 验证沿 B/C 的领域 API。

纯数据库来源变化可以重新准备；已经 close 的外部资源保原操作与回执，不扩大原关闭集合。
plain 快照后来出现 ACP 时，重备后按真实新资源报告未确认，不假装旧关闭已经覆盖。
同人物的无关昵称变化不会触发重备。罕见的整次隐私清退仍然原子；实际 JSON 选择扫描和
触发器成本需要在 W63 的大人物族样本记录，不凭空加索引或全局修订。

merge 的冲突按 Runtime key、Plugin 完整对象决定，核完整来源/目标集合与 alias absence；
只给将丢弃行的历史快照核全部原字段，目标已有对象的无关 value 更新不构成新门槛。
route 继承在当前 writer 从真实 Binding/Presence/Registry 候选重算，不复制旧 reader 实例。
旧 rollback 只选择原配置行 ID/版本的实际 moves，其他配置移动不阻止恢复。

## SELF initiative 追加实施

root 将 `memory/self_reflection/initiative.py` 交 A 收尾。receipt-only claim 已改为 reader
快照准备原来源、窗口、正文计数、指纹和 batch；批量读取 cursor/window/retry，正文流只读一次。
writer 核原 SELF 身份及实际 SpaceBinding 集合、cursor、原 window/retry 字段及完整原 receipt 输入。
查询返回标识/字段比较，不在 writer 做 hash 或正文解码。新窗口的 expiry 资格取准入后时间；
原 retry window 忽略新预算和后来回执，保原 run、checkpoint、预算及 fingerprint。原范围里来源
改变会重备或沿原语义隔离；范围外的迟到回执留给下一窗口，不造成无关重准备。

`tests/unit/test_self_initiative_memory.py` **37 passed**，包括 3 个新增实际边界测试：
hash 在 writer 前；选中 excerpt 改变后重备；准备后的迟到回执不被吸入当前窗口。
该源文件独立 mypy 无错误。root 同时修 ordinary 的 barrier 和跨 generation 隔离进度，不能把
37 项都归为 initiative 单文件的独立贡献。

## 删除与保留

- A 产品已无 public `sessions()`、`apply_snapshot_write` 调用；root 已删除旧 helper，正在删除最后 factory 属性。
- 删除旧 session closure，保原 ID、真正的 owner/authority/CAS、原 audit/receipt 与预算。
- 删除测试对旧 `connection_acquisition`/holder lower bound 口径的依赖，改 owner 实际 phase。
- 保 history read 的真实 driver interrupt/等待收尾；它不是旧 writer 观测 patch。
- 原日期审查报告保当时事实。现行 canonical/client/control/admin 合同已同步新边界。
- A 的 55 个测试文件按实际 AST context 分流：owned begin 为 writer，纯读为 reader；
  15 个原 setup helper 调用展开为明确 writer，没有新增产品或测试的 snapshot retry 兼容 API。
  `tests/support/event_seed.py` 的分词在外、同 session fixture 组合保留，owned seed 用统一 writer。

## 未完成的交付验证

最后媒体/客户端批次先有 26 passed / 3 failed。preview 是 Windows 260 字符路径问题，
`--basetemp D:/Temp/yuki-sqlite-a-preview` 下该文件 8 passed；未为环境问题改业务规则。
两次 cold runtime 的 group/qq 接续由 root 定位为外部注入 Database 被装配方错误关闭，
已按实际 owner 修复，root 报告该组 6 passed；不是 SQLite commit 失败或吞掉关闭状态。
另一混合客户端/人物组 93 passed / 2 failed：Rebuild 历史账号转移采用返回 0，已交 B；
Self complete 的 Result→dict 错误由 root 修正，Self 当前 37 passed。全分支状态由 root 的全量检查汇总。
A 114 源文件首轮 mypy 报告的类型问题已逐项修正，最后两文件复验中；Ruff 已通过。
首轮源码实施后仍需三轮全范围复审、
删 public factory/旧 helper 后的全量测试与性能对照，再由 root 推进提交、PR 和 CI。
