# Persistent Yuki environment

[中文](persistent-environment.zh-CN.md)

Yuki shares one Linux environment, `/home/yuki` and `/workspace` across people and groups. Files do not expire. `short_state` remains a separate expiring hint area. Directories, files and full logs are not automatically added to context. The Bot's `./workspace:/app/workspace` stores artifacts/short state; terminal files live under the host's `/var/lib/yuki-sandbox/home/workspace`, exposed as `/workspace` through the original home mount. Explicit publication and attachment import connect these stores.

## Runtime and tools

The trusted host Manager owns the persistent gVisor container `yuki-environment`, labeled `io.yuki.sandbox=persistent-v1`. OpenSandbox execd is pinned to `441042c288a3eacbe93b36236a9baf6f6254ad4d`; the image retains its Apache license. Terminals and services share execd PTY and the original Supervisor. There is no second package executor or sudo approval system.

Commands run as root **inside the environment**, with `HOME=/home/yuki`. Bash, Python, Node.js, Git, curl, jq, rg, archive and compilation tools are preinstalled. Shell commands, apt, pip/npm and ordinary environment file operations use the terminal directly. The pip user base, npm prefix and caches retain the original home. Environment root does not expose host Docker control, Bot databases or credentials.

| Tool | Behavior |
| --- | --- |
| `terminal_exec` | Starts one original execution and returns its `run_id` after about five seconds; `tty=true` supplies an interactive Bash retaining directory, variables and functions |
| `terminal_read` | Reads current progress, exit status, byte-cursor output and original terminal `completion/artifact`; optional `wait_seconds` defaults to zero and only waits for observation |
| `terminal_control` | `input` sends raw text, `interrupt` sends SIGINT, and `cancel/close` stops the original task; files already written remain |

There is no default execution deadline. Explicit `timeout_seconds=0` also disables the timer; a positive value sets the requested deadline without a software maximum. Canceling a read cancels observation only. Empty output is not completion, and a nonzero exit does not undo earlier file writes. Cursor reads identify lost output after bounded log rotation.

New Python, Node and system-package work uses the terminal. `run_python`, `environment_packages`, `get_code_run`, `cancel_code_run` and `terminal_write` no longer accept new calls. Original Python/APT runs remain readable by `terminal_read` with their original completion acknowledgements. An upgrade never dispatches an old unstarted APT intent or replays an installation with an unknown outcome. New package budgets, dpkg collection and automatic Docker checkpoint production are retired; existing images and original receipts remain.

`environment_service` retains register/start/stop/delete/status/logs and real recovery across environment starts, using the original `restart` policy. Stop disables recovery first. Service and consecutive-failure counts are not capped. Updating a registration preserves its current original run until a real restart.

Ordinary file read/write/search/list/edit/delete operations use the terminal. The eight ordinary `workspace_*` CRUD tools, public artifact listing/text paging/delete entries and ordinary overwrite CAS are removed without new-call aliases. `workspace_inspect` still passes selected media to the model; `workspace_publish` creates immutable artifacts for the unified send tool. Conversation attachment import, private tool artifacts and actual media bridges remain. The terminal does not read Bot temporary media caches or private indexes. Later file edits do not change a published version.

The artifact manifest uses separate SQLite. File preparation, SHA256 and fsync happen outside the writer; short transactions verify actual revision/immutability and commit metadata. Reads safely open the original FD before releasing SQLite and verifying content. Publication errors discard only private pending files. Renamed blobs survive uncertain commits until the original GC checks actual references. Ordinary publication has no heuristic 200 MiB, 512 MiB or object-count gate. Actual model-media and platform-transfer limits still apply.

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
docker compose exec --user 10001:10001 bot qq-ai-bot-cli setup environment-check
```

The deployment root is the Compose directory mounting `./workspace`. The installer records only `YUKI_MANAGER_WORKSPACE_STORE` in `/etc/yuki-sandbox/deployment.env`, preserves other settings and refuses to replace an already configured artifact root. Without an argument it retains the existing value or legacy default. It does not move home or alter existing mounts. `environment-check` uses actual Bot UID 10001 to check artifact-directory write access and the existing socket/status/readiness. It no longer calls retired `workspace_list`, creates no container, executes no code, calls no model and sends no messages. This proves connectivity, not live administrator/public-network acceptance.

Update the host's `src/qq_ai_bot/sandbox/`, its `workspace/` dependencies, `deploy/sandbox/` and original Manager venv. Retain the original systemd configuration and every deployment override. The Supervisor is additionally mounted as one read-only source file. A Bot-image update alone does not give an existing environment root identity, current code or a different network.

Existing environments need one explicit replacement:

1. Record original container ID/start, networks/mounts, Manager checkpoint selection and active runs. Existing work may finish first. Deliberately stopping the environment causes a real generation interruption, never automatic replay of ordinary work or unknown installations.
2. Stop the Manager and original environment. In the same stopped-writer window, consistently back up `jobs.sqlite3`, home/runtime, artifact/short-state and the Bot receipt database. Save the original full rootFS as a private Docker commit image. A commit excludes bind mounts, so home/runtime need separate backups.
3. Preserve original home, runtime files and images; archive the stopped container. Offline, point the existing `environment_state.checkpoint_image` at this full rootFS snapshot without altering run/request/hash, completion, service or budget facts. The Manager prefers this setting; changing `--image` alone does not override an existing checkpoint.
4. Update Manager/firewall and let the original Manager create the unique active environment: same home/runtime and execd, `--user 0:0`, current read-only Supervisor and public bridge. Creation explicitly overrides old image user/proxy defaults while retaining HOME and installed apt/pip/npm files. Never run another active environment against the same home or replace the original rootFS with an empty base image.
5. Verify actual UID/HOME, original files/installations, Supervisor mount, original receipts, service recovery and public/private network boundaries. Record Bot health, fixed tools and natural traffic separately. Build locally and verify transfers; do not restart Docker, QQ, proxies or unrelated peers.

RootFS snapshots may retain the original execd secret in image configuration. Keep them as private operations backups; do not publish them or print their environment. Rollback retains current messages, home, runtime and receipt databases, never overwriting new facts with an old whole database. Bot-only rollback may leave the environment running. Actual identity/network replacement requires container/data evidence; health is not a substitute.

## Focused acceptance

`tests/unit/test_terminal_environment.py` covers original Manager SQLite, legacy APT non-replay, admission without heuristic counts, original rootFS selection, the network-guard shell script, and isolated native Supervisor PTY/input/files/background work/explicit timeout. Workspace bridges and Work receipt regressions separately cover publication, media and owned/external runs. These are not real gVisor, administrator-package or public-network measurements.

`deploy/sandbox/validate_runtime.py` performs a real terminal/publication probe only against an explicitly supplied isolated socket. Deployment must separately record administrator identity, preserved installations, execd reconnect, package management, services, networks and resources. Do not send real QQ test messages or infer cache/cost from smaller declarations.

The [2026-09-12 record](https://github.com/YuanYeYouTao/Yuki/blob/v3.9.0/docs/operations/persistent-environment-validation-20260912.md) is a historical deployment/restore point, not evidence that this root/network/tool contract has been deployed.
