"""Application assembly and usable control read/write contracts."""

from __future__ import annotations

import dataclasses
import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select
from tests.conftest import make_settings

from qq_ai_bot.admin.config_service import RuntimeConfigService
from qq_ai_bot.admin.models import ConfigScopeType
from qq_ai_bot.control_plane import (
    ConfigQueryScope,
    ControlCommand,
    ControlCommandError,
    ControlCommandService,
    ControlPrincipal,
    ControlQueryError,
    ControlQueryService,
    DecisionContext,
    PageRequest,
    PrincipalSource,
    ProblemCode,
)
from qq_ai_bot.control_plane.query_cursors import encode_query_cursor
from qq_ai_bot.control_plane.query_types import QueryCursorPhase, QueryResourceKind
from qq_ai_bot.domain.identity import PersonId, PrincipalId, RequestId, SpaceId
from qq_ai_bot.identity.db_models import CanonicalPersonModel, CanonicalSpaceModel
from qq_ai_bot.memory.repository import MemoryFactRepository
from qq_ai_bot.memory.service import MemoryFactService
from qq_ai_bot.persistence.control_command import ControlCommandAdapter
from qq_ai_bot.persistence.control_query import ControlQueryAdapter
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import (
    AdminOperationEventModel,
    MemoryFactModel,
    RuntimeConfigOverrideModel,
)

_NOW = datetime(2026, 9, 27, tzinfo=UTC)


def context(*capabilities: str) -> DecisionContext:
    principal = ControlPrincipal(
        principal_id=PrincipalId.new(),
        person_id=PersonId.new(),
        source=PrincipalSource.CLI,
        roles=("maintainer",),
        granted_capabilities=capabilities,
        authenticated=True,
        active=True,
    )
    return DecisionContext(
        request_id=RequestId.new(),
        principal=principal,
        source=principal.source,
        canonical_target=principal.person_id,
        reason="control-foundation-test",
    )


async def override(
    database: Database,
    key: str,
    value: object,
    *,
    mode: str = "hot",
    scope: str = "global",
    owner: str | None = None,
) -> None:
    async with database.sessions() as session, session.begin():
        session.add(
            RuntimeConfigOverrideModel(
                config_key=key,
                scope_type=scope,
                value_json=json.dumps(value),
                value_type=(
                    "boolean"
                    if type(value) is bool
                    else "integer"
                    if type(value) is int
                    else "string"
                ),
                apply_mode=mode,
                canonical_person_id=owner if scope == "user" else None,
                canonical_space_id=owner if scope == "group" else None,
                version=1,
                created_at=_NOW,
                updated_at=_NOW,
                updated_by="test",
            )
        )


@pytest.mark.asyncio
async def test_config_defaults_schema_and_secrets(database: Database) -> None:
    settings = make_settings(database.url, llm_api_key="foundation-secret")
    runtime = RuntimeConfigService(settings=settings, database=database)
    await runtime.initialize()
    service = ControlQueryService(
        ControlQueryAdapter(
            database,
            settings=settings,
            runtime_config=runtime,
        )
    )
    ctx = context("control.config.read")
    effective = await service.list_effective_configs(ctx, PageRequest(limit=100))
    specs = await service.list_config_specs(ctx, PageRequest(limit=100))
    actual = {item.key: item for item in effective.items}
    schema = {item.key: item for item in specs.items}
    assert actual["agent.max_model_requests"].value == settings.agent_max_model_requests
    assert actual["agent.max_model_requests"].saved_value == settings.agent_max_model_requests
    assert actual["agent.max_model_requests"].version is None
    assert schema["agent.max_model_requests"].minimum is not None
    assert schema["agent.max_model_requests"].display_name
    assert schema["agent.max_model_requests"].allowed_scopes
    dumped = json.dumps(dataclasses.asdict(effective), default=str)
    assert "foundation-secret" not in dumped
    # A schema cursor cannot be consumed by effective values or stored overrides.
    for read in (service.list_effective_configs, service.list_config_overrides):
        with pytest.raises(ControlQueryError) as exc:
            await read(ctx, PageRequest(cursor=specs.next_cursor))
        assert exc.value.problem.code is ProblemCode.VALIDATION_ERROR


@pytest.mark.asyncio
async def test_memory_health_reports_missing_embedding_provider_as_degraded(
    database: Database,
) -> None:
    settings = make_settings(
        database.url,
        memory_embedding_enabled=True,
        memory_embedding_base_url="",
        memory_embedding_api_key="",
    )
    runtime = RuntimeConfigService(settings=settings, database=database)
    await runtime.initialize()
    health = await ControlQueryAdapter(
        database, settings=settings, runtime_config=runtime
    ).read_memory_health()
    assert health.embedding == "not_configured"
    assert health.embedding_requested is True
    assert health.embedding_configured is False
    assert health.embedding_saved_enabled is True
    assert health.embedding_config_version is None


@pytest.mark.asyncio
async def test_embedding_admin_switch_activates_at_next_start(database: Database) -> None:
    settings = make_settings(database.url, memory_embedding_enabled=True)
    await override(
        database,
        "memory.embedding_enabled",
        False,
        mode="restart_required",
    )
    runtime = RuntimeConfigService(settings=settings, database=database)
    await runtime.initialize()
    assert (await runtime.startup_settings_updates())["memory_embedding_enabled"] is False


@pytest.mark.asyncio
async def test_restart_saved_active_and_removed_values(database: Database) -> None:
    settings = make_settings(database.url)
    await override(database, "llm.model", "startup-model", mode="restart_required")
    runtime = RuntimeConfigService(settings=settings, database=database)
    await runtime.initialize()
    adapter = ControlQueryAdapter(database, settings=settings, runtime_config=runtime)
    assert (await adapter.read_system()).pending_restart.count == 0
    async with database.sessions() as session, session.begin():
        row = await session.scalar(select(RuntimeConfigOverrideModel))
        assert row is not None
        row.value_json = '"saved-model"'
        row.version = 2
    page = await adapter.list_effective_configs(PageRequest(limit=100))
    # llm.model may fall on a later page in the full Registry.
    items = list(page.items)
    while page.next_cursor:
        page = await adapter.list_effective_configs(PageRequest(limit=100, cursor=page.next_cursor))
        items.extend(page.items)
    value = next(item for item in items if item.key == "llm.model")
    assert (value.value, value.saved_value, value.version) == ("startup-model", "saved-model", 2)
    assert value.pending_restart
    assert (await adapter.read_system()).pending_restart.keys == ("llm.model",)
    async with database.sessions() as session, session.begin():
        row = await session.scalar(select(RuntimeConfigOverrideModel))
        assert row is not None
        await session.delete(row)
    assert await runtime.pending_restart_count() == 1
    inspected = (await runtime.inspect_configs(("llm.model",)))[0]
    assert inspected.effective.value == "startup-model"
    assert inspected.saved.value == settings.model_runtime.llm_model
    assert inspected.pending_restart and inspected.version is None


@pytest.mark.asyncio
async def test_canonical_scope_precedence_and_cursor_isolation(database: Database) -> None:
    person, space = PersonId.new(), SpaceId.new()
    async with database.sessions() as session, session.begin():
        session.add(
            CanonicalPersonModel(
                id=person.text, revision=1, enabled=True, created_at=_NOW, updated_at=_NOW
            )
        )
        session.add(
            CanonicalSpaceModel(
                id=space.text, revision=1, enabled=True, created_at=_NOW, updated_at=_NOW
            )
        )
    settings = make_settings(database.url)
    runtime = RuntimeConfigService(settings=settings, database=database)
    await runtime.initialize()
    spec = runtime.registry.get("agent.max_model_requests")
    assert ConfigScopeType.USER in spec.allowed_scopes
    await override(database, spec.key, 10)
    await override(database, spec.key, 11, scope="group", owner=space.text)
    await override(database, spec.key, 12, scope="user", owner=person.text)
    adapter = ControlQueryAdapter(database, settings=settings, runtime_config=runtime)
    scope = ConfigQueryScope(person_id=person, space_id=space)
    page = await adapter.list_effective_configs(PageRequest(limit=100), scope=scope)
    value = next(item for item in page.items if item.key == spec.key)
    assert value.value == value.saved_value == 12
    assert value.source == "runtime:user" and value.person_id == person
    assert value.version == 1
    with pytest.raises(ControlQueryError) as exc:
        await adapter.list_effective_configs(PageRequest(cursor=page.next_cursor))
    assert exc.value.problem.code is ProblemCode.VALIDATION_ERROR
    with pytest.raises(ControlQueryError):
        await adapter.list_effective_configs(
            PageRequest(), scope=ConfigQueryScope(person_id=PersonId.new())
        )
    with pytest.raises(ControlQueryError):
        await adapter.list_memory_evidence(
            PageRequest(
                cursor=encode_query_cursor(
                    QueryResourceKind.MEMORY_FACT,
                    QueryCursorPhase.CANONICAL,
                    "1",
                )
            ),
            include_content=False,
        )


@pytest.mark.asyncio
async def test_config_query_version_can_write_and_replay(database: Database) -> None:
    settings = make_settings(database.url)
    runtime = RuntimeConfigService(settings=settings, database=database)
    await runtime.initialize()
    queries = ControlQueryService(
        ControlQueryAdapter(database, settings=settings, runtime_config=runtime)
    )
    commands = ControlCommandService(
        ControlCommandAdapter(database, settings=settings, runtime_config=runtime)
    )
    ctx = context("control.config.read", "control.config.mutate")
    payload = {
        "key": "agent.max_model_requests",
        "scope_type": "global",
        "scope_id": "",
        "value": 20,
    }
    command = ControlCommand(request_id=ctx.request_id, expected_revision=0, payload=payload)
    result = await commands.set_config(ctx, command)
    assert result.success
    assert await commands.set_config(ctx, command) == result
    async with database.sessions() as session:
        audits = (await session.scalars(select(AdminOperationEventModel))).all()
        assert len(audits) == 2
        assert all(row.trigger_message_id == "" for row in audits)
        assert all(row.control_request_id == ctx.request_id.text for row in audits)
        assert all(row.actor_principal_kind == "control" for row in audits)
        assert all(row.actor_principal_id == ctx.principal.principal_id.text for row in audits)
    page = await queries.list_effective_configs(ctx, PageRequest(limit=100))
    value = next(item for item in page.items if item.key == payload["key"])
    assert value.value == 20 and value.version == result.revision
    stale_ctx = dataclasses.replace(ctx, request_id=RequestId.new())
    with pytest.raises(ControlCommandError) as exc:
        await commands.set_config(
            stale_ctx,
            ControlCommand(request_id=stale_ctx.request_id, expected_revision=0, payload=payload),
        )
    assert exc.value.problem.code is ProblemCode.VERSION_CONFLICT
    async with database.sessions() as session:
        # The runtime domain audit and control audit are different evidence layers;
        # replay contributes neither one again.
        assert (
            await session.scalar(select(func.count()).select_from(RuntimeConfigOverrideModel)) == 1
        )


@pytest.mark.asyncio
async def test_container_assembles_live_control_services(database: Database, tmp_path) -> None:
    from tests.support.model_profiles import write_fake_profiles

    from qq_ai_bot.container import ApplicationContainer

    settings = make_settings(
        database.url, model_profiles_file=write_fake_profiles(tmp_path / "models.toml")
    )
    runtime = RuntimeConfigService(settings=settings, database=database)
    await runtime.initialize()
    container = ApplicationContainer(settings, database=database, runtime_config=runtime)
    try:
        query = container.control_plane.queries._port
        management = container.control_plane.commands._port._management
        assert query._config is container.runtime_config
        assert query._connections is container.gateway_registry
        assert management._automation is container.automation
        assert management._memories is container.memories
        snapshot = await container.control_plane.queries.read_system(context("control.system.read"))
        assert snapshot.pending_restart.count == 0
        health = await container.control_plane.queries.read_health(context("control.health.read"))
        assert health.database == "ok" and health.queue is not None
        components = {item.name: item for item in health.components}
        assert components["work"].enabled is True
        assert components["work"].running is False
        assert components["gateway"].running is False
        assert all(item.healthy is None for item in health.components)
        # Default deny still applies at the assembled application boundary.
        async with database.sessions() as session:
            before = await session.scalar(
                select(func.count()).select_from(AdminOperationEventModel)
            )
        with pytest.raises(ControlQueryError):
            await container.control_plane.queries.list_effective_configs(context(), PageRequest())
        async with database.sessions() as session:
            assert (
                await session.scalar(select(func.count()).select_from(AdminOperationEventModel))
                == before
            )
    finally:
        await container.close()


@pytest.mark.asyncio
async def test_health_database_failure_does_not_invent_queue_or_revision(database, monkeypatch):
    async def unavailable():
        return False

    monkeypatch.setattr(database, "ping", unavailable)
    health = await ControlQueryAdapter(database).read_health()
    assert health.database == "unavailable"
    assert health.queue is None and health.identity_revision is None


@pytest.mark.asyncio
async def test_memory_read_revision_supports_mutation_and_stale_rejection(
    database: Database,
) -> None:
    async with database.sessions() as session, session.begin():
        session.add(
            MemoryFactModel(
                scope_type="self",
                visibility_type="global",
                kind="fact",
                memory_key="fixture",
                category="fixture",
                content="fixture",
                normalized_content="fixture",
                source_type="explicit",
                authority="agent_reflection",
                status="active",
                created_at=_NOW,
                updated_at=_NOW,
            )
        )
    queries = ControlQueryService(ControlQueryAdapter(database))
    commands = ControlCommandService(
        ControlCommandAdapter(
            database,
            memories=MemoryFactService(MemoryFactRepository(database)),
        )
    )
    ctx = context("control.memory.metadata.read", "control.memory.mutate")
    fact = (await queries.list_memory_facts(ctx, PageRequest())).items[0]
    command = ControlCommand(
        request_id=ctx.request_id,
        expected_revision=fact.revision,
        payload={"action": "quarantine", "resource_id": str(fact.fact_id)},
    )
    result = await commands.mutate_memory(ctx, command)
    assert result.revision > fact.revision
    assert await commands.mutate_memory(ctx, command) == result
    updated = (await queries.list_memory_facts(ctx, PageRequest())).items[0]
    assert updated.revision == result.revision
    assert updated.content is None  # metadata permission does not expose content
    stale_ctx = dataclasses.replace(ctx, request_id=RequestId.new())
    with pytest.raises(ControlCommandError) as exc:
        await commands.mutate_memory(
            stale_ctx,
            ControlCommand(
                request_id=stale_ctx.request_id,
                expected_revision=fact.revision,
                payload=command.payload,
            ),
        )
    assert exc.value.problem.code is ProblemCode.VERSION_CONFLICT


@pytest.mark.asyncio
async def test_automation_operator_id_is_not_used_as_person(database: Database) -> None:
    class RecordingAutomation:
        def __init__(self) -> None:
            self.actors: list[str] = []

        async def administer_create(
            self, _spec: object, *, owner_id: str, **_kwargs: object
        ) -> None:
            self.actors.append(owner_id)
            raise RuntimeError("synthetic domain precondition")

    automation = RecordingAutomation()
    commands = ControlCommandService(ControlCommandAdapter(database, automation=automation))
    ctx = context("control.automation.mutate")
    assert ctx.principal.principal_id.text != ctx.principal.person_id.text
    with pytest.raises(ControlCommandError) as exc:
        await commands.mutate_automation(
            ctx,
            ControlCommand(
                request_id=ctx.request_id,
                expected_revision=0,
                payload={"action": "create", "spec": {"script": {}}},
            ),
        )
    assert exc.value.problem.code is ProblemCode.PRECONDITION_FAILED
    assert automation.actors == [ctx.principal.person_id.text]
    operator = dataclasses.replace(ctx.principal, person_id=None)
    no_person = dataclasses.replace(ctx, principal=operator, request_id=RequestId.new())
    with pytest.raises(ControlCommandError):
        await commands.mutate_automation(
            no_person,
            ControlCommand(
                request_id=no_person.request_id,
                expected_revision=0,
                payload={"action": "create", "spec": {"script": {}}},
            ),
        )
    assert automation.actors == [ctx.principal.person_id.text]


@pytest.mark.asyncio
async def test_missing_runtime_does_not_guess_effective_state(database: Database) -> None:
    adapter = ControlQueryAdapter(database)
    with pytest.raises(ControlQueryError):
        await adapter.read_system()
    with pytest.raises(ControlQueryError):
        await adapter.list_effective_configs(PageRequest())
