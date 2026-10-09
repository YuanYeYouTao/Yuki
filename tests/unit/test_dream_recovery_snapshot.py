"""Restart recovery waits for the writer and uses original durable operation receipts."""

import asyncio
import json
from types import SimpleNamespace

from sqlalchemy import event, func, select, update
from tests.unit.test_dream_plan_transactions import create, sources, specs
from tests.unit.test_memory_writer_boundaries import other_writer_and_read_only

from qq_ai_bot.memory.dream import repository as dream_repository
from qq_ai_bot.memory.dream.db_models import (
    MemoryDreamClusterModel,
    MemoryDreamOperationModel,
    MemoryDreamRunModel,
)
from qq_ai_bot.memory.dream.models import (
    DreamOperationType,
    DreamOutput,
    DreamPlanStatistics,
    DreamRunMode,
)


async def test_dream_empty_recovery_is_read_only_with_other_writer(database):
    dreams, _ = await sources(database)
    async with other_writer_and_read_only(database):
        assert await dreams.reset_processing_after_restart() == 0


async def test_dream_recovery_waits_for_writer_and_reads_committed_operation_receipt(
    database, monkeypatch
):
    dreams, rows = await sources(database)
    run = await create(dreams, specs(rows), mode=DreamRunMode.INCREMENTAL)
    async with database.immediate_session() as writer:
        cluster = await writer.scalar(select(MemoryDreamClusterModel))
        cluster.status = "processing"
        cluster.attempts = 2
        cluster.model_calls = 2
        await writer.execute(update(MemoryDreamRunModel).values(model_calls=7))
        operation = await dreams.create_operation(
            cluster_id=cluster.id,
            action_index=0,
            operation_type=DreamOperationType.KEEP,
            source_facts=rows,
            anchor_fact_id=None,
            session=writer,
        )
        operation_id = operation.id
    aggregate_reads = 0
    acquiring = asyncio.Event()

    def capture(connection, _cursor, statement, *_args):
        nonlocal aggregate_reads
        sql = statement.lstrip().upper()
        if sql.startswith("BEGIN IMMEDIATE"):
            connection.info["test_writer_held"] = True
            acquiring.set()
        if "GROUP BY MEMORY_DREAM_OPERATIONS.CLUSTER_ID" in sql:
            aggregate_reads += 1
            assert connection.info.get("test_writer_held")

    def released(connection):
        connection.info.pop("test_writer_held", None)

    engine = database.engine.sync_engine
    event.listen(engine, "before_cursor_execute", capture)
    event.listen(engine, "commit", released)
    event.listen(engine, "rollback", released)
    try:
        async with database.immediate_session() as writer:
            acquiring.clear()
            recovery = asyncio.create_task(dreams.reset_processing_after_restart())
            await asyncio.wait_for(acquiring.wait(), timeout=2)
            assert not recovery.done()
            await writer.execute(
                update(MemoryDreamOperationModel)
                .where(MemoryDreamOperationModel.id == operation_id)
                .values(status="committed")
            )
        assert await recovery == 1
    finally:
        event.remove(engine, "before_cursor_execute", capture)
        event.remove(engine, "commit", released)
        event.remove(engine, "rollback", released)
    assert aggregate_reads == 1
    current = await dreams.get_run(run.public_id)
    assert current.completed_clusters == 1 and current.model_calls == 7
    async with database.sessions() as reader:
        cluster = await reader.scalar(select(MemoryDreamClusterModel))
        assert cluster.status == "completed" and cluster.operation_count == 1
        assert cluster.attempts == 2 and cluster.model_calls == 2
        assert await reader.scalar(select(func.count()).select_from(MemoryDreamOperationModel)) == 1
        assert (await reader.get(MemoryDreamOperationModel, operation_id)).status == "committed"
    assert await dreams.reset_processing_after_restart() == 0


async def test_dream_run_cluster_and_preview_json_are_prepared_before_first_write(
    database, monkeypatch
):
    dreams, rows = await sources(database)
    held = False
    encodings = []
    dump_statistics = DreamPlanStatistics.model_dump_json
    dump_proposal = DreamOutput.model_dump_json

    def encode(*args, **kwargs):
        assert not held
        encodings.append("cluster")
        return json.dumps(*args, **kwargs)

    def statistics(value, *args, **kwargs):
        assert not held
        encodings.append("statistics")
        return dump_statistics(value, *args, **kwargs)

    def proposal(value, *args, **kwargs):
        assert not held
        encodings.append("proposal")
        return dump_proposal(value, *args, **kwargs)

    def capture(_connection, _cursor, statement, *_args):
        nonlocal held
        if statement.lstrip().upper().startswith(("INSERT", "UPDATE", "BEGIN IMMEDIATE")):
            held = True

    def released(_connection):
        nonlocal held
        held = False

    monkeypatch.setattr(dream_repository, "json", SimpleNamespace(dumps=encode, loads=json.loads))
    monkeypatch.setattr(DreamPlanStatistics, "model_dump_json", statistics)
    monkeypatch.setattr(DreamOutput, "model_dump_json", proposal)
    engine = database.engine.sync_engine
    event.listen(engine, "before_cursor_execute", capture)
    event.listen(engine, "commit", released)
    event.listen(engine, "rollback", released)
    try:
        await create(dreams, specs(rows))
        async with database.sessions() as reader:
            identity = await reader.scalar(select(MemoryDreamClusterModel.id))
        await dreams.save_preview(
            cluster_id=identity,
            source_fingerprint="verified",
            proposal=DreamOutput(),
            model_calls=1,
            source_characters=10,
            output_characters=0,
        )
        await dreams.save_preview(
            cluster_id=identity,
            source_fingerprint="verified",
            proposal=DreamOutput(),
            model_calls=2,
            source_characters=10,
            output_characters=0,
        )
    finally:
        event.remove(engine, "before_cursor_execute", capture)
        event.remove(engine, "commit", released)
        event.remove(engine, "rollback", released)
    assert encodings == ["statistics", "cluster", "proposal", "proposal"]
