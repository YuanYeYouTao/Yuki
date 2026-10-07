"""Store activation recovery and delivery intent independently of model prose."""

import json
from collections import defaultdict

from alembic import op

revision = "0059"
down_revision = "0058"
branch_labels = None
depends_on = None


_SCHEMA = (
    "\nCREATE TABLE IF NOT EXISTS runtime_work_recovery (\n\twork_id VARCHAR(36) NOT NULL, \n\tactivation_id VARCHAR(36) NOT NULL, \n\texit_reason VARCHAR(64) DEFAULT 'running' NOT NULL, \n\tstage VARCHAR(32) DEFAULT 'activation' NOT NULL, \n\tfailure_json TEXT DEFAULT '{}' NOT NULL, \n\tattempts INTEGER DEFAULT '0' NOT NULL, \n\tnot_before FLOAT DEFAULT '0' NOT NULL, \n\tupdated FLOAT NOT NULL, \n\tPRIMARY KEY (work_id), \n\tFOREIGN KEY(work_id) REFERENCES runtime_work (id) ON DELETE CASCADE\n)\n\n",
    "CREATE INDEX IF NOT EXISTS ix_work_recovery_ready ON runtime_work_recovery (not_before, work_id)",
    "\nCREATE TABLE IF NOT EXISTS runtime_delivery_intents (\n\tid VARCHAR(256) NOT NULL, \n\twork_id VARCHAR(36) NOT NULL, \n\tkind VARCHAR(24) NOT NULL, \n\ttarget_key VARCHAR(256) DEFAULT '' NOT NULL, \n\tmessage_count INTEGER DEFAULT '1' NOT NULL, \n\tstate VARCHAR(24) NOT NULL, \n\tpayload_json TEXT DEFAULT '{}' NOT NULL, \n\treceipt_json TEXT DEFAULT '{}' NOT NULL, \n\tnot_before FLOAT DEFAULT '0' NOT NULL, \n\tcreated FLOAT NOT NULL, \n\tupdated FLOAT NOT NULL, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(work_id) REFERENCES runtime_work (id) ON DELETE CASCADE\n)\n\n",
    "CREATE INDEX IF NOT EXISTS ix_delivery_target_window ON runtime_delivery_intents (target_key, created, state)",
    "CREATE INDEX IF NOT EXISTS ix_delivery_work_state ON runtime_delivery_intents (work_id, state)",
    "\nCREATE TABLE IF NOT EXISTS canonical_rollup_signals (\n\tconversation_id VARCHAR(36) NOT NULL, \n\tgeneration INTEGER NOT NULL, \n\tevent_id INTEGER NOT NULL, \n\trevision INTEGER DEFAULT '1' NOT NULL, \n\tPRIMARY KEY (conversation_id), \n\tFOREIGN KEY(conversation_id) REFERENCES canonical_conversations (id) ON DELETE CASCADE\n)\n\n",
    "\nCREATE TABLE IF NOT EXISTS runtime_automation_cursors (\n\trun_id INTEGER NOT NULL, \n\tscript_hash VARCHAR(64) NOT NULL, \n\tphase VARCHAR(24) NOT NULL, \n\tpayload_json TEXT NOT NULL, \n\tupdated FLOAT NOT NULL, \n\tPRIMARY KEY (run_id), \n\tFOREIGN KEY(run_id) REFERENCES automation_runs (id) ON DELETE CASCADE\n)\n\n",
    "\nCREATE TABLE IF NOT EXISTS runtime_checkpoint_quota (\n\tid INTEGER NOT NULL, \n\tbytes INTEGER NOT NULL, \n\tPRIMARY KEY (id), \n\tCONSTRAINT ck_runtime_checkpoint_bytes CHECK (id = 1 AND bytes >= 0 AND bytes <= 67108864)\n)\n\n",
    "CREATE INDEX IF NOT EXISTS ix_runtime_media_refs_sha ON runtime_work_media_refs(sha256)",
    "INSERT OR IGNORE INTO runtime_checkpoint_quota(id, bytes) SELECT 1, COALESCE((SELECT SUM(length(CAST(payload_json AS BLOB))) FROM runtime_work_journal),0) + COALESCE((SELECT SUM(length(content)) FROM runtime_work_media),0)",
    "CREATE TRIGGER IF NOT EXISTS quota_runtime_work_journal_insert AFTER INSERT ON runtime_work_journal BEGIN UPDATE runtime_checkpoint_quota SET bytes=bytes+(length(CAST(NEW.payload_json AS BLOB))) WHERE id=1; END",
    "CREATE TRIGGER IF NOT EXISTS quota_runtime_work_journal_delete AFTER DELETE ON runtime_work_journal BEGIN UPDATE runtime_checkpoint_quota SET bytes=bytes+(-length(CAST(OLD.payload_json AS BLOB))) WHERE id=1; END",
    "CREATE TRIGGER IF NOT EXISTS quota_runtime_work_journal_update AFTER UPDATE ON runtime_work_journal BEGIN UPDATE runtime_checkpoint_quota SET bytes=bytes+(length(CAST(NEW.payload_json AS BLOB))-length(CAST(OLD.payload_json AS BLOB))) WHERE id=1; END",
    "CREATE TRIGGER IF NOT EXISTS quota_runtime_work_media_insert AFTER INSERT ON runtime_work_media BEGIN UPDATE runtime_checkpoint_quota SET bytes=bytes+(length(CAST(NEW.content AS BLOB))) WHERE id=1; END",
    "CREATE TRIGGER IF NOT EXISTS quota_runtime_work_media_delete AFTER DELETE ON runtime_work_media BEGIN UPDATE runtime_checkpoint_quota SET bytes=bytes+(-length(CAST(OLD.content AS BLOB))) WHERE id=1; END",
    "CREATE TRIGGER IF NOT EXISTS quota_runtime_work_media_update AFTER UPDATE ON runtime_work_media BEGIN UPDATE runtime_checkpoint_quota SET bytes=bytes+(length(CAST(NEW.content AS BLOB))-length(CAST(OLD.content AS BLOB))) WHERE id=1; END",
)


def upgrade() -> None:
    for statement in _SCHEMA:
        op.execute(statement)
    reconcile_legacy_progress(op.get_bind())


def reconcile_legacy_progress(connection) -> None:
    """Old snapshots are cumulative observations, never additional charges."""
    from sqlalchemy import text

    groups = defaultdict(dict)
    owners = defaultdict(set)
    for row in connection.execute(
        text("SELECT source_json, progress_json FROM sandbox_task_runs WHERE progress_json != '{}'")
    ):
        try:
            source, progress = json.loads(row.source_json), json.loads(row.progress_json)
            identity, group = source.get("work_id"), progress.get("group_id")
            if not identity or not group:
                continue
            values = tuple(
                max(0, int(progress.get(key, 0)))
                for key in ("models_used", "tools_used", "messages_used")
            )
        except (ValueError, TypeError, AttributeError):
            continue
        owners[group].add(identity)
        previous = groups[identity].get(group, (0, 0, 0))
        groups[identity][group] = tuple(max(a, b) for a, b in zip(values, previous, strict=True))
    for identity, snapshots in groups.items():
        values = tuple(max(v[index] for v in snapshots.values()) for index in range(3))
        connection.execute(
            text(
                "UPDATE runtime_work SET model_requests=MAX(model_requests,:m), tool_calls=MAX(tool_calls,:t), sent_messages=MAX(sent_messages,:s) WHERE id=:id"
            ),
            dict(id=identity, m=values[0], t=values[1], s=values[2]),
        )
        if len(snapshots) > 1 or any(len(owners[group]) > 1 for group in snapshots):
            connection.execute(
                text(
                    "UPDATE runtime_work SET state='suspended', reason='legacy_budget_ownership_ambiguous', revision=revision+1 WHERE id=:id AND state NOT IN ('completed','failed','cancelled')"
                ),
                dict(id=identity),
            )
    connection.execute(
        text("""UPDATE runtime_work_budgets SET
        models=MAX(models, COALESCE((SELECT SUM(w.model_requests) FROM runtime_work w
            WHERE w.id=root_id OR w.id IN (SELECT work_id FROM runtime_subagents WHERE root_id=runtime_work_budgets.root_id)),0)),
        tools=MAX(tools, COALESCE((SELECT SUM(w.tool_calls) FROM runtime_work w
            WHERE w.id=root_id OR w.id IN (SELECT work_id FROM runtime_subagents WHERE root_id=runtime_work_budgets.root_id)),0))""")
    )


def downgrade() -> None:
    # Runtime rollback cannot discard accepted receipts or reset task budgets.
    pass
