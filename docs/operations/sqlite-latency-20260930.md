# SQLite 与后端延迟修复范围

本轮对应 `755f725` 审计任务书的已确认路径，源码版本仍是未发布的 3.9.0 开发基线，数据库 head 为 `0082`。本文说明实现与验证范围，不是上线证明。

| 审计项 | 当前修复 |
| --- | --- |
| F01、F02 | 过期 outbox processing 转 uncertain，保留原尝试，不自动重发；同尝试且凭据一致的迟到回执可确认；未分类 transport 异常不重试，仓库也检查安全未发送类别 |
| F03 | 申请 writer 后取领取时间；租约 UPDATE 的有效期条件使用 SQLite 执行时钟，排队期间到期的租约不能续期或提交 |
| F04 | 显式 BEGIN 只读快照读取固定内部事件和 rollup 后计算指纹；短 writer 内复核租约、generation、来源 revision 和 canonical owner；0082 闭合指纹来源变化的 revision 覆盖 |
| F05 | Provider 容量与前台预留统一由 Executor admission 控制；会话层只跟踪取消，不重复限流；排队后重新核验 dispatch 来源，普通后台 Work 不被抢占 |
| F06 | 已存在 Relationship 只读返回；缺失才申请 writer 并重新解析 owner 与创建，不使用旧只读快照重建关系 |
| F07 | 投影容量回收只查询其他视图的 key、大小和失效状态，不加载其 payload；原 prefix 与容量 CAS 保留 |
| F11 | Rebuild 已 committed/skipped item 不在重复 prepare 时重新登记完成回执，不覆盖 committed 事实 |
| F13 | SQLite holder 在真实 DBAPI commit/rollback 完成后清除，失败保留至成功 rollback、失效或连接退出；不访问失效连接的 info |
| F14 | 参与 snapshot connection 的创建、load、save、close 在同一专用线程；冻结保存 payload，提交取消后按真实结果更新 revision，缓存 pin 与淘汰共同串行 |
| 前台诊断与后台空轮询 | 诊断共用一个有界异步 consumer，冻结原身份和隐私代次；空 notification、Emoji、Work repair/reclaim 和未来未满足的 wait 不申请 writer，发现候选后有界写内复核 |

## 验证方式

开发中仅运行各模块直接相关的既有测试及新增真实 SQLite 锁竞争、取消、迟到回执、隐私删除和 CAS 回归。最终 PR CI 执行完整检查，不在开发中反复跑全量套件。

回归分别验证：持有其他 writer 时模型结果与后续调用仍可返回；已有关系无 DML；future wait/空队列无 BEGIN IMMEDIATE；snapshot 保存等待时事件循环仍响应；来源读取后发生删除、元数据修改、owner 变化或租约到期时不能 dispatch；混合后台请求仍保留前台容量。合成结果不替代生产负载或真实 QQ 验收。

诊断队列过载、失败和退出时可能缺少样本，统计为最终可见且 coverage 可不完整。聊天账本、模型预算、Work journal、权限核验和外部效果回执继续走原持久化合同。详见 [执行诊断](../architecture/execution-trace.md)。

## 部署及剩余范围

按生产手册检查最终 Compose 镜像、保留旧镜像与一致性数据库备份，只替换 Bot，核对 `0082`、OneBot 重连、健康及重启次数。旧镜像严格校验 `0081`，代码回退前需用新镜像将 schema downgrade 到 `0081`；该迁移仅撤销新增触发器，不能用旧数据库备份覆盖新消息与回执。

本轮没有完成整个审计任务书：Profile autoflush、Memory evidence 批量读取、Dream owner 解析、配置版本 CAS 及其他次级存储项目仍须按各自生产调用边界处理。投影原 prefix JSON 比较仍保留，不能宣称所有 writer 内 CPU 都已移除。Provider 自身响应、网络与产品发送节奏不属于本轮消除的等待。
