"""Only pure DB preparation retries a real WAL stale read snapshot."""

from __future__ import annotations

import hashlib

import pytest
from sqlalchemy import event as sqlalchemy_event
from sqlalchemy import func, select, text
from tests.unit.test_memory_mutation import _context, _event, _service
from tests.unit.test_memory_v2 import _claim

from qq_ai_bot.memory.claim_processor import MemoryProcessingContext
from qq_ai_bot.memory.enums import MemoryProcessingSource, MemoryScopeType
from qq_ai_bot.memory.mutation.models import (
    MemoryMutationOperation,
    MemoryMutationRequest,
    MemoryMutationTarget,
)
from qq_ai_bot.memory.mutation.service import MemoryMutationRejected
from qq_ai_bot.memory.repository import MemoryJobRepository
from qq_ai_bot.persistence.models import MemoryFactModel, MemoryMutationReceiptModel


@pytest.mark.parametrize("entry", ["tool", "worker"])
async def test_stale_snapshot_reprepares_without_repeating_resolution(database, monkeypatch, entry):
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

    async def resolve(*args, **kwargs):
        nonlocal resolve_calls
        resolve_calls += 1
        return await original_resolve(*args, **kwargs)

    async def prepare(*args, **kwargs):
        nonlocal preparations
        preparations += 1
        preparation_sessions.add(kwargs["session"])
        await original_prepare(*args, **kwargs)
        if preparations == 1:
            # A separate, harmless identity-label commit advances the real WAL.
            # This leaves the decision input and original queue claim unchanged.
            async with database.immediate_session() as writer:
                await writer.execute(
                    text("UPDATE identity_bindings SET display_name='snapshot-gate'")
                )

    async def reserve(**kwargs):
        reserved_ids.append(kwargs["mutation_id"])
        return await original_reserve(**kwargs)

    monkeypatch.setattr(processor, "resolve", resolve)
    monkeypatch.setattr(facts, "prepare_evidence_write", prepare)
    monkeypatch.setattr(mutations._receipts, "reserve", reserve)
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
        result = await mutations.mutate(request, _context(source))
        assert len(reserved_ids) == 2  # First receipt INSERT rejected the stale snapshot.
    else:
        claim = processor.validate(_claim(), source)
        assert claim is not None
        jobs = MemoryJobRepository(database)
        assert await jobs.enqueue(source.id, "private:1001")
        (job,) = await jobs.claim()
        result = await mutations.mutate_validated_claim(
            claim,
            MemoryProcessingContext(source=MemoryProcessingSource.LIVE, event=source),
            conversation_key="private:1001",
            job=job,
        )
        assert len(reserved_ids) == 1  # The earlier job fence rejected before receipt INSERT.
    assert result.ok and resolve_calls == 1 and len(preparation_sessions) == 2
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
