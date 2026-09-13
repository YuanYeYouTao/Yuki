"""Independent requests own their sends; a delivered caption can close the work."""

import json
from dataclasses import replace
from itertools import pairwise

import pytest
from sqlalchemy import select
from tests.conftest import build_harness, make_settings
from tests.support.runtime_wire import install_wire
from tests.support.social_identity_cases import social_env

from qq_ai_bot.automation.models import TurnOrigin
from qq_ai_bot.domain.conversations import ConversationScope
from qq_ai_bot.domain.messages import ChatMessage, ChatResponse, ToolCall, ToolFunction
from qq_ai_bot.identity.db_models import CanonicalSpaceModel
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.persistence.models import ChatEventModel
from qq_ai_bot.runtime.work_activation import activate_work
from qq_ai_bot.runtime.work_control import WorkControl, work_control_tools
from qq_ai_bot.runtime.work_repository import WorkRepository
from qq_ai_bot.services.agent_runner import AgentRuntime
from qq_ai_bot.social.tools import social_tool_definitions


def call(name, args, identity):
    return ChatResponse(
        "", 0, tool_calls=(ToolCall(identity, ToolFunction(name, json.dumps(args))),)
    )


class DeliveryBackend:
    def __init__(self, env):
        self.env = env
        self.owners = []

    def definitions(self, runtime, **kwargs):
        return tuple(
            sorted((*work_control_tools(), *social_tool_definitions()), key=lambda t: t.name)
        )

    def begin_batch(self, *args):
        pass

    def parallel_safe(self, *args):
        return False

    def is_side_effecting(self, *args):
        return True

    async def execute(self, name, arguments, runtime):
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

    def post_commit_recovery_text(self):
        return None


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
    artifact = env.store.write("exam.docx", b"immutable document fixture")
    steps = iter(
        [
            call(
                "task_control",
                {"action": "accept", "goal": "make exam", "output_kind": "artifact"},
                "accept",
            ),
            call(
                "send_group_message",
                {
                    "artifact_id": artifact["artifact_id"],
                    "attachment_kind": "file",
                    "text": "答案已整理好",
                },
                "send",
            ),
            call(
                "task_control",
                {"action": "complete", "artifact_ids": [artifact["artifact_id"]]},
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
            repo, env.context.conversation_id, 1, source_key, source, validate
        ) as control:
            assert control.current["id"] == old["id"]
            result = await chat._agent_runner.run(
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
            result = await chat._agent_runner.run(
                (ChatMessage(role="user", content="make exam"),),
                replace(runtime, work_control=control),
                backend,
            )
            assert result.suppress_delivery and result.work_state == "completed"
            assert control.final_delivery and len(captured) == 3
            # Crash after the delivered checkpoint, before the lifecycle transition.
            recovered = WorkControl(repo, control.lease, source_key, source, validate)
            recovered.current = await repo.get(new_id)
            again = await chat._agent_runner.run(
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
                    {"action": "accept", "goal": "resend document", "output_kind": "artifact"},
                    "resend-accept",
                ),
                call(
                    "send_group_message",
                    {
                        "artifact_id": artifact["artifact_id"],
                        "attachment_kind": "file",
                        "text": "再发一次",
                    },
                    "resend",
                ),
                call(
                    "task_control",
                    {"action": "complete", "artifact_ids": [artifact["artifact_id"]]},
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
            resend = await chat._agent_runner.run(
                (ChatMessage(role="user", content="send it again"),),
                replace(runtime, work_control=control),
                backend,
            )
            assert resend.suppress_delivery and control.final_delivery
        assert len(backend.owners) == 2 and backend.owners[0] != backend.owners[1]
        assert [name for name, _ in env.bot.calls].count("upload_group_file") == 2
        assert [name for name, _ in env.bot.calls].count("send_group_msg") == 2
        assert len(captured) == 6
    finally:
        await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "caption_status,same_target",
    [
        ("succeeded", True),
        ("succeeded", False),
        ("failed", True),
        ("not_sent", True),
    ],
)
async def test_file_receipt_survives_caption_failure_and_other_targets_still_need_reply(
    database, tmp_path, caption_status, same_target
):
    env = await social_env(database, tmp_path)
    repo = WorkRepository(database)
    lease = await repo.acquire(env.context.conversation_id, 1)

    async def validate():
        pass

    control = WorkControl(repo, lease, "delivery", {}, validate)
    await control.execute(
        "task_control", {"action": "accept", "goal": "file", "output_kind": "artifact"}, "accept"
    )
    control.observe_result(
        "send_group_message",
        json.dumps(
            {
                "ok": True,
                "data": {
                    "status": "succeeded" if caption_status == "succeeded" else "failed",
                    "file": {"status": "succeeded"},
                    "caption": {"status": caption_status},
                    "target": {
                        "kind": "space",
                        "id": env.space if same_target else "another-space",
                    },
                    **(
                        {"error": "file_sent_caption_unconfirmed"}
                        if caption_status != "succeeded"
                        else {}
                    ),
                },
            }
        ),
        True,
        arguments='{"artifact_id":"doc","attachment_kind":"file","text":"done"}',
    )
    result = json.loads(
        await control.execute(
            "task_control", {"action": "complete", "artifact_ids": ["doc"]}, "finish"
        )
    )
    assert result["ok"]  # Never require uploading the confirmed file again.
    assert control.completion_delivered is (caption_status == "succeeded" and same_target)
    await repo.release(lease)


@pytest.mark.asyncio
async def test_accept_handoff_is_atomic_and_recovery_does_not_block_old_work_forever(
    database, tmp_path
):
    from qq_ai_bot.runtime.work_session import WorkSession
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
    await recovered.settle(delivered=False, pending_inputs=True)
    assert (await repo.get(old["id"]))["state"] == "queued"  # Later input is not stranded.
    resumed = WorkControl(repo, lease, "old", {}, validate)
    resumed.current = await repo.get(old["id"])
    again = WorkSession(resumed, "fixed-contract")
    transcript = await again.restore(TurnTranscript(()))
    assert resumed.handoff_work_id is None  # Its own background completion may now resume.
    assert "不得代做或重发" in transcript.request().messages[-1].content
    assert (await repo.get(new["id"]))["model_requests"] == 0
    await repo.release(lease)
