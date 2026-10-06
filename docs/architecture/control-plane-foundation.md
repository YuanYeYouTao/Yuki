# Control Plane 地基

按 [development-contract](development-contract.md) 开发；完整管理 WebUI 通过同一应用边界接入 canonical 状态。
本文描述源码合同，不表示 PR 已合并、生产已部署或浏览器已验收。

## 建设顺序与完成标准

先打通真实应用服务、可信主体、读写版本、执行回执和传输合同，再按业务域完成接口与页面。
完整 WebUI 的功能范围包括身份/配置、Work/自动化、自主参与、模型/插件、Memory、媒体/工作环境；
按层完成这些功能，不把简表或只读演示视为完整产品。

公共地基的验收是：真实服务唯一装配、权限来自可信入口、读出的版本能提交修改、
外部等待不持写锁、并发修改有真实冲突、请求重入不产生第二次效果、失败可按原 ID 查证。
具体业务详情、文件操作和 HTTP 会话按下述后续层接入，不预造通用调度器、万能工具入口或空接口。

## 应用与身份

`ApplicationContainer.control_plane` 装配唯一 `ControlPlaneBundle`：`access`、`queries`、
`commands` 和启动恢复函数。配置、连接、插件、自动化、Memory、维护和 embedding
依赖来自正在运行的容器，不另建 registry、第二个 Bot 或数据库事实源。

查询和命令分别经过现有 ControlPrincipal/DecisionContext 能力核验。角色仅作描述，不隐含授权。
内部事件沿用 `chat_events.id`；管理请求没有入站聊天事件，不伪造 QQ 消息或借用平台 message_id。

### 可信 operator 入口

`CONTROL_OPERATORS_FILE` 可选地指向 TOML，格式见 `config/control-operators.example.toml`。
每个 operator 声明独立 UUID、token 环境变量引用和明确 capabilities；Person 绑定可为空。
未配置文件时默认拒绝全部认证；错误配置拒绝启动。重复主体、凭据引用和歧义凭据拒绝使用。
凭据须为 32–4096 位 ASCII 字符，由服务器环境提供，不保存到回执或下发浏览器。

`access.authenticate` 只接收凭据与受信任适配器确定的 CLI/Web 来源；主体和权限由服务器生成。
请求不能提交自己的 roles、authenticated 或能力。声明在应用装配时读取，修改需重启；
凭据认证读取当前进程环境，部署环境更新仍须按部署方式生效。可选 Person 必须已经存在，
认证不创建 Person，也不要求 operator 绑定 QQ。

此入口不是 HTTP 登录会话；Cookie、退出/撤销、CSRF 和监听配置须在 HTTP 层完成后才开放浏览器访问。
现有 QQ/CLI 尚有直接调用共享领域服务的入口，不声称所有入口已迁入 Bundle。

## 查询、版本与配置

- DTO 只投影公开字段，不返回 ORM、私有 journal、Settings 或 credentials。
- 配置 schema 来自原 Registry：名称、说明、上下限、选项及适用 scope，前端不复制规则。
- `list_effective_configs` 接受 canonical `ConfigQueryScope`，复用 Person → Space → global
  解析；`value/source` 是当前进程值，`saved_value/saved_source` 是保存后的期望值。
- `scope_type/owner_kind/person_id/space_id/version` 定位被选中的覆盖项；继承的全局版本不能
  当作新建 Person 覆盖项的版本，新建使用 expected revision 0。无覆盖时 version 为 null。
- 敏感配置只给 configured，不返回当前或保存值。待重启比较当前和期望值，启动已采用的覆盖
  不再显示待重启，删除覆盖仍能显示差异。
- Memory fact、自动化、插件、表情和语音 revision 与修改合同一致。
  时间戳版本更新保持单调，插件 在 Manager 锁内复核版本后执行效果。
- keyset cursor 绑定资源和查询范围；schema、有效配置、保存覆盖、fact/evidence 及各类 operation
  不能混用。Page.snapshot_at 是本次读取时刻，不是跨页锁定快照或 CAS revision。

配置修改、删除和回滚均按 canonical Person/Space 重新校验继承后的关联参数。
直接领域入口自己开启短写事务，控制面复用调用方的短写事务；读取、校验、写入和领域审计
使用同一事务，不再与进程配置锁交错。普通校验拒绝发生在写入前；写入或审计后的异常
退出整个事务，不能被吞成失败结果后继续提交配置或控制回执。运行中的热配置从已提交数据库读取。

组件健康区分 enabled、running、healthy 和 checked_at。未知项为 null，running 不冒充健康。
数据库不可达时 queue 和 identity_revision 为 null，不制造空队列或版本。组件查询经过权限核验，
不调用付费模型；公开存活探针和浏览器受权管理诊断仍是不同入口。

## 命令、外部效果与恢复

数据库修改在短事务内保留 request ID、expected revision、同步审计和幂等回执。
领域数据库修改使用原事务的 savepoint；被拒绝时先撤销本次领域写入，再在同一短事务登记
原请求的失败审计和回执。它不用于外部效果，也不能把网络或文件效果当成可回滚写入。
成功配置的领域审计和 Control Plane 回执分别保留证据，但共享真实 principal 与独立 control_request_id；
平台 trigger_message_id 留空。0073 迁移仅转换由既有回执证明关联的历史控制审计，QQ 审计不改写。

插件 approve/enable/disable/doctor 使用实际 PluginManager。批准与运行状态分别查询，
启动失败不能返回 enabled 成功。

外部命令采用原 `control_command_receipts` 的短事务意图 → 事务外真实效果 → 短事务结果登记。
不增加第二个 worker、持久请求载荷或恢复状态库。操作 ID 为 `control:<principal_uuid>:<request_uuid>`。
同一请求重入查原回执，换 payload 返回幂等冲突；同一资源的 running/unknown 意图阻止并发外部操作。
已证实的版本/领域拒绝记 failed；中断、效果后异常或最终回执提交失败记 unknown。
启动将遗留 running 转为 unknown，不重跑。未知结果保留原 ID，并阻止用新请求绕过。

未知结果的查证须使用真实 Manager/领域证据；本层没有“忽略未知并重新执行”的按钮，也不把
当前状态猜成原请求已经成功。后续如需人工处理入口，应定义具体领域的证据与释放条件，
不能提供通用强制成功/清空回执操作。

Memory rebuild 历史扫描、Dream embedding reconcile/聚类在写事务外准备；短事务只登记原计划。
维护 run/start 仅以 yuki 为目标，expected revision 为 0；复用现有 worker 的串行一次处理，
不在控制写事务内执行，不接受会产生维护效果的假 plan。返回 revision 1 为本次回执版本，
不把变更数量冒充 Memory 资源版本。start 必须定位已有 run，错误 ID 不创建
新计划。plan/start/cancel 使用原 rebuild/dream ID、预算与状态，operator ID 不制造 Person。

`read_operation` / `list_operations(kind=control|rebuild|dream)` 查询原状态。
waiting/blocked/unknown 不压成 running/failed；无可计算进度时 progress 为 null，终态为 1。
rebuild/dream 的首次命令、重入与查询复用同一投影；旧诊断不冒充已取消/成功/运行状态的当前错误。
失败诊断缺失或不符合公开分类时给出通用失败类别，不输出原始诊断，也不修改领域证据。
自动化 run_now 是新的调度，不能充当旧 run 恢复。插件通知 retry 仅接受已有证据证明的发送前暂态失败，
保留尝试预算；已发送、未知、处理中和预算耗尽拒绝重发。

## 自动化的操作人与执行归属

operator Principal 负责管理授权和审计；自动化长期 owner 明确为已有 canonical Person 或 SELF。
创建可用 spec 包装：`script`、`owner_id`、`conversation_id`、可选 `max_runs`。
无包装时兼容绑定了 Person 的 operator 默认归属；无 Person 时必须明确 owner。
更新保留既有 owner 和场景，不改成操作人；SELF 暂停/恢复等操作不要求 Person QQ 绑定。
仓库禁止把 completed/cancelled 改回可调度状态；管理入口检查实际变更结果，领域拒绝不增加 revision。

群任务要求明确 canonical Conversation、当前 SpaceActiveRoute、Binding 和真实 Presence。
Person 账号复用活动路由，多个候选没有明确路由时拒绝首项猜测。私聊场景与 owner 匹配。
脚本按真实 owner 当前权限验证，SELF 使用稳定 SELF 权限，不继承管理员能力。
TaskSpec、Social 路由、run/step/投递回执和 source_key 使用原服务，不伪造入站事件。

## 公开协议与发现

`queries.describe` 只投影已经实现的 Query/Command 方法、其现有能力、敏感度与当前主体是否有权。
它不授权、不调度，不把全部声明能力误报成已实现功能。

`control_plane.wire` 提供纯协议转换：`control.v1`、原 request_id、data/problem、canonical UUID、
UTC 时间、opaque cursor、真实状态和 null 进度。命令只接受 request_id/expected_revision/payload，
拒绝客户端主体、布尔 revision、平台编号及未知 envelope 字段；分页有界。
旧游标查询保持 keyset 连续翻页；显式编号页对同一授权筛选执行 SQL COUNT 与
LIMIT/OFFSET，可直接跳页，不把页码当内部事件身份或恢复位置。
输出仅接受公开 DTO，raw dict、ORM、异常、Principal 和 Settings 不能直接成为响应。
HTTP handler 应调用这些转换和共享服务，不能把数据库方法直接暴露为通用 RPC。

模型 profile revision 将无序 capability 集合规范排序，重启和 PYTHONHASHSEED 不再改变同一合同。
旧未规范 hash 与新 hash 可能产生一次已有合同边界；不修改历史、重置 Work、重发消息或全量 /ai new。

## 完整 WebUI 的建设合同

正式前端与 HTTP 的当前实现及验收边界见 [WebUI 合同](webui-console.md)。
下面是完整产品的能力合同；各页面沿对应原服务与持久回执实现，不能用部分页面或原始 JSON 编辑替代闭环：

| 下一层 | 必须完成的闭环 |
| --- | --- |
| 身份/配置 | 详情与筛选、适用动作；文件型 JSON/TOML/人格的验证、原子保存、加载结果与生效范围 |
| Work/自动化 | 原 work/run/step、等待、预算、子工作、投递回执的详情及领域允许的取消/续跑 |
| 自主参与/模型 | Jev/群决策时间线、参数读写、Profile/Route 配置与调用统计，不重算第二份数学模型 |
| 插件 | 配置 schema、监控游标/队列/outbox 详情与有证据的处理动作 |
| Memory/关系 | 主体筛选、内部事件证据、运行水位与自省统计、关系投影和实际领域动作 |
| 内容/工作区 | 聊天 timeline、24 小时媒体权限及过期、长期共享工作区、文件/表情/语音操作 |
| HTTP | 登录会话、退出/撤销、CSRF/Origin、请求体限制、错误映射、文件授权、同源监听与反代 |
| 前端 | 导航、schema 表单、revision 冲突、异步状态、内容渲染、跨域闭环与浏览器测试 |

HTTP 与文件入口在相应授权及失效测试通过前不开放。Cookie 会话必须按服务器主体重新核验权限，
凭据不成为页面 URL；文件不能直接暴露宿主路径、私有 journal 或网关地址。
保持单 Bot 装配与 SQLite instance lock，现有容器没有因此新增管理端口。

## 验证与交付

reply delay 的 min/max 修改与审计仍在同一事务。跨作用域验证只索引一次有效 override，
按 USER 优先于 GROUP/GLOBAL 的原继承规则计算用户单边值及群上下界极值；复杂度为
O(N+U+G)，不枚举用户与群的笛卡尔积。重复 canonical owner 仍明确拒绝，不能以优化
之名跳过最终 writer 当前配置验证。

使用实际容器装配和真实本地 Plugin 生命周期，覆盖未授权、并发版本、幂等、等待不持写锁、
效果后异常、取消、重启、跨资源分页、可信 operator、无 QQ operator 的 Person/SELF 自动化及迁移。
模型 hash 使用多个独立进程验收。测试不发送真实 QQ 消息、不调用付费模型。

源代码通过验证、PR、合并、部署和真人/浏览器验收分别记录；新后端合同不能代替生产或完整 WebUI 验收。

## 执行过程与聊天查询

执行诊断见 [执行过程查看合同](execution-trace.md)。应用查询提供
`list_execution_trace`、`read_execution_trace`、`list_chat_events` 和
`list_social_receipts`。`control.execution.metadata.read` /
`control.execution.content.read` 与 `control.chat.metadata.read` /
`control.chat.content.read` 分别授权元数据和正文。列表默认不返回正文，
请求正文时显式要求对应权限。接收与已发送消息来自原 `chat_events`，
投递确定性来自原 Social 回执；没有引入诊断驱动的恢复或重新发送。
