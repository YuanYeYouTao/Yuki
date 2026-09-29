"""Record Claude cache creation tokens without inferring historical values."""

import sqlalchemy as sa
from alembic import op

revision = "0081"
down_revision = "0080"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Historical invocations remain NULL: read/input totals cannot reconstruct
    # whether those tokens were billed as ordinary input or cache writes.
    columns = {
        column["name"] for column in sa.inspect(op.get_bind()).get_columns("model_invocations")
    }
    for name in (
        "cache_creation_input_tokens",
        "cache_creation_5m_input_tokens",
        "cache_creation_1h_input_tokens",
    ):
        if name not in columns:
            op.add_column("model_invocations", sa.Column(name, sa.Integer()))


def downgrade() -> None:
    # Keep already-recorded billing evidence during a code rollback.
    pass
