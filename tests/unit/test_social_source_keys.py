"""Long execution identities retain receipt replay and unknown-effect fences."""

from dataclasses import replace

import pytest
from sqlalchemy import select
from tests.support.social_identity_cases import social_env

from qq_ai_bot.social.db_models import SocialOperationModel
from qq_ai_bot.social.models import OperationStatus, SocialError, SocialTarget
from qq_ai_bot.social.repository import SocialOperationRepository
from qq_ai_bot.social.source_keys import social_source_key


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
    with pytest.raises(SocialError, match="invalid_operation"):
        await prepare(first_source, call_id="c" * 129)

    reloaded = SocialOperationRepository(database)
    assert await reloaded.find(short, "same-call") == old
    assert await reloaded.find(first_source, "same-call") == first
    assert await reloaded.find(second_source, "same-call") == second
    async with database.sessions() as session:
        rows = list(await session.scalars(select(SocialOperationModel)))
        assert len(rows) == 3
        assert next(row for row in rows if row.id == old.operation_id).source_turn_id == short
