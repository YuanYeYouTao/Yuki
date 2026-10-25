<!-- release-baseline: version=4.0.0 schema=0143 -->

中文（默认） · [English](README.en.md)

<div align="center">

<p><img src="img/Yuki_2.png" alt="Yuki" width="280"></p>

<h1>Yuki</h1>

<p>一个在真实对话中持续存在的社会化 AI Agent</p>

<p>
  <a href="docs/releases/v4.0.0.md"><img src="https://img.shields.io/badge/Development-4.0.0-orange" alt="Yuki 4.0.0 development"></a>
  <a href="https://github.com/YuanYeYouTao/Yuki/releases/tag/v3.9.0"><img src="https://img.shields.io/badge/Stable-3.9.0-blue" alt="Yuki 3.9.0 stable"></a>
  <img src="https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white" alt="Python 3.12">
  <a href="https://github.com/YuanYeYouTao/Yuki/actions/workflows/quality.yml"><img src="https://github.com/YuanYeYouTao/Yuki/actions/workflows/quality.yml/badge.svg" alt="Quality"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-AGPL--3.0--only-blue" alt="AGPL-3.0-only License"></a>
</p>

[4.0.0 更新说明](docs/releases/v4.0.0.md) · [升级到 4.0](docs/upgrade-4.0.0.md) · [Benchmark 报告](docs/benchmarks/2026-10-20-yuki-flash-1017/README.md) · [桌面客户端](https://github.com/Institute-of-Agent-Life/Yuki-Desktop) · [使用帮助](docs/help.md)

</div>

Yuki 是一个开源、自托管的社会化 AI Agent。她通过 QQ、Telegram、飞书、Lark 和个人客户端参与对话，记住人物、偏好和共同经历，也能在持久 Linux 环境里处理文件、运行程序、维护项目和执行定时任务。人物身份、记忆与任务由自己的数据库保存，更换模型、账号或网关后可以继续使用。

**当前开发基线为 4.0.0，正式下载版本为 [3.9.0](https://github.com/YuanYeYouTao/Yuki/releases/tag/v3.9.0)。** 4.0 把多平台和个人客户端放进同一套后端：聊天、SELF 自主行动、定时任务、插件和长任务续跑使用共同 Main，人物资料和记忆跟着人走，各个聊天入口保留自己的对话。

## 4.0 这一版

- **从 QQ 走向多平台和个人客户端。** QQ、Telegram、飞书、Lark、App API 与 ACP 服务共用身份、记忆、工具和任务。飞书和 Lark 使用企业自建应用的长连接，支持单聊、群聊和话题，接入方式见[配置说明](docs/operations/feishu.zh-CN.md)。个人设备可以配对、发送附件、接收事件、查看任务，并向已有任务补充信息；桌面项目 [Yuki Desktop](https://github.com/Institute-of-Agent-Life/Yuki-Desktop) 单独维护。旧管理 WebUI 已退出，管理使用 CLI 和 `/ai admin`。
- **连接自己电脑上的编码 Agent。** 独立[本机连接器](docs/operations/local-agent-connector.zh-CN.md)让服务器上的 Yuki 查询和监督本人授权的 Codex、Claude Code 或 ACP Agent，进度和问题交回原任务。Codex 接既有 daemon，Claude 使用官方 SDK 的新建或明确恢复；三种驱动的实测边界见[实施记录](docs/operations/local-agent-connector-implementation-20261023.md)。
- **把人物和账号分开。** 同一个人可以关联多个账号，也可以没有平台账号。同人合并、账号转交、暂停和解绑分别处理；改昵称或换账号不会把旧消息、记忆和任务改归别人。超级管理员资格属于人物。
- **让终端承担日常工作。** 普通文件、项目和依赖直接由持久终端操作，支持交互输入、后台执行、查询和取消。direct 与可选 Code Mode 使用同一套工具和执行后端。
- **长任务继续沿原记录推进。** Work 保存目标、父子关系、模型请求、工具执行与交付记录。新消息可以补充当前工作；重启后查询原执行，接着处理已经发生的结果。Main Compact 整理长会话和执行上下文，沿用当前模型。
- **媒体按需读取，表情独立配图。** 图片、视频、贴纸和文件先提供索引，Agent 再决定读哪些内容。共享表情目录保存文件与检索资料，文字发送和后台配图各有执行记录。
- **整理插件与后台链路。** Plugin API 升至 7.0，养鲲和 GitHub 监控迁至独立仓库。上下文准备、人物查询、诊断和 SQLite 热查询也做了集中清理与修复。

[完整发布说明](docs/releases/v4.0.0.md)按功能介绍变化，并列出正式 3.9.0 之后全部 37 个已合并 PR 及贡献者。

## 聊天之外，做事的能力如何

我们在本地外部任务容器中，给 Yuki 的原生 Main／Work 执行链跑了两套完整评测。使用 **DeepSeek V4.1 Flash 1017**，沿用 Yuki 的工具和执行记录。

| Benchmark | 通过 | 任务解决率 | 输入缓存命中率 |
| --- | ---: | ---: | ---: |
| SWE-bench Lite，300 题 | 216 / 300 | **72.00%** | **97.47%**，全量计量完整 |
| Terminal-Bench 2.0，89 题 | 66 / 89 | **74.16%** | **98.32%**，已记录用量 |

SWE Lite 覆盖真实仓库的 issue 修复；Terminal-Bench 涵盖编译、系统配置、数据处理等终端任务。SWE 累计输入约 4.63 亿 token，其中约 4.52 亿命中缓存；多轮任务中的重复上下文得到了大量复用。

评测于 2026-10-20 开始，固定源码为 `313b6b9b`，早于本次 4.0.0 基线。TB 的 84 题计量完整，5 题保留待核标记；只取完整 84 题，缓存命中率为 **98.26%**。6 题同时记录超时与验证通过，按原 Harbor reward 计入通过数。配置、逐题结果、token、耗时和失败分析都在[报告目录](docs/benchmarks/2026-10-20-yuki-flash-1017/README.md)。

## 日常使用

| 能力 | 可以怎样使用 |
| --- | --- |
| 长期聊天与记忆 | 在群聊和私聊里持续交流，查询旧事，明确要求记住、纠正或删除事实 |
| 文件与多模态 | 阅读图片、视频帧、语音转写和文档，继续追问，保存原件或交付处理结果 |
| 搜索与网页 | 按配置使用模型原生搜索、独立搜索连接或外部搜索服务，读取网页 |
| 持久工作环境 | 保存项目，运行 Python、Node.js 和 Shell，安装依赖，启动后台工作 |
| 任务与自动化 | 查看进度、补充要求、等待事件、安排定时任务，管理父子任务 |
| 跨平台交流 | 查找已观察联系人、发送文字和文件、原生提及人物，读取获准历史 |

文字、图片、文件和表情统一使用 `send`。发文件直接传工作区路径，程序内部冻结交付版本，无需模型先发布；收藏和附件保存直接复制到最终文件，不留下永久中转副本。已发送、部分成功和未知结果仍认原回执，不因重试重新读取或重发。

模型支持 Chat Completions、Responses、Claude Messages 和 Gemini GenerateContent。模型连接、思考档位、搜索与任务路由都通过 Profile 配置。

记忆由 Agent 按需检索；提取、SELF 自省和 Dream 整理可分别选择模型。长期事实、短期记录、聊天历史和工作文件各自保存。记忆的可见范围见 [Memory 文档](docs/architecture/memory-v2.md)。

持久工作环境是单独部署的 Linux 环境，提供 Bash、Python、Node.js、Git 和编译工具。普通工作文件长期保存；入站媒体原件使用共享的 24 小时缓存，需要保留的内容可复制到工作区或表情目录。部署与交互终端见[持久环境说明](docs/operations/persistent-environment.zh-CN.md)。

## 客户端与生态

| 项目 | 用途 |
| --- | --- |
| [Yuki Desktop](https://github.com/Institute-of-Agent-Life/Yuki-Desktop) | 跨平台桌面客户端项目；当前公开仓库提供简介与许可证 |
| [个人 API 与 ACP](docs/architecture/client-api.md) | 设备配对、消息、附件、事件流、任务查询与接续，外部 ACP 客户端接入 |
| [Yuki-Semantic-Participation](https://github.com/Institute-of-Agent-Life/Yuki-Semantic-Participation) | 可选的语义参与控制器 |
| [Yuki-Kun-Game](https://github.com/Institute-of-Agent-Life/Yuki-Kun-Game) | 独立养鲲游戏插件 |
| [Yuki-GitHub-Monitor](https://github.com/Institute-of-Agent-Life/Yuki-GitHub-Monitor) | 独立 GitHub 仓库监控插件 |

角色设定使用一份可自由编辑的 `character/SOUL.md`，角色名称在资料中独立修改。空人格也可运行；工具、权限和任务规则由程序维护。升级现有部署请先按[单 SOUL 升级说明](docs/operations/soul-deployment-20261022.md)显式整理原件并初始化角色名称。

## 安装与升级

使用现成部署包，可从 [3.9.0 Release](https://github.com/YuanYeYouTao/Yuki/releases/tag/v3.9.0) 下载，按 [3.9.0 安装与升级指南](docs/upgrade-3.9.0.md)配置。它的镜像为 `ghcr.io/yuanyeyoutao/yuki-qqbot:3.9.0`，提供 Linux amd64 和 arm64。

开发 4.0 源码需要 Python 3.12、uv 和 C 编译器。Python 的 SQLite 须支持扩展加载，以下使用 uv 管理的 Python。Linux 使用 GCC/Clang，macOS 使用 Xcode Command Line Tools，Windows 在 MSVC 开发者终端执行，或用 `CC` 指定 MinGW GCC。Vec1 随包构建；安装已发布的平台 wheel 或使用镜像时无需编译器。

```bash
git clone https://github.com/YuanYeYouTao/Yuki.git
cd Yuki
uv sync --all-extras --managed-python
uv run yuki --help
```

当前命令统一为 `yuki`，服务入口为 `yuki-bot`。从 3.9 迁移的步骤见 [4.0 升级指南](docs/upgrade-4.0.0.md)。QQ 使用 SnowLuma；仅使用飞书、Lark、Telegram 或个人客户端时，无需配置 QQ 账号。

后续开发的 B1–B6 代码清理包含连接器 `0.2.0` / WSS 协议 `2` 配套升级、Memory Fake 结果修正及旧 CLI/Python 表面撤销，见[兼容与升级说明](docs/upgrade-code-deletion-20261023.md)。这不代表新的正式 Release 或已部署状态。

4.0.0 发布基线为 schema **0143**、Plugin API **7.0**、manifest **25**。当前开发源码为 schema **0153**、Plugin API **8.0**、manifest **33**；聊天历史支持平台/群私聊组合筛选、可分页的原入口/话题发现，显式查询可读取重开上下文前的记录。

旧库的正文索引转换需要停业务写入，完成升级后再启动 Bot；本次0148仅增加来源定位索引，不重建全文。升级前备份数据库、配置、插件和工作文件，详见[现行历史读取合同](docs/architecture/chat-history.md)、[原实施记录](docs/operations/chat-history-448-implementation-20261022.md)与[来源及执行优化任务书](docs/architecture/Yuki-聊天来源发现与检索执行删减优化任务书-2026-10-22.md)。

0153 将已有向量原样移入同库 Vec1 表，不重新调用 Embedding API。升级前停止旧 writer 并备份主库与参与快照文件，使用带 Vec1 的新构建完成迁移后再启动；步骤及本地性能对照见[算法优化实施记录](docs/operations/algorithm-performance-implementation-20261026.md)。

## 架构与开发

开发前阅读[共同架构约束](docs/architecture/development-contract.md)和[架构索引](docs/architecture/README.md)。普通聊天、后台任务与插件复用同一主 Agent；原生平台协议留在适配器中，人物、会话和任务沿内部 ID 关联。

```bash
uv sync --all-extras
uv run ruff check .
uv run mypy --platform linux src
uv run pytest
```

| 文档 | 内容 |
| --- | --- |
| [使用帮助](docs/help.md) | 聊天命令、模型配置和日常管理 |
| [4.0.0 发布说明](docs/releases/v4.0.0.md) | 主要功能、修复、兼容性变化和完整 PR 清单 |
| [Benchmark 报告](docs/benchmarks/2026-10-20-yuki-flash-1017/README.md) | 两套全量评测、用量、缓存与逐题数据 |
| [Canonical runtime](docs/architecture/canonical-runtime.md) | 人物、账号、空间和会话 |
| [飞书与 Lark 接入](docs/operations/feishu.zh-CN.md) | 自建应用、群消息权限、长连接和媒体 |
| [会话 Compact](docs/architecture/conversation-compact.md) | 长会话和执行上下文整理 |
| [Plugin API 8.0](docs/plugin-development/index.md) | 插件开发与 SDK |
| [持久子 Agent](docs/architecture/persistent-subagents.md) | 任务树、父子问答和恢复 |
| [CHANGELOG](CHANGELOG.md) | 历史版本变化 |

## License

当前源码采用 [AGPL-3.0-only](LICENSE)。历史版本以随包许可证为准。
