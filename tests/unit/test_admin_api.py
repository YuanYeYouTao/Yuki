"""Management through real ASGI, SQLite, plugins and fake model connections."""

import gzip
import hashlib
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import event, func, select
from tests.conftest import make_settings
from tests.integration.test_plugin_frozen_binding import write_plugin
from tests.support.canonical_ingress import fixture_ingress
from tests.support.client_identity import client_identity_service
from tests.support.model_profiles import write_fake_profiles
from tests.unit.test_admin_operator_access import operator_file

from yuki import __version__
from yuki.admin.authority.operators import ControlOperatorAccess
from yuki.admin.config_files import ConfigFileService
from yuki.admin.config_service import RuntimeConfigService
from yuki.admin.operations import AdminOperations
from yuki.admin.queries import AdminQueries
from yuki.admin_api.http import AdminApi, attach_admin_api
from yuki.conversation.canonical_db_models import ControlCommandReceiptModel
from yuki.execution_trace.db_models import ExecutionTraceEntryModel
from yuki.identity.db_models import CanonicalPersonModel, IdentityBindingModel, PresenceModel
from yuki.memory.rebuild.repository import MemoryRebuildRepository
from yuki.model_runtime.executor import TaskModelExecutor
from yuki.model_runtime.pool import ModelClientPool
from yuki.model_runtime.profiles import load_model_profile_catalog, model_profile_environment
from yuki.model_runtime.routes import ModelRouter
from yuki.persistence.people_repository import PeopleRepository
from yuki.persistence.repositories import GroupSettingsRepository
from yuki.plugin_host.discovery import PluginDiscovery
from yuki.plugin_host.event_bus import PluginEventBus
from yuki.plugin_host.extension_registry import ExtensionRegistry
from yuki.plugin_host.loader import PluginLoader
from yuki.plugin_host.manager import PluginManager
from yuki.plugin_host.repository import PluginInstallationRepository
from yuki_plugin_sdk.api import PLUGIN_API_VERSION

TOKEN = "test-admin-" + "a" * 48
READER = "test-reader-" + "b" * 48
GRANTS = (
    "control.health.read",
    "control.operation.read",
    "identity.person.create",
    "identity.person.read",
    "identity.person.disable",
    "identity.person.enable",
    "identity.person.superuser",
    "identity.binding.enable",
    "identity.presence.read",
    "identity.presence.set_ingest",
    "control.work.read",
    "identity.binding.attach",
    "identity.binding.read",
    "identity.binding.disable",
    "identity.binding.transfer",
    "identity.binding.unbind",
    "identity.presence.register",
    "identity.presence.unregister",
    "identity.presence.transfer",
    "identity.client.pair",
    "identity.client.revoke",
    "identity.person.forget",
    "identity.person.merge",
    "identity.person.rename",
    "control.plugin.read",
    "control.plugin.mutate",
    "control.plugin.config.content.read",
    "control.plugin.config.mutate",
    "control.config.file.content.read",
    "control.config.file.mutate",
    "control.work.mutate",
    "control.execution.metadata.read",
    "control.execution.content.read",
)


def command(revision=0, **fields):
    return {"request_id": str(uuid4()), "expected_revision": revision, **fields}


@pytest.fixture
async def admin(database, tmp_path, monkeypatch):
    monkeypatch.setenv("YUKI_TEST_OPERATOR_TOKEN", TOKEN)
    monkeypatch.setenv("YUKI_READER_TOKEN", READER)
    policy, _ = operator_file(tmp_path, capabilities=GRANTS)
    policy.write_text(
        policy.read_text()
        + f'''\n[[operators]]
principal_id = "{uuid4()}"
token_env = "YUKI_READER_TOKEN"
capabilities = ["control.health.read", "control.execution.metadata.read"]
'''
    )
    profiles = write_fake_profiles(tmp_path / "models.toml")
    settings = make_settings(database.url, model_profiles_file=profiles)
    catalog = load_model_profile_catalog(profiles, environment=model_profile_environment(settings))
    models = TaskModelExecutor(router=ModelRouter(catalog), pool=ModelClientPool())
    files = ConfigFileService(settings, catalog, model_executor=models)
    root = tmp_path / "plugins"
    write_plugin(root / "test.contract", 1, "READ", "value")
    manager = PluginManager(
        enabled=True,
        discovery=PluginDiscovery(root, yuki_version=__version__),
        installations=PluginInstallationRepository(database),
        loader=PluginLoader(),
        extensions=ExtensionRegistry(),
        event_bus=PluginEventBus(),
        context_factory=lambda *_: SimpleNamespace(calls=[]),
    )
    await manager.start()

    async def health():
        return [{"name": "fixture", "enabled": True, "running": True, "healthy": None}]

    identities = client_identity_service(database, fixture_ingress(database)._router)
    people = PeopleRepository(database, memory_rebuilds=MemoryRebuildRepository(database))
    api = AdminApi(
        ControlOperatorAccess(database, policy),
        AdminOperations(
            database,
            manager,
            files,
            identities=identities,
            people=people,
            config=RuntimeConfigService(settings=settings, database=database),
            groups=GroupSettingsRepository(database),
            router=fixture_ingress(database)._router,
        ),
        identities,
        AdminQueries(database, manager, files, health),
    )
    app = FastAPI()

    @app.get("/healthz")
    async def public_health():
        return {"ok": True}

    attach_admin_api(app, lambda: api)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": f"Bearer {TOKEN}"},
        ) as client:
            yield client, api, manager, models, profiles
    finally:
        await manager.stop()
        await models.close()


async def test_operator_permissions_rotation_and_secret_validation(admin, database, monkeypatch):
    client, *_ = admin
    assert (await client.get("/healthz", headers={"Authorization": ""})).status_code == 200
    assert (await client.get("/admin/v1/health", headers={"Authorization": ""})).status_code == 401
    assert (await client.get("/admin/v1/health")).json()["data"][0]["healthy"] is None
    denied = await client.post(
        "/admin/v1/persons", json=command(), headers={"Authorization": f"Bearer {READER}"}
    )
    assert denied.status_code == 403
    secret = "fixture-secret-do-not-echo"
    invalid = await client.post(
        "/admin/v1/models/secrets", json=command(name="KEY", value=secret, person_id="forged")
    )
    assert invalid.status_code == 422 and secret not in invalid.text
    async with database.read_session() as session:
        assert await session.scalar(select(func.count(ControlCommandReceiptModel.id))) == 0
    monkeypatch.setenv("YUKI_TEST_OPERATOR_TOKEN", "rotated-" + "c" * 48)
    assert (await client.get("/admin/v1/health")).status_code == 401


async def test_character_owner_grant_endpoints_are_retired(admin):
    client, *_ = admin
    assert (await client.get("/admin/v1/character/owners")).status_code == 404
    assert (
        await client.post(
            f"/admin/v1/character/owners/{uuid4()}/grant",
            json={"request_id": str(uuid4())},
        )
    ).status_code == 404


async def test_identity_creation_binding_attach_disable_and_original_replay(admin, database):
    client, *_ = admin
    async with database.read_session() as session:
        initial_persons = await session.scalar(select(func.count(CanonicalPersonModel.id)))
        initial_bindings = await session.scalar(select(func.count(IdentityBindingModel.id)))
    original = command()
    first = await client.post("/admin/v1/persons", json=original)
    assert first.status_code == 200, first.text
    identity = first.json()["data"]["resource_id"]
    assert (await client.post("/admin/v1/persons", json=original)).json() == first.json()
    conflict = await client.post("/admin/v1/persons", json={**original, "expected_revision": 1})
    assert (
        conflict.status_code == 409 and conflict.json()["problem"]["code"] == "idempotency_conflict"
    )
    binding_request = command(
        1, platform="telegram", external_account_id="opaque:account", display_name="Same name"
    )
    bound = await client.post(f"/admin/v1/persons/{identity}/bindings", json=binding_request)
    assert bound.status_code == 200, bound.text
    binding_id = bound.json()["data"]["resource_id"]
    assert (
        await client.post(f"/admin/v1/persons/{identity}/bindings", json=binding_request)
    ).json() == bound.json()
    other = (await client.post("/admin/v1/persons", json=command())).json()["data"]["resource_id"]
    rejected = await client.post(
        f"/admin/v1/persons/{other}/bindings",
        json=command(1, platform="telegram", external_account_id="opaque:account"),
    )
    assert rejected.status_code == 409
    disabled = await client.post(f"/admin/v1/bindings/{binding_id}/disable", json=command(1))
    assert disabled.status_code == 200, disabled.text
    assert (await client.get(f"/admin/v1/bindings/{binding_id}")).json()["data"][
        "status"
    ] == "disabled"
    result = await client.post(f"/admin/v1/persons/{identity}/disable", json=command(2))
    assert result.status_code == 200, result.text
    assert (await client.get(f"/admin/v1/persons/{identity}")).json()["data"]["enabled"] is False
    request_result = await client.get(f"/admin/v1/operations/{original['request_id']}")
    assert (
        request_result.status_code == 200
        and request_result.json()["data"]["result_resource_id"] == identity
    )
    async with database.read_session() as session:
        assert (
            await session.scalar(select(func.count(CanonicalPersonModel.id))) == initial_persons + 2
        )
        assert (
            await session.scalar(select(func.count(IdentityBindingModel.id)))
            == initial_bindings + 1
        )


@pytest.mark.parametrize("status", ["active", "disabled", "ended"])
async def test_binding_attach_only_conflicts_with_current_presence(admin, database, status):
    client, *_ = admin
    person = (await client.post("/admin/v1/persons", json=command())).json()["data"]["resource_id"]
    async with database.immediate_session() as session:
        presence = await session.scalar(
            select(PresenceModel).where(
                PresenceModel.platform == "qq", PresenceModel.external_account_id == "8001"
            )
        )
        assert presence is not None
        presence_id = presence.id
        presence.status = status
    response = await client.post(
        f"/admin/v1/persons/{person}/bindings",
        json=command(1, platform="qq", external_account_id="8001", display_name="Former account"),
    )
    if status != "ended":
        assert response.status_code == 409, response.text
        assert response.json()["problem"]["code"] == "binding_ambiguous"
    else:
        assert response.status_code == 200, response.text
        binding_id = response.json()["data"]["resource_id"]
        async with database.read_session() as session:
            binding = await session.get(IdentityBindingModel, binding_id)
            assert binding is not None and binding.person_id == person
    async with database.read_session() as session:
        retained = await session.get(PresenceModel, presence_id)
        assert retained is not None and retained.external_account_id == "8001"


async def plugin_action(client, action, **fields):
    if action == "discover":
        revision = 0
    else:
        revision = (await client.get("/admin/v1/plugins/test.contract")).json()["data"]["revision"]
    response = await client.post(
        f"/admin/v1/plugins/test.contract/actions/{action}", json=command(revision, **fields)
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


async def test_real_plugin_lifecycle_and_revision_conflict(admin):
    client, _, manager, *_ = admin
    await plugin_action(client, "discover")
    await plugin_action(client, "approve", permissions=["tool.register"])
    enabled = await plugin_action(client, "enable")
    assert enabled["success"] and manager.running_plugin_ids == ("test.contract",)
    doctor = await plugin_action(client, "doctor")
    assert doctor["effective_state"]["status"] == "healthy"
    stale = await client.post("/admin/v1/plugins/test.contract/actions/disable", json=command(1))
    assert stale.status_code == 409
    assert stale.json()["problem"]["code"] == "version_conflict"
    await plugin_action(client, "disable")
    assert manager.running_plugin_ids == ()


async def test_effect_acknowledgement_loss_is_queryable_and_never_redispatched(admin, monkeypatch):
    client, _, manager, *_ = admin
    await plugin_action(client, "approve", permissions=["tool.register"])
    shown = (await client.get("/admin/v1/plugins/test.contract")).json()["data"]
    original_enable = manager.enable
    dispatched = []

    async def lost(*args, **kwargs):
        result = await original_enable(*args, **kwargs)
        dispatched.append(result.plugin_id)
        raise RuntimeError("acknowledgement lost after effect")

    monkeypatch.setattr(manager, "enable", lost)
    body = command(shown["revision"])
    url = "/admin/v1/plugins/test.contract/actions/enable"
    original = await client.post(url, json=body)
    assert original.status_code == 200, original.text
    assert original.json()["data"]["operation"]["status"] == "unknown"
    assert (await client.post(url, json=body)).json() == original.json()
    assert (await client.post(url, json=command(shown["revision"]))).status_code == 409
    operation = original.json()["data"]["operation"]["operation_id"]
    queried = await client.get(f"/admin/v1/operations/{operation}")
    assert queried.json()["data"]["status"] == "unknown"
    assert dispatched == ["test.contract"] and manager.running_plugin_ids == ("test.contract",)


async def test_model_apply_uses_original_atomic_hot_reload_and_secret_store(admin):
    client, api, _, models, profiles = admin
    current = (await client.get("/admin/v1/models")).json()["data"]
    document = current["document"]
    document["profiles"]["main"]["model"] = "changed-fake-model"
    document["profiles"]["main"]["api_key_env"] = "TEST_MANAGED_MODEL_KEY"
    body = command(current["revision"], document=document)
    applied = await client.post("/admin/v1/models/apply", json=body)
    assert applied.status_code == 200, applied.text
    assert applied.json()["data"]["effective_state"]["status"] == "applied"
    assert api.commands.model.files.loaded_catalog.profiles["main"].model == "changed-fake-model"
    assert (await client.post("/admin/v1/models/apply", json=body)).json() == applied.json()
    secret = "synthetic-model-key-private"
    saved = await client.post(
        "/admin/v1/models/secrets",
        json=command(
            applied.json()["data"]["revision"], name="TEST_MANAGED_MODEL_KEY", value=secret
        ),
    )
    assert saved.status_code == 200, saved.text
    assert secret not in saved.text
    assert secret not in (await client.get("/admin/v1/models")).text
    assert secret not in profiles.read_text()
    assert (
        models._pool.api_key_for(api.commands.model.files.loaded_catalog.profiles["main"]) == secret
    )


async def test_trace_metadata_and_content_have_separate_permissions(admin, database):
    client, *_ = admin
    raw = json.dumps({"data": {"message": "private fixture content"}, "redactions": []}).encode()
    now = datetime.now(UTC)
    async with database.immediate_session() as session:
        session.add(
            ExecutionTraceEntryModel(
                turn_id="fixture-turn",
                operation_id=str(uuid4()),
                kind="turn_start",
                payload_status="recorded",
                payload_gzip=gzip.compress(raw),
                payload_sha256=hashlib.sha256(raw).hexdigest(),
                payload_bytes=len(raw),
                created_at=now,
                expires_at=now + timedelta(hours=1),
            )
        )
    url = "/admin/v1/traces/turn/fixture-turn"
    reader = {"Authorization": f"Bearer {READER}"}
    statements = []

    def capture(_connection, _cursor, statement, _parameters, _context, _many):
        if (
            statement.lstrip().upper().startswith("SELECT")
            and "execution_trace_entries" in statement
        ):
            statements.append(statement)

    event.listen(database.engine.sync_engine, "before_cursor_execute", capture)
    try:
        metadata = await client.get(url, headers=reader)
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", capture)
    assert metadata.status_code == 200 and "private fixture content" not in metadata.text
    assert statements and all("payload_gzip" not in statement for statement in statements)
    assert (await client.get(url, params={"content": True}, headers=reader)).status_code == 403
    content = await client.get(url, params={"content": True})
    assert content.status_code == 200 and "private fixture content" in content.text


async def test_plugin_configuration_and_observation_use_registered_domain_schema(admin):
    client, _, manager, _, profiles = admin
    root = profiles.parent / "plugins" / "test.config"
    root.mkdir()
    root.joinpath("plugin.toml").write_text(f"""id = "test.config"
name = "Configuration fixture"
description = "Synthetic configuration and observation fixture"
version = "1.0.0"
entrypoint = "configuration:Fixture"
plugin_api = "{PLUGIN_API_VERSION}"
yuki_requires = ">=3.8"
permissions = ["plugin.config.read"]
""")
    root.joinpath("configuration.py").write_text("""
from pydantic import BaseModel, ConfigDict
class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    greeting: str = "hello"
class Fixture:
    async def register(self, registrar):
        registrar.register_config_schema(Settings)
    async def start(self, context):
        pass
    async def stop(self):
        pass
    async def observe(self, context, request):
        return {"limit": request.limit, "items": []}
""")
    await manager.discover()
    target = "/admin/v1/plugins/test.config"
    revision = (await client.get(target)).json()["data"]["revision"]
    approved = await client.post(
        target + "/actions/approve", json=command(revision, permissions=["plugin.config.read"])
    )
    assert approved.status_code == 200, approved.text
    enabled = await client.post(
        target + "/actions/enable", json=command(approved.json()["data"]["revision"])
    )
    assert enabled.status_code == 200, enabled.text
    config = (await client.get(target + "/configuration")).json()["data"]
    assert config["values"] == {"greeting": "hello"}
    body = command(
        config["revision"], scope_type="global", owner_id=None, values={"greeting": "changed"}
    )
    saved = await client.post(target + "/configuration", json=body)
    assert saved.status_code == 200, saved.text
    assert (await client.post(target + "/configuration", json=body)).json() == saved.json()
    assert (await client.get(target + "/configuration")).json()["data"]["values"] == {
        "greeting": "changed"
    }
    invalid = await client.post(
        target + "/configuration",
        json=command(saved.json()["data"]["revision"], values={"extra": 1}),
    )
    assert invalid.status_code == 422
    observed = await client.get(target + "/observe", params={"limit": 5})
    assert observed.status_code == 200 and observed.json()["data"] == {"limit": 5, "items": []}


async def test_cli_client_uses_new_api_and_stops_on_unknown_result(admin, monkeypatch):
    from yuki.admin_api.client import mutate, plugin_mutation

    client, _, manager, *_ = admin
    approved = await plugin_mutation(
        client, "test.contract", "approve", permissions=["tool.register"]
    )
    assert approved["success"]
    original_enable = manager.enable
    dispatches = []

    async def lost(*args, **kwargs):
        await original_enable(*args, **kwargs)
        dispatches.append("effect")
        raise RuntimeError("lost confirmation")

    monkeypatch.setattr(manager, "enable", lost)
    request_id = str(uuid4())
    with pytest.raises(RuntimeError, match="request remains unconfirmed"):
        await mutate(
            client,
            "plugins/test.contract/actions/enable",
            approved["revision"],
            request_id=request_id,
        )
    assert dispatches == ["effect"]
    queried = await client.get(f"/admin/v1/operations/{request_id}")
    assert queried.json()["data"]["status"] == "unknown"


@pytest.mark.parametrize("status", ["failed", "sent", "uncertain"])
async def test_notification_retry_preserves_original_delivery_evidence(admin, database, status):
    from tests.unit.test_plugin_notification_idempotency import _install, _request

    from yuki.conversation.hydrate import ensure_canonical_conversation
    from yuki.identity.canonical_repository import find_identity_binding, find_presence
    from yuki.persistence.unit_of_work import state_revision
    from yuki.plugin_host.db_models import PluginNotificationOutboxModel
    from yuki_plugin_sdk.models import NotificationTarget

    client, *_ = admin
    async with database.immediate_session() as writer:
        binding = await find_identity_binding(writer, "1001", platform="qq")
        presence = await find_presence(writer, "8000", platform="qq")
        conversation = await ensure_canonical_conversation(
            writer,
            kind="private",
            primary_scope_key="bot:8000:private:1001",
            person_id=binding.person_id,
            identity_binding_id=binding.id,
            presence_id=presence.id,
        )
    target = NotificationTarget(
        target_type="private",
        target_id=binding.person_id,
        conversation_id=conversation.conversation_id,
    )
    repository = await _install(database, target=target, presence_id=presence.id)
    await repository.publish(
        plugin_id="test.publication-idempotency",
        request=_request(ask_agent=False, agent_intent="", target=target),
    )
    from yuki.gateway.social import TransportMessage, transport_payload

    (pending,) = await repository.unprepared_outbox()
    await repository.prepare_outbox(
        pending,
        (
            {
                "message": transport_payload(TransportMessage(pending.text)),
                "route": {
                    "platform": "qq",
                    "presence_id": presence.id,
                    "binding_id": binding.id,
                    "conversation_id": conversation.conversation_id,
                },
            },
        ),
    )
    claimed = await repository.claim_outbox()
    assert claimed is not None
    async with database.immediate_session() as session:
        row = await session.get(PluginNotificationOutboxModel, claimed.id)
        row.status = status
        row.last_error_category = "bot_unavailable"
        row.platform_message_id = "original-message" if status == "sent" else None
        current_revision = state_revision(row.updated_at)
    body = command(current_revision)
    response = await client.post(f"/admin/v1/plugin-outbox/{claimed.id}/retry", json=body)
    if status == "failed":
        assert response.status_code == 200, response.text
        assert (
            await client.post(f"/admin/v1/plugin-outbox/{claimed.id}/retry", json=body)
        ).json() == response.json()
    else:
        assert response.status_code == 409
    async with database.read_session() as session:
        row = await session.get(PluginNotificationOutboxModel, claimed.id)
        assert row.status == ("pending" if status == "failed" else status)
        assert row.attempts == claimed.attempts
        assert row.platform_message_id == ("original-message" if status == "sent" else None)


@pytest.mark.parametrize("action", ["cancel", "resume"])
async def test_work_management_keeps_original_work_and_replays_one_revision(
    admin, database, tmp_path, action
):
    from tests.support.social_identity_cases import social_env

    from yuki.conversation.canonical_db_models import CanonicalConversationModel
    from yuki.runtime.work_repository import WorkRepository

    client, *_ = admin
    env = await social_env(database, tmp_path)
    repository = WorkRepository(database)
    async with database.read_session() as session:
        generation = await session.scalar(
            select(CanonicalConversationModel.generation).where(
                CanonicalConversationModel.id == env.context.conversation_id
            )
        )
    lease = await repository.acquire(env.context.conversation_id, generation)
    assert lease is not None
    row = await repository.accept(
        lease,
        source_key="management-fixture",
        source={"origin": "user_message"},
        goal="retain original execution",
    )
    if action == "resume":
        await repository.transition(lease, row["id"], row["revision"], "failed")
        row = await repository.get(row["id"])
    body = command(row["revision"])
    url = f"/admin/v1/works/{row['id']}/{action}"
    changed = await client.post(url, json=body)
    assert changed.status_code == 200, changed.text
    assert (await client.post(url, json=body)).json() == changed.json()
    actual = await repository.get(row["id"])
    assert actual["id"] == row["id"]
    assert actual["revision"] == changed.json()["data"]["revision"]
    assert actual["model_requests"] == row["model_requests"]
    assert actual["state"] == ("cancelled" if action == "cancel" else "queued")


async def test_person_merge_and_names_use_real_operator_and_preserve_original_replay(admin):
    client, *_ = admin
    source = (await client.post("/admin/v1/persons", json=command(display_name="Source"))).json()[
        "data"
    ]["resource_id"]
    target = (await client.post("/admin/v1/persons", json=command(display_name="Target"))).json()[
        "data"
    ]["resource_id"]
    request = {"request_id": str(uuid4()), "to_person": target}
    denied = await client.post(
        f"/admin/v1/persons/{source}/merge",
        json=request,
        headers={"Authorization": f"Bearer {READER}"},
    )
    assert denied.status_code == 403
    merged = await client.post(f"/admin/v1/persons/{source}/merge", json=request)
    assert merged.status_code == 200, merged.text
    assert merged.json()["data"]["person_id"] == target
    renamed = await client.post(
        f"/admin/v1/persons/{source}/rename",
        json={
            "request_id": str(uuid4()),
            "display_name": "Chosen",
        },
    )
    assert renamed.status_code == 200, renamed.text
    current = (await client.get(f"/admin/v1/persons/{source}")).json()["data"]
    assert current["id"] == target and current["display_name"] == "Chosen"
    replay = await client.post(f"/admin/v1/persons/{source}/merge", json=request)
    assert replay.status_code == 200 and replay.json()["data"]["replay"]
    assert replay.json()["data"]["person_id"] == target
    unknown = await client.post(
        f"/admin/v1/persons/{uuid4()}/merge", json={**request, "request_id": str(uuid4())}
    )
    assert unknown.status_code == 404, unknown.text
