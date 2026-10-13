"""#267: implicit group sources merge by quiet window; original Work input goes first."""

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, update
from tests.conftest import MemorySender, build_harness, make_settings
from tests.support.social_identity_cases import social_env

from qq_ai_bot.admin.models import ConversationRuntimeConfig
from qq_ai_bot.asr.service import AudioInput
from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
from qq_ai_bot.conversation.ordinary_admission_db_models import OrdinaryTurnAdmissionModel
from qq_ai_bot.conversation.scope import runtime_conversation_key
from qq_ai_bot.domain.audio import AudioTranscript, serialize_transcripts
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import (
    AttachmentKind,
    InboundMessage,
    MessageAttachment,
    SenderIdentity,
)
from qq_ai_bot.identity.db_models import CanonicalSpaceModel
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.persistence.models import MemoryJobModel
from qq_ai_bot.runtime.work_control import WorkControl
from qq_ai_bot.runtime.work_repository import WorkRepository
from qq_ai_bot.runtime.work_schema_v1 import inputs
from qq_ai_bot.runtime.work_wait import WorkWaitRepository
from qq_ai_bot.services.autonomous_groups import AutonomousGroupService
from qq_ai_bot.services.turn_coordinator import ConversationTurnCoordinator


def _policy(*, debounce: float, autonomous: bool = False) -> ConversationRuntimeConfig:
    return ConversationRuntimeConfig(
        autonomous_enabled=autonomous,
        autonomous_debounce_seconds=debounce,
        autonomous_admission_threshold=100,
        autonomous_batch_limit=1,
        autonomous_presence_window_seconds=0,
        interrupt_autonomous_on_new_message=True,
    )


class _Runtime:
    def __init__(self, policy: ConversationRuntimeConfig) -> None:
        self.policy = policy

    async def snapshot(self, **_: object) -> SimpleNamespace:
        return SimpleNamespace(conversation_policy=lambda: self.policy)


def _message(event_id: int, *, group: str = "20001") -> InboundMessage:
    return InboundMessage(
        message_id=f"m{event_id}",
        event_type="message:test",
        scope_type=ScopeType.GROUP,
        sender=SenderIdentity("10001"),
        text=f"片段{event_id}",
        bot_user_id="80001",
        group_id=group,
        source_event_id=event_id,
    )


def _service(policy: ConversationRuntimeConfig) -> AutonomousGroupService:
    return AutonomousGroupService(
        chat=SimpleNamespace(),  # type: ignore[arg-type]
        admission_features=SimpleNamespace(),  # type: ignore[arg-type]
        runtime_config=_Runtime(policy),  # type: ignore[arg-type]
        turn_coordinator=ConversationTurnCoordinator(),
    )


def _key(message: InboundMessage) -> str:
    return runtime_conversation_key(identity=message.scope(), inbound=message)


async def _wait_for_worker(service: AutonomousGroupService, scope_key: str) -> None:
    while (state := service._states.get(scope_key)) is not None and state.task is not None:
        await asyncio.shield(state.task)
        await asyncio.sleep(0)


async def test_worker_attempts_only_latest_source_and_ignores_older_classification() -> None:
    service = _service(_policy(debounce=0.05))
    attempts: list[int] = []

    def attempt_for(event_id: int):
        async def attempt() -> int:
            attempts.append(event_id)
            return 1

        return attempt

    first = _message(100)
    service.observe(first, ordinary_attempt=attempt_for(100))
    await asyncio.sleep(0.02)
    service.observe(_message(102), ordinary_attempt=attempt_for(102))
    # A slower classification of an older source neither wins nor extends the window.
    service.observe(_message(101), ordinary_attempt=attempt_for(101))
    service.observe(_message(102), ordinary_attempt=attempt_for(102))
    await _wait_for_worker(service, _key(first))

    assert attempts == [102]


async def test_worker_window_zero_runs_ordinary_even_without_autonomy() -> None:
    service = _service(_policy(debounce=0))
    attempts: list[int] = []
    other_group: list[int] = []

    async def attempt() -> int:
        attempts.append(1)
        return 0

    async def other() -> int:
        other_group.append(1)
        return 0

    message = _message(7)
    service.observe(message, ordinary_attempt=attempt)
    service.observe(_message(8, group="20002"), ordinary_attempt=other)
    # Legacy observation without an attempt still needs autonomy and stays idle.
    service.observe(_message(9, group="20003"))
    await _wait_for_worker(service, _key(message))
    await _wait_for_worker(service, _key(_message(8, group="20002")))

    assert attempts == [1] and other_group == [1]
    await service.close()


async def _open_group_scene(database, tmp_path, provider, *, debounce: float):
    env = await social_env(database, tmp_path)
    async with database.sessions() as session, session.begin():
        await session.execute(
            update(CanonicalSpaceModel)
            .where(CanonicalSpaceModel.id == env.space)
            .values(require_mention=False)
        )
    harness = build_harness(
        database,
        make_settings(
            database.url,
            enabled_groups_csv="20001",
            conversation_autonomous_debounce_seconds=debounce,
        ),
        provider,
    )
    processor = harness.processor
    autonomous = AutonomousGroupService(
        chat=processor._chat,
        admission_features=SimpleNamespace(),  # type: ignore[arg-type]
        runtime_config=processor._runtime_config,
        turn_coordinator=processor._turn_coordinator,
    )
    processor._autonomous = autonomous

    def message(index: int, text: str, *, mention: bool = False) -> InboundMessage:
        return InboundMessage(
            message_id=f"open-{index}",
            event_type="message:test",
            scope_type=ScopeType.GROUP,
            sender=SenderIdentity("10001"),
            text=text,
            bot_user_id="80001",
            group_id="20001",
            mentions_bot=mention,
            conversation_id=env.context.conversation_id,
            person_id=env.person,
            presence_id=env.presence,
            space_id=env.space,
        )

    return env, harness, autonomous, message


async def _admissions(database) -> list[OrdinaryTurnAdmissionModel]:
    async with database.sessions() as session:
        return list((await session.scalars(select(OrdinaryTurnAdmissionModel))).all())


async def test_open_floor_fragments_admit_one_turn_for_latest_source(database, tmp_path):
    provider = FakeLLMProvider()
    _env, harness, autonomous, message = await _open_group_scene(
        database, tmp_path, provider, debounce=0.2
    )
    sender = MemorySender()
    fragments = [
        message(index, text) for index, text in enumerate(("我想问", "那个", "明天几点开会"))
    ]
    results = [await harness.processor.handle(fragment, sender) for fragment in fragments]
    assert [result.reason for result in results] == ["ordinary_deferred"] * 3
    assert provider.requests == [] and await _admissions(database) == []

    # Same keeper resent: no new version, no new window, no second source.
    coordinator = harness.processor._turn_coordinator
    latest = max(harness.processor._observed_human_turns)
    key = harness.processor._observed_human_turns[latest].snapshot.scope_key
    version = coordinator._states[key].version
    resent = await harness.processor.handle(fragments[-1], sender)
    assert resent.reason == "group_observed"
    assert coordinator._states[key].version == version

    await _wait_for_worker(autonomous, key)
    admitted = await _admissions(database)
    assert [row.event_id for row in admitted] == [latest]
    assert len(provider.requests) >= 1
    await autonomous.close()


async def test_explicit_call_during_window_cancels_pending_open_source(database, tmp_path):
    provider = FakeLLMProvider()
    _env, harness, autonomous, message = await _open_group_scene(
        database, tmp_path, provider, debounce=0.2
    )
    sender = MemorySender()
    assert (await harness.processor.handle(message(1, "在吗"), sender)).reason == (
        "ordinary_deferred"
    )
    pending = next(iter(harness.processor._observed_human_turns.values()))
    direct = await harness.processor.handle(message(2, "帮我看看", mention=True), sender)
    assert direct.reason == "chat"
    await _wait_for_worker(autonomous, pending.snapshot.scope_key)

    admitted = await _admissions(database)
    assert len(admitted) == 1
    assert admitted[0].event_id not in harness.processor._observed_human_turns
    await autonomous.close()


async def test_open_floor_rechecks_require_mention_when_window_closes(database, tmp_path):
    provider = FakeLLMProvider()
    env, harness, autonomous, message = await _open_group_scene(
        database, tmp_path, provider, debounce=0.2
    )
    assert (await harness.processor.handle(message(1, "有人吗"), MemorySender())).reason == (
        "ordinary_deferred"
    )
    pending = next(iter(harness.processor._observed_human_turns.values()))
    async with database.sessions() as session, session.begin():
        await session.execute(
            update(CanonicalSpaceModel)
            .where(CanonicalSpaceModel.id == env.space)
            .values(require_mention=True)
        )
    await _wait_for_worker(autonomous, pending.snapshot.scope_key)

    assert await _admissions(database) == [] and provider.requests == []
    await autonomous.close()


class _Repository:
    def __init__(self, database: object, rows: list[dict[str, object]]) -> None:
        self.database = database
        self.rows = rows
        self.staged: list[int] = []

    async def input_images(self, _item: dict[str, object]) -> tuple[()]:
        return ()

    async def has_pending_business_inputs(self, *_args: object, **_kwargs: object) -> bool:
        return False

    async def stage(self, _lease: object, selected: list[int], _attempt: str) -> None:
        self.staged.extend(selected)


async def test_signal_with_visible_anchor_keeps_wait_payload(database) -> None:
    signal = {
        "kind": "work_signal",
        "wait_id": "wait-1",
        "reason": "signal",
        "conditions": [{"kind": "conversation", "matched": {"event_id": 100}}],
    }
    rows = [
        {
            "id": 1,
            "kind": "control",
            "event_id": 100,
            "ready": True,
            "payload_json": json.dumps({"text": json.dumps(signal), "signal": True}),
        },
        {
            "id": 2,
            "kind": "message",
            "event_id": 100,
            "ready": True,
            "payload_json": json.dumps({"text": "原文"}),
        },
    ]
    repository = _Repository(database, rows)

    class _Control(WorkControl):
        async def pending(self) -> list[dict[str, object]]:  # type: ignore[override]
            return rows

        async def reconcile_completed_children(self) -> None:
            return None

    control = _Control.__new__(_Control)
    control.repository = repository  # type: ignore[assignment]
    control.session = None
    control.current = {"id": "work-1"}
    control.lease = SimpleNamespace(conversation_id="c", generation=1)  # type: ignore[assignment]
    messages = await control.take_inputs("a1", observed_event_ids=frozenset({100}))

    assert "wait-1" in messages[0].content and "Work 信号" in messages[0].content
    assert "已在当前聊天展示" in messages[1].content
    assert repository.staged == [1, 2]


async def test_open_floor_input_reaches_original_wait_without_waiting_window(database, tmp_path):
    from qq_ai_bot.runtime.work_repository import WorkRepository
    from qq_ai_bot.runtime.work_wait import WorkWaitRepository

    provider = FakeLLMProvider()
    env, harness, autonomous, message = await _open_group_scene(
        database, tmp_path, provider, debounce=5
    )
    repository = WorkRepository(database)
    lease = await repository.acquire(env.context.conversation_id, 1)
    assert lease

    async def validate() -> None:
        assert await repository.valid(lease)

    source = {"actor_person_id": env.person, "principal_kind": "person", "origin": "user_message"}
    control = WorkControl(repository, lease, "open-floor-wait", source, validate)
    assert json.loads(
        await control.execute("task_control", {"action": "accept", "goal": "等回复"}, "accept")
    )["ok"]
    work_id = control.current["id"]
    await WorkWaitRepository(repository).register(
        lease,
        work_id=work_id,
        source=control.source,
        call_key="wait-conversation",
        mode="any",
        conditions=[{"kind": "conversation"}],
        deadline_at=None,
    )

    result = await harness.processor.handle(message(1, "好了，结果是42"), MemorySender())

    assert result.reason == "work_input_queued"
    admitted = await _admissions(database)
    assert len(admitted) == 1 and admitted[0].route == "work"
    assert admitted[0].work_id == work_id
    # Nothing was left for the quiet window to open as a second Main.
    assert autonomous._states == {} and provider.requests == []
    await autonomous.close()


VOICE_REQUIREMENT = "本周改为周四提交，必须保留这一要求"


def _voice(message):
    return replace(
        message,
        text="",
        attachments=(MessageAttachment(AttachmentKind.AUDIO, "voice", file="synthetic.amr"),),
    )


def _asr(processor, *, source="current"):
    transcript = serialize_transcripts((AudioTranscript(source, 0, VOICE_REQUIREMENT),))
    service = SimpleNamespace(prepare=AsyncMock(return_value=AudioInput(transcript=transcript)))
    processor._asr = service
    return service


async def _original_wait(database, env, *, conditions=None, mode="any"):
    repository = WorkRepository(database)
    lease = await repository.acquire(env.context.conversation_id, 1)
    assert lease

    async def validate():
        assert await repository.valid(lease)

    control = WorkControl(
        repository,
        lease,
        "voice-input-wait",
        {"actor_person_id": env.person, "principal_kind": "person", "origin": "user_message"},
        validate,
    )
    assert json.loads(
        await control.execute("task_control", {"action": "accept", "goal": "等待原消息"}, "accept")
    )["ok"]
    registered = await WorkWaitRepository(repository).register(
        lease,
        work_id=control.current["id"],
        source=control.source,
        call_key="voice-input-wait",
        mode=mode,
        conditions=conditions or [{"kind": "conversation"}],
        deadline_at=None,
    )
    return repository, control, registered


@pytest.mark.parametrize("mention", [False, True])
@pytest.mark.parametrize("source", ["current", "reply"])
async def test_original_wait_prepares_voice_and_memory_before_publication(
    database, tmp_path, mention, source
):
    provider = FakeLLMProvider()
    env, harness, worker, message = await _open_group_scene(
        database, tmp_path, provider, debounce=60
    )
    async with database.immediate_session() as writer:
        await writer.execute(
            update(CanonicalSpaceModel)
            .where(CanonicalSpaceModel.id == env.space)
            .values(require_mention=True)
        )
    service = _asr(harness.processor, source=source)
    _repository, control, registered = await _original_wait(database, env)
    try:
        incoming = _voice(message(1, "", mention=mention))
        if source == "reply":
            incoming = replace(
                incoming,
                text="请阅读引用的语音",
                attachments=(),
                reply_attachments=tuple(
                    replace(item, source="reply") for item in incoming.attachments
                ),
            )
        result = await harness.processor.handle(incoming, MemorySender())
        assert result.reason == "work_input_queued" and not provider.requests
        admission = (await _admissions(database))[0]
        assert admission.work_id == control.current["id"]
        event = await harness.ledger.get_event(admission.event_id)
        assert VOICE_REQUIREMENT in event.audio_transcript
        current = await harness.processor.ordinary_admissions.current(event.id)
        assert current is not None and current.activation_id == admission.activation_id
        async with database.sessions() as reader:
            assert await reader.scalar(
                select(MemoryJobModel.id).where(MemoryJobModel.event_id == event.id)
            )
            payload = json.loads(
                await reader.scalar(
                    select(inputs.c.payload_json).where(inputs.c.id == admission.input_id)
                )
            )
        assert json.loads(payload["text"])["wait_id"] == registered["id"]
        selected = await control.take_inputs("voice-signal")
        assert VOICE_REQUIREMENT in selected[0].content
        if source == "reply":
            assert "回复消息中的语音转写" in selected[0].content
            assert VOICE_REQUIREMENT not in event.evidence_content
        assert service.prepare.await_count == 1
    finally:
        await worker.close()


async def test_open_floor_merge_keeps_prior_voice_in_actual_main_input(database, tmp_path):
    provider = FakeLLMProvider(lambda _: "NO_REPLY")
    _env, harness, worker, message = await _open_group_scene(
        database, tmp_path, provider, debounce=0.2
    )
    service = _asr(harness.processor)
    try:
        assert (await harness.processor.handle(_voice(message(1, "")), MemorySender())).reason == (
            "ordinary_deferred"
        )
        observed = next(iter(harness.processor._observed_human_turns.values()))
        assert (
            await harness.processor.handle(message(2, "请按刚才语音的要求处理"), MemorySender())
        ).reason == "ordinary_deferred"
        await _wait_for_worker(worker, observed.snapshot.scope_key)
        event = await harness.ledger.get_event(observed.record.id)
        assert VOICE_REQUIREMENT in event.audio_transcript
        assert len(await _admissions(database)) == 1
        assert len(provider.requests) == 1
        submitted = "\n".join(item.content or "" for item in provider.requests[0].messages)
        assert VOICE_REQUIREMENT in submitted and "请按刚才语音的要求处理" in submitted
        assert service.prepare.await_count == 1
    finally:
        await worker.close()


async def test_unmatched_group_voice_does_not_buy_asr_or_enqueue_memory(database, tmp_path):
    provider = FakeLLMProvider()
    env, harness, worker, message = await _open_group_scene(
        database, tmp_path, provider, debounce=0
    )
    async with database.immediate_session() as writer:
        await writer.execute(
            update(CanonicalSpaceModel)
            .where(CanonicalSpaceModel.id == env.space)
            .values(require_mention=True)
        )
    service = _asr(harness.processor)
    try:
        result = await harness.processor.handle(_voice(message(1, "")), MemorySender())
        assert result.reason == "group_observed"
        assert not provider.requests and not await _admissions(database)
        service.prepare.assert_not_awaited()
        async with database.sessions() as reader:
            assert await reader.scalar(select(MemoryJobModel.id)) is None
    finally:
        await worker.close()


@pytest.mark.parametrize("waiting", [False, True])
async def test_overlong_implicit_group_input_keeps_original_wait_unpublished(
    database, tmp_path, waiting
):
    provider = FakeLLMProvider()
    env, harness, worker, message = await _open_group_scene(
        database, tmp_path, provider, debounce=60
    )
    async with database.immediate_session() as writer:
        await writer.execute(
            update(CanonicalSpaceModel)
            .where(CanonicalSpaceModel.id == env.space)
            .values(require_mention=True)
        )
    if waiting:
        repository, control, _registered = await _original_wait(database, env)
    sender = MemorySender()
    try:
        result = await harness.processor.handle(
            message(1, "x" * (harness.settings.max_input_characters + 1)), sender
        )
        assert result.reason == ("input_too_long" if waiting else "group_observed")
        assert not await _admissions(database) and not provider.requests
        assert len(sender.messages) == int(waiting)
        if waiting:
            assert "消息过长" in sender.messages[0].text
            assert (await WorkWaitRepository(repository).describe(control.current["id"]))[
                "status"
            ] == "active"
        async with database.sessions() as reader:
            assert await reader.scalar(select(inputs.c.id)) is None
    finally:
        await worker.close()


async def test_wait_source_generation_change_during_asr_does_not_publish(database, tmp_path):
    provider = FakeLLMProvider()
    env, harness, worker, message = await _open_group_scene(
        database, tmp_path, provider, debounce=60
    )
    await _original_wait(database, env)
    service = _asr(harness.processor)
    transcript = service.prepare.return_value

    async def reset(*_args):
        async with database.immediate_session() as writer:
            await writer.execute(
                update(CanonicalConversationModel)
                .where(CanonicalConversationModel.id == env.context.conversation_id)
                .values(generation=2)
            )
        return transcript

    service.prepare.side_effect = reset
    try:
        result = await harness.processor.handle(
            _voice(message(1, "", mention=True)), MemorySender()
        )
        assert result.reason == "turn_interrupted"
        assert not await _admissions(database) and not provider.requests
        async with database.sessions() as reader:
            assert await reader.scalar(select(inputs.c.id)) is None
    finally:
        await worker.close()


async def test_partial_wait_keeps_voice_before_later_time_signal(database, tmp_path):
    provider = FakeLLMProvider()
    env, harness, worker, message = await _open_group_scene(
        database, tmp_path, provider, debounce=60
    )
    service = _asr(harness.processor)
    repository, control, registered = await _original_wait(
        database,
        env,
        mode="all",
        conditions=[{"kind": "conversation"}, {"kind": "time_due", "after_seconds": 0}],
    )
    try:
        result = await harness.processor.handle(_voice(message(1, "")), MemorySender())
        assert result.reason == "work_wait_partial"
        assert not await _admissions(database) and not provider.requests
        described = await WorkWaitRepository(repository).describe(control.current["id"])
        assert described["status"] == "active"
        event_id = described["conditions"][0]["matched"]["event_id"]
        assert VOICE_REQUIREMENT in (await harness.ledger.get_event(event_id)).audio_transcript
        assert await WorkWaitRepository(repository).deliver_due() == 1
        selected = await control.take_inputs("partial-voice-signal")
        assert registered["id"] in selected[0].content and VOICE_REQUIREMENT in selected[0].content
        assert service.prepare.await_count == 1
    finally:
        await worker.close()


@pytest.mark.parametrize("cancel", [False, True])
async def test_failed_or_cancelled_wait_voice_does_not_publish_unprepared_input(
    database, tmp_path, cancel
):
    provider = FakeLLMProvider()
    env, harness, worker, message = await _open_group_scene(
        database, tmp_path, provider, debounce=60
    )
    repository, control, _registered = await _original_wait(database, env)
    entered, release = asyncio.Event(), asyncio.Event()
    service = _asr(harness.processor)

    async def prepare(*_args):
        entered.set()
        if cancel:
            await release.wait()
        return AudioInput(error="no_speech")

    service.prepare.side_effect = prepare
    task = asyncio.create_task(
        harness.processor.handle(_voice(message(1, "", mention=True)), MemorySender())
    )
    try:
        await entered.wait()
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            assert (await task).reason == "asr_no_speech"
        assert not await _admissions(database) and not provider.requests
        assert (await WorkWaitRepository(repository).describe(control.current["id"]))[
            "status"
        ] == "active"
        async with database.sessions() as reader:
            assert await reader.scalar(select(inputs.c.id)) is None
            assert await reader.scalar(select(MemoryJobModel.id)) is None
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await worker.close()
