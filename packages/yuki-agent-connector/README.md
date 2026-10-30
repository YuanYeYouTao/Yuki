# Yuki Agent Connector

Connector **0.4.1 / wire protocol 4** connects an existing computer to Yuki through
one outbound WSS connection. It provides native Codex, Claude SDK and ACP
connections, and optional local Shell/file capabilities. It does not
install the Bot, create another business task system or mirror native histories.

Desktop includes this locked Python runtime for the Connector. No user Python
installation is required. Independent CLI installations use Python 3.12:
`uv tool install path/to/yuki_agent_connector-0.4.1-py3-none-any.whl`.

## Installation and ownership

```text
yuki-agent-connector pair --url https://your-yuki.example --label "My computer"
yuki-agent-connector instance add --driver codex --label Codex --command /absolute/path/to/codex
yuki-agent-connector doctor
yuki-agent-connector service install
yuki-agent-connector service start
yuki-agent-connector status --json
yuki-agent-connector logs
```

Pairing reads an explicitly issued one-time connector code with hidden input.
Desktop uses the same CLI's `--code-stdin`; its renderer does not receive the
code. The internal device ID owns the installation's journal; its server URL is
transport configuration. Use another `--data-dir` for another device identity. Native credentials stay
local. Status omits device tokens, instance environments and native owner endpoints.

Change the address of the same Yuki with the installation's existing credentials:

```text
yuki-agent-connector server --url https://your-yuki.example
```

This only saves the address, preserving the device, token, instances, full access
and journal. It does not contact the server or start/stop the service. The new
address applies on the next explicit Connector start; a running connection keeps
its current address. An explicit service stop also stops Connector-owned Shell
executions, so applying an address is not an uninterrupted Shell migration.
Desktop's chat address is configured independently. Remote addresses require
HTTPS/WSS; HTTP/WS is allowed only for localhost, 127.0.0.1 and ::1.
Existing journals with URL/device metadata are opened under their original
device ID; the URL change does not replay operations or rewrite their receipts.

Instances are explicit native installations/profiles. `instance configure`
accepts UTF-8 JSON stdin to update an original ID. Omitted arguments/environment
are retained; an empty argument array clears arguments. `CODEX_HOME` and
`CLAUDE_CONFIG_DIR` select native profiles. Cwd is working context, not a Yuki
file-access boundary: new instances default to home, existing ones retain their
configured directory, tasks can override it. Profile edits apply on service restart.

Discovery reads registered installations, PATH/native/npm locations and the
installed Codex daemon's current package metadata. It does not search projects
or login files. Registered installation sources are resolved again at startup
so upgrades retain the original profile/daemon. `discover --instance ID --json`
resolves that original source without starting it. `diagnose --instance ID`
performs an explicit handshake/account read without creating a model task.
An explicitly changed manual executable clears its previous installation source.
Claude npm discovery reads the current package's declared `bin` entry, including
Windows native executables behind npm shims. Its original npm package source is
resolved again on upgrade; old version directories are not additional candidates.

Services run as the current OS user: Windows Task Scheduler uses InteractiveToken
and LeastPrivilege, Linux uses systemd --user, macOS uses LaunchAgent. Closing
Desktop leaves the service running. Stop/uninstall preserves config, journal
and native stores. WSS loss does not restart native owners. Revoked server
authentication stops remote access while owners remain alive until local stop.
Codex shutdown closes only this Connector's proxy; Connector-owned SDK/ACP and
ordinary Shell executions stop on service shutdown. Relative native profile and
socket paths remain anchored to their configured directory; the Codex proxy
itself launches from home without changing the native task directory.

## Full access

Full access lets Yuki use this user's actual Shell and files, including programs
that the user has installed. It is independent of Codex sandbox flags. Old configs/new installs default to false:

```text
yuki-agent-connector full-access --enabled true --json
yuki-agent-connector full-access --enabled false --json
```

The running Connector reads its original local config before new device
operations. No permission poll, extra service or native-owner restart is needed.
Turning access off rejects new general Shell/file operations and further
stdin. Old Agent-only grants remain; original receipts/output and original
interrupt/cancel/close remain available. Turning off does not undo effects or
implicitly cancel running scripts.

Local status exposes full_access and readonly computer facts. The authenticated
device-status RPC includes the actual platform, Shell, home, pipe/TTY support
and interrupt capability. Hello/catalog omit the private home path.

## Terminal and files

The existing command uses `resource: terminal`; Agent commands retain their
original shape. Actions: exec/read/input/interrupt/cancel/close/status/get.
Cloud allocates run_id from the original Host exec request; subsequent commands
retain that run. Each control has its original Host ID. Duplicate IDs query
receipts instead of executing again.

Exec accepts command, optional cwd, tty and timeout_seconds. Windows uses an
installed PowerShell; POSIX uses the user's Shell. This version supports real
pipes (`tty: false`); true TTY requests explicitly fail. Input sends original
UTF-8 bytes. Read accepts cursor/wait_seconds and returns bounded output plus a
byte cursor. Incomplete UTF-8 waits for the next read. Cancellation targets the
original Windows Job Object/POSIX process group, including descendants retaining
the output pipe after the parent exits, while preserving unrelated processes.
Windows pipe mode has no console Ctrl-Break and reports interrupt_unsupported;
interrupt_supported is false there. Use explicit cancel for its original tree.

Each run retains at most two 4 MiB log segments (8 MiB of recent output).
Rotation does not stop the process or change its completion. Cursors retain
their original absolute byte offsets across segments and reader restarts;
output_lost reports when an older requested range has been rotated out. Existing
unsegmented `<run_id>.log` files remain readable. Historical output is not
promised to remain in full, while original execution and completion receipts
remain in the journal.

Completion is one stable result shared by terminal_event.result and the
receipt's completion. Output cursors do not rewrite it. A restarted service
without the original process owner reports unknown and retains the original
journal instead of starting another process. Ordinary commands need no Work.

Files use `resource: file` over the same connection. Stat selects an open file
and returns transfer_id/version/size/SHA-256. Read uses that original handle and
64 KiB chunks. In-place changes fail; where the OS permits path replacement, the
retained original file is used. Inode metadata changes trigger a hash check.
Windows may disallow replacing an open selected file. Release closes the handle.
Internal WSS base64 chunks never become tool text or terminal stdout.

Write accepts original chunk IDs and transfer_id/path/offset/data_base64/final/
size/SHA-256. Each transfer stages its own bytes and verifies size/hash before
final replacement. The same journal retains chunk and whole-transfer receipts
without base64. File/get accepts transfer_id and the original requested path,
including relative/home paths. A lost final response can be recovered without
rereading expired cloud attachments or overwriting later user edits. Interrupted
staging files retain the original transfer for reconciliation; final replacement
removes its staging file. No transfer database or extra daemon is introduced.

## Existing command-line tools

Yuki can run programs already installed for this OS user through the same local
Shell, including OpenCLI and other browser/agent tools. This package does not
bundle, install or adapt OpenCLI, browser extensions, Node or browsers. Setup,
browser authorization, profiles and native sessions belong to the chosen tool.
Its exit code and original stdout remain ordinary terminal evidence, and a lost
response is queried through the original terminal run/request. Do not repeat
an effect merely because the CLI response is unknown. Upload/download files
use the same local file bridge; paths retain their actual computer identity.

## Native agents and progress queries

Codex uses the official app-server proxy to the current user's existing daemon;
socket/endpoint can select an explicit owner. Its maintained WebSocket transport
uses a private socketpair, not another local TCP listener. Native connections
bypass HTTP proxies; outbound WSS/pairing honor proxy and CA settings. Claude
uses the pinned official SDK, an explicit native executable and native settings;
stored sessions are readable, but an independent active Claude TUI is not
claimed attached. ACP negotiates native capabilities; its callbacks use the
actual local session directory.

Agent actions remain list/get/watch/prompt/steer/queue/respond/cancel/detach,
advertised only where supported. Session watches follow actual turns;
execution-scoped watches retain the original turn. Detach ends observation,
not a native session. Formal permission/response IDs retain their native owner.
response_submitted/cancel_submitted confirms submission, not task completion.

Operation receipts, native progress and Work settlement remain distinct. Codex
get returns the latest summary turn by default. include_history returns one
native cursor page, history_order desc, next_cursor toward older material. A
specified execution queries that turn and a page of its items. Native idle is
not a claim that Work completed.

Claude parses the real SDK JSONL chain then selects a recent window. Window
order is ascending; next_cursor is the older offset. tool_use inputs and
tool_result outputs remain, thinking is omitted. Disk work is not O(k). ACP
without native history reports history_available=false without load/resume.
A specified old execution is never replaced by a session watch's newer turn.

The private journal retains original dispatch evidence, observation bindings,
process logs and unacknowledged important events. Native
transcripts/reasoning/token streams are not mirrored. Events use one original
sequence and ACK; terminal facts remain after ACK. Unknown outcomes never cause
blind redispatch.

## Distribution and validation

Desktop ships a platform-specific relocatable CPython runtime with this exact
locked wheel and dependencies. Its content digest distinguishes same-version
builds. Upgrades retain the selected config/data/IDs/journal. Another installation
and external-runtime adoption require their explicit UI actions. Bot and
Connector must both use protocol 4. Builds use pinned Python archive/dependency
hashes, then move to a Unicode/spaces path for module/driver/SQLite/TLS/status
checks. Notices/licenses travel with the runtime. Managed launches preserve
isolated mode, so project modules cannot replace the bundled Connector. Package
assembly runs on its target OS/architecture; signing/notarization is a separate
release check. An unsigned local build is not a release.

```text
uv sync --frozen --python 3.12
uv run --no-sync ruff check .
uv run --no-sync mypy src
uv run --no-sync pytest -q
uv build --out-dir dist
uv run --no-sync python scripts/verify_wheel.py
uv run --frozen python scripts/build_runtime.py --output /staging/agent-connector --cache /build-cache/python
```

CI runs on Linux/Windows/macOS. Real Shell tests check bytes, duplicate requests,
process-tree cancellation, unrelated processes and restored original receipts.
Protocol fixtures, actual provider/OS-service acceptance, local builds and deployed
user acceptance remain separate evidence. Browser behavior is owned by the user's
selected CLI; this package does not claim its installation or browser acceptance.
