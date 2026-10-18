"""Disposable diagnostics release each writer and never replay uncertain commits."""

import asyncio
import gc
import weakref

from sqlalchemy import Column, Integer, MetaData, String, Table, event, insert, literal, select

from yuki.execution_trace.db_models import ExecutionTraceStateModel
from yuki.persistence.diagnostic_writer import DiagnosticWrite, DiagnosticWriter


def statement(identifier, *, accepted=True):
    return insert(ExecutionTraceStateModel).from_select(
        ["id", "privacy_generation"],
        select(literal(identifier), literal(0)).where(literal(accepted)),
    )


async def test_each_record_is_prepared_before_its_short_commit(database):
    writer = DiagnosticWriter()
    prepared = []
    commits = []

    async def prepare(identifier):
        # This await may run an encoder; no SQL writer has been entered.
        await asyncio.sleep(0)
        prepared.append(identifier)
        return DiagnosticWrite(database, statement(identifier))

    def before_sql(_conn, _cursor, sql, _parameters, _context, _many):
        if sql.startswith("INSERT"):
            assert len(prepared) == writer.committed + 1

    def committing(_conn):
        commits.append(True)
        assert len(commits) == writer.committed + 1

    event.listen(database.engine.sync_engine, "before_cursor_execute", before_sql)
    event.listen(database.engine.sync_engine, "commit", committing)
    await writer.start()
    for identifier in range(1, 7):
        assert writer.submit("probe", 32, lambda identifier=identifier: prepare(identifier))
    await writer.drain()
    assert writer.committed == 6
    assert writer.failures == 0
    assert len(commits) == 6
    assert (await writer.health())["bytes"] == 0
    await writer.close()
    event.remove(database.engine.sync_engine, "before_cursor_execute", before_sql)
    event.remove(database.engine.sync_engine, "commit", committing)
    async with database.sessions() as session:
        assert (await session.scalars(select(ExecutionTraceStateModel.id))).all() == list(
            range(1, 7)
        )


async def test_zero_row_guard_marks_only_its_record_after_commit(database, caplog):
    writer = DiagnosticWriter()
    failed = []

    async def prepare(identifier):
        return DiagnosticWrite(
            database,
            statement(identifier, accepted=identifier != 2),
            lambda: failed.append(identifier),
        )

    await writer.start()
    for identifier in range(1, 4):
        writer.submit("source_guard", 32, lambda identifier=identifier: prepare(identifier))
    await writer.drain()
    await writer.close()
    assert failed == [2]
    assert writer.committed == 2
    assert writer.failures == 1
    assert "diagnostic_discarded kind=source_guard coverage_incomplete=true" in caplog.text
    async with database.sessions() as session:
        assert (await session.scalars(select(ExecutionTraceStateModel.id))).all() == [1, 3]


async def test_unknown_commit_does_not_replay_or_claim_success(database, monkeypatch):
    writer = DiagnosticWriter()
    failed = []
    calls = []
    original_commit = database.engine.sync_engine.dialect.do_commit

    def acknowledged_lost(connection):
        calls.append(True)
        original_commit(connection)
        raise RuntimeError("synthetic_commit_ack_lost")

    async def prepare(identifier):
        return DiagnosticWrite(database, statement(identifier), lambda: failed.append(identifier))

    monkeypatch.setattr(database.engine.sync_engine.dialect, "do_commit", acknowledged_lost)
    await writer.start()
    for identifier in (1, 2):
        writer.submit("uncertain", 32, lambda identifier=identifier: prepare(identifier))
    await writer.drain()
    await writer.close()
    assert len(calls) == 2
    assert failed == [1, 2]
    assert writer.failures == 2
    assert writer.committed == 0
    monkeypatch.setattr(database.engine.sync_engine.dialect, "do_commit", original_commit)
    async with database.sessions() as session:
        assert (await session.scalars(select(ExecutionTraceStateModel.id))).all() == [1, 2]


async def test_statement_failure_does_not_drop_other_records_or_replay(database):
    writer = DiagnosticWriter()
    calls = []
    failed = []

    async def prepare(index, identifier):
        calls.append(index)
        return DiagnosticWrite(database, statement(identifier), lambda: failed.append(index))

    await writer.start()
    for index, identifier in enumerate((1, 1, 2)):
        writer.submit("constraint", 32, lambda i=index, n=identifier: prepare(i, n))
    await writer.drain()
    assert calls == [0, 1, 2]
    assert failed == [1]
    assert writer.failures == 1
    assert writer.committed == 2
    async with database.sessions() as session:
        assert (await session.scalars(select(ExecutionTraceStateModel.id))).all() == [1, 2]
    writer.submit("next", 32, lambda: prepare(3, 3))
    await writer.close()
    assert writer.committed == 3


async def test_shutdown_cancels_preparation_without_starting_a_writer(database):
    writer = DiagnosticWriter(max_bytes=100)
    entered = asyncio.Event()
    hold = asyncio.Event()

    async def prepare():
        entered.set()
        await hold.wait()
        return DiagnosticWrite(database, statement(1))

    await writer.start()
    assert writer.submit("cancel", 40, prepare)
    assert writer.submit("unprepared", 40, prepare)
    assert not writer.submit("over_capacity", 40, prepare)
    await entered.wait()
    assert (await writer.health())["bytes"] == 80
    await writer.close(drain_seconds=0)
    assert writer.dropped == 3
    assert writer.committed == 0
    assert (await writer.health())["bytes"] == 0
    async with database.sessions() as session:
        assert (await session.scalars(select(ExecutionTraceStateModel.id))).all() == []


async def test_idle_consumer_releases_preparation_and_statement_owners(database):
    class Owner:
        identifier = 1

    owner = Owner()
    reference = weakref.ref(owner)
    writer = DiagnosticWriter()

    async def prepare(captured=owner):
        return DiagnosticWrite(database, statement(captured.identifier))

    await writer.start()
    writer.submit("release", 32, prepare)
    await writer.drain()
    del owner, prepare
    gc.collect()
    assert reference() is None
    await writer.close()


async def test_compiled_statement_cache_does_not_retain_a_completed_payload(database):
    class Payload(str):
        pass

    payload = Payload("original-payload")
    reference = weakref.ref(payload)
    writer = DiagnosticWriter()
    table = Table(
        "diagnostic_payload_owner",
        MetaData(),
        Column("id", Integer, primary_key=True),
        Column("payload", String),
    )
    async with database.engine.begin() as connection:
        await connection.run_sync(table.create)

    async def prepare(captured=payload):
        return DiagnosticWrite(
            database,
            insert(table).values(id=1, payload=literal(captured)),
        )

    await writer.start()
    writer.submit("payload_owner", 32, prepare)
    await writer.drain()
    del payload, prepare
    gc.collect()
    assert reference() is None
    await writer.close()
