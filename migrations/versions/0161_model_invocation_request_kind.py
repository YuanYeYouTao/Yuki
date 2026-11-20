"""Record which request of a Main chain a model invocation is, and its chain (#649).

``request_kind`` (main / execution_compact / public_compact) and
``request_chain_id`` are observation only. Both are nullable without a
default, so SQLite adds them to the table definition without rewriting or
reading rows; historical rows stay NULL because neither value can be
reconstructed from them. No index: lookups stay inside the existing
conversation and time-range indexes.

0160 belongs to the watchdog change (#664). Until that merges this revision
follows 0159; once it merges, ``down_revision`` becomes ``"0160"`` so the
bundled migrations keep a single head.
"""

import sqlalchemy as sa
from alembic import op

revision = "0161"
down_revision = "0159"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("model_invocations", sa.Column("request_kind", sa.String(32)))
    op.add_column("model_invocations", sa.Column("request_chain_id", sa.String(64)))


def downgrade() -> None:
    with op.batch_alter_table("model_invocations") as batch:
        batch.drop_column("request_chain_id")
        batch.drop_column("request_kind")
