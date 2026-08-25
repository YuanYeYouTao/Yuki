"""C23a: Plugin API 2.0-compatible canonical invocation/SDK projections."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import select, text
from tests.support.gateway import napcat_registry

from qq_ai_bot.automation.authority import (
    AuthorityContext,
    DelegatedAuthority,
    PermissionLevel,
)
from qq_ai_bot.automation.models import AutomationContext, TurnOrigin
from qq_ai_bot.automation.registry import CapabilityExecutionContext
from qq_ai_bot.conversation.canonical_db_models import (
    CanonicalConversationModel,
    ConversationLegacyAliasModel,
)
from qq_ai_bot.conversation.hydrate import require_primary_alias_for_conversation
from qq_ai_bot.conversation.scope import plugin_conversation_key
from qq_ai_bot.domain.conversations import ConversationScope, ScopeType
from qq_ai_bot.domain.identity import AuthorKind
from qq_ai_bot.domain.messages import InboundMessage, SenderIdentity
from qq_ai_bot.identity.db_models import IdentityRuntimeStateModel
from qq_ai_bot.identity.dual_write import ensure_canonical_person_preconfig
from qq_ai_bot.identity.dual_write import (
    ensure_canonical_presence_preconfig as ensure_v2_presence,
)
from qq_ai_bot.identity.errors import IdentityDualWriteError
from qq_ai_bot.identity.ingress import CanonicalIngressResolver, _ensure_person_id
from qq_ai_bot.identity.routing import PresenceRouter
from qq_ai_bot.identity.write_settings import (
    IdentityWriteSettings,
    configure_identity_write_settings,
)
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.repository_records import EventRecord
from qq_ai_bot.plugin_host.admission_adapter import PluginAdmissionSignalAdapter
from qq_ai_bot.plugin_host.automation_adapter import _automation_invocation
from qq_ai_bot.plugin_host.canonical_projection import (
    projection_from_automation,
    projection_from_event,
    projection_from_existing_conversation,
    projection_from_inbound,
)
from qq_ai_bot.plugin_host.extension_registry import ExtensionRegistry
from qq_ai_bot.plugin_host.facades import PluginInvocation, _current_message
from yuki_plugin_sdk.api import PLUGIN_API_VERSION, is_api_compatible
from yuki_plugin_sdk.models import (
    AdmissionSignal,
    AdmissionSignalContext,
    CanonicalIdentityProjection,
    CurrentMessage,
    PublishNotificationRequest,
)
from yuki_plugin_sdk.permissions import PluginPermission
from yuki_plugin_sdk.registrar import AdmissionSignalRegistration
from yuki_plugin_sdk.sessions import CreateAgentSessionRequest

_NOW = datetime(2026, 8, 25, tzinfo=UTC)
_CUTOVER = "550e8400-e29b-41d4-a716-446655440099"
_V1_CURRENT = {
    "message_id": "m-1",
    "sender_user_id": "1001",
    "scope_type": "private",
    "group_id": None,
    "text": "hello",
    "mentioned_user_ids": [],
    "received_at": "2026-08-01T00:00:00Z",
}


@dataclass
class _Bot:
    self_id: str

    async def call_api(self, *_args: object, **_kwargs: object) -> dict[str, object]:
        return {}


async def _true(*_args: object, **_kwargs: object) -> bool:
    return True


async def _flip_v2(database: Database) -> None:
    async with database.sessions() as session, session.begin():
        row = await session.get(IdentityRuntimeStateModel, 1)
        assert row is not None
        row.state = "v2"
        row.cutover_id = _CUTOVER
        row.source_fingerprint = "cutover-fingerprint"
        row.completed_at = _NOW


def _private(message_id: str, user_id: str = "1001", bot_user_id: str = "8000") -> InboundMessage:
    return InboundMessage(
        message_id=message_id,
        event_type="message:test",
        scope_type=ScopeType.PRIVATE,
        sender=SenderIdentity(user_id=user_id),
        text="hi",
        bot_user_id=bot_user_id,
    )


def test_plugin_api_stays_2_0_and_v1_golden_payloads_parse() -> None:
    assert PLUGIN_API_VERSION == "2.0"
    assert is_api_compatible("2.0")
    assert is_api_compatible("2.9")
    assert is_api_compatible("3.0") is False
    current = CurrentMessage.model_validate(_V1_CURRENT)
    assert current.person_id is None
    assert current.space_id is None
    assert current.conversation_id is None
    assert current.presence_id is None
    context = AdmissionSignalContext.model_validate(
        {
            "conversation_key": "private:8000:1001",
            "origin": "user_message",
            "current": _V1_CURRENT,
            "text_is_untrusted": True,
        }
    )
    assert context.conversation_key == "private:8000:1001"
    assert context.person_id is None
    assert isinstance(context, CanonicalIdentityProjection)
    with pytest.raises(ValidationError):
        CurrentMessage.model_validate({**_V1_CURRENT, "unknown_field": "x"})


def test_plugin_request_dtos_cannot_assert_canonical_identity() -> None:
    with pytest.raises(ValidationError):
        CreateAgentSessionRequest.model_validate(
            {
                "name": "session",
                "instructions": "do work",
                "person_id": str(uuid4()),
            }
        )
    with pytest.raises(ValidationError):
        PublishNotificationRequest.model_validate(
            {
                "event_key": "k1",
                "event_type": "push",
                "external_source": "src",
                "target": {"target_type": "private", "target_id": "1001"},
                "occurred_at": "2026-08-01T00:00:00Z",
                "summary": "hello",
                "conversation_id": str(uuid4()),
            }
        )
    with pytest.raises(ValidationError):
        AdmissionSignal.model_validate(
            {
                "source_plugin_id": "com.example.x",
                "score_delta": 1,
                "reason_code": "ok",
                "summary": "ok",
                "confidence": 0.5,
                "presence_id": str(uuid4()),
            }
        )
    assert PluginPermission.MESSAGE_CURRENT_READ.value == "message.current.read"


def test_inbound_projection_overrides_caller_supplied_identity() -> None:
    inbound = InboundMessage(
        message_id="cmd-1",
        event_type="message:test",
        scope_type=ScopeType.PRIVATE,
        sender=SenderIdentity(user_id="1001"),
        text="/ai plugin run demo ping",
        bot_user_id="8001",
        legacy_conversation_key="bot:8000:private:1001",
        person_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        conversation_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        presence_id="cccccccc-cccc-4ccc-8ccc-cccccccccccc",
    )
    invocation = PluginInvocation(
        plugin_id="demo.plugin",
        origin=TurnOrigin.USER_MESSAGE,
        actor_user_id="1001",
        bot_user_id="8001",
        inbound=inbound,
        person_id="spoofed-person",
        space_id="spoofed-space",
        conversation_id="spoofed-conversation",
        presence_id="spoofed-presence",
        legacy_conversation_key="private:8001:1001",
    )
    assert invocation.person_id == inbound.person_id
    assert invocation.space_id is None
    assert invocation.conversation_id == inbound.conversation_id
    assert invocation.presence_id == inbound.presence_id
    assert invocation.conversation_key == "bot:8000:private:1001"
    current = _current_message(inbound)
    assert current is not None
    assert current.person_id == inbound.person_id
    assert current.conversation_id == inbound.conversation_id
    assert current.presence_id == inbound.presence_id
    identity = ConversationScope.private("8001", "1001")
    assert plugin_conversation_key(inbound, identity) == "bot:8000:private:1001"


def test_v1_unstamped_inbound_keeps_stable_key_and_none_ids() -> None:
    inbound = InboundMessage(
        message_id="v1",
        event_type="message:test",
        scope_type=ScopeType.PRIVATE,
        sender=SenderIdentity(user_id="1001"),
        text="hi",
        bot_user_id="8001",
    )
    identity = ConversationScope.private("8001", "1001")
    assert plugin_conversation_key(inbound, identity) == identity.key
    projection = projection_from_inbound(inbound)
    assert projection.sdk_fields() == {
        "person_id": None,
        "space_id": None,
        "conversation_id": None,
        "presence_id": None,
    }
    assert projection.conversation_key is None


def test_external_bot_event_never_projects_person() -> None:
    record = EventRecord(
        id=7,
        bot_user_id="8000",
        platform_message_id="ext-1",
        scope_type=ScopeType.GROUP,
        sender_user_id="7001",
        direction="inbound",
        content="bot says hi",
        visual_summary="",
        segments=(),
        occurred_at=_NOW,
        group_id="2001",
        author_kind=AuthorKind.EXTERNAL_BOT.value,
        author_person_id="should-not-leak",
        canonical_conversation_id="dddddddd-dddd-4ddd-8ddd-dddddddddddd",
        ingress_presence_id="eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
    )
    projection = projection_from_event(record)
    assert projection.person_id is None
    assert projection.conversation_id == record.canonical_conversation_id
    assert projection.presence_id == record.ingress_presence_id


def test_automation_without_conversation_keeps_stable_key() -> None:
    projection = projection_from_automation(
        conversation_key="automation:11",
        conversation_id=None,
        person_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        space_id=None,
    )
    assert projection.conversation_id is None
    assert projection.person_id is None
    assert projection.space_id is None
    assert projection.presence_id is None
    assert projection.conversation_key == "automation:11"
    context = CapabilityExecutionContext(
        authority=AuthorityContext(
            origin=TurnOrigin.SCHEDULED_AUTOMATION,
            actor_user_id="1001",
            actor_is_superuser=True,
            bot_user_id="8000",
            delegated_authority=DelegatedAuthority(
                creator_user_id="1001",
                bot_user_id="8000",
                created_from_message_id="create-message",
                created_at=_NOW.isoformat(),
                permission_level=PermissionLevel.USER,
                granted_capabilities=("plugin.demo.inspect",),
                capability_schema_versions={"plugin.demo.inspect": 1},
                current_group_id=None,
            ),
            allowed_capabilities=frozenset({"plugin.demo.inspect"}),
        ),
        automation_id=11,
        automation_run_id=12,
        step_id="inspect",
        creator_user_id="1001",
        bot_user_id="8000",
        current_group_id=None,
        scheduled_for=_NOW,
        actual_started_at=_NOW,
        local_time=_NOW,
        timezone="Asia/Shanghai",
        automation_context=AutomationContext(scene="creator_private"),
        conversation_key="person:aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        canonical_target_person_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        canonical_conversation_id=None,
    )
    invocation = _automation_invocation("demo.plugin", context)
    assert invocation.conversation_key == context.conversation_key
    assert invocation.conversation_id is None
    assert invocation.person_id is None
    assert invocation.presence_id is None


def test_automation_with_conversation_projects_primary_key() -> None:
    conversation_id = "ffffffff-ffff-4fff-8fff-ffffffffffff"
    context = CapabilityExecutionContext(
        authority=AuthorityContext(
            origin=TurnOrigin.SCHEDULED_AUTOMATION,
            actor_user_id="1001",
            actor_is_superuser=True,
            bot_user_id="8000",
            delegated_authority=DelegatedAuthority(
                creator_user_id="1001",
                bot_user_id="8000",
                created_from_message_id="create-message",
                created_at=_NOW.isoformat(),
                permission_level=PermissionLevel.USER,
                granted_capabilities=("plugin.demo.inspect",),
                capability_schema_versions={"plugin.demo.inspect": 1},
                current_group_id=None,
            ),
            allowed_capabilities=frozenset({"plugin.demo.inspect"}),
        ),
        automation_id=11,
        automation_run_id=12,
        step_id="inspect",
        creator_user_id="1001",
        bot_user_id="8000",
        current_group_id=None,
        scheduled_for=_NOW,
        actual_started_at=_NOW,
        local_time=_NOW,
        timezone="Asia/Shanghai",
        automation_context=AutomationContext(scene="creator_private"),
        conversation_key="bot:8000:private:1001",
        canonical_target_person_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        canonical_conversation_id=conversation_id,
    )
    invocation = _automation_invocation("demo.plugin", context)
    assert invocation.conversation_key == "bot:8000:private:1001"
    assert invocation.conversation_id == conversation_id
    assert invocation.person_id == "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    assert invocation.space_id is None


@pytest.mark.asyncio
async def test_missing_or_duplicate_primary_alias_fails_closed(database: Database) -> None:
    missing_id = str(uuid4())
    async with database.sessions() as session:
        with pytest.raises(IdentityDualWriteError) as missing:
            await require_primary_alias_for_conversation(session, missing_id)
        assert missing.value.category == "state_mismatch"

    configure_identity_write_settings(IdentityWriteSettings(superusers=frozenset({"9000"})))
    conversation_id = str(uuid4())
    async with database.sessions() as session, session.begin():
        person = await ensure_canonical_person_preconfig(session, "1001", now=_NOW)
        alias_id = str(uuid4())
        session.add(
            CanonicalConversationModel(
                id=conversation_id,
                kind="private",
                person_id=person,
                space_id=None,
                primary_alias_id=alias_id,
                primary_marker=1,
                generation=1,
                starts_after_event_id=0,
                last_event_id=0,
                last_generation_change_event_id=0,
                covered_through_event_id=0,
                uncovered_event_count=0,
                uncovered_character_count=0,
                revision=1,
                created_at=_NOW,
                updated_at=_NOW,
            )
        )
        session.add(
            ConversationLegacyAliasModel(
                id=alias_id,
                conversation_id=conversation_id,
                scope_key="bot:8000:private:1001",
                is_primary=1,
                created_at=_NOW,
                updated_at=_NOW,
            )
        )
    async with database.sessions() as session, session.begin():
        await session.execute(text("DROP INDEX IF EXISTS uq_conversation_legacy_aliases_primary"))
        session.add(
            ConversationLegacyAliasModel(
                id=str(uuid4()),
                conversation_id=conversation_id,
                scope_key="bot:8001:private:1001",
                is_primary=1,
                created_at=_NOW,
                updated_at=_NOW,
            )
        )
    async with database.sessions() as session:
        with pytest.raises(IdentityDualWriteError) as duplicate:
            await projection_from_existing_conversation(
                session,
                conversation_id=conversation_id,
                person_id="ignored-if-resolver-fails",
            )
        assert duplicate.value.category == "state_mismatch"
        primaries = list(
            await session.scalars(
                select(ConversationLegacyAliasModel.scope_key).where(
                    ConversationLegacyAliasModel.conversation_id == conversation_id,
                    ConversationLegacyAliasModel.is_primary == 1,
                )
            )
        )
        assert len(primaries) == 2


@pytest.mark.asyncio
async def test_presence_switch_keeps_sdk_conversation_key(database: Database) -> None:
    configure_identity_write_settings(IdentityWriteSettings(superusers=frozenset({"9000"})))
    await _flip_v2(database)
    registry = napcat_registry(gateway_instance_id="gw-c23a")
    router = PresenceRouter(database, registry, membership_probe=_true)
    resolver = CanonicalIngressResolver(database, registry, router)
    bot_a = _Bot("8000")
    bot_b = _Bot("8001")
    async with database.sessions() as session, session.begin():
        presence_a = await ensure_v2_presence(session, "8000")
        presence_b = await ensure_v2_presence(session, "8001")
        await _ensure_person_id(session, "1001")
    registry.connect(bot_a)
    registry.connect(bot_b)
    registry.bind_presence(platform="qq", external_account_id="8000", presence_id=presence_a)
    registry.bind_presence(platform="qq", external_account_id="8001", presence_id=presence_b)
    first = await resolver.pre_admit(bot_a, _private("c23a-1"))
    second = await resolver.pre_admit(bot_b, _private("c23a-2", bot_user_id="8001"))
    assert first is not None and second is not None
    assert first.conversation_id == second.conversation_id
    assert first.primary_alias == second.primary_alias
    assert first.presence_id == presence_a
    assert second.presence_id == presence_b
    assert first.presence_id != second.presence_id
    first_current = _current_message(first.message)
    second_current = _current_message(second.message)
    assert first_current is not None and second_current is not None
    assert first_current.conversation_id == second_current.conversation_id == first.conversation_id
    assert first_current.presence_id == presence_a
    assert second_current.presence_id == presence_b
    first_invocation = PluginInvocation(
        plugin_id="demo.plugin",
        origin=TurnOrigin.USER_MESSAGE,
        actor_user_id="1001",
        bot_user_id="8000",
        inbound=first.message,
    )
    second_invocation = PluginInvocation(
        plugin_id="demo.plugin",
        origin=TurnOrigin.USER_MESSAGE,
        actor_user_id="1001",
        bot_user_id="8001",
        inbound=second.message,
    )
    assert first_invocation.conversation_key == second_invocation.conversation_key
    assert first_invocation.conversation_key == first.primary_alias
    assert first_invocation.conversation_key != ConversationScope.private("8001", "1001").key
    assert first_invocation.conversation_id == second_invocation.conversation_id
    assert first_invocation.presence_id != second_invocation.presence_id
    received: list[AdmissionSignalContext] = []

    async def provider(context: AdmissionSignalContext) -> AdmissionSignal:
        received.append(context)
        return AdmissionSignal(
            source_plugin_id="com.example.context",
            score_delta=1,
            reason_code="ok",
            summary="ok",
            confidence=0.9,
        )

    from tests.unit.test_plugin_admission_adapter import _runtime

    registry_ext = ExtensionRegistry()
    registry_ext.registrar(
        "com.example.context",
        (PluginPermission.ADMISSION_SIGNAL_REGISTER,),
    ).register_admission_signal(AdmissionSignalRegistration(name="context", provider=provider))
    adapter = PluginAdmissionSignalAdapter(registry_ext)
    await adapter.collect(
        message=first.message,
        origin=TurnOrigin.USER_MESSAGE,
        runtime=_runtime(),
    )
    await adapter.collect(
        message=second.message,
        origin=TurnOrigin.USER_MESSAGE,
        runtime=_runtime(),
    )
    assert len(received) == 2
    assert received[0].conversation_key == received[1].conversation_key == first.primary_alias
    assert received[0].conversation_id == received[1].conversation_id
    assert received[0].presence_id != received[1].presence_id


@pytest.mark.asyncio
async def test_external_bot_private_ingress_has_no_person(database: Database) -> None:
    configure_identity_write_settings(
        IdentityWriteSettings(superusers=frozenset({"9000"}), ignored_bot_users=frozenset({"7001"}))
    )
    await _flip_v2(database)
    registry = napcat_registry(gateway_instance_id="gw-ext-bot")
    router = PresenceRouter(database, registry, membership_probe=_true)
    resolver = CanonicalIngressResolver(database, registry, router)
    bot = _Bot("8000")
    async with database.sessions() as session, session.begin():
        presence = await ensure_v2_presence(session, "8000")
    registry.connect(bot)
    registry.bind_presence(platform="qq", external_account_id="8000", presence_id=presence)
    admitted = await resolver.pre_admit(
        bot,
        InboundMessage(
            message_id="ext-bot",
            event_type="message:test",
            scope_type=ScopeType.PRIVATE,
            sender=SenderIdentity(user_id="7001", is_bot=True),
            text="hi",
            bot_user_id="8000",
        ),
    )
    assert admitted is not None
    assert admitted.dropped is False
    assert admitted.person_id is None
    assert admitted.conversation_id is None
    assert admitted.author_kind == AuthorKind.EXTERNAL_BOT.value
    current = _current_message(admitted.message)
    assert current is not None
    assert current.person_id is None
    assert current.conversation_id is None
    invocation = PluginInvocation(
        plugin_id="demo.plugin",
        origin=TurnOrigin.USER_MESSAGE,
        actor_user_id="7001",
        bot_user_id="8000",
        inbound=admitted.message,
    )
    assert invocation.conversation_key == ConversationScope.private("8000", "7001").key
    assert invocation.person_id is None
    assert invocation.conversation_id is None
