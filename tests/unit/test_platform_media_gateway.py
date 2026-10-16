"""Platform file IDs resolve at their own adapter; common media never calls OneBot."""

import base64
import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from qq_ai_bot.adapters.onebot.media import OneBotMediaAdapter
from qq_ai_bot.gateway.media import MediaResolutionError, PlatformMediaFile
from qq_ai_bot.gateway.providers.telegram import TelegramAPIError, TelegramClient, TelegramProvider
from qq_ai_bot.services.media_resolver import MediaResolver
from qq_ai_bot.vision.models import MediaReference


@pytest.mark.parametrize("kind", ["image", "audio"])
async def test_qq_file_lookup_and_audio_conversion_are_adapter_owned(kind):
    raw = SimpleNamespace(
        call_api=AsyncMock(return_value={"base64": base64.b64encode(b"original bytes").decode()})
    )
    gateway = OneBotMediaAdapter(raw)
    resolver = MediaResolver()
    try:
        reference = MediaReference(file="original-file")
        result = await (
            resolver.resolve_audio(reference, gateway)
            if kind == "audio"
            else resolver.resolve(reference, gateway)
        )
        assert result.content == b"original bytes"
        raw.call_api.assert_awaited_once_with(
            "get_record" if kind == "audio" else "get_image",
            **{"file": "original-file", **({"out_format": "mp3"} if kind == "audio" else {})},
        )
    finally:
        await resolver.close()


async def test_gateway_local_path_never_becomes_a_bot_filesystem_source():
    gateway = OneBotMediaAdapter(
        SimpleNamespace(call_api=AsyncMock(return_value={"file": "/private/gateway/original.mp3"}))
    )
    with pytest.raises(MediaResolutionError) as caught:
        await gateway.resolve_media_file("original-file", "audio", max_download_bytes=1024)
    assert caught.value.code == "resource_unavailable"


async def test_common_attachment_download_uses_platform_file_bytes(tmp_path):
    gateway = SimpleNamespace(
        resolve_media_file=AsyncMock(return_value=PlatformMediaFile(content=b"document"))
    )
    resolver = MediaResolver()
    try:
        destination = tmp_path / "document"
        await resolver.download_attachment(
            MediaReference(file="platform-file"),
            destination,
            max_download_bytes=1024,
            gateway=gateway,
        )
        assert destination.read_bytes() == b"document"
        gateway.resolve_media_file.assert_awaited_once_with(
            "platform-file", "file", max_download_bytes=1024
        )
    finally:
        await resolver.close()


async def test_telegram_native_identity_and_file_bytes_hide_token_urls(caplog):
    token = "fixture:private-token"
    requested = []

    def respond(request):
        requested.append(request)
        if request.url.path.endswith("/getMe"):
            return httpx.Response(
                200, json={"ok": True, "result": {"id": 8000, "username": "fixture_bot"}}
            )
        if request.url.path.endswith("/getFile"):
            assert json.loads(request.content) == {"file_id": "original-file"}
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {"file_path": "documents/original.txt", "file_size": 8},
                },
            )
        return httpx.Response(200, content=b"document", headers={"content-type": "text/plain"})

    caplog.set_level(logging.INFO, logger="httpx")
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        gateway = TelegramClient(token, client=http)
        await gateway.get_me()
        profile = TelegramProvider().describe_connection(gateway)
        assert (profile.platform, profile.external_account_id) == ("telegram", "8000")
        assert "message_history" not in profile.capabilities
        resolver = MediaResolver()
        try:
            result = await resolver.resolve(MediaReference(file="original-file"), gateway)
            assert result.content == b"document"
            assert token not in repr(result) + repr(gateway) + caplog.text
            assert "[telegram-url-redacted]" in caplog.text
            assert len(requested) == 3
        finally:
            await resolver.close()


@pytest.mark.parametrize("failure", ["rejected", "transport", "malformed"])
async def test_telegram_send_errors_are_sanitized_and_do_not_automatically_retry(failure):
    requests = []

    def respond(request):
        requests.append(request)
        if failure == "transport":
            raise httpx.ReadTimeout(f"credential URL: {request.url}")
        if failure == "malformed":
            return httpx.Response(502, text="untrusted upstream error")
        return httpx.Response(
            400,
            json={"ok": False, "error_code": 400, "description": "private-token must never appear"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        gateway = TelegramClient("fixture:private-token", client=http)
        with pytest.raises(TelegramAPIError) as caught:
            await gateway.call(
                "sendMessage", {"chat_id": "original-chat", "text": "original message"}
            )
        assert caught.value.uncertain is (failure != "rejected")
        assert "private-token" not in str(caught.value)
        assert len(requests) == 1


async def test_telegram_upload_uses_native_multipart_and_original_caption():
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(
            200, json={"ok": True, "result": {"message_id": 17, "chat": {"id": -42}}}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        gateway = TelegramClient("fixture:token", client=http)
        result = await gateway.call(
            "sendDocument",
            {
                "chat_id": "-42",
                "caption": "original caption",
                "reply_parameters": {"message_id": 3},
            },
            {"document": ("original.txt", b"original bytes", "text/plain")},
        )
    assert result["message_id"] == 17
    assert requests[0].headers["content-type"].startswith("multipart/form-data")
    assert b"original bytes" in requests[0].content and b"original caption" in requests[0].content


async def test_telegram_file_download_enforces_actual_byte_budget():
    def respond(request):
        if request.url.path.endswith("/getFile"):
            return httpx.Response(200, json={"ok": True, "result": {"file_path": "original"}})
        return httpx.Response(200, content=b"oversized file")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        gateway = TelegramClient("fixture:token", client=http)
        with pytest.raises(MediaResolutionError) as caught:
            await gateway.resolve_media_file("original", "file", max_download_bytes=3)
    assert caught.value.code == "too_large"
