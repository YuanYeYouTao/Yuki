"""Stable, non-reversible fingerprints for backfill reports.

Reports and CLI output must not include message bodies, secrets, absolute
paths, or full external account/space ids. The source digest covers every
input that can change classification, owner reuse, create attributes, or
the shadow plan.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from qq_ai_bot.identity.inventory import IDENTITY_PLATFORM, INVENTORY_VERSION


def normalize_external_id(raw: object) -> str | None:
    """Trim an opaque external id. Empty or oversized values are dropped."""

    if raw is None:
        return None
    value = str(raw).strip()
    if not value or len(value) > 255:
        return None
    return value


def fingerprint_external_id(external_id: str, *, platform: str = IDENTITY_PLATFORM) -> str:
    """16-hex fingerprint of one platform-scoped external id."""

    digest = hashlib.sha256(f"{platform}\0{external_id}".encode()).hexdigest()
    return digest[:16]


def fingerprint_id_set(values: Iterable[str], *, platform: str = IDENTITY_PLATFORM) -> str:
    """Fingerprint a set of external ids without revealing membership."""

    material = "\n".join(
        fingerprint_external_id(item, platform=platform) for item in sorted(set(values))
    )
    return hashlib.sha256(material.encode()).hexdigest()[:16]


def _text_digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:16]


def source_fingerprint(
    *,
    accounts: Mapping[str, Mapping[str, Any]],
    spaces: Mapping[str, Mapping[str, Any]],
    bindings: Mapping[str, str],
    presences: Mapping[str, str],
    space_bindings: Mapping[str, str],
    shadows: Sequence[Mapping[str, Any]],
    settings_superusers: Iterable[str],
    settings_enabled_groups: Iterable[str],
    settings_ignored_bots: Iterable[str],
    platform: str = IDENTITY_PLATFORM,
) -> str:
    """Stable digest of classification inputs. Callers receive only this digest."""

    payload = {
        "inventory": INVENTORY_VERSION,
        "platform": platform,
        "accounts": {
            fingerprint_external_id(key, platform=platform): dict(value)
            for key, value in sorted(accounts.items())
        },
        "spaces": {
            fingerprint_external_id(key, platform=platform): dict(value)
            for key, value in sorted(spaces.items())
        },
        "bindings": {
            fingerprint_external_id(key, platform=platform): person_id
            for key, person_id in sorted(bindings.items())
        },
        "presences": {
            fingerprint_external_id(key, platform=platform): presence_id
            for key, presence_id in sorted(presences.items())
        },
        "space_bindings": {
            fingerprint_external_id(key, platform=platform): space_id
            for key, space_id in sorted(space_bindings.items())
        },
        "shadows": sorted(
            (dict(item) for item in shadows),
            key=lambda item: (
                str(item.get("table", "")),
                str(item.get("column", "")),
                str(item.get("row_key", "")),
                str(item.get("source", "")),
            ),
        ),
        "settings": {
            "superusers": fingerprint_id_set(settings_superusers, platform=platform),
            "enabled_groups": fingerprint_id_set(settings_enabled_groups, platform=platform),
            "ignored_bot_users": fingerprint_id_set(settings_ignored_bots, platform=platform),
        },
    }
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def account_fingerprint_material(
    *,
    sources: Iterable[str],
    flags: Mapping[str, bool],
    nickname: str,
    existing_person_id: str | None,
    existing_binding_person_id: str | None,
    existing_presence_id: str | None,
    shadow_person_ids: Iterable[str],
    shadow_presence_ids: Iterable[str],
) -> dict[str, Any]:
    return {
        "sources": sorted(sources),
        "flags": {key: bool(flags[key]) for key in sorted(flags)},
        "nickname": _text_digest(nickname),
        "existing_person_id": existing_person_id or "",
        "existing_binding_person_id": existing_binding_person_id or "",
        "existing_presence_id": existing_presence_id or "",
        "shadow_person_ids": sorted(shadow_person_ids),
        "shadow_presence_ids": sorted(shadow_presence_ids),
    }


def space_fingerprint_material(
    *,
    sources: Iterable[str],
    name: str,
    enabled: bool,
    autonomous_enabled: bool,
    require_mention: bool,
    has_groups_row: bool,
    existing_space_id: str | None,
    existing_binding_space_id: str | None,
    shadow_space_ids: Iterable[str],
) -> dict[str, Any]:
    return {
        "sources": sorted(sources),
        "name": _text_digest(name),
        "enabled": bool(enabled),
        "autonomous_enabled": bool(autonomous_enabled),
        "require_mention": bool(require_mention),
        "has_groups_row": bool(has_groups_row),
        "existing_space_id": existing_space_id or "",
        "existing_binding_space_id": existing_binding_space_id or "",
        "shadow_space_ids": sorted(shadow_space_ids),
    }


def fingerprint_row_key(row_key: Sequence[tuple[str, object]]) -> str:
    """Hash a shadow primary key without emitting the raw values."""

    parts = []
    for name, value in row_key:
        raw = "" if value is None else str(value)
        digest = hashlib.sha256(f"{name}\0{raw}".encode()).hexdigest()[:16]
        parts.append(f"{name}:{digest}")
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()[:16]


def shadow_fingerprint_material(
    *,
    table: str,
    column: str,
    row_key: Sequence[tuple[str, object]],
    source: str,
    current: str | None,
    fillable: bool,
    platform: str = IDENTITY_PLATFORM,
) -> dict[str, Any]:
    return {
        "table": table,
        "column": column,
        "row_key": fingerprint_row_key(row_key),
        "source": fingerprint_external_id(source, platform=platform),
        "current": current or "",
        "fillable": bool(fillable),
    }


def looks_like_secret_or_path(text: str) -> bool:
    """Heuristic guard for accidental report leakage."""

    lowered = text.casefold()
    if any(
        token in lowered for token in ("secret", "token", "password", "api_key", "authorization")
    ):
        return True
    if ":\\" in text or text.startswith("/") or "://" in text:
        return True
    return False
