"""Prepare and atomically freeze the selected history of a Main Agent turn."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Any

from qq_ai_bot.conversation.frozen_fragments import FrozenFragments
from qq_ai_bot.conversation.observations import (
    ContextObservation,
    ContextObservationRepository,
    ObservationSummaryError,
)
from qq_ai_bot.conversation.projections import (
    ProjectionPublication,
    ProjectionSnapshot,
    PromptProjectionRepository,
)
from qq_ai_bot.domain.messages import ChatMessage
from qq_ai_bot.event_prompt import ChatEventPromptRenderer
from qq_ai_bot.llm.base import LLMError
from qq_ai_bot.persistence.event_repository import EventLedgerRepository
from qq_ai_bot.persistence.repository_records import EventRecord
from qq_ai_bot.runtime.work_repository import WorkCapacityError
from qq_ai_bot.services.context_assembler import AssembledContext

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class PreparedHistory:
    repository: PromptProjectionRepository
    view_key: str
    context_key: str
    contract_revision: str
    context: AssembledContext
    fragments: FrozenFragments
    previous: ProjectionSnapshot | None
    reason: str | None
    actor_id: str = ""
    read_scope: str = ""
    committed: ProjectionSnapshot | None = None
    committed_input: list[dict[str, Any]] | None = None

    async def prepare_commit(
        self, fragments: FrozenFragments, *, current_snapshot: dict[str, Any] | None = None
    ) -> ProjectionPublication:
        version = self.context.read_version
        if version is None or version.conversation_id is None:
            raise ValueError("projection requires a canonical read version")
        previous = self.committed or self.previous
        return await self.repository.prepare_commit(
            view_key=self.view_key,
            conversation_id=version.conversation_id,
            generation=version.generation,
            expected_source_revision=version.prompt_source_revision,
            starts_after_event_id=version.starts_after_event_id,
            context_key=self.context_key,
            contract_revision=self.contract_revision,
            items=list(fragments.items),
            expected_epoch=previous.epoch_id if previous else None,
            expected_revision=previous.revision if previous else 0,
            rebuild_reason=None if self.committed is not None else self.reason,
            actor_id=self.actor_id,
            read_scope=self.read_scope,
            selected_summary_text=self.context.rollup_text,
            selected_summary_coverage=self.context.prompt_effective_coverage,
            current_snapshot=current_snapshot,
            snapshot_event_id=self.context.current_event_id,
            snapshot_fragment_index=(
                len(fragments.items) - 1
                if self.context.current_event_id is None
                and len(fragments.items) == len(self.fragments.items) + 1
                and list(fragments.items[:-1]) == list(self.fragments.items)
                else None
            ),
        )

    async def commit(
        self, fragments: FrozenFragments, *, current_snapshot: dict[str, Any] | None = None
    ) -> ProjectionSnapshot:
        """Call after authority/source validation, immediately before model dispatch.

        Retries reuse this immutable preparation; no model output is interpreted
        as a platform send, and concurrent preparations cannot replace each other.
        """
        if self.committed is not None:
            if self.committed_input == list(fragments.items):
                return self.committed
        publication = await self.prepare_commit(fragments, current_snapshot=current_snapshot)
        async with self.repository.database.immediate_session() as writer:
            self.committed = await publication(writer)
        self.committed_input = list(fragments.items)
        logger.info(
            "prompt_projection_committed view=%s epoch=%s revision=%d reason=%s items=%d",
            self.view_key,
            self.committed.epoch_id,
            self.committed.revision,
            self.committed.rebuild_reason,
            len(fragments.items),
        )
        return self.committed


async def prepare_history(
    repository: PromptProjectionRepository,
    context: AssembledContext,
    *,
    view_key: str,
    context_key: str,
    contract_revision: str,
    history_fits: Callable[[tuple[ChatMessage, ...]], bool],
    actor_id: str = "",
    read_scope: str = "",
    context_fits: Callable[[AssembledContext], bool] | None = None,
    context_hard_fits: Callable[[AssembledContext], bool] | None = None,
    summarize_observations: Callable[[tuple[ContextObservation, ...]], Awaitable[dict[str, Any]]]
    | None = None,
) -> PreparedHistory:
    """The caller supplies the read-policy identity, never just a sending route.

    An epoch can accumulate only currently selected events. A narrower read
    window starts a fresh epoch so old private/dynamic material cannot bypass
    the caller's current selection or capacity limits.
    """
    if context.read_version is None or context.read_version.conversation_id is None:
        raise ValueError("projection requires a canonical read version")
    previous = await repository.read(view_key)
    version = context.read_version
    conversation_id = version.conversation_id
    if conversation_id is None:
        raise ValueError("projection requires a canonical conversation")
    sources = ContextObservationRepository(repository.database)
    observations = (
        await sources.read(
            conversation_id=conversation_id,
            generation=version.generation,
            actor_id=actor_id,
            read_scope=read_scope,
            view_key=view_key,
        )
        if actor_id and read_scope
        else ()
    )
    reason = None
    frozen = FrozenFragments.load([])
    if previous is None:
        reason = await repository.invalidation_reason(view_key) or "bootstrap"
    elif previous.contract_revision != contract_revision:
        reason = "contract_changed"
    elif previous.context_key != context_key:
        reason = "read_scope_changed"
    else:
        frozen = FrozenFragments.load(previous.items())
        if not {identity for identity, _ in frozen.observation_sources} <= {
            row.id for row in observations
        }:
            reason = "source_changed"
            frozen = FrozenFragments.load([])
        elif not frozen.event_ids <= context.visible_event_ids and (
            previous.selected_summary_text is None
            or context.projection_scope not in {"", "main", "self_initiative"}
        ):
            reason = "capacity"
            frozen = FrozenFragments.load([])
        elif context.current_event_id in frozen.event_ids:
            # A deliberate repeat of an event is a new attempt, not an append of
            # the same trigger. Preserve that distinction in the epoch reason.
            reason = "source_changed"
            frozen = FrozenFragments.load([])
    if (
        not frozen.items
        and actor_id
        and read_scope
        and reason in {"bootstrap", "capacity", "contract_changed"}
    ):
        # Representation provenance outlives the bounded projection cache. A
        # genuine source/reset change clears it; ordinary eviction does not.
        selected = await sources.selected(
            view_key=view_key,
            conversation_id=conversation_id,
            generation=version.generation,
            actor_id=actor_id,
            read_scope=read_scope,
        )
        allowed_observations = {row.id for row in observations}
        selected = [
            item
            for item in selected
            if (not item["event_ids"] or set(item["event_ids"]) <= context.visible_event_ids)
            and ("observation_id" not in item or item["observation_id"] in allowed_observations)
        ]
        # Grouping can change across an explicit capacity epoch. Replay each
        # original event only once; use the current event fragment for the new
        # portion of an overlapping group rather than duplicating its envelope.
        unique: list[dict[str, Any]] = []
        seen: set[int] = set()
        singles = {
            identity: message
            for ids, message in context.history_event_fragments
            for identity in ids
        }
        for item in selected:
            ids = set(item["event_ids"])
            if ids & seen:
                for identity in item["event_ids"]:
                    if identity not in seen and identity in singles:
                        unique.extend(
                            FrozenFragments.load([])
                            .extend_history((((identity,), singles[identity]),), ())
                            .items
                        )
            else:
                unique.append(item)
            seen.update(ids)
        frozen = FrozenFragments.load(unique)
    selected_context = context
    # Preserve a selected summary while it and all chat fit. New derived rows
    # must not silently replace the prefix or omit newly covered unseen chat.
    if (
        previous is not None
        and previous.selected_summary_text is not None
        and reason is None
        and context.projection_scope in {"", "main", "self_initiative"}
    ):
        ledger = EventLedgerRepository(repository.database)
        after = previous.selected_summary_coverage
        through = max(context.prompt_raw_tail_end_event_id, context.prompt_effective_coverage)
        missing_rows: list[EventRecord] = []
        while after < through:
            page = await ledger.list_scope_after(
                version.scope,
                after_event_id=after,
                through_event_id=through,
                limit=256,
                message_only=True,
            )
            if not page:
                break
            missing_rows.extend(
                row
                for row in page
                if row.id not in frozen.event_ids and row.id != context.current_event_id
            )
            after = page[-1].id
        renderer = ChatEventPromptRenderer(
            missing_rows,
            bot_display_name=context.history_bot_display_name,
            timezone=context.history_timezone,
            yuki_account_ids=context.history_yuki_account_ids,
        )
        additions = tuple(
            (ids, message) for _, ids, message in renderer.main_agent_history(missing_rows)
        )
        individual = tuple(
            (ids, message)
            for row in missing_rows
            for _, ids, message in renderer.main_agent_history((row,))
        )
        selected_context = replace(
            context,
            rollup_text=previous.selected_summary_text,
            prompt_effective_coverage=previous.selected_summary_coverage,
            history_fragments=additions,
            history_event_fragments=individual,
        )
    extended = frozen.extend_history(
        selected_context.history_fragments, selected_context.history_event_fragments
    )
    for observation in observations:
        extended = extended.append_observation(
            observation.id, observation.version, observation.message()
        )
    fresh = FrozenFragments.load([]).extend_history(
        context.history_fragments, context.history_event_fragments
    )
    # Canonical event coverage never proves private clues were summarized.
    for observation in observations:
        fresh = fresh.append_observation(observation.id, observation.version, observation.message())
    fits = (
        context_fits(replace(selected_context, history_messages=extended.messages()))
        if context_fits is not None
        else history_fits(extended.messages())
    )
    hard_fits = (
        context_hard_fits(replace(selected_context, history_messages=extended.messages()))
        if context_hard_fits is not None
        else fits
    )
    original_fragments, original_context, original_reason = extended, selected_context, reason
    ready_rollup = (
        previous is not None
        and context.prompt_effective_coverage > previous.selected_summary_coverage
        and bool(context.rollup_text.strip())
        and context.metrics.rollup_mode != "emergency"
    )
    if extended.items != fresh.items and (not hard_fits or (not fits and ready_rollup)):
        # This preparation is a new activation boundary. A published semantic
        # summary may replace old chat here, never inside an active transcript.
        reason = "capacity" if not hard_fits else "rollup_ready"
        extended = fresh
        selected_context = context
    fits = (
        context_fits(replace(selected_context, history_messages=extended.messages()))
        if context_fits is not None
        else history_fits(extended.messages())
    )
    hard_fits = (
        context_hard_fits(replace(selected_context, history_messages=extended.messages()))
        if context_hard_fits is not None
        else fits
    )
    if not fits and observations and summarize_observations is not None:
        summary = await sources.prepared_summary(
            view_key=view_key,
            observations=observations,
            conversation_id=conversation_id,
            generation=version.generation,
            actor_id=actor_id,
            read_scope=read_scope,
        )
        if summary is None and not hard_fits:
            try:
                payload = await summarize_observations(observations)
                summary = await sources.publish_scope_summary(
                    view_key=view_key,
                    observations=observations,
                    payload=payload,
                    conversation_id=conversation_id,
                    generation=version.generation,
                    actor_id=actor_id,
                    read_scope=read_scope,
                    expected_source_revision=version.prompt_source_revision,
                )
            except (ObservationSummaryError, WorkCapacityError, LLMError) as exc:
                if context_hard_fits is None or not context_hard_fits(
                    replace(original_context, history_messages=original_fragments.messages())
                ):
                    raise
                logger.info("observation_summary_deferred category=%s", type(exc).__name__)
        # A paid candidate is reusable, but not yet observation coverage. Only
        # the dispatch CAS admits it and transfers its artifact ownership.
        candidate = extended
        if summary is not None:
            candidate_items: list[dict[str, Any]] = []
            # A compiled A can own both a chat event and its dynamic snapshot.
            # Summarizing the snapshot cannot silently cover the original chat.
            snapshot_events = {
                identity
                for item in extended.items
                if "observation_id" in item
                for identity in item["event_ids"]
                if identity > selected_context.prompt_effective_coverage
            }
            ledger = EventLedgerRepository(repository.database)
            raw_events = []
            for identity in sorted(snapshot_events):
                event = await ledger.get_event(identity)
                if event is None or event.canonical_conversation_id != conversation_id:
                    from qq_ai_bot.conversation.projections import ProjectionConflict

                    raise ProjectionConflict("snapshot chat source changed")
                raw_events.append(event)
            renderer = ChatEventPromptRenderer(
                raw_events,
                bot_display_name=context.history_bot_display_name,
                timezone=context.history_timezone,
                yuki_account_ids=context.history_yuki_account_ids,
            )
            raw_fragments = {
                event.id: FrozenFragments.load([])
                .extend_history(
                    tuple(
                        (ids, message) for _, ids, message in renderer.main_agent_history((event,))
                    ),
                    (),
                )
                .items[0]
                for event in raw_events
            }
            for item in extended.items:
                if "observation_id" not in item:
                    candidate_items.append(item)
                else:
                    candidate_items.extend(
                        raw_fragments[identity]
                        for identity in item["event_ids"]
                        if identity in raw_fragments
                    )
            candidate = FrozenFragments.load(candidate_items).append_observation(
                summary.id, summary.version, summary.message()
            )
        candidate_fits = (
            context_fits(replace(selected_context, history_messages=candidate.messages()))
            if context_fits is not None
            else history_fits(candidate.messages())
        )
        if summary is not None and candidate_fits:
            extended, reason = candidate, "capacity"
        elif context_hard_fits is not None and context_hard_fits(
            replace(original_context, history_messages=original_fragments.messages())
        ):
            extended, selected_context, reason = (
                original_fragments,
                original_context,
                original_reason,
            )
    # The original assembler has already selected a bounded history. Its rollup
    # is compiled separately, before these event fragments, in every epoch.
    selected_context = replace(
        selected_context,
        history_messages=extended.messages(),
        read_version=replace(
            version,
            visible_event_ids=tuple(
                sorted(
                    extended.event_ids
                    | ({context.current_event_id} if context.current_event_id else set())
                )
            ),
            selected_summary_text=selected_context.rollup_text,
            observation_sources=extended.observation_sources,
            observation_actor_id=actor_id,
            observation_read_scope=read_scope,
        ),
    )
    return PreparedHistory(
        repository=repository,
        view_key=view_key,
        context_key=context_key,
        contract_revision=contract_revision,
        context=selected_context,
        fragments=extended,
        previous=previous,
        reason=reason,
        actor_id=actor_id,
        read_scope=read_scope,
    )
