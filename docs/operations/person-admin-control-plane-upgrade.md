# 人物授权升级与管理命令迁移

本说明适用于迁移 `0136/0137` 的开发版本。资格属于 Person；旧 `SUPERUSERS` 不再是运行授权源，数据库升级不会从账号名单自动授予资格。本文是升级操作说明，未表示生产已升级。

## 旧部署准备

先在旧部署目录保存候选，再删除旧配置。使用本版本的脚本和原 SQLite 文件；脚本仅以只读连接查找全部历史 QQ Binding、原 Person 和当前合并根，不启动 Bot、不调用模型、不写数据库、不授予资格。

```sh
python scripts/prepare_person_authority.py --deployment-root /path/to/yuki --output /path/to/person-authority-candidates.json
```

数据库不在 `.env` 的 `DATABASE_URL` 或默认 `data/yuki.db` 时，另传 `--database /absolute/path/yuki.db`。输出包含候选身份资料，按部署数据保管。候选可能涉及账号转移，必须按原人与当前真实归属核对，不能把账号现任主人自动当成原管理员。

然后从 `.env`、Compose override、systemd/实际进程环境和当前 shell 移除旧 `SUPERUSERS`。保留原模型、网关、群配置和业务数据。新版本沿原 Bot 启动迁移；同一数据库只运行一个主动 Bot。`0136` 增加 Person 资格及原管理回执的内部事件/revision 关联，`0137` 删除无业务消费者的设备读取活跃度列；原事件、已付请求、设备撤销和投递回执保留。

## Operator 与人物资格

Operator 仍使用现有独立 Bearer policy，格式见[示例](../../config/control-operators.example.toml)。已有部署保留自己的 policy 和 `token_env`；需要管理新能力时显式补对应 capability。角色名称不隐含授权。向导为新部署生成同一种 policy，将凭据保存到 `.env`，不打印凭据。管理不需要 QQ 账号。

基础 Compose 的 Bot 仅 `expose: 8080`，没有向宿主发布该端口。直接在容器内调用 CLI；以下示例使用向导默认的 token 环境变量，自定义 policy 使用其声明的 `token_env`：

```sh
docker compose exec bot sh -c 'YUKI_CONTROL_CREDENTIAL="$YUKI_CONTROL_OPERATOR_TOKEN" yuki admin health'
docker compose exec bot sh -c 'YUKI_CONTROL_CREDENTIAL="$YUKI_CONTROL_OPERATOR_TOKEN" yuki admin person list'
docker compose exec bot sh -c 'YUKI_CONTROL_CREDENTIAL="$YUKI_CONTROL_OPERATOR_TOKEN" yuki admin person show <PersonID>'
docker compose exec bot sh -c 'YUKI_CONTROL_CREDENTIAL="$YUKI_CONTROL_OPERATOR_TOKEN" yuki admin person superuser grant <PersonID> --revision <当前revision>'
```

将尖括号示例替换为真实内部 ID/revision 后执行。没有 Person 时先 `admin person create --display-name "名称"`，再读取其当前 revision 并 grant；人物可以没有账号。每个候选逐项确认、逐项授予，不批量按裸 QQ 号赋权。

`admin person superuser revoke` 撤销资格；`person disable/enable` 是全局人物启停，停用不撤销资格。账号解绑/停用不改变 Person 的资格，账号转移不携带资格；同人合并保留双方当前资格，合并后撤销不会被旧成员或旧 grant 请求恢复。不保护“最后一个管理员”，停用后可由独立 operator 恢复。

`admin binding list --person <PersonID>` 和 `binding show` 可发现账号；恢复用 `binding enable <BindingID> --revision <revision>`。转移用 `binding transfer <BindingID> --to-person <PersonID>`，保旧时段与历史。App/ACP 账号由服务器生成，沿原配对流程，不手工挂接或转移。

## 配对、插件与原回执

```sh
docker compose exec bot sh -c 'YUKI_CONTROL_CREDENTIAL="$YUKI_CONTROL_OPERATOR_TOKEN" yuki admin client pair --person <PersonID>'
docker compose exec bot sh -c 'YUKI_CONTROL_CREDENTIAL="$YUKI_CONTROL_OPERATOR_TOKEN" yuki setup apply-pending'
docker compose exec bot sh -c 'YUKI_CONTROL_CREDENTIAL="$YUKI_CONTROL_OPERATOR_TOKEN" yuki plugin list'
docker compose exec bot sh -c 'YUKI_CONTROL_CREDENTIAL="$YUKI_CONTROL_OPERATOR_TOKEN" yuki admin operation <原operation或请求UUID>'
```

配对码只显示一次，交给本人设备。设备凭据不变成管理凭据。插件配置 schema 来自已批准安装定义，首次启用前、停用后和启动失败后均可修改，查询不会启动插件。`plugin doctor/inspect` 展示原完整诊断；密钥轮换使用 `admin model secret set` 的环境引用，不在聊天或命令行参数提交密钥。

`yuki admin` 修改和支持该参数的 `plugin configure/retry-outbox` 可通过 `--request-id` 保留原请求。其余插件生命周期命令沿原 CLI 返回的 operation 查询；不要因确认丢失再次执行。确认丢失、running/unknown 继续查询原 operation，不能换 ID 重做或声称效果未发生。旧 grant 等请求重放只读取旧结果，不再次修改当前状态。Work 使用 `admin work list/show` 发现原 ID 和 revision，再 cancel/resume；原来源、预算、journal 和已付事实保留。

## 聊天命令

使用 `/ai help` 查看自助/会话操作，使用 `/ai admin help` 查看管理操作。管理语法集中到 `/ai admin <领域> <操作>`，具体参数由各领域帮助给出：

| 领域 | 语义 |
| --- | --- |
| `person/binding/presence` | 按内部 ID 管理人物、外部账号时段和 Yuki 接入身份；启用不代表连接已经在线 |
| `group current enable` | 恢复当前停用/未登记群，沿真实连接和当前群，不要求普通入账先成功 |
| `config` | 沿原配置覆盖和变更记录；返回 change ID、需重启或仅未来生效等真实应用方式 |
| `memory/preference` | 本人普通操作仍在 `/ai memory/preference`；跨人物与维护操作使用显式 admin 入口 |
| `emoji doctor/repair` | doctor 只读诊断；repair 明确修复，不把诊断暗中当修改 |
| `plugin/work/model/operation` | 原插件管理、任务取消/恢复、模型查看和原回执查询；文件与秘密管理走 operator CLI |

旧裸 `/ai config`、插件管理别名和误称私聊的全局人物开关删除。`/ai link`、`/ai forgetme`、会话控制和普通插件注册调用保留各自职责。合法带图命令不再被“有图不可写”吞掉。App/ACP 输入这些文字按普通正文处理，不执行平台管理员命令。

现行合同见[控制面](../architecture/control-plane-foundation.md)、[管理 API](../architecture/admin-api.md)及[个人 API](../architecture/client-api.md)；实现和五轮复审见[实施记录](person-admin-control-plane-20261018.md)。
