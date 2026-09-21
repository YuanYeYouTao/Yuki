"""Plugin API version and stable feature identifiers."""

from __future__ import annotations

import re

PLUGIN_API_VERSION = "3.0"
_API_VERSION = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")

DEFAULT_FEATURES: frozenset[str] = frozenset(
    {
        "message.normalized.v1",
        "message.current.mentions.v1",
        "prompt.fragment.v1",
        "admission.signal.v1",
        "automation.action.v1",
        "plugin.agent_session.v1",
        "emoji.facade.v1",
        "emoji.selection_signals.v1",
        "speech.facade.v1",
        "speech.tts_provider.v1",
        "mcp.facade.v1",
        "notification.facade.v1",
        "media.artifact.v1",
        "http.credential.v1",
    }
)


def is_api_compatible(requested: str, host: str = PLUGIN_API_VERSION) -> bool:
    """Load only the exact SDK contract implemented by this Host."""

    requested = requested.strip()
    host = host.strip()
    return _API_VERSION.fullmatch(requested) is not None and requested == host
