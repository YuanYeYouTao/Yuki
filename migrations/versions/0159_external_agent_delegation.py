"""Delegation executor facts and session-addressable external-agent events.

``runtime_subagents.executor_json`` is null for Yuki's own workers; when set, the
child Work is a delegation or observation record of a native agent on a device
(no Yuki model loop). Events gain the internal instance and native session they
were received for, backfilled once here from the stored original payload, so
session-level reads (unowned waits, observation timelines) use an index instead
of scanning JSON.
"""

from alembic import op

revision = "0159"
down_revision = "0158"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE runtime_subagents ADD COLUMN executor_json TEXT")
    op.execute("ALTER TABLE external_agent_events ADD COLUMN instance_id VARCHAR(36)")
    op.execute("ALTER TABLE external_agent_events ADD COLUMN native_session_id TEXT")
    op.execute(
        "UPDATE external_agent_events SET "
        "instance_id = (SELECT i.id FROM external_agent_instances AS i "
        "WHERE i.device_id = external_agent_events.device_id "
        "AND i.local_id = json_extract(external_agent_events.payload_json, '$.instance_id')), "
        "native_session_id = json_extract(payload_json, '$.session_id')"
    )
    op.execute(
        "CREATE INDEX ix_external_agent_events_session ON external_agent_events "
        "(instance_id, native_session_id, kind, id)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_external_agent_events_session")
    op.execute("ALTER TABLE external_agent_events DROP COLUMN native_session_id")
    op.execute("ALTER TABLE external_agent_events DROP COLUMN instance_id")
    op.execute("ALTER TABLE runtime_subagents DROP COLUMN executor_json")
