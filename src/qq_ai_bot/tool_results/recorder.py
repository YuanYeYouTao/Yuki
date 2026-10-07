"""Source-authorized tool evidence and content-free invocation diagnostics."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, insert, literal, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from qq_ai_bot.conversation.autonomy_db_models import InitiativeRunModel
from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
from qq_ai_bot.conversation.correlation import (
    load_correlated_chat_event,
    require_live_conversation,
    stamp_conversation_correlation,
)
from qq_ai_bot.execution_trace.db_models import ExecutionTraceStateModel
from qq_ai_bot.identity.db_models import (
    CanonicalPersonModel,
    CanonicalSpaceModel,
    IdentityBindingModel,
    PresenceModel,
    SpaceBindingModel,
)
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.diagnostic_writer import DiagnosticWriter
from qq_ai_bot.persistence.models import (
    ChatEventModel,
    MemoryToolReceiptModel,
    ToolInvocationModel,
)
from qq_ai_bot.runtime.observability import claim_runtime_turn_id
from qq_ai_bot.tool_results.redaction import redact_sensitive_data, redact_sensitive_text


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


class ToolInvocationRepository:
    def __init__(
        self,
        database: Database,
        *,
        writer: DiagnosticWriter | None = None,
        reflection_excerpt_characters: int = 2000,
        reflection_retention_days: int = 7,
    ) -> None:
        self._database = database
        self.writer = writer
        self._reflection_excerpt_characters = max(1, min(reflection_excerpt_characters, 8000))
        self._reflection_retention_days = max(1, min(reflection_retention_days, 30))

    async def preflight_conversation_correlation(
        self,
        canonical_conversation_id: str | None,
    ) -> None:
        """Fail closed when a supplied canonical Conversation is not live.

        Uses the same canonical kind/existence helpers as
        ``stamp_conversation_correlation`` (``require_live_conversation``).
        The short read-only session is closed before the caller may connect.
        A provided id that is the wrong kind, a Presence/Person/Space id, or a
        missing/stale Conversation fails closed here.
        """

        from qq_ai_bot.persistence.unit_of_work import optional_session

        if canonical_conversation_id is None or not str(canonical_conversation_id).strip():
            return
        async with optional_session(self._database, None, write=False) as session:
            await require_live_conversation(session, canonical_conversation_id)

    async def record_invocation(
        self,
        *,
        conversation_key: str,
        provider_id: str,
        tool_name: str,
        success: bool,
        latency_seconds: float,
        result_size: int,
        artifact_created: bool,
        error_category: str | None,
        trigger_message_id: str = "",
        trigger_event_id: int | None = None,
        bot_user_id: str = "",
        result_excerpt: str = "",
        canonical_conversation_id: str | None = None,
        ingress_presence_id: str | None = None,
        initiative_run_id: str | None = None,
        tool_call_id: str | None = None,
        execution_id: str | None = None,
        audit_source: tuple[str, int, int] | None = None,
    ) -> None:
        # Evidence remains source-authorized synchronous persistence; only the
        # content-free invocation is disposable telemetry.
        if initiative_run_id is not None and (
            trigger_event_id is not None
            or not tool_call_id
            or not tool_call_id.strip()
            or not execution_id
            or not execution_id.strip()
        ):
            raise ValueError(
                "initiative receipt requires an exclusive run source and call identity"
            )
        if any(len(value) > 255 for value in (tool_call_id, execution_id) if value):
            raise ValueError("tool call identity exceeds storage bounds")
        if audit_source is not None and canonical_conversation_id != audit_source[0]:
            raise ValueError("tool_audit_source_mismatch")
        now = datetime.now(UTC)
        redacted = _redact_reflection_result(result_excerpt.strip())
        invocation = ToolInvocationModel(
            runtime_turn_id=claim_runtime_turn_id(),
            conversation_key_hash=hashlib.sha256(conversation_key.encode()).hexdigest(),
            provider_id=provider_id[:128],
            tool_name=tool_name[:255],
            success=success,
            latency_seconds=max(0.0, latency_seconds),
            result_size=max(0, result_size),
            artifact_created=artifact_created,
            error_category=error_category[:128] if error_category else None,
            created_at=now,
        )
        receipt: dict[str, Any] | None = None
        guards: list[Any] = []
        async with self._database.sessions() as session:
            await stamp_conversation_correlation(session, invocation, canonical_conversation_id)
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
            if initiative_run_id is not None:
                from qq_ai_bot.memory.self_origin import resolve_self_origin

                source = await resolve_self_origin(
                    session,
                    initiative_run_id=initiative_run_id,
                    canonical_conversation_id=canonical_conversation_id,
                    require_live=False,
                    require_group_projection=False,
                )
                await stamp_conversation_correlation(
                    session, invocation, source.canonical_conversation_id
                )
                invocation.conversation_key_hash = hashlib.sha256(
                    source.partition.encode()
                ).hexdigest()
                receipt = {
                    "trigger_event_id": None,
                    "initiative_run_id": initiative_run_id,
                    "bot_user_id": source.bot_user_id,
                    "canonical_person_id": None,
                    "canonical_space_id": source.space_id,
                    "conversation_key_hash": invocation.conversation_key_hash,
                }
                # Historical evidence can stay readable; a delayed write cannot
                # resurrect an old generation or disabled source after erasure.
                guards.append(
                    select(InitiativeRunModel.id)
                    .join(
                        CanonicalConversationModel,
                        CanonicalConversationModel.id == InitiativeRunModel.conversation_id,
                    )
                    .join(PresenceModel, PresenceModel.id == InitiativeRunModel.presence_id)
                    .join(
                        CanonicalSpaceModel, CanonicalSpaceModel.id == InitiativeRunModel.space_id
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
                        PresenceModel.platform == "qq",
                        PresenceModel.external_account_id == source.bot_user_id,
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
                if event is not None:
                    await stamp_conversation_correlation(
                        session, invocation, event.canonical_conversation_id
                    )
                from qq_ai_bot.identity.memory_guard import refuse_legacy_live_event

                if event is not None and not await refuse_legacy_live_event(session, event):
                    from qq_ai_bot.memory.partition import (
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
                    binding: Any
                    owner: Any
                    owner_id: str | None
                    if partition.space_id:
                        binding = SpaceBindingModel
                        owner = CanonicalSpaceModel
                        external = str(event.group_id)
                        owner_id = partition.space_id
                        owner_column = binding.space_id
                        external_column = binding.external_space_id
                    else:
                        binding = IdentityBindingModel
                        owner = CanonicalPersonModel
                        external = str(event.private_peer_user_id or event.sender_user_id)
                        owner_id = partition.person_id
                        owner_column = binding.person_id
                        external_column = binding.external_account_id
                    guards.extend(
                        (
                            select(owner.id)
                            .where(owner.id == owner_id, owner.enabled.is_(True))
                            .exists(),
                            select(owner_column)
                            .where(
                                owner_column == owner_id,
                                external_column == external,
                                binding.platform == "qq",
                                binding.status == "active",
                            )
                            .exists(),
                            select(func.count(func.distinct(owner_column)))
                            .where(
                                external_column == external,
                                binding.platform == "qq",
                                binding.status == "active",
                            )
                            .scalar_subquery()
                            == 1,
                        )
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
                provider_id=invocation.provider_id,
                tool_name=invocation.tool_name,
                success=success,
                result_excerpt=redacted[: self._reflection_excerpt_characters],
                result_characters=len(redacted),
                error_category=invocation.error_category,
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
            async with self._database.sessions() as session, session.begin():
                inserted = await session.scalar(statement)
                if inserted is None:
                    if key and await session.scalar(
                        select(MemoryToolReceiptModel.id).where(
                            MemoryToolReceiptModel.source_call_key == key
                        )
                    ):
                        return
                    from qq_ai_bot.memory.partition import MemoryPartitionResolutionError

                    raise MemoryPartitionResolutionError("tool_receipt_source_changed")
        frozen = tuple(
            (column.name, getattr(invocation, column.name))
            for column in invocation.__table__.columns
            if column.name != "id"
        )
        if self.writer is not None:
            size = 1024 + sum(len(value.encode()) for _, value in frozen if isinstance(value, str))
            self.writer.submit(
                "tool_invocation",
                size,
                lambda: self._insert_telemetry(frozen, privacy_generation, audit_source),
            )
        else:
            await self._insert_telemetry(frozen, privacy_generation, audit_source)

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

    async def _insert_telemetry(
        self,
        frozen: tuple[tuple[str, Any], ...],
        privacy_generation: int,
        audit_source: tuple[str, int, int] | None = None,
    ) -> None:
        table = ToolInvocationModel.__table__
        values = dict(frozen)
        guard = (
            func.coalesce(
                select(ExecutionTraceStateModel.privacy_generation)
                .where(ExecutionTraceStateModel.id == 1)
                .scalar_subquery(),
                0,
            )
            == privacy_generation
        )
        if audit_source is not None:
            guard &= self._audit_source_guard(audit_source)
        async with self._database.sessions() as session, session.begin():
            await session.execute(
                insert(ToolInvocationModel).from_select(
                    list(values),
                    select(
                        *(literal(value, type_=table.c[key].type) for key, value in values.items())
                    ).where(guard),
                    include_defaults=False,
                )
            )


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
