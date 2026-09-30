"""Writer attribution covers real commit/rollback tails without affecting transactions."""

import json
import sqlite3

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import OperationalError

from qq_ai_bot.persistence.sqlite_diagnostics import install_sqlite_diagnostics


def holders(caplog):
    return [
        json.loads(record.getMessage().split("holders=", 1)[1])
        for record in caplog.records
        if record.getMessage().startswith("sqlite_write_contended holders=")
    ]


@pytest.mark.parametrize("finish", ["commit", "rollback"])
def test_real_transaction_tail_retains_holder_and_completion_clears_it(tmp_path, caplog, finish):
    engine = create_engine(f"sqlite:///{tmp_path / 'tail.db'}", connect_args={"timeout": 0})
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE diagnostics (id INTEGER)"))
    original = getattr(engine.dialect, f"do_{finish}")
    probe_tail = True

    def blocked_tail(connection):
        nonlocal probe_tail
        if probe_tail:
            probe_tail = False
            with engine.connect() as contender, pytest.raises(OperationalError):
                contender.execute(text("INSERT INTO diagnostics VALUES (2)"))
        original(connection)

    setattr(engine.dialect, f"do_{finish}", blocked_tail)
    install_sqlite_diagnostics(engine)
    try:
        with engine.connect() as first:
            first.execute(text("INSERT INTO diagnostics VALUES (1)"))
            getattr(first, finish)()
            during_tail = holders(caplog)[-1]
            assert [row["operation"] for row in during_tail] == ["INSERT INTO diagnostics"]
            assert during_tail[0]["held_seconds"] >= 0
            # Keep the original connection checked out. A later contention
            # must attribute only the NEW writer, not await a pool checkin.
            with engine.connect() as second:
                second.execute(text("UPDATE diagnostics SET id = 3"))
                with engine.connect() as third, pytest.raises(OperationalError):
                    third.execute(text("DELETE FROM diagnostics"))
                assert [row["operation"] for row in holders(caplog)[-1]] == ["UPDATE diagnostics"]
                # Avoid invoking the injected rollback tail recursively.
                original(second.connection)
                second.commit()
    finally:
        engine.dispose()


def test_failed_commit_keeps_holder_until_invalidation_and_never_reconnects_info(tmp_path, caplog):
    engine = create_engine(f"sqlite:///{tmp_path / 'failure.db'}", connect_args={"timeout": 0})
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE diagnostics (id INTEGER)"))
    original = engine.dialect.do_commit
    fail_commit = True

    def failing_commit(connection):
        if fail_commit:
            raise sqlite3.OperationalError("synthetic commit failure")
        original(connection)

    engine.dialect.do_commit = failing_commit
    install_sqlite_diagnostics(engine)
    reconnects = []
    event.listen(engine.pool, "connect", lambda *_args: reconnects.append(True))
    try:
        with engine.connect() as first:
            first.execute(text("INSERT INTO diagnostics VALUES (1)"))
            with pytest.raises(OperationalError, match="synthetic commit failure"):
                first.commit()
            with engine.connect() as second, pytest.raises(OperationalError):
                second.execute(text("DELETE FROM diagnostics"))
            assert [row["operation"] for row in holders(caplog)[-1]] == ["INSERT INTO diagnostics"]
            first.invalidate()
            connections_before_rollback = len(reconnects)
            first.rollback()
            assert len(reconnects) == connections_before_rollback
            fail_commit = False
            with engine.connect() as second:
                second.execute(text("UPDATE diagnostics SET id = 3"))
                with engine.connect() as third, pytest.raises(OperationalError):
                    third.execute(text("DELETE FROM diagnostics"))
                assert [row["operation"] for row in holders(caplog)[-1]] == ["UPDATE diagnostics"]
                second.rollback()
    finally:
        engine.dispose()
