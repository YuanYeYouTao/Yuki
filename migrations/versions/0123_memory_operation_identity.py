"""Keep original Memory operation receipts without merging independent content."""

from alembic import op

revision = "0123"
down_revision = "0122"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("memory_mutation_receipts") as batch:
        batch.drop_constraint("uq_memory_mutation_receipts_claim", type_="unique")


def downgrade() -> None:
    raise RuntimeError("Independent Memory operations cannot restore content uniqueness")
