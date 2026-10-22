"""Ordinary handoff preserves the real adapters' private protocols and fixed inputs."""

import asyncio
import json
from dataclasses import replace

import httpx
import pytest
from sqlalchemy import select, update
from tests.integration.test_codemode_provider_wire import (
    DIALECTS,
    SIGNATURE,
    answer,
    wire_calls_and_results,
)
from tests.integration.test_current_material_main_contract import _catalog, _deployment, _profile
from tests.unit.test_auto_reply_merge import _wait_for_worker
from tests.unit.test_ordinary_input_handoff import A, B, C, _tool, _work_count

from yuki.domain.messages import ChatResponse
from yuki.identity.db_models import CanonicalSpaceModel
from yuki.llm.anthropic_messages import AnthropicMessagesProvider
from yuki.llm.gemini import GeminiProvider
from yuki.llm.openai_compatible import OpenAICompatibleProvider
from yuki.llm.openai_responses import OpenAIResponsesProvider
from yuki.model_runtime.models import ModelProtocol
from yuki.model_runtime.pool import ModelClientPool
from yuki.persistence.models import ChatEventModel


@pytest.mark.parametrize(("vendor", "protocol"), DIALECTS)
async def test_actual_adapters_continue_original_ordinary_chain_after_input_handoff(
    database, tmp_path, vendor, protocol
):
    captured = []
    arrived = {number: asyncio.Event() for number in (1, 2)}
    release = {number: asyncio.Event() for number in (1, 2)}
    responses = iter(
        [
            _tool("query_chat_history", {"view": "recent"}, "signed-read"),
            ChatResponse("NO_REPLY", 0),
        ]
    )

    async def transport(request):
        captured.append(json.loads(request.content))
        number = len(captured)
        if number in arrived:
            arrived[number].set()
            await release[number].wait()
        return httpx.Response(
            200, json=answer(next(responses, ChatResponse("NO_REPLY", 0)), protocol, number)
        )

    async with httpx.AsyncClient(
        base_url="https://handoff-wire.invalid/v1/", transport=httpx.MockTransport(transport)
    ) as client:
        adapter_class = {
            "chat_completions": OpenAICompatibleProvider,
            "responses": OpenAIResponsesProvider,
            "anthropic_messages": AnthropicMessagesProvider,
            "gemini": GeminiProvider,
        }[protocol]
        adapter = adapter_class(
            base_url="https://handoff-wire.invalid/v1/",
            api_key="synthetic",
            timeout_seconds=2,
            max_retries=0,
            client=client,
            **({"provider_name": vendor} if protocol == "chat_completions" else {}),
        )
        app, env, original, sender = await _deployment(
            database, tmp_path, adapter, conversation_autonomous_debounce_seconds=0
        )
        profile = _profile().model_copy(
            update={"provider": vendor, "protocol": ModelProtocol(protocol), "model": "synthetic"}
        )
        app.models.apply_catalog(
            _catalog(profile), ModelClientPool(injected_profiles={"main": adapter})
        )
        async with database.immediate_session() as writer:
            await writer.execute(
                update(CanonicalSpaceModel)
                .where(CanonicalSpaceModel.id == env.space)
                .values(require_mention=False)
            )
        worker = app.autonomous_groups
        try:
            first = replace(original, message_id="wire-a", text=A, mentions_bot=False)
            assert (await app.processor.handle(first, sender)).reason == "ordinary_deferred"
            await asyncio.wait_for(arrived[1].wait(), 10)
            key = next(iter(worker._states))
            for message_id, text in (("wire-b", B), ("wire-c", C)):
                assert (
                    await app.processor.handle(
                        replace(first, message_id=message_id, text=text), sender
                    )
                ).reason == "ordinary_deferred"
            release[1].set()
            await asyncio.wait_for(arrived[2].wait(), 10)
            async with database.read_session() as reader:
                events = {
                    row.content: row.id
                    for row in await reader.scalars(
                        select(ChatEventModel).where(ChatEventModel.content.in_((A, C)))
                    )
                }
            a = await app.processor.ordinary_admissions.get(events[A])
            c = await app.processor.ordinary_admissions.get(events[C])
            assert c is not None and c.activation_id == a.activation_id
            release[2].set()
            await asyncio.wait_for(_wait_for_worker(worker, key), 10)

            assert len(captured) == 2
            assert C not in json.dumps(captured[0], ensure_ascii=False)
            assert C in json.dumps(captured[1], ensure_ascii=False)
            calls, results = wire_calls_and_results(captured[1], protocol)
            assert calls == ["query_chat_history"]
            assert len(results) == 1 and results[0][0] == "signed-read"
            assert SIGNATURE in json.dumps(captured[1])
            assert captured[0]["tools"] == captured[1]["tools"]
            if protocol == "gemini":
                assert captured[0]["systemInstruction"] == captured[1]["systemInstruction"]
            elif protocol == "anthropic_messages":
                assert captured[0]["system"] == captured[1]["system"]
            elif protocol == "responses":
                assert captured[0]["instructions"] == captured[1]["instructions"]
            else:
                system = [item for item in captured[0]["messages"] if item["role"] == "system"]
                assert system and system == [
                    item for item in captured[1]["messages"] if item["role"] == "system"
                ]
            assert await _work_count(database) == sender.calls == 0
        finally:
            for event in release.values():
                event.set()
            assert not await app.lifecycle._close_entries(app.lifecycle._entries)
