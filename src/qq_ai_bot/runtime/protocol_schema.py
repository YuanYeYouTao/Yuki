"""Ownership and deletion fences for private Work protocol files."""

import sqlalchemy as sa

from qq_ai_bot.runtime.work_schema_v1 import work

objects = sa.Table(
    "runtime_protocol_objects",
    work.metadata,
    sa.Column("sha256", sa.String(64), primary_key=True),
    sa.Column("byte_size", sa.Integer, nullable=False),
    sa.Column("prepared_at", sa.Float, nullable=False),
    sa.Column("deleting", sa.Boolean, nullable=False, server_default="0"),
    sa.CheckConstraint("byte_size >= 0", name="ck_protocol_object_bytes"),
    sa.Index("ix_protocol_objects_gc", "deleting", "prepared_at"),
    sa.Index("ix_protocol_objects_gc_cursor", "deleting", "prepared_at", "sha256"),
)
refs = sa.Table(
    "runtime_protocol_refs",
    work.metadata,
    sa.Column("work_id", sa.ForeignKey("runtime_work.id", ondelete="CASCADE"), primary_key=True),
    sa.Column(
        "sha256",
        sa.ForeignKey("runtime_protocol_objects.sha256", ondelete="RESTRICT"),
        primary_key=True,
    ),
    sa.Index("ix_protocol_refs_digest", "sha256"),
)

usage = sa.Table(
    "runtime_protocol_usage",
    work.metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("byte_size", sa.Integer, nullable=False, server_default="0"),
    sa.CheckConstraint("id = 1 AND byte_size >= 0", name="ck_protocol_usage_singleton"),
)

QUOTA_SQL = (
    "INSERT OR IGNORE INTO runtime_protocol_usage (id, byte_size) VALUES (1, 0)",
    """CREATE TRIGGER IF NOT EXISTS protocol_object_insert AFTER INSERT ON runtime_protocol_objects
    BEGIN UPDATE runtime_protocol_usage SET byte_size=byte_size+NEW.byte_size WHERE id=1; END""",
    """CREATE TRIGGER IF NOT EXISTS protocol_object_delete AFTER DELETE ON runtime_protocol_objects
    BEGIN UPDATE runtime_protocol_usage SET byte_size=byte_size-OLD.byte_size WHERE id=1; END""",
)


def install_quota(connection: sa.Connection) -> None:
    for statement in QUOTA_SQL:
        connection.exec_driver_sql(statement)
