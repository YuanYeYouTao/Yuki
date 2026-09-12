"""Durable work race and recovery contracts against a real SQLite database."""

import asyncio
import json
from itertools import pairwise

import pytest
from sqlalchemy import select, update
from tests.support.social_identity_cases import social_env

from qq_ai_bot.runtime.work_control import WorkControl
from qq_ai_bot.runtime.work_repository import WorkConflict, WorkRepository
from qq_ai_bot.runtime.work_schema_v1 import effects, inputs, scope


@pytest.mark.asyncio
async def test_work_fencing_input_consumption_and_late_receipts(database, tmp_path):
    env = await social_env(database, tmp_path)
    conversation_id = env.context.conversation_id
    a, b = WorkRepository(database), WorkRepository(database)
    claims = await asyncio.gather(a.acquire(conversation_id, 1), b.acquire(conversation_id, 1))
    assert sum(item is not None for item in claims) == 1
    lease = next(item for item in claims if item is not None)
    item = await a.accept(lease, source_key="drawing", source={"actor": "original"}, goal="draw")
    repeated = await b.accept(
        lease, source_key="drawing", source={"actor": "original"}, goal="draw"
    )
    assert item["id"] == repeated["id"]
    with pytest.raises(WorkConflict):
        await a.accept(lease, source_key="drawing", source={"actor": "other"}, goal="draw")
    ids = await asyncio.gather(
        *[repo.enqueue(conversation_id, 1, "new-input", kind="message") for repo in (a, b)]
    )
    assert ids[0] == ids[1]
    assert await a.valid(lease)  # New input never revokes a running activation.
    await a.stage(lease, [ids[0]], "attempt")
    with pytest.raises(WorkConflict):
        await b.stage(lease, [ids[0]], "second-attempt")
    await a.consume(lease, "attempt")
    await b.consume(lease, "attempt")
    await a.checkpoint(lease, item["id"], {"pending_run_id": "child"}, models=2, tools=1)
    assert await a.prepare_effect(lease, item["id"], "send-once", "message")
    assert not await b.prepare_effect(lease, item["id"], "send-once", "message")
    await a.cancel(conversation_id)
    assert not await a.valid(lease)
    with pytest.raises(WorkConflict):
        await b.checkpoint(lease, item["id"], {}, models=1)
    # An already accepted send survives cancellation and cannot become retryable.
    await a.record_effect("send-once", "accepted", {"message_id": "real-platform-id"})
    await b.record_effect("send-once", "accepted", {"message_id": "real-platform-id"})
    with pytest.raises(WorkConflict):
        await b.record_effect("send-once", "failed", {})
    persisted = await a.get(item["id"])
    assert persisted["state"] == "cancelled"
    assert persisted["model_requests"] == 2
    assert json.loads(persisted["checkpoint_json"])["pending_run_id"] == "child"
    async with database.sessions() as session:
        assert (await session.execute(select(inputs.c.state))).scalar_one() == "consumed"
        assert (await session.execute(select(effects.c.state))).scalar_one() == "accepted"


@pytest.mark.asyncio
async def test_expired_owner_cannot_release_or_mutate_replacement(database, tmp_path):
    env = await social_env(database, tmp_path)
    key = env.context.conversation_id
    repository = WorkRepository(database)
    old = await repository.acquire(key, 1)
    assert old
    item = await repository.accept(old, source_key="goal", source={}, goal="render")
    async with database.sessions() as session, session.begin():
        await session.execute(
            update(scope).where(scope.c.conversation_id == key).values(lease_until=0)
        )
    new = await WorkRepository(database).acquire(key, 1)
    assert new and new.fence > old.fence
    await repository.release(old)
    assert await repository.valid(new)
    with pytest.raises(WorkConflict):
        await repository.transition(old, item["id"], 1, "completed")
    waiting = await repository.transition(new, item["id"], 1, "waiting_external")
    with pytest.raises(WorkConflict):
        await repository.transition(new, item["id"], 1, "running")
    done = await repository.transition(new, item["id"], waiting["revision"], "completed")
    with pytest.raises(WorkConflict):
        await repository.transition(new, item["id"], done["revision"], "running")
    await repository.cancel(key, generation=2)
    assert await repository.acquire(key, 1) is None
    assert await repository.acquire(key, 2) is None  # Canonical generation is authoritative.
    from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel

    async with database.sessions() as session, session.begin():
        await session.execute(
            update(CanonicalConversationModel)
            .where(CanonicalConversationModel.id == key)
            .values(generation=2)
        )
    assert await repository.acquire(key, 2)


@pytest.mark.asyncio
async def test_progress_continues_work_and_finish_waits_for_delivery(database, tmp_path):
    env = await social_env(database, tmp_path)
    repository = WorkRepository(database)
    lease = await repository.acquire(env.context.conversation_id, 1)
    assert lease
    sent = []

    async def validate():
        assert await repository.valid(lease)

    async def deliver(text, key):
        sent.append(text)
        return {"transport_accepted": True, "message_id": key, "ledger_recorded": True}

    control = WorkControl(repository, lease, "source", {}, validate, deliver)
    assert not json.loads(await control.execute("report_progress", {"text": "starting"}, "p0"))[
        "ok"
    ]
    assert json.loads(
        await control.execute(
            "task_control",
            {"action": "accept", "goal": "draw", "output_kind": "state_change"},
            "c1",
        )
    )["ok"]
    identity = control.current["id"]
    await repository.checkpoint(lease, identity, {"transcript_ref": "preserved"})
    await control.execute("report_progress", {"text": "accepted"}, "p1")
    await control.execute("report_progress", {"text": "accepted"}, "p1")
    assert sent == ["accepted"]
    persisted = await repository.get(identity)
    assert persisted["state"] == "running"
    assert json.loads(persisted["checkpoint_json"])["transcript_ref"] == "preserved"
    assert not json.loads(await control.execute("task_control", {"action": "complete"}, "c2"))["ok"]
    control.observe_result(
        "terminal_exec",
        json.dumps(
            {
                "ok": True,
                "data": {
                    "run_id": "verified-run",
                    "pending": True,
                },
            }
        ),
        True,
    )
    control.observe_result(
        "get_code_run",
        json.dumps(
            {
                "ok": True,
                "data": {
                    "run_id": "verified-run",
                    "pending": False,
                    "exit_code": 0,
                },
            }
        ),
        True,
        side_effecting=False,
    )
    assert control.known_effects[-1]["side_effecting"] is True
    assert json.loads(await control.execute("task_control", {"action": "complete"}, "c3"))["ok"]
    assert (await repository.get(identity))["state"] == "running"
    await control.settle(delivered=True, pending_inputs=True)
    assert (await repository.get(identity))["state"] == "running"
    await control.execute("task_control", {"action": "complete"}, "c4")
    await control.settle(delivered=False, pending_inputs=False)
    assert (await repository.get(identity))["state"] == "suspended"


@pytest.mark.asyncio
@pytest.mark.parametrize("steer", [False, True])
@pytest.mark.parametrize("wire_protocol", [None, "responses", "chat_completions"])
async def test_agent_loop_speaks_then_executes_and_proposes_finish(
    database, tmp_path, steer, wire_protocol
):
    from tests.conftest import build_harness, make_settings

    from qq_ai_bot.automation.models import TurnOrigin
    from qq_ai_bot.domain.messages import (
        ChatMessage,
        ChatResponse,
        ChatTool,
        ToolCall,
        ToolFunction,
    )
    from qq_ai_bot.llm.fake import FakeLLMProvider
    from qq_ai_bot.runtime.work_control import work_control_tools
    from qq_ai_bot.services.agent_runner import AgentRuntime

    def call(name, args, identity):
        return ChatResponse(
            "", 0, tool_calls=(ToolCall(identity, ToolFunction(name, json.dumps(args))),)
        )

    responses = iter(
        [
            call(
                "task_control",
                {"action": "accept", "goal": "render", "output_kind": "state_change"},
                "accept",
            ),
            call("report_progress", {"text": "接下了，开始准备"}, "progress"),
            *([call("render_fixture", {}, "old-plan")] if steer else []),
            call("render_fixture", {}, "render"),
            call("task_control", {"action": "complete"}, "complete"),
            ChatResponse("图片已经生成", 0),
        ]
    )
    provider = FakeLLMProvider(lambda _: next(responses))
    harness = build_harness(database, make_settings(database.url), provider)
    chat = harness.processor._chat
    env = await social_env(database, tmp_path)
    repo = WorkRepository(database)
    lease = await repo.acquire(env.context.conversation_id, 1)
    assert lease
    observed = []

    async def validate():
        assert await repo.valid(lease)

    async def deliver(text, key):
        observed.append("say")
        return {"transport_accepted": True, "message_id": key}

    control = WorkControl(repo, lease, "source-loop", {}, validate, deliver)
    wire_client, captured = None, []
    if wire_protocol:
        from tests.support.runtime_wire import install_wire

        wire_client, captured = install_wire(chat, provider, wire_protocol)
    original_complete = provider.complete

    async def complete(request):
        if steer and len(provider.requests) == 2:
            identity = await repo.enqueue(
                lease.conversation_id,
                1,
                "steering",
                kind="message",
                work_id=control.current["id"],
                ready=False,
            )
            await repo.prepare_input(identity, {"text": "背景改成透明"})
        return await original_complete(request)

    provider.complete = complete

    class Backend:
        def definitions(self, runtime, **kwargs):
            return tuple(
                sorted(
                    (
                        *work_control_tools(),
                        ChatTool("render_fixture", "test render", {"type": "object"}),
                    ),
                    key=lambda tool: tool.name,
                )
            )

        def begin_batch(self, *args):
            pass

        def parallel_safe(self, *args):
            return False

        def is_side_effecting(self, *args):
            return True

        async def execute(self, name, arguments, runtime):
            assert name == "render_fixture"
            observed.append("render")
            return json.dumps({"ok": True, "data": {"artifact_id": "png", "exit_code": 0}})

        def finalize(self, text, runtime):
            return text

        def exhausted(self, runtime):
            return "exhausted"

        def post_commit_recovery_text(self):
            return None

    runtime = AgentRuntime(
        origin=TurnOrigin.USER_MESSAGE,
        actor_user_id="10001",
        actor_is_superuser=False,
        delegated_authority=None,
        conversation_key="test-work",
        current_group_id=None,
        bot_user_id="80001",
        gateway=None,
        runtime_config=await chat._runtime_config.snapshot(),
        current_time=chat._time.current_default(),
        allowed_capabilities=frozenset(),
        max_tool_calls=8,
        max_model_requests=8,
        work_control=control,
    )
    result = await chat._agent_runner.run(
        (ChatMessage(role="user", content="画图"),), runtime, Backend()
    )
    assert observed == ["say", "render"]
    assert result.text == "图片已经生成"
    assert control.ending == "completed"
    assert (await repo.get(control.current["id"]))["state"] == "running"
    for before, after in zip(provider.requests, provider.requests[1:], strict=False):
        assert after.tools == before.tools
        assert after.messages[: len(before.messages)] == before.messages
    if steer:
        if wire_protocol == "responses":
            rendered = json.dumps(captured[-1]["input"], ensure_ascii=False)
            assert rendered.count("背景改成透明") == 1
            assert "new_input_before_execution" in rendered
        else:
            final = provider.requests[-1].messages
            assert sum("背景改成透明" in (message.content or "") for message in final) == 1
            assert any("new_input_before_execution" in str(message) for message in final)
        async with database.sessions() as session:
            assert (await session.execute(select(inputs.c.state))).scalar_one() == "consumed"

    if wire_client:
        await wire_client.aclose()
        sequence = "input" if wire_protocol == "responses" else "messages"
        for before, after in pairwise(captured):
            assert after["tools"] == before["tools"]
            assert after[sequence][: len(before[sequence])] == before[sequence]


@pytest.mark.asyncio
async def test_reset_and_privacy_are_atomic_work_boundaries(database, tmp_path):
    from qq_ai_bot.conversation.hydrate import bump_canonical_generation
    from qq_ai_bot.persistence.models import ChatEventModel
    from qq_ai_bot.persistence.people_repository import PeopleRepository
    from qq_ai_bot.runtime.work_schema_v1 import work

    env = await social_env(database, tmp_path)
    repository = WorkRepository(database)
    key = env.context.conversation_id
    lease = await repository.acquire(key, 1)
    assert lease
    item = await repository.accept(
        lease, source_key="private-goal", source={"actor_user_id": "10001"}, goal="personal text"
    )
    await repository.enqueue(key, 1, "follow-up", kind="message", work_id=item["id"])
    await repository.prepare_effect(lease, item["id"], "already-sent", "progress")
    await repository.record_effect("already-sent", "accepted", {"message_id": "123"})
    async with database.sessions() as session, session.begin():
        event_id = (await session.execute(select(ChatEventModel.id))).scalar_one()
        assert await bump_canonical_generation(session, key, event_id=event_id) == 2
    assert not await repository.valid(lease)
    assert (await repository.get(item["id"]))["state"] == "cancelled"
    replacement = await repository.acquire(key, 2)
    assert replacement
    # Replaying the same reset does not cancel the newer activation.
    async with database.sessions() as session, session.begin():
        assert await bump_canonical_generation(session, key, event_id=event_id) == 2
    assert await repository.valid(replacement)
    assert await PeopleRepository(database).delete_person("10001")
    async with database.sessions() as session:
        for table in (work, scope, inputs, effects):
            assert not (await session.execute(select(table))).first()
    assert not await repository.valid(replacement)


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["chat_completions", "responses"])
@pytest.mark.parametrize("receipt_arrived", [False, True])
async def test_work_recovery_pairs_calls_without_reexecution(
    database, tmp_path, protocol, receipt_arrived
):
    from qq_ai_bot.domain.messages import ChatMessage, ProviderContinuation, ToolCall, ToolFunction
    from qq_ai_bot.runtime.work_control import WorkControl
    from qq_ai_bot.runtime.work_session import WorkSession
    from qq_ai_bot.services.turn_transcript import TurnTranscript

    env = await social_env(database, tmp_path)
    repository = WorkRepository(database)
    lease = await repository.acquire(env.context.conversation_id, 1)
    assert lease

    async def validate():
        pass

    control = WorkControl(repository, lease, "event", {"trigger_event_id": 1}, validate)
    control.current = await repository.accept(
        lease, source_key="event", source=control.source, goal="draw"
    )
    session = WorkSession(control, "same-contract")
    transcript = await session.restore(TurnTranscript((ChatMessage(role="user", content="draw"),)))
    call = ToolCall("render-1", ToolFunction("terminal_exec", '{"command":"render"}'))
    if protocol == "responses":
        transcript.accept(
            ProviderContinuation(
                "openai",
                "responses",
                (
                    {
                        "type": "function_call",
                        "call_id": call.id,
                        "name": call.function.name,
                        "arguments": call.function.arguments,
                    },
                ),
            )
        )
    else:
        transcript.append(
            ChatMessage(
                role="assistant", tool_calls=(call,), reasoning_content="provider protocol state"
            )
        )
    await session.save("response", (call,))
    key = session.call_key(call.id)
    assert await repository.prepare_effect(lease, control.current["id"], key, "tool")
    if receipt_arrived:
        await repository.record_effect(
            key, "accepted", {"result": '{"ok":true,"run_id":"original-run","pending":true}'}
        )
    await repository.release(lease)
    new_lease = await repository.acquire(env.context.conversation_id, 1)
    assert new_lease
    recovered = WorkControl(repository, new_lease, "event", control.source, validate)
    recovered.current = await repository.get(control.current["id"])
    resumed = WorkSession(recovered, "same-contract")
    rebuilt = await resumed.restore(
        TurnTranscript((ChatMessage(role="user", content="must not replace prefix"),))
    )
    assert rebuilt.chain_id == transcript.chain_id
    request = rebuilt.request()
    assert request.messages[0].content == "draw"
    output = request.items[-1].output if protocol == "responses" else request.messages[-1].content
    parsed = json.loads(output)
    assert (
        (parsed.get("run_id") == "original-run") if receipt_arrived else parsed["replay_forbidden"]
    )
    calls = []

    async def execute_again():
        calls.append("replayed")
        return "wrong"

    await resumed.execute(call, execute_again)
    assert calls == []
    await resumed.save("paired")
    # A second restart must not append another result for the same call.
    second = WorkSession(recovered, "same-contract")
    again = await second.restore(TurnTranscript(()))
    assert again.request() == rebuilt.request()


@pytest.mark.asyncio
@pytest.mark.parametrize("steer", [False, True])
async def test_real_chat_entry_progress_delivery_and_work_completion(
    database, tmp_path, steer, monkeypatch
):
    from dataclasses import replace
    from types import SimpleNamespace

    from tests.conftest import MemorySender, build_harness, make_settings
    from tests.unit.test_commands_and_chat import inbound

    from qq_ai_bot.conversation.hydrate import ensure_canonical_conversation
    from qq_ai_bot.domain.messages import ChatResponse, ToolCall, ToolFunction
    from qq_ai_bot.identity.canonical_repository import ensure_person, ensure_presence
    from qq_ai_bot.llm.fake import FakeLLMProvider
    from qq_ai_bot.runtime.work_schema_v1 import work
    from qq_ai_bot.services.main_agent_contract import MainAgentContract
    from qq_ai_bot.workspace.short_state import ShortState
    from qq_ai_bot.workspace.store import WorkspaceStore

    steps = iter(
        [
            (
                "task_control",
                {"action": "accept", "goal": "记录指定短期信息", "output_kind": "state_change"},
            ),
            ("report_progress", {"text": "接下了，这就记录。"}),
            *(
                [("update_short_state", {"slot": 1, "text": "old-plan", "expected_revision": 0})]
                if steer
                else []
            ),
            ("update_short_state", {"slot": 1, "text": "runtime-check", "expected_revision": 0}),
            ("task_control", {"action": "complete"}),
            (None, None),
        ]
    )

    def respond(request):
        name, arguments = next(steps)
        if name is None:
            return "已经记录好了。"
        return ChatResponse(
            "",
            0,
            tool_calls=(
                ToolCall(str(len(provider.requests)), ToolFunction(name, json.dumps(arguments))),
            ),
        )

    provider = FakeLLMProvider(respond)
    harness = build_harness(
        database, make_settings(database.url, runtime_work_enabled=True), provider
    )
    chat = harness.processor._chat
    state = ShortState(WorkspaceStore(tmp_path / "short-state"))
    chat._agent_runner.main_contract = MainAgentContract(
        chat, SimpleNamespace(_registry=None), state
    )
    chat._tools.short_state = state
    async with database.sessions() as session, session.begin():
        person = await ensure_person(session, "1001")
        presence = await ensure_presence(session, "9999")
        conversation = await ensure_canonical_conversation(
            session, kind="private", primary_scope_key="private:9999:1001", person_id=person
        )
    message = replace(
        inbound("记录指定短期信息", message_id="real-work"),
        conversation_id=conversation.conversation_id,
        legacy_conversation_key="private:9999:1001",
        person_id=person,
        presence_id=presence,
    )
    sender = MemorySender()
    model_waiting, input_ready = asyncio.Event(), asyncio.Event()
    original_complete = provider.complete

    async def complete(request):
        if steer and len(provider.requests) == 2:
            model_waiting.set()
            await input_ready.wait()
        return await original_complete(request)

    provider.complete = complete
    if not steer:
        from sqlalchemy.exc import OperationalError

        async def locked_release(self, lease):
            raise OperationalError(
                "UPDATE runtime_work_scopes", {}, Exception("database is locked")
            )

        monkeypatch.setattr(WorkRepository, "release", locked_release)
    running = asyncio.create_task(harness.processor.handle(message, sender))
    if steer:
        await asyncio.wait_for(model_waiting.wait(), 5)
        followup = replace(
            message,
            message_id="real-work-steer",
            text="补充要求：仍然写 runtime-check，别写 old-plan",
        )
        admitted = await asyncio.wait_for(harness.processor.handle(followup, MemorySender()), 5)
        assert admitted.reason == "work_input_queued", admitted
        input_ready.set()
    result = await asyncio.wait_for(running, 10)
    assert result.reason == "chat", result
    assert [m.text for m in sender.messages] == ["接下了，这就记录。", "已经记录好了。"]
    async with database.sessions() as session:
        row = (await session.execute(select(work))).mappings().one()
        assert row["state"] == "completed"
        assert row["sent_messages"] == 2
    assert state.snapshot()[0]["text"] == "runtime-check"
    assert all(request.tools == provider.requests[0].tools for request in provider.requests)


@pytest.mark.asyncio
async def test_child_completion_has_one_parent_consumer_and_scheduler(database, tmp_path):
    from types import SimpleNamespace
    from uuid import uuid4

    from tests.conftest import build_harness, make_settings

    from qq_ai_bot.domain.messages import ChatResponse, ToolCall, ToolFunction
    from qq_ai_bot.identity.db_models import CanonicalSpaceModel
    from qq_ai_bot.llm.fake import FakeLLMProvider
    from qq_ai_bot.persistence.models import ChatEventModel
    from qq_ai_bot.runtime.work_scheduler import WorkScheduler
    from qq_ai_bot.sandbox.continuation_worker import SandboxContinuationWorker
    from qq_ai_bot.sandbox.task_repository import SandboxTaskRepository
    from qq_ai_bot.services.main_agent_contract import MainAgentContract
    from qq_ai_bot.workspace.short_state import ShortState
    from qq_ai_bot.workspace.store import WorkspaceStore

    env = await social_env(database, tmp_path)
    repository = WorkRepository(database)
    async with database.sessions() as session, session.begin():
        event_id = (await session.execute(select(ChatEventModel.id))).scalar_one()
        space = await session.get(CanonicalSpaceModel, env.space)
        space.enabled = True
    source = {
        "origin": "user_message",
        "conversation_id": env.context.conversation_id,
        "actor_user_id": "10001",
        "trigger_id": "inbound",
        "trigger_event_id": event_id,
        "bot_user_id": "80001",
        "presence_id": env.presence,
        "generation": 1,
    }
    lease = await repository.acquire(env.context.conversation_id, 1)
    assert lease
    parent = await repository.accept(
        lease, source_key="message:parent", source=source, goal="记录完成结果"
    )
    await repository.transition(lease, parent["id"], parent["revision"], "waiting_external")
    await repository.release(lease)
    tasks = SandboxTaskRepository(database)
    await tasks.prepare("child-request", {"command": "draw"}, {**source, "work_id": parent["id"]})
    run_id = str(uuid4())
    await tasks.receive(
        {
            "request_id": "child-request",
            "run_id": run_id,
            "result": {"run_id": run_id, "status": "succeeded", "pending": False, "exit_code": 0},
        }
    )
    steps = iter(
        [
            ("update_short_state", {"slot": 1, "text": "child-done", "expected_revision": 0}),
            ("task_control", {"action": "complete"}),
            (None, None),
        ]
    )

    def respond(request):
        name, args = next(steps)
        return (
            "后台任务已完成。"
            if name is None
            else ChatResponse(
                "",
                0,
                tool_calls=(
                    ToolCall(str(len(provider.requests)), ToolFunction(name, json.dumps(args))),
                ),
            )
        )

    provider = FakeLLMProvider(respond)
    harness = build_harness(
        database,
        make_settings(database.url, runtime_work_enabled=True, enabled_groups_csv="20001"),
        provider,
    )
    chat = harness.processor._chat
    state = ShortState(WorkspaceStore(tmp_path / "resume-state"))
    chat._tools.short_state = state
    chat._agent_runner.main_contract = MainAgentContract(
        chat, SimpleNamespace(_registry=None), state
    )
    app = SimpleNamespace(
        database=database,
        settings=harness.settings,
        sandbox_tasks=tasks,
        ledger=harness.ledger,
        presence_router=env.router,
        runtime_config=chat._runtime_config,
        conversation_scopes=chat._conversation_scopes,
        turn_coordinator=chat._turn_coordinator,
        chat=chat,
    )
    old = SandboxContinuationWorker(app)
    await old._drain_request("child-request")
    await old._drain_request("child-request")
    assert (await old.repository.get("child-request")).state == "observed"
    assert not provider.requests
    scheduler = WorkScheduler(app)
    await scheduler.drain_once()
    assert (await repository.get(parent["id"]))["state"] == "completed"
    assert state.snapshot()[0]["text"] == "child-done"
    assert len([call for call in env.bot.calls if call[0] == "send_group_msg"]) == 1
    await scheduler.drain_once()
    await old._drain_request("child-request")
    assert len(provider.requests) == 3
    async with database.sessions() as session:
        assert len((await session.execute(select(inputs))).all()) == 1


@pytest.mark.asyncio
async def test_artifact_completion_requires_verified_delivery(database, tmp_path):
    env = await social_env(database, tmp_path)
    repo = WorkRepository(database)
    lease = await repo.acquire(env.context.conversation_id, 1)

    async def validate():
        assert await repo.valid(lease)

    control = WorkControl(repo, lease, "artifact-work", {}, validate)
    assert json.loads(
        await control.execute(
            "task_control",
            {
                "action": "accept",
                "goal": "draw",
                "output_kind": "artifact",
            },
            "accept",
        )
    )["ok"]
    finish = {"action": "complete", "artifact_ids": ["png"]}
    assert not json.loads(await control.execute("task_control", finish, "missing"))["ok"]
    control.observe_result("workspace_publish", '{"ok":true,"data":{"artifact_id":"png"}}', True)
    assert not json.loads(await control.execute("task_control", finish, "unsent"))["ok"]
    control.observe_result(
        "send_group_message",
        '{"ok":true,"data":{"status":"succeeded"}}',
        True,
        arguments='{"artifact_id":"png"}',
    )
    assert json.loads(await control.execute("task_control", finish, "sent"))["ok"]
    await control.settle(delivered=True, pending_inputs=False)
    assert (await repo.get(control.current["id"]))["state"] == "completed"


@pytest.mark.asyncio
async def test_receipt_repair_records_accepted_delivery_without_resending(database, tmp_path):
    from tests.conftest import build_harness, make_settings

    from qq_ai_bot.persistence.models import ChatEventModel
    from qq_ai_bot.runtime.work_delivery import repair_receipt_ledger

    env = await social_env(database, tmp_path)
    harness = build_harness(database, make_settings(database.url))
    repo = WorkRepository(database)
    lease = await repo.acquire(env.context.conversation_id, 1)
    async with database.sessions() as session:
        event_id = (await session.execute(select(ChatEventModel.id))).scalar_one()
    source = {"trigger_event_id": event_id, "origin": "user_message"}

    async def validate():
        assert await repo.valid(lease)

    control = WorkControl(repo, lease, "accepted-delivery", source, validate)
    control.current = await repo.accept(
        lease, source_key=control.source_key, source=source, goal="draw"
    )
    await repo.prepare_effect(lease, control.current["id"], "delivery", "final")
    await repo.record_effect(
        "delivery",
        "accepted",
        {
            "transport_accepted": True,
            "message_id": "already-sent",
            "text": "done",
        },
    )
    before = len(env.bot.calls)
    await repair_receipt_ledger(control, harness.ledger)
    await repair_receipt_ledger(control, harness.ledger)
    assert len(env.bot.calls) == before
    async with database.sessions() as session:
        rows = (
            (
                await session.execute(
                    select(ChatEventModel).where(
                        ChatEventModel.platform_message_id == "already-sent"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1 and rows[0].caused_by_event_id == event_id


@pytest.mark.asyncio
async def test_new_epoch_retains_execution_evidence_and_budget(database, tmp_path):
    from qq_ai_bot.domain.messages import ChatMessage
    from qq_ai_bot.runtime.work_session import WorkSession
    from qq_ai_bot.services.turn_transcript import TurnTranscript

    env = await social_env(database, tmp_path)
    repo = WorkRepository(database)
    lease = await repo.acquire(env.context.conversation_id, 1)

    async def validate():
        assert await repo.valid(lease)

    control = WorkControl(repo, lease, "epoch", {"trigger_event_id": 123}, validate)
    control.current = await repo.accept(lease, source_key="epoch", source={}, goal="draw")
    await repo.checkpoint(lease, control.current["id"], None, models=3, tools=2, active_seconds=61)
    first = WorkSession(control, "old-contract")
    await first.restore(TurnTranscript((ChatMessage("user", "draw"),)))
    control.known_effects = [{"run_id": "original", "pending": True, "ok": True}]
    await first.save("paired")
    control.current = await repo.get(control.current["id"])
    second = WorkSession(control, "new-contract")
    restored = await second.restore(TurnTranscript((ChatMessage("user", "draw"),)))
    assert control.current["active_seconds"] == 61
    assert control.current["model_requests"] == 3 and control.current["tool_calls"] == 2
    assert control.known_effects[0]["run_id"] == "original"
    assert "original" in restored.request().messages[-1].content
    assert "恢复" in restored.request().messages[-1].content
    # A steering message arriving at final delivery must resume, not repeatedly
    # restore the suppressed-delivery marker and leave its input pending forever.
    control.ending = "completed"
    await second.save("delivery")
    identity = await repo.enqueue(
        lease.conversation_id,
        lease.generation,
        "late-steering",
        kind="message",
        work_id=control.current["id"],
    )
    await repo.prepare_input(identity, {"text": "change background"})
    third = WorkSession(control, "new-contract")
    await third.restore(TurnTranscript(()))
    assert third.recovered_delivery is None
    assert await control.pending()


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", ["plugin_session", "scheduled_automation"])
async def test_sync_main_entry_returns_result_without_acquiring_send_authority(
    database, tmp_path, origin
):
    from types import SimpleNamespace

    from tests.conftest import build_harness, make_settings

    from qq_ai_bot.domain.messages import ChatMessage, ChatResponse, ToolCall, ToolFunction
    from qq_ai_bot.llm.fake import FakeLLMProvider
    from qq_ai_bot.runtime.origin import TurnOrigin
    from qq_ai_bot.services.agent_runner import AgentRuntime
    from qq_ai_bot.services.main_agent_contract import MainAgentContract, ShortStateOnlyBackend
    from qq_ai_bot.workspace.short_state import ShortState
    from qq_ai_bot.workspace.store import WorkspaceStore

    steps = iter(
        [
            ("task_control", {"action": "accept", "goal": "write answer", "output_kind": "answer"}),
            ("report_progress", {"text": "must not be sent"}),
            ("task_control", {"action": "complete"}),
        ]
    )

    def respond(request):
        step = next(steps, None)
        if step is None:
            return "computed answer"
        name, args = step
        return ChatResponse(
            "",
            0,
            tool_calls=(
                ToolCall(str(len(provider.requests)), ToolFunction(name, json.dumps(args))),
            ),
        )

    provider = FakeLLMProvider(respond)
    harness = build_harness(
        database, make_settings(database.url, runtime_work_enabled=True), provider
    )
    chat = harness.processor._chat
    env = await social_env(database, tmp_path)
    state = ShortState(WorkspaceStore(tmp_path / "sync-state"))
    chat._agent_runner.main_contract = MainAgentContract(
        chat, SimpleNamespace(_registry=None), state
    )
    runtime = AgentRuntime(
        origin=TurnOrigin(origin),
        actor_user_id="10001",
        actor_is_superuser=False,
        delegated_authority=None,
        conversation_key="sync-work",
        current_group_id=None,
        bot_user_id="80001",
        gateway=None,
        runtime_config=await chat._runtime_config.snapshot(),
        current_time=chat._time.current_default(),
        allowed_capabilities=frozenset(),
        max_tool_calls=1,
        max_model_requests=8,
        canonical_conversation_id=env.context.conversation_id,
        execution_id="same-invocation",
    )
    backend = ShortStateOnlyBackend(state)
    result = await chat._main_turns.run((ChatMessage("user", "write answer"),), runtime, backend)
    assert result.text == "computed answer" and result.work_state == "completed"
    assert any("progress_delivery_not_authorized" in str(r.messages) for r in provider.requests)
    count = len(provider.requests)
    repeated = await chat._main_turns.run((ChatMessage("user", "write answer"),), runtime, backend)
    assert repeated.text == "computed answer" and repeated.model_requests == 0
    assert len(provider.requests) == count


@pytest.mark.asyncio
async def test_independent_work_queues_without_overwriting_waiting_parent(database, tmp_path):
    from qq_ai_bot.domain.conversations import ConversationScope
    from qq_ai_bot.persistence.models import ChatEventModel
    from qq_ai_bot.runtime.work_activation import activate_work

    env = await social_env(database, tmp_path)
    repo = WorkRepository(database)
    lease = await repo.acquire(env.context.conversation_id, 1)
    async with database.sessions() as session:
        original_id = (await session.execute(select(ChatEventModel.id))).scalar_one()
    source = {
        "origin": "user_message",
        "actor_user_id": "10001",
        "trigger_event_id": original_id,
        "trigger_id": "inbound",
        "generation": 1,
        "bot_user_id": "80001",
        "presence_id": env.presence,
        "conversation_id": env.context.conversation_id,
    }
    original = await repo.accept(lease, source_key="old-work", source=source, goal="draw original")
    original = await repo.transition(
        lease, original["id"], original["revision"], "waiting_external"
    )
    await env.service.writer.append(
        scope=ConversationScope.group("80001", "20001"),
        platform_message_id="independent-work",
        sender_user_id="10001",
        direction="inbound",
        content="meanwhile write an answer",
    )
    # Read the actual canonical event regardless of the writer's append wrapper.
    async with database.sessions() as session:
        new_id = await session.scalar(
            select(ChatEventModel.id).where(
                ChatEventModel.platform_message_id == "independent-work"
            )
        )
    newer_source = {**source, "trigger_event_id": new_id, "trigger_id": "independent-work"}

    async def validate():
        pass

    control = WorkControl(repo, lease, "new-message", newer_source, validate)
    control.current = original
    result = json.loads(
        await control.execute(
            "task_control",
            {
                "action": "accept",
                "goal": "write separate answer",
                "output_kind": "answer",
            },
            "queue",
        )
    )
    assert result["ok"] and result["state"] == "queued"
    assert (await repo.get(original["id"]))["goal"] == "draw original"
    assert (await repo.get(original["id"]))["state"] == "waiting_external"
    queued = await repo.get(result["queued_work_id"])
    assert queued["model_requests"] == 0 and queued["goal"] == "write separate answer"
    await repo.release(lease)
    async with activate_work(
        repo,
        lease.conversation_id,
        1,
        queued["source_key"],
        json.loads(queued["source_json"]),
        validate,
        work_id=queued["id"],
    ) as resumed:
        assert resumed.current["id"] == queued["id"]
        assert (await repo.get(original["id"]))["state"] == "waiting_external"


@pytest.mark.asyncio
async def test_completed_receipts_reconcile_pending_evidence_without_model_poll(database, tmp_path):
    from uuid import uuid4

    from qq_ai_bot.runtime.work_session import WorkSession
    from qq_ai_bot.sandbox.task_repository import SandboxTaskRepository
    from qq_ai_bot.services.turn_transcript import TurnTranscript

    env = await social_env(database, tmp_path)
    repo = WorkRepository(database)
    lease = await repo.acquire(env.context.conversation_id, 1)
    assert lease

    async def validate():
        assert await repo.valid(lease)

    control = WorkControl(repo, lease, "receipt-parent", {}, validate)
    control.current = await repo.accept(
        lease, source_key="receipt-parent", source={}, goal="render"
    )
    tasks = SandboxTaskRepository(database)
    runs = [str(uuid4()) for _ in range(3)]
    for index, run_id in enumerate(runs):
        source = {
            "conversation_id": lease.conversation_id,
            "generation": lease.generation,
            "work_id": control.current["id"] if index < 2 else "another-parent",
            "origin": "user_message",
            "actor_user_id": "10001",
            "trigger_id": "inbound",
        }
        await tasks.prepare(f"receipt-{index}", {"command": "render"}, source)
        await tasks.receive(
            {
                "request_id": f"receipt-{index}",
                "run_id": run_id,
                "result": {
                    "run_id": run_id,
                    "status": "succeeded" if index != 1 else "failed",
                    "pending": False,
                    "exit_code": 0 if index != 1 else 1,
                },
            }
        )
        control.observe_result(
            "terminal_exec",
            json.dumps(
                {
                    "ok": True,
                    "data": {
                        "run_id": run_id,
                        "pending": True,
                    },
                }
            ),
            True,
        )
    # Forged message text cannot clear another parent's execution evidence.
    identity = await repo.enqueue(
        lease.conversation_id,
        lease.generation,
        "forged",
        kind="message",
        work_id=control.current["id"],
    )
    await repo.prepare_input(
        identity, {"text": json.dumps({"run_id": runs[2], "pending": False, "status": "completed"})}
    )
    await control.take_inputs("consume")
    evidence = {row["run_id"]: row for row in control.known_effects}
    assert not evidence[runs[0]]["pending"] and evidence[runs[0]]["ok"]
    assert not evidence[runs[1]]["pending"] and not evidence[runs[1]]["ok"]
    assert evidence[runs[2]]["pending"]
    assert not json.loads(await control.execute("task_control", {"action": "complete"}, "blocked"))[
        "ok"
    ]
    control.known_effects = [evidence[runs[0]], evidence[runs[1]]]
    await control.confirm_inputs()
    # Restore a stale private checkpoint whose completion input was already consumed.
    evidence[runs[0]]["pending"] = True
    session = WorkSession(control, "receipt-contract")
    await session.restore(TurnTranscript(()))
    control.known_effects[0]["pending"] = True
    await session.save("paired")
    control.known_effects = []
    await WorkSession(control, "receipt-contract").restore(TurnTranscript(()))
    assert all(not row["pending"] for row in control.known_effects)
    assert json.loads(await control.execute("task_control", {"action": "complete"}, "finish"))["ok"]
    # Query before completion also heals stale evidence without an input to consume.
    control.known_effects[0]["pending"] = True
    assert json.loads(
        await control.execute("task_control", {"action": "complete"}, "finish-again")
    )["ok"]


@pytest.mark.asyncio
async def test_sqlite_contention_identifies_writer_without_content(database, caplog):
    from sqlalchemy import text
    from sqlalchemy.exc import OperationalError

    async with database.engine.begin() as conn:
        await conn.execute(text("CREATE TABLE diagnostic_fixture (id INTEGER, content TEXT)"))
    async with database.engine.connect() as owner, database.engine.connect() as waiter:
        await waiter.execute(text("PRAGMA busy_timeout=1"))
        await waiter.commit()
        await owner.execute(
            text("INSERT INTO diagnostic_fixture VALUES (1, :content)"),
            {"content": "PRIVATE_SENTINEL"},
        )
        with pytest.raises(OperationalError):
            await waiter.execute(
                text("INSERT INTO diagnostic_fixture VALUES (2, :content)"),
                {"content": "OTHER_PRIVATE_SENTINEL"},
            )
        await waiter.rollback()
        assert "INSERT INTO diagnostic_fixture" in caplog.text
        assert "held_seconds" in caplog.text
        assert "PRIVATE_SENTINEL" not in caplog.text
        await owner.rollback()
        caplog.clear()
        await waiter.execute(text("INSERT INTO diagnostic_fixture VALUES (3, 'ok')"))
        await waiter.commit()
        assert "sqlite_write_contended" not in caplog.text
