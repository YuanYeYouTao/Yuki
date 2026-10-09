"""Optional group refreshes never add a writer to every ordinary message."""

import asyncio
from datetime import UTC, datetime

import pytest
from sqlalchemy import event, select
from sqlalchemy.exc import OperationalError
from tests.conftest import build_harness, make_settings
from tests.support.user_profiles_helpers import inbound

from qq_ai_bot.identity.db_models import CanonicalSpaceModel, SpaceBindingModel
from qq_ai_bot.identity.errors import CanonicalIdentityError
from qq_ai_bot.persistence.people_repository import GroupSettingsRepository
from qq_ai_bot.services.policies import EffectiveGroupPolicy


class GroupResolver:
    def __init__(self, name):
        self.name = name
        self.calls = 0

    async def resolve_group_name(self, group_id):
        self.calls += 1
        return self.name


@pytest.mark.parametrize("resolver", [None, GroupResolver("")])
async def test_no_group_name_refresh_does_not_wait_for_writer(database, resolver):
    harness = build_harness(database, make_settings(database.url))
    message = inbound("ordinary", message_id="ordinary", group_id="2001")
    sql = []

    def record(_conn, _cursor, statement, *_args):
        sql.append(statement)

    async with database.immediate_session():
        event.listen(database.engine.sync_engine, "before_cursor_execute", record)
        try:
            await asyncio.wait_for(
                harness.processor._observe_group_metadata(
                    message, EffectiveGroupPolicy(enabled=True), resolver
                ),
                timeout=0.5,
            )
        finally:
            event.remove(database.engine.sync_engine, "before_cursor_execute", record)
    assert not sql


async def test_cached_refresh_keeps_name_without_separate_observation_writer(database):
    harness = build_harness(database, make_settings(database.url))
    resolver = GroupResolver("current name")
    message = inbound("ordinary", message_id="ordinary", group_id="2001")
    policy = EffectiveGroupPolicy(enabled=True)
    await harness.processor._observe_group_metadata(message, policy, resolver)
    async with database.immediate_session():
        await asyncio.wait_for(
            harness.processor._observe_group_metadata(message, policy, resolver), timeout=0.5
        )
    assert resolver.calls == 1
    assert (await harness.groups.get("2001")).name == "current name"
    # Actual person observation retains the group's last-seen fact.
    before = datetime.now(UTC).replace(tzinfo=None)
    await harness.profiles.observe(user_id="1001", nickname="caller", group_id="2001")
    async with database.sessions() as reader:
        row = await reader.scalar(
            select(SpaceBindingModel).where(SpaceBindingModel.external_space_id == "2001")
        )
        assert row.last_seen_at >= before


async def test_optional_group_refresh_busy_does_not_abort_chat_entry(database, monkeypatch):
    harness = build_harness(database, make_settings(database.url))

    async def busy(*_args, **_kwargs):
        raise OperationalError("UPDATE space_bindings", {}, RuntimeError("database is locked"))

    monkeypatch.setattr(harness.groups, "observe", busy)
    await harness.processor._observe_group_metadata(
        inbound("ordinary", message_id="ordinary", group_id="2001"),
        EffectiveGroupPolicy(enabled=True),
        GroupResolver("new name"),
    )
    assert (await harness.groups.get("2001")).name == "test-2001"


async def test_group_refresh_acquires_writer_before_reading_current_owner(database):
    groups = GroupSettingsRepository(database)
    statements = []

    def record(_conn, _cursor, statement, *_args):
        statements.append(statement)

    event.listen(database.engine.sync_engine, "before_cursor_execute", record)
    try:
        await groups.observe("2001", name="renamed")
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", record)
    assert statements[0] == "BEGIN IMMEDIATE"
    async with database.sessions() as reader:
        space = await reader.scalar(
            select(CanonicalSpaceModel)
            .join(SpaceBindingModel, SpaceBindingModel.space_id == CanonicalSpaceModel.id)
            .where(SpaceBindingModel.external_space_id == "2001")
        )
        assert space.name == "renamed"
        assert space.enabled and space.require_mention


async def test_optional_metadata_does_not_swallow_identity_rejection(database, monkeypatch):
    harness = build_harness(database, make_settings(database.url))

    async def rejected(*_args, **_kwargs):
        raise CanonicalIdentityError("canonical_owner_disabled")

    monkeypatch.setattr(harness.groups, "observe", rejected)
    with pytest.raises(CanonicalIdentityError) as failure:
        await harness.processor._observe_group_metadata(
            inbound("ordinary", message_id="ordinary", group_id="2001"),
            EffectiveGroupPolicy(enabled=True),
            GroupResolver("new name"),
        )
    assert failure.value.category == "canonical_owner_disabled"
