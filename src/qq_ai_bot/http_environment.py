"""Load standard HTTP transport environment once at process startup."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import dotenv_values

_PROXY_VARIABLES = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")
_CA_VARIABLES = ("SSL_CERT_FILE", "SSL_CERT_DIR")


def load_http_environment(env_file: Path = Path(".env")) -> None:
    """Share dotenv proxy/CA settings without exporting unrelated Bot secrets."""

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
