"""Administrator qualification belongs to a Person, never an account or old alias."""

from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import BaseModel, ConfigDict
from tests.conftest import make_settings
from tests.support.canonical_identity import ensure_person
from tests.support.client_identity import client_identity_service
from tests.support.person_merge import merge_persons
from tests.unit.test_memory_mutation import _context, _event, _service

from yuki.admin.audit import AdminAuditService
from yuki.admin.control_resolution import ControlAccess
from yuki.admin.models import AdminActor
from yuki.domain.conversations import ScopeType
from yuki.domain.messages import InboundMessage, SenderIdentity
from yuki.identity.authority import person_is_superuser
from yuki.identity.bootstrap import bootstrap_settings_identity
from yuki.identity.canonical_repository import (
    create_person_binding,
    find_identity_binding,
    forget_person_by_id,
)
from yuki.identity.db_models import CanonicalPersonModel
from yuki.memory.models import MemoryFactCreate
from yuki.memory.mutation.models import MemoryMutationOperation, MemoryMutationRequest
from yuki.memory.mutation.service import MemoryMutationRejected
from yuki.memory.subjects import ResolvedSubject
from yuki.persistence.models import MemoryFactModel, MemoryMutationReceiptModel
from yuki.plugin_host.capability_adapter import PluginCapabilityAdapter
from yuki.plugin_host.extension_registry import ExtensionRegistry
from yuki.plugin_host.facades import HostPluginContext, PluginFacadeServices, PluginInvocation
from yuki.plugin_host.repository import PluginInstallationRepository
from yuki.runtime.origin import TurnOrigin
from yuki.services.admin.memory_admin import MemoryAdminService
from yuki.services.agent_tools import ToolRuntime
from yuki_plugin_sdk import PLUGIN_API_VERSION
from yuki_plugin_sdk.models import PermissionLevel, RiskClass
from yuki_plugin_sdk.permissions import PluginPermission
from yuki_plugin_sdk.registrar import ToolMetadata, ToolRegistration

NOW = datetime(2026, 10, 18, tzinfo=UTC)


async def person(database, *, privileged=False, enabled=True):
    identity = str(uuid4())
    async with database.immediate_session() as session:
        session.add(
            CanonicalPersonModel(
                id=identity,
                is_superuser=privileged,
                enabled=enabled,
                created_at=NOW,
                updated_at=NOW,
            )
        )
    return identity


async def qualified(database, identity):
    async with database.read_session() as session:
        return await person_is_superuser(session, identity)


async def test_accountless_person_qualification_and_disable_are_independent(database):
    owner = await person(database, privileged=True)
    assert await qualified(database, owner)
    assert (await ControlAccess(database).principal_for_person(owner)).allows(
        "identity.person.superuser"
    )
    async with database.immediate_session() as session:
        row = await session.get(CanonicalPersonModel, owner)
        row.enabled = False
    assert not await qualified(database, owner)
    async with database.immediate_session() as session:
        row = await session.get(CanonicalPersonModel, owner)
        assert row.is_superuser
        row.enabled = True
    assert await qualified(database, owner)


async def test_transfer_and_binding_disable_never_move_qualification(database):
    async with database.immediate_session() as session:
        original = await ensure_person(session, "9000")
        (await session.get(CanonicalPersonModel, original)).is_superuser = True
        foreign = await create_person_binding(
            session, external_account_id="9000", platform="telegram", display_name="Other", now=NOW
        )
        other = foreign.person_id
    assert not await qualified(database, other)
    service = client_identity_service(database)
    async with database.immediate_session() as session:
        original_binding = await find_identity_binding(session, "9000")
        original_binding.status = "disabled"
    assert await qualified(database, original)
    async with database.immediate_session() as session:
        original_binding = await find_identity_binding(session, "9000")
        original_binding.status = "active"
        transferred = await service.transfer_binding(session, original_binding, other, now=NOW)
        assert transferred.person_id == other
    assert await qualified(database, original)
    assert not await qualified(database, other)


async def test_merge_preserves_paused_qualification_without_reviving_old_source(database):
    source = await person(database, privileged=True, enabled=False)
    target = await person(database)
    async with database.immediate_session() as session:
        await merge_persons(session, source, target, now=NOW)
    assert await qualified(database, target)
    assert not await qualified(database, source)
    async with database.immediate_session() as session:
        (await session.get(CanonicalPersonModel, target)).is_superuser = False
        original = await session.get(CanonicalPersonModel, source)
        assert original.is_superuser
        original.enabled = True
    # The retained original true is historical, not another live grant.
    assert not await qualified(database, source)
    assert not await qualified(database, target)


async def test_forget_then_same_account_is_new_unprivileged_person(database):
    async with database.immediate_session() as session:
        original = await ensure_person(session, "authority-test-account")
        (await session.get(CanonicalPersonModel, original)).is_superuser = True
    async with database.immediate_session() as session:
        await forget_person_by_id(session, original)
    async with database.immediate_session() as session:
        newcomer = await ensure_person(session, "authority-test-account")
    assert newcomer != original and not await qualified(database, newcomer)


async def test_old_environment_never_seeds_person_on_restart(database, monkeypatch):
    monkeypatch.setenv("SUPERUSERS", "unbound-old-admin-account")
    settings = make_settings(database.url)
    await bootstrap_settings_identity(database, settings)
    await bootstrap_settings_identity(database, settings)
    async with database.read_session() as session:
        assert await find_identity_binding(session, "unbound-old-admin-account") is None


async def test_revoked_snapshot_cannot_read_other_person_memory(database):
    service, facts, ledger, _ = _service(database)
    async with database.immediate_session() as session:
        owner = await ensure_person(session, "9000")
        target = await ensure_person(session, "1002")
        (await session.get(CanonicalPersonModel, owner)).is_superuser = True
    fact = await facts.remember(
        MemoryFactCreate(
            scope_type="person",
            subject_user_id="1002",
            canonical_subject_person_id=target,
            memory_key="authority-fixture",
            category="explicit",
            content="private",
            importance=5,
            source_type="explicit",
            authority="explicit",
        )
    )
    actor = AdminActor("9000", True, "", "private:9000", person_id=owner)
    admin = MemoryAdminService(
        settings=make_settings(database.url),
        memories=facts,
        audit=AdminAuditService(database),
        mutations=service,
        ledger=ledger,
    )
    assert (await admin.show_fact(actor, fact.id)).content == "private"
    async with database.immediate_session() as session:
        (await session.get(CanonicalPersonModel, owner)).is_superuser = False
    with pytest.raises(PermissionError):
        await admin.show_fact(actor, fact.id)


@pytest.mark.parametrize("consumer", ["admin", "plugin"])
async def test_memory_search_rechecks_role_before_disclosing_awaited_result(
    database, monkeypatch, consumer
):
    service, facts, ledger, _ = _service(database)
    async with database.immediate_session() as writer:
        owner = await ensure_person(writer, "9000")
        target = await ensure_person(writer, "1002")
    fact = await facts.remember(
        MemoryFactCreate(
            scope_type="person",
            subject_user_id="1002",
            canonical_subject_person_id=target,
            memory_key="authority-fixture",
            category="explicit",
            content="private memory",
            importance=5,
            source_type="explicit",
            authority="explicit",
        )
    )
    event = await _event(
        ledger, message_id="original-private-search", sender_user_id="9000", content="search memory"
    )
    admin = MemoryAdminService(
        settings=make_settings(database.url),
        memories=facts,
        audit=AdminAuditService(database),
        mutations=service,
        ledger=ledger,
    )
    search = admin._memory_context.search

    async def finish_after_revocation(**kwargs):
        result = await search(**kwargs)
        assert fact.id in {hit.fact.id for hit in result.hits}
        async with database.immediate_session() as writer:
            (await writer.get(CanonicalPersonModel, owner)).is_superuser = False
        return result

    monkeypatch.setattr(admin._memory_context, "search", finish_after_revocation)
    if consumer == "admin":
        actor = AdminActor("9000", True, "source", "private:9000", person_id=owner)
        with pytest.raises(PermissionError):
            await admin.search_person(actor, "1002", "private memory", person_id=target)
    else:
        host = HostPluginContext(
            plugin_id="authority.memory",
            approved_permissions=(PluginPermission.MEMORY_SEARCH,),
            services=PluginFacadeServices(
                ledger=ledger,
                memories=facts,
                memory_context=admin._memory_context,
                runtime_config=admin._runtime_config,
            ),
        )
        incoming = InboundMessage(
            "original-private-search",
            "message",
            ScopeType.PRIVATE,
            SenderIdentity("9000"),
            "search memory",
            bot_user_id=event.bot_user_id,
            source_event_id=event.id,
            person_id=owner,
            conversation_id=event.canonical_conversation_id,
            presence_id=event.ingress_presence_id,
        )
        invocation = PluginInvocation(
            host.plugin_id, TurnOrigin.PLUGIN_SESSION, "9000", event.bot_user_id, inbound=incoming
        )
        from yuki_plugin_sdk.errors import PluginPermissionError

        async with host.bind(invocation):
            with pytest.raises(PluginPermissionError):
                await host.memory.search("private memory", scope_type="person", subject_id=target)


async def test_memory_writer_rechecks_revoke_but_replays_original_success(database):
    service, facts, ledger, _ = _service(database)
    async with database.immediate_session() as session:
        owner = await ensure_person(session, "9000")
        target = await ensure_person(session, "1002")
        (await session.get(CanonicalPersonModel, owner)).is_superuser = True
    fact = await facts.remember(
        MemoryFactCreate(
            scope_type="person",
            subject_user_id="1002",
            canonical_subject_person_id=target,
            memory_key="authority-fixture",
            category="explicit",
            content="old",
            importance=5,
            source_type="explicit",
            authority="explicit",
        )
    )
    event = await _event(
        ledger, message_id="authority-admin-event", sender_user_id="9000", content="new fact"
    )
    context = replace(_context(event), actor_is_superuser=True, actor_person_id=owner)
    request = MemoryMutationRequest(
        operation=MemoryMutationOperation.INVALIDATE,
        fact_id=fact.id,
        reason="explicit administrator invalidation",
    )
    prepared = await service._prepare(request, context)
    async with database.immediate_session() as session:
        (await session.get(CanonicalPersonModel, owner)).is_superuser = False
    with pytest.raises(MemoryMutationRejected):
        await service._commit_prepared(prepared)
    assert (await facts.get_fact(fact.id)).content == "old"
    async with database.immediate_session() as session:
        (await session.get(CanonicalPersonModel, owner)).is_superuser = True
    committed = await service._commit_prepared(prepared)
    assert committed.ok
    async with database.immediate_session() as session:
        (await session.get(CanonicalPersonModel, owner)).is_superuser = False
    replayed = await service._commit_prepared(prepared)
    assert replayed.ok and replayed.deduplicated
    assert replayed.new_fact_id == committed.new_fact_id


@pytest.mark.parametrize(
    "operation", [MemoryMutationOperation.CORRECT, MemoryMutationOperation.UPDATE_METADATA]
)
async def test_memory_admin_original_event_replays_original_mutation(database, operation):
    from sqlalchemy import func, select

    service, facts, ledger, _ = _service(database)
    async with database.immediate_session() as writer:
        owner = await ensure_person(writer, "9000")
        (await writer.get(CanonicalPersonModel, owner)).is_superuser = True
    fact = await facts.remember(
        MemoryFactCreate(
            scope_type="person",
            subject_user_id="9000",
            canonical_subject_person_id=owner,
            memory_key="authority-fixture",
            category="explicit",
            content="old",
            importance=5,
            source_type="explicit",
            authority="explicit",
        )
    )
    event = await _event(
        ledger, message_id="stable-admin-memory", sender_user_id="9000", content="new memory"
    )
    actor = AdminActor(
        "9000",
        True,
        "transport-id",
        "private:9000",
        bot_user_id=event.bot_user_id,
        trigger_event_id=event.id,
        person_id=owner,
    )
    admin = MemoryAdminService(
        settings=make_settings(database.url),
        memories=facts,
        audit=AdminAuditService(database),
        mutations=service,
        ledger=ledger,
    )

    async def apply():
        return await admin._apply_mutation(
            actor,
            target=ResolvedSubject(fact.scope_type, "9000", None),
            operation=operation,
            fact_id=fact.id,
            new_content="new memory" if operation is MemoryMutationOperation.CORRECT else None,
            importance=4,
            reason="original command",
        )

    first = await apply()
    assert first.ok
    async with database.read_session() as reader:
        receipt_ids = tuple(await reader.scalars(select(MemoryMutationReceiptModel.mutation_id)))
        fact_count = await reader.scalar(select(func.count()).select_from(MemoryFactModel))
    repeated = await apply()
    assert repeated.ok and repeated.deduplicated
    assert repeated.mutation_id == first.mutation_id
    assert repeated.new_fact_id == first.new_fact_id
    async with database.read_session() as reader:
        assert (
            tuple(await reader.scalars(select(MemoryMutationReceiptModel.mutation_id)))
            == receipt_ids
        )
        assert await reader.scalar(select(func.count()).select_from(MemoryFactModel)) == fact_count


async def test_queued_privileged_plugin_rechecks_person_after_scope_wait(database):
    owner = await person(database, privileged=True)
    installations = PluginInstallationRepository(database)
    await installations.upsert_discovered(
        plugin_id="authority.plugin",
        name="Authority",
        version="1.0",
        plugin_api=PLUGIN_API_VERSION,
        yuki_requires=">=3.8",
        manifest_hash="a" * 64,
        entrypoint="plugin:Plugin",
        requested_permissions=("tool.register",),
    )
    await installations.approve("authority.plugin", permissions=("tool.register",))
    await installations.set_enabled("authority.plugin", enabled=True)
    effects = []

    class Arguments(BaseModel):
        model_config = ConfigDict(extra="forbid")

    async def handler(_arguments):
        effects.append("privileged effect")
        return {}

    registry = ExtensionRegistry()
    registry.registrar("authority.plugin", (PluginPermission.TOOL_REGISTER,)).register_tool(
        ToolRegistration(
            ToolMetadata(
                name="admin_effect",
                description="Isolated administrator effect",
                permission=PermissionLevel.SUPERUSER,
                risk=RiskClass.MUTATE,
            ),
            Arguments,
            Arguments,
            handler,
        )
    )

    @asynccontextmanager
    async def scope(*_args):
        async with database.immediate_session() as session:
            (await session.get(CanonicalPersonModel, owner)).is_superuser = False
        yield

    adapter = PluginCapabilityAdapter(
        registry=registry, installations=installations, invocation_scope=scope
    )
    runtime = ToolRuntime(
        inbound=InboundMessage(
            "source",
            "message",
            ScopeType.PRIVATE,
            SenderIdentity("opaque-account"),
            "request",
            bot_user_id="8000",
            person_id=owner,
        ),
        actor_is_superuser=True,
    )
    declaration = adapter.definitions(replace(runtime, declaration_only=True))[0]
    result = await adapter.execute(declaration.name, "{}", runtime)
    assert not result.ok and result.error_code == "plugin_tool_denied"
    assert not effects
    assert adapter.definitions(replace(runtime, declaration_only=True)) == (declaration,)
