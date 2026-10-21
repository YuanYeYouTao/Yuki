"""Text and deferred media retain independent, restart-safe Social effects."""

import asyncio
import base64
import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.exc import OperationalError
from tests.support.agent_backend import StubAgentBackend
from tests.support.canonical_identity import ensure_person
from tests.support.social_identity_cases import add_second_account, social_env
from tests.support.sticker_delivery import DirectoryBridge, make_delivery, settle
from tests.support.work_runner_helpers import case, run, tool
from tests.support.workspace_snapshots import snapshot_bytes

from yuki.conversation.canonical_db_models import CanonicalConversationModel
from yuki.domain.messages import ChatResponse
from yuki.domain.tool_actor import ToolActor
from yuki.emoji.delivery import EmojiDeliveryService
from yuki.emoji.library import EmojiLibrary
from yuki.gateway.providers.telegram import TelegramClient
from yuki.identity.canonical_repository import create_person_binding, create_presence
from yuki.identity.db_models import CanonicalPersonModel, PresenceModel
from yuki.identity.person_identity import merge_persons
from yuki.persistence.models import ChatEventModel
from yuki.runtime.effect_outcomes import current_result_capture
from yuki.runtime.origin import TurnOrigin
from yuki.runtime.subagent_repository import SubagentRepository
from yuki.runtime.subagent_schema import children
from yuki.runtime.work_activation import current_work_control
from yuki.runtime.work_control import WorkControl
from yuki.runtime.work_journal import WorkJournal
from yuki.runtime.work_management import manage_work
from yuki.runtime.work_repository import WorkRepository
from yuki.runtime.work_schema_v1 import scope, work
from yuki.runtime.work_supervisor import recover_failure
from yuki.social.db_models import SocialEmojiIntentModel, SocialOperationModel
from yuki.social.emoji_intents import derived_call_id
from yuki.social.models import OperationStatus, SocialError, SocialTarget
from yuki.social.repository import payload_hash
from yuki.workspace.service import WorkspaceService

ARGS = {"text": "正文先到", "emoji": {"goal": "开心", "emotion": "高兴"}}


def sends(env):
    return [(action, payload) for action, payload in env.bot.calls if action.startswith("send_")]


async def test_text_returns_while_selection_never_finishes(database, tmp_path):
    env = await social_env(database, tmp_path)
    delivery, library = make_delivery(env)
    long_description = "只用于后台选图的长描述。" * 200
    library.asset = library.asset.model_copy(update={"description": long_description})
    library.release.clear()
    await delivery.start()
    try:
        result = await asyncio.wait_for(env.service.execute("send_message", ARGS, env.context), 3)
        await asyncio.wait_for(library.entered.wait(), 3)
        assert result["status"] == "succeeded" and result["delivered_text"] == ARGS["text"]
        assert result["emoji"]["status"] == "accepted"
        assert [action for action, _ in sends(env)] == ["send_group_msg"]
        assert library.publish_calls == 0
        library.release.set()
        completed = await settle(delivery, env.context)
        assert completed["status"] == "succeeded"
        assert len(sends(env)) == 2
        intent = await delivery.intents.find(env.context.turn_id, env.context.call_id)
        assert "description" not in intent.selected and "ocr_text" not in intent.selected
        async with database.sessions() as session:
            receipt = await session.get(SocialOperationModel, result["operation_id"])
            assert receipt.payload_hash == payload_hash(
                "send_message",
                SocialTarget(kind="space", id=UUID(env.space)),
                ARGS,
                env.context.conversation_id,
            )
            events = list(
                await session.scalars(
                    select(ChatEventModel)
                    .where(ChatEventModel.direction == "outbound")
                    .order_by(ChatEventModel.id)
                )
            )
            assert events[0].content == ARGS["text"]
            assert len(events) == 2
            assert long_description not in events[1].content
            assert long_description not in events[1].segments_json
            assert "开心" in events[1].content and "开心" in events[1].segments_json
        history = await env.service.execute(
            "query_chat_history", {"view": "recent", "event_kind": "all"}, env.context
        )
        rendered = json.dumps(history, ensure_ascii=False)
        assert long_description not in rendered and "开心" in rendered
    finally:
        await delivery.close()


@pytest.mark.parametrize("private", [False, True])
async def test_qq_library_sticker_reaches_native_small_sticker_payload_only(
    database, tmp_path, private
):
    env = await social_env(database, tmp_path)
    delivery, library = make_delivery(env)
    target = {}
    if private:
        assert await env.router.cas_takeover_person(env.person) == "taken"
        target = {"target": {"kind": "person", "target_id": env.person}}
    await delivery.start()
    try:
        text = await env.service.execute("send_message", {**ARGS, **target}, env.context)
        illustrated = await settle(delivery, env.context)
        assert text["status"] == illustrated["status"] == "succeeded"
        wire = sends(env)
        action = "send_private_msg" if private else "send_group_msg"
        destination = {"user_id": 10001} if private else {"group_id": 20001}
        assert wire == [
            (
                action,
                {**destination, "message": [{"type": "text", "data": {"text": ARGS["text"]}}]},
            ),
            (
                action,
                {
                    **destination,
                    "message": [
                        {
                            "type": "image",
                            "data": {
                                "file": "base64://" + base64.b64encode(library.content).decode(),
                                "sub_type": 1,
                            },
                        }
                    ],
                },
            ),
        ]
        assert library.selected_platforms == ["qq"]
        async with database.sessions() as session:
            receipt = await session.get(SocialOperationModel, illustrated["operation_id"])
            image = await session.get(ChatEventModel, receipt.event_id)
            segment = json.loads(image.segments_json)[0]["data"]
            assert segment["is_sticker"] and segment["emoji_id"] == library.asset.id
            assert segment["tags"] == ["开心"]
        artifact = snapshot_bytes(env.store, "ordinary.png", library.content)
        for kind in ("image", "file"):
            result = await env.service.execute(
                "send_message",
                {**target, "artifact_id": artifact["artifact_id"], "attachment_kind": kind},
                replace(env.context, call_id=f"ordinary-{kind}"),
            )
            assert result["status"] == "succeeded"
            if kind == "image":
                assert sends(env)[-1][1]["message"] == [
                    {
                        "type": "image",
                        "data": {"file": "base64://" + base64.b64encode(library.content).decode()},
                    }
                ]
            else:
                upload, payload = env.bot.calls[-1]
                assert upload == ("upload_private_file" if private else "upload_group_file")
                assert payload["name"] == "ordinary.png" and payload["file"].startswith(
                    "/transfer/"
                )
                assert "message" not in payload
        assert list((tmp_path / "transfer").iterdir()) == []
    finally:
        await delivery.close()


@pytest.mark.parametrize("target_platform", ["qq", "telegram"])
@pytest.mark.parametrize("matching_library", [False, True])
async def test_illustration_uses_actual_target_platform_instead_of_actor_entry(
    database, tmp_path, target_platform, matching_library
):
    env = await social_env(database, tmp_path)
    assert await env.router.cas_takeover_person(env.person) == "taken"
    now = datetime.now(UTC)
    telegram = TelegramClient("offline-fixture-token")
    telegram.self_id = "80002"
    telegram.call = AsyncMock(return_value={"message_id": 17})
    async with database.immediate_session() as session:
        binding = await create_person_binding(
            session,
            external_account_id="10002",
            display_name="same person on Telegram",
            now=now,
            platform="telegram",
        )
        from yuki.identity.person_identity import merge_persons

        await merge_persons(session, binding.person_id, env.person)
        presence = await create_presence(
            session, external_account_id=telegram.self_id, now=now, platform="telegram"
        )
    env.registry.connect(telegram, provider_id="telegram", presence_id=presence.id)
    _, fixture = make_delivery(env)
    workspace = WorkspaceService(env.store)
    workspace.sandbox = DirectoryBridge(tmp_path / "terminal", env.store)
    library = EmojiLibrary(workspace)
    assets = {}
    for platform in ("qq", "telegram"):
        if not matching_library and platform == target_platform:
            continue
        assets[platform] = await library.store_content(
            fixture.content, platform=platform, tags=("开心",), is_sticker=True
        )
    delivery = EmojiDeliveryService(env.service, library)
    env.service.emoji_delivery = delivery
    target = {"kind": "person", "target_id": env.person}
    if target_platform == "telegram":
        target["binding_id"] = binding.id
    arguments = {**ARGS, "target": target}
    await delivery.start()
    try:
        result = await env.service.execute("send_message", arguments, env.context)
        assert result["status"] == "succeeded"
        illustrated = await settle(delivery, env.context)
        intent = await delivery.intents.find(env.context.turn_id, env.context.call_id)
        assert intent.source["platform"] == target_platform
        if matching_library:
            assert illustrated["status"] == "succeeded"
            assert intent.selected["platform"] == target_platform
            assert intent.selected["id"] == assets[target_platform].id
        else:
            assert illustrated["status"] == "failed"
            assert illustrated["error"] == "emoji_no_candidate" and intent.selected is None
        if target_platform == "telegram":
            assert sends(env) == []
            assert [call.args[0] for call in telegram.call.await_args_list] == [
                "sendMessage",
                *(["sendPhoto"] if matching_library else []),
            ]
            assert all(call.args[1]["chat_id"] == "10002" for call in telegram.call.await_args_list)
        else:
            assert len(sends(env)) == (2 if matching_library else 1)
            assert telegram.call.await_count == 0
    finally:
        await delivery.close()
        await telegram.close()


async def test_unavailable_and_empty_library_do_not_block_text(database, tmp_path):
    env = await social_env(database, tmp_path)
    unavailable = await env.service.execute("send_message", ARGS, env.context)
    assert unavailable["status"] == "succeeded"
    assert unavailable["emoji"]["status"] == "not_accepted"
    delivery, library = make_delivery(env)
    library.asset = None
    context = replace(env.context, call_id="empty-library")
    await delivery.start()
    try:
        result = await env.service.execute("send_message", ARGS, context)
        assert result["status"] == "succeeded"
        completed = await settle(delivery, context)
        assert completed["error"] == "emoji_no_candidate"
        assert len(sends(env)) == 2
    finally:
        await delivery.close()


async def test_pure_illustration_is_accepted_without_fake_text_receipt(database, tmp_path):
    env = await social_env(database, tmp_path)
    delivery, library = make_delivery(env)
    arguments = {"emoji": {"goal": "开心" * 800, "emotion": "高兴" * 200}}
    result = await env.service.execute("send_message", arguments, env.context)
    assert result["status"] == "accepted"
    assert sends(env) == []
    assert await env.service.receipts.find(env.context.turn_id, env.context.call_id) is None
    original = await delivery.intents.find(env.context.turn_id, env.context.call_id)
    assert original.intent == arguments["emoji"]
    assert await env.service.execute("send_message", arguments, env.context) == result
    with pytest.raises(SocialError, match="idempotency_conflict"):
        await env.service.execute("send_message", {"emoji": {"goal": "另一张"}}, env.context)
    await delivery.start()
    try:
        assert (await settle(delivery, env.context))["status"] == "succeeded"
        assert len(sends(env)) == 1 and library.select_calls == 1
    finally:
        await delivery.close()


async def test_original_replay_and_concurrent_acceptance_send_once(database, tmp_path):
    env = await social_env(database, tmp_path)
    delivery, library = make_delivery(env)
    results = await asyncio.gather(
        *(env.service.execute("send_message", ARGS, env.context) for _ in range(4))
    )
    assert any(result["emoji"]["status"] == "accepted" for result in results)
    async with database.sessions() as session:
        assert len(list(await session.scalars(select(SocialEmojiIntentModel)))) == 1
    await delivery.start()
    try:
        assert (await settle(delivery, env.context))["status"] == "succeeded"
        replay = await env.service.execute("send_message", ARGS, env.context)
        assert replay["status"] == "succeeded" and replay["emoji"]["status"] == "succeeded"
        assert len(sends(env)) == 2 and library.select_calls == 1
    finally:
        await delivery.close()


async def test_legacy_finished_request_does_not_gain_new_illustration(database, tmp_path):
    env = await social_env(database, tmp_path)
    delivery, _ = make_delivery(env)
    # A historical completed call has its original complete payload, but no promise
    # of a new independent illustration effect.
    target = SocialTarget(kind="space", id=UUID(env.space))
    old = await env.service.receipts.prepare(
        source_turn_id=env.context.turn_id,
        tool_call_id=env.context.call_id,
        source_conversation_id=env.context.conversation_id,
        action="send_message",
        target=target,
        payload=ARGS,
    )
    async with database.immediate_session() as session:
        await env.service.receipts.claim(
            old.operation_id, presence_id=env.presence, session=session
        )
        await env.service.receipts.finish(
            old.operation_id, status=OperationStatus.SUCCEEDED, session=session
        )
    result = await env.service.execute("send_message", ARGS, env.context)
    assert result["status"] == "succeeded" and "emoji" not in result
    assert await delivery.project(env.context) is None and sends(env) == []


@pytest.mark.parametrize("selected", [False, True])
async def test_restart_recovers_original_selection_input(database, tmp_path, selected):
    env = await social_env(database, tmp_path)
    delivery, library = make_delivery(env)
    await env.service.execute("send_message", ARGS, env.context)
    intent = await delivery.intents.find(env.context.turn_id, env.context.call_id)
    if selected:
        await delivery.intents.select(intent.id, library.asset.model_dump(mode="json"))
    restarted = EmojiDeliveryService(env.service, library)
    env.service.emoji_delivery = restarted
    await restarted.start()
    try:
        assert (await settle(restarted, env.context))["status"] == "succeeded"
        assert library.select_calls == (0 if selected else 1)
        assert len(sends(env)) == 2
    finally:
        await restarted.close()


async def test_claimed_unknown_media_is_never_resent_after_restart(database, tmp_path):
    env = await social_env(database, tmp_path)
    delivery, library = make_delivery(env)
    await env.service.execute("send_message", ARGS, env.context)
    intent = await delivery.intents.find(env.context.turn_id, env.context.call_id)
    await delivery.intents.select(intent.id, library.asset.model_dump(mode="json"))
    artifact = await library.publish(library.asset, request_id=intent.id)
    arguments = {
        "text": "",
        "target": {"kind": "space", "target_id": env.space},
        "artifact_id": artifact["artifact_id"],
        "attachment_kind": "image",
    }
    receipt = await env.service.receipts.prepare(
        source_turn_id=intent.source_turn_id,
        tool_call_id=derived_call_id(intent.id),
        source_conversation_id=env.context.conversation_id,
        action="send_message",
        target=intent.target,
        payload=arguments,
    )
    await env.service.receipts.claim(receipt.operation_id, presence_id=env.presence)
    assert await env.service.receipts.recover_interrupted() == 1
    await delivery.start()
    try:
        result = await settle(delivery, env.context)
        assert result["status"] == "uncertain"
        assert len(sends(env)) == 1
    finally:
        await delivery.close()


async def accept_work(env):
    repository = WorkRepository(env.db)
    lease = await repository.acquire(env.context.conversation_id, 1)
    source = {"origin": "user_message", "actor_user_id": "10001"}
    accepted = await repository.accept(
        lease, source_key="illustration-test", source=source, goal="send text and picture"
    )
    control = WorkControl(repository, lease, "illustration-test", source, lambda: None)
    control.current = accepted
    token = current_work_control.set(control)
    try:
        result = await env.service.execute("send_message", ARGS, env.context)
    finally:
        current_work_control.reset(token)
    return repository, lease, accepted, result


@pytest.mark.parametrize("ending", ["completed", "scope_cancel", "work_cancel", "generation"])
async def test_completed_work_releases_lease_but_real_revocation_stops_image(
    database, tmp_path, ending
):
    env = await social_env(database, tmp_path)
    delivery, _ = make_delivery(env)
    repository, lease, accepted, result = await accept_work(env)
    assert result["emoji"]["status"] == "accepted"
    async with database.immediate_session() as session:
        if ending == "scope_cancel":
            await repository.cancel(env.context.conversation_id, session=session)
        elif ending == "work_cancel":
            await manage_work(session, accepted["id"], accepted["revision"], "cancel")
        elif ending == "generation":
            await session.execute(
                update(CanonicalConversationModel)
                .where(CanonicalConversationModel.id == env.context.conversation_id)
                .values(generation=2)
            )
        else:
            await session.execute(
                update(work).where(work.c.id == accepted["id"]).values(state="completed")
            )
    await repository.release(lease)
    await delivery.start()
    try:
        completed = await settle(delivery, env.context)
        assert completed["status"] == ("succeeded" if ending == "completed" else "failed")
        assert len(sends(env)) == (2 if ending == "completed" else 1)
    finally:
        await delivery.close()


async def test_later_main_failure_does_not_revoke_an_accepted_illustration(database, tmp_path):
    env = await social_env(database, tmp_path)
    delivery, _ = make_delivery(env)
    repository, lease, accepted, text = await accept_work(env)
    async with database.sessions() as session:
        before = await session.scalar(
            select(scope.c.cancel_epoch).where(
                scope.c.conversation_id == env.context.conversation_id
            )
        )
    control = WorkControl(
        repository,
        lease,
        "illustration-test",
        {"origin": "user_message", "actor_user_id": "10001"},
        lambda: None,
    )
    control.current = accepted
    await recover_failure(control, RuntimeError("later main model call failed"))
    assert (await repository.get(accepted["id"]))["state"] == "failed"
    async with database.sessions() as session:
        after = await session.scalar(
            select(scope.c.cancel_epoch).where(
                scope.c.conversation_id == env.context.conversation_id
            )
        )
        assert after == before
    await repository.release(lease)
    await delivery.start()
    try:
        assert (await settle(delivery, env.context))["status"] == "succeeded"
        assert len(sends(env)) == 2
        assert (
            await env.service.receipts.get(text["operation_id"])
        ).status is OperationStatus.SUCCEEDED
        replay = await env.service.execute("send_message", ARGS, env.context)
        assert replay["status"] == "succeeded" and replay["emoji"]["status"] == "succeeded"
        assert len(sends(env)) == 2
    finally:
        await delivery.close()


async def test_worker_failure_that_revokes_child_epoch_stops_its_pending_image(database, tmp_path):
    env = await social_env(database, tmp_path)
    delivery, _ = make_delivery(env)
    repository = WorkRepository(database)
    lease = await repository.acquire(env.context.conversation_id, 1)
    source = {"origin": "user_message", "actor_user_id": "10001"}
    parent = await repository.accept(lease, source_key="parent", source=source, goal="delegate")
    database.subagents_enabled = True
    workers = SubagentRepository(repository)
    identity = await workers.start(lease, parent["id"], "image-worker", {"goal": "send picture"})
    child_lease = await workers.acquire(identity)
    child = await repository.get(identity)
    control = WorkControl(repository, child_lease, child["source_key"], source, lambda: None)
    control.current = child
    token = current_work_control.set(control)
    try:
        text = await env.service.execute("send_message", ARGS, env.context)
    finally:
        current_work_control.reset(token)
    intent = await delivery.intents.find(env.context.turn_id, env.context.call_id)
    await recover_failure(control, RuntimeError("worker failed and its execution was revoked"))
    assert (await repository.get(identity))["state"] == "failed"
    async with database.sessions() as session:
        epoch = await session.scalar(
            select(children.c.cancel_epoch).where(children.c.work_id == identity)
        )
        assert epoch != intent.source["work_cancel_epoch"]
    await repository.release(child_lease)
    await repository.release(lease)
    await delivery.start()
    try:
        result = await settle(delivery, env.context)
        assert result["status"] == "failed" and result["error"] == "emoji_source_cancelled"
        assert len(sends(env)) == 1
        assert (
            await env.service.receipts.get(text["operation_id"])
        ).status is OperationStatus.SUCCEEDED
    finally:
        await delivery.close()


@pytest.mark.parametrize("swapped_prepared_route", [False, True])
async def test_illustration_preserves_explicit_account_among_same_person_same_presence(
    database, tmp_path, monkeypatch, swapped_prepared_route
):
    env = await social_env(database, tmp_path)
    assert await env.router.cas_takeover_person(env.person) == "taken"
    binding_b = await add_second_account(env)
    delivery, _ = make_delivery(env)
    arguments = {
        **ARGS,
        "target": {"kind": "person", "target_id": env.person, "binding_id": binding_b},
    }
    text = await env.service.execute("send_message", arguments, env.context)
    assert text["status"] == "succeeded"
    original = env.service.send_route

    async def route(target, context, binding_id=None):
        if swapped_prepared_route and context.deferred_emoji_id is not None:
            return await env.router.resolve_send_for_person(env.person)
        return await original(target, context, binding_id)

    monkeypatch.setattr(env.service, "send_route", route)
    await delivery.start()
    try:
        result = await settle(delivery, env.context)
        if swapped_prepared_route:
            assert result["status"] == "not_sent" and result["error"] == "emoji_route_changed"
            expected = [10002]
        else:
            assert result["status"] == "succeeded"
            expected = [10002, 10002]
        assert [
            payload["user_id"] for action, payload in sends(env) if action == "send_private_msg"
        ] == expected
        assert (
            await env.service.receipts.get(text["operation_id"])
        ).status is OperationStatus.SUCCEEDED
    finally:
        await delivery.close()


async def test_cancellation_between_selection_and_claim_is_rechecked(
    database, tmp_path, monkeypatch
):
    env = await social_env(database, tmp_path)
    delivery, library = make_delivery(env)
    repository, lease, _, _ = await accept_work(env)
    publish = library.publish

    async def cancel_after_publish(*args, **kwargs):
        result = await publish(*args, **kwargs)
        await repository.cancel(env.context.conversation_id)
        return result

    monkeypatch.setattr(library, "publish", cancel_after_publish)
    await repository.release(lease)
    await delivery.start()
    try:
        result = await settle(delivery, env.context)
        assert result["status"] == "not_sent"
        assert len(sends(env)) == 1
        async with database.sessions() as session:
            intent = await session.scalar(select(SocialEmojiIntentModel))
            assert intent.error_category == "emoji_source_cancelled"
    finally:
        await delivery.close()


@pytest.mark.parametrize("text,mention", [("正文先到", False), ("", False), ("", True)])
async def test_real_agent_finishes_work_without_waiting_for_illustration(
    database, tmp_path, text, mention
):
    arguments = {**ARGS, "text": text}
    if mention:
        arguments["text"] = "@test-10001"
    fixture = await case(
        database,
        tmp_path,
        [
            ChatResponse("", 0, tool_calls=(tool("send_message", arguments, "illustration-send"),)),
            ChatResponse("", 0, tool_calls=(tool("task_control", {"action": "complete"}),)),
        ],
    )
    env = fixture.env
    delivery, library = make_delivery(env)
    library.release.clear()
    context = replace(
        env.context,
        turn_id=fixture.control.current["id"],
        call_id="illustration-send",
        person_refs={"current_speaker": env.person},
    )

    async def deliver(invocation):
        nonlocal context
        context = replace(context, call_id=invocation.identity.operation_id)
        result = await env.service.execute(
            invocation.call.function.name, json.loads(invocation.call.function.arguments), context
        )
        if not text and not mention:
            # Read the real crash window: admission has committed, while the
            # original Work effect still has no recorded tool result.
            token = current_result_capture.set(None)
            try:
                recovered = json.loads(
                    await WorkJournal(fixture.repository).effect_result(
                        invocation.identity.operation_id, original_tool="send_message"
                    )
                )
            finally:
                current_result_capture.reset(token)
            assert recovered["ok"] and recovered["data"]["status"] == "accepted"
            assert recovered["data"]["emoji"]["status"] == "accepted"
            assert recovered["replay_forbidden"] is True
        return json.dumps({"ok": True, "data": result})

    fixture.backend = StubAgentBackend(
        execute_call=deliver,
        parallel_safe=lambda *_: False,
        is_side_effecting=lambda *_: True,
        finalize=lambda text, _runtime: text,
    )
    await delivery.start()
    try:
        result = await asyncio.wait_for(run(fixture), 5)
        assert result.work_state == "completed" and len(fixture.provider.requests) == 2
        assert fixture.control.accepted_ending() == "completed"
        assert len(sends(env)) == bool(text or mention) and library.publish_calls == 0
        await fixture.repository.release(fixture.control.lease)
        library.release.set()
        assert (await settle(delivery, context))["status"] == "succeeded"
        assert (
            len(sends(env)) == (2 if text or mention else 1) and len(fixture.provider.requests) == 2
        )
    finally:
        await delivery.close()


async def test_database_discovery_failure_does_not_kill_pending_delivery(
    database, tmp_path, monkeypatch
):
    env = await social_env(database, tmp_path)
    delivery, library = make_delivery(env)
    await env.service.execute("send_message", ARGS, env.context)
    original = delivery.intents.pending
    attempts = 0

    async def pending(after=None):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OperationalError(
                "select pending", (), sqlite3.OperationalError("database is locked")
            )
        return await original(after)

    monkeypatch.setattr(delivery.intents, "pending", pending)
    await delivery.start()
    try:
        assert (await settle(delivery, env.context))["status"] == "succeeded"
        assert attempts >= 2 and library.select_calls == 1 and len(sends(env)) == 2
        assert delivery.running
    finally:
        await delivery.close()


async def test_person_merge_preserves_accepted_original_target(database, tmp_path):
    env = await social_env(database, tmp_path)
    await env.router.cas_takeover_person(env.person)
    delivery, _ = make_delivery(env)
    arguments = {**ARGS, "target": {"kind": "person", "target_id": env.person}}
    await env.service.execute("send_message", arguments, env.context)
    async with database.immediate_session() as session:
        new_root = await ensure_person(session, "10002")
        await merge_persons(session, env.person, new_root)
    await delivery.start()
    try:
        projected = await settle(delivery, env.context)
        assert projected["status"] == "succeeded"
        receipt = await env.service.receipts.get(projected["operation_id"])
        assert str(receipt.target.id) == new_root
        intent = await delivery.intents.find(env.context.turn_id, env.context.call_id)
        assert str(intent.target.id) == env.person
        assert [
            payload["user_id"] for action, payload in sends(env) if action == "send_private_msg"
        ] == [10001, 10001]
    finally:
        await delivery.close()


async def test_cancellation_after_claim_preserves_confirmed_image(database, tmp_path, monkeypatch):
    env = await social_env(database, tmp_path)
    delivery, _ = make_delivery(env)
    repository, lease, _, _ = await accept_work(env)
    original = env.bot.call_api

    async def cancel_after_claim(action, **params):
        if action == "send_group_msg" and any(
            part["type"] == "image" for part in params["message"]
        ):
            await repository.cancel(env.context.conversation_id)
        return await original(action, **params)

    monkeypatch.setattr(env.bot, "call_api", cancel_after_claim)
    await repository.release(lease)
    await delivery.start()
    try:
        assert (await settle(delivery, env.context))["status"] == "succeeded"
        assert len(sends(env)) == 2
    finally:
        await delivery.close()


async def test_pure_illustration_admission_acknowledgement_loss_is_not_rejection(
    database, tmp_path, monkeypatch
):
    env = await social_env(database, tmp_path)
    delivery, _ = make_delivery(env)
    admit = delivery.admit

    async def lose_acknowledgement(*args, **kwargs):
        await admit(*args, **kwargs)
        raise OperationalError("commit acknowledgement", (), sqlite3.OperationalError("lost"))

    monkeypatch.setattr(delivery, "admit", lose_acknowledgement)
    arguments = {"emoji": ARGS["emoji"]}
    first = await env.service.execute("send_message", arguments, env.context)
    assert first["status"] == "unknown" and first["emoji"]["status"] == "unknown"
    replay = await env.service.execute("send_message", arguments, env.context)
    assert replay["emoji"]["status"] == "accepted" and len(sends(env)) == 0
    await delivery.start()
    try:
        assert (await settle(delivery, env.context))["status"] == "succeeded"
        assert len(sends(env)) == 1
    finally:
        await delivery.close()


@pytest.mark.parametrize("revocation", ["event_removed", "person_disabled", "presence_retired"])
async def test_deferred_effect_keeps_real_actor_source_and_presence(database, tmp_path, revocation):
    env = await social_env(database, tmp_path)
    delivery, _ = make_delivery(env)
    async with database.sessions() as session:
        event_id = await session.scalar(
            select(ChatEventModel.id).where(
                ChatEventModel.canonical_conversation_id == env.context.conversation_id,
                ChatEventModel.direction == "inbound",
            )
        )
    actor = ToolActor(
        user_id="10001",
        bot_user_id="80001",
        group_id="20001",
        origin=TurnOrigin.USER_MESSAGE,
        instruction="private context never retained",
        event_id=event_id,
        person_id=env.person,
        conversation_id=env.context.conversation_id,
        presence_id=env.presence,
        space_id=env.space,
    )
    context = replace(env.context, actor=actor, trigger_event_id=event_id, presence_id=env.presence)
    result = await env.service.execute("send_message", ARGS, context)
    assert result["status"] == "succeeded" and result["emoji"]["status"] == "accepted"
    async with database.immediate_session() as session:
        intent = await session.scalar(select(SocialEmojiIntentModel))
        assert actor.instruction not in intent.source_json
        if revocation == "event_removed":
            await session.execute(delete(ChatEventModel).where(ChatEventModel.id == event_id))
        elif revocation == "person_disabled":
            await session.execute(
                update(CanonicalPersonModel)
                .where(CanonicalPersonModel.id == env.person)
                .values(enabled=False)
            )
        else:
            await session.execute(
                update(PresenceModel)
                .where(PresenceModel.id == env.presence)
                .values(status="disabled")
            )
    await delivery.start()
    try:
        assert (await settle(delivery, context))["status"] == "failed"
        assert len(sends(env)) == 1
        receipt = await env.service.receipts.get(result["operation_id"])
        assert receipt.status is OperationStatus.SUCCEEDED
    finally:
        await delivery.close()
