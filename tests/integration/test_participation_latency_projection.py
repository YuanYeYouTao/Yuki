"""Real canonical source projection with same-thread SELF and stop boundary."""

from __future__ import annotations

import time

from tests.integration.test_participation_latency_commands import (
    latency_evidence as latency_evidence,
)
from tests.integration.test_participation_projection_cost import _append, _resolve_new
from tests.support.semantic_participation_host_helpers import (
    _event_and_route,
    _host,
    _item,
    _observation,
)
from yuki_participation.models import Snapshot


async def test_projection_deduplicates_boundaries_but_not_distinct_self_anchors(
    database, tmp_path, latency_evidence
):
    host, _ = await _host(database, tmp_path, observer=False)
    try:
        first = await _event_and_route(database, host.ledger, content="synthetic first source")
        item = await _item(host, first)
        unit = _resolve_new(item, first, 1)
        older = await _append(host, author="8000", cause=first.id, age=30)
        newer = await _append(host, author="8000", cause=first.id, age=20)
        await host._hydrate(item)
        stop = await _append(host, author="1001", reply_to=first.id, age=15)
        await host._hydrate(item)
        stop_event = item.controller.state.events[f"event:{stop.id}"]
        snap = Snapshot(
            scope=item.scene.scope, focus=stop_event, context=(), sequence=2, issued_at=time.time()
        )
        assert item.controller.apply_semantic_observation(_observation(snap, act="ask_yuki_stop"))
        assert item.controller.state.boundaries
        focus = await _append(host, author="1001", age=10)
        projected = host._event(focus, item)
        older_ref = item.controller.state.events[f"event:{older.id}"].ref
        newer_ref = item.controller.state.events[f"event:{newer.id}"].ref
        assert projected is not None and projected.unit_ambiguous
        actual = [(o.key, o.thread, o.target, o.self_anchor) for o in projected.unit_options]
        assert actual == [
            ("new", f"event:{focus.id}", focus.author_person_id, None),
            ("u1", unit.thread, focus.author_person_id, newer_ref),
            ("u2", unit.thread, focus.author_person_id, older_ref),
            ("u3", unit.thread, focus.author_person_id, None),
        ]
        quote = await _append(host, author="1001", reply_to=older.id, age=5)
        quoted = host._event(quote, item)
        assert quoted.reply_to == older_ref and quoted.thread == unit.thread
        assert not quoted.unit_ambiguous and quoted.unit_options == ()
        own = await _append(host, author="8000", cause=first.id, age=1)
        projected_self = host._event(own, item)
        assert projected_self.kind == "self" and projected_self.thread == unit.thread
        assert not projected_self.unit_ambiguous and projected_self.unit_options == ()
        latency_evidence.update(
            boundary="real Host/Controller/SQLite with synthetic observed stop and messages",
            options=len(actual),
            same_thread_self_anchors=2,
            boundary_count=len(item.controller.state.boundaries),
            boundary_duplicate_removed=True,
            quoted_options=len(quoted.unit_options),
            self_options=len(projected_self.unit_options),
        )
    finally:
        await host.close()
