<div align="center">

<p><img src="img/Yuki_2.png" alt="Yuki" width="280"></p>

<h1>Yuki-QQbot</h1>

<p>面向个人部署、以长期关系和长期记忆为核心的 QQ AI Agent</p>

<p>
  <a href="https://github.com/YuanYeYouTao/Yuki-QQbot/releases/tag/v3.8.1"><img src="https://img.shields.io/badge/Release-3.8.1-blue" alt="Yuki 3.8.1 release"></a>
  <img src="https://img.shields.io/badge/Schema-0055-blue" alt="Alembic head 0055">
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

最新已发布版本为 **3.8.1**；当前源码基线为 **3.8.2（发布准备中）**，包含插件唤醒、Memory P1
治理、意图读取与记忆可靠性修复。3.8 只运行
canonical schema，Alembic head 为 `0055`。

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

### 社交工具与临时工作区

普通 Main Agent 固定暴露 QQ 社交、临时工作区和 Python 沙箱共十四个工具，
也可显式注册到自动化。发送遵守已有 canonical 路由；不确定的网络结果不自动重发。
工作区全会话共享，内容修改后 24 小时过期，不是永久文件库或私人保险箱。
Python 通过独立 runsc 容器运行，可经代理访问公网 HTTP/HTTPS；宿主、内网和网关隔离。
沙箱依赖额外宿主管理器，未安装时工具返回不可用，不能退回 Bot 内执行。
部署与边界详见 [社交、工作区与沙箱](docs/operations/social-workspace-sandbox.md)。

### 接收语音识别

私聊语音、触发回复的群聊语音和引用语音会自动经 Qwen ASR 转写，再进入同一个 Main Agent。
默认复用现有千问连接；无需安装本地识别模型，也不依赖 Genie-TTS。转写保存在消息历史中，
供后续聊天、历史搜索和 Rollup 使用；被引用语音不作为当前发言者的记忆证据。
配置与限制见 [语音识别](docs/speech/recognition.md)。

### 记忆读取与自动保存

可以自然地询问本人或历史共同群友的记忆、指定旧群名查询群整体或某人在群里的记忆；
同名时 Yuki 会要求澄清。权限取决于后端历史成员关系，不取决于当前登录的 Yuki 账号或旧群
是否启用。**共同群关系可开放人物完整 Person 结构化事实（含私聊来源）**，但不开放原始
私聊、他人 evidence/private SELF，也不改变写入权限。

普通自动提取最长等待一小时聚合，优先保存稳定事实、持续偏好和有意义的单次经历；
没有值得保存的内容时空提取是正常结果。明确的记住、纠正和删除仍立即处理。
详见唯一现行 [Memory 合同](docs/architecture/memory-v2.md)。

### 原生看图与联网

DeepSeek V4.1 使用正式模型名 `deepseek-flash`。主模型 profile 声明 `image_input` 后，
当前/引用图片经安全下载、限量抽帧直接进入完整 Main Agent，不再先调用 Qwen 描述图片。
图片仅在本轮工具循环中保留，不把 Base64 写进历史；后续重新查看可引用原图片。
视频查看（Issue #21）使用本地 FFmpeg 对真实消息/引用中的 MP4/MOV 视频抽帧，
同时支持 QQ `video` 消息和以普通 `file` 附件发送的视频：
默认最长 600 秒，按 5 秒期望间隔自适应取帧，最多 16 帧；达到帧数预算后均匀覆盖首尾。
分别通过 `VISION_VIDEO_MAX_DURATION_SECONDS`、`VISION_VIDEO_SAMPLE_INTERVAL_SECONDS`、
`VISION_VIDEO_MAX_FRAMES` 调整，也可通过对应 `vision.video_*` 运行时配置修改。
视频仍与图片共享处理后帧数/字节预算，分辨率最长边 4096。
视频下载独立使用 `VISION_VIDEO_MAX_DOWNLOAD_BYTES`，默认 200 MiB；普通图片继续使用
`VISION_MAX_DOWNLOAD_BYTES`，默认 20 MiB。提高下载上限不提高模型输入帧数或压缩后预算。
HTTP 视频流式写入受控临时文件，不将整个大视频缓存在内存；下载和抽帧期间继续受并发与超时限制。
临时视频及 JPEG 在抽帧结束后立即删除（包括异常、超时与取消），不建立视频文件缓存；
本轮模型循环结束后释放帧引用，不将帧 Base64 存入历史。进程强杀/主机故障无法执行清理，
因此临时文件属于容器临时目录，不放入持久化数据目录。
采样帧附带时间位置进入同一个 Main Agent，不使用额外看图模型；不分析音轨，
不能保证看到所有瞬间。不支持视频网页链接解析、网关本地路径或仅有文件 ID 的视频。
Docker 已包含 FFmpeg；源码运行需将 `ffmpeg`、`ffprobe` 加入 PATH。

### 文件附件读取

发送或引用附件后可以直接要求 Yuki 查看，不需要另起文件问答 Agent。
文件名只辅助选择预算；图片/视频、PDF、DOCX、XLSX 会检查实际内容格式。
文本、Markdown、代码、CSV/TSV、JSON/YAML 等读取限量文本；源码只作为文本，不执行。
PDF 提取文字（不含扫描页 OCR 和嵌入图片）；DOCX 提取段落/表格文字；XLSX 按工作簿顺序
读取单元格原始值及已有公式缓存，不计算公式，不还原图表、样式或日期显示格式。

- 普通文件最多 20 MiB，已标记为 MP4/MOV 的视频使用独立 200 MiB 预算；改扩展名不会放宽非视频预算。
- 每轮文档正文最多 20,000 字符；每份 PDF/工作簿最多 20 页/工作表，结果明确标记截断和读取范围。
- 文件与图片共享本轮附件数量上限；默认最多 5 个，超出的附件会标为未读取。
- 文档解析单进程串行、有超时；Linux 解析进程另设 384 MiB 地址空间和 CPU 时间限制。
- 不执行宏、脚本、公式，不跟随文档内外链，不解压到用户路径，不读取任意本机路径；
  加密、未知格式或损坏附件明确返回失败。普通 ZIP、旧二进制 DOC/XLS 暂不支持。
- 提取内容仅放进当前完整 Main Agent 的请求尾部，标为不可信资料，不写入历史正文；
  原附件引用仍保留。后续需要重新查看时请引用附件。临时下载/解码文件用完即删。

### 表情包与搜索

表情包入库分类、OCR/标签与去重仍走独立后台视觉任务，保留现有可配置 VisionProvider。
`VISION_ENABLED` 控制该外接服务，不是原生看图的开关；无图片能力的 profile 不会被强行喂图。

新版 DeepSeek Responses 忽略内置 `web_search`；使用 Tavily 可配置 `WEB_MODE=tavily`
与 `TAVILY_API_KEY`。
`web_search` 已在默认首轮常驻工具名单中。`both` 保留 Tavily
函数工具，不再因原生工具可用而移除它；Agent 可直接选择 Tavily，无须先等待原生失败。
`native` 仍表示仅原生，`disabled` 仍禁止联网；显式自定义工具名单保持有效。
部署级工具结构不随消息关键词或域名切换。联网路由器及自动换后端重跑已删除；
旧配置值 `native_with_tavily_fallback` 仅映射为 `both`。普通模型错误恢复和调用预算仍保留，
不会因缺来源另起 Agent 循环。不能把“未报错”视为原生搜索成功。DeepSeek Responses 当前无原生搜索，Anthropic
入口已独立实测可用，但本项目尚未接入该协议。

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
- 插件外部事件使用独立账本类型与有界不可信 digest，不进入普通历史，也不会把稳定前缀改写成
  动态 system 消息。
- 插件主动事件只负责唤醒正常 Main Agent：复用同一 Conversation snapshot、Rollup、历史、
  Memory、工具 schema 和模型 profile；事件提醒只作为不落账的当前 user 尾部，主动回复通过
  `caused_by_event_id` 保存因果。
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
curl -fLO https://github.com/YuanYeYouTao/Yuki-QQbot/releases/download/v3.8.1/install.sh
chmod +x install.sh
./install.sh
```

Windows PowerShell：

```powershell
Invoke-WebRequest -Uri https://github.com/YuanYeYouTao/Yuki-QQbot/releases/download/v3.8.1/install.ps1 -OutFile install.ps1
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

上述下载命令仍指向已发布的 `3.8.1`；当前源码安装器默认 `3.8.2`，须等待对应 Release 资产
发布后使用，不能将未发布版本视为可下载。安装器会校验 Release bundle、固定镜像版本、备份已有部署、受控更新
内置插件，并在 Bot 启动前执行离线 recount/check 与队列 doctor。任一门禁失败都保持
Bot 停止。密钥输入不回显，安装器不会在线试用 API key。

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

- fresh install：无父 revision 的 `0048` canonical baseline，随后升级到 `0049 -> 0050 -> 0051 -> 0052`。
- historical bridge：只接受已完成 canonical v2 的旧 `0048` 数据库。
- pre-3.8、v1、dual-write、backfill/cutover 中间态数据库不受支持，启动时失败关闭。
- `0049` 仍是不提供 downgrade 的 canonical bridge；`0050` 增加主动回复因果列和索引，`0051`
  只增加 Memory recall 评估与主动读取结果的无正文观测列。
  `0052` 增加社交操作回执，防止不确定发送被重试；社交工具仍在开发，不代表已经可用。
  生产数据的可靠回退方式仍是恢复升级前同一时点的 DB/WAL/SHM 快照。

升级前必须停止 Bot 与 Provider，并把以下文件作为一组保存：

- `data/qq_ai_bot.db`
- `data/qq_ai_bot.db-wal`
- `data/qq_ai_bot.db-shm`
- `.env`、`config/`、Compose 文件与镜像 digest
- `plugins/github-monitor/` 与 `data/plugin_artifacts/`；Bot 镜像不包含 Compose 挂载的插件代码

完整步骤见 [Yuki 3.8.1 升级指南](docs/upgrade-3.8.1.md)。不满足桥接前提时，新建 3.8 部署，
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

涉及 schema 或发布时还要验证 fresh `0048 -> 0049 -> 0050 -> 0051 -> 0052`、populated `0051 -> 0052`、SQLite
`foreign_key_check`、FTS/trigger、release smoke 和 Docker Compose 配置。

## 文档

- [使用与运维帮助](docs/help.md)
- [3.8 canonical runtime](docs/architecture/canonical-runtime.md)
- [Conversation Rollup](docs/architecture/conversation-rollup.md)
- [插件唤醒 Main Agent 与主动回复因果合同](docs/architecture/Yuki-插件唤醒Main-Agent与主动回复因果修复任务书.md)
- [Memory V2](docs/architecture/memory-v2.md)
- [Memory 变更合同](docs/architecture/memory-change.md)
- [Plugin API 2.0](docs/plugin-development/index.md)
- [MCP 架构](docs/mcp/architecture.md)
- [SnowLuma Provider](docs/deployment/snowluma.md)
- [3.8.1 升级指南](docs/upgrade-3.8.1.md)
- [3.8.1 发布说明](docs/releases/v3.8.1.md)
- [3.8.2 发布准备说明](docs/releases/v3.8.2.md)
- [3.8.2 升级指南](docs/upgrade-3.8.2.md)
- [版本化 Docker Release](docs/operations/versioned-docker-release.md)
- [CHANGELOG](CHANGELOG.md)

## License

[MIT](LICENSE)
