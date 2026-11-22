# Memory 当前检索合同

总合同见 [Memory](memory-v2.md)。主 Agent 通过 `search_memory` 主动搜索。普通轮次不按相似度注入
旧长期事实，不产生归因模型任务、激活状态、强化权重或召回回执；只在 system 消息末尾确定性渲染
常驻记忆段（本人的明确事实、群的核心集、人物画像、所有者确认的 SELF 原则）与经历目录（本会话可见经历的
日期、标题与 fact 编号，水位之后的经历放在当前消息里），规则见
[常驻记忆段](memory-v2.md#常驻记忆段)与[经历目录](memory-v2.md#经历目录)。

## 授权先于检索

真实请求者和目标选择器交给 `MemoryReadScopeResolver`。没有 target 时，在数据库层筛选本次主体
有权读取的 Person、PersonGroup、Group 和当前可见 SELF。历史 owner 没有活跃平台 Binding 仍可检索。
模型工具的 target 只接受单键 `{person_id}`、`{space_id}` 或 `{"self": true}`，按内部 ID 查找，
不承担授权证明；名称选择器已从模型工具删除。`since`／`until` 须带时区，until 不含。
原始私聊、本会话不可见的 evidence、他人的 private SELF 继续使用各自真实权限边界。
已知主体沿调用链传内部 Person/Space；账号换绑不改写原事实与历史 membership 的 owner。
同一人归并集合用于本人、subject、private SELF 和共同群 SQL 判断，无 Binding 的来源人物也纳入；
同 Space 与同人名称命中去重。结果按当前 root 组织人物块，原 fact/evidence owner 和来源 ID 保持原件。

## 查询与排序

FTS 和向量候选先在授权 SQL 范围内检索，再在共同候选池用原词法分数、向量相似度及既有 RRF
合并排序。原有模型 confidence 保留，但不另行构造 authority、Activation、IntentRanker 或 MMR 分数。
同 key 的独立事实均可返回；不把单值槽或争议分类当作读取条件。

query 不按字符数截断；无实际消费者的 entities、preferred_kinds 参数已退出工具 schema 和调用链。
省略 query（且不给 fact_id）时，在同一 target 解析与 SQL 授权范围内按既有 overview 顺序
（importance、confidence、updated_at 降序）列出有效事实，不调用 Embedding；`since`／`until`、
`limit` 与 truncated/exhaustive 含义同搜索，本工具没有游标。
语义检索不按经验相似度阈值
提前丢掉候选。真实候选工作预算和返回数量预算继续生效，并明确报告 truncated/exhaustive 和原因。
Embedding 的来源资格、profile、向量维度和候选内容指纹仍核验，不能拿过期索引冒充完整覆盖。
Vec1 在同一 SQLite 读取快照内按 metadata fact_id IN 接收原授权子查询，然后只搬运候选值
做原 codec 精排，不先取全库固定 top-N 再删除无权限项。原生候选有限时以
`semantic_approximate=true` 和 `exhaustive=false` 说明，未召回项的 hash 新鲜度保持未知。
近似性和普通结果截断不等于 `partial_failure`；缺失覆盖、已发现的 stale 候选与实际降级才是
检索的部分失败，不能因精排成功抹去这些事实。

用户指定的严格日期在 SQL 候选阶段应用，采用 start-inclusive/end-exclusive；不重复在应用层过滤，
不自动放宽日期。实际事件时间与事实更新时间不同，不能用 updated_at 声称事情发生日期。

## 按编号读取的证据

`search_memory(fact_id=…)` 先按上面的同一 SQL 授权读取事实，授权不变；读到后结果另附
`evidence`：本会话可见的证据，每条 `{event_id, excerpt}`（存储的证据摘录），按 event_id 升序；
存在但这里不可见的证据只给 `evidence_hidden: N`（为 0 时省略）。没有证据行的事实 `evidence` 为空列表。
其他检索结果不带证据；工具声明不变。

可见范围按当前会话（`canonical_conversations`，私聊对方取人物根），与常驻层共用
`memory/resident.py` 的 `evidence_origins` 和逐条判定 `evidence_shown_in`：

| 证据 | 群会话 | 与 P 的私聊 |
|---|---|---|
| 任一群里的聊天事件（所有群视作一个公共空间） | 显示 | 显示 |
| P 本人私聊里的聊天事件（含并入 P 的旧人物的私聊） | 隐藏 | 显示 |
| 别人私聊里的聊天事件、无法追溯会话的证据 | 隐藏 | 隐藏 |
| 工具回执证据（无论触发事件在哪） | 隐藏 | 隐藏 |

证据事件被删除时证据行随之级联删除，不再显示或计数。模型要更多上下文时用
`read_chat(around=event_id)`，其授权不改；别的群的事件 ID 交到当前群后可读到那个群的邻文，
这是“所有群视作一个公共空间”的直接后果。依据规则写明常驻条目与画像后的 fact 编号可这样回查。

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
