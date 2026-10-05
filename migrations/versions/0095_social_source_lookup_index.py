"""Index the original global Person inbound-source existence check."""

import sqlalchemy as sa
from alembic import op

revision = "0095"
down_revision = "0094"
branch_labels = None
depends_on = None

# Frozen ownership: preserve the existing permission predicate and manage only this index.
INDEX_NAME = "ix_chat_events_social_source_author"
TABLE = "chat_events"
INDEX_SQL = (
    f"CREATE INDEX {INDEX_NAME} ON {TABLE} (author_person_id, id) "
    "WHERE direction = 'inbound' AND author_kind = 'person'"
)


def _normalized(sql: str) -> str:
    result: list[str] = []
    position = 0
    literal = False
    while position < len(sql):
        character = sql[position]
        if character == "'":
            result.append(character)
            if literal and position + 1 < len(sql) and sql[position + 1] == "'":
                result.append("'")
                position += 2
                continue
            literal = not literal
        elif literal or (character != '"' and not character.isspace()):
            result.append(character)
        position += 1
    return "".join(result)


def _validate(*, required: bool) -> bool:
    bind = op.get_bind()
    target = bind.execute(
        sa.text("SELECT type FROM sqlite_master WHERE name=:name"), {"name": TABLE}
    ).scalar()
    columns = {row[1] for row in bind.exec_driver_sql(f'PRAGMA table_info("{TABLE}")')}
    if target != "table" or not {"id", "author_person_id", "direction", "author_kind"} <= columns:
        raise RuntimeError("social source index table shape mismatch")
    existing = bind.execute(
        sa.text("SELECT type,tbl_name,sql FROM sqlite_master WHERE name=:name"),
        {"name": INDEX_NAME},
    ).first()
    if existing is None:
        if required:
            raise RuntimeError("social source index missing")
        return False
    if existing[0] != "index" or existing[1] != TABLE or existing[2] is None:
        raise RuntimeError("social source index shape mismatch")
    shape = next(
        (
            row
            for row in bind.exec_driver_sql(f'PRAGMA index_list("{TABLE}")')
            if row[1] == INDEX_NAME
        ),
        None,
    )
    keys = tuple(
        (row[2], row[3], row[4])
        for row in bind.exec_driver_sql(f'PRAGMA index_xinfo("{INDEX_NAME}")')
        if row[5] == 1
    )
    if (
        shape is None
        or shape[2] != 0
        or shape[3] != "c"
        or shape[4] != 1
        or keys != (("author_person_id", 0, "BINARY"), ("id", 0, "BINARY"))
        or _normalized(existing[2]) != _normalized(INDEX_SQL)
    ):
        raise RuntimeError("social source index shape mismatch")
    return True


def upgrade() -> None:
    if not _validate(required=False):
        op.execute(sa.text(INDEX_SQL))


def downgrade() -> None:
    _validate(required=True)
    op.drop_index(INDEX_NAME, table_name=TABLE)
