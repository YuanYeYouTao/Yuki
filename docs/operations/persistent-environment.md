# Persistent Yuki environment

[中文](persistent-environment.zh-CN.md)

Yuki shares one Linux environment, `/home/yuki` and `/workspace` across people and groups. Ordinary persistent files do not expire. `short_state` remains a separate expiring hint area. Directories, files and full logs are not automatically added to context. The Bot's `./workspace:/app/workspace` stores artifacts/short state; terminal files live under the host's `/var/lib/yuki-sandbox/home/workspace`, exposed as `/workspace` through the original home mount. Explicit publication and attachment import connect these stores.

## Runtime and tools

Temporary media from every conversation shares `/inputs/media-cache`, a read-only view of the same Bot-owned cache, not another copy in the 2 GiB home. Bot remains its sole writer and expiry/cleanup owner. Main may read it from the terminal or copy selected originals into `/workspace` or a sticker folder for unrestricted editing. Existing `workspace_inspect` and `workspace_publish` also accept `/inputs/media-cache/...` through the existing safe FD reader. The user explicitly authorized this shared view; terminal reads are global rather than limited to the current Conversation. Persistent files, collected stickers and immutable artifacts do not expire with temporary media.

The trusted host Manager owns the persistent gVisor container `yuki-environment`, labeled `io.yuki.sandbox=persistent-v1`. OpenSandbox execd is pinned to `441042c288a3eacbe93b36236a9baf6f6254ad4d`; the image retains its Apache license. Terminals and services share execd PTY and the original Supervisor. There is no second package executor or sudo approval system.

Commands run as root **inside the environment**, with `HOME=/home/yuki`. Bash, Python, Node.js, Git, curl, jq, rg, archive and compilation tools are preinstalled. Shell commands, apt, pip/npm and ordinary environment file operations use the terminal directly. The pip user base, npm prefix and caches retain the original home. Environment root does not expose host Docker control, Bot databases or credentials.

| Tool | Behavior |
| --- | --- |
| `terminal_exec` | Starts one original execution and returns its `run_id` after about five seconds; `tty=true` supplies an interactive Bash retaining directory, variables and functions |
| `terminal_read` | Reads current progress, exit status, byte-cursor output and original terminal `completion/artifact`; optional `wait_seconds` defaults to zero and only waits for observation |
| `terminal_control` | `input` sends raw text, `interrupt` sends SIGINT, and `cancel/close` stops the original task; files already written remain |
| `external_agent` | Lists operator-configured ACP agents, submits or reads one original prompt, answers its permission, cancels that prompt or explicitly closes its peer |

There is no default execution deadline. Explicit `timeout_seconds=0` also disables the timer; a positive value sets the requested deadline without a software maximum. Canceling a read cancels observation only. Empty output is not completion, and a nonzero exit does not undo earlier file writes. Cursor reads identify lost output after bounded log rotation.

New Python, Node and system-package work uses the terminal. `run_python`, `environment_packages`, `get_code_run`, `cancel_code_run` and `terminal_write` no longer accept new calls. Original Python/APT runs remain readable by `terminal_read` with their original completion acknowledgements. An upgrade never dispatches an old unstarted APT intent or replays an installation with an unknown outcome. New package budgets, dpkg collection and automatic Docker checkpoint production are retired; existing images and original receipts remain.

`environment_service` retains register/start/stop/delete/status/logs and real recovery across environment starts, using the original `restart` policy. Stop disables recovery first. Service and consecutive-failure counts are not capped. Updating a registration preserves its current original run until a real restart.

Ordinary file read/write/search/list/edit/delete operations use the terminal. The eight ordinary `workspace_*` CRUD tools, public artifact listing/text paging/delete entries and ordinary overwrite CAS are removed without new-call aliases. `workspace_inspect` still passes selected media to the model; `workspace_publish` creates immutable artifacts for the unified send tool. Conversation attachment import, private tool artifacts and actual media bridges remain. The terminal does not read Bot temporary media caches or private indexes. Later file edits do not change a published version.

The artifact manifest uses separate SQLite. File preparation, SHA256 and fsync happen outside the writer; short transactions verify actual revision/immutability and commit metadata. Reads safely open the original FD before releasing SQLite and verifying content. Publication errors discard only private pending files. Renamed blobs survive uncertain commits until the original GC checks actual references. Ordinary publication has no heuristic 200 MiB, 512 MiB or object-count gate. Actual model-media and platform-transfer limits still apply.

### ACP peers in the original environment

Manager `--acp-config /etc/yuki-sandbox/acp-agents.toml` (or `YUKI_MANAGER_ACP_CONFIG` in its existing systemd environment) selects programs from the [operator example](../../deploy/sandbox/acp-agents.example.toml). The fixed `external_agent` tool accepts `list|prompt|get|respond|cancel|close` and original handles; it does not accept executable, arguments, environment or a working directory. Listing reads configuration without starting an agent and returns IDs/descriptions, not credentials. A configured program is not proof that its adapter or login works.

The standard-library peer launcher runs inside the same Linux environment and lazily loads `agent-client-protocol==0.12.1`. It owns one long-lived Agent connection and session. Each prompt has a separate original Manager job and Bot execution receipt; completing a prompt or Work does not close the peer or discard its cache. Prompt results, text/tool/file updates and permission callbacks use the original runtime mount, with durable inbox/accepted/results files and byte-cursor events. JSON-RPC stdout is separate from diagnostic stderr. The Manager does not parse terminal output as protocol. Queries page only the original prompt's updates and retain a separate canonical completion; permission delivery ACKs do not prevent rereading the original result.

Bot/Manager reconnect observes these same files and process, without another `initialize`, `session/new` or `session/prompt`. A real peer death after accepted dispatch leaves the original prompt uncertain unless its original result exists. Native cancellation targets only the original prompt; `terminal_read` and `terminal_control` understand such prompt jobs and do not kill a reusable peer. Explicit close records closing intent in original Manager jobs before returning admission, including when close arrives before peer-open. Purge waits for actual peer exit, removes that peer's protocol/spec/prompt bodies and retains minimal original IDs/hash/terminal facts. Other peers, shared HOME, operator login files and published artifacts remain. This does not claim deletion of vendor-side history or every file an external program wrote in HOME.

`permission_mode="allow"` uses the existing authorized administrator execution. Explicit `"ask"` holds the original SDK question until an answer or cancellation for that same peer/prompt/question, routed through the original Work input. External text is tool evidence; delivery remains Main/Social. The Bot counts its own delegation and model calls, not the agent's unobservable internal rounds. Missing vendor usage/cache remains unknown.

## Recovery, resources and network

The Manager persists the original request, launch intent and execd session identity before dispatch. After disconnect it checks the native exit record, otherwise reattaches to the original session. The durable `started` marker prevents another execution. A stopped PTY, handshake timeout or missing legacy APT heartbeat does not prove failure from age. Uncertain submission is queried by its original request, never blindly resubmitted. The Bot persists completion before acknowledging the outbox. Lost acknowledgements or repeated reads do not launch commands or reset budgets.

Container ID and start time form the generation. Bot/Manager restarts retain the original container and processes. Real environment recreation records interrupted ordinary jobs while registered services follow their policy. Docker restart policy is `no`; the Manager starts the container after storage mounts. Updating the Manager does not silently delete or recreate the environment.

Default deployment resources remain a 2 GiB home ext4 filesystem, separate 128 MiB runtime ext4 filesystem, Docker 512 MiB memory, 128 MiB additional swap, one CPU, 128 processes and bounded `/tmp`. These are deployment resources, not admission scores. There are no one-execution, four-PTY, two-service, 256 MiB available-memory, 1 GiB package-growth, four-queue or 512-unacknowledged-completion gates. Disk-full, process and OOM outcomes reflect actual OS/Docker facts. Each task retains two 4 MiB log segments. Completed logs and acknowledged runtime files older than seven days are reclaimed independently of retained SQLite execution/completion receipts.

New environments use the existing public Docker bridge `yuki-sandbox-egress`. Host firewall rules block environment-initiated access to the host, private, link-local and other non-public IPv4 destinations. Normal public TCP/UDP, including SSH, is available without public-port allowlists. execd publishes no host port; only replies to host-initiated control connections are admitted. Docker sockets, Bot configuration/databases, QQ credentials and private artifact indexes are not mounted. This bridge disables IPv6. HTTP proxies are not forced; an explicit operator `--proxy` configures one. The old internal Squid bridge may remain during transition, but new public access does not depend on it.

On a Docker user-defined bridge, gVisor cannot reach Docker's loopback DNS at `127.0.0.11`; `--dns` alone does not bypass it. Pass Manager `--resolv-conf /var/lib/yuki-sandbox/home/.config/resolv.conf` to mount that ordinary file at `/etc/resolv.conf`. Populate it with deployment-specific reachable DNS servers before creating the environment; there is no built-in resolver or automatic fallback. The file remains writable from environment root at `/home/yuki/.config/resolv.conf` and survives through the persistent home. This option applies when creating a container; changing it does not replace an existing environment. See the [gVisor DNS guidance](https://gvisor.dev/docs/user_guide/faq/#dns-doesnt-work-on-a-docker-user-defined-bridge-network-including-docker-compose).

Main entries, automation and subagents use fixed capability contracts. Directory queries do not modify submitted definitions. Environment root does not extend person/send/delegation/plugin-domain authorization. Optional Code Mode's Monty remains a filesystem/network-free orchestration runtime, not a terminal alias.

## Installation, upgrade and rollback

The configuration installer does not install the host Manager. The Linux Docker host needs Python 3.12, verified runsc, the environment image, bridge/firewall and mounted home/runtime filesystems. Docker Desktop or a healthy Bot does not prove these dependencies are connected.

```sh
sudo /opt/yuki-sandbox/deploy/sandbox/install-manager.sh --deployment-root /absolute/deployment/path
docker compose exec --user 10001:10001 bot yuki setup environment-check
```

The deployment root is the Compose directory mounting `./workspace`. The installer records only `YUKI_MANAGER_WORKSPACE_STORE` in `/etc/yuki-sandbox/deployment.env`, preserves other settings and refuses to replace an already configured artifact root. Without an argument it retains the existing value or legacy default. It does not move home or alter existing mounts. `environment-check` uses actual Bot UID 10001 to check artifact-directory write access and the existing socket/status/readiness. It no longer calls retired `workspace_list`, creates no container, executes no code, calls no model and sends no messages. This proves connectivity, not live administrator/public-network acceptance.

Update the host's `src/yuki/sandbox/`, its `workspace/` dependencies and `deploy/sandbox/`. Install the current requirements into the original Manager venv:

```sh
sudo /opt/yuki-sandbox/venv/bin/python -m pip install -r /opt/yuki-sandbox/deploy/sandbox/requirements-manager.txt
```

Set the actual host directory corresponding to Bot's `CONVERSATION_MEDIA_CACHE_DIRECTORY` in the existing `/etc/yuki-sandbox/deployment.env`, preserving all other fields:

```ini
YUKI_MANAGER_MEDIA_CACHE=/absolute/deployment/path/data/conversation-media-cache
```

Alternatively supply Manager's explicit `--media-cache` argument. There is no inferred repository path or generic mount configuration. New environments mount this directory read-only at `/home/yuki/inputs/media-cache`, exposed through the existing `/inputs` link. Do not share the whole Bot data directory, databases or configuration. Manager status reports `media_cache.configured/shared`; a missing mount in an old environment does not disable other tools or trigger automatic recreation.

Adding this view to an existing Docker container requires one explicit replacement through the complete private rootFS checkpoint procedure below. Retain the same home/runtime, installations and services. Verify an actual terminal can read cached originals, writes fail with EROFS, and copies in `/workspace` remain editable; status and future image declarations alone do not prove that the mount is live.

The standalone sticker helper and its format-reading dependencies run inside the original persistent environment. Main and Manager invoke the same file; the Host never connects directly to the user-editable search cache. Updating only the Bot image or a future environment Dockerfile does not update an existing rootFS. Copy the current helper into the original container at `/opt/yuki-runtime/`, verify it from the existing terminal, and preserve installed packages, files, mounts and services. For an existing checkpoint deployment, stop Manager after active executions have finished, privately snapshot the original full rootFS with the installed helper, and update its existing `checkpoint_image` through the offline `set_setting` port. Otherwise a later rebuild from the old checkpoint loses the helper. Installing the helper alone does not require recreation; enabling the new read-only media mount requires one explicit replacement. Retain the original home/runtime binds and protect both the live image and selected checkpoint during cleanup. Retain systemd and all overlays. Supervisor and `acp_peer.py` keep their original read-only source mounts; the ACP SDK remains in environment Python.

Sticker originals and per-asset JSON are the sole source; `search.sqlite3` is disposable. Main can run:

```sh
python /opt/yuki-runtime/sticker_index.py --workspace /workspace rebuild --platform qq
python /opt/yuki-runtime/sticker_index.py --workspace /workspace update stickers/qq/index/<asset-id>.json
python /opt/yuki-runtime/sticker_index.py --workspace /workspace search --platform qq 'laughing'
```

Replace the example asset ID with the real filename. Omit `--platform` on rebuild to process existing platform folders. Ordinary file operations need no helper. Newly edited tags become searchable after update/rebuild finishes; parent directory mtime is not a synchronization signal. The helper needs no Bot database or operator credentials and calls no model or social transport. An unfinished cold cache is not an empty library.

New environment images install ACP SDK 0.12.1. Existing rootFS snapshots retain their installed packages and need an explicit in-place SDK installation:

```sh
sudo sh /opt/yuki-sandbox/deploy/sandbox/install-acp-sdk.sh --container yuki-environment
```

This installs only the pinned SDK, verifies its version and checks the peer's read-only mount. It does not recreate the environment. If the mount is absent, use the explicit replacement below, preserving the full original rootFS; a new `--image` value cannot upgrade a selected checkpoint. Install selected vendor adapters through the original administrator terminal (for example `npm install -g @agentclientprotocol/codex-acp@2.2.2 @agentclientprotocol/claude-agent-acp@0.93.0` with environment Node 22), and configure their actual login or explicit operator environment. The example alone does not install or authenticate them. The peer preserves the environment's standard proxy/CA variables and gives explicit agent environment precedence; it does not inherit Bot secrets.

Existing environments need one explicit replacement:

1. Record original container ID/start, networks/mounts, Manager checkpoint selection and active runs. Existing work may finish first. Deliberately stopping the environment causes a real generation interruption, never automatic replay of ordinary work or unknown installations.
2. Stop the Manager and original environment. In the same stopped-writer window, consistently back up `jobs.sqlite3`, home/runtime, artifact/short-state and the Bot receipt database. Save the original full rootFS as a private Docker commit image. A commit excludes bind mounts, so home/runtime need separate backups.
3. Preserve original home, runtime files and images; archive the stopped container. Offline, point the existing `environment_state.checkpoint_image` at this full rootFS snapshot without altering run/request/hash, completion, service or budget facts. The Manager prefers this setting; changing `--image` alone does not override an existing checkpoint.
4. Update Manager/firewall and let the original Manager create the unique active environment: same home/runtime and execd, `--user 0:0`, current read-only Supervisor/ACP peer and public bridge. Creation explicitly overrides old image user/proxy defaults while retaining HOME and installed apt/pip/npm files. Never run another active environment against the same home or replace the original rootFS with an empty base image.
5. Verify actual UID/HOME, original files/installations, Supervisor mount, original receipts, service recovery and public/private network boundaries. Record Bot health, fixed tools and natural traffic separately. Build locally and verify transfers; do not restart Docker, QQ, proxies or unrelated peers.

RootFS snapshots may retain the original execd secret in image configuration. Keep them as private operations backups; do not publish them or print their environment. Rollback retains current messages, home, runtime and receipt databases, never overwriting new facts with an old whole database. Bot-only rollback may leave the environment running. Actual identity/network replacement requires container/data evidence; health is not a substitute.

## Focused acceptance

`tests/unit/test_terminal_environment.py` covers original Manager SQLite, legacy APT non-replay, admission without heuristic counts, original rootFS selection, the network-guard shell script, and isolated native Supervisor PTY/input/files/background work/explicit timeout. Workspace bridges and Work receipt regressions separately cover publication, media and owned/external runs. These are not real gVisor, administrator-package or public-network measurements.

`tests/unit/test_acp_manager.py` exercises original SQLite/file-wire recovery, prompt completion versus peer lifetime, native cancel aliases, original update pagination/ACK, close-before-open, launch/close races and scoped purge. `tests/integration/test_acp_manager_peer.py` connects the real Manager and SDK peer to a local stdio Agent, including Manager restart during a held permission, original prompt reuse, updates and canonical completion. It substitutes only execd/Supervisor launch. `tests/integration/test_acp_host_peer.py` separately exercises SDK callbacks and lifecycle. These tests do not claim paid vendor execution, production deployment, real cache hits or cost savings.

`deploy/sandbox/validate_runtime.py` performs a real terminal/publication probe only against an explicitly supplied isolated socket. Deployment must separately record administrator identity, preserved installations, execd reconnect, package management, services, networks and resources. Do not send real QQ test messages or infer cache/cost from smaller declarations.

The [2026-09-12 record](https://github.com/YuanYeYouTao/Yuki/blob/v3.9.0/docs/operations/persistent-environment-validation-20260912.md) is a historical deployment/restore point, not evidence that this root/network/tool contract has been deployed.
