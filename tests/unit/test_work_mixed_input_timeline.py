"""Group observations and directed inputs cross one actual paired boundary."""

import asyncio
import json
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select, text
from tests.conftest import MemorySender
from tests.support.runtime_execution import make_work_resumer
from tests.support.work_session import WorkSession
from tests.unit.test_history_dispatch_ownership import _scene, _tool
from tests.unit.test_work_reporting_runner_gemini_wire import content_parts, gemini_wire

from qq_ai_bot.conversation.projection_models import PromptProjectionModel
from qq_ai_bot.domain.messages import ChatMessage, ChatResponse, SenderIdentity
from qq_ai_bot.identity.canonical_repository import ensure_person
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.llm.gemini import GeminiProvider
from qq_ai_bot.model_runtime.models import ModelCapability
from qq_ai_bot.persistence.models import ChatEventModel
from qq_ai_bot.runtime.work_control import WorkControl
from qq_ai_bot.runtime.work_repository import WorkRepository
from qq_ai_bot.runtime.work_schema_v1 import effects, inputs, journal, work
from qq_ai_bot.runtime.work_session import WorkSession as RuntimeWorkSession
from qq_ai_bot.services.main_agent_turns import MainAgentTurnService
from qq_ai_bot.services.turn_transcript import TurnTranscript


@pytest.mark.asyncio
async def test_successful_ordinary_compaction_keeps_new_ambient_rendering_once(
    database, tmp_path, monkeypatch
):
    from tests.unit.test_ordinary_compaction import summary_response

    provider = FakeLLMProvider()
    held, release = asyncio.Event(), asyncio.Event()
    main, auxiliary = [], []

    def respond(request):
        if request.structured_output:
            auxiliary.append(request)
            return summary_response(request)
        main.append(request)
        if len(main) == 1:
            return _tool("send_message", {"text": "original result delivered"}, "original-send")
        if len(main) == 2:
            original = _tool("get_my_capabilities", {"mode": "summary"}, "read-capabilities")
            return replace(original, content="临时研究正文。" * 6500)
        if len(main) == 3:
            return _tool(
                "send_message",
                {"text": "result retained"},
                "send-result",
            )
        assert len(main) == 4
        return ChatResponse("NO_REPLY", 0)

    provider._responder = respond
    env, harness, chat, _state, inbound = await _scene(database, tmp_path, provider)
    chat._tools.social_service = env.service
    snapshot = chat._runtime_config.snapshot

    async def low_soft_snapshot(*args, **kwargs):
        config = await snapshot(*args, **kwargs)
        return replace(config, context=replace(config.context, compaction_window_tokens=22000))

    monkeypatch.setattr(chat._runtime_config, "snapshot", low_soft_snapshot)
    client, wires = gemini_wire(SimpleNamespace(provider=provider, runner=chat.runtime.runner))
    chat._models = chat.runtime.runner._models
    catalog = chat._models._router.catalog
    profile = next(iter(catalog.profiles.values()))
    catalog.profiles[profile.id] = profile.model_copy(
        update={"capabilities": profile.capabilities | {ModelCapability.STRUCTURED_OUTPUT}}
    )
    complete = provider.complete

    async def held_first(request):
        if len(provider.requests) == 1:
            held.set()
            await release.wait()
        return await complete(request)

    provider.complete = held_first
    running = asyncio.create_task(harness.processor.handle(inbound, MemorySender()))
    ambient_text = "ambient-after-paid-compaction-原观察须保留"
    try:
        await asyncio.wait_for(held.wait(), 5)
        async with database.sessions() as writer, writer.begin():
            other_person = await ensure_person(writer, "10002")
        ambient = replace(
            inbound,
            message_id="ambient-during-first-http",
            text=ambient_text,
            sender=SenderIdentity("10002"),
            person_id=other_person,
            mentions_bot=False,
        )
        observed = await harness.processor.handle(ambient, MemorySender())
        assert observed.reason == "group_observed"
        release.set()
        result = await asyncio.wait_for(running, 15)
        assert result.reason == "chat"
        assert len(main) == 4 and len(auxiliary) == 1
        main_wires = [
            wire
            for wire, request in zip(wires, provider.requests, strict=True)
            if not request.structured_output
        ]
        assert [
            json.dumps(wire, ensure_ascii=False).count(ambient_text) for wire in main_wires
        ] == [0, 0, 1, 1]
        assert main[0].request_chain_id == main[1].request_chain_id
        assert main[1].request_chain_id != main[2].request_chain_id == main[3].request_chain_id
        for field in ("systemInstruction", "tools", "toolConfig", "generationConfig"):
            assert all(wire[field] == main_wires[0][field] for wire in main_wires[1:])
        assert content_parts(main_wires[3])[: len(content_parts(main_wires[2]))] == content_parts(
            main_wires[2]
        )
        sent = [arguments for action, arguments in env.bot.calls if action == "send_group_msg"]
        assert len(sent) == 2
        assert sum("original result delivered" in str(arguments) for arguments in sent) == 1
        assert sum("result retained" in str(arguments) for arguments in sent) == 1
        async with database.sessions() as reader:
            assert not (await reader.execute(select(work))).all()
            event = await reader.scalar(
                select(ChatEventModel).where(
                    ChatEventModel.platform_message_id == ambient.message_id
                )
            )
            projection = (await reader.scalars(select(PromptProjectionModel))).one()
            delivered = (
                await reader.scalars(
                    select(ChatEventModel).where(
                        ChatEventModel.direction == "outbound",
                        ChatEventModel.canonical_conversation_id == env.context.conversation_id,
                    )
                )
            ).all()
        assert len(delivered) == 2
        assert event.id in {
            event_id
            for entry in json.loads(projection.payload_json)
            for event_id in entry.get("event_ids", [])
        }
    finally:
        release.set()
        if not running.done():
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
        await client.aclose()


@pytest.mark.asyncio
async def test_two_steers_two_ambient_and_later_followup_keep_original_work_receipt(
    database, tmp_path
):
    provider = FakeLLMProvider()
    held, release = asyncio.Event(), asyncio.Event()

    def respond(request):
        number = len(provider.requests)
        if number == 1:
            return _tool(
                "task_control",
                {
                    "action": "accept",
                    "goal": "记录本轮结果",
                    "output_kind": "state_change",
                    "reporting": "quiet",
                },
                "accept",
            )
        if number == 2:
            return _tool(
                "update_short_state",
                {"slot": 1, "text": "accepted-original", "expected_revision": 1},
                "original-write",
            )
        if number == 3:
            return _tool(
                "update_short_state",
                {"slot": 1, "text": "stale-plan", "expected_revision": 2},
                "stale-write",
            )
        if number == 4:
            return _tool(
                "task_control",
                {
                    "action": "update",
                    "context_note": {
                        "version": 1,
                        "facts": [{"text": "原结果已保存", "refs": ["goal"]}],
                        "unresolved": [],
                        "next_steps": [],
                    },
                },
                "note",
            )
        if number == 5:
            return _tool("send_message", {"text": "followup received"}, "followup-send")
        if number == 6:
            return ChatResponse("followup handled", 0)
        assert number == 7, "must not replay an original model or business call"
        return _tool("task_control", {"action": "complete"}, "complete")

    provider._responder = respond
    env, harness, chat, state, inbound = await _scene(database, tmp_path, provider, request_limit=4)
    client, wires = gemini_wire(SimpleNamespace(provider=provider, runner=chat.runtime.runner))
    chat._models = chat.runtime.runner._models
    complete = provider.complete

    async def blocked_complete(request):
        if len(provider.requests) == 2:
            held.set()
            await release.wait()
        return await complete(request)

    provider.complete = blocked_complete
    async with database.sessions() as session, session.begin():
        other_person = await ensure_person(session, "10002")
    observed = [
        "steer-one-改为核对",
        "ambient-one-群聊资料",
        "steer-two-保留原结果",
        "ambient-two-补充线索",
    ]
    running = asyncio.create_task(harness.processor.handle(inbound, MemorySender()))
    try:
        await asyncio.wait_for(held.wait(), 5)
        for index, text in enumerate(observed):
            directed = index % 2 == 0
            message = replace(
                inbound,
                message_id=f"mixed-{index}",
                text=text,
                sender=SenderIdentity("10001" if directed else "10002"),
                person_id=env.person if directed else other_person,
                mentions_bot=directed,
            )
            result = await asyncio.wait_for(harness.processor.handle(message, MemorySender()), 5)
            assert result.reason == ("work_input_queued" if directed else "group_observed")
        release.set()
        result = await asyncio.wait_for(running, 10)
        assert result.reason == "chat" and len(provider.requests) == 4
        # Gemini checkpoints are opaque in ChatRequest; inspect actual wire.
        text = json.dumps(wires[3], ensure_ascii=False)
        assert {item: text.count(item) for item in observed} == {item: 1 for item in observed}
        assert [text.index(item) for item in observed] == sorted(
            text.index(item) for item in observed
        )
        assert "original-write" in text
        assert content_parts(wires[3])[: len(content_parts(wires[2]))] == content_parts(wires[2])
        assert "stale-plan" not in state.snapshot()[0]["text"]
        assert state.snapshot()[0]["text"] == "accepted-original"
        # Every actual response call still has exactly one paired result, even
        # the old plan rejected because directed input arrived during HTTP.
        call_ids = [
            part["functionCall"]["id"]
            for _, part in content_parts(wires[3])
            if "functionCall" in part
        ]
        result_ids = [
            part["functionResponse"]["id"]
            for _, part in content_parts(wires[3])
            if "functionResponse" in part
        ]
        assert set(call_ids) == set(result_ids)
        assert len(call_ids) == len(set(call_ids)) == len(result_ids)
        repository = WorkRepository(database)
        async with database.sessions() as reader:
            original = (await reader.execute(select(work))).mappings().one()
            original_receipts = [
                dict(row)
                for row in (
                    await reader.execute(select(effects).where(effects.c.work_id == original["id"]))
                ).mappings()
            ]
            original_inputs = [
                dict(row)
                for row in (
                    await reader.execute(select(inputs).where(inputs.c.work_id == original["id"]))
                ).mappings()
            ]
        assert original["state"] == "queued" and original["model_requests"] == 4
        assert original["tool_calls"] == 1
        assert len(original_receipts) == 1 and original_receipts[0]["state"] == "accepted"
        assert len(original_inputs) == 2 and all(
            row["state"] == "consumed" for row in original_inputs
        )
        followup = replace(inbound, message_id="mixed-followup", text="followup-下一轮继续讨论")
        ordinary = await harness.processor.handle(followup, MemorySender())
        assert ordinary.reason == "chat" and len(provider.requests) == 6
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
        await resumer.resume(await repository.get(original["id"]))
        assert resumer.last_error is None and len(provider.requests) == 7
        resumed_text = "\n".join(str(message.content) for message in provider.requests[-1].messages)
        assert {item: resumed_text.count(item) for item in [*observed, followup.text]} == {
            item: 1 for item in [*observed, followup.text]
        }
        assert all(request.tools == provider.requests[0].tools for request in provider.requests)
        assert all(wire["tools"] == wires[0]["tools"] for wire in wires)
        saved = await repository.get(original["id"])
        assert saved["model_requests"] == 5 and saved["tool_calls"] == 1
        async with database.sessions() as reader:
            receipts = [
                dict(row)
                for row in (
                    await reader.execute(select(effects).where(effects.c.work_id == original["id"]))
                ).mappings()
            ]
            saved_inputs = [
                dict(row)
                for row in (
                    await reader.execute(select(inputs).where(inputs.c.work_id == original["id"]))
                ).mappings()
            ]
            events = (
                await reader.scalars(
                    select(ChatEventModel).where(
                        ChatEventModel.canonical_conversation_id == env.context.conversation_id,
                        ChatEventModel.content.in_(observed),
                    )
                )
            ).all()
        assert receipts == original_receipts
        assert saved_inputs == original_inputs
        assert len(events) == 4
    finally:
        release.set()
        if not running.done():
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
        await client.aclose()


@pytest.mark.asyncio
async def test_ambient_candidate_cancelled_before_admission_is_observed_after_reopen(
    database, tmp_path
):
    provider = FakeLLMProvider()
    response_held, response_release, candidate_held = (
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
    )

    def respond(request):
        number = len(provider.requests)
        if number == 1:
            return _tool(
                "task_control",
                {
                    "action": "accept",
                    "goal": "保留已有结果",
                    "output_kind": "state_change",
                    "reporting": "quiet",
                },
                "accept",
            )
        if number == 2:
            return _tool(
                "update_short_state",
                {"slot": 1, "text": "accepted-before-cancel", "expected_revision": 1},
                "original-write",
            )
        if number == 3:
            return _tool("task_control", {"action": "update", "reason": "继续核对"}, "status")
        if number == 4:
            return _tool("send_message", {"text": "new input received"}, "fresh-answer")
        assert number == 5, "no replay or candidate HTTP is allowed"
        return ChatResponse("internal final", 0)

    provider._responder = respond
    _env, harness, chat, state, inbound = await _scene(database, tmp_path, provider)
    client, wires = gemini_wire(SimpleNamespace(provider=provider, runner=chat.runtime.runner))
    chat._models = chat.runtime.runner._models
    complete = provider.complete

    async def held_complete(request):
        if len(provider.requests) == 2:
            response_held.set()
            await response_release.wait()
        return await complete(request)

    provider.complete = held_complete
    executor = chat.runtime.runner._models
    execute = executor.execute

    async def hold_candidate(*args, **kwargs):
        if len(provider.requests) == 3:
            candidate_held.set()
            await asyncio.Event().wait()
        return await execute(*args, **kwargs)

    executor.execute = hold_candidate
    running = asyncio.create_task(harness.processor.handle(inbound, MemorySender()))
    ambient_text = "ambient-unadmitted-仅候选未派发"
    try:
        await asyncio.wait_for(response_held.wait(), 5)
        async with database.sessions() as session, session.begin():
            other_person = await ensure_person(session, "10002")
        ambient = replace(
            inbound,
            message_id="ambient-unadmitted",
            text=ambient_text,
            sender=SenderIdentity("10002"),
            person_id=other_person,
            mentions_bot=False,
        )
        observation = await harness.processor.handle(ambient, MemorySender())
        assert observation.reason == "group_observed"
        response_release.set()
        await asyncio.wait_for(candidate_held.wait(), 5)
        # Boundary has been prepared, but the executor never admitted request4.
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)
        assert len(provider.requests) == len(wires) == 3
        repository = WorkRepository(database)
        async with database.sessions() as reader:
            saved_work = (await reader.execute(select(work))).mappings().one()
            receipt_before = [
                dict(row) for row in (await reader.execute(select(effects))).mappings()
            ]
            projection = (await reader.scalars(select(PromptProjectionModel))).one()
            assert ambient_text not in projection.payload_json
            original_event = await reader.scalar(
                select(ChatEventModel).where(ChatEventModel.content == ambient_text)
            )
            assert original_event is not None
            event_id = original_event.id
        assert saved_work["model_requests"] == 3 and saved_work["tool_calls"] == 1
        assert len(receipt_before) == 1 and receipt_before[0]["state"] == "accepted"
        assert state.snapshot()[0]["text"] == "accepted-before-cancel"
        executor.execute = execute
        await database.close()
        chat.runtime.main_turns = MainAgentTurnService(
            chat._prompt_composer, chat.runtime.runner, database
        )
        followup = replace(inbound, message_id="after-unadmitted", text="新的正常讨论")
        result = await harness.processor.handle(followup, MemorySender())
        assert result.reason == "chat"
        assert len(provider.requests) == len(wires) == 5
        body = json.dumps(wires[3], ensure_ascii=False)
        assert body.count(ambient_text) == 1
        assert f"#{event_id}>" in body
        assert wires[3]["tools"] == wires[0]["tools"]
        async with database.sessions() as reader:
            receipt_after = [
                dict(row) for row in (await reader.execute(select(effects))).mappings()
            ]
            unchanged = await repository.get(saved_work["id"])
        assert receipt_after == receipt_before
        assert unchanged["model_requests"] == 3 and unchanged["tool_calls"] == 1
    finally:
        executor.execute = execute
        response_release.set()
        if not running.done():
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
        await client.aclose()


@pytest.mark.asyncio
async def test_boundary_journal_writer_failure_rolls_back_projection_before_any_http(
    database, tmp_path, monkeypatch
):
    provider = FakeLLMProvider()
    held, release = asyncio.Event(), asyncio.Event()
    paired_guard, session_owner = [], []

    def respond(request):
        number = len(provider.requests)
        if number == 1:
            return _tool(
                "task_control",
                {
                    "action": "accept",
                    "goal": "保留既有写入",
                    "output_kind": "state_change",
                    "reporting": "quiet",
                },
                "accept",
            )
        if number == 2:
            return _tool(
                "update_short_state",
                {"slot": 1, "text": "accepted-before-writer-failure", "expected_revision": 1},
                "original-write",
            )
        if number == 3:
            return _tool(
                "task_control",
                {
                    "action": "update",
                    "context_note": {
                        "version": 1,
                        "facts": [{"text": "原结果已保存", "refs": ["goal"]}],
                        "unresolved": [],
                        "next_steps": [],
                    },
                },
                "note",
            )
        raise AssertionError("failed journal transaction must not dispatch request4")

    provider._responder = respond
    _env, harness, chat, state, inbound = await _scene(database, tmp_path, provider)
    client, wires = gemini_wire(SimpleNamespace(provider=provider, runner=chat.runtime.runner))
    chat._models = chat.runtime.runner._models
    complete = provider.complete

    async def blocked_complete(request):
        if len(provider.requests) == 2:
            held.set()
            await release.wait()
        return await complete(request)

    provider.complete = blocked_complete
    save = WorkSession.save

    async def record_save(self, phase, *args, **kwargs):
        await save(self, phase, *args, **kwargs)
        if phase == "paired" and len(provider.requests) == 3:
            session_owner.append(self)
            paired_guard.append(deepcopy(self.source_guard.snapshot()))

    monkeypatch.setattr(RuntimeWorkSession, "save", record_save)
    running = asyncio.create_task(harness.processor.handle(inbound, MemorySender()))
    ambient_text = "ambient-writer-failure-不能提前消费"
    try:
        await asyncio.wait_for(held.wait(), 5)
        async with database.sessions() as writer, writer.begin():
            # Real transaction fault after projection publication but before
            # the original dispatched journal upsert can commit.
            await writer.execute(
                text(
                    "CREATE TRIGGER fail_dispatched_journal "
                    "BEFORE UPDATE ON runtime_work_journal "
                    "WHEN NEW.phase='dispatched' BEGIN "
                    "SELECT RAISE(ABORT,'injected_dispatched_journal_failure'); END"
                )
            )
            other_person = await ensure_person(writer, "10002")
        ambient = replace(
            inbound,
            message_id="ambient-writer-failure",
            text=ambient_text,
            sender=SenderIdentity("10002"),
            person_id=other_person,
            mentions_bot=False,
        )
        observation = await harness.processor.handle(ambient, MemorySender())
        assert observation.reason == "group_observed"
        async with database.sessions() as reader:
            original_projection = (await reader.scalars(select(PromptProjectionModel))).one()
            original_projection_data = (
                original_projection.epoch_id,
                original_projection.revision,
                original_projection.payload_json,
            )
            original_effects = [
                dict(row) for row in (await reader.execute(select(effects))).mappings()
            ]
        release.set()
        result = await asyncio.wait_for(running, 10)
        assert result.reason == "chat"  # Original Work supervisor owns the protected pause.
        assert len(provider.requests) == len(wires) == 3
        assert state.snapshot()[0]["text"] == "accepted-before-writer-failure"
        assert session_owner and paired_guard
        assert session_owner[-1].source_guard.snapshot() == paired_guard[-1]
        async with database.sessions() as reader:
            projection = (await reader.scalars(select(PromptProjectionModel))).one()
            assert (
                projection.epoch_id,
                projection.revision,
                projection.payload_json,
            ) == original_projection_data
            assert ambient_text not in projection.payload_json
            receipts = [dict(row) for row in (await reader.execute(select(effects))).mappings()]
            row = (await reader.execute(select(journal))).mappings().one()
            assert row["phase"] == "paired" and ambient_text not in row["payload_json"]
            saved_work = (await reader.execute(select(work))).mappings().one()
            saved_inputs = (await reader.execute(select(inputs))).mappings().all()
        assert receipts == original_effects
        assert saved_work["model_requests"] == 4 and saved_work["tool_calls"] == 1
        assert saved_inputs == []
    finally:
        release.set()
        if not running.done():
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
        async with database.sessions() as writer, writer.begin():
            await writer.execute(text("DROP TRIGGER IF EXISTS fail_dispatched_journal"))
        await client.aclose()


@pytest.mark.asyncio
async def test_committed_boundary_http_unknown_keeps_exact_native_checkpoint_and_receipt(
    database, tmp_path, monkeypatch
):
    provider = FakeLLMProvider()
    held, release = asyncio.Event(), asyncio.Event()
    dispatched_sessions = []

    def respond(request):
        number = len(provider.requests)
        if number == 1:
            return _tool(
                "task_control",
                {
                    "action": "accept",
                    "goal": "保留旧效果",
                    "output_kind": "state_change",
                    "reporting": "quiet",
                },
                "accept",
            )
        if number == 2:
            return _tool(
                "update_short_state",
                {"slot": 1, "text": "accepted-before-http-unknown", "expected_revision": 1},
                "original-write",
            )
        if number == 3:
            return _tool(
                "task_control",
                {
                    "action": "update",
                    "context_note": {
                        "version": 1,
                        "facts": [{"text": "原结果已保存", "refs": ["goal"]}],
                        "unresolved": [],
                        "next_steps": [],
                    },
                },
                "note",
            )
        assert number == 4
        # MockTransport received the actual fourth wire. The peer may have
        # accepted it; no response arrives, so this is not a pre-HTTP failure.
        raise httpx.ReadTimeout("synthetic response unavailable")

    provider._responder = respond
    env, harness, chat, state, inbound = await _scene(database, tmp_path, provider)
    client, wires = gemini_wire(SimpleNamespace(provider=provider, runner=chat.runtime.runner))
    chat._models = chat.runtime.runner._models
    complete = provider.complete

    async def blocked_complete(request):
        if len(provider.requests) == 2:
            held.set()
            await release.wait()
        return await complete(request)

    provider.complete = blocked_complete
    save = WorkSession.save

    async def record_save(self, phase, *args, **kwargs):
        await save(self, phase, *args, **kwargs)
        if phase == "dispatched" and len(provider.requests) == 3:
            dispatched_sessions.append(self)

    monkeypatch.setattr(RuntimeWorkSession, "save", record_save)
    running = asyncio.create_task(harness.processor.handle(inbound, MemorySender()))
    ambient_text = "ambient-http-unknown-已派发但未收到回复"
    repository = WorkRepository(database)
    lease = None
    try:
        await asyncio.wait_for(held.wait(), 5)
        async with database.sessions() as writer, writer.begin():
            other_person = await ensure_person(writer, "10002")
        observation = await harness.processor.handle(
            replace(
                inbound,
                message_id="ambient-http-unknown",
                text=ambient_text,
                sender=SenderIdentity("10002"),
                person_id=other_person,
                mentions_bot=False,
            ),
            MemorySender(),
        )
        assert observation.reason == "group_observed"
        release.set()
        result = await asyncio.wait_for(running, 10)
        assert result.reason == "chat"
        assert len(provider.requests) == len(wires) == 4
        assert json.dumps(wires[3], ensure_ascii=False).count(ambient_text) == 1
        assert state.snapshot()[0]["text"] == "accepted-before-http-unknown"
        assert dispatched_sessions
        owner = dispatched_sessions[-1]
        async with database.sessions() as reader:
            projection = (await reader.scalars(select(PromptProjectionModel))).one()
            assert projection.payload_json.count(ambient_text) == 1
            row = (await reader.execute(select(journal))).mappings().one()
            assert row["phase"] == "dispatched"
            receipts = [dict(item) for item in (await reader.execute(select(effects))).mappings()]
            saved = (await reader.execute(select(work))).mappings().one()
        assert len(receipts) == 1 and receipts[0]["state"] == "accepted"
        assert saved["model_requests"] == 4 and saved["tool_calls"] == 1
        await database.close()
        # Read-only protocol restoration under a new legitimate scope lease.
        # This does not resume execution or issue another HTTP/tool request.
        lease = await repository.acquire(env.context.conversation_id, saved["generation"])
        assert lease is not None

        async def validate():
            await repository.validate(lease)

        control = WorkControl(
            repository,
            lease,
            owner.control.source_key,
            deepcopy(owner.control.source),
            validate,
            current=dict(saved),
        )
        control.bind_context_access(owner.control.context_access)
        restored_session = WorkSession(control, owner.contract)
        control.session = restored_session
        restored = await restored_session.restore(
            TurnTranscript((ChatMessage("user", "fresh wakeup must not replace unknown wire"),))
        )
        assert restored_session.uses_recovery_transcript
        assert restored_session.source_guard.snapshot() == owner.source_guard.snapshot()
        sequence = restored.request()
        actual = provider.requests[3]
        adapter = GeminiProvider(
            base_url="https://gemini.invalid",
            api_key="synthetic",
            timeout_seconds=2,
            max_retries=0,
            client=client,
        )
        rebuilt = adapter._build_payload(
            replace(
                actual,
                messages=sequence.messages,
                continuation=sequence.continuation,
                continuation_items=sequence.items,
                request_chain_id=restored.chain_id,
            )
        )
        assert rebuilt == wires[3]
        assert "fresh wakeup must not replace unknown wire" not in json.dumps(rebuilt)
        assert "signature-original-write" in json.dumps(rebuilt)
        async with database.sessions() as reader:
            assert [
                dict(item) for item in (await reader.execute(select(effects))).mappings()
            ] == receipts
        current = await repository.get(saved["id"])
        assert current["model_requests"] == 4 and current["tool_calls"] == 1
        assert len(provider.requests) == 4
    finally:
        release.set()
        if not running.done():
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
        if lease is not None:
            await repository.release(lease)
        await client.aclose()
