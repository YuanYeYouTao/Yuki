"""Explicit connector grants and native resource/operation receipts."""

import sqlalchemy as sa
from alembic import op

revision = "0148"
down_revision = "0147"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in ("client_devices", "client_link_codes"):
        op.add_column(
            table,
            sa.Column("purpose", sa.String(16), nullable=False, server_default="chat"),
        )
    op.execute(
        """
        CREATE TABLE external_agent_instances (
            id VARCHAR(36) NOT NULL,
            device_id VARCHAR(36) NOT NULL,
            local_id TEXT NOT NULL,
            catalog_json TEXT NOT NULL,
            PRIMARY KEY (id),
            UNIQUE (device_id, local_id),
            FOREIGN KEY(device_id) REFERENCES client_devices (id) ON DELETE CASCADE
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_external_agent_instances_device_id ON external_agent_instances (device_id)"
    )
    op.execute(
        """
        CREATE TABLE external_agent_sessions (
            id VARCHAR(36) NOT NULL,
            instance_id VARCHAR(36) NOT NULL,
            native_id TEXT NOT NULL,
            PRIMARY KEY (id),
            UNIQUE (instance_id, native_id),
            FOREIGN KEY(instance_id) REFERENCES external_agent_instances (id) ON DELETE CASCADE
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_external_agent_sessions_instance_id ON external_agent_sessions (instance_id)"
    )
    op.execute(
        """
        CREATE TABLE external_agent_executions (
            id VARCHAR(36) NOT NULL,
            session_id VARCHAR(36) NOT NULL,
            native_id TEXT NOT NULL,
            PRIMARY KEY (id),
            UNIQUE (session_id, native_id),
            FOREIGN KEY(session_id) REFERENCES external_agent_sessions (id) ON DELETE CASCADE
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_external_agent_executions_session_id ON external_agent_executions (session_id)"
    )
    op.execute(
        """
        CREATE TABLE external_agent_operations (
            id VARCHAR(256) NOT NULL,
            instance_id VARCHAR(36) NOT NULL,
            session_id VARCHAR(36),
            actor_person_id VARCHAR(36) NOT NULL,
            work_id VARCHAR(36),
            source_json TEXT NOT NULL,
            action VARCHAR(16) NOT NULL,
            payload_hash VARCHAR(64) NOT NULL,
            command_json TEXT NOT NULL,
            run_id VARCHAR(36),
            native_execution_id TEXT,
            result_json TEXT,
            created_at DATETIME NOT NULL,
            dispatched_at DATETIME,
            detached_at DATETIME,
            PRIMARY KEY (id),
            FOREIGN KEY(instance_id) REFERENCES external_agent_instances (id) ON DELETE CASCADE,
            FOREIGN KEY(session_id) REFERENCES external_agent_sessions (id) ON DELETE CASCADE,
            FOREIGN KEY(actor_person_id) REFERENCES persons (id) ON DELETE CASCADE,
            UNIQUE (run_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_external_agent_operations_session ON external_agent_operations (session_id, action)"
    )
    op.execute(
        "CREATE INDEX ix_external_agent_operations_work ON external_agent_operations (work_id)"
    )
    op.execute(
        """
        CREATE TABLE external_agent_events (
            id INTEGER NOT NULL,
            device_id VARCHAR(36) NOT NULL,
            event_id TEXT NOT NULL,
            operation_id VARCHAR(256),
            kind VARCHAR(32) NOT NULL,
            payload_json TEXT NOT NULL,
            created_at DATETIME NOT NULL,
            PRIMARY KEY (id),
            UNIQUE (device_id, event_id),
            FOREIGN KEY(device_id) REFERENCES client_devices (id) ON DELETE CASCADE,
            FOREIGN KEY(operation_id) REFERENCES external_agent_operations (id) ON DELETE CASCADE
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_external_agent_events_operation ON external_agent_events (operation_id, id)"
    )


def downgrade() -> None:
    raise RuntimeError("Native grants and original execution receipts are retained.")
