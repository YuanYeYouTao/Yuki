"""Independent illustrations recover original effects across real transaction windows."""

import asyncio
import sqlite3
import threading
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import replace
from datetime import UTC, datetime
from io import BytesIO
from tempfile import TemporaryFile
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from PIL import Image
from sqlalchemy import select, update
from tests.support.canonical_identity import ensure_person
from tests.support.memory_identity import transfer_account
from tests.support.person_merge import merge_persons
from tests.support.social_identity_cases import Bot, social_env
from tests.support.sticker_delivery import make_delivery, settle

from yuki.conversation.autonomy_repository import AutonomyRepository
from yuki.conversation.canonical_db_models import (
    CanonicalConversationModel,
    PersonActiveRouteModel,
)
from yuki.domain.tool_actor import ToolActor
from yuki.emoji.library import snapshot_identity
from yuki.gateway.providers.telegram import TelegramClient
from yuki.identity.canonical_repository import (
    create_person_binding,
    create_presence,
    create_space_binding,
)
from yuki.persistence.models import ChatEventModel
from yuki.runtime.origin import TurnOrigin
from yuki.runtime.work_activation import current_work_control
from yuki.runtime.work_control import WorkControl
from yuki.runtime.work_repository import WorkRepository
from yuki.services.media_resolver import _media_io
from yuki.social.db_models import SocialEmojiIntentModel
from yuki.social.emoji_intents import EmojiIntentRepository, derived_call_id
from yuki.social.models import OperationStatus, SocialError, SocialTarget

ARGS = {"text": "正文", "emoji": {"goal": "开心"}}


def sends(bot):
    return [(method, args) for method, args in bot.calls if method.startswith("send_")]


@pytest.mark.parametrize("text", ["", "正文"])
async def test_emoji_admission_and_dispatch_keep_original_codecs_outside_writer(
    database, tmp_path, monkeypatch, text
):
    import yuki.social.emoji_intents as intents_module

    env = await social_env(database, tmp_path)
    delivery, _library = make_delivery(env)
    writer_active = ContextVar("test_original_emoji_writer", default=False)
    writer = database.immediate_session

    @asynccontextmanager
    async def observed_writer():
        async with writer() as session:
            token = writer_active.set(True)
            try:
                yield session
            finally:
                writer_active.reset(token)

    monkeypatch.setattr(database, "immediate_session", observed_writer)

    def checked(operation):
        def apply(*args, **kwargs):
            assert not writer_active.get(), "original emoji bodies are prepared on a reader"
            return operation(*args, **kwargs)

        return apply

    monkeypatch.setattr(intents_module, "encode", checked(intents_module.encode))
    monkeypatch.setattr(intents_module, "payload_hash", checked(intents_module.payload_hash))
    monkeypatch.setattr(
        EmojiIntentRepository, "record", staticmethod(checked(EmojiIntentRepository.record))
    )
    result = await env.service.execute("send", {"text": text, "emoji": ARGS["emoji"]}, env.context)
    assert result["status"] == ("succeeded" if text else "accepted")
    intent = await delivery.intents.find(env.context.turn_id, env.context.call_id)
    await delivery._deliver(intent)
    final = await delivery.intents.find(env.context.turn_id, env.context.call_id)
    assert final.id == intent.id and final.closed
    assert len(sends(env.bot)) == (2 if text else 1)


@pytest.mark.parametrize("error_type", [OSError, RuntimeError])
async def test_emoji_only_admission_preserves_unknown_commit_and_original_intent(
    database, tmp_path, monkeypatch, error_type
):
    env = await social_env(database, tmp_path)
    delivery, _library = make_delivery(env)
    original_writer = database.immediate_session
    failure = error_type("original admission acknowledgement lost")

    @asynccontextmanager
    async def lose_acknowledgement():
        async with original_writer() as writer:
            commit = writer.commit

            async def commit_without_ack():
                await commit()
                raise failure

            monkeypatch.setattr(writer, "commit", commit_without_ack)
            yield writer

    monkeypatch.setattr(database, "immediate_session", lose_acknowledgement)
    arguments = {"emoji": {"goal": "开心"}}
    result = await env.service.execute("send", arguments, env.context)
    assert result["status"] == "unknown"
    assert result["emoji"] == {
        "status": "unknown",
        "error": "emoji_admission_unconfirmed",
        "mutation_committed": None,
        "database_write_phase": "commit",
    }
    assert "database_write_phase:commit" in failure.__notes__
    original = await delivery.intents.find(env.context.turn_id, env.context.call_id)
    assert original is not None and not sends(env.bot)
    replay = await env.service.execute("send", arguments, env.context)
    assert replay["status"] == "accepted"
    assert (await delivery.intents.find(env.context.turn_id, env.context.call_id)).id == original.id
    assert not sends(env.bot)


@pytest.mark.parametrize("failure", [False, True])
async def test_late_selection_close_cannot_erase_another_owners_frozen_version(
    database, tmp_path, failure
):
    env = await social_env(database, tmp_path)
    first, first_library = make_delivery(env)
    second, second_library = make_delivery(env)
    await env.service.execute("send", ARGS, env.context)
    intent = await first.intents.find(env.context.turn_id, env.context.call_id)
    selecting, release_selection = asyncio.Event(), asyncio.Event()
    publishing, first_publishing = asyncio.Event(), asyncio.Event()

    async def late_selection(*args, **kwargs):
        selecting.set()
        await release_selection.wait()
        if failure:
            raise OSError("selection failed after another owner selected")
        return None

    async def blocked_publish(*args, **kwargs):
        publishing.set()
        await asyncio.Event().wait()

    async def first_publish(*args, **kwargs):
        first_publishing.set()
        await asyncio.Event().wait()

    first_library.select = late_selection
    first_library.publish = first_publish
    second_library.publish = blocked_publish
    task_a = asyncio.create_task(first._deliver(intent))
    await asyncio.wait_for(selecting.wait(), 3)
    task_b = asyncio.create_task(second._deliver(intent))
    try:
        await asyncio.wait_for(publishing.wait(), 3)
        release_selection.set()
        await asyncio.wait_for(first_publishing.wait(), 3)
        frozen = await first.intents.find(env.context.turn_id, env.context.call_id)
        assert not frozen.closed and frozen.selected is not None
        assert frozen.error_category is None
    finally:
        task_a.cancel()
        task_b.cancel()
        await asyncio.gather(task_a, task_b, return_exceptions=True)
    restarted, library = make_delivery(env)
    await restarted.start()
    try:
        assert (await settle(restarted, env.context))["status"] == "succeeded"
        assert len(sends(env.bot)) == 2 and library.select_calls == 0
    finally:
        await restarted.close()


@pytest.mark.parametrize("phase", ["selection", "publish", "network"])
async def test_slow_illustration_does_not_block_another_intent(database, tmp_path, phase):
    env = await social_env(database, tmp_path)
    delivery, library = make_delivery(env)
    entered, release = asyncio.Event(), asyncio.Event()
    slow = await env.service.execute("send", ARGS, env.context)
    slow_id = slow["emoji"]["intent_id"]
    select_asset, publish, call_api = library.select, library.publish, env.bot.call_api
    blocked_network = False

    async def selection(goal, **kwargs):
        if phase == "selection" and goal == "开心":
            entered.set()
            await release.wait()
        return await select_asset(goal, **kwargs)

    async def publication(asset, *, request_id):
        if phase == "publish" and request_id == slow_id:
            entered.set()
            await release.wait()
        return await publish(asset, request_id=request_id)

    async def network(method, **arguments):
        nonlocal blocked_network
        if (
            phase == "network"
            and not blocked_network
            and any(part["type"] == "image" for part in arguments.get("message", ()))
        ):
            blocked_network = True
            entered.set()
            await release.wait()
        return await call_api(method, **arguments)

    library.select, library.publish, env.bot.call_api = selection, publication, network
    await delivery.start()
    try:
        await asyncio.wait_for(entered.wait(), 3)
        fast_context = replace(env.context, call_id="independent-fast")
        fast = await env.service.execute(
            "send", {"text": "第二条正文", "emoji": {"goal": "另一张"}}, fast_context
        )
        assert fast["status"] == "succeeded"
        assert (await settle(delivery, fast_context))["status"] == "succeeded"
        assert not (await delivery.intents.find(env.context.turn_id, env.context.call_id)).closed
        release.set()
        assert (await settle(delivery, env.context))["status"] == "succeeded"
        assert len(sends(env.bot)) == 4
    finally:
        await delivery.close()


async def test_pending_discovery_crosses_a_page_of_active_intents(database, tmp_path):
    env = await social_env(database, tmp_path)
    delivery, library = make_delivery(env)
    await env.service.execute("send", {"emoji": {"goal": "blocked"}}, env.context)
    original = await delivery.intents.find(env.context.turn_id, env.context.call_id)
    async with database.immediate_session() as session:
        row = await session.get(SocialEmojiIntentModel, original.id)
        first_at = row.created_at
        for index in range(99):
            session.add(
                SocialEmojiIntentModel(
                    id=str(uuid4()),
                    source_turn_id=f"page-source-{index}",
                    tool_call_id="image",
                    source_conversation_id=original.conversation_id,
                    payload_hash=original.payload_hash,
                    target_kind=original.target.kind,
                    target_id=str(original.target.id),
                    source_json=row.source_json,
                    intent_json=row.intent_json,
                    closed=False,
                    created_at=first_at,
                    updated_at=first_at,
                )
            )
    release = asyncio.Event()
    select_asset = library.select

    async def selection(goal, **kwargs):
        if goal == "blocked":
            await release.wait()
        return await select_asset(goal, **kwargs)

    library.select = selection
    await delivery.start()
    try:
        fast_context = replace(env.context, call_id="page-101")
        await env.service.execute("send", {"emoji": {"goal": "fast"}}, fast_context)
        async with asyncio.timeout(10):
            while True:
                intent = await delivery.intents.find(fast_context.turn_id, fast_context.call_id)
                if intent.closed:
                    break
                await asyncio.sleep(0.01)
        assert (await delivery.project(fast_context))["status"] == "succeeded"
        assert len(sends(env.bot)) == 1
        assert not (await delivery.intents.find(env.context.turn_id, env.context.call_id)).closed
    finally:
        await delivery.close()


@pytest.mark.parametrize("uncertain", [False, True])
async def test_original_dispatch_restores_without_source_or_manager(database, tmp_path, uncertain):
    env = await social_env(database, tmp_path)
    delivery, library = make_delivery(env)
    await env.service.execute("send", ARGS, env.context)
    original_call = env.bot.call_api

    async def transport(method, **arguments):
        result = await original_call(method, **arguments)
        if uncertain:
            raise OSError("transport acknowledgement lost")
        return result

    env.bot.call_api = transport
    intent = await delivery.intents.find(env.context.turn_id, env.context.call_id)
    await delivery._deliver(intent)
    async with database.immediate_session() as session:
        await session.execute(
            update(SocialEmojiIntentModel)
            .where(SocialEmojiIntentModel.id == intent.id)
            .values(closed=False, error_category="file_mutation_unknown_read_current_version")
        )
        await session.execute(
            update(CanonicalConversationModel)
            .where(CanonicalConversationModel.id == intent.conversation_id)
            .values(generation=CanonicalConversationModel.generation + 1)
        )
    current = await delivery.intents.find(env.context.turn_id, env.context.call_id)
    library.publish = AsyncMock(side_effect=OSError("manager gone"))
    await delivery._deliver(current)
    facts = await delivery.project(env.context)
    assert facts["status"] == ("uncertain" if uncertain else "succeeded")
    library.publish.assert_not_awaited()
    assert len(sends(env.bot)) == 2


async def test_pure_illustration_keeps_internal_reply_and_mention_without_foreground_probe(
    database, tmp_path
):
    env = await social_env(database, tmp_path)
    delivery, library = make_delivery(env)
    async with database.read_session() as session:
        event = await session.scalar(select(ChatEventModel))
    context = env.context
    env.bot.calls.clear()
    result = await env.service.execute(
        "send",
        {
            "emoji": {"goal": "开心"},
            "text": f"@{{account:{env.account}}}",
            "reply_to_event_id": event.id,
        },
        context,
    )
    assert result["status"] == "succeeded"
    assert len(sends(env.bot)) == 1
    assert [part["type"] for part in sends(env.bot)[0][1]["message"]] == ["reply", "at"]
    intent = await delivery.intents.find(context.turn_id, context.call_id)
    await delivery._deliver(intent)
    assert (await delivery.project(context))["status"] == "succeeded"
    assert len(sends(env.bot)) == 2
    method, payload = sends(env.bot)[-1]
    assert method == "send_group_msg" and [part["type"] for part in payload["message"]] == ["image"]
    assert sends(env.bot)[0][1]["message"][0]["data"]["id"] == event.platform_message_id
    assert library.select_calls == 1


async def test_same_binding_uses_original_presence_among_two_connected_accounts(database, tmp_path):
    env = await social_env(database, tmp_path)
    assert await env.router.cas_takeover_person(env.person) == "taken"
    other = Bot("80002")
    async with database.immediate_session() as session:
        presence = await create_presence(
            session, external_account_id=other.self_id, now=datetime.now(UTC)
        )
    env.registry.connect(other, provider_id="snowluma", presence_id=presence.id)
    delivery, _ = make_delivery(env)
    await env.service.execute(
        "send", {**ARGS, "target": {"kind": "person", "target_id": env.person}}, env.context
    )
    intent = await delivery.intents.find(env.context.turn_id, env.context.call_id)
    await delivery._deliver(intent)
    assert (await delivery.project(env.context))["status"] == "succeeded"
    assert len(sends(env.bot)) == 2 and sends(other) == []


async def test_self_source_presence_is_independent_of_its_actual_private_target(database, tmp_path):
    env = await social_env(database, tmp_path)
    assert await env.router.cas_takeover_person(env.person) == "taken"
    other = Bot("80002")
    async with database.immediate_session() as session:
        presence = await create_presence(
            session, external_account_id=other.self_id, now=datetime.now(UTC)
        )
        route = await session.get(PersonActiveRouteModel, env.person)
        route.presence_id = presence.id
        route.route_generation += 1
    env.registry.connect(other, provider_id="snowluma", presence_id=presence.id)
    autonomy = AutonomyRepository(database)
    binding = await autonomy.ensure_binding(env.context.conversation_id, 1)
    binding = await autonomy.transition(binding, master_enabled=True, external_enabled=True)
    accepted = await autonomy.accept_host_proposal(
        proposal_id="independent-scene",
        binding=binding,
        owner=binding.effective_owner,
        space_id=env.space,
        presence_id=env.presence,
        sources=(),
        trigger_kind="intrinsic",
    )
    actor = ToolActor(
        user_id="",
        bot_user_id=env.bot.self_id,
        group_id="20001",
        origin=TurnOrigin.SELF_INITIATIVE,
        instruction="",
        principal_kind="self",
        initiative_run_id=accepted.run.run_id,
        conversation_id=env.context.conversation_id,
        space_id=env.space,
        presence_id=env.presence,
    )
    context = replace(
        env.context, actor=actor, presence_id=env.presence, initiative_run_id=accepted.run.run_id
    )
    delivery, _ = make_delivery(env)
    text = await env.service.execute(
        "send", {**ARGS, "target": {"kind": "person", "target_id": env.person}}, context
    )
    assert text["status"] == "succeeded"
    intent = await delivery.intents.find(context.turn_id, context.call_id)
    assert intent.source["context"]["presence_id"] == env.presence
    assert intent.source["target_presence_id"] == presence.id
    await delivery._deliver(intent)
    assert (await delivery.project(context))["status"] == "succeeded"
    assert len(sends(other)) == 2 and sends(env.bot) == []


@pytest.mark.parametrize("sequence", [False, True])
async def test_prepared_person_merge_keeps_original_request_hash(database, tmp_path, sequence):
    env = await social_env(database, tmp_path)
    assert await env.router.cas_takeover_person(env.person) == "taken"
    arguments = {
        "text": "same original request",
        "target": {"kind": "person", "target_id": env.person},
    }
    target = SocialTarget(kind="person", id=UUID(env.person))
    route = await env.service.send_route(target, env.context)
    from yuki.gateway.social import TransportMessage, transport_payload

    plan = {
        "message": transport_payload(TransportMessage(arguments["text"])),
        "route": env.service._route_payload(route),
    }
    if sequence:
        prepared, _ = await env.service.receipts.prepare_sequence(
            source_turn_id=env.context.turn_id,
            tool_call_id=env.context.call_id,
            source_conversation_id=env.context.conversation_id,
            target=target,
            payload=arguments,
            parts=(arguments,),
            prepared={**plan, "parts": [plan["message"]]},
        )
    else:
        prepared = await env.service.receipts.prepare(
            source_turn_id=env.context.turn_id,
            tool_call_id=env.context.call_id,
            source_conversation_id=env.context.conversation_id,
            target=target,
            action="send_message",
            payload=arguments,
            prepared=plan,
        )
    async with database.immediate_session() as session:
        new_root = await ensure_person(session, "10002")
        await merge_persons(session, env.person, new_root)
    result = await env.service.execute("send", arguments, env.context)
    assert result["status"] == "succeeded" and result["operation_id"] == prepared.operation_id
    current = await env.service.receipts.get(prepared.operation_id)
    assert str(current.target.id) == env.person and current.payload_hash == prepared.payload_hash
    delivered = result["parts"][0] if sequence else result
    assert delivered["delivered_text_available"]
    assert delivered["delivered_text"] == arguments["text"]
    assert len(sends(env.bot)) == 1
    with pytest.raises(SocialError, match="idempotency_conflict"):
        await env.service.execute("send", dict(arguments, text="changed request"), env.context)


@pytest.mark.parametrize("phase", ["claim", "finish"])
async def test_original_social_cas_survives_committed_acknowledgement_loss(
    database, tmp_path, monkeypatch, phase
):
    from sqlalchemy.exc import OperationalError

    env = await social_env(database, tmp_path)
    delivery, library = make_delivery(env)
    await env.service.execute("send", ARGS, env.context)
    intent = await delivery.intents.find(env.context.turn_id, env.context.call_id)
    intent = await delivery.intents.select(intent.id, library.asset.model_dump(mode="json"))
    writer, boundary = database.immediate_session, getattr(env.service.receipts, phase)
    armed = fired = False

    async def arm(*args, **kwargs):
        nonlocal armed
        result = await boundary(*args, **kwargs)
        if (phase == "claim" and result) or (
            phase == "finish" and kwargs.get("status") is OperationStatus.SUCCEEDED
        ):
            armed = True
        return result

    @asynccontextmanager
    async def lost_ack():
        nonlocal fired
        async with writer() as session:
            yield session
        if armed and not fired:
            fired = True
            raise OperationalError(
                "commit completed but ACK lost", (), sqlite3.OperationalError("lost")
            )

    with monkeypatch.context() as patch:
        patch.setattr(database, "immediate_session", lost_ack)
        patch.setattr(env.service.receipts, phase, arm)
        if phase == "claim":
            with pytest.raises(OperationalError):
                await delivery._deliver(intent)
        else:
            await delivery._deliver(intent)
    assert fired and len(sends(env.bot)) == (1 if phase == "claim" else 2)
    await delivery._deliver(intent)
    assert len(sends(env.bot)) == (1 if phase == "claim" else 2)
    await env.service.receipts.recover_interrupted()
    assert (await delivery.project(env.context))["status"] == (
        "uncertain" if phase == "claim" else "succeeded"
    )


@pytest.mark.parametrize("uncertain", [False, True])
async def test_native_telegram_parts_and_work_projection_use_original_delivery_vector(
    database, tmp_path, uncertain
):
    env = await social_env(database, tmp_path)
    telegram = TelegramClient("offline-fixture-token")
    telegram.self_id = "80002"
    methods = []

    async def call(method, arguments, **kwargs):
        if method == "getChatMember":
            return {"status": "member"}
        methods.append(method)
        if method == "sendSticker" and uncertain:
            raise OSError("native sticker acknowledgement unknown")
        return {"message_id": len(methods) + 10}

    telegram.call = call
    async with database.immediate_session() as session:
        now = datetime.now(UTC)
        person_binding = await create_person_binding(
            session,
            external_account_id="10002",
            display_name="same person",
            now=now,
            platform="telegram",
        )
        await merge_persons(session, person_binding.person_id, env.person)
        presence = await create_presence(
            session,
            external_account_id=telegram.self_id,
            platform="telegram",
            now=now,
        )
        space_binding = await create_space_binding(
            session,
            external_space_id="-100002",
            name="TG group",
            enabled=True,
            autonomous_enabled=True,
            require_mention=False,
            now=now,
            platform="telegram",
        )
    env.registry.connect(telegram, provider_id="telegram", presence_id=presence.id)
    assert await env.router.cas_takeover_space(space_binding.space_id) == "taken"
    delivery, library = make_delivery(env)
    content = BytesIO()
    Image.new("RGB", (8, 8), "yellow").save(content, format="WEBP")
    library.content = content.getvalue()
    import hashlib

    library.asset = library.asset.model_copy(
        update={
            "platform": "telegram",
            "mime_type": "image/webp",
            "image_format": "webp",
            "sha256": hashlib.sha256(library.content).hexdigest(),
            "byte_size": len(library.content),
        }
    )
    repository = WorkRepository(database)
    lease = await repository.acquire(env.context.conversation_id, 1)
    source = {"origin": "user_message", "actor_user_id": "10001"}
    accepted = await repository.accept(
        lease, source_key="native-parts", source=source, goal="send sticker"
    )
    control = WorkControl(repository, lease, "native-parts", source, lambda: None)
    control.current = accepted
    token = current_work_control.set(control)
    try:
        result = await env.service.execute(
            "send",
            {
                "emoji": {"goal": "开心"},
                "target": {"kind": "space", "target_id": space_binding.space_id},
                "text": f"@{{account:{person_binding.account_id}}}",
            },
            env.context,
        )
    finally:
        current_work_control.reset(token)
        await repository.release(lease)
    assert result["status"] == "succeeded" and methods == ["sendMessage"]
    intent = await delivery.intents.find(env.context.turn_id, env.context.call_id)
    try:
        await delivery._deliver(intent)
        receipt = await env.service.receipts.find(intent.source_turn_id, derived_call_id(intent.id))
        assert receipt.status is (
            OperationStatus.UNCERTAIN if uncertain else OperationStatus.SUCCEEDED
        )
        async with database.read_session() as session:
            outbound = list(
                await session.scalars(
                    select(ChatEventModel)
                    .where(ChatEventModel.direction == "outbound")
                    .order_by(ChatEventModel.id)
                )
            )
        assert outbound[0].content.startswith("@")
        if not uncertain:
            assert outbound[1].content == "[表情: 开心]"
        async with database.immediate_session() as session:
            await session.execute(
                update(SocialEmojiIntentModel)
                .where(SocialEmojiIntentModel.id == intent.id)
                .values(closed=False, error_category="late_preparation_error")
            )
        projected = await delivery.project(env.context)
        assert projected["status"] == ("uncertain" if uncertain else "succeeded")
        assert projected["status"] == ("uncertain" if uncertain else "succeeded")
        async with database.read_session() as session:
            recovered = await EmojiIntentRepository.effect_facts(
                session,
                intent.id,
                conversation_id=env.context.conversation_id,
                work_id=accepted["id"],
                call_id=env.context.call_id,
            )
        assert (
            recovered["status"] == "accepted"
            and recovered["emoji"]["status"] == projected["status"]
        )
        library.publish = AsyncMock(side_effect=OSError("manager unavailable"))
        current = await delivery.intents.find(env.context.turn_id, env.context.call_id)
        await delivery._deliver(current)
        library.publish.assert_not_awaited()
        assert methods == ["sendMessage", "sendSticker"]
    finally:
        await telegram.close()


async def test_admission_commit_acknowledgement_loss_wakes_an_already_sleeping_owner(
    database, tmp_path, monkeypatch
):
    from sqlalchemy.exc import OperationalError

    env = await social_env(database, tmp_path)
    delivery, library = make_delivery(env)
    admit = delivery.intents.admit
    discovered = asyncio.Event()
    pending = delivery.intents.pending

    async def discover(after=None):
        result = await pending(after)
        if not result:
            discovered.set()
        return result

    async def lose_ack(**kwargs):
        await admit(**kwargs)
        raise OperationalError("admit committed but ACK lost", (), sqlite3.OperationalError("lost"))

    monkeypatch.setattr(delivery.intents, "pending", discover)
    monkeypatch.setattr(delivery.intents, "admit", lose_ack)
    arguments = {"emoji": ARGS["emoji"]}
    await delivery.start()
    try:
        await asyncio.wait_for(discovered.wait(), 3)
        result = await env.service.execute("send", arguments, env.context)
        assert result["status"] == "unknown" and result["emoji"]["status"] == "unknown"
        assert (await settle(delivery, env.context))["status"] == "succeeded"
        assert len(sends(env.bot)) == 1 and library.select_calls == 1
        assert (await env.service.execute("send", arguments, env.context))["emoji"][
            "status"
        ] == "succeeded"
        assert len(sends(env.bot)) == 1
    finally:
        await delivery.close()


async def test_original_request_replay_wakes_its_persisted_pending_intent(
    database, tmp_path, monkeypatch
):
    env = await social_env(database, tmp_path)
    delivery, library = make_delivery(env)
    discovered = asyncio.Event()
    pending = delivery.intents.pending

    async def discover(after=None):
        result = await pending(after)
        if not result:
            discovered.set()
        return result

    monkeypatch.setattr(delivery.intents, "pending", discover)
    await delivery.start()
    try:
        await asyncio.wait_for(discovered.wait(), 3)
        with monkeypatch.context() as patch:
            # The durable row survives an absent in-memory notification.
            patch.setattr(delivery, "wake", lambda: None)
            result = await env.service.execute("send", ARGS, env.context)
        assert result["emoji"]["status"] == "accepted" and library.select_calls == 0
        replay = await env.service.execute("send", ARGS, env.context)
        assert replay["status"] == "succeeded" and len(sends(env.bot)) == 1
        assert (await settle(delivery, env.context))["status"] == "succeeded"
        assert library.select_calls == 1 and len(sends(env.bot)) == 2
    finally:
        await delivery.close()


@pytest.mark.parametrize("private_target", [False, True])
async def test_account_transfer_does_not_retarget_a_pending_illustration(
    database, tmp_path, private_target
):
    env = await social_env(database, tmp_path)
    delivery, _ = make_delivery(env)
    arguments = {"emoji": {"goal": "开心"}}
    if private_target:
        assert await env.router.cas_takeover_person(env.person) == "taken"
        arguments.update(text="原正文", target={"kind": "person", "target_id": env.person})
    else:
        arguments["text"] = f"@{{account:{env.account}}}"
    original = await env.service.execute("send", arguments, env.context)
    intent = await delivery.intents.find(env.context.turn_id, env.context.call_id)
    original_source, original_target = intent.source, intent.target

    transferred = await transfer_account(database, "10001", "10002")
    await delivery._deliver(intent)

    result = await delivery.project(env.context)
    assert result["status"] in ({"failed", "not_sent"} if private_target else {"succeeded"})
    assert len(sends(env.bot)) == (1 if private_target else 2)
    current = await delivery.intents.find(env.context.turn_id, env.context.call_id)
    assert current.source == original_source and current.target == original_target
    if private_target:
        assert transferred["old_binding_id"] == original_source["binding_id"]
        assert (await env.service.receipts.get(original["operation_id"])).status is (
            OperationStatus.SUCCEEDED
        )
    else:
        assert original_source["content"]["mentions"] == []
        assert all(part["type"] == "image" for part in sends(env.bot)[-1][1]["message"])


async def test_two_illustration_owners_share_the_original_dispatch_claim(database, tmp_path):
    env = await social_env(database, tmp_path)
    first, _ = make_delivery(env)
    await env.service.execute("send", ARGS, env.context)
    intent = await first.intents.find(env.context.turn_id, env.context.call_id)
    second, _ = make_delivery(env)
    network, entered, release = env.bot.call_api, asyncio.Event(), asyncio.Event()

    async def blocked_image(action, **arguments):
        if any(part["type"] == "image" for part in arguments.get("message", ())):
            entered.set()
            await release.wait()
        return await network(action, **arguments)

    env.bot.call_api = blocked_image
    task_a = asyncio.create_task(first._deliver(intent))
    task_b = None
    try:
        await asyncio.wait_for(entered.wait(), 3)
        task_b = asyncio.create_task(second._deliver(intent))
        await asyncio.wait_for(task_b, 3)
        assert (await first.project(env.context))["status"] == "uncertain"
        release.set()
        await asyncio.wait_for(task_a, 3)
        assert (await first.project(env.context))["status"] == "succeeded"
        assert len(sends(env.bot)) == 2
    finally:
        release.set()
        await asyncio.gather(
            task_a, *((task_b,) if task_b is not None else ()), return_exceptions=True
        )


async def test_repeated_close_cancellation_joins_the_actual_copy_and_keeps_pending_intent(
    database, tmp_path
):
    env = await social_env(database, tmp_path)
    delivery, library = make_delivery(env)
    await env.service.execute("send", ARGS, env.context)
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()

    def copy_original(asset):
        entered.set()
        release.wait()
        try:
            with TemporaryFile() as stream:
                stream.write(library.content)
                stream.flush()
                return env.store.snapshot(
                    stream.fileno(),
                    f"smile.{asset.image_format}",
                    artifact_id=snapshot_identity(asset.platform, asset.sha256),
                    source_key=f"sticker:{asset.platform}:{asset.sha256}",
                )
        finally:
            finished.set()

    async def publication(asset, *, request_id):
        return await _media_io(copy_original, asset)

    library.publish = publication
    await delivery.start()
    closing = None
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        actual_owner = next(iter(delivery._deliveries.values()))
        closing = asyncio.create_task(delivery.close())
        async with asyncio.timeout(3):
            # Observe Task.cancel itself; the blocked physical copy has no async signal.
            while not actual_owner.cancelling():  # noqa: ASYNC110
                await asyncio.sleep(0)
        closing.cancel()
        await asyncio.sleep(0)
        closing.cancel()
        await asyncio.sleep(0)
        assert not closing.done() and not finished.is_set()
    finally:
        release.set()
        if closing is not None:
            await asyncio.gather(closing, return_exceptions=True)
        else:
            await delivery.close()
    assert finished.is_set() and not delivery._deliveries
    assert len(sends(env.bot)) == 1
    assert (await delivery.project(env.context))["status"] == "accepted"
    identity = snapshot_identity(library.asset.platform, library.asset.sha256)
    assert env.store.read_bytes(identity)[1] == library.content
