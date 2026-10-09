<!-- release-baseline: version=3.9.0 schema=0102 -->

中文（默认） · [English](README.en.md)

<div align="center">

<p><img src="img/Yuki_2.png" alt="Yuki" width="280"></p>

<h1>Yuki</h1>

<p>一个在真实 QQ 对话中持续存在的社会化 AI Agent</p>

<p>
  <a href="https://github.com/YuanYeYouTao/Yuki/releases/tag/v3.8.4"><img src="https://img.shields.io/badge/Release-3.8.4-blue" alt="Yuki 3.8.4"></a>
  <img src="https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white" alt="Python 3.12">
  <img src="https://img.shields.io/badge/Deploy-Docker%20Compose-2496ED?logo=docker&logoColor=white" alt="Docker Compose">
  <a href="https://github.com/YuanYeYouTao/Yuki/actions/workflows/quality.yml"><img src="https://github.com/YuanYeYouTao/Yuki/actions/workflows/quality.yml/badge.svg" alt="Quality"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-yellow" alt="MIT License"></a>
</p>

[下载正式版 3.8.4](https://github.com/YuanYeYouTao/Yuki/releases/tag/v3.8.4) · [3.8.4 发布说明](docs/releases/v3.8.4.md) · [3.8.4 升级指南](docs/upgrade-3.8.4.md) · [使用帮助](docs/help.md)

</div>

Yuki 是一个开源、自托管的社会化 AI Agent，探索数字生命如何在真实社交场景中持续存在。她当前运行在 QQ 私聊和群聊中，记住人与共同经历，也能使用工具和持久工作环境完成跨消息的任务。身份和记忆由自己的数据库保存，更换模型、QQ 账号或网关时可以继续沿用。

**当前正式版为 3.8.4。** 本版将可见发言统一为主 Agent 显式 `send_message`，并加入可选的群聊语义观察、SELF 自主参与和自主自动化。Yuki 可决定发言、分条发送或沉默；自主参与默认关闭，真实 QQ 群聊中的长期效果仍在验证。工作环境需要单独部署；3.8.4 正式包不包含管理 WebUI。

**当前开发基线为 3.9.0，尚未发布。** 源码变更和全部已合并 PR 见 [3.9.0 发布说明草案](docs/releases/v3.9.0.md)；升级准备见 [3.9.0 升级草案](docs/upgrade-3.9.0.md)。正式下载入口仍为上方的 3.8.4 Release。

## 3.9.0 的主要变化

- **默认 direct 构建**：保持同一主 Agent 循环和固定完整工具声明，镜像不包含 Monty binding、worker 或 launcher。Code Mode 是需显式选择镜像并启用的可选模式，启用前单独验证目标机器的资源余量。
- **管理 WebUI 与共享执行 Runtime**：在手帐风格界面查看真实聊天、执行轨迹、工具回执、模型用量与工作区，管理模型连接和任务路由。聊天、SELF、插件、自动化及 Work 续跑共用主 Agent，WebUI 默认关闭。
- **可恢复的长任务**：Work 保留原目标、累计预算、协议检查点和交付回执；上下文按真实请求容量整理，研究资料按需读取。终端等待、子任务和用户追加要求沿原任务接续，已确认的效果不因重启重做。
- **简化记忆策略**：同 key 的独立事实可以共存，修改与删除按 fact ID；删除归因/强化评分、自动升格和容量腾位。提取、SELF 与 Dream 可分别选择模型，允许部分整理与未决结果。
- **Agent 按需取资料**：长期事实由 `search_memory` 按需检索，不再每轮自动注入。当前、引用、历史附件、工作区和获准工具图片统一交给原主模型的原生多模态输入。
- **联网与真实协议反馈**：新安装默认开启模型搜索，保留显式禁用和连接能力边界。适配器不从正文制造工具调用，也不暗中追加付费续跑；真实错误和已知用量保留，已派发或未知效果先核原回执。
- **减少回复前的数据库等待**：历史读取、上下文准备和可丢诊断写入移出关键写事务；后台维护采用索引、有界分页和短事务。实际延迟仍受模型响应、工具请求和宿主资源影响。
- **移除现有 MCP**：连接、工具目录、管理页面、SDK 和自动化入口一并退役；通用工具结果、媒体和回执继续保留。Plugin API 升至 **3.4**，数据库 head 为 **0104**，旧插件需适配并重新批准。
- **退出语音输出**：Genie 合成、声线/偏好、工具参数、SDK/管理功能、Worker 与发布依赖一并移除；入站/引用 ASR、历史语音和原回执保留。升级前先核旧执行、冷备语音事实与被引用文件，再迁移专属表；不自动重发或改发文字。

这些是当前源码变化。各 Provider 的真实 API、自然聊天延迟和长期任务效果仍按各自验收记录核对；可选 Code Mode 的隔离验证不代表生产容量或长期内存验收。

## 当前源码能做什么

| 能力 | 使用方式 |
| --- | --- |
| 长期聊天与记忆 | 在群聊、私聊中持续交流，查询旧事，明确要求记住、纠正或删除事实 |
| 图片、语音和附件 | 接收图片、语音、视频或文档；同一会话内可不引用原附件继续追问 |
| QQ 社交操作 | 查询成员、结构化 @、发送群消息或私聊、撤回自己的消息；目标与权限由后端校验 |
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
- 选定文件发布为不可变快照后，通过 QQ 发送链路交付；旧 `artifact_id` 保持兼容。

默认家目录容量 2 GiB，容器内存上限 512 MiB，最多一个主要执行任务、四个终端会话和两个内部服务。环境没有浏览器或桌面，也不挂载 Bot 数据库、QQ 凭据或宿主 Docker 控制接口。

这是一项**单独部署的可选能力**，需要 Linux 宿主、gVisor 和 Yuki Manager；普通 Bot 部署包不会自动安装。配置、资源限制与恢复方式见[持久环境说明](docs/operations/persistent-environment.zh-CN.md)（[English](docs/operations/persistent-environment.md)）。

## 记忆与跨会话连续性

长期记忆用于保存稳定事实、偏好和有意义的经历；普通自动提取会聚合消息，明确要求记住、纠正和删除时则即时处理。主 Agent 自主判断何时调用 `search_memory`，不再每轮自动搜索或把全部长期事实塞入上下文。检索受当前主体的可见范围和工作预算约束，结果会说明截断或不完整状态。同 key 不是唯一槽位，事实按 ID 和真实证据保存；提取、SELF 自省和 Dream 整理可在模型配置中分别绑定连接。长会话通过 Rollup 压缩历史，原始历史仍有独立查询入口。

`short_state` 是全局共用、有界且会过期的短期记录区，用于暂存跨会话信息；它与长期记忆、持久文件分开。群聊和私聊不会自动拼成同一份完整聊天历史。

记忆读取仍有具体边界：历史共同群关系可以开放人物结构化事实，**其中可能包含私聊来源的事实**，但不开放他人的原始私聊或私有证据。部署前请阅读 [Memory 的范围与权限](docs/architecture/memory-v2.md)。模型提取和回忆可能出错，重要信息应核对来源。

## 图片、语音、文件与联网

- **图片与视频**：当前、引用和历史附件，以及 Agent 选取的工作区或获准工具图片，共用原主模型的原生图片输入。历史附件按内部事件索引、会话和缓存有效期核验；缺少图片能力时明确报告未读，不隐式换模型或调用独立 Qwen 视觉摘要。MP4/MOV 视频通过 FFmpeg 抽帧进入同一个主 Agent，不分析音轨，也不保证覆盖所有瞬间。后台表情分类和插件显式视觉能力仍可使用独立视觉连接。
- **接收语音**：私聊、符合回复策略的群聊及引用语音经 Qwen ASR 转写，进入聊天历史、搜索和 Rollup。可复用千问连接，不依赖本地合成服务；语音合成与发送已退役。见[语音识别说明](docs/speech/recognition.md)。
- **文件阅读**：支持文本、代码、CSV/JSON、PDF 文字、DOCX 和 XLSX 的有界提取。扫描 PDF 不做 OCR，表格公式不重算，宏和附件中的代码不会因阅读而执行。在同一会话中，后续提问可不引用原附件；Yuki 按需读取 24 小时临时缓存。
- **联网**：新安装未设置联网开关时默认 `WEB_MODE=native`；显式 `disabled` 继续禁用，既有连接的搜索选择不被改写。开启不代表任意模型支持搜索：原生搜索需要连接声明实际能力，Gemini 可显式使用独立搜索桥；外部 `web_search` 需要配置后端。外部搜索默认使用 Tavily，也可设置 `WEB_MODE=tavily`、`WEB_SEARCH_BACKEND=deepseek_anthropic` 使用 [DeepSeek 搜索桥](docs/deepseek-search-bridge.md)，Tavily 密钥仅用于可选失败兜底。DeepSeek 主 Agent 当前不声明原生搜索。协议和配置边界见[模型供应商说明](docs/architecture/model-providers.md)。

聊天、插件唤醒、自动化和任务续跑使用统一主 Agent 与固定完整工具合同。工具结构在部署内保持固定，执行时再检查权限与预算；这减少请求前缀变化，但不保证 Provider 的缓存命中率。

历史样本中，Gemini 自然群聊一个回复轮的三次请求，输入加权缓存比例为 **90.55%**；DeepSeek Responses 的手工累积聊天实验九次请求为 **95.11%**，其中八次热续接为 **99.02%**。日期、场景和计量覆盖不同，不能作为当前线上命中率或修改前后提速对照；完整样本与多场景结果见 [3.9.0 缓存统计](docs/releases/v3.9.0.md#缓存与用量)。

## 配置与启动

基础部署需要：

- Linux amd64，或运行 Linux 容器的 Windows Docker Desktop；
- Docker Engine 和 Docker Compose v2；
- 可用的模型服务配置，支持项目接入的 Chat Completions 或 Responses 协议；
- SnowLuma QQ 网关及登录账号。Yuki 不再支持 NapCat；现有网关抽象仍可用于其他实现。

从 [3.8.4 Release](https://github.com/YuanYeYouTao/Yuki/releases/tag/v3.8.4) 下载部署包，解压后可以手动填写 `.env` 和模型配置，也可以使用配置向导。

开发主线增加 Claude Messages、Gemini GenerateContent 和常见 Chat 供应商方言。
多个供应商可按任务显式配置；能力与恢复边界见[模型协议说明](docs/architecture/model-providers.md)
及[多供应商示例](config/model_profiles.providers.example.toml)。这不是既有 3.8.4 部署包的能力声明。

Linux：

```bash
curl -fLO https://github.com/YuanYeYouTao/Yuki/releases/download/v3.8.4/install.sh
chmod +x install.sh
./install.sh
```

Windows PowerShell：

```powershell
Invoke-WebRequest -Uri https://github.com/YuanYeYouTao/Yuki/releases/download/v3.8.4/install.ps1 -OutFile install.ps1
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

**向导只负责配置。** 在空目录中下载并校验部署包，在已有部署中保留 Compose、插件和数据；确认后备份并写入配置。它不会停服、迁移数据库、启动服务或切换网关。

当前 3.9.0 源码的模型连接保存在 `webui-config/model_profiles.toml`。3.8.4 正式包沿随包路径；升级到当前源码且旧部署只有
`config/model_profiles.toml`，先按[模型配置路径迁移](docs/operations/model-profile-path-migration.md)
核对并迁移；向导不会自动覆盖当前 WebUI 的模型连接。选定文件缺失时启动会明确失败。

首次部署在配置完成后，进入部署目录执行：

```bash
docker compose config --quiet
docker compose pull
docker compose run --rm --no-deps --entrypoint qq-ai-bot-cli bot init-db
docker compose up -d
```

还需完成 QQ 登录和所选插件的初始化。首次部署和从旧版升级见 [3.8.4 升级指南](docs/upgrade-3.8.4.md)。已有部署应保留原项目名、Compose 覆盖文件和挂载配置。

正式镜像为 `ghcr.io/yuanyeyoutao/yuki-qqbot:3.8.4`。历史 3.8.4 的 TTS Worker 资产仍属该旧版本，不适用于当前源码。发布包提供 `SHA256SUMS`。单独下载的环境模板附件名为 `default.env.example`，压缩包内仍为 `.env.example`。

## 升级与日常维护

3.9.0 源码使用 Plugin API **3.4**，数据库单一 head 为 **0104**；3.8.4 正式包的 head 为 **0072**。升级仍以实际镜像随包迁移为准，应用版本号不能替代数据库检查，不能通过 `stamp` 跳过迁移。插件需移除 MCP 依赖、适配 API 3.4 并重新批准；旧 `llm.generate` / `agent.run` 已统一到主入口。

准备使用 3.9.0 开发提交时，先按[升级草案](docs/upgrade-3.9.0.md)核对旧 MCP 挂载、环境配置和管理权限；退役的管理授权会阻止严格校验通过。数据库提交新 head 后不能仅切回旧镜像，也不能用旧备份覆盖升级后的消息和回执。

升级前保存一致的数据库、配置、插件及文件备份；持久环境还需保存家目录与运行回执。暂停写入只涉及 Bot 和相关 Manager，不需要关闭整个 Docker 或 QQ 网关。回退时应先保全升级后的新消息、文件和回执；当前源码见 [3.9.0 升级草案](docs/upgrade-3.9.0.md)。

```bash
docker compose ps
docker compose logs --tail 200 bot
docker compose exec bot qq-ai-bot-cli gateway doctor --provider snowluma
```

所有命令沿用部署时的 Compose 参数。同一 QQ 只允许一条活动连接，重复连接不会挤掉旧连接。旧部署的 Compose 与 `.env` 不会由安装器自动替换，退役配置处理见 [SnowLuma 部署](docs/deployment/snowluma.md)与[升级草案](docs/upgrade-3.9.0.md#旧-napcat-部署配置)。

## 相邻项目

[Alice](https://github.com/LlmKira/Alice) 探索 AI 如何持续参与真实聊天；[Letta](https://docs.letta.com/) 关注有记忆、能保持状态的 Agent；[AstrBot](https://docs.astrbot.app/) 提供面向 QQ 等聊天平台的 Agent 与插件框架。Yuki 关注这些能力如何在同一个持续存在的群聊主体中协同工作：认识人、积累共同经历与记忆，并自主判断何时参与。

## 架构与开发

开发前阅读 [共同架构约束](docs/architecture/development-contract.md) 与
[架构文档索引](docs/architecture/README.md)。历史任务书不替代现行合同。

一个数据库对应一个长期存在的 Yuki。人物、群空间、QQ 账号和网关连接分别建模，聊天历史与记忆不绑定在某一次登录连接上。工具由后端执行权限、预算、幂等和审计检查。

目前提供 QQ 交互、CLI 和共享 Control Plane。当前源码的手帐管理 WebUI 与同源管理 HTTP 默认关闭，
接入聊天、执行轨迹、配置和自动化等服务；3.8.4 正式发布不包含此管理界面。
构建、权限及功能边界见 [WebUI 文档](docs/architecture/webui-console.md)。

```bash
uv sync --extra dev
uv run ruff format --check
uv run ruff check
uv run mypy --platform linux src
uv run pytest
```

开发时按改动范围选择定向验证。Quality 执行源码检查、前端测试/构建与保留回归；Release 验证版本身份、单个 direct 镜像和无源码部署包，不再运行旧 Memory 质量门或重复发布链。
常驻回归使用 fake Provider 和隔离数据库；Gemini、DeepSeek 等付费 API 的缓存对照单独手动执行，
报告区分冷轮、热轮和缺失计量，不作为普通测试或固定命中率门槛。

| 文档 | 内容 |
| --- | --- |
| [使用帮助](docs/help.md) | 聊天、命令与日常操作 |
| [架构说明](docs/architecture/canonical-runtime.md) | 人物、空间、账号和会话的关系 |
| [开发约束](docs/architecture/development-contract.md) | 事件 ID、解耦边界、固定工具、续跑和事务原则 |
| [Rollup](docs/architecture/conversation-rollup.md) | 长会话的历史压缩 |
| [Memory](docs/architecture/memory-v2.md) | 记忆提取、检索和权限 |
| [Plugin API 3.4](docs/plugin-development/index.md) | 插件开发与能力边界 |
| [工具结果](docs/architecture/tool-results.md) | 结果预算、媒体与持久回执 |
| [版本化发布](docs/operations/versioned-docker-release.md) | 镜像、下载包与发布流程 |
| [CHANGELOG](CHANGELOG.md) | 历史变更 |

## License

[MIT](LICENSE)
