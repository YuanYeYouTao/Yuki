"""Persist ingress-resolved internal reply references without reconstructing history.

Revision ID: 0068
Revises: 0067
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0068"
down_revision: str | None = "0067"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "reply_to_event_id" not in {
        column["name"] for column in inspector.get_columns("chat_events")
    }:
        op.execute(
            "ALTER TABLE chat_events ADD COLUMN reply_to_event_id INTEGER "
            "REFERENCES chat_events(id) ON UPDATE RESTRICT ON DELETE RESTRICT"
        )
    if "ix_chat_events_reply_to_event_id" not in {
        index["name"] for index in inspector.get_indexes("chat_events")
    }:
        op.create_index("ix_chat_events_reply_to_event_id", "chat_events", ["reply_to_event_id"])


def downgrade() -> None:
    # Code rollback retains real ingress provenance. Recreating the earlier code
    # must not discard references written while this version was active.
    pass
