"""Filesystem-only plugin discovery; discovery never imports plugin Python code."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from yuki.plugin_host.manifest import PluginManifest, load_manifest
from yuki.plugin_host.models import PluginDiscoveryRecord, PluginStatus
from yuki_plugin_sdk.api import PLUGIN_API_VERSION
from yuki_plugin_sdk.errors import ManifestValidationError


@dataclass(frozen=True, slots=True)
class DiscoveredPlugin:
    record: PluginDiscoveryRecord
    manifest: PluginManifest | None = None


class PluginDiscovery:
    def __init__(
        self,
        directory: Path,
        *,
        yuki_version: str,
        plugin_api: str = PLUGIN_API_VERSION,
        enabled: bool = True,
    ) -> None:
        self._directory = directory
        self._yuki_version = yuki_version
        self._plugin_api = plugin_api
        self._enabled = enabled

    def discover(self) -> tuple[DiscoveredPlugin, ...]:
        if not self._enabled or not self._directory.exists():
            return ()
        if not self._directory.is_dir():
            return (
                DiscoveredPlugin(
                    PluginDiscoveryRecord(
                        directory=self._directory,
                        status=PluginStatus.INVALID,
                        error_category="plugin_directory_invalid",
                        detail="插件目录不是文件夹",
                    )
                ),
            )
        found: list[DiscoveredPlugin] = []
        seen: set[str] = set()
        for child in sorted(self._directory.iterdir(), key=lambda item: item.name):
            if not child.is_dir() or child.name.startswith("."):
                continue
            if not (child / "plugin.toml").is_file():
                continue
            try:
                manifest = load_manifest(
                    child,
                    yuki_version=self._yuki_version,
                    host_plugin_api=self._plugin_api,
                    expected_directory_name=child.name,
                )
                if manifest.id in seen:
                    raise ManifestValidationError("duplicate plugin id")
                seen.add(manifest.id)
            except ManifestValidationError as exc:
                found.append(
                    DiscoveredPlugin(
                        PluginDiscoveryRecord(
                            directory=child,
                            status=PluginStatus.INVALID,
                            error_category="manifest_invalid",
                            detail=str(exc)[:500],
                        )
                    )
                )
                continue
            found.append(
                DiscoveredPlugin(
                    PluginDiscoveryRecord(
                        directory=child,
                        plugin_id=manifest.id,
                        status=PluginStatus.DISCOVERED,
                    ),
                    manifest,
                )
            )
        return tuple(found)
