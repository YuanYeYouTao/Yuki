"""Independent social preflight and durable operation ownership."""

import asyncio
from dataclasses import replace
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import select, update
from tests.support.canonical_identity import ensure_space
from tests.support.social_identity_cases import social_env

from yuki.conversation.canonical_db_models import SpaceActiveRouteModel
from yuki.gateway.social import TransportMessage
from yuki.identity.db_models import CanonicalSpaceModel, PresenceModel, SpaceBindingModel
from yuki.identity.routing import RouteSendError
from yuki.persistence.database import Database
from yuki.persistence.models import ChatEventModel
from yuki.social.models import SocialError, SocialTarget


@pytest.mark.parametrize("enabled", [True, False])
async def test_explicit_group_send_does_not_require_autonomous_participation(
    database, tmp_path, enabled
):
    env = await social_env(database, tmp_path)
    async with database.immediate_session() as writer:
        await writer.execute(
            update(CanonicalSpaceModel)
            .where(CanonicalSpaceModel.id == env.space)
            .values(autonomous_enabled=False, enabled=enabled)
        )
    if not enabled:
        with pytest.raises(SocialError, match="space_not_allowed"):
            await env.service.execute("send", {"text": "explicit report"}, env.context)
        assert not any(action == "send_group_msg" for action, _ in env.bot.calls)
    else:
        result = await env.service.execute("send", {"text": "explicit report"}, env.context)
        assert result["status"] == "succeeded"
        assert sum(action == "send_group_msg" for action, _ in env.bot.calls) == 1


@pytest.mark.asyncio
async def test_slow_cross_space_send_does_not_block_current_group_reply(
    database: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = await social_env(database, tmp_path)
    async with database.immediate_session() as session:
        other = await ensure_space(session, "20002")
        await session.execute(
            update(CanonicalSpaceModel)
            .where(CanonicalSpaceModel.id == other)
            .values(autonomous_enabled=True)
        )
    assert await env.router.cas_takeover_space(other) == "taken"
    other_target = SocialTarget(kind="space", id=UUID(other))
    other_route = await env.service.send_route(other_target, env.context)
    async with database.read_session() as session:
        event_id = await session.scalar(select(ChatEventModel.id).limit(1))
    current_context = replace(env.context, call_id="current", trigger_event_id=event_id)
    current_target = SocialTarget(kind="space", id=UUID(env.space))
    current_route = await env.service.send_route(current_target, current_context)
    entered, release = asyncio.Event(), asyncio.Event()
    original = env.bot.call_api

    async def slow_send(action, **params):
        if action == "send_group_msg" and params["group_id"] == 20002:
            entered.set()
            await release.wait()
        return await original(action, **params)

    monkeypatch.setattr(env.bot, "call_api", slow_send)
    slow = asyncio.create_task(
        env.service._effect(
            "send_message",
            {"text": "other"},
            replace(env.context, call_id="other"),
            other_target,
            other_route,
            "send",
            {"message": TransportMessage("concurrent")},
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), 2)
        result = await asyncio.wait_for(
            env.service._effect(
                "send_message",
                {"text": "current"},
                current_context,
                current_target,
                current_route,
                "send",
                {"message": TransportMessage("concurrent")},
            ),
            2,
        )
        assert result["status"] == "succeeded"
        assert not slow.done()
    finally:
        release.set()
        await slow
    assert len([action for action, _ in env.bot.calls if action == "send_group_msg"]) == 2


@pytest.mark.asyncio
async def test_concurrent_same_operation_dispatches_once(
    database: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = await social_env(database, tmp_path)
    target = SocialTarget(kind="space", id=UUID(env.space))
    route = await env.service.send_route(target, env.context)
    ready = asyncio.Event()
    calls = 0
    prepare = env.service.receipts.prepare

    async def together(**kwargs):
        nonlocal calls
        receipt = await prepare(**kwargs)
        calls += 1
        if calls == 2:
            ready.set()
        await ready.wait()
        return receipt

    monkeypatch.setattr(env.service.receipts, "prepare", together)
    results = await asyncio.gather(
        *[
            env.service._effect(
                "send_message",
                {"text": "one"},
                env.context,
                target,
                route,
                "send",
                {"message": TransportMessage("concurrent")},
            )
            for _ in range(2)
        ]
    )
    assert any(result["status"] == "succeeded" for result in results)
    assert len([action for action, _ in env.bot.calls if action == "send_group_msg"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", ["paused", "generation", "binding", "presence", "connection", "reconnect", "target"]
)
async def test_route_change_after_prepare_before_claim_fails_closed(
    database: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    env = await social_env(database, tmp_path)
    target = SocialTarget(kind="space", id=UUID(env.space))
    route = await env.service.send_route(target, env.context)
    prepare = env.service.receipts.prepare

    async def change_after_prepare(*args, **kwargs):
        prepared = await prepare(*args, **kwargs)
        if change in {"connection", "reconnect"}:
            env.registry.disconnect(env.bot)
            if change == "reconnect":
                env.registry.connect(env.bot, provider_id="snowluma", presence_id=env.presence)
        else:
            async with database.immediate_session() as session:
                if change in {"paused", "generation"}:
                    row = await session.get(SpaceActiveRouteModel, env.space)
                    assert row is not None
                    if change == "paused":
                        row.paused = True
                    else:
                        row.route_generation += 1
                elif change == "binding":
                    binding = await session.get(SpaceBindingModel, route.binding_id)
                    assert binding is not None
                    binding.status = "disabled"
                elif change == "presence":
                    presence = await session.get(PresenceModel, env.presence)
                    assert presence is not None
                    presence.enabled = False
                else:
                    space = await session.get(CanonicalSpaceModel, env.space)
                    assert space is not None
                    space.enabled = False
        return prepared

    monkeypatch.setattr(env.service.receipts, "prepare", change_after_prepare)
    if change in {"paused", "generation"}:
        result = await env.service._effect(
            "send_message",
            {"text": "one"},
            env.context,
            target,
            route,
            "send",
            {"message": TransportMessage("current entry")},
        )
        assert result["status"] == "succeeded"
        assert any(action == "send_group_msg" for action, _ in env.bot.calls)
        return
    with pytest.raises((RouteSendError, SocialError)):
        await env.service._effect(
            "send_message",
            {"text": "one"},
            env.context,
            target,
            route,
            "send",
            {"message": TransportMessage("concurrent")},
        )
    assert not any(action == "send_group_msg" for action, _ in env.bot.calls)
    receipt = await env.service.receipts.find(env.context.turn_id, env.context.call_id)
    assert receipt is not None and receipt.status.value == "prepared"
