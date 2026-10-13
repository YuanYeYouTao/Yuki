"""Reusable SQL capture for queries and writer boundaries."""

from contextlib import contextmanager

from sqlalchemy import event


@contextmanager
def capture_sql(database):
    statements = []

    def capture(_connection, _cursor, sql, parameters, *_args):
        statements.append((sql, parameters))

    event.listen(database.engine.sync_engine, "before_cursor_execute", capture)
    try:
        yield statements
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", capture)
