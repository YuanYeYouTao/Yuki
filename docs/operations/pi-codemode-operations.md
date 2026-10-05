# Pi / Code Mode compatibility and operations

This is the experiment branch's procedure, not a record of a production switch.
Production access, a second Bot, image publication, and deployment remain
unauthorized. The dedicated temporary Linux validation VM was explicitly
authorized on 2026-10-05; its VM and temporary caches must be deleted after the
authorized acceptance work. Current evidence is in
[the delivery record](../architecture/pi-codemode-delivery.md).

## Compatibility matrix

| Reader / producer | Stored facts | Permitted behavior and evidence |
| --- | --- | --- |
| Historical `755f7250`, head `0081` | Its own original records | Produce an isolated legitimate fixture, then run every normal migration through `0092` |
| Historical `8204b28e`, head `0091` | Its own original records | Same normal upgrade; original Work, journal, finite root budget, Social unknown, Manager IDs, plugin grants and automation cursor remain |
| New reader / producer, head `0092` | Legacy rows without invocation metadata | Read the original journal and original domain receipts; missing dispatch evidence stays legacy, never fabricated |
| New reader / producer | Versioned child intent / effect | Continue only the original owner, operation, generation, receipt and remaining budget |
| Old producer | Head `0092` with new child facts | Refused: its actual `init-db` cannot locate `0092`; the new downgrade guard refuses loss of versioned original invocation facts |
| New reader, changed worker or API | Old pending composition | Settle the original outer call as partial; do not load an incompatible VM or replay its business calls |
| New reader, lowered VM limits | Old pending dump with larger or absent recorded policy | Settle `code_engine_resource_policy_changed`, retaining original bytes, counters and child results |
| New reader, paired partial / completed | Already paired outer result | Read/reconcile only; never resume that VM or append another outer result |
| New reader, privacy deletion / interrupted GC | Old bytes without a live authorized reference | Refuse snapshot/artifact reads and finish GC; copying the bytes back grants no ownership |

Executable evidence: `tests/integration/test_pi_migration_chains.py`,
`test_codemode_resource_policy.py`, `test_codemode_backup_recovery.py`,
`test_codemode_manager_backup.py`,
`test_code_composition_restore.py`, and `test_invocation_crash_windows.py`.
Historical producers run in subprocesses with their exact archived sources;
they do not share a live Bot database. Downgrade does not replace the live
database with an older backup.

## Fixed distribution and Linux worker

```sh
uv sync --frozen --extra dev --python 3.12
scripts/build_monty_worker.sh /absolute/task-owned/build-directory
```

The common distribution builder pins Monty source, patch, Rust `1.96.0`, and
maturin `1.9.6`, builds the worker-only runtime and matching CPython 3.12 wheel,
and exports archive-verified dependency notices. Inspect `artifacts.json` and
`THIRD_PARTY_NOTICES.json` before configuring a distribution. `uv sync` removes
the unindexed wheel: install the local wheel after the last sync, as both
Docker build stages do. No sandbox executable is discovered through PATH.

Linux requires the digest-verified compiled launcher and worker installed as
root-owned, non-group/world-writable files under `/opt/yuki-monty`. The Host
must be unprivileged (images configure UID 10001; the dedicated native
validation used UID 501). The launcher accepts only the binding's literal `subprocess`
argument, clears environment, and starts `/usr/bin/bwrap` with separate user,
mount, PID, network, IPC and UTS namespaces, namespace UID/GID 65534, and no
capabilities. It mounts only the native worker, read-only runtime libraries,
proc/dev, an empty etc directory and transient tmp. It exposes no application
state, workspace, Manager socket, Docker socket, credential or network bridge.
There is no unrestricted Linux fallback if user namespaces or immutable files
are unavailable. Do not bypass a failure with `--privileged` or an unrestricted
worker command.

Docker's default seccomp profile was observed to refuse `unshare(CLONE_NEWUSER)`
with EPERM in the dedicated validation VM. Packaging the launcher and wheel
does not enable its sandbox under that profile. Leave Code Mode unconfigured
there; the verified deployment in this stage is the unprivileged native Linux
Host. No `--privileged`, `seccomp=unconfined`, extra capability or host Docker
security change was used. A container deployment must prove its own approved
isolation configuration before enabling Code Mode; the default image is not
evidence for that deployment.

Configure the actual artifact hashes, not these illustrative values:

```text
CODE_MODE_WORKER_PATH=/opt/yuki-monty/monty
CODE_MODE_WORKER_SHA256=<artifacts.json worker.sha256>
CODE_MODE_LAUNCHER_PATH=/opt/yuki-monty/monty-isolated
CODE_MODE_LAUNCHER_SHA256=<artifacts.json launcher.sha256>
CODE_MODE_MAX_WORKER_PROCESSES=2
CODE_MODE_FOREGROUND_RESERVED_PROCESSES=1
```

The 2/1 process defaults are a measured admission policy: an actual native
background worker holds one slot, a second background waits, and a foreground
native worker starts in the reserved slot; cancelling a waiter leaks neither
PID nor reservation. This establishes concurrency behavior, not a throughput
or production memory-capacity claim. Changing this global policy requires a
runtime restart. Independent Runner instances share the one runtime loop's
capacity. Existing persistent environments retain their own Manager limits.

VM defaults are 64 MiB, 10 seconds per feed, recursion 200 and 256 suspensions
per feed. Host defaults independently bound code/stdout at 64 KiB, inputs and
results at 256 KiB, snapshots at 8 MiB, cumulative suspensions at 1024, and the
wall watchdog at 30 seconds. The launcher additionally limits address space
to 512 MiB, CPU to 30 seconds, file writes to 4 MiB, file descriptors to 32 and
core dumps to zero. Host JSON traversal refuses oversize values before copying
an unbounded tree; cumulative stdout counts UTF-8 bytes across suspensions.
These are explicit ceilings; memory/capacity sizing on production hardware is
still unmeasured.

In an authorized dedicated Linux validation machine, build/install only these
artifacts and the local binding, then run as the unprivileged Host:

```sh
python scripts/verify_monty_isolation.py --output /tmp/monty-isolation.json
```

The verifier observes actual PID namespaces, UID maps, mountinfo, interfaces,
rlimits, finite engine execution, every observed descendant's shutdown, SIGKILL
of the binding owner, foreground/background admission and a SIGSTOP fault that
forces the real Host wall watchdog. A passing report is
required; the presence of a subprocess or an unexecuted Dockerfile is not
Linux isolation evidence. The native Debian 12/aarch64 report is
[the actual isolation evidence](../architecture/pi-codemode-evidence/p09-linux-isolation.json).
Two synthetic suspended native scripts used approximately 4 MiB high-water RSS each;
this measures that fixture, not production sizing. The stopped worker was
discarded by a 0.5-second watchdog in approximately 0.5 seconds. Every observed process
exited after normal cleanup, watchdog expiry and owner SIGKILL.
The dedicated validation image is `deploy/codemode/Dockerfile.validation`;
both that image and the application image were built. Their offline packaging probes
verified the fixed artifact hashes, binding import, original licenses and notices;
the application also migrated a new temporary DB normally through 0092 and verified
its built WebUI. That historical build also bundled the Pi reference license;
the current packaging contract includes Yuki, Monty and typeshed licenses, with
Pi recorded only as a design reference. The updated container recipes have not
been rebuilt since this clarification. See
[the packaging evidence](../architecture/pi-codemode-evidence/p09-container-packaging.json).
Run `scripts/verify_monty_packaging.py application|validation` inside those
network-disabled, read-only containers as UID 10001, with a private /tmp tmpfs
for the application role. This probe verifies the actual default namespace refusal;
it starts no Bot or Provider. No image has been published or deployed.

## Consistent backup scope

Use a new restricted destination explicitly approved for the actual deployment.
Do not grant any of this material to Monty. Before copying, stop Bot admission
through the existing runtime shutdown (`ActivationTasks.stop_admission/drain`),
wait for original Work recovery/release, and pause only the actual Bot and
Manager writers. Record all configured paths rather than assuming compose
defaults. Preserve gateway, proxy and custom services.

| Required material | Consistency and access |
| --- | --- |
| Bot SQLite database | SQLite backup API after writers stop; integrity and foreign-key checks |
| `work-protocol/` beside that DB | All live original private refs and immutable bytes, including code snapshots |
| Configured tool artifact directory | Original indexes, contents, ownership and deletion fences |
| Shared `/home/yuki` and `/workspace` filesystem images/trees | Preserve current content, file versions and immutable publication objects |
| Manager runtime mount and receipt DB | Original `jobs.sqlite3`, manifest DB, intent/run IDs, completion records and retained logs; pause its writers too |
| Actual configs, units, compose overrides and model profiles | Restricted administrator storage; keep credentials in their existing private mechanism, never in VM mounts or public reports |
| Deployment and artifact identities | Git/image/source hashes, migration head, worker/launcher/wheel/notice hashes |

On the copied Bot data, use:

```sh
python scripts/verify_work_backup.py /absolute/snapshot/qq_ai_bot.db /absolute/snapshot/data
```

This read-only verifier checks SQLite integrity/foreign keys and every live
private protocol/artifact file's size and digest. It refuses a missing or
deleting owned object. It does **not** validate Manager receipt semantics or
credential provisioning; preserve and check those with their original owners.
The synthetic protocol/artifact test also copies an example Manager evidence
file. The separate Manager backup test copies its actual `jobs.sqlite3`,
environment dispatch record, independent status/log files, workspace manifest
and files beside the Bot DB/private objects. A new Manager reconciles the
same original run, publishes one completion, and a new Bot inbox consumes it
idempotently without queuing another execution. The execd receipt is synthetic;
that test does not claim a real gVisor environment or production rollback.
Historical Manager identities are additionally checked by migration tests.
No private production backup was supplied or inspected.

## Authorized switch and failure procedure

1. Obtain the actual instance/build/deployment authorization. Record its sole
   SQLite writer, current version/head, configured paths and original unresolved
   operation IDs; prepare a new immutable distribution and notices beforehand.
2. Stop new admission and join original owner cleanup. Reconcile pending Social,
   Manager, plugin/MCP operations by their original persistent IDs. Missing
   upstream evidence remains unknown; never retry an entire program or send.
3. Take the consistent restricted backup above. Run the normal Alembic upgrade
   using the new distribution's `qq-ai-bot-cli init-db`, with the approved
   database configuration. Do not stamp past earlier revisions. Verify head
   `0092`, original facts, references and finite budgets before resuming.
4. Replace only the approved Bot distribution/configuration. Verify worker
   hashes and unprivileged isolation. Start one Bot; observe original Work
   continuation and existing health checks. Do not warm it by manufacturing
   model tasks or external messages.
5. On authority, duplicate-effect, budget, snapshot or delivery uncertainty,
   stop admission again. Preserve all new facts. Keep the new compatible reader
   for inspection/reconciliation; rebuild a corrected producer. An old binary
   may inspect only a separately copied compatible legacy dataset and cannot
   take over a live database containing new child facts.
6. If backup verification or migration fails, leave the producer stopped and
   retain both evidence and failure output. If Linux isolation fails, Code Mode
   stays unavailable. Do not clear queues, budgets, ownership or unknown states;
   do not overwrite new messages with an old DB. A paired partial returns to
   the original model for new planning rather than reopening the VM.

No production switch, rollback, true delivery or private backup recovery has
been run. Every future execution records its actual commands and evidence
separately from these instructions.
