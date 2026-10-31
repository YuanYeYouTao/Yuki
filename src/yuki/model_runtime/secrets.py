"""Local operator-entered model keys, separate from readable profile documents."""

from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from yuki.config import Settings
    from yuki.settings_domains import ModelRuntimeSettings

MAX_SECRET_FILE_BYTES = 256 * 1024
KEY_ALIAS = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")


def model_secret_environment(settings: Settings | ModelRuntimeSettings) -> dict[str, str]:
    """Preserve startup Settings credentials while migrating them to bundle sidecars."""
    return {
        "LLM_API_KEY": settings.llm_api_key,
        "LLM_FLASH_API_KEY": settings.llm_flash_api_key,
        "MEMORY_EMBEDDING_API_KEY": settings.memory_embedding_api_key,
        "VISION_API_KEY": settings.vision_api_key,
        "ASR_API_KEY": settings.asr_api_key,
        "TAVILY_API_KEY": settings.tavily_api_key,
    }


def model_secrets_path(profile_path: Path) -> Path:
    return profile_path.with_name("model_profiles.secrets.json")


def read_model_secrets(profile_path: Path) -> tuple[bytes | None, dict[str, str]]:
    """Require a private regular file; never include key material in errors."""
    path = model_secrets_path(profile_path)
    try:
        before = path.lstat()
    except FileNotFoundError:
        return None, {}
    except OSError as exc:
        raise ValueError("cannot read model secret file") from exc
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise ValueError("invalid model secret file")
    if os.name == "posix" and stat.S_IMODE(before.st_mode) & 0o077:
        raise ValueError("model secret file is not private")
    if before.st_size > MAX_SECRET_FILE_BYTES:
        raise ValueError("model secret file is too large")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    try:
        with os.fdopen(os.open(path, flags), "rb") as stream:
            opened = os.fstat(stream.fileno())
            if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
                raise ValueError("model secret file changed during read")
            raw = stream.read(MAX_SECRET_FILE_BYTES + 1)
        if len(raw) > MAX_SECRET_FILE_BYTES:
            raise ValueError("model secret file is too large")
        parsed = json.loads(raw)
        if not isinstance(parsed, dict) or set(parsed) != {"version", "keys"}:
            raise ValueError("invalid model secret file")
        keys = parsed["keys"]
        if parsed["version"] != 1 or not isinstance(keys, dict):
            raise ValueError("invalid model secret file")
        if any(
            not isinstance(name, str)
            or KEY_ALIAS.fullmatch(name) is None
            or not isinstance(value, str)
            or not value
            or not value.strip()
            or len(value) > 8192
            or "\n" in value
            or "\r" in value
            for name, value in keys.items()
        ):
            raise ValueError("invalid model secret file")
        return raw, keys
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("cannot read model secret file") from exc


def encode_model_secrets(keys: dict[str, str]) -> bytes:
    if any(
        KEY_ALIAS.fullmatch(name) is None
        or not value
        or not value.strip()
        or len(value) > 8192
        or "\n" in value
        or "\r" in value
        for name, value in keys.items()
    ):
        raise ValueError("invalid model key")
    encoded = json.dumps({"version": 1, "keys": keys}, ensure_ascii=False).encode()
    if len(encoded) > MAX_SECRET_FILE_BYTES:
        raise ValueError("model secret file is too large")
    return encoded
