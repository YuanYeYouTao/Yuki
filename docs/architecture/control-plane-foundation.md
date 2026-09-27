# Control Plane 地基

按当前 development-contract 开发；WebUI 通过真实应用服务接入 canonical 状态。

## 建设顺序

先完成后端框架，再按业务域接入，最后搭建 HTTP 和页面。框架验收依据真实运行状态、
权限、版本和回执；不以预留方法或只读页面替代完整业务能力。

### 应用装配与查询合同

- 容器装配唯一 ControlPlaneBundle，注入运行中的配置、连接及领域服务。
- 查询 DTO 明确版本、来源与快照时间；配置表单由原 Registry 提供约束。
- 有效配置复用 RuntimeConfigService，并区分当前生效值与已保存值。
- 分页按资源隔离，拒绝跨资源游标；读取不修改事实。
- 身份、权限、修改和审计使用现有 ControlPrincipal/DecisionContext 与内部 ID。

### 管理执行边界

- 配置、身份等数据库修改保留 request ID、revision、原子审计及回执。
- 外部连接、插件生命周期、历史扫描和聚类在写事务外执行。
- 操作人 Principal 与长期 Person/SELF 所有者分开，不把 Web 请求伪装为 QQ 事件。
- 等待、失败和未知效果按原操作 ID 查询；重入不盲目重发。
- 复用正在运行的 Manager/worker，不另造 registry 或第二套恢复循环。

### 业务域、HTTP 和页面

按身份/配置、Work/自动化、自主参与、模型/插件、Memory、媒体/工作环境逐域完成
查询、修改、真实状态和证据闭环。HTTP 实现认证、CSRF、协议转换、脱敏与文件授权；
前端复用已验证合同。现阶段尚未提供管理 HTTP API 或前端。

## 验收

装配使用真实应用实例；未授权调用无写入；读取版本可用于修改；默认及已保存配置正确
投影；并发修改被拒绝；同一请求不重复产生效果；跨资源分页失败；外部等待不持写锁。
状态与批准、保存与生效、任务完成与消息送达分别报告。

不新增第二个 Bot 或数据库事实源，不改写聊天历史和用户人格，不通过真实 QQ 发送
或付费模型调用验证后台框架。

## 当前实现边界

第一层已实现于源码：`ApplicationContainer.control_plane` 装配 Query/Command 服务，
复用运行中的配置、连接、MCP、自动化和 Memory 实例。查询仍经过能力核验，Bundle
不创建 Principal，不自动授予权限，不启动第二个运行时。

配置 schema 提供原 Registry 的名称、说明、上下限、选项和适用范围。
`list_effective_configs` 接受 canonical `ConfigQueryScope`，按 Person → Space → global
解析；不解析平台账号。`value/source` 是当前进程值，`saved_value/saved_source` 是保存后
期望值；`scope_type/owner_kind/person_id/space_id/version` 定位被选中的保存覆盖项。
继承来的全局版本不能作为新建 Person 覆盖项的版本；新建仍用 expected revision 0。
未存覆盖时 version 为 null；敏感配置只给 configured，两个 value 均不返回。
待重启比较当前与期望值，已在启动时激活的覆盖不再显示待重启，删除覆盖仍能显示差异。

Memory fact、自动化、插件、MCP、表情和语音查询提供与修改合同一致的 revision。
MCP 尚无持久状态时 revision 为 0。schema、有效配置、保存覆盖、fact 和 evidence
游标互相隔离；有效配置游标也绑定查询的 Person/Space 范围。Page.snapshot_at 是读取时刻，
不是跨页锁定快照，也不能代替资源 revision 的并发校验。

管理自动化复用已注册插件和 Social 能力的 AutomationService；需要 Person 的现有管理
入口读取 principal.person_id，不把 principal_id 当 Person。无 Person 的 operator 与 SELF
任务完整管理合同仍需下一层完成。

以下是阻止管理 HTTP 接入的存量缺口，不属于本层完成项：

- 插件修改仍需接入真实 PluginManager 生命周期，而非只更新安装表。
- MCP 外部连接、Memory rebuild/dream/maintenance 准备仍需移出写事务。
- 长操作需要按原操作 ID 查询和恢复，未知效果不得重放；现有同步完成回执不能假装
  已具备后台 command 执行恢复。
- 各业务域查询、修改、取消、恢复和失败证据要逐域验收；QQ/CLI 入口尚未全部迁入
  ControlPlaneBundle，认证、HTTP 和页面也未实现。

后续按上述顺序补齐，不能直接给现有 management methods 加 HTTP 路由后宣称框架完整。
