"""Environment-only plugin secrets; values are never enumerable or persisted."""

from __future__ import annotations

import os
import re
from collections.abc import Iterable

from yuki_plugin_sdk.errors import PluginPermissionError

_SECRET_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")


class BoundSecretsFacade:
    def __init__(self, *, plugin_id: str, declared_names: Iterable[str]) -> None:
        self._plugin_id = plugin_id
        self._declared = frozenset(_validated_name(name) for name in declared_names)

    def configured(self, name: str) -> bool:
        normalized = self._require_declared(name)
        return bool(os.getenv(self._environment_name(normalized), ""))

    def get(self, name: str) -> str:
        normalized = self._require_declared(name)
        value = os.getenv(self._environment_name(normalized), "")
        if not value:
            raise PluginPermissionError("declared plugin secret is not configured")
        return value

    def _require_declared(self, name: str) -> str:
        normalized = _validated_name(name)
        if normalized not in self._declared:
            raise PluginPermissionError("plugin secret was not declared")
        return normalized

    def _environment_name(self, name: str) -> str:
        plugin = re.sub(r"[^A-Z0-9]", "_", self._plugin_id.upper())
        return f"YUKI_PLUGIN__{plugin}__{name}"


def _validated_name(value: str) -> str:
    normalized = value.strip().upper()
    if _SECRET_NAME.fullmatch(normalized) is None:
        raise ValueError("plugin secret name must use uppercase letters, digits, and underscore")
    return normalized


__all__ = ["BoundSecretsFacade"]
