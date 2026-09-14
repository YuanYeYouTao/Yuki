"""Durable reflection cycles, source retries and request accounting."""

import sqlalchemy as sa
from alembic import op

revision = "0061"
down_revision = "0060"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from qq_ai_bot.persistence.models import (
        MemorySelfReflectionCycleModel,
        MemorySelfReflectionRequestModel,
    )

    for model in (MemorySelfReflectionCycleModel, MemorySelfReflectionRequestModel):
        model.__table__.create(op.get_bind(), checkfirst=True)
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
        op.add_column(
            "memory_self_reflection_runs",
            sa.Column(name, kind, nullable=default is None, server_default=default),
        )
    op.add_column(
        "memory_self_reflection_states",
        sa.Column("last_policy_reason", sa.String(32), nullable=True),
    )
    op.add_column(
        "memory_self_reflection_states",
        sa.Column("last_policy_event_id", sa.Integer(), nullable=True),
    )
    op.add_column(
        "memory_self_reflection_runtime",
        sa.Column("ingress_events_total", sa.Integer(), nullable=False, server_default="0"),
    )
    # Preserve already recorded provider usage; legacy unrecorded transport retries remain unknown.
    op.execute("""INSERT INTO memory_self_reflection_requests(run_id, local_date, created_at, status, output_tokens)
        SELECT 0, date(created_at, '+8 hours'), created_at,
            CASE WHEN success THEN 'completed' ELSE 'failed' END, completion_tokens
        FROM model_invocations WHERE task='memory_self_reflection'""")
    # Source-range metadata only: no event, fact, ownership or cursor is modified.
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
    op.create_index(
        "ix_memory_self_reflection_runs_cycle_id", "memory_self_reflection_runs", ["cycle_id"]
    )


def downgrade() -> None:
    # Preserve budgets, source batches and committed receipts on code rollback.
    pass
