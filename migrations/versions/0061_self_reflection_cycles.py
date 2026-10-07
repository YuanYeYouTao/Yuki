"""Durable reflection cycles, source retries and request accounting."""

import sqlalchemy as sa
from alembic import op

revision = "0061"
down_revision = "0060"
branch_labels = None
depends_on = None


_TABLES = (
    '\nCREATE TABLE IF NOT EXISTS memory_self_reflection_cycles (\n\tid VARCHAR(64) NOT NULL, \n\tsource_key VARCHAR(128) NOT NULL, \n\t"trigger" VARCHAR(16) NOT NULL, \n\tsource_event_id INTEGER, \n\tconversation_id VARCHAR(36), \n\tstatus VARCHAR(24) NOT NULL, \n\tcreated_at DATETIME NOT NULL, \n\tstarted_at DATETIME, \n\tcompleted_at DATETIME, \n\treport_json TEXT NOT NULL, \n\tdelivery_state VARCHAR(24) NOT NULL, \n\tdelivery_receipt_json TEXT, \n\tPRIMARY KEY (id), \n\tUNIQUE (source_key)\n)\n\n',
    "\nCREATE TABLE IF NOT EXISTS memory_self_reflection_requests (\n\tid INTEGER NOT NULL, \n\tattempt_kind VARCHAR(24) DEFAULT 'initial' NOT NULL, \n\trun_id INTEGER NOT NULL, \n\tlocal_date VARCHAR(10) NOT NULL, \n\tcreated_at DATETIME NOT NULL, \n\tstatus VARCHAR(24) NOT NULL, \n\toutput_tokens INTEGER, \n\tPRIMARY KEY (id)\n)\n\n",
    "CREATE INDEX IF NOT EXISTS ix_memory_self_reflection_requests_local_date ON memory_self_reflection_requests (local_date)",
    "CREATE INDEX IF NOT EXISTS ix_memory_self_reflection_requests_run_id ON memory_self_reflection_requests (run_id)",
)


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    request_table_is_new = not inspector.has_table("memory_self_reflection_requests")
    run_columns = {c["name"] for c in inspector.get_columns("memory_self_reflection_runs")}
    state_columns = {c["name"] for c in inspector.get_columns("memory_self_reflection_states")}
    runtime_columns = {c["name"] for c in inspector.get_columns("memory_self_reflection_runtime")}
    for statement in _TABLES:
        op.execute(statement)
    for name, kind, default in (
        ("cycle_id", sa.String(64), None),
        ("attempt_count", sa.Integer(), "0"),
        ("retry_state", sa.String(24), None),
        ("next_attempt_at", sa.DateTime(timezone=True), None),
        ("first_failed_at", sa.DateTime(timezone=True), None),
        ("input_fingerprint", sa.String(64), None),
        ("processed_events", sa.Integer(), "0"),
        ("processed_characters", sa.Integer(), "0"),
        ("checkpoint_json", sa.Text(), None),
    ):
        if name in run_columns:
            continue
        op.add_column(
            "memory_self_reflection_runs",
            sa.Column(name, kind, nullable=default is None, server_default=default),
        )
    if "last_policy_reason" not in state_columns:
        op.add_column(
            "memory_self_reflection_states",
            sa.Column("last_policy_reason", sa.String(32), nullable=True),
        )
    if "last_policy_event_id" not in state_columns:
        op.add_column(
            "memory_self_reflection_states",
            sa.Column("last_policy_event_id", sa.Integer(), nullable=True),
        )
    if "ingress_events_total" not in runtime_columns:
        op.add_column(
            "memory_self_reflection_runtime",
            sa.Column("ingress_events_total", sa.Integer(), nullable=False, server_default="0"),
        )
    # Preserve already recorded provider usage; legacy unrecorded transport retries remain unknown.
    if request_table_is_new:
        op.execute("""INSERT INTO memory_self_reflection_requests(run_id, local_date, created_at, status, output_tokens)
        SELECT 0, date(created_at, '+8 hours'), created_at,
            CASE WHEN success THEN 'completed' ELSE 'failed' END, completion_tokens
        FROM model_invocations WHERE task='memory_self_reflection'""")
    # Source-range metadata only: no event, fact, ownership or cursor is modified.
    if "processed_events" not in run_columns:
        op.execute("""UPDATE memory_self_reflection_runs AS r
        SET processed_events = (SELECT count(*) FROM chat_events e
            JOIN canonical_conversations c ON c.id=e.canonical_conversation_id
            WHERE e.id BETWEEN r.first_event_id AND r.last_event_id
            AND (c.person_id=r.canonical_person_id OR c.space_id=r.canonical_space_id)),
            processed_characters = (SELECT coalesce(sum(length(e.content)), 0) FROM chat_events e
            JOIN canonical_conversations c ON c.id=e.canonical_conversation_id
            WHERE e.id BETWEEN r.first_event_id AND r.last_event_id
            AND (c.person_id=r.canonical_person_id OR c.space_id=r.canonical_space_id))
        WHERE r.status != 'completed'""")
    if "ix_memory_self_reflection_runs_cycle_id" not in {
        index["name"] for index in inspector.get_indexes("memory_self_reflection_runs")
    }:
        op.create_index(
            "ix_memory_self_reflection_runs_cycle_id", "memory_self_reflection_runs", ["cycle_id"]
        )


def downgrade() -> None:
    # Preserve budgets, source batches and committed receipts on code rollback.
    pass
