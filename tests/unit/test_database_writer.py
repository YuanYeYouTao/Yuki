"""File WAL, actual worker barriers, original outcomes, and reader concurrency."""

import asyncio
import sqlite3
import threading
from contextvars import ContextVar
from types import SimpleNamespace

import aiosqlite
import pytest
from sqlalchemy import event, func, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from yuki.persistence.database import (
    Database,
    write_failure_phase,
    write_failure_retryable,
    write_failure_uncertain,
)


class ProbeBase(DeclarativeBase):
    pass


class ProbeRow(ProbeBase):
    __tablename__ = "writer_probe"
    id: Mapped[int] = mapped_column(primary_key=True)


@pytest.fixture
async def writer_db(tmp_path):
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'writer.db'}")
    async with database.engine.begin() as connection:
        await connection.run_sync(ProbeBase.metadata.create_all)
    try:
        yield database
    finally:
        await database.close()


async def count_rows(database):
    async with database.read_session() as session:
        return await session.scalar(select(func.count()).select_from(ProbeRow))


async def test_fifo_waits_before_checkout_while_readers_continue(writer_db):
    entered, release = asyncio.Event(), asyncio.Event()
    checked_out = []
    order = []
    identity = ContextVar("writer-test-identity", default="")

    def checkout(*args):
        checked_out.append(True)

    event.listen(writer_db.engine.sync_engine.pool, "checkout", checkout)

    async def owner():
        async with writer_db.immediate_session() as session:
            session.add(ProbeRow(id=1))
            await session.flush()
            entered.set()
            await release.wait()

    async def contender(number, waiting):
        identity.set(str(number))
        caller = asyncio.current_task()
        waiting.set()
        async with writer_db.immediate_session() as session:
            assert asyncio.current_task() is caller
            assert identity.get() == str(number)
            order.append(number)
            session.add(ProbeRow(id=number))

    first = asyncio.create_task(owner())
    await entered.wait()
    baseline = len(checked_out)
    waiters = []
    for number in (2, 3, 4):
        waiting = asyncio.Event()
        waiters.append(asyncio.create_task(contender(number, waiting)))
        await waiting.wait()
    assert len(checked_out) == baseline  # Queued writers have no connection.
    assert await count_rows(writer_db) == 0  # A parallel reader sees committed facts.
    waiters[1].cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiters[1]
    release.set()
    await first
    await asyncio.gather(waiters[0], waiters[2])
    assert order == [2, 4]
    assert await count_rows(writer_db) == 3


async def test_owned_reader_does_not_flush_or_commit_and_writer_keeps_autoflush(writer_db):
    async with writer_db.read_session() as reader:
        reader.add(ProbeRow(id=1))
        assert await reader.scalar(select(func.count()).select_from(ProbeRow)) == 0
    assert await count_rows(writer_db) == 0
    async with writer_db.immediate_session() as writer:
        writer.add(ProbeRow(id=1))
        assert await writer.scalar(select(func.count()).select_from(ProbeRow)) == 1
    assert await count_rows(writer_db) == 1


async def test_hints_belong_to_confirmed_root_commit_not_savepoint_or_reader(writer_db):
    hints = writer_db.commit_hints.subscribe()
    async with writer_db.read_session() as reader:
        await reader.execute(text("SELECT 1"))
    assert hints.empty()
    with pytest.raises(ValueError):
        async with writer_db.immediate_session() as writer:
            async with writer.begin_nested():
                writer.add(ProbeRow(id=1))
            assert hints.empty()
            raise ValueError("roll back root")
    assert hints.empty()
    assert await count_rows(writer_db) == 0
    async with writer_db.immediate_session() as writer:
        writer.add(ProbeRow(id=2))
    assert hints.get_nowait() is True
    assert hints.empty()


@pytest.mark.parametrize("phase", ["begin", "execute", "autoflush", "flush", "commit"])
async def test_cancel_waits_for_actual_driver_exit_and_keeps_commit_fact(
    writer_db, phase, monkeypatch
):
    entered, release = threading.Event(), threading.Event()
    attempted, next_entered = asyncio.Event(), asyncio.Event()
    hints = writer_db.commit_hints.subscribe()

    def pause():
        entered.set()
        release.wait()
        return 0

    if phase == "begin":
        async with writer_db.read_session() as reader:
            connection = await reader.connection()
            driver = (await connection.get_raw_connection()).driver_connection
            await driver.set_trace_callback(
                lambda sql: pause() if sql == "BEGIN IMMEDIATE" else None
            )

    async def writer():
        async with writer_db.immediate_session() as session:
            connection = await session.connection()
            driver = (await connection.get_raw_connection()).driver_connection
            if phase == "commit":
                await driver.set_trace_callback(lambda sql: pause() if sql == "COMMIT" else None)
                session.add(ProbeRow(id=1))
            else:
                await driver.create_function("pause_writer", 0, pause)
                await session.execute(
                    text(
                        "CREATE TEMP TRIGGER pause_insert BEFORE INSERT ON writer_probe "
                        "BEGIN SELECT pause_writer(); END"
                    )
                )
                if phase in {"flush", "autoflush"}:
                    session.add(ProbeRow(id=1))
                    if phase == "autoflush":
                        await session.scalar(select(func.count()).select_from(ProbeRow))
                else:
                    await session.execute(text("INSERT INTO writer_probe VALUES(1)"))

    async def next_writer():
        attempted.set()
        async with writer_db.immediate_session() as session:
            next_entered.set()
            session.add(ProbeRow(id=2))

    original = asyncio.create_task(writer())
    successor = None
    try:
        await asyncio.wait_for(asyncio.to_thread(entered.wait), 5)
        original.cancel()
        successor = asyncio.create_task(next_writer())
        await attempted.wait()
        original.cancel()  # Repeated cancellation cannot abandon physical cleanup.
        assert not next_entered.is_set()
        assert not original.done()
        assert hints.empty()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError) as cancelled:
        await original
    await asyncio.wait_for(successor, 5)
    assert next_entered.is_set()
    assert await count_rows(writer_db) == (2 if phase == "commit" else 1)
    if phase == "commit":
        assert "database_commit:confirmed" in cancelled.value.__notes__
        assert hints.qsize() == 2
    else:
        assert hints.qsize() == 1
    assert writer_db.writer_available


async def test_begin_busy_preserves_driver_code_and_confirmed_noncommit(writer_db, tmp_path):
    async with writer_db.read_session() as session:
        await session.execute(text("PRAGMA busy_timeout=0"))
    external = sqlite3.connect(tmp_path / "writer.db", timeout=0)
    try:
        external.execute("BEGIN IMMEDIATE")
        with pytest.raises(OperationalError) as failed:
            async with writer_db.immediate_session():
                pytest.fail("BEGIN did not acquire the external writer")
        assert failed.value.orig.sqlite_errorcode == sqlite3.SQLITE_BUSY
        assert write_failure_phase(failed.value) == "begin"
        assert not write_failure_uncertain(failed.value)
        assert write_failure_retryable(failed.value)
    finally:
        external.close()
    async with writer_db.immediate_session() as session:
        session.add(ProbeRow(id=1))
    assert await count_rows(writer_db) == 1


async def test_commit_ack_loss_remains_unknown_even_if_busy_shaped(writer_db, monkeypatch):
    commit = AsyncSession.commit

    async def lost_ack(session):
        await commit(session)
        error = sqlite3.OperationalError("lost commit confirmation")
        error.sqlite_errorcode = sqlite3.SQLITE_BUSY
        raise OperationalError("COMMIT", None, error)

    with monkeypatch.context() as fault:
        fault.setattr(AsyncSession, "commit", lost_ack)
        with pytest.raises(OperationalError) as failed:
            async with writer_db.immediate_session() as session:
                session.add(ProbeRow(id=1))
    assert write_failure_phase(failed.value) == "commit"
    assert write_failure_uncertain(failed.value)
    assert not write_failure_retryable(failed.value)
    assert await count_rows(writer_db) == 1  # Original fact, never replayed.


async def test_normal_writes_reuse_connection_and_different_files_do_not_queue(writer_db, tmp_path):
    connected = []
    event.listen(writer_db.engine.sync_engine.pool, "connect", lambda *args: connected.append(True))
    other = Database(f"sqlite+aiosqlite:///{tmp_path / 'other.db'}")
    try:
        async with writer_db.immediate_session() as session:
            session.add(ProbeRow(id=1))
            async with other.immediate_session() as independent:
                await independent.execute(text("CREATE TABLE other_fact(id INTEGER)"))
        async with writer_db.immediate_session() as session:
            session.add(ProbeRow(id=2))
        assert not connected  # Fixture connection remains pooled through both writes.
    finally:
        await other.close()


@pytest.fixture
async def physical_db(tmp_path, monkeypatch):
    control = SimpleNamespace(
        block=False, fail=False, entered=threading.Event(), release=threading.Event()
    )
    connect = sqlite3.connect

    class PhysicalConnection(sqlite3.Connection):
        def close(self):
            if control.block:
                control.entered.set()
                assert control.release.wait(5), "physical close was not released"
            if control.fail:
                raise sqlite3.OperationalError("physical close failed")
            return super().close()

    def physical_connect(*args, **kwargs):
        return connect(*args, **kwargs, factory=PhysicalConnection)

    monkeypatch.setattr(sqlite3, "connect", physical_connect)
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'physical.db'}")
    async with database.engine.begin() as connection:
        await connection.run_sync(ProbeBase.metadata.create_all)
    async with database.read_session() as reader:
        raw = await (await reader.connection()).get_raw_connection()
        driver = raw.driver_connection
    try:
        yield database, control
    finally:
        control.block = control.fail = False
        control.release.set()
        if (
            driver.physical_exit is not None
            and driver.physical_exit.done()
            and driver.physical_exit.exception() is not None
        ):
            # Fault-injection cleanup only: the product owner remains stopped.
            await aiosqlite.Connection.stop(driver)
        await database.close()


async def test_failed_rollback_keeps_owner_until_physical_close_even_under_repeated_cancel(
    physical_db,
    monkeypatch,
):
    database, control = physical_db
    attempted = asyncio.Event()
    next_entered = asyncio.Event()
    control.block = True

    async def failed_rollback(_session):
        raise RuntimeError("rollback transport failed")

    async def original():
        async with database.immediate_session() as writer:
            writer.add(ProbeRow(id=1))
            await writer.flush()
            raise ValueError("original business failure")

    async def successor():
        attempted.set()
        async with database.immediate_session() as writer:
            next_entered.set()
            writer.add(ProbeRow(id=2))

    with monkeypatch.context() as fault:
        fault.setattr(AsyncSession, "rollback", failed_rollback)
        owner = asyncio.create_task(original())
        await asyncio.wait_for(asyncio.to_thread(control.entered.wait), 5)
        next_owner = asyncio.create_task(successor())
        await attempted.wait()
        owner.cancel()
        owner.cancel()
        assert not owner.done() and not next_entered.is_set()
        control.release.set()
        with pytest.raises(asyncio.CancelledError):
            await owner
        await asyncio.wait_for(next_owner, 5)
    assert database.writer_available
    assert await count_rows(database) == 1


async def test_unconfirmed_physical_close_stops_admission_without_masking_original(
    physical_db, monkeypatch
):
    database, control = physical_db
    control.fail = True

    async def failed_rollback(_session):
        raise RuntimeError("rollback transport failed")

    with monkeypatch.context() as fault:
        fault.setattr(AsyncSession, "rollback", failed_rollback)
        with pytest.raises(ValueError, match="original business failure") as failed:
            async with database.immediate_session() as writer:
                writer.add(ProbeRow(id=1))
                await writer.flush()
                raise ValueError("original business failure")
    assert write_failure_uncertain(failed.value)
    assert not database.writer_available
    with pytest.raises(RuntimeError, match="database_writer_unavailable"):
        async with database.immediate_session():
            pytest.fail("unconfirmed physical exit admitted a successor")


async def test_shutdown_waits_for_actual_pool_close_under_repeated_cancel(physical_db):
    database, control = physical_db
    control.block = True
    closing = asyncio.create_task(database.close())
    try:
        await asyncio.wait_for(asyncio.to_thread(control.entered.wait), 5)
        closing.cancel()
        closing.cancel()
        assert not closing.done() and not database.writer_available
    finally:
        control.release.set()
    with pytest.raises(asyncio.CancelledError):
        await closing
    with pytest.raises(RuntimeError, match="database_writer_unavailable"):
        async with database.immediate_session():
            pytest.fail("closed resource admitted a writer")
