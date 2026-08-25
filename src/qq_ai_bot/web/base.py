"""Web search provider protocol, safe errors, and public URL validation."""

from __future__ import annotations

import ipaddress
from typing import Protocol
from urllib.parse import SplitResult, urlsplit, urlunsplit

from qq_ai_bot.web.models import WebSearchRequest, WebSearchResponse, WebSearchSource

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
_BLOCKED_HOST_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home", ".docker")


class WebSearchError(RuntimeError):
    """A sanitized provider or protocol error safe to expose as a tool result."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


class WebSearchValidationError(WebSearchError):
    """The model supplied an invalid or disallowed query/URL."""


class WebSearchProvider(Protocol):
    """Provider-neutral web search and extraction contract."""

    async def search(self, request: WebSearchRequest) -> WebSearchResponse:
        """Search and extract a bounded set of relevant public pages."""

    async def extract(self, url: str, query: str) -> WebSearchSource:
        """Extract query-relevant content from one validated public page."""

    async def close(self) -> None:
        """Release provider-owned resources."""


def normalize_public_url(url: str) -> str:
    """Validate and normalize one public HTTP(S) URL without resolving DNS."""

    candidate = url.strip()
    if not candidate or len(candidate) > 2048:
        raise WebSearchValidationError("invalid_url", "URL 不能为空且不能超过 2048 个字符")
    try:
        parsed = urlsplit(candidate)
        port = parsed.port
    except ValueError as exc:
        raise WebSearchValidationError("invalid_url", "URL 格式无效") from exc
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
        raise WebSearchValidationError("invalid_url", "只允许公开的 http 或 https URL")
    if parsed.username is not None or parsed.password is not None:
        raise WebSearchValidationError("invalid_url", "URL 不得包含用户名或密码")

    host = parsed.hostname.rstrip(".").casefold()
    try:
        ascii_host = host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise WebSearchValidationError("invalid_url", "URL 主机名无效") from exc
    if (
        ascii_host in _BLOCKED_HOSTS
        or ascii_host.endswith(_BLOCKED_HOST_SUFFIXES)
        or ("." not in ascii_host and ":" not in ascii_host)
    ):
        raise WebSearchValidationError("private_url", "不允许访问本地或内部主机")
    try:
        address = ipaddress.ip_address(ascii_host)
    except ValueError:
        address = None
    if address is not None and (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    ):
        raise WebSearchValidationError("private_url", "不允许访问本地或私有 IP")

    default_port = (parsed.scheme.casefold() == "http" and port == 80) or (
        parsed.scheme.casefold() == "https" and port == 443
    )
    host_for_netloc = f"[{ascii_host}]" if ":" in ascii_host else ascii_host
    netloc = host_for_netloc if port is None or default_port else f"{host_for_netloc}:{port}"
    normalized = SplitResult(
        scheme=parsed.scheme.casefold(),
        netloc=netloc,
        path=parsed.path or "/",
        query=parsed.query,
        fragment="",
    )
    return urlunsplit(normalized)
