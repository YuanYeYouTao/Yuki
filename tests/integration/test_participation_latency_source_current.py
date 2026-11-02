"""Current source revisions use the real Host/SQLite semantics across edits."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import event, update
from tests.integration.test_participation_latency_commands import (
    latency_evidence as latency_evidence,
)
from tests.support.semantic_participation_host_helpers import _event_and_route, _host, _item

from yuki.conversation.initiative_sources import source_revision
from yuki.persistence.models import ChatEventModel


@pytest.mark.parametrize(
    "change",
    [
        "none",
        "body",
        "bool_difference",
        "large_integer_difference",
        "signed_zero_difference",
        "missing_null_equivalent",
        "escaped_equivalent",
        "unrelated_event",
    ],
)
async def test_current_source_exact_revision_and_unrelated_edits(
    database, tmp_path, latency_evidence, change
):
    host, _ = await _host(database, tmp_path, observer=False)
    try:
        source = await _event_and_route(database, host.ledger, content="synthetic original")
        other = await _event_and_route(database, host.ledger, content="synthetic unrelated")
        segment = {
            "type": "mention",
            "data": {
                "kind": "member",
                "display_name": "合成名称",
                "person_id": None,
                "text_start": True,
                "text_end": 2,
                "source": {"native": "not-semantic"},
            },
        }
        if change == "large_integer_difference":
            segment["data"]["text_start"] = 2**63
        elif change == "signed_zero_difference":
            segment["data"]["text_start"] = -0.0
        async with database.immediate_session() as writer:
            await writer.execute(
                update(ChatEventModel)
                .where(ChatEventModel.id == source.id)
                .values(segments_json=json.dumps([segment], ensure_ascii=False))
            )
        source = await host.ledger.get_event(source.id)
        item = await _item(host, source)
        ref = item.controller.state.events[f"event:{source.id}"].ref
        before_revision = source_revision(source)
        assert (await host._sources_current(item, (ref,)))[ref]
        item.controller.take_changes()

        updated = {"type": "mention", "data": dict(segment["data"])}
        if change == "bool_difference":
            updated["data"]["text_start"] = 1
        elif change == "large_integer_difference":
            updated["data"]["text_start"] = 2**63 + 1
        elif change == "signed_zero_difference":
            updated["data"]["text_start"] = 0.0
        elif change == "missing_null_equivalent":
            del updated["data"]["person_id"]
        async with database.immediate_session() as writer:
            if change == "body":
                await writer.execute(
                    update(ChatEventModel)
                    .where(ChatEventModel.id == source.id)
                    .values(content="synthetic corrected")
                )
            elif change == "unrelated_event":
                await writer.execute(
                    update(ChatEventModel)
                    .where(ChatEventModel.id == other.id)
                    .values(content="synthetic unrelated correction")
                )
            elif change != "none":
                await writer.execute(
                    update(ChatEventModel)
                    .where(ChatEventModel.id == source.id)
                    .values(
                        segments_json=json.dumps(
                            [updated], ensure_ascii=change == "escaped_equivalent"
                        )
                    )
                )
        after_revision = source_revision(await host.ledger.get_event(source.id))
        statements = []

        def capture(_connection, _cursor, sql, _parameters, _context, _many):
            if sql.lstrip().lower().startswith("select") and "from chat_events" in sql.lower():
                statements.append(sql)

        event.listen(database.engine.sync_engine, "before_cursor_execute", capture)
        try:
            actual = (await host._sources_current(item, (ref,)))[ref]
        finally:
            event.remove(database.engine.sync_engine, "before_cursor_execute", capture)
        equivalent = change in {
            "none",
            "missing_null_equivalent",
            "escaped_equivalent",
            "unrelated_event",
        }
        latency_evidence.update(
            boundary="real Host source check and SQLite, synthetic canonical events; no provider",
            change=change,
            original_digest_equal=after_revision == before_revision,
            current=actual,
            invalidated_revision=item.controller.state.invalidated.get(ref.event_id),
            source_sql_selects=[sql.lower().split("from chat_events")[0] for sql in statements],
        )
        assert statements
        assert (after_revision == before_revision) == equivalent
        assert actual == equivalent
        if equivalent:
            assert ref.event_id not in item.controller.state.invalidated
        else:
            assert item.controller.state.invalidated.get(ref.event_id) == ref.revision
    finally:
        await host.close()
