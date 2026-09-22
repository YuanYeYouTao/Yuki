"""Reconcile actual Host receipts into participation without repeating execution."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from pydantic import ValidationError
from sqlalchemy import select
from yuki_participation.models import Effect, Feedback
from yuki_participation.self_report import SelfReport

from qq_ai_bot.conversation.autonomy_binding import AcceptedInitiative, AutonomyOwner
from qq_ai_bot.conversation.autonomy_db_models import InitiativeFeedbackModel, InitiativeRunModel
from qq_ai_bot.persistence.models import MemoryToolReceiptModel
from qq_ai_bot.runtime.subagent_schema import children
from qq_ai_bot.runtime.work_schema_v1 import journal, work
from qq_ai_bot.social.db_models import SocialOperationModel

if TYPE_CHECKING:
    from qq_ai_bot.services.semantic_participation import SemanticParticipationService, _Session

_ACTIVE = {"accepted", "running"}
_SEND_ACTIONS = {"send_message", "send_file_caption"}


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
        call = origin.tool_call_id
        call_parts = call.split(":")
        logical = (
            call_parts[1]
            if len(call_parts) == 3 and call_parts[0] == "seq"
            else hashlib.sha256(call.encode()).hexdigest()[:24]
        )
        key = (origin.source_turn_id, logical, row.target_kind, row.target_id)
        groups.setdefault(key, []).append(row)
    result = {}
    for (turn, logical, target_kind, target_id), group_rows in groups.items():
        identity = hashlib.sha256(
            f"{turn}\0{logical}\0{target_kind}\0{target_id}".encode()
        ).hexdigest()
        run_ref = (
            turn.split(":initiative:", 1)[1]
            if ":initiative:" in turn
            else "social-run:" + hashlib.sha256(turn.encode()).hexdigest()
        )
        effect = Effect(
            effect_id=f"social:{identity}",
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


async def sync_scope_effects(service: SemanticParticipationService, item: _Session) -> None:
    """Call once per active scope/tick, including legacy-only scopes, before rate integration."""
    rows = await _scope_social_rows(service, item.scene.conversation_id)
    current = [
        row for row in rows if row.target_kind == "space" and row.target_id == item.scene.space_id
    ]
    effects = _logical_social_effects(current)
    initiative_ids = {run for run, _ in effects.values() if not run.startswith("social-run:")}
    async with service.database.sessions() as session:
        valid_runs = (
            set(
                await session.scalars(
                    select(InitiativeRunModel.id).where(
                        InitiativeRunModel.id.in_(initiative_ids),
                        InitiativeRunModel.conversation_id == item.scene.conversation_id,
                        InitiativeRunModel.generation == item.scene.generation,
                    )
                )
            )
            if initiative_ids
            else set()
        )
    for run_ref, effect in effects.values():
        if run_ref.startswith("social-run:") or run_ref in valid_runs:
            item.controller.observe_committed_effect(run_ref, effect)


async def _feedback_rows(
    service: SemanticParticipationService, run_id: str
) -> list[InitiativeFeedbackModel]:
    async with service.database.sessions() as session:
        return list(
            await session.scalars(
                select(InitiativeFeedbackModel)
                .where(
                    InitiativeFeedbackModel.run_id == run_id,
                )
                .order_by(InitiativeFeedbackModel.sequence)
            )
        )


async def reconcile_run(service: SemanticParticipationService, run: AcceptedInitiative) -> None:
    """Durable Host feedback precedes replay into the rebuildable controller snapshot."""
    # Re-read the outbox row: an earlier page/tick may have committed its terminal state.
    run = await service.repository.get_run(run.run_id) or run
    task = await service.work.by_source(f"initiative:{run.run_id}")
    if task is None and run.state in _ACTIVE:
        await service._dispatch(run)
        return
    async with service.database.sessions() as session:
        social = list(
            await session.scalars(
                select(SocialOperationModel).where(
                    SocialOperationModel.source_turn_id
                    == f"{run.conversation_id}:initiative:{run.run_id}",
                )
            )
        )
        if social:
            social.extend(
                await session.scalars(
                    select(SocialOperationModel).where(
                        SocialOperationModel.source_turn_id.in_(
                            f"social-caption:{row.id}" for row in social
                        ),
                    )
                )
            )
        tools = list(
            await session.scalars(
                select(MemoryToolReceiptModel)
                .where(
                    MemoryToolReceiptModel.initiative_run_id == run.run_id,
                    MemoryToolReceiptModel.trigger_event_id.is_(None),
                )
                .order_by(MemoryToolReceiptModel.id)
            )
        )
        checkpoint = (
            await session.scalar(
                select(journal.c.payload_json).where(
                    journal.c.work_id == task["id"],
                )
            )
            if task
            else None
        )
        charged_work = (
            (
                await session.execute(
                    select(work.c.id, work.c.model_requests).where(
                        work.c.conversation_id == run.conversation_id,
                        work.c.generation == run.generation,
                        work.c.id.in_(
                            select(children.c.work_id).where(children.c.root_id == task["id"])
                        ),
                    )
                )
            ).all()
            if task
            else []
        )
    actual = {key: effect for key, (_, effect) in _logical_social_effects(social).items()}
    actual.update(
        {
            f"tool:{row.id}": Effect(
                effect_id=f"tool:{row.id}", kind="tool", at=_timestamp(row.created_at)
            )
            for row in tools
        }
    )
    # Root/worker counters are disjoint charges. The shared budget already sums
    # these, including internal summary charges, so adding it would double-count.
    counts = [(task["id"], task["model_requests"]), *charged_work] if task else []
    model_refs = {
        f"work-model:{identity}:{ordinal}"
        for identity, count in counts
        for ordinal in range(1, int(count) + 1)
    }
    outcome = run.state
    if run.state in _ACTIVE and task:
        if task["state"] == "completed":
            outcome = "completed" if actual else "no_reply"
        elif task["state"] in {"failed", "cancelled", "suspended", "waiting_user"}:
            outcome = "interrupted"
        else:
            outcome = "running"
    durable = await _feedback_rows(service, run.run_id)
    known = {ref for row in durable for ref in json.loads(row.payload_json).get("effects", ())}
    unseen = sorted((set(actual) | model_refs) - known)
    latest = durable[-1] if durable else None
    # Append bounded pages of NEW receipts, not the same cumulative payload every tick.
    # All 120 charged requests retain independent stable ordinals, even across a crash.
    batches = [unseen[start : start + 64] for start in range(0, len(unseen), 64)]
    if not batches and (latest is None or latest.outcome != outcome):
        batches = [[]]
    for batch in batches:
        target_refs = tuple(
            sorted(
                {
                    run.space_id if target == "group" else target
                    for ref in batch
                    if ref in actual
                    for target in actual[ref].actual_targets
                }
            )
        )
        run = await service.repository.record_feedback(
            run.run_id,
            sequence=run.feedback_sequence + 1,
            outcome=outcome,
            effect_refs=tuple(batch),
            actual_target_refs=target_refs,
            considered_sources=run.sources,
        )
    if batches:
        durable = await _feedback_rows(service, run.run_id)
    # Acquire the controller only after all asynchronous receipt I/O. Replay and
    # checkpoint below are synchronous, so a newly loaded scope cannot be evicted midway.
    item = service._sessions.get((run.conversation_id, run.generation))
    if item is None:
        return  # Original-generation results never wake a newer generation.
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
            item.controller.observe_run_feedback(
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
    service._save(item)
