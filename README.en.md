<!-- release-baseline: version=4.0.0 schema=0156 -->

[中文](README.md) · English

<div align="center">

<p><img src="img/Yuki_2.png" alt="Yuki" width="280"></p>

<h1>Yuki</h1>

<p>A social AI agent with a lasting presence in real conversations</p>

<p>
  <a href="https://github.com/YuanYeYouTao/Yuki/releases/tag/v4.0.0"><img src="https://img.shields.io/badge/Stable-4.0.0-blue" alt="Yuki 4.0.0 stable"></a>
  <img src="https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white" alt="Python 3.12">
  <a href="https://github.com/YuanYeYouTao/Yuki/actions/workflows/quality.yml"><img src="https://github.com/YuanYeYouTao/Yuki/actions/workflows/quality.yml/badge.svg" alt="Quality"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-AGPL--3.0--only-blue" alt="AGPL-3.0-only License"></a>
</p>

[4.0.0 release notes](docs/releases/v4.0.0.en.md) · [Upgrade to 4.0](docs/upgrade-4.0.0.en.md) · [Benchmark report](docs/benchmarks/2026-10-20-yuki-flash-1017/SUMMARY.en.md) · [Desktop client](https://github.com/Institute-of-Agent-Life/Yuki-Desktop) · [Help](docs/help.md)

</div>

Yuki is an open-source, self-hosted AI agent. She takes part in conversations on QQ, Telegram, Feishu, Lark and personal clients, and remembers people, their preferences and what you have been through together. She also has a Linux computer of her own where she writes code, runs programs, uses a browser and does scheduled work. With your permission, she can use your own computer too.

People, memories and tasks are stored in your own deployment. Switch models, accounts or chat platforms and Yuki is still the same Yuki.

## What she can do

| Capability | In practice |
| --- | --- |
| Long-term conversation and memory | Keep talking in groups and private chats and look up past events; ask her to remember, correct or forget something. One person's accounts on different platforms belong to one person |
| Files and media | Read images, video, voice messages and documents and answer follow-up questions; send text, images, files and stickers |
| Search and webpages | Use the model's built-in search or a separately configured search connection, and read webpages |
| Her own computer | A persistent Linux environment: terminal, Python, Node.js, Git, package installs and background services. Depending on the install profile it also has a browser, or a desktop you can watch and take over |
| Your computer | Through [Yuki Desktop](https://github.com/Institute-of-Agent-Life/Yuki-Desktop) and the local connector, once you switch it on: run commands, read and write files, take screenshots and operate windows, and use a dedicated Chrome. She can also drive Codex, Claude Code or another ACP agent on your computer |
| Moving files | Copy files between chat attachments, her environment and your computer |
| Long and scheduled tasks | Tasks continue after a restart; you can add instructions midway, check progress, split work into subtasks, and run work on a schedule or on events |
| Plugins | Extend her through [Plugin API 9.0](docs/plugin-development/index.md) |

Models can use the Chat Completions, Responses, Claude Messages or Gemini protocols. Chat, memory processing, vision, speech transcription and search can each use a different model.

## Work beyond conversation

We ran two complete benchmarks locally in external task containers, using Yuki's native Main/Work execution, tools and receipts with **DeepSeek V4.1 Flash 1017**.

| Benchmark | Solved | Resolution rate | Input cache hit rate |
| --- | ---: | ---: | ---: |
| SWE-bench Lite, 300 tasks | 216 / 300 | **72.00%** | **97.47%**, complete accounting |
| Terminal-Bench 2.0, 89 tasks | 66 / 89 | **74.16%** | **98.32%**, observed usage |

SWE Lite tests issue repairs in real repositories. Terminal-Bench covers compilation, system configuration, data processing and other terminal work. SWE accumulated 463 million input tokens, of which 452 million were cache hits: repeated context was heavily reused across task turns.

The evaluation began on October 20, 2026 at fixed revision `313b6b9b`, before the 4.0.0 release. TB has complete accounting for 84 tasks and five marked unverified; the complete 84-task cache rate is **98.26%**. Six tasks have both a timeout and a passing verifier reward and remain in the original Harbor pass count. Settings, per-task results, tokens, timings and failure analysis are in the [report directory](docs/benchmarks/2026-10-20-yuki-flash-1017/SUMMARY.en.md).

## Install

The host needs Docker with the Compose plugin and nothing else. Docker must run Linux containers on amd64 or arm64.

```sh
curl -fsSLO https://github.com/YuanYeYouTao/Yuki/releases/download/v4.0.0/install.sh
sh install.sh
```

On Windows, in PowerShell:

```powershell
Invoke-WebRequest https://github.com/YuanYeYouTao/Yuki/releases/download/v4.0.0/install.ps1 -OutFile install.ps1
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

The installer uses `yuki/` under the current directory unless you pass `--dir`. It downloads and verifies the release bundle, runs a setup wizard for the model connection, search, whether to enable QQ and other settings, asks for a sandbox profile, creates the data volumes, starts everything, and finishes only once Yuki's environment is ready.

| Sandbox profile | Contents | Memory, roughly |
| --- | --- | --- |
| `lite` | Terminal and files | 512 MiB |
| `headless` | Adds a headless browser | 1 GiB |
| `desktop` | Adds a desktop you can watch and take over | 3 GiB |

The installer suggests a profile from the memory Docker reports; you can pick another. If Docker has gVisor registered (`runsc` with `--overlay2=none`), the sandbox runs under gVisor; otherwise it uses the default `runc`. That choice is made on the first install, written to `.env`, and never changed silently. To switch profiles later, edit `YUKI_SANDBOX_PROFILE` in `.env` and run `docker compose up -d`. Moving to a larger profile installs the extra software into the existing environment; files and earlier installs stay.

Unattended install (main model only, no QQ):

```sh
sh install.sh --yes --profile lite --answers model.json
```

`model.json` looks like `{"protocol": "...", "base_url": "...", "api_key": "...", "model": "..."}`, where `protocol` is `chat_completions`, `responses`, `anthropic_messages` or `gemini`. Without `--yes`, the installer must run in an interactive terminal.

After installing:

- **Name the character** (once): `docker compose run --rm --entrypoint yuki bot init-db --character-name "Yuki"`. The persona lives in `character/SOUL.md` and can be edited at any time.
- **Become the administrator and connect Desktop**: the setup wizard ends by printing the commands that create your person, grant superuser status and pair a device; see [Person authorization](docs/operations/person-admin-control-plane-upgrade.md) (Chinese). Using Desktop remotely needs an HTTPS address for the service; see [Desktop deployment and remote access](docs/operations/desktop-local-deployment.zh-CN.md) or [Tailscale private access](docs/operations/tailscale-connection.zh-CN.md) (Chinese).
- **Connect chat platforms**: QQ goes through [SnowLuma](docs/deployment/snowluma.md); scan the login code in Desktop's computer window. For Telegram, set `TELEGRAM_BOT_TOKEN` in `.env`. Feishu and Lark are covered in the [setup guide](docs/operations/feishu.zh-CN.md) (Chinese). Telegram, Feishu, Lark or personal clients alone need no QQ account.
- **Connect your own computer** from Yuki Desktop under Settings → This Computer; see the [local connector guide](docs/operations/local-agent-connector.zh-CN.md) (Chinese).

## Upgrade

Run the same installer again in the installation directory. It replaces the release files, keeps `.env`, `data/`, `docker-compose.override.yml` and all data volumes, and the database migrates when the Bot starts. Back up first. Coming from 3.9.0 there is more to do: the database file name, the persona file, administrator status, plugins, and moving the sandbox Manager from the host into Compose. Follow the [4.0 upgrade guide](docs/upgrade-4.0.0.en.md) step by step.

## Documentation

| Document | Contents |
| --- | --- |
| [Help](docs/help.md) | Chat commands, model setup and daily administration |
| [4.0.0 release notes](docs/releases/v4.0.0.en.md) | Everything that changed in this release |
| [4.0 upgrade guide](docs/upgrade-4.0.0.en.md) | Upgrading from 3.9.0 or a 4.0 development deployment |
| [Persistent environment](docs/operations/persistent-environment.md) | Sandbox profiles, resources, networking and migration |
| [Desktop computer and headed environment](docs/operations/desktop-computer.zh-CN.md) | Screenshots, clicks and the browser on Yuki's own computer (Chinese) |
| [Local connector](docs/operations/local-agent-connector.zh-CN.md) | Connecting your computer, full access, screen and browser, native coding agents (Chinese) |
| [Feishu and Lark setup](docs/operations/feishu.zh-CN.md) | Custom apps, permissions, long connections and media (Chinese) |
| [SnowLuma (QQ)](docs/deployment/snowluma.md) | QQ gateway deployment and login |
| [Plugin API 9.0](docs/plugin-development/index.md) | Plugin development and SDK |
| [Architecture index](docs/architecture/README.md) | Entry point to the current architecture documents |
| [CHANGELOG](CHANGELOG.md) | Earlier versions |

## Clients and ecosystem

| Project | Purpose |
| --- | --- |
| [Yuki Desktop](https://github.com/Institute-of-Agent-Life/Yuki-Desktop) | Desktop client for Windows, macOS and Linux: chat, tasks, files, memory, computers, models and usage; it can also deploy Yuki locally. Released separately |
| [Personal API and ACP](docs/architecture/client-api.md) | Device pairing, messages, attachments, event streams, task access and continuation, external ACP clients |
| [Yuki-Semantic-Participation](https://github.com/Institute-of-Agent-Life/Yuki-Semantic-Participation) | Optional semantic participation controller |
| [Yuki-Kun-Game](https://github.com/Institute-of-Agent-Life/Yuki-Kun-Game) | Standalone Kun game plugin |
| [Yuki-GitHub-Monitor](https://github.com/Institute-of-Agent-Life/Yuki-GitHub-Monitor) | Standalone GitHub repository monitor plugin |

## Development

You need Python 3.12, [uv](https://docs.astral.sh/uv/) and a C compiler, because the SQLite vector extension is built with the package (GCC/Clang on Linux, Xcode Command Line Tools on macOS, an MSVC developer terminal on Windows). The release images need no compiler.

```bash
git clone https://github.com/YuanYeYouTao/Yuki.git
cd Yuki
uv sync --all-extras --managed-python
uv run ruff check .
uv run mypy --platform linux src
uv run pytest tests/unit -q -m "not native_worker"
```

The management CLI is `yuki` and the server entry point is `yuki-bot`. Before changing the architecture, runtime, tools or persistence, read the [development contract](docs/architecture/development-contract.md) and the [architecture index](docs/architecture/README.md).

## License

Current source is licensed under [AGPL-3.0-only](LICENSE). Earlier versions retain their bundled license.
