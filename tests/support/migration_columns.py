"""Compare original columns through known ledger identity renames at current head."""

import sqlite3
from collections.abc import Sequence


def preserved_projection(connection: sqlite3.Connection, table: str, columns: Sequence[str]) -> str:
    current = {row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')}
    renamed = {
        "chat_events": {"platform_message_id": "origin_key"},
        "canonical_event_receipts": {"platform_message_id": "ingress_key"},
    }.get(table, {})
    projected = []
    for original in columns:
        actual = original if original in current else renamed.get(original, original)
        # SQLite can interpret a missing double-quoted column as a string literal.
        # A comparison must read a real column, including on intermediate heads.
        assert actual in current, (table, original, actual)
        projected.append(f'"{actual}" AS "{original}"')
    return ",".join(projected)
