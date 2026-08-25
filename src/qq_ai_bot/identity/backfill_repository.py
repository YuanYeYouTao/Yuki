"""SQLite adapter for identity backfill.

This layer talks to sqlite3 and never imports argparse, a renderer, or ORM
models. Callers pass a file path; the adapter does not print it.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from qq_ai_bot.identity.backfill_types import (
    AccountEvidence,
    BackfillPlan,
    BackfillSettingsInput,
    CanonicalPersonRow,
    CanonicalSpaceRow,
    IdentityBindingRow,
    MemoryOwnerAssignment,
    MutableAccountEvidence,
    MutableSpaceEvidence,
    PresenceRow,
    ShadowAssignment,
    SpaceBindingRow,
    SpaceEvidence,
)
from qq_ai_bot.identity.c22_automation import c22_signature_chunks
from qq_ai_bot.identity.c23_plugin import c23_signature_chunks
from qq_ai_bot.identity.canonical_memory_owners import c21_signature_chunks
from qq_ai_bot.identity.errors import IdentityBackfillPreconditionError
from qq_ai_bot.identity.inventory import (
    HUMAN_PLUGIN_MESSAGE_ROLES,
    IDENTITY_PLATFORM,
    REQUIRED_C7_SCHEMA,
    SHADOW_FILL_SPECS,
    YUKI_SELF_COLUMNS,
)
from qq_ai_bot.identity.sanitize import (
    account_fingerprint_material,
    normalize_external_id,
    shadow_fingerprint_material,
    source_fingerprint,
    space_fingerprint_material,
)

Failpoint = Callable[[str], None]


def sqlite_path_from_url(url: str) -> Path:
    """Resolve a sqlite URL to a filesystem path without logging it."""

    for prefix in ("sqlite+aiosqlite:///", "sqlite:///"):
        if url.startswith(prefix):
            raw = url.removeprefix(prefix)
            if raw == ":memory:" or raw.startswith(":memory:"):
                raise ValueError("identity backfill requires a file-backed sqlite database")
            return Path(raw)
    raise ValueError("identity backfill requires a sqlite database")


def sqlite_file_uri(path: Path, *, mode: str) -> str:
    """Build a sqlite URI. Callers must never log or print the result."""

    return f"{path.resolve().as_uri()}?mode={mode}"


def connect_sqlite(path: Path, *, readonly: bool = False) -> sqlite3.Connection:
    if not path.is_file():
        raise IdentityBackfillPreconditionError("database_missing")
    uri = sqlite_file_uri(path, mode="ro" if readonly else "rw")
    connection = sqlite3.connect(uri, uri=True, isolation_level=None, timeout=30.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=30000")
    return connection


def utc_now_text() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()
    return row is not None


def _column_exists(connection: sqlite3.Connection, table: str, column: str) -> bool:
    return any(str(row[1]) == column for row in connection.execute(f'PRAGMA table_info("{table}")'))


def _account(
    store: dict[str, MutableAccountEvidence], raw: object
) -> MutableAccountEvidence | None:
    external_id = normalize_external_id(raw)
    if external_id is None:
        return None
    return store.setdefault(external_id, MutableAccountEvidence())


def _space(store: dict[str, MutableSpaceEvidence], raw: object) -> MutableSpaceEvidence | None:
    external_id = normalize_external_id(raw)
    if external_id is None:
        return None
    return store.setdefault(external_id, MutableSpaceEvidence())


def _add_source(target: MutableAccountEvidence | MutableSpaceEvidence, source: str) -> None:
    target.sources.add(source)


class IdentityBackfillRepository:
    """Read snapshots and apply canonical identity writes through sqlite3."""

    def __init__(self, path: Path, failpoint: Failpoint | None = None) -> None:
        self._path = path
        self.failpoint = failpoint

    def connect(self, *, readonly: bool = False) -> sqlite3.Connection:
        return connect_sqlite(self._path, readonly=readonly)

    def require_c7_ready(self, connection: sqlite3.Connection) -> None:
        for table, columns in REQUIRED_C7_SCHEMA.items():
            if not _table_exists(connection, table):
                raise IdentityBackfillPreconditionError("incomplete_schema")
            present = {str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")')}
            if any(column not in present for column in columns):
                raise IdentityBackfillPreconditionError("incomplete_schema")
        rows = connection.execute(
            "SELECT id, state FROM identity_runtime_state ORDER BY id"
        ).fetchall()
        if len(rows) != 1:
            raise IdentityBackfillPreconditionError("identity_runtime_state")
        row = rows[0]
        if int(row["id"]) != 1 or str(row["state"]) != "v1":
            raise IdentityBackfillPreconditionError("identity_runtime_state")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise IdentityBackfillPreconditionError("foreign_key_check")

    def trip(self, name: str) -> None:
        if self.failpoint is not None:
            self.failpoint(name)

    def _trip(self, name: str) -> None:
        self.trip(name)

    def load_snapshot(
        self,
        connection: sqlite3.Connection,
        settings: BackfillSettingsInput,
    ) -> tuple[
        dict[str, AccountEvidence],
        dict[str, SpaceEvidence],
        dict[str, CanonicalPersonRow],
        dict[str, IdentityBindingRow],
        dict[str, CanonicalSpaceRow],
        dict[str, SpaceBindingRow],
        dict[str, PresenceRow],
        tuple[ShadowAssignment, ...],
        str,
    ]:
        accounts: dict[str, MutableAccountEvidence] = {}
        spaces: dict[str, MutableSpaceEvidence] = {}

        def mark_account(source: str, raw: object) -> MutableAccountEvidence | None:
            item = _account(accounts, raw)
            if item is None:
                return None
            _add_source(item, source)
            return item

        def mark_space(source: str, raw: object) -> MutableSpaceEvidence | None:
            item = _space(spaces, raw)
            if item is None:
                return None
            _add_source(item, source)
            return item

        for external_id in settings.superusers:
            item = mark_account("settings.superusers", external_id)
            if item is not None:
                item.superuser = True
        for external_id in settings.ignored_bot_users:
            item = mark_account("settings.ignored_bot_users", external_id)
            if item is not None:
                item.ignored_bot = True
        for external_id in settings.enabled_groups:
            mark_space("settings.enabled_groups", external_id)

        if _table_exists(connection, "people"):
            for row in connection.execute(
                "SELECT user_id, nickname, enabled, is_bot, canonical_person_id FROM people"
            ):
                item = mark_account("people.user_id", row["user_id"])
                if item is None:
                    continue
                item.nickname = str(row["nickname"] or "")[:128]
                item.legacy_is_bot = bool(row["is_bot"])
                item.people_human = not bool(row["is_bot"])
                item.existing_person_id = row["canonical_person_id"]

        if _table_exists(connection, "groups"):
            for row in connection.execute(
                "SELECT group_id, name, enabled, require_mention, autonomous_enabled, "
                "canonical_space_id FROM groups"
            ):
                space_item = mark_space("groups.group_id", row["group_id"])
                if space_item is None:
                    continue
                space_item.has_groups_row = True
                space_item.name = str(row["name"] or "")[:128]
                space_item.enabled = bool(row["enabled"])
                space_item.require_mention = bool(row["require_mention"])
                space_item.autonomous_enabled = bool(row["autonomous_enabled"])
                space_item.existing_space_id = row["canonical_space_id"]

        if _table_exists(connection, "memberships"):
            for row in connection.execute("SELECT user_id, group_id FROM memberships"):
                member = mark_account("memberships.user_id", row["user_id"])
                if member is not None:
                    member.member = True
                mark_space("memberships.group_id", row["group_id"])

        if _table_exists(connection, "person_aliases"):
            for row in connection.execute("SELECT user_id, group_scope FROM person_aliases"):
                alias = mark_account("person_aliases.user_id", row["user_id"])
                if alias is not None:
                    alias.supporting_person = True
                if str(row["group_scope"] or ""):
                    mark_space("person_aliases.group_scope", row["group_scope"])

        if _table_exists(connection, "chat_events"):
            for row in connection.execute(
                "SELECT bot_user_id, sender_user_id, private_peer_user_id, group_id "
                "FROM chat_events"
            ):
                yuki = mark_account("chat_events.bot_user_id", row["bot_user_id"])
                if yuki is not None:
                    yuki.yuki_self = True
                sender = mark_account("chat_events.sender_user_id", row["sender_user_id"])
                if sender is not None and normalize_external_id(
                    row["sender_user_id"]
                ) != normalize_external_id(row["bot_user_id"]):
                    sender.human_sender = True
                peer = mark_account("chat_events.private_peer_user_id", row["private_peer_user_id"])
                if peer is not None:
                    peer.private_peer = True
                mark_space("chat_events.group_id", row["group_id"])

        if _table_exists(connection, "conversation_scopes"):
            for row in connection.execute(
                "SELECT bot_user_id, private_peer_user_id, group_id FROM conversation_scopes"
            ):
                yuki = mark_account("conversation_scopes.bot_user_id", row["bot_user_id"])
                if yuki is not None:
                    yuki.yuki_self = True
                peer = mark_account(
                    "conversation_scopes.private_peer_user_id",
                    row["private_peer_user_id"],
                )
                if peer is not None:
                    peer.private_peer = True
                mark_space("conversation_scopes.group_id", row["group_id"])

        self._load_extension_keys(connection, mark_account, mark_space)

        persons = self._load_persons(connection)
        bindings = self._load_bindings(connection)
        canonical_spaces = self._load_spaces(connection)
        space_bindings = self._load_space_bindings(connection)
        presences = self._load_presences(connection)

        for external_id, binding in bindings.items():
            item = mark_account("identity_bindings.external_account_id", external_id)
            if item is None:
                continue
            item.existing_binding_person_id = binding.person_id
            if not item.nickname:
                item.nickname = binding.display_name[:128]
        for external_id, presence in presences.items():
            item = mark_account("presences.external_account_id", external_id)
            if item is not None:
                item.existing_presence_id = presence.id
        for external_id, space_binding in space_bindings.items():
            space_item = mark_space("space_bindings.external_space_id", external_id)
            if space_item is None:
                continue
            space_item.existing_binding_space_id = space_binding.space_id
            if not space_item.name:
                space_item.name = space_binding.display_name[:128]

        shadows = self._load_shadows(connection)
        self._attach_shadow_owners(accounts, spaces, shadows)

        frozen_accounts = {
            key: AccountEvidence(
                external_id=key,
                sources=frozenset(item.sources),
                yuki_self=item.yuki_self,
                ignored_bot=item.ignored_bot,
                legacy_is_bot=item.legacy_is_bot,
                human_sender=item.human_sender,
                private_peer=item.private_peer,
                member=item.member,
                superuser=item.superuser,
                people_human=item.people_human,
                supporting_person=item.supporting_person,
                strong_person=item.strong_person,
                nickname=item.nickname,
                existing_person_id=item.existing_person_id,
                existing_binding_person_id=item.existing_binding_person_id,
                existing_presence_id=item.existing_presence_id,
                shadow_person_ids=frozenset(item.shadow_person_ids),
                shadow_presence_ids=frozenset(item.shadow_presence_ids),
            )
            for key, item in accounts.items()
        }
        frozen_spaces = {
            key: SpaceEvidence(
                external_id=key,
                sources=frozenset(item.sources),
                name=item.name,
                enabled=item.enabled,
                autonomous_enabled=item.autonomous_enabled,
                require_mention=item.require_mention,
                has_groups_row=item.has_groups_row,
                existing_space_id=item.existing_space_id,
                existing_binding_space_id=item.existing_binding_space_id,
                shadow_space_ids=frozenset(item.shadow_space_ids),
            )
            for key, item in spaces.items()
        }
        fingerprint = source_fingerprint(
            accounts={
                key: account_fingerprint_material(
                    sources=item.sources,
                    flags={
                        "yuki_self": item.yuki_self,
                        "ignored_bot": item.ignored_bot,
                        "legacy_is_bot": item.legacy_is_bot,
                        "human_sender": item.human_sender,
                        "private_peer": item.private_peer,
                        "member": item.member,
                        "superuser": item.superuser,
                        "people_human": item.people_human,
                        "supporting_person": item.supporting_person,
                        "strong_person": item.strong_person,
                    },
                    nickname=item.nickname,
                    existing_person_id=item.existing_person_id,
                    existing_binding_person_id=item.existing_binding_person_id,
                    existing_presence_id=item.existing_presence_id,
                    shadow_person_ids=item.shadow_person_ids,
                    shadow_presence_ids=item.shadow_presence_ids,
                )
                for key, item in frozen_accounts.items()
            },
            spaces={
                key: space_fingerprint_material(
                    sources=item.sources,
                    name=item.name,
                    enabled=item.enabled,
                    autonomous_enabled=item.autonomous_enabled,
                    require_mention=item.require_mention,
                    has_groups_row=item.has_groups_row,
                    existing_space_id=item.existing_space_id,
                    existing_binding_space_id=item.existing_binding_space_id,
                    shadow_space_ids=item.shadow_space_ids,
                )
                for key, item in frozen_spaces.items()
            },
            bindings={key: row.person_id for key, row in bindings.items()},
            presences={key: row.id for key, row in presences.items()},
            space_bindings={key: row.space_id for key, row in space_bindings.items()},
            shadows=tuple(
                shadow_fingerprint_material(
                    table=item.table,
                    column=item.column,
                    row_key=item.row_key,
                    source=item.value,
                    current=item.current,
                    fillable=item.fillable,
                )
                for item in shadows
            ),
            settings_superusers=settings.superusers,
            settings_enabled_groups=settings.enabled_groups,
            settings_ignored_bots=settings.ignored_bot_users,
        )
        return (
            frozen_accounts,
            frozen_spaces,
            persons,
            bindings,
            canonical_spaces,
            space_bindings,
            presences,
            shadows,
            fingerprint,
        )

    def _load_extension_keys(
        self,
        connection: sqlite3.Connection,
        mark_account: Callable[[str, object], MutableAccountEvidence | None],
        mark_space: Callable[[str, object], MutableSpaceEvidence | None],
    ) -> None:
        for table, column in YUKI_SELF_COLUMNS:
            if table in {"chat_events", "conversation_scopes"}:
                continue
            if not _table_exists(connection, table) or not _column_exists(
                connection, table, column
            ):
                continue
            for row in connection.execute(f'SELECT "{column}" FROM "{table}"'):
                item = mark_account(f"{table}.{column}", row[0])
                if item is not None:
                    item.yuki_self = True

        def mark_weak_person(source: str, raw: object) -> None:
            item = mark_account(source, raw)
            if item is not None:
                item.supporting_person = True

        def mark_strong_person(source: str, raw: object) -> None:
            item = mark_account(source, raw)
            if item is not None:
                item.strong_person = True

        if _table_exists(connection, "automations"):
            for row in connection.execute("SELECT creator_user_id FROM automations"):
                mark_strong_person("automations.creator_user_id", row[0])

        if _table_exists(connection, "plugin_background_target_grants"):
            for row in connection.execute(
                "SELECT created_by_user_id, target_type, target_id "
                "FROM plugin_background_target_grants"
            ):
                mark_strong_person(
                    "plugin_background_target_grants.created_by_user_id",
                    row["created_by_user_id"],
                )
                if row["target_type"] == "private":
                    mark_strong_person(
                        "plugin_background_target_grants.target_id", row["target_id"]
                    )
                elif row["target_type"] == "group":
                    mark_space("plugin_background_target_grants.target_id", row["target_id"])

        for table, source in (
            ("plugin_notification_outbox", "plugin_notification_outbox.target_id"),
            ("plugin_background_turn_jobs", "plugin_background_turn_jobs.target_id"),
        ):
            if not _table_exists(connection, table):
                continue
            for row in connection.execute(f"SELECT target_type, target_id FROM {table}"):
                if row["target_type"] == "private":
                    mark_strong_person(source, row["target_id"])
                elif row["target_type"] == "group":
                    mark_space(source, row["target_id"])

        if _table_exists(connection, "plugin_state"):
            for row in connection.execute("SELECT subject_user_id FROM plugin_state"):
                mark_weak_person("plugin_state.subject_user_id", row[0])
        if _table_exists(connection, "plugin_agent_sessions"):
            for row in connection.execute(
                "SELECT owner_user_id, scope_type, scope_id FROM plugin_agent_sessions"
            ):
                mark_strong_person("plugin_agent_sessions.owner_user_id", row["owner_user_id"])
                if row["scope_type"] == "group":
                    mark_space("plugin_agent_sessions.scope_id", row["scope_id"])
        if _table_exists(connection, "plugin_agent_messages"):
            for row in connection.execute(
                "SELECT sender_user_id, role FROM plugin_agent_messages "
                "WHERE sender_user_id IS NOT NULL"
            ):
                if str(row["role"] or "") in HUMAN_PLUGIN_MESSAGE_ROLES:
                    mark_strong_person(
                        "plugin_agent_messages.sender_user_id", row["sender_user_id"]
                    )

        for table, source in (
            ("plugin_config_values", "plugin_config_values.scope_id"),
            ("runtime_config_overrides", "runtime_config_overrides.scope_id"),
        ):
            if not _table_exists(connection, table):
                continue
            for row in connection.execute(f"SELECT scope_type, scope_id FROM {table}"):
                if row["scope_type"] == "user":
                    mark_strong_person(source, row["scope_id"])
                elif row["scope_type"] == "group":
                    mark_space(source, row["scope_id"])

        if _table_exists(connection, "emoji_assets"):
            for row in connection.execute(
                "SELECT first_seen_user_id, first_seen_group_id FROM emoji_assets"
            ):
                mark_weak_person("emoji_assets.first_seen_user_id", row["first_seen_user_id"])
                mark_space("emoji_assets.first_seen_group_id", row["first_seen_group_id"])
        if _table_exists(connection, "emoji_scope_states"):
            for row in connection.execute(
                "SELECT scope_type, scope_id FROM emoji_scope_states WHERE scope_type = 'group'"
            ):
                mark_space("emoji_scope_states.scope_id", row["scope_id"])
        if _table_exists(connection, "emoji_usage_events"):
            for row in connection.execute("SELECT actor_user_id, group_id FROM emoji_usage_events"):
                mark_weak_person("emoji_usage_events.actor_user_id", row["actor_user_id"])
                mark_space("emoji_usage_events.group_id", row["group_id"])

    def _load_persons(self, connection: sqlite3.Connection) -> dict[str, CanonicalPersonRow]:
        if not _table_exists(connection, "persons"):
            return {}
        return {
            str(row["id"]): CanonicalPersonRow(
                id=str(row["id"]),
                enabled=bool(row["enabled"]),
                revision=int(row["revision"]),
                created_at=str(row["created_at"]),
                updated_at=str(row["updated_at"]),
            )
            for row in connection.execute(
                "SELECT id, enabled, revision, created_at, updated_at FROM persons"
            )
        }

    def _load_bindings(self, connection: sqlite3.Connection) -> dict[str, IdentityBindingRow]:
        if not _table_exists(connection, "identity_bindings"):
            return {}
        rows: dict[str, IdentityBindingRow] = {}
        for row in connection.execute(
            "SELECT id, person_id, platform, external_account_id, display_name, "
            "status, revision, created_at, updated_at FROM identity_bindings"
        ):
            if str(row["platform"]) != IDENTITY_PLATFORM:
                continue
            rows[str(row["external_account_id"])] = IdentityBindingRow(
                id=str(row["id"]),
                person_id=str(row["person_id"]),
                platform=str(row["platform"]),
                external_account_id=str(row["external_account_id"]),
                display_name=str(row["display_name"]),
                status=str(row["status"]),
                revision=int(row["revision"]),
                created_at=str(row["created_at"]),
                updated_at=str(row["updated_at"]),
            )
        return rows

    def _load_spaces(self, connection: sqlite3.Connection) -> dict[str, CanonicalSpaceRow]:
        if not _table_exists(connection, "spaces"):
            return {}
        return {
            str(row["id"]): CanonicalSpaceRow(
                id=str(row["id"]),
                name=str(row["name"]),
                enabled=bool(row["enabled"]),
                autonomous_enabled=bool(row["autonomous_enabled"]),
                require_mention=bool(row["require_mention"]),
                revision=int(row["revision"]),
                created_at=str(row["created_at"]),
                updated_at=str(row["updated_at"]),
            )
            for row in connection.execute(
                "SELECT id, name, enabled, autonomous_enabled, require_mention, "
                "revision, created_at, updated_at FROM spaces"
            )
        }

    def _load_space_bindings(self, connection: sqlite3.Connection) -> dict[str, SpaceBindingRow]:
        if not _table_exists(connection, "space_bindings"):
            return {}
        rows: dict[str, SpaceBindingRow] = {}
        for row in connection.execute(
            "SELECT id, space_id, platform, external_space_id, display_name, "
            "status, revision, created_at, updated_at FROM space_bindings"
        ):
            if str(row["platform"]) != IDENTITY_PLATFORM:
                continue
            rows[str(row["external_space_id"])] = SpaceBindingRow(
                id=str(row["id"]),
                space_id=str(row["space_id"]),
                platform=str(row["platform"]),
                external_space_id=str(row["external_space_id"]),
                display_name=str(row["display_name"]),
                status=str(row["status"]),
                revision=int(row["revision"]),
                created_at=str(row["created_at"]),
                updated_at=str(row["updated_at"]),
            )
        return rows

    def _load_presences(self, connection: sqlite3.Connection) -> dict[str, PresenceRow]:
        if not _table_exists(connection, "presences"):
            return {}
        rows: dict[str, PresenceRow] = {}
        for row in connection.execute(
            "SELECT id, platform, external_account_id, enabled, ingest_eligible, "
            "revision, created_at, updated_at FROM presences"
        ):
            if str(row["platform"]) != IDENTITY_PLATFORM:
                continue
            rows[str(row["external_account_id"])] = PresenceRow(
                id=str(row["id"]),
                platform=str(row["platform"]),
                external_account_id=str(row["external_account_id"]),
                enabled=bool(row["enabled"]),
                ingest_eligible=bool(row["ingest_eligible"]),
                revision=int(row["revision"]),
                created_at=str(row["created_at"]),
                updated_at=str(row["updated_at"]),
            )
        return rows

    def _load_shadows(self, connection: sqlite3.Connection) -> tuple[ShadowAssignment, ...]:
        assignments: list[ShadowAssignment] = []
        for spec in SHADOW_FILL_SPECS:
            if spec.completeness == "shape_only_optional" or spec.source_column is None:
                continue
            if not _table_exists(connection, spec.table):
                continue
            if not _column_exists(connection, spec.table, spec.column):
                continue
            if not _column_exists(connection, spec.table, spec.source_column):
                continue
            pk_sql = ", ".join(spec.pk)
            extra = f', "{spec.role_column}"' if spec.role_column else ""
            rows = connection.execute(
                f'SELECT {pk_sql}, "{spec.column}", "{spec.source_column}"{extra} '
                f'FROM "{spec.table}" WHERE {spec.extra_where} ORDER BY {pk_sql}'
            )
            for row in rows:
                source = normalize_external_id(row[spec.source_column])
                if source is None:
                    continue
                current = row[spec.column]
                fillable = True
                if spec.role_column is not None:
                    fillable = str(row[spec.role_column] or "") in HUMAN_PLUGIN_MESSAGE_ROLES
                assignments.append(
                    ShadowAssignment(
                        table=spec.table,
                        row_key=tuple((key, row[key]) for key in spec.pk),
                        column=spec.column,
                        value=source,
                        current=str(current) if current is not None else None,
                        fillable=fillable,
                    )
                )
        assignments.sort(
            key=lambda item: (item.table, item.column, item.row_key, item.value, item.current or "")
        )
        return tuple(assignments)

    def load_event_author_owners(
        self,
        connection: sqlite3.Connection,
        *,
        person_bindings: dict[str, str],
        presence_bindings: dict[str, str],
        external_bot_accounts: frozenset[str],
    ) -> tuple[tuple[MemoryOwnerAssignment, ...], tuple[tuple[object, ...], ...]]:
        """Plan complete author triples for historical ledger rows.

        The three columns are written together so the canonical author shape
        triggers never observe a partial owner. Existing partial or mismatched
        triples fail closed instead of being repaired by guessing.
        """

        required = (
            "id",
            "sender_user_id",
            "direction",
            "event_kind",
            "author_kind",
            "author_person_id",
            "author_presence_id",
        )
        if not _table_exists(connection, "chat_events") or any(
            not _column_exists(connection, "chat_events", column) for column in required
        ):
            return (), ()

        assignments: list[MemoryOwnerAssignment] = []
        material: list[tuple[object, ...]] = []
        rows = connection.execute(
            "SELECT id, sender_user_id, direction, event_kind, author_kind, "
            "author_person_id, author_presence_id FROM chat_events ORDER BY id"
        )
        for row in rows:
            event_id = int(row["id"])
            sender = normalize_external_id(row["sender_user_id"])
            direction = str(row["direction"] or "")
            event_kind = str(row["event_kind"] or "")
            person_id: str | None = None
            presence_id: str | None = None
            if event_kind == "external_event" or direction == "external":
                author_kind = "system"
            elif sender is not None and sender in presence_bindings:
                author_kind = "yuki"
                presence_id = presence_bindings[sender]
            elif sender is not None and sender in external_bot_accounts:
                author_kind = "external_bot"
            elif sender is not None and sender in person_bindings:
                author_kind = "person"
                person_id = person_bindings[sender]
            else:
                raise IdentityBackfillPreconditionError("unclassified")

            current = (
                None if row["author_kind"] is None else str(row["author_kind"]),
                None if row["author_person_id"] is None else str(row["author_person_id"]),
                None if row["author_presence_id"] is None else str(row["author_presence_id"]),
            )
            desired = (author_kind, person_id, presence_id)
            material.append((event_id, *desired, *current))
            if current == desired:
                continue
            if current != (None, None, None):
                raise IdentityBackfillPreconditionError("canonical_owner_mismatch")
            assignments.append(
                MemoryOwnerAssignment(
                    table="chat_events",
                    row_id=event_id,
                    values=(
                        ("author_kind", author_kind),
                        ("author_person_id", person_id),
                        ("author_presence_id", presence_id),
                    ),
                )
            )
        return tuple(assignments), tuple(material)

    @staticmethod
    def _attach_shadow_owners(
        accounts: dict[str, MutableAccountEvidence],
        spaces: dict[str, MutableSpaceEvidence],
        shadows: tuple[ShadowAssignment, ...],
    ) -> None:
        for shadow in shadows:
            if shadow.current is None:
                continue
            external_id = normalize_external_id(shadow.value)
            if external_id is None:
                continue
            if shadow.column.endswith("presence_id") or shadow.column == "canonical_presence_id":
                item = accounts.get(external_id)
                if item is not None:
                    item.shadow_presence_ids.add(shadow.current)
                continue
            if "space" in shadow.column:
                space_item = spaces.get(external_id)
                if space_item is not None:
                    space_item.shadow_space_ids.add(shadow.current)
                continue
            account = accounts.get(external_id)
            if account is not None:
                account.shadow_person_ids.add(shadow.current)

    def schema_signature(self, connection: sqlite3.Connection) -> str:
        rows = connection.execute(
            "SELECT type, name, sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
        ).fetchall()
        payload = [(str(row[0]), str(row[1]), str(row[2] or "")) for row in rows]
        return hashlib.sha256(
            json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode()
        ).hexdigest()

    def business_signature(self, connection: sqlite3.Connection) -> str:
        chunks: list[str] = []
        statements = (
            "SELECT id, enabled, revision, created_at, updated_at FROM persons ORDER BY id",
            "SELECT id, person_id, platform, external_account_id, display_name, status, "
            "revision, created_at, updated_at FROM identity_bindings "
            "ORDER BY platform, external_account_id",
            "SELECT id, name, enabled, autonomous_enabled, require_mention, revision, "
            "created_at, updated_at FROM spaces ORDER BY id",
            "SELECT id, space_id, platform, external_space_id, display_name, status, "
            "revision, created_at, updated_at FROM space_bindings "
            "ORDER BY platform, external_space_id",
            "SELECT id, platform, external_account_id, enabled, ingest_eligible, revision, "
            "created_at, updated_at FROM presences ORDER BY platform, external_account_id",
            "SELECT id, state, cutover_id, source_fingerprint, completed_at, revision, "
            "created_at, updated_at FROM identity_runtime_state ORDER BY id",
        )
        for sql in statements:
            table = sql.split(" FROM ", 1)[1].split(" ", 1)[0]
            if not _table_exists(connection, table):
                continue
            chunks.append(sql)
            chunks.extend(str(tuple(row)) for row in connection.execute(sql))
        for spec in SHADOW_FILL_SPECS:
            if not _table_exists(connection, spec.table) or not _column_exists(
                connection, spec.table, spec.column
            ):
                continue
            pk_sql = ", ".join(spec.pk)
            sql = f'SELECT {pk_sql}, "{spec.column}" FROM "{spec.table}" ORDER BY {pk_sql}'
            chunks.append(sql)
            chunks.extend(str(tuple(row)) for row in connection.execute(sql))
        if _table_exists(connection, "chat_events") and all(
            _column_exists(connection, "chat_events", column)
            for column in ("author_kind", "author_person_id", "author_presence_id")
        ):
            sql = (
                "SELECT id, author_kind, author_person_id, author_presence_id "
                "FROM chat_events ORDER BY id"
            )
            chunks.append(sql)
            chunks.extend(str(tuple(row)) for row in connection.execute(sql))
        chunks.extend(c21_signature_chunks(connection))
        chunks.extend(c22_signature_chunks(connection))
        chunks.extend(c23_signature_chunks(connection))
        return hashlib.sha256("\n".join(chunks).encode()).hexdigest()

    def natural_key_projection(self, connection: sqlite3.Connection) -> dict[str, object]:
        """Canonical business projection keyed by natural ids, not UUIDs."""

        bindings = [
            (str(row["platform"]), str(row["external_account_id"]), str(row["status"]))
            for row in connection.execute(
                "SELECT platform, external_account_id, status FROM identity_bindings "
                "ORDER BY platform, external_account_id"
            )
        ]
        space_bindings = [
            (str(row["platform"]), str(row["external_space_id"]), str(row["status"]))
            for row in connection.execute(
                "SELECT platform, external_space_id, status FROM space_bindings "
                "ORDER BY platform, external_space_id"
            )
        ]
        presences = [
            (
                str(row["platform"]),
                str(row["external_account_id"]),
                int(row["enabled"]),
                int(row["ingest_eligible"]),
            )
            for row in connection.execute(
                "SELECT platform, external_account_id, enabled, ingest_eligible "
                "FROM presences ORDER BY platform, external_account_id"
            )
        ]
        people = [
            (str(row["user_id"]), 1 if row["canonical_person_id"] else 0)
            for row in connection.execute(
                "SELECT user_id, canonical_person_id FROM people ORDER BY user_id"
            )
        ]
        groups = [
            (str(row["group_id"]), 1 if row["canonical_space_id"] else 0)
            for row in connection.execute(
                "SELECT group_id, canonical_space_id FROM groups ORDER BY group_id"
            )
        ]
        return {
            "identity_bindings": bindings,
            "space_bindings": space_bindings,
            "presences": presences,
            "people_person_shadow": people,
            "groups_space_shadow": groups,
        }

    def apply_plan(self, connection: sqlite3.Connection, plan: BackfillPlan, now: str) -> None:
        from qq_ai_bot.identity.canonical_memory_schema import memory_fact_canonical_conflict_kind

        if _table_exists(connection, "memory_facts") and memory_fact_canonical_conflict_kind(
            connection
        ):
            raise IdentityBackfillPreconditionError("canonical_memory_fact_conflict")
        for account in plan.accounts:
            if account.create_person and account.person_id is not None:
                connection.execute(
                    "INSERT INTO persons (id, enabled, revision, created_at, updated_at) "
                    "VALUES (?, 1, 1, ?, ?)",
                    (account.person_id, now, now),
                )
            if account.create_binding and account.binding_id is not None and account.person_id:
                connection.execute(
                    "INSERT INTO identity_bindings ("
                    "id, person_id, platform, external_account_id, display_name, "
                    "status, revision, created_at, updated_at"
                    ") VALUES (?, ?, ?, ?, ?, 'active', 1, ?, ?)",
                    (
                        account.binding_id,
                        account.person_id,
                        IDENTITY_PLATFORM,
                        account.external_id,
                        account.display_name,
                        now,
                        now,
                    ),
                )
            if account.create_presence and account.presence_id is not None:
                connection.execute(
                    "INSERT INTO presences ("
                    "id, platform, external_account_id, enabled, ingest_eligible, "
                    "revision, created_at, updated_at"
                    ") VALUES (?, ?, ?, 1, 1, 1, ?, ?)",
                    (account.presence_id, IDENTITY_PLATFORM, account.external_id, now, now),
                )
        for space in plan.spaces:
            if space.create_space:
                connection.execute(
                    "INSERT INTO spaces ("
                    "id, name, enabled, autonomous_enabled, require_mention, "
                    "revision, created_at, updated_at"
                    ") VALUES (?, ?, ?, ?, ?, 1, ?, ?)",
                    (
                        space.space_id,
                        space.name,
                        int(space.enabled),
                        int(space.autonomous_enabled),
                        int(space.require_mention),
                        now,
                        now,
                    ),
                )
            if space.create_binding:
                connection.execute(
                    "INSERT INTO space_bindings ("
                    "id, space_id, platform, external_space_id, display_name, "
                    "status, revision, created_at, updated_at"
                    ") VALUES (?, ?, ?, ?, ?, 'active', 1, ?, ?)",
                    (
                        space.binding_id,
                        space.space_id,
                        IDENTITY_PLATFORM,
                        space.external_id,
                        space.display_name,
                        now,
                        now,
                    ),
                )
        self._trip("after_foundation_writes")
        for shadow in plan.shadows:
            assignments = " AND ".join(f'"{key}" = ?' for key, _value in shadow.row_key)
            sql = (
                f'UPDATE "{shadow.table}" SET "{shadow.column}" = ? '
                f'WHERE {assignments} AND "{shadow.column}" IS NULL'
            )
            params = [shadow.value, *[value for _key, value in shadow.row_key]]
            connection.execute(sql, params)
        self._trip("after_shadow_writes")
        for owner in (*plan.memory_owners, *plan.automation_targets, *plan.event_authors):
            assignments = ", ".join(f'"{column}" = ?' for column, _value in owner.values)
            unchanged = " AND ".join(f'"{column}" IS NULL' for column, _value in owner.values)
            sql = f'UPDATE "{owner.table}" SET {assignments} WHERE id = ? AND {unchanged}'
            params = [value for _column, value in owner.values]
            params.append(owner.row_id)
            connection.execute(sql, params)
        self._trip("after_c21_owner_writes")

    def record_succeeded_run(
        self,
        connection: sqlite3.Connection,
        plan: BackfillPlan,
        *,
        now: str,
        started_at: str,
    ) -> None:
        persons = sum(1 for item in plan.accounts if item.classification == "person")
        bindings = persons
        presences = sum(1 for item in plan.accounts if item.classification == "yuki_presence")
        connection.execute(
            "INSERT INTO identity_backfill_runs ("
            "mode, status, checkpoint, processed_count, persons_count, "
            "identity_bindings_count, spaces_count, space_bindings_count, "
            "presences_count, conflicts_count, skipped_count, error_category, "
            "started_at, finished_at, created_at, updated_at"
            ") VALUES ('apply', 'succeeded', ?, ?, ?, ?, ?, ?, ?, 0, ?, NULL, ?, ?, ?, ?)",
            (
                plan.source_fingerprint[:128],
                plan.processed_subjects,
                persons,
                bindings,
                len(plan.spaces),
                len(plan.spaces),
                presences,
                plan.skipped_external_bots,
                started_at,
                now,
                now,
                now,
            ),
        )

    def record_conflict_audit(
        self,
        connection: sqlite3.Connection,
        plan: BackfillPlan,
        *,
        now: str,
        started_at: str,
    ) -> None:
        connection.execute(
            "INSERT INTO identity_backfill_runs ("
            "mode, status, checkpoint, processed_count, persons_count, "
            "identity_bindings_count, spaces_count, space_bindings_count, "
            "presences_count, conflicts_count, skipped_count, error_category, "
            "started_at, finished_at, created_at, updated_at"
            ") VALUES ('apply', 'failed', ?, ?, 0, 0, 0, 0, 0, ?, ?, "
            "'identity_conflict', ?, ?, ?, ?)",
            (
                plan.source_fingerprint[:128],
                plan.processed_subjects,
                len(plan.conflicts),
                plan.skipped_external_bots,
                started_at,
                now,
                now,
                now,
            ),
        )
        for conflict in plan.conflicts:
            connection.execute(
                "INSERT INTO identity_conflicts ("
                "platform, external_id, subject_kind, conflict_kind, status, "
                "error_category, resolved_at, created_at, updated_at"
                ") VALUES (?, ?, ?, ?, 'open', ?, NULL, ?, ?) "
                "ON CONFLICT(platform, external_id, subject_kind, conflict_kind) DO UPDATE SET "
                "status = 'open', resolved_at = NULL, "
                "error_category = excluded.error_category, updated_at = excluded.updated_at",
                (
                    conflict.platform,
                    conflict.external_id,
                    conflict.subject_kind,
                    conflict.conflict_kind,
                    conflict.error_category,
                    now,
                    now,
                ),
            )

    def record_failed_run(
        self,
        connection: sqlite3.Connection,
        *,
        source_fingerprint: str,
        now: str,
        started_at: str,
        error_category: str,
    ) -> None:
        connection.execute(
            "INSERT INTO identity_backfill_runs ("
            "mode, status, checkpoint, processed_count, persons_count, "
            "identity_bindings_count, spaces_count, space_bindings_count, "
            "presences_count, conflicts_count, skipped_count, error_category, "
            "started_at, finished_at, created_at, updated_at"
            ") VALUES ('apply', 'failed', ?, 0, 0, 0, 0, 0, 0, 0, 0, ?, ?, ?, ?, ?)",
            (
                source_fingerprint[:128] or "apply_aborted",
                error_category,
                started_at,
                now,
                now,
                now,
            ),
        )

    def count_rows(self, connection: sqlite3.Connection, table: str) -> int:
        if not _table_exists(connection, table):
            return 0
        row = connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()
        return int(row[0] if row is not None else 0)

    def foreign_key_violations(self, connection: sqlite3.Connection) -> list[tuple[object, ...]]:
        return [tuple(row) for row in connection.execute("PRAGMA foreign_key_check")]

    def conversation_shadows_populated(self, connection: sqlite3.Connection) -> int:
        total = 0
        checks = (
            ("chat_events", "canonical_conversation_id"),
            ("chat_events", "canonical_event_id"),
            ("conversation_scopes", "canonical_conversation_id"),
            ("plugin_notification_outbox", "canonical_conversation_id"),
            ("plugin_background_turn_jobs", "canonical_conversation_id"),
            ("speech_generations", "canonical_conversation_id"),
            ("tool_invocations", "canonical_conversation_id"),
            ("web_search_runs", "canonical_conversation_id"),
            ("model_invocations", "canonical_conversation_id"),
            ("runtime_turn_observations", "canonical_conversation_id"),
            ("reply_effect_events", "canonical_conversation_id"),
        )
        for table, column in checks:
            if not _table_exists(connection, table) or not _column_exists(
                connection, table, column
            ):
                continue
            row = connection.execute(
                f'SELECT COUNT(*) FROM "{table}" WHERE "{column}" IS NOT NULL'
            ).fetchone()
            total += int(row[0] if row is not None else 0)
        return total
