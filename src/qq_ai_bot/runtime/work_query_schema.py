"""Current indexes for scoped Work metadata, separate from the frozen schema."""

from typing import Any

from sqlalchemy import Index, func, literal_column, text
from sqlalchemy.dialects import sqlite
from sqlalchemy.schema import CreateIndex

from qq_ai_bot.runtime.work_schema_v1 import WORK_STATES, inputs, work

SOURCE_SCOPE_FIELDS = (
    "actor_user_id",
    "actor_person_id",
    "origin",
    "plugin_id",
    "delegation_id",
    "execution_boundary",
    "principal_kind",
    "initiative_run_id",
)

QUERY_INDEX_NAMES = (
    "ix_runtime_work_query_scope_updated",
    "ix_runtime_work_query_updated",
    "ix_runtime_work_query_state_updated",
)

INPUT_REPAIR_INDEX_NAME = "ix_runtime_work_inputs_abandoned"
INPUT_REPAIR_INDEX_SQL = (
    f"CREATE INDEX {INPUT_REPAIR_INDEX_NAME} ON runtime_work_inputs (id) "
    "WHERE state = 'pending' AND ready IS 0"
)

Index(
    INPUT_REPAIR_INDEX_NAME,
    inputs.c.id,
    sqlite_where=text("state = 'pending' AND ready IS 0"),
)


def work_statuses(status: str) -> tuple[str, ...]:
    terminal = {"completed", "failed", "cancelled"}
    if status not in {"active", "terminal"}:
        raise ValueError("invalid_work_query_status")
    return tuple(state for state in WORK_STATES if (state in terminal) == (status == "terminal"))


def source_scope_column(key: str) -> Any:
    # Literal paths must match the SQLite expression index. These are fixed
    # internal field names, never supplied by the caller.
    if key not in SOURCE_SCOPE_FIELDS:
        raise ValueError("invalid_work_source_field")
    return func.json_extract(work.c.source_json, literal_column(f"'$.{key}'"))


Index(
    "ix_runtime_work_query_scope_updated",
    work.c.conversation_id,
    work.c.generation,
    *(source_scope_column(key) for key in SOURCE_SCOPE_FIELDS),
    work.c.updated.desc(),
    work.c.id.desc(),
)

Index("ix_runtime_work_query_updated", work.c.updated.desc(), work.c.id.desc())

Index(
    "ix_runtime_work_query_state_updated",
    work.c.state,
    work.c.updated.desc(),
    work.c.id.desc(),
)


def query_index_sql() -> dict[str, str]:
    return {
        index.name: str(CreateIndex(index).compile(dialect=sqlite.dialect()))
        for index in work.indexes
        if index.name in QUERY_INDEX_NAMES
    }
