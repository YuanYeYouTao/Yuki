"""Explicit ASR wire protocols retain audio, transcript and failure facts."""

import base64
import json
from email import policy
from email.parser import BytesParser

import httpx
import pytest
from tests.conftest import make_settings

from yuki.admin.config_service import RuntimeConfigService
from yuki.asr.provider import ASRError, HTTPASRProvider
from yuki.config import Settings


async def test_default_qwen_chat_audio_contract_is_unchanged():
    audio = b"original-mp3"
    calls = []

    def respond(request):
        calls.append(request)
        body = json.loads(request.content)
        assert request.url == "https://asr.test/v1/chat/completions"
        assert body["model"] == "qwen3-asr-flash"
        assert [message["role"] for message in body["messages"]] == ["user"]
        block = body["messages"][0]["content"][0]
        assert block["type"] == "input_audio"
        assert block["input_audio"]["data"] == (
            "data:audio/mpeg;base64," + base64.b64encode(audio).decode()
        )
        assert body["asr_options"] == {"enable_itn": False} and body["stream"] is False
        return httpx.Response(
            200, json={"choices": [{"finish_reason": "stop", "message": {"content": "原话"}}]}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = HTTPASRProvider(base_url="https://asr.test/v1", api_key="fixture", client=client)
        assert await provider.transcribe(audio) == "原话"
    assert len(calls) == 1


async def test_explicit_gemini_transcription_sends_original_multipart_audio():
    audio = b"original-mp3"
    calls = []

    def respond(request):
        calls.append(request)
        assert request.url == "https://agm.test/v1/audio/transcriptions"
        assert request.headers["authorization"] == "Bearer fixture"
        mime = b"Content-Type: " + request.headers["content-type"].encode() + b"\r\n\r\n"
        message = BytesParser(policy=policy.default).parsebytes(mime + request.content)
        parts = {
            part.get_param("name", header="content-disposition"): part
            for part in message.iter_parts()
        }
        assert parts["file"].get_filename() == "audio.mp3"
        assert parts["file"].get_content_type() == "audio/mpeg"
        assert parts["file"].get_payload(decode=True) == audio
        assert parts["model"].get_content().strip() == "gemini-3.8-flash"
        prompt = parts["prompt"].get_payload(decode=True).decode("utf-8")
        assert "逐字转写" in prompt and "原语言" in prompt
        return httpx.Response(200, json={"text": "  明天再说。\nSee you.  "})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = HTTPASRProvider(
            base_url="https://agm.test/v1/",
            api_key="fixture",
            model="gemini-3.8-flash",
            protocol="audio_transcriptions",
            client=client,
        )
        assert await provider.transcribe(audio) == "明天再说。\nSee you."
    assert len(calls) == 1


@pytest.mark.parametrize("protocol", ["chat_completions", "audio_transcriptions"])
@pytest.mark.parametrize(
    ("status", "error"),
    [
        (401, "not_configured"),
        (403, "not_configured"),
        (429, "rate_limited"),
        (500, "provider_failed"),
    ],
)
async def test_protocol_failures_keep_original_category_without_retry(protocol, status, error):
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(status, json={"error": "actual provider failure"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = HTTPASRProvider(
            base_url="https://asr.test/v1", api_key="fixture", protocol=protocol, client=client
        )
        with pytest.raises(ASRError, match=error):
            await provider.transcribe(b"mp3")
    assert len(calls) == 1


@pytest.mark.parametrize("protocol", ["chat_completions", "audio_transcriptions"])
@pytest.mark.parametrize(
    ("transcript", "error"), [(" \n\t", "no_speech"), (None, "invalid_response")]
)
async def test_invalid_transcript_retains_error_contract(protocol, transcript, error):
    payload = (
        {"text": transcript}
        if protocol == "audio_transcriptions"
        else {"choices": [{"finish_reason": "stop", "message": {"content": transcript}}]}
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=payload))
    ) as client:
        provider = HTTPASRProvider(
            base_url="https://asr.test/v1", api_key="fixture", protocol=protocol, client=client
        )
        with pytest.raises(ASRError, match=error):
            await provider.transcribe(b"mp3")


@pytest.mark.parametrize("with_protocol", [False, True])
async def test_all_asr_restart_settings_activate_after_new_startup(database, with_protocol):
    settings = make_settings(database.url)
    current = RuntimeConfigService(settings=settings, database=database)
    await current.initialize()
    desired = {
        "enabled": False,
        "base_url": "https://agm.test/v1",
        "model": "gemini-3.8-flash",
        "timeout_seconds": 45,
        "max_download_bytes": 1048576,
        "max_duration_seconds": 120,
        "global_concurrency": 3,
        "queue_max_pending": 10,
    }
    if with_protocol:
        desired["protocol"] = "audio_transcriptions"
    for key, value in desired.items():
        changed = await current.set_override(
            "asr." + key,
            value,
            scope_type="global",
            scope_id="",
            actor_user_id="operator",
            trigger_message_id="asr-config-test",
        )
        assert changed.success
    restarted = RuntimeConfigService(settings=settings, database=database)
    await restarted.initialize()
    activated = Settings(
        _env_file=None, **{**settings.model_dump(), **await restarted.startup_settings_updates()}
    )
    for key, value in desired.items():
        assert getattr(activated.asr, "asr_" + key) == value
