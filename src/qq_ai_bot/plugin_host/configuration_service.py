"""Atomic, schema-validated management of original canonical plugin config rows."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ValidationError
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.unit_of_work import next_updated_at, state_revision
from qq_ai_bot.plugin_host.db_models import PluginConfigValueModel, PluginInstallationModel
from qq_ai_bot.plugin_host.ownership import (
    PluginOwnershipError,
    require_config_readable,
    require_live_person,
    require_live_space,
)

MAX_BYTES = 256 * 1024


class PluginConfigurationError(ValueError):
    def __init__(self, category: str) -> None:
        self.category = category
        super().__init__(category)


def _json(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True)
    if len(encoded.encode("utf-8")) > MAX_BYTES:
        raise PluginConfigurationError("validation_error")
    return encoded


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_plain(item) for item in value]
    return value


class PluginConfigurationService:
    def __init__(self, database: Database, schema: Callable[[str], type[BaseModel] | None]) -> None:
        self._database = database
        self._schema = schema

    @staticmethod
    def _scope(scope_type: str, owner_id: str | None) -> None:
        if type(scope_type) is not str or scope_type not in {"global", "user", "group"}:
            raise PluginConfigurationError("validation_error")
        if scope_type == "global":
            if owner_id is not None:
                raise PluginConfigurationError("validation_error")
        elif type(owner_id) is not str:
            raise PluginConfigurationError("validation_error")
        else:
            try:
                if str(UUID(owner_id)) != owner_id:
                    raise ValueError("invalid owner")
            except ValueError as exc:
                raise PluginConfigurationError("validation_error") from exc

    async def _read(
        self, session: AsyncSession, plugin_id: str, scope_type: str, owner_id: str | None
    ) -> tuple[int, list[PluginConfigValueModel], PluginInstallationModel]:
        self._scope(scope_type, owner_id)
        installation = await session.get(PluginInstallationModel, plugin_id)
        if installation is None:
            raise PluginConfigurationError("not_found")
        try:
            if scope_type == "user":
                await require_live_person(session, owner_id)
            elif scope_type == "group":
                await require_live_space(session, owner_id)
            stmt = select(PluginConfigValueModel).where(
                PluginConfigValueModel.plugin_id == plugin_id,
                PluginConfigValueModel.scope_type == scope_type,
            )
            if scope_type == "user":
                stmt = stmt.where(PluginConfigValueModel.canonical_person_id == owner_id)
            elif scope_type == "group":
                stmt = stmt.where(PluginConfigValueModel.canonical_space_id == owner_id)
            rows = list(
                (await session.scalars(stmt.order_by(PluginConfigValueModel.key).limit(257))).all()
            )
            if (
                len(rows) > 256
                or sum(len(row.value_json.encode("utf-8")) for row in rows) > MAX_BYTES
            ):
                raise PluginConfigurationError("state_mismatch")
            for row in rows:
                await require_config_readable(session, row)
        except PluginOwnershipError as exc:
            raise PluginConfigurationError("state_mismatch") from exc
        return (
            self._revision(plugin_id, scope_type, owner_id, installation, rows),
            rows,
            installation,
        )

    @staticmethod
    def _revision(
        plugin_id: str,
        scope_type: str,
        owner_id: str | None,
        installation: PluginInstallationModel,
        rows: list[PluginConfigValueModel],
    ) -> int:
        material = [
            plugin_id,
            scope_type,
            owner_id,
            installation.manifest_hash,
            state_revision(installation.updated_at),
            [
                (row.id, row.key, row.version, row.value_json)
                for row in sorted(rows, key=lambda row: row.key)
            ],
        ]
        # Values are bounded separately. Hash framing must not halve their byte
        # budget by JSON-escaping the already serialized original row values.
        encoded = json.dumps(material, ensure_ascii=False, allow_nan=False, sort_keys=True)
        revision = int(hashlib.sha256(encoded.encode()).hexdigest()[:13], 16) + 1
        return revision

    def _require_schema(self, plugin_id: str) -> type[BaseModel]:
        if type(plugin_id) is not str or not plugin_id or len(plugin_id) > 128:
            raise PluginConfigurationError("validation_error")
        schema = self._schema(plugin_id)
        if schema is None:
            raise PluginConfigurationError("operation_unavailable")
        if len(schema.model_fields) > 256:
            raise PluginConfigurationError("operation_unavailable")
        return schema

    async def read(
        self, plugin_id: str, *, scope_type: str = "global", owner_id: str | None = None
    ) -> dict[str, Any]:
        schema = self._require_schema(plugin_id)
        async with self._database.sessions() as session:
            await session.execute(text("BEGIN"))
            revision, rows, _ = await self._read(session, plugin_id, scope_type, owner_id)
            try:
                raw = {
                    row.key: json.loads(row.value_json)
                    for row in rows
                    if row.key in schema.model_fields
                }
            except (TypeError, ValueError) as exc:
                raise PluginConfigurationError("state_mismatch") from exc
        valid = all(row.key in schema.model_fields for row in rows)
        try:
            values = schema.model_validate(raw).model_dump(mode="json")
        except ValidationError:
            values = raw
            valid = False
        if self._schema(plugin_id) is not schema:
            raise PluginConfigurationError("version_conflict")
        _json(values)
        document_schema = schema.model_json_schema()
        _json(document_schema)
        return {
            "plugin_id": plugin_id,
            "scope_type": scope_type,
            "owner_id": owner_id,
            "revision": revision,
            "schema": document_schema,
            "values": values,
            "valid": valid,
            "apply_mode": "plugin_defined",
        }

    async def save(self, plugin_id: str, expected_revision: int, spec: Mapping[str, Any]) -> int:
        if set(spec) != {"scope_type", "owner_id", "values"}:
            raise PluginConfigurationError("validation_error")
        scope_type, owner_id = spec["scope_type"], spec["owner_id"]
        self._scope(scope_type, owner_id)
        schema = self._require_schema(plugin_id)
        try:
            # Execute plugin validators before opening the write transaction.
            raw = _plain(spec["values"])
            if not isinstance(raw, dict) or set(raw) - set(schema.model_fields):
                raise ValueError("unknown config fields")
            _json(raw)
            values = schema.model_validate(raw).model_dump(mode="json")
            serialized = {key: _json(value) for key, value in values.items()}
            _json(values)
        except (TypeError, ValueError, ValidationError) as exc:
            raise PluginConfigurationError("validation_error") from exc
        async with self._database.sessions() as session, session.begin():
            await session.execute(text("BEGIN IMMEDIATE"))
            revision, rows, installation = await self._read(
                session, plugin_id, scope_type, owner_id
            )
            if revision != expected_revision or self._schema(plugin_id) is not schema:
                raise PluginConfigurationError("version_conflict")
            existing = {row.key: row for row in rows}
            # Ownership and all validation precede the first write/flush.
            now = datetime.now(UTC)
            saved_rows = []
            for key, content in serialized.items():
                row = existing.get(key)
                if row is None:
                    row = PluginConfigValueModel(
                        plugin_id=plugin_id,
                        scope_type=scope_type,
                        key=key,
                        value_json=content,
                        version=1,
                        updated_at=now,
                        canonical_person_id=owner_id if scope_type == "user" else None,
                        canonical_space_id=owner_id if scope_type == "group" else None,
                    )
                    session.add(row)
                else:
                    row.value_json = content
                    row.version += 1
                    row.updated_at = next_updated_at(row.updated_at, now)
                saved_rows.append(row)
            for key in existing.keys() - serialized.keys():
                await session.delete(existing[key])
            await session.flush()
            return self._revision(plugin_id, scope_type, owner_id, installation, saved_rows)
