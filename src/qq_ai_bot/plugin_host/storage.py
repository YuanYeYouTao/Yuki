"""Permission-bound plugin KV storage with optimistic update retries."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterable, Mapping

from qq_ai_bot.plugin_host.repository import (
    PluginStateRepository,
    PluginVersionConflictError,
)
from yuki_plugin_sdk.errors import PluginPermissionError
from yuki_plugin_sdk.models import JsonValue
from yuki_plugin_sdk.permissions import PluginPermission


class BoundStorageFacade:
    """Expose only the caller plugin's own namespace.

    Writes are plugin-global: they do not stamp subject_user_id or isolate
    storage by Person. Person-owned rows are a repository-path contract.
    """

    def __init__(
        self,
        *,
        repository: PluginStateRepository,
        plugin_id: str,
        approved_permissions: Iterable[PluginPermission],
        storage_mb: int = 10,
    ) -> None:
        if isinstance(storage_mb, bool) or not isinstance(storage_mb, int):
            raise ValueError("plugin storage limit must be an integer from 1 to 10240 MB")
        if not 1 <= storage_mb <= 10_240:
            raise ValueError("plugin storage limit must be an integer from 1 to 10240 MB")
        self._repository = repository
        self._plugin_id = plugin_id
        self._permissions = frozenset(approved_permissions)
        self._capacity_bytes = storage_mb * 1024 * 1024
        self._write_lock = asyncio.Lock()

    async def get(self, namespace: str, key: str) -> JsonValue:
        self._require()
        row = await self._repository.get(
            plugin_id=self._plugin_id,
            namespace=namespace,
            key=key,
        )
        return _json_value(row.value) if row is not None else None

    async def set(self, namespace: str, key: str, value: JsonValue) -> None:
        self._require()
        async with self._write_lock:
            for _ in range(3):
                row = await self._repository.get(
                    plugin_id=self._plugin_id,
                    namespace=namespace,
                    key=key,
                )
                await self._require_capacity(
                    current_size=_serialized_size(row.value) if row is not None else 0,
                    value=value,
                )
                try:
                    await self._repository.compare_and_set(
                        plugin_id=self._plugin_id,
                        namespace=namespace,
                        key=key,
                        expected_version=row.version if row is not None else 0,
                        value=value,
                    )
                    return
                except PluginVersionConflictError:
                    continue
        raise PluginVersionConflictError("plugin state changed repeatedly")

    async def delete(self, namespace: str, key: str) -> bool:
        self._require()
        return await self._repository.delete(
            plugin_id=self._plugin_id,
            namespace=namespace,
            key=key,
        )

    async def list(self, namespace: str) -> Mapping[str, JsonValue]:
        self._require()
        rows = await self._repository.list_namespace(
            plugin_id=self._plugin_id,
            namespace=namespace,
        )
        return {row.key: _json_value(row.value) for row in rows}

    async def compare_and_set(
        self,
        namespace: str,
        key: str,
        expected: JsonValue,
        value: JsonValue,
    ) -> bool:
        self._require()
        async with self._write_lock:
            row = await self._repository.get(
                plugin_id=self._plugin_id,
                namespace=namespace,
                key=key,
            )
            current = _json_value(row.value) if row is not None else None
            if current != expected:
                return False
            await self._require_capacity(
                current_size=_serialized_size(row.value) if row is not None else 0,
                value=value,
            )
            try:
                await self._repository.compare_and_set(
                    plugin_id=self._plugin_id,
                    namespace=namespace,
                    key=key,
                    expected_version=row.version if row is not None else 0,
                    value=value,
                )
            except PluginVersionConflictError:
                return False
            return True

    async def _require_capacity(self, *, current_size: int, value: JsonValue) -> None:
        usage = await self._repository.storage_usage_bytes(plugin_id=self._plugin_id)
        projected = usage - current_size + _serialized_size(value)
        if projected > self._capacity_bytes:
            raise PluginPermissionError("plugin storage capacity exceeded")

    def _require(self) -> None:
        if PluginPermission.STORAGE_PRIVATE not in self._permissions:
            raise PluginPermissionError("plugin lacks storage.private permission")


def _json_value(value: object) -> JsonValue:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    raise TypeError("stored plugin value is not JSON-compatible")


def _serialized_size(value: object) -> int:
    payload = json.dumps(
        _json_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return len(payload.encode("utf-8"))


__all__ = ["BoundStorageFacade"]
