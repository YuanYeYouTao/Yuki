"""Episode index rows and long-term impressions for the layered memory P2.1 (#681).

``memory_episode_indexes`` holds one row per conversation audience (a private chat's
person root, or a space): the recent list, at most ten long-term impressions with
their entry time and source, the month counts, the fact ID the row was brought up to
(``through_fact_id``), the owner lock and a revision compared on every write. The
row is a derived view: dropping it loses no fact; the next nightly run or the owner's
``generate`` rebuilds it. Two partial unique indexes give one row per audience.

``ix_memory_facts_supersedes_id`` (partial) serves the successor lookup
``supersedes_id IN (...)``, a full scan without it. ``memory_facts.last_injected_at``
has no writer since F24 and is not in any index, CHECK or trigger, so SQLite drops
it in place.

Downgrade drops the table and the index and adds ``last_injected_at`` back as an
empty nullable column; old values are not restored. Frozen: does not import mutable
ORM models.
"""

import sqlalchemy as sa
from alembic import op

revision = "0165"
down_revision = "0164"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "memory_episode_indexes",
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
        sa.Column("recent_fact_ids_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("memorable_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("month_counts_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("through_fact_id", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("locked", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("as_of", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "(owner_kind = 'private' AND owner_person_id IS NOT NULL AND owner_space_id IS NULL)"
            " OR (owner_kind = 'space' AND owner_space_id IS NOT NULL"
            " AND owner_person_id IS NULL)",
            name="ck_memory_episode_indexes_owner",
        ),
    )
    op.create_index(
        "uq_memory_episode_indexes_private",
        "memory_episode_indexes",
        ["owner_person_id"],
        unique=True,
        sqlite_where=sa.text("owner_kind = 'private'"),
    )
    op.create_index(
        "uq_memory_episode_indexes_space",
        "memory_episode_indexes",
        ["owner_space_id"],
        unique=True,
        sqlite_where=sa.text("owner_kind = 'space'"),
    )
    op.create_index(
        "ix_memory_facts_supersedes_id",
        "memory_facts",
        ["supersedes_id"],
        sqlite_where=sa.text("supersedes_id IS NOT NULL"),
    )
    op.drop_column("memory_facts", "last_injected_at")


def downgrade() -> None:
    op.add_column("memory_facts", sa.Column("last_injected_at", sa.DateTime(timezone=True)))
    op.drop_index("ix_memory_facts_supersedes_id", table_name="memory_facts")
    op.drop_index("uq_memory_episode_indexes_space", table_name="memory_episode_indexes")
    op.drop_index("uq_memory_episode_indexes_private", table_name="memory_episode_indexes")
    op.drop_table("memory_episode_indexes")
