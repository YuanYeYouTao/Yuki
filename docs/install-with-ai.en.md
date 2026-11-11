# Let an AI assistant install Yuki

[中文](install-with-ai.md) · [README](../README.en.md#install)

Copy the whole block below into an AI assistant that can run commands on your computer (such as Claude Code or Codex), after filling in the three choices on its second line. It checks, installs and verifies step by step, and stops whenever something needs you (typing a secret, granting administrator rights).

```text
Please install Yuki 4.0.1 (https://github.com/YuanYeYouTao/Yuki) for me.
My situation: install it on [this computer / a Linux server I'm logged into over SSH]; my model service is [e.g. DeepSeek, an OpenAI-compatible service, or Ollama on this computer]; Yuki's computer should be [lite: terminal only / headless: plus a browser / desktop: plus a desktop — recommend from memory if unsure].

Work in this order. Before each step tell me what you will run and why, then run it. If something fails, show me the relevant log and stop; do not work around it on your own:

1. Check the machine: OS and architecture, free memory and disk, whether `docker info` works with Linux containers, and whether `docker compose version` works.
2. If Docker is missing: tell me how to install it for my system (macOS: Docker Desktop, OrbStack or Colima; Windows: Docker Desktop with WSL 2; Linux: Docker Engine). I do any step that needs an administrator password or a graphical installer; wait until I'm done.
3. Choose an installation directory (default: yuki/ under the current directory) and download the installer:
   curl -fsSLO https://github.com/YuanYeYouTao/Yuki/releases/download/v4.0.1/install.sh
   Then run `sh install.sh --dir <directory>` interactively in the terminal. It asks for Yuki's name, my name, the model connection and the size:
   - I type the API key into the terminal myself. Never ask me to paste it into the chat, and never put it in command arguments or shell history.
   - If it must run unattended, create a model.json only I can read (chmod 600), let me fill in the key, run
     `sh install.sh --dir <directory> --yes --profile <size> --owner-name "<my name>" --answers model.json`, and delete model.json right after.
   - For a model service on this computer (such as Ollama) use http://host.docker.internal:<port>/v1, not 127.0.0.1.
4. The installer ends with an address and a binding code (valid for 10 minutes). Give them to me so I can choose "連接已有 Yuki" (Connect an existing Yuki) in Yuki Desktop. If it expires, run `sh deploy/pair.sh` in the installation directory for a new one.
5. Verify and report:
   - `docker compose ps` shows bot and manager running;
   - `docker compose exec --user 10001:10001 bot yuki setup environment-check` prints "ok": true;
   - `curl -fsS http://127.0.0.1:8080/livez` succeeds.
6. On a server: Yuki listens on the server's 127.0.0.1:8080 only. Help me connect it to my computer safely with Tailscale following docs/operations/tailscale-connection.zh-CN.md. Do not open port 8080 to the internet and do not turn off the firewall.

Notes:
- Never delete Docker volumes whose names start with yuki-, or data/, character/ and workspace/ in the installation directory.
- If port 8080 is taken, set YUKI_PORT=<another port> in .env and run `docker compose up -d`.
- If the sandbox does not come up, read `docker compose logs manager`. Behind a fake-ip proxy such as Clash/mihomo, names inside the environment may resolve to 198.18.x.x and fail; see the DNS notes in docs/operations/persistent-environment.md.
- If this Docker already runs an older Yuki (3.9 or earlier), do not install over it; follow docs/upgrade-4.0.1.en.md instead.
```

## Afterwards

- Personality: edit `character/SOUL.md` in the installation directory.
- QQ, Telegram, Feishu: see the [README](../README.en.md#after-installing).
- Upgrading later: run the installer again in the same directory.
