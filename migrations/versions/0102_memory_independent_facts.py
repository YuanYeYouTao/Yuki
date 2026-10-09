"""Allow independent facts with the same memory key; retain all historical data."""

from alembic import op

revision = "0102"
down_revision = "0101"
branch_labels = None
depends_on = None

_INDEXES = (
    "uq_memory_facts_active_canonical_person_key",
    "uq_memory_facts_active_canonical_person_group_key",
    "uq_memory_facts_active_canonical_group_key",
    "uq_memory_facts_active_canonical_self_key",
)


def upgrade() -> None:
    for name in _INDEXES:
        op.execute(f'DROP INDEX IF EXISTS "{name}"')


def downgrade() -> None:
    raise RuntimeError(
        "Independent facts cannot restore single-active uniqueness without losing data"
    )
