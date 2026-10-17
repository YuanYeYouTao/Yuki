"""Read canonical scope and its selected public checkpoint in one snapshot."""

from __future__ import annotations

from dataclasses import replace

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from yuki.conversation.canonical_db_models import (
    CanonicalConversationModel,
)
from yuki.conversation.hydrate import (
    conversation_for_scope,
    hydrate_scope_state_from_canonical,
)
from yuki.conversation.projection_models import PromptProjectionModel
from yuki.conversation.projections import current_checkpoint_query
from yuki.conversation.scope import ConversationTurnSnapshot, turn_matches_hydrated_scope
from yuki.conversation.scope_models import (
    ConversationCoverageError,
    ConversationPromptSnapshot,
    ConversationScopeState,
)
from yuki.domain.conversations import ConversationScope
from yuki.persistence.database import Database
from yuki.persistence.models import ChatEventModel
from yuki.persistence.repository_helpers import _event_record, keeper_event_clause
from yuki.persistence.repository_records import EventRecord


async def _with_proactive_cause_metadata(
    session: AsyncSession,
    events: tuple[EventRecord, ...],
) -> tuple[EventRecord, ...]:
    """Bulk-hydrate source/type for causal model projections without N+1 reads."""

    cause_ids = tuple(
        dict.fromkeys(
            event.caused_by_event_id
            for event in events
            if event.origin == "plugin_background" and event.caused_by_event_id is not None
        )
    )
    if not cause_ids:
        return events
    rows = tuple(
        (
            await session.scalars(
                select(ChatEventModel).where(
                    ChatEventModel.id.in_(cause_ids),
                    ChatEventModel.event_kind == "external_event",
                    ChatEventModel.direction == "external",
                    keeper_event_clause(),
                )
            )
        ).all()
    )
    causes = {row.id: _event_record(row) for row in rows}
    enriched: list[EventRecord] = []
    for event in events:
        cause = causes.get(event.caused_by_event_id or 0)
        if cause is None or cause.canonical_conversation_id != event.canonical_conversation_id:
            enriched.append(event)
            continue
        enriched.append(
            replace(
                event,
                caused_by_external_source=cause.external_source,
                caused_by_external_event_type=cause.external_event_type,
            )
        )
    return tuple(enriched)


class ConversationScopeRepository:
    """Read scopes without deriving ownership from transport message IDs."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def get(
        self, scope: ConversationScope, *, expected_turn: ConversationTurnSnapshot | None = None
    ) -> ConversationScopeState | None:
        async with self._database.sessions() as session:
            conversation = (
                await session.get(CanonicalConversationModel, expected_turn.conversation_id)
                if expected_turn
                else await conversation_for_scope(session, scope)
            )
            if conversation is None:
                return None
            return await hydrate_scope_state_from_canonical(session, scope, conversation)

    async def generation_matches(self, conversation_id: str, generation: int) -> bool:
        async with self._database.sessions() as session:
            conversation = await session.get(CanonicalConversationModel, conversation_id)
            return conversation is not None and int(conversation.generation) == generation

    async def load_prompt_snapshot(
        self,
        scope: ConversationScope,
        *,
        before_event_id: int | None = None,
        expected_turn: ConversationTurnSnapshot | None = None,
    ) -> ConversationPromptSnapshot:
        async with self._database.sessions() as session:
            await session.execute(text("BEGIN"))
            conversation = (
                await session.get(CanonicalConversationModel, expected_turn.conversation_id)
                if expected_turn
                else await conversation_for_scope(session, scope)
            )
            if conversation is None:
                raise ConversationCoverageError("conversation scope does not exist")
            state = await hydrate_scope_state_from_canonical(session, scope, conversation)
            if expected_turn is not None and not turn_matches_hydrated_scope(
                expected_turn,
                conversation_id=state.id,
                generation=state.generation,
                runtime_key=state.runtime_scope_key,
            ):
                raise ConversationCoverageError("turn identity changed before prompt snapshot")
            checkpoint = await session.scalar(
                current_checkpoint_query(conversation.id).options(
                    defer(PromptProjectionModel.payload_json)
                )
            )
            coverage = (
                checkpoint.selected_summary_coverage
                if checkpoint and checkpoint.selected_summary_text is not None
                else state.starts_after_event_id
            )
            query = select(ChatEventModel).where(
                ChatEventModel.canonical_conversation_id == state.id,
                ChatEventModel.id > coverage,
                ChatEventModel.id <= state.last_event_id,
                ChatEventModel.event_kind == "message",
                keeper_event_clause(),
            )
            if before_event_id is not None:
                query = query.where(ChatEventModel.id < before_event_id)
            rows = tuple((await session.scalars(query.order_by(ChatEventModel.id))).all())
            events = await _with_proactive_cause_metadata(
                session, tuple(_event_record(row) for row in rows)
            )
            return ConversationPromptSnapshot(
                scope=state,
                raw_events=events,
                checkpoint_text=(checkpoint.selected_summary_text or "") if checkpoint else "",
                checkpoint_kind=checkpoint.selected_summary_kind if checkpoint else None,
                checkpoint_revision=checkpoint.revision if checkpoint else 0,
                effective_coverage=coverage,
                raw_tail_end_event_id=events[-1].id if events else coverage,
                prompt_source_revision=conversation.prompt_source_revision,
            )
