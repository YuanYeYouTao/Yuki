# Yuki Agent Connector

Connector **0.9.5 / wire protocol 5** connects an existing computer to Yuki through
one outbound WSS connection. It provides native Codex, Claude SDK and ACP
connections, and optional local Shell/file capabilities. It does not
install the Bot, create another business task system or mirror native histories.

Desktop includes this locked Python runtime for the Connector. No user Python
installation is required. Independent CLI installations use Python 3.12:
`uv tool install path/to/yuki_agent_connector-0.9.5-py3-none-any.whl`.

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
accepts UTF-8 JSON stdin to update an original ID, or create the caller's ID with
`create: true`. Desktop uses one save through its bundled CLI, including for an
independently installed Connector's original data directory. Omitted arguments/environment
are retained; an empty argument array clears arguments. `CODEX_HOME` and
`CLAUDE_CONFIG_DIR` select native profiles. Cwd is working context, not a Yuki
file-access boundary: new instances default to home, existing ones retain their
configured directory, tasks can override it. Online Desktop saves refresh the original
device through its existing status channel. Since 0.4.3 the Connector applies instance changes
without restarting unrelated native owners, Shell or file transfers. Independent
CLI edits are applied on the next status request or reconnect; older running services
require their own reload, which Desktop reports without taking them over.

`instance remove ID` removes that local configuration and, after applying the complete
catalog, its current task candidate. Original device/instance IDs, native data and
receipts remain. The delete action does not cancel paid work; explicit original
operation controls still use its original owner while the device remains authorized.

Discovery reads registered installations, PATH/native/npm locations and the
installed Codex daemon's current package metadata. It does not search projects
or login files. Registered installation sources are resolved again at startup
so upgrades retain the original profile/daemon. `discover --instance ID --json`
resolves that original source without starting it. `diagnose --instance ID`
performs the driver-specific readiness check without creating a model task. For a
default Codex target, preparation invokes the selected installation's official daemon
start once, then connects the original proxy target. Existing explicit endpoints or
sockets are used directly, without starting another default daemon. Reconnects do not
repeat daemon preparation; Yuki does not update or restart the native daemon.
An explicitly changed manual executable clears its previous installation source.
Claude npm discovery reads the current package's declared `bin` entry, including
Windows native executables behind npm shims. Its original npm package source is
resolved again on upgrade; old version directories are not additional candidates.
The discovered desktop, daemon-package and npm labels describe installation sources,
not different connection modes or proof that a native service is running. Claude's
SDK calls the selected CLI; an npm installation is not an alternative to the SDK.
ACP starts the selected compatible program with exact ordered arguments. Its flags
come from that program's documentation, not a universal `--stdio` default.

Desktop starts its Connector with `run --desktop-stdin` and owns the open stdin
pipe. Desktop exit or loss of that pipe stops this Connector through its normal
cleanup. Hiding the Desktop window in the tray does not exit the app. This flag
is for Desktop ownership; ordinary `run` does not observe stdin.

With `--desktop-stdin` the Connector also writes one JSON object per stdout line
for its Desktop supervisor, e.g. `{"type": "state", "state": "connected"}`; states are
`connected`, `disconnected` (with a diagnostic `reason`), `authorization_lost` (handshake
HTTP 401/403 only) and `protocol_unsupported`. Every other handshake status and every
close after the handshake reconnects. An unexpected error is logged with its stack in
`connector.log` and exits non-zero so Desktop can restart the process.

Explicitly installed standalone services run as the current OS user: Windows Task
Scheduler uses InteractiveToken and LeastPrivilege, Linux uses systemd --user,
macOS uses LaunchAgent. They remain independent of Desktop. Stop/uninstall preserves config, journal
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
plus scope/observe/release, advertised only where supported. Session watches
follow actual turns; execution-scoped watches retain the original turn. Detach
ends observation, not a native session; detach with release also closes the
native hold Yuki has at the turn boundary once no other operation holds it
(Claude SDK process, ACP session or process, Codex client subscription only).
Formal permission/response IDs retain their native owner.
response_submitted/cancel_submitted confirms submission, not task completion.

Protocol 5: prompt/queue/watch/steer/respond/cancel/detach are answered by a
`receipt` event written in the same journal transaction as their outcome and
retransmitted until the server's ACK; reads and device resources keep their RPC
response. A duplicate command ID never executes again.

An observe watch only ever receives request metadata (request ID, tool name,
`observed: true`) and is never answered, controlled or written through. A Claude
terminal session is observed through the profile's session registry
(`$CLAUDE_CONFIG_DIR/sessions`); a Claude session live in another process is
not resumed (`session_live_elsewhere`) unless explicitly forked. A scope
(`write_paths`, `network`, command prefixes) only narrows: Claude routes side
effects through the connector with a `permissions.ask` override, Codex narrows
and later restores the thread settings, ACP switches to an asking mode; anything
outside the scope is reported, except that ACP client callbacks the connector runs
itself (`terminal/create`, `fs/write_text_file`) are refused outright (progress
`scope_refused`, `invalid_params`) without a card. An ACP request inside the scope
is approved only with the agent's `allow_once` option; without one it is reported.
A command is inside the scope when, after stripping surrounding whitespace (Python's
Unicode whitespace, so a trailing newline is dropped), it contains none of
``; & | < > ` $ ( )``, newline or carriage return (inside quotes and `2>&1`
included) and equals a prefix or starts with the prefix plus one ASCII space
(`git log` matches `git log -5`, not `git logx`, `git push` or `git<TAB>log`);
only `*` admits the rest. Without `*`, combined commands are outside the scope
(#647 not relaxed): native approvals are reported, an ACP `terminal/create` is
refused; with `*`, they pass the command check even when `write_paths` or
`network` are narrowed. The prefix check looks neither at the cwd nor at the paths a
command writes, so `write_paths` does not confine an approved command: Claude runs
it under the user's own account with no connector sandbox; Codex sandboxed
commands never ask, and an `accept` only consents to that one request (where and
whether it then runs is up to Codex); an ACP agent runs its own commands under its
own sandbox, if any, while the connector runs an in-scope `terminal/create`
directly under the user's account. Since 0.9.5, under `commands` without `*`, only
a Codex command approval whose `kind` is absent or `command` can be auto-approved
by prefix; `writeStdin` (input to a running terminal), `humanCommand`, null or any
other kind is reported. Such a card may not show the input; decline or ask the user
when it does not. The connector does not declare `requiredHumanApproval`, so
`humanCommand` does not arrive today; under `*` the kind is not checked, while the
other conditions still apply (no `additionalPermissions`, network allowed when a
`networkApprovalContext` is present, a non-empty string or list command).
Known defects in 0.9.5, being fixed separately (planned for 0.9.6), so these
protections do not hold yet: (1) detaching an ACP delegation while Yuki's prompt
still runs drops its scope before the session closes, and `terminal/create` /
`fs/write_text_file` callbacks in that window run unchecked; (2) a Codex
file-change approval checks each change's `path` and `grantRoot` but not a move
destination (`kind.move_path`); (3) a Codex `item/permissions/requestApproval`
answer with `scope: "session"` is forwarded as is, beyond one request.
A full scope (`write_paths` containing `/`,
`network: true`, `commands: ["*"]`, 0.9.2) narrows nothing: Claude opens in
permission mode `auto` with only SendMessage forced to ask, Codex gets
`dangerFullAccess` + approvals `never` (restored after Yuki's turn like any
scope), ACP keeps the agent's own mode; whatever the agent still asks is
approved except questions and cross-session messages. A Claude process opened
in auto mode accepts only full-scope inputs (`scope_conflict` otherwise).

ACP (0.9.4): an agent advertising `_session/steering` (top-level
`_meta.steering.supported`, e.g. codex-acp 2.9.0, claude-agent-acp 0.109.1) is
declared `steer`; a steer goes only to Yuki's running prompt with
`idleBehavior: "promptRequired"` (`turn_not_running` when the agent did not take
it), and without the extension steer/queue stay unsupported. A rejection's effect
is whatever the agent reports (the prompt's `stopReason`, a failed tool call), not
inferred from an option ID; Yuki's `deny` selects the agent's one-time reject.
Prompt receipts carry the session `cwd`; resuming a session this connector no
longer holds uses session/list when offered, else the cwd recorded when the
session was created here (`cwd_unknown` without a record).

Codex thread settings apply to the thread's next
turns: a turn that starts before Yuki's queued input (for example the user's own)
also runs with the narrowed settings until Yuki's turn ends or the item is
cancelled; the receipt says so (`scope_race`) and carries the settings to restore
(`thread_settings_before`), so a restarted connector still restores them. Codex
always lets a workspace-write turn write the thread's cwd, so a scope that does
not cover it is refused (`scope_unsupported`). Owner answers are one-time only
(`one_time_only` for session/policy-wide options). Codex requests of turns Yuki did not start reach
Yuki as metadata only. Turns a native agent starts by itself inside an owner
Yuki holds carry `owner_operation_id` and `followup_execution`.

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
Connector must both use protocol 5. Builds use pinned Python archive/dependency
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
