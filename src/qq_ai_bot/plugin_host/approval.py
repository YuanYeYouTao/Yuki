"""Manifest-hash-bound plugin permission approval contracts."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol

from qq_ai_bot.plugin_host.manifest import PluginManifest
from qq_ai_bot.plugin_host.models import PluginApprovalRecord
from yuki_plugin_sdk.permissions import PluginPermission


class PluginApprovalStore(Protocol):
    async def get(self, plugin_id: str) -> PluginApprovalRecord | None: ...

    async def save(self, record: PluginApprovalRecord) -> None: ...

    async def delete(self, plugin_id: str) -> bool: ...


class InMemoryApprovalStore:
    """Small deterministic store for Host tests and SDK contract tests."""

    def __init__(self) -> None:
        self._records: dict[str, PluginApprovalRecord] = {}

    async def get(self, plugin_id: str) -> PluginApprovalRecord | None:
        return self._records.get(plugin_id)

    async def save(self, record: PluginApprovalRecord) -> None:
        self._records[record.plugin_id] = record

    async def delete(self, plugin_id: str) -> bool:
        return self._records.pop(plugin_id, None) is not None


class PluginApprovalService:
    def __init__(self, store: PluginApprovalStore) -> None:
        self._store = store

    async def approve(
        self,
        manifest: PluginManifest,
        *,
        approved_by: str,
        permissions: tuple[PluginPermission, ...] | None = None,
    ) -> PluginApprovalRecord:
        selected = permissions if permissions is not None else manifest.permissions
        if len(set(selected)) != len(selected):
            raise ValueError("approved permissions cannot contain duplicates")
        unexpected = set(selected) - set(manifest.permissions)
        if unexpected:
            names = ", ".join(sorted(item.value for item in unexpected))
            raise ValueError(f"cannot approve permissions not requested by manifest: {names}")
        record = PluginApprovalRecord(
            plugin_id=manifest.id,
            manifest_hash=manifest.manifest_hash,
            approved_permissions=selected,
            approved_by=approved_by,
            approved_at=datetime.now(UTC),
        )
        await self._store.save(record)
        return record

    async def revoke(self, plugin_id: str) -> bool:
        return await self._store.delete(plugin_id)
