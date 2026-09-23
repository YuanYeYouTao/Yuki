"""Allow a durable SELF automation owner and persist one-shot Work waits."""

import sqlalchemy as sa
from alembic import op

revision = "0071"
down_revision = "0070"
branch_labels = None
depends_on = None

_OWNER_CHECK = (
    "((creator_kind = 'person' AND (canonical_creator_person_id IS NOT NULL OR "
    "status IN ('completed', 'cancelled', 'failed', 'blocked'))) OR "
    "(creator_kind = 'self' AND canonical_creator_person_id IS NULL "
    "AND creator_user_id = '' AND created_from_message_id = '' "
    "AND canonical_target_person_id IS NULL)) AND "
    "((status IN ('active', 'paused') "
    "AND ((canonical_target_person_id IS NOT NULL AND canonical_target_space_id IS NULL) OR "
    "(canonical_target_person_id IS NULL AND canonical_target_space_id IS NOT NULL))) OR "
    "(status IN ('completed', 'cancelled', 'failed', 'blocked') "
    "AND NOT (canonical_target_person_id IS NOT NULL AND canonical_target_space_id IS NOT NULL)))"
)


def upgrade() -> None:
    op.execute("PRAGMA defer_foreign_keys=ON")
    op.add_column(
        "chat_events",
        sa.Column("external_resume_wait", sa.Boolean(), nullable=False, server_default="0"),
    )
    op.add_column("admin_operation_events", sa.Column("actor_principal_kind", sa.String(8)))
    op.add_column("admin_operation_events", sa.Column("actor_principal_id", sa.String(36)))
    op.create_table(
        "self_time_settings",
        sa.Column("principal_id", sa.String(4), primary_key=True),
        sa.Column("timezone", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("principal_id = 'self'", name="ck_self_time_identity"),
    )
    with op.batch_alter_table("automations", recreate="always") as batch:
        batch.add_column(
            sa.Column("creator_kind", sa.String(8), nullable=False, server_default="person")
        )
        batch.drop_constraint("ck_automations_canonical_owner", type_="check")
        batch.create_check_constraint("ck_automations_canonical_owner", _OWNER_CHECK)
    op.create_index(
        "uq_automation_self_creation_call",
        "automations",
        ["creation_source_key"],
        unique=True,
        sqlite_where=sa.text("creator_kind = 'self' AND creation_source_key IS NOT NULL"),
    )
    op.create_table(
        "runtime_work_waits",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "work_id",
            sa.String(36),
            sa.ForeignKey("runtime_work.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "conversation_id",
            sa.String(36),
            sa.ForeignKey("canonical_conversations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("generation", sa.Integer, nullable=False),
        sa.Column("principal_kind", sa.String(8), nullable=False),
        sa.Column("principal_id", sa.String(36), nullable=False),
        sa.Column("call_key", sa.String(256), nullable=False, unique=True),
        sa.Column("request_json", sa.Text, nullable=False),
        sa.Column("mode", sa.String(3), nullable=False),
        sa.Column("conditions_json", sa.Text, nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("deadline", sa.Float),
        sa.Column("created", sa.Float, nullable=False),
        sa.Column("updated", sa.Float, nullable=False),
        sa.Column("delivered", sa.Float),
        sa.CheckConstraint("mode IN ('any','all')", name="ck_runtime_work_wait_mode"),
        sa.CheckConstraint(
            "status IN ('active','delivered','cancelled','expired','invalidated')",
            name="ck_runtime_work_wait_status",
        ),
        sa.CheckConstraint(
            "(principal_kind = 'self' AND principal_id = 'self') OR "
            "(principal_kind = 'person' AND principal_id != '' AND principal_id != 'self')",
            name="ck_runtime_work_wait_principal",
        ),
    )
    op.create_index(
        "uq_runtime_work_wait_active",
        "runtime_work_waits",
        ["work_id"],
        unique=True,
        sqlite_where=sa.text("status = 'active'"),
    )
    op.create_index(
        "ix_runtime_work_wait_scope",
        "runtime_work_waits",
        ["conversation_id", "generation", "status"],
    )
    op.create_index(
        "ix_runtime_work_wait_deadline",
        "runtime_work_waits",
        ["status", "deadline"],
    )


def downgrade() -> None:
    # Wait and SELF owner identities are durable facts. Destructive rollback is forbidden.
    pass
