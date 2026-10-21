from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from yuki.gateway.media import MediaResolutionError
from yuki.gateway.providers.feishu import (
    FeishuAPIError,
    FeishuClient,
    FeishuProvider,
    decode_account_id,
    encode_account_id,
)


async def test_identity_namespaces_the_app_and_reuses_the_tenant_token():
    requests = []

    def respond(request):
        requests.append(request)
        if request.url.path.endswith("tenant_access_token/internal"):
            assert json.loads(request.content) == {"app_id": "cli_a", "app_secret": "secret"}
            return httpx.Response(
                200, json={"code": 0, "tenant_access_token": "token", "expire": 7200}
            )
        assert request.headers["authorization"] == "Bearer token"
        return httpx.Response(
            200, json={"code": 0, "bot": {"open_id": "ou_bot", "app_name": "Yuki"}}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = FeishuClient("cli_a", "secret", domain="https://open.larksuite.com", client=http)
        await asyncio.gather(client.get_me(), client.get_me())
        assert (client.platform, client.self_id, client.name) == ("lark", "cli_a:ou_bot", "Yuki")
        assert (
            FeishuProvider("lark").describe_connection(client).external_account_id == "cli_a:ou_bot"
        )
        await client.close()
        assert not http.is_closed
    assert sum(request.url.path.endswith("internal") for request in requests) == 1
    assert encode_account_id("cli_b", "ou_bot") != client.self_id
    assert decode_account_id("cli_a", client.self_id) == "ou_bot"
    with pytest.raises(ValueError, match="app_mismatch"):
        decode_account_id("cli_b", client.self_id)


@pytest.mark.parametrize("failure", ["timeout", "invalid_json", "missing_code", "rejected"])
async def test_write_failure_is_not_replayed_and_errors_do_not_include_secrets(failure):
    writes = []

    def respond(request):
        if request.url.path.endswith("internal"):
            return httpx.Response(
                200, json={"code": 0, "tenant_access_token": "token-secret", "expire": 7200}
            )
        writes.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("token-secret private upstream detail", request=request)
        if failure == "invalid_json":
            return httpx.Response(502, content=b"token-secret private upstream detail")
        if failure == "missing_code":
            return httpx.Response(200, json={"msg": "token-secret"})
        return httpx.Response(400, json={"code": 99991663, "msg": "token-secret"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = FeishuClient("cli_a", "app-secret", client=http)
        with pytest.raises(FeishuAPIError) as raised:
            await client.call("POST", "/open-apis/im/v1/messages", json={"text": "one"})
        assert raised.value.uncertain is (failure != "rejected")
        assert "secret" not in str(raised.value)
    assert len(writes) == 1


async def test_websocket_credentials_do_not_need_a_tenant_token():
    def respond(request):
        assert request.url.path == "/callback/ws/endpoint"
        assert "authorization" not in request.headers
        assert json.loads(request.content) == {"AppID": "cli_a", "AppSecret": "secret"}
        return httpx.Response(
            200, json={"code": 0, "data": {"URL": "wss://example.invalid/events"}}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        result = await FeishuClient("cli_a", "secret", client=http).websocket_endpoint()
        assert result["data"]["URL"] == "wss://example.invalid/events"


async def test_rejected_token_is_replaced_only_for_the_next_independent_call():
    tokens, writes = [], []

    def respond(request):
        if request.url.path.endswith("internal"):
            token = f"token-{len(tokens)}"
            tokens.append(token)
            return httpx.Response(
                200, json={"code": 0, "tenant_access_token": token, "expire": 7200}
            )
        writes.append(request)
        if len(writes) == 1:
            return httpx.Response(400, json={"code": 99991663})
        return httpx.Response(200, json={"code": 0, "data": {"message_id": "om_second"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = FeishuClient("cli_a", "secret", client=http)
        with pytest.raises(FeishuAPIError, match="feishu_rejected"):
            await client.call("POST", "/open-apis/im/v1/messages", json={"text": "first"})
        assert len(writes) == 1
        result = await client.call(
            "POST", "/open-apis/im/v1/messages", json={"text": "independent second"}
        )
    assert result["data"]["message_id"] == "om_second"
    assert [request.headers["authorization"] for request in writes] == [
        "Bearer token-0",
        "Bearer token-1",
    ]
    assert [json.loads(request.content)["text"] for request in writes] == [
        "first",
        "independent second",
    ]


async def test_upload_sends_binary_multipart_without_a_credential_in_the_file_or_url():
    def respond(request):
        if request.url.path.endswith("internal"):
            return httpx.Response(
                200, json={"code": 0, "tenant_access_token": "token-secret", "expire": 7200}
            )
        assert request.url.path == "/open-apis/im/v1/files"
        assert request.headers["authorization"] == "Bearer token-secret"
        assert request.headers["content-type"].startswith("multipart/form-data; boundary=")
        assert b'name="file_type"\r\n\r\nstream' in request.content
        assert b'filename="original.txt"' in request.content
        assert b"original-file-bytes" in request.content
        assert b"token-secret" not in request.content
        assert "secret" not in str(request.url)
        return httpx.Response(200, json={"code": 0, "data": {"file_key": "file_uploaded"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        result = await FeishuClient("cli_a", "app-secret", client=http).call(
            "POST",
            "/open-apis/im/v1/files",
            json={"file_type": "stream", "file_name": "original.txt"},
            files={"file": ("original.txt", b"original-file-bytes", "text/plain")},
        )
    assert result["data"]["file_key"] == "file_uploaded"


async def test_original_message_and_resource_keys_are_used_for_media():
    requests = []

    def respond(request):
        requests.append(request)
        if request.url.path.endswith("internal"):
            return httpx.Response(
                200, json={"code": 0, "tenant_access_token": "token", "expire": 7200}
            )
        assert request.url.path == "/open-apis/im/v1/messages/om_original/resources/file_original"
        assert request.url.params["type"] == "file"
        assert request.headers["authorization"] == "Bearer token"
        return httpx.Response(200, content=b"original-file", headers={"content-type": "text/plain"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = FeishuClient("cli_a", "secret", client=http)
        handle = json.dumps(
            {"message_id": "om_original", "resource_key": "file_original", "type": "file"}
        )
        media = await client.resolve_media_file(handle, "file", max_download_bytes=100)
        assert media.chunks is not None
        assert b"".join([part async for part in media.chunks()]) == b"original-file"
        media = await client.resolve_media_file(handle, "file", max_download_bytes=3)
        with pytest.raises(MediaResolutionError, match="下载预算"):
            assert media.chunks is not None
            _ = [part async for part in media.chunks()]
    assert len(requests) == 3


@pytest.mark.parametrize("handle", ["file_key_without_message", '{"type":"sticker"}', "[]"])
async def test_unresolvable_media_is_not_sent_to_a_different_lookup_api(handle):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: pytest.fail("I/O"))
    ) as http:
        client = FeishuClient("cli_a", "secret", client=http)
        with pytest.raises(MediaResolutionError):
            await client.resolve_media_file(handle, "image", max_download_bytes=None)
