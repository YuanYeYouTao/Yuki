"""Frozen human source and unit binding; this is admission, not a Main journal."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from uuid import uuid4

from sqlalchemy import and_, or_, select, text
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
from qq_ai_bot.conversation.initiative_sources import source_revision
from qq_ai_bot.conversation.ordinary_admission_db_models import OrdinaryTurnAdmissionModel
from qq_ai_bot.identity.db_models import (
    CanonicalPersonModel,
    CanonicalSpaceModel,
    IdentityBindingModel,
    PresenceModel,
    SpaceBindingModel,
)
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import ChatEventModel
from qq_ai_bot.persistence.repository_records import EventRecord
from qq_ai_bot.persistence.unit_of_work import optional_session


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


class OrdinaryAdmissionConflict(RuntimeError):
    pass


class OrdinaryAdmissionDuplicate(OrdinaryAdmissionConflict):
    """Rollback a competing mailbox publication; the original admission wins."""


def _dto(row: OrdinaryTurnAdmissionModel) -> OrdinaryAdmission:
    binding = None
    if row.unit_key is not None and row.target_hint is not None:
        binding = OrdinaryParticipationBinding(
            row.conversation_id,
            row.generation,
            row.event_id,
            row.source_revision,
            row.unit_key,
            row.target_hint,
            tuple((str(ref[0]), int(ref[1])) for ref in json.loads(row.basis_json)),
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
        basis_json = json.dumps(binding.basis if binding else (), separators=(",", ":"))
        if binding and (len(binding.unit_key) > 256 or len(binding.target_hint) > 256):
            raise OrdinaryAdmissionConflict("ordinary_binding_capacity")
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
            ),
            event,
            basis_json,
        )

    async def commit(
        self,
        prepared: PreparedOrdinaryAdmission,
        *,
        session: AsyncSession | None = None,
        work_id: str | None = None,
        input_id: int | None = None,
    ) -> bool:
        if session is None:
            if await self.get(prepared.event.id) is not None:
                return False
            async with self.database.immediate_session() as owned:
                return await self.commit(
                    prepared, session=owned, work_id=work_id, input_id=input_id
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
            or not await _identity_allowed(session, current, conversation)
        ):
            raise OrdinaryAdmissionConflict("ordinary_source_changed")
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
    if person is None or presence is None or not presence.enabled:
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
