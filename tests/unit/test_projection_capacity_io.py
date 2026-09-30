"""Reclamation preserves prefix boundaries without loading unrelated payloads."""

from sqlalchemy import event
from tests.support.projection_cases import projection_storage_cases
from tests.support.social_identity_cases import social_env


async def test_projection_capacity_reads_metadata_only(database, tmp_path):
    env = await social_env(database, tmp_path)
    statements = []

    def capture(_conn, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    event.listen(database.engine.sync_engine, "before_cursor_execute", capture)
    try:
        await projection_storage_cases(database, env.context.conversation_id)
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", capture)
    others = [sql for sql in statements if "prompt_projections.view_key !=" in sql]
    assert others
    assert all("prompt_projections.payload_json" not in sql for sql in others)
