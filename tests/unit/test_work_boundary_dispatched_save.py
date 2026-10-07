"""A prepared observation must not be consumed by a failed dispatch save."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy import text as sql_text
from tests.conftest import MemorySender
from tests.support.runtime_execution import make_work_resumer
from tests.support.work_session import WorkSession
from tests.unit.test_history_dispatch_ownership import _scene, _tool
from tests.unit.test_work_reporting_runner_gemini_wire import gemini_wire

from qq_ai_bot.conversation.projection_models import PromptProjectionModel
from qq_ai_bot.domain.messages import SenderIdentity
from qq_ai_bot.identity.canonical_repository import ensure_person
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.runtime.work_repository import WorkConflict, WorkRepository
from qq_ai_bot.runtime.work_schema_v1 import effects, journal, work
from qq_ai_bot.runtime.work_session import WorkSession as RuntimeWorkSession
from qq_ai_bot.services.main_agent_turns import MainAgentTurnService


async def test_dispatch_save_failure_does_not_publish_ambient_observation(
    database, tmp_path, monkeypatch
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
                    "goal": "保留已有结果",
                    "output_kind": "state_change",
                    "reporting": "quiet",
                },
                "accept",
            )
        if number == 2:
            return _tool(
                "update_short_state",
                {
                    "slot": 1,
                    "text": "accepted-before-save-failure",
                    "expected_revision": 1,
                },
                "original-write",
            )
        assert number == 3
        return _tool("task_control", {"action": "update", "reason": "继续核对"}, "status")

    provider._responder = respond
    _env, harness, chat, state, inbound = await _scene(database, tmp_path, provider)
    client, wires = gemini_wire(SimpleNamespace(provider=provider, runner=chat.runtime.runner))
    chat._models = chat.runtime.runner._models
    complete = provider.complete

    async def held_complete(request):
        if len(provider.requests) == 2:
            held.set()
            await release.wait()
        return await complete(request)

    provider.complete = held_complete
    save = WorkSession.save

    async def fail_dispatched(self, phase, *args, **kwargs):
        if phase == "dispatched" and len(provider.requests) == 3:
            raise WorkConflict("injected_dispatch_save_failure")
        return await save(self, phase, *args, **kwargs)

    monkeypatch.setattr(RuntimeWorkSession, "save", fail_dispatched)
    running = asyncio.create_task(harness.processor.handle(inbound, MemorySender()))
    text = "ambient-before-failed-dispatch-save"
    try:
        await asyncio.wait_for(held.wait(), 5)
        async with database.sessions() as session, session.begin():
            other = await ensure_person(session, "10002")
        result = await harness.processor.handle(
            replace(
                inbound,
                message_id="ambient-save-fault",
                text=text,
                sender=SenderIdentity("10002"),
                person_id=other,
                mentions_bot=False,
            ),
            MemorySender(),
        )
        assert result.reason == "group_observed"
        release.set()
        await asyncio.wait_for(running, 10)
        assert len(provider.requests) == len(wires) == 3
        assert state.snapshot()[0]["text"] == "accepted-before-save-failure"
        async with database.sessions() as session:
            projection = (await session.scalars(select(PromptProjectionModel))).one()
            assert text not in projection.payload_json
    finally:
        release.set()
        if not running.done():
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
        await client.aclose()


@pytest.mark.parametrize("failure", ["save_entry", "writer"])
async def test_business_resume_initial_selection_is_atomic_with_dispatched_journal(
    database, tmp_path, monkeypatch, failure
):
    provider = FakeLLMProvider()
    provider._responder = lambda request: (
        _tool(
            "task_control",
            {
                "action": "accept",
                "goal": "保留原工作",
                "output_kind": "state_change",
                "reporting": "quiet",
            },
            "accept",
        )
        if len(provider.requests) == 1
        else _tool(
            "update_short_state",
            {
                "slot": 1,
                "text": "original-accepted-state",
                "expected_revision": 1,
            },
            "original-write",
        )
    )
    env, harness, chat, state, inbound = await _scene(database, tmp_path, provider, request_limit=2)
    result = await harness.processor.handle(inbound, MemorySender())
    assert result.reason == "chat" and len(provider.requests) == 2
    repository = WorkRepository(database)
    async with database.sessions() as reader:
        item = dict((await reader.execute(select(work))).mappings().one())
        original_journal = dict((await reader.execute(select(journal))).mappings().one())
        original_effects = [dict(row) for row in (await reader.execute(select(effects))).mappings()]
        projection = (await reader.scalars(select(PromptProjectionModel))).one()
        original_projection = (projection.epoch_id, projection.revision, projection.payload_json)
    assert item["state"] == "queued" and original_journal["phase"] == "paired"
    async with database.sessions() as session, session.begin():
        other = await ensure_person(session, "10002")
    ambient = "fresh-chat-must-not-be-observed-by-failed-resume"
    observed = await harness.processor.handle(
        replace(
            inbound,
            message_id="resume-unadmitted",
            text=ambient,
            sender=SenderIdentity("10002"),
            person_id=other,
            mentions_bot=False,
        ),
        MemorySender(),
    )
    assert observed.reason == "group_observed"
    await database.close()
    chat.runtime.main_turns = MainAgentTurnService(
        chat._prompt_composer, chat.runtime.runner, database
    )
    save = WorkSession.save
    if failure == "save_entry":

        async def fail_initial(self, phase, *args, **kwargs):
            if phase == "dispatched":
                raise WorkConflict("injected_resume_dispatch_save_failure")
            return await save(self, phase, *args, **kwargs)

        monkeypatch.setattr(RuntimeWorkSession, "save", fail_initial)
    else:
        async with database.immediate_session() as writer:
            await writer.execute(
                sql_text(
                    "CREATE TRIGGER fail_resume_journal BEFORE UPDATE ON runtime_work_journal "
                    "WHEN NEW.phase='dispatched' BEGIN "
                    "SELECT RAISE(ABORT,'resume_save_failure'); END"
                )
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
    await resumer.resume(item)
    assert len(provider.requests) == 2
    assert state.snapshot()[0]["text"] == "original-accepted-state"
    async with database.sessions() as reader:
        projection = (await reader.scalars(select(PromptProjectionModel))).one()
        assert (
            projection.epoch_id,
            projection.revision,
            projection.payload_json,
        ) == original_projection
        saved = dict((await reader.execute(select(journal))).mappings().one())
        assert saved == original_journal
        assert [
            dict(row) for row in (await reader.execute(select(effects))).mappings()
        ] == original_effects
        current = dict((await reader.execute(select(work))).mappings().one())
        assert current["model_requests"] == item["model_requests"] + 1
        assert current["tool_calls"] == item["tool_calls"]
