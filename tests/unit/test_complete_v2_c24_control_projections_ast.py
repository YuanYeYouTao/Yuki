"""AST gate: C24c control projections stay transport-neutral and fail closed."""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

from qq_ai_bot.control_plane.query_port import ControlQueryPort
from qq_ai_bot.control_plane.query_service import ControlQueryService

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src" / "qq_ai_bot"
QUERY_TYPES = SRC_ROOT / "control_plane" / "query_types.py"
QUERY_PORT = SRC_ROOT / "control_plane" / "query_port.py"
QUERY_SERVICE = SRC_ROOT / "control_plane" / "query_service.py"
ADAPTER = SRC_ROOT / "persistence" / "control_query.py"
CONTROL_PLANE_ROOT = SRC_ROOT / "control_plane"

_PERSON_CAP = "identity.person.read"
_SPACE_CAP = "identity.space.read"
_LEGACY_REVEAL = "reveal_first_seen"
_REVEAL_PERSON = "reveal_first_seen_person"
_REVEAL_SPACE = "reveal_first_seen_space"

_FORBIDDEN_MODULES = (
    "sqlalchemy",
    "qq_ai_bot.persistence",
    "qq_ai_bot.emoji.db_models",
    "qq_ai_bot.identity.db_models",
)
_RAW_LEAK_ATTRS = frozenset(
    {
        "scope_id",
        "first_seen_user_id",
        "first_seen_group_id",
        "actor_user_id",
        "group_id",
        "updated_by",
    }
)
_OWNER_SINKS = frozenset(
    {
        "person_id",
        "space_id",
        "first_seen_person_id",
        "first_seen_space_id",
        "legacy_owner",
    }
)
_PROJECTION_FNS = frozenset(
    {
        "_config_owner_projection",
        "_project_config_override",
        "_project_emoji_asset",
        "_project_emoji_space_enablement",
        "list_config_overrides",
        "list_emoji_assets",
        "list_effective_configs",
    }
)


def _attr_name(node: ast.expr | None) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def test_control_plane_query_types_stay_free_of_orm() -> None:
    for path in (QUERY_TYPES, QUERY_PORT, QUERY_SERVICE):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith("sqlalchemy")
                    assert alias.name not in _FORBIDDEN_MODULES
            if isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith("sqlalchemy")
                assert not any(node.module.startswith(item) for item in _FORBIDDEN_MODULES)


def test_control_plane_package_has_no_http_or_orm() -> None:
    for path in CONTROL_PLANE_ROOT.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith("sqlalchemy")
                assert not node.module.startswith("fastapi")
                assert not node.module.startswith("starlette")
            if isinstance(node, ast.Name):
                assert node.id not in {"APIRouter", "FastAPI", "Request"}


def test_adapter_projections_do_not_assign_raw_ids_to_owner_sinks() -> None:
    tree = ast.parse(ADAPTER.read_text(encoding="utf-8"), filename=str(ADAPTER))
    hits: list[tuple[str, int]] = []

    class _Visitor(ast.NodeVisitor):
        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self._scan(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self._scan(node)

        def _scan(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
            if node.name not in _PROJECTION_FNS:
                self.generic_visit(node)
                return
            for child in ast.walk(node):
                if isinstance(child, ast.keyword) and child.arg in _OWNER_SINKS:
                    if _uses_raw_attr(child.value):
                        hits.append((node.name, child.lineno))
                if isinstance(child, ast.Call) and _attr_name(child.func) in {
                    "PersonId.parse",
                    "SpaceId.parse",
                    "_try_person_id",
                    "_try_space_id",
                    "mask_external_id",
                }:
                    if child.func.__class__.__name__ == "Attribute" and child.func.attr in {
                        "parse",
                    }:
                        pass
                    if any(_uses_raw_attr(arg) for arg in child.args):
                        if _attr_name(child.func) in {"_try_person_id", "_try_space_id"}:
                            hits.append((node.name, child.lineno))
                        if _attr_name(child.func) == "parse" and _uses_raw_attr(
                            child.args[0] if child.args else child
                        ):
                            hits.append((node.name, child.lineno))
            self.generic_visit(node)

    _Visitor().visit(tree)
    assert hits == []


def _uses_raw_attr(node: ast.AST) -> bool:
    for child in ast.walk(node):
        if isinstance(child, ast.Attribute) and child.attr in _RAW_LEAK_ATTRS:
            return True
        if isinstance(child, ast.Name) and child.id in _RAW_LEAK_ATTRS:
            return True
    return False


def test_adapter_never_reads_usage_or_first_seen_raw_columns_for_projection() -> None:
    source = ADAPTER.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(ADAPTER))
    names = {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and node.attr
        in {"first_seen_user_id", "first_seen_group_id", "actor_user_id", "EmojiUsageEventModel"}
    }
    assert names == set()
    assert "EmojiUsageEventModel" not in source
    assert ".offset(" not in source


def _function_named(tree: ast.AST, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} missing")


def _kwonly_names(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    return {arg.arg for arg in fn.args.kwonlyargs}


def _positional_names(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    return [arg.arg for arg in fn.args.args]


def _capability_literals(node: ast.AST) -> set[str]:
    found: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Constant) and child.value in {_PERSON_CAP, _SPACE_CAP}:
            found.add(str(child.value))
    return found


def _legacy_reveal_names(node: ast.AST) -> list[str]:
    found: list[str] = []
    for child in ast.walk(node):
        if isinstance(child, ast.Name) and child.id == _LEGACY_REVEAL:
            found.append(child.id)
        if isinstance(child, ast.arg) and child.arg == _LEGACY_REVEAL:
            found.append(child.arg)
        if isinstance(child, ast.keyword) and child.arg == _LEGACY_REVEAL:
            found.append(child.arg)
    return found


def test_control_query_service_list_emoji_assets_public_signature_is_unchanged() -> None:
    signature = inspect.signature(ControlQueryService.list_emoji_assets)
    assert list(signature.parameters) == ["self", "context", "request"]
    port = inspect.signature(ControlQueryPort.list_emoji_assets)
    assert _REVEAL_PERSON in port.parameters
    assert _REVEAL_SPACE in port.parameters
    assert _LEGACY_REVEAL not in port.parameters


def test_first_seen_reveal_flags_are_independent_in_port_service_and_adapter() -> None:
    port_fn = _function_named(
        ast.parse(QUERY_PORT.read_text(encoding="utf-8"), filename=str(QUERY_PORT)),
        "list_emoji_assets",
    )
    service_fn = _function_named(
        ast.parse(QUERY_SERVICE.read_text(encoding="utf-8"), filename=str(QUERY_SERVICE)),
        "list_emoji_assets",
    )
    adapter_tree = ast.parse(ADAPTER.read_text(encoding="utf-8"), filename=str(ADAPTER))
    adapter_fn = _function_named(adapter_tree, "list_emoji_assets")
    project_fn = _function_named(adapter_tree, "_project_emoji_asset")

    for fn in (port_fn, adapter_fn, project_fn):
        assert _REVEAL_PERSON in _kwonly_names(fn)
        assert _REVEAL_SPACE in _kwonly_names(fn)
        assert _LEGACY_REVEAL not in _kwonly_names(fn)
        assert _legacy_reveal_names(fn) == []

    assert _positional_names(service_fn) == ["self", "context", "request"]
    assert _REVEAL_PERSON not in _kwonly_names(service_fn)
    assert _REVEAL_SPACE not in _kwonly_names(service_fn)
    assert _legacy_reveal_names(service_fn) == []

    port_calls = [
        node
        for node in ast.walk(service_fn)
        if isinstance(node, ast.Call) and _attr_name(node.func) == "list_emoji_assets"
    ]
    assert len(port_calls) == 1
    keywords = {item.arg: item.value for item in port_calls[0].keywords}
    assert set(keywords) >= {_REVEAL_PERSON, _REVEAL_SPACE}
    assert _capability_literals(keywords[_REVEAL_PERSON]) == {_PERSON_CAP}
    assert _capability_literals(keywords[_REVEAL_SPACE]) == {_SPACE_CAP}
    for node in ast.walk(service_fn):
        if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
            caps = _capability_literals(node)
            assert not ({_PERSON_CAP, _SPACE_CAP} <= caps)

    project_keywords = {
        child.arg: child.value
        for child in ast.walk(project_fn)
        if isinstance(child, ast.keyword) and child.arg in _OWNER_SINKS
    }
    person_names = {
        child.id
        for child in ast.walk(project_keywords["first_seen_person_id"])
        if isinstance(child, ast.Name)
    }
    space_names = {
        child.id
        for child in ast.walk(project_keywords["first_seen_space_id"])
        if isinstance(child, ast.Name)
    }
    assert "first_seen_person" in person_names
    assert "first_seen_space" in space_names
    assert _REVEAL_SPACE not in person_names
    assert _REVEAL_PERSON not in space_names

    assignments = {
        target.id: node.value
        for node in ast.walk(project_fn)
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name) and target.id in {"first_seen_person", "first_seen_space"}
    }
    person_assign_names = {
        child.id
        for child in ast.walk(assignments["first_seen_person"])
        if isinstance(child, ast.Name)
    }
    space_assign_names = {
        child.id
        for child in ast.walk(assignments["first_seen_space"])
        if isinstance(child, ast.Name)
    }
    assert _REVEAL_PERSON in person_assign_names
    assert _REVEAL_SPACE not in person_assign_names
    assert _REVEAL_SPACE in space_assign_names
    assert _REVEAL_PERSON not in space_assign_names

    adapter_project_calls = [
        node
        for node in ast.walk(adapter_fn)
        if isinstance(node, ast.Call) and _attr_name(node.func) == "_project_emoji_asset"
    ]
    assert len(adapter_project_calls) == 1
    adapter_keywords = {item.arg for item in adapter_project_calls[0].keywords}
    assert {_REVEAL_PERSON, _REVEAL_SPACE} <= adapter_keywords
    assert _LEGACY_REVEAL not in adapter_keywords
