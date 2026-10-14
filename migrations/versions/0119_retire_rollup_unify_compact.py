"""Retire derived Rollup storage while retaining public checkpoints and paid facts."""

from alembic import op

from qq_ai_bot.conversation.projection_compact_schema import PROJECTION_TRIGGERS_0119

revision = "0119"
down_revision = "0118"
branch_labels = None
depends_on = None

_RETIRED_CONFIG_KEYS = (
    "context.compaction_window_tokens",
    "context.compaction_trigger_ratio",
    "context.compaction_target_ratio",
    "context.work_compaction_trigger_ratio",
    "context.work_compaction_target_ratio",
    "context.compaction_output_tokens",
    "context.rollup_output_tokens",
    "context.rollup_summary_characters",
)


def upgrade() -> None:
    connection = op.get_bind()
    connection.exec_driver_sql("""
        UPDATE prompt_projections AS p
        SET selected_summary_text=r.summary_text,
            selected_summary_coverage=r.covered_through_event_id,
            selected_summary_kind='compact', selected_summary_renderer=2,
            payload_json='[]', byte_size=2, revision=p.revision+1,
            invalidated_reason='compact'
        FROM canonical_conversation_rollups AS r
        JOIN canonical_conversations AS c ON c.id=r.conversation_id
        WHERE p.conversation_id=c.id AND p.generation=c.generation
          AND p.starts_after_event_id=c.starts_after_event_id
          AND p.source_revision=c.prompt_source_revision
          AND p.selected_summary_text IS NULL AND r.generation=c.generation
          AND r.summary_kind IN ('model', 'extractive', 'migration')
          AND r.covered_through_event_id BETWEEN c.starts_after_event_id AND c.last_event_id
    """)
    connection.exec_driver_sql("""
        UPDATE runtime_work
        SET checkpoint_json=json_remove(checkpoint_json,'$.context_rollup'),
            state='queued', reason='context_prepared', revision=revision+1
        WHERE state='waiting_external'
          AND json_extract(checkpoint_json,'$.context_rollup') IS NOT NULL
    """)
    keys = ",".join("?" for _ in _RETIRED_CONFIG_KEYS)
    connection.exec_driver_sql(
        f"DELETE FROM runtime_config_overrides WHERE config_key IN ({keys})",
        _RETIRED_CONFIG_KEYS,
    )
    connection.exec_driver_sql(
        """
        UPDATE prompt_projections AS p
        SET selected_summary_kind='compact', selected_summary_renderer=2,
            payload_json='[]', byte_size=2, revision=p.revision+1,
            invalidated_reason='compact'
        FROM canonical_conversations AS c
        WHERE p.conversation_id=c.id AND p.generation=c.generation
          AND p.starts_after_event_id=c.starts_after_event_id
          AND p.source_revision=c.prompt_source_revision
          AND p.selected_summary_text IS NOT NULL AND trim(p.selected_summary_text) <> ''
          AND p.selected_summary_coverage BETWEEN c.starts_after_event_id AND c.last_event_id
    """
    )
    # SQLite rebuild must temporarily remove triggers that refer to this table.
    # Recreate the actual installed trigger SQL verbatim; source/privacy contracts
    # and third-party trigger definitions remain intact.
    triggers = list(
        connection.exec_driver_sql(
            "SELECT name, sql FROM sqlite_master WHERE type='trigger' "
            "AND (tbl_name='canonical_conversations' OR sql LIKE '%canonical_conversations%')"
        )
    )
    retained = [
        (name, sql) for name, sql in triggers if "canonical_conversation_rollup" not in name
    ]
    for name, _sql in triggers:
        connection.exec_driver_sql('DROP TRIGGER "' + name.replace('"', '""') + '"')
    for table in (
        "canonical_conversation_rollup_emergency_overlays",
        "canonical_conversation_rollup_jobs",
        "canonical_conversation_rollups",
        "canonical_rollup_signals",
    ):
        op.drop_table(table)
    with op.batch_alter_table("canonical_conversations", recreate="always") as batch:
        batch.drop_constraint("ck_canonical_conversations_watermarks", type_="check")
        batch.drop_column("covered_through_event_id")
        batch.drop_column("uncovered_event_count")
        batch.drop_column("uncovered_character_count")
        batch.create_check_constraint(
            "ck_canonical_conversations_watermarks",
            "starts_after_event_id >= 0 AND last_event_id >= 0 "
            "AND last_generation_change_event_id >= 0 "
            "AND starts_after_event_id <= last_event_id "
            "AND last_generation_change_event_id <= last_event_id",
        )
    for _name, sql in retained:
        connection.exec_driver_sql(sql)
    # Erase private compact material while retaining its content-free dispatch
    # fact, so privacy invalidation cannot authorise another paid request.
    for name in ("context_observation_privacy", "context_observation_privacy_initial"):
        connection.exec_driver_sql(f'DROP TRIGGER IF EXISTS "{name}"')
        connection.exec_driver_sql(PROJECTION_TRIGGERS_0119[name])


def downgrade() -> None:
    raise RuntimeError(
        "Retired Rollup rows cannot be reconstructed; retain forward execution facts"
    )
