"""A completed background summary is selected only at a new preparation boundary."""

from tests.unit.rollup_test_helpers import candidate_summary
from tests.unit.test_history_soft_coverage import _history

from qq_ai_bot.conversation.projections import PromptProjectionRepository
from qq_ai_bot.conversation.rollup.models import RollupKind
from qq_ai_bot.domain.conversations import ConversationScope
from qq_ai_bot.persistence.scoped_event_uow import ScopedEventLedgerUnitOfWork
from qq_ai_bot.services.history_projection import prepare_history


async def test_background_commit_keeps_frozen_prefix_then_adopts_once(database):
    assembler, rollups, model, arguments = await _history(database, hold=False)
    projections = PromptProjectionRepository(database)
    context = await assembler.assemble_self_initiative(**arguments)
    options = dict(
        view_key="a" * 64,
        context_key="b" * 64,
        contract_revision="c" * 64,
        history_fits=lambda _: True,
        context_hard_fits=lambda _: True,
    )
    first = await prepare_history(projections, context, context_fits=lambda _: True, **options)
    original_messages = first.fragments.messages()
    await first.commit(first.fragments)
    scope = ConversationScope.group("8000", "2001")
    claim = await rollups.claim_scope_for_foreground(
        scope, lease_owner="background-test", lease_seconds=30
    )
    candidate = await rollups.candidate_for_claim(claim, token_budget=1000)
    assert candidate is not None
    # The summary has already read its sources when a later chat arrives.
    late = await ScopedEventLedgerUnitOfWork(database, config=rollups.config).append(
        scope=scope,
        platform_message_id="late-chat",
        sender_user_id="8000",
        direction="outbound",
        sender_is_bot=True,
        content="chat while the summary was being generated",
    )
    await rollups.commit_candidate(
        claim,
        candidate,
        summary_text=candidate_summary(candidate, "earlier conversation"),
        summary_kind=RollupKind.MODEL,
    )
    assert first.fragments.messages() == original_messages
    fresh = await assembler.assemble_self_initiative(**arguments)
    assert fresh.prompt_effective_coverage > context.prompt_effective_coverage
    stable = await prepare_history(projections, fresh, context_fits=lambda _: True, **options)
    assert stable.context.rollup_text == context.rollup_text
    assert stable.fragments.messages()[: len(original_messages)] == original_messages
    assert late.event.id in stable.fragments.event_ids
    await stable.commit(stable.fragments)
    adopted = await prepare_history(projections, fresh, context_fits=lambda _: False, **options)
    assert adopted.reason == "rollup_ready"
    assert adopted.context.rollup_text == fresh.rollup_text
    assert late.event.id in adopted.fragments.event_ids
    assert all(
        identity > fresh.prompt_effective_coverage for identity in adopted.fragments.event_ids
    )
    selected = await adopted.commit(adopted.fragments)
    again = await prepare_history(projections, fresh, context_fits=lambda _: False, **options)
    assert again.reason is None
    retained = await again.commit(again.fragments)
    assert retained.epoch_id == selected.epoch_id
    assert model.requests == []
