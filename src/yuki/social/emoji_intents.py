"""Persist optional selection inputs without duplicating Social delivery facts."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import or_, select, true, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncSession

from yuki.conversation.canonical_db_models import CanonicalConversationModel
from yuki.identity.person_identity import person_enabled_condition
from yuki.persistence.database import Database
from yuki.persistence.models import ChatEventModel
from yuki.persistence.unit_of_work import optional_session
from yuki.runtime.subagent_schema import children
from yuki.runtime.work_schema_v1 import scope, work
from yuki.social.db_models import SocialEmojiIntentModel, SocialOperationModel
from yuki.social.models import SocialError, SocialTarget
from yuki.social.repository import payload_hash
from yuki.social.source_keys import social_call_key, social_source_key

if TYPE_CHECKING:
    from yuki.runtime.effect_outcomes import ResultCapture


def encode(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def derived_call_id(identity: str) -> str:
    return f"emoji:{identity}"


@dataclass(frozen=True)
class EmojiIntent:
    id: str
    source_turn_id: str
    tool_call_id: str
    conversation_id: str
    target: SocialTarget
    payload_hash: str
    source: dict[str, Any]
    intent: dict[str, Any]
    selected: dict[str, Any] | None
    closed: bool
    error_category: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class PreparedEmojiIntent:
    id: str
    turn_id: str
    call_id: str
    conversation_id: str
    target: SocialTarget
    payload_hash: str
    source: dict[str, Any]
    source_json: str
    intent_json: str
    has_text: bool


def project_admission(identity: str, error_category: str | None) -> dict[str, Any]:
    return {
        "intent_id": identity,
        "status": "unknown"
        if error_category == "file_mutation_unknown_read_current_version"
        else "failed"
        if error_category is not None
        else "accepted",
        **({"error": error_category} if error_category is not None else {}),
    }


def project_intent(intent: EmojiIntent, receipt: dict[str, Any] | None) -> dict[str, Any]:
    publication_unknown = intent.error_category == "file_mutation_unknown_read_current_version"
    if receipt is not None:
        parts = receipt.get("parts", (receipt.get("file", receipt),))
        dispatched = any(part["status"] not in {"prepared", "not_sent"} for part in parts)
        if not dispatched and intent.error_category is not None:
            return {
                "intent_id": intent.id,
                "status": "unknown" if publication_unknown else "not_sent",
                "operation_id": receipt["operation_id"],
                "error": intent.error_category,
            }
        if dispatched:
            return {"intent_id": intent.id, **receipt}
    return project_admission(intent.id, intent.error_category)


async def validate_source(session: AsyncSession, source: dict[str, Any]) -> None:
    """Recheck revocation, never the lifetime of an already completed activation."""
    context = source["context"]
    conversation = await session.get(CanonicalConversationModel, context["conversation_id"])
    if conversation is None or conversation.generation != source["generation"]:
        raise SocialError("emoji_source_obsolete")
    cancel_epoch = await session.scalar(
        select(scope.c.cancel_epoch).where(scope.c.conversation_id == conversation.id)
    )
    if (cancel_epoch or 0) != source["cancel_epoch"]:
        raise SocialError("emoji_source_cancelled")
    work_id = source.get("work_id")
    if work_id is not None:
        original = (
            (await session.execute(select(work.c.state).where(work.c.id == work_id)))
            .mappings()
            .first()
        )
        child_epoch = await session.scalar(
            select(children.c.cancel_epoch).where(children.c.work_id == work_id)
        )
        if (
            original is None
            or original["state"] == "cancelled"
            or child_epoch != source.get("work_cancel_epoch")
        ):
            raise SocialError("emoji_source_cancelled")
    event_id = context.get("trigger_event_id") or context.get("caused_by_event_id")
    if event_id is not None:
        event = await session.get(ChatEventModel, event_id)
        if event is None or event.canonical_conversation_id != conversation.id:
            raise SocialError("emoji_source_removed")
    actor = context.get("actor")
    person_id = actor.get("person_id") if actor is not None else None
    if person_id is not None and not await session.scalar(
        select(person_enabled_condition(person_id))
    ):
        raise SocialError("permission_denied")


class EmojiIntentRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    @staticmethod
    def record(row: SocialEmojiIntentModel) -> EmojiIntent:
        return EmojiIntent(
            id=row.id,
            source_turn_id=row.source_turn_id,
            tool_call_id=row.tool_call_id,
            conversation_id=row.source_conversation_id,
            target=SocialTarget(kind=row.target_kind, id=row.target_id),
            payload_hash=row.payload_hash,
            source=json.loads(row.source_json),
            intent=json.loads(row.intent_json),
            selected=json.loads(row.selected_json) if row.selected_json is not None else None,
            closed=row.closed,
            error_category=row.error_category,
            created_at=row.created_at,
        )

    async def find(self, turn_id: str, call_id: str) -> EmojiIntent | None:
        async with self.database.read_session() as session:
            row = await session.scalar(
                select(SocialEmojiIntentModel).where(
                    SocialEmojiIntentModel.source_turn_id == social_source_key(turn_id),
                    SocialEmojiIntentModel.tool_call_id == social_call_key(call_id),
                )
            )
            return self.record(row) if row is not None else None

    async def get(self, identity: str, *, session: AsyncSession) -> EmojiIntent:
        row = await session.get(SocialEmojiIntentModel, identity)
        if row is None:
            raise SocialError("emoji_source_removed")
        return self.record(row)

    @staticmethod
    def prepare(
        *,
        turn_id: str,
        call_id: str,
        conversation_id: str,
        target: SocialTarget,
        arguments: dict[str, Any],
        source: dict[str, Any],
    ) -> PreparedEmojiIntent:
        turn_id, call_id = social_source_key(turn_id), social_call_key(call_id)
        return PreparedEmojiIntent(
            str(uuid5(NAMESPACE_URL, encode(["social-emoji", turn_id, call_id]))),
            turn_id,
            call_id,
            conversation_id,
            target,
            payload_hash("send_message", target, arguments, conversation_id),
            source,
            encode(source),
            encode(arguments["emoji"]),
            bool(arguments.get("text", "").strip()),
        )

    async def admit(
        self,
        *,
        prepared: PreparedEmojiIntent,
        work_effect: ResultCapture | None = None,
        session: AsyncSession | None = None,
    ) -> dict[str, Any]:
        transaction = (
            self.database.immediate_session()
            if session is None
            else optional_session(self.database, session, write=True)
        )
        async with transaction as session:
            existing = (
                await session.execute(
                    select(
                        SocialEmojiIntentModel.id,
                        SocialEmojiIntentModel.payload_hash,
                        SocialEmojiIntentModel.error_category,
                    ).where(
                        SocialEmojiIntentModel.source_turn_id == prepared.turn_id,
                        SocialEmojiIntentModel.tool_call_id == prepared.call_id,
                    )
                )
            ).first()
            if existing is not None:
                if existing.payload_hash != prepared.payload_hash:
                    raise SocialError("idempotency_conflict")
                identity, error = existing.id, existing.error_category
            else:
                await validate_source(session, prepared.source)
                now = datetime.now(UTC)
                await session.execute(
                    insert(SocialEmojiIntentModel).values(
                        id=prepared.id,
                        source_turn_id=prepared.turn_id,
                        tool_call_id=prepared.call_id,
                        source_conversation_id=prepared.conversation_id,
                        payload_hash=prepared.payload_hash,
                        target_kind=prepared.target.kind,
                        target_id=str(prepared.target.id),
                        source_json=prepared.source_json,
                        intent_json=prepared.intent_json,
                        closed=False,
                        created_at=now,
                        updated_at=now,
                    )
                )
                identity, error = prepared.id, None
            if work_effect is not None:
                from yuki.runtime.work_repository import WorkRepository

                await WorkRepository.bind_domain_receipt(
                    session, work_effect.work_id, work_effect.effect_key, f"social-emoji:{identity}"
                )
        return project_admission(identity, error)

    @staticmethod
    async def effect_facts(
        session: AsyncSession,
        identity: str,
        *,
        conversation_id: str,
        work_id: str,
        call_id: str,
    ) -> dict[str, Any] | None:
        row = await session.get(SocialEmojiIntentModel, identity)
        if row is None or (
            row.source_conversation_id != conversation_id
            or row.tool_call_id != social_call_key(call_id)
            or json.loads(row.source_json).get("work_id") != work_id
        ):
            return None
        intent = EmojiIntentRepository.record(row)
        return {
            "status": "accepted",
            "emoji": await EmojiIntentRepository.project(session, intent),
        }

    @staticmethod
    async def project(session: AsyncSession, intent: EmojiIntent) -> dict[str, Any]:
        operation = await session.scalar(
            select(SocialOperationModel).where(
                SocialOperationModel.source_turn_id == intent.source_turn_id,
                SocialOperationModel.tool_call_id == derived_call_id(intent.id),
            )
        )
        from yuki.social.repository import SocialOperationRepository

        facts = (
            await SocialOperationRepository.delivery_facts(
                session,
                operation.id,
                conversation_id=intent.conversation_id,
                tool_call_id=derived_call_id(intent.id),
                planned_file_parts=1,
            )
            if operation is not None
            else None
        )
        return project_intent(intent, facts)

    async def pending(self, after: tuple[datetime, str] | None = None) -> tuple[EmojiIntent, ...]:
        async with self.database.read_session() as session:
            while True:
                cursor = (
                    or_(
                        SocialEmojiIntentModel.created_at > after[0],
                        (SocialEmojiIntentModel.created_at == after[0])
                        & (SocialEmojiIntentModel.id > after[1]),
                    )
                    if after is not None
                    else true()
                )
                rows = list(
                    await session.scalars(
                        select(SocialEmojiIntentModel)
                        .where(SocialEmojiIntentModel.closed.is_(False), cursor)
                        .order_by(SocialEmojiIntentModel.created_at, SocialEmojiIntentModel.id)
                        .limit(100)
                    )
                )
                ready = []
                for row in rows:
                    intent = self.record(row)
                    finished = await self._text_dispatch_finished(session, intent)
                    if not finished:
                        try:
                            await validate_source(session, intent.source)
                        except SocialError:
                            # Revocation is settled even when the original text
                            # connection or an unsent later step is still pending.
                            finished = True
                    if finished:
                        ready.append(intent)
                if ready or not rows:
                    return tuple(ready)
                after = (rows[-1].created_at, rows[-1].id)

    @staticmethod
    async def _text_dispatch_finished(session: AsyncSession, intent: EmojiIntent) -> bool:
        """Order against the original text steps, independently of their success."""
        operation_id = intent.source.get("after_operation_id")
        if operation_id is None:
            return True
        parent = await session.get(SocialOperationModel, operation_id)
        if parent is None:
            return True
        if parent.status in {"succeeded", "failed", "uncertain"}:
            return True
        if parent.action != "send_message_sequence":
            return parent.status not in {"prepared", "executing"}
        from yuki.social.repository import sequence_prefix

        prefix = sequence_prefix(parent.tool_call_id)
        statuses = list(
            await session.scalars(
                select(SocialOperationModel.status).where(
                    SocialOperationModel.source_turn_id == parent.source_turn_id,
                    SocialOperationModel.tool_call_id.in_(
                        f"{prefix}{index}" for index in range(parent.planned_parts or 0)
                    ),
                )
            )
        )
        return "executing" not in statuses and (
            any(status in {"failed", "uncertain"} for status in statuses)
            or (
                bool(statuses)
                and len(statuses) == parent.planned_parts
                and all(status == "succeeded" for status in statuses)
            )
        )

    async def select(self, identity: str, selected: dict[str, Any]) -> EmojiIntent:
        selected_json = encode(selected)
        async with self.database.immediate_session() as session:
            await session.execute(
                update(SocialEmojiIntentModel)
                .where(
                    SocialEmojiIntentModel.id == identity,
                    SocialEmojiIntentModel.selected_json.is_(None),
                    SocialEmojiIntentModel.closed.is_(False),
                )
                .values(selected_json=selected_json, updated_at=datetime.now(UTC))
            )
            row = await session.get(SocialEmojiIntentModel, identity)
        if row is None:
            raise SocialError("emoji_source_removed")
        return self.record(row)

    async def close(
        self,
        identity: str,
        error_category: str | None = None,
        *,
        unselected_only: bool = False,
    ) -> EmojiIntent:
        async with self.database.immediate_session() as session:
            await session.execute(
                update(SocialEmojiIntentModel)
                .where(
                    SocialEmojiIntentModel.id == identity,
                    SocialEmojiIntentModel.closed.is_(False),
                    SocialEmojiIntentModel.selected_json.is_(None) if unselected_only else true(),
                )
                .values(closed=True, error_category=error_category, updated_at=datetime.now(UTC))
            )
            row = await session.get(SocialEmojiIntentModel, identity)
        if row is None:
            raise SocialError("emoji_source_removed")
        return self.record(row)
