# Memory 当前检索合同

总合同见 [Memory](memory-v2.md)。主 Agent 通过 `search_memory` 主动搜索；普通轮次不自动注入
旧长期事实，不产生归因模型任务、激活状态、强化权重或零注入召回回执。

## 授权先于检索

真实请求者和目标选择器交给 `MemoryReadScopeResolver`。没有 target 时，在数据库层筛选本次主体
有权读取的 Person、PersonGroup、Group 和当前可见 SELF。历史 owner 没有活跃 QQ Binding 仍可检索。
人物、群名片、subject_ref 和群选择器不承担授权证明。多个冲突人物选择器明确拒绝。
原始私聊、完整 evidence、他人的 private SELF 继续使用各自真实权限边界。

## 查询与排序

FTS 和向量候选先在授权 SQL 范围内检索，再在共同候选池用原词法分数、向量相似度及既有 RRF
合并排序。原有模型 confidence 保留，但不另行构造 authority、Activation、IntentRanker 或 MMR 分数。
同 key 的独立事实均可返回；不把单值槽或争议分类当作读取条件。

query 和 entities 不按字符数截断；preferred_kinds 不限制固定数量。语义检索不按经验相似度阈值
提前丢掉候选。真实候选工作预算和返回数量预算继续生效，并明确报告 truncated/exhaustive 和原因。
Embedding 的来源资格、profile、向量维度和内容指纹仍核验，不能拿过期索引冒充完整覆盖。

用户指定的严格日期在 SQL 候选阶段应用，采用 start-inclusive/end-exclusive；不重复在应用层过滤，
不自动放宽日期。实际事件时间与事实更新时间不同，不能用 updated_at 声称事情发生日期。

## 结果含义

词法或向量相近只是候选，模型结合 content 和真实事件证据判断相关性；没有 topic_admission
校准准入投影，不把默认阈值当作语义验收。空结果只说明本次未匹配，不证明全库无记录。
返回 N 条或 truncated 不代表全部存档。权限拒绝、基础设施故障、向量降级分别报告。

Plugin/Admin 查询保持纯读取。主 Agent 实际读到的工具输出仍属于其真实请求与原 Work 历史；
不为无消费者的使用率评分另建 receipt。固定主工具合同和执行处权限核验保持。
