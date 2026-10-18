"""Voice transcription and the shared raw cache consume one admitted audio source."""

import asyncio
import base64
import io
import shutil
import wave
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from tests.conftest import make_settings
from tests.integration import test_telegram_runtime as native
from tests.integration.test_telegram_event_edits import media_reader  # noqa: F401
from tests.integration.test_telegram_runtime import telegram_env  # noqa: F401
from tests.support.social_identity_cases import social_env

from yuki.asr.service import ASRService
from yuki.conversation.media_service import ConversationMediaService
from yuki.domain.audio import parse_transcripts
from yuki.domain.conversations import ConversationScope, ScopeType
from yuki.domain.messages import AttachmentKind, InboundMessage, MessageAttachment, SenderIdentity
from yuki.services.image_preprocessor import ImagePreprocessor
from yuki.services.media_resolver import MediaResolver


def wave_bytes(sample_count: int) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as source:
        source.setnchannels(1)
        source.setsampwidth(2)
        source.setframerate(16_000)
        source.writeframes(b"\x00\x00" * sample_count)
    return output.getvalue()


@pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="actual audio normalization"
)
@pytest.mark.parametrize("kind", ["record", "audio"])
@pytest.mark.parametrize("inline", [False, True])
async def test_audio_original_cache_and_real_asr_transcode_share_one_download(
    database, tmp_path, kind, inline
):
    env = await social_env(database, tmp_path)
    content = wave_bytes(4_000)
    downloads = []

    async def respond(request):
        downloads.append(request)
        return httpx.Response(200, content=content, headers={"content-type": "audio/wav"})

    locator = (
        "data:audio/wav;base64," + base64.b64encode(content).decode()
        if inline
        else "https://source.test/voice.wav"
    )
    segment = {"type": kind, "data": {"file": locator, "name": "voice.wav"}}
    event = (
        await env.service.writer.append(
            scope=ConversationScope.group("80001", "20001"),
            platform_message_id="audio-shared",
            sender_user_id="10001",
            direction="inbound",
            content="",
            segments=(segment,),
        )
    ).event
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        resolver = MediaResolver(client=client, host_resolver=lambda *_args: ("8.8.8.8",))
        media = ConversationMediaService(
            database, tmp_path / "cache", resolver, ImagePreprocessor()
        )
        transcript = AsyncMock(return_value="识别后的真实语音")
        provider = SimpleNamespace(transcribe=transcript, close=AsyncMock())
        asr = ASRService(
            settings=make_settings(database.url, asr_enabled=True).asr,
            provider=provider,
            resolver=resolver,
        )
        incoming = InboundMessage(
            "audio-shared",
            "message",
            ScopeType.GROUP,
            SenderIdentity("10001"),
            text="",
            group_id="20001",
            bot_user_id="80001",
            source_event_id=event.id,
            conversation_id=event.canonical_conversation_id,
            segments=(segment,),
            attachments=(
                MessageAttachment(
                    AttachmentKind.AUDIO,
                    "voice",
                    segment_index=0,
                    file=locator,
                    filename="voice.wav",
                ),
            ),
        )
        incoming = media.prepare_message(incoming, None, source_segments=event.segments)
        try:
            _, result = await asyncio.gather(
                media._cache_event(event.id, None, incoming), asr.prepare(incoming, None)
            )
            item, path = await media.authorized_path(event_id=event.id, attachment_index=0)
            assert item.kind == kind and path.read_bytes() == content
            assert len(downloads) == (0 if inline else 1) and transcript.await_count == 1
            normalized = transcript.await_args.args[0]
            assert normalized and normalized != content
            assert [
                (part.source, part.segment_index, part.text)
                for part in parse_transcripts(result.transcript)
            ] == [("current", 0, "识别后的真实语音")]
            assert not result.error
        finally:
            await media.close()
            await asr.close()


@pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="actual audio normalization"
)
@pytest.mark.parametrize("reply_has_media", [False, True])
async def test_known_reply_audio_uses_edited_original_position_instead_of_old_platform_snapshot(
    telegram_env,  # noqa: F811
    media_reader,  # noqa: F811
    database,
    monkeypatch,
    reply_has_media,
):
    from yuki.asr.audio import prepare_audio

    env, reader = telegram_env, media_reader
    env.model._responder = lambda _: "NO_REPLY"
    reader.contents.update({"old-photo": wave_bytes(4_000), "new-photo": wave_bytes(8_000)})
    transcribe = AsyncMock(return_value="真实原件的语音转写")
    asr = ASRService(
        settings=make_settings(database.url, asr_enabled=True).asr,
        provider=SimpleNamespace(transcribe=transcribe, close=AsyncMock()),
        resolver=reader.service.resolver,
    )
    env.harness.processor._asr = asr
    env.harness.processor._conversation_media = reader.service
    monkeypatch.setattr(env.gateway.client, "resolve_media_file", reader.gateway.resolve_media_file)
    original = native.update(text="")
    original["message"]["voice"] = {
        "file_id": "old-photo",
        "file_name": "voice.wav",
        "mime_type": "audio/wav",
    }
    try:
        await env.gateway.receive(original)
        await native.drain(env)
        await asyncio.gather(*tuple(reader.service._pending))
        edit = native.update(3, text="原音频现在带有说明", edit=True)
        edit["edited_message"]["voice"] = {
            "file_id": "new-photo",
            "file_name": "voice.wav",
            "mime_type": "audio/wav",
        }
        await env.gateway.receive(edit)
        # Telegram's received reply snapshot still points to the old positional
        # shape. Canonical ingress has the real original event, now audio slot 1.
        reply = native.update(4, text="听一下引用的语音")
        reply["message"]["reply_to_message"] = dict(original["message"])
        if not reply_has_media:
            reply["message"]["reply_to_message"].pop("voice")
        await env.gateway.receive(reply)
        await native.drain(env)
        await asyncio.gather(*tuple(reader.service._pending))
        expected = await prepare_audio(reader.contents["new-photo"], max_duration_seconds=120)
        assert transcribe.await_count == 2
        assert transcribe.await_args.args[0] == expected
        assert reader.downloads == ["old-photo", "new-photo"]
        latest = "\n".join(message.content or "" for message in env.model.requests[-1].messages)
        assert "真实原件的语音转写" in latest
        assert "无法读取" not in latest
        assert '"kind":"audio"' in latest and '"source":"reply"' in latest
    finally:
        await asr.close()
