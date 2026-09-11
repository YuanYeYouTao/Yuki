"""Safety invariants for canonical social effects."""

import asyncio
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from qq_ai_bot.capabilities.catalog import UnifiedToolCatalog, UnifiedToolCatalogEntry
from qq_ai_bot.capabilities.exposure import AuthorityFirstExposurePlanner
from qq_ai_bot.capabilities.models import CapabilityExposure, CapabilityTrustSource
from qq_ai_bot.capabilities.provider import ChatToolCapabilityProvider
from qq_ai_bot.conversation.hydrate import ensure_canonical_conversation
from qq_ai_bot.identity.canonical_repository import ensure_person, ensure_presence
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.runtime.origin import TurnOrigin
from qq_ai_bot.sandbox.client import sandbox_tools
from qq_ai_bot.social.automation import automation_name, register_social_automation
from qq_ai_bot.social.models import OperationStatus, SocialError, SocialTarget
from qq_ai_bot.social.repository import SocialOperationRepository
from qq_ai_bot.social.tools import social_tool_definitions
from qq_ai_bot.workspace.service import workspace_tools


@pytest.mark.asyncio
async def test_transfer_permission_failure_preserves_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from qq_ai_bot.social.transfer import ArtifactTransfer
    from qq_ai_bot.workspace.store import WorkspaceStore

    store = WorkspaceStore(tmp_path / "workspace")
    artifact = store.write("hello.txt", b"hello")
    transfer = ArtifactTransfer(store, tmp_path / "transfer", "/transfer")
    original = Path.mkdir

    def denied(path: Path, *args: Any, **kwargs: Any) -> None:
        if path == transfer.root:
            raise PermissionError("private path must not leak")
        original(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "mkdir", denied)
        with pytest.raises(SocialError, match=r"^artifact_transfer_unavailable$"):
            async with transfer.prepare(artifact["artifact_id"]):
                pytest.fail("must not invoke gateway")
    assert store.read(artifact["artifact_id"])["text"] == "hello"
    async with transfer.prepare(artifact["artifact_id"]):
        assert len(list(transfer.root.iterdir())) == 1
    assert list(transfer.root.iterdir()) == []


@pytest.mark.asyncio
async def test_social_receipt_claim_replay_and_interrupted_delivery(database: Database) -> None:
    definitions = (*social_tool_definitions(), *workspace_tools(), *sandbox_tools())
    assert definitions == (*social_tool_definitions(), *workspace_tools(), *sandbox_tools())
    descriptors = ChatToolCapabilityProvider(
        definitions, source=CapabilityTrustSource.CORE
    ).descriptors()
    assert len(descriptors) == 14
    assert all(
        descriptor.exposure is CapabilityExposure.DIRECT_ALWAYS for descriptor in descriptors
    )
    assert all(not descriptor.required_permissions for descriptor in descriptors)
    assert all(
        descriptor.allowed_origins
        == frozenset(
            {TurnOrigin.USER_MESSAGE, TurnOrigin.AUTONOMOUS_GROUP, TurnOrigin.SCHEDULED_AUTOMATION}
        )
        for descriptor in descriptors
    )
    catalog = UnifiedToolCatalog(
        entries=tuple(
            UnifiedToolCatalogEntry(
                descriptor=item,
                provider_id="core",
                scope_ids=item.scope_ids,
                compact_description=item.description,
                tags=(),
                searchable_text=item.model_name,
                estimated_schema_tokens=500,
                available=True,
                revision="1",
            )
            for item in descriptors
        ),
        scopes=(),
        revision="1",
    )
    planner = AuthorityFirstExposurePlanner(first_round_hard_cap=1, schema_token_budget=1)
    plan = planner.plan_initial(
        catalog=catalog,
        requestable_ids=frozenset({"find_contacts"}),
        hits=(),
        memory_view=None,
        kernel_tools=(),
        query="unrelated",
        artifact_available=False,
        reply_target_available=False,
    )
    assert {item.descriptor.model_name for item in plan.entries} == {
        tool.name for tool in definitions
    }
    assert plan.callable_ids == frozenset({"find_contacts"})
    from qq_ai_bot.automation.authority import PermissionLevel
    from qq_ai_bot.automation.registry import AutomationCapabilityRegistry

    registry = AutomationCapabilityRegistry()
    register_social_automation(registry, {})
    assert set(registry.names_for(PermissionLevel.USER)) == {
        automation_name(tool.name) for tool in definitions
    }
    for tool in definitions:
        assert registry.require(automation_name(tool.name)).argument_schema == tool.parameters
    async with database.sessions() as session, session.begin():
        person = await ensure_person(session, "10001")
        presence = await ensure_presence(session, "80001")
        conversation = await ensure_canonical_conversation(
            session, kind="private", primary_scope_key="private:80001:10001", person_id=person
        )
    repository = SocialOperationRepository(database)
    target = SocialTarget(kind="person", id=UUID(person))

    async def prepare(text: str = "hello"):
        return await repository.prepare(
            source_turn_id="turn-1",
            tool_call_id="call-1",
            source_conversation_id=conversation.conversation_id,
            action="send_private_message",
            target=target,
            payload={"text": text},
        )

    receipt = await prepare()
    assert await prepare() == receipt
    with pytest.raises(SocialError, match="idempotency_conflict"):
        await prepare("changed")
    claims = await asyncio.gather(
        *[repository.claim(receipt.operation_id, presence_id=presence) for _ in range(2)]
    )
    assert sum(claims) == 1
    with pytest.raises(RuntimeError, match="rollback"):
        async with database.sessions() as session, session.begin():
            await repository.finish(
                receipt.operation_id,
                status=OperationStatus.SUCCEEDED,
                platform_reference="123",
                session=session,
            )
            raise RuntimeError("rollback")
    assert (await repository.get(receipt.operation_id)).status == OperationStatus.EXECUTING
    assert await repository.recover_interrupted() == 1
    recovered = await prepare()
    assert recovered.status == OperationStatus.UNCERTAIN
    assert recovered.error_category == "process_interrupted"
    assert not await repository.claim(receipt.operation_id, presence_id=presence)
    assert await repository.recover_interrupted() == 0
    async with database.sessions() as session, session.begin():
        with pytest.raises(SocialError, match="invalid_transition"):
            await repository.finish(
                receipt.operation_id,
                status=OperationStatus.SUCCEEDED,
                platform_reference="123",
                session=session,
            )


@pytest.mark.asyncio
async def test_social_gateway_delivery_and_fail_closed(database: Database, tmp_path: Path) -> None:
    from sqlalchemy import select

    from qq_ai_bot.conversation.rollup.models import RollupPolicyConfig
    from qq_ai_bot.domain.conversations import ConversationScope
    from qq_ai_bot.gateway.providers import builtin_provider_catalog
    from qq_ai_bot.gateway.registry import GatewayConnectionRegistry
    from qq_ai_bot.identity.db_models import CanonicalPersonModel
    from qq_ai_bot.identity.routing import PresenceRouter
    from qq_ai_bot.persistence.models import ChatEventModel
    from qq_ai_bot.persistence.scoped_event_uow import ScopedEventLedgerUnitOfWork
    from qq_ai_bot.social.service import SocialContext, SocialService
    from qq_ai_bot.social.transfer import ArtifactTransfer
    from qq_ai_bot.workspace.store import WorkspaceStore

    class Bot:
        self_id = "80001"

        def __init__(self):
            self.calls: list[tuple[str, dict[str, Any]]] = []
            self.fail = False

        async def call_api(self, action: str, **params: Any):
            self.calls.append((action, params))
            if self.fail:
                raise TimeoutError()
            if action == "get_group_member_list":
                return [{"user_id": 10001, "nickname": "known"}]
            return {"message_id": 1000 + len(self.calls)}

    bot = Bot()
    registry = GatewayConnectionRegistry(providers=builtin_provider_catalog())
    router = PresenceRouter(database, registry)
    writer = ScopedEventLedgerUnitOfWork(database, config=RollupPolicyConfig())
    async with database.sessions() as session, session.begin():
        person = await ensure_person(session, "10001")
        presence = await ensure_presence(session, bot.self_id)
        row = await session.get(CanonicalPersonModel, person)
        row.enabled = True
    registry.connect(bot, provider_id="snowluma")
    registry.bind_presence(platform="qq", external_account_id=bot.self_id, presence_id=presence)
    await writer.append(
        scope=ConversationScope.private(bot.self_id, "10001"),
        platform_message_id="1",
        sender_user_id="10001",
        direction="inbound",
        content="hello",
    )
    async with database.sessions() as session:
        event = await session.scalar(
            select(ChatEventModel).where(ChatEventModel.platform_message_id == "1")
        )
        conversation_id = event.canonical_conversation_id
    assert await router.cas_takeover_person(person) in {"taken", "unchanged"}
    service = SocialService(database, router, writer)
    store = WorkspaceStore(tmp_path / "workspace")
    service.transfer = ArtifactTransfer(store, tmp_path / "transfer", "/transfer")
    context = SocialContext("turn", "text", conversation_id)
    args = {"target_id": person, "text": "reply"}
    first = await service.execute("send_private_message", args, context)
    assert first["status"] == "succeeded" and len(bot.calls) == 1
    assert await service.execute("send_private_message", args, context) == first
    async with database.sessions() as session:
        outbound = await session.scalar(
            select(ChatEventModel).where(
                ChatEventModel.platform_message_id == first["platform_reference"]
            )
        )
        assert (
            outbound.canonical_conversation_id == conversation_id
            and outbound.author_presence_id == presence
        )
        event_id = outbound.id
    recalled = await service.execute(
        "recall_own_message",
        {"event_id": event_id},
        SocialContext("turn", "recall", conversation_id),
    )
    assert recalled["status"] == "succeeded" and bot.calls[-1][0] == "delete_msg"
    artifact = store.write("report.txt", b"report")
    sent = await service.execute(
        "send_private_message",
        {"target_id": person, "artifact_id": artifact["artifact_id"], "attachment_kind": "file"},
        SocialContext("turn", "file", conversation_id),
    )
    assert sent["status"] == "succeeded" and bot.calls[-1][0] == "upload_private_file"
    assert list((tmp_path / "transfer").iterdir()) == []
    bot.fail = True
    uncertain_context = SocialContext("turn", "timeout", conversation_id)
    uncertain = await service.execute("send_private_message", args, uncertain_context)
    assert uncertain["status"] == "uncertain"
    count = len(bot.calls)
    assert await service.execute("send_private_message", args, uncertain_context) == uncertain
    assert len(bot.calls) == count
    bot.fail = False
    from qq_ai_bot.conversation.canonical_db_models import PersonActiveRouteModel
    from qq_ai_bot.identity.canonical_repository import ensure_space
    from qq_ai_bot.identity.db_models import CanonicalSpaceModel

    async with database.sessions() as session, session.begin():
        space_id = await ensure_space(session, "20001")
        space = await session.get(CanonicalSpaceModel, space_id)
        space.enabled = space.autonomous_enabled = True
        unknown = await ensure_person(session, "10002")
        row = await session.get(CanonicalPersonModel, unknown)
        row.enabled = True
    with pytest.raises(SocialError, match="contact_not_allowed"):
        await service.execute("send_private_message", {"target_id": unknown, "text": "no"}, context)
    assert await router.cas_takeover_space(space_id) in {"taken", "unchanged"}
    group = await service.execute(
        "send_group_message",
        {"target_id": space_id, "text": "group"},
        SocialContext("turn", "group", conversation_id),
    )
    assert group["status"] == "succeeded"
    async with database.sessions() as session:
        event = await session.scalar(
            select(ChatEventModel).where(
                ChatEventModel.platform_message_id == group["platform_reference"]
            )
        )
        assert event.canonical_conversation_id != conversation_id and event.group_id == "20001"
    members = await service.execute("get_group_members", {"target_id": space_id}, context)
    assert members["items"][0]["display_name"] == "known"
    poke = await service.execute(
        "poke_person",
        {"target_id": person, "space_id": space_id},
        SocialContext("turn", "poke", conversation_id),
    )
    assert poke["status"] == "succeeded" and bot.calls[-1][0] == "send_poke"
    async with database.sessions() as session, session.begin():
        route = await session.get(PersonActiveRouteModel, person)
        route.paused = True
    count = len(bot.calls)
    with pytest.raises(SocialError, match="route_paused"):
        await service.execute(
            "send_private_message", args, SocialContext("turn", "paused", conversation_id)
        )
    assert len(bot.calls) == count
    # A paused proactive route does not block a proven reply to the private sender.
    from dataclasses import replace
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import update

    from qq_ai_bot.social.db_models import SocialOperationModel

    async with database.sessions() as session, session.begin():
        await session.execute(
            update(SocialOperationModel).values(updated_at=datetime.now(UTC) - timedelta(minutes=2))
        )
    reply_context = SocialContext(
        "reply-turn",
        "reply-file",
        conversation_id,
        reply_message_id="1",
        reply_presence_id=presence,
    )
    replied = await service.execute(
        "send_private_message",
        {"target_id": person, "artifact_id": artifact["artifact_id"], "attachment_kind": "file"},
        reply_context,
    )
    assert replied["status"] == "succeeded" and bot.calls[-1][0] == "upload_private_file"
    async with database.sessions() as session:
        paused = await session.get(PersonActiveRouteModel, person)
        assert paused.paused
    with pytest.raises(SocialError, match="invalid_reply_context"):
        await service.send_route(
            SocialTarget(kind="person", id=UUID(person)),
            replace(reply_context, reply_message_id="forged"),
        )
    registry.disconnect(bot)
    with pytest.raises(Exception, match="disconnected"):
        await service.send_route(SocialTarget(kind="person", id=UUID(person)), reply_context)
