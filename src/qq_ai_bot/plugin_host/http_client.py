"""Bounded public-HTTP client for approved in-process plugins."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from qq_ai_bot.services.http_logging import redact_httpx_url
from yuki_plugin_sdk.errors import PluginPermissionError
from yuki_plugin_sdk.permissions import PluginPermission
from yuki_plugin_sdk.results import PluginResult

DnsResolver = Callable[[str, int], Awaitable[tuple[str, ...]]]

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_FORBIDDEN_REQUEST_HEADERS = frozenset({"authorization", "cookie", "host", "proxy-authorization"})
_SAFE_RESPONSE_HEADERS = frozenset(
    {
        "etag",
        "last-modified",
        "retry-after",
        "link",
        "x-ratelimit-limit",
        "x-ratelimit-remaining",
        "x-ratelimit-used",
        "x-ratelimit-reset",
        "x-ratelimit-resource",
        "x-github-request-id",
    }
)


@dataclass(frozen=True, slots=True)
class _ResolvedTarget:
    """A logical URL paired with the already-approved address to connect to."""

    logical_url: str
    connect_url: str
    host: str
    host_header: str
    scheme: str
    port: int


class SafeHttpClient:
    """Validate every target and redirect before reading a bounded response body."""

    def __init__(
        self,
        *,
        timeout_seconds: float,
        max_response_bytes: int,
        client: httpx.AsyncClient | None = None,
        resolver: DnsResolver | None = None,
    ) -> None:
        if timeout_seconds <= 0 or max_response_bytes <= 0:
            raise ValueError("HTTP timeout and response limit must be positive")
        self._client = client or httpx.AsyncClient(
            timeout=timeout_seconds,
            follow_redirects=False,
            trust_env=False,
        )
        self._owns_client = client is None
        self._max_bytes = max_response_bytes
        self._resolver = resolver or _resolve_public_addresses
        self._timeout = httpx.Timeout(timeout_seconds)
        self._timeout_seconds = timeout_seconds

    async def request(
        self,
        method: str,
        url: str,
        *,
        allowed_hosts: frozenset[str] | None,
        headers: Mapping[str, str] | None = None,
        body: bytes | None = None,
        authorization: str | None = None,
    ) -> PluginResult:
        current = url
        normalized_method = method.strip().upper()
        if normalized_method not in {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"}:
            raise ValueError("unsupported HTTP method")
        caller_headers = {
            key: value
            for key, value in (headers or {}).items()
            if key.casefold() not in _FORBIDDEN_REQUEST_HEADERS
        }
        caller_origin: tuple[str, str, int] | None = None
        caller_data_allowed = True
        for redirect_index in range(6):
            try:
                target = await self._resolve_target(current, allowed_hosts)
            except PluginPermissionError:
                raise
            except (OSError, TimeoutError):
                return PluginResult(
                    ok=False,
                    error_code="http.request_failed",
                    detail="HTTP request failed",
                )
            origin = (target.scheme, target.host, target.port)
            if caller_origin is None:
                caller_origin = origin
            elif caller_origin != origin:
                caller_data_allowed = False
            request_headers = dict(caller_headers) if caller_data_allowed else {}
            if authorization is not None and caller_data_allowed:
                request_headers["Authorization"] = authorization
            request_headers["Host"] = target.host_header
            request_headers["Connection"] = "close"
            extensions: dict[str, object] = {"timeout": self._timeout.as_dict()}
            if target.scheme == "https":
                extensions["sni_hostname"] = target.host
            response: httpx.Response | None = None
            try:
                request = httpx.Request(
                    normalized_method,
                    target.connect_url,
                    headers=request_headers,
                    content=body if caller_data_allowed else None,
                    extensions=extensions,
                )
                with redact_httpx_url("[plugin-url-redacted]"):
                    response = await self._client.send(
                        request,
                        auth=None,
                        follow_redirects=False,
                        stream=True,
                    )
                if response.status_code in _REDIRECT_STATUSES:
                    location = response.headers.get("location")
                    if not location or redirect_index >= 5:
                        return PluginResult(
                            ok=False,
                            error_code="http.redirect_rejected",
                            detail="redirect is missing or exceeded the limit",
                        )
                    current = urljoin(target.logical_url, location)
                    if response.status_code == 303:
                        normalized_method = "GET"
                        body = None
                    continue
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > self._max_bytes:
                        return PluginResult(
                            ok=False,
                            error_code="http.response_too_large",
                            detail="response body exceeded the configured limit",
                        )
                    chunks.append(chunk)
                raw = b"".join(chunks)
                charset = response.encoding or "utf-8"
                text = raw.decode(charset, errors="replace")
                successful = response.is_success or response.status_code == 304
                safe_headers = {
                    key.casefold(): value[:2_000]
                    for key, value in response.headers.items()
                    if key.casefold() in _SAFE_RESPONSE_HEADERS
                }
                return PluginResult(
                    ok=successful,
                    data={
                        "status_code": response.status_code,
                        "body": text,
                        "content_type": response.headers.get("content-type", "")[:256],
                        "url": _without_query(target.logical_url),
                        "headers": safe_headers,
                    },
                    error_code=None if successful else "http.upstream_error",
                    detail=("" if successful else "upstream returned a non-success status"),
                )
            except httpx.HTTPError:
                # Never copy exception text into plugin-visible output: httpx errors may
                # contain the full URL (including query-string credentials) or headers.
                return PluginResult(
                    ok=False,
                    error_code="http.request_failed",
                    detail="HTTP request failed",
                )
            finally:
                if response is not None:
                    await response.aclose()
        raise AssertionError("redirect loop must terminate")

    async def _resolve_target(
        self,
        url: str,
        allowed_hosts: frozenset[str] | None,
    ) -> _ResolvedTarget:
        try:
            parsed = urlsplit(url)
            hostname = parsed.hostname
        except ValueError:
            raise PluginPermissionError("plugin HTTP URL is invalid") from None
        if parsed.scheme not in {"http", "https"} or not hostname:
            raise PluginPermissionError("plugin HTTP only accepts public HTTP(S) URLs")
        if parsed.username or parsed.password:
            raise PluginPermissionError("credentials in plugin HTTP URLs are forbidden")
        try:
            host = hostname.rstrip(".").casefold().encode("idna").decode("ascii")
        except UnicodeError:
            raise PluginPermissionError("plugin HTTP URL is invalid") from None
        if allowed_hosts is not None and host not in allowed_hosts:
            raise PluginPermissionError("HTTP host is not in the plugin allowlist")
        try:
            explicit_port = parsed.port
        except ValueError:
            raise PluginPermissionError("plugin HTTP URL has an invalid port") from None
        port = explicit_port or (443 if parsed.scheme == "https" else 80)
        async with asyncio.timeout(self._timeout_seconds):
            addresses = await self._resolver(host, port)
        try:
            parsed_addresses = tuple(ipaddress.ip_address(item) for item in addresses)
        except ValueError as exc:
            raise PluginPermissionError(
                "plugin HTTP target resolved to an invalid address"
            ) from exc
        if not parsed_addresses or any(not address.is_global for address in parsed_addresses):
            raise PluginPermissionError("plugin HTTP target resolved to a non-public address")
        logical_authority = _authority(host, explicit_port)
        connect_authority = _authority(str(parsed_addresses[0]), port)
        logical_url = urlunsplit((parsed.scheme, logical_authority, parsed.path, parsed.query, ""))
        connect_url = urlunsplit((parsed.scheme, connect_authority, parsed.path, parsed.query, ""))
        return _ResolvedTarget(
            logical_url=logical_url,
            connect_url=connect_url,
            host=host,
            host_header=logical_authority,
            scheme=parsed.scheme,
            port=port,
        )

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()


class BoundHttpFacade:
    def __init__(
        self,
        *,
        client: SafeHttpClient,
        approved_permissions: Iterable[PluginPermission],
        allowed_hosts: Iterable[str],
        secrets: object | None = None,
        http_concurrency: int = 1,
    ) -> None:
        if isinstance(http_concurrency, bool) or not isinstance(http_concurrency, int):
            raise ValueError("HTTP concurrency limit must be an integer from 1 to 64")
        if not 1 <= http_concurrency <= 64:
            raise ValueError("HTTP concurrency limit must be an integer from 1 to 64")
        permissions = frozenset(approved_permissions)
        unrestricted = PluginPermission.NETWORK_HTTP_UNRESTRICTED in permissions
        allowlisted = PluginPermission.NETWORK_HTTP_ALLOWLISTED in permissions
        self._enabled = unrestricted or allowlisted
        self._allowed_hosts = None if unrestricted else frozenset(allowed_hosts)
        self._client = client
        self._secrets = secrets
        self._semaphore = asyncio.Semaphore(http_concurrency)

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        body: bytes | None = None,
        auth_secret: str | None = None,
    ) -> PluginResult:
        if not self._enabled:
            raise PluginPermissionError("plugin lacks an HTTP permission")
        authorization: str | None = None
        if auth_secret is not None:
            get_secret = getattr(self._secrets, "get", None)
            if not callable(get_secret):
                raise PluginPermissionError("plugin HTTP credential service is unavailable")
            authorization = f"Bearer {get_secret(auth_secret)}"
        async with self._semaphore:
            return await self._client.request(
                method,
                url,
                allowed_hosts=self._allowed_hosts,
                headers=headers,
                body=body,
                authorization=authorization,
            )


async def _resolve_public_addresses(host: str, port: int) -> tuple[str, ...]:
    loop = asyncio.get_running_loop()
    records = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return tuple(dict.fromkeys(str(record[4][0]) for record in records))


def _without_query(url: str) -> str:
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _authority(host: str, port: int | None) -> str:
    bracketed = f"[{host}]" if ":" in host else host
    return f"{bracketed}:{port}" if port is not None else bracketed


__all__ = ["BoundHttpFacade", "SafeHttpClient"]
