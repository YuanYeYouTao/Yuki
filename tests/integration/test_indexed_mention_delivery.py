"""Real ingress, native sends and original receipts share one account resolution."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import event, select, update
from tests.support.commands_and_chat_helpers import inbound
from tests.support.social_identity_cases import social_env

from yuki.capabilities.invocation import ToolInvocationContext, current_invocation
from yuki.domain.messages import MessageMention, SenderIdentity
from yuki.identity.canonical_repository import create_person_binding, replace_assignment
from yuki.identity.canonical_uow import CanonicalIngressUnitOfWork
from yuki.identity.db_models import (
    AccountModel,
    AccountNameObservationModel,
    AccountSpaceProfileModel,
    CanonicalPersonModel,
    IdentityBindingModel,
)
from yuki.identity.ingress import CanonicalIngressResolver
from yuki.identity.person_identity import merge_persons
from yuki.persistence.models import ChatEventModel
from yuki.services.agent_tools import ToolRuntime
from yuki.social.agent_adapter import invoke_social


async def append_message(env, native, name, *, at, message_id, mentions=(), group_id="20001"):
    text = "hello" if not mentions else "hello @Ice"
    message = replace(
        inbound(text, message_id=message_id, user_id=native, group_id=group_id),
        bot_user_id=env.bot.self_id,
        sender=SenderIdentity(native, nickname=name, nickname_known=True),
        occurred_at=at,
        received_at=datetime.now(UTC),
        mentions=mentions,
        segments=(
            {"type": "text", "data": {"text": text}}
            if not mentions
            else {"type": "at", "data": {"qq": mentions[0].target_user_id, "name": "Ice"}},
        ),
    )
    resolver = CanonicalIngressResolver(env.db, env.registry, env.router)
    admitted = await resolver.pre_admit(env.bot, message)
    assert not admitted.dropped
    result = await CanonicalIngressUnitOfWork(env.db, env.router).append_inbound(
        admitted.message, admitted
    )
    assert result.created
    return replace(
        admitted.message,
        source_event_id=result.event.id,
        conversation_id=result.event.canonical_conversation_id,
    )


@pytest.fixture
async def env(database, tmp_path):
    result = await social_env(database, tmp_path)
    result.started = datetime.now(UTC)
    result.source = await append_message(
        result, "10001", "Owner", at=result.started, message_id="real-source"
    )
    result.bot.calls.clear()
    return result


async def send(env, text, *, call_id="send", **arguments):
    runtime = ToolRuntime(inbound=env.source)
    token = current_invocation.set(ToolInvocationContext(runtime=runtime, call_id=call_id))
    try:
        return await invoke_social(
            env.service, "send_message", {"text": text, **arguments}, runtime
        )
    finally:
        current_invocation.reset(token)


async def account(env, native):
    async with env.db.sessions() as session:
        return await session.scalar(
            select(AccountModel).where(
                AccountModel.platform == "qq", AccountModel.external_account_id == native
            )
        )


def native_targets(env):
    return [
        part["data"]["qq"]
        for action, params in env.bot.calls
        if action == "send_group_msg"
        for part in params["message"]
        if part["type"] == "at"
    ]


async def test_prior_unknown_native_mention_and_current_name_are_one_account(env):
    await append_message(
        env,
        "10001",
        "Owner",
        at=env.started + timedelta(seconds=1),
        message_id="unknown-ice",
        mentions=(MessageMention("member", 0, 6, 10, "10055", "Ice"),),
    )
    async with env.db.sessions() as session, session.begin():
        binding = await create_person_binding(
            session,
            external_account_id="10055",
            display_name="Ice",
            now=env.started + timedelta(seconds=2),
        )
        selected = binding.account_id
    await append_message(
        env, "10055", "Ice", at=env.started + timedelta(seconds=3), message_id="known-ice"
    )
    async with env.db.sessions() as session:
        stages = set(
            await session.scalars(
                select(AccountNameObservationModel.assignment_id).where(
                    AccountNameObservationModel.account_id == selected,
                    AccountNameObservationModel.name == "Ice",
                )
            )
        )
    assert stages == {None, binding.id}
    result = await send(env, "前😀@ Ice，后@Ice！")
    assert result["status"] == "succeeded" and not result.get("unresolved_mentions")
    assert native_targets(env) == ["10055", "10055"]
    assert result["mentioned_accounts"] == [
        {"account_id": selected, "display_name": "Ice", "reference": f"@{{account:{selected}}}"}
    ]
    async with env.db.sessions() as session:
        row = await session.get(ChatEventModel, result["event_id"])
        from yuki.persistence.repository_helpers import _event_record

        recorded = _event_record(row)
    mentions = [item["data"] for item in recorded.segments if item["type"] == "mention"]
    assert [item["account_id"] for item in mentions] == [selected, selected]
    assert [recorded.content[item["text_start"] : item["text_end"]] for item in mentions] == [
        "@ Ice",
        "@Ice",
    ]


@pytest.mark.parametrize("same_person", [False, True])
async def test_same_name_uses_message_time_not_last_seen_or_late_arrival(env, same_person):
    if same_person:
        async with env.db.sessions() as session, session.begin():
            second = await session.scalar(
                select(IdentityBindingModel)
                .join(AccountModel)
                .where(AccountModel.platform == "qq", AccountModel.external_account_id == "10002")
            )
            await merge_persons(session, second.person_id, env.person)
    for native, second in (("10001", 1), ("10002", 4), ("10001", 2)):
        await append_message(
            env,
            native,
            "Diana",
            at=env.started + timedelta(seconds=second),
            message_id=f"{native}-{second}",
        )
    async with env.db.sessions() as session, session.begin():
        future = env.started + timedelta(days=3)
        await session.execute(
            update(AccountSpaceProfileModel)
            .where(AccountSpaceProfileModel.account_id == env.account)
            .values(last_seen_at=future)
        )
        await session.execute(
            update(AccountNameObservationModel)
            .where(AccountNameObservationModel.account_id == env.account)
            .values(last_seen_at=future)
        )
    chosen = await account(env, "10002")
    result = await send(env, "@Diana 看看，@ Diana！")
    assert result["status"] == "succeeded"
    assert native_targets(env) == ["10002", "10002"]
    assert [item["account_id"] for item in result["mentioned_accounts"]] == [chosen.id]


async def test_explicit_account_and_original_receipt_keep_the_frozen_target(env, monkeypatch):
    for native, second in (("10001", 1), ("10002", 2)):
        await append_message(
            env, native, "Diana", at=env.started + timedelta(seconds=second), message_id=native
        )
    first = await send(env, f"@{{account:{env.account}}} 第一条", call_id="explicit")
    assert native_targets(env) == ["10001"]
    assert [item["account_id"] for item in first["mentioned_accounts"]] == [env.account]
    chosen = await account(env, "10002")
    arguments = "@Diana 原文"
    original = await send(env, arguments, call_id="original")
    assert native_targets(env) == ["10001", "10002"]
    assert [item["account_id"] for item in original["mentioned_accounts"]] == [chosen.id]
    await append_message(
        env, "10001", "Diana", at=env.started + timedelta(seconds=10), message_id="newest"
    )

    async def must_not_resolve_again(*args, **kwargs):
        raise AssertionError("an original send receipt must not resolve today's names again")

    monkeypatch.setattr("yuki.social.service.prepare_for_entry", must_not_resolve_again)
    assert await send(env, arguments, call_id="original") == original
    assert native_targets(env) == ["10001", "10002"]


async def test_natural_name_uses_the_destination_conversation_not_the_invoking_one(env):
    from tests.support.canonical_identity import ensure_space

    async with env.db.sessions() as session, session.begin():
        space = await ensure_space(session, "20002")
    for native, second in (("10001", 1), ("10002", 2)):
        await append_message(
            env,
            native,
            "Diana",
            at=env.started + timedelta(seconds=second),
            message_id=f"target-{native}",
            group_id="20002",
        )
    earlier, later = (
        native
        for _, native in sorted(
            [((await account(env, native)).id, native) for native in ("10001", "10002")]
        )
    )
    target_message = await append_message(
        env,
        later,
        "Diana",
        at=env.started + timedelta(seconds=3),
        message_id="target-latest",
        group_id="20002",
    )
    await append_message(
        env, earlier, "Diana", at=env.started + timedelta(seconds=10), message_id="source-latest"
    )
    assert target_message.conversation_id != env.source.conversation_id
    assert await env.router.cas_takeover_space(space) == "taken"
    result = await send(env, "@ Diana 去目标群", target={"kind": "space", "target_id": space})
    selected = await account(env, later)
    assert result["status"] == "succeeded"
    assert [item["account_id"] for item in result["mentioned_accounts"]] == [selected.id]
    assert native_targets(env) == [later]
    async with env.db.sessions() as session:
        row = await session.get(ChatEventModel, result["event_id"])
    assert row.canonical_conversation_id == target_message.conversation_id


async def test_cross_target_private_name_uses_the_original_participants_history(env):
    private = await append_message(
        env,
        "10002",
        "Ice",
        at=datetime.now(UTC),
        message_id="private-member",
        group_id=None,
        mentions=(MessageMention("yuki", 0, 6, 10, env.bot.self_id, "Ice"),),
    )
    async with env.db.sessions() as session:
        member = await session.scalar(
            select(IdentityBindingModel)
            .join(AccountModel)
            .where(AccountModel.platform == "qq", AccountModel.external_account_id == "10002")
        )
    assert await env.router.cas_takeover_person(member.person_id) == "taken"
    target = {"kind": "person", "target_id": member.person_id}
    route = await env.router.resolve_send_for_person(member.person_id)
    assert route.conversation_id is None
    await send(env, "先前的私聊回复", target=target, call_id="private-history")
    physical_member = await account(env, "10002")
    physical_yuki = await account(env, env.bot.self_id)
    # Pick the larger UUID so missing history cannot pass through stable-ID fallback.
    if physical_member.id > physical_yuki.id:
        await append_message(
            env,
            "10002",
            "Ice",
            at=datetime.now(UTC) + timedelta(seconds=1),
            message_id="private-latest-member",
            group_id=None,
        )
        await send(env, "源群的最近回复", call_id="group-history")
        selected, native = physical_member, "10002"
    else:
        await append_message(
            env,
            "10002",
            "Ice",
            at=datetime.now(UTC) + timedelta(seconds=1),
            message_id="source-group-latest-member",
        )
        selected, native = physical_yuki, env.bot.self_id
    result = await send(env, "@ Ice 私聊里的称呼", target=target, call_id="private-mention")
    assert result["status"] == "succeeded"
    assert [item["account_id"] for item in result["mentioned_accounts"]] == [selected.id]
    sent = [params for method, params in env.bot.calls if method == "send_private_msg"]
    assert len(sent) == 2 and sent[-1]["user_id"] == 10002
    assert sent[-1]["message"][0] == {"type": "at", "data": {"qq": native}}
    async with env.db.sessions() as session:
        row = await session.get(ChatEventModel, result["event_id"])
    assert row.canonical_conversation_id == private.conversation_id
    assert row.canonical_conversation_id != env.source.conversation_id


async def test_recipient_transfer_after_preparation_does_not_block_the_real_source(
    env, monkeypatch
):
    await append_message(
        env, "10002", "Ice", at=env.started + timedelta(seconds=1), message_id="recipient"
    )
    target = await account(env, "10002")
    prepare = env.service.receipts.prepare
    moved = False

    async def transfer_after_prepare(**kwargs):
        nonlocal moved
        receipt = await prepare(**kwargs)
        if not moved:
            async with env.db.sessions() as session, session.begin():
                old = await session.scalar(
                    select(IdentityBindingModel).where(IdentityBindingModel.account_id == target.id)
                )
                other = await session.get(CanonicalPersonModel, env.person)
                current = await session.get(AccountModel, target.id)
                await replace_assignment(
                    session, current, old.id, other, now=env.started + timedelta(seconds=2)
                )
            moved = True
        return receipt

    monkeypatch.setattr(env.service.receipts, "prepare", transfer_after_prepare)
    result = await send(env, "@Ice 这条正文照常送达")
    assert result["status"] == "succeeded" and native_targets(env) == ["10002"]
    assert [item["account_id"] for item in result["mentioned_accounts"]] == [target.id]


async def test_split_mentions_and_replay_only_report_confirmed_original_parts(env, monkeypatch):
    await append_message(
        env, "10002", "Ice", at=env.started + timedelta(seconds=1), message_id="recipient"
    )
    from yuki.services.message_splitter import OutboundMessageSplitter

    snapshot = SimpleNamespace(
        reply=SimpleNamespace(
            hard_max_messages=20,
            max_qq_message_chars=16,
            delay_min_seconds=0,
            delay_max_seconds=0,
        )
    )
    original_snapshot = env.service.runtime_config

    class Config:
        async def snapshot(self):
            return snapshot

    env.service.runtime_config = Config()
    text = "前😀@ Ice 看看。\n后@Ice 再见。"
    original = await send(env, text)
    assert original["status"] == "succeeded" and original["sent_messages"] == 2
    assert native_targets(env) == ["10002", "10002"]
    chosen = await account(env, "10002")
    assert [item["account_id"] for item in original["mentioned_accounts"]] == [chosen.id]

    def must_not_split_again(*args, **kwargs):
        raise AssertionError("replay must retain the original physical parts")

    monkeypatch.setattr(OutboundMessageSplitter, "render_message", must_not_split_again)
    assert await send(env, text) == original
    assert native_targets(env) == ["10002", "10002"]
    env.service.runtime_config = original_snapshot


async def test_preparation_releases_reader_before_network_and_other_writer_can_commit(
    env, monkeypatch
):
    await append_message(
        env, "10002", "Ice", at=env.started + timedelta(seconds=1), message_id="recipient"
    )
    reached, release = asyncio.Event(), asyncio.Event()
    from yuki.social import service

    prepare = service.prepare_for_entry
    reader = None

    async def remember_reader(session, *args, **kwargs):
        nonlocal reader
        reader = session
        return await prepare(session, *args, **kwargs)

    monkeypatch.setattr(service, "prepare_for_entry", remember_reader)
    call_api = env.bot.call_api

    async def wait_at_network(action, **params):
        if action == "send_group_msg":
            assert reader is not None and not reader.in_transaction()
            reached.set()
            await release.wait()
        return await call_api(action, **params)

    monkeypatch.setattr(env.bot, "call_api", wait_at_network)
    sending = asyncio.create_task(send(env, "@ Ice 等待实际网关"))
    try:
        await asyncio.wait_for(reached.wait(), 5)
        await asyncio.wait_for(
            append_message(
                env,
                "10001",
                "Owner",
                at=env.started + timedelta(seconds=3),
                message_id="concurrent-writer",
            ),
            5,
        )
        release.set()
        assert (await sending)["status"] == "succeeded"
    finally:
        release.set()
        if not sending.done():
            await sending


async def test_partial_send_reports_only_the_successfully_mentioned_account(env, monkeypatch):
    await append_message(
        env, "10002", "Ice", at=env.started + timedelta(seconds=1), message_id="recipient"
    )
    snapshot = SimpleNamespace(
        reply=SimpleNamespace(
            hard_max_messages=20,
            max_qq_message_chars=16,
            delay_min_seconds=0,
            delay_max_seconds=0,
        )
    )

    class Config:
        async def snapshot(self):
            return snapshot

    env.service.runtime_config = Config()
    original_api = env.bot.call_api
    attempts = 0

    async def fail_second_send(action, **params):
        nonlocal attempts
        if action == "send_group_msg":
            attempts += 1
            if attempts == 2:
                raise RuntimeError("gateway response lost")
        return await original_api(action, **params)

    monkeypatch.setattr(env.bot, "call_api", fail_second_send)
    text = "@ Ice 首段已送达。\n@Owner 第二段未知。"
    result = await send(env, text)
    selected = await account(env, "10002")
    assert result["status"] == "uncertain" and result["sent_messages"] == 1
    assert [part["status"] for part in result["parts"]] == ["succeeded", "uncertain"]
    assert [item["account_id"] for item in result["mentioned_accounts"]] == [selected.id]
    assert native_targets(env) == ["10002"]
    assert await send(env, text) == result
    assert attempts == 2
    async with env.db.sessions() as session:
        recorded = list(
            await session.scalars(
                select(ChatEventModel).where(ChatEventModel.direction == "outbound")
            )
        )
    assert len(recorded) == 1 and "首段已送达" in recorded[0].content


async def test_real_indexed_preparation_reads_while_another_writer_is_active(env):
    from yuki.social.content import prepare_for_entry

    for native, second in (("10001", 1), ("10002", 2)):
        await append_message(
            env, native, "Diana", at=env.started + timedelta(seconds=second), message_id=native
        )
    target = await account(env, "10002")
    route = await env.router.resolve_send_for_conversation(env.source.conversation_id)
    statements = []

    def capture(connection, cursor, statement, parameters, context, executemany):
        statements.append((statement, parameters))

    async with env.db.immediate_session() as writer:
        await writer.execute(
            update(CanonicalPersonModel)
            .where(CanonicalPersonModel.id == env.person)
            .values(display_name="uncommitted display update")
        )
        event.listen(env.db.engine.sync_engine, "before_cursor_execute", capture)
        try:
            async with env.db.sessions() as reader:
                prepared = await asyncio.wait_for(
                    prepare_for_entry(
                        reader,
                        "@Diana 看看，@ Diana，再@Diana",
                        platform=route.platform,
                        space_binding_id=route.binding_id,
                        conversation_id=route.conversation_id,
                        operations=env.router.operations(route),
                    ),
                    5,
                )
        finally:
            event.remove(env.db.engine.sync_engine, "before_cursor_execute", capture)
    assert [item.account_id for item in prepared.message.mentions] == [target.id] * 3
    assert len(statements) == 2
    assert all(statement.lstrip().upper().startswith("SELECT") for statement, _ in statements)
    plans = []
    async with env.db.engine.connect() as connection:
        for statement, parameters in statements:
            rows = await connection.exec_driver_sql("EXPLAIN QUERY PLAN " + statement, parameters)
            plans.append("\n".join(row[3] for row in rows))
    assert (
        plans[0].count("ix_account_names_name_scope_account (name=? AND space_binding_id=?)") == 2
    ), plans[0]
    assert "ix_chat_events_conversation_message_sender_time" in plans[1]
    assert "SCAN chat_events" not in plans[1]


async def test_notification_preparation_uses_its_target_conversation_and_keeps_the_plan(
    env, tmp_path
):
    from tests.integration.test_notification_platform_entries import install

    from yuki.persistence.event_repository import EventLedgerRepository
    from yuki.plugin_host.media_artifacts import PluginMediaArtifactStore
    from yuki.plugin_host.notification_delivery import (
        PlatformNotificationTransport,
        PluginNotificationOutboxWorker,
    )
    from yuki.services.effect_gate import ConversationEffectGate
    from yuki_plugin_sdk.models import NotificationTarget, PublishNotificationRequest

    for native, second in (("10001", 1), ("10002", 2)):
        await append_message(
            env, native, "Diana", at=env.started + timedelta(seconds=second), message_id=native
        )
    repository = await install(env.db)
    target = NotificationTarget(
        target_type="group", target_id=env.space, conversation_id=env.source.conversation_id
    )
    await repository.grant_target(
        plugin_id="native-notification",
        target=target,
        bot_user_id=env.bot.self_id,
        created_by_user_id=env.person,
        presence_id=env.presence,
    )
    receipt = await repository.publish(
        plugin_id="native-notification",
        request=PublishNotificationRequest(
            event_key="indexed-mention",
            event_type="fixture",
            external_source="fixture",
            target=target,
            occurred_at=env.started,
            summary="native mention",
            text="@ Diana 原通知",
        ),
    )
    worker = PluginNotificationOutboxWorker(
        repository=repository,
        artifacts=PluginMediaArtifactStore(env.db, tmp_path / "notification-artifacts"),
        ledger=EventLedgerRepository(env.db),
        transport=PlatformNotificationTransport(database=env.db, router=env.router),
        effect_gate=ConversationEffectGate(),
    )
    await worker._prepare_pending()
    await append_message(
        env, "10001", "Diana", at=env.started + timedelta(seconds=5), message_id="newer-input"
    )
    async with env.db.sessions() as session, session.begin():
        current = await session.scalar(
            select(IdentityBindingModel)
            .join(AccountModel)
            .where(AccountModel.platform == "qq", AccountModel.external_account_id == "10002")
        )
        physical = await session.get(AccountModel, current.account_id)
        owner = await session.get(CanonicalPersonModel, env.person)
        await replace_assignment(
            session, physical, current.id, owner, now=env.started + timedelta(seconds=6)
        )
    claimed = await repository.claim_outbox()
    assert claimed is not None and claimed.source_event_id == receipt.source_event_id
    await worker._deliver(claimed)
    assert native_targets(env) == ["10002"]
    assert await repository.claim_outbox() is None
    async with env.db.sessions() as session:
        delivered = await session.scalar(
            select(ChatEventModel).where(ChatEventModel.direction == "outbound")
        )
    assert delivered.caused_by_event_id == receipt.source_event_id
    assert delivered.canonical_conversation_id == env.source.conversation_id
