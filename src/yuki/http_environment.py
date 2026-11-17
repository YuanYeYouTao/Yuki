"""Load standard HTTP transport environment once at process startup."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from dotenv import dotenv_values
from httpcore._async.http_proxy import AsyncTunnelHTTPConnection
from httpcore._synchronization import AsyncShieldCancellation

_PROXY_VARIABLES = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")
_CA_VARIABLES = ("SSL_CERT_FILE", "SSL_CERT_DIR")


_tunnel_request = AsyncTunnelHTTPConnection.handle_async_request


async def _release_failed_tunnel(self: AsyncTunnelHTTPConnection, request: Any) -> Any:
    # HTTPCore 1.0.9 leaves the proxy connection ACTIVE when TLS through CONNECT
    # fails or is cancelled, so the pool never reclaims the slot (encode/httpcore#1023).
    try:
        return await _tunnel_request(self, request)
    except BaseException:
        if not self._connected:
            with AsyncShieldCancellation():
                await self._connection.aclose()
        raise


def load_http_environment(env_file: Path = Path(".env")) -> None:
    """Share dotenv proxy/CA settings without exporting unrelated Bot secrets."""

    AsyncTunnelHTTPConnection.handle_async_request = _release_failed_tunnel  # type: ignore[method-assign]

    values = dotenv_values(env_file, encoding="utf-8")
    for name in _PROXY_VARIABLES:
        lowercase = name.lower()
        if name in os.environ or lowercase in os.environ:
            continue
        value = values.get(lowercase) if lowercase in values else values.get(name)
        if value is not None:
            os.environ[name] = value

    # HTTPX prefers FILE to DIR. A file value must not override a process DIR.
    if not any(name in os.environ for name in _CA_VARIABLES):
        for name in _CA_VARIABLES:
            value = values.get(name)
            if value is not None:
                os.environ[name] = value
