"""Only pure DB preparation retries a real WAL stale read snapshot."""

from __future__ import annotations

import asyncio
import hashlib
import sqlite3
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import event as sqlalchemy_event
from sqlalchemy import func, select, text
from sqlalchemy.exc import OperationalError
from tests.unit.test_memory_mutation import _context, _event, _service
from tests.unit.test_memory_v2 import _claim

from qq_ai_bot.memory.claim_processor import MemoryProcessingContext
from qq_ai_bot.memory.enums import MemoryProcessingSource, MemoryScopeType
from qq_ai_bot.memory.mutation import service as mutation_service
from qq_ai_bot.memory.mutation.models import (
    MemoryMutationOperation,
    MemoryMutationRequest,
    MemoryMutationTarget,
)
from qq_ai_bot.memory.mutation.service import MemoryMutationRejected
from qq_ai_bot.memory.repository import MemoryJobRepository
from qq_ai_bot.persistence.models import MemoryFactModel, MemoryMutationReceiptModel


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
    transaction = facts.repository.transaction
    resolve = processor.resolve
    reserve = mutations._receipts.reserve
    finalize = mutations._receipts.finalize
    resolutions, mutation_ids = [], []
    native = sqlite3.OperationalError("unknown transaction acknowledgement")
    native.sqlite_errorcode = code
    acknowledgement = OperationalError(phase, {}, native)

    @asynccontextmanager
    async def unknown_ack(*, read_snapshot=False):
        try:
            async with transaction(read_snapshot=read_snapshot) as session:
                yield session
        except OperationalError:
            if read_snapshot:
                raise acknowledgement from None
            raise
        if read_snapshot:
            raise acknowledgement

    async def resolve_once(*args, **kwargs):
        resolutions.append(True)
        return await resolve(*args, **kwargs)

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

    monkeypatch.setattr(facts.repository, "transaction", unknown_ack)
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
            MemoryProcessingContext(source=MemoryProcessingSource.LIVE, event=source),
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
@pytest.mark.parametrize("code", [5, 517])
@pytest.mark.parametrize("stop", ["complete", "cancel"])
async def test_four_busy_rollbacks_reprepare_without_repeating_resolution(
    database, monkeypatch, entry, code, stop
):
    mutations, facts, ledger, processor = _service(database)
    source = await _event(
        ledger, message_id=f"snapshot-{entry}", sender_user_id="1001", content="我准备考研"
    )
    resolve_calls = 0
    preparations = 0
    preparation_sessions = set()
    reserved_ids = []
    original_resolve = processor.resolve
    original_prepare = facts.prepare_evidence_write
    original_reserve = mutations._receipts.reserve
    original_fence = mutation_service.fence_memory_job_claim
    fence_calls = 0
    errors = []
    fourth = asyncio.Event()

    async def resolve(*args, **kwargs):
        nonlocal resolve_calls
        resolve_calls += 1
        return await original_resolve(*args, **kwargs)

    async def prepare(*args, **kwargs):
        nonlocal preparations
        preparations += 1
        preparation_sessions.add(kwargs["session"])
        await original_prepare(*args, **kwargs)
        if (
            code == 517
            and (len(preparation_sessions) <= 4 or stop == "cancel")
            and not kwargs["session"].info.get("memory_evidence_write_started")
        ):
            # A separate, harmless identity-label commit advances the real WAL.
            # This leaves the decision input and original queue claim unchanged.
            async with database.immediate_session() as writer:
                await writer.execute(
                    text("UPDATE identity_bindings SET display_name=:label"),
                    {"label": f"snapshot-gate-{preparations}"},
                )

    async def reserve(**kwargs):
        reserved_ids.append(kwargs["mutation_id"])
        if code == 5 and entry == "tool" and (len(reserved_ids) <= 4 or stop == "cancel"):
            async with database.immediate_session() as writer:
                await writer.execute(
                    text("UPDATE identity_bindings SET display_name='busy-writer'")
                )
                return await original_reserve(**kwargs)
        return await original_reserve(**kwargs)

    async def fence(session, job):
        nonlocal fence_calls
        fence_calls += 1
        if code == 5 and (fence_calls <= 4 or stop == "cancel"):
            async with database.immediate_session() as writer:
                await writer.execute(
                    text("UPDATE identity_bindings SET display_name='busy-writer'")
                )
                return await original_fence(session, job)
        return await original_fence(session, job)

    def capture(context):
        errors.append(getattr(context.original_exception, "sqlite_errorcode", None))
        if len(errors) >= 4:
            fourth.set()

    monkeypatch.setattr(processor, "resolve", resolve)
    monkeypatch.setattr(facts, "prepare_evidence_write", prepare)
    monkeypatch.setattr(mutations._receipts, "reserve", reserve)
    monkeypatch.setattr(mutation_service, "fence_memory_job_claim", fence)
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
        invocation = mutations.mutate(request, _context(source))
    else:
        claim = processor.validate(_claim(), source)
        assert claim is not None
        jobs = MemoryJobRepository(database)
        assert await jobs.enqueue(source.id, "private:1001")
        (job,) = await jobs.claim()
        invocation = mutations.mutate_validated_claim(
            claim,
            MemoryProcessingContext(source=MemoryProcessingSource.LIVE, event=source),
            conversation_key="private:1001",
            job=job,
        )
    engine = database.engine.sync_engine
    sqlalchemy_event.listen(engine, "handle_error", capture)
    task = asyncio.create_task(invocation)
    try:
        if stop == "cancel":
            await asyncio.wait_for(fourth.wait(), timeout=5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            result = await task
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        sqlalchemy_event.remove(engine, "handle_error", capture)
    assert errors[:4] == [code] * 4 and resolve_calls == 1
    if stop == "cancel":
        assert len(set(reserved_ids)) <= 1
        async with database.immediate_session() as writer:
            assert await writer.scalar(select(func.count()).select_from(MemoryFactModel)) == 0
            assert (
                await writer.scalar(select(func.count()).select_from(MemoryMutationReceiptModel))
                == 0
            )
        return
    assert result.ok and len(preparation_sessions) == 5
    assert len(reserved_ids) == (5 if entry == "tool" else 1)
    assert set(reserved_ids) == {result.mutation_id}
    async with database.sessions() as reader:
        assert await reader.scalar(select(func.count()).select_from(MemoryFactModel)) == 1
        assert (
            await reader.scalar(select(func.count()).select_from(MemoryMutationReceiptModel)) == 1
        )


@pytest.mark.parametrize("entry", ["tool", "worker"])
@pytest.mark.parametrize("change", ["binding_owner", "source_hidden"])
async def test_retry_rejects_changed_claim_identity_or_source(database, monkeypatch, entry, change):
    mutations, facts, ledger, processor = _service(database)
    source = await _event(
        ledger,
        message_id=f"snapshot-rejection-{entry}-{change}",
        sender_user_id="1001",
        content="我准备考研",
    )
    resolve_calls = 0
    original_resolve = processor.resolve
    original_prepare = facts.prepare_evidence_write
    changed = False
    sqlite_errors = []

    def capture_error(exception_context):
        sqlite_errors.append(
            getattr(exception_context.original_exception, "sqlite_errorcode", None)
        )

    async def resolve(*args, **kwargs):
        nonlocal resolve_calls
        resolve_calls += 1
        return await original_resolve(*args, **kwargs)

    async def prepare(*args, **kwargs):
        nonlocal changed
        await original_prepare(*args, **kwargs)
        if not changed:
            changed = True
            async with database.immediate_session() as writer:
                if change == "binding_owner":
                    await writer.execute(
                        text(
                            "UPDATE identity_bindings SET person_id=(SELECT person_id "
                            "FROM identity_bindings WHERE platform='qq' "
                            "AND external_account_id='1002'), revision=revision+1 "
                            "WHERE platform='qq' AND external_account_id='1001'"
                        )
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

    monkeypatch.setattr(processor, "resolve", resolve)
    monkeypatch.setattr(facts, "prepare_evidence_write", prepare)
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
            MemoryProcessingContext(source=MemoryProcessingSource.LIVE, event=source),
            conversation_key="private:1001",
            job=job,
        )
    sqlalchemy_event.listen(database.engine.sync_engine, "handle_error", capture_error)
    try:
        with pytest.raises(MemoryMutationRejected, match="memory_resolution_snapshot_changed"):
            await operation
    finally:
        sqlalchemy_event.remove(database.engine.sync_engine, "handle_error", capture_error)
    assert resolve_calls == 1
    assert 517 in sqlite_errors  # Real BUSY_SNAPSHOT, rather than a mocked retry exception.
    async with database.sessions() as reader:
        assert await reader.scalar(select(func.count()).select_from(MemoryFactModel)) == 0
        assert (
            await reader.scalar(select(func.count()).select_from(MemoryMutationReceiptModel)) == 0
        )
