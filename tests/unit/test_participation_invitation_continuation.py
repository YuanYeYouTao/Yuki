"""Real Host admission/receipts with synthetic Jev, no model or QQ requests."""

import asyncio
import time
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from tests.unit.test_semantic_participation_host import _event_and_route, _host, _item, _proposal

from qq_ai_bot.conversation.ordinary_admission import OrdinaryAdmissionRepository
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.services.participation_feedback import sync_scope_effects
from qq_ai_bot.social.db_models import SocialOperationModel


@pytest.mark.parametrize(
    ("old_pending", "master_enabled", "prior_context"),
    [(False, True, False), (True, True, False), (False, False, False), (False, True, True)],
)
async def test_real_invitation_promotes_human_and_confirmed_reply_continues_without_jev(
    database, tmp_path, old_pending, master_enabled, prior_context
):
    host, policy = await _host(database, tmp_path)
    policy.autonomous_enabled = master_enabled
    promoted = asyncio.Event()
    calls = []

    async def promote(event_id, frozen):
        source = await host.app.ledger.get_event(event_id)
        prepared = host.ordinary.prepare(source, frozen.generation, 1, frozen)
        assert await host.ordinary.commit(prepared)
        await host.on_ordinary_admitted(frozen, prepared.admission)
        calls.append((event_id, frozen))
        promoted.set()

    host.set_promoter(promote)
    try:
        if prior_context:
            await host.app.ledger.append(
                bot_user_id="8000",
                platform_message_id=str(uuid4()),
                scope_type=ScopeType.GROUP,
                sender_user_id="1002",
                direction="inbound",
                content="这里还有一条先前的群消息",
                group_id="2001",
                occurred_at=datetime.now(UTC) - timedelta(seconds=60),
            )
        first = await _event_and_route(database, host.app.ledger)
        item = await _item(host, first)
        binding = await host._binding(item)
        assert binding.master_enabled is master_enabled
        assert binding.external_enabled
        source = item.controller.state.events[f"event:{first.id}"]
        # The real queue/evaluator generates the invitation interpretation once.
        await item.observation.evaluate_due(time.time(), active=False)
        assert len(host._observer.calls) == 1
        if prior_context:
            assert host._observer.calls[0].context
        if old_pending:
            pending = _proposal(item, binding, source)
            assert (
                await host.repository.query_proposal(
                    conversation_id=pending.scope.conversation_id,
                    generation=pending.scope.generation,
                    owner=binding.effective_owner,
                    controller_epoch=pending.controller_epoch,
                    proposal_id=pending.proposal_id,
                )
                is None
            )
        await host._advance_scene(item)
        await asyncio.wait_for(promoted.wait(), timeout=5)
        assert len(calls) == 1 and calls[0][0] == first.id
        assert await host.ordinary.current(first.id) is not None
        assert not await host.repository.list_active()
        assert not await host.work.by_source(f"event:{first.id}")
        assert not item.controller.state.proposals
        assert not item.controller.state.feedback

        if prior_context:
            # This case verifies the real non-empty interpretation basis through
            # promotion. The other person's pending focus remains legitimate.
            assert len(calls[0][1].basis) == 2
            return

        # An admission alone is not confirmation that Yuki spoke. A known group
        # send supplies the expression while retaining the transport target.
        own, _ = await host.app.ledger.append(
            bot_user_id="8000",
            platform_message_id=str(uuid4()),
            scope_type=ScopeType.GROUP,
            sender_user_id="8000",
            direction="outbound",
            content="我们继续这个问题。",
            group_id=first.group_id,
            occurred_at=datetime.now(UTC),
            origin="social_tool",
        )
        async with database.immediate_session() as session:
            session.add(
                SocialOperationModel(
                    id=str(uuid4()),
                    source_turn_id=f"{item.scene.conversation_id}:event:{first.id}",
                    tool_call_id="actual-reply",
                    source_conversation_id=item.scene.conversation_id,
                    action="send_message",
                    payload_hash="0" * 64,
                    target_kind="space",
                    target_id=item.scene.space_id,
                    presence_id=first.ingress_presence_id,
                    status="succeeded",
                    event_id=own.id,
                    created_at=datetime.now(UTC),
                    updated_at=datetime.now(UTC),
                )
            )
        await sync_scope_effects(host, item)
        await host._hydrate(item)
        await sync_scope_effects(host, item)
        assert any(u.expressed for u in item.controller.participating_units(time.time()))

        for text in ("那另一种情况呢？", "继续说下去吧"):
            following, _ = await host.app.ledger.append(
                bot_user_id="8000",
                platform_message_id=str(uuid4()),
                scope_type=ScopeType.GROUP,
                sender_user_id="1001",
                direction="inbound",
                content=text,
                group_id=first.group_id,
                occurred_at=datetime.now(UTC),
            )
            frozen = await host.continuation_for_event(following.id)
            assert frozen is not None and frozen.unit_key == calls[0][1].unit_key
            prepared = OrdinaryAdmissionRepository.prepare(following, frozen.generation, 1, frozen)
            assert await host.ordinary.commit(prepared)
            await host.on_ordinary_admitted(frozen, prepared.admission)
            assert not item.observation.queue.pending
            await item.observation.evaluate_due(time.time(), active=True)
            assert len(host._observer.calls) == 1
        assert len(item.controller.state.effects) == 1
        assert next(iter(item.controller.state.effects.values())).effect.actual_targets == (
            "group",
        )
    finally:
        await host.close()
