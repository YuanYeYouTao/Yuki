"""Incoming voice provider, transport, persistence and real turn contracts."""

from __future__ import annotations

import asyncio
import base64
import json
from dataclasses import replace

import httpx
import pytest
from nonebot.adapters.onebot.v11 import Message, MessageSegment
from sqlalchemy import select, text
from tests.conftest import MemorySender, build_harness, make_settings
from tests.unit.test_normalizer import group_event, private_event

from qq_ai_bot.adapters.onebot.normalizer import normalize_event
from qq_ai_bot.asr.provider import ASRError, QwenASRProvider
from qq_ai_bot.asr.service import ASRService
from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
from qq_ai_bot.conversation.rollup.renderer import rollup_source_projection
from qq_ai_bot.domain.audio import AudioTranscript, parse_transcripts, serialize_transcripts
from qq_ai_bot.domain.messages import ChatResponse
from qq_ai_bot.event_prompt import ChatEventPromptRenderer
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.services.media_resolver import MediaResolutionError, MediaResolver
from qq_ai_bot.vision.models import MediaReference


class Recognizer:
    def __init__(self, text="我喜欢吃草莓", *, error=""):
        self.text = text
        self.error = error
        self.calls = 0

    async def transcribe(self, audio: bytes) -> str:
        self.calls += 1
        if self.error:
            raise ASRError(self.error)
        return self.text

    async def close(self):
        pass


class Gateway(MemorySender):
    def __init__(self):
        super().__init__()
        self.api_calls = []

    async def call_api(self, action, params):
        self.api_calls.append((action, params))
        return {
            "file": "/gateway/private/voice.mp3",
            "url": "https://expired.invalid/voice.silk",
            "base64": base64.b64encode(b"converted mp3").decode(),
        }


def voice(*, group=False, text="", message_id=31):
    message = MessageSegment.record("qq-voice.silk") + Message(text)
    if group:
        message = MessageSegment.at(9999) + message
    return normalize_event(
        group_event(message, message_id=message_id)
        if group
        else private_event(message, message_id=message_id)
    )


@pytest.fixture
def decoder(monkeypatch):
    async def prepare(content, **kwargs):
        assert content == b"converted mp3"
        return content

    monkeypatch.setattr("qq_ai_bot.asr.service.prepare_audio", prepare)


@pytest.mark.asyncio
async def test_qwen_wire_and_no_leaked_credentials():
    calls = []

    def handle(request: httpx.Request):
        calls.append(request)
        body = json.loads(request.content)
        assert request.url.path == "/compatible-mode/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer test-key"
        assert body["model"] == "qwen3-asr-flash"
        assert body["asr_options"] == {"enable_itn": False}
        assert len(body["messages"]) == 1 and "tools" not in body
        item = body["messages"][0]["content"][0]
        assert item["type"] == "input_audio"
        assert item["input_audio"]["data"] == "data:audio/mpeg;base64,bXAz"
        return httpx.Response(
            200,
            json={"choices": [{"finish_reason": "stop", "message": {"content": "你好，Yuki。"}}]},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        provider = QwenASRProvider(
            base_url="https://example.com/compatible-mode/v1/", api_key="test-key", client=client
        )
        assert await provider.transcribe(b"mp3") == "你好，Yuki。"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_provider_failures_are_sanitized_and_not_retried() -> None:
    for status, payload, code in [
        (401, {}, "not_configured"),
        (429, {}, "rate_limited"),
        (500, {}, "provider_failed"),
        (200, {"choices": []}, "invalid_response"),
        (200, {"choices": [1]}, "invalid_response"),
        (200, {"choices": [{"finish_reason": "length"}]}, "incomplete_transcript"),
        (200, {"choices": [{"finish_reason": "stop", "message": {"content": " "}}]}, "no_speech"),
    ]:
        await _check_provider_failures_are_sanitized_and_not_retried(status, payload, code)


async def _check_provider_failures_are_sanitized_and_not_retried(status, payload, code):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(status, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        provider = QwenASRProvider(base_url="https://example.com", api_key="secret", client=client)
        with pytest.raises(ASRError, match=code):
            await provider.transcribe(b"mp3")
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_record_conversion_prefers_bytes_and_refuses_container_path():
    resolver = MediaResolver()
    try:
        gateway = Gateway()
        result = await resolver.resolve_audio(MediaReference(file="qq-voice.silk"), gateway)
        assert result.content == b"converted mp3"
        assert gateway.api_calls == [("get_record", {"file": "qq-voice.silk", "out_format": "mp3"})]

        class PathOnly:
            async def call_api(self, *args):
                return {"file": "/etc/passwd"}

        with pytest.raises(MediaResolutionError, match="语音缺少"):
            await resolver.resolve_audio(MediaReference(file="qq-voice.silk"), PathOnly())
        with pytest.raises(MediaResolutionError):
            await resolver.resolve_audio(MediaReference(url="http://127.0.0.1/private"))
    finally:
        await resolver.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("group", [False, True])
async def test_voice_reaches_main_agent_history_search_and_rollup(
    database: Database, decoder, group
):
    provider = FakeLLMProvider(lambda _: ChatResponse("", 0))
    harness = build_harness(database, make_settings(database.url), provider)
    recognizer = Recognizer()
    asr = ASRService(settings=harness.settings.asr, provider=recognizer, resolver=MediaResolver())
    harness.processor._asr = asr
    gateway = Gateway()
    inbound = voice(group=group)
    try:
        result = await harness.processor.handle(inbound, gateway)
        assert result.sent_messages == 0, result
        request = provider.requests[-1]
        assert "我喜欢吃草莓" in "\n".join(m.content or "" for m in request.messages)
        assert all("base64" not in (m.content or "") for m in request.messages if m.role == "user")
        saved = await harness.ledger.find_by_platform_message(
            bot_user_id="9999", platform_message_id="31"
        )
        assert saved is not None
        assert "我喜欢吃草莓" not in saved.content  # Raw ingress remains immutable.
        assert "我喜欢吃草莓" in saved.evidence_content
        assert "我喜欢吃草莓" in ChatEventPromptRenderer((saved,)).render_reference_event(saved)
        assert "我喜欢吃草莓" in rollup_source_projection(saved)
        assert any(row.id == saved.id for row in await harness.ledger.search(keyword="喜欢吃草莓"))
        assert any(
            row.id == saved.id
            for row in await harness.ledger.search(keyword="草莓", user_id="1001")
        )
        repeated = await harness.processor.handle(inbound, gateway)
        assert repeated.sent_messages == 0
        assert recognizer.calls == 1
        followup = normalize_event(private_event(Message("我喜欢吃什么？"), message_id=32))
        if group:
            followup = normalize_event(
                group_event(MessageSegment.at(9999) + Message("我喜欢吃什么？"), message_id=32)
            )
        await harness.processor.handle(followup, gateway)
        assert "我喜欢吃草莓" in "\n".join(m.content or "" for m in provider.requests[-1].messages)
    finally:
        await asr.close()


@pytest.mark.asyncio
async def test_failed_audio_does_not_ask_model_to_guess(database: Database, decoder):
    provider = FakeLLMProvider()
    harness = build_harness(database, make_settings(database.url), provider)
    asr = ASRService(
        settings=harness.settings.asr,
        provider=Recognizer(error="no_speech"),
        resolver=MediaResolver(),
    )
    harness.processor._asr = asr
    gateway = Gateway()
    try:
        result = await harness.processor.handle(voice(), gateway)
        assert result.reason == "asr_no_speech"
        assert not provider.requests
        assert "没有识别出" in gateway.messages[0].text
        await harness.processor.handle(voice(text="这条语音说了什么", message_id=32), gateway)
        assert provider.requests
        assert "不得猜测" in "\n".join(m.content or "" for m in provider.requests[-1].messages)
    finally:
        await asr.close()


@pytest.mark.asyncio
async def test_spoken_admin_syntax_does_not_execute_command(database: Database, decoder):
    provider = FakeLLMProvider(lambda _: "听到了。")
    harness = build_harness(database, make_settings(database.url), provider)
    asr = ASRService(
        settings=harness.settings.asr, provider=Recognizer("/ai new"), resolver=MediaResolver()
    )
    harness.processor._asr = asr
    try:
        await harness.processor.handle(voice(), Gateway())
        assert provider.requests
        async with database.sessions() as session:
            conversation = await session.scalar(select(CanonicalConversationModel))
            assert conversation is not None and conversation.generation == 1
    finally:
        await asr.close()


@pytest.mark.asyncio
async def test_quote_is_understood_but_not_attributed_to_current_speaker(
    database: Database, decoder
):
    harness = build_harness(
        database, make_settings(database.url), FakeLLMProvider(lambda _: "听到了。")
    )
    inbound = voice(text="他刚才说什么？")
    inbound = replace(
        inbound,
        attachments=(),
        reply_attachments=tuple(replace(a, source="reply") for a in inbound.attachments),
        reply_sender_user_id="2000",
    )
    asr = ASRService(
        settings=harness.settings.asr, provider=Recognizer("我是小王"), resolver=MediaResolver()
    )
    harness.processor._asr = asr
    try:
        await harness.processor.handle(inbound, Gateway())
        saved = await harness.ledger.find_by_platform_message(
            bot_user_id="9999", platform_message_id="31"
        )
        assert saved and "我是小王" in saved.perceived_content
        assert "我是小王" not in saved.evidence_content
    finally:
        await asr.close()


@pytest.mark.asyncio
async def test_deadline_and_cancellation_release_queue(database: Database, decoder):
    started = asyncio.Event()

    class Hanging(Recognizer):
        async def transcribe(self, audio):
            started.set()
            await asyncio.Event().wait()
            return "never"

    settings = make_settings(database.url, asr_timeout_seconds=0.15, asr_queue_max_pending=1)
    service = ASRService(settings=settings.asr, provider=Hanging(), resolver=MediaResolver())
    try:
        task = asyncio.create_task(service.prepare(voice(), Gateway()))
        await started.wait()
        assert (await service.prepare(voice(), Gateway())).error == "busy"
        assert (await task).error == "timeout"
        assert service.health()["pending"] == 0
        task = asyncio.create_task(service.prepare(voice(), Gateway()))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert service.health()["pending"] == 0
    finally:
        await service.close()


def test_credential_reuse_and_inline_audio_redaction():
    settings = make_settings(
        "sqlite+aiosqlite://",
        vision_api_key="qwen-secret",
        vision_base_url="https://qwen.example/v1",
    )
    assert settings.asr_credentials == ("https://qwen.example/v1", "qwen-secret")
    explicit = make_settings(
        "sqlite+aiosqlite://", asr_base_url="https://new.example/v1", llm_api_key="deepseek-key"
    )
    assert explicit.asr_credentials == ("https://new.example/v1", "")
    payload = "data:audio/wav;base64,U0VDUkVU"
    message = normalize_event(private_event(Message(MessageSegment.record(payload))))
    assert message.attachments[0].file == payload
    assert payload not in json.dumps(message.segments)
    serialized = serialize_transcripts((AudioTranscript("current", 0, "hello"),))
    assert parse_transcripts(serialized)[0].text == "hello"


@pytest.mark.asyncio
async def test_new_cancels_recognition_without_late_history_or_reply(database, decoder):
    entered = asyncio.Event()

    class Hanging(Recognizer):
        async def transcribe(self, audio):
            entered.set()
            await asyncio.Event().wait()
            return "不应写入的新结果"

    provider = FakeLLMProvider()
    harness = build_harness(database, make_settings(database.url), provider)
    asr = ASRService(settings=harness.settings.asr, provider=Hanging(), resolver=MediaResolver())
    harness.processor._asr = asr
    gateway = Gateway()
    try:
        task = asyncio.create_task(harness.processor.handle(voice(), gateway))
        await asyncio.wait_for(entered.wait(), 2)
        await harness.processor.handle(
            normalize_event(private_event(Message("/ai new"), message_id=32)),
            gateway,
        )
        result = await asyncio.wait_for(task, 2)
        assert result.reason == "turn_interrupted"
        assert not provider.requests
        assert len(gateway.messages) == 1
        saved = await harness.ledger.find_by_platform_message(
            bot_user_id="9999",
            platform_message_id="31",
        )
        assert saved and not saved.audio_transcript
        assert asr.health()["pending"] == 0
        assert not await harness.ledger.set_audio_transcript(
            saved.id,
            serialize_transcripts((AudioTranscript("current", 0, "late"),)),
            generation=1,
        )
    finally:
        await asr.close()


@pytest.mark.asyncio
async def test_derived_audio_updates_revision_and_survives_migration_rollback(
    database, monkeypatch
):
    from alembic import command
    from alembic.config import Config

    from qq_ai_bot.persistence.schema_guard import require_canonical_schema

    harness = build_harness(database, make_settings(database.url))
    await harness.processor.handle(voice(), Gateway())  # Unconfigured ASR leaves a source row.
    saved = await harness.ledger.find_by_platform_message(
        bot_user_id="9999", platform_message_id="31"
    )
    assert saved
    async with database.sessions() as session:
        conversation = await session.get(
            CanonicalConversationModel, saved.canonical_conversation_id
        )
        before = conversation.prompt_source_revision
        count = conversation.uncovered_character_count
        generation = conversation.generation
    transcript = serialize_transcripts((AudioTranscript("current", 0, "迁移之后还能查到草莓"),))
    assert await harness.ledger.set_audio_transcript(saved.id, transcript, generation=generation)
    async with database.sessions() as session:
        conversation = await session.get(
            CanonicalConversationModel, saved.canonical_conversation_id
        )
        assert conversation.prompt_source_revision > before
        assert conversation.uncovered_character_count > count
    monkeypatch.setenv("DATABASE_URL", database.url)
    config = Config("alembic.ini")
    await asyncio.to_thread(command.stamp, config, "0055")
    await asyncio.to_thread(command.downgrade, config, "0054")
    # Original chat text still works with the previous FTS schema; no speech is erased.
    preserved = await harness.ledger.get_event(saved.id)
    assert preserved and preserved.audio_transcript == transcript
    # The isolated fixture starts with current ORM metadata, while this test
    # stamps it as an older revision to exercise the ASR migration path.
    async with database.engine.begin() as connection:
        await connection.execute(text("DROP TABLE canonical_generation_reset_batches"))
        await connection.execute(text("DROP TABLE conversation_media_items"))
    await asyncio.to_thread(command.upgrade, config, "head")
    await require_canonical_schema(database.url)
    assert any(r.id == saved.id for r in await harness.ledger.search(keyword="迁移之后"))


def test_asr_evidence_is_validated_only_for_its_actual_speaker():
    from tests.unit.test_memory_v2 import _claim, _event

    from qq_ai_bot.memory.validation import MemoryClaimValidator

    event = replace(
        _event(),
        content="",
        audio_transcript=serialize_transcripts((AudioTranscript("current", 0, "我准备考研"),)),
    )
    assert MemoryClaimValidator().validate(_claim(), event) is not None
    quoted = replace(
        event, audio_transcript=serialize_transcripts((AudioTranscript("reply", 0, "我准备考研"),))
    )
    assert MemoryClaimValidator().validate(_claim(), quoted) is None


@pytest.mark.asyncio
async def test_late_transcript_rebuilds_already_covered_rollup(database):
    from datetime import UTC, datetime

    from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationRollupModel

    harness = build_harness(database, make_settings(database.url))
    await harness.processor.handle(voice(), Gateway())
    source = await harness.ledger.find_by_platform_message(
        bot_user_id="9999", platform_message_id="31"
    )
    assert source
    async with database.sessions() as session, session.begin():
        conversation = await session.get(
            CanonicalConversationModel, source.canonical_conversation_id
        )
        generation = conversation.generation
        conversation.covered_through_event_id = source.id
        session.add(
            CanonicalConversationRollupModel(
                conversation_id=conversation.id,
                generation=generation,
                covered_through_event_id=source.id,
                summary_text="这条语音尚未识别。",
                summary_kind="model",
                source_fingerprint="a" * 64,
                revision=1,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )
    assert await harness.ledger.set_audio_transcript(
        source.id,
        serialize_transcripts((AudioTranscript("current", 0, "补全这条语音"),)),
        generation=generation,
    )
    async with database.sessions() as session:
        assert (
            await session.get(CanonicalConversationRollupModel, source.canonical_conversation_id)
            is None
        )
        conversation = await session.get(
            CanonicalConversationModel, source.canonical_conversation_id
        )
        assert conversation.covered_through_event_id == conversation.starts_after_event_id
        assert conversation.uncovered_character_count > len("补全这条语音")
