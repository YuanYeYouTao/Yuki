"""Connection changes before and after the durable Social claim boundary."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from tests.support.social_identity_cases import social_env

from qq_ai_bot.identity.routing import RouteSendError
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.social.models import SocialTarget


def _change_connection(env, reconnect: bool) -> None:
    env.registry.disconnect(env.bot)
    if reconnect:
        env.registry.connect(env.bot, provider_id="snowluma", presence_id=env.presence)


@pytest.mark.asyncio
@pytest.mark.parametrize("reconnect", [False, True], ids=["disconnect", "reconnect"])
async def test_connection_change_after_claim_sql_rolls_back_before_dispatch(
    database: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reconnect: bool
) -> None:
    env = await social_env(database, tmp_path)
    target = SocialTarget(kind="space", id=UUID(env.space))
    route = await env.service.send_route(target, env.context)
    original_claim = env.service.receipts.claim
    changed = False

    async def claim_then_change(*args, **kwargs):
        nonlocal changed
        claimed = await original_claim(*args, **kwargs)
        assert claimed
        assert kwargs.get("session") is not None
        _change_connection(env, reconnect)
        changed = True
        return claimed

    monkeypatch.setattr(env.service.receipts, "claim", claim_then_change)
    with pytest.raises(RouteSendError):
        await env.service._effect(
            "send_message",
            {"text": "one"},
            env.context,
            target,
            route,
            "send_group_msg",
            {"group_id": 20001, "message": []},
        )
    assert changed
    assert not any(action == "send_group_msg" for action, _ in env.bot.calls)
    receipt = await env.service.receipts.find(env.context.turn_id, env.context.call_id)
    assert receipt is not None and receipt.status.value == "prepared"


@pytest.mark.asyncio
@pytest.mark.parametrize("reconnect", [False, True], ids=["disconnect", "reconnect"])
async def test_connection_change_after_claim_commit_preserves_uncertain_on_public_replay(
    database: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reconnect: bool
) -> None:
    env = await social_env(database, tmp_path)
    target = SocialTarget(kind="space", id=UUID(env.space))
    route = await env.service.send_route(target, env.context)
    original_immediate = database.immediate_session
    changed = False

    @asynccontextmanager
    async def commit_then_change():
        nonlocal changed
        async with original_immediate() as session:
            yield session
        # This runs after COMMIT, before control returns to the effect's
        # dispatch block. Preserve the claimed operation instead of retrying.
        if not changed:
            _change_connection(env, reconnect)
            changed = True

    monkeypatch.setattr(database, "immediate_session", commit_then_change)
    result = await env.service._effect(
        "send_message",
        {"text": "one"},
        env.context,
        target,
        route,
        "send_group_msg",
        {"group_id": 20001, "message": []},
    )
    assert changed
    assert result["status"] == "uncertain"
    assert not any(action == "send_group_msg" for action, _ in env.bot.calls)
    receipt = await env.service.receipts.find(env.context.turn_id, env.context.call_id)
    assert receipt is not None and receipt.status.value == "uncertain"
    assert result["operation_id"] == receipt.operation_id

    route_lookup = AsyncMock(side_effect=AssertionError("uncertain replay cannot resolve a route"))
    probe = AsyncMock(side_effect=AssertionError("uncertain replay cannot probe membership"))
    dispatch = AsyncMock(side_effect=AssertionError("uncertain replay cannot dispatch"))
    monkeypatch.setattr(env.service, "send_route", route_lookup)
    monkeypatch.setattr(env.router, "_probe", probe)
    monkeypatch.setattr(env.service, "_call", dispatch)
    replay = await env.service.execute("send_message", {"text": "one"}, env.context)
    assert replay["status"] == "uncertain"
    assert replay["operation_id"] == receipt.operation_id
    route_lookup.assert_not_awaited()
    probe.assert_not_awaited()
    dispatch.assert_not_awaited()
