"""Independent requests own their sends; a delivered caption can close the work."""

import json
from dataclasses import replace
from datetime import UTC, datetime
from itertools import pairwise
from uuid import uuid4

import pytest
from sqlalchemy import select
from tests.conftest import build_harness, make_settings

# P10: explicit Invocation fixture contract; existing assertions are retained.
from tests.support.agent_backend import StubAgentBackend
from tests.support.runtime_wire import install_wire
from tests.support.social_identity_cases import social_env
from tests.support.work_runner_helpers import case, run, tool
from tests.support.workspace_snapshots import snapshot_bytes

from qq_ai_bot.automation.models import TurnOrigin
from qq_ai_bot.domain.conversations import ConversationScope
from qq_ai_bot.domain.messages import ChatMessage, ChatResponse, ChatTool, ToolCall, ToolFunction
from qq_ai_bot.identity.db_models import CanonicalSpaceModel, IdentityBindingModel
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.persistence.models import ChatEventModel
from qq_ai_bot.runtime.work_activation import activate_work
from qq_ai_bot.runtime.work_control import WorkControl, work_control_tools
from qq_ai_bot.runtime.work_queries import WorkQueries
from qq_ai_bot.runtime.work_repository import WorkRepository
from qq_ai_bot.services.agent_runner import AgentRuntime
from qq_ai_bot.social.tools import social_tool_definitions


def call(name, args, identity):
    return ChatResponse(
        "", 0, tool_calls=(ToolCall(identity, ToolFunction(name, json.dumps(args))),)
    )


class DeliveryBackend(StubAgentBackend):
    def __init__(self, env):
        self.env = env
        self.owners = []

    def definitions(self, runtime, **kwargs):
        return tuple(
            sorted((*work_control_tools(), *social_tool_definitions()), key=lambda t: t.name)
        )

    def parallel_safe(self, *args):
        return False

    def is_side_effecting(self, *args):
        return True

    async def execute_call(self, invocation):
        name = invocation.call.function.name
        arguments = invocation.call.function.arguments
        runtime = invocation.context.runtime
        control = runtime.work_control
        self.owners.append(control.current["id"])
        result = await self.env.service.execute(
            name,
            json.loads(arguments),
            replace(self.env.context, turn_id=control.current["id"], call_id="file"),
        )
        return json.dumps({"ok": True, "data": result})

    def finalize(self, text, runtime):
        return text

    def exhausted(self, runtime):
        raise AssertionError("unexpected exhaustion")


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["responses", "chat_completions"])
async def test_independent_request_sends_once_and_caption_finishes_without_extra_request(
    database, tmp_path, protocol
):
    env = await social_env(database, tmp_path)
    repo = WorkRepository(database)
    async with database.sessions() as session, session.begin():
        original_id = await session.scalar(select(ChatEventModel.id))
        space = await session.get(CanonicalSpaceModel, env.space)
        space.enabled = True
    source = {
        "origin": "user_message",
        "actor_user_id": "10001",
        "actor_person_id": env.person,
        "trigger_event_id": original_id,
        "bot_user_id": "80001",
        "presence_id": env.presence,
        "generation": 1,
        "conversation_id": env.context.conversation_id,
    }
    lease = await repo.acquire(env.context.conversation_id, 1)
    old = await repo.accept(lease, source_key="original", source=source, goal="draw a banana")
    await repo.checkpoint(lease, old["id"], None, messages=16)
    await repo.release(lease)
    await env.service.writer.append(
        scope=ConversationScope.group("80001", "20001"),
        platform_message_id="new-request",
        sender_user_id="10001",
        direction="inbound",
        content="make an exam document",
    )
    async with database.sessions() as session:
        event_id = await session.scalar(
            select(ChatEventModel.id).where(ChatEventModel.platform_message_id == "new-request")
        )
    source = {**source, "trigger_event_id": event_id}
    source_key = f"event:{env.context.conversation_id}:{event_id}"
    artifact = snapshot_bytes(env.store, "exam.docx", b"immutable document fixture")
    steps = iter(
        [
            call(
                "task_control",
                {"action": "accept", "goal": "make exam"},
                "accept",
            ),
            call(
                "send_message",
                {
                    "artifact_id": artifact["artifact_id"],
                    "attachment_kind": "file",
                    "text": "答案已整理好",
                },
                "send",
            ),
            call(
                "task_control",
                {"action": "complete"},
                "complete",
            ),
        ]
    )
    provider = FakeLLMProvider(lambda _: next(steps))
    chat = build_harness(database, make_settings(database.url), provider).processor._chat
    client, captured = install_wire(chat, provider, protocol)
    backend = DeliveryBackend(env)
    runtime = AgentRuntime(
        origin=TurnOrigin.USER_MESSAGE,
        actor_user_id="10001",
        actor_is_superuser=False,
        delegated_authority=None,
        conversation_key="delivery-test",
        current_group_id="20001",
        bot_user_id="80001",
        gateway=None,
        runtime_config=await chat._runtime_config.snapshot(),
        current_time=chat._time.current_default(),
        allowed_capabilities=frozenset(),
        max_tool_calls=8,
        max_model_requests=8,
    )

    async def validate():
        pass

    try:
        async with activate_work(
            repo, env.context.conversation_id, 1, source_key, source, validate, work_id=old["id"]
        ) as control:
            assert control.current["id"] == old["id"]
            result = await chat.runtime.runner.run(
                (ChatMessage(role="user", content="make exam"),),
                replace(runtime, work_control=control),
                backend,
            )
            assert result.suppress_delivery and not result.text
            assert not backend.owners and len(captured) == 1
            new_id = control.handoff_work_id
        assert (await repo.get(old["id"]))["state"] == "suspended"
        new = await repo.get(new_id)
        assert new["state"] == "queued" and new["model_requests"] == 0
        # A later activation selects the new event's owner, not the old task.
        async with activate_work(
            repo, env.context.conversation_id, 1, source_key, source, validate
        ) as control:
            assert control.current["id"] == new_id
            result = await chat.runtime.runner.run(
                (ChatMessage(role="user", content="make exam"),),
                replace(runtime, work_control=control),
                backend,
            )
            assert result.suppress_delivery and result.work_state == "completed"
            assert control.accepted_ending() == "completed" and len(captured) == 3
            # Crash after the delivered checkpoint, before the lifecycle transition.
            recovered = WorkControl(repo, control.lease, source_key, source, validate)
            recovered.current = await repo.get(new_id)
            again = await chat.runtime.runner.run(
                (ChatMessage(role="user", content="must not replace history"),),
                replace(runtime, work_control=recovered),
                backend,
            )
            assert again.suppress_delivery and len(captured) == 3
        assert backend.owners == [new_id]
        assert (await repo.get(new_id))["state"] == "completed"
        assert (await repo.get(old["id"]))["sent_messages"] == 16
        assert [name for name, _ in env.bot.calls].count("upload_group_file") == 1
        assert [name for name, _ in env.bot.calls].count("send_group_msg") == 1
        sequence = "input" if protocol == "responses" else "messages"
        for before, after in pairwise(captured):
            assert before["tools"] == after["tools"]
        assert captured[2][sequence][: len(captured[1][sequence])] == captured[1][sequence]
        # An explicit later resend is a new intent, even for the same artifact bytes.
        await env.service.writer.append(
            scope=ConversationScope.group("80001", "20001"),
            platform_message_id="resend-request",
            sender_user_id="10001",
            direction="inbound",
            content="send the same document again",
        )
        async with database.sessions() as session:
            resend_id = await session.scalar(
                select(ChatEventModel.id).where(
                    ChatEventModel.platform_message_id == "resend-request"
                )
            )
        steps = iter(
            [
                call(
                    "task_control",
                    {"action": "accept", "goal": "resend document"},
                    "resend-accept",
                ),
                call(
                    "send_message",
                    {
                        "artifact_id": artifact["artifact_id"],
                        "attachment_kind": "file",
                        "text": "再发一次",
                    },
                    "resend",
                ),
                call(
                    "task_control",
                    {"action": "complete"},
                    "resend-done",
                ),
            ]
        )
        async with activate_work(
            repo,
            env.context.conversation_id,
            1,
            f"event:{env.context.conversation_id}:{resend_id}",
            {**source, "trigger_event_id": resend_id},
            validate,
        ) as control:
            assert control.current is None
            resend = await chat.runtime.runner.run(
                (ChatMessage(role="user", content="send it again"),),
                replace(runtime, work_control=control),
                backend,
            )
            assert resend.suppress_delivery and control.accepted_ending() == "completed"
        assert len(backend.owners) == 2 and backend.owners[0] != backend.owners[1]
        assert [name for name, _ in env.bot.calls].count("upload_group_file") == 2
        assert [name for name, _ in env.bot.calls].count("send_group_msg") == 2
        assert len(captured) == 6
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_accept_handoff_is_atomic_and_recovery_does_not_block_old_work_forever(
    database, tmp_path
):
    from tests.support.work_session import WorkSession

    from qq_ai_bot.services.turn_transcript import TurnTranscript

    env = await social_env(database, tmp_path)
    repo = WorkRepository(database)
    lease = await repo.acquire(env.context.conversation_id, 1)
    old = await repo.accept(lease, source_key="old", source={}, goal="original")
    new = await repo.accept(
        lease, source_key="new", source={}, goal="independent", handoff_from=old["id"]
    )
    assert new["state"] == "queued"

    async def validate():
        pass

    # Simulate losing the process after acceptance, before the paired checkpoint.
    recovered = WorkControl(repo, lease, "old", {}, validate)
    recovered.current = await repo.get(old["id"])
    session = WorkSession(recovered, "fixed-contract")
    await session.restore(TurnTranscript((ChatMessage(role="user", content="original"),)))
    assert recovered.handoff_work_id == new["id"]
    await session.save("paired")
    await recovered.settle(pending_inputs=True)
    assert (await repo.get(old["id"]))["state"] == "queued"  # Later input is not stranded.
    resumed = WorkControl(repo, lease, "old", {}, validate)
    resumed.current = await repo.get(old["id"])
    again = WorkSession(resumed, "fixed-contract")
    transcript = await again.restore(TurnTranscript(()))
    assert resumed.handoff_work_id is None  # Its own background completion may now resume.
    material = json.loads(transcript.request().messages[-1].content)
    assert material["goal"] == old["goal"]
    assert material["work_id"] == old["id"]
    assert (await repo.get(new["id"]))["model_requests"] == 0
    await repo.release(lease)


@pytest.mark.asyncio
async def test_same_person_new_account_keeps_work_scope_and_queues_new_internal_event(
    database, tmp_path
):
    env = await social_env(database, tmp_path)
    now = datetime.now(UTC)
    async with database.immediate_session() as session:
        original = await session.scalar(select(ChatEventModel))
        session.add(
            IdentityBindingModel(
                id=str(uuid4()),
                person_id=env.person,
                platform="qq",
                external_account_id="11099",
                display_name="same person",
                status="active",
                revision=1,
                first_seen_at=now,
                last_seen_at=now,
                created_at=now,
                updated_at=now,
            )
        )
    appended = await env.service.writer.append(
        scope=ConversationScope.group("80001", "20001"),
        platform_message_id="other-binding-new-request",
        sender_user_id="11099",
        direction="inbound",
        content="new independent request",
    )
    latest = appended.event
    assert latest.author_person_id == original.author_person_id == env.person
    source = {
        "origin": "user_message",
        "principal_kind": "person",
        "actor_person_id": env.person,
        "actor_user_id": "10001",
        "trigger_event_id": original.id,
        "bot_user_id": "80001",
        "presence_id": env.presence,
        "generation": 1,
        "conversation_id": env.context.conversation_id,
    }
    repo = WorkRepository(database)
    lease = await repo.acquire(env.context.conversation_id, 1)
    old = await repo.accept(lease, source_key="binding-original", source=source, goal="original")
    switched = {**source, "actor_user_id": "11099", "trigger_event_id": latest.id}
    assert await WorkQueries(repo).get(lease, switched, old["id"], local=True) is not None
    await repo.release(lease)

    async def validate():
        pass

    async with activate_work(
        repo,
        env.context.conversation_id,
        1,
        "binding-original",
        switched,
        validate,
        work_id=old["id"],
    ) as control:
        assert control.current["id"] == old["id"]
        response = json.loads(
            await control.execute(
                "task_control",
                {"action": "accept", "goal": "independent"},
                "new-binding",
            )
        )
        assert response["ok"], response
        queued = await repo.get(response["queued_work_id"])
        saved = json.loads(queued["source_json"])
        assert saved["actor_person_id"] == env.person
        assert saved["actor_user_id"] == "11099"
        assert saved["trigger_event_id"] == latest.id


@pytest.mark.asyncio
async def test_retired_work_report_is_ignored_by_original_social_reader(database, tmp_path):
    env = await social_env(database, tmp_path)
    original = {
        "text": "原调用已发送",
        "work_report": {"kind": "retired", "reply_to_event_ids": [999999]},
    }
    result = await env.service.execute("send_message", original, env.context)
    assert result["status"] == "succeeded"
    assert await env.service.execute("send_message", original, env.context) == result
    assert len([name for name, _ in env.bot.calls if name == "send_group_msg"]) == 1
    schema = next(t for t in social_tool_definitions() if t.name == "send_message").parameters
    assert "work_report" not in schema["properties"]


async def test_file_sent_caption_unknown_can_explain_and_complete_without_reupload(
    database, tmp_path
):
    test_case = await case(database, tmp_path, [])
    env, control = test_case.env, test_case.control
    artifact = snapshot_bytes(env.store, "original.txt", b"confirmed original file")
    original_gateway = env.bot.call_api

    async def caption_disconnected(action, **parameters):
        result = await original_gateway(action, **parameters)
        if action == "send_group_msg" and "文件附言" in str(parameters):
            raise RuntimeError("caption confirmation lost after gateway call")
        return result

    env.bot.call_api = caption_disconnected

    async def execute_original(invocation):
        from qq_ai_bot.capabilities.results import normalize_legacy_result
        from qq_ai_bot.runtime.effect_outcomes import current_result_capture

        test_case.observed.append(invocation.call.function.name)
        receipt = await env.service.execute(
            invocation.call.function.name,
            json.loads(invocation.call.function.arguments),
            replace(
                env.context, turn_id=control.current["id"], call_id=invocation.identity.operation_id
            ),
        )
        result = json.dumps({"ok": receipt["status"] == "succeeded", "data": receipt})
        capture = current_result_capture.get()
        capture.outcome = normalize_legacy_result(
            result, provider_id="core", tool_name=invocation.call.function.name
        )
        return result

    test_case.backend.execute_call = execute_original
    responses = iter(
        [
            ChatResponse(
                "",
                0,
                tool_calls=(
                    tool(
                        "send_message",
                        {
                            "artifact_id": artifact["artifact_id"],
                            "attachment_kind": "file",
                            "text": "文件附言",
                        },
                        "file",
                    ),
                ),
            ),
            ChatResponse(
                "",
                0,
                tool_calls=(
                    tool("send_message", {"text": "文件已送达，原附言尚未确认。"}, "explanation"),
                    tool("task_control", {"action": "complete"}, "complete"),
                ),
            ),
        ]
    )
    test_case.provider._responder = lambda _request: next(responses)
    result = await run(test_case)
    assert result.work_state == "completed" and len(test_case.provider.requests) == 2
    recovered = WorkControl(
        control.repository, control.lease, control.source_key, control.source, control.validate
    )
    recovered.current = await control.repository.get(control.current["id"])
    await test_case.runner.run(
        (ChatMessage("user", "resume original"),),
        replace(test_case.runtime, work_control=recovered),
        test_case.backend,
    )
    assert len(test_case.provider.requests) == 2
    assert [name for name, _ in env.bot.calls].count("upload_group_file") == 1
    assert [name for name, _ in env.bot.calls].count("send_group_msg") == 2
    await recovered.settle(pending_inputs=False)
    assert recovered.current["state"] == "completed"
    file_fact = next(
        fact for fact in await recovered.effect_evidence() if fact.get("delivered_artifacts")
    )
    assert file_fact["delivered_artifacts"] == [artifact["artifact_id"]]
    assert file_fact["uncertain"] is True and file_fact["caption_delivered"] is False


@pytest.mark.parametrize("send_status", ["succeeded", "failed", "uncertain"])
async def test_direct_send_then_complete_pairs_later_calls_without_executing_them(
    database, tmp_path, send_status
):
    calls = (
        tool("send_message", {"text": "原调用说明"}, "send"),
        tool("task_control", {"action": "complete", "result": "部分成果保留"}, "complete"),
        tool("write_fixture", {}, "after-complete"),
    )
    test_case = await case(
        database, tmp_path, [ChatResponse("", 0, tool_calls=calls)], send_status=send_status
    )
    result = await run(test_case)
    assert result.work_state == "completed" and result.text == "部分成果保留"
    assert test_case.observed == ["send_message"]
    assert len(test_case.provider.requests) == 1
    await test_case.control.settle(pending_inputs=False)
    persisted = await test_case.repository.get(test_case.control.current["id"])
    assert persisted["state"] == "completed"
    transcript = test_case.control.session.transcript.request()
    pairs = {
        message.tool_call_id: json.loads(message.content)
        for message in transcript.messages
        if message.role == "tool"
    }
    assert set(pairs) == {call.id for call in calls}
    assert pairs["after-complete"]["executed"] is False
    facts = await test_case.control.effect_evidence()
    assert facts[0]["uncertain"] is (send_status == "uncertain")


@pytest.mark.parametrize("mode", ["active", "neutral", "disabled"])
async def test_direct_memory_and_send_follow_original_order_without_work_admission(
    database, tmp_path, mode
):
    calls = (
        tool("memory_change", {}, "remember"),
        tool("send_message", {"text": "执行后说明"}, "send"),
    )
    test_case = await case(
        database, tmp_path, [ChatResponse("", 0, tool_calls=calls), ChatResponse("", 0)]
    )
    test_case.backend.definitions = lambda *_args, **_kwargs: (
        *work_control_tools(),
        *social_tool_definitions(),
        ChatTool("memory_change", "remember", {"type": "object"}),
    )
    if mode == "neutral":
        test_case.control.current = None
    elif mode == "disabled":
        test_case.runtime = replace(test_case.runtime, work_control=None)
    result = await run(test_case)
    assert test_case.observed == ["memory_change", "send_message"]
    assert result.text == "" and len(test_case.provider.requests) == 2


async def test_mixed_code_resource_yield_stops_later_calls_in_original_batch(
    database, tmp_path, monkeypatch
):
    from qq_ai_bot.services.agent_runner import CODE_COMPOSITION_YIELDED

    calls = (
        tool("execute_code", {"code": "# resource pause"}, "original-code"),
        tool("send_message", {"text": "not reached"}, "later-send"),
    )
    test_case = await case(database, tmp_path, [ChatResponse("", 0, tool_calls=calls)])
    original_definitions = test_case.backend.definitions
    test_case.backend.definitions = lambda *args, **kwargs: (
        *original_definitions(*args, **kwargs),
        ChatTool("execute_code", "compose", {"type": "object"}),
    )

    async def yield_original(*_args, **_kwargs):
        test_case.control.yield_segment = True
        return CODE_COMPOSITION_YIELDED

    monkeypatch.setattr(test_case.runner, "_run_code_call", yield_original)
    result = await run(test_case)
    assert result.work_state == "queued" and len(test_case.provider.requests) == 1
    assert test_case.observed == []
    session = test_case.control.session
    saved = await session.journal.load(
        test_case.control.lease, test_case.control.current["id"], session.contract
    )
    assert saved.record["phase"] == "response"
    assert [call["id"] for call in json.loads(saved.record["payload_json"])["pending"]] == [
        "original-code",
        "later-send",
    ]


@pytest.mark.parametrize("kind", ["message", "completion"])
async def test_late_auxiliary_fact_does_not_veto_final_but_business_input_is_observed(
    database, tmp_path, monkeypatch, kind
):
    test_case = await case(
        database,
        tmp_path,
        [ChatResponse("first internal final", 0), ChatResponse("updated final", 0)],
    )
    complete = test_case.provider.complete

    async def with_late_input(request):
        response = await complete(request)
        if len(test_case.provider.requests) == 1:
            identity = await test_case.repository.enqueue(
                test_case.control.lease.conversation_id,
                test_case.control.lease.generation,
                "late-" + kind,
                kind=kind,
                work_id=test_case.control.current["id"],
                ready=False,
            )
            await test_case.repository.prepare_input(identity, {"text": "actual late " + kind})
        return response

    monkeypatch.setattr(test_case.provider, "complete", with_late_input)
    result = await run(test_case)
    assert len(test_case.provider.requests) == (2 if kind == "message" else 1)
    assert result.text == ("updated final" if kind == "message" else "first internal final")
    if kind == "message":
        assert "actual late message" in str(test_case.provider.requests[-1].messages)
    await test_case.control.settle(pending_inputs=bool(await test_case.control.pending()))
    assert test_case.control.current["state"] == "completed"
