# R2 补充：Memory fact 的证据计数

这是全域 R2 C 收束后的独立专项，归 root 的 R2；不算 R3。起点仍为 `f8fbe14d`，在共享工作树上接续本轮修复。只修改 `src/yuki/memory/repository.py` 和新的实际回归，没有修改 FactService、SELF repository 或 self_origin。

## 调用链与问题

`MemoryFactRepository._execute_facts_with_count` 服务于 get_fact/get_facts、目标内选择、mutation locator 和普通 fact 列表。FactService 的 confirm/contest/version/restore/管理确认等实际 writer 消费者也通过这些投影读取 `evidence_count`。prepared writer 已有完整证据计数，但当前选择涉及未在原准备集合中的 fact 时，旧分支调用 prepare_evidence_rows，加载完整 MemoryEvidence ORM 行并转换每条 DTO，只为得到一个整数。

这个分支不能用“已验证”标记或缺项时假设 0 取代。现在保已有实际准备的 counts；缺项按同一个 readable_evidence_predicate 分 256 个 fact ID 执行 SQL GROUP BY，只返回 fact_id/count。无可读来源的组计 0，不将完整正文或历史转换到 Python。事务内新 evidence 仍沿现有 additions/counts 更新，后续投影不重复扫描已知计数。

`list_evidence` 的准备分支原来还会隐式调用 prepare_evidence_rows。完整证据真正的 writer 消费者是 MemoryMutationService 的 Dream bundle；DreamService 在 reader 的 prepare_write/prepare_dream_evidence 已准备原 sources、anchor copy 和 recompose outputs。`DreamService._input` 及历史 fingerprint 计算均在 reader，显式准备所用事实。新 fact 由 create_fact 建立空证据集合，add_evidence/copy_prepared_evidence 维护本事务新增项。

因此删除 list_evidence 准备分支的隐式补扫，直接读取真实 prepared rows 与现有 additions。没有 assert、默认空列表、validated 位、新框架或 reader 绕入 writer。普通 owned reader 仍查询实际证据；原 created_at/id 倒序、limit 与完整 source proof 保留。

MemoryAudit、AgentTool、MemoryAdmin 的普通证据读取已追到 reader；它们不需要伪造 prepared writer。两个完整准备入口和各 Dream bundle 使用点已核，没有实际 writer 消费者仅靠“缺项 fallback”取得正文。

## 实际验证

- 新增 `test_memory_count_projection.py` 4 项：301 个实际事实跨页投影、duplicate 来源不计数、writer 追加证据后 count 增加、prepared 原顺序/limit/本事务新增项，以及 SELF 原 receipt 正文清退后计数变 0。记录实际 `_evidence_record` 调用，writer 的缺项计数没有创建 Evidence DTO。
- 连同 evidence batch、Memory V2、mutation、Dream、Dream plan/input/busy/recovery，9 个文件 **101 passed / 2 warnings，35.28 秒**。这覆盖已有完整合成、复制、来源变化与 paid 恢复语义，不将新单测单独当作全链验证。
- repository 单文件 mypy 独立 cache 通过，产品和新测试 Ruff/format、git diff --check 通过。
- 没有运行真实模型或平台消息；测试使用原离线 fixture。未单独做耗时阈值或宣称线上提速，正式同负载性能对照由 root 统一运行。

全域 R2 C 的 D110 逐文件覆盖、公共取消与 owner 修复另见 [R2 C 报告](sqlite-writer-review-r2-c-20261023.md)。本专项未开始 R3，也未提交或推送。
