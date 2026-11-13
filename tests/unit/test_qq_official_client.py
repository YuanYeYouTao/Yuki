"""QQ Open Platform credentials, API bases and error facts.

Failure cases written before the implementation:

1. Each API call buys a new access token although the cached one is still valid.
2. A token the platform rejected with HTTP 401 stays cached, so every later call fails;
   or the rejected write is silently replayed with a fresh token.
3. A token past its ``expires_in`` keeps being used.
4. Sandbox credentials talk to the production API, or production to the sandbox.
5. The credential preflight accepts an AppID/AppSecret the token endpoint rejected,
   or the secret leaks into the raised error.
6. Platform refusal codes lose their meaning: 40034128 is not a definite reply-limit
   failure, or a transport failure on a write is reported as definitely not delivered.
"""

from __future__ import annotations

import httpx
import pytest
from tests.support.qq_official import FakeQQPlatform

from yuki.gateway.providers.qq_official import (
    QQOfficialAPIError,
    QQOfficialClient,
    QQOfficialReplyLimit,
    verify_qq_official_credentials,
)


async def test_token_is_cached_until_rejected_and_rejected_write_is_not_replayed():
    platform = FakeQQPlatform()
    async with platform.client() as http:
        client = QQOfficialClient("1020", "secret-value", client=http)
        await client.call("GET", "/gateway")
        await client.call("GET", "/gateway")
        assert platform.tokens_requested() == 1
        assert platform.requests[-1].headers["authorization"] == "QQBot token-1"

        platform.reply("POST", "/v2/users/U1/messages", 401, code=11244, message="token invalid")
        with pytest.raises(QQOfficialAPIError) as rejected:
            await client.call("POST", "/v2/users/U1/messages", {"content": "hi"})
        assert rejected.value.uncertain is False
        assert len(platform.messages()) == 1

        await client.call("GET", "/gateway")
        assert platform.tokens_requested() == 2
        assert platform.requests[-1].headers["authorization"] == "QQBot token-2"


async def test_expired_token_is_refetched():
    platform = FakeQQPlatform()
    platform.reply("POST", "/app/getAppAccessToken", access_token="short", expires_in="0")
    async with platform.client() as http:
        client = QQOfficialClient("1020", "secret-value", client=http)
        await client.call("GET", "/gateway")
        await client.call("GET", "/gateway")
    assert platform.tokens_requested() == 2
    assert platform.requests[-1].headers["authorization"] == "QQBot token-1"


@pytest.mark.parametrize(
    ("sandbox", "host"), [(False, "api.bot.qq.com"), (True, "sandbox.api.sgroup.qq.com")]
)
async def test_sandbox_only_changes_the_api_base(sandbox, host):
    platform = FakeQQPlatform()
    async with platform.client() as http:
        client = QQOfficialClient("1020", "secret-value", sandbox=sandbox, client=http)
        await client.call("GET", "/gateway")
    assert [item.host for item in platform.requests] == ["bots.qq.com", host]
    assert platform.requests[0].body == {"appId": "1020", "clientSecret": "secret-value"}


async def test_preflight_rejects_refused_credentials_without_leaking_secret():
    platform = FakeQQPlatform()
    platform.reply("POST", "/app/getAppAccessToken", code=100016, message="invalid appid")
    async with platform.client() as http:
        with pytest.raises(QQOfficialAPIError) as refused:
            await verify_qq_official_credentials("1020", "secret-value", client=http)
        assert "secret-value" not in repr(refused.value) + str(refused.value)
        assert refused.value.error_code == 100016
        await verify_qq_official_credentials("1020", "secret-value", sandbox=True, client=http)
    assert platform.requests[-1].host == "sandbox.api.sgroup.qq.com"
    assert platform.requests[-1].path == "/gateway"


async def test_refusal_codes_and_unknown_transport_are_distinct_facts():
    platform = FakeQQPlatform()
    platform.reply(
        "POST", "/v2/groups/G1/messages", 400, code=40034128, message="reply limit exceeded"
    )
    async with platform.client() as http:
        client = QQOfficialClient("1020", "secret-value", client=http)
        with pytest.raises(QQOfficialReplyLimit) as limited:
            await client.call("POST", "/v2/groups/G1/messages", {"content": "hi"})
        assert limited.value.uncertain is False and limited.value.error_code == 40034128

    def broken(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/app/getAppAccessToken":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 7200})
        raise httpx.ReadTimeout("lost", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(broken)) as http:
        client = QQOfficialClient("1020", "secret-value", client=http)
        with pytest.raises(QQOfficialAPIError) as unknown:
            await client.call("POST", "/v2/groups/G1/messages", {"content": "hi"})
        assert unknown.value.uncertain is True
