"""Reclamation preserves prefix boundaries without loading unrelated payloads."""

import pytest
from sqlalchemy import event
from tests.support.projection_cases import projection_storage_cases
from tests.support.social_identity_cases import social_env

from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
from qq_ai_bot.conversation.projections import ProjectionConflict, PromptProjectionRepository


async def _projection(database, tmp_path):
    env = await social_env(database, tmp_path)
    async with database.sessions() as session:
        source = await session.get(CanonicalConversationModel, env.context.conversation_id)
        arguments = dict(
            view_key="a" * 64,
            conversation_id=source.id,
            generation=source.generation,
            expected_source_revision=source.prompt_source_revision,
            starts_after_event_id=source.starts_after_event_id,
            context_key="b" * 64,
            contract_revision="c" * 64,
        )
    repository = PromptProjectionRepository(database, max_context_characters=128)
    saved = await repository.commit(**arguments, items=[{"a": 1}], rebuild_reason="bootstrap")
    arguments.update(expected_epoch=saved.epoch_id, expected_revision=saved.revision)
    return repository, arguments


async def test_projection_prefix_payload_is_read_only_before_writer(database, tmp_path):
    repository, arguments = await _projection(database, tmp_path)
    statements = []

    def capture(_conn, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    event.listen(database.engine.sync_engine, "before_cursor_execute", capture)
    try:
        saved = await repository.commit(**arguments, items=[{"a": 1}, {"b": 2}])
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", capture)
    assert saved.revision == 2
    writer = next(i for i, sql in enumerate(statements) if sql == "BEGIN IMMEDIATE")
    payload_reads = [
        i
        for i, sql in enumerate(statements)
        if sql.lstrip().upper().startswith("SELECT") and "prompt_projections.payload_json" in sql
    ]
    assert payload_reads and all(i < writer for i in payload_reads)


async def test_projection_rechecks_revision_after_prefix_preparation(
    database, tmp_path, monkeypatch
):
    repository, arguments = await _projection(database, tmp_path)
    original = repository._prepare_prefix

    async def invalidate_after_prepare(view_key, payload):
        prepared = await original(view_key, payload)
        await repository.invalidate_view(view_key, reason="capacity")
        return prepared

    monkeypatch.setattr(repository, "_prepare_prefix", invalidate_after_prepare)
    with pytest.raises(ProjectionConflict, match="revision changed"):
        await repository.commit(**arguments, items=[{"a": 1}, {"b": 2}])
    assert await repository.read(arguments["view_key"]) is None
    assert await repository.invalidation_reason(arguments["view_key"]) == "capacity"


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
