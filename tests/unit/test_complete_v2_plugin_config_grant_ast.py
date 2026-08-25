"""AST gate: complete-v2 config/grant/publish has no raw QQ fallback."""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src" / "qq_ai_bot"
OWNER_FILES = (
    SRC_ROOT / "plugin_host" / "ownership.py",
    SRC_ROOT / "plugin_host" / "repository.py",
    SRC_ROOT / "plugin_host" / "notification_repository.py",
    SRC_ROOT / "plugin_host" / "notification_delivery.py",
    SRC_ROOT / "plugin_host" / "background_turns.py",
)
_FORBIDDEN_V2_CALLS = frozenset(
    {
        "_grant_row_by_provenance",
        "get_bots",
        "resolve_send_for_target",
    }
)
_V1_NAME_MARKERS = ("legacy", "_v1_", "v1_")
_RAW_OWNER_ATTRS = frozenset(
    {
        "scope_id",
        "target_id",
        "created_by_user_id",
        "bot_user_id",
        "subject_user_id",
    }
)
_RESOLVE_FNS = frozenset(
    {
        "resolve_human_person_id",
        "resolve_active_space_id",
        "resolve_config_owners",
        "resolve_grant_target_owners",
        "require_existing_presence",
        "find_config_lineage",
        "find_grant_lineage",
        "stamp_config_owners",
        "stamp_grant_owners",
        "inherit_publication_canonicals",
    }
)
_CANONICAL_SINKS = frozenset(
    {
        "canonical_person_id",
        "canonical_space_id",
        "canonical_target_person_id",
        "canonical_target_space_id",
        "canonical_created_by_person_id",
        "canonical_presence_id",
        "canonical_conversation_id",
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


def _is_named_v2_helper(name: str) -> bool:
    lowered = name.casefold()
    return "_v2_" in lowered or lowered.endswith("_v2") or lowered.startswith("_v2")


def _is_resolve_call(node: ast.Call) -> bool:
    return _attr_name(node.func) in _RESOLVE_FNS


def _is_raw_owner_source(node: ast.expr) -> bool:
    if isinstance(node, ast.Attribute) and node.attr in _RAW_OWNER_ATTRS:
        return True
    return isinstance(node, ast.Name) and node.id in _RAW_OWNER_ATTRS


def _contains_raw_owner(node: ast.AST) -> bool:
    if isinstance(node, ast.expr) and _is_raw_owner_source(node):
        return True
    return any(_contains_raw_owner(child) for child in ast.iter_child_nodes(node))


def _is_complete_v2_test(expr: ast.expr) -> bool:
    if isinstance(expr, ast.Name) and expr.id == "complete_v2":
        return True
    if isinstance(expr, ast.Attribute) and expr.attr == "complete_v2":
        return True
    if isinstance(expr, ast.Await):
        return _is_complete_v2_test(expr.value)
    if isinstance(expr, ast.Call) and _attr_name(expr.func) in {
        "identity_runtime_is_complete_v2",
        "runtime_is_complete_v2",
        "uses_canonical_send",
    }:
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
        if self.proven_v2 and _contains_raw_owner(node):
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
        if self.proven_v2 and _attr_name(node.func) in _FORBIDDEN_V2_CALLS:
            self.hits.append(node.lineno)
        if self.proven_v2 and not _is_resolve_call(node):
            for keyword in node.keywords:
                if keyword.arg in _CANONICAL_SINKS and _contains_raw_owner(keyword.value):
                    self.hits.append(node.lineno)
        self.generic_visit(node)


def _scan_function(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[int]:
    if _is_named_v1_helper(node.name):
        return []
    visitor = _FallbackVisitor(proven_v2=_is_named_v2_helper(node.name))
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


def test_complete_v2_config_grant_ast_forbids_raw_fallback() -> None:
    failures: list[str] = []
    for path in OWNER_FILES:
        text = path.read_text(encoding="utf-8")
        assert "get_bots" not in text
        if path.name not in {"notification_delivery.py", "background_turns.py"}:
            assert "GatewayConnection" not in text
        tree = ast.parse(text)
        for lineno in _scan_module(tree):
            failures.append(f"{_posix_rel(path)}:{lineno} complete-v2 raw identity fallback")
    assert not failures, "\n".join(failures)


def test_ast_gate_catches_synthetic_v2_target_compare() -> None:
    tree = ast.parse(
        "\n".join(
            (
                "async def _load_enabled_grant(session, plugin_id, target, complete_v2):",
                "    if complete_v2:",
                "        return row.target_id == target.target_id",
                "    return row.target_id == target.target_id",
            )
        )
    )
    hits = _scan_module(tree)
    assert hits, "AST gate must catch complete-v2 raw target_id authorization"


def test_ast_gate_catches_v2_provenance_grant_lookup() -> None:
    tree = ast.parse(
        "\n".join(
            (
                "async def grant_creator(session, plugin_id, target_type, target_id, complete_v2):",
                "    if complete_v2:",
                "        return await _grant_row_by_provenance(",
                "            session, plugin_id=plugin_id,",
                "            target_type=target_type, target_id=target_id",
                "        )",
                "    return None",
            )
        )
    )
    hits = _scan_module(tree)
    assert hits, "AST gate must catch complete-v2 raw provenance grant lookup"


def test_ast_gate_catches_v2_resolve_send_for_target() -> None:
    tree = ast.parse(
        "\n".join(
            (
                "async def _resolve_via_router(self, complete_v2):",
                "    if complete_v2:",
                "        return await self._router.resolve_send_for_target(",
                "            bot_user_id=bot_user_id, target_type=target_type, target_id=target_id",
                "        )",
                "    return None",
            )
        )
    )
    hits = _scan_module(tree)
    assert hits, "AST gate must catch complete-v2 resolve_send_for_target fallback"


def test_ast_gate_allows_resolve_from_trusted_binding_input() -> None:
    tree = ast.parse(
        "\n".join(
            (
                "async def stamp_grant_owners(session, row, *, complete_v2):",
                "    if complete_v2:",
                "        row.canonical_target_person_id = await resolve_human_person_id(",
                "            session, row.target_id, complete_v2=True",
                "        )",
            )
        )
    )
    assert _scan_module(tree) == []
