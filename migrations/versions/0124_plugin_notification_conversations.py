"""Persist each plugin notification grant's original Conversation entry."""

from alembic import op

revision = "0124"
down_revision = "0123"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE plugin_background_target_grants ADD COLUMN canonical_conversation_id VARCHAR(36) REFERENCES canonical_conversations(id)"
    )
    # Existing children are the authoritative original publication, not today's routes.
    op.execute("""
        UPDATE plugin_background_target_grants AS g SET canonical_conversation_id=(
          SELECT min(canonical_conversation_id) FROM (
            SELECT o.canonical_conversation_id FROM plugin_notification_outbox o
            WHERE o.plugin_id=g.plugin_id AND o.canonical_conversation_id IS NOT NULL
              AND ((g.target_type='private' AND o.canonical_target_person_id=g.canonical_target_person_id)
                OR (g.target_type='group' AND o.canonical_target_space_id=g.canonical_target_space_id))
            UNION
            SELECT j.canonical_conversation_id FROM plugin_background_turn_jobs j
            WHERE j.plugin_id=g.plugin_id AND j.canonical_conversation_id IS NOT NULL
              AND ((g.target_type='private' AND j.canonical_target_person_id=g.canonical_target_person_id)
                OR (g.target_type='group' AND j.canonical_target_space_id=g.canonical_target_space_id))
          ) HAVING count(*)=1
        )
    """)
    # An unqueued legacy grant has no execution to replay. A unique existing
    # original entry may be filled; ambiguity stays NULL for explicit selection.
    op.execute("""
        UPDATE plugin_background_target_grants AS g SET canonical_conversation_id=(
          SELECT min(c.id) FROM canonical_conversations c
          WHERE (g.target_type='private' AND c.person_id=g.canonical_target_person_id
                 AND c.entry_presence_id=g.canonical_presence_id)
             OR (g.target_type='group' AND c.space_id=g.canonical_target_space_id
                 AND c.space_binding_id IN (SELECT b.id FROM space_bindings b JOIN presences p
                     ON p.id=g.canonical_presence_id AND b.platform=p.platform
                     WHERE b.space_id=g.canonical_target_space_id))
          HAVING count(*)=1
        ) WHERE g.canonical_conversation_id IS NULL
    """)
    op.drop_index(
        "uq_plugin_background_target_person", table_name="plugin_background_target_grants"
    )
    op.drop_index("uq_plugin_background_target_space", table_name="plugin_background_target_grants")
    op.execute(
        "CREATE UNIQUE INDEX uq_plugin_background_target_person ON plugin_background_target_grants(plugin_id,canonical_target_person_id) WHERE target_type='private' AND canonical_conversation_id IS NULL"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_plugin_background_target_space ON plugin_background_target_grants(plugin_id,canonical_target_space_id) WHERE target_type='group' AND canonical_conversation_id IS NULL"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_plugin_background_target_conversation ON plugin_background_target_grants(plugin_id,canonical_conversation_id) WHERE canonical_conversation_id IS NOT NULL"
    )


def downgrade() -> None:
    raise RuntimeError("Distinct notification entry grants cannot be merged")
