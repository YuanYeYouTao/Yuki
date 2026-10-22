# SQLite writer 实施记录：Memory（B）

基线 main `c2ce7da66ab54efe192993b9615df48266a62137`，任务书 `4bffe24f587901a979da0cf86cb0847a1cdb98ff`。
工作分支 `codex/sqlite-writer-runtime-20261023`。本文记录本地实现和测试，不代表提交、合并或生产验收。
产品仍按现行 development-contract；没有调用真实模型、操作生产库或发送平台消息。

## 实现边界

所有 Memory 自有主库读取改用 `read_session()`，写入改用公共 `immediate_session()`；外部供给 session 原样复用。
需要一致视图的准备显式只读 BEGIN，关闭 reader 后才申请 writer。模型、HTTP、历史解码/证据渲染、输入指纹和 embedding 编码在读阶段或 DB 外。

`PreparedMemoryEvidence` 保存本次实际证据、复制目标及两跳关系依赖。提交先核原 evidence 成员/字段与当前来源资格，再核复制来源与目标共同资格，以及原关系拓扑的成员；不拿全库 revision、数量或最高 ID 代替这些依赖。它是一次提交的领域材料，不持久化，也不建立另一个队列、journal 或重试 runtime。
目标描述在 reader 解析到 canonical owner，writer 按原实际映射复核；当前权限检查和事实操作共用 writer。已固定的 selector 保持原 fact ID，后来出现的独立同 key 事实不改变原选择。

Memory mutation 保原 mutation ID、事实/evidence/relation ID、原队列 claim 和实际回执。逐 live claim、逐自省 proposal/episode 原子提交。job 完成只核原 claim 并写原行，未知确认读原持久事实，取消继续传播。

Dream 的整个 cluster 仍是一个业务原子单元。模型消费的输入先在 reader 核原指纹，复制 evidence/bundle 在 reader 准备；writer 核对应实际来源与原 fact/partition。已付 preview 消费必须仍为 ready。rollback 按单个 operation 准备原 source/result、added IDs 与需要恢复的元数据，再核当前 source/result、外属 ID 与后续依赖缺席；保原 operation 和 mutation receipt。
实际新 operation/result 的 SQLite ID、当前时间参与最终签名，少量签名拼装/hash 必须留在提交内；没有为宣称零 JSON 增加片段模板或通用缓存机制。

Rebuild 每个 source 的 staging 仍一次提交全部 proposals+item；采用时每 proposal 的 fact/evidence/state 与原 proposal receipt 同提交。读取阶段做正文解码、来源 hash、主体补全和校验；writer 核原 run selection、proposal/item 实际载荷、源字段、当前 selection 资格与 live job 缺席。历史提及按原 ingress 时段归属，今天 membership/换绑不取代原 Person。最终 run 状态按 writer 内当前待处理项选择。receipt sweep 保每页原子，隐私忘记在 writer 核真实相关集合及 selection/original-selection 原件，包含新匹配行。

维护先读当前到期候选和完整证据，writer 用当前 expiry 条件处理；两个跳数关系用于冲突清除。Hygiene 在 reader 渲染/规范化来源，writer 核本页证据成员及来源字段，再核 tool provenance，状态与事件同页提交。显式 FTS rebuild 保完整 SQLite 维护命令。

## 删除与保留

删除 `MemoryFactRepository.transaction`、`apply_evidence_write`、`FactService.prepare_evidence_write`、Memory 的第二套 BUSY 重试循环、首写 SQL listener/flag；删除 job claim 的 no-op UPDATE 与 Dream `WHERE false` 抢锁 UPDATE。
只读路径不再自动 commit。模型请求数、付费 checkpoint、原 claim/lease、真实 source/result/operation/receipt、授权与隐私边界保留。

## W/V 对照

| 任务 | 实际落点 | 验证 |
| --- | --- | --- |
| W42 | fact/job/relation/targets、自有 read/write、preference SQL 精确 key | Memory V2、independent facts、job claims、partition、writer boundaries |
| W43–W45 | mutation reader source proof、evidence/copy 当前复核、same-session helpers | mutation、snapshot owner、并发 resolution、证据 batch/retirement；V03/V05/V17/V21/V46 |
| W46 | 删除旧 mixed transaction、retry、首写 flag 与空 claim UPDATE | 静态扫描无旧入口；真实排队/取消/未知确认测试 |
| W47–W48 | Self repository 由 root、initiative 由 A 共同实施 | 见 root/A 实施记录及 Self 专门验证；不在本报告冒领 |
| W49 | 原 quiet 分支 Self service/worker/control 与配置合流；C 合流模型/前台活动 | self_reflection_priority、background_quiet_priority、cycle_enqueue；V13/V24/V47 |
| W50–W51 | Dream whole cluster、新 prepared rollback、preview 当前状态 | Dream 37 项、输入源交换、paid 排队/取消、第二 action 失败整簇回滚；V46/V48 |
| W52–W53 | embedding reader/hash/HTTP/encode 保锁外，现有短 CAS 统一入口 | embedding claim/partial/manual、Memory retrieval/query cost；V07/V20/V37/V49 |
| W54–W55 | rebuild proposal/selection/source/live absence、当前 finalization、privacy phantom | Rebuild/source post-prepare 四窗口、privacy preparation、receipt finalization；V17/V50 |
| W56 | maintenance、quality、FTS 独立维护 | maintenance query cost、源快照、hygiene reason；V29/V46 |

## 实测

- 最终完整 B unit 范围 41 个文件：328 passed（105.54 秒）；初轮 321 passed / 3 failed。三处为旧观察点：恢复测试等待实际 BEGIN、两个已退休 retry_due 的测试消费者；已迁到 FIFO 准入前观测并删旧消费者。
- 定向原 paid/job/person、mutation 与 writer 组合 53 passed；Dream 普通 37 passed；新的 owner/源交换/unknown ack 19 passed；Dream 第二 action 失败与 paid 等待 6 passed；maintenance 12 passed。
- 新 Rebuild source/selection/proposal/live-job 在准备后变化四窗口，连同对应回归 26 passed；恢复原载荷后沿原 proposal、原一次模型结果继续提交。
- quiet/control 19 passed：等待不占 writer、close 取消等待、抢占退出、固定时段、原付费位置、manual 来源准备后删除/改 identity、预算准入跨日。
- 固定 selector ID 后新增同 key 事实、独立并发准备 3 passed。
- `ruff check` Memory 产品通过；`mypy src/yuki/memory` 已通过（仅 pyproject 原 unused-section 提示）。
- 最后一处 enqueue 改为 writer 内按当前事件资格和原 identity 核验、不加载正文，定向 21 passed；避免将不相关派生正文变化作为 queue 拒绝条件。
- 三轮实现后交叉审查继续由主任务汇总；这里不把尚未完成的复审写成已验收。

旧 BUSY_SNAPSHOT 专属注入已迁成真实 reader 准备后、writer admission 前的交错。未知提交测试仍核实际原回执；不在已持共享 writer 的 hook 中同步等待另一个 writer。

## 逐文件范围

以下是本轮 87 个 Memory 产品路径的改动/保留清单。未改动的纯领域算法、DTO 和委托不冒充本轮逐行全文重写；前三轮源码审查证据保留在日期审查目录。root/A 共同实施文件的结果以其记录为准。

| 文件（相对 src/yuki/memory） | 状态 |
| --- | --- |
| `__init__.py` | 保留：无本轮产品改动；既有领域算法/类型/只读委托，未改成 writer |
| `audit.py` | 已迁移本文件的 DB 入口/边界；对应定向测试见上 |
| `authorized_scope.py` | 保留：无本轮产品改动；既有领域算法/类型/只读委托，未改成 writer |
| `claim_processor.py` | 保留：无本轮产品改动；既有领域算法/类型/只读委托，未改成 writer |
| `context.py` | 保留：无本轮产品改动；既有领域算法/类型/只读委托，未改成 writer |
| `dream/__init__.py` | 保留：无本轮产品改动；既有领域算法/类型/只读委托，未改成 writer |
| `dream/db_models.py` | 保留：无本轮产品改动；既有领域算法/类型/只读委托，未改成 writer |
| `dream/models.py` | 保留：无本轮产品改动；既有领域算法/类型/只读委托，未改成 writer |
| `dream/planning.py` | 保留：无本轮产品改动；既有领域算法/类型/只读委托，未改成 writer |
| `dream/repository.py` | 已迁移本文件的 DB 入口/边界；对应定向测试见上 |
| `dream/service.py` | 已迁移本文件的 DB 入口/边界；对应定向测试见上 |
| `dream/worker.py` | 已迁移本文件的 DB 入口/边界；对应定向测试见上 |
| `eligibility.py` | 保留：无本轮产品改动；既有领域算法/类型/只读委托，未改成 writer |
| `embedding/__init__.py` | 保留：无本轮产品改动；既有领域算法/类型/只读委托，未改成 writer |
| `embedding/codec.py` | 保留：无本轮产品改动；既有领域算法/类型/只读委托，未改成 writer |
| `embedding/fake.py` | 保留：无本轮产品改动；既有领域算法/类型/只读委托，未改成 writer |
| `embedding/health.py` | 保留：无本轮产品改动；既有领域算法/类型/只读委托，未改成 writer |
| `embedding/jobs.py` | 已迁移本文件的 DB 入口/边界；对应定向测试见上 |
| `embedding/metrics.py` | 保留：无本轮产品改动；既有领域算法/类型/只读委托，未改成 writer |
| `embedding/models.py` | 保留：无本轮产品改动；既有领域算法/类型/只读委托，未改成 writer |
| `embedding/provider.py` | 保留：无本轮产品改动；既有领域算法/类型/只读委托，未改成 writer |
| `embedding/query_cache.py` | 保留：无本轮产品改动；既有领域算法/类型/只读委托，未改成 writer |
| `embedding/qwen.py` | 保留：无本轮产品改动；既有领域算法/类型/只读委托，未改成 writer |
| `embedding/repository.py` | 已迁移本文件的 DB 入口/边界；对应定向测试见上 |
| `embedding/runtime.py` | 保留：无本轮产品改动；既有领域算法/类型/只读委托，未改成 writer |
| `embedding/semantic.py` | 保留：无本轮产品改动；既有领域算法/类型/只读委托，未改成 writer |
| `embedding/text.py` | 保留：无本轮产品改动；既有领域算法/类型/只读委托，未改成 writer |
| `embedding/worker.py` | 保留：无本轮产品改动；既有领域算法/类型/只读委托，未改成 writer |
| `enums.py` | 保留：无本轮产品改动；既有领域算法/类型/只读委托，未改成 writer |
| `errors.py` | 保留：无本轮产品改动；既有领域算法/类型/只读委托，未改成 writer |
| `event_extractor.py` | 保留：无本轮产品改动；既有领域算法/类型/只读委托，未改成 writer |
| `extraction.py` | 保留：无本轮产品改动；既有领域算法/类型/只读委托，未改成 writer |
| `fts.py` | 已迁移本文件的 DB 入口/边界；对应定向测试见上 |
| `job_claims.py` | 已迁移本文件的 DB 入口/边界；对应定向测试见上 |
| `maintenance.py` | 已迁移本文件的 DB 入口/边界；对应定向测试见上 |
| `metrics.py` | 保留：无本轮产品改动；既有领域算法/类型/只读委托，未改成 writer |
| `models.py` | 保留：无本轮产品改动；既有领域算法/类型/只读委托，未改成 writer |
| `mutation/__init__.py` | 保留：无本轮产品改动；既有领域算法/类型/只读委托，未改成 writer |
| `mutation/models.py` | 保留：无本轮产品改动；既有领域算法/类型/只读委托，未改成 writer |
| `mutation/repository.py` | 已迁移本文件的 DB 入口/边界；对应定向测试见上 |
| `mutation/service.py` | 已迁移本文件的 DB 入口/边界；对应定向测试见上 |
| `partition.py` | 保留：无本轮产品改动；既有领域算法/类型/只读委托，未改成 writer |
| `projections.py` | 保留：无本轮产品改动；既有领域算法/类型/只读委托，未改成 writer |
| `quality/__init__.py` | 保留：无本轮产品改动；既有领域算法/类型/只读委托，未改成 writer |
| `quality/audit.py` | 已迁移本文件的 DB 入口/边界；对应定向测试见上 |
| `quality/event_evidence.py` | 保留：无本轮产品改动；既有领域算法/类型/只读委托，未改成 writer |
| `quality/hygiene.py` | 已迁移本文件的 DB 入口/边界；对应定向测试见上 |
| `quality/models.py` | 保留：无本轮产品改动；既有领域算法/类型/只读委托，未改成 writer |
| `query.py` | 保留：无本轮产品改动；既有领域算法/类型/只读委托，未改成 writer |
| `ranking.py` | 保留：无本轮产品改动；既有领域算法/类型/只读委托，未改成 writer |
| `read_scope.py` | 已迁移本文件的 DB 入口/边界；对应定向测试见上 |
| `rebuild/__init__.py` | 保留：无本轮产品改动；既有领域算法/类型/只读委托，未改成 writer |
| `rebuild/models.py` | 保留：无本轮产品改动；既有领域算法/类型/只读委托，未改成 writer |
| `rebuild/privacy.py` | 已迁移本文件的 DB 入口/边界；对应定向测试见上 |
| `rebuild/receipt_finalization.py` | 已迁移本文件的 DB 入口/边界；对应定向测试见上 |
| `rebuild/repository.py` | 已迁移本文件的 DB 入口/边界；对应定向测试见上 |
| `rebuild/service.py` | 已迁移本文件的 DB 入口/边界；对应定向测试见上 |
| `rebuild/trusted_sources.py` | 保留：无本轮产品改动；既有领域算法/类型/只读委托，未改成 writer |
| `rebuild/worker.py` | 保留：无本轮产品改动；既有领域算法/类型/只读委托，未改成 writer |
| `repository.py` | 已迁移本文件的 DB 入口/边界；对应定向测试见上 |
| `retrieval.py` | 已迁移本文件的 DB 入口/边界；对应定向测试见上 |
| `runtime/__init__.py` | 保留：无本轮产品改动；既有领域算法/类型/只读委托，未改成 writer |
| `runtime/capability_view.py` | 保留：无本轮产品改动；既有领域算法/类型/只读委托，未改成 writer |
| `runtime/contract.py` | 保留：无本轮产品改动；既有领域算法/类型/只读委托，未改成 writer |
| `runtime/partition_lookup.py` | 已迁移本文件的 DB 入口/边界；对应定向测试见上 |
| `runtime/query_plane.py` | 保留：无本轮产品改动；既有领域算法/类型/只读委托，未改成 writer |
| `runtime/resolver.py` | 保留：无本轮产品改动；既有领域算法/类型/只读委托，未改成 writer |
| `runtime/turn_session.py` | 保留：无本轮产品改动；既有领域算法/类型/只读委托，未改成 writer |
| `self_origin.py` | 保留：无本轮产品改动；既有领域算法/类型/只读委托，未改成 writer |
| `self_reflection/__init__.py` | 保留：无本轮产品改动；既有领域算法/类型/只读委托，未改成 writer |
| `self_reflection/control.py` | 已迁移本文件的 DB 入口/边界；对应定向测试见上 |
| `self_reflection/db_models.py` | 保留：无本轮产品改动；既有领域算法/类型/只读委托，未改成 writer |
| `self_reflection/initiative.py` | 共同实施：root/A 负责真实 scan/claim/advance/恢复；本报告不代替其验证 |
| `self_reflection/models.py` | 保留：无本轮产品改动；既有领域算法/类型/只读委托，未改成 writer |
| `self_reflection/reporting.py` | 已迁移本文件的 DB 入口/边界；对应定向测试见上 |
| `self_reflection/repository.py` | 共同实施：root/A 负责真实 scan/claim/advance/恢复；本报告不代替其验证 |
| `self_reflection/service.py` | 已迁移本文件的 DB 入口/边界；对应定向测试见上 |
| `self_reflection/worker.py` | 已迁移本文件的 DB 入口/边界；对应定向测试见上 |
| `service.py` | 已迁移本文件的 DB 入口/边界；对应定向测试见上 |
| `subjects.py` | 已迁移本文件的 DB 入口/边界；对应定向测试见上 |
| `targets.py` | 已迁移本文件的 DB 入口/边界；对应定向测试见上 |
| `temporal.py` | 保留：无本轮产品改动；既有领域算法/类型/只读委托，未改成 writer |
| `temporal_filter.py` | 保留：无本轮产品改动；既有领域算法/类型/只读委托，未改成 writer |
| `testing.py` | 保留：无本轮产品改动；既有领域算法/类型/只读委托，未改成 writer |
| `tool_intent.py` | 保留：无本轮产品改动；既有领域算法/类型/只读委托，未改成 writer |
| `validation.py` | 保留：无本轮产品改动；既有领域算法/类型/只读委托，未改成 writer |
| `worker.py` | 保留：无本轮产品改动；既有领域算法/类型/只读委托，未改成 writer |
