"""C23b-2a: canonical ownership for plugin config, grants, and publication."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.conversation.canonical_db_models import CanonicalEventReceiptModel
from qq_ai_bot.identity.db_models import (
    CanonicalPersonModel,
    IdentityBindingModel,
    IdentityRuntimeStateModel,
    SpaceBindingModel,
)
from qq_ai_bot.identity.dual_write import (
    ensure_canonical_person_preconfig,
    ensure_canonical_space_preconfig,
)
from qq_ai_bot.identity.dual_write import (
    ensure_canonical_presence_preconfig as ensure_v2_presence,
)
from qq_ai_bot.identity.inventory import IDENTITY_PLATFORM
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import ChatEventModel, GroupModel, PersonModel
from qq_ai_bot.plugin_host.db_models import (
    PluginBackgroundTargetGrantModel,
    PluginBackgroundTurnJobModel,
    PluginConfigValueModel,
    PluginNotificationOutboxModel,
)
from qq_ai_bot.plugin_host.notification_repository import PluginNotificationRepository
from qq_ai_bot.plugin_host.ownership import (
    CANONICAL_OWNER_DISABLED,
    CANONICAL_OWNER_MISMATCH,
    MISSING_CANONICAL_OWNER,
    STATE_MISMATCH,
    PluginOwnershipError,
    require_v2_grant_readable,
)
from qq_ai_bot.plugin_host.repository import PluginConfigRepository, PluginInstallationRepository
from yuki_plugin_sdk.api import PLUGIN_API_VERSION, is_api_compatible
from yuki_plugin_sdk.models import NotificationTarget, PublishNotificationRequest

_NOW = datetime(2026, 8, 25, tzinfo=UTC)
_CUTOVER = "550e8400-e29b-41d4-a716-446655440099"
PLUGIN_ID = "com.example.c23b2a"


async def _install(database: Database, plugin_id: str = PLUGIN_ID) -> None:
    repository = PluginInstallationRepository(database)
    await repository.upsert_discovered(
        plugin_id=plugin_id,
        name="C23b-2a",
        version="1.0.0",
        plugin_api="2.0",
        yuki_requires=">=3.4",
        manifest_hash="a" * 64,
        entrypoint="plugin:Plugin",
        requested_permissions=("notification.publish", "notification.agent"),
    )
    await repository.approve(plugin_id)
    await repository.set_enabled(plugin_id, enabled=True)
    await repository.set_status(plugin_id, status="running")


async def _flip_v2(database: Database) -> None:
    async with database.sessions() as session, session.begin():
        row = await session.get(IdentityRuntimeStateModel, 1)
        assert row is not None
        row.state = "v2"
        row.cutover_id = _CUTOVER
        row.source_fingerprint = "cutover-fingerprint"
        row.completed_at = _NOW


async def _second_binding(database: Database, external_id: str, person_id: str) -> None:
    async with database.sessions() as session, session.begin():
        session.add(
            IdentityBindingModel(
                id=str(uuid4()),
                person_id=person_id,
                platform=IDENTITY_PLATFORM,
                external_account_id=external_id,
                display_name="",
                status="active",
                revision=1,
                created_at=_NOW,
                updated_at=_NOW,
            )
        )


async def _second_space_binding(database: Database, group_id: str, space_id: str) -> None:
    async with database.sessions() as session, session.begin():
        session.add(
            SpaceBindingModel(
                id=str(uuid4()),
                space_id=space_id,
                platform=IDENTITY_PLATFORM,
                external_space_id=group_id,
                display_name="",
                status="active",
                revision=1,
                created_at=_NOW,
                updated_at=_NOW,
            )
        )


def _assert_closed(
    exc_info: pytest.ExceptionInfo[BaseException],
    category: str,
    *forbidden: str,
) -> None:
    assert exc_info.value.category == category
    text = str(exc_info.value)
    assert "IdentityDualWriteError" not in text
    assert "unclassified" not in text
    for token in forbidden:
        assert token not in text


def _publish_request(
    target: NotificationTarget,
    *,
    event_key: str = "evt-1",
    text: str = "hello",
    ask_agent: bool = True,
) -> PublishNotificationRequest:
    return PublishNotificationRequest(
        event_key=event_key,
        event_type="test",
        external_source="test",
        target=target,
        occurred_at=_NOW,
        summary="summary",
        payload={"k": "v"},
        text=text,
        ask_agent=ask_agent,
        agent_intent="reply",
    )


async def _counts(database: Database) -> tuple[int, int, int]:
    async with database.sessions() as session:
        events = int(await session.scalar(select(func.count(ChatEventModel.id))) or 0)
        outbox = int(
            await session.scalar(select(func.count(PluginNotificationOutboxModel.id))) or 0
        )
        jobs = int(await session.scalar(select(func.count(PluginBackgroundTurnJobModel.id))) or 0)
    return events, outbox, jobs


def test_plugin_api_remains_2_0() -> None:
    assert PLUGIN_API_VERSION == "2.0"
    assert is_api_compatible("2.0")


@pytest.mark.asyncio
async def test_v1_config_grant_publish_golden_unchanged(database: Database) -> None:
    await _install(database)
    from qq_ai_bot.persistence.people_repository import GroupSettingsRepository, PeopleRepository

    await PeopleRepository(database).observe(user_id="9000", nickname="Admin")
    await GroupSettingsRepository(database).set_enabled("2001", True)
    configs = PluginConfigRepository(database)
    stored = await configs.compare_and_set(
        plugin_id=PLUGIN_ID,
        scope_type="user",
        scope_id="1001",
        key="theme",
        expected_version=0,
        value="dark",
    )
    assert stored.scope_id == "1001"
    assert stored.value == "dark"
    loaded = await configs.get(plugin_id=PLUGIN_ID, scope_type="user", scope_id="1001", key="theme")
    assert loaded is not None
    assert loaded.scope_id == "1001"
    notifications = PluginNotificationRepository(database)
    target = NotificationTarget(target_type="group", target_id="2001")
    await notifications.grant_target(
        plugin_id=PLUGIN_ID,
        target=target,
        bot_user_id="9999",
        created_by_user_id="9000",
    )
    receipt = await notifications.publish(
        plugin_id=PLUGIN_ID,
        request=_publish_request(target, event_key="v1-golden"),
    )
    assert receipt.event_created
    async with database.sessions() as session:
        event = await session.get(ChatEventModel, receipt.source_event_id)
        people = int(await session.scalar(select(func.count()).select_from(PersonModel)) or 0)
        groups = int(await session.scalar(select(func.count()).select_from(GroupModel)) or 0)
    assert event is not None
    assert event.origin == "plugin_background"
    assert event.author_kind == "system"
    assert event.sender_user_id == "9999"
    assert people >= 1
    assert groups >= 1


@pytest.mark.asyncio
async def test_v2_two_bindings_share_one_user_config(database: Database) -> None:
    await _install(database)
    await _flip_v2(database)
    async with database.sessions() as session, session.begin():
        person = await ensure_canonical_person_preconfig(session, "1001", now=_NOW)
    await _second_binding(database, "1002", person)
    configs = PluginConfigRepository(database)
    first = await configs.compare_and_set(
        plugin_id=PLUGIN_ID,
        scope_type="user",
        scope_id="1001",
        key="theme",
        expected_version=0,
        value="dark",
    )
    assert first.canonical_person_id == person
    assert first.canonical_space_id is None
    second = await configs.compare_and_set(
        plugin_id=PLUGIN_ID,
        scope_type="user",
        scope_id="1002",
        key="theme",
        expected_version=first.version,
        value="light",
    )
    assert second.id == first.id
    assert second.canonical_person_id == person
    loaded = await configs.get(plugin_id=PLUGIN_ID, scope_type="user", scope_id="1002", key="theme")
    assert loaded is not None
    assert loaded.value == "light"
    listed = await configs.list_scope(plugin_id=PLUGIN_ID, scope_type="user", scope_id="1002")
    assert [row.id for row in listed] == [first.id]
    async with database.sessions() as session:
        rows = list((await session.scalars(select(PluginConfigValueModel))).all())
        people = int(await session.scalar(select(func.count()).select_from(PersonModel)) or 0)
    assert len(rows) == 1
    assert people == 0


@pytest.mark.asyncio
async def test_v2_two_space_bindings_share_one_group_config(database: Database) -> None:
    await _install(database)
    await _flip_v2(database)
    async with database.sessions() as session, session.begin():
        space = await ensure_canonical_space_preconfig(session, "2001", now=_NOW)
    await _second_space_binding(database, "2009", space)
    configs = PluginConfigRepository(database)
    first = await configs.compare_and_set(
        plugin_id=PLUGIN_ID,
        scope_type="group",
        scope_id="2001",
        key="lang",
        expected_version=0,
        value="zh",
    )
    assert first.canonical_space_id == space
    assert first.canonical_person_id is None
    second = await configs.compare_and_set(
        plugin_id=PLUGIN_ID,
        scope_type="group",
        scope_id="2009",
        key="lang",
        expected_version=first.version,
        value="en",
    )
    assert second.id == first.id
    loaded = await configs.get(plugin_id=PLUGIN_ID, scope_type="group", scope_id="2009", key="lang")
    assert loaded is not None
    assert loaded.value == "en"
    async with database.sessions() as session:
        assert int(await session.scalar(select(func.count(PluginConfigValueModel.id))) or 0) == 1


@pytest.mark.asyncio
async def test_v2_global_config_stays_unowned(database: Database) -> None:
    await _install(database)
    await _flip_v2(database)
    configs = PluginConfigRepository(database)
    stored = await configs.compare_and_set(
        plugin_id=PLUGIN_ID,
        scope_type="global",
        scope_id="",
        key="flag",
        expected_version=0,
        value=True,
    )
    assert stored.canonical_person_id is None
    assert stored.canonical_space_id is None
    loaded = await configs.get(plugin_id=PLUGIN_ID, scope_type="global", scope_id="", key="flag")
    assert loaded is not None
    assert loaded.value is True


@pytest.mark.asyncio
async def test_v2_config_missing_disabled_wrong_kind_fail_closed(database: Database) -> None:
    await _install(database)
    await _flip_v2(database)
    configs = PluginConfigRepository(database)
    with pytest.raises(PluginOwnershipError) as missing:
        await configs.compare_and_set(
            plugin_id=PLUGIN_ID,
            scope_type="user",
            scope_id="1001",
            key="theme",
            expected_version=0,
            value="x",
        )
    _assert_closed(missing, MISSING_CANONICAL_OWNER, "1001")
    async with database.sessions() as session, session.begin():
        person = await ensure_canonical_person_preconfig(session, "1001", now=_NOW)
        presence = await ensure_v2_presence(session, "8000")
        await ensure_canonical_space_preconfig(session, "2001", now=_NOW)
    with pytest.raises(PluginOwnershipError) as presence_owner:
        await configs.compare_and_set(
            plugin_id=PLUGIN_ID,
            scope_type="user",
            scope_id="8000",
            key="theme",
            expected_version=0,
            value="x",
        )
    _assert_closed(presence_owner, CANONICAL_OWNER_MISMATCH, "8000", presence)
    stored = await configs.compare_and_set(
        plugin_id=PLUGIN_ID,
        scope_type="user",
        scope_id="1001",
        key="theme",
        expected_version=0,
        value="ok",
    )
    async with database.sessions() as session, session.begin():
        row = await session.get(CanonicalPersonModel, person)
        assert row is not None
        row.enabled = False
    with pytest.raises(PluginOwnershipError) as disabled:
        await configs.get(plugin_id=PLUGIN_ID, scope_type="user", scope_id="1001", key="theme")
    _assert_closed(disabled, CANONICAL_OWNER_DISABLED, "1001", person)
    async with database.sessions() as session:
        dual = await session.get(PluginConfigValueModel, stored.id)
        assert dual is not None
        assert dual.canonical_person_id == person
    del stored


@pytest.mark.asyncio
async def test_v2_two_bindings_share_one_grant_and_presence_change_does_not_split(
    database: Database,
) -> None:
    await _install(database)
    await _flip_v2(database)
    async with database.sessions() as session, session.begin():
        creator = await ensure_canonical_person_preconfig(session, "9000", now=_NOW)
        person = await ensure_canonical_person_preconfig(session, "1001", now=_NOW)
        presence_a = await ensure_v2_presence(session, "8000")
        presence_b = await ensure_v2_presence(session, "8001")
    await _second_binding(database, "1002", person)
    notifications = PluginNotificationRepository(database)
    first = await notifications.grant_target(
        plugin_id=PLUGIN_ID,
        target=NotificationTarget(target_type="private", target_id="1001"),
        bot_user_id="8000",
        created_by_user_id="9000",
    )
    second = await notifications.grant_target(
        plugin_id=PLUGIN_ID,
        target=NotificationTarget(target_type="private", target_id="1002"),
        bot_user_id="8001",
        created_by_user_id="9000",
    )
    assert first.target_id == second.target_id
    grants = await notifications.list_grants(PLUGIN_ID)
    assert len(grants) == 1
    creator_id = await notifications.grant_creator(
        plugin_id=PLUGIN_ID, target_type="private", target_id="1002"
    )
    assert creator_id == "9000"
    async with database.sessions() as session:
        rows = list((await session.scalars(select(PluginBackgroundTargetGrantModel))).all())
        assert len(rows) == 1
        assert rows[0].canonical_target_person_id == person
        assert rows[0].canonical_target_space_id is None
        assert rows[0].canonical_created_by_person_id == creator
        assert rows[0].canonical_presence_id == presence_b
        assert rows[0].canonical_presence_id != presence_a
        people = int(await session.scalar(select(func.count()).select_from(PersonModel)) or 0)
    assert people == 0


@pytest.mark.asyncio
async def test_v2_two_space_bindings_share_one_group_grant(database: Database) -> None:
    await _install(database)
    await _flip_v2(database)
    async with database.sessions() as session, session.begin():
        await ensure_canonical_person_preconfig(session, "9000", now=_NOW)
        space = await ensure_canonical_space_preconfig(session, "2001", now=_NOW)
        await ensure_v2_presence(session, "8000")
    await _second_space_binding(database, "2009", space)
    notifications = PluginNotificationRepository(database)
    await notifications.grant_target(
        plugin_id=PLUGIN_ID,
        target=NotificationTarget(target_type="group", target_id="2001"),
        bot_user_id="8000",
        created_by_user_id="9000",
    )
    again = await notifications.grant_target(
        plugin_id=PLUGIN_ID,
        target=NotificationTarget(target_type="group", target_id="2009"),
        bot_user_id="8000",
        created_by_user_id="9000",
    )
    assert again.target_id == "2001"
    async with database.sessions() as session:
        rows = list((await session.scalars(select(PluginBackgroundTargetGrantModel))).all())
        assert len(rows) == 1
        assert rows[0].canonical_target_space_id == space
        assert rows[0].canonical_target_person_id is None


@pytest.mark.asyncio
async def test_v2_conflicting_populated_grants_fail_closed_without_merge(
    database: Database,
) -> None:
    await _install(database)
    await _flip_v2(database)
    async with database.sessions() as session, session.begin():
        await ensure_canonical_person_preconfig(session, "9000", now=_NOW)
        person = await ensure_canonical_person_preconfig(session, "1001", now=_NOW)
        await _second_binding_in_session(session, "1002", person)
        await ensure_v2_presence(session, "8000")
        session.add(
            PluginBackgroundTargetGrantModel(
                plugin_id=PLUGIN_ID,
                target_type="private",
                target_id="1001",
                bot_user_id="8000",
                enabled=True,
                created_by_user_id="9000",
                created_at=_NOW,
                updated_at=_NOW,
                canonical_target_person_id=person,
                canonical_created_by_person_id=(
                    await session.scalar(
                        select(IdentityBindingModel.person_id).where(
                            IdentityBindingModel.external_account_id == "9000"
                        )
                    )
                ),
            )
        )
        session.add(
            PluginBackgroundTargetGrantModel(
                plugin_id=PLUGIN_ID,
                target_type="private",
                target_id="1002",
                bot_user_id="8000",
                enabled=True,
                created_by_user_id="9000",
                created_at=_NOW,
                updated_at=_NOW,
                canonical_target_person_id=person,
            )
        )
    notifications = PluginNotificationRepository(database)
    with pytest.raises(PluginOwnershipError) as conflict:
        await notifications.grant_target(
            plugin_id=PLUGIN_ID,
            target=NotificationTarget(target_type="private", target_id="1002"),
            bot_user_id="8000",
            created_by_user_id="9000",
        )
    _assert_closed(conflict, STATE_MISMATCH, "1001", "1002", person)
    async with database.sessions() as session:
        grants = int(
            await session.scalar(select(func.count(PluginBackgroundTargetGrantModel.id))) or 0
        )
        assert grants == 2


async def _second_binding_in_session(
    session: AsyncSession, external_id: str, person_id: str
) -> None:
    session.add(
        IdentityBindingModel(
            id=str(uuid4()),
            person_id=person_id,
            platform=IDENTITY_PLATFORM,
            external_account_id=external_id,
            display_name="",
            status="active",
            revision=1,
            created_at=_NOW,
            updated_at=_NOW,
        )
    )


@pytest.mark.asyncio
async def test_v2_publication_inherits_grant_and_event_canonicals(
    database: Database,
) -> None:
    await _install(database)
    await _flip_v2(database)
    async with database.sessions() as session, session.begin():
        await ensure_canonical_person_preconfig(session, "9000", now=_NOW)
        person = await ensure_canonical_person_preconfig(session, "1001", now=_NOW)
        presence = await ensure_v2_presence(session, "8001")
    notifications = PluginNotificationRepository(database)
    target = NotificationTarget(target_type="private", target_id="1001")
    await notifications.grant_target(
        plugin_id=PLUGIN_ID,
        target=target,
        bot_user_id="8001",
        created_by_user_id="9000",
    )
    receipt = await notifications.publish(
        plugin_id=PLUGIN_ID,
        request=_publish_request(target, event_key="inherit-1"),
    )
    assert receipt.event_created
    assert receipt.delivery_enqueued
    assert receipt.agent_turn_enqueued
    async with database.sessions() as session:
        event = await session.get(ChatEventModel, receipt.source_event_id)
        grant = await session.scalar(select(PluginBackgroundTargetGrantModel))
        outbox = await session.scalar(select(PluginNotificationOutboxModel))
        job = await session.scalar(select(PluginBackgroundTurnJobModel))
        people = int(await session.scalar(select(func.count()).select_from(PersonModel)) or 0)
        groups = int(await session.scalar(select(func.count()).select_from(GroupModel)) or 0)
    assert event is not None and grant is not None and outbox is not None and job is not None
    assert event.origin == "plugin_background"
    assert event.author_kind == "system"
    assert event.author_person_id is None
    assert event.author_presence_id is None
    assert event.canonical_conversation_id
    assert grant.canonical_target_person_id == person
    assert grant.canonical_presence_id == presence
    assert outbox.canonical_target_person_id == grant.canonical_target_person_id
    assert outbox.canonical_target_space_id == grant.canonical_target_space_id
    assert outbox.canonical_conversation_id == event.canonical_conversation_id
    assert outbox.canonical_presence_id == grant.canonical_presence_id
    assert job.canonical_target_person_id == grant.canonical_target_person_id
    assert job.canonical_target_space_id == grant.canonical_target_space_id
    assert job.canonical_conversation_id == event.canonical_conversation_id
    assert job.canonical_presence_id == grant.canonical_presence_id
    assert people == 0
    assert groups == 0


@pytest.mark.asyncio
async def test_v2_double_publish_same_key_same_payload_is_deduplicated(
    database: Database,
) -> None:
    await _install(database)
    await _flip_v2(database)
    async with database.sessions() as session, session.begin():
        await ensure_canonical_person_preconfig(session, "9000", now=_NOW)
        await ensure_canonical_person_preconfig(session, "1001", now=_NOW)
        await ensure_v2_presence(session, "8001")
    notifications = PluginNotificationRepository(database)
    target = NotificationTarget(target_type="private", target_id="1001")
    await notifications.grant_target(
        plugin_id=PLUGIN_ID,
        target=target,
        bot_user_id="8001",
        created_by_user_id="9000",
    )
    request = _publish_request(target, event_key="dup-1")
    first = await notifications.publish(plugin_id=PLUGIN_ID, request=request)
    second = await notifications.publish(plugin_id=PLUGIN_ID, request=request)
    assert first.event_created
    assert second.deduplicated
    assert not second.event_created
    assert first.source_event_id == second.source_event_id
    assert await _counts(database) == (1, 1, 1)
    async with database.sessions() as session:
        receipts = int(
            await session.scalar(select(func.count()).select_from(CanonicalEventReceiptModel)) or 0
        )
        events = list((await session.scalars(select(ChatEventModel))).all())
    assert receipts == 0
    assert len(events) == 1
    assert events[0].canonical_event_id
    assert events[0].source_plugin_id == PLUGIN_ID
    assert events[0].external_event_key == "dup-1"


@pytest.mark.asyncio
async def test_v2_double_publish_same_key_conflicting_payload_fails_closed(
    database: Database,
) -> None:
    await _install(database)
    await _flip_v2(database)
    async with database.sessions() as session, session.begin():
        await ensure_canonical_person_preconfig(session, "9000", now=_NOW)
        await ensure_canonical_person_preconfig(session, "1001", now=_NOW)
        await ensure_v2_presence(session, "8001")
    notifications = PluginNotificationRepository(database)
    target = NotificationTarget(target_type="private", target_id="1001")
    await notifications.grant_target(
        plugin_id=PLUGIN_ID,
        target=target,
        bot_user_id="8001",
        created_by_user_id="9000",
    )
    first = await notifications.publish(
        plugin_id=PLUGIN_ID,
        request=_publish_request(target, event_key="dup-conflict", text="hello"),
    )
    assert first.event_created
    before = await _counts(database)
    conflict = PublishNotificationRequest(
        event_key="dup-conflict",
        event_type="test",
        external_source="test",
        target=target,
        occurred_at=_NOW,
        summary="summary",
        payload={"k": "other"},
        text="hello",
        ask_agent=True,
        agent_intent="reply",
    )
    with pytest.raises(PluginOwnershipError) as closed:
        await notifications.publish(plugin_id=PLUGIN_ID, request=conflict)
    _assert_closed(closed, STATE_MISMATCH)
    assert await _counts(database) == before


@pytest.mark.asyncio
async def test_v2_rebind_cannot_resurrect_old_grant(database: Database) -> None:
    await _install(database)
    await _flip_v2(database)
    async with database.sessions() as session, session.begin():
        await ensure_canonical_person_preconfig(session, "9000", now=_NOW)
        person_a = await ensure_canonical_person_preconfig(session, "1001", now=_NOW)
        person_b = await ensure_canonical_person_preconfig(session, "1003", now=_NOW)
        await ensure_v2_presence(session, "8001")
    notifications = PluginNotificationRepository(database)
    target = NotificationTarget(target_type="private", target_id="1001")
    await notifications.grant_target(
        plugin_id=PLUGIN_ID,
        target=target,
        bot_user_id="8001",
        created_by_user_id="9000",
    )
    assert (
        await notifications.grant_creator(
            plugin_id=PLUGIN_ID, target_type="private", target_id="1001"
        )
        == "9000"
    )
    async with database.sessions() as session, session.begin():
        binding = await session.scalar(
            select(IdentityBindingModel).where(IdentityBindingModel.external_account_id == "1001")
        )
        assert binding is not None
        binding.person_id = person_b
        binding.revision += 1
    assert (
        await notifications.grant_creator(
            plugin_id=PLUGIN_ID, target_type="private", target_id="1001"
        )
        is None
    )
    async with database.sessions() as session:
        grant = await session.scalar(select(PluginBackgroundTargetGrantModel))
        assert grant is not None
        assert grant.enabled is True
        assert grant.canonical_target_person_id == person_a
        assert grant.target_id == "1001"


@pytest.mark.asyncio
async def test_v2_publish_via_second_binding_reuses_grant_and_inherits(
    database: Database,
) -> None:
    await _install(database)
    await _flip_v2(database)
    async with database.sessions() as session, session.begin():
        await ensure_canonical_person_preconfig(session, "9000", now=_NOW)
        person = await ensure_canonical_person_preconfig(session, "1001", now=_NOW)
        await ensure_v2_presence(session, "8001")
    await _second_binding(database, "1002", person)
    notifications = PluginNotificationRepository(database)
    await notifications.grant_target(
        plugin_id=PLUGIN_ID,
        target=NotificationTarget(target_type="private", target_id="1001"),
        bot_user_id="8001",
        created_by_user_id="9000",
    )
    receipt = await notifications.publish(
        plugin_id=PLUGIN_ID,
        request=_publish_request(
            NotificationTarget(target_type="private", target_id="1002"),
            event_key="second-binding",
        ),
    )
    async with database.sessions() as session:
        grant_count = int(
            await session.scalar(select(func.count(PluginBackgroundTargetGrantModel.id))) or 0
        )
        event = await session.get(ChatEventModel, receipt.source_event_id)
        outbox = await session.scalar(select(PluginNotificationOutboxModel))
        grant = await session.scalar(select(PluginBackgroundTargetGrantModel))
    assert grant_count == 1
    assert event is not None and outbox is not None and grant is not None
    assert outbox.canonical_target_person_id == person
    assert outbox.canonical_conversation_id == event.canonical_conversation_id
    assert outbox.canonical_target_person_id == grant.canonical_target_person_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mutate", "category"),
    (
        ("null_creator", MISSING_CANONICAL_OWNER),
        ("null_target", MISSING_CANONICAL_OWNER),
        ("wrong_person", CANONICAL_OWNER_MISMATCH),
        ("disabled_person", CANONICAL_OWNER_DISABLED),
    ),
)
async def test_v2_bad_grant_publish_commits_nothing(
    database: Database,
    mutate: str,
    category: str,
) -> None:
    await _install(database)
    await _flip_v2(database)
    async with database.sessions() as session, session.begin():
        creator = await ensure_canonical_person_preconfig(session, "9000", now=_NOW)
        person = await ensure_canonical_person_preconfig(session, "1001", now=_NOW)
        other = await ensure_canonical_person_preconfig(session, "1003", now=_NOW)
        await ensure_v2_presence(session, "8001")
    notifications = PluginNotificationRepository(database)
    target = NotificationTarget(target_type="private", target_id="1001")
    await notifications.grant_target(
        plugin_id=PLUGIN_ID,
        target=target,
        bot_user_id="8001",
        created_by_user_id="9000",
    )
    async with database.sessions() as session, session.begin():
        grant = await session.scalar(select(PluginBackgroundTargetGrantModel))
        assert grant is not None
        if mutate == "null_creator":
            grant.canonical_created_by_person_id = None
        elif mutate == "null_target":
            grant.canonical_target_person_id = None
        elif mutate == "wrong_person":
            grant.canonical_target_person_id = other
        else:
            row = await session.get(CanonicalPersonModel, person)
            assert row is not None
            row.enabled = False
    before = await _counts(database)
    with pytest.raises(PluginOwnershipError) as closed:
        await notifications.publish(
            plugin_id=PLUGIN_ID,
            request=_publish_request(target, event_key=f"bad-{mutate}"),
        )
    _assert_closed(closed, category, "1001", "9000", person, creator)
    assert await _counts(database) == before


@pytest.mark.asyncio
async def test_v2_double_and_wrong_kind_grants_fail_closed(database: Database) -> None:
    await _install(database)
    await _flip_v2(database)
    async with database.sessions() as session, session.begin():
        creator = await ensure_canonical_person_preconfig(session, "9000", now=_NOW)
        person = await ensure_canonical_person_preconfig(session, "1001", now=_NOW)
        space = await ensure_canonical_space_preconfig(session, "2001", now=_NOW)
        presence = await ensure_v2_presence(session, "8001")
    async with database.sessions() as session:
        dual = PluginBackgroundTargetGrantModel(
            plugin_id=PLUGIN_ID,
            target_type="private",
            target_id="1001",
            bot_user_id="8001",
            enabled=True,
            created_by_user_id="9000",
            created_at=_NOW,
            updated_at=_NOW,
            canonical_created_by_person_id=creator,
            canonical_target_person_id=person,
            canonical_target_space_id=space,
            canonical_presence_id=presence,
        )
        with pytest.raises(PluginOwnershipError) as double:
            await require_v2_grant_readable(session, dual)
        _assert_closed(double, STATE_MISMATCH, "1001", person, space)
        wrong = PluginBackgroundTargetGrantModel(
            plugin_id=PLUGIN_ID,
            target_type="private",
            target_id="8001",
            bot_user_id="8001",
            enabled=True,
            created_by_user_id="9000",
            created_at=_NOW,
            updated_at=_NOW,
            canonical_created_by_person_id=creator,
            canonical_target_person_id=presence,
            canonical_presence_id=presence,
        )
        with pytest.raises(PluginOwnershipError) as kind:
            await require_v2_grant_readable(session, wrong)
        _assert_closed(kind, CANONICAL_OWNER_MISMATCH, "8001", presence)


@pytest.mark.asyncio
async def test_v2_presence_cannot_be_grant_creator_or_person_target(
    database: Database,
) -> None:
    await _install(database)
    await _flip_v2(database)
    async with database.sessions() as session, session.begin():
        await ensure_canonical_person_preconfig(session, "9000", now=_NOW)
        await ensure_canonical_person_preconfig(session, "1001", now=_NOW)
        presence = await ensure_v2_presence(session, "8001")
    notifications = PluginNotificationRepository(database)
    with pytest.raises(PluginOwnershipError) as creator:
        await notifications.grant_target(
            plugin_id=PLUGIN_ID,
            target=NotificationTarget(target_type="private", target_id="1001"),
            bot_user_id="8001",
            created_by_user_id="8001",
        )
    _assert_closed(creator, CANONICAL_OWNER_MISMATCH, "8001", presence)
    with pytest.raises(PluginOwnershipError) as target:
        await notifications.grant_target(
            plugin_id=PLUGIN_ID,
            target=NotificationTarget(target_type="private", target_id="8001"),
            bot_user_id="8001",
            created_by_user_id="9000",
        )
    _assert_closed(target, CANONICAL_OWNER_MISMATCH, "8001", presence)
    assert await _counts(database) == (0, 0, 0)
    async with database.sessions() as session:
        assert (
            int(await session.scalar(select(func.count(PluginBackgroundTargetGrantModel.id))) or 0)
            == 0
        )
