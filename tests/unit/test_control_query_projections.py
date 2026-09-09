"""C10 paged read projections: keyset, PII, v1/v2, and import isolation."""

from __future__ import annotations

import dataclasses
import json
import math
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import event, func, select, text

from qq_ai_bot.control_plane import (
    AuditEventView,
    ControlPrincipal,
    ControlQueryError,
    ControlQueryService,
    Cursor,
    DecisionContext,
    ExternalIdView,
    ExternalIdVisibility,
    PageRequest,
    PresenceConnectionState,
    PrincipalSource,
    ProblemCode,
    QueryResourceKind,
    StateEpoch,
    mask_external_id,
)
from qq_ai_bot.control_plane.paging import Page
from qq_ai_bot.control_plane.query_cursors import (
    CANONICAL_RESOURCE_KINDS,
    TIME_ID_RESOURCE_KINDS,
    allowed_cursor_phases,
)
from qq_ai_bot.control_plane.query_types import (
    LAST4_MIN_SOURCE_LENGTH,
    REDACTED_DISPLAY,
    QueryCursorPhase,
)
from qq_ai_bot.domain.identity import PersonId, PrincipalId, RequestId, SpaceId
from qq_ai_bot.gateway.providers import builtin_provider_catalog
from qq_ai_bot.gateway.providers.snowluma import SNOWLUMA_CAPABILITIES
from qq_ai_bot.gateway.registry import GatewayConnectionRegistry
from qq_ai_bot.identity.canonical_repository import IDENTITY_PLATFORM
from qq_ai_bot.identity.db_models import (
    CanonicalPersonModel,
    CanonicalSpaceModel,
    IdentityBindingModel,
    PresenceModel,
    SpaceBindingModel,
)
from qq_ai_bot.persistence.control_query import ControlQueryAdapter, project_audit_event
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import RuntimeConfigOverrideModel

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src"
CONTROL_PLANE_ROOT = SRC_ROOT / "qq_ai_bot" / "control_plane"
ADAPTER_PATH = SRC_ROOT / "qq_ai_bot" / "persistence" / "control_query.py"
_NOW = datetime(2026, 8, 24, 10, 0, tzinfo=UTC)
_READ_CAPS = (
    "identity.person.read",
    "identity.binding.read",
    "identity.binding.read_external",
    "identity.space.read",
    "identity.presence.read",
    "identity.membership.read",
    "conversation.metadata.read",
    "route.read",
    "control.system.read",
    "control.health.read",
    "control.audit.read",
    "control.operation.read",
    "control.config.read",
    "control.memory.metadata.read",
    "control.memory.content.read",
    "control.automation.read",
    "control.plugin.read",
    "control.mcp.read",
    "control.emoji.read",
    "control.speech.read",
)
_COUNT_TABLES = (
    "persons",
    "identity_bindings",
    "spaces",
    "space_bindings",
    "presences",
    "canonical_conversations",
    "admin_operation_events",
    "person_active_routes",
    "space_binding_ingest_routes",
    "space_active_routes",
)


def _principal(*capabilities: str) -> ControlPrincipal:
    return ControlPrincipal(
        principal_id=PrincipalId.new(),
        person_id=PersonId.new(),
        source=PrincipalSource.QQ,
        roles=("superuser",),
        granted_capabilities=capabilities,
        authenticated=True,
        active=True,
    )


def _context(
    principal: ControlPrincipal,
) -> DecisionContext[ControlPrincipal, PrincipalSource, object]:
    return DecisionContext(
        request_id=RequestId.new(),
        principal=principal,
        source=principal.source,
        canonical_target=principal.person_id or SpaceId.new(),
        reason="query",
    )


def _service(
    database: Database,
    *,
    connection_registry: object | None = None,
) -> ControlQueryService:
    return ControlQueryService(
        ControlQueryAdapter(database, connection_registry=connection_registry)
    )


def _uuid() -> str:
    return str(uuid4())


async def _counts(database: Database) -> dict[str, int]:
    async with database.sessions() as session:
        values: dict[str, int] = {}
        for table in _COUNT_TABLES:
            values[table] = int(await session.scalar(text(f"SELECT COUNT(*) FROM {table}")) or 0)
        return values


def _python_files(root: Path) -> tuple[Path, ...]:
    return tuple(sorted(path for path in root.rglob("*.py") if path.is_file()))


async def _add_person(
    database: Database,
    *,
    person_id: str | None = None,
    enabled: bool = True,
) -> str:
    token = person_id or _uuid()
    async with database.sessions() as session, session.begin():
        session.add(
            CanonicalPersonModel(
                id=token, enabled=enabled, revision=1, created_at=_NOW, updated_at=_NOW
            )
        )
    return token


async def _add_binding(
    database: Database,
    *,
    person_id: str,
    external_account_id: str,
    display_name: str = "Ada",
    binding_id: str | None = None,
) -> str:
    token = binding_id or _uuid()
    async with database.sessions() as session, session.begin():
        session.add(
            IdentityBindingModel(
                id=token,
                person_id=person_id,
                platform=IDENTITY_PLATFORM,
                external_account_id=external_account_id,
                display_name=display_name,
                status="active",
                revision=1,
                first_seen_at=_NOW,
                last_seen_at=_NOW,
                created_at=_NOW,
                updated_at=_NOW,
            )
        )
    return token


async def _add_space(
    database: Database,
    *,
    space_id: str | None = None,
    name: str = "room",
) -> str:
    token = space_id or _uuid()
    async with database.sessions() as session, session.begin():
        session.add(
            CanonicalSpaceModel(
                id=token,
                name=name,
                enabled=True,
                autonomous_enabled=True,
                require_mention=True,
                revision=1,
                created_at=_NOW,
                updated_at=_NOW,
            )
        )
    return token


async def _add_space_binding(
    database: Database,
    *,
    space_id: str,
    external_space_id: str,
    display_name: str = "group",
    binding_id: str | None = None,
) -> str:
    token = binding_id or _uuid()
    async with database.sessions() as session, session.begin():
        session.add(
            SpaceBindingModel(
                id=token,
                space_id=space_id,
                platform=IDENTITY_PLATFORM,
                external_space_id=external_space_id,
                display_name=display_name,
                status="active",
                revision=1,
                first_seen_at=_NOW,
                last_seen_at=_NOW,
                created_at=_NOW,
                updated_at=_NOW,
            )
        )
    return token


async def _add_presence(
    database: Database,
    *,
    external_account_id: str,
    presence_id: str | None = None,
) -> str:
    token = presence_id or _uuid()
    async with database.sessions() as session, session.begin():
        session.add(
            PresenceModel(
                id=token,
                platform=IDENTITY_PLATFORM,
                external_account_id=external_account_id,
                enabled=True,
                ingest_eligible=True,
                revision=1,
                created_at=_NOW,
                updated_at=_NOW,
            )
        )
    return token


@pytest.mark.asyncio
async def test_person_pages_are_unique_stable_keyset(database: Database) -> None:
    for _ in range(25):
        await _add_person(database)
    async with database.sessions() as session:
        ids = sorted(await session.scalars(select(CanonicalPersonModel.id)))
    service = _service(database)
    context = _context(_principal("identity.person.read"))
    seen: list[str] = []
    cursor = None
    pages = 0
    while True:
        page = await service.list_persons(context, PageRequest(limit=7, cursor=cursor))
        pages += 1
        chunk = [item.person_id.text for item in page.items if item.person_id is not None]
        assert len(chunk) == len(set(chunk))
        assert not set(chunk) & set(seen)
        seen.extend(chunk)
        if page.next_cursor is None:
            break
        cursor = page.next_cursor
    assert seen == ids
    assert pages >= 4


@pytest.mark.asyncio
async def test_keyset_survives_concurrent_insert_and_delete(database: Database) -> None:
    ids = sorted({await _add_person(database) for _ in range(12)})
    service = _service(database)
    context = _context(_principal("identity.person.read"))
    first = await service.list_persons(context, PageRequest(limit=5))
    first_ids = [item.person_id.text for item in first.items if item.person_id is not None]
    deleted = ids[7]
    async with database.sessions() as session, session.begin():
        row = await session.get(CanonicalPersonModel, deleted)
        assert row is not None
        await session.delete(row)
    inserted = await _add_person(database, person_id="ffffffff-ffff-4fff-8fff-ffffffffffff")
    second = await service.list_persons(context, PageRequest(limit=20, cursor=first.next_cursor))
    second_ids = [item.person_id.text for item in second.items if item.person_id is not None]
    assert not set(first_ids) & set(second_ids)
    assert deleted not in second_ids
    assert inserted in second_ids or inserted > first_ids[-1]


@pytest.mark.asyncio
async def test_cross_resource_and_tampered_cursors_are_rejected(database: Database) -> None:
    await _add_person(database)
    service = _service(database)
    context = _context(_principal("identity.person.read", "identity.space.read"))
    person_page = await service.list_persons(context, PageRequest(limit=1))
    assert person_page.next_cursor is None or person_page.items
    with pytest.raises(ControlQueryError) as cross:
        await service.list_spaces(
            context, PageRequest(limit=10, cursor=Cursor("c10v1|person|c|" + _uuid()))
        )
    assert cross.value.problem.code is ProblemCode.VALIDATION_ERROR
    with pytest.raises(ControlQueryError) as tampered:
        await service.list_persons(context, PageRequest(limit=10, cursor=Cursor("not-a-cursor")))
    assert tampered.value.problem.code is ProblemCode.VALIDATION_ERROR


@pytest.mark.asyncio
async def test_external_ids_are_masked_unless_read_external(database: Database) -> None:
    person_id = await _add_person(database)
    await _add_binding(database, person_id=person_id, external_account_id="55123456789")
    await _add_binding(
        database, person_id=person_id, external_account_id="ab", display_name="short"
    )
    masked_ctx = _context(_principal("identity.binding.read"))
    revealed_ctx = _context(_principal("identity.binding.read", "identity.binding.read_external"))
    service = _service(database)
    masked = await service.list_identity_bindings(masked_ctx, PageRequest(limit=20))
    revealed = await service.list_identity_bindings(revealed_ctx, PageRequest(limit=20))
    assert {item.external.visibility for item in masked.items} == {ExternalIdVisibility.MASKED}
    assert all(item.external.value is None for item in masked.items)
    long_item = next(item for item in masked.items if item.external.last4 == "6789")
    short_item = next(item for item in masked.items if item.display_name == "short")
    assert short_item.external.last4 is None
    dumped = json.dumps([dataclasses.asdict(item) for item in masked.items], default=str)
    assert "55123456789" not in dumped
    assert '"value": "ab"' not in dumped
    assert '"last4": "ab"' not in dumped
    assert {item.external.visibility for item in revealed.items} == {ExternalIdVisibility.REVEALED}
    values = {item.external.value for item in revealed.items}
    assert {"55123456789", "ab"} <= values
    assert long_item.external.configured is True


def test_mask_helper_rejects_short_last4_leak() -> None:
    assert LAST4_MIN_SOURCE_LENGTH == 8
    empty = mask_external_id("", reveal=False)
    assert empty == ExternalIdView(
        visibility=ExternalIdVisibility.MASKED,
        configured=False,
        last4=None,
        value=None,
    )
    empty_reveal = mask_external_id("", reveal=True)
    assert empty_reveal == empty
    five = mask_external_id("12345", reveal=False)
    assert five.last4 is None
    assert five.value is None
    assert five.configured is True
    eight = mask_external_id("12345678", reveal=False)
    assert eight.last4 is None
    assert eight.value is None
    nine = mask_external_id("123456789", reveal=False)
    assert nine.last4 == "6789"
    assert nine.value is None
    revealed = mask_external_id("abcd", reveal=True)
    assert revealed.value == "abcd"
    assert revealed.last4 is None
    assert revealed.visibility is ExternalIdVisibility.REVEALED


@pytest.mark.asyncio
async def test_capability_denial_is_not_an_empty_success(database: Database) -> None:
    await _add_person(database)
    service = _service(database)
    context = _context(_principal("identity.space.read"))
    with pytest.raises(ControlQueryError) as denied:
        await service.list_persons(context, PageRequest(limit=10))
    assert denied.value.problem.code is ProblemCode.CAPABILITY_DENIED


@pytest.mark.asyncio
async def test_presence_projection_reports_actual_provider_and_connection_generation(
    database: Database,
) -> None:
    presence_id = await _add_presence(database, external_account_id="8111")
    registry = GatewayConnectionRegistry(
        providers=builtin_provider_catalog(),
        gateway_instance_id="gw-control",
    )
    bot = SimpleNamespace(self_id="8111")
    registry.connect(bot, provider_id="snowluma", presence_id=presence_id)
    service = _service(database, connection_registry=registry)
    context = _context(_principal("identity.presence.read"))

    connected = await service.list_presences(context, PageRequest(limit=10))

    view = next(item for item in connected.items if item.presence_id.text == presence_id)
    assert view.connection_state is PresenceConnectionState.CONNECTED
    assert view.connection_provider == "snowluma"
    assert view.connection_generation == 1
    assert view.connection_capabilities == tuple(sorted(SNOWLUMA_CAPABILITIES))
    assert view.external.visibility is ExternalIdVisibility.MASKED

    registry.disconnect(bot)
    disconnected = await service.list_presences(context, PageRequest(limit=10))
    view = next(item for item in disconnected.items if item.presence_id.text == presence_id)
    assert view.connection_state is PresenceConnectionState.DISCONNECTED
    assert view.connection_provider is None
    assert view.connection_generation == 1
    assert view.connection_capabilities == ()


@pytest.mark.asyncio
async def test_synthetic_yuki_is_not_presence_count(database: Database) -> None:
    await _add_presence(database, external_account_id="8112")
    await _add_presence(database, external_account_id="8113")
    service = _service(database)
    context = _context(_principal("control.system.read", "control.health.read"))
    yuki = await service.read_yuki(context)
    system = await service.read_system(context)
    health = await service.read_health(context)
    async with database.sessions() as session:
        presence_count = int(await session.scalar(select(func.count(PresenceModel.id))) or 0)
    assert yuki.yuki_count == 1
    assert yuki.presence_count == presence_count
    assert system.presences.count == presence_count
    assert health.database == "ok"
    dumped = json.dumps(dataclasses.asdict(system), default=str)
    assert "sqlite" not in dumped
    assert database.url not in dumped


@pytest.mark.asyncio
async def test_pending_restart_exposes_keys_not_values(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.conftest import make_settings

    from qq_ai_bot.container import ApplicationContainer

    async with database.sessions() as session, session.begin():
        session.add(
            RuntimeConfigOverrideModel(
                config_key="llm.model",
                scope_type="global",
                value_json='"do-not-leak"',
                value_type="string",
                apply_mode="restart_required",
                version=1,
                created_at=_NOW,
                updated_at=_NOW,
                updated_by="9000",
            )
        )
    system = await _service(database).read_system(_context(_principal("control.system.read")))
    assert system.pending_restart.keys == ("llm.model",)
    assert system.pending_restart.count == 1
    dumped = json.dumps(dataclasses.asdict(system), default=str)
    assert "do-not-leak" not in dumped
    assert "9000" not in dumped
    # Exercise the production startup handoff, not just a copied Settings object.
    # Avoid constructing clients/workers; retain the actual override repository.
    original = make_settings(
        database.url, vision_enabled=False, vision_api_key="", vision_base_url=""
    )
    old_model = original.model_runtime.llm_model
    captured: dict[str, object] = {}

    def capture_constructor(self, settings, *, database, runtime_config):
        captured.update(settings=settings, database=database)

    monkeypatch.setattr(ApplicationContainer, "__init__", capture_constructor)
    await ApplicationContainer.create(original)
    active = captured["settings"]
    try:
        assert active.llm_model == "do-not-leak"
        assert active.model_runtime.llm_model == "do-not-leak"
        assert original.model_runtime.llm_model == old_model
        assert active.model_runtime is not original.model_runtime
    finally:
        await captured["database"].close()
    captured.clear()
    async with database.sessions() as session, session.begin():
        session.add(
            RuntimeConfigOverrideModel(
                config_key="vision.enabled",
                scope_type="global",
                value_json="true",
                value_type="boolean",
                apply_mode="restart_required",
                version=1,
                created_at=_NOW,
                updated_at=_NOW,
                updated_by="9000",
            )
        )
    with pytest.raises(ValueError, match="activated runtime settings failed validation") as failed:
        await ApplicationContainer.create(original)
    assert not captured  # Invalid combined settings must not create any clients.
    assert failed.value.__suppress_context__
    assert "do-not-leak" not in str(failed.value)


@pytest.mark.asyncio
async def test_binding_list_avoids_n_plus_one(database: Database) -> None:
    person_id = await _add_person(database)
    for index in range(12):
        await _add_binding(
            database,
            person_id=person_id,
            external_account_id=f"510{index:03d}",
        )
    statements: list[str] = []

    def _capture(_conn: object, clause: object, *_args: object, **_kwargs: object) -> None:
        statements.append(str(clause))

    engine = database.engine.sync_engine
    event.listen(engine, "before_cursor_execute", _capture)
    try:
        await _service(database).list_identity_bindings(
            _context(_principal("identity.binding.read")),
            PageRequest(limit=20),
        )
    finally:
        event.remove(engine, "before_cursor_execute", _capture)
    selects = [item for item in statements if "SELECT" in item.upper()]
    assert len(selects) <= 6
    assert all("OFFSET" not in item.upper() for item in statements)


@pytest.mark.asyncio
async def test_queries_do_not_write(database: Database) -> None:
    person_id = await _add_person(database)
    await _add_binding(database, person_id=person_id, external_account_id="1199")
    before = await _counts(database)
    service = _service(database)
    context = _context(_principal(*_READ_CAPS))
    await service.read_system(context)
    await service.list_persons(context, PageRequest(limit=10))
    await service.list_identity_bindings(context, PageRequest(limit=10))
    assert await _counts(database) == before
    async with database.sessions() as session:
        dirty = session.dirty or session.new or session.deleted
        assert not dirty


def _page_dump(page: Page[object]) -> str:
    return json.dumps(
        {
            "items": [dataclasses.asdict(item) for item in page.items],
            "next_cursor": None if page.next_cursor is None else page.next_cursor.value,
            "snapshot_at": None if page.snapshot_at is None else page.snapshot_at.isoformat(),
        },
        default=str,
    )


def _assert_page_hides(page: Page[object], *external_ids: str) -> None:
    dumped = _page_dump(page)
    for token in external_ids:
        assert token not in dumped
    assert page.snapshot_at is not None


def test_cursor_phase_table_covers_every_resource_kind() -> None:
    assert CANONICAL_RESOURCE_KINDS.isdisjoint(TIME_ID_RESOURCE_KINDS)
    assert CANONICAL_RESOURCE_KINDS | TIME_ID_RESOURCE_KINDS == frozenset(QueryResourceKind)
    for kind in QueryResourceKind:
        phases = allowed_cursor_phases(kind, epoch=StateEpoch.V2)
        if kind in CANONICAL_RESOURCE_KINDS:
            assert phases == frozenset({QueryCursorPhase.CANONICAL})
        else:
            assert phases == frozenset({QueryCursorPhase.TIME_ID})


def test_audit_event_view_rejects_nonfinite_and_negative_duration() -> None:
    created = datetime(2026, 1, 1, tzinfo=UTC)
    valid = AuditEventView(
        audit_id=1,
        capability="control.audit.read",
        operation="inspect",
        target_type="person",
        success=True,
        error_category=None,
        duration_seconds=0.0,
        created_at=created,
    )
    assert valid.duration_seconds == 0.0
    for duration in (math.nan, math.inf, -math.inf, -0.1, -1):
        with pytest.raises(ValueError):
            AuditEventView(
                audit_id=1,
                capability="control.audit.read",
                operation="inspect",
                target_type="person",
                success=True,
                error_category=None,
                duration_seconds=duration,
                created_at=created,
            )
        row = SimpleNamespace(
            id=1,
            capability="control.audit.read",
            operation="inspect",
            target_type="person",
            success=True,
            error_category=None,
            duration_seconds=duration,
            created_at=created,
        )
        with pytest.raises(ControlQueryError) as mismatch:
            project_audit_event(row, snapshot_at=created)
        assert mismatch.value.problem.code is ProblemCode.STATE_MISMATCH


@pytest.mark.asyncio
async def test_canonical_display_metadata_redacts_exact_and_embedded_ids(
    database: Database,
) -> None:
    person_id = await _add_person(database)
    space_id = await _add_space(database, name="75200000001")
    other_space = await _add_space(database, name="safe-room")
    await _add_binding(
        database,
        person_id=person_id,
        external_account_id="65123456789",
        display_name="65123456789",
    )
    await _add_binding(
        database,
        person_id=person_id,
        external_account_id="65223456780",
        display_name="QQ 65223456780",
    )
    await _add_binding(
        database,
        person_id=person_id,
        external_account_id="65323456781",
        display_name="Ada",
    )
    await _add_space_binding(
        database,
        space_id=space_id,
        external_space_id="75200000001",
        display_name="75200000001",
    )
    await _add_space_binding(
        database,
        space_id=other_space,
        external_space_id="75200000009",
        display_name="群 75200000009",
    )
    service = _service(database)
    masked_binding_ctx = _context(_principal("identity.binding.read"))
    masked_space_ctx = _context(_principal("identity.space.read"))
    reveal_ctx = _context(
        _principal(
            "identity.binding.read",
            "identity.space.read",
            "identity.binding.read_external",
        )
    )
    bindings = await service.list_identity_bindings(masked_binding_ctx, PageRequest(limit=20))
    spaces = await service.list_spaces(masked_space_ctx, PageRequest(limit=20))
    space_bindings = await service.list_space_bindings(masked_space_ctx, PageRequest(limit=20))
    _assert_page_hides(
        bindings,
        "65123456789",
        "65223456780",
        "65323456781",
    )
    _assert_page_hides(spaces, "75200000001", "75200000009")
    _assert_page_hides(space_bindings, "75200000001", "75200000009")
    names = {item.display_name for item in bindings.items}
    assert REDACTED_DISPLAY in names
    assert "Ada" in names
    space_names = {item.name for item in spaces.items}
    assert REDACTED_DISPLAY in space_names
    assert "safe-room" in space_names
    revealed_bindings = await service.list_identity_bindings(reveal_ctx, PageRequest(limit=20))
    revealed_spaces = await service.list_spaces(reveal_ctx, PageRequest(limit=20))
    revealed_space_bindings = await service.list_space_bindings(reveal_ctx, PageRequest(limit=20))
    assert {
        "65123456789",
        "QQ 65223456780",
        "Ada",
    } <= {item.display_name for item in revealed_bindings.items}
    assert {
        "65123456789",
        "65223456780",
        "65323456781",
    } <= {item.external.value for item in revealed_bindings.items}
    assert "75200000001" in {item.name for item in revealed_spaces.items}
    assert {
        "75200000001",
        "群 75200000009",
    } <= {item.display_name for item in revealed_space_bindings.items}
