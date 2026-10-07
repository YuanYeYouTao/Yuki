"""Finite startup files: safe snapshots, shared validation, optimistic atomic saves."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import stat
import tempfile
import tomllib
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

import tomlkit
from pydantic import TypeAdapter, ValidationError

from qq_ai_bot.config import Settings
from qq_ai_bot.model_runtime.executor import TaskModelExecutor
from qq_ai_bot.model_runtime.models import ModelProfile, ModelTask
from qq_ai_bot.model_runtime.pool import ModelClientPool
from qq_ai_bot.model_runtime.profiles import (
    ModelProfileCatalog,
    ModelRuntimeConfigurationError,
    model_profile_environment,
    parse_model_profile_catalog,
)
from qq_ai_bot.model_runtime.secrets import (
    KEY_ALIAS,
    encode_model_secrets,
    model_secrets_path,
    read_model_secrets,
)
from qq_ai_bot.services.participation_parameters import (
    DEFAULT_AUTONOMY_PARAMETERS,
    AutonomyParameters,
)
from qq_ai_bot.web.base import WebSearchProvider

if TYPE_CHECKING:
    from qq_ai_bot.application.modules.web import WebModule

MAX_CONFIG_BYTES = 256 * 1024
CONFIG_FILE_IDS = frozenset({"model_profiles", "system_prompt", "bot_persona", "autonomous_model"})
_PUBLIC_PROFILE_FIELDS: dict[str, TypeAdapter[Any]] = {
    name: TypeAdapter(field.annotation)
    for name, field in ModelProfile.model_fields.items()
    if name not in {"id", "headers"}
}


class ConfigFileError(Exception):
    def __init__(self, category: str) -> None:
        self.category = category
        super().__init__(category)


def _revision(content: bytes | None) -> int:
    # Transport revisions are integers, bounded by JavaScript's safe range.
    return 0 if content is None else int(hashlib.sha256(content).hexdigest()[:13], 16) + 1


def _read(path: Path) -> bytes | None:
    try:
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise ConfigFileError("precondition_failed")
        if before.st_size > MAX_CONFIG_BYTES:
            raise ConfigFileError("validation_error")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        with os.fdopen(os.open(path, flags), "rb") as stream:
            opened = os.fstat(stream.fileno())
            if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
                raise ConfigFileError("version_conflict")
            data = stream.read(MAX_CONFIG_BYTES + 1)
        if len(data) > MAX_CONFIG_BYTES:
            raise ConfigFileError("validation_error")
        return data
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ConfigFileError("operation_unavailable") from exc


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_plain(item) for item in value]
    return value


class ConfigFileService:
    """Versioned startup files, with atomic activation of model connections."""

    def __init__(
        self,
        settings: Settings,
        catalog: ModelProfileCatalog | None = None,
        *,
        autonomy_parameters: Callable[[], AutonomyParameters] | None = None,
        model_executor: TaskModelExecutor | None = None,
        web_module: WebModule | None = None,
    ) -> None:
        self._settings = settings
        self._catalog = catalog
        self._autonomy_parameters = autonomy_parameters
        self._model_executor = model_executor
        self._web_module = web_module
        self._lock = asyncio.Lock()

    @property
    def loaded_catalog(self) -> ModelProfileCatalog | None:
        return self._catalog

    @property
    def model_hot_reload_enabled(self) -> bool:
        return self._model_executor is not None

    def _path(self, file_id: str) -> Path:
        paths = {
            "model_profiles": self._settings.model_profiles_file,
            "system_prompt": self._settings.system_prompt_file,
            "bot_persona": self._settings.resolved_bot_persona_file,
            "autonomous_model": self._settings.semantic_participation_model_config_file,
        }
        if type(file_id) is not str or file_id not in paths:
            raise ConfigFileError("validation_error")
        path = paths[file_id]
        if path is None:
            raise ConfigFileError("operation_unavailable")
        return path.absolute()

    def _catalog_from(self, content: str) -> ModelProfileCatalog:
        settings = self._settings
        return parse_model_profile_catalog(
            content,
            environment=model_profile_environment(settings),
        )

    async def read(self, file_id: str) -> dict[str, Any]:
        path = self._path(file_id)
        content = await asyncio.to_thread(_read, path)
        result: dict[str, Any] = {
            "file_id": file_id,
            "exists": content is not None,
            "revision": _revision(content),
            "apply_mode": (
                "hot_reload"
                if file_id == "autonomous_model"
                or (file_id == "model_profiles" and self._model_executor)
                else "restart"
            ),
            "valid": True,
            "matches_loaded": None,
            "writable_directory": await asyncio.to_thread(os.access, path.parent, os.W_OK),
        }
        try:
            text = "" if content is None else content.decode("utf-8")
            if file_id == "autonomous_model":
                result["parameter_schema"] = AutonomyParameters.model_json_schema()
                result["defaults"] = DEFAULT_AUTONOMY_PARAMETERS.model_dump(mode="json")
                loaded = self._autonomy_parameters() if self._autonomy_parameters else None
                result["loaded_document"] = loaded.model_dump(mode="json") if loaded else None
                parameters = (
                    AutonomyParameters.model_validate_json(content)
                    if content is not None
                    else DEFAULT_AUTONOMY_PARAMETERS
                )
                result["document"] = parameters.model_dump(mode="json")
                result["matches_loaded"] = parameters == loaded if loaded else None
            elif file_id == "model_profiles":
                result["search_backend"] = self._settings.web_search_backend
                _secret_bytes, saved_keys = await asyncio.to_thread(read_model_secrets, path)
                # Headers are kept server-side even for content-authorized readers.
                raw: dict[str, Any] = (
                    tomllib.loads(text)
                    if text
                    else {"schema_version": 3, "profiles": {}, "routes": {}}
                )
                if not isinstance(raw.get("profiles"), dict):
                    raise ValueError("invalid profiles")
                raw_profiles = raw["profiles"]
                routes = raw.get("routes", {})
                if not isinstance(routes, dict):
                    raise ValueError("invalid routes")
                profiles: dict[str, Any] = {}
                permitted = set(ModelProfile.model_fields) - {"id", "headers"}
                permitted.update(
                    {"base_url_env", "model_env", "reasoning_effort_env", "thinking_mode"}
                )
                for name, profile in raw_profiles.items():
                    if not isinstance(profile, dict):
                        raise ValueError("invalid profile")
                    selected = {}
                    for key, value in profile.items():
                        if key not in permitted:
                            continue
                        adapter = _PUBLIC_PROFILE_FIELDS.get(key)
                        if adapter is None:
                            if type(value) is str:
                                selected[key] = value
                        else:
                            try:
                                typed = adapter.validate_python(value)
                                selected[key] = adapter.dump_python(
                                    typed, mode="json", exclude_unset=True
                                )
                            except ValidationError:
                                # Malformed typed fields cannot smuggle unreviewed objects.
                                continue
                    profiles[name] = selected
                result["document"] = {
                    "schema_version": raw.get("schema_version", 3)
                    if type(raw.get("schema_version", 3)) is int
                    else 3,
                    "profiles": profiles,
                    "search_connection": raw.get("search_connection"),
                    "routes": {
                        task.value: routes.get(task.value)
                        for task in ModelTask
                        if isinstance(routes.get(task.value), str)
                    },
                }
                result["profile_schema"] = ModelProfile.model_json_schema()
                result["tasks"] = [task.value for task in ModelTask]
                result["saved_api_key_profiles"] = [
                    name
                    for name, profile in raw_profiles.items()
                    if isinstance(profile, dict) and profile.get("api_key_env") in saved_keys
                ]
                if content is not None:
                    saved = self._catalog_from(text)
                    result["matches_loaded"] = saved == self._catalog if self._catalog else None
                    result["resolved_profiles"] = {
                        name: {
                            "base_url": profile.base_url,
                            "model": profile.model,
                            "reasoning_effort": (
                                profile.reasoning_effort.value
                                if profile.reasoning_effort is not None
                                else None
                            ),
                        }
                        for name, profile in saved.profiles.items()
                    }
            else:
                result["content"] = text
                if not text.strip():
                    raise ValueError("empty persona")
                actual = (
                    self._settings.bot_persona
                    if file_id == "bot_persona"
                    else self._settings.system_prompt
                )
                # Settings uses Path.read_text's universal newline decoding.
                # Preserve the editor's original text, compare the loaded semantics.
                candidate = text.replace("\r\n", "\n").replace("\r", "\n").strip()
                if file_id == "system_prompt":
                    candidate = candidate.replace(
                        "{{YUKI_PERSONA_CORE}}", self._settings.bot_persona
                    )
                result["matches_loaded"] = candidate == actual
        except (UnicodeError, ValueError, ModelRuntimeConfigurationError):
            # Do not reflect parser errors containing input, headers, URLs or host paths.
            result["valid"] = False
            result["error_category"] = "validation_error"
        return result

    async def save(self, file_id: str, expected_revision: int, spec: Mapping[str, Any]) -> int:
        async with self._lock:
            path = self._path(file_id)
            original = await asyncio.to_thread(_read, path)
            if _revision(original) != expected_revision:
                raise ConfigFileError("version_conflict")
            secret_write: tuple[Path, bytes | None, bytes] | None = None
            pending_catalog: ModelProfileCatalog | None = None
            pending_pool: ModelClientPool | None = None
            pending_search: WebSearchProvider | None = None
            try:
                if file_id == "autonomous_model":
                    if set(spec) != {"document"} or not isinstance(spec["document"], Mapping):
                        raise ValueError("invalid document")
                    parameters = AutonomyParameters.model_validate(_plain(spec["document"]))
                    text = json.dumps(parameters.model_dump(mode="json"), indent=2) + "\n"
                elif file_id == "model_profiles":
                    if set(spec) not in ({"document"}, {"document", "api_keys"}) or not isinstance(
                        spec["document"], Mapping
                    ):
                        raise ValueError("invalid document")
                    document = _plain(spec["document"])
                    if document.get("search_connection") is None:
                        document.pop("search_connection", None)
                    profiles = document.get("profiles")
                    if not isinstance(profiles, dict):
                        raise ValueError("invalid profiles")
                    updates = spec.get("api_keys", {})
                    if not isinstance(updates, Mapping):
                        raise ValueError("invalid key updates")
                    previous_secret_bytes, saved_keys = await asyncio.to_thread(
                        read_model_secrets, path
                    )
                    for name, value in updates.items():
                        if (
                            type(name) is not str
                            or KEY_ALIAS.fullmatch(name) is None
                            or type(value) is not str
                            or name in saved_keys
                            or not any(
                                isinstance(profile, dict) and profile.get("api_key_env") == name
                                for profile in profiles.values()
                            )
                        ):
                            raise ValueError("invalid model key update")
                        saved_keys[name] = value
                    if updates:
                        secret_write = (
                            model_secrets_path(path),
                            previous_secret_bytes,
                            encode_model_secrets(saved_keys),
                        )
                    existing = tomllib.loads(original.decode("utf-8")) if original else {}
                    old_profiles = existing.get("profiles", {})
                    if not isinstance(old_profiles, dict):
                        raise ValueError("invalid previous profiles")
                    for name, profile in profiles.items():
                        if not isinstance(profile, dict) or "headers" in profile:
                            raise ValueError("headers must stay server-side")
                        previous = old_profiles.get(name, {})
                        if not isinstance(previous, dict):
                            raise ValueError("invalid previous profile")
                        if "headers" in previous:
                            profile["headers"] = previous["headers"]
                    text = tomlkit.dumps(document)
                    pending_catalog = self._catalog_from(text)
                    content = text.encode("utf-8")
                    if len(content) > MAX_CONFIG_BYTES:
                        raise ValueError("file too large")
                    if self._model_executor is not None:
                        pending_pool = ModelClientPool(
                            secret_overrides={
                                "LLM_API_KEY": self._settings.llm_api_key,
                                "LLM_FLASH_API_KEY": self._settings.llm_flash_api_key,
                                **saved_keys,
                            }
                        )
                        try:
                            for profile in pending_catalog.profiles.values():
                                pending_pool.get(profile)
                        except Exception:
                            await pending_pool.close()
                            pending_pool = None
                            raise ValueError("model connection unavailable") from None
                        if self._web_module is not None:
                            try:
                                pending_search = self._web_module.prepare(
                                    pending_catalog, pending_pool
                                )
                            except Exception:
                                await pending_pool.close()
                                pending_pool = None
                                raise
                else:
                    if set(spec) != {"content"} or type(spec["content"]) is not str:
                        raise ValueError("invalid content")
                    text = spec["content"]
                    if not text.strip() or "\x00" in text:
                        raise ValueError("empty or invalid text")
                content = text.encode("utf-8")
                if len(content) > MAX_CONFIG_BYTES:
                    raise ValueError("file too large")
            except BaseException as exc:
                await asyncio.gather(
                    *(
                        resource.close()
                        for resource in (pending_search, pending_pool)
                        if resource is not None
                    ),
                    return_exceptions=True,
                )
                if isinstance(
                    exc, (TypeError, ValueError, UnicodeError, tomlkit.exceptions.TOMLKitError)
                ):
                    raise ConfigFileError("validation_error") from exc
                raise

            async def persist_and_activate() -> int:
                try:
                    revision = await asyncio.to_thread(
                        self._replace_model_bundle, path, original, content, secret_write
                    )
                    if pending_catalog is not None and pending_pool is not None:
                        assert self._model_executor is not None
                        # Search activation can reject a concurrent shutdown. Do
                        # that before switching the model router, whose validated
                        # catalog swap is synchronous and has no external I/O.
                        if self._web_module is not None:
                            self._web_module.activate(pending_search)
                        self._model_executor.apply_catalog(pending_catalog, pending_pool)
                        self._catalog = pending_catalog
                    return revision
                except BaseException:
                    if pending_catalog is not None and self._catalog is not pending_catalog:
                        try:
                            await asyncio.to_thread(
                                self._restore_model_bundle,
                                path,
                                original,
                                content,
                                secret_write,
                            )
                        finally:
                            await asyncio.gather(
                                *(
                                    resource.close()
                                    for resource in (pending_search, pending_pool)
                                    if resource is not None
                                ),
                                return_exceptions=True,
                            )
                    raise

            writing = asyncio.create_task(persist_and_activate())
            try:
                return await asyncio.shield(writing)
            except asyncio.CancelledError:
                # The OS write cannot be interrupted. Keep ownership until it ends;
                # cancellation still becomes an unknown persistent control receipt.
                while not writing.done():
                    try:
                        await asyncio.shield(writing)
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        break
                if not writing.cancelled():
                    writing.exception()
                raise

    def _replace_model_bundle(
        self,
        path: Path,
        original: bytes | None,
        content: bytes,
        secret_write: tuple[Path, bytes | None, bytes] | None,
    ) -> int:
        if secret_write is not None:
            secret_path, previous, replacement = secret_write
            self._replace(secret_path, previous, replacement)
        return self._replace(path, original, content)

    def _restore_model_bundle(
        self,
        path: Path,
        original: bytes | None,
        replacement: bytes,
        secret_write: tuple[Path, bytes | None, bytes] | None,
    ) -> None:
        """Undo only our exact bytes; never overwrite a concurrent external edit."""
        restore_errors: list[BaseException] = []
        try:
            self._restore_file(path, original, replacement)
        except BaseException as exc:
            restore_errors.append(exc)
        if secret_write is not None:
            secret_path, previous, secret_replacement = secret_write
            try:
                self._restore_file(secret_path, previous, secret_replacement)
            except BaseException as exc:
                restore_errors.append(exc)
        if restore_errors:
            raise restore_errors[0]

    def _restore_file(self, path: Path, original: bytes | None, replacement: bytes) -> None:
        current = _read(path)
        if current == original:
            return
        if current != replacement:
            raise ConfigFileError("version_conflict")
        if original is None:
            path.unlink()
        else:
            self._replace(path, replacement, original)

    @staticmethod
    def _replace(path: Path, original: bytes | None, content: bytes) -> int:
        if _read(path) != original:
            raise ConfigFileError("version_conflict")
        # Parent directories are deployment-owned; do not create arbitrary trees.
        try:
            descriptor, temporary = tempfile.mkstemp(prefix=".yuki-config-", dir=path.parent)
        except OSError as exc:
            raise ConfigFileError("operation_unavailable") from exc
        try:
            with os.fdopen(descriptor, "wb") as stream:
                if original is not None:
                    os.chmod(temporary, stat.S_IMODE(path.stat().st_mode))
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            if _read(path) != original:
                raise ConfigFileError("version_conflict")
            os.replace(temporary, path)
            # Exceptions after replacement propagate: the control receipt becomes unknown.
            if os.name == "posix":
                folder = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                try:
                    os.fsync(folder)
                finally:
                    os.close(folder)
            return _revision(content)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
