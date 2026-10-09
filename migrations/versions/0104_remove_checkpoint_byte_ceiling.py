"""Remove retired Work quotas, delivery fields and opportunity occupancy."""

from alembic import op

revision = "0104"
down_revision = "0103"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    for table in ("runtime_work_journal", "runtime_work_media"):
        for event in ("insert", "delete", "update"):
            connection.exec_driver_sql(f"DROP TRIGGER quota_{table}_{event}")
    op.drop_table("runtime_checkpoint_quota")
    op.drop_index("ix_delivery_target_window", table_name="runtime_delivery_intents")
    op.drop_column("runtime_delivery_intents", "target_key")
    op.drop_column("runtime_delivery_intents", "not_before")
    op.drop_index("uq_autonomy_runs_one_active", table_name="autonomy_initiative_runs")
    op.drop_index("ix_runtime_work_query_scope_updated", table_name="runtime_work")
    connection.exec_driver_sql(
        "CREATE INDEX ix_runtime_work_query_scope_updated ON runtime_work "
        "(conversation_id, generation, json_extract(source_json, '$.actor_person_id'), "
        "json_extract(source_json, '$.origin'), json_extract(source_json, '$.plugin_id'), "
        "json_extract(source_json, '$.delegation_id'), "
        "json_extract(source_json, '$.execution_boundary'), "
        "json_extract(source_json, '$.principal_kind'), "
        "json_extract(source_json, '$.initiative_run_id'), updated DESC, id DESC)"
    )
    with op.batch_alter_table("automation_runs") as batch:
        batch.drop_constraint("ck_automation_runs_status", type_="check")
        batch.create_check_constraint(
            "ck_automation_runs_status",
            "status IN ('running', 'succeeded', 'failed', 'cancelled', 'missed', 'uncertain', 'blocked')",
        )


def downgrade() -> None:
    raise RuntimeError("Retired Work policy is not restored")
