# Yuki 使用与运维帮助

Yuki 3.9.0 的 Alembic head 为 `0119`，Plugin API 为 `3.5`，只支持 canonical runtime。永久 Yuki、
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

两个安装器默认安装 `3.9.0`，正式镜像支持 `linux/amd64` 和 `linux/arm64`；部署包见
[3.9.0 Release](https://github.com/YuanYeYouTao/Yuki/releases/tag/v3.9.0)。引导配置会询问主模型、
可选 Flash/Embedding/Web/Vision、QQ Provider、Plugin 与 Automation，不再配置 Genie。
密钥输入不回显，程序不会在线试用 API key。

日常运维：

```bash
docker compose config --quiet
docker compose up -d
docker compose ps
docker compose logs --tail 200 bot
```

不要提交 `.env`、`data/`、`plugins/` 中的私有数据、Provider 登录目录或任何 Cookie/token。

## QQ Provider

Yuki 不再支持 NapCat，当前附带的 QQ/OneBot v11 Provider 为 SnowLuma。现有 Gateway 抽象保留，
业务 owner 不随网关实现改变。SnowLuma 可以连接不同 QQ；同一 QQ 只能有一条活动连接，
重复连接拒绝新的，不会自动挤掉旧连接。

安装向导只写配置，不执行停止、启动或自动切换。旧部署的 Compose 与 `.env` 不会自动替换，
需要按[旧 NapCat 配置清退](upgrade-3.9.0.md#旧-napcat-部署配置)逐项处理。

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
详见 [SnowLuma 部署](deployment/snowluma.md)。

Provider 合同检查：

```bash
docker compose exec bot yuki gateway doctor --provider snowluma
```

doctor 是只读检查，不发送消息、不调用私有 action，也不输出凭据。

## 模型配置

使用 `yuki setup` 完成初始引导；后续在模型 TOML 中明确选择供应商、协议、
API Base URL、模型与任务路由，通过 `admin model apply` 保存并热应用。
新增密钥使用 `admin model secret set`，密钥进入服务器私有文件，不回显。
连接配置与命令见[管理 API](architecture/admin-api.md)。

验证一项新连接时：

1. 用 `admin model show` 读取当前配置与 revision；先只修改主对话的路由。
2. 通过 `admin model apply <file> --revision <revision>` 应用并确认返回的实际状态。
3. 在明确授权的测试会话发送消息，按需测试图片与工具；用 `admin trace` 查询原轮次，
   用 `model stats` 查看原调用记录。配置保存本身不调用模型。
4. 需要恢复时，用同一入口将路由改回并确认应用。

支持 Chat Completions、Responses、Claude Messages 和原生 Gemini GenerateContent；
思考、图片、工具、结构化输出与缓存取决于所选连接。真实密钥和上游可用性通过实际请求验证。
Google 搜索沿显式搜索连接与 `search_mode="bridge"`，不根据消息内容暗中切换 Provider。
用量是 Yuki 入账记录，不是供应商账单。

主 Agent 在启动时冻结工具合同，执行处按当前 Principal 核验授权。默认 direct 模式直接声明部署内固定完整工具清单；显式启用 Code Mode 时，固定直调工具保留终端、联网搜索和网页读取，其余工具经 Code Mode 调用。
Capability 决定 metadata、外部 ID、正文、mutation 与 destructive 操作的不同权限。

管理 CLI 通过 Bearer API 调用原领域服务，权限来自服务器 operator 声明；
QQ 管理仍使用 `/ai`。当前范围见[管理 API](architecture/admin-api.md)。

高风险边界：

- 不开放任意 OneBot action、plugin arbitrary run 或 raw SQL。
- `SUPERUSERS`、数据库 URL、token、Cookie 和 API key 不可读回。
- `/healthz` 只返回公开瘦健康载荷。
- 管理审计、路由和内容查询必须经过对应 Capability。

在 QQ 中使用 `/ai help` 与 `/ai capabilities` 查看当前可用命令和能力；实际结果以当前
Principal、会话和运行配置为准。

## Plugin API 3.5

当前 Host 只接受精确声明 Plugin API `3.5` 的插件。未发布的事件、未实现的 `runtime.config.read/write` 权限、Genie 专属接口与直接模型 LLMFacade 已移除；插件自己的配置仍使用 `plugin.config.read/write`，模型任务使用 `agent.run`。插件可以使用固定 primary `conversation_key`，也可读取可选的
person、space、conversation 和 presence ID。插件不能自报超级管理员，也不能绕过
Control Plane、Capability 或 Gateway Registry。

插件发布的主动事件继续以独立 `external_event` 落账，不伪装成真人聊天。插件若请求 Yuki
点评，只会创建可靠 WakeupRequest；Worker 加载该 canonical Conversation 与普通聊天完全相同的
compact 检查点、raw history、本轮资料、Prompt compiler、工具合同和模型 profile，再把当前事件摘要作为
唯一的临时 user 尾部。这个提醒不写入 history；成功发送的 Yuki 主动消息会用
`caused_by_event_id` 指向来源事件。compact 不删除这些原件，也不改变唤醒任务的来源、授权或回执。
工具执行继续核验该提醒的真实来源、当前授权与目标范围。

开发入口：

- [Plugin 开发索引](plugin-development/index.md)
- [架构](plugin-development/architecture.md)
- [权限与安全](plugin-development/security.md)
- [从旧 Plugin API 迁移](plugin-development/api-3.5-migration.md)

## Emoji 与 Vision

- Emoji 资产有独立生命周期、审核和作用域；见 [Emoji 文档](emoji-system/architecture.md)。
- Vision 是可选 Provider，失败时不会把任意外部 URL 当作可信媒体。

这些扩展都服从同一 Principal、Capability、审计、路由和 canonical owner 规则。

## 数据与升级

当前完整迁移链至 `0119`。`0098` 退役 MCP 状态，`0099` 冻结已提交摘要表示；`0100` 关联插件后台任务的原 Work，`0101` 将旧效果结果归一为明确 outcome 并保留原正文；`0102` 移除同 memory_key 单 active 的唯一索引，同 key 的独立事实可以共存。`0103`移除旧人物评分存储；`0104`删除无消费者的检查点配额记账、投递死字段及主动轮唯一占用；`0105`统一 Work 父子树并退役旧完成门槛；`0106`保留自动化的完整指令与原 run 恢复版本；`0107`增加实际查询索引。`0108`的资料差量缓存由`0109`清退，普通运行资料只用于当前激活，公共历史保留原聊天；`0110`将定时定义转为共同 Main 任务，保留原版本、到期 run、Work、预算和效果回执；`0111`删除 Memory 回执只接受 event/initiative 的封闭检查，无真人事件的 SELF 定时执行沿原发生、版本与 execution 记账。`0112`统一同一Conversation的公共历史，退出个人/SELF时区配置及公共笔记/观察摘要，原私有执行与引用保全。`0113`删除generation/reset时清除原观察及DAG的旧触发器，新会话仍取消旧来源的新执行并清公共历史，原件保留。历史 `0048` bridge 的来源限制仍有效，不能用版本号或
手工 stamp 跳过来源校验。`0097` 退役四张 Genie 表及固定配置键，执行前必须核原
Work/发送回执及被引用 WAV，保全专属事实；不能把生成表当作纯缓存。

`0114` 将旧群暂停状态迁入共同配置；`0115` 删除无消费者的反思诊断列；`0116` 退役旧 detached continuation claim；`0117` 删除无消费者的 OneBot 诊断日志；`0118` 为等待中的 sandbox run 建索引；`0119` 将有效公共检查点迁入既有 projection，退役旧 Rollup 表、等待与配置。原事件、Work、预算、journal、私有协议和执行回执保留。

升级前停止实际写入本次数据库与备份目录的进程，并按同一时点备份：

- `yuki.db`
- `yuki.db-wal`
- `yuki.db-shm`
- 配置、Compose 文件、镜像 digest 和 Provider 登录目录
- 宿主插件、协议对象、工具 artifacts、持久 Work 证据及被引用音频

`0097` 与 `0102` 明确拒绝丢失事实的 downgrade，优先向前修补。换回旧镜像不会恢复已删事实；恢复历史
快照也不能覆盖升级后新消息、文件和回执。不要手工 stamp revision 或只恢复主 DB。
当前权限清退、旧执行核对、冷备、插件 API 更新及切换步骤见
[3.9.0 升级指南](upgrade-3.9.0.md)。

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
docker compose --profile snowluma ps --all
docker compose logs --tail 200 snowluma
docker compose logs --tail 200 bot
```

若看到 `provider_conflict`，先停止旧 Provider并等待 Registry 注销。同一 QQ 的新连接不会挤掉旧
连接。

### 回复很慢

分别检查 Gateway 延迟、模型首 token、工具循环、Web/插件调用、Main compact 请求、Memory worker
和发送回执。不要只根据最终回复时间判断网络不稳定。管理健康可提供分类状态，但不包含 secret
或模型 reasoning。

### 数据库升级失败

不要继续启动 Bot。保留日志和故障现场，确认快照 checksum，然后按升级指南判断来源是否满足
historical 0048 bridge。pre-3.8 数据库不能通过关闭检查强行启动。

## 发布与版本

发布流程见 [版本化 Docker Release](operations/versioned-docker-release.md)，当前版本见
[3.9.0 说明](releases/v3.9.0.md)，旧版本记录见 [3.8.1 说明](releases/v3.8.1.md)。正式镜像与 Release 由 tag 流水线核对版本身份及 main 祖先关系，构建后验证已发布镜像、部署包、release smoke 与匿名拉取；Quality 的源码、前端与测试结果另行核对。
