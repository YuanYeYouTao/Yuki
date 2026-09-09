# Memory worker recovery — September 2026

Version stays 3.8.1; schema stays 0051. No history reset, fact deletion, embedding rebuild,
persona change, or recall-selection policy change is part of this patch.

## Findings and scope

- Production self-reflection was enabled but its scheduler task was no longer running. A run
  remained `processing` after its model invocation had already succeeded. The old worker caught
  only a narrow exception list; an unexpected exception could exit the scheduler and also prevent
  that scheduler's stale-run recovery. The original exception was not captured and was not
  reproduced; its exact underlying cause remains unconfirmed.
- The patch contains unexpected failures at batch and scheduler boundaries, retains cancellation,
  recovers durable partial results, and makes completed scheduler tasks restartable. Sanitized
  function/line diagnostics support investigating any recurrence without logging memory content.
- Dream allowed 12 clusters but only 12 requests, although each cluster can require a repair.
  Its default request budget is now 24. Unattempted work is deferred without source checkpointing
  instead of being counted as an executed failure. Existing environment overrides need updating.

## Validation

- Full deterministic suite: 800 passed. Related self-reflection/Dream tests: 59 passed after
  adding recovery-count assertions. Ruff and mypy passed; memory quality: 19/19 cases passed.
- Real-model probes used an isolated, SQLite-backup copy, not the live memory database. Twelve
  application-level model executions including structured repairs: three self-reflection,
  two consolidation, four Dream, three attribution. This counts executor calls, not any invisible
  upstream retries. No QQ connections or outbound messages were started by the probes.
- Self-reflection: two batches completed, two committed memories; one schema repair succeeded.
- Dream: two clusters, four requests, zero completed and two failed. Observed failures included
  absent tool output, missing/invalid source coverage and excessive recomposed length. The run
  reached a terminal state. Raising the budget does not fix invalid model output; guards remain
  intact and this is a known unresolved quality issue, not reported as a successful Dream test.
- Attribution: direct recall and a paraphrased recommendation correctly selected the supplied
  synthetic memory; an unrelated greeting selected none. This small check is not a measurement
  of real-conversation recall quality or hidden influence.

## Follow-up interpretation

The audited 48-hour sample had 19 used items among 404 evaluated items (4.70%), with 522 injected
items overall. This is not 19/522: unevaluated items are not confirmed unused. Three synthetic
checks do not prove the classifier is always correct. No metric threshold was relaxed to make
the number look better. Production observation must distinguish scheduler liveness, terminal
run status, committed results, evaluation coverage and actual use.

Deploy only a locally built image. Back up DB/WAL/SHM and configuration, retain the prior image,
replace only Bot, then verify its health, connected provider and stale-run recovery. Do not
restart SnowLuma or erase remembered data as a workaround.
