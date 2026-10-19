"""Person-owned local executors, grants, and original operation receipts."""

from alembic import op

revision = "0146"
down_revision = "0145"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE client_executors (
        id VARCHAR(36) PRIMARY KEY NOT NULL,
        person_id VARCHAR(36) NOT NULL REFERENCES persons(id) ON DELETE CASCADE,
        token_hash VARCHAR(64) NOT NULL UNIQUE,
        label VARCHAR(128) NOT NULL, platform VARCHAR(64) NOT NULL,
        revision INTEGER NOT NULL, paused BOOLEAN NOT NULL,
        created_at DATETIME NOT NULL, last_seen_at DATETIME, revoked_at DATETIME
    )""")
    op.create_index("ix_client_executors_person", "client_executors", ["person_id"])
    op.execute("""CREATE TABLE executor_grants (
        executor_id VARCHAR(36) NOT NULL REFERENCES client_executors(id) ON DELETE CASCADE,
        capability VARCHAR NOT NULL, scope_alias VARCHAR NOT NULL,
        scope_digest VARCHAR NOT NULL, revision INTEGER NOT NULL,
        PRIMARY KEY (executor_id, capability, scope_alias)
    )""")
    op.execute("""CREATE TABLE executor_operations (
        op_id VARCHAR(36) PRIMARY KEY NOT NULL,
        executor_id VARCHAR(36) REFERENCES client_executors(id) ON DELETE SET NULL,
        person_id VARCHAR(36) REFERENCES persons(id) ON DELETE SET NULL,
        work_id VARCHAR, tool_call_key VARCHAR NOT NULL UNIQUE,
        capability VARCHAR, params_json TEXT, state VARCHAR NOT NULL,
        level VARCHAR, result_json TEXT, observation_json TEXT,
        receipt_hash VARCHAR(64) NOT NULL, created_at DATETIME,
        dispatched_at DATETIME, acknowledged_at DATETIME, settled_at DATETIME,
        CONSTRAINT ck_executor_operation_state CHECK (state IN (
            'registered','dispatched','acknowledged','awaiting_user','succeeded','failed','unknown'))
    )""")
    op.create_index(
        "ix_executor_operations_history",
        "executor_operations",
        ["executor_id", "created_at", "op_id"],
    )


def downgrade() -> None:
    op.drop_table("executor_operations")
    op.drop_table("executor_grants")
    op.drop_table("client_executors")
