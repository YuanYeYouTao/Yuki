# 管理 API 与 CLI

管理入口为原 Bot HTTP 服务的 `/admin/v1`。`yuki admin` 与在线
`yuki plugin` 调用这个入口，不启动另一个 Bot 或直接写运行中的数据库。
CLI 的 `ADMIN_API_URL` 默认 `http://127.0.0.1:8080`；它只决定客户端连接地址，
HTTP 监听与部署端口继续沿原服务器配置。

基础 Compose 将 `webui-config/` 只读挂载。需要在线保存模型连接时，在本地 Compose
override 中将同一目录挂载为可写（`./webui-config:/app/webui-config`）；保留该既有路径。
未授予文件写权限时，API 不会自行改动部署挂载。

## 身份与权限

服务器通过 `CONTROL_OPERATORS_FILE` 读取 operator TOML，格式见
[`config/control-operators.example.toml`](../../config/control-operators.example.toml)。
实际 token 放在声明所指的服务器环境变量中。CLI 从 `YUKI_CONTROL_CREDENTIAL`
取得同一凭据，以 `Authorization: Bearer …` 逐次认证。未配置 operator 时拒绝认证；
operator 角色只作描述，能力逐项声明；Person 超管映射到明确的有限管理能力，凭据轮换立即影响后续请求。operator 不能凭管理权限以某个 Person 发言。

旧浏览器管理入口、前端与旧控制面查询层已从源码退役。管理 API 使用同一个模型文件服务
与原领域服务，不创建浏览器登录会话，也不需要 Cookie 或 CSRF token。

## 方法与 CLI

状态命令使用 UUID4 `request_id` 与非负整数 `expected_revision`；下述身份关联类操作只使用原请求 UUID，不接受请求方自报主体。
返回 `data`；命令含 `success`、资源 ID、revision、审计 ID 与公开结果，外部命令可含原 operation。

| 方法 | CLI／消费者 | 权限 |
| --- | --- | --- |
| `GET /health` | `admin health` | `control.health.read` |
| `GET /operations/{id}` | `admin operation <id>` | `control.operation.read` |
| `GET /traces/{turn\|work\|event}/{id}` | `admin trace <kind> <id>` | `control.execution.metadata.read`；`content=true` 另需 `control.execution.content.read` |
| `GET /persons`、`GET /bindings`、`GET /presences`、`GET /works` | 各领域 `list --after <cursor> --limit <n>` | 对应人物/绑定/Presence/Work 读取能力 |
| `GET /presences/{id}`、`POST /presences/{id}` | `admin presence show\|enable\|disable\|ingest` | `identity.presence.read\|set_ingest` |
| `GET /works/{id}` | `admin work show <id>`，含当前 revision | `control.work.read` |
| `POST /persons/{id}/enable`、`POST /bindings/{id}/enable` | `admin person\|binding enable` | `identity.person.enable\|identity.binding.enable` |
| `POST /persons/{id}/superuser/{grant\|revoke}` | `admin person superuser grant\|revoke <id>` | `identity.person.superuser` |
| `POST /persons`；`GET /persons/{id}`；`POST /persons/{id}/disable` | `admin person create\|show\|disable` | 对应 `identity.person.create\|read\|disable` |
| `POST /persons/{id}/bindings`；`GET /bindings/{id}`；`POST /bindings/{id}/disable` | `admin binding attach\|show\|disable` | 对应 `identity.binding.attach\|read\|disable` |
| `POST /persons/{id}/client-pair`；`POST /devices/{id}/revoke` | `admin client pair --person <id>`；`admin client revoke <id>` | `identity.client.pair\|revoke` |
| `POST /bindings/{id}/transfer` | `admin binding transfer <id> --to-person <id>` | `identity.binding.transfer` |
| `POST /persons/{id}/forget` | `admin person forget <id>` | `identity.person.forget` |
| `POST /persons/{id}/merge`；`POST /persons/{id}/rename` | `admin person merge <id> --to-person <id>`；`admin person rename <id> <name>` | `identity.person.merge\|rename` |
| `GET /plugins`、`GET /plugins/{id}` | `plugin list\|inspect\|permissions`、部署向导 | `control.plugin.read` |
| `POST /plugins/{id}/actions/{discover\|approve\|enable\|disable\|doctor}` | 同名 `plugin` 命令、部署向导 | `control.plugin.mutate` |
| `GET /plugins/{id}/configuration`；`POST` 同路径 | `plugin configuration\|configure` | `control.plugin.config.content.read\|mutate` |
| `GET /plugins/{id}/observe` | `plugin observe` | `control.plugin.config.content.read` |
| `POST /plugin-outbox/{id}/retry` | `plugin retry-outbox` | `control.plugin.mutate` |
| `GET /models`；`POST /models/apply`；`POST /models/secrets` | `admin model show\|apply`、`admin model secret set` | `control.config.file.content.read\|mutate` |
| `POST /works/{id}/{cancel\|resume}` | `admin work cancel\|resume` | `control.work.mutate` |

健康投影分别报告 enabled、running、healthy 和检查时间；没有健康证据时为 null。
轨迹查询使用内部事件 ID 与原 turn/Work ID，按有效期过滤，使用 `after` 与 `limit`
续读；正文单独授权。它不读取私有恢复 journal，也不制造恢复或重新发送资格。

新建 Person 的 revision 为 1；挂接绑定使用该 Person 当前 revision，仅接受没有当前 Binding 记录的
平台账号，且不能把 Yuki 当前启用的 Presence 挂成 Person。`app` 绑定由设备配对生成，不能手动挂接。
转移为原绑定保留旧 Person 与会话，另建目标 Person 的绑定和会话；旧主动路由暂停。
服务器生成的 `app/acp` 绑定不可手工挂接或转移。disabled 外部账号可合法转移；已转移的历史 Binding 不恢复旧时段。停用保持历史、Memory、Work 和回执归属。平台标识沿原通用字符串合同。
配对码、撤销、转移与遗忘使用 `{request_id}`，转移另需 `to_person`，不需要 revision。
配对码明文只返回一次；相同请求重放返回原码 ID 与到期时间，不能取回明文。

## 人物归并与名称

`POST /persons` 可带 `display_name`，缺省为空；名称不是主键，允许重名和没有账号。
`POST /persons/{id}/merge {request_id,to_person}` 需 `identity.person.merge`；
`POST /persons/{id}/rename {request_id,display_name}` 需 `identity.person.rename`。
两者调用共同人物领域操作，当前查询解析最终人物，原请求按原 UUID/参数返回原回执。
归并和改名不创建 App、不切换入口、不重写 Conversation、原事件或已付输入。
`GET /persons/{id}` 同时返回当前 `id`、原查询 `source_person_id`、主名和当前 `revision/enabled`，
另以 `source_revision/source_enabled` 展示该原来源的真实状态。显式 `enable` 使用该来源的
`source_revision`，恢复指定来源并启用当前人物；其他已停用的合并成员、设备与 Binding 保持原状。
disable、超管资格和新账号挂接继续使用当前人物的 `revision`。旧请求重放只返回原回执。
账号真正转给另一人继续用独立 `binding transfer`，不会创建同人关系。

## 回执与效果

数据库命令与结果回执在同一个短事务提交。领域拒绝先回滚领域写入，再登记可缓存的失败。
外部命令先提交原表中的意图，在事务外调用真实 Manager／文件服务，再登记结果。
使用原 `control_command_receipts`，不保存请求正文或密钥。

同一原主体、同一请求号、同一操作与内容返回原结果；换内容返回
`idempotency_conflict`。HTTP 中断或超时保留原请求号，查原回执；CLI 不自动重发。
`admin operation` 同时接受完整 operation ID，或当前 operator 的 request UUID。
`running`／`unknown` 沿原 ID 查证，未知不重发；CLI 遇到未确认结果会停止当前流程，
部署向导不会把它当成插件配置已完成。已确认未写入或整体回滚的失败按原失败回执返回；
效果或回滚仍未确认才保留 unknown。旧 unknown 不自动重试，也不永久阻挡新 UUID 的显式操作；
真实并发由原领域 Manager／文件服务的锁和 revision 裁决，无需清空旧回执。

插件授权、版本检查、启动与停止由原 PluginManager 执行。配置使用已批准安装定义的 schema，首次启用前、停用或启动失败后均可修正，查询不启动插件；
observe 只调用运行中插件的观察合同。通知只允许原领域规则证明可重试的发送前失败；
已发送、未知、处理中或预算耗尽均不重发，已有尝试次数保持不变。

Work 取消与续跑调用 `manage_work`，沿原 revision、执行身份、来源与效果回执处理。
模型保存调用共享 `ConfigFileService` 原子保存并热应用，返回 `applied` 或实际的待重启状态。
安装向导与模型保存共用搜索默认配置生成：官方 DeepSeek Chat / Responses 使用同密钥的独立
Messages 搜索连接，Gemini 使用自身独立桥，Anthropic Messages 使用原生搜索。已有的显式
关闭、搜索模式、后端和手选连接保留；自定义网关沿实际地址与凭据处理。详见[独立原生搜索](../deepseek-search-bridge.md)。
密钥写入 `model_profiles.secrets.json`，可新增或更新已有别名；别名使用合法环境变量名称。
配置文件中的 `api_key_env` 必须引用该别名。
读取模型时只返回配置与已配置状态，响应、审计和 TOML 不包含密钥值。

例如，先读取 `admin model show` 的 revision，再执行：

```sh
yuki admin model apply ./models.toml --revision <revision>
yuki admin model secret set MY_MODEL_KEY --from-env NEW_MODEL_SECRET --revision <revision>
yuki admin person create --display-name "人物名称"
yuki admin person merge <source-person-id> --to-person <target-person-id>
yuki admin person rename <person-id> "新名称"
yuki admin binding attach --person <person-id> --platform qq --account <account> --revision <revision>
```

`--request-id` 可显式保留原请求；不要在未知结果后换 ID 重做。密钥从命令进程环境读取，
不作为命令行参数传入。配置覆盖、Space 启停、人物启停、表情与 Memory rebuild 使用 `/ai admin <领域>`
命令，自主参与参数沿原 JSON 文件热更新；本阶段不新增这些能力的第二套管理接口。


## 人物资格、恢复与查询

超管资格持久化于 Person，换账号/平台不改变，账号转移不携带资格。Person 停用暂停使用而不撤销资格；显式 revoke 后旧 grant 请求重放只返回原结果，不再次授予。可无账号，不能以昵称作为授权键。

列表返回 `items/next_cursor`，沿内部 ID 续读。Binding 可按 person_id、platform、external_account_id 明确检索；外部账号仅用于发现，后续操作使用内部 ID。人物详情列出实际绑定。Work 列表/详情含原来源分类（origin/owner/initiative_run_id），详情复用原暂停原因、信号和可执行操作投影；不含原始 source 正文、恢复 journal、checkpoint 或工具正文。

Presence POST body 在原 request_id/expected_revision 外接受 enabled、ingest_eligible。启停与接入资格分开，保持历史作者/原投递事实；恢复不代表连接在线。所有管理 GET 没有 DML。

```sh
yuki admin person list
yuki admin person show <PersonID>
yuki admin person superuser grant <PersonID> --revision <当前revision>
yuki admin person superuser revoke <PersonID> --revision <当前revision>
yuki admin person enable <PersonID> --revision <source_revision>
yuki admin binding list --person <PersonID>
yuki admin binding enable <BindingID> --revision <当前revision>
yuki admin presence disable <PresenceID> --revision <当前revision>
yuki admin work list --state waiting_user
yuki admin work show <WorkID>
```

CLI、HTTP 与聊天共用 `admin/operations`，秘密不通过聊天参数提交。聊天语法和一次升级准备见[人物授权升级与命令迁移](../operations/person-admin-control-plane-upgrade.md)。
