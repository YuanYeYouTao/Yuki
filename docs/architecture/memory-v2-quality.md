# Memory 来源审计与显式治理

记忆收录、纠正和检索遵循 [Memory](memory-v2.md)、[修改入口](memory-change.md)及真实来源和权限。模型声明的 importance、confidence、来源类型保留，不以固定分数、风格、长度或版本化合成样本裁决内容。

旧 synthetic suite、冻结 baseline、质量门禁与 release-check 已退休。生产来源审计保留：`memory audit` 只读取数据库，报告问题类型、数量和有限内部行 ID，不输出正文，也不自动修改事实。相同 key 可对应多个独立事实，不再将它们报为单值槽冲突。

历史 superseded 缺链与异步过期待处理保留诊断计数，不否决当前整体健康。正常争议、待审核、
终态暂存和旧 embedding profile 不凭状态存在判故障；事实与每条证据保留各自原始 authority。

`memory hygiene scan` 提供派生数据问题清单，显式 apply 复核原指纹、授权和来源；FTS 全量重建仍是独立维护操作。写入前冻结证据和 owner，事务内不等待模型或扫描历史，已提交效果按原回执恢复。

来源审计与写入使用相同的证据文本语义：普通真人来源采用原事件的 evidence_content，
包括已提交的 ASR；自省来源采用对应的实际事件渲染。不能因审计只读取旧 content 而把有效语音事实误报为失效。
事实、原证据和历史关系沿内部 Person/Space/Conversation 核验，当前平台账号换绑不重新定义旧 owner。

永久证据抽样删源任务已退役，原 compaction 表与回执只作为历史诊断保留。
模型输入摘录、诊断样例与持久证据是不同范围，不能用样例数量代替完整来源核验。
新调用的完整 Memory 结果不缓存，纯 query embedding 向量复用后仍查询当前 SQL 授权和事实。

验证以真实 SQL 原子性、隐私和来源、请求协议、累计预算及原回执恢复为主。测试和 CI 不冻结工具数量、提示词措辞或经验策略。诊断不证明记忆的语义质量，healthz 不等于自然聊天验收。

操作入口见 [运维](../operations/memory-quality.md)。
