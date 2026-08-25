"""C10 paged read projections: keyset, PII, v1/v2, and import isolation."""

from __future__ import annotations

import ast
import dataclasses
import json
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import get_type_hints
from uuid import uuid4

import pytest
from sqlalchemy import event, text

from qq_ai_bot.control_plane import (
    AuditEventView,
    ControlPrincipal,
    ControlQueryError,
    ControlQueryService,
    Cursor,
    DecisionContext,
    ExternalIdView,
    ExternalIdVisibility,
    IdentityResolution,
    PageRequest,
    PresenceConnectionState,
    PrincipalSource,
    ProblemCode,
    QueryResourceKind,
    RouteKind,
    RouteReferenceState,
    StateEpoch,
    classify_route_reference,
    is_protocol_capability,
    mask_external_id,
    sanitize_projected_display,
)
from qq_ai_bot.control_plane.paging import Page
from qq_ai_bot.control_plane.query_cursors import (
    CANONICAL_RESOURCE_KINDS,
    TIME_ID_RESOURCE_KINDS,
    TWO_PHASE_RESOURCE_KINDS,
    allowed_cursor_phases,
    decode_integer_cursor_key,
    decode_operation_cursor_key,
    decode_resource_cursor,
    decode_time_id_key,
    encode_operation_cursor_key,
)
from qq_ai_bot.control_plane.query_types import (
    LAST4_MIN_SOURCE_LENGTH,
    REDACTED_DISPLAY,
    QueryCursorPhase,
)
from qq_ai_bot.conversation.canonical_db_models import (
    CanonicalConversationModel,
    ConversationLegacyAliasModel,
    PersonActiveRouteModel,
    SpaceActiveRouteModel,
    SpaceBindingIngestRouteModel,
)
from qq_ai_bot.conversation.rollup.db_models import ConversationScopeModel
from qq_ai_bot.domain.identity import PersonId, PrincipalId, RequestId, SpaceId
from qq_ai_bot.gateway.providers import builtin_provider_catalog
from qq_ai_bot.gateway.providers.snowluma import SNOWLUMA_CAPABILITIES
from qq_ai_bot.gateway.registry import GatewayConnectionRegistry
from qq_ai_bot.health import HealthPayload
from qq_ai_bot.identity.db_models import (
    CanonicalPersonModel,
    CanonicalSpaceModel,
    IdentityBackfillRunModel,
    IdentityBindingModel,
    IdentityConflictModel,
    IdentityRuntimeStateModel,
    PresenceModel,
    SpaceBindingModel,
)
from qq_ai_bot.identity.inventory import IDENTITY_PLATFORM
from qq_ai_bot.persistence.control_query import ControlQueryAdapter, project_audit_event
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import (
    AdminOperationEventModel,
    GroupModel,
    PersonModel,
    RuntimeConfigOverrideModel,
)

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
    "people",
    "groups",
    "canonical_conversations",
    "conversation_scopes",
    "admin_operation_events",
    "identity_backfill_runs",
    "identity_conflicts",
    "identity_runtime_state",
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


async def _add_legacy_person(
    database: Database,
    user_id: str,
    *,
    is_bot: bool = False,
    nickname: str = "legacy",
    first_seen_at: datetime | None = None,
) -> None:
    seen = first_seen_at or _NOW
    async with database.sessions() as session, session.begin():
        session.add(
            PersonModel(
                user_id=user_id,
                nickname=nickname,
                enabled=True,
                is_bot=is_bot,
                first_seen_at=seen,
                last_seen_at=seen,
            )
        )


async def _add_legacy_group(
    database: Database,
    group_id: str,
    *,
    name: str = "legacy-group",
) -> None:
    async with database.sessions() as session, session.begin():
        session.add(
            GroupModel(
                group_id=group_id,
                name=name,
                enabled=True,
                require_mention=True,
                autonomous_enabled=True,
                first_seen_at=_NOW,
                last_seen_at=_NOW,
                updated_at=_NOW,
            )
        )


async def _set_v2(database: Database) -> None:
    async with database.sessions() as session, session.begin():
        row = await session.get(IdentityRuntimeStateModel, 1)
        assert row is not None
        row.state = "v2"
        row.cutover_id = _uuid()
        row.source_fingerprint = "a" * 64
        row.completed_at = _NOW


@pytest.mark.asyncio
async def test_empty_pages_have_snapshot_and_no_cursor(database: Database) -> None:
    service = _service(database)
    context = _context(_principal(*_READ_CAPS))
    before = await _counts(database)
    page = await service.list_persons(context, PageRequest(limit=20))
    assert page.items == ()
    assert page.next_cursor is None
    assert page.snapshot_at is not None
    assert page.snapshot_at.tzinfo is not None
    assert await _counts(database) == before


@pytest.mark.asyncio
async def test_person_pages_are_unique_stable_keyset(database: Database) -> None:
    ids = sorted({await _add_person(database) for _ in range(25)})
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
async def test_v1_legacy_fallback_has_no_fake_uuid_and_v2_does_not_fallback(
    database: Database,
) -> None:
    person_id = await _add_person(database)
    await _add_legacy_person(database, "12345678")
    await _add_legacy_person(database, "ab")
    service = _service(database)
    context = _context(_principal("identity.person.read", "identity.binding.read"))
    page = await service.list_persons(context, PageRequest(limit=20))
    resolutions = {item.resolution for item in page.items}
    assert IdentityResolution.CANONICAL in resolutions
    assert IdentityResolution.UNRESOLVED in resolutions
    unresolved = [item for item in page.items if item.resolution is IdentityResolution.UNRESOLVED]
    assert all(item.person_id is None for item in unresolved)
    dumped = json.dumps([dataclasses.asdict(item) for item in page.items], default=str)
    assert person_id in dumped
    assert "12345678" not in dumped
    bindings = await service.list_identity_bindings(context, PageRequest(limit=20))
    legacy_bindings = [
        item for item in bindings.items if item.resolution is IdentityResolution.UNRESOLVED
    ]
    assert legacy_bindings
    assert all(item.binding_id is None and item.person_id is None for item in legacy_bindings)
    assert all(item.external.value is None for item in legacy_bindings)
    await _set_v2(database)
    v2 = await service.list_persons(context, PageRequest(limit=20))
    assert {item.resolution for item in v2.items} == {IdentityResolution.CANONICAL}
    assert all(item.person_id is not None for item in v2.items)


@pytest.mark.asyncio
async def test_external_ids_are_masked_unless_read_external(database: Database) -> None:
    person_id = await _add_person(database)
    await _add_binding(database, person_id=person_id, external_account_id="123456789")
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
    assert "123456789" not in dumped
    assert '"value": "ab"' not in dumped
    assert '"last4": "ab"' not in dumped
    assert {item.external.visibility for item in revealed.items} == {ExternalIdVisibility.REVEALED}
    values = {item.external.value for item in revealed.items}
    assert values == {"123456789", "ab"}
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


def test_display_redaction_requires_a_concrete_full_external_id() -> None:
    assert REDACTED_DISPLAY == "redacted"
    assert (
        sanitize_projected_display(
            "123456789",
            external_ids=("123456789",),
            reveal=False,
        )
        == REDACTED_DISPLAY
    )
    assert (
        sanitize_projected_display(
            "QQ 123456789",
            external_ids=("123456789",),
            reveal=False,
        )
        == REDACTED_DISPLAY
    )
    assert (
        sanitize_projected_display(
            "Ada",
            external_ids=("123456789",),
            reveal=False,
        )
        == "Ada"
    )
    assert (
        sanitize_projected_display(
            "QQ 123456789",
            external_ids=("123456789",),
            reveal=True,
        )
        == "QQ 123456789"
    )
    assert sanitize_projected_display("Ada", external_ids=("",), reveal=False) == "Ada"


@pytest.mark.asyncio
async def test_conversation_audit_and_backfill_omit_content_and_secrets(
    database: Database,
) -> None:
    person_id = await _add_person(database)
    await _add_legacy_person(database, "8000", is_bot=True)
    await _add_legacy_person(database, "1001")
    conversation_id = _uuid()
    alias_id = _uuid()
    async with database.sessions() as session, session.begin():
        session.add(
            CanonicalConversationModel(
                id=conversation_id,
                kind="private",
                person_id=person_id,
                space_id=None,
                primary_alias_id=alias_id,
                primary_marker=1,
                generation=2,
                starts_after_event_id=0,
                last_event_id=9,
                last_generation_change_event_id=3,
                covered_through_event_id=8,
                uncovered_event_count=1,
                uncovered_character_count=12,
                revision=1,
                created_at=_NOW,
                updated_at=_NOW,
            )
        )
        session.add(
            ConversationLegacyAliasModel(
                id=alias_id,
                conversation_id=conversation_id,
                scope_key="private:8000:12345678",
                is_primary=1,
                created_at=_NOW,
                updated_at=_NOW,
            )
        )
        session.add(
            ConversationScopeModel(
                scope_key="private:8000:1001",
                bot_user_id="8000",
                scope_type="private",
                private_peer_user_id="1001",
                group_id=None,
                generation=1,
                starts_after_event_id=0,
                last_event_id=4,
                last_generation_change_event_id=0,
                uncovered_event_count=4,
                uncovered_character_count=40,
                created_at=_NOW,
                updated_at=_NOW,
                canonical_conversation_id=None,
            )
        )
        session.add(
            AdminOperationEventModel(
                actor_user_id="9000",
                trigger_message_id="trigger-1",
                conversation_key="private:9000",
                capability="web_search",
                operation="inspect",
                target_type="person",
                target_id="12345678",
                before_json='{"api_key":"sk-secret","path":"C:/secrets/key"}',
                after_json='{"cookie":"sid=1","reasoning":"hidden"}',
                success=True,
                error_category=None,
                duration_seconds=0.25,
                created_at=_NOW,
            )
        )
        session.add(
            IdentityBackfillRunModel(
                mode="dry_run",
                status="running",
                checkpoint="C:/secrets/backfill.json",
                processed_count=4,
                persons_count=1,
                identity_bindings_count=1,
                spaces_count=0,
                space_bindings_count=0,
                presences_count=0,
                conflicts_count=1,
                skipped_count=0,
                error_category=None,
                started_at=_NOW,
                finished_at=None,
                created_at=_NOW,
                updated_at=_NOW + timedelta(seconds=3),
            )
        )
        session.add(
            IdentityConflictModel(
                platform=IDENTITY_PLATFORM,
                external_id="12345678",
                subject_kind="account",
                conflict_kind="ambiguous_identity",
                status="open",
                error_category="unclassified",
                created_at=_NOW,
                updated_at=_NOW,
            )
        )
    service = _service(database)
    context = _context(_principal(*_READ_CAPS))
    conversations = await service.list_conversations(context, PageRequest(limit=20))
    audits = await service.list_audit_events(context, PageRequest(limit=20))
    operations = await service.list_backfill_operations(context, PageRequest(limit=20))
    conflicts = await service.list_backfill_conflicts(context, PageRequest(limit=20))
    payload = json.dumps(
        {
            "conversations": [dataclasses.asdict(item) for item in conversations.items],
            "audits": [dataclasses.asdict(item) for item in audits.items],
            "operations": [dataclasses.asdict(item) for item in operations.items],
            "conflicts": [dataclasses.asdict(item) for item in conflicts.items],
        },
        default=str,
    )
    for leaked in (
        "12345678",
        "sk-secret",
        "C:/secrets",
        "sid=1",
        "hidden",
        "private:8000:12345678",
        "api_key",
        "cookie",
        "reasoning",
    ):
        assert leaked not in payload
    assert any(item.capability == "web_search" for item in audits.items)
    assert conversations.items
    assert all(not hasattr(item, "content") for item in conversations.items)
    assert operations.items[0].operation.state_epoch.value == "v1"


@pytest.mark.asyncio
async def test_runtime_state_missing_fail_close(database: Database) -> None:
    async with database.sessions() as session, session.begin():
        row = await session.get(IdentityRuntimeStateModel, 1)
        assert row is not None
        await session.delete(row)
    service = _service(database)
    context = _context(_principal("control.system.read"))
    with pytest.raises(ControlQueryError) as denied:
        await service.read_system(context)
    assert denied.value.problem.code is ProblemCode.STATE_MISMATCH


@pytest.mark.asyncio
async def test_capability_denial_is_not_an_empty_success(database: Database) -> None:
    await _add_person(database)
    service = _service(database)
    context = _context(_principal("identity.space.read"))
    with pytest.raises(ControlQueryError) as denied:
        await service.list_persons(context, PageRequest(limit=10))
    assert denied.value.problem.code is ProblemCode.CAPABILITY_DENIED


@pytest.mark.asyncio
async def test_routes_and_presence_unavailable_without_get_bots(database: Database) -> None:
    person_id = await _add_person(database)
    space_id = await _add_space(database)
    binding_id = await _add_binding(database, person_id=person_id, external_account_id="1001")
    space_binding_id = await _add_space_binding(
        database, space_id=space_id, external_space_id="2001"
    )
    presence_id = await _add_presence(database, external_account_id="8000")
    async with database.sessions() as session, session.begin():
        session.add(
            PersonActiveRouteModel(
                person_id=person_id,
                identity_binding_id=binding_id,
                presence_id=presence_id,
                route_generation=1,
                paused=False,
                revision=1,
                created_at=_NOW,
                updated_at=_NOW,
            )
        )
        session.add(
            SpaceBindingIngestRouteModel(
                space_binding_id=space_binding_id,
                ingest_presence_id=presence_id,
                route_generation=1,
                paused=False,
                revision=1,
                created_at=_NOW,
                updated_at=_NOW,
            )
        )
        session.add(
            SpaceActiveRouteModel(
                space_id=space_id,
                space_binding_id=space_binding_id,
                presence_id=presence_id,
                route_generation=1,
                paused=False,
                revision=1,
                created_at=_NOW,
                updated_at=_NOW,
            )
        )
    service = _service(database)
    context = _context(_principal("route.read", "identity.presence.read"))
    person_routes = await service.list_person_active_routes(context, PageRequest(limit=10))
    ingest_routes = await service.list_space_binding_ingest_routes(context, PageRequest(limit=10))
    space_routes = await service.list_space_active_routes(context, PageRequest(limit=10))
    presences = await service.list_presences(context, PageRequest(limit=10))
    assert person_routes.items[0].kind is RouteKind.PERSON_ACTIVE
    assert ingest_routes.items[0].kind is RouteKind.SPACE_BINDING_INGEST
    assert space_routes.items[0].kind is RouteKind.SPACE_ACTIVE
    assert person_routes.items[0].reference_state is RouteReferenceState.CONSISTENT
    assert ingest_routes.items[0].reference_state is RouteReferenceState.CONSISTENT
    assert space_routes.items[0].reference_state is RouteReferenceState.CONSISTENT
    assert presences.items[0].connection_state is PresenceConnectionState.UNAVAILABLE
    assert presences.items[0].connection_provider is None
    assert presences.items[0].connection_generation is None
    assert presences.items[0].connection_capabilities == ()
    assert presences.items[0].connection_problem.code is ProblemCode.OPERATION_UNAVAILABLE


@pytest.mark.asyncio
async def test_presence_projection_reports_actual_provider_and_connection_generation(
    database: Database,
) -> None:
    presence_id = await _add_presence(database, external_account_id="8000")
    registry = GatewayConnectionRegistry(
        providers=builtin_provider_catalog(),
        gateway_instance_id="gw-control",
    )
    bot = SimpleNamespace(self_id="8000")
    registry.connect(bot, provider_id="snowluma", presence_id=presence_id)
    service = _service(database, connection_registry=registry)
    context = _context(_principal("identity.presence.read"))

    connected = await service.list_presences(context, PageRequest(limit=10))

    view = connected.items[0]
    assert view.connection_state is PresenceConnectionState.CONNECTED
    assert view.connection_provider == "snowluma"
    assert view.connection_generation == 1
    assert view.connection_capabilities == tuple(sorted(SNOWLUMA_CAPABILITIES))
    assert view.external.visibility is ExternalIdVisibility.MASKED

    registry.disconnect(bot)
    disconnected = await service.list_presences(context, PageRequest(limit=10))
    assert disconnected.items[0].connection_state is PresenceConnectionState.DISCONNECTED
    assert disconnected.items[0].connection_provider is None
    assert disconnected.items[0].connection_generation == 1
    assert disconnected.items[0].connection_capabilities == ()


def test_route_reference_classifier_does_not_invent_a_match() -> None:
    assert (
        classify_route_reference(
            expected_owner_id="p1",
            actual_owner_id="p2",
            binding_platform="qq",
            presence_platform="qq",
        )
        is RouteReferenceState.STATE_MISMATCH
    )
    assert (
        classify_route_reference(
            expected_owner_id="p1",
            actual_owner_id="p1",
            binding_platform="qq",
            presence_platform="telegram",
        )
        is RouteReferenceState.STATE_MISMATCH
    )
    assert (
        classify_route_reference(
            expected_owner_id="p1",
            actual_owner_id=None,
            binding_platform="qq",
            presence_platform="qq",
        )
        is RouteReferenceState.STATE_MISMATCH
    )


@pytest.mark.asyncio
async def test_synthetic_yuki_is_not_presence_count(database: Database) -> None:
    await _add_presence(database, external_account_id="8000")
    await _add_presence(database, external_account_id="8001")
    service = _service(database)
    context = _context(_principal("control.system.read", "control.health.read"))
    yuki = await service.read_yuki(context)
    system = await service.read_system(context)
    health = await service.read_health(context)
    assert yuki.yuki_count == 1
    assert yuki.presence_count == 2
    assert system.presences.canonical == 2
    assert health.database == "ok"
    dumped = json.dumps(dataclasses.asdict(system), default=str)
    assert "sqlite" not in dumped
    assert database.url not in dumped


@pytest.mark.asyncio
async def test_pending_restart_exposes_keys_not_values(database: Database) -> None:
    async with database.sessions() as session, session.begin():
        session.add(
            RuntimeConfigOverrideModel(
                config_key="llm.model",
                scope_type="global",
                scope_id="",
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


@pytest.mark.asyncio
async def test_binding_list_avoids_n_plus_one(database: Database) -> None:
    person_id = await _add_person(database)
    for index in range(12):
        await _add_binding(
            database,
            person_id=person_id,
            external_account_id=f"100{index:02d}",
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
    await _add_binding(database, person_id=person_id, external_account_id="1001")
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


def test_control_plane_stays_free_of_orm_and_adapter_has_no_offset() -> None:
    forbidden = (
        "sqlalchemy",
        "qq_ai_bot.persistence",
        "AdminActor",
        "get_bots",
        "OFFSET",
        "offset",
    )
    for path in _python_files(CONTROL_PLANE_ROOT):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith("sqlalchemy")
                    assert alias.name != "qq_ai_bot.persistence"
            if isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith("sqlalchemy")
                assert not node.module.startswith("qq_ai_bot.persistence")
            if isinstance(node, ast.Name):
                assert node.id != "Any"
                assert node.id != "AdminActor"
                assert node.id != "get_bots"
    adapter = ADAPTER_PATH.read_text(encoding="utf-8")
    tree = ast.parse(adapter, filename=str(ADAPTER_PATH))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr != "offset"
            assert node.func.attr != "paginate"
        if isinstance(node, ast.Name):
            assert node.id != "get_bots"
    assert "get_bots" not in adapter
    assert ".offset(" not in adapter
    assert "unresolved[-1].user_id" not in adapter
    assert "unresolved[-1].group_id" not in adapter
    assert "PersonModel.user_id >" not in adapter
    assert "GroupModel.group_id >" not in adapter
    assert 'literal_column("rowid", Integer)' in adapter
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "encode_query_cursor"
        ):
            for arg in (*node.args, *tuple(item.value for item in node.keywords)):
                if isinstance(arg, ast.Attribute) and arg.attr in {
                    "user_id",
                    "group_id",
                    "external_account_id",
                    "external_space_id",
                    "nickname",
                }:
                    raise AssertionError("legacy external id encoded into cursor")
    assert not any(
        isinstance(node, ast.Constant)
        and type(node.value) is str
        and "OFFSET" in node.value.upper()
        for node in ast.walk(tree)
    )
    assert "__tablename__" not in "".join(
        path.read_text(encoding="utf-8") for path in _python_files(CONTROL_PLANE_ROOT)
    )
    for token in forbidden[:3]:
        assert (
            token
            not in "\n".join(
                path.read_text(encoding="utf-8")
                for path in _python_files(CONTROL_PLANE_ROOT)
                if path.name != "query_types.py"
            )
            or token == "OFFSET"
        )


def test_public_healthz_shape_is_unchanged() -> None:
    keys = set(get_type_hints(HealthPayload))
    assert "status" in keys
    assert "version" in keys
    assert "database" in keys
    assert "onebot_connected" in keys
    assert "webui_token" not in keys
    assert "api_key" not in keys


def test_no_yuki_table_and_no_delivery_route() -> None:
    adapter = ADAPTER_PATH.read_text(encoding="utf-8")
    assert 'tablename__ = "yuki"' not in adapter
    assert "DeliveryRoute" not in adapter
    assert "presence_active_route" not in adapter
    assert "YukiModel" not in adapter


_KIND_LISTERS: dict[QueryResourceKind, tuple[str, tuple[str, ...]]] = {
    QueryResourceKind.PERSON: ("list_persons", ("identity.person.read",)),
    QueryResourceKind.BINDING: ("list_identity_bindings", ("identity.binding.read",)),
    QueryResourceKind.SPACE: ("list_spaces", ("identity.space.read",)),
    QueryResourceKind.SPACE_BINDING: ("list_space_bindings", ("identity.space.read",)),
    QueryResourceKind.PRESENCE: ("list_presences", ("identity.presence.read",)),
    QueryResourceKind.CONVERSATION: ("list_conversations", ("conversation.metadata.read",)),
    QueryResourceKind.PERSON_ROUTE: ("list_person_active_routes", ("route.read",)),
    QueryResourceKind.INGEST_ROUTE: ("list_space_binding_ingest_routes", ("route.read",)),
    QueryResourceKind.SPACE_ROUTE: ("list_space_active_routes", ("route.read",)),
    QueryResourceKind.AUDIT: ("list_audit_events", ("control.audit.read",)),
    QueryResourceKind.OPERATION: ("list_backfill_operations", ("control.operation.read",)),
    QueryResourceKind.CONFLICT: ("list_backfill_conflicts", ("control.operation.read",)),
    QueryResourceKind.CONFIG: ("list_config_specs", ("control.config.read",)),
    QueryResourceKind.MEMORY_FACT: ("list_memory_facts", ("control.memory.metadata.read",)),
    QueryResourceKind.MEMORY_JOB: ("list_memory_jobs", ("control.memory.metadata.read",)),
    QueryResourceKind.AUTOMATION: ("list_automations", ("control.automation.read",)),
    QueryResourceKind.PLUGIN: ("list_plugins", ("control.plugin.read",)),
    QueryResourceKind.MCP: ("list_mcp_servers", ("control.mcp.read",)),
    QueryResourceKind.EMOJI: ("list_emoji_assets", ("control.emoji.read",)),
    QueryResourceKind.SPEECH: ("list_speech_profiles", ("control.speech.read",)),
}


def _phase_payload(kind: QueryResourceKind, phase: QueryCursorPhase) -> str:
    if kind is QueryResourceKind.OPERATION:
        return "2026-01-01T00:00:00+00:00#1#1"
    if kind in {
        QueryResourceKind.CONFLICT,
        QueryResourceKind.MEMORY_FACT,
        QueryResourceKind.MEMORY_JOB,
        QueryResourceKind.AUTOMATION,
    }:
        return "1"
    if kind in TWO_PHASE_RESOURCE_KINDS and phase is QueryCursorPhase.UNRESOLVED:
        return "0"
    if phase is QueryCursorPhase.TIME_ID:
        return "2026-01-01T00:00:00+00:00#1"
    return "cursor-token"


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
    assert TWO_PHASE_RESOURCE_KINDS.isdisjoint(CANONICAL_RESOURCE_KINDS)
    assert TWO_PHASE_RESOURCE_KINDS.isdisjoint(TIME_ID_RESOURCE_KINDS)
    assert CANONICAL_RESOURCE_KINDS.isdisjoint(TIME_ID_RESOURCE_KINDS)
    assert (
        TWO_PHASE_RESOURCE_KINDS | CANONICAL_RESOURCE_KINDS | TIME_ID_RESOURCE_KINDS
        == frozenset(QueryResourceKind)
    )
    assert set(_KIND_LISTERS) == set(QueryResourceKind)
    for kind in QueryResourceKind:
        v1 = allowed_cursor_phases(kind, epoch=StateEpoch.V1)
        v2 = allowed_cursor_phases(kind, epoch=StateEpoch.V2)
        if kind in TWO_PHASE_RESOURCE_KINDS:
            assert v1 == frozenset({QueryCursorPhase.CANONICAL, QueryCursorPhase.UNRESOLVED})
            assert v2 == frozenset({QueryCursorPhase.CANONICAL})
        elif kind in CANONICAL_RESOURCE_KINDS:
            assert v1 == v2 == frozenset({QueryCursorPhase.CANONICAL})
        else:
            assert v1 == v2 == frozenset({QueryCursorPhase.TIME_ID})


def test_decode_resource_cursor_rejects_unsupported_phase_and_payload() -> None:
    for kind in QueryResourceKind:
        allowed = allowed_cursor_phases(kind, epoch=StateEpoch.V1)
        for phase in QueryCursorPhase:
            cursor = Cursor(f"c10v1|{kind.value}|{phase.value}|{_phase_payload(kind, phase)}")
            if phase in allowed:
                decoded = decode_resource_cursor(cursor, expected_kind=kind, epoch=StateEpoch.V1)
                assert decoded[0] is phase
            else:
                with pytest.raises(ControlQueryError) as rejected:
                    decode_resource_cursor(cursor, expected_kind=kind, epoch=StateEpoch.V1)
                assert rejected.value.problem.code is ProblemCode.VALIDATION_ERROR
        if kind in TWO_PHASE_RESOURCE_KINDS:
            unresolved = Cursor(
                f"c10v1|{kind.value}|u|{_phase_payload(kind, QueryCursorPhase.UNRESOLVED)}"
            )
            with pytest.raises(ControlQueryError) as v2_rejected:
                decode_resource_cursor(unresolved, expected_kind=kind, epoch=StateEpoch.V2)
            assert v2_rejected.value.problem.code is ProblemCode.VALIDATION_ERROR
    for raw in (
        "c10v1|operation|c|not-int",
        "c10v1|operation|c|-1",
        "c10v1|operation|c|0",
        "c10v1|operation|c|+1",
        "c10v1|operation|c|01",
        "c10v1|operation|c|1",
        "c10v1|conflict|c|not-int",
        "c10v1|conflict|c|-1",
        "c10v1|conflict|c|0",
        "c10v1|conversation|u|not-int",
        "c10v1|conversation|u|-1",
        "c10v1|conversation|u|+1",
        "c10v1|conversation|u|01",
        "c10v1|person|u|not-int",
        "c10v1|person|u|-1",
        "c10v1|person|u|01",
        "c10v1|binding|u|not-int",
        "c10v1|binding|u|-1",
        "c10v1|binding|u|01",
        "c10v1|space|u|not-int",
        "c10v1|space|u|-1",
        "c10v1|space|u|01",
        "c10v1|space_binding|u|not-int",
        "c10v1|space_binding|u|-1",
        "c10v1|space_binding|u|01",
        "c10v2|person|c|token",
        "c10v1|person|c|token",
        "c10v1|audit|t|2026-01-01T00:00:00+00:00",
        "c10v1|audit|t|2026-01-01T00:00:00#1",
        "c10v1|audit|t|2026-01-01T00:00:00+00:00#0",
        "c10v1|audit|t|2026-01-01T00:00:00+00:00#-1",
        "c10v1|audit|c|1",
    ):
        kind_token = raw.split("|")[1] if raw.count("|") >= 3 else "person"
        kind = QueryResourceKind(kind_token)
        if raw == "c10v1|person|c|token":
            with pytest.raises(ControlQueryError) as cross:
                decode_resource_cursor(
                    Cursor(raw), expected_kind=QueryResourceKind.SPACE, epoch=StateEpoch.V1
                )
            assert cross.value.problem.code is ProblemCode.VALIDATION_ERROR
            continue
        with pytest.raises(ControlQueryError) as rejected:
            decode_resource_cursor(Cursor(raw), expected_kind=kind, epoch=StateEpoch.V1)
        assert rejected.value.problem.code is ProblemCode.VALIDATION_ERROR
    decode_integer_cursor_key("0", minimum=0)
    decode_integer_cursor_key("1", minimum=1)
    decode_time_id_key("2026-01-01T00:00:00+00:00#1")
    with pytest.raises(ControlQueryError) as zero:
        decode_integer_cursor_key("0", minimum=1)
    assert zero.value.problem.code is ProblemCode.VALIDATION_ERROR


def test_external_id_view_rejects_contradictory_protocol_states() -> None:
    with pytest.raises(ValueError):
        ExternalIdView(
            visibility=ExternalIdVisibility.MASKED,
            configured=False,
            last4="2345",
            value=None,
        )
    with pytest.raises(ValueError):
        ExternalIdView(
            visibility=ExternalIdVisibility.REVEALED,
            configured=True,
            last4=None,
            value=None,
        )
    with pytest.raises(ValueError):
        ExternalIdView(
            visibility=ExternalIdVisibility.REVEALED,
            configured=True,
            last4=None,
            value="",
        )
    with pytest.raises(ValueError):
        ExternalIdView(
            visibility=ExternalIdVisibility.REVEALED,
            configured=False,
            last4=None,
            value="12345",
        )
    with pytest.raises(ValueError):
        ExternalIdView(
            visibility=ExternalIdVisibility.MASKED,
            configured=True,
            last4=None,
            value="12345",
        )
    with pytest.raises(ValueError):
        ExternalIdView(
            visibility=ExternalIdVisibility.REVEALED,
            configured=True,
            last4="2345",
            value="123456789",
        )
    unconfigured = ExternalIdView(
        visibility=ExternalIdVisibility.MASKED,
        configured=False,
        last4=None,
        value=None,
    )
    assert unconfigured.last4 is None
    masked_partial = ExternalIdView(
        visibility=ExternalIdVisibility.MASKED,
        configured=True,
        last4="6789",
        value=None,
    )
    assert masked_partial.last4 == "6789"
    revealed = ExternalIdView(
        visibility=ExternalIdVisibility.REVEALED,
        configured=True,
        last4=None,
        value="123456789",
    )
    assert revealed.value == "123456789"


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
async def test_every_resource_rejects_unsupported_cursor_phase(database: Database) -> None:
    service = _service(database)
    for kind, (method_name, caps) in _KIND_LISTERS.items():
        method = getattr(service, method_name)
        context = _context(_principal(*caps))
        allowed = allowed_cursor_phases(kind, epoch=StateEpoch.V1)
        for phase in QueryCursorPhase:
            request = PageRequest(
                limit=5,
                cursor=Cursor(f"c10v1|{kind.value}|{phase.value}|{_phase_payload(kind, phase)}"),
            )
            if phase in allowed:
                page = await method(context, request)
                assert page.snapshot_at is not None
            else:
                with pytest.raises(ControlQueryError) as rejected:
                    await method(context, request)
                assert rejected.value.problem.code is ProblemCode.VALIDATION_ERROR


@pytest.mark.asyncio
async def test_cursor_payload_errors_are_controlled_validation_errors(database: Database) -> None:
    service = _service(database)
    cases = (
        (
            "list_backfill_operations",
            ("control.operation.read",),
            "c10v1|operation|c|not-int",
        ),
        (
            "list_backfill_operations",
            ("control.operation.read",),
            "c10v1|operation|c|1",
        ),
        (
            "list_backfill_operations",
            ("control.operation.read",),
            "c10v1|operation|c|-1",
        ),
        (
            "list_backfill_conflicts",
            ("control.operation.read",),
            "c10v1|conflict|c|-1",
        ),
        (
            "list_conversations",
            ("conversation.metadata.read",),
            "c10v1|conversation|u|not-int",
        ),
        (
            "list_conversations",
            ("conversation.metadata.read",),
            "c10v1|conversation|t|x",
        ),
        (
            "list_persons",
            ("identity.person.read",),
            "c10v1|person|t|2026-01-01T00:00:00+00:00#1",
        ),
        (
            "list_audit_events",
            ("control.audit.read",),
            "c10v1|audit|c|1",
        ),
        (
            "list_audit_events",
            ("control.audit.read",),
            "c10v1|audit|t|2026-01-01T00:00:00+00:00#0",
        ),
    )
    for method_name, caps, raw in cases:
        with pytest.raises(ControlQueryError) as rejected:
            await getattr(service, method_name)(
                _context(_principal(*caps)),
                PageRequest(limit=5, cursor=Cursor(raw)),
            )
        assert rejected.value.problem.code is ProblemCode.VALIDATION_ERROR
        assert type(rejected.value) is ControlQueryError


@pytest.mark.asyncio
async def test_membership_cannot_query_conversations(database: Database) -> None:
    service = _service(database)
    with pytest.raises(ControlQueryError) as denied:
        await service.list_conversations(
            _context(_principal("identity.membership.read")),
            PageRequest(limit=5),
        )
    assert denied.value.problem.code is ProblemCode.CAPABILITY_DENIED
    allowed = await service.list_conversations(
        _context(_principal("conversation.metadata.read")),
        PageRequest(limit=5),
    )
    assert allowed.items == ()
    assert "conversation.content.read" not in _READ_CAPS
    assert is_protocol_capability("conversation.content.read") is False


@pytest.mark.asyncio
async def test_binding_presence_and_space_binding_last4_boundaries_v1_v2(
    database: Database,
) -> None:
    person_id = await _add_person(database)
    space_id = await _add_space(database)
    await _add_binding(database, person_id=person_id, external_account_id="12345678")
    await _add_binding(
        database, person_id=person_id, external_account_id="123456789", display_name="long"
    )
    await _add_space_binding(database, space_id=space_id, external_space_id="87654321")
    await _add_space_binding(database, space_id=space_id, external_space_id="876543210")
    await _add_presence(database, external_account_id="80000000")
    await _add_presence(database, external_account_id="800000001")
    await _add_legacy_person(database, "12345670")
    await _add_legacy_person(database, "123456701")
    await _add_legacy_group(database, "20000000")
    await _add_legacy_group(database, "200000001")
    service = _service(database)
    binding_ctx = _context(_principal("identity.binding.read"))
    space_ctx = _context(_principal("identity.space.read"))
    presence_ctx = _context(_principal("identity.presence.read"))

    async def _assert_masked() -> None:
        bindings = await service.list_identity_bindings(binding_ctx, PageRequest(limit=50))
        spaces = await service.list_space_bindings(space_ctx, PageRequest(limit=50))
        presences = await service.list_presences(presence_ctx, PageRequest(limit=50))
        last4s = {item.external.last4 for item in bindings.items}
        assert None in last4s
        assert "6789" in last4s
        assert "5678" not in last4s
        space_last4s = {item.external.last4 for item in spaces.items}
        assert None in space_last4s
        assert "3210" in space_last4s
        assert "4321" not in space_last4s
        presence_last4s = {item.external.last4 for item in presences.items}
        assert None in presence_last4s
        assert "0001" in presence_last4s
        assert "0000" not in presence_last4s
        dumped = json.dumps(
            [
                {
                    "last4": item.external.last4,
                    "value": item.external.value,
                    "configured": item.external.configured,
                }
                for item in (*bindings.items, *spaces.items, *presences.items)
            ]
        )
        for leaked in ("12345678", "123456789", "87654321", "876543210", "80000000", "800000001"):
            assert leaked not in dumped

    await _assert_masked()
    await _set_v2(database)
    await _assert_masked()


@pytest.mark.asyncio
async def test_v1_unresolved_pages_hide_raw_ids_in_cursor_and_display(
    database: Database,
) -> None:
    people = ("223456789", "223456790", "223456791")
    groups = ("200000002", "200000003", "200000004")
    for index, user_id in enumerate(people):
        await _add_legacy_person(
            database,
            user_id,
            nickname=f"QQ {user_id}",
            first_seen_at=_NOW + timedelta(seconds=index),
        )
    for group_id in groups:
        await _add_legacy_group(database, group_id, name=f"群 {group_id}")
    service = _service(database)
    person_ctx = _context(_principal("identity.person.read"))
    binding_ctx = _context(_principal("identity.binding.read"))
    space_ctx = _context(_principal("identity.space.read"))
    person_page = await service.list_persons(person_ctx, PageRequest(limit=2))
    binding_page = await service.list_identity_bindings(binding_ctx, PageRequest(limit=2))
    space_page = await service.list_spaces(space_ctx, PageRequest(limit=2))
    space_binding_page = await service.list_space_bindings(space_ctx, PageRequest(limit=2))
    assert person_page.next_cursor is not None
    assert binding_page.next_cursor is not None
    assert space_page.next_cursor is not None
    assert space_binding_page.next_cursor is not None
    _assert_page_hides(person_page, *people)
    _assert_page_hides(binding_page, *people)
    _assert_page_hides(space_page, *groups)
    _assert_page_hides(space_binding_page, *groups)
    assert binding_page.next_cursor.value.startswith("c10v1|binding|u|")
    assert space_binding_page.next_cursor.value.startswith("c10v1|space_binding|u|")
    assert binding_page.next_cursor.value.split("|")[-1].isdigit()
    assert all(item.display_name == REDACTED_DISPLAY for item in binding_page.items)
    assert all(item.name == REDACTED_DISPLAY for item in space_page.items)
    assert all(item.display_name == REDACTED_DISPLAY for item in space_binding_page.items)
    revealed = await service.list_identity_bindings(
        _context(_principal("identity.binding.read", "identity.binding.read_external")),
        PageRequest(limit=2),
    )
    assert {item.external.value for item in revealed.items} <= set(people)
    assert {item.display_name for item in revealed.items} <= {f"QQ {item}" for item in people}
    dumped = _page_dump(revealed)
    assert any(token in dumped for token in people)


@pytest.mark.asyncio
async def test_canonical_display_metadata_redacts_exact_and_embedded_ids(
    database: Database,
) -> None:
    person_id = await _add_person(database)
    space_id = await _add_space(database, name="200000001")
    other_space = await _add_space(database, name="safe-room")
    await _add_binding(
        database,
        person_id=person_id,
        external_account_id="123456789",
        display_name="123456789",
    )
    await _add_binding(
        database,
        person_id=person_id,
        external_account_id="223456780",
        display_name="QQ 223456780",
    )
    await _add_binding(
        database,
        person_id=person_id,
        external_account_id="323456781",
        display_name="Ada",
    )
    await _add_space_binding(
        database,
        space_id=space_id,
        external_space_id="200000001",
        display_name="200000001",
    )
    await _add_space_binding(
        database,
        space_id=other_space,
        external_space_id="200000009",
        display_name="群 200000009",
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
        "123456789",
        "223456780",
        "323456781",
    )
    _assert_page_hides(spaces, "200000001", "200000009")
    _assert_page_hides(space_bindings, "200000001", "200000009")
    names = {item.display_name for item in bindings.items}
    assert REDACTED_DISPLAY in names
    assert "Ada" in names
    space_names = {item.name for item in spaces.items}
    assert REDACTED_DISPLAY in space_names
    assert "safe-room" in space_names
    revealed_bindings = await service.list_identity_bindings(reveal_ctx, PageRequest(limit=20))
    revealed_spaces = await service.list_spaces(reveal_ctx, PageRequest(limit=20))
    revealed_space_bindings = await service.list_space_bindings(reveal_ctx, PageRequest(limit=20))
    assert {item.display_name for item in revealed_bindings.items} == {
        "123456789",
        "QQ 223456780",
        "Ada",
    }
    assert {item.external.value for item in revealed_bindings.items} == {
        "123456789",
        "223456780",
        "323456781",
    }
    assert "200000001" in {item.name for item in revealed_spaces.items}
    assert {item.display_name for item in revealed_space_bindings.items} == {
        "200000001",
        "群 200000009",
    }


@pytest.mark.asyncio
async def test_legacy_unresolved_rowid_keyset_paginates_and_rejects_bad_integers(
    database: Database,
) -> None:
    stamps = {
        "223456789": _NOW + timedelta(seconds=1),
        "223456790": _NOW + timedelta(seconds=2),
        "223456791": _NOW + timedelta(seconds=3),
    }
    for user_id, seen in stamps.items():
        await _add_legacy_person(
            database,
            user_id,
            nickname=f"n-{user_id[-1]}",
            first_seen_at=seen,
        )
    await _add_legacy_group(database, "200000002", name="g-2")
    await _add_legacy_group(database, "200000003", name="g-3")
    await _add_legacy_group(database, "200000004", name="g-4")
    service = _service(database)
    person_ctx = _context(_principal("identity.person.read"))
    binding_ctx = _context(_principal("identity.binding.read"))
    space_ctx = _context(_principal("identity.space.read"))
    first_people = await service.list_persons(person_ctx, PageRequest(limit=2))
    first_bindings = await service.list_identity_bindings(binding_ctx, PageRequest(limit=2))
    first_spaces = await service.list_spaces(space_ctx, PageRequest(limit=2))
    assert first_people.next_cursor is not None
    assert first_bindings.next_cursor is not None
    assert first_spaces.next_cursor is not None
    _assert_page_hides(first_people, *stamps)
    _assert_page_hides(first_bindings, *stamps)
    _assert_page_hides(first_spaces, "200000002", "200000003", "200000004")
    seen_people = [item.created_at for item in first_people.items]
    seen_bindings = [item.display_name for item in first_bindings.items]
    seen_spaces = [item.name for item in first_spaces.items]
    async with database.sessions() as session, session.begin():
        leftover = await session.get(PersonModel, "223456791")
        assert leftover is not None
        await session.delete(leftover)
        leftover_group = await session.get(GroupModel, "200000004")
        assert leftover_group is not None
        await session.delete(leftover_group)
    await _add_legacy_person(
        database,
        "223456792",
        nickname="n-2",
        first_seen_at=_NOW + timedelta(seconds=4),
    )
    await _add_legacy_group(database, "200000005", name="g-5")
    second_people = await service.list_persons(
        person_ctx,
        PageRequest(limit=20, cursor=first_people.next_cursor),
    )
    second_bindings = await service.list_identity_bindings(
        binding_ctx,
        PageRequest(limit=20, cursor=first_bindings.next_cursor),
    )
    second_spaces = await service.list_spaces(
        space_ctx,
        PageRequest(limit=20, cursor=first_spaces.next_cursor),
    )
    _assert_page_hides(second_people, *stamps, "223456792")
    _assert_page_hides(second_bindings, *stamps, "223456792")
    _assert_page_hides(second_spaces, "200000002", "200000003", "200000004", "200000005")
    second_people_stamps = [item.created_at for item in second_people.items]
    second_binding_names = [item.display_name for item in second_bindings.items]
    second_space_names = [item.name for item in second_spaces.items]
    assert not set(seen_people) & set(second_people_stamps)
    assert not set(seen_bindings) & set(second_binding_names)
    assert not set(seen_spaces) & set(second_space_names)
    assert stamps["223456789"] in seen_people or stamps["223456790"] in seen_people
    assert (_NOW + timedelta(seconds=4)) in second_people_stamps
    assert "n-2" in second_binding_names
    assert "g-5" in second_space_names
    for method, caps, raw in (
        ("list_persons", ("identity.person.read",), "c10v1|person|u|not-int"),
        ("list_persons", ("identity.person.read",), "c10v1|person|u|-1"),
        ("list_persons", ("identity.person.read",), "c10v1|person|u|01"),
        ("list_identity_bindings", ("identity.binding.read",), "c10v1|binding|u|0"),
        ("list_identity_bindings", ("identity.binding.read",), "c10v1|binding|u|-1"),
        ("list_spaces", ("identity.space.read",), "c10v1|space|u|01"),
        ("list_space_bindings", ("identity.space.read",), "c10v1|space_binding|u|not-int"),
    ):
        if raw.endswith("|u|0"):
            page = await getattr(service, method)(
                _context(_principal(*caps)),
                PageRequest(limit=5, cursor=Cursor(raw)),
            )
            assert page.snapshot_at is not None
            continue
        with pytest.raises(ControlQueryError) as rejected:
            await getattr(service, method)(
                _context(_principal(*caps)),
                PageRequest(limit=5, cursor=Cursor(raw)),
            )
        assert rejected.value.problem.code is ProblemCode.VALIDATION_ERROR


def test_operation_cursor_key_keeps_full_local_id() -> None:
    stamp = datetime(2026, 1, 1, 0, 0, 0, 123456, tzinfo=UTC)
    key = encode_operation_cursor_key(stamp, 1, 101)
    assert decode_operation_cursor_key(key) == (stamp, 1, 101)
    assert encode_operation_cursor_key(stamp, 1, 1) != key


@pytest.mark.asyncio
async def test_operation_keyset_pages_250_same_created_at_without_loss(
    database: Database,
) -> None:
    stamp = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    async with database.sessions() as session, session.begin():
        for index in range(250):
            session.add(
                IdentityBackfillRunModel(
                    mode="dry_run",
                    status="running",
                    checkpoint=None,
                    processed_count=index,
                    persons_count=0,
                    identity_bindings_count=0,
                    spaces_count=0,
                    space_bindings_count=0,
                    presences_count=0,
                    conflicts_count=0,
                    skipped_count=0,
                    error_category=None,
                    started_at=stamp,
                    finished_at=None,
                    created_at=stamp,
                    updated_at=stamp,
                )
            )
    service = _service(database)
    context = _context(_principal("control.operation.read"))
    seen: list[str] = []
    cursor = None
    pages = 0
    while True:
        page = await service.list_backfill_operations(
            context,
            PageRequest(limit=50, cursor=cursor),
        )
        pages += 1
        seen.extend(item.operation.operation_id for item in page.items)
        if page.next_cursor is None:
            break
        cursor = page.next_cursor
    assert pages == 5
    assert len(seen) == 250
    assert len(set(seen)) == 250
