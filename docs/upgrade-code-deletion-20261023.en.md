# Code cleanup and connector upgrade (2026-10-23)

[中文](upgrade-code-deletion-20261023.md) · [Execution record](operations/code-deletion-20261023.md)

This covers the B1–B6 source candidate. It is not a new formal release, a PyPI publication,
or a deployment receipt. The execution record identifies the tested commit and PR.

The connector is now `yuki-agent-connector 0.2.0`, using WSS protocol `2`. Upgrade the Bot
and connector together. Preserve configuration, device namespace, credentials and the original
SQLite journal. Unsupported versions are rejected; do not delete the journal or recreate IDs.

Native instances start and recover independently. Later readiness updates the existing catalog.
The connector sends one original durable event per commit ACK, while RPC responses remain
independent. Reconnection reuses original event/operation IDs. An explicit repository event list
remains one atomic transaction. The measured window reduces memory and writer/FIFO delay at
the cost of backlog throughput; see the workload and figures in the execution record.

Plugin Memory keeps its public method signatures. Calls within one event retain separate call
identities. Replays read original receipts; ambiguous historical event-wide receipts are retained
without inferred migration or replay. Producer labels do not establish explicit human intent.
Confidence reaches the actual fact and evidence. Updates create correction versions, and
`confidence=None` preserves the original value.

The SDK Fake follows Host results: `add.data.memory` contains the record and its
`person:<integer>` memory ID; `update.data.memory_id` identifies the new correction version;
`delete.data.memory_id` identifies the invalidated fact. Fixtures using UUIDs, the old add result
shape, or in-place corrections must change. Original versions and evidence remain available.

The following surfaces are intentionally removed, without aliases:

- Synthetic `yuki prompt inspect/compare` commands. Real model/runtime diagnostics remain.
- `ModelExecutionPriority.EXCLUSIVE`; foreground, durable background, maintenance,
  SELF quiet behavior and configured concurrency remain.
- `PromptTrust` and `PromptContribution(trust=...)`. Actual source labels, SOUL boundaries,
  message ordering, plugin trust metadata and approval remain.
- `CapabilityExposure`, unused scope metadata and `initial_exposure()`. MainAgentContract
  owns compiled immutable input schemas; current grants are projected and checked separately.
- Retired Host helpers and unused construction parameters listed in the execution record.

Host constructors for Chat, Social, Workspace, Emoji and Participation now require their actual
dependencies instead of later assignment. The duplicate `SocialService.workspace` alias is removed;
use `workspace_service.store`. External callers of these Host internals must migrate as well.

Workspace owners upgrade their independent manifest at opening. Artifacts retain original IDs,
bytes, hashes, revisions and provenance; old artifact expiry no longer hides originals. Publication,
reference protection, orphan GC, short-state expiry and the separate media cache lifetime remain.

New codec3 ordinary/cross-entry source proofs omit unrelated diagnostic privacy state.
Original codec2 fingerprints and readers remain unchanged. An unprovable old paid checkpoint
keeps its Work, budget and journal; it is not recertified with today's inputs or dispatched again.
Actual observation privacy and compact publication checks remain.

Frozen main-database migrations and production/user data are untouched. Generic SDK
`ON_FAILURE` and Plugin Main cold-replay semantics remain unchanged. Subscription configuration
failures require a configuration fix and explicit plugin restart.
