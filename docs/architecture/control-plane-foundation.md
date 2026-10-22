# 管理控制面合同

按[共同开发约束](development-contract.md)开发。当前管理入口为[管理 API 与 CLI](admin-api.md)与显式 `/ai admin`；旧浏览器控制面已退役。源码、PR、合并与生产分别记录。

## 主体与入口

超级管理员资格是 `Person.is_superuser` 的持久事实。平台账号只解析当前真实人物，解绑不丢资格，转给另一人不携带资格。有效授权保原 actor Person ID，核原人物与当前 root 启用及当前资格；合并保双方当前资格，撤销后不从旧成员恢复。operator 保独立 Bearer 与明确能力，不能冒充人物发言。

App/ACP 个人消息不解析管理员或插件直达命令。Person 是超管不改变设备凭据用途，也不动态改写模型固定工具声明。长期执行中的写入、特权读取和外部效果按当前授权处理，原已发生效果和 paid 事实保留。

## 共同应用与领域

`admin/operations/` 按身份、群、运行配置、插件、模型和 Work 组织共同应用操作；`admin/queries.py` 提供原事实的只读投影。HTTP/CLI 和 `services/commands/` 的聊天适配只解析、调用、呈现，不互相调用字符串 handler，不复制身份与领域规则。

原人物领域、RuntimeConfigService、PluginManager、Memory mutation 和 manage_work 继续拥有实际修改。公共身份 owner 由应用装配一次构造，分别注入个人 API、关联命令和管理操作，不依赖 Admin API 对象取得身份服务。

管理语法为 `/ai admin <领域> <操作>`；普通会话控制、本人 Memory/Preference/Automation、`/ai link`、`forgetme` 与插件注册调用各保真实职责。原生平台共用停用/未登记群恢复入口，按接纳前的真实连接与同平台人物、路由证明执行，不依赖普通聊天先成功入账。Person disable 停用当前人物；enable 明确恢复指定来源，并启用其当前人物，不恢复其他已停用来源、设备或 Binding。旧 private/on/off 管理语法已删除。

Account 资料和 Assignment 归属各有唯一事实源。管理入口共用替换归属、暂停恢复与同人合并操作；
解绑／注销结束阶段，不能用 disable 代替。显式人↔Yuki 转换建立新阶段；物理连接独立保留，
重连不恢复已结束归属。目录查询采用游标分页，平台健康按已注册 Provider 和真实连接投影。

## 原回执与事务

共同执行器复用 `control_command_receipts` 和原审计。数据库修改与控制回执在同一短事务，外部效果先登记原意图，事务外执行后结算；Memory 等既有领域回执不被第二套完成表替换。

数据库命令使用主库统一 writer，授权、领域变更、savepoint 与原回执共用该 session。
人物归并、清除和旧配置 rollback 先在 reader 准备正文及原选择；进入 writer 后核当前真实来源，
变化只触发本次纯数据库准备重做。外部命令仍按原意图与真实效果结算，不进入数据库异常重试。


普通聊天管理请求使用已入账事件本身的 canonical UUID 关联原请求，业务来源仍沿原内部账本主键；保存原 revision 后重放不读取今日 revision 重算指纹。平台消息号不重建已有业务身份。重复请求只读原回执，不同主体或新请求不全局合并。

running/unknown、确认丢失沿原 ID 查证；未知不等于失败，不授予重发资格。新操作和旧操作均有真实 replay reader，不能因改命令语法删除升级证据。发送中文提示失败不重复执行已经完成的修改。
已确认未写入或整体回滚的失败保留原失败回执；只有效果或回滚未确认才记 unknown。旧 unknown 保留并且不自动重试，也不永久阻挡新 UUID 的显式操作；真实并发由原 Manager／文件服务的锁和 revision 裁决。

人物/Binding/Presence 恢复和对象发现有明确管理入口；Work 详情提供当前 revision。停用 Presence 保原 ID、作者和投递回执，不宣称真实连接已经断开或上线。模型密钥沿原私有 sidecar 原子保存，同别名可轮换，bundle revision 同时覆盖正文与密钥原件；离线插件配置使用已批准定义，不隐式启动插件。

配置 catalog 复用 Settings 的真实字段约束，重启字段从注册表共同映射到启动设置，不再维护一张手写子集。
个人 GET 鉴权只读，不更新辅助活跃时间。诊断投影分别展示各平台与现有环境证据，无证据为 unknown；诊断不制造业务完成事实。

升级去除旧 SUPERUSERS 的顺序及一次候选清单见[人物授权升级](../operations/person-admin-control-plane-upgrade.md)。原 #299 历史交付见[当时记录](../operations/issue-299-delivery-2026-10-15.md)。
