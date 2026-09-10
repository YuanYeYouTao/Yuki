"""Resolve trusted OneBot image references with configurable SSRF and size controls."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import inspect
import ipaddress
import socket
from collections.abc import Awaitable, Callable, Sequence
from contextlib import nullcontext
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import SplitResult, urljoin, urlsplit, urlunsplit

import httpx

from qq_ai_bot.vision.models import DownloadedMedia, MediaReference

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_BLOCKED_HOSTS = frozenset(
    {
        "localhost",
        "docker",
        "bot",
        "napcat",
        "snowluma",
        "host.docker.internal",
        "gateway.docker.internal",
    }
)
_BLOCKED_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home", ".docker")
HostResolver = Callable[[str, int], Awaitable[Sequence[str]] | Sequence[str]]


class OneBotMediaGateway(Protocol):
    """Small gateway surface needed to resolve a OneBot file identifier."""

    async def call_api(self, action: str, params: dict[str, Any]) -> Any:
        """Call one OneBot action."""


class MediaResolutionError(RuntimeError):
    """A sanitized media error that never includes credentials or signed URLs."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


class MediaResolver:
    """Download or decode one trusted event-derived image reference."""

    def __init__(
        self,
        *,
        gateway: OneBotMediaGateway | None = None,
        max_download_bytes: int = 10 * 1024 * 1024,
        timeout_seconds: float = 10,
        max_redirects: int = 3,
        allow_private_urls: bool = False,
        client: httpx.AsyncClient | None = None,
        host_resolver: HostResolver | None = None,
    ) -> None:
        if max_download_bytes <= 0:
            raise ValueError("max_download_bytes must be positive")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if not 0 <= max_redirects <= 3:
            raise ValueError("max_redirects must be between 0 and 3")
        self._gateway = gateway
        self._max_download_bytes = max_download_bytes
        self._timeout_seconds = timeout_seconds
        self._max_redirects = max_redirects
        self._allow_private_urls = allow_private_urls
        self._host_resolver = host_resolver or _resolve_host
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds),
            follow_redirects=False,
            trust_env=False,
            # Connections are pinned to a validated IP below. Do not pool one
            # IP-origin connection across virtual hosts with different SNI.
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=0),
        )

    async def resolve(
        self,
        reference: MediaReference,
        gateway: OneBotMediaGateway | None = None,
    ) -> DownloadedMedia:
        """Resolve in the required URL, inline-data, then ``get_image`` order."""

        if reference.url and _looks_like_http(reference.url):
            return await self._download(reference.url)

        file_value = (reference.file or "").strip()
        if _looks_like_http(file_value):
            return await self._download(file_value)
        if file_value.startswith("base64://") or file_value.startswith("data:image/"):
            return self._decode_inline(file_value)
        if not file_value:
            raise MediaResolutionError("resource_unavailable", "图片资源缺少可读取的引用")
        active_gateway = gateway or self._gateway
        if active_gateway is None:
            raise MediaResolutionError("resource_unavailable", "图片文件引用当前不可读取")

        try:
            async with asyncio.timeout(self._timeout_seconds):
                payload = await active_gateway.call_api("get_image", {"file": file_value})
        except TimeoutError as exc:
            raise MediaResolutionError("get_image_failed", "图片资源查询超时") from exc
        except Exception as exc:
            raise MediaResolutionError("get_image_failed", "图片资源查询失败") from exc
        return await self._resolve_get_image_payload(payload)

    async def download_attachment(
        self, reference: MediaReference, destination: Path, *, max_download_bytes: int
    ) -> None:
        """Stream event media to a caller-owned temporary path using the same URL policy."""
        bounded = MediaResolver(
            max_download_bytes=max_download_bytes,
            timeout_seconds=self._timeout_seconds,
            max_redirects=self._max_redirects,
            allow_private_urls=self._allow_private_urls,
            client=self._client,
            host_resolver=self._host_resolver,
        )
        location = reference.url or reference.file or ""
        if _looks_like_http(location):
            async with asyncio.timeout(self._timeout_seconds):
                await bounded._download_with_redirects(location, destination=destination)
        elif location.startswith("base64://"):
            with destination.open("wb") as output:
                output.write(bounded._decode_inline(location).content)
        else:
            raise MediaResolutionError("resource_unavailable", "视频缺少可下载地址")

    async def _resolve_get_image_payload(self, payload: Any) -> DownloadedMedia:
        if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
            payload = payload["data"]
        if not isinstance(payload, dict):
            raise MediaResolutionError("resource_unavailable", "图片资源查询结果无效")
        for key in ("url", "file"):
            candidate = payload.get(key)
            if isinstance(candidate, str) and _looks_like_http(candidate):
                return await self._download(candidate)
        for key in ("base64", "file"):
            candidate = payload.get(key)
            if not isinstance(candidate, str):
                continue
            if key == "base64" and not candidate.startswith(("base64://", "data:image/")):
                candidate = f"base64://{candidate}"
            if candidate.startswith(("base64://", "data:image/")):
                return self._decode_inline(candidate)
        # A container-local path is intentionally not opened here.
        raise MediaResolutionError("resource_unavailable", "图片资源已失效或当前不可读取")

    async def _download(self, raw_url: str) -> DownloadedMedia:
        try:
            async with asyncio.timeout(self._timeout_seconds):
                result = await self._download_with_redirects(raw_url)
                assert result is not None
                return result
        except TimeoutError as exc:
            raise MediaResolutionError("timeout", "图片资源下载超时") from exc

    async def _download_with_redirects(
        self, raw_url: str, *, destination: Path | None = None
    ) -> DownloadedMedia | None:
        current = raw_url
        for redirect_count in range(self._max_redirects + 1):
            normalized, request_url, original_host = await self._validate_public_url(current)
            request_headers = {
                "Host": _host_header(normalized),
                "Connection": "close",
            }
            request_extensions = (
                {"sni_hostname": original_host}
                if urlsplit(normalized).scheme.casefold() == "https"
                else None
            )
            try:
                async with self._client.stream(
                    "GET",
                    request_url,
                    headers=request_headers,
                    follow_redirects=False,
                    timeout=self._timeout_seconds,
                    extensions=request_extensions,
                ) as response:
                    if response.status_code in _REDIRECT_STATUSES:
                        location = response.headers.get("Location")
                        if not location or redirect_count >= self._max_redirects:
                            raise MediaResolutionError(
                                "redirect_rejected", "图片下载重定向无效或次数过多"
                            )
                        current = urljoin(normalized, location)
                        continue
                    if response.status_code >= 400:
                        raise MediaResolutionError("download_failed", "图片资源下载失败")
                    declared = _content_length(response.headers.get("Content-Length"))
                    if declared is not None and declared > self._max_download_bytes:
                        raise MediaResolutionError("too_large", "图片超过允许的下载大小")
                    content = bytearray()
                    byte_count = 0
                    with destination.open("wb") if destination else nullcontext() as output:
                        async for chunk in response.aiter_bytes(chunk_size=65536):
                            byte_count += len(chunk)
                            if byte_count > self._max_download_bytes:
                                raise MediaResolutionError("too_large", "媒体超过允许的下载大小")
                            if output is not None:
                                output.write(chunk)
                            else:
                                content.extend(chunk)
            except MediaResolutionError:
                raise
            except httpx.TimeoutException as exc:
                raise MediaResolutionError("timeout", "图片资源下载超时") from exc
            except httpx.RequestError as exc:
                raise MediaResolutionError("download_failed", "无法连接图片资源") from exc
            if not byte_count:
                raise MediaResolutionError("empty_media", "图片资源为空")
            if destination is not None:
                return None
            return _downloaded(bytes(content), response.headers.get("Content-Type"))
        raise MediaResolutionError("redirect_rejected", "图片下载重定向次数过多")

    async def _validate_public_url(self, raw_url: str) -> tuple[str, str, str]:
        normalized, host, port = _normalize_http_url(
            raw_url,
            allow_private_urls=self._allow_private_urls,
        )
        resolved = self._host_resolver(host, port)
        try:
            addresses = await resolved if inspect.isawaitable(resolved) else resolved
        except (OSError, socket.gaierror) as exc:
            raise MediaResolutionError("dns_failed", "图片资源域名解析失败") from exc
        if not addresses:
            raise MediaResolutionError("dns_failed", "图片资源域名没有可用地址")
        validated_addresses: list[str] = []
        for value in addresses:
            try:
                address = ipaddress.ip_address(value)
            except ValueError as exc:
                raise MediaResolutionError("dns_failed", "图片资源域名解析结果无效") from exc
            if not self._allow_private_urls and not address.is_global:
                raise MediaResolutionError("private_url", "不允许访问本地、私有或保留地址")
            validated_addresses.append(address.compressed)
        # Connect to the exact address that was checked instead of allowing the
        # HTTP stack to resolve the hostname a second time (DNS-rebinding TOCTOU).
        return normalized, _replace_url_host(normalized, validated_addresses[0]), host

    def _decode_inline(self, value: str) -> DownloadedMedia:
        content_type: str | None = None
        if value.startswith("base64://"):
            encoded = value.removeprefix("base64://")
        else:
            header, separator, encoded = value.partition(",")
            if not separator or ";base64" not in header.casefold():
                raise MediaResolutionError("invalid_base64", "图片 data URL 必须使用 Base64")
            media_type = header[5:].split(";", 1)[0].casefold()
            if not media_type.startswith("image/"):
                raise MediaResolutionError("invalid_media_type", "data URL 不是图片")
            content_type = media_type
        max_encoded_length = ((self._max_download_bytes + 2) // 3) * 4 + 4
        # Reject before split/join so a whitespace-heavy attacker cannot force a
        # second unbounded allocation before the configured byte limit applies.
        if not encoded or len(encoded) > max_encoded_length + 4096:
            raise MediaResolutionError("too_large", "Base64 图片超过允许大小")
        encoded = "".join(encoded.split())
        if not encoded or len(encoded) > max_encoded_length:
            raise MediaResolutionError("too_large", "Base64 图片超过允许大小")
        try:
            content = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise MediaResolutionError("invalid_base64", "图片 Base64 无效") from exc
        if not content:
            raise MediaResolutionError("empty_media", "Base64 图片为空")
        if len(content) > self._max_download_bytes:
            raise MediaResolutionError("too_large", "Base64 图片超过允许大小")
        return _downloaded(content, content_type)

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()


async def _resolve_host(host: str, port: int) -> Sequence[str]:
    loop = asyncio.get_running_loop()
    records = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return tuple(dict.fromkeys(record[4][0].split("%", 1)[0] for record in records))


def _normalize_http_url(
    raw_url: str,
    *,
    allow_private_urls: bool = False,
) -> tuple[str, str, int]:
    candidate = raw_url.strip()
    if not candidate or len(candidate) > 4096:
        raise MediaResolutionError("invalid_url", "图片 URL 无效")
    try:
        parsed = urlsplit(candidate)
        explicit_port = parsed.port
    except ValueError as exc:
        raise MediaResolutionError("invalid_url", "图片 URL 格式无效") from exc
    scheme = parsed.scheme.casefold()
    if scheme not in {"http", "https"} or not parsed.hostname:
        raise MediaResolutionError("invalid_url", "只允许 HTTP 或 HTTPS 图片 URL")
    if parsed.username is not None or parsed.password is not None:
        raise MediaResolutionError("invalid_url", "图片 URL 不得包含用户名或密码")
    host = parsed.hostname.rstrip(".").casefold()
    try:
        ascii_host = host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise MediaResolutionError("invalid_url", "图片 URL 主机名无效") from exc
    if not allow_private_urls and (
        ascii_host in _BLOCKED_HOSTS
        or ascii_host.endswith(_BLOCKED_SUFFIXES)
        or ("." not in ascii_host and ":" not in ascii_host)
    ):
        raise MediaResolutionError("private_url", "不允许访问本地或内部主机")
    try:
        literal = ipaddress.ip_address(ascii_host)
    except ValueError:
        literal = None
    if not allow_private_urls and literal is not None and not literal.is_global:
        raise MediaResolutionError("private_url", "不允许访问本地、私有或保留地址")
    port = explicit_port or (443 if scheme == "https" else 80)
    is_default_port = (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    host_for_netloc = f"[{ascii_host}]" if ":" in ascii_host else ascii_host
    netloc = host_for_netloc if is_default_port else f"{host_for_netloc}:{port}"
    normalized = urlunsplit(SplitResult(scheme, netloc, parsed.path or "/", parsed.query, ""))
    return normalized, ascii_host, port


def _looks_like_http(value: str) -> bool:
    return value.strip().casefold().startswith(("http://", "https://"))


def _replace_url_host(url: str, address: str) -> str:
    parsed = urlsplit(url)
    port = parsed.port or (443 if parsed.scheme.casefold() == "https" else 80)
    default_port = (parsed.scheme.casefold() == "http" and port == 80) or (
        parsed.scheme.casefold() == "https" and port == 443
    )
    host = f"[{address}]" if ":" in address else address
    netloc = host if default_port else f"{host}:{port}"
    return urlunsplit(SplitResult(parsed.scheme, netloc, parsed.path, parsed.query, ""))


def _host_header(url: str) -> str:
    parsed = urlsplit(url)
    assert parsed.hostname is not None
    port = parsed.port or (443 if parsed.scheme.casefold() == "https" else 80)
    default_port = (parsed.scheme.casefold() == "http" and port == 80) or (
        parsed.scheme.casefold() == "https" and port == 443
    )
    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    return host if default_port else f"{host}:{port}"


def _content_length(value: str | None) -> int | None:
    if not value:
        return None
    try:
        parsed = int(value)
    except ValueError:
        return None
    return parsed if parsed >= 0 else None


def _downloaded(content: bytes, content_type: str | None) -> DownloadedMedia:
    normalized_type = None
    if content_type:
        candidate = content_type.partition(";")[0].strip().casefold()
        normalized_type = candidate or None
    return DownloadedMedia(
        content=content,
        content_type=normalized_type,
        content_hash=hashlib.sha256(content).hexdigest(),
        byte_size=len(content),
    )
