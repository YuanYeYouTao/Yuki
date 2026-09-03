# Memory 当前检索合同

适用于 canonical 3.8.1 / schema 0051。总合同见 [Memory 架构](memory-v2.md)；
P1 不以降低注入数或保证固定缓存命中率为验收目标。

## 授权先于检索

真实请求者与目标选择器 → MemoryReadScopeResolver → ResolvedReadScope +
MemoryQueryIntent → MemoryQueryPlane → 现有检索与排序。

模型可以填写目的、实体、时间、种类与查询目标，不能声明权限。Person/Space 所有权由
Binding 解析到 canonical ID，SQL 在目标范围内筛选，再计算相似度。昵称、QQ/群号、
query vector、RRF 分数都不会扩大 scope。

本人 Person 可读；他人 Person 要有历史共同群；Group 要有请求者历史 membership；
PersonGroup 要双方在该群的历史 membership。Person 可包含私聊来源事实，不要求当前群
evidence；列表、相关搜索、overview 与 get_memory_fact 必须一致。
当前会话群、路由暂停、群停用和 Provider 切换不改变该关系；forget 后重新判断。
完整证据、原始私聊和他人 private SELF 不开放，写入政策不复用此 resolver。
SELF 维持 global/current-private/current-group 可见性。

## 目标与意图

自动预取从当前人物、当前群、真实提及/引用出发，不遍历所有历史群和群友；
维持 background/continuation 契约与最多两条的总预算，不新增前置模型调用。
主动工具由正常完整 Main Agent 提供 purpose、entities、preferred kinds、绝对时间范围；
后端解析好的目标用于排序，不能充当权限凭证。

人物、群与 SELF 的无 query 总览使用 overview；有 query 使用 relevant/lexical/hybrid。
主体分类不迁移、不复制事实。返回数量有界，空结果是正常成功，不是权限错误。
工具 schema 是部署级固定结构，不能用本轮昵称或群号改写。
不可重试的歧义/无权限与基础设施故障分开处理，不强制结束正常对话。

### 读取工具与选择器

- get_person_memories：subject_ref（真实 mention/reply 优先）、display_name 或兼容 user_id
  三选一；无群选择器时返回获准 Person 与相关 PersonGroup。可用 group_id 或 group_name
  限定共同群；这些参数不代替后端授权。
- get_group_memories：group_name 或 group_id；群聊省略目标默认当前群，私聊要求指定目标。
- get_self_memories：只查现有 global/current-private/current-group，不能指定他人的私聊。
- get_memory_fact：同一结构读取政策；get_memory_evidence 仍是更严格的证据接口。
- 名称须在获准历史关系内精确唯一；歧义最多返回五个候选和 has_more，retryable=false。
  不用全社会关系图作为每轮预取目标。
- 默认固定首轮读取工具包含 Person、Group、SELF；用户显式 pin 配置保持原值。
- 同轮相同已授权查询复用检索结果，减少数据库/embedding 工作；每次入口仍重验权限，
  不缓存永久许可。记忆修改清除本轮读缓存；权限拒绝不做自动重试，不新增读取次数配额。

## 检索核

- QueryBuilder 规范化文本、有界引用和结构化 intent；保留 FTS、短词 LIKE、
  embedding 与现有 rerank。
- 非空且启用语义检索时生成 query embedding；overview、lexical 不调用 embedding。
- 词法/语义候选都先按 canonical scope、active、有效期、kind/profile 做 SQL 筛选。
- RRF 融合各来源 rank，再按精确命中、authority、conflict、importance、confidence、
  updated_at/fact_id 等规则稳定排序。RRF/rank 不是跨模型的相关性概率。
- preferred kinds、时间和主体是排序信号，不能靠它们授予权限。
- active + contested conflict 可以带争议标记返回；superseded、invalidated、未采用的
  contested claim 不作为普通 active 事实。争议关系不跨 scope。
- 部分 embedding 覆盖仍由 FTS 补足；provider 故障退回词法，日志只记脱敏类别，
  不记录查询、事实、QQ、群号、向量或 provider 原始错误。

## 暴露、回执与统计

检索是纯读。只有真正进入 Main Agent 请求的结果才确认暴露；不是“检索到了”就算使用。
Plugin API 2.0 / 管理查询不写普通用户 recall 或 activation。
零注入的正常预取轮仍记 receipt，且不触发 attribution。
完成评估但未使用、尚未评估、失败、禁用、抢占/取消和队列满分别记录。
历史 used=false 不回填为确认无用。

Main Agent 使用现有 History、Rollup、Memory 和工具协议，不另建短上下文。
固定前缀与工具结构保持稳定；尾部召回内容仍可能变化，不能承诺固定缓存命中率。
本轮不新增冷却或抑制阈值，也不以强迫回复引用记忆提高使用率。

指标与排障见 [指标口径](memory-v2-quality-metrics.md)、
[质量运维](../operations/memory-quality.md)。旧 phase/Adaptive 文档不是当前权限合同。
