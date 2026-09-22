"""New admission deadline and internal reply projection regression boundaries."""

import time
from datetime import UTC, datetime

import pytest
from tests.unit.test_semantic_participation_host import (
    _event_and_route,
    _host,
    _item,
    _proposal,
    _runs,
    _score,
)

from qq_ai_bot.conversation.autonomy_binding import AutonomyOwner
from qq_ai_bot.domain.conversations import ScopeType

pytestmark = pytest.mark.asyncio


async def test_commit_deadline_rejects_new_proposal_but_does_not_lose_accepted_run(
    database, tmp_path
):
    host, _ = await _host(database, tmp_path)
    try:
        event = await _event_and_route(database, host.app.ledger)
        item = await _item(host, event)
        binding = await host._binding(item)
        source = item.controller.state.events[f"event:{event.id}"]
        proposal = _proposal(item, binding, source)
        args = dict(
            proposal_id=proposal.proposal_id,
            binding=binding,
            owner=AutonomyOwner.SEMANTIC,
            space_id=item.scene.space_id,
            presence_id=item.scene.presence_id,
            sources=(host._source(item, source.ref),),
            source_guard=(host._source(item, source.ref),),
            support_refs=(proposal.support.observation_id,),
        )
        expired = await host.repository.accept_host_proposal(**args, expires_at=time.time() - 1)
        assert expired.outcome == "expired" and not await _runs(host)
        accepted = await host.repository.accept_host_proposal(**args, expires_at=time.time() + 30)
        replay = await host.repository.accept_host_proposal(**args, expires_at=time.time() - 1)
        assert replay.outcome == "duplicate" and replay.run.run_id == accepted.run.run_id
    finally:
        await host.close()


async def test_real_quote_preserves_resolved_discussion_and_group_self_target(database, tmp_path):
    host, _ = await _host(database, tmp_path)
    try:
        first = await _event_and_route(database, host.app.ledger)
        item = await _item(host, first)
        _score(item, item.controller.state.events[f"event:{first.id}"])
        own, _ = await host.app.ledger.append(
            bot_user_id="8000",
            platform_message_id="quote-self",
            scope_type=ScopeType.GROUP,
            sender_user_id="8000",
            direction="outbound",
            content="先把图片分成两层。",
            group_id=first.group_id,
            occurred_at=datetime.now(UTC),
            caused_by_event_id=first.id,
            origin="system_task",
        )
        await host._hydrate(item)
        answer, _ = await host.app.ledger.append(
            bot_user_id="8000",
            platform_message_id="quote-human",
            scope_type=ScopeType.GROUP,
            sender_user_id="1001",
            direction="inbound",
            content="你说的第二层怎么做？",
            group_id=first.group_id,
            occurred_at=datetime.now(UTC),
            reply_to_event_id=own.id,
        )
        await host._hydrate(item)
        anchor = item.controller.state.events[f"event:{own.id}"]
        reply = item.controller.state.events[f"event:{answer.id}"]
        assert anchor.target == "group"
        assert anchor.thread == f"event:{first.id}"
        assert reply.reply_to == anchor.ref and reply.thread == anchor.thread
        assert not reply.unit_ambiguous
        _score(item, reply, act="extend_yuki")
        assert (
            item.controller.state.observations[reply.ref.event_id].matching_self_anchor
            == anchor.ref
        )
    finally:
        await host.close()
