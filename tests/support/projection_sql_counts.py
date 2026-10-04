"""Reusable deterministic SQL/UTF-8 counters; wall-clock samples are diagnostic only."""

from contextlib import contextmanager
from math import ceil

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


def parameter_bytes(parameters):
    """Count actual strings/binary values bound to SQL, including batch rows."""
    if isinstance(parameters, str):
        return len(parameters.encode("utf-8"))
    if isinstance(parameters, bytes):
        return len(parameters)
    if isinstance(parameters, dict):
        return sum(parameter_bytes(value) for value in parameters.values())
    if isinstance(parameters, (tuple, list)):
        return sum(parameter_bytes(value) for value in parameters)
    return 0


def timing_summary(samples):
    ordered = sorted(samples)
    return {
        "samples": len(ordered),
        "p50_ms": ordered[ceil(len(ordered) * 0.50) - 1] * 1000,
        "p95_ms": ordered[ceil(len(ordered) * 0.95) - 1] * 1000,
        "max_ms": ordered[-1] * 1000,
    }
