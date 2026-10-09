# Persistent Yuki environment

[中文](persistent-environment.zh-CN.md)

Yuki has one global Linux home and workspace. Files are not partitioned by user or
group and do not expire. `short_state` remains a separate small, expiring hint area;
directory listings, files and execution logs are never automatically added to prompts.

## Runtime and tools

The trusted host Manager owns a constant gVisor container, `yuki-environment`, with
the lifecycle label `io.yuki.sandbox=persistent-v1`. It uses OpenSandbox execd at
`441042c288a3eacbe93b36236a9baf6f6254ad4d`; the upstream Apache license is included at
`/usr/share/licenses/opensandbox/LICENSE`. No Jupyter, browser or separate Agent runs.

`/home/yuki` is a 2 GiB ext4 loop filesystem. `/workspace`, `/inputs` and `/work`
refer to its workspace, compatibility inputs and working directories. Bash, Python,
Node.js, Git, curl, jq, rg, archive and compilation tools are preinstalled. pip user
packages, npm prefix and caches live under the persistent home. Disk-full errors
include the capacity and available bytes; terminal writes obey the same filesystem.

Path tools read/write/list/mkdir/move/search/patch files in that filesystem. Reads
return actual SHA256 versions; updates require the current version. Paths are
opened through directory FDs without following host-side symlinks or hardlinks.
Large/binary edits can use the terminal. Version checks detect stale edits, but an
uncooperative terminal writer can still race the final rename; serialize such edits.

`workspace_publish` creates an immutable snapshot for the existing artifact sending
API. `workspace_list` without `path` and old `artifact_id`, `name`, `expected_revision`
arguments remain supported. Migration records map old IDs to paths and preserve
`by-id/` aliases plus `manifest.json`; existing artifact rows keep their nine-column
layout for rollback. Published artifacts retain the existing 512 MiB/1,000-object
store limit independently of the 2 GiB writable home. Explicit deletion reclaims them.

The private artifact manifest is a separate SQLite database. File preparation,
SHA256 and fsync run outside its writer transaction. Publication rechecks revision,
immutability and quotas before renaming private pending files and committing the
whole batch's metadata. Artifact reads/listings use read transactions; reads open
and validate the selected regular, single-link file descriptor before releasing
SQLite, then read and verify its content hash. Cleanup discovers files outside the
writer, rechecks their metadata/references in a short transaction and unlinks at
most 128 unreferenced blobs after commit.
Publication errors discard only private pending files. A commit error can occur
after a successful commit, so renamed blobs remain until reference-checked GC
proves they are unreferenced; an uncertain result does not delete committed files.

`terminal_exec` starts a durable task, returns after about five seconds, and exposes
incremental byte cursors through `terminal_read`. `terminal_write` sends raw input;
`terminal_control` interrupts/cancels/closes. A real PTY running Bash retains shell
variables, functions and directory; execd's ordinary Bash sessions are not used as
a substitute. Normal default timeout is 1,800 seconds; zero means no timer and the
maximum explicit timer is 86,400 seconds. Python work uses `terminal_exec` and explicit
`workspace_publish` immutable snapshots. New `run_python` submissions are rejected; original
run IDs, historical results, and completion acknowledgements remain readable.

The in-container supervisor stores bounded output and exit records independently
of the Manager connection. The host persists launch intent/session identity before
dispatch. It never reconnects a stopped execd session to rerun a command. A durable
start marker adds another duplicate-execution guard. Container start time is part
of the generation identity: interrupted ordinary jobs fail with explicit receipts;
registered services recover according to their policy. Uncertain submissions are
queried by request ID, never blindly retried. Rejected admission creates no continuation.
Docker restart policy is `no` for this container. The enabled Manager starts it only
after the storage unit has mounted both filesystems; Bot or Manager restarts leave
the existing container and its processes running.

`environment_service` registers, starts, stops and inspects at most two internal
services. Stop disables recovery before signaling the process. Repeated failures use
backoff and a five-start limit; a healthy minute or explicit start resets it. `environment_packages`
serializes apt install/remove/repair as root **inside** the container. Successful
installs record dpkg versions and a Docker image checkpoint. The current and previous
checkpoint are retained. Package growth is monitored against a cumulative 1 GiB
budget, including restored checkpoints; this is an admission/monitor budget, not
an overlay filesystem hard quota. Interrupted apt operations can be repaired explicitly.

## Boundaries and resources

Ordinary commands use UID/GID 10001. Docker control, Bot configuration/database, QQ
credentials and the private artifact index are never mounted into the environment.
The authenticated execd API has no public port. Host firewall rules allow only replies
to host-initiated execd connections; new connections to the host remain blocked. Public
HTTP(S) uses the existing Squid exit; registered services can use container localhost.

Limits are 512 MiB RAM, 128 MiB additional swap, one CPU and 128 processes. Admission
allows one main execution, four PTYs and two services and stops below 256 MiB host
available RAM. A separate 128 MiB ext4 loop filesystem bounds runtime files/logs,
so the runtime bind cannot bypass the home quota. Each task retains two 4 MiB log
segments; completed output is rotated/pruned, and acknowledged runtime files older
than seven days are removed. SQLite execution/completion receipts remain independent.

All main Agent entries expose the same frozen definitions, including continuations
and delegated automation. File/terminal access does not extend send/poke/automation
authority. New capabilities do not silently upgrade existing delegation grants.

## Deployment and rollback

The default Compose already persists `./workspace:/app/workspace`. This is the
shared artifact/short-state store, not the writable terminal directory. Manager
owns `/var/lib/yuki-sandbox/home/workspace`; the environment sees that directory
as `/workspace` through its existing `/home/yuki` mount. Do not add another
writable workspace volume or mount Bot data/Docker control into the environment.

The configuration-only installer does not install the host Manager. Path file
tools and terminals remain unavailable until the Linux Docker host has Python
3.12, the verified runsc runtime, the existing environment image and dedicated
egress/network guard, and the bounded home/runtime filesystems. Docker Desktop
alone does not provide this host/systemd setup. A healthy Bot is not proof that
these dependencies are connected.

After preparing those prerequisites and installing the Manager source/venv below,
explicitly connect the artifact store to the deployment root on that Linux host:

```sh
sudo /opt/yuki-sandbox/deploy/sandbox/install-manager.sh --deployment-root /absolute/deployment/path
docker compose exec --user 10001:10001 bot qq-ai-bot-cli setup environment-check
```

The root must be the directory whose Compose mounts `./workspace`; using the same
directory gives Bot and Manager one artifact store. The installer records only
`YUKI_MANAGER_WORKSPACE_STORE` in `/etc/yuki-sandbox/deployment.env`, preserves
other settings and refuses to replace an already configured store. No argument
preserves that file or the legacy `/opt/yuki-qqbot/workspace` default. It does not
guess complex quoted/continued old values: only its whole double-quoted format
or a simple absolute literal path is supported, with `#` kept as a path character.
Unsupported values are refused; other EnvironmentFile bytes remain unchanged. It does not
move persistent home, change an existing container mount, or silently switch an
active Manager to another store. Such a migration requires separate planning.

`environment-check` requires the actual Bot UID 10001, verifies write access to
the artifact-store directory and reads the existing `environment_status` and a
one-item `workspace_list` response. It reports nonzero status for a missing socket,
wrong UID, unready environment or unavailable file interface. It does not create
containers, run code, call a model, send QQ messages or open the Bot database.
It checks connected read capabilities; it does not claim a fresh terminal/package
installation has been executed. Existing deployment overrides remain authoritative.

Build `Dockerfile.environment` and the Bot image locally for linux/amd64. Transfer
and verify/load images on the server; never build there or restart Docker/SnowLuma.
Install Manager source under `/opt/yuki-sandbox/src` and websockets 15.0.1 into its
dedicated Python 3.12 venv. Install `yuki-environment-storage.service` and the updated
`yuki-sandbox.service`; fixed-path storage setup refuses unexpected mounts/symlinks.

Stop only Bot and Manager for a consistent database/config/artifact/receipt backup.
Initialize/mount storage, then run Manager with `--persistent-home ... --migrate-only`
and verify every copied artifact hash. Start the updated Manager and replace **only**
the Compose Bot service using all existing overrides plus the new image override.
Validate healthz, OneBot connection, completion/continuation workers, tool manifest
and real gVisor resources/network/file operations. Retain the prior image and latest
consistent backup. Preserve QQ, proxy, Docker and system maintenance without changing
unrelated service switches. MCP is retired by migration `0098`; the old image cannot
run directly against that head. See the [upgrade guide](../upgrade-3.9.0.md).

Rollback changes images/units, not the current database/home. Keep the persistent
environment running if rolling back Bot alone; its label is excluded from legacy
cleanup. If disabling it, stop registered services deliberately and preserve both
loop images and the Manager SQLite receipt database. Never copy an older whole
database over new messages or execution receipts. Artifact snapshots remain readable
by the previous binary. Path-based tools require the new Manager and Bot contracts.

## Focused acceptance

`test_persistent_environment.py` checks versions, snapshots, path confinement,
pagination, Unicode cursors and receipt staging. Existing workspace/sandbox tests and
the real main-entry HTTP comparison cover both Provider protocols and delegation.
The opt-in Docker scripts `persistent_environment_check.py` and
`persistent_recovery_check.py` exercise real execd, shell state, reconnect, package
checkpoints, service recovery, interrupted installation, OOM and network failures.
The validation image has Docker access solely as a local trusted harness; never deploy
it as the execution container. Server acceptance uses a separate named gVisor container
and small ext4 test filesystems without Bot/QQ mounts or actual message sending.

See the [2026-09-12 deployment and acceptance record](persistent-environment-validation-20260912.md)
for deployed revisions, measurements and the retained restore point.
