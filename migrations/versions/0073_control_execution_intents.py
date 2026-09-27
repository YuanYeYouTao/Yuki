"""Preserve control execution intent before releasing the writer for external work."""

import sqlalchemy as sa
from alembic import op

revision = "0073"
down_revision = "0072"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "admin_operation_events", sa.Column("control_request_id", sa.String(36), nullable=True)
    )
    op.create_index(
        "ix_admin_operation_events_control_request_id",
        "admin_operation_events",
        ["control_request_id"],
    )
    # Only a proven receipt/audit pair is migrated. QQ audit provenance is untouched.
    op.execute(
        sa.text("""UPDATE admin_operation_events SET
        control_request_id = trigger_message_id,
        actor_principal_id = actor_user_id,
        actor_principal_kind = 'control', trigger_message_id = ''
        WHERE id IN (SELECT r.audit_id FROM control_command_receipts r
            WHERE r.audit_id IS NOT NULL AND r.principal_id = admin_operation_events.actor_user_id
            AND r.request_id = admin_operation_events.trigger_message_id
            AND admin_operation_events.conversation_key = '')""")
    )
    with op.batch_alter_table("control_command_receipts") as batch:
        batch.drop_constraint("ck_control_command_receipts_status", type_="check")
        batch.drop_constraint("ck_control_command_receipts_lifecycle", type_="check")
        batch.create_check_constraint(
            "ck_control_command_receipts_status",
            "status IN ('succeeded', 'failed', 'running', 'unknown')",
        )
        batch.create_check_constraint(
            "ck_control_command_receipts_lifecycle",
            "(status = 'succeeded' AND problem_code IS NULL AND audit_id IS NOT NULL "
            "AND audit_id >= 1 AND effective_state_json IS NOT NULL) OR "
            "(status = 'failed' AND problem_code IS NOT NULL AND length(problem_code) > 0 "
            "AND length(problem_code) <= 64 AND problem_code = trim(problem_code) "
            "AND effective_state_json IS NULL AND result_resource_id IS NULL "
            "AND result_revision IS NULL) OR "
            "(status IN ('running', 'unknown') AND audit_id IS NOT NULL AND audit_id >= 1 "
            "AND result_resource_id IS NULL AND result_revision IS NULL "
            "AND effective_state_json IS NULL AND operation_kind = 'control' "
            "AND operation_ref IS NOT NULL AND ((status = 'running' AND problem_code IS NULL) "
            "OR (status = 'unknown' AND problem_code IS NOT NULL)))",
        )


def downgrade() -> None:
    raise RuntimeError("control execution evidence cannot be discarded")
