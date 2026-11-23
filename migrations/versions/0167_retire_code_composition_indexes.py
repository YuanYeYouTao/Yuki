"""Drop the Code Mode composition-child indexes on ``runtime_work_effects`` (#573).

Code Mode is retired. Only composition children carried
``$.invocation.parent_effect_key``/``child_ordinal``/``feed_index``/``engine_call_id``;
direct invocations no longer write those keys. Existing receipts, including settled
``code_composition`` parents and their children, stay unchanged as original facts.
Frozen: the downgrade recreates the 0096 SQL verbatim and imports no runtime module.
"""

from alembic import op

revision = "0167"
down_revision = "0166"
branch_labels = None
depends_on = None

INDEXES = {
    "ix_runtime_effects_parent": "CREATE INDEX IF NOT EXISTS ix_runtime_effects_parent ON runtime_work_effects (work_id, json_extract(receipt_json, '$.invocation.parent_effect_key'), effect_key) WHERE json_extract(receipt_json, '$.invocation.version') = 1",
    "ux_runtime_effects_child_ordinal": "CREATE UNIQUE INDEX IF NOT EXISTS ux_runtime_effects_child_ordinal ON runtime_work_effects (work_id, json_extract(receipt_json, '$.invocation.parent_effect_key'), json_extract(receipt_json, '$.invocation.child_ordinal')) WHERE json_extract(receipt_json, '$.invocation.version') = 1 AND json_type(receipt_json, '$.invocation.parent_effect_key') = 'text'",
    "ux_runtime_effects_engine_call": "CREATE UNIQUE INDEX IF NOT EXISTS ux_runtime_effects_engine_call ON runtime_work_effects (work_id, json_extract(receipt_json, '$.invocation.parent_effect_key'), json_extract(receipt_json, '$.invocation.feed_index'), json_extract(receipt_json, '$.invocation.engine_call_id')) WHERE json_extract(receipt_json, '$.invocation.version') = 1 AND json_type(receipt_json, '$.invocation.parent_effect_key') = 'text'",
}


def upgrade() -> None:
    for name in INDEXES:
        op.execute(f"DROP INDEX IF EXISTS {name}")


def downgrade() -> None:
    for statement in INDEXES.values():
        op.execute(statement)
