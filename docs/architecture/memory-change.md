# Memory 变更合同

当前主体与查询合同见 [Memory](memory-v2.md) 和 [检索](memory-v2-retrieval.md)。

`MemoryMutationService` 统一处理主 Agent、命令、管理入口、Worker、自省和 Dream 的事实变更。
权限、真实主体、来源和原操作回执在执行处核验；模型提供的昵称、账号和主体引用只是选择器。
主体解析、完整原始引用核验和模型工作在首次数据库写入前完成。

## 创建与指定事实变更

`memory_key` 是描述标签，同一 canonical 所有者、kind 和 key 可以保存多条独立 active 事实。
CREATE 不搜索同 key 的旧事实，不调用关系分类模型，不争议化、替换或淘汰已有事实。
事实的 confidence、authority 和各条 evidence 的原始值保留，不经固定权重、权威等级或证据乘积重算。

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

事实正文写真实姓名、群名片或真实 ID，不写无信息的被提及者占位称呼。引用必须匹配实际
来源文本；展示姓名恢复不能改写历史来源后冒充原文。内部 event_id 和真实 tool receipt
沿链传递，不用平台消息 ID 重建业务身份。

已完成 target/owner/evidence 准备的变更直接构造有效事实，不再把同一资料转回提取 DTO 二次解析。
SELF 的 kind、category 和 key 不设固定名称白名单。私聊资料传播到 global 仍执行已有隐私边界。
可信自省可为每条 SELF 事实附加多条真实来源；每条来源分别核验 owner 和可见范围。

## 原子提交与恢复

同一原请求的 mutation/operation ID 和持久回执负责幂等，不以同 key 全库唯一代替幂等。
事实、版本、证据、状态事件和变更回执作为同一纯数据库单元提交。失败整体回滚，取消传播。
SQLite WAL 读快照在首写前冻结来源和 owner。仅原生 SQLITE_BUSY_SNAPSHOT（517）在新 session
重备同一数据库单元，复用原 ID，不重跑模型、外部效果或重新发送。提交后的 embedding 调度失败
不撤销已提交事实。

0102 只移除四个 single-active canonical key 索引。0101 的事实、历史表和回执原样升级；
不清库，不为旧数据重新分配 owner，不恢复会破坏独立事实的唯一索引。

普通变更不提供物理 purge；隐私删除继续由现有明确授权入口处理。
