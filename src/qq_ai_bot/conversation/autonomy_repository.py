"""Durable exclusive selector and proposal admission, intentionally not wired to execution.

Only a trusted host calls this repository after resolving source revisions, semantic
support and scene permissions. It does not interpret text or grant SELF tool authority.
Short transactions only read keyed host records and persist admission/feedback; no
callbacks, network, history scans, model calls or Work execution run under the write lock.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import select, tuple_, update

from qq_ai_bot.conversation.autonomy_binding import (
    AcceptedInitiative,
    AutonomyBinding,
    AutonomyOwner,
    InitiativeSource,
    InitiativeSourceKind,
)
from qq_ai_bot.conversation.autonomy_db_models import (
    AutonomyBindingModel,
    InitiativeFeedbackModel,
    InitiativeRunModel,
    InitiativeSourceClaimModel,
)
from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
from qq_ai_bot.identity.db_models import CanonicalSpaceModel, PresenceModel
from qq_ai_bot.persistence.database import Database

_ACTIVE = ("accepted", "running")
_OUTCOMES = frozenset({"running", "completed", "no_reply", "interrupted", "failed"})


class AutonomyConflict(ValueError):
    """A stale selector or conflicting replay cannot overwrite committed state."""


@dataclass(frozen=True, slots=True)
class AdmissionResult:
    outcome: str
    run: AcceptedInitiative | None = None


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _source_values(sources: tuple[InitiativeSource, ...]) -> list[dict[str, str]]:
    return [
        {"kind": source.kind.value, "source_id": source.source_id, "revision": source.revision}
        for source in sorted(sources)
    ]


def _binding(row: AutonomyBindingModel) -> AutonomyBinding:
    return AutonomyBinding(
        conversation_id=row.conversation_id,
        generation=row.generation,
        master_enabled=row.master_enabled,
        external_enabled=row.external_enabled,
        effective_owner=AutonomyOwner(row.effective_owner),
        controller_epoch=row.controller_epoch,
        fallback_reason=row.fallback_reason,
        revision=row.revision,
    )


def _run(row: InitiativeRunModel) -> AcceptedInitiative:
    return AcceptedInitiative(
        run_id=row.id,
        proposal_id=row.proposal_id,
        conversation_id=row.conversation_id,
        generation=row.generation,
        space_id=row.space_id,
        presence_id=row.presence_id,
        controller_epoch_at_acceptance=row.controller_epoch,
        sources=tuple(
            InitiativeSource(
                InitiativeSourceKind(item["kind"]), item["source_id"], item["revision"]
            )
            for item in json.loads(row.sources_json)
        ),
        owner=AutonomyOwner(row.owner),
        target_person_id=row.target_person_id,
        support_refs=tuple(json.loads(row.support_refs_json)),
        state=row.state,
        feedback_sequence=row.feedback_sequence,
    )


class AutonomyRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def ensure_binding(self, conversation_id: str, generation: int) -> AutonomyBinding:
        """Register a current group generation, default OFF; never enable it implicitly."""
        async with self._database.immediate_session() as session:
            conversation = await session.get(CanonicalConversationModel, conversation_id)
            if (
                conversation is None
                or conversation.kind != "space"
                or conversation.generation != generation
            ):
                raise AutonomyConflict("autonomy_conversation_changed")
            row = await session.get(AutonomyBindingModel, (conversation_id, generation))
            if row is None:
                row = AutonomyBindingModel(
                    conversation_id=conversation_id,
                    generation=generation,
                    master_enabled=False,
                    external_enabled=False,
                    effective_owner="off",
                    controller_epoch=0,
                    revision=1,
                    updated_at=datetime.now(UTC),
                )
                session.add(row)
                await session.flush()
            return _binding(row)

    async def get_binding(self, conversation_id: str, generation: int) -> AutonomyBinding | None:
        async with self._database.sessions() as session:
            row = await session.get(AutonomyBindingModel, (conversation_id, generation))
            return _binding(row) if row is not None else None

    async def transition(
        self,
        expected: AutonomyBinding,
        *,
        master_enabled: bool,
        external_enabled: bool,
        semantic_ready: bool,
        fallback_reason: str | None = None,
    ) -> AutonomyBinding:
        """CAS includes configuration changes, so provider recovery cannot undo manual OFF."""
        desired = expected.transition(
            master_enabled=master_enabled,
            external_enabled=external_enabled,
            semantic_ready=semantic_ready,
            fallback_reason=fallback_reason,
        )
        async with self._database.immediate_session() as session:
            conversation = await session.get(CanonicalConversationModel, expected.conversation_id)
            row = await session.get(
                AutonomyBindingModel, (expected.conversation_id, expected.generation)
            )
            if (
                conversation is None
                or conversation.generation != expected.generation
                or row is None
                or _binding(row) != expected
            ):
                raise AutonomyConflict("autonomy_binding_changed")
            if desired == expected:
                return expected
            changed = await session.scalar(
                update(AutonomyBindingModel)
                .where(
                    AutonomyBindingModel.conversation_id == expected.conversation_id,
                    AutonomyBindingModel.generation == expected.generation,
                    AutonomyBindingModel.revision == expected.revision,
                )
                .values(
                    master_enabled=desired.master_enabled,
                    external_enabled=desired.external_enabled,
                    effective_owner=desired.effective_owner.value,
                    controller_epoch=desired.controller_epoch,
                    revision=desired.revision,
                    fallback_reason=desired.fallback_reason,
                    updated_at=datetime.now(UTC),
                )
                .returning(AutonomyBindingModel.revision)
            )
            if changed is None:
                raise AutonomyConflict("autonomy_binding_changed")
            return desired

    async def accept_host_proposal(
        self,
        *,
        proposal_id: str,
        binding: AutonomyBinding,
        owner: AutonomyOwner,
        space_id: str,
        presence_id: str,
        sources: tuple[InitiativeSource, ...],
        target_person_id: str | None = None,
        support_refs: tuple[str, ...] = (),
    ) -> AdmissionResult:
        """Persist a host-validated opportunity, not permission to invoke an Agent.

        Source and support validation must be implemented by the SELF host adapter before
        this protocol is connected. Only *focus* sources belong here: shared context must
        not become a consumed opportunity. Already accepted proposal replay returns its
        original run even after a mode/generation switch; it never dispatches it again.
        """
        if not proposal_id or len(proposal_id) > 128 or proposal_id != proposal_id.strip():
            raise ValueError("initiative_proposal_id_invalid")
        if len(support_refs) > 32 or any(not item or len(item) > 128 for item in support_refs):
            raise ValueError("initiative_support_refs_invalid")
        if owner is AutonomyOwner.SEMANTIC and not support_refs:
            raise ValueError("initiative_semantic_support_required")
        now = datetime.now(UTC)
        requested = AcceptedInitiative(
            run_id=str(uuid4()),
            proposal_id=proposal_id,
            conversation_id=binding.conversation_id,
            generation=binding.generation,
            space_id=space_id,
            presence_id=presence_id,
            controller_epoch_at_acceptance=binding.controller_epoch,
            sources=sources,
            owner=owner,
            target_person_id=target_person_id,
            support_refs=support_refs,
        )
        source_json = _json(_source_values(sources))
        support_json = _json(sorted(set(support_refs)))
        payload_hash = hashlib.sha256(
            _json([space_id, presence_id, target_person_id, source_json, support_json]).encode(
                "utf-8"
            )
        ).hexdigest()
        async with self._database.immediate_session() as session:
            prior = await session.scalar(
                select(InitiativeRunModel).where(
                    InitiativeRunModel.conversation_id == binding.conversation_id,
                    InitiativeRunModel.generation == binding.generation,
                    InitiativeRunModel.owner == owner.value,
                    InitiativeRunModel.controller_epoch == binding.controller_epoch,
                    InitiativeRunModel.proposal_id == proposal_id,
                )
            )
            if prior is not None:
                if prior.payload_hash != payload_hash:
                    raise AutonomyConflict("initiative_proposal_replay_conflict")
                return AdmissionResult("duplicate", _run(prior))
            current = await session.get(
                AutonomyBindingModel, (binding.conversation_id, binding.generation)
            )
            conversation = await session.get(CanonicalConversationModel, binding.conversation_id)
            if (
                current is None
                or conversation is None
                or conversation.generation != binding.generation
            ):
                return AdmissionResult("stale_binding")
            if not current.master_enabled:
                return AdmissionResult("disabled")
            if not _binding(current).accepts(
                owner=owner,
                epoch=binding.controller_epoch,
                conversation_id=binding.conversation_id,
                generation=binding.generation,
            ):
                return AdmissionResult("stale_binding")
            space = await session.get(CanonicalSpaceModel, space_id)
            presence = await session.get(PresenceModel, presence_id)
            if (
                conversation.kind != "space"
                or conversation.space_id != space_id
                or space is None
                or not space.enabled
                or presence is None
                or not presence.enabled
            ):
                return AdmissionResult("scene_unavailable")
            claimed = await session.scalar(
                select(InitiativeSourceClaimModel.run_id)
                .where(
                    InitiativeSourceClaimModel.conversation_id == binding.conversation_id,
                    InitiativeSourceClaimModel.generation == binding.generation,
                    tuple_(
                        InitiativeSourceClaimModel.source_kind,
                        InitiativeSourceClaimModel.source_id,
                        InitiativeSourceClaimModel.source_revision,
                    ).in_(
                        [
                            (source.kind.value, source.source_id, source.revision)
                            for source in sources
                        ]
                    ),
                )
                .limit(1)
            )
            if claimed is not None:
                return AdmissionResult("source_considered")
            active = await session.scalar(
                select(InitiativeRunModel.id)
                .where(
                    InitiativeRunModel.conversation_id == binding.conversation_id,
                    InitiativeRunModel.generation == binding.generation,
                    InitiativeRunModel.state.in_(_ACTIVE),
                )
                .limit(1)
            )
            if active is not None:
                return AdmissionResult("busy")
            row = InitiativeRunModel(
                id=requested.run_id,
                proposal_id=proposal_id,
                conversation_id=binding.conversation_id,
                generation=binding.generation,
                owner=owner.value,
                controller_epoch=binding.controller_epoch,
                space_id=space_id,
                presence_id=presence_id,
                target_person_id=target_person_id,
                payload_hash=payload_hash,
                sources_json=source_json,
                support_refs_json=support_json,
                state="accepted",
                feedback_sequence=0,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            await session.flush()
            session.add_all(
                InitiativeSourceClaimModel(
                    conversation_id=binding.conversation_id,
                    generation=binding.generation,
                    source_kind=source.kind.value,
                    source_id=source.source_id,
                    source_revision=source.revision,
                    run_id=row.id,
                )
                for source in sources
            )
            return AdmissionResult("accepted", _run(row))

    async def get_run(self, run_id: str) -> AcceptedInitiative | None:
        """Read factual admission after restart/switch; callers still must authorize execution."""
        async with self._database.sessions() as session:
            row = await session.get(InitiativeRunModel, run_id)
            return _run(row) if row is not None else None

    async def query_proposal(
        self,
        *,
        conversation_id: str,
        generation: int,
        owner: AutonomyOwner,
        controller_epoch: int,
        proposal_id: str,
    ) -> AcceptedInitiative | None:
        """Recover uncertain admission by its original key without proposing or executing again."""
        async with self._database.sessions() as session:
            row = await session.scalar(
                select(InitiativeRunModel).where(
                    InitiativeRunModel.conversation_id == conversation_id,
                    InitiativeRunModel.generation == generation,
                    InitiativeRunModel.owner == owner.value,
                    InitiativeRunModel.controller_epoch == controller_epoch,
                    InitiativeRunModel.proposal_id == proposal_id,
                )
            )
            return _run(row) if row is not None else None

    async def record_feedback(
        self,
        run_id: str,
        *,
        sequence: int,
        outcome: str,
        actual_target_refs: tuple[str, ...] = (),
        effect_refs: tuple[str, ...] = (),
        considered_sources: tuple[InitiativeSource, ...] = (),
    ) -> AcceptedInitiative:
        """Commit a real host result exactly once, independent of current selector state.

        References identify durable receipts/targets, never model prose. Unknown delivery
        remains a referenced effect, not a claim that sending succeeded. Later factual
        receipts may append to a terminal run, but cannot change its settled state or
        dispatch it again. Identical replay is harmless.
        """
        if sequence < 1 or outcome not in _OUTCOMES:
            raise ValueError("initiative_feedback_invalid")
        for refs in (actual_target_refs, effect_refs):
            if len(refs) > 64 or any(not item or len(item) > 128 for item in refs):
                raise ValueError("initiative_feedback_refs_invalid")
        if len(considered_sources) > 32 or len(set(considered_sources)) != len(considered_sources):
            raise ValueError("initiative_feedback_sources_invalid")
        payload = _json(
            {
                "actual_targets": actual_target_refs,
                "effects": effect_refs,
                "considered_sources": _source_values(considered_sources),
            }
        )
        if len(payload.encode("utf-8")) > 16_384:
            raise ValueError("initiative_feedback_too_large")
        async with self._database.immediate_session() as session:
            row = await session.get(InitiativeRunModel, run_id)
            if row is None:
                raise AutonomyConflict("initiative_run_missing")
            previous = await session.get(InitiativeFeedbackModel, (run_id, sequence))
            if previous is not None:
                if previous.outcome != outcome or previous.payload_json != payload:
                    raise AutonomyConflict("initiative_feedback_replay_conflict")
                return _run(row)
            active = row.state in _ACTIVE
            if not active and outcome == "running":
                raise AutonomyConflict("initiative_run_terminal")
            if sequence != row.feedback_sequence + 1:
                raise AutonomyConflict("initiative_feedback_sequence_gap")
            claimed = (
                set(
                    await session.execute(
                        select(
                            InitiativeSourceClaimModel.source_kind,
                            InitiativeSourceClaimModel.source_id,
                            InitiativeSourceClaimModel.source_revision,
                        ).where(
                            InitiativeSourceClaimModel.conversation_id == row.conversation_id,
                            InitiativeSourceClaimModel.generation == row.generation,
                            tuple_(
                                InitiativeSourceClaimModel.source_kind,
                                InitiativeSourceClaimModel.source_id,
                                InitiativeSourceClaimModel.source_revision,
                            ).in_(
                                [
                                    (item.kind.value, item.source_id, item.revision)
                                    for item in considered_sources
                                ]
                            ),
                        )
                    )
                )
                if considered_sources
                else set()
            )
            now = datetime.now(UTC)
            if active:
                row.state = outcome
            row.feedback_sequence = sequence
            row.updated_at = now
            session.add(
                InitiativeFeedbackModel(
                    run_id=run_id,
                    sequence=sequence,
                    outcome=outcome,
                    payload_json=payload,
                    created_at=now,
                )
            )
            session.add_all(
                InitiativeSourceClaimModel(
                    conversation_id=row.conversation_id,
                    generation=row.generation,
                    source_kind=item.kind.value,
                    source_id=item.source_id,
                    source_revision=item.revision,
                    run_id=row.id,
                )
                for item in considered_sources
                if (item.kind.value, item.source_id, item.revision) not in claimed
            )
            return _run(row)
