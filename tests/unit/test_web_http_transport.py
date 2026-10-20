"""Pinned destinations, proxy TLS and owned response resources over local sockets."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import ipaddress
import os
import ssl
import struct
import sys
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpcore
import httpx
import pytest
import pytest_asyncio
import trustme

from yuki.web.http_transport import create_web_http_client


@pytest.fixture(scope="module")
def event_loop_policy():
    # CPython 3.12's Proactor fake TLS server can fail before Server._detach
    # after a reset. This changes only the local server test fixture's loop.
    if sys.platform == "win32":
        return asyncio.WindowsSelectorEventLoopPolicy()
    return asyncio.DefaultEventLoopPolicy()


@pytest.fixture(autouse=True)
def isolated_proxy_environment(monkeypatch):
    for name in list(os.environ):
        if name.lower().endswith("_proxy") or name in {"SSL_CERT_FILE", "SSL_CERT_DIR"}:
            monkeypatch.delenv(name, raising=False)
    # Also prevents the native test host's registry/system proxy from leaking in.
    monkeypatch.setenv("NO_PROXY", "*")


@dataclass
class _Network:
    ca: trustme.CA
    servers: list = field(default_factory=list)
    tasks: set = field(default_factory=set)
    writers: set = field(default_factory=set)
    requests: list = field(default_factory=list)
    proxies: list = field(default_factory=list)
    socks: list = field(default_factory=list)
    sni: list = field(default_factory=list)
    ports: set = field(default_factory=set)
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    plain_port: int = 0
    tls_port: int = 0
    proxy_port: int = 0
    tls_proxy_port: int = 0
    wrong_proxy_port: int = 0
    socks_port: int = 0

    def context(self, *hostnames):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.ca.issue_cert(*hostnames).configure_cert(context)
        context.set_servername_callback(lambda obj, name, ctx: self.sni.append(name))
        return context

    async def serve(self, handler, tls=None):
        async def owned(reader, writer):
            task = asyncio.current_task()
            self.tasks.add(task)
            self.writers.add(writer)
            try:
                await handler(reader, writer)
            except (ConnectionError, asyncio.IncompleteReadError, ssl.SSLError):
                pass
            finally:
                writer.close()
                with contextlib.suppress(ConnectionError, ssl.SSLError):
                    await writer.wait_closed()
                self.writers.discard(writer)
                self.tasks.discard(task)

        tls_options = {"ssl_handshake_timeout": 1, "ssl_shutdown_timeout": 0.1} if tls else {}
        server = await asyncio.start_server(owned, "127.0.0.1", 0, ssl=tls, **tls_options)
        self.servers.append(server)
        return server.sockets[0].getsockname()[1]

    async def relay(self, reader, writer, upstream, uplink):
        async def pump(source, destination):
            try:
                while chunk := await source.read(65536):
                    destination.write(chunk)
                    await destination.drain()
            except ConnectionError:
                pass
            finally:
                destination.close()

        pumps = [
            asyncio.create_task(pump(reader, uplink)),
            asyncio.create_task(pump(upstream, writer)),
        ]
        try:
            await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in pumps:
                task.cancel()
            await asyncio.gather(*pumps, return_exceptions=True)

    async def origin(self, reader, writer):
        header = await reader.readuntil(b"\r\n\r\n")
        lines = header.decode().split("\r\n")
        path = lines[0].split()[1]
        headers = dict(line.split(": ", 1) for line in lines[1:] if ": " in line)
        self.requests.append(headers)
        self.changed.set()
        if path == "/blocked":
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 100000\r\n\r\nfirst")
            await writer.drain()
            await reader.read()
            return
        cookie = b"Set-Cookie: site_session=origin_secret; Path=/\r\n" if path == "/cookie" else b""
        body = b"public page"
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Length: 11\r\n"
            + cookie
            + b"Connection: close\r\n\r\n"
            + body
        )
        await writer.drain()

    async def proxy(self, reader, writer):
        header = await reader.readuntil(b"\r\n\r\n")
        lines = header.decode().split("\r\n")
        method, target, _ = lines[0].split()
        headers = dict(line.split(": ", 1) for line in lines[1:] if ": " in line)
        self.proxies.append((method, target, headers))
        parsed = urlsplit(f"http://{target}" if method == "CONNECT" else target)
        ipaddress.ip_address(parsed.hostname)
        assert parsed.port in self.ports
        upstream, uplink = await asyncio.open_connection("127.0.0.1", parsed.port)
        if method == "CONNECT":
            writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            await writer.drain()
        else:
            origin_lines = [f"{method} {parsed.path or '/'} HTTP/1.1"]
            origin_lines.extend(
                line for line in lines[1:] if not line.lower().startswith("proxy-authorization:")
            )
            uplink.write("\r\n".join(origin_lines).encode())
            await uplink.drain()
        await self.relay(reader, writer, upstream, uplink)

    async def socks_proxy(self, reader, writer):
        version, count = await reader.readexactly(2)
        methods = await reader.readexactly(count)
        assert version == 5
        method = 2 if 2 in methods else 0
        writer.write(bytes([5, method]))
        await writer.drain()
        auth = None
        if method == 2:
            assert (await reader.readexactly(1))[0] == 1
            user = await reader.readexactly((await reader.readexactly(1))[0])
            password = await reader.readexactly((await reader.readexactly(1))[0])
            auth = user, password
            writer.write(b"\x01\x00")
            await writer.drain()
        version, command, _, kind = await reader.readexactly(4)
        assert (version, command) == (5, 1)
        assert kind in {1, 4}, "The SOCKS proxy must receive an IP, not resolve the target name"
        address = str(ipaddress.ip_address(await reader.readexactly(4 if kind == 1 else 16)))
        port = struct.unpack("!H", await reader.readexactly(2))[0]
        self.socks.append((address, port, auth))
        assert port in self.ports
        upstream, uplink = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x00")
        await writer.drain()
        await self.relay(reader, writer, upstream, uplink)

    async def wait_requests(self, count):
        while len(self.requests) < count:
            self.changed.clear()
            await self.changed.wait()


@pytest_asyncio.fixture
async def network(tmp_path, monkeypatch):
    ca = trustme.CA()
    ca_path = tmp_path / "ca.pem"
    ca.cert_pem.write_to_path(ca_path)
    monkeypatch.setenv("SSL_CERT_FILE", str(ca_path))
    result = _Network(ca)
    result.plain_port = await result.serve(result.origin)
    result.tls_port = await result.serve(
        result.origin, result.context("origin.test", "sub.origin.test")
    )
    result.ports.update((result.plain_port, result.tls_port))
    result.proxy_port = await result.serve(result.proxy)
    result.tls_proxy_port = await result.serve(result.proxy, result.context("localhost"))
    result.wrong_proxy_port = await result.serve(result.proxy, result.context("wrong-proxy.test"))
    result.socks_port = await result.serve(result.socks_proxy)
    try:
        yield result
    finally:
        for server in result.servers:
            server.close()
        for writer in tuple(result.writers):
            writer.close()
        for task in tuple(result.tasks):
            task.cancel()
        await asyncio.gather(*tuple(result.tasks), return_exceptions=True)
        for server in result.servers:
            await server.wait_closed()


def _request(network, *, tls=True, host="origin.test", ip="1.1.1.1", path="/"):
    scheme, port = ("https", network.tls_port) if tls else ("http", network.plain_port)
    ip_host = f"[{ip}]" if ":" in ip else ip
    extensions = {"original_url": f"{scheme}://{host}:{port}{path}"}
    if tls:
        extensions["sni_hostname"] = host
    return httpx.Request(
        "GET", f"{scheme}://{ip_host}:{port}{path}", headers={"Host": host}, extensions=extensions
    )


@pytest.mark.parametrize("proxy_tls", [False, True])
@pytest.mark.parametrize("target_tls", [False, True])
@pytest.mark.parametrize("address", ["1.1.1.1", "2606:4700:4700::1111"])
async def test_http_proxies_keep_pinned_target_host_tls_and_proxy_auth(
    network, monkeypatch, proxy_tls, target_tls, address
):
    scheme = "https" if proxy_tls else "http"
    port = network.tls_proxy_port if proxy_tls else network.proxy_port
    monkeypatch.setenv("ALL_PROXY", f"{scheme}://probe_user:p%40ss@localhost:{port}")
    monkeypatch.setenv("NO_PROXY", "")
    async with create_web_http_client(timeout_seconds=1) as client:
        response = await client.send(_request(network, tls=target_tls, ip=address))
    assert response.text == "public page"
    method, target, headers = network.proxies[0]
    assert method == ("CONNECT" if target_tls else "GET")
    assert address in target
    if ":" in address:
        assert f"[{address}]:" in target
    assert (
        headers["Proxy-Authorization"] == "Basic " + base64.b64encode(b"probe_user:p@ss").decode()
    )
    assert network.requests[0]["Host"] == "origin.test"
    assert "Proxy-Authorization" not in network.requests[0]
    if proxy_tls:
        assert "localhost" in network.sni
    if target_tls:
        assert "origin.test" in network.sni


@pytest.mark.parametrize("proxy_tls,wrong_target", [(False, True), (True, True), (True, False)])
async def test_wrong_target_or_proxy_certificate_is_rejected_and_errors_are_safe(
    network, monkeypatch, caplog, proxy_tls, wrong_target
):
    port = network.tls_proxy_port if wrong_target else network.wrong_proxy_port
    if not proxy_tls:
        port = network.proxy_port
    monkeypatch.setenv(
        "HTTPS_PROXY", f"{'https' if proxy_tls else 'http'}://secret_user:secret@localhost:{port}"
    )
    monkeypatch.setenv("NO_PROXY", "")
    async with create_web_http_client(timeout_seconds=1) as client:
        with pytest.raises(httpx.ConnectError) as caught:
            await client.send(
                _request(network, host="wrong-origin.test" if wrong_target else "origin.test")
            )
    assert "SSLCertVerificationError" in caplog.text
    assert "secret" not in str(caught.value) + caplog.text
    assert not network.requests


@pytest.mark.parametrize("proxy_tls", [False, True])
async def test_untrusted_target_or_proxy_ca_is_not_bypassed(
    network, monkeypatch, tmp_path, proxy_tls
):
    other_ca = tmp_path / "other-ca.pem"
    trustme.CA().cert_pem.write_to_path(other_ca)
    monkeypatch.setenv("SSL_CERT_FILE", str(other_ca))
    scheme = "https" if proxy_tls else "http"
    port = network.tls_proxy_port if proxy_tls else network.proxy_port
    monkeypatch.setenv("HTTPS_PROXY", f"{scheme}://localhost:{port}")
    monkeypatch.setenv("NO_PROXY", "")
    async with create_web_http_client(timeout_seconds=1) as client:
        with pytest.raises(httpx.ConnectError):
            await client.send(_request(network))
    assert not network.requests


@pytest.mark.parametrize("target_tls", [False, True])
async def test_direct_connection_preserves_original_host_and_tls(network, target_tls):
    async with create_web_http_client(timeout_seconds=1) as client:
        response = await client.send(_request(network, tls=target_tls, ip="127.0.0.1"))
    assert response.text == "public page"
    assert network.requests[0]["Host"] == "origin.test"
    assert not network.proxies and not network.socks
    if target_tls:
        assert network.sni == ["origin.test"]


@pytest.mark.parametrize("scheme", ["socks5", "socks5h"])
@pytest.mark.parametrize("address", ["1.1.1.1", "2606:4700:4700::1111"])
async def test_socks_connects_literal_checked_ip_with_original_sni_and_auth(
    network, monkeypatch, scheme, address
):
    monkeypatch.setenv("ALL_PROXY", f"{scheme}://probe_user:p%40ss@localhost:{network.socks_port}")
    monkeypatch.setenv("NO_PROXY", "")
    async with create_web_http_client(timeout_seconds=1) as client:
        response = await client.send(_request(network, ip=address))
    assert response.text == "public page"
    assert network.socks == [(address, network.tls_port, (b"probe_user", b"p@ss"))]
    assert network.sni == ["origin.test"]


async def test_protocol_proxy_precedes_all_and_no_proxy_matches_original_host_port(
    network, monkeypatch
):
    monkeypatch.setenv("HTTPS_PROXY", f"http://localhost:{network.proxy_port}")
    monkeypatch.setenv("ALL_PROXY", f"socks5://localhost:{network.socks_port}")
    monkeypatch.setenv("NO_PROXY", f"origin.test:{network.plain_port}")
    async with create_web_http_client(timeout_seconds=1) as client:
        await client.send(_request(network))
        await client.send(_request(network, tls=False, ip="127.0.0.1"))
        await client.send(_request(network, tls=False, host="another.test"))
    assert len(network.proxies) == 1
    assert len(network.socks) == 1
    assert network.socks[0][:2] == ("1.1.1.1", network.plain_port)
    assert len(network.requests) == 3


async def test_environment_proxy_without_scheme_uses_standard_http(network, monkeypatch):
    monkeypatch.setenv("ALL_PROXY", f"localhost:{network.proxy_port}")
    monkeypatch.setenv("NO_PROXY", "")
    async with create_web_http_client(timeout_seconds=1) as client:
        response = await client.send(_request(network))
    assert response.text == "public page"
    assert len(network.proxies) == 1


@pytest.mark.parametrize(
    "no_proxy,original,bypass",
    [
        ("https://origin.test", "https://origin.test", True),
        ("https://origin.test:443", "https://origin.test", True),
        ("https://origin.test:443", "https://origin.test:8443", False),
        ("http://origin.test", "https://origin.test", False),
        ("https://origin.test", "https://sub.origin.test", False),
        ("https://test", "https://origin.test", False),
    ],
)
async def test_url_no_proxy_preserves_protocol_port_and_exact_domain(
    network, monkeypatch, no_proxy, original, bypass
):
    monkeypatch.setenv("HTTPS_PROXY", f"http://localhost:{network.proxy_port}")
    monkeypatch.setenv("NO_PROXY", no_proxy)
    request = _request(network, ip="127.0.0.1", host=httpx.URL(original).host)
    request.extensions["original_url"] = original
    async with create_web_http_client(timeout_seconds=1) as client:
        response = await client.send(request)
    assert response.text == "public page"
    assert bool(network.proxies) is not bypass


@pytest.mark.parametrize(
    "proxy", ["invalid://secret_user:secret@localhost", "http://secret_user:secret@localhost:bad"]
)
async def test_invalid_proxy_is_sanitized_without_direct_fallback(network, monkeypatch, proxy):
    monkeypatch.setenv("ALL_PROXY", proxy)
    monkeypatch.setenv("NO_PROXY", "")
    async with create_web_http_client(timeout_seconds=1) as client:
        with pytest.raises(httpx.ProxyError) as caught:
            await client.send(_request(network, tls=False, ip="127.0.0.1"))
    assert "secret" not in str(caught.value)
    assert not network.requests


async def test_anonymous_client_does_not_share_cookies_between_hosts_on_the_same_ip(network):
    async with create_web_http_client(timeout_seconds=1) as client:
        await client.send(
            _request(network, tls=False, host="first.test", ip="127.0.0.1", path="/cookie")
        )
        await client.send(_request(network, tls=False, host="second.test", ip="127.0.0.1"))
        assert not client.cookies
    assert all("Cookie" not in headers for headers in network.requests)


async def test_capacity_wait_times_out_and_client_close_does_not_admit_queued_connections(network):
    client = create_web_http_client(timeout_seconds=1)
    # Only capacity waiting is under test; local socket setup keeps its normal timeout.
    client.timeout = httpx.Timeout(1, pool=0.05)
    responses = []
    try:
        for _ in range(10):
            responses.append(
                await client.send(
                    _request(network, tls=False, ip="127.0.0.1", path="/blocked"), stream=True
                )
            )
        with pytest.raises(httpx.PoolTimeout):
            await client.send(_request(network, tls=False, ip="127.0.0.1"))
        # Closing is a separate contract: a pool timer must not race resource cleanup.
        client.timeout = httpx.Timeout(1, pool=None)
        queued = asyncio.create_task(client.send(_request(network, tls=False, ip="127.0.0.1")))
        await asyncio.sleep(0)
        await client.aclose()
        with pytest.raises(httpx.RequestError, match="closed"):
            await asyncio.wait_for(queued, timeout=10)  # Watchdog for queued rejection.
        assert len(network.requests) == 10
    finally:
        await client.aclose()
        for response in responses:
            await response.aclose()


async def test_repeated_cancel_joins_owned_close_before_releasing_capacity(network, monkeypatch):
    started, finish, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    close = httpcore.AsyncConnectionPool.aclose

    async def slow_close(pool):
        started.set()
        await finish.wait()
        await close(pool)
        closed.set()

    monkeypatch.setattr(httpcore.AsyncConnectionPool, "aclose", slow_close)
    async with create_web_http_client(timeout_seconds=1) as client:
        response = await client.send(
            _request(network, tls=False, ip="127.0.0.1", path="/blocked"), stream=True
        )
        closing = asyncio.create_task(response.aclose())
        await started.wait()
        closing.cancel()
        await asyncio.sleep(0)
        closing.cancel()
        await asyncio.sleep(0)
        assert not closing.done()
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await closing
        assert closed.is_set()
        # The original lease is gone: all ten physical slots remain available.
        responses = [
            await client.send(
                _request(network, tls=False, ip="127.0.0.1", path="/blocked"), stream=True
            )
            for _ in range(10)
        ]
        for response in responses:
            await response.aclose()
