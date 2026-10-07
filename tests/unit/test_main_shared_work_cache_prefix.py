"""Actual Main composition retains shared H across W1 -> W2 -> W1."""

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from tests.conftest import MemorySender
from tests.support.runtime_execution import make_work_resumer
from tests.unit.test_history_dispatch_ownership import _scene, _tool
from tests.unit.test_work_reporting_runner_gemini_wire import gemini_wire

from qq_ai_bot.conversation.frozen_fragments import FrozenFragments
from qq_ai_bot.conversation.projection_models import PromptProjectionModel
from qq_ai_bot.domain.messages import ChatResponse
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.runtime.work_repository import WorkRepository
from qq_ai_bot.runtime.work_schema_v1 import work
from qq_ai_bot.services.main_agent_turns import MainAgentTurnService


@pytest.mark.asyncio
async def test_real_main_shared_history_switches_work_with_one_fixed_gemini_manifest(
    database, tmp_path
):
    provider = FakeLLMProvider()

    def respond(request):
        number = len(provider.requests)
        if number in {1, 3}:
            label = "W1" if number == 1 else "W2"
            return _tool(
                "task_control",
                {
                    "action": "accept",
                    "goal": f"{label}核对合成资料",
                    "output_kind": "answer",
                    "reporting": "quiet",
                },
                f"accept-{label}",
            )
        if number in {2, 4}:
            label = "W1" if number == 2 else "W2"
            return _tool(
                "task_control",
                {
                    "action": "update",
                    "context_note": {
                        "version": 1,
                        "facts": [{"text": f"{label}已观察线索", "refs": ["goal"]}],
                        "unresolved": [],
                        "next_steps": [],
                    },
                },
                f"note-{label}",
            )
        assert number == 5, "must not replay either earlier accept/note"
        return ChatResponse("W1继续核对完毕。", 0)

    provider._responder = respond
    env, harness, chat, _, inbound = await _scene(database, tmp_path, provider, request_limit=2)
    client, wires = gemini_wire(SimpleNamespace(provider=provider, runner=chat.runtime.runner))
    chat._models = chat.runtime.runner._models
    try:
        # Compare the fixed Provider view; hidden execution schemas remain in
        # the separate frozen API and do not alter this public cache prefix.
        declared = await chat.runtime.runner.main_contract.model_definitions()
        first = await harness.processor.handle(replace(inbound, text="请进行W1"), MemorySender())
        assert first.reason == "chat" and len(provider.requests) == 2
        async with database.sessions() as reader:
            first_view = (await reader.scalars(select(PromptProjectionModel))).one()
            old_h = FrozenFragments.load(json.loads(first_view.payload_json))
            first_work_id = await reader.scalar(select(work.c.id))
        second = await harness.processor.handle(
            replace(inbound, message_id="work-two-input", text="请另进行W2"), MemorySender()
        )
        assert second.reason == "chat" and len(provider.requests) == 4
        repository = WorkRepository(database)
        async with database.sessions() as reader:
            ids = (await reader.scalars(select(work.c.id))).all()
            second_view = (await reader.scalars(select(PromptProjectionModel))).one()
            current_h = FrozenFragments.load(json.loads(second_view.payload_json))
        assert len(ids) == 2
        second_work_id = next(identity for identity in ids if identity != first_work_id)
        first_work = await repository.get(first_work_id)
        second_work = await repository.get(second_work_id)
        assert first_work["state"] == second_work["state"] == "queued"
        assert first_work["model_requests"] == second_work["model_requests"] == 2
        assert current_h.items[: len(old_h.items)] == old_h.items
        assert "W1已观察线索" in second_view.payload_json
        # Reopen the actual SQLite pool and Main service, then use the original
        # WorkResumer entry rather than hand-constructing a public prefix.
        await database.close()
        chat.runtime.main_turns = MainAgentTurnService(
            chat._prompt_composer, chat.runtime.runner, database
        )
        resumer = make_work_resumer(
            repository,
            ledger=harness.ledger,
            scopes=chat._conversation_scopes,
            turns=chat._turn_coordinator,
            router=env.router,
            config=chat._runtime_config,
            generate_self=chat.generate_self_initiative,
            generate_wakeup=chat.generate_main_agent_wakeup,
            validate_snapshot=chat.validate_turn_snapshot,
            run_effect=chat.run_effect,
            bindings=chat.runtime.bindings,
        )
        await resumer.resume(first_work)
        assert resumer.last_error is None
        assert len(provider.requests) == len(wires) == 5
        resumed = provider.requests[-1]
        public_positions = [resumed.messages.index(message) for message in current_h.messages()]
        assert public_positions == sorted(public_positions)
        assert resumed.tools == declared
        assert all(request.tools == declared for request in provider.requests)
        assert all(body["tools"] == wires[0]["tools"] for body in wires)
        assert all(body["systemInstruction"] == wires[0]["systemInstruction"] for body in wires)
        text = "\n".join(str(message.content) for message in resumed.messages)
        assert "请进行W1" in text and "请另进行W2" in text
        assert "W1已观察线索" in text and "W2已观察线索" in text
        assert "work_current_material" in text and "W1核对合成资料" in text
        assert not any(message.tool_calls for message in resumed.messages)
        assert resumed.request_chain_id not in {
            provider.requests[0].request_chain_id,
            provider.requests[2].request_chain_id,
        }
        saved_first, saved_second = (
            await repository.get(first_work_id),
            await repository.get(second_work_id),
        )
        assert saved_first["model_requests"] == 3
        assert saved_second["model_requests"] == 2
        assert saved_first["tool_calls"] == saved_second["tool_calls"] == 0
    finally:
        await client.aclose()
