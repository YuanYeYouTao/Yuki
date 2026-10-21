"""Keep existing private history on its primary account; split future entrances."""

import sqlalchemy as sa
from alembic import op

revision = "0120"
down_revision = "0119"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    owners = []
    unresolved = []
    for conversation, person, key in connection.exec_driver_sql(
        "SELECT c.id,c.person_id,a.scope_key FROM canonical_conversations c "
        "LEFT JOIN conversation_legacy_aliases a ON a.id=c.primary_alias_id "
        "WHERE c.kind='private'"
    ):
        parts = key.split(":") if key else []
        if len(parts) == 2 and parts[0] == "binding":
            # A safe schema-only downgrade retains the newer frozen primary key.
            matches = list(
                connection.execute(
                    sa.text(
                        "SELECT id FROM identity_bindings WHERE id=:binding AND person_id=:person"
                    ),
                    {"binding": parts[1], "person": person},
                )
            )
        elif len(parts) == 4 and parts[0] == "bot" and parts[2] == "private":
            matches = list(
                connection.execute(
                    sa.text(
                        "SELECT id FROM identity_bindings WHERE person_id=:person "
                        "AND external_account_id=:account"
                    ),
                    {"person": person, "account": parts[3]},
                )
            )
        else:
            matches = []
        if len(matches) != 1:
            unresolved.append(conversation)
        else:
            owners.append((matches[0][0], conversation))
    if unresolved:
        raise RuntimeError(
            "0120 cannot resolve primary binding for conversations: " + ", ".join(unresolved)
        )
    triggers = list(
        connection.exec_driver_sql(
            "SELECT name,sql FROM sqlite_master WHERE type='trigger' "
            "AND (tbl_name='canonical_conversations' OR sql LIKE '%canonical_conversations%')"
        )
    )
    for name, _ in triggers:
        connection.exec_driver_sql('DROP TRIGGER "' + name.replace('"', '""') + '"')
    op.add_column("canonical_conversations", sa.Column("identity_binding_id", sa.String(36)))
    for binding, conversation in owners:
        connection.execute(
            sa.text("UPDATE canonical_conversations SET identity_binding_id=:binding WHERE id=:id"),
            {"binding": binding, "id": conversation},
        )
    with op.batch_alter_table("canonical_conversations", recreate="always") as batch:
        batch.drop_index("uq_canonical_conversations_person")
        batch.create_index(
            "uq_canonical_conversations_binding",
            ["identity_binding_id"],
            unique=True,
            sqlite_where=sa.text("kind='private'"),
        )
        batch.create_foreign_key(
            "fk_canonical_conversations_binding",
            "identity_bindings",
            ["identity_binding_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch.drop_constraint("ck_canonical_conversations_owner_xor", type_="check")
        batch.create_check_constraint(
            "ck_canonical_conversations_owner_xor",
            "(kind='private' AND person_id IS NOT NULL AND space_id IS NULL "
            "AND identity_binding_id IS NOT NULL) OR "
            "(kind='space' AND person_id IS NULL AND space_id IS NOT NULL "
            "AND identity_binding_id IS NULL)",
        )
    for _, sql in triggers:
        connection.exec_driver_sql(sql)


def downgrade() -> None:
    connection = op.get_bind()
    split = list(
        connection.exec_driver_sql(
            "SELECT person_id FROM canonical_conversations WHERE kind='private' "
            "GROUP BY person_id HAVING count(*)>1"
        )
    )
    if split:
        raise RuntimeError(
            "Independent entrance histories cannot be merged back: "
            + ", ".join(row[0] for row in split)
        )
    triggers = list(
        connection.exec_driver_sql(
            "SELECT name,sql FROM sqlite_master WHERE type='trigger' "
            "AND (tbl_name='canonical_conversations' OR sql LIKE '%canonical_conversations%')"
        )
    )
    for name, _ in triggers:
        connection.exec_driver_sql('DROP TRIGGER "' + name.replace('"', '""') + '"')
    with op.batch_alter_table("canonical_conversations", recreate="always") as batch:
        batch.drop_index("uq_canonical_conversations_binding")
        batch.drop_constraint("fk_canonical_conversations_binding", type_="foreignkey")
        batch.drop_constraint("ck_canonical_conversations_owner_xor", type_="check")
        batch.drop_column("identity_binding_id")
        batch.create_index(
            "uq_canonical_conversations_person",
            ["person_id"],
            unique=True,
            sqlite_where=sa.text("kind='private'"),
        )
        batch.create_check_constraint(
            "ck_canonical_conversations_owner_xor",
            "(kind='private' AND person_id IS NOT NULL AND space_id IS NULL) OR "
            "(kind='space' AND person_id IS NULL AND space_id IS NOT NULL)",
        )
    for _, sql in triggers:
        connection.exec_driver_sql(sql)
