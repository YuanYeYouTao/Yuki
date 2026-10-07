"""Audit F4 triggers with corrected invariants; real SQLite publication retained."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from tests.support.correctness_wire import KINDS
from tests.unit.test_history_preparation_reuse import context, snapshot

from qq_ai_bot.conversation.frozen_fragments import FrozenFragments
from qq_ai_bot.domain.messages import ChatMessage, ChatRequest
from qq_ai_bot.persistence.event_repository import EventLedgerRepository
from qq_ai_bot.services.history_projection import prepare_history
from qq_ai_bot.services.prompt_composer import PromptComposer


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("mode", ["emergency", "model"])
async def test_frozen_emergency_summary_keeps_representation_without_epoch(
    database, monkeypatch, kind, mode
):
    old = context((((1,), ChatMessage("user", "original raw history")),))
    old = replace(
        old,
        rollup_text="truncated old emergency facts",
        prompt_effective_coverage=0,
        metrics=replace(old.metrics, rollup_mode=mode),
    )
    frozen = FrozenFragments.load([]).extend_history(
        old.history_fragments, old.history_event_fragments
    )
    previous = snapshot(frozen, summary=old.rollup_text, coverage=0, kind=mode)
    incoming = replace(
        old,
        rollup_text="new completed semantic summary",
        metrics=replace(old.metrics, rollup_mode="model" if mode == "emergency" else "emergency"),
    )
    monkeypatch.setattr(
        EventLedgerRepository,
        "read_scope_missing_history",
        AsyncMock(return_value=(incoming.read_version, ())),
    )
    repository = SimpleNamespace(database=database, read=AsyncMock(return_value=previous))
    prepared = await prepare_history(
        repository,
        incoming,
        view_key="a" * 64,
        context_key="b" * 64,
        contract_revision="c" * 64,
        history_fits=lambda _: True,
        context_fits=lambda _: True,
        context_hard_fits=lambda _: True,
    )
    assert prepared.reason is None
    assert prepared.context.rollup_text == old.rollup_text
    before = PromptComposer._conversation_history(old)
    after = PromptComposer._conversation_history(prepared.context)
    label = "Incomplete emergency" if mode == "emergency" else "Conversation summary"
    assert label in before[0].content and label in after[0].content
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))
    ) as client:
        adapter_class, _, _ = KINDS[kind]
        adapter = adapter_class(
            base_url="https://wire.invalid",
            api_key="synthetic",
            timeout_seconds=1,
            max_retries=0,
            client=client,
        )
        wires = [
            adapter._build_payload(
                ChatRequest(
                    messages=(
                        ChatMessage("system", "fixed"),
                        *messages,
                        ChatMessage("user", "current"),
                    ),
                    model="fixed",
                )
            )
            for messages in (before, after)
        ]
    assert wires[0] == wires[1]


@pytest.mark.asyncio
async def test_real_background_catchup_preserves_frozen_emergency(database):
    from tests.unit.rollup_test_helpers import candidate_summary
    from tests.unit.test_work_effective_rollup_source import _candidate_with_overlay

    from qq_ai_bot.conversation.projections import PromptProjectionRepository
    from qq_ai_bot.conversation.rollup.models import RollupKind

    repository, scope, claim, candidate, _, _ = await _candidate_with_overlay(
        database, catches_up=True
    )
    ledger = EventLedgerRepository(database)

    async def current():
        version, events = await ledger.read_scope_context(scope, limit=256)
        loaded = await repository.load_prompt_snapshot(scope)
        old = context(
            tuple(
                ((e.id,), ChatMessage("user", e.content))
                for e in events
                if e.id > loaded.effective_coverage
            )
        )
        return replace(
            old,
            read_version=version,
            current_event_id=None,
            rollup_text=loaded.rollup.summary_text,
            prompt_effective_coverage=loaded.effective_coverage,
            prompt_raw_tail_end_event_id=max(e.id for e in events),
            visible_event_ids=frozenset(e.id for e in events),
            metrics=replace(old.metrics, rollup_mode=loaded.rollup.summary_kind.value),
        )

    projections = PromptProjectionRepository(database)

    async def prepare(ctx):
        return await prepare_history(
            projections,
            ctx,
            view_key="a" * 64,
            context_key="b" * 64,
            contract_revision="c" * 64,
            history_fits=lambda _: True,
            context_fits=lambda _: True,
            context_hard_fits=lambda _: True,
        )

    old = await current()
    first = await prepare(old)
    committed = await first.commit(first.fragments)
    await repository.commit_candidate(
        claim,
        candidate,
        summary_text=candidate_summary(candidate, "new semantic facts"),
        summary_kind=RollupKind.MODEL,
    )
    incoming = await current()
    assert old.metrics.rollup_mode == "emergency" and incoming.metrics.rollup_mode == "model"
    # Reload the persisted projection after a real connection-pool restart;
    # activation-local objects cannot supply its frozen representation.
    await database.close()
    projections = PromptProjectionRepository(database)
    second = await prepare(incoming)
    assert second.reason is None
    assert second.context.rollup_text == old.rollup_text
    again = await second.commit(second.fragments)
    assert again.epoch_id == committed.epoch_id
    before = PromptComposer._conversation_history(first.context)
    after = PromptComposer._conversation_history(second.context)
    assert before[0] == after[0]
    assert (
        "Incomplete emergency" in before[0].content and "Incomplete emergency" in after[0].content
    )
