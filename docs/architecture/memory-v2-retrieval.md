# Memory 当前检索合同

总合同见 [Memory](memory-v2.md)。主 Agent 通过 `search_memory` 主动搜索；普通轮次不自动注入
旧长期事实，不产生归因模型任务、激活状态、强化权重或零注入召回回执。

## 授权先于检索

真实请求者和目标选择器交给 `MemoryReadScopeResolver`。没有 target 时，在数据库层筛选本次主体
有权读取的 Person、PersonGroup、Group 和当前可见 SELF。历史 owner 没有活跃平台 Binding 仍可检索。
人物、群名片、subject_ref 和群选择器不承担授权证明。多个冲突人物选择器明确拒绝。
原始私聊、完整 evidence、他人的 private SELF 继续使用各自真实权限边界。
已知主体沿调用链传内部 Person/Space；账号换绑不改写原事实与历史 membership 的 owner。

## 查询与排序

FTS 和向量候选先在授权 SQL 范围内检索，再在共同候选池用原词法分数、向量相似度及既有 RRF
合并排序。原有模型 confidence 保留，但不另行构造 authority、Activation、IntentRanker 或 MMR 分数。
同 key 的独立事实均可返回；不把单值槽或争议分类当作读取条件。

query 不按字符数截断；无实际消费者的 entities、preferred_kinds 参数已退出工具 schema 和调用链。
语义检索不按经验相似度阈值
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

每个新调用都重新读取当前事实与 SQL 授权，不以相同参数复用完整检索结果或 fact detail。
原 call 的持久工具回执用于恢复，不能拿它替代另一个新 call 的读取。
`QueryEmbeddingCache` 只按 Profile 指纹和查询文本复用向量；命中后仍在当前授权范围召回和排序，
所以新增事实、修正和历史关系变化无需等待完整结果缓存过期。Embedding 请求、缓存命中、失败和
真实已知 usage 分别计量；缺字段保持未知，不能据此推断 Main 模型缓存。

记忆的使用规则放在主 Agent 与工作者各自的固定提示词中，一条链只提供一次。
新工具结果只返回实际事实、主体、引用和证据状态，不随每次查询重复整段规则。
历史已保存的工具回执仍由原 decoder 读取；不改旧回执或已付费的私有请求。
