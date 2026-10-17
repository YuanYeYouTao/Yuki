"""Frozen human source and unit binding; this is admission, not a Main journal."""

from __future__ import annotations

import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from uuid import uuid4

from sqlalchemy import and_, or_, select, text
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncSession

from yuki.conversation.canonical_db_models import CanonicalConversationModel
from yuki.conversation.initiative_sources import source_revision
from yuki.conversation.ordinary_admission_db_models import OrdinaryTurnAdmissionModel
from yuki.identity.db_models import (
    CanonicalPersonModel,
    CanonicalSpaceModel,
    IdentityBindingModel,
    PresenceModel,
    SpaceBindingModel,
)
from yuki.identity.person_identity import current_person_id, person_root_id
from yuki.persistence.database import Database
from yuki.persistence.models import ChatEventModel
from yuki.persistence.repository_helpers import _event_record
from yuki.persistence.repository_records import EventRecord
from yuki.persistence.unit_of_work import optional_session


@dataclass(frozen=True, slots=True)
class OrdinaryParticipationBinding:
    conversation_id: str
    generation: int
    event_id: int
    source_revision: str
    unit_key: str
    target_hint: str
    basis: tuple[tuple[str, int], ...] = ()


@dataclass(frozen=True, slots=True)
class OrdinaryAdmission:
    event_id: int
    conversation_id: str
    generation: int
    source_revision: str
    actor_person_id: str
    presence_id: str
    activation_id: str
    coordinator_version: int
    binding: OrdinaryParticipationBinding | None
    route: str
    work_id: str | None
    input_id: int | None
    reason: str | None = None


class OrdinaryAdmissionConflict(RuntimeError):
    pass


class OrdinaryAdmissionDuplicate(OrdinaryAdmissionConflict):
    """Rollback a competing mailbox publication; the original admission wins."""


def _dto(row: OrdinaryTurnAdmissionModel) -> OrdinaryAdmission:
    saved = json.loads(row.basis_json)
    basis = saved["basis"] if isinstance(saved, dict) else saved
    reason = saved.get("reason") if isinstance(saved, dict) else None
    binding = None
    if row.unit_key is not None and row.target_hint is not None:
        binding = OrdinaryParticipationBinding(
            row.conversation_id,
            row.generation,
            row.event_id,
            row.source_revision,
            row.unit_key,
            row.target_hint,
            tuple((str(ref[0]), int(ref[1])) for ref in basis),
        )
    return OrdinaryAdmission(
        row.event_id,
        row.conversation_id,
        row.generation,
        row.source_revision,
        row.actor_person_id,
        row.presence_id,
        row.activation_id,
        row.coordinator_version,
        binding,
        row.route,
        row.work_id,
        row.input_id,
        reason,
    )


@dataclass(frozen=True, slots=True)
class PreparedOrdinaryAdmission:
    admission: OrdinaryAdmission
    event: EventRecord
    basis_json: str


class OrdinaryAdmissionRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def get(
        self, event_id: int, *, conversation_id: str | None = None, generation: int | None = None
    ) -> OrdinaryAdmission | None:
        async with self.database.sessions() as session:
            row = await session.get(OrdinaryTurnAdmissionModel, event_id)
            if (
                row is None
                or (generation is not None and row.generation != generation)
                or (conversation_id is not None and row.conversation_id != conversation_id)
            ):
                return None
            return _dto(row)

    async def admitted_event_ids(
        self, conversation_id: str, generation: int, event_ids: tuple[int, ...]
    ) -> set[int]:
        if not event_ids:
            return set()
        async with self.database.sessions() as session:
            return set(
                await session.scalars(
                    select(OrdinaryTurnAdmissionModel.event_id).where(
                        OrdinaryTurnAdmissionModel.conversation_id == conversation_id,
                        OrdinaryTurnAdmissionModel.generation == generation,
                        OrdinaryTurnAdmissionModel.event_id.in_(event_ids),
                    )
                )
            )

    async def current(
        self, event_id: int, *, conversation_id: str | None = None, generation: int | None = None
    ) -> OrdinaryAdmission | None:
        rows = await self.current_admissions(conversation_id, generation, (event_id,))
        return rows[0] if rows else None

    async def current_admissions(
        self,
        conversation_id: str | None,
        generation: int | None,
        event_ids: tuple[int, ...],
        *,
        session: AsyncSession | None = None,
    ) -> tuple[OrdinaryAdmission, ...]:
        if not event_ids:
            return ()
        a, e, c = OrdinaryTurnAdmissionModel, ChatEventModel, CanonicalConversationModel
        p, presence, identity = CanonicalPersonModel, PresenceModel, IdentityBindingModel
        space, binding = CanonicalSpaceModel, SpaceBindingModel
        query = (
            select(a, e)
            .join(e, e.id == a.event_id)
            .join(c, c.id == a.conversation_id)
            .join(p, p.id == a.actor_person_id)
            .join(presence, presence.id == a.presence_id)
            .join(
                identity,
                and_(
                    identity.person_id == p.id,
                    identity.platform == presence.platform,
                    identity.external_account_id == e.sender_user_id,
                ),
            )
            .outerjoin(space, space.id == c.space_id)
            .outerjoin(
                binding,
                and_(
                    binding.space_id == space.id,
                    binding.platform == presence.platform,
                    binding.external_space_id == e.group_id,
                ),
            )
            .where(
                a.event_id.in_(event_ids),
                c.generation == a.generation,
                e.id > c.starts_after_event_id,
                e.suppression_status == "keeper",
                e.canonical_conversation_id == a.conversation_id,
                e.author_person_id == a.actor_person_id,
                e.ingress_presence_id == a.presence_id,
                presence.enabled.is_(True),
                presence.external_account_id == e.bot_user_id,
                identity.status == "active",
                p.enabled.is_(True),
                select(p.enabled)
                .where(p.id == person_root_id(a.actor_person_id))
                .correlate(a)
                .scalar_subquery()
                .is_(True),
                or_(
                    and_(c.kind == "private", c.person_id == p.id, p.enabled.is_(True)),
                    and_(c.kind == "space", space.enabled.is_(True), binding.status == "active"),
                ),
            )
        )
        if conversation_id is not None:
            query = query.where(a.conversation_id == conversation_id)
        if generation is not None:
            query = query.where(a.generation == generation)
        owned = session is None
        async with optional_session(self.database, session, write=False) as active:
            if owned:
                await active.execute(text("BEGIN"))
            rows = (await active.execute(query)).all()
            return tuple(
                _dto(row)
                for row, event in rows
                if str(source_revision(event)) == row.source_revision
            )

    @staticmethod
    def prepare(
        event: EventRecord,
        generation: int,
        coordinator_version: int,
        binding: OrdinaryParticipationBinding | None = None,
        *,
        reason: str | None = None,
    ) -> PreparedOrdinaryAdmission:
        if (
            not event.author_is_human()
            or not event.canonical_conversation_id
            or not event.ingress_presence_id
            or event.direction != "inbound"
            or event.event_kind != "message"
        ):
            raise OrdinaryAdmissionConflict("ordinary_source_not_human")
        revision = str(source_revision(event))
        if binding is not None and (
            binding.event_id != event.id
            or binding.conversation_id != event.canonical_conversation_id
            or binding.generation != generation
            or binding.source_revision != revision
        ):
            raise OrdinaryAdmissionConflict("ordinary_binding_source_changed")
        basis = binding.basis if binding else ()
        basis_json = json.dumps(
            {"basis": basis, "reason": reason} if reason is not None else basis,
            separators=(",", ":"),
        )
        return PreparedOrdinaryAdmission(
            OrdinaryAdmission(
                event.id,
                event.canonical_conversation_id,
                generation,
                revision,
                event.author_person_id or "",
                event.ingress_presence_id,
                str(uuid4()),
                coordinator_version,
                binding,
                "ordinary",
                None,
                None,
                reason,
            ),
            event,
            basis_json,
        )

    def publisher(
        self, admission: PreparedOrdinaryAdmission
    ) -> Callable[[AsyncSession, int], Awaitable[None]]:
        async def publish(session: AsyncSession, input_id: int) -> None:
            from yuki.conversation.ordinary_admission_db_models import (
                OrdinaryTurnAdmissionModel,
            )
            from yuki.runtime.work_repository import WorkConflict
            from yuki.runtime.work_schema_v1 import inputs

            existing = await session.get(OrdinaryTurnAdmissionModel, admission.event.id)
            source = (
                await session.execute(
                    select(
                        inputs.c.work_id,
                        inputs.c.conversation_id,
                        inputs.c.generation,
                        inputs.c.event_id,
                    ).where(inputs.c.id == input_id)
                )
            ).first()
            if (
                source is None
                or source.work_id is None
                or source.conversation_id != admission.admission.conversation_id
                or source.generation != admission.admission.generation
                or source.event_id != admission.event.id
            ):
                raise WorkConflict("ordinary_input_source_mismatch")
            if (
                existing is not None
                and existing.activation_id == admission.admission.activation_id
                and existing.route == "work"
                and existing.input_id == input_id
                and existing.work_id == source.work_id
            ):
                return
            if not await self.commit(
                admission,
                session=session,
                work_id=source.work_id,
                input_id=input_id,
            ):
                raise OrdinaryAdmissionDuplicate("ordinary_already_admitted")

        return publish

    async def commit_to_activation(
        self,
        prepared: PreparedOrdinaryAdmission,
        activation: OrdinaryAdmission,
        *,
        session: AsyncSession,
        admission_allowed: Callable[[AsyncSession], Awaitable[bool]] | None = None,
    ) -> bool:
        source = prepared.admission
        if (source.conversation_id, source.generation) != (
            activation.conversation_id,
            activation.generation,
        ):
            raise OrdinaryAdmissionConflict("ordinary_activation_source_mismatch")
        selected = replace(
            prepared,
            admission=replace(
                source,
                activation_id=activation.activation_id,
                coordinator_version=activation.coordinator_version,
            ),
        )
        if await self.commit(selected, session=session, admission_allowed=admission_allowed):
            return True
        existing = await session.get(OrdinaryTurnAdmissionModel, source.event_id)
        if existing is not None and _dto(existing) == selected.admission:
            return False
        if existing is not None:
            raise OrdinaryAdmissionDuplicate("ordinary_already_admitted")
        raise OrdinaryAdmissionConflict("ordinary_admission_disallowed")

    async def commit(
        self,
        prepared: PreparedOrdinaryAdmission,
        *,
        session: AsyncSession | None = None,
        work_id: str | None = None,
        input_id: int | None = None,
        admission_allowed: Callable[[AsyncSession], Awaitable[bool]] | None = None,
    ) -> bool:
        if session is None:
            if await self.get(prepared.event.id) is not None:
                return False
            async with self.database.immediate_session() as owned:
                return await self.commit(
                    prepared,
                    session=owned,
                    work_id=work_id,
                    input_id=input_id,
                    admission_allowed=admission_allowed,
                )
        a, e = prepared.admission, prepared.event
        if await session.get(OrdinaryTurnAdmissionModel, e.id) is not None:
            return False
        # Only indexed source/owner checks remain under the writer; the digest and
        # binding JSON were frozen outside it. A primary-key source is not history.
        current = await session.get(ChatEventModel, e.id)
        conversation = await session.get(CanonicalConversationModel, a.conversation_id)
        fields = (
            "content",
            "visual_summary",
            "audio_transcript",
            "author_person_id",
            "author_kind",
            "suppression_status",
            "canonical_conversation_id",
            "caused_by_event_id",
            "reply_to_event_id",
            "ingress_presence_id",
            "direction",
            "event_kind",
        )
        if (
            current is None
            or conversation is None
            or conversation.generation != a.generation
            or e.id <= conversation.starts_after_event_id
            or current.suppression_status != "keeper"
            or any(getattr(current, name) != getattr(e, name) for name in fields)
            or _event_record(current).segments != e.segments
            or not await _identity_allowed(session, current, conversation)
        ):
            raise OrdinaryAdmissionConflict("ordinary_source_changed")
        if admission_allowed is not None and not await admission_allowed(session):
            return False
        binding = a.binding
        result = await session.execute(
            insert(OrdinaryTurnAdmissionModel)
            .values(
                event_id=a.event_id,
                conversation_id=a.conversation_id,
                generation=a.generation,
                source_revision=a.source_revision,
                actor_person_id=a.actor_person_id,
                presence_id=a.presence_id,
                activation_id=a.activation_id,
                coordinator_version=a.coordinator_version,
                unit_key=binding.unit_key if binding else None,
                target_hint=binding.target_hint if binding else None,
                basis_json=prepared.basis_json,
                route="work" if input_id is not None else "ordinary",
                work_id=work_id,
                input_id=input_id,
                created=time.time(),
            )
            .on_conflict_do_nothing(index_elements=[OrdinaryTurnAdmissionModel.event_id])
            .returning(OrdinaryTurnAdmissionModel.event_id)
        )
        return result.scalar_one_or_none() is not None


async def _identity_allowed(
    session: AsyncSession, event: ChatEventModel, conversation: CanonicalConversationModel
) -> bool:
    person = await session.get(CanonicalPersonModel, event.author_person_id)
    presence = await session.get(PresenceModel, event.ingress_presence_id)
    if person is None or not person.enabled or presence is None or not presence.enabled:
        return False
    root = await session.get(CanonicalPersonModel, await current_person_id(session, person.id))
    if root is None or not root.enabled:
        return False
    identity = await session.scalar(
        select(IdentityBindingModel.id).where(
            IdentityBindingModel.person_id == person.id,
            IdentityBindingModel.platform == presence.platform,
            IdentityBindingModel.external_account_id == event.sender_user_id,
            IdentityBindingModel.status == "active",
        )
    )
    if identity is None or presence.external_account_id != event.bot_user_id:
        return False
    if conversation.kind == "private":
        return conversation.person_id == person.id and person.enabled
    space = await session.get(CanonicalSpaceModel, conversation.space_id)
    if space is None or not space.enabled:
        return False
    return (
        await session.scalar(
            select(SpaceBindingModel.id).where(
                SpaceBindingModel.space_id == space.id,
                SpaceBindingModel.platform == presence.platform,
                SpaceBindingModel.external_space_id == event.group_id,
                SpaceBindingModel.status == "active",
            )
        )
        is not None
    )
