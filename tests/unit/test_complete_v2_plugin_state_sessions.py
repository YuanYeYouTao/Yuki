"""C23b-1: canonical ownership for plugin state and isolated agent sessions."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from tests.unit.test_plugin_agent_sessions import _install, _service

from qq_ai_bot.identity.db_models import (
    CanonicalPersonModel,
    CanonicalSpaceModel,
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
from qq_ai_bot.identity.write_settings import (
    IdentityWriteSettings,
    configure_identity_write_settings,
)
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import GroupModel, PersonModel
from qq_ai_bot.plugin_host.db_models import PluginAgentSessionModel, PluginStateModel
from qq_ai_bot.plugin_host.ownership import (
    CANONICAL_OWNER_DISABLED,
    CANONICAL_OWNER_MISMATCH,
    MISSING_CANONICAL_OWNER,
    STATE_MISMATCH,
    PluginOwnershipError,
    require_v2_session_readable,
    stamp_session_owners,
)
from qq_ai_bot.plugin_host.repository import PluginStateRepository
from qq_ai_bot.plugin_host.session_facade import BoundAgentSessionFacade
from qq_ai_bot.plugin_host.session_repository import PluginAgentSessionRepository
from qq_ai_bot.plugin_host.storage import BoundStorageFacade
from qq_ai_bot.services.plugin_sessions import PluginSessionNotFoundError
from yuki_plugin_sdk.api import PLUGIN_API_VERSION, is_api_compatible
from yuki_plugin_sdk.permissions import PluginPermission
from yuki_plugin_sdk.sessions import CreateAgentSessionRequest, RunAgentSessionRequest

_NOW = datetime(2026, 8, 25, tzinfo=UTC)
_CUTOVER = "550e8400-e29b-41d4-a716-446655440099"


async def _flip_v2(database: Database) -> None:
    async with database.sessions() as session, session.begin():
        row = await session.get(IdentityRuntimeStateModel, 1)
        assert row is not None
        row.state = "v2"
        row.cutover_id = _CUTOVER
        row.source_fingerprint = "cutover-fingerprint"
        row.completed_at = _NOW


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


async def _set_person_binding_status(database: Database, external_id: str, status: str) -> None:
    async with database.sessions() as session, session.begin():
        row = await session.scalar(
            select(IdentityBindingModel).where(
                IdentityBindingModel.platform == IDENTITY_PLATFORM,
                IdentityBindingModel.external_account_id == external_id,
            )
        )
        assert row is not None
        row.status = status


async def _set_space_binding_status(database: Database, group_id: str, status: str) -> None:
    async with database.sessions() as session, session.begin():
        row = await session.scalar(
            select(SpaceBindingModel).where(
                SpaceBindingModel.platform == IDENTITY_PLATFORM,
                SpaceBindingModel.external_space_id == group_id,
            )
        )
        assert row is not None
        row.status = status


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


def test_plugin_api_remains_2_0() -> None:
    assert PLUGIN_API_VERSION == "2.0"
    assert is_api_compatible("2.0")


@pytest.mark.asyncio
async def test_v1_dual_write_human_person_group_space_never_bot(
    database: Database,
) -> None:
    configure_identity_write_settings(IdentityWriteSettings(ignored_bot_users=frozenset({"7777"})))
    await _install(database, "com.example.c23b1-v1")
    async with database.sessions() as session, session.begin():
        person = await ensure_canonical_person_preconfig(session, "1001", now=_NOW)
        space = await ensure_canonical_space_preconfig(session, "2001", now=_NOW)
        presence = await ensure_v2_presence(session, "8000")
    states = PluginStateRepository(database)
    sessions = PluginAgentSessionRepository(database)
    owned = await states.compare_and_set(
        plugin_id="com.example.c23b1-v1",
        namespace="notes",
        key="sticky",
        expected_version=0,
        value={"text": "hello"},
        subject_user_id="1001",
    )
    assert owned.canonical_person_id == person
    assert owned.subject_user_id == "1001"
    bot_state = await states.compare_and_set(
        plugin_id="com.example.c23b1-v1",
        namespace="notes",
        key="bot",
        expected_version=0,
        value={"text": "no"},
        subject_user_id="8000",
    )
    assert bot_state.canonical_person_id is None
    ignored = await states.compare_and_set(
        plugin_id="com.example.c23b1-v1",
        namespace="notes",
        key="ext",
        expected_version=0,
        value={"text": "no"},
        subject_user_id="7777",
    )
    assert ignored.canonical_person_id is None
    async with database.sessions() as session:
        people = int(await session.scalar(select(func.count()).select_from(PersonModel)) or 0)
        groups = int(await session.scalar(select(func.count()).select_from(GroupModel)) or 0)
    assert people == 0
    assert groups == 0
    private = await sessions.create(
        plugin_id="com.example.c23b1-v1",
        owner_user_id="1001",
        scope_type="user",
        scope_id="1001",
    )
    assert private.canonical_owner_person_id == person
    assert private.canonical_space_id is None
    group = await sessions.create(
        plugin_id="com.example.c23b1-v1",
        owner_user_id="1001",
        scope_type="group",
        scope_id="2001",
    )
    assert group.canonical_space_id == space
    assert group.canonical_owner_person_id == person
    plugin = await sessions.create(
        plugin_id="com.example.c23b1-v1",
        owner_user_id=None,
        scope_type="plugin",
        scope_id="",
    )
    assert plugin.canonical_owner_person_id is None
    assert plugin.canonical_space_id is None
    async with database.sessions() as session, session.begin():
        row = PluginAgentSessionModel(
            session_id=str(uuid4()),
            plugin_id="com.example.c23b1-v1",
            owner_user_id="8000",
            scope_type="user",
            scope_id="8000",
            name="",
            model="",
            instructions="presence-owner",
            persistence="durable",
            context_profile="none",
            allowed_capabilities_json="[]",
            status="active",
            next_sequence=1,
            turn_count=0,
            created_at=_NOW,
            updated_at=_NOW,
            last_active_at=_NOW,
        )
        session.add(row)
        await stamp_session_owners(session, row, complete_v2=False)
        assert row.canonical_owner_person_id is None
    del presence


@pytest.mark.asyncio
async def test_v2_two_bindings_share_state_and_session_lineage(database: Database) -> None:
    await _install(database, "com.example.c23b1-share")
    await _flip_v2(database)
    async with database.sessions() as session, session.begin():
        person = await ensure_canonical_person_preconfig(session, "1001", now=_NOW)
    await _second_binding(database, "1002", person)
    states = PluginStateRepository(database)
    first = await states.compare_and_set(
        plugin_id="com.example.c23b1-share",
        namespace="notes",
        key="sticky",
        expected_version=0,
        value={"text": "from-1001"},
        subject_user_id="1001",
    )
    assert first.canonical_person_id == person
    second = await states.compare_and_set(
        plugin_id="com.example.c23b1-share",
        namespace="notes",
        key="sticky",
        expected_version=first.version,
        value={"text": "from-1002"},
        subject_user_id="1002",
    )
    assert second.canonical_person_id == person
    loaded = await states.get(plugin_id="com.example.c23b1-share", namespace="notes", key="sticky")
    assert loaded is not None
    assert loaded.value == {"text": "from-1002"}
    assert loaded.canonical_person_id == person
    sessions = PluginAgentSessionRepository(database)
    created = await sessions.create(
        plugin_id="com.example.c23b1-share",
        owner_user_id="1001",
        scope_type="user",
        scope_id="1001",
    )
    listed = await sessions.list_scope(
        plugin_id="com.example.c23b1-share",
        scope_type="user",
        scope_id="1002",
    )
    assert [row.session_id for row in listed] == [created.session_id]
    authorized = await sessions.get_for_actor(
        plugin_id="com.example.c23b1-share",
        session_id=created.session_id,
        actor_user_id="1002",
        current_group_id=None,
    )
    assert authorized is not None
    assert authorized.canonical_owner_person_id == person
    message = await sessions.append_message(
        plugin_id="com.example.c23b1-share",
        session_id=created.session_id,
        role="user",
        content="same person",
        sender_user_id="1002",
    )
    assert message.canonical_sender_person_id == person


@pytest.mark.asyncio
async def test_v2_presence_change_does_not_split_space_lineage(database: Database) -> None:
    await _install(database, "com.example.c23b1-space")
    await _flip_v2(database)
    async with database.sessions() as session, session.begin():
        person = await ensure_canonical_person_preconfig(session, "1001", now=_NOW)
        space = await ensure_canonical_space_preconfig(session, "2001", now=_NOW)
        await ensure_v2_presence(session, "8000")
        await ensure_v2_presence(session, "8001")
    sessions = PluginAgentSessionRepository(database)
    created = await sessions.create(
        plugin_id="com.example.c23b1-space",
        owner_user_id="1001",
        scope_type="group",
        scope_id="2001",
    )
    assert created.canonical_space_id == space
    assert created.canonical_owner_person_id == person
    listed = await sessions.list_scope(
        plugin_id="com.example.c23b1-space",
        scope_type="group",
        scope_id="2001",
    )
    assert [row.session_id for row in listed] == [created.session_id]
    assert listed[0].canonical_space_id == space


@pytest.mark.asyncio
async def test_v2_missing_disabled_wrong_kind_and_dual_fail_closed(
    database: Database,
) -> None:
    await _install(database, "com.example.c23b1-fail")
    await _flip_v2(database)
    states = PluginStateRepository(database)
    sessions = PluginAgentSessionRepository(database)
    with pytest.raises(PluginOwnershipError) as missing_state:
        await states.compare_and_set(
            plugin_id="com.example.c23b1-fail",
            namespace="notes",
            key="owned",
            expected_version=0,
            value={"text": "x"},
            subject_user_id="1001",
        )
    assert missing_state.value.category == MISSING_CANONICAL_OWNER
    assert "1001" not in str(missing_state.value)
    with pytest.raises(PluginOwnershipError) as missing_session:
        await sessions.create(
            plugin_id="com.example.c23b1-fail",
            owner_user_id="1001",
            scope_type="user",
            scope_id="1001",
        )
    assert missing_session.value.category == MISSING_CANONICAL_OWNER
    assert "1001" not in str(missing_session.value)
    async with database.sessions() as session, session.begin():
        person = await ensure_canonical_person_preconfig(session, "1001", now=_NOW)
        presence = await ensure_v2_presence(session, "8000")
        space = await ensure_canonical_space_preconfig(session, "2001", now=_NOW)
    with pytest.raises(PluginOwnershipError) as presence_owner:
        await sessions.create(
            plugin_id="com.example.c23b1-fail",
            owner_user_id="8000",
            scope_type="user",
            scope_id="8000",
        )
    assert presence_owner.value.category == CANONICAL_OWNER_MISMATCH
    created = await sessions.create(
        plugin_id="com.example.c23b1-fail",
        owner_user_id="1001",
        scope_type="user",
        scope_id="1001",
    )
    async with database.sessions() as session, session.begin():
        row = await session.get(CanonicalPersonModel, person)
        assert row is not None
        row.enabled = False
    with pytest.raises(PluginOwnershipError) as disabled:
        await sessions.get(plugin_id="com.example.c23b1-fail", session_id=created.session_id)
    assert disabled.value.category == CANONICAL_OWNER_DISABLED
    async with database.sessions() as session:
        dual = PluginAgentSessionModel(
            session_id=str(uuid4()),
            plugin_id="com.example.c23b1-fail",
            owner_user_id="1001",
            scope_type="user",
            scope_id="1001",
            name="",
            model="",
            instructions="dual",
            persistence="durable",
            context_profile="none",
            allowed_capabilities_json="[]",
            status="active",
            next_sequence=1,
            turn_count=0,
            created_at=_NOW,
            updated_at=_NOW,
            last_active_at=_NOW,
            canonical_owner_person_id=person,
            canonical_space_id=space,
        )
        with pytest.raises(PluginOwnershipError) as dual_exc:
            await require_v2_session_readable(session, dual)
        assert dual_exc.value.category == STATE_MISMATCH
    missing_id = str(uuid4())
    async with database.sessions() as session:
        ghost = PluginAgentSessionModel(
            session_id=str(uuid4()),
            plugin_id="com.example.c23b1-fail",
            owner_user_id="1001",
            scope_type="user",
            scope_id="1001",
            name="",
            model="",
            instructions="ghost",
            persistence="durable",
            context_profile="none",
            allowed_capabilities_json="[]",
            status="active",
            next_sequence=1,
            turn_count=0,
            created_at=_NOW,
            updated_at=_NOW,
            last_active_at=_NOW,
            canonical_owner_person_id=missing_id,
        )
        with pytest.raises(PluginOwnershipError) as gone:
            await require_v2_session_readable(session, ghost)
        assert gone.value.category == MISSING_CANONICAL_OWNER
    wrong = PluginAgentSessionModel(
        session_id=str(uuid4()),
        plugin_id="com.example.c23b1-fail",
        owner_user_id="8000",
        scope_type="user",
        scope_id="8000",
        name="",
        model="",
        instructions="wrong",
        persistence="durable",
        context_profile="none",
        allowed_capabilities_json="[]",
        status="active",
        next_sequence=1,
        turn_count=0,
        created_at=_NOW,
        updated_at=_NOW,
        last_active_at=_NOW,
        canonical_owner_person_id=presence,
    )
    async with database.sessions() as session:
        with pytest.raises(PluginOwnershipError) as kind:
            await require_v2_session_readable(session, wrong)
        assert kind.value.category == CANONICAL_OWNER_MISMATCH


@pytest.mark.asyncio
async def test_v2_message_inherits_parent_and_rejects_spoof(database: Database) -> None:
    await _install(database, "com.example.c23b1-msg")
    await _flip_v2(database)
    async with database.sessions() as session, session.begin():
        owner = await ensure_canonical_person_preconfig(session, "1001", now=_NOW)
        other = await ensure_canonical_person_preconfig(session, "1003", now=_NOW)
    sessions = PluginAgentSessionRepository(database)
    created = await sessions.create(
        plugin_id="com.example.c23b1-msg",
        owner_user_id="1001",
        scope_type="user",
        scope_id="1001",
    )
    with pytest.raises(PluginOwnershipError) as spoof:
        await sessions.append_message(
            plugin_id="com.example.c23b1-msg",
            session_id=created.session_id,
            role="user",
            content="stolen",
            sender_user_id="1003",
        )
    assert spoof.value.category == CANONICAL_OWNER_MISMATCH
    user = await sessions.append_message(
        plugin_id="com.example.c23b1-msg",
        session_id=created.session_id,
        role="user",
        content="mine",
        sender_user_id="1001",
    )
    assert user.canonical_sender_person_id == owner
    tool = await sessions.append_message(
        plugin_id="com.example.c23b1-msg",
        session_id=created.session_id,
        role="tool",
        content="{}",
    )
    assert tool.canonical_sender_person_id is None
    assistant = await sessions.append_message(
        plugin_id="com.example.c23b1-msg",
        session_id=created.session_id,
        role="assistant",
        content="ok",
    )
    assert assistant.canonical_sender_person_id is None
    del other


@pytest.mark.asyncio
async def test_v2_run_fails_closed_before_agent_execution(database: Database) -> None:
    await _install(database, "com.example.c23b1-run")
    await _flip_v2(database)
    async with database.sessions() as session, session.begin():
        person = await ensure_canonical_person_preconfig(session, "1001", now=_NOW)
    provider = FakeLLMProvider()
    service, _, _ = await _service(database, provider)
    facade = BoundAgentSessionFacade(
        service=service,
        plugin_id="com.example.c23b1-run",
        actor_user_id="1001",
        current_group_id=None,
        approved_permissions=(PluginPermission.AGENT_SESSION,),
    )
    created = await facade.create(CreateAgentSessionRequest(name="v2", instructions="keep going"))
    async with database.sessions() as session, session.begin():
        row = await session.get(CanonicalPersonModel, person)
        assert row is not None
        row.enabled = False
    with pytest.raises(PluginOwnershipError) as closed:
        await facade.run(
            RunAgentSessionRequest(session_id=created.session_id, user_input="should not run")
        )
    assert closed.value.category == CANONICAL_OWNER_DISABLED
    assert provider.requests == []


@pytest.mark.asyncio
async def test_v2_other_person_cannot_use_session(database: Database) -> None:
    await _install(database, "com.example.c23b1-other")
    await _flip_v2(database)
    async with database.sessions() as session, session.begin():
        await ensure_canonical_person_preconfig(session, "1001", now=_NOW)
        await ensure_canonical_person_preconfig(session, "1003", now=_NOW)
    service, _, _ = await _service(database, FakeLLMProvider())
    owner = BoundAgentSessionFacade(
        service=service,
        plugin_id="com.example.c23b1-other",
        actor_user_id="1001",
        current_group_id=None,
        approved_permissions=(PluginPermission.AGENT_SESSION,),
    )
    created = await owner.create(CreateAgentSessionRequest(name="private", instructions="only me"))
    other = BoundAgentSessionFacade(
        service=service,
        plugin_id="com.example.c23b1-other",
        actor_user_id="1003",
        current_group_id=None,
        approved_permissions=(PluginPermission.AGENT_SESSION,),
    )
    with pytest.raises(PluginSessionNotFoundError):
        await other.run(RunAgentSessionRequest(session_id=created.session_id, user_input="no"))


@pytest.mark.asyncio
async def test_v2_subject_owned_state_without_person_is_unreadable(
    database: Database,
) -> None:
    await _install(database, "com.example.c23b1-orphan")
    states = PluginStateRepository(database)
    await states.compare_and_set(
        plugin_id="com.example.c23b1-orphan",
        namespace="notes",
        key="orphan",
        expected_version=0,
        value={"text": "legacy"},
        subject_user_id="1001",
    )
    await _flip_v2(database)
    with pytest.raises(PluginOwnershipError) as missing:
        await states.get(
            plugin_id="com.example.c23b1-orphan",
            namespace="notes",
            key="orphan",
        )
    assert missing.value.category == MISSING_CANONICAL_OWNER
    async with database.sessions() as session:
        row = await session.scalar(select(PluginStateModel))
        assert row is not None
        assert row.subject_user_id == "1001"
        assert row.canonical_person_id is None


@pytest.mark.asyncio
async def test_v2_disabled_person_write_and_create_are_canonical_owner_disabled(
    database: Database,
) -> None:
    await _install(database, "com.example.c23b1-disabled-write")
    await _flip_v2(database)
    async with database.sessions() as session, session.begin():
        person = await ensure_canonical_person_preconfig(session, "1001", now=_NOW)
        row = await session.get(CanonicalPersonModel, person)
        assert row is not None
        row.enabled = False
    states = PluginStateRepository(database)
    sessions = PluginAgentSessionRepository(database)
    with pytest.raises(PluginOwnershipError) as state_write:
        await states.compare_and_set(
            plugin_id="com.example.c23b1-disabled-write",
            namespace="notes",
            key="owned",
            expected_version=0,
            value={"text": "x"},
            subject_user_id="1001",
        )
    _assert_closed(state_write, CANONICAL_OWNER_DISABLED, "1001", person)
    with pytest.raises(PluginOwnershipError) as session_write:
        await sessions.create(
            plugin_id="com.example.c23b1-disabled-write",
            owner_user_id="1001",
            scope_type="user",
            scope_id="1001",
        )
    _assert_closed(session_write, CANONICAL_OWNER_DISABLED, "1001", person)


@pytest.mark.asyncio
async def test_v2_inactive_person_and_space_bindings_are_canonical_owner_disabled(
    database: Database,
) -> None:
    await _install(database, "com.example.c23b1-inactive")
    await _flip_v2(database)
    async with database.sessions() as session, session.begin():
        person = await ensure_canonical_person_preconfig(session, "1001", now=_NOW)
        space = await ensure_canonical_space_preconfig(session, "2001", now=_NOW)
    await _set_person_binding_status(database, "1001", "disabled")
    states = PluginStateRepository(database)
    sessions = PluginAgentSessionRepository(database)
    with pytest.raises(PluginOwnershipError) as person_state:
        await states.compare_and_set(
            plugin_id="com.example.c23b1-inactive",
            namespace="notes",
            key="owned",
            expected_version=0,
            value={"text": "x"},
            subject_user_id="1001",
        )
    _assert_closed(person_state, CANONICAL_OWNER_DISABLED, "1001", person)
    with pytest.raises(PluginOwnershipError) as person_session:
        await sessions.create(
            plugin_id="com.example.c23b1-inactive",
            owner_user_id="1001",
            scope_type="user",
            scope_id="1001",
        )
    _assert_closed(person_session, CANONICAL_OWNER_DISABLED, "1001", person)
    await _set_person_binding_status(database, "1001", "active")
    await _set_space_binding_status(database, "2001", "disabled")
    with pytest.raises(PluginOwnershipError) as space_session:
        await sessions.create(
            plugin_id="com.example.c23b1-inactive",
            owner_user_id="1001",
            scope_type="group",
            scope_id="2001",
        )
    _assert_closed(space_session, CANONICAL_OWNER_DISABLED, "2001", space)
    async with database.sessions() as session, session.begin():
        person_row = await session.get(CanonicalPersonModel, person)
        assert person_row is not None
        person_row.enabled = True
        space_row = await session.get(CanonicalSpaceModel, space)
        assert space_row is not None
        space_row.enabled = False
    await _set_person_binding_status(database, "1001", "active")
    await _set_space_binding_status(database, "2001", "active")
    with pytest.raises(PluginOwnershipError) as space_disabled:
        await sessions.create(
            plugin_id="com.example.c23b1-inactive",
            owner_user_id="1001",
            scope_type="group",
            scope_id="2001",
        )
    _assert_closed(space_disabled, CANONICAL_OWNER_DISABLED, "2001", space)


@pytest.mark.asyncio
async def test_v2_group_message_inherits_owner_and_rejects_outside_person(
    database: Database,
) -> None:
    await _install(database, "com.example.c23b1-group-spoof")
    await _flip_v2(database)
    async with database.sessions() as session, session.begin():
        owner = await ensure_canonical_person_preconfig(session, "1001", now=_NOW)
        outsider = await ensure_canonical_person_preconfig(session, "1003", now=_NOW)
        await ensure_canonical_space_preconfig(session, "2001", now=_NOW)
    sessions = PluginAgentSessionRepository(database)
    created = await sessions.create(
        plugin_id="com.example.c23b1-group-spoof",
        owner_user_id="1001",
        scope_type="group",
        scope_id="2001",
    )
    with pytest.raises(PluginOwnershipError) as spoof:
        await sessions.append_message(
            plugin_id="com.example.c23b1-group-spoof",
            session_id=created.session_id,
            role="user",
            content="stolen",
            sender_user_id="1003",
        )
    _assert_closed(spoof, CANONICAL_OWNER_MISMATCH, "1003", outsider)
    user = await sessions.append_message(
        plugin_id="com.example.c23b1-group-spoof",
        session_id=created.session_id,
        role="user",
        content="mine",
        sender_user_id="1001",
    )
    assert user.canonical_sender_person_id == owner


@pytest.mark.asyncio
async def test_v2_group_owner_shadow_missing_fails_closed_before_agent(
    database: Database,
) -> None:
    await _install(database, "com.example.c23b1-group-shadow")
    await _flip_v2(database)
    async with database.sessions() as session, session.begin():
        await ensure_canonical_person_preconfig(session, "1001", now=_NOW)
        await ensure_canonical_space_preconfig(session, "2001", now=_NOW)
    provider = FakeLLMProvider()
    service, sessions, _ = await _service(database, provider)
    facade = BoundAgentSessionFacade(
        service=service,
        plugin_id="com.example.c23b1-group-shadow",
        actor_user_id="1001",
        current_group_id="2001",
        approved_permissions=(PluginPermission.AGENT_SESSION,),
    )
    created = await facade.create(
        CreateAgentSessionRequest(name="group", instructions="keep going")
    )
    async with database.sessions() as session, session.begin():
        row = await session.get(PluginAgentSessionModel, str(created.session_id))
        assert row is not None
        assert row.owner_user_id == "1001"
        row.canonical_owner_person_id = None
    with pytest.raises(PluginOwnershipError) as loaded:
        await sessions.get(
            plugin_id="com.example.c23b1-group-shadow",
            session_id=str(created.session_id),
        )
    _assert_closed(loaded, MISSING_CANONICAL_OWNER, "1001")
    with pytest.raises(PluginOwnershipError) as listed:
        await sessions.list_scope(
            plugin_id="com.example.c23b1-group-shadow",
            scope_type="group",
            scope_id="2001",
        )
    _assert_closed(listed, MISSING_CANONICAL_OWNER, "1001")
    with pytest.raises(PluginOwnershipError) as appended:
        await sessions.append_message(
            plugin_id="com.example.c23b1-group-shadow",
            session_id=str(created.session_id),
            role="user",
            content="no",
            sender_user_id="1001",
        )
    _assert_closed(appended, MISSING_CANONICAL_OWNER, "1001")
    with pytest.raises(PluginOwnershipError) as closed:
        await facade.run(
            RunAgentSessionRequest(session_id=created.session_id, user_input="should not run")
        )
    _assert_closed(closed, MISSING_CANONICAL_OWNER, "1001")
    assert provider.requests == []


@pytest.mark.asyncio
async def test_v2_unbound_actor_get_for_actor_is_not_found(database: Database) -> None:
    await _install(database, "com.example.c23b1-unbound")
    await _flip_v2(database)
    async with database.sessions() as session, session.begin():
        person = await ensure_canonical_person_preconfig(session, "1001", now=_NOW)
    sessions = PluginAgentSessionRepository(database)
    created = await sessions.create(
        plugin_id="com.example.c23b1-unbound",
        owner_user_id="1001",
        scope_type="user",
        scope_id="1001",
    )
    missing = await sessions.get_for_actor(
        plugin_id="com.example.c23b1-unbound",
        session_id=created.session_id,
        actor_user_id="9999",
        current_group_id=None,
    )
    assert missing is None
    async with database.sessions() as session, session.begin():
        row = await session.get(CanonicalPersonModel, person)
        assert row is not None
        row.enabled = False
    unbound_disabled = await sessions.get_for_actor(
        plugin_id="com.example.c23b1-unbound",
        session_id=created.session_id,
        actor_user_id="9999",
        current_group_id=None,
    )
    assert unbound_disabled is None
    with pytest.raises(PluginOwnershipError) as owner_disabled:
        await sessions.get_for_actor(
            plugin_id="com.example.c23b1-unbound",
            session_id=created.session_id,
            actor_user_id="1001",
            current_group_id=None,
        )
    _assert_closed(owner_disabled, CANONICAL_OWNER_DISABLED, "1001", "9999")
    async with database.sessions() as session, session.begin():
        row = await session.get(CanonicalPersonModel, person)
        assert row is not None
        row.enabled = True
    await _set_person_binding_status(database, "1001", "disabled")
    with pytest.raises(PluginOwnershipError) as inactive_owner:
        await sessions.get_for_actor(
            plugin_id="com.example.c23b1-unbound",
            session_id=created.session_id,
            actor_user_id="1001",
            current_group_id=None,
        )
    _assert_closed(inactive_owner, CANONICAL_OWNER_DISABLED, "1001")


@pytest.mark.asyncio
async def test_v2_bound_storage_is_plugin_global_not_person_isolated(
    database: Database,
) -> None:
    await _install(database, "com.example.c23b1-storage")
    await _flip_v2(database)
    async with database.sessions() as session, session.begin():
        person = await ensure_canonical_person_preconfig(session, "1001", now=_NOW)
    await _second_binding(database, "1002", person)
    states = PluginStateRepository(database)
    storage = BoundStorageFacade(
        repository=states,
        plugin_id="com.example.c23b1-storage",
        approved_permissions=(PluginPermission.STORAGE_PRIVATE,),
    )
    await storage.set("notes", "sticky", {"text": "global"})
    loaded = await states.get(
        plugin_id="com.example.c23b1-storage", namespace="notes", key="sticky"
    )
    assert loaded is not None
    assert loaded.subject_user_id is None
    assert loaded.canonical_person_id is None
    assert loaded.value == {"text": "global"}
    first = await states.compare_and_set(
        plugin_id="com.example.c23b1-storage",
        namespace="notes",
        key="owned",
        expected_version=0,
        value={"text": "from-1001"},
        subject_user_id="1001",
    )
    assert first.canonical_person_id == person
    second = await states.compare_and_set(
        plugin_id="com.example.c23b1-storage",
        namespace="notes",
        key="owned",
        expected_version=first.version,
        value={"text": "from-1002"},
        subject_user_id="1002",
    )
    assert second.canonical_person_id == person
    assert second.value == {"text": "from-1002"}


@pytest.mark.asyncio
async def test_v2_global_state_shadow_corruption_fail_closes_list(
    database: Database,
) -> None:
    await _install(database, "com.example.c23b1-state-corrupt")
    await _flip_v2(database)
    async with database.sessions() as session, session.begin():
        person = await ensure_canonical_person_preconfig(session, "1001", now=_NOW)
    states = PluginStateRepository(database)
    healthy = await states.compare_and_set(
        plugin_id="com.example.c23b1-state-corrupt",
        namespace="notes",
        key="ok",
        expected_version=0,
        value={"text": "ok"},
    )
    assert healthy.subject_user_id is None
    assert healthy.canonical_person_id is None
    async with database.sessions() as session, session.begin():
        session.add(
            PluginStateModel(
                plugin_id="com.example.c23b1-state-corrupt",
                namespace="notes",
                key="spoof",
                value_json='{"text":"bad"}',
                version=1,
                subject_user_id=None,
                canonical_person_id=person,
                updated_at=_NOW,
            )
        )
    with pytest.raises(PluginOwnershipError) as spoofed:
        await states.get(
            plugin_id="com.example.c23b1-state-corrupt",
            namespace="notes",
            key="spoof",
        )
    _assert_closed(spoofed, STATE_MISMATCH, "1001", person)
    still_ok = await states.get(
        plugin_id="com.example.c23b1-state-corrupt",
        namespace="notes",
        key="ok",
    )
    assert still_ok is not None
    assert still_ok.value == {"text": "ok"}
    with pytest.raises(PluginOwnershipError) as listed:
        await states.list_namespace(
            plugin_id="com.example.c23b1-state-corrupt",
            namespace="notes",
        )
    _assert_closed(listed, STATE_MISMATCH, "1001", person)
