"""ASR retains successful voice data under the configured input and time budgets."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from tests.conftest import make_settings
from tests.support.commands_and_chat_helpers import inbound

from qq_ai_bot.asr.provider import QwenASRProvider
from qq_ai_bot.asr.service import ASRService
from qq_ai_bot.domain.audio import parse_transcripts
from qq_ai_bot.domain.messages import AttachmentKind, MessageAttachment


@pytest.mark.parametrize("oversized", [False, True])
async def test_all_voice_parts_and_long_transcripts_preserve_source(
    database, monkeypatch, oversized
):
    settings = make_settings(database.url, asr_enabled=True)
    recognized = "spoken text " * 1500
    requests = []

    def response(request):
        requests.append(request)
        return httpx.Response(
            200, json={"choices": [{"finish_reason": "stop", "message": {"content": recognized}}]}
        )

    resolver = SimpleNamespace(
        resolve_audio=AsyncMock(return_value=SimpleNamespace(content=b"mp3")), close=AsyncMock()
    )
    prepare = AsyncMock(return_value=b"normalized-mp3")
    monkeypatch.setattr("qq_ai_bot.asr.service.prepare_audio", prepare)
    message = replace(
        inbound("", message_id="audio-many", group_id="3001"),
        attachments=tuple(
            MessageAttachment(
                AttachmentKind.AUDIO,
                "audio",
                segment_index=i,
                file=f"part-{i}",
                file_size=settings.asr_max_download_bytes + 1 if oversized and i == 0 else 10,
            )
            for i in range(4)
        ),
        reply_attachments=(
            MessageAttachment(
                AttachmentKind.AUDIO, "quoted", segment_index=8, source="reply", file="quoted"
            ),
        ),
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
        provider = QwenASRProvider(base_url="https://asr.test/v1", api_key="fixture", client=client)
        service = ASRService(settings=settings.asr, provider=provider, resolver=resolver)
        result = await service.prepare(message, None)
    transcripts = parse_transcripts(result.transcript)
    expected = [("current", i) for i in range(1 if oversized else 0, 4)] + [("reply", 8)]
    assert [(item.source, item.segment_index) for item in transcripts] == expected
    assert all(item.text == recognized.strip() for item in transcripts)
    assert len(requests) == len(transcripts)
    assert result.error == ("audio_limit" if oversized else "")
    assert "回复消息中的语音" in result.context
    assert result.context.count(recognized.strip()) == len(transcripts)


async def test_blank_transcript_still_reports_no_speech(database, monkeypatch):
    monkeypatch.setattr("qq_ai_bot.asr.service.prepare_audio", AsyncMock(return_value=b"mp3"))
    service = ASRService(
        settings=make_settings(database.url, asr_enabled=True).asr,
        provider=SimpleNamespace(transcribe=AsyncMock(return_value=" \n\t ")),
        resolver=SimpleNamespace(
            resolve_audio=AsyncMock(return_value=SimpleNamespace(content=b"mp3"))
        ),
    )
    message = replace(
        inbound("", message_id="blank-voice"),
        attachments=(MessageAttachment(AttachmentKind.AUDIO, "audio", file="blank"),),
    )
    result = await service.prepare(message, None)
    assert result.error == "no_speech" and not result.transcript
