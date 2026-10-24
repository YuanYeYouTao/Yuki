# Yuki Agent Connector

Independent Python 3.12 connector for an existing local Codex daemon, the official Claude
Agent SDK and generic ACP agents. It does not install the Yuki Bot, own business tasks,
mirror native transcripts or send chat messages itself.

For ordinary Desktop users, use the bundled setup UI; no Python or terminal installation is required.
For an independently managed CLI installation, install the wheel with Python 3.12, for example
`uv tool install path/to/yuki_agent_connector-0.3.4-py3-none-any.whl`.
The connector uses its own lock file and does not install the Bot project. The pinned
Claude SDK currently includes its own large CLI payload; the driver nevertheless
requires the explicitly selected native executable and never silently runs that payload.

```text
yuki-agent-connector pair --url https://your-yuki.example --label "My computer"
yuki-agent-connector instance add --driver codex --label Codex --command /absolute/path/to/codex --cwd /your/project
yuki-agent-connector doctor
yuki-agent-connector service install
yuki-agent-connector service start
yuki-agent-connector status --json
yuki-agent-connector logs
```

Pairing reads an explicitly issued one-time **connector** code with hidden input,
exchanges it at `/agents/v1/pair`, and stores the independent device credential in
the user's private installation. `--code-stdin` is the same entry for Desktop and
other headless callers; `YUKI_CONNECTOR_CODE` also supports unattended pairing.
The server URL, internal device ID and pending journal keep one installation identity.
Use a separate `--data-dir` to pair another device identity. Native credentials are
never sent to Yuki, and `status --json`/`instance list` omit credentials, environment
and owner endpoints.

Profiles use `instance add --id STABLE_ID --env CODEX_HOME=/your/profile` or the
native `CLAUDE_CONFIG_DIR`; repeat `--env` for explicit local settings. `--agent-arg`
passes configured ACP or Codex executable arguments. They are installation settings,
not model-controlled command strings. `instance remove ID` changes configuration;
restart the service to apply profile changes.

- Codex defaults to the official `app-server proxy`, which finds the current user's
  already-running daemon. `--socket` or `--endpoint` selects an explicit existing
  owner. The maintained `websockets` library handles a private socketpair to the
  official byte proxy; no local TCP listener or hand-written WebSocket framing is
  exposed. Native owner connections bypass HTTP proxies; the outbound Yuki WSS and
  pairing HTTP honor standard proxy/CA environment settings.
- Claude uses official `claude-agent-sdk==0.2.172`, explicit `cli_path`, the native
  `claude_code` system/tool presets and `user/project/local` configuration. Choose a
  real executable on Windows rather than `.cmd`/`.bat`. Stored sessions can be
  listed/read; an independent active TUI is not live-attached. Explicit resume creates
  an SDK-managed continuation. Precise steer/queue is not advertised by this driver.
- ACP uses `agent-client-protocol==0.12.1` and negotiates optional list/load/resume.
  Files and terminal callbacks execute in the selected computer/session's actual
  working directory. ACP request IDs are qualified by their real protocol owner;
  ACP does not expose a persistent native turn ID. No server `/workspace` is assumed.

The fixed wire actions are `list/get/watch/prompt/steer/queue/respond/cancel/detach`.
Drivers advertise only their supported actions. A watch covers one original
execution (or the first actual execution when idle), then ends at its native
terminal receipt. It never closes the session or daemon. Permission replies use the
original `permission_id` and formal native `response` object (ACP can also select
an original `option_id`). `response_submitted` / `cancel_submitted` confirms only
delivery into that formal native interface. It does not claim the original prompt
continued, stopped or completed; that execution has its own receipts. Interrupted or
unconfirmed submissions stay unknown, and no fallback interrupts, forks or resubmits
an original operation.

Only original dispatch facts, observation bindings and unacknowledged important
events enter the private journal. Duplicate original commands do not dispatch twice;
an acceptance/crash window stays unknown until original-resource reconciliation.
Native histories, reasoning, signatures and token streams are not mirrored. Important
events persist before transmission and are removed from the outbox only after server
ACK. Terminal facts remain queryable after ACK.

Network loss does not exit SDK/ACP owners. Revoked server authentication stops remote
connections without killing local owners or repeatedly retrying rejected credentials.
Local `service stop`/process exit is different: Codex closes only this connector's
client/proxy; SDK/ACP subprocesses owned by the connector stop with it. Reconciliation
does not silently resume a new paid execution after a process crash.

Services are per OS user: Windows Task Scheduler uses `InteractiveToken` and
`LeastPrivilege` after login; Linux uses `systemd --user`; macOS uses a user
LaunchAgent. `service stop` and `service uninstall` preserve configuration, journal
and native stores. Closing Desktop does not stop this service. Desktop invokes the
same CLI with the explicitly selected original data directory, without another driver or credential copy.

Development and artifact checks:

```text
uv sync --frozen --python 3.12
uv run --no-sync ruff check .
uv run --no-sync mypy src
uv run --no-sync pytest -q
uv build --out-dir dist
uv run --no-sync python scripts/verify_wheel.py
```

The independent CI job runs on Linux, Windows and macOS and uploads the built wheel.
`scripts/service_smoke.py` can test an actual disposable user service against a local
fake server with zero native instances; it does not touch existing tasks or paid models.
Real provider tests, OS service tests and source-level fake protocol tests remain
distinct acceptance evidence.


### Connector 0.3.0 / wire protocol 3

Update Bot and Connector together. Keep the original journal and device identity. The hello uses
`protocol_version: 3`. Existing watches retain their original execution scope; a new watch with only
a session ID follows that session across turns. Supplying an execution ID watches that turn alone.
Detaching stops observation, not the native session.

The local journal reduces native facts into one receipt projection. Events carry `sequence` and
`operation_state`; query and command responses return the same projection with `state_sequence`.
A lost stream becomes unknown without erasing confirmed admission. Late progress cannot reopen a
terminal observation. Unknown dispatches are reconciled using the original IDs, never resubmitted.

Codex reconnects to the selected owner and restores observations from the journal. Native questions
belong to the connection that issued them. Claude SDK and ACP report loss of their owned execution
streams without pretending they can recover an inaccessible process. Native readiness is separate
from the device WebSocket connection.

Events stream from the original journal by sequence, using WebSocket backpressure. Commit ACKs prune
originals without imposing a round trip before every send. Failure of the event sender, catalog sender
or reader ends that connection. An acknowledged old-protocol event whose ACK was lost remains the same
fact after upgrade; replay does not duplicate its Work input. Complete diffs are no longer pushed as
progress; file paths, command results, plans, questions and native tool milestones remain available.

### Desktop-managed distribution (0.3.1; wire protocol remains 3)

Desktop ships a platform-specific, relocatable CPython directory with this exact
locked Connector wheel and dependencies. The user does not need Python, uv, pip,
or a terminal. The build machine assembles it with `scripts/build_runtime.py`,
using pinned python-build-standalone archive digests and `uv.lock` hashes. A build
moves the complete directory to a path containing spaces/Unicode before checking
the real module entry point, SQLite/TLS, all three drivers and local status.
Python's distribution notices, dependency dist-info licenses, the Yuki license,
and the dependency lock travel with the runtime. This is not a relocated venv.

Desktop copies the bundle to a stable per-user runtime directory before installing
a service. The selected data directory remains unchanged across runtime upgrades:
config, device/profile IDs and journal are not copied or recreated. The runtime
content digest distinguishes builds with the same package version. A separate
installation is created only through the explicit UI action. Existing installations
can be selected with their original absolute Python path and data directory;
Desktop does not upgrade an externally managed Python installation.

Managed launches use `-I -B -m yuki_agent_connector`; the installed user service
and Claude inspection subprocess preserve isolated mode. Thus project/CWD Python
files and user site-packages cannot replace the bundled module. The module and
service model are the same as the independent CLI. Package assembly must run on
the target OS/architecture, and official installers still require the corresponding
platform signing/notarization checks; an unsigned local build is not a release.

`discover --json` reads registered Codex installation locations and explicit
absolute executable locations. It never searches the current directory or reads
login files. `instance configure` reads UTF-8 JSON bytes from stdin independently
of the console encoding, including isolated Windows runtimes. It updates a
specified original instance ID; it retains environment, native owner endpoint and
journal. Version 0.3.2 permits omitting `cwd`: existing instances retain their
directory and new instances use the original home-directory default. It returns
`{"instance_id":"..."}` only after saving; continue with that original identity
if a later service operation fails. Registering an installation does not create
a native thread. A registered installation source is re-resolved when the Codex client
starts so an app update need not create another profile. `diagnose --instance ID`
is an explicit no-model native handshake and account/read check; an installed
binary, a running Connector service, device admission, and native login readiness
are separate observations. Status contains actual registered service executable
information, not merely the Python used by the status command.

Version 0.3.4 adds `discover --instance ID --json` to resolve the original
instance's current executable as `{"instance_id":"...","command":"..."}`.
It uses the same registered installation source as startup, without starting
Codex or changing configuration or the journal. Editing that instance's label
or project also re-resolves its source before validating the executable. An
explicitly changed manual path clears the registered source; a missing original
installation fails rather than selecting another candidate.

Example build-machine command (choose a new output directory):

```text
uv run --frozen python scripts/build_runtime.py --output /staging/agent-connector --cache /build-cache/python
```

Desktop authorizes through the current chat Person to issue a connector-purpose
code, then passes it to this CLI on stdin. The renderer never receives that code.
Task and permission UI forwards the original server IDs through Main/Work;
submission acknowledgement does not assert native execution success.
