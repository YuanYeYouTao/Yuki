"""Schema-validated, scope-bound plugin configuration Facade."""

from __future__ import annotations

from collections.abc import Iterable

from pydantic import BaseModel, TypeAdapter, ValidationError

from yuki.plugin_host.repository import (
    PluginConfigRepository,
    PluginVersionConflictError,
)
from yuki_plugin_sdk.errors import PluginPermissionError
from yuki_plugin_sdk.models import JsonValue
from yuki_plugin_sdk.permissions import PluginPermission


class BoundConfigFacade:
    def __init__(
        self,
        *,
        repository: PluginConfigRepository,
        plugin_id: str,
        approved_permissions: Iterable[PluginPermission],
        schema: type[BaseModel] | None = None,
        current_user_id: str | None = None,
        current_group_id: str | None = None,
    ) -> None:
        self._repository = repository
        self._plugin_id = plugin_id
        self._permissions = frozenset(approved_permissions)
        self._schema = schema
        self._current_user_id = current_user_id
        self._current_group_id = current_group_id

    async def get(
        self,
        key: str,
        *,
        scope_type: str = "global",
        scope_id: str = "",
    ) -> JsonValue:
        self._require(PluginPermission.PLUGIN_CONFIG_READ)
        resolved = self._scope(scope_type, scope_id)
        row = await self._repository.get(
            plugin_id=self._plugin_id,
            scope_type=scope_type,
            scope_id=resolved,
            key=key,
        )
        return _json_value(row.value) if row is not None else None

    async def set(
        self,
        key: str,
        value: JsonValue,
        *,
        scope_type: str = "global",
        scope_id: str = "",
    ) -> None:
        self._require(PluginPermission.PLUGIN_CONFIG_WRITE)
        resolved = self._scope(scope_type, scope_id)
        value = self._validate(key, value)
        for _ in range(3):
            row = await self._repository.get(
                plugin_id=self._plugin_id,
                scope_type=scope_type,
                scope_id=resolved,
                key=key,
            )
            try:
                await self._repository.compare_and_set(
                    plugin_id=self._plugin_id,
                    scope_type=scope_type,
                    scope_id=resolved,
                    key=key,
                    expected_version=row.version if row is not None else 0,
                    value=value,
                )
                return
            except PluginVersionConflictError:
                continue
        raise PluginVersionConflictError("plugin config changed repeatedly")

    def _validate(self, key: str, value: JsonValue) -> JsonValue:
        if self._schema is None:
            return value
        field = self._schema.model_fields.get(key)
        if field is None:
            raise ValueError(f"unknown plugin config key: {key}")
        try:
            validated: object = TypeAdapter(field.annotation).validate_python(value)
        except ValidationError as exc:
            raise ValueError(f"invalid plugin config value for {key}") from exc
        return _json_value(validated)

    def _scope(self, scope_type: str, scope_id: str) -> str:
        if scope_type == "global":
            if scope_id:
                raise PluginPermissionError("global plugin config cannot have scope_id")
            return ""
        if scope_type == "user":
            expected = self._current_user_id
        elif scope_type == "group":
            expected = self._current_group_id
        else:
            raise ValueError("plugin config scope must be global, user, or group")
        if expected is None or (scope_id and scope_id != expected):
            raise PluginPermissionError("plugin config scope is outside the current turn")
        return expected

    def _require(self, permission: PluginPermission) -> None:
        if permission not in self._permissions:
            raise PluginPermissionError(f"plugin lacks {permission.value} permission")


def _json_value(value: object) -> JsonValue:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, BaseModel):
        return _json_value(value.model_dump(mode="json"))
    if isinstance(value, list | tuple | set | frozenset):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    raise TypeError("plugin config value is not JSON-compatible")


__all__ = ["BoundConfigFacade"]
