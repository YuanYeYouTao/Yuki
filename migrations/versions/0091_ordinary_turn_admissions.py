"""Freeze human admission identity without creating ordinary Work journals."""

import sqlalchemy as sa
from alembic import op

revision = "0091"
down_revision = "0090"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ordinary_turn_admissions",
        sa.Column(
            "event_id",
            sa.Integer(),
            sa.ForeignKey("chat_events.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "conversation_id",
            sa.String(36),
            sa.ForeignKey("canonical_conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("source_revision", sa.String(128), nullable=False),
        sa.Column("actor_person_id", sa.String(36), nullable=False),
        sa.Column("presence_id", sa.String(36), nullable=False),
        sa.Column("activation_id", sa.String(36), nullable=False),
        sa.Column("coordinator_version", sa.Integer(), nullable=False),
        sa.Column("unit_key", sa.String(256)),
        sa.Column("target_hint", sa.String(256)),
        sa.Column("basis_json", sa.Text(), nullable=False),
        sa.Column("route", sa.String(16), nullable=False),
        sa.Column("work_id", sa.String(36), sa.ForeignKey("runtime_work.id", ondelete="SET NULL")),
        sa.Column(
            "input_id", sa.Integer(), sa.ForeignKey("runtime_work_inputs.id", ondelete="SET NULL")
        ),
        sa.Column("created", sa.Float(), nullable=False),
        sa.CheckConstraint("generation >= 1 AND coordinator_version >= 0"),
        sa.CheckConstraint("route IN ('ordinary', 'work')"),
    )


def downgrade() -> None:
    if op.get_bind().execute(sa.text("SELECT 1 FROM ordinary_turn_admissions LIMIT 1")).first():
        raise RuntimeError(
            "ordinary admissions exist; use a compatible runtime without dropping facts"
        )
    op.drop_table("ordinary_turn_admissions")
