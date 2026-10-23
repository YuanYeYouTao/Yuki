"""Plugin API version and stable feature identifiers."""

from __future__ import annotations

import re

PLUGIN_API_VERSION = "8.0"
_API_VERSION = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")

DEFAULT_FEATURES: frozenset[str] = frozenset(
    {
        "message.normalized.v1",
        "message.history.v2",
        "message.current.mentions.v2",
        "prompt.fragment.v1",
        "admission.signal.v1",
        "plugin.agent_session.v2",
        "notification.facade.v1",
        "media.artifact.v1",
        "http.credential.v1",
    }
)


def is_api_compatible(requested: str, host: str = PLUGIN_API_VERSION) -> bool:
    """A Host satisfies earlier additive contracts in its own major version."""
    wanted = _API_VERSION.fullmatch(requested.strip())
    available = _API_VERSION.fullmatch(host.strip())
    return bool(
        wanted and available and wanted[1] == available[1] and int(wanted[2]) <= int(available[2])
    )
