"""Real Social receipts around native MockHTTP, without QQ/Telegram traffic."""

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime
from email.parser import BytesParser
from email.policy import default
from html.parser import HTMLParser
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import select
from tests.conftest import build_harness, make_settings
from tests.integration.test_telegram_runtime import update
from tests.support.social_identity_cases import social_env
from tests.support.workspace_snapshots import snapshot_bytes

from yuki.capabilities.invocation import direct_invocations
from yuki.capabilities.results import normalize_legacy_result
from yuki.conversation.canonical_db_models import (
    CanonicalConversationModel,
    SpaceActiveRouteModel,
)
from yuki.domain.messages import ToolCall, ToolFunction
from yuki.gateway.providers import builtin_provider_catalog
from yuki.gateway.providers.telegram import TelegramClient
from yuki.gateway.providers.telegram_ingress import TelegramGateway
from yuki.gateway.registry import GatewayConnectionRegistry
from yuki.gateway.social import TransportMedia, TransportMessage, transport_payload
from yuki.identity.canonical_repository import create_presence, replace_assignment
from yuki.identity.canonical_uow import CanonicalIngressUnitOfWork
from yuki.identity.db_models import AccountModel, CanonicalPersonModel, IdentityBindingModel
from yuki.identity.ingress import CanonicalIngressResolver
from yuki.identity.routing import PresenceRouter
from yuki.llm.fake import FakeLLMProvider
from yuki.persistence.models import ChatEventModel
from yuki.services.message_splitter import OutboundMessageSplitter
from yuki.social.db_models import SocialOperationModel
from yuki.social.models import SocialError
from yuki.social.repository import payload_hash, sequence_prefix
from yuki.social.service import SocialContext, SocialService
from yuki.workspace.store import WorkspaceStore


def visible(value):
    class Text(HTMLParser):
        def __init__(self):
            super().__init__()
            self.parts = []

        def handle_data(self, data):
            self.parts.append(data)

    parsed = Text()
    parsed.feed(value)
    return "".join(parsed.parts)


def request_parts(request):
    if request.headers.get("content-type", "").startswith("application/json"):
        return json.loads(request.content), {}
    message = BytesParser(policy=default).parsebytes(
        f"Content-Type: {request.headers['content-type']}\r\n\r\n".encode() + request.content
    )
    values, files = {}, {}
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if part.get_filename():
            files[name] = part.get_payload(decode=True)
        else:
            value = part.get_payload(decode=True).decode()
            values[name] = json.loads(value) if name in {"reply_parameters"} else value
    return values, files


@pytest.fixture
async def native_env(database, tmp_path):
    wire = []
    failure = SimpleNamespace(method=None, timeout=False)

    def http(request):
        method = request.url.path.rsplit("/", 1)[-1]
        params, files = request_parts(request)
        wire.append((method, params, files))
        if method == "getMe":
            result = {"id": 80001, "is_bot": True, "username": "offline_yuki"}
        elif method == "getChatMember":
            result = {"status": "administrator", "user": {"id": params["user_id"]}}
        elif method == "deleteMessage":
            result = True
        elif method in {"sendMessage", "sendDocument", "sendPhoto"}:
            if method == failure.method:
                if failure.timeout:
                    raise httpx.ReadTimeout("offline lost acknowledgement", request=request)
                return httpx.Response(200, json={"ok": False, "error_code": 400})
            field, limit = ("text", 4096) if method == "sendMessage" else ("caption", 1024)
            if len(visible(params.get(field, ""))) > limit:
                return httpx.Response(200, json={"ok": False, "error_code": 400})
            result = {"message_id": 1000 + len(wire), "chat": {"id": params["chat_id"]}}
        else:
            raise AssertionError(method)
        return httpx.Response(200, json={"ok": True, "result": result})

    harness = build_harness(
        database, make_settings(database.url), FakeLLMProvider(lambda _: "NO_REPLY")
    )
    registry = GatewayConnectionRegistry(providers=builtin_provider_catalog())
    router = PresenceRouter(database, registry)
    harness.processor._canonical_ingress = CanonicalIngressResolver(database, registry, router)
    harness.processor._canonical_uow = CanonicalIngressUnitOfWork(database, router)
    social = SocialService(database, router, harness.scoped_events)
    social.workspace = WorkspaceStore(tmp_path / "workspace")
    async with httpx.AsyncClient(transport=httpx.MockTransport(http)) as transport:
        client = TelegramClient("offline-only-token", client=transport)
        await client.get_me()
        async with database.immediate_session() as session:
            presence = await create_presence(
                session,
                external_account_id=client.self_id,
                platform="telegram",
                now=datetime.now(UTC),
            )
        registry.connect(client, provider_id="telegram", presence_id=presence.id)
        gateway = TelegramGateway(
            database, registry, router, harness.processor, harness.scoped_events, client
        )
        gateway.presence_id = presence.id
        await gateway.receive(update(chat=-10042, text="!ai prepare native test"))
        if gateway._turns:
            await asyncio.gather(*tuple(gateway._turns))
        async with database.read_session() as session:
            event = await session.scalar(select(ChatEventModel).order_by(ChatEventModel.id))
            conversation = await session.get(
                CanonicalConversationModel, event.canonical_conversation_id
            )
            account = await session.scalar(
                select(AccountModel).where(
                    AccountModel.platform == "telegram", AccountModel.external_account_id == "42"
                )
            )
        config = await harness.processor._runtime_config.snapshot(
            user_id=event.author_person_id, group_id=conversation.space_id
        )
        config = replace(
            config,
            reply=replace(
                config.reply,
                max_qq_message_chars=20000,
                hard_max_messages=1,
                delay_min_seconds=0,
                delay_max_seconds=0,
            ),
        )
        context = SocialContext(
            "native-test",
            "original-call",
            conversation.id,
            person_refs={"current_speaker": event.author_person_id},
            space_id=conversation.space_id,
            trigger_event_id=event.id,
            runtime_snapshot=config,
        )
        wire.clear()
        yield SimpleNamespace(
            service=social,
            context=context,
            wire=wire,
            failure=failure,
            event=event,
            account=account.id,
            account_name=account.display_name,
            store=social.workspace,
            db=database,
            registry=registry,
            client=client,
            gateway=gateway,
        )
        await gateway.close()


def sends(env):
    return [item for item in env.wire if item[0].startswith("send")]


async def test_native_recall_uses_original_presence_and_internal_event_in_topic(native_env):
    env = native_env
    await env.gateway.receive(update(2, chat=-10042, topic=17, text="!ai original topic"))
    if env.gateway._turns:
        await asyncio.gather(*tuple(env.gateway._turns))
    async with env.db.read_session() as session:
        source = await session.scalar(
            select(ChatEventModel)
            .where(ChatEventModel.id > env.event.id, ChatEventModel.direction == "inbound")
            .order_by(ChatEventModel.id.desc())
        )
        conversation = await session.get(
            CanonicalConversationModel, source.canonical_conversation_id
        )
    assert conversation.thread_id == "17"
    context = replace(
        env.context,
        call_id="topic-send",
        conversation_id=conversation.id,
        trigger_event_id=source.id,
    )
    sent = await env.service.execute(
        "send", {"text": "original reply", "reply_to_event_id": source.id}, context
    )
    assert sent["status"] == "succeeded"
    send_params = sends(env)[-1][1]
    assert send_params["message_thread_id"] == 17
    assert send_params["reply_parameters"] == {"message_id": 2}
    async with env.db.read_session() as session:
        event = await session.get(ChatEventModel, sent["event_id"])
        original = (
            event.content,
            event.segments_json,
            event.platform_message_id,
            event.canonical_conversation_id,
            event.author_presence_id,
            event.reply_to_event_id,
        )
    assert event.author_presence_id == source.ingress_presence_id
    assert event.canonical_conversation_id == conversation.id
    assert await env.service.router.cas_takeover_space(conversation.space_id) == "taken"
    other_requests = []

    def other_http(request):
        other_requests.append(request)
        return httpx.Response(200, json={"ok": True, "result": {"id": 80002}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(other_http)) as transport:
        other = TelegramClient("another-offline-token", client=transport)
        await other.get_me()
        async with env.db.immediate_session() as session:
            other_presence = await create_presence(
                session,
                external_account_id=other.self_id,
                platform="telegram",
                now=datetime.now(UTC),
            )
            route = await session.get(SpaceActiveRouteModel, conversation.space_id)
            route.presence_id = other_presence.id
        env.registry.connect(other, provider_id="telegram", presence_id=other_presence.id)
        other_requests.clear()
        recall_context = replace(context, call_id="topic-recall")
        args = {"event_id": sent["event_id"]}
        result = await env.service.execute("recall_own_message", args, recall_context)
        assert result["status"] == "succeeded"
        assert result["presence_id"] == source.ingress_presence_id
        assert await env.service.execute("recall_own_message", args, recall_context) == result
        assert not other_requests
    deletes = [params for method, params, _ in env.wire if method == "deleteMessage"]
    assert deletes == [{"chat_id": "-10042", "message_id": int(original[2].rsplit(":", 1)[-1])}]
    async with env.db.read_session() as session:
        kept = await session.get(ChatEventModel, sent["event_id"])
        original_source = await session.get(ChatEventModel, source.id)
        topic = await session.get(CanonicalConversationModel, conversation.id)
    assert (
        kept.content,
        kept.segments_json,
        kept.platform_message_id,
        kept.canonical_conversation_id,
        kept.author_presence_id,
        kept.reply_to_event_id,
    ) == original
    assert original_source.content == "!ai original topic"
    assert topic.thread_id == "17"


async def test_native_text_limit_counts_decoded_mentions_and_preserves_reply(native_env):
    env = native_env
    text = "<&你好>" * 1200
    args = {
        "text": f"@{{account:{env.account}}}" + text,
        "reply_to_event_id": env.event.id,
    }
    result = await env.service.execute("send", args, env.context)
    assert result["status"] == "succeeded" and result["planned_messages"] == 2
    wire = sends(env)
    assert len(wire) == 2 and all(len(visible(item[1]["text"])) <= 4096 for item in wire)
    assert '<a href="tg://user?id=42">' in wire[0][1]["text"]
    assert wire[0][1]["reply_parameters"] == {"message_id": 1}
    assert "reply_parameters" not in wire[1][1] and "<a " not in wire[1][1]["text"]
    assert (
        "".join(part["delivered_text"] for part in result["parts"]) == "@" + env.account_name + text
    )
    assert await env.service.execute("send", args, env.context) == result
    assert len(sends(env)) == 2


async def test_native_short_file_caption_is_one_atomic_receipt(native_env):
    env = native_env
    artifact = snapshot_bytes(env.store, "native.txt", b"original file")
    args = {
        "artifact_id": artifact["artifact_id"],
        "attachment_kind": "file",
        "text": "x" * 1024,
        "reply_to_event_id": env.event.id,
    }
    result = await env.service.execute("send", args, env.context)
    assert result["status"] == "succeeded" and "parts" not in result
    assert len(sends(env)) == 1
    method, params, files = sends(env)[0]
    assert method == "sendDocument" and files["document"] == b"original file"
    assert params["caption"] == args["text"]
    assert params["reply_parameters"] == {"message_id": 1}
    assert await env.service.execute("send", args, env.context) == result
    assert len(sends(env)) == 1


@pytest.mark.parametrize("fail_caption", [False, True])
async def test_native_long_caption_has_distinct_facts_and_never_reuploads(native_env, fail_caption):
    env = native_env
    artifact = snapshot_bytes(env.store, "long.txt", b"immutable")
    args = {
        "artifact_id": artifact["artifact_id"],
        "attachment_kind": "file",
        "text": f"@{{account:{env.account}}}" + "长" * 5000,
    }
    if fail_caption:
        env.failure.method = "sendMessage"
    result = await env.service.execute("send", args, env.context)
    assert result["parts"][0]["status"] == "succeeded"
    assert result["parts"][1]["status"] == ("failed" if fail_caption else "succeeded")
    assert [item[0] for item in sends(env)] == ["sendDocument", "sendMessage"]
    assert len(visible(sends(env)[0][1]["caption"])) == 1024
    assert len(visible(sends(env)[1][1]["text"])) <= 4096
    if fail_caption:
        assert result["error"] == "TelegramAPIError"
    else:
        assert (
            "".join(part["delivered_text"] for part in result["parts"])
            == "@" + env.account_name + "长" * 5000
        )
    assert await env.service.execute("send", args, env.context) == result
    assert len(sends(env)) == 2


async def test_native_timeout_is_unknown_and_same_call_never_retries(native_env):
    env = native_env
    artifact = snapshot_bytes(env.store, "unknown.txt", b"original")
    env.failure.method, env.failure.timeout = "sendDocument", True
    args = {"artifact_id": artifact["artifact_id"], "attachment_kind": "file", "text": "caption"}
    result = await env.service.execute("send", args, env.context)
    assert result["status"] == "uncertain"
    assert await env.service.execute("send", args, env.context) == result
    assert len(sends(env)) == 1
    async with env.db.read_session() as reader:
        rows = list(await reader.scalars(select(SocialOperationModel)))
    assert len(rows) == 1 and rows[0].status == "uncertain" and rows[0].event_id is None


async def test_qq_file_caption_keeps_canonical_segments_and_does_not_reupload(
    database, tmp_path, monkeypatch
):
    env = await social_env(database, tmp_path)
    async with database.read_session() as reader:
        binding = await reader.scalar(
            select(IdentityBindingModel).where(IdentityBindingModel.account_id == env.account)
        )
    artifact = snapshot_bytes(env.store, "qq.txt", b"original")
    args = {
        "artifact_id": artifact["artifact_id"],
        "attachment_kind": "file",
        "text": f"@{{account:{env.account}}}附言",
    }
    result = await env.service.execute("send", args, env.context)
    assert result["parts"][0]["status"] == result["parts"][1]["status"] == "succeeded"
    async with database.read_session() as reader:
        caption = await reader.get(ChatEventModel, result["parts"][1]["event_id"])
    segments = json.loads(caption.segments_json)
    mention = next(item["data"] for item in segments if item["type"] == "mention")
    assert mention["account_id"] == env.account
    assert mention["person_id"] == env.person
    assert mention["assignment_id"] == binding.id
    assert mention["presence_id"] is None and mention["kind"] == "member"
    assert caption.content[mention["text_start"] : mention["text_end"]].startswith("@")
    assert caption.content.endswith("附言")
    sent_caption = next(params for method, params in env.bot.calls if method == "send_group_msg")
    assert sent_caption["message"][0] == {"type": "at", "data": {"qq": "10001"}}
    original_segments = caption.segments_json
    async with database.immediate_session() as session:
        original_plans = list(
            await session.execute(
                select(SocialOperationModel.id, SocialOperationModel.prepared_json).order_by(
                    SocialOperationModel.id
                )
            )
        )
        physical = await session.get(AccountModel, env.account)
        recipient = await session.scalar(
            select(IdentityBindingModel)
            .join(AccountModel)
            .where(AccountModel.platform == "qq", AccountModel.external_account_id == "10002")
        )
        owner = await session.get(CanonicalPersonModel, recipient.person_id)
        replacement = await replace_assignment(
            session, physical, binding.id, owner, now=datetime.now(UTC)
        )
        assert replacement.id != binding.id and replacement.person_id != env.person

    async def must_not_prepare_again(*args, **kwargs):
        raise AssertionError("successful captions retain the original account and assignment")

    monkeypatch.setattr("yuki.social.service.prepare_for_entry", must_not_prepare_again)
    assert await env.service.execute("send", args, env.context) == result
    assert sum(action == "upload_group_file" for action, _ in env.bot.calls) == 1
    assert sum(action == "send_group_msg" for action, _ in env.bot.calls) == 1
    async with database.read_session() as reader:
        preserved = await reader.get(ChatEventModel, caption.id)
        plans = list(
            await reader.execute(
                select(SocialOperationModel.id, SocialOperationModel.prepared_json).order_by(
                    SocialOperationModel.id
                )
            )
        )
    assert preserved.segments_json == original_segments and plans == original_plans


@pytest.mark.parametrize("caption_failure", ["failed", "timeout", "preflight"])
async def test_qq_confirmed_file_is_not_reuploaded_when_caption_fails(
    database, tmp_path, monkeypatch, caption_failure
):
    env = await social_env(database, tmp_path)
    artifact = snapshot_bytes(env.store, "confirmed.txt", b"original")
    args = {"artifact_id": artifact["artifact_id"], "attachment_kind": "file", "text": "附言"}
    original_call, original_effect = env.bot.call_api, env.service._effect

    class Rejected(RuntimeError):
        uncertain = False

    async def fail_caption(action, **params):
        if action == "send_group_msg":
            env.bot.calls.append((action, params))
            if caption_failure == "failed":
                raise Rejected
            raise httpx.ReadTimeout("offline lost acknowledgement")
        return await original_call(action, **params)

    async def fail_preflight(name, arguments, context, *rest, **kwargs):
        if context.sequence_part_index == 1:
            raise SocialError("permission_denied")
        return await original_effect(name, arguments, context, *rest, **kwargs)

    if caption_failure == "preflight":
        monkeypatch.setattr(env.service, "_effect", fail_preflight)
    else:
        monkeypatch.setattr(env.bot, "call_api", fail_caption)
    result = await env.service.execute("send", args, env.context)
    assert result["parts"][0]["status"] == "succeeded"
    assert (
        result["parts"][1]["status"]
        == {"failed": "failed", "timeout": "uncertain", "preflight": "prepared"}[caption_failure]
    )
    assert result["error"] == (
        "delivery_uncertain"
        if caption_failure == "preflight"
        else result["parts"][1]["error_category"]
    )
    env.registry.disconnect(env.bot)
    assert await env.service.execute("send", args, env.context) == result
    assert sum(action == "upload_group_file" for action, _ in env.bot.calls) == 1


@pytest.mark.parametrize("lost_ack", [False, True])
async def test_sequence_replay_reads_confirmed_or_unknown_facts_while_disconnected(
    native_env, lost_ack
):
    env = native_env
    if lost_ack:
        env.failure.method, env.failure.timeout = "sendMessage", True
    args = {"text": "原始消息" * 1500}
    result = await env.service.execute("send", args, env.context)
    assert result["status"] == ("uncertain" if lost_ack else "succeeded")
    sent = len(sends(env))
    env.registry.disconnect(env.client)
    assert await env.service.execute("send", args, env.context) == result
    with pytest.raises(SocialError, match="idempotency_conflict"):
        await env.service.execute("send", {"text": "changed"}, env.context)
    assert len(sends(env)) == sent


async def test_retired_sequence_hash_never_executes_or_silently_reinterprets(native_env):
    env = native_env
    context = replace(
        env.context,
        runtime_snapshot=replace(
            env.context.runtime_snapshot,
            reply=replace(
                env.context.runtime_snapshot.reply, max_qq_message_chars=1000, hard_max_messages=8
            ),
        ),
    )
    args = {"text": "旧记录" * 1000}
    result = await env.service.execute("send", args, context)
    parent = await env.service.receipts.find(context.turn_id, context.call_id)
    old_payload = {
        "original": args,
        "chunks": OutboundMessageSplitter.render(args["text"], runtime=context.runtime_snapshot),
    }
    async with env.db.immediate_session() as writer:
        row = await writer.get(SocialOperationModel, parent.operation_id)
        row.payload_hash = payload_hash(
            parent.action, parent.target, old_payload, context.conversation_id
        )
    env.registry.disconnect(env.client)
    with pytest.raises(SocialError, match="idempotency_conflict"):
        await env.service.execute("send", args, context)
    assert result["status"] == "succeeded"
    assert len(sends(env)) == 3


async def test_never_dispatched_sequence_keeps_first_plan_across_config_changes(
    native_env, monkeypatch
):
    env = native_env
    args = {"text": "恢复" * 3000}
    original_prepare = env.service.receipts.prepare_sequence

    class BeforeDispatch(BaseException):
        pass

    async def interrupt_prepare(**kwargs):
        await original_prepare(**kwargs)
        raise BeforeDispatch

    monkeypatch.setattr(env.service.receipts, "prepare_sequence", interrupt_prepare)
    with pytest.raises(BeforeDispatch):
        await env.service.execute("send", args, env.context)
    parent = await env.service.receipts.find(env.context.turn_id, env.context.call_id)
    assert parent.status == "prepared" and not sends(env)
    monkeypatch.setattr(env.service.receipts, "prepare_sequence", original_prepare)
    context = replace(
        env.context,
        runtime_snapshot=replace(
            env.context.runtime_snapshot,
            reply=replace(
                env.context.runtime_snapshot.reply, max_qq_message_chars=1000, hard_max_messages=8
            ),
        ),
    )
    result = await env.service.execute("send", args, context)
    assert result["operation_id"] == parent.operation_id
    assert result["status"] == "succeeded" and result["planned_messages"] == 2
    assert len(sends(env)) == 2
    assert "".join(part["delivered_text"] for part in result["parts"]) == args["text"]


async def test_confirmed_prefix_does_not_dispatch_missing_tail_on_recovery(native_env, monkeypatch):
    env = native_env
    args = {"text": "原调用" * 2000}
    original = env.service._effect

    class Interrupted(BaseException):
        pass

    async def stop_before_tail(name, arguments, context, *rest, **kwargs):
        if context.sequence_part_index == 1:
            raise Interrupted
        return await original(name, arguments, context, *rest, **kwargs)

    monkeypatch.setattr(env.service, "_effect", stop_before_tail)
    with pytest.raises(Interrupted):
        await env.service.execute("send", args, env.context)
    assert len(sends(env)) == 1
    env.registry.disconnect(env.client)
    result = await env.service.execute("send", args, env.context)
    assert result["sent_messages"] == 1 and result["parts"][1]["status"] == "prepared"
    assert len(sends(env)) == 1


@pytest.mark.parametrize("old_limit,new_limit", [(1000, 4000), (4000, 1000)])
async def test_prepared_layout_replacement_cannot_dispatch_stale_child(
    native_env, monkeypatch, old_limit, new_limit
):
    env = native_env
    args = {"text": "消息" * 1500}
    old_chunks = tuple(
        args["text"][i : i + old_limit] for i in range(0, len(args["text"]), old_limit)
    )
    target = await env.service.target("space", {}, env.context)
    route = await env.service.send_route(target, env.context)
    parent, _ = await env.service.receipts.prepare_sequence(
        source_turn_id=env.context.turn_id,
        tool_call_id=env.context.call_id,
        source_conversation_id=env.context.conversation_id,
        target=target,
        payload=args,
        parts=tuple({"text": chunk} for chunk in old_chunks),
        prepared={
            "message": transport_payload(TransportMessage(args["text"])),
            "parts": [transport_payload(TransportMessage(chunk)) for chunk in old_chunks],
            "route": env.service._route_payload(route),
        },
    )
    index = len(old_chunks) - 1
    old_context = replace(
        env.context,
        call_id=f"{sequence_prefix(env.context.call_id)}{index}",
        sequence_part_index=index,
        parent_operation_id=parent.operation_id,
    )
    entered, release = asyncio.Event(), asyncio.Event()
    original_prepare = env.service.receipts.prepare
    old_id = []

    async def hold_old_prepared(**kwargs):
        receipt = await original_prepare(**kwargs)
        if kwargs["tool_call_id"] == old_context.call_id and not entered.is_set():
            old_id.append(receipt.operation_id)
            entered.set()
            await release.wait()
        return receipt

    monkeypatch.setattr(env.service.receipts, "prepare", hold_old_prepared)
    old = asyncio.create_task(
        env.service._effect(
            "send_message",
            {"text": old_chunks[index]},
            old_context,
            target,
            route,
            "send",
            {"message": TransportMessage(old_chunks[index])},
            content=old_chunks[index],
            ledger_segments=({"type": "text", "data": {"text": old_chunks[index]}},),
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), 2)
        context = replace(
            env.context,
            runtime_snapshot=replace(
                env.context.runtime_snapshot,
                reply=replace(
                    env.context.runtime_snapshot.reply,
                    max_qq_message_chars=new_limit,
                    hard_max_messages=8,
                ),
            ),
        )
        result = await env.service.execute("send", args, context)
        assert result["status"] == "succeeded"
    finally:
        release.set()
        stale_result = await old
    assert "".join(visible(item[1]["text"]) for item in sends(env)) == args["text"]
    assert len(sends(env)) == len(old_chunks)
    assert stale_result["operation_id"] == old_id[0]
    async with env.db.read_session() as reader:
        original_row = await reader.get(SocialOperationModel, old_id[0])
        assert original_row is not None


async def test_work_unclaimed_delivery_replans_without_resetting_original_budget(
    native_env, monkeypatch
):
    from yuki.runtime.work_activation import current_work_control
    from yuki.runtime.work_budget import charge
    from yuki.runtime.work_budget_schema import budgets
    from yuki.runtime.work_control import WorkControl
    from yuki.runtime.work_repository import WorkRepository

    env = native_env
    repository = WorkRepository(env.db)
    lease = await repository.acquire(env.context.conversation_id, 1)
    source = {"origin": "user_message", "actor_person_id": env.event.author_person_id}
    control = WorkControl(repository, lease, "native-original", source, AsyncMock())
    control.current = await repository.accept(
        lease, source_key="native-original", source=source, goal="deliver"
    )
    identity = control.current["id"]
    async with env.db.immediate_session() as writer:
        await charge(writer, identity, models=3, tools=7)
    token = current_work_control.set(control)
    original_claim = env.service.receipts.claim

    class BeforeClaim(RuntimeError):
        pass

    async def stop_before_claim(*args, **kwargs):
        raise BeforeClaim

    args = {"text": "Work恢复" * 1000}
    try:
        monkeypatch.setattr(env.service.receipts, "claim", stop_before_claim)
        with pytest.raises(BeforeClaim):
            await env.service.execute("send", args, env.context)
        monkeypatch.setattr(env.service.receipts, "claim", original_claim)
        context = replace(
            env.context,
            runtime_snapshot=replace(
                env.context.runtime_snapshot,
                reply=replace(
                    env.context.runtime_snapshot.reply,
                    max_qq_message_chars=1000,
                    hard_max_messages=8,
                ),
            ),
        )
        result = await env.service.execute("send", args, context)
        assert result["status"] == "succeeded"
        assert control.current["id"] == identity
        async with env.db.read_session() as reader:
            budget = (
                (await reader.execute(select(budgets).where(budgets.c.root_id == identity)))
                .mappings()
                .one()
            )
        assert budget["models"] == 3 and budget["tools"] == 7
        assert control.current["sent_messages"] == len(sends(env))
    finally:
        current_work_control.reset(token)
        await repository.release(lease)


@pytest.mark.parametrize("lost_ack", [False, True])
async def test_work_dispatched_plan_and_reservations_are_readonly_on_recovery(native_env, lost_ack):
    from yuki.runtime.work_activation import current_work_control
    from yuki.runtime.work_control import WorkControl
    from yuki.runtime.work_recovery_schema import deliveries
    from yuki.runtime.work_repository import WorkRepository

    env = native_env
    repository = WorkRepository(env.db)
    lease = await repository.acquire(env.context.conversation_id, 1)
    source = {"origin": "user_message", "actor_person_id": env.event.author_person_id}
    control = WorkControl(repository, lease, "native-paid", source, AsyncMock())
    control.current = await repository.accept(
        lease, source_key="native-paid", source=source, goal="deliver"
    )
    token = current_work_control.set(control)
    if lost_ack:
        env.failure.method, env.failure.timeout = "sendMessage", True
    args = {"text": "原有Work" * 1000}
    try:
        result = await env.service.execute("send", args, env.context)

        async def persisted_facts():
            async with env.db.read_session() as reader:
                operations = list(
                    await reader.scalars(
                        select(SocialOperationModel).order_by(SocialOperationModel.id)
                    )
                )
                intents = (
                    (await reader.execute(select(deliveries).order_by(deliveries.c.id)))
                    .mappings()
                    .all()
                )
            return (
                [(row.id, row.status, row.payload_hash, row.planned_parts) for row in operations],
                [dict(row) for row in intents],
            )

        original = await persisted_facts()
        sent, count = len(sends(env)), control.current["sent_messages"]
        context = replace(
            env.context,
            runtime_snapshot=replace(
                env.context.runtime_snapshot,
                reply=replace(
                    env.context.runtime_snapshot.reply,
                    max_qq_message_chars=1000,
                    hard_max_messages=8,
                ),
            ),
        )
        env.registry.disconnect(env.client)
        assert await env.service.execute("send", args, context) == result
        assert await persisted_facts() == original
        assert len(sends(env)) == sent and control.current["sent_messages"] == count
    finally:
        current_work_control.reset(token)
        await repository.release(lease)


async def prepare_paid_send(env, args):
    from yuki.runtime.effect_outcomes import ResultCapture
    from yuki.runtime.work_control import WorkControl
    from yuki.runtime.work_repository import WorkRepository

    repository = WorkRepository(env.db)
    lease = await repository.acquire(env.context.conversation_id, 1)
    source = {"origin": "user_message"}
    control = WorkControl(repository, lease, "native-t3", source, AsyncMock())
    control.current = await repository.accept(
        lease, source_key="native-t3", source=source, goal="send"
    )
    call = ToolCall("paid-file", ToolFunction("send", json.dumps(args)))
    invocation = direct_invocations(
        (call,),
        SimpleNamespace(work_control=control),
        chain_id="original-native",
        request_sequence=1,
    )[0]
    key = invocation.identity.operation_id
    assert await repository.prepare_effect(
        lease, control.current["id"], key, "tool", invocation=invocation.durable_metadata()
    )
    assert await repository.admit_dispatch(lease, control.current["id"], key, charge=True)
    return (
        control,
        ResultCapture(control.current["id"], key),
        replace(env.context, turn_id=control.current["id"], call_id=key),
    )


@pytest.mark.parametrize("long_caption,fail_tail", [(False, False), (True, False), (True, True)])
async def test_native_t3_recovery_preserves_exact_media_and_caption_facts(
    native_env, long_caption, fail_tail
):
    from yuki.runtime.effect_outcomes import current_result_capture, execution_evidence
    from yuki.runtime.work_activation import current_work_control
    from yuki.runtime.work_journal import WorkJournal
    from yuki.runtime.work_schema_v1 import effects

    env = native_env
    artifact = snapshot_bytes(env.store, "paid.txt", b"immutable original")
    args = {
        "artifact_id": artifact["artifact_id"],
        "attachment_kind": "file",
        "text": "附言" * (2500 if long_caption else 10),
    }
    control, capture, context = await prepare_paid_send(env, args)
    if fail_tail:
        env.failure.method = "sendMessage"
    work_token, capture_token = (
        current_work_control.set(control),
        current_result_capture.set(capture),
    )
    try:
        result = await env.service.execute("send", args, context)
    finally:
        current_result_capture.reset(capture_token)
        current_work_control.reset(work_token)
    try:
        # No T3 recording: restore from the actual T2 intent and bound Social facts.
        async with env.db.read_session() as reader:
            state = await reader.scalar(
                select(effects.c.state).where(effects.c.effect_key == context.call_id)
            )
        assert state == "prepared"
        count = len(sends(env))
        env.registry.disconnect(env.client)
        recovered = json.loads(await WorkJournal(control.repository).effect_result(context.call_id))
        for response in ({"ok": result["status"] == "succeeded", "data": result}, recovered):
            fact = execution_evidence(
                normalize_legacy_result(json.dumps(response), provider_id="core", tool_name="send"),
                tool="send",
                side_effecting=True,
                arguments=json.dumps(args),
            )
            assert fact["delivered_artifacts"] == [artifact["artifact_id"]]
            assert fact["caption_delivered"] is (not fail_tail)
        assert len(sends(env)) == count
    finally:
        await control.repository.release(control.lease)


@pytest.mark.parametrize("caption_confirmed", [False, True])
async def test_legacy_qq_file_caption_layout_t3_recovery_reads_original_ledger(
    database, tmp_path, caption_confirmed
):
    from yuki.runtime.delivery_intents import reserve
    from yuki.runtime.effect_outcomes import current_result_capture, execution_evidence
    from yuki.runtime.work_journal import WorkJournal

    env = await social_env(database, tmp_path)
    artifact = snapshot_bytes(env.store, "legacy.txt", b"original legacy file")
    args = {"artifact_id": artifact["artifact_id"], "attachment_kind": "file", "text": "原附言"}
    control, capture, context = await prepare_paid_send(env, args)
    target = await env.service.target("space", {}, context)
    route = await env.service.send_route(target, context)
    parent = await env.service.receipts.prepare(
        source_turn_id=context.turn_id,
        tool_call_id=context.call_id,
        source_conversation_id=context.conversation_id,
        action="send_message",
        target=target,
        payload=args,
        work_effect=capture,
    )
    # The old producer reserved both actual wire steps before its original send.
    await reserve(
        control,
        parent.operation_id,
        "artifact",
        {"target": target.model_dump(mode="json"), "arguments": args},
        count=2,
    )
    capture_token = current_result_capture.set(capture)
    try:
        await env.service._effect(
            "send_message",
            args,
            context,
            target,
            route,
            "send",
            {
                "message": TransportMessage(
                    media=TransportMedia(
                        "file", b"original legacy file", "legacy.txt", "text/plain"
                    )
                )
            },
            content="[文件: legacy.txt]",
            ledger_segments=({"type": "file", "data": {"artifact_id": artifact["artifact_id"]}},),
        )
        if caption_confirmed:
            await env.service._effect(
                "send_file_caption",
                {"text": "原附言", "segments": None, "target": None},
                replace(
                    context,
                    turn_id=f"social-caption:{parent.operation_id}",
                    call_id="caption",
                    parent_operation_id=parent.operation_id,
                ),
                target,
                route,
                "send",
                {"message": TransportMessage("原附言")},
                content="原附言",
                ledger_segments=({"type": "text", "data": {"text": "原附言"}},),
            )
    finally:
        current_result_capture.reset(capture_token)
    try:
        env.registry.disconnect(env.bot)
        recovered = json.loads(await WorkJournal(control.repository).effect_result(context.call_id))
        fact = execution_evidence(
            normalize_legacy_result(json.dumps(recovered), provider_id="core", tool_name="send"),
            tool="send",
            side_effecting=True,
            arguments=json.dumps(args),
        )
        assert fact["delivered_artifacts"] == [artifact["artifact_id"]]
        assert fact["caption_delivered"] is caption_confirmed
        assert sum(action == "upload_group_file" for action, _ in env.bot.calls) == 1
    finally:
        await control.repository.release(control.lease)


async def assert_committed_ack_loss_preserves_delivery(env, monkeypatch):
    import sqlite3

    from sqlalchemy.exc import OperationalError
    from sqlalchemy.ext.asyncio import AsyncSession

    from yuki.social.models import OperationStatus

    original_finish, original_commit = env.service.receipts.finish, AsyncSession.commit

    async def mark_commit(*args, **kwargs):
        result = await original_finish(*args, **kwargs)
        if kwargs.get("status") is OperationStatus.SUCCEEDED:
            kwargs["session"].info["native_lost_ack"] = True
        return result

    async def lost_ack(session):
        await original_commit(session)
        if session.info.pop("native_lost_ack", False):
            raise OperationalError("COMMIT", {}, sqlite3.OperationalError("offline lost ack"))

    monkeypatch.setattr(env.service.receipts, "finish", mark_commit)
    monkeypatch.setattr(AsyncSession, "commit", lost_ack)
    args = {"text": "confirmed before local ack loss"}
    result = await env.service.execute("send", args, env.context)
    assert result["status"] == "succeeded" and result["delivered_text"] == args["text"]
    assert await env.service.execute("send", args, env.context) == result


async def test_telegram_committed_receipt_does_not_depend_on_local_ack(native_env, monkeypatch):
    await assert_committed_ack_loss_preserves_delivery(native_env, monkeypatch)
    assert len(sends(native_env)) == 1


async def test_qq_committed_receipt_does_not_depend_on_local_ack(database, tmp_path, monkeypatch):
    env = await social_env(database, tmp_path)
    await assert_committed_ack_loss_preserves_delivery(env, monkeypatch)
    assert sum(action == "send_group_msg" for action, _ in env.bot.calls) == 1


async def test_two_original_executors_cannot_mix_changed_tail_layouts(native_env, monkeypatch):
    env = native_env
    args = {"text": "原" * 2500}

    def context(hard_max):
        return replace(
            env.context,
            runtime_snapshot=replace(
                env.context.runtime_snapshot,
                reply=replace(
                    env.context.runtime_snapshot.reply,
                    max_qq_message_chars=1000,
                    hard_max_messages=hard_max,
                ),
            ),
        )

    old_wait, old_release = asyncio.Event(), asyncio.Event()
    new_wait, new_release = asyncio.Event(), asyncio.Event()
    original_prepare, original_plan = (
        env.service.receipts.prepare,
        env.service.receipts.prepare_sequence,
    )
    plans = 0

    async def hold_first_receipt(**kwargs):
        result = await original_prepare(**kwargs)
        if not old_wait.is_set():
            old_wait.set()
            await old_release.wait()
        return result

    async def hold_second_plan(**kwargs):
        nonlocal plans
        result = await original_plan(**kwargs)
        plans += 1
        if plans == 2:
            new_wait.set()
            await new_release.wait()
        return result

    monkeypatch.setattr(env.service.receipts, "prepare", hold_first_receipt)
    monkeypatch.setattr(env.service.receipts, "prepare_sequence", hold_second_plan)
    old = asyncio.create_task(env.service.execute("send", args, context(3)))
    new = None
    try:
        await asyncio.wait_for(old_wait.wait(), 2)
        new = asyncio.create_task(env.service.execute("send", args, context(2)))
        await asyncio.wait_for(new_wait.wait(), 2)
        old_release.set()
        await asyncio.wait_for(old, 3)
        new_release.set()
        result = await asyncio.wait_for(new, 3)
        assert result["status"] == "succeeded"
        text = "".join(visible(item[1]["text"]) for item in sends(env))
        assert text.replace("\n", "") == args["text"]
        assert len(sends(env)) == 3
    finally:
        old_release.set()
        new_release.set()
        await asyncio.gather(
            old, *(tuple([new]) if new is not None else ()), return_exceptions=True
        )


@pytest.mark.parametrize("caption", ["", "文件附言"])
async def test_qq_file_reply_is_recorded_only_on_the_actual_caption_part(
    database, tmp_path, caption
):
    env = await social_env(database, tmp_path)
    async with database.read_session() as reader:
        source = await reader.scalar(
            select(ChatEventModel).where(ChatEventModel.direction == "inbound")
        )
    artifact = snapshot_bytes(env.store, "quoted.txt", b"original file")
    result = await env.service.execute(
        "send",
        {
            "artifact_id": artifact["artifact_id"],
            "attachment_kind": "file",
            "text": caption,
            "reply_to_event_id": source.id,
        },
        env.context,
    )
    assert result["status"] == "succeeded"
    async with database.read_session() as reader:
        events = list(
            await reader.scalars(
                select(ChatEventModel)
                .where(ChatEventModel.direction == "outbound")
                .order_by(ChatEventModel.id)
            )
        )
    assert events[0].reply_to_event_id is None and events[0].reply_to_message_id is None
    assert len(events) == (2 if caption else 1)
    if caption:
        assert events[1].reply_to_event_id == source.id
        assert env.bot.calls[-1][1]["message"][0] == {
            "type": "reply",
            "data": {"id": source.platform_message_id},
        }
    original_effects = list(env.bot.calls)
    assert (
        await env.service.execute(
            "send",
            {
                "artifact_id": artifact["artifact_id"],
                "attachment_kind": "file",
                "text": caption,
                "reply_to_event_id": source.id,
            },
            env.context,
        )
        == result
    )
    assert env.bot.calls == original_effects
