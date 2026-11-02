"""Unknown prefix and ordinary mention retain real Processor admission."""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest
from sqlalchemy import select
from tests.conftest import MemorySender, build_harness, make_settings
from tests.integration.test_participation_latency_commands import (
    latency_evidence as latency_evidence,
)
from tests.support.commands_and_chat_helpers import inbound
from tests.support.semantic_participation_host_helpers import _event_and_route, _host

from yuki.llm.fake import FakeLLMProvider
from yuki.persistence.models import ChatEventModel


@pytest.mark.parametrize("text", ["/ai not-a-known-command", "@Yuki 普通提问"])
async def test_regular_main_still_uses_real_binding_and_original_admission(
    database, tmp_path, monkeypatch, latency_evidence, text
):
    provider = FakeLLMProvider(lambda _: "NO_REPLY")
    harness = build_harness(
        database,
        make_settings(database.url, conversation_semantic_participation_enabled=True),
        provider,
    )
    host, _ = await _host(database, tmp_path, observer=False)
    await _event_and_route(database, host.ledger)
    harness.processor.set_participation(host)
    original = host.binding_for_event
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocked(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(host, "binding_for_event", blocked)
    sender = MemorySender()
    source = replace(
        inbound(text, message_id="synthetic-main", group_id="2001", mentions_bot=True),
        bot_user_id="8000",
    )
    task = asyncio.create_task(harness.processor.handle(source, sender))
    try:
        await asyncio.wait_for(entered.wait(), 30)
        assert not task.done()
        release.set()
        result = await asyncio.wait_for(task, 30)
        async with database.read_session() as reader:
            row = await reader.scalar(
                select(ChatEventModel).where(ChatEventModel.origin_key == "synthetic-main")
            )
        admission = await host.ordinary.get(row.id)
        latency_evidence.update(
            boundary="real Processor/Host/admission/SQLite, fake model and sender",
            input_shape="unknown_prefix" if text.startswith("/ai") else "mention",
            binding_reached=True,
            source_event_id=row.id,
            admission=admission is not None,
            provider_requests=len(provider.requests),
            simulated_sends=sender.calls,
            handled=result.handled,
        )
        assert admission is not None and admission.event_id == row.id
        assert admission.actor_person_id == row.author_person_id
        assert len(provider.requests) == 1 and sender.calls == 0
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await host.close()
