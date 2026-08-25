"""C9 control-plane principal and contract freeze."""

from __future__ import annotations

import ast
import dataclasses
import inspect
import itertools
import math
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from pathlib import Path
from uuid import uuid4

import pytest

from qq_ai_bot.control_plane import (
    CONTROL_CAPABILITY_DESCRIPTORS,
    CONTROL_CAPABILITY_IDS,
    DEFAULT_PAGE_LIMIT,
    DENIED_CAPABILITY_ID,
    FROZEN_PROBLEM_CODES,
    MAX_PAGE_LIMIT,
    CapabilityFamily,
    CapabilitySensitivity,
    CatalogCapabilityView,
    CatalogSourceKind,
    ControlCapabilityDescriptor,
    ControlCommand,
    ControlPrincipal,
    ControlResult,
    Cursor,
    DecisionContext,
    OperationRef,
    OperationStatus,
    Page,
    PageRequest,
    PolicyDecision,
    PolicyEffect,
    PrincipalSource,
    Problem,
    ProblemCode,
    StateEpoch,
    decide,
    is_forbidden_control_capability,
    is_protocol_capability,
    paginate,
    project_catalog_capabilities,
    project_source_capability_id,
)
from qq_ai_bot.control_plane.contracts import DecisionContext as ContractsDecisionContext
from qq_ai_bot.domain.control import DecisionContext as DomainDecisionContext
from qq_ai_bot.domain.identity import PersonId, PrincipalId, RequestId, SpaceId

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src"
CONTROL_PLANE_ROOT = SRC_ROOT / "qq_ai_bot" / "control_plane"
EXPECTED_PROBLEM_CODES = (
    "unauthenticated",
    "capability_denied",
    "not_found",
    "validation_error",
    "version_conflict",
    "idempotency_conflict",
    "binding_ambiguous",
    "route_ambiguous",
    "route_paused",
    "populated_merge_forbidden",
    "legacy_identity_forbidden",
    "pending_cutover",
    "state_mismatch",
    "precondition_failed",
    "secret_not_readable",
    "operation_unavailable",
)
FORBIDDEN_IMPORT_PREFIXES = (
    "qq_ai_bot.cli",
    "qq_ai_bot.plugins",
    "qq_ai_bot.services",
    "qq_ai_bot.admin",
    "qq_ai_bot.config",
    "qq_ai_bot.persistence",
    "qq_ai_bot.domain.messages",
    "nonebot",
    "nonebot_adapter_onebot",
    "sqlalchemy",
    "alembic",
    "httpx",
    "pydantic",
)
FORBIDDEN_IMPORT_TOKENS = (
    "AdminActor",
    "InboundMessage",
    "Settings",
    "TargetResolver",
    "matcher",
    "renderer",
)
DANGEROUS_CAPABILITIES = (
    "call_onebot_api",
    "onebot:call_onebot_api:any_public_action",
    "mcp.call",
    "mcp.tool.call",
    "plugin.run",
    "plugin.arbitrary_run",
    "raw_sql",
    "sql.execute",
    "secret.read",
    "secret.write",
)
_NOW = datetime(2026, 8, 24, 10, 0, tzinfo=UTC)


class _ImportCollector(ast.NodeVisitor):
    def __init__(self) -> None:
        self.modules: list[str] = []
        self.names: list[str] = []

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.modules.append(alias.name)
            self.names.append(alias.asname or alias.name)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = ("." * node.level) + (node.module or "")
        self.modules.append(module)
        for alias in node.names:
            self.names.append(alias.name)


def _python_files(root: Path) -> tuple[Path, ...]:
    return tuple(sorted(path for path in root.rglob("*.py") if path.is_file()))


def _matches(module: str, prefix: str) -> bool:
    return module == prefix or module.startswith(prefix + ".")


def _qq_principal(
    *,
    person_id: PersonId | None = None,
    roles: tuple[str, ...] = ("superuser",),
    capabilities: tuple[str, ...] = ("identity.person.read",),
    authenticated: bool = True,
    active: bool = True,
) -> ControlPrincipal:
    return ControlPrincipal(
        principal_id=PrincipalId.new(),
        person_id=person_id or PersonId.new(),
        source=PrincipalSource.QQ,
        roles=roles,
        granted_capabilities=capabilities,
        authenticated=authenticated,
        active=active,
    )


def _context(
    principal: ControlPrincipal,
    target: object | None = None,
) -> DecisionContext[ControlPrincipal, PrincipalSource, object]:
    return DecisionContext(
        request_id=RequestId.new(),
        principal=principal,
        source=principal.source,
        canonical_target=target if target is not None else principal.person_id or SpaceId.new(),
        reason="inspect",
    )


def _operation(**overrides: object) -> OperationRef:
    values: dict[str, object] = {
        "operation_id": "backfill-1",
        "status": OperationStatus.RUNNING,
        "progress": 0.25,
        "state_epoch": StateEpoch.V1,
        "error_category": None,
        "created_at": _NOW,
        "updated_at": _NOW + timedelta(seconds=5),
    }
    values.update(overrides)
    return OperationRef(
        operation_id=str(values["operation_id"]),
        status=values["status"],  # type: ignore[arg-type]
        progress=values["progress"],  # type: ignore[arg-type]
        state_epoch=values["state_epoch"],  # type: ignore[arg-type]
        error_category=values["error_category"],  # type: ignore[arg-type]
        created_at=values["created_at"],  # type: ignore[arg-type]
        updated_at=values["updated_at"],  # type: ignore[arg-type]
    )


def test_problem_codes_are_exactly_the_frozen_set() -> None:
    assert tuple(code.value for code in ProblemCode) == EXPECTED_PROBLEM_CODES
    assert FROZEN_PROBLEM_CODES == frozenset(EXPECTED_PROBLEM_CODES)
    assert len(ProblemCode) == 16
    problem = Problem(ProblemCode.NOT_FOUND)
    assert not hasattr(problem, "message")
    assert "message" not in {field.name for field in dataclasses.fields(Problem)}
    source = Path(inspect.getfile(Problem)).read_text(encoding="utf-8")
    assert "未认证" not in source
    assert "权限" not in source


def test_page_request_limit_and_cursor_reject_offset() -> None:
    request = PageRequest()
    assert request.limit == DEFAULT_PAGE_LIMIT == 20
    assert MAX_PAGE_LIMIT == 100
    assert request.cursor is None
    assert PageRequest(limit=100, cursor=Cursor("after-z")).limit == 100
    with pytest.raises(TypeError):
        PageRequest(limit=20, offset=0)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        PageRequest(limit=True)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        PageRequest(limit=0)
    with pytest.raises(ValueError):
        PageRequest(limit=101)
    with pytest.raises(ValueError):
        Cursor("")
    with pytest.raises(ValueError):
        Cursor(" padded ")
    with pytest.raises(ValueError):
        Cursor("has space")
    with pytest.raises(ValueError):
        Cursor("bad\ncursor")
    with pytest.raises(ValueError):
        Cursor("bad\x00cursor")
    with pytest.raises(ValueError):
        Cursor("x" * 257)


def test_page_preserves_stable_order_and_opaque_cursor() -> None:
    items = ("c", "a", "b", "d")
    first = paginate(items, PageRequest(limit=2), sort_key=lambda item: item)
    assert first.items == ("a", "b")
    assert first.next_cursor == Cursor("b")
    second = paginate(
        items, PageRequest(limit=2, cursor=first.next_cursor), sort_key=lambda item: item
    )
    assert second.items == ("c", "d")
    assert second.next_cursor is None
    preserved = Page(("zeta", "alpha"), next_cursor=Cursor("zeta"))
    assert preserved.items == ("zeta", "alpha")
    with pytest.raises(ValueError, match="cursor"):
        paginate(items, PageRequest(limit=2, cursor=Cursor("missing")), sort_key=lambda item: item)
    with pytest.raises(ValueError, match="unique"):
        paginate(("a", "a"), PageRequest(limit=1), sort_key=lambda item: item)


def test_multiple_qq_resolved_superuser_principals_coexist() -> None:
    first_person = PersonId.new()
    second_person = PersonId.new()
    first = _qq_principal(person_id=first_person)
    second = _qq_principal(person_id=second_person)
    assert first.principal_id != second.principal_id
    assert first.person_id != second.person_id
    assert first.roles == frozenset({"superuser"})
    assert second.roles == frozenset({"superuser"})
    assert first.allows("identity.person.read")
    assert second.allows("identity.person.read")
    with pytest.raises(ValueError, match="person_id"):
        ControlPrincipal(
            principal_id=PrincipalId.new(),
            person_id=None,
            source=PrincipalSource.QQ,
            authenticated=True,
            active=True,
        )
    system = ControlPrincipal(
        principal_id=PrincipalId.new(),
        person_id=None,
        source=PrincipalSource.SYSTEM,
        authenticated=True,
        active=True,
        granted_capabilities=("control.system.read",),
    )
    assert system.person_id is None
    assert system.allows("control.system.read")


def test_principal_normalizes_and_rejects_illegal_tokens() -> None:
    principal = _qq_principal(roles=("SUPERUSER",), capabilities=(" Identity.Person.Read ",))
    assert principal.roles == frozenset({"superuser"})
    assert principal.granted_capabilities == frozenset({"identity.person.read"})
    assert isinstance(principal.roles, frozenset)
    assert isinstance(principal.granted_capabilities, frozenset)
    with pytest.raises(dataclasses.FrozenInstanceError):
        principal.authenticated = False  # type: ignore[misc]
    with pytest.raises(ValueError, match="illegal role"):
        _qq_principal(roles=("",))
    with pytest.raises(ValueError, match="illegal role"):
        _qq_principal(roles=("超级管理员",))
    with pytest.raises(ValueError, match="illegal capability"):
        _qq_principal(capabilities=("identity.person.read!",))
    with pytest.raises(ValueError, match="forbidden capability"):
        _qq_principal(capabilities=("call_onebot_api",))
    with pytest.raises(TypeError):
        _qq_principal(authenticated=1)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("authenticated", "active", "granted", "expected", "code"),
    [
        (False, True, True, False, ProblemCode.UNAUTHENTICATED),
        (True, False, True, False, ProblemCode.PRECONDITION_FAILED),
        (True, True, False, False, ProblemCode.CAPABILITY_DENIED),
        (False, False, False, False, ProblemCode.UNAUTHENTICATED),
        (True, True, True, True, None),
    ],
)
def test_permission_matrix_default_deny(
    authenticated: bool,
    active: bool,
    granted: bool,
    expected: bool,
    code: ProblemCode | None,
) -> None:
    principal = _qq_principal(
        authenticated=authenticated,
        active=active,
        capabilities=("identity.person.read",) if granted else (),
    )
    assert principal.allows("identity.person.read") is expected
    assert principal.allows("identity.person.forget") is False
    assert principal.allows("") is False
    assert principal.allows("call_onebot_api") is False
    decision = decide(_context(principal), "identity.person.read")
    assert decision.allowed is expected
    if expected:
        assert decision.effect is PolicyEffect.ALLOW
        assert decision.problem is None
    else:
        assert decision.effect is PolicyEffect.DENY
        assert decision.problem is not None
        assert decision.problem.code is code


def test_decision_context_is_reused_and_copied_envelope_is_rejected() -> None:
    assert DecisionContext is DomainDecisionContext
    assert ContractsDecisionContext is DomainDecisionContext
    principal = _qq_principal()
    context = _context(principal, target=principal.person_id)
    assert decide(context, "IDENTITY.PERSON.READ").allowed is True

    @dataclass(frozen=True, slots=True)
    class CopiedEnvelope:
        request_id: RequestId
        principal: ControlPrincipal
        source: PrincipalSource
        canonical_target: object
        reason: str = ""

    with pytest.raises(TypeError, match="DecisionContext"):
        decide(
            CopiedEnvelope(
                request_id=RequestId.new(),
                principal=principal,
                source=PrincipalSource.QQ,
                canonical_target=principal.person_id,
            ),
            "identity.person.read",
        )

    @dataclass(frozen=True, slots=True)
    class _StubPrincipal:
        principal_id: PrincipalId

    with pytest.raises(TypeError, match="ControlPrincipal"):
        decide(
            DecisionContext(
                request_id=RequestId.new(),
                principal=_StubPrincipal(PrincipalId.new()),
                source=PrincipalSource.QQ,
                canonical_target=SpaceId.new(),
            ),
            "identity.person.read",
        )
    malformed = decide(context, 1)
    assert malformed.effect is PolicyEffect.DENY
    assert malformed.problem is not None
    assert malformed.problem.code is ProblemCode.VALIDATION_ERROR
    assert malformed.capability == DENIED_CAPABILITY_ID
    illegal = decide(context, "not a capability")
    assert illegal.effect is PolicyEffect.DENY
    assert illegal.problem is not None
    assert illegal.problem.code is ProblemCode.VALIDATION_ERROR
    assert illegal.capability == DENIED_CAPABILITY_ID


def test_control_command_requires_request_uuid_and_revision() -> None:
    request_id = RequestId.new()
    command = ControlCommand(
        request_id=request_id,
        expected_revision=3,
        payload={"op": "enable", "nested": {"ok": True}},
    )
    assert command.request_id is request_id
    assert command.expected_revision == 3
    assert command.payload["op"] == "enable"
    with pytest.raises(TypeError, match="RequestId"):
        ControlCommand(request_id=str(uuid4()), expected_revision=0, payload={})  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="RequestId"):
        ControlCommand(request_id=PrincipalId.new(), expected_revision=0, payload={})  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        ControlCommand(request_id=request_id, expected_revision=True, payload={})  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        ControlCommand(request_id=request_id, expected_revision=-1, payload={})
    with pytest.raises(TypeError, match="json"):
        ControlCommand(request_id=request_id, expected_revision=0, payload={"when": _NOW})


def test_result_and_operation_invariants() -> None:
    result = ControlResult(
        success=True,
        resource_id=PersonId.new().text,
        revision=4,
        audit_id="audit-1",
        effective_state={"enabled": True},
        operation=_operation(status=OperationStatus.QUEUED, progress=0),
    )
    assert result.success is True
    assert result.operation is not None
    assert result.operation.state_epoch is StateEpoch.V1
    succeeded = _operation(status=OperationStatus.SUCCEEDED, progress=1, error_category=None)
    assert succeeded.error_category is None
    failed = _operation(
        status=OperationStatus.FAILED,
        progress=0.5,
        error_category=ProblemCode.STATE_MISMATCH,
    )
    assert failed.error_category == "state_mismatch"
    with pytest.raises(ValueError, match="error_category"):
        _operation(status=OperationStatus.SUCCEEDED, error_category="state_mismatch")
    with pytest.raises(ValueError, match="error_category"):
        _operation(status=OperationStatus.FAILED, error_category=None)
    with pytest.raises(ValueError, match="progress"):
        _operation(progress=1.01)
    with pytest.raises(ValueError, match="progress"):
        _operation(progress=-0.01)
    with pytest.raises(TypeError, match="progress"):
        _operation(progress=True)
    with pytest.raises(ValueError, match="progress"):
        _operation(progress=math.nan)
    with pytest.raises(ValueError, match="progress"):
        _operation(progress=math.inf)
    with pytest.raises(ValueError, match="progress"):
        _operation(progress=-math.inf)
    with pytest.raises(ValueError, match="updated_at"):
        _operation(updated_at=_NOW - timedelta(seconds=1))
    with pytest.raises(ValueError, match="timezone-aware"):
        _operation(created_at=datetime(2026, 8, 24, 10, 0))
    plus_eight = timezone(timedelta(hours=8))
    with pytest.raises(ValueError, match="updated_at"):
        _operation(
            created_at=datetime(2026, 8, 24, 10, 0, tzinfo=plus_eight),
            updated_at=datetime(2026, 8, 24, 1, 0, tzinfo=UTC),
        )
    with pytest.raises(ValueError, match="sanitized"):
        _operation(status=OperationStatus.FAILED, error_category="失败")
    with pytest.raises(TypeError):
        ControlResult(
            success=1,  # type: ignore[arg-type]
            resource_id="r1",
            revision=0,
            audit_id="a1",
            effective_state={},
        )


def test_dangerous_capabilities_are_absent_and_web_search_is_legal() -> None:
    assert "web_search" not in CONTROL_CAPABILITY_IDS
    assert DENIED_CAPABILITY_ID == "control.denied"
    assert DENIED_CAPABILITY_ID not in CONTROL_CAPABILITY_IDS
    assert not is_forbidden_control_capability("web_search")
    assert not is_forbidden_control_capability("mcp.web_search")
    assert (
        project_source_capability_id("web_search", source_kind=CatalogSourceKind.MCP_FIXED_TOOLSET)
        == "web_search"
    )
    assert (
        project_source_capability_id(
            "mcp.web_search", source_kind=CatalogSourceKind.MCP_FIXED_TOOLSET
        )
        == "mcp.web_search"
    )
    for capability in DANGEROUS_CAPABILITIES:
        assert capability not in CONTROL_CAPABILITY_IDS
        assert is_forbidden_control_capability(capability)
        for kind in CatalogSourceKind:
            assert project_source_capability_id(capability, source_kind=kind) is None
    projected = project_catalog_capabilities(
        (
            CatalogCapabilityView(
                CatalogSourceKind.ACTION_CATALOG,
                "action:relationship.get:any_user",
            ),
            CatalogCapabilityView(
                CatalogSourceKind.PERMISSION_CATALOG,
                "onebot:call_onebot_api:any_public_action",
            ),
            CatalogCapabilityView(CatalogSourceKind.MCP_FIXED_TOOLSET, "web_search"),
            CatalogCapabilityView(CatalogSourceKind.CONFIG_CATALOG, "config:llm.model"),
        )
    )
    assert projected == (
        "action:relationship.get:any_user",
        "web_search",
        "config:llm.model",
    )
    families = {item.family.value for item in CONTROL_CAPABILITY_DESCRIPTORS}
    assert families == {"identity", "route", "control"}
    assert "identity.binding.read_external" in CONTROL_CAPABILITY_IDS
    assert len(CONTROL_CAPABILITY_IDS) == len(CONTROL_CAPABILITY_DESCRIPTORS)


def test_no_fourth_registry_symbol() -> None:
    for path in _python_files(CONTROL_PLANE_ROOT):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                assert "Registry" not in node.name
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                assert "registry" not in node.name.casefold()
    import qq_ai_bot.control_plane as package
    import qq_ai_bot.control_plane.contracts as contracts

    for module in (package, contracts):
        assert not any("Registry" in name for name in module.__all__)
        assert not any(name.endswith("Registry") for name in dir(module) if name[:1].isupper())


def test_ast_import_guard_and_zero_io_schema() -> None:
    io_calls = {"open", "connect", "urlopen", "run", "Popen"}
    schema_tokens = {"__tablename__", "mapped_column", "Mapped", "Table"}
    for path in _python_files(CONTROL_PLANE_ROOT):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        collector = _ImportCollector()
        collector.visit(tree)
        for module in collector.modules:
            assert not any(_matches(module, prefix) for prefix in FORBIDDEN_IMPORT_PREFIXES), (
                f"{path.name} imports {module}"
            )
        for name in collector.names:
            assert name not in FORBIDDEN_IMPORT_TOKENS
        for token in schema_tokens:
            assert token not in source
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                called = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
                assert called not in io_calls
    assert inspect.isfunction(decide)
    assert "AdminActor" not in (CONTROL_PLANE_ROOT / "principal.py").read_text(encoding="utf-8")


def test_json_aliases_have_no_any_and_page_is_generic() -> None:
    for path in _python_files(CONTROL_PLANE_ROOT):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                assert node.id != "Any"
            if isinstance(node, ast.Attribute):
                assert node.attr != "Any"
    assert Page.__type_params__
    assert Page[str] is not Page[int]


def test_fresh_process_import_stays_on_domain_contracts() -> None:
    env = os.environ.copy()
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = os.pathsep.join(item for item in (str(SRC_ROOT), existing) if item)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import qq_ai_bot.control_plane as cp;"
                "import sys;"
                "mods = sorted("
                "m for m in sys.modules"
                " if m.startswith('qq_ai_bot.') or m.startswith('sqlalchemy')"
                ");"
                "print('\\n'.join(mods));"
                "assert cp.DecisionContext is __import__("
                "'qq_ai_bot.domain.control', fromlist=['DecisionContext']"
                ").DecisionContext"
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        cwd=str(REPO_ROOT),
    )
    assert result.returncode == 0, result.stderr
    loaded = set(result.stdout.splitlines())
    assert "qq_ai_bot.control_plane" in loaded
    assert "qq_ai_bot.domain.control" in loaded
    assert not any(item.startswith("sqlalchemy") for item in loaded)
    assert not any(item.startswith("qq_ai_bot.admin") for item in loaded)
    assert not any(item.startswith("qq_ai_bot.persistence") for item in loaded)
    assert not any(item.startswith("qq_ai_bot.cli") for item in loaded)


def test_finite_json_and_page_snapshot_validation() -> None:
    with pytest.raises(ValueError):
        ControlCommand(request_id=RequestId.new(), expected_revision=0, payload={"n": math.inf})
    with pytest.raises(ValueError):
        Page(("x",), snapshot_at=datetime(2026, 1, 1))
    page = Page(("x",), snapshot_at=_NOW)
    assert page.snapshot_at == _NOW


_DANGEROUS_SEGMENT_FAMILIES = (
    ("onebot", "send_group_msg"),
    ("onebot", "call"),
    ("mcp", "github", "create_issue"),
    ("mcp", "github", "call"),
    ("plugin", "github", "run"),
    ("plugin", "foo", "run"),
    ("plugin", "foo", "call"),
    ("plugin", "foo", "execute"),
    ("plugin", "foo", "invoke"),
    ("control", "plugin", "run"),
    ("control", "plugin", "arbitrary", "execute"),
    ("control", "mcp", "tool", "call"),
    ("control", "mcp", "provider", "invoke"),
    ("sql", "query"),
    ("database", "sql", "execute"),
    ("action", "database", "query"),
    ("action", "database", "execute"),
    ("action", "call_onebot_api_v2", "send"),
    ("secret", "value"),
    ("secret", "reveal"),
)
_REPRODUCED_DANGEROUS_IDS = (
    "mcp.github.create_issue",
    "onebot.send_group_msg",
    "plugin.github.run",
    "sql.query",
    "database.sql.execute",
    "action:database.query",
    "action:database.execute",
    "action:call_onebot_api_v2:send",
    "onebot.call",
    "mcp:github:call",
    "secret.value",
    "secret.reveal",
    "control.plugin.run",
    "control.plugin.arbitrary.execute",
    "control.mcp.tool.call",
    "control.mcp.provider.invoke",
)
_SAFE_UNAPPROVED_MANAGEMENT = (
    "plugin.read",
    "plugin.enable",
    "plugin.disable",
    "plugin.approve",
    "plugin.retry",
    "plugin.unknown_metadata",
    "mcp.server.read",
    "mcp.server.enable",
    "mcp.server.disable",
    "mcp.server.refresh",
    "mcp.server.reconnect",
    "control.plugin.doctor",
    "control.plugin.outbox.read",
    "control.mcp.server.health",
    "control.mcp.server.reconnect",
)


def _expand_capability_variants(parts: tuple[str, ...]) -> frozenset[str]:
    variants: set[str] = set()
    for seps in itertools.product(".:", repeat=len(parts) - 1):
        raw = parts[0] + "".join(sep + part for sep, part in zip(seps, parts[1:], strict=True))
        variants.update({raw, raw.upper(), raw.swapcase(), raw.title(), f"  {raw}  "})
    return frozenset(variants)


def test_generated_dangerous_variants_are_never_projected_or_allowed() -> None:
    principal = _qq_principal()
    context = _context(principal)
    seen = 0
    for family in _DANGEROUS_SEGMENT_FAMILIES:
        variants = _expand_capability_variants(family)
        assert len(variants) > 4
        for raw in variants:
            seen += 1
            folded = raw.strip().casefold()
            assert is_forbidden_control_capability(raw)
            assert is_forbidden_control_capability(folded)
            for kind in CatalogSourceKind:
                assert project_source_capability_id(raw, source_kind=kind) is None
            with pytest.raises(ValueError):
                PolicyDecision.allow(raw)
            with pytest.raises(ValueError):
                _qq_principal(capabilities=(folded,))
            denied = decide(context, raw)
            assert denied.effect is PolicyEffect.DENY
            assert denied.capability == DENIED_CAPABILITY_ID
            assert denied.problem is not None
            assert denied.problem.code is ProblemCode.CAPABILITY_DENIED
            assert folded not in denied.capability
            assert "mcp.github" not in denied.capability
    assert seen >= 40
    for reproduced in _REPRODUCED_DANGEROUS_IDS:
        assert is_forbidden_control_capability(reproduced)
        with pytest.raises(ValueError, match="forbidden"):
            PolicyDecision.allow(reproduced)
        denied = PolicyDecision.deny(reproduced, Problem(ProblemCode.CAPABILITY_DENIED))
        assert denied.capability == DENIED_CAPABILITY_ID


def test_web_search_is_the_only_mcp_exception_and_source_must_match() -> None:
    assert (
        project_source_capability_id("web_search", source_kind=CatalogSourceKind.MCP_FIXED_TOOLSET)
        == "web_search"
    )
    assert (
        project_source_capability_id(
            "  MCP.WEB_SEARCH  ", source_kind=CatalogSourceKind.MCP_FIXED_TOOLSET
        )
        == "mcp.web_search"
    )
    for kind in CatalogSourceKind:
        if kind is CatalogSourceKind.MCP_FIXED_TOOLSET:
            continue
        assert project_source_capability_id("web_search", source_kind=kind) is None
        assert project_source_capability_id("mcp.web_search", source_kind=kind) is None
    assert (
        project_source_capability_id("web_search", source_kind=CatalogSourceKind.PLUGIN_METADATA)
        is None
    )
    for extra in ("web_search.run", "mcp.web_search.call", "mcp.web_search.extra"):
        assert is_forbidden_control_capability(extra)
        assert (
            project_source_capability_id(extra, source_kind=CatalogSourceKind.MCP_FIXED_TOOLSET)
            is None
        )
    PolicyDecision.allow("web_search")
    PolicyDecision.allow("mcp.web_search")


def test_source_kind_mismatch_does_not_project() -> None:
    assert (
        project_source_capability_id(
            "action:relationship.get:any_user",
            source_kind=CatalogSourceKind.CONFIG_CATALOG,
        )
        is None
    )
    assert (
        project_source_capability_id(
            "config:llm.model",
            source_kind=CatalogSourceKind.ACTION_CATALOG,
        )
        is None
    )
    assert (
        project_source_capability_id(
            "identity.person.read",
            source_kind=CatalogSourceKind.ACTION_CATALOG,
        )
        is None
    )
    assert (
        project_source_capability_id(
            "identity.person.read",
            source_kind=CatalogSourceKind.PERMISSION_CATALOG,
        )
        == "identity.person.read"
    )
    mismatched = project_catalog_capabilities(
        (
            CatalogCapabilityView(
                CatalogSourceKind.CONFIG_CATALOG,
                "action:relationship.get:any_user",
            ),
            CatalogCapabilityView(
                CatalogSourceKind.ACTION_CATALOG,
                "mcp.github.create_issue",
            ),
            CatalogCapabilityView(CatalogSourceKind.PLUGIN_METADATA, "config:llm.model"),
        )
    )
    assert mismatched == ()


def test_public_descriptor_constructor_rejects_dangerous_and_inconsistent() -> None:
    ok = ControlCapabilityDescriptor(
        id="identity.person.read",
        family=CapabilityFamily.IDENTITY,
        sensitivity=CapabilitySensitivity.METADATA_READ,
        mutating=False,
    )
    assert ok.id == "identity.person.read"
    future = ControlCapabilityDescriptor(
        id="control.plugin.doctor",
        family=CapabilityFamily.CONTROL,
        sensitivity=CapabilitySensitivity.METADATA_READ,
        mutating=False,
    )
    assert future.id == "control.plugin.doctor"
    assert is_protocol_capability(future.id) is False
    outbox = ControlCapabilityDescriptor(
        id="control.plugin.outbox.read",
        family=CapabilityFamily.CONTROL,
        sensitivity=CapabilitySensitivity.METADATA_READ,
        mutating=False,
    )
    assert outbox.id == "control.plugin.outbox.read"
    assert is_protocol_capability(outbox.id) is False
    health = ControlCapabilityDescriptor(
        id="control.mcp.server.health",
        family=CapabilityFamily.CONTROL,
        sensitivity=CapabilitySensitivity.METADATA_READ,
        mutating=False,
    )
    assert health.id == "control.mcp.server.health"
    reconnect = ControlCapabilityDescriptor(
        id="control.mcp.server.reconnect",
        family=CapabilityFamily.CONTROL,
        sensitivity=CapabilitySensitivity.MUTATE,
        mutating=True,
    )
    assert reconnect.id == "control.mcp.server.reconnect"
    assert is_protocol_capability(reconnect.id) is False
    with pytest.raises(ValueError, match="forbidden"):
        ControlCapabilityDescriptor(
            id="mcp.github.create_issue",
            family=CapabilityFamily.IDENTITY,
            sensitivity=CapabilitySensitivity.MUTATE,
            mutating=True,
        )
    with pytest.raises(ValueError, match="forbidden"):
        ControlCapabilityDescriptor(
            id="control.plugin.run",
            family=CapabilityFamily.CONTROL,
            sensitivity=CapabilitySensitivity.MUTATE,
            mutating=True,
        )
    with pytest.raises(ValueError, match="forbidden"):
        ControlCapabilityDescriptor(
            id="control.plugin.arbitrary.execute",
            family=CapabilityFamily.CONTROL,
            sensitivity=CapabilitySensitivity.MUTATE,
            mutating=True,
        )
    with pytest.raises(ValueError, match="forbidden"):
        ControlCapabilityDescriptor(
            id="control.mcp.tool.call",
            family=CapabilityFamily.CONTROL,
            sensitivity=CapabilitySensitivity.MUTATE,
            mutating=True,
        )
    with pytest.raises(ValueError, match="forbidden"):
        ControlCapabilityDescriptor(
            id="control.mcp.provider.invoke",
            family=CapabilityFamily.CONTROL,
            sensitivity=CapabilitySensitivity.MUTATE,
            mutating=True,
        )
    with pytest.raises(ValueError, match="family"):
        ControlCapabilityDescriptor(
            id="identity.person.read",
            family=CapabilityFamily.ROUTE,
            sensitivity=CapabilitySensitivity.METADATA_READ,
            mutating=False,
        )
    with pytest.raises(ValueError, match="sensitivity"):
        ControlCapabilityDescriptor(
            id="identity.person.read",
            family=CapabilityFamily.IDENTITY,
            sensitivity=CapabilitySensitivity.METADATA_READ,
            mutating=True,
        )
    with pytest.raises(ValueError, match="sensitivity"):
        ControlCapabilityDescriptor(
            id="identity.person.enable",
            family=CapabilityFamily.IDENTITY,
            sensitivity=CapabilitySensitivity.MUTATE,
            mutating=False,
        )
    with pytest.raises(TypeError):
        ControlCapabilityDescriptor(
            id="identity.person.read",
            family=CapabilityFamily.IDENTITY,
            sensitivity=CapabilitySensitivity.METADATA_READ,
            mutating=1,  # type: ignore[arg-type]
        )


def test_result_tokens_reject_control_characters_and_overlong() -> None:
    with pytest.raises(ValueError):
        ControlResult(
            success=True,
            resource_id="id\nleak",
            revision=0,
            audit_id="audit-1",
            effective_state={},
        )
    with pytest.raises(ValueError):
        ControlResult(
            success=True,
            resource_id="ok",
            revision=0,
            audit_id="x" * 129,
            effective_state={},
        )
    with pytest.raises(ValueError):
        ControlResult(
            success=True,
            resource_id="用户原文",
            revision=0,
            audit_id="audit-1",
            effective_state={},
        )


def test_safe_management_is_unapproved_until_descriptor_table() -> None:
    principal = _qq_principal()
    context = _context(principal)
    for capability in _SAFE_UNAPPROVED_MANAGEMENT:
        assert is_forbidden_control_capability(capability) is False
        assert is_protocol_capability(capability) is False
        for kind in CatalogSourceKind:
            assert project_source_capability_id(capability, source_kind=kind) is None
        with pytest.raises(ValueError):
            PolicyDecision.allow(capability)
        with pytest.raises(ValueError):
            _qq_principal(capabilities=(capability,))
        denied = decide(context, capability)
        assert denied.effect is PolicyEffect.DENY
        assert denied.capability == DENIED_CAPABILITY_ID
        assert denied.problem is not None
        assert denied.problem.code is ProblemCode.CAPABILITY_DENIED


def test_plugin_metadata_cannot_self_report_run_or_web_search() -> None:
    assert (
        project_source_capability_id("plugin.run", source_kind=CatalogSourceKind.PLUGIN_METADATA)
        is None
    )
    assert (
        project_source_capability_id("web_search", source_kind=CatalogSourceKind.PLUGIN_METADATA)
        is None
    )
    assert is_forbidden_control_capability("plugin.run")
    assert is_forbidden_control_capability("plugin.foo.invoke")
    assert is_forbidden_control_capability("mcp.tool.call")
    assert (
        project_catalog_capabilities(
            (
                CatalogCapabilityView(CatalogSourceKind.PLUGIN_METADATA, "plugin.run"),
                CatalogCapabilityView(CatalogSourceKind.PLUGIN_METADATA, "web_search"),
                CatalogCapabilityView(CatalogSourceKind.PLUGIN_METADATA, "plugin.read"),
            )
        )
        == ()
    )


def test_action_catalog_nested_sql_and_onebot_variants_are_forbidden() -> None:
    for capability in (
        "action:database.query",
        "action:database.execute",
        "action:call_onebot_api_v2:send",
        "ACTION:DATABASE.QUERY",
        "action.database.execute",
        "action:call_onebot_api_v2.send",
    ):
        assert is_forbidden_control_capability(capability)
        assert is_protocol_capability(capability) is False
        assert (
            project_source_capability_id(capability, source_kind=CatalogSourceKind.ACTION_CATALOG)
            is None
        )


def test_paginate_rejects_empty_or_control_sort_keys_even_for_one_page() -> None:
    request = PageRequest(limit=20)
    with pytest.raises(ValueError):
        paginate(("only",), request, sort_key=lambda item: "")
    with pytest.raises(ValueError):
        paginate(("only",), request, sort_key=lambda item: "bad\nkey")
    page = paginate(("only",), request, sort_key=lambda item: "only")
    assert page.items == ("only",)
    assert page.next_cursor is None


def test_datetime_requires_real_utcoffset() -> None:
    class _OffsetlessTz(tzinfo):
        def utcoffset(self, dt: datetime | None) -> timedelta | None:
            return None

        def dst(self, dt: datetime | None) -> timedelta | None:
            return None

        def tzname(self, dt: datetime | None) -> str:
            return "offsetless"

    with pytest.raises(ValueError, match="timezone-aware"):
        _operation(created_at=datetime(2026, 8, 24, 10, 0, tzinfo=_OffsetlessTz()))
    with pytest.raises(ValueError, match="timezone-aware"):
        Page(("x",), snapshot_at=datetime(2026, 8, 24, 10, 0, tzinfo=_OffsetlessTz()))
