"""Telegram, Feishu/Lark and QQ official Bot credentials: one document plus private secrets.

This is the only source of these credentials. It reuses the model bundle's file
reader, atomic replacement, bundle revision and 0600 secret sidecar format.
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import tomlkit

from yuki.admin.config_files import ConfigFileError, ConfigFileService, _bundle_revision, _read
from yuki.model_runtime.secrets import encode_model_secrets, read_secret_file

# Per platform key: non-secret settings with their value types, then secret fields.
PLATFORM_SCHEMAS: dict[str, tuple[dict[str, type[str] | type[bool]], tuple[str, ...]]] = {
    "telegram": ({}, ("bot_token",)),
    "feishu": ({"app_id": str}, ("app_secret",)),
    "lark": ({"app_id": str}, ("app_secret",)),
    "qq_official": ({"app_id": str, "sandbox": bool}, ("app_secret",)),
}


def platform_secrets_path(path: Path) -> Path:
    return path.with_name("platforms.secrets.json")


def secret_alias(platform: str, name: str) -> str:
    return f"{platform}_{name}".upper()


@dataclass(frozen=True, slots=True)
class PlatformConfig:
    """What a platform factory receives: the saved settings and secret values."""

    platform: str
    enabled: bool
    settings: Mapping[str, str | bool]
    secrets: Mapping[str, str]

    def _required(self) -> list[str]:
        text, secret_names = PLATFORM_SCHEMAS[self.platform]
        values = [str(self.settings[name]) for name, kind in text.items() if kind is str]
        return values + [self.secrets.get(name, "") for name in secret_names]

    @property
    def configured(self) -> bool:
        return all(self._required())


def empty_platform(platform: str) -> PlatformConfig:
    text, _ = PLATFORM_SCHEMAS[platform]
    return PlatformConfig(
        platform, False, {name: "" if kind is str else False for name, kind in text.items()}, {}
    )


@dataclass(frozen=True, slots=True)
class PlatformBundle:
    content: bytes | None
    secret_bytes: bytes | None
    platforms: Mapping[str, PlatformConfig]
    keys: Mapping[str, str]

    @property
    def revision(self) -> int:
        return _bundle_revision(self.content, self.secret_bytes)

    def changed(
        self,
        platform: str,
        *,
        enabled: bool,
        settings: Mapping[str, Any],
        secrets: Mapping[str, str],
    ) -> PlatformBundle:
        """Merge one platform's submitted values; an empty secret removes the saved one."""
        if platform not in PLATFORM_SCHEMAS:
            raise ConfigFileError("validation_error")
        text, secret_names = PLATFORM_SCHEMAS[platform]
        if not set(settings) <= set(text) or not set(secrets) <= set(secret_names):
            raise ConfigFileError("validation_error")
        current = self.platforms.get(platform) or empty_platform(platform)
        merged = dict(current.settings)
        for name, value in settings.items():
            if type(value) is not text[name]:
                raise ConfigFileError("validation_error")
            merged[name] = value.strip() if isinstance(value, str) else value
        keys = dict(self.keys)
        for name, value in secrets.items():
            if value:
                keys[secret_alias(platform, name)] = value
            else:
                keys.pop(secret_alias(platform, name), None)
        config = PlatformConfig(
            platform,
            enabled,
            merged,
            {
                name: keys[secret_alias(platform, name)]
                for name in secret_names
                if secret_alias(platform, name) in keys
            },
        )
        required = config._required()
        # Credentials belong together, and only complete credentials can run.
        if (any(required) or enabled) and not all(required):
            raise ConfigFileError("validation_error")
        platforms = {**self.platforms, platform: config}
        try:
            secret_bytes = (
                encode_model_secrets(keys) if keys != dict(self.keys) else self.secret_bytes
            )
        except ValueError:
            raise ConfigFileError("validation_error") from None
        content = tomlkit.dumps(
            {
                name: {"enabled": item.enabled, **item.settings}
                for name, item in sorted(platforms.items())
            }
        ).encode("utf-8")
        return PlatformBundle(content, secret_bytes, platforms, keys)


def _parse(platform: str, section: object, keys: Mapping[str, str]) -> PlatformConfig:
    text, secret_names = PLATFORM_SCHEMAS[platform]
    if not isinstance(section, dict) or set(section) != {"enabled", *text}:
        raise ValueError("invalid platform section")
    if type(section["enabled"]) is not bool or any(
        type(section[name]) is not kind for name, kind in text.items()
    ):
        raise ValueError("invalid platform value")
    return PlatformConfig(
        platform,
        section["enabled"],
        {name: section[name] for name in text},
        {
            name: keys[secret_alias(platform, name)]
            for name in secret_names
            if secret_alias(platform, name) in keys
        },
    )


def load_platforms(path: Path) -> PlatformBundle:
    content = _read(path)
    try:
        secret_bytes, keys = read_secret_file(platform_secrets_path(path))
        raw = tomllib.loads(content.decode("utf-8")) if content is not None else {}
        if not set(raw) <= set(PLATFORM_SCHEMAS):
            raise ValueError("unknown platform")
        platforms = {name: _parse(name, section, keys) for name, section in raw.items()}
    except (ValueError, UnicodeError):
        # Parser errors may quote values; report only the category.
        raise ConfigFileError("validation_error") from None
    return PlatformBundle(content, secret_bytes, platforms, keys)


def write_platforms(path: Path, original: PlatformBundle, updated: PlatformBundle) -> int:
    """Secrets first, then the document; an unchanged sidecar is not rewritten."""
    assert updated.content is not None
    secrets_path = platform_secrets_path(path)
    if _read(secrets_path) != original.secret_bytes:
        raise ConfigFileError("version_conflict")
    writes_secrets = updated.secret_bytes != original.secret_bytes
    if writes_secrets:
        assert updated.secret_bytes is not None
        ConfigFileService._replace(secrets_path, original.secret_bytes, updated.secret_bytes)
    try:
        ConfigFileService._replace(path, original.content, updated.content)
    except BaseException:
        if writes_secrets and _read(secrets_path) == updated.secret_bytes:
            if original.secret_bytes is None:
                secrets_path.unlink()
            else:
                assert updated.secret_bytes is not None
                ConfigFileService._replace(
                    secrets_path, updated.secret_bytes, original.secret_bytes
                )
        raise
    return updated.revision
