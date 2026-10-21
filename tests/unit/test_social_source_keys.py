"""Long execution identities retain receipt replay and unknown-effect fences."""

from dataclasses import replace
from datetime import UTC, datetime

import pytest
from sqlalchemy import select, update
from tests.support.canonical_identity import ensure_person, ensure_presence
from tests.support.event_seed import seed_event
from tests.support.social_identity_cases import Bot, social_env

from yuki.domain.conversations import ScopeType
from yuki.identity.canonical_repository import ensure_account, replace_assignment
from yuki.identity.db_models import AccountModel, CanonicalPersonModel, IdentityBindingModel
from yuki.persistence.repositories import EventLedgerRepository
from yuki.social.db_models import SocialOperationModel
from yuki.social.models import OperationStatus, SocialError, SocialTarget
from yuki.social.repository import SocialOperationRepository
from yuki.social.source_keys import social_call_key, social_source_key


def test_source_digest_is_stable_and_includes_the_complete_identity():
    for source in ("turn-1", "x" * 128, "源" * 128):
        assert social_source_key(source) == source
    prefix = "源" * 129
    first = social_source_key(prefix + "first")
    assert first.startswith("social-source:v1:sha256:")
    assert len(first) <= 128
    assert social_source_key(prefix + "first") == first
    assert social_source_key(first) == first
    assert social_source_key(prefix + "second") != first


@pytest.mark.asyncio
@pytest.mark.parametrize("step", ["execute", "generate"])
@pytest.mark.parametrize("interrupted", [False, True])
async def test_automation_source_replay_never_repeats_a_send(database, tmp_path, step, interrupted):
    env = await social_env(database, tmp_path)
    source = f"{env.context.conversation_id}:execution:automation:1:{step}:{'a' * 64}"
    assert len(source) > 128
    context = replace(env.context, turn_id=source, call_id="delivery")
    arguments = {"text": "already delivered or awaiting reconciliation"}
    if interrupted:
        receipt = await env.service.receipts.prepare(
            source_turn_id=source,
            tool_call_id=context.call_id,
            source_conversation_id=context.conversation_id,
            action="send_message",
            target=SocialTarget(kind="space", id=env.space),
            payload=arguments,
        )
        assert await env.service.receipts.claim(receipt.operation_id, presence_id=env.presence)
        assert await env.service.receipts.recover_interrupted() == 1
    else:
        receipt = await env.service.execute("send_message", arguments, context)
        assert receipt["status"] == "succeeded"

    # Recreate the repository so replay relies on durable state, not process memory.
    env.service.receipts = SocialOperationRepository(database)
    recovered = await env.service.receipts.find(source, context.call_id)
    assert recovered is not None
    expected = OperationStatus.UNCERTAIN if interrupted else OperationStatus.SUCCEEDED
    assert recovered.status is expected
    replay = await env.service.execute("send_message", arguments, context)
    assert replay["operation_id"] == recovered.operation_id
    assert replay["status"] == expected.value
    sends = [call for call in env.bot.calls if call[0] == "send_group_msg"]
    assert len(sends) == (0 if interrupted else 1)
    async with database.sessions() as session:
        stored = await session.get(SocialOperationModel, recovered.operation_id)
        assert stored.source_turn_id == social_source_key(source)
        assert stored.source_conversation_id == context.conversation_id


@pytest.mark.asyncio
async def test_receipt_source_preserves_legacy_lookup_and_execution_separation(database, tmp_path):
    env = await social_env(database, tmp_path)
    repository = env.service.receipts
    target = SocialTarget(kind="space", id=env.space)

    async def prepare(source, text="same content", call_id="same-call"):
        return await repository.prepare(
            source_turn_id=source,
            tool_call_id=call_id,
            source_conversation_id=env.context.conversation_id,
            action="send_message",
            target=target,
            payload={"text": text},
        )

    short = "s" * 128
    old = await prepare(short)
    first_source = short + ":execution:first"
    second_source = short + ":execution:second"
    first = await prepare(first_source)
    second = await prepare(second_source)
    assert len({old.operation_id, first.operation_id, second.operation_id}) == 3
    assert await prepare(first_source) == first
    with pytest.raises(SocialError, match="idempotency_conflict"):
        await prepare(first_source, "changed content")
    # Host operation IDs may exceed the legacy receipt column width. P06 now
    # stores a full-identity digest; it remains a distinct original operation.
    long_call = "c" * 129
    long_receipt = await prepare(first_source, call_id=long_call)
    assert await prepare(first_source, call_id=long_call) == long_receipt
    assert await repository.find(first_source, long_call) == long_receipt
    assert social_call_key(long_call) != social_call_key(long_call + "other")
    with pytest.raises(SocialError, match="idempotency_conflict"):
        await prepare(first_source, "changed content", call_id=long_call)

    reloaded = SocialOperationRepository(database)
    assert await reloaded.find(short, "same-call") == old
    assert await reloaded.find(first_source, "same-call") == first
    assert await reloaded.find(second_source, "same-call") == second
    async with database.sessions() as session:
        rows = list(await session.scalars(select(SocialOperationModel)))
        assert len(rows) == 4
        assert next(row for row in rows if row.id == old.operation_id).source_turn_id == short


@pytest.mark.asyncio
async def test_contacts_page_people_instead_of_accounts_and_keep_exact_ambiguity(
    database, tmp_path
):
    env = await social_env(database, tmp_path)
    now = datetime.now(UTC)
    async with database.immediate_session() as writer:
        second = await ensure_person(writer, "11002")
        first, last = sorted((env.person, second))
        await writer.execute(
            update(CanonicalPersonModel)
            .where(CanonicalPersonModel.id.in_((first, last)))
            .values(display_name="same")
        )
        await writer.execute(
            update(AccountModel)
            .where(
                AccountModel.id.in_(
                    select(IdentityBindingModel.account_id).where(
                        IdentityBindingModel.person_id.in_((first, last))
                    )
                )
            )
            .values(display_name="platform nickname")
        )
        owner = await writer.get(CanonicalPersonModel, first)
        for index in range(32):
            account = await ensure_account(
                writer,
                str(918200000 + index),
                platform="qq",
                display_name="platform nickname",
                now=now,
            )
            await replace_assignment(writer, account, None, owner, now=now)
    await seed_event(
        EventLedgerRepository(database),
        bot_user_id=env.bot.self_id,
        scope_type=ScopeType.GROUP,
        group_id="20001",
        sender_user_id="11002",
        direction="inbound",
        platform_message_id="second-known-person",
        content="known inbound",
    )
    matches = await env.service.contacts("person", "same", exact=True)
    assert {item["target_id"] for item in matches} == {first, last}
    with pytest.raises(SocialError, match="target_ambiguous"):
        await env.service.target("person", {"display_name": "same"}, env.context)
    result = await env.service.execute("find_contacts", {"display_name": "same"}, env.context)
    items = {item["target_id"]: item for item in result["items"]}
    assert set(items) == {first, last}
    assert len(items[first]["bindings"]) == 33 and len(items[last]["bindings"]) == 1


@pytest.mark.asyncio
async def test_history_reads_canonical_ledger_without_probing_any_presence(database, tmp_path):
    env = await social_env(database, tmp_path)
    other = Bot("80002")
    async with database.immediate_session() as writer:
        second = await ensure_presence(writer, other.self_id)
    env.registry.connect(other, provider_id="snowluma", presence_id=second)
    env.bot.calls.clear()
    result = await env.service.execute(
        "query_chat_history",
        {
            "view": "recent",
            "event_kind": "all",
            "target": {"conversation_id": env.context.conversation_id},
        },
        env.context,
    )
    assert result["source"] == "canonical_ledger" and result["coverage"] == "observed"
    assert result["returned_count"] == 1
    assert result["events"][0]["person_id"] == env.person
    assert not env.bot.calls and not other.calls
