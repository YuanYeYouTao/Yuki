"""Real SQLite preparation cost and ordinary feedback recovery, without model calls."""

import statistics
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import event as sql_event
from tests.conftest import make_settings
from tests.support.semantic_participation_host_helpers import _event_and_route
from tests.unit.test_participation_ordinary_feedback import ordinary_expression
from yuki_participation.participation import ParticipationCheckpoint
from yuki_participation.self_report import SelfDelta

from yuki.admin.config_service import RuntimeConfigService
from yuki.domain.messages import ChatResponse


@pytest.mark.parametrize("prepared_input", [False, True])
async def test_warm_binding_and_hint_keep_original_source_and_saved_quiet(
    database, tmp_path, prepared_input
):
    host, item, source, _, _ = await ordinary_expression(database, tmp_path, send=False)
    host.app.runtime_config = RuntimeConfigService(
        settings=make_settings(database.url, conversation_semantic_participation_enabled=True),
        database=database,
    )
    host.app.chat = SimpleNamespace(validate_turn_snapshot=AsyncMock(return_value=True))
    queries = []
    scanned = []
    read_context = host.app.ledger.read_scope_context

    async def count_history(*args, **kwargs):
        version, rows = await read_context(*args, **kwargs)
        scanned.append(len(rows))
        return version, rows

    def count_sql(_conn, _cursor, statement, _parameters, _context, _many):
        queries.append(statement)

    host.app.ledger.read_scope_context = count_history
    sql_event.listen(database.engine.sync_engine, "before_cursor_execute", count_sql)
    try:
        for index in range(63):
            await _event_and_route(database, host.app.ledger, content=f"history {index}")
        await host._hydrate(item)
        item.observation.queue.pending.clear()
        original = item.controller.state.events[f"event:{source.id}"]
        snapshot = SimpleNamespace(
            conversation_id=item.scene.conversation_id, generation=item.scene.generation
        )
        runtime = SimpleNamespace(
            turn_snapshot=snapshot,
            effective_trigger_event_id=source.id,
            visible_event_ids=frozenset(),
        )
        prepared_runtime = await host.app.runtime_config.snapshot(group_id=item.scene.space_id)
        samples = {"binding": [], "hint": []}
        for sequence in range(1, 6):
            queries.clear()
            scanned.clear()
            started = time.perf_counter()
            binding = await host.binding_for_event(
                source.id,
                **({"record": source, "runtime": prepared_runtime} if prepared_input else {}),
            )
            samples["binding"].append(
                (len(queries), sum(scanned), (time.perf_counter() - started) * 1000)
            )
            assert binding.event_id == source.id
            assert binding.basis == ((original.ref.event_id, original.ref.revision),)
            queries.clear()
            scanned.clear()
            started = time.perf_counter()
            await host.observe_main_response(
                runtime,
                sequence,
                ChatResponse("", 0, provider_request_id=f"local-{sequence}"),
                SelfDelta(engage="quiet"),
            )
            samples["hint"].append(
                (len(queries), sum(scanned), (time.perf_counter() - started) * 1000)
            )
            assert sum(scanned) == 0, "a resident hint must not rescan unrelated history"
            checkpoint = ParticipationCheckpoint.model_validate(
                item.controller.state.host_checkpoint["participation_v1"]
            )
            unit = next(iter(checkpoint.units.values()))
            assert unit.hint.delta.engage == "quiet"
            assert unit.hint.source == original.ref
            assert unit.hint.sequence == sequence
        for name, values in samples.items():
            label = f"{name}_prepared_input" if prepared_input and name == "binding" else name
            print(
                f"participation_cost {label}: n={len(values)} "
                f"SQL={statistics.median(v[0] for v in values):g} "
                f"history_rows={statistics.median(v[1] for v in values):g} "
                f"elapsed_ms={statistics.median(v[2] for v in values):.3f}"
            )
        host._sessions.clear()
        restored = await host._session(item.scene)
        recovered = ParticipationCheckpoint.model_validate(
            restored.controller.state.host_checkpoint["participation_v1"]
        )
        assert recovered.units == checkpoint.units
        assert not restored.observation.queue.pending
        assert host._observer.calls == []
    finally:
        sql_event.remove(database.engine.sync_engine, "before_cursor_execute", count_sql)
        await host.close()
