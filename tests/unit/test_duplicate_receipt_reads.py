"""Exact Social and canonical receipt reuse remains readonly and fenced."""

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from uuid import UUID

import pytest
from sqlalchemy import event, update
from tests.unit.test_canonical_ingress import _Bot, _message, _stack
from tests.unit.test_memory_writer_boundaries import other_writer_and_read_only

from qq_ai_bot.conversation.canonical_db_models import SpaceBindingIngestRouteModel
from qq_ai_bot.conversation.hydrate import ensure_canonical_conversation
from qq_ai_bot.identity import canonical_uow
from qq_ai_bot.identity.canonical_repository import ensure_person, ensure_presence
from qq_ai_bot.identity.errors import CanonicalIdentityError
from qq_ai_bot.social.db_models import SocialOperationModel
from qq_ai_bot.social.models import OperationStatus, SocialError, SocialTarget
from qq_ai_bot.social.repository import SocialOperationRepository


async def social(database):
    async with database.immediate_session() as writer:
        person = await ensure_person(writer, "1001")
        conversation = await ensure_canonical_conversation(
            writer, kind="private", primary_scope_key="private:8000:1001", person_id=person
        )
    repository = SocialOperationRepository(database)
    args = dict(
        source_turn_id="writer-turn",
        tool_call_id="writer-call",
        source_conversation_id=conversation.conversation_id,
        action="send_message_sequence",
        target=SocialTarget(kind="person", id=UUID(person)),
        payload={"parts": ["a", "b"]},
        planned_parts=2,
    )
    return repository, args


@pytest.mark.parametrize("status", list(OperationStatus))
async def test_social_exact_receipt_reuse_needs_no_writer_in_every_status(database, status):
    repository, args = await social(database)
    original = await repository.prepare(**args)
    async with database.immediate_session() as writer:
        await writer.execute(
            update(SocialOperationModel)
            .where(SocialOperationModel.id == original.operation_id)
            .values(status=status.value)
        )
    async with other_writer_and_read_only(database):
        reused = await repository.prepare(**args)
    assert reused.operation_id == original.operation_id and reused.status is status


@pytest.mark.parametrize("change", ["payload", "parts", "source", "target"])
async def test_social_read_reuse_preserves_payload_plan_source_and_target_conflicts(
    database, change
):
    repository, args = await social(database)
    await repository.prepare(**args)
    if change == "payload":
        args["payload"] = {"parts": ["different", "b"]}
    elif change == "parts":
        args["planned_parts"] = 3
    elif change == "source":
        args["source_conversation_id"] = "different-source"
    else:
        args["target"] = SocialTarget(kind="person", id=UUID(int=0))
    async with other_writer_and_read_only(database):
        with pytest.raises(
            SocialError,
            match=("target_not_found" if change == "target" else "idempotency_conflict"),
        ):
            await repository.prepare(**args)


async def test_social_competing_prepares_keep_one_original_receipt_and_claim(database):
    repository, args = await social(database)
    receipts = await asyncio.gather(repository.prepare(**args), repository.prepare(**args))
    assert receipts[0].operation_id == receipts[1].operation_id
    async with database.immediate_session() as writer:
        presence = await ensure_presence(writer, "8000")
    claims = await asyncio.gather(
        *(repository.claim(receipt.operation_id, presence_id=presence) for receipt in receipts)
    )
    assert sum(claims) == 1


async def canonical(database, group=False):
    registry, resolver, uow = await _stack(database)
    bot = _Bot("8000")
    async with database.immediate_session() as writer:
        presence = await ensure_presence(writer, "8000")
    registry.connect(bot)
    registry.bind_presence(platform="qq", external_account_id="8000", presence_id=presence)
    admitted = await resolver.pre_admit(
        bot, _message(message_id="readonly-duplicate", group_id="2001" if group else None)
    )
    assert admitted is not None and not admitted.dropped
    first = await uow.append_inbound(admitted.message, admitted)
    return uow, admitted, first


@pytest.mark.parametrize("group", [False, True])
async def test_canonical_exact_keeper_returns_under_other_writer(database, group):
    uow, admitted, first = await canonical(database, group)
    async with other_writer_and_read_only(database):
        duplicate = await uow.append_inbound(admitted.message, admitted)
    assert duplicate.event.id == first.event.id
    assert not duplicate.created and not duplicate.job_signalled
    assert duplicate.scope.runtime_scope_key == first.scope.runtime_scope_key
    assert duplicate.scope.generation == first.scope.generation
    assert duplicate.scope.last_event_id == first.scope.last_event_id


@pytest.mark.parametrize("change", ["content", "paused"])
async def test_canonical_read_duplicate_retains_compatibility_and_ingest_fence(database, change):
    uow, admitted, _ = await canonical(database, group=True)
    message = admitted.message
    if change == "content":
        message = replace(message, text="different")
    else:
        async with database.immediate_session() as writer:
            await writer.execute(
                update(SpaceBindingIngestRouteModel)
                .where(SpaceBindingIngestRouteModel.space_binding_id == admitted.space_binding_id)
                .values(paused=True)
            )
    async with other_writer_and_read_only(database):
        with pytest.raises(CanonicalIdentityError) as error:
            await uow.append_inbound(message, admitted)
    assert error.value.category == ("receipt_conflict" if change == "content" else "paused")


async def test_canonical_input_encoding_hash_and_event_dto_precede_writer(database, monkeypatch):
    registry, resolver, uow = await _stack(database)
    bot = _Bot("8000")
    async with database.immediate_session() as writer:
        presence = await ensure_presence(writer, "8000")
    registry.connect(bot)
    registry.bind_presence(platform="qq", external_account_id="8000", presence_id=presence)
    admitted = await resolver.pre_admit(bot, _message(message_id="prepared-dto"))
    held = False
    prepared = []
    fingerprint = canonical_uow._fingerprint
    initialize = canonical_uow.ChatEventModel.__init__

    def encode(*args, **kwargs):
        assert not held
        prepared.append("json")
        return json.dumps(*args, **kwargs)

    def hash_content(content):
        assert not held
        prepared.append("hash")
        return fingerprint(content)

    def initialize_event(row, *args, **kwargs):
        assert not held
        prepared.append("dto")
        initialize(row, *args, **kwargs)

    def capture(_connection, _cursor, statement, *_args):
        nonlocal held
        if statement.lstrip().upper().startswith(("INSERT", "UPDATE", "BEGIN IMMEDIATE")):
            held = True

    monkeypatch.setattr(canonical_uow, "json", SimpleNamespace(dumps=encode))
    monkeypatch.setattr(canonical_uow, "_fingerprint", hash_content)
    monkeypatch.setattr(canonical_uow.ChatEventModel, "__init__", initialize_event)
    event.listen(database.engine.sync_engine, "before_cursor_execute", capture)
    try:
        assert (await uow.append_inbound(admitted.message, admitted)).created
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", capture)
    assert prepared == ["json", "hash", "dto"]
