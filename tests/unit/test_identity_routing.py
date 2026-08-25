"""Single-Presence unique route defaults for identity cutover."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest
from tests.unit.test_identity_cutover import (
    _NOW,
    _insert_canonical_conversation,
    _insert_event,
    _insert_scope,
    _open,
    _prepare_snapshots,
    _seed_identity,
    _service,
)
from tests.unit.test_migration_0043 import _upgrade


def _insert_identity_binding(
    connection: sqlite3.Connection,
    *,
    person_id: str,
    external_account_id: str,
    created_at: str = _NOW,
) -> str:
    binding_id = str(uuid4())
    connection.execute(
        "INSERT INTO identity_bindings("
        "id, person_id, platform, external_account_id, display_name, status, "
        "revision, created_at, updated_at"
        ") VALUES (?, ?, 'qq', ?, '', 'active', 1, ?, ?)",
        (binding_id, person_id, external_account_id, created_at, created_at),
    )
    return binding_id


def _insert_space_binding(
    connection: sqlite3.Connection,
    *,
    space_id: str,
    external_space_id: str,
    created_at: str = _NOW,
) -> str:
    binding_id = str(uuid4())
    connection.execute(
        "INSERT INTO space_bindings("
        "id, space_id, platform, external_space_id, display_name, status, "
        "revision, created_at, updated_at"
        ") VALUES (?, ?, 'qq', ?, '', 'active', 1, ?, ?)",
        (binding_id, space_id, external_space_id, created_at, created_at),
    )
    return binding_id


def test_single_presence_unique_defaults_succeed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = tmp_path / "unique-defaults.db"
    _upgrade(live, monkeypatch, "head")
    with _open(live) as connection:
        ids = _seed_identity(connection)
        connection.commit()
    settings = _prepare_snapshots(live, tmp_path / "unique-defaults-snap")
    planned = _service(live, settings).plan()
    assert planned.status == "succeeded", planned.error_category
    applied = _service(live, settings).apply(planned.source_fingerprint)
    assert applied.status == "succeeded", applied.error_category
    with _open(live) as connection:
        person = connection.execute(
            "SELECT identity_binding_id, presence_id, paused FROM person_active_routes "
            "WHERE person_id = ?",
            (ids["person"],),
        ).fetchone()
        assert person is not None
        assert str(person[0]) == ids["binding"]
        assert str(person[1]) == ids["presence"]
        assert int(person[2]) == 0
        ingest = connection.execute(
            "SELECT ingest_presence_id, paused FROM space_binding_ingest_routes "
            "WHERE space_binding_id = ?",
            (ids["space_binding"],),
        ).fetchone()
        assert ingest is not None
        assert str(ingest[0]) == ids["presence"]
        space = connection.execute(
            "SELECT space_binding_id, presence_id FROM space_active_routes WHERE space_id = ?",
            (ids["space"],),
        ).fetchone()
        assert space is not None
        assert str(space[0]) == ids["space_binding"]
        assert str(space[1]) == ids["presence"]
        assert connection.execute("SELECT COUNT(*) FROM person_active_routes").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM space_active_routes").fetchone()[0] == 1
        assert (
            connection.execute("SELECT COUNT(*) FROM space_binding_ingest_routes").fetchone()[0]
            == 1
        )


@pytest.mark.parametrize("first_external", ("1001", "1099"))
def test_single_presence_two_person_bindings_block(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    first_external: str,
) -> None:
    live = tmp_path / f"two-person-{first_external}.db"
    _upgrade(live, monkeypatch, "head")
    other = "1099" if first_external == "1001" else "1001"
    with _open(live) as connection:
        ids = _seed_identity(connection)
        connection.execute("DELETE FROM identity_bindings")
        earlier = "2026-08-23T00:00:00+00:00"
        later = "2026-08-25T00:00:00+00:00"
        _insert_identity_binding(
            connection,
            person_id=ids["person"],
            external_account_id=first_external,
            created_at=earlier,
        )
        _insert_identity_binding(
            connection,
            person_id=ids["person"],
            external_account_id=other,
            created_at=later,
        )
        connection.commit()
    settings = _prepare_snapshots(live, tmp_path / f"two-person-{first_external}-snap")
    planned = _service(live, settings).plan()
    assert planned.error_category == "route_ambiguity"
    with _open(live) as connection:
        assert connection.execute("SELECT COUNT(*) FROM person_active_routes").fetchone()[0] == 0
        assert connection.execute("SELECT state FROM identity_runtime_state").fetchone()[0] == "v1"


@pytest.mark.parametrize("first_external", ("2001", "2099"))
def test_single_presence_two_space_bindings_block(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    first_external: str,
) -> None:
    live = tmp_path / f"two-space-{first_external}.db"
    _upgrade(live, monkeypatch, "head")
    other = "2099" if first_external == "2001" else "2001"
    with _open(live) as connection:
        ids = _seed_identity(connection)
        connection.execute("DELETE FROM space_bindings")
        earlier = "2026-08-23T00:00:00+00:00"
        later = "2026-08-25T00:00:00+00:00"
        _insert_space_binding(
            connection,
            space_id=ids["space"],
            external_space_id=first_external,
            created_at=earlier,
        )
        _insert_space_binding(
            connection,
            space_id=ids["space"],
            external_space_id=other,
            created_at=later,
        )
        connection.commit()
    settings = _prepare_snapshots(live, tmp_path / f"two-space-{first_external}-snap")
    planned = _service(live, settings).plan()
    assert planned.error_category == "route_ambiguity"
    with _open(live) as connection:
        assert connection.execute("SELECT COUNT(*) FROM space_active_routes").fetchone()[0] == 0
        assert connection.execute("SELECT state FROM identity_runtime_state").fetchone()[0] == "v1"


@pytest.mark.parametrize(
    ("binding_created_at", "label"),
    (
        ("2026-08-23T00:00:00+00:00", "before-presence"),
        ("2026-08-25T00:00:00+00:00", "after-presence"),
    ),
)
def test_single_presence_defaults_are_ordering_independent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    binding_created_at: str,
    label: str,
) -> None:
    live = tmp_path / f"order-{label}.db"
    _upgrade(live, monkeypatch, "head")
    with _open(live) as connection:
        ids = _seed_identity(connection)
        connection.execute(
            "UPDATE identity_bindings SET created_at = ? WHERE id = ?",
            (binding_created_at, ids["binding"]),
        )
        connection.execute(
            "UPDATE space_bindings SET created_at = ? WHERE id = ?",
            (binding_created_at, ids["space_binding"]),
        )
        connection.execute(
            "UPDATE presences SET created_at = ? WHERE id = ?",
            ("2026-08-24T12:00:00+00:00", ids["presence"]),
        )
        connection.commit()
    settings = _prepare_snapshots(live, tmp_path / f"order-{label}-snap")
    planned = _service(live, settings).plan()
    assert planned.status == "succeeded", planned.error_category
    applied = _service(live, settings).apply(planned.source_fingerprint)
    assert applied.status == "succeeded", applied.error_category
    with _open(live) as connection:
        person = connection.execute(
            "SELECT identity_binding_id, presence_id FROM person_active_routes WHERE person_id = ?",
            (ids["person"],),
        ).fetchone()
        space = connection.execute(
            "SELECT space_binding_id, presence_id FROM space_active_routes WHERE space_id = ?",
            (ids["space"],),
        ).fetchone()
        assert person is not None and space is not None
        assert str(person[0]) == ids["binding"]
        assert str(person[1]) == ids["presence"]
        assert str(space[0]) == ids["space_binding"]
        assert str(space[1]) == ids["presence"]


def test_identity_preconfig_does_not_create_empty_conversation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = tmp_path / "no-empty-conversation.db"
    _upgrade(live, monkeypatch, "head")
    with _open(live) as connection:
        ids = _seed_identity(connection)
        connection.commit()
    settings = _prepare_snapshots(live, tmp_path / "no-empty-conversation-snap")
    planned = _service(live, settings).plan()
    assert planned.status == "succeeded", planned.error_category
    applied = _service(live, settings).apply(planned.source_fingerprint)
    assert applied.status == "succeeded", applied.error_category
    with _open(live) as connection:
        assert connection.execute("SELECT state FROM identity_runtime_state").fetchone()[0] == "v2"
        assert connection.execute("SELECT COUNT(*) FROM canonical_conversations").fetchone()[0] == 0
        assert (
            connection.execute("SELECT COUNT(*) FROM conversation_legacy_aliases").fetchone()[0]
            == 0
        )
        assert not list(
            connection.execute(
                "SELECT 1 FROM conversation_legacy_aliases WHERE scope_key LIKE 'cutover:%'"
            )
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM person_active_routes WHERE person_id = ?",
                (ids["person"],),
            ).fetchone()[0]
            == 1
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM space_active_routes WHERE space_id = ?",
                (ids["space"],),
            ).fetchone()[0]
            == 1
        )


def test_route_defaults_do_not_change_conversation_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = tmp_path / "routes-keep-generation.db"
    _upgrade(live, monkeypatch, "head")
    scope_key = "bot:8000:group:2001"
    with _open(live) as connection:
        ids = _seed_identity(connection)
        event_id = _insert_event(
            connection,
            bot_user_id="8000",
            platform_message_id="keep-gen",
            author_person_id=ids["person"],
        )
        _insert_scope(
            connection,
            scope_key=scope_key,
            bot_user_id="8000",
            group_id="2001",
            last_event_id=event_id,
            starts_after_event_id=event_id,
            generation=5,
            last_generation_change_event_id=event_id,
        )
        conversation_id = _insert_canonical_conversation(
            connection,
            kind="space",
            owner_id=ids["space"],
            primary_scope_key=scope_key,
            generation=5,
            starts_after_event_id=event_id,
            last_event_id=event_id,
            last_generation_change_event_id=event_id,
            covered_through_event_id=event_id,
        )
        connection.commit()
    settings = _prepare_snapshots(live, tmp_path / "routes-keep-generation-snap")
    planned = _service(live, settings).plan()
    assert planned.status == "succeeded", planned.error_category
    applied = _service(live, settings).apply(planned.source_fingerprint)
    assert applied.status == "succeeded", applied.error_category
    with _open(live) as connection:
        generation = connection.execute(
            "SELECT generation FROM canonical_conversations WHERE id = ?",
            (conversation_id,),
        ).fetchone()[0]
        rollup_generation = connection.execute(
            "SELECT generation FROM canonical_conversation_rollups WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()[0]
        assert int(generation) == 5
        assert int(rollup_generation) == 5
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM person_active_routes WHERE person_id = ?",
                (ids["person"],),
            ).fetchone()[0]
            == 1
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM space_active_routes WHERE space_id = ?",
                (ids["space"],),
            ).fetchone()[0]
            == 1
        )
