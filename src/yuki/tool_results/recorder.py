"""Source-authorized durable tool receipts for Memory reflection."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, literal, select, text
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from yuki.conversation.autonomy_db_models import InitiativeRunModel
from yuki.conversation.canonical_db_models import CanonicalConversationModel
from yuki.conversation.correlation import (
    load_correlated_chat_event,
    require_live_conversation,
    try_live_conversation,
)
from yuki.execution_trace.db_models import ExecutionTraceStateModel
from yuki.identity.db_models import (
    CanonicalPersonModel,
    CanonicalSpaceModel,
    PresenceModel,
)
from yuki.persistence.database import Database
from yuki.persistence.models import (
    ChatEventModel,
    MemoryToolReceiptModel,
)
from yuki.tool_results.redaction import redact_sensitive_data, redact_sensitive_text


def _redact_reflection_result(value: str) -> str:
    """Redact structured secrets before a bounded tool result can become evidence."""

    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return redact_sensitive_text(value)
    return json.dumps(
        redact_sensitive_data(decoded),
        ensure_ascii=False,
        separators=(",", ":"),
    )


class ToolReceiptRepository:
    def __init__(
        self,
        database: Database,
        *,
        reflection_excerpt_characters: int = 2000,
        reflection_retention_days: int = 7,
    ) -> None:
        self._database = database
        self._reflection_excerpt_characters = max(1, min(reflection_excerpt_characters, 8000))
        self._reflection_retention_days = max(1, min(reflection_retention_days, 30))

    async def record_receipt(
        self,
        *,
        provider_id: str,
        tool_name: str,
        success: bool,
        error_category: str | None,
        trigger_event_id: int | None = None,
        bot_user_id: str = "",
        result_excerpt: str = "",
        canonical_conversation_id: str | None = None,
        ingress_presence_id: str | None = None,
        initiative_run_id: str | None = None,
        principal_kind: str = "person",
        tool_call_id: str | None = None,
        execution_id: str | None = None,
        audit_source: tuple[str, int, int] | None = None,
        source_event_ids: tuple[int, ...] = (),
        source_privacy_generation: int | None = None,
    ) -> None:
        # Without an original source there is no Memory evidence to persist.
        if initiative_run_id is None and principal_kind != "self" and trigger_event_id is None:
            return
        if (initiative_run_id is not None or principal_kind == "self") and (
            trigger_event_id is not None
            or not tool_call_id
            or not tool_call_id.strip()
            or not execution_id
            or not execution_id.strip()
        ):
            raise ValueError("SELF receipt requires an actorless source and call identity")
        if any(len(value) > 255 for value in (tool_call_id, execution_id) if value):
            raise ValueError("tool call identity exceeds storage bounds")
        if audit_source is not None and canonical_conversation_id != audit_source[0]:
            raise ValueError("tool_audit_source_mismatch")
        now = datetime.now(UTC)
        redacted = _redact_reflection_result(result_excerpt.strip())
        receipt: dict[str, Any] | None = None
        guards: list[Any] = []
        async with self._database.read_session() as session:
            source_conversation = await try_live_conversation(session, canonical_conversation_id)
            proven_conversation = (
                source_conversation.id if source_conversation is not None else None
            )
            privacy_generation = (
                audit_source[2]
                if audit_source is not None
                else (
                    await session.scalar(
                        select(ExecutionTraceStateModel.privacy_generation).where(
                            ExecutionTraceStateModel.id == 1
                        )
                    )
                    or 0
                )
            )
            if source_event_ids and source_privacy_generation is not None:
                privacy_generation = min(privacy_generation, source_privacy_generation)
            if initiative_run_id is not None or principal_kind == "self":
                from yuki.memory.self_origin import (
                    resolve_self_origin,
                    self_execution_origin_sql,
                )

                source = await resolve_self_origin(
                    session,
                    initiative_run_id=initiative_run_id,
                    execution_id=execution_id,
                    canonical_conversation_id=canonical_conversation_id,
                    require_live=False,
                    require_group_projection=False,
                )
                if source.canonical_conversation_id != proven_conversation:
                    await require_live_conversation(session, source.canonical_conversation_id)
                receipt = {
                    "trigger_event_id": None,
                    "initiative_run_id": initiative_run_id,
                    "bot_user_id": source.bot_user_id,
                    "canonical_person_id": None,
                    "canonical_space_id": source.space_id,
                    "conversation_key_hash": hashlib.sha256(source.partition.encode()).hexdigest(),
                }
                # Historical evidence can stay readable; a delayed write cannot
                # resurrect an old generation or disabled source after erasure.
                if initiative_run_id is None:
                    guards.append(
                        text(
                            "EXISTS ("
                            + self_execution_origin_sql(":self_execution_id")
                            + " AND ic.generation=:self_generation AND ip.status='active' "
                            "AND isp.enabled=1)"
                        ).bindparams(
                            self_execution_id=execution_id, self_generation=source.generation
                        )
                    )
                else:
                    guards.append(
                        select(InitiativeRunModel.id)
                        .join(
                            CanonicalConversationModel,
                            CanonicalConversationModel.id == InitiativeRunModel.conversation_id,
                        )
                        .join(PresenceModel, PresenceModel.id == InitiativeRunModel.presence_id)
                        .join(
                            CanonicalSpaceModel,
                            CanonicalSpaceModel.id == InitiativeRunModel.space_id,
                        )
                        .where(
                            InitiativeRunModel.id == initiative_run_id,
                            InitiativeRunModel.conversation_id == source.canonical_conversation_id,
                            InitiativeRunModel.space_id == source.space_id,
                            InitiativeRunModel.presence_id == source.presence_id,
                            InitiativeRunModel.generation == CanonicalConversationModel.generation,
                            CanonicalConversationModel.kind == "space",
                            CanonicalConversationModel.space_id == source.space_id,
                            CanonicalConversationModel.person_id.is_(None),
                            PresenceModel.enabled.is_(True),
                            CanonicalSpaceModel.enabled.is_(True),
                        )
                        .exists()
                    )
            else:
                event = await load_correlated_chat_event(
                    session,
                    trigger_event_id=trigger_event_id,
                    canonical_conversation_id=canonical_conversation_id,
                    bot_user_id=bot_user_id,
                    ingress_presence_id=ingress_presence_id,
                )
                if event is not None and event.canonical_conversation_id != proven_conversation:
                    await require_live_conversation(session, event.canonical_conversation_id)
                from yuki.identity.memory_guard import refuse_legacy_live_event

                if event is not None and not await refuse_legacy_live_event(session, event):
                    from yuki.memory.partition import (
                        MemoryPartitionResolutionError,
                        resolve_memory_partition_for_event,
                    )

                    partition = await resolve_memory_partition_for_event(session, event)
                    conversation = await session.get(
                        CanonicalConversationModel, event.canonical_conversation_id
                    )
                    assert conversation is not None
                    if not (
                        (
                            conversation.kind == "space"
                            and conversation.space_id == partition.space_id
                            and conversation.person_id is None
                            and partition.person_id is None
                        )
                        or (
                            conversation.kind == "private"
                            and conversation.person_id == partition.person_id
                            and conversation.space_id is None
                            and partition.space_id is None
                        )
                    ):
                        # A moved QQ Binding cannot relabel an internal event's
                        # already-owned Conversation as the Binding's new owner.
                        raise MemoryPartitionResolutionError("tool_receipt_source_changed")
                    receipt = {
                        "trigger_event_id": event.id,
                        "initiative_run_id": None,
                        "bot_user_id": event.bot_user_id,
                        "canonical_person_id": partition.person_id,
                        "canonical_space_id": partition.space_id,
                        "conversation_key_hash": hashlib.sha256(
                            partition.value.encode()
                        ).hexdigest(),
                    }
                    guards.append(
                        select(ChatEventModel.id)
                        .join(
                            CanonicalConversationModel,
                            CanonicalConversationModel.id
                            == ChatEventModel.canonical_conversation_id,
                        )
                        .where(
                            ChatEventModel.id == event.id,
                            ChatEventModel.canonical_event_id == event.canonical_event_id,
                            ChatEventModel.ingress_presence_id == event.ingress_presence_id,
                            ChatEventModel.author_person_id == event.author_person_id,
                            ChatEventModel.canonical_conversation_id
                            == event.canonical_conversation_id,
                            CanonicalConversationModel.prompt_source_revision
                            == conversation.prompt_source_revision,
                            ChatEventModel.suppression_status.in_(("keeper",))
                            | ChatEventModel.suppression_status.is_(None),
                            CanonicalConversationModel.generation == conversation.generation,
                            CanonicalConversationModel.kind == conversation.kind,
                            CanonicalConversationModel.person_id == partition.person_id,
                            CanonicalConversationModel.space_id == partition.space_id,
                            ChatEventModel.id > CanonicalConversationModel.starts_after_event_id,
                            ChatEventModel.id
                            > CanonicalConversationModel.last_generation_change_event_id,
                        )
                        .exists()
                    )
                    owner: Any
                    owner_id: str | None
                    if partition.space_id:
                        owner = CanonicalSpaceModel
                        owner_id = partition.space_id
                    else:
                        owner = CanonicalPersonModel
                        owner_id = partition.person_id
                    guards.extend(
                        (
                            select(owner.id)
                            .where(owner.id == owner_id, owner.enabled.is_(True))
                            .exists(),
                        )
                    )
                    if partition.person_id:
                        from yuki.identity.person_identity import person_root_id

                        guards.append(
                            select(CanonicalPersonModel.id)
                            .where(
                                CanonicalPersonModel.id == person_root_id(partition.person_id),
                                CanonicalPersonModel.enabled.is_(True),
                            )
                            .exists()
                        )
        privacy_guard = (
            func.coalesce(
                select(ExecutionTraceStateModel.privacy_generation)
                .where(ExecutionTraceStateModel.id == 1)
                .scalar_subquery(),
                0,
            )
            == privacy_generation
        )
        if audit_source is not None:
            guards.append(self._audit_source_guard(audit_source))
        if receipt is not None:
            key = (
                hashlib.sha256(
                    json.dumps(
                        [initiative_run_id, execution_id, provider_id, tool_name, tool_call_id]
                        if initiative_run_id is not None
                        else [trigger_event_id, execution_id, provider_id, tool_name, tool_call_id],
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ).encode()
                ).hexdigest()
                if execution_id and tool_call_id
                else None
            )
            # Re-running only this audit from the original effect result is safe;
            # no tool dispatch is part of evidence repair.
            receipt.update(
                tool_call_id=tool_call_id,
                execution_id=execution_id,
                source_call_key=key,
                source_event_ids_json=json.dumps(list(source_event_ids)),
                provider_id=provider_id[:128],
                tool_name=tool_name[:255],
                success=success,
                result_excerpt=redacted[: self._reflection_excerpt_characters],
                result_characters=len(redacted),
                error_category=error_category[:128] if error_category else None,
                created_at=now,
                expires_at=now + timedelta(days=self._reflection_retention_days),
            )
            table = MemoryToolReceiptModel.__table__
            statement = (
                sqlite_insert(MemoryToolReceiptModel)
                .from_select(
                    list(receipt),
                    select(
                        *(
                            literal(value, type_=table.c[name].type)
                            for name, value in receipt.items()
                        )
                    ).where(privacy_guard, *guards),
                    include_defaults=False,
                )
                .on_conflict_do_nothing(index_elements=["source_call_key"])
                .returning(table.c.id)
            )
            async with self._database.immediate_session() as session:
                inserted = await session.scalar(statement)
                if inserted is None:
                    if key and await session.scalar(
                        select(MemoryToolReceiptModel.id).where(
                            MemoryToolReceiptModel.source_call_key == key
                        )
                    ):
                        return
                    from yuki.memory.partition import MemoryPartitionResolutionError

                    raise MemoryPartitionResolutionError("tool_receipt_source_changed")

    @staticmethod
    def _audit_source_guard(audit_source: tuple[str, int, int]) -> Any:
        conversation_id, generation, _privacy_generation = audit_source
        return (
            select(CanonicalConversationModel.id)
            .where(
                CanonicalConversationModel.id == conversation_id,
                CanonicalConversationModel.generation == generation,
            )
            .exists()
        )
