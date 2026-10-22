"""Real WAL contention reprepares the original database plan without model replay."""

import asyncio
import sqlite3
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.exc import OperationalError
from tests.conftest import make_settings
from tests.unit.test_memory_dream import _empty_dream_statistics, _fact_with_evidence, _services

from yuki.memory.dream.db_models import (
    MemoryDreamClusterModel,
    MemoryDreamOperationModel,
    MemoryDreamRunModel,
)
from yuki.memory.dream.models import (
    DreamAction,
    DreamClusterStatus,
    DreamOperationType,
    DreamOutput,
    DreamRunMode,
)
from yuki.memory.dream.service import DreamService
from yuki.memory.dream.worker import DreamWorker


async def _case(database, monkeypatch, *, clusters=1):
    mutations, facts, ledger, dreams = _services(database)
    rows = tuple(
        [
            await _fact_with_evidence(
                facts,
                ledger,
                message_id=f"busy-{index}",
                memory_key=f"busy:{index}",
                content=f"source {index}",
            )
            for index in range(clusters * 2)
        ]
    )
    service = object.__new__(DreamService)
    settings = make_settings(database.url)
    service._settings = settings
    service._facts, service._mutations, service._repository = facts, mutations, dreams
    run = await dreams.create_run(
        mode=DreamRunMode.FULL,
        statistics=_empty_dream_statistics(),
        clusters=tuple(
            (
                f"cluster-{index}",
                "partition",
                "8000",
                "fact",
                tuple(row.id for row in rows[index * 2 : index * 2 + 2]),
                service._cluster_fingerprint(rows[index * 2 : index * 2 + 2]),
            )
            for index in range(clusters)
        ),
        snapshot_max_fact_id=rows[-1].id,
        actor_user_id=None,
        scheduled_slot=None,
    )
    assert await dreams.start_run(run.public_id)
    calls = []

    async def decide(_payload, *, run, cluster, **_kwargs):
        assert await dreams.reserve_model_call(
            run_public_id=run.public_id,
            cluster_id=cluster.id,
            maximum=settings.memory_dream_max_model_calls_per_run,
        )
        calls.append(cluster.id)
        return DreamOutput(
            actions=(
                DreamAction(operation=DreamOperationType.KEEP, source_refs=("memory_1",)),
                DreamAction(operation=DreamOperationType.KEEP, source_refs=("memory_2",)),
            )
        ), 1

    monkeypatch.setattr(service, "_decide", decide)
    worker = DreamWorker(settings=settings, repository=dreams, service=service)
    return facts, dreams, service, run, worker, calls


@pytest.mark.parametrize("cancel", [False, True])
async def test_paid_cluster_waits_for_shared_writer_without_model_replay(
    database, monkeypatch, cancel
):
    _facts, dreams, service, _run, worker, calls = await _case(database, monkeypatch)
    decide, save = service._decide, dreams.save_preview
    holding, release, prepared = asyncio.Event(), asyncio.Event(), asyncio.Event()
    other = None

    async def hold():
        async with database.immediate_session() as writer:
            await writer.execute(update(MemoryDreamRunModel).values(updated_at=datetime.now(UTC)))
            holding.set()
            await release.wait()

    async def decide_and_hold(*args, **kwargs):
        nonlocal other
        output = await decide(*args, **kwargs)
        other = asyncio.create_task(hold())
        await holding.wait()
        return output

    async def capture(*args, **kwargs):
        prepared.set()
        return await save(*args, **kwargs)

    monkeypatch.setattr(service, "_decide", decide_and_hold)
    monkeypatch.setattr(dreams, "save_preview", capture)
    task = asyncio.create_task(worker._drain_active())
    try:
        await asyncio.wait_for(prepared.wait(), timeout=5)
        assert len(calls) == 1 and not task.done()
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        release.set()
        await other
        if not cancel:
            await task
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if other:
            await asyncio.gather(other, return_exceptions=True)
    async with database.read_session() as reader:
        cluster = await reader.scalar(select(MemoryDreamClusterModel))
        assert cluster.model_calls == 1
        assert await reader.scalar(select(func.count()).select_from(MemoryDreamOperationModel)) == (
            0 if cancel else 2
        )
        assert cluster.status == ("processing" if cancel else "completed")
    assert len(calls) == 1


async def test_original_committed_receipt_is_not_overwritten_as_zero_failure(database, monkeypatch):
    _, dreams, service, run, worker, calls = await _case(database, monkeypatch)
    cluster = await dreams.claim_next_cluster(run.public_id)
    await service.process_cluster(run, cluster)
    await worker._finish_cluster_failure(
        cluster.id, status=DreamClusterStatus.FAILED, error_category="test_exhaustion"
    )
    async with database.read_session() as reader:
        current = await reader.get(MemoryDreamClusterModel, cluster.id)
        assert (current.status, current.operation_count, current.model_calls) == ("completed", 2, 1)
        assert current.error_category == "recovered_committed_operation"
        assert await reader.scalar(select(func.count()).select_from(MemoryDreamOperationModel)) == 2
    assert calls == [cluster.id]


@pytest.mark.parametrize("category", ["OperationalError", "OSError"])
async def test_process_commit_acknowledgement_loss_preserves_actual_operations(
    database, monkeypatch, category
):
    facts, dreams, _, _run, worker, calls = await _case(database, monkeypatch)
    original = facts.repository.write_session
    native = sqlite3.OperationalError("commit acknowledgement lost")
    native.sqlite_errorcode = 5
    failure = (
        OperationalError("COMMIT", {}, native)
        if category == "OperationalError"
        else OSError("commit acknowledgement lost")
    )

    @asynccontextmanager
    async def lost_confirmation(prepared):
        async with original(prepared) as session:
            yield session
        raise failure

    monkeypatch.setattr(facts.repository, "write_session", lost_confirmation)
    if category == "OperationalError":
        with pytest.raises(OperationalError) as caught:
            await worker._drain_active()
        assert caught.value is failure
    else:
        await worker._drain_active()
    async with database.read_session() as reader:
        assert await reader.scalar(select(func.count()).select_from(MemoryDreamOperationModel)) == 2
    assert len(calls) == 1
    if category == "OperationalError":
        assert await dreams.reset_processing_after_restart() == 1
    await worker._drain_active()
    async with database.read_session() as reader:
        cluster = await reader.scalar(select(MemoryDreamClusterModel))
        assert (cluster.status, cluster.model_calls, cluster.operation_count) == ("completed", 1, 2)
    assert len(calls) == 1


async def test_second_action_failure_rolls_back_whole_cluster_with_original_paid_call(
    database, monkeypatch
):
    _facts, dreams, service, run, _, calls = await _case(database, monkeypatch)
    cluster = await dreams.claim_next_cluster(run.public_id)
    mutate = service._mutations.mutate_dream
    actions = []

    async def fail_second(**kwargs):
        actions.append(kwargs["mutation_id"])
        if len(actions) == 2:
            raise OSError("second action")
        return await mutate(**kwargs)

    monkeypatch.setattr(service._mutations, "mutate_dream", fail_second)
    with pytest.raises(OSError, match="second action"):
        await service.process_cluster(run, cluster)
    async with database.read_session() as reader:
        assert await reader.scalar(select(func.count()).select_from(MemoryDreamOperationModel)) == 0
        from yuki.persistence.models import MemoryMutationReceiptModel

        assert (
            await reader.scalar(select(func.count()).select_from(MemoryMutationReceiptModel)) == 0
        )
    assert len(actions) == 2 and len(set(actions)) == 2 and len(calls) == 1
    assert (await dreams.get_run(run.public_id)).model_calls == 1
