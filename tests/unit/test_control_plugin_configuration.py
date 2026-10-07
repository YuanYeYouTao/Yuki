"""Original plugin rows, whole-schema CAS, durable controls and bounded observations."""

import asyncio
import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from pydantic import create_model
from sqlalchemy import event, select
from tests.conftest import make_settings
from tests.unit.test_canonical_ingress import _message
from tests.unit.test_control_operator_access import operator_file
from tests.unit.test_control_plane_foundation import context
from tests.unit.test_webui_activity import ingress

from qq_ai_bot import __version__
from qq_ai_bot.application.control_access import ControlOperatorAccess
from qq_ai_bot.application.modules.control_plane import ControlPlaneBundle
from qq_ai_bot.control_plane import (
    ControlCommand,
    ControlCommandError,
    ControlCommandService,
    ControlQueryError,
    ControlQueryService,
    PageRequest,
    ProblemCode,
    YukiControlTarget,
)
from qq_ai_bot.control_plane.query_cursors import encode_query_cursor
from qq_ai_bot.control_plane.query_types import QueryCursorPhase, QueryResourceKind
from qq_ai_bot.identity.canonical_repository import ensure_person, ensure_space
from qq_ai_bot.persistence.control_command import ControlCommandAdapter
from qq_ai_bot.persistence.control_query import ControlQueryAdapter
from qq_ai_bot.persistence.models import AdminOperationEventModel
from qq_ai_bot.plugin_host.config import BoundConfigFacade
from qq_ai_bot.plugin_host.configuration_service import (
    PluginConfigurationError,
    PluginConfigurationService,
)
from qq_ai_bot.plugin_host.db_models import PluginConfigValueModel, PluginNotificationOutboxModel
from qq_ai_bot.plugin_host.discovery import PluginDiscovery
from qq_ai_bot.plugin_host.event_bus import PluginEventBus
from qq_ai_bot.plugin_host.extension_registry import ExtensionRegistry
from qq_ai_bot.plugin_host.loader import PluginLoader
from qq_ai_bot.plugin_host.manager import PluginManagementRejected, PluginManager
from qq_ai_bot.plugin_host.repository import (
    PluginConfigRepository,
    PluginInstallationRepository,
    PluginStateRepository,
    PluginVersionConflictError,
)
from qq_ai_bot.plugin_host.storage import BoundStorageFacade
from qq_ai_bot.webui.http import attach_webui
from yuki_plugin_sdk.models import StrictModel
from yuki_plugin_sdk.observation import PluginObservationRequest
from yuki_plugin_sdk.permissions import PluginPermission

PLUGIN = "test.configuration"


@pytest.mark.parametrize("expected_version", [0, 1])
async def test_per_key_config_concurrent_cas_has_one_winner(
    database, plugin, monkeypatch, expected_version
):
    from qq_ai_bot.plugin_host import ownership

    repository = PluginConfigRepository(database)
    arguments = dict(plugin_id=PLUGIN, scope_type="global", scope_id="", key="low")
    if expected_version:
        await repository.compare_and_set(**arguments, expected_version=0, value=1)
    barrier = asyncio.Barrier(2)
    original = ownership.find_config_lineage

    async def read_same_version(*args, **kwargs):
        row = await original(*args, **kwargs)
        await barrier.wait()
        return row

    monkeypatch.setattr(ownership, "find_config_lineage", read_same_version)
    outcomes = await asyncio.wait_for(
        asyncio.gather(
            repository.compare_and_set(**arguments, expected_version=expected_version, value=3),
            repository.compare_and_set(**arguments, expected_version=expected_version, value=5),
            return_exceptions=True,
        ),
        timeout=5,
    )
    assert sum(isinstance(result, PluginVersionConflictError) for result in outcomes) == 1
    winners = [result for result in outcomes if not isinstance(result, BaseException)]
    assert len(winners) == 1
    assert winners[0].version == expected_version + 1
    monkeypatch.setattr(ownership, "find_config_lineage", original)
    stored = await repository.get(**arguments)
    assert stored is not None and stored.version == winners[0].version
    assert stored.value == winners[0].value


async def test_per_key_config_stale_delete_cannot_remove_new_version(database, plugin, monkeypatch):
    from qq_ai_bot.plugin_host import ownership

    repository = PluginConfigRepository(database)
    arguments = dict(plugin_id=PLUGIN, scope_type="global", scope_id="", key="low")
    await repository.compare_and_set(**arguments, expected_version=0, value=1)
    observed = asyncio.Event()
    resume = asyncio.Event()
    original = ownership.require_config_readable

    async def pause_delete(session, row):
        await original(session, row)
        if asyncio.current_task().get_name() == "stale-config-delete":
            observed.set()
            await resume.wait()

    monkeypatch.setattr(ownership, "require_config_readable", pause_delete)
    deletion = asyncio.create_task(
        repository.delete(**arguments, expected_version=1), name="stale-config-delete"
    )
    try:
        await asyncio.wait_for(observed.wait(), timeout=5)
        replacement = await repository.compare_and_set(**arguments, expected_version=1, value=5)
        resume.set()
        with pytest.raises(PluginVersionConflictError):
            await asyncio.wait_for(deletion, timeout=5)
        stored = await repository.get(**arguments)
        assert stored is not None and stored.version == replacement.version == 2
        assert stored.value == 5
    finally:
        resume.set()
        if not deletion.done():
            deletion.cancel()
        await asyncio.gather(deletion, return_exceptions=True)


@pytest.fixture
async def plugin(database, tmp_path):
    root = tmp_path / "plugins" / PLUGIN
    root.mkdir(parents=True)
    root.joinpath("plugin.toml").write_text(
        f'''
id = "{PLUGIN}"
name = "Configuration fixture"
version = "1.0.0"
description = "Offline control regression"
entrypoint = "fixture:Fixture"
plugin_api = "3.2"
yuki_requires = ">=3.8"
permissions = ["plugin.config.read", "storage.private"]
''',
        encoding="utf-8",
    )
    root.joinpath("fixture.py").write_text(
        """
from pydantic import Field, model_validator
from yuki_plugin_sdk.models import StrictModel
class Config(StrictModel):
    low: int = Field(default=1, ge=1, le=20)
    high: int = Field(default=10, ge=1, le=20)
    @model_validator(mode="after")
    def ordered(self):
        if self.low > self.high: raise ValueError("invalid range")
        return self
class Fixture:
    async def register(self, registrar): registrar.register_config_schema(Config)
    async def start(self, context): pass
    async def stop(self): pass
    async def observe(self, context, request):
        assert not hasattr(context, "storage") and not hasattr(context, "config")
        return {"value": await context.get_config("low"),
                "state": await context.get_state("fixture", "key")}
""",
        encoding="utf-8",
    )
    granted = []

    def factory(manifest, permissions):
        granted.append(permissions)
        return SimpleNamespace(
            config=BoundConfigFacade(
                repository=PluginConfigRepository(database),
                plugin_id=manifest.id,
                approved_permissions=permissions,
            ),
            storage=BoundStorageFacade(
                repository=PluginStateRepository(database),
                plugin_id=manifest.id,
                approved_permissions=permissions,
            ),
        )

    manager = PluginManager(
        enabled=True,
        discovery=PluginDiscovery(tmp_path / "plugins", yuki_version=__version__),
        installations=PluginInstallationRepository(database),
        loader=PluginLoader(),
        extensions=ExtensionRegistry(),
        event_bus=PluginEventBus(),
        context_factory=factory,
    )
    await manager.start()
    await manager.approve(
        PLUGIN, actor_user_id="offline", permissions=("plugin.config.read", "storage.private")
    )
    await manager.enable(PLUGIN, actor_user_id="offline")
    try:
        yield manager, PluginConfigurationService(database, manager.configuration_schema), granted
    finally:
        await manager.stop()


@pytest.mark.parametrize("scope", ["global", "user", "group"])
async def test_full_schema_reuses_original_canonical_config_rows(database, plugin, scope):
    manager, service, _ = plugin
    async with database.sessions() as session, session.begin():
        owner = (
            await ensure_person(session, "1001")
            if scope == "user"
            else await ensure_space(session, "2001")
            if scope == "group"
            else None
        )
    view = await service.read(PLUGIN, scope_type=scope, owner_id=owner)
    spec = {"scope_type": scope, "owner_id": owner, "values": {"low": 3, "high": 5}}
    revision = await service.save(PLUGIN, view["revision"], spec)
    assert revision != view["revision"] and 0 < revision < 2**53
    assert (await service.read(PLUGIN, scope_type=scope, owner_id=owner))["revision"] == revision
    facade = BoundConfigFacade(
        repository=PluginConfigRepository(database),
        plugin_id=PLUGIN,
        approved_permissions=[PluginPermission.PLUGIN_CONFIG_READ],
        schema=manager.configuration_schema(PLUGIN),
        current_user_id="1001",
        current_group_id="2001",
    )
    assert await facade.get("low", scope_type=scope) == 3
    with pytest.raises(PluginConfigurationError, match="version_conflict"):
        await service.save(PLUGIN, view["revision"], spec)
    async with database.sessions() as session:
        rows = (await session.scalars(select(PluginConfigValueModel))).all()
        assert len(rows) == 2 and {row.key for row in rows} == {"low", "high"}
        assert all(
            row.canonical_person_id == (owner if scope == "user" else None)
            and row.canonical_space_id == (owner if scope == "group" else None)
            for row in rows
        )


@pytest.mark.parametrize(
    "values", [{"low": 0}, {"low": 15, "high": 5}, {"other": "private"}, {"low": float("nan")}]
)
async def test_invalid_values_are_rejected_before_any_writer(database, plugin, values):
    _, service, _ = plugin
    view = await service.read(PLUGIN)
    statements = []

    def capture(_conn, _cursor, statement, *_args):
        statements.append(statement)

    event.listen(database.engine.sync_engine, "before_cursor_execute", capture)
    try:
        with pytest.raises(PluginConfigurationError, match="validation_error"):
            await service.save(
                PLUGIN,
                view["revision"],
                {"scope_type": "global", "owner_id": None, "values": values},
            )
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", capture)
    assert not statements
    assert (await service.read(PLUGIN))["revision"] == view["revision"]


async def test_concurrent_scope_saves_have_one_winner(plugin):
    _, service, _ = plugin
    revision = (await service.read(PLUGIN))["revision"]
    results = await asyncio.gather(
        *(
            service.save(
                PLUGIN,
                revision,
                {"scope_type": "global", "owner_id": None, "values": {"low": low, "high": 10}},
            )
            for low in (2, 3)
        ),
        return_exceptions=True,
    )
    assert sum(type(result) is int for result in results) == 1
    assert (
        sum(
            isinstance(result, PluginConfigurationError) and result.category == "version_conflict"
            for result in results
        )
        == 1
    )


async def test_serialized_config_budget_does_not_count_hash_framing_twice(database, plugin):
    class Document(StrictModel):
        content: str = ""

    service = PluginConfigurationService(database, lambda _: Document)
    view = await service.read(PLUGIN)
    content = '"\\' * 50000
    revision = await service.save(
        PLUGIN,
        view["revision"],
        {"scope_type": "global", "owner_id": None, "values": {"content": content}},
    )
    current = await service.read(PLUGIN)
    assert current["revision"] == revision and current["values"]["content"] == content


async def test_schema_with_more_than_supported_key_count_is_unavailable(database, plugin):
    schema = create_model(
        "TooManyKeys", __base__=StrictModel, **{f"field_{i}": (int, 1) for i in range(257)}
    )
    service = PluginConfigurationService(database, lambda _: schema)
    with pytest.raises(PluginConfigurationError, match="operation_unavailable"):
        await service.read(PLUGIN)


async def test_scope_owner_must_be_live_canonical_identity(database, plugin):
    _, service, _ = plugin
    with pytest.raises(PluginConfigurationError, match="validation_error"):
        await service.read(PLUGIN, scope_type="user", owner_id="1001")
    async with database.sessions() as session, session.begin():
        owner = await ensure_space(session, "2001")
    with pytest.raises(PluginConfigurationError, match="state_mismatch"):
        await service.read(PLUGIN, scope_type="user", owner_id=owner)


async def test_undeclared_old_fields_are_not_projected_and_are_deleted_on_save(database, plugin):
    _, service, _ = plugin
    async with database.sessions() as session, session.begin():
        session.add(
            PluginConfigValueModel(
                plugin_id=PLUGIN,
                scope_type="global",
                key="obsolete",
                value_json='"private-old-content"',
                version=1,
                updated_at=datetime.now(UTC),
            )
        )
    view = await service.read(PLUGIN)
    assert not view["valid"] and "private-old-content" not in json.dumps(view)
    await service.save(
        PLUGIN,
        view["revision"],
        {"scope_type": "global", "owner_id": None, "values": view["values"]},
    )
    assert (await service.read(PLUGIN))["valid"]
    async with database.sessions() as session:
        assert (
            await session.scalar(
                select(PluginConfigValueModel).where(PluginConfigValueModel.key == "obsolete")
            )
            is None
        )


async def test_permissions_unavailable_schema_and_original_receipt(database, plugin, monkeypatch):
    manager, service, _ = plugin
    queries = ControlQueryService(ControlQueryAdapter(database, plugins=manager))
    commands = ControlCommandService(ControlCommandAdapter(database, plugins=manager))
    with pytest.raises(ControlQueryError) as exc:
        await queries.read_plugin_configuration(context("control.plugin.read"), PLUGIN)
    assert exc.value.problem.code is ProblemCode.CAPABILITY_DENIED
    ctx = replace(
        context("control.plugin.config.mutate"), canonical_target=YukiControlTarget.PERMANENT_YUKI
    )
    view = await service.read(PLUGIN)
    command = ControlCommand(
        request_id=ctx.request_id,
        expected_revision=view["revision"],
        payload={
            "action": "save",
            "resource_id": PLUGIN,
            "spec": {"scope_type": "global", "owner_id": None, "values": {"low": 2, "high": 5}},
        },
    )
    result = await commands.configure_plugin(ctx, command)
    assert result.success and result.effective_state["status"] == "saved"
    assert await commands.configure_plugin(ctx, command) == result
    async with database.sessions() as session:
        rows = (await session.scalars(select(PluginConfigValueModel))).all()
        assert all(row.version == 1 for row in rows)
        audits = (await session.scalars(select(AdminOperationEventModel))).all()
        assert "low" not in " ".join(row.after_json for row in audits)
    with pytest.raises(PluginConfigurationError, match="operation_unavailable"):
        await service.read("never-imported")
    # Original effect fence: do not rerun a config write whose post-commit result is unknown.
    original = PluginConfigurationService.save

    async def fail_after_save(self, *args):
        await original(self, *args)
        raise RuntimeError("crash after committed plugin config")

    monkeypatch.setattr(PluginConfigurationService, "save", fail_after_save)
    ctx2 = replace(ctx, request_id=type(ctx.request_id).new())
    command2 = replace(command, request_id=ctx2.request_id, expected_revision=result.revision)
    unknown = await commands.configure_plugin(ctx2, command2)
    assert not unknown.success and unknown.operation.status.value == "unknown"
    assert (await commands.configure_plugin(ctx2, command2)).operation.status.value == "unknown"
    ctx3 = replace(ctx, request_id=type(ctx.request_id).new())
    with pytest.raises(ControlCommandError) as exc:
        await commands.configure_plugin(ctx3, replace(command2, request_id=ctx3.request_id))
    assert exc.value.problem.code is ProblemCode.PRECONDITION_FAILED


async def test_observation_only_reads_without_manager_lock_and_rejects_stale_projection(plugin):
    manager, _, granted = plugin
    managed = manager._running[PLUGIN]
    original = managed.loaded.instance.observe

    async def observe(context, request):
        assert not manager._lock.locked()
        return await original(context, request)

    managed.loaded.instance.observe = observe
    result = await manager.observe(PLUGIN, PluginObservationRequest())
    assert result == {"value": None, "state": None}
    assert granted[-1] == frozenset(
        {PluginPermission.PLUGIN_CONFIG_READ, PluginPermission.STORAGE_PRIVATE}
    )

    async def stale(context, request):
        manager._running.pop(PLUGIN)
        return {}

    managed.loaded.instance.observe = stale
    try:
        with pytest.raises(PluginManagementRejected, match="observation_changed"):
            await manager.observe(PLUGIN, PluginObservationRequest())
    finally:
        manager._running[PLUGIN] = managed


async def test_observation_oversize_and_permission_denial_are_controlled(database, plugin):
    manager, _, _ = plugin

    async def oversized(*_):
        return {"content": "a" * 65536}

    manager._running[PLUGIN].loaded.instance.observe = oversized
    with pytest.raises(PluginManagementRejected, match="observation_invalid"):
        await manager.observe(PLUGIN, PluginObservationRequest())
    queries = ControlQueryService(ControlQueryAdapter(database, plugins=manager))
    with pytest.raises(ControlQueryError) as exc:
        await queries.read_plugin_observation(context("control.plugin.read"), PLUGIN)
    assert exc.value.problem.code is ProblemCode.CAPABILITY_DENIED
    with pytest.raises(ControlQueryError) as exc:
        await queries.read_plugin_observation(
            context("control.plugin.config.content.read"), "not-loaded"
        )
    assert exc.value.problem.code is ProblemCode.OPERATION_UNAVAILABLE


@pytest.mark.parametrize("slow_factory", [False, True])
async def test_observation_timeout_covers_context_creation_and_plugin_callback(
    plugin, monkeypatch, slow_factory
):
    manager, _, _ = plugin

    async def slow(*_):
        await asyncio.Event().wait()

    original_timeout = asyncio.timeout
    timeouts = []

    def short_timeout(seconds):
        timeouts.append(seconds)
        return original_timeout(0.01)

    monkeypatch.setattr(asyncio, "timeout", short_timeout)
    if slow_factory:
        manager._context_factory = slow
    else:
        manager._running[PLUGIN].loaded.instance.observe = slow
    with pytest.raises(TimeoutError):
        await manager.observe(PLUGIN, PluginObservationRequest())
    assert timeouts == [5]
    assert not manager._lock.locked()


async def test_historical_outbox_without_owner_never_reenters_live_queue(database, plugin):
    resolver, uow, bot = await ingress(database)
    admitted = await resolver.pre_admit(bot, _message(message_id="old-outbox"))
    source = await uow.append_inbound(admitted.message, admitted)
    now = datetime.now(UTC)
    async with database.sessions() as session, session.begin():
        row = PluginNotificationOutboxModel(
            notification_id="legacy",
            part_key="text",
            source_event_id=source.event.id,
            plugin_id=PLUGIN,
            target_type="private",
            target_id="1001",
            bot_user_id="8000",
            part_type="text",
            status="failed",
            attempts=1,
            max_attempts=5,
            last_error_category="bot_unavailable",
            next_attempt_at=now,
            created_at=now,
            updated_at=now,
        )
        session.add(row)
        await session.flush()
        item_id = row.id
    ctx = context("control.plugin.read", "control.plugin.mutate")
    queries = ControlQueryService(ControlQueryAdapter(database))
    item = (await queries.list_plugin_outbox(ctx, PageRequest(), plugin_id=PLUGIN)).items[0]
    assert not item.fields["can_retry"]
    commands = ControlCommandService(ControlCommandAdapter(database))
    command = ControlCommand(
        request_id=ctx.request_id,
        expected_revision=item.fields["revision"],
        payload={"action": "retry", "resource_id": str(item_id)},
    )
    for _ in range(2):
        with pytest.raises(ControlCommandError) as exc:
            await commands.mutate_plugin(ctx, command)
        assert exc.value.problem.code is ProblemCode.STATE_MISMATCH
    async with database.sessions() as session:
        assert (await session.get(PluginNotificationOutboxModel, item_id)).status == "failed"


async def test_http_config_roundtrip_and_finite_query_fields(
    database, plugin, tmp_path, monkeypatch
):
    manager, _, _ = plugin
    origin = "http://127.0.0.1:18765"
    secret = "plugin-test-" + "a" * 48
    monkeypatch.setenv("YUKI_TEST_OPERATOR_TOKEN", secret)
    path, _ = operator_file(
        tmp_path,
        capabilities=(
            "control.plugin.config.content.read",
            "control.plugin.config.mutate",
            "control.plugin.read",
        ),
    )
    settings = make_settings(database.url, webui_enabled=True, webui_origin=origin)
    adapter = ControlCommandAdapter(database, plugins=manager)
    bundle = ControlPlaneBundle(
        ControlOperatorAccess(database, path),
        ControlQueryService(ControlQueryAdapter(database, plugins=manager)),
        ControlCommandService(adapter),
        adapter.recover_interrupted_controls,
    )
    app = FastAPI()
    attach_webui(app, settings, lambda: bundle)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=origin) as client:
        headers = {"Origin": origin}
        assert (
            await client.post("/api/control/login", headers=headers, json={"credential": secret})
        ).status_code == 200
        headers["x-yuki-csrf"] = (await client.get("/api/control/session")).json()["csrf"]
        view = await client.post(
            "/api/control/queries/read_plugin_configuration",
            headers=headers,
            json={"plugin_id": PLUGIN},
        )
        assert view.status_code == 200
        request_id = context().request_id.text
        headers["x-request-id"] = request_id
        body = {
            "request_id": request_id,
            "expected_revision": view.json()["data"]["fields"]["revision"],
            "payload": {
                "resource_id": PLUGIN,
                "action": "save",
                "spec": {"scope_type": "global", "owner_id": None, "values": {"low": 4, "high": 5}},
            },
        }
        for _ in range(2):
            result = await client.post(
                "/api/control/commands/configure_plugin", headers=headers, json=body
            )
            assert result.status_code == 200 and result.json()["data"]["success"]
        observed = await client.post(
            "/api/control/queries/read_plugin_observation",
            headers=headers,
            json={"plugin_id": PLUGIN},
        )
        assert observed.status_code == 200 and observed.json()["data"]["fields"]["value"] == 4
        assert (
            await client.post(
                "/api/control/queries/list_plugin_outbox",
                headers=headers,
                json={"plugin_id": PLUGIN},
            )
        ).status_code == 200
        assert (
            await client.post(
                "/api/control/queries/read_plugin_observation",
                headers=headers,
                json={"plugin_id": PLUGIN, "storage_namespace": "private"},
            )
        ).status_code == 400


async def test_outbox_metadata_cursor_and_retry_use_original_send_certainty(database, plugin):
    resolver, uow, bot = await ingress(database)
    admitted = await resolver.pre_admit(
        bot, _message(message_id="outbox-source", group_id="2001", text="private inbound")
    )
    stored = await uow.append_inbound(admitted.message, admitted)
    now = datetime.now(UTC)
    async with database.sessions() as session, session.begin():
        for index, (status, receipt, category, attempts) in enumerate(
            [
                ("failed", None, "gateway_disconnected", 1),
                ("uncertain", None, "gateway_disconnected", 1),
                ("failed", "private-platform-id", "gateway_disconnected", 1),
                ("failed", None, "RouteSendError", 1),
                ("failed", None, "bot_unavailable", 5),
            ]
        ):
            session.add(
                PluginNotificationOutboxModel(
                    notification_id=f"notification-{index}",
                    part_key="text",
                    source_event_id=stored.event.id,
                    plugin_id=PLUGIN,
                    target_type="group",
                    target_id="2001",
                    bot_user_id="8000",
                    part_type="text",
                    text="private outbox content",
                    status=status,
                    attempts=attempts,
                    max_attempts=5,
                    next_attempt_at=now,
                    platform_message_id=receipt,
                    last_error_category=category,
                    created_at=now,
                    updated_at=now,
                    canonical_target_space_id=admitted.space_id,
                    canonical_conversation_id=admitted.conversation_id,
                    canonical_presence_id=admitted.presence_id,
                )
            )
    queries = ControlQueryService(ControlQueryAdapter(database))
    ctx = context("control.plugin.read", "control.plugin.mutate")
    bad_cursor = encode_query_cursor(
        QueryResourceKind.PLUGIN_OUTBOX,
        QueryCursorPhase.CANONICAL,
        hashlib.sha256(PLUGIN.encode()).hexdigest()[:32] + ":9223372036854775808",
    )
    with pytest.raises(ControlQueryError) as exc:
        await queries.list_plugin_outbox(ctx, PageRequest(cursor=bad_cursor), plugin_id=PLUGIN)
    assert exc.value.problem.code is ProblemCode.VALIDATION_ERROR
    statements = []

    def capture(_conn, _cursor, stmt, *_):
        statements.append(stmt)

    event.listen(database.engine.sync_engine, "before_cursor_execute", capture)
    try:
        page = await queries.list_plugin_outbox(ctx, PageRequest(limit=2), plugin_id=PLUGIN)
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", capture)
    sql = next(stmt for stmt in statements if "FROM plugin_notification_outbox" in stmt)
    assert (
        "plugin_notification_outbox.text" not in sql
        and "plugin_notification_outbox.media_handle_id" not in sql
    )
    assert "private" not in str(page)
    with pytest.raises(ControlQueryError):
        await queries.list_plugin_outbox(
            ctx, PageRequest(limit=2, cursor=page.next_cursor), plugin_id="another"
        )
    all_rows = (await queries.list_plugin_outbox(ctx, PageRequest(), plugin_id=PLUGIN)).items
    assert sum(row.fields["can_retry"] for row in all_rows) == 1
    commands = ControlCommandService(ControlCommandAdapter(database))
    for row in all_rows:
        cmd_ctx = replace(ctx, request_id=type(ctx.request_id).new())
        command = ControlCommand(
            request_id=cmd_ctx.request_id,
            expected_revision=row.fields["revision"],
            payload={"resource_id": row.resource_id, "action": "retry"},
        )
        if row.fields["can_retry"]:
            result = await commands.mutate_plugin(cmd_ctx, command)
            assert result.success and result.effective_state["status"] == "pending"
            assert await commands.mutate_plugin(cmd_ctx, command) == result
        else:
            with pytest.raises(ControlCommandError) as exc:
                await commands.mutate_plugin(cmd_ctx, command)
            assert exc.value.problem.code is ProblemCode.PRECONDITION_FAILED


async def test_approval_snapshot_and_background_pages_use_original_rows(database, plugin):
    from tests.unit.test_webui_activity import _message, ingress

    from qq_ai_bot.plugin_host.db_models import PluginBackgroundTurnJobModel

    manager, _, _ = plugin
    queries = ControlQueryService(ControlQueryAdapter(database, plugins=manager))
    ctx = context("control.plugin.read")
    approval = await queries.read_plugin_approval(ctx, PLUGIN)
    assert approval.fields["manifest_hash_matches"]
    assert set(approval.fields["requested_permissions"]) == {
        "plugin.config.read",
        "storage.private",
    }
    assert approval.fields["revision"] > 0
    resolver, uow, bot = await ingress(database)
    for i in range(3):
        admitted = await resolver.pre_admit(
            bot, _message(message_id=f"background-{i}", text="private original")
        )
        appended = await uow.append_inbound(admitted.message, admitted)
        async with database.immediate_session() as session:
            session.add(
                PluginBackgroundTurnJobModel(
                    source_event_id=appended.event.id,
                    plugin_id=PLUGIN,
                    target_type="group",
                    target_id="private target",
                    bot_user_id="8000",
                    agent_intent="private intent",
                    generated_text="private output",
                    status="completed",
                    created_at=datetime.now(UTC),
                    updated_at=datetime.now(UTC),
                    next_attempt_at=datetime.now(UTC),
                )
            )
    statements = []

    def capture(_conn, _cursor, statement, *args):
        if statement.lstrip().startswith("SELECT"):
            statements.append(statement.lower().split("\nfrom ")[0])

    event.listen(database.engine.sync_engine, "before_cursor_execute", capture)
    try:
        first = await queries.list_plugin_background_turns(
            ctx, PageRequest(limit=2), plugin_id=PLUGIN
        )
        second = await queries.list_plugin_background_turns(
            ctx, PageRequest(limit=2, cursor=first.next_cursor), plugin_id=PLUGIN
        )
        assert len(first.items) == 2 and len(second.items) == 1 and second.next_cursor is None
        assert int(first.items[-1].resource_id) > int(second.items[0].resource_id)
        assert not any(
            name in "\n".join(statements)
            for name in ("agent_intent", "generated_text", ".target_id", "bot_user_id")
        )
        with pytest.raises(ControlQueryError):
            await queries.list_plugin_background_turns(
                ctx, PageRequest(cursor=first.next_cursor), plugin_id="other.plugin"
            )
        denied_before = len(statements)
        with pytest.raises(ControlQueryError) as denied:
            await queries.list_plugin_background_turns(context(), PageRequest(), plugin_id=PLUGIN)
        assert denied.value.problem.code is ProblemCode.CAPABILITY_DENIED
        assert len(statements) == denied_before
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", capture)
