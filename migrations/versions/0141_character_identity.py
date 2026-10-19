"""Add the stable single-character identity and explicit owner grants."""

from datetime import UTC, datetime
from uuid import uuid4

import sqlalchemy as sa
from alembic import op

revision = "0141"
down_revision = "0140"
branch_labels = None
depends_on = None


def upgrade() -> None:
    profile = op.create_table(
        "character_profile",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("singleton", sa.Integer(), nullable=False, unique=True),
        sa.Column("slug", sa.String(), nullable=False),
        sa.Column("display_name", sa.String(), nullable=False),
        sa.Column("bio", sa.Text(), nullable=False),
        sa.Column("accent_light", sa.String(7), nullable=False),
        sa.Column("accent_dark", sa.String(7), nullable=False),
        sa.Column("avatar_sha256", sa.String(64), nullable=True),
        sa.Column("default_avatar_sha256", sa.String(64), nullable=True),
        sa.Column("avatar_revision", sa.Integer(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("singleton = 1", name="ck_character_singleton"),
    )
    now = datetime.now(UTC)
    op.bulk_insert(
        profile,
        [
            {
                "id": str(uuid4()),
                "singleton": 1,
                "slug": "yuki",
                "display_name": "Yuki",
                "bio": "",
                "accent_light": "#3A6BD6",
                "accent_dark": "#7FA4FF",
                "avatar_sha256": None,
                "default_avatar_sha256": None,
                "avatar_revision": 0,
                "revision": 1,
                "created_at": now,
                "updated_at": now,
            }
        ],
    )
    op.create_table(
        "character_owner_grants",
        sa.Column(
            "person_id",
            sa.String(36),
            sa.ForeignKey("persons.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("granted_by_principal", sa.String(36), nullable=False),
        sa.Column("granted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("character_owner_grants")
    op.drop_table("character_profile")
