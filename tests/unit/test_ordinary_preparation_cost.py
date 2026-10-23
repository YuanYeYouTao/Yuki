"""Committed ordinary feedback updates its controller without reloading a history page."""

from unittest.mock import AsyncMock

from tests.support.semantic_participation_host_helpers import _event_and_route, _host

from yuki.persistence.models import ChatEventModel
from yuki.services.participation_feedback import admission_unit_binding


async def test_binding_and_committed_feedback_share_group_scope_preparation(
    database, tmp_path, monkeypatch
):
    host, _ = await _host(database, tmp_path, observer=False)
    try:
        row = await _event_and_route(database, host.ledger)
        hydrate = AsyncMock(wraps=host._hydrate)
        monkeypatch.setattr(host, "_hydrate", hydrate)
        binding = await host.binding_for_event(row.id)
        assert binding is not None
        config_reads = host.runtime_config.snapshot.await_count
        history_reads = hydrate.await_count
        assert history_reads
        prepared = host.ordinary.prepare(row, binding.generation, 1, binding, reason="mention")
        assert await host.ordinary.commit(prepared)
        await host.on_ordinary_admitted(binding, prepared.admission)
        assert hydrate.await_count == history_reads
        assert host.runtime_config.snapshot.await_count == config_reads
        assert all(
            call.kwargs.get("group_id") for call in host.runtime_config.snapshot.await_args_list
        )
        item = host._sessions[(binding.conversation_id, binding.generation)]
        event = item.controller.state.events[f"event:{row.id}"]
        unit = admission_unit_binding(prepared.admission)
        # The actual library accepts this as the same already-observed original input.
        assert item.controller.observe_unit_input(unit, event.ref)
        before = item.controller.state.model_dump_json()
        await host.on_ordinary_admitted(binding, prepared.admission)
        assert item.controller.state.model_dump_json() == before
        assert hydrate.await_count == history_reads
    finally:
        await host.close()


async def test_feedback_revalidates_deleted_source_without_rehydrating(
    database, tmp_path, monkeypatch
):
    host, _ = await _host(database, tmp_path, observer=False)
    try:
        row = await _event_and_route(database, host.ledger)
        binding = await host.binding_for_event(row.id)
        prepared = host.ordinary.prepare(row, binding.generation, 1, binding, reason="mention")
        assert await host.ordinary.commit(prepared)
        async with database.immediate_session() as writer:
            await writer.delete(await writer.get(ChatEventModel, row.id))
        save = AsyncMock(wraps=host._save)
        monkeypatch.setattr(host, "_save", save)
        await host.on_ordinary_admitted(binding, prepared.admission)
        save.assert_not_awaited()
    finally:
        await host.close()


async def test_cold_feedback_recovers_original_admission(database, tmp_path):
    host, _ = await _host(database, tmp_path, observer=False)
    try:
        row = await _event_and_route(database, host.ledger)
        binding = await host.binding_for_event(row.id)
        prepared = host.ordinary.prepare(row, binding.generation, 1, binding, reason="mention")
        assert await host.ordinary.commit(prepared)
        host._sessions.clear()
        await host.on_ordinary_admitted(binding, prepared.admission)
        item = host._sessions[(binding.conversation_id, binding.generation)]
        event = item.controller.state.events[f"event:{row.id}"]
        assert item.controller.observe_unit_input(
            admission_unit_binding(prepared.admission), event.ref
        )
    finally:
        await host.close()
