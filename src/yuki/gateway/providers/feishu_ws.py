"""Async Feishu event connection; admission ACKs never wait for Main to finish."""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
import urllib.request
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urlsplit

from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus
from websockets.proxy import get_proxy
from websockets.uri import parse_uri

from yuki.gateway.providers.feishu import FeishuAPIError, FeishuClient
from yuki.gateway.providers.feishu_protocol import Frame

logger = logging.getLogger(__name__)
# websockets DEBUG writes signed URLs and full event frames. Restrict only the
# connection's protocol logger; category-only application diagnostics remain.
_protocol_logger = logging.getLogger(__name__ + ".protocol")
_protocol_logger.setLevel(logging.WARNING)


class FeishuWebSocket:
    def __init__(
        self,
        client: FeishuClient,
        handler: Callable[[dict[str, Any]], Awaitable[None]],
        *,
        on_connected: Callable[[], None] = lambda: None,
        on_disconnected: Callable[[], None] = lambda: None,
        connector: Callable[..., Any] = connect,
    ) -> None:
        self.client, self.handler = client, handler
        self.on_connected, self.on_disconnected = on_connected, on_disconnected
        self.connector = connector
        self._connection: Any = None
        self._task: asyncio.Task[None] | None = None
        self._service_id = 0
        self._fragments: dict[str, tuple[float, list[bytes | None]]] = {}
        # These are protocol defaults, replaced by the endpoint/pong configuration.
        self._config = {
            "ReconnectNonce": 30,
            "ReconnectCount": -1,
            "ReconnectInterval": 120,
            "PingInterval": 120,
        }
        self.last_success_at: datetime | None = None
        self.last_error_category: str | None = None

    @property
    def connected(self) -> bool:
        return self._connection is not None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def _configure(self, config: dict[str, Any]) -> None:
        self._config.update({key: config[key] for key in self._config if key in config})

    async def start(self) -> None:
        await self._connect()
        self._task = asyncio.create_task(self._run(), name=f"{self.client.platform}-websocket")

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        await self._disconnect()

    async def _connect(self) -> None:
        try:
            endpoint = await self.client.websocket_endpoint()
        except FeishuAPIError as exc:
            # The public endpoint protocol distinguishes temporary server errors
            # from application/authentication rejection, just as the SDK does.
            if exc.error_code is not None and exc.error_code not in {1, 1000040343}:
                raise PermissionError("feishu_endpoint_rejected") from None
            raise
        data = endpoint["data"]
        self._configure(data.get("ClientConfig") or {})
        query = parse_qs(urlsplit(data["URL"]).query)
        service = int(query["service_id"][0])
        uri = parse_uri(data["URL"])
        proxy = get_proxy(uri)
        if proxy is None and not urllib.request.proxy_bypass(f"{uri.host}:{uri.port}"):
            # websockets doesn't consume ALL_PROXY, which the shared HTTP setup
            # already uses. Keep its more specific and system proxy selections.
            proxy = urllib.request.getproxies().get("all")
        try:
            connection = await self.connector(
                data["URL"],
                ping_interval=None,
                max_size=None,
                logger=_protocol_logger,
                proxy=proxy,
            )
        except InvalidStatus as exc:
            headers = exc.response.headers
            code = headers.get("handshake-status")
            if code == "403" or (
                code == "514" and headers.get("handshake-autherrcode") == "1000040350"
            ):
                raise PermissionError("feishu_websocket_rejected") from None
            raise ConnectionError("feishu_websocket_handshake_failed") from None
        self._connection, self._service_id = connection, service
        try:
            self.on_connected()
        except BaseException:
            await self._disconnect()
            raise
        self.last_error_category = None
        self.last_success_at = datetime.now(UTC)

    async def _disconnect(self) -> None:
        connection, self._connection = self._connection, None
        self._fragments.clear()
        if connection is not None:
            self.on_disconnected()
            await connection.close()

    async def _run(self) -> None:
        try:
            while True:
                try:
                    await self._serve()
                    self.last_error_category = "connection_closed"
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.last_error_category = type(exc).__name__
                    logger.warning("feishu_connection_lost category=%s", type(exc).__name__)
                finally:
                    await self._disconnect()
                await self._reconnect()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.last_error_category = type(exc).__name__
            logger.error("feishu_connection_stopped category=%s", type(exc).__name__)

    async def _reconnect(self) -> None:
        await asyncio.sleep(random.random() * self._config["ReconnectNonce"])
        attempts = 0
        while self._config["ReconnectCount"] < 0 or attempts < self._config["ReconnectCount"]:
            try:
                await self._connect()
                return
            except PermissionError:
                raise
            except Exception as exc:
                self.last_error_category = type(exc).__name__
                logger.warning("feishu_reconnect_failed category=%s", type(exc).__name__)
            attempts += 1
            await asyncio.sleep(self._config["ReconnectInterval"])
        raise ConnectionError("feishu_reconnect_exhausted")

    async def _serve(self) -> None:
        ping = asyncio.create_task(self._ping(), name=f"{self.client.platform}-heartbeat")
        receive = asyncio.create_task(self._receive(), name=f"{self.client.platform}-frames")
        try:
            done, _pending = await asyncio.wait(
                (ping, receive), return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                await task
        finally:
            ping.cancel()
            receive.cancel()
            await asyncio.gather(ping, receive, return_exceptions=True)

    async def _receive(self) -> None:
        async for message in self._connection:
            await self.handle_frame(message)

    async def _ping(self) -> None:
        while True:
            frame = Frame(SeqID=0, LogID=0, service=self._service_id, method=0)
            frame.headers.add(key="type", value="ping")
            await self._connection.send(frame.SerializeToString())
            await asyncio.sleep(self._config["PingInterval"])

    async def handle_frame(self, message: bytes) -> None:
        frame = Frame.FromString(message)
        headers = {item.key: item.value for item in frame.headers}
        if frame.method == 0:
            if headers.get("type") == "pong":
                if frame.payload:
                    self._configure(json.loads(frame.payload))
                self.last_success_at = datetime.now(UTC)
            return
        if frame.method != 1 or headers.get("type") not in {"event", "card"}:
            return
        payload = frame.payload
        now = time.monotonic()
        self._fragments = {key: value for key, value in self._fragments.items() if value[0] > now}
        count, index = int(headers.get("sum", "1")), int(headers.get("seq", "0"))
        if count > 1:
            key = headers["message_id"]
            _expires, parts = self._fragments.setdefault(key, (now + 5, [None] * count))
            if len(parts) != count or not 0 <= index < count:
                raise ValueError("feishu_fragment_conflict")
            parts[index] = payload
            if any(part is None for part in parts):
                return
            payload = b"".join(part for part in parts if part is not None)
            del self._fragments[key]
        started = time.monotonic()
        code = 200
        try:
            # Card interaction is acknowledged but doesn't manufacture a chat message.
            if headers["type"] == "event":
                await self.handler(json.loads(payload))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            code = 500
            logger.error("feishu_event_admission_failed category=%s", type(exc).__name__)
        frame.headers.add(key="biz_rt", value=str(int((time.monotonic() - started) * 1000)))
        frame.payload = json.dumps({"code": code}, separators=(",", ":")).encode()
        await self._connection.send(frame.SerializeToString())
        if code == 200:
            self.last_success_at = datetime.now(UTC)
