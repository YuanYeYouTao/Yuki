"""Keep original publication handles after temporary media artifact cleanup."""

from alembic import op

revision = "0128"
down_revision = "0127"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table(
        "plugin_notification_outbox",
        naming_convention={"fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s"},
    ) as batch:
        batch.drop_constraint(
            "fk_plugin_notification_outbox_media_handle_id_plugin_media_artifacts",
            type_="foreignkey",
        )


def downgrade() -> None:
    raise RuntimeError("Original publication handles may outlive their media artifacts")
