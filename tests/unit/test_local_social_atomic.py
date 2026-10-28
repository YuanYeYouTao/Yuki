"""Local Social publishes one ledger fact and its original receipt atomically."""

import asyncio
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from tests.unit.test_app_messages import admin as admin
from tests.unit.test_app_messages import app_env as app_env
from tests.unit.test_app_messages import drain
from tests.unit.test_client_api import bearer, pair, person_and_code

from yuki.conversation.canonical_db_models import CanonicalEventReceiptModel
from yuki.persistence.models import ChatEventModel
from yuki.social.db_models import SocialOperationModel
from yuki.social.service import SocialContext


async def source(env, database):
    _, code = await person_and_code(env.client)
    device = await pair(env.client, code)
    response = await env.client.post(
        "/client/v1/messages",
        headers=bearer(device["token"]),
        json={"request_id": str(uuid4()), "text": "original local source"},
    )
    assert response.status_code == 200
    await drain(env)
    data = response.json()["data"]
    async with database.read_session() as reader:
        event = await reader.get(ChatEventModel, data["event_id"])
        return SocialContext(
            str(uuid4()),
            "original-local-call",
            data["conversation_id"],
            person_refs={"current_speaker": event.author_person_id},
            trigger_event_id=event.id,
            reply_presence_id=event.ingress_presence_id,
        )


async def counts(database):
    async with database.read_session() as reader:
        return (
            await reader.scalar(select(func.count(ChatEventModel.id))),
            await reader.scalar(select(func.count(CanonicalEventReceiptModel.id))),
        )


async def test_failed_local_append_rolls_back_claim_and_never_succeeds_without_event(
    app_env, database, monkeypatch
):
    context = await source(app_env, database)
    before = await counts(database)
    append = app_env.social.writer.append
    with monkeypatch.context() as patch:
        patch.setattr(
            app_env.social.writer, "append", AsyncMock(side_effect=OSError("local write"))
        )
        with pytest.raises(OSError, match="local write"):
            await app_env.social.execute("send", {"text": "atomic original"}, context)
    original = await app_env.social.receipts.find(context.turn_id, context.call_id)
    assert original.status == "prepared" and original.event_id is None
    assert await counts(database) == before
    result = await app_env.social.execute("send", {"text": "atomic original"}, context)
    assert result["status"] == "succeeded" and result["operation_id"] == original.operation_id
    assert await counts(database) == (before[0] + 1, before[1])
    assert app_env.social.writer.append == append


async def test_concurrent_local_calls_share_exact_event_without_native_dispatch(
    app_env, database, monkeypatch
):
    from yuki.gateway.providers.app.social import AppSocialOperations

    context = await source(app_env, database)
    before = await counts(database)
    network = AsyncMock(side_effect=AssertionError("local native dispatch"))
    monkeypatch.setattr(AppSocialOperations, "send", network)
    results = await asyncio.gather(
        *(app_env.social.execute("send", {"text": "one original"}, context) for _ in range(2))
    )
    assert all(result["status"] == "succeeded" for result in results)
    assert results[0]["event_id"] == results[1]["event_id"]
    assert await counts(database) == (before[0] + 1, before[1])
    async with database.read_session() as reader:
        event = await reader.get(ChatEventModel, results[0]["event_id"])
        assert event.origin_key is None and event.native_message_id is None
    network.assert_not_called()


@pytest.mark.parametrize("after_commit", [False, True])
async def test_local_cancellation_preserves_the_actual_atomic_outcome(
    app_env, database, monkeypatch, after_commit
):
    context = await source(app_env, database)
    before = await counts(database)
    finish = app_env.social.receipts.finish
    commit = AsyncSession.commit

    async def marked(*args, **kwargs):
        value = await finish(*args, **kwargs)
        if not after_commit:
            raise asyncio.CancelledError
        kwargs["session"].info["cancel_local_commit"] = True
        return value

    async def cancelled(session):
        await commit(session)
        if session.info.pop("cancel_local_commit", False):
            raise asyncio.CancelledError

    with monkeypatch.context() as patch:
        patch.setattr(app_env.social.receipts, "finish", marked)
        patch.setattr(AsyncSession, "commit", cancelled)
        with pytest.raises(asyncio.CancelledError):
            await app_env.social.execute("send", {"text": "cancelled local"}, context)
    receipt = await app_env.social.receipts.find(context.turn_id, context.call_id)
    assert receipt.status == ("succeeded" if after_commit else "prepared")
    assert (receipt.event_id is not None) == after_commit
    assert await counts(database) == (before[0] + int(after_commit), before[1])


async def test_proven_old_local_execution_resumes_same_receipt_but_missing_proof_does_not(
    app_env, database
):
    context = await source(app_env, database)
    args = {"text": "original local"}
    result = await app_env.social.execute("send", args, context)
    async with database.immediate_session() as writer:
        row = await writer.get(SocialOperationModel, result["operation_id"])
        event = await writer.get(ChatEventModel, row.event_id)
        # This fixture represents the old durable claim before its atomic append.
        row.status, row.event_id, row.original_event_id = "executing", None, None
        row.original_conversation_id = None
        await writer.flush()
        await writer.delete(event)
    before = await counts(database)
    resumed = await app_env.social.execute("send", args, context)
    assert resumed["status"] == "succeeded" and resumed["operation_id"] == result["operation_id"]
    assert await counts(database) == (before[0] + 1, before[1])
    async with database.immediate_session() as writer:
        row = await writer.get(SocialOperationModel, resumed["operation_id"])
        row.status, row.event_id, row.original_event_id = "executing", None, None
        row.prepared_json = None
    current = await app_env.social.receipts.get(result["operation_id"])
    assert not app_env.social._resumable_local(current)
    assert await counts(database) == (before[0] + 1, before[1])
