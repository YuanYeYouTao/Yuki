"""Resident memory core sets for the layered memory P1 (#681).

One row per owner (person root, space, or SELF) holding an ordered list of
fact IDs and the selection watermark. It is a derived view: no memory text is
stored, no existing row is backfilled, and dropping it loses no fact.
Frozen: does not import mutable ORM models.
"""

import sqlalchemy as sa
from alembic import op

revision = "0162"
down_revision = "0161"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "memory_resident_sets",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("owner_kind", sa.String(16), nullable=False),
        sa.Column(
            "owner_person_id",
            sa.String(36),
            sa.ForeignKey("persons.id", ondelete="CASCADE"),
        ),
        sa.Column(
            "owner_space_id",
            sa.String(36),
            sa.ForeignKey("spaces.id", ondelete="CASCADE"),
        ),
        sa.Column("fact_ids_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("considered_through_fact_id", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("selected_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "(owner_kind = 'person' AND owner_person_id IS NOT NULL AND owner_space_id IS NULL)"
            " OR (owner_kind = 'space' AND owner_space_id IS NOT NULL AND owner_person_id IS NULL)"
            " OR (owner_kind = 'self' AND owner_person_id IS NULL AND owner_space_id IS NULL)",
            name="ck_memory_resident_sets_owner",
        ),
        sa.CheckConstraint(
            "source IN ('dream', 'owner') AND (owner_kind != 'self' OR source = 'owner')",
            name="ck_memory_resident_sets_source",
        ),
    )
    op.create_index(
        "uq_memory_resident_sets_owner",
        "memory_resident_sets",
        [
            "owner_kind",
            sa.text("COALESCE(owner_person_id, '')"),
            sa.text("COALESCE(owner_space_id, '')"),
        ],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_memory_resident_sets_owner", table_name="memory_resident_sets")
    op.drop_table("memory_resident_sets")
