"""Attachment preparation releases its activation and resumes the original mailbox."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete, insert, select
from tests.conftest import build_harness, make_settings
from tests.unit.test_work_protocol_continuity import _control

from qq_ai_bot.domain.messages import ChatImage, ChatMessage
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.runtime.origin import TurnOrigin
from qq_ai_bot.runtime.subagent_schema import media, media_refs
from qq_ai_bot.runtime.work_activation import activate_work
from qq_ai_bot.runtime.work_control import WorkControl, WorkInputsPreparing, work_control_tools
from qq_ai_bot.runtime.work_repository import WorkCapacityError, WorkConflict, WorkRepository
from qq_ai_bot.runtime.work_schema_v1 import inputs
from qq_ai_bot.runtime.work_session import WorkSession
from qq_ai_bot.services.agent_runner import AgentRuntime
from qq_ai_bot.services.turn_transcript import TurnTranscript


async def enqueue(control, key="attachment", *, ready=False):
    return await control.repository.enqueue(
        control.lease.conversation_id,
        control.lease.generation,
        key,
        kind="message",
        work_id=control.current["id"],
        ready=ready,
    )


@pytest.mark.asyncio
async def test_unready_input_yields_without_polling(database, tmp_path):
    control = await _control(database, tmp_path)
    await enqueue(control)
    # Connection acquisition/pre-ping is not application polling. Read the real
    # mailbox first, then prove the unready branch completes without another
    # read or suspension once that result is available, independent of CI load.
    pending = await control.pending()
    assert pending and not pending[0]["ready"]
    control.repository.pending = AsyncMock(
        side_effect=[pending, AssertionError("unready input must not be polled")]
    )
    operation = control.take_inputs("preparing")
    try:
        with pytest.raises(WorkInputsPreparing):
            operation.send(None)
    finally:
        operation.close()
    control.repository.pending.assert_awaited_once_with(
        control.lease, work_id=control.current["id"]
    )
    assert control.staged_attempt is None


@pytest.mark.asyncio
async def test_runner_unready_input_releases_activation_without_provider_request(
    database, tmp_path
):
    original = await _control(database, tmp_path)
    input_id = await enqueue(original)
    await original.repository.release(original.lease)
    provider = FakeLLMProvider("must not dispatch")
    chat = build_harness(database, make_settings(database.url), provider).processor._chat

    async def validate():
        pass

    async with activate_work(
        original.repository,
        original.lease.conversation_id,
        original.lease.generation,
        original.source_key,
        original.source,
        validate,
    ) as control:
        runtime = AgentRuntime(
            origin=TurnOrigin.USER_MESSAGE,
            actor_user_id="10001",
            actor_is_superuser=False,
            delegated_authority=None,
            conversation_key="input-preparation",
            current_group_id="20001",
            bot_user_id="80001",
            gateway=None,
            runtime_config=await chat._runtime_config.snapshot(),
            current_time=chat._time.current_default(),
            allowed_capabilities=frozenset(),
            max_tool_calls=8,
            max_model_requests=8,
            work_control=control,
        )
        result = await asyncio.wait_for(
            chat.runtime.runner.run(
                (ChatMessage("user", "original task"),),
                runtime,
                SimpleNamespace(definitions=lambda *_args, **_kwargs: work_control_tools()),
            ),
            timeout=0.5,
        )
        assert result.suppress_delivery and result.work_state == "waiting_external"
        assert result.model_requests == 0 and result.work_id == original.current["id"]
    assert not await original.repository.valid(control.lease)
    assert (await original.repository.get(original.current["id"]))["state"] == "waiting_external"
    assert provider.requests == []
    assert await original.repository.prepare_input(input_id, {"text": "ready"})
    assert (await original.repository.get(original.current["id"]))["state"] == "queued"


@pytest.mark.asyncio
@pytest.mark.parametrize("ready_before_settle", [False, True])
async def test_prepared_input_resumes_same_work_and_media_after_activation_release(
    database, tmp_path, ready_before_settle
):
    control = await _control(database, tmp_path)
    identity = control.current["id"]
    control.session = WorkSession(control, "fixed")
    await control.session.restore(TurnTranscript((ChatMessage("user", "original task"),)))
    await control.session.save("paired")
    await control.repository.checkpoint(
        control.lease, identity, {"retained": "original-checkpoint"}, models=3, tools=2
    )
    input_id = await enqueue(control)
    with pytest.raises(WorkInputsPreparing):
        await control.take_inputs("preparing")
    control.ending = "waiting_external"
    image = ChatImage("data:image/png;base64,YXVkaXQ=")

    async def ready():
        assert await control.repository.prepare_input(
            input_id, {"text": "use this attachment"}, images=(image,)
        )

    if ready_before_settle:
        await ready()
    # Model the stale pre-settlement pending observation: transition must
    # serialize its final mailbox check with preparation's writer transaction.
    await control.settle(delivered=False, pending_inputs=False)
    await control.repository.release(control.lease)
    if not ready_before_settle:
        assert (await control.repository.get(identity))["state"] == "waiting_external"
        await ready()
    assert (await control.repository.get(identity))["state"] == "queued"
    fresh_repo = WorkRepository(database)
    lease = await fresh_repo.acquire(control.lease.conversation_id, control.lease.generation)
    assert lease
    current = await fresh_repo.get(identity)
    assert current["model_requests"] == 3 and current["tool_calls"] == 2
    assert json.loads(current["checkpoint_json"])["retained"] == "original-checkpoint"
    resumed = WorkControl(fresh_repo, lease, "anchor-test", control.source, control.validate)
    resumed.current = current
    resumed.session = WorkSession(resumed, "fixed")
    current = (ChatMessage("user", "Current chat; continue the original task"),)
    restored = await resumed.session.restore(TurnTranscript(current))
    # A journal save before staging must retain the pending input's refs.
    await resumed.session.save("paired")
    additions = await resumed.take_inputs("resumed")
    assert len(additions) == 1 and additions[0].images == (image,)
    assert "use this attachment" in additions[0].content
    restored.append(additions[0])
    await resumed.session.save("paired")
    await resumed.confirm_inputs()
    async with database.sessions() as reader:
        row = (await reader.execute(select(inputs))).mappings().one()
        assert row["id"] == input_id and row["work_id"] == identity
        assert row["state"] == "consumed"
    again = await WorkSession(resumed, "fixed").restore(TurnTranscript(current))
    assert again.request().messages[-1].images == (image,)
    assert await fresh_repo.prepare_input(input_id, {"text": "retry must not replace"})


@pytest.mark.asyncio
async def test_later_ready_input_does_not_spin_past_preparing_head(database, tmp_path):
    control = await _control(database, tmp_path)
    first = await enqueue(control, "first")
    await enqueue(control, "later-ready", ready=True)
    control.ending = "waiting_external"
    await control.settle(delivered=False, pending_inputs=True)
    assert control.current["state"] == "waiting_external"
    assert await control.repository.prepare_input(first, {"text": "first ready"})
    assert (await control.repository.get(control.current["id"]))["state"] == "queued"


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_work", [False, True])
async def test_discard_or_reset_rejects_late_preparation_without_media_write(
    database, tmp_path, cancel_work
):
    control = await _control(database, tmp_path)
    input_id = await enqueue(control)
    if cancel_work:
        await control.repository.cancel(control.lease.conversation_id)
    else:
        await control.repository.discard_input(input_id)
    assert not await control.repository.prepare_input(
        input_id, {"text": "late"}, images=(ChatImage("data:image/png;base64,bGF0ZQ=="),)
    )
    async with database.sessions() as reader:
        assert await reader.scalar(select(media.c.sha256)) is None


@pytest.mark.asyncio
async def test_prepared_media_requires_original_work_reference(database, tmp_path):
    control = await _control(database, tmp_path)
    input_id = await enqueue(control)
    assert await control.repository.prepare_input(
        input_id, {"text": "private"}, images=(ChatImage("data:image/png;base64,b3duZWQ="),)
    )
    async with database.immediate_session() as writer:
        await writer.execute(delete(media_refs))
    with pytest.raises(WorkConflict, match="work_input_media_missing"):
        await control.take_inputs("must-not-dispatch")
    assert control.staged_attempt is None


@pytest.mark.asyncio
async def test_full_mailbox_keeps_original_input_preparation_and_deduplication(database, tmp_path):
    control = await _control(database, tmp_path)
    input_id = await enqueue(control)
    async with database.immediate_session() as writer:
        await writer.execute(
            insert(inputs),
            [
                {
                    "conversation_id": control.lease.conversation_id,
                    "generation": control.lease.generation,
                    "source_key": f"full-{i}",
                    "kind": "message",
                    "work_id": control.current["id"],
                    "ready": False,
                    "payload_json": "{}",
                    "state": "pending",
                    "created": 1,
                }
                for i in range(127)
            ],
        )
    with pytest.raises(WorkCapacityError, match="work_input_capacity"):
        await enqueue(control, "cannot-admit-new")
    assert await enqueue(control) == input_id
    assert await control.repository.prepare_input(input_id, {"text": "original stays admissible"})
    assert await control.repository.prepare_input(input_id, {"text": "duplicate"})
    additions = await control.take_inputs("full-ready")
    assert len(additions) == 1 and "original stays admissible" in additions[0].content
