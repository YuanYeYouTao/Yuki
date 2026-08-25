"""AST gate: complete-v2 Runtime Config and Emoji reads have no raw QQ fallback."""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src" / "qq_ai_bot"
OWNER_FILES = (
    SRC_ROOT / "admin" / "config_service.py",
    SRC_ROOT / "emoji" / "repository.py",
    SRC_ROOT / "identity" / "c24_scopes.py",
)
_V1_NAME_MARKERS = ("legacy", "_v1_", "v1_")
_RESOLVE_PREFIXES = (
    "resolve_live_",
    "resolve_c24_",
    "require_live_",
    "try_live_",
)
_RAW_OWNER_ATTRS = frozenset(
    {
        "scope_id",
        "user_id",
        "group_id",
        "actor_user_id",
        "owner_keys",
        "first_seen_user_id",
        "first_seen_group_id",
    }
)
_RESOLVE_FNS = frozenset(
    {
        "resolve_live_person_id",
        "resolve_live_space_id",
        "resolve_c24_user_config_scope",
        "resolve_c24_group_config_scope",
        "try_live_person_id",
        "try_live_space_id",
        "require_live_person",
        "require_live_space",
    }
)
_CANONICAL_SINKS = frozenset(
    {
        "canonical_person_id",
        "canonical_space_id",
        "canonical_actor_person_id",
        "canonical_first_seen_person_id",
        "canonical_first_seen_space_id",
    }
)


def _posix_rel(path: Path) -> str:
    return path.resolve().relative_to(REPO_ROOT).as_posix()


def _attr_name(node: ast.expr | None) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _is_named_v1_helper(name: str) -> bool:
    lowered = name.casefold()
    return any(marker in lowered for marker in _V1_NAME_MARKERS)


def _is_resolver_helper(name: str) -> bool:
    return any(name.startswith(prefix) for prefix in _RESOLVE_PREFIXES)


def _is_resolve_call(node: ast.Call) -> bool:
    return _attr_name(node.func) in _RESOLVE_FNS


def _is_raw_owner_column(node: ast.expr) -> bool:
    return isinstance(node, ast.Attribute) and node.attr in _RAW_OWNER_ATTRS


def _is_raw_owner_source(node: ast.expr) -> bool:
    if _is_raw_owner_column(node):
        return True
    return isinstance(node, ast.Name) and node.id in _RAW_OWNER_ATTRS


def _contains_raw_owner(node: ast.AST) -> bool:
    if isinstance(node, ast.expr) and _is_raw_owner_source(node):
        return True
    return any(_contains_raw_owner(child) for child in ast.iter_child_nodes(node))


def _contains_raw_owner_column(node: ast.AST) -> bool:
    if isinstance(node, ast.expr) and _is_raw_owner_column(node):
        return True
    return any(_contains_raw_owner_column(child) for child in ast.iter_child_nodes(node))


def _is_complete_v2_test(expr: ast.expr) -> bool:
    if isinstance(expr, ast.Name) and expr.id in {"complete_v2", "canonical_match"}:
        return True
    if isinstance(expr, ast.Attribute) and expr.attr == "complete_v2":
        return True
    if isinstance(expr, ast.Await):
        return _is_complete_v2_test(expr.value)
    if isinstance(expr, ast.Call) and _attr_name(expr.func) == "identity_runtime_is_complete_v2":
        return True
    if isinstance(expr, ast.BoolOp) and isinstance(expr.op, ast.And):
        return any(_is_complete_v2_test(value) for value in expr.values)
    return False


def _is_not_complete_v2_test(expr: ast.expr) -> bool:
    if isinstance(expr, ast.UnaryOp) and isinstance(expr.op, ast.Not):
        return _is_complete_v2_test(expr.operand)
    return False


class _FallbackVisitor(ast.NodeVisitor):
    def __init__(self, *, proven_v2: bool) -> None:
        self.proven_v2 = proven_v2
        self.hits: list[int] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        return

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        return

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        return

    def visit_Lambda(self, node: ast.Lambda) -> None:
        return

    def visit_If(self, node: ast.If) -> None:
        if _is_complete_v2_test(node.test):
            nested = _FallbackVisitor(proven_v2=True)
            for statement in node.body:
                nested.visit(statement)
            self.hits.extend(nested.hits)
            nested_else = _FallbackVisitor(proven_v2=False)
            for statement in node.orelse:
                nested_else.visit(statement)
            self.hits.extend(nested_else.hits)
            return
        if _is_not_complete_v2_test(node.test):
            nested = _FallbackVisitor(proven_v2=False)
            for statement in node.body:
                nested.visit(statement)
            self.hits.extend(nested.hits)
            nested_else = _FallbackVisitor(proven_v2=True)
            for statement in node.orelse:
                nested_else.visit(statement)
            self.hits.extend(nested_else.hits)
            return
        self.generic_visit(node)

    def visit_Compare(self, node: ast.Compare) -> None:
        if self.proven_v2 and _contains_raw_owner_column(node):
            self.hits.append(node.lineno)
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        sinks_assigned = any(_attr_name(target) in _CANONICAL_SINKS for target in node.targets)
        if self.proven_v2 and sinks_assigned:
            allowed_resolve = (
                isinstance(node.value, ast.Await)
                and isinstance(node.value.value, ast.Call)
                and _is_resolve_call(node.value.value)
            )
            if _contains_raw_owner(node.value) and not allowed_resolve:
                self.hits.append(node.lineno)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        if self.proven_v2 and _attr_name(node.func) == "fill_person_space_shadows":
            self.hits.append(node.lineno)
        if self.proven_v2 and not _is_resolve_call(node):
            for keyword in node.keywords:
                if keyword.arg in _CANONICAL_SINKS and _contains_raw_owner(keyword.value):
                    self.hits.append(node.lineno)
                if keyword.arg == "owner_keys":
                    self.hits.append(node.lineno)
        self.generic_visit(node)


def _scan_function(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[int]:
    if _is_named_v1_helper(node.name) or _is_resolver_helper(node.name):
        return []
    visitor = _FallbackVisitor(proven_v2=False)
    for statement in node.body:
        visitor.visit(statement)
    return visitor.hits


def _scan_module(tree: ast.AST) -> list[int]:
    hits: list[int] = []

    class Collector(ast.NodeVisitor):
        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            hits.extend(_scan_function(node))
            self.generic_visit(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            hits.extend(_scan_function(node))
            self.generic_visit(node)

    Collector().visit(tree)
    return hits


def test_complete_v2_c24_ast_forbids_raw_fallback() -> None:
    failures: list[str] = []
    for path in OWNER_FILES:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for lineno in _scan_module(tree):
            failures.append(f"{_posix_rel(path)}:{lineno} complete-v2 raw identity fallback")
    config = (SRC_ROOT / "admin" / "config_service.py").read_text(encoding="utf-8")
    assert "owner_keys" not in config
    assert "scope_id.in_" not in config
    assert not failures, "\n".join(failures)


def test_ast_gate_catches_synthetic_v2_scope_compare() -> None:
    tree = ast.parse(
        "\n".join(
            (
                "async def list_relevant(self, *, user_id, group_id):",
                "    if complete_v2:",
                "        return Model.scope_id == user_id or Model.scope_id == group_id",
                "    return Model.scope_id == user_id",
            )
        )
    )
    hits = _scan_module(tree)
    assert hits, "AST gate must catch complete-v2 raw scope_id authorization"


def test_try_live_swallows_every_identity_failure() -> None:
    tree = ast.parse((SRC_ROOT / "identity" / "c24_scopes.py").read_text(encoding="utf-8"))
    found = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name in {
            "try_live_person_id",
            "try_live_space_id",
        }:
            found += 1
            raises = [child for child in ast.walk(node) if isinstance(child, ast.Raise)]
            assert not raises, f"{node.name} must return None for every identity failure"
    assert found == 2


def test_ast_gate_allows_resolve_from_trusted_binding_input() -> None:
    tree = ast.parse(
        "\n".join(
            (
                "async def list_relevant(self, session, user_id):",
                "    if complete_v2:",
                "        person_id = await resolve_live_person_id(session, user_id)",
                "        return Model.canonical_person_id == person_id",
            )
        )
    )
    assert _scan_module(tree) == []
