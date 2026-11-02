"""Processor latency contract with real local Host/SQLite and a simulated sender.

No QQ or provider request is made. The barrier tests dependency ordering rather
than imposing a wall-clock latency budget.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest
from sqlalchemy import select
from tests.conftest import MemorySender, build_harness, make_settings
from tests.support.commands_and_chat_helpers import inbound
from tests.support.semantic_participation_host_helpers import _host

from yuki.persistence.models import ChatEventModel


@pytest.fixture
def latency_evidence(tmp_path: Path, request: pytest.FixtureRequest):
    """Persist synthetic-only evidence even when the expected regression fails."""
    directory = Path(os.environ.get("YUKI_LATENCY_ARTIFACT_DIR", str(tmp_path)))
    directory.mkdir(parents=True, exist_ok=True)
    evidence = {
        "scenario": request.node.name,
        "boundary": (
            "real Processor/Host/SQLite; MemorySender returns a simulated receipt; "
            "no real QQ or model"
        ),
    }
    try:
        yield evidence
    finally:
        name = request.node.name.replace("[", "-").replace("]", "").replace("/", "-")
        (directory / f"{name}.json").write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )


@pytest.mark.parametrize("scope", ["private", "group"])
async def test_ping_finishes_without_waiting_for_unconsumed_binding(
    database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, latency_evidence, scope: str
):
    harness = build_harness(
        database,
        make_settings(database.url, conversation_semantic_participation_enabled=True),
    )
    host, _ = await _host(database, tmp_path, observer=False)
    harness.processor.set_participation(host)
    original_binding = host.binding_for_event
    binding_entered = asyncio.Event()
    release_binding = asyncio.Event()

    async def binding_barrier(*args, **kwargs):
        binding_entered.set()
        await release_binding.wait()
        return await original_binding(*args, **kwargs)

    monkeypatch.setattr(host, "binding_for_event", binding_barrier)
    sender = MemorySender()
    message = inbound(
        "/ai ping",
        message_id=f"latency-ping-{scope}",
        group_id="2001" if scope == "group" else None,
    )
    task = asyncio.create_task(harness.processor.handle(message, sender))
    barrier_observer = asyncio.create_task(binding_entered.wait())
    try:
        done, _ = await asyncio.wait_for(
            asyncio.wait((task, barrier_observer), return_when=asyncio.FIRST_COMPLETED), timeout=30
        )
        finished_before_release = task in done
        latency_evidence["before_barrier_release"] = {
            "command_finished": finished_before_release,
            "binding_entered": binding_entered.is_set(),
            "simulated_sends": sender.calls,
        }
        release_binding.set()
        result = await asyncio.wait_for(task, timeout=30)
        async with database.read_session() as reader:
            rows = list(await reader.scalars(select(ChatEventModel).order_by(ChatEventModel.id)))
        source = [row for row in rows if row.direction == "inbound" and row.content == "/ai ping"]
        sent = [
            row for row in rows if row.direction == "outbound" and row.content.startswith("pong (")
        ]
        latency_evidence["after_cleanup"] = {
            "handled": result.handled,
            "sent_messages": result.sent_messages,
            "simulated_sends": sender.calls,
            "inbound_events": len(source),
            "confirmed_outbound_events": len(sent),
            "source_has_internal_id_and_person": bool(
                source and source[0].id and source[0].author_person_id
            ),
            "same_conversation": bool(
                source
                and sent
                and source[0].canonical_conversation_id == sent[0].canonical_conversation_id
            ),
        }
        assert result.handled and result.sent_messages == 1
        assert sender.calls == 1 and sender.messages[0].text.startswith("pong (")
        assert len(source) == len(sent) == 1
        assert source[0].id and source[0].author_person_id and sent[0].author_presence_id
        assert source[0].canonical_conversation_id == sent[0].canonical_conversation_id
        # Binding may run for other legitimate consumers; the command cannot depend on it.
        assert finished_before_release, "ping waited for an unconsumed participation binding"
    finally:
        release_binding.set()
        if not task.done():
            task.cancel()
        if not barrier_observer.done():
            barrier_observer.cancel()
        await asyncio.gather(task, barrier_observer, return_exceptions=True)
        await host.close()
