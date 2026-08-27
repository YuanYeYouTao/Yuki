# Yuki 3.8 使用与运维帮助

Yuki 3.8 只支持 canonical runtime，当前源码 Alembic head 为 `0050`，Plugin API 为 `2.0`。永久 Yuki、
Person、Binding、Space、Presence 和 canonical Conversation 的关系见
[当前架构](architecture/canonical-runtime.md)。

## 安装与启动

Linux：

```bash
chmod +x install.sh
./install.sh
```

Windows PowerShell：

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

两个安装器默认安装 `3.8.1`。引导配置会询问主模型、可选 Flash/Embedding/Web/Vision、QQ
Provider、MCP、Plugin、Automation 与 Speech。密钥输入不回显，程序不会在线试用 API key。

日常运维：

```bash
docker compose config --quiet
docker compose up -d
docker compose ps
docker compose logs --tail 200 bot
```

不要提交 `.env`、`data/`、`plugins/` 中的私有数据、Provider 登录目录或任何 Cookie/token。

## QQ Provider

NapCat 与 SnowLuma 是同层正式 Provider。不同 QQ 可以同时在线；同一 QQ 只能有一条活动连接。
重复连接拒绝新的，不会自动挤掉旧连接。

切换同一 QQ 必须重新运行安装器，让 `gateway-action.json` 完成：

1. 停止并移除旧 Provider。
2. 确认旧连接已从 Registry 注销。
3. 启动目标 Provider。
4. 验证新连接沿用原 Presence。

停止失败时不会启动新 Provider。切换不改变 Conversation、Memory 或 RouteGeneration。

SnowLuma 默认地址：

- noVNC：`http://127.0.0.1:6081`
- WebUI：`http://127.0.0.1:5099`

默认 bind address 由以下变量明确控制：

```dotenv
SNOWLUMA_NOVNC_BIND_ADDRESS=127.0.0.1
SNOWLUMA_WEBUI_BIND_ADDRESS=127.0.0.1
```

只有明确需要远程访问时才设为 `0.0.0.0`，并同时启用强 VNC 密码、WebUI 认证、主机防火墙和
可信源限制。优先使用 VPN 或 TLS 反向代理；不要公网暴露 VNC、OneBot HTTP/WS、token 或 Cookie。
详见 [SnowLuma 部署与切换](deployment/snowluma.md)。

Provider 合同检查：

```bash
docker compose exec bot qq-ai-bot-cli gateway doctor --provider napcat
docker compose exec bot qq-ai-bot-cli gateway doctor --provider snowluma
```

doctor 是只读检查，不发送消息、不调用私有 action，也不输出凭据。

## 模型配置

配置入口是引导式 `qq-ai-bot-cli setup`。模型 Provider 使用 OpenAI-compatible Chat Completions
或 Responses；是否支持 thinking、native web、缓存和具体参数由对应 Provider 决定。

通用原则：

- 不在聊天、日志、Issue 或 Git 中粘贴 API key。
- Responses 请求默认不发送 `temperature`。
- 模型不支持某个请求字段时，在 profile 中关闭该能力，不伪装成功。
- Web、Embedding、Vision 和 Speech 都是可选能力；不可用时应有界降级，不影响纯文本主路径。
- secret 只能写入或查询“是否已配置”，不能通过控制面读回。

修改配置后先检查：

```bash
docker compose config --quiet
docker compose up -d --force-recreate bot
docker compose logs --tail 200 bot
```

## Conversation 与群聊

- 私聊 Conversation 属于 Person；同一 Person 的多个 QQ Binding 共用会话。
- 群聊 Conversation 属于 Space；更换 Yuki Presence 不会产生新的群会话。
- `/ai new` 是改变 Conversation generation 的显式操作。
- 群 ingest 路由决定哪个 Presence 处理某个外部群。路由暂停或不匹配时，消息在 Agent 和 Memory
  之前失败关闭。
- 当前事件回复优先使用 ingress 连接；自动化和主动通知读取持久路由。
- @、reply 与 mention-only 消息从 OneBot `original_message` 保序投影；任一 Yuki Presence 都
  被识别为 Yuki。

Rollup 只压缩 Prompt 历史，原始 `chat_events` 不被摘要替代。摘要是不可信输入，不进入 Memory。
详见 [Conversation Rollup](architecture/conversation-rollup.md)。

## Memory 与关系

Memory owner：

- SELF：永久 Yuki 自身。
- PERSON：永久 Person。
- GROUP：永久 Space。
- PERSON_GROUP：某 Person 与某 Space 的共同经历。

`memory_change.visibility` 只对 SELF 生效。PERSON、PERSON_GROUP 和 GROUP 目标若带合法的
`current_scope` 或 `global` hint，会忽略该 hint 后继续授权与 mutation；非法 visibility 仍会
被拒绝。Memory 事实保留 Evidence、authority、confidence、状态、冲突和版本链。

同一 Person 的多个 Binding 共享关系、偏好、人物记忆和历史。Yuki Presence 与第三方机器人不
创建人物关系。`/ai forgetme` 按 Person 删除其拥有的数据，删除后旧 Binding 不再能读取关系或
Memory。

更多资料：

- [Memory V2](architecture/memory-v2.md)
- [Memory 变更](architecture/memory-change.md)
- [Memory 质量运维](operations/memory-quality.md)
- [Memory 重建](architecture/memory-v2-rebuild.md)

## 工具、权限与控制面

主 Agent 只看到当前 Principal 被授予且本轮允许的工具。Capability 决定 metadata、外部 ID、
正文、mutation 与 destructive 操作的不同权限。

Control Plane 提供 CLI、QQ command 和未来 WebUI 共用的 Query/Command 服务。未来 WebUI 只能
把登录身份转换为 `ControlPrincipal` 与 `DecisionContext`，不能直接访问 ORM、数据库或
Gateway。3.8 尚未提供管理 HTTP API、登录或前端。

高风险边界：

- 不开放任意 OneBot action、MCP 任意调用、plugin arbitrary run 或 raw SQL。
- `SUPERUSERS`、数据库 URL、token、Cookie 和 API key 不可读回。
- `/healthz` 只返回公开瘦健康载荷。
- 管理审计、路由和内容查询必须经过对应 Capability。

在 QQ 中使用 `/ai help` 与 `/ai capabilities` 查看当前可用命令和能力；实际结果以当前
Principal、会话和运行配置为准。

## Plugin API 2.0

3.8 只接受 Plugin API `2.0`。插件可以使用固定 primary `conversation_key`，也可读取可选的
person、space、conversation 和 presence ID。插件不能自报超级管理员，也不能绕过
Control Plane、Capability 或 Gateway Registry。

插件发布的主动事件继续以独立 `external_event` 落账，不伪装成真人聊天。插件若请求 Yuki
点评，只会创建可靠 WakeupRequest；Worker 加载该 canonical Conversation 与普通聊天完全相同的
Rollup、raw history、Memory、Prompt compiler、工具 schema 和模型 profile，再把当前事件摘要作为
唯一的临时 user 尾部。这个提醒不写入 history；成功发送的 Yuki 主动消息会用
`caused_by_event_id` 指向来源事件。pending/processing 唤醒任务会阻止 Rollup 提前覆盖来源，任务
终态后自动解除。没有真实用户事件证明时，管理员与 mutation 能力继续失败关闭，当前目标允许的
Web、Memory read 和 history read 仍可使用。

开发入口：

- [Plugin 开发索引](plugin-development/index.md)
- [架构](plugin-development/architecture.md)
- [权限与安全](plugin-development/security.md)
- [从旧 Plugin API 迁移](plugin-development/api-2.0-migration.md)

## MCP、Emoji、Vision 与 Speech

- MCP 支持 stdio 与 Streamable HTTP；配置见 [MCP 文档](mcp/architecture.md)。
- Emoji 资产有独立生命周期、审核和作用域；见 [Emoji 文档](emoji-system/architecture.md)。
- Vision 是可选 Provider，失败时不会把任意外部 URL 当作可信媒体。
- Speech 使用独立 Genie-TTS Worker；默认关闭，见 [Speech 文档](speech/architecture.md)。

这些扩展都服从同一 Principal、Capability、审计、路由和 canonical owner 规则。

## 数据与升级

全新 3.8 数据库执行 `0048 -> 0049 -> 0050`。历史 bridge 只接受已经完成 canonical v2 的旧
`0048`；已有 canonical `0049` 直接升级到 `0050`。
更早数据库和 v1/backfill/cutover 中间态不支持。

升级前停止所有写入，并按同一时点备份：

- `qq_ai_bot.db`
- `qq_ai_bot.db-wal`
- `qq_ai_bot.db-shm`
- 配置、Compose 文件、镜像 digest 和 Provider 登录目录
- `plugins/github-monitor/` 与 `data/plugin_artifacts/`

`0049` 不提供 downgrade；`0050` 是追加式因果迁移。生产失败时仍应恢复完整快照，不能手工
stamp revision、git revert 数据
或只恢复主 DB。3.8.0 升级时还必须在停写状态运行会话 uncovered recount/check、
受控替换 GitHub Monitor 并通过离线 queue doctor，详见
[3.8.1 升级指南](upgrade-3.8.1.md)。

## 故障排查

### 私聊可用但群聊不回复

检查：

1. Space 与 SpaceBinding 是否 enabled。
2. SpaceBinding ingest route 是否 paused。
3. 当前 Presence 是否为该外部群的唯一 ingest。
4. 当前 QQ 是否仍是群成员，且 Provider 成员探针可用。
5. 群策略是否要求 @，以及当前消息是否正确投影到 Yuki Presence。

不要为“先能说话”而伪造旧 group/scope 行或随意改 Conversation generation。

### Provider 已登录但没有连接

```bash
docker compose --profile napcat --profile snowluma ps --all
docker compose logs --tail 200 napcat
docker compose logs --tail 200 snowluma
docker compose logs --tail 200 bot
```

若看到 `provider_conflict`，先停止旧 Provider并等待 Registry 注销。同一 QQ 的新连接不会挤掉旧
连接。

### 回复很慢

分别检查 Gateway 延迟、模型首 token、工具循环、Web/MCP 调用、Rollup backlog、Memory worker
和发送回执。不要只根据最终回复时间判断网络不稳定。管理健康可提供分类状态，但不包含 secret
或模型 reasoning。

### 数据库升级失败

不要继续启动 Bot。保留日志和故障现场，确认快照 checksum，然后按升级指南判断来源是否满足
historical 0048 bridge。pre-3.8 数据库不能通过关闭检查强行启动。

## 发布与版本

发布流程见 [版本化 Docker Release](operations/versioned-docker-release.md)，3.8 说明见
[发布说明](releases/v3.8.1.md)。正式镜像与 Release 只能由通过 Quality、迁移矩阵、release
smoke 和匿名拉取验证的 tag 生成。
