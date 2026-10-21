"""Compare read work on the same SQLite engine, without wall-clock assertions."""

import sqlite3
from contextlib import closing, contextmanager

from sqlalchemy import event
from sqlalchemy.engine import make_url


@contextmanager
def capture_reads(database):
    statements = []

    def capture(_connection, _cursor, sql, parameters, _context, _many):
        if sql.lstrip().upper().startswith(("SELECT", "WITH")):
            statements.append((sql, parameters))

    event.listen(database.engine.sync_engine, "before_cursor_execute", capture)
    try:
        yield statements
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", capture)


def read_vm_steps(database, statements):
    """Replay captured reads against unchanged fixtures, explicitly read-only."""
    path = make_url(database.url).database
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as reader:
        reader.execute("SELECT name FROM sqlite_master").fetchall()
        steps = 0

        def tick():
            nonlocal steps
            steps += 1
            return 0

        reader.set_progress_handler(tick, 1)
        for sql, parameters in statements:
            reader.execute(sql, parameters).fetchall()
        reader.set_progress_handler(None, 0)
    return steps
