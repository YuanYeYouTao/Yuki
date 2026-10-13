"""A context reset retires public history, not original private evidence."""

from alembic import op

revision = "0113"
down_revision = "0112"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DROP TRIGGER context_observation_reset")


def downgrade() -> None:
    raise RuntimeError("Context resets cannot resume deleting original private evidence")
