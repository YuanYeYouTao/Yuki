"""Worker close cancels a real contended database unit without losing its owner."""

import asyncio
import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, func, select, update
from tests.support.model_executor import InjectedModelExecutor
from tests.unit.test_memory_rebuild import _event, _ExtractionProvider, _service
from tests.unit.test_memory_v2 import _claim

from qq_ai_bot.domain.messages import ChatResponse
from qq_ai_bot.memory.enums import MemoryRebuildRunStatus
from qq_ai_bot.memory.extraction import BatchMemoryClaim, BatchMemoryExtractionOutput
from qq_ai_bot.memory.rebuild.models import MemoryRebuildSelection
from qq_ai_bot.memory.rebuild.worker import MemoryRebuildWorker
from qq_ai_bot.memory.repository import MemoryJobRepository
from qq_ai_bot.memory.worker import MemoryWorker
from qq_ai_bot.persistence.models import (
    MemoryFactModel,
    MemoryJobModel,
    MemoryMutationReceiptModel,
    MemoryRebuildProposalModel,
)
from qq_ai_bot.services.concurrency import ConcurrencyManager


class _Provider(_ExtractionProvider):
    async def complete(self, request):
        payload = json.loads(request.messages[-1].content or "{}")
        if "events" not in payload:
            return await super().complete(request)
        self.requests += 1
        return ChatResponse(
            content=BatchMemoryExtractionOutput(
                claims=(
                    BatchMemoryClaim(
                        source_event_id=payload["events"][0]["source_event_id"], claim=_claim()
                    ),
                )
            ).model_dump_json(),
            latency_seconds=0,
        )


@pytest.mark.parametrize("kind", ["live", "rebuild"])
async def test_close_during_real_busy_preserves_original_owner_and_committed_truth(
    database, monkeypatch, kind
):
    provider = _Provider()
    settings, ledger, facts, _, service = await _service(
        database,
        provider=provider,
        memory_batch_seconds=0.01,
        memory_batch_trigger_count=1,
        memory_batch_max_wait_seconds=0,
    )
    source = await _event(ledger, message_id="shutdown", content="我准备考研")
    if kind == "live":
        jobs = MemoryJobRepository(database)
        worker = MemoryWorker(
            settings=settings,
            jobs=jobs,
            facts=facts,
            ledger=ledger,
            model_executor=InjectedModelExecutor(provider),
            concurrency=ConcurrencyManager(1),
        )
        assert await worker.enqueue(source.id, "private:1001")
        async with database.immediate_session() as session:
            await session.execute(update(MemoryJobModel).values(attempts=7))
    else:
        run = await service.plan(MemoryRebuildSelection(all_events=True), actor_user_id="9000")
        await service.start(run.public_id, actor_user_id="9000")
        worker = MemoryRebuildWorker(service, interval_seconds=0.01)
        assert await worker.process_once() == 1
        assert await worker.process_once() == 0
        await service.set_review(run.public_id, "all", approved=True, actor_user_id="9000")
        proposals = await service.review(run.public_id, actor_user_id="9000")
        original_proposal_id = proposals[0].proposal_id
        original_statistics = await service.repository.statistics(run.public_id)

    ready, write, busy = asyncio.Event(), asyncio.Event(), asyncio.Event()
    prepare = facts.prepare_evidence_write

    async def prepared(*args, **kwargs):
        result = await prepare(*args, **kwargs)
        if "memory_evidence_counts" in kwargs["session"].info:
            ready.set()
            await write.wait()
        return result

    errors = []

    def record(context):
        errors.append(getattr(context.original_exception, "sqlite_errorcode", None))
        busy.set()

    monkeypatch.setattr(facts, "prepare_evidence_write", prepared)
    event.listen(database.engine.sync_engine, "handle_error", record)
    await worker.start()
    try:
        if kind == "rebuild":
            await service.commit(run.public_id, actor_user_id="9000")
        await asyncio.wait_for(ready.wait(), timeout=5)
        async with database.immediate_session() as writer:
            # An independent connection holds the writer while the worker upgrades
            # its already prepared snapshot. This produces actual native BUSY 5.
            await writer.execute(
                update(MemoryFactModel).values(updated_at=MemoryFactModel.updated_at)
            )
            write.set()
            await asyncio.wait_for(busy.wait(), timeout=5)
            task = worker._task
            await asyncio.wait_for(worker.close(), timeout=5)
            assert task.done() and task.cancelled()
        assert errors[0] == sqlite3.SQLITE_BUSY
        assert all(code in {None, sqlite3.SQLITE_BUSY} for code in errors)
        assert provider.requests == 1
        async with database.sessions() as session:
            assert await session.scalar(select(func.count()).select_from(MemoryFactModel)) == 0
            assert (
                await session.scalar(select(func.count()).select_from(MemoryMutationReceiptModel))
                == 0
            )
            if kind == "live":
                job = await session.scalar(select(MemoryJobModel))
                assert job.status == "processing" and job.error_category is None
                identity, attempts = job.id, job.attempts
            else:
                proposal = await session.get(MemoryRebuildProposalModel, original_proposal_id)
                assert proposal.commit_status == "pending"
                current = await service.repository.get_run(run.public_id)
                assert current.status is MemoryRebuildRunStatus.COMMITTING
                assert await service.repository.statistics(run.public_id) == original_statistics
        if kind == "live":
            async with database.immediate_session() as session:
                await session.execute(
                    update(MemoryJobModel)
                    .where(MemoryJobModel.id == identity)
                    .values(updated_at=datetime.now(UTC) - timedelta(minutes=6))
                )
            (recovered,) = await jobs.claim()
            assert recovered.id == identity and recovered.attempts == attempts == 7
            # Reuse the already obtained claim in this test process; no new extraction.
            result = await worker._process_claims(recovered, (_claim(),))
            await jobs.complete(recovered, outcome=result.outcome)
            async with database.sessions() as session:
                assert (await session.get(MemoryJobModel, identity)).status == "done"
        else:
            assert await worker.process_once() == 1
            current = await service.repository.get_run(run.public_id)
            assert current.public_id == run.public_id
            assert current.status is MemoryRebuildRunStatus.COMPLETED
            async with database.sessions() as session:
                proposal = await session.get(MemoryRebuildProposalModel, original_proposal_id)
                assert proposal.commit_status == "committed"
        assert provider.requests == 1
        await worker.close()
    finally:
        write.set()
        await worker.close()
        event.remove(database.engine.sync_engine, "handle_error", record)
