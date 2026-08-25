<div align="center">

<p><img src="img/Yuki_2.png" alt="Yuki" width="280"></p>

<h1>Yuki-QQbot</h1>

<p>面向个人部署、以长期关系和长期记忆为核心的 QQ AI Agent</p>

<p>
  <a href="https://github.com/YuanYeYouTao/Yuki-QQbot/releases/tag/v3.8.0"><img src="https://img.shields.io/badge/Release-3.8.0-blue" alt="Yuki 3.8.0 release"></a>
  <img src="https://img.shields.io/badge/Schema-0049-blue" alt="Alembic head 0049">
  <img src="https://img.shields.io/badge/Plugin%20API-2.0-8A2BE2" alt="Plugin API 2.0">
  <img src="https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white" alt="Python 3.12">
  <img src="https://img.shields.io/badge/Deploy-Docker%20Compose-2496ED?logo=docker&logoColor=white" alt="Docker Compose">
  <a href="https://github.com/YuanYeYouTao/Yuki-QQbot/actions/workflows/quality.yml"><img src="https://github.com/YuanYeYouTao/Yuki-QQbot/actions/workflows/quality.yml/badge.svg" alt="Quality"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-yellow" alt="MIT License"></a>
</p>

</div>

Yuki 不是给 QQ 套一层模型回复的问答机器人。它把 Conversation、Memory、Relationship、
Automation、Plugin 与 QQ 登录账号和 Gateway 连接分开，让同一个长期角色能够换账号、换
Provider，并在权限边界内持续记住人与共同经历。

当前版本为 **3.8.0**。3.8 只运行 canonical schema；Alembic head 为 `0049`。

## 3.8 核心合同

| 领域 | 现行合同 |
| --- | --- |
| 永久 Yuki | 一个数据库对应一个永久 Yuki；人格、SELF、记忆、关系和设置不属于 QQ 或 Gateway |
| 人物与空间 | Person 可有多个 IdentityBinding；Space 可有多个 SpaceBinding |
| Yuki 账号 | Presence 只表示 Yuki 的平台账号，不是 Person |
| Conversation | 私聊按 Person，群聊按 Space；换账号或 Provider 不重置会话 |
| QQ Provider | NapCat 与 SnowLuma 同为正式 OneBot v11 Provider |
| 连接冲突 | 不同 QQ 可并存；同一 QQ 的第二条活动连接被拒绝 |
| 路由 | Person 主动路由、Space ingest 路由、Space 主动路由相互独立 |
| Memory | SELF、PERSON、GROUP、PERSON_GROUP 按 canonical owner 隔离 |
| Plugin | Plugin API 2.0；旧 conversation key 固定映射到 primary alias |
| 管理能力 | transport-neutral Control Plane 是未来 WebUI 的唯一业务后端边界 |

完整结构见 [Yuki 3.8 canonical runtime](docs/architecture/canonical-runtime.md)。

## 消息主路径

```text
QQ event
   |
   v
NapCat or SnowLuma provider-aware OneBot adapter
   |
   v
Presence / Person / Space / canonical Conversation resolution
   |
   v
ingest fence -> transport receipt -> immutable event ledger
   |
   v
Conversation runtime + Rollup + Memory retrieval
   |
   v
Capability-filtered Agent tools
   |
   v
reply through ingress connection or deterministic active route
```

事件账本保留真实 OneBot provenance。模型不能直接访问数据库、token、Cookie、任意 OneBot
action 或宿主机；工具调用由后端进行权限、预算、幂等和审计检查。

## 功能

- 私聊、群聊、回复与保序 mention 投影，多轮 Conversation 和 History Rollup。
- Memory V2：证据、事实、混合召回、冲突、版本链、生命周期、Dream 与受控变更。
- 按永久 Person 保存的好感度、信任度和偏好。
- 有界 Agent 工具循环、联网搜索、MCP、自动化与 Plugin API 2.0。
- 可选图片理解、表情资产管理和本地 Genie-TTS 语音。
- NapCat/SnowLuma 多 Provider、多 Presence、确定性路由与连接健康投影。
- transport-neutral Control Plane；3.8 本身不开放管理 HTTP API，也不包含 WebUI。

## 快速安装

要求：

- Linux amd64，或在 Windows 上运行 Linux Containers 的 Docker Desktop
- Docker Engine 与 Docker Compose v2
- 一个可用的 OpenAI-compatible 模型配置
- 至少一个 NapCat 或 SnowLuma QQ 登录账号

Linux：

```bash
curl -fLO https://github.com/YuanYeYouTao/Yuki-QQbot/releases/download/v3.8.0/install.sh
chmod +x install.sh
./install.sh
```

Windows PowerShell：

```powershell
Invoke-WebRequest -Uri https://github.com/YuanYeYouTao/Yuki-QQbot/releases/download/v3.8.0/install.ps1 -OutFile install.ps1
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

安装器默认版本为 `3.8.0`。它会校验 Release bundle、固定镜像版本、备份已有部署、运行引导配置、
执行 Compose 检查并启动所选 Provider。密钥输入不回显，安装器不会在线试用 API key。

源码验证：

```bash
cp .env.example .env
docker compose config --quiet
docker compose up -d
```

不要提交 `.env`、`data/`、Gateway 登录目录或 SnowLuma/NapCat Cookie。

## NapCat 与 SnowLuma

两者处于同一 Provider 层：

- NapCat QQ A 与 SnowLuma QQ B 可以同时在线。
- 同一 QQ 无论同 Provider 还是跨 Provider，只允许一条活动连接。重复连接拒绝新的，保留旧的。
- 切换同一 QQ 必须先停止旧 Provider并确认连接注销，再启动新 Provider。
- 切换只改变 GatewayConnection 和 ConnectionGeneration；Presence、Conversation、Memory 和
  RouteGeneration 保持不变。

安装向导通过 Compose profiles 管理 `napcat`、`snowluma` 和可选 `speech`。SnowLuma noVNC
和 WebUI 默认只绑定 `127.0.0.1`。只有明确需要远程访问时才将
`SNOWLUMA_NOVNC_BIND_ADDRESS` 或 `SNOWLUMA_WEBUI_BIND_ADDRESS` 设为 `0.0.0.0`；此时必须配置
强密码、主机防火墙和可信来源限制，不能暴露 OneBot HTTP/WS 或 VNC 原始端口。

首次登录、持久目录和故障恢复见
[SnowLuma Provider 部署与切换](docs/deployment/snowluma.md)。Yuki 不宣称任何 Provider 或切换
方式能够降低腾讯账号风控风险。

## 数据库与升级

3.8 的数据库合同：

- fresh install：无父 revision 的 `0048` canonical baseline，随后升级到 `0049`。
- historical bridge：只接受已完成 canonical v2 的旧 `0048` 数据库。
- pre-3.8、v1、dual-write、backfill/cutover 中间态数据库不受支持，启动时失败关闭。
- `0049` 不提供 downgrade；唯一数据回退方式是恢复升级前同一时点的 DB/WAL/SHM 快照。

升级前必须停止 Bot 与 Provider，并把以下文件作为一组保存：

- `data/qq_ai_bot.db`
- `data/qq_ai_bot.db-wal`
- `data/qq_ai_bot.db-shm`
- `.env`、`config/`、Compose 文件与镜像 digest

完整步骤见 [Yuki 3.8 升级指南](docs/upgrade-3.8.0.md)。不满足桥接前提时，新建 3.8 部署，
不要让 3.8 自动猜测或修复旧身份数据。

## 未来 WebUI

未来 WebUI 必须只调用 `ControlPlaneBundle` 的 Query/Command 服务：

```text
WebUI/HTTP -> authentication adapter -> ControlPrincipal/DecisionContext
           -> ControlPlaneBundle -> repositories/runtime registries
```

WebUI 不得直接读取 ORM、数据库、Gateway 连接或 secret，也不得复用 QQ 消息中的 @/正文证明。
3.8 已提供分页、Capability、乐观并发、幂等回执、审计和长任务投影；尚未实现 HTTP 管理 API、
登录、Cookie、CSRF 或前端。

## 日常运维

```bash
docker compose ps
docker compose logs --tail 200 bot
docker compose pull
docker compose up -d
```

Provider 状态：

```bash
docker compose --profile napcat --profile snowluma ps --all
docker compose exec bot qq-ai-bot-cli gateway doctor --provider napcat
docker compose exec bot qq-ai-bot-cli gateway doctor --provider snowluma
```

`/healthz` 只返回公开瘦健康信息。连接明细、路由状态、完整 external ID、内容和管理健康必须经过
Control Plane capability；secret 永远不可回读。

## 开发验证

```bash
uv sync --extra dev
uv run ruff format --check
uv run ruff check
uv run mypy src
uv run pytest
```

涉及 schema 或发布时还要验证 fresh `0048 -> 0049`、populated `0048 -> 0049`、SQLite
`foreign_key_check`、FTS/trigger、release smoke 和 Docker Compose 配置。

## 文档

- [使用与运维帮助](docs/help.md)
- [3.8 canonical runtime](docs/architecture/canonical-runtime.md)
- [Conversation Rollup](docs/architecture/conversation-rollup.md)
- [Memory V2](docs/architecture/memory-v2.md)
- [Memory 变更合同](docs/architecture/memory-change.md)
- [Plugin API 2.0](docs/plugin-development/index.md)
- [MCP 架构](docs/mcp/architecture.md)
- [SnowLuma Provider](docs/deployment/snowluma.md)
- [3.8 升级指南](docs/upgrade-3.8.0.md)
- [3.8 发布说明](docs/releases/v3.8.0.md)
- [版本化 Docker Release](docs/operations/versioned-docker-release.md)
- [CHANGELOG](CHANGELOG.md)

## License

[MIT](LICENSE)
