"""C0 read-only freeze of the permanent-yuki migration current-state contract.

These tests inspect public objects and application source. They must not change
production behavior and must keep the current legacy architecture green.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import get_type_hints

import pytest
from tests.conftest import make_settings

from qq_ai_bot.admin.action_service import TargetResolver
from qq_ai_bot.admin.config_registry import ConfigRegistry
from qq_ai_bot.admin.config_service import RuntimeConfigService
from qq_ai_bot.admin.models import AdminActor, ConfigApplyMode
from qq_ai_bot.admin.permission_catalog import CapabilityKind, PermissionCatalogService
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import InboundMessage, SenderIdentity
from qq_ai_bot.health import HealthPayload
from qq_ai_bot.memory.repository import MemoryFactRepository
from qq_ai_bot.memory.service import MemoryFactService
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.metadata import Base
from qq_ai_bot.persistence.repositories import AgentActionRepository, EventLedgerRepository
from qq_ai_bot.services.agent_tools import AgentToolService, ToolRuntime

SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "qq_ai_bot"
MIGRATIONS_ROOT = Path(__file__).resolve().parents[2] / "migrations" / "versions"
CONTROL_PLANE_ROOT = SRC_ROOT / "control_plane"

FRAMEWORK_HTTP_PATHS = frozenset(
    {
        "/docs",
        "/docs/",
        "/redoc",
        "/redoc/",
        "/openapi.json",
        "/docs/oauth2-redirect",
    }
)
SECRET_HEALTH_TOKENS = (
    "api_key",
    "access_token",
    "password",
    "secret",
    "cookie",
    "authorization",
    "private_key",
    "webui_token",
    "login_credential",
)
FORBIDDEN_PERSISTENCE_TABLES = frozenset(
    {
        "yuki",
        "yukis",
        "yuki_self",
        "yuki_selves",
        "yukiself",
        "gateway_connection",
        "gateway_connections",
        "presence_active_route",
        "presence_active_routes",
        "delivery_route",
        "delivery_routes",
    }
)
FORBIDDEN_PERSISTENCE_TYPES = frozenset(
    {
        "Yuki",
        "YukiSelf",
        "YukiModel",
        "YukiSelfModel",
        "GatewayConnection",
        "GatewayConnectionModel",
        "DeliveryRoute",
        "DeliveryRouteModel",
        "PresenceActiveRoute",
        "PresenceActiveRouteModel",
    }
)
CONTROL_PLANE_TRANSPORT_PREFIXES = (
    "qq_ai_bot.cli",
    "qq_ai_bot.plugins.ai_chat.matcher",
    "nonebot.matcher",
    "qq_ai_bot.services.command_service",
    "qq_ai_bot.services.config_commands",
)
FROZEN_HEALTH_KEYS = frozenset(get_type_hints(HealthPayload))


def _python_files(root: Path) -> tuple[Path, ...]:
    if not root.exists():
        return ()
    return tuple(sorted(path for path in root.rglob("*.py") if path.is_file()))


def _constant_str(node: ast.expr) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _call_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _keyword_value(node: ast.Call, name: str) -> ast.expr | None:
    for keyword in node.keywords:
        if keyword.arg == name:
            return keyword.value
    return None


def _string_seq(node: ast.expr | None) -> tuple[str, ...]:
    if node is None:
        return ()
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return (node.value,)
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        values = [_constant_str(item) for item in node.elts]
        return tuple(value for value in values if value is not None)
    return ()


def _matches(module: str, prefix: str) -> bool:
    return module == prefix or module.startswith(prefix + ".")


class _ImportCollector(ast.NodeVisitor):
    def __init__(self, module_package: str) -> None:
        self.records: list[tuple[str, int, tuple[str, ...]]] = []
        self._package = module_package
        self._type_checking_depth = 0

    @staticmethod
    def _is_type_checking_test(test: ast.expr) -> bool:
        if isinstance(test, ast.Name) and test.id == "TYPE_CHECKING":
            return True
        return isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"

    def _record(self, module: str, lineno: int, names: tuple[str, ...] = ()) -> None:
        if module:
            self.records.append((module, lineno, names))

    def visit_If(self, node: ast.If) -> None:
        if self._is_type_checking_test(node.test):
            self._type_checking_depth += 1
            for child in node.body:
                self.visit(child)
            self._type_checking_depth -= 1
            for child in node.orelse:
                self.visit(child)
            return
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self._record(alias.name, node.lineno, (alias.name.split(".")[-1],))

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        names = tuple(alias.name for alias in node.names)
        if node.level == 0:
            self._record(node.module or "", node.lineno, names)
            return
        base_parts = self._package.split(".")
        anchor = base_parts[: len(base_parts) - (node.level - 1)]
        if node.module:
            anchor = [*anchor, *node.module.split(".")]
        self._record(".".join(anchor), node.lineno, names)

    def visit_Call(self, node: ast.Call) -> None:
        target: str | None = None
        if isinstance(node.func, ast.Attribute) and node.func.attr == "import_module":
            target = _first_string_argument(node)
        elif isinstance(node.func, ast.Name) and node.func.id == "__import__":
            target = _first_string_argument(node)
        if target is not None:
            self._record(target, node.lineno)
        self.generic_visit(node)


def _first_string_argument(node: ast.Call) -> str | None:
    if node.args and isinstance(node.args[0], ast.Constant):
        value = node.args[0].value
        if isinstance(value, str):
            return value
    return None


def _module_package(path: Path, root: Path) -> str:
    relative = path.relative_to(root.parent)
    parts = list(relative.with_suffix("").parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    else:
        parts = parts[:-1]
    return ".".join(parts)


def _scan_imports(path: Path, root: Path) -> tuple[tuple[str, int, tuple[str, ...]], ...]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    collector = _ImportCollector(_module_package(path, root))
    collector.visit(tree)
    return tuple(collector.records)


def _module_name(path: Path) -> str:
    relative = path.relative_to(SRC_ROOT.parent)
    parts = list(relative.with_suffix("").parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _class_defines_tablename(node: ast.ClassDef) -> bool:
    for item in node.body:
        if isinstance(item, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "__tablename__" for target in item.targets
        ):
            return True
        if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
            if item.target.id == "__tablename__":
                return True
    return False


def _current_orm_model_modules() -> frozenset[str]:
    modules: set[str] = set()
    for path in _python_files(SRC_ROOT):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if any(
            isinstance(node, ast.ClassDef) and _class_defines_tablename(node) for node in tree.body
        ):
            modules.add(_module_name(path))
    return frozenset(modules)


def _fully_qualified_import_candidates(module: str, names: tuple[str, ...]) -> tuple[str, ...]:
    """Expand one AST import into module and each ``module.name`` candidate."""

    candidates: list[str] = []
    if module:
        candidates.append(module)
    last_segment = module.rsplit(".", 1)[-1] if module else ""
    for name in names:
        if not name or name == "*":
            continue
        if module and name in {module, last_segment}:
            continue
        candidates.append(f"{module}.{name}" if module else name)
    return tuple(dict.fromkeys(candidates))


def _control_plane_forbidden_prefixes(orm_modules: frozenset[str]) -> tuple[str, ...]:
    return (
        *CONTROL_PLANE_TRANSPORT_PREFIXES,
        *sorted(orm_modules),
        "qq_ai_bot.persistence.metadata",
        "sqlalchemy",
    )


def _forbidden_import_hits(
    candidates: tuple[str, ...],
    orm_modules: frozenset[str],
) -> tuple[str, ...]:
    prefixes = _control_plane_forbidden_prefixes(orm_modules)
    return tuple(
        candidate
        for candidate in candidates
        if any(_matches(candidate, prefix) for prefix in prefixes)
    )


def _classify_control_plane_imports(
    source: str,
    *,
    package: str = "qq_ai_bot.control_plane",
    orm_modules: frozenset[str] | None = None,
) -> tuple[tuple[int, str, tuple[str, ...], tuple[str, ...]], ...]:
    """Run the AST collector and classifier on an in-memory snippet."""

    selected_orm = _current_orm_model_modules() if orm_modules is None else orm_modules
    collector = _ImportCollector(package)
    collector.visit(ast.parse(source))
    findings: list[tuple[int, str, tuple[str, ...], tuple[str, ...]]] = []
    for module, lineno, names in collector.records:
        candidates = _fully_qualified_import_candidates(module, names)
        hits = _forbidden_import_hits(candidates, selected_orm)
        findings.append((lineno, module, candidates, hits))
    return tuple(findings)


class _RouteCollector(ast.NodeVisitor):
    def __init__(self) -> None:
        self.http_routes: list[tuple[str, tuple[str, ...], int]] = []

    def visit_Call(self, node: ast.Call) -> None:
        name = _call_name(node.func)
        if name == "add_api_route":
            path = _constant_str(node.args[0]) if node.args else None
            methods = _string_seq(_keyword_value(node, "methods"))
            if path is not None:
                self.http_routes.append((path, methods or ("GET",), node.lineno))
        elif name == "include_router":
            prefix = _constant_str(_keyword_value(node, "prefix")) or ""
            self.http_routes.append((prefix or "<included-router>", ("*",), node.lineno))
        elif name == "APIRouter":
            prefix = _constant_str(_keyword_value(node, "prefix"))
            if prefix:
                self.http_routes.append((prefix, ("*",), node.lineno))
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_decorated(node)
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_decorated(node)
        self.generic_visit(node)

    def _visit_decorated(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        http_receivers = {"app", "router", "api", "server_app"}
        http_verbs = {"get", "post", "put", "patch", "delete", "head", "options", "api_route"}
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call) or not isinstance(decorator.func, ast.Attribute):
                continue
            if decorator.func.attr not in http_verbs:
                continue
            receiver = decorator.func.value
            receiver_name = receiver.id if isinstance(receiver, ast.Name) else _call_name(receiver)
            if receiver_name not in http_receivers:
                continue
            path = _constant_str(decorator.args[0]) if decorator.args else None
            if path is None:
                continue
            methods = _string_seq(_keyword_value(decorator, "methods"))
            self.http_routes.append(
                (path, methods or (decorator.func.attr.upper(),), decorator.lineno)
            )


def _application_http_routes() -> list[tuple[str, Path, tuple[str, ...], int]]:
    routes: list[tuple[str, Path, tuple[str, ...], int]] = []
    for path in _python_files(SRC_ROOT):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        collector = _RouteCollector()
        collector.visit(tree)
        for route_path, methods, lineno in collector.http_routes:
            routes.append((route_path, path, methods, lineno))
    return routes


def _application_owned_http_routes() -> list[tuple[str, Path, tuple[str, ...], int]]:
    return [route for route in _application_http_routes() if route[0] not in FRAMEWORK_HTTP_PATHS]


def _actor(
    user_id: str = "9000",
    *,
    text: str = "",
    group_id: str | None = None,
    mentions: tuple[str, ...] = (),
) -> AdminActor:
    return AdminActor(
        user_id=user_id,
        is_superuser=user_id == "9000",
        trigger_message_id="c0-admin",
        conversation_key=f"group:{group_id}" if group_id else f"private:{user_id}",
        current_group_id=group_id,
        mentioned_user_ids=mentions,
        current_message_text=text,
    )


def _inbound(user_id: str = "9000") -> InboundMessage:
    return InboundMessage(
        message_id="c0-onebot",
        event_type="message",
        scope_type=ScopeType.PRIVATE,
        sender=SenderIdentity(user_id=user_id),
        text="任意 raw OneBot",
        bot_user_id="7777",
    )


def _contains_superuser_guard(node: ast.AST) -> bool:
    for child in ast.walk(node):
        if isinstance(child, ast.Attribute) and child.attr == "superusers":
            return True
        if isinstance(child, ast.Name) and child.id in {"superusers", "superuser"}:
            return True
    return False


def test_healthz_is_the_only_application_fastapi_route_and_stays_thin() -> None:
    routes = _application_owned_http_routes()
    assert routes, "application FastAPI surface disappeared"
    details = "\n".join(
        f"  {path.relative_to(SRC_ROOT.parent)}:{lineno} {methods} {route_path}"
        for route_path, path, methods, lineno in routes
    )
    assert all(route[0] == "/healthz" for route in routes), (
        f"application FastAPI routes beyond /healthz and framework docs/openapi:\n{details}"
    )
    assert all("GET" in route[2] for route in routes)
    assert FROZEN_HEALTH_KEYS == {
        "status",
        "version",
        "database",
        "llm_configured",
        "web_configured",
        "vision_configured",
        "onebot_connected",
        "automation_enabled",
        "automation_worker_running",
        "active_automation_count",
        "plugin_system_enabled",
        "plugin_running_count",
        "emoji_enabled",
        "emoji_worker_running",
        "emoji_asset_count",
        "emoji_pending_jobs",
        "speech_enabled",
        "speech_worker_connected",
        "speech_worker_ready",
        "speech_japanese_frontend_available",
        "speech_default_profile_loaded",
        "speech_can_send_record",
        "speech_queue_depth",
        "mcp_enabled",
        "mcp_configured_servers",
        "mcp_connected_servers",
        "mcp_cached_tools",
        "mcp_automation_tools",
        "mcp_automation_missing_tools",
        "mcp_active_calls",
        "memory_embedding_enabled",
        "memory_embedding_configured",
        "memory_embedding_coverage",
        "memory_embedding_pending_jobs",
        "memory_embedding_failed_jobs",
        "memory_maintenance_running",
        "memory_contested_facts",
        "memory_active_contested_facts",
        "memory_consistency_healthy",
        "memory_expired_active_facts",
        "memory_classifier_recent_errors",
        "memory_maintenance_last_success_at",
        "memory_rebuild",
        "memory_self_reflection",
        "memory_dream",
        "conversation_rollup",
        "conversation_generation_superseded_effects",
        "conversation_prefix_shape_match_total",
        "conversation_prefix_shape_split_total",
        "uptime_seconds",
    }
    lowered = {key.casefold() for key in FROZEN_HEALTH_KEYS}
    leaked = [token for token in SECRET_HEALTH_TOKENS if any(token in key for key in lowered)]
    assert leaked == []
    hints = get_type_hints(HealthPayload)
    assert hints["status"] is str
    assert hints["version"] is str
    assert hints["database"] is str


def test_target_resolver_requires_current_qq_event_proof() -> None:
    current = _actor(
        text="把 @张三 的好感度降低 5，不要动别人",
        group_id="2001",
        mentions=("12345678",),
    )
    assert (
        TargetResolver.user({"target": "mentioned_user", "user_id": "12345678"}, current)
        == "12345678"
    )
    assert TargetResolver.user({"target": "self"}, current) == "9000"
    assert TargetResolver.group({"target": "current_group"}, current) == "2001"
    with pytest.raises(ValueError):
        TargetResolver.user({"target": "explicit_user_id", "user_id": "87654321"}, current)
    with pytest.raises(ValueError):
        TargetResolver.user({"target": "mentioned_user", "user_id": "87654321"}, current)
    with pytest.raises(ValueError):
        TargetResolver.group({"target": "explicit_group_id", "group_id": "9999"}, current)
    with pytest.raises(ValueError):
        TargetResolver.user({"target": "arbitrary_user", "user_id": "12345678"}, current)
    private = _actor(text="查一下")
    with pytest.raises(ValueError):
        TargetResolver.group({"target": "current_group"}, private)
    with pytest.raises(ValueError):
        TargetResolver.config_scope("group", "9999", current)
    assert TargetResolver.config_scope("user", "self", current) == ("user", "9000")
    with pytest.raises(ValueError):
        TargetResolver.config_scope("user", "87654321", current)


@pytest.mark.asyncio
async def test_secrets_are_unread_and_superusers_are_not_control_mutable(
    database: Database,
) -> None:
    registry = ConfigRegistry()
    superusers = registry.get("superusers")
    assert superusers.apply_mode is ConfigApplyMode.IMMUTABLE
    assert superusers.sensitive
    assert superusers.mutable is False
    secret_specs = tuple(
        spec for spec in registry.list() if spec.apply_mode is ConfigApplyMode.SECRET
    )
    assert secret_specs
    assert all(spec.sensitive and spec.mutable is False for spec in secret_specs)

    settings = make_settings(
        database.url,
        llm_api_key="c0-llm-secret",
        vision_api_key="c0-vision-secret",
        onebot_access_token="c0-onebot-secret",
    )
    service = RuntimeConfigService(settings=settings, database=database)
    for key in ("superusers", "llm.api_key", "vision.api_key", "onebot.access_token"):
        effective = await service.get_effective(key)
        assert effective.value is None
        rejected = await service.set_override(
            key,
            "should-never-write",
            scope_type="global",
            scope_id="",
            actor_user_id="9000",
            trigger_message_id=f"c0-{key}",
        )
        assert rejected.success is False
        assert rejected.error_category == "permission_denied"

    report = PermissionCatalogService(
        settings=settings, config_registry=registry
    ).report_for_message(_inbound())
    payload = report.to_dict()
    secret_descriptors = payload["groups"]["secret"][CapabilityKind.CONFIGURATION.value]
    assert secret_descriptors
    assert all("value" not in descriptor for descriptor in secret_descriptors)
    rendered = json.dumps(payload, ensure_ascii=False)
    assert "c0-llm-secret" not in rendered
    assert "c0-vision-secret" not in rendered
    assert "c0-onebot-secret" not in rendered


def test_generic_onebot_is_only_exposed_on_event_bound_superuser_chat() -> None:
    assignments: list[tuple[Path, int, bool]] = []
    for path in _python_files(SRC_ROOT):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.keyword) and node.arg == "allow_generic_onebot":
                value = node.value
                if isinstance(value, ast.Constant) and value.value is False:
                    assignments.append((path, node.lineno, False))
                    continue
                if isinstance(value, ast.Constant) and value.value is True:
                    raise AssertionError(
                        f"{path.relative_to(SRC_ROOT.parent)}:{node.lineno} hard-enables raw OneBot"
                    )
                assert path == SRC_ROOT / "services" / "chat.py", (
                    f"raw OneBot enablement escaped the event-bound chat path: "
                    f"{path.relative_to(SRC_ROOT.parent)}:{node.lineno}"
                )
                assert _contains_superuser_guard(value)
                assignments.append((path, node.lineno, True))
    assert any(enabled for _path, _lineno, enabled in assignments)
    assert any(not enabled for _path, _lineno, enabled in assignments)


@pytest.mark.asyncio
async def test_call_onebot_api_stays_on_existing_gated_tool_path(database: Database) -> None:
    settings = make_settings(database.url)
    tools = AgentToolService(
        settings=settings,
        ledger=EventLedgerRepository(database),
        memories=MemoryFactService(MemoryFactRepository(database)),
        actions=AgentActionRepository(database),
    )
    runtime = ToolRuntime(
        inbound=_inbound("9000"),
        gateway=None,
        allow_generic_onebot=False,
        actor_user_id="9000",
        actor_is_superuser=True,
    )
    assert "call_onebot_api" not in {tool.name for tool in tools.definitions(runtime)}
    denied = json.loads(
        await tools.execute(
            "call_onebot_api",
            json.dumps({"action": "get_status", "params": {}}),
            runtime,
        )
    )
    assert denied["ok"] is False
    assert denied["error"] == "permission_denied"


def test_future_control_plane_must_not_import_transport_or_orm() -> None:
    orm_modules = _current_orm_model_modules()
    assert {
        "qq_ai_bot.persistence.models",
        "qq_ai_bot.conversation.db_models",
        "qq_ai_bot.conversation.rollup.db_models",
        "qq_ai_bot.plugin_host.db_models",
        "qq_ai_bot.emoji.db_models",
        "qq_ai_bot.speech.db_models",
        "qq_ai_bot.memory.dream.db_models",
        "qq_ai_bot.model_runtime.db_models",
    } <= orm_modules
    if not CONTROL_PLANE_ROOT.exists():
        return
    violations: list[str] = []
    for path in _python_files(CONTROL_PLANE_ROOT):
        for module, lineno, names in _scan_imports(path, SRC_ROOT):
            candidates = _fully_qualified_import_candidates(module, names)
            hits = _forbidden_import_hits(candidates, orm_modules)
            if hits:
                rel = path.relative_to(SRC_ROOT.parent)
                violations.append(
                    f"{rel}:{lineno} imports {module} hits={hits!r} candidates={candidates!r}"
                )
    assert not violations, (
        "control_plane must keep Query Port/DTO pure; ORM adapters stay outside:\n"
        + "\n".join(violations)
    )


def test_control_plane_import_classifier_rejects_parent_and_sqlalchemy_bypasses() -> None:
    orm_modules = _current_orm_model_modules()
    cases = (
        ("import qq_ai_bot.cli", "qq_ai_bot.cli"),
        ("from qq_ai_bot import cli", "qq_ai_bot.cli"),
        ("from qq_ai_bot.services import command_service", "qq_ai_bot.services.command_service"),
        ("from qq_ai_bot.plugins.ai_chat import matcher", "qq_ai_bot.plugins.ai_chat.matcher"),
        ("from qq_ai_bot.persistence import models", "qq_ai_bot.persistence.models"),
        ("from qq_ai_bot.conversation import db_models", "qq_ai_bot.conversation.db_models"),
        ("import sqlalchemy", "sqlalchemy"),
        ("from sqlalchemy import select", "sqlalchemy"),
        ("from sqlalchemy.orm import Mapped", "sqlalchemy"),
        ("from qq_ai_bot.persistence import metadata", "qq_ai_bot.persistence.metadata"),
        ("from .. import cli", "qq_ai_bot.cli"),
    )
    for source, expected in cases:
        findings = _classify_control_plane_imports(source, orm_modules=orm_modules)
        hits = tuple(
            hit for _lineno, _module, _candidates, row_hits in findings for hit in row_hits
        )
        assert hits, f"classifier missed {source!r}; findings={findings!r}"
        assert any(_matches(hit, expected) for hit in hits), (
            f"{source!r} should hit {expected}; hits={hits!r} findings={findings!r}"
        )


def test_control_plane_import_classifier_allows_pure_contracts() -> None:
    source = """
import json
from dataclasses import dataclass
from qq_ai_bot.domain.conversations import ConversationScope
from qq_ai_bot.control_plane.contracts import PageRequest, ControlResult
"""
    findings = _classify_control_plane_imports(source)
    leaked = [
        f"line {lineno} imports {module} hits={hits!r} candidates={candidates!r}"
        for lineno, module, candidates, hits in findings
        if hits
    ]
    assert leaked == []
    imported = {module for _lineno, module, _candidates, _hits in findings}
    assert {
        "json",
        "dataclasses",
        "qq_ai_bot.domain.conversations",
        "qq_ai_bot.control_plane.contracts",
    } <= imported


def test_forbidden_identity_and_management_symbols_are_absent_from_current_persistence() -> None:
    tables = {name.casefold() for name in Base.metadata.tables}
    present = sorted(tables & FORBIDDEN_PERSISTENCE_TABLES)
    assert present == []

    persisted_types: list[str] = []
    for path in _python_files(SRC_ROOT):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:
            if not isinstance(node, ast.ClassDef) or node.name not in FORBIDDEN_PERSISTENCE_TYPES:
                continue
            assigned = False
            for item in node.body:
                if isinstance(item, ast.Assign) and any(
                    isinstance(target, ast.Name) and target.id == "__tablename__"
                    for target in item.targets
                ):
                    assigned = True
                if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                    if item.target.id == "__tablename__":
                        assigned = True
            if assigned:
                persisted_types.append(
                    f"{path.relative_to(SRC_ROOT.parent)}:{node.lineno} {node.name}"
                )
    assert persisted_types == []

    created: list[str] = []
    for path in _python_files(MIGRATIONS_ROOT):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or _call_name(node.func) != "create_table":
                continue
            table = _constant_str(node.args[0]) if node.args else None
            if table and table.casefold() in FORBIDDEN_PERSISTENCE_TABLES:
                created.append(f"{path.name}:{node.lineno} {table}")
    assert created == []

    management = [
        f"{path.relative_to(SRC_ROOT.parent)}:{lineno} {methods} {route_path}"
        for route_path, path, methods, lineno in _application_owned_http_routes()
        if route_path != "/healthz"
    ]
    assert management == []
