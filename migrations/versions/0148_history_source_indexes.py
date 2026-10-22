"""Seek original conversation sources by Space and either Binding projection."""

import sqlalchemy as sa
from alembic import op

revision = "0148"
down_revision = "0147"
branch_labels = None
depends_on = None

_INDEXES = (
    ("ix_conversations_space", "space_id"),
    ("ix_conversations_identity_binding", "identity_binding_id"),
    ("ix_conversations_space_binding", "space_binding_id"),
)


def upgrade() -> None:
    for name, column in _INDEXES:
        op.create_index(
            name,
            "canonical_conversations",
            [column, "id"],
            sqlite_where=sa.text(f"{column} IS NOT NULL"),
        )


def downgrade() -> None:
    for name, _column in reversed(_INDEXES):
        op.drop_index(name, table_name="canonical_conversations")
