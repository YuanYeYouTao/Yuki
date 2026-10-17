"""Separate ACP Conversations and their request scopes without rewriting original receipts."""

from datetime import UTC, datetime
from uuid import uuid4

import sqlalchemy as sa
from alembic import op

revision = "0132"
down_revision = "0131"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    connection.exec_driver_sql(
        "ALTER TABLE control_command_receipts ADD COLUMN request_conversation_id VARCHAR(36) "
        "REFERENCES canonical_conversations(id) ON DELETE RESTRICT"
    )
    op.drop_index("uq_canonical_conversations_binding", table_name="canonical_conversations")
    connection.exec_driver_sql(
        "CREATE UNIQUE INDEX uq_canonical_conversations_binding "
        "ON canonical_conversations(identity_binding_id,entry_presence_id,coalesce(thread_id,'')) "
        "WHERE kind='private'"
    )
    op.create_index(
        "uq_identity_bindings_acp_person",
        "identity_bindings",
        ["person_id"],
        unique=True,
        sqlite_where=sa.text("platform='acp' AND status <> 'transferred'"),
    )
    for name in (
        "uq_control_command_receipts_principal_request",
        "uq_control_command_receipts_person_request",
    ):
        op.drop_index(name, table_name="control_command_receipts")
    op.create_index(
        "uq_control_command_receipts_principal_request",
        "control_command_receipts",
        ["principal_id", "request_id"],
        unique=True,
        sqlite_where=sa.text("request_person_id IS NULL AND request_conversation_id IS NULL"),
    )
    op.create_index(
        "uq_control_command_receipts_person_request",
        "control_command_receipts",
        ["request_person_id", "request_id"],
        unique=True,
        sqlite_where=sa.text("request_person_id IS NOT NULL AND request_conversation_id IS NULL"),
    )
    op.create_index(
        "uq_control_command_receipts_conversation_request",
        "control_command_receipts",
        ["request_conversation_id", "request_id"],
        unique=True,
        sqlite_where=sa.text("request_conversation_id IS NOT NULL"),
    )
    connection.execute(
        sa.text(
            "INSERT INTO presences(id,platform,external_account_id,enabled,ingest_eligible,revision,"
            "created_at,updated_at) VALUES(:id,'acp','yuki-acp',1,0,1,:now,:now)"
        ),
        {"id": str(uuid4()), "now": datetime.now(UTC)},
    )


def downgrade() -> None:
    raise RuntimeError("ACP session histories and request scopes require a forward migration")
