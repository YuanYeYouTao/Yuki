"""C12: core admin services take ControlPrincipal + canonical targets."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from tests.conftest import make_settings
from tests.unit.test_plugin_facades import invocation
from tests.unit.test_runtime_admin import actor

from qq_ai_bot.admin.action_service import AdminActionService, TargetResolver
from qq_ai_bot.admin.audit import AdminAuditService
from qq_ai_bot.admin.config_service import RuntimeConfigService
from qq_ai_bot.admin.control_resolution import ControlAccess, ControlPrincipalLookupError
from qq_ai_bot.admin.models import AdminActor, ControlAuditRef
from qq_ai_bot.control_plane.principal import ControlPrincipal, PrincipalSource
from qq_ai_bot.control_plane.targets import PersonControlTarget, SpaceControlTarget
from qq_ai_bot.domain.control import DecisionContext
from qq_ai_bot.domain.identity import PersonId, PrincipalId, RequestId
from qq_ai_bot.memory.repository import MemoryFactRepository
from qq_ai_bot.memory.service import MemoryFactService
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.repositories import (
    GroupSettingsRepository,
    PeopleRepository,
    PrivateUserSettingsRepository,
    RelationshipRepository,
    UserProfileRepository,
)
from qq_ai_bot.plugin_host.facades import HostPluginContext, PluginFacadeServices
from qq_ai_bot.services.admin.group_admin import GroupAdminService
from qq_ai_bot.services.admin.memory_admin import MemoryAdminService
from qq_ai_bot.services.admin.preference_admin import PreferenceAdminService
from qq_ai_bot.services.admin.private_access_admin import PrivateAccessAdminService
from qq_ai_bot.services.admin.relationship_admin import RelationshipAdminService
from yuki_plugin_sdk.errors import PluginPermissionError
from yuki_plugin_sdk.permissions import PluginPermission

SRC_ROOT = Path(__file__).resolve().parents[2] / "src"
CORE_ADMIN_FILES = (
    SRC_ROOT / "qq_ai_bot" / "services" / "admin" / "relationship_admin.py",
    SRC_ROOT / "qq_ai_bot" / "services" / "admin" / "group_admin.py",
    SRC_ROOT / "qq_ai_bot" / "services" / "admin" / "private_access_admin.py",
    SRC_ROOT / "qq_ai_bot" / "services" / "admin" / "preference_admin.py",
    SRC_ROOT / "qq_ai_bot" / "services" / "admin" / "control_auth.py",
)
FORBIDDEN_CORE_TOKENS = ("AdminActor", "Settings.superusers", "is_superuser=True")


def _principal(*capabilities: str, roles: tuple[str, ...] = ()) -> ControlPrincipal:
    return ControlPrincipal(
        principal_id=PrincipalId.new(),
        person_id=PersonId.new(),
        source=PrincipalSource.QQ,
        roles=roles,
        granted_capabilities=capabilities,
        authenticated=True,
        active=True,
    )


def _person_context(
    principal: ControlPrincipal,
    storage_user_id: str,
    *,
    lockout_protected: bool = False,
    person_id: PersonId | None = None,
) -> DecisionContext[ControlPrincipal, PrincipalSource, PersonControlTarget]:
    return DecisionContext(
        request_id=RequestId.new(),
        principal=principal,
        source=principal.source,
        canonical_target=PersonControlTarget(
            person_id=person_id if person_id is not None else principal.person_id,
            storage_user_id=storage_user_id,
            lockout_protected=lockout_protected,
        ),
    )


def _space_context(
    principal: ControlPrincipal,
    storage_group_id: str,
) -> DecisionContext[ControlPrincipal, PrincipalSource, SpaceControlTarget]:
    return DecisionContext(
        request_id=RequestId.new(),
        principal=principal,
        source=principal.source,
        canonical_target=SpaceControlTarget(space_id=None, storage_group_id=storage_group_id),
    )


def _audit(user_id: str = "9000") -> ControlAuditRef:
    return ControlAuditRef(
        user_id=user_id, trigger_message_id="c12", conversation_key="private:9000"
    )


def test_core_admin_services_have_no_transport_actor_auth() -> None:
    for path in CORE_ADMIN_FILES:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        for token in FORBIDDEN_CORE_TOKENS:
            assert token not in source, f"{path.name} still contains {token}"
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                assert node.id != "AdminActor"
            if isinstance(node, ast.Attribute) and node.attr == "superusers":
                raise AssertionError(f"{path.name} still reads Settings.superusers")


def test_plugin_and_automation_do_not_self_report_superuser() -> None:
    files = (
        "src/qq_ai_bot/plugin_host/facades.py",
        "src/qq_ai_bot/automation/handlers.py",
        "src/qq_ai_bot/automation/service.py",
        "src/qq_ai_bot/mcp/admin.py",
    )
    repo = SRC_ROOT.parent
    for rel in files:
        source = (repo / rel).read_text(encoding="utf-8")
        tree = ast.parse(source, filename=rel)
        assert "is_superuser=True" not in source, rel
        assert "actor_is_superuser=True" not in source, rel
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            constructed = (isinstance(func, ast.Name) and func.id == "AdminActor") or (
                isinstance(func, ast.Attribute) and func.attr == "AdminActor"
            )
            assert not constructed, rel
    assert "is_superuser=True" not in (
        SRC_ROOT / "qq_ai_bot" / "services" / "admin" / "relationship_admin.py"
    ).read_text(encoding="utf-8")


def test_target_resolver_still_requires_current_message_proof() -> None:
    current = actor(
        text="把 @张三 的好感度降低 5",
        group_id="2001",
        mentions=("12345678",),
    )
    assert (
        TargetResolver.user({"target": "mentioned_user", "user_id": "12345678"}, current)
        == "12345678"
    )
    with pytest.raises(ValueError):
        TargetResolver.user({"target": "explicit_user_id", "user_id": "87654321"}, current)
    assert TargetResolver.group({"target": "current_group"}, current) == "2001"
    with pytest.raises(ValueError):
        TargetResolver.user({"target": "arbitrary_user", "user_id": "12345678"}, current)


@pytest.mark.asyncio
async def test_relationship_core_rejects_forged_principal_without_capability(
    database: Database,
) -> None:
    settings = make_settings(database.url)
    audit = AdminAuditService(database)
    service = RelationshipAdminService(
        relationships=RelationshipRepository(database),
        audit=audit,
        runtime_config=RuntimeConfigService(settings=settings, database=database),
    )
    forged = _principal()
    context = _person_context(forged, "1001")
    with pytest.raises(PermissionError, match="超级管理员"):
        await service.set_affection(context, 88, audit=_audit())


@pytest.mark.asyncio
async def test_relationship_and_group_real_call_chain_uses_resolved_principal(
    database: Database,
) -> None:
    settings = make_settings(database.url)
    people = UserProfileRepository(database)
    await people.observe(user_id="9000", nickname="管理员")
    await people.observe(user_id="1001", nickname="成员")
    access = ControlAccess(database, superuser_ids=settings.superusers)
    principal = await access.principal_for_qq("9000")
    assert "superuser" in principal.roles
    assert "control.relationship.mutate" in principal.granted_capabilities
    audit = AdminAuditService(database)
    relationships = RelationshipAdminService(
        relationships=RelationshipRepository(database),
        audit=audit,
        runtime_config=RuntimeConfigService(settings=settings, database=database),
    )
    context = access.context(principal, await access.person_target("1001"))
    before, after = await relationships.set_affection(context, 88, audit=_audit("9000"))
    assert before.affection_score != after.affection_score or after.affection_score == 88
    assert after.affection_score == 88
    history = await audit.history(capability="relationship")
    assert history[0].actor_user_id == "9000"
    assert history[0].target_id == "1001"

    groups = GroupAdminService(
        groups=GroupSettingsRepository(database),
        runtime_config=RuntimeConfigService(settings=settings, database=database),
        audit=audit,
    )
    space = access.context(principal, await access.space_target("2001"))
    enabled = await groups.enable_current_group(space, audit=_audit("9000"))
    assert enabled.enabled


@pytest.mark.asyncio
async def test_missing_binding_fails_closed_and_does_not_synthesize_person(
    database: Database,
) -> None:
    access = ControlAccess(database, superuser_ids=frozenset({"9000"}))
    with pytest.raises(ControlPrincipalLookupError):
        await access.principal_for_qq("9000")
    target = await access.person_target("1001")
    assert target.person_id is None
    assert target.storage_user_id == "1001"
    assert target.lockout_protected is False


@pytest.mark.asyncio
async def test_private_access_honors_lockout_protected_without_settings(
    database: Database,
) -> None:
    settings = make_settings(database.url)
    audit = AdminAuditService(database)
    service = PrivateAccessAdminService(
        private_users=PrivateUserSettingsRepository(database),
        audit=audit,
        runtime_config=RuntimeConfigService(settings=settings, database=database),
    )
    principal = _principal("control.private_access.mutate", roles=("superuser",))
    protected = _person_context(principal, "9000", lockout_protected=True)
    with pytest.raises(ValueError, match="不能关闭超级用户"):
        await service.disable_user(protected, audit=_audit())
    allowed = _person_context(principal, "1001", lockout_protected=False)
    row = await service.disable_user(allowed, audit=_audit())
    assert row.enabled is False


@pytest.mark.asyncio
async def test_preference_self_service_and_foreign_denied(database: Database) -> None:
    settings = make_settings(database.url)
    memories = MemoryFactService(MemoryFactRepository(database))
    service = PreferenceAdminService(
        settings=settings,
        memories=memories,
        audit=AdminAuditService(database),
    )
    owner = _principal("control.preference.read")
    own = _person_context(owner, "1001", person_id=owner.person_id)
    rows = await service.list_preferences(own, _audit("1001"))
    assert rows == ()
    await service.set_preference(own, "口味", "微辣", audit=_audit("1001"))
    listed = await service.list_preferences(own, _audit("1001"))
    assert listed[0].key == "口味"
    stranger = _principal("control.preference.read")
    other = _person_context(stranger, "1001", person_id=PersonId.new())
    with pytest.raises(PermissionError):
        await service.list_preferences(other, _audit("2002"))


@pytest.mark.asyncio
async def test_admin_action_real_chain_rejects_unbound_actor(database: Database) -> None:
    settings = make_settings(database.url)
    audit = AdminAuditService(database)
    runtime = RuntimeConfigService(settings=settings, database=database)
    actions = AdminActionService(
        settings=settings,
        database=database,
        relationships=RelationshipAdminService(
            relationships=RelationshipRepository(database),
            audit=audit,
            runtime_config=runtime,
        ),
        memories=MemoryAdminService(
            settings=settings,
            memories=MemoryFactService(MemoryFactRepository(database)),
            audit=audit,
        ),
        preferences=PreferenceAdminService(
            settings=settings,
            memories=MemoryFactService(MemoryFactRepository(database)),
            audit=audit,
        ),
        groups=GroupAdminService(
            groups=GroupSettingsRepository(database),
            runtime_config=runtime,
            audit=audit,
        ),
        private_access=PrivateAccessAdminService(
            private_users=PrivateUserSettingsRepository(database),
            audit=audit,
            runtime_config=runtime,
        ),
    )
    unbound = AdminActor(
        user_id="9000",
        is_superuser=True,
        trigger_message_id="nl-1",
        conversation_key="group:2001",
        current_group_id="2001",
        current_message_text="self",
    )
    with pytest.raises(PermissionError, match="控制主体"):
        await actions.execute("relationship.get", {"target": "self"}, unbound)


@pytest.mark.asyncio
async def test_plugin_cannot_self_report_superuser_to_mutate_relationship(
    database: Database,
) -> None:
    people = UserProfileRepository(database)
    await people.observe(user_id="10001", nickname="路人")
    await people.observe(user_id="9000", nickname="管理员")
    settings = make_settings(database.url)
    audit = AdminAuditService(database)
    service = RelationshipAdminService(
        relationships=RelationshipRepository(database),
        audit=audit,
        runtime_config=RuntimeConfigService(settings=settings, database=database),
    )
    context = HostPluginContext(
        plugin_id="example.plugin",
        approved_permissions=(PluginPermission.RELATIONSHIP_WRITE,),
        superuser_ids=("9000",),
        services=PluginFacadeServices(
            people=PeopleRepository(database),
            relationships=RelationshipRepository(database),
            relationship_admin=service,
        ),
    )
    with context.bind(invocation(user_id="10001")):
        with pytest.raises(PluginPermissionError, match="SUPERUSERS"):
            await context.relationship.adjust("10001", affection_delta=5, reason="提权")
    with context.bind(invocation(user_id="9000")):
        result = await context.relationship.adjust("10001", affection_delta=5, reason="合法调整")
    assert result.ok
    snapshot = await RelationshipRepository(database).get("10001")
    assert snapshot is not None
    assert snapshot.affection_score == 55
