"""Reconcile actual Host receipts into participation without repeating execution."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

from pydantic import ValidationError
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only
from yuki_participation.models import Effect, Feedback, Scope, SourceRef
from yuki_participation.participation import ParticipationCheckpoint
from yuki_participation.self_report import SelfReport

from qq_ai_bot.conversation.autonomy_binding import AcceptedInitiative, AutonomyOwner
from qq_ai_bot.conversation.autonomy_db_models import InitiativeFeedbackModel, InitiativeRunModel
from qq_ai_bot.conversation.ordinary_admission import OrdinaryAdmission, OrdinaryAdmissionRepository
from qq_ai_bot.persistence.models import ChatEventModel, MemoryToolReceiptModel
from qq_ai_bot.runtime.subagent_schema import children
from qq_ai_bot.runtime.work_schema_v1 import journal, work
from qq_ai_bot.social.db_models import SocialOperationModel

if TYPE_CHECKING:
    from yuki_participation.participation import UnitBinding

    from qq_ai_bot.services.semantic_participation import SemanticParticipationService, _Session

_ACTIVE = {"accepted", "running"}
_SEND_ACTIONS = {"send_message", "send_file_caption"}


def admission_unit_binding(admission: OrdinaryAdmission) -> UnitBinding | None:
    """Convert only the unit/source mapping frozen by the original admission."""
    from yuki_participation.participation import ParticipationUnit, UnitBinding

    binding = admission.binding
    if binding is None:
        return None
    return UnitBinding(
        scope=Scope(conversation_id=admission.conversation_id, generation=admission.generation),
        unit=ParticipationUnit(thread=binding.unit_key, target=binding.target_hint),
        actor=admission.actor_person_id,
        basis=tuple(SourceRef(event_id=key, revision=revision) for key, revision in binding.basis),
    )


async def _ordinary_bindings(
    service: SemanticParticipationService, item: _Session, rows: list[SocialOperationModel]
) -> dict[str, tuple[OrdinaryAdmission, UnitBinding]]:
    repository = OrdinaryAdmissionRepository(service.database)
    prepared: dict[str, tuple[OrdinaryAdmission, UnitBinding]] = {}
    prefix = f"{item.scene.conversation_id}:event:"
    turns = {}
    for turn in dict.fromkeys(row.source_turn_id for row in rows):
        event_id = turn.removeprefix(prefix) if turn.startswith(prefix) else ""
        if not event_id.isdecimal() or int(event_id) < 1:
            continue
        turns[int(event_id)] = turn
    if not turns:
        return prepared
    async with service.database.sessions() as session:
        await session.execute(text("BEGIN"))
        admissions: list[OrdinaryAdmission] = []
        ids = tuple(turns)
        for start in range(0, len(ids), 128):
            admissions.extend(
                await repository.current_admissions(
                    item.scene.conversation_id,
                    item.scene.generation,
                    ids[start : start + 128],
                    session=session,
                )
            )
        bindings = [(admission, admission_unit_binding(admission)) for admission in admissions]
        refs = tuple(ref for _, binding in bindings if binding is not None for ref in binding.basis)
        versions = cast(
            dict[str, Any], item.controller.state.host_checkpoint.get("source_versions", {})
        )
        retained = ParticipationCheckpoint.model_validate(
            item.controller.state.host_checkpoint.get("participation_v1", {})
        )
        known = {ref for unit in retained.units.values() for ref in unit.binding.basis}
        known.update(event.ref for event in item.controller.state.events.values())
        for boundary in item.controller.state.boundaries.values():
            known.update((boundary.source, *boundary.dependencies, *boundary.release_dependencies))
            if boundary.released_by is not None:
                known.add(boundary.released_by)
        verifiable = tuple(ref for ref in refs if ref.event_id in versions or ref in known)
        # An admission alone cannot establish a controller source version. Before
        # cold hydration leave this association unknown; invalidating an unseen
        # ref would poison its first real event.
        valid = (
            await service._sources_current(item, verifiable, session=session) if verifiable else {}
        )
        for admission, binding in bindings:
            if binding is not None and all(valid.get(ref, False) for ref in binding.basis):
                prepared[turns[admission.event_id]] = admission, binding
    return prepared


def _timestamp(value: datetime) -> float:
    return value.replace(tzinfo=UTC).timestamp() if value.tzinfo is None else value.timestamp()


def _charged_ref(ref: str) -> bool:
    parts = ref.split(":")
    return (
        len(parts) == 3
        and parts[0] == "work-model"
        and bool(parts[1])
        and parts[2].isdecimal()
        and int(parts[2]) > 0
    )


def _send_key(origin: SocialOperationModel, row: SocialOperationModel) -> tuple[str, str, str, str]:
    call = origin.tool_call_id
    parts = call.split(":")
    logical = (
        parts[1]
        if len(parts) == 3 and parts[0] == "seq"
        else hashlib.sha256(call.encode()).hexdigest()[:24]
    )
    return origin.source_turn_id, logical, row.target_kind, row.target_id


def _send_effect_id(key: tuple[str, str, str, str]) -> str:
    return "social:" + hashlib.sha256("\0".join(key).encode()).hexdigest()


def _logical_social_effects(rows: list[SocialOperationModel]) -> dict[str, tuple[str, Effect]]:
    """A sequence and file caption share their actual parent logical send identity."""
    by_id = {row.id: row for row in rows}
    groups: dict[tuple[str, str, str, str], list[SocialOperationModel]] = {}
    for row in rows:
        if row.status != "succeeded" or row.action not in _SEND_ACTIONS:
            continue
        origin = row
        if row.source_turn_id.startswith("social-caption:"):
            parent_origin = by_id.get(row.source_turn_id.partition(":")[2])
            if parent_origin is None:
                continue  # A caption cannot invent the parent execution identity.
            origin = parent_origin
        key = _send_key(origin, row)
        groups.setdefault(key, []).append(row)
    result = {}
    for (turn, logical, target_kind, target_id), group_rows in groups.items():
        run_ref = (
            turn.split(":initiative:", 1)[1]
            if ":initiative:" in turn
            else "social-run:" + hashlib.sha256(turn.encode()).hexdigest()
        )
        effect = Effect(
            effect_id=_send_effect_id((turn, logical, target_kind, target_id)),
            kind="message",
            at=min(_timestamp(part.updated_at) for part in group_rows),
            actual_targets=("group",) if target_kind == "space" else (target_id,),
        )
        result[effect.effect_id] = (run_ref, effect)
    return result


async def _scope_social_rows(
    service: SemanticParticipationService, conversation_id: str
) -> list[SocialOperationModel]:
    async with service.database.sessions() as session:
        return list(
            await session.scalars(
                select(SocialOperationModel)
                .where(
                    SocialOperationModel.source_conversation_id == conversation_id,
                    SocialOperationModel.updated_at >= datetime.now(UTC) - timedelta(seconds=600),
                )
                .order_by(SocialOperationModel.updated_at.desc())
                .limit(2048)
            )
        )


async def sync_scope_effects(
    service: SemanticParticipationService,
    item: _Session,
    *,
    rows: list[SocialOperationModel] | None = None,
    apply_committed: bool = True,
) -> None:
    """Call once per active scope/tick, including legacy-only scopes, before rate integration."""
    if rows is None:
        rows = await _scope_social_rows(service, item.scene.conversation_id)
    current = [
        row for row in rows if row.target_kind == "space" and row.target_id == item.scene.space_id
    ]
    effects = _logical_social_effects(current)
    ordinary = await _ordinary_bindings(service, item, current)
    initiative_ids = {run for run, _ in effects.values() if not run.startswith("social-run:")}
    async with service.database.sessions() as session:
        valid_run_rows = (
            list(
                await session.scalars(
                    select(InitiativeRunModel).where(
                        InitiativeRunModel.id.in_(initiative_ids),
                        InitiativeRunModel.conversation_id == item.scene.conversation_id,
                        InitiativeRunModel.generation == item.scene.generation,
                    )
                )
            )
            if initiative_ids
            else []
        )
    valid_runs = {run.id for run in valid_run_rows}
    semantic_runs = {run.id for run in valid_run_rows if run.owner == "semantic"}
    run_threads = {run.id: run.thread_key for run in valid_run_rows if run.thread_key}
    outbound_threads = cast(
        dict[str, str], item.controller.state.host_checkpoint.setdefault("outbound_threads", {})
    )
    by_id = {row.id: row for row in current}
    output_ids = tuple({row.event_id for row in current if row.event_id is not None})
    valid_outputs: set[int] = set()
    anchor_refs = tuple(
        observed.ref
        for identity in output_ids
        if (observed := item.controller.state.events.get(f"event:{identity}")) is not None
        and observed.kind == "self"
    )
    valid_anchors: dict[SourceRef, bool] = {}
    if output_ids and ordinary:
        async with service.database.sessions() as session:
            await session.execute(text("BEGIN"))
            for start in range(0, len(output_ids), 128):
                valid_outputs.update(
                    await session.scalars(
                        select(ChatEventModel.id).where(
                            ChatEventModel.id.in_(output_ids[start : start + 128]),
                            ChatEventModel.canonical_conversation_id == item.scene.conversation_id,
                            ChatEventModel.author_kind == "yuki",
                            ChatEventModel.direction == "outbound",
                            ChatEventModel.suppression_status == "keeper",
                        )
                    )
                )
            valid_anchors = (
                await service._sources_current(item, anchor_refs, session=session)
                if anchor_refs
                else {}
            )
    for row in current:
        if row.status != "succeeded" or row.event_id is None or row.action not in _SEND_ACTIONS:
            continue
        origin = row
        if row.source_turn_id.startswith("social-caption:"):
            parent = by_id.get(row.source_turn_id.partition(":")[2])
            if parent is None:
                continue
            origin = parent
        _, marker, run_id = origin.source_turn_id.partition(":initiative:")
        key = f"event:{row.event_id}"
        if marker and run_id in semantic_runs:
            item.controller.observe_public_anchor(run_id, key, _timestamp(row.updated_at))
        if marker and run_id in run_threads:
            thread = run_threads[run_id]
            outbound_threads[key] = thread
            cached = item.controller.state.events.get(key)
            if cached is not None and cached.kind == "self" and cached.thread != thread:
                item.controller.state.events[key] = cached.model_copy(update={"thread": thread})
        frozen = ordinary.get(origin.source_turn_id)
        actual = effects.get(_send_effect_id(_send_key(origin, row)))
        if frozen is not None and actual is not None and row.presence_id == frozen[0].presence_id:
            _, binding = frozen
            # The receipt is the send fact; the original admission is the unit
            # fact. Neither later context nor a model-supplied ID can replace it.
            observed = item.controller.state.events.get(key)
            if row.event_id not in valid_outputs:
                continue
            # Controller revisions are source versions, not content hashes.
            # Before hydration the receipt can establish expression, but cannot
            # invent a public anchor. A subsequent sync attaches the real ref.
            anchor = (
                observed.ref
                if observed is not None
                and observed.kind == "self"
                and valid_anchors.get(observed.ref, False)
                else None
            )
            if item.controller.observe_unit_expression(
                binding, actual[0], actual[1], anchor=anchor
            ):
                outbound_threads[key] = binding.unit.thread
                cached = item.controller.state.events.get(key)
                if (
                    cached is not None
                    and cached.kind == "self"
                    and cached.thread != binding.unit.thread
                ):
                    item.controller.state.events[key] = cached.model_copy(
                        update={"thread": binding.unit.thread}
                    )
    if len(outbound_threads) > 1024:
        for key in tuple(outbound_threads)[:-1024]:
            outbound_threads.pop(key)
    for run_ref, effect in effects.values():
        if apply_committed and (run_ref.startswith("social-run:") or run_ref in valid_runs):
            item.controller.observe_committed_effect(run_ref, effect)


@dataclass
class _RunFacts:
    run: AcceptedInitiative
    task: Any
    actual: dict[str, Effect]
    model_refs: set[str]
    durable: list[InitiativeFeedbackModel]
    checkpoint: str | None

    def pending(self) -> tuple[str, list[str]]:
        outcome = self.run.state
        if self.run.state in _ACTIVE and self.task:
            state = self.task["state"]
            # Only a real terminal Work ends the run. A retained suspended/
            # waiting_user Work keeps its accepted/running initiative, so an
            # explicit resume continues through the original SELF source.
            outcome = (
                ("completed" if self.actual else "no_reply")
                if state == "completed"
                else "interrupted"
                if state in {"failed", "cancelled"}
                else "running"
            )
        known = {
            ref for row in self.durable for ref in json.loads(row.payload_json).get("effects", ())
        }
        unseen = sorted((set(self.actual) | self.model_refs) - known)
        return outcome, unseen

    def changed(self) -> bool:
        outcome, unseen = self.pending()
        return bool(unseen or not self.durable or self.durable[-1].outcome != outcome)


async def _read_facts(
    service: SemanticParticipationService,
    ids: tuple[str, ...],
    session: AsyncSession,
    *,
    errors: list[Exception] | None = None,
) -> list[_RunFacts]:
    """Exact receipts/counters in one explicit snapshot; no implicit evidence LIMIT."""
    runs = await service.repository.get_runs(ids, session=session, errors=errors)
    turns = {f"{run.conversation_id}:initiative:{run.run_id}": run.run_id for run in runs}
    tasks = (
        (
            await session.execute(
                select(
                    work.c.id,
                    work.c.source_key,
                    work.c.state,
                    work.c.model_requests,
                    work.c.conversation_id,
                    work.c.generation,
                ).where(work.c.source_key.in_(f"initiative:{run.run_id}" for run in runs))
            )
        )
        .mappings()
        .all()
    )
    by_source = {task["source_key"]: task for task in tasks}
    social_query = select(SocialOperationModel).options(
        load_only(
            SocialOperationModel.id,
            SocialOperationModel.source_turn_id,
            SocialOperationModel.tool_call_id,
            SocialOperationModel.status,
            SocialOperationModel.action,
            SocialOperationModel.updated_at,
            SocialOperationModel.target_kind,
            SocialOperationModel.target_id,
        )
    )
    social = list(
        await session.scalars(social_query.where(SocialOperationModel.source_turn_id.in_(turns)))
    )
    parents = tuple(f"social-caption:{row.id}" for row in social)
    captions: list[SocialOperationModel] = []
    for start in range(0, len(parents), 128):
        captions.extend(
            await session.scalars(
                social_query.where(
                    SocialOperationModel.source_turn_id.in_(parents[start : start + 128])
                )
            )
        )
    parent_turn = {f"social-caption:{row.id}": row.source_turn_id for row in social}
    social_by_run: dict[str, list[SocialOperationModel]] = {run.run_id: [] for run in runs}
    for row in (*social, *captions):
        social_by_run[turns[parent_turn.get(row.source_turn_id, row.source_turn_id)]].append(row)
    tools = (
        await session.execute(
            select(
                MemoryToolReceiptModel.id,
                MemoryToolReceiptModel.initiative_run_id,
                MemoryToolReceiptModel.created_at,
            ).where(
                MemoryToolReceiptModel.initiative_run_id.in_(ids),
                MemoryToolReceiptModel.trigger_event_id.is_(None),
            )
        )
    ).all()
    durable = list(
        await session.scalars(
            select(InitiativeFeedbackModel)
            .where(InitiativeFeedbackModel.run_id.in_(ids))
            .order_by(InitiativeFeedbackModel.sequence)
        )
    )
    task_ids = tuple(task["id"] for task in tasks)
    charged = (
        (
            await session.execute(
                select(
                    children.c.root_id,
                    work.c.id,
                    work.c.model_requests,
                    work.c.conversation_id,
                    work.c.generation,
                )
                .join(work, work.c.id == children.c.work_id)
                .where(children.c.root_id.in_(task_ids))
            )
        )
        .mappings()
        .all()
        if task_ids
        else []
    )
    replay_ids = tuple(
        task["id"]
        for task in tasks
        if (task["conversation_id"], task["generation"]) in service._sessions
    )
    checkpoints: dict[str, str] = (
        {
            identity: payload
            for identity, payload in (
                await session.execute(
                    select(journal.c.work_id, journal.c.payload_json).where(
                        journal.c.work_id.in_(replay_ids)
                    )
                )
            ).all()
        }
        if replay_ids
        else {}
    )
    result = []
    for run in runs:
        try:
            task = by_source.get(f"initiative:{run.run_id}")
            actual = {
                key: effect
                for key, (_, effect) in _logical_social_effects(social_by_run[run.run_id]).items()
            }
            actual.update(
                {
                    f"tool:{identity}": Effect(
                        effect_id=f"tool:{identity}", kind="tool", at=_timestamp(created)
                    )
                    for identity, run_id, created in tools
                    if run_id == run.run_id
                }
            )
            counts = [(task["id"], task["model_requests"])] if task else []
            counts.extend(
                (row["id"], row["model_requests"])
                for row in charged
                if task
                and row["root_id"] == task["id"]
                and row["conversation_id"] == run.conversation_id
                and row["generation"] == run.generation
            )
            model_refs = {
                f"work-model:{identity}:{ordinal}"
                for identity, count in counts
                for ordinal in range(1, int(count) + 1)
            }
            result.append(
                _RunFacts(
                    run,
                    task,
                    actual,
                    model_refs,
                    [row for row in durable if row.run_id == run.run_id],
                    checkpoints.get(task["id"]) if task else None,
                )
            )
        except (ValueError, TypeError, KeyError) as exc:
            if errors is None:
                raise
            errors.append(exc)
    return result


async def _commit_pending(service: SemanticParticipationService, run_id: str) -> _RunFacts | None:
    # Each <=64-ref feedback page is its own atomic transaction, as before.
    # Hot producers cannot monopolize a tick; remaining refs stay in factual tables.
    for _ in range(4):
        for attempt in range(3):
            try:
                async with service.database.sessions() as session:
                    await session.execute(text("BEGIN"))
                    facts = await _read_facts(service, (run_id,), session)
                    if not facts:
                        return None
                    fact = facts[0]
                    if not fact.changed():
                        return fact
                    outcome, unseen = fact.pending()
                    batch = unseen[:64]
                    targets = tuple(
                        sorted(
                            {
                                fact.run.space_id if target == "group" else target
                                for ref in batch
                                if ref in fact.actual
                                for target in fact.actual[ref].actual_targets
                            }
                        )
                    )
                    await service.repository.record_feedback(
                        run_id,
                        sequence=fact.run.feedback_sequence + 1,
                        outcome=outcome,
                        effect_refs=tuple(batch),
                        actual_target_refs=targets,
                        considered_sources=fact.run.sources,
                        session=session,
                    )
                    await session.commit()
                break
            except OperationalError as exc:
                if getattr(exc.orig, "sqlite_errorcode", None) != 517 or attempt == 2:
                    raise
    async with service.database.sessions() as session:
        await session.execute(text("BEGIN"))
        facts = await _read_facts(service, (run_id,), session)
        return facts[0] if facts else None


async def reconcile_page(
    service: SemanticParticipationService, runs: tuple[AcceptedInitiative | str, ...]
) -> None:
    """Read unchanged rows together; durable feedback precedes one save per controller."""
    if not runs:
        return
    facts = []
    failures: list[Exception] = []
    # Bound parameters and snapshot lifetime even for the active outbox's 128 entries.
    for start in range(0, len(runs), 16):
        async with service.database.sessions() as session:
            await session.execute(text("BEGIN"))
            facts.extend(
                await _read_facts(
                    service,
                    tuple(
                        run if isinstance(run, str) else run.run_id
                        for run in runs[start : start + 16]
                    ),
                    session,
                    errors=failures,
                )
            )
    prepared = []
    for fact in facts:
        try:
            if fact.task is None and fact.run.state in _ACTIVE:
                await service._dispatch(fact.run)
                continue
            committed = await _commit_pending(service, fact.run.run_id) if fact.changed() else fact
            if committed is not None:
                prepared.append(committed)
        except Exception as exc:
            failures.append(exc)
    async with service._session_lock:
        items = {}
        for fact in prepared:
            run = fact.run
            item = service._sessions.get((run.conversation_id, run.generation))
            if item is not None:
                try:
                    _replay(item, fact)
                except Exception as exc:
                    failures.append(exc)
                items[(run.conversation_id, run.generation)] = item
        for item in items.values():
            try:
                await service._save(item)
            except Exception as exc:
                failures.append(exc)
    if failures:
        raise failures[0]


def _replay(item: _Session, fact: _RunFacts) -> None:
    run, actual, durable, checkpoint = fact.run, fact.actual, fact.durable, fact.checkpoint
    # Receipt I/O is complete. Keep the same cached controller through replay and
    # asynchronous checkpoint; eviction must not restore an older revision meanwhile.
    for row in durable:
        payload = json.loads(row.payload_json)
        effects = tuple(
            actual[ref]
            if ref in actual
            else Effect(effect_id=ref, kind="compute", at=_timestamp(row.created_at))
            for ref in payload.get("effects", ())
            if ref in actual or _charged_ref(ref)
        )
        if run.owner is AutonomyOwner.SEMANTIC:
            # Admission's local accepted receipt uses 1; durable Host pages start at 2.
            proposal_missing = run.proposal_id not in item.controller.state.proposals
            applied = item.controller.observe_run_feedback(
                Feedback(
                    run_ref=run.run_id,
                    proposal_id=run.proposal_id,
                    sequence=row.sequence + 1,
                    outcome="accepted"
                    if row.outcome == "running"
                    else "interrupted"
                    if row.outcome == "failed"
                    else row.outcome,
                    at=_timestamp(row.created_at),
                    effects=effects,
                )
            )
            if proposal_missing and not applied:
                # A cold/pruned snapshot may have no original proposal. Host
                # receipts still restore their real charges/effects under the
                # original run, without inventing admission or a new proposal.
                for effect in effects:
                    item.controller.observe_committed_effect(run.run_id, effect)
        else:
            for effect in effects:
                item.controller.observe_committed_effect(run.run_id, effect)
    if checkpoint:
        try:
            progress = json.loads(checkpoint).get("metadata", {}).get("progress", {})
            reports = progress.get("self_reports", ())
        except (ValueError, TypeError, AttributeError):
            reports = ()
        if isinstance(reports, (list, tuple)):
            for raw in reports[-32:]:
                try:
                    report = SelfReport.model_validate(raw)
                except (ValidationError, TypeError):
                    continue
                if report.run_ref == run.run_id:
                    item.controller.observe_self_report(report)
