<!-- release-baseline: version=4.0.0 schema=0143 -->

[中文](README.md) · English

<div align="center">

<p><img src="img/Yuki_2.png" alt="Yuki" width="280"></p>

<h1>Yuki</h1>

<p>A social AI agent with a lasting presence in real conversations</p>

<p>
  <a href="docs/releases/v4.0.0.en.md"><img src="https://img.shields.io/badge/Development-4.0.0-orange" alt="Yuki 4.0.0 development"></a>
  <a href="https://github.com/YuanYeYouTao/Yuki/releases/tag/v3.9.0"><img src="https://img.shields.io/badge/Stable-3.9.0-blue" alt="Yuki 3.9.0 stable"></a>
  <img src="https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white" alt="Python 3.12">
  <a href="https://github.com/YuanYeYouTao/Yuki/actions/workflows/quality.yml"><img src="https://github.com/YuanYeYouTao/Yuki/actions/workflows/quality.yml/badge.svg" alt="Quality"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-AGPL--3.0--only-blue" alt="AGPL-3.0-only License"></a>
</p>

[4.0.0 release notes](docs/releases/v4.0.0.en.md) · [Upgrade to 4.0](docs/upgrade-4.0.0.en.md) · [Benchmark report](docs/benchmarks/2026-10-20-yuki-flash-1017/SUMMARY.en.md) · [Desktop client](https://github.com/Institute-of-Agent-Life/Yuki-Desktop) · [Help](docs/help.md)

</div>

Yuki is an open-source, self-hosted social AI agent. She joins conversations through QQ, Telegram, Feishu, Lark and personal clients, remembers people and shared experiences, and uses a persistent Linux environment to work with files, run programs, maintain projects and carry out scheduled tasks. Identity, memory and tasks live in your own database and can continue across changes of model, account or gateway.

**The development baseline is 4.0.0; the stable download is [3.9.0](https://github.com/YuanYeYouTao/Yuki/releases/tag/v3.9.0).** Version 4.0 brings platforms and personal clients into one backend. Chat, SELF initiative, schedules, plugins and task resumption share Main. Profiles and memories belong to people, while each conversation entry keeps its own history.

## What changes in 4.0

- **More ways to reach the same Yuki.** QQ, Telegram, Feishu, Lark, the personal API and ACP share identity, memory, tools and tasks. Feishu and Lark use a custom app's long connection for direct messages, groups and threads; see the [setup guide](docs/operations/feishu.zh-CN.md). Personal devices can pair, send attachments, receive events, inspect work and add instructions to an existing task. [Yuki Desktop](https://github.com/Institute-of-Agent-Life/Yuki-Desktop) is maintained separately. The old management WebUI is retired; administration uses the CLI and `/ai admin`.
- **People and accounts are separate.** A person can own several accounts or none. Merging the same person, transferring an account, pausing it and unlinking it have distinct meanings. Renaming or reassigning an account preserves the original ownership of past messages, memories and tasks. Superuser status belongs to the person.
- **A terminal for everyday work.** Files, projects and dependencies are handled directly in the persistent terminal, with interactive input, background execution, inspection and cancellation. Direct mode and optional Code Mode share tools and execution services.
- **Tasks continue from their records.** Work retains goals, parent-child relationships, model requests, tool runs and delivery records. New messages can update ongoing work. Recovery reads the original execution, and Main Compact uses the current model to condense long conversations and task context.
- **Media on demand and background sticker selection.** Incoming images, video, stickers and files first provide an index; the agent chooses what to inspect. A shared sticker directory holds files and search information. Text delivery and background image selection have their own execution records.
- **Updated plugins and backend maintenance.** Plugin API moves to 7.0. The Kun game and GitHub monitor are maintained in separate repositories. Context preparation, person queries, diagnostics and SQLite hot queries receive a broad cleanup and fixes.

The [release notes](docs/releases/v4.0.0.en.md) cover the changes and link all 37 merged PRs since the formal 3.9.0 tag, with contributor attribution.

## Work beyond conversation

We ran two complete benchmarks locally in external task containers, using Yuki's native Main/Work execution, tools and receipts with **DeepSeek V4.1 Flash 1017**.

| Benchmark | Solved | Resolution rate | Input cache hit rate |
| --- | ---: | ---: | ---: |
| SWE-bench Lite, 300 tasks | 216 / 300 | **72.00%** | **97.47%**, complete accounting |
| Terminal-Bench 2.0, 89 tasks | 66 / 89 | **74.16%** | **98.32%**, observed usage |

SWE Lite tests issue repairs in real repositories. Terminal-Bench covers compilation, system configuration, data processing and other terminal work. SWE accumulated 463 million input tokens, of which 452 million were cache hits: repeated context was heavily reused across task turns.

The evaluation began on October 20, 2026 at fixed revision `313b6b9b`, before this 4.0.0 baseline. TB has complete accounting for 84 tasks and five marked unverified; the complete 84-task cache rate is **98.26%**. Six tasks have both a timeout and a passing verifier reward and remain in the original Harbor pass count. Settings, per-task results, tokens, timings and failure analysis are in the [report directory](docs/benchmarks/2026-10-20-yuki-flash-1017/SUMMARY.en.md).

## Everyday use

| Capability | What you can do |
| --- | --- |
| Conversation and memory | Keep talking in groups or privately, look up past events, and ask Yuki to remember, correct or delete facts |
| Files and media | Inspect images, video frames, voice transcriptions and documents, ask follow-up questions, save originals and deliver results |
| Search and webpages | Use configured native model search, an independent search connection or an external provider, and read webpages |
| Persistent workspace | Save projects, run Python, Node.js and Shell, install dependencies and start background work |
| Tasks and automation | Check progress, add instructions, wait for events, schedule work and manage parent-child tasks |
| Cross-platform communication | Find observed contacts, send text and files, mention accounts and read authorized history |

Text, images, files and stickers use the single `send` tool. A workspace path is frozen internally for delivery, without a separate model publication call. Attachment saving and sticker collection copy directly to their final files without permanent intermediary snapshots. Recovery keeps the original delivery receipts and frozen bytes, including partial or unknown outcomes.

Supported model protocols include Chat Completions, Responses, Claude Messages and Gemini GenerateContent. Profiles select model connections, reasoning settings, search and task routes.

The agent retrieves memory when needed. Extraction, SELF reflection and Dream can each use a different model. Long-term facts, short-term notes, conversation history and working files are stored separately. See [Memory scope](docs/architecture/memory-v2.md).

The persistent workspace is a separately deployed Linux environment with Bash, Python, Node.js, Git and build tools. Working files persist. Incoming media originals use a shared 24-hour cache and can be copied into the workspace or sticker directory for longer storage. See the [workspace guide](docs/operations/persistent-environment.md).

## Clients and ecosystem

| Project | Purpose |
| --- | --- |
| [Yuki Desktop](https://github.com/Institute-of-Agent-Life/Yuki-Desktop) | Cross-platform desktop client project; its public repository currently provides an introduction and license |
| [Personal API and ACP](docs/architecture/client-api.md) | Device pairing, messages, attachments, event streams, task access and external ACP clients |
| [Yuki-Semantic-Participation](https://github.com/Institute-of-Agent-Life/Yuki-Semantic-Participation) | Optional semantic participation controller |
| [Yuki-Kun-Game](https://github.com/Institute-of-Agent-Life/Yuki-Kun-Game) | Standalone Kun game plugin |
| [Yuki-GitHub-Monitor](https://github.com/Institute-of-Agent-Life/Yuki-GitHub-Monitor) | Standalone GitHub repository monitor plugin |

Character preferences live in one editable `character/SOUL.md`; the character name is maintained separately. An empty SOUL is valid, and runtime, tool and authorization rules remain in the program. Existing deployments must explicitly select their persona and initialize the name using the [SOUL upgrade procedure](docs/operations/soul-deployment-20261022.md).

## Install and upgrade

For a packaged deployment, download the [3.9.0 Release](https://github.com/YuanYeYouTao/Yuki/releases/tag/v3.9.0) and follow its [installation and upgrade guide](docs/upgrade-3.9.0.md). Its image is `ghcr.io/yuanyeyoutao/yuki-qqbot:3.9.0`, for Linux amd64 and arm64.

To work on the 4.0 source, install Python 3.12 and uv:

```bash
git clone https://github.com/YuanYeYouTao/Yuki.git
cd Yuki
uv sync --all-extras
uv run yuki --help
```

The CLI is `yuki` and the server entry point is `yuki-bot`. See the [4.0 upgrade guide](docs/upgrade-4.0.0.en.md) for migration from 3.9. QQ uses SnowLuma; Feishu, Lark, Telegram or personal-client-only setups need no QQ account.

The 4.0.0 release baseline is schema **0143**, Plugin API **7.0**, and manifest **25**. Subsequent #448 development unifies chat-history reads with schema **0145**, Plugin API **7.1**, and manifest **26**. Stop business writers while upgrading the text index, then start the Bot after conversion completes. Back up the database, configuration, plugins and working files first. See the [current history contract](docs/architecture/chat-history.md) and [implementation record](docs/operations/chat-history-448-implementation-20261022.md).

## Architecture and development

Read the [development contract](docs/architecture/development-contract.md) and [architecture index](docs/architecture/README.md). Chat, background tasks and plugins reuse Main. Native platform protocols stay in adapters, while people, conversations and tasks use internal IDs.

```bash
uv sync --all-extras
uv run ruff check .
uv run mypy --platform linux src
uv run pytest
```

| Documentation | Contents |
| --- | --- |
| [Help](docs/help.md) | Chat commands, model setup and daily administration |
| [4.0.0 release notes](docs/releases/v4.0.0.en.md) | Features, fixes, compatibility changes and the complete PR ledger |
| [Benchmark report](docs/benchmarks/2026-10-20-yuki-flash-1017/SUMMARY.en.md) | Full evaluations, usage, cache accounting and per-task data |
| [Canonical runtime](docs/architecture/canonical-runtime.md) | People, accounts, spaces and conversations |
| [Feishu and Lark setup](docs/operations/feishu.zh-CN.md) | Custom apps, message permissions, long connections and media |
| [Conversation compact](docs/architecture/conversation-compact.md) | Long conversations and task context |
| [Plugin API 7.1](docs/plugin-development/index.md) | Plugin development and SDK |
| [Persistent subagents](docs/architecture/persistent-subagents.md) | Task trees, parent-child questions and recovery |
| [CHANGELOG](CHANGELOG.md) | Earlier versions |

## License

Current source is licensed under [AGPL-3.0-only](LICENSE). Earlier versions retain their bundled license.
