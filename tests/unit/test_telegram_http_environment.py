"""Telegram's default HTTP client uses the standard proxy and CA environment."""

from __future__ import annotations

import json
import logging
import selectors
import socket
import socketserver
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import anyio
import pytest
import trustme

from yuki.gateway.media import MediaResolutionError
from yuki.gateway.providers.telegram import TelegramAPIError, TelegramClient

_TOKEN = "fixture:not-a-real-telegram-token"
_PROXY_PASSWORD = "fixture-proxy-password"
_HTTP_ENV = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
)


class _TelegramNetwork:
    def __init__(self):
        self.requests = []
        self.connects = []
        self.reject_after = None
        self.origin_port = 0


@pytest.fixture
def telegram_network(monkeypatch, tmp_path):
    for key in _HTTP_ENV:
        monkeypatch.delenv(key, raising=False)
    connect_tcp = anyio.connect_tcp

    async def loopback_only(remote_host, remote_port, **kwargs):
        assert str(remote_host) == "127.0.0.1", "Test attempted a non-loopback connection"
        return await connect_tcp(remote_host, remote_port, **kwargs)

    monkeypatch.setattr(anyio, "connect_tcp", loopback_only)
    network = _TelegramNetwork()
    ca = trustme.CA()
    certificate = ca.issue_cert("127.0.0.1")
    ca_path = tmp_path / "telegram-test-ca.pem"
    ca.cert_pem.write_to_path(ca_path)
    monkeypatch.setenv("SSL_CERT_FILE", str(ca_path))

    class Origin(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            network.requests.append(("POST", self.path, body, dict(self.headers)))
            if self.path == f"/bot{_TOKEN}/getMe":
                result = {"id": 8000, "username": "fixture_bot"}
            else:
                assert self.path == f"/bot{_TOKEN}/getFile"
                result = {"file_path": "documents/original.txt", "file_size": 8}
            self.respond(json.dumps({"ok": True, "result": result}).encode(), "application/json")

        def do_GET(self):
            network.requests.append(("GET", self.path, None, dict(self.headers)))
            self.respond(b"document", "text/plain")

        def respond(self, content, content_type):
            self.send_response(200)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Content-Type", content_type)
            self.end_headers()
            self.wfile.write(content)

        def log_message(self, *_args):
            pass

    class Proxy(socketserver.StreamRequestHandler):
        def handle(self):
            line = self.rfile.readline().decode("ascii").strip()
            method, target, _version = line.split(" ", 2)
            headers = {}
            while (header := self.rfile.readline()) not in (b"\r\n", b""):
                name, value = header.decode("latin1").split(":", 1)
                headers[name.lower()] = value.strip()
            network.connects.append((method, target, headers))
            assert method == "CONNECT"
            assert target == f"127.0.0.1:{network.origin_port}"
            if network.reject_after is not None and len(network.connects) > network.reject_after:
                self.wfile.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
                self.wfile.flush()
                return
            with socket.create_connection(("127.0.0.1", network.origin_port)) as upstream:
                self.wfile.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
                self.wfile.flush()
                with selectors.DefaultSelector() as selector:
                    selector.register(self.connection, selectors.EVENT_READ, upstream)
                    selector.register(upstream, selectors.EVENT_READ, self.connection)
                    while True:
                        for key, _events in selector.select():
                            try:
                                data = key.fileobj.recv(65536)
                                if not data:
                                    return
                                key.data.sendall(data)
                            except ConnectionError:
                                return

    origin = ThreadingHTTPServer(("127.0.0.1", 0), Origin)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    certificate.configure_cert(context)
    origin.socket = context.wrap_socket(origin.socket, server_side=True)
    network.origin_port = origin.server_address[1]
    proxy = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Proxy)
    proxy.daemon_threads = True
    threads = [
        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        for server in (origin, proxy)
    ]
    for thread in threads:
        thread.start()
    network.api_url = f"https://127.0.0.1:{network.origin_port}"
    network.proxy_url = f"http://127.0.0.1:{proxy.server_address[1]}"
    try:
        yield network
    finally:
        for server in (proxy, origin):
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join()


async def _identity_and_media(network):
    client = TelegramClient(_TOKEN, api_base_url=network.api_url)
    try:
        await client.get_me()
        assert (client.self_id, client.username) == ("8000", "fixture_bot")
        media = await client.resolve_media_file("original-file", "file", max_download_bytes=1024)
        assert media.content == b"document"
    finally:
        await client.close()
    assert [(method, path) for method, path, _, _ in network.requests] == [
        ("POST", f"/bot{_TOKEN}/getMe"),
        ("POST", f"/bot{_TOKEN}/getFile"),
        ("GET", f"/file/bot{_TOKEN}/documents/original.txt"),
    ]
    assert network.requests[0][2] == {}
    assert network.requests[1][2] == {"file_id": "original-file"}


@pytest.mark.parametrize("proxy_name", ["HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"])
async def test_api_and_media_use_standard_proxy_and_ca(
    telegram_network, monkeypatch, caplog, proxy_name
):
    monkeypatch.setenv(proxy_name, telegram_network.proxy_url)
    caplog.set_level(logging.INFO, logger="httpx")
    await _identity_and_media(telegram_network)
    assert len(telegram_network.connects) == 3
    assert _TOKEN not in caplog.text
    assert "[telegram-url-redacted]" in caplog.text


@pytest.mark.parametrize("no_proxy_name", ["NO_PROXY", "no_proxy"])
async def test_no_proxy_bypasses_proxy_for_api_and_media(
    telegram_network, monkeypatch, no_proxy_name
):
    monkeypatch.setenv("HTTPS_PROXY", telegram_network.proxy_url)
    monkeypatch.setenv(no_proxy_name, "127.0.0.1")
    await _identity_and_media(telegram_network)
    assert telegram_network.connects == []


async def test_no_proxy_configuration_keeps_normal_direct_requests(telegram_network):
    await _identity_and_media(telegram_network)
    assert telegram_network.connects == []


async def test_proxy_credentials_do_not_reach_telegram_or_request_logs(
    telegram_network, monkeypatch, caplog
):
    proxy_url = telegram_network.proxy_url.replace(
        "http://", f"http://fixture-proxy-user:{_PROXY_PASSWORD}@"
    )
    monkeypatch.setenv("HTTPS_PROXY", proxy_url)
    caplog.set_level(logging.INFO, logger="httpx")
    await _identity_and_media(telegram_network)
    assert all(headers.get("proxy-authorization") for _, _, headers in telegram_network.connects)
    assert all(
        "proxy-authorization" not in {key.lower() for key in headers}
        for _, _, _, headers in telegram_network.requests
    )
    assert _TOKEN not in caplog.text and _PROXY_PASSWORD not in caplog.text


@pytest.mark.parametrize("failure_stage", ["api", "download"])
async def test_proxy_failure_is_not_retried_as_direct(
    telegram_network, monkeypatch, caplog, failure_stage
):
    monkeypatch.setenv("HTTPS_PROXY", telegram_network.proxy_url)
    telegram_network.reject_after = 0 if failure_stage == "api" else 1
    caplog.set_level(logging.INFO, logger="httpx")
    client = TelegramClient(_TOKEN, api_base_url=telegram_network.api_url)
    try:
        if failure_stage == "api":
            with pytest.raises(TelegramAPIError) as caught:
                await client.get_me()
            assert caught.value.code == "telegram_transport_unknown"
            assert caught.value.uncertain
        else:
            with pytest.raises(MediaResolutionError) as caught:
                await client.resolve_media_file("original-file", "file", max_download_bytes=1024)
            assert caught.value.code == "download_failed"
    finally:
        await client.close()
    assert len(telegram_network.connects) == (1 if failure_stage == "api" else 2)
    assert [path for _, path, _, _ in telegram_network.requests] == (
        [] if failure_stage == "api" else [f"/bot{_TOKEN}/getFile"]
    )
    assert _TOKEN not in caplog.text
