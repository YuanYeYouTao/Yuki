"""Paid Memory results finish through their original SQLite receipts and budgets."""

import asyncio

import pytest
from sqlalchemy import event as sql_event
from sqlalchemy import select, update
from tests.conftest import make_settings
from tests.support.model_executor import InjectedModelExecutor
from tests.unit.test_dream_busy_recovery import _case
from tests.unit.test_memory_mutation import _event, _service
from tests.unit.test_memory_rebuild import _service as rebuild_service
from tests.unit.test_memory_worker_shutdown import _Provider as ExtractionProvider

from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.memory.dream.db_models import MemoryDreamClusterModel
from qq_ai_bot.memory.dream.models import DreamClusterStatus, DreamOutput
from qq_ai_bot.memory.dream.service import DreamBudgetExhausted
from qq_ai_bot.memory.enums import MemoryRebuildJobOutcome
from qq_ai_bot.memory.job_claims import MemoryJobClaimLost
from qq_ai_bot.memory.repository import MemoryFactRepository, MemoryJobRepository
from qq_ai_bot.memory.self_reflection.models import SelfReflectionOutput
from qq_ai_bot.memory.self_reflection.repository import SelfReflectionRepository
from qq_ai_bot.memory.self_reflection.service import (
    _CHECKPOINT,
    ReflectionCheckpoint,
    SelfReflectionService,
)
from qq_ai_bot.memory.self_reflection.worker import SelfReflectionWorker
from qq_ai_bot.memory.worker import MemoryWorker
from qq_ai_bot.persistence.models import (
    MemoryFactModel,
    MemoryJobModel,
    MemoryMutationReceiptModel,
    MemorySelfReflectionRunModel,
)
from qq_ai_bot.services.concurrency import ConcurrencyManager


@pytest.mark.parametrize("merge_owner", [False, True])
async def test_reflection_checkpoint_finishes_after_the_original_daily_request_is_spent(
    database, merge_owner
):
    mutations, _, ledger, _ = _service(database, self_memory_enabled=True)
    repository = SelfReflectionRepository(database)
    await repository.scan_new_events()
    source = await _event(
        ledger,
        message_id="reflection-paid-tail",
        sender_user_id="1001",
        content="我觉得你很喜欢认真核对事实",
    )
    settings = make_settings(
        database.url,
        self_memory_enabled=True,
        memory_self_reflection_enabled=True,
        memory_self_reflection_event_threshold=1,
        memory_self_reflection_character_threshold=1,
        memory_self_reflection_max_daily_calls=1,
    )
    await repository.scan_new_events()
    (batch,) = await repository.claim_due(
        scheduled_slot="original-paid",
        event_threshold=1,
        character_threshold=1,
        max_wait_seconds=0,
        max_events=10,
        max_characters=16000,
        force=True,
    )
    provider = FakeLLMProvider(lambda _: (_ for _ in ()).throw(AssertionError("replayed model")))
    service = SelfReflectionService(
        settings=settings,
        repository=repository,
        mutations=mutations,
        models=InjectedModelExecutor(provider),
    )
    _, event_map, tool_map = await service._input(batch)
    output = SelfReflectionOutput.model_validate(
        {
            "proposals": [
                {
                    "category": "self_preference",
                    "kind": "preference",
                    "memory_key": "self:verify",
                    "content": "我喜欢认真核对事实",
                    "importance": 3,
                    "confidence": 0.8,
                    "reason": "current feedback",
                    "evidence_refs": ["event_1"],
                    "visibility": "current_scope",
                }
            ]
        }
    )
    await repository.save_checkpoint(
        batch.run_id,
        _CHECKPOINT.dump_json(
            ReflectionCheckpoint(output.model_dump(mode="json"), event_map, tool_map)
        ).decode(),
    )
    worker = SelfReflectionWorker(settings=settings, repository=repository, service=service)
    paid = await worker.control.reserve_request(batch.run_id)
    await worker.control.finish_request(paid, "completed", 12)
    if merge_owner:
        from tests.unit.test_memory_person_identity import merge_people

        from qq_ai_bot.identity.canonical_repository import find_identity_binding

        async with database.sessions() as reader:
            target = (await find_identity_binding(reader, "1002")).person_id
            saved = (await reader.get(MemorySelfReflectionRunModel, batch.run_id)).checkpoint_json
        await merge_people(database, source.author_person_id, target)
        async with database.sessions() as reader:
            assert (
                await reader.get(MemorySelfReflectionRunModel, batch.run_id)
            ).checkpoint_json == saved
    assert await repository.recover_interrupted(batch.run_id, "sqlite_busy") == "failed"
    await worker.run_now(
        source_event_id=source.id, conversation_id=source.canonical_conversation_id
    )
    assert await worker.process_once() == 1
    assert not provider.requests
    assert (await worker.control.snapshot())["calls_today"] == 1
    async with database.sessions() as reader:
        original = await reader.get(MemorySelfReflectionRunModel, batch.run_id)
        assert original.status == "completed" and original.committed_count == 1


async def test_dream_worker_applies_paid_preview_at_limit_and_defers_only_new_request(
    database, monkeypatch
):
    _, dreams, service, run, worker, calls = await _case(database, monkeypatch, clusters=3)
    settings = make_settings(database.url, memory_dream_max_model_calls_per_run=2)
    service._settings = worker._settings = settings
    first = await dreams.claim_next_cluster(run.public_id)
    _, operations, valid = await service.process_cluster(run, first)
    assert valid
    await dreams.finish_cluster(
        first.id, status=DreamClusterStatus.COMPLETED, operation_count=operations
    )
    second = await dreams.claim_next_cluster(run.public_id)
    assert await dreams.reserve_model_call(
        run_public_id=run.public_id, cluster_id=second.id, maximum=2
    )
    _, _, fingerprint = await service._input(await dreams.cluster_facts(second))
    await dreams.save_preview(
        cluster_id=second.id,
        source_fingerprint=fingerprint,
        proposal=DreamOutput(),
        model_calls=1,
        source_characters=10,
        output_characters=0,
    )
    async with database.immediate_session() as writer:
        await writer.execute(
            update(MemoryDreamClusterModel)
            .where(MemoryDreamClusterModel.id == second.id)
            .values(status="pending")
        )

    async def no_more_requests(*args, **kwargs):
        assert not await dreams.reserve_model_call(
            run_public_id=run.public_id, cluster_id=kwargs["cluster"].id, maximum=2
        )
        raise DreamBudgetExhausted()

    monkeypatch.setattr(service, "_decide", no_more_requests)
    await worker._drain_active()
    assert len(calls) == 1 and (await dreams.get_run(run.public_id)).model_calls == 2
    async with database.sessions() as reader:
        rows = list(
            await reader.scalars(
                select(MemoryDreamClusterModel).order_by(MemoryDreamClusterModel.id)
            )
        )
    assert rows[1].status == "completed" and rows[1].error_category is None
    assert rows[2].status == "skipped" and rows[2].error_category == "budget_deferred"


async def test_job_completion_retries_only_database_after_real_wal_snapshot_conflict(
    database, monkeypatch
):
    _, _, ledger, _ = _service(database)
    event = await _event(
        ledger, message_id="completion-busy", sender_user_id="1001", content="evidence"
    )
    jobs = MemoryJobRepository(database)
    assert await jobs.enqueue(event.id, event.scope.key)
    (job,) = await jobs.claim()
    original = MemoryFactRepository.apply_evidence_write
    attempts = []
    native_errors = []

    async def interleave(repository, operation):
        async def complete(session):
            await session.scalar(select(MemoryJobModel.id).where(MemoryJobModel.id == job.id))
            attempts.append(job.id)
            if len(attempts) == 1:
                async with database.immediate_session() as other:
                    await other.execute(
                        update(MemoryJobModel)
                        .where(MemoryJobModel.id == job.id)
                        .values(error_category="unrelated metadata")
                    )
            return await operation(session)

        return await original(repository, complete)

    def error(context):
        native_errors.append(getattr(context.original_exception, "sqlite_errorcode", None))

    monkeypatch.setattr(MemoryFactRepository, "apply_evidence_write", interleave)
    sql_event.listen(database.engine.sync_engine, "handle_error", error)
    try:
        await jobs.complete(job, outcome=MemoryRebuildJobOutcome.NO_CLAIMS)
    finally:
        sql_event.remove(database.engine.sync_engine, "handle_error", error)
    assert native_errors == [517] and attempts == [job.id, job.id]
    async with database.sessions() as reader:
        row = await reader.get(MemoryJobModel, job.id)
        assert row.status == "done" and row.attempts == job.attempts
    with pytest.raises(MemoryJobClaimLost):
        await jobs.complete(job, outcome=MemoryRebuildJobOutcome.NO_CLAIMS)


async def test_worker_keeps_paid_output_and_applied_claim_during_completion_contention(
    database, monkeypatch
):
    provider = ExtractionProvider()
    settings, ledger, facts, _, _ = await rebuild_service(
        database,
        provider=provider,
        memory_batch_trigger_count=1,
        memory_batch_max_wait_seconds=0,
    )
    source = await _event(
        ledger, message_id="worker-paid-completion", sender_user_id="1001", content="我准备考研"
    )
    jobs = MemoryJobRepository(database)
    worker = MemoryWorker(
        settings=settings,
        jobs=jobs,
        facts=facts,
        ledger=ledger,
        model_executor=InjectedModelExecutor(provider),
        concurrency=ConcurrencyManager(),
    )
    assert await worker.enqueue(source.id, source.scope.key)
    async with database.immediate_session() as writer:
        await writer.execute(update(MemoryJobModel).values(attempts=7))
        original_job_id = await writer.scalar(select(MemoryJobModel.id))
    original_write = MemoryFactRepository.apply_evidence_write
    original_complete = jobs.complete
    completing, attempts, errors = False, [], []

    async def complete(*args, **kwargs):
        nonlocal completing
        completing = True
        try:
            return await original_complete(*args, **kwargs)
        finally:
            completing = False

    async def write(repository, operation):
        if not completing:
            return await original_write(repository, operation)

        async def interleave(session):
            await session.scalar(select(MemoryJobModel.id))
            attempts.append(original_job_id)
            if len(attempts) == 1:
                async with database.immediate_session() as other:
                    await other.execute(
                        update(MemoryJobModel).values(error_category="competing metadata commit")
                    )
            return await operation(session)

        return await original_write(repository, interleave)

    def record_error(context):
        errors.append(getattr(context.original_exception, "sqlite_errorcode", None))

    monkeypatch.setattr(jobs, "complete", complete)
    monkeypatch.setattr(MemoryFactRepository, "apply_evidence_write", write)
    sql_event.listen(database.engine.sync_engine, "handle_error", record_error)
    try:
        assert await worker.process_once() == 1
        assert await worker.process_once() == 0
    finally:
        sql_event.remove(database.engine.sync_engine, "handle_error", record_error)
    assert errors == [517] and attempts == [original_job_id, original_job_id]
    assert provider.requests == 1
    async with database.sessions() as reader:
        job = await reader.get(MemoryJobModel, original_job_id)
        assert job.status == "done" and job.attempts == 7 and job.outcome == "claims_applied"
        assert len(list(await reader.scalars(select(MemoryFactModel)))) == 1
        assert len(list(await reader.scalars(select(MemoryMutationReceiptModel)))) == 1


@pytest.mark.parametrize("cancel", [False, True])
async def test_job_completion_reads_unknown_commit_and_keeps_cancellation(
    database, monkeypatch, cancel
):
    _, _, ledger, _ = _service(database)
    event = await _event(
        ledger, message_id="completion-ack", sender_user_id="1001", content="evidence"
    )
    jobs = MemoryJobRepository(database)
    await jobs.enqueue(event.id, event.scope.key)
    (job,) = await jobs.claim()
    original = MemoryFactRepository.apply_evidence_write

    async def lost_ack(repository, operation):
        await original(repository, operation)
        if cancel:
            raise asyncio.CancelledError
        raise OSError("commit acknowledgement unavailable")

    monkeypatch.setattr(MemoryFactRepository, "apply_evidence_write", lost_ack)
    if cancel:
        with pytest.raises(asyncio.CancelledError):
            await jobs.complete(job)
    else:
        await jobs.complete(job)
    async with database.sessions() as reader:
        row = await reader.get(MemoryJobModel, job.id)
        assert row.status == "done" and row.attempts == 0
