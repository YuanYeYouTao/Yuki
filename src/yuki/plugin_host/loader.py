"""Trusted in-process plugin entrypoint loader with module-name isolation."""

from __future__ import annotations

import hashlib
import importlib.util
import inspect
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

from yuki.plugin_host.manifest import PluginManifest
from yuki_plugin_sdk.errors import PluginLifecycleError
from yuki_plugin_sdk.plugin import Plugin


@dataclass(frozen=True, slots=True)
class LoadedPlugin:
    manifest: PluginManifest
    root: Path
    module_name: str
    module: ModuleType
    instance: Plugin


class PluginLoader:
    def load(self, root: Path, manifest: PluginManifest) -> LoadedPlugin:
        resolved_root = root.resolve()
        module_path, symbol = manifest.entrypoint_parts
        relative = Path(*module_path.split("."))
        file_path = (resolved_root / relative).with_suffix(".py")
        package_path = resolved_root / relative / "__init__.py"
        is_package = False
        if package_path.is_file():
            file_path = package_path
            is_package = True
        if not file_path.is_file() or not file_path.resolve().is_relative_to(resolved_root):
            raise PluginLifecycleError("plugin entrypoint module was not found")
        digest = hashlib.sha256(f"{manifest.id}:{manifest.manifest_hash}".encode()).hexdigest()[:12]
        isolated_name = f"_yuki_plugin_{digest}"
        spec = importlib.util.spec_from_file_location(
            isolated_name,
            file_path,
            submodule_search_locations=[str(file_path.parent)] if is_package else None,
        )
        if spec is None or spec.loader is None:
            raise PluginLifecycleError("plugin entrypoint could not be loaded")
        module = importlib.util.module_from_spec(spec)
        sys.modules[isolated_name] = module
        try:
            spec.loader.exec_module(module)
            target = getattr(module, symbol)
            instance = target()
        except Exception as exc:
            sys.modules.pop(isolated_name, None)
            raise PluginLifecycleError("plugin entrypoint initialization failed") from exc
        if not isinstance(instance, Plugin) or not all(
            inspect.iscoroutinefunction(getattr(instance, name, None))
            for name in ("register", "start", "stop")
        ):
            sys.modules.pop(isolated_name, None)
            raise PluginLifecycleError("entrypoint does not implement async Plugin lifecycle")
        return LoadedPlugin(
            manifest=manifest,
            root=resolved_root,
            module_name=isolated_name,
            module=module,
            instance=instance,
        )

    @staticmethod
    def unload(plugin: LoadedPlugin) -> None:
        sys.modules.pop(plugin.module_name, None)
