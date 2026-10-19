"""Normalize observed names once and index exact targets and real message activity.

The name rule is frozen here; accepted event and effect snapshots are untouched.
The caller owns the offline SQLite transaction (migrations/env.py).
"""

import unicodedata
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine import Connection

revision = "0142"
down_revision = "0141"
branch_labels = None
depends_on = None


def _normalized_name(value: str) -> str:
    visible = "".join(
        " " if character.isspace() else character
        for character in value
        if not unicodedata.category(character).startswith("C")
    )
    return " ".join(visible.split())[:128].rstrip()


def _instant(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _normalize_observations(connection: Connection) -> None:
    rows = (
        connection.execute(sa.text("SELECT * FROM account_name_observations ORDER BY id"))
        .mappings()
        .all()
    )
    keep: dict[tuple[str, str | None, str | None, str], dict[str, Any]] = {}
    removed: list[dict[str, object]] = []
    for row in rows:
        name = _normalized_name(row["name"])
        if not name:
            removed.append({"id": row["id"]})
            continue
        key = row["account_id"], row["assignment_id"], row["space_binding_id"], name
        existing = keep.get(key)
        if existing is None:
            keep[key] = {
                "id": row["id"],
                "name": name,
                "first": row["first_seen_at"],
                "last": row["last_seen_at"],
            }
        else:
            existing["first"] = min(existing["first"], row["first_seen_at"], key=_instant)
            existing["last"] = max(existing["last"], row["last_seen_at"], key=_instant)
            removed.append({"id": row["id"]})
    # Delete normalized duplicates first, while retaining the original unique
    # owner/stage/scope/name index throughout the offline transaction.
    if removed:
        connection.execute(sa.text("DELETE FROM account_name_observations WHERE id=:id"), removed)
    if keep:
        connection.execute(
            sa.text(
                "UPDATE account_name_observations SET name=:name,first_seen_at=:first,"
                "last_seen_at=:last WHERE id=:id"
            ),
            list(keep.values()),
        )


def upgrade() -> None:
    _normalize_observations(op.get_bind())
    op.create_index(
        "ix_account_names_name_scope_account",
        "account_name_observations",
        ["name", "space_binding_id", "account_id"],
    )
    op.drop_index("ix_chat_events_conversation_sender_time", table_name="chat_events")
    op.create_index(
        "ix_chat_events_conversation_message_sender_time",
        "chat_events",
        ["canonical_conversation_id", "sender_user_id", "occurred_at", "id"],
        sqlite_where=sa.text("suppression_status = 'keeper' AND event_kind = 'message'"),
    )


def downgrade() -> None:
    raise RuntimeError("0142 irreversibly normalizes observed account names")
