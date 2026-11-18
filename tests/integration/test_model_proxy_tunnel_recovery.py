"""Model traffic recovers after failed HTTP proxy tunnels without a process restart.

Failure cases specified before implementation:
- CONNECT 200 followed by a TLS handshake that never completes must not keep a pool
  slot; more failures than the pool limit must not turn later requests into PoolTimeout.
- CONNECT 200 followed by the proxy closing the tunnel must not keep a pool slot.
- Cancelling the caller during the tunnel TLS handshake must not keep a pool slot.
- Every failed tunnel must close its proxy socket (the proxy observes EOF).
- After the proxy recovers, the same production client pool reaches the model.
- A non-2xx CONNECT reply keeps failing as a proxy error without changing recovery.
- An established tunnel keeps HTTP keep-alive reuse for consecutive requests.

All traffic uses real loopback sockets: a local CONNECT proxy and a local TLS model
server trusted through SSL_CERT_FILE. The evidence fixture records each attempt.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import ssl

import pytest
import trustme

from yuki.domain.messages import ChatMessage, ChatRequest
from yuki.http_environment import load_http_environment
from yuki.llm.base import LLMError
from yuki.model_runtime.models import ModelProfile, ModelProtocol
from yuki.model_runtime.pool import ModelClientPool

POOL_LIMIT = 20  # ModelClientPool's per-connection limit.

_PROXY_ENV = (
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
)


class _ModelServer:
    """TLS chat-completions endpoint for host model.test with keep-alive."""

    def __init__(self, context: ssl.SSLContext) -> None:
        self.context = context
        self.connections = 0
        self.requests = 0

    async def handle(self, reader, writer):
        self.connections += 1
        try:
            while True:
                head = await reader.readuntil(b"\r\n\r\n")
                length = next(
                    int(line.split(b":", 1)[1])
                    for line in head.split(b"\r\n")
                    if line.lower().startswith(b"content-length:")
                )
                await reader.readexactly(length)
                self.requests += 1
                body = json.dumps(
                    {
                        "id": f"chat-{self.requests}",
                        "choices": [
                            {
                                "index": 0,
                                "finish_reason": "stop",
                                "message": {"role": "assistant", "content": "recovered"},
                            }
                        ],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                    }
                ).encode()
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                    + f"Content-Length: {len(body)}\r\n\r\n".encode()
                    + body
                )
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError, ssl.SSLError):
            pass
        finally:
            writer.close()


class _Proxy:
    """HTTP CONNECT proxy whose upstream behavior the test switches."""

    def __init__(self, model: _ModelServer) -> None:
        self.model = model
        self.mode = "stall"
        self.model_port = 0
        self.connects: list[str] = []
        self.peer_closed = 0
        self.closed = asyncio.Condition()
        self.established = asyncio.Event()

    async def handle(self, reader, writer):
        mode = self.mode
        try:
            await reader.readuntil(b"\r\n\r\n")
            self.connects.append(mode)
            if mode == "refuse":
                writer.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
                await writer.drain()
                return
            writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            await writer.drain()
            self.established.set()
            if mode == "drop":
                return
            if mode == "stall":
                # Upstream is blackholed: never relay the ClientHello.
                while await reader.read(65536):
                    pass
                async with self.closed:
                    self.peer_closed += 1
                    self.closed.notify_all()
                return
            upstream_reader, upstream_writer = await asyncio.open_connection(
                "127.0.0.1", self.model_port
            )

            async def pipe(source, target):
                with contextlib.suppress(ConnectionError):
                    while data := await source.read(65536):
                        target.write(data)
                        await target.drain()
                target.close()

            await asyncio.gather(pipe(reader, upstream_writer), pipe(upstream_reader, writer))
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()


@pytest.fixture
async def proxied_model(monkeypatch, tmp_path):
    ca = trustme.CA()
    ca_path = tmp_path / "ca.pem"
    ca.cert_pem.write_to_path(str(ca_path))
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ca.issue_cert("model.test").configure_cert(context)
    model = _ModelServer(context)
    proxy = _Proxy(model)
    model_server = await asyncio.start_server(model.handle, "127.0.0.1", 0, ssl=context)
    proxy_server = await asyncio.start_server(proxy.handle, "127.0.0.1", 0)
    proxy.model_port = model_server.sockets[0].getsockname()[1]
    for name in _PROXY_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(
        "HTTPS_PROXY", f"http://127.0.0.1:{proxy_server.sockets[0].getsockname()[1]}"
    )
    monkeypatch.setenv("SSL_CERT_FILE", str(ca_path))
    # The production startup entry, with no dotenv overrides.
    load_http_environment(tmp_path / "missing.env")
    pool = ModelClientPool()
    profile = ModelProfile(
        id="proxied",
        provider="openai",
        protocol=ModelProtocol.CHAT_COMPLETIONS,
        model="proxied-model",
        base_url="https://model.test/v1",
        api_key_env="",
        timeout_seconds=0.3,
        max_retries=0,
        default_temperature=0.7,
        default_max_output_tokens=64,
    )
    try:
        yield proxy, model, pool.get(profile)
    finally:
        await pool.close()
        for server in (proxy_server, model_server):
            server.close()


REQUEST = ChatRequest(messages=(ChatMessage("user", "ping"),), model="proxied-model")


async def _attempt(provider) -> str:
    try:
        response = await provider.complete(REQUEST)
    except LLMError as exc:
        cause = exc.__cause__
        return f"{type(exc).__name__}:{type(cause).__name__ if cause else None}"
    return f"ok:{response.content}"


async def test_failed_proxy_tunnels_release_model_pool(proxied_model, tool_delivery_evidence):
    _, evidence = tool_delivery_evidence
    proxy, model, provider = proxied_model
    attempts = evidence.setdefault("attempts", [])

    proxy.mode = "stall"
    for _ in range(POOL_LIMIT):
        attempts.append(("stall", await _attempt(provider)))
    proxy.mode = "drop"
    for _ in range(2):
        attempts.append(("drop", await _attempt(provider)))
    proxy.mode = "stall"
    for _ in range(2):
        proxy.established.clear()
        task = asyncio.create_task(provider.complete(REQUEST))
        async with asyncio.timeout(2):  # A starved pool never reaches the proxy.
            await proxy.established.wait()
        await asyncio.sleep(0.02)  # Inside the tunnel TLS handshake.
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        attempts.append(("cancelled", "CancelledError"))
    proxy.mode = "refuse"
    attempts.append(("refuse", await _attempt(provider)))

    failures = [result for mode, result in attempts if mode in {"stall", "drop"}]
    assert not any("PoolTimeout" in result for result in failures), failures
    assert all(result.startswith("LLM") for result in failures)
    assert attempts[-1][1] == "LLMUnavailableError:ProxyError"

    # Stalled and cancelled tunnels: the proxy saw the client close each socket.
    async with asyncio.timeout(2), proxy.closed:
        await proxy.closed.wait_for(lambda: proxy.peer_closed >= POOL_LIMIT + 2)
    evidence["peer_closed"] = proxy.peer_closed

    proxy.mode = "tunnel"
    for _ in range(2):
        attempts.append(("tunnel", await _attempt(provider)))
    assert [result for mode, result in attempts if mode == "tunnel"] == ["ok:recovered"] * 2
    evidence["model"] = {"connections": model.connections, "requests": model.requests}
    evidence["proxy_connects"] = proxy.connects
    assert model.connections == 1 and model.requests == 2
