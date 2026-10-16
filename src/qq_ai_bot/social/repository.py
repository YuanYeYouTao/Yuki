"""Durable claims for social effects; uncertainty is never a retry instruction."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

from sqlalchemy import select, text, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
from qq_ai_bot.identity.db_models import CanonicalPersonModel, CanonicalSpaceModel
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import ChatEventModel
from qq_ai_bot.persistence.unit_of_work import optional_session
from qq_ai_bot.social.db_models import SocialOperationModel
from qq_ai_bot.social.models import OperationStatus, SocialError, SocialReceipt, SocialTarget
from qq_ai_bot.social.source_keys import social_call_key, social_source_key

if TYPE_CHECKING:
    from qq_ai_bot.runtime.effect_outcomes import ResultCapture

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
            target_row = await reader.get(
                CanonicalPersonModel if target.kind == "person" else CanonicalSpaceModel,
                str(target.id),
            )
            if target_row is None:
                raise SocialError("target_not_found")
            existing = await reader.scalar(
                select(SocialOperationModel).where(
                    SocialOperationModel.source_turn_id == source_turn_id,
                    SocialOperationModel.tool_call_id == tool_call_id,
                )
            )
            if existing is not None:
                if existing.payload_hash != digest or existing.planned_parts != planned_parts:
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
            if row.payload_hash != digest or row.planned_parts != planned_parts:
                raise SocialError("idempotency_conflict")
            if work_effect is not None:
                from qq_ai_bot.runtime.work_repository import WorkRepository

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
        legacy_payload: dict[str, Any] | None = None,
        work_effect: ResultCapture | None = None,
    ) -> tuple[SocialReceipt, bool]:
        """Replan only steps with no dispatch fact; preserve original receipt IDs."""
        from qq_ai_bot.runtime.work_recovery_schema import deliveries
        from qq_ai_bot.runtime.work_repository import encode_json
        from qq_ai_bot.runtime.work_schema_v1 import work

        source_turn_id = social_source_key(source_turn_id)
        tool_call_id = social_call_key(tool_call_id)
        digest = payload_hash("send_message_sequence", target, payload, source_conversation_id)
        legacy = (
            payload_hash("send_message_sequence", target, legacy_payload, source_conversation_id)
            if legacy_payload is not None
            else None
        )
        prefix = sequence_prefix(tool_call_id)
        async with self.database.immediate_session() as session:
            row = await session.scalar(
                select(SocialOperationModel).where(
                    SocialOperationModel.source_turn_id == source_turn_id,
                    SocialOperationModel.tool_call_id == tool_call_id,
                )
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
                    target_kind=target.kind,
                    target_id=str(target.id),
                    status=OperationStatus.PREPARED.value,
                    created_at=now,
                    updated_at=now,
                )
                session.add(row)
            elif (
                row.source_conversation_id != source_conversation_id
                or row.target_kind != target.kind
                or row.target_id != str(target.id)
                or not (
                    (row.action == "send_message_sequence" and row.payload_hash in {digest, legacy})
                    or (
                        row.action == "send_message"
                        and row.status == "prepared"
                        and row.payload_hash
                        == payload_hash("send_message", target, payload, source_conversation_id)
                    )
                )
            ):
                raise SocialError("idempotency_conflict")
            dispatch = not any(child.status != "prepared" for child in children)
            if dispatch:

                async def replan_reservation(
                    operation_id: str, arguments: dict[str, Any], kind: str, count: int
                ) -> None:
                    intent = (
                        (
                            await session.execute(
                                select(deliveries).where(
                                    deliveries.c.id == operation_id,
                                    deliveries.c.state == "reserved",
                                )
                            )
                        )
                        .mappings()
                        .first()
                    )
                    if intent is None:
                        return
                    await session.execute(
                        update(deliveries)
                        .where(deliveries.c.id == operation_id)
                        .values(
                            payload_json=encode_json(
                                {"target": target.model_dump(mode="json"), "arguments": arguments}
                            ),
                            kind=kind,
                            message_count=count,
                        )
                    )
                    difference = count - intent["message_count"]
                    if difference:
                        await session.execute(
                            update(work)
                            .where(work.c.id == intent["work_id"])
                            .values(sent_messages=work.c.sent_messages + difference)
                        )

                if row.action == "send_message":
                    # A direct plan reserved before its first claim has no physical
                    # send to count; the new parts retain this original parent ID.
                    await replan_reservation(row.id, payload, "sequence", 0)
                row.action = "send_message_sequence"
                row.payload_hash = digest
                row.planned_parts = len(parts)
                row.updated_at = datetime.now(UTC)
                for index, part in enumerate(parts):
                    child = next(
                        (item for item in children if item.tool_call_id == f"{prefix}{index}"),
                        None,
                    )
                    if child is None:
                        now = datetime.now(UTC)
                        child = SocialOperationModel(
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
                        session.add(child)
                    else:
                        child.payload_hash = payload_hash(
                            "send_message", target, part, source_conversation_id
                        )
                        await replan_reservation(
                            child.id, part, "artifact" if part.get("artifact_id") else "message", 1
                        )
                current_keys = {f"{prefix}{index}" for index in range(len(parts))}
                for child in children:
                    if child.tool_call_id not in current_keys:
                        # The row stays as an original, never-dispatched receipt.
                        # Its removed plan cannot spend a delivery reservation.
                        intent = (
                            (
                                await session.execute(
                                    select(deliveries).where(
                                        deliveries.c.id == child.id,
                                        deliveries.c.state == "reserved",
                                    )
                                )
                            )
                            .mappings()
                            .first()
                        )
                        if intent is not None:
                            await session.execute(
                                update(deliveries)
                                .where(deliveries.c.id == child.id)
                                .values(message_count=0)
                            )
                            await session.execute(
                                update(work)
                                .where(work.c.id == intent["work_id"])
                                .values(
                                    sent_messages=work.c.sent_messages - intent["message_count"]
                                )
                            )
            if work_effect is not None:
                from qq_ai_bot.runtime.work_repository import WorkRepository

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

    async def finish(
        self,
        operation_id: str,
        *,
        status: OperationStatus,
        session: AsyncSession,
        platform_reference: str | None = None,
        event_id: int | None = None,
        error_category: str | None = None,
    ) -> None:
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
                error_category=error_category,
                updated_at=datetime.now(UTC),
            )
        )
        if cast(CursorResult[Any], result).rowcount != 1:
            raise SocialError("invalid_transition")

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
            return cast(CursorResult[Any], result).rowcount

    @staticmethod
    async def delivery_facts(
        session: AsyncSession,
        operation_id: str,
        *,
        conversation_id: str,
        tool_call_id: str,
        planned_file_parts: int | None,
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

        def receipt(row: SocialOperationModel | None) -> dict[str, Any]:
            if row is None:
                return {"status": "not_sent"}
            return {
                "operation_id": row.id,
                "status": row.status,
                "target": {"kind": row.target_kind, "id": row.target_id},
                "event_id": row.event_id,
                "error_category": row.error_category,
            }

        result = receipt(parent)
        parts = [result.copy()]
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
            parts = [receipt(children.get(f"{prefix}{index}")) for index in range(count)]
            result.update(
                planned_messages=count,
                sent_messages=sum(part["status"] == "succeeded" for part in parts),
                parts=parts,
            )
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
                result["caption"] = receipt(caption)
                parts.append(result["caption"])
        elif parent.action == "send_message":
            # An unplanned legacy file/caption cannot prove whole-call success.
            complete_plan = False
        for part in parts:
            if part["status"] != "succeeded" or part.get("event_id") is None:
                continue
            event = await session.get(ChatEventModel, part["event_id"])
            operation = await session.get(SocialOperationModel, part["operation_id"])
            if (
                event is None
                or operation is None
                or event.direction != "outbound"
                or event.author_kind != "yuki"
                or event.author_presence_id != operation.presence_id
                or event.event_kind != "message"
                or event.suppression_status != "keeper"
            ):
                continue
            conversation = await session.get(
                CanonicalConversationModel, event.canonical_conversation_id
            )
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
            part["delivered_text"] = "".join(
                item.get("data", {}).get("text", "")
                for item in segments
                if isinstance(item, dict) and item.get("type") == "text"
            )
            for item in segments:
                if not isinstance(item, dict) or item.get("type") not in {"file", "image"}:
                    continue
                data = item.get("data", {})
                if isinstance(data, dict) and isinstance(data.get("artifact_id"), str):
                    part.update(artifact_id=data["artifact_id"], attachment_kind=item["type"])
        if parent.action == "send_message":
            result.update(parts[0])
            if parts[0].get("artifact_id") and "file" not in result:
                result["file"] = parts[0]
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
            error_category=row.error_category,
            payload_hash=row.payload_hash,
        )
