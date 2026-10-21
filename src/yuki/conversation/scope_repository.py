"""Read canonical scope and its selected public checkpoint in one snapshot."""

from __future__ import annotations

import json
from dataclasses import replace

from sqlalchemy import Integer, cast, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from yuki.conversation.canonical_db_models import (
    CanonicalConversationModel,
)
from yuki.conversation.hydrate import (
    conversation_for_scope,
    hydrate_scope_state_from_canonical,
)
from yuki.conversation.projections import (
    SUMMARY_RENDERER_VERSION,
    PromptProjectionRepository,
    current_checkpoint_query,
)
from yuki.conversation.prompt_history import PromptHistorySource
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

    def __init__(
        self, database: Database, *, projections: PromptProjectionRepository | None = None
    ) -> None:
        self._database = database
        self.projections = (
            projections if projections is not None else PromptProjectionRepository(database)
        )

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
        """Read the full uncovered raw tail for explicit raw-history consumers."""
        async with self._database.sessions() as session:
            await session.execute(text("BEGIN"))
            conversation, state = await self._prompt_scope(session, scope, expected_turn)
            checkpoint = await session.scalar(current_checkpoint_query(conversation.id))
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
                state,
                events,
                (checkpoint.selected_summary_text or "") if checkpoint else "",
                checkpoint.selected_summary_kind if checkpoint else None,
                checkpoint.revision if checkpoint else 0,
                coverage,
                events[-1].id if events else coverage,
                conversation.prompt_source_revision,
            )

    async def _prompt_scope(
        self,
        session: AsyncSession,
        scope: ConversationScope,
        expected_turn: ConversationTurnSnapshot | None,
    ) -> tuple[CanonicalConversationModel, ConversationScopeState]:
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
        return conversation, state

    async def prepare_prompt_snapshot(
        self,
        scope: ConversationScope,
        *,
        before_event_id: int | None = None,
        expected_turn: ConversationTurnSnapshot | None = None,
        timezone: str = "Asia/Shanghai",
        trigger_event_id: int | None = None,
    ) -> ConversationPromptSnapshot:
        async with self._database.sessions() as session:
            await session.execute(text("BEGIN"))
            conversation, state = await self._prompt_scope(session, scope, expected_turn)
            checkpoint = await session.scalar(current_checkpoint_query(conversation.id))
            coverage = (
                checkpoint.selected_summary_coverage
                if checkpoint and checkpoint.selected_summary_text is not None
                else state.starts_after_event_id
            )
            projection = await self.projections.read(state.id, session=session)
            reusable = projection
            if (
                projection is not None
                and projection.selected_summary_text
                and (
                    projection.selected_summary_kind is None
                    or projection.selected_summary_renderer != SUMMARY_RENDERER_VERSION
                )
            ):
                # Keep the original head for its CAS/source_changed rebuild,
                # but its obsolete derived membership cannot suppress raw bodies.
                reusable = None
            through = (
                min(state.last_event_id, before_event_id - 1)
                if before_event_id
                else state.last_event_id
            )
            stamp = (
                state.id,
                state.generation,
                conversation.prompt_source_revision,
                state.starts_after_event_id,
                coverage,
                checkpoint.selected_summary_text if checkpoint else "",
                checkpoint.selected_summary_kind if checkpoint else None,
                projection.selected_summary_renderer
                if projection is not None and projection.selected_summary_text
                else None,
                timezone,
                before_event_id,
            )
            source = self.projections.history_source(state.id)
            if source is None or source.stamp != stamp or not source.reconcile(reusable):
                source = PromptHistorySource(
                    stamp,
                    projection=reusable,
                    timezone=timezone,
                    after_event_id=coverage,
                )
            captured_pending = source.pending(through)
            query = select(ChatEventModel).where(
                ChatEventModel.canonical_conversation_id == state.id,
                ChatEventModel.id > source.scanned_through,
                ChatEventModel.id <= through,
                ChatEventModel.event_kind == "message",
                keeper_event_clause(),
            )
            rows = tuple((await session.scalars(query.order_by(ChatEventModel.id))).all())
            additions = await _with_proactive_cause_metadata(
                session, tuple(_event_record(row) for row in rows)
            )
            source.extend(additions, through_event_id=through)
            pending = source.candidate_pending(
                captured_pending,
                additions,
                frozen_event_ids=reusable.fragments.event_ids if reusable is not None else (),
            )
            bodies = {row.id: row for row in additions}
            missing = tuple(event_id for event_id in pending if event_id not in bodies)
            if missing:
                requested = func.json_each(json.dumps(missing)).table_valued("value")
                selected = tuple(
                    (
                        await session.scalars(
                            select(ChatEventModel)
                            .where(
                                ChatEventModel.id.in_(select(cast(requested.c.value, Integer))),
                                ChatEventModel.canonical_conversation_id == state.id,
                                ChatEventModel.id > coverage,
                                ChatEventModel.id <= through,
                                ChatEventModel.event_kind == "message",
                                keeper_event_clause(),
                            )
                            .order_by(ChatEventModel.id)
                        )
                    ).all()
                )
                bodies.update(
                    (row.id, row)
                    for row in await _with_proactive_cause_metadata(
                        session, tuple(_event_record(row) for row in selected)
                    )
                )
            trigger_id = trigger_event_id or (
                expected_turn.trigger_event_id if expected_turn is not None else None
            )
            current_event = bodies.get(trigger_id) if trigger_id is not None else None
            if trigger_id is not None and current_event is None:
                trigger = await session.get(ChatEventModel, trigger_id)
                current_event = _event_record(trigger) if trigger is not None else None
            events = tuple(bodies[event_id] for event_id in pending if event_id in bodies)
            references = source.capture(through)
            self.projections.remember_history_source(state.id, source)
            return ConversationPromptSnapshot(
                scope=state,
                raw_events=events,
                checkpoint_text=(checkpoint.selected_summary_text or "") if checkpoint else "",
                checkpoint_kind=checkpoint.selected_summary_kind if checkpoint else None,
                checkpoint_revision=checkpoint.revision if checkpoint else 0,
                effective_coverage=coverage,
                raw_tail_end_event_id=source.raw_tail(through),
                prompt_source_revision=conversation.prompt_source_revision,
                projection=projection,
                source=source,
                references=references,
                current_event=current_event,
                frozen_prefix=reusable.fragments if reusable is not None else None,
            )
