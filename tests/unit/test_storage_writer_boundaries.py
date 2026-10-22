"""Identity preparation precedes isolated transcript allocation and media writes."""

import asyncio
from contextlib import contextmanager

import pytest
from sqlalchemy import event
from tests.support.social_identity_cases import social_env

from yuki.plugin_host.ownership import PluginOwnershipError
from yuki.plugin_host.repository import PluginInstallationRepository
from yuki.plugin_host.session_repository import PluginAgentSessionRepository


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
        plugin_api="3.2",
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
    env = await social_env(database, tmp_path)
    repository = await session_repository(database)
    with sql_capture(database) as statements:
        session = await repository.create(
            plugin_id="metadata.session",
            owner_user_id=env.person,
            scope_type="user",
            scope_id=env.person,
        )
    assert_reads_precede_dml(statements)
    with sql_capture(database) as statements:
        message = await repository.append_message(
            plugin_id="metadata.session",
            session_id=session.session_id,
            role="user",
            content="one",
            sender_user_id=env.person,
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
    env = await social_env(database, tmp_path)
    repository = await session_repository(database)
    session = await repository.create(
        plugin_id="metadata.session",
        owner_user_id=env.person,
        scope_type="user",
        scope_id=env.person,
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
                sender_user_id=env.person,
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


@pytest.mark.parametrize("kind", ["state", "config"])
async def test_plugin_value_receipt_decodes_after_the_owned_writer(database, monkeypatch, kind):
    import yuki.plugin_host.repository as repositories

    await session_repository(database)
    original_decode = repositories._decode
    value = {"original": "complete stored value " * 30_000}

    def decode(raw):
        assert not database._writer.locked()
        return original_decode(raw)

    monkeypatch.setattr(repositories, "_decode", decode)
    if kind == "state":
        repository = repositories.PluginStateRepository(database)
        record = await repository.compare_and_set(
            plugin_id="metadata.session",
            namespace="original",
            key="value",
            expected_version=0,
            value=value,
        )
        saved = await repository.get(
            plugin_id="metadata.session", namespace="original", key="value"
        )
    else:
        repository = repositories.PluginConfigRepository(database)
        record = await repository.compare_and_set(
            plugin_id="metadata.session",
            scope_type="global",
            scope_id="",
            key="value",
            expected_version=0,
            value=value,
        )
        saved = await repository.get(
            plugin_id="metadata.session", scope_type="global", scope_id="", key="value"
        )
    assert record.value == saved.value == value
    assert record.id == saved.id and record.version == saved.version == 1


async def test_plugin_message_metadata_decodes_after_transcript_commit(
    database, tmp_path, monkeypatch
):
    import yuki.plugin_host.session_repository as sessions

    env = await social_env(database, tmp_path)
    repository = await session_repository(database)
    session = await repository.create(
        plugin_id="metadata.session",
        owner_user_id=env.person,
        scope_type="user",
        scope_id=env.person,
    )
    original = sessions._message_record

    def message_record(row):
        assert not database._writer.locked()
        return original(row)

    monkeypatch.setattr(sessions, "_message_record", message_record)
    record = await repository.append_message(
        plugin_id="metadata.session",
        session_id=session.session_id,
        role="user",
        content="original content",
        sender_user_id=env.person,
        metadata={"attachments": ["original metadata " * 100] * 50},
    )
    (stored,) = await repository.list_messages(
        plugin_id="metadata.session", session_id=session.session_id
    )
    assert record == stored and record.sequence == 1
