"""Small pinned-execd transport; only the trusted Manager uses its access token."""

from __future__ import annotations

import asyncio
import json
import urllib.request
from typing import Any


class Execd:
    def __init__(self, address: str, token: str) -> None:
        self.address, self.token = address, token

    async def request(self, method: str, path: str, data: Any = None) -> dict[str, Any]:
        def send() -> dict[str, Any]:
            request = urllib.request.Request(
                f"http://{self.address}:44772{path}",
                data=json.dumps(data).encode() if data is not None else None,
                method=method,
                headers={"X-EXECD-ACCESS-TOKEN": self.token, "Content-Type": "application/json"},
            )
            # Never route the host control token through an environment proxy.
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(request, timeout=3) as response:
                raw = response.read(65537)
            if len(raw) > 65536:
                raise ValueError("execd_response_too_large")
            if path == "/ping" or not raw:
                return {}
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise ValueError("execd_invalid_response")
            return result

        return await asyncio.to_thread(send)

    async def connect(self, session: str, *, tty: bool) -> Any:
        from websockets.asyncio.client import connect

        return await connect(
            f"ws://{self.address}:44772/pty/{session}/ws?pty={int(tty)}&takeover=1",
            additional_headers={"X-EXECD-ACCESS-TOKEN": self.token},
            proxy=None,
            open_timeout=3,
            max_size=2 * 1024 * 1024,
            max_queue=4,
        )
