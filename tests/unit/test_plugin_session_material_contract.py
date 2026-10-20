"""Session material and known output are distinct from permission to start a run."""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from tests.conftest import make_settings
from tests.support.canonical_identity import ensure_person, ensure_space
from tests.support.model_executor import InjectedModelExecutor

from yuki.admin.config_service import RuntimeConfigService
from yuki.identity.db_models import CanonicalSpaceModel
from yuki.llm.fake import FakeLLMProvider
from yuki.plugin_host.db_models import PluginAgentMessageModel, PluginAgentSessionModel
from yuki.plugin_host.ownership import PluginOwnershipError
from yuki.plugin_host.repository import PluginInstallationRepository
from yuki.plugin_host.session_facade import BoundAgentSessionFacade
from yuki.plugin_host.session_repository import (
    PluginAgentSessionRepository,
    PluginSessionUnavailableError,
)
from yuki.services.concurrency import ConcurrencyManager
from yuki.services.plugin_sessions import PluginAgentSessionService, PluginSessionNotFoundError
from yuki_plugin_sdk.api import PLUGIN_API_VERSION
from yuki_plugin_sdk.sessions import (
    CreateAgentSessionRequest,
    RunAgentSessionRequest,
    SessionStatus,
)
from yuki_plugin_sdk.testing import FakeAgentSessionFacade


@pytest.fixture
async def plugin_session(database):
    plugin = "test.session-material"
    async with database.immediate_session() as session:
        space = await ensure_space(session, "2001")
        other_space = await ensure_space(session, "2002")
        person = await ensure_person(session, "1001")
    installations = PluginInstallationRepository(database)
    await installations.upsert_discovered(
        plugin_id=plugin,
        name="Session material",
        version="1",
        plugin_api=PLUGIN_API_VERSION,
        yuki_requires=">=3",
        manifest_hash="b" * 64,
        entrypoint="plugin:Plugin",
        requested_permissions=("agent.session", "group.current.read"),
    )
    await installations.approve(plugin)
    await installations.set_enabled(plugin, enabled=True)
    repository = PluginAgentSessionRepository(database)
    provider = FakeLLMProvider(lambda _: "known complete output")
    service = PluginAgentSessionService(
        model_executor=InjectedModelExecutor(provider),
        concurrency=ConcurrencyManager(),
        runtime_config=RuntimeConfigService(
            database=database, settings=make_settings(database.url)
        ),
        repository=repository,
    )
    facade = BoundAgentSessionFacade(
        service=service,
        plugin_id=plugin,
        actor_user_id=None,
        current_group_id=space,
        approved_permissions=("agent.session", "group.current.read"),
    )
    created = await facade.create(
        CreateAgentSessionRequest(name="private story", instructions="Continue this story.")
    )
    request = RunAgentSessionRequest(
        session_id=created.session_id, user_input="original input", max_model_requests=1
    )

    async def pause():
        async with database.immediate_session() as session:
            (await session.get(CanonicalSpaceModel, space)).enabled = False

    return SimpleNamespace(
        plugin=plugin,
        space=space,
        other_space=other_space,
        person=person,
        repository=repository,
        provider=provider,
        service=service,
        facade=facade,
        created=created,
        request=request,
        pause=pause,
    )


@pytest.mark.parametrize("reader", ["history", "record"])
async def test_paused_space_keeps_original_session_material(plugin_session, reader):
    env = plugin_session
    await env.repository.append_message(
        plugin_id=env.plugin,
        session_id=str(env.created.session_id),
        role="assistant",
        content="original persisted output",
        metadata={"chapter": 2},
    )
    await env.pause()
    if reader == "history":
        messages = await env.repository.list_messages(
            plugin_id=env.plugin, session_id=str(env.created.session_id)
        )
        assert [(row.role, row.content, row.metadata) for row in messages] == [
            ("assistant", "original persisted output", {"chapter": 2})
        ]
    else:
        record = await env.repository.get(
            plugin_id=env.plugin, session_id=str(env.created.session_id)
        )
        assert record.session_id == str(env.created.session_id)
        assert record.canonical_space_id == env.space and record.turn_count == 1


async def test_pause_after_model_response_keeps_paid_assistant(
    database, plugin_session, monkeypatch
):
    env = plugin_session
    complete = env.provider.complete

    async def pause_after_response(request):
        result = await complete(request)
        await env.pause()
        return result

    monkeypatch.setattr(env.provider, "complete", pause_after_response)
    result = await env.facade.run(env.request)
    assert result.text == "known complete output" and result.model_requests == 1
    async with database.sessions() as session:
        messages = tuple(
            await session.scalars(
                select(PluginAgentMessageModel)
                .where(PluginAgentMessageModel.session_id == str(env.created.session_id))
                .order_by(PluginAgentMessageModel.sequence)
            )
        )
    assert len(env.provider.requests) == 1
    assert [(row.role, row.content) for row in messages] == [
        ("user", "original input"),
        ("assistant", "known complete output"),
    ]


async def test_pause_after_assistant_commit_keeps_public_run_result(plugin_session, monkeypatch):
    env = plugin_session
    append = env.repository.append_message

    async def pause_after_commit(**kwargs):
        result = await append(**kwargs)
        if kwargs["role"] == "assistant":
            await env.pause()
        return result

    monkeypatch.setattr(env.repository, "append_message", pause_after_commit)
    result = await env.facade.run(env.request)
    assert result.session.session_id == env.created.session_id
    assert result.text == "known complete output" and result.model_requests == 1
    assert len(env.provider.requests) == 1


async def test_paused_space_does_not_admit_a_new_model_request(plugin_session):
    env = plugin_session
    await env.pause()
    with pytest.raises(PluginOwnershipError, match="canonical_owner_disabled"):
        await env.facade.run(env.request)
    assert env.provider.requests == []


async def test_foreign_scene_cannot_run_original_session(plugin_session):
    env = plugin_session
    foreign = BoundAgentSessionFacade(
        service=env.service,
        plugin_id=env.plugin,
        actor_user_id=None,
        current_group_id=env.other_space,
        approved_permissions=("agent.session", "group.current.read"),
    )
    with pytest.raises(PluginSessionNotFoundError):
        await foreign.run(env.request)
    assert env.provider.requests == []


async def test_available_space_returns_complete_original_result(plugin_session):
    env = plugin_session
    result = await env.facade.run(env.request)
    assert result.session.session_id == env.created.session_id
    assert result.text == "known complete output" and result.model_requests == 1
    messages = await env.repository.list_messages(
        plugin_id=env.plugin, session_id=str(env.created.session_id)
    )
    assert [(row.role, row.content) for row in messages] == [
        ("user", "original input"),
        ("assistant", "known complete output"),
    ]
    assert len(env.provider.requests) == 1


async def test_per_message_metadata_remains_original_material(plugin_session):
    env = plugin_session
    metadata = {"chapter": 2, "origin": {"note": "plugin-owned message data"}}
    stored = await env.repository.append_message(
        plugin_id=env.plugin,
        session_id=str(env.created.session_id),
        role="assistant",
        content="original persisted chapter",
        metadata=metadata,
    )
    (read,) = await env.repository.list_messages(
        plugin_id=env.plugin, session_id=str(env.created.session_id)
    )
    assert read.id == stored.id
    assert read.content == "original persisted chapter" and read.metadata == metadata
    assert not env.provider.requests


async def _expiring_session(database, env, monkeypatch):
    import yuki.plugin_host.session_repository as repository_module
    import yuki.services.plugin_sessions as service_module

    clock = SimpleNamespace(now=datetime.now(UTC))

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock.now if tz is not None else clock.now.replace(tzinfo=None)

    monkeypatch.setattr(repository_module, "datetime", Clock)
    monkeypatch.setattr(service_module, "datetime", Clock)
    expiry = clock.now + timedelta(seconds=1)
    async with database.immediate_session() as writer:
        row = await writer.get(PluginAgentSessionModel, str(env.created.session_id))
        row.expires_at = expiry
    return clock, expiry


@pytest.mark.parametrize("maintenance", [False, True])
async def test_paid_response_crossing_session_expiry_keeps_original_material_and_result(
    database, plugin_session, monkeypatch, maintenance
):
    env = plugin_session
    clock, expiry = await _expiring_session(database, env, monkeypatch)
    paid, release = asyncio.Event(), asyncio.Event()
    complete = env.provider.complete

    async def hold_paid_response(request):
        response = await complete(request)
        paid.set()
        await release.wait()
        return response

    monkeypatch.setattr(env.provider, "complete", hold_paid_response)
    running = asyncio.create_task(env.facade.run(env.request))
    try:
        async with asyncio.timeout(5):
            await paid.wait()
        clock.now = expiry + timedelta(seconds=1)
        if maintenance:
            assert await env.repository.expire_due() == 1
        release.set()
        result = await running
    finally:
        release.set()
        await asyncio.gather(running, return_exceptions=True)
    assert result.session.session_id == env.created.session_id
    assert result.session.status.value == ("expired" if maintenance else "active")
    assert result.text == "known complete output" and result.model_requests == 1
    original = await env.repository.get(
        plugin_id=env.plugin, session_id=str(env.created.session_id)
    )
    assert original.expires_at == expiry and original.canonical_space_id == env.space
    assert original.turn_count == 1 and original.next_sequence == 3
    messages = await env.repository.list_messages(
        plugin_id=env.plugin, session_id=str(env.created.session_id)
    )
    assert [(item.sequence, item.role, item.content) for item in messages] == [
        (1, "user", "original input"),
        (2, "assistant", "known complete output"),
    ]
    with pytest.raises(PluginSessionNotFoundError):
        await env.facade.run(env.request)
    assert len(env.provider.requests) == 1
    assert (
        await env.repository.get_for_actor(
            plugin_id=env.plugin,
            session_id=str(env.created.session_id),
            actor_user_id=None,
            current_group_id=env.other_space,
        )
        is None
    )


async def test_expiry_after_assistant_commit_does_not_hide_completed_public_result(
    database, plugin_session, monkeypatch
):
    env = plugin_session
    clock, expiry = await _expiring_session(database, env, monkeypatch)
    append = env.repository.append_message

    async def expire_after_commit(**kwargs):
        result = await append(**kwargs)
        if kwargs["role"] == "assistant":
            clock.now = expiry + timedelta(seconds=1)
        return result

    monkeypatch.setattr(env.repository, "append_message", expire_after_commit)
    result = await env.facade.run(env.request)
    assert result.session.session_id == env.created.session_id
    assert result.text == "known complete output" and result.model_requests == 1
    assert len(env.provider.requests) == 1


async def test_deleted_session_during_paid_response_is_never_recreated(
    database, plugin_session, monkeypatch
):
    env = plugin_session
    paid, release = asyncio.Event(), asyncio.Event()
    complete = env.provider.complete

    async def hold_paid_response(request):
        response = await complete(request)
        paid.set()
        await release.wait()
        return response

    monkeypatch.setattr(env.provider, "complete", hold_paid_response)
    running = asyncio.create_task(env.facade.run(env.request))
    try:
        async with asyncio.timeout(5):
            await paid.wait()
        assert await env.repository.delete(
            plugin_id=env.plugin, session_id=str(env.created.session_id)
        )
        release.set()
        with pytest.raises(PluginSessionUnavailableError):
            await running
    finally:
        release.set()
        await asyncio.gather(running, return_exceptions=True)
    assert (
        await env.repository.get(plugin_id=env.plugin, session_id=str(env.created.session_id))
        is None
    )
    assert (
        await env.repository.list_messages(
            plugin_id=env.plugin, session_id=str(env.created.session_id)
        )
        == ()
    )
    assert len(env.provider.requests) == 1


async def test_explicitly_closed_session_keeps_material_but_cannot_admit_new_run(plugin_session):
    env = plugin_session
    await env.repository.append_message(
        plugin_id=env.plugin,
        session_id=str(env.created.session_id),
        role="assistant",
        content="original accepted chapter",
    )
    result = await env.facade.close(env.created.session_id)
    assert result.status.value == "closed"
    with pytest.raises(PluginSessionNotFoundError):
        await env.facade.run(env.request)
    assert not env.provider.requests
    messages = await env.repository.list_messages(
        plugin_id=env.plugin, session_id=str(env.created.session_id)
    )
    assert [item.content for item in messages] == ["original accepted chapter"]


@pytest.mark.parametrize("status", [SessionStatus.EXPIRED, SessionStatus.BLOCKED])
async def test_sdk_fake_keeps_terminal_state_and_does_not_admit_another_run(status):
    sessions = FakeAgentSessionFacade()
    created = await sessions.create(
        CreateAgentSessionRequest(name="original", instructions="original task")
    )
    sessions.sessions[created.session_id] = created.model_copy(update={"status": status})
    with pytest.raises(ValueError, match=status.value):
        await sessions.run(RunAgentSessionRequest(session_id=created.session_id, user_input="new"))
    assert sessions.history[created.session_id] == []
    assert sessions.sessions[created.session_id].status is status
