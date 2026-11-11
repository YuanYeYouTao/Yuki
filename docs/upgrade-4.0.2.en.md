# Yuki 4.0.2 upgrade guide

<!-- release-baseline: version=4.0.0 schema=0156 -->

[中文](https://github.com/YuanYeYouTao/Yuki/blob/v4.0.2/docs/upgrade-4.0.2.md) · [4.0.0 release notes](https://github.com/YuanYeYouTao/Yuki/blob/v4.0.2/docs/releases/v4.0.0.en.md) · [Every merged PR](https://github.com/YuanYeYouTao/Yuki/blob/v4.0.2/docs/releases/v4.0.0-prs.md)

This guide covers two kinds of deployment: the stable **3.9.0** release, and deployments that ran 4.0 development images after 3.9.0. For a new install you do not need it; use the installer described in the [README](https://github.com/YuanYeYouTao/Yuki/blob/v4.0.2/README.en.md#install).

Work through the steps in order. Steps marked "3.9.0 only" have usually been done already on a 4.0 development deployment; check and move on.

## What changes

| Item | 3.9.0 | 4.0.0 |
| --- | --- | --- |
| Image | `ghcr.io/yuanyeyoutao/yuki-qqbot` | `ghcr.io/yuanyeyoutao/yuki`, plus the sandbox image `ghcr.io/yuanyeyoutao/yuki-environment` |
| Python package / commands | `qq_ai_bot` / `qq-ai-bot-cli`, `qq-ai-bot` | `yuki` / `yuki`, `yuki-bot`; the old names are gone |
| Compose project / network | `qq-ai-bot` / `qq-ai-bot-network` | `yuki` / `yuki-network` |
| Database file | `data/qq_ai_bot.db` | `data/yuki.db` (fixed in the Compose file) |
| Database revision | `0119` | `0156`, one forward migration chain |
| Sandbox Manager | systemd service on the host, loop-mounted ext4, Squid | the `manager` Compose service, with data in named Docker volumes |
| Plugin API | `3.5` | `9.0`, exact match |
| Administrators | `SUPERUSERS` in `.env` (QQ numbers) | superuser status on a Person |
| Persona | system prompt, `persona.md` and other files | one `character/SOUL.md`; the character name lives in the database |
| Management UI | management WebUI | Yuki Desktop, the CLI, `/ai admin` |
| Personal client API | — | revision 10 |
| Local connector | — | 0.6.2, device protocol 4 |

## 1. Take stock

In the old deployment directory, note these facts; later steps depend on them:

```sh
cd /path/to/yuki
docker compose ls                                   # actual Compose project name
docker compose exec bot printenv DATABASE_URL       # database the Bot actually uses
docker inspect --format '{{index .Config.Labels "io.yuki.sandbox"}}/{{index .Config.Labels "io.yuki.sandbox.project"}}' yuki-environment
systemctl status yuki-sandbox --no-pager            # is the host Manager running?
```

If the last two show `persistent-v1/` and a running `yuki-sandbox`, the sandbox is run by the host Manager and you need step 7. Without a `yuki-environment` container, skip step 7.

Also check: which plugins you use and the Plugin API they declare; where the persona comes from (`SYSTEM_PROMPT`, `SYSTEM_PROMPT_FILE`, `BOT_PERSONA_FILE`, `YUKI_PERSONA_FILE`, `BOT_DISPLAY_NAME`); your own changes to `.env` and the Compose files; and, on a 4.0 development deployment, whether the directory has a `compose.yaml` or other override files.

## 2. Prepare plugins and the persona ahead of time

Both can be done before the downtime.

**Plugins.** 4.0 loads only plugins that declare `plugin_api = "9.0"`. Product version 4.0 and Plugin API 9.0 are different things; port the real calls before changing the number in the manifest. From 3.5, work through each step:

| API version | What to change |
| --- | --- |
| [4.0 identity](https://github.com/YuanYeYouTao/Yuki/blob/v4.0.2/docs/plugin-development/api-4.0-migration.md) | People, memory, message and configuration targets use internal Person, Space and Conversation IDs instead of QQ numbers |
| [5.0 stickers](https://github.com/YuanYeYouTao/Yuki/blob/v4.0.2/docs/plugin-development/api-5.0-migration.md) | Remove `ctx.emoji` and its permissions, features and hooks; media goes through the current artifact and message interfaces |
| [6.0 mentions](https://github.com/YuanYeYouTao/Yuki/blob/v4.0.2/docs/plugin-development/api-6.0-migration.md) | Use `CurrentMessage.mentions` and `message.current.mentions.v2` |
| [7.0 standalone sessions](https://github.com/YuanYeYouTao/Yuki/blob/v4.0.2/docs/plugin-development/api-7.0-migration.md) | Use `plugin.agent_session.v2` and drop the retired creation parameters and return fields |
| [8.0 automation](https://github.com/YuanYeYouTao/Yuki/blob/v4.0.2/docs/plugin-development/api-8.0-migration.md) | Define automation with the current TaskSpec and the original request identity |
| [9.0 message identity](https://github.com/YuanYeYouTao/Yuki/blob/v4.0.2/docs/plugin-development/api-9.0-migration.md) | `native_message_id` may be null; tasks, replies and history follow `event_id` |

Explicit `max_model_requests` / `max_tool_calls` passed to `ctx.agent.run()` are now the total budget for the whole task, not per-segment limits. The Kun game and GitHub monitor plugins no longer ship with the main repository; use the current versions of [Yuki-Kun-Game](https://github.com/Institute-of-Agent-Life/Yuki-Kun-Game) and [Yuki-GitHub-Monitor](https://github.com/Institute-of-Agent-Life/Yuki-GitHub-Monitor). Existing plugin KV data, jobs, outboxes and unfinished receipts are kept.

**Persona (3.9.0 only).** 4.0 reads a single `character/SOUL.md`; the character name lives in the database's character profile. Compare the old system prompt and `persona.md`, pick the persona content you actually want, and turn it into one SOUL; do not just concatenate the files. Tool, permission and task rules come from the program and do not belong in SOUL. An empty SOUL is fine. See the [single-SOUL upgrade notes](https://github.com/YuanYeYouTao/Yuki/blob/v4.0.2/docs/operations/soul-deployment-20261022.md) (Chinese).

## 3. Let tasks finish and stop the old services

Let Yuki finish what she is doing, or cancel it explicitly. The sandbox migration script refuses to continue while executions are queued or running (registered long-running services do not count).

Then, in the old deployment directory and with the **old** Compose file, stop the services:

```sh
docker compose down
```

This stops the Bot and SnowLuma. The sandbox container `yuki-environment` is not part of this Compose project and keeps running until step 7. Only one Bot may write to a database at any time.

## 4. Export the old administrator list (3.9.0 only)

In 4.0, superuser status belongs to a Person, and the database migration grants it to nobody based on `SUPERUSERS`. The migration also drops the old account-binding table, so export the candidates with the read-only script from the 4.0 image **before the database is upgraded**:

```sh
docker pull ghcr.io/yuanyeyoutao/yuki:4.0.0
docker run --rm -v "$PWD:/deploy" --entrypoint python ghcr.io/yuanyeyoutao/yuki:4.0.0 \
  /app/scripts/prepare_person_authority.py --deployment-root /deploy \
  --database /deploy/data/qq_ai_bot.db --output /deploy/person-authority-candidates.json
```

The script only reads the database and grants nothing. The list contains identity data; keep it with your deployment data. After exporting, remove `SUPERUSERS` from `.env`, the Compose files and the process environment.

## 5. Back up

With the services stopped, take a complete backup:

- The deployment directory: `data/` (the database together with its `-wal` and `-shm` files), `.env`, `config/`, `webui-config/` (including `model_profiles.secrets.json`), `character/`, `plugins/`, `workspace/`, `social-transfer/`, and the QQ login data `snowluma-data/`, `snowluma-qq-config/`, `snowluma-qq-data/`, `snowluma-extra-accounts/`.
- The host Manager's data: `/etc/yuki-sandbox/` and the sandbox root (`YUKI_SANDBOX_ROOT` in `/etc/yuki-sandbox/deployment.env`, `/var/lib/yuki-sandbox` by default).

Database migrations only go forward, some cannot be reversed, and an old image cannot run an upgraded database. If something goes wrong, prefer fixing forward. If you really must restore the backup, first save the new messages, files and effects produced after the upgrade; never overwrite them with the old database.

## 6. Put the 4.0.0 release files in place

The sandbox migration script and the new Compose file ship in the release bundle, so put them into the deployment directory first. Do not run the installer yet: it stops when it finds the host Manager's environment and asks you to do step 7 first.

```sh
cd /path/to/yuki
curl -fsSLO https://github.com/YuanYeYouTao/Yuki/releases/download/v4.0.2/yuki-4.0.2-deploy.tar.gz
curl -fsSLO https://github.com/YuanYeYouTao/Yuki/releases/download/v4.0.2/SHA256SUMS
sha256sum --ignore-missing -c SHA256SUMS
tar -xzf yuki-4.0.2-deploy.tar.gz --strip-components=1
```

This replaces the release files (`docker-compose.yml`, `.env.example`, `install.sh`, `deploy/sandbox/` and so on) and leaves `.env` and the data directories alone. From now on use only `docker-compose.yml`, and put your own changes (ports, extra mounts, environment variables) in `docker-compose.override.yml`. If a 4.0 development deployment has a `compose.yaml`, Compose prefers it: move the parts you want to keep into `docker-compose.override.yml` and move `compose.yaml` out of the way.

**Database file (3.9.0 only).** The new Compose file sets `DATABASE_URL=sqlite+aiosqlite:////app/data/yuki.db` under `environment`, which takes precedence over `.env`; changing `DATABASE_URL` in `.env` has no effect. Choose one:

- Rename it (make sure `data/yuki.db` does not exist yet):

  ```sh
  mv data/qq_ai_bot.db data/yuki.db
  for suffix in -wal -shm; do
    [ -f "data/qq_ai_bot.db$suffix" ] && mv "data/qq_ai_bot.db$suffix" "data/yuki.db$suffix"
  done
  ```

- Or keep the name and point to it in `docker-compose.override.yml`:

  ```yaml
  services:
    bot:
      environment:
        DATABASE_URL: sqlite+aiosqlite:////app/data/qq_ai_bot.db
  ```

Either way, never let the Bot create a new empty database at `data/yuki.db`. On a 4.0 development deployment, check against the actual path found in step 1.

**`.env` and persona (3.9.0 only).**

- Save the persona prepared in step 2 as `character/SOUL.md`; if you do not want one, copy the `config/SOUL.md` template from the bundle. Remove `SYSTEM_PROMPT`, `SYSTEM_PROMPT_FILE`, `BOT_PERSONA_FILE`, `YUKI_PERSONA_FILE` and `BOT_DISPLAY_NAME`; keep `BOT_ALIASES` only for wake-up aliases you still want.
- Remove retired settings. Leftover variables are ignored and do not break startup, but they mislead:

| Setting | What to do |
| --- | --- |
| `SUPERUSERS` | Remove (exported in step 4) |
| `AGENT_MAX_MODEL_REQUESTS`, `AGENT_MAX_TOOL_CALLS` | Remove; per-segment limits are gone |
| `CONTEXT_WINDOW_TOKENS`, `CONTEXT_WORK_WINDOW_TOKENS` | Remove. The early compaction threshold is `CONTEXT_COMPACT_TRIGGER_TOKENS` (default 96000); hard capacity comes from the profile's `max_input_tokens` / `context_window_tokens` |
| `CONVERSATION_ROLLUP_*`, `CONTEXT_COMPACTION_WINDOW_TOKENS`, `WORK_COMPACTION_*` | Remove; Memory evidence compaction settings stay |
| `WEB_EXTRACT_MAX_RESULTS`, `WEB_SOURCE_MAX_RUNS_PER_CONVERSATION` | Remove; webpage text is read explicitly |
| `VISION_MAX_RETRIES`, the `emoji_replacement` route | Remove |
| `SNOWLUMA_NOVNC_*` | Remove; the QQ login screen is viewed in Yuki Desktop |
| Proxies | Use the standard `HTTP_PROXY` / `HTTPS_PROXY` / `ALL_PROXY` / `NO_PROXY`; see the [proxy notes](https://github.com/YuanYeYouTao/Yuki/blob/v4.0.2/docs/operations/http-proxy.zh-CN.md) (Chinese) |
| Speech transcription | Choose the `chat_completions` or `audio_transcriptions` protocol explicitly |

## 7. Move the sandbox into Compose

On 3.9.0 and 4.0 development deployments the sandbox Manager runs on the host (systemd, loop-mounted ext4). 4.0.0 runs it in Compose, and the move happens once. On the Docker host, as root:

```sh
sudo sh deploy/sandbox/migrate-host-manager.sh --deployment-root /path/to/yuki
```

The script does the following. The first run only copies; it deletes nothing:

1. Confirms no execution is queued or running, stops the host `yuki-sandbox` service, and disables it and `yuki-environment-storage` at boot (the storage mounts stay until `--cleanup`).
2. Stops `yuki-environment` and `docker commit`s its full rootFS as `yuki-environment:migrated-<time>`. That image is the starting point for the new environment; everything you installed is in it.
3. Creates the Docker volumes, copies HOME, run records, the media cache and `jobs.sqlite3` into them, and makes the new Manager create the environment from that checkpoint image. If the old environment mounted a host `resolv.conf` (for gVisor), it is copied as it was to `.config/yuki/resolv.conf` in HOME and `YUKI_SANDBOX_RESOLV_CONF` is set.
4. Renames the old container to `yuki-environment-retired` and removes the old Squid egress container, the internal bridge and the related firewall rules.
5. Writes the old settings into the deployment's `.env`: the old browser mode becomes a profile (headed → `desktop`, headless → `headless`, otherwise `lite`), the old runtime becomes `YUKI_SANDBOX_RUNTIME`, and resources and extra packages become `YUKI_SANDBOX_*`. Values already in `.env` are not overwritten.

The old loop images, `jobs.sqlite3` and media cache directory stay where they are until you run `--cleanup`.

Notes:

- The host needs `python3` (host-Manager deployments already have it).
- If you use gVisor but Docker's `runsc` lacks `--overlay2=none`, software installed in the environment is not in the Docker layer. The script refuses and asks you to export it with `runsc tar rootfs-upper` first; see the [Mac migration record](https://github.com/YuanYeYouTao/Yuki/blob/v4.0.2/docs/operations/mac-arm64-migration-20261107.md) (Chinese).
- The checkpoint image contains the old execd credential. Keep it private, never push it to a public registry, and do not delete it: the environment is recreated from it.

Deployments without a host Manager skip this step. 4.0 keeps the media cache in the `yuki-media-cache` volume; the script copies the old `data/conversation-media-cache` there, and the old directory can be removed once confirmed.

## 8. Start 4.0.0

Run the installer in the deployment directory:

```sh
sh install.sh
```

Because `.env` exists, it skips the setup wizard. It sets `YUKI_VERSION` to `4.0.2`, asks for a profile if none is set, settles the runtime, creates any missing volumes, pulls the images, runs `docker compose up -d`, and waits until the Bot can reach the sandbox. The Bot migrates the database to `0156` when it starts. Without the installer: set `YUKI_VERSION=4.0.0` in `.env`, make sure `YUKI_SANDBOX_PROFILE` and `YUKI_SANDBOX_RUNTIME` are set, run `sh deploy/sandbox/prepare.sh`, then `docker compose up -d`.

About the database migration:

- `0119` to `0156` is one chain. `0124` is followed directly by `0126`, and `0145` by `0147`; do not invent the missing numbers. A database that already ran a 4.0 development build continues from its actual revision.
- Do not skip migrations with `alembic stamp`, and do not reset conversations or task budgets for the upgrade.
- `0153` moves existing vectors into Vec1 tables in the same database without new embedding calls.
- People, conversations, tasks, cumulative budgets and delivery receipts are all kept.

## 9. Migrate old model settings

If 3.9.0 configured models in `.env` (`LLM_*` and friends) or through the management WebUI, move them into `webui-config/model_profiles.toml` with the one-time offline command. It makes no upstream calls and costs nothing, but must not run while the Bot is running:

```sh
docker compose stop bot
docker compose run --rm --entrypoint yuki bot admin model migrate-legacy
docker compose start bot
```

Check the exit code and the `ok`, `revision` and `consumed_env_fields` fields of the output. On success, remove the variables listed in `consumed_env_fields` from `.env`; the command reports names only and never edits your files. If the result is `partial` or uncertain, check the saved revision before retrying. Details: [model configuration migration](https://github.com/YuanYeYouTao/Yuki/blob/v4.0.2/docs/upgrade-model-config-20261031.md) (Chinese).

## 10. Administrators, character name and plugins

**Operator credential.** Management commands need an operator credential. An old deployment without an operator configuration can get one by running the setup wizard again in the deployment directory; it backs up the previous configuration:

```sh
docker run --rm -it --user "$(id -u):$(id -g)" -v "$PWD:/deploy" -w /deploy --entrypoint yuki \
  ghcr.io/yuanyeyoutao/yuki:4.0.0 setup --deployment-root /deploy
```

**Grant superuser status.** Check each candidate from step 4 against the real person, then grant one at a time:

```sh
docker compose exec bot sh -c 'YUKI_CONTROL_CREDENTIAL="$YUKI_CONTROL_OPERATOR_TOKEN" yuki admin person list'
docker compose exec bot sh -c 'YUKI_CONTROL_CREDENTIAL="$YUKI_CONTROL_OPERATOR_TOKEN" yuki admin person show <PersonID>'
docker compose exec bot sh -c 'YUKI_CONTROL_CREDENTIAL="$YUKI_CONTROL_OPERATOR_TOKEN" yuki admin person superuser grant <PersonID> --revision <current revision>'
```

Someone without an account can be created first with `admin person create --display-name "Name"`. Transferring an account does not carry superuser status, unlinking does not revoke it, and ordinary pairing never grants it. Then pair Yuki Desktop with `admin client pair --person <PersonID>`; superusers can administer Yuki with their own Desktop credentials. See [Person authorization](https://github.com/YuanYeYouTao/Yuki/blob/v4.0.2/docs/operations/person-admin-control-plane-upgrade.md) (Chinese). Old character-owner records are kept but no longer grant anything.

**Character name (3.9.0 only).** Initialize it once while the database has no character profile; an existing character is never renamed this way:

```sh
docker compose run --rm --entrypoint yuki bot init-db --character-name "Yuki"
```

**Plugins.** With the ported plugins from step 2 in place, rediscover them with `yuki plugin list` and related commands (also with the operator credential), review the actual manifests and approve the permissions they need. Do not widen approvals to get around interface changes, and do not resend old notifications or paid model requests.

**Management.** The management WebUI is gone. Use Yuki Desktop, the `yuki admin` CLI and `/ai admin <area> <action>` in chat; `/ai help` and `/ai admin help` list the ordinary and administrative actions.

## 11. Clients, QQ and the connector

- **Yuki Desktop:** use the release that matches 4.0.0 (personal client API revision 10). Remote access needs an HTTPS address for the service; see [Desktop deployment and remote access](https://github.com/YuanYeYouTao/Yuki/blob/v4.0.2/docs/operations/desktop-local-deployment.zh-CN.md) or the [Tailscale guide](https://github.com/YuanYeYouTao/Yuki/blob/v4.0.2/docs/operations/tailscale-connection.zh-CN.md) (Chinese).
- **QQ login screen:** no longer published on port 6081. Open the computer window in a Yuki Desktop paired to an administrator and choose QQ login to view it and scan the code; the SnowLuma management page is still at `127.0.0.1:5099`. QQ login data directories are unchanged. See [SnowLuma](https://github.com/YuanYeYouTao/Yuki/blob/v4.0.2/docs/deployment/snowluma.md).
- **Local connector:** a Desktop-managed connector updates with Desktop. A standalone install follows the [local connector guide](https://github.com/YuanYeYouTao/Yuki/blob/v4.0.2/docs/operations/local-agent-connector.zh-CN.md) (Chinese) to 0.6.2, keeping its device, pairing and journal. The device protocol is still 4: older connectors still run commands and handle files, but not screen or browser actions.

## 12. Verify

```sh
docker compose ps
docker compose exec bot yuki setup verify
docker compose exec -T --user 10001:10001 bot yuki setup environment-check
```

Then confirm, one by one:

- The database is at `0156`, and the original people, account assignments, conversations, tasks and delivery records are there.
- Sandbox: the terminal shows the old files and installed software, registered services run as configured, and the environment reaches the public internet.
- Superuser and ordinary permissions behave as expected, and Desktop and paired devices connect.
- The chat platforms you use send and receive, and plugins run under their new approvals.

A health check only says the corresponding service is up; it does not prove that model calls, chat platforms or plugins work.

## 13. Clean up

Once everything checks out, remove what the host Manager left behind:

```sh
sudo sh deploy/sandbox/migrate-host-manager.sh --deployment-root /path/to/yuki --cleanup
```

This removes the `yuki-environment-retired` container, the host systemd units, the loop volumes and the sandbox root (`/var/lib/yuki-sandbox` or your configured path), `/opt/yuki-sandbox` and `/etc/yuki-sandbox`. It refuses to run until the new Manager has created its environment. Delete the old media cache directory (by default `data/conversation-media-cache`) yourself after confirming the `yuki-media-cache` volume works.

You can also remove 3.9.0's `qq-ai-bot-network` network, the old `ghcr.io/yuanyeyoutao/yuki-qqbot` image and the downloaded `yuki-4.0.2-deploy.tar.gz`. Do not delete the `yuki-environment:migrated-<time>` checkpoint image or the `yuki-sandbox-*` and `yuki-media-cache` volumes; `docker compose down -v` does not remove them.

## Reference

- [Persistent environment](https://github.com/YuanYeYouTao/Yuki/blob/v4.0.2/docs/operations/persistent-environment.md): profiles, resources, networking and migration details
- [4.0.0 release notes](https://github.com/YuanYeYouTao/Yuki/blob/v4.0.2/docs/releases/v4.0.0.en.md)
- [Architecture index](https://github.com/YuanYeYouTao/Yuki/blob/v4.0.2/docs/architecture/README.md)
