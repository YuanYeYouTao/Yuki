"""Link each plugin background Job to its exact original Work.

A nullable unique RESTRICT reference replaces recomputing the capability-bound
invocation hash. Existing rows are linked only by one trustworthy match of the
original plugin, source event, Conversation, generation and owner; an active Job
whose admission cannot be proven absent fails closed with a stable reason.
Old Work is never deleted or terminated here.
"""

import sqlalchemy as sa
from alembic import op

revision = "0100"
down_revision = "0099"
branch_labels = None
depends_on = None

# Frozen migration ownership; do not import mutable runtime metadata.
TABLE = "plugin_background_turn_jobs"
COLUMN = "work_id"
INDEX_NAME = "ux_plugin_background_turn_work"
INDEX_SQL = f"CREATE UNIQUE INDEX {INDEX_NAME} ON {TABLE} ({COLUMN})"
REASON_AMBIGUOUS = "runtime_work_link_ambiguous"
REASON_UNPROVEN = "runtime_work_link_unproven"

# The candidate Work for one Job: same owner, plugin, source event, Conversation,
# and a generation consistent with the source that admitted it.
_MATCH = (
    "FROM runtime_work w "
    "WHERE json_valid(w.source_json) "
    "AND json_extract(w.source_json, '$.owner') = 'plugin_background' "
    "AND json_extract(w.source_json, '$.plugin_id') = j.plugin_id "
    "AND json_extract(w.source_json, '$.trigger_event_id') = j.source_event_id "
    "AND json_extract(w.source_json, '$.conversation_id') = j.canonical_conversation_id "
    "AND w.conversation_id = j.canonical_conversation_id "
    "AND json_extract(w.source_json, '$.generation') = w.generation"
)
_COUNT = f"(SELECT COUNT(*) {_MATCH})"
_ONLY = f"(SELECT w.id {_MATCH})"


def _columns() -> dict[str, tuple[str, int]]:
    bind = op.get_bind()
    return {
        str(row[1]): (str(row[2]).upper(), int(row[3]))
        for row in bind.exec_driver_sql(f'PRAGMA table_info("{TABLE}")')
    }


def _validate(*, required: bool) -> bool:
    bind = op.get_bind()
    target = bind.execute(
        sa.text("SELECT type FROM sqlite_master WHERE name=:name"), {"name": TABLE}
    ).scalar()
    columns = _columns()
    if target != "table" or not {"id", "plugin_id", "source_event_id", "status"} <= set(columns):
        raise RuntimeError("plugin turn work link table shape mismatch")
    if COLUMN not in columns:
        if required:
            raise RuntimeError("plugin turn work link column missing")
        return False
    if columns[COLUMN] != ("VARCHAR(36)", 0):
        raise RuntimeError("plugin turn work link column shape mismatch")
    references = [
        row
        for row in bind.exec_driver_sql(f'PRAGMA foreign_key_list("{TABLE}")')
        if row[3] == COLUMN
    ]
    if len(references) != 1 or (
        references[0][2],
        references[0][4],
        str(references[0][6]).upper(),
    ) != ("runtime_work", "id", "RESTRICT"):
        raise RuntimeError("plugin turn work link reference shape mismatch")
    index = bind.execute(
        sa.text("SELECT tbl_name, sql FROM sqlite_master WHERE type='index' AND name=:name"),
        {"name": INDEX_NAME},
    ).first()
    if index is None or index[0] != TABLE or index[1] is None:
        raise RuntimeError("plugin turn work link index missing")
    if "".join(index[1].replace('"', "").split()) != "".join(INDEX_SQL.split()):
        raise RuntimeError("plugin turn work link index shape mismatch")
    return True


def _backfill() -> None:
    bind = op.get_bind()
    # Unique trustworthy match not already owned by another Job: link it.
    bind.exec_driver_sql(
        f"UPDATE {TABLE} AS j SET {COLUMN} = {_ONLY} "
        f"WHERE j.{COLUMN} IS NULL AND {_COUNT} = 1 "
        f"AND NOT EXISTS (SELECT 1 FROM {TABLE} o WHERE o.{COLUMN} = {_ONLY})"
    )
    # Several candidates: an active Job cannot pick one. Keep the facts for review.
    bind.exec_driver_sql(
        f"UPDATE {TABLE} AS j SET status = 'failed', last_error_category = "
        f"'{REASON_AMBIGUOUS}', lease_until = NULL "
        f"WHERE j.{COLUMN} IS NULL AND j.status IN ('pending', 'processing') "
        f"AND {_COUNT} > 0"
    )
    # No candidate: only a never-claimed Job proves it never admitted a Work
    # (it stays a pending new Job). Any claimed attempt may have had its Work
    # archived or removed, so it fails closed instead of running as new.
    bind.exec_driver_sql(
        f"UPDATE {TABLE} AS j SET status = 'failed', last_error_category = "
        f"'{REASON_UNPROVEN}', lease_until = NULL "
        f"WHERE j.{COLUMN} IS NULL AND j.status IN ('pending', 'processing') "
        f"AND j.attempts > 0"
    )


def upgrade() -> None:
    if not _validate(required=False):
        # Native ADD COLUMN keeps the table and every trigger on other tables.
        op.execute(
            sa.text(
                f"ALTER TABLE {TABLE} ADD COLUMN {COLUMN} VARCHAR(36) "
                "REFERENCES runtime_work (id) ON DELETE RESTRICT"
            )
        )
        op.execute(sa.text(INDEX_SQL))
        _validate(required=True)
    _backfill()


def downgrade() -> None:
    _validate(required=True)
    bind = op.get_bind()
    parked = bind.exec_driver_sql(
        f"SELECT COUNT(*) FROM {TABLE} j JOIN runtime_work w ON w.id = j.{COLUMN} "
        "WHERE j.status IN ('pending', 'processing') "
        "AND w.state NOT IN ('completed', 'failed', 'cancelled')"
    ).scalar()
    if parked:
        # The older producer has no parked Job: it would burn attempts and
        # strand the retained Work. Settle or cancel those Jobs first.
        raise RuntimeError("plugin turn work link has live owners")
    op.drop_index(INDEX_NAME, table_name=TABLE)
    op.execute(sa.text(f"ALTER TABLE {TABLE} DROP COLUMN {COLUMN}"))
