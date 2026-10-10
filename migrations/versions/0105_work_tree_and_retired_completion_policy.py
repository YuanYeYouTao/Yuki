"""One Work parent relation and removal of retired completion policy."""

from alembic import op

revision = "0105"
down_revision = "0104"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    connection.exec_driver_sql(
        "ALTER TABLE runtime_work ADD COLUMN parent_work_id VARCHAR(36) "
        "REFERENCES runtime_work(id) ON DELETE RESTRICT"
    )
    connection.exec_driver_sql(
        "UPDATE runtime_work SET parent_work_id = "
        "(SELECT root_id FROM runtime_subagents WHERE work_id = runtime_work.id) "
        "WHERE id IN (SELECT work_id FROM runtime_subagents)"
    )
    connection.exec_driver_sql(
        "CREATE INDEX ix_runtime_work_parent ON runtime_work(parent_work_id)"
    )
    op.drop_index("ix_runtime_subagents_root", table_name="runtime_subagents")
    op.drop_index("ix_work_children_root", table_name="runtime_subagents")
    with op.batch_alter_table(
        "runtime_subagents",
        naming_convention={"fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s"},
    ) as batch:
        batch.drop_constraint("fk_runtime_subagents_root_id_runtime_work", type_="foreignkey")
        batch.drop_column("root_id")
    op.drop_column("runtime_work", "output_kind")
    op.drop_column("runtime_work", "deliver_artifacts")
    connection.exec_driver_sql(
        "UPDATE runtime_work SET checkpoint_json = json_remove(checkpoint_json, "
        "'$.communication.input_feedback_through_id', '$.communication.stage_feedback_batch') "
        "WHERE json_type(checkpoint_json, '$.communication.input_feedback_through_id') IS NOT NULL "
        "OR json_type(checkpoint_json, '$.communication.stage_feedback_batch') IS NOT NULL"
    )
    connection.exec_driver_sql(
        "DELETE FROM runtime_delivery_intents WHERE kind = 'notice' "
        "AND state IN ('planned', 'blocked')"
    )


def downgrade() -> None:
    raise RuntimeError("Retired completion policy and duplicate Work relations are not restored")
