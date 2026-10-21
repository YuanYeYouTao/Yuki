"""Strict Plugin Manifest v1 parsing, compatibility checks, and hashing."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import tomllib
from pathlib import Path
from typing import Self

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version
from pydantic import Field, field_validator, model_validator

from yuki_plugin_sdk.api import PLUGIN_API_VERSION, is_api_compatible
from yuki_plugin_sdk.errors import ManifestValidationError
from yuki_plugin_sdk.models import PluginResourceLimits, StrictModel
from yuki_plugin_sdk.permissions import (
    RESERVED_PLUGIN_NAMESPACES,
    PluginPermission,
    parse_permissions,
)

_PLUGIN_ID = re.compile(r"^[a-z0-9](?:[a-z0-9.-]{0,126}[a-z0-9])?$")
_ENTRYPOINT = re.compile(
    r"^(?P<module>[a-zA-Z_][a-zA-Z0-9_]*(?:\.[a-zA-Z_][a-zA-Z0-9_]*)*):"
    r"(?P<symbol>[a-zA-Z_][a-zA-Z0-9_]*)$"
)
_HOST_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_SECRET_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")


class ManifestNetwork(StrictModel):
    allowed_hosts: tuple[str, ...] = Field(default=(), max_length=128)

    @field_validator("allowed_hosts", mode="before")
    @classmethod
    def _normalize_hosts(cls, value: object) -> tuple[str, ...]:
        if value is None:
            return ()
        if not isinstance(value, list | tuple):
            raise ValueError("network.allowed_hosts must be a list")
        normalized: list[str] = []
        for raw in value:
            if not isinstance(raw, str):
                raise ValueError("network.allowed_hosts entries must be strings")
            host = _normalize_host(raw)
            if host not in normalized:
                normalized.append(host)
        return tuple(normalized)


class ManifestLimits(PluginResourceLimits):
    pass


class PluginManifest(StrictModel):
    id: str
    name: str = Field(min_length=1, max_length=128)
    version: str
    description: str = Field(min_length=1, max_length=1_000)
    entrypoint: str
    plugin_api: str
    yuki_requires: str
    permissions: tuple[PluginPermission, ...] = ()
    secrets: tuple[str, ...] = Field(default=(), max_length=64)
    network: ManifestNetwork = Field(default_factory=ManifestNetwork)
    limits: ManifestLimits = Field(default_factory=ManifestLimits)

    @field_validator("id")
    @classmethod
    def _valid_id(cls, value: str) -> str:
        normalized = value.strip()
        if len(normalized) > 128 or _PLUGIN_ID.fullmatch(normalized) is None:
            raise ValueError("plugin id must contain lowercase letters, digits, dots, or hyphens")
        if any(
            normalized == namespace
            or normalized.startswith(f"{namespace}.")
            or normalized.startswith(f"{namespace}-")
            for namespace in RESERVED_PLUGIN_NAMESPACES
        ):
            raise ValueError("plugin id uses a reserved namespace")
        return normalized

    @field_validator("version")
    @classmethod
    def _valid_version(cls, value: str) -> str:
        try:
            parsed = Version(value)
        except InvalidVersion as exc:
            raise ValueError("version must be a valid PEP 440 version") from exc
        return str(parsed)

    @field_validator("entrypoint")
    @classmethod
    def _valid_entrypoint(cls, value: str) -> str:
        normalized = value.strip()
        if _ENTRYPOINT.fullmatch(normalized) is None:
            raise ValueError("entrypoint must use module.path:Symbol")
        return normalized

    @field_validator("plugin_api")
    @classmethod
    def _valid_api(cls, value: str) -> str:
        if not is_api_compatible(value, value):
            raise ValueError("invalid plugin API version")
        return value.strip()

    @field_validator("yuki_requires")
    @classmethod
    def _valid_yuki_specifier(cls, value: str) -> str:
        try:
            SpecifierSet(value)
        except InvalidSpecifier as exc:
            raise ValueError("yuki_requires must be a valid version specifier") from exc
        return value.strip()

    @field_validator("permissions", mode="before")
    @classmethod
    def _valid_permissions(cls, value: object) -> tuple[PluginPermission, ...]:
        if value is None:
            return ()
        if not isinstance(value, list | tuple):
            raise ValueError("permissions must be a list")
        if not all(isinstance(item, str) for item in value):
            raise ValueError("permissions entries must be strings")
        return parse_permissions(tuple(value))

    @field_validator("secrets", mode="before")
    @classmethod
    def _valid_secrets(cls, value: object) -> tuple[str, ...]:
        if value is None:
            return ()
        if not isinstance(value, list | tuple) or not all(isinstance(item, str) for item in value):
            raise ValueError("secrets must be a list of names")
        result: list[str] = []
        for item in value:
            normalized = item.strip().upper()
            if _SECRET_NAME.fullmatch(normalized) is None:
                raise ValueError("secret names must use uppercase letters, digits, and underscore")
            if normalized not in result:
                result.append(normalized)
        return tuple(result)

    @model_validator(mode="after")
    def _permission_constraints(self) -> Self:
        network_permission = (
            PluginPermission.NETWORK_HTTP_ALLOWLISTED in self.permissions
            or PluginPermission.NETWORK_HTTP_UNRESTRICTED in self.permissions
        )
        if self.network.allowed_hosts and not network_permission:
            raise ValueError("network.allowed_hosts requires a network HTTP permission")
        return self

    @property
    def manifest_hash(self) -> str:
        payload = json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @property
    def entrypoint_parts(self) -> tuple[str, str]:
        match = _ENTRYPOINT.fullmatch(self.entrypoint)
        assert match is not None
        return match.group("module"), match.group("symbol")

    def check_compatibility(
        self,
        *,
        host_plugin_api: str = PLUGIN_API_VERSION,
        yuki_version: str,
    ) -> None:
        if not is_api_compatible(self.plugin_api, host_plugin_api):
            raise ManifestValidationError(
                f"plugin API {self.plugin_api} is incompatible with host {host_plugin_api}"
            )
        try:
            compatible = Version(yuki_version) in SpecifierSet(self.yuki_requires)
        except (InvalidVersion, InvalidSpecifier) as exc:
            raise ManifestValidationError("invalid host or manifest version constraint") from exc
        if not compatible:
            raise ManifestValidationError(
                f"Yuki {yuki_version} does not satisfy {self.yuki_requires}"
            )


def load_manifest(
    path: Path,
    *,
    yuki_version: str,
    host_plugin_api: str = PLUGIN_API_VERSION,
    expected_directory_name: str | None = None,
) -> PluginManifest:
    manifest_path = path / "plugin.toml" if path.is_dir() else path
    try:
        payload = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
        manifest = PluginManifest.model_validate(payload)
        manifest.check_compatibility(
            host_plugin_api=host_plugin_api,
            yuki_version=yuki_version,
        )
    except ManifestValidationError:
        raise
    except (OSError, tomllib.TOMLDecodeError, ValueError) as exc:
        raise ManifestValidationError("invalid plugin manifest") from exc
    directory_name = expected_directory_name
    if directory_name is None and manifest_path.parent.name:
        directory_name = manifest_path.parent.name
    if directory_name is not None and directory_name != manifest.id:
        raise ManifestValidationError("plugin directory name must match manifest id")
    return manifest


def _normalize_host(value: str) -> str:
    candidate = value.strip().rstrip(".").casefold()
    if not candidate or any(token in candidate for token in ("://", "/", "?", "#", "*", "@")):
        raise ValueError("allowed host must be an exact hostname without URL syntax")
    try:
        host = candidate.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError("allowed host is invalid") from exc
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".lan")):
        raise ValueError("local or internal hosts are not allowed")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        labels = host.split(".")
        if len(labels) < 2 or any(_HOST_LABEL.fullmatch(label) is None for label in labels):
            raise ValueError("allowed host must be a valid public hostname") from None
    else:
        if not address.is_global:
            raise ValueError("private or reserved IP addresses are not allowed")
    return host
