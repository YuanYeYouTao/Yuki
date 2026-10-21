"""Person names and current same-human ownership, preserving original sources."""

import sqlalchemy as sa
from alembic import op

revision = "0129"
down_revision = "0128"
branch_labels = None
depends_on = None

_ROUTE_SQL = (
    """
CREATE TRIGGER trg_person_active_routes_consistency_insert
BEFORE INSERT ON person_active_routes
BEGIN
    SELECT RAISE(ABORT, 'person active route ownership or platform mismatch')
    WHERE NOT EXISTS (
        SELECT 1
        FROM identity_bindings AS binding
        JOIN presences AS presence ON presence.id = NEW.presence_id
        JOIN persons AS owner ON owner.id = binding.person_id
        JOIN persons AS target ON target.id = NEW.person_id
        WHERE binding.id = NEW.identity_binding_id
          AND COALESCE(owner.merged_into_person_id, owner.id)
              = COALESCE(target.merged_into_person_id, target.id)
          AND binding.platform = presence.platform
    );
END
""",
    """
CREATE TRIGGER trg_person_active_routes_consistency_update
BEFORE UPDATE ON person_active_routes
BEGIN
    SELECT RAISE(ABORT, 'person active route ownership or platform mismatch')
    WHERE NOT EXISTS (
        SELECT 1
        FROM identity_bindings AS binding
        JOIN presences AS presence ON presence.id = NEW.presence_id
        JOIN persons AS owner ON owner.id = binding.person_id
        JOIN persons AS target ON target.id = NEW.person_id
        WHERE binding.id = NEW.identity_binding_id
          AND COALESCE(owner.merged_into_person_id, owner.id)
              = COALESCE(target.merged_into_person_id, target.id)
          AND binding.platform = presence.platform
    );
END
""",
    """
CREATE TRIGGER trg_identity_bindings_route_consistency_update
BEFORE UPDATE OF person_id, platform ON identity_bindings
BEGIN
    SELECT RAISE(ABORT, 'identity binding update would break person active route')
    WHERE EXISTS (
        SELECT 1
        FROM person_active_routes AS route
        JOIN presences AS presence ON presence.id = route.presence_id
        JOIN persons AS owner ON owner.id = NEW.person_id
        JOIN persons AS target ON target.id = route.person_id
        WHERE route.identity_binding_id = NEW.id
          AND (
            COALESCE(target.merged_into_person_id, target.id)
                IS NOT COALESCE(owner.merged_into_person_id, owner.id)
            OR NEW.platform IS NOT presence.platform
          )
    );
END
""",
)


def upgrade() -> None:
    with op.batch_alter_table(
        "persons",
        naming_convention={"fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s"},
    ) as batch:
        batch.add_column(
            sa.Column("display_name", sa.String(128), nullable=False, server_default="")
        )
        batch.add_column(sa.Column("merged_into_person_id", sa.String(36), nullable=True))
        batch.create_foreign_key(
            "fk_persons_merged_into_person_id_persons",
            "persons",
            ["merged_into_person_id"],
            ["id"],
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        )
        batch.create_index("ix_persons_merged_into_person_id", ["merged_into_person_id"])
    op.execute("""UPDATE persons SET display_name = COALESCE((
        SELECT alias FROM person_aliases
        WHERE canonical_person_id=persons.id AND canonical_space_id IS NULL
          AND alias_type='nickname' AND alias <> ''
        ORDER BY last_seen_at DESC, id DESC LIMIT 1), '')""")
    op.create_index(
        "ix_conversations_person",
        "canonical_conversations",
        ["person_id"],
        sqlite_where=sa.text("person_id IS NOT NULL"),
    )
    with op.batch_alter_table(
        "social_operation_receipts",
        naming_convention={"fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s"},
    ) as batch:
        batch.drop_constraint(
            "fk_social_operation_receipts_source_conversation_id_canonical_conversations",
            type_="foreignkey",
        )
    with op.batch_alter_table("control_command_receipts") as batch:
        batch.drop_constraint("uq_control_command_receipts_principal_request", type_="unique")
        batch.add_column(sa.Column("request_person_id", sa.String(36), nullable=True))
        batch.create_index(
            "uq_control_command_receipts_principal_request",
            ["principal_id", "request_id"],
            unique=True,
            sqlite_where=sa.text("request_person_id IS NULL"),
        )
        batch.create_index(
            "uq_control_command_receipts_person_request",
            ["request_person_id", "request_id"],
            unique=True,
            sqlite_where=sa.text("request_person_id IS NOT NULL"),
        )
    for name in (
        "trg_person_active_routes_consistency_insert",
        "trg_person_active_routes_consistency_update",
        "trg_identity_bindings_route_consistency_update",
    ):
        op.execute(f"DROP TRIGGER {name}")
    for statement in _ROUTE_SQL:
        op.execute(statement)


def downgrade() -> None:
    raise RuntimeError("Person association and original send receipts cannot be discarded")
