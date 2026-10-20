"""Session material and known output are distinct from permission to start a run."""

from types import SimpleNamespace

import pytest
from sqlalchemy import select
from tests.conftest import make_settings
from tests.support.canonical_identity import ensure_person, ensure_space
from tests.support.model_executor import InjectedModelExecutor

from yuki.admin.config_service import RuntimeConfigService
from yuki.identity.db_models import CanonicalSpaceModel
from yuki.llm.fake import FakeLLMProvider
from yuki.plugin_host.db_models import PluginAgentMessageModel
from yuki.plugin_host.ownership import PluginOwnershipError
from yuki.plugin_host.repository import PluginInstallationRepository
from yuki.plugin_host.session_facade import BoundAgentSessionFacade
from yuki.plugin_host.session_repository import PluginAgentSessionRepository
from yuki.services.concurrency import ConcurrencyManager
from yuki.services.plugin_sessions import PluginAgentSessionService, PluginSessionNotFoundError
from yuki_plugin_sdk.api import PLUGIN_API_VERSION
from yuki_plugin_sdk.sessions import CreateAgentSessionRequest, RunAgentSessionRequest


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


@pytest.mark.xfail(
    strict=True,
    raises=PluginOwnershipError,
    reason=(
        "F04: paused Space must not hide session material/DTO; "
        "docs/operations/backend-five-pass-audit-20261020.md"
    ),
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


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=(
        "F04: already returned model output must be saved after Space pause; "
        "docs/operations/backend-five-pass-audit-20261020.md"
    ),
)
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
    try:
        await env.facade.run(env.request)
    except PluginOwnershipError:
        # Inspect the independently persisted known result, even if the later
        # return/read gate is still broken. This case does not freeze that error.
        pass
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


@pytest.mark.xfail(
    strict=True,
    raises=PluginOwnershipError,
    reason=(
        "F04: persisted known result must return after Space pause; "
        "docs/operations/backend-five-pass-audit-20261020.md"
    ),
)
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
