"""Own protocol files by original Work and fence physical reclamation."""

import sqlalchemy as sa
from alembic import op

revision = "0088"
down_revision = "0087"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "runtime_protocol_objects",
        sa.Column("sha256", sa.String(64), primary_key=True),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("prepared_at", sa.Float(), nullable=False),
        sa.Column("deleting", sa.Boolean(), nullable=False, server_default="0"),
        sa.CheckConstraint("byte_size >= 0", name="ck_protocol_object_bytes"),
    )
    op.create_index(
        "ix_protocol_objects_gc", "runtime_protocol_objects", ["deleting", "prepared_at"]
    )
    op.create_table(
        "runtime_protocol_refs",
        sa.Column(
            "work_id",
            sa.String(36),
            sa.ForeignKey("runtime_work.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "sha256",
            sa.String(64),
            sa.ForeignKey("runtime_protocol_objects.sha256", ondelete="RESTRICT"),
            primary_key=True,
        ),
    )
    op.create_index("ix_protocol_refs_digest", "runtime_protocol_refs", ["sha256"])
    op.create_table(
        "runtime_protocol_usage",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("byte_size", sa.Integer(), nullable=False, server_default="0"),
        sa.CheckConstraint("id = 1 AND byte_size >= 0", name="ck_protocol_usage_singleton"),
    )
    op.execute("INSERT INTO runtime_protocol_usage (id, byte_size) VALUES (1, 0)")
    op.execute(
        "CREATE TRIGGER protocol_object_insert AFTER INSERT ON runtime_protocol_objects "
        "BEGIN UPDATE runtime_protocol_usage SET byte_size=byte_size+NEW.byte_size WHERE id=1; END"
    )
    op.execute(
        "CREATE TRIGGER protocol_object_delete AFTER DELETE ON runtime_protocol_objects "
        "BEGIN UPDATE runtime_protocol_usage SET byte_size=byte_size-OLD.byte_size WHERE id=1; END"
    )


def downgrade() -> None:
    if op.get_bind().execute(sa.text("SELECT 1 FROM runtime_protocol_refs LIMIT 1")).first():
        raise RuntimeError("cannot downgrade while private protocol files are referenced")
    op.execute("DROP TRIGGER protocol_object_insert")
    op.execute("DROP TRIGGER protocol_object_delete")
    op.drop_table("runtime_protocol_usage")
    op.drop_table("runtime_protocol_refs")
    op.drop_table("runtime_protocol_objects")
