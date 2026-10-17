"""Personal devices and one-time codes; retain prior owners on transferred bindings."""

from datetime import UTC, datetime
from uuid import uuid4

import sqlalchemy as sa
from alembic import op

revision = "0121"
down_revision = "0120"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    triggers = list(
        connection.exec_driver_sql(
            "SELECT name,sql FROM sqlite_master WHERE type='trigger' "
            "AND (tbl_name='identity_bindings' OR sql LIKE '%identity_bindings%')"
        )
    )
    for name, _ in triggers:
        connection.exec_driver_sql('DROP TRIGGER "' + name.replace('"', '""') + '"')
    with op.batch_alter_table("identity_bindings", recreate="always") as batch:
        batch.drop_constraint("uq_identity_bindings_platform_account", type_="unique")
        batch.drop_constraint("ck_identity_bindings_status", type_="check")
        batch.add_column(sa.Column("history_starts_at", sa.DateTime(timezone=True), nullable=True))
        batch.create_check_constraint(
            "ck_identity_bindings_status", "status IN ('active','disabled','transferred')"
        )
        batch.create_index(
            "uq_identity_bindings_platform_account",
            ["platform", "external_account_id"],
            unique=True,
            sqlite_where=sa.text("status <> 'transferred'"),
        )
        batch.create_index(
            "uq_identity_bindings_app_person",
            ["person_id"],
            unique=True,
            sqlite_where=sa.text("platform='app' AND status <> 'transferred'"),
        )
    for _, sql in triggers:
        connection.exec_driver_sql(sql)
    op.create_table(
        "client_devices",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "person_id",
            sa.String(36),
            sa.ForeignKey("persons.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("token_hash", sa.String(64), unique=True, nullable=False),
        sa.Column("label", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_client_devices_person", "client_devices", ["person_id"])
    op.create_table(
        "client_link_codes",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("code_hash", sa.String(64), unique=True, nullable=False),
        sa.Column(
            "person_id",
            sa.String(36),
            sa.ForeignKey("persons.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("issuer_principal_id", sa.String(36), nullable=False),
        sa.Column("issuer_source", sa.String(32), nullable=False),
        sa.Column("source_binding_id", sa.String(36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_client_link_codes_expiry", "client_link_codes", ["expires_at"])
    now = datetime.now(UTC)
    connection.execute(
        sa.text(
            "INSERT INTO presences(id,platform,external_account_id,enabled,ingest_eligible,revision,"
            "created_at,updated_at) VALUES(:id,'app','yuki-app',1,0,1,:now,:now)"
        ),
        {"id": str(uuid4()), "now": now},
    )


def downgrade() -> None:
    raise RuntimeError("Personal credential and ownership history require a forward migration")
