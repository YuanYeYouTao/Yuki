"""Explicit operator and /ai capability IDs; no UI catalog or implied grants."""

from __future__ import annotations

import re
from typing import Final

from yuki.admin.authority.tokens import MAX_CAPABILITY_ID_LENGTH

_CAPABILITY_TOKEN = re.compile(r"\A[a-z][a-z0-9_]*(?:[.:][a-z][a-z0-9_]*)*\Z")
DENIED_CAPABILITY_ID: Final[str] = "control.denied"
# Existing declarations remain parseable; only live execution consumers grant effects.
CONTROL_CAPABILITY_IDS: Final[frozenset[str]] = frozenset(
    {
        "control.audit.read",
        "control.automation.content.read",
        "control.automation.mutate",
        "control.automation.read",
        "control.chat.content.read",
        "control.chat.metadata.read",
        "control.config.file.content.read",
        "control.config.file.mutate",
        "control.config.mutate",
        "control.config.read",
        "control.emoji.mutate",
        "control.emoji.read",
        "control.environment.file.mutate",
        "control.execution.content.read",
        "control.execution.metadata.read",
        "control.group.mutate",
        "control.health.read",
        "control.memory.content.read",
        "control.memory.dream",
        "control.memory.maintenance",
        "control.memory.metadata.read",
        "control.memory.mutate",
        "control.memory.rebuild",
        "control.operation.cancel",
        "control.operation.read",
        "control.operation.retry",
        "control.plugin.config.content.read",
        "control.plugin.config.mutate",
        "control.plugin.mutate",
        "control.plugin.read",
        "control.preference.mutate",
        "control.preference.read",
        "control.private_access.mutate",
        "control.system.read",
        "control.terminal.content.read",
        "control.terminal.mutate",
        "control.work.mutate",
        "control.work.read",
        "control.workspace.content.read",
        "control.workspace.metadata.read",
        "conversation.metadata.read",
        "identity.binding.attach",
        "identity.binding.disable",
        "identity.binding.enable",
        "identity.binding.read",
        "identity.binding.read_external",
        "identity.binding.transfer",
        "identity.client.pair",
        "identity.client.revoke",
        "identity.membership.read",
        "identity.person.create",
        "identity.person.disable",
        "identity.person.enable",
        "identity.person.superuser",
        "identity.person.forget",
        "identity.person.read",
        "identity.person.merge",
        "identity.person.rename",
        "identity.presence.read",
        "identity.presence.register",
        "identity.presence.set_ingest",
        "identity.presence.start",
        "identity.presence.stop",
        "identity.space.binding.attach",
        "identity.space.disable",
        "identity.space.enable",
        "identity.space.read",
        "identity.space.require_mention",
        "route.pause",
        "route.read",
        "route.reconcile",
        "route.resume",
        "route.set",
        "web_search",
    }
)


def normalize_capability_id(value: object) -> str:
    if type(value) is not str:
        raise TypeError("capability id must be a str")
    token = value.strip().casefold()
    if len(token) > MAX_CAPABILITY_ID_LENGTH:
        raise ValueError("illegal capability id")
    if not token or _CAPABILITY_TOKEN.fullmatch(token) is None:
        raise ValueError("illegal capability id")
    return token


def is_protocol_capability(capability_id: str) -> bool:
    return capability_id.strip().casefold() in CONTROL_CAPABILITY_IDS
