"""Keep the local notification ledger event on the original outbox row."""

from alembic import op

revision = "0154"
down_revision = "0153"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    connection.exec_driver_sql(
        "ALTER TABLE plugin_notification_outbox ADD COLUMN event_id INTEGER "
        "REFERENCES chat_events(id) ON DELETE SET NULL"
    )
    connection.exec_driver_sql(
        "CREATE INDEX ix_plugin_notification_outbox_event "
        "ON plugin_notification_outbox (event_id)"
    )


def downgrade() -> None:
    connection = op.get_bind()
    connection.exec_driver_sql("DROP INDEX ix_plugin_notification_outbox_event")
    connection.exec_driver_sql("ALTER TABLE plugin_notification_outbox DROP COLUMN event_id")
