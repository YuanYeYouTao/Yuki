"""Wire-compatible async ACK, fragmentation, heartbeat and reconnection contracts."""

import asyncio
import json
import logging
import urllib.request
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from websockets.asyncio.server import serve

from yuki.gateway.providers.feishu import FeishuAPIError
from yuki.gateway.providers.feishu_protocol import Frame
from yuki.gateway.providers.feishu_ws import FeishuWebSocket


def frame(payload=b"{}", **headers):
    result = Frame(SeqID=7, LogID=8, service=9, method=1, payload=payload)
    for key, value in {
        "type": "event",
        "message_id": "message",
        "sum": "1",
        "seq": "0",
        **headers,
    }.items():
        result.headers.add(key=key, value=value)
    return result


def socket(handler=None):
    client = SimpleNamespace(platform="feishu")
    result = FeishuWebSocket(client, handler or AsyncMock())
    result._connection = SimpleNamespace(send=AsyncMock(), close=AsyncMock())
    return result


def test_public_frame_schema_matches_official_wire_fields():
    original = bytes.fromhex("08071008180920012a0d0a047479706512056576656e7442027b7d")
    parsed = Frame.FromString(original)
    assert parsed.SeqID == 7 and parsed.LogID == 8 and parsed.service == 9 and parsed.method == 1
    assert parsed.headers[0].key == "type" and parsed.headers[0].value == "event"
    assert parsed.payload == b"{}" and parsed.SerializeToString() == original


async def test_fragment_ack_waits_for_admission_and_preserves_original_frame_identity():
    entered, admitted = asyncio.Event(), asyncio.Event()

    async def handler(payload):
        assert payload == {"event": {"message": "received"}}
        entered.set()
        await admitted.wait()

    ws = socket(handler)
    payload = json.dumps({"event": {"message": "received"}}).encode()
    await ws.handle_frame(frame(payload[11:], sum="2", seq="1").SerializeToString())
    ws._connection.send.assert_not_awaited()
    task = asyncio.create_task(
        ws.handle_frame(frame(payload[:11], sum="2", seq="0").SerializeToString())
    )
    await asyncio.wait_for(entered.wait(), 1)
    ws._connection.send.assert_not_awaited()
    admitted.set()
    await task
    ack = Frame.FromString(ws._connection.send.call_args.args[0])
    assert ack.SeqID == 7 and ack.LogID == 8 and ack.service == 9
    assert json.loads(ack.payload) == {"code": 200}
    assert any(item.key == "biz_rt" for item in ack.headers)
    assert not ws._fragments


async def test_failed_admission_is_500_without_logging_payload_or_secrets(caplog):
    handler = AsyncMock(side_effect=RuntimeError("sensitive-secret-url"))
    ws = socket(handler)
    await ws.handle_frame(frame(b'{"sensitive_chat":"private"}').SerializeToString())
    ack = Frame.FromString(ws._connection.send.call_args.args[0])
    assert json.loads(ack.payload) == {"code": 500}
    assert "RuntimeError" in caplog.text
    assert "sensitive-secret-url" not in caplog.text and "sensitive_chat" not in caplog.text


async def test_pong_config_replaces_server_intervals_and_control_does_not_enter_chat():
    ws = socket()
    pong = Frame(SeqID=0, LogID=0, service=9, method=0)
    pong.headers.add(key="type", value="pong")
    pong.payload = (
        b'{"PingInterval":17,"ReconnectCount":4,"ReconnectInterval":3,"ReconnectNonce":2}'
    )
    await ws.handle_frame(pong.SerializeToString())
    assert ws.last_success_at is not None
    ws.handler.assert_not_awaited()
    ws._connection.send.assert_not_awaited()


async def test_card_interaction_is_acknowledged_without_becoming_human_speech():
    ws = socket()
    await ws.handle_frame(frame(type="card").SerializeToString())
    ws.handler.assert_not_awaited()
    assert json.loads(Frame.FromString(ws._connection.send.call_args.args[0]).payload) == {
        "code": 200
    }


async def test_cancellation_never_acknowledges_an_uncommitted_event():
    ws = socket(AsyncMock(side_effect=asyncio.CancelledError))
    with pytest.raises(asyncio.CancelledError):
        await ws.handle_frame(frame().SerializeToString())
    ws._connection.send.assert_not_awaited()


class Connection:
    def __init__(self):
        self.incoming = asyncio.Queue()
        self.sent = []
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        message = await self.incoming.get()
        if message is None:
            raise StopAsyncIteration
        return message

    async def send(self, value):
        self.sent.append(Frame.FromString(value))

    async def close(self):
        self.closed = True


async def test_reconnect_uses_new_endpoint_and_connection_callbacks_without_replaying_events():
    first, second = Connection(), Connection()
    connected = asyncio.Event()
    transitions, urls = [], []
    handler = AsyncMock()

    async def connector(url, **kwargs):
        urls.append((url, kwargs))
        return first if len(urls) == 1 else second

    def on_connected():
        transitions.append("connected")
        if len(urls) == 2:
            connected.set()

    client = SimpleNamespace(
        platform="feishu",
        websocket_endpoint=AsyncMock(
            return_value={
                "code": 0,
                "data": {
                    "URL": "wss://example.invalid/event?service_id=9&device_id=private",
                    "ClientConfig": {
                        "ReconnectNonce": 0,
                        "ReconnectInterval": 0,
                        "ReconnectCount": 3,
                        "PingInterval": 120,
                    },
                },
            }
        ),
    )
    ws = FeishuWebSocket(
        client,
        handler,
        connector=connector,
        on_connected=on_connected,
        on_disconnected=lambda: transitions.append("disconnected"),
    )
    await ws.start()
    await first.incoming.put(None)
    await asyncio.wait_for(connected.wait(), 1)
    assert transitions == ["connected", "disconnected", "connected"]
    assert client.websocket_endpoint.await_count == 2
    assert ws.connected and ws.running
    assert urls[0][1]["ping_interval"] is None and urls[0][1]["max_size"] is None
    assert not urls[0][1]["logger"].isEnabledFor(logging.DEBUG)
    handler.assert_not_awaited()
    await ws.close()
    assert first.closed and second.closed and not ws.connected and not ws.running
    assert transitions[-1] == "disconnected"


async def test_actual_async_websocket_accepts_event_ack_without_debug_secret_leak(
    caplog, monkeypatch
):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    caplog.set_level(logging.DEBUG)
    acked = asyncio.Event()
    handler = AsyncMock()
    received = []

    async def server(connection):
        await connection.send(frame(b'{"private_chat":"fixture-private-chat"}').SerializeToString())
        async for raw in connection:
            message = Frame.FromString(raw)
            received.append(message)
            if message.method == 1:
                acked.set()

    server_logger = logging.Logger("test.feishu.server", level=logging.WARNING)
    async with serve(server, "127.0.0.1", 0, logger=server_logger) as listener:
        port = listener.sockets[0].getsockname()[1]
        client = SimpleNamespace(
            platform="feishu",
            websocket_endpoint=AsyncMock(
                return_value={
                    "code": 0,
                    "data": {"URL": f"ws://127.0.0.1:{port}/?service_id=9&ticket=fixture-secret"},
                }
            ),
        )
        ws = FeishuWebSocket(client, handler)
        await ws.start()
        try:
            await asyncio.wait_for(acked.wait(), 2)
            handler.assert_awaited_once_with({"private_chat": "fixture-private-chat"})
            ack = next(item for item in received if item.method == 1)
            assert json.loads(ack.payload) == {"code": 200}
            assert any(item.method == 0 for item in received)
        finally:
            await ws.close()
    assert "fixture-secret" not in caplog.text and "fixture-private-chat" not in caplog.text


async def test_endpoint_auth_rejection_stops_reconnection_without_replaying_or_logging_secret():
    client = SimpleNamespace(
        platform="feishu",
        websocket_endpoint=AsyncMock(
            side_effect=FeishuAPIError(
                "feishu_rejected",
                error_code=1000040344,
            )
        ),
    )
    connector = AsyncMock()
    ws = FeishuWebSocket(client, AsyncMock(), connector=connector)
    ws._config["ReconnectNonce"] = 0
    with pytest.raises(PermissionError, match="feishu_endpoint_rejected"):
        await ws._reconnect()
    client.websocket_endpoint.assert_awaited_once()
    connector.assert_not_awaited()


@pytest.mark.parametrize(
    ("proxies", "expected"),
    [
        ({"all": "http://all-proxy:7890"}, "http://all-proxy:7890"),
        ({"all": "socks5h://all-proxy:1080"}, "socks5h://all-proxy:1080"),
        (
            {"https": "http://https-proxy:7891", "all": "http://all-proxy:7890"},
            "http://https-proxy:7891",
        ),
        (
            {
                "wss": "http://wss-proxy:7892",
                "https": "http://https-proxy:7891",
                "all": "http://all-proxy:7890",
            },
            "http://wss-proxy:7892",
        ),
        (
            {"socks": "http://system-proxy:1080", "all": "http://all-proxy:7890"},
            "socks5h://system-proxy:1080",
        ),
        ({}, None),
    ],
)
async def test_native_specific_and_system_proxy_precede_all_proxy(monkeypatch, proxies, expected):
    monkeypatch.setattr(urllib.request, "getproxies", lambda: proxies)
    monkeypatch.setattr(urllib.request, "proxy_bypass", lambda _host: False)
    connector = AsyncMock(return_value=Connection())
    client = SimpleNamespace(
        platform="feishu",
        websocket_endpoint=AsyncMock(
            return_value={"data": {"URL": "wss://example.invalid/event?service_id=9"}}
        ),
    )
    ws = FeishuWebSocket(client, AsyncMock(), connector=connector)
    await ws._connect()
    assert connector.call_args.kwargs["proxy"] == expected
    await ws.close()


async def test_no_proxy_bypasses_all_and_more_specific_proxy(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "example.invalid")
    monkeypatch.setenv("no_proxy", "example.invalid")
    monkeypatch.setattr(
        urllib.request,
        "getproxies",
        lambda: {"wss": "http://wss-proxy:7892", "all": "http://all-proxy:7890"},
    )
    connector = AsyncMock(return_value=Connection())
    client = SimpleNamespace(
        platform="feishu",
        websocket_endpoint=AsyncMock(
            return_value={"data": {"URL": "wss://example.invalid/event?service_id=9"}}
        ),
    )
    ws = FeishuWebSocket(client, AsyncMock(), connector=connector)
    await ws._connect()
    assert connector.call_args.kwargs["proxy"] is None
    await ws.close()


async def test_proxy_connection_failure_never_attempts_direct_connection(monkeypatch):
    monkeypatch.setattr(urllib.request, "getproxies", lambda: {"all": "http://all-proxy:7890"})
    monkeypatch.setattr(urllib.request, "proxy_bypass", lambda _host: False)
    connector = AsyncMock(side_effect=OSError("proxy unavailable"))
    client = SimpleNamespace(
        platform="feishu",
        websocket_endpoint=AsyncMock(
            return_value={"data": {"URL": "wss://example.invalid/event?service_id=9"}}
        ),
    )
    ws = FeishuWebSocket(client, AsyncMock(), connector=connector)
    with pytest.raises(OSError, match="proxy unavailable"):
        await ws._connect()
    connector.assert_awaited_once()
    assert connector.call_args.kwargs["proxy"] == "http://all-proxy:7890"
    assert not ws.connected
