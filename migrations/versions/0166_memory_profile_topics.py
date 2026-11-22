"""Profile topics (记忆意图召回评估 §2): ``memory_profile_versions.topics_json``.

Each profile version may carry up to four short topics with their supporting fact IDs,
written by the same Dream profile call as the entries. Versions written before this
revision keep NULL, which the Dream step treats as due once. SQLite adds and drops the
nullable column in place. Frozen: does not import mutable ORM models.
"""

import sqlalchemy as sa
from alembic import op

revision = "0166"
down_revision = "0165"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("memory_profile_versions", sa.Column("topics_json", sa.Text()))


def downgrade() -> None:
    op.drop_column("memory_profile_versions", "topics_json")
