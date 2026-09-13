"""Fence new Host operation identities without merging historical automations."""

from alembic import op

revision = "0060"
down_revision = "0059"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from qq_ai_bot.runtime.automation_budget_schema import budgets

    budgets.create(op.get_bind(), checkfirst=True)
    # Existing recorded usage remains charged, including unfinished step work.
    # Keeping values above the new limit is intentional: they are paused by
    # reservation rather than silently receiving a fresh allowance.
    op.execute("""
        INSERT OR IGNORE INTO runtime_automation_budgets(run_id, models, tools)
        SELECT r.id,
            max(r.llm_calls, coalesce(json_extract(i.payload_json, '$.llm_calls'), 0)
                + max(0, coalesce(w.model_requests, 0)
                    - coalesce(json_extract(i.payload_json, '$.pending_usage.models'), 0))),
            max(r.tool_calls, coalesce(json_extract(i.payload_json, '$.tool_calls'), 0)
                + max(0, coalesce(w.tool_calls, 0)
                    - coalesce(json_extract(i.payload_json, '$.pending_usage.tools'), 0)))
        FROM automation_runs r
        LEFT JOIN runtime_automation_cursors i ON i.run_id = r.id
        LEFT JOIN runtime_work w ON w.id = json_extract(i.payload_json, '$.work_id')
    """)
    # Old main calls did not persist enough Host ownership to resume safely.
    # Retain their journal and receipts; never replay them under a new identity.
    op.execute("""
        UPDATE runtime_work
        SET state = 'suspended', revision = revision + 1,
            checkpoint_json = json_set(checkpoint_json,
                '$.migration_reason', 'entrypoint_owner_revalidation_required')
        WHERE state IN ('queued', 'running', 'waiting_external', 'waiting_user')
          AND json_extract(source_json, '$.origin') IN ('plugin_session', 'scheduled_automation')
          AND json_extract(source_json, '$.owner') IS NULL
    """)
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_automation_creation_call "
        "ON automations(canonical_creator_person_id, creation_source_key) "
        "WHERE creation_source_key LIKE 'call:%'"
    )


def downgrade() -> None:
    # Keep the effect fence; rolling code back must not permit duplicate calls.
    pass
