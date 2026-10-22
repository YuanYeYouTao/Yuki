"""Administrator-facing state machine for controlled historical memory rebuild."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass, replace
from typing import Any
from weakref import WeakSet

from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from yuki.config import Settings
from yuki.identity.authority import person_is_superuser
from yuki.memory.claim_processor import (
    MemoryClaimProcessor,
    MemoryClaimProcessResult,
    MemoryClaimResolution,
    MemoryProcessingContext,
)
from yuki.memory.eligibility import MemoryEventEligibilityPolicy
from yuki.memory.enums import (
    MemoryRebuildCommitStatus,
    MemoryRebuildExpiredClaimPolicy,
    MemoryRebuildReviewStatus,
    MemoryRebuildRunStatus,
    MemoryRebuildThirdPartyMode,
    MemoryResolutionAction,
    MemorySourceType,
)
from yuki.memory.event_extractor import MemoryEventExtractor
from yuki.memory.extraction import (
    EXTRACTION_PROMPT_VERSION,
    EXTRACTION_SCHEMA_VERSION,
    SOURCE_ADAPTATION_VERSION,
    MemoryClaim,
    source_event_fingerprint,
)
from yuki.memory.rebuild.models import (
    MemoryRebuildPlanStatistics,
    MemoryRebuildReviewEntry,
    MemoryRebuildRun,
    MemoryRebuildSelection,
)
from yuki.memory.rebuild.repository import MemoryRebuildRepository
from yuki.model_runtime.dispatch_guard import model_dispatch_guard
from yuki.model_runtime.models import ModelTask
from yuki.persistence.models import (
    ChatEventModel,
    MemoryJobModel,
    MemoryRebuildItemModel,
    MemoryRebuildProposalModel,
    MemoryRebuildRunModel,
)
from yuki.persistence.repositories import EventLedgerRepository
from yuki.persistence.unit_of_work import optional_session

SUBJECT_RESOLVER_VERSION = "1"
CLAIM_VALIDATOR_VERSION = "2"


def canonical_json(value: Any) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True, slots=True)
class PreparedRebuildPlan:
    snapshot: int
    statistics: MemoryRebuildPlanStatistics


async def prepare_rebuild_core(
    *,
    settings: Settings,
    ledger: EventLedgerRepository,
    selection: MemoryRebuildSelection,
) -> PreparedRebuildPlan:
    if not settings.memory_rebuild_enabled:
        raise RuntimeError("MEMORY_REBUILD_ENABLED is false")
    snapshot = await ledger.maximum_event_id()
    statistics = await ledger.count_rebuild_candidates(
        selection,
        snapshot_max_event_id=snapshot,
    )
    return PreparedRebuildPlan(snapshot, statistics)


async def plan_rebuild_core(
    *,
    settings: Settings,
    repository: MemoryRebuildRepository,
    ledger: EventLedgerRepository,
    selection: MemoryRebuildSelection,
    actor_user_id: str,
    model_name: str | None = None,
    session: AsyncSession | None = None,
    prepared: PreparedRebuildPlan | None = None,
) -> MemoryRebuildRun:
    if prepared is None:
        if session is not None:
            raise ValueError("rebuild plan must be prepared before opening a writer")
        prepared = await prepare_rebuild_core(settings=settings, ledger=ledger, selection=selection)
    selection_json = canonical_json(selection)
    return await repository.create_run(
        selection=selection,
        selection_json=selection_json,
        selection_hash=hashlib.sha256(selection_json.encode()).hexdigest(),
        snapshot_max_event_id=prepared.snapshot,
        fingerprint=extraction_fingerprint(settings, model_name=model_name),
        statistics=prepared.statistics,
        actor_user_id=actor_user_id,
        session=session,
    )


async def start_rebuild_core(
    repository: MemoryRebuildRepository,
    run_id: str,
    *,
    settings: Settings,
    session: AsyncSession | None = None,
) -> MemoryRebuildRun:
    if not settings.memory_rebuild_enabled:
        raise RuntimeError("MEMORY_REBUILD_ENABLED is false")
    if await repository.executing_count(session=session):
        raise RuntimeError("another memory rebuild run is executing")
    changed = await repository.transition(
        run_id,
        expected={MemoryRebuildRunStatus.PLANNED},
        status=MemoryRebuildRunStatus.EXTRACTING,
        session=session,
    )
    if not changed:
        raise ValueError("run is not in planned state")
    run = await repository.get_run(run_id, session=session)
    if run is None:
        raise ValueError("memory rebuild run not found")
    return run


async def cancel_rebuild_core(
    repository: MemoryRebuildRepository,
    run_id: str,
    *,
    session: AsyncSession | None = None,
) -> MemoryRebuildRun:
    run = await repository.get_run(run_id, session=session)
    if run is None:
        raise ValueError("memory rebuild run not found")
    if run.status in {
        MemoryRebuildRunStatus.COMPLETED,
        MemoryRebuildRunStatus.CANCELLED,
    }:
        return run
    changed = await repository.transition(
        run_id,
        expected={run.status},
        status=MemoryRebuildRunStatus.CANCELLED,
        session=session,
    )
    if not changed:
        raise RuntimeError("memory rebuild state changed concurrently")
    current = await repository.get_run(run_id, session=session)
    if current is None:
        raise ValueError("memory rebuild run not found")
    return current


def extraction_fingerprint(settings: Settings, *, model_name: str | None = None) -> str:
    payload = {
        "task": ModelTask.MEMORY_EXTRACTION.value,
        "model": model_name or settings.llm_model,
        "prompt": EXTRACTION_PROMPT_VERSION,
        "schema": EXTRACTION_SCHEMA_VERSION,
        "subject_resolver": SUBJECT_RESOLVER_VERSION,
        "claim_validator": CLAIM_VALIDATOR_VERSION,
        "source_adaptation": SOURCE_ADAPTATION_VERSION,
    }
    return hashlib.sha256(canonical_json(payload).encode()).hexdigest()


async def manage_rebuild_core(
    repository: MemoryRebuildRepository,
    run_id: str,
    *,
    action: str,
    settings: Settings,
    model_name: str,
    actor_user_id: str,
    proposal_ids: tuple[int, ...] | None = None,
    session: AsyncSession | None = None,
) -> MemoryRebuildRun:
    """One state machine for trusted CLI/Control, under their existing authority."""
    run = await repository.get_run(run_id, session=session)
    if run is None:
        raise ValueError("memory rebuild run not found")
    if action in {"approve", "reject"}:
        if run.status not in {
            MemoryRebuildRunStatus.REVIEW,
            MemoryRebuildRunStatus.COMMIT_PAUSED,
            MemoryRebuildRunStatus.EXTRACTION_PAUSED,
            MemoryRebuildRunStatus.FAILED,
        }:
            raise ValueError("run is not ready for review")
        await repository.set_review(
            run_id,
            proposal_ids=proposal_ids,
            status=MemoryRebuildReviewStatus.APPROVED
            if action == "approve"
            else MemoryRebuildReviewStatus.REJECTED,
            actor_user_id=actor_user_id,
            session=session,
        )
    else:
        if action == "pause":
            target = {
                MemoryRebuildRunStatus.EXTRACTING: MemoryRebuildRunStatus.EXTRACTION_PAUSED,
                MemoryRebuildRunStatus.COMMITTING: MemoryRebuildRunStatus.COMMIT_PAUSED,
            }.get(run.status)
        elif action == "resume":
            if not settings.memory_rebuild_enabled:
                raise RuntimeError("MEMORY_REBUILD_ENABLED is false")
            target = {
                MemoryRebuildRunStatus.EXTRACTION_PAUSED: MemoryRebuildRunStatus.EXTRACTING,
                MemoryRebuildRunStatus.COMMIT_PAUSED: MemoryRebuildRunStatus.COMMITTING,
            }.get(run.status)
        elif action == "retry":
            if run.status not in {
                MemoryRebuildRunStatus.FAILED,
                MemoryRebuildRunStatus.REVIEW,
                MemoryRebuildRunStatus.COMMIT_PAUSED,
                MemoryRebuildRunStatus.EXTRACTION_PAUSED,
            }:
                raise ValueError("run is not ready for retry")
            target = await repository.reset_failed(run_id, session=session)
        elif action == "commit":
            if not settings.memory_rebuild_enabled:
                raise RuntimeError("MEMORY_REBUILD_ENABLED is false")
            if run.status not in {
                MemoryRebuildRunStatus.REVIEW,
                MemoryRebuildRunStatus.COMMIT_PAUSED,
                MemoryRebuildRunStatus.EXTRACTION_PAUSED,
            }:
                raise ValueError("run is not ready for commit")
            target = MemoryRebuildRunStatus.COMMITTING
        else:
            raise ValueError("unsupported rebuild action")
        if target is None or not await repository.transition(
            run_id, expected={run.status}, status=target, session=session
        ):
            raise RuntimeError("memory rebuild state changed concurrently")
    loaded = await repository.get_run(run_id, session=session)
    if loaded is None:
        raise ValueError("memory rebuild run not found")
    return loaded


class MemoryRebuildService:
    """All command, tool, and worker entry points share this state machine."""

    def __init__(
        self,
        *,
        settings: Settings,
        repository: MemoryRebuildRepository,
        ledger: EventLedgerRepository,
        extractor: MemoryEventExtractor,
        processor: MemoryClaimProcessor,
        eligibility: MemoryEventEligibilityPolicy | None = None,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.ledger = ledger
        self.extractor = extractor
        self.processor = processor
        self.eligibility = eligibility or MemoryEventEligibilityPolicy()
        self._active_in_flight_calls = 0
        self._in_flight_tasks: dict[str, set[asyncio.Task[Any]]] = {}
        self._cancelled_tasks: WeakSet[asyncio.Task[Any]] = WeakSet()

    @property
    def active_in_flight_calls(self) -> int:
        return self._active_in_flight_calls

    async def _authorize(self, actor_user_id: str, *, session: AsyncSession | None = None) -> None:
        async with optional_session(self.repository.database, session, write=False) as active:
            allowed = await person_is_superuser(active, actor_user_id)
        if not allowed:
            raise PermissionError("memory rebuild requires the current real superuser")

    async def _run_authorized(self, run_id: str, *, session: AsyncSession) -> bool:
        creator = await session.scalar(
            select(MemoryRebuildRunModel.created_by_user_id).where(
                MemoryRebuildRunModel.public_id == run_id
            )
        )
        return await person_is_superuser(session, creator)

    async def _pause_unauthorized_run(self, run: MemoryRebuildRun) -> None:
        await self.repository.transition(
            run.public_id,
            expected={run.status},
            status=MemoryRebuildRunStatus.EXTRACTION_PAUSED
            if run.status is MemoryRebuildRunStatus.EXTRACTING
            else MemoryRebuildRunStatus.COMMIT_PAUSED,
            error_category="initiator_permission_denied",
        )

    async def _continue_run(self, run: MemoryRebuildRun) -> bool:
        async with self.repository.database.read_session() as session:
            allowed = await self._run_authorized(run.public_id, session=session)
        if not allowed:
            await self._pause_unauthorized_run(run)
        return allowed

    def _available(self) -> None:
        if not self.settings.memory_rebuild_enabled:
            raise RuntimeError("MEMORY_REBUILD_ENABLED is false")

    async def plan(
        self,
        selection: MemoryRebuildSelection,
        *,
        actor_user_id: str,
        authorize: bool = True,
        session: AsyncSession | None = None,
        prepared: PreparedRebuildPlan | None = None,
    ) -> MemoryRebuildRun:
        if authorize:
            await self._authorize(actor_user_id, session=session)
        return await plan_rebuild_core(
            settings=self.settings,
            repository=self.repository,
            ledger=self.ledger,
            selection=selection,
            actor_user_id=actor_user_id,
            model_name=self.extractor.model_name,
            session=session,
            prepared=prepared,
        )

    async def list(self, *, actor_user_id: str) -> tuple[MemoryRebuildRun, ...]:
        await self._authorize(actor_user_id)
        return await self.repository.list_runs()

    async def status(self, run_id: str, *, actor_user_id: str) -> dict[str, Any]:
        await self._authorize(actor_user_id)
        run = await self._require(run_id)
        return {
            "run": run.model_dump(mode="json"),
            "statistics": await self.repository.statistics(run_id),
            "pending_review": await self.repository.pending_review_count(run_id),
            "pending_commit": await self.repository.remaining_commit_count(run_id),
        }

    async def start(
        self,
        run_id: str,
        *,
        actor_user_id: str,
        authorize: bool = True,
        session: AsyncSession | None = None,
    ) -> MemoryRebuildRun:
        if authorize:
            await self._authorize(actor_user_id, session=session)
        self._available()
        if await self.repository.executing_count(session=session):
            raise RuntimeError("another memory rebuild run is executing")
        changed = await self.repository.transition(
            run_id,
            expected={MemoryRebuildRunStatus.PLANNED},
            status=MemoryRebuildRunStatus.EXTRACTING,
            session=session,
        )
        if not changed:
            raise ValueError("run is not in planned state")
        return await self._require(run_id, session=session)

    async def pause(self, run_id: str, *, actor_user_id: str) -> MemoryRebuildRun:
        await self._authorize(actor_user_id)
        return await manage_rebuild_core(
            self.repository,
            run_id,
            action="pause",
            settings=self.settings,
            model_name=self.extractor.model_name,
            actor_user_id=actor_user_id,
        )

    async def resume(self, run_id: str, *, actor_user_id: str) -> MemoryRebuildRun:
        await self._authorize(actor_user_id)
        return await manage_rebuild_core(
            self.repository,
            run_id,
            action="resume",
            settings=self.settings,
            model_name=self.extractor.model_name,
            actor_user_id=actor_user_id,
        )

    async def cancel(
        self,
        run_id: str,
        *,
        actor_user_id: str,
        authorize: bool = True,
        session: AsyncSession | None = None,
    ) -> MemoryRebuildRun:
        if authorize:
            await self._authorize(actor_user_id, session=session)
        run = await self._require(run_id, session=session)
        if run.status in {
            MemoryRebuildRunStatus.COMPLETED,
            MemoryRebuildRunStatus.CANCELLED,
        }:
            return run
        changed = await self.repository.transition(
            run_id,
            expected={run.status},
            status=MemoryRebuildRunStatus.CANCELLED,
            session=session,
        )
        if not changed:
            raise RuntimeError("memory rebuild state changed concurrently")
        for task in tuple(self._in_flight_tasks.get(run_id, ())):
            self._cancelled_tasks.add(task)
            task.cancel()
        return await self._require(run_id, session=session)

    async def review(
        self,
        run_id: str,
        *,
        actor_user_id: str,
        page: int = 1,
    ) -> tuple[MemoryRebuildReviewEntry, ...]:
        await self._authorize(actor_user_id)
        if page <= 0:
            raise ValueError("page must be positive")
        await self._require(run_id)
        size = self.settings.memory_rebuild_review_page_size
        rows = await self.repository.review_rows(run_id, offset=(page - 1) * size, limit=size)
        result: list[MemoryRebuildReviewEntry] = []
        for proposal, event in rows:
            claim = MemoryClaim.model_validate_json(proposal.claim_json)
            result.append(
                MemoryRebuildReviewEntry(
                    proposal_id=proposal.id,
                    event_id=event.id,
                    event_time=event.occurred_at,
                    sender_user_id=event.sender_user_id,
                    group_id=event.group_id,
                    subject_user_id=proposal.subject_user_id,
                    scope_type=proposal.scope_type,
                    operation=proposal.operation,
                    kind=proposal.kind,
                    memory_key=claim.memory_key,
                    content=claim.content,
                    confidence=proposal.confidence,
                    authority=proposal.authority,
                    valid_from=claim.valid_from,
                    valid_until=claim.valid_until,
                    source_excerpt=claim.evidence_quote[
                        : self.settings.memory_rebuild_source_excerpt_characters
                    ],
                    review_status=proposal.review_status,
                )
            )
        return tuple(result)

    async def set_review(
        self,
        run_id: str,
        selector: str,
        *,
        approved: bool,
        actor_user_id: str,
    ) -> int:
        await self._authorize(actor_user_id)
        run = await self._require(run_id)
        if run.status not in {
            MemoryRebuildRunStatus.REVIEW,
            MemoryRebuildRunStatus.COMMIT_PAUSED,
            MemoryRebuildRunStatus.EXTRACTION_PAUSED,
            MemoryRebuildRunStatus.FAILED,
        }:
            raise ValueError("run is not ready for review")
        proposal_ids: tuple[int, ...] | None
        if selector.casefold() == "all":
            proposal_ids = None
        elif selector.lstrip().startswith("{"):
            filters = json.loads(selector)
            if not isinstance(filters, dict):
                raise ValueError("review filter must be an object")
            proposal_ids = await self.repository.proposal_ids_for_filter(run_id, filters)
        else:
            try:
                proposal_ids = tuple(int(item) for item in selector.split(",") if item.strip())
            except ValueError as exc:
                raise ValueError("proposal ids must be comma-separated integers") from exc
            if not proposal_ids:
                raise ValueError("proposal selector is empty")
        changed = await self.repository.set_review(
            run_id,
            proposal_ids=proposal_ids,
            status=MemoryRebuildReviewStatus.APPROVED
            if approved
            else MemoryRebuildReviewStatus.REJECTED,
            actor_user_id=actor_user_id,
        )
        return changed

    async def commit(self, run_id: str, *, actor_user_id: str) -> MemoryRebuildRun:
        await self._authorize(actor_user_id)
        return await manage_rebuild_core(
            self.repository,
            run_id,
            action="commit",
            settings=self.settings,
            model_name=self.extractor.model_name,
            actor_user_id=actor_user_id,
        )

    async def retry(self, run_id: str, *, actor_user_id: str) -> MemoryRebuildRun:
        await self._authorize(actor_user_id)
        return await manage_rebuild_core(
            self.repository,
            run_id,
            action="retry",
            settings=self.settings,
            model_name=self.extractor.model_name,
            actor_user_id=actor_user_id,
        )

    async def purge(self, run_id: str, *, actor_user_id: str) -> bool:
        await self._authorize(actor_user_id)
        return await self.repository.purge(run_id)

    async def process_extraction_once(self, run: MemoryRebuildRun) -> int:
        if run.status is not MemoryRebuildRunStatus.EXTRACTING:
            return 0
        if not await self._continue_run(run):
            return 0
        pending = await self.repository.pending_extraction_events(
            run.public_id, limit=self.settings.memory_rebuild_scan_batch_size
        )
        if pending:
            semaphore = asyncio.Semaphore(self.settings.memory_rebuild_extraction_concurrency)
            results = await asyncio.gather(
                *(self._extract_one(run, event, semaphore) for event in pending)
            )
            return sum(state == "processed" for state in results)
        scanned = await self.repository.item_count(run.public_id)
        if run.selection.maximum_events is not None and scanned >= run.selection.maximum_events:
            await self.repository.transition(
                run.public_id,
                expected={MemoryRebuildRunStatus.EXTRACTING},
                status=MemoryRebuildRunStatus.REVIEW,
            )
            return 0
        page_limit = min(
            self.settings.memory_rebuild_scan_batch_size,
            (run.selection.maximum_events - scanned)
            if run.selection.maximum_events is not None
            else self.settings.memory_rebuild_scan_batch_size,
        )
        checkpoint_time, checkpoint_event_id = await self.repository.scan_checkpoint(run.public_id)
        rows = await self.ledger.list_rebuild_candidates(
            run.selection,
            snapshot_max_event_id=run.snapshot_max_event_id,
            after_occurred_at=checkpoint_time,
            after_event_id=checkpoint_event_id,
            limit=page_limit,
        )
        if not rows:
            await self.repository.transition(
                run.public_id,
                expected={MemoryRebuildRunStatus.EXTRACTING},
                status=MemoryRebuildRunStatus.REVIEW,
            )
            return 0
        semaphore = asyncio.Semaphore(self.settings.memory_rebuild_extraction_concurrency)
        results = await asyncio.gather(
            *(self._extract_one(run, source_event, semaphore) for source_event in rows)
        )
        for source_event, state in zip(rows, results, strict=True):
            if state not in {"processed", "complete", "failed"}:
                break
            await self.repository.update_scan_checkpoint(run.public_id, source_event)
        return sum(state == "processed" for state in results)

    async def _extract_one(
        self,
        run: MemoryRebuildRun,
        source_event: Any,
        semaphore: asyncio.Semaphore,
    ) -> str:
        async with semaphore:
            current = await self._require(run.public_id)
            if current.status is not MemoryRebuildRunStatus.EXTRACTING:
                return "deferred"
            if not await self._continue_run(current):
                return "deferred"
            event = await self._adapt_event(source_event, run.selection.third_party_mode)
            event_hash = source_event_fingerprint(event)
            item_id, acquired, item_status = await self.repository.ensure_item(
                run.public_id,
                event_id=source_event.id,
                source_event_hash=event_hash,
            )
            if not acquired:
                if item_status in {"staged", "no_claims", "skipped", "committed", "failed"}:
                    return "complete"
                return "deferred"
            current = await self._require(run.public_id)
            if current.status is not MemoryRebuildRunStatus.EXTRACTING:
                await self.repository.defer_item(item_id, category="run_not_extracting")
                return "deferred"
            try:
                context = await self.ledger.list_scope_before(
                    source_event.scope,
                    before_event_id=source_event.id,
                    limit=self.settings.memory_rebuild_context_event_limit,
                    message_only=True,
                )
                if not await self._continue_run(current):
                    await self.repository.defer_item(
                        item_id, category="initiator_permission_denied"
                    )
                    return "deferred"
                task = asyncio.current_task()
                if task is not None:
                    self._in_flight_tasks.setdefault(run.public_id, set()).add(task)
                self._active_in_flight_calls += 1
                try:

                    async def authorize_dispatch() -> None:
                        active = await self._require(run.public_id)
                        if active.status is not MemoryRebuildRunStatus.EXTRACTING:
                            raise PermissionError("rebuild_run_not_extracting")
                        if not await self._continue_run(active):
                            raise PermissionError("rebuild_initiator_permission_denied")

                    with model_dispatch_guard(authorize_dispatch):
                        extracted = await self.extractor.extract(event, context=context)
                finally:
                    self._active_in_flight_calls -= 1
                    if task is not None:
                        tasks = self._in_flight_tasks.get(run.public_id)
                        if tasks is not None:
                            tasks.discard(task)
                            if not tasks:
                                self._in_flight_tasks.pop(run.public_id, None)
                staged: list[tuple[MemoryClaim, Any, str]] = []
                for raw_claim in extracted.output.claims:
                    claim = raw_claim
                    claim = claim.model_copy(update={"source_type": MemorySourceType.REBUILD})
                    validated = self.processor.validate(
                        claim,
                        event,
                        subject_context=extracted.subject_context,
                    )
                    if validated is None:
                        continue
                    claim_json = canonical_json(claim)
                    claim_hash = hashlib.sha256(f"{event_hash}:{claim_json}".encode()).hexdigest()
                    staged.append((claim, validated, claim_hash))
                await self.repository.stage_claims(
                    run.public_id,
                    item_id=item_id,
                    event_id=source_event.id,
                    claims=tuple(staged),
                    input_tokens=extracted.input_tokens,
                    output_tokens=extracted.output_tokens,
                    latency_seconds=extracted.latency_seconds,
                )
            except asyncio.CancelledError:
                await self.repository.defer_item(item_id, category="cancelled")
                if asyncio.current_task() in self._cancelled_tasks:
                    return "deferred"
                raise
            except PermissionError:
                await self.repository.defer_item(item_id, category="initiator_permission_denied")
                return "deferred"
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                await self.repository.fail_item(
                    item_id,
                    type(exc).__name__,
                    retry_initial_seconds=self.settings.memory_rebuild_retry_initial_seconds,
                )
                return "deferred"
            return "processed"

    async def process_commit_once(self, run: MemoryRebuildRun) -> int:
        if run.status is not MemoryRebuildRunStatus.COMMITTING:
            return 0
        if not await self._continue_run(run):
            return 0
        rows = await self.repository.next_commit_rows(
            run.public_id,
            limit=self.settings.memory_rebuild_commit_batch_size,
        )
        processed = 0
        affected_item_ids: set[int] = set()
        for proposal, item, stored_event in rows:
            current = await self._require(run.public_id)
            if current.status is not MemoryRebuildRunStatus.COMMITTING:
                break
            if not await self._continue_run(current):
                break
            affected_item_ids.add(item.id)
            event = await self.repository.get_event(stored_event.id)
            if event is not None:
                event = await self._adapt_event(event, run.selection.third_party_mode)
            if event is None or source_event_fingerprint(event) != item.source_event_hash:
                await self.repository.finish_proposal(
                    proposal.id,
                    status=MemoryRebuildCommitStatus.SKIPPED,
                    fact_id=None,
                    action="noop",
                    reason_code="source_event_changed",
                )
                processed += 1
                continue
            receipt = await self.repository.receipt_status(event.id)
            if receipt in {"done", "pending", "processing"} or (
                receipt == "failed" and not run.selection.include_failed_live_jobs
            ):
                reason = (
                    "already_processed"
                    if receipt == "done"
                    else (
                        "live_job_active"
                        if receipt in {"pending", "processing"}
                        else "failed_live_job_not_selected"
                    )
                )
                await self.repository.finish_proposal(
                    proposal.id,
                    status=MemoryRebuildCommitStatus.SKIPPED,
                    fact_id=None,
                    action="noop",
                    reason_code=reason,
                )
                processed += 1
                continue
            if not self.eligibility.is_eligible(event):
                await self.repository.finish_proposal(
                    proposal.id,
                    status=MemoryRebuildCommitStatus.SKIPPED,
                    fact_id=None,
                    action="noop",
                    reason_code="event_ineligible_at_commit",
                )
                processed += 1
                continue
            claim = MemoryClaim.model_validate_json(proposal.claim_json)
            subject_context = await self.extractor.subject_context(event)
            validated = self.processor.validate(
                claim,
                event,
                subject_context=subject_context,
            )
            if (
                validated is None
                or (
                    validated.fact.canonical_subject_person_id
                    != proposal.canonical_subject_person_id
                    if proposal.canonical_subject_person_id is not None
                    else validated.fact.subject_user_id != proposal.subject_user_id
                )
                or (
                    validated.fact.canonical_subject_space_id != proposal.canonical_subject_space_id
                    if proposal.canonical_subject_space_id is not None
                    else validated.fact.group_id != proposal.group_id
                )
                or validated.fact.scope_type.value != proposal.scope_type
            ):
                await self.repository.finish_proposal(
                    proposal.id,
                    status=MemoryRebuildCommitStatus.SKIPPED,
                    fact_id=None,
                    action="noop",
                    reason_code="commit_revalidation_failed",
                )
                processed += 1
                continue
            expired = bool(
                validated.fact.valid_until is not None
                and validated.fact.valid_until <= run.snapshot_created_at
            )
            if expired and (
                run.selection.expired_claim_policy is MemoryRebuildExpiredClaimPolicy.SKIP
            ):
                await self.repository.finish_proposal(
                    proposal.id,
                    status=MemoryRebuildCommitStatus.SKIPPED,
                    fact_id=None,
                    action="noop",
                    reason_code="historical_claim_expired",
                )
                processed += 1
                continue
            try:
                resolution = self.processor.resolve(
                    validated,
                    MemoryProcessingContext(
                        event=event,
                        force_expired_invalidated=expired,
                    ),
                )

                async with self.repository.database.read_session() as reader:
                    await reader.execute(text("BEGIN"))
                    evidence_preparation = await self.processor._facts.prepare_write(
                        (),
                        targets=(validated.fact,),
                        session=reader,
                    )
                    raw_run = await reader.get(MemoryRebuildRunModel, proposal.run_id)
                    if (
                        raw_run is None
                        or MemoryRebuildSelection.model_validate_json(raw_run.selection_json)
                        != run.selection
                    ):
                        raise ValueError("rebuild_selection_changed")
                    run_values = {
                        name: getattr(raw_run, name)
                        for name in (
                            "id",
                            "selection_json",
                            "selection_hash",
                            "snapshot_max_event_id",
                            "snapshot_created_at",
                            "extraction_fingerprint",
                        )
                    }
                    event_values = {
                        name: getattr(stored_event, name)
                        for name in (
                            "id",
                            "bot_user_id",
                            "platform_message_id",
                            "scope_type",
                            "sender_user_id",
                            "group_id",
                            "private_peer_user_id",
                            "direction",
                            "content",
                            "audio_transcript",
                            "segments_json",
                            "reply_to_message_id",
                            "occurred_at",
                            "observed_at",
                            "origin",
                            "canonical_event_id",
                            "canonical_conversation_id",
                            "author_kind",
                            "author_person_id",
                            "ingress_presence_id",
                            "suppression_status",
                        )
                    }
                    proposal_values = {
                        name: getattr(proposal, name)
                        for name in (
                            "id",
                            "run_id",
                            "item_id",
                            "event_id",
                            "claim_index",
                            "claim_json",
                            "claim_hash",
                            "scope_type",
                            "canonical_subject_person_id",
                            "canonical_subject_space_id",
                            "subject_user_id",
                            "group_id",
                        )
                    }
                    item_values = {
                        name: getattr(item, name)
                        for name in ("id", "run_id", "event_id", "source_event_hash", "status")
                    }
                from yuki.persistence.repository_helpers import _event_record

                original_event = _event_record(stored_event)
                recorded_people = bool(original_event.mentioned_person_ids) or any(
                    part.get("type") == "yuki_context"
                    and isinstance(data := part.get("data"), dict)
                    and "mentioned_person_ids" in data
                    for part in original_event.segments
                )
                selection_conditions = self.ledger._rebuild_conditions(
                    run.selection,
                    snapshot_max_event_id=run.snapshot_max_event_id,
                    eligible_only=True,
                )

                async def commit(
                    owned: AsyncSession,
                    resolution: MemoryClaimResolution = resolution,
                    proposal_id: int = proposal.id,
                    run_values: dict[str, Any] = run_values,
                    proposal_values: dict[str, Any] = proposal_values,
                    item_values: dict[str, Any] = item_values,
                    event_values: dict[str, Any] = event_values,
                    selection_conditions: tuple[Any, ...] = selection_conditions,
                    event: Any = event,
                    validated: Any = validated,
                    stored_event: Any = stored_event,
                    recorded_people: bool = recorded_people,
                ) -> MemoryClaimProcessResult:
                    original = await owned.get(MemoryRebuildProposalModel, proposal_id)
                    if original is None:
                        raise ValueError("rebuild_proposal_removed")
                    if original.commit_status == MemoryRebuildCommitStatus.COMMITTED.value:
                        if original.actual_action is None or original.actual_reason_code is None:
                            raise RuntimeError("committed_proposal_receipt_incomplete")
                        return MemoryClaimProcessResult(
                            original.actual_fact_id,
                            MemoryResolutionAction(original.actual_action),
                            original.actual_reason_code,
                        )
                    current_run = await owned.get(MemoryRebuildRunModel, original.run_id)
                    if (
                        current_run is None
                        or current_run.status != MemoryRebuildRunStatus.COMMITTING.value
                        or original.review_status != MemoryRebuildReviewStatus.APPROVED.value
                        or original.commit_status != MemoryRebuildCommitStatus.PENDING.value
                    ):
                        raise ValueError("rebuild_proposal_authority_changed")
                    for model, values in (
                        (MemoryRebuildRunModel, run_values),
                        (MemoryRebuildProposalModel, proposal_values),
                        (MemoryRebuildItemModel, item_values),
                    ):
                        if (
                            await owned.scalar(
                                select(model.id).where(
                                    *(getattr(model, key) == value for key, value in values.items())
                                )
                            )
                            is None
                        ):
                            raise ValueError("rebuild_preparation_changed")
                    if (
                        await owned.scalar(
                            select(ChatEventModel.id).where(
                                *(
                                    getattr(ChatEventModel, key) == value
                                    for key, value in event_values.items()
                                ),
                                *selection_conditions,
                            )
                        )
                        is None
                    ):
                        raise ValueError("rebuild_source_or_selection_changed")
                    live_status = await owned.scalar(
                        select(MemoryJobModel.status).where(MemoryJobModel.event_id == event.id)
                    )
                    if live_status in {"done", "pending", "processing"} or (
                        live_status == "failed" and not run.selection.include_failed_live_jobs
                    ):
                        raise ValueError("rebuild_live_job_changed")
                    # A recorded/historical mention keeps the Person who owned it
                    # at ingress; today's membership or account owner cannot replace it.
                    from yuki.identity.db_models import PresenceModel
                    from yuki.identity.ownership_interval import historical_person_ids
                    from yuki.identity.person_identity import (
                        person_family_condition,
                        same_person_id,
                    )
                    from yuki.memory.partition import resolve_canonical_memory_partition_for_event
                    from yuki.persistence.models import MembershipModel

                    if event.mentioned_person_ids and not recorded_people:
                        presence = await owned.get(PresenceModel, event.ingress_presence_id)
                        people = (
                            await historical_person_ids(
                                owned,
                                presence.platform,
                                event.mentioned_user_ids,
                                stored_event.observed_at,
                            )
                            if presence is not None
                            else {}
                        )
                        if (
                            tuple(
                                dict.fromkeys(
                                    people[account]
                                    for account in event.mentioned_user_ids
                                    if account in people
                                )
                            )
                            != event.mentioned_person_ids
                        ):
                            raise ValueError("rebuild_historical_subject_changed")
                    owners = await self.processor._facts.requested_target_owners(
                        validated.fact, session=owned
                    )
                    source_partition = await resolve_canonical_memory_partition_for_event(
                        owned, event
                    )
                    if (
                        owners.subject_space_id
                        and owners.subject_space_id != source_partition.space_id
                    ):
                        raise ValueError("rebuild_target_changed")
                    if owners.subject_person_id:
                        is_speaker = bool(
                            event.author_person_id
                            and await same_person_id(
                                owned, owners.subject_person_id, event.author_person_id
                            )
                        )
                        is_member = bool(
                            source_partition.space_id
                            and await owned.scalar(
                                select(MembershipModel.canonical_person_id).where(
                                    person_family_condition(
                                        MembershipModel.canonical_person_id,
                                        owners.subject_person_id,
                                    ),
                                    MembershipModel.canonical_space_id == source_partition.space_id,
                                )
                            )
                        )
                        cited = any(
                            [
                                await same_person_id(owned, owners.subject_person_id, person_id)
                                for person_id in event.mentioned_person_ids
                            ]
                        )
                        if event.reply_to_event_id is not None:
                            reply = await owned.get(ChatEventModel, event.reply_to_event_id)
                            cited = cited or bool(
                                reply is not None
                                and reply.author_person_id
                                and await same_person_id(
                                    owned, owners.subject_person_id, reply.author_person_id
                                )
                            )
                        if not is_speaker and not is_member and not cited:
                            raise ValueError("rebuild_target_changed")
                    if not await self._run_authorized(run.public_id, session=owned):
                        raise PermissionError("rebuild_initiator_permission_denied")
                    result = await self.processor.apply_resolution(resolution, session=owned)
                    # The existing proposal is the durable completion owner. Facts
                    # and this receipt commit together, before disposable usage.
                    await self.repository.finish_proposal(
                        proposal_id,
                        status=MemoryRebuildCommitStatus.COMMITTED,
                        fact_id=result.fact_id,
                        action=result.action.value,
                        reason_code=result.reason_code,
                        session=owned,
                    )
                    return result

                try:
                    async with self.processor._facts.repository.write_session(
                        evidence_preparation
                    ) as writer:
                        result = await commit(writer)
                except Exception:
                    # The proposal is atomic with the fact. Read it before
                    # classifying failure; an unknown outcome does not grant replay authority.
                    commit_receipt = await self.repository.proposal_result(proposal.id)
                    if (
                        commit_receipt
                        and commit_receipt.commit_status
                        == MemoryRebuildCommitStatus.COMMITTED.value
                    ):
                        if (
                            commit_receipt.actual_action is None
                            or commit_receipt.actual_reason_code is None
                        ):
                            raise RuntimeError("committed_proposal_receipt_incomplete") from None
                        result = MemoryClaimProcessResult(
                            commit_receipt.actual_fact_id,
                            MemoryResolutionAction(commit_receipt.actual_action),
                            commit_receipt.actual_reason_code,
                        )
                    else:
                        raise
            except asyncio.CancelledError:
                raise
            except PermissionError:
                await self._pause_unauthorized_run(run)
                break
            except (OSError, RuntimeError, TypeError, ValueError, SQLAlchemyError) as exc:
                await self.repository.fail_proposal(
                    proposal.id,
                    type(exc).__name__,
                    retry_initial_seconds=self.settings.memory_rebuild_retry_initial_seconds,
                )
                continue
            if result.fact_id is not None:
                await self.processor._facts.schedule_embedding(result.fact_id)
            processed += 1
        await self.repository.complete_item_receipts(
            run.public_id,
            include_failed_live_jobs=run.selection.include_failed_live_jobs,
            item_ids=tuple(sorted(affected_item_ids)),
            limit=self.settings.memory_rebuild_commit_batch_size,
        )
        # Rejected proposals and no-claims items never enter next_commit_rows.
        # Drain one bounded page each tick, without persisting another cursor.
        await self.repository.complete_item_receipts(
            run.public_id,
            include_failed_live_jobs=run.selection.include_failed_live_jobs,
            limit=self.settings.memory_rebuild_commit_batch_size,
        )
        await self.repository.finalize_commit(run.public_id)
        return processed

    async def _adapt_event(self, event: Any, mode: MemoryRebuildThirdPartyMode) -> Any:
        if mode is MemoryRebuildThirdPartyMode.DISABLED:
            return replace(
                event, mentioned_user_ids=(), mentioned_person_ids=(), reply_sender_user_id=None
            )
        return await self.ledger.hydrate_rebuild_subjects(event)

    async def _require(
        self, run_id: str, *, session: AsyncSession | None = None
    ) -> MemoryRebuildRun:
        run = await self.repository.get_run(run_id, session=session)
        if run is None:
            raise ValueError("memory rebuild run not found")
        return run
