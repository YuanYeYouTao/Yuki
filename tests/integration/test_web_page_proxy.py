"""Public page readers use standard proxies without weakening their URL policy."""

from __future__ import annotations

import asyncio
import contextlib
import ssl
import sys
from collections.abc import AsyncIterator

import anyio
import httpx
import pytest
import pytest_asyncio
import trustme

from qq_ai_bot.services import media_resolver
from qq_ai_bot.services.media_resolver import MediaResolutionError, MediaResolver
from qq_ai_bot.vision.models import MediaReference
from qq_ai_bot.web.base import WebSearchError
from qq_ai_bot.web.bridge_state import BridgeState
from qq_ai_bot.web.page_extractor import DirectPageExtractor

_PROXY_ENV = (
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
)


@pytest.fixture(scope="module")
def event_loop_policy():
    # Python 3.12 Proactor's fake TLS server can retain its server attachment
    # after cancellation despite both peer handlers finishing. Do not change
    # the application's policy to work around a loopback test-server issue.
    if sys.platform == "win32":
        return asyncio.WindowsSelectorEventLoopPolicy()
    return asyncio.DefaultEventLoopPolicy()


class _WebNetwork:
    """Real loopback sockets; public fixture addresses never leave this process."""

    def __init__(self):
        self.answers = {
            "origin.example": ["1.1.1.1"],
            "next.example": ["8.8.8.8"],
            "private.example": ["127.0.0.1"],
            "mixed.example": ["1.1.1.1", "127.0.0.1"],
        }
        self.dns = []
        self.connects = []
        self.direct_dials = []
        self.snis = []
        self.requests = []
        self.active = set()
        self.errors = []
        self.started = {stage: asyncio.Event() for stage in ("headers", "body")}

    async def resolve(self, host, port):
        self.dns.append((host, port))
        return self.answers[host]

    async def origin(self, reader, writer):
        task = asyncio.current_task()
        self.active.add(task)
        try:
            raw = await reader.readuntil(b"\r\n\r\n")
            lines = raw.decode("latin1").split("\r\n")
            path = lines[0].split(" ")[1]
            headers = {
                name.lower(): value.strip()
                for name, value in (line.split(":", 1) for line in lines[1:] if ":" in line)
            }
            self.requests.append((path, headers))
            if path == "/slow-headers":
                self.started["headers"].set()
                await reader.read()
                return
            if path == "/slow-body":
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n"
                    b"Content-Length: 100000\r\nConnection: close\r\n\r\nfirst-chunk"
                )
                await writer.drain()
                self.started["body"].set()
                await reader.read()
                return
            if path in {"/redirect", "/private-redirect"}:
                host = "next.example" if path == "/redirect" else "private.example"
                writer.write(
                    f"HTTP/1.1 302 Found\r\nLocation: https://{host}/final\r\n"
                    "Content-Length: 0\r\nConnection: close\r\n\r\n".encode()
                )
            elif path == "/too-large":
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n"
                    b"Content-Length: 2097153\r\nConnection: close\r\n\r\n"
                )
            else:
                body = b"<html><script>not page text</script><body>Original article</body></html>"
                cookie = (
                    "Set-Cookie: session=origin-secret; Path=/\r\n" if path == "/cookie" else ""
                )
                writer.write(
                    (
                        "HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n"
                        f"Content-Length: {len(body)}\r\n{cookie}Connection: close\r\n\r\n"
                    ).encode()
                    + body
                )
            await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError, ssl.SSLError):
            pass
        except Exception as exc:
            self.errors.append(f"origin: {type(exc).__name__}: {exc}")
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError, ssl.SSLError, TimeoutError):
                await asyncio.wait_for(writer.wait_closed(), 0.25)
            self.active.discard(task)

    async def proxy(self, reader, writer):
        task = asyncio.current_task()
        self.active.add(task)
        upstream = None
        pumps = []
        try:
            raw = await reader.readuntil(b"\r\n\r\n")
            line = raw.decode("latin1").split("\r\n")[0]
            method, target, _ = line.split(" ", 2)
            assert method == "CONNECT", line
            assert target in {"1.1.1.1:443", "8.8.8.8:443"}, target
            self.connects.append(target)
            upstream_reader, upstream = await asyncio.open_connection("127.0.0.1", self.tls_port)
            writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            await writer.drain()

            async def pump(source, destination):
                try:
                    while chunk := await source.read(65536):
                        destination.write(chunk)
                        await destination.drain()
                except ConnectionError:
                    pass

            pumps = [
                asyncio.create_task(pump(reader, upstream)),
                asyncio.create_task(pump(upstream_reader, writer)),
            ]
            _, pending = await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
            for item in pending:
                item.cancel()
            await asyncio.gather(*pumps, return_exceptions=True)
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        except Exception as exc:
            self.errors.append(f"proxy: {type(exc).__name__}: {exc}")
        finally:
            for item in pumps:
                if not item.done():
                    item.cancel()
            await asyncio.gather(*pumps, return_exceptions=True)
            for stream in (writer, upstream):
                if stream is not None:
                    stream.close()
            for stream in (writer, upstream):
                if stream is not None:
                    with contextlib.suppress(ConnectionError, TimeoutError):
                        await asyncio.wait_for(stream.wait_closed(), 0.25)
            self.active.discard(task)

    async def wait_idle(self):
        async with asyncio.timeout(2):
            while self.active:
                await asyncio.gather(*tuple(self.active))
        assert self.errors == []


@pytest_asyncio.fixture
async def web_network(monkeypatch, tmp_path) -> AsyncIterator[_WebNetwork]:
    network = _WebNetwork()
    for key in _PROXY_ENV:
        monkeypatch.delenv(key, raising=False)
    ca = trustme.CA()
    cert = ca.issue_cert("origin.example", "next.example")
    ca_path = tmp_path / "ca.pem"
    ca.cert_pem.write_to_path(ca_path)
    monkeypatch.setenv("SSL_CERT_FILE", str(ca_path))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    cert.configure_cert(context)
    context.set_servername_callback(lambda _, name, __: network.snis.append(name))
    tls = await asyncio.start_server(
        network.origin, "127.0.0.1", 0, ssl=context, ssl_shutdown_timeout=0.25
    )
    plain = await asyncio.start_server(network.origin, "127.0.0.1", 0)
    proxy = await asyncio.start_server(network.proxy, "127.0.0.1", 0)
    network.tls_port = tls.sockets[0].getsockname()[1]
    plain_port = plain.sockets[0].getsockname()[1]
    proxy_port = proxy.sockets[0].getsockname()[1]
    network.proxy_url = f"http://127.0.0.1:{proxy_port}"
    monkeypatch.setenv("HTTPS_PROXY", network.proxy_url)
    monkeypatch.setattr(media_resolver, "_resolve_host", network.resolve)
    connect_tcp = anyio.connect_tcp

    async def local_connect_tcp(remote_host, remote_port, **kwargs):
        if str(remote_host) in {"1.1.1.1", "8.8.8.8"}:
            network.direct_dials.append((str(remote_host), remote_port))
            assert remote_port in {80, 443}
            remote_host = "127.0.0.1"
            remote_port = network.tls_port if remote_port == 443 else plain_port
        else:
            assert str(remote_host) == "127.0.0.1", "Test attempted non-loopback network"
        return await connect_tcp(remote_host, remote_port, **kwargs)

    monkeypatch.setattr(anyio, "connect_tcp", local_connect_tcp)
    try:
        yield network
    finally:
        for server in (proxy, tls, plain):
            server.close()
            await server.wait_closed()
        pending = tuple(network.active)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        assert network.errors == []


def _extractor(tmp_path, timeout=2):
    return DirectPageExtractor(
        state=BridgeState(tmp_path / "pages.sqlite3"), timeout_seconds=timeout
    )


async def test_default_reader_follows_proxy_redirect_with_checked_ips_and_original_sni(
    web_network, tmp_path
):
    reader = _extractor(tmp_path)
    try:
        result = await reader.extract("https://origin.example/redirect", "article")
    finally:
        await reader.close()
    assert result.url == "https://origin.example/redirect"
    assert result.relevant_content == "Original article"
    assert web_network.dns == [("origin.example", 443), ("next.example", 443)]
    assert web_network.connects == ["1.1.1.1:443", "8.8.8.8:443"]
    assert web_network.snis == ["origin.example", "next.example"]
    assert [headers["host"] for _, headers in web_network.requests] == [
        "origin.example",
        "next.example",
    ]
    assert web_network.direct_dials == []
    await web_network.wait_idle()


async def test_no_proxy_matches_original_host_again_on_redirect(web_network, tmp_path, monkeypatch):
    monkeypatch.setenv("NO_PROXY", "origin.example")
    reader = _extractor(tmp_path)
    try:
        result = await reader.extract("https://origin.example/redirect", "article")
    finally:
        await reader.close()
    assert result.relevant_content == "Original article"
    assert web_network.direct_dials == [("1.1.1.1", 443)]
    assert web_network.connects == ["8.8.8.8:443"]
    assert web_network.snis == ["origin.example", "next.example"]
    await web_network.wait_idle()


@pytest.mark.parametrize("path,expected_connects", [("/private-redirect", 1), ("mixed", 0)])
async def test_proxy_never_receives_a_rejected_private_or_mixed_dns_hop(
    web_network, tmp_path, path, expected_connects
):
    reader = _extractor(tmp_path)
    url = "https://mixed.example/article" if path == "mixed" else "https://origin.example" + path
    try:
        with pytest.raises(WebSearchError) as caught:
            await reader.extract(url, "article")
    finally:
        await reader.close()
    assert isinstance(caught.value.__cause__, MediaResolutionError)
    assert caught.value.__cause__.code == "private_url"
    assert len(web_network.connects) == expected_connects
    assert web_network.direct_dials == []
    await web_network.wait_idle()


async def test_page_cache_survives_proxy_change_and_download_failure_is_not_cached(
    web_network, tmp_path, monkeypatch
):
    reader = _extractor(tmp_path)
    try:
        first = await reader.extract("https://origin.example/article", "same question")
        count = len(web_network.connects)
        monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
        cached = await reader.extract("https://origin.example/article", "same question")
        assert cached == first
        assert len(web_network.connects) == count
        with pytest.raises(WebSearchError):
            await reader.extract("https://origin.example/article", "new question")
        assert web_network.direct_dials == []
        monkeypatch.setenv("HTTPS_PROXY", web_network.proxy_url)
        recovered = await reader.extract("https://origin.example/article", "new question")
        assert recovered.relevant_content == first.relevant_content
        assert len(web_network.connects) == count + 1
    finally:
        await reader.close()
    await web_network.wait_idle()


@pytest.mark.parametrize("stage", ["headers", "body"])
async def test_reader_web_timeout_closes_stalled_proxy_stream(web_network, tmp_path, stage):
    reader = _extractor(tmp_path, timeout=0.08)
    client = reader.media._client
    try:
        with pytest.raises(WebSearchError) as caught:
            await asyncio.wait_for(reader.extract(f"https://origin.example/slow-{stage}", "q"), 1)
        assert isinstance(caught.value.__cause__, MediaResolutionError)
        assert caught.value.__cause__.code == "timeout"
        await web_network.wait_idle()
    finally:
        await reader.close()
    assert client.is_closed


@pytest.mark.parametrize("stage", ["headers", "body"])
async def test_reader_cancel_propagates_and_closes_its_proxy_connection(
    web_network, tmp_path, stage
):
    reader = _extractor(tmp_path)
    client = reader.media._client
    task = asyncio.create_task(reader.extract(f"https://origin.example/slow-{stage}", "q"))
    try:
        await asyncio.wait_for(web_network.started[stage].wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        await web_network.wait_idle()
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await reader.close()
    assert client.is_closed


async def test_public_reader_does_not_send_one_hosts_cookie_to_another_host(web_network, tmp_path):
    web_network.answers["next.example"] = ["1.1.1.1"]
    reader = _extractor(tmp_path)
    try:
        await reader.extract("https://origin.example/cookie", "q")
        await reader.extract("https://next.example/article", "q")
    finally:
        await reader.close()
    assert [headers.get("cookie") for _, headers in web_network.requests] == [None, None]
    await web_network.wait_idle()


async def test_reader_closes_proxy_response_when_declared_page_exceeds_its_budget(
    web_network, tmp_path
):
    reader = _extractor(tmp_path)
    try:
        with pytest.raises(WebSearchError) as caught:
            await reader.extract("https://origin.example/too-large", "q")
        assert isinstance(caught.value.__cause__, MediaResolutionError)
        assert caught.value.__cause__.code == "too_large"
        await web_network.wait_idle()
    finally:
        await reader.close()


async def test_other_media_resolvers_keep_their_existing_direct_default(web_network, monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", web_network.proxy_url)
    media = MediaResolver(host_resolver=web_network.resolve)
    try:
        result = await media.resolve(MediaReference(url="http://origin.example/article"))
    finally:
        await media.close()
    assert b"Original article" in result.content
    assert web_network.direct_dials == [("1.1.1.1", 80)]
    assert web_network.connects == []
    await web_network.wait_idle()


async def test_reader_leaves_injected_media_client_ownership_with_the_caller(tmp_path):
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200)))
    media = MediaResolver(client=client)
    reader = DirectPageExtractor(
        state=BridgeState(tmp_path / "pages.sqlite3"), timeout_seconds=1, media=media
    )
    try:
        await reader.close()
        assert not client.is_closed
    finally:
        await client.aclose()
