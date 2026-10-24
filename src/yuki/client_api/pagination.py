"""Opaque keyset cursors for the existing personal resources."""

import base64
import json
from typing import Any

from yuki.client_api.errors import ClientError


def cursor(value: Any) -> str:
    return (
        base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode())
        .decode()
        .rstrip("=")
    )


def decode_cursor(value: str) -> Any:
    try:
        return json.loads(
            base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
        )
    except (ValueError, TypeError):
        raise ClientError("validation_error") from None
