"""Keep existing watches bound to their original single execution."""

from alembic import op

revision = "0150"
down_revision = "0149"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE external_agent_operations
        SET command_json = json_set(command_json, '$.observation_scope', 'execution')
        WHERE action = 'watch'
          AND json_extract(command_json, '$.observation_scope') IS NULL
        """
    )


def downgrade() -> None:
    raise RuntimeError("native observation scope migration is irreversible")
