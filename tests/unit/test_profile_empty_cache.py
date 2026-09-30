"""Only successful empty metadata is cached, with scope and original-owner fences."""

import asyncio
from dataclasses import replace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from tests.unit.test_user_profiles import FakeOneBot, inbound

from qq_ai_bot.adapters.onebot.profiles import OneBotUserProfileResolver
from qq_ai_bot.identity.canonical_repository import ensure_person
from qq_ai_bot.identity.db_models import IdentityBindingModel
from qq_ai_bot.identity.errors import CanonicalIdentityError
from qq_ai_bot.persistence.people_repository import UserProfileRepository
from qq_ai_bot.services.user_profiles import ProfileResolution, UserProfileService


async def test_empty_api_card_cache_survives_per_turn_resolvers_and_expires(monkeypatch):
    repository = AsyncMock()
    repository.get.return_value = None
    service = UserProfileService(repository)
    bot = FakeOneBot({"card": ""})
    now = [100.0]
    monkeypatch.setattr("qq_ai_bot.services.user_profiles.time.monotonic", lambda: now[0])
    message = inbound("hello", message_id="a", nickname="name", group_id="2001")
    for _ in range(2):
        await service.capture(message, OneBotUserProfileResolver(cast(Any, bot)))
    assert len(bot.calls) == 1
    await service.capture(
        replace(message, group_id="2002"), OneBotUserProfileResolver(cast(Any, bot))
    )
    assert len(bot.calls) == 2
    now[0] = 161
    await service.capture(message, OneBotUserProfileResolver(cast(Any, bot)))
    assert len(bot.calls) == 3
    for i in range(300):
        await service.capture(
            replace(message, group_id=str(i)), OneBotUserProfileResolver(cast(Any, bot))
        )
    assert len(service._empty_profiles) == 256


async def test_supplied_runtime_snapshot_is_reused(database):
    from tests.conftest import make_settings

    from qq_ai_bot.admin.config_service import RuntimeConfigService

    runtime = RuntimeConfigService(settings=make_settings(database.url), database=database)
    snapshot = await runtime.snapshot(user_id="1001")
    runtime.snapshot = AsyncMock(side_effect=AssertionError("duplicate snapshot"))
    service = UserProfileService(UserProfileRepository(database), runtime)
    await service.capture(inbound("hello", message_id="a", nickname="name"), runtime=snapshot)
    runtime.snapshot.assert_not_called()


async def test_late_profile_cannot_write_to_recreated_person(database):
    repository = UserProfileRepository(database)
    async with database.sessions() as session:
        original = await session.scalar(
            select(IdentityBindingModel.person_id).where(
                IdentityBindingModel.external_account_id == "1001"
            )
        )
    assert original
    entered, release = asyncio.Event(), asyncio.Event()

    class Resolver:
        async def resolve(self, message):
            entered.set()
            await release.wait()
            return ProfileResolution("old private name", "", True, False)

    pending = asyncio.create_task(
        UserProfileService(repository).capture(
            replace(inbound("hello", message_id="late"), person_id=original), Resolver()
        )
    )
    await entered.wait()
    try:
        await repository.delete_person("1001")
        async with database.immediate_session() as session:
            replacement = await ensure_person(session, "1001", display_name="new name")
        assert replacement != original
    finally:
        release.set()
    with pytest.raises(CanonicalIdentityError):
        await pending
    profile = await repository.get(user_id="1001")
    assert profile is not None and profile.nickname == "new name"
