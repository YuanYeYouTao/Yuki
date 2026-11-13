"""QQ official Bot message facts: REFIDX_, passive msg_id/msg_seq and inbound anchors."""

from alembic import op

revision = "0157"
down_revision = "0156"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE qq_official_messages (
        presence_id VARCHAR(36) NOT NULL
            REFERENCES account_assignments(id) ON DELETE RESTRICT,
        message_id VARCHAR(255) NOT NULL,
        chat_kind VARCHAR(8) NOT NULL,
        chat_id VARCHAR(255) NOT NULL,
        direction VARCHAR(8) NOT NULL,
        ref_idx VARCHAR(255),
        reply_to VARCHAR(255),
        msg_seq INTEGER,
        created_at DATETIME NOT NULL,
        PRIMARY KEY (presence_id, message_id)
    )""")
    op.execute(
        "CREATE INDEX ix_qq_official_messages_ref ON qq_official_messages"
        "(presence_id, ref_idx) WHERE ref_idx IS NOT NULL"
    )
    op.execute(
        "CREATE INDEX ix_qq_official_messages_reply ON qq_official_messages"
        "(presence_id, reply_to, msg_seq) WHERE reply_to IS NOT NULL"
    )
    op.execute(
        "CREATE INDEX ix_qq_official_messages_inbound ON qq_official_messages"
        "(presence_id, chat_kind, chat_id, created_at) WHERE direction = 'inbound'"
    )


def downgrade() -> None:
    op.execute("DROP TABLE qq_official_messages")
