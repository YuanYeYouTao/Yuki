"""Episode titles for the layered memory P2 episode index (#681).

``memory_facts.title`` holds the short title of an episode, written by the
model call that writes the episode (self-reflection, extraction, Dream). Other
kinds keep NULL. The column is nullable without a default, so SQLite adds it to
the table definition without rewriting or reading rows; existing episodes stay
NULL until the owner runs the backfill CLI. Downgrade drops only the column
(``ALTER TABLE DROP COLUMN`` keeps the table, its triggers and indexes); no fact
is lost. Frozen: does not import mutable ORM models.
"""

import sqlalchemy as sa
from alembic import op

revision = "0163"
down_revision = "0162"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("memory_facts", sa.Column("title", sa.Text()))


def downgrade() -> None:
    op.drop_column("memory_facts", "title")
