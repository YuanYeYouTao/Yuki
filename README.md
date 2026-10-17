<!-- release-baseline: version=3.9.0 schema=0119 -->

中文（默认） · [English](README.en.md)

<div align="center">

<p><img src="img/Yuki_2.png" alt="Yuki" width="280"></p>

<h1>Yuki</h1>

<p>一个在真实对话中持续存在的社会化 AI Agent</p>

<p>
  <a href="https://github.com/YuanYeYouTao/Yuki/releases/tag/v3.9.0"><img src="https://img.shields.io/badge/Release-3.9.0-blue" alt="Yuki 3.9.0"></a>
  <img src="https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white" alt="Python 3.12">
  <img src="https://img.shields.io/badge/Deploy-Docker%20Compose-2496ED?logo=docker&logoColor=white" alt="Docker Compose">
  <a href="https://github.com/YuanYeYouTao/Yuki/actions/workflows/quality.yml"><img src="https://github.com/YuanYeYouTao/Yuki/actions/workflows/quality.yml/badge.svg" alt="Quality"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-AGPL--3.0--only-blue" alt="AGPL-3.0-only License"></a>
</p>

[下载正式版 3.9.0](https://github.com/YuanYeYouTao/Yuki/releases/tag/v3.9.0) · [3.9.0 发布说明](docs/releases/v3.9.0.md) · [3.9.0 升级指南](docs/upgrade-3.9.0.md) · [使用帮助](docs/help.md)

</div>

Yuki 是一个开源、自托管的社会化 AI Agent，探索数字生命如何在真实社交场景中持续存在。她当前运行在 QQ 私聊和群聊中，记住人与共同经历，也能使用工具和持久工作环境完成跨消息的任务。身份和记忆由自己的数据库保存，更换模型、QQ 账号或网关时可以继续沿用。

**当前正式版为 3.9.0。** 聊天、SELF 自主参与、定时任务、插件和长任务续跑共用同一主 Agent。你可以在手帐式管理 WebUI 中查看聊天、执行轨迹、模型用量和工作区；长会话由当前 Main 模型整理，任务沿原检查点与真实回执继续。完成或等待决定会在原工具批次、Code 程序收拢后结算，父子任务也能继续问答和交付。默认使用 direct 镜像，Code Mode 与持久工作环境按需部署，管理 WebUI 默认关闭。

完整功能、修复、贡献者和已知限制见 [3.9.0 发布说明](docs/releases/v3.9.0.md)；从旧版迁移请先阅读 [3.9.0 升级指南](docs/upgrade-3.9.0.md)。

## 3.9.0 的主要变化

- **默认 direct 构建**：保持同一主 Agent 循环和固定完整工具声明，默认镜像不包含 Monty binding、worker 或 launcher。Code Mode 是可选的工具调用方式，使用单独构建的 `codemode` target 并显式启用；两种模式共用工具实现与执行后端。
- **管理 API 与共享执行 Runtime**：管理 CLI 通过 Bearer API 调用原身份、插件、模型配置、诊断与 Work 服务；聊天、SELF、插件、自动化及 Work 续跑共用主 Agent。
- **按群控制接话与主动唤醒**：自动接话、主动唤醒和自主参与的源码默认值为开启，可按实际配置分别控制。连续普通群消息按可配置窗口合并接话机会，明确呼叫和原 Work 输入不经过该等待；关闭新机会不取消已接纳任务或定时安排。Jev 语义控制器仍是默认关闭的可选接入。
- **可恢复的长任务与子 Agent**：Work 保留目标、父子关系、累计预算、原检查点和真实回执。完成、等待或请求输入后，原工具批次和 Code 程序仍会收拢，再由统一出口结算；当前任务被取消、标记失败或真实授权失效时，仍会停止新效果。子 Agent 共用 Main 的完整工具能力，真实提问可以唤醒可接续的父任务，前台也能直接管理已获授权的旧子任务，无需另开父任务。重启不重复已确认效果，也不重开已结束祖先。
- **简化记忆策略**：同 key 的独立事实可以共存，修改与删除按 fact ID；删除归因/强化评分、自动升格、容量腾位及旧人物好感度/信任评分。提取、SELF 与 Dream 可分别选择模型，允许部分整理与未决结果；自动提取只采纳新的事实断言。
- **Agent 按需取资料**：长期事实由 `search_memory` 按需检索，不再每轮自动注入。当前、引用、历史附件、工作区和获准工具图片统一交给原主模型的原生多模态输入。
- **Main compact 与共同历史**：一个 Conversation 共用一份原事件历史，当前 Main 按真实输入容量整理公共历史或私有执行尾部；工具配对、来源和已付请求保留。`/ai compact [重点]` 可手动整理公共历史，旧独立 Rollup 与 Work/ordinary 摘要链退出。程序提供的本轮资料使用尖括号标签标明系统资料边界，放在当前正文之前，只用于当前激活；任务笔记继续归原 Work 私有入口。
- **合并转发按需读取**：Agent 可沿原会话事件读取第一层转发节点，保留原顺序与部分失败结果。入账时不自动展开，内层转发也不递归读取。
- **多协议与联网**：支持 Chat Completions、Responses、Claude Messages 和 Gemini GenerateContent，按任务显式选择连接。新安装默认开启模型搜索，保留显式禁用和实际连接能力；新 Profile 的默认思考档位为 medium，旧显式设置不被覆盖。
- **保留真实失败与回执**：模型错误、工具失败和未知效果沿原调用保存；迟到结果仍可按原执行核对。截断的已付响应保留为不完整结果，不为解释错误或修补纯文本截断自动再买一轮模型请求，也不自动往 QQ 插入固定报错。
- **减少回复前的数据库等待**：历史读取、上下文准备和可丢诊断写入移出关键写事务；后台维护采用索引、有界分页和短事务。实际延迟仍受模型响应、工具请求和宿主资源影响。
- **清退旧能力与接口**：现有 MCP 的连接、目录、管理页面、SDK 和自动化入口一并移除，模型侧运维管理工具及无消费者的辅助动作日志也已退役。通用工具结果、媒体和原回执继续保留。Plugin API 升至 **3.5**，删除未实现的事件/权限声明；数据库 head 为 **0119**。
- **amd64 与 arm64 发行**：默认 direct 镜像覆盖 Linux amd64 和 arm64，Bot、SnowLuma 与两套安装器按宿主架构选择镜像；已有部署仍保留自己的 Compose 与挂载。见 [PR #431](https://github.com/YuanYeYouTao/Yuki/pull/431)。
- **退出语音输出**：Genie 合成、声线/偏好、工具参数、SDK/管理功能、Worker 与发布依赖一并移除；入站/引用 ASR、历史语音、原执行回执和被引用文件保留，不自动重发或改发文字。

升级步骤、兼容性变化与各项验证范围见发布说明。模型响应、工具请求与宿主资源仍会影响实际体验；版本更新不保证固定的延迟、费用或缓存命中率。

**当前开发分支：** 个人客户端与 QQ、Telegram 共用同一个运行时，平台原生传输经适配器接入。私聊按 Binding＋Presence、群按 SpaceBinding＋topic 隔离会话；人物记忆仍沿内部 Person。默认模型退役任意 OneBot action、戳一戳和远端转发历史展开，公共历史及成员目录只声明已观察账本。Plugin API **4.0**、Main 合同 **20**、迁移 **0128**；配置、升级及后续平台扩展见[平台适配与升级](docs/architecture/platform-adapters.md)。本地实施/验证进度见[实施记录](docs/architecture/Yuki-299与多平台解耦实施记录-2026-10-16.md)，正式 3.9.0 的历史说明保留如上。

养鲲已移至独立仓库 [Yuki-Kun-Game](https://github.com/YuanYeYouTao/Yuki-Kun-Game)，本仓库不再附带游戏源码或专用迁移。独立版需要 Plugin API 4.0 Host；安装与旧存档离线升级见插件仓库。

会话 [Compact](docs/architecture/conversation-compact.md)（[#417](https://github.com/YuanYeYouTao/Yuki/issues/417)、[#418](https://github.com/YuanYeYouTao/Yuki/issues/418)）由当前 Main 模型按真实容量整理公共历史或私有执行尾部。子 Agent 共用 Main 的完整工具合同，执行按自己的真实来源与授权核验；旧独立 Rollup 和 Work/ordinary 摘要链已退役。

## Yuki 能做什么

| 能力 | 使用方式 |
| --- | --- |
| 长期聊天与记忆 | 在群聊、私聊中持续交流，查询旧事，明确要求记住、纠正或删除事实 |
| 图片、语音和附件 | 接收图片、语音、视频或文档；同一会话内可不引用原附件继续追问，历史读取以已观察账本为准 |
| 跨平台社交操作 | 查询已观察成员、结构化 @、发送群消息或私聊、撤回自己的消息；共用内部目标和权限，协议由适配器处理 |
| 搜索与插件 | 使用配置好的联网工具和获准插件处理外部信息 |
| 持久工作环境 | 保存项目与文件，运行 Python、Node.js 或 Shell，安装依赖并交付结果 |
| 后台任务与自动化 | 启动作业后继续聊天，随后查询进度；按已授予的权限执行定时任务和续跑 |
| 表情与语音识别 | 表情包检索、分类和发送；入站语音识别保留 |

这些能力取决于部署配置、模型能力和授权范围。任务被接纳、执行完成和消息发送分别有状态记录；调用工具不等于结果已经交付。

## 持久工作环境

启用后，Yuki 拥有全会话共用的 Linux 工作目录，可以保存下载文件、Git 项目、脚本与依赖。文件工具和终端操作同一份文件，普通工作文件不再按 24 小时过期。

- 预装 Bash、Python、Node.js、Git 和基础编译工具，支持 pip、npm，以及由 Manager 管理的 apt 安装和环境检查点。
- 终端支持交互输入、增量输出、取消和后台执行。可以先启动一个任务，继续处理消息，再回来看结果。
- Bot 或 Manager 重启时，环境进程可以继续运行；环境本身重启后，普通任务标记中断，已登记服务按策略恢复。
- 选定文件发布为不可变快照后，通过对应平台的统一发送链路交付；旧 `artifact_id` 保持兼容。

默认家目录容量 2 GiB，容器内存上限 512 MiB，最多一个主要执行任务、四个终端会话和两个内部服务。环境没有浏览器或桌面，也不挂载 Bot 数据库、QQ 凭据或宿主 Docker 控制接口。

这是一项**单独部署的可选能力**，需要 Linux 宿主、gVisor 和 Yuki Manager；普通 Bot 部署包不会自动安装。配置、资源限制与恢复方式见[持久环境说明](docs/operations/persistent-environment.zh-CN.md)（[English](docs/operations/persistent-environment.md)）。

## 记忆与跨会话连续性

长期记忆用于保存稳定事实、偏好和有意义的经历；普通自动提取会聚合消息，明确要求记住、纠正和删除时则即时处理。主 Agent 自主判断何时调用 `search_memory`，不再每轮自动搜索或把全部长期事实塞入上下文。检索受当前主体的可见范围和工作预算约束，结果会说明截断或不完整状态。同 key 不是唯一槽位，事实按 ID 和真实证据保存；提取、SELF 自省和 Dream 整理可在模型配置中分别绑定连接。长会话通过 Main compact 检查点接续历史，原始历史仍有独立查询入口。

`short_state` 是全局共用、有界且会过期的短期记录区，用于暂存跨会话信息。写入只需要 slot/text，空正文清空对应槽位；每次激活只展示当前有效内容，不再保存首份或变化差量。它与长期记忆、持久文件分开，群聊和私聊不会自动拼成同一份完整聊天历史。

记忆读取仍有具体边界：历史共同群关系可以开放人物结构化事实，**其中可能包含私聊来源的事实**，但不开放他人的原始私聊或私有证据。部署前请阅读 [Memory 的范围与权限](docs/architecture/memory-v2.md)。模型提取和回忆可能出错，重要信息应核对来源。

## 图片、语音、文件与联网

- **图片与视频**：当前、引用和历史附件，以及 Agent 选取的工作区或获准工具图片，共用原主模型的原生图片输入。历史附件按内部事件索引、会话和缓存有效期核验；缺少图片能力时明确报告未读，不隐式换模型或调用独立 Qwen 视觉摘要。MP4/MOV 视频通过 FFmpeg 抽帧进入同一个主 Agent，不分析音轨，也不保证覆盖所有瞬间。后台表情分类和插件显式视觉能力仍可使用独立视觉连接。
- **接收语音**：私聊、符合回复策略的群聊及引用语音经 Qwen ASR 转写，进入聊天历史、搜索和公共 compact。按附件顺序保留已识别全文与来源，部分失败保留已完成转写。可复用千问连接，不依赖本地合成服务；语音合成与发送已退役。见[语音识别说明](docs/speech/recognition.md)。
- **文件阅读**：支持文本、代码、CSV/JSON、PDF 文字、DOCX 和 XLSX 的有界提取。扫描 PDF 不做 OCR，表格公式不重算，宏和附件中的代码不会因阅读而执行。在同一会话中，后续提问可不引用原附件；Yuki 按需读取 24 小时临时缓存。
- **联网**：安装向导与 Admin/CLI 共用模型搜索默认配置，无需 Tavily 密钥。DeepSeek Chat/Responses 保留主协议并生成共用密钥的独立 Messages 搜索连接，Gemini 默认使用独立搜索桥，Anthropic Messages 默认使用原生搜索。未设置联网开关及旧 `WEB_ENABLED=true` 均使用 `native`，显式关闭、外部后端和手选连接保持原选择。模型搜索失败如实返回，配套网页读取使用直接 HTTP；仅显式选择 Tavily 服务需要其密钥。自定义网关沿真实地址和凭据配置，旧 `deepseek_anthropic` 保留原搜索来源。配置及真实能力边界见[独立原生搜索](docs/deepseek-search-bridge.md)与[模型供应商说明](docs/architecture/model-providers.md)。

聊天、插件唤醒、自动化和任务续跑使用统一主 Agent 与固定完整工具合同。工具结构在部署内保持固定，执行时再检查权限与预算；这减少请求前缀变化，但不保证 Provider 的缓存命中率。

历史 Gemini / DeepSeek 缓存样本、计量覆盖和实验限制见 [3.9.0 缓存统计](docs/releases/v3.9.0.md#缓存与用量)。它们不是当前线上命中率或新版提速、费用下降的证明；缺失缓存字段继续记为未知。

## 配置与启动

基础部署需要：

- Linux amd64 或 arm64（包括 Apple Silicon 上的 Linux 虚拟机），或运行 Linux 容器的 Docker Desktop；
- Docker Engine 和 Docker Compose v2；
- 可用的模型服务配置，支持项目接入的 Chat Completions 或 Responses 协议；
- SnowLuma QQ 网关及登录账号；NapCat 支持已退役，现有网关抽象仍可用于其他实现。

从 [3.9.0 Release](https://github.com/YuanYeYouTao/Yuki/releases/tag/v3.9.0) 下载部署包，解压后可以手动填写 `.env` 和模型配置，也可以使用配置向导。

3.9.0 默认 direct 镜像提供 `linux/amd64` 与 `linux/arm64`，两套安装器识别 amd64/x86_64 和 arm64/aarch64。已有 ARM 部署若保留 `platform: linux/amd64`，请按升级指南核对并调整原服务配置；安装器不会覆盖已有 Compose。Code Mode 使用单独的 `codemode` 构建。

当前源码支持 Chat Completions、Responses、Claude Messages、Gemini GenerateContent 和常见供应商参数方言。模型、思考、结构化输出及搜索按 Profile 显式配置，不按消息内容暗中换 Provider。管理 API 保存连接后，新任务采用新配置，已派发请求仍沿原连接与协议状态继续。
能力、配置与恢复边界见[模型协议说明](docs/architecture/model-providers.md)及[多供应商示例](config/model_profiles.providers.example.toml)。

Linux：

```bash
curl -fLO https://github.com/YuanYeYouTao/Yuki/releases/download/v3.9.0/install.sh
chmod +x install.sh
./install.sh
```

Windows PowerShell：

```powershell
Invoke-WebRequest -Uri https://github.com/YuanYeYouTao/Yuki/releases/download/v3.9.0/install.ps1 -OutFile install.ps1
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

**向导只负责配置。** 在空目录中下载并校验部署包，在已有部署中保留 Compose、插件和数据；确认后备份并写入配置。它不会停服、迁移数据库、启动服务或切换网关。

3.9.0 的模型连接保存在 `webui-config/model_profiles.toml`。旧部署若只有
`config/model_profiles.toml`，先按[模型配置路径迁移](docs/operations/model-profile-path-migration.md)
核对并迁移；向导不会自动覆盖当前模型连接。选定文件缺失时启动会明确失败。

首次部署在配置完成后，进入部署目录执行：

```bash
docker compose config --quiet
docker compose pull
docker compose run --rm --no-deps --entrypoint qq-ai-bot-cli bot init-db
docker compose up -d
```

还需完成 QQ 登录和所选插件的初始化。首次部署和从旧版升级见 [3.9.0 升级指南](docs/upgrade-3.9.0.md)。已有部署应保留原项目名、Compose 覆盖文件和挂载配置。

正式镜像为 `ghcr.io/yuanyeyoutao/yuki-qqbot:3.9.0`。3.9.0 不再提供 TTS Worker。发布包提供 `SHA256SUMS`。单独下载的环境模板附件名为 `default.env.example`，压缩包内仍为 `.env.example`。

## 升级与日常维护

3.9.0 使用 Plugin API **3.5**、主 manifest **19** 和 Code API **`yuki.codemode.api.v2`**，数据库单一 head 为 **0119**；历史 3.8.4 正式包的 head 为 **0072**。升级仍以实际镜像随包迁移为准，应用版本号不能替代数据库检查，不能通过 `stamp` 跳过迁移。插件需移除旧 MCP、退役事件/权限和关系服务依赖，按 [API 3.5 迁移](docs/plugin-development/api-3.5-migration.md)适配实际合同并重新批准；Host 精确匹配 API 版本与 manifest。

从旧版升级时，先按[升级指南](docs/upgrade-3.9.0.md)核对旧 MCP 挂载、环境配置和管理权限；退役的管理授权会阻止严格校验通过。数据库提交新 head 后不能仅切回旧镜像，也不能用旧备份覆盖升级后的消息和回执。

升级前保存一致的数据库、配置、插件及文件备份；持久环境还需保存家目录与运行回执。升级 Bot 时只暂停 Bot 写入，保留网关和其他服务；持久环境的备份与恢复按其独立说明执行。回退时应先保全升级后的新消息、文件和回执，详见 [3.9.0 升级指南](docs/upgrade-3.9.0.md)。

```bash
docker compose ps
docker compose logs --tail 200 bot
docker compose exec bot qq-ai-bot-cli gateway doctor --provider snowluma
```

所有命令沿用部署时的 Compose 参数。同一 QQ 只允许一条活动连接，重复连接不会挤掉旧连接。旧部署的 Compose 与 `.env` 不会由安装器自动替换，退役配置处理见 [SnowLuma 部署](docs/deployment/snowluma.md)与[升级指南](docs/upgrade-3.9.0.md#旧-napcat-部署配置)。

## 相邻项目

[Alice](https://github.com/LlmKira/Alice) 探索 AI 如何持续参与真实聊天；[Letta](https://docs.letta.com/) 关注有记忆、能保持状态的 Agent；[AstrBot](https://docs.astrbot.app/) 提供面向 QQ 等聊天平台的 Agent 与插件框架。Yuki 关注这些能力如何在同一个持续存在的群聊主体中协同工作：认识人、积累共同经历与记忆，并自主判断何时参与。

## 架构与开发

开发前阅读 [共同架构约束](docs/architecture/development-contract.md) 与
[架构文档索引](docs/architecture/README.md)。历史任务书不替代现行合同。

一个数据库对应一个长期存在的 Yuki。人物、群空间、QQ 账号和网关连接分别建模，聊天历史与记忆不绑定在某一次登录连接上。工具由后端执行权限、预算、幂等和审计检查。

当前开发分支提供 QQ、Telegram、个人客户端与管理 CLI；旧浏览器管理界面与旧控制面实现已从源码退役。
管理入口调用原领域服务与回执，权限及当前方法见[管理 API](docs/architecture/admin-api.md)。
个人客户端 API 与参考 CLI 已按 #299 完成本地实现和验证，见[交付记录](docs/operations/issue-299-delivery-2026-10-15.md)；合并与上线状态分别记录。

```bash
uv sync --extra dev
uv run ruff format --check
uv run ruff check
uv run mypy --platform linux src
uv run pytest
```

开发时按改动范围选择定向验证。Quality 执行源码检查、前端测试/构建与保留回归；Release 验证版本身份、amd64/arm64 direct 镜像和无源码部署包，不再运行旧 Memory 质量门或重复发布链。
常驻回归使用 fake Provider 和隔离数据库；Gemini、DeepSeek 等付费 API 的缓存对照单独手动执行，
报告区分冷轮、热轮和缺失计量，不作为普通测试或固定命中率门槛。

| 文档 | 内容 |
| --- | --- |
| [使用帮助](docs/help.md) | 聊天、命令与日常操作 |
| [架构说明](docs/architecture/canonical-runtime.md) | 人物、空间、账号和会话的关系 |
| [开发约束](docs/architecture/development-contract.md) | 事件 ID、解耦边界、固定工具、续跑和事务原则 |
| [会话 Compact](docs/architecture/conversation-compact.md) | 长会话的历史压缩 |
| [Memory](docs/architecture/memory-v2.md) | 记忆提取、检索和权限 |
| [Plugin API 4.0](docs/plugin-development/index.md) | 插件开发与能力边界 |
| [持久子 Agent](docs/architecture/persistent-subagents.md) | 任务树、父子问答、完整工具能力与恢复 |
| [工具结果](docs/architecture/tool-results.md) | 结果预算、媒体与持久回执 |
| [版本化发布](docs/operations/versioned-docker-release.md) | 镜像、下载包与发布流程 |
| [CHANGELOG](CHANGELOG.md) | 历史变更 |

## License

当前源码采用 [AGPL-3.0-only](LICENSE)（GNU Affero General Public License 第 3 版，仅限该版本）。历史发布版本以对应版本随附的 LICENSE 为准。
