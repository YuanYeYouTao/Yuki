"""Synthetic host integration; no provider request or QQ send."""

import time
from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from tests.unit.test_self_initiative_memory_quality import reflection_fact
from tests.unit.test_semantic_participation_host import (
    _event_and_route,
    _host,
    _item,
    _observation,
    _proposal,
    _runs,
)
from yuki_participation.models import CandidateKind, Proposal, Snapshot

from qq_ai_bot.conversation.autonomy_db_models import InitiativeSourceClaimModel
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.services.participation_feedback import sync_scope_effects
from qq_ai_bot.social.db_models import SocialOperationModel

pytestmark = pytest.mark.asyncio


async def test_group_memory_seed_does_not_require_recent_human(database, tmp_path):
    _, _, _, _, fact_id = await reflection_fact(database)
    host, _ = await _host(database, tmp_path)
    try:
        current = await _event_and_route(database, host.app.ledger, group="3001")
        item = await _item(host, current)
        item.controller.state.events.clear()
        await host._seeds(item)
        seed = item.controller.state.events[f"memory:{fact_id}"]
        assert seed.kind == "seed" and seed.target == "group"
    finally:
        await host.close()


async def test_tick_discovers_semantic_scope_without_new_message(database, tmp_path):
    host, _ = await _host(database, tmp_path)
    try:
        first = await _event_and_route(database, host.app.ledger)
        item = await _item(host, first)
        await host._binding(item)
        host._sessions.clear()  # Simulate a scope absent from the bounded cache.
        host._advance_scene = AsyncMock()
        host._reconcile = AsyncMock()
        await host.tick()
        assert (first.canonical_conversation_id, 1) in host._sessions
        host._advance_scene.assert_awaited_once()
    finally:
        await host.close()


async def test_newer_unscored_turn_fences_old_host_admission(database, tmp_path):
    host, _ = await _host(database, tmp_path)
    try:
        first = await _event_and_route(database, host.app.ledger)
        item = await _item(host, first)
        source = item.controller.state.events[f"event:{first.id}"]
        binding = await host._binding(item)
        proposal = _proposal(item, binding, source)
        await host.app.ledger.append(
            bot_user_id="8000",
            platform_message_id=str(uuid4()),
            scope_type=ScopeType.GROUP,
            sender_user_id="1001",
            direction="inbound",
            content="先别回答，这个问题已经解决。",
            group_id=first.group_id,
            occurred_at=datetime.now(UTC),
        )
        await host._hydrate(item)
        await host._admit(item, binding, proposal)
        assert not await _runs(host)
    finally:
        await host.close()


async def test_source_free_intrinsic_admission_and_confirmed_send_thread(database, tmp_path):
    host, _ = await _host(database, tmp_path)
    try:
        first = await _event_and_route(database, host.app.ledger)
        item = await _item(host, first)
        binding = await host._binding(item)
        now = time.time()
        proposal = Proposal(
            proposal_id=str(uuid4()),
            scope=item.scene.scope,
            controller_epoch=binding.controller_epoch,
            kind=CandidateKind.INTRINSIC,
            thread="intrinsic:1",
            target_hint="group",
            sources=(),
            support=None,
            created_at=now,
            expires_at=now + 60,
        )
        item.controller.state.proposals[proposal.proposal_id] = proposal
        item.controller._set(pending=proposal.proposal_id)
        await host._admit(item, binding, proposal)
        runs = await _runs(host)
        assert len(runs) == 1
        assert runs[0].trigger_kind == "intrinsic" and runs[0].thread_key == proposal.thread
        assert runs[0].sources_json == "[]" and runs[0].support_refs_json == "[]"
        async with database.sessions() as session:
            assert (
                await session.scalar(select(func.count()).select_from(InitiativeSourceClaimModel))
                == 0
            )

        own, _ = await host.app.ledger.append(
            bot_user_id="8000",
            platform_message_id=str(uuid4()),
            scope_type=ScopeType.GROUP,
            sender_user_id="8000",
            direction="outbound",
            content="我想到一个新话题。",
            group_id=first.group_id,
            occurred_at=datetime.now(UTC),
            origin="system_task",
        )
        await host._hydrate(item)
        assert item.controller.state.events[f"event:{own.id}"].thread != proposal.thread
        async with database.sessions() as session, session.begin():
            session.add(
                SocialOperationModel(
                    id=str(uuid4()),
                    source_turn_id=f"{runs[0].conversation_id}:initiative:{runs[0].id}",
                    tool_call_id="fixture-send",
                    source_conversation_id=runs[0].conversation_id,
                    action="send_message",
                    payload_hash="0" * 64,
                    target_kind="space",
                    target_id=item.scene.space_id,
                    presence_id=item.scene.presence_id,
                    status="succeeded",
                    event_id=own.id,
                    created_at=datetime.now(UTC),
                    updated_at=datetime.now(UTC),
                )
            )
        await sync_scope_effects(host, item)
        await host._hydrate(item)
        assert item.controller.state.events[f"event:{own.id}"].thread == proposal.thread
        anchor_key = f"event:{own.id}"
        anchor_event = item.controller.state.events[anchor_key]
        item.controller.state.events[anchor_key] = anchor_event.model_copy(
            update={"at": anchor_event.at - 400}
        )

        for index in range(6):
            await host.app.ledger.append(
                bot_user_id="8000",
                platform_message_id=str(uuid4()),
                scope_type=ScopeType.GROUP,
                sender_user_id="1002",
                direction="inbound",
                content=f"旁人的话题 {index}",
                group_id=first.group_id,
                occurred_at=datetime.now(UTC),
            )
        await host._hydrate(item)
        answer, _ = await host.app.ledger.append(
            bot_user_id="8000",
            platform_message_id=str(uuid4()),
            scope_type=ScopeType.GROUP,
            sender_user_id="1001",
            direction="inbound",
            content="这个话题可以继续说说吗？",
            group_id=first.group_id,
            occurred_at=datetime.now(UTC),
        )
        await host._hydrate(item)
        reply = item.controller.state.events[f"event:{answer.id}"]
        anchor = item.controller.state.events[f"event:{own.id}"]
        option = next(option for option in reply.unit_options if option.self_anchor == anchor.ref)
        assert reply.reply_to is None and option.thread == proposal.thread
        snapshot = Snapshot(
            scope=item.scene.scope,
            focus=reply,
            context=(anchor,),
            sequence=1,
            issued_at=time.time(),
            kind=CandidateKind.CONVERSATION,
        )
        assert item.controller.apply_semantic_observation(
            _observation(snapshot, act="extend_yuki", unit=option.key)
        )
        assert (
            item.controller.state.observations[reply.ref.event_id].matching_self_anchor
            == anchor.ref
        )
    finally:
        await host.close()
