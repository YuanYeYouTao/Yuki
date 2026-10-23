# Memory 变更合同

当前主体与查询合同见 [Memory](memory-v2.md) 和 [检索](memory-v2-retrieval.md)。

`MemoryMutationService` 统一处理主 Agent、命令、管理入口、Worker、自省和 Dream 的事实变更。
权限、真实主体、来源和原操作回执在执行处核验；模型提供的昵称、账号和主体引用只是选择器。
主体解析、完整原始引用核验和模型工作在首次数据库写入前完成。
已知 Person、Space 和 Conversation 直接传内部 ID；平台账号仅由明确传输入口解析或作为历史出处保留。

## 创建与指定事实变更

`memory_key` 是描述标签，同一 canonical 所有者、kind 和 key 可以保存多条独立 active 事实。
CREATE 不搜索同 key 的旧事实，不调用关系分类模型，不争议化、替换或淘汰已有事实。
事实的 confidence、authority 和各条 evidence 的原始值保留，不经固定权重、权威等级或证据乘积重算。

普通提取与 Rebuild 只创建 Person、PersonGroup、Group 事实；自省只以本批新证据创建 SELF 记忆和 Episode，
模型输出不再携带旧 fact 操作或候选裁决。以下完整领域操作仍供 Main、命令和管理入口显式使用，
Dream 仍整理已有正式记忆；收窄自省不删除共享操作、SELF 主体解析或原执行授权。

Dream 可按实际内容重组 fact、preference 或 episode，不要求 focus、正文和 key 唯一，
不限制固定输出条数。部分整理只替换输出实际引用的来源；其余事实保持原样，不提前写处理
checkpoint。新事实复制全部获准原证据，模型输入采样不裁剪持久来源；回滚沿原签名和回执。
单条真实来源也可参与 merge、synthesize 或 resolve，沿原操作语义处理。

| 操作 | 行为 |
|---|---|
| create | 创建独立事实及来源证据 |
| correct | 按实际 fact_id 创建新版本，原指定事实 superseded |
| invalidate | 使指定事实失效，保留来源、审计和回执 |
| restore | 恢复指定事实；已到 valid_until 的事实不恢复为 active |
| contest | 显式标记指定事实争议；提供新内容时保留双方和矛盾关系 |
| merge | 合并指定同 owner/visibility 的事实和真实可读证据 |
| reassign | 按真实目标建新版本并保留原事实历史 |
| update_metadata | 按指定事实建新版本 |

`fact_id`/`merge_fact_id` 是实际数据库事实 ID；没有 ID 时，selector 必须在已解析 target 内
唯一精确匹配 key、旧内容和所提供分类。模糊或多条匹配返回候选供调用方选择，不猜测目标。

## 身份、来源与可见范围

事实归属使用内部主体，正文沿真实来源使用姓名、群名片及必要事实信息，不写无信息的被提及者占位称呼。引用必须匹配实际
来源文本；展示姓名恢复不能改写历史来源后冒充原文。内部 event_id 和真实 tool receipt
沿链传递，不用平台消息 ID 重建业务身份。

已完成 target/owner/evidence 准备的变更直接构造有效事实，不再把同一资料转回提取 DTO 二次解析。
已保存事实使用原 canonical owner，不按当前平台账号绑定重新归属；停用 Person、Space 或绑定不抹除既有事实的永久所有者。
Dream 合成新事实保留原 owner 并取得新的事实 ID 与时间，不继承旧正文的审计时间；当前执行权限仍在实际调用处核验。
SELF 的 kind、category 和 key 不设固定名称白名单。私聊资料传播到 global 仍执行已有隐私边界。
可信自省可为每条 SELF 事实附加多条真实来源；每条来源分别核验 owner 和可见范围。
主SELF沿原initiative和execution的实际工具回执进入同一领域事务；重复摘录及未消费的
补充引用不阻止有效来源写入，不伪造真人事件或借用平台账号作为所有者。

## 原子提交与恢复

同一原请求的 mutation/operation ID 和持久回执负责幂等，不以同 key 全库唯一代替幂等。
主工具使用原 Host 调用的稳定 operation ID；相同正文的新调用仍是独立请求。
原 operation 携带不同参数时报告冲突，不返回另一份变更的成功结果。原调用确认丢失时先读其持久回执。
原修正结果后来 superseded 或 invalidated，不改变该次修正已提交及原结果 ID；命令和插件直接
消费原 mutation 的结果，不再用当前 active 人物列表二次裁决成功。当前对象展示与原效果分开，
真实授权和隐私清退仍沿原入口处理，不恢复已经删除的正文。
Work 已绑定 `memory:<mutation_id>` 而尚未保存工具结果时，从原 receipt 只读恢复；原 outcome、
old/new fact ID 与调用归属保留，不再次执行 mutation，也不因当前事实状态变化推翻原效果。
缺回执或 processing 保持未确认；已知拒绝和无变化按原领域结果返回，不冒充成功或未知。
事实、版本、证据、状态事件和变更回执作为同一纯数据库单元提交。失败整体回滚，取消传播。
完整来源与复制证据在只读快照准备，关闭 reader 后进入主库统一 FIFO writer。
提交时核对实际来源、成员集合、归属和权限；不升级旧读快照，不再运行 Memory 自己的 BUSY 重试循环。
只读准备可并行；未知提交或回滚确认先按原回执查证，不能重放模型、外部效果或发送。
提交后的 embedding 调度失败不撤销已提交事实。

已保存的自省输出与来源仍匹配的 Dream preview 使用原 run、操作与回执收尾；
每日或单 run 模型额度只在真实新 Provider 派发前检查。纯数据库收尾不会重买模型，
也不因额度已满而拒绝原已付结果。普通提取 job 的完成写入排队后只核原 claim 并提交收尾，
这不等于为未保存的提取输出新增了跨进程检查点。

0102 只移除四个 single-active canonical key 索引。0101 的事实、历史表和回执原样升级；
不清库，不为旧数据重新分配 owner，不恢复会破坏独立事实的唯一索引。

普通变更不提供物理 purge；隐私删除继续由现有明确授权入口处理。
持久证据不做抽样删源；模型请求中的证据摘录预算不改变原 evidence 的保留和授权。
