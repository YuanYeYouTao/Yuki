"""#439: ordinary input ownership joins the original Main, not just its history."""

import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.exc import SQLAlchemyError
from tests.conftest import MemorySender
from tests.support.canonical_identity import ensure_person
from tests.unit.test_auto_reply_merge import (
    VOICE_REQUIREMENT,
    _admissions,
    _asr,
    _open_group_scene,
    _wait_for_worker,
)

from yuki.asr.service import AudioInput
from yuki.conversation.initiative_sources import source_revision
from yuki.conversation.ordinary_admission import OrdinaryParticipationBinding
from yuki.conversation.projection_models import PromptProjectionChunkModel
from yuki.domain.messages import (
    AttachmentKind,
    ChatResponse,
    MessageAttachment,
    OutboundMessage,
    SenderIdentity,
    ToolCall,
    ToolFunction,
)
from yuki.identity.db_models import CanonicalPersonModel, CanonicalSpaceModel
from yuki.llm.fake import FakeLLMProvider
from yuki.memory.runtime.turn_session import TurnMemorySession
from yuki.persistence.models import ChatEventModel
from yuki.runtime.origin import TurnOrigin
from yuki.runtime.work_activation import current_work_control
from yuki.runtime.work_schema_v1 import work
from yuki.services.main_agent_contract import MainAgentContract
from yuki.social.db_models import SocialOperationModel
from yuki.workspace.short_state import ShortState
from yuki.workspace.store import WorkspaceStore

A = "A：先看原来的问题"
B = "B：补充一个背景"
C = "C：实际需求改为救一下 Diana"
ANSWER = "已按补充处理 Diana"


def _tool(name, arguments, call_id):
    return ChatResponse(
        "", 0, tool_calls=(ToolCall(call_id, ToolFunction(name, json.dumps(arguments))),)
    )


def _plain(request):
    return "\n".join(message.content or "" for message in request.messages)


class _PausedProvider(FakeLLMProvider):
    def __init__(self, responses, *, pauses=(1, 2)):
        self.responses = iter(responses)
        self.entered = []
        self.controls = []
        self.arrived = {number: asyncio.Event() for number in pauses}
        self.release = {number: asyncio.Event() for number in pauses}
        super().__init__(self._respond)

    def _respond(self, _request):
        response = next(self.responses, "NO_REPLY")
        if isinstance(response, BaseException):
            raise response
        return response

    async def _complete(self, request):
        self.entered.append(request)
        control = current_work_control.get()
        self.controls.append((control is not None, control.current if control else None))
        number = len(self.entered)
        if number in self.arrived:
            self.arrived[number].set()
            await self.release[number].wait()
        return await super()._complete(request)

    async def wait(self, number):
        await asyncio.wait_for(self.arrived[number].wait(), 10)


async def _scene(
    database, tmp_path, provider, *, reason="group_open", runtime_work_enabled=True, **settings
):
    env, harness, worker, message = await _open_group_scene(
        database,
        tmp_path,
        provider,
        debounce=0,
        runtime_work_enabled=runtime_work_enabled,
        conversation_semantic_participation_enabled=(reason != "group_open"),
        **settings,
    )
    chat = harness.processor._chat
    state = ShortState(WorkspaceStore(tmp_path / "short-state"))
    chat.runtime.runner.main_contract = MainAgentContract(chat, state, code_enabled=False)
    chat.runtime.runner.code_mode_settings = harness.settings
    chat._tools.short_state = state
    chat._tools.social_service = env.service
    if reason != "group_open":
        async with database.immediate_session() as writer:
            await writer.execute(
                update(CanonicalSpaceModel)
                .where(CanonicalSpaceModel.id == env.space)
                .values(require_mention=True)
            )

        async def binding_for_event(event_id, *, record=None, runtime=None):
            event = record or await harness.ledger.get_event(event_id)
            return OrdinaryParticipationBinding(
                env.context.conversation_id,
                1,
                event_id,
                str(source_revision(event)),
                "ordinary-thread",
                "group",
            )

        async def continuation_for_event(event_id):
            return await binding_for_event(event_id), reason

        harness.processor.set_participation(
            SimpleNamespace(
                continuation_for_event=continuation_for_event,
                binding_for_event=binding_for_event,
                continuation_current=AsyncMock(return_value=True),
                on_ordinary_admitted=AsyncMock(),
            )
        )
    return env, harness, worker, message


async def _event(database, content):
    async with database.read_session() as reader:
        return await reader.scalar(select(ChatEventModel).where(ChatEventModel.content == content))


async def _work_count(database):
    async with database.read_session() as reader:
        return await reader.scalar(select(func.count()).select_from(work))


async def _start_and_observe(
    database, harness, worker, message, sender, provider, *, incoming=None, finish=True
):
    assert (await harness.processor.handle(message(1, A), sender)).reason == "ordinary_deferred"
    await provider.wait(1)
    key = next(iter(worker._states))
    assert (await harness.processor.handle(message(2, B), sender)).reason == "ordinary_deferred"
    assert (await harness.processor.handle(incoming or message(3, C), sender)).reason == (
        "ordinary_deferred"
    )
    a = await _event(database, A)
    if incoming is None:
        c = await _event(database, C)
    else:
        async with database.read_session() as reader:
            c = await reader.scalar(
                select(ChatEventModel).where(
                    ChatEventModel.platform_message_id == incoming.message_id,
                    ChatEventModel.direction == "inbound",
                )
            )
    assert [row.event_id for row in await _admissions(database)] == [a.id]
    assert worker._states[key].message.source_event_id == c.id
    provider.release[1].set()
    await provider.wait(2)
    first, continued = provider.entered[:2]
    assert C not in _plain(first)
    assert B in _plain(continued) and C in _plain(continued)
    fragments = [
        item.content
        for item in continued.messages
        if item.role == "user"
        and C in (item.content or "")
        and any(line.startswith((f"#{c.id}|", f"#{c.id}>")) for line in item.content.splitlines())
    ]
    assert fragments
    # Added inputs extend the original private sequence; the frozen prefix and
    # full tool declarations do not change with the pending worker slot.
    assert continued.messages[: len(first.messages)] == first.messages
    assert continued.tools == first.tools
    if finish:
        provider.release[2].set()
        await asyncio.wait_for(_wait_for_worker(worker, key), 10)
    return a, c


@pytest.mark.parametrize(
    ("reason", "final", "runtime_work_enabled"),
    [
        ("group_open", "NO_REPLY", True),
        ("continuation", "", True),
        ("group_open", "普通 final", True),
        ("group_open", "NO_REPLY", False),
    ],
)
async def test_observed_ordinary_input_joins_original_activation_without_work(
    database, tmp_path, reason, final, runtime_work_enabled
):
    provider = _PausedProvider(
        [_tool("query_chat_history", {"view": "recent"}, "read"), ChatResponse(final, 0)]
    )
    _env, harness, worker, message = await _scene(
        database, tmp_path, provider, reason=reason, runtime_work_enabled=runtime_work_enabled
    )
    sender = MemorySender()
    try:
        a, c = await _start_and_observe(database, harness, worker, message, sender, provider)
        admissions = {row.event_id: row for row in await _admissions(database)}
        assert admissions[c.id].activation_id == admissions[a.id].activation_id
        assert admissions[c.id].coordinator_version == admissions[a.id].coordinator_version
        assert admissions[c.id].route == "ordinary" and admissions[c.id].work_id is None
        assert len(provider.requests) == 2
        assert len({request.request_chain_id for request in provider.requests}) == 1
        assert provider.controls == [(runtime_work_enabled, None)] * 2
        assert await _work_count(database) == sender.calls == 0
    finally:
        await worker.close()


async def test_initial_history_exposure_preserves_unaccepted_ordinary_opportunity(
    database, tmp_path, monkeypatch
):
    provider = _PausedProvider(["NO_REPLY", "NO_REPLY"], pauses=(1,))
    _env, harness, worker, message = await _scene(database, tmp_path, provider)
    sender = MemorySender()
    assembler = harness.processor._chat._context_assembler
    original = assembler.assemble
    entered, release = asyncio.Event(), asyncio.Event()
    first = True

    async def assemble(**kwargs):
        nonlocal first
        if first:
            first = False
            entered.set()
            await release.wait()
        return await original(**kwargs)

    monkeypatch.setattr(assembler, "assemble", assemble)
    try:
        await harness.processor.handle(message(1, A), sender)
        await asyncio.wait_for(entered.wait(), 10)
        key = next(iter(worker._states))
        await harness.processor.handle(message(2, B), sender)
        await harness.processor.handle(message(3, C), sender)
        a, c = await _event(database, A), await _event(database, C)
        release.set()
        await provider.wait(1)
        assert B in _plain(provider.entered[0]) and C in _plain(provider.entered[0])
        assert A in provider.entered[0].messages[-1].content
        assert [row.event_id for row in await _admissions(database)] == [a.id]
        provider.release[1].set()
        await asyncio.wait_for(_wait_for_worker(worker, key), 10)
        admissions = {row.event_id: row for row in await _admissions(database)}
        assert admissions[c.id].activation_id != admissions[a.id].activation_id
        assert len(provider.requests) == 2
        assert len({request.request_chain_id for request in provider.requests}) == 2
        assert await _work_count(database) == sender.calls == 0
    finally:
        release.set()
        await worker.close()


async def _social_sender(env, sender, monkeypatch):
    original = env.bot.call_api

    async def gateway(action, **params):
        if action == "send_group_msg":
            env.bot.calls.append((action, params))
            parts = params["message"]
            text = (
                parts
                if isinstance(parts, str)
                else "".join(part["data"]["text"] for part in parts if part["type"] == "text")
            )
            receipt = await sender.send(OutboundMessage(text=text))
            return {"message_id": int(receipt.platform_message_id)}
        return await original(action, **params)

    monkeypatch.setattr(env.bot, "call_api", gateway)


async def test_accepted_input_does_not_reactivate_and_duplicate_real_social_send(
    database, tmp_path, monkeypatch
):
    # The old second activation would buy two more requests and deliver the same
    # payload. No hash/cache suppresses it: the original admission owns C.
    provider = _PausedProvider(
        [
            _tool("query_chat_history", {"view": "recent"}, "read"),
            _tool("send", {"text": ANSWER}, "answer"),
            "NO_REPLY",
            _tool("send", {"text": ANSWER}, "answer"),
            "NO_REPLY",
        ]
    )
    env, harness, worker, message = await _scene(database, tmp_path, provider)
    sender = MemorySender()
    await _social_sender(env, sender, monkeypatch)
    try:
        a, c = await _start_and_observe(database, harness, worker, message, sender, provider)
        admissions = {row.event_id: row for row in await _admissions(database)}
        assert admissions[c.id].activation_id == admissions[a.id].activation_id
        assert len(provider.requests) == 3
        assert len({request.request_chain_id for request in provider.requests}) == 1
        assert [message.text for message in sender.messages] == [ANSWER]
        async with database.read_session() as reader:
            receipts = list(await reader.scalars(select(SocialOperationModel)))
        assert len(receipts) == 1 and receipts[0].status == "succeeded"
        assert receipts[0].source_turn_id == f"{env.context.conversation_id}:event:{a.id}"
        assert receipts[0].event_id is not None
        assert receipts[0].event_id not in admissions
        assert await _work_count(database) == 0
    finally:
        await worker.close()


@pytest.mark.parametrize("a_is_superuser", [False, True])
async def test_other_members_public_input_keeps_original_execution_authority(
    database, tmp_path, monkeypatch, a_is_superuser
):
    provider = _PausedProvider(
        [
            _tool("query_chat_history", {"view": "recent"}, "before-c"),
            _tool("query_chat_history", {"view": "recent"}, "after-c"),
            "NO_REPLY",
        ]
    )
    env, harness, worker, message = await _scene(
        database,
        tmp_path,
        provider,
    )
    async with database.immediate_session() as writer:
        c_person = await ensure_person(writer, "10002")
        actor = await writer.get(CanonicalPersonModel, env.person)
        actor.is_superuser = a_is_superuser
        other = await writer.get(CanonicalPersonModel, c_person)
        other.is_superuser = not a_is_superuser
    incoming = replace(message(3, C), sender=SenderIdentity("10002"), person_id=c_person)
    tools = harness.processor._chat._tools
    chat = harness.processor._chat
    sessions = []

    def open_memory_session(*, autonomous):
        assert not autonomous
        session = TurnMemorySession.open(origin=TurnOrigin.USER_MESSAGE)
        sessions.append(session)
        return session

    monkeypatch.setattr(chat, "open_memory_session", open_memory_session)
    original = tools.execute
    runtimes = []

    async def execute(name, arguments_json, runtime):
        runtimes.append(runtime)
        return await original(name, arguments_json, runtime)

    monkeypatch.setattr(tools, "execute", execute)
    try:
        a, c = await _start_and_observe(
            database, harness, worker, message, MemorySender(), provider, incoming=incoming
        )
        admissions = {row.event_id: row for row in await _admissions(database)}
        assert admissions[c.id].activation_id == admissions[a.id].activation_id
        assert admissions[c.id].actor_person_id == c_person != admissions[a.id].actor_person_id
        assert admissions[c.id].source_revision == str(source_revision(c))
        assert admissions[c.id].presence_id == c.ingress_presence_id
        assert len(provider.requests) == 3
        assert len(runtimes) == 2
        for runtime in runtimes:
            assert runtime.actor_user_id == "10001"
            assert runtime.actor_context.person_id == env.person
            assert runtime.actor_is_superuser is a_is_superuser
            assert runtime.effective_trigger_event_id == a.id
            assert runtime.effective_execution_id == f"event:{a.id}"
        assert runtimes[1].memory_session is runtimes[0].memory_session
        assert len(sessions) == 1 and runtimes[0].memory_session is sessions[0]
        assert runtimes[1].memory_allowed_scopes == runtimes[0].memory_allowed_scopes
        assert await _work_count(database) == 0
    finally:
        await worker.close()


@pytest.mark.parametrize(
    ("reason", "source", "runtime_work_enabled"),
    [
        ("group_open", "current", True),
        ("continuation", "reply", True),
        ("group_open", "current", False),
        ("continuation", "reply", False),
    ],
)
async def test_steered_voice_reuses_prepared_transcript_and_partial_failure(
    database, tmp_path, reason, source, runtime_work_enabled
):
    provider = _PausedProvider(
        [_tool("query_chat_history", {"view": "recent"}, "read"), "NO_REPLY"]
    )
    _env, harness, worker, message = await _scene(
        database, tmp_path, provider, reason=reason, runtime_work_enabled=runtime_work_enabled
    )
    asr = _asr(harness.processor, source=source)
    asr.prepare.return_value = AudioInput(
        transcript=asr.prepare.return_value.transcript, error="no_speech"
    )
    attachment = MessageAttachment(AttachmentKind.AUDIO, "voice", source=source, file="fake.amr")
    incoming = replace(
        message(3, C),
        attachments=(attachment,) if source == "current" else (),
        reply_attachments=(attachment,) if source == "reply" else (),
        reply_text="引用的补充语音" if source == "reply" else None,
    )
    try:
        a, c = await _start_and_observe(
            database, harness, worker, message, MemorySender(), provider, incoming=incoming
        )
        admissions = {row.event_id: row for row in await _admissions(database)}
        assert admissions[c.id].activation_id == admissions[a.id].activation_id
        current = await harness.ledger.get_event(c.id)
        assert admissions[c.id].source_revision == str(source_revision(current))
        assert VOICE_REQUIREMENT in _plain(provider.requests[1])
        assert "部分语音未识别" in _plain(provider.requests[1])
        assert len(provider.requests) == 2 and asr.prepare.await_count == 1
        assert provider.controls == [(runtime_work_enabled, None)] * 2
        assert VOICE_REQUIREMENT in (await harness.ledger.get_event(c.id)).audio_transcript
        assert await _work_count(database) == 0
    finally:
        await worker.close()


async def test_steered_attachments_add_private_indices_without_automatic_reads(database, tmp_path):
    provider = _PausedProvider(
        [_tool("query_chat_history", {"view": "recent"}, "read"), "NO_REPLY"]
    )
    _env, harness, worker, message = await _scene(database, tmp_path, provider)
    incoming = replace(
        message(3, C),
        attachments=(MessageAttachment(AttachmentKind.IMAGE, "image", file="fake.png"),),
        segments=(
            {"type": "text", "data": {"text": C}},
            {"type": "image", "data": {"file": "fake.png", "name": "unread.png"}},
        ),
        reply_segments=({"type": "file", "data": {"file": "fake.txt", "name": "quoted.txt"}},),
        reply_text="看一下引用的文件",
        reply_attachments=(
            MessageAttachment(AttachmentKind.FILE, "file", source="reply", file="fake.txt"),
        ),
    )
    try:
        a, c = await _start_and_observe(
            database, harness, worker, message, MemorySender(), provider, incoming=incoming
        )
        admissions = {row.event_id: row for row in await _admissions(database)}
        assert admissions[c.id].activation_id == admissions[a.id].activation_id
        assert not any(message.images for message in provider.requests[1].messages)
        material = _plain(provider.requests[1])
        assert C in material
        assert "unread.png" in material and "quoted.txt" in material
        assert '"origin":"reply_observed"' in material
        assert len(provider.requests) == 2
        async with database.read_session() as reader:
            public = list(await reader.scalars(select(PromptProjectionChunkModel.payload_json)))
        assert public and all("本轮附件索引" not in value for value in public)
        assert await _work_count(database) == 0
    finally:
        await worker.close()


async def test_new_identical_input_after_last_admission_keeps_its_own_send(
    database, tmp_path, monkeypatch
):
    provider = _PausedProvider(
        [
            _tool("query_chat_history", {"view": "recent"}, "read"),
            _tool("send", {"text": ANSWER}, "answer"),
            "NO_REPLY",
            _tool("send", {"text": ANSWER}, "answer"),
            "NO_REPLY",
        ],
        pauses=(1, 2, 3),
    )
    env, harness, worker, message = await _scene(database, tmp_path, provider)
    sender = MemorySender()
    await _social_sender(env, sender, monkeypatch)
    try:
        a, c = await _start_and_observe(
            database, harness, worker, message, sender, provider, finish=False
        )
        provider.release[2].set()
        await provider.wait(3)
        # This is a different event with identical text, after A's final business
        # request was frozen. It must survive C's worker handoff.
        assert (await harness.processor.handle(message(4, C), sender)).reason == "ordinary_deferred"
        d_id = max(harness.processor._observed_human_turns)
        assert d_id != c.id
        key = next(iter(worker._states))
        provider.release[3].set()
        await asyncio.wait_for(_wait_for_worker(worker, key), 10)
        admissions = {row.event_id: row for row in await _admissions(database)}
        assert admissions[c.id].activation_id == admissions[a.id].activation_id
        assert admissions[d_id].activation_id != admissions[a.id].activation_id
        assert [message.text for message in sender.messages] == [ANSWER, ANSWER]
        async with database.read_session() as reader:
            receipts = list(await reader.scalars(select(SocialOperationModel)))
        assert len(receipts) == 2 and all(row.status == "succeeded" for row in receipts)
        assert len({row.payload_hash for row in receipts}) == 1
        assert len({row.event_id for row in receipts}) == 2
        assert {row.source_turn_id for row in receipts} == {
            f"{env.context.conversation_id}:event:{a.id}",
            f"{env.context.conversation_id}:event:{d_id}",
        }
        chains = [request.request_chain_id for request in provider.requests]
        assert chains[0] == chains[1] == chains[2]
        assert chains[3] == chains[4] != chains[0]
        assert await _work_count(database) == 0
    finally:
        await worker.close()


async def test_late_classification_does_not_claim_old_observation_or_replace_new_latest(
    database, tmp_path, monkeypatch
):
    provider = _PausedProvider(
        [_tool("query_chat_history", {"view": "recent"}, "read"), "NO_REPLY"]
    )
    _env, harness, worker, message = await _scene(
        database, tmp_path, provider, reason="continuation"
    )
    sender = MemorySender()
    service = harness.processor._participation
    original = service.continuation_for_event
    entered, release = asyncio.Event(), asyncio.Event()

    async def classify(event_id):
        if (await harness.ledger.get_event(event_id)).content == C:
            entered.set()
            await release.wait()
        return await original(event_id)

    monkeypatch.setattr(service, "continuation_for_event", classify)
    pending = None
    try:
        await harness.processor.handle(message(1, A), sender)
        await provider.wait(1)
        key = next(iter(worker._states))
        await harness.processor.handle(message(2, B), sender)
        pending = asyncio.create_task(harness.processor.handle(message(3, C), sender))
        await asyncio.wait_for(entered.wait(), 10)
        c = await _event(database, C)
        provider.release[1].set()
        await provider.wait(2)
        assert C in _plain(provider.entered[1])
        assert await harness.processor.ordinary_admissions.get(c.id) is None
        assert (await harness.processor.handle(message(4, "D：后到的新请求"), sender)).reason == (
            "ordinary_deferred"
        )
        d_id = max(harness.processor._observed_human_turns)
        release.set()
        assert (await pending).reason == "ordinary_deferred"
        assert worker._states[key].message.source_event_id == d_id
        provider.release[2].set()
        await asyncio.wait_for(_wait_for_worker(worker, key), 10)
        a = await _event(database, A)
        admissions = {row.event_id: row for row in await _admissions(database)}
        assert c.id not in admissions
        assert admissions[d_id].activation_id != admissions[a.id].activation_id
        assert len(provider.requests) == 3
        assert provider.requests[2].request_chain_id != provider.requests[0].request_chain_id
        assert await _work_count(database) == sender.calls == 0
    finally:
        release.set()
        if pending is not None:
            await asyncio.gather(pending, return_exceptions=True)
        await worker.close()


@pytest.mark.parametrize("cancel", [False, True])
async def test_accepted_input_is_not_reactivated_after_failure_or_cancellation(
    database, tmp_path, cancel
):
    provider = _PausedProvider(
        [
            _tool("query_chat_history", {"view": "recent"}, "read"),
            RuntimeError("synthetic provider failure"),
        ]
    )
    _env, harness, worker, message = await _scene(database, tmp_path, provider)
    sender = MemorySender()
    try:
        a, c = await _start_and_observe(
            database, harness, worker, message, sender, provider, finish=False
        )
        key = next(iter(worker._states))
        admissions = {row.event_id: row for row in await _admissions(database)}
        assert admissions[c.id].activation_id == admissions[a.id].activation_id
        if cancel:
            task = worker._states[key].task
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await asyncio.sleep(0)
        else:
            provider.release[2].set()
        await asyncio.wait_for(_wait_for_worker(worker, key), 10)
        assert len(provider.entered) == 2
        assert len({request.request_chain_id for request in provider.entered}) == 1
        assert (await harness.processor.ordinary_admissions.get(c.id)).activation_id == (
            admissions[a.id].activation_id
        )
        assert await _work_count(database) == sender.calls == 0
    finally:
        await worker.close()


@pytest.mark.parametrize("other_person", [False, True])
async def test_work_accepted_during_original_round_keeps_real_mailbox_authorization(
    database, tmp_path, other_person
):
    provider = _PausedProvider(
        [
            _tool("task_control", {"action": "accept", "goal": "处理原任务"}, "accept"),
            _tool("query_chat_history", {"view": "recent"}, "read"),
            _tool("task_control", {"action": "complete", "result": "原任务结束"}, "complete"),
            "NO_REPLY",
        ],
        pauses=(1, 2, 3),
    )
    _env, harness, worker, message = await _scene(database, tmp_path, provider)
    sender = MemorySender()
    try:
        await harness.processor.handle(message(1, A), sender)
        await provider.wait(1)
        key = next(iter(worker._states))
        provider.release[1].set()
        await provider.wait(2)
        control = provider.controls[1][1]
        assert control is not None
        work_id = control["id"]
        incoming = message(3, C)
        if other_person:
            async with database.immediate_session() as writer:
                person = await ensure_person(writer, "10002")
            incoming = replace(incoming, sender=SenderIdentity("10002"), person_id=person)
        result = await harness.processor.handle(incoming, sender)
        c = await _event(database, C)
        assert result.reason == ("ordinary_deferred" if other_person else "work_input_queued")
        admission = await harness.processor.ordinary_admissions.get(c.id)
        if other_person:
            assert admission is None
        else:
            assert admission.route == "work" and admission.work_id == work_id
            assert admission.input_id is not None
        provider.release[2].set()
        await provider.wait(3)
        assert provider.controls[2][1]["id"] == work_id
        assert C in _plain(provider.entered[2])
        if other_person:
            assert await harness.processor.ordinary_admissions.get(c.id) is None
        provider.release[3].set()
        await asyncio.wait_for(_wait_for_worker(worker, key), 10)
        async with database.read_session() as reader:
            rows = (await reader.execute(select(work))).mappings().all()
        assert len(rows) == 1 and rows[0]["id"] == work_id and rows[0]["state"] == "completed"
        admission = await harness.processor.ordinary_admissions.get(c.id)
        assert admission.route == ("ordinary" if other_person else "work")
        chains = {request.request_chain_id for request in provider.requests}
        assert len(chains) == (2 if other_person else 1)
        assert sender.calls == 0
    finally:
        await worker.close()


@pytest.mark.parametrize("entrance", ["direct", "semantic_invitation"])
async def test_direct_stays_immediate_and_invitation_joins_the_existing_worker(
    database, tmp_path, monkeypatch, entrance
):
    provider = _PausedProvider(
        [
            _tool("query_chat_history", {"view": "recent"}, "read")
            if entrance == "semantic_invitation"
            else "NO_REPLY",
            "NO_REPLY",
        ]
    )
    _env, harness, worker, message = await _scene(
        database,
        tmp_path,
        provider,
        reason="continuation" if entrance == "semantic_invitation" else "group_open",
    )
    sender = MemorySender()
    pending = None
    direct_entered = asyncio.Event()
    chat = harness.processor._chat
    original = chat.handle_turn

    async def handle_turn(incoming, *args, **kwargs):
        if incoming.text == "D：明确的新请求":
            direct_entered.set()
        return await original(incoming, *args, **kwargs)

    monkeypatch.setattr(chat, "handle_turn", handle_turn)
    if entrance == "semantic_invitation":
        service = harness.processor._participation
        classify = service.continuation_for_event

        async def invitation(event_id):
            binding, reason = await classify(event_id)
            if (await harness.ledger.get_event(event_id)).content == "D：明确的新请求":
                return binding, "semantic_invitation"
            return binding, reason

        monkeypatch.setattr(service, "continuation_for_event", invitation)
    try:
        await harness.processor.handle(message(1, A), sender)
        await provider.wait(1)
        key = next(iter(worker._states))
        await harness.processor.handle(message(3, C), sender)
        c = await _event(database, C)
        pending = asyncio.create_task(
            harness.processor.handle(
                message(4, "D：明确的新请求", mention=entrance == "direct"), sender
            )
        )
        if entrance == "semantic_invitation":
            assert (await pending).reason == "ordinary_deferred"
            d = await _event(database, "D：明确的新请求")
            assert await harness.processor.ordinary_admissions.get(d.id) is None
            assert worker._states[key].message.source_event_id == d.id
        else:
            # Direct input is admitted now; its model dispatch still obeys the
            # existing conversation lock.
            await asyncio.wait_for(direct_entered.wait(), 10)
            d = await _event(database, "D：明确的新请求")
            assert await harness.processor.ordinary_admissions.get(d.id) is not None
        assert await harness.processor.ordinary_admissions.get(c.id) is None
        provider.release[1].set()
        await provider.wait(2)
        assert "D：明确的新请求" in provider.entered[1].messages[-1].content
        if entrance == "semantic_invitation":
            admitted = await harness.processor.ordinary_admissions.get(d.id)
            original_admission = await harness.processor.ordinary_admissions.get(
                (await _event(database, A)).id
            )
            assert admitted.activation_id == original_admission.activation_id
            assert admitted.reason == "semantic_invitation"
            assert admitted.binding.event_id == d.id
        else:
            assert await harness.processor.ordinary_admissions.get(c.id) is None
        provider.release[2].set()
        if entrance == "direct":
            assert (await pending).reason == "chat"
        await asyncio.wait_for(_wait_for_worker(worker, key), 10)
        assert len(provider.entered) == 2
        if entrance == "semantic_invitation":
            assert provider.entered[0].request_chain_id == provider.entered[1].request_chain_id
            assert (await harness.processor.ordinary_admissions.get(d.id)).reason == (
                "semantic_invitation"
            )
        else:
            assert provider.entered[0].request_chain_id != provider.entered[1].request_chain_id
            assert await harness.processor.ordinary_admissions.get(c.id) is None
        assert await _work_count(database) == sender.calls == 0
    finally:
        for event in provider.release.values():
            event.set()
        if pending is not None:
            await asyncio.gather(pending, return_exceptions=True)
        await worker.close()


@pytest.mark.parametrize("failure", ["read", "save"])
async def test_derived_participation_database_error_does_not_block_selected_request(
    database, tmp_path, monkeypatch, failure
):
    provider = _PausedProvider([_tool("get_recent_chat_history", {}, "read"), "NO_REPLY"])
    _env, harness, worker, message = await _scene(
        database, tmp_path, provider, reason="continuation"
    )
    processor = harness.processor
    sender = MemorySender()
    try:
        await processor.handle(message(1, A), sender)
        await provider.wait(1)
        key = next(iter(worker._states))
        await processor.handle(message(2, C), sender)
        c = await _event(database, C)
        if failure == "save":
            processor._participation.on_ordinary_admitted.side_effect = SQLAlchemyError(
                "synthetic derived save failure"
            )
        else:
            record_feedback = processor._record_ordinary_participation

            async def fail_derived_read(event_id, binding):
                if event_id != c.id:
                    return await record_feedback(event_id, binding)
                with monkeypatch.context() as scoped:
                    scoped.setattr(
                        processor.ordinary_admissions,
                        "get",
                        AsyncMock(side_effect=SQLAlchemyError("synthetic derived read failure")),
                    )
                    await record_feedback(event_id, binding)

            monkeypatch.setattr(processor, "_record_ordinary_participation", fail_derived_read)
        provider.release[1].set()
        await provider.wait(2)
        a = await _event(database, A)
        admitted = await processor.ordinary_admissions.get(c.id)
        assert (
            admitted.activation_id == (await processor.ordinary_admissions.get(a.id)).activation_id
        )
        assert C in _plain(provider.entered[1])
        provider.release[2].set()
        await asyncio.wait_for(_wait_for_worker(worker, key), 10)
        assert len(provider.entered) == 2
        assert await _work_count(database) == sender.calls == 0
    finally:
        for release in provider.release.values():
            release.set()
        await worker.close()


async def test_derived_participation_read_still_propagates_cancellation(
    database, tmp_path, monkeypatch
):
    provider = _PausedProvider([], pauses=())
    _env, harness, worker, _message = await _scene(
        database, tmp_path, provider, reason="continuation"
    )
    monkeypatch.setattr(
        harness.processor.ordinary_admissions,
        "get",
        AsyncMock(side_effect=asyncio.CancelledError),
    )
    try:
        with pytest.raises(asyncio.CancelledError):
            await harness.processor._record_ordinary_participation(1, None)
        assert not provider.requests
    finally:
        await worker.close()


async def test_pending_input_survives_envelope_eviction_and_context_observations(
    database, tmp_path, monkeypatch
):
    provider = _PausedProvider(["NO_REPLY", "NO_REPLY"])
    _env, harness, worker, message = await _scene(
        database, tmp_path, provider, reason="continuation", runtime_work_enabled=False
    )
    processor = harness.processor
    service = processor._participation
    classify = service.continuation_for_event

    async def classify_humans(event_id):
        record = await harness.ledger.get_event(event_id)
        if record.content.startswith("仅观察"):
            return None
        return await classify(event_id)

    monkeypatch.setattr(service, "continuation_for_event", classify_humans)
    sender = MemorySender()
    try:
        await processor.handle(message(1, A), sender)
        await provider.wait(1)
        key = next(iter(worker._states))
        await processor.handle(message(2, C), sender)
        c = await _event(database, C)
        for number in range(257):
            observed = await processor.handle(message(3 + number, f"仅观察 {number}"), sender)
            assert observed.reason == "group_observed"
        assert worker._states[key].message.source_event_id == c.id
        assert await processor.ordinary_admissions.get(c.id) is None
        provider.release[1].set()
        await provider.wait(2)
        admitted = await processor.ordinary_admissions.get(c.id)
        assert admitted is not None and admitted.reason == "continuation"
        assert C in provider.entered[1].messages[-1].content
        provider.release[2].set()
        await asyncio.wait_for(_wait_for_worker(worker, key), 10)
        assert len(provider.entered) == 2 and sender.calls == 0
    finally:
        for release in provider.release.values():
            release.set()
        await worker.close()


async def test_background_invitation_waits_for_original_turn_without_losing_its_input(
    database, tmp_path, monkeypatch
):
    provider = _PausedProvider(["NO_REPLY", "NO_REPLY"])
    _env, harness, worker, message = await _scene(
        database, tmp_path, provider, reason="continuation", runtime_work_enabled=False
    )
    processor = harness.processor
    service = processor._participation
    classify = service.continuation_for_event
    invited = False

    async def before_observer(event_id):
        if (await harness.ledger.get_event(event_id)).content == C:
            return (
                (await service.binding_for_event(event_id), "semantic_invitation")
                if invited
                else None
            )
        return await classify(event_id)

    monkeypatch.setattr(service, "continuation_for_event", before_observer)
    sender = MemorySender()
    original_turn = asyncio.create_task(processor.handle(message(1, A, mention=True), sender))
    try:
        await provider.wait(1)
        assert (await processor.handle(message(2, C), sender)).reason == "group_observed"
        c = await _event(database, C)
        binding = await service.binding_for_event(c.id)
        invited = True
        waiting = asyncio.Event()
        conversation = processor._concurrency.conversation

        @asynccontextmanager
        async def handoff(conversation_key):
            waiting.set()
            async with conversation(conversation_key):
                yield

        monkeypatch.setattr(processor._concurrency, "conversation", handoff)
        assert (
            await processor.promote_committed_event(c.id, binding, reason="semantic_invitation")
        ).reason == "ordinary_deferred"
        await asyncio.wait_for(waiting.wait(), 10)
        key = next(iter(worker._states))
        assert await processor.ordinary_admissions.get(c.id) is None
        # A repeated observer callback retains the same source and current window.
        assert (
            await processor.promote_committed_event(c.id, binding, reason="semantic_invitation")
        ).reason == "ordinary_deferred"
        provider.release[1].set()
        assert (await original_turn).reason == "chat"
        await provider.wait(2)
        admitted = await processor.ordinary_admissions.get(c.id)
        assert admitted.reason == "semantic_invitation" and admitted.binding == binding
        assert provider.entered[0].request_chain_id != provider.entered[1].request_chain_id
        provider.release[2].set()
        await asyncio.wait_for(_wait_for_worker(worker, key), 10)
        assert len(provider.entered) == 2 and sender.calls == 0
    finally:
        for release in provider.release.values():
            release.set()
        await asyncio.gather(original_turn, return_exceptions=True)
        await worker.close()


async def test_source_changed_during_attachment_metadata_read_is_not_accepted(
    database, tmp_path, monkeypatch
):
    provider = _PausedProvider(
        [_tool("query_chat_history", {"view": "recent"}, "read"), "NO_REPLY"]
    )
    _env, harness, worker, message = await _scene(database, tmp_path, provider)
    sender = MemorySender()
    from yuki.services import processor as processor_module

    original_metadata = processor_module._attachment_metadata
    old_metadata = []

    async def prepare(incoming, record, selected_database):
        metadata = await original_metadata(incoming, record, selected_database)
        if not incoming.attachments:
            return metadata
        old_metadata.append(metadata)
        # A true source edit during the current indexed metadata read cannot
        # authorize its stale body under the replacement source revision.
        async with database.immediate_session() as writer:
            await writer.execute(
                update(ChatEventModel)
                .where(ChatEventModel.id == record.id)
                .values(
                    content="C：编辑后的新要求，附件也已替换",
                    segments_json=json.dumps(
                        [
                            {"type": "text", "data": {"text": "编辑后的新要求"}},
                            {"type": "image", "data": {"file": "new.png"}},
                        ]
                    ),
                )
            )
        return metadata

    monkeypatch.setattr(processor_module, "_attachment_metadata", prepare)
    incoming = replace(
        message(3, C),
        attachments=(MessageAttachment(AttachmentKind.IMAGE, "image", file="old.png"),),
        segments=(
            {"type": "text", "data": {"text": C}},
            {"type": "image", "data": {"file": "old.png"}},
        ),
    )
    try:
        await harness.processor.handle(message(1, A), sender)
        await provider.wait(1)
        key = next(iter(worker._states))
        a_id = worker._states[key].message.source_event_id
        await harness.processor.handle(incoming, sender)
        c_id = worker._states[key].message.source_event_id
        assert a_id is not None and c_id is not None and c_id != a_id
        provider.release[1].set()
        await provider.wait(2)
        admitted = await harness.processor.ordinary_admissions.get(c_id)
        original = await harness.processor.ordinary_admissions.get(a_id)
        assert admitted is None or admitted.activation_id != original.activation_id
        assert old_metadata and old_metadata[0]
        assert old_metadata[0] not in _plain(provider.entered[1])
        provider.release[2].set()
        await asyncio.wait_for(_wait_for_worker(worker, key), 10)
        assert all(old_metadata[0] not in _plain(request) for request in provider.requests)
        assert not any(item.images for request in provider.requests for item in request.messages)
        assert await _work_count(database) == sender.calls == 0
    finally:
        await worker.close()


async def test_source_changed_during_asr_does_not_publish_old_transcript_or_ordinary_input(
    database, tmp_path
):
    provider = _PausedProvider(
        [_tool("query_chat_history", {"view": "recent"}, "read"), "NO_REPLY"]
    )
    _env, harness, worker, message = await _scene(
        database, tmp_path, provider, reason="continuation"
    )
    sender = MemorySender()
    asr = _asr(harness.processor)
    original = asr.prepare.return_value

    async def prepare(incoming, *_args):
        async with database.immediate_session() as writer:
            await writer.execute(
                update(ChatEventModel)
                .where(ChatEventModel.id == incoming.source_event_id)
                .values(
                    content="C：编辑后的新要求，语音已替换",
                    segments_json=json.dumps(
                        [
                            {"type": "text", "data": {"text": "编辑后的新要求"}},
                            {"type": "record", "data": {"file": "new.amr"}},
                        ]
                    ),
                )
            )
        return original

    asr.prepare.side_effect = prepare
    incoming = replace(
        message(3, C),
        attachments=(MessageAttachment(AttachmentKind.AUDIO, "voice", file="old.amr"),),
        segments=(
            {"type": "text", "data": {"text": C}},
            {"type": "record", "data": {"file": "old.amr"}},
        ),
    )
    try:
        await harness.processor.handle(message(1, A), sender)
        await provider.wait(1)
        key = next(iter(worker._states))
        a_id = worker._states[key].message.source_event_id
        await harness.processor.handle(incoming, sender)
        c_id = worker._states[key].message.source_event_id
        assert a_id is not None and c_id is not None and c_id != a_id
        provider.release[1].set()
        await provider.wait(2)
        admitted = await harness.processor.ordinary_admissions.get(c_id)
        original_admission = await harness.processor.ordinary_admissions.get(a_id)
        assert admitted is None or admitted.activation_id != original_admission.activation_id
        assert VOICE_REQUIREMENT not in _plain(provider.entered[1])
        current = await harness.ledger.get_event(c_id)
        assert not current.audio_transcript
        provider.release[2].set()
        await asyncio.wait_for(_wait_for_worker(worker, key), 10)
        assert all(VOICE_REQUIREMENT not in _plain(request) for request in provider.requests)
        assert not (await harness.ledger.get_event(c_id)).audio_transcript
        assert await _work_count(database) == sender.calls == 0
    finally:
        await worker.close()
