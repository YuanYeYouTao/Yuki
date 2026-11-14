"""Offline QQ Open Platform HTTP API: records every request, replies from a script."""

from __future__ import annotations

import json
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any

import httpx


@dataclass
class Request:
    method: str
    host: str
    path: str
    headers: dict[str, str]
    body: Any


@dataclass
class FakeQQPlatform:
    """Default replies are successes; ``script`` queues explicit responses per route."""

    gateway_url: str = "ws://127.0.0.1:1/websocket"
    requests: list[Request] = field(default_factory=list)
    script: dict[tuple[str, str], deque[httpx.Response]] = field(
        default_factory=lambda: defaultdict(deque)
    )
    tokens: int = 0
    sent: int = 0

    def reply(self, method: str, path: str, status: int = 200, **payload: Any) -> None:
        self.script[(method, path)].append(httpx.Response(status, json=payload))

    def handle(self, request: httpx.Request) -> httpx.Response:
        content = request.content
        body: Any = None
        if content:
            json_body = request.headers.get("content-type", "").startswith("application/json")
            body = json.loads(content) if json_body else content
        path = request.url.path
        self.requests.append(
            Request(request.method, request.url.host, path, dict(request.headers), body)
        )
        queued = self.script.get((request.method, path))
        if queued:
            return queued.popleft()
        if path == "/app/getAppAccessToken":
            self.tokens += 1
            return httpx.Response(
                200, json={"access_token": f"token-{self.tokens}", "expires_in": "7200"}
            )
        if path == "/gateway":
            return httpx.Response(200, json={"url": self.gateway_url})
        if request.method == "POST" and path.endswith("/messages"):
            self.sent += 1
            return httpx.Response(
                200,
                json={
                    "id": f"OUT-{self.sent}",
                    "timestamp": "2026-11-13T10:00:00+08:00",
                    "ext_info": {"ref_idx": f"REFIDX_OUT-{self.sent}"},
                },
            )
        if path.endswith("/upload_prepare"):
            return httpx.Response(
                200,
                json={
                    "upload_id": "upload-1",
                    "block_size": "4",
                    "parts": [
                        {"index": 2, "presigned_url": "https://cos.example/part-2"},
                        {"index": 1, "presigned_url": "https://cos.example/part-1"},
                    ],
                },
            )
        if path.endswith("/upload_part_finish"):
            return httpx.Response(200, json={})
        if path.endswith("/files"):
            return httpx.Response(200, json={"file_info": "FILE-INFO-1"})
        if request.url.host == "cos.example":
            return httpx.Response(200)
        if request.method == "DELETE":
            return httpx.Response(200)
        raise AssertionError((request.method, str(request.url)))

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handle))

    def messages(self, prefix: str = "") -> list[Request]:
        return [
            item
            for item in self.requests
            if item.method == "POST"
            and item.path.endswith("/messages")
            and item.path.startswith(prefix)
        ]

    def tokens_requested(self) -> int:
        return sum(item.path == "/app/getAppAccessToken" for item in self.requests)
