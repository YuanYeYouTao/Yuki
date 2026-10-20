# Upgrading to the Yuki 4.0.0 development baseline

<!-- release-baseline: version=4.0.0 schema=0143 -->

[中文](upgrade-4.0.0.md) · [Development notes](releases/v4.0.0.en.md) · [Complete PR ledger](releases/v4.0.0-prs.md)

This guide covers official 3.9.0 and later development deployments. The 4.0.0 source baseline has database head `0143`, exact Plugin API `7.0`, Main tool contract `25` and unchanged Code API `yuki.codemode.api.v2`. The official 3.9.0 package remains at schema `0119` and API `3.5`. Install the new source using its lockfile or build it locally.

## Identify the existing deployment

| Item | Official 3.9.0 | 4.0.0 development baseline |
| --- | --- | --- |
| Python module/package | `qq_ai_bot` / old package name | `yuki`, without an old-module shim |
| Executables | `qq-ai-bot-cli` / `qq-ai-bot` | `yuki` / `yuki-bot` |
| Database | `0119` | `0143`, 23 additional revisions |
| Plugin API | `3.5` | `7.0`, exact match |
| Administration | Temporary browser UI | Management HTTP API, CLI and `/ai admin`; separate personal clients |
| Superuser identity | QQ account list | Persistent Person authority; independent operator credentials |
| Files/packages | Separate workspace and installation tools | Shared persistent terminal, with artifact inspection/publication |
| Stickers/media | Old pool and automatic expansion | File library, background stickers and shared originals read on demand |

Record the actual `DATABASE_URL`, database revision, full Compose/systemd configuration, Bot and gateways, Manager, environment mounts, plugins and approvals. A development deployment continues from its actual revision; do not infer database structure from the application version.

## 1. Keep the original database and deployment names

New installations default to Compose project `yuki`, network `yuki-network` and `data/yuki.db`. Existing deployments retain their project, network, volumes, workspace and database path. Renaming the old database is unnecessary, and the new default must not create a second empty database.

If an older native/source installation did not explicitly set `DATABASE_URL`, set its original absolute path before upgrading so migration and startup use the same database:

```dotenv
DATABASE_URL=sqlite+aiosqlite:////srv/yuki/data/qq_ai_bot.db
```

Update installed packages, imports, systemd, Manager module paths, scripts and actual Compose commands to `yuki` / `yuki-bot`. Sync the locked dependencies or rebuild the target image; moving source files is insufficient. The future image repository is `ghcr.io/yuanyeyoutao/yuki`; preparing this baseline does not publish a 4.0.0 image tag.

## 2. Back up consistently and migrate forward

Stop Bot writes and preserve the main database, the participation/Manager/artifact receipt databases actually used, configuration, plugin data, protocol objects and referenced files. For a persistent terminal, also save its HOME/runtime filesystems and complete current rootFS. Image configuration can contain an execd credential, so keep these backups private. Pause only writers involved in the migration; retain existing gateways, proxies and unrelated services.

Test the target version against an isolated copy with the same configuration, then migrate the original deployment through its normal startup path. `0119` to `0143` is one chain with 23 new revisions. `0124` directly precedes `0126`; no `0125` needs to be invented. Do not skip migrations with `stamp`, reset conversations or reset cumulative budgets.

| Revisions | Changes and preserved data |
| --- | --- |
| `0120`–`0122` | Separate private conversations by original account/platform entrance and topic; personal devices and pairing retain existing history |
| `0123`–`0124` | Preserve independent Memory operation receipts and each notification grant's original Conversation |
| `0126`–`0129` | Canonical monitor/Rebuild targets, publication provenance after temporary-media cleanup, independent personal names and same-human ownership |
| `0130`–`0131` | Retire dual context windows and automatic search extraction overrides while retaining configuration audit history |
| `0132` | Separate ACP sessions and original request Conversation scopes without rewriting old receipts |
| `0133` | Move current projection bytes to immutable append chunks, preserving order and contents |
| `0134` | Retire unused tool/turn diagnostic tables and indexes; retain real receipts |
| `0135`–`0137` | Name lookup index, Person superuser authority and management-source references; remove device-read heartbeat writes |
| `0138`–`0140` | Retire the sticker pool; persist background sticker intents and actually observed attachment snapshots |
| `0141` | Replace dual ownership tables with Account and authoritative AccountAssignment; preserve original periods, authors, conversations and executions |
| `0142` | Normalize derived names and index positional mention lookup without rewriting events or delivery receipts |
| `0143` | Backfill original event attachment references, add current query indexes and remove ineffective independent-session capability storage |

Changes including `0141` and `0143` are irreversible, and retired diagnostic/configuration data cannot always be reconstructed. Prefer a forward fix. An old image cannot simply run the upgraded database. If restoring a backup is necessary, first preserve newer messages, files and completed effects and plan their consistency separately.

## 3. Move administrative authority to Person

Before removing old `SUPERUSERS` configuration, export historical account/Person candidates using the target version's read-only script:

```sh
python scripts/prepare_person_authority.py --deployment-root /path/to/yuki --output /private/person-authority-candidates.json
```

Use `--database /absolute/path/original.db` for a nondefault database. Verify the real person and transfer history, then remove `SUPERUSERS` from `.env`, Compose/systemd and the actual process environment. Migration does not automatically grant superuser authority.

An existing independent operator uses `yuki admin person list/show` to obtain the internal Person ID and revision, then explicitly grants authority with `person superuser grant`. A person without an account can be created first. Transferring an account does not transfer authority, unlinking does not revoke it, and replaying an old grant cannot revive a revoked privilege. See the [authority migration procedure](operations/person-admin-control-plane-upgrade.md) for credentials and commands.

Chat administration uses `/ai admin <domain> <operation>`. `/ai help` and `/ai admin help` separate personal/conversation operations from administration. App/ACP message text does not execute platform administration commands. Pairing, files and secrets use their designated APIs/CLI; a personal device token is not an operator credential.

## 4. Adapt plugins to API 7.0

The Host only loads `plugin_api = "7.0"`. Product 4.0 and Plugin API 7.0 are separate versions. Update actual consumers before changing the manifest:

| API step | Required adaptation |
| --- | --- |
| [4.0 identity](plugin-development/api-4.0-migration.md) | General People/Memory/message/configuration targets use internal Person, Space and Conversation IDs rather than QQ IDs |
| [5.0 stickers](plugin-development/api-5.0-migration.md) | Remove `ctx.emoji`, old emoji permissions, Features and Hooks; use current media artifacts and messaging |
| [6.0 mentions](plugin-development/api-6.0-migration.md) | Use `CurrentMessage.mentions` and `message.current.mentions.v2` instead of old mentioned fields |
| [7.0 sessions](plugin-development/api-7.0-migration.md) | Use `plugin.agent_session.v2`; remove creation metadata/allowed_capabilities, run allowed_capabilities/max_tool_calls and result tool_calls_used |

Independent sessions retain per-message metadata, history, real `max_model_requests` and request counts. Main's `ctx.agent.run()` retains its separate tool and authorization contract.

Preserve original KV, jobs, outbox, approval and incomplete receipts. Rediscover the updated plugin, review its actual manifest and approve the capabilities it needs. Do not broaden approval to bypass an interface change or resend old notifications/paid requests. Bundled Echo and Subscription Monitor manifests/examples are prepared for API 7.0 in this baseline. [Kun](https://github.com/Institute-of-Agent-Life/Yuki-Kun-Game) and [GitHub Monitor](https://github.com/Institute-of-Agent-Life/Yuki-GitHub-Monitor) use their independently maintained compatible versions. GitHub Monitor is no longer bundled in the main deployment archive.

## 5. Coordinate the persistent environment and media mounts

Check the host's `src/yuki`, Manager unit/dependencies, current Supervisor, ACP peer and sticker helpers in addition to the Bot. Environment commands run as root with `HOME=/home/yuki`; keep existing packages, services, HOME/runtime, jobs and complete rootFS. Save a complete snapshot before explicitly changing the environment according to the [persistent environment guide](operations/persistent-environment.md). Do not replace installed software with an empty base image or start two active environments sharing one HOME.

- Configure Manager through `--acp-config` or `YUKI_MANAGER_ACP_CONFIG`. The environment needs pinned `agent-client-protocol==0.12.1` and read-only `/opt/yuki-runtime/acp_peer.py`. External adapters, login and provider credentials are operator configuration.
- Mount the same Bot cache read-only at `/inputs/media-cache`; `YUKI_MANAGER_MEDIA_CACHE` points to its actual host directory. Existing containers do not gain mounts automatically. Bot remains responsible for original downloads and 24-hour cleanup.
- Authoritative sticker images/JSON live under `/workspace/stickers/<platform>/`. Update the helpers and rebuildable search index. `0138` does not automatically import the old pool. Save any old images you intend to keep before cleaning the retired directory after checking references; keep parent data/workspace/artifact directories.
- For a gVisor custom bridge, explicitly mount a resolver file with Manager's `--resolv-conf`. Retain a reachable deployment-specific DNS configuration rather than assuming Docker's `127.0.0.11` works.

Read old terminal/APT/Python/external-agent results by their original request/run and completion. If replacing the environment interrupts execution, preserve the real interrupted/unknown state; an upgrade does not authorize replay. `yuki setup environment-check` checks the existing socket/artifact connection. Administrator execution, PTY, installation, background recovery, public networking and shared read-only media need their own verification.

## 6. Configuration and mounted prompts

| Item | Current behavior |
| --- | --- |
| Compact | Default `CONTEXT_COMPACT_TRIGGER_TOKENS=96000` schedules early compaction; hard input capacity comes from explicit Profile limits and the actual output allocation |
| Old windows | Remove `CONTEXT_WINDOW_TOKENS` and `CONTEXT_WORK_WINDOW_TOKENS`; old values are not copied into the new trigger or model capacity |
| Old Rollup | Remove retired `CONVERSATION_ROLLUP_*`, `CONTEXT_COMPACTION_WINDOW_TOKENS`, `WORK_COMPACTION_*` settings and `conversation_compaction` route; retain current Memory evidence-compaction settings |
| Web | Remove `WEB_EXTRACT_MAX_RESULTS` and `WEB_SOURCE_MAX_RUNS_PER_CONVERSATION`; read pages explicitly |
| Proxy | Use standard `HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY`, `NO_PROXY` and actual CA configuration; see [proxy operations](operations/http-proxy.zh-CN.md) |
| ASR | Explicitly select `chat_completions` or `audio_transcriptions`; the model name does not select the protocol |
| Stickers | Remove the retired `emoji_replacement` route, obsolete emoji overrides and `VISION_MAX_RETRIES`; retain other profiles, vision/ASR connections and secrets |

`SYSTEM_PROMPT_FILE` refers to a real deployment file. Back it up and update it alongside the current contract: changing source or an example does not replace a mounted file. See the [quiet-SELF prompt delivery material](operations/system-prompt-quiet-self-initiative-20261018.md). Preserve custom persona content while updating general speaking, tool and persistent-work rules. Previously paid requests remain unchanged.

## 7. Verify the upgraded deployment

Verify schema `0143`, original people/account periods, conversations, Work, cumulative budgets, protocols and delivery receipts, plus referenced media, configuration and plugin data/approvals. Separately check operator access, personal pairing and original-request lookup, QQ/TG connections, Manager/socket, installed environment files, the shared read-only cache, sticker helpers and fixed tools. App/ACP continue using original devices and sessions.

HTTP admission, Main ending, Work completion and platform delivery have separate receipts. After disconnection or lost acknowledgement, query the original operation/request/run; retain running/unknown rather than submitting another ID. Record real provider usage, cache and natural-chat behavior from their actual observations.

See the [architecture index](architecture/README.md), [personal/ACP contract](architecture/client-api.md) and [complete merged-PR ledger](releases/v4.0.0-prs.md).
