"""Person profiles for the layered memory P4 (#227).

``memory_profiles`` holds one row per (person root, audience) with the current
version pointer, the signature of the available fact set last handled, the last
attempt time and the owner lock. ``memory_profile_versions`` stores every version
as a full snapshot with its source and reason; rows are only appended (whole
profiles are deleted when their person is forgotten). ``memory_space_members`` holds
each group's displayed members, one row per position, so forgetting a person
cascades. All three are derived views: dropping them loses no fact.

Person core sets are replaced by profiles: the person rows of
``memory_resident_sets`` are deleted and the table is rebuilt so both CHECKs allow
space and SELF owners only, then the expression unique index is recreated. The
downgrade drops the three tables and restores the original CHECKs and index; it
does not restore deleted person rows. Frozen: does not import mutable ORM models.
"""

import sqlalchemy as sa
from alembic import op

revision = "0164"
down_revision = "0163"
branch_labels = None
depends_on = None

_PERSON_OWNER = "(owner_kind = 'person' AND owner_person_id IS NOT NULL AND owner_space_id IS NULL)"
_SPACE_SELF_OWNER = (
    "(owner_kind = 'space' AND owner_space_id IS NOT NULL AND owner_person_id IS NULL)"
    " OR (owner_kind = 'self' AND owner_person_id IS NULL AND owner_space_id IS NULL)"
)
_RESIDENT_COLUMNS = (
    "id, owner_kind, owner_person_id, owner_space_id, fact_ids_json, source,"
    " considered_through_fact_id, revision, selected_at, created_at, updated_at"
)


def _rebuild_resident_sets(owner_check: str) -> None:
    op.create_table(
        "_memory_resident_sets_rebuild",
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
        sa.CheckConstraint(owner_check, name="ck_memory_resident_sets_owner"),
        sa.CheckConstraint(
            "source IN ('dream', 'owner') AND (owner_kind != 'self' OR source = 'owner')",
            name="ck_memory_resident_sets_source",
        ),
    )
    op.execute(
        f"INSERT INTO _memory_resident_sets_rebuild ({_RESIDENT_COLUMNS})"
        f" SELECT {_RESIDENT_COLUMNS} FROM memory_resident_sets"
    )
    op.drop_index("uq_memory_resident_sets_owner", table_name="memory_resident_sets")
    op.drop_table("memory_resident_sets")
    op.rename_table("_memory_resident_sets_rebuild", "memory_resident_sets")
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


def upgrade() -> None:
    op.execute("DELETE FROM memory_resident_sets WHERE owner_kind = 'person'")
    _rebuild_resident_sets(_SPACE_SELF_OWNER)
    op.create_table(
        "memory_profiles",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "person_id",
            sa.String(36),
            sa.ForeignKey("persons.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("audience", sa.String(16), nullable=False),
        sa.Column("current_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("considered_signature", sa.String(64)),
        sa.Column("attempted_at", sa.DateTime(timezone=True)),
        sa.Column("locked", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("person_id", "audience", name="uq_memory_profiles_person_audience"),
        sa.CheckConstraint("audience IN ('public', 'private')", name="ck_memory_profiles_audience"),
        sa.CheckConstraint("current_version >= 0", name="ck_memory_profiles_version"),
    )
    op.create_table(
        "memory_profile_versions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "profile_id",
            sa.Integer(),
            sa.ForeignKey("memory_profiles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("entries_json", sa.Text(), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("dream_run_public_id", sa.String(36)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("profile_id", "version", name="uq_memory_profile_versions_version"),
        sa.CheckConstraint("version >= 1", name="ck_memory_profile_versions_version"),
        sa.CheckConstraint(
            "source IN ('dream', 'owner', 'program')", name="ck_memory_profile_versions_source"
        ),
    )
    op.create_table(
        "memory_space_members",
        sa.Column(
            "space_id",
            sa.String(36),
            sa.ForeignKey("spaces.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("position", sa.Integer(), primary_key=True),
        sa.Column(
            "person_id",
            sa.String(36),
            sa.ForeignKey("persons.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("dream_run_public_id", sa.String(36)),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("space_id", "person_id", name="uq_memory_space_members_person"),
        sa.CheckConstraint("position BETWEEN 1 AND 5", name="ck_memory_space_members_position"),
        sa.CheckConstraint("source IN ('dream', 'owner')", name="ck_memory_space_members_source"),
    )
    op.create_index("ix_memory_space_members_person", "memory_space_members", ["person_id"])


def downgrade() -> None:
    op.drop_index("ix_memory_space_members_person", table_name="memory_space_members")
    op.drop_table("memory_space_members")
    op.drop_table("memory_profile_versions")
    op.drop_table("memory_profiles")
    _rebuild_resident_sets(f"{_PERSON_OWNER} OR {_SPACE_SELF_OWNER}")
