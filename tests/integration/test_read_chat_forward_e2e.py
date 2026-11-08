"""read_chat reads one merged-forward layer through the original QQ connection, end to end.

Failure cases were listed before the implementation in
docs/operations/read-chat-forward-20261108.md (numbers below refer to it).

The stored outer event mirrors production row 135443: SnowLuma pushes only
``{"type": "forward", "data": {"id": …}}``. A fake OneBot connection answers
``get_forward_msg`` (text node, image node, nested forward node, failure) and
``get_image``; everything else is the real SQLite ledger, SnowLuma Provider,
Registry, Social read kernel, AgentToolService, Main loop and media cache.

Artifacts: ``test-results/read-chat-forward/{main-loop,failures}.json``
(commit and worktree state recorded; UUIDs normalized).
"""

import asyncio
import base64
import io
import json
import re
import subprocess
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from PIL import Image
from sqlalchemy import delete, select
from tests.conftest import MemorySender, build_harness, make_settings
from tests.support.event_seed import seed_scoped_event
from tests.support.social_identity_cases import social_env

from yuki.capabilities.invocation import ToolInvocationContext, current_invocation
from yuki.conversation.scope import ConversationTurnSnapshot
from yuki.domain.conversations import ConversationScope, ScopeType
from yuki.domain.messages import (
    ChatResponse,
    InboundMessage,
    SenderIdentity,
    ToolCall,
    ToolFunction,
)
from yuki.llm.fake import FakeLLMProvider
from yuki.persistence.models import ChatEventModel, ConversationMediaItemModel
from yuki.services.agent_tools import ToolRuntime
from yuki.services.main_agent_contract import MainAgentContract
from yuki.tool_results.artifacts import ToolArtifactRepository
from yuki.workspace.short_state import ShortState

ROOT = Path(__file__).resolve().parents[2]
ARTIFACT = ROOT / "test-results" / "read-chat-forward"
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
# Seeding time and artifact handles differ per run; the forward's own times do not.
_RUN_FACTS = (
    (re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d+\+08:00"), "<seeded-at>"),
    (re.compile(r"\b[0-9a-f]{32}\b"), "<artifact-handle>"),
)
SIGNED_URL = "https://multimedia.nt.qq.com.cn/download?rkey=signed-secret"


def _png() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (4, 4), (200, 30, 30)).save(buffer, format="PNG")
    return buffer.getvalue()


PNG = _png()

# One reply of each envelope the OneBot implementations use (case 11, 13).
FIRST_LAYER = {
    "messages": [
        {
            "sender": {"user_id": 30001, "nickname": "群友甲"},
            "time": 1762560000,
            "message": [{"type": "text", "data": {"text": "第一层校验词：蓝鲸731"}}],
        },
        {
            "sender": {"user_id": 30002, "nickname": "群友乙", "card": "乙的群名片"},
            "time": 1762560060,
            "message": [
                {"type": "text", "data": {"text": "看这张图"}},
                {"type": "image", "data": {"file": "inner-photo.png", "file_size": 77}},
                {
                    "type": "file",
                    "data": {
                        "file": "report.pdf",
                        "name": "report.pdf",
                        "url": SIGNED_URL,
                        "file_size": 12,
                    },
                },
            ],
        },
        {
            "type": "node",
            "data": {
                "nickname": "群友丙",
                "content": [{"type": "forward", "data": {"id": "fwd-inner-never-read"}}],
            },
        },
    ]
}


def _revision() -> dict:
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True, cwd=ROOT).strip()
    dirty = subprocess.check_output(["git", "status", "--porcelain"], text=True, cwd=ROOT)
    return {"commit": head, "worktree_dirty": bool(dirty.strip())}


def _write(name: str, value: dict) -> None:
    ARTIFACT.mkdir(parents=True, exist_ok=True)
    seen: dict[str, str] = {}
    text = json.dumps({**_revision(), **value}, ensure_ascii=False, indent=2, sort_keys=True)
    text = _UUID.sub(lambda m: seen.setdefault(m.group(0), f"<uuid:{len(seen) + 1}>"), text)
    for pattern, placeholder in _RUN_FACTS:
        text = pattern.sub(placeholder, text)
    (ARTIFACT / name).write_text(text + "\n", encoding="utf-8")


def _answer_onebot(env, replies: dict) -> None:
    """The fake QQ connection: forward replies by locator, get_image by file."""
    original = env.bot.call_api

    async def call_api(action, **params):
        if action == "get_forward_msg":
            env.bot.calls.append((action, params))
            reply = replies[params["id"]]
            if isinstance(reply, BaseException):
                raise reply
            if callable(reply):
                return await reply()
            return reply
        if action == "get_image":
            env.bot.calls.append((action, params))
            assert params == {"file": "inner-photo.png"}
            return {"base64": base64.b64encode(PNG).decode()}
        return await original(action, **params)

    env.bot.call_api = call_api


async def _forward_event(env, segments, key="forwarded-record"):
    return (
        await seed_scoped_event(
            env.service.writer,
            scope=ConversationScope.group("80001", "20001"),
            origin_key=key,
            sender_user_id="10001",
            direction="inbound",
            content="[合并转发]",
            segments=tuple(segments),
        )
    ).event


def _forward(locator):
    return {"type": "forward", "data": {"id": locator}}


CONTEXT = {"type": "yuki_context", "data": {"platform": "qq"}}


async def _media_rows(database, event_id):
    async with database.read_session() as reader:
        return list(
            await reader.scalars(
                select(ConversationMediaItemModel)
                .where(ConversationMediaItemModel.source_event_id == event_id)
                .order_by(ConversationMediaItemModel.attachment_index)
            )
        )


def _tool_call(call_id, name, arguments):
    return ChatResponse(
        "", 0, tool_calls=(ToolCall(call_id, ToolFunction(name, json.dumps(arguments))),)
    )


async def _ask(env, database, provider, **settings):
    """A real group turn asking about the stored forward, on the fixed Main contract."""
    harness = build_harness(
        database, make_settings(database.url, enabled_groups_csv="20001", **settings), provider
    )
    chat = harness.processor._chat
    chat._tools.social_service = env.service
    chat._tools.workspace_service = env.service.workspace_service
    # The application wires its media cache to the same live connection Registry.
    env.service.workspace_service.conversation_media.gateway_registry = env.registry
    chat.runtime.runner.main_contract = MainAgentContract(chat, ShortState(env.store))
    chat._tool_artifacts = ToolArtifactRepository(
        database, env.store.root.parent / "tool-originals", retention_seconds=60
    )
    result = await harness.processor.handle(
        InboundMessage(
            ingress_key="ask-about-forward",
            native_message_id="ask-about-forward",
            event_type="message:test",
            scope_type=ScopeType.GROUP,
            sender=SenderIdentity("10001"),
            text="上面那份聊天记录里说了什么？",
            bot_user_id="80001",
            group_id="20001",
            mentions_bot=True,
            conversation_id=env.context.conversation_id,
            person_id=env.person,
            space_id=env.space,
            presence_id=env.presence,
        ),
        MemorySender(),
    )
    return result.reason


async def test_main_reads_first_forward_layer_and_views_inner_image_on_demand(database, tmp_path):
    env = await social_env(database, tmp_path)
    _answer_onebot(
        env,
        {"fwd-A": FIRST_LAYER, "fwd-B": RuntimeError("retcode=1200 消息已过期")},
    )
    # Two outer forwards share one locator (case 15); a third one fails (case 10, 16).
    outer = await _forward_event(
        env, [_forward("fwd-A"), _forward("fwd-A"), _forward("fwd-B"), CONTEXT]
    )
    seen: list[dict] = []

    def respond(request):
        returned = [
            json.loads(message.content) for message in request.messages if message.role == "tool"
        ]
        seen.extend(returned[len(seen) :])
        number = len(provider.requests)
        if number == 1:
            return _tool_call("read-forward", "read_chat", {"around": outer.id, "context": 0})
        if number == 2:
            image = returned[-1]["forwards"][0]["messages"][1]["attachments"][0]
            # Reading registered metadata only: nothing was downloaded (case 19).
            assert not any(action == "get_image" for action, _ in env.bot.calls)
            return _tool_call(
                "view-inner",
                "view_file",
                {"event_id": outer.id, "attachment": image["attachment"], "question": "图里是什么"},
            )
        return "读到了：蓝鲸731"

    provider = FakeLLMProvider(respond)
    env.bot.calls.clear()
    assert await _ask(env, database, provider) == "chat" and len(provider.requests) == 3
    history, viewed = seen
    assert "ok" not in history, history
    assert [row["event_id"] for row in history["events"]] == [outer.id]
    first, duplicate, failed = history["forwards"]
    assert first["segment"] == 0 and duplicate["segment"] == 1
    assert first["messages"] == duplicate["messages"]
    text, photo, nested = first["messages"]
    assert text == {
        "author_name": "群友甲",
        "occurred_at": "2025-11-08T08:00:00+08:00",
        "content": "第一层校验词：蓝鲸731",
    }
    assert photo["author_name"] == "乙的群名片" and photo["content"] == "看这张图"
    image, document = photo["attachments"]
    # Slots follow the three numbered outer forward segments (case 20).
    assert image == {"attachment": 3, "kind": "image", "size": 77}
    assert document == {"attachment": 4, "kind": "file", "name": "report.pdf", "size": 12}
    assert nested == {"author_name": "群友丙", "nested_forwards": 1}
    assert failed == {"segment": 2, "error": "forward_read_failed"}
    # One native read per distinct locator, never the inner forward (case 15, 17).
    forward_calls = [
        params["id"] for action, params in env.bot.calls if action == "get_forward_msg"
    ]
    assert sorted(forward_calls) == ["fwd-A", "fwd-B"]
    rendered = json.dumps(history, ensure_ascii=False)
    for leaked in ("fwd-A", "fwd-B", "fwd-inner", "rkey", "30001", "30002", env.presence):
        assert leaked not in rendered  # case 28
    # view_file reads the inner image through the original receiving account (case 24).
    assert ("get_image", {"file": "inner-photo.png"}) in env.bot.calls, viewed
    assert viewed.get("ok") is not False, viewed
    assert not any(message.images for message in provider.requests[1].messages)
    rows = await _media_rows(database, outer.id)
    assert [(row.attachment_index, row.kind, row.segment_index) for row in rows] == [
        (3, "image", 0),
        (4, "file", 0),
    ]
    assert json.loads(rows[0].source_snapshot_json)["data"]["file"] == "inner-photo.png"

    # A later read of the same snapshot keeps its slot (case 21).
    again = await env.service.execute(
        "read_chat", {"around": outer.id, "context": 0}, replace(env.context, call_id="again")
    )
    assert again["forwards"][0]["messages"][1]["attachments"][0]["attachment"] == 3
    assert len(await _media_rows(database, outer.id)) == 2

    # Browsing and neighbours never reach the gateway (case 1).
    env.bot.calls.clear()
    browse = await env.service.execute("read_chat", {}, replace(env.context, call_id="browse"))
    assert outer.id in {row["event_id"] for row in browse["events"]}
    assert "forwards" not in browse and env.bot.calls == []
    _write(
        "main-loop.json",
        {
            "scenario": "Main reads read_chat(around) on a stored forward, then view_file",
            "outer_segments": [_forward("fwd-A"), _forward("fwd-A"), _forward("fwd-B"), CONTEXT],
            "model_saw_read_chat": history,
            "model_saw_view_file": {key: viewed[key] for key in sorted(viewed) if key != "result"},
            "forward_calls": sorted(forward_calls),
            "media_slots": [
                {"attachment": row.attachment_index, "kind": row.kind, "segment": row.segment_index}
                for row in rows
            ],
            "model_requests": len(provider.requests),
        },
    )


async def _read_tool(env, harness_tools, arguments, call_id):
    """The model-facing read_chat result through AgentToolService, in a real group turn."""
    async with env.db.read_session() as reader:
        trigger = await reader.scalar(select(ChatEventModel.id).order_by(ChatEventModel.id))
    message = InboundMessage(
        ingress_key=call_id,
        native_message_id=call_id,
        event_type="message:test",
        scope_type=ScopeType.GROUP,
        sender=SenderIdentity("10001"),
        text="读转发",
        bot_user_id="80001",
        group_id="20001",
        conversation_id=env.context.conversation_id,
        person_id=env.person,
        presence_id=env.presence,
        space_id=env.space,
        source_event_id=trigger,
    )
    snapshot = ConversationTurnSnapshot(env.context.conversation_id, call_id, 1, trigger, 1)
    runtime = ToolRuntime(message, turn_snapshot=snapshot)
    token = current_invocation.set(ToolInvocationContext(runtime=runtime, invocation_key=call_id))
    try:
        return (
            await harness_tools.execute(
                "read_chat", json.dumps({"context": 0, **arguments}), runtime
            )
        ).model_payload()
    finally:
        current_invocation.reset(token)


async def test_forward_failures_keep_their_real_errors(database, tmp_path):
    env = await social_env(database, tmp_path)
    harness = build_harness(database, make_settings(database.url, enabled_groups_csv="20001"))
    tools = harness.processor._chat._tools
    tools.social_service = env.service
    record: dict[str, object] = {}

    # Empty list is a successful empty layer; a missing locator fails alone (case 9, 12).
    _answer_onebot(env, {"fwd-empty": {"status": "ok", "retcode": 0, "data": {"messages": []}}})
    mixed = await _forward_event(
        env, [_forward("fwd-empty"), {"type": "forward", "data": {}}], key="mixed"
    )
    result = await _read_tool(env, tools, {"around": mixed.id}, "mixed")
    assert "forwards" in result, result
    assert result["forwards"] == [
        {"segment": 0, "messages": []},
        {"segment": 1, "error": "forward_locator_missing"},
    ]
    assert "ok" not in result
    record["empty_and_missing_locator"] = result["forwards"]

    # Every group failed: a real top-level failure keeping events and groups (case 10, 16).
    _answer_onebot(env, {"fwd-dead": {"status": "failed", "retcode": 1200, "data": None}})
    dead = await _forward_event(env, [_forward("fwd-dead")], key="dead")
    result = await _read_tool(env, tools, {"around": dead.id}, "dead")
    assert result["ok"] is False and result["error"] == "forward_read_failed"
    assert result["forwards"] == [{"segment": 0, "error": "forward_read_failed"}]
    assert [row["event_id"] for row in result["events"]] == [dead.id]
    record["all_failed"] = {key: result[key] for key in ("ok", "error", "message", "forwards")}

    # The original Presence offline: no other account is tried (case 6).
    env.bot.calls.clear()
    env.registry.disconnect(env.bot)
    result = await _read_tool(env, tools, {"around": dead.id}, "offline")
    assert result["error"] == "forward_presence_unavailable"
    assert env.bot.calls == []
    record["presence_offline"] = result["forwards"]
    env.registry.connect(env.bot, provider_id="snowluma", presence_id=env.presence)

    # A non-anchor reading never expands; an unreadable anchor never reaches the gateway (case 2).
    env.bot.calls.clear()
    result = await _read_tool(env, tools, {"around": 999999}, "unknown-anchor")
    assert result.get("around_status") == "unavailable" and "forwards" not in result
    assert env.bot.calls == []

    # Cancellation propagates and leaves no slot (case 18).
    started, release = asyncio.Event(), asyncio.Event()

    async def blocked():
        started.set()
        await release.wait()
        return FIRST_LAYER

    _answer_onebot(env, {"fwd-slow": blocked})
    slow = await _forward_event(env, [_forward("fwd-slow")], key="slow")
    task = asyncio.create_task(
        env.service.execute("read_chat", {"around": slow.id}, replace(env.context, call_id="slow"))
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await _media_rows(database, slow.id) == []

    # The outer event left the ledger while the gateway answered: no slot, no old body (case 22).
    async def erase_then_answer():
        async with database.immediate_session() as writer:
            await writer.execute(delete(ChatEventModel).where(ChatEventModel.id == erased.id))
        return FIRST_LAYER

    _answer_onebot(env, {"fwd-erased": erase_then_answer})
    erased = await _forward_event(env, [_forward("fwd-erased")], key="erased")
    result = await _read_tool(env, tools, {"around": erased.id}, "erased")
    assert result["ok"] is False and result["error"] == "history_source_changed"
    assert "蓝鲸731" not in json.dumps(result, ensure_ascii=False)
    assert await _media_rows(database, erased.id) == []
    record["source_changed"] = {key: result[key] for key in ("ok", "error")}

    # A connection without forward_read (Telegram) fails without any call (case 7, 8).
    record["provider_without_capability"] = await _telegram_forward(env, database, tools)
    _write("failures.json", record)


async def _telegram_forward(env, database, tools):
    import httpx

    from yuki.gateway.providers.telegram import TelegramClient
    from yuki.identity.canonical_repository import (
        create_person_binding,
        create_presence,
        create_space_binding,
    )

    wire = []

    def telegram(request):
        wire.append(request.url.path)
        return httpx.Response(200, json={"ok": True, "result": {}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(telegram)) as transport:
        client = TelegramClient("offline-test-token", client=transport)
        client.self_id = "88001"
        now = datetime.now(UTC)
        async with database.immediate_session() as session:
            presence = await create_presence(
                session, external_account_id=client.self_id, platform="telegram", now=now
            )
            group = await create_space_binding(
                session,
                external_space_id="-10088001",
                name="TG群",
                enabled=True,
                autonomous_enabled=True,
                require_mention=True,
                platform="telegram",
                now=now,
            )
            await create_person_binding(
                session,
                external_account_id="91001",
                display_name="TG群友",
                platform="telegram",
                now=now,
            )
        env.registry.connect(client, provider_id="telegram", presence_id=presence.id)
        tg_event = (
            await seed_scoped_event(
                env.service.writer,
                scope=ConversationScope.group(
                    client.self_id,
                    group.external_space_id,
                    platform="telegram",
                    presence_id=presence.id,
                ),
                origin_key="tg-forward",
                sender_user_id="91001",
                direction="inbound",
                content="forwarded",
                segments=(_forward("tg-locator"),),
            )
        ).event
        env.bot.calls.clear()
        result = await _read_tool(env, tools, {"around": tg_event.id}, "telegram")
    assert result["ok"] is False and result["error"] == "forward_unsupported"
    assert result["forwards"] == [{"segment": 0, "error": "forward_unsupported"}]
    assert wire == [] and env.bot.calls == []
    return result["forwards"]


async def test_replayed_receipt_reads_its_stored_forward_result(database, tmp_path):
    """Case 29: the original call key returns its persisted result, never a new read."""
    from tests.support.work_session import invoke_tool
    from tests.unit.test_tool_effect_audit import active_work

    env, work, runtime = await active_work(database, tmp_path)
    harness = build_harness(database, make_settings(database.url, enabled_groups_csv="20001"))
    tools = harness.processor._chat._tools
    tools.social_service = env.service
    _answer_onebot(env, {"fwd-A": FIRST_LAYER})
    outer = await _forward_event(env, [_forward("fwd-A")])
    call = ToolCall(
        "read-forward", ToolFunction("read_chat", json.dumps({"around": outer.id, "context": 0}))
    )

    async def execute():
        token = current_invocation.set(
            ToolInvocationContext(runtime=runtime, invocation_key=work.call_key(call.id))
        )
        try:
            result = await tools.execute("read_chat", call.function.arguments, runtime)
        finally:
            current_invocation.reset(token)
        return json.dumps(result.model_payload(), ensure_ascii=False)

    env.bot.calls.clear()
    original = json.loads(await invoke_tool(work, call, execute, side_effecting=False))
    assert "forwards" in original, original
    assert original["forwards"][0]["messages"][0]["content"] == "第一层校验词：蓝鲸731"
    assert [action for action, _ in env.bot.calls] == ["get_forward_msg"]
    # The connection now answers differently; the original call must not see it.
    _answer_onebot(env, {"fwd-A": {"messages": []}})
    env.bot.calls.clear()
    replayed = json.loads(await invoke_tool(work, call, execute, side_effecting=False))
    assert replayed == original and env.bot.calls == []
    _write(
        "replay.json",
        {
            "scenario": "same Work call key replayed after a forward read",
            "original": original,
            "replayed_equals_original": replayed == original,
            "gateway_calls_on_replay": env.bot.calls,
        },
    )


async def test_oversized_forward_layer_goes_through_the_common_result_budget(database, tmp_path):
    """Case 27: no node or text cap; the common budget keeps a preview and a read handle."""
    env = await social_env(database, tmp_path)
    nodes = [
        {"sender": {"nickname": f"群友{index}"}, "message": f"第{index}条 " + "长" * 2000}
        for index in range(60)
    ]
    _answer_onebot(env, {"fwd-long": {"data": {"messages": nodes}}})
    outer = await _forward_event(env, [_forward("fwd-long")])
    seen: list[dict] = []

    def respond(request):
        returned = [
            json.loads(message.content) for message in request.messages if message.role == "tool"
        ]
        seen.extend(returned[len(seen) :])
        if not returned:
            return _tool_call("read-long", "read_chat", {"around": outer.id, "context": 0})
        if len(returned) == 1:
            more = next(
                item
                for item in returned[-1]["read_more"]
                if item["path"] == ["forwards", 0, "messages"]
            )
            return _tool_call("read-more", "read_result", {**more, "limit": 3})
        return "读完了"

    provider = FakeLLMProvider(respond)
    # Compaction is its own contract; keep this turn on the result budget alone.
    assert await _ask(env, database, provider, context_compact_trigger_tokens=500000) == "chat"
    preview, page = seen
    assert preview["truncated"] is True and preview["artifact_handle"]
    assert len(json.dumps(preview, ensure_ascii=False)) < 64000
    assert "ok" not in page, page
    # The page continues the very nodes the preview did not show.
    shown = len(preview["preview"]["value"]["messages"])
    assert page["path"] == ["forwards", 0, "messages"]
    assert [node["author_name"] for node in page["value"]] == [
        f"群友{index}" for index in range(shown, shown + 3)
    ]
    assert [action for action, _ in env.bot.calls].count("get_forward_msg") == 1
    _write(
        "oversized.json",
        {
            "scenario": "60 nodes x 2000 characters through the common result budget",
            "preview_keys": sorted(preview),
            "read_more": preview["read_more"],
            "preview_characters": len(json.dumps(preview, ensure_ascii=False)),
            "read_result_keys": sorted(page),
        },
    )
