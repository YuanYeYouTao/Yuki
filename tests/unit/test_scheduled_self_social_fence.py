"""Scheduled SELF sends retain their frozen Automation conversation generation."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select, update
from tests.conftest import make_settings
from tests.integration.test_self_automation_delivery import self_actor
from tests.support.social_identity_cases import social_env
from tests.unit.test_automation_runtime import FakeClock

from qq_ai_bot.automation.registry import build_capability_registry
from qq_ai_bot.automation.repository import AutomationRepository
from qq_ai_bot.automation.service import AutomationService
from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import AutomationModel
from qq_ai_bot.runtime.origin import TurnOrigin
from qq_ai_bot.social.models import SocialError
from qq_ai_bot.time.service import TimeContextService


async def _scheduled_self(database: Database, tmp_path: Path):
    env = await social_env(database, tmp_path)
    actor = await self_actor(database, env)
    repository = AutomationRepository(database)
    now = datetime(2026, 10, 1, tzinfo=UTC)
    service = AutomationService(
        settings=make_settings(database.url, automation_enabled=True, enabled_groups_csv="20001"),
        repository=repository,
        registry=build_capability_registry(),
        time_service=TimeContextService(database, clock=FakeClock(now)),
    )
    owner, _ = await service.create_task(
        {
            "name": "SELF frozen-scene reminder",
            "goal": "喝水",
            "trigger": {"type": "daily", "hour": 15, "minute": 0},
            "strategy": "static",
            "context": {"scene": "current_group"},
            "delivery": {"target": "current_group", "text": "现在喝水"},
        },
        actor=actor,
        conversation_key="bot:80001:group:20001",
    )
    assert owner.authority_snapshot["conversation_generation"] == 1
    run = await repository.create_run(
        owner.id, scheduled_for=owner.next_run_at, actual_started_at=now
    )
    assert run is not None
    scheduled_actor = replace(
        actor,
        origin=TurnOrigin.SCHEDULED_AUTOMATION,
        initiative_run_id=None,
        automation_run_id=run.id,
        execution_id=f"automation:{run.id}",
    )
    env.context = replace(
        env.context,
        turn_id=f"automation:{run.id}",
        origin="scheduled_automation",
        automation_run_id=run.id,
        presence_id=env.presence,
        actor=scheduled_actor,
    )
    return env, owner


@pytest.mark.asyncio
async def test_generation_reset_after_scheduled_route_preparation_blocks_self_send(
    database: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env, owner = await _scheduled_self(database, tmp_path)
    original_send_route = env.service.send_route
    route_reads = 0

    async def prepare_then_reset(*args, **kwargs):
        nonlocal route_reads
        route = await original_send_route(*args, **kwargs)
        route_reads += 1
        # Public execute first resolves its route; the second read is the
        # preclaim refresh after the durable PREPARED receipt exists.
        if route_reads == 2:
            async with database.immediate_session() as writer:
                await writer.execute(
                    update(CanonicalConversationModel)
                    .where(CanonicalConversationModel.id == env.context.conversation_id)
                    .values(generation=2)
                )
        return route

    monkeypatch.setattr(env.service, "send_route", prepare_then_reset)
    with pytest.raises(SocialError, match="self_automation_scene_changed"):
        await env.service.execute("send_message", {"text": "现在喝水"}, env.context)
    assert route_reads == 2
    assert not any(action == "send_group_msg" for action, _ in env.bot.calls)
    receipt = await env.service.receipts.find(env.context.turn_id, env.context.call_id)
    assert receipt is not None and receipt.status.value == "prepared"
    async with database.sessions() as session:
        snapshot = await session.scalar(
            select(AutomationModel.authority_snapshot_json).where(AutomationModel.id == owner.id)
        )
    assert snapshot is not None
    assert json.loads(snapshot) == owner.authority_snapshot


@pytest.mark.asyncio
async def test_scheduled_self_same_generation_sends_once_and_preserves_frozen_authority(
    database: Database, tmp_path: Path
) -> None:
    env, owner = await _scheduled_self(database, tmp_path)
    result = await env.service.execute("send_message", {"text": "现在喝水"}, env.context)
    replay = await env.service.execute("send_message", {"text": "现在喝水"}, env.context)
    assert result["status"] == replay["status"] == "succeeded"
    assert result["operation_id"] == replay["operation_id"]
    assert len([action for action, _ in env.bot.calls if action == "send_group_msg"]) == 1
    async with database.sessions() as session:
        snapshot = await session.scalar(
            select(AutomationModel.authority_snapshot_json).where(AutomationModel.id == owner.id)
        )
    assert snapshot is not None and json.loads(snapshot) == owner.authority_snapshot
