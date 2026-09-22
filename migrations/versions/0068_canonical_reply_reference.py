"""Persist ingress-resolved internal reply references without reconstructing history.

Revision ID: 0068
Revises: 0067
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0068"
down_revision: str | None = "0067"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE chat_events ADD COLUMN reply_to_event_id INTEGER "
        "REFERENCES chat_events(id) ON UPDATE RESTRICT ON DELETE RESTRICT"
    )
    op.create_index("ix_chat_events_reply_to_event_id", "chat_events", ["reply_to_event_id"])


def downgrade() -> None:
    # Code rollback retains real ingress provenance. Recreating the earlier code
    # must not discard references written while this version was active.
    pass
