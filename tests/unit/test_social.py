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
async def test_group_directory_miss_refreshes_without_group_message(database: Database) -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from sqlalchemy import select

    from qq_ai_bot.identity.canonical_repository import ensure_space
    from qq_ai_bot.identity.db_models import CanonicalSpaceModel
    from qq_ai_bot.memory.read_scope import MemoryReadScopeResolver
    from qq_ai_bot.persistence.repositories import UserProfileRepository
    from qq_ai_bot.social.service import SocialService

    async with database.sessions.begin() as session:
        await ensure_presence(session, "80001")
        space_id = await ensure_space(session, "2001", name="旧群名")
    await UserProfileRepository(database).observe(user_id="1001", nickname="远野", group_id="2001")
    router = SimpleNamespace(resolve_presence=AsyncMock(return_value=object()))
    service = SocialService(database, router, None)
    service._call = AsyncMock(
        return_value=[
            {"group_id": 2001, "group_name": "数字生命研究所"},
            {"group_id": 9988776655, "group_name": "未登记群"},
        ]
    )
    assert await service.contacts("space", "数字生命研究所", exact=True) == [
        {"target_id": space_id, "display_name": "数字生命研究所", "kind": "space"}
    ]
    assert service._call.call_args.args[1:] == ("get_group_list", {"no_cache": True})
    assert await MemoryReadScopeResolver(database).groups_named("1001", "数字生命研究所") == (
        "2001",
    )
    assert await MemoryReadScopeResolver(database).groups_named("99999", "数字生命研究所") == ()
    first_refresh_calls = service._call.call_count
    assert await service.contacts("space", "未登记群", exact=True) == []
    assert service._call.call_count == first_refresh_calls
    service._directory_checked_at -= 31
    service._call.side_effect = TimeoutError()
    assert await service.contacts("space", "另一个名字", exact=True) == []
    async with database.sessions() as session:
        assert (
            await session.scalar(
                select(CanonicalSpaceModel.name).where(CanonicalSpaceModel.id == space_id)
            )
            == "数字生命研究所"
        )


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
        import shutil

        snapshot = next(transfer.root.iterdir())
        copied = tmp_path / "gateway-copy"
        shutil.copy2(snapshot, copied)
        assert copied.stat().st_mode & 0o200
        with copied.open("r+b") as stream:
            assert stream.read() == b"hello"
    assert list(transfer.root.iterdir()) == []


@pytest.mark.asyncio
async def test_social_receipt_claim_replay_and_interrupted_delivery(database: Database) -> None:
    definitions = (*social_tool_definitions(), *workspace_tools(), *sandbox_tools())
    assert definitions == (*social_tool_definitions(), *workspace_tools(), *sandbox_tools())
    descriptors = ChatToolCapabilityProvider(
        definitions, source=CapabilityTrustSource.CORE
    ).descriptors()
    assert len(descriptors) == 27
    assert all(
        descriptor.exposure is CapabilityExposure.DIRECT_ALWAYS for descriptor in descriptors
    )
    assert all(not descriptor.required_permissions for descriptor in descriptors)
    from qq_ai_bot.sandbox.environment_tools import SANDBOX_TOOLS
    from qq_ai_bot.workspace.tools import WORKSPACE_TOOLS

    working = SANDBOX_TOOLS | WORKSPACE_TOOLS | {"find_contacts", "read_conversation_history"}
    for descriptor in descriptors:
        expected = (
            frozenset(TurnOrigin)
            if descriptor.model_name in working
            else frozenset(
                {
                    TurnOrigin.USER_MESSAGE,
                    TurnOrigin.AUTONOMOUS_GROUP,
                    TurnOrigin.SCHEDULED_AUTOMATION,
                    *(
                        (TurnOrigin.PLUGIN_BACKGROUND,)
                        if descriptor.model_name == "send_message"
                        else ()
                    ),
                }
            )
        )
        assert descriptor.allowed_origins == expected
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

    with pytest.raises(SocialError, match="invalid_operation"):
        await repository.prepare(
            source_turn_id="retired-turn",
            tool_call_id="retired-call",
            source_conversation_id=conversation.conversation_id,
            action="send_private_message",
            target=target,
            payload={"text": "retired"},
        )

    async def prepare(text: str = "hello"):
        return await repository.prepare(
            source_turn_id="turn-1",
            tool_call_id="call-1",
            source_conversation_id=conversation.conversation_id,
            action="send_message",
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
            self.fail_action = None

        async def call_api(self, action: str, **params: Any):
            self.calls.append((action, params))
            if self.fail or self.fail_action == action:
                raise TimeoutError()
            if action == "get_group_member_list":
                return [{"user_id": 10001, "nickname": "known"}]
            if action == "get_group_member_info":
                return {"user_id": params["user_id"]}
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
        inbound_event_id = event.id
    assert await router.cas_takeover_person(person) in {"taken", "unchanged"}
    service = SocialService(database, router, writer)
    store = WorkspaceStore(tmp_path / "workspace")
    service.transfer = ArtifactTransfer(store, tmp_path / "transfer", "/transfer")
    context = SocialContext("turn", "text", conversation_id)
    args = {"target": {"kind": "person", "target_id": person}, "text": "reply"}
    first = await service.execute("send_message", args, context)
    assert first["status"] == "succeeded" and len(bot.calls) == 1
    assert await service.execute("send_message", args, context) == first
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
        "send_message",
        {
            "target": {"kind": "person", "target_id": person},
            "artifact_id": artifact["artifact_id"],
            "attachment_kind": "file",
        },
        SocialContext("turn", "file", conversation_id),
    )
    assert sent["status"] == "succeeded" and bot.calls[-1][0] == "upload_private_file"
    assert list((tmp_path / "transfer").iterdir()) == []
    bot.fail = True
    uncertain_context = SocialContext("turn", "timeout", conversation_id)
    uncertain = await service.execute("send_message", args, uncertain_context)
    assert uncertain["status"] == "uncertain"
    count = len(bot.calls)
    assert await service.execute("send_message", args, uncertain_context) == uncertain
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
        await service.execute(
            "send_message",
            {"target": {"kind": "person", "target_id": unknown}, "text": "no"},
            SocialContext("turn", "unknown", conversation_id),
        )
    assert await router.cas_takeover_space(space_id) in {"taken", "unchanged"}
    group = await service.execute(
        "send_message",
        {"target": {"kind": "space", "target_id": space_id}, "text": "group"},
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
            "send_message", args, SocialContext("turn", "paused", conversation_id)
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
        trigger_event_id=inbound_event_id,
        reply_presence_id=presence,
    )
    group_context = SocialContext("group-poke", "default", conversation_id, space_id=space_id)
    poked = await service.execute("poke_person", {"target_id": person}, group_context)
    assert poked["status"] == "succeeded"
    assert bot.calls[-1][0] == "send_poke" and bot.calls[-1][1]["group_id"] == 20001
    assert bot.calls[-2][0] == "get_group_member_info"
    count = len(bot.calls)
    for scene_args, error in (
        ({"scene": "private"}, "route_paused"),
        ({"space_id": "20001"}, "invalid_space_id"),
        ({"scene": "private", "space_id": space_id}, "invalid_poke_scene"),
    ):
        with pytest.raises(SocialError, match=error):
            await service.execute("poke_person", {"target_id": person, **scene_args}, group_context)
    assert len(bot.calls) == count
    async with database.sessions() as session:
        paused = await session.get(PersonActiveRouteModel, person)
        assert paused.paused
    replied = await service.execute(
        "send_message",
        {
            "target": {"kind": "person", "target_id": person},
            "artifact_id": artifact["artifact_id"],
            "attachment_kind": "file",
        },
        reply_context,
    )
    assert replied["status"] == "succeeded" and bot.calls[-1][0] == "upload_private_file"
    async with database.sessions() as session:
        paused = await session.get(PersonActiveRouteModel, person)
        assert paused.paused
    with pytest.raises(SocialError, match="invalid_reply_context"):
        await service.send_route(
            SocialTarget(kind="person", id=UUID(person)),
            replace(reply_context, trigger_event_id=999999),
        )
    # File and caption are independently receipted. Replay must never re-send,
    # including a deleted source artifact or a failed caption.
    for index, failing_action in enumerate((None, "send_private_msg", "upload_private_file")):
        async with database.sessions() as session, session.begin():
            await session.execute(
                update(SocialOperationModel).values(
                    updated_at=datetime.now(UTC) - timedelta(minutes=2)
                )
            )
        item = store.write(f"caption-{index}.txt", b"hello world")
        combined_args = {
            "target": {"kind": "person", "target_id": person},
            "artifact_id": item["artifact_id"],
            "attachment_kind": "file",
            "text": "hello caption",
        }
        combined_context = replace(reply_context, call_id=f"caption-{index}")
        bot.fail_action = failing_action
        before = len(bot.calls)
        combined = await service.execute("send_message", combined_args, combined_context)
        actions = [action for action, _ in bot.calls[before:]]
        assert actions == (
            ["upload_private_file"]
            if failing_action == "upload_private_file"
            else ["upload_private_file", "send_private_msg"]
        )
        assert combined["file"]["status"] == (
            "uncertain" if failing_action == "upload_private_file" else "succeeded"
        )
        assert combined["caption"]["status"] == (
            "not_sent"
            if failing_action == "upload_private_file"
            else "uncertain"
            if failing_action
            else "succeeded"
        )
        if failing_action == "send_private_msg":
            assert combined["error"] == "file_sent_caption_unconfirmed"
        count = len(bot.calls)
        store.delete(item["artifact_id"], expected_revision=item["revision"])
        assert await service.execute("send_message", combined_args, combined_context) == combined
        assert len(bot.calls) == count
        async with database.sessions() as session:
            file_event = (
                await session.scalar(
                    select(ChatEventModel).where(
                        ChatEventModel.platform_message_id == combined["file"]["platform_reference"]
                    )
                )
                if combined["file"]["platform_reference"]
                else None
            )
            if failing_action != "upload_private_file":
                assert (
                    file_event is not None and file_event.content == f"[文件: caption-{index}.txt]"
                )
    bot.fail_action = None
    # Crash after a confirmed upload but before caption dispatch: do not resume
    # either network action when the same tool call is replayed.
    interrupted_args = {
        "target": {"kind": "person", "target_id": person},
        "artifact_id": artifact["artifact_id"],
        "attachment_kind": "file",
        "text": "pending caption",
    }
    interrupted_context = replace(reply_context, call_id="caption-crash")
    interrupted = await service.receipts.prepare(
        source_turn_id=interrupted_context.turn_id,
        tool_call_id=interrupted_context.call_id,
        source_conversation_id=conversation_id,
        action="send_message",
        target=SocialTarget(kind="person", id=UUID(person)),
        payload=interrupted_args,
    )
    assert await service.receipts.claim(interrupted.operation_id, presence_id=presence)
    async with database.sessions() as session, session.begin():
        await service.receipts.finish(
            interrupted.operation_id,
            status=OperationStatus.SUCCEEDED,
            platform_reference="confirmed-before-crash",
            session=session,
        )
    before = len(bot.calls)
    interrupted_result = await service.execute(
        "send_message", interrupted_args, interrupted_context
    )
    assert interrupted_result["file"]["status"] == "succeeded"
    assert interrupted_result["caption"]["status"] == "not_sent"
    assert len(bot.calls) == before
    with pytest.raises(SocialError, match="unknown_tool"):
        await service.execute(
            "send_private_message",
            {"target_id": person, "text": "retired"},
            SocialContext("turn", "retired", conversation_id),
        )
    registry.disconnect(bot)
    with pytest.raises(Exception, match="disconnected"):
        await service.send_route(SocialTarget(kind="person", id=UUID(person)), reply_context)
    from tests.support.social_identity_cases import run_identity_scenarios

    await run_identity_scenarios(tmp_path)


@pytest.mark.asyncio
async def test_send_message_defaults_to_current_group_and_replays_receipt(
    database: Database, tmp_path: Path
) -> None:
    from dataclasses import replace

    from sqlalchemy import select
    from tests.support.social_identity_cases import social_env

    from qq_ai_bot.conversation.canonical_db_models import SpaceActiveRouteModel
    from qq_ai_bot.persistence.models import ChatEventModel

    env = await social_env(database, tmp_path)
    async with database.sessions() as session:
        inbound = await session.scalar(
            select(ChatEventModel).where(ChatEventModel.platform_message_id == "inbound")
        )
        assert inbound is not None
        assert inbound.ingress_presence_id == env.presence
    context = replace(env.context, trigger_event_id=inbound.id)
    sent = await env.service.execute("send_message", {"text": "第一步完成"}, context)
    assert sent["status"] == "succeeded"
    assert sent["target"] == {"kind": "space", "id": env.space}
    assert env.bot.calls[-1][0] == "send_group_msg"
    async with database.sessions() as session, session.begin():
        route = await session.get(SpaceActiveRouteModel, env.space)
        assert route is not None
        route.paused = True
    before = len(env.bot.calls)
    assert await env.service.execute("send_message", {"text": "第一步完成"}, context) == sent
    assert len(env.bot.calls) == before
    with pytest.raises(SocialError, match="idempotency_conflict"):
        await env.service.execute("send_message", {"text": "不同内容"}, context)


@pytest.mark.asyncio
async def test_send_message_sanitizes_internal_event_prefix_before_effect(
    database: Database, tmp_path: Path
) -> None:
    from dataclasses import replace

    from sqlalchemy import select
    from tests.support.social_identity_cases import social_env

    from qq_ai_bot.persistence.models import ChatEventModel

    env = await social_env(database, tmp_path)
    context = replace(env.context, call_id="sanitize-event-prefix")
    receipt = await env.service.execute(
        "send_message",
        {"text": "#62052>那刻度校完，我可就直接反超了喵"},
        context,
    )
    assert receipt["status"] == "succeeded"
    assert env.bot.calls[-1][1]["message"] == [
        {"type": "text", "data": {"text": "那刻度校完，我可就直接反超了喵"}}
    ]
    async with database.sessions() as session:
        row = await session.scalar(
            select(ChatEventModel)
            .where(ChatEventModel.direction == "outbound")
            .order_by(ChatEventModel.id.desc())
            .limit(1)
        )
    assert row is not None
    assert row.content == "那刻度校完，我可就直接反超了喵"
    before = len(env.bot.calls)
    with pytest.raises(SocialError, match="empty_message_after_sanitization"):
        await env.service.execute(
            "send_message",
            {"text": "#62052>"},
            replace(context, call_id="sanitize-empty"),
        )
    assert len(env.bot.calls) == before


@pytest.mark.asyncio
async def test_send_message_reuses_automatic_reply_splitting(
    database: Database, tmp_path: Path
) -> None:
    from dataclasses import replace
    from types import SimpleNamespace

    from tests.support.social_identity_cases import social_env

    from qq_ai_bot.admin.models import ReplyRuntimeConfig

    env = await social_env(database, tmp_path)
    snapshot = SimpleNamespace(reply=ReplyRuntimeConfig(0, 0, 1800, 10))
    context = replace(env.context, runtime_snapshot=snapshot)
    args = {"text": "第一段\n第二段\n第三段"}
    result = await env.service.execute("send_message", args, context)
    assert result["status"] == "succeeded"
    assert result["planned_messages"] == result["sent_messages"] == 3
    assert [
        params["message"][0]["data"]["text"]
        for action, params in env.bot.calls
        if action == "send_group_msg"
    ] == ["第一段", "第二段", "第三段"]
    assert await env.service.execute("send_message", args, context) == result
    assert len([action for action, _ in env.bot.calls if action == "send_group_msg"]) == 3


@pytest.mark.asyncio
async def test_social_sends_and_pokes_have_no_frequency_gate(
    database: Database, tmp_path: Path
) -> None:
    from dataclasses import replace

    from tests.support.social_identity_cases import social_env

    env = await social_env(database, tmp_path)
    for index in range(4):
        receipt = await env.service.execute(
            "send_message",
            {"text": f"第 {index + 1} 条"},
            replace(env.context, call_id=f"send-{index}"),
        )
        assert receipt["status"] == "succeeded"
    for index in range(2):
        receipt = await env.service.execute(
            "poke_person",
            {"target_id": env.person},
            replace(env.context, call_id=f"poke-{index}"),
        )
        assert receipt["status"] == "succeeded"
    assert sum(action == "send_group_msg" for action, _ in env.bot.calls) == 4
    assert sum(action == "send_poke" for action, _ in env.bot.calls) == 2


@pytest.mark.asyncio
async def test_send_message_split_stops_on_uncertain_part_without_resending(
    database: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import replace
    from types import SimpleNamespace

    from tests.support.social_identity_cases import social_env

    from qq_ai_bot.admin.models import ReplyRuntimeConfig

    env = await social_env(database, tmp_path)
    snapshot = SimpleNamespace(reply=ReplyRuntimeConfig(0, 0, 1800, 10))
    context = replace(env.context, runtime_snapshot=snapshot)
    args = {"text": "第一段\n第二段\n第三段"}
    original_call = env.bot.call_api
    sends = 0

    async def fail_second(action, **params):
        nonlocal sends
        if action == "send_group_msg":
            sends += 1
            if sends == 2:
                raise RuntimeError("simulated_disconnect")
        return await original_call(action, **params)

    monkeypatch.setattr(env.bot, "call_api", fail_second)
    result = await env.service.execute("send_message", args, context)
    assert result["status"] == "uncertain"
    assert result["sent_messages"] == 1
    assert [part["status"] for part in result["parts"]] == ["succeeded", "uncertain"]
    assert sends == 2
    assert await env.service.execute("send_message", args, context) == result
    assert sends == 2
    changed = replace(
        context,
        runtime_snapshot=SimpleNamespace(reply=ReplyRuntimeConfig(0, 0, 2, 10)),
    )
    with pytest.raises(SocialError, match="idempotency_conflict"):
        await env.service.execute("send_message", args, changed)
    assert sends == 2


@pytest.mark.asyncio
async def test_send_message_split_reports_confirmed_failure_not_uncertainty(
    database: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import replace
    from types import SimpleNamespace

    from tests.support.social_identity_cases import social_env

    from qq_ai_bot.admin.models import ReplyRuntimeConfig

    env = await social_env(database, tmp_path)
    context = replace(
        env.context,
        runtime_snapshot=SimpleNamespace(reply=ReplyRuntimeConfig(0, 0, 1800, 10)),
    )
    original_effect = env.service._effect
    calls = 0

    async def reject_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            return {"error": "confirmed_rejection"}
        return await original_effect(*args, **kwargs)

    monkeypatch.setattr(env.service, "_effect", reject_second)
    result = await env.service.execute("send_message", {"text": "第一段\n第二段\n第三段"}, context)
    assert result["status"] == "failed"
    assert result["error"] == "confirmed_rejection"
    assert result["sent_messages"] == 1
    assert calls == 2
    assert sum(action == "send_group_msg" for action, _ in env.bot.calls) == 1


@pytest.mark.asyncio
async def test_send_message_explicit_person(database: Database, tmp_path: Path) -> None:
    from dataclasses import replace

    from tests.support.social_identity_cases import social_env

    env = await social_env(database, tmp_path)
    assert await env.router.cas_takeover_person(env.person) in {"taken", "unchanged"}
    context = replace(env.context, call_id="private")
    sent = await env.service.execute(
        "send_message",
        {"target": {"kind": "person", "target_id": env.person}, "text": "私信"},
        context,
    )
    assert sent["status"] == "succeeded"
    assert sent["target"] == {"kind": "person", "id": env.person}
    assert env.bot.calls[-1][0] == "send_private_msg"


@pytest.mark.asyncio
async def test_legacy_agent_delivery_checks_durable_send_receipt(
    database: Database, tmp_path: Path
) -> None:
    from types import SimpleNamespace

    from tests.support.social_identity_cases import social_env

    from qq_ai_bot.automation.executor import AutomationExecutor
    from qq_ai_bot.automation.models import AutomationStep
    from qq_ai_bot.social.models import OperationStatus, SocialTarget

    env = await social_env(database, tmp_path)
    turn = f"{env.context.conversation_id}:execution:automation:17:execute:hash"
    target = SocialTarget(kind="space", id=UUID(env.space))
    first = await env.service.receipts.prepare(
        source_turn_id=turn,
        tool_call_id="send-1",
        source_conversation_id=env.context.conversation_id,
        action="send_message",
        target=target,
        payload={"text": "sent explicitly"},
    )
    assert await env.service.receipts.claim(first.operation_id, presence_id=env.presence)
    async with database.sessions() as session, session.begin():
        await env.service.receipts.finish(
            first.operation_id,
            status=OperationStatus.SUCCEEDED,
            platform_reference="42",
            session=session,
        )
    agent = AutomationStep(
        id="execute", call="yuki.agent", arguments={"instruction": "go"}, save_as="result"
    )
    delivery = AutomationStep(
        id="deliver",
        call="onebot.send_group_message",
        arguments={"group_id": "$current_group_id", "text": "${result.text}"},
    )
    automation = SimpleNamespace(
        script=SimpleNamespace(steps=(agent, delivery)),
        script_hash="hash",
        canonical_target_space_id=env.space,
        canonical_creator_person_id=env.person,
    )
    executor = object.__new__(AutomationExecutor)
    executor._repository = SimpleNamespace(_database=database)
    run = SimpleNamespace(id=17)
    assert (
        await executor._legacy_agent_delivery_status(
            automation, run, 1, env.context.conversation_id
        )
        == "succeeded"
    )
    unknown = await env.service.receipts.prepare(
        source_turn_id=turn,
        tool_call_id="send-2",
        source_conversation_id=env.context.conversation_id,
        action="send_message",
        target=target,
        payload={"text": "maybe sent"},
    )
    assert await env.service.receipts.claim(unknown.operation_id, presence_id=env.presence)
    async with database.sessions() as session, session.begin():
        await env.service.receipts.finish(
            unknown.operation_id,
            status=OperationStatus.UNCERTAIN,
            session=session,
        )
    assert (
        await executor._legacy_agent_delivery_status(
            automation, run, 1, env.context.conversation_id
        )
        == "uncertain"
    )


@pytest.mark.asyncio
async def test_send_message_media_uses_same_receipt_and_no_replay(
    database: Database, tmp_path: Path
) -> None:
    from dataclasses import replace
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from tests.support.social_identity_cases import social_env

    from qq_ai_bot.domain.messages import AttachmentKind, OutboundMedia, OutboundMessage
    from qq_ai_bot.emoji.models import EmojiPreparationResult, EmojiPreparationStatus

    env = await social_env(database, tmp_path)
    assert await env.router.cas_takeover_person(env.person) in {"taken", "unchanged"}
    audio = tmp_path / "reply.wav"
    audio.write_bytes(b"test-audio")
    voice_message = OutboundMessage(
        media=(
            OutboundMedia(
                kind=AttachmentKind.AUDIO,
                mime_type="audio/wav",
                summary="语音消息",
                local_path=str(audio),
                spoken_text="你好",
                generation_id=7,
            ),
        )
    )
    env.service.speech_delivery = SimpleNamespace(
        prepare=AsyncMock(return_value=SimpleNamespace(message=voice_message)),
        record_success=AsyncMock(),
    )
    context = replace(
        env.context,
        call_id="voice",
        actor=SimpleNamespace(),
        runtime_snapshot=SimpleNamespace(
            speech=SimpleNamespace(enabled=True, agent_delivery_enabled=True),
            emoji=SimpleNamespace(enabled=True),
        ),
        voice_delivery_allowed=True,
    )
    voice_args = {
        "target": {"kind": "person", "target_id": env.person},
        "text": "你好",
        "voice": {"request_basis": "agent_initiated"},
    }
    sent = await env.service.execute("send_message", voice_args, context)
    assert sent["status"] == "succeeded"
    assert env.bot.calls[-1][1]["message"][0]["type"] == "record"
    assert await env.service.execute("send_message", voice_args, context) == sent
    env.service.speech_delivery.prepare.assert_awaited_once()
    env.service.speech_delivery.record_success.assert_awaited_once()

    emoji_message = OutboundMessage(
        media=(
            OutboundMedia(
                kind=AttachmentKind.IMAGE,
                content=b"test-image",
                mime_type="image/png",
                summary="笑脸",
                emoji_id="emoji-1",
            ),
        )
    )
    env.service.emoji_delivery = SimpleNamespace(
        prepare=AsyncMock(
            return_value=EmojiPreparationResult(
                status=EmojiPreparationStatus.READY,
                message=emoji_message,
                emoji_id="emoji-1",
                reason_code="selected",
            )
        ),
        record_send_accepted=AsyncMock(),
        record_success=AsyncMock(),
    )
    emoji_context = replace(context, call_id="emoji")
    emoji_args = {
        "target": {"kind": "person", "target_id": env.person},
        "text": "看这个",
        "emoji": {"goal": "开心"},
    }
    emoji_receipt = await env.service.execute("send_message", emoji_args, emoji_context)
    assert emoji_receipt["status"] == "succeeded"
    assert [part["type"] for part in env.bot.calls[-1][1]["message"]] == ["text", "image"]
    assert await env.service.execute("send_message", emoji_args, emoji_context) == emoji_receipt
    env.service.emoji_delivery.prepare.assert_awaited_once()


@pytest.mark.asyncio
async def test_chat_agent_sends_only_via_explicit_tool(database: Database, tmp_path: Path) -> None:
    import json

    from tests.conftest import MemorySender, build_harness, make_settings
    from tests.support.social_identity_cases import social_env

    from qq_ai_bot.domain.conversations import ConversationScope, ScopeType
    from qq_ai_bot.domain.messages import (
        ChatResponse,
        InboundMessage,
        SenderIdentity,
        ToolCall,
        ToolFunction,
    )
    from qq_ai_bot.llm.fake import FakeLLMProvider
    from qq_ai_bot.services.main_agent_contract import MainAgentContract
    from qq_ai_bot.workspace.short_state import ShortState

    env = await social_env(database, tmp_path)
    calls = 0

    def respond(_request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return ChatResponse(
                "",
                0,
                tool_calls=(
                    ToolCall(
                        "send-step",
                        ToolFunction(
                            "send_message", json.dumps({"text": "第一步完成\n第二步完成"})
                        ),
                    ),
                ),
            )
        return "内部收尾，不再自动发送"

    provider = FakeLLMProvider(respond)
    harness = build_harness(
        database, make_settings(database.url, enabled_groups_csv="20001"), provider
    )
    chat = harness.processor._chat
    chat._tools.social_service = env.service
    chat._agent_runner.main_contract = MainAgentContract(chat, ShortState(env.store))
    sender = MemorySender()
    result = await harness.processor.handle(
        InboundMessage(
            message_id="explicit-tool-inbound",
            event_type="message:test",
            scope_type=ScopeType.GROUP,
            sender=SenderIdentity("10001"),
            text="做完第一步告诉我",
            bot_user_id="80001",
            group_id="20001",
            mentions_bot=True,
            conversation_id=env.context.conversation_id,
            legacy_conversation_key=ConversationScope.group("80001", "20001").key,
            person_id=env.person,
            space_id=env.space,
            presence_id=env.presence,
        ),
        sender,
    )
    assert result.reason == "chat" and result.sent_messages == 2
    assert [action for action, _ in env.bot.calls if action == "send_group_msg"] == [
        "send_group_msg",
        "send_group_msg",
    ]
    assert not sender.messages
    assert await harness.relationship_jobs.pending_count() == 1


@pytest.mark.asyncio
async def test_chat_agent_recovers_unsent_final_through_send_message(
    database: Database, tmp_path: Path
) -> None:
    import json

    from tests.conftest import MemorySender, build_harness, make_settings
    from tests.support.social_identity_cases import social_env

    from qq_ai_bot.domain.conversations import ConversationScope, ScopeType
    from qq_ai_bot.domain.messages import (
        ChatResponse,
        InboundMessage,
        SenderIdentity,
        ToolCall,
        ToolFunction,
    )
    from qq_ai_bot.llm.fake import FakeLLMProvider
    from qq_ai_bot.services.main_agent_contract import MainAgentContract
    from qq_ai_bot.workspace.short_state import ShortState

    env = await social_env(database, tmp_path)
    requests = []

    def respond(request):
        requests.append(request)
        if len(requests) == 1:
            return ChatResponse("你好，我在。", 0)
        if len(requests) == 2:
            assert any(
                message.role == "system" and "上一段最终正文没有发送给用户" in message.content
                for message in request.messages
            )
            return ChatResponse(
                "",
                0,
                tool_calls=(
                    ToolCall(
                        "send-recovered",
                        ToolFunction("send_message", json.dumps({"text": "你好，我在。"})),
                    ),
                ),
            )
        return ChatResponse("内部收尾", 0)

    provider = FakeLLMProvider(respond)
    harness = build_harness(
        database, make_settings(database.url, enabled_groups_csv="20001"), provider
    )
    chat = harness.processor._chat
    chat._tools.social_service = env.service
    chat._agent_runner.main_contract = MainAgentContract(chat, ShortState(env.store))
    sender = MemorySender()
    result = await harness.processor.handle(
        InboundMessage(
            message_id="unsent-final-inbound",
            event_type="message:test",
            scope_type=ScopeType.GROUP,
            sender=SenderIdentity("10001"),
            text="你现在知道怎么回复吗",
            bot_user_id="80001",
            group_id="20001",
            mentions_bot=True,
            conversation_id=env.context.conversation_id,
            legacy_conversation_key=ConversationScope.group("80001", "20001").key,
            person_id=env.person,
            space_id=env.space,
            presence_id=env.presence,
        ),
        sender,
    )
    assert result.reason == "chat" and result.sent_messages == 1
    assert len(requests) == 3
    assert [action for action, _ in env.bot.calls if action == "send_group_msg"] == [
        "send_group_msg"
    ]
    assert not sender.messages


@pytest.mark.asyncio
async def test_chat_agent_can_choose_silent_final(database: Database, tmp_path: Path) -> None:
    from tests.conftest import MemorySender, build_harness, make_settings
    from tests.support.social_identity_cases import social_env

    from qq_ai_bot.domain.conversations import ConversationScope, ScopeType
    from qq_ai_bot.domain.messages import ChatResponse, InboundMessage, SenderIdentity
    from qq_ai_bot.llm.fake import FakeLLMProvider
    from qq_ai_bot.services.main_agent_contract import MainAgentContract
    from qq_ai_bot.workspace.short_state import ShortState

    env = await social_env(database, tmp_path)
    calls = 0

    def respond(_request):
        nonlocal calls
        calls += 1
        return ChatResponse("", 0)

    provider = FakeLLMProvider(respond)
    harness = build_harness(
        database, make_settings(database.url, enabled_groups_csv="20001"), provider
    )
    chat = harness.processor._chat
    chat._tools.social_service = env.service
    chat._agent_runner.main_contract = MainAgentContract(chat, ShortState(env.store))
    sender = MemorySender()
    result = await harness.processor.handle(
        InboundMessage(
            message_id="silent-final-inbound",
            event_type="message:test",
            scope_type=ScopeType.GROUP,
            sender=SenderIdentity("10001"),
            text="这条不用回",
            bot_user_id="80001",
            group_id="20001",
            mentions_bot=True,
            conversation_id=env.context.conversation_id,
            legacy_conversation_key=ConversationScope.group("80001", "20001").key,
            person_id=env.person,
            space_id=env.space,
            presence_id=env.presence,
        ),
        sender,
    )
    assert result.reason == "chat" and result.sent_messages == 0
    assert calls == 1
    assert not sender.messages
    assert not [
        action for action, _ in env.bot.calls if action in {"send_group_msg", "send_private_msg"}
    ]


@pytest.mark.asyncio
async def test_chat_agent_rejects_repeated_unsent_final(database: Database, tmp_path: Path) -> None:
    from tests.conftest import MemorySender, build_harness, make_settings
    from tests.support.social_identity_cases import social_env

    from qq_ai_bot.domain.conversations import ConversationScope, ScopeType
    from qq_ai_bot.domain.messages import ChatResponse, InboundMessage, SenderIdentity
    from qq_ai_bot.llm.fake import FakeLLMProvider
    from qq_ai_bot.services.main_agent_contract import MainAgentContract
    from qq_ai_bot.workspace.short_state import ShortState

    env = await social_env(database, tmp_path)
    requests = []

    def respond(request):
        requests.append(request)
        return ChatResponse("只写正文，不调用工具", 0)

    provider = FakeLLMProvider(respond)
    harness = build_harness(
        database, make_settings(database.url, enabled_groups_csv="20001"), provider
    )
    chat = harness.processor._chat
    chat._tools.social_service = env.service
    chat._agent_runner.main_contract = MainAgentContract(chat, ShortState(env.store))
    sender = MemorySender()
    result = await harness.processor.handle(
        InboundMessage(
            message_id="repeated-unsent-inbound",
            event_type="message:test",
            scope_type=ScopeType.GROUP,
            sender=SenderIdentity("10001"),
            text="回我一句",
            bot_user_id="80001",
            group_id="20001",
            mentions_bot=True,
            conversation_id=env.context.conversation_id,
            legacy_conversation_key=ConversationScope.group("80001", "20001").key,
            person_id=env.person,
            space_id=env.space,
            presence_id=env.presence,
        ),
        sender,
    )
    assert result.reason == "llm_failure"
    assert len(requests) == 2
    assert sender.messages
    assert not [
        action for action, _ in env.bot.calls if action in {"send_group_msg", "send_private_msg"}
    ]


@pytest.mark.asyncio
async def test_plugin_background_send_is_bound_to_frozen_job_target(
    database: Database, tmp_path: Path
) -> None:
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from tests.support.social_identity_cases import social_env

    from qq_ai_bot.capabilities.invocation import ToolInvocationContext, current_invocation
    from qq_ai_bot.domain.conversations import ConversationScope
    from qq_ai_bot.social.agent_adapter import invoke_social

    env = await social_env(database, tmp_path)
    event = await env.service.writer.append_external(
        scope=ConversationScope.group("80001", "20001"),
        platform_message_id="plugin-event-1",
        source_plugin_id="test-plugin",
        external_source="test",
        external_event_key="event-1",
        external_event_type="notice",
        external_payload={},
        external_target_id="20001",
        content="plugin notice",
        occurred_at=datetime.now(UTC),
    )
    runtime = SimpleNamespace(
        origin=TurnOrigin.PLUGIN_BACKGROUND,
        effective_trigger_event_id=event.event.id,
        effective_conversation_id=env.context.conversation_id,
        inbound=None,
        read_only=False,
        tools_closed=False,
        space_id=env.space,
        person_id=None,
    )
    token = current_invocation.set(ToolInvocationContext(runtime, call_id="plugin-send"))
    try:
        receipt = await invoke_social(env.service, "send_message", {"text": "通知"}, runtime)
        assert receipt["status"] == "succeeded"
        assert env.bot.calls[-1][0] == "send_group_msg"
        with pytest.raises(SocialError, match="permission_denied"):
            await invoke_social(
                env.service,
                "send_message",
                {"target": {"kind": "person", "target_id": env.person}, "text": "越界"},
                runtime,
            )
    finally:
        current_invocation.reset(token)
