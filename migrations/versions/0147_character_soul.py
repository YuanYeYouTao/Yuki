"""Canonical character presentation and Person editing grants.

The selected name is explicitly initialized by the setup/upgrade operation.
This migration does not infer it from environment variables or old prompt text.
"""

import sqlalchemy as sa
from alembic import op

revision = "0147"
down_revision = "0145"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "character_profile",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("singleton", sa.Integer(), nullable=False, unique=True),
        sa.Column("slug", sa.String(), nullable=False),
        sa.Column("display_name", sa.String(), nullable=False),
        sa.Column("bio", sa.Text(), nullable=False),
        sa.Column("accent_light", sa.String(7), nullable=False),
        sa.Column("accent_dark", sa.String(7), nullable=False),
        sa.Column("avatar_sha256", sa.String(64)),
        sa.Column("default_avatar_sha256", sa.String(64)),
        sa.Column("avatar_revision", sa.Integer(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("singleton = 1", name="ck_character_singleton"),
    )
    op.create_table(
        "character_owner_grants",
        sa.Column("person_id", sa.String(36), sa.ForeignKey("persons.id"), primary_key=True),
        sa.Column("granted_by_principal", sa.String(36), nullable=False),
        sa.Column("granted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
    )


def downgrade() -> None:
    raise RuntimeError(
        "Character identity and original grants are retained; restore matching code."
    )
