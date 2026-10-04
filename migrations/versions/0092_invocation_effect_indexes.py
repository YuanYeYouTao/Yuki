"""Index versioned original invocation identities; preserve legacy receipts."""

import sqlalchemy as sa
from alembic import op

revision = "0092"
down_revision = "0091"
branch_labels = None
depends_on = None

# Frozen migration SQL; never import future runtime declarations.
INDEXES = {
    "ix_runtime_effects_parent": "CREATE INDEX IF NOT EXISTS ix_runtime_effects_parent ON runtime_work_effects (work_id, json_extract(receipt_json, '$.invocation.parent_effect_key'), effect_key) WHERE json_extract(receipt_json, '$.invocation.version') = 1",
    "ux_runtime_effects_child_ordinal": "CREATE UNIQUE INDEX IF NOT EXISTS ux_runtime_effects_child_ordinal ON runtime_work_effects (work_id, json_extract(receipt_json, '$.invocation.parent_effect_key'), json_extract(receipt_json, '$.invocation.child_ordinal')) WHERE json_extract(receipt_json, '$.invocation.version') = 1 AND json_type(receipt_json, '$.invocation.parent_effect_key') = 'text'",
    "ux_runtime_effects_engine_call": "CREATE UNIQUE INDEX IF NOT EXISTS ux_runtime_effects_engine_call ON runtime_work_effects (work_id, json_extract(receipt_json, '$.invocation.parent_effect_key'), json_extract(receipt_json, '$.invocation.feed_index'), json_extract(receipt_json, '$.invocation.engine_call_id')) WHERE json_extract(receipt_json, '$.invocation.version') = 1 AND json_type(receipt_json, '$.invocation.parent_effect_key') = 'text'",
}


def upgrade() -> None:
    for statement in INDEXES.values():
        op.execute(statement)


def downgrade() -> None:
    if (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT 1 FROM runtime_work_effects WHERE json_extract(receipt_json, '$.invocation.version') = 1 OR json_extract(receipt_json, '$.composition.version') = 1 LIMIT 1"
            )
        )
        .first()
    ):
        raise RuntimeError("versioned invocation facts exist; use a compatible reader")
    for name in INDEXES:
        op.execute(f"DROP INDEX IF EXISTS {name}")
