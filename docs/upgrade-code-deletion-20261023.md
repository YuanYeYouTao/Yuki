# 代码清理与连接器升级说明（2026-10-23）

[English](upgrade-code-deletion-20261023.en.md) · [完整执行记录](operations/code-deletion-20261023.md)

本文对应 B1–B6 源码清理候选，不代表新的正式 Release、PyPI 发布或已部署状态。
实际基线、测试与 PR 信息见执行记录。

## 本机连接器需要与 Bot 配套升级

本批连接器包为 `yuki-agent-connector 0.2.0`，WSS 协议为 `2`。Bot 与连接器同时使用该协议，
旧协议会明确拒绝，不隐式切换服务器 Agent。更新时保留原配置、设备 namespace、凭据和
SQLite journal；不要通过删 journal、重新配对或更换执行 ID 来处理升级。

连接器各实例独立启动和恢复，已就绪实例可先使用，后就绪实例通过原连接更新目录。
传输每次发送一个原持久事件，主库提交完成后 ACK，再发送下一项；同连接的 RPC 响应
继续独立处理。ACK 丢失后重连沿原 event/operation ID 对账。显式传给 repository 的 event
list 仍是整笔原子事务，后项非法会整批回滚。

离线同负载对照确认该窗口降低峰值内存、writer 持有与聊天 FIFO 等待，同时降低高积压
场景吞吐；数值和测量条件见执行记录。它没有改变 Work 完成、预算或用户任务的门槛。

## 插件 Memory 和测试 Fake

公开 SDK 方法签名保持。Host 的逐调用 ID 不再被原事件 ID 覆盖；同事件的两次独立调用
互不占用回执，原调用重放仍读原回执。历史 event-wide 插件回执不能可靠映射到某次调用时，
保留原冲突/未知事实，不猜测迁移或补跑。

`add` 的 `automatic` / `plugin` 不能凭生产者标签冒称真人显式指令；实际来源和 authority
沿原 actor/场景判断，confidence 进入真实 fact 与 evidence。`update` 创建修正版本，
`confidence=None` 保留原值；原版本和原证据仍在。背景、第三方和 Group 的来源语义继续独立。

Fake 使用同一返回形状：`add.data.memory` 是记录，`memory_id` 为 `person:<整数>`；
`update.data.memory_id` 是新修正版本 ID，`delete.data.memory_id` 是被失效的 ID。
依赖旧 UUID、`add.data.memory_id` 或原地修改模型的 fixture 应更新，不保旧别名。

## 有意退出的 CLI 与 Host Python 表面

- 退出 `yuki prompt inspect` 和 `yuki prompt compare`；`model profiles/routes/stats`、
  `runtime snapshot/memory-session` 保留，真实 Prompt 编译与模型用量诊断继续使用。
- 退出 `ModelExecutionPriority.EXCLUSIVE`。前台、持久后台、维护、SELF quiet 和真实并发额度保留。
- 退出 `PromptTrust` 及 `PromptContribution(trust=...)`。已有 source/角色/正文顺序、SOUL 边界
  与插件 `trusted_level`/批准继续沿原合同。
- 退出 `CapabilityExposure`、旧 scope metadata 和 `TurnCapabilityRuntime.initial_exposure()`。
  当前执行 grants 在构造时投影；固定 schema 由 MainAgentContract 编译一次，每次执行仍核当前授权。
- 退役的内部 helper、无效参数和构造参数见 B1 记录与完整任务表。Host 深层 import 的调用者
  应迁移到现役接口；这些删除没有插件 SDK 兼容别名。
- `SocialService.workspace` 重复别名退出，使用 `workspace_service.store`；Chat、Social、
  Workspace、Emoji、Participation 等构造器改为直接接收实际依赖，不再支持构造后回填。
  依赖这些 Host 内部 Python 表面的仓外调用者需同步迁移。

## 存量数据与恢复

Workspace 独立 manifest 在每个实际 owner 打开时完成 source_key/旧 expiry 升级；artifact
永久保留原 ID、字节、hash、revision 和 provenance，旧 expiry 不再拒读。每次普通事务仍核真实
路径与链接，发布、引用保护和孤儿 GC 保留。short_state 与入站媒体的 24 小时寿命独立保留。

新来源证明使用 codec3，无 observation 的普通来源与跨入口输入不再绑定诊断 privacy counter。
已有 codec2 继续使用原指纹和 reader，不重写旧已付请求。旧 counter 没有单独保存且原证明
不能成立时，保留原 Work、预算和 journal，停止该次恢复；不会拿今日上下文重新认证、重买响应
或重发工具。实际 observation DAG 和 compact 发布的隐私检查保留。

本批不改主库冻结迁移，不清理生产数据库、缓存、备份或用户原件。通用 SDK `ON_FAILURE`
和 Plugin Main 的原同参冷恢复合同保持；订阅插件非法配置停止后，修正配置并显式重启该插件。
