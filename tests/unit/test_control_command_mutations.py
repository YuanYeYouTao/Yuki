"""C11 identity/route commands: v1 cutover, v2 mutations, idempotency, and PII."""

from __future__ import annotations

import ast
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select, text

from qq_ai_bot.control_plane import (
    CommandOperation,
    ControlCommand,
    ControlCommandError,
    ControlCommandService,
    ControlPrincipal,
    ControlResult,
    DecisionContext,
    PrincipalSource,
    ProblemCode,
    RouteKind,
    RouteReferenceState,
    YukiControlTarget,
    bind_command_hash,
    is_protocol_capability,
)
from qq_ai_bot.control_plane.command_types import YUKI_TARGET_TOKEN
from qq_ai_bot.conversation.canonical_db_models import (
    CanonicalConversationModel,
    ControlCommandReceiptModel,
    ConversationLegacyAliasModel,
    PersonActiveRouteModel,
    SpaceActiveRouteModel,
    SpaceBindingIngestRouteModel,
)
from qq_ai_bot.conversation.rollup.db_models import ConversationScopeModel
from qq_ai_bot.domain.identity import (
    PersonId,
    PresenceId,
    PrincipalId,
    RequestId,
    SpaceBindingId,
    SpaceId,
)
from qq_ai_bot.identity.db_models import (
    CanonicalPersonModel,
    CanonicalSpaceModel,
    IdentityBindingModel,
    IdentityConflictModel,
    IdentityRuntimeStateModel,
    PresenceModel,
    SpaceBindingModel,
)
from qq_ai_bot.identity.inventory import IDENTITY_PLATFORM
from qq_ai_bot.persistence.control_command import ControlCommandAdapter
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import AdminOperationEventModel, GroupModel, PersonModel

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src"
CONTROL_PLANE_ROOT = SRC_ROOT / "qq_ai_bot" / "control_plane"
ADAPTER_PATH = SRC_ROOT / "qq_ai_bot" / "persistence" / "control_command.py"
_NOW = datetime(2026, 8, 24, 11, 0, tzinfo=UTC)
_WRITE_CAPS = (
    "identity.person.enable",
    "identity.person.disable",
    "identity.binding.attach",
    "identity.space.enable",
    "identity.space.disable",
    "identity.space.binding.attach",
    "identity.presence.register",
    "identity.presence.start",
    "identity.presence.stop",
    "identity.presence.set_ingest",
    "route.set",
    "route.pause",
    "route.resume",
)
_DOMAIN_TABLES = (
    "persons",
    "identity_bindings",
    "spaces",
    "space_bindings",
    "presences",
    "people",
    "groups",
    "person_active_routes",
    "space_binding_ingest_routes",
    "space_active_routes",
)
_SIGNATURE_TABLES = (
    "canonical_conversations",
    "conversation_legacy_aliases",
    "conversation_scopes",
    "conversation_rollups",
    "chat_events",
    "memory_facts",
)
_METHODS = (
    "enable_person",
    "disable_person",
    "attach_identity_binding",
    "enable_space",
    "disable_space",
    "attach_space_binding",
    "register_presence",
    "start_presence",
    "stop_presence",
    "set_presence_ingest",
    "set_route",
    "pause_route",
    "resume_route",
)
_METHOD_CAPABILITY = {
    "enable_person": "identity.person.enable",
    "disable_person": "identity.person.disable",
    "attach_identity_binding": "identity.binding.attach",
    "enable_space": "identity.space.enable",
    "disable_space": "identity.space.disable",
    "attach_space_binding": "identity.space.binding.attach",
    "register_presence": "identity.presence.register",
    "start_presence": "identity.presence.start",
    "stop_presence": "identity.presence.stop",
    "set_presence_ingest": "identity.presence.set_ingest",
    "set_route": "route.set",
    "pause_route": "route.pause",
    "resume_route": "route.resume",
}


def _principal(
    *capabilities: str, authenticated: bool = True, active: bool = True
) -> ControlPrincipal:
    return ControlPrincipal(
        principal_id=PrincipalId.new(),
        person_id=PersonId.new(),
        source=PrincipalSource.QQ,
        roles=("superuser",),
        granted_capabilities=capabilities,
        authenticated=authenticated,
        active=active,
    )


def _context(
    principal: ControlPrincipal,
    target: object,
    *,
    request_id: RequestId | None = None,
) -> DecisionContext[ControlPrincipal, PrincipalSource, object]:
    return DecisionContext(
        request_id=request_id or RequestId.new(),
        principal=principal,
        source=principal.source,
        canonical_target=target,
        reason="c11",
    )


def _command(
    request_id: RequestId,
    *,
    expected_revision: int = 1,
    payload: object | None = None,
) -> ControlCommand:
    return ControlCommand(
        request_id=request_id,
        expected_revision=expected_revision,
        payload={} if payload is None else payload,
    )


def _service(database: Database) -> ControlCommandService:
    return ControlCommandService(ControlCommandAdapter(database))


def _uuid() -> str:
    return str(uuid4())


def _python_files(root: Path) -> tuple[Path, ...]:
    return tuple(sorted(path for path in root.rglob("*.py") if path.is_file()))


async def _set_v2(database: Database) -> None:
    async with database.sessions() as session, session.begin():
        row = await session.get(IdentityRuntimeStateModel, 1)
        assert row is not None
        row.state = "v2"
        row.cutover_id = _uuid()
        row.source_fingerprint = "a" * 64
        row.completed_at = _NOW


async def _add_person(
    database: Database,
    *,
    person_id: str | None = None,
    enabled: bool = True,
    revision: int = 1,
) -> str:
    token = person_id or _uuid()
    async with database.sessions() as session, session.begin():
        session.add(
            CanonicalPersonModel(
                id=token,
                enabled=enabled,
                revision=revision,
                created_at=_NOW,
                updated_at=_NOW,
            )
        )
    return token


async def _add_space(
    database: Database,
    *,
    space_id: str | None = None,
    enabled: bool = True,
    revision: int = 1,
) -> str:
    token = space_id or _uuid()
    async with database.sessions() as session, session.begin():
        session.add(
            CanonicalSpaceModel(
                id=token,
                name="room",
                enabled=enabled,
                autonomous_enabled=True,
                require_mention=True,
                revision=revision,
                created_at=_NOW,
                updated_at=_NOW,
            )
        )
    return token


async def _add_binding(
    database: Database,
    *,
    person_id: str,
    external_account_id: str,
    binding_id: str | None = None,
    display_name: str = "Ada Lovelace",
    platform: str = IDENTITY_PLATFORM,
    status: str = "active",
) -> str:
    token = binding_id or _uuid()
    async with database.sessions() as session, session.begin():
        session.add(
            IdentityBindingModel(
                id=token,
                person_id=person_id,
                platform=platform,
                external_account_id=external_account_id,
                display_name=display_name,
                status=status,
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
    binding_id: str | None = None,
    platform: str = IDENTITY_PLATFORM,
    status: str = "active",
) -> str:
    token = binding_id or _uuid()
    async with database.sessions() as session, session.begin():
        session.add(
            SpaceBindingModel(
                id=token,
                space_id=space_id,
                platform=platform,
                external_space_id=external_space_id,
                display_name="Group Display",
                status=status,
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
    enabled: bool = True,
    ingest_eligible: bool = True,
    platform: str = IDENTITY_PLATFORM,
) -> str:
    token = presence_id or _uuid()
    async with database.sessions() as session, session.begin():
        session.add(
            PresenceModel(
                id=token,
                platform=platform,
                external_account_id=external_account_id,
                enabled=enabled,
                ingest_eligible=ingest_eligible,
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
    canonical_person_id: str | None = None,
) -> None:
    async with database.sessions() as session, session.begin():
        session.add(
            PersonModel(
                user_id=user_id,
                nickname="legacy-nick",
                enabled=True,
                is_bot=is_bot,
                first_seen_at=_NOW,
                last_seen_at=_NOW,
                canonical_person_id=canonical_person_id,
            )
        )


async def _add_legacy_group(
    database: Database,
    group_id: str,
    *,
    canonical_space_id: str | None = None,
) -> None:
    async with database.sessions() as session, session.begin():
        session.add(
            GroupModel(
                group_id=group_id,
                name="legacy-group",
                enabled=True,
                require_mention=True,
                autonomous_enabled=True,
                first_seen_at=_NOW,
                last_seen_at=_NOW,
                updated_at=_NOW,
                canonical_space_id=canonical_space_id,
            )
        )


async def _counts(database: Database) -> dict[str, int]:
    async with database.sessions() as session:
        values: dict[str, int] = {}
        for table in _DOMAIN_TABLES:
            values[table] = int(await session.scalar(text(f"SELECT COUNT(*) FROM {table}")) or 0)
        values["admin_operation_events"] = int(
            await session.scalar(text("SELECT COUNT(*) FROM admin_operation_events")) or 0
        )
        values["control_command_receipts"] = int(
            await session.scalar(text("SELECT COUNT(*) FROM control_command_receipts")) or 0
        )
        return values


async def _signature(database: Database) -> tuple[tuple[str, tuple[tuple[object, ...], ...]], ...]:
    async with database.sessions() as session:
        parts: list[tuple[str, tuple[tuple[object, ...], ...]]] = []
        for table in _SIGNATURE_TABLES:
            rows = (await session.execute(text(f"SELECT * FROM {table} ORDER BY rowid"))).all()
            parts.append((table, tuple(tuple(row) for row in rows)))
        return tuple(parts)


def _dump(value: object) -> str:
    return json.dumps(value, default=str, ensure_ascii=True)


def _assert_hidden(blob: str, *secrets: str) -> None:
    for item in secrets:
        assert item not in blob


async def _ledger_blob(database: Database) -> str:
    async with database.sessions() as session:
        audits = list(await session.scalars(select(AdminOperationEventModel)))
        receipts = list(await session.scalars(select(ControlCommandReceiptModel)))
    payload = []
    for row in audits:
        payload.append(
            {
                "actor": row.actor_user_id,
                "capability": row.capability,
                "operation": row.operation,
                "target_type": row.target_type,
                "target_id": row.target_id,
                "before": row.before_json,
                "after": row.after_json,
                "error": row.error_category,
            }
        )
    for row in receipts:
        payload.append(
            {
                "hash": row.payload_hash,
                "status": row.status,
                "resource": row.result_resource_id,
                "state": row.effective_state_json,
                "problem": row.problem_code,
            }
        )
    return _dump(payload)


def _invoke_payload(method: str, ids: dict[str, str]) -> object:
    if method in {
        "enable_person",
        "disable_person",
        "enable_space",
        "disable_space",
        "start_presence",
        "stop_presence",
    }:
        return {}
    if method == "attach_identity_binding":
        return {
            "platform": IDENTITY_PLATFORM,
            "external_account_id": "ext-account-alpha-9911",
            "display_name": "Ada Lovelace",
        }
    if method == "attach_space_binding":
        return {
            "platform": IDENTITY_PLATFORM,
            "external_space_id": "ext-space-beta-8822",
            "display_name": "Group Display",
        }
    if method == "register_presence":
        return {"platform": IDENTITY_PLATFORM, "external_account_id": "ext-yuki-gamma-7733"}
    if method == "set_presence_ingest":
        return {"ingest_eligible": False}
    if method == "set_route":
        return {
            "kind": RouteKind.PERSON_ACTIVE.value,
            "identity_binding_id": ids["binding"],
            "presence_id": ids["presence"],
        }
    return {"kind": RouteKind.PERSON_ACTIVE.value}


def _invoke_target(method: str, ids: dict[str, str]) -> object:
    if method in {"enable_person", "disable_person", "attach_identity_binding", "set_route"}:
        return PersonId.parse(ids["person"])
    if method in {"pause_route", "resume_route"}:
        return PersonId.parse(ids["person"])
    if method in {"enable_space", "disable_space", "attach_space_binding"}:
        return SpaceId.parse(ids["space"])
    if method == "register_presence":
        return YukiControlTarget.PERMANENT_YUKI
    return PresenceId.parse(ids["presence"])


def _wrong_target(method: str, ids: dict[str, str]) -> object:
    if method in {"enable_space", "disable_space", "attach_space_binding"}:
        return PersonId.parse(ids["person"])
    return SpaceId.parse(ids["space"])


async def _add_person_route(
    database: Database,
    *,
    person_id: str,
    binding_id: str,
    presence_id: str,
    paused: bool = False,
) -> None:
    async with database.sessions() as session, session.begin():
        session.add(
            PersonActiveRouteModel(
                person_id=person_id,
                identity_binding_id=binding_id,
                presence_id=presence_id,
                route_generation=1,
                paused=paused,
                revision=1,
                created_at=_NOW,
                updated_at=_NOW,
            )
        )


async def _add_ingest_route(
    database: Database,
    *,
    binding_id: str,
    presence_id: str,
    paused: bool = False,
) -> None:
    async with database.sessions() as session, session.begin():
        session.add(
            SpaceBindingIngestRouteModel(
                space_binding_id=binding_id,
                ingest_presence_id=presence_id,
                route_generation=1,
                paused=paused,
                revision=1,
                created_at=_NOW,
                updated_at=_NOW,
            )
        )


async def _add_space_route(
    database: Database,
    *,
    space_id: str,
    binding_id: str,
    presence_id: str,
    paused: bool = False,
) -> None:
    async with database.sessions() as session, session.begin():
        session.add(
            SpaceActiveRouteModel(
                space_id=space_id,
                space_binding_id=binding_id,
                presence_id=presence_id,
                route_generation=1,
                paused=paused,
                revision=1,
                created_at=_NOW,
                updated_at=_NOW,
            )
        )


async def _bypass_runtime_checks(database: Database) -> None:
    async with database.sessions() as session, session.begin():
        await session.execute(text("PRAGMA foreign_keys=OFF"))
        await session.execute(
            text("ALTER TABLE identity_runtime_state RENAME TO identity_runtime_state_guarded")
        )
        await session.execute(
            text(
                "CREATE TABLE identity_runtime_state ("
                "id INTEGER PRIMARY KEY, "
                "state VARCHAR(8) NOT NULL, "
                "cutover_id VARCHAR(36), "
                "source_fingerprint VARCHAR(64), "
                "completed_at DATETIME, "
                "revision INTEGER NOT NULL, "
                "created_at DATETIME, "
                "updated_at DATETIME)"
            )
        )
        await session.execute(
            text(
                "INSERT INTO identity_runtime_state "
                "SELECT id, state, cutover_id, source_fingerprint, completed_at, "
                "revision, created_at, updated_at FROM identity_runtime_state_guarded"
            )
        )
        await session.execute(text("DROP TABLE identity_runtime_state_guarded"))


async def _person_revision(database: Database, person_id: str) -> int:
    async with database.sessions() as session:
        row = await session.get(CanonicalPersonModel, person_id)
        assert row is not None
        return int(row.revision)


def test_bound_hash_includes_operation_target_and_revision() -> None:
    payload = {"platform": IDENTITY_PLATFORM, "external_account_id": "1001"}
    first = bind_command_hash(
        operation=CommandOperation.BINDING_ATTACH.value,
        target_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        expected_revision=1,
        payload=payload,
    )
    other_target = bind_command_hash(
        operation=CommandOperation.BINDING_ATTACH.value,
        target_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        expected_revision=1,
        payload=payload,
    )
    other_op = bind_command_hash(
        operation=CommandOperation.PERSON_ENABLE.value,
        target_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        expected_revision=1,
        payload=payload,
    )
    other_rev = bind_command_hash(
        operation=CommandOperation.BINDING_ATTACH.value,
        target_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        expected_revision=2,
        payload=payload,
    )
    assert len({first, other_target, other_op, other_rev}) == 4
    assert len(first) == 64


def test_no_c11_command_is_legacy_equivalent() -> None:
    assert set(_METHODS) == set(_METHOD_CAPABILITY)
    assert is_protocol_capability("web_search") is True
    assert is_protocol_capability("mcp.web_search") is True


@pytest.mark.asyncio
async def test_v1_rejects_every_command_without_fake_legacy_rows(database: Database) -> None:
    person_id = await _add_person(database)
    space_id = await _add_space(database)
    binding_id = await _add_binding(database, person_id=person_id, external_account_id="seed-1")
    presence_id = await _add_presence(database, external_account_id="seed-yuki")
    ids = {
        "person": person_id,
        "space": space_id,
        "binding": binding_id,
        "presence": presence_id,
    }
    before = await _counts(database)
    service = _service(database)
    principal = _principal(*_WRITE_CAPS)
    for method in _METHODS:
        context = _context(principal, _invoke_target(method, ids))
        command = _command(
            context.request_id, expected_revision=0, payload=_invoke_payload(method, ids)
        )
        with pytest.raises(ControlCommandError) as rejected:
            await getattr(service, method)(context, command)
        assert rejected.value.problem.code is ProblemCode.PENDING_CUTOVER
    after = await _counts(database)
    for table in _DOMAIN_TABLES:
        assert after[table] == before[table]
    assert after["admin_operation_events"] == before["admin_operation_events"] + len(_METHODS)
    assert after["control_command_receipts"] == before["control_command_receipts"] + len(_METHODS)
    async with database.sessions() as session:
        statuses = list(await session.scalars(select(ControlCommandReceiptModel.status)))
        problems = list(await session.scalars(select(ControlCommandReceiptModel.problem_code)))
    assert set(statuses) == {"failed"}
    assert set(problems) == {"pending_cutover"}


@pytest.mark.asyncio
@pytest.mark.parametrize("method", _METHODS)
async def test_each_capability_denial_writes_nothing(database: Database, method: str) -> None:
    person_id = await _add_person(database)
    space_id = await _add_space(database)
    binding_id = await _add_binding(database, person_id=person_id, external_account_id="seed-2")
    presence_id = await _add_presence(database, external_account_id="seed-yuki-2")
    ids = {
        "person": person_id,
        "space": space_id,
        "binding": binding_id,
        "presence": presence_id,
    }
    granted = tuple(item for item in _WRITE_CAPS if item != _METHOD_CAPABILITY[method])
    before = await _counts(database)
    context = _context(_principal(*granted), _invoke_target(method, ids))
    command = _command(context.request_id, payload=_invoke_payload(method, ids))
    with pytest.raises(ControlCommandError) as rejected:
        await getattr(_service(database), method)(context, command)
    assert rejected.value.problem.code is ProblemCode.CAPABILITY_DENIED
    assert await _counts(database) == before


@pytest.mark.asyncio
async def test_inactive_and_unauthenticated_write_nothing(database: Database) -> None:
    person_id = await _add_person(database)
    before = await _counts(database)
    service = _service(database)
    inactive = _context(_principal(*_WRITE_CAPS, active=False), PersonId.parse(person_id))
    with pytest.raises(ControlCommandError) as rejected:
        await service.enable_person(inactive, _command(inactive.request_id))
    assert rejected.value.problem.code is ProblemCode.PRECONDITION_FAILED
    unauthenticated = _context(
        _principal(*_WRITE_CAPS, authenticated=False), PersonId.parse(person_id)
    )
    with pytest.raises(ControlCommandError) as denied:
        await service.enable_person(unauthenticated, _command(unauthenticated.request_id))
    assert denied.value.problem.code is ProblemCode.UNAUTHENTICATED
    assert await _counts(database) == before


@pytest.mark.asyncio
async def test_v2_happy_paths_and_safe_effective_state(database: Database) -> None:
    await _set_v2(database)
    person_id = await _add_person(database)
    space_id = await _add_space(database)
    service = _service(database)
    principal = _principal(*_WRITE_CAPS)
    secrets = (
        "ext-account-alpha-9911",
        "ext-space-beta-8822",
        "ext-yuki-gamma-7733",
        "Ada Lovelace",
        "Group Display",
    )

    disable = _context(principal, PersonId.parse(person_id))
    disabled = await service.disable_person(
        disable, _command(disable.request_id, expected_revision=1)
    )
    assert disabled.success is True
    assert disabled.resource_id == person_id
    assert disabled.revision == 2
    assert disabled.effective_state == {"enabled": False, "revision": 2}

    enable = _context(principal, PersonId.parse(person_id))
    enabled = await service.enable_person(enable, _command(enable.request_id, expected_revision=2))
    assert enabled.effective_state == {"enabled": True, "revision": 3}
    noop = _context(principal, PersonId.parse(person_id))
    same = await service.enable_person(noop, _command(noop.request_id, expected_revision=3))
    assert same.revision == 3

    attach = _context(principal, PersonId.parse(person_id))
    binding = await service.attach_identity_binding(
        attach,
        _command(
            attach.request_id,
            expected_revision=3,
            payload={
                "platform": IDENTITY_PLATFORM.upper(),
                "external_account_id": "ext-account-alpha-9911",
                "display_name": "Ada Lovelace",
            },
        ),
    )
    assert binding.revision == 1
    assert binding.effective_state["person_id"] == person_id
    assert "external_account_id" not in binding.effective_state
    assert binding.effective_state["platform"] == IDENTITY_PLATFORM

    space_off = _context(principal, SpaceId.parse(space_id))
    await service.disable_space(space_off, _command(space_off.request_id, expected_revision=1))
    space_on = _context(principal, SpaceId.parse(space_id))
    space_enabled = await service.enable_space(
        space_on, _command(space_on.request_id, expected_revision=2)
    )
    assert space_enabled.effective_state == {"enabled": True, "revision": 3}
    space_attach = _context(principal, SpaceId.parse(space_id))
    space_binding = await service.attach_space_binding(
        space_attach,
        _command(
            space_attach.request_id,
            expected_revision=3,
            payload={
                "platform": IDENTITY_PLATFORM,
                "external_space_id": "ext-space-beta-8822",
                "display_name": "Group Display",
            },
        ),
    )
    assert space_binding.effective_state["space_id"] == space_id

    register = _context(principal, YukiControlTarget.PERMANENT_YUKI)
    presence = await service.register_presence(
        register,
        _command(
            register.request_id,
            expected_revision=0,
            payload={"platform": IDENTITY_PLATFORM, "external_account_id": "ext-yuki-gamma-7733"},
        ),
    )
    assert presence.revision == 1
    assert presence.effective_state["enabled"] is True
    assert presence.effective_state["ingest_eligible"] is True
    presence_id = PresenceId.parse(presence.resource_id)

    stop = _context(principal, presence_id)
    stopped = await service.stop_presence(stop, _command(stop.request_id, expected_revision=1))
    assert stopped.revision == 2
    assert stopped.effective_state["enabled"] is False
    stop_again = _context(principal, presence_id)
    stopped_noop = await service.stop_presence(
        stop_again, _command(stop_again.request_id, expected_revision=2)
    )
    assert stopped_noop.revision == 2
    start = _context(principal, presence_id)
    started = await service.start_presence(start, _command(start.request_id, expected_revision=2))
    assert started.revision == 3
    ingest = _context(principal, presence_id)
    ingest_off = await service.set_presence_ingest(
        ingest,
        _command(ingest.request_id, expected_revision=3, payload={"ingest_eligible": False}),
    )
    assert ingest_off.revision == 4
    ingest_again = _context(principal, presence_id)
    ingest_noop = await service.set_presence_ingest(
        ingest_again,
        _command(ingest_again.request_id, expected_revision=4, payload={"ingest_eligible": False}),
    )
    assert ingest_noop.revision == 4
    ingest_on = _context(principal, presence_id)
    await service.set_presence_ingest(
        ingest_on,
        _command(ingest_on.request_id, expected_revision=4, payload={"ingest_eligible": True}),
    )

    person_route = _context(principal, PersonId.parse(person_id))
    created = await service.set_route(
        person_route,
        _command(
            person_route.request_id,
            expected_revision=0,
            payload={
                "kind": RouteKind.PERSON_ACTIVE.value,
                "identity_binding_id": binding.resource_id,
                "presence_id": presence.resource_id,
            },
        ),
    )
    assert created.revision == 1
    assert created.effective_state["route_generation"] == 1
    assert created.effective_state["paused"] is False
    assert created.effective_state["reference_state"] == RouteReferenceState.CONSISTENT.value
    same_set = _context(principal, PersonId.parse(person_id))
    unchanged = await service.set_route(
        same_set,
        _command(
            same_set.request_id,
            expected_revision=1,
            payload={
                "kind": RouteKind.PERSON_ACTIVE.value,
                "identity_binding_id": binding.resource_id,
                "presence_id": presence.resource_id,
            },
        ),
    )
    assert unchanged.revision == 1
    assert unchanged.effective_state["route_generation"] == 1
    pause = _context(principal, PersonId.parse(person_id))
    paused = await service.pause_route(
        pause,
        _command(
            pause.request_id,
            expected_revision=1,
            payload={"kind": RouteKind.PERSON_ACTIVE.value},
        ),
    )
    assert paused.revision == 2
    assert paused.effective_state["paused"] is True
    assert paused.effective_state["route_generation"] == 2
    pause_again = _context(principal, PersonId.parse(person_id))
    paused_noop = await service.pause_route(
        pause_again,
        _command(
            pause_again.request_id,
            expected_revision=2,
            payload={"kind": RouteKind.PERSON_ACTIVE.value},
        ),
    )
    assert paused_noop.revision == 2
    resume = _context(principal, PersonId.parse(person_id))
    resumed = await service.resume_route(
        resume,
        _command(
            resume.request_id,
            expected_revision=2,
            payload={"kind": RouteKind.PERSON_ACTIVE.value},
        ),
    )
    assert resumed.revision == 3
    assert resumed.effective_state["paused"] is False
    assert resumed.effective_state["route_generation"] == 3

    ingest_route = _context(principal, SpaceBindingId.parse(space_binding.resource_id))
    ingest_created = await service.set_route(
        ingest_route,
        _command(
            ingest_route.request_id,
            expected_revision=0,
            payload={
                "kind": RouteKind.SPACE_BINDING_INGEST.value,
                "ingest_presence_id": presence.resource_id,
            },
        ),
    )
    assert ingest_created.revision == 1
    space_route = _context(principal, SpaceId.parse(space_id))
    space_created = await service.set_route(
        space_route,
        _command(
            space_route.request_id,
            expected_revision=0,
            payload={
                "kind": RouteKind.SPACE_ACTIVE.value,
                "space_binding_id": space_binding.resource_id,
                "presence_id": presence.resource_id,
            },
        ),
    )
    assert space_created.revision == 1

    async with database.sessions() as session:
        person = await session.get(CanonicalPersonModel, person_id)
        space = await session.get(CanonicalSpaceModel, space_id)
        assert person is not None and person.revision == 4
        assert space is not None
        assert space.revision == 4
        assert space.autonomous_enabled is True
        assert space.require_mention is True
        people_count = int(await session.scalar(text("SELECT COUNT(*) FROM people")) or 0)
        groups_count = int(await session.scalar(text("SELECT COUNT(*) FROM groups")) or 0)
        assert people_count == 0
        assert groups_count == 0
        audit = await session.get(AdminOperationEventModel, int(binding.audit_id))
        receipt = await session.scalar(
            select(ControlCommandReceiptModel).where(
                ControlCommandReceiptModel.audit_id == int(binding.audit_id)
            )
        )
        assert audit is not None and receipt is not None
        assert receipt.audit_id == audit.id
        assert audit.actor_user_id == principal.principal_id.text
        blob = await _ledger_blob(database)
        blob += _dump(
            {
                "binding": dict(binding.effective_state),
                "presence": dict(presence.effective_state),
                "binding_id": binding.resource_id,
                "presence_id": presence.resource_id,
                "audit": binding.audit_id,
            }
        )
        _assert_hidden(blob, *secrets)


@pytest.mark.asyncio
async def test_strict_payloads_and_revision_rules(database: Database) -> None:
    await _set_v2(database)
    person_id = await _add_person(database)
    service = _service(database)
    principal = _principal(*_WRITE_CAPS)
    unknown = _context(principal, PersonId.parse(person_id))
    with pytest.raises(ControlCommandError) as rejected:
        await service.enable_person(unknown, _command(unknown.request_id, payload={"extra": True}))
    assert rejected.value.problem.code is ProblemCode.VALIDATION_ERROR
    missing = _context(principal, PersonId.parse(person_id))
    with pytest.raises(ControlCommandError) as missing_err:
        await service.attach_identity_binding(
            missing, _command(missing.request_id, payload={"platform": IDENTITY_PLATFORM})
        )
    assert missing_err.value.problem.code is ProblemCode.VALIDATION_ERROR
    confused = _context(principal, PersonId.parse(person_id))
    with pytest.raises(ControlCommandError) as confused_err:
        await service.attach_identity_binding(
            confused,
            _command(
                confused.request_id,
                payload={"platform": IDENTITY_PLATFORM, "external_account_id": 9911},
            ),
        )
    assert confused_err.value.problem.code is ProblemCode.VALIDATION_ERROR
    padded = _context(principal, PersonId.parse(person_id))
    with pytest.raises(ControlCommandError) as padded_err:
        await service.attach_identity_binding(
            padded,
            _command(
                padded.request_id,
                payload={"platform": IDENTITY_PLATFORM, "external_account_id": " 9911 "},
            ),
        )
    assert padded_err.value.problem.code is ProblemCode.VALIDATION_ERROR
    version = _context(principal, PersonId.parse(person_id))
    with pytest.raises(ControlCommandError) as version_err:
        await service.enable_person(version, _command(version.request_id, expected_revision=9))
    assert version_err.value.problem.code is ProblemCode.VERSION_CONFLICT
    missing_person = _context(principal, PersonId.new())
    with pytest.raises(ControlCommandError) as missing_person_err:
        await service.enable_person(
            missing_person, _command(missing_person.request_id, expected_revision=1)
        )
    assert missing_person_err.value.problem.code is ProblemCode.NOT_FOUND
    wrong_target = _context(principal, SpaceId.new())
    with pytest.raises(ControlCommandError) as wrong_target_err:
        await service.enable_person(wrong_target, _command(wrong_target.request_id))
    assert wrong_target_err.value.problem.code is ProblemCode.VALIDATION_ERROR
    create_rev = _context(principal, YukiControlTarget.PERMANENT_YUKI)
    with pytest.raises(ControlCommandError) as create_err:
        await service.register_presence(
            create_rev,
            _command(
                create_rev.request_id,
                expected_revision=1,
                payload={"platform": IDENTITY_PLATFORM, "external_account_id": "ext-new-yuki"},
            ),
        )
    assert create_err.value.problem.code is ProblemCode.VERSION_CONFLICT
    async with database.sessions() as session:
        failed = list(await session.scalars(select(ControlCommandReceiptModel)))
        assert failed
        assert all(row.status == "failed" for row in failed)


@pytest.mark.asyncio
async def test_same_and_different_request_replay(database: Database) -> None:
    await _set_v2(database)
    person_id = await _add_person(database, enabled=False)
    service = _service(database)
    principal = _principal(*_WRITE_CAPS)
    context = _context(principal, PersonId.parse(person_id))
    command = _command(context.request_id, expected_revision=1)
    first = await service.enable_person(context, command)
    second = await service.enable_person(context, command)
    assert first.audit_id == second.audit_id
    assert first.revision == second.revision == 2
    before = await _counts(database)
    again = await service.enable_person(context, command)
    assert again.audit_id == first.audit_id
    assert await _counts(database) == before
    conflict = _command(context.request_id, expected_revision=2)
    with pytest.raises(ControlCommandError) as rejected:
        await service.enable_person(context, conflict)
    assert rejected.value.problem.code is ProblemCode.IDEMPOTENCY_CONFLICT
    assert await _counts(database) == before
    other = _context(principal, PersonId.parse(person_id), request_id=context.request_id)
    with pytest.raises(ControlCommandError) as cross:
        await service.disable_person(other, _command(context.request_id, expected_revision=2))
    assert cross.value.problem.code is ProblemCode.IDEMPOTENCY_CONFLICT
    assert await _counts(database) == before


@pytest.mark.asyncio
async def test_recorded_failure_replay_and_corrupt_receipt(database: Database) -> None:
    await _set_v2(database)
    service = _service(database)
    principal = _principal(*_WRITE_CAPS)
    missing = PersonId.new()
    context = _context(principal, missing)
    command = _command(context.request_id, expected_revision=1)
    with pytest.raises(ControlCommandError) as first:
        await service.enable_person(context, command)
    assert first.value.problem.code is ProblemCode.NOT_FOUND
    before = await _counts(database)
    with pytest.raises(ControlCommandError) as replayed:
        await service.enable_person(context, command)
    assert replayed.value.problem.code is ProblemCode.NOT_FOUND
    assert await _counts(database) == before
    async with database.sessions() as session, session.begin():
        row = await session.scalar(select(ControlCommandReceiptModel))
        assert row is not None
        row.problem_code = "not_a_protocol_code"
    with pytest.raises(ControlCommandError) as corrupt:
        await service.enable_person(context, command)
    assert corrupt.value.problem.code is ProblemCode.STATE_MISMATCH
    assert await _counts(database) == before


@pytest.mark.asyncio
async def test_failpoint_rolls_back_audit_receipt_and_domain(database: Database) -> None:
    await _set_v2(database)
    person_id = await _add_person(database, enabled=False)
    adapter = ControlCommandAdapter(database)

    def _boom() -> None:
        raise RuntimeError("audit failpoint")

    adapter._after_audit_flush = _boom
    service = ControlCommandService(adapter)
    before = await _counts(database)
    context = _context(_principal(*_WRITE_CAPS), PersonId.parse(person_id))
    with pytest.raises(RuntimeError, match="audit failpoint"):
        await service.enable_person(context, _command(context.request_id, expected_revision=1))
    assert await _counts(database) == before
    async with database.sessions() as session:
        person = await session.get(CanonicalPersonModel, person_id)
        assert person is not None
        assert person.enabled is False
        assert person.revision == 1


@pytest.mark.asyncio
async def test_two_database_owners_race_same_and_different_payload(database: Database) -> None:
    await _set_v2(database)
    person_id = await _add_person(database, enabled=False)
    principal = _principal(*_WRITE_CAPS)
    context = _context(principal, PersonId.parse(person_id))
    command = _command(context.request_id, expected_revision=1)
    other = Database(database.url)
    try:
        first = ControlCommandService(ControlCommandAdapter(database))
        second = ControlCommandService(ControlCommandAdapter(other))
        results = await asyncio.gather(
            first.enable_person(context, command),
            second.enable_person(context, command),
        )
        assert results[0].audit_id == results[1].audit_id
        assert results[0].revision == results[1].revision == 2
        counts = await _counts(database)
        assert counts["admin_operation_events"] == 1
        assert counts["control_command_receipts"] == 1
        owner = await _add_person(database)
        raced = _context(principal, PersonId.parse(owner))
        left = _command(
            raced.request_id,
            expected_revision=1,
            payload={
                "platform": IDENTITY_PLATFORM,
                "external_account_id": "race-left-account",
            },
        )
        right = _command(
            raced.request_id,
            expected_revision=1,
            payload={
                "platform": IDENTITY_PLATFORM,
                "external_account_id": "race-right-account",
            },
        )
        outcomes = await asyncio.gather(
            first.attach_identity_binding(raced, left),
            second.attach_identity_binding(raced, right),
            return_exceptions=True,
        )
        successes = [item for item in outcomes if not isinstance(item, BaseException)]
        failures = [item for item in outcomes if isinstance(item, ControlCommandError)]
        assert len(successes) == 1
        assert len(failures) == 1
        assert failures[0].problem.code is ProblemCode.IDEMPOTENCY_CONFLICT
        async with database.sessions() as session:
            attached = len(
                list(
                    await session.scalars(
                        select(IdentityBindingModel).where(IdentityBindingModel.person_id == owner)
                    )
                )
            )
        assert attached == 1
    finally:
        await other.close()
    async with database.sessions() as session:
        person = await session.get(CanonicalPersonModel, person_id)
        assert person is not None
        assert person.enabled is True
        assert person.revision == 2


@pytest.mark.asyncio
async def test_binding_conflicts_populated_merge_and_yuki(database: Database) -> None:
    await _set_v2(database)
    owner = await _add_person(database)
    other = await _add_person(database)
    await _add_binding(database, person_id=other, external_account_id="bound-elsewhere")
    await _add_presence(database, external_account_id="yuki-self")
    await _add_legacy_person(database, "ignored-bot-77", is_bot=True)
    await _add_legacy_person(database, "unresolved-human")
    await _add_legacy_person(database, "owned-other", canonical_person_id=other)
    await _add_legacy_person(database, "owned-self", canonical_person_id=owner)
    async with database.sessions() as session, session.begin():
        session.add(
            IdentityConflictModel(
                platform=IDENTITY_PLATFORM,
                external_id="conflicted-account",
                subject_kind="account",
                conflict_kind="ambiguous_identity",
                status="open",
                error_category="canonical_kind_mismatch",
                created_at=_NOW,
                updated_at=_NOW,
            )
        )
    service = _service(database)
    principal = _principal(*_WRITE_CAPS)

    async def _attach(external_id: str) -> ProblemCode:
        context = _context(principal, PersonId.parse(owner))
        with pytest.raises(ControlCommandError) as rejected:
            await service.attach_identity_binding(
                context,
                _command(
                    context.request_id,
                    expected_revision=1,
                    payload={"platform": IDENTITY_PLATFORM, "external_account_id": external_id},
                ),
            )
        return rejected.value.problem.code

    assert await _attach("bound-elsewhere") is ProblemCode.BINDING_AMBIGUOUS
    assert await _attach("yuki-self") is ProblemCode.PRECONDITION_FAILED
    assert await _attach("ignored-bot-77") is ProblemCode.PRECONDITION_FAILED
    assert await _attach("unresolved-human") is ProblemCode.BINDING_AMBIGUOUS
    assert await _attach("owned-other") is ProblemCode.POPULATED_MERGE_FORBIDDEN
    assert await _attach("owned-self") is ProblemCode.PRECONDITION_FAILED
    assert await _attach("conflicted-account") is ProblemCode.BINDING_AMBIGUOUS

    async def _register(external_id: str) -> ProblemCode:
        context = _context(principal, YukiControlTarget.PERMANENT_YUKI)
        with pytest.raises(ControlCommandError) as rejected:
            await service.register_presence(
                context,
                _command(
                    context.request_id,
                    expected_revision=0,
                    payload={"platform": IDENTITY_PLATFORM, "external_account_id": external_id},
                ),
            )
        return rejected.value.problem.code

    assert await _register("bound-elsewhere") is ProblemCode.PRECONDITION_FAILED
    assert await _register("yuki-self") is ProblemCode.PRECONDITION_FAILED
    assert await _register("ignored-bot-77") is ProblemCode.PRECONDITION_FAILED
    async with database.sessions() as session:
        bindings = int(await session.scalar(text("SELECT COUNT(*) FROM identity_bindings")) or 0)
        people = int(await session.scalar(text("SELECT COUNT(*) FROM people")) or 0)
    assert bindings == 1
    assert people == 4


@pytest.mark.asyncio
async def test_space_attach_rejects_populated_and_unresolved(database: Database) -> None:
    await _set_v2(database)
    space = await _add_space(database)
    other = await _add_space(database)
    await _add_space_binding(database, space_id=other, external_space_id="space-taken")
    await _add_legacy_group(database, "unresolved-space")
    await _add_legacy_group(database, "owned-space", canonical_space_id=other)
    service = _service(database)
    principal = _principal(*_WRITE_CAPS)

    async def _attach(external_id: str) -> ProblemCode:
        context = _context(principal, SpaceId.parse(space))
        with pytest.raises(ControlCommandError) as rejected:
            await service.attach_space_binding(
                context,
                _command(
                    context.request_id,
                    expected_revision=1,
                    payload={"platform": IDENTITY_PLATFORM, "external_space_id": external_id},
                ),
            )
        return rejected.value.problem.code

    assert await _attach("space-taken") is ProblemCode.BINDING_AMBIGUOUS
    assert await _attach("unresolved-space") is ProblemCode.BINDING_AMBIGUOUS
    assert await _attach("owned-space") is ProblemCode.POPULATED_MERGE_FORBIDDEN


@pytest.mark.asyncio
async def test_route_owner_platform_and_conversation_bytes_unchanged(database: Database) -> None:
    await _set_v2(database)
    person_id = await _add_person(database)
    space_id = await _add_space(database)
    binding_id = await _add_binding(
        database, person_id=person_id, external_account_id="route-human"
    )
    other_person = await _add_person(database)
    other_binding = await _add_binding(
        database, person_id=other_person, external_account_id="route-other"
    )
    presence_id = await _add_presence(database, external_account_id="route-yuki")
    foreign_presence = await _add_presence(database, external_account_id="route-tg", platform="tg")
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
                generation=4,
                starts_after_event_id=0,
                last_event_id=11,
                last_generation_change_event_id=3,
                covered_through_event_id=10,
                uncovered_event_count=1,
                uncovered_character_count=8,
                revision=2,
                created_at=_NOW,
                updated_at=_NOW,
            )
        )
        session.add(
            ConversationLegacyAliasModel(
                id=alias_id,
                conversation_id=conversation_id,
                scope_key="private:8000:1001",
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
                generation=4,
                starts_after_event_id=0,
                last_event_id=11,
                last_generation_change_event_id=3,
                uncovered_event_count=1,
                uncovered_character_count=8,
                created_at=_NOW,
                updated_at=_NOW,
                canonical_conversation_id=conversation_id,
            )
        )
    before = await _signature(database)
    service = _service(database)
    principal = _principal(*_WRITE_CAPS)
    owner_mismatch = _context(principal, PersonId.parse(person_id))
    with pytest.raises(ControlCommandError) as mismatch:
        await service.set_route(
            owner_mismatch,
            _command(
                owner_mismatch.request_id,
                expected_revision=0,
                payload={
                    "kind": RouteKind.PERSON_ACTIVE.value,
                    "identity_binding_id": other_binding,
                    "presence_id": presence_id,
                },
            ),
        )
    assert mismatch.value.problem.code is ProblemCode.ROUTE_AMBIGUOUS
    platform = _context(principal, PersonId.parse(person_id))
    with pytest.raises(ControlCommandError) as platform_err:
        await service.set_route(
            platform,
            _command(
                platform.request_id,
                expected_revision=0,
                payload={
                    "kind": RouteKind.PERSON_ACTIVE.value,
                    "identity_binding_id": binding_id,
                    "presence_id": foreign_presence,
                },
            ),
        )
    assert platform_err.value.problem.code is ProblemCode.ROUTE_AMBIGUOUS
    created = _context(principal, PersonId.parse(person_id))
    route = await service.set_route(
        created,
        _command(
            created.request_id,
            expected_revision=0,
            payload={
                "kind": RouteKind.PERSON_ACTIVE.value,
                "identity_binding_id": binding_id,
                "presence_id": presence_id,
                "paused": False,
            },
        ),
    )
    assert route.effective_state["route_generation"] == 1
    pause = _context(principal, PersonId.parse(person_id))
    paused = await service.pause_route(
        pause,
        _command(
            pause.request_id, expected_revision=1, payload={"kind": RouteKind.PERSON_ACTIVE.value}
        ),
    )
    assert paused.effective_state["route_generation"] == 2
    resume = _context(principal, PersonId.parse(person_id))
    resumed = await service.resume_route(
        resume,
        _command(
            resume.request_id, expected_revision=2, payload={"kind": RouteKind.PERSON_ACTIVE.value}
        ),
    )
    assert resumed.effective_state["route_generation"] == 3
    space_binding = await _add_space_binding(
        database, space_id=space_id, external_space_id="route-space"
    )
    ingest = _context(principal, SpaceBindingId.parse(space_binding))
    await service.set_route(
        ingest,
        _command(
            ingest.request_id,
            expected_revision=0,
            payload={
                "kind": RouteKind.SPACE_BINDING_INGEST.value,
                "ingest_presence_id": presence_id,
            },
        ),
    )
    space_route = _context(principal, SpaceId.parse(space_id))
    await service.set_route(
        space_route,
        _command(
            space_route.request_id,
            expected_revision=0,
            payload={
                "kind": RouteKind.SPACE_ACTIVE.value,
                "space_binding_id": space_binding,
                "presence_id": presence_id,
            },
        ),
    )
    assert await _signature(database) == before
    async with database.sessions() as session:
        conversation = await session.get(CanonicalConversationModel, conversation_id)
        assert conversation is not None
        assert conversation.generation == 4
        assert conversation.revision == 2
        assert conversation.last_event_id == 11
        person_route = await session.get(PersonActiveRouteModel, person_id)
        ingest_row = await session.get(SpaceBindingIngestRouteModel, space_binding)
        space_row = await session.get(SpaceActiveRouteModel, space_id)
        assert person_route is not None
        assert ingest_row is not None
        assert space_row is not None
        assert person_route.revision == 3
        assert person_route.route_generation == 3


@pytest.mark.asyncio
async def test_missing_runtime_state_fails_closed_without_writes(database: Database) -> None:
    person_id = await _add_person(database)
    async with database.sessions() as session, session.begin():
        row = await session.get(IdentityRuntimeStateModel, 1)
        assert row is not None
        await session.delete(row)
    before = await _counts(database)
    context = _context(_principal(*_WRITE_CAPS), PersonId.parse(person_id))
    with pytest.raises(ControlCommandError) as rejected:
        await _service(database).enable_person(context, _command(context.request_id))
    assert rejected.value.problem.code is ProblemCode.STATE_MISMATCH
    after = await _counts(database)
    assert after["admin_operation_events"] == before["admin_operation_events"]
    assert after["control_command_receipts"] == before["control_command_receipts"]
    assert after["persons"] == before["persons"]


@pytest.mark.asyncio
async def test_presence_create_is_server_assigned_and_idempotent(database: Database) -> None:
    await _set_v2(database)
    service = _service(database)
    principal = _principal(*_WRITE_CAPS)
    context = _context(principal, YukiControlTarget.PERMANENT_YUKI)
    command = _command(
        context.request_id,
        expected_revision=0,
        payload={"platform": IDENTITY_PLATFORM, "external_account_id": "server-uuid-yuki"},
    )
    first = await service.register_presence(context, command)
    second = await service.register_presence(context, command)
    assert first.resource_id == second.resource_id
    assert first.audit_id == second.audit_id
    PresenceId.parse(first.resource_id)
    assert first.resource_id != context.request_id.text
    async with database.sessions() as session:
        count = int(await session.scalar(text("SELECT COUNT(*) FROM presences")) or 0)
        assert count == 1


@pytest.mark.asyncio
async def test_route_set_create_conflict_and_disabled_precondition(database: Database) -> None:
    await _set_v2(database)
    person_id = await _add_person(database, enabled=False)
    binding_id = await _add_binding(
        database, person_id=person_id, external_account_id="disabled-owner"
    )
    presence_id = await _add_presence(database, external_account_id="disabled-yuki")
    service = _service(database)
    principal = _principal(*_WRITE_CAPS)
    context = _context(principal, PersonId.parse(person_id))
    with pytest.raises(ControlCommandError) as rejected:
        await service.set_route(
            context,
            _command(
                context.request_id,
                expected_revision=0,
                payload={
                    "kind": RouteKind.PERSON_ACTIVE.value,
                    "identity_binding_id": binding_id,
                    "presence_id": presence_id,
                },
            ),
        )
    assert rejected.value.problem.code is ProblemCode.PRECONDITION_FAILED
    await _set_enabled(database, person_id)
    created = _context(principal, PersonId.parse(person_id))
    await service.set_route(
        created,
        _command(
            created.request_id,
            expected_revision=0,
            payload={
                "kind": RouteKind.PERSON_ACTIVE.value,
                "identity_binding_id": binding_id,
                "presence_id": presence_id,
            },
        ),
    )
    again = _context(principal, PersonId.parse(person_id))
    with pytest.raises(ControlCommandError) as version:
        await service.set_route(
            again,
            _command(
                again.request_id,
                expected_revision=0,
                payload={
                    "kind": RouteKind.PERSON_ACTIVE.value,
                    "identity_binding_id": binding_id,
                    "presence_id": presence_id,
                },
            ),
        )
    assert version.value.problem.code is ProblemCode.VERSION_CONFLICT


async def _set_enabled(database: Database, person_id: str) -> None:
    async with database.sessions() as session, session.begin():
        row = await session.get(CanonicalPersonModel, person_id)
        assert row is not None
        row.enabled = True


def _problem_blob(error: ControlCommandError) -> str:
    return _dump(
        {
            "code": error.problem.code.value,
            "details": dict(error.problem.details),
            "message": str(error),
            "args": error.args,
        }
    )


@pytest.mark.asyncio
async def test_corrupted_success_receipt_does_not_echo_secret(database: Database) -> None:
    await _set_v2(database)
    person_id = await _add_person(database, enabled=False)
    service = _service(database)
    principal = _principal(*_WRITE_CAPS)
    context = _context(principal, PersonId.parse(person_id))
    command = _command(context.request_id, expected_revision=1)
    first = await service.enable_person(context, command)
    assert first.effective_state == {"enabled": True, "revision": 2}
    replayed = await service.enable_person(context, command)
    assert replayed.audit_id == first.audit_id
    assert replayed.effective_state == first.effective_state
    secret = "sk-replay-leak"
    async with database.sessions() as session, session.begin():
        row = await session.scalar(
            select(ControlCommandReceiptModel).where(
                ControlCommandReceiptModel.request_id == command.request_id.text
            )
        )
        assert row is not None
        row.effective_state_json = json.dumps({"api_key": secret})
    before = await _counts(database)
    with pytest.raises(ControlCommandError) as rejected:
        await service.enable_person(context, command)
    assert rejected.value.problem.code is ProblemCode.STATE_MISMATCH
    _assert_hidden(_problem_blob(rejected.value), secret, "api_key")
    assert await _counts(database) == before
    conflict = _command(context.request_id, expected_revision=2)
    with pytest.raises(ControlCommandError) as leaked:
        await service.enable_person(context, conflict)
    assert leaked.value.problem.code is ProblemCode.IDEMPOTENCY_CONFLICT
    _assert_hidden(_problem_blob(leaked.value), secret, "api_key")
    assert await _counts(database) == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state",
    (
        {"enabled": True},
        {"enabled": True, "revision": 2, "extra": True},
        {"enabled": 1, "revision": 2},
        {"enabled": True, "revision": 0},
        {"password": "hidden", "revision": 2},
    ),
)
async def test_corrupt_effective_state_shapes_are_state_mismatch(
    database: Database, state: dict[str, object]
) -> None:
    await _set_v2(database)
    person_id = await _add_person(database, enabled=False)
    service = _service(database)
    context = _context(_principal(*_WRITE_CAPS), PersonId.parse(person_id))
    command = _command(context.request_id, expected_revision=1)
    await service.enable_person(context, command)
    async with database.sessions() as session, session.begin():
        row = await session.scalar(select(ControlCommandReceiptModel))
        assert row is not None
        row.effective_state_json = json.dumps(state)
    before = await _counts(database)
    with pytest.raises(ControlCommandError) as rejected:
        await service.enable_person(context, command)
    assert rejected.value.problem.code is ProblemCode.STATE_MISMATCH
    assert await _counts(database) == before


@pytest.mark.asyncio
async def test_broken_audit_chain_is_state_mismatch(database: Database) -> None:
    await _set_v2(database)
    service = _service(database)
    principal = _principal(*_WRITE_CAPS)
    first_target = PersonId.new()
    second_target = PersonId.new()
    first_ctx = _context(principal, first_target)
    second_ctx = _context(principal, second_target)
    first_cmd = _command(first_ctx.request_id, expected_revision=1)
    second_cmd = _command(second_ctx.request_id, expected_revision=1)
    with pytest.raises(ControlCommandError) as first:
        await service.enable_person(first_ctx, first_cmd)
    with pytest.raises(ControlCommandError) as second:
        await service.enable_person(second_ctx, second_cmd)
    assert first.value.problem.code is ProblemCode.NOT_FOUND
    assert second.value.problem.code is ProblemCode.NOT_FOUND

    async def _receipt(request_id: RequestId) -> ControlCommandReceiptModel:
        async with database.sessions() as session:
            row = await session.scalar(
                select(ControlCommandReceiptModel).where(
                    ControlCommandReceiptModel.request_id == request_id.text
                )
            )
            assert row is not None
            session.expunge(row)
            return row

    first_receipt = await _receipt(first_cmd.request_id)
    second_receipt = await _receipt(second_cmd.request_id)
    assert first_receipt.audit_id is not None
    assert second_receipt.audit_id is not None

    async with database.sessions() as session, session.begin():
        row = await session.get(ControlCommandReceiptModel, first_receipt.id)
        assert row is not None
        row.audit_id = None
    before = await _counts(database)
    with pytest.raises(ControlCommandError) as null_audit:
        await service.enable_person(first_ctx, first_cmd)
    assert null_audit.value.problem.code is ProblemCode.STATE_MISMATCH
    assert await _counts(database) == before

    async with database.sessions() as session, session.begin():
        row = await session.get(ControlCommandReceiptModel, first_receipt.id)
        assert row is not None
        row.audit_id = first_receipt.audit_id
        await session.execute(text("PRAGMA foreign_keys=OFF"))
        await session.execute(
            text("DELETE FROM admin_operation_events WHERE id = :audit_id"),
            {"audit_id": first_receipt.audit_id},
        )
    with pytest.raises(ControlCommandError) as missing_audit:
        await service.enable_person(first_ctx, first_cmd)
    assert missing_audit.value.problem.code is ProblemCode.STATE_MISMATCH

    async with database.sessions() as session, session.begin():
        left = await session.get(ControlCommandReceiptModel, second_receipt.id)
        right = await session.scalar(
            select(ControlCommandReceiptModel).where(
                ControlCommandReceiptModel.request_id == first_cmd.request_id.text
            )
        )
        assert left is not None and right is not None
        left.audit_id, right.audit_id = right.audit_id, left.audit_id
    with pytest.raises(ControlCommandError) as swapped:
        await service.enable_person(second_ctx, second_cmd)
    assert swapped.value.problem.code is ProblemCode.STATE_MISMATCH


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    (
        ("actor_user_id", str(uuid4())),
        ("capability", "route.set"),
        ("operation", "identity.person.disable"),
        ("success", True),
        ("before_json", json.dumps({"api_key": "sk-audit-leak"})),
        ("after_json", json.dumps({"api_key": "sk-audit-leak"})),
    ),
)
async def test_corrupted_failure_audit_fields_are_state_mismatch(
    database: Database, field: str, value: object
) -> None:
    await _set_v2(database)
    service = _service(database)
    context = _context(_principal(*_WRITE_CAPS), PersonId.new())
    command = _command(context.request_id, expected_revision=1)
    with pytest.raises(ControlCommandError) as first:
        await service.enable_person(context, command)
    assert first.value.problem.code is ProblemCode.NOT_FOUND
    secret = "sk-audit-leak"
    async with database.sessions() as session, session.begin():
        receipt = await session.scalar(select(ControlCommandReceiptModel))
        assert receipt is not None and receipt.audit_id is not None
        audit = await session.get(AdminOperationEventModel, receipt.audit_id)
        assert audit is not None
        setattr(audit, field, value)
    before = await _counts(database)
    with pytest.raises(ControlCommandError) as rejected:
        await service.enable_person(context, command)
    assert rejected.value.problem.code is ProblemCode.STATE_MISMATCH
    _assert_hidden(_problem_blob(rejected.value), secret)
    assert await _counts(database) == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "sql",
    (
        "UPDATE identity_runtime_state SET state = 'v2'",
        "UPDATE identity_runtime_state SET cutover_id = :cutover",
        "UPDATE identity_runtime_state SET source_fingerprint = 'fp', completed_at = :stamp",
        "UPDATE identity_runtime_state SET revision = 0",
        "UPDATE identity_runtime_state SET updated_at = :past, created_at = :stamp",
        "INSERT INTO identity_runtime_state "
        "(id, state, revision, created_at, updated_at) VALUES (2, 'v1', 1, :stamp, :stamp)",
        "UPDATE identity_runtime_state SET state = 'v2', cutover_id = :cutover, "
        "source_fingerprint = '', completed_at = :stamp",
        "UPDATE identity_runtime_state SET state = 'v2', cutover_id = 'not-a-uuid4', "
        "source_fingerprint = 'fingerprint-token', completed_at = :stamp",
    ),
)
async def test_malformed_runtime_epoch_writes_nothing(database: Database, sql: str) -> None:
    await _bypass_runtime_checks(database)
    if "source_fingerprint = ''" in sql or "not-a-uuid4" in sql:
        await _set_v2(database)
        await _bypass_runtime_checks(database)
    person_id = await _add_person(database, enabled=False)
    async with database.sessions() as session, session.begin():
        await session.execute(
            text(sql),
            {
                "cutover": str(uuid4()),
                "stamp": _NOW,
                "past": datetime(2020, 1, 1, tzinfo=UTC),
            },
        )
    before = await _counts(database)
    revision = await _person_revision(database, person_id)
    context = _context(_principal(*_WRITE_CAPS), PersonId.parse(person_id))
    with pytest.raises(ControlCommandError) as rejected:
        await _service(database).enable_person(context, _command(context.request_id))
    assert rejected.value.problem.code is ProblemCode.STATE_MISMATCH
    after = await _counts(database)
    assert after == before
    assert await _person_revision(database, person_id) == revision


@pytest.mark.asyncio
async def test_cross_platform_ids_ignore_qq_legacy_rows(database: Database) -> None:
    await _set_v2(database)
    await _add_legacy_person(database, "same-id", is_bot=True)
    await _add_legacy_person(database, "presence-same")
    await _add_legacy_group(database, "same-id")
    person_id = await _add_person(database)
    space_id = await _add_space(database)
    service = _service(database)
    principal = _principal(*_WRITE_CAPS)
    attach = _context(principal, PersonId.parse(person_id))
    binding = await service.attach_identity_binding(
        attach,
        _command(
            attach.request_id,
            expected_revision=1,
            payload={"platform": "telegram", "external_account_id": "same-id"},
        ),
    )
    assert binding.effective_state["platform"] == "telegram"
    space_attach = _context(principal, SpaceId.parse(space_id))
    space_binding = await service.attach_space_binding(
        space_attach,
        _command(
            space_attach.request_id,
            expected_revision=1,
            payload={"platform": "telegram", "external_space_id": "same-id"},
        ),
    )
    assert space_binding.effective_state["platform"] == "telegram"
    register = _context(principal, YukiControlTarget.PERMANENT_YUKI)
    presence = await service.register_presence(
        register,
        _command(
            register.request_id,
            expected_revision=0,
            payload={"platform": "telegram", "external_account_id": "presence-same"},
        ),
    )
    assert presence.effective_state["platform"] == "telegram"
    qq_person = await _add_person(database)
    qq_space = await _add_space(database)
    with pytest.raises(ControlCommandError) as qq_account:
        context = _context(principal, PersonId.parse(qq_person))
        await service.attach_identity_binding(
            context,
            _command(
                context.request_id,
                expected_revision=1,
                payload={"platform": IDENTITY_PLATFORM, "external_account_id": "same-id"},
            ),
        )
    assert qq_account.value.problem.code is ProblemCode.PRECONDITION_FAILED
    with pytest.raises(ControlCommandError) as qq_space_err:
        context = _context(principal, SpaceId.parse(qq_space))
        await service.attach_space_binding(
            context,
            _command(
                context.request_id,
                expected_revision=1,
                payload={"platform": IDENTITY_PLATFORM, "external_space_id": "same-id"},
            ),
        )
    assert qq_space_err.value.problem.code is ProblemCode.BINDING_AMBIGUOUS
    with pytest.raises(ControlCommandError) as qq_presence:
        context = _context(principal, YukiControlTarget.PERMANENT_YUKI)
        await service.register_presence(
            context,
            _command(
                context.request_id,
                expected_revision=0,
                payload={"platform": IDENTITY_PLATFORM, "external_account_id": "same-id"},
            ),
        )
    assert qq_presence.value.problem.code is ProblemCode.PRECONDITION_FAILED
    with pytest.raises(ControlCommandError) as telegram_collision:
        context = _context(principal, PersonId.parse(qq_person))
        await service.attach_identity_binding(
            context,
            _command(
                context.request_id,
                expected_revision=1,
                payload={"platform": "telegram", "external_account_id": "same-id"},
            ),
        )
    assert telegram_collision.value.problem.code is ProblemCode.BINDING_AMBIGUOUS


@pytest.mark.asyncio
async def test_pause_remains_available_when_dependencies_are_unhealthy(
    database: Database,
) -> None:
    await _set_v2(database)
    person_id = await _add_person(database)
    space_id = await _add_space(database)
    binding_id = await _add_binding(
        database, person_id=person_id, external_account_id="pause-human"
    )
    space_binding = await _add_space_binding(
        database, space_id=space_id, external_space_id="pause-space"
    )
    presence_id = await _add_presence(database, external_account_id="pause-yuki")
    await _add_person_route(
        database, person_id=person_id, binding_id=binding_id, presence_id=presence_id
    )
    await _add_ingest_route(database, binding_id=space_binding, presence_id=presence_id)
    await _add_space_route(
        database, space_id=space_id, binding_id=space_binding, presence_id=presence_id
    )
    service = _service(database)
    principal = _principal(*_WRITE_CAPS)

    async def _unhealthy() -> None:
        async with database.sessions() as session, session.begin():
            person = await session.get(CanonicalPersonModel, person_id)
            space = await session.get(CanonicalSpaceModel, space_id)
            binding = await session.get(IdentityBindingModel, binding_id)
            space_row = await session.get(SpaceBindingModel, space_binding)
            presence = await session.get(PresenceModel, presence_id)
            assert person is not None and space is not None
            assert binding is not None and space_row is not None and presence is not None
            person.enabled = False
            space.enabled = False
            binding.status = "disabled"
            space_row.status = "disabled"
            presence.enabled = False
            presence.ingest_eligible = False

    await _unhealthy()
    person_pause = _context(principal, PersonId.parse(person_id))
    paused = await service.pause_route(
        person_pause,
        _command(
            person_pause.request_id,
            expected_revision=1,
            payload={"kind": RouteKind.PERSON_ACTIVE.value},
        ),
    )
    assert paused.effective_state["paused"] is True
    assert paused.effective_state["route_generation"] == 2
    pause_again = _context(principal, PersonId.parse(person_id))
    noop = await service.pause_route(
        pause_again,
        _command(
            pause_again.request_id,
            expected_revision=2,
            payload={"kind": RouteKind.PERSON_ACTIVE.value},
        ),
    )
    assert noop.revision == 2
    assert noop.effective_state["route_generation"] == 2
    with pytest.raises(ControlCommandError) as person_resume:
        resume = _context(principal, PersonId.parse(person_id))
        await service.resume_route(
            resume,
            _command(
                resume.request_id,
                expected_revision=2,
                payload={"kind": RouteKind.PERSON_ACTIVE.value},
            ),
        )
    assert person_resume.value.problem.code is ProblemCode.PRECONDITION_FAILED
    ingest_pause = _context(principal, SpaceBindingId.parse(space_binding))
    ingest = await service.pause_route(
        ingest_pause,
        _command(
            ingest_pause.request_id,
            expected_revision=1,
            payload={"kind": RouteKind.SPACE_BINDING_INGEST.value},
        ),
    )
    assert ingest.effective_state["paused"] is True
    with pytest.raises(ControlCommandError) as ingest_resume:
        resume = _context(principal, SpaceBindingId.parse(space_binding))
        await service.resume_route(
            resume,
            _command(
                resume.request_id,
                expected_revision=2,
                payload={"kind": RouteKind.SPACE_BINDING_INGEST.value},
            ),
        )
    assert ingest_resume.value.problem.code is ProblemCode.PRECONDITION_FAILED
    space_pause = _context(principal, SpaceId.parse(space_id))
    space = await service.pause_route(
        space_pause,
        _command(
            space_pause.request_id,
            expected_revision=1,
            payload={"kind": RouteKind.SPACE_ACTIVE.value},
        ),
    )
    assert space.effective_state["paused"] is True
    with pytest.raises(ControlCommandError) as space_resume:
        resume = _context(principal, SpaceId.parse(space_id))
        await service.resume_route(
            resume,
            _command(
                resume.request_id,
                expected_revision=2,
                payload={"kind": RouteKind.SPACE_ACTIVE.value},
            ),
        )
    assert space_resume.value.problem.code is ProblemCode.PRECONDITION_FAILED


@pytest.mark.asyncio
@pytest.mark.parametrize("method", _METHODS)
async def test_v1_malformed_payload_is_cached_validation_error(
    database: Database, method: str
) -> None:
    person_id = await _add_person(database)
    space_id = await _add_space(database)
    binding_id = await _add_binding(database, person_id=person_id, external_account_id="v1-pay")
    presence_id = await _add_presence(database, external_account_id="v1-yuki-pay")
    ids = {
        "person": person_id,
        "space": space_id,
        "binding": binding_id,
        "presence": presence_id,
    }
    before = await _counts(database)
    service = _service(database)
    context = _context(_principal(*_WRITE_CAPS), _invoke_target(method, ids))
    command = _command(context.request_id, expected_revision=0, payload={"unknown": True})
    with pytest.raises(ControlCommandError) as rejected:
        await getattr(service, method)(context, command)
    assert rejected.value.problem.code is ProblemCode.VALIDATION_ERROR
    with pytest.raises(ControlCommandError) as replayed:
        await getattr(service, method)(context, command)
    assert replayed.value.problem.code is ProblemCode.VALIDATION_ERROR
    after = await _counts(database)
    for table in _DOMAIN_TABLES:
        assert after[table] == before[table]
    assert after["control_command_receipts"] == before["control_command_receipts"] + 1
    assert after["admin_operation_events"] == before["admin_operation_events"] + 1


@pytest.mark.asyncio
@pytest.mark.parametrize("method", _METHODS)
async def test_v1_wrong_target_is_cached_validation_error(database: Database, method: str) -> None:
    person_id = await _add_person(database)
    space_id = await _add_space(database)
    binding_id = await _add_binding(database, person_id=person_id, external_account_id="v1-tgt")
    presence_id = await _add_presence(database, external_account_id="v1-yuki-tgt")
    ids = {
        "person": person_id,
        "space": space_id,
        "binding": binding_id,
        "presence": presence_id,
    }
    before = await _counts(database)
    service = _service(database)
    context = _context(_principal(*_WRITE_CAPS), _wrong_target(method, ids))
    command = _command(
        context.request_id, expected_revision=0, payload=_invoke_payload(method, ids)
    )
    with pytest.raises(ControlCommandError) as rejected:
        await getattr(service, method)(context, command)
    assert rejected.value.problem.code is ProblemCode.VALIDATION_ERROR
    after = await _counts(database)
    for table in _DOMAIN_TABLES:
        assert after[table] == before[table]
    assert after["control_command_receipts"] == before["control_command_receipts"] + 1


@pytest.mark.asyncio
async def test_register_presence_binds_yuki_token_not_person(
    database: Database,
) -> None:
    await _set_v2(database)
    person_id = await _add_person(database)
    service = _service(database)
    principal = _principal(*_WRITE_CAPS)
    context = _context(principal, PersonId.parse(person_id))
    command = _command(
        context.request_id,
        expected_revision=0,
        payload={"platform": IDENTITY_PLATFORM, "external_account_id": "yuki-bind-token"},
    )
    with pytest.raises(ControlCommandError) as rejected:
        await service.register_presence(context, command)
    assert rejected.value.problem.code is ProblemCode.VALIDATION_ERROR
    async with database.sessions() as session:
        receipt = await session.scalar(select(ControlCommandReceiptModel))
        audit = await session.scalar(select(AdminOperationEventModel))
    assert receipt is not None and audit is not None
    expected = bind_command_hash(
        operation=CommandOperation.PRESENCE_REGISTER.value,
        target_id=YUKI_TARGET_TOKEN,
        expected_revision=0,
        payload={"external_account_id": "yuki-bind-token", "platform": IDENTITY_PLATFORM},
    )
    assert receipt.payload_hash == expected
    assert audit.target_type == "yuki"
    assert audit.target_id == YUKI_TARGET_TOKEN
    assert person_id not in (audit.target_id, receipt.payload_hash)


@pytest.mark.asyncio
async def test_route_create_nonzero_revision_is_version_conflict(database: Database) -> None:
    await _set_v2(database)
    person_id = await _add_person(database)
    space_id = await _add_space(database)
    binding_id = await _add_binding(database, person_id=person_id, external_account_id="rev-human")
    space_binding = await _add_space_binding(
        database, space_id=space_id, external_space_id="rev-space"
    )
    presence_id = await _add_presence(database, external_account_id="rev-yuki")
    service = _service(database)
    principal = _principal(*_WRITE_CAPS)
    person_ctx = _context(principal, PersonId.parse(person_id))
    with pytest.raises(ControlCommandError) as person_err:
        await service.set_route(
            person_ctx,
            _command(
                person_ctx.request_id,
                expected_revision=1,
                payload={
                    "kind": RouteKind.PERSON_ACTIVE.value,
                    "identity_binding_id": binding_id,
                    "presence_id": presence_id,
                },
            ),
        )
    assert person_err.value.problem.code is ProblemCode.VERSION_CONFLICT
    ingest_ctx = _context(principal, SpaceBindingId.parse(space_binding))
    with pytest.raises(ControlCommandError) as ingest_err:
        await service.set_route(
            ingest_ctx,
            _command(
                ingest_ctx.request_id,
                expected_revision=1,
                payload={
                    "kind": RouteKind.SPACE_BINDING_INGEST.value,
                    "ingest_presence_id": presence_id,
                },
            ),
        )
    assert ingest_err.value.problem.code is ProblemCode.VERSION_CONFLICT
    space_ctx = _context(principal, SpaceId.parse(space_id))
    with pytest.raises(ControlCommandError) as space_err:
        await service.set_route(
            space_ctx,
            _command(
                space_ctx.request_id,
                expected_revision=1,
                payload={
                    "kind": RouteKind.SPACE_ACTIVE.value,
                    "space_binding_id": space_binding,
                    "presence_id": presence_id,
                },
            ),
        )
    assert space_err.value.problem.code is ProblemCode.VERSION_CONFLICT
    async with database.sessions() as session:
        assert await session.get(PersonActiveRouteModel, person_id) is None
        assert await session.get(SpaceBindingIngestRouteModel, space_binding) is None
        assert await session.get(SpaceActiveRouteModel, space_id) is None


def _result_blob(result: object) -> str:
    if type(result) is not ControlResult:
        raise AssertionError("result must be ControlResult")
    return json.dumps(
        {
            "success": result.success,
            "resource_id": result.resource_id,
            "revision": result.revision,
            "audit_id": result.audit_id,
            "effective_state": dict(result.effective_state),
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )


async def _bypass_receipt_checks(database: Database) -> None:
    async with database.sessions() as session, session.begin():
        await session.execute(text("PRAGMA foreign_keys=OFF"))
        await session.execute(
            text("ALTER TABLE control_command_receipts RENAME TO control_command_receipts_guarded")
        )
        await session.execute(
            text(
                "CREATE TABLE control_command_receipts ("
                "id INTEGER PRIMARY KEY, "
                "principal_id VARCHAR(36) NOT NULL, "
                "request_id VARCHAR(36) NOT NULL, "
                "payload_hash VARCHAR(64) NOT NULL, "
                "status VARCHAR(16) NOT NULL, "
                "result_resource_id VARCHAR(255), "
                "effective_state_json VARCHAR(4096), "
                "result_revision INTEGER, "
                "problem_code VARCHAR(64), "
                "audit_id INTEGER, "
                "operation_kind VARCHAR(64), "
                "operation_ref VARCHAR(128), "
                "created_at DATETIME NOT NULL, "
                "updated_at DATETIME NOT NULL)"
            )
        )
        await session.execute(
            text(
                "INSERT INTO control_command_receipts SELECT "
                "id, principal_id, request_id, payload_hash, status, "
                "result_resource_id, effective_state_json, result_revision, "
                "problem_code, audit_id, operation_kind, operation_ref, "
                "created_at, updated_at FROM control_command_receipts_guarded"
            )
        )
        await session.execute(text("DROP TABLE control_command_receipts_guarded"))


@pytest.mark.asyncio
async def test_safe_shaped_false_enable_state_is_rejected(database: Database) -> None:
    await _set_v2(database)
    person_id = await _add_person(database, enabled=False)
    service = _service(database)
    context = _context(_principal(*_WRITE_CAPS), PersonId.parse(person_id))
    command = _command(context.request_id, expected_revision=1)
    first = await service.enable_person(context, command)
    assert first.effective_state == {"enabled": True, "revision": 2}
    async with database.sessions() as session, session.begin():
        row = await session.scalar(select(ControlCommandReceiptModel))
        assert row is not None
        row.effective_state_json = json.dumps({"enabled": False, "revision": 2})
    before = await _counts(database)
    with pytest.raises(ControlCommandError) as rejected:
        await service.enable_person(context, command)
    assert rejected.value.problem.code is ProblemCode.STATE_MISMATCH
    assert await _counts(database) == before


@pytest.mark.asyncio
async def test_same_target_same_operation_audit_swap_is_rejected(database: Database) -> None:
    await _set_v2(database)
    person_id = await _add_person(database, enabled=False)
    service = _service(database)
    principal = _principal(*_WRITE_CAPS)
    first_ctx = _context(principal, PersonId.parse(person_id))
    first = await service.enable_person(
        first_ctx, _command(first_ctx.request_id, expected_revision=1)
    )
    second_ctx = _context(principal, PersonId.parse(person_id))
    second = await service.enable_person(
        second_ctx, _command(second_ctx.request_id, expected_revision=2)
    )
    assert first.revision == 2
    assert second.revision == 2
    async with database.sessions() as session:
        audits = list(await session.scalars(select(AdminOperationEventModel)))
    assert {row.trigger_message_id for row in audits} == {
        first_ctx.request_id.text,
        second_ctx.request_id.text,
    }
    assert all(row.conversation_key == "" for row in audits)
    async with database.sessions() as session, session.begin():
        rows = list(await session.scalars(select(ControlCommandReceiptModel)))
        assert len(rows) == 2
        rows[0].audit_id, rows[1].audit_id = rows[1].audit_id, rows[0].audit_id
    with pytest.raises(ControlCommandError) as first_err:
        await service.enable_person(first_ctx, _command(first_ctx.request_id, expected_revision=1))
    with pytest.raises(ControlCommandError) as second_err:
        await service.enable_person(
            second_ctx, _command(second_ctx.request_id, expected_revision=2)
        )
    assert first_err.value.problem.code is ProblemCode.STATE_MISMATCH
    assert second_err.value.problem.code is ProblemCode.STATE_MISMATCH


@pytest.mark.asyncio
async def test_nested_unsafe_audit_json_is_rejected(database: Database) -> None:
    await _set_v2(database)
    person_id = await _add_person(database, enabled=False)
    service = _service(database)
    context = _context(_principal(*_WRITE_CAPS), PersonId.parse(person_id))
    command = _command(context.request_id, expected_revision=1)
    await service.enable_person(context, command)
    secret = "sk-nested"
    async with database.sessions() as session, session.begin():
        receipt = await session.scalar(select(ControlCommandReceiptModel))
        assert receipt is not None and receipt.audit_id is not None
        audit = await session.get(AdminOperationEventModel, receipt.audit_id)
        assert audit is not None
        audit.before_json = json.dumps({"enabled": {"api_key": secret}, "revision": 1})
    before = await _counts(database)
    with pytest.raises(ControlCommandError) as rejected:
        await service.enable_person(context, command)
    assert rejected.value.problem.code is ProblemCode.STATE_MISMATCH
    _assert_hidden(_problem_blob(rejected.value), secret, "api_key")
    assert await _counts(database) == before


@pytest.mark.asyncio
async def test_semantic_mutations_of_safe_typed_state_are_rejected(database: Database) -> None:
    await _set_v2(database)
    person_id = await _add_person(database)
    space_id = await _add_space(database)
    service = _service(database)
    principal = _principal(*_WRITE_CAPS)
    disable = _context(principal, PersonId.parse(person_id))
    await service.disable_person(disable, _command(disable.request_id, expected_revision=1))
    attach = _context(principal, PersonId.parse(person_id))
    binding = await service.attach_identity_binding(
        attach,
        _command(
            attach.request_id,
            expected_revision=2,
            payload={"platform": IDENTITY_PLATFORM, "external_account_id": "semantic-human"},
        ),
    )
    register = _context(principal, YukiControlTarget.PERMANENT_YUKI)
    presence = await service.register_presence(
        register,
        _command(
            register.request_id,
            expected_revision=0,
            payload={"platform": IDENTITY_PLATFORM, "external_account_id": "semantic-yuki"},
        ),
    )
    stop = _context(principal, PresenceId.parse(presence.resource_id))
    stop_cmd = _command(stop.request_id, expected_revision=1)
    await service.stop_presence(stop, stop_cmd)
    ingest = _context(principal, PresenceId.parse(presence.resource_id))
    ingest_cmd = _command(
        ingest.request_id, expected_revision=2, payload={"ingest_eligible": False}
    )
    await service.set_presence_ingest(ingest, ingest_cmd)
    start = _context(principal, PresenceId.parse(presence.resource_id))
    await service.start_presence(start, _command(start.request_id, expected_revision=3))
    enable = _context(principal, PersonId.parse(person_id))
    await service.enable_person(enable, _command(enable.request_id, expected_revision=3))
    route = _context(principal, PersonId.parse(person_id))
    route_cmd = _command(
        route.request_id,
        expected_revision=0,
        payload={
            "kind": RouteKind.PERSON_ACTIVE.value,
            "identity_binding_id": binding.resource_id,
            "presence_id": presence.resource_id,
        },
    )
    created = await service.set_route(route, route_cmd)
    other = PersonId.new().text

    async def _mutate_receipt(request_id: RequestId, state: dict[str, object]) -> None:
        async with database.sessions() as session, session.begin():
            row = await session.scalar(
                select(ControlCommandReceiptModel).where(
                    ControlCommandReceiptModel.request_id == request_id.text
                )
            )
            assert row is not None
            row.effective_state_json = json.dumps(state)
            if row.audit_id is not None:
                audit = await session.get(AdminOperationEventModel, row.audit_id)
                assert audit is not None
                after = json.loads(audit.after_json)
                after.update({key: value for key, value in state.items() if key in after})
                audit.after_json = json.dumps(after)

    await _mutate_receipt(disable.request_id, {"enabled": True, "revision": 2})
    with pytest.raises(ControlCommandError) as disabled:
        await service.disable_person(disable, _command(disable.request_id, expected_revision=1))
    assert disabled.value.problem.code is ProblemCode.STATE_MISMATCH
    await _mutate_receipt(
        attach.request_id,
        {
            "binding_id": binding.resource_id,
            "person_id": other,
            "platform": "telegram",
            "status": "active",
            "revision": 1,
        },
    )
    with pytest.raises(ControlCommandError) as attached:
        await service.attach_identity_binding(
            attach,
            _command(
                attach.request_id,
                expected_revision=2,
                payload={"platform": IDENTITY_PLATFORM, "external_account_id": "semantic-human"},
            ),
        )
    assert attached.value.problem.code is ProblemCode.STATE_MISMATCH
    await _mutate_receipt(
        ingest.request_id,
        {
            "presence_id": presence.resource_id,
            "platform": IDENTITY_PLATFORM,
            "enabled": True,
            "ingest_eligible": True,
            "revision": 2,
        },
    )
    with pytest.raises(ControlCommandError) as ingest_err:
        await service.set_presence_ingest(ingest, ingest_cmd)
    assert ingest_err.value.problem.code is ProblemCode.STATE_MISMATCH
    await _mutate_receipt(
        stop.request_id,
        {
            "presence_id": presence.resource_id,
            "platform": IDENTITY_PLATFORM,
            "enabled": True,
            "ingest_eligible": True,
            "revision": 2,
        },
    )
    with pytest.raises(ControlCommandError) as stopped:
        await service.stop_presence(stop, stop_cmd)
    assert stopped.value.problem.code is ProblemCode.STATE_MISMATCH
    await _mutate_receipt(
        route.request_id,
        {
            "kind": RouteKind.SPACE_ACTIVE.value,
            "owner_id": space_id,
            "binding_id": binding.resource_id,
            "presence_id": presence.resource_id,
            "paused": True,
            "revision": created.revision,
            "route_generation": created.effective_state["route_generation"],
            "reference_state": RouteReferenceState.CONSISTENT.value,
        },
    )
    with pytest.raises(ControlCommandError) as route_err:
        await service.set_route(route, route_cmd)
    assert route_err.value.problem.code is ProblemCode.STATE_MISMATCH


@pytest.mark.asyncio
async def test_coordinated_resource_and_audit_substitution_is_rejected(
    database: Database,
) -> None:
    await _set_v2(database)
    person_id = await _add_person(database, enabled=False)
    other = await _add_person(database, enabled=False)
    service = _service(database)
    context = _context(_principal(*_WRITE_CAPS), PersonId.parse(person_id))
    command = _command(context.request_id, expected_revision=1)
    await service.enable_person(context, command)
    async with database.sessions() as session, session.begin():
        receipt = await session.scalar(select(ControlCommandReceiptModel))
        assert receipt is not None and receipt.audit_id is not None
        receipt.result_resource_id = other
        audit = await session.get(AdminOperationEventModel, receipt.audit_id)
        assert audit is not None
        audit.target_id = other
    with pytest.raises(ControlCommandError) as rejected:
        await service.enable_person(context, command)
    assert rejected.value.problem.code is ProblemCode.STATE_MISMATCH


@pytest.mark.asyncio
async def test_non_cacheable_failed_problem_is_rejected(database: Database) -> None:
    await _set_v2(database)
    service = _service(database)
    context = _context(_principal(*_WRITE_CAPS), PersonId.new())
    command = _command(context.request_id, expected_revision=1)
    with pytest.raises(ControlCommandError) as first:
        await service.enable_person(context, command)
    assert first.value.problem.code is ProblemCode.NOT_FOUND
    async with database.sessions() as session, session.begin():
        receipt = await session.scalar(select(ControlCommandReceiptModel))
        assert receipt is not None and receipt.audit_id is not None
        receipt.problem_code = ProblemCode.STATE_MISMATCH.value
        audit = await session.get(AdminOperationEventModel, receipt.audit_id)
        assert audit is not None
        audit.error_category = ProblemCode.STATE_MISMATCH.value
        audit.after_json = json.dumps({"problem": ProblemCode.STATE_MISMATCH.value})
    with pytest.raises(ControlCommandError) as rejected:
        await service.enable_person(context, command)
    assert rejected.value.problem.code is ProblemCode.STATE_MISMATCH


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "column,value",
    (
        ("problem_code", "not_found"),
        ("result_resource_id", None),
        ("result_revision", None),
        ("effective_state_json", None),
        ("operation_kind", "backfill"),
        ("operation_ref", "run-1"),
        ("operation_kind", "rebuild"),
        ("operation_ref", "rebuild:forged"),
    ),
)
async def test_bypassed_success_receipt_lifecycle_is_rejected(
    database: Database, column: str, value: object
) -> None:
    await _set_v2(database)
    person_id = await _add_person(database, enabled=False)
    service = _service(database)
    context = _context(_principal(*_WRITE_CAPS), PersonId.parse(person_id))
    command = _command(context.request_id, expected_revision=1)
    await service.enable_person(context, command)
    await _bypass_receipt_checks(database)
    async with database.sessions() as session, session.begin():
        row = await session.scalar(select(ControlCommandReceiptModel))
        assert row is not None
        setattr(row, column, value)
    with pytest.raises(ControlCommandError) as rejected:
        await service.enable_person(context, command)
    assert rejected.value.problem.code is ProblemCode.STATE_MISMATCH


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "column,value",
    (
        ("result_resource_id", str(uuid4())),
        ("result_revision", 1),
        ("effective_state_json", json.dumps({"enabled": True, "revision": 1})),
        ("operation_kind", "backfill"),
        ("operation_ref", "run-1"),
    ),
)
async def test_bypassed_failed_receipt_lifecycle_is_rejected(
    database: Database, column: str, value: object
) -> None:
    await _set_v2(database)
    service = _service(database)
    context = _context(_principal(*_WRITE_CAPS), PersonId.new())
    command = _command(context.request_id, expected_revision=1)
    with pytest.raises(ControlCommandError):
        await service.enable_person(context, command)
    await _bypass_receipt_checks(database)
    async with database.sessions() as session, session.begin():
        row = await session.scalar(select(ControlCommandReceiptModel))
        assert row is not None
        setattr(row, column, value)
    with pytest.raises(ControlCommandError) as rejected:
        await service.enable_person(context, command)
    assert rejected.value.problem.code is ProblemCode.STATE_MISMATCH


@pytest.mark.asyncio
async def test_safe_success_replay_is_byte_equivalent_for_every_operation(
    database: Database,
) -> None:
    await _set_v2(database)
    person_id = await _add_person(database)
    space_id = await _add_space(database)
    service = _service(database)
    principal = _principal(*_WRITE_CAPS)
    results: list[tuple[object, object, object]] = []

    async def _once(method: str, target: object, command: ControlCommand) -> None:
        context = _context(principal, target, request_id=command.request_id)
        first = await getattr(service, method)(context, command)
        second = await getattr(service, method)(context, command)
        assert _result_blob(second) == _result_blob(first)
        results.append((method, first, second))

    await _once(
        "disable_person",
        PersonId.parse(person_id),
        _command(RequestId.new(), expected_revision=1),
    )
    await _once(
        "enable_person",
        PersonId.parse(person_id),
        _command(RequestId.new(), expected_revision=2),
    )
    attach = _command(
        RequestId.new(),
        expected_revision=3,
        payload={"platform": IDENTITY_PLATFORM, "external_account_id": "replay-human"},
    )
    await _once("attach_identity_binding", PersonId.parse(person_id), attach)
    await _once(
        "disable_space",
        SpaceId.parse(space_id),
        _command(RequestId.new(), expected_revision=1),
    )
    await _once(
        "enable_space",
        SpaceId.parse(space_id),
        _command(RequestId.new(), expected_revision=2),
    )
    space_attach = _command(
        RequestId.new(),
        expected_revision=3,
        payload={"platform": IDENTITY_PLATFORM, "external_space_id": "replay-space"},
    )
    await _once("attach_space_binding", SpaceId.parse(space_id), space_attach)
    register = _command(
        RequestId.new(),
        expected_revision=0,
        payload={"platform": IDENTITY_PLATFORM, "external_account_id": "replay-yuki"},
    )
    await _once("register_presence", YukiControlTarget.PERMANENT_YUKI, register)
    presence_id = PresenceId.parse(results[-1][1].resource_id)
    await _once("stop_presence", presence_id, _command(RequestId.new(), expected_revision=1))
    await _once("start_presence", presence_id, _command(RequestId.new(), expected_revision=2))
    ingest = _command(RequestId.new(), expected_revision=3, payload={"ingest_eligible": True})
    await _once("set_presence_ingest", presence_id, ingest)
    binding_id = results[2][1].resource_id
    space_binding_id = results[5][1].resource_id
    set_person = _command(
        RequestId.new(),
        expected_revision=0,
        payload={
            "kind": RouteKind.PERSON_ACTIVE.value,
            "identity_binding_id": binding_id,
            "presence_id": presence_id.text,
        },
    )
    await _once("set_route", PersonId.parse(person_id), set_person)
    await _once(
        "pause_route",
        PersonId.parse(person_id),
        _command(
            RequestId.new(),
            expected_revision=1,
            payload={"kind": RouteKind.PERSON_ACTIVE.value},
        ),
    )
    await _once(
        "resume_route",
        PersonId.parse(person_id),
        _command(
            RequestId.new(),
            expected_revision=2,
            payload={"kind": RouteKind.PERSON_ACTIVE.value},
        ),
    )
    set_ingest = _command(
        RequestId.new(),
        expected_revision=0,
        payload={
            "kind": RouteKind.SPACE_BINDING_INGEST.value,
            "ingest_presence_id": presence_id.text,
        },
    )
    await _once("set_route", SpaceBindingId.parse(space_binding_id), set_ingest)
    set_space = _command(
        RequestId.new(),
        expected_revision=0,
        payload={
            "kind": RouteKind.SPACE_ACTIVE.value,
            "space_binding_id": space_binding_id,
            "presence_id": presence_id.text,
        },
    )
    await _once("set_route", SpaceId.parse(space_id), set_space)
    assert {item[0] for item in results} >= set(_METHODS)


def test_control_plane_stays_clean_and_adapter_has_no_legacy_insert() -> None:
    adapter = ADAPTER_PATH.read_text(encoding="utf-8")
    assert "get_bots" not in adapter
    assert "OFFSET" not in adapter
    assert ".offset(" not in adapter
    assert "DeliveryRoute" not in adapter
    assert "presence_active_route" not in adapter
    assert "GatewayConnection" not in adapter
    assert "INSERT INTO people" not in adapter
    assert "INSERT INTO groups" not in adapter
    assert "PersonModel(" not in adapter
    assert "GroupModel(" not in adapter
    assert "APIRouter" not in adapter
    assert "FastAPI" not in adapter
    tree = ast.parse(adapter, filename=str(ADAPTER_PATH))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr != "offset"
        if isinstance(node, ast.Name):
            assert node.id != "get_bots"
            assert node.id != "AdminActor"
    for path in _python_files(CONTROL_PLANE_ROOT):
        source = path.read_text(encoding="utf-8")
        parsed = ast.parse(source, filename=str(path))
        for node in ast.walk(parsed):
            if isinstance(node, ast.Name):
                assert node.id != "Any"
                assert node.id != "AdminActor"
                assert node.id != "get_bots"
            if isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith("sqlalchemy")
                assert not node.module.startswith("qq_ai_bot.persistence")
                assert not node.module.startswith("qq_ai_bot.admin")
    assert is_protocol_capability("web_search") is True
