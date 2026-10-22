"""Only pure DB preparation retries a real WAL stale read snapshot."""

from __future__ import annotations

import asyncio
import hashlib
import sqlite3
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest
from sqlalchemy import event as sqlalchemy_event
from sqlalchemy import func, select, text
from sqlalchemy.exc import OperationalError
from tests.unit.test_memory_mutation import _context, _event, _service
from tests.unit.test_memory_v2 import _claim

from yuki.identity.canonical_repository import find_identity_binding, replace_assignment
from yuki.identity.db_models import CanonicalPersonModel
from yuki.memory.claim_processor import MemoryProcessingContext
from yuki.memory.enums import MemoryScopeType
from yuki.memory.mutation.models import (
    MemoryMutationOperation,
    MemoryMutationRequest,
    MemoryMutationTarget,
)
from yuki.memory.mutation.service import MemoryMutationRejected
from yuki.memory.repository import MemoryJobRepository
from yuki.persistence.models import MemoryFactModel, MemoryMutationReceiptModel


@pytest.mark.parametrize("entry", ["tool", "worker"])
@pytest.mark.parametrize("phase", ["commit", "rollback"])
@pytest.mark.parametrize("code", [5, 517])
async def test_mutation_unknown_transaction_ack_uses_original_receipt_without_replay(
    database, monkeypatch, entry, phase, code
):
    mutations, facts, ledger, processor = _service(database)
    source = await _event(
        ledger, message_id=f"unknown-{entry}", sender_user_id="1001", content="我准备考研"
    )
    transaction = facts.repository.write_session
    resolve = processor.resolve
    reserve = mutations._receipts.reserve
    finalize = mutations._receipts.finalize
    resolutions, mutation_ids = [], []
    native = sqlite3.OperationalError("unknown transaction acknowledgement")
    native.sqlite_errorcode = code
    acknowledgement = OperationalError(phase, {}, native)

    @asynccontextmanager
    async def unknown_ack(prepared):
        try:
            async with transaction(prepared) as session:
                yield session
        except OperationalError:
            raise acknowledgement from None
        raise acknowledgement

    def resolve_once(*args, **kwargs):
        resolutions.append(True)
        return resolve(*args, **kwargs)

    async def reserve_once(**kwargs):
        mutation_ids.append(kwargs["mutation_id"])
        return await reserve(**kwargs)

    async def finalize_once(*args, **kwargs):
        result = await finalize(*args, **kwargs)
        if phase == "rollback":
            failure = sqlite3.OperationalError("operation failed after actual DML")
            failure.sqlite_errorcode = 517
            raise OperationalError("operation", {}, failure)
        return result

    monkeypatch.setattr(facts.repository, "write_session", unknown_ack)
    monkeypatch.setattr(processor, "resolve", resolve_once)
    monkeypatch.setattr(mutations._receipts, "reserve", reserve_once)
    monkeypatch.setattr(mutations._receipts, "finalize", finalize_once)
    if entry == "tool":
        invocation = mutations.mutate(
            MemoryMutationRequest(
                operation=MemoryMutationOperation.CREATE,
                target=MemoryMutationTarget(
                    subject_ref="current_speaker", scope_type=MemoryScopeType.PERSON
                ),
                new_content="准备考研",
                memory_key="education:plan",
                category="education",
            ),
            _context(source),
        )
    else:
        claim = processor.validate(_claim(), source)
        assert claim is not None
        jobs = MemoryJobRepository(database)
        assert await jobs.enqueue(source.id, "private:1001")
        (job,) = await jobs.claim()
        invocation = mutations.mutate_validated_claim(
            claim,
            MemoryProcessingContext(event=source),
            conversation_key="private:1001",
            job=job,
        )
    if phase == "commit":
        result = await invocation
        assert result.ok and result.deduplicated and result.mutation_id == mutation_ids[0]
    else:
        with pytest.raises(OperationalError) as caught:
            await invocation
        assert caught.value is acknowledgement
    assert len(resolutions) == len(mutation_ids) == 1
    async with database.immediate_session() as session:
        expected = int(phase == "commit")
        assert await session.scalar(select(func.count()).select_from(MemoryFactModel)) == expected
        assert (
            await session.scalar(select(func.count()).select_from(MemoryMutationReceiptModel))
            == expected
        )


@pytest.mark.parametrize("entry", ["tool", "worker"])
@pytest.mark.parametrize("cancel", [False, True])
async def test_prepared_mutation_waits_for_writer_without_resolution_replay(
    database, monkeypatch, entry, cancel
):
    mutations, facts, ledger, processor = _service(database)
    source = await _event(
        ledger, message_id=f"owner-wait-{entry}", sender_user_id="1001", content="我准备考研"
    )
    resolutions = []
    resolve, prepare = processor.resolve, facts.prepare_write
    prepared = asyncio.Event()

    def once(*args, **kwargs):
        resolutions.append(True)
        return resolve(*args, **kwargs)

    async def capture(*args, **kwargs):
        value = await prepare(*args, **kwargs)
        if kwargs.get("session") is None:
            prepared.set()
        return value

    monkeypatch.setattr(processor, "resolve", once)
    monkeypatch.setattr(facts, "prepare_write", capture)
    if entry == "tool":
        invocation = mutations.mutate(
            MemoryMutationRequest(
                operation=MemoryMutationOperation.CREATE,
                target=MemoryMutationTarget(
                    subject_ref="current_speaker", scope_type=MemoryScopeType.PERSON
                ),
                new_content="准备考研",
                memory_key="education:plan",
                category="education",
            ),
            _context(source),
        )
    else:
        claim = processor.validate(_claim(), source)
        jobs = MemoryJobRepository(database)
        await jobs.enqueue(source.id, source.scope.key)
        (job,) = await jobs.claim()
        invocation = mutations.mutate_validated_claim(
            claim, MemoryProcessingContext(event=source), conversation_key=source.scope.key, job=job
        )
    async with database.immediate_session() as writer:
        await writer.execute(text("UPDATE accounts SET display_name='unrelated commit'"))
        task = asyncio.create_task(invocation)
        await asyncio.wait_for(prepared.wait(), timeout=5)
        assert not task.done()
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    if not cancel:
        assert (await task).ok
    assert len(resolutions) == 1
    async with database.read_session() as reader:
        assert await reader.scalar(select(func.count()).select_from(MemoryFactModel)) == int(
            not cancel
        )
        assert await reader.scalar(
            select(func.count()).select_from(MemoryMutationReceiptModel)
        ) == int(not cancel)


@pytest.mark.parametrize("entry", ["tool", "worker"])
@pytest.mark.parametrize("change", ["binding_owner", "source_hidden"])
async def test_prepared_commit_keeps_original_owner_and_rejects_hidden_source(
    database, monkeypatch, entry, change
):
    mutations, facts, ledger, processor = _service(database)
    source = await _event(
        ledger,
        message_id=f"snapshot-rejection-{entry}-{change}",
        sender_user_id="1001",
        content="我准备考研",
    )
    resolve_calls = 0
    original_resolve = processor.resolve
    original_prepare = facts.prepare_write
    changed = False
    sqlite_errors = []

    def capture_error(exception_context):
        sqlite_errors.append(
            getattr(exception_context.original_exception, "sqlite_errorcode", None)
        )

    def resolve(*args, **kwargs):
        nonlocal resolve_calls
        resolve_calls += 1
        return original_resolve(*args, **kwargs)

    async def prepare(*args, **kwargs):
        nonlocal changed
        prepared = await original_prepare(*args, **kwargs)
        if kwargs.get("session") is None and not changed:
            changed = True
            async with database.immediate_session() as writer:
                if change == "binding_owner":
                    original = await find_identity_binding(writer, "1001")
                    target = await find_identity_binding(writer, "1002")
                    person = await writer.get(CanonicalPersonModel, target.person_id)
                    await replace_assignment(
                        writer, original.account, original.id, person, now=datetime.now(UTC)
                    )
                else:
                    await writer.execute(
                        text(
                            "UPDATE chat_events SET suppression_status='duplicate', "
                            "utterance_fingerprint=:fingerprint WHERE id=:event_id"
                        ),
                        {
                            "fingerprint": hashlib.sha256(source.content.encode()).hexdigest(),
                            "event_id": source.id,
                        },
                    )

        return prepared

    monkeypatch.setattr(processor, "resolve", resolve)
    monkeypatch.setattr(facts, "prepare_write", prepare)
    if entry == "tool":
        request = MemoryMutationRequest(
            operation=MemoryMutationOperation.CREATE,
            target=MemoryMutationTarget(
                subject_ref="current_speaker", scope_type=MemoryScopeType.PERSON
            ),
            new_content="准备考研",
            memory_key="education:plan",
            category="education",
        )
        operation = mutations.mutate(request, _context(source))
    else:
        claim = processor.validate(_claim(), source)
        assert claim is not None
        jobs = MemoryJobRepository(database)
        assert await jobs.enqueue(source.id, "private:1001")
        (job,) = await jobs.claim()
        operation = mutations.mutate_validated_claim(
            claim,
            MemoryProcessingContext(event=source),
            conversation_key="private:1001",
            job=job,
        )
    sqlalchemy_event.listen(database.engine.sync_engine, "handle_error", capture_error)
    try:
        if change == "source_hidden":
            with pytest.raises(MemoryMutationRejected, match="memory_resolution_snapshot_changed"):
                await operation
        else:
            result = await operation
            assert result.ok
    finally:
        sqlalchemy_event.remove(database.engine.sync_engine, "handle_error", capture_error)
    assert resolve_calls == 1
    assert sqlite_errors == []
    async with database.read_session() as reader:
        stored = list(await reader.scalars(select(MemoryFactModel)))
        if change == "binding_owner":
            assert len(stored) == 1
            assert stored[0].canonical_subject_person_id == source.author_person_id
            current_owner = (await find_identity_binding(reader, "1001")).person_id
            assert current_owner != source.author_person_id
            assert stored[0].canonical_subject_person_id != current_owner
        else:
            assert not stored
        assert await reader.scalar(
            select(func.count()).select_from(MemoryMutationReceiptModel)
        ) == (1 if change == "binding_owner" else 0)
