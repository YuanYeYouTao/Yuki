# Yuki 永久主体、多 Presence 与 WebUI 控制面任务书

## 1. 审计结论与交付目标

本任务书已经过“指挥官独立审查 → Grok 红队 → 反驳纠偏 → Grok 复核”多轮对抗审计。最终撤回并禁止了以下错误方向：

- 不把控制面当成 Conversation、Scope 或路由。
- 不在核心服务中伪造 `AdminActor`。
- 不把 GatewayConnection 持久化为业务身份。
- 不让自动化继续绑定创建时的旧 Yuki QQ。
- 不用路由接管修改 Conversation generation。
- 不把 plugin/automation/command 当作 `author_kind`。
- 不推迟 Binding、Presence 和路由的可管理能力。

正式执行时：

- 删除旧任务书 `docs/architecture/Yuki-QQ多网关与SnowLuma兼容解耦任务书.md`。
- 新建 `docs/architecture/Yuki-永久主体、多Presence与WebUI控制面任务书.md`，内容以本计划为准。
- 本轮实现身份地基、多 Presence、canonical Conversation、确定性路由，以及未来 WebUI 可直接复用的 transport-neutral 控制面服务层。
- 本轮不实现 HTTP 管理 API、不开放新端口、不做前端。
- 不推送、不部署、不涨产品版本。C0–C24 按 Commit 合同推进；剩余已合并工作按三枚本地 Bundle/Commit 验收，最终纠偏只 amend Bundle 3 / HEAD，不另开第四枚 Commit。

---

## 2. 不可变架构合同

### 2.1 Yuki 与身份

- 一个数据库就是一个永久 Yuki，不建立 `Yuki` 或 `YukiSelf` 表。
- SELF 继续由 `memory_facts.scope_type='self'` 表示。
- 换 NapCat、SnowLuma、容器或网关实例不会创建新 Presence。
- 换 Yuki QQ 账号会新增 Presence，但人格、SELF、记忆、会话、关系、设置、插件状态和自动化仍属于同一个 Yuki。
- Yuki Presence 不是 Person；第三方机器人也不是 Person。

Canonical ID 使用标准库 UUID4、`TEXT(36)`。账本和队列本地序号继续用 INTEGER。

### 2.2 Canonical 数据模型

新增以下核心表：

- `persons`
  - `id`、`enabled`、`revision`、时间戳。
  - 关系、私聊权限、长期人物状态最终挂在这里。

- `identity_bindings`
  - `id`、`person_id`、`platform`、`external_account_id`、显示元数据、状态、revision。
  - 唯一约束：`(platform, external_account_id)`。
  - 一个 Person 可以有多个 Binding。

- `spaces`
  - `id`、`name`、`enabled`、`autonomous_enabled`、`require_mention`、revision。
  - v2 正式让 `require_mention` 成为有效行为，不再保留死配置。

- `space_bindings`
  - `id`、`space_id`、`platform`、`external_space_id`、显示元数据、状态、revision。
  - 唯一约束：`(platform, external_space_id)`。

- `presences`
  - `id`、`platform`、`external_account_id`、`enabled`、`ingest_eligible`、revision。
  - 唯一约束：`(platform, external_account_id)`。
  - 只表示 Yuki 的平台账号。

- `canonical_conversations`
  - `id`、`kind=private|space`、`person_id|space_id`、generation、rollup 水位和 revision。
  - 私聊唯一对应 Person，群聊唯一对应 Space。
  - generation 只因 `/ai new` 等显式会话重置变化。

- `conversation_legacy_aliases`
  - 多个旧 `scope_key` 可以指向同一 Conversation。
  - 每个 Conversation 必须恰好一个 primary alias。
  - 迁移时取最小旧 `conversation_scopes.id` 对应的 key 为 primary。
  - 新 Conversation 首次创建时固化一次；换账号和路由接管不得改变。

- `canonical_event_receipts`
  - OneBot 消息的显式传输幂等记录。
  - 唯一键为 `(ingress_presence_id, event_type, platform_message_id)`。
  - 插件外部事件继续使用自己的 canonical 唯一键，不进入此表。

- `identity_runtime_state`
  - 单例状态 `v1|v2`，记录 cutover ID、源指纹和完成时间。

- `identity_backfill_runs`、`identity_conflicts`
  - 保存回填进度、分类结果及不可自动解决的冲突。

- `control_command_receipts`
  - 唯一键 `(principal_id, request_id)`。
  - 保存请求载荷哈希、结果、审计 ID 和长任务引用。
  - 相同 request ID、相同载荷返回旧结果；不同载荷返回幂等冲突。

### 2.3 三张业务路由表

只能存在以下三张持久路由表：

1. `person_active_routes`
   - `person_id → identity_binding_id + presence_id`
   - 用于向 Person 主动发送通知和自动化结果。
   - 含 `route_generation`、`paused`、revision。

2. `space_binding_ingest_routes`
   - `space_binding_id → ingest_presence_id`
   - 每个外部群只能有一个 ingest Presence。
   - 含 `route_generation`、`paused`、revision。

3. `space_active_routes`
   - `space_id → space_binding_id + presence_id`
   - 用于向 canonical Space 主动发送。
   - 含 `route_generation`、`paused`、revision。

禁止：

- 泛化 `DeliveryRoute`。
- `presence_active_routes`。
- 把 GatewayConnection ID 写进业务路由。
- 把控制面建立为第四种路由。
- 路由变更时修改 Conversation generation。

### 2.4 GatewayConnection

GatewayConnection 只存在于内存 Registry：

- 记录 `gateway_instance_id`、Presence、provider、活 Bot 句柄、connection generation、健康和能力。
- Bot 句柄、连接对象、token 不落库。
- 每个 Presence 恰好一条确定的 active connection。
- 不同 Presence 可以同时在线。
- 同一 Presence 多连接且没有确定 pin 时失败关闭，禁止按 `get_bots()` 顺序选择。
- 同 `self_id` 双反向 WS 能否稳定区分不阻塞身份地基；SnowLuma 阶段再做专门 probe。

### 2.5 事件作者与来源

`author_kind` 固定为：

- `person`
- `yuki`
- `external_bot`
- `system`

以下信息属于 `origin` 或 `event_kind`，不是作者：

- user message
- command
- plugin external event
- automation
- scheduled task
- migration/maintenance

`chat_events` 增加 nullable canonical 字段：

- `canonical_conversation_id`
- `author_kind`
- `author_person_id`
- `author_presence_id`
- `ingress_presence_id`
- `utterance_fingerprint`
- `suppression_status`
- `canonical_event_id`
- provider/gateway provenance

旧 `bot_user_id`、QQ sender/group 字段继续保留为 provenance 和兼容列，不再表示所有权。

---

## 3. 入站与路由行为

### 3.1 群消息

顺序固定为：

1. 用事件对应的 Bot 句柄解析 GatewayConnection 和 Presence。
2. 用外部群号解析 SpaceBinding。
3. 检查 `space_binding_ingest_routes`。
4. route paused 或当前 Presence 不是 ingest 时，在 policy、claim、账本、Agent、Memory 之前丢弃。
5. 最终围栏复核、receipt claim 和 canonical event append 在同一 `BEGIN IMMEDIATE` 内完成。
6. 被丢弃的扇出事件只产生无正文的聚合指标或限频日志。

主 ingest 离线时：

- 候选必须满足 Presence enabled、ingest eligible、平台匹配、Registry 中恰好一条 active connection、能力合格，并通过实时群成员探针。
- 恰好一个候选：CAS 更新 `space_binding_ingest_routes`，仅增加 route generation。
- 0 或多个候选：route paused，失败关闭。
- 不修改 Conversation、rollup、cadence、effect gate 或短期上下文。

### 3.2 私聊

- 私聊不经过群 ingest 围栏。
- 一个 Person 的所有 IdentityBinding 进入同一 canonical private Conversation。
- 回复优先使用该事件的 ingress GatewayConnection。
- 原连接失效时只能换同一 Presence 的 active connection，不得换其他 Presence。
- 主动私聊才读取 `person_active_routes`。

### 3.3 mention、reply 与 self

- sender 属于任一 Yuki Presence 时为 `author_kind=yuki`。
- mention 目标属于同平台任一 Yuki Presence 时，`mentions_yuki=true`。
- reply 优先根据被引用 canonical event 的 `author_kind=yuki` 判断。
- 找不到 canonical 引用时才使用 Presence 外部 ID 集合作保守判断。
- 工具、Memory、自省、cadence、展示层不得再使用 `sender_user_id == bot_user_id`。
- 第三方 bot 为 `external_bot`，不得创建 Person、关系或人物记忆。

---

## 4. WebUI-ready 控制面

### 4.1 边界

新增 transport-neutral 控制面，但不增加 HTTP 路由。

依赖方向：

```text
未来 HTTP / 当前 CLI / 当前 QQ 命令
                ↓ adapter
      ControlPlaneBundle / Application Services
                ↓
       repositories / runtime registries
```

规则：

- `control_plane` 不得 import CLI、matcher、command renderer 或 ORM model。
- ORM 查询实现放在 persistence adapter 中，通过 Query Port 返回 DTO。
- 核心业务服务不得接受 `AdminActor`。
- QQ 的正文、@、当前群证明只存在于 QQ adapter 的 `TargetResolver`。
- adapter 将真实事件解析为 `ControlPrincipal + DecisionContext + canonical target`。
- 禁止为了复用旧服务而伪造 `AdminActor(is_superuser=True)`。

### 4.2 Principal 与 RBAC 扩展点

`ControlPrincipal` 至少包含：

- `principal_id`
- `person_id | None`
- `source=qq|cli|future_web|system`
- roles
- granted capabilities
- authenticated/active 状态

当前实现：

- 每个 `SUPERUSERS` 外部 QQ 经 IdentityBinding 解析为 Person Principal。
- 不要求只能配置一个超管。
- 找不到 Binding 时返回 `not_found`，不得合成假 Person。

未来 WebUI：

- 登录认证只需产生同样的 `ControlPrincipal`。
- 可增加角色和持久 grant，不需要重写业务服务。
- 本轮不创建完整 RBAC 管理表。

Capability 来源继续复用：

- PermissionCatalog
- ActionRegistry
- ConfigRegistry
- 已有插件/自动化能力元数据

只新增身份、路由、控制面自身的 capability descriptor；禁止第四套独立 Capability Registry。

### 4.3 稳定控制协议

所有 Query/Command 使用结构化 DTO：

- `PageRequest`
  - opaque cursor
  - 默认 limit 20，最大 100
  - 禁止 offset

- `Page[T]`
  - items
  - next cursor
  - 可选 snapshot timestamp

- `DecisionContext`
  - principal
  - request ID
  - source
  - canonical target
  - reason
  - correlation ID

- `ControlCommand`
  - `request_id`
  - `expected_revision`
  - payload

- `ControlResult`
  - success
  - resource ID
  - revision
  - audit ID
  - effective state
  - optional `OperationRef`

统一错误码至少包含：

- `unauthenticated`
- `capability_denied`
- `not_found`
- `validation_error`
- `version_conflict`
- `idempotency_conflict`
- `binding_ambiguous`
- `route_ambiguous`
- `route_paused`
- `populated_merge_forbidden`
- `legacy_identity_forbidden`
- `pending_cutover`
- `state_mismatch`
- `precondition_failed`
- `secret_not_readable`
- `operation_unavailable`

中文提示只属于 QQ/CLI renderer，不是稳定 API 契约。

### 4.4 乐观并发、幂等和审计

- Person、Space、Presence、Binding、路由使用 `expected_revision`。
- 路由自动接管另使用 `expected_route_generation`。
- Config 对现有 version 增加客户端必填的 expected version。
- Conversation reset 使用 expected conversation generation。
- 每个 mutation 都必须在业务事务中同步写审计和 command receipt。
- 禁止业务先提交、审计后补。
- 长任务不新建通用执行器；复用 backfill、rebuild、dream、automation 等现有 run/job。
- 控制面统一投影为 `OperationRef`：
  - queued/running/succeeded/failed/cancelled
  - progress
  - state epoch `v1|v2`
  - sanitized error category
  - created/updated timestamps

### 4.5 敏感信息

Capability 分层：

- metadata read
- external ID / PII read
- content read
- mutate
- destructive

默认规则：

- Person/Space metadata 不返回完整 QQ/群号。
- 完整 Binding external ID 需要 `identity.binding.read_external`。
- 会话列表默认不返回正文。
- 原始事件正文需要 `conversation.content.read`。
- Memory 正文、evidence excerpt 需要 `memory.content.read`。
- secret 永远不返回，即使是超级管理员；只返回 `configured=true|false`。
- 不返回 token、cookie、API key、数据库 URL、完整本地路径、provider 原始回执、模型 reasoning。
- `/healthz` 继续保持公开的瘦健康载荷，不加入管理字段。
- 管理健康由鉴权后的 Control Query 提供。

### 4.6 资源覆盖

控制面至少提供以下查询和命令：

| 资源 | 查询 | 命令 |
|---|---|---|
| System/Yuki | 版本、runtime state、健康、队列、pending restart | 无 Yuki 重建 |
| Person | 分页、详情、关系、权限、绑定、成员关系 | enable/disable、附加空 Binding、forget |
| Space | 分页、详情、Binding、成员、设置 | enable/disable、autonomous、require mention、附加空 Binding |
| Presence | 状态、能力、连接快照 | 注册、启停、ingest eligibility |
| Routes | 三张路由状态、generation、歧义原因 | 设定、pause/resume、reconcile |
| Conversation | 分页、generation、rollup、路由、事件元数据 | 显式 new/stop |
| Config | specs、effective、history、pending restart | set/unset/rollback |
| Audit | 按 principal/resource/capability/time 分页 | 无篡改 |
| Memory | fact、evidence、conflict、health、job | mutation、rebuild、dream、maintenance |
| Relationship/Preference | 查询与历史 | set/adjust/delete |
| Automation | 列表、脚本、运行历史、状态 | create/update/pause/resume/cancel/run-now |
| Plugin | 安装状态、权限、doctor、outbox | approve/enable/disable/retry |
| MCP | server、tool、health、cache | enable/disable/refresh/reconnect |
| Emoji/Speech | asset/profile/job/health | 生命周期管理 |
| Operations | 全部长任务状态 | cancel/retry（仅底层支持时） |

明确不进入控制面：

- 任意 `call_onebot_api`
- MCP 任意 tool call
- plugin arbitrary run
- raw SQL
- secret 读取/修改
- NapCat token 配置渲染
- 任意 provider 私有 action

---

## 5. 迁移状态机

### 5.1 v1 Expand

- 旧表和旧键仍是真源。
- 旧写入同时 dual-write nullable canonical shadow。
- canonical 预配置允许写入，但无法在 legacy 中等价表达的 Binding、SpaceBinding、Presence 路由命令返回 `pending_cutover`。
- 禁止插入假 `people`、`groups` 或 `conversation_scopes` 让 v1 看似生效。
- v1 查询可以读取 canonical projection；缺值时由专门 adapter 回退 legacy，不允许 Control Query 自己读 ORM。

### 5.2 分类回填

分类规则：

- 只作为 Yuki `bot_user_id/self_id` 出现的账号 → Presence。
- ignored bot → external_bot，不创建 Person。
- 人类 sender、private peer、member、已配置 superuser → Person + IdentityBinding。
- legacy group → Space + SpaceBinding。
- 同一外部账号同时具备冲突身份时写 `identity_conflicts` 并停止该对象回填。
- Settings 中未落库的 SUPERUSERS/ENABLED_GROUPS 可以生成 canonical 空 Binding，但不得生成 legacy 假行。
- 回填必须幂等；二次运行零业务 diff。

### 5.3 Cutover plan

`identity-cutover --plan` 必须：

1. 验证生产目标 revision。
2. 验证服务已停并记录数据库、WAL、SHM 快照。
3. 排空 memory jobs、reflection pending、rollup jobs。
4. 验证 automation、plugin outbox、dream、rebuild、mutation 没有 processing lease。
5. 验证 canonical shadow 完整。
6. 验证所有 pending Binding/路由在 v2 可一次生效。
7. 分类多 Presence 历史重复。
8. 生成 migration rollup。
9. 生成不可变 source manifest 和摘要指纹。
10. 有任何 conflict、未知身份或未解决路由时失败关闭。

旧 Scope 合并：

- 不取 min/max `starts_after_event_id`。
- 读取每个旧 Scope 的有效 semantic rollup 和 raw suffix。
- 仅在同 Space、同 sender Binding、同 platform message ID、同 event type、同规范化正文/segments、同平台时间完全一致时 suppress。
- 同 platform message ID 但内容冲突时阻断。
- 不同 message ID 视为独立事件。
- 生成一条 migration rollup：有界、确定性、按 Scope 公平分配证据，禁止尾部截断；digest 覆盖实际落库表示。
- 新 Conversation 从 cutover watermark 开始空 raw suffix。
- 原始账本行全部保留。
- 旧事件绝不重新进入 Memory worker。

### 5.4 Cutover apply

`identity-cutover --apply <manifest>`：

- 在单个 `BEGIN IMMEDIATE` 中重新验证 source fingerprint。
- 写 canonical Conversation、aliases、routes、event mapping、worker baselines。
- 让 pending canonical 预配置正式生效。
- 最后翻转 `identity_runtime_state=v2`。
- 任何失败整事务回滚。
- v2 二进制遇到 v1 状态拒绝启动；v1 二进制遇到 v2 状态拒绝启动。
- apply / cutover 完成后的数据回滚只能恢复 cutover 前同一时间点 DB/WAL/SHM 快照；禁止 Alembic downgrade 与 git revert。apply 前的 0048 schema reverse 见 Bundle 3。

---

## 6. Commit 实施合同

每次只允许 Grok 完成一个 Commit。

### C0 — `docs(security): freeze permanent-yuki migration contract`

首个安全性 Commit，必须先于任何生产代码：

- 删除旧任务书并写入本任务书。
- 新增只读架构扫描和现状测试。
- 冻结 `/healthz` 路由和无敏感字段形状。
- 冻结 TargetResolver 的真实事件证明。
- 冻结 secret 不可读、SUPERUSERS 不可通过控制面修改。
- 冻结 raw OneBot 仅限事件绑定超管路径。
- 加入未来 `control_plane` 禁止 import CLI、matcher、command renderer、ORM 的静态门。
- 禁止修改生产 `src`、schema、运行时行为。
- 不提交 `.env`、`data/`、`.cursor/`、`tmp/`。

验收：现有完整测试、ruff、mypy 全绿，生产路径 diff 为零。
回滚：普通 revert。

### C1 — `refactor(domain): add canonical identity value types`

- 增加 Person/Space/Binding/Presence/Conversation/Principal/Request UUID 类型。
- 增加 `AuthorKind` 四态。
- 分离 ConversationGeneration、RouteGeneration、ConnectionGeneration 类型。
- 增加 transport-neutral DecisionContext 基础类型。
- 不修改旧 ConversationScope。

验收：非法 UUID、类型误用、循环 import 测试。
回滚：普通 revert。

### C2 — `feat(identity): add canonical identity foundation`

Alembic `0043`：

- 建 `persons`、`identity_bindings`、`spaces`、`space_bindings`、`presences`。
- 建 `identity_runtime_state=v1`、`identity_backfill_runs`、`identity_conflicts`。
- 增加 revision、状态和唯一约束。
- 不建 Yuki 表。
- 将所有新表加入 0005 的未来表排除集。
- 更新 Alembic head 与 fresh-install 检查。

验收：0005 阶段没有未来表；head 有完整新表；fresh 与 0042 升级 schema 等价。
回滚：downgrade 0043 + revert。

### C3 — `feat(conversation): add canonical conversations and routes`

Alembic `0044`：

- 建 `canonical_conversations`。
- 建多对一 `conversation_legacy_aliases`，每 Conversation 恰好一个 primary。
- 建三张正式路由表。
- 建 `control_command_receipts`。
- 不建 connection 表、cutover 表或泛化路由。

验收：多 alias 成功、双 primary 失败、路由跨平台引用不一致失败。
回滚：downgrade 0044。

### C4 — `feat(ledger): add canonical event shadows`

Alembic `0045`：

- 给 chat events、旧 scope 增加 nullable canonical event/conversation/author/presence 字段。
- 保留旧唯一约束和旧 insert。
- `author_kind` 只接受四态。
- origin/event_kind 独立。

验收：全部旧 ledger 测试不变；plugin/automation 不得写入 author kind。
回滚：downgrade 0045。

### C5 — `feat(identity): add person and space ownership shadows`

Alembic `0046`：

- Memory fact subject/visibility。
- Relationship、alias、membership。
- Person/group setting、time、speech preference。
- 只加 nullable shadow，不改旧唯一键或 hash。

验收：旧 Memory、关系、偏好测试全绿。
回滚：downgrade 0046。

### C6 — `feat(identity): add extension ownership shadows`

Alembic `0047`：

- Automation。
- Plugin state/session/grant/outbox/background job。
- Runtime config scope。
- Emoji、speech、MCP、observability correlation。
- 不改旧 UNIQUE 或业务读取。

验收：插件 API 2.0、自动化、配置、emoji、speech 测试全绿。
回滚：downgrade 0047。

### C7 — `feat(identity): add audited backfill preflight`

- 实现只读 `--dry-run` 和幂等 `--apply`。
- 按身份分类规则回填。
- 使用 `identity_backfill_runs` 记录进度，不依赖尚未实现的 Control Operation。
- 输出无正文、无 secret 的冲突报告。
- 支持测试库擦除 canonical 回填结果，生产不提供破坏性 erase。

验收：Yuki QQ 不成为 Person；ignored bot 不成为 Person；二次 apply 零 diff；冲突失败关闭。
回滚：revert 服务；v1 不读取这些数据。

### C8 — `feat(identity): dual-write canonical identity shadows`

- 盘点所有 legacy people/groups/events/scope 写点。
- v1 写入同步填 canonical shadow。
- 旧读者完全不变。
- `/ai forgetme` 同时清除 canonical shadow，防止幽灵 Person。
- canonical 预配置不得制造 legacy 假行。

验收：AST 写点覆盖；v1 行为与基线一致；dual-write 漏写夹具失败。
回滚：revert；C7 可重建 shadow。

### C9 — `feat(control): define principal and control contracts`

- 建立 ControlPrincipal、PolicyDecision、DecisionContext。
- 建 Page/Cursor、Problem、ControlCommand、ControlResult、OperationRef。
- 建 Capability 投影，不建立第四 Registry。
- 支持多个 SUPERUSERS Principal。
- 本 Commit 零 I/O、零 schema。

验收：权限矩阵、错误码冻结、import 守卫。
回滚：普通 revert。

### C10 — `feat(control): add paged read projections`

- Query Port 放在 control plane。
- ORM 实现在 persistence adapter。
- 提供 System、Yuki synthetic summary、Person、Binding、Space、Presence、Conversation、三路由、audit、backfill operation 查询。
- GatewayConnection 在 C16 前返回 unavailable snapshot，不扫描 `get_bots()` 冒充真相。
- 所有 list 使用稳定 cursor 排序。

验收：空页、翻页无重复/漏项、PII 默认遮盖、control plane 无 ORM import。
回滚：普通 revert。

### C11 — `feat(control): add identity and route commands`

- 附加空 IdentityBinding/SpaceBinding。
- 注册、启停 Presence，设置 ingest eligibility。
- 修改 Person/Space enabled。
- 管理三张路由的目标、paused、revision。
- 禁止 populated Person/Space merge。
- mutation、audit、command receipt 同事务。
- v1 无法等价表达的命令返回 `pending_cutover`，不制造 legacy 假行。

验收：expected revision、request idempotency、populated merge 拒绝、route change 不改 Conversation。
回滚：revert 命令；canonical 预配置可保留待 cutover。

### C12 — `refactor(admin): remove transport actors from core services`

- Relationship、Group、PrivateAccess、Preference 核心服务改吃 Principal + canonical target。
- 修改所有调用方：QQ command、AdminCapability、plugin facade、automation handler。
- QQ adapter 保留当前消息/@/群的证明。
- 删除核心服务中的 AdminActor 和 Settings.superusers 鉴权。
- 禁止任何 `is_superuser=True` 伪造入口。

验收：core grep 无 AdminActor；QQ TargetResolver 安全测试仍绿；插件不得自报超管。
回滚：原子 revert 本 Commit。

### C13 — `feat(control): expose config and audit services`

- Config Query/Command。
- set/unset/rollback 强制 expected version。
- Audit Query 强制 Principal capability。
- 敏感 Config 只返回 configured。
- 删除 RuntimeConfigService 内部伪造 actor 的公共使用路径。

验收：并发冲突、secret、审计同事务、分页。
回滚：普通 revert。

### C14 — `feat(control): expose memory operations`

- 复用现有 Memory Query Plane、Mutation、rebuild、dream、maintenance。
- 区分 metadata/content/mutate capability。
- 长任务返回 OperationRef。
- 不改变 Memory key 和 worker 读取。

验收：无 content 权限不返回 excerpt；mutation 幂等；长任务可查询。
回滚：普通 revert。

### C15 — `feat(control): expose remaining management resources`

- Automation、Plugin、MCP、Emoji、Speech、管理健康的 Query/Command。
- 明确排除 MCP call、plugin run、raw OneBot。
- 管理健康暂不改变连接语义，连接真相在 C16/C18 接入。

验收：Capability 目录不包含三类危险能力；公开 `/healthz` 无变化。
回滚：普通 revert。

### C16 — `feat(gateway): register connections by presence`

- 实现内存 GatewayConnectionRegistry。
- NapCat connect/disconnect 生命周期绑定 Presence。
- 不同 Presence 多账号并存。
- 每 Presence 唯一 active connection；同 Presence 多连接歧义失败关闭。
- Control Query 接入运行时快照。
- Bot 句柄、连接状态不落库。
- 暂不迁调用方。

验收：0/1/多连接、重连 generation、不同 Presence 并存、数据库无连接对象。
回滚：普通 revert，无 schema。

### C17 — `feat(ingress): add gated canonical message pipeline`

- 实现仅在 state=v2 注册的 ingress resolver。
- Person/Space/Presence 解析。
- author kind 四态。
- 任一 Presence mention/reply。
- 群 ingest fence。
- fence、receipt、append 同事务。
- 私聊绕过群 fence。
- state=v1 matcher 行为完全不变。

验收：v1 黄金测试；v2 双 Presence/同 Person/同 Space/非 ingest drop。
回滚：revert dormant v2 路径。

### C18 — `feat(routing): enforce deterministic presence routing`

- 路由 monitor、可达探针、CAS takeover。
- 主动发送先读 Person/Space route，再向 Registry 请求 Presence connection。
- 迁走 automation gateway、plugin notification、health 中的 `get_bots()` 选连。
- 接管只修改对应业务 route generation。
- Registry 切连接只修改 connection generation。
- Conversation generation 永远不变。

验收：0/1/>1 候选；路由与连接 generation 隔离；无 `get_bots()` 首项选择。
回滚：调用方仍只能退回 Registry 单连接兼容模式，不得恢复字典首项。

### C19 — `feat(conversation): hydrate canonical runtime state`

- canonical Conversation/Event hydrate。
- turn coordinator、effect gate、cadence、rollup、reply hydrate 的 gated v2 实现。
- 固化插件 primary legacy alias。
- Memory partition 与 Conversation ID 仍分族，不得混用。

验收：多 alias 同 Conversation；alias 换 Presence 不变；v1/v2 对拍。
回滚：普通 revert。

### C20 — `feat(identity): dual-write person and space state`

- Person/Space enabled、SUPERUSERS/ENABLED_GROUPS bootstrap。
- aliases、memberships、time、speech preference、relationship。
- v1 继续读 legacy；v2 reader gated。
- forgetme 保持 Person 级完整删除。

验收：v1 准入不变；v2 任一 Binding 继承权限/关系/设置。
回滚：revert dual-write reader/writer。

### C21 — `feat(memory): add canonical memory ownership`

- Memory fact subject、SELF visibility、jobs、reflection、dream、tool receipt 增加 canonical 所有权。
- 新 hash/cursor 使用独立 canonical 列。
- v1 worker 继续旧键。
- v2 禁止旧事件重新 enqueue。
- migration rollup 不写 Memory job。

验收：v1 去重不变；v2 重放被拒；SELF 不随 Presence 变化。
回滚：普通 revert，旧键仍在。

### C22 — `feat(automation): route canonical targets at send time`

- Automation target 改为 Person/Space。
- `bot_user_id` 仅 provenance。
- v2 发送时解析当前 Person/Space route → Presence → Registry。
- 换 Yuki QQ 并更新路由后，旧任务继续通过新 Presence 发送。
- v1 仍使用 legacy 发送路径。

验收：旧任务换 Presence 后仍工作；route paused 不发；权限委托按 Principal 重验。
回滚：普通 revert。

### C23 — `feat(plugins): add canonical identity projections`

- Plugin API 保持 2.0。
- SDK 增加可选 person/space/conversation/presence ID。
- 旧插件无需修改。
- Host state/grant/outbox/background dual-write canonical target。
- SDK `conversation_key` 始终输出固定 primary legacy alias。

验收：旧插件合同测试；换 Presence 后 alias 不变；Host state 不分裂。
回滚：普通 revert。

### C24 — `feat(identity): complete canonical scope shadows`

- 完成 Config、Emoji、Speech、MCP、observability 的 canonical scope。
- 控制面投影读 canonical shadow。
- v1 仍可回退 legacy。
- 暂不修改旧 UNIQUE。

验收：所有 scope inventory 有明确所有者；不存在未分类 QQ/group key。
回滚：普通 revert。

### C25–C27 合并为 Bundle 3 — `feat(migration): cut over permanent-yuki identity safely`

最终 cutover/release 一枚 Commit，parent 必须是已验收 Bundle 2。apply / cutover 之后禁止再用 Alembic downgrade 或 git revert 当数据回滚。

Alembic `0048`：

- 建 `identity_cutover_manifests` / `identity_cutover_runs`。
- SQLite table-rebuild 去掉业务表对 `people` / `groups` 的 carrier FK；`conversation_scopes.id` 子表保留。旧 unique 改为 `WHERE canonical_event_id IS NULL`。
- apply 前：仅当 `identity_runtime_state=v1` 且不存在成功 apply 记录时，允许 Alembic `0048` downgrade。它必须忠实还原当时完整的 0047 schema：全部 people/groups carrier FK、精确 ON DELETE / ON UPDATE、索引、触发器与 FTS（含 `uq_chat_events_bot_platform_message` 的 0047 全表 UNIQUE）。DDL 前校验无重复 `(bot_user_id, platform_message_id)`、无缺失 parent / 孤儿行，失败关闭。这是 schema reverse，不是 cutover 后的数据回滚。
- apply / cutover 后：数据回滚只恢复 `--plan` 记录的同一时间点 DB/WAL/SHM 快照。禁止 Alembic downgrade，禁止 git revert。
- 0005 继续排除 cutover 表，并内联恢复历史 people/groups FK。

`identity-cutover --plan`：

- 校验目标 git revision、停机 token、DB/WAL/SHM 快照文件。
- 排空 memory/reflection/rollup pending 与 processing，以及 automation/plugin outbox/dream/rebuild/emoji/relationship/embedding 的 processing lease。
- 校验 canonical shadow、open `identity_conflicts`、未知 identity、route ambiguity、`problem_code=pending_cutover`。
- 多 Presence 重复：仅完全相同 Space、sender Binding、platform message ID、event type、规范正文/segments、平台时间才 suppress；同 message ID 内容冲突阻断；不同 message ID 不合并。
- 读取各 legacy Scope 有效 semantic rollup 与 raw suffix，按有界、确定性、按 Scope 公平分配的证据生成 migration rollup 与 cutover watermark；禁止尾部截断；digest 覆盖实际落库表示。原 ledger 保留，旧事件不重新 enqueue Memory。
- 生成不可变、可重算 source fingerprint；plan 不翻 `identity_runtime_state`，不改 legacy 真源。重复 plan 稳定。

`identity-cutover --apply <manifest>`：

- 单个 `BEGIN IMMEDIATE` 复核 fingerprint。
- 写 canonical conversations/aliases/routes/event mapping/worker baselines。
- 已存在的 canonical 预配置在 flip 后正式生效；未解决的 `pending_cutover` 回执必须在 plan 阶段阻断。
- 最后翻转 singleton `identity_runtime_state=v2`。
- 任一 failpoint 整事务回滚，数据库签名与 apply 前一致。
- `IDENTITY_BINARY_EPOCH`（不是产品版本号）：v2 binary 遇 v1 拒绝启动；v1 binary 遇 v2 拒绝启动。仅挂生产 `main.startup`。

Complete-v2 运行时：

- 禁止创建/更新 `people`、`groups`、`conversation_scopes`。`ensure_runtime_people_row` / `ensure_runtime_group_row` fail closed。
- automation / plugin / QQ ingress / canonical append 成功且上述三表行数不增。
- 新人只建 Person + active IdentityBinding；第三方 bot 不建 Person。
- 未知群不得自动建 SpaceBinding；未知 Yuki Presence 不得自动注册。
- plugin 外部事件与 outbox 走 canonical Conversation/Event，不得 `get_or_create` legacy scope。
- 旧 / 未知 external ID 只做 provenance；若该 ID 指向另一个已知 Person/Space，发送 fail closed（`target_mismatch`）。
- `forgetme` / `delete_person` 不再依赖 people CASCADE；应用层删除 memberships、aliases、relationship/speech/time 子行与可归属 `chat_events`。

Issue #51 Rollup 安全合同（当前运行时；出处 https://github.com/YuanYeYouTao/Yuki-QQbot/issues/51）：

- 默认 `summary_max_characters` 从 1200 提到 2400 只是预算余量，不是语义修复。
- 模型失败、origin 排除或前台溢出时，写入独立、有界的 EMERGENCY overlay；永不推进 semantic checkpoint coverage。
- Prompt 可以使用有效 overlay；后台 semantic worker 必须从 semantic checkpoint + 永久账本重建，不得以 overlay 正文当唯一来源。部分追上只推进 overlay 的 base revision；最终追上在同一事务原子删除 overlay。
- generation 不匹配、reset、forget、delete 必须清除过期 overlay。status 分离 semantic / overlay / effective coverage，永不暴露 summary 正文。
- migration rollup 使用有界、确定性、按 Scope 公平分配的证据，禁止尾部截断；digest 覆盖实际落库表示。

apply / cutover 完成后的数据回滚：**只允许**恢复 `--plan` 记录的同一时间点 DB/WAL/SHM 快照。禁止 Alembic downgrade、禁止 git revert、禁止用产品版本回退当数据回滚。apply 前的 0048 downgrade 只做 schema reverse，见上方 Alembic `0048` 合同。

验收命令（必须全绿，进程环境 `LLM_THINKING_ENABLED=true`、`LLM_REASONING_EFFORT=high`）：

- 本地 ruff：`uv run ruff format --check . --exclude tmp --exclude .cursor` 与 `uv run ruff check . --exclude tmp --exclude .cursor`。CI 干净 checkout 没有用户 `tmp/` / `.cursor/`，可用 `uv run ruff format --check .` 与 `uv run ruff check .`。本地 exclude 只为保留这些用户文件、避免扫进门禁。禁止只扫 `src tests`：会漏掉 `migrations/`、`scripts/`、`examples/`。
- `mypy src`
- 全套 pytest
- Alembic 空库 head = `0048`
- `0042`→head 与 fresh schema 等价
- 历史 0032 门：`tests/unit/test_memory_migration_matrix.py` 的 `MATRIX['memory-dream']='0032'` 路径（0032→0041→head），不是独立 golden `.db`
- `PRAGMA foreign_key_check`
- identity cutover plan / apply / failpoint / snapshot rollback
- `git diff --check` 与 secret / 禁项扫描
- `web_search` / `mcp.web_search` 仍为合法固定 MCP 工具；公共 `/healthz` 形状不变

回滚：未 apply 且 `identity_runtime_state=v1`、无成功 apply 时，允许按上方合同执行 0048 schema downgrade，仓库可用普通 revert。**已 apply 的生产数据只能恢复快照**，禁止 Alembic downgrade 与 git revert。

### 三 Bundle 实际边界

| Bundle | Hash | Subject | 边界 |
| --- | --- | --- | --- |
| 1 | `94c0696003c0086e85d2831e3189104694a77fef` | `feat(control): complete transport-neutral management plane` | 原 C12–C15。控制面吃真实 principal；无管理 HTTP；`web_search` / `mcp.web_search` 合法。 |
| 2 | `ea25bc028cc9fae26281d46835e9e8938804b965` | `feat(identity): activate canonical multi-presence runtime` | 原 C16–C24。多 Presence 运行时。当时仍用 `ensure_runtime_*` 与 `conversation_scopes` 顶 SQLite FK，由 Bundle 3 拆除。 |
| 3 | `本 Commit（见 Git 历史）` | `feat(migration): cut over permanent-yuki identity safely` | 原 C25–C27。0048 + plan/apply + 去掉 legacy carrier FK + v2 只读合同 + Issue #51 rollup 安全合同。parent 为最终 Bundle 2 `ea25bc028cc9fae26281d46835e9e8938804b965`。 |

剩余已合并工作按上述三枚本地 Bundle/Commit 验收。最终纠偏只 amend Bundle 3 / HEAD，不另开第四枚 Commit，不推送、不部署、不涨产品版本。不夹带 `.cursor/`、`tmp/` 或无关的用户 `docs/architecture/Yuki-3.7.0-群聊统一会话与Rollup重构任务书.md`。

恢复步骤：

1. 停 Bot，确认没有第二实例连接 SQLite。
2. 保存故障现场副本。
3. 只恢复 plan 记录的 DB/WAL/SHM 三件套（校验 `db_sha256` / size）。
4. 需要回到 v1 运行时，必须使用 v1 binary 对着已恢复的 v1 快照；v2 binary 会拒绝 v1 状态。
5. 已 cutover / apply 后不要 `alembic downgrade`，不要 `git revert` 已 cutover 的数据目录。

---

## 7. 指挥与验收协议

每个 Commit 严格执行：

1. 指挥官向 Grok 提供该 Commit 的唯一任务边界、允许文件族、禁止项和验收命令。
2. Grok 只实现当前 Commit，自行运行测试并创建 Git commit。
3. Grok 报告：
   - commit hash
   - 修改文件
   - schema/行为变化
   - 执行的测试及结果
   - 未解决风险
4. 指挥官只读检查：
   - `git show --stat --oneline`
   - 完整 diff
   - `git diff --check`
   - 工作树状态
   - secret/PII 泄露
   - 越界文件
   - 目标测试、ruff、mypy
5. 验收失败时，在同一个 Grok session 中要求修正并 amend；指挥官不亲自编码。
6. 当前 Commit 未通过，禁止开始下一 Commit。
7. Grok 长时间运行时耐心等待；CLI 会话失活才中止并使用 `--continue` 提交报告。
8. 不并行修改共享工作树，不 squash 已验收 Commit，不夹带 `.cursor/`、`tmp/` 或用户无关修改（含无关的用户 3.7.0 任务书）。
9. C27 后再做一次全库 Grok 红队审计；未通过不允许 push、deploy 或发布。剩余已合并工作按三枚本地 Bundle 验收，最终纠偏 amend Bundle 3 / HEAD，不另开第四枚 Commit。

通用测试门：

- 本地 ruff：`uv run ruff format --check . --exclude tmp --exclude .cursor` 与 `uv run ruff check . --exclude tmp --exclude .cursor`。CI 干净 checkout 可用 `uv run ruff format --check .` 与 `uv run ruff check .`；本地 exclude 保留用户 `tmp/` / `.cursor/`。禁止只扫 `src tests`，以免漏掉 `migrations/`、`scripts/`、`examples/`。
- `mypy src`
- 目标 pytest
- schema/runtime Commit 跑完整 pytest
- Alembic 空库升级
- 0042 等价升级
- 历史 0032 门：`tests/unit/test_memory_migration_matrix.py` 的 `MATRIX['memory-dream']='0032'` 路径（0032→0041→head），不是独立 golden `.db`
- foreign key check
- `git diff --check`
- 现有 memory quality/release validation
- 无 secret、token、`.env` 进入 Git

---

## 8. 明确假设与非目标

- 当前只生成本地 Commit，不推送云端、不部署、不涨产品版本。剩余已合并工作按三枚本地 Bundle 验收，最终纠偏 amend Bundle 3 / HEAD，不另开第四枚 Commit。
- 本轮不实现 WebUI、HTTP 管理 API、登录、Cookie、CSRF 或前端。
- 未来 WebUI 必须只依赖 ControlPlaneBundle；禁止直接读 ORM。
- 本轮不自动合并两个已有数据的 Person 或 Space。
- 人类换号只能给已有 Person 附加空 Binding；已有双方数据时返回冲突。
- Yuki 换号不受上述限制：新增 Presence 即继承整个 Yuki。
- SnowLuma provider 方言、同 Presence 双网关 probe 和部署 overlay 不在本任务书实现范围。
- 不宣称任何网关能降低腾讯封号风险。
