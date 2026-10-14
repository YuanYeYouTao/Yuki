<!-- release-baseline: version=3.9.0 schema=0118 -->

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
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-AGPL--3.0--only-blue" alt="AGPL-3.0-only License"></a>
</p>

[Download 3.8.4](https://github.com/YuanYeYouTao/Yuki/releases/tag/v3.8.4) · [3.8.4 release notes](docs/releases/v3.8.4.md) · [3.8.4 upgrade guide](docs/upgrade-3.8.4.md) · [Help](docs/help.md)

</div>

Yuki is an open-source, self-hosted social AI agent exploring what a persistent digital life can be in real conversations. She currently runs in QQ private chats and groups, remembers people and shared experiences, uses tools and a persistent workspace to carry work across messages. Her identity and memory live in Yuki's own database and can survive a change of model, QQ account, or gateway.

**The current release is 3.8.4.** In this release, the main agent sends visible messages explicitly through `send_message`, and optional group semantic observation, SELF initiative, and SELF automation are available. Yuki can choose to speak, split a reply, or remain silent. Autonomous participation in 3.8.4 is disabled by default, and long-term behavior in real QQ groups is still being evaluated. The workspace is deployed separately; the official 3.8.4 bundle does not include the management WebUI.

**The current development baseline is 3.9.0 and is not released.** See the [draft 3.9.0 release notes](docs/releases/v3.9.0.md) for every merged PR and the [draft upgrade guide](docs/upgrade-3.9.0.md) for preparation. The 3.8.4 Release above remains the official download.

## Main changes in 3.9.0

- **Direct build by default:** The same main-agent loop exposes the fixed complete tool declaration. The default image contains no Monty binding, worker, or launcher. Code Mode is an optional tool-calling mode using a separately built `codemode` target and explicit opt-in; both modes share tool implementations and the execution backend.
- **Management WebUI and a shared execution runtime:** The journal-style interface shows actual conversations, execution traces, tool receipts, model usage, and workspace files, and manages model connections and task routes. Chat, SELF, plugins, automation, and Work resumption share the main agent. WebUI is disabled by default.
- **Group participation controls:** Automatic replies, proactive wakeups, and autonomous participation default to enabled in current source and can be controlled separately through configuration. Ordinary message bursts are combined over a configurable wait before starting a new reply; explicit calls and original Work inputs bypass it. Disabling new opportunities does not cancel accepted tasks or schedules. The optional Jev semantic controller remains disabled by default.
- **Recoverable long tasks:** Work retains its goals, parent/child relationships, cumulative budget, original checkpoints, and actual receipts. Context is condensed against actual request capacity. The agent can finish its goal without every historical failure or unknown result vetoing completion. Waiting and explicit continuation preserve the original task; continuing a child does not reopen a finished ancestor, and confirmed effects are not repeated.
- **Simpler memory policy:** Independent facts can share a key; changes and deletion use fact IDs. Attribution/reinforcement scoring, automatic promotion, capacity-driven eviction, and the old affection/trust scores are removed. Extraction, SELF, and Dream can use separate model profiles, with partial and unresolved results retained. Automatic extraction accepts new factual assertions.
- **Agent-directed retrieval:** The agent uses `search_memory` when past facts are needed instead of injecting them every turn. Current, quoted, historical, workspace, and authorized tool images enter the original main model's native multimodal input.
- **Shared public history and current material:** A Conversation keeps one history of original events rather than rebuilding it for each speaker, entry point, or personal preference. Program-provided material has explicit source and end markers before the current message. It is used only for that activation and stays out of public history, automatic snapshots, and summaries; task notes remain private to their original Work.
- **Forwarded messages on demand:** The agent can read the first layer of forwarded nodes through the original conversation event, preserving order and partial failures. Ingestion does not automatically expand forwards, and nested forwards are not recursively fetched.
- **Multiple protocols and web access:** Chat Completions, Responses, Claude Messages, and Gemini GenerateContent use explicit task routes. New installations enable model search by default while respecting explicit disablement and actual connection capabilities. New profiles default to medium reasoning; existing explicit settings are preserved.
- **Actual failures and receipts:** Model errors, failed tools, and unknown effects retain their original call records. Late results can still be reconciled against the original execution. Paid truncated output remains an incomplete result; Yuki does not automatically buy another model request to explain an error or repair plain-text truncation, or insert a fixed error message into QQ.
- **Less database waiting before replies:** History reads, context preparation, and optional diagnostic writes are moved out of critical write transactions. Background maintenance uses indexes, bounded pages, and short transactions. Actual latency still depends on model responses, tool requests, and host resources.
- **Retired capabilities and interfaces:** Legacy MCP connections, discovery, management pages, SDK capabilities, and automation entry points are removed together. Model-side administration tools and unused auxiliary action logs are also retired; shared tool results, media, and original receipts remain. Plugin API is now **3.5**, removing unimplemented event and permission declarations. The current development database head is **0118**.
- **Speech output retirement:** Genie synthesis, voice profiles/preferences, tool parameters, SDK/management features, the Worker and release dependencies are removed. Incoming/quoted ASR, historical audio and original receipts remain. Reconcile old executions and verify a cold backup of speech facts and referenced files before migrating the dedicated tables; no automatic resend or conversion to text occurs.

These describe the current source. Real provider API behavior, natural-chat latency, and long-task outcomes require their respective acceptance evidence. Optional Code Mode isolation checks do not establish production capacity or long-term memory behavior.

**Future plan: The current WebUI in 3.9.0 is temporary; the next major release will completely redesign it around multiple clients.**

[Conversation compact](docs/architecture/conversation-compact.md) ([#417](https://github.com/YuanYeYouTao/Yuki/issues/417), [#418](https://github.com/YuanYeYouTao/Yuki/issues/418)) uses the current Main model to condense public history or private execution context when the complete request exceeds its actual capacity. Subagents share Main's tool contract and execute under their own real source and authorization. The independent Rollup and Work/ordinary summary pipelines are retired.

## Current source capabilities

| Capability | How it is used |
| --- | --- |
| Conversation and long-term memory | Chat in groups or privately, look up past events, and explicitly ask Yuki to remember, correct, or delete facts |
| Images, voice, and attachments | Receive images, voice, video, or documents, ask follow-up questions without quoting the attachment, and read forwarded messages on demand |
| QQ social actions | Look up members, use structured mentions and pokes, send group/private messages, and recall Yuki's own messages; the backend checks targets and permissions |
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

Long-term memory stores stable facts, preferences, and meaningful experiences. Routine extraction aggregates messages; explicit requests to remember, correct, or delete are handled immediately. The main agent decides when to call `search_memory` instead of automatically searching every turn or injecting all long-term facts. Retrieval respects the current actor's visible scope and work budget, and reports truncation or incomplete results. Keys are labels rather than unique slots; facts retain their IDs and evidence. Extraction, SELF reflection, and Dream can each select a model connection. Main compact checkpoints retain long conversation context, while raw history remains independently searchable.

`short_state` is a shared, bounded, expiring area for temporary cross-conversation notes. Writes need only slot/text; empty text clears the slot. Each activation shows only current effective content, without retaining an initial snapshot or subsequent change deltas. It is distinct from long-term memory and workspace files. Group and private chats are not automatically combined into one complete history.

Memory reads have a specific privacy boundary: a past shared-group relationship can expose structured facts about a person, **including facts sourced from private chat**, but not another person's raw private messages or private evidence. Read [Memory scope and permissions](docs/architecture/memory-v2.md) before deployment. Model extraction and recall can be wrong; verify important claims against their sources.

## Images, voice, files, and web access

- **Images and video:** Current, quoted, and historical attachments, along with agent-selected workspace and authorized tool images, share the original main model's native image input. Historical reads check internal event IDs, conversation ownership, and cache expiry. A model without image input reports the material as unread; Yuki does not silently switch models or request a separate Qwen visual summary. MP4/MOV video is sampled into frames by FFmpeg for the same main agent. Audio tracks are not analyzed, and sampling may miss moments. Background sticker classification and explicit plugin vision capabilities can still use a separate vision connection.
- **Incoming voice:** Private messages, group messages that meet the reply policy, and quoted voice can be transcribed with Qwen ASR and included in chat history, search, and public compact. Recognized text and sources are retained in attachment order, including completed transcriptions when another attachment fails. Qwen connectivity can be reused; speech synthesis and sending are retired. See [speech recognition](docs/speech/recognition.md).
- **Files:** Bounded extraction supports text, code, CSV/JSON, PDF text, DOCX, and XLSX. Scanned PDFs do not receive OCR; spreadsheet formulas are not recalculated; reading does not execute macros or embedded code. Within the same conversation, later questions can read the original attachment's 24-hour temporary cache without quoting it.
- **Web:** New installations without an explicit web switch default to `WEB_MODE=native`. Explicit `disabled` remains disabled, and existing connection search choices are preserved. Enabling web access does not grant every model search capability: native search requires a truthful capability declaration, Gemini can use an explicitly selected search bridge, and external `web_search` requires a configured backend. External search defaults to Tavily. Set `WEB_MODE=tavily` and `WEB_SEARCH_BACKEND=deepseek_anthropic` to use the [DeepSeek search bridge](docs/deepseek-search-bridge.md), with an optional Tavily key for failure fallback. The DeepSeek main agent currently does not declare native search. See the [provider contract](docs/architecture/model-providers.md) for protocol and configuration boundaries.

Chat, plugin wakeups, automation, and task resumption use the same main agent with its complete tool declarations. The tool schema stays fixed within a deployment; permissions and budgets are checked at execution time. This reduces request-prefix variation but does not guarantee provider cache hits.

Historical Gemini / DeepSeek samples, accounting coverage, and experiment limits are recorded in the [3.9.0 cache measurements](docs/releases/v3.9.0.md#缓存与用量). They do not establish current production cache rates, faster replies, or lower costs in the new version. Missing cache fields remain unknown.

## Configuration and startup

Basic deployment requires Linux amd64 or arm64 (including Linux VMs on Apple Silicon), or Docker Desktop running Linux containers, Docker Engine with Compose v2, and a configured model service using one of the supported Chat Completions or Responses integrations. Current development source uses a logged-in SnowLuma QQ gateway; NapCat is retired on this branch, while the existing gateway abstraction remains available for other implementations.

The development branch supports Chat Completions, Responses, Claude Messages, Gemini GenerateContent, and common vendor parameter dialects. Profiles explicitly select models, reasoning, structured output, and search; message content does not secretly select a provider. After WebUI saves a connection, new tasks use it, while dispatched requests retain their original connection and protocol state.
See the [protocol contract](docs/architecture/model-providers.md) and [multi-provider example](config/model_profiles.providers.example.toml) for capability and recovery boundaries. These describe current source; use the official 3.8.4 bundle's instructions for its gateway, model, and deployment configuration.

Download the deployment bundle from the [3.8.4 Release](https://github.com/YuanYeYouTao/Yuki/releases/tag/v3.8.4). After extraction, fill in `.env` and model configuration manually or use the guided setup.

Multi-platform support belongs to current development source and future releases: Bot and SnowLuma select their image for the Docker host, and both installers recognize amd64/x86_64 and arm64/aarch64. This source change does not update previously published artifacts. ARM deployments need a release containing this fix, or a source build with `docker buildx build --platform linux/arm64 --target direct --load -t yuki:local .` and the Bot service's `image` set to `yuki:local` in the deployment directory.

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

Current 3.9.0 source stores model connections in `webui-config/model_profiles.toml`; the official 3.8.4 bundle uses its bundled paths. When upgrading an older deployment with only `config/model_profiles.toml`, follow the [path migration guide](docs/operations/model-profile-path-migration.md) first. The wizard does not overwrite current WebUI connections; a missing selected file causes an explicit startup failure.

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

The 3.9.0 source uses Plugin API **3.5** and current development database head **0118**; the official 3.8.4 package has head **0072**. Follow the migrations bundled with the actual target image. An application version does not replace a schema check, and `stamp` must not skip migrations. Remove legacy MCP, retired event/permission, and relationship-service dependencies, follow the [API 3.5 migration](docs/plugin-development/api-3.5-migration.md), and renew plugin approval. The Host matches the API version and manifest exactly.

Before deploying a 3.9.0 development commit, use the [draft upgrade guide](docs/upgrade-3.9.0.md) to check old MCP mounts, environment settings, and operator grants. Retired operator capabilities fail strict validation. After committing the new database head, an image-only rollback is insufficient; an old backup must not overwrite new messages or receipts.

Before upgrading, make a consistent backup of the database, configuration, plugins, and files. For a persistent workspace, retain its home directory and execution receipts. Pause writes from Bot and related Manager components; you do not need to shut down all of Docker or the QQ gateway. Preserve any new messages, files, and receipts before rollback; use the [3.9.0 draft upgrade guide](docs/upgrade-3.9.0.md) for current source.

```bash
docker compose ps
docker compose logs --tail 200 bot
docker compose exec bot qq-ai-bot-cli gateway doctor --provider snowluma
```

Keep the same Compose arguments used for deployment. Only one active connection can use a given QQ account; a duplicate connection does not displace the existing one. The installer does not replace an existing Compose file or `.env`; see [SnowLuma deployment](docs/deployment/snowluma.md) and the [draft upgrade instructions](docs/upgrade-3.9.0.md#旧-napcat-部署配置) for retiring old configuration.

## Related projects

[Alice](https://github.com/LlmKira/Alice) explores how an AI can participate continuously in real conversations; [Letta](https://docs.letta.com/) focuses on agents that retain memory and state; and [AstrBot](https://docs.astrbot.app/) provides an agent and plugin framework for QQ and other chat platforms. Yuki explores how these capabilities work together in one persistent group-chat subject: getting to know people, building shared experiences and memories, and deciding when to participate.

## Architecture and development

Read the [shared development contract](docs/architecture/development-contract.md) and [architecture index](docs/architecture/README.md) before development. Historical task specifications do not replace the current contract.

One database represents one long-lived Yuki. People, groups, QQ accounts, and gateway connections are modeled separately, so conversation history and memories are not tied to a single login. The backend checks tool permissions, budgets, idempotency, and audit records.

Yuki provides QQ interaction, a CLI, and a shared Control Plane. This development branch adds a hand-journal WebUI and same-origin admin HTTP, disabled by default. The published 3.8.4 assets remain unchanged. Build instructions, authorization, and remaining scope are documented in [WebUI](docs/architecture/webui-console.md).

```bash
uv sync --extra dev
uv run ruff format --check
uv run ruff check
uv run mypy --platform linux src
uv run pytest
```

Use checks matching the change. Quality runs source checks, frontend tests/build, and retained regressions. Release validates identity, one direct image, and its source-free deployment bundle; retired Memory quality gates and parallel release paths are removed.
Routine regressions use fake providers and isolated databases. Paid Gemini and DeepSeek cache comparisons run separately, with cold rounds, warm rounds, and missing accounting reported explicitly; they are not ordinary tests or fixed cache-hit thresholds.

| Document | Topic |
| --- | --- |
| [Help](docs/help.md) | Chat, commands, and everyday use |
| [Architecture](docs/architecture/canonical-runtime.md) | People, spaces, accounts, and conversations |
| [Development contract](docs/architecture/development-contract.md) | Event IDs, boundaries, fixed tools, resumption, and transactions |
| [Conversation compact](docs/architecture/conversation-compact.md) | Long-conversation condensation |
| [Memory](docs/architecture/memory-v2.md) | Extraction, retrieval, and permissions |
| [Plugin API 3.5](docs/plugin-development/index.md) | Plugin development and capability boundaries |
| [Work, compact, and subagent design](docs/architecture/Yuki-Work摘要退役与会话Compact及任务树设计审查报告-2026-10-14.md) | Historical design for summary retirement, compact, and capability inheritance |
| [Tool results](docs/architecture/tool-results.md) | Result budgets, media, and durable receipts |
| [Versioned releases](docs/operations/versioned-docker-release.md) | Images, bundles, and the release process |
| [CHANGELOG](CHANGELOG.md) | Historical changes |

## License

Current source is licensed under [AGPL-3.0-only](LICENSE) (GNU Affero General Public License, version 3 only). Historical releases use the LICENSE distributed with that version.
