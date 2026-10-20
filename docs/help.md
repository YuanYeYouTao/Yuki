# Yuki 使用与运维帮助

本页跟随 Yuki **4.0.0 开发基线**：Alembic head 为 `0143`，Plugin API 为 `7.0`。
人物、账号、空间与会话的关系见[当前架构](architecture/canonical-runtime.md)，
版本迁移见 [4.0 升级指南](upgrade-4.0.0.md)。

## 安装与启动

现成部署包使用 [3.9.0 Release](https://github.com/YuanYeYouTao/Yuki/releases/tag/v3.9.0)，
安装器与配置步骤见[对应版本指南](upgrade-3.9.0.md)。4.0 当前提供源码开发基线，
从 3.9 迁移见 [4.0 指南](upgrade-4.0.0.md)。

源码开发先安装 Python 3.12 和 uv：

```bash
uv sync --all-extras
uv run yuki --help
```

命令入口统一为 `yuki`，服务进程使用 `yuki-bot`。引导配置填写模型连接、平台和插件；
密钥输入不回显。

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

## Plugin API 7.0

当前 Host 只接受精确声明 Plugin API `7.0` 的插件。插件使用内部 Person、Space、Conversation ID，人物提及保留原位快照；插件配置仍使用 `plugin.config.read/write`，主 Agent 任务使用 `agent.run`。独立模型会话为 `plugin.agent_session.v2`，保留连续历史和真实模型请求预算。插件的实际权限由管理员批准和当前调用来源决定。

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
- [迁移到 Plugin API 7.0](plugin-development/api-7.0-migration.md)

## Emoji 与 Vision

- 共享表情原件与可重建索引位于持久工作区，文字与后台配图共用 Social 及原回执；见[表情文档](emoji-system/architecture.md)。
- Vision 是可选 Provider，失败时不会把任意外部 URL 当作可信媒体。

这些扩展都服从同一 Principal、Capability、审计、路由和 canonical owner 规则。

## 数据与升级

当前 Alembic head 为 `0143`。正式 3.9.0 的 head 为 `0119`；4.0 新增 23 个迁移节点，涉及人物与账号归属、个人设备、插件会话、表情文件库和查询索引。迁移保留原事件、Work、预算、私有协议与执行回执，逐项变化见 [4.0 升级指南](upgrade-4.0.0.md)。

历史 `0048` bridge 仍按数据库真实来源处理，不能通过手工 stamp 跳过。`0097` 退役 Genie 表前需保全原 Work、发送回执及被引用音频；这些材料不是可随意删除的缓存。

升级前停止实际写入本次数据库与备份目录的进程，并按同一时点备份：

- `yuki.db`
- `yuki.db-wal`
- `yuki.db-shm`
- 配置、Compose 文件、镜像 digest 和 Provider 登录目录
- 宿主插件、协议对象、工具 artifacts、持久 Work 证据及被引用音频

`0097` 与 `0102` 明确拒绝丢失事实的 downgrade，优先向前修补。换回旧镜像不会恢复已删事实；恢复历史
快照也不能覆盖升级后新消息、文件和回执。不要手工 stamp revision 或只恢复主 DB。
当前权限清退、旧执行核对、冷备、插件 API 更新及切换步骤见
[4.0.0 升级指南](upgrade-4.0.0.md)。

## 故障排查

### 私聊可用但群聊不回复

检查：

1. Space 是否启用，SpaceBinding 是否为 `active`。
2. Yuki 账号的 AccountAssignment 是否为 `active`、允许 ingest，Provider 是否真实在线。
3. 群 ingest 路由是否暂停，所指 Presence 是否与当前入站账号一致。
4. 冷路由或恢复所需的群成员探针是否成功；已认证且同一路由的普通入站无需逐条重查。
5. 当前群策略是否要求 @，原生提及是否指向当前 Yuki 账号。

群内超级管理员可用 `/ai admin group current enable` 恢复当前群，具体行为见[身份与群接入合同](architecture/canonical-runtime.md)。按原内部编号查询路由与归属，不手工改库或复活已结束的账号阶段。

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

当前开发基线见 [4.0.0 说明](releases/v4.0.0.md)，正式下载版本见 [3.9.0 说明](releases/v3.9.0.md)。本次准备不发布 4.0 镜像。正式发布流程见 [版本化 Docker Release](operations/versioned-docker-release.md)：tag 流水线核对版本身份及 main 祖先关系，构建后验证镜像、部署包与 release smoke；PR 的 Quality 检查验证源码与测试。
