"""Known plugin results outlive optional audit failures; dispatch failures do not."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, text
from tests.support.social_identity_cases import social_env

from yuki.domain.messages import InboundMessage, SenderIdentity
from yuki.identity.db_models import CanonicalPersonModel
from yuki.persistence.models import ChatEventModel
from yuki.persistence.repositories import EventLedgerRepository
from yuki.plugin_host.audit import PluginAuditService
from yuki.plugin_host.capability_adapter import PluginCapabilityAdapter
from yuki.plugin_host.extension_registry import ExtensionKind, ExtensionRegistry
from yuki.plugin_host.facades import HostPluginContext, PluginFacadeServices, PluginInvocation
from yuki.plugin_host.repository import PluginAuditRepository, PluginInstallationRepository
from yuki.runtime.origin import TurnOrigin
from yuki.services.agent_tools import ToolRuntime
from yuki.social.db_models import SocialOperationModel
from yuki.social.repository import SocialOperationRepository
from yuki_plugin_sdk.api import PLUGIN_API_VERSION
from yuki_plugin_sdk.errors import PluginPermissionError
from yuki_plugin_sdk.models import RetryPolicy, RiskClass, StrictModel
from yuki_plugin_sdk.permissions import PluginPermission
from yuki_plugin_sdk.registrar import ToolMetadata, ToolRegistration
from yuki_plugin_sdk.results import ToolResult


class Arguments(StrictModel):
    pass


@pytest.fixture
async def plugin_tool(database):
    plugin = "test.known-audit"
    installations = PluginInstallationRepository(database)
    await installations.upsert_discovered(
        plugin_id=plugin,
        name="Known result",
        version="1",
        plugin_api=PLUGIN_API_VERSION,
        yuki_requires=">=3",
        manifest_hash="a" * 64,
        entrypoint="plugin:Plugin",
        requested_permissions=(PluginPermission.TOOL_REGISTER.value,),
    )
    await installations.approve(plugin)
    await installations.set_enabled(plugin, enabled=True)
    return plugin, installations


def adapter(plugin_tool, risk, handler, audit):
    plugin, installations = plugin_tool
    registry = ExtensionRegistry()
    registry.registrar(plugin, (PluginPermission.TOOL_REGISTER,)).register_tool(
        ToolRegistration(
            ToolMetadata(
                name="effect",
                description="Return the original effect result",
                risk=risk,
                retry_policy=RetryPolicy.TRANSIENT_ONCE,
            ),
            Arguments,
            ToolResult,
            handler,
        )
    )
    name = registry.list(kind=ExtensionKind.TOOL)[0].model_name
    return PluginCapabilityAdapter(
        registry=registry, installations=installations, audit=PluginAuditService(audit)
    ), name


@pytest.mark.parametrize(
    ("risk", "audit_offline"),
    [(RiskClass.READ, True), (RiskClass.MUTATE, True), (RiskClass.MUTATE, False)],
)
async def test_known_result_does_not_retry_or_become_unknown(plugin_tool, risk, audit_offline):
    handler = AsyncMock(
        return_value=ToolResult(
            data={"receipt": "original"}, mutation_committed=risk == RiskClass.MUTATE
        )
    )
    audit = SimpleNamespace(
        record=AsyncMock(side_effect=OSError("audit offline") if audit_offline else None)
    )
    tool, name = adapter(plugin_tool, risk, handler, audit)
    result = await tool.execute(name, "{}", ToolRuntime(inbound=None))
    handler.assert_awaited_once()
    assert result.ok and result.data == {"receipt": "original"}
    assert result.mutation_committed is (risk == RiskClass.MUTATE)
    assert not result.uncertain


async def test_actual_mutating_dispatch_failure_is_not_retried(plugin_tool):
    handler = AsyncMock(side_effect=OSError("transport confirmation lost"))
    audit = SimpleNamespace(record=AsyncMock())
    tool, name = adapter(plugin_tool, RiskClass.MUTATE, handler, audit)
    result = await tool.execute(name, "{}", ToolRuntime(inbound=None))
    handler.assert_awaited_once()
    assert not result.ok and result.uncertain
    assert result.error_code == "plugin_effect_unknown"


async def test_revoked_installation_does_not_dispatch(plugin_tool):
    handler = AsyncMock(return_value=ToolResult(data={"receipt": "must not run"}))
    tool, name = adapter(
        plugin_tool, RiskClass.MUTATE, handler, SimpleNamespace(record=AsyncMock())
    )
    plugin, installations = plugin_tool
    await installations.set_enabled(plugin, enabled=False)
    result = await tool.execute(name, "{}", ToolRuntime(inbound=None))
    handler.assert_not_awaited()
    assert not result.ok and result.error_code == "plugin_tool_denied"


async def test_optional_audit_does_not_swallow_cancellation():
    audit = PluginAuditService(
        SimpleNamespace(record=AsyncMock(side_effect=asyncio.CancelledError))
    )
    with pytest.raises(asyncio.CancelledError):
        await audit.record(
            plugin_id="test.cancelled-audit",
            actor_user_id=None,
            operation="original",
            permission=None,
            success=True,
        )


@pytest.fixture
async def onebot_call(database, tmp_path):
    scene = await social_env(database, tmp_path)
    plugin = "test.onebot-audit"
    installations = PluginInstallationRepository(database)
    await installations.upsert_discovered(
        plugin_id=plugin,
        name="OneBot audit",
        version="1",
        plugin_api=PLUGIN_API_VERSION,
        yuki_requires=">=3",
        manifest_hash="d" * 64,
        entrypoint="plugin:Plugin",
        requested_permissions=(PluginPermission.ONEBOT_MUTATE.value,),
    )
    await installations.approve(plugin)
    await installations.set_enabled(plugin, enabled=True)
    async with database.immediate_session() as session:
        event = await session.scalar(
            select(ChatEventModel).where(ChatEventModel.direction == "inbound")
        )
        (await session.get(CanonicalPersonModel, event.author_person_id)).is_superuser = True
    gateway = SimpleNamespace(call_api=AsyncMock(return_value={"status": "ok", "changed": True}))
    inbound = InboundMessage(
        message_id=event.platform_message_id,
        event_type="message",
        scope_type=event.scope_type,
        sender=SenderIdentity(event.sender_user_id),
        text=event.content,
        bot_user_id=event.bot_user_id,
        group_id=event.group_id,
        source_event_id=event.id,
        person_id=event.author_person_id,
        conversation_id=event.canonical_conversation_id,
        presence_id=event.ingress_presence_id,
        space_id=scene.space,
        platform="qq",
    )
    invocation = PluginInvocation(
        plugin_id=plugin,
        origin=TurnOrigin.USER_MESSAGE,
        actor_user_id=event.sender_user_id,
        bot_user_id=event.bot_user_id,
        inbound=inbound,
        gateway=gateway,
    )
    host = HostPluginContext(
        plugin_id=plugin,
        approved_permissions=(PluginPermission.ONEBOT_MUTATE,),
        services=PluginFacadeServices(
            ledger=EventLedgerRepository(database),
            audit=PluginAuditService(PluginAuditRepository(database)),
        ),
    )
    yield SimpleNamespace(host=host, invocation=invocation, gateway=gateway, event=event)
    await host.close_host_resources()


@pytest.mark.parametrize("audit_offline", [False, True])
async def test_onebot_known_ack_returns_original_result(database, onebot_call, audit_offline):
    env = onebot_call
    if audit_offline:
        async with database.immediate_session() as session:
            await session.execute(
                text(
                    "CREATE TRIGGER offline_audit_failure BEFORE INSERT ON plugin_audit_events "
                    "BEGIN SELECT RAISE(ABORT, 'offline-audit'); END"
                )
            )
    try:
        with env.host.bind(env.invocation):
            result = await env.host.onebot.call_mutating_action(
                "set_group_whole_ban", {"group_id": env.event.group_id, "enable": True}
            )
    finally:
        env.gateway.call_api.assert_awaited_once_with(
            "set_group_whole_ban", {"group_id": env.event.group_id, "enable": True}
        )
    assert result.ok and result.data == {"result": {"status": "ok", "changed": True}}


async def test_onebot_mutation_requires_real_administrator(database, onebot_call):
    env = onebot_call
    async with database.immediate_session() as session:
        (await session.get(CanonicalPersonModel, env.event.author_person_id)).is_superuser = False
    with env.host.bind(env.invocation), pytest.raises(PluginPermissionError, match="administrator"):
        await env.host.onebot.call_mutating_action(
            "set_group_whole_ban", {"group_id": env.event.group_id, "enable": True}
        )
    env.gateway.call_api.assert_not_awaited()


async def test_onebot_privileged_send_rechecks_role_after_writer_wait(
    database, onebot_call, monkeypatch
):
    env = onebot_call
    env.gateway.call_api.return_value = {"message_id": "must-not-send"}
    prepared = asyncio.Event()
    resume = asyncio.Event()
    queued = asyncio.Event()
    prepare = SocialOperationRepository.prepare
    claim = SocialOperationRepository.claim
    claims = []

    async def prepare_then_wait(repository, **kwargs):
        receipt = await prepare(repository, **kwargs)
        prepared.set()
        await resume.wait()
        return receipt

    async def record_claim(repository, *args, **kwargs):
        claims.append(args[0])
        return await claim(repository, *args, **kwargs)

    monkeypatch.setattr(SocialOperationRepository, "prepare", prepare_then_wait)
    monkeypatch.setattr(SocialOperationRepository, "claim", record_claim)

    async def send():
        with env.host.bind(env.invocation):
            return await env.host.onebot.call_mutating_action(
                "send_group_msg", {"group_id": env.event.group_id, "message": "original"}
            )

    task = asyncio.create_task(send())
    try:
        await asyncio.wait_for(prepared.wait(), timeout=5)
        writer = database.immediate_session

        @asynccontextmanager
        async def observe_claim_wait():
            if asyncio.current_task() is task:
                queued.set()
            async with writer() as session:
                yield session

        monkeypatch.setattr(database, "immediate_session", observe_claim_wait)
        async with writer() as session:
            person = await session.get(CanonicalPersonModel, env.event.author_person_id)
            person.is_superuser = False
            resume.set()
            await asyncio.wait_for(queued.wait(), timeout=5)
            assert not task.done()
            env.gateway.call_api.assert_not_awaited()
        with pytest.raises(PluginPermissionError, match="administrator"):
            await task
    finally:
        resume.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert claims == []
    env.gateway.call_api.assert_not_awaited()
    async with database.read_session() as session:
        receipts = (await session.scalars(select(SocialOperationModel))).all()
        outbound = await session.scalar(
            select(ChatEventModel.id).where(ChatEventModel.direction == "outbound")
        )
    assert len(receipts) == 1 and receipts[0].status == "prepared"
    assert outbound is None
