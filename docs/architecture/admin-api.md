# 管理 API 与 CLI

管理入口为原 Bot HTTP 服务的 `/admin/v1`。`qq-ai-bot-cli admin` 与在线
`qq-ai-bot-cli plugin` 调用这个入口，不启动另一个 Bot 或直接写运行中的数据库。
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
角色不授予能力，凭据轮换立即影响后续请求。operator 不能凭管理权限以某个 Person 发言。

旧 WebUI 在 P2 期间仍提供原入口；两个入口共用同一个模型文件服务与原领域服务。
P3 删除旧入口。新 API 不创建浏览器登录会话，也不需要 Cookie 或 CSRF token。

## 方法与 CLI

所有写入都要求 UUID4 `request_id` 与非负整数 `expected_revision`，不接受请求方自报主体。
返回 `data`；命令含 `success`、资源 ID、revision、审计 ID 与公开结果，外部命令可含原 operation。

| 方法 | CLI／消费者 | 权限 |
| --- | --- | --- |
| `GET /health` | `admin health` | `control.health.read` |
| `GET /operations/{id}` | `admin operation <id>` | `control.operation.read` |
| `GET /traces/{turn\|work\|event}/{id}` | `admin trace <kind> <id>` | `control.execution.metadata.read`；`content=true` 另需 `control.execution.content.read` |
| `POST /persons`；`GET /persons/{id}`；`POST /persons/{id}/disable` | `admin person create\|show\|disable` | 对应 `identity.person.create\|read\|disable` |
| `POST /persons/{id}/bindings`；`GET /bindings/{id}`；`POST /bindings/{id}/disable` | `admin binding attach\|show\|disable` | 对应 `identity.binding.attach\|read\|disable` |
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

新建 Person 的 revision 为 1；挂接绑定使用该 Person 当前 revision，仅接受从未绑定过的
平台账号，且不能把 Yuki 自己的 Presence 挂成 Person。已出现的账号保持原归属；
转移在 P5 实现。停用保持历史、Memory、Work 和回执归属。平台标识沿原通用字符串合同。

## 回执与效果

数据库命令与结果回执在同一个短事务提交。领域拒绝先回滚领域写入，再登记可缓存的失败。
外部命令先提交原表中的意图，在事务外调用真实 Manager／文件服务，再登记结果。
使用原 `control_command_receipts`，不保存请求正文或密钥。

同一 operator、同一请求号、同一操作与内容返回原结果；换内容返回
`idempotency_conflict`。HTTP 中断或超时保留原请求号，查原回执；CLI 不自动重发。
`admin operation` 同时接受完整 operation ID，或当前 operator 的 request UUID。
`running`／`unknown` 沿原 ID 查证，未知不重发；CLI 遇到未确认结果会停止当前流程，
部署向导不会把它当成插件配置已完成。无“清空 unknown”入口。

插件授权、版本检查、启动与停止由原 PluginManager 执行。配置使用已注册 schema，
observe 只调用运行中插件的观察合同。通知只允许原领域规则证明可重试的发送前失败；
已发送、未知、处理中或预算耗尽均不重发，已有尝试次数保持不变。

Work 取消与续跑调用 `manage_work`，沿原 revision、执行身份、来源与效果回执处理。
模型保存调用共享 `ConfigFileService` 原子保存并热应用，返回 `applied` 或实际的待重启状态。
密钥写入 `model_profiles.secrets.json`，只保存新增别名；别名使用合法环境变量名称，
原 `YUKI_WEBUI_KEY_*` 别名继续可读。配置文件中的 `api_key_env` 必须引用该别名。
读取模型时只返回配置与已配置状态，响应、审计和 TOML 不包含密钥值。

例如，先读取 `admin model show` 的 revision，再执行：

```sh
qq-ai-bot-cli admin model apply ./models.toml --revision <revision>
qq-ai-bot-cli admin model secret set MY_MODEL_KEY --from-env NEW_MODEL_SECRET --revision <revision>
qq-ai-bot-cli admin person create
qq-ai-bot-cli admin binding attach --person <person-id> --platform qq --account <account> --revision <revision>
```

`--request-id` 可显式保留原请求；不要在未知结果后换 ID 重做。密钥从命令进程环境读取，
不作为命令行参数传入。配置覆盖、群启停、私聊权限、表情与 Memory rebuild 继续使用原 `/ai`
命令，自主参与参数沿原 JSON 文件热更新；本阶段不新增这些能力的第二套管理接口。
