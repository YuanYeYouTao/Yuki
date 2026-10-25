"""Fact-scoped reuse and frozen snapshot capture use real owners and SQLite."""

import asyncio

import pytest
from sqlalchemy import delete, event, update
from tests.support.semantic_participation_host_helpers import _event_and_route, _host
from yuki_participation.snapshot import thaw

from yuki.persistence.models import ChatEventModel


async def test_context_reuses_bodies_but_reloads_inserts_revisions_and_deletes(database, tmp_path):
    host, _ = await _host(database, tmp_path, observer=False)
    try:
        source = await _event_and_route(database, host.ledger)
        scene = await host._scene(source.canonical_conversation_id)
        scope = scene.identity
        first = await host.ledger.read_scope_context(scope, limit=64, message_only=True)
        statements = []

        def record(_conn, _cursor, statement, _parameters, _context, _many):
            statements.append(statement)

        event.listen(database.engine.sync_engine, "before_cursor_execute", record)
        try:
            same = await host.ledger.read_scope_context(
                scope, limit=64, message_only=True, previous=first
            )
        finally:
            event.remove(database.engine.sync_engine, "before_cursor_execute", record)
        assert same[1] is first[1]
        assert not any("chat_events.content" in statement for statement in statements)
        newer = await _event_and_route(database, host.ledger)
        page = await host.ledger.read_scope_context(
            scope, limit=64, message_only=True, previous=same
        )
        assert page[1] is not same[1] and page[1][-1].id == newer.id
        smaller = await host.ledger.read_scope_context(
            scope, limit=1, message_only=True, previous=page
        )
        assert [row.id for row in smaller[1]] == [newer.id]
        larger = await host.ledger.read_scope_context(
            scope, limit=64, message_only=True, previous=smaller
        )
        assert [row.id for row in larger[1]] == [source.id, newer.id]
        async with database.immediate_session() as writer:
            await writer.execute(
                update(ChatEventModel)
                .where(ChatEventModel.id == source.id)
                .values(content="corrected source")
            )
        corrected = await host.ledger.read_scope_context(
            scope, limit=64, message_only=True, previous=page
        )
        assert corrected[1][0].content == "corrected source"
        async with database.immediate_session() as writer:
            await writer.execute(delete(ChatEventModel).where(ChatEventModel.id == source.id))
        removed = await host.ledger.read_scope_context(
            scope, limit=64, message_only=True, previous=corrected
        )
        assert [row.id for row in removed[1]] == [newer.id]
    finally:
        await host.close()


async def test_inflight_save_freezes_before_await_and_preserves_later_body_change(
    database, tmp_path
):
    host, _ = await _host(database, tmp_path, observer=False)
    source = await _event_and_route(database, host.ledger)
    item = await host._session(await host._scene(source.canonical_conversation_id))
    entered, release = asyncio.Event(), asyncio.Event()
    original = host._store.save
    captured = []

    async def delayed(value, *, expected_revision):
        captured.append(value)
        entered.set()
        await release.wait()
        return await original(value, expected_revision=expected_revision)

    item.controller.set_state_entry("host_checkpoint", "extension", {"version": "before"})
    host._store.save = delayed
    saving = asyncio.create_task(host._save(item))
    try:
        await entered.wait()
        item.controller.set_state_entry("host_checkpoint", "extension", {"version": "after"})
        saving.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await saving
        revision, progress, body = await host._store.load(item.scene.scope)
        assert revision == item.revision
        assert thaw(item.scene.scope, progress, body).host_checkpoint["extension"] == {
            "version": "before"
        }
        assert item.controller.body_changed
        await host._save(item)
        revision, progress, body = await host._store.load(item.scene.scope)
        assert revision == item.revision
        assert thaw(item.scene.scope, progress, body).host_checkpoint["extension"] == {
            "version": "after"
        }
        assert captured[0].body != captured[1].body
    finally:
        release.set()
        await asyncio.gather(saving, return_exceptions=True)
        await host.close()


async def test_unchanged_save_skips_all_encoding_and_sqlite_work(database, tmp_path, monkeypatch):
    host, _ = await _host(database, tmp_path, observer=False)
    try:
        source = await _event_and_route(database, host.ledger)
        item = await host._session(await host._scene(source.canonical_conversation_id))
        await host._save(item)
        revision = item.revision
        monkeypatch.setattr(
            "yuki.services.semantic_participation.freeze",
            lambda *_a, **_k: pytest.fail("unchanged save encoded a snapshot"),
        )
        await host._save(item)
        assert item.revision == revision
    finally:
        await host.close()
