"""Keep disabled group participation configurable after retiring admin tools."""

from alembic import op

revision = "0114"
down_revision = "0113"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Preserve the effective disabled state in the shared configuration store.
    # Original operation audits and tool receipts are historical facts.
    op.execute("""
        INSERT INTO runtime_config_overrides (
            config_key, scope_type, canonical_space_id, value_json, value_type,
            apply_mode, version, created_at, updated_at, updated_by
        )
        SELECT 'conversation.autonomous_enabled', 'group', id, 'false', 'boolean',
               'hot', 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 'migration:0114'
        FROM spaces WHERE autonomous_enabled = 0
        ON CONFLICT(config_key, canonical_space_id) WHERE scope_type = 'group'
        DO UPDATE SET value_json = 'false',
                      version = runtime_config_overrides.version + 1,
                      updated_at = CURRENT_TIMESTAMP,
                      updated_by = 'migration:0114'
    """)
    op.execute("UPDATE spaces SET autonomous_enabled = 1 WHERE autonomous_enabled = 0")


def downgrade() -> None:
    raise RuntimeError("Retired admin tools do not recreate a second participation gate")
