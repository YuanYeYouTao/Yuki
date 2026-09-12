# Evidence worker and historical activation repair

Current patch: Yuki 3.8.2, schema 0051. No migration, model calls, personality changes,
history reset, embedding rebuild or recall-policy changes.

## Evidence compaction

An unhandled SQLAlchemy error could terminate the scheduler. Its old `start()` then
mistook a completed task for a live worker. This failure mode was reproduced; the exact
exception responsible for the production record gap on September 11 remains unknown.

The worker now isolates ordinary exceptions (including SQLAlchemy errors), preserves
cancellation, bounds lock acquisition plus each batch to 120 seconds, and retries through
the existing polling loop. `start()` can replace completed tasks; it never duplicates a live
task. Lock flags are cleared on cancellation/failure. No additional executor is introduced.

`running`, `last_success_at` and `last_error_category` describe the worker itself. The existing
Dream health field `compaction_last_error_category` projects `worker_not_running` when
enabled but dead, or the latest worker failure category. Public health fields are unchanged.
Dream's existing `running` still describes an active Dream run, not scheduler liveness.
Diagnostics never include exception messages, SQL parameters or memory content.

## Historical activation

The inspected production database and its pre-deployment backup both lacked activation
states for 814 active facts, all dating before August 25. Newly created facts had states.
This identifies a historical gap, not deletion by the latest attachment deployment; the
original migration/restore event responsible is not established.

The existing maintenance worker now fills missing states transactionally in bounded batches
(`memory_maintenance_batch_limit`, default 100), prioritizing active facts. Its normal interval
remains 300 seconds. Existing rows are never overwritten, including under duplicate runs.
Initialization follows the current initial-activation policy and uses the fact's original
`created_at`, so repair does not make an old fact artificially fresh. `last_recalled_at` stays
null, `recall_count` and `revision` start at zero. Historical usage is not inferred or replayed.
Facts, evidence and recall receipts are not changed by the repair itself. Read-only queries do
not become repair writes. Maintenance exceptions are isolated so a transient failure does not
permanently disable subsequent repair cycles.

After rollout, expect gradual repair, not an instantaneous change at startup: 814 active gaps
take nine successful batches with the default budget. Afterwards inactive historical gaps are
also filled. Used memories can then follow the existing authorized reinforcement path.

## Verification and rollout

Regression tests cover database-error recovery, lock timeout, cancellation, dead-task restart,
safe logging, bounded repair, rollback, preservation of existing state and fact data,
idempotency, original timestamps and successful reinforcement after repair.

This change does not itself deploy or repair production. Before separately authorized rollout,
retain the normal database/config/image backup procedure. Observe missing-state counts,
compaction progress and worker errors after rollout; do not equate container health with
completed memory work. Existing genuine usage must never be reset to force a clean report.
