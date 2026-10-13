"""Prepare and atomically freeze the selected history of a Main Agent turn."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

from qq_ai_bot.conversation.frozen_fragments import FrozenFragments
from qq_ai_bot.conversation.projections import (
    SUMMARY_RENDERER_VERSION,
    ProjectionPublication,
    ProjectionSnapshot,
    PromptProjectionRepository,
)
from qq_ai_bot.domain.messages import ChatMessage
from qq_ai_bot.event_prompt import ChatEventPromptRenderer
from qq_ai_bot.execution_trace.phases import collect_phase_metrics
from qq_ai_bot.persistence.event_repository import EventLedgerRepository
from qq_ai_bot.services.context_assembler import AssembledContext

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class PreparedHistory:
    repository: PromptProjectionRepository
    view_key: str
    context_key: str
    context: AssembledContext
    fragments: FrozenFragments
    previous: ProjectionSnapshot | None
    reason: str | None
    committed: ProjectionSnapshot | None = None
    committed_input: list[dict[str, Any]] | None = None
    previous_item_count: int | None = None

    async def prepare_commit(
        self,
        fragments: FrozenFragments,
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
            items=list(fragments.items),
            expected_epoch=previous.epoch_id if previous else None,
            expected_revision=previous.revision if previous else 0,
            rebuild_reason=None if self.committed is not None else self.reason,
            selected_summary_text=self.context.rollup_text,
            selected_summary_coverage=self.context.prompt_effective_coverage,
            selected_summary_kind=self.context.metrics.rollup_mode,
            selected_summary_renderer=SUMMARY_RENDERER_VERSION,
            previous_snapshot=previous,
            previous_item_count=(
                (len(self.committed_input) if self.committed_input is not None else None)
                if self.committed is not None
                else self.previous_item_count
            ),
        )

    async def commit(
        self,
        fragments: FrozenFragments,
    ) -> ProjectionSnapshot:
        """Call after authority/source validation, immediately before model dispatch.

        Retries reuse this immutable preparation; no model output is interpreted
        as a platform send, and concurrent preparations cannot replace each other.
        """
        if self.committed is not None:
            if self.committed_input == list(fragments.items):
                return self.committed
        publication = await self.prepare_commit(fragments)
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
    history_fits: Callable[[tuple[ChatMessage, ...]], bool],
    context_fits: Callable[[AssembledContext], bool] | None = None,
    context_hard_fits: Callable[[AssembledContext], bool] | None = None,
) -> PreparedHistory:
    """Append canonical public history in the Conversation's existing epoch."""
    if context.read_version is None or context.read_version.conversation_id is None:
        raise ValueError("projection requires a canonical read version")
    previous = await repository.read(view_key)
    previous_fragments = FrozenFragments.load(previous.items()) if previous is not None else None
    version = context.read_version
    conversation_id = version.conversation_id
    if conversation_id is None:
        raise ValueError("projection requires a canonical conversation")
    reason = None
    invalidated_reason = None
    previous_item_count = None
    frozen = FrozenFragments.load([])
    frozen_event_ids: frozenset[int] = frozenset()
    if previous is None:
        invalidated_reason = await repository.invalidation_reason(view_key)
        reason = invalidated_reason or "bootstrap"
    elif previous.selected_summary_text and (
        previous.selected_summary_kind is None
        or previous.selected_summary_renderer != SUMMARY_RENDERER_VERSION
    ):
        # Legacy text did not record its renderer/kind. Rebuild from currently
        # authorized source instead of guessing a label for the old body.
        reason = "rollup"
    else:
        assert previous_fragments is not None
        frozen = previous_fragments
        previous_item_count = len(frozen.items)
        frozen_event_ids = frozen.event_ids
        if (
            not frozen_event_ids <= context.visible_event_ids
            and previous.selected_summary_text is None
        ):
            reason = "capacity"
            frozen = FrozenFragments.load([])
            frozen_event_ids = frozenset()
    if not frozen.items:
        # Representation provenance outlives the bounded projection cache. A
        # genuine source/reset change clears it; ordinary eviction does not.
        selected = await repository.selected(
            view_key=view_key,
            conversation_id=conversation_id,
            generation=version.generation,
            visible_event_ids=context.visible_event_ids,
        )
        selected = [
            item
            for item in selected
            if (not item["event_ids"] or set(item["event_ids"]) <= context.visible_event_ids)
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
        frozen_event_ids = frozen.event_ids
    collect_phase_metrics(F=len(frozen.items), E=len(frozen_event_ids))
    selected_context = context
    # Preserve a selected summary while it and all chat fit. New derived rows
    # must not silently replace the prefix or omit newly covered unseen chat.
    if previous is not None and previous.selected_summary_text is not None and reason is None:
        ledger = EventLedgerRepository(repository.database)
        through = max(context.prompt_raw_tail_end_event_id, context.prompt_effective_coverage)
        current_version, missing_rows = await ledger.read_scope_missing_history(
            version,
            after_event_id=previous.selected_summary_coverage,
            through_event_id=through,
            frozen_event_ids=frozen_event_ids,
            current_event_id=context.current_event_id,
        )
        if current_version != version:
            from qq_ai_bot.services.turn_coordinator import HistorySourceChangedError

            raise HistorySourceChangedError(version)
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
            metrics=replace(context.metrics, rollup_mode=previous.selected_summary_kind),
            prompt_effective_coverage=previous.selected_summary_coverage,
            history_fragments=additions,
            history_event_fragments=individual,
        )
    extended = frozen.extend_history(
        selected_context.history_fragments, selected_context.history_event_fragments
    )

    selected_context = replace(selected_context, history_messages=extended.messages())
    collect_phase_metrics(candidate_count=1)
    extended_messages = extended.messages()
    fits = (
        context_fits(replace(selected_context, history_messages=extended_messages))
        if context_fits is not None
        else history_fits(extended_messages)
    )
    hard_fits = (
        context_hard_fits(replace(selected_context, history_messages=extended_messages))
        if context_hard_fits is not None
        else fits
    )
    ready_rollup = (
        previous is not None
        and context.prompt_effective_coverage > previous.selected_summary_coverage
        and bool(context.rollup_text.strip())
        and context.metrics.rollup_mode != "emergency"
    )
    if not hard_fits or (not fits and ready_rollup):
        candidate_events = {
            identity for ids, _message in context.history_fragments for identity in ids
        }
        fresh = FrozenFragments.load(
            [
                item
                for item in frozen.items
                if item["event_ids"] and set(item["event_ids"]) <= candidate_events
            ]
        ).extend_history(context.history_fragments, context.history_event_fragments)
        collect_phase_metrics(candidate_count=1)
        if extended.items != fresh.items:
            # A new activation may adopt the already published summary.
            reason = "capacity" if not hard_fits else "rollup_ready"
            extended = fresh
            selected_context = replace(context, history_messages=fresh.messages())
            extended_messages = extended.messages()
            fits = (
                context_fits(replace(selected_context, history_messages=extended_messages))
                if context_fits is not None
                else history_fits(extended_messages)
            )
            hard_fits = (
                context_hard_fits(replace(selected_context, history_messages=extended_messages))
                if context_hard_fits is not None
                else fits
            )
    # The original assembler has already selected a bounded history. Its rollup
    # is compiled separately, before these event fragments, in every epoch.
    selected_context = replace(
        selected_context,
        history_messages=extended_messages,
        read_version=replace(
            version,
            visible_event_ids=tuple(
                sorted(
                    extended.event_ids
                    | ({context.current_event_id} if context.current_event_id else set())
                )
            ),
            selected_summary_text=selected_context.rollup_text,
        ),
    )
    return PreparedHistory(
        repository=repository,
        view_key=view_key,
        context_key=context_key,
        context=selected_context,
        fragments=extended,
        previous=previous,
        reason=reason,
        previous_item_count=previous_item_count,
    )
