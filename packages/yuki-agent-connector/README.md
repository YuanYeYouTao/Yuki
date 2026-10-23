# Yuki Agent Connector

Independent Python 3.12 connector for an existing local Codex daemon, the official Claude
Agent SDK and generic ACP agents. It does not install the Yuki Bot, own business tasks,
mirror native transcripts or send chat messages itself.

Install the built wheel with Python 3.12, for example `uv tool install path/to/yuki_agent_connector-0.2.0-py3-none-any.whl`.
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
same CLI with the same default installation, without another runtime or credential copy.

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


### Connector 0.2.0 / wire protocol 2

Bot and Connector must be updated to a matching revision. The WebSocket hello now uses
`protocol_version: 2`; version 1 is rejected rather than treated as compatible. Device,
instance, native session, execution, operation IDs and the original journal remain unchanged.
Each native instance starts and recovers independently. A ready sibling can connect immediately;
later readiness is sent through the same connection's catalog update without replacing its owner.

One original durable event is the in-flight credit and commit/ACK unit. RPC responses keep using
the independent socket reader while that event waits for SQLite. The connector reads its original
journal by sequence and sends the stored JSON, then advances only after its ACK. Disconnects replay
only unacknowledged records; they never redispatch native operations. This bounds transport backlog
materialization at the cost of one commit/ACK round trip per event; it is not an execution limit.
Explicit server `receive_events([...])` calls still commit or roll back the whole supplied list.
