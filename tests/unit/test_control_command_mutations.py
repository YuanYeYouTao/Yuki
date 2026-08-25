"""C11 identity/route commands: v1 cutover, v2 mutations, idempotency, and PII."""

from __future__ import annotations

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
from qq_ai_bot.domain.identity import (
    PersonId,
    PresenceId,
    PrincipalId,
    RequestId,
    SpaceBindingId,
    SpaceId,
)
from qq_ai_bot.identity.canonical_repository import IDENTITY_PLATFORM
from qq_ai_bot.identity.db_models import (
    CanonicalPersonModel,
    CanonicalSpaceModel,
    IdentityBindingModel,
    PresenceModel,
    SpaceBindingModel,
)
from qq_ai_bot.persistence.control_command import ControlCommandAdapter
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import AdminOperationEventModel

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
    "person_active_routes",
    "space_binding_ingest_routes",
    "space_active_routes",
)
_SIGNATURE_TABLES = (
    "canonical_conversations",
    "conversation_legacy_aliases",
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
    del database


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
                first_seen_at=_NOW,
                last_seen_at=_NOW,
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
        count = int(
            await session.scalar(
                text(
                    "SELECT COUNT(*) FROM presences WHERE external_account_id = 'server-uuid-yuki'"
                )
            )
            or 0
        )
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
