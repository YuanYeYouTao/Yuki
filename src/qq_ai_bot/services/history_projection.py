"""Prepare and atomically freeze the selected history of a Main Agent turn."""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace

from qq_ai_bot.conversation.frozen_fragments import FrozenFragments
from qq_ai_bot.conversation.projections import (
    ProjectionSnapshot,
    PromptProjectionRepository,
)
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
    committed: ProjectionSnapshot | None = None

    async def commit(self, fragments: FrozenFragments) -> ProjectionSnapshot:
        """Call after authority/source validation, immediately before model dispatch.

        Retries reuse this immutable preparation; no model output is interpreted
        as a platform send, and concurrent preparations cannot replace each other.
        """
        if self.committed is not None:
            if self.committed.items() == list(fragments.items):
                return self.committed
        version = self.context.read_version
        if version is None or version.conversation_id is None:
            raise ValueError("projection requires a canonical read version")
        previous = self.committed or self.previous
        self.committed = await self.repository.commit(
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
        )
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
    max_history_characters: int,
) -> PreparedHistory:
    """The caller supplies the read-policy identity, never just a sending route.

    An epoch can accumulate only currently selected events. A narrower read
    window starts a fresh epoch so old private/dynamic material cannot bypass
    the caller's current selection or capacity limits.
    """
    if context.read_version is None or context.read_version.conversation_id is None:
        raise ValueError("projection requires a canonical read version")
    previous = await repository.read(view_key)
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
        if not frozen.event_ids <= context.visible_event_ids:
            reason = "capacity"
            frozen = FrozenFragments.load([])
        elif context.current_event_id in frozen.event_ids:
            # A deliberate repeat of an event is a new attempt, not an append of
            # the same trigger. Preserve that distinction in the epoch reason.
            reason = "source_changed"
            frozen = FrozenFragments.load([])
    extended = frozen.extend_history(context.history_fragments, context.history_event_fragments)
    if sum(len(item.content or "") for item in extended.messages()) > max_history_characters:
        reason = "capacity"
        extended = FrozenFragments.load([]).extend_history(
            context.history_fragments, context.history_event_fragments
        )
    # The original assembler has already selected a bounded history. Its rollup
    # is compiled separately, before these event fragments, in every epoch.
    selected_context = replace(context, history_messages=extended.messages())
    return PreparedHistory(
        repository=repository,
        view_key=view_key,
        context_key=context_key,
        contract_revision=contract_revision,
        context=selected_context,
        fragments=extended,
        previous=previous,
        reason=reason,
    )
