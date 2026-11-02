"""Actual SQLite read snapshots cannot carry stale source proof across phases."""

from __future__ import annotations

import asyncio
import json

import pytest
from sqlalchemy import update
from tests.integration.test_participation_latency_commands import (
    latency_evidence as latency_evidence,
)
from tests.unit.test_participation_pipeline_retirement import _relationship

from yuki.persistence.models import ChatEventModel


@pytest.mark.parametrize("change", ["body", "mention", "suppression"])
@pytest.mark.parametrize("concurrent_hydrate", [False, True])
async def test_hydration_rechecks_original_source_after_read_snapshot_changes(
    database, tmp_path, monkeypatch, latency_evidence, change, concurrent_hydrate
):
    host, item, prior, original, _ = await _relationship(database, tmp_path)
    first_read = asyncio.Event()
    release = asyncio.Event()
    calls = []
    original_current = host.ordinary.current_admissions
    try:
        ref = item.controller.state.events[f"event:{prior.id}"].ref

        async def intercept(*args, **kwargs):
            result = await original_current(*args, **kwargs)
            calls.append({"has_session": kwargs.get("session") is not None, "rows": len(result)})
            if len(calls) == 1:
                first_read.set()
                await release.wait()
            return result

        monkeypatch.setattr(host.ordinary, "current_admissions", intercept)
        first = asyncio.create_task(host._hydrate(item))
        await asyncio.wait_for(first_read.wait(), 10)
        values = (
            {"content": "synthetic corrected source"}
            if change == "body"
            else {
                "segments_json": json.dumps(
                    [
                        {
                            "type": "mention",
                            "data": {
                                "kind": "member",
                                "display_name": "synthetic",
                                "person_id": prior.author_person_id,
                                "text_start": 0,
                                "text_end": 1,
                            },
                        }
                    ]
                )
            }
            if change == "mention"
            else {"suppression_status": "duplicate", "utterance_fingerprint": "f" * 64}
        )
        async with database.immediate_session() as writer:
            await writer.execute(
                update(ChatEventModel).where(ChatEventModel.id == prior.id).values(**values)
            )
        if concurrent_hydrate:
            await asyncio.wait_for(host._hydrate(item), 10)
        release.set()
        await asyncio.wait_for(first, 10)
        await host._validate_boundaries(item)
        current = (await host._sources_current(item, (ref,)))[ref]
        saved = await host.ordinary.get(prior.id)
        latency_evidence.update(
            boundary="real Host/SQLite WAL read snapshots; canonical source changed by test SQL",
            change=change,
            concurrent_hydrate=concurrent_hydrate,
            current_admission_calls=calls,
            original_source_current=current,
            original_invalidated_revision=item.controller.state.invalidated.get(ref.event_id),
            original_activation_preserved=saved.activation_id == original.admission.activation_id,
        )
        assert (
            not current and item.controller.state.invalidated.get(ref.event_id, 0) >= ref.revision
        )
        assert saved.activation_id == original.admission.activation_id
    finally:
        release.set()
        if "first" in locals() and not first.done():
            first.cancel()
            await asyncio.gather(first, return_exceptions=True)
        await host.close()
