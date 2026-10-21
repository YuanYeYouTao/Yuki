"""Host-owned direct prefixes resolved to existing deterministic plugin commands."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from yuki.plugin_host.extension_registry import ExtensionKind, ExtensionRegistry
from yuki.settings_domains import validate_direct_command_bindings


class RunningPlugins(Protocol):
    @property
    def running_plugin_ids(self) -> tuple[str, ...]: ...


@dataclass(frozen=True, slots=True)
class DirectCommandMatch:
    prefix: str
    plugin_id: str
    command_name: str
    arguments: str
    active: bool
    reason: str


@dataclass(frozen=True, slots=True)
class DirectCommandDiagnostic:
    prefix: str
    plugin_id: str
    command_name: str
    active: bool
    reason: str


class DirectCommandRouter:
    """Match static configuration; execution remains in PluginCommandAdapter."""

    def __init__(
        self,
        *,
        bindings: Mapping[str, str],
        registry: ExtensionRegistry,
        manager: RunningPlugins,
    ) -> None:
        validated = validate_direct_command_bindings(dict(bindings))
        self._bindings = tuple(
            (prefix, *target.rsplit(":", 1)) for prefix, target in validated.items()
        )
        self._registry = registry
        self._manager = manager

    def match(self, text: str) -> DirectCommandMatch | None:
        stripped = text.strip()
        for prefix, plugin_id, command_name in self._bindings:
            if stripped.startswith(prefix):
                active, reason = self._status(plugin_id, command_name)
                return DirectCommandMatch(
                    prefix=prefix,
                    plugin_id=plugin_id,
                    command_name=command_name,
                    arguments=stripped[len(prefix) :].strip(),
                    active=active,
                    reason=reason,
                )
        return None

    def diagnostics(self, *, plugin_id: str | None = None) -> tuple[DirectCommandDiagnostic, ...]:
        rows: list[DirectCommandDiagnostic] = []
        for prefix, owner, command_name in self._bindings:
            if plugin_id is not None and owner != plugin_id:
                continue
            active, reason = self._status(owner, command_name)
            rows.append(DirectCommandDiagnostic(prefix, owner, command_name, active, reason))
        return tuple(rows)

    def _status(self, plugin_id: str, command_name: str) -> tuple[bool, str]:
        if plugin_id not in self._manager.running_plugin_ids:
            return False, "plugin_not_running"
        item = self._registry.get(f"{plugin_id}:{command_name}")
        if item is None or item.kind is not ExtensionKind.COMMAND:
            return False, "command_not_registered"
        return True, "active"


__all__ = [
    "DirectCommandDiagnostic",
    "DirectCommandMatch",
    "DirectCommandRouter",
]
