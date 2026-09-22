"""A lost controller checkpoint does not replay an already accepted direct source."""

from sqlalchemy import select, update
from tests.unit.test_semantic_participation_host import _event_and_route, _host, _item
from yuki_participation.store import SnapshotStore

from qq_ai_bot.runtime.work_schema_v1 import inputs, work


async def test_restart_recovers_only_committed_direct_work(database, tmp_path):
    host, _ = await _host(database, tmp_path, observer=False)
    host._store.close()
    path = tmp_path / "same-host-checkpoint.db"
    host._store = SnapshotStore(path)
    handled = await _event_and_route(database, host.app.ledger, content="已经交给主入口处理")
    pending = await _event_and_route(database, host.app.ledger, content="仅排队，未被Work接纳")
    untouched = await _event_and_route(database, host.app.ledger, content="尚未处理的新消息")
    item = await _item(host, handled)
    lease = await host.work.acquire(item.scene.conversation_id, item.scene.generation)
    assert lease is not None
    accepted = await host.work.accept(
        lease,
        source_key=f"event:{item.scene.conversation_id}:{handled.id}",
        source={
            "trigger_event_id": handled.id,
            "conversation_id": item.scene.conversation_id,
            "generation": item.scene.generation,
            "origin": "user_message",
        },
        goal="处理已接纳的消息",
        output_kind="answer",
    )
    await host.work.release(lease)
    async with database.immediate_session() as session:
        await session.execute(
            update(work).where(work.c.id == accepted["id"]).values(state="completed")
        )
    await host.work.enqueue(
        item.scene.conversation_id,
        item.scene.generation,
        f"event:{item.scene.conversation_id}:{pending.id}",
        kind="message",
        event_id=pending.id,
    )
    # Hard crash before participation checkpoint commit: the Work DB survives.
    host._store.close()
    restarted, _ = await _host(database, tmp_path, observer=False)
    restarted._store.close()
    restarted._store = SnapshotStore(path)
    try:
        restored = await _item(restarted, handled)
        assert restarted._dirty == {}
        assert restored.controller.state.consumed == {f"event:{handled.id}": 1}
        assert not restored.controller.legacy_source_allowed(
            restored.controller.state.events[f"event:{handled.id}"]
        )
        for event in (pending, untouched):
            assert restored.controller.legacy_source_allowed(
                restored.controller.state.events[f"event:{event.id}"]
            )
        async with database.sessions() as session:
            assert (
                await session.scalar(select(inputs.c.state).where(inputs.c.event_id == pending.id))
                == "pending"
            )
    finally:
        await restarted.close()
