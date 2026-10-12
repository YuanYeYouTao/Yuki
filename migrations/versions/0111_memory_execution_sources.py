"""Remove retired event/initiative source enumeration from Memory receipts."""

from alembic import op

revision = "0111"
down_revision = "0110"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("memory_tool_receipts") as batch:
        batch.drop_constraint("ck_memory_tool_receipts_source", type_="check")
    with op.batch_alter_table("memory_mutation_receipts") as batch:
        batch.drop_constraint("ck_memory_mutation_trigger_source", type_="check")


def downgrade():
    raise RuntimeError("Memory execution receipts cannot be narrowed back to event/initiative")
