"""SQLite adapter for identity cutover.

Talks to sqlite3 only. Never imports argparse, a renderer, or ORM models.
Callers pass a file path; the adapter does not print it.
"""

from __future__ import annotations

import gc
import hashlib
import json
import shutil
import sqlite3
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final, cast
from uuid import uuid4

from qq_ai_bot.identity.backfill_repository import (
    connect_sqlite,
    utc_now_text,
)
from qq_ai_bot.identity.c21_evidence import (
    require_c21_readable_evidence,
    require_planned_evidence_alignable,
)
from qq_ai_bot.identity.c21_readable_owners import (
    require_c21_readable_owners,
    snapshot_c21_cutoff,
)
from qq_ai_bot.identity.c22_automation import require_c22_runnable_automation_targets
from qq_ai_bot.identity.c23_plugin import require_c23_plugin_targets
from qq_ai_bot.identity.cutover_source import (
    live_source_manifest,
    manifests_equal,
    normalize_event_text,
    normalize_segments_json,
    source_manifest_fingerprint,
)
from qq_ai_bot.identity.cutover_types import (
    CUTOVER_INVENTORY_VERSION,
    ConversationWatermark,
    CutoverPlan,
    CutoverSettingsInput,
    DuplicateDecision,
    SnapshotEvidence,
    compute_decision_digest,
    is_sha256_hex,
)
from qq_ai_bot.identity.errors import IdentityCutoverError, IdentityCutoverPreconditionError
from qq_ai_bot.identity.inventory import (
    EVENT_AUTHOR_KINDS,
    HUMAN_PLUGIN_MESSAGE_ROLES,
    IDENTITY_PLATFORM,
    REQUIRED_C27_SCHEMA,
    SHADOW_FILL_SPECS,
    ShadowFillSpec,
)
from qq_ai_bot.identity.sanitize import fingerprint_external_id, normalize_external_id

Failpoint = Callable[[str], None]

_WAL_MAGIC: frozenset[bytes] = frozenset({b"\x37\x7f\x06\x82", b"\x37\x7f\x06\x83"})
_EMPTY_DIGEST: Final[str] = hashlib.sha256(b"").hexdigest()
_DOWNTIME_TOKEN_DOMAIN: Final[str] = "identity-cutover-downtime"

# Frozen one-time cutover bound. Matches the 2400-character rollup default but
# is not read from live settings, so apply stays model-free and deterministic.
MIGRATION_SUMMARY_MAX_CHARACTERS: Final[int] = 2400
_MIGRATION_EMPTY_SUMMARY: Final[str] = "migration-empty"
_MIGRATION_SCOPE_SEPARATOR: Final[str] = "\n---\n"
_CUTOVER_ALIAS_PREFIX: Final[str] = "cutover:"
_INCOMPATIBLE_CONVERSATION: Final[str] = "populated_merge_forbidden"
_CONVERSATION_STATE_COLUMNS: Final[str] = (
    "id, generation, starts_after_event_id, last_event_id, "
    "last_generation_change_event_id, covered_through_event_id, "
    "uncovered_event_count, uncovered_character_count"
)


@dataclass(frozen=True, slots=True)
class _MigrationScopeMaterial:
    semantic: str
    suffix_events: tuple[str, ...]

    @property
    def has_content(self) -> bool:
        return bool(self.semantic) or bool(self.suffix_events)


def _clip_text(text: str, limit: int) -> str:
    if limit <= 0 or not text:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit]


def _joined_size(parts: tuple[str, ...] | list[str]) -> int:
    kept = [part for part in parts if part]
    if not kept:
        return 0
    return sum(len(part) for part in kept) + (len(kept) - 1)


def _fair_allocate(requested: tuple[int, ...], budget: int) -> tuple[int, ...]:
    """Water-fill `budget` across requested sizes in stable index order."""

    allocated = [0] * len(requested)
    if not requested or budget <= 0:
        return tuple(allocated)
    remaining = budget
    active = [index for index, size in enumerate(requested) if size > 0]
    while active and remaining > 0:
        share, leftover = divmod(remaining, len(active))
        if share == 0:
            for index in active:
                if leftover <= 0:
                    break
                need = requested[index] - allocated[index]
                if need <= 0:
                    continue
                allocated[index] += 1
                leftover -= 1
                remaining -= 1
            break
        next_active: list[int] = []
        for index in active:
            take = min(share, requested[index] - allocated[index])
            allocated[index] += take
            remaining -= take
            if allocated[index] < requested[index]:
                next_active.append(index)
        active = next_active
    return tuple(allocated)


def _fit_pieces(parts: tuple[str, ...], budget: int) -> tuple[str, ...]:
    if budget <= 0:
        return ()
    kept: list[str] = []
    remaining = budget
    for part in parts:
        if not part:
            continue
        sep = 1 if kept else 0
        available = remaining - sep
        if available <= 0:
            break
        if len(part) <= available:
            kept.append(part)
            remaining -= sep + len(part)
            continue
        if not kept:
            clipped = _clip_text(part, available)
            if clipped:
                kept.append(clipped)
        break
    return tuple(kept)


def _fill_event_side(
    events: tuple[str, ...],
    budget: int,
    *,
    offset: int,
    reverse: bool,
) -> dict[int, str]:
    if not events or budget <= 0:
        return {}
    order = list(range(len(events) - 1, -1, -1)) if reverse else list(range(len(events)))
    chosen: dict[int, str] = {}
    remaining = budget
    outermost = True
    for local_index in order:
        text = events[local_index]
        if not text:
            continue
        sep = 1 if chosen else 0
        available = remaining - sep
        if available <= 0:
            break
        key = offset + local_index
        if outermost:
            outermost = False
            clipped = _clip_text(text, available)
            if not clipped:
                continue
            chosen[key] = clipped
            remaining -= sep + len(clipped)
            continue
        if len(text) <= available:
            chosen[key] = text
            remaining -= sep + len(text)
    return chosen


def _bounded_suffix_events(events: tuple[str, ...], budget: int) -> tuple[str, ...]:
    """Keep early and recent events under `budget`. Prefer whole events."""

    cleaned = tuple(event for event in events if event)
    if not cleaned or budget <= 0:
        return ()
    if len(cleaned) == 1:
        text = _clip_text(cleaned[0], budget)
        return (text,) if text else ()

    mid = (len(cleaned) + 1) // 2
    early = cleaned[:mid]
    recent = cleaned[mid:]
    early_budget = budget // 2
    recent_budget = budget - early_budget
    chosen = _fill_event_side(early, early_budget, offset=0, reverse=False)
    chosen.update(_fill_event_side(recent, recent_budget, offset=mid, reverse=True))

    def occupied(start: int, end: int) -> int:
        texts = [chosen[index] for index in range(start, end) if index in chosen]
        return _joined_size(texts)

    leftover = budget - occupied(0, len(cleaned))
    if leftover > 0:
        early_need = max(0, _joined_size(early) - occupied(0, mid))
        recent_need = max(0, _joined_size(recent) - occupied(mid, len(cleaned)))
        extra_early, extra_recent = _fair_allocate((early_need, recent_need), leftover)
        if extra_early:
            early_next = occupied(0, mid) + extra_early
            for key in [key for key in chosen if key < mid]:
                del chosen[key]
            chosen.update(_fill_event_side(early, early_next, offset=0, reverse=False))
        if extra_recent:
            recent_next = occupied(mid, len(cleaned)) + extra_recent
            for key in [key for key in chosen if key >= mid]:
                del chosen[key]
            chosen.update(_fill_event_side(recent, recent_next, offset=mid, reverse=True))
    ordered = tuple(chosen[index] for index in sorted(chosen))
    return _fit_pieces(ordered, budget)


def _scope_requested_characters(semantic: str, events: tuple[str, ...]) -> int:
    return _joined_size([part for part in (semantic, *events) if part])


def _render_scope_parts(semantic: str, events: tuple[str, ...], budget: int) -> tuple[str, ...]:
    if budget <= 0:
        return ()
    parts: list[str] = []
    remaining = budget
    if semantic:
        text = _clip_text(semantic, remaining)
        if text:
            parts.append(text)
            remaining -= len(text)
    if remaining <= 0:
        return tuple(parts)
    if parts:
        remaining -= 1
        if remaining <= 0:
            return tuple(parts)
    parts.extend(_bounded_suffix_events(events, remaining))
    return tuple(parts)


def _ordinal_prefix(ordinal: int) -> str:
    return f"[{ordinal}]\n"


def _join_blocks_bounded(blocks: tuple[str, ...], separator: str, limit: int) -> str:
    pieces = [block for block in blocks if block]
    if not pieces:
        return ""
    joined = separator.join(pieces)
    if len(joined) <= limit:
        return joined
    while pieces:
        joined = separator.join(pieces)
        overflow = len(joined) - limit
        if overflow <= 0:
            return joined
        longest = max(range(len(pieces)), key=lambda index: len(pieces[index]))
        keep = len(pieces[longest]) - overflow
        if keep <= 0:
            del pieces[longest]
            continue
        pieces[longest] = pieces[longest][:keep]
    return ""


def _assemble_migration_summary(materials: tuple[_MigrationScopeMaterial, ...]) -> str:
    contributors = tuple(
        (index, item) for index, item in enumerate(materials, start=1) if item.has_content
    )
    if not contributors:
        return _MIGRATION_EMPTY_SUMMARY
    labeled = len(contributors) > 1
    prefixes = tuple(_ordinal_prefix(index) if labeled else "" for index, _item in contributors)
    reserved = sum(len(prefix) for prefix in prefixes) + (
        len(_MIGRATION_SCOPE_SEPARATOR) * max(0, len(contributors) - 1)
    )
    content_budget = max(0, MIGRATION_SUMMARY_MAX_CHARACTERS - reserved)
    requested = tuple(
        _scope_requested_characters(item.semantic, item.suffix_events)
        for _index, item in contributors
    )
    budgets = list(_fair_allocate(requested, content_budget))
    rendered = [
        _render_scope_parts(item.semantic, item.suffix_events, budget)
        for (_index, item), budget in zip(contributors, budgets, strict=True)
    ]
    leftover = content_budget - sum(_joined_size(parts) for parts in rendered)
    if leftover > 0:
        demand = []
        for (_index, item), parts in zip(contributors, rendered, strict=True):
            full = _scope_requested_characters(item.semantic, item.suffix_events)
            demand.append(max(0, full - _joined_size(parts)))
        extras = _fair_allocate(tuple(demand), leftover)
        if any(extras):
            budgets = [budget + extra for budget, extra in zip(budgets, extras, strict=True)]
            rendered = [
                _render_scope_parts(item.semantic, item.suffix_events, budget)
                for (_index, item), budget in zip(contributors, budgets, strict=True)
            ]
    blocks: list[str] = []
    for prefix, parts in zip(prefixes, rendered, strict=True):
        body = "\n".join(parts)
        if body:
            blocks.append(f"{prefix}{body}")
    if not blocks:
        return _MIGRATION_EMPTY_SUMMARY
    assembled = _join_blocks_bounded(
        tuple(blocks),
        _MIGRATION_SCOPE_SEPARATOR,
        MIGRATION_SUMMARY_MAX_CHARACTERS,
    )
    return assembled or _MIGRATION_EMPTY_SUMMARY


def _migration_summary_fingerprint(
    summary: str,
    *,
    last_event_id: int,
    covered_through_event_id: int,
    generation: int,
    scope_count: int,
    suffix_event_total: int,
) -> str:
    payload = {
        "covered_through_event_id": covered_through_event_id,
        "generation": generation,
        "last_event_id": last_event_id,
        "limit": MIGRATION_SUMMARY_MAX_CHARACTERS,
        "scope_count": scope_count,
        "suffix_event_total": suffix_event_total,
        "summary": summary,
        "summary_kind": "migration",
    }
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


_DRAIN_STATUS: dict[str, tuple[str, ...]] = {
    "memory_jobs": ("pending", "processing"),
    "memory_reflection_jobs": ("pending", "processing"),
    "conversation_rollup_jobs": ("pending", "processing"),
    "canonical_conversation_rollup_jobs": ("pending", "processing"),
    "plugin_notification_outbox": ("processing",),
    "plugin_background_turn_jobs": ("processing",),
    "memory_dream_runs": ("running", "rolling_back"),
    "memory_rebuild_runs": ("extracting", "committing"),
    "emoji_jobs": ("processing",),
    "relationship_jobs": ("processing",),
    "memory_embedding_jobs": ("processing",),
    "automations": (),
}


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()
    return row is not None


def _column_exists(connection: sqlite3.Connection, table: str, column: str) -> bool:
    return any(str(row[1]) == column for row in connection.execute(f'PRAGMA table_info("{table}")'))


def _paths_equivalent(left: Path, right: Path) -> bool:
    try:
        if left.exists() and right.exists() and left.samefile(right):
            return True
    except OSError:
        pass
    try:
        return left.expanduser().resolve(strict=False) == right.expanduser().resolve(strict=False)
    except OSError:
        return False


def downtime_token_digest(token: str) -> str:
    material = f"{_DOWNTIME_TOKEN_DOMAIN}\0{token}".encode()
    return hashlib.sha256(material).hexdigest()


def _snapshot_field(digest: str, size: int) -> str:
    return f"{digest}:{size}"


def _privacy_snapshot_fields(snapshot: SnapshotEvidence | None) -> tuple[str, str, str]:
    if snapshot is None:
        empty = _snapshot_field(_EMPTY_DIGEST, 0)
        return empty, empty, empty
    return (
        _snapshot_field(snapshot.db_sha256, snapshot.db_size),
        _snapshot_field(snapshot.wal_sha256, snapshot.wal_size),
        _snapshot_field(snapshot.shm_sha256, snapshot.shm_size),
    )


def _persisted_account_class(
    external_id: str,
    *,
    people_bots: dict[str, bool],
    bindings: dict[str, str],
    presences: dict[str, str],
) -> str | None:
    has_binding = external_id in bindings
    has_presence = external_id in presences
    if has_binding and has_presence:
        return None
    is_bot = people_bots.get(external_id)
    is_yuki = has_presence and not has_binding
    is_external_bot = bool(is_bot) and not is_yuki
    people_human = is_bot is False
    if is_yuki and (people_human or has_binding):
        return None
    if is_external_bot and has_binding:
        return None
    if is_yuki:
        return "yuki_presence"
    if is_external_bot:
        return "external_bot"
    if has_binding or people_human:
        return "person"
    return None


def _resolved_canonical(
    kind: str,
    external_id: str,
    bindings: dict[str, str],
    spaces: dict[str, str],
    presences: dict[str, str],
) -> str | None:
    if kind == "person":
        return bindings.get(external_id)
    if kind == "space":
        return spaces.get(external_id)
    return presences.get(external_id)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _replace_path(destination: Path, source: Path | None) -> None:
    last_error: OSError | None = None
    for attempt in range(20):
        try:
            if destination.exists() or destination.is_symlink():
                destination.unlink()
            if source is not None:
                shutil.copy2(source, destination)
            return
        except OSError as exc:
            last_error = exc
            gc.collect()
            time.sleep(0.05 * (attempt + 1))
    if last_error is not None:
        raise last_error


def restore_sqlite_snapshot(
    target: Path,
    snapshot_db: Path,
    snapshot_wal: Path,
    snapshot_shm: Path,
) -> None:
    """Replace a sqlite file with the plan-recorded DB/WAL/SHM snapshot."""

    if not snapshot_db.is_file():
        raise IdentityCutoverPreconditionError("snapshot_missing")
    target.parent.mkdir(parents=True, exist_ok=True)
    _replace_path(target, snapshot_db)
    wal_target = Path(str(target) + "-wal")
    shm_target = Path(str(target) + "-shm")
    if snapshot_wal.is_file() and snapshot_wal.stat().st_size:
        _replace_path(wal_target, snapshot_wal)
    else:
        _replace_path(wal_target, None)
    if snapshot_shm.is_file() and snapshot_shm.stat().st_size:
        _replace_path(shm_target, snapshot_shm)
    else:
        _replace_path(shm_target, None)


class IdentityCutoverRepository:
    """Read-only planning and transactional apply for identity cutover."""

    def __init__(self, path: Path, failpoint: Failpoint | None = None) -> None:
        self._path = path
        self.failpoint = failpoint

    def connect(self, *, readonly: bool = False) -> sqlite3.Connection:
        return connect_sqlite(self._path, readonly=readonly)

    def trip(self, name: str) -> None:
        if self.failpoint is not None:
            self.failpoint(name)

    def require_c27_ready(self, connection: sqlite3.Connection) -> None:
        for table, columns in REQUIRED_C27_SCHEMA.items():
            if not _table_exists(connection, table):
                raise IdentityCutoverPreconditionError("incomplete_schema")
            present = {str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")')}
            if any(column not in present for column in columns):
                raise IdentityCutoverPreconditionError("incomplete_schema")
        rows = connection.execute(
            "SELECT id, state FROM identity_runtime_state ORDER BY id"
        ).fetchall()
        if len(rows) != 1 or int(rows[0]["id"]) != 1 or str(rows[0]["state"]) != "v1":
            raise IdentityCutoverPreconditionError("identity_runtime_state")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise IdentityCutoverPreconditionError("foreign_key_check")

    def snapshot_evidence(self, settings: CutoverSettingsInput) -> SnapshotEvidence:
        db = Path(settings.snapshot_db)
        wal = Path(settings.snapshot_wal)
        shm = Path(settings.snapshot_shm)
        self._require_snapshot_not_live(db, wal, shm)
        self._require_snapshot_companions(db, wal, shm)
        return SnapshotEvidence(
            db_sha256=_file_sha256(db),
            wal_sha256=_file_sha256(wal),
            shm_sha256=_file_sha256(shm),
            db_size=int(db.stat().st_size),
            wal_size=int(wal.stat().st_size),
            shm_size=int(shm.stat().st_size),
        )

    def snapshot_source_manifest(
        self, settings: CutoverSettingsInput
    ) -> dict[str, dict[str, int | str]]:
        db = Path(settings.snapshot_db)
        wal = Path(settings.snapshot_wal)
        shm = Path(settings.snapshot_shm)
        self._require_snapshot_not_live(db, wal, shm)
        self._require_snapshot_companions(db, wal, shm)
        with tempfile.TemporaryDirectory(prefix="cutover-snap-") as raw:
            assembled = Path(raw) / "snapshot.db"
            shutil.copy2(db, assembled)
            shutil.copy2(wal, Path(str(assembled) + "-wal"))
            shutil.copy2(shm, Path(str(assembled) + "-shm"))
            connection = connect_sqlite(assembled, readonly=True)
            try:
                return live_source_manifest(connection)
            except sqlite3.Error as exc:
                raise IdentityCutoverError("source_fingerprint") from exc
            finally:
                connection.close()

    def _require_snapshot_not_live(self, db: Path, wal: Path, shm: Path) -> None:
        live = self._path
        companions = (live, Path(str(live) + "-wal"), Path(str(live) + "-shm"))
        for candidate in (db, wal, shm):
            if any(_paths_equivalent(candidate, item) for item in companions):
                raise IdentityCutoverPreconditionError("downtime_evidence")

    @staticmethod
    def _require_snapshot_companions(db: Path, wal: Path, shm: Path) -> None:
        if not db.is_file() or not wal.exists() or not shm.exists():
            raise IdentityCutoverPreconditionError("snapshot_missing")
        if wal.stat().st_size == 0:
            return
        with wal.open("rb") as handle:
            magic = handle.read(4)
        if magic not in _WAL_MAGIC:
            raise IdentityCutoverPreconditionError("snapshot_missing")

    def require_source_fresh(
        self,
        connection: sqlite3.Connection,
        settings: CutoverSettingsInput,
        stored_payload: dict[str, object],
        snapshot: SnapshotEvidence,
    ) -> dict[str, dict[str, int | str]]:
        live = live_source_manifest(connection)
        stored = stored_payload.get("source")
        if not isinstance(stored, dict) or not manifests_equal(live, stored):
            raise IdentityCutoverError("source_fingerprint")
        stored_snapshot = stored_payload.get("snapshot")
        if not isinstance(stored_snapshot, dict) or stored_snapshot != {
            "db_sha256": snapshot.db_sha256,
            "wal_sha256": snapshot.wal_sha256,
            "shm_sha256": snapshot.shm_sha256,
            "db_size": snapshot.db_size,
            "wal_size": snapshot.wal_size,
            "shm_size": snapshot.shm_size,
        }:
            raise IdentityCutoverError("source_fingerprint")
        snapshot_live = self.snapshot_source_manifest(settings)
        if not manifests_equal(snapshot_live, live):
            raise IdentityCutoverError("source_fingerprint")
        return live

    def require_revision(self, settings: CutoverSettingsInput) -> None:
        expected = settings.expected_git_revision.strip()
        actual = settings.git_revision.strip()
        if not expected or not actual or expected != actual:
            raise IdentityCutoverPreconditionError("git_revision")
        if not settings.downtime_token.strip():
            raise IdentityCutoverPreconditionError("downtime_evidence")

    def require_drained(self, connection: sqlite3.Connection) -> None:
        for table, statuses in _DRAIN_STATUS.items():
            if not _table_exists(connection, table):
                continue
            columns = {str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")')}
            if statuses and "status" in columns:
                query = (
                    f'SELECT COUNT(*) FROM "{table}" WHERE status IN '
                    f"({', '.join('?' for _ in statuses)})"
                )
                params: tuple[object, ...] = statuses
            else:
                evidence: list[str] = []
                if "lease_until" in columns:
                    evidence.append("lease_until IS NOT NULL")
                if "claimed_until" in columns:
                    evidence.append("claimed_until IS NOT NULL")
                if "lease_owner" in columns:
                    evidence.append("lease_owner IS NOT NULL")
                if "lease_token" in columns:
                    evidence.append("lease_token IS NOT NULL")
                if "claimed_by" in columns:
                    evidence.append("claimed_by IS NOT NULL")
                if not evidence:
                    continue
                query = f'SELECT COUNT(*) FROM "{table}" WHERE {" OR ".join(evidence)}'
                params = ()
            count = int(connection.execute(query, params).fetchone()[0])
            if count:
                raise IdentityCutoverPreconditionError("lease_not_drained")

    def require_no_open_conflicts(self, connection: sqlite3.Connection) -> None:
        if not _table_exists(connection, "identity_conflicts"):
            return
        open_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM identity_conflicts WHERE status = 'open'"
            ).fetchone()[0]
        )
        if open_count:
            raise IdentityCutoverPreconditionError("identity_conflicts")

    def require_no_canonical_memory_fact_conflicts(self, connection: sqlite3.Connection) -> None:
        from qq_ai_bot.identity.canonical_memory_schema import memory_fact_canonical_conflict_kind

        if not _table_exists(connection, "memory_facts"):
            return
        if memory_fact_canonical_conflict_kind(connection) is not None:
            raise IdentityCutoverPreconditionError("canonical_memory_fact_conflict")

    def require_c21_readable_owners(self, connection: sqlite3.Connection, cutoff: str) -> None:
        require_c21_readable_owners(connection, cutoff)

    def require_shadows_complete(self, connection: sqlite3.Connection) -> None:
        bindings = self._external_person_ids(connection)
        spaces = self._external_space_ids(connection)
        presences = self._external_presence_ids(connection)
        people_bots = self._people_bot_flags(connection)
        person_ids = self._table_ids(connection, "persons")
        space_ids = self._table_ids(connection, "spaces")
        presence_ids = self._table_ids(connection, "presences")
        self._require_legacy_bot_people_classified(connection, bindings)
        self._require_event_authors_classified(
            connection,
            bindings=bindings,
            presences=presences,
            person_ids=person_ids,
            presence_ids=presence_ids,
        )
        for spec in SHADOW_FILL_SPECS:
            if not self._shadow_spec_ready(connection, spec):
                continue
            if spec.completeness == "shape_only_optional":
                self._require_shape_only_spec(
                    connection,
                    spec,
                    person_ids=person_ids,
                    space_ids=space_ids,
                    presence_ids=presence_ids,
                )
                continue
            if spec.source_column is None:
                raise IdentityCutoverPreconditionError("shadows_incomplete")
            pk_sql = ", ".join(f'"{key}"' for key in spec.pk)
            extra = f', "{spec.role_column}"' if spec.role_column else ""
            rows = connection.execute(
                f'SELECT {pk_sql}, "{spec.column}", "{spec.source_column}"{extra} '
                f'FROM "{spec.table}" WHERE {spec.extra_where}'
            )
            for row in rows:
                if spec.role_column is not None:
                    fillable = str(row[spec.role_column] or "") in HUMAN_PLUGIN_MESSAGE_ROLES
                    if not fillable:
                        if row[spec.column] is not None:
                            raise IdentityCutoverPreconditionError("shadows_incomplete")
                        continue
                source = normalize_external_id(row[spec.source_column])
                current = None if row[spec.column] is None else str(row[spec.column])
                if source is None:
                    continue
                account_class = _persisted_account_class(
                    source,
                    people_bots=people_bots,
                    bindings=bindings,
                    presences=presences,
                )
                resolved = _resolved_canonical(spec.kind, source, bindings, spaces, presences)
                if spec.kind == "person":
                    if account_class in {"yuki_presence", "external_bot"}:
                        if current is not None:
                            raise IdentityCutoverPreconditionError("shadows_incomplete")
                        continue
                    if account_class != "person" or resolved is None or current != resolved:
                        raise IdentityCutoverPreconditionError("shadows_incomplete")
                    continue
                if spec.kind == "presence":
                    if account_class != "yuki_presence":
                        if current is not None:
                            raise IdentityCutoverPreconditionError("shadows_incomplete")
                        continue
                    if resolved is None or current != resolved:
                        raise IdentityCutoverPreconditionError("shadows_incomplete")
                    continue
                if resolved is None or current != resolved:
                    raise IdentityCutoverPreconditionError("shadows_incomplete")

    def _require_event_authors_classified(
        self,
        connection: sqlite3.Connection,
        *,
        bindings: dict[str, str],
        presences: dict[str, str],
        person_ids: set[str],
        presence_ids: set[str],
    ) -> None:
        if not _table_exists(connection, "chat_events"):
            return
        required = ("author_kind", "author_person_id", "author_presence_id", "sender_user_id")
        if any(not _column_exists(connection, "chat_events", column) for column in required):
            raise IdentityCutoverPreconditionError("shadows_incomplete")
        for row in connection.execute(
            "SELECT sender_user_id, author_kind, author_person_id, author_presence_id "
            "FROM chat_events"
        ):
            kind = None if row["author_kind"] is None else str(row["author_kind"])
            if kind not in EVENT_AUTHOR_KINDS:
                raise IdentityCutoverPreconditionError("shadows_incomplete")
            person_id = None if row["author_person_id"] is None else str(row["author_person_id"])
            presence_id = (
                None if row["author_presence_id"] is None else str(row["author_presence_id"])
            )
            sender = normalize_external_id(row["sender_user_id"])
            if kind == "person":
                expected = bindings.get(sender) if sender is not None else None
                if (
                    person_id is None
                    or person_id not in person_ids
                    or expected is None
                    or person_id != expected
                    or presence_id is not None
                ):
                    raise IdentityCutoverPreconditionError("shadows_incomplete")
                continue
            if kind == "yuki":
                expected = presences.get(sender) if sender is not None else None
                if (
                    presence_id is None
                    or presence_id not in presence_ids
                    or expected is None
                    or presence_id != expected
                    or person_id is not None
                ):
                    raise IdentityCutoverPreconditionError("shadows_incomplete")
                continue
            if person_id is not None or presence_id is not None:
                raise IdentityCutoverPreconditionError("shadows_incomplete")

    def _require_shape_only_spec(
        self,
        connection: sqlite3.Connection,
        spec: ShadowFillSpec,
        *,
        person_ids: set[str],
        space_ids: set[str],
        presence_ids: set[str],
    ) -> None:
        parents = {"person": person_ids, "space": space_ids, "presence": presence_ids}[spec.kind]
        selected = [f'"{spec.column}"']
        if spec.pair_column is not None and _column_exists(
            connection, spec.table, spec.pair_column
        ):
            selected.append(f'"{spec.pair_column}"')
        elif spec.pair_column is not None:
            raise IdentityCutoverPreconditionError("shadows_incomplete")
        if spec.scope_column is not None and _column_exists(
            connection, spec.table, spec.scope_column
        ):
            selected.append(f'"{spec.scope_column}"')
        elif spec.scope_column is not None:
            raise IdentityCutoverPreconditionError("shadows_incomplete")
        for row in connection.execute(f'SELECT {", ".join(selected)} FROM "{spec.table}"'):
            current = None if row[spec.column] is None else str(row[spec.column])
            if current is None:
                continue
            if current not in parents:
                raise IdentityCutoverPreconditionError("shadows_incomplete")
            if spec.pair_column is not None and row[spec.pair_column] is not None:
                raise IdentityCutoverPreconditionError("shadows_incomplete")
            if spec.scope_column is not None and spec.required_scope is not None:
                if str(row[spec.scope_column] or "") != spec.required_scope:
                    raise IdentityCutoverPreconditionError("shadows_incomplete")

    def _table_ids(self, connection: sqlite3.Connection, table: str) -> set[str]:
        if not _table_exists(connection, table):
            return set()
        return {str(row[0]) for row in connection.execute(f'SELECT id FROM "{table}"')}

    def _require_legacy_bot_people_classified(
        self,
        connection: sqlite3.Connection,
        bindings: dict[str, str],
    ) -> None:
        if not _table_exists(connection, "people"):
            return
        if not _column_exists(connection, "people", "is_bot"):
            return
        if not _column_exists(connection, "people", "canonical_person_id"):
            return
        for row in connection.execute(
            "SELECT user_id, canonical_person_id FROM people WHERE is_bot = 1"
        ):
            user_id = normalize_external_id(row["user_id"])
            if user_id is None:
                continue
            if user_id in bindings or row["canonical_person_id"] is not None:
                raise IdentityCutoverPreconditionError("shadows_incomplete")

    @staticmethod
    def _shadow_spec_ready(connection: sqlite3.Connection, spec: ShadowFillSpec) -> bool:
        if not _table_exists(connection, spec.table):
            return False
        if not _column_exists(connection, spec.table, spec.column):
            return False
        if spec.source_column is not None and not _column_exists(
            connection, spec.table, spec.source_column
        ):
            return False
        if spec.role_column is not None and not _column_exists(
            connection, spec.table, spec.role_column
        ):
            return False
        return True

    def _external_person_ids(self, connection: sqlite3.Connection) -> dict[str, str]:
        if not _table_exists(connection, "identity_bindings"):
            return {}
        return {
            str(row["external_account_id"]): str(row["person_id"])
            for row in connection.execute(
                "SELECT external_account_id, person_id FROM identity_bindings WHERE platform = ?",
                (IDENTITY_PLATFORM,),
            )
        }

    def _external_space_ids(self, connection: sqlite3.Connection) -> dict[str, str]:
        if not _table_exists(connection, "space_bindings"):
            return {}
        return {
            str(row["external_space_id"]): str(row["space_id"])
            for row in connection.execute(
                "SELECT external_space_id, space_id FROM space_bindings WHERE platform = ?",
                (IDENTITY_PLATFORM,),
            )
        }

    def _external_presence_ids(self, connection: sqlite3.Connection) -> dict[str, str]:
        if not _table_exists(connection, "presences"):
            return {}
        return {
            str(row["external_account_id"]): str(row["id"])
            for row in connection.execute(
                "SELECT external_account_id, id FROM presences WHERE platform = ?",
                (IDENTITY_PLATFORM,),
            )
        }

    def _people_bot_flags(self, connection: sqlite3.Connection) -> dict[str, bool]:
        if not _table_exists(connection, "people") or not _column_exists(
            connection, "people", "is_bot"
        ):
            return {}
        return {
            str(row["user_id"]): bool(int(row["is_bot"]))
            for row in connection.execute("SELECT user_id, is_bot FROM people")
        }

    def require_known_identities(self, connection: sqlite3.Connection) -> None:
        if _table_exists(connection, "people"):
            unknown = connection.execute(
                """
                SELECT p.user_id FROM people AS p
                LEFT JOIN identity_bindings AS b
                  ON b.platform = ? AND b.external_account_id = p.user_id
                LEFT JOIN presences AS pr
                  ON pr.platform = ? AND pr.external_account_id = p.user_id
                WHERE p.is_bot = 0 AND b.id IS NULL AND pr.id IS NULL
                """,
                (IDENTITY_PLATFORM, IDENTITY_PLATFORM),
            ).fetchone()
            if unknown is not None:
                raise IdentityCutoverPreconditionError("unknown_identity")
        if _table_exists(connection, "groups"):
            unknown_space = connection.execute(
                """
                SELECT g.group_id FROM groups AS g
                LEFT JOIN space_bindings AS b
                  ON b.platform = ? AND b.external_space_id = g.group_id
                WHERE b.id IS NULL
                """,
                (IDENTITY_PLATFORM,),
            ).fetchone()
            if unknown_space is not None:
                raise IdentityCutoverPreconditionError("unknown_identity")

    def _presence_states(self, connection: sqlite3.Connection) -> dict[str, tuple[str, bool, bool]]:
        return {
            str(row["id"]): (
                str(row["platform"]),
                bool(int(row["enabled"])),
                bool(int(row["ingest_eligible"])),
            )
            for row in connection.execute(
                "SELECT id, platform, enabled, ingest_eligible FROM presences"
            )
        }

    def _space_enabled(self, connection: sqlite3.Connection, space_id: str) -> bool:
        row = connection.execute("SELECT enabled FROM spaces WHERE id = ?", (space_id,)).fetchone()
        return row is not None and bool(int(row["enabled"]))

    def _required_person_ids(self, connection: sqlite3.Connection) -> tuple[str, ...]:
        return tuple(
            str(row["person_id"])
            for row in connection.execute(
                "SELECT DISTINCT person_id FROM identity_bindings "
                "WHERE status = 'active' ORDER BY person_id"
            )
        )

    def _required_space_bindings(self, connection: sqlite3.Connection) -> tuple[sqlite3.Row, ...]:
        return tuple(
            connection.execute(
                """
                SELECT b.id, b.space_id, b.platform, b.status
                FROM space_bindings AS b
                JOIN spaces AS s ON s.id = b.space_id
                WHERE b.status = 'active' AND s.enabled = 1
                ORDER BY b.created_at, b.id
                """
            )
        )

    def _required_space_ids(self, connection: sqlite3.Connection) -> tuple[str, ...]:
        return tuple(
            str(row["id"])
            for row in connection.execute(
                """
                SELECT DISTINCT s.id
                FROM spaces AS s
                JOIN space_bindings AS b ON b.space_id = s.id
                WHERE s.enabled = 1 AND b.status = 'active'
                ORDER BY s.id
                """
            )
        )

    def _person_route_valid(
        self,
        connection: sqlite3.Connection,
        route: sqlite3.Row,
        *,
        person_id: str,
        presence_states: dict[str, tuple[str, bool, bool]],
    ) -> bool:
        binding = connection.execute(
            "SELECT id, person_id, platform, status FROM identity_bindings WHERE id = ?",
            (route["identity_binding_id"],),
        ).fetchone()
        if binding is None or str(binding["person_id"]) != person_id:
            return False
        if str(binding["status"]) != "active":
            return False
        state = presence_states.get(str(route["presence_id"]))
        if state is None or not state[1]:
            return False
        if state[0] != str(binding["platform"]):
            return False
        paused = route["paused"]
        if paused is None or int(paused) not in {0, 1}:
            return False
        return True

    def _space_ingest_valid(
        self,
        connection: sqlite3.Connection,
        route: sqlite3.Row,
        *,
        binding: sqlite3.Row,
        presence_states: dict[str, tuple[str, bool, bool]],
    ) -> bool:
        if str(route["space_binding_id"]) != str(binding["id"]):
            return False
        if str(binding["status"]) != "active":
            return False
        if not self._space_enabled(connection, str(binding["space_id"])):
            return False
        state = presence_states.get(str(route["ingest_presence_id"]))
        if state is None or not state[1]:
            return False
        if state[0] != str(binding["platform"]):
            return False
        paused = route["paused"]
        if paused is None or int(paused) not in {0, 1}:
            return False
        if int(paused) == 0 and not state[2]:
            return False
        return True

    def _space_active_valid(
        self,
        connection: sqlite3.Connection,
        route: sqlite3.Row,
        *,
        space_id: str,
        presence_states: dict[str, tuple[str, bool, bool]],
    ) -> bool:
        if str(route["space_id"]) != space_id:
            return False
        if not self._space_enabled(connection, space_id):
            return False
        binding = connection.execute(
            "SELECT id, space_id, platform, status FROM space_bindings WHERE id = ?",
            (route["space_binding_id"],),
        ).fetchone()
        if binding is None or str(binding["space_id"]) != space_id:
            return False
        if str(binding["status"]) != "active":
            return False
        state = presence_states.get(str(route["presence_id"]))
        if state is None or not state[1]:
            return False
        if state[0] != str(binding["platform"]):
            return False
        paused = route["paused"]
        if paused is None or int(paused) not in {0, 1}:
            return False
        return True

    def _validate_existing_routes(
        self,
        connection: sqlite3.Connection,
        presence_states: dict[str, tuple[str, bool, bool]],
    ) -> None:
        for route in connection.execute(
            "SELECT person_id, identity_binding_id, presence_id, paused FROM person_active_routes"
        ):
            if not self._person_route_valid(
                connection,
                route,
                person_id=str(route["person_id"]),
                presence_states=presence_states,
            ):
                raise IdentityCutoverPreconditionError("route_ambiguity")
        for ingest in connection.execute(
            "SELECT space_binding_id, ingest_presence_id, paused FROM space_binding_ingest_routes"
        ):
            binding = connection.execute(
                "SELECT id, space_id, platform, status FROM space_bindings WHERE id = ?",
                (ingest["space_binding_id"],),
            ).fetchone()
            if binding is None or not self._space_ingest_valid(
                connection,
                ingest,
                binding=binding,
                presence_states=presence_states,
            ):
                raise IdentityCutoverPreconditionError("route_ambiguity")
        for active in connection.execute(
            "SELECT space_id, space_binding_id, presence_id, paused FROM space_active_routes"
        ):
            if not self._space_active_valid(
                connection,
                active,
                space_id=str(active["space_id"]),
                presence_states=presence_states,
            ):
                raise IdentityCutoverPreconditionError("route_ambiguity")

    def _require_exact_required_routes(
        self,
        connection: sqlite3.Connection,
        presence_states: dict[str, tuple[str, bool, bool]],
    ) -> None:
        for person_id in self._required_person_ids(connection):
            routes = list(
                connection.execute(
                    "SELECT person_id, identity_binding_id, presence_id, paused "
                    "FROM person_active_routes WHERE person_id = ?",
                    (person_id,),
                )
            )
            valid = [
                route
                for route in routes
                if self._person_route_valid(
                    connection,
                    route,
                    person_id=person_id,
                    presence_states=presence_states,
                )
            ]
            if len(valid) != 1:
                raise IdentityCutoverPreconditionError("route_ambiguity")
        for binding in self._required_space_bindings(connection):
            ingest = connection.execute(
                "SELECT space_binding_id, ingest_presence_id, paused "
                "FROM space_binding_ingest_routes WHERE space_binding_id = ?",
                (binding["id"],),
            ).fetchone()
            if ingest is None or not self._space_ingest_valid(
                connection,
                ingest,
                binding=binding,
                presence_states=presence_states,
            ):
                raise IdentityCutoverPreconditionError("route_ambiguity")
        for space_id in self._required_space_ids(connection):
            routes = list(
                connection.execute(
                    "SELECT space_id, space_binding_id, presence_id, paused "
                    "FROM space_active_routes WHERE space_id = ?",
                    (space_id,),
                )
            )
            valid = [
                route
                for route in routes
                if self._space_active_valid(
                    connection,
                    route,
                    space_id=space_id,
                    presence_states=presence_states,
                )
            ]
            if len(valid) != 1:
                raise IdentityCutoverPreconditionError("route_ambiguity")

    def _require_single_presence_defaults_safe(
        self,
        connection: sqlite3.Connection,
        presence_states: dict[str, tuple[str, bool, bool]],
    ) -> None:
        if len(presence_states) != 1:
            return
        platform, enabled, _eligible = next(iter(presence_states.values()))
        for person_id in self._required_person_ids(connection):
            existing = connection.execute(
                "SELECT 1 FROM person_active_routes WHERE person_id = ?",
                (person_id,),
            ).fetchone()
            if existing is not None:
                continue
            matches = self._active_owner_bindings(
                connection,
                table="identity_bindings",
                owner_column="person_id",
                owner_id=person_id,
                platform=platform,
            )
            if not enabled or len(matches) != 1:
                raise IdentityCutoverPreconditionError("route_ambiguity")
        for binding in self._required_space_bindings(connection):
            existing = connection.execute(
                "SELECT 1 FROM space_binding_ingest_routes WHERE space_binding_id = ?",
                (binding["id"],),
            ).fetchone()
            if existing is not None:
                continue
            if not enabled or str(binding["platform"]) != platform:
                raise IdentityCutoverPreconditionError("route_ambiguity")
        for space_id in self._required_space_ids(connection):
            existing = connection.execute(
                "SELECT 1 FROM space_active_routes WHERE space_id = ?",
                (space_id,),
            ).fetchone()
            if existing is not None:
                continue
            matches = self._active_owner_bindings(
                connection,
                table="space_bindings",
                owner_column="space_id",
                owner_id=space_id,
                platform=platform,
            )
            if not enabled or len(matches) != 1:
                raise IdentityCutoverPreconditionError("route_ambiguity")

    @staticmethod
    def _active_owner_bindings(
        connection: sqlite3.Connection,
        *,
        table: str,
        owner_column: str,
        owner_id: str,
        platform: str,
    ) -> tuple[str, ...]:
        if table not in {"identity_bindings", "space_bindings"}:
            raise IdentityCutoverPreconditionError("route_ambiguity")
        if owner_column not in {"person_id", "space_id"}:
            raise IdentityCutoverPreconditionError("route_ambiguity")
        return tuple(
            str(row["id"])
            for row in connection.execute(
                f"SELECT id FROM {table} WHERE {owner_column} = ? "
                f"AND status = 'active' AND platform = ?",
                (owner_id, platform),
            )
        )

    def _unique_active_binding_id(
        self,
        connection: sqlite3.Connection,
        *,
        table: str,
        owner_column: str,
        owner_id: str,
        platform: str,
    ) -> str:
        matches = self._active_owner_bindings(
            connection,
            table=table,
            owner_column=owner_column,
            owner_id=owner_id,
            platform=platform,
        )
        if len(matches) != 1:
            raise IdentityCutoverPreconditionError("route_ambiguity")
        return matches[0]

    def require_routes_decidable(self, connection: sqlite3.Connection) -> None:
        presence_count = int(connection.execute("SELECT COUNT(*) FROM presences").fetchone()[0])
        if presence_count == 0:
            needs_route = connection.execute(
                "SELECT 1 FROM space_bindings UNION ALL "
                "SELECT 1 FROM identity_bindings WHERE status = 'active' LIMIT 1"
            ).fetchone()
            if needs_route is not None:
                raise IdentityCutoverPreconditionError("route_ambiguity")
            return
        presence_states = self._presence_states(connection)
        self._validate_existing_routes(connection, presence_states)
        if presence_count > 1:
            self._require_exact_required_routes(connection, presence_states)
            return
        self._require_single_presence_defaults_safe(connection, presence_states)

    def _route_default_applicability(self, connection: sqlite3.Connection) -> dict[str, object]:
        presence_count = int(connection.execute("SELECT COUNT(*) FROM presences").fetchone()[0])
        states = self._presence_states(connection) if presence_count else {}
        if presence_count == 1 and states:
            platform, enabled, ingest_eligible = next(iter(states.values()))
            default_applicable = True
        else:
            platform, enabled, ingest_eligible = "", False, False
            default_applicable = False
        persons = [
            {
                "has_route": connection.execute(
                    "SELECT 1 FROM person_active_routes WHERE person_id = ?",
                    (person_id,),
                ).fetchone()
                is not None,
                "owner_id": person_id,
            }
            for person_id in self._required_person_ids(connection)
        ]
        space_bindings = [
            {
                "binding_id": str(binding["id"]),
                "has_ingest_route": connection.execute(
                    "SELECT 1 FROM space_binding_ingest_routes WHERE space_binding_id = ?",
                    (binding["id"],),
                ).fetchone()
                is not None,
                "platform": str(binding["platform"]),
                "space_id": str(binding["space_id"]),
            }
            for binding in self._required_space_bindings(connection)
        ]
        spaces = [
            {
                "has_active_route": connection.execute(
                    "SELECT 1 FROM space_active_routes WHERE space_id = ?",
                    (space_id,),
                ).fetchone()
                is not None,
                "space_id": space_id,
            }
            for space_id in self._required_space_ids(connection)
        ]
        return {
            "default_applicable": default_applicable,
            "enabled": enabled,
            "ingest_eligible": ingest_eligible,
            "persons": persons,
            "platform": platform,
            "presence_count": presence_count,
            "space_bindings": space_bindings,
            "spaces": spaces,
        }

    def require_no_pending_preconfig(self, connection: sqlite3.Connection) -> int:
        if not _table_exists(connection, "control_command_receipts"):
            return 0
        columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(control_command_receipts)")
        }
        if "problem_code" in columns:
            pending = int(
                connection.execute(
                    "SELECT COUNT(*) FROM control_command_receipts "
                    "WHERE problem_code = 'pending_cutover'"
                ).fetchone()[0]
            )
        elif "outcome" in columns:
            pending = int(
                connection.execute(
                    "SELECT COUNT(*) FROM control_command_receipts "
                    "WHERE outcome = 'pending_cutover'"
                ).fetchone()[0]
            )
        elif "status" in columns:
            pending = int(
                connection.execute(
                    "SELECT COUNT(*) FROM control_command_receipts WHERE status = 'pending_cutover'"
                ).fetchone()[0]
            )
        else:
            pending = 0
        if pending:
            raise IdentityCutoverPreconditionError("pending_preconfiguration")
        return pending

    def _canonical_owner(
        self, connection: sqlite3.Connection, row: sqlite3.Row
    ) -> tuple[str, str] | None:
        if row["group_id"]:
            space = connection.execute(
                "SELECT space_id FROM space_bindings WHERE platform = ? AND external_space_id = ?",
                (IDENTITY_PLATFORM, row["group_id"]),
            ).fetchone()
            if space is None:
                return None
            return ("space", str(space["space_id"]))
        peer = row["private_peer_user_id"] or row["sender_user_id"]
        person = connection.execute(
            "SELECT person_id FROM identity_bindings "
            "WHERE platform = ? AND external_account_id = ?",
            (IDENTITY_PLATFORM, peer),
        ).fetchone()
        if person is None:
            return None
        return ("private", str(person["person_id"]))

    def _exact_sender_key(self, connection: sqlite3.Connection, row: sqlite3.Row) -> str:
        sender = normalize_external_id(row["sender_user_id"]) or ""
        presence = connection.execute(
            "SELECT id FROM presences WHERE platform = ? AND external_account_id = ?",
            (IDENTITY_PLATFORM, sender),
        ).fetchone()
        if presence is not None:
            return f"presence:{presence['id']}"
        binding = connection.execute(
            "SELECT id FROM identity_bindings WHERE platform = ? AND external_account_id = ?",
            (IDENTITY_PLATFORM, sender),
        ).fetchone()
        if binding is not None:
            return f"binding:{binding['id']}"
        author_kind = str(row["author_kind"] or "") if "author_kind" in row.keys() else ""
        if author_kind == "system":
            return "system"
        return f"unbound:{sender}"

    def classify_duplicates(self, connection: sqlite3.Connection) -> tuple[DuplicateDecision, ...]:
        if not _table_exists(connection, "chat_events"):
            return ()
        rows = connection.execute(
            """
            SELECT e.id, e.group_id, e.private_peer_user_id, e.sender_user_id,
                   e.platform_message_id, e.event_kind, e.content, e.segments_json,
                   e.occurred_at, e.scope_type, e.author_kind
            FROM chat_events AS e
            ORDER BY e.id
            """
        ).fetchall()
        buckets: dict[tuple[str, ...], list[sqlite3.Row]] = {}
        message_groups: dict[tuple[str, str, str, str], list[sqlite3.Row]] = {}
        decisions: list[DuplicateDecision] = []
        for row in rows:
            owner = self._canonical_owner(connection, row)
            if owner is None:
                continue
            sender_key = self._exact_sender_key(connection, row)
            message_id = str(row["platform_message_id"] or "").strip()
            if not message_id:
                continue
            event_type = str(row["event_kind"] or "")
            content = normalize_event_text(row["content"])
            segments = normalize_segments_json(row["segments_json"])
            occurred = str(row["occurred_at"] or "")
            identity_key = (owner[0], owner[1], sender_key, message_id)
            content_key = (event_type, content, segments, occurred)
            buckets.setdefault((*identity_key, *content_key), []).append(row)
            message_groups.setdefault(identity_key, []).append(row)
        for identity_key, group in message_groups.items():
            contents = {
                (
                    str(item["event_kind"] or ""),
                    normalize_event_text(item["content"]),
                    normalize_segments_json(item["segments_json"]),
                    str(item["occurred_at"] or ""),
                )
                for item in group
            }
            if identity_key[3] and len(contents) > 1:
                raise IdentityCutoverPreconditionError("duplicate_content_conflict")
        for key, group in buckets.items():
            if len(group) < 2:
                continue
            sender_key = str(key[2])
            sender_id = sender_key.split(":", 1)[1] if ":" in sender_key else sender_key
            decisions.append(
                DuplicateDecision(
                    kind="suppress",
                    space_or_person=fingerprint_external_id(str(key[1])),
                    sender_binding_id=sender_id,
                    platform_message_id_fingerprint=fingerprint_external_id(str(key[3])),
                    event_ids=tuple(int(item["id"]) for item in group),
                    keeper_event_id=int(group[0]["id"]),
                )
            )
        return tuple(decisions)

    def _presence_accounts(self, connection: sqlite3.Connection) -> tuple[str, ...]:
        if not _table_exists(connection, "presences"):
            return ()
        return tuple(
            str(row["external_account_id"])
            for row in connection.execute(
                "SELECT external_account_id FROM presences ORDER BY created_at, id"
            )
        )

    def _scopes_for_owner(
        self,
        connection: sqlite3.Connection,
        *,
        kind: str,
        externals: tuple[str, ...],
        presence_accounts: tuple[str, ...],
    ) -> tuple[sqlite3.Row, ...]:
        if not externals or not _table_exists(connection, "conversation_scopes"):
            return ()
        placeholders = ", ".join("?" for _ in externals)
        if kind == "space":
            query = (
                f"SELECT id, scope_key, bot_user_id, scope_type, group_id, "
                f"private_peer_user_id, generation, last_event_id, starts_after_event_id "
                f"FROM conversation_scopes WHERE group_id IN ({placeholders}) ORDER BY id"
            )
            return tuple(connection.execute(query, externals))
        query = (
            f"SELECT id, scope_key, bot_user_id, scope_type, group_id, "
            f"private_peer_user_id, generation, last_event_id, starts_after_event_id "
            f"FROM conversation_scopes WHERE private_peer_user_id IN ({placeholders}) "
            f"ORDER BY id"
        )
        scopes = tuple(connection.execute(query, externals))
        del presence_accounts
        return scopes

    def _scope_suffix_events(
        self,
        connection: sqlite3.Connection,
        scope: sqlite3.Row,
        covered: int,
        suppress_event_ids: frozenset[int],
    ) -> tuple[sqlite3.Row, ...]:
        if not _table_exists(connection, "chat_events"):
            return ()
        if scope["scope_type"] == "group":
            rows = tuple(
                connection.execute(
                    """
                    SELECT id, content FROM chat_events
                    WHERE bot_user_id = ? AND scope_type = 'group' AND group_id = ?
                      AND id > ? AND id <= ?
                    ORDER BY id
                    """,
                    (
                        scope["bot_user_id"],
                        scope["group_id"],
                        covered,
                        int(scope["last_event_id"]),
                    ),
                )
            )
        else:
            rows = tuple(
                connection.execute(
                    """
                    SELECT id, content FROM chat_events
                    WHERE bot_user_id = ? AND scope_type = 'private'
                      AND private_peer_user_id = ? AND id > ? AND id <= ?
                    ORDER BY id
                    """,
                    (
                        scope["bot_user_id"],
                        scope["private_peer_user_id"],
                        covered,
                        int(scope["last_event_id"]),
                    ),
                )
            )
        return tuple(row for row in rows if int(row["id"]) not in suppress_event_ids)

    def _migration_for_scopes(
        self,
        connection: sqlite3.Connection,
        scopes: tuple[sqlite3.Row, ...],
        suppress_event_ids: frozenset[int],
    ) -> tuple[str, str, int, int, int]:
        materials: list[_MigrationScopeMaterial] = []
        last_event_id = 0
        generation = 1
        suffix_event_total = 0
        rollups_ready = _table_exists(connection, "conversation_rollups")
        for scope in scopes:
            last_event_id = max(last_event_id, int(scope["last_event_id"]))
            generation = max(generation, int(scope["generation"]))
            rollup = None
            if rollups_ready:
                rollup = connection.execute(
                    """
                    SELECT covered_through_event_id, summary_text, source_fingerprint
                    FROM conversation_rollups
                    WHERE scope_id = ? AND generation = ?
                    ORDER BY revision DESC LIMIT 1
                    """,
                    (scope["id"], scope["generation"]),
                ).fetchone()
            covered = (
                int(rollup["covered_through_event_id"])
                if rollup is not None
                else int(scope["starts_after_event_id"])
            )
            summary = str(rollup["summary_text"] or "").strip() if rollup is not None else ""
            suffix = self._scope_suffix_events(connection, scope, covered, suppress_event_ids)
            events = tuple(text for item in suffix if (text := str(item["content"] or "").strip()))
            suffix_event_total += len(events)
            materials.append(_MigrationScopeMaterial(semantic=summary, suffix_events=events))
        material = _assemble_migration_summary(tuple(materials))
        fingerprint = _migration_summary_fingerprint(
            material,
            last_event_id=last_event_id,
            covered_through_event_id=last_event_id,
            generation=generation,
            scope_count=len(scopes),
            suffix_event_total=suffix_event_total,
        )
        return material, fingerprint, last_event_id, last_event_id, generation

    def _owner_event_high_watermark(
        self,
        connection: sqlite3.Connection,
        *,
        kind: str,
        externals: tuple[str, ...],
    ) -> int:
        if not externals or not _table_exists(connection, "chat_events"):
            return 0
        placeholders = ", ".join("?" for _ in externals)
        if kind == "space":
            return int(
                connection.execute(
                    f"SELECT COALESCE(MAX(id), 0) FROM chat_events "
                    f"WHERE group_id IN ({placeholders})",
                    externals,
                ).fetchone()[0]
            )
        return int(
            connection.execute(
                f"SELECT COALESCE(MAX(id), 0) FROM chat_events "
                f"WHERE scope_type = 'private' AND private_peer_user_id IN ({placeholders})",
                externals,
            ).fetchone()[0]
        )

    def _proven_event_scope_key(
        self,
        connection: sqlite3.Connection,
        *,
        kind: str,
        externals: tuple[str, ...],
    ) -> str:
        if not externals or not _table_exists(connection, "chat_events"):
            return ""
        placeholders = ", ".join("?" for _ in externals)
        if kind == "space":
            rows = connection.execute(
                f"SELECT bot_user_id, group_id FROM chat_events "
                f"WHERE group_id IN ({placeholders}) ORDER BY id",
                externals,
            )
        else:
            rows = connection.execute(
                f"SELECT bot_user_id, private_peer_user_id FROM chat_events "
                f"WHERE scope_type = 'private' AND private_peer_user_id IN ({placeholders}) "
                f"ORDER BY id",
                externals,
            )
        for row in rows:
            bot = str(row["bot_user_id"] or "").strip()
            target = (
                str(row["group_id"] or "").strip()
                if kind == "space"
                else str(row["private_peer_user_id"] or "").strip()
            )
            if not bot or not target:
                continue
            key = f"bot:{bot}:group:{target}" if kind == "space" else f"bot:{bot}:private:{target}"
            if self._is_real_scope_key(key):
                return key
        return ""

    @staticmethod
    def _is_real_scope_key(key: str) -> bool:
        return bool(key) and not key.startswith(_CUTOVER_ALIAS_PREFIX)

    def _watermark_for_owner(
        self,
        connection: sqlite3.Connection,
        *,
        kind: str,
        owner_id: str,
        scopes: tuple[sqlite3.Row, ...],
        presence_accounts: tuple[str, ...],
        fallback_externals: tuple[str, ...],
        suppress_event_ids: frozenset[int],
    ) -> ConversationWatermark:
        del presence_accounts
        if scopes:
            primary_key = str(scopes[0]["scope_key"])
            if not self._is_real_scope_key(primary_key):
                raise IdentityCutoverPreconditionError("canonical_kind_mismatch")
            covered_keys = tuple(str(scope["scope_key"]) for scope in scopes)
            summary, fingerprint, last_id, covered, generation = self._migration_for_scopes(
                connection, scopes, suppress_event_ids
            )
        else:
            last_id = self._owner_event_high_watermark(
                connection, kind=kind, externals=fallback_externals
            )
            primary_key = (
                self._proven_event_scope_key(connection, kind=kind, externals=fallback_externals)
                if last_id
                else ""
            )
            if last_id and not self._is_real_scope_key(primary_key):
                raise IdentityCutoverPreconditionError("canonical_kind_mismatch")
            covered_keys = (primary_key,) if primary_key else ()
            summary = _MIGRATION_EMPTY_SUMMARY
            fingerprint = hashlib.sha256(f"empty:{owner_id}".encode()).hexdigest()
            covered = last_id
            generation = 1
        return ConversationWatermark(
            kind=kind,  # type: ignore[arg-type]
            owner_id=owner_id,
            primary_scope_key=primary_key,
            primary_scope_key_fingerprint=fingerprint_external_id(primary_key or owner_id),
            covered_scope_keys=covered_keys,
            last_event_id=last_id,
            covered_through_event_id=covered,
            generation=generation,
            rollup_fingerprint=fingerprint[:64],
            suffix_event_count=0,
            migration_summary=summary,
        )

    def conversation_watermarks(
        self,
        connection: sqlite3.Connection,
        *,
        suppress_event_ids: frozenset[int] = frozenset(),
    ) -> tuple[ConversationWatermark, ...]:
        marks: list[ConversationWatermark] = []
        presence_accounts = self._presence_accounts(connection)
        if _table_exists(connection, "spaces"):
            for space in connection.execute("SELECT id FROM spaces ORDER BY id"):
                bindings = list(
                    connection.execute(
                        "SELECT external_space_id FROM space_bindings WHERE space_id = ? "
                        "ORDER BY created_at, id",
                        (space["id"],),
                    )
                )
                externals = tuple(str(item["external_space_id"]) for item in bindings)
                scopes = self._scopes_for_owner(
                    connection,
                    kind="space",
                    externals=externals,
                    presence_accounts=presence_accounts,
                )
                if not scopes and not externals:
                    continue
                mark = self._watermark_for_owner(
                    connection,
                    kind="space",
                    owner_id=str(space["id"]),
                    scopes=scopes,
                    presence_accounts=presence_accounts,
                    fallback_externals=externals,
                    suppress_event_ids=suppress_event_ids,
                )
                if not scopes and mark.last_event_id == 0:
                    continue
                marks.append(mark)
        if _table_exists(connection, "persons"):
            for person in connection.execute("SELECT id FROM persons ORDER BY id"):
                bindings = list(
                    connection.execute(
                        "SELECT external_account_id FROM identity_bindings "
                        "WHERE person_id = ? ORDER BY created_at, id",
                        (person["id"],),
                    )
                )
                externals = tuple(str(item["external_account_id"]) for item in bindings)
                scopes = self._scopes_for_owner(
                    connection,
                    kind="private",
                    externals=externals,
                    presence_accounts=presence_accounts,
                )
                if not scopes and not externals:
                    continue
                mark = self._watermark_for_owner(
                    connection,
                    kind="private",
                    owner_id=str(person["id"]),
                    scopes=scopes,
                    presence_accounts=presence_accounts,
                    fallback_externals=externals,
                    suppress_event_ids=suppress_event_ids,
                )
                if not scopes and mark.last_event_id == 0:
                    continue
                marks.append(mark)
        return tuple(marks)

    def build_plan(
        self,
        connection: sqlite3.Connection,
        settings: CutoverSettingsInput,
        snapshot: SnapshotEvidence,
        *,
        c21_cutoff: str | None = None,
    ) -> CutoverPlan:
        self.require_revision(settings)
        self.require_drained(connection)
        self.require_no_open_conflicts(connection)
        self.require_no_canonical_memory_fact_conflicts(connection)
        self.require_shadows_complete(connection)
        cutoff = c21_cutoff or snapshot_c21_cutoff(settings.snapshot_db)
        self.require_c21_readable_owners(connection, cutoff)
        self.require_known_identities(connection)
        self.require_routes_decidable(connection)
        pending = self.require_no_pending_preconfig(connection)
        duplicates = self.classify_duplicates(connection)
        suppress_ids = tuple(
            event_id
            for item in duplicates
            if item.kind == "suppress" and item.keeper_event_id is not None
            for event_id in item.event_ids
            if event_id != item.keeper_event_id
        )
        watermarks = self.conversation_watermarks(
            connection, suppress_event_ids=frozenset(suppress_ids)
        )
        self._require_conversations_alignable(connection, watermarks)
        require_planned_evidence_alignable(
            connection,
            watermarks=watermarks,
            suppress_event_ids=suppress_ids,
        )
        require_c22_runnable_automation_targets(connection)
        require_c23_plugin_targets(connection)
        source = live_source_manifest(connection)
        snapshot_source = self.snapshot_source_manifest(settings)
        if not manifests_equal(source, snapshot_source):
            raise IdentityCutoverPreconditionError("source_fingerprint")
        fingerprint = source_manifest_fingerprint(source)
        digest = compute_decision_digest(
            conversations=watermarks,
            duplicates=duplicates,
            suppress_event_ids=suppress_ids,
            pending_preconfig=pending,
            route_default_applicability=self._route_default_applicability(connection),
            c21_readable_cutoff=cutoff,
        )
        return CutoverPlan(
            source_fingerprint=fingerprint,
            decision_digest=digest,
            source_manifest=source,
            snapshot=snapshot,
            git_revision=settings.git_revision,
            conversations=watermarks,
            duplicates=duplicates,
            suppress_event_ids=suppress_ids,
            conflict_event_groups=0,
            pending_preconfig=pending,
            drain_ok=True,
            shadows_complete=True,
            c21_readable_cutoff=cutoff,
        )

    def schema_signature(self, connection: sqlite3.Connection) -> str:
        rows = connection.execute(
            "SELECT type, name, sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
        ).fetchall()
        material = "\n".join(f"{row[0]}|{row[1]}|{row[2] or ''}" for row in rows)
        return hashlib.sha256(material.encode()).hexdigest()

    def business_signature(self, connection: sqlite3.Connection) -> str:
        return source_manifest_fingerprint(live_source_manifest(connection))

    def persist_manifest(self, connection: sqlite3.Connection, plan: CutoverPlan) -> None:
        payload = self._canonical_manifest_payload(plan)
        existing = connection.execute(
            "SELECT payload_json FROM identity_cutover_manifests WHERE fingerprint = ?",
            (plan.source_fingerprint,),
        ).fetchone()
        if existing is not None:
            if str(existing["payload_json"]) == payload:
                return
            raise IdentityCutoverPreconditionError("decision_digest")
        connection.execute(
            "INSERT INTO identity_cutover_manifests (fingerprint, payload_json, created_at) "
            "VALUES (?, ?, ?)",
            (plan.source_fingerprint, payload, utc_now_text()),
        )

    @classmethod
    def _canonical_manifest_payload(cls, plan: CutoverPlan) -> str:
        return json.dumps(
            cls._safe_manifest_payload(plan),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )

    @staticmethod
    def _safe_manifest_payload(plan: CutoverPlan) -> dict[str, object]:
        if not is_sha256_hex(plan.decision_digest):
            raise IdentityCutoverPreconditionError("decision_digest")
        return {
            "c21_readable_cutoff": plan.c21_readable_cutoff,
            "conversation_count": len(plan.conversations),
            "decision_digest": plan.decision_digest,
            "duplicate_count": len(plan.duplicates),
            "git_revision": plan.git_revision,
            "inventory": CUTOVER_INVENTORY_VERSION,
            "pending_preconfig": plan.pending_preconfig,
            "snapshot": {
                "db_sha256": plan.snapshot.db_sha256,
                "db_size": plan.snapshot.db_size,
                "shm_sha256": plan.snapshot.shm_sha256,
                "shm_size": plan.snapshot.shm_size,
                "wal_sha256": plan.snapshot.wal_sha256,
                "wal_size": plan.snapshot.wal_size,
            },
            "source": plan.source_manifest,
            "source_fingerprint": plan.source_fingerprint,
            "suppress_event_count": len(plan.suppress_event_ids),
        }

    @staticmethod
    def manifest_decision_digest(payload: dict[str, object]) -> str:
        digest = payload.get("decision_digest")
        if not is_sha256_hex(digest):
            raise IdentityCutoverError("decision_digest")
        return str(digest)

    def record_run(
        self,
        connection: sqlite3.Connection,
        settings: CutoverSettingsInput,
        *,
        mode: str,
        status: str,
        source_fingerprint: str | None,
        error_category: str | None,
        snapshot: SnapshotEvidence | None = None,
    ) -> None:
        now = utc_now_text()
        db_field, wal_field, shm_field = _privacy_snapshot_fields(snapshot)
        connection.execute(
            """
            INSERT INTO identity_cutover_runs (
                mode, status, git_revision, downtime_token,
                snapshot_db, snapshot_wal, snapshot_shm,
                source_fingerprint, error_category, created_at, finished_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                mode,
                status,
                settings.git_revision,
                downtime_token_digest(settings.downtime_token),
                db_field,
                wal_field,
                shm_field,
                source_fingerprint,
                error_category,
                now,
                now,
            ),
        )

    def load_manifest_fingerprint(self, connection: sqlite3.Connection, fingerprint: str) -> str:
        return str(self.load_manifest_payload(connection, fingerprint)["source_fingerprint"])

    def load_manifest_payload(
        self, connection: sqlite3.Connection, fingerprint: str
    ) -> dict[str, object]:
        row = connection.execute(
            "SELECT fingerprint, payload_json FROM identity_cutover_manifests "
            "WHERE fingerprint = ?",
            (fingerprint,),
        ).fetchone()
        if row is None:
            raise IdentityCutoverPreconditionError("manifest_missing")
        payload = json.loads(str(row["payload_json"]))
        if not isinstance(payload, dict):
            raise IdentityCutoverPreconditionError("manifest_missing")
        return payload

    def _apply_default_routes(
        self, connection: sqlite3.Connection, presence_id: str, now: str
    ) -> None:
        presence_count = int(connection.execute("SELECT COUNT(*) FROM presences").fetchone()[0])
        if presence_count != 1:
            return
        states = self._presence_states(connection)
        state = states.get(presence_id)
        if state is None:
            raise IdentityCutoverPreconditionError("route_ambiguity")
        platform, enabled, ingest_eligible = state
        if not enabled:
            if self._required_person_ids(connection) or self._required_space_ids(connection):
                raise IdentityCutoverPreconditionError("route_ambiguity")
            return
        ingest_paused = 0 if ingest_eligible else 1
        for binding in self._required_space_bindings(connection):
            if str(binding["platform"]) != platform:
                raise IdentityCutoverPreconditionError("route_ambiguity")
            existing = connection.execute(
                "SELECT 1 FROM space_binding_ingest_routes WHERE space_binding_id = ?",
                (binding["id"],),
            ).fetchone()
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO space_binding_ingest_routes (
                        space_binding_id, ingest_presence_id, route_generation,
                        paused, revision, created_at, updated_at
                    ) VALUES (?, ?, 1, ?, 1, ?, ?)
                    """,
                    (binding["id"], presence_id, ingest_paused, now, now),
                )
        for space_id in self._required_space_ids(connection):
            existing = connection.execute(
                "SELECT 1 FROM space_active_routes WHERE space_id = ?",
                (space_id,),
            ).fetchone()
            if existing is not None:
                continue
            binding_id = self._unique_active_binding_id(
                connection,
                table="space_bindings",
                owner_column="space_id",
                owner_id=space_id,
                platform=platform,
            )
            connection.execute(
                """
                INSERT INTO space_active_routes (
                    space_id, space_binding_id, presence_id, route_generation,
                    paused, revision, created_at, updated_at
                ) VALUES (?, ?, ?, 1, 0, 1, ?, ?)
                """,
                (space_id, binding_id, presence_id, now, now),
            )
        for person_id in self._required_person_ids(connection):
            existing = connection.execute(
                "SELECT 1 FROM person_active_routes WHERE person_id = ?",
                (person_id,),
            ).fetchone()
            if existing is not None:
                continue
            binding_id = self._unique_active_binding_id(
                connection,
                table="identity_bindings",
                owner_column="person_id",
                owner_id=person_id,
                platform=platform,
            )
            connection.execute(
                """
                INSERT INTO person_active_routes (
                    person_id, identity_binding_id, presence_id, route_generation,
                    paused, revision, created_at, updated_at
                ) VALUES (?, ?, ?, 1, 0, 1, ?, ?)
                """,
                (person_id, binding_id, presence_id, now, now),
            )

    def _presence_external_ids(self, connection: sqlite3.Connection) -> tuple[str, ...]:
        return tuple(
            str(row["external_account_id"])
            for row in connection.execute(
                "SELECT external_account_id FROM presences ORDER BY created_at, id"
            )
        )

    def _runtime_alias_keys(
        self,
        connection: sqlite3.Connection,
        mark: ConversationWatermark,
        presence_accounts: tuple[str, ...],
    ) -> tuple[str, ...]:
        keys: list[str] = []
        if self._is_real_scope_key(mark.primary_scope_key):
            keys.append(mark.primary_scope_key)
        keys.extend(key for key in mark.covered_scope_keys if self._is_real_scope_key(key))
        if mark.kind == "space":
            externals = [
                str(row["external_space_id"])
                for row in connection.execute(
                    "SELECT external_space_id FROM space_bindings WHERE space_id = ? "
                    "ORDER BY created_at, id",
                    (mark.owner_id,),
                )
            ]
            keys.extend(
                f"bot:{bot}:group:{space}" for bot in presence_accounts for space in externals
            )
        else:
            externals = [
                str(row["external_account_id"])
                for row in connection.execute(
                    "SELECT external_account_id FROM identity_bindings "
                    "WHERE person_id = ? ORDER BY created_at, id",
                    (mark.owner_id,),
                )
            ]
            keys.extend(
                f"bot:{bot}:private:{peer}" for bot in presence_accounts for peer in externals
            )
        return tuple(dict.fromkeys(key for key in keys if self._is_real_scope_key(key)))

    def _ensure_aliases(
        self,
        connection: sqlite3.Connection,
        conversation_id: str,
        keys: tuple[str, ...],
        now: str,
    ) -> None:
        has_primary = connection.execute(
            "SELECT 1 FROM conversation_legacy_aliases "
            "WHERE conversation_id = ? AND is_primary = 1",
            (conversation_id,),
        ).fetchone()
        for key in keys:
            existing = connection.execute(
                "SELECT conversation_id FROM conversation_legacy_aliases WHERE scope_key = ?",
                (key,),
            ).fetchone()
            if existing is not None:
                continue
            primary = has_primary is None
            connection.execute(
                """
                INSERT INTO conversation_legacy_aliases (
                    id, conversation_id, scope_key, is_primary, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (str(uuid4()), conversation_id, key, 1 if primary else 0, now, now),
            )
            if primary:
                has_primary = True

    def _persist_migration_rollup(
        self,
        connection: sqlite3.Connection,
        conversation_id: str,
        mark: ConversationWatermark,
        now: str,
    ) -> None:
        if not _table_exists(connection, "canonical_conversation_rollups"):
            return
        existing = connection.execute(
            "SELECT conversation_id FROM canonical_conversation_rollups WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()
        summary = mark.migration_summary or "migration-empty"
        fingerprint = mark.rollup_fingerprint.lower()
        if existing is None:
            connection.execute(
                """
                INSERT INTO canonical_conversation_rollups (
                    conversation_id, generation, covered_through_event_id, summary_text,
                    summary_kind, source_fingerprint, revision, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'migration', ?, 1, ?, ?)
                """,
                (
                    conversation_id,
                    mark.generation,
                    mark.covered_through_event_id,
                    summary,
                    fingerprint,
                    now,
                    now,
                ),
            )
            return
        connection.execute(
            """
            UPDATE canonical_conversation_rollups
            SET generation = ?,
                covered_through_event_id = ?,
                summary_text = ?,
                summary_kind = 'migration',
                source_fingerprint = ?,
                revision = revision + 1,
                updated_at = ?
            WHERE conversation_id = ?
            """,
            (
                mark.generation,
                mark.covered_through_event_id,
                summary,
                fingerprint,
                now,
                conversation_id,
            ),
        )

    @staticmethod
    def _is_plugin_external(event: sqlite3.Row) -> bool:
        keys = event.keys()
        if str(event["event_kind"] or "") == "external_event":
            return True
        if "direction" in keys and str(event["direction"] or "") == "external":
            return True
        for column in ("source_plugin_id", "external_source", "external_event_key"):
            if column in keys and event[column]:
                return True
        return False

    @classmethod
    def _is_onebot_inbound_message(cls, event: sqlite3.Row) -> bool:
        message_id = str(event["platform_message_id"] or "").strip()
        if not message_id:
            return False
        direction = str(event["direction"] or "") if "direction" in event.keys() else ""
        if direction != "inbound":
            return False
        if str(event["event_kind"] or "") != "message":
            return False
        return not cls._is_plugin_external(event)

    def _duplicate_canonical_ids(
        self,
        events: list[sqlite3.Row],
        plan: CutoverPlan,
    ) -> dict[int, str]:
        by_id = {int(row["id"]): row for row in events}
        assigned: dict[int, str] = {}
        for item in plan.duplicates:
            if item.kind != "suppress" or not item.event_ids:
                continue
            existing = {
                str(by_id[event_id]["canonical_event_id"])
                for event_id in item.event_ids
                if event_id in by_id and by_id[event_id]["canonical_event_id"]
            }
            if len(existing) > 1:
                raise IdentityCutoverPreconditionError("receipt_conflict")
            shared = next(iter(existing)) if existing else str(uuid4())
            for event_id in item.event_ids:
                assigned[event_id] = shared
        return assigned

    def _legacy_scope_owner(
        self, connection: sqlite3.Connection, scope: sqlite3.Row
    ) -> tuple[str, str]:
        if str(scope["scope_type"]) == "private":
            peer = normalize_external_id(scope["private_peer_user_id"])
            if peer is None:
                raise IdentityCutoverPreconditionError("canonical_kind_mismatch")
            person_id = self._external_person_ids(connection).get(peer)
            if person_id is None:
                raise IdentityCutoverPreconditionError("canonical_kind_mismatch")
            return "private", person_id
        group = normalize_external_id(scope["group_id"])
        if group is None:
            raise IdentityCutoverPreconditionError("canonical_kind_mismatch")
        space_id = self._external_space_ids(connection).get(group)
        if space_id is None:
            raise IdentityCutoverPreconditionError("canonical_kind_mismatch")
        return "space", space_id

    def _mark_for_owner(self, plan: CutoverPlan, kind: str, owner_id: str) -> ConversationWatermark:
        for mark in plan.conversations:
            if mark.kind == kind and mark.owner_id == owner_id:
                return mark
        raise IdentityCutoverPreconditionError(_INCOMPATIBLE_CONVERSATION)

    def _mapped_legacy_conversation(
        self, connection: sqlite3.Connection, scope: sqlite3.Row
    ) -> sqlite3.Row:
        if not _table_exists(connection, "conversation_legacy_aliases"):
            raise IdentityCutoverPreconditionError("canonical_kind_mismatch")
        aliases = list(
            connection.execute(
                "SELECT conversation_id FROM conversation_legacy_aliases WHERE scope_key = ?",
                (str(scope["scope_key"]),),
            )
        )
        if len(aliases) != 1:
            raise IdentityCutoverPreconditionError("canonical_kind_mismatch")
        conversation = connection.execute(
            "SELECT id, kind, person_id, space_id, covered_through_event_id "
            "FROM canonical_conversations WHERE id = ?",
            (str(aliases[0]["conversation_id"]),),
        ).fetchone()
        if conversation is None:
            raise IdentityCutoverPreconditionError("canonical_kind_mismatch")
        kind, owner_id = self._legacy_scope_owner(connection, scope)
        actual_owner = conversation["person_id"] if kind == "private" else conversation["space_id"]
        if str(conversation["kind"]) != kind or str(actual_owner or "") != owner_id:
            raise IdentityCutoverPreconditionError("canonical_kind_mismatch")
        return cast(sqlite3.Row, conversation)

    def _stored_migration_rollup(
        self, connection: sqlite3.Connection, conversation_id: str
    ) -> sqlite3.Row | None:
        if not _table_exists(connection, "canonical_conversation_rollups"):
            return None
        return cast(
            sqlite3.Row | None,
            connection.execute(
                "SELECT summary_text, summary_kind, source_fingerprint, covered_through_event_id "
                "FROM canonical_conversation_rollups WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone(),
        )

    def _require_stored_migration_matches_mark(
        self,
        connection: sqlite3.Connection,
        conversation_id: str,
        mark: ConversationWatermark,
        covered_through_event_id: int,
    ) -> sqlite3.Row:
        stored = self._stored_migration_rollup(connection, conversation_id)
        if stored is None:
            raise IdentityCutoverPreconditionError(_INCOMPATIBLE_CONVERSATION)
        if (
            str(stored["summary_kind"]) != "migration"
            or str(stored["source_fingerprint"]) != mark.rollup_fingerprint
            or str(stored["summary_text"]) != mark.migration_summary
            or int(stored["covered_through_event_id"]) != mark.covered_through_event_id
            or int(covered_through_event_id) != mark.covered_through_event_id
        ):
            raise IdentityCutoverPreconditionError(_INCOMPATIBLE_CONVERSATION)
        return stored

    def _scope_ledger_high_watermark(
        self, connection: sqlite3.Connection, scope: sqlite3.Row
    ) -> int:
        if not _table_exists(connection, "chat_events"):
            return 0
        if str(scope["scope_type"]) == "group":
            return int(
                connection.execute(
                    "SELECT COALESCE(MAX(id), 0) FROM chat_events "
                    "WHERE bot_user_id = ? AND scope_type = 'group' AND group_id = ?",
                    (scope["bot_user_id"], scope["group_id"]),
                ).fetchone()[0]
            )
        return int(
            connection.execute(
                "SELECT COALESCE(MAX(id), 0) FROM chat_events "
                "WHERE bot_user_id = ? AND scope_type = 'private' "
                "AND private_peer_user_id = ?",
                (scope["bot_user_id"], scope["private_peer_user_id"]),
            ).fetchone()[0]
        )

    def _require_no_processing_rollup_leases(self, connection: sqlite3.Connection) -> None:
        for table in ("conversation_rollup_jobs", "canonical_conversation_rollup_jobs"):
            if not _table_exists(connection, table):
                continue
            columns = {str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")')}
            clauses: list[str] = []
            if "status" in columns:
                clauses.append("status = 'processing'")
            if "lease_until" in columns:
                clauses.append("lease_until IS NOT NULL")
            if "lease_owner" in columns:
                clauses.append("lease_owner IS NOT NULL")
            if not clauses:
                continue
            count = int(
                connection.execute(
                    f'SELECT COUNT(*) FROM "{table}" WHERE {" OR ".join(clauses)}'
                ).fetchone()[0]
            )
            if count:
                raise IdentityCutoverPreconditionError("lease_not_drained")

    def _require_legacy_scope_carriers_ready(
        self, connection: sqlite3.Connection, plan: CutoverPlan
    ) -> None:
        """Fail closed unless leftover scope carriers are proven in canonical v2."""

        self._require_no_processing_rollup_leases(connection)
        if not _table_exists(connection, "conversation_scopes"):
            return
        scopes = list(connection.execute("SELECT * FROM conversation_scopes ORDER BY id"))
        mapped: dict[int, sqlite3.Row] = {}
        for scope in scopes:
            conversation = self._mapped_legacy_conversation(connection, scope)
            mapped[int(scope["id"])] = conversation
            kind, owner_id = self._legacy_scope_owner(connection, scope)
            mark = self._mark_for_owner(plan, kind, owner_id)
            self._require_stored_migration_matches_mark(
                connection,
                str(conversation["id"]),
                mark,
                int(conversation["covered_through_event_id"]),
            )
        if _table_exists(connection, "conversation_rollups"):
            for rollup in connection.execute("SELECT * FROM conversation_rollups"):
                scope = connection.execute(
                    "SELECT * FROM conversation_scopes WHERE id = ?",
                    (int(rollup["scope_id"]),),
                ).fetchone()
                if scope is None:
                    raise IdentityCutoverPreconditionError(_INCOMPATIBLE_CONVERSATION)
                if int(rollup["generation"]) != int(scope["generation"]):
                    raise IdentityCutoverPreconditionError(_INCOMPATIBLE_CONVERSATION)
                conversation = mapped[int(scope["id"])]
                kind, owner_id = self._legacy_scope_owner(connection, scope)
                mark = self._mark_for_owner(plan, kind, owner_id)
                stored = self._require_stored_migration_matches_mark(
                    connection,
                    str(conversation["id"]),
                    mark,
                    int(conversation["covered_through_event_id"]),
                )
                semantic = str(rollup["summary_text"] or "").strip()
                if semantic and semantic not in str(stored["summary_text"]):
                    if len(semantic) <= MIGRATION_SUMMARY_MAX_CHARACTERS:
                        raise IdentityCutoverPreconditionError(_INCOMPATIBLE_CONVERSATION)
        if not _table_exists(connection, "conversation_rollup_emergency_overlays"):
            return
        for overlay in connection.execute("SELECT * FROM conversation_rollup_emergency_overlays"):
            scope = connection.execute(
                "SELECT * FROM conversation_scopes WHERE id = ?",
                (int(overlay["scope_id"]),),
            ).fetchone()
            if scope is None:
                raise IdentityCutoverPreconditionError(_INCOMPATIBLE_CONVERSATION)
            conversation = mapped[int(scope["id"])]
            kind, owner_id = self._legacy_scope_owner(connection, scope)
            mark = self._mark_for_owner(plan, kind, owner_id)
            stored = self._require_stored_migration_matches_mark(
                connection,
                str(conversation["id"]),
                mark,
                int(conversation["covered_through_event_id"]),
            )
            semantic_row = None
            if _table_exists(connection, "conversation_rollups"):
                semantic_row = connection.execute(
                    "SELECT covered_through_event_id, summary_text FROM conversation_rollups "
                    "WHERE scope_id = ? AND generation = ?",
                    (int(scope["id"]), int(scope["generation"])),
                ).fetchone()
            semantic_covered = (
                int(semantic_row["covered_through_event_id"])
                if semantic_row is not None
                else int(scope["starts_after_event_id"])
            )
            overlay_covered = int(overlay["covered_through_event_id"])
            last_event_id = int(scope["last_event_id"])
            if overlay_covered > last_event_id:
                raise IdentityCutoverPreconditionError(_INCOMPATIBLE_CONVERSATION)
            if overlay_covered > semantic_covered:
                if self._scope_ledger_high_watermark(connection, scope) < overlay_covered:
                    raise IdentityCutoverPreconditionError(_INCOMPATIBLE_CONVERSATION)
            if int(mark.covered_through_event_id) < last_event_id:
                raise IdentityCutoverPreconditionError(_INCOMPATIBLE_CONVERSATION)
            overlay_text = str(overlay["summary_text"] or "").strip()
            stored_text = str(stored["summary_text"])
            semantic_text = (
                str(semantic_row["summary_text"] or "").strip() if semantic_row is not None else ""
            )
            if overlay_text and stored_text == overlay_text and stored_text != semantic_text:
                raise IdentityCutoverPreconditionError(_INCOMPATIBLE_CONVERSATION)

    def _retire_legacy_conversation_scope_carriers(self, connection: sqlite3.Connection) -> None:
        """Drop leftover v1 scope/rollup/job rows after aliases and canonical rollups exist.

        complete-v2 readers hydrate from conversation_legacy_aliases plus
        canonical conversations. Leftover scopes keep
        canonical_conversation_id ON DELETE RESTRICT and would block
        Person-level forgetme. Ordinary v2 runtime must not mutate these
        tables, so cutover retires them once before the v2 flip.
        """

        if not _table_exists(connection, "conversation_scopes"):
            return
        for table in (
            "conversation_rollup_emergency_overlays",
            "conversation_rollup_jobs",
            "conversation_rollups",
        ):
            if _table_exists(connection, table):
                connection.execute(f'DELETE FROM "{table}"')
        connection.execute("DELETE FROM conversation_scopes")

    def _retire_conversation_memory_jobs(self, connection: sqlite3.Connection) -> None:
        if not _table_exists(connection, "memory_jobs"):
            return
        if not _table_exists(connection, "canonical_conversations"):
            return
        if not _table_exists(connection, "chat_events"):
            return
        for conversation in connection.execute(
            "SELECT id, starts_after_event_id FROM canonical_conversations"
        ):
            connection.execute(
                """
                DELETE FROM memory_jobs
                WHERE status IN ('pending', 'failed')
                  AND EXISTS (
                    SELECT 1 FROM chat_events
                    WHERE chat_events.id = memory_jobs.event_id
                      AND chat_events.canonical_conversation_id = ?
                      AND chat_events.id <= ?
                  )
                """,
                (str(conversation["id"]), int(conversation["starts_after_event_id"])),
            )

    def _persist_onebot_receipts(
        self,
        connection: sqlite3.Connection,
        events: list[sqlite3.Row],
        assigned_event_ids: dict[int, str],
        _suppress: set[int],
        now: str,
    ) -> None:
        if not _table_exists(connection, "canonical_event_receipts"):
            return
        presence_by_account = {
            str(row["external_account_id"]): str(row["id"])
            for row in connection.execute("SELECT id, external_account_id FROM presences")
        }
        for event in events:
            event_id = int(event["id"])
            if not self._is_onebot_inbound_message(event):
                continue
            presence_id = presence_by_account.get(str(event["bot_user_id"] or ""))
            canonical_event_id = assigned_event_ids.get(event_id)
            if presence_id is None or canonical_event_id is None:
                continue
            observed = str(event["observed_at"] or now)
            existing = connection.execute(
                "SELECT canonical_event_id FROM canonical_event_receipts "
                "WHERE ingress_presence_id = ? AND event_type = ? "
                "AND platform_message_id = ?",
                (presence_id, "message", str(event["platform_message_id"])[:128]),
            ).fetchone()
            if existing is not None:
                if str(existing["canonical_event_id"]) != canonical_event_id:
                    raise IdentityCutoverPreconditionError("receipt_conflict")
                continue
            connection.execute(
                """
                INSERT INTO canonical_event_receipts (
                    ingress_presence_id, event_type, platform_message_id,
                    canonical_event_id, created_at, observed_at
                ) VALUES (?, 'message', ?, ?, ?, ?)
                """,
                (
                    presence_id,
                    str(event["platform_message_id"])[:128],
                    canonical_event_id,
                    now,
                    observed,
                ),
            )

    def _existing_conversation_for_mark(
        self, connection: sqlite3.Connection, mark: ConversationWatermark
    ) -> sqlite3.Row | None:
        if not _table_exists(connection, "canonical_conversations"):
            return None
        owner_column = "person_id" if mark.kind == "private" else "space_id"
        return cast(
            sqlite3.Row | None,
            connection.execute(
                f"SELECT {_CONVERSATION_STATE_COLUMNS} FROM canonical_conversations "
                f"WHERE kind = ? AND {owner_column} = ?",
                (mark.kind, mark.owner_id),
            ).fetchone(),
        )

    def _conversation_beyond_watermark(
        self,
        connection: sqlite3.Connection,
        existing: sqlite3.Row,
        mark: ConversationWatermark,
    ) -> bool:
        if int(existing["generation"]) > mark.generation:
            return True
        if int(existing["last_event_id"]) > mark.last_event_id:
            return True
        if int(existing["covered_through_event_id"]) > mark.covered_through_event_id:
            return True
        if int(existing["starts_after_event_id"]) > mark.last_event_id:
            return True
        if int(existing["last_generation_change_event_id"]) > mark.last_event_id:
            return True
        if _table_exists(connection, "chat_events"):
            mapped = int(
                connection.execute(
                    "SELECT COALESCE(MAX(id), 0) FROM chat_events "
                    "WHERE canonical_conversation_id = ?",
                    (existing["id"],),
                ).fetchone()[0]
            )
            if mapped > mark.last_event_id:
                return True
        if not _table_exists(connection, "canonical_conversation_rollups"):
            return False
        rollup = connection.execute(
            "SELECT generation, covered_through_event_id, summary_kind "
            "FROM canonical_conversation_rollups WHERE conversation_id = ?",
            (existing["id"],),
        ).fetchone()
        if rollup is None:
            return False
        rollup_generation = int(rollup["generation"])
        rollup_covered = int(rollup["covered_through_event_id"])
        if rollup_generation > mark.generation or rollup_covered > mark.covered_through_event_id:
            return True
        kind = str(rollup["summary_kind"] or "")
        if kind != "migration" and (
            rollup_covered > 0
            and (
                rollup_generation != mark.generation
                or rollup_covered != mark.covered_through_event_id
            )
        ):
            return True
        return False

    def _require_conversations_alignable(
        self,
        connection: sqlite3.Connection,
        conversations: tuple[ConversationWatermark, ...],
    ) -> None:
        for mark in conversations:
            existing = self._existing_conversation_for_mark(connection, mark)
            if existing is None:
                if not self._is_real_scope_key(mark.primary_scope_key):
                    raise IdentityCutoverPreconditionError("canonical_kind_mismatch")
                continue
            if self._conversation_beyond_watermark(connection, existing, mark):
                raise IdentityCutoverPreconditionError(_INCOMPATIBLE_CONVERSATION)

    def _align_conversation_to_watermark(
        self,
        connection: sqlite3.Connection,
        conversation_id: str,
        mark: ConversationWatermark,
        now: str,
    ) -> None:
        connection.execute(
            """
            UPDATE canonical_conversations
            SET generation = ?,
                starts_after_event_id = ?,
                last_event_id = ?,
                last_generation_change_event_id = ?,
                covered_through_event_id = ?,
                uncovered_event_count = 0,
                uncovered_character_count = 0,
                revision = revision + 1,
                updated_at = ?
            WHERE id = ?
            """,
            (
                mark.generation,
                mark.covered_through_event_id,
                mark.last_event_id,
                mark.covered_through_event_id,
                mark.covered_through_event_id,
                now,
                conversation_id,
            ),
        )
        self._persist_migration_rollup(connection, conversation_id, mark, now)
        self._require_conversation_rollup_generation(connection, conversation_id, mark.generation)

    def _require_conversation_rollup_generation(
        self, connection: sqlite3.Connection, conversation_id: str, generation: int
    ) -> None:
        conversation = connection.execute(
            "SELECT generation FROM canonical_conversations WHERE id = ?",
            (conversation_id,),
        ).fetchone()
        if conversation is None or int(conversation["generation"]) != generation:
            raise IdentityCutoverPreconditionError(_INCOMPATIBLE_CONVERSATION)
        if not _table_exists(connection, "canonical_conversation_rollups"):
            return
        rollup = connection.execute(
            "SELECT generation FROM canonical_conversation_rollups WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()
        if rollup is None or int(rollup["generation"]) != generation:
            raise IdentityCutoverPreconditionError(_INCOMPATIBLE_CONVERSATION)

    def _single_presence_id(self, connection: sqlite3.Connection) -> str | None:
        rows = list(connection.execute("SELECT id FROM presences"))
        if len(rows) == 1:
            return str(rows[0]["id"])
        needs_route = connection.execute(
            "SELECT 1 FROM space_bindings UNION ALL "
            "SELECT 1 FROM identity_bindings WHERE status = 'active' LIMIT 1"
        ).fetchone()
        if not rows and needs_route is not None:
            raise IdentityCutoverPreconditionError("route_ambiguity")
        return None

    def apply_plan(self, connection: sqlite3.Connection, plan: CutoverPlan) -> None:
        now = utc_now_text()
        presence_id = self._single_presence_id(connection)
        presence_accounts = self._presence_external_ids(connection)
        self._require_conversations_alignable(connection, plan.conversations)
        self.trip("after_revalidate")
        conversation_ids: dict[tuple[str, str], str] = {}
        for mark in plan.conversations:
            existing = self._existing_conversation_for_mark(connection, mark)
            if existing is not None:
                conversation_ids[(mark.kind, mark.owner_id)] = str(existing["id"])
                self._align_conversation_to_watermark(connection, str(existing["id"]), mark, now)
                continue
            if not self._is_real_scope_key(mark.primary_scope_key):
                raise IdentityCutoverPreconditionError("canonical_kind_mismatch")
            conversation_id = str(uuid4())
            alias_id = str(uuid4())
            connection.execute(
                """
                INSERT INTO canonical_conversations (
                    id, kind, person_id, space_id, primary_alias_id, primary_marker,
                    generation, starts_after_event_id, last_event_id,
                    last_generation_change_event_id, covered_through_event_id,
                    uncovered_event_count, uncovered_character_count, revision,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, 0, 0, 1, ?, ?)
                """,
                (
                    conversation_id,
                    mark.kind,
                    mark.owner_id if mark.kind == "private" else None,
                    mark.owner_id if mark.kind == "space" else None,
                    alias_id,
                    mark.generation,
                    mark.covered_through_event_id,
                    mark.last_event_id,
                    mark.covered_through_event_id,
                    mark.covered_through_event_id,
                    now,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO conversation_legacy_aliases (
                    id, conversation_id, scope_key, is_primary, created_at, updated_at
                ) VALUES (?, ?, ?, 1, ?, ?)
                """,
                (alias_id, conversation_id, mark.primary_scope_key, now, now),
            )
            self._persist_migration_rollup(connection, conversation_id, mark, now)
            self._require_conversation_rollup_generation(
                connection, conversation_id, mark.generation
            )
            conversation_ids[(mark.kind, mark.owner_id)] = conversation_id
        self.trip("after_conversations")
        for mark in plan.conversations:
            alias_conversation_id = conversation_ids.get((mark.kind, mark.owner_id))
            if alias_conversation_id is None:
                continue
            self._ensure_aliases(
                connection,
                alias_conversation_id,
                self._runtime_alias_keys(connection, mark, presence_accounts),
                now,
            )
        self.trip("after_aliases")
        presence_count = int(connection.execute("SELECT COUNT(*) FROM presences").fetchone()[0])
        if presence_count == 1 and presence_id is not None:
            self._apply_default_routes(connection, presence_id, now)
        self.trip("after_routes")
        suppress = set(plan.suppress_event_ids)
        assigned_event_ids: dict[int, str] = {}
        if _table_exists(connection, "chat_events"):
            events = connection.execute(
                """
                SELECT id, group_id, private_peer_user_id, sender_user_id, scope_type,
                       canonical_event_id, ingress_presence_id, event_kind,
                       platform_message_id, observed_at, direction, origin,
                       source_plugin_id, external_source, external_event_key,
                       bot_user_id
                FROM chat_events
                """
            ).fetchall()
            duplicate_ids = self._duplicate_canonical_ids(events, plan)
            for event in events:
                owner: tuple[str, str] | None = None
                if event["group_id"]:
                    space = connection.execute(
                        "SELECT space_id FROM space_bindings WHERE platform = ? "
                        "AND external_space_id = ?",
                        (IDENTITY_PLATFORM, event["group_id"]),
                    ).fetchone()
                    if space is not None:
                        owner = ("space", str(space["space_id"]))
                else:
                    person = connection.execute(
                        "SELECT person_id FROM identity_bindings WHERE platform = ? "
                        "AND external_account_id = ?",
                        (
                            IDENTITY_PLATFORM,
                            event["private_peer_user_id"] or event["sender_user_id"],
                        ),
                    ).fetchone()
                    if person is not None:
                        owner = ("private", str(person["person_id"]))
                mapped_conversation_id = conversation_ids.get(owner) if owner else None
                event_id = int(event["id"])
                canonical_event_id = (
                    duplicate_ids.get(event_id) or event["canonical_event_id"] or str(uuid4())
                )
                if event_id in suppress:
                    status = "duplicate"
                    utterance = hashlib.sha256(
                        f"cutover-duplicate:{event['id']}".encode()
                    ).hexdigest()
                else:
                    status = "keeper"
                    utterance = None
                assigned_event_ids[event_id] = canonical_event_id
                connection.execute(
                    """
                    UPDATE chat_events
                    SET canonical_event_id = ?,
                        canonical_conversation_id = COALESCE(canonical_conversation_id, ?),
                        suppression_status = COALESCE(suppression_status, ?),
                        utterance_fingerprint = COALESCE(utterance_fingerprint, ?)
                    WHERE id = ?
                    """,
                    (
                        canonical_event_id,
                        mapped_conversation_id,
                        status,
                        utterance,
                        event["id"],
                    ),
                )
            self._persist_onebot_receipts(connection, events, assigned_event_ids, suppress, now)
        self.trip("after_event_mapping")
        self._retire_conversation_memory_jobs(connection)
        self.trip("before_scope_carrier_retirement")
        self._require_legacy_scope_carriers_ready(connection, plan)
        self._retire_legacy_conversation_scope_carriers(connection)
        self.trip("after_scope_carrier_retirement")
        self.trip("after_baselines")
        self.trip("after_preconfig")
        self.require_c21_readable_owners(connection, plan.c21_readable_cutoff)
        self.trip("after_c21_owners")
        require_c21_readable_evidence(connection)
        require_c22_runnable_automation_targets(connection)
        require_c23_plugin_targets(connection)
        cutover_id = str(uuid4())
        self.trip("before_flip")
        connection.execute(
            """
            UPDATE identity_runtime_state
            SET state = 'v2',
                cutover_id = ?,
                source_fingerprint = ?,
                completed_at = ?,
                revision = revision + 1,
                updated_at = ?
            WHERE id = 1
            """,
            (cutover_id, plan.source_fingerprint, now, now),
        )
