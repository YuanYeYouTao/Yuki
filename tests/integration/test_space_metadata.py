"""Native name observations update existing subjects without opening chat work."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from nonebot.adapters.onebot.v11 import NoticeEvent
from sqlalchemy import event, select

from yuki.gateway.providers import builtin_provider_catalog
from yuki.gateway.registry import GatewayConnectionRegistry, RegistryClosed
from yuki.identity.canonical_repository import (
    create_presence,
    create_space_binding,
    find_space_binding,
)
from yuki.identity.db_models import CanonicalSpaceModel, SpaceBindingModel
from yuki.identity.space_metadata import observe_space_name
from yuki.persistence.metadata import Base
from yuki.plugins.ai_chat import metadata


async def existing_group(database, *, platform="qq", group_id="40001", account="80001"):
    async with database.immediate_session() as session:
        presence = await create_presence(
            session, external_account_id=account, platform=platform, now=datetime.now(UTC)
        )
        binding = await create_space_binding(
            session,
            external_space_id=group_id,
            name="original",
            enabled=False,
            autonomous_enabled=False,
            require_mention=True,
            platform=platform,
            now=datetime.now(UTC),
        )
    return SimpleNamespace(presence=presence.id, binding=binding.id, space=binding.space_id)


async def protected_rows(database):
    names = (
        "persons",
        "accounts",
        "account_assignments",
        "canonical_conversations",
        "ordinary_turn_admissions",
        "chat_events",
        "runtime_work",
        "model_invocations",
        "model_context_observations",
        "social_operation_receipts",
        "space_active_routes",
        "space_binding_ingest_routes",
    )
    async with database.read_session() as session:
        return {
            name: (await session.execute(select(Base.metadata.tables[name]))).all()
            for name in names
        }


def write_recorder(statements):
    def record(_connection, _cursor, statement, *_args):
        if statement.split()[0].upper() in {"INSERT", "UPDATE", "DELETE", "REPLACE"} or (
            statement.strip().upper() == "BEGIN IMMEDIATE"
        ):
            statements.append(statement)

    return record


@pytest.mark.parametrize("platform", ["qq", "telegram"])
async def test_native_space_name_preserves_original_subject_and_admission(database, platform):
    owner = await existing_group(database, platform=platform)
    before = await protected_rows(database)
    async with database.read_session() as session:
        old_binding = await session.get(SpaceBindingModel, owner.binding)
        last_seen = old_binding.last_seen_at
    assert await observe_space_name(
        database,
        presence_id=owner.presence,
        space_binding_id=owner.binding,
        name=" new\n native\x00name ",
    )
    async with database.read_session() as session:
        binding = await session.get(SpaceBindingModel, owner.binding)
        space = await session.get(CanonicalSpaceModel, owner.space)
        assert binding.space_id == owner.space and binding.platform == platform
        assert binding.display_name == space.name == "new native name"
        assert binding.last_seen_at == last_seen and binding.status == "active"
        assert not space.enabled and not space.autonomous_enabled and space.require_mention
        assert binding.revision == space.revision == 2
    assert await protected_rows(database) == before


@pytest.mark.parametrize("name", [None, "", " \n\x00 ", "original"])
async def test_missing_or_unchanged_space_name_never_acquires_a_writer(database, name):
    owner = await existing_group(database)
    statements = []
    record = write_recorder(statements)
    event.listen(database.engine.sync_engine, "before_cursor_execute", record)
    try:
        assert not await observe_space_name(
            database,
            presence_id=owner.presence,
            space_binding_id=owner.binding,
            name=name,
        )
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", record)
    assert not statements


@pytest.mark.parametrize("unknown", ["presence", "binding", "platform"])
async def test_unknown_or_cross_platform_source_does_not_create_metadata(database, unknown):
    owner = await existing_group(database)
    other = await existing_group(database, platform="telegram")
    before = await protected_rows(database)
    assert not await observe_space_name(
        database,
        presence_id=str(uuid4())
        if unknown == "presence"
        else other.presence
        if unknown == "platform"
        else owner.presence,
        space_binding_id=str(uuid4()) if unknown == "binding" else owner.binding,
        name="not observed here",
    )
    async with database.read_session() as session:
        assert (await session.get(CanonicalSpaceModel, owner.space)).name == "original"
        assert (await session.get(CanonicalSpaceModel, other.space)).name == "original"
    assert await protected_rows(database) == before


def group_notice(**changes):
    return NoticeEvent.model_validate(
        {
            "time": 1792250000,
            "self_id": 80001,
            "post_type": "notice",
            "notice_type": "notify",
            "sub_type": "group_name",
            "group_id": 40001,
            "user_id": 99112233,
            "name_new": "renamed",
            **changes,
        }
    )


async def test_qq_notice_uses_original_presence_without_model_member_probe_or_operator_person(
    database, monkeypatch
):
    owner = await existing_group(database)
    other_platform = await existing_group(database, platform="telegram")
    registry = GatewayConnectionRegistry(providers=builtin_provider_catalog())
    bot = SimpleNamespace(self_id="80001", call_api=AsyncMock(side_effect=AssertionError("no RPC")))
    registry.connect(bot, provider_id="snowluma", presence_id=owner.presence)
    before = await protected_rows(database)
    monkeypatch.setattr(
        metadata,
        "get_container",
        lambda: SimpleNamespace(database=database, gateway_registry=registry),
    )
    await metadata.handle_group_name_notice(bot, group_notice())
    async with database.read_session() as session:
        assert (await session.get(CanonicalSpaceModel, owner.space)).name == "renamed"
        assert (await session.get(CanonicalSpaceModel, other_platform.space)).name == "original"
    bot.call_api.assert_not_awaited()
    assert await protected_rows(database) == before


@pytest.mark.parametrize(
    "changes",
    [
        {"sub_type": "poke"},
        {"notice_type": "group_card"},
        {"group_id": 989898},
        {"name_new": None},
        {"self_id": 80002},
    ],
)
async def test_other_notices_or_missing_original_group_do_not_update_names(database, changes):
    owner = await existing_group(database)
    registry = GatewayConnectionRegistry(providers=builtin_provider_catalog())
    bot = SimpleNamespace(self_id="80001")
    registry.connect(bot, provider_id="snowluma", presence_id=owner.presence)
    assert not await metadata.observe_group_name_notice(
        database, registry, bot, group_notice(**changes)
    )
    async with database.read_session() as session:
        assert (await session.get(CanonicalSpaceModel, owner.space)).name == "original"


async def test_notice_cannot_borrow_another_registered_bot_presence(database):
    owner = await existing_group(database)
    registry = GatewayConnectionRegistry(providers=builtin_provider_catalog())
    original, other = SimpleNamespace(self_id="80001"), SimpleNamespace(self_id="80002")
    registry.connect(original, provider_id="snowluma", presence_id=owner.presence)
    with pytest.raises(RegistryClosed):
        await metadata.observe_group_name_notice(database, registry, other, group_notice())
    registry.disconnect(original)
    registry.connect(other, provider_id="snowluma", presence_id=owner.presence)
    assert not await metadata.observe_group_name_notice(
        database, registry, other, group_notice(self_id=80002)
    )
    async with database.read_session() as session:
        assert (await session.get(CanonicalSpaceModel, owner.space)).name == "original"


async def test_space_metadata_rechecks_new_writer_after_closed_reader(database, monkeypatch):
    owner = await existing_group(database)
    from contextlib import asynccontextmanager

    original_read = database.read_session
    changed = False
    conflicts = []

    def error(context):
        conflicts.append(getattr(context.original_exception, "sqlite_errorcode", None))

    @asynccontextmanager
    async def concurrent_write():
        nonlocal changed
        async with original_read() as reader:
            yield reader
        if not changed:
            changed = True
            async with database.immediate_session() as writer:
                space = await writer.get(CanonicalSpaceModel, owner.space)
                space.require_mention = False
                space.revision += 1

    monkeypatch.setattr(database, "read_session", concurrent_write)
    event.listen(database.engine.sync_engine, "handle_error", error)
    try:
        assert await observe_space_name(
            database,
            presence_id=owner.presence,
            space_binding_id=owner.binding,
            name="current name",
        )
    finally:
        event.remove(database.engine.sync_engine, "handle_error", error)
    assert changed and not conflicts
    async with database.read_session() as session:
        binding = await find_space_binding(session, "40001")
        space = await session.get(CanonicalSpaceModel, owner.space)
        assert binding.id == owner.binding and binding.space_id == owner.space
        assert space.name == binding.display_name == "current name"
        assert not space.require_mention and space.revision == 3
