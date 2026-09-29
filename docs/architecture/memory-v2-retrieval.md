# Memory 当前检索合同

主动搜索的每条候选包含固定 `match` 投影：`lexical_match`、`semantic_candidate`、
`topic_admission=passed|not_passed|unknown`。准入采用该请求有效的已校准 profile 与主题阈值；
未校准/故障为 unknown，overview 不套主题门槛。字段只解释候选，不过滤主动结果，
passed 也不是语义真实性保证。尚未完成真实样本校准，不应以默认阈值替代验收。

`search_memory.target` 的群选择器只用于明确限定目标群，不用于提交权限证明；后端自行解析历史关系。
事件 mention/reply 优先使用 subject_ref，姓名使用名称入口，兼容账号仍可使用。
多个人物选择器一律返回 invalid_person_selector，不静默覆盖目标。
空结果仅代表当前查询无匹配，截断/N 条结果不代表全库；拒绝和故障也不能解释成无记录。
严格日期不自动放宽；宽语义候选不是认证过的相关事实，模型须结合内容判断。

总合同见 [Memory 架构](memory-v2.md)；本分支检索改造进度见
[search_memory 任务书](Yuki-search-memory-taskbook-2026-09-29.md)。

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

主 Agent 只声明 `search_memory`，必填非空 query 默认 hybrid，purpose 默认 recall。
原有三个 `get_*_memories` 仅保留执行层兼容旧回执，不进入新工具声明。
非法枚举和无效区间返回 invalid_arguments，不能静默丢弃。
工具的 effective_query 摘要说明实际模式和时间约束，不包含未授权目标。

指定日期默认 temporal_constraint=strict，范围为 `[start_at, end_at)`，严格边界必须带
时区。事件时间未知或范围外不返回，允许空结果，不自动退回软排序。soft 只用于明确的
宽泛偏好。时间使用 valid_from，不将创建时间或临时解析的正文日期冒充事件发生时间。
关键词、向量和总览均在候选截断前筛选；无日期的总览与详情权限保持不变。

主 Agent 不再每轮自动注入长期事实；模型根据完整 History/Rollup 判断何时调用
`search_memory`，不另建短上下文或意图识别 Agent。姓名须通过显式目标解析，SELF 不代替姓名解析；权限拒绝不
重试，歧义先澄清，空结果只允许有实质区别的补查。生产只记录脱敏参数形状，不存完整入参。

旧自动预取内核仍是内部能力，不再决定正常主请求的长期事实注入。
主动工具由正常完整 Main Agent 提供 purpose、entities、preferred kinds、绝对时间范围；
后端仅为主动查询明确目标补缺省重点，不覆盖已提供 subjects；自动查询没有明确重点时留空，
不能将所有有权读取目标或当前发言者自动当成主题。意图不能充当权限凭证。

内部领域读取仍可使用无 query 总览；`search_memory` 必须提供非空 query。
主体分类不迁移、不复制事实。返回数量有界，空结果是正常成功，不是权限错误。
上下文 `event_bound_memory_refs` 仅描述当前消息已绑定的 `subject_ref`，不是读取范围或
人物白名单；未列出的姓名仍通过 `display_name` 交给后端解析、鉴权。此列表不扩展到
全部历史群友，也不根据姓名动态改工具 schema。
统一搜索工具标注 `result_scope=authorized_maximum|explicit_targets`、如实计算的
`exhaustive` 和本次 `returned_count`。无目标时通过 canonical 历史关系的 SQL 授权过滤
全部获准 owner，不依赖 owner 是否仍有活跃 QQ Binding，也不先取前 N 个目标。
显式目标也只在实际候选或返回预算被截断时标记 `truncated=true`；目标存在但查询返回零条
不是截断。Embedding 未配置或故障时保留 `exhaustive=false` 及具体降级原因；显式目标的
语义索引覆盖尚未逐事实证明时同样不宣称穷尽，模型不能由空结果断言没有这类记忆。
全局词法候选、语义候选或向量扫描达到工作预算时返回 `truncated=true`、
`exhaustive=false` 和 `partial_reason=global_candidate_budget`，不把预算截断说成没有记忆。
启用语义但 embedding 未配置或调用失败时也返回 `exhaustive=false` 和明确的语义状态，
表示词法降级的结果不能证明语义空间没有更好事实。
查询成功但当前授权范围内有未建索引或正文已变更的事实时，返回
`semantic_status=index_incomplete`、`partial_reason=semantic_index_incomplete` 和
`exhaustive=false`；这只表示语义索引覆盖不足，不将已返回候选判为无效，
也不能据此断言没有相关事实。
核心记忆工具的固定 grounding 规则必须
穿过统一工具结果转换和正常结果预算器到达模型；插件不能以同名字段声明可信规则。
记忆列表超过原有工具字符预算时只保留排序靠前的完整事实，并返回 `truncated=true`、
`returned_count`、`truncation_reason=response_character_budget`，不裁剪事实正文。
这是输出预算，不是新增条数配额；不能把截断列表视为全部存档。连首条完整事实也放不下时
仍返回 result_too_large，不能谎报空结果。回执只确认实际返回并进入后续请求的事实。
工具 schema 是部署级固定结构，不能用本轮昵称或群号改写。

人物查询默认省略 `group_id/group_name`；处于群聊或引用群成员不等于用户要求限定群。
显式群限定被拒绝时，结果标记 `denied_scope=explicit_group`、`query_executed=false`；
这不是该人物所有范围的拒绝，也不是没有记忆。后端不自动去掉群限定重试，模型不应
把范围拒绝扩大成全局结论。原有历史共同群权限和兼容账号入口保持不变。

overview 没有执行主题匹配，因此候选投影的 `lexical_match` 与 `semantic_candidate`
均为 false，`topic_admission=unknown`。总览排序不再填充伪造的零词法分数；
这些字段描述本次查询的实际路径，不表示总览中事实本身不可信或没有价值。
不可重试的歧义/无权限与基础设施故障分开处理，不强制结束正常对话。

### 读取工具与选择器

- search_memory：无 target 时按本次真实主体的最大可读范围搜索；`target.scope=person` 时用
  subject_ref（真实 mention/reply 优先）、display_name 或兼容 user_id 三选一，可再用
  group_id/group_name 缩小范围；`target.scope=group` 要明确指定 group_id/group_name；
  `target.scope=self` 只查 global/current-private/current-group，不能指定他人的私聊。
- get_memory_fact：同一结构读取政策；get_memory_evidence 仍是更严格的证据接口。
- 名称须在获准历史关系内精确唯一；歧义最多返回五个候选和 has_more，retryable=false。
  不用全社会关系图作为每轮预取目标。
- 主 Agent 的固定声明只包含统一搜索和事实/证据详情工具；实际调用仍按本次主体和目标核验权限。
- 同轮相同已授权查询复用检索结果，减少数据库/embedding 工作；每次入口仍重验权限，
  不缓存永久许可。记忆修改清除本轮读缓存；权限拒绝不做自动重试，不新增读取次数配额。

## 检索核

- 新部署默认请求启用 Embedding；缺少 DashScope 地址或密钥时不创建 Provider，
  明确标为 `not_configured` 并继续使用 FTS。管理界面的全局开关保存后重启生效；
  `memory.semantic_enabled` 是独立的热检索策略开关，不能用它证明向量服务已配置或索引已覆盖。
  旧部署显式 `MEMORY_EMBEDDING_ENABLED=false` 继续保持关闭，直到管理员修改。
- QueryBuilder 规范化文本、有界引用和结构化 intent；保留 FTS、短词 LIKE、
  embedding 与现有 rerank。
- FTS5 trigram 不索引两个汉字的原词。整句检索只返回泛主题候选时，模型可将该原词作为
  两字 query 单独补查，后端继续在相同 SQL 授权范围内有界 LIKE；不自动把整句所有二字片段
  展开为 OR 候选，也不把命中原词误称为能回答问题。
- 非空且启用语义检索时生成 query embedding；overview、lexical 不调用 embedding。
- 词法/语义候选都先按 canonical scope、active、有效期、kind/profile 做 SQL 筛选。
- 无目标主动搜索在同一个 SQL 授权条件下取全局词法候选、全局当前 profile 向量，
  再合并去重与全局排序；不因某个 owner 的先取数量而漏掉后续 owner。词法候选和
  向量扫描仍有全局工作预算；触及预算时结果是部分结果，不能把 `candidate_count`
  当成全范围穷尽证明。显式目标保留原目标内检索路径。
- 原始语义相似度提供相关性档位；意图实体、时间、种类参与排序。活跃度/重要性不能让弱相关
  越过强主题。RRF/rank 不是相关性概率。
- preferred kinds、软时间和主体是排序信号，不能靠它们授予权限；strict 时间是候选准入条件。
- active + contested conflict 可以带争议标记返回；superseded、invalidated、未采用的
  contested claim 不作为普通 active 事实。争议关系不跨 scope。
- 主动查询在 embedding 故障时仍可退回词法，须把真实语义状态返回模型。无词法候选
  允许零条；Main Agent须判断返回事实与问题的关系，不相关则忽略或实质性补查。
  这是可用性降级，不是强相关验收通过。日志只记脱敏类别，
  不记录查询、事实、QQ、群号、向量或 provider 原始错误。

## 暴露、回执与统计

检索是纯读。只有真正进入 Main Agent 请求的结果才确认暴露；不是“检索到了”就算使用。
工具暴露会清除初始 `no_memory` 原因；聊天退出时统一关闭Memory Session，即使被取消、
发送围栏拒绝或其他异常打断，也记录 `interrupted`，不绕过发送围栏做强化。
Plugin API 2.0 / 管理查询不写普通用户 recall 或 activation。
不发生自动注入的普通轮不应伪造长期事实暴露；若旧内核在其他受控入口运行，
零注入仍有独立 receipt，且不触发 attribution。
完成评估但未使用、尚未评估、失败、禁用、抢占/取消和队列满分别记录。
历史 used=false 不回填为确认无用。

Main Agent 使用现有 History、Rollup 和固定工具合同，不另建短上下文。
主请求不再把旧自动召回事实拼入尾部；`search_memory` 返回的事实只有实际进入后续模型
请求才计暴露。固定前缀与工具结构保持稳定，不承诺固定缓存命中率。
缺证据可以按完整前文实质性补查，不强迫每轮调用或回复引用记忆。

指标与排障见 [指标口径](memory-v2-quality-metrics.md)、
[质量运维](../operations/memory-quality.md)。旧 phase/Adaptive 文档不是当前权限合同。

固定读取说明须在 Provider 的 240 字 compact description 内保留完整目标选择合同；
不能把姓名/真实引用规则追加在截断位置之后。列表有数量上限，不能因返回少量事实就宣称
全部存档已列尽。日期说明明确当地零点与时区偏移，后端不猜测或纠正模型给出的合法日期。
补充实施与验收边界见 [意图召回修正任务书](Yuki-意图召回修正任务书.md)。
