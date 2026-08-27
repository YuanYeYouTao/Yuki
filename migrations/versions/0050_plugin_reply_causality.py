"""Record durable causality for proactive plugin replies.

Revision ID: 0050
Revises: 0049
Create Date: 2026-08-28
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0050"
down_revision: str | None = "0049"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # SQLite accepts an inline REFERENCES clause on ADD COLUMN but cannot add
    # the generated FK as a second ALTER statement.
    op.execute(
        "ALTER TABLE chat_events ADD COLUMN caused_by_event_id INTEGER "
        "REFERENCES chat_events(id) ON UPDATE RESTRICT ON DELETE RESTRICT"
    )
    op.create_index(
        "ix_chat_events_caused_by_event_id",
        "chat_events",
        ["caused_by_event_id"],
        unique=False,
    )

    # Historical rows are linked only when the durable outbox proves one unique
    # source event in the same canonical conversation. Ambiguous rows remain NULL.
    op.execute(
        """
        UPDATE chat_events AS reply
        SET caused_by_event_id = (
            SELECT MIN(outbox.source_event_id)
            FROM plugin_notification_outbox AS outbox
            JOIN chat_events AS source ON source.id = outbox.source_event_id
            WHERE outbox.platform_message_id = reply.platform_message_id
              AND outbox.canonical_conversation_id = reply.canonical_conversation_id
              AND source.canonical_conversation_id = reply.canonical_conversation_id
              AND source.id < reply.id
              AND source.event_kind = 'external_event'
              AND source.direction = 'external'
              AND source.origin = 'plugin_background'
              AND source.suppression_status = 'keeper'
            GROUP BY outbox.platform_message_id, outbox.canonical_conversation_id
            HAVING COUNT(DISTINCT outbox.source_event_id) = 1
        )
        WHERE reply.event_kind = 'message'
          AND reply.direction = 'outbound'
          AND reply.origin = 'plugin_background'
          AND EXISTS (
            SELECT 1
            FROM plugin_notification_outbox AS candidate
            WHERE candidate.platform_message_id = reply.platform_message_id
              AND candidate.canonical_conversation_id = reply.canonical_conversation_id
          )
        """
    )


def downgrade() -> None:
    op.drop_index("ix_chat_events_caused_by_event_id", table_name="chat_events")
    op.drop_column("chat_events", "caused_by_event_id")
