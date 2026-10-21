"""Native typing uses the selected connection and target without message effects."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from yuki.gateway.providers import builtin_provider_catalog
from yuki.gateway.providers.app import AppHandle, AppProvider
from yuki.gateway.providers.snowluma import SnowLumaProvider
from yuki.gateway.providers.telegram import TelegramAPIError, TelegramClient, TelegramProvider
from yuki.gateway.registry import GatewayConnectionRegistry
from yuki.gateway.social import TransportTarget


@pytest.mark.parametrize(
    "target",
    [TransportTarget("person", "42"), TransportTarget("space", "-10042", thread_id="17")],
)
async def test_telegram_typing_uses_native_chat_and_original_topic_without_stop_request(target):
    requests = []

    def http(request):
        requests.append((request.url.path.rsplit("/", 1)[-1], json.loads(request.content)))
        return httpx.Response(200, json={"ok": True, "result": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(http)) as transport:
        client = TelegramClient("offline-token", client=transport)
        activity = TelegramProvider().activity(client, target)
        assert activity.refresh_interval_seconds == 4

        await activity.start()
        await activity.start()
        await activity.stop()

    params = {"chat_id": target.external_id, "action": "typing"}
    if target.thread_id is not None:
        params["message_thread_id"] = int(target.thread_id)
    assert requests == [("sendChatAction", params), ("sendChatAction", params)]


async def test_telegram_typing_failure_is_not_replayed_or_replaced_by_a_message():
    requests = []

    def http(request):
        requests.append(request.url.path.rsplit("/", 1)[-1])
        return httpx.Response(403, json={"ok": False, "error_code": 403})

    async with httpx.AsyncClient(transport=httpx.MockTransport(http)) as transport:
        activity = TelegramProvider().activity(
            TelegramClient("offline-token", client=transport), TransportTarget("person", "42")
        )
        with pytest.raises(TelegramAPIError, match="telegram_rejected"):
            await activity.start()
        await activity.stop()

    assert requests == ["sendChatAction"]


async def test_qq_private_typing_and_cancel_use_original_account_and_native_status():
    bot = SimpleNamespace(call_api=AsyncMock())
    activity = SnowLumaProvider().activity(bot, TransportTarget("person", "123456"))
    assert activity is not None and activity.refresh_interval_seconds == 4

    await activity.start()
    await activity.stop()

    assert bot.call_api.await_args_list == [
        (("set_input_status",), {"user_id": 123456, "event_type": 1}),
        (("set_input_status",), {"user_id": 123456, "event_type": 0}),
    ]


def test_qq_group_has_no_private_typing_operation():
    bot = SimpleNamespace(call_api=AsyncMock())
    assert SnowLumaProvider().activity(bot, TransportTarget("space", "123456")) is None
    bot.call_api.assert_not_called()


async def test_qq_typing_failure_is_not_retried_by_the_adapter():
    bot = SimpleNamespace(call_api=AsyncMock(side_effect=RuntimeError("offline")))
    activity = SnowLumaProvider().activity(bot, TransportTarget("person", "123456"))
    assert activity is not None

    with pytest.raises(RuntimeError, match="offline"):
        await activity.start()

    bot.call_api.assert_awaited_once_with("set_input_status", user_id=123456, event_type=1)


def test_app_does_not_claim_a_native_typing_capability():
    assert AppProvider().activity(AppHandle(), TransportTarget("person", "person-id")) is None


async def test_connection_profiles_describe_actual_typing_scope():
    qq = SimpleNamespace(self_id="1001")

    assert "typing_private" in SnowLumaProvider().describe_connection(qq).capabilities
    assert "typing_group" not in SnowLumaProvider().describe_connection(qq).capabilities
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"ok": True, "result": {"id": 1001}})
        )
    ) as transport:
        telegram = TelegramClient("offline-token", client=transport)
        await telegram.get_me()
        assert {"typing_private", "typing_group"} <= TelegramProvider().describe_connection(
            telegram
        ).capabilities
    assert (
        not {"typing_private", "typing_group"}
        & AppProvider().describe_connection(AppHandle()).capabilities
    )


async def test_registry_activity_keeps_original_presence_across_platforms_and_multiple_bots():
    requests_a, requests_b = [], []

    def http_a(request):
        requests_a.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": True})

    def http_b(request):
        requests_b.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": True})

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(http_a)) as transport_a,
        httpx.AsyncClient(transport=httpx.MockTransport(http_b)) as transport_b,
    ):
        telegram_a = TelegramClient("offline-a", client=transport_a)
        telegram_a.self_id = "1001"
        telegram_b = TelegramClient("offline-b", client=transport_b)
        telegram_b.self_id = "1002"
        qq = SimpleNamespace(self_id="1001", call_api=AsyncMock())
        registry = GatewayConnectionRegistry(providers=builtin_provider_catalog())
        registry.connect(telegram_a, provider_id="telegram", presence_id="tg-a")
        registry.connect(telegram_b, provider_id="telegram", presence_id="tg-b")
        registry.connect(qq, provider_id="snowluma", presence_id="qq")
        target = TransportTarget("person", "42")
        original = registry.activity(registry.resolve_active("tg-a"), target)
        qq_activity = registry.activity(registry.resolve_active("qq"), target)
        assert original is not None and qq_activity is not None

        await original.start()

        assert requests_a == [{"chat_id": "42", "action": "typing"}]
        assert requests_b == []
        qq.call_api.assert_not_awaited()
        await qq_activity.start()
        qq.call_api.assert_awaited_once_with("set_input_status", user_id=42, event_type=1)
        assert len(requests_a) == 1 and requests_b == []
