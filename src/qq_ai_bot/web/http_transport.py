"""Environment-aware HTTP for public pages whose destination IP was validated."""

from __future__ import annotations

import asyncio
import logging
import ssl
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import contextmanager
from http.cookiejar import CookieJar, DefaultCookiePolicy
from typing import Any
from urllib.request import getproxies, proxy_bypass

import httpcore
import httpx

logger = logging.getLogger(__name__)

_ERRORS: dict[type[Exception], type[httpx.RequestError]] = {
    httpcore.ConnectTimeout: httpx.ConnectTimeout,
    httpcore.ReadTimeout: httpx.ReadTimeout,
    httpcore.WriteTimeout: httpx.WriteTimeout,
    httpcore.PoolTimeout: httpx.PoolTimeout,
    httpcore.ConnectError: httpx.ConnectError,
    httpcore.ReadError: httpx.ReadError,
    httpcore.WriteError: httpx.WriteError,
    httpcore.RemoteProtocolError: httpx.RemoteProtocolError,
    httpcore.LocalProtocolError: httpx.LocalProtocolError,
    httpcore.ProxyError: httpx.ProxyError,
    httpcore.UnsupportedProtocol: httpx.UnsupportedProtocol,
}


@contextmanager
def _map_errors() -> Iterator[None]:
    try:
        yield
    except (
        httpcore.TimeoutException,
        httpcore.NetworkError,
        httpcore.ProtocolError,
        httpcore.ProxyError,
        httpcore.UnsupportedProtocol,
    ) as exc:
        error = _ERRORS.get(type(exc), httpx.RequestError)
        cause: BaseException = exc
        while (next_cause := cause.__cause__ or cause.__context__) is not None:
            cause = next_cause
        logger.warning("web_http_failed cause=%s", type(cause).__name__)
        # Proxy replies and URLs may contain credentials. Preserve the category,
        # rather than copying remote text into an exception or its traceback.
        raise error(f"Web HTTP {type(exc).__name__}") from None


async def _join_cleanup[T](task: asyncio.Future[T]) -> T:
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError as original:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        try:
            task.result()
        except Exception as cleanup:
            original.add_note(f"web_http_cleanup_failed:{type(cleanup).__name__}")
        raise


class _TLSStream(httpcore.AsyncNetworkStream):
    def __init__(
        self,
        stream: httpcore.AsyncNetworkStream,
        *,
        target_host: str,
        proxy_host: str,
        target_context: ssl.SSLContext,
        proxy_context: ssl.SSLContext,
    ) -> None:
        self._stream = stream
        self._target_host = target_host
        self._proxy_host = proxy_host
        self._target_context = target_context
        self._proxy_context = proxy_context

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:  # noqa: ASYNC109
        return await self._stream.read(max_bytes, timeout)

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:  # noqa: ASYNC109
        await self._stream.write(buffer, timeout)

    async def aclose(self) -> None:
        await self._stream.aclose()

    def get_extra_info(self, info: str) -> Any:
        return self._stream.get_extra_info(info)

    async def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,  # noqa: ASYNC109 - public HTTPCore interface
    ) -> httpcore.AsyncNetworkStream:
        # CONNECT currently forwards the target's sni_hostname to the proxy's
        # own connection too. Distinct contexts identify the actual TLS peer.
        if ssl_context is self._proxy_context:
            server_hostname = self._proxy_host
        elif ssl_context is self._target_context:
            server_hostname = self._target_host
        stream = await self._stream.start_tls(ssl_context, server_hostname, timeout)
        return _TLSStream(
            stream,
            target_host=self._target_host,
            proxy_host=self._proxy_host,
            target_context=self._target_context,
            proxy_context=self._proxy_context,
        )


class _TLSBackend(httpcore.AsyncNetworkBackend):
    def __init__(
        self,
        *,
        target_host: str,
        proxy_host: str,
        target_context: ssl.SSLContext,
        proxy_context: ssl.SSLContext,
    ) -> None:
        self._backend = httpcore.AnyIOBackend()
        self._target_host = target_host
        self._proxy_host = proxy_host
        self._target_context = target_context
        self._proxy_context = proxy_context

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,  # noqa: ASYNC109 - public HTTPCore interface
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> httpcore.AsyncNetworkStream:
        stream = await self._backend.connect_tcp(host, port, timeout, local_address, socket_options)
        return _TLSStream(
            stream,
            target_host=self._target_host,
            proxy_host=self._proxy_host,
            target_context=self._target_context,
            proxy_context=self._proxy_context,
        )


class _OwnedPool(httpx.AsyncByteStream):
    def __init__(
        self, pool: httpcore.AsyncConnectionPool, release: Callable[[_OwnedPool], None]
    ) -> None:
        self.pool = pool
        self.response: httpcore.Response | None = None
        self._release = release
        self._closing: asyncio.Task[None] | None = None

    async def __aiter__(self) -> AsyncIterator[bytes]:
        assert self.response is not None
        with _map_errors():
            async for part in self.response.aiter_stream():
                yield part

    async def _close(self) -> None:
        try:
            with _map_errors():
                if self.response is not None:
                    await self.response.aclose()
        finally:
            try:
                with _map_errors():
                    await self.pool.aclose()
            finally:
                self._release(self)

    async def aclose(self) -> None:
        if self._closing is None:
            self._closing = asyncio.create_task(self._close())
        await _join_cleanup(self._closing)


class _WebHTTPTransport(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        # Keep the original MediaResolver's total physical capacity, even though
        # each response owns a pool to avoid pooling by IP across virtual hosts.
        self._capacity = asyncio.Semaphore(10)
        self._active: set[_OwnedPool] = set()
        self._closed = False
        self._target_context = httpx.create_ssl_context(trust_env=True)
        self._proxy_context = httpx.create_ssl_context(trust_env=True)

    def _pool(self, original_url: httpx.URL) -> httpcore.AsyncConnectionPool:
        proxies = getproxies()
        selected = None
        host = original_url.raw_host.decode("ascii")
        if ":" in host:
            host = f"[{host}]"
        port = original_url.port or (443 if original_url.scheme == "https" else 80)
        authority = f"{host}:{port}"
        if not (proxy_bypass(authority) or proxy_bypass(f"{original_url.scheme}://{authority}")):
            selected = proxies.get(original_url.scheme) or proxies.get("all")
        options: dict[str, Any] = {
            "ssl_context": self._target_context,
            "max_connections": 1,
            "max_keepalive_connections": 0,
            "retries": 0,
        }
        if not selected:
            return httpcore.AsyncConnectionPool(**options)
        try:
            if "://" not in selected:
                selected = f"http://{selected}"
            proxy = httpx.Proxy(selected)
            if proxy.url.scheme in {"socks5", "socks5h"}:
                return httpcore.AsyncSOCKSProxy(
                    str(proxy.url), proxy_auth=proxy.raw_auth, **options
                )
            return httpcore.AsyncHTTPProxy(
                str(proxy.url),
                proxy_auth=proxy.raw_auth,
                proxy_ssl_context=(self._proxy_context if proxy.url.scheme == "https" else None),
                network_backend=_TLSBackend(
                    target_host=original_url.host,
                    proxy_host=proxy.url.host,
                    target_context=self._target_context,
                    proxy_context=self._proxy_context,
                ),
                **options,
            )
        except (ValueError, httpx.InvalidURL):
            raise httpx.ProxyError("Invalid web proxy configuration") from None

    def _release(self, owned: _OwnedPool) -> None:
        self._active.remove(owned)
        self._capacity.release()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        original = httpx.URL(request.extensions["original_url"])
        try:
            async with asyncio.timeout(request.extensions.get("timeout", {}).get("pool")):
                await self._capacity.acquire()
        except TimeoutError:
            raise httpx.PoolTimeout("Web HTTP PoolTimeout") from None
        if self._closed:
            self._capacity.release()
            raise httpx.RequestError("Web HTTP client is closed")
        try:
            pool = self._pool(original)
        except BaseException:
            self._capacity.release()
            raise
        owned = _OwnedPool(pool, self._release)
        self._active.add(owned)
        try:
            host = request.url.raw_host
            if isinstance(pool, httpcore.AsyncHTTPProxy) and b":" in host:
                # HTTPCore serializes proxy authorities from URL.host verbatim.
                # IPv6 CONNECT and absolute-form targets require brackets.
                host = b"[" + host + b"]"
            core_request = httpcore.Request(
                request.method,
                url=httpcore.URL(
                    scheme=request.url.raw_scheme,
                    host=host,
                    port=request.url.port,
                    target=request.url.raw_path,
                ),
                headers=request.headers.raw,
                content=request.stream,
                extensions=request.extensions,
            )
            with _map_errors():
                owned.response = await pool.handle_async_request(core_request)
            return httpx.Response(
                owned.response.status,
                headers=owned.response.headers,
                stream=owned,
                extensions=owned.response.extensions,
            )
        except BaseException:
            await owned.aclose()
            raise

    async def aclose(self) -> None:
        self._closed = True
        await _join_cleanup(asyncio.gather(*(owned.aclose() for owned in tuple(self._active))))


def create_web_http_client(*, timeout_seconds: float) -> httpx.AsyncClient:
    """Create an anonymous page client; the caller owns its close lifecycle."""
    return httpx.AsyncClient(
        transport=_WebHTTPTransport(),
        timeout=httpx.Timeout(timeout_seconds),
        follow_redirects=False,
        trust_env=False,  # The transport selects standard proxies using the original URL.
        cookies=CookieJar(policy=DefaultCookiePolicy(allowed_domains=())),
    )
