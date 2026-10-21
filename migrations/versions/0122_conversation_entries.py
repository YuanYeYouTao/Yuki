"""Keep platform entries and Telegram topics in distinct conversations."""

from alembic import op

revision = "0122"
down_revision = "0121"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Nullable additions preserve every existing conversation and its frozen alias.
    op.execute(
        "ALTER TABLE canonical_conversations ADD COLUMN entry_presence_id VARCHAR(36) REFERENCES presences(id)"
    )
    op.execute(
        "ALTER TABLE canonical_conversations ADD COLUMN space_binding_id VARCHAR(36) REFERENCES space_bindings(id)"
    )
    op.execute("ALTER TABLE canonical_conversations ADD COLUMN thread_id VARCHAR(128)")
    op.execute("""
        UPDATE canonical_conversations AS c SET entry_presence_id = COALESCE(
          (SELECT e.ingress_presence_id FROM chat_events e
           WHERE e.canonical_conversation_id=c.id AND e.ingress_presence_id IS NOT NULL
           ORDER BY e.id LIMIT 1),
          (SELECT p.id FROM presences p JOIN identity_bindings b
           ON b.id=c.identity_binding_id AND b.platform='app'
           WHERE p.platform='app' AND p.external_account_id='yuki-app'),
          (SELECT p.id FROM conversation_legacy_aliases a JOIN presences p
           ON p.platform='qq' AND a.scope_key LIKE 'bot:'||p.external_account_id||':private:%'
           WHERE a.conversation_id=c.id ORDER BY a.created_at,a.id LIMIT 1)
        ) WHERE c.kind='private'
    """)
    op.execute("""
        UPDATE canonical_conversations AS c SET space_binding_id=(
          SELECT b.id FROM space_bindings b JOIN conversation_legacy_aliases a
          ON a.conversation_id=c.id AND a.is_primary=1
          WHERE b.space_id=c.space_id AND b.platform='qq'
            AND a.scope_key LIKE 'bot:%:group:'||b.external_space_id
        ) WHERE c.kind='space'
    """)
    # Preserve old source distinctions, and separate real platform/topic entries.
    op.drop_index("uq_chat_events_external_event_target", table_name="chat_events")
    op.execute(
        "CREATE UNIQUE INDEX uq_chat_events_external_event_target ON chat_events(source_plugin_id,external_event_key,canonical_conversation_id,scope_type,external_target_id) WHERE event_kind='external_event'"
    )
    op.drop_index("uq_canonical_conversations_binding", table_name="canonical_conversations")
    op.drop_index("uq_canonical_conversations_space", table_name="canonical_conversations")
    op.execute(
        "CREATE UNIQUE INDEX uq_canonical_conversations_binding ON canonical_conversations(identity_binding_id,entry_presence_id) WHERE kind='private'"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_canonical_conversations_space ON canonical_conversations(space_binding_id,coalesce(thread_id,'')) WHERE kind='space' AND space_binding_id IS NOT NULL"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_canonical_conversations_legacy_space ON canonical_conversations(space_id) WHERE kind='space' AND space_binding_id IS NULL"
    )


def downgrade() -> None:
    raise RuntimeError("Distinct entry histories cannot be merged by a schema downgrade")
