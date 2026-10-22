"""Durable claims for social effects; uncertainty is never a retry instruction."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID, uuid4

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from yuki.conversation.canonical_db_models import CanonicalConversationModel
from yuki.identity.db_models import CanonicalPersonModel, CanonicalSpaceModel
from yuki.persistence.database import Database
from yuki.persistence.models import ChatEventModel
from yuki.persistence.unit_of_work import optional_session
from yuki.social.db_models import SocialEmojiIntentModel, SocialOperationModel
from yuki.social.models import OperationStatus, SocialError, SocialReceipt, SocialTarget
from yuki.social.source_keys import social_call_key, social_source_key

if TYPE_CHECKING:
    from yuki.runtime.effect_outcomes import ResultCapture

_ACTIONS = frozenset(
    {
        "send_message",
        "send_message_sequence",
        "send_file_caption",
        "poke_person",
        "recall_own_message",
    }
)


def sequence_prefix(tool_call_id: str) -> str:
    """The stable child-key prefix of one send_message_sequence parent."""
    return f"seq:{hashlib.sha256(tool_call_id.encode()).hexdigest()[:24]}:"


def payload_hash(
    action: str, target: SocialTarget, payload: dict[str, Any], conversation_id: str
) -> str:
    encoded = json.dumps(
        {
            "action": action,
            "target": target.model_dump(mode="json"),
            "payload": payload,
            "source_conversation_id": conversation_id,
        },
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


class SocialOperationRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    @staticmethod
    async def _same_target(
        session: AsyncSession, row: SocialOperationModel, target: SocialTarget
    ) -> bool:
        if row.target_kind != target.kind:
            return False
        if row.target_id == str(target.id):
            return True
        if target.kind != "person":
            return False
        from yuki.identity.person_identity import same_person_id

        return await same_person_id(session, row.target_id, str(target.id))

    @staticmethod
    async def _matches_payload(
        session: AsyncSession,
        row: SocialOperationModel,
        *,
        action: str,
        target: SocialTarget,
        payload: dict[str, Any],
        conversation_id: str,
    ) -> bool:
        return await SocialOperationRepository._same_target(session, row, target) and (
            row.payload_hash
            == payload_hash(
                action,
                SocialTarget(kind=row.target_kind, id=UUID(row.target_id)),
                payload,
                conversation_id,
            )
        )

    async def prepare(
        self,
        *,
        source_turn_id: str,
        tool_call_id: str,
        source_conversation_id: str,
        action: str,
        target: SocialTarget,
        payload: dict[str, Any],
        planned_parts: int | None = None,
        work_effect: ResultCapture | None = None,
        prepared: dict[str, Any] | None = None,
    ) -> SocialReceipt:
        if action not in _ACTIONS or not source_turn_id or not tool_call_id:
            raise SocialError("invalid_operation")
        tool_call_id = social_call_key(tool_call_id)
        if planned_parts is not None and (
            action != "send_message_sequence" or type(planned_parts) is not int or planned_parts < 1
        ):
            raise SocialError("invalid_operation")
        source_turn_id = social_source_key(source_turn_id)
        digest = payload_hash(action, target, payload, source_conversation_id)
        async with self.database.sessions() as reader:
            await reader.execute(text("BEGIN"))
            existing = await reader.scalar(
                select(SocialOperationModel).where(
                    SocialOperationModel.source_turn_id == source_turn_id,
                    SocialOperationModel.tool_call_id == tool_call_id,
                )
            )
            if existing is not None:
                if existing.planned_parts != planned_parts or not await self._matches_payload(
                    reader,
                    existing,
                    action=action,
                    target=target,
                    payload=payload,
                    conversation_id=source_conversation_id,
                ):
                    raise SocialError("idempotency_conflict")
                if work_effect is None:
                    return self._receipt(existing)
        now = datetime.now(UTC)
        async with self.database.sessions() as session, session.begin():
            target_row = await session.get(
                CanonicalPersonModel if target.kind == "person" else CanonicalSpaceModel,
                str(target.id),
            )
            if target_row is None:
                raise SocialError("target_not_found")
            await session.execute(
                insert(SocialOperationModel)
                .values(
                    id=str(uuid4()),
                    source_turn_id=source_turn_id,
                    tool_call_id=tool_call_id,
                    source_conversation_id=source_conversation_id,
                    action=action,
                    payload_hash=digest,
                    planned_parts=planned_parts,
                    prepared_json=json.dumps(prepared, ensure_ascii=False)
                    if prepared is not None
                    else None,
                    target_kind=target.kind,
                    target_id=str(target.id),
                    status=OperationStatus.PREPARED.value,
                    created_at=now,
                    updated_at=now,
                )
                .on_conflict_do_nothing(index_elements=["source_turn_id", "tool_call_id"])
            )
            row = await session.scalar(
                select(SocialOperationModel).where(
                    SocialOperationModel.source_turn_id == source_turn_id,
                    SocialOperationModel.tool_call_id == tool_call_id,
                )
            )
            assert row is not None
            if row.planned_parts != planned_parts or not await self._matches_payload(
                session,
                row,
                action=action,
                target=target,
                payload=payload,
                conversation_id=source_conversation_id,
            ):
                raise SocialError("idempotency_conflict")
            if work_effect is not None:
                from yuki.runtime.work_repository import WorkRepository

                await WorkRepository.bind_domain_receipt(
                    session, work_effect.work_id, work_effect.effect_key, f"social:{row.id}"
                )
            return self._receipt(row)

    async def prepare_sequence(
        self,
        *,
        source_turn_id: str,
        tool_call_id: str,
        source_conversation_id: str,
        target: SocialTarget,
        payload: dict[str, Any],
        parts: tuple[dict[str, Any], ...],
        work_effect: ResultCapture | None = None,
        prepared: dict[str, Any] | None = None,
    ) -> tuple[SocialReceipt, bool]:
        """Freeze the physical plan once; later callers read the same original parts."""
        source_turn_id = social_source_key(source_turn_id)
        tool_call_id = social_call_key(tool_call_id)
        digest = payload_hash("send_message_sequence", target, payload, source_conversation_id)
        prefix = sequence_prefix(tool_call_id)
        async with self.database.immediate_session() as session:
            row = await session.scalar(
                select(SocialOperationModel).where(
                    SocialOperationModel.source_turn_id == source_turn_id,
                    SocialOperationModel.tool_call_id == tool_call_id,
                )
            )
            if row is not None and await self._same_target(session, row, target):
                # A Person merge changes current authority, never the original
                # operation target, payload digest or child identities.
                target = SocialTarget(kind=row.target_kind, id=UUID(row.target_id))
                digest = payload_hash(
                    "send_message_sequence", target, payload, source_conversation_id
                )
            children = list(
                await session.scalars(
                    select(SocialOperationModel).where(
                        SocialOperationModel.source_turn_id == source_turn_id,
                        SocialOperationModel.tool_call_id.startswith(prefix),
                        SocialOperationModel.action == "send_message",
                    )
                )
            )
            children_by_key = {item.tool_call_id: item for item in children}
            if row is None:
                now = datetime.now(UTC)
                row = SocialOperationModel(
                    id=str(uuid4()),
                    source_turn_id=source_turn_id,
                    tool_call_id=tool_call_id,
                    source_conversation_id=source_conversation_id,
                    action="send_message_sequence",
                    payload_hash=digest,
                    planned_parts=len(parts),
                    prepared_json=json.dumps(prepared, ensure_ascii=False)
                    if prepared is not None
                    else None,
                    target_kind=target.kind,
                    target_id=str(target.id),
                    status=OperationStatus.PREPARED.value,
                    created_at=now,
                    updated_at=now,
                )
                session.add(row)
                for index, part in enumerate(parts):
                    session.add(
                        SocialOperationModel(
                            id=str(uuid4()),
                            source_turn_id=source_turn_id,
                            tool_call_id=f"{prefix}{index}",
                            source_conversation_id=source_conversation_id,
                            action="send_message",
                            payload_hash=payload_hash(
                                "send_message", target, part, source_conversation_id
                            ),
                            target_kind=target.kind,
                            target_id=str(target.id),
                            status=OperationStatus.PREPARED.value,
                            created_at=now,
                            updated_at=now,
                        )
                    )
                dispatch = True
            else:
                if (
                    row.source_conversation_id != source_conversation_id
                    or row.target_kind != target.kind
                    or row.target_id != str(target.id)
                    or row.action != "send_message_sequence"
                    or row.payload_hash != digest
                    or row.planned_parts != len(parts)
                ):
                    raise SocialError("idempotency_conflict")
                for index, part in enumerate(parts):
                    child = children_by_key.get(f"{prefix}{index}")
                    if child is None or child.payload_hash != payload_hash(
                        "send_message", target, part, source_conversation_id
                    ):
                        raise SocialError("idempotency_conflict")
                dispatch = row.status == "prepared" and all(
                    child.status == "prepared" for child in children
                )
            if work_effect is not None:
                from yuki.runtime.work_repository import WorkRepository

                await WorkRepository.bind_domain_receipt(
                    session, work_effect.work_id, work_effect.effect_key, f"social:{row.id}"
                )
            return self._receipt(row), dispatch

    async def claim(
        self,
        operation_id: str,
        *,
        presence_id: str,
        session: AsyncSession | None = None,
        expected_payload_hash: str | None = None,
        parent_operation_id: str | None = None,
        sequence_part_index: int | None = None,
    ) -> bool:
        """Commit the send boundary before issuing any network action."""
        async with optional_session(self.database, session, write=True) as active:
            if parent_operation_id is not None and sequence_part_index is not None:
                parent = await active.get(SocialOperationModel, parent_operation_id)
                if parent is None or sequence_part_index >= (parent.planned_parts or 0):
                    return False
            predicates = [
                SocialOperationModel.id == operation_id,
                SocialOperationModel.status == OperationStatus.PREPARED.value,
            ]
            if expected_payload_hash is not None:
                predicates.append(SocialOperationModel.payload_hash == expected_payload_hash)
            result = await active.execute(
                update(SocialOperationModel)
                .where(*predicates)
                .values(
                    status=OperationStatus.EXECUTING.value,
                    presence_id=presence_id,
                    updated_at=datetime.now(UTC),
                )
            )
            return cast(CursorResult[Any], result).rowcount == 1

    async def stop_sequence(self, operation_id: str, error_category: str) -> None:
        """Record a real preflight exit without claiming any unsent child step."""
        async with self.database.immediate_session() as session:
            await session.execute(
                update(SocialOperationModel)
                .where(
                    SocialOperationModel.id == operation_id,
                    SocialOperationModel.action == "send_message_sequence",
                    SocialOperationModel.status == "prepared",
                )
                .values(
                    status="failed",
                    error_category=error_category,
                    updated_at=datetime.now(UTC),
                )
            )

    async def finish(
        self,
        operation_id: str,
        *,
        status: OperationStatus,
        session: AsyncSession,
        platform_reference: str | None = None,
        event_id: int | None = None,
        error_category: str | None = None,
    ) -> SocialReceipt:
        """Caller commits a confirmed outgoing ledger append in this same transaction."""
        if status not in {
            OperationStatus.SUCCEEDED,
            OperationStatus.FAILED,
            OperationStatus.UNCERTAIN,
        }:
            raise SocialError("invalid_transition")
        if error_category is not None and (
            not error_category
            or len(error_category) > 64
            or not all(c.isascii() and (c.isalnum() or c == "_") for c in error_category)
        ):
            raise SocialError("invalid_error_category")
        if platform_reference is not None and len(platform_reference) > 255:
            raise SocialError("invalid_platform_reference")
        if event_id is not None and (
            type(event_id) is not int or event_id <= 0 or status is not OperationStatus.SUCCEEDED
        ):
            raise SocialError("invalid_event_reference")
        result = await session.execute(
            update(SocialOperationModel)
            .where(
                SocialOperationModel.id == operation_id,
                SocialOperationModel.status == OperationStatus.EXECUTING.value,
            )
            .values(
                status=status.value,
                platform_reference=platform_reference,
                event_id=event_id,
                original_event_id=event_id,
                original_conversation_id=select(ChatEventModel.canonical_conversation_id)
                .where(ChatEventModel.id == event_id)
                .scalar_subquery()
                if event_id is not None
                else None,
                error_category=error_category,
                updated_at=datetime.now(UTC),
            )
            .returning(SocialOperationModel)
        )
        row = result.scalar_one_or_none()
        if row is None:
            raise SocialError("invalid_transition")
        return self._receipt(row)

    async def get(self, operation_id: str) -> SocialReceipt:
        async with self.database.sessions() as session:
            row = await session.get(SocialOperationModel, operation_id)
            if row is None:
                raise SocialError("operation_not_found")
            return self._receipt(row)

    async def find(self, source_turn_id: str, tool_call_id: str) -> SocialReceipt | None:
        source_turn_id = social_source_key(source_turn_id)
        tool_call_id = social_call_key(tool_call_id)
        async with self.database.sessions() as session:
            row = await session.scalar(
                select(SocialOperationModel).where(
                    SocialOperationModel.source_turn_id == source_turn_id,
                    SocialOperationModel.tool_call_id == tool_call_id,
                )
            )
            return self._receipt(row) if row is not None else None

    async def recover_interrupted(self) -> int:
        """Startup-only recovery, before admitting turns; never execute old payloads."""
        # Only live illustration dependencies need an interrupted text owner.
        # Prepare their original plan before any UPDATE acquires the writer.
        interrupted = []
        async with self.database.sessions() as reader:
            parents = await reader.scalars(
                select(SocialOperationModel)
                .join(
                    SocialEmojiIntentModel,
                    SocialOperationModel.id
                    == func.json_extract(
                        SocialEmojiIntentModel.source_json, "$.after_operation_id"
                    ),
                )
                .where(
                    SocialEmojiIntentModel.closed.is_(False),
                    SocialOperationModel.action == "send_message_sequence",
                    SocialOperationModel.status == "prepared",
                )
                .distinct()
            )
            for parent in parents:
                prefix = sequence_prefix(parent.tool_call_id)
                keys = tuple(f"{prefix}{index}" for index in range(parent.planned_parts or 0))
                statuses = list(
                    await reader.scalars(
                        select(SocialOperationModel.status).where(
                            SocialOperationModel.source_turn_id == parent.source_turn_id,
                            SocialOperationModel.tool_call_id.in_(keys),
                        )
                    )
                )
                if "prepared" in statuses and any(status != "prepared" for status in statuses):
                    interrupted.append((parent.id, parent.source_turn_id, keys))
        async with self.database.sessions() as session, session.begin():
            result = await session.execute(
                update(SocialOperationModel)
                .where(
                    SocialOperationModel.status == OperationStatus.EXECUTING.value,
                )
                .values(
                    status=OperationStatus.UNCERTAIN.value,
                    error_category="process_interrupted",
                    updated_at=datetime.now(UTC),
                )
            )
            recovered = cast(CursorResult[Any], result).rowcount
            steps = SocialOperationModel.__table__.alias("original_text_step")
            for identity, source_turn_id, keys in interrupted:
                original = select(steps.c.id).where(
                    steps.c.source_turn_id == source_turn_id,
                    steps.c.tool_call_id.in_(keys),
                )
                stopped = await session.execute(
                    update(SocialOperationModel)
                    .where(
                        SocialOperationModel.id == identity,
                        SocialOperationModel.action == "send_message_sequence",
                        SocialOperationModel.status == "prepared",
                        SocialOperationModel.planned_parts == len(keys),
                        original.where(steps.c.status == "prepared").exists(),
                        original.where(steps.c.status != "prepared").exists(),
                    )
                    .values(
                        status="failed",
                        error_category="process_interrupted",
                        updated_at=datetime.now(UTC),
                    )
                )
                recovered += cast(CursorResult[Any], stopped).rowcount
            return recovered

    @staticmethod
    async def delivery_facts(
        session: AsyncSession,
        operation_id: str,
        *,
        conversation_id: str,
        tool_call_id: str,
        planned_file_parts: int | None,
        origin: str | None = None,
        caused_by_event_id: int | None = None,
    ) -> dict[str, Any] | None:
        """Complete delivery facts of one original operation in the caller's snapshot.

        The prospective plan is the parent's ``planned_parts`` (a sequence) or the
        caller's reserved file+caption count; a missing plan is never inferred from
        surviving successes. Absent planned parts read as ``not_sent``.
        """
        parent = await session.get(SocialOperationModel, operation_id)
        if parent is None or (
            parent.source_conversation_id != conversation_id
            or parent.tool_call_id != social_call_key(tool_call_id)
        ):
            return None

        def receipt(row: SocialOperationModel | None, *, detailed: bool = False) -> dict[str, Any]:
            if row is None:
                return {"status": "not_sent"}
            if detailed:
                value = SocialOperationRepository._receipt(row).model_dump(mode="json")
                value.pop("platform_reference", None)
                if row.status == "succeeded":
                    value["delivered_text_available"] = False
                return value
            return {
                "operation_id": row.id,
                "status": row.status,
                "target": {"kind": row.target_kind, "id": row.target_id},
                "event_id": row.event_id,
                "original_event_id": row.original_event_id,
                "original_conversation_id": row.original_conversation_id,
                "error_category": row.error_category,
            }

        result = receipt(parent)
        prepared = json.loads(parent.prepared_json) if parent.prepared_json is not None else None
        operations = {parent.id: parent}
        parts = [receipt(parent, detailed=origin is not None)]
        complete_plan = True
        if parent.action == "send_message_sequence":
            count = parent.planned_parts
            if count is None or count < 1:
                result["status"] = "uncertain"
                return result
            prefix = sequence_prefix(parent.tool_call_id)
            children = {
                row.tool_call_id: row
                for row in await session.scalars(
                    select(SocialOperationModel).where(
                        SocialOperationModel.source_turn_id == parent.source_turn_id,
                        SocialOperationModel.source_conversation_id == conversation_id,
                        SocialOperationModel.target_kind == parent.target_kind,
                        SocialOperationModel.target_id == parent.target_id,
                        SocialOperationModel.action == "send_message",
                        SocialOperationModel.tool_call_id.startswith(prefix),
                    )
                )
            }
            operations.update((row.id, row) for row in children.values())
            parts = [
                receipt(children.get(f"{prefix}{index}"), detailed=origin is not None)
                for index in range(count)
            ]
            result.update(
                planned_messages=count,
                sent_messages=sum(part["status"] == "succeeded" for part in parts),
                parts=parts,
            )
        elif prepared is not None and "parts" in prepared:
            # Current direct sends freeze their complete one-part transport plan.
            # A Work reservation describes ownership, not another caption send.
            result["parts"] = parts
        elif planned_file_parts is not None:
            result["file"] = parts[0]
            if planned_file_parts > 1:
                caption = await session.scalar(
                    select(SocialOperationModel).where(
                        SocialOperationModel.source_turn_id == f"social-caption:{parent.id}",
                        SocialOperationModel.tool_call_id == "caption",
                        SocialOperationModel.source_conversation_id == conversation_id,
                        SocialOperationModel.target_kind == parent.target_kind,
                        SocialOperationModel.target_id == parent.target_id,
                        SocialOperationModel.action == "send_file_caption",
                    )
                )
                result["caption"] = receipt(caption, detailed=origin is not None)
                result["caption"]["text_planned"] = True
                parts.append(result["caption"])
                if caption is not None:
                    operations[caption.id] = caption
        elif parent.action == "send_message":
            # An unplanned legacy file/caption cannot prove whole-call success.
            complete_plan = False
        if prepared is not None and ("parts" in prepared or "message" in prepared):
            layouts = prepared.get("parts") or [prepared["message"]]
            attachment = prepared.get("artifact")
            # Historical file/caption receipts have a separate caption operation,
            # rather than the current parent's complete transport plan.
            planned_receipts = parts if parent.action == "send_message_sequence" else parts[:1]
            for part, layout in zip(planned_receipts, layouts, strict=True):
                part["text_planned"] = bool(layout["text"])
                if layout["media"] and attachment is not None:
                    part.update(attachment)
            if attachment is not None:
                result.update(attachment)
        event_ids = [part["event_id"] for part in parts if part.get("event_id") is not None]
        selected_events = func.json_each(json.dumps(event_ids)).table_valued("value")
        anchors = (
            {
                event.id: (event, conversation)
                for event, conversation in await session.execute(
                    select(ChatEventModel, CanonicalConversationModel)
                    .outerjoin(
                        CanonicalConversationModel,
                        CanonicalConversationModel.id == ChatEventModel.canonical_conversation_id,
                    )
                    .where(ChatEventModel.id.in_(select(selected_events.c.value)))
                )
            }
            if event_ids
            else {}
        )
        for part in parts:
            if part["status"] != "succeeded" or part.get("event_id") is None:
                continue
            event, conversation = anchors.get(part["event_id"], (None, None))
            operation = operations[part["operation_id"]]
            if (
                event is None
                or event.direction != "outbound"
                or event.author_kind != "yuki"
                or event.author_presence_id != operation.presence_id
                or event.event_kind != "message"
                or event.suppression_status != "keeper"
                or (
                    origin is not None
                    and (event.origin != origin or event.caused_by_event_id != caused_by_event_id)
                )
            ):
                continue
            if (
                conversation is None
                or (
                    conversation.space_id
                    if parent.target_kind == "space"
                    else conversation.person_id
                )
                != parent.target_id
            ):
                continue
            # This is an original confirmed ledger anchor, never arguments or
            # a prospective media position inferred from sequence status.
            try:
                segments = json.loads(event.segments_json)
            except (ValueError, TypeError):
                continue
            if not isinstance(segments, list):
                continue
            part["delivered_text_available"] = True
            part["delivered_text"] = (
                event.content
                if any(isinstance(item, dict) and item.get("type") == "record" for item in segments)
                else "".join(
                    item.get("data", {}).get("text", "")
                    for item in segments
                    if isinstance(item, dict) and item.get("type") == "text"
                )
            )
            for item in segments:
                if not isinstance(item, dict) or item.get("type") not in {"file", "image"}:
                    continue
                data = item.get("data", {})
                if isinstance(data, dict) and isinstance(data.get("artifact_id"), str):
                    part.update(artifact_id=data["artifact_id"], attachment_kind=item["type"])
        if parent.action == "send_message":
            result.update(parts[0])
        result["status"] = (
            "succeeded"
            if complete_plan and all(p["status"] == "succeeded" for p in parts)
            else "uncertain"
            if not complete_plan
            or any(p["status"] in {"prepared", "executing", "uncertain", "not_sent"} for p in parts)
            else "failed"
        )
        return result

    @staticmethod
    def _receipt(row: SocialOperationModel) -> SocialReceipt:
        return SocialReceipt(
            operation_id=row.id,
            action=row.action,
            status=OperationStatus(row.status),
            target=SocialTarget.model_validate({"kind": row.target_kind, "id": row.target_id}),
            presence_id=row.presence_id,
            platform_reference=row.platform_reference,
            event_id=row.event_id,
            original_event_id=row.original_event_id,
            original_conversation_id=row.original_conversation_id,
            error_category=row.error_category,
            payload_hash=row.payload_hash,
            prepared=json.loads(row.prepared_json) if row.prepared_json is not None else None,
        )
