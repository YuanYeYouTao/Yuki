"""One input read view replaces duplicate negative probes; writer still owns delivery."""

from unittest.mock import AsyncMock

from sqlalchemy import event, select
from tests.support.event_seed import seed_event
from tests.support.semantic_participation_host_helpers import _event_and_route
from tests.unit.test_work_settlement_writer import _running_work

from yuki.persistence.models import ChatEventModel
from yuki.persistence.repositories import EventLedgerRepository
from yuki.runtime.work_repository import WorkRepository
from yuki.runtime.work_schema_v1 import inputs
from yuki.runtime.work_wait import WorkWaitRepository


async def test_unmatched_input_uses_one_narrow_read_without_writer_or_media(database, monkeypatch):
    row = await _event_and_route(database, EventLedgerRepository(database))
    sessions, statements = [], []
    original = database.read_session

    def session(*args, **kwargs):
        sessions.append(1)
        return original(*args, **kwargs)

    def capture(_connection, _cursor, statement, *_args):
        statements.append(statement)

    monkeypatch.setattr(database, "read_session", session)
    event.listen(database.engine.sync_engine, "before_cursor_execute", capture)
    prepare = AsyncMock(return_value=True)
    try:
        assert (
            await WorkWaitRepository(WorkRepository(database)).match_input(
                row.id, prepare_source=prepare
            )
            is None
        )
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", capture)
    prepare.assert_not_awaited()
    assert len(sessions) == 1
    assert not any("BEGIN IMMEDIATE" in statement for statement in statements)
    assert not any("chat_events.content" in statement for statement in statements)


async def test_wait_rechecks_source_after_external_preparation(database, tmp_path):
    repository, lease, control = await _running_work(database, tmp_path)
    try:
        wait = WorkWaitRepository(repository)
        await wait.register(
            lease,
            work_id=control.current["id"],
            source=control.source,
            call_key="wait-input",
            mode="any",
            conditions=[{"kind": "conversation"}],
            deadline_at=None,
        )
        appended = await seed_event(
            EventLedgerRepository(database),
            bot_user_id="80001",
            platform_message_id="new-wait-source",
            scope_type="group",
            sender_user_id="10001",
            direction="inbound",
            content="new input",
            group_id="20001",
        )
        source = appended[0]

        async def prepare():
            # This real writer can run because media/source preparation owns no writer.
            async with database.immediate_session() as writer:
                await writer.delete(await writer.get(ChatEventModel, source.id))
            return True

        assert await wait.match_input(source.id, prepare_source=prepare) is None
        async with database.read_session() as reader:
            assert not await reader.scalar(
                select(inputs.c.id).where(inputs.c.event_id == source.id)
            )
        assert (await wait.describe(control.current["id"]))["status"] == "active"
    finally:
        await repository.release(lease)
