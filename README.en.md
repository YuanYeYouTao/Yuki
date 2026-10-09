<!-- release-baseline: version=3.9.0 schema=0102 -->

[简体中文](README.md) · English

<div align="center">

<p><img src="img/Yuki_2.png" alt="Yuki" width="280"></p>

<h1>Yuki</h1>

<p>A persistent social AI agent in real QQ conversations</p>

<p>
  <a href="https://github.com/YuanYeYouTao/Yuki/releases/tag/v3.8.4"><img src="https://img.shields.io/badge/Release-3.8.4-blue" alt="Yuki 3.8.4"></a>
  <img src="https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white" alt="Python 3.12">
  <img src="https://img.shields.io/badge/Deploy-Docker%20Compose-2496ED?logo=docker&logoColor=white" alt="Docker Compose">
  <a href="https://github.com/YuanYeYouTao/Yuki/actions/workflows/quality.yml"><img src="https://github.com/YuanYeYouTao/Yuki/actions/workflows/quality.yml/badge.svg" alt="Quality"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-yellow" alt="MIT License"></a>
</p>

[Download 3.8.4](https://github.com/YuanYeYouTao/Yuki/releases/tag/v3.8.4) · [3.8.4 release notes](docs/releases/v3.8.4.md) · [3.8.4 upgrade guide](docs/upgrade-3.8.4.md) · [Help](docs/help.md)

</div>

Yuki is an open-source, self-hosted social AI agent exploring what a persistent digital life can be in real conversations. She currently runs in QQ private chats and groups, remembers people and shared experiences, maintains long-term relationships, and uses tools and a persistent workspace to carry work across messages. Her identity, memory, and relationships live in Yuki's own database and can survive a change of model, QQ account, or gateway.

**The current release is 3.8.4.** In this release, the main agent sends visible messages explicitly through `send_message`, and optional group semantic observation, SELF initiative, and SELF automation are available. Yuki can choose to speak, split a reply, or remain silent. Autonomous participation is disabled by default, and long-term behavior in real QQ groups is still being evaluated. The workspace is deployed separately; the official 3.8.4 bundle does not include the management WebUI.

**The current development baseline is 3.9.0 and is not released.** See the [draft 3.9.0 release notes](docs/releases/v3.9.0.md) for every merged PR and the [draft upgrade guide](docs/upgrade-3.9.0.md) for preparation. The 3.8.4 Release above remains the official download.

## Main changes in 3.9.0

- **Direct build by default:** The same main-agent loop exposes the full authorized tool set without a Monty binding, worker, or launcher in the image. Code Mode requires an explicitly selected image and opt-in; validate resource headroom on the target host before enabling it.
- **Management WebUI and a shared execution runtime:** The journal-style interface shows actual conversations, execution traces, tool receipts, model usage, and workspace files, and manages model connections and task routes. Chat, SELF, plugins, automation, and Work resumption share the main agent. WebUI is disabled by default.
- **Recoverable long tasks:** Work retains its original goal, cumulative budget, protocol checkpoints, and delivery receipts. Context is condensed against actual request capacity, and research material is read on demand. Terminal waits, subtasks, and additional user requirements continue the original task; restarts do not repeat confirmed effects.
- **Agent-directed retrieval:** The agent uses `search_memory` when past facts are needed instead of injecting them every turn. Current, quoted, historical, workspace, and authorized tool images enter the original main model's native multimodal input.
- **Web access and model error feedback:** New installations enable model search by default while respecting explicit disablement and connection capabilities. An explicit Gemini tool-format rejection can be returned to the model for up to two corrections in the same execution chain when safe recovery conditions hold, without resetting budgets or blindly resending messages.
- **Less database waiting before replies:** History reads, context preparation, and optional diagnostic writes are moved out of critical write transactions. Background maintenance uses indexes, bounded pages, and short transactions. Actual latency still depends on model responses, tool requests, and host resources.
- **Legacy MCP removal:** Connections, tool discovery, management pages, SDK capabilities, and automation entry points are retired together. Shared tool results, media, and receipts remain. Plugin API is now **3.3**, the database head is **0099**, and old plugins require adaptation and renewed approval.
- **Speech output retirement:** Genie synthesis, voice profiles/preferences, tool parameters, SDK/management features, the Worker and release dependencies are removed. Incoming/quoted ASR, historical audio and original receipts remain. Reconcile old executions and verify a cold backup of speech facts and referenced files before migrating the dedicated tables; no automatic resend or conversion to text occurs.

These describe the current source. Real provider API behavior, natural-chat latency, and long-task outcomes require their respective acceptance evidence. Optional Code Mode isolation checks do not establish production capacity or long-term memory behavior.

## Current source capabilities

| Capability | How it is used |
| --- | --- |
| Conversation and long-term memory | Chat in groups or privately, look up past events, and explicitly ask Yuki to remember, correct, or delete facts |
| Images, voice, and attachments | Receive images, voice, video, or documents and ask follow-up questions in the same conversation without quoting the attachment |
| QQ social actions | Look up members, use structured mentions, send group/private messages, and recall Yuki's own messages; the backend checks targets and permissions |
| Search and plugins | Use configured online tools and approved plugins |
| Persistent workspace | Save projects and files, run Python, Node.js, or Shell, install dependencies, and deliver results |
| Background work and automation | Start work while continuing the conversation, check progress later, and run scheduled work within granted permissions |
| Stickers and speech recognition | Sticker search, classification, and sending; incoming audio recognition remains |

Availability depends on deployment configuration, model capabilities, and authorization. Acceptance, execution, and message delivery are tracked separately; calling a tool does not mean its result was delivered.

## Persistent workspace

When enabled, Yuki gets a shared Linux working directory across conversations. Downloads, Git repositories, scripts, and dependencies persist; ordinary workspace files do not expire after 24 hours.

- Bash, Python, Node.js, Git, and basic build tools are preinstalled. The workspace supports pip and npm, plus Manager-controlled apt installation and environment checkpoints.
- Terminals support interactive input, incremental output, cancellation, and background execution. Yuki can start a task, continue chatting, and return to its result.
- Processes can continue while Bot or Manager restarts. A workspace restart interrupts ordinary tasks; registered services recover according to policy.
- Selected files are delivered to QQ as immutable snapshots. Legacy `artifact_id` references remain compatible.

The default home-directory quota is 2 GiB and container memory limit is 512 MiB, with at most one primary execution task, four terminal sessions, and two internal services. The workspace has no browser or desktop and does not mount the Bot database, QQ credentials, or host Docker control interface.

This is an **optional, separately deployed capability** requiring a Linux host, gVisor, and Yuki Manager. The standard Bot deployment bundle does not install it. See the [workspace operations guide](docs/operations/persistent-environment.md) ([Chinese](docs/operations/persistent-environment.zh-CN.md)) for setup, limits, and recovery.

## Memory and continuity

Long-term memory stores stable facts, preferences, and meaningful experiences. Routine extraction aggregates messages; explicit requests to remember, correct, or delete are handled immediately. The main agent decides when to call `search_memory` instead of automatically searching every turn or injecting all long-term facts. Retrieval respects the current actor's visible scope and work budget, and reports truncation or incomplete results. Rollup condenses long conversation history, while raw history remains independently searchable.

`short_state` is a shared, bounded, expiring area for temporary cross-conversation notes. It is distinct from long-term memory and workspace files. Group and private chats are not automatically combined into one complete history.

Memory reads have a specific privacy boundary: a past shared-group relationship can expose structured facts about a person, **including facts sourced from private chat**, but not another person's raw private messages or private evidence. Read [Memory scope and permissions](docs/architecture/memory-v2.md) before deployment. Model extraction and recall can be wrong; verify important claims against their sources.

## Images, voice, files, and web access

- **Images and video:** Current, quoted, and historical attachments, along with agent-selected workspace and authorized tool images, share the original main model's native image input. Historical reads check internal event IDs, conversation ownership, and cache expiry. A model without image input reports the material as unread; Yuki does not silently switch models or request a separate Qwen visual summary. MP4/MOV video is sampled into frames by FFmpeg for the same main agent. Audio tracks are not analyzed, and sampling may miss moments. Background sticker classification and explicit plugin vision capabilities can still use a separate vision connection.
- **Incoming voice:** Private messages, group messages that meet the reply policy, and quoted voice can be transcribed with Qwen ASR and included in chat history, search, and Rollup. Qwen connectivity can be reused; speech synthesis and sending are retired. See [speech recognition](docs/speech/recognition.md).
- **Files:** Bounded extraction supports text, code, CSV/JSON, PDF text, DOCX, and XLSX. Scanned PDFs do not receive OCR; spreadsheet formulas are not recalculated; reading does not execute macros or embedded code. Within the same conversation, later questions can read the original attachment's 24-hour temporary cache without quoting it.
- **Web:** New installations without an explicit web switch default to `WEB_MODE=native`. Explicit `disabled` remains disabled, and existing connection search choices are preserved. Enabling web access does not grant every model search capability: native search requires a truthful capability declaration, Gemini can use an explicitly selected search bridge, and external `web_search` requires a configured backend. External search defaults to Tavily. Set `WEB_MODE=tavily` and `WEB_SEARCH_BACKEND=deepseek_anthropic` to use the [DeepSeek search bridge](docs/deepseek-search-bridge.md), with an optional Tavily key for failure fallback. The DeepSeek main agent currently does not declare native search. See the [provider contract](docs/architecture/model-providers.md) for protocol and configuration boundaries.

Chat, plugin wakeups, automation, and task resumption use the same main agent with its complete tool declarations. The tool schema stays fixed within a deployment; permissions and budgets are checked at execution time. This reduces request-prefix variation but does not guarantee provider cache hits.

In historical samples, three requests within one natural Gemini group-chat reply had an input-weighted cached-token ratio of **90.55%**. A manual DeepSeek Responses cumulative-chat experiment measured **95.11%** across nine requests, or **99.02%** for its eight warm continuations. Dates, scenarios, and measurement coverage differ; these are neither current production rates nor before/after latency comparisons. See the [3.9.0 cache measurements](docs/releases/v3.9.0.md#缓存与用量) for complete samples and multi-scenario results.

## Configuration and startup

Basic deployment requires Linux amd64 or Windows Docker Desktop running Linux containers, Docker Engine with Compose v2, a configured model service using one of the supported Chat Completions or Responses integrations, and a logged-in NapCat or SnowLuma QQ gateway.

The development branch adds native Claude Messages, Gemini GenerateContent, and common Chat vendor dialects.
Providers can be explicitly assigned to tasks; see the [protocol contract](docs/architecture/model-providers.md)
and [multi-provider example](config/model_profiles.providers.example.toml). These additions do not describe the existing 3.8.4 deployment archive.

Download the deployment bundle from the [3.8.4 Release](https://github.com/YuanYeYouTao/Yuki/releases/tag/v3.8.4). After extraction, fill in `.env` and model configuration manually or use the guided setup.

Linux:

```bash
curl -fLO https://github.com/YuanYeYouTao/Yuki/releases/download/v3.8.4/install.sh
chmod +x install.sh
./install.sh
```

Windows PowerShell:

```powershell
Invoke-WebRequest -Uri https://github.com/YuanYeYouTao/Yuki/releases/download/v3.8.4/install.ps1 -OutFile install.ps1
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

**The wizard configures only.** In an empty directory it downloads and verifies the deployment bundle; for an existing deployment it preserves Compose, plugins, and data, then backs up and writes configuration after confirmation. It does not stop services, migrate the database, start services, or switch gateways.

Model connections are stored in `webui-config/model_profiles.toml`. If an older deployment only has `config/model_profiles.toml`, follow the [path migration guide](docs/operations/model-profile-path-migration.md) first. The wizard does not overwrite current WebUI connections; a missing selected file causes an explicit startup failure.

After configuring a fresh deployment, run from its deployment directory:

```bash
docker compose config --quiet
docker compose pull
docker compose run --rm --no-deps --entrypoint qq-ai-bot-cli bot init-db
docker compose up -d
```

Complete QQ login and any plugin setup you selected. For a fresh deployment or an upgrade from an older release, see the [3.8.4 upgrade guide](docs/upgrade-3.8.4.md). Existing deployments must retain their project name, Compose overrides, and mounted configuration.

The Bot image is `ghcr.io/yuanyeyoutao/yuki-qqbot:3.8.4`; historical 3.8.4 TTS Worker assets belong to that older release and are not required by current source. The release includes `SHA256SUMS`. The standalone environment-template asset is named `default.env.example`, while the archive contains `.env.example`.

## Upgrading and maintenance

The 3.9.0 source uses Plugin API **3.3** and database head **0099**; the official 3.8.4 package has head **0072**. Follow the migrations bundled with the actual target image. An application version does not replace a schema check, and `stamp` must not skip migrations. Plugins must remove MCP dependencies, adapt to API 3.3, and receive renewed approval. Legacy `llm.generate` / `agent.run` calls now use the unified main entry point.

Before deploying a 3.9.0 development commit, use the [draft upgrade guide](docs/upgrade-3.9.0.md) to check old MCP mounts, environment settings, and operator grants. Retired operator capabilities fail strict validation. After committing the new database head, an image-only rollback is insufficient; an old backup must not overwrite new messages or receipts.

Before upgrading, make a consistent backup of the database, configuration, plugins, and files. For a persistent workspace, retain its home directory and execution receipts. Pause writes from Bot and related Manager components; you do not need to shut down all of Docker or the QQ gateway. Preserve any new messages, files, and receipts before rollback; see the [3.8.4 upgrade guide](docs/upgrade-3.8.4.md).

```bash
docker compose ps
docker compose logs --tail 200 bot
docker compose exec bot qq-ai-bot-cli gateway doctor --provider snowluma
```

Use `napcat` instead of `snowluma` in the last command when appropriate, and keep the same Compose arguments used for deployment. Only one active connection should use a given QQ account; stop the old connection before switching gateways. See [SnowLuma deployment and switching](docs/deployment/snowluma.md).

## Related projects

[Alice](https://github.com/LlmKira/Alice) explores how an AI can participate continuously in real conversations; [Letta](https://docs.letta.com/) focuses on agents that retain memory and state; and [AstrBot](https://docs.astrbot.app/) provides an agent and plugin framework for QQ and other chat platforms. Yuki explores how these capabilities work together in one persistent group-chat subject: getting to know people, building relationships and memories, and deciding when to participate.

## Architecture and development

Read the [shared development contract](docs/architecture/development-contract.md) and [architecture index](docs/architecture/README.md) before development. Historical task specifications do not replace the current contract.

One database represents one long-lived Yuki. People, groups, QQ accounts, and gateway connections are modeled separately, so conversation history and relationships are not tied to a single login. The backend checks tool permissions, budgets, idempotency, and audit records.

Yuki provides QQ interaction, a CLI, and a shared Control Plane. This development branch adds a hand-journal WebUI and same-origin admin HTTP, disabled by default. The published 3.8.4 assets remain unchanged. Build instructions, authorization, and remaining scope are documented in [WebUI](docs/architecture/webui-console.md).

```bash
uv sync --extra dev
uv run ruff format --check
uv run ruff check
uv run mypy src
uv run pytest
```

Use targeted checks during development; the release pipeline also verifies migrations, images, and source-free deployment.
Routine regressions use fake providers and isolated databases. Paid Gemini and DeepSeek cache comparisons run separately, with cold rounds, warm rounds, and missing accounting reported explicitly; they are not ordinary tests or fixed cache-hit thresholds.

| Document | Topic |
| --- | --- |
| [Help](docs/help.md) | Chat, commands, and everyday use |
| [Architecture](docs/architecture/canonical-runtime.md) | People, spaces, accounts, and conversations |
| [Development contract](docs/architecture/development-contract.md) | Event IDs, boundaries, fixed tools, resumption, and transactions |
| [Rollup](docs/architecture/conversation-rollup.md) | Long-conversation condensation |
| [Memory](docs/architecture/memory-v2.md) | Extraction, retrieval, and permissions |
| [Plugin API 3.3](docs/plugin-development/index.md) | Plugin development and capability boundaries |
| [Tool results](docs/architecture/tool-results.md) | Result budgets, media, and durable receipts |
| [Versioned releases](docs/operations/versioned-docker-release.md) | Images, bundles, and the release process |
| [CHANGELOG](CHANGELOG.md) | Historical changes |

## License

[MIT](LICENSE)
