"""Selected routes send once; batched replay preserves original confirmed content."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import event, select
from tests.support.social_identity_cases import social_env

from yuki.admin.models import ReplyRuntimeConfig
from yuki.execution_trace.phases import current_metrics
from yuki.identity.routing import RouteSendError
from yuki.persistence.models import ChatEventModel


@pytest.mark.parametrize("current_entry", [False, True])
async def test_known_group_route_sends_without_remote_membership_preflight(
    database, tmp_path, current_entry
):
    env = await social_env(database, tmp_path)
    async with database.sessions() as reader:
        source = await reader.scalar(select(ChatEventModel))
    context = replace(
        env.context,
        trigger_event_id=source.id if current_entry else None,
        presence_id=env.presence if current_entry else None,
    )
    env.bot.missing.add(env.bot.self_id)
    env.bot.calls.clear()
    result = await env.service.execute("send_message", {"text": "actual delivery"}, context)
    assert result["status"] == "succeeded"
    assert [action for action, _ in env.bot.calls] == ["send_group_msg"]
    # Multi-candidate discovery still evaluates reachability; send does not invent a fallback.
    with pytest.raises(RouteSendError, match="group_unavailable"):
        await env.router.accessible_group_connections(env.space)


async def test_sequence_replay_batches_reads_and_retains_origin_privacy(database, tmp_path):
    env = await social_env(database, tmp_path)
    counts = []
    for count in (2, 12):
        context = replace(
            env.context,
            call_id=f"sequence-{count}",
            runtime_snapshot=SimpleNamespace(reply=ReplyRuntimeConfig(0, 0, 1800, 20)),
        )
        args = {"text": "\n".join(f"part {index}" for index in range(count))}
        sent = await env.service.execute("send_message", args, context)
        assert sent["status"] == "succeeded" and sent["sent_messages"] == count
        statements = []

        def capture(
            _connection, _cursor, statement, _parameters, _context, _many, *, statements=statements
        ):
            statements.append(statement)

        event.listen(database.engine.sync_engine, "before_cursor_execute", capture)
        try:
            replay = await env.service.execute("send_message", args, context)
        finally:
            event.remove(database.engine.sync_engine, "before_cursor_execute", capture)
        assert replay == sent
        counts.append(len(statements))
        assert [part["delivered_text"] for part in replay["parts"]] == [
            f"part {index}" for index in range(count)
        ]
        # A different source cannot expose the original content, even though success remains fact.
        isolated = await env.service.execute(
            "send_message", args, replace(context, origin="another_source")
        )
        assert isolated["status"] == "succeeded"
        assert all(not part["delivered_text_available"] for part in isolated["parts"])
    assert counts[1] == counts[0], "sequence replay must not add per-part database round trips"


async def test_sequence_keeps_configured_delay_between_confirmed_parts(
    database, tmp_path, monkeypatch
):
    env = await social_env(database, tmp_path)
    context = replace(
        env.context,
        runtime_snapshot=SimpleNamespace(reply=ReplyRuntimeConfig(1.5, 1.5, 1800, 10)),
    )
    sends_at_sleep = []

    async def delay(seconds):
        assert seconds == 1.5
        sends_at_sleep.append(sum(action == "send_group_msg" for action, _ in env.bot.calls))

    sleeper = AsyncMock(side_effect=delay)
    monkeypatch.setattr("yuki.social.service.asyncio.sleep", sleeper)
    result = await env.service.execute("send_message", {"text": "first\nsecond\nthird"}, context)
    assert result["status"] == "succeeded"
    assert sends_at_sleep == [1, 2]


async def test_delivery_timings_separate_platform_wait_from_claim_and_settlement(
    database, tmp_path
):
    env = await social_env(database, tmp_path)
    metrics = {}
    token = current_metrics.set(metrics)
    try:
        result = await env.service.execute("send_message", {"text": "timed delivery"}, env.context)
    finally:
        current_metrics.reset(token)
    assert result["status"] == "succeeded"
    assert {
        "social_preparation_seconds",
        "social_claim_seconds",
        "social_transport_seconds",
        "social_settlement_seconds",
    } <= metrics.keys()
    assert all(value >= 0 for value in metrics.values())
