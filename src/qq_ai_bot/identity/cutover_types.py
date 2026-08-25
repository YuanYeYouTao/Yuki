"""Persistence-free value types for identity cutover planning and reports."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Literal

from qq_ai_bot.identity.inventory import IDENTITY_PLATFORM

# v3: retire leftover scope carriers only after fail-closed alias/rollup/overlay proofs.
CUTOVER_INVENTORY_VERSION = "c27-cutover-v3"
_SHA256_HEX = frozenset("0123456789abcdef")

CutoverMode = Literal["plan", "apply"]
CutoverStatus = Literal["succeeded", "blocked", "failed"]


@dataclass(frozen=True, slots=True)
class CutoverSettingsInput:
    expected_git_revision: str
    git_revision: str
    downtime_token: str
    snapshot_db: str
    snapshot_wal: str
    snapshot_shm: str


@dataclass(frozen=True, slots=True)
class SnapshotEvidence:
    db_sha256: str
    wal_sha256: str
    shm_sha256: str
    db_size: int
    wal_size: int
    shm_size: int


@dataclass(frozen=True, slots=True)
class DuplicateDecision:
    kind: Literal["suppress", "conflict", "independent"]
    space_or_person: str
    sender_binding_id: str
    platform_message_id_fingerprint: str
    event_ids: tuple[int, ...]
    keeper_event_id: int | None


@dataclass(frozen=True, slots=True)
class ConversationWatermark:
    kind: Literal["private", "space"]
    owner_id: str
    primary_scope_key: str
    primary_scope_key_fingerprint: str
    covered_scope_keys: tuple[str, ...]
    last_event_id: int
    covered_through_event_id: int
    generation: int
    rollup_fingerprint: str
    suffix_event_count: int
    migration_summary: str


@dataclass(frozen=True, slots=True)
class CutoverPlan:
    source_fingerprint: str
    decision_digest: str
    source_manifest: dict[str, dict[str, int | str]]
    snapshot: SnapshotEvidence
    git_revision: str
    conversations: tuple[ConversationWatermark, ...]
    duplicates: tuple[DuplicateDecision, ...]
    suppress_event_ids: tuple[int, ...]
    conflict_event_groups: int
    pending_preconfig: int
    drain_ok: bool
    shadows_complete: bool
    c21_readable_cutoff: str


def is_sha256_hex(value: object) -> bool:
    if not isinstance(value, str) or len(value) != 64:
        return False
    return value == value.lower() and all(char in _SHA256_HEX for char in value)


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _migration_text_digest(summary: str) -> str:
    return hashlib.sha256(summary.encode("utf-8")).hexdigest()


def compute_decision_digest(
    *,
    conversations: tuple[ConversationWatermark, ...],
    duplicates: tuple[DuplicateDecision, ...],
    suppress_event_ids: tuple[int, ...],
    pending_preconfig: int,
    route_default_applicability: object,
    c21_readable_cutoff: str,
    inventory_version: str = CUTOVER_INVENTORY_VERSION,
) -> str:
    """Hash write-affecting plan decisions in memory. Callers persist only the digest."""

    material = {
        "c21_readable_cutoff": c21_readable_cutoff,
        "conversations": [
            {
                "covered_scope_keys": list(item.covered_scope_keys),
                "covered_through_event_id": item.covered_through_event_id,
                "generation": item.generation,
                "kind": item.kind,
                "last_event_id": item.last_event_id,
                "migration_summary_digest": _migration_text_digest(item.migration_summary),
                "owner_id": item.owner_id,
                "primary_scope_key": item.primary_scope_key,
                "rollup_fingerprint": item.rollup_fingerprint,
                "suffix_event_count": item.suffix_event_count,
            }
            for item in conversations
        ],
        "duplicates": [
            {
                "event_ids": list(item.event_ids),
                "keeper_event_id": item.keeper_event_id,
                "kind": item.kind,
                "platform_message_id_fingerprint": item.platform_message_id_fingerprint,
                "sender_binding_id": item.sender_binding_id,
                "space_or_person": item.space_or_person,
            }
            for item in duplicates
        ],
        "inventory": inventory_version,
        "pending_preconfig": pending_preconfig,
        "route_default_applicability": route_default_applicability,
        "suppress_event_ids": list(suppress_event_ids),
    }
    return hashlib.sha256(_canonical_json(material).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class CutoverCounts:
    conversations: int
    aliases: int
    routes: int
    mapped_events: int
    suppressed_events: int
    baselines: int


@dataclass(frozen=True, slots=True)
class CutoverReport:
    mode: CutoverMode
    status: CutoverStatus
    source_fingerprint: str
    inventory_version: str
    platform: str
    git_revision: str
    counts: CutoverCounts
    run_recorded: bool = False
    error_category: str | None = None
    business_diff: int = 0


def failed_cutover_report(mode: CutoverMode, category: str) -> CutoverReport:
    return CutoverReport(
        mode=mode,
        status="failed",
        source_fingerprint="",
        inventory_version=CUTOVER_INVENTORY_VERSION,
        platform=IDENTITY_PLATFORM,
        git_revision="",
        counts=CutoverCounts(
            conversations=0,
            aliases=0,
            routes=0,
            mapped_events=0,
            suppressed_events=0,
            baselines=0,
        ),
        run_recorded=False,
        error_category=category,
    )


def blocked_cutover_report(mode: CutoverMode, category: str) -> CutoverReport:
    return CutoverReport(
        mode=mode,
        status="blocked",
        source_fingerprint="",
        inventory_version=CUTOVER_INVENTORY_VERSION,
        platform=IDENTITY_PLATFORM,
        git_revision="",
        counts=CutoverCounts(
            conversations=0,
            aliases=0,
            routes=0,
            mapped_events=0,
            suppressed_events=0,
            baselines=0,
        ),
        run_recorded=False,
        error_category=category,
    )
