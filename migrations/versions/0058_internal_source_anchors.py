"""Move internal provenance to ledger/work anchors without replaying work."""

import json

import sqlalchemy as sa
from alembic import op

revision = "0058"
down_revision = "0057"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    for table, columns in (
        (
            "web_search_runs",
            (
                sa.Column("trigger_event_id", sa.Integer()),
                sa.Column("execution_id", sa.String(255)),
            ),
        ),
        ("automations", (sa.Column("creation_source_key", sa.String(255)),)),
    ):
        existing = {c["name"] for c in sa.inspect(bind).get_columns(table)}
        for column in columns:
            if column.name not in existing:
                op.add_column(table, column)
    for table, column in (
        ("web_search_runs", "trigger_event_id"),
        ("automations", "creation_source_key"),
    ):
        name = f"ix_{table}_{column}"
        if name not in {i["name"] for i in sa.inspect(bind).get_indexes(table)}:
            op.create_index(name, table, [column])

    # One-time import of unambiguous, conversation-scoped legacy provenance.
    # Runtime never repeats this platform lookup or grants access to unknown rows.
    bind.execute(
        sa.text("""
        UPDATE web_search_runs SET trigger_event_id = (
            SELECT min(e.id) FROM chat_events e
            WHERE e.canonical_conversation_id = web_search_runs.canonical_conversation_id
              AND e.platform_message_id = web_search_runs.trigger_message_id
              AND (e.suppression_status IS NULL OR e.suppression_status = 'keeper')
            HAVING count(*) = 1
        ) WHERE trigger_event_id IS NULL
        """)
    )
    # Unresolvable legacy web results are a bounded cache, not the chat ledger.
    # Discard them instead of retaining ambiguous authorization/privacy provenance.
    # Alembic's SQLite wrapper disables foreign keys: delete children explicitly.
    bind.execute(
        sa.text(
            "DELETE FROM web_search_sources WHERE run_id IN ("
            "SELECT id FROM web_search_runs WHERE trigger_event_id IS NULL AND execution_id IS NULL)"
        )
    )
    bind.execute(
        sa.text(
            "DELETE FROM web_search_runs WHERE trigger_event_id IS NULL AND execution_id IS NULL"
        )
    )

    # Change only admission/dedup keys. Work ids, journals, budgets and receipts stay intact.
    for row in (
        bind.execute(
            sa.text(
                "SELECT id, conversation_id, source_key, source_json FROM runtime_work "
                "WHERE source_key LIKE 'message:%'"
            )
        )
        .mappings()
        .all()
    ):
        source = json.loads(row["source_json"])
        event_id = source.get("trigger_event_id")
        if type(event_id) is int and event_id > 0:
            bind.execute(
                sa.text("UPDATE runtime_work SET source_key=:key WHERE id=:id"),
                {"key": f"event:{row['conversation_id']}:{event_id}", "id": row["id"]},
            )
    bind.execute(
        sa.text("""
        UPDATE runtime_work_inputs
        SET source_key = 'event:' || conversation_id || ':' || event_id
        WHERE kind = 'message' AND event_id IS NOT NULL AND source_key LIKE 'message:%'
        """)
    )
    for row in bind.execute(sa.text("SELECT id, source_json FROM runtime_work")).mappings().all():
        source = json.loads(row["source_json"])
        if "trigger_id" in source:
            source.pop("trigger_id")
            bind.execute(
                sa.text("UPDATE runtime_work SET source_json=:source WHERE id=:id"),
                {
                    "id": row["id"],
                    "source": json.dumps(
                        source, ensure_ascii=False, sort_keys=True, allow_nan=False
                    ),
                },
            )

    for row in (
        bind.execute(sa.text("SELECT request_id, source_json FROM sandbox_task_runs"))
        .mappings()
        .all()
    ):
        source = json.loads(row["source_json"])
        if "trigger_id" in source:
            previous = source.pop("trigger_id")
            if source.get("origin") == "scheduled_automation":
                source["step_id"] = previous
            bind.execute(
                sa.text("UPDATE sandbox_task_runs SET source_json=:source WHERE request_id=:id"),
                {
                    "id": row["request_id"],
                    "source": json.dumps(
                        source, ensure_ascii=False, sort_keys=True, allow_nan=False
                    ),
                },
            )

    # Delegated automation creation already used a stable run/step key in its old field.
    # User-origin records are imported only with one exact Bot/Person event match.
    for row in (
        bind.execute(
            sa.text(
                "SELECT id, bot_user_id, canonical_creator_person_id, created_from_message_id "
                "FROM automations WHERE creation_source_key IS NULL"
            )
        )
        .mappings()
        .all()
    ):
        old = row["created_from_message_id"]
        key = None
        if old.startswith("auto:"):
            key = f"execution:{old}"
        else:
            matches = (
                bind.execute(
                    sa.text("""
                SELECT id FROM chat_events WHERE platform_message_id=:old
                AND bot_user_id=:bot AND author_person_id=:person
                AND direction='inbound' AND event_kind='message'
                AND (suppression_status IS NULL OR suppression_status='keeper') LIMIT 2
            """),
                    {
                        "old": old,
                        "bot": row["bot_user_id"],
                        "person": row["canonical_creator_person_id"],
                    },
                )
                .scalars()
                .all()
            )
            if len(matches) == 1:
                key = f"event:{matches[0]}"
        if key:
            bind.execute(
                sa.text("UPDATE automations SET creation_source_key=:key WHERE id=:id"),
                {"key": key, "id": row["id"]},
            )


def downgrade() -> None:
    # Additive provenance and original receipts survive rollback; no event replay.
    pass
