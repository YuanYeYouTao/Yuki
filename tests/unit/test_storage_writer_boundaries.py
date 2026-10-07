"""Identity preparation precedes isolated transcript allocation and media writes."""

import asyncio
from contextlib import contextmanager

import pytest
from sqlalchemy import event
from tests.support.social_identity_cases import social_env

from qq_ai_bot.plugin_host.ownership import PluginOwnershipError
from qq_ai_bot.plugin_host.repository import PluginInstallationRepository
from qq_ai_bot.plugin_host.session_repository import PluginAgentSessionRepository


@contextmanager
def sql_capture(database):
    statements = []

    def capture(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement.lstrip().upper())

    event.listen(database.engine.sync_engine, "before_cursor_execute", capture)
    try:
        yield statements
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", capture)


def assert_reads_precede_dml(statements):
    first = next(
        i
        for i, statement in enumerate(statements)
        if statement.startswith(("UPDATE", "INSERT", "DELETE"))
    )
    assert not any(statement.startswith("SELECT") for statement in statements[first + 1 :])


async def session_repository(database):
    await PluginInstallationRepository(database).upsert_discovered(
        plugin_id="metadata.session",
        name="Metadata",
        version="1.0.0",
        plugin_api="3.1",
        yuki_requires=">=3.0.0,<4.0",
        manifest_hash="ab" * 32,
        entrypoint="plugin:Metadata",
        requested_permissions=("storage.private",),
    )
    return PluginAgentSessionRepository(database)


@pytest.mark.asyncio
async def test_session_identity_reads_finish_before_sequence_or_transcript_write(
    database, tmp_path
):
    await social_env(database, tmp_path)
    repository = await session_repository(database)
    with sql_capture(database) as statements:
        session = await repository.create(
            plugin_id="metadata.session",
            owner_user_id="10001",
            scope_type="user",
            scope_id="10001",
        )
    assert_reads_precede_dml(statements)
    with sql_capture(database) as statements:
        message = await repository.append_message(
            plugin_id="metadata.session",
            session_id=session.session_id,
            role="user",
            content="one",
            sender_user_id="10001",
        )
    assert message.sequence == 1
    assert_reads_precede_dml(statements)
    with sql_capture(database) as statements:
        reset = await repository.reset(plugin_id="metadata.session", session_id=session.session_id)
    assert reset is not None and reset.next_sequence == 1 and reset.turn_count == 0
    assert_reads_precede_dml(statements)


@pytest.mark.asyncio
async def test_bad_sender_does_not_allocate_sequence_and_concurrent_appends_are_unique(
    database, tmp_path
):
    await social_env(database, tmp_path)
    repository = await session_repository(database)
    session = await repository.create(
        plugin_id="metadata.session",
        owner_user_id="10001",
        scope_type="user",
        scope_id="10001",
    )
    with pytest.raises(PluginOwnershipError):
        await repository.append_message(
            plugin_id="metadata.session",
            session_id=session.session_id,
            role="user",
            content="rejected",
            sender_user_id="99999",
        )
    messages = await asyncio.gather(
        *(
            repository.append_message(
                plugin_id="metadata.session",
                session_id=session.session_id,
                role="user",
                content=str(i),
                sender_user_id="10001",
            )
            for i in range(4)
        )
    )
    assert sorted(message.sequence for message in messages) == [1, 2, 3, 4]
    assert (
        len(
            await repository.list_messages(
                plugin_id="metadata.session", session_id=session.session_id
            )
        )
        == 4
    )
