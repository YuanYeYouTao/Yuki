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

Pick one:

| I want to… | How |
| --- | --- |
| Use it on my own computer — the simplest way | Install Docker, then "在本機部署 Yuki" (Deploy Yuki locally) in [Yuki Desktop](https://github.com/Institute-of-Agent-Life/Yuki-Desktop) |
| Run it on a server around the clock, with QQ, Feishu or Telegram | Run the installer on the server, then connect Yuki Desktop with the binding code it prints |

Not comfortable with the command line? [Let an AI assistant install it](#let-an-ai-assistant-install-it).

### On your own computer

1. Install Docker: on macOS [Docker Desktop](https://docs.docker.com/desktop/), [OrbStack](https://orbstack.dev/) or [Colima](https://github.com/abiosoft/colima); on Windows Docker Desktop (WSL 2); on Linux Docker Engine.
2. Download and open [Yuki Desktop](https://github.com/Institute-of-Agent-Life/Yuki-Desktop) and choose "在本機部署 Yuki".
3. Choose a data directory, enter Yuki's name, your name and a model service (or pick one from the presets), choose the size of Yuki's computer, and start.

When it finishes, Yuki Desktop connects by itself and you can start talking.

### On a server

The server needs Docker with the Compose plugin only (Linux containers, amd64 or arm64).

```sh
curl -fsSLO https://github.com/YuanYeYouTao/Yuki/releases/download/v4.0.0/install.sh
sh install.sh
```

On Windows, in PowerShell:

```powershell
Invoke-WebRequest https://github.com/YuanYeYouTao/Yuki/releases/download/v4.0.0/install.ps1 -OutFile install.ps1
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

The installer asks for Yuki's name, your name, the model connection (and whether to use QQ and so on) and the size of Yuki's computer. It starts everything, waits until the environment is ready, and ends with:

```text
Connect Yuki Desktop:
  1. Open Yuki Desktop and choose "連接已有 Yuki".
  2. Address: http://127.0.0.1:8080 on this computer. ...
  3. Binding code: XXXXXXXX  (valid for 10 minutes; a new one: sh deploy/pair.sh)
```

In Yuki Desktop choose "連接已有 Yuki" (Connect an existing Yuki) and enter the address and code; you become this Yuki's administrator. The code is valid for 10 minutes; run `sh deploy/pair.sh` in the deployment directory for a new one.

Yuki listens on the server's own `127.0.0.1:8080` only, never on the public internet. To reach a server from your computer, [Tailscale](docs/operations/tailscale-connection.zh-CN.md) is the easiest; with a domain you can also set up [HTTPS](docs/operations/desktop-local-deployment.zh-CN.md).

### How big Yuki's computer is

| Size | What it has | Memory, roughly |
| --- | --- | --- |
| `lite` | Terminal and files | 512 MiB |
| `headless` | Plus a headless browser | 1 GiB |
| `desktop` | Plus a desktop you can watch and take over | 3 GiB |

The installer recommends a size from the machine's memory; you can choose another. To change it later, edit `YUKI_SANDBOX_PROFILE` in the deployment's `.env` and run `docker compose up -d`; a larger size installs what it needs into the existing environment and keeps all files. If Docker has gVisor (`runsc` with `--overlay2=none`) the environment uses it, otherwise the default `runc`.

Unattended install (for scripts, or an AI assistant running it for you):

```sh
sh install.sh --yes --profile lite --owner-name "Your name" --answers model.json
```

`model.json` holds the model connection: `{"protocol": "chat_completions", "base_url": "...", "api_key": "...", "model": "..."}`, where `protocol` is `chat_completions`, `responses`, `anthropic_messages` or `gemini`. Delete the file afterwards.

### Let an AI assistant install it

Copy the [installation prompt](docs/install-with-ai.en.md) into an AI assistant that can use a terminal (such as Claude Code or Codex). It checks the machine, helps you install Docker, runs the installer and verifies the result. Type secrets into the terminal yourself; never paste them into the chat.

### After installing

- **Personality**: edit `character/SOUL.md` in the deployment directory.
- **Chat platforms**: QQ uses [SnowLuma](docs/deployment/snowluma.md) — scan the login code in Yuki Desktop's computer window; for Telegram set `TELEGRAM_BOT_TOKEN` in `.env`; for Feishu and Lark see [the guide](docs/operations/feishu.zh-CN.md).
- **Your own computer**: connect it in Yuki Desktop under "設定 → 這台電腦", see the [local connector](docs/operations/local-agent-connector.zh-CN.md).

## Upgrade

Run the same installer again in the installation directory. It replaces the release files, keeps `.env`, `data/`, `docker-compose.override.yml` and all data volumes, and the database migrates when the Bot starts. Back up first. Coming from 3.9.0 there is more to do: the database file name, the persona file, administrator status, plugins, and moving the sandbox Manager from the host into Compose. Follow the [4.0 upgrade guide](docs/upgrade-4.0.0.en.md) step by step.

## Documentation

| Document | Contents |
| --- | --- |
| [Help](docs/help.md) | Chat commands, model setup and daily administration |
| [4.0.0 release notes](docs/releases/v4.0.0.en.md) | Everything that changed in this release |
| [Install with an AI assistant](docs/install-with-ai.en.md) | A prompt to paste into an AI assistant that can use a terminal |
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
