"""Durable activation exits and independently recoverable delivery intents."""

import sqlalchemy as sa

from qq_ai_bot.persistence.models import Base

recovery = sa.Table(
    "runtime_work_recovery",
    Base.metadata,
    sa.Column("work_id", sa.ForeignKey("runtime_work.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("activation_id", sa.String(36), nullable=False),
    sa.Column("exit_reason", sa.String(64), nullable=False, server_default="running"),
    sa.Column("stage", sa.String(32), nullable=False, server_default="activation"),
    sa.Column("failure_json", sa.Text, nullable=False, server_default="{}"),
    sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
    sa.Column("not_before", sa.Float, nullable=False, server_default="0"),
    sa.Column("updated", sa.Float, nullable=False),
    sa.Index("ix_work_recovery_ready", "not_before", "work_id"),
)

deliveries = sa.Table(
    "runtime_delivery_intents",
    Base.metadata,
    sa.Column("id", sa.String(256), primary_key=True),
    sa.Column("work_id", sa.ForeignKey("runtime_work.id", ondelete="CASCADE"), nullable=False),
    sa.Column("kind", sa.String(24), nullable=False),
    sa.Column("target_key", sa.String(256), nullable=False, server_default=""),
    sa.Column("message_count", sa.Integer, nullable=False, server_default="1"),
    sa.Column("state", sa.String(24), nullable=False),
    sa.Column("payload_json", sa.Text, nullable=False, server_default="{}"),
    sa.Column("receipt_json", sa.Text, nullable=False, server_default="{}"),
    sa.Column("not_before", sa.Float, nullable=False, server_default="0"),
    sa.Column("created", sa.Float, nullable=False),
    sa.Column("updated", sa.Float, nullable=False),
    sa.Index("ix_delivery_work_state", "work_id", "state"),
    sa.Index("ix_delivery_target_window", "target_key", "created", "state"),
)

invocations = sa.Table(
    "runtime_automation_cursors",
    Base.metadata,
    sa.Column("run_id", sa.ForeignKey("automation_runs.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("script_hash", sa.String(64), nullable=False),
    sa.Column("phase", sa.String(24), nullable=False),
    sa.Column("payload_json", sa.Text, nullable=False),
    sa.Column("updated", sa.Float, nullable=False),
)

quota = sa.Table(
    "runtime_checkpoint_quota",
    Base.metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("bytes", sa.Integer, nullable=False),
    sa.CheckConstraint(
        "id = 1 AND bytes >= 0 AND bytes <= 67108864", name="ck_runtime_checkpoint_bytes"
    ),
)


def install_quota(connection: sa.Connection) -> None:
    """Triggers account deltas for every writer, including archival and migrations."""
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_runtime_media_refs_sha ON runtime_work_media_refs(sha256)"
    )
    connection.exec_driver_sql(
        "INSERT OR IGNORE INTO runtime_checkpoint_quota(id, bytes) SELECT 1, "
        "COALESCE((SELECT SUM(length(CAST(payload_json AS BLOB))) FROM runtime_work_journal),0) + "
        "COALESCE((SELECT SUM(length(content)) FROM runtime_work_media),0)"
    )
    for statement in quota_trigger_sql().values():
        connection.exec_driver_sql(statement)


def quota_trigger_sql() -> dict[str, str]:
    result = {}
    for table, column in (
        ("runtime_work_journal", "payload_json"),
        ("runtime_work_media", "content"),
    ):
        for event, delta in (
            ("INSERT", f"length(CAST(NEW.{column} AS BLOB))"),
            ("DELETE", f"-length(CAST(OLD.{column} AS BLOB))"),
            ("UPDATE", f"length(CAST(NEW.{column} AS BLOB))-length(CAST(OLD.{column} AS BLOB))"),
        ):
            name = f"quota_{table}_{event.lower()}"
            result[name] = (
                f"CREATE TRIGGER IF NOT EXISTS {name} "
                f"AFTER {event} ON {table} "
                f"BEGIN UPDATE runtime_checkpoint_quota SET bytes=bytes+({delta}) WHERE id=1; END"
            )
    return result
