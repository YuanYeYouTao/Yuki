"""Local, deterministic names for OneBot ``face`` segments."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class QQFaceResolver:
    """Resolve QQ face IDs without a network or vision request."""

    def __init__(
        self,
        mapping_path: str | Path | None = None,
        *,
        mapping: dict[str | int, str] | None = None,
    ) -> None:
        if mapping is not None:
            self._mapping = _clean_mapping(mapping)
            return
        path = Path(mapping_path) if mapping_path is not None else _default_mapping_path()
        self._mapping = _load_mapping(path)

    def resolve(self, face_id: str | int) -> str:
        """Return a readable name, or preserve an unknown ID."""

        normalized = str(face_id).strip()
        if not normalized:
            return "ID 未知"
        return self._mapping.get(normalized, f"ID {normalized}")


def _default_mapping_path() -> Path:
    working_copy = Path.cwd() / "config" / "qq_face_map.json"
    if working_copy.is_file():
        return working_copy
    return Path(__file__).resolve().parents[3] / "config" / "qq_face_map.json"


def _load_mapping(path: Path) -> dict[str, str]:
    try:
        decoded: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return _clean_mapping(decoded if isinstance(decoded, dict) else {})


def _clean_mapping(mapping: dict[Any, Any]) -> dict[str, str]:
    clean: dict[str, str] = {}
    for key, value in mapping.items():
        normalized_key = str(key).strip()
        if normalized_key and isinstance(value, str) and value.strip():
            clean[normalized_key] = " ".join(value.split())[:40]
    return clean
