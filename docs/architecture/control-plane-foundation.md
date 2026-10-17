# 管理控制面合同

按 [共同开发约束](development-contract.md) 开发。当前管理入口为
[管理 API 与 CLI](admin-api.md)，旧浏览器管理界面与旧查询／命令适配层已从源码退役。
本文描述源码，不表示生产已经升级。

## 应用边界

`ApplicationContainer.admin_api` 使用当前 Bot 的数据库、PluginManager、模型文件服务、
Work 与诊断事实。operator CLI 通过 Bearer HTTP 调用有限方法；QQ `/ai` 沿原领域服务。
个人客户端使用独立的 Person 设备凭据，按 #299 的后续阶段接入，不借 operator 身份发言。

`admin/authority/` 提供真实主体、授权判断、目标、问题和当前命令类型；角色不隐含能力。
权限在执行处核验，读取正文单独授权。不提供通用 RPC、任意 SQL、任意网关动作、
工作区管理终端或不存在的个人端接口。

## 原回执与事务

`persistence/command_receipts.py` 使用原 `control_command_receipts` 与审计关联字段。
数据库修改及其回执共用一个短事务，领域拒绝先回滚本次修改。
外部命令先登记原意图，在事务外执行真实效果，再登记结果。
同一主体和请求号核对完整操作指纹；重复查询或提交不产生新的效果资格。

原 operation ID、历史状态与领域运行引用保留。中断或确认丢失沿原 ID 查证，
启动将遗留 running 标为 unknown，不重跑；不提供清空 unknown 或强制成功入口。
回执验证核对身份、审计关联、资源 ID、revision 和公开结果，不另造领域完成门槛。

插件批准、配置与启停由原服务决定；Work 管理调用原 `manage_work`，不改来源、预算或
已接纳效果。模型连接使用同一个 `ConfigFileService`，原子保存并热应用。
密钥保留在原私有 sidecar 中，不进入响应、审计正文或运行容器的工具环境。

## 退役与切换

旧前端、浏览器会话、CSRF、旧方法表、DTO 与读模型不进入新 API。
原历史效果证据不随界面删除。源码与宿主 Manager 按同一版本交付；
生产切换先更新 Bot，再更新 Manager，并核对原 control running 回执。
实际切换与待核验项目记录在 [#299 交付记录](../operations/issue-299-delivery-2026-10-15.md)。
