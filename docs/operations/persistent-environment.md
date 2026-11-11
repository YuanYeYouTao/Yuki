# Persistent Yuki environment

[中文](persistent-environment.zh-CN.md)

Yuki shares one Linux environment, `/home/yuki` and `/workspace` across people and groups. Ordinary persistent files do not expire. `short_state` remains a separate expiring hint area. Directories, files and full logs are not automatically added to context. The Bot's `./workspace:/app/workspace` stores artifacts/short state; terminal files live under `workspace/` in the Docker volume `yuki-sandbox-home`, exposed as `/workspace` inside the environment. Sending freezes an immutable delivery snapshot from an ordinary file; inbound attachments copy directly from the shared cache to the working directory.

Temporary media from every conversation shares `/inputs/media-cache`, a read-only mount of the same Bot-owned cache, not another copy in the 2 GiB home. Bot remains its sole writer and expiry/cleanup owner; expired media is deleted by the original cleaner, while ordinary working files, collected stickers and immutable artifacts do not expire with it. Main may read it from the terminal or copy originals into `/workspace` or a sticker folder for unrestricted editing, read selected media with `workspace_inspect`, or send it with `send(path="/inputs/media-cache/...")`. The Host reuses the original safe FD bridge and does not follow model-written links to host files. The user explicitly authorized this global shared view; terminal reads are not limited to the current Conversation.

## Runtime and tools

The Compose service `manager` owns the long-lived container `yuki-environment`, labeled `io.yuki.sandbox=persistent-v1` and with its owning Compose project in `io.yuki.sandbox.project`. Manager runs the same image and version as the Bot, controls Docker through the mounted docker.sock, and sets the environment firewall from the host network; the Bot reaches it only through `manager.sock`. OpenSandbox execd is pinned to `441042c288a3eacbe93b36236a9baf6f6254ad4d`; the image retains its Apache license. Terminals and services share execd PTY and the original Supervisor. There is no second package executor or sudo approval system.

Commands run as root **inside the environment**, with `HOME=/home/yuki`. Bash, Python, Node.js, Git, curl, jq, rg, archive and compilation tools are preinstalled. Shell commands, apt, pip/npm and ordinary environment file operations use the terminal directly. The pip user base, npm prefix and caches retain the original home. Environment root does not expose host Docker control, Bot databases or credentials.

| Tool | Behavior |
| --- | --- |
| `terminal_exec` | Starts one original execution and returns its `run_id` after about five seconds; `tty=true` supplies an interactive Bash retaining directory, variables and functions |
| `terminal_read` | Reads current progress, exit status, byte-cursor output and original terminal `completion/artifact`; optional `wait_seconds` defaults to zero and only waits for observation |
| `terminal_control` | `input` sends raw text, `interrupt` sends SIGINT, and `cancel/close` stops the original task; files already written remain |

There is no default execution deadline. Explicit `timeout_seconds=0` also disables the timer; a positive value sets the requested deadline without a software maximum. Canceling a read cancels observation only, not the command. Empty output is not completion, and a nonzero exit does not undo earlier file writes. Cursor reads identify lost output after log rotation.

Bot task intents, completions, permission callbacks and continuations share the main Database writer.
They commit under the original request/run before acknowledgement; completion and parent Work input
settle atomically. File preparation, JSON encoding and remote waits stay outside that writer. Manager
keeps its own SQLite owner for jobs.sqlite3 and does not join the Bot's main writer queue. Canceling
observation or restarting Bot does not cancel the original Manager execution or resubmit an existing
request.

New Python, Node and system-package work uses the terminal. `run_python`, `environment_packages`, `get_code_run`, `cancel_code_run` and `terminal_write` no longer accept new calls. Original Python/APT runs remain readable by `terminal_read` with their original completion acknowledgements. An upgrade never dispatches an old unstarted APT intent or replays an installation with an unknown outcome. New package budgets, dpkg collection and automatic Docker checkpoint production are retired; existing checkpoint images and original receipts remain.

`environment_service` retains register/start/stop/delete/status/logs and real recovery across environment starts, using the original `restart` policy. Stop disables recovery first. Service and consecutive-failure counts are not capped. Updating a registration preserves its current original run; the next real start uses the new content.

Ordinary file read/write/search/list/edit/delete operations use the terminal. The eight ordinary `workspace_*` CRUD tools, public artifact listing/text paging/delete entries and ordinary overwrite CAS are removed without new-call aliases. `workspace_inspect` still passes selected media to the model; `send(path=..., attachment_kind=...)` internally freezes an immutable artifact and delivers it. There is no separate model publication tool. Sticker collection and attachment saving stream directly from the read-only cache into their final files, without permanent intermediary snapshots. Conversation attachment promotion, private tool artifacts and actual media bridges remain; the terminal can read temporary media originals through the shared read-only mount, while private Bot artifact indexes are not mounted. Later file edits do not change a published version.

The artifact manifest uses separate SQLite. File preparation, SHA256 and fsync happen outside the writer; short transactions verify actual revision/immutability and commit metadata. Reads safely open the original FD before releasing SQLite and verifying content. Publication errors discard only private pending files. Renamed blobs survive uncertain commits until the original GC checks actual references. Ordinary publication has no heuristic 200 MiB, 512 MiB or object-count gate. Actual model-media and platform-transfer limits still apply.

### External agents are separate from the environment

The external_agent tool has left the environment tools and the Manager's new-task path. Native agents on the user's computer connect to the ExternalAgent service through a separate connector; the server/workspace no longer launches Codex, Claude or ACP subprocesses. Terminals, files and environment services retain their original Manager path, independent of device connectivity.

When upgrading, remove `--acp-config` from the original Manager ExecStart and `YUKI_MANAGER_ACP_CONFIG` from the deployment environment. New environments do not install the ACP SDK, mount acp_peer.py, or install vendor adapters that stand in for the user's computer. Updating source does not automatically delete or recreate existing rootFS files, user installations or processes.

Original server ACP request/run/peer/permission/cursor/completion receipts remain readable. The legacy reader observes original activities, answers original permissions, precisely cancels them and settles actual completions. It cannot admit another peer or prompt. Unknown outcomes remain unknown; Work is not bulk-reset and paid operations are not resubmitted. Obsolete mounts may remain until actual container recreation; removing one mount does not require interrupting all terminals. Archiving Yuki work does not delete native sessions, user logins or vendor history.

## Recovery, resources and network

The Manager persists the original request, launch intent and execd session identity before dispatch. After disconnect it checks the native exit record, otherwise reattaches to the original session. The durable `started` marker prevents another execution. A stopped PTY, handshake timeout or missing legacy APT heartbeat does not prove failure from age. Uncertain submission is queried by its original request, never blindly resubmitted. The Bot persists completion before acknowledging the outbox. Lost acknowledgements or repeated reads do not launch commands or reset budgets.

Container ID and start time form the generation. Bot/Manager restarts retain the original container and processes. Real environment recreation records interrupted ordinary jobs while registered services follow their policy. Docker restart policy is `no`; the Manager starts the container after its own checks. Upgrading the Manager does not silently delete or recreate the environment.

Resources get initial values from the profile (see below), and each can be overridden individually in `.env`. These are deployment resources, not admission scores; capacity, process and OOM outcomes reflect actual OS/Docker facts. Under runc, exceeding memory ends only the offending process; under runsc (gVisor), the whole environment exits and is handled as a real generation interruption. Each task retains two 4 MiB log segments. Completed logs and acknowledged runtime files older than seven days are reclaimed independently of retained SQLite execution/completion receipts.

HOME, run records, Manager state, code files and the media cache are Docker named volumes with no separate size limit; they share the disk with Docker and Bot data. `environment_status.storage` reports the capacity and free space of that shared disk (`shared_disk: true`). An environment that fills the disk makes Bot database writes fail, and the actual error is reported.

The environment joins the `yuki-sandbox-egress` bridge (`yuki-egress0`, IPv6 disabled), which Manager creates when missing. Manager sets rules in the iptables backend Docker uses (the one containing the DOCKER-USER chain): the environment cannot initiate access to the host, private, link-local and other non-public IPv4 destinations; normal public TCP/UDP, including SSH, is available without port allowlists; only replies to Manager's own execd connections are admitted. If Docker uses the nftables firewall backend (no DOCKER-USER), Manager reports `network_guard_unsupported` and does not create the environment; if Manager is not on the host network (for example rootless Docker), it reports `network_guard_missing`. Docker sockets, Bot configuration/databases, QQ credentials and private artifact indexes are not mounted into the environment. `YUKI_SANDBOX_PROXY` can set an HTTP proxy for programs in the environment; none is set by default.

On a Docker user-defined bridge, gVisor cannot reach Docker's DNS at `127.0.0.11`. Under runsc, write reachable DNS servers into a file in HOME (for example `.config/resolv.conf`, `/home/yuki/.config/resolv.conf` inside the environment) and set `YUKI_SANDBOX_RESOLV_CONF=.config/resolv.conf`; Manager mounts it at `/etc/resolv.conf` when creating the environment. runc uses Docker's DNS directly. See the [gVisor DNS guidance](https://gvisor.dev/docs/user_guide/faq/#dns-doesnt-work-on-a-docker-user-defined-bridge-network-including-docker-compose).

Main entries, automation and subagents use fixed capability contracts. Directory queries do not modify submitted definitions. Environment root does not extend person/send/delegation/plugin-domain authorization. Optional Code Mode's Monty remains a filesystem/network-free orchestration runtime, not a terminal alias.

## Installation, profiles and upgrade

### Installation

`install.sh` (`install.ps1` on Windows) does everything in one step: downloads and verifies the release bundle, configures the character and model on first run, selects the profile, determines the runtime, creates the data volumes (`deploy/sandbox/prepare.sh`), pulls images, runs `docker compose up -d`, then runs `yuki setup environment-check` as the Bot's user until the environment is ready. The host needs only Docker and Compose; no systemd unit, venv, loop disk or sync script is installed.

```sh
sh install.sh --dir ~/yuki                       # interactive
sh install.sh --dir ~/yuki --yes --profile lite --answers model.json   # non-interactive
```

`model.json` takes the same configuration input as Desktop: `{"protocol", "base_url", "api_key", "model"}`. Running it again in the same directory upgrades: release files are replaced, while `.env`, data and `docker-compose.override.yml` (put custom changes there) are kept. The volumes are external, so `docker compose down -v` does not delete them. One Docker host runs one Yuki sandbox: a Manager from another Compose project that finds an environment it does not own reports `environment_name_in_use` and does not take it over.

### Profiles

| Setting | `lite` | `headless` | `desktop` |
| --- | --- | --- | --- |
| Environment image | `yuki-environment:<version>` | `yuki-environment:<version>-desktop` | `yuki-environment:<version>-desktop` |
| Browser | None | Headless Chromium | Chromium + Xvnc desktop |
| Memory / additional swap | 512 / 128 MiB | 1024 / 256 MiB | 3072 / 512 MiB |
| CPU / processes | 1 / 256 | 1.5 / 1024 | 2 / 2048 |
| /tmp / /dev/shm | 128 / 64 MiB | 256 / 128 MiB | 512 / 256 MiB |

The installer recommends a profile from the memory Docker reports (below 2 GiB `lite`, 2–4 GiB `headless`, more `desktop`); the user chooses and nothing is blocked. Desktop local deployments default to `desktop`. The `computer` and `browser` tools are declared under all three profiles: under `lite` they return `browser_unavailable`, and under `headless` `computer` returns `display_unavailable`. `environment_status` reports the profile, mode, runtime, installation result and actual limits.

To change profile, edit `YUKI_SANDBOX_PROFILE` in `.env` and run `docker compose up -d`. Moving up, Manager installs the required Debian packages on the original rootFS (the result is written to `programs`; on failure the original installation is kept and retried on the next start). Moving down changes only mode and resources; installed software and browser data are kept. A running desktop service restarts as a new run in the new mode. Memory, swap, CPU and process limits take effect immediately through `docker update`; if Docker refuses, its message is written verbatim to `limits_update_error`. /tmp and /dev/shm take effect only on explicit recreation; pending items are listed in `recreate_required`.

The runtime `YUKI_SANDBOX_RUNTIME` is written at first install as `runsc` or `runc`, depending on whether Docker has registered runsc with `--overlay2=none`, and is kept afterwards; it never switches implicitly from gVisor to runc. If runsc is configured but Docker lacks it, Manager reports `gvisor_unavailable`; if runsc lacks `--overlay2=none`, it reports `gvisor_overlay_unsupported` (otherwise installs inside the environment would not reach the Docker layer and checkpoints would lose software).

### Code files

Yuki's own programs inside the environment (Supervisor, sticker index, desktop services, browser driver, `yuki-computer` and so on; see `persistent.ENVIRONMENT_FILES`) are not in the environment image. On every start Manager writes this version's files into the `yuki-sandbox-code` volume, and the environment mounts each file read-only at its original path. Rewriting in place keeps the inode, so the running environment and old rootFS checkpoints both get current code with each Bot upgrade, without recreation.

### State and recovery

The execd token and execution receipts live in `jobs.sqlite3` in `yuki-sandbox-state`. If the state volume is lost while the environment still exists, Manager reports `manager_state_missing` and neither generates a new token nor takes over. If Manager recovery fails, it exits with the reason in its log and `restart: on-failure` retries; the Bot keeps running and terminal tools report the environment as unavailable.

### Migrating from the host Manager

Old deployments (systemd, loop-mounted ext4, host venv) migrate once, explicitly, with `deploy/sandbox/migrate-host-manager.sh`; the installer refuses when it detects an old environment and points to it. Run the script as root on the Docker host. It confirms there are no active executions, stops and disables the host Manager, `docker commit`s the original environment rootFS as a private checkpoint (if runsc lacks `--overlay2=none`, it first exports the upper layer as in the [Mac migration record](mac-arm64-migration-20261107.md)), copies HOME, run records, the media cache and `jobs.sqlite3` into the matching volumes and points them at that checkpoint, removes the old Squid, internal bridge and related firewall rules, and writes the original configuration into `.env`. The original loop images, databases and container (renamed `yuki-environment-retired`) are kept as they are; after `docker compose up -d` and checking terminals, files, services and network, delete them with `--cleanup`.

Checkpoint images may contain the original execd secret. Keep them as private backups; do not push them publicly.

### Retiring media snapshots

Old intermediary snapshots are retired with the [offline script](../../scripts/retire_media_snapshots.py), auditing a complete stopped copy. By default it only lists candidates; `--apply` retires only snapshots confirmed unreferenced whose ordinary file still holds the same bytes.

## Focused acceptance

- `scripts/sandbox_compose_e2e.py` (CI `sandbox-compose`): installs `lite` with the real installer, calls Manager as the Bot's user, and verifies the terminal, public reachability with host and private networks unreachable, the `lite` browser being unavailable, Manager restart keeping the environment and original executions, and `down -v` keeping data; it then switches to `headless`, installs the browser on the original environment and opens a page. Artifact: `test-results/sandbox-compose/steps.json`.
- `tests/unit/test_sandbox_profiles.py`: lost state volume, project ownership, runtime checks, profile-change installation and failure, immediate limits and recreation-required items.
- `tests/unit/test_terminal_environment.py`: Manager SQLite, legacy APT non-replay, original rootFS selection, volume mounts, firewall rules and backend selection, and isolated native Supervisor PTY/input/files/background work/explicit timeout.
- Headed desktop: see `tests/integration/test_computer_browser_e2e.py` (CI `desktop-headed`).

These are not production rollout or vendor-execution acceptance.
