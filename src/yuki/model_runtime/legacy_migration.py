"""One-time local migration through the original bundle CAS writer, without an Agent."""

from __future__ import annotations

import asyncio
import copy
import json
import tomllib
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any

from pydantic import ConfigDict, TypeAdapter, ValidationError
from sqlalchemy import and_, delete, or_, select
from sqlalchemy.exc import SQLAlchemyError

from yuki.admin.config_files import ConfigFileError, ConfigFileService
from yuki.admin.config_service import RuntimeConfigService
from yuki.config import Settings
from yuki.http_environment import load_http_environment
from yuki.model_runtime.connections import (
    ASRConnection,
    EmbeddingConnection,
    SearchConnection,
    VisionConnection,
)
from yuki.model_runtime.models import ModelProfile
from yuki.model_runtime.secrets import read_model_secrets
from yuki.persistence.database import Database
from yuki.persistence.instance_lock import ApplicationAlreadyActiveError, SQLiteApplicationLock
from yuki.persistence.models import RuntimeConfigOverrideModel

_PARAMETERS = {
    "llm.temperature": "default_temperature",
    "llm.max_output_tokens": "default_max_output_tokens",
    "llm.thinking_enabled": "thinking_enabled",
}
_AGENT_ROUTES = ("chat_agent", "plugin_agent_session")


class LegacyMigrationError(Exception):
    def __init__(self, category: str, conflicts: list[dict[str, Any]] | None = None) -> None:
        self.category = category
        self.conflicts = conflicts or []
        super().__init__(category)


@dataclass(frozen=True)
class _LegacyOverride:
    id: int
    version: int
    key: str
    scope: str
    value: Any


async def _legacy_inputs(
    settings: Settings, database: Database
) -> tuple[Settings, list[_LegacyOverride], list[_LegacyOverride]]:
    runtime = RuntimeConfigService(settings=settings, database=database)
    await runtime.initialize()
    startup = await runtime.startup_settings_updates()
    async with database.read_session() as reader:
        rows = await reader.scalars(
            select(RuntimeConfigOverrideModel).where(
                RuntimeConfigOverrideModel.config_key.in_((*_PARAMETERS, "llm.model"))
            )
        )
        facts = []
        for row in rows:
            try:
                value = json.loads(row.value_json)
            except ValueError:
                raise LegacyMigrationError("invalid_legacy_override") from None
            facts.append(
                _LegacyOverride(row.id, row.version, row.config_key, row.scope_type, value)
            )
    # NULL did not submit an explicit parameter; its retired current row can still be cleared.
    overrides = [row for row in facts if row.value is not None]
    conflicts = Counter((row.key, row.scope) for row in overrides if row.scope != "global")
    if conflicts:
        raise LegacyMigrationError(
            "scoped_model_overrides",
            [
                {"key": key, "scope": scope, "count": count}
                for (key, scope), count in sorted(conflicts.items())
            ],
        )
    for override in overrides:
        if override.key in _PARAMETERS:
            field = ModelProfile.model_fields[_PARAMETERS[override.key]]
            TypeAdapter(field.rebuild_annotation(), config=ConfigDict(strict=True)).validate_python(
                override.value
            )
        else:
            startup["llm_model"] = override.value
    return Settings(_env_file=None, **{**settings.model_dump(), **startup}), overrides, facts


async def _retire_overrides(database: Database, facts: list[_LegacyOverride]) -> None:
    async with database.immediate_session() as writer:
        retired = await writer.scalars(
            delete(RuntimeConfigOverrideModel)
            .where(
                or_(
                    *(
                        and_(
                            RuntimeConfigOverrideModel.id == fact.id,
                            RuntimeConfigOverrideModel.version == fact.version,
                            RuntimeConfigOverrideModel.config_key == fact.key,
                            RuntimeConfigOverrideModel.scope_type == fact.scope,
                        )
                        for fact in facts
                    )
                )
            )
            .returning(RuntimeConfigOverrideModel.id)
        )
        if len(retired.all()) != len(facts):
            # Roll back the short writer; the separately saved bundle remains a known fact.
            raise LegacyMigrationError("legacy_changed")


def _migrate_parameters(document: dict[str, Any], overrides: list[_LegacyOverride]) -> list[str]:
    profiles, routes = document["profiles"], document["routes"]
    changed = []
    if any(row.key == "llm.model" for row in overrides):
        for name, profile in profiles.items():
            if profile.get("model_env") == "LLM_MODEL":
                profile["model"] = next(row.value for row in overrides if row.key == "llm.model")
                del profile["model_env"]
                changed.append(name)
    parameters = {_PARAMETERS[row.key]: row.value for row in overrides if row.key in _PARAMETERS}
    by_profile: dict[str, list[str]] = defaultdict(list)
    for route in _AGENT_ROUTES:
        by_profile[routes[route]].append(route)
    for name, agent_routes in by_profile.items():
        original = profiles[name]
        updates = {key: value for key, value in parameters.items() if original.get(key) != value}
        if not updates:
            continue
        shared = any(
            target == name and route not in _AGENT_ROUTES for route, target in routes.items()
        )
        shared = (
            shared
            or document.get("search_connection") == name
            or any(profile.get("search_connection") == name for profile in profiles.values())
        )
        target = name
        if shared:
            target = f"{name}.legacy-agent"
            suffix = 2
            while target in profiles:
                target = f"{name}.legacy-agent-{suffix}"
                suffix += 1
            profiles[target] = copy.deepcopy(original)
            for route in agent_routes:
                routes[route] = target
        profiles[target].update(updates)
        changed.append(target)
    return changed


async def migrate_legacy(
    settings: Settings, expected_revision: int | None = None
) -> dict[str, Any]:
    lock = SQLiteApplicationLock(settings.sqlite_path)
    database = Database(settings.database_url)
    try:
        lock.acquire()
        active, overrides, facts = await _legacy_inputs(settings, database)
        files = ConfigFileService(active)
        current = await files.read()
        if expected_revision is None:
            expected_revision = current["revision"]
        if expected_revision != current["revision"]:
            raise ConfigFileError("version_conflict")
        if not current["valid"]:
            raise ConfigFileError("validation_error")
        document = current["document"]
        raw = tomllib.loads(
            await asyncio.to_thread(active.model_profiles_file.read_text, encoding="utf-8")
        )
        # Preserve declarations (including capability ordering) outside the migrated fields.
        # The shared writer restores private headers from the original server-side file.
        for name, profile in raw["profiles"].items():
            document["profiles"][name] = {
                key: value for key, value in profile.items() if key != "headers"
            }
        _, keys = await asyncio.to_thread(read_model_secrets, active.model_profiles_file)
        updates: dict[str, str] = {}

        def alias(name: str, key: str) -> str:
            if not key:
                return ""
            candidate = f"YUKI_LEGACY_{name.upper()}_KEY"
            suffix = 2
            while candidate in keys and keys[candidate] != key:
                candidate = f"YUKI_LEGACY_{name.upper()}_KEY_{suffix}"
                suffix += 1
            if candidate not in keys:
                keys[candidate] = key
                updates[candidate] = key
            return candidate

        migrated = []
        if "embedding" not in document:
            document["embedding"] = EmbeddingConnection(
                enabled=active.memory_embedding_configured,
                provider=active.memory_embedding_provider,
                base_url=active.memory_embedding_base_url,
                model=active.memory_embedding_model,
                api_key_env=alias("embedding", active.memory_embedding_api_key),
                dimensions=active.memory_embedding_dimensions,
                output_type=active.memory_embedding_output_type,
                document_template_version=active.memory_embedding_document_template_version,
                query_instruct=active.memory_embedding_query_instruct,
            ).model_dump(mode="json")
            migrated.append("embedding")
        if "vision" not in document:
            if active.vision_enabled and active.vision_provider != "qwen":
                raise LegacyMigrationError("unsupported_legacy_vision_provider")
            document["vision"] = VisionConnection(
                enabled=active.vision_enabled,
                base_url=active.vision_base_url,
                model=active.vision_model,
                api_key_env=alias("vision", active.vision_api_key),
            ).model_dump(mode="json")
            migrated.append("vision")
        if "asr" not in document:
            base_url, key = active.asr_credentials
            document["asr"] = ASRConnection(
                enabled=bool(active.asr_enabled and base_url),
                base_url=base_url,
                model=active.asr_model,
                protocol=active.asr_protocol,
                api_key_env=alias("asr", key),
            ).model_dump(mode="json")
            migrated.append("asr")
        if "search" not in document:
            document["search"] = SearchConnection(
                tavily_api_key_env=alias("search", active.tavily_api_key)
            ).model_dump(mode="json")
            migrated.append("search")
        profiles = _migrate_parameters(document, overrides)
        revision = current["revision"]
        if migrated or profiles or facts:
            revision = await files.save(
                expected_revision, {"document": document, "api_keys": updates}
            )
        result = {
            "ok": True,
            "revision": revision,
            "migrated": migrated,
            "migrated_profiles": profiles,
            "apply_mode": "restart",
        }
        if facts:
            try:
                await _retire_overrides(database, facts)
            except LegacyMigrationError as exc:
                return {**result, "ok": False, "partial": True, "error_category": exc.category}
            except (SQLAlchemyError, OSError):
                return {
                    **result,
                    "ok": False,
                    "partial": True,
                    "error_category": "legacy_retirement_unconfirmed",
                }
        return {**result, "retired_overrides": len(facts)}
    finally:
        try:
            await database.close()
        finally:
            lock.release()


async def run_migration_command(revision: int | None) -> int:
    try:
        load_http_environment()
        result = await migrate_legacy(Settings(), revision)
    except LegacyMigrationError as exc:
        result = {"ok": False, "error_category": exc.category, "conflicts": exc.conflicts}
    except ConfigFileError as exc:
        result = {"ok": False, "error_category": exc.category}
    except ApplicationAlreadyActiveError:
        result = {"ok": False, "error_category": "application_active"}
    except (ValueError, ValidationError):
        result = {"ok": False, "error_category": "validation_error"}
    except (SQLAlchemyError, OSError):
        result = {"ok": False, "error_category": "operation_unavailable"}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["ok"] else 1
